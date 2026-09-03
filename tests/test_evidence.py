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
    evidence_usefulness_policy_from_json,
    evidence_usefulness_policy_to_dict,
    evidence_usefulness_policy_to_json,
    evidence_usefulness_policy_with_changes,
)
from engram.identity import scope_key
from engram.resolution import (
    DisclosureBasis,
    EvidencePackageTruncationReason,
    PropositionOwnership,
    build_evidence_package,
    canonical_proposition_references,
    canonical_proposition_references_from_dict,
    canonical_proposition_references_to_dict,
    disclosure_decision,
    disclosure_decision_from_dict,
    disclosure_decision_to_dict,
    disclosure_decision_with_changes,
    empty_evidence_package,
    evidence_package,
    evidence_package_from_dict,
    evidence_package_from_json,
    evidence_package_to_dict,
    evidence_package_to_json,
    evidence_package_with_changes,
    feature_set,
    proposition_evidence_record,
    proposition_evidence_record_from_dict,
    proposition_evidence_record_from_json,
    proposition_evidence_record_to_dict,
    proposition_evidence_record_to_json,
    proposition_evidence_record_with_changes,
    proposition_trust_inputs,
    proposition_trust_inputs_from_dict,
    proposition_trust_inputs_to_dict,
    proposition_validity_inputs,
    proposition_validity_inputs_from_dict,
    proposition_validity_inputs_to_dict,
    proposition_validity_inputs_with_changes,
    validate_canonical_proposition_references,
    validate_disclosure_decision,
    validate_evidence_package,
    validate_proposition_evidence_record,
    validate_proposition_trust_inputs,
    validate_proposition_validity_inputs,
)


def internal_record() -> dict:
    result = proposition_evidence_record(
        proposition_id="proposition:01J5M6Q9J8",
        source_resolver="structured_graph",
        source_contributions=("structured_graph",),
        features=feature_set(
            values={"structured_match": 1.0, "supplied_trust": 0.84},
            unavailable=("semantic_similarity",),
        ),
        canonical_references=canonical_proposition_references(
            subject_entity_id="entity:alan-turing",
            predicate_id="predicate:birth-date",
            object_entity_id="entity:1912-06-23",
        ),
        validity=proposition_validity_inputs(
            evaluation_time="2026-08-16T12:00:00Z",
            active=True,
            system_current=True,
            valid_time_current=True,
            valid_from="1912-06-23T00:00:00Z",
            valid_from_available=True,
        ),
        trust=proposition_trust_inputs(
            trust_category="verified_public",
            trust_category_available=True,
            supplied_trust=0.84,
            supplied_trust_available=True,
            supplied_trust_version=3,
            supplied_trust_version_available=True,
        ),
        disclosure=disclosure_decision(
            ownership=PropositionOwnership.PUBLIC,
            basis=DisclosureBasis.PUBLIC_RULE,
            scope=scope_key(namespace="support", context_fingerprint="account:one"),
            policy_version="proposition-disclosure-v1",
        ),
        path=("proposition:01J5M6Q9J8",),
        selection_reasons=("canonical_complete", "structured_match"),
    )
    return result


def change_record(value: object, **changes: object) -> dict:
    result = proposition_evidence_record_with_changes(value, changes)
    return result


def semantic_record() -> dict:
    result = proposition_evidence_record_with_changes(
        internal_record(),
        {
            "source_resolver": "support_semantic",
            "source_contributions": ("support_semantic",),
            "features": feature_set(
                values={"semantic_similarity": 0.81, "supplied_trust": 0.84},
                unavailable=("structured_match",),
            ),
            "selection_reasons": ("canonical_complete", "semantic_similarity"),
        },
    )
    return result


def policy_record(*, features=()) -> dict:
    selected_features = (
        features
        if type(features) is dict
        else feature_set(
            values={"canonical_completeness": 1.0, "structured_match": 1.0, "supplied_trust": 0.84},
            unavailable=("semantic_similarity", "source_agreement"),
        )
    )
    result = proposition_evidence_record_with_changes(internal_record(), {"features": selected_features})
    return result


def test_proposition_evidence_record_codec_is_deterministic_and_concrete() -> None:
    record = internal_record()
    encoded = proposition_evidence_record_to_json(record)

    serialized = proposition_evidence_record_to_dict(record)
    assert type(record) is dict
    assert proposition_evidence_record_from_dict(serialized) == record
    assert proposition_evidence_record_from_json(encoded) == record
    assert encoded == json_dumps(serialized, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    assert "null" not in encoded
    assert set(serialized) == {
        "schema_version",
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
    assert copied["canonical_references"] is not record["canonical_references"]
    assert copied["validity"] is not record["validity"]
    assert copied["trust"] is not record["trust"]
    assert copied["disclosure"] is not record["disclosure"]
    assert copied["features"] is not record["features"]

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
        supplied_trust_version=1,
        supplied_trust_version_available=True,
    )
    disclosure = disclosure_decision(
        ownership=PropositionOwnership.PUBLIC,
        basis=DisclosureBasis.PUBLIC_RULE,
        scope=scope,
        policy_version="proposition-disclosure-v1",
    )

    contracts = (
        (
            references,
            validate_canonical_proposition_references,
            canonical_proposition_references_to_dict,
            canonical_proposition_references_from_dict,
        ),
        (
            validity,
            validate_proposition_validity_inputs,
            proposition_validity_inputs_to_dict,
            proposition_validity_inputs_from_dict,
        ),
        (trust, validate_proposition_trust_inputs, proposition_trust_inputs_to_dict, proposition_trust_inputs_from_dict),
        (disclosure, validate_disclosure_decision, disclosure_decision_to_dict, disclosure_decision_from_dict),
    )
    for value, validator, serializer, decoder in contracts:
        copied = validator(value)
        serialized = serializer(value)
        decoded = decoder(serialized)
        assert type(value) is dict
        assert type(copied) is dict
        assert copied == value
        assert copied is not value
        assert decoded == value
        malformed = dict(value)
        malformed["unexpected"] = True
        with pytest_raises(InvalidRequestError):
            validator(malformed)

    scope["namespace"] = "mutated"
    assert disclosure["scope"]["namespace"] == "support"
    disclosure["authority_available"] = True
    with pytest_raises(InvalidRequestError):
        validate_disclosure_decision(disclosure)


def test_proposition_trust_inputs_accept_available_zero_revision() -> None:
    result = proposition_trust_inputs(
        supplied_trust=0.75,
        supplied_trust_available=True,
        supplied_trust_version=0,
        supplied_trust_version_available=True,
    )

    assert result["supplied_trust_version"] == 0
    assert result["supplied_trust_version_available"] is True


def test_proposition_evidence_normalization_merges_sources_features_and_reasons_deterministically() -> None:
    structured = internal_record()
    semantic = semantic_record()

    forward = canonicalize_proposition_evidence((structured, semantic, semantic))
    reverse = canonicalize_proposition_evidence((semantic, structured))

    assert forward == reverse
    assert len(forward) == 1
    merged = forward[0]
    assert merged["source_resolver"] == "structured_graph"
    assert merged["source_contributions"] == ("structured_graph", "support_semantic")
    assert merged["features"]["values"] == {
        "semantic_similarity": 0.81,
        "source_agreement": 1.0,
        "structured_match": 1.0,
        "supplied_trust": 0.84,
    }
    assert merged["features"]["unavailable"] == ()
    assert merged["selection_reasons"] == (
        "canonical_complete",
        "semantic_similarity",
        "source_agreement",
        "structured_match",
    )
    assert canonicalize_proposition_evidence(forward) == forward
    assert structured["source_contributions"] == ("structured_graph",)
    assert semantic["source_contributions"] == ("support_semantic",)


def test_proposition_evidence_normalization_orders_distinct_propositions_by_stable_id() -> None:
    first = proposition_evidence_record_with_changes(
        internal_record(), {"proposition_id": "proposition:a", "path": ("proposition:a",)}
    )
    second = proposition_evidence_record_with_changes(
        internal_record(), {"proposition_id": "proposition:b", "path": ("proposition:b",)}
    )

    normalized = canonicalize_proposition_evidence((second, first))

    assert tuple(record["proposition_id"] for record in normalized) == ("proposition:a", "proposition:b")


@pytest_mark.parametrize(
    ("conflicting", "message"),
    [
        (
            change_record(
                semantic_record(),
                canonical_references=canonical_proposition_references(
                    "entity:alan-turing",
                    "predicate:birth-date",
                    "entity:different",
                ),
            ),
            "conflicting canonical references",
        ),
        (
            change_record(
                semantic_record(),
                features=feature_set(values={"semantic_similarity": 0.81, "structured_match": 0.5, "supplied_trust": 0.84}),
            ),
            "conflicting measured feature structured_match",
        ),
        (
            change_record(
                semantic_record(),
                disclosure=disclosure_decision_with_changes(
                    semantic_record()["disclosure"],
                    {"scope": scope_key(namespace="different")},
                ),
            ),
            "conflicting current evidence state",
        ),
    ],
)
def test_proposition_evidence_normalization_rejects_conflicts_in_either_order(conflicting, message) -> None:
    with pytest_raises(InvalidRequestError, match=message):
        canonicalize_proposition_evidence((internal_record(), conflicting))
    with pytest_raises(InvalidRequestError, match=message):
        canonicalize_proposition_evidence((conflicting, internal_record()))


def test_proposition_evidence_normalization_enforces_input_source_and_cooperative_bounds() -> None:
    calls = 0

    def check() -> None:
        nonlocal calls
        calls += 1

    records = tuple(
        change_record(
            internal_record(),
            source_resolver=f"source_{index}",
            source_contributions=(f"source_{index}",),
        )
        for index in range(9)
    )

    with pytest_raises(InvalidRequestError, match="sources exceed the limit of 8"):
        canonicalize_proposition_evidence(records, check)
    with pytest_raises(InvalidRequestError, match="at most 1000"):
        canonicalize_proposition_evidence((internal_record(),) * 1_001)
    assert calls == 10


def test_evidence_usefulness_policy_codec_and_frozen_hand_authored_floors() -> None:
    policy = evidence_usefulness_policy()
    serialized = evidence_usefulness_policy_to_dict(policy)
    encoded = evidence_usefulness_policy_to_json(policy)

    assert type(policy) is dict
    assert evidence_usefulness_policy_from_dict(serialized) == policy
    assert evidence_usefulness_policy_from_json(encoded) == policy
    assert serialized == {
        "policy_version": "proposition-evidence-usefulness-v1",
        "canonical_completeness_floor": 1.0,
        "structured_match_floor": 1.0,
        "semantic_similarity_floor": 0.6,
        "source_agreement_floor": 1.0,
        "supplied_trust_floor": 0.0,
        "supplied_trust_floor_available": False,
    }
    with pytest_raises(InvalidRequestError, match="frozen at 0.6"):
        evidence_usefulness_policy_with_changes(policy, {"semantic_similarity_floor": 0.61})
    with pytest_raises(InvalidRequestError, match="unsupported evidence usefulness"):
        evidence_usefulness_policy_with_changes(policy, {"policy_version": "fitted-v2"})
    with pytest_raises(InvalidRequestError, match="invalid fields"):
        evidence_usefulness_policy_from_dict({**serialized, "coefficient": 0.5})


@pytest_mark.parametrize(
    ("field", "malformed", "message"),
    (
        ("policy_version", 1, "policy_version must be a string"),
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
    structured = evaluate_evidence_usefulness(policy, policy_record())
    semantic = evaluate_evidence_usefulness(
        policy,
        policy_record(
            features=feature_set(
                values={"canonical_completeness": 1.0, "semantic_similarity": 0.60, "supplied_trust": 0.84},
                unavailable=("source_agreement", "structured_match"),
            )
        ),
    )

    assert structured["included"] is True
    assert EvidenceUsefulnessReason.STRUCTURED_MATCH_QUALIFIED in structured["reasons"]
    assert EvidenceUsefulnessReason.SUPPLIED_TRUST_AVAILABLE in structured["reasons"]
    assert semantic["included"] is True
    assert EvidenceUsefulnessReason.SEMANTIC_SIMILARITY_QUALIFIED in semantic["reasons"]
    assert tuple(reason.value for reason in semantic["reasons"]) == tuple(sorted(reason.value for reason in semantic["reasons"]))


def test_evidence_usefulness_distinguishes_unavailable_from_measured_zero() -> None:
    policy = evidence_usefulness_policy()
    signal_unavailable = evaluate_evidence_usefulness(
        policy,
        policy_record(
            features=feature_set(
                values={"canonical_completeness": 1.0, "supplied_trust": 0.84},
                unavailable=("semantic_similarity", "source_agreement", "structured_match"),
            )
        ),
    )
    signal_zero = evaluate_evidence_usefulness(
        policy,
        policy_record(
            features=feature_set(
                values={"canonical_completeness": 1.0, "semantic_similarity": 0.0, "supplied_trust": 0.84},
                unavailable=("source_agreement", "structured_match"),
            )
        ),
    )
    canonical_unavailable = evaluate_evidence_usefulness(
        policy,
        policy_record(
            features=feature_set(
                values={"structured_match": 1.0, "supplied_trust": 0.84},
                unavailable=("canonical_completeness",),
            )
        ),
    )
    canonical_zero = evaluate_evidence_usefulness(
        policy,
        policy_record(
            features=feature_set(values={"canonical_completeness": 0.0, "structured_match": 1.0, "supplied_trust": 0.84})
        ),
    )

    assert EvidenceUsefulnessReason.RETRIEVAL_SIGNAL_UNAVAILABLE in signal_unavailable["reasons"]
    assert EvidenceUsefulnessReason.RETRIEVAL_SIGNAL_BELOW_FLOOR in signal_zero["reasons"]
    assert EvidenceUsefulnessReason.CANONICAL_COMPLETENESS_UNAVAILABLE in canonical_unavailable["reasons"]
    assert EvidenceUsefulnessReason.CANONICAL_COMPLETENESS_BELOW_FLOOR in canonical_zero["reasons"]
    assert not signal_unavailable["included"]
    assert not signal_zero["included"]
    assert not canonical_unavailable["included"]
    assert not canonical_zero["included"]


def test_evidence_usefulness_configured_trust_floor_distinguishes_absence_zero_and_boundary() -> None:
    policy = evidence_usefulness_policy(supplied_trust_floor=0.5, supplied_trust_floor_available=True)
    unavailable_record = change_record(
        policy_record(),
        trust=proposition_trust_inputs(),
        features=feature_set(
            values={"canonical_completeness": 1.0, "structured_match": 1.0},
            unavailable=("semantic_similarity", "source_agreement", "supplied_trust"),
        ),
    )
    measured_zero_record = change_record(
        policy_record(),
        trust=proposition_trust_inputs(
            supplied_trust=0.0,
            supplied_trust_available=True,
            supplied_trust_version=1,
            supplied_trust_version_available=True,
        ),
        features=feature_set(
            values={"canonical_completeness": 1.0, "structured_match": 1.0, "supplied_trust": 0.0},
            unavailable=("semantic_similarity", "source_agreement"),
        ),
    )
    boundary_record = change_record(
        policy_record(),
        trust=proposition_trust_inputs(
            supplied_trust=0.5,
            supplied_trust_available=True,
            supplied_trust_version=1,
            supplied_trust_version_available=True,
        ),
        features=feature_set(
            values={"canonical_completeness": 1.0, "structured_match": 1.0, "supplied_trust": 0.5},
            unavailable=("semantic_similarity", "source_agreement"),
        ),
    )

    unavailable = evaluate_evidence_usefulness(policy, unavailable_record)
    measured_zero = evaluate_evidence_usefulness(policy, measured_zero_record)
    boundary = evaluate_evidence_usefulness(policy, boundary_record)

    assert EvidenceUsefulnessReason.SUPPLIED_TRUST_REQUIRED_UNAVAILABLE in unavailable["reasons"]
    assert EvidenceUsefulnessReason.SUPPLIED_TRUST_BELOW_FLOOR in measured_zero["reasons"]
    assert EvidenceUsefulnessReason.SUPPLIED_TRUST_FLOOR_SATISFIED in boundary["reasons"]
    assert unavailable["included"] is False
    assert measured_zero["included"] is False
    assert boundary["included"] is True


def test_evidence_usefulness_default_policy_does_not_rank_or_require_supplied_trust() -> None:
    unavailable_record = change_record(
        policy_record(),
        trust=proposition_trust_inputs(),
        features=feature_set(
            values={"canonical_completeness": 1.0, "structured_match": 1.0},
            unavailable=("semantic_similarity", "source_agreement", "supplied_trust"),
        ),
    )
    measured_zero_record = change_record(
        policy_record(),
        trust=proposition_trust_inputs(
            supplied_trust=0.0,
            supplied_trust_available=True,
            supplied_trust_version=1,
            supplied_trust_version_available=True,
        ),
        features=feature_set(
            values={"canonical_completeness": 1.0, "structured_match": 1.0, "supplied_trust": 0.0},
            unavailable=("semantic_similarity", "source_agreement"),
        ),
    )

    policy = evidence_usefulness_policy()
    unavailable = evaluate_evidence_usefulness(policy, unavailable_record)
    measured_zero = evaluate_evidence_usefulness(policy, measured_zero_record)

    assert unavailable["included"] is True
    assert measured_zero["included"] is True
    assert EvidenceUsefulnessReason.SUPPLIED_TRUST_UNAVAILABLE in unavailable["reasons"]
    assert EvidenceUsefulnessReason.SUPPLIED_TRUST_AVAILABLE in measured_zero["reasons"]


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
    record = policy_record(
        features=feature_set(
            values={
                "canonical_completeness": 1.0,
                "semantic_similarity": 0.59,
                "source_agreement": 1.0,
                "structured_match": 0.0,
                "supplied_trust": 0.84,
            },
        )
    )

    decision = evaluate_evidence_usefulness(evidence_usefulness_policy(), record)

    assert decision["included"] is False
    assert EvidenceUsefulnessReason.SOURCE_AGREEMENT_QUALIFIED in decision["reasons"]
    assert EvidenceUsefulnessReason.RETRIEVAL_SIGNAL_BELOW_FLOOR in decision["reasons"]


def test_proposition_evidence_distinguishes_unavailable_trust_from_measured_zero() -> None:
    unavailable = change_record(internal_record(), trust=proposition_trust_inputs())
    measured_zero = change_record(
        internal_record(),
        trust=proposition_trust_inputs(
            supplied_trust=0.0,
            supplied_trust_available=True,
            supplied_trust_version=1,
            supplied_trust_version_available=True,
        ),
    )

    assert unavailable["trust"]["supplied_trust"] == measured_zero["trust"]["supplied_trust"] == 0.0
    assert unavailable["trust"]["supplied_trust_available"] is False
    assert measured_zero["trust"]["supplied_trust_available"] is True
    assert proposition_evidence_record_from_json(proposition_evidence_record_to_json(unavailable)) == unavailable
    assert proposition_evidence_record_from_json(proposition_evidence_record_to_json(measured_zero)) == measured_zero


@pytest_mark.parametrize(
    ("factory", "internal_message"),
    [
        (lambda: change_record(internal_record(), proposition_id="x" * 257), "256 UTF-8 bytes"),
        (lambda: change_record(internal_record(), proposition_id="proposition id"), "whitespace"),
        (lambda: change_record(internal_record(), selection_reasons=("reason with spaces",)), "whitespace"),
        (lambda: change_record(internal_record(), path=("proposition:other",)), "singleton proposition_id"),
        (lambda: change_record(internal_record(), path=()), "singleton proposition_id"),
        (lambda: change_record(internal_record(), selection_reasons=()), "1 through 16"),
        (lambda: change_record(internal_record(), selection_reasons=("z", "a")), "unique and sorted"),
        (lambda: change_record(internal_record(), selection_reasons=tuple(f"r{i:02d}" for i in range(17))), "1 through 16"),
        (lambda: change_record(internal_record(), source_contributions=()), "1 through 8"),
        (lambda: change_record(internal_record(), source_contributions=("semantic_proposition",)), "must be present"),
        (
            lambda: change_record(
                internal_record(),
                validity=proposition_validity_inputs_with_changes(internal_record()["validity"], {"active": False}),
            ),
            "active conflicts with the disclosed invalidation boundary",
        ),
        (
            lambda: change_record(
                internal_record(),
                validity=proposition_validity_inputs_with_changes(internal_record()["validity"], {"system_current": False}),
            ),
            "system_current conflicts with the disclosed system interval",
        ),
        (
            lambda: change_record(
                internal_record(),
                validity=proposition_validity_inputs_with_changes(internal_record()["validity"], {"valid_time_current": False}),
            ),
            "conflicts with the disclosed",
        ),
        (
            lambda: change_record(
                internal_record(),
                trust=proposition_trust_inputs(supplied_trust=0.5, supplied_trust_available=False),
            ),
            "must be zero when unavailable",
        ),
        (
            lambda: change_record(
                internal_record(),
                disclosure=disclosure_decision(
                    ownership=PropositionOwnership.COMPANY,
                    basis=DisclosureBasis.PUBLIC_RULE,
                    scope=scope_key(namespace="support"),
                    policy_version="proposition-disclosure-v1",
                ),
            ),
            "trusted scope authority",
        ),
    ],
)
def test_proposition_evidence_rejects_malformed_or_ambiguous_values(factory, internal_message) -> None:
    with pytest_raises(InvalidRequestError, match=internal_message):
        factory()


def test_company_disclosure_requires_exact_scope_authority_provenance() -> None:
    disclosure = disclosure_decision(
        ownership=PropositionOwnership.COMPANY,
        basis=DisclosureBasis.TRUSTED_SCOPE_AUTHORITY,
        scope=scope_key(namespace="support", context_fingerprint="tenant:acme"),
        policy_version="proposition-disclosure-v1",
        authority="tapestry-visibility-v3",
        authority_available=True,
    )
    record = change_record(internal_record(), disclosure=disclosure)

    assert proposition_evidence_record_from_json(proposition_evidence_record_to_json(record)) == record
    assert record["disclosure"]["scope"] == scope_key(namespace="support", context_fingerprint="tenant:acme")


@pytest_mark.parametrize("field", ["raw_proposition", "passage", "proof", "credential", "cypher", "embedding", "properties"])
def test_proposition_evidence_decoder_rejects_excluded_payload_fields(field: str) -> None:
    payload = proposition_evidence_record_to_dict(internal_record())
    payload[field] = "secret"

    with pytest_raises(InvalidRequestError, match="invalid fields"):
        proposition_evidence_record_from_dict(payload)


def test_proposition_evidence_decoder_rejects_nested_unknown_fields_and_versions() -> None:
    unknown = proposition_evidence_record_to_dict(internal_record())
    validity = unknown["validity"]
    assert isinstance(validity, dict)
    validity["unexpected"] = "secret"
    with pytest_raises(InvalidRequestError, match="invalid fields"):
        proposition_evidence_record_from_dict(unknown)

    unsupported = proposition_evidence_record_to_dict(internal_record())
    references = unsupported["canonical_references"]
    assert isinstance(references, dict)
    references["schema_version"] = 2
    with pytest_raises(InvalidRequestError, match="schema_version"):
        proposition_evidence_record_from_dict(unsupported)


def test_evidence_package_codec_canonicalizes_and_deduplicates() -> None:
    first = internal_record()
    second = change_record(
        first,
        proposition_id="proposition:01J5M6Q9J9",
        path=("proposition:01J5M6Q9J9",),
    )

    package = build_evidence_package((second, first, first))

    assert package.__class__ is dict
    assert tuple(record["proposition_id"] for record in package["records"]) == (first["proposition_id"], second["proposition_id"])
    assert package["retained_count"] == 2
    assert package["omitted_count"] == 1
    assert package["truncated"] is True
    assert package["truncation_reasons"] == (EvidencePackageTruncationReason.DUPLICATE_PROPOSITION_ID,)
    serialized = evidence_package_to_dict(package)
    encoded = evidence_package_to_json(package)
    assert evidence_package_from_dict(serialized) == package
    assert evidence_package_from_json(encoded) == package
    assert "null" not in encoded
    assert evidence_package_to_json(empty_evidence_package()) == evidence_package_to_json(build_evidence_package(()))
    copied = validate_evidence_package(package)
    assert copied == package
    assert copied is not package
    assert copied["records"][0] is not package["records"][0]


def test_evidence_package_rejects_conflicting_same_id_projections() -> None:
    conflict = change_record(internal_record(), features=feature_set(values={"structured_match": 0.5}))

    with pytest_raises(InvalidRequestError, match="conflicting Proposition evidence projections"):
        build_evidence_package((internal_record(), conflict))


def test_evidence_package_applies_count_limit_after_canonical_ordering() -> None:
    records = tuple(
        change_record(internal_record(), proposition_id=f"proposition:{index:02d}", path=(f"proposition:{index:02d}",))
        for index in reversed(range(12))
    )

    package = build_evidence_package(records)

    assert package["retained_count"] == 10
    assert package["omitted_count"] == 2
    assert tuple(record["proposition_id"] for record in package["records"]) == tuple(
        f"proposition:{index:02d}" for index in range(10)
    )
    assert package["truncation_reasons"] == (EvidencePackageTruncationReason.RECORD_LIMIT,)
    assert len(evidence_package_to_json(package).encode("utf-8")) <= 65_536


def test_evidence_package_applies_complete_serialized_byte_limit() -> None:
    records = tuple(
        change_record(
            internal_record(),
            proposition_id=f"proposition:{index:02d}",
            path=(f"proposition:{index:02d}",),
            selection_reasons=tuple(f"reason_{reason:02d}_{'x' * 70}" for reason in range(16)),
        )
        for index in range(5)
    )

    package = build_evidence_package(records, max_bytes=5_000)

    assert 0 < package["retained_count"] < len(records)
    assert package["omitted_count"] == len(records) - package["retained_count"]
    assert package["truncation_reasons"] == (EvidencePackageTruncationReason.SERIALIZED_SIZE_LIMIT,)
    assert len(evidence_package_to_json(package).encode("utf-8")) <= 5_000


def test_evidence_package_hard_byte_limit_truncates_before_construction() -> None:
    large_features = feature_set(values={f"feature_{index:02d}_{'x' * 75}": 1.0 for index in range(64)})
    records = tuple(
        change_record(
            internal_record(),
            proposition_id=f"proposition:{index:02d}",
            path=(f"proposition:{index:02d}",),
            features=large_features,
            selection_reasons=tuple(f"reason_{reason:02d}_{'x' * 70}" for reason in range(16)),
        )
        for index in range(10)
    )

    package = build_evidence_package(records)

    assert package["retained_count"] < len(records)
    assert package["omitted_count"] == len(records) - package["retained_count"]
    assert package["truncation_reasons"] == (EvidencePackageTruncationReason.SERIALIZED_SIZE_LIMIT,)
    assert len(evidence_package_to_json(package).encode("utf-8")) <= 65_536
    with pytest_raises(InvalidRequestError, match="65536 UTF-8 bytes"):
        evidence_package(records, 10, 0, False, ())


@pytest_mark.parametrize(
    ("changes", "message"),
    [
        ({"wire_version": 3}, "wire_version"),
        ({"records": (internal_record(),) * 11, "retained_count": 11}, "limit of 10"),
        ({"records": (internal_record(),), "retained_count": 0}, "must equal"),
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
        evidence_package_with_changes(empty_evidence_package(), changes)


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
    with pytest_raises(InvalidRequestError, match="max_records"):
        build_evidence_package((internal_record(),), max_records=11)
    with pytest_raises(InvalidRequestError, match="max_bytes"):
        build_evidence_package((internal_record(),), max_bytes=255)
    with pytest_raises(InvalidRequestError, match="input exceeds the limit of 1000"):
        build_evidence_package((internal_record(),) * 1_001)


def test_evidence_package_json_decoder_rejects_oversized_input_before_parsing() -> None:
    with pytest_raises(InvalidRequestError, match="65536 UTF-8 bytes"):
        evidence_package_from_json("{" + " " * 65_536 + "}")
