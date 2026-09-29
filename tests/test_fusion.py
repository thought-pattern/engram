"""Section 5 feature, fusion, eligibility, ambiguity, and policy conformance."""

from collections.abc import Mapping
from datetime import UTC, datetime
from json import dumps as json_dumps

from pytest import mark as pytest_mark, raises as pytest_raises

from engram.artifacts import LifecycleState, artifact_provenance, artifact_statistics, cached_response_artifact
from engram.config import reranker_config
from engram.constants import Tier
from engram.core import Engram
from engram.errors import InvalidRequestError
from engram.fusion import (
    CandidateFusionEngine,
    EngramCandidateAuthority,
    FusionFeature,
    FusionPolicyReason,
    fusion_policy,
    fusion_policy_from_dict,
    fusion_policy_to_dict,
    normalize_validated_candidate_features,
    permissive_candidate_authority,
    policy_fingerprint,
    validate_fusion_policy,
)
from engram.identity import build_retrieval_representation, build_standalone_identity, scope_key
from engram.repository import ArtifactRepository
from engram.reranking import TransparentLogisticReranker
from engram.resolution import (
    CandidateSource,
    EvidenceKind,
    ExpectedObjectType,
    QueryFrameBuilder,
    ResolutionOutcome,
    candidate as resolution_candidate,
    capture_resolution_budget,
    evidence_reference,
    feature_set,
    query_frame_with_changes,
    validate_candidate,
    validate_evidence_reference,
    validate_resolution_budget,
)

from .support_fixtures import PROPOSITION_REFERENCE_A

NOW = datetime(2026, 8, 15, 12, 0, tzinfo=UTC)
START_NS = 1_000_000_000
SCOPE = scope_key(namespace="tenant-a", context_fingerprint="context-a")
EMPTY_TEST_MAPPING = {}


def conformance_fusion() -> CandidateFusionEngine:
    result = CandidateFusionEngine(authority=permissive_candidate_authority)
    return result


def frame(engine: object = ()) -> dict:
    selected = engine if isinstance(engine, Engram) else Engram()
    budget = capture_resolution_budget(lambda: START_NS)
    result = QueryFrameBuilder(selected, lambda: START_NS, lambda: NOW).build(
        "Which response is supported?",
        SCOPE,
        diagnostic_seed="fusion-test",
        budget=budget,
    )
    return result


def support_reference(proposition_id: str = "proposition-1") -> dict:
    result = evidence_reference(proposition_id, "support_semantic", EvidenceKind.SUPPORT, SCOPE)
    return result


def candidate(
    statement_id: str,
    source: CandidateSource,
    features: dict[str, float],
    *,
    response: str = "Supported response",
    evidence: tuple[dict, ...] = (),
    scope: dict = SCOPE,
    lifecycle: LifecycleState = LifecycleState.ACTIVE,
    provenance: dict[str, object] = EMPTY_TEST_MAPPING,
    diagnostics: dict[str, object] = EMPTY_TEST_MAPPING,
) -> dict:
    result = resolution_candidate(
        candidate_id=f"candidate:{source.value}:{statement_id}",
        statement_id=statement_id,
        response=response,
        source=source,
        features=feature_set(values=features),
        evidence=evidence,
        scope=scope,
        lifecycle=lifecycle,
        provenance=provenance,
        diagnostics=diagnostics,
    )
    return result


def supported_pair(statement_id: str = "stmt-1", *, semantic: float = 0.92, lexical: float = 0.95):
    result = (
        candidate(
            statement_id,
            CandidateSource.SPARSE,
            {"sparse_score": lexical},
            diagnostics={"lexical_trace": "sensitive-value"},
        ),
        candidate(
            statement_id,
            CandidateSource.SUPPORT_SEMANTIC,
            {"semantic_score": semantic, "support_coverage": 1.0},
            evidence=(support_reference(f"proposition:{statement_id}"),),
            diagnostics={"semantic_trace": "sensitive-value"},
        ),
    )
    return result


def artifact(
    *,
    response: str = "Supported response",
    valid_until: str = "",
    valid_until_available: bool = False,
    metadata: dict[str, object] = EMPTY_TEST_MAPPING,
) -> dict:
    result = cached_response_artifact(
        statement_id="stmt-artifact",
        generation=1,
        response=response,
        query_identity=build_standalone_identity("Which response is supported?", SCOPE),
        retrieval=build_retrieval_representation("Which response is supported?"),
        tier=Tier.STATIC,
        lifecycle=LifecycleState.ACTIVE,
        scope=SCOPE,
        support_references=(PROPOSITION_REFERENCE_A,),
        valid_from="",
        valid_from_available=False,
        valid_until=valid_until,
        valid_until_available=valid_until_available,
        superseded_by="",
        provenance=artifact_provenance("released", "regulator", "2026-08-14T12:00:00Z"),
        statistics=artifact_statistics(query_count=4, hit_count=3),
        metadata=metadata,
    )
    return result


def report_candidates(decision) -> list[dict]:
    values = decision["report"]["candidates"]
    assert isinstance(values, list)
    result = values
    return result


def report_reason_codes(decision, index: int = 0) -> tuple[str, ...]:
    eligibility = report_candidates(decision)[index]["eligibility"]
    assert isinstance(eligibility, Mapping)
    values = eligibility["reason_codes"]
    assert isinstance(values, list) and all(isinstance(value, str) for value in values)
    result = tuple(values)
    return result


def test_policy_is_closed_and_fingerprinted() -> None:
    policy = fusion_policy()

    assert policy_fingerprint(policy) == policy_fingerprint(fusion_policy())
    assert policy_fingerprint(policy) != policy_fingerprint({**policy, "answer_threshold": 0.79})
    assert policy["answer_threshold"] == 0.78
    assert policy["evidence_threshold"] == 0.35
    assert policy["ambiguity_margin"] == 0.12
    assert policy["minimum_independent_sources"] == 2
    assert policy["require_support_for_non_exact"] is True
    assert policy["weights"] == {
        FusionFeature.EXACT: 1.0,
        FusionFeature.LEXICAL: 1.0,
        FusionFeature.SEMANTIC: 1.0,
        FusionFeature.ENTITY: 0.4,
        FusionFeature.RELATION: 0.5,
        FusionFeature.OBJECT_TYPE: 0.3,
        FusionFeature.SUPPORT: 0.8,
        FusionFeature.HISTORY: 0.2,
        FusionFeature.FRESHNESS: 0.2,
        FusionFeature.AUTHORITY: 0.5,
        FusionFeature.AGREEMENT: 0.9,
        FusionFeature.MARGIN: 0.0,
    }
    invalid = fusion_policy_to_dict(policy)
    invalid["unknown"] = True
    with pytest_raises(InvalidRequestError, match="invalid fields"):
        fusion_policy_from_dict(invalid)
    with pytest_raises(InvalidRequestError, match="between 0 and 1"):
        validate_fusion_policy({**policy, "answer_threshold": 1.1})


def test_resolver_scores_use_source_specific_normalization() -> None:
    exact = normalize_validated_candidate_features(
        validate_candidate(candidate("exact", CandidateSource.EXACT, {"exact_match": 1.0}))
    )
    lexical = normalize_validated_candidate_features(
        validate_candidate(candidate("sparse", CandidateSource.SPARSE, {"sparse_score": 0.7}))
    )
    semantic = normalize_validated_candidate_features(
        validate_candidate(candidate("semantic", CandidateSource.SUPPORT_SEMANTIC, {"semantic_score": 0.8}))
    )

    assert exact["values"][FusionFeature.EXACT] == 1.0
    assert lexical["values"][FusionFeature.LEXICAL] == 0.7
    assert semantic["values"][FusionFeature.SEMANTIC] == 0.8
    assert FusionFeature.LEXICAL not in semantic["available"]


def test_deduplication_retains_contributions_diagnostics_and_evidence() -> None:
    values = supported_pair()
    duplicate_evidence = validate_candidate({**values[0], "evidence": (support_reference("proposition:stmt-1"),)})

    decision = conformance_fusion().decide(frame(), (duplicate_evidence, values[1]))

    assert decision["outcome"] == ResolutionOutcome.ANSWER
    assert len(decision["response_candidates"]) == 1
    assert len(decision["response_candidates"][0]["evidence"]) == 1
    report = report_candidates(decision)[0]
    contributions = report["contributions"]
    assert isinstance(contributions, list)
    assert len(contributions) == 2
    fields = set()
    for item in contributions:
        contribution = item
        diagnostic_fields = contribution["diagnostic_fields"]
        assert isinstance(diagnostic_fields, list) and all(isinstance(value, str) for value in diagnostic_fields)
        fields.add(tuple(diagnostic_fields))
    assert fields == {("lexical_trace",), ("semantic_trace",)}
    assert "sensitive-value" not in json_dumps(dict(decision["report"]))
    assert decision["working_memory_bytes"] > 0


def test_transparent_fusion_answers_only_supported_independent_agreement() -> None:
    decision = conformance_fusion().decide(frame(), supported_pair())

    assert decision["outcome"] == ResolutionOutcome.ANSWER
    assert decision["selected_candidate_available"] is True
    assert decision["selected_candidate"]["source"] == CandidateSource.SUPPORT_SEMANTIC
    assert decision["confidence_available"] is True
    assert decision["confidence"] >= fusion_policy()["answer_threshold"]
    assert decision["reason_codes"] == ("answer_fusion_threshold", "answer_no_runner_up")
    candidate_report = report_candidates(decision)[0]
    score_parts = candidate_report["score_contributions"]
    assert isinstance(score_parts, Mapping)
    assert score_parts["lexical"] > 0
    assert score_parts["semantic"] > 0
    assert score_parts["support"] > 0
    assert score_parts["agreement"] > 0


def test_fusion_fast_path_still_rejects_malformed_public_candidates() -> None:
    malformed_data: dict[str, object] = dict(supported_pair()[0])
    malformed_data["source"] = "removed_source"
    malformed = malformed_data

    with pytest_raises(InvalidRequestError, match="candidate source"):
        conformance_fusion().decide(frame(), (malformed,))


def test_fusion_result_does_not_alias_inputs_or_selected_response_candidate() -> None:
    values = supported_pair()
    decision = conformance_fusion().decide(frame(), values)
    original_response = decision["selected_candidate"]["response"]

    values[0]["response"] = "mutated after fusion"
    values[1]["response"] = "also mutated after fusion"

    assert decision["selected_candidate"]["response"] == original_response
    assert decision["response_candidates"][0]["response"] == original_response

    decision["selected_candidate"]["response"] = "mutated selected copy"
    assert decision["response_candidates"][0]["response"] == original_response


def test_missing_authority_abstains_by_default() -> None:
    decision = CandidateFusionEngine().decide(frame(), supported_pair())

    assert decision["outcome"] == ResolutionOutcome.MISS
    assert FusionPolicyReason.AUTHORITATIVE_STATEMENT_MISSING.value in report_reason_codes(decision)


def test_single_source_or_incomplete_support_cannot_answer() -> None:
    lexical, semantic = supported_pair()
    single = conformance_fusion().decide(frame(), (semantic,))
    no_support = validate_candidate({**semantic, "features": feature_set(values={"semantic_score": 0.99}), "evidence": ()})
    unsupported = conformance_fusion().decide(frame(), (lexical, no_support))

    assert single["outcome"] == ResolutionOutcome.EVIDENCE
    assert unsupported["outcome"] == ResolutionOutcome.EVIDENCE
    single_reasons = report_reason_codes(single)
    unsupported_reasons = report_reason_codes(unsupported)
    assert FusionPolicyReason.INDEPENDENT_SOURCES_MISSING.value in single_reasons
    assert FusionPolicyReason.SUPPORT_INCOMPLETE.value in unsupported_reasons


def test_close_distinct_candidates_abstain_even_above_answer_threshold() -> None:
    candidates = (*supported_pair("stmt-a"), *supported_pair("stmt-b", semantic=0.91, lexical=0.94))

    decision = conformance_fusion().decide(frame(), candidates)

    assert decision["outcome"] == ResolutionOutcome.EVIDENCE
    assert decision["selected_candidate_available"] is False
    assert len(decision["response_candidates"]) == 2
    assert decision["reason_codes"][0] == FusionPolicyReason.AMBIGUOUS_TOP_CANDIDATES.value
    assert decision["report"]["top_two_margin_available"] is True
    assert decision["report"]["top_two_margin"] < fusion_policy()["ambiguity_margin"]


@pytest_mark.parametrize(
    ("changed", "expected_reason"),
    [
        ({"scope": scope_key(namespace="other")}, FusionPolicyReason.CANDIDATE_SCOPE_MISMATCH),
        ({"lifecycle": LifecycleState.RETIRED}, FusionPolicyReason.CANDIDATE_LIFECYCLE_INELIGIBLE),
    ],
)
def test_central_structural_eligibility_filters_before_scoring(changed, expected_reason) -> None:
    value = candidate("stmt", CandidateSource.EXACT, {"exact_match": 1.0})

    decision = conformance_fusion().decide(frame(), (validate_candidate({**value, **changed}),))

    assert decision["outcome"] == ResolutionOutcome.MISS
    eligibility = report_candidates(decision)[0]["eligibility"]
    reason_codes = eligibility["reason_codes"]
    assert isinstance(reason_codes, list) and all(isinstance(value, str) for value in reason_codes)
    assert expected_reason.value in reason_codes


def test_conflicting_responses_for_one_statement_are_not_selectable() -> None:
    first, second = supported_pair()

    decision = conformance_fusion().decide(
        frame(),
        (first, validate_candidate({**second, "response": "Conflicting response"})),
    )

    assert decision["outcome"] == ResolutionOutcome.MISS
    assert FusionPolicyReason.CANDIDATE_STATEMENT_CONFLICT.value in report_reason_codes(decision)


def test_identity_and_object_type_mismatches_block_direct_selection() -> None:
    lexical, semantic = supported_pair()
    lexical = validate_candidate(
        {**lexical, "features": feature_set(values={**dict(lexical["features"]["values"]), "entity_match": 0.0})}
    )
    semantic = validate_candidate(
        {**semantic, "features": feature_set(values={**dict(semantic["features"]["values"]), "object_type_match": 0.0})}
    )
    selected_frame = query_frame_with_changes(frame(), {"expected_object_type": ExpectedObjectType.PERSON})

    decision = conformance_fusion().decide(selected_frame, (lexical, semantic))

    assert decision["outcome"] == ResolutionOutcome.EVIDENCE
    reasons = report_reason_codes(decision)
    assert FusionPolicyReason.IDENTITY_FEATURE_MISMATCH.value in reasons
    assert FusionPolicyReason.OBJECT_TYPE_FEATURE_MISMATCH.value in reasons


def test_exact_candidate_remains_safe_single_source_answer() -> None:
    exact = candidate("stmt-exact", CandidateSource.EXACT, {"exact_match": 1.0})

    decision = conformance_fusion().decide(frame(), (exact,))

    assert decision["outcome"] == ResolutionOutcome.ANSWER
    assert decision["confidence"] == 1.0
    assert decision["reason_codes"] == ("answer_exact_eligible", "answer_no_runner_up")


@pytest_mark.parametrize(
    ("artifact_value", "candidate_response", "expected_reason"),
    [
        (
            artifact(valid_until="2026-08-15T11:59:59Z", valid_until_available=True),
            "Supported response",
            FusionPolicyReason.ARTIFACT_INELIGIBLE,
        ),
        (
            artifact(metadata={"visibility": "private"}),
            "Supported response",
            FusionPolicyReason.OWNERSHIP_VISIBILITY_MISMATCH,
        ),
        (
            artifact(),
            "Changed response",
            FusionPolicyReason.AUTHORITATIVE_RESPONSE_MISMATCH,
        ),
    ],
)
def test_authoritative_revalidation_blocks_stale_hidden_or_changed_state(
    artifact_value, candidate_response, expected_reason
) -> None:
    engine = Engram()
    engine.response_repository = ArtifactRepository((artifact_value,))
    fusion = CandidateFusionEngine(authority=EngramCandidateAuthority(engine))
    exact = candidate("stmt-artifact", CandidateSource.EXACT, {"exact_match": 1.0}, response=candidate_response)

    decision = fusion.decide(frame(engine), (exact,))

    assert decision["outcome"] == ResolutionOutcome.MISS
    eligibility = report_candidates(decision)[0]["eligibility"]
    reason_codes = eligibility["reason_codes"]
    assert isinstance(reason_codes, list) and all(isinstance(value, str) for value in reason_codes)
    assert expected_reason.value in reason_codes


def test_authoritative_features_use_explicit_support_history_and_authority() -> None:
    accepted = artifact(metadata={"authority": 0.85, "visibility": "scope", "support_complete": True})
    engine = Engram()
    engine.response_repository = ArtifactRepository((accepted,))
    fusion = CandidateFusionEngine(authority=EngramCandidateAuthority(engine))
    exact = candidate("stmt-artifact", CandidateSource.EXACT, {"exact_match": 1.0})

    decision = fusion.decide(frame(engine), (exact,))

    assert decision["outcome"] == ResolutionOutcome.ANSWER
    normalized = report_candidates(decision)[0]["normalized_features"]
    values = normalized["values"]
    assert values["support"] == 1.0
    assert values["history"] == 0.75
    assert values["authority"] == 0.85


def test_normalization_rejects_cross_source_spoofing_and_keeps_priority_distinct() -> None:
    sparse = normalize_validated_candidate_features(
        validate_candidate(
            candidate(
                "sparse-spoof",
                CandidateSource.SPARSE,
                {
                    "sparse_score": 0.4,
                    "exact_match": 1.0,
                    "semantic_score": 1.0,
                    "priority": 999.0,
                    "hit_rate": 1.0,
                },
            )
        )
    )
    semantic = normalize_validated_candidate_features(
        validate_candidate(
            candidate(
                "semantic-spoof",
                CandidateSource.SUPPORT_SEMANTIC,
                {"semantic_score": 0.8, "sparse_score": 1.0, "vector_weight": 99.0},
            )
        )
    )

    assert sparse["available"] == (FusionFeature.LEXICAL,)
    assert sparse["values"][FusionFeature.LEXICAL] == 0.4
    assert semantic["available"] == (FusionFeature.SEMANTIC,)
    assert semantic["values"][FusionFeature.SEMANTIC] == 0.8
    assert FusionFeature.HISTORY not in sparse["available"]


def test_explicit_mismatch_uses_conservative_aggregation() -> None:
    lexical, semantic = supported_pair()
    lexical = validate_candidate(
        {**lexical, "features": feature_set(values={**dict(lexical["features"]["values"]), "entity_match": 1.0})}
    )
    semantic = validate_candidate(
        {**semantic, "features": feature_set(values={**dict(semantic["features"]["values"]), "entity_match": 0.0})}
    )

    decision = conformance_fusion().decide(frame(), (lexical, semantic))

    assert decision["outcome"] == ResolutionOutcome.EVIDENCE
    normalized = report_candidates(decision)[0]["normalized_features"]
    assert isinstance(normalized, Mapping)
    values = normalized["values"]
    assert isinstance(values, Mapping)
    assert values["entity"] == 0.0
    assert FusionPolicyReason.IDENTITY_FEATURE_MISMATCH.value in report_reason_codes(decision)


def test_order_invariance_canonicalizes_candidates_and_evidence() -> None:
    lexical, semantic = supported_pair()
    extra = support_reference("proposition-extra")
    semantic = validate_candidate({**semantic, "evidence": (*semantic["evidence"], extra)})
    engine = conformance_fusion()

    first = engine.decide(frame(), (lexical, semantic))
    second = engine.decide(frame(), (semantic, lexical))

    assert first == second


def test_per_candidate_filtering_preserves_valid_independent_group() -> None:
    lexical, semantic = supported_pair()
    excluded = candidate(
        "stmt-1",
        CandidateSource.UTILITY,
        {"object_type_match": 1.0},
        scope=scope_key(namespace="other"),
    )

    decision = conformance_fusion().decide(frame(), (excluded, semantic, lexical))

    assert decision["outcome"] == ResolutionOutcome.ANSWER
    report = report_candidates(decision)[0]
    assert report["sources"] == ["support_semantic", "sparse"]
    assert FusionPolicyReason.CANDIDATE_SCOPE_MISMATCH.value in report_reason_codes(decision)


def test_candidate_and_evidence_identity_conflicts_abstain_deterministically() -> None:
    lexical, semantic = supported_pair()
    conflicting_id = validate_candidate({**semantic, "candidate_id": lexical["candidate_id"]})
    candidate_conflict = conformance_fusion().decide(frame(), (lexical, conflicting_id))
    reference = support_reference("proposition-conflict")
    reference_variant = validate_evidence_reference({**reference, "resolver": "different-resolver"})
    semantic = validate_candidate({**semantic, "evidence": (reference, reference_variant)})
    evidence_conflict = conformance_fusion().decide(frame(), (lexical, semantic))

    assert candidate_conflict["outcome"] == ResolutionOutcome.MISS
    assert FusionPolicyReason.CANDIDATE_ID_CONFLICT.value in report_reason_codes(candidate_conflict)
    assert evidence_conflict["outcome"] == ResolutionOutcome.EVIDENCE
    assert FusionPolicyReason.EVIDENCE_REFERENCE_CONFLICT.value in report_reason_codes(evidence_conflict)


def test_authority_revalidates_artifact_generation_and_support() -> None:
    accepted = artifact()
    engine = Engram()
    engine.response_repository = ArtifactRepository((accepted,))
    fusion = CandidateFusionEngine(authority=EngramCandidateAuthority(engine))
    stale_generation = candidate(
        "stmt-artifact",
        CandidateSource.EXACT,
        {"exact_match": 1.0},
        provenance={"generation": 0},
    )
    lexical = candidate("stmt-artifact", CandidateSource.SPARSE, {"sparse_score": 0.95})
    stale_support = candidate(
        "stmt-artifact",
        CandidateSource.SUPPORT_SEMANTIC,
        {"semantic_score": 0.95, "support_coverage": 1.0},
        evidence=(support_reference("proposition-stale"),),
    )

    generation_decision = fusion.decide(frame(engine), (stale_generation,))
    support_decision = fusion.decide(frame(engine), (lexical, stale_support))

    assert generation_decision["outcome"] == ResolutionOutcome.MISS
    assert FusionPolicyReason.AUTHORITATIVE_GENERATION_MISMATCH.value in report_reason_codes(generation_decision)
    assert support_decision["outcome"] == ResolutionOutcome.EVIDENCE
    assert FusionPolicyReason.SUPPORT_REFERENCE_STALE.value in report_reason_codes(support_decision)


def test_explicit_conflict_and_fusion_memory_exhaustion_are_typed() -> None:
    lexical, semantic = supported_pair()
    lexical = validate_candidate(
        {**lexical, "features": feature_set(values={**dict(lexical["features"]["values"]), "explicit_conflict": 1.0})}
    )
    conflict = conformance_fusion().decide(frame(), (lexical, semantic))
    selected_frame = frame()
    memory_frame = query_frame_with_changes(
        selected_frame,
        {"budget": validate_resolution_budget({**selected_frame["budget"], "max_working_memory_bytes": 1})},
    )
    memory = conformance_fusion().decide(memory_frame, supported_pair())

    assert conflict["outcome"] == ResolutionOutcome.EVIDENCE
    assert FusionPolicyReason.EXPLICIT_CONFLICT.value in report_reason_codes(conflict)
    assert memory["outcome"] == ResolutionOutcome.MISS
    assert memory["reason_codes"] == (FusionPolicyReason.FUSION_MEMORY_EXHAUSTED.value,)
    assert memory["working_memory_bytes"] == memory_frame["budget"]["max_working_memory_bytes"]


def test_explicit_fusion_memory_allowance_is_concrete_and_enforced() -> None:
    selected_frame = frame()
    lexical, semantic = supported_pair()
    engine = conformance_fusion()
    required = engine.decide(selected_frame, (lexical, semantic))["working_memory_bytes"]

    exhausted = engine.decide(
        selected_frame,
        (lexical, semantic),
        working_memory_limit=required - 1,
        working_memory_limit_available=True,
    )

    assert exhausted["outcome"] == ResolutionOutcome.MISS
    assert exhausted["reason_codes"] == (FusionPolicyReason.FUSION_MEMORY_EXHAUSTED.value,)
    assert exhausted["working_memory_bytes"] == required - 1
    with pytest_raises(InvalidRequestError, match="requires availability"):
        engine.decide(selected_frame, (), working_memory_limit=1)


def test_policy_reasons_are_closed_content_free_identifiers() -> None:
    assert len(set(FusionPolicyReason)) == len(FusionPolicyReason)
    assert all(reason.value == reason.value.lower() and " " not in reason.value for reason in FusionPolicyReason)


@pytest_mark.parametrize("shortlist_size", [1, 2, 8])
@pytest_mark.parametrize(
    "scores",
    [(0.60,), (0.99, 0.93, 0.60)],
    ids=["one-weak-candidate", "close-leaders"],
)
def test_reranker_reorders_but_does_not_change_the_answer_policy(shortlist_size, scores) -> None:
    candidates = tuple(
        value for index, score in enumerate(scores) for value in supported_pair(f"s{index}", semantic=score, lexical=score)
    )
    selected_frame = frame()
    baseline = CandidateFusionEngine(authority=permissive_candidate_authority).decide(selected_frame, candidates)
    reranker = TransparentLogisticReranker(reranker_config(enabled=True, shortlist_size=shortlist_size))

    reranked = CandidateFusionEngine(authority=permissive_candidate_authority, reranker=reranker).decide(selected_frame, candidates)

    assert reranked["report"]["reranker"]["applied"] is True
    assert baseline["outcome"] == ResolutionOutcome.EVIDENCE
    assert reranked["outcome"] == baseline["outcome"]
    assert reranked["confidence"] == baseline["confidence"]
