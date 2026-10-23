"""Canonical, count- and byte-bounded collections of full-Proposition evidence.

This owner preserves complete records and reports duplicate, count, and byte
truncation. Individual evidence contracts are owned below it.
"""

from engram.constants_evidence import (
    EVIDENCE_PACKAGE_FIELDS,
    MAX_EVIDENCE_PACKAGE_BYTES,
    MAX_EVIDENCE_PACKAGE_INPUT_RECORDS,
    MAX_EVIDENCE_PACKAGE_RECORD_COUNT,
    MAX_EVIDENCE_PACKAGE_RECORDS,
    MAX_EVIDENCE_PACKAGE_TRUNCATION_REASONS,
    MIN_EVIDENCE_PACKAGE_BYTES,
    EvidencePackageTruncationReason,
)
from engram.constants_resolution import MAX_REASON_CODE_BYTES
from engram.errors import InvalidRequestError
from engram.resolution_evidence import (
    proposition_evidence_record_from_dict,
    trusted_proposition_evidence_record_to_dict,
    validate_proposition_evidence_record,
)
from engram.resolution_values import exact_mapping, freeze_mapping, json_text, load_json_mapping, require_int, require_list
from engram.validation import require_any_text, require_bool


def evidence_package_payload(value: dict) -> dict[str, object]:
    result = {
        "records": [trusted_proposition_evidence_record_to_dict(record) for record in value.get("records", ())],
        "retained_count": value.get("retained_count", 0),
        "omitted_count": value.get("omitted_count", 0),
        "truncated": value.get("truncated", False),
        "truncation_reasons": [reason.value for reason in value.get("truncation_reasons", ())],
    }
    return result


def evidence_package(
    records: object,
    retained_count: object,
    omitted_count: object,
    truncated: object,
    truncation_reasons: object,
) -> dict:
    """Build one canonical, count- and byte-bounded full-Proposition package."""
    if not isinstance(records, tuple):
        raise InvalidRequestError("evidence package records must be a tuple of PropositionEvidenceRecord values")
    # Counts are refused before any record is validated or copied.
    if len(records) > MAX_EVIDENCE_PACKAGE_RECORDS:
        raise InvalidRequestError(f"evidence package records exceeds the limit of {MAX_EVIDENCE_PACKAGE_RECORDS}")
    try:
        validated_records = tuple(validate_proposition_evidence_record(record) for record in records)
    except InvalidRequestError as err:
        raise InvalidRequestError("evidence package records must be a tuple of PropositionEvidenceRecord values") from err
    identifiers = tuple(record.get("proposition_id", "") for record in validated_records)
    if identifiers != tuple(sorted(identifiers)):
        raise InvalidRequestError("evidence package records must use canonical Proposition-ID order")
    if len(set(identifiers)) != len(identifiers):
        raise InvalidRequestError("evidence package records must have unique Proposition IDs")
    retained = require_int(retained_count, "evidence package retained_count", 0, MAX_EVIDENCE_PACKAGE_RECORD_COUNT)
    omitted = require_int(omitted_count, "evidence package omitted_count", 0, MAX_EVIDENCE_PACKAGE_RECORD_COUNT)
    if retained != len(validated_records):
        raise InvalidRequestError("evidence package retained_count must equal the number of records")
    normalized_truncated = require_bool(truncated, "evidence package truncated")
    if not isinstance(truncation_reasons, tuple):
        raise InvalidRequestError("evidence package truncation_reasons must be a tuple")
    if len(truncation_reasons) > MAX_EVIDENCE_PACKAGE_TRUNCATION_REASONS:
        raise InvalidRequestError(
            f"evidence package truncation_reasons exceeds the limit of {MAX_EVIDENCE_PACKAGE_TRUNCATION_REASONS}"
        )
    if not all(isinstance(reason, EvidencePackageTruncationReason) for reason in truncation_reasons):
        raise InvalidRequestError("evidence package truncation_reasons must contain EvidencePackageTruncationReason values")
    normalized_reasons = tuple(truncation_reasons)
    reason_values = tuple(reason.value for reason in normalized_reasons)
    if reason_values != tuple(sorted(set(reason_values))):
        raise InvalidRequestError("evidence package truncation_reasons must be unique and sorted")
    if normalized_truncated != (omitted > 0):
        raise InvalidRequestError("evidence package truncated must equal whether omitted_count is positive")
    if normalized_truncated != bool(normalized_reasons):
        raise InvalidRequestError("evidence package truncation_reasons must be present exactly when truncated")
    result: dict = {
        "records": validated_records,
        "retained_count": retained,
        "omitted_count": omitted,
        "truncated": normalized_truncated,
        "truncation_reasons": normalized_reasons,
    }
    encoded = json_text(evidence_package_payload(result)).encode("utf-8")
    if len(encoded) > MAX_EVIDENCE_PACKAGE_BYTES:
        raise InvalidRequestError(f"evidence package exceeds the limit of {MAX_EVIDENCE_PACKAGE_BYTES} UTF-8 bytes")
    return result


def empty_evidence_package() -> dict:
    """Return one isolated concrete empty evidence package."""
    result: dict = {
        "records": (),
        "retained_count": 0,
        "omitted_count": 0,
        "truncated": False,
        "truncation_reasons": (),
    }
    return result


def validate_evidence_package(value: object) -> dict:
    """Revalidate and defensively copy one evidence-package dictionary."""
    data = exact_mapping(value, "EvidencePackage", EVIDENCE_PACKAGE_FIELDS)
    result = evidence_package(
        data.get("records", ()),
        data.get("retained_count", 0),
        data.get("omitted_count", 0),
        data.get("truncated", False),
        data.get("truncation_reasons", ()),
    )
    return result


def canonical_proposition_evidence_records(records: object) -> tuple[tuple[dict, ...], int]:
    if not isinstance(records, tuple):
        raise InvalidRequestError("evidence package input must be a tuple of PropositionEvidenceRecord values")
    if len(records) > MAX_EVIDENCE_PACKAGE_INPUT_RECORDS:
        raise InvalidRequestError(f"evidence package input exceeds the limit of {MAX_EVIDENCE_PACKAGE_INPUT_RECORDS}")
    try:
        validated_records = tuple(validate_proposition_evidence_record(record) for record in records)
    except InvalidRequestError as err:
        raise InvalidRequestError("evidence package input must be a tuple of PropositionEvidenceRecord values") from err
    by_proposition_id: dict[str, dict] = {}
    duplicate_count = 0
    for record in validated_records:
        proposition_id = record.get("proposition_id", "")
        if proposition_id in by_proposition_id:
            previous = by_proposition_id.get(proposition_id, {})
            if previous != record:
                raise InvalidRequestError(f"conflicting Proposition evidence projections for Proposition ID: {proposition_id}")
            duplicate_count += 1
            continue
        by_proposition_id[proposition_id] = record
    canonical = tuple(by_proposition_id.get(proposition_id, {}) for proposition_id in sorted(by_proposition_id))
    result = (canonical, duplicate_count)
    return result


def build_evidence_package(
    records: object,
    *,
    max_records: object = MAX_EVIDENCE_PACKAGE_RECORDS,
    max_bytes: object = MAX_EVIDENCE_PACKAGE_BYTES,
) -> dict:
    """Canonicalize, deduplicate, and fit full-Proposition evidence to configured bounds."""
    retained_limit = require_int(max_records, "evidence package max_records", 0, MAX_EVIDENCE_PACKAGE_RECORDS)
    byte_limit = require_int(max_bytes, "evidence package max_bytes", MIN_EVIDENCE_PACKAGE_BYTES, MAX_EVIDENCE_PACKAGE_BYTES)
    canonical, duplicate_count = canonical_proposition_evidence_records(records)
    reasons: set[EvidencePackageTruncationReason] = set()
    omitted = duplicate_count
    if duplicate_count:
        reasons.add(EvidencePackageTruncationReason.DUPLICATE_PROPOSITION_ID)
    retained = canonical[:retained_limit]
    if len(canonical) > retained_limit:
        omitted += len(canonical) - retained_limit
        reasons.add(EvidencePackageTruncationReason.RECORD_LIMIT)

    while True:
        ordered_reasons = tuple(sorted(reasons, key=lambda reason: reason.value))
        package: dict = {
            "records": retained,
            "retained_count": len(retained),
            "omitted_count": omitted,
            "truncated": omitted > 0,
            "truncation_reasons": ordered_reasons,
        }
        payload = evidence_package_payload(package)
        if len(json_text(payload).encode("utf-8")) <= byte_limit:
            result = package
            break
        if not retained:
            raise InvalidRequestError("evidence package max_bytes cannot contain the empty package envelope")
        retained = retained[:-1]
        omitted += 1
        reasons.add(EvidencePackageTruncationReason.SERIALIZED_SIZE_LIMIT)
    return result


def evidence_package_to_dict(value: object) -> dict:
    """Serialize one evidence package."""
    package = validate_evidence_package(value)
    result = evidence_package_payload(package)
    return result


def evidence_package_from_dict(value: object) -> dict:
    """Decode one evidence package from its exact serialized form."""
    data = exact_mapping(value, "EvidencePackage", EVIDENCE_PACKAGE_FIELDS)
    records = require_list(data.get("records", []), "evidence package records")
    if len(records) > MAX_EVIDENCE_PACKAGE_RECORDS:
        raise InvalidRequestError(f"evidence package records exceeds the limit of {MAX_EVIDENCE_PACKAGE_RECORDS}")
    raw_reasons = require_list(data.get("truncation_reasons", []), "evidence package truncation_reasons")
    reasons = []
    for reason_value in raw_reasons:
        try:
            reason = EvidencePackageTruncationReason(
                require_any_text(
                    reason_value,
                    "evidence package truncation reason",
                    MAX_REASON_CODE_BYTES,
                    allow_empty=False,
                )
            )
        except ValueError as err:
            raise InvalidRequestError("unsupported evidence package truncation reason") from err
        reasons.append(reason)
    decoded_records = tuple(
        proposition_evidence_record_from_dict(freeze_mapping(record, "evidence package record")) for record in records
    )
    result = evidence_package(
        decoded_records,
        data.get("retained_count", 0),
        data.get("omitted_count", 0),
        data.get("truncated", False),
        tuple(reasons),
    )
    return result


def evidence_package_to_json(value: object) -> str:
    """Serialize one evidence package deterministically."""
    payload = evidence_package_to_dict(value)
    result = json_text(payload)
    return result


def trusted_evidence_package_to_json(value: dict) -> str:
    """Serialize an orchestrator-owned evidence package."""
    payload = evidence_package_payload(value)
    result = json_text(payload)
    return result


def evidence_package_from_json(value: str) -> dict:
    """Decode one bounded evidence package from deterministic JSON."""
    if not isinstance(value, str):
        raise InvalidRequestError("EvidencePackage JSON must be a string")
    if len(value.encode("utf-8")) > MAX_EVIDENCE_PACKAGE_BYTES:
        raise InvalidRequestError(f"evidence package exceeds the limit of {MAX_EVIDENCE_PACKAGE_BYTES} UTF-8 bytes")
    data = load_json_mapping(value, "EvidencePackage JSON")
    result = evidence_package_from_dict(data)
    return result
