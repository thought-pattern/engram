"""Contract tests for Section 7 full-Claim evidence and packaging."""

import json
from dataclasses import replace

import pytest

from engram.errors import InvalidRequestError
from engram.evidence import (
    EvidenceUsefulnessDecision,
    EvidenceUsefulnessPolicy,
    EvidenceUsefulnessReason,
    canonicalize_claim_evidence,
)
from engram.identity import ScopeKey
from engram.resolution import (
    EMPTY_EVIDENCE_PACKAGE,
    CanonicalClaimReferences,
    ClaimEvidenceRecord,
    ClaimOwnership,
    ClaimTrustInputs,
    ClaimValidityInputs,
    DisclosureBasis,
    DisclosureDecision,
    EvidencePackage,
    EvidencePackageTruncationReason,
    FeatureSet,
)


def _record() -> ClaimEvidenceRecord:
    return ClaimEvidenceRecord(
        claim_id="claim:01J5M6Q9J8",
        source_resolver="structured_graph",
        source_contributions=("structured_graph",),
        features=FeatureSet(
            values={"structured_match": 1.0, "supplied_trust": 0.84},
            unavailable=("semantic_similarity",),
        ),
        canonical_references=CanonicalClaimReferences(
            subject_entity_id="entity:alan-turing",
            predicate_id="predicate:birth-date",
            object_entity_id="entity:1912-06-23",
        ),
        validity=ClaimValidityInputs(
            evaluation_time="2026-08-16T12:00:00Z",
            active=True,
            system_current=True,
            valid_time_current=True,
            valid_from="1912-06-23T00:00:00Z",
            valid_from_available=True,
        ),
        trust=ClaimTrustInputs(
            trust_category="verified_public",
            trust_category_available=True,
            supplied_trust=0.84,
            supplied_trust_available=True,
            supplied_trust_version=3,
            supplied_trust_version_available=True,
        ),
        disclosure=DisclosureDecision(
            ownership=ClaimOwnership.PUBLIC,
            basis=DisclosureBasis.PUBLIC_RULE,
            scope=ScopeKey(namespace="support", context_fingerprint="account:one"),
            policy_version="claim-disclosure-v1",
        ),
        path=("claim:01J5M6Q9J8",),
        selection_reasons=("canonical_complete", "structured_match"),
    )


def _semantic_record() -> ClaimEvidenceRecord:
    return replace(
        _record(),
        source_resolver="support_semantic",
        source_contributions=("support_semantic",),
        features=FeatureSet(
            values={"semantic_similarity": 0.81, "supplied_trust": 0.84},
            unavailable=("structured_match",),
        ),
        selection_reasons=("canonical_complete", "semantic_similarity"),
    )


def _policy_record(*, features=()) -> ClaimEvidenceRecord:
    selected_features = (
        features
        if isinstance(features, FeatureSet)
        else FeatureSet(
            values={"canonical_completeness": 1.0, "structured_match": 1.0, "supplied_trust": 0.84},
            unavailable=("semantic_similarity", "source_agreement"),
        )
    )
    return replace(_record(), features=selected_features)


def test_claim_evidence_record_codec_is_deterministic_and_concrete() -> None:
    record = _record()
    encoded = record.to_json()

    assert ClaimEvidenceRecord.from_dict(record.to_dict()) == record
    assert ClaimEvidenceRecord.from_json(encoded) == record
    assert encoded == json.dumps(record.to_dict(), sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    assert "null" not in encoded
    assert set(record.to_dict()) == {
        "schema_version",
        "claim_id",
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


def test_claim_evidence_normalization_merges_sources_features_and_reasons_deterministically() -> None:
    structured = _record()
    semantic = _semantic_record()

    forward = canonicalize_claim_evidence((structured, semantic, semantic))
    reverse = canonicalize_claim_evidence((semantic, structured))

    assert forward == reverse
    assert len(forward) == 1
    merged = forward[0]
    assert merged.source_resolver == "structured_graph"
    assert merged.source_contributions == ("structured_graph", "support_semantic")
    assert merged.features.values == {
        "semantic_similarity": 0.81,
        "source_agreement": 1.0,
        "structured_match": 1.0,
        "supplied_trust": 0.84,
    }
    assert merged.features.unavailable == ()
    assert merged.selection_reasons == (
        "canonical_complete",
        "semantic_similarity",
        "source_agreement",
        "structured_match",
    )
    assert canonicalize_claim_evidence(forward) == forward
    assert structured.source_contributions == ("structured_graph",)
    assert semantic.source_contributions == ("support_semantic",)


def test_claim_evidence_normalization_orders_distinct_claims_by_stable_id() -> None:
    first = replace(_record(), claim_id="claim:a", path=("claim:a",))
    second = replace(_record(), claim_id="claim:b", path=("claim:b",))

    normalized = canonicalize_claim_evidence((second, first))

    assert tuple(record.claim_id for record in normalized) == ("claim:a", "claim:b")


@pytest.mark.parametrize(
    ("conflicting", "message"),
    [
        (
            replace(
                _semantic_record(),
                canonical_references=CanonicalClaimReferences(
                    "entity:alan-turing",
                    "predicate:birth-date",
                    "entity:different",
                ),
            ),
            "conflicting canonical references",
        ),
        (
            replace(
                _semantic_record(),
                features=FeatureSet(values={"semantic_similarity": 0.81, "structured_match": 0.5, "supplied_trust": 0.84}),
            ),
            "conflicting measured feature structured_match",
        ),
        (
            replace(
                _semantic_record(),
                disclosure=replace(
                    _semantic_record().disclosure,
                    scope=ScopeKey(namespace="different"),
                ),
            ),
            "conflicting current evidence state",
        ),
    ],
)
def test_claim_evidence_normalization_rejects_conflicts_in_either_order(conflicting, message) -> None:
    with pytest.raises(InvalidRequestError, match=message):
        canonicalize_claim_evidence((_record(), conflicting))
    with pytest.raises(InvalidRequestError, match=message):
        canonicalize_claim_evidence((conflicting, _record()))


def test_claim_evidence_normalization_enforces_input_source_and_cooperative_bounds() -> None:
    calls = 0

    def check() -> None:
        nonlocal calls
        calls += 1

    records = tuple(
        replace(
            _record(),
            source_resolver=f"source_{index}",
            source_contributions=(f"source_{index}",),
        )
        for index in range(9)
    )

    with pytest.raises(InvalidRequestError, match="sources exceed the limit of 8"):
        canonicalize_claim_evidence(records, check)
    with pytest.raises(InvalidRequestError, match="at most 1000"):
        canonicalize_claim_evidence((_record(),) * 1_001)
    assert calls == 10


def test_evidence_usefulness_policy_codec_and_frozen_hand_authored_floors() -> None:
    policy = EvidenceUsefulnessPolicy()

    assert EvidenceUsefulnessPolicy.from_dict(policy.to_dict()) == policy
    assert EvidenceUsefulnessPolicy.from_json(policy.to_json()) == policy
    assert policy.to_dict() == {
        "policy_version": "claim-evidence-usefulness-v1",
        "canonical_completeness_floor": 1.0,
        "structured_match_floor": 1.0,
        "semantic_similarity_floor": 0.6,
        "source_agreement_floor": 1.0,
        "supplied_trust_floor": 0.0,
        "supplied_trust_floor_available": False,
    }
    with pytest.raises(InvalidRequestError, match="frozen at 0.6"):
        replace(policy, semantic_similarity_floor=0.61)
    with pytest.raises(InvalidRequestError, match="unsupported evidence usefulness"):
        replace(policy, policy_version="fitted-v2")
    with pytest.raises(InvalidRequestError, match="invalid fields"):
        EvidenceUsefulnessPolicy.from_dict({**policy.to_dict(), "coefficient": 0.5})


@pytest.mark.parametrize(
    ("field", "malformed", "message"),
    (
        ("policy_version", 1, "policy_version must be a string"),
        ("semantic_similarity_floor", 1, "semantic_similarity_floor must be a float"),
        ("supplied_trust_floor_available", 0, "supplied_trust_floor_available must be a boolean"),
    ),
)
def test_evidence_usefulness_policy_decoder_rejects_wrong_wire_types(field, malformed, message) -> None:
    payload = EvidenceUsefulnessPolicy().to_dict()
    payload[field] = malformed

    with pytest.raises(InvalidRequestError, match=message):
        EvidenceUsefulnessPolicy.from_dict(payload)


def test_evidence_usefulness_accepts_exact_structured_and_semantic_floor_boundaries() -> None:
    policy = EvidenceUsefulnessPolicy()
    structured = policy.evaluate(_policy_record())
    semantic = policy.evaluate(
        _policy_record(
            features=FeatureSet(
                values={"canonical_completeness": 1.0, "semantic_similarity": 0.60, "supplied_trust": 0.84},
                unavailable=("source_agreement", "structured_match"),
            )
        )
    )

    assert structured.included is True
    assert EvidenceUsefulnessReason.STRUCTURED_MATCH_QUALIFIED in structured.reasons
    assert EvidenceUsefulnessReason.SUPPLIED_TRUST_AVAILABLE in structured.reasons
    assert semantic.included is True
    assert EvidenceUsefulnessReason.SEMANTIC_SIMILARITY_QUALIFIED in semantic.reasons
    assert tuple(reason.value for reason in semantic.reasons) == tuple(sorted(reason.value for reason in semantic.reasons))


def test_evidence_usefulness_distinguishes_unavailable_from_measured_zero() -> None:
    policy = EvidenceUsefulnessPolicy()
    signal_unavailable = policy.evaluate(
        _policy_record(
            features=FeatureSet(
                values={"canonical_completeness": 1.0, "supplied_trust": 0.84},
                unavailable=("semantic_similarity", "source_agreement", "structured_match"),
            )
        )
    )
    signal_zero = policy.evaluate(
        _policy_record(
            features=FeatureSet(
                values={"canonical_completeness": 1.0, "semantic_similarity": 0.0, "supplied_trust": 0.84},
                unavailable=("source_agreement", "structured_match"),
            )
        )
    )
    canonical_unavailable = policy.evaluate(
        _policy_record(
            features=FeatureSet(
                values={"structured_match": 1.0, "supplied_trust": 0.84},
                unavailable=("canonical_completeness",),
            )
        )
    )
    canonical_zero = policy.evaluate(
        _policy_record(features=FeatureSet(values={"canonical_completeness": 0.0, "structured_match": 1.0, "supplied_trust": 0.84}))
    )

    assert EvidenceUsefulnessReason.RETRIEVAL_SIGNAL_UNAVAILABLE in signal_unavailable.reasons
    assert EvidenceUsefulnessReason.RETRIEVAL_SIGNAL_BELOW_FLOOR in signal_zero.reasons
    assert EvidenceUsefulnessReason.CANONICAL_COMPLETENESS_UNAVAILABLE in canonical_unavailable.reasons
    assert EvidenceUsefulnessReason.CANONICAL_COMPLETENESS_BELOW_FLOOR in canonical_zero.reasons
    assert not signal_unavailable.included
    assert not signal_zero.included
    assert not canonical_unavailable.included
    assert not canonical_zero.included


def test_evidence_usefulness_configured_trust_floor_distinguishes_absence_zero_and_boundary() -> None:
    policy = EvidenceUsefulnessPolicy(supplied_trust_floor=0.5, supplied_trust_floor_available=True)
    unavailable_record = replace(
        _policy_record(),
        trust=ClaimTrustInputs(),
        features=FeatureSet(
            values={"canonical_completeness": 1.0, "structured_match": 1.0},
            unavailable=("semantic_similarity", "source_agreement", "supplied_trust"),
        ),
    )
    measured_zero_record = replace(
        _policy_record(),
        trust=ClaimTrustInputs(
            supplied_trust=0.0,
            supplied_trust_available=True,
            supplied_trust_version=1,
            supplied_trust_version_available=True,
        ),
        features=FeatureSet(
            values={"canonical_completeness": 1.0, "structured_match": 1.0, "supplied_trust": 0.0},
            unavailable=("semantic_similarity", "source_agreement"),
        ),
    )
    boundary_record = replace(
        _policy_record(),
        trust=ClaimTrustInputs(
            supplied_trust=0.5,
            supplied_trust_available=True,
            supplied_trust_version=1,
            supplied_trust_version_available=True,
        ),
        features=FeatureSet(
            values={"canonical_completeness": 1.0, "structured_match": 1.0, "supplied_trust": 0.5},
            unavailable=("semantic_similarity", "source_agreement"),
        ),
    )

    unavailable = policy.evaluate(unavailable_record)
    measured_zero = policy.evaluate(measured_zero_record)
    boundary = policy.evaluate(boundary_record)

    assert EvidenceUsefulnessReason.SUPPLIED_TRUST_REQUIRED_UNAVAILABLE in unavailable.reasons
    assert EvidenceUsefulnessReason.SUPPLIED_TRUST_BELOW_FLOOR in measured_zero.reasons
    assert EvidenceUsefulnessReason.SUPPLIED_TRUST_FLOOR_SATISFIED in boundary.reasons
    assert unavailable.included is False
    assert measured_zero.included is False
    assert boundary.included is True


def test_evidence_usefulness_default_policy_does_not_rank_or_require_supplied_trust() -> None:
    unavailable_record = replace(
        _policy_record(),
        trust=ClaimTrustInputs(),
        features=FeatureSet(
            values={"canonical_completeness": 1.0, "structured_match": 1.0},
            unavailable=("semantic_similarity", "source_agreement", "supplied_trust"),
        ),
    )
    measured_zero_record = replace(
        _policy_record(),
        trust=ClaimTrustInputs(
            supplied_trust=0.0,
            supplied_trust_available=True,
            supplied_trust_version=1,
            supplied_trust_version_available=True,
        ),
        features=FeatureSet(
            values={"canonical_completeness": 1.0, "structured_match": 1.0, "supplied_trust": 0.0},
            unavailable=("semantic_similarity", "source_agreement"),
        ),
    )

    unavailable = EvidenceUsefulnessPolicy().evaluate(unavailable_record)
    measured_zero = EvidenceUsefulnessPolicy().evaluate(measured_zero_record)

    assert unavailable.included is True
    assert measured_zero.included is True
    assert EvidenceUsefulnessReason.SUPPLIED_TRUST_UNAVAILABLE in unavailable.reasons
    assert EvidenceUsefulnessReason.SUPPLIED_TRUST_AVAILABLE in measured_zero.reasons


def test_evidence_usefulness_decision_rejects_inconsistent_external_reason_sets() -> None:
    with pytest.raises(InvalidRequestError, match="inconsistent reasons"):
        EvidenceUsefulnessDecision(
            claim_id="claim:one",
            included=True,
            reasons=(EvidenceUsefulnessReason.RETRIEVAL_SIGNAL_BELOW_FLOOR,),
        )
    with pytest.raises(InvalidRequestError, match="requires an exclusion reason"):
        EvidenceUsefulnessDecision(
            claim_id="claim:one",
            included=False,
            reasons=(EvidenceUsefulnessReason.STRUCTURED_MATCH_QUALIFIED,),
        )


def test_evidence_usefulness_source_agreement_cannot_rescue_subfloor_retrieval() -> None:
    record = _policy_record(
        features=FeatureSet(
            values={
                "canonical_completeness": 1.0,
                "semantic_similarity": 0.59,
                "source_agreement": 1.0,
                "structured_match": 0.0,
                "supplied_trust": 0.84,
            },
        )
    )

    decision = EvidenceUsefulnessPolicy().evaluate(record)

    assert decision.included is False
    assert EvidenceUsefulnessReason.SOURCE_AGREEMENT_QUALIFIED in decision.reasons
    assert EvidenceUsefulnessReason.RETRIEVAL_SIGNAL_BELOW_FLOOR in decision.reasons


def test_claim_evidence_distinguishes_unavailable_trust_from_measured_zero() -> None:
    unavailable = replace(_record(), trust=ClaimTrustInputs())
    measured_zero = replace(
        _record(),
        trust=ClaimTrustInputs(
            supplied_trust=0.0,
            supplied_trust_available=True,
            supplied_trust_version=1,
            supplied_trust_version_available=True,
        ),
    )

    assert unavailable.trust.supplied_trust == measured_zero.trust.supplied_trust == 0.0
    assert unavailable.trust.supplied_trust_available is False
    assert measured_zero.trust.supplied_trust_available is True
    assert ClaimEvidenceRecord.from_json(unavailable.to_json()) == unavailable
    assert ClaimEvidenceRecord.from_json(measured_zero.to_json()) == measured_zero


@pytest.mark.parametrize(
    ("factory", "message"),
    [
        (lambda: replace(_record(), schema_version=2), "unsupported Claim evidence record"),
        (lambda: replace(_record(), claim_id="x" * 257), "256 UTF-8 bytes"),
        (lambda: replace(_record(), claim_id="claim id"), "whitespace"),
        (lambda: replace(_record(), selection_reasons=("reason with spaces",)), "whitespace"),
        (lambda: replace(_record(), path=("claim:other",)), "singleton claim_id"),
        (lambda: replace(_record(), path=()), "singleton claim_id"),
        (lambda: replace(_record(), selection_reasons=()), "1 through 16"),
        (lambda: replace(_record(), selection_reasons=("z", "a")), "unique and sorted"),
        (lambda: replace(_record(), selection_reasons=tuple(f"r{i:02d}" for i in range(17))), "1 through 16"),
        (lambda: replace(_record(), source_contributions=()), "1 through 8"),
        (lambda: replace(_record(), source_contributions=("semantic_claim",)), "must be present"),
        (
            lambda: replace(_record(), validity=replace(_record().validity, active=False)),
            "currently eligible",
        ),
        (
            lambda: replace(_record(), validity=replace(_record().validity, system_current=False)),
            "currently eligible",
        ),
        (
            lambda: replace(
                _record(),
                validity=replace(_record().validity, valid_time_current=False),
            ),
            "conflicts with the disclosed",
        ),
        (
            lambda: replace(
                _record(),
                trust=ClaimTrustInputs(supplied_trust=0.5, supplied_trust_available=False),
            ),
            "must be zero when unavailable",
        ),
        (
            lambda: replace(
                _record(),
                disclosure=DisclosureDecision(
                    ownership=ClaimOwnership.COMPANY,
                    basis=DisclosureBasis.PUBLIC_RULE,
                    scope=ScopeKey(namespace="support"),
                    policy_version="claim-disclosure-v1",
                ),
            ),
            "trusted scope authority",
        ),
    ],
)
def test_claim_evidence_rejects_malformed_or_ambiguous_values(factory, message) -> None:
    with pytest.raises(InvalidRequestError, match=message):
        factory()


def test_company_disclosure_requires_exact_scope_authority_provenance() -> None:
    disclosure = DisclosureDecision(
        ownership=ClaimOwnership.COMPANY,
        basis=DisclosureBasis.TRUSTED_SCOPE_AUTHORITY,
        scope=ScopeKey(namespace="support", context_fingerprint="tenant:acme"),
        policy_version="claim-disclosure-v1",
        authority="tapestry-visibility-v3",
        authority_available=True,
    )
    record = replace(_record(), disclosure=disclosure)

    assert ClaimEvidenceRecord.from_json(record.to_json()) == record
    assert record.disclosure.scope == ScopeKey(namespace="support", context_fingerprint="tenant:acme")


@pytest.mark.parametrize("field", ["raw_claim", "passage", "proof", "credential", "cypher", "embedding", "properties"])
def test_claim_evidence_decoder_rejects_excluded_payload_fields(field: str) -> None:
    payload = _record().to_dict()
    payload[field] = "secret"

    with pytest.raises(InvalidRequestError, match="invalid fields"):
        ClaimEvidenceRecord.from_dict(payload)


def test_claim_evidence_decoder_rejects_nested_unknown_fields_and_versions() -> None:
    unknown = _record().to_dict()
    validity = unknown["validity"]
    assert isinstance(validity, dict)
    validity["system_to"] = "secret"
    with pytest.raises(InvalidRequestError, match="invalid fields"):
        ClaimEvidenceRecord.from_dict(unknown)

    unsupported = _record().to_dict()
    references = unsupported["canonical_references"]
    assert isinstance(references, dict)
    references["schema_version"] = 2
    with pytest.raises(InvalidRequestError, match="schema_version"):
        ClaimEvidenceRecord.from_dict(unsupported)


def test_evidence_package_codec_canonicalizes_and_deduplicates() -> None:
    first = _record()
    second = replace(
        first,
        claim_id="claim:01J5M6Q9J9",
        path=("claim:01J5M6Q9J9",),
    )

    package = EvidencePackage.build((second, first, first))

    assert tuple(record.claim_id for record in package.records) == (first.claim_id, second.claim_id)
    assert package.retained_count == 2
    assert package.omitted_count == 1
    assert package.truncated is True
    assert package.truncation_reasons == (EvidencePackageTruncationReason.DUPLICATE_CLAIM_ID,)
    assert EvidencePackage.from_dict(package.to_dict()) == package
    assert EvidencePackage.from_json(package.to_json()) == package
    assert "null" not in package.to_json()
    assert EMPTY_EVIDENCE_PACKAGE.to_json() == EvidencePackage.build(()).to_json()


def test_evidence_package_rejects_conflicting_same_id_projections() -> None:
    conflict = replace(_record(), features=FeatureSet(values={"structured_match": 0.5}))

    with pytest.raises(InvalidRequestError, match="conflicting Claim evidence projections"):
        EvidencePackage.build((_record(), conflict))


def test_evidence_package_applies_count_limit_after_canonical_ordering() -> None:
    records = tuple(
        replace(_record(), claim_id=f"claim:{index:02d}", path=(f"claim:{index:02d}",)) for index in reversed(range(12))
    )

    package = EvidencePackage.build(records)

    assert package.retained_count == 10
    assert package.omitted_count == 2
    assert tuple(record.claim_id for record in package.records) == tuple(f"claim:{index:02d}" for index in range(10))
    assert package.truncation_reasons == (EvidencePackageTruncationReason.RECORD_LIMIT,)
    assert len(package.to_json().encode("utf-8")) <= 65_536


def test_evidence_package_applies_complete_serialized_byte_limit() -> None:
    records = tuple(
        replace(
            _record(),
            claim_id=f"claim:{index:02d}",
            path=(f"claim:{index:02d}",),
            selection_reasons=tuple(f"reason_{reason:02d}_{'x' * 70}" for reason in range(16)),
        )
        for index in range(5)
    )

    package = EvidencePackage.build(records, max_bytes=5_000)

    assert 0 < package.retained_count < len(records)
    assert package.omitted_count == len(records) - package.retained_count
    assert package.truncation_reasons == (EvidencePackageTruncationReason.SERIALIZED_SIZE_LIMIT,)
    assert len(package.to_json().encode("utf-8")) <= 5_000


def test_evidence_package_hard_byte_limit_truncates_before_construction() -> None:
    large_features = FeatureSet(values={f"feature_{index:02d}_{'x' * 75}": 1.0 for index in range(64)})
    records = tuple(
        replace(
            _record(),
            claim_id=f"claim:{index:02d}",
            path=(f"claim:{index:02d}",),
            features=large_features,
            selection_reasons=tuple(f"reason_{reason:02d}_{'x' * 70}" for reason in range(16)),
        )
        for index in range(10)
    )

    package = EvidencePackage.build(records)

    assert package.retained_count < len(records)
    assert package.omitted_count == len(records) - package.retained_count
    assert package.truncation_reasons == (EvidencePackageTruncationReason.SERIALIZED_SIZE_LIMIT,)
    assert len(package.to_json().encode("utf-8")) <= 65_536
    with pytest.raises(InvalidRequestError, match="65536 UTF-8 bytes"):
        EvidencePackage(records, 10, 0, False, ())


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"wire_version": 2}, "unsupported evidence package wire_version"),
        ({"records": (_record(),) * 11, "retained_count": 11}, "limit of 10"),
        ({"records": (_record(),), "retained_count": 0}, "must equal"),
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
    with pytest.raises(InvalidRequestError, match=message):
        replace(EMPTY_EVIDENCE_PACKAGE, **changes)


def test_evidence_package_decoder_is_closed_and_rejects_unknown_reasons() -> None:
    payload = EMPTY_EVIDENCE_PACKAGE.to_dict()
    payload["unexpected"] = True
    with pytest.raises(InvalidRequestError, match="invalid fields"):
        EvidencePackage.from_dict(payload)

    payload = EMPTY_EVIDENCE_PACKAGE.to_dict()
    payload.update({"omitted_count": 1, "truncated": True, "truncation_reasons": ["content_filtered"]})
    with pytest.raises(InvalidRequestError, match="unsupported evidence package truncation reason"):
        EvidencePackage.from_dict(payload)


def test_evidence_package_builder_validates_requested_limits() -> None:
    with pytest.raises(InvalidRequestError, match="max_records"):
        EvidencePackage.build((_record(),), max_records=11)
    with pytest.raises(InvalidRequestError, match="max_bytes"):
        EvidencePackage.build((_record(),), max_bytes=255)
    with pytest.raises(InvalidRequestError, match="input exceeds the limit of 1000"):
        EvidencePackage.build((_record(),) * 1_001)


def test_evidence_package_json_decoder_rejects_oversized_input_before_parsing() -> None:
    with pytest.raises(InvalidRequestError, match="65536 UTF-8 bytes"):
        EvidencePackage.from_json("{" + " " * 65_536 + "}")
