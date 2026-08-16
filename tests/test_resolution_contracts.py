"""Contract tests for the Section 4 resolution substrate."""

import json
from dataclasses import replace
from datetime import UTC, datetime
from types import MappingProxyType
from typing import Any, cast

import pytest

from engram.artifacts import LifecycleState
from engram.core import Engram
from engram.errors import InvalidRequestError
from engram.identity import ScopeKey, build_standalone_identity
from engram.resolution import (
    EMPTY_CANDIDATE,
    AccountingObservation,
    BudgetConsumption,
    BudgetLedger,
    Candidate,
    CandidateSource,
    CanonicalClaimReferences,
    ClaimEvidenceRecord,
    ClaimOwnership,
    ClaimTrustInputs,
    ClaimValidityInputs,
    CostClass,
    DisclosureBasis,
    DisclosureDecision,
    EvidenceKind,
    EvidencePackage,
    EvidenceReference,
    ExpectedObjectType,
    FeatureSet,
    InheritanceProvenance,
    QueryFrame,
    QueryFrameBuilder,
    ResolutionBudget,
    ResolutionOutcome,
    ResolutionResult,
    ResolverResult,
    ResolverState,
    RewriteTraceStep,
)


def _scope() -> ScopeKey:
    return ScopeKey(namespace="support", context_fingerprint="account:one")


def _budget() -> ResolutionBudget:
    return ResolutionBudget.capture(lambda: 1_000_000_000, total_time_ms=100, resolver_time_ms=25)


def _frame() -> QueryFrame:
    engram = Engram()
    return QueryFrameBuilder(engram, lambda: 1_000_000_000, lambda: datetime(2026, 8, 12, tzinfo=UTC)).build(
        "What's PostgreSQL?",
        _scope(),
        required_metadata={"channel": "support"},
        required_source_label="tapestry:released",
        diagnostic_seed="request-1",
        budget=_budget(),
    )


def _evidence() -> EvidenceReference:
    return EvidenceReference(
        evidence_id="claim-1",
        resolver="structured_graph",
        kind=EvidenceKind.CLAIM,
        scope=_scope(),
        provenance={"query": "entity"},
        diagnostics={"row": 1},
    )


def _claim_record() -> ClaimEvidenceRecord:
    return ClaimEvidenceRecord(
        claim_id="claim-full",
        source_resolver="structured_graph",
        source_contributions=("structured_graph",),
        features=FeatureSet(
            values={"canonical_completeness": 1.0, "structured_match": 1.0},
            unavailable=("semantic_similarity", "source_agreement", "supplied_trust"),
        ),
        canonical_references=CanonicalClaimReferences("entity:subject", "predicate:relation", "entity:object"),
        validity=ClaimValidityInputs(
            evaluation_time="2026-08-12T00:00:00Z",
            active=True,
            system_current=True,
            valid_time_current=True,
        ),
        trust=ClaimTrustInputs(),
        disclosure=DisclosureDecision(
            ownership=ClaimOwnership.PUBLIC,
            basis=DisclosureBasis.PUBLIC_RULE,
            scope=_scope(),
            policy_version="claim-disclosure-v1",
        ),
        path=("claim-full",),
        selection_reasons=("canonical_complete", "structured_match"),
    )


def _candidate(source: CandidateSource = CandidateSource.EXACT) -> Candidate:
    return Candidate(
        candidate_id=f"candidate:{source.value}:stmt-1",
        statement_id="stmt-1",
        response="PostgreSQL is an open-source relational database.",
        source=source,
        features=FeatureSet(values={"exact_match": 1.0}, unavailable=("semantic_score",)),
        evidence=(_evidence(),),
        scope=_scope(),
        lifecycle=LifecycleState.ACTIVE,
        provenance={"origin": "canonical"},
        diagnostics={"key": "canonical"},
    )


def _resolver_result(candidate: Candidate = EMPTY_CANDIDATE) -> ResolverResult:
    candidates = () if candidate is EMPTY_CANDIDATE else (candidate,)
    accounting = () if candidate is EMPTY_CANDIDATE else (AccountingObservation(candidate.statement_id, ("postgresql",)),)
    return ResolverResult(
        resolver="exact",
        state=ResolverState.COMPLETED,
        candidates=candidates,
        accounting=accounting,
        diagnostics={"owners": len(candidates)},
        consumption=BudgetConsumption(elapsed_ns=5, resolvers=1, candidates=len(candidates)),
    )


def test_budget_codec_and_captured_deadline_are_deterministic() -> None:
    budget = _budget()

    assert budget.deadline_ns == 1_100_000_000
    assert ResolutionBudget.from_dict(budget.to_dict()) == budget
    assert ResolutionBudget.from_json(budget.to_json()) == budget
    assert budget.to_json() == json.dumps(budget.to_dict(), sort_keys=True, separators=(",", ":"), ensure_ascii=False)


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"total_time_ms": 0}, "total_time_ms"),
        ({"resolver_time_ms": 101}, "resolver_time_ms"),
        ({"allowed_cost_classes": ()}, "must not be empty"),
        ({"started_ns": 1, "deadline_ns": 0}, "present or absent together"),
        ({"started_ns": 10, "deadline_ns": 10}, "must be after"),
    ],
)
def test_budget_rejects_invalid_limits(changes, message) -> None:
    with pytest.raises(InvalidRequestError, match=message):
        replace(_budget(), **changes)


def test_budget_consumption_codec_and_ledger_exhaustion() -> None:
    budget = replace(_budget(), max_candidates=2, max_evidence=1)
    ledger = BudgetLedger(budget, lambda: 1_050_000_000)
    first = ledger.add(BudgetConsumption(resolvers=1, candidates=1, evidence=1))
    second = ledger.add(BudgetConsumption(resolvers=1, candidates=2))

    assert first.exhausted_dimensions == ()
    assert second.exhausted_dimensions == ("candidates",)
    assert ledger.remaining_candidates() == 0
    assert ledger.remaining_evidence() == 0
    assert BudgetConsumption.from_json(second.to_json()) == second


def test_frame_builder_captures_one_clock_and_shared_preprocessing() -> None:
    engram = Engram()
    calls = []

    def utc_clock() -> datetime:
        calls.append("utc")
        return datetime(2026, 8, 12, 12, 30, tzinfo=UTC)

    frame = QueryFrameBuilder(engram, lambda: 9, utc_clock).build("What's PostgreSQL?", _scope(), diagnostic_seed="same")

    assert frame.original_text == "What's PostgreSQL?"
    assert frame.resolved_text == "What is PostgreSQL?"
    assert frame.identity == build_standalone_identity("What's PostgreSQL?", _scope())
    assert frame.expected_object_type == ExpectedObjectType.UNKNOWN
    assert frame.inheritance == ()
    assert frame.rewrite_chain == ()
    assert frame.eligibility_context.evaluation_time == "2026-08-12T12:30:00Z"
    assert calls == ["utc"]
    assert frame.diagnostic_id.startswith("resolution:sha256:")


def test_frame_builder_recaptures_caller_limits_at_the_trusted_boundary() -> None:
    supplied = ResolutionBudget.capture(lambda: 100, total_time_ms=50, resolver_time_ms=10)
    calls = []

    def monotonic_clock() -> int:
        calls.append("monotonic")
        return 500

    frame = QueryFrameBuilder(Engram(), monotonic_clock, lambda: datetime(2026, 8, 12, tzinfo=UTC)).build(
        "What is Engram?",
        budget=supplied,
    )

    assert frame.budget.started_ns == 500
    assert frame.budget.deadline_ns == 50_000_500
    assert frame.budget.total_time_ms == supplied.total_time_ms
    assert calls == ["monotonic"]


def test_frame_builder_validates_authoritative_identity_scope() -> None:
    identity = build_standalone_identity("Where is PostgreSQL?", ScopeKey(namespace="other"))

    with pytest.raises(InvalidRequestError, match="identity scope"):
        QueryFrameBuilder(Engram(), lambda: 1, lambda: datetime(2026, 8, 12, tzinfo=UTC)).build(
            "Where is PostgreSQL?", _scope(), identity=identity
        )


def test_frame_codec_preserves_traces_and_immutable_metadata() -> None:
    frame = replace(
        _frame(),
        inheritance=(InheritanceProvenance("relation", 3),),
        rewrite_chain=(RewriteTraceStep("rule-1", "input", "output"),),
    )
    decoded = QueryFrame.from_json(frame.to_json())

    assert decoded == frame
    assert isinstance(decoded.required_metadata, MappingProxyType)
    with pytest.raises(TypeError):
        cast(Any, decoded.required_metadata)["changed"] = True


def test_frame_rejects_identity_and_eligibility_scope_mismatch() -> None:
    frame = _frame()

    with pytest.raises(InvalidRequestError, match="identity scope"):
        replace(frame, scope=ScopeKey(namespace="other"))
    with pytest.raises(InvalidRequestError, match="eligibility context namespace"):
        replace(frame, scope=ScopeKey(namespace="other"), identity=replace(frame.identity, scope=ScopeKey(namespace="other")))


def test_feature_set_codec_distinguishes_unavailable_from_zero() -> None:
    features = FeatureSet(values={"exact_match": 0.0}, unavailable=("semantic_score",))

    assert FeatureSet.from_json(features.to_json()) == features
    assert features.values["exact_match"] == 0.0
    assert "semantic_score" not in features.values


def test_feature_set_rejects_overlap_nonfinite_and_unsorted_absence() -> None:
    with pytest.raises(InvalidRequestError, match="both available and unavailable"):
        FeatureSet(values={"score": 0.0}, unavailable=("score",))
    with pytest.raises(InvalidRequestError, match="finite"):
        FeatureSet(values={"score": float("nan")})
    with pytest.raises(InvalidRequestError, match="unique and sorted"):
        FeatureSet(unavailable=("z", "a"))


def test_candidate_and_evidence_codecs_preserve_exact_unicode_and_concrete_values() -> None:
    candidate = replace(_candidate(), response="Café ☕ — exact accepted text")

    assert Candidate.from_json(candidate.to_json()) == candidate
    assert EvidenceReference.from_json(_evidence().to_json()) == _evidence()
    assert "null" not in candidate.to_json()


def test_resolver_result_codec_and_state_invariants() -> None:
    result = _resolver_result(_candidate())

    assert ResolverResult.from_json(result.to_json()) == result
    with pytest.raises(InvalidRequestError, match="non-completed"):
        replace(result, state=ResolverState.FAILED)


def test_resolution_answer_codec_and_invariants() -> None:
    candidate = _candidate()
    resolver = _resolver_result(candidate)
    result = ResolutionResult(
        outcome=ResolutionOutcome.ANSWER,
        selected_candidate=candidate,
        selected_candidate_available=True,
        response_candidates=(candidate,),
        evidence=(),
        confidence=1.0,
        confidence_available=True,
        reason_codes=("exact_unique_eligible",),
        frame_diagnostics={"diagnostic_id": _frame().diagnostic_id},
        resolver_results=(resolver,),
        budget=resolver.consumption,
    )

    assert ResolutionResult.from_json(result.to_json()) == result
    selected = result.to_dict()["selected_candidate"]
    assert isinstance(selected, dict)
    assert selected["response"] == candidate.response
    lexical = replace(candidate, source=CandidateSource.LEXICAL)
    fused = replace(result, selected_candidate=lexical, response_candidates=(lexical,), confidence=0.81)
    assert ResolutionResult.from_json(fused.to_json()) == fused
    with pytest.raises(InvalidRequestError, match="only the selected"):
        replace(result, selected_candidate=lexical)
    with pytest.raises(InvalidRequestError, match="positive confidence"):
        replace(result, confidence=0.25, confidence_available=False)
    with pytest.raises(InvalidRequestError, match="top-level evidence"):
        replace(result, evidence=(_evidence(),))


def test_resolution_evidence_and_miss_invariants_use_concrete_empty_candidate() -> None:
    lexical = replace(_candidate(), source=CandidateSource.LEXICAL)
    evidence_result = ResolutionResult(
        outcome=ResolutionOutcome.EVIDENCE,
        selected_candidate=EMPTY_CANDIDATE,
        selected_candidate_available=False,
        response_candidates=(lexical,),
        evidence=(),
        confidence=0.0,
        confidence_available=False,
        reason_codes=("non_exact_candidates",),
        frame_diagnostics={},
        resolver_results=(),
        budget=BudgetConsumption(),
    )
    miss = replace(evidence_result, outcome=ResolutionOutcome.MISS, response_candidates=(), reason_codes=("no_usable_output",))

    assert ResolutionResult.from_json(evidence_result.to_json()) == evidence_result
    assert ResolutionResult.from_json(miss.to_json()) == miss
    assert miss.to_dict()["selected_candidate"] == {}
    assert ResolutionResult.from_json(miss.to_json()) == miss
    with pytest.raises(InvalidRequestError, match="EVIDENCE requires"):
        replace(evidence_result, response_candidates=())
    with pytest.raises(InvalidRequestError, match="MISS cannot"):
        replace(miss, evidence=(_evidence(),))
    with pytest.raises(InvalidRequestError, match="EMPTY_CANDIDATE"):
        replace(miss, selected_candidate=lexical)
    with pytest.raises(InvalidRequestError, match="unavailable and zero"):
        replace(evidence_result, confidence=0.5, confidence_available=True)


def test_contract_loaders_reject_null_unknown_fields_and_unsupported_versions() -> None:
    budget = _budget().to_dict()
    budget["unexpected"] = True
    with pytest.raises(InvalidRequestError, match="invalid fields"):
        ResolutionBudget.from_dict(budget)
    with pytest.raises(InvalidRequestError, match="must contain an object"):
        QueryFrame.from_json("null")
    with pytest.raises(InvalidRequestError, match="unsupported candidate schema_version"):
        replace(_candidate(), schema_version=2)


def test_contracts_enforce_nested_byte_and_collection_bounds() -> None:
    with pytest.raises(InvalidRequestError, match="16384 UTF-8 bytes"):
        replace(_candidate(), diagnostics={"detail": "x" * 16_385})
    with pytest.raises(InvalidRequestError, match="item limit"):
        replace(_resolver_result(_candidate()), candidates=(_candidate(),) * 1_001)
    with pytest.raises(InvalidRequestError, match="limit of 64"):
        replace(BudgetConsumption(), exhausted_dimensions=tuple(f"d{index:02d}" for index in range(65)))


@pytest.mark.parametrize(
    "factory",
    [
        lambda: replace(_budget(), schema_version=2),
        lambda: replace(BudgetConsumption(), schema_version=2),
        lambda: replace(_frame(), schema_version=2),
        lambda: replace(FeatureSet(), schema_version=2),
        lambda: replace(_evidence(), schema_version=2),
        lambda: replace(_candidate(), schema_version=2),
        lambda: replace(AccountingObservation("stmt-1"), schema_version=2),
        lambda: replace(_resolver_result(), schema_version=3),
        lambda: replace(
            ResolutionResult(
                outcome=ResolutionOutcome.MISS,
                selected_candidate=EMPTY_CANDIDATE,
                selected_candidate_available=False,
                response_candidates=(),
                evidence=(),
                confidence=0.0,
                confidence_available=False,
                reason_codes=("miss",),
                frame_diagnostics={},
                resolver_results=(),
                budget=BudgetConsumption(),
            ),
            schema_version=3,
        ),
    ],
)
def test_every_versioned_resolution_contract_rejects_unknown_versions(factory) -> None:
    with pytest.raises(InvalidRequestError, match="unsupported"):
        factory()


def test_resolver_result_current_field_set_is_exact() -> None:
    result = replace(_resolver_result(), resolver="structured_graph", claim_evidence=(_claim_record(),))

    assert frozenset(result.to_dict()) == frozenset(
        {
            "schema_version",
            "resolver",
            "state",
            "reason_code",
            "candidates",
            "evidence",
            "claim_evidence",
            "accounting",
            "diagnostics",
            "consumption",
        }
    )
    assert result.to_dict()["claim_evidence"] == [_claim_record().to_dict()]
    assert ResolverResult.from_json(result.to_json()) == result
    missing = result.to_dict()
    missing.pop("claim_evidence")
    with pytest.raises(InvalidRequestError, match="invalid fields"):
        ResolverResult.from_dict(missing)
    added = result.to_dict()
    added["legacy_projection"] = []
    with pytest.raises(InvalidRequestError, match="invalid fields"):
        ResolverResult.from_dict(added)


def test_resolver_result_rejects_mismatched_claim_evidence_source() -> None:
    with pytest.raises(InvalidRequestError, match="source must match"):
        replace(_resolver_result(), claim_evidence=(_claim_record(),))


def test_resolution_result_current_package_fields_are_exact() -> None:
    miss = ResolutionResult(
        outcome=ResolutionOutcome.MISS,
        selected_candidate=EMPTY_CANDIDATE,
        selected_candidate_available=False,
        response_candidates=(),
        evidence=(),
        confidence=0.0,
        confidence_available=False,
        reason_codes=("miss",),
        frame_diagnostics={},
        resolver_results=(),
        budget=BudgetConsumption(),
    )
    package = EvidencePackage.build((_claim_record(),))
    evidence_result = replace(
        miss,
        outcome=ResolutionOutcome.EVIDENCE,
        reason_codes=("claim_evidence_included",),
        evidence_package_available=True,
        evidence_package=package,
    )

    assert evidence_result.schema_version == 1
    assert evidence_result.to_dict()["evidence_package_available"] is True
    assert evidence_result.to_dict()["evidence_package"] == package.to_dict()
    assert ResolutionResult.from_json(miss.to_json()) == miss
    assert ResolutionResult.from_json(evidence_result.to_json()) == evidence_result

    missing = evidence_result.to_dict()
    missing.pop("evidence_package")
    with pytest.raises(InvalidRequestError, match="invalid fields"):
        ResolutionResult.from_dict(missing)
    added = evidence_result.to_dict()
    added["compatibility_version"] = 1
    with pytest.raises(InvalidRequestError, match="invalid fields"):
        ResolutionResult.from_dict(added)
    with pytest.raises(InvalidRequestError, match="unpackaged Claim evidence"):
        replace(
            miss,
            resolver_results=(replace(_resolver_result(), resolver="structured_graph", claim_evidence=(_claim_record(),)),),
        )


def test_resolution_result_answer_and_miss_package_invariants() -> None:
    candidate = _candidate()
    answer = ResolutionResult(
        outcome=ResolutionOutcome.ANSWER,
        selected_candidate=candidate,
        selected_candidate_available=True,
        response_candidates=(candidate,),
        evidence=(),
        confidence=1.0,
        confidence_available=True,
        reason_codes=("answer",),
        frame_diagnostics={},
        resolver_results=(),
        budget=BudgetConsumption(),
    )
    available_empty_miss = ResolutionResult(
        outcome=ResolutionOutcome.MISS,
        selected_candidate=EMPTY_CANDIDATE,
        selected_candidate_available=False,
        response_candidates=(),
        evidence=(),
        confidence=0.0,
        confidence_available=False,
        reason_codes=("miss",),
        frame_diagnostics={},
        resolver_results=(),
        budget=BudgetConsumption(),
        evidence_package_available=True,
    )

    assert ResolutionResult.from_json(answer.to_json()) == answer
    assert ResolutionResult.from_json(available_empty_miss.to_json()) == available_empty_miss
    with pytest.raises(InvalidRequestError, match="ANSWER cannot contain"):
        replace(
            answer,
            evidence_package_available=True,
            evidence_package=EvidencePackage.build((_claim_record(),)),
        )
    with pytest.raises(InvalidRequestError, match="MISS cannot contain"):
        replace(
            available_empty_miss,
            evidence_package=EvidencePackage.build((_claim_record(),)),
        )


def test_closed_vocabularies_are_complete() -> None:
    assert {value.value for value in CostClass} == {"exact", "cheap", "standard", "expensive"}
    assert {value.value for value in ResolverState} == {"completed", "unavailable", "skipped", "exhausted", "failed"}
    assert {value.value for value in ResolutionOutcome} == {"ANSWER", "EVIDENCE", "MISS"}
