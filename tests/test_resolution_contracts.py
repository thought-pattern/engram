"""Contract tests for the Section 4 resolution substrate."""

from datetime import UTC, datetime
from json import loads as json_loads

from pytest import mark as pytest_mark, raises as pytest_raises

from engram.constants import (
    BUDGET_CONSUMPTION_FIELDS,
    EVIDENCE_REFERENCE_FIELDS,
    QUERY_FRAME_FIELDS,
    CandidateSource,
    DisclosureBasis,
    EvidenceKind,
    ExpectedObjectType,
    LifecycleState,
    PropositionOwnership,
    ResolutionOutcome,
    ResolverState,
)
from engram.core import Engram
from engram.errors import InvalidRequestError
from engram.identity import extract_standalone_identity, scope_key
from engram.resolution import (
    BudgetLedger,
    QueryFrameBuilder,
    accounting_observation,
    accounting_observation_from_dict,
    accounting_observation_to_dict,
    budget_consumption,
    budget_consumption_from_dict,
    budget_consumption_to_dict,
    budget_consumption_with_changes,
    build_evidence_package,
    candidate as resolution_candidate,
    candidate_from_dict,
    candidate_to_dict,
    canonical_proposition_references,
    capture_resolution_budget,
    disclosure_decision,
    empty_candidate,
    evidence_package_to_dict,
    evidence_reference,
    evidence_reference_from_dict,
    evidence_reference_to_dict,
    feature_set,
    feature_set_from_dict,
    feature_set_to_dict,
    freeze_mapping,
    inheritance_provenance,
    proposition_evidence_record,
    proposition_evidence_record_to_dict,
    proposition_trust_inputs,
    proposition_validity_inputs,
    query_frame_from_dict,
    query_frame_to_dict,
    query_frame_with_changes,
    resolution_budget_from_dict,
    resolution_budget_to_dict,
    resolution_result,
    resolution_result_from_dict,
    resolution_result_to_dict,
    resolution_result_with_changes,
    resolver_result,
    resolver_result_from_dict,
    resolver_result_to_dict,
    rewrite_trace_step,
    validate_accounting_observation,
    validate_budget_consumption,
    validate_candidate,
    validate_evidence_reference,
    validate_feature_set,
    validate_inheritance_provenance,
    validate_resolution_budget,
    validate_resolver_result,
    validate_rewrite_trace_step,
)

SUPPORT_SCOPE = scope_key(namespace="support", context_fingerprint="account:one")
# A structured-graph Proposition evidence reference; evidence_reference copies every nested mapping, so each
# evidence_reference(**PROPOSITION_EVIDENCE_FIELDS) call returns an isolated reference and this constant stays read-only.
PROPOSITION_EVIDENCE_FIELDS = {
    "evidence_id": "proposition-1",
    "resolver": "structured_graph",
    "kind": EvidenceKind.PROPOSITION,
    "scope": SUPPORT_SCOPE,
    "provenance": {"query": "entity"},
    "diagnostics": {"row": 1},
}
# The accepted EXACT candidate for stmt-1 and the completed exact-resolver results with and without it. The
# contract validators copy their input, so these validated records are shared read-only inputs.
EXACT_CANDIDATE = resolution_candidate(
    candidate_id="candidate:exact:stmt-1",
    statement_id="stmt-1",
    response="PostgreSQL is an open-source relational database.",
    source=CandidateSource.EXACT,
    features=feature_set(values={"exact_match": 1.0}, unavailable=("semantic_score",)),
    evidence=(evidence_reference(**PROPOSITION_EVIDENCE_FIELDS),),
    scope=SUPPORT_SCOPE,
    lifecycle=LifecycleState.ACTIVE,
    provenance={"origin": "canonical"},
    diagnostics={"key": "canonical"},
)
EXACT_RESOLVER_RESULT = resolver_result(
    resolver="exact",
    state=ResolverState.COMPLETED,
    candidates=(EXACT_CANDIDATE,),
    accounting=(accounting_observation("stmt-1", ("postgresql",)),),
    diagnostics={"owners": 1},
    consumption=budget_consumption(elapsed_ns=5, resolvers=1, candidates=1),
)
EMPTY_RESOLVER_RESULT = resolver_result(
    resolver="exact",
    state=ResolverState.COMPLETED,
    candidates=(),
    accounting=(),
    diagnostics={"owners": 0},
    consumption=budget_consumption(elapsed_ns=5, resolvers=1, candidates=0),
)
# A complete, current, public structured-graph Proposition evidence record.
PROPOSITION_RECORD = proposition_evidence_record(
    proposition_id="proposition-full",
    source_resolver="structured_graph",
    source_contributions=("structured_graph",),
    features=feature_set(
        values={"canonical_completeness": 1.0, "structured_match": 1.0},
        unavailable=("semantic_similarity", "source_agreement", "supplied_trust"),
    ),
    canonical_references=canonical_proposition_references("entity:subject", "predicate:relation", "entity:object"),
    validity=proposition_validity_inputs(
        evaluation_time="2026-08-12T00:00:00Z",
        active=True,
        system_current=True,
        valid_time_current=True,
    ),
    trust=proposition_trust_inputs(),
    disclosure=disclosure_decision(
        ownership=PropositionOwnership.PUBLIC,
        basis=DisclosureBasis.PUBLIC_RULE,
        scope=SUPPORT_SCOPE,
    ),
    path=("proposition-full",),
    selection_reasons=("canonical_complete", "structured_match"),
)


def test_budget_codec_and_measurement_start_are_deterministic() -> None:
    budget = capture_resolution_budget(lambda: 1_000_000_000)

    assert type(budget) is dict
    assert budget.get("started_ns", 0) == 1_000_000_000
    assert resolution_budget_from_dict(resolution_budget_to_dict(budget)) == budget
    assert resolution_budget_from_dict(resolution_budget_to_dict(budget)) == budget
    copied = validate_resolution_budget(budget)
    assert copied == budget and copied is not budget


@pytest_mark.parametrize(
    ("changes", "message"),
    [
        ({"allowed_cost_classes": ()}, "must not be empty"),
        ({"started_ns": -1}, "started_ns"),
    ],
)
def test_budget_rejects_invalid_limits(changes, message) -> None:
    with pytest_raises(InvalidRequestError, match=message):
        validate_resolution_budget({**capture_resolution_budget(lambda: 1_000_000_000), **changes})


def test_budget_consumption_codec_and_ledger_exhaustion() -> None:
    budget = validate_resolution_budget(
        {**capture_resolution_budget(lambda: 1_000_000_000), "max_candidates": 2, "max_evidence": 1}
    )
    ledger = BudgetLedger(budget)
    first = ledger.add(budget_consumption(resolvers=1, candidates=1, evidence=1))
    second = ledger.add(budget_consumption(resolvers=1, candidates=2))

    assert type(second) is dict
    assert set(first) == BUDGET_CONSUMPTION_FIELDS
    assert first.get("exhausted_dimensions", ()) == ()
    assert second.get("exhausted_dimensions", ()) == ("candidates",)
    assert ledger.remaining_candidates() == 0
    assert ledger.remaining_evidence() == 0
    assert budget_consumption_from_dict(budget_consumption_to_dict(second)) == second
    copied = validate_budget_consumption(second)
    assert copied == second and copied is not second


def test_frame_builder_captures_one_clock_and_shared_preprocessing() -> None:
    engram = Engram()
    calls = []

    def utc_clock() -> datetime:
        calls.append("utc")
        result = datetime(2026, 8, 12, 12, 30, tzinfo=UTC)
        return result

    frame = QueryFrameBuilder(engram, lambda: 9, utc_clock).build("What's PostgreSQL?", SUPPORT_SCOPE, diagnostic_seed="same")

    assert set(frame) == QUERY_FRAME_FIELDS
    assert frame.get("original_text", "") == "What's PostgreSQL?"
    assert frame.get("resolved_text", "") == "What is PostgreSQL?"
    assert frame.get("identity", {}) == extract_standalone_identity("What's PostgreSQL?", SUPPORT_SCOPE)
    assert frame.get("expected_object_type", ExpectedObjectType.UNKNOWN) == ExpectedObjectType.UNKNOWN
    assert frame.get("inheritance", ()) == ()
    assert frame.get("rewrite_chain", ()) == ()
    assert frame.get("eligibility_context", {}).get("evaluation_time", "") == "2026-08-12T12:30:00Z"
    assert calls == ["utc"]
    assert frame.get("diagnostic_id", "").startswith("resolution:sha256:")


def test_frame_builder_recaptures_caller_limits_at_the_trusted_boundary() -> None:
    supplied = capture_resolution_budget(lambda: 100)
    calls = []

    def monotonic_clock() -> int:
        calls.append("monotonic")
        result = 500
        return result

    frame = QueryFrameBuilder(Engram(), monotonic_clock, lambda: datetime(2026, 8, 12, tzinfo=UTC)).build(
        "What is Engram?",
        budget=supplied,
    )

    assert frame.get("budget", {}).get("started_ns", 0) == 500
    assert calls == ["monotonic"]


def test_frame_builder_validates_authoritative_identity_scope() -> None:
    identity = extract_standalone_identity("Where is PostgreSQL?", scope_key(namespace="other"))

    with pytest_raises(InvalidRequestError, match="identity scope"):
        QueryFrameBuilder(Engram(), lambda: 1, lambda: datetime(2026, 8, 12, tzinfo=UTC)).build(
            "Where is PostgreSQL?", SUPPORT_SCOPE, identity=identity
        )


def test_frame_codec_preserves_traces_and_isolates_metadata() -> None:
    base = QueryFrameBuilder(Engram(), lambda: 1_000_000_000, lambda: datetime(2026, 8, 12, tzinfo=UTC)).build(
        "What's PostgreSQL?",
        SUPPORT_SCOPE,
        required_metadata={"channel": "support"},
        required_source_label="released",
        diagnostic_seed="request-1",
        budget=capture_resolution_budget(lambda: 1_000_000_000),
    )
    frame = query_frame_with_changes(
        base,
        {
            "inheritance": (inheritance_provenance("relation", 3),),
            "rewrite_chain": (rewrite_trace_step("rule-1", "input", "output"),),
        },
    )
    decoded = query_frame_from_dict(query_frame_to_dict(frame))
    inherited = decoded.get("inheritance", ())[0]
    rewrite = decoded.get("rewrite_chain", ())[0]

    assert decoded == frame
    assert type(inherited) is dict
    assert type(rewrite) is dict
    inherited_copy = validate_inheritance_provenance(inherited)
    rewrite_copy = validate_rewrite_trace_step(rewrite)
    assert inherited_copy == inherited and inherited_copy is not inherited
    assert rewrite_copy == rewrite and rewrite_copy is not rewrite
    assert "required_metadata" in decoded
    decoded_metadata = decoded.get("required_metadata", {})
    assert isinstance(decoded_metadata, dict)
    decoded_metadata["changed"] = True
    assert "changed" not in frame.get("required_metadata", {})


def test_frame_rejects_identity_and_eligibility_scope_mismatch() -> None:
    frame = QueryFrameBuilder(Engram(), lambda: 1_000_000_000, lambda: datetime(2026, 8, 12, tzinfo=UTC)).build(
        "What's PostgreSQL?",
        SUPPORT_SCOPE,
        required_metadata={"channel": "support"},
        required_source_label="released",
        diagnostic_seed="request-1",
        budget=capture_resolution_budget(lambda: 1_000_000_000),
    )

    with pytest_raises(InvalidRequestError, match="identity scope"):
        query_frame_with_changes(frame, {"scope": scope_key(namespace="other")})
    changed_identity = dict(frame.get("identity", {}))
    changed_identity["scope"] = scope_key(namespace="other")
    with pytest_raises(InvalidRequestError, match="eligibility context namespace"):
        query_frame_with_changes(
            frame,
            {"scope": scope_key(namespace="other"), "identity": changed_identity},
        )


def test_feature_set_codec_distinguishes_unavailable_from_zero() -> None:
    features = feature_set(values={"exact_match": 0.0}, unavailable=("semantic_score",))
    serialized = feature_set_to_dict(features)

    assert type(features) is dict
    assert feature_set_from_dict(feature_set_to_dict(features)) == features
    assert feature_set_from_dict(serialized) == features
    values = features.get("values", {})
    assert "exact_match" in values
    assert values.get("exact_match", 0.0) == 0.0
    assert "semantic_score" not in values
    copied = validate_feature_set(features)
    assert copied == features
    assert copied is not features
    copied_values = copied.get("values", {})
    assert copied_values is not values
    values["new"] = 1.0
    assert "new" not in copied_values
    malformed = dict(features)
    malformed["unexpected"] = True
    with pytest_raises(InvalidRequestError, match="invalid fields"):
        validate_feature_set(malformed)


def test_feature_set_rejects_overlap_nonfinite_and_unsorted_absence() -> None:
    with pytest_raises(InvalidRequestError, match="both available and unavailable"):
        feature_set(values={"score": 0.0}, unavailable=("score",))
    with pytest_raises(InvalidRequestError, match="finite"):
        feature_set(values={"score": float("nan")})
    with pytest_raises(InvalidRequestError, match="unique and sorted"):
        feature_set(unavailable=("z", "a"))


def test_candidate_and_evidence_codecs_preserve_exact_unicode_and_concrete_values() -> None:
    candidate = validate_candidate({**EXACT_CANDIDATE, "response": "Café ☕ — exact accepted text"})
    reference = evidence_reference(**PROPOSITION_EVIDENCE_FIELDS)
    serialized_reference = evidence_reference_to_dict(reference)

    assert type(candidate) is dict
    assert candidate_from_dict(candidate_to_dict(candidate)) == candidate
    copied_candidate = validate_candidate(candidate)
    assert copied_candidate == candidate and copied_candidate is not candidate
    assert evidence_reference_from_dict(evidence_reference_to_dict(reference)) == reference
    assert evidence_reference_from_dict(serialized_reference) == reference


def test_evidence_reference_is_an_exact_isolated_dictionary() -> None:
    reference = evidence_reference(**PROPOSITION_EVIDENCE_FIELDS)
    copied = validate_evidence_reference(reference)

    assert type(reference) is dict
    assert set(reference) == EVIDENCE_REFERENCE_FIELDS
    assert copied == reference
    assert copied is not reference
    assert copied.get("scope", {}) is not reference.get("scope", {})
    assert copied.get("provenance", {}) is not reference.get("provenance", {})
    assert copied.get("diagnostics", {}) is not reference.get("diagnostics", {})
    reference_provenance = reference.get("provenance", {})
    reference_provenance["changed"] = True
    assert "changed" not in copied.get("provenance", {})
    malformed = dict(reference)
    malformed["unexpected"] = True
    with pytest_raises(InvalidRequestError, match="invalid fields"):
        validate_evidence_reference(malformed)


def test_resolver_result_codec_and_state_invariants() -> None:
    result = EXACT_RESOLVER_RESULT
    observation = result.get("accounting", ())[0]

    assert resolver_result_from_dict(resolver_result_to_dict(result)) == result
    assert type(observation) is dict
    assert accounting_observation_from_dict(accounting_observation_to_dict(observation)) == observation
    copied = validate_accounting_observation(observation)
    assert copied == observation and copied is not observation
    with pytest_raises(InvalidRequestError, match="non-completed"):
        validate_resolver_result({**result, "state": ResolverState.FAILED})


def test_resolution_answer_codec_and_invariants() -> None:
    candidate = EXACT_CANDIDATE
    resolver = EXACT_RESOLVER_RESULT
    frame = QueryFrameBuilder(Engram(), lambda: 1_000_000_000, lambda: datetime(2026, 8, 12, tzinfo=UTC)).build(
        "What's PostgreSQL?",
        SUPPORT_SCOPE,
        required_metadata={"channel": "support"},
        required_source_label="released",
        diagnostic_seed="request-1",
        budget=capture_resolution_budget(lambda: 1_000_000_000),
    )
    diagnostic_id = frame.get("diagnostic_id", "")
    assert diagnostic_id
    result = resolution_result(
        outcome=ResolutionOutcome.ANSWER,
        selected_candidate=candidate,
        selected_candidate_available=True,
        response_candidates=(candidate,),
        evidence=(),
        confidence=1.0,
        confidence_available=True,
        reason_codes=("exact_unique_eligible",),
        frame_diagnostics={"diagnostic_id": diagnostic_id},
        resolver_results=(resolver,),
        budget=resolver.get("consumption", {}),
    )

    assert resolution_result_from_dict(resolution_result_to_dict(result)) == result
    serialized = resolution_result_to_dict(result)
    assert "selected_candidate" in serialized
    selected = serialized.get("selected_candidate", {})
    assert isinstance(selected, dict)
    assert selected.get("response", "") == candidate.get("response", "")
    lexical = validate_candidate({**candidate, "source": CandidateSource.SPARSE})
    fused = resolution_result_with_changes(
        result,
        {"selected_candidate": lexical, "response_candidates": (lexical,), "confidence": 0.81},
    )
    assert resolution_result_from_dict(resolution_result_to_dict(fused)) == fused
    with pytest_raises(InvalidRequestError, match="only the selected"):
        resolution_result_with_changes(result, {"selected_candidate": lexical})
    with pytest_raises(InvalidRequestError, match="positive confidence"):
        resolution_result_with_changes(result, {"confidence": 0.25, "confidence_available": False})
    with pytest_raises(InvalidRequestError, match="top-level evidence"):
        resolution_result_with_changes(result, {"evidence": (evidence_reference(**PROPOSITION_EVIDENCE_FIELDS),)})


def test_resolution_evidence_and_miss_invariants_use_concrete_empty_candidate() -> None:
    lexical = validate_candidate({**EXACT_CANDIDATE, "source": CandidateSource.SPARSE})
    evidence_result = resolution_result(
        outcome=ResolutionOutcome.EVIDENCE,
        selected_candidate=empty_candidate(),
        selected_candidate_available=False,
        response_candidates=(lexical,),
        evidence=(),
        confidence=0.0,
        confidence_available=False,
        reason_codes=("non_exact_candidates",),
        frame_diagnostics={},
        resolver_results=(),
        budget=budget_consumption(),
    )
    miss = resolution_result_with_changes(
        evidence_result,
        {"outcome": ResolutionOutcome.MISS, "response_candidates": (), "reason_codes": ("no_usable_output",)},
    )

    assert resolution_result_from_dict(resolution_result_to_dict(evidence_result)) == evidence_result
    assert resolution_result_from_dict(resolution_result_to_dict(miss)) == miss
    serialized_miss = resolution_result_to_dict(miss)
    assert "selected_candidate" in serialized_miss
    assert serialized_miss.get("selected_candidate", {}) == {}
    assert resolution_result_from_dict(resolution_result_to_dict(miss)) == miss
    with pytest_raises(InvalidRequestError, match="EVIDENCE requires"):
        resolution_result_with_changes(evidence_result, {"response_candidates": ()})
    with pytest_raises(InvalidRequestError, match="MISS cannot"):
        resolution_result_with_changes(miss, {"evidence": (evidence_reference(**PROPOSITION_EVIDENCE_FIELDS),)})
    with pytest_raises(InvalidRequestError, match="concrete empty candidate"):
        resolution_result_with_changes(miss, {"selected_candidate": lexical})
    with pytest_raises(InvalidRequestError, match="unavailable and zero"):
        resolution_result_with_changes(evidence_result, {"confidence": 0.5, "confidence_available": True})


def test_contract_loaders_reject_null_and_unknown_fields() -> None:
    budget = resolution_budget_to_dict(capture_resolution_budget(lambda: 1_000_000_000))
    budget["unexpected"] = True
    with pytest_raises(InvalidRequestError, match="invalid fields"):
        resolution_budget_from_dict(budget)
    with pytest_raises(InvalidRequestError):
        query_frame_from_dict(json_loads("null"))


def test_contracts_enforce_nested_byte_and_collection_bounds() -> None:
    with pytest_raises(InvalidRequestError, match="16384 UTF-8 bytes"):
        validate_candidate({**EXACT_CANDIDATE, "diagnostics": {"detail": "x" * 16_385}})
    with pytest_raises(InvalidRequestError, match="item limit"):
        validate_resolver_result({**EXACT_RESOLVER_RESULT, "candidates": (EXACT_CANDIDATE,) * 1_001})
    with pytest_raises(InvalidRequestError, match="limit of 64"):
        budget_consumption_with_changes(
            budget_consumption(), {"exhausted_dimensions": tuple(f"d{index:02d}" for index in range(65))}
        )


def test_resolver_result_current_field_set_is_exact() -> None:
    result = validate_resolver_result(
        {**EMPTY_RESOLVER_RESULT, "resolver": "structured_graph", "proposition_evidence": (PROPOSITION_RECORD,)}
    )

    assert set(resolver_result_to_dict(result)) == set(
        {
            "resolver",
            "state",
            "reason_code",
            "candidates",
            "evidence",
            "proposition_evidence",
            "accounting",
            "diagnostics",
            "consumption",
        }
    )
    serialized_evidence = resolver_result_to_dict(result).get("proposition_evidence", [])
    assert serialized_evidence == [proposition_evidence_record_to_dict(PROPOSITION_RECORD)]
    assert resolver_result_from_dict(resolver_result_to_dict(result)) == result
    missing = resolver_result_to_dict(result)
    missing.pop("proposition_evidence")
    with pytest_raises(InvalidRequestError, match="invalid fields"):
        resolver_result_from_dict(missing)
    added = resolver_result_to_dict(result)
    added["unexpected_projection"] = []
    with pytest_raises(InvalidRequestError, match="invalid fields"):
        resolver_result_from_dict(added)


def test_resolver_result_rejects_mismatched_proposition_evidence_source() -> None:
    with pytest_raises(InvalidRequestError, match="source must match"):
        validate_resolver_result({**EMPTY_RESOLVER_RESULT, "proposition_evidence": (PROPOSITION_RECORD,)})


def test_resolution_result_current_package_fields_are_exact() -> None:
    miss = resolution_result(
        outcome=ResolutionOutcome.MISS,
        selected_candidate=empty_candidate(),
        selected_candidate_available=False,
        response_candidates=(),
        evidence=(),
        confidence=0.0,
        confidence_available=False,
        reason_codes=("miss",),
        frame_diagnostics={},
        resolver_results=(),
        budget=budget_consumption(),
    )
    package = build_evidence_package((PROPOSITION_RECORD,))
    evidence_result = resolution_result_with_changes(
        miss,
        {
            "outcome": ResolutionOutcome.EVIDENCE,
            "reason_codes": ("proposition_evidence_included",),
            "evidence_package_available": True,
            "evidence_package": package,
        },
    )

    serialized_evidence_result = resolution_result_to_dict(evidence_result)
    assert serialized_evidence_result.get("evidence_package_available", False) is True
    assert serialized_evidence_result.get("evidence_package", {}) == evidence_package_to_dict(package)
    assert resolution_result_from_dict(resolution_result_to_dict(miss)) == miss
    assert resolution_result_from_dict(resolution_result_to_dict(evidence_result)) == evidence_result

    missing = resolution_result_to_dict(evidence_result)
    missing.pop("evidence_package")
    with pytest_raises(InvalidRequestError, match="invalid fields"):
        resolution_result_from_dict(missing)
    added = resolution_result_to_dict(evidence_result)
    added["unsupported_version"] = 1
    with pytest_raises(InvalidRequestError, match="invalid fields"):
        resolution_result_from_dict(added)
    with pytest_raises(InvalidRequestError, match="unpackaged Proposition evidence"):
        resolution_result_with_changes(
            miss,
            {
                "resolver_results": (
                    validate_resolver_result(
                        {
                            **EMPTY_RESOLVER_RESULT,
                            "resolver": "structured_graph",
                            "proposition_evidence": (PROPOSITION_RECORD,),
                        }
                    ),
                )
            },
        )


def test_resolution_result_answer_and_miss_package_invariants() -> None:
    candidate = EXACT_CANDIDATE
    answer = resolution_result(
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
        budget=budget_consumption(),
    )
    available_empty_miss = resolution_result(
        outcome=ResolutionOutcome.MISS,
        selected_candidate=empty_candidate(),
        selected_candidate_available=False,
        response_candidates=(),
        evidence=(),
        confidence=0.0,
        confidence_available=False,
        reason_codes=("miss",),
        frame_diagnostics={},
        resolver_results=(),
        budget=budget_consumption(),
        evidence_package_available=True,
    )

    assert resolution_result_from_dict(resolution_result_to_dict(answer)) == answer
    assert resolution_result_from_dict(resolution_result_to_dict(available_empty_miss)) == available_empty_miss
    with pytest_raises(InvalidRequestError, match="ANSWER cannot contain"):
        resolution_result_with_changes(
            answer,
            {
                "evidence_package_available": True,
                "evidence_package": build_evidence_package((PROPOSITION_RECORD,)),
            },
        )
    with pytest_raises(InvalidRequestError, match="MISS cannot contain"):
        resolution_result_with_changes(
            available_empty_miss,
            {"evidence_package": build_evidence_package((PROPOSITION_RECORD,))},
        )


def test_mixed_key_types_are_a_validation_error_not_a_type_error() -> None:
    with pytest_raises(InvalidRequestError, match="keys must be strings"):
        freeze_mapping({1: "a", "b": 2}, "required_metadata")
