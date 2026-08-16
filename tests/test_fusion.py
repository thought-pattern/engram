"""Section 5 feature, fusion, eligibility, ambiguity, and policy conformance."""

import json
from collections.abc import Mapping
from dataclasses import replace
from datetime import UTC, datetime
from typing import cast

import pytest

from engram.artifacts import ArtifactProvenance, ArtifactStatistics, CachedResponseArtifact, LifecycleState
from engram.constants import Tier
from engram.core import Engram
from engram.errors import InvalidRequestError
from engram.fusion import (
    FEATURE_DEFINITIONS,
    PERMISSIVE_CANDIDATE_AUTHORITY,
    CandidateEligibility,
    CandidateFusionEngine,
    EngramCandidateAuthority,
    FeatureNormalizer,
    FusedCandidate,
    FusionContribution,
    FusionDecision,
    FusionFeature,
    FusionFeatureRole,
    FusionPolicy,
    FusionPolicyReason,
    NormalizedFeatureSet,
    policy_fingerprint,
)
from engram.identity import ScopeKey, build_retrieval_representation, build_standalone_identity
from engram.repository import ArtifactRepository
from engram.resolution import (
    Candidate,
    CandidateSource,
    EvidenceKind,
    EvidenceReference,
    ExpectedObjectType,
    FeatureSet,
    QueryFrame,
    QueryFrameBuilder,
    ResolutionBudget,
    ResolutionOutcome,
)

NOW = datetime(2026, 8, 15, 12, 0, tzinfo=UTC)
START_NS = 1_000_000_000
SCOPE = ScopeKey(namespace="tenant-a", context_fingerprint="context-a")


def conformance_fusion(clock_ns=lambda: START_NS) -> CandidateFusionEngine:
    return CandidateFusionEngine(authority=PERMISSIVE_CANDIDATE_AUTHORITY, clock_ns=clock_ns)


def frame(engine: Engram | None = None) -> QueryFrame:
    selected = engine or Engram()
    budget = ResolutionBudget.capture(lambda: START_NS, total_time_ms=100, resolver_time_ms=25)
    return QueryFrameBuilder(selected, lambda: START_NS, lambda: NOW).build(
        "Which response is supported?",
        SCOPE,
        diagnostic_seed="fusion-test",
        budget=budget,
    )


def support_reference(claim_id: str = "claim-1") -> EvidenceReference:
    return EvidenceReference(claim_id, "support_semantic", EvidenceKind.SUPPORT, SCOPE)


def candidate(
    statement_id: str,
    source: CandidateSource,
    features: Mapping[str, float],
    *,
    response: str = "Supported response",
    evidence: tuple[EvidenceReference, ...] = (),
    scope: ScopeKey = SCOPE,
    lifecycle: LifecycleState = LifecycleState.ACTIVE,
    provenance: Mapping[str, object] = {},
    diagnostics: Mapping[str, object] = {},
) -> Candidate:
    return Candidate(
        candidate_id=f"candidate:{source.value}:{statement_id}",
        statement_id=statement_id,
        response=response,
        source=source,
        features=FeatureSet(values=features),
        evidence=evidence,
        scope=scope,
        lifecycle=lifecycle,
        provenance=provenance,
        diagnostics=diagnostics,
    )


def supported_pair(statement_id: str = "stmt-1", *, semantic: float = 0.92, lexical: float = 0.95):
    return (
        candidate(
            statement_id,
            CandidateSource.LEXICAL,
            {"lexical_score": lexical, "recency": 0.9},
            diagnostics={"lexical_trace": "sensitive-value"},
        ),
        candidate(
            statement_id,
            CandidateSource.SUPPORT_SEMANTIC,
            {"semantic_score": semantic, "support_coverage": 1.0},
            evidence=(support_reference(f"claim:{statement_id}"),),
            diagnostics={"semantic_trace": "sensitive-value"},
        ),
    )


def artifact(
    *,
    response: str = "Supported response",
    valid_until: str = "",
    valid_until_available: bool = False,
    metadata: Mapping[str, object] = {},
) -> CachedResponseArtifact:
    return CachedResponseArtifact(
        statement_id="stmt-artifact",
        generation=1,
        response=response,
        query_identity=build_standalone_identity("Which response is supported?", SCOPE),
        retrieval=build_retrieval_representation("Which response is supported?"),
        tier=Tier.STATIC,
        lifecycle=LifecycleState.ACTIVE,
        scope=SCOPE,
        support_claim_ids=("claim-artifact",),
        valid_from="",
        valid_from_available=False,
        valid_until=valid_until,
        valid_until_available=valid_until_available,
        knowledge_epoch=0,
        knowledge_epoch_available=False,
        superseded_by="",
        provenance=ArtifactProvenance("released", "regulator", "2026-08-14T12:00:00Z"),
        statistics=ArtifactStatistics(query_count=4, hit_count=3),
        metadata=metadata,
    )


def report_candidates(decision) -> list[Mapping[str, object]]:
    values = decision.report["candidates"]
    assert isinstance(values, list)
    return cast(list[Mapping[str, object]], values)


def report_reason_codes(decision, index: int = 0) -> tuple[str, ...]:
    eligibility = report_candidates(decision)[index]["eligibility"]
    assert isinstance(eligibility, Mapping)
    values = eligibility["reason_codes"]
    assert isinstance(values, list) and all(isinstance(value, str) for value in values)
    return tuple(cast(list[str], values))


def test_feature_specification_is_closed_bounded_and_concrete() -> None:
    assert set(FEATURE_DEFINITIONS) == set(FusionFeature)
    assert len(FusionFeature) == 13
    assert all(definition.minimum == 0.0 and definition.maximum == 1.0 for definition in FEATURE_DEFINITIONS.values())
    assert all(definition.meaning and definition.unavailable_meaning for definition in FEATURE_DEFINITIONS.values())
    empty = NormalizedFeatureSet.empty()
    assert set(empty.values) == set(FusionFeature)
    assert set(empty.values.values()) == {0.0}
    assert empty.available == ()
    assert "null" not in json.dumps(empty.to_dict())


def test_policy_codec_is_closed_versioned_and_fingerprinted() -> None:
    policy = FusionPolicy()

    assert FusionPolicy.from_json(policy.to_json()) == policy
    assert policy_fingerprint(policy) == "1f9d19acaedc277b4916bc366e74dc8c03d921e335acd21ac7fc2f1b449d963c"
    assert policy.answer_threshold == 0.78
    assert policy.evidence_threshold == 0.35
    assert policy.ambiguity_margin == 0.12
    assert policy.minimum_independent_sources == 2
    assert policy.require_support_for_non_exact is True
    assert policy.weights == {
        FusionFeature.EXACT: 1.0,
        FusionFeature.PATTERN: 0.75,
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
    invalid = policy.to_dict()
    invalid["unknown"] = True
    with pytest.raises(InvalidRequestError, match="invalid fields"):
        FusionPolicy.from_dict(invalid)
    with pytest.raises(InvalidRequestError, match="unsupported"):
        replace(policy, formula_version=2)
    with pytest.raises(InvalidRequestError, match="between 0 and 1"):
        replace(policy, answer_threshold=1.1)


def test_resolver_scores_use_source_specific_normalization() -> None:
    exact = FeatureNormalizer.normalize(candidate("exact", CandidateSource.EXACT, {"exact_match": 1.0}))
    pattern = FeatureNormalizer.normalize(candidate("pattern", CandidateSource.PATTERN, {"pattern_specificity": 4.0}))
    lexical = FeatureNormalizer.normalize(candidate("lexical", CandidateSource.LEXICAL, {"lexical_score": 0.7}))
    semantic = FeatureNormalizer.normalize(
        candidate("semantic", CandidateSource.SUPPORT_SEMANTIC, {"semantic_score": 0.8, "legacy_retrieval_score": 999.0})
    )

    assert exact.values[FusionFeature.EXACT] == 1.0
    assert pattern.values[FusionFeature.PATTERN] == 0.5
    assert lexical.values[FusionFeature.LEXICAL] == 0.7
    assert semantic.values[FusionFeature.SEMANTIC] == 0.8
    assert FusionFeature.LEXICAL not in semantic.available


def test_deduplication_retains_contributions_diagnostics_and_evidence() -> None:
    values = supported_pair()
    duplicate_evidence = replace(values[0], evidence=(support_reference("claim:stmt-1"),))

    decision = conformance_fusion().decide(frame(), (duplicate_evidence, values[1]))

    assert decision.outcome == ResolutionOutcome.ANSWER
    assert len(decision.response_candidates) == 1
    assert len(decision.response_candidates[0].evidence) == 1
    report = report_candidates(decision)[0]
    contributions = report["contributions"]
    assert isinstance(contributions, list)
    assert len(contributions) == 2
    fields = set()
    for item in contributions:
        contribution = cast(Mapping[str, object], item)
        diagnostic_fields = contribution["diagnostic_fields"]
        assert isinstance(diagnostic_fields, list) and all(isinstance(value, str) for value in diagnostic_fields)
        fields.add(tuple(cast(list[str], diagnostic_fields)))
    assert fields == {("lexical_trace",), ("semantic_trace",)}
    assert "sensitive-value" not in json.dumps(dict(decision.report))
    assert decision.working_memory_bytes > 0
    assert FusionDecision.from_json(decision.to_json()) == decision


def test_transparent_fusion_answers_only_supported_independent_agreement() -> None:
    decision = conformance_fusion().decide(frame(), supported_pair())

    assert decision.outcome == ResolutionOutcome.ANSWER
    assert decision.selected_candidate_available is True
    assert decision.selected_candidate.source == CandidateSource.SUPPORT_SEMANTIC
    assert decision.confidence_available is True
    assert decision.confidence >= FusionPolicy().answer_threshold
    assert decision.reason_codes == ("answer_fusion_threshold", "answer_no_runner_up")
    candidate_report = report_candidates(decision)[0]
    score_parts = candidate_report["score_contributions"]
    assert isinstance(score_parts, Mapping)
    assert score_parts["lexical"] > 0
    assert score_parts["semantic"] > 0
    assert score_parts["support"] > 0
    assert score_parts["agreement"] > 0


def test_missing_authority_abstains_by_default() -> None:
    decision = CandidateFusionEngine(clock_ns=lambda: START_NS).decide(frame(), supported_pair())

    assert decision.outcome == ResolutionOutcome.MISS
    assert FusionPolicyReason.AUTHORITATIVE_STATEMENT_MISSING.value in report_reason_codes(decision)


def test_single_source_or_incomplete_support_cannot_answer() -> None:
    lexical, semantic = supported_pair()
    single = conformance_fusion().decide(frame(), (semantic,))
    no_support = replace(semantic, features=FeatureSet(values={"semantic_score": 0.99}), evidence=())
    unsupported = conformance_fusion().decide(frame(), (lexical, no_support))

    assert single.outcome == ResolutionOutcome.EVIDENCE
    assert unsupported.outcome == ResolutionOutcome.EVIDENCE
    single_reasons = report_reason_codes(single)
    unsupported_reasons = report_reason_codes(unsupported)
    assert FusionPolicyReason.INDEPENDENT_SOURCES_MISSING.value in single_reasons
    assert FusionPolicyReason.SUPPORT_INCOMPLETE.value in unsupported_reasons


def test_close_distinct_candidates_abstain_even_above_answer_threshold() -> None:
    candidates = (*supported_pair("stmt-a"), *supported_pair("stmt-b", semantic=0.91, lexical=0.94))

    decision = conformance_fusion().decide(frame(), candidates)

    assert decision.outcome == ResolutionOutcome.EVIDENCE
    assert decision.selected_candidate_available is False
    assert len(decision.response_candidates) == 2
    assert decision.reason_codes[0] == FusionPolicyReason.AMBIGUOUS_TOP_CANDIDATES.value
    assert decision.report["top_two_margin_available"] is True
    assert cast(float, decision.report["top_two_margin"]) < FusionPolicy().ambiguity_margin


@pytest.mark.parametrize(
    ("changed", "expected_reason"),
    [
        ({"scope": ScopeKey(namespace="other")}, FusionPolicyReason.CANDIDATE_SCOPE_MISMATCH),
        ({"lifecycle": LifecycleState.RETIRED}, FusionPolicyReason.CANDIDATE_LIFECYCLE_INELIGIBLE),
    ],
)
def test_central_structural_eligibility_filters_before_scoring(changed, expected_reason) -> None:
    value = candidate("stmt", CandidateSource.EXACT, {"exact_match": 1.0})

    decision = conformance_fusion().decide(frame(), (replace(value, **changed),))

    assert decision.outcome == ResolutionOutcome.MISS
    eligibility = cast(Mapping[str, object], report_candidates(decision)[0]["eligibility"])
    reason_codes = eligibility["reason_codes"]
    assert isinstance(reason_codes, list) and all(isinstance(value, str) for value in reason_codes)
    assert expected_reason.value in cast(list[str], reason_codes)


def test_conflicting_responses_for_one_statement_are_not_selectable() -> None:
    first, second = supported_pair()

    decision = conformance_fusion().decide(frame(), (first, replace(second, response="Conflicting response")))

    assert decision.outcome == ResolutionOutcome.MISS
    assert FusionPolicyReason.CANDIDATE_STATEMENT_CONFLICT.value in report_reason_codes(decision)


def test_identity_and_object_type_mismatches_block_direct_selection() -> None:
    lexical, semantic = supported_pair()
    lexical = replace(lexical, features=FeatureSet(values={**dict(lexical.features.values), "entity_match": 0.0}))
    semantic = replace(semantic, features=FeatureSet(values={**dict(semantic.features.values), "object_type_match": 0.0}))
    selected_frame = replace(frame(), expected_object_type=ExpectedObjectType.PERSON)

    decision = conformance_fusion().decide(selected_frame, (lexical, semantic))

    assert decision.outcome == ResolutionOutcome.EVIDENCE
    reasons = report_reason_codes(decision)
    assert FusionPolicyReason.IDENTITY_FEATURE_MISMATCH.value in reasons
    assert FusionPolicyReason.OBJECT_TYPE_FEATURE_MISMATCH.value in reasons


def test_exact_candidate_remains_safe_single_source_answer() -> None:
    exact = candidate("stmt-exact", CandidateSource.EXACT, {"exact_match": 1.0})

    decision = conformance_fusion().decide(frame(), (exact,))

    assert decision.outcome == ResolutionOutcome.ANSWER
    assert decision.confidence == 1.0
    assert decision.reason_codes == ("answer_exact_eligible", "answer_no_runner_up")


@pytest.mark.parametrize(
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
    fusion = CandidateFusionEngine(authority=EngramCandidateAuthority(engine), clock_ns=lambda: START_NS)
    exact = candidate("stmt-artifact", CandidateSource.EXACT, {"exact_match": 1.0}, response=candidate_response)

    decision = fusion.decide(frame(engine), (exact,))

    assert decision.outcome == ResolutionOutcome.MISS
    eligibility = cast(Mapping[str, object], report_candidates(decision)[0]["eligibility"])
    reason_codes = eligibility["reason_codes"]
    assert isinstance(reason_codes, list) and all(isinstance(value, str) for value in reason_codes)
    assert expected_reason.value in cast(list[str], reason_codes)


def test_authoritative_features_use_explicit_support_history_and_authority() -> None:
    accepted = artifact(metadata={"authority": 0.85, "visibility": "scope", "support_complete": True})
    engine = Engram()
    engine.response_repository = ArtifactRepository((accepted,))
    fusion = CandidateFusionEngine(authority=EngramCandidateAuthority(engine), clock_ns=lambda: START_NS)
    exact = candidate("stmt-artifact", CandidateSource.EXACT, {"exact_match": 1.0})

    decision = fusion.decide(frame(engine), (exact,))

    assert decision.outcome == ResolutionOutcome.ANSWER
    normalized = cast(Mapping[str, object], report_candidates(decision)[0]["normalized_features"])
    values = cast(Mapping[str, float], normalized["values"])
    assert values["support"] == 1.0
    assert values["history"] == 0.75
    assert values["authority"] == 0.85


def test_feature_roles_and_internal_contract_codecs_are_complete() -> None:
    assert all(
        definition.producer
        and definition.owner_section
        and definition.trust_boundary
        and definition.raw_range
        and definition.combination_rule
        and isinstance(definition.role, FusionFeatureRole)
        for definition in FEATURE_DEFINITIONS.values()
    )
    raw = candidate("codec", CandidateSource.LEXICAL, {"lexical_score": 0.7})
    normalized = FeatureNormalizer.normalize(raw)
    eligibility = CandidateEligibility(
        feature_values={FusionFeature.HISTORY: 0.5},
        feature_available=(FusionFeature.HISTORY,),
    )
    contribution = FusionContribution(raw, normalized, eligibility)
    score_contributions = dict.fromkeys(FusionFeature, 0.0)
    fused = FusedCandidate(raw, (contribution,), normalized, 0.7, score_contributions, eligibility)
    decision = conformance_fusion().decide(frame(), supported_pair())

    assert NormalizedFeatureSet.from_json(normalized.to_json()) == normalized
    assert CandidateEligibility.from_json(eligibility.to_json()) == eligibility
    assert FusionContribution.from_json(contribution.to_json()) == contribution
    assert FusedCandidate.from_json(fused.to_json()) == fused
    assert FusionDecision.from_json(decision.to_json()) == decision
    assert "null" not in decision.to_json()


def test_normalization_rejects_cross_source_spoofing_and_keeps_priority_distinct() -> None:
    lexical = FeatureNormalizer.normalize(
        candidate(
            "lexical-spoof",
            CandidateSource.LEXICAL,
            {
                "lexical_score": 0.4,
                "exact_match": 1.0,
                "semantic_score": 1.0,
                "priority": 999.0,
                "hit_rate": 1.0,
            },
        )
    )
    semantic = FeatureNormalizer.normalize(
        candidate(
            "semantic-spoof",
            CandidateSource.SUPPORT_SEMANTIC,
            {"semantic_score": 0.8, "lexical_score": 1.0, "vector_weight": 99.0, "legacy_retrieval_score": 99.0},
        )
    )

    assert lexical.available == (FusionFeature.LEXICAL,)
    assert lexical.values[FusionFeature.LEXICAL] == 0.4
    assert semantic.available == (FusionFeature.SEMANTIC,)
    assert semantic.values[FusionFeature.SEMANTIC] == 0.8
    assert FusionFeature.HISTORY not in lexical.available


def test_explicit_mismatch_uses_conservative_aggregation() -> None:
    lexical, semantic = supported_pair()
    lexical = replace(lexical, features=FeatureSet(values={**dict(lexical.features.values), "entity_match": 1.0}))
    semantic = replace(semantic, features=FeatureSet(values={**dict(semantic.features.values), "entity_match": 0.0}))

    decision = conformance_fusion().decide(frame(), (lexical, semantic))

    assert decision.outcome == ResolutionOutcome.EVIDENCE
    normalized = report_candidates(decision)[0]["normalized_features"]
    assert isinstance(normalized, Mapping)
    values = normalized["values"]
    assert isinstance(values, Mapping)
    assert values["entity"] == 0.0
    assert FusionPolicyReason.IDENTITY_FEATURE_MISMATCH.value in report_reason_codes(decision)


def test_order_invariance_canonicalizes_candidates_and_evidence() -> None:
    lexical, semantic = supported_pair()
    extra = support_reference("claim-extra")
    semantic = replace(semantic, evidence=(*semantic.evidence, extra))
    engine = conformance_fusion()

    first = engine.decide(frame(), (lexical, semantic))
    second = engine.decide(frame(), (semantic, lexical))

    assert first.to_json() == second.to_json()


def test_per_candidate_filtering_preserves_valid_independent_group() -> None:
    lexical, semantic = supported_pair()
    excluded = candidate(
        "stmt-1",
        CandidateSource.UTILITY,
        {"object_type_match": 1.0},
        scope=ScopeKey(namespace="other"),
    )

    decision = conformance_fusion().decide(frame(), (excluded, semantic, lexical))

    assert decision.outcome == ResolutionOutcome.ANSWER
    report = report_candidates(decision)[0]
    assert report["sources"] == ["support_semantic", "lexical"]
    assert FusionPolicyReason.CANDIDATE_SCOPE_MISMATCH.value in report_reason_codes(decision)


def test_candidate_and_evidence_identity_conflicts_abstain_deterministically() -> None:
    lexical, semantic = supported_pair()
    conflicting_id = replace(semantic, candidate_id=lexical.candidate_id)
    candidate_conflict = conformance_fusion().decide(frame(), (lexical, conflicting_id))
    reference = support_reference("claim-conflict")
    reference_variant = replace(reference, resolver="different-resolver")
    semantic = replace(semantic, evidence=(reference, reference_variant))
    evidence_conflict = conformance_fusion().decide(frame(), (lexical, semantic))

    assert candidate_conflict.outcome == ResolutionOutcome.MISS
    assert FusionPolicyReason.CANDIDATE_ID_CONFLICT.value in report_reason_codes(candidate_conflict)
    assert evidence_conflict.outcome == ResolutionOutcome.EVIDENCE
    assert FusionPolicyReason.EVIDENCE_REFERENCE_CONFLICT.value in report_reason_codes(evidence_conflict)


def test_authority_revalidates_generation_support_and_legacy_response() -> None:
    accepted = artifact()
    engine = Engram()
    engine.response_repository = ArtifactRepository((accepted,))
    fusion = CandidateFusionEngine(authority=EngramCandidateAuthority(engine), clock_ns=lambda: START_NS)
    stale_generation = candidate(
        "stmt-artifact",
        CandidateSource.EXACT,
        {"exact_match": 1.0},
        provenance={"generation": 0},
    )
    lexical = candidate("stmt-artifact", CandidateSource.LEXICAL, {"lexical_score": 0.95})
    stale_support = candidate(
        "stmt-artifact",
        CandidateSource.SUPPORT_SEMANTIC,
        {"semantic_score": 0.95, "support_coverage": 1.0},
        evidence=(support_reference("claim-stale"),),
    )

    generation_decision = fusion.decide(frame(engine), (stale_generation,))
    support_decision = fusion.decide(frame(engine), (lexical, stale_support))

    assert generation_decision.outcome == ResolutionOutcome.MISS
    assert FusionPolicyReason.AUTHORITATIVE_GENERATION_MISMATCH.value in report_reason_codes(generation_decision)
    assert support_decision.outcome == ResolutionOutcome.EVIDENCE
    assert FusionPolicyReason.SUPPORT_REFERENCE_STALE.value in report_reason_codes(support_decision)

    legacy_engine = Engram()
    legacy_id = legacy_engine.store("Current legacy response")
    legacy_fusion = CandidateFusionEngine(authority=EngramCandidateAuthority(legacy_engine), clock_ns=lambda: START_NS)
    changed = candidate(
        legacy_id,
        CandidateSource.LEXICAL,
        {"lexical_score": 0.9},
        response="Changed legacy response",
    )
    legacy_decision = legacy_fusion.decide(frame(legacy_engine), (changed,))
    assert legacy_decision.outcome == ResolutionOutcome.MISS
    assert FusionPolicyReason.AUTHORITATIVE_RESPONSE_MISMATCH.value in report_reason_codes(legacy_decision)


def test_explicit_conflict_and_fusion_resource_exhaustion_are_typed() -> None:
    lexical, semantic = supported_pair()
    lexical = replace(
        lexical,
        features=FeatureSet(values={**dict(lexical.features.values), "explicit_conflict": 1.0}),
    )
    conflict = conformance_fusion().decide(frame(), (lexical, semantic))
    deadline = conformance_fusion(clock_ns=lambda: START_NS + 100_000_000).decide(
        frame(), supported_pair(), (support_reference("graph-evidence"),)
    )
    selected_frame = frame()
    memory_frame = replace(
        selected_frame,
        budget=replace(selected_frame.budget, max_working_memory_bytes=1),
    )
    memory = conformance_fusion().decide(memory_frame, supported_pair())

    assert conflict.outcome == ResolutionOutcome.EVIDENCE
    assert FusionPolicyReason.EXPLICIT_CONFLICT.value in report_reason_codes(conflict)
    assert deadline.outcome == ResolutionOutcome.EVIDENCE
    assert deadline.reason_codes[0] == FusionPolicyReason.FUSION_DEADLINE_EXHAUSTED.value
    assert memory.outcome == ResolutionOutcome.MISS
    assert memory.reason_codes == (FusionPolicyReason.FUSION_MEMORY_EXHAUSTED.value,)
    assert memory.working_memory_bytes == memory_frame.budget.max_working_memory_bytes


def test_explicit_fusion_memory_allowance_is_concrete_and_enforced() -> None:
    selected_frame = frame()
    lexical, semantic = supported_pair()
    engine = conformance_fusion()
    required = engine.decide(selected_frame, (lexical, semantic)).working_memory_bytes

    exhausted = engine.decide(
        selected_frame,
        (lexical, semantic),
        working_memory_limit=required - 1,
        working_memory_limit_available=True,
    )

    assert exhausted.outcome == ResolutionOutcome.MISS
    assert exhausted.reason_codes == (FusionPolicyReason.FUSION_MEMORY_EXHAUSTED.value,)
    assert exhausted.working_memory_bytes == required - 1
    with pytest.raises(InvalidRequestError, match="requires availability"):
        engine.decide(selected_frame, (), working_memory_limit=1)


def test_policy_reasons_are_closed_content_free_identifiers() -> None:
    assert len(set(FusionPolicyReason)) == len(FusionPolicyReason)
    assert all(reason.value == reason.value.lower() and " " not in reason.value for reason in FusionPolicyReason)
