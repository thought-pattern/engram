"""Contract tests for the Section 4 resolution substrate."""

from datetime import UTC, datetime
from json import dumps as json_dumps

from pytest import mark as pytest_mark, raises as pytest_raises

from engram.artifacts import LifecycleState
from engram.core import Engram
from engram.errors import InvalidRequestError
from engram.identity import build_standalone_identity, scope_key
from engram.resolution import (
    BudgetLedger,
    CandidateSource,
    DisclosureBasis,
    EvidenceKind,
    ExpectedObjectType,
    PropositionOwnership,
    QueryFrameBuilder,
    ResolutionOutcome,
    ResolverState,
    accounting_observation,
    accounting_observation_from_dict,
    accounting_observation_to_dict,
    accounting_observation_with_changes,
    budget_consumption,
    budget_consumption_from_json,
    budget_consumption_to_json,
    budget_consumption_with_changes,
    build_evidence_package,
    candidate as resolution_candidate,
    candidate_from_json,
    candidate_to_json,
    candidate_with_changes,
    canonical_proposition_references,
    capture_resolution_budget,
    disclosure_decision,
    empty_candidate,
    evidence_package_to_dict,
    evidence_reference,
    evidence_reference_from_dict,
    evidence_reference_from_json,
    evidence_reference_to_dict,
    evidence_reference_to_json,
    evidence_reference_with_changes,
    feature_set,
    feature_set_from_dict,
    feature_set_from_json,
    feature_set_to_dict,
    feature_set_to_json,
    feature_set_with_changes,
    inheritance_provenance,
    proposition_evidence_record,
    proposition_evidence_record_to_dict,
    proposition_trust_inputs,
    proposition_validity_inputs,
    query_frame_from_json,
    query_frame_to_json,
    query_frame_with_changes,
    resolution_budget_from_dict,
    resolution_budget_from_json,
    resolution_budget_to_dict,
    resolution_budget_to_json,
    resolution_budget_with_changes,
    resolution_result,
    resolution_result_from_dict,
    resolution_result_from_json,
    resolution_result_to_dict,
    resolution_result_to_json,
    resolution_result_with_changes,
    resolver_result,
    resolver_result_from_dict,
    resolver_result_from_json,
    resolver_result_to_dict,
    resolver_result_to_json,
    resolver_result_with_changes,
    rewrite_trace_step,
    validate_accounting_observation,
    validate_budget_consumption,
    validate_candidate,
    validate_evidence_reference,
    validate_feature_set,
    validate_inheritance_provenance,
    validate_resolution_budget,
    validate_rewrite_trace_step,
)


def internal_scope() -> dict:
    result = scope_key(namespace="support", context_fingerprint="account:one")
    return result


def internal_budget() -> dict:
    result = capture_resolution_budget(lambda: 1_000_000_000)
    return result


def internal_frame() -> dict:
    engram = Engram()
    result = QueryFrameBuilder(engram, lambda: 1_000_000_000, lambda: datetime(2026, 8, 12, tzinfo=UTC)).build(
        "What's PostgreSQL?",
        internal_scope(),
        required_metadata={"channel": "support"},
        required_source_label="tapestry:released",
        diagnostic_seed="request-1",
        budget=internal_budget(),
    )
    return result


def internal_evidence() -> dict:
    result = evidence_reference(
        evidence_id="proposition-1",
        resolver="structured_graph",
        kind=EvidenceKind.PROPOSITION,
        scope=internal_scope(),
        provenance={"query": "entity"},
        diagnostics={"row": 1},
    )
    return result


def proposition_record() -> dict:
    result = proposition_evidence_record(
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
            scope=internal_scope(),
            policy_version="proposition-disclosure-v1",
        ),
        path=("proposition-full",),
        selection_reasons=("canonical_complete", "structured_match"),
    )
    return result


def internal_candidate(source: CandidateSource = CandidateSource.EXACT) -> dict:
    result = resolution_candidate(
        candidate_id=f"candidate:{source.value}:stmt-1",
        statement_id="stmt-1",
        response="PostgreSQL is an open-source relational database.",
        source=source,
        features=feature_set(values={"exact_match": 1.0}, unavailable=("semantic_score",)),
        evidence=(internal_evidence(),),
        scope=internal_scope(),
        lifecycle=LifecycleState.ACTIVE,
        provenance={"origin": "canonical"},
        diagnostics={"key": "canonical"},
    )
    return result


def internal_resolver_result(candidate_value=()) -> dict:
    candidates = (candidate_value,) if candidate_value else ()
    accounting = (accounting_observation(candidate_value["statement_id"], ("postgresql",)),) if candidate_value else ()
    result = resolver_result(
        resolver="exact",
        state=ResolverState.COMPLETED,
        candidates=candidates,
        accounting=accounting,
        diagnostics={"owners": len(candidates)},
        consumption=budget_consumption(elapsed_ns=5, resolvers=1, candidates=len(candidates)),
    )
    return result


def test_budget_codec_and_measurement_start_are_deterministic() -> None:
    budget = internal_budget()

    assert type(budget) is dict
    assert budget["started_ns"] == 1_000_000_000
    assert resolution_budget_from_dict(resolution_budget_to_dict(budget)) == budget
    assert resolution_budget_from_json(resolution_budget_to_json(budget)) == budget
    assert resolution_budget_to_json(budget) == json_dumps(
        resolution_budget_to_dict(budget), sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
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
        resolution_budget_with_changes(internal_budget(), changes)


def test_budget_consumption_codec_and_ledger_exhaustion() -> None:
    budget = resolution_budget_with_changes(internal_budget(), {"max_candidates": 2, "max_evidence": 1})
    ledger = BudgetLedger(budget)
    first = ledger.add(budget_consumption(resolvers=1, candidates=1, evidence=1))
    second = ledger.add(budget_consumption(resolvers=1, candidates=2))

    assert type(second) is dict
    assert first["exhausted_dimensions"] == ()
    assert second["exhausted_dimensions"] == ("candidates",)
    assert ledger.remaining_candidates() == 0
    assert ledger.remaining_evidence() == 0
    assert budget_consumption_from_json(budget_consumption_to_json(second)) == second
    copied = validate_budget_consumption(second)
    assert copied == second and copied is not second


def test_frame_builder_captures_one_clock_and_shared_preprocessing() -> None:
    engram = Engram()
    calls = []

    def utc_clock() -> datetime:
        calls.append("utc")
        result = datetime(2026, 8, 12, 12, 30, tzinfo=UTC)
        return result

    frame = QueryFrameBuilder(engram, lambda: 9, utc_clock).build("What's PostgreSQL?", internal_scope(), diagnostic_seed="same")

    assert frame["original_text"] == "What's PostgreSQL?"
    assert frame["resolved_text"] == "What is PostgreSQL?"
    assert frame["identity"] == build_standalone_identity("What's PostgreSQL?", internal_scope())
    assert frame["expected_object_type"] == ExpectedObjectType.UNKNOWN
    assert frame["inheritance"] == ()
    assert frame["rewrite_chain"] == ()
    assert frame["eligibility_context"]["evaluation_time"] == "2026-08-12T12:30:00Z"
    assert calls == ["utc"]
    assert frame["diagnostic_id"].startswith("resolution:sha256:")


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

    assert frame["budget"]["started_ns"] == 500
    assert calls == ["monotonic"]


def test_frame_builder_validates_authoritative_identity_scope() -> None:
    identity = build_standalone_identity("Where is PostgreSQL?", scope_key(namespace="other"))

    with pytest_raises(InvalidRequestError, match="identity scope"):
        QueryFrameBuilder(Engram(), lambda: 1, lambda: datetime(2026, 8, 12, tzinfo=UTC)).build(
            "Where is PostgreSQL?", internal_scope(), identity=identity
        )


def test_frame_codec_preserves_traces_and_isolates_metadata() -> None:
    frame = query_frame_with_changes(
        internal_frame(),
        {
            "inheritance": (inheritance_provenance("relation", 3),),
            "rewrite_chain": (rewrite_trace_step("rule-1", "input", "output"),),
        },
    )
    decoded = query_frame_from_json(query_frame_to_json(frame))

    assert decoded == frame
    assert type(decoded["inheritance"][0]) is dict
    assert type(decoded["rewrite_chain"][0]) is dict
    inherited_copy = validate_inheritance_provenance(decoded["inheritance"][0])
    rewrite_copy = validate_rewrite_trace_step(decoded["rewrite_chain"][0])
    assert inherited_copy == decoded["inheritance"][0] and inherited_copy is not decoded["inheritance"][0]
    assert rewrite_copy == decoded["rewrite_chain"][0] and rewrite_copy is not decoded["rewrite_chain"][0]
    assert isinstance(decoded["required_metadata"], dict)
    decoded["required_metadata"]["changed"] = True
    assert "changed" not in frame["required_metadata"]


def test_frame_rejects_identity_and_eligibility_scope_mismatch() -> None:
    frame = internal_frame()

    with pytest_raises(InvalidRequestError, match="identity scope"):
        query_frame_with_changes(frame, {"scope": scope_key(namespace="other")})
    changed_identity = dict(frame["identity"])
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
    assert feature_set_from_json(feature_set_to_json(features)) == features
    assert feature_set_from_dict(serialized) == features
    assert features["values"]["exact_match"] == 0.0
    assert "semantic_score" not in features["values"]
    copied = validate_feature_set(features)
    assert copied == features
    assert copied is not features
    assert copied["values"] is not features["values"]
    features["values"]["new"] = 1.0
    assert "new" not in copied["values"]
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
    candidate = candidate_with_changes(internal_candidate(), {"response": "Café ☕ — exact accepted text"})
    reference = internal_evidence()
    serialized_reference = evidence_reference_to_dict(reference)

    assert type(candidate) is dict
    assert candidate_from_json(candidate_to_json(candidate)) == candidate
    copied_candidate = validate_candidate(candidate)
    assert copied_candidate == candidate and copied_candidate is not candidate
    assert evidence_reference_from_json(evidence_reference_to_json(reference)) == reference
    assert evidence_reference_from_dict(serialized_reference) == reference
    assert "null" not in candidate_to_json(candidate)


def test_evidence_reference_is_an_exact_isolated_dictionary() -> None:
    reference = internal_evidence()
    copied = validate_evidence_reference(reference)

    assert type(reference) is dict
    assert copied == reference
    assert copied is not reference
    assert copied["scope"] is not reference["scope"]
    assert copied["provenance"] is not reference["provenance"]
    assert copied["diagnostics"] is not reference["diagnostics"]
    reference["provenance"]["changed"] = True
    assert "changed" not in copied["provenance"]
    malformed = dict(reference)
    malformed["unexpected"] = True
    with pytest_raises(InvalidRequestError, match="invalid fields"):
        validate_evidence_reference(malformed)


def test_resolver_result_codec_and_state_invariants() -> None:
    result = internal_resolver_result(internal_candidate())
    observation = result["accounting"][0]

    assert resolver_result_from_json(resolver_result_to_json(result)) == result
    assert type(observation) is dict
    assert accounting_observation_from_dict(accounting_observation_to_dict(observation)) == observation
    copied = validate_accounting_observation(observation)
    assert copied == observation and copied is not observation
    with pytest_raises(InvalidRequestError, match="non-completed"):
        resolver_result_with_changes(result, {"state": ResolverState.FAILED})


def test_resolution_answer_codec_and_invariants() -> None:
    candidate = internal_candidate()
    resolver = internal_resolver_result(candidate)
    result = resolution_result(
        outcome=ResolutionOutcome.ANSWER,
        selected_candidate=candidate,
        selected_candidate_available=True,
        response_candidates=(candidate,),
        evidence=(),
        confidence=1.0,
        confidence_available=True,
        reason_codes=("exact_unique_eligible",),
        frame_diagnostics={"diagnostic_id": internal_frame()["diagnostic_id"]},
        resolver_results=(resolver,),
        budget=resolver["consumption"],
    )

    assert resolution_result_from_json(resolution_result_to_json(result)) == result
    selected = resolution_result_to_dict(result)["selected_candidate"]
    assert isinstance(selected, dict)
    assert selected["response"] == candidate["response"]
    lexical = candidate_with_changes(candidate, {"source": CandidateSource.SPARSE})
    fused = resolution_result_with_changes(
        result,
        {"selected_candidate": lexical, "response_candidates": (lexical,), "confidence": 0.81},
    )
    assert resolution_result_from_json(resolution_result_to_json(fused)) == fused
    with pytest_raises(InvalidRequestError, match="only the selected"):
        resolution_result_with_changes(result, {"selected_candidate": lexical})
    with pytest_raises(InvalidRequestError, match="positive confidence"):
        resolution_result_with_changes(result, {"confidence": 0.25, "confidence_available": False})
    with pytest_raises(InvalidRequestError, match="top-level evidence"):
        resolution_result_with_changes(result, {"evidence": (internal_evidence(),)})


def test_resolution_evidence_and_miss_invariants_use_concrete_empty_candidate() -> None:
    lexical = candidate_with_changes(internal_candidate(), {"source": CandidateSource.SPARSE})
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

    assert resolution_result_from_json(resolution_result_to_json(evidence_result)) == evidence_result
    assert resolution_result_from_json(resolution_result_to_json(miss)) == miss
    assert resolution_result_to_dict(miss)["selected_candidate"] == {}
    assert resolution_result_from_json(resolution_result_to_json(miss)) == miss
    with pytest_raises(InvalidRequestError, match="EVIDENCE requires"):
        resolution_result_with_changes(evidence_result, {"response_candidates": ()})
    with pytest_raises(InvalidRequestError, match="MISS cannot"):
        resolution_result_with_changes(miss, {"evidence": (internal_evidence(),)})
    with pytest_raises(InvalidRequestError, match="concrete empty candidate"):
        resolution_result_with_changes(miss, {"selected_candidate": lexical})
    with pytest_raises(InvalidRequestError, match="unavailable and zero"):
        resolution_result_with_changes(evidence_result, {"confidence": 0.5, "confidence_available": True})


def test_contract_loaders_reject_null_unknown_fields_and_unsupported_versions() -> None:
    budget = resolution_budget_to_dict(internal_budget())
    budget["unexpected"] = True
    with pytest_raises(InvalidRequestError, match="invalid fields"):
        resolution_budget_from_dict(budget)
    with pytest_raises(InvalidRequestError, match="must contain an object"):
        query_frame_from_json("null")
    with pytest_raises(InvalidRequestError, match="unsupported candidate schema_version"):
        candidate_with_changes(internal_candidate(), {"schema_version": 2})


def test_contracts_enforce_nested_byte_and_collection_bounds() -> None:
    with pytest_raises(InvalidRequestError, match="16384 UTF-8 bytes"):
        candidate_with_changes(internal_candidate(), {"diagnostics": {"detail": "x" * 16_385}})
    with pytest_raises(InvalidRequestError, match="item limit"):
        resolver_result_with_changes(
            internal_resolver_result(internal_candidate()), {"candidates": (internal_candidate(),) * 1_001}
        )
    with pytest_raises(InvalidRequestError, match="limit of 64"):
        budget_consumption_with_changes(
            budget_consumption(), {"exhausted_dimensions": tuple(f"d{index:02d}" for index in range(65))}
        )


@pytest_mark.parametrize(
    "factory",
    [
        lambda: resolution_budget_with_changes(internal_budget(), {"schema_version": 3}),
        lambda: budget_consumption_with_changes(budget_consumption(), {"schema_version": 2}),
        lambda: query_frame_with_changes(internal_frame(), {"schema_version": 3}),
        lambda: feature_set_with_changes(feature_set(), {"schema_version": 2}),
        lambda: evidence_reference_with_changes(internal_evidence(), {"schema_version": 2}),
        lambda: candidate_with_changes(internal_candidate(), {"schema_version": 2}),
        lambda: accounting_observation_with_changes(accounting_observation("stmt-1"), {"schema_version": 2}),
        lambda: resolver_result_with_changes(internal_resolver_result(), {"schema_version": 3}),
        lambda: resolution_result_with_changes(
            resolution_result(
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
            ),
            {"schema_version": 3},
        ),
    ],
)
def test_every_versioned_resolution_contract_rejects_unknown_versions(factory) -> None:
    with pytest_raises(InvalidRequestError, match="unsupported"):
        factory()


def test_resolver_result_current_field_set_is_exact() -> None:
    result = resolver_result_with_changes(
        internal_resolver_result(),
        {"resolver": "structured_graph", "proposition_evidence": (proposition_record(),)},
    )

    assert set(resolver_result_to_dict(result)) == set(
        {
            "schema_version",
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
    assert resolver_result_to_dict(result)["proposition_evidence"] == [proposition_evidence_record_to_dict(proposition_record())]
    assert resolver_result_from_json(resolver_result_to_json(result)) == result
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
        resolver_result_with_changes(internal_resolver_result(), {"proposition_evidence": (proposition_record(),)})


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
    package = build_evidence_package((proposition_record(),))
    evidence_result = resolution_result_with_changes(
        miss,
        {
            "outcome": ResolutionOutcome.EVIDENCE,
            "reason_codes": ("proposition_evidence_included",),
            "evidence_package_available": True,
            "evidence_package": package,
        },
    )

    assert evidence_result["schema_version"] == 1
    assert resolution_result_to_dict(evidence_result)["evidence_package_available"] is True
    assert resolution_result_to_dict(evidence_result)["evidence_package"] == evidence_package_to_dict(package)
    assert resolution_result_from_json(resolution_result_to_json(miss)) == miss
    assert resolution_result_from_json(resolution_result_to_json(evidence_result)) == evidence_result

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
                    resolver_result_with_changes(
                        internal_resolver_result(),
                        {"resolver": "structured_graph", "proposition_evidence": (proposition_record(),)},
                    ),
                )
            },
        )


def test_resolution_result_answer_and_miss_package_invariants() -> None:
    candidate = internal_candidate()
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

    assert resolution_result_from_json(resolution_result_to_json(answer)) == answer
    assert resolution_result_from_json(resolution_result_to_json(available_empty_miss)) == available_empty_miss
    with pytest_raises(InvalidRequestError, match="ANSWER cannot contain"):
        resolution_result_with_changes(
            answer,
            {
                "evidence_package_available": True,
                "evidence_package": build_evidence_package((proposition_record(),)),
            },
        )
    with pytest_raises(InvalidRequestError, match="MISS cannot contain"):
        resolution_result_with_changes(
            available_empty_miss,
            {"evidence_package": build_evidence_package((proposition_record(),))},
        )
