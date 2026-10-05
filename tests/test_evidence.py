"""Contract tests for Section 7 full-Proposition evidence and packaging."""

from json import dumps as json_dumps

from pytest import mark as pytest_mark, raises as pytest_raises

from engram.errors import InvalidRequestError
from engram.evidence import (
    EvidenceUsefulnessReason,
    canonicalize_proposition_evidence,
    evaluate_evidence_usefulness,
    evidence_usefulness_decision,
    evidence_usefulness_policy,
    evidence_usefulness_policy_from_dict,
    evidence_usefulness_policy_to_dict,
    validate_evidence_usefulness_policy,
)
from engram.identity import scope_key
from engram.resolution import (
    DisclosureBasis,
    EvidencePackageTruncationReason,
    PropositionOwnership,
    build_evidence_package,
    canonical_proposition_references,
    disclosure_decision,
    empty_evidence_package,
    evidence_package,
    evidence_package_from_dict,
    evidence_package_from_json,
    evidence_package_to_dict,
    evidence_package_to_json,
    feature_set,
    proposition_evidence_record,
    proposition_evidence_record_from_dict,
    proposition_evidence_record_from_json,
    proposition_evidence_record_to_dict,
    proposition_evidence_record_to_json,
    proposition_evidence_record_with_changes,
    proposition_trust_inputs,
    proposition_validity_inputs,
    validate_canonical_proposition_references,
    validate_disclosure_decision,
    validate_evidence_package,
    validate_proposition_evidence_record,
    validate_proposition_trust_inputs,
    validate_proposition_validity_inputs,
)

# Read-only inputs: proposition_evidence_record and the *_with_changes revalidation copy every nested component.
STRUCTURED_RECORD_VALUES = {
    "proposition_id": "proposition:01J5M6Q9J8",
    "source_resolver": "structured_graph",
    "source_contributions": ("structured_graph",),
    "features": feature_set(
        values={"structured_match": 1.0, "supplied_trust": 0.84},
        unavailable=("semantic_similarity",),
    ),
    "canonical_references": canonical_proposition_references(
        subject_entity_id="entity:alan-turing",
        predicate_id="predicate:birth-date",
        object_entity_id="entity:1912-06-23",
    ),
    "validity": proposition_validity_inputs(
        evaluation_time="2026-08-16T12:00:00Z",
        active=True,
        system_current=True,
        valid_time_current=True,
        valid_from="1912-06-23T00:00:00Z",
        valid_from_available=True,
    ),
    "trust": proposition_trust_inputs(
        trust_category="verified_public",
        trust_category_available=True,
        supplied_trust=0.84,
        supplied_trust_available=True,
    ),
    "disclosure": disclosure_decision(
        ownership=PropositionOwnership.PUBLIC,
        basis=DisclosureBasis.PUBLIC_RULE,
        scope=scope_key(namespace="support", context_fingerprint="account:one"),
    ),
    "path": ("proposition:01J5M6Q9J8",),
    "selection_reasons": ("canonical_complete", "structured_match"),
}
SEMANTIC_RECORD_CHANGES = {
    "source_resolver": "support_semantic",
    "source_contributions": ("support_semantic",),
    "features": feature_set(
        values={"semantic_similarity": 0.81, "supplied_trust": 0.84},
        unavailable=("structured_match",),
    ),
    "selection_reasons": ("canonical_complete", "semantic_similarity"),
}
POLICY_RECORD_FEATURES = feature_set(
    values={"canonical_completeness": 1.0, "structured_match": 1.0, "supplied_trust": 0.84},
    unavailable=("semantic_similarity", "source_agreement"),
)


def test_proposition_evidence_record_codec_is_deterministic_and_concrete() -> None:
    record = proposition_evidence_record(**STRUCTURED_RECORD_VALUES)
    encoded = proposition_evidence_record_to_json(record)

    serialized = proposition_evidence_record_to_dict(record)
    assert type(record) is dict
    assert proposition_evidence_record_from_dict(serialized) == record
    assert proposition_evidence_record_from_json(encoded) == record
    assert encoded == json_dumps(serialized, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    assert "null" not in encoded
    assert set(serialized) == {
        "proposition_id",
        "source_resolver",
        "source_contributions",
        "features",
        "canonical_references",
        "validity",
        "trust",
        "disclosure",
        "path",
        "selection_reasons",
    }
    copied = validate_proposition_evidence_record(record)
    assert copied == record
    assert copied is not record
    assert {"canonical_references", "validity", "trust", "disclosure", "features"} <= set(record)
    assert copied.get("canonical_references", {}) is not record.get("canonical_references", {})
    assert copied.get("validity", {}) is not record.get("validity", {})
    assert copied.get("trust", {}) is not record.get("trust", {})
    assert copied.get("disclosure", {}) is not record.get("disclosure", {})
    assert copied.get("features", {}) is not record.get("features", {})

    malformed = dict(record)
    malformed["unexpected"] = True
    with pytest_raises(InvalidRequestError, match="invalid fields"):
        validate_proposition_evidence_record(malformed)
    record["selection_reasons"] = ()
    with pytest_raises(InvalidRequestError, match="1 through 16"):
        validate_proposition_evidence_record(record)


def test_proposition_evidence_leaf_contracts_are_exact_validated_dictionaries() -> None:
    scope = scope_key(namespace="support", context_fingerprint="account:one")
    references = canonical_proposition_references("entity:subject", "predicate:relation", "entity:object")
    validity = proposition_validity_inputs(
        evaluation_time="2026-08-16T12:00:00Z",
        active=True,
        system_current=True,
        valid_time_current=True,
    )
    trust = proposition_trust_inputs(
        supplied_trust=0.0,
        supplied_trust_available=True,
    )
    disclosure = disclosure_decision(
        ownership=PropositionOwnership.PUBLIC,
        basis=DisclosureBasis.PUBLIC_RULE,
        scope=scope,
    )

    contracts = (
        (references, validate_canonical_proposition_references),
        (validity, validate_proposition_validity_inputs),
        (trust, validate_proposition_trust_inputs),
        (disclosure, validate_disclosure_decision),
    )
    for value, validator in contracts:
        copied = validator(value)
        assert copied == value
        assert copied is not value
        malformed = dict(value)
        malformed["unexpected"] = True
        with pytest_raises(InvalidRequestError):
            validator(malformed)

    scope["namespace"] = "mutated"
    assert disclosure.get("scope", {}).get("namespace", "") == "support"
    disclosure["authority_available"] = True
    with pytest_raises(InvalidRequestError):
        validate_disclosure_decision(disclosure)


def test_proposition_evidence_normalization_merges_sources_features_and_reasons_deterministically() -> None:
    structured = proposition_evidence_record(**STRUCTURED_RECORD_VALUES)
    semantic = proposition_evidence_record_with_changes(
        proposition_evidence_record(**STRUCTURED_RECORD_VALUES), SEMANTIC_RECORD_CHANGES
    )

    forward = canonicalize_proposition_evidence((structured, semantic, semantic))
    reverse = canonicalize_proposition_evidence((semantic, structured))

    assert forward == reverse
    assert len(forward) == 1
    merged = forward[0]
    merged_features = merged.get("features", {})
    assert merged.get("source_resolver", "") == "structured_graph"
    assert merged.get("source_contributions", ()) == ("structured_graph", "support_semantic")
    assert merged_features.get("values", {}) == {
        "semantic_similarity": 0.81,
        "source_agreement": 1.0,
        "structured_match": 1.0,
        "supplied_trust": 0.84,
    }
    assert "unavailable" in merged_features
    assert merged_features.get("unavailable", ()) == ()
    assert merged.get("selection_reasons", ()) == (
        "canonical_complete",
        "semantic_similarity",
        "source_agreement",
        "structured_match",
    )
    assert canonicalize_proposition_evidence(forward) == forward
    assert structured.get("source_contributions", ()) == ("structured_graph",)
    assert semantic.get("source_contributions", ()) == ("support_semantic",)


def test_proposition_evidence_normalization_orders_distinct_propositions_by_stable_id() -> None:
    first = proposition_evidence_record_with_changes(
        proposition_evidence_record(**STRUCTURED_RECORD_VALUES), {"proposition_id": "proposition:a", "path": ("proposition:a",)}
    )
    second = proposition_evidence_record_with_changes(
        proposition_evidence_record(**STRUCTURED_RECORD_VALUES), {"proposition_id": "proposition:b", "path": ("proposition:b",)}
    )

    normalized = canonicalize_proposition_evidence((second, first))

    assert tuple(record.get("proposition_id", "") for record in normalized) == ("proposition:a", "proposition:b")


@pytest_mark.parametrize(
    ("conflicting", "message"),
    [
        (
            proposition_evidence_record_with_changes(
                proposition_evidence_record_with_changes(
                    proposition_evidence_record(**STRUCTURED_RECORD_VALUES), SEMANTIC_RECORD_CHANGES
                ),
                {
                    "canonical_references": canonical_proposition_references(
                        "entity:alan-turing",
                        "predicate:birth-date",
                        "entity:different",
                    ),
                },
            ),
            "conflicting canonical references",
        ),
        (
            proposition_evidence_record_with_changes(
                proposition_evidence_record_with_changes(
                    proposition_evidence_record(**STRUCTURED_RECORD_VALUES), SEMANTIC_RECORD_CHANGES
                ),
                {"features": feature_set(values={"semantic_similarity": 0.81, "structured_match": 0.5, "supplied_trust": 0.84})},
            ),
            "conflicting measured feature structured_match",
        ),
        (
            proposition_evidence_record_with_changes(
                proposition_evidence_record_with_changes(
                    proposition_evidence_record(**STRUCTURED_RECORD_VALUES), SEMANTIC_RECORD_CHANGES
                ),
                {
                    "disclosure": validate_disclosure_decision(
                        {**STRUCTURED_RECORD_VALUES.get("disclosure", {}), "scope": scope_key(namespace="different")}
                    ),
                },
            ),
            "conflicting current evidence state",
        ),
    ],
)
def test_proposition_evidence_normalization_rejects_conflicts_in_either_order(conflicting, message) -> None:
    with pytest_raises(InvalidRequestError, match=message):
        canonicalize_proposition_evidence((proposition_evidence_record(**STRUCTURED_RECORD_VALUES), conflicting))
    with pytest_raises(InvalidRequestError, match=message):
        canonicalize_proposition_evidence((conflicting, proposition_evidence_record(**STRUCTURED_RECORD_VALUES)))


def test_proposition_evidence_normalization_enforces_input_source_and_cooperative_bounds() -> None:
    calls = 0

    def check() -> None:
        nonlocal calls
        calls += 1

    records = tuple(
        proposition_evidence_record_with_changes(
            proposition_evidence_record(**STRUCTURED_RECORD_VALUES),
            {"source_resolver": f"source_{index}", "source_contributions": (f"source_{index}",)},
        )
        for index in range(9)
    )

    with pytest_raises(InvalidRequestError, match="sources exceed the limit of 8"):
        canonicalize_proposition_evidence(records, check)
    with pytest_raises(InvalidRequestError, match="at most 1000"):
        canonicalize_proposition_evidence((proposition_evidence_record(**STRUCTURED_RECORD_VALUES),) * 1_001)
    assert calls == 10


def test_evidence_usefulness_policy_codec_and_frozen_hand_authored_floors() -> None:
    policy = evidence_usefulness_policy()
    serialized = evidence_usefulness_policy_to_dict(policy)

    assert evidence_usefulness_policy_from_dict(serialized) == policy
    assert serialized == {
        "canonical_completeness_floor": 1.0,
        "structured_match_floor": 1.0,
        "semantic_similarity_floor": 0.6,
        "source_agreement_floor": 1.0,
        "supplied_trust_floor": 0.0,
        "supplied_trust_floor_available": False,
    }
    with pytest_raises(InvalidRequestError, match="frozen at 0.6"):
        validate_evidence_usefulness_policy({**policy, "semantic_similarity_floor": 0.61})
    with pytest_raises(InvalidRequestError, match="invalid fields"):
        evidence_usefulness_policy_from_dict({**serialized, "coefficient": 0.5})


@pytest_mark.parametrize(
    ("field", "malformed", "message"),
    (
        ("semantic_similarity_floor", 1, "semantic_similarity_floor must be a float"),
        ("supplied_trust_floor_available", 0, "supplied_trust_floor_available must be a boolean"),
    ),
)
def test_evidence_usefulness_policy_decoder_rejects_wrong_wire_types(field, malformed, message) -> None:
    payload = evidence_usefulness_policy_to_dict(evidence_usefulness_policy())
    payload[field] = malformed

    with pytest_raises(InvalidRequestError, match=message):
        evidence_usefulness_policy_from_dict(payload)


def test_evidence_usefulness_accepts_exact_structured_and_semantic_floor_boundaries() -> None:
    policy = evidence_usefulness_policy()
    structured_record = proposition_evidence_record(**STRUCTURED_RECORD_VALUES)
    structured = evaluate_evidence_usefulness(
        policy,
        proposition_evidence_record_with_changes(structured_record, {"features": POLICY_RECORD_FEATURES}),
    )
    semantic = evaluate_evidence_usefulness(
        policy,
        proposition_evidence_record_with_changes(
            structured_record,
            {
                "features": feature_set(
                    values={"canonical_completeness": 1.0, "semantic_similarity": 0.60, "supplied_trust": 0.84},
                    unavailable=("source_agreement", "structured_match"),
                )
            },
        ),
    )

    structured_reasons = structured.get("reasons", ())
    semantic_reasons = semantic.get("reasons", ())
    assert structured.get("included", False) is True
    assert EvidenceUsefulnessReason.STRUCTURED_MATCH_QUALIFIED in structured_reasons
    assert EvidenceUsefulnessReason.SUPPLIED_TRUST_AVAILABLE in structured_reasons
    assert semantic.get("included", False) is True
    assert EvidenceUsefulnessReason.SEMANTIC_SIMILARITY_QUALIFIED in semantic_reasons
    assert tuple(reason.value for reason in semantic_reasons) == tuple(sorted(reason.value for reason in semantic_reasons))


def test_evidence_usefulness_distinguishes_unavailable_from_measured_zero() -> None:
    policy = evidence_usefulness_policy()
    structured_record = proposition_evidence_record(**STRUCTURED_RECORD_VALUES)
    signal_unavailable = evaluate_evidence_usefulness(
        policy,
        proposition_evidence_record_with_changes(
            structured_record,
            {
                "features": feature_set(
                    values={"canonical_completeness": 1.0, "supplied_trust": 0.84},
                    unavailable=("semantic_similarity", "source_agreement", "structured_match"),
                )
            },
        ),
    )
    signal_zero = evaluate_evidence_usefulness(
        policy,
        proposition_evidence_record_with_changes(
            structured_record,
            {
                "features": feature_set(
                    values={"canonical_completeness": 1.0, "semantic_similarity": 0.0, "supplied_trust": 0.84},
                    unavailable=("source_agreement", "structured_match"),
                )
            },
        ),
    )
    canonical_unavailable = evaluate_evidence_usefulness(
        policy,
        proposition_evidence_record_with_changes(
            structured_record,
            {
                "features": feature_set(
                    values={"structured_match": 1.0, "supplied_trust": 0.84},
                    unavailable=("canonical_completeness",),
                )
            },
        ),
    )
    canonical_zero = evaluate_evidence_usefulness(
        policy,
        proposition_evidence_record_with_changes(
            structured_record,
            {"features": feature_set(values={"canonical_completeness": 0.0, "structured_match": 1.0, "supplied_trust": 0.84})},
        ),
    )

    assert EvidenceUsefulnessReason.RETRIEVAL_SIGNAL_UNAVAILABLE in signal_unavailable.get("reasons", ())
    assert EvidenceUsefulnessReason.RETRIEVAL_SIGNAL_BELOW_FLOOR in signal_zero.get("reasons", ())
    assert EvidenceUsefulnessReason.CANONICAL_COMPLETENESS_UNAVAILABLE in canonical_unavailable.get("reasons", ())
    assert EvidenceUsefulnessReason.CANONICAL_COMPLETENESS_BELOW_FLOOR in canonical_zero.get("reasons", ())
    for decision in (signal_unavailable, signal_zero, canonical_unavailable, canonical_zero):
        assert "included" in decision
        assert not decision.get("included", False)


def test_evidence_usefulness_configured_trust_floor_distinguishes_absence_zero_and_boundary() -> None:
    policy = evidence_usefulness_policy(supplied_trust_floor=0.5, supplied_trust_floor_available=True)
    structured_record = proposition_evidence_record(**STRUCTURED_RECORD_VALUES)
    unavailable_record = proposition_evidence_record_with_changes(
        structured_record,
        {
            "trust": proposition_trust_inputs(),
            "features": feature_set(
                values={"canonical_completeness": 1.0, "structured_match": 1.0},
                unavailable=("semantic_similarity", "source_agreement", "supplied_trust"),
            ),
        },
    )
    measured_zero_record = proposition_evidence_record_with_changes(
        structured_record,
        {
            "trust": proposition_trust_inputs(
                supplied_trust=0.0,
                supplied_trust_available=True,
            ),
            "features": feature_set(
                values={"canonical_completeness": 1.0, "structured_match": 1.0, "supplied_trust": 0.0},
                unavailable=("semantic_similarity", "source_agreement"),
            ),
        },
    )
    boundary_record = proposition_evidence_record_with_changes(
        structured_record,
        {
            "trust": proposition_trust_inputs(
                supplied_trust=0.5,
                supplied_trust_available=True,
            ),
            "features": feature_set(
                values={"canonical_completeness": 1.0, "structured_match": 1.0, "supplied_trust": 0.5},
                unavailable=("semantic_similarity", "source_agreement"),
            ),
        },
    )

    unavailable = evaluate_evidence_usefulness(policy, unavailable_record)
    measured_zero = evaluate_evidence_usefulness(policy, measured_zero_record)
    boundary = evaluate_evidence_usefulness(policy, boundary_record)

    assert EvidenceUsefulnessReason.SUPPLIED_TRUST_REQUIRED_UNAVAILABLE in unavailable.get("reasons", ())
    assert EvidenceUsefulnessReason.SUPPLIED_TRUST_BELOW_FLOOR in measured_zero.get("reasons", ())
    assert EvidenceUsefulnessReason.SUPPLIED_TRUST_FLOOR_SATISFIED in boundary.get("reasons", ())
    assert "included" in unavailable
    assert unavailable.get("included", False) is False
    assert "included" in measured_zero
    assert measured_zero.get("included", False) is False
    assert boundary.get("included", False) is True


def test_evidence_usefulness_default_policy_does_not_rank_or_require_supplied_trust() -> None:
    structured_record = proposition_evidence_record(**STRUCTURED_RECORD_VALUES)
    unavailable_record = proposition_evidence_record_with_changes(
        structured_record,
        {
            "trust": proposition_trust_inputs(),
            "features": feature_set(
                values={"canonical_completeness": 1.0, "structured_match": 1.0},
                unavailable=("semantic_similarity", "source_agreement", "supplied_trust"),
            ),
        },
    )
    measured_zero_record = proposition_evidence_record_with_changes(
        structured_record,
        {
            "trust": proposition_trust_inputs(
                supplied_trust=0.0,
                supplied_trust_available=True,
            ),
            "features": feature_set(
                values={"canonical_completeness": 1.0, "structured_match": 1.0, "supplied_trust": 0.0},
                unavailable=("semantic_similarity", "source_agreement"),
            ),
        },
    )

    policy = evidence_usefulness_policy()
    unavailable = evaluate_evidence_usefulness(policy, unavailable_record)
    measured_zero = evaluate_evidence_usefulness(policy, measured_zero_record)

    assert unavailable.get("included", False) is True
    assert measured_zero.get("included", False) is True
    assert EvidenceUsefulnessReason.SUPPLIED_TRUST_UNAVAILABLE in unavailable.get("reasons", ())
    assert EvidenceUsefulnessReason.SUPPLIED_TRUST_AVAILABLE in measured_zero.get("reasons", ())


def test_evidence_usefulness_decision_rejects_inconsistent_external_reason_sets() -> None:
    with pytest_raises(InvalidRequestError, match="inconsistent reasons"):
        evidence_usefulness_decision(
            proposition_id="proposition:one",
            included=True,
            reasons=(EvidenceUsefulnessReason.RETRIEVAL_SIGNAL_BELOW_FLOOR,),
        )
    with pytest_raises(InvalidRequestError, match="requires an exclusion reason"):
        evidence_usefulness_decision(
            proposition_id="proposition:one",
            included=False,
            reasons=(EvidenceUsefulnessReason.STRUCTURED_MATCH_QUALIFIED,),
        )


def test_evidence_usefulness_source_agreement_cannot_rescue_subfloor_retrieval() -> None:
    record = proposition_evidence_record_with_changes(
        proposition_evidence_record(**STRUCTURED_RECORD_VALUES),
        {
            "features": feature_set(
                values={
                    "canonical_completeness": 1.0,
                    "semantic_similarity": 0.59,
                    "source_agreement": 1.0,
                    "structured_match": 0.0,
                    "supplied_trust": 0.84,
                },
            )
        },
    )

    decision = evaluate_evidence_usefulness(evidence_usefulness_policy(), record)

    assert "included" in decision
    assert decision.get("included", False) is False
    assert EvidenceUsefulnessReason.SOURCE_AGREEMENT_QUALIFIED in decision.get("reasons", ())
    assert EvidenceUsefulnessReason.RETRIEVAL_SIGNAL_BELOW_FLOOR in decision.get("reasons", ())


def test_proposition_evidence_distinguishes_unavailable_trust_from_measured_zero() -> None:
    unavailable = proposition_evidence_record_with_changes(
        proposition_evidence_record(**STRUCTURED_RECORD_VALUES), {"trust": proposition_trust_inputs()}
    )
    measured_zero = proposition_evidence_record_with_changes(
        proposition_evidence_record(**STRUCTURED_RECORD_VALUES),
        {
            "trust": proposition_trust_inputs(
                supplied_trust=0.0,
                supplied_trust_available=True,
            )
        },
    )

    unavailable_trust = unavailable.get("trust", {})
    measured_zero_trust = measured_zero.get("trust", {})
    assert {"supplied_trust", "supplied_trust_available"} <= set(unavailable_trust)
    assert {"supplied_trust", "supplied_trust_available"} <= set(measured_zero_trust)
    assert unavailable_trust.get("supplied_trust", 0.0) == measured_zero_trust.get("supplied_trust", 0.0) == 0.0
    assert unavailable_trust.get("supplied_trust_available", False) is False
    assert measured_zero_trust.get("supplied_trust_available", False) is True
    assert proposition_evidence_record_from_json(proposition_evidence_record_to_json(unavailable)) == unavailable
    assert proposition_evidence_record_from_json(proposition_evidence_record_to_json(measured_zero)) == measured_zero


@pytest_mark.parametrize(
    ("apply_invalid_change", "internal_message"),
    [
        (lambda record: proposition_evidence_record_with_changes(record, {"proposition_id": "x" * 257}), "256 UTF-8 bytes"),
        (lambda record: proposition_evidence_record_with_changes(record, {"proposition_id": "proposition id"}), "whitespace"),
        (
            lambda record: proposition_evidence_record_with_changes(record, {"selection_reasons": ("reason with spaces",)}),
            "whitespace",
        ),
        (
            lambda record: proposition_evidence_record_with_changes(record, {"path": ("proposition:other",)}),
            "singleton proposition_id",
        ),
        (lambda record: proposition_evidence_record_with_changes(record, {"path": ()}), "singleton proposition_id"),
        (lambda record: proposition_evidence_record_with_changes(record, {"selection_reasons": ()}), "1 through 16"),
        (lambda record: proposition_evidence_record_with_changes(record, {"selection_reasons": ("z", "a")}), "unique and sorted"),
        (
            lambda record: proposition_evidence_record_with_changes(
                record, {"selection_reasons": tuple(f"r{i:02d}" for i in range(17))}
            ),
            "1 through 16",
        ),
        (lambda record: proposition_evidence_record_with_changes(record, {"source_contributions": ()}), "1 through 8"),
        (
            lambda record: proposition_evidence_record_with_changes(record, {"source_contributions": ("semantic_proposition",)}),
            "must be present",
        ),
        (
            lambda record: proposition_evidence_record_with_changes(
                record,
                {"validity": validate_proposition_validity_inputs({**record.get("validity", {}), "active": False})},
            ),
            "active conflicts with the disclosed invalidation boundary",
        ),
        (
            lambda record: proposition_evidence_record_with_changes(
                record,
                {"validity": validate_proposition_validity_inputs({**record.get("validity", {}), "system_current": False})},
            ),
            "system_current conflicts with the disclosed system interval",
        ),
        (
            lambda record: proposition_evidence_record_with_changes(
                record,
                {"validity": validate_proposition_validity_inputs({**record.get("validity", {}), "valid_time_current": False})},
            ),
            "conflicts with the disclosed",
        ),
        (
            lambda record: proposition_evidence_record_with_changes(
                record,
                {"trust": proposition_trust_inputs(supplied_trust=0.5, supplied_trust_available=False)},
            ),
            "must be zero when unavailable",
        ),
        (
            lambda record: proposition_evidence_record_with_changes(
                record,
                {
                    "disclosure": disclosure_decision(
                        ownership=PropositionOwnership.COMPANY,
                        basis=DisclosureBasis.PUBLIC_RULE,
                        scope=scope_key(namespace="support"),
                    )
                },
            ),
            "trusted scope authority",
        ),
    ],
)
def test_proposition_evidence_rejects_malformed_or_ambiguous_values(apply_invalid_change, internal_message) -> None:
    with pytest_raises(InvalidRequestError, match=internal_message):
        apply_invalid_change(proposition_evidence_record(**STRUCTURED_RECORD_VALUES))


def test_company_disclosure_requires_exact_scope_authority_provenance() -> None:
    disclosure = disclosure_decision(
        ownership=PropositionOwnership.COMPANY,
        basis=DisclosureBasis.TRUSTED_SCOPE_AUTHORITY,
        scope=scope_key(namespace="support", context_fingerprint="tenant:acme"),
        authority="visibility-authority-v3",
        authority_available=True,
    )
    record = proposition_evidence_record_with_changes(
        proposition_evidence_record(**STRUCTURED_RECORD_VALUES), {"disclosure": disclosure}
    )

    assert proposition_evidence_record_from_json(proposition_evidence_record_to_json(record)) == record
    assert record.get("disclosure", {}).get("scope", {}) == scope_key(namespace="support", context_fingerprint="tenant:acme")


@pytest_mark.parametrize("field", ["raw_proposition", "passage", "proof", "credential", "cypher", "embedding", "properties"])
def test_proposition_evidence_decoder_rejects_excluded_payload_fields(field: str) -> None:
    payload = proposition_evidence_record_to_dict(proposition_evidence_record(**STRUCTURED_RECORD_VALUES))
    payload[field] = "secret"

    with pytest_raises(InvalidRequestError, match="invalid fields"):
        proposition_evidence_record_from_dict(payload)


def test_proposition_evidence_decoder_rejects_nested_unknown_fields() -> None:
    unknown = proposition_evidence_record_to_dict(proposition_evidence_record(**STRUCTURED_RECORD_VALUES))
    assert "validity" in unknown
    validity = unknown.get("validity", {})
    assert isinstance(validity, dict)
    validity["unexpected"] = "secret"
    with pytest_raises(InvalidRequestError, match="invalid fields"):
        proposition_evidence_record_from_dict(unknown)


def test_evidence_package_codec_canonicalizes_and_deduplicates() -> None:
    first = proposition_evidence_record(**STRUCTURED_RECORD_VALUES)
    second = proposition_evidence_record_with_changes(
        first,
        {"proposition_id": "proposition:01J5M6Q9J9", "path": ("proposition:01J5M6Q9J9",)},
    )

    package = build_evidence_package((second, first, first))

    expected_ids = (first.get("proposition_id", ""), second.get("proposition_id", ""))
    assert package.__class__ is dict
    assert tuple(record.get("proposition_id", "") for record in package.get("records", ())) == expected_ids
    assert package.get("retained_count", 0) == 2
    assert package.get("omitted_count", 0) == 1
    assert package.get("truncated", False) is True
    assert package.get("truncation_reasons", ()) == (EvidencePackageTruncationReason.DUPLICATE_PROPOSITION_ID,)
    serialized = evidence_package_to_dict(package)
    encoded = evidence_package_to_json(package)
    assert evidence_package_from_dict(serialized) == package
    assert evidence_package_from_json(encoded) == package
    assert "null" not in encoded
    assert evidence_package_to_json(empty_evidence_package()) == evidence_package_to_json(build_evidence_package(()))
    copied = validate_evidence_package(package)
    assert copied == package
    assert copied is not package
    assert copied.get("records", ())[0] is not package.get("records", ())[0]


def test_evidence_package_rejects_conflicting_same_id_projections() -> None:
    conflict = proposition_evidence_record_with_changes(
        proposition_evidence_record(**STRUCTURED_RECORD_VALUES), {"features": feature_set(values={"structured_match": 0.5})}
    )

    with pytest_raises(InvalidRequestError, match="conflicting Proposition evidence projections"):
        build_evidence_package((proposition_evidence_record(**STRUCTURED_RECORD_VALUES), conflict))


def test_evidence_package_applies_count_limit_after_canonical_ordering() -> None:
    records = tuple(
        proposition_evidence_record_with_changes(
            proposition_evidence_record(**STRUCTURED_RECORD_VALUES),
            {"proposition_id": f"proposition:{index:02d}", "path": (f"proposition:{index:02d}",)},
        )
        for index in reversed(range(12))
    )

    package = build_evidence_package(records)

    assert package.get("retained_count", 0) == 10
    assert package.get("omitted_count", 0) == 2
    assert tuple(record.get("proposition_id", "") for record in package.get("records", ())) == tuple(
        f"proposition:{index:02d}" for index in range(10)
    )
    assert package.get("truncation_reasons", ()) == (EvidencePackageTruncationReason.RECORD_LIMIT,)
    assert len(evidence_package_to_json(package).encode("utf-8")) <= 65_536


def test_evidence_package_applies_complete_serialized_byte_limit() -> None:
    records = tuple(
        proposition_evidence_record_with_changes(
            proposition_evidence_record(**STRUCTURED_RECORD_VALUES),
            {
                "proposition_id": f"proposition:{index:02d}",
                "path": (f"proposition:{index:02d}",),
                "selection_reasons": tuple(f"reason_{reason:02d}_{'x' * 70}" for reason in range(16)),
            },
        )
        for index in range(5)
    )

    package = build_evidence_package(records, max_bytes=5_000)

    retained_count = package.get("retained_count", 0)
    assert 0 < retained_count < len(records)
    assert package.get("omitted_count", 0) == len(records) - retained_count
    assert package.get("truncation_reasons", ()) == (EvidencePackageTruncationReason.SERIALIZED_SIZE_LIMIT,)
    assert len(evidence_package_to_json(package).encode("utf-8")) <= 5_000


def test_evidence_package_hard_byte_limit_truncates_before_construction() -> None:
    large_features = feature_set(values={f"feature_{index:02d}_{'x' * 75}": 1.0 for index in range(64)})
    records = tuple(
        proposition_evidence_record_with_changes(
            proposition_evidence_record(**STRUCTURED_RECORD_VALUES),
            {
                "proposition_id": f"proposition:{index:02d}",
                "path": (f"proposition:{index:02d}",),
                "features": large_features,
                "selection_reasons": tuple(f"reason_{reason:02d}_{'x' * 70}" for reason in range(16)),
            },
        )
        for index in range(10)
    )

    package = build_evidence_package(records)

    assert "retained_count" in package
    retained_count = package.get("retained_count", 0)
    assert retained_count < len(records)
    assert package.get("omitted_count", 0) == len(records) - retained_count
    assert package.get("truncation_reasons", ()) == (EvidencePackageTruncationReason.SERIALIZED_SIZE_LIMIT,)
    assert len(evidence_package_to_json(package).encode("utf-8")) <= 65_536
    with pytest_raises(InvalidRequestError, match="65536 UTF-8 bytes"):
        evidence_package(records, 10, 0, False, ())


@pytest_mark.parametrize(
    ("changes", "message"),
    [
        ({"records": (proposition_evidence_record(**STRUCTURED_RECORD_VALUES),) * 11, "retained_count": 11}, "limit of 10"),
        ({"records": (proposition_evidence_record(**STRUCTURED_RECORD_VALUES),), "retained_count": 0}, "must equal"),
        ({"omitted_count": 1, "truncated": False}, "truncated must equal"),
        (
            {
                "omitted_count": 1,
                "truncated": True,
                "truncation_reasons": (),
            },
            "present exactly when truncated",
        ),
    ],
)
def test_evidence_package_rejects_inconsistent_envelopes(changes, message) -> None:
    with pytest_raises(InvalidRequestError, match=message):
        validate_evidence_package({**empty_evidence_package(), **changes})


def test_evidence_package_decoder_is_closed_and_rejects_unknown_reasons() -> None:
    payload = evidence_package_to_dict(empty_evidence_package())
    payload["unexpected"] = True
    with pytest_raises(InvalidRequestError, match="invalid fields"):
        evidence_package_from_dict(payload)

    payload = evidence_package_to_dict(empty_evidence_package())
    payload.update({"omitted_count": 1, "truncated": True, "truncation_reasons": ["content_filtered"]})
    with pytest_raises(InvalidRequestError, match="unsupported evidence package truncation reason"):
        evidence_package_from_dict(payload)


def test_evidence_package_builder_validates_requested_limits() -> None:
    record = proposition_evidence_record(**STRUCTURED_RECORD_VALUES)
    with pytest_raises(InvalidRequestError, match="max_records"):
        build_evidence_package((record,), max_records=11)
    with pytest_raises(InvalidRequestError, match="max_bytes"):
        build_evidence_package((record,), max_bytes=255)
    with pytest_raises(InvalidRequestError, match="input exceeds the limit of 1000"):
        build_evidence_package((record,) * 1_001)


def test_evidence_package_json_decoder_rejects_oversized_input_before_parsing() -> None:
    with pytest_raises(InvalidRequestError, match="65536 UTF-8 bytes"):
        evidence_package_from_json("{" + " " * 65_536 + "}")
