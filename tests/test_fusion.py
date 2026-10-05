"""Section 5 feature, fusion, eligibility, ambiguity, and policy conformance."""

from datetime import UTC, datetime
from json import dumps as json_dumps

from pytest import mark as pytest_mark, raises as pytest_raises

from engram.artifacts import LifecycleState, validate_cached_response_artifact
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
from engram.identity import extract_standalone_identity, retrieval_representation, scope_key
from engram.repository import ArtifactRepository
from engram.reranking import TransparentLogisticReranker
from engram.resolution import (
    CandidateSource,
    EvidenceKind,
    ExpectedObjectType,
    QueryFrameBuilder,
    ResolutionOutcome,
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
FRAME_REQUEST = "Which response is supported?"
# Read-only shared candidate fields: each call site overrides its own values and validate_candidate copies the rest.
CANDIDATE_FIELDS = {
    "response": "Supported response",
    "evidence": (),
    "scope": SCOPE,
    "lifecycle": LifecycleState.ACTIVE,
    "provenance": {},
    "diagnostics": {},
}
# The default independently supported pair: a sparse match and a support-semantic match for one statement.
SUPPORTED_LEXICAL = validate_candidate(
    {
        **CANDIDATE_FIELDS,
        "candidate_id": "candidate:sparse:stmt-1",
        "statement_id": "stmt-1",
        "source": CandidateSource.SPARSE,
        "features": feature_set(values={"sparse_score": 0.95}),
        "diagnostics": {"lexical_trace": "sensitive-value"},
    }
)
SUPPORTED_SEMANTIC = validate_candidate(
    {
        **CANDIDATE_FIELDS,
        "candidate_id": "candidate:support_semantic:stmt-1",
        "statement_id": "stmt-1",
        "source": CandidateSource.SUPPORT_SEMANTIC,
        "features": feature_set(values={"semantic_score": 0.92, "support_coverage": 1.0}),
        "evidence": (evidence_reference("proposition:stmt-1", "support_semantic", EvidenceKind.SUPPORT, SCOPE),),
        "diagnostics": {"semantic_trace": "sensitive-value"},
    }
)
# Read-only accepted artifact fields; validate_cached_response_artifact copies them at every call site.
ACCEPTED_ARTIFACT_FIELDS = {
    "statement_id": "stmt-artifact",
    "generation": 1,
    "response": "Supported response",
    "query_identity": extract_standalone_identity(FRAME_REQUEST, SCOPE),
    "retrieval": retrieval_representation(FRAME_REQUEST),
    "tier": Tier.STATIC,
    "lifecycle": LifecycleState.ACTIVE,
    "scope": SCOPE,
    "support_references": (PROPOSITION_REFERENCE_A,),
    "valid_from": "",
    "valid_from_available": False,
    "valid_until": "",
    "valid_until_available": False,
    "superseded_by": "",
    "provenance": {"source_label": "released", "caller_id": "regulator", "accepted_at": "2026-08-14T12:00:00Z"},
    "statistics": {"hit_count": 3, "query_count": 4, "last_hit": "", "last_hit_available": False},
    "metadata": {},
}


def report_candidates(decision) -> list[dict]:
    report = decision.get("report", {})
    assert isinstance(report, dict)
    values = report.get("candidates", [])
    assert isinstance(values, list)
    result = values
    return result


def report_reason_codes(decision, index: int = 0) -> tuple[str, ...]:
    eligibility = report_candidates(decision)[index].get("eligibility", {})
    assert isinstance(eligibility, dict)
    assert "reason_codes" in eligibility
    values = eligibility.get("reason_codes", [])
    assert isinstance(values, list) and all(isinstance(value, str) for value in values)
    result = tuple(values)
    return result


def test_policy_is_closed_and_fingerprinted() -> None:
    policy = fusion_policy()

    assert policy_fingerprint(policy) == policy_fingerprint(fusion_policy())
    assert policy_fingerprint(policy) != policy_fingerprint({**policy, "answer_threshold": 0.79})
    assert policy.get("answer_threshold", 0.0) == 0.78
    assert policy.get("evidence_threshold", 0.0) == 0.35
    assert policy.get("ambiguity_margin", 0.0) == 0.12
    assert policy.get("minimum_independent_sources", 0) == 2
    assert policy.get("require_support_for_non_exact", False) is True
    assert policy.get("weights", {}) == {
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
        validate_candidate(
            {
                **CANDIDATE_FIELDS,
                "candidate_id": "candidate:exact:exact",
                "statement_id": "exact",
                "source": CandidateSource.EXACT,
                "features": feature_set(values={"exact_match": 1.0}),
            }
        )
    )
    lexical = normalize_validated_candidate_features(
        validate_candidate(
            {
                **CANDIDATE_FIELDS,
                "candidate_id": "candidate:sparse:sparse",
                "statement_id": "sparse",
                "source": CandidateSource.SPARSE,
                "features": feature_set(values={"sparse_score": 0.7}),
            }
        )
    )
    semantic = normalize_validated_candidate_features(
        validate_candidate(
            {
                **CANDIDATE_FIELDS,
                "candidate_id": "candidate:support_semantic:semantic",
                "statement_id": "semantic",
                "source": CandidateSource.SUPPORT_SEMANTIC,
                "features": feature_set(values={"semantic_score": 0.8}),
            }
        )
    )

    assert exact.get("values", {}).get(FusionFeature.EXACT, 0.0) == 1.0
    assert lexical.get("values", {}).get(FusionFeature.LEXICAL, 0.0) == 0.7
    assert semantic.get("values", {}).get(FusionFeature.SEMANTIC, 0.0) == 0.8
    assert "available" in semantic
    assert FusionFeature.LEXICAL not in semantic.get("available", ())


def test_deduplication_retains_contributions_diagnostics_and_evidence() -> None:
    duplicate_evidence = validate_candidate(
        {
            **SUPPORTED_LEXICAL,
            "evidence": (evidence_reference("proposition:stmt-1", "support_semantic", EvidenceKind.SUPPORT, SCOPE),),
        }
    )
    selected_frame = QueryFrameBuilder(Engram(), lambda: START_NS, lambda: NOW).build(
        FRAME_REQUEST, SCOPE, diagnostic_seed="fusion-test", budget=capture_resolution_budget(lambda: START_NS)
    )

    decision = CandidateFusionEngine(authority=permissive_candidate_authority).decide(
        selected_frame, (duplicate_evidence, SUPPORTED_SEMANTIC)
    )

    response_candidates = decision.get("response_candidates", ())
    assert decision.get("outcome", ResolutionOutcome.MISS) == ResolutionOutcome.ANSWER
    assert len(response_candidates) == 1
    assert len(response_candidates[0].get("evidence", ())) == 1
    report = report_candidates(decision)[0]
    contributions = report.get("contributions", [])
    assert isinstance(contributions, list)
    assert len(contributions) == 2
    fields = set()
    for item in contributions:
        contribution = item
        assert "diagnostic_fields" in contribution
        diagnostic_fields = contribution.get("diagnostic_fields", [])
        assert isinstance(diagnostic_fields, list) and all(isinstance(value, str) for value in diagnostic_fields)
        fields.add(tuple(diagnostic_fields))
    assert fields == {("lexical_trace",), ("semantic_trace",)}
    assert "report" in decision
    assert "sensitive-value" not in json_dumps(dict(decision.get("report", {})))
    assert decision.get("working_memory_bytes", 0) > 0


def test_transparent_fusion_answers_only_supported_independent_agreement() -> None:
    selected_frame = QueryFrameBuilder(Engram(), lambda: START_NS, lambda: NOW).build(
        FRAME_REQUEST, SCOPE, diagnostic_seed="fusion-test", budget=capture_resolution_budget(lambda: START_NS)
    )
    decision = CandidateFusionEngine(authority=permissive_candidate_authority).decide(
        selected_frame, (SUPPORTED_LEXICAL, SUPPORTED_SEMANTIC)
    )

    assert decision.get("outcome", ResolutionOutcome.MISS) == ResolutionOutcome.ANSWER
    assert decision.get("selected_candidate_available", False) is True
    assert decision.get("selected_candidate", {}).get("source", CandidateSource.EXACT) == CandidateSource.SUPPORT_SEMANTIC
    assert decision.get("confidence_available", False) is True
    assert "confidence" in decision
    assert decision.get("confidence", 0.0) >= fusion_policy().get("answer_threshold", 0.0)
    assert decision.get("reason_codes", ()) == ("answer_fusion_threshold", "answer_no_runner_up")
    candidate_report = report_candidates(decision)[0]
    score_parts = candidate_report.get("score_contributions", {})
    assert isinstance(score_parts, dict)
    assert score_parts.get("lexical", 0.0) > 0
    assert score_parts.get("semantic", 0.0) > 0
    assert score_parts.get("support", 0.0) > 0
    assert score_parts.get("agreement", 0.0) > 0


def test_fusion_fast_path_still_rejects_malformed_public_candidates() -> None:
    malformed_data: dict[str, object] = dict(SUPPORTED_LEXICAL)
    malformed_data["source"] = "removed_source"
    malformed = malformed_data
    selected_frame = QueryFrameBuilder(Engram(), lambda: START_NS, lambda: NOW).build(
        FRAME_REQUEST, SCOPE, diagnostic_seed="fusion-test", budget=capture_resolution_budget(lambda: START_NS)
    )

    with pytest_raises(InvalidRequestError, match="candidate source"):
        CandidateFusionEngine(authority=permissive_candidate_authority).decide(selected_frame, (malformed,))


def test_fusion_result_does_not_alias_inputs_or_selected_response_candidate() -> None:
    # Copy the shared candidates before mutating them after fusion.
    values = (dict(SUPPORTED_LEXICAL), dict(SUPPORTED_SEMANTIC))
    selected_frame = QueryFrameBuilder(Engram(), lambda: START_NS, lambda: NOW).build(
        FRAME_REQUEST, SCOPE, diagnostic_seed="fusion-test", budget=capture_resolution_budget(lambda: START_NS)
    )
    decision = CandidateFusionEngine(authority=permissive_candidate_authority).decide(selected_frame, values)
    selected_candidate = decision.get("selected_candidate", {})
    original_response = selected_candidate.get("response", "")
    assert original_response

    values[0]["response"] = "mutated after fusion"
    values[1]["response"] = "also mutated after fusion"

    assert selected_candidate.get("response", "") == original_response
    assert decision.get("response_candidates", ())[0].get("response", "") == original_response

    selected_candidate["response"] = "mutated selected copy"
    assert decision.get("response_candidates", ())[0].get("response", "") == original_response


def test_missing_authority_abstains_by_default() -> None:
    selected_frame = QueryFrameBuilder(Engram(), lambda: START_NS, lambda: NOW).build(
        FRAME_REQUEST, SCOPE, diagnostic_seed="fusion-test", budget=capture_resolution_budget(lambda: START_NS)
    )
    decision = CandidateFusionEngine().decide(selected_frame, (SUPPORTED_LEXICAL, SUPPORTED_SEMANTIC))

    assert "outcome" in decision
    assert decision.get("outcome", ResolutionOutcome.MISS) == ResolutionOutcome.MISS
    assert FusionPolicyReason.AUTHORITATIVE_STATEMENT_MISSING.value in report_reason_codes(decision)


def test_single_source_or_incomplete_support_cannot_answer() -> None:
    lexical, semantic = SUPPORTED_LEXICAL, SUPPORTED_SEMANTIC
    single_frame = QueryFrameBuilder(Engram(), lambda: START_NS, lambda: NOW).build(
        FRAME_REQUEST, SCOPE, diagnostic_seed="fusion-test", budget=capture_resolution_budget(lambda: START_NS)
    )
    single = CandidateFusionEngine(authority=permissive_candidate_authority).decide(single_frame, (semantic,))
    no_support = validate_candidate({**semantic, "features": feature_set(values={"semantic_score": 0.99}), "evidence": ()})
    unsupported_frame = QueryFrameBuilder(Engram(), lambda: START_NS, lambda: NOW).build(
        FRAME_REQUEST, SCOPE, diagnostic_seed="fusion-test", budget=capture_resolution_budget(lambda: START_NS)
    )
    unsupported = CandidateFusionEngine(authority=permissive_candidate_authority).decide(unsupported_frame, (lexical, no_support))

    assert single.get("outcome", ResolutionOutcome.MISS) == ResolutionOutcome.EVIDENCE
    assert unsupported.get("outcome", ResolutionOutcome.MISS) == ResolutionOutcome.EVIDENCE
    single_reasons = report_reason_codes(single)
    unsupported_reasons = report_reason_codes(unsupported)
    assert FusionPolicyReason.INDEPENDENT_SOURCES_MISSING.value in single_reasons
    assert FusionPolicyReason.SUPPORT_INCOMPLETE.value in unsupported_reasons


def test_close_distinct_candidates_abstain_even_above_answer_threshold() -> None:
    candidates = (
        validate_candidate({**SUPPORTED_LEXICAL, "candidate_id": "candidate:sparse:stmt-a", "statement_id": "stmt-a"}),
        validate_candidate(
            {
                **SUPPORTED_SEMANTIC,
                "candidate_id": "candidate:support_semantic:stmt-a",
                "statement_id": "stmt-a",
                "evidence": (evidence_reference("proposition:stmt-a", "support_semantic", EvidenceKind.SUPPORT, SCOPE),),
            }
        ),
        validate_candidate(
            {
                **SUPPORTED_LEXICAL,
                "candidate_id": "candidate:sparse:stmt-b",
                "statement_id": "stmt-b",
                "features": feature_set(values={"sparse_score": 0.94}),
            }
        ),
        validate_candidate(
            {
                **SUPPORTED_SEMANTIC,
                "candidate_id": "candidate:support_semantic:stmt-b",
                "statement_id": "stmt-b",
                "features": feature_set(values={"semantic_score": 0.91, "support_coverage": 1.0}),
                "evidence": (evidence_reference("proposition:stmt-b", "support_semantic", EvidenceKind.SUPPORT, SCOPE),),
            }
        ),
    )
    selected_frame = QueryFrameBuilder(Engram(), lambda: START_NS, lambda: NOW).build(
        FRAME_REQUEST, SCOPE, diagnostic_seed="fusion-test", budget=capture_resolution_budget(lambda: START_NS)
    )

    decision = CandidateFusionEngine(authority=permissive_candidate_authority).decide(selected_frame, candidates)

    report = decision.get("report", {})
    assert decision.get("outcome", ResolutionOutcome.MISS) == ResolutionOutcome.EVIDENCE
    assert "selected_candidate_available" in decision
    assert decision.get("selected_candidate_available", False) is False
    assert len(decision.get("response_candidates", ())) == 2
    assert decision.get("reason_codes", ())[0] == FusionPolicyReason.AMBIGUOUS_TOP_CANDIDATES.value
    assert report.get("top_two_margin_available", False) is True
    assert "top_two_margin" in report
    assert report.get("top_two_margin", 0.0) < fusion_policy().get("ambiguity_margin", 0.0)


@pytest_mark.parametrize(
    ("changed", "expected_reason"),
    [
        ({"scope": scope_key(namespace="other")}, FusionPolicyReason.CANDIDATE_SCOPE_MISMATCH),
        ({"lifecycle": LifecycleState.RETIRED}, FusionPolicyReason.CANDIDATE_LIFECYCLE_INELIGIBLE),
    ],
)
def test_central_structural_eligibility_filters_before_scoring(changed, expected_reason) -> None:
    value = {
        **CANDIDATE_FIELDS,
        "candidate_id": "candidate:exact:stmt",
        "statement_id": "stmt",
        "source": CandidateSource.EXACT,
        "features": feature_set(values={"exact_match": 1.0}),
    }
    selected_frame = QueryFrameBuilder(Engram(), lambda: START_NS, lambda: NOW).build(
        FRAME_REQUEST, SCOPE, diagnostic_seed="fusion-test", budget=capture_resolution_budget(lambda: START_NS)
    )

    decision = CandidateFusionEngine(authority=permissive_candidate_authority).decide(
        selected_frame, (validate_candidate({**value, **changed}),)
    )

    assert "outcome" in decision
    assert decision.get("outcome", ResolutionOutcome.MISS) == ResolutionOutcome.MISS
    eligibility = report_candidates(decision)[0].get("eligibility", {})
    reason_codes = eligibility.get("reason_codes", [])
    assert isinstance(reason_codes, list) and all(isinstance(value, str) for value in reason_codes)
    assert expected_reason.value in reason_codes


def test_conflicting_responses_for_one_statement_are_not_selectable() -> None:
    selected_frame = QueryFrameBuilder(Engram(), lambda: START_NS, lambda: NOW).build(
        FRAME_REQUEST, SCOPE, diagnostic_seed="fusion-test", budget=capture_resolution_budget(lambda: START_NS)
    )

    decision = CandidateFusionEngine(authority=permissive_candidate_authority).decide(
        selected_frame,
        (SUPPORTED_LEXICAL, validate_candidate({**SUPPORTED_SEMANTIC, "response": "Conflicting response"})),
    )

    assert "outcome" in decision
    assert decision.get("outcome", ResolutionOutcome.MISS) == ResolutionOutcome.MISS
    assert FusionPolicyReason.CANDIDATE_STATEMENT_CONFLICT.value in report_reason_codes(decision)


def test_identity_and_object_type_mismatches_block_direct_selection() -> None:
    lexical_values = SUPPORTED_LEXICAL.get("features", {}).get("values", {})
    semantic_values = SUPPORTED_SEMANTIC.get("features", {}).get("values", {})
    lexical = validate_candidate(
        {**SUPPORTED_LEXICAL, "features": feature_set(values={**dict(lexical_values), "entity_match": 0.0})}
    )
    semantic = validate_candidate(
        {**SUPPORTED_SEMANTIC, "features": feature_set(values={**dict(semantic_values), "object_type_match": 0.0})}
    )
    base_frame = QueryFrameBuilder(Engram(), lambda: START_NS, lambda: NOW).build(
        FRAME_REQUEST, SCOPE, diagnostic_seed="fusion-test", budget=capture_resolution_budget(lambda: START_NS)
    )
    selected_frame = query_frame_with_changes(base_frame, {"expected_object_type": ExpectedObjectType.PERSON})

    decision = CandidateFusionEngine(authority=permissive_candidate_authority).decide(selected_frame, (lexical, semantic))

    assert decision.get("outcome", ResolutionOutcome.MISS) == ResolutionOutcome.EVIDENCE
    reasons = report_reason_codes(decision)
    assert FusionPolicyReason.IDENTITY_FEATURE_MISMATCH.value in reasons
    assert FusionPolicyReason.OBJECT_TYPE_FEATURE_MISMATCH.value in reasons


def test_exact_candidate_remains_safe_single_source_answer() -> None:
    exact = validate_candidate(
        {
            **CANDIDATE_FIELDS,
            "candidate_id": "candidate:exact:stmt-exact",
            "statement_id": "stmt-exact",
            "source": CandidateSource.EXACT,
            "features": feature_set(values={"exact_match": 1.0}),
        }
    )
    selected_frame = QueryFrameBuilder(Engram(), lambda: START_NS, lambda: NOW).build(
        FRAME_REQUEST, SCOPE, diagnostic_seed="fusion-test", budget=capture_resolution_budget(lambda: START_NS)
    )

    decision = CandidateFusionEngine(authority=permissive_candidate_authority).decide(selected_frame, (exact,))

    assert decision.get("outcome", ResolutionOutcome.MISS) == ResolutionOutcome.ANSWER
    assert decision.get("confidence", 0.0) == 1.0
    assert decision.get("reason_codes", ()) == ("answer_exact_eligible", "answer_no_runner_up")


@pytest_mark.parametrize(
    ("artifact_value", "candidate_response", "expected_reason"),
    [
        (
            validate_cached_response_artifact(
                {**ACCEPTED_ARTIFACT_FIELDS, "valid_until": "2026-08-15T11:59:59Z", "valid_until_available": True}
            ),
            "Supported response",
            FusionPolicyReason.ARTIFACT_INELIGIBLE,
        ),
        (
            validate_cached_response_artifact({**ACCEPTED_ARTIFACT_FIELDS, "metadata": {"visibility": "private"}}),
            "Supported response",
            FusionPolicyReason.OWNERSHIP_VISIBILITY_MISMATCH,
        ),
        (
            validate_cached_response_artifact(ACCEPTED_ARTIFACT_FIELDS),
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
    exact = validate_candidate(
        {
            **CANDIDATE_FIELDS,
            "candidate_id": "candidate:exact:stmt-artifact",
            "statement_id": "stmt-artifact",
            "response": candidate_response,
            "source": CandidateSource.EXACT,
            "features": feature_set(values={"exact_match": 1.0}),
        }
    )
    selected_frame = QueryFrameBuilder(engine, lambda: START_NS, lambda: NOW).build(
        FRAME_REQUEST, SCOPE, diagnostic_seed="fusion-test", budget=capture_resolution_budget(lambda: START_NS)
    )

    decision = fusion.decide(selected_frame, (exact,))

    assert "outcome" in decision
    assert decision.get("outcome", ResolutionOutcome.MISS) == ResolutionOutcome.MISS
    eligibility = report_candidates(decision)[0].get("eligibility", {})
    reason_codes = eligibility.get("reason_codes", [])
    assert isinstance(reason_codes, list) and all(isinstance(value, str) for value in reason_codes)
    assert expected_reason.value in reason_codes


def test_authoritative_features_use_explicit_support_history_and_authority() -> None:
    accepted = validate_cached_response_artifact(
        {**ACCEPTED_ARTIFACT_FIELDS, "metadata": {"authority": 0.85, "visibility": "scope", "support_complete": True}}
    )
    engine = Engram()
    engine.response_repository = ArtifactRepository((accepted,))
    fusion = CandidateFusionEngine(authority=EngramCandidateAuthority(engine))
    exact = validate_candidate(
        {
            **CANDIDATE_FIELDS,
            "candidate_id": "candidate:exact:stmt-artifact",
            "statement_id": "stmt-artifact",
            "source": CandidateSource.EXACT,
            "features": feature_set(values={"exact_match": 1.0}),
        }
    )
    selected_frame = QueryFrameBuilder(engine, lambda: START_NS, lambda: NOW).build(
        FRAME_REQUEST, SCOPE, diagnostic_seed="fusion-test", budget=capture_resolution_budget(lambda: START_NS)
    )

    decision = fusion.decide(selected_frame, (exact,))

    assert decision.get("outcome", ResolutionOutcome.MISS) == ResolutionOutcome.ANSWER
    normalized = report_candidates(decision)[0].get("normalized_features", {})
    values = normalized.get("values", {})
    assert values.get("support", 0.0) == 1.0
    assert values.get("history", 0.0) == 0.75
    assert values.get("authority", 0.0) == 0.85


def test_normalization_rejects_cross_source_spoofing_and_keeps_priority_distinct() -> None:
    sparse = normalize_validated_candidate_features(
        validate_candidate(
            {
                **CANDIDATE_FIELDS,
                "candidate_id": "candidate:sparse:sparse-spoof",
                "statement_id": "sparse-spoof",
                "source": CandidateSource.SPARSE,
                "features": feature_set(
                    values={
                        "sparse_score": 0.4,
                        "exact_match": 1.0,
                        "semantic_score": 1.0,
                        "priority": 999.0,
                        "hit_rate": 1.0,
                    }
                ),
            }
        )
    )
    semantic = normalize_validated_candidate_features(
        validate_candidate(
            {
                **CANDIDATE_FIELDS,
                "candidate_id": "candidate:support_semantic:semantic-spoof",
                "statement_id": "semantic-spoof",
                "source": CandidateSource.SUPPORT_SEMANTIC,
                "features": feature_set(values={"semantic_score": 0.8, "sparse_score": 1.0, "vector_weight": 99.0}),
            }
        )
    )

    assert "available" in sparse
    assert sparse.get("available", ()) == (FusionFeature.LEXICAL,)
    assert sparse.get("values", {}).get(FusionFeature.LEXICAL, 0.0) == 0.4
    assert semantic.get("available", ()) == (FusionFeature.SEMANTIC,)
    assert semantic.get("values", {}).get(FusionFeature.SEMANTIC, 0.0) == 0.8
    assert FusionFeature.HISTORY not in sparse.get("available", ())


def test_explicit_mismatch_uses_conservative_aggregation() -> None:
    lexical_values = SUPPORTED_LEXICAL.get("features", {}).get("values", {})
    semantic_values = SUPPORTED_SEMANTIC.get("features", {}).get("values", {})
    lexical = validate_candidate(
        {**SUPPORTED_LEXICAL, "features": feature_set(values={**dict(lexical_values), "entity_match": 1.0})}
    )
    semantic = validate_candidate(
        {**SUPPORTED_SEMANTIC, "features": feature_set(values={**dict(semantic_values), "entity_match": 0.0})}
    )
    selected_frame = QueryFrameBuilder(Engram(), lambda: START_NS, lambda: NOW).build(
        FRAME_REQUEST, SCOPE, diagnostic_seed="fusion-test", budget=capture_resolution_budget(lambda: START_NS)
    )

    decision = CandidateFusionEngine(authority=permissive_candidate_authority).decide(selected_frame, (lexical, semantic))

    assert decision.get("outcome", ResolutionOutcome.MISS) == ResolutionOutcome.EVIDENCE
    normalized = report_candidates(decision)[0].get("normalized_features", {})
    assert isinstance(normalized, dict)
    values = normalized.get("values", {})
    assert isinstance(values, dict)
    assert "entity" in values
    assert values.get("entity", 0.0) == 0.0
    assert FusionPolicyReason.IDENTITY_FEATURE_MISMATCH.value in report_reason_codes(decision)


def test_order_invariance_canonicalizes_candidates_and_evidence() -> None:
    lexical = SUPPORTED_LEXICAL
    extra = evidence_reference("proposition-extra", "support_semantic", EvidenceKind.SUPPORT, SCOPE)
    semantic = validate_candidate({**SUPPORTED_SEMANTIC, "evidence": (*SUPPORTED_SEMANTIC.get("evidence", ()), extra)})
    engine = CandidateFusionEngine(authority=permissive_candidate_authority)
    first_frame = QueryFrameBuilder(Engram(), lambda: START_NS, lambda: NOW).build(
        FRAME_REQUEST, SCOPE, diagnostic_seed="fusion-test", budget=capture_resolution_budget(lambda: START_NS)
    )
    second_frame = QueryFrameBuilder(Engram(), lambda: START_NS, lambda: NOW).build(
        FRAME_REQUEST, SCOPE, diagnostic_seed="fusion-test", budget=capture_resolution_budget(lambda: START_NS)
    )

    first = engine.decide(first_frame, (lexical, semantic))
    second = engine.decide(second_frame, (semantic, lexical))

    assert first == second


def test_per_candidate_filtering_preserves_valid_independent_group() -> None:
    excluded = validate_candidate(
        {
            **CANDIDATE_FIELDS,
            "candidate_id": "candidate:utility:stmt-1",
            "statement_id": "stmt-1",
            "source": CandidateSource.UTILITY,
            "features": feature_set(values={"object_type_match": 1.0}),
            "scope": scope_key(namespace="other"),
        }
    )
    selected_frame = QueryFrameBuilder(Engram(), lambda: START_NS, lambda: NOW).build(
        FRAME_REQUEST, SCOPE, diagnostic_seed="fusion-test", budget=capture_resolution_budget(lambda: START_NS)
    )

    decision = CandidateFusionEngine(authority=permissive_candidate_authority).decide(
        selected_frame, (excluded, SUPPORTED_SEMANTIC, SUPPORTED_LEXICAL)
    )

    assert decision.get("outcome", ResolutionOutcome.MISS) == ResolutionOutcome.ANSWER
    report = report_candidates(decision)[0]
    assert report.get("sources", []) == ["support_semantic", "sparse"]
    assert FusionPolicyReason.CANDIDATE_SCOPE_MISMATCH.value in report_reason_codes(decision)


def test_candidate_and_evidence_identity_conflicts_abstain_deterministically() -> None:
    lexical, semantic = SUPPORTED_LEXICAL, SUPPORTED_SEMANTIC
    lexical_candidate_id = lexical.get("candidate_id", "")
    assert lexical_candidate_id
    conflicting_id = validate_candidate({**semantic, "candidate_id": lexical_candidate_id})
    candidate_frame = QueryFrameBuilder(Engram(), lambda: START_NS, lambda: NOW).build(
        FRAME_REQUEST, SCOPE, diagnostic_seed="fusion-test", budget=capture_resolution_budget(lambda: START_NS)
    )
    candidate_conflict = CandidateFusionEngine(authority=permissive_candidate_authority).decide(
        candidate_frame, (lexical, conflicting_id)
    )
    reference = evidence_reference("proposition-conflict", "support_semantic", EvidenceKind.SUPPORT, SCOPE)
    reference_variant = validate_evidence_reference({**reference, "resolver": "different-resolver"})
    semantic = validate_candidate({**semantic, "evidence": (reference, reference_variant)})
    evidence_frame = QueryFrameBuilder(Engram(), lambda: START_NS, lambda: NOW).build(
        FRAME_REQUEST, SCOPE, diagnostic_seed="fusion-test", budget=capture_resolution_budget(lambda: START_NS)
    )
    evidence_conflict = CandidateFusionEngine(authority=permissive_candidate_authority).decide(evidence_frame, (lexical, semantic))

    assert "outcome" in candidate_conflict
    assert candidate_conflict.get("outcome", ResolutionOutcome.MISS) == ResolutionOutcome.MISS
    assert FusionPolicyReason.CANDIDATE_ID_CONFLICT.value in report_reason_codes(candidate_conflict)
    assert evidence_conflict.get("outcome", ResolutionOutcome.MISS) == ResolutionOutcome.EVIDENCE
    assert FusionPolicyReason.EVIDENCE_REFERENCE_CONFLICT.value in report_reason_codes(evidence_conflict)


def test_authority_revalidates_artifact_generation_and_support() -> None:
    accepted = validate_cached_response_artifact(ACCEPTED_ARTIFACT_FIELDS)
    engine = Engram()
    engine.response_repository = ArtifactRepository((accepted,))
    fusion = CandidateFusionEngine(authority=EngramCandidateAuthority(engine))
    stale_generation = validate_candidate(
        {
            **CANDIDATE_FIELDS,
            "candidate_id": "candidate:exact:stmt-artifact",
            "statement_id": "stmt-artifact",
            "source": CandidateSource.EXACT,
            "features": feature_set(values={"exact_match": 1.0}),
            "provenance": {"generation": 0},
        }
    )
    lexical = validate_candidate(
        {
            **CANDIDATE_FIELDS,
            "candidate_id": "candidate:sparse:stmt-artifact",
            "statement_id": "stmt-artifact",
            "source": CandidateSource.SPARSE,
            "features": feature_set(values={"sparse_score": 0.95}),
        }
    )
    stale_support = validate_candidate(
        {
            **CANDIDATE_FIELDS,
            "candidate_id": "candidate:support_semantic:stmt-artifact",
            "statement_id": "stmt-artifact",
            "source": CandidateSource.SUPPORT_SEMANTIC,
            "features": feature_set(values={"semantic_score": 0.95, "support_coverage": 1.0}),
            "evidence": (evidence_reference("proposition-stale", "support_semantic", EvidenceKind.SUPPORT, SCOPE),),
        }
    )
    generation_frame = QueryFrameBuilder(engine, lambda: START_NS, lambda: NOW).build(
        FRAME_REQUEST, SCOPE, diagnostic_seed="fusion-test", budget=capture_resolution_budget(lambda: START_NS)
    )
    support_frame = QueryFrameBuilder(engine, lambda: START_NS, lambda: NOW).build(
        FRAME_REQUEST, SCOPE, diagnostic_seed="fusion-test", budget=capture_resolution_budget(lambda: START_NS)
    )

    generation_decision = fusion.decide(generation_frame, (stale_generation,))
    support_decision = fusion.decide(support_frame, (lexical, stale_support))

    assert "outcome" in generation_decision
    assert generation_decision.get("outcome", ResolutionOutcome.MISS) == ResolutionOutcome.MISS
    assert FusionPolicyReason.AUTHORITATIVE_GENERATION_MISMATCH.value in report_reason_codes(generation_decision)
    assert support_decision.get("outcome", ResolutionOutcome.MISS) == ResolutionOutcome.EVIDENCE
    assert FusionPolicyReason.SUPPORT_REFERENCE_STALE.value in report_reason_codes(support_decision)


def test_explicit_conflict_and_fusion_memory_exhaustion_are_typed() -> None:
    lexical_values = SUPPORTED_LEXICAL.get("features", {}).get("values", {})
    lexical = validate_candidate(
        {**SUPPORTED_LEXICAL, "features": feature_set(values={**dict(lexical_values), "explicit_conflict": 1.0})}
    )
    conflict_frame = QueryFrameBuilder(Engram(), lambda: START_NS, lambda: NOW).build(
        FRAME_REQUEST, SCOPE, diagnostic_seed="fusion-test", budget=capture_resolution_budget(lambda: START_NS)
    )
    conflict = CandidateFusionEngine(authority=permissive_candidate_authority).decide(conflict_frame, (lexical, SUPPORTED_SEMANTIC))
    selected_frame = QueryFrameBuilder(Engram(), lambda: START_NS, lambda: NOW).build(
        FRAME_REQUEST, SCOPE, diagnostic_seed="fusion-test", budget=capture_resolution_budget(lambda: START_NS)
    )
    memory_frame = query_frame_with_changes(
        selected_frame,
        {"budget": validate_resolution_budget({**selected_frame.get("budget", {}), "max_working_memory_bytes": 1})},
    )
    memory = CandidateFusionEngine(authority=permissive_candidate_authority).decide(
        memory_frame, (SUPPORTED_LEXICAL, SUPPORTED_SEMANTIC)
    )

    assert conflict.get("outcome", ResolutionOutcome.MISS) == ResolutionOutcome.EVIDENCE
    assert FusionPolicyReason.EXPLICIT_CONFLICT.value in report_reason_codes(conflict)
    assert "outcome" in memory
    assert memory.get("outcome", ResolutionOutcome.MISS) == ResolutionOutcome.MISS
    assert memory.get("reason_codes", ()) == (FusionPolicyReason.FUSION_MEMORY_EXHAUSTED.value,)
    assert memory.get("working_memory_bytes", 0) == memory_frame.get("budget", {}).get("max_working_memory_bytes", 0)


def test_explicit_fusion_memory_allowance_is_concrete_and_enforced() -> None:
    selected_frame = QueryFrameBuilder(Engram(), lambda: START_NS, lambda: NOW).build(
        FRAME_REQUEST, SCOPE, diagnostic_seed="fusion-test", budget=capture_resolution_budget(lambda: START_NS)
    )
    lexical, semantic = SUPPORTED_LEXICAL, SUPPORTED_SEMANTIC
    engine = CandidateFusionEngine(authority=permissive_candidate_authority)
    required = engine.decide(selected_frame, (lexical, semantic)).get("working_memory_bytes", 0)
    assert required > 0

    exhausted = engine.decide(
        selected_frame,
        (lexical, semantic),
        working_memory_limit=required - 1,
        working_memory_limit_available=True,
    )

    assert "outcome" in exhausted
    assert exhausted.get("outcome", ResolutionOutcome.MISS) == ResolutionOutcome.MISS
    assert exhausted.get("reason_codes", ()) == (FusionPolicyReason.FUSION_MEMORY_EXHAUSTED.value,)
    assert exhausted.get("working_memory_bytes", 0) == required - 1
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
        validate_candidate(value)
        for index, score in enumerate(scores)
        for value in (
            {
                **SUPPORTED_LEXICAL,
                "candidate_id": f"candidate:sparse:s{index}",
                "statement_id": f"s{index}",
                "features": feature_set(values={"sparse_score": score}),
            },
            {
                **SUPPORTED_SEMANTIC,
                "candidate_id": f"candidate:support_semantic:s{index}",
                "statement_id": f"s{index}",
                "features": feature_set(values={"semantic_score": score, "support_coverage": 1.0}),
                "evidence": (evidence_reference(f"proposition:s{index}", "support_semantic", EvidenceKind.SUPPORT, SCOPE),),
            },
        )
    )
    selected_frame = QueryFrameBuilder(Engram(), lambda: START_NS, lambda: NOW).build(
        FRAME_REQUEST, SCOPE, diagnostic_seed="fusion-test", budget=capture_resolution_budget(lambda: START_NS)
    )
    baseline = CandidateFusionEngine(authority=permissive_candidate_authority).decide(selected_frame, candidates)
    reranker = TransparentLogisticReranker(reranker_config(enabled=True, shortlist_size=shortlist_size))

    reranked = CandidateFusionEngine(authority=permissive_candidate_authority, reranker=reranker).decide(selected_frame, candidates)

    assert reranked.get("report", {}).get("reranker", {}).get("applied", False) is True
    assert baseline.get("outcome", ResolutionOutcome.MISS) == ResolutionOutcome.EVIDENCE
    assert reranked.get("outcome", ResolutionOutcome.MISS) == baseline.get("outcome", ResolutionOutcome.MISS)
    assert "confidence" in reranked
    assert "confidence" in baseline
    assert reranked.get("confidence", 0.0) == baseline.get("confidence", 0.0)
