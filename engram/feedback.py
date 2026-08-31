"""Versioned feedback learning and bounded negative-resolution state."""

import hashlib
import json
import math
import threading
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from enum import Enum
from functools import lru_cache
from types import MappingProxyType, NoneType

from engram.constants import (
    DEFAULT_NEGATIVE_MAX_RECORDS,
    DEFAULT_NEGATIVE_TTL_SECONDS,
    EMPTY_FINGERPRINT,
    EMPTY_NEGATIVE_CREATED_AT,
    EMPTY_NEGATIVE_EXPIRES_AT,
    EMPTY_SCOPE_KEY,
    FEEDBACK_BUCKET_FIELDS,
    FEEDBACK_BUCKET_SCHEMA_VERSION,
    FEEDBACK_BUCKET_SECONDS,
    FEEDBACK_CONTRACT_FINGERPRINT,
    FEEDBACK_HALF_LIFE_SECONDS,
    FEEDBACK_HISTORY_FIELDS,
    FEEDBACK_HISTORY_SCHEMA_VERSION,
    FEEDBACK_KEY_SCHEMA_VERSION,
    FEEDBACK_MAX_BUCKETS_PER_RECORD,
    FEEDBACK_MAX_RELATIONSHIP_RECORDS,
    FEEDBACK_MAX_STATEMENT_RECORDS,
    FEEDBACK_MINIMUM_VERDICT_SAMPLES,
    FEEDBACK_OBSERVATION_FIELDS,
    FEEDBACK_OBSERVATION_SCHEMA_VERSION,
    FEEDBACK_OUTCOME_COUNTER_FIELDS,
    FEEDBACK_POLICY_FIELDS,
    FEEDBACK_POLICY_SCHEMA_VERSION,
    FEEDBACK_POLICY_VERSION,
    FEEDBACK_PRIOR_ACCEPT,
    FEEDBACK_PRIOR_REJECT,
    FEEDBACK_RECORD_FIELDS,
    FEEDBACK_RECORD_SCHEMA_VERSION,
    FEEDBACK_STATE_FIELDS,
    FEEDBACK_STATE_SCHEMA_VERSION,
    FEEDBACK_STATISTICS_FIELDS,
    FEEDBACK_STATISTICS_SCHEMA_VERSION,
    MAX_CONSTRAINT_JSON_BYTES,
    MAX_FEEDBACK_BUCKET_SECONDS,
    MAX_FEEDBACK_BUCKETS,
    MAX_FEEDBACK_HALF_LIFE_SECONDS,
    MAX_FEEDBACK_OBSERVATIONS,
    MAX_FEEDBACK_POLICY_RECORDS,
    MAX_FEEDBACK_POLICY_SAMPLES,
    MAX_FEEDBACK_PRIOR_MASS,
    MAX_FEEDBACK_REASON_BYTES,
    MAX_FEEDBACK_SIGNATURE_BYTES,
    MAX_FINGERPRINT_BYTES,
    MAX_INSPECTION_RECORDS,
    MAX_NAMESPACE_BYTES,
    MAX_NEGATIVE_RECORDS,
    MAX_NEGATIVE_TTL_SECONDS,
    MAX_REFERENCE_ID_BYTES,
    MAX_STATEMENT_ID_BYTES,
    MAX_TIMESTAMP_BYTES,
    MAX_VERSION_BYTES,
    NEGATIVE_KEY_SCHEMA_VERSION,
    NEGATIVE_LOOKUP_FIELDS,
    NEGATIVE_RESOLUTION_FIELDS,
    NEGATIVE_RESOLUTION_KEY_FIELDS,
    NEGATIVE_RESOLUTION_SCHEMA_VERSION,
    POLICY_SUPPRESSION_FIELDS,
    RELATIONSHIP_FEEDBACK_KEY_FIELDS,
    STALE_EXCLUSION_FIELDS,
    STATEMENT_FEEDBACK_KEY_FIELDS,
    FeedbackObservationKind,
    FeedbackOutcome,
    FeedbackReferenceKind,
    LifecycleHandoffStatus,
    NegativeResolutionReason,
)
from engram.errors import ConflictError, IdentityValidationError, InvalidRequestError
from engram.identity import (
    QueryIdentity,
    ScopeKey,
    query_identity,
    query_identity_from_dict,
    query_identity_to_dict,
    scope_key_from_dict,
    scope_key_to_dict,
    validate_query_identity,
    validate_scope_key,
)
from engram.mutations import (
    MutationOperation,
    MutationReceipt,
    MutationReceiptLedger,
    MutationResultCode,
    ReceiptCompletionState,
    ReceiptLookupOutcome,
    mutation_receipt,
    mutation_receipt_ledger_from_snapshot,
    receipt_lookup_receipt,
    validate_mutation_receipt,
)


def _text(value: object, name: str, maximum_bytes: int, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        raise InvalidRequestError(f"{name} must be a string")
    if not allow_empty and not value:
        raise InvalidRequestError(f"{name} must not be empty")
    if len(value.encode("utf-8")) > maximum_bytes:
        raise InvalidRequestError(f"{name} exceeds the UTF-8 limit of {maximum_bytes} bytes")
    if any(ord(character) < 32 or 127 <= ord(character) <= 159 or 0xD800 <= ord(character) <= 0xDFFF for character in value):
        raise InvalidRequestError(f"{name} contains a control or surrogate character")
    return value


def _integer(value: object, name: str, minimum: int = 0, maximum: int = 9_223_372_036_854_775_807) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise InvalidRequestError(f"{name} must be an integer from {minimum} through {maximum}")
    return value


def _number(value: object, name: str, minimum: float, maximum: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise InvalidRequestError(f"{name} must be a number")
    normalized = float(value)
    if not math.isfinite(normalized) or not minimum <= normalized <= maximum:
        raise InvalidRequestError(f"{name} must be finite and from {minimum} through {maximum}")
    return normalized


def _boolean(value: object, name: str) -> bool:
    if not isinstance(value, bool):
        raise InvalidRequestError(f"{name} must be a boolean")
    return value


def _exact_mapping(value: object, name: str, fields: set[str]) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise InvalidRequestError(f"{name} must be an object")
    actual = set(value)
    if actual != fields:
        raise InvalidRequestError(f"{name} has invalid fields: missing={sorted(fields - actual)}, extra={sorted(actual - fields)}")
    return value


def _json_text(value: object) -> str:
    result = json.dumps(_thaw_json(value), sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    return result


def _thaw_json(value: object) -> object:
    if isinstance(value, Mapping):
        result = {str(key): _thaw_json(item) for key, item in value.items()}
        return result
    if isinstance(value, tuple):
        result = [_thaw_json(item) for item in value]
        return result
    return value


def _load_json_mapping(value: str, name: str) -> Mapping[str, object]:
    if not isinstance(value, str):
        raise InvalidRequestError(f"{name} must be a string")
    try:
        decoded = json.loads(value)
    except json.JSONDecodeError as error:
        raise InvalidRequestError(f"{name} must be valid JSON") from error
    if not isinstance(decoded, Mapping):
        raise InvalidRequestError(f"{name} must contain an object")
    return decoded


def canonical_fingerprint(value: object) -> str:
    """Return one content-only lowercase SHA-256 fingerprint."""

    result = hashlib.sha256(_json_text(value).encode("utf-8")).hexdigest()
    return result


def _feedback_payload_signature(observations) -> str:
    """Hash a bounded observation sequence without the generic receipt item ceiling.

    Feedback observations are already strict, bounded contracts.  Hashing each
    canonical observation first keeps the receipt input fixed-size while the
    byte counter retains one explicit bound on the complete validated payload.
    Observation time is execution metadata and is deliberately omitted from
    retry identity.
    """

    digest = hashlib.sha256(b"engram-feedback-observations-v1\0")
    seen_observations: set[str] = set()
    total_bytes = 0
    for observation in observations:
        complete = _trusted_feedback_observation_to_dict(observation)
        complete_text = _json_text(complete)
        complete_fingerprint = hashlib.sha256(complete_text.encode("utf-8")).hexdigest()
        if complete_fingerprint in seen_observations:
            raise InvalidRequestError("feedback observations must be unique")
        seen_observations.add(complete_fingerprint)

        complete.pop("observed_at")
        encoded = _json_text(complete).encode("utf-8")
        total_bytes += len(encoded)
        if total_bytes > MAX_FEEDBACK_SIGNATURE_BYTES:
            raise InvalidRequestError(
                f"feedback observations exceed the signature input limit of {MAX_FEEDBACK_SIGNATURE_BYTES} bytes"
            )
        digest.update(hashlib.sha256(encoded).digest())
    digest.update(len(observations).to_bytes(8, "big"))
    result = f"sha256:{digest.hexdigest()}"
    return result


def _fingerprint(value: object, name: str) -> str:
    if not isinstance(value, str):
        raise InvalidRequestError(f"{name} must be a string")
    if len(value) != MAX_FINGERPRINT_BYTES or any(character not in "0123456789abcdef" for character in value):
        raise InvalidRequestError(f"{name} must be a lowercase SHA-256 fingerprint")
    return value


@lru_cache(maxsize=4_096)
def _parse_timestamp_text(text: str, name: str) -> datetime:
    _text(text, name, MAX_TIMESTAMP_BYTES)
    if not text.endswith("Z"):
        raise InvalidRequestError(f"{name} must be a canonical RFC 3339 UTC timestamp ending in Z")
    try:
        parsed = datetime.fromisoformat(text[:-1] + "+00:00")
    except ValueError as error:
        raise InvalidRequestError(f"{name} must be a canonical RFC 3339 UTC timestamp") from error
    if parsed.tzinfo != UTC or parsed.isoformat().replace("+00:00", "Z") != text:
        raise InvalidRequestError(f"{name} must use the canonical RFC 3339 UTC representation")
    return parsed


def _parse_timestamp(value: object, name: str) -> datetime:
    if not isinstance(value, str):
        raise InvalidRequestError(f"{name} must be a string")
    result = _parse_timestamp_text(value, name)
    return result


def canonical_utc(value: datetime) -> str:
    """Serialize an aware UTC datetime using the repository's canonical form."""

    if not isinstance(value, datetime) or isinstance(value.tzinfo, NoneType):
        raise InvalidRequestError("feedback clock must return an aware datetime")
    result = value.astimezone(UTC).isoformat().replace("+00:00", "Z")
    return result


def _bucket_start(value: str, bucket_seconds: int) -> str:
    parsed = _parse_timestamp(value, "feedback observed_at")
    epoch_seconds = int(parsed.timestamp())
    start_seconds = epoch_seconds - (epoch_seconds % bucket_seconds)
    result = canonical_utc(datetime.fromtimestamp(start_seconds, UTC))
    return result


def _diagnostic_id(value: str) -> str:
    result = f"sha256:{hashlib.sha256(value.encode('utf-8')).hexdigest()[:24]}"
    return result


def constraint_fingerprint(
    expected_object_type: str,
    required_metadata: Mapping[str, object],
    required_source_label: str,
) -> str:
    """Fingerprint the bounded request constraints that affect eligibility."""

    if not isinstance(required_metadata, Mapping):
        raise InvalidRequestError("feedback required_metadata must be an object")
    _text(expected_object_type, "feedback expected_object_type", 64)
    _text(required_source_label, "feedback required_source_label", 256, allow_empty=True)
    value = {
        "expected_object_type": expected_object_type,
        "required_metadata": dict(required_metadata),
        "required_source_label": required_source_label,
    }
    try:
        encoded = _json_text(value).encode("utf-8")
    except (TypeError, ValueError, RecursionError) as error:
        raise InvalidRequestError("feedback constraints must contain bounded JSON values") from error
    if len(encoded) > MAX_CONSTRAINT_JSON_BYTES:
        raise InvalidRequestError(f"feedback constraints exceed the JSON limit of {MAX_CONSTRAINT_JSON_BYTES} bytes")
    result = hashlib.sha256(encoded).hexdigest()
    return result


FeedbackPolicy = dict


def feedback_policy(
    policy_version: object = FEEDBACK_POLICY_VERSION,
    minimum_verdict_samples: object = FEEDBACK_MINIMUM_VERDICT_SAMPLES,
    prior_accept: object = FEEDBACK_PRIOR_ACCEPT,
    prior_reject: object = FEEDBACK_PRIOR_REJECT,
    half_life_seconds: object = FEEDBACK_HALF_LIFE_SECONDS,
    bucket_seconds: object = FEEDBACK_BUCKET_SECONDS,
    max_buckets_per_record: object = FEEDBACK_MAX_BUCKETS_PER_RECORD,
    max_statement_records: object = FEEDBACK_MAX_STATEMENT_RECORDS,
    max_relationship_records: object = FEEDBACK_MAX_RELATIONSHIP_RECORDS,
    schema_version: object = FEEDBACK_POLICY_SCHEMA_VERSION,
) -> FeedbackPolicy:
    """Build the hand-authored, unfitted aging and history policy."""
    version = _integer(schema_version, "feedback policy schema_version", 0)
    if version != FEEDBACK_POLICY_SCHEMA_VERSION:
        raise InvalidRequestError(f"unsupported feedback policy schema_version: {version}")
    validated_policy_version = _text(policy_version, "feedback policy_version", MAX_VERSION_BYTES)
    validated_samples = _integer(minimum_verdict_samples, "feedback minimum_verdict_samples", 1, MAX_FEEDBACK_POLICY_SAMPLES)
    validated_prior_accept = _number(prior_accept, "feedback prior_accept", 0.0, MAX_FEEDBACK_PRIOR_MASS)
    validated_prior_reject = _number(prior_reject, "feedback prior_reject", 0.0, MAX_FEEDBACK_PRIOR_MASS)
    if validated_prior_accept + validated_prior_reject <= 0:
        raise InvalidRequestError("feedback priors must have positive total mass")
    validated_half_life = _integer(half_life_seconds, "feedback half_life_seconds", 1, MAX_FEEDBACK_HALF_LIFE_SECONDS)
    validated_bucket_seconds = _integer(bucket_seconds, "feedback bucket_seconds", 1, MAX_FEEDBACK_BUCKET_SECONDS)
    validated_max_buckets = _integer(max_buckets_per_record, "feedback max_buckets_per_record", 1, MAX_FEEDBACK_BUCKETS)
    validated_statement_records = _integer(max_statement_records, "feedback max_statement_records", 1, MAX_FEEDBACK_POLICY_RECORDS)
    validated_relationship_records = _integer(
        max_relationship_records, "feedback max_relationship_records", 1, MAX_FEEDBACK_POLICY_RECORDS
    )
    result: FeedbackPolicy = {
        "policy_version": validated_policy_version,
        "minimum_verdict_samples": validated_samples,
        "prior_accept": validated_prior_accept,
        "prior_reject": validated_prior_reject,
        "half_life_seconds": validated_half_life,
        "bucket_seconds": validated_bucket_seconds,
        "max_buckets_per_record": validated_max_buckets,
        "max_statement_records": validated_statement_records,
        "max_relationship_records": validated_relationship_records,
        "schema_version": version,
    }
    return result


def validate_feedback_policy(value: object) -> FeedbackPolicy:
    data = _exact_mapping(value, "FeedbackPolicy", FEEDBACK_POLICY_FIELDS)
    result = feedback_policy(
        data["policy_version"],
        data["minimum_verdict_samples"],
        data["prior_accept"],
        data["prior_reject"],
        data["half_life_seconds"],
        data["bucket_seconds"],
        data["max_buckets_per_record"],
        data["max_statement_records"],
        data["max_relationship_records"],
        data["schema_version"],
    )
    return result


def feedback_policy_with_changes(value: object, changes: object) -> FeedbackPolicy:
    current = validate_feedback_policy(value)
    if not isinstance(changes, Mapping) or not set(changes).issubset(FEEDBACK_POLICY_FIELDS):
        raise InvalidRequestError("feedback policy changes contain invalid fields")
    updated: dict[str, object] = dict(current)
    updated.update(changes)
    result = validate_feedback_policy(updated)
    return result


def feedback_policy_to_dict(value: object) -> dict[str, object]:
    current = validate_feedback_policy(value)
    result = dict(current)
    return result


def feedback_policy_to_json(value: object) -> str:
    data = feedback_policy_to_dict(value)
    result = _json_text(data)
    return result


def feedback_policy_from_dict(value: object) -> FeedbackPolicy:
    data = _exact_mapping(value, "FeedbackPolicy", FEEDBACK_POLICY_FIELDS)
    result = feedback_policy(
        schema_version=_integer(data["schema_version"], "feedback policy schema_version", 0),
        policy_version=_text(data["policy_version"], "feedback policy_version", MAX_VERSION_BYTES),
        minimum_verdict_samples=_integer(
            data["minimum_verdict_samples"], "feedback minimum_verdict_samples", 1, MAX_FEEDBACK_POLICY_SAMPLES
        ),
        prior_accept=_number(data["prior_accept"], "feedback prior_accept", 0.0, MAX_FEEDBACK_PRIOR_MASS),
        prior_reject=_number(data["prior_reject"], "feedback prior_reject", 0.0, MAX_FEEDBACK_PRIOR_MASS),
        half_life_seconds=_integer(data["half_life_seconds"], "feedback half_life_seconds", 1, MAX_FEEDBACK_HALF_LIFE_SECONDS),
        bucket_seconds=_integer(data["bucket_seconds"], "feedback bucket_seconds", 1, MAX_FEEDBACK_BUCKET_SECONDS),
        max_buckets_per_record=_integer(data["max_buckets_per_record"], "feedback max_buckets_per_record", 1, MAX_FEEDBACK_BUCKETS),
        max_statement_records=_integer(
            data["max_statement_records"], "feedback max_statement_records", 1, MAX_FEEDBACK_POLICY_RECORDS
        ),
        max_relationship_records=_integer(
            data["max_relationship_records"], "feedback max_relationship_records", 1, MAX_FEEDBACK_POLICY_RECORDS
        ),
    )
    return result


def feedback_policy_from_json(value: str) -> FeedbackPolicy:
    data = _load_json_mapping(value, "FeedbackPolicy JSON")
    result = feedback_policy_from_dict(data)
    return result


def feedback_policy_fingerprint(policy: object = {}) -> str:
    policy_value = feedback_policy() if policy == {} else policy
    try:
        data = feedback_policy_to_dict(policy_value)
    except InvalidRequestError as error:
        raise InvalidRequestError("feedback policy fingerprint requires FeedbackPolicy") from error
    result = canonical_fingerprint(data)
    return result


StatementFeedbackKey = dict


def statement_feedback_key(
    statement_id: object,
    generation: object,
    generation_available: object,
    policy_fingerprint: object,
    contract_fingerprint: object = FEEDBACK_CONTRACT_FINGERPRINT,
    schema_version: object = FEEDBACK_KEY_SCHEMA_VERSION,
) -> StatementFeedbackKey:
    """Build one statement-lineage observation partition."""
    version = _integer(schema_version, "statement feedback key schema_version", 0)
    if version != FEEDBACK_KEY_SCHEMA_VERSION:
        raise InvalidRequestError(f"unsupported statement feedback key schema_version: {version}")
    validated_statement_id = _text(statement_id, "feedback statement_id", MAX_STATEMENT_ID_BYTES)
    validated_generation = _integer(generation, "feedback generation", 0)
    generation_is_available = _boolean(generation_available, "feedback generation_available")
    if generation_is_available and validated_generation < 1:
        raise InvalidRequestError("available feedback generation must be positive")
    if not generation_is_available and validated_generation != 0:
        raise InvalidRequestError("unavailable feedback generation must be zero")
    validated_policy = _fingerprint(policy_fingerprint, "feedback policy_fingerprint")
    validated_contract = _fingerprint(contract_fingerprint, "feedback contract_fingerprint")
    result: StatementFeedbackKey = {
        "statement_id": validated_statement_id,
        "generation": validated_generation,
        "generation_available": generation_is_available,
        "policy_fingerprint": validated_policy,
        "contract_fingerprint": validated_contract,
        "schema_version": version,
    }
    return result


def validate_statement_feedback_key(value: object) -> StatementFeedbackKey:
    data = _exact_mapping(value, "StatementFeedbackKey", STATEMENT_FEEDBACK_KEY_FIELDS)
    result = statement_feedback_key(
        data["statement_id"],
        data["generation"],
        data["generation_available"],
        data["policy_fingerprint"],
        data["contract_fingerprint"],
        data["schema_version"],
    )
    return result


def statement_feedback_key_to_dict(value: object) -> dict[str, object]:
    current = validate_statement_feedback_key(value)
    result = dict(current)
    return result


def statement_feedback_key_fingerprint(value: object) -> str:
    data = statement_feedback_key_to_dict(value)
    result = canonical_fingerprint(data)
    return result


def statement_feedback_key_signature(value: object) -> tuple[object, ...]:
    current = validate_statement_feedback_key(value)
    result = (
        current["statement_id"],
        current["generation"],
        current["generation_available"],
        current["policy_fingerprint"],
        current["contract_fingerprint"],
        current["schema_version"],
    )
    return result


def statement_feedback_key_to_json(value: object) -> str:
    data = statement_feedback_key_to_dict(value)
    result = _json_text(data)
    return result


def statement_feedback_key_from_dict(value: object) -> StatementFeedbackKey:
    data = _exact_mapping(value, "StatementFeedbackKey", STATEMENT_FEEDBACK_KEY_FIELDS)
    result = statement_feedback_key(
        schema_version=_integer(data["schema_version"], "statement feedback key schema_version", 0),
        statement_id=_text(data["statement_id"], "feedback statement_id", MAX_STATEMENT_ID_BYTES),
        generation=_integer(data["generation"], "feedback generation", 0),
        generation_available=_boolean(data["generation_available"], "feedback generation_available"),
        policy_fingerprint=_fingerprint(data["policy_fingerprint"], "feedback policy_fingerprint"),
        contract_fingerprint=_fingerprint(data["contract_fingerprint"], "feedback contract_fingerprint"),
    )
    return result


def statement_feedback_key_from_json(value: str) -> StatementFeedbackKey:
    data = _load_json_mapping(value, "StatementFeedbackKey JSON")
    result = statement_feedback_key_from_dict(data)
    return result


RelationshipFeedbackKey = dict


def relationship_feedback_key(
    query_identity: object,
    scope: object,
    constraint_fingerprint: object,
    statement: object,
    schema_version: object = FEEDBACK_KEY_SCHEMA_VERSION,
) -> RelationshipFeedbackKey:
    """Build one exact query, scope, constraint, and statement partition."""
    version = _integer(schema_version, "relationship feedback key schema_version", 0)
    if version != FEEDBACK_KEY_SCHEMA_VERSION:
        raise InvalidRequestError(f"unsupported relationship feedback key schema_version: {version}")
    try:
        validated_query_identity = validate_query_identity(query_identity)
    except IdentityValidationError as error:
        raise InvalidRequestError("relationship query_identity must be a QueryIdentity") from error
    try:
        validated_scope = validate_scope_key(scope)
    except IdentityValidationError as error:
        raise InvalidRequestError("relationship scope must match query identity scope") from error
    if validated_query_identity["scope"] != validated_scope:
        raise InvalidRequestError("relationship scope must match query identity scope")
    validated_constraint = _fingerprint(constraint_fingerprint, "relationship constraint_fingerprint")
    try:
        validated_statement = validate_statement_feedback_key(statement)
    except InvalidRequestError as error:
        raise InvalidRequestError("relationship statement must be a StatementFeedbackKey") from error
    result: RelationshipFeedbackKey = {
        "query_identity": validated_query_identity,
        "scope": validated_scope,
        "constraint_fingerprint": validated_constraint,
        "statement": validated_statement,
        "schema_version": version,
    }
    return result


def validate_relationship_feedback_key(value: object) -> RelationshipFeedbackKey:
    data = _exact_mapping(value, "RelationshipFeedbackKey", RELATIONSHIP_FEEDBACK_KEY_FIELDS)
    result = relationship_feedback_key(
        data["query_identity"],
        data["scope"],
        data["constraint_fingerprint"],
        data["statement"],
        data["schema_version"],
    )
    return result


def relationship_feedback_key_to_dict(value: object) -> dict[str, object]:
    current = validate_relationship_feedback_key(value)
    result = {
        "schema_version": current["schema_version"],
        "query_identity": _feedback_wire_value(current["query_identity"]),
        "scope": _feedback_wire_value(current["scope"]),
        "constraint_fingerprint": current["constraint_fingerprint"],
        "statement": statement_feedback_key_to_dict(current["statement"]),
    }
    return result


def relationship_feedback_key_fingerprint(value: object) -> str:
    data = relationship_feedback_key_to_dict(value)
    result = canonical_fingerprint(data)
    return result


def relationship_feedback_key_to_json(value: object) -> str:
    data = relationship_feedback_key_to_dict(value)
    result = _json_text(data)
    return result


def relationship_feedback_key_from_dict(
    value: object,
    identity_cache: object = (),
    scope_cache: object = (),
    statement_cache: object = (),
) -> RelationshipFeedbackKey:
    data = _exact_mapping(value, "RelationshipFeedbackKey", RELATIONSHIP_FEEDBACK_KEY_FIELDS)
    for name in ("query_identity", "scope", "statement"):
        if not isinstance(data[name], Mapping):
            raise InvalidRequestError(f"relationship {name} must be an object")
    if identity_cache != () and not isinstance(identity_cache, dict):
        raise InvalidRequestError("relationship identity_cache must be a dictionary")
    if scope_cache != () and not isinstance(scope_cache, dict):
        raise InvalidRequestError("relationship scope_cache must be a dictionary")
    if statement_cache != () and not isinstance(statement_cache, dict):
        raise InvalidRequestError("relationship statement_cache must be a dictionary")
    identities = identity_cache if isinstance(identity_cache, dict) else {}
    scopes = scope_cache if isinstance(scope_cache, dict) else {}
    statements = statement_cache if isinstance(statement_cache, dict) else {}
    identity_fingerprint = canonical_fingerprint(data["query_identity"])
    scope_fingerprint = canonical_fingerprint(data["scope"])
    statement_fingerprint = canonical_fingerprint(data["statement"])
    if identity_fingerprint in identities:
        query_identity = identities[identity_fingerprint]
    else:
        query_identity = query_identity_from_dict(data["query_identity"])
        identities[identity_fingerprint] = query_identity
    if scope_fingerprint in scopes:
        scope = scopes[scope_fingerprint]
    else:
        scope = scope_key_from_dict(data["scope"])
        scopes[scope_fingerprint] = scope
    if statement_fingerprint in statements:
        statement = statements[statement_fingerprint]
    else:
        statement = statement_feedback_key_from_dict(data["statement"])
        statements[statement_fingerprint] = statement
    version = _integer(data["schema_version"], "relationship feedback key schema_version", 0)
    if version != FEEDBACK_KEY_SCHEMA_VERSION:
        raise InvalidRequestError(f"unsupported relationship feedback key schema_version: {version}")
    if query_identity["scope"] != scope:
        raise InvalidRequestError("relationship scope must match query identity scope")
    result: RelationshipFeedbackKey = {
        "query_identity": query_identity,
        "scope": scope,
        "constraint_fingerprint": _fingerprint(data["constraint_fingerprint"], "relationship constraint_fingerprint"),
        "statement": statement,
        "schema_version": version,
    }
    return result


def relationship_feedback_key_from_json(value: str) -> RelationshipFeedbackKey:
    data = _load_json_mapping(value, "RelationshipFeedbackKey JSON")
    result = relationship_feedback_key_from_dict(data)
    return result


FeedbackObservation = dict


def feedback_observation(
    reference_kind: object,
    reference_id: object,
    kind: object,
    outcome: object,
    query_identity: object,
    scope: object,
    constraint_fingerprint: object,
    statement_id: object,
    generation: object,
    generation_available: object,
    policy_fingerprint: object,
    observed_at: object,
    reason: object = "",
    contract_fingerprint: object = FEEDBACK_CONTRACT_FINGERPRINT,
    schema_version: object = FEEDBACK_OBSERVATION_SCHEMA_VERSION,
) -> FeedbackObservation:
    """Build one exactly targeted candidacy or external verdict observation."""
    version = _integer(schema_version, "feedback observation schema_version", 0)
    if version != FEEDBACK_OBSERVATION_SCHEMA_VERSION:
        raise InvalidRequestError(f"unsupported feedback observation schema_version: {version}")
    if not isinstance(reference_kind, FeedbackReferenceKind):
        raise InvalidRequestError("feedback reference_kind must be a FeedbackReferenceKind")
    validated_reference_id = _text(reference_id, "feedback reference_id", MAX_REFERENCE_ID_BYTES)
    if not isinstance(kind, FeedbackObservationKind) or not isinstance(outcome, FeedbackOutcome):
        raise InvalidRequestError("feedback kind and outcome must use the closed vocabularies")
    if kind == FeedbackObservationKind.CANDIDACY and outcome != FeedbackOutcome.CANDIDATE:
        raise InvalidRequestError("candidacy feedback must use the candidate outcome")
    if kind == FeedbackObservationKind.VERDICT and outcome == FeedbackOutcome.CANDIDATE:
        raise InvalidRequestError("verdict feedback cannot use the candidate outcome")
    try:
        validated_query_identity = validate_query_identity(query_identity)
    except IdentityValidationError as error:
        raise InvalidRequestError("feedback query_identity must be a QueryIdentity") from error
    try:
        validated_scope = validate_scope_key(scope)
    except IdentityValidationError as error:
        raise InvalidRequestError("feedback scope must match query identity scope") from error
    if validated_query_identity["scope"] != validated_scope:
        raise InvalidRequestError("feedback scope must match query identity scope")
    validated_constraint = _fingerprint(constraint_fingerprint, "feedback constraint_fingerprint")
    statement = statement_feedback_key(
        statement_id,
        generation,
        generation_available,
        policy_fingerprint,
        contract_fingerprint,
    )
    validated_observed_at = canonical_utc(_parse_timestamp(observed_at, "feedback observed_at"))
    validated_reason = _text(reason, "feedback reason", MAX_FEEDBACK_REASON_BYTES, allow_empty=True)
    result: FeedbackObservation = {
        "reference_kind": reference_kind,
        "reference_id": validated_reference_id,
        "kind": kind,
        "outcome": outcome,
        "query_identity": validated_query_identity,
        "scope": validated_scope,
        "constraint_fingerprint": validated_constraint,
        "statement_id": statement["statement_id"],
        "generation": statement["generation"],
        "generation_available": statement["generation_available"],
        "policy_fingerprint": statement["policy_fingerprint"],
        "observed_at": validated_observed_at,
        "reason": validated_reason,
        "contract_fingerprint": statement["contract_fingerprint"],
        "schema_version": version,
    }
    return result


def _trusted_feedback_observation(
    reference_kind: FeedbackReferenceKind,
    reference_id: str,
    kind: FeedbackObservationKind,
    outcome: FeedbackOutcome,
    query_identity: QueryIdentity,
    scope: ScopeKey,
    constraint_fingerprint: str,
    statement_id: str,
    generation: int,
    generation_available: bool,
    policy_fingerprint: str,
    observed_at: str,
    reason: str = "",
    contract_fingerprint: str = FEEDBACK_CONTRACT_FINGERPRINT,
) -> FeedbackObservation:
    """Build an observation from values established by the regulated service."""
    result: FeedbackObservation = {
        "reference_kind": reference_kind,
        "reference_id": reference_id,
        "kind": kind,
        "outcome": outcome,
        "query_identity": query_identity,
        "scope": scope,
        "constraint_fingerprint": constraint_fingerprint,
        "statement_id": statement_id,
        "generation": generation,
        "generation_available": generation_available,
        "policy_fingerprint": policy_fingerprint,
        "observed_at": observed_at,
        "reason": reason,
        "contract_fingerprint": contract_fingerprint,
        "schema_version": FEEDBACK_OBSERVATION_SCHEMA_VERSION,
    }
    return result


def validate_feedback_observation(value: object) -> FeedbackObservation:
    data = _exact_mapping(value, "FeedbackObservation", FEEDBACK_OBSERVATION_FIELDS)
    result = feedback_observation(
        data["reference_kind"],
        data["reference_id"],
        data["kind"],
        data["outcome"],
        data["query_identity"],
        data["scope"],
        data["constraint_fingerprint"],
        data["statement_id"],
        data["generation"],
        data["generation_available"],
        data["policy_fingerprint"],
        data["observed_at"],
        data["reason"],
        data["contract_fingerprint"],
        data["schema_version"],
    )
    return result


def feedback_observation_with_changes(value: object, changes: object) -> FeedbackObservation:
    current = validate_feedback_observation(value)
    if not isinstance(changes, Mapping) or not set(changes).issubset(FEEDBACK_OBSERVATION_FIELDS):
        raise InvalidRequestError("feedback observation changes contain invalid fields")
    updated: dict[str, object] = dict(current)
    updated.update(changes)
    result = validate_feedback_observation(updated)
    return result


def feedback_observation_statement_key(value: object) -> StatementFeedbackKey:
    current = validate_feedback_observation(value)
    result = _trusted_feedback_observation_statement_key(current)
    return result


def _trusted_feedback_observation_statement_key(current: FeedbackObservation) -> StatementFeedbackKey:
    """Derive a statement key from an observation validated by the store boundary."""
    result: StatementFeedbackKey = {
        "statement_id": current["statement_id"],
        "generation": current["generation"],
        "generation_available": current["generation_available"],
        "policy_fingerprint": current["policy_fingerprint"],
        "contract_fingerprint": current["contract_fingerprint"],
        "schema_version": FEEDBACK_KEY_SCHEMA_VERSION,
    }
    return result


def feedback_observation_relationship_key(value: object) -> RelationshipFeedbackKey:
    current = validate_feedback_observation(value)
    statement = _trusted_feedback_observation_statement_key(current)
    result = _trusted_feedback_observation_relationship_key(current, statement)
    return result


def _trusted_feedback_observation_relationship_key(
    current: FeedbackObservation,
    statement: StatementFeedbackKey,
) -> RelationshipFeedbackKey:
    """Derive a relationship key from one store-owned observation and statement key."""
    result: RelationshipFeedbackKey = {
        "query_identity": current["query_identity"],
        "scope": current["scope"],
        "constraint_fingerprint": current["constraint_fingerprint"],
        "statement": statement,
        "schema_version": FEEDBACK_KEY_SCHEMA_VERSION,
    }
    return result


def feedback_observation_to_dict(value: object) -> dict[str, object]:
    current = validate_feedback_observation(value)
    result = _trusted_feedback_observation_to_dict(current)
    return result


def _trusted_feedback_observation_to_dict(current: FeedbackObservation) -> dict[str, object]:
    """Serialize an observation already validated by the feedback-store boundary."""
    result = {
        "schema_version": current["schema_version"],
        "reference_kind": current["reference_kind"].value,
        "reference_id": current["reference_id"],
        "kind": current["kind"].value,
        "outcome": current["outcome"].value,
        "query_identity": query_identity_to_dict(current["query_identity"]),
        "scope": scope_key_to_dict(current["scope"]),
        "constraint_fingerprint": current["constraint_fingerprint"],
        "statement_id": current["statement_id"],
        "generation": current["generation"],
        "generation_available": current["generation_available"],
        "policy_fingerprint": current["policy_fingerprint"],
        "contract_fingerprint": current["contract_fingerprint"],
        "observed_at": current["observed_at"],
        "reason": current["reason"],
    }
    return result


def feedback_observation_to_json(value: object) -> str:
    data = feedback_observation_to_dict(value)
    result = _json_text(data)
    return result


def feedback_observation_from_dict(value: object) -> FeedbackObservation:
    data = _exact_mapping(value, "FeedbackObservation", FEEDBACK_OBSERVATION_FIELDS)
    if not isinstance(data["query_identity"], Mapping) or not isinstance(data["scope"], Mapping):
        raise InvalidRequestError("feedback observation identity and scope must be objects")
    try:
        reference_kind = FeedbackReferenceKind(data["reference_kind"])
        kind = FeedbackObservationKind(data["kind"])
        outcome = FeedbackOutcome(data["outcome"])
    except (TypeError, ValueError) as error:
        raise InvalidRequestError("feedback observation contains an unsupported enum value") from error
    result = feedback_observation(
        schema_version=_integer(data["schema_version"], "feedback observation schema_version", 0),
        reference_kind=reference_kind,
        reference_id=_text(data["reference_id"], "feedback reference_id", MAX_REFERENCE_ID_BYTES),
        kind=kind,
        outcome=outcome,
        query_identity=query_identity_from_dict(data["query_identity"]),
        scope=scope_key_from_dict(data["scope"]),
        constraint_fingerprint=_fingerprint(data["constraint_fingerprint"], "feedback constraint_fingerprint"),
        statement_id=_text(data["statement_id"], "feedback statement_id", MAX_STATEMENT_ID_BYTES),
        generation=_integer(data["generation"], "feedback generation", 0),
        generation_available=_boolean(data["generation_available"], "feedback generation_available"),
        policy_fingerprint=_fingerprint(data["policy_fingerprint"], "feedback policy_fingerprint"),
        contract_fingerprint=_fingerprint(data["contract_fingerprint"], "feedback contract_fingerprint"),
        observed_at=canonical_utc(_parse_timestamp(data["observed_at"], "feedback observed_at")),
        reason=_text(data["reason"], "feedback reason", MAX_FEEDBACK_REASON_BYTES, allow_empty=True),
    )
    return result


def feedback_observation_from_json(value: str) -> FeedbackObservation:
    data = _load_json_mapping(value, "FeedbackObservation JSON")
    result = feedback_observation_from_dict(data)
    return result


FeedbackStatistics = dict


def feedback_statistics(
    candidate_count: object = 0,
    accept_count: object = 0,
    rejected_quality: object = 0,
    rejected_context: object = 0,
    rejected_stale: object = 0,
    rejected_policy: object = 0,
    schema_version: object = FEEDBACK_STATISTICS_SCHEMA_VERSION,
) -> FeedbackStatistics:
    """Build inspectable raw aggregate feedback counters."""
    version = _integer(schema_version, "feedback statistics schema_version", 0)
    if version != FEEDBACK_STATISTICS_SCHEMA_VERSION:
        raise InvalidRequestError(f"unsupported feedback statistics schema_version: {version}")
    values = {
        "candidate_count": candidate_count,
        "accept_count": accept_count,
        "rejected_quality": rejected_quality,
        "rejected_context": rejected_context,
        "rejected_stale": rejected_stale,
        "rejected_policy": rejected_policy,
    }
    validated = {name: _integer(value, f"feedback statistics {name}", 0) for name, value in values.items()}
    result: FeedbackStatistics = {
        "candidate_count": validated["candidate_count"],
        "accept_count": validated["accept_count"],
        "rejected_quality": validated["rejected_quality"],
        "rejected_context": validated["rejected_context"],
        "rejected_stale": validated["rejected_stale"],
        "rejected_policy": validated["rejected_policy"],
        "schema_version": version,
    }
    return result


def validate_feedback_statistics(value: object) -> FeedbackStatistics:
    data = _exact_mapping(value, "FeedbackStatistics", FEEDBACK_STATISTICS_FIELDS)
    result = feedback_statistics(
        data["candidate_count"],
        data["accept_count"],
        data["rejected_quality"],
        data["rejected_context"],
        data["rejected_stale"],
        data["rejected_policy"],
        data["schema_version"],
    )
    return result


def feedback_statistics_verdict_count(value: object) -> int:
    current = validate_feedback_statistics(value)
    result = (
        current["accept_count"]
        + current["rejected_quality"]
        + current["rejected_context"]
        + current["rejected_stale"]
        + current["rejected_policy"]
    )
    return result


def feedback_statistics_increment(value: object, outcome: object) -> FeedbackStatistics:
    current = validate_feedback_statistics(value)
    if not isinstance(outcome, FeedbackOutcome):
        raise InvalidRequestError("feedback increment outcome must be a FeedbackOutcome")
    updated: dict[str, object] = dict(current)
    field_name = FEEDBACK_OUTCOME_COUNTER_FIELDS[outcome]
    updated[field_name] = current[field_name] + 1
    result = validate_feedback_statistics(updated)
    return result


def feedback_statistics_add(first: object, second: object) -> FeedbackStatistics:
    try:
        first_value = validate_feedback_statistics(first)
        second_value = validate_feedback_statistics(second)
    except InvalidRequestError as error:
        raise InvalidRequestError("feedback statistics can only add FeedbackStatistics") from error
    result = feedback_statistics(
        candidate_count=first_value["candidate_count"] + second_value["candidate_count"],
        accept_count=first_value["accept_count"] + second_value["accept_count"],
        rejected_quality=first_value["rejected_quality"] + second_value["rejected_quality"],
        rejected_context=first_value["rejected_context"] + second_value["rejected_context"],
        rejected_stale=first_value["rejected_stale"] + second_value["rejected_stale"],
        rejected_policy=first_value["rejected_policy"] + second_value["rejected_policy"],
    )
    return result


def feedback_statistics_to_dict(value: object) -> dict[str, int]:
    current = validate_feedback_statistics(value)
    result = {
        "schema_version": current["schema_version"],
        "candidate_count": current["candidate_count"],
        "accept_count": current["accept_count"],
        "rejected_quality": current["rejected_quality"],
        "rejected_context": current["rejected_context"],
        "rejected_stale": current["rejected_stale"],
        "rejected_policy": current["rejected_policy"],
    }
    return result


def feedback_statistics_to_json(value: object) -> str:
    data = feedback_statistics_to_dict(value)
    result = _json_text(data)
    return result


def feedback_statistics_from_dict(value: object) -> FeedbackStatistics:
    data = _exact_mapping(value, "FeedbackStatistics", FEEDBACK_STATISTICS_FIELDS)
    values = {name: _integer(data[name], f"feedback statistics {name}", 0) for name in FEEDBACK_STATISTICS_FIELDS}
    result = feedback_statistics(**values)
    return result


def feedback_statistics_from_json(value: str) -> FeedbackStatistics:
    data = _load_json_mapping(value, "FeedbackStatistics JSON")
    result = feedback_statistics_from_dict(data)
    return result


FeedbackBucket = dict


def feedback_bucket(
    start_at: object,
    statistics: object,
    schema_version: object = FEEDBACK_BUCKET_SCHEMA_VERSION,
) -> FeedbackBucket:
    """Build one bounded deterministic time bucket of raw counters."""
    version = _integer(schema_version, "feedback bucket schema_version", 0)
    if version != FEEDBACK_BUCKET_SCHEMA_VERSION:
        raise InvalidRequestError(f"unsupported feedback bucket schema_version: {version}")
    validated_start_at = canonical_utc(_parse_timestamp(start_at, "feedback bucket start_at"))
    try:
        validated_statistics = validate_feedback_statistics(statistics)
    except InvalidRequestError as error:
        raise InvalidRequestError("feedback bucket statistics must be FeedbackStatistics") from error
    result: FeedbackBucket = {
        "start_at": validated_start_at,
        "statistics": validated_statistics,
        "schema_version": version,
    }
    return result


def validate_feedback_bucket(value: object) -> FeedbackBucket:
    data = _exact_mapping(value, "FeedbackBucket", FEEDBACK_BUCKET_FIELDS)
    result = feedback_bucket(data["start_at"], data["statistics"], data["schema_version"])
    return result


def feedback_bucket_to_dict(value: object) -> dict[str, object]:
    current = validate_feedback_bucket(value)
    result = {
        "schema_version": current["schema_version"],
        "start_at": current["start_at"],
        "statistics": feedback_statistics_to_dict(current["statistics"]),
    }
    return result


def feedback_bucket_from_dict(value: object) -> FeedbackBucket:
    data = _exact_mapping(value, "FeedbackBucket", FEEDBACK_BUCKET_FIELDS)
    if not isinstance(data["statistics"], Mapping):
        raise InvalidRequestError("feedback bucket statistics must be an object")
    result = feedback_bucket(
        schema_version=_integer(data["schema_version"], "feedback bucket schema_version", 0),
        start_at=canonical_utc(_parse_timestamp(data["start_at"], "feedback bucket start_at")),
        statistics=feedback_statistics_from_dict(data["statistics"]),
    )
    return result


def _apply_buckets(
    buckets: tuple[FeedbackBucket, ...],
    outcome: FeedbackOutcome,
    observed_at: str,
    policy: FeedbackPolicy,
) -> tuple[FeedbackBucket, ...]:
    start = _bucket_start(observed_at, policy["bucket_seconds"])
    values = {bucket["start_at"]: bucket for bucket in buckets}
    current = values.get(start, feedback_bucket(start, feedback_statistics()))
    updated_statistics = feedback_statistics_increment(current["statistics"], outcome)
    values[start] = feedback_bucket(start, updated_statistics)
    ordered = tuple(values[key] for key in sorted(values))
    result = ordered[-policy["max_buckets_per_record"] :]
    return result


StatementFeedbackRecord = dict


def statement_feedback_record(
    key: object,
    raw: object,
    buckets: object,
    last_outcome: object,
    last_observed_at: object,
    schema_version: object = FEEDBACK_RECORD_SCHEMA_VERSION,
) -> StatementFeedbackRecord:
    version = _integer(schema_version, "statement feedback record schema_version", 0)
    if version != FEEDBACK_RECORD_SCHEMA_VERSION:
        raise InvalidRequestError(f"unsupported statement feedback record schema_version: {version}")
    try:
        validated_key = validate_statement_feedback_key(key)
        validated_raw = validate_feedback_statistics(raw)
    except InvalidRequestError as error:
        raise InvalidRequestError("statement feedback record key and raw statistics have invalid types") from error
    if not isinstance(buckets, tuple):
        raise InvalidRequestError("statement feedback buckets must be a tuple of FeedbackBucket values")
    try:
        validated_buckets = tuple(validate_feedback_bucket(bucket) for bucket in buckets)
    except InvalidRequestError as error:
        raise InvalidRequestError("statement feedback buckets must be a tuple of FeedbackBucket values") from error
    bucket_starts = tuple(bucket["start_at"] for bucket in validated_buckets)
    if len(validated_buckets) > MAX_FEEDBACK_BUCKETS or tuple(sorted(bucket_starts)) != bucket_starts:
        raise InvalidRequestError("statement feedback buckets must be bounded and ordered")
    if len(set(bucket_starts)) != len(bucket_starts):
        raise InvalidRequestError("statement feedback bucket starts must be unique")
    if not isinstance(last_outcome, FeedbackOutcome):
        raise InvalidRequestError("statement feedback last_outcome must be a FeedbackOutcome")
    validated_last_observed_at = canonical_utc(_parse_timestamp(last_observed_at, "statement feedback last_observed_at"))
    result: StatementFeedbackRecord = {
        "key": validated_key,
        "raw": validated_raw,
        "buckets": validated_buckets,
        "last_outcome": last_outcome,
        "last_observed_at": validated_last_observed_at,
        "schema_version": version,
    }
    return result


def validate_statement_feedback_record(value: object) -> StatementFeedbackRecord:
    data = _exact_mapping(value, "StatementFeedbackRecord", FEEDBACK_RECORD_FIELDS)
    result = statement_feedback_record(
        data["key"],
        data["raw"],
        data["buckets"],
        data["last_outcome"],
        data["last_observed_at"],
        data["schema_version"],
    )
    return result


def statement_feedback_record_apply(value: object, observation: object, policy: object) -> StatementFeedbackRecord:
    current = validate_statement_feedback_record(value)
    validated_observation = validate_feedback_observation(observation)
    validated_policy = validate_feedback_policy(policy)
    result = _trusted_statement_feedback_record_apply(current, validated_observation, validated_policy)
    return result


def _trusted_statement_feedback_record_apply(
    current: StatementFeedbackRecord,
    validated_observation: FeedbackObservation,
    validated_policy: FeedbackPolicy,
) -> StatementFeedbackRecord:
    """Apply one observation after the store boundary validated every input."""
    observation_key = _trusted_feedback_observation_statement_key(validated_observation)
    if observation_key != current["key"]:
        raise ConflictError("feedback observation does not match statement aggregate key")
    last_observed_at, last_outcome = max(
        (current["last_observed_at"], current["last_outcome"]),
        (validated_observation["observed_at"], validated_observation["outcome"]),
        key=lambda item: (item[0], item[1].value),
    )
    raw = feedback_statistics_increment(current["raw"], validated_observation["outcome"])
    buckets = _apply_buckets(
        current["buckets"], validated_observation["outcome"], validated_observation["observed_at"], validated_policy
    )
    result = _trusted_statement_feedback_record(current["key"], raw, buckets, last_outcome, last_observed_at)
    return result


def _trusted_statement_feedback_record(
    key: StatementFeedbackKey,
    raw: FeedbackStatistics,
    buckets: tuple[FeedbackBucket, ...],
    last_outcome: FeedbackOutcome,
    last_observed_at: str,
) -> StatementFeedbackRecord:
    """Build a statement aggregate from store-owned validated components."""
    result: StatementFeedbackRecord = {
        "key": key,
        "raw": raw,
        "buckets": buckets,
        "last_outcome": last_outcome,
        "last_observed_at": last_observed_at,
        "schema_version": FEEDBACK_RECORD_SCHEMA_VERSION,
    }
    return result


def statement_feedback_record_to_dict(value: object) -> dict[str, object]:
    current = validate_statement_feedback_record(value)
    result = {
        "schema_version": current["schema_version"],
        "key": statement_feedback_key_to_dict(current["key"]),
        "raw": feedback_statistics_to_dict(current["raw"]),
        "buckets": [feedback_bucket_to_dict(bucket) for bucket in current["buckets"]],
        "last_outcome": current["last_outcome"].value,
        "last_observed_at": current["last_observed_at"],
    }
    return result


def statement_feedback_record_from_dict(value: object) -> StatementFeedbackRecord:
    data = _exact_mapping(value, "StatementFeedbackRecord", FEEDBACK_RECORD_FIELDS)
    if not isinstance(data["key"], Mapping) or not isinstance(data["raw"], Mapping):
        raise InvalidRequestError("statement feedback key and raw values must be objects")
    if not isinstance(data["buckets"], list) or not all(isinstance(item, Mapping) for item in data["buckets"]):
        raise InvalidRequestError("statement feedback buckets must be an array of objects")
    try:
        last_outcome = FeedbackOutcome(data["last_outcome"])
    except (TypeError, ValueError) as error:
        raise InvalidRequestError("statement feedback contains an unsupported last_outcome") from error
    version = _integer(data["schema_version"], "statement feedback record schema_version", 0)
    if version != FEEDBACK_RECORD_SCHEMA_VERSION:
        raise InvalidRequestError(f"unsupported statement feedback record schema_version: {version}")
    buckets = tuple(feedback_bucket_from_dict(item) for item in data["buckets"])
    bucket_starts = tuple(bucket["start_at"] for bucket in buckets)
    if len(buckets) > MAX_FEEDBACK_BUCKETS or tuple(sorted(bucket_starts)) != bucket_starts:
        raise InvalidRequestError("statement feedback buckets must be bounded and ordered")
    if len(set(bucket_starts)) != len(bucket_starts):
        raise InvalidRequestError("statement feedback bucket starts must be unique")
    result: StatementFeedbackRecord = {
        "key": statement_feedback_key_from_dict(data["key"]),
        "raw": feedback_statistics_from_dict(data["raw"]),
        "buckets": buckets,
        "last_outcome": last_outcome,
        "last_observed_at": canonical_utc(_parse_timestamp(data["last_observed_at"], "statement feedback last_observed_at")),
        "schema_version": version,
    }
    return result


RelationshipFeedbackRecord = dict


def relationship_feedback_record(
    key: object,
    raw: object,
    buckets: object,
    last_outcome: object,
    last_observed_at: object,
    schema_version: object = FEEDBACK_RECORD_SCHEMA_VERSION,
) -> RelationshipFeedbackRecord:
    version = _integer(schema_version, "relationship feedback record schema_version", 0)
    if version != FEEDBACK_RECORD_SCHEMA_VERSION:
        raise InvalidRequestError(f"unsupported relationship feedback record schema_version: {version}")
    try:
        validated_key = validate_relationship_feedback_key(key)
        validated_raw = validate_feedback_statistics(raw)
    except InvalidRequestError as error:
        raise InvalidRequestError("relationship feedback record key and raw statistics have invalid types") from error
    if not isinstance(buckets, tuple):
        raise InvalidRequestError("relationship feedback buckets must be a tuple of FeedbackBucket values")
    try:
        validated_buckets = tuple(validate_feedback_bucket(bucket) for bucket in buckets)
    except InvalidRequestError as error:
        raise InvalidRequestError("relationship feedback buckets must be a tuple of FeedbackBucket values") from error
    bucket_starts = tuple(bucket["start_at"] for bucket in validated_buckets)
    if len(validated_buckets) > MAX_FEEDBACK_BUCKETS or tuple(sorted(bucket_starts)) != bucket_starts:
        raise InvalidRequestError("relationship feedback buckets must be bounded and ordered")
    if len(set(bucket_starts)) != len(bucket_starts):
        raise InvalidRequestError("relationship feedback bucket starts must be unique")
    if not isinstance(last_outcome, FeedbackOutcome):
        raise InvalidRequestError("relationship feedback last_outcome must be a FeedbackOutcome")
    validated_last_observed_at = canonical_utc(_parse_timestamp(last_observed_at, "relationship feedback last_observed_at"))
    result: RelationshipFeedbackRecord = {
        "key": validated_key,
        "raw": validated_raw,
        "buckets": validated_buckets,
        "last_outcome": last_outcome,
        "last_observed_at": validated_last_observed_at,
        "schema_version": version,
    }
    return result


def validate_relationship_feedback_record(value: object) -> RelationshipFeedbackRecord:
    data = _exact_mapping(value, "RelationshipFeedbackRecord", FEEDBACK_RECORD_FIELDS)
    result = relationship_feedback_record(
        data["key"],
        data["raw"],
        data["buckets"],
        data["last_outcome"],
        data["last_observed_at"],
        data["schema_version"],
    )
    return result


def relationship_feedback_record_apply(value: object, observation: object, policy: object) -> RelationshipFeedbackRecord:
    current = validate_relationship_feedback_record(value)
    validated_observation = validate_feedback_observation(observation)
    validated_policy = validate_feedback_policy(policy)
    result = _trusted_relationship_feedback_record_apply(current, validated_observation, validated_policy)
    return result


def _trusted_relationship_feedback_record_apply(
    current: RelationshipFeedbackRecord,
    validated_observation: FeedbackObservation,
    validated_policy: FeedbackPolicy,
) -> RelationshipFeedbackRecord:
    """Apply one relationship observation after validating the store boundary."""
    statement_key = _trusted_feedback_observation_statement_key(validated_observation)
    observation_key = _trusted_feedback_observation_relationship_key(validated_observation, statement_key)
    if observation_key != current["key"]:
        raise ConflictError("feedback observation does not match relationship aggregate key")
    last_observed_at, last_outcome = max(
        (current["last_observed_at"], current["last_outcome"]),
        (validated_observation["observed_at"], validated_observation["outcome"]),
        key=lambda item: (item[0], item[1].value),
    )
    raw = feedback_statistics_increment(current["raw"], validated_observation["outcome"])
    buckets = _apply_buckets(
        current["buckets"], validated_observation["outcome"], validated_observation["observed_at"], validated_policy
    )
    result = _trusted_relationship_feedback_record(current["key"], raw, buckets, last_outcome, last_observed_at)
    return result


def _trusted_relationship_feedback_record(
    key: RelationshipFeedbackKey,
    raw: FeedbackStatistics,
    buckets: tuple[FeedbackBucket, ...],
    last_outcome: FeedbackOutcome,
    last_observed_at: str,
) -> RelationshipFeedbackRecord:
    """Build a relationship aggregate from store-owned validated components."""
    result: RelationshipFeedbackRecord = {
        "key": key,
        "raw": raw,
        "buckets": buckets,
        "last_outcome": last_outcome,
        "last_observed_at": last_observed_at,
        "schema_version": FEEDBACK_RECORD_SCHEMA_VERSION,
    }
    return result


def relationship_feedback_record_to_dict(value: object) -> dict[str, object]:
    current = validate_relationship_feedback_record(value)
    result = {
        "schema_version": current["schema_version"],
        "key": relationship_feedback_key_to_dict(current["key"]),
        "raw": feedback_statistics_to_dict(current["raw"]),
        "buckets": [feedback_bucket_to_dict(bucket) for bucket in current["buckets"]],
        "last_outcome": current["last_outcome"].value,
        "last_observed_at": current["last_observed_at"],
    }
    return result


def relationship_feedback_record_from_dict(
    value: object,
    identity_cache: object = (),
    scope_cache: object = (),
    statement_cache: object = (),
) -> RelationshipFeedbackRecord:
    data = _exact_mapping(value, "RelationshipFeedbackRecord", FEEDBACK_RECORD_FIELDS)
    if not isinstance(data["key"], Mapping) or not isinstance(data["raw"], Mapping):
        raise InvalidRequestError("relationship feedback key and raw values must be objects")
    if not isinstance(data["buckets"], list) or not all(isinstance(item, Mapping) for item in data["buckets"]):
        raise InvalidRequestError("relationship feedback buckets must be an array of objects")
    try:
        last_outcome = FeedbackOutcome(data["last_outcome"])
    except (TypeError, ValueError) as error:
        raise InvalidRequestError("relationship feedback contains an unsupported last_outcome") from error
    version = _integer(data["schema_version"], "relationship feedback record schema_version", 0)
    if version != FEEDBACK_RECORD_SCHEMA_VERSION:
        raise InvalidRequestError(f"unsupported relationship feedback record schema_version: {version}")
    buckets = tuple(feedback_bucket_from_dict(item) for item in data["buckets"])
    bucket_starts = tuple(bucket["start_at"] for bucket in buckets)
    if len(buckets) > MAX_FEEDBACK_BUCKETS or tuple(sorted(bucket_starts)) != bucket_starts:
        raise InvalidRequestError("relationship feedback buckets must be bounded and ordered")
    if len(set(bucket_starts)) != len(bucket_starts):
        raise InvalidRequestError("relationship feedback bucket starts must be unique")
    result: RelationshipFeedbackRecord = {
        "key": relationship_feedback_key_from_dict(data["key"], identity_cache, scope_cache, statement_cache),
        "raw": feedback_statistics_from_dict(data["raw"]),
        "buckets": buckets,
        "last_outcome": last_outcome,
        "last_observed_at": canonical_utc(_parse_timestamp(data["last_observed_at"], "relationship feedback last_observed_at")),
        "schema_version": version,
    }
    return result


PolicySuppression = dict


def policy_suppression(
    statement_id: object, namespace: object, policy_fingerprint: object, observed_at: object
) -> PolicySuppression:
    validated_statement_id = _text(statement_id, "policy suppression statement_id", MAX_STATEMENT_ID_BYTES)
    validated_namespace = _text(namespace, "policy suppression namespace", MAX_NAMESPACE_BYTES, allow_empty=True)
    validated_policy = _fingerprint(policy_fingerprint, "policy suppression policy_fingerprint")
    validated_observed_at = canonical_utc(_parse_timestamp(observed_at, "policy suppression observed_at"))
    result: PolicySuppression = {
        "statement_id": validated_statement_id,
        "namespace": validated_namespace,
        "policy_fingerprint": validated_policy,
        "observed_at": validated_observed_at,
    }
    return result


def validate_policy_suppression(value: object) -> PolicySuppression:
    data = _exact_mapping(value, "PolicySuppression", POLICY_SUPPRESSION_FIELDS)
    result = policy_suppression(data["statement_id"], data["namespace"], data["policy_fingerprint"], data["observed_at"])
    return result


def policy_suppression_signature(value: object) -> tuple[str, str, str, str]:
    current = validate_policy_suppression(value)
    result = (current["statement_id"], current["namespace"], current["policy_fingerprint"], current["observed_at"])
    return result


def policy_suppression_to_dict(value: object) -> dict[str, object]:
    current = validate_policy_suppression(value)
    result = dict(current)
    return result


def policy_suppression_from_dict(value: object) -> PolicySuppression:
    result = validate_policy_suppression(value)
    return result


StaleExclusion = dict


def stale_exclusion(statement_id: object, generation: object, generation_available: object, observed_at: object) -> StaleExclusion:
    key = statement_feedback_key(
        statement_id,
        generation,
        generation_available,
        EMPTY_FINGERPRINT,
        FEEDBACK_CONTRACT_FINGERPRINT,
    )
    validated_observed_at = canonical_utc(_parse_timestamp(observed_at, "stale exclusion observed_at"))
    result: StaleExclusion = {
        "statement_id": key["statement_id"],
        "generation": key["generation"],
        "generation_available": key["generation_available"],
        "observed_at": validated_observed_at,
    }
    return result


def validate_stale_exclusion(value: object) -> StaleExclusion:
    data = _exact_mapping(value, "StaleExclusion", STALE_EXCLUSION_FIELDS)
    result = stale_exclusion(data["statement_id"], data["generation"], data["generation_available"], data["observed_at"])
    return result


def stale_exclusion_signature(value: object) -> tuple[str, int, bool, str]:
    current = validate_stale_exclusion(value)
    result = (current["statement_id"], current["generation"], current["generation_available"], current["observed_at"])
    return result


def stale_exclusion_to_dict(value: object) -> dict[str, object]:
    current = validate_stale_exclusion(value)
    result = dict(current)
    return result


def stale_exclusion_from_dict(value: object) -> StaleExclusion:
    result = validate_stale_exclusion(value)
    return result


FeedbackHistory = dict


def feedback_history(
    value: object = 0.0,
    available: object = False,
    statement_value: object = 0.0,
    statement_available: object = False,
    relationship_value: object = 0.0,
    relationship_available: object = False,
    statement_samples: object = 0.0,
    relationship_samples: object = 0.0,
    policy_fingerprint: object = EMPTY_FINGERPRINT,
    feedback_policy_value: object = "",
    schema_version: object = FEEDBACK_HISTORY_SCHEMA_VERSION,
) -> FeedbackHistory:
    version = _integer(schema_version, "feedback history schema_version", 1)
    if version != FEEDBACK_HISTORY_SCHEMA_VERSION:
        raise InvalidRequestError(f"unsupported feedback history schema_version: {version}")
    validated_value = _number(value, "feedback history value", 0.0, 1.0)
    validated_available = _boolean(available, "feedback history available")
    validated_statement_value = _number(statement_value, "feedback history statement_value", 0.0, 1.0)
    validated_statement_available = _boolean(statement_available, "feedback history statement_available")
    validated_relationship_value = _number(relationship_value, "feedback history relationship_value", 0.0, 1.0)
    validated_relationship_available = _boolean(relationship_available, "feedback history relationship_available")
    validated_statement_samples = _number(statement_samples, "feedback history statement_samples", 0.0, 1.0e18)
    validated_relationship_samples = _number(relationship_samples, "feedback history relationship_samples", 0.0, 1.0e18)
    validated_policy = _fingerprint(policy_fingerprint, "feedback history policy_fingerprint")
    if feedback_policy_value == "":
        feedback_policy_value = feedback_policy_fingerprint()
    validated_feedback_policy = _fingerprint(feedback_policy_value, "feedback history feedback_policy_fingerprint")
    if not validated_available and validated_value != 0.0:
        raise InvalidRequestError("unavailable feedback history must use zero value")
    result: FeedbackHistory = {
        "schema_version": version,
        "value": validated_value,
        "available": validated_available,
        "statement_value": validated_statement_value,
        "statement_available": validated_statement_available,
        "relationship_value": validated_relationship_value,
        "relationship_available": validated_relationship_available,
        "statement_samples": validated_statement_samples,
        "relationship_samples": validated_relationship_samples,
        "policy_fingerprint": validated_policy,
        "feedback_policy_fingerprint": validated_feedback_policy,
    }
    return result


def validate_feedback_history(value: object) -> FeedbackHistory:
    data = _exact_mapping(value, "FeedbackHistory", FEEDBACK_HISTORY_FIELDS)
    result = feedback_history(
        value=data["value"],
        available=data["available"],
        statement_value=data["statement_value"],
        statement_available=data["statement_available"],
        relationship_value=data["relationship_value"],
        relationship_available=data["relationship_available"],
        statement_samples=data["statement_samples"],
        relationship_samples=data["relationship_samples"],
        policy_fingerprint=data["policy_fingerprint"],
        feedback_policy_value=data["feedback_policy_fingerprint"],
        schema_version=data["schema_version"],
    )
    return result


def feedback_history_to_dict(value: object) -> dict[str, object]:
    current = validate_feedback_history(value)
    result = dict(current)
    return result


def feedback_history_to_json(value: object) -> str:
    data = feedback_history_to_dict(value)
    result = _json_text(data)
    return result


def feedback_history_from_dict(value: object) -> FeedbackHistory:
    result = validate_feedback_history(value)
    return result


def feedback_history_from_json(value: str) -> FeedbackHistory:
    data = _load_json_mapping(value, "FeedbackHistory JSON")
    result = feedback_history_from_dict(data)
    return result


FeedbackState = dict


def _freeze_feedback_value(value: object) -> object:
    """Recursively freeze one validated feedback contract for structural sharing."""
    value_type = type(value)
    if value_type in (dict, MappingProxyType):
        mapping = value
        result: object = MappingProxyType({key: _freeze_feedback_value(nested) for key, nested in mapping.items()})
    elif value_type is tuple or value_type is list:
        result = tuple(_freeze_feedback_value(nested) for nested in value)
    else:
        result = value
    return result


def _trusted_feedback_state(
    policy: FeedbackPolicy,
    statement_records: tuple[StatementFeedbackRecord, ...],
    relationship_records: tuple[RelationshipFeedbackRecord, ...],
    policy_suppressions: tuple[PolicySuppression, ...],
    stale_exclusions: tuple[StaleExclusion, ...],
    receipts: dict[str, object],
    statement_evictions: int,
    relationship_evictions: int,
    policy_suppression_evictions: int,
    stale_exclusion_evictions: int,
) -> FeedbackState:
    """Assemble state from values owned and maintained by FeedbackStore."""
    result: FeedbackState = {
        "policy": policy,
        "statement_records": statement_records,
        "relationship_records": relationship_records,
        "policy_suppressions": policy_suppressions,
        "stale_exclusions": stale_exclusions,
        "receipts": receipts,
        "statement_evictions": statement_evictions,
        "relationship_evictions": relationship_evictions,
        "policy_suppression_evictions": policy_suppression_evictions,
        "stale_exclusion_evictions": stale_exclusion_evictions,
        "schema_version": FEEDBACK_STATE_SCHEMA_VERSION,
    }
    return result


def _trusted_feedback_state_copy(value: FeedbackState) -> FeedbackState:
    """Defensively copy state whose invariants are already established."""
    receipts = json.loads(_json_text(value["receipts"]))
    if not isinstance(receipts, dict):
        raise InvalidRequestError("feedback receipts must decode to an object")
    result = _trusted_feedback_state(
        value["policy"],
        value["statement_records"],
        value["relationship_records"],
        value["policy_suppressions"],
        value["stale_exclusions"],
        receipts,
        value["statement_evictions"],
        value["relationship_evictions"],
        value["policy_suppression_evictions"],
        value["stale_exclusion_evictions"],
    )
    return result


def _feedback_wire_value(value: object) -> object:
    """Project an already validated feedback value into deterministic JSON types."""
    value_type = type(value)
    if value_type in (dict, MappingProxyType):
        mapping = value
        result: object = {key: _feedback_wire_value(nested) for key, nested in mapping.items()}
    elif value_type is tuple or value_type is list:
        result = [_feedback_wire_value(nested) for nested in value]
    elif isinstance(value, Enum):
        result = value.value
    else:
        result = value
    return result


def _trusted_feedback_key_fingerprint(value: Mapping[str, object]) -> str:
    """Fingerprint a key already validated by its record decoder or store."""
    result = canonical_fingerprint(_feedback_wire_value(value))
    return result


def _feedback_state_from_validated_components(
    policy: FeedbackPolicy,
    statement_records: tuple[StatementFeedbackRecord, ...],
    relationship_records: tuple[RelationshipFeedbackRecord, ...],
    policy_suppressions: tuple[PolicySuppression, ...],
    stale_exclusions: tuple[StaleExclusion, ...],
    receipts: Mapping[str, object],
    statement_evictions: object,
    relationship_evictions: object,
    policy_suppression_evictions: object,
    stale_exclusion_evictions: object,
    schema_version: int,
) -> FeedbackState:
    """Enforce state-wide invariants after every nested value was validated once."""
    if schema_version != FEEDBACK_STATE_SCHEMA_VERSION:
        raise InvalidRequestError(f"unsupported feedback state schema_version: {schema_version}")
    if len(statement_records) > policy["max_statement_records"]:
        raise InvalidRequestError("feedback statement records exceed policy capacity")
    if len(relationship_records) > policy["max_relationship_records"]:
        raise InvalidRequestError("feedback relationship records exceed policy capacity")
    statement_fingerprints = tuple(_trusted_feedback_key_fingerprint(record["key"]) for record in statement_records)
    relationship_fingerprints = tuple(_trusted_feedback_key_fingerprint(record["key"]) for record in relationship_records)
    if len(set(statement_fingerprints)) != len(statement_fingerprints):
        raise InvalidRequestError("feedback statement record keys must be unique")
    if len(set(relationship_fingerprints)) != len(relationship_fingerprints):
        raise InvalidRequestError("feedback relationship record keys must be unique")
    if statement_fingerprints != tuple(sorted(statement_fingerprints)):
        raise InvalidRequestError("feedback statement records must use canonical key order")
    if relationship_fingerprints != tuple(sorted(relationship_fingerprints)):
        raise InvalidRequestError("feedback relationship records must use canonical key order")
    maximum_buckets = policy["max_buckets_per_record"]
    if any(len(record["buckets"]) > maximum_buckets for record in statement_records):
        raise InvalidRequestError("feedback statement record buckets exceed policy retention")
    if any(len(record["buckets"]) > maximum_buckets for record in relationship_records):
        raise InvalidRequestError("feedback relationship record buckets exceed policy retention")
    if len(policy_suppressions) > policy["max_statement_records"]:
        raise InvalidRequestError("feedback policy suppressions exceed policy capacity")
    if len(stale_exclusions) > policy["max_statement_records"]:
        raise InvalidRequestError("feedback stale exclusions exceed policy capacity")
    suppression_keys = tuple(
        (value["statement_id"], value["namespace"], value["policy_fingerprint"]) for value in policy_suppressions
    )
    exclusion_keys = tuple(
        (value["statement_id"], value["generation"], value["generation_available"]) for value in stale_exclusions
    )
    if len(set(suppression_keys)) != len(suppression_keys):
        raise InvalidRequestError("feedback policy suppressions must be unique")
    if len(set(exclusion_keys)) != len(exclusion_keys):
        raise InvalidRequestError("feedback stale exclusions must be unique")
    if policy_suppressions != tuple(sorted(policy_suppressions, key=policy_suppression_signature)):
        raise InvalidRequestError("feedback policy suppressions must use canonical order")
    if stale_exclusions != tuple(sorted(stale_exclusions, key=stale_exclusion_signature)):
        raise InvalidRequestError("feedback stale exclusions must use canonical order")
    mutation_receipt_ledger_from_snapshot(receipts)
    receipt_copy = json.loads(_json_text(receipts))
    if not isinstance(receipt_copy, dict):
        raise InvalidRequestError("feedback receipts must decode to an object")
    result = _trusted_feedback_state(
        policy,
        statement_records,
        relationship_records,
        policy_suppressions,
        stale_exclusions,
        receipt_copy,
        _integer(statement_evictions, "feedback statement_evictions", 0),
        _integer(relationship_evictions, "feedback relationship_evictions", 0),
        _integer(policy_suppression_evictions, "feedback policy_suppression_evictions", 0),
        _integer(stale_exclusion_evictions, "feedback stale_exclusion_evictions", 0),
    )
    result["schema_version"] = schema_version
    return result


def feedback_state(
    policy: object = {},
    statement_records: object = (),
    relationship_records: object = (),
    policy_suppressions: object = (),
    stale_exclusions: object = (),
    receipts: object = {},
    statement_evictions: object = 0,
    relationship_evictions: object = 0,
    policy_suppression_evictions: object = 0,
    stale_exclusion_evictions: object = 0,
    schema_version: object = FEEDBACK_STATE_SCHEMA_VERSION,
) -> FeedbackState:
    version = _integer(schema_version, "feedback state schema_version", 1)
    if version != FEEDBACK_STATE_SCHEMA_VERSION:
        raise InvalidRequestError(f"unsupported feedback state schema_version: {version}")
    policy_source = feedback_policy() if policy == {} else policy
    validated_policy = validate_feedback_policy(policy_source)
    if not isinstance(statement_records, tuple):
        raise InvalidRequestError("feedback statement_records must be a tuple of StatementFeedbackRecord values")
    if not isinstance(relationship_records, tuple):
        raise InvalidRequestError("feedback relationship_records must be a tuple of RelationshipFeedbackRecord values")
    if not isinstance(policy_suppressions, tuple):
        raise InvalidRequestError("feedback policy_suppressions must be a tuple of PolicySuppression values")
    if not isinstance(stale_exclusions, tuple):
        raise InvalidRequestError("feedback stale_exclusions must be a tuple of StaleExclusion values")
    validated_statements = tuple(validate_statement_feedback_record(record) for record in statement_records)
    validated_relationships = tuple(validate_relationship_feedback_record(record) for record in relationship_records)
    validated_suppressions = tuple(validate_policy_suppression(value) for value in policy_suppressions)
    validated_exclusions = tuple(validate_stale_exclusion(value) for value in stale_exclusions)
    if not isinstance(receipts, Mapping):
        raise InvalidRequestError("feedback receipts must be an object")
    receipt_source = MutationReceiptLedger().snapshot() if receipts == {} else receipts
    result = _feedback_state_from_validated_components(
        validated_policy,
        validated_statements,
        validated_relationships,
        validated_suppressions,
        validated_exclusions,
        receipt_source,
        statement_evictions,
        relationship_evictions,
        policy_suppression_evictions,
        stale_exclusion_evictions,
        version,
    )
    return result


def validate_feedback_state(value: object) -> FeedbackState:
    data = _exact_mapping(value, "FeedbackState", FEEDBACK_STATE_FIELDS)
    result = feedback_state(
        policy=data["policy"],
        statement_records=data["statement_records"],
        relationship_records=data["relationship_records"],
        policy_suppressions=data["policy_suppressions"],
        stale_exclusions=data["stale_exclusions"],
        receipts=data["receipts"],
        statement_evictions=data["statement_evictions"],
        relationship_evictions=data["relationship_evictions"],
        policy_suppression_evictions=data["policy_suppression_evictions"],
        stale_exclusion_evictions=data["stale_exclusion_evictions"],
        schema_version=data["schema_version"],
    )
    return result


def feedback_state_to_dict(value: object) -> dict[str, object]:
    current = validate_feedback_state(value)
    result = _feedback_wire_value(current)
    return result


def feedback_state_signature(value: object) -> str:
    data = feedback_state_to_dict(value)
    result = canonical_fingerprint(data)
    return result


def feedback_state_to_json(value: object) -> str:
    data = feedback_state_to_dict(value)
    result = _json_text(data)
    return result


def feedback_state_from_dict(value: object) -> FeedbackState:
    data = _exact_mapping(value, "FeedbackState", FEEDBACK_STATE_FIELDS)
    for name in ("policy", "receipts"):
        if not isinstance(data[name], Mapping):
            raise InvalidRequestError(f"feedback state {name} must be an object")
    for name in ("statement_records", "relationship_records", "policy_suppressions", "stale_exclusions"):
        values = data[name]
        if not isinstance(values, list) or not all(isinstance(item, Mapping) for item in values):
            raise InvalidRequestError(f"feedback state {name} must be an array of objects")
    statement_values = data["statement_records"]
    relationship_values = data["relationship_records"]
    suppression_values = data["policy_suppressions"]
    exclusion_values = data["stale_exclusions"]
    parsed_statements = tuple(statement_feedback_record_from_dict(item) for item in statement_values)
    statement_cache = {_trusted_feedback_key_fingerprint(record["key"]): record["key"] for record in parsed_statements}
    identity_cache: dict[str, QueryIdentity] = {}
    scope_cache: dict[str, ScopeKey] = {}
    parsed_relationships = tuple(
        relationship_feedback_record_from_dict(item, identity_cache, scope_cache, statement_cache) for item in relationship_values
    )
    result = _feedback_state_from_validated_components(
        feedback_policy_from_dict(data["policy"]),
        parsed_statements,
        parsed_relationships,
        tuple(policy_suppression_from_dict(item) for item in suppression_values),
        tuple(stale_exclusion_from_dict(item) for item in exclusion_values),
        data["receipts"],
        data["statement_evictions"],
        data["relationship_evictions"],
        data["policy_suppression_evictions"],
        data["stale_exclusion_evictions"],
        _integer(data["schema_version"], "feedback state schema_version", 1),
    )
    return result


def feedback_state_from_json(value: str) -> FeedbackState:
    data = _load_json_mapping(value, "FeedbackState JSON")
    result = feedback_state_from_dict(data)
    return result


FeedbackMutationCandidate = dict


class _PreparedFeedbackState(dict[str, object]):
    """Opaque link between a returned state projection and its off-live owner."""

    def __init__(self, state: FeedbackState, target: object, candidate: object) -> None:
        super().__init__(state)
        self["receipts"] = json.loads(_json_text(state["receipts"]))
        self.target = target
        self.candidate = candidate
        self.canonical = state


def feedback_mutation_candidate(before: object, after: object, receipt: object, replayed: object) -> FeedbackMutationCandidate:
    result: FeedbackMutationCandidate = {
        "before": validate_feedback_state(before),
        "after": validate_feedback_state(after),
        "receipt": validate_mutation_receipt(receipt),
        "replayed": _boolean(replayed, "feedback mutation replayed"),
    }
    return result


def _trusted_feedback_mutation_candidate(
    before: FeedbackState,
    after: FeedbackState,
    receipt: MutationReceipt,
    replayed: bool,
) -> FeedbackMutationCandidate:
    """Build a candidate from isolated store snapshots and a ledger-owned receipt."""
    result: FeedbackMutationCandidate = {
        "before": before,
        "after": _trusted_feedback_state_copy(after) if after is before else after,
        "receipt": validate_mutation_receipt(receipt),
        "replayed": replayed,
    }
    return result


def validate_feedback_mutation_candidate(value: object) -> FeedbackMutationCandidate:
    data = _exact_mapping(value, "FeedbackMutationCandidate", set({"before", "after", "receipt", "replayed"}))
    result = feedback_mutation_candidate(data["before"], data["after"], data["receipt"], data["replayed"])
    return result


def _new_statement_record(observation: FeedbackObservation, policy: FeedbackPolicy) -> StatementFeedbackRecord:
    statistics = feedback_statistics_increment(feedback_statistics(), observation["outcome"])
    bucket = feedback_bucket(_bucket_start(observation["observed_at"], policy["bucket_seconds"]), statistics)
    result = _trusted_statement_feedback_record(
        _trusted_feedback_observation_statement_key(observation),
        statistics,
        (bucket,),
        observation["outcome"],
        observation["observed_at"],
    )
    return result


def _new_relationship_record(observation: FeedbackObservation, policy: FeedbackPolicy) -> RelationshipFeedbackRecord:
    statistics = feedback_statistics_increment(feedback_statistics(), observation["outcome"])
    bucket = feedback_bucket(_bucket_start(observation["observed_at"], policy["bucket_seconds"]), statistics)
    statement_key = _trusted_feedback_observation_statement_key(observation)
    result = _trusted_relationship_feedback_record(
        _trusted_feedback_observation_relationship_key(observation, statement_key),
        statistics,
        (bucket,),
        observation["outcome"],
        observation["observed_at"],
    )
    return result


def _record_buckets(value: object) -> tuple[FeedbackBucket, ...]:
    if not isinstance(value, Mapping) or not isinstance(value.get("buckets", []), tuple):
        raise InvalidRequestError("feedback record must contain a tuple of buckets")
    result = tuple(validate_feedback_bucket(bucket) for bucket in value["buckets"])
    return result


def _aged_values(records: tuple[object, ...], at: str, policy: FeedbackPolicy) -> dict[FeedbackOutcome, float]:
    evaluation = _parse_timestamp(at, "feedback history evaluation time")
    values = dict.fromkeys(FeedbackOutcome, 0.0)
    for record in records:
        for bucket in _record_buckets(record):
            start = _parse_timestamp(bucket["start_at"], "feedback bucket start_at")
            # A bucket represents its complete interval. Using its deterministic
            # end prevents same-bucket samples from falling below the integer
            # floor merely because they arrived after the bucket boundary.
            effective_at = start + timedelta(seconds=policy["bucket_seconds"])
            age_seconds = max(0.0, (evaluation - effective_at).total_seconds())
            weight = 0.5 ** (age_seconds / policy["half_life_seconds"])
            stats = bucket["statistics"]
            values[FeedbackOutcome.CANDIDATE] += stats["candidate_count"] * weight
            values[FeedbackOutcome.ACCEPTED] += stats["accept_count"] * weight
            values[FeedbackOutcome.REJECTED_QUALITY] += stats["rejected_quality"] * weight
            values[FeedbackOutcome.REJECTED_CONTEXT] += stats["rejected_context"] * weight
            values[FeedbackOutcome.REJECTED_STALE] += stats["rejected_stale"] * weight
            values[FeedbackOutcome.REJECTED_POLICY] += stats["rejected_policy"] * weight
    return values


class FeedbackStore:
    """Thread-safe authoritative owner for persisted Section 6 feedback state."""

    def __init__(self, state: object = ()) -> None:
        if state == ():
            state = feedback_state()
        validated_state = validate_feedback_state(state)
        self._lock = threading.RLock()
        self._install(validated_state)

    def _install(self, state: FeedbackState) -> None:
        policy = _freeze_feedback_value(state["policy"])
        statement_records = tuple(_freeze_feedback_value(record) for record in state["statement_records"])
        relationship_records = tuple(_freeze_feedback_value(record) for record in state["relationship_records"])
        policy_suppressions = tuple(_freeze_feedback_value(value) for value in state["policy_suppressions"])
        stale_exclusions = tuple(_freeze_feedback_value(value) for value in state["stale_exclusions"])
        self._state_dirty = False
        self._policy = policy
        self._statement_records = {statement_feedback_key_fingerprint(record["key"]): record for record in statement_records}
        self._relationship_records = {
            relationship_feedback_key_fingerprint(record["key"]): record for record in relationship_records
        }
        self._policy_suppressions = {
            (value["statement_id"], value["namespace"], value["policy_fingerprint"]): value for value in policy_suppressions
        }
        self._stale_exclusions = {
            (value["statement_id"], value["generation"], value["generation_available"]): value for value in stale_exclusions
        }
        self._receipts = mutation_receipt_ledger_from_snapshot(state["receipts"])
        self._statement_evictions = state["statement_evictions"]
        self._relationship_evictions = state["relationship_evictions"]
        self._policy_suppression_evictions = state["policy_suppression_evictions"]
        self._stale_exclusion_evictions = state["stale_exclusion_evictions"]
        self._state = _trusted_feedback_state(
            policy,
            statement_records,
            relationship_records,
            policy_suppressions,
            stale_exclusions,
            self._receipts.snapshot(),
            self._statement_evictions,
            self._relationship_evictions,
            self._policy_suppression_evictions,
            self._stale_exclusion_evictions,
        )

    @property
    def policy(self) -> FeedbackPolicy:
        result = validate_feedback_policy(self._policy)
        return result

    def snapshot(self) -> FeedbackState:
        with self._lock:
            if not self._state_dirty:
                result = _trusted_feedback_state_copy(self._state)
                return result
            state = _trusted_feedback_state(
                self._policy,
                tuple(record for _, record in sorted(self._statement_records.items())),
                tuple(record for _, record in sorted(self._relationship_records.items())),
                tuple(sorted(self._policy_suppressions.values(), key=policy_suppression_signature)),
                tuple(sorted(self._stale_exclusions.values(), key=stale_exclusion_signature)),
                self._receipts.snapshot(),
                self._statement_evictions,
                self._relationship_evictions,
                self._policy_suppression_evictions,
                self._stale_exclusion_evictions,
            )
            self._state = state
            self._state_dirty = False
            result = _trusted_feedback_state_copy(state)
            return result

    def _candidate_copy(self):
        """Create one shallow off-live owner while sharing immutable records."""

        candidate = object.__new__(FeedbackStore)
        candidate._lock = threading.RLock()
        candidate._state = self._state
        candidate._state_dirty = False
        candidate._policy = self._policy
        candidate._statement_records = dict(self._statement_records)
        candidate._relationship_records = dict(self._relationship_records)
        candidate._policy_suppressions = dict(self._policy_suppressions)
        candidate._stale_exclusions = dict(self._stale_exclusions)
        candidate._receipts = self._receipts._trusted_clone()
        candidate._statement_evictions = self._statement_evictions
        candidate._relationship_evictions = self._relationship_evictions
        candidate._policy_suppression_evictions = self._policy_suppression_evictions
        candidate._stale_exclusion_evictions = self._stale_exclusion_evictions
        return candidate

    def replace_from_snapshot(self, state: FeedbackState) -> None:
        if isinstance(state, _PreparedFeedbackState) and state.target is self and isinstance(state.candidate, FeedbackStore):
            if dict(state) != state.canonical:
                raise InvalidRequestError("prepared feedback state was modified before publication")
            candidate = state.candidate
            with self._lock, candidate._lock:
                self._state = candidate._state
                self._state_dirty = candidate._state_dirty
                self._policy = candidate._policy
                self._statement_records = dict(candidate._statement_records)
                self._relationship_records = dict(candidate._relationship_records)
                self._policy_suppressions = dict(candidate._policy_suppressions)
                self._stale_exclusions = dict(candidate._stale_exclusions)
                self._receipts = candidate._receipts._trusted_clone()
                self._statement_evictions = candidate._statement_evictions
                self._relationship_evictions = candidate._relationship_evictions
                self._policy_suppression_evictions = candidate._policy_suppression_evictions
                self._stale_exclusion_evictions = candidate._stale_exclusion_evictions
            return
        validated_state = validate_feedback_state(state)
        with self._lock:
            self._install(validated_state)

    def _apply_observation(self, observation: FeedbackObservation) -> None:
        self._state_dirty = True
        statement_key = _trusted_feedback_observation_statement_key(observation)
        statement_fingerprint = _trusted_feedback_key_fingerprint(statement_key)
        statement = self._statement_records.get(statement_fingerprint)
        if statement:
            updated_statement = _trusted_statement_feedback_record_apply(statement, observation, self._policy)
        else:
            updated_statement = _new_statement_record(observation, self._policy)
        self._statement_records[statement_fingerprint] = _freeze_feedback_value(updated_statement)
        relationship_key = _trusted_feedback_observation_relationship_key(observation, statement_key)
        relationship_fingerprint = _trusted_feedback_key_fingerprint(relationship_key)
        relationship = self._relationship_records.get(relationship_fingerprint)
        if relationship:
            updated_relationship = _trusted_relationship_feedback_record_apply(relationship, observation, self._policy)
        else:
            updated_relationship = _new_relationship_record(observation, self._policy)
        self._relationship_records[relationship_fingerprint] = _freeze_feedback_value(updated_relationship)
        suppression_key = (
            observation["statement_id"],
            observation["scope"]["namespace"],
            observation["policy_fingerprint"],
        )
        if observation["outcome"] == FeedbackOutcome.REJECTED_POLICY:
            self._policy_suppressions[suppression_key] = _freeze_feedback_value(
                policy_suppression(*suppression_key, observation["observed_at"])
            )
        elif observation["outcome"] == FeedbackOutcome.ACCEPTED:
            self._policy_suppressions.pop(suppression_key, {})
        if observation["outcome"] == FeedbackOutcome.REJECTED_STALE:
            exclusion = stale_exclusion(
                observation["statement_id"],
                observation["generation"],
                observation["generation_available"],
                observation["observed_at"],
            )
            exclusion_key = (exclusion["statement_id"], exclusion["generation"], exclusion["generation_available"])
            self._stale_exclusions[exclusion_key] = _freeze_feedback_value(exclusion)

    def _enforce_capacity(self) -> None:
        while len(self._statement_records) > self._policy["max_statement_records"]:
            oldest_key = min(
                self._statement_records,
                key=lambda key: (self._statement_records[key]["last_observed_at"], key),
            )
            del self._statement_records[oldest_key]
            self._statement_evictions += 1
        while len(self._relationship_records) > self._policy["max_relationship_records"]:
            oldest_key = min(
                self._relationship_records,
                key=lambda key: (self._relationship_records[key]["last_observed_at"], key),
            )
            del self._relationship_records[oldest_key]
            self._relationship_evictions += 1
        while len(self._policy_suppressions) > self._policy["max_statement_records"]:
            oldest_key = min(
                self._policy_suppressions,
                key=lambda key: (self._policy_suppressions[key]["observed_at"], key),
            )
            del self._policy_suppressions[oldest_key]
            self._policy_suppression_evictions += 1
        while len(self._stale_exclusions) > self._policy["max_statement_records"]:
            oldest_key = min(
                self._stale_exclusions,
                key=lambda key: (self._stale_exclusions[key]["observed_at"], key),
            )
            del self._stale_exclusions[oldest_key]
            self._stale_exclusion_evictions += 1

    def prepare(
        self,
        request_id: str,
        observations: tuple[FeedbackObservation, ...],
        lifecycle_status: LifecycleHandoffStatus = LifecycleHandoffStatus.NOT_APPLICABLE,
    ) -> FeedbackMutationCandidate:
        _text(request_id, "feedback request_id", MAX_REFERENCE_ID_BYTES)
        if not isinstance(observations, tuple) or not observations:
            raise InvalidRequestError("feedback observations must be a non-empty tuple of FeedbackObservation values")
        if len(observations) > MAX_FEEDBACK_OBSERVATIONS:
            raise InvalidRequestError(f"feedback observations exceed the limit of {MAX_FEEDBACK_OBSERVATIONS}")
        validated_observations = tuple(validate_feedback_observation(observation) for observation in observations)
        result = self._prepare_validated(request_id, validated_observations, lifecycle_status)
        return result

    def _prepare_validated(
        self,
        request_id: str,
        validated_observations: tuple[FeedbackObservation, ...],
        lifecycle_status: LifecycleHandoffStatus = LifecycleHandoffStatus.NOT_APPLICABLE,
    ) -> FeedbackMutationCandidate:
        """Prepare observations already validated by an internal service boundary."""
        _text(request_id, "feedback request_id", MAX_REFERENCE_ID_BYTES)
        if not validated_observations or len(validated_observations) > MAX_FEEDBACK_OBSERVATIONS:
            raise InvalidRequestError("validated feedback observations must be a non-empty bounded tuple")
        if not isinstance(lifecycle_status, LifecycleHandoffStatus):
            raise InvalidRequestError("feedback lifecycle_status must be LifecycleHandoffStatus")
        signature = _feedback_payload_signature(validated_observations)
        with self._lock:
            before = self.snapshot()
            lookup = self._receipts.lookup(request_id, MutationOperation.RECORD_FEEDBACK, signature)
            if lookup["outcome"] == ReceiptLookupOutcome.REPLAY:
                result = _trusted_feedback_mutation_candidate(before, before, receipt_lookup_receipt(lookup), True)
                return result
            if lookup["outcome"] == ReceiptLookupOutcome.CONFLICT:
                raise ConflictError(f"feedback request_id is associated with a different observation: {request_id}")
            if lookup["outcome"] == ReceiptLookupOutcome.IN_PROGRESS:
                raise ConflictError(f"feedback request is in progress and requires recovery: {request_id}")
            if lookup["outcome"] == ReceiptLookupOutcome.EXPIRED:
                raise ConflictError(f"feedback request result expired and cannot be reapplied safely: {request_id}")
            candidate = self._candidate_copy()
            for observation in validated_observations:
                candidate._apply_observation(observation)
            candidate._enforce_capacity()
            outcomes = {outcome.value: 0 for outcome in FeedbackOutcome}
            for observation in validated_observations:
                outcomes[observation["outcome"].value] += 1
            receipt = mutation_receipt(
                sequence=candidate._receipts.next_sequence,
                request_id=request_id,
                operation=MutationOperation.RECORD_FEEDBACK,
                payload_signature=signature,
                result_code=MutationResultCode.FEEDBACK_RECORDED,
                affected_generations=(),
                result={
                    "observation_count": len(validated_observations),
                    "outcomes": outcomes,
                    "lifecycle_status": lifecycle_status.value,
                },
                completion_state=ReceiptCompletionState.COMPLETED,
                created_at=max(observation["observed_at"] for observation in validated_observations),
            )
            candidate._receipts.record(receipt)
            after = _PreparedFeedbackState(candidate.snapshot(), self, candidate)
            result = _trusted_feedback_mutation_candidate(before, after, receipt, False)
            return result

    def history(
        self,
        query_identity: QueryIdentity,
        constraint: str,
        statement_id: str,
        current_generation: int,
        policy_fingerprint: str,
        evaluation_time: str,
    ) -> FeedbackHistory:
        try:
            validated_query_identity = validate_query_identity(query_identity)
        except IdentityValidationError as error:
            raise InvalidRequestError("feedback history query_identity must be QueryIdentity") from error
        _fingerprint(constraint, "feedback history constraint_fingerprint")
        _text(statement_id, "feedback history statement_id", MAX_STATEMENT_ID_BYTES)
        _integer(current_generation, "feedback history current_generation", 1)
        policy_value = _fingerprint(policy_fingerprint, "feedback history policy_fingerprint")
        _parse_timestamp(evaluation_time, "feedback history evaluation_time")
        with self._lock:
            statement_records = tuple(
                record
                for record in self._statement_records.values()
                if record["key"]["statement_id"] == statement_id
                and record["key"]["policy_fingerprint"] == policy_value
                and record["key"]["contract_fingerprint"] == FEEDBACK_CONTRACT_FINGERPRINT
                and record["key"]["generation_available"]
                and record["key"]["generation"] <= current_generation
            )
            relationship_records = tuple(
                record
                for record in self._relationship_records.values()
                if record["key"]["statement"]["statement_id"] == statement_id
                and record["key"]["statement"]["policy_fingerprint"] == policy_value
                and record["key"]["statement"]["contract_fingerprint"] == FEEDBACK_CONTRACT_FINGERPRINT
                and record["key"]["statement"]["generation_available"]
                and record["key"]["statement"]["generation"] <= current_generation
                and record["key"]["query_identity"] == validated_query_identity
                and record["key"]["scope"] == validated_query_identity["scope"]
                and record["key"]["constraint_fingerprint"] == constraint
            )
            statement_values = _aged_values(statement_records, evaluation_time, self._policy)
            relationship_values = _aged_values(relationship_records, evaluation_time, self._policy)

        statement_accept = statement_values[FeedbackOutcome.ACCEPTED]
        statement_reject = statement_values[FeedbackOutcome.REJECTED_QUALITY] + statement_values[FeedbackOutcome.REJECTED_STALE]
        relationship_accept = relationship_values[FeedbackOutcome.ACCEPTED]
        relationship_reject = sum(
            relationship_values[outcome]
            for outcome in (
                FeedbackOutcome.REJECTED_QUALITY,
                FeedbackOutcome.REJECTED_CONTEXT,
                FeedbackOutcome.REJECTED_STALE,
                FeedbackOutcome.REJECTED_POLICY,
            )
        )
        statement_samples = statement_accept + statement_reject
        relationship_samples = relationship_accept + relationship_reject
        statement_available = statement_samples >= self._policy["minimum_verdict_samples"]
        relationship_available = relationship_samples >= self._policy["minimum_verdict_samples"]

        def posterior(accepted: float, rejected: float) -> float:
            numerator = accepted + self._policy["prior_accept"]
            denominator = accepted + rejected + self._policy["prior_accept"] + self._policy["prior_reject"]
            result = numerator / denominator
            return result

        statement_value = posterior(statement_accept, statement_reject) if statement_available else 0.0
        relationship_value = posterior(relationship_accept, relationship_reject) if relationship_available else 0.0
        available = statement_available or relationship_available
        if statement_available and relationship_available:
            value = (statement_value + relationship_value) / 2.0
        elif statement_available:
            value = statement_value
        elif relationship_available:
            value = relationship_value
        else:
            value = 0.0
        result = feedback_history(
            value=value,
            available=available,
            statement_value=statement_value,
            statement_available=statement_available,
            relationship_value=relationship_value,
            relationship_available=relationship_available,
            statement_samples=statement_samples,
            relationship_samples=relationship_samples,
            policy_fingerprint=policy_value,
            feedback_policy_value=feedback_policy_fingerprint(self._policy),
        )
        return result

    def stale_excluded(self, statement_id: str, current_generation: int, generation_available: bool = True) -> bool:
        _text(statement_id, "stale exclusion statement_id", MAX_STATEMENT_ID_BYTES)
        _integer(current_generation, "stale exclusion current_generation", 0)
        _boolean(generation_available, "stale exclusion generation_available")
        result = self._trusted_stale_excluded(statement_id, current_generation, generation_available)
        return result

    def _trusted_stale_excluded(
        self,
        statement_id: str,
        current_generation: int,
        generation_available: bool = True,
    ) -> bool:
        """Check a validated in-process eligibility tuple."""
        result = False
        with self._lock:
            for exclusion in self._stale_exclusions.values():
                if exclusion["statement_id"] != statement_id:
                    continue
                if not exclusion["generation_available"]:
                    result = not generation_available
                    break
                if generation_available and current_generation >= exclusion["generation"]:
                    result = True
                    break
        return result

    def policy_suppressed(self, statement_id: str, namespace: str, policy_fingerprint: str) -> bool:
        _text(statement_id, "policy suppression statement_id", MAX_STATEMENT_ID_BYTES)
        _text(namespace, "policy suppression namespace", MAX_NAMESPACE_BYTES, allow_empty=True)
        policy_value = _fingerprint(policy_fingerprint, "policy suppression policy_fingerprint")
        result = self._trusted_policy_suppressed(statement_id, namespace, policy_value)
        return result

    def _trusted_policy_suppressed(self, statement_id: str, namespace: str, policy_fingerprint: str) -> bool:
        """Check a validated in-process policy-suppression tuple."""
        with self._lock:
            result = (statement_id, namespace, policy_fingerprint) in self._policy_suppressions
        return result

    def inspect(self, limit: int = MAX_INSPECTION_RECORDS) -> dict[str, object]:
        _integer(limit, "feedback inspection limit", 1, MAX_INSPECTION_RECORDS)
        with self._lock:
            statement_values = sorted(
                self._statement_records.values(), key=lambda record: statement_feedback_key_fingerprint(record["key"])
            )
            relationship_values = sorted(
                self._relationship_records.values(),
                key=lambda record: relationship_feedback_key_fingerprint(record["key"]),
            )
            receipt_snapshot = self._receipts.snapshot()
            receipt_values = sorted(
                receipt_snapshot["receipts"],
                key=lambda value: value["sequence"],
                reverse=True,
            )
            receipt_tombstones = receipt_snapshot["tombstones"]
            result = {
                "schema_version": FEEDBACK_STATE_SCHEMA_VERSION,
                "policy_version": self._policy["policy_version"],
                "policy_fingerprint": feedback_policy_fingerprint(self._policy),
                "statement_record_count": len(statement_values),
                "relationship_record_count": len(relationship_values),
                "policy_suppression_count": len(self._policy_suppressions),
                "stale_exclusion_count": len(self._stale_exclusions),
                "receipt_count": len(receipt_values),
                "receipt_tombstone_count": len(receipt_tombstones),
                "receipts": [
                    {
                        "diagnostic_id": _diagnostic_id(value["request_id"]),
                        "sequence": value["sequence"],
                        "result_code": value["result_code"],
                        "completion_state": value["completion_state"],
                        "observation_count": value["result"].get("observation_count", 0),
                        "outcomes": value["result"].get("outcomes", {}),
                        "lifecycle_status": value["result"].get("lifecycle_status", ""),
                    }
                    for value in receipt_values[:limit]
                ],
                "omitted_receipt_count": max(0, len(receipt_values) - limit),
                "statement_evictions": self._statement_evictions,
                "relationship_evictions": self._relationship_evictions,
                "policy_suppression_evictions": self._policy_suppression_evictions,
                "stale_exclusion_evictions": self._stale_exclusion_evictions,
                "statements": [
                    {
                        "diagnostic_id": _diagnostic_id(statement_feedback_key_fingerprint(record["key"])),
                        "statistics": feedback_statistics_to_dict(record["raw"]),
                        "bucket_count": len(record["buckets"]),
                        "last_outcome": record["last_outcome"].value,
                        "last_observed_at": record["last_observed_at"],
                    }
                    for record in statement_values[:limit]
                ],
                "omitted_statement_count": max(0, len(statement_values) - limit),
                "relationships": [
                    {
                        "diagnostic_id": _diagnostic_id(relationship_feedback_key_fingerprint(record["key"])),
                        "statistics": feedback_statistics_to_dict(record["raw"]),
                        "bucket_count": len(record["buckets"]),
                        "last_outcome": record["last_outcome"].value,
                        "last_observed_at": record["last_observed_at"],
                    }
                    for record in relationship_values[:limit]
                ],
                "omitted_relationship_count": max(0, len(relationship_values) - limit),
            }
        return result


def feedback_store_from_dict(value: Mapping[str, object]) -> FeedbackStore:
    """Construct a feedback store from one validated serialized state."""
    state = feedback_state_from_dict(value)
    result = FeedbackStore(state)
    return result


NegativeResolutionKey = dict


def negative_resolution_key(
    query_identity: object,
    scope: object,
    constraint_fingerprint: object,
    knowledge_epoch: object,
    knowledge_epoch_available: object,
    normalization_version: object,
    resolver_plan_fingerprint: object,
    capability_readiness_fingerprint: object,
    policy_fingerprint: object,
    schema_version: object = NEGATIVE_KEY_SCHEMA_VERSION,
) -> NegativeResolutionKey:
    """Build the exact knowledge and policy state for one observed miss."""
    version = _integer(schema_version, "negative key schema_version", 0)
    if version != NEGATIVE_KEY_SCHEMA_VERSION:
        raise InvalidRequestError(f"unsupported negative key schema_version: {version}")
    try:
        validated_query_identity = validate_query_identity(query_identity)
    except IdentityValidationError as error:
        raise InvalidRequestError("negative query_identity must be QueryIdentity") from error
    try:
        validated_scope = validate_scope_key(scope)
    except IdentityValidationError as error:
        raise InvalidRequestError("negative scope must match query identity scope") from error
    if validated_query_identity["scope"] != validated_scope:
        raise InvalidRequestError("negative scope must match query identity scope")
    validated_constraint = _fingerprint(constraint_fingerprint, "negative constraint_fingerprint")
    validated_epoch = _integer(knowledge_epoch, "negative knowledge_epoch", 0)
    epoch_available = _boolean(knowledge_epoch_available, "negative knowledge_epoch_available")
    if not epoch_available:
        raise InvalidRequestError("negative resolution requires an available knowledge epoch")
    validated_normalization_version = _integer(normalization_version, "negative normalization_version", 1)
    validated_plan = _fingerprint(resolver_plan_fingerprint, "negative resolver_plan_fingerprint")
    validated_readiness = _fingerprint(capability_readiness_fingerprint, "negative capability_readiness_fingerprint")
    validated_policy = _fingerprint(policy_fingerprint, "negative policy_fingerprint")
    result: NegativeResolutionKey = {
        "query_identity": validated_query_identity,
        "scope": validated_scope,
        "constraint_fingerprint": validated_constraint,
        "knowledge_epoch": validated_epoch,
        "knowledge_epoch_available": epoch_available,
        "normalization_version": validated_normalization_version,
        "resolver_plan_fingerprint": validated_plan,
        "capability_readiness_fingerprint": validated_readiness,
        "policy_fingerprint": validated_policy,
        "schema_version": version,
    }
    return result


def validate_negative_resolution_key(value: object) -> NegativeResolutionKey:
    data = _exact_mapping(value, "NegativeResolutionKey", NEGATIVE_RESOLUTION_KEY_FIELDS)
    result = negative_resolution_key(
        data["query_identity"],
        data["scope"],
        data["constraint_fingerprint"],
        data["knowledge_epoch"],
        data["knowledge_epoch_available"],
        data["normalization_version"],
        data["resolver_plan_fingerprint"],
        data["capability_readiness_fingerprint"],
        data["policy_fingerprint"],
        data["schema_version"],
    )
    return result


def negative_resolution_key_with_changes(value: object, changes: object) -> NegativeResolutionKey:
    current = validate_negative_resolution_key(value)
    if not isinstance(changes, Mapping) or not set(changes).issubset(NEGATIVE_RESOLUTION_KEY_FIELDS):
        raise InvalidRequestError("negative resolution key changes contain invalid fields")
    updated: dict[str, object] = dict(current)
    updated.update(changes)
    result = validate_negative_resolution_key(updated)
    return result


def negative_resolution_key_to_dict(value: object) -> dict[str, object]:
    current = validate_negative_resolution_key(value)
    result = {
        "schema_version": current["schema_version"],
        "query_identity": query_identity_to_dict(current["query_identity"]),
        "scope": scope_key_to_dict(current["scope"]),
        "constraint_fingerprint": current["constraint_fingerprint"],
        "knowledge_epoch": current["knowledge_epoch"],
        "knowledge_epoch_available": current["knowledge_epoch_available"],
        "normalization_version": current["normalization_version"],
        "resolver_plan_fingerprint": current["resolver_plan_fingerprint"],
        "capability_readiness_fingerprint": current["capability_readiness_fingerprint"],
        "policy_fingerprint": current["policy_fingerprint"],
    }
    return result


def negative_resolution_key_fingerprint(value: object) -> str:
    data = negative_resolution_key_to_dict(value)
    result = canonical_fingerprint(data)
    return result


def negative_resolution_key_to_json(value: object) -> str:
    data = negative_resolution_key_to_dict(value)
    result = _json_text(data)
    return result


def negative_resolution_key_relationship_fingerprint(value: object) -> str:
    current = validate_negative_resolution_key(value)
    data = {
        "query_identity": query_identity_to_dict(current["query_identity"]),
        "scope": scope_key_to_dict(current["scope"]),
        "constraint_fingerprint": current["constraint_fingerprint"],
    }
    result = canonical_fingerprint(data)
    return result


def negative_resolution_key_from_dict(value: object) -> NegativeResolutionKey:
    data = _exact_mapping(value, "NegativeResolutionKey", NEGATIVE_RESOLUTION_KEY_FIELDS)
    if not isinstance(data["query_identity"], Mapping) or not isinstance(data["scope"], Mapping):
        raise InvalidRequestError("negative identity and scope must be objects")
    result = negative_resolution_key(
        schema_version=_integer(data["schema_version"], "negative key schema_version", 0),
        query_identity=query_identity_from_dict(data["query_identity"]),
        scope=scope_key_from_dict(data["scope"]),
        constraint_fingerprint=_fingerprint(data["constraint_fingerprint"], "negative constraint_fingerprint"),
        knowledge_epoch=_integer(data["knowledge_epoch"], "negative knowledge_epoch", 0),
        knowledge_epoch_available=_boolean(data["knowledge_epoch_available"], "negative knowledge_epoch_available"),
        normalization_version=_integer(data["normalization_version"], "negative normalization_version", 1),
        resolver_plan_fingerprint=_fingerprint(data["resolver_plan_fingerprint"], "negative resolver_plan_fingerprint"),
        capability_readiness_fingerprint=_fingerprint(
            data["capability_readiness_fingerprint"], "negative capability_readiness_fingerprint"
        ),
        policy_fingerprint=_fingerprint(data["policy_fingerprint"], "negative policy_fingerprint"),
    )
    return result


def negative_resolution_key_from_json(value: str) -> NegativeResolutionKey:
    data = _load_json_mapping(value, "NegativeResolutionKey JSON")
    result = negative_resolution_key_from_dict(data)
    return result


NegativeResolution = dict


def negative_resolution(
    key: object,
    reason: object,
    created_at: object,
    expires_at: object,
    hit_count: object = 0,
    schema_version: object = NEGATIVE_RESOLUTION_SCHEMA_VERSION,
) -> NegativeResolution:
    version = _integer(schema_version, "negative resolution schema_version", 0)
    if version != NEGATIVE_RESOLUTION_SCHEMA_VERSION:
        raise InvalidRequestError(f"unsupported negative resolution schema_version: {version}")
    try:
        validated_key = validate_negative_resolution_key(key)
    except InvalidRequestError as error:
        raise InvalidRequestError("negative resolution key has an invalid type") from error
    if not isinstance(reason, NegativeResolutionReason):
        raise InvalidRequestError("negative resolution reason has an invalid type")
    created = _parse_timestamp(created_at, "negative created_at")
    expires = _parse_timestamp(expires_at, "negative expires_at")
    if expires <= created:
        raise InvalidRequestError("negative expires_at must be after created_at")
    validated_created_at = canonical_utc(created)
    validated_expires_at = canonical_utc(expires)
    validated_hit_count = _integer(hit_count, "negative hit_count", 0)
    result: NegativeResolution = {
        "key": validated_key,
        "reason": reason,
        "created_at": validated_created_at,
        "expires_at": validated_expires_at,
        "hit_count": validated_hit_count,
        "schema_version": version,
    }
    return result


def empty_negative_resolution() -> NegativeResolution:
    key = negative_resolution_key(
        query_identity("unavailable", scope=EMPTY_SCOPE_KEY),
        EMPTY_SCOPE_KEY,
        EMPTY_FINGERPRINT,
        0,
        True,
        1,
        EMPTY_FINGERPRINT,
        EMPTY_FINGERPRINT,
        EMPTY_FINGERPRINT,
    )
    result = negative_resolution(
        key,
        NegativeResolutionReason.INSUFFICIENT_KNOWLEDGE,
        EMPTY_NEGATIVE_CREATED_AT,
        EMPTY_NEGATIVE_EXPIRES_AT,
    )
    return result


def validate_negative_resolution(value: object) -> NegativeResolution:
    data = _exact_mapping(value, "NegativeResolution", NEGATIVE_RESOLUTION_FIELDS)
    result = negative_resolution(
        data["key"],
        data["reason"],
        data["created_at"],
        data["expires_at"],
        data["hit_count"],
        data["schema_version"],
    )
    return result


def negative_resolution_with_changes(value: object, changes: object) -> NegativeResolution:
    current = validate_negative_resolution(value)
    if not isinstance(changes, Mapping) or not set(changes).issubset(NEGATIVE_RESOLUTION_FIELDS):
        raise InvalidRequestError("negative resolution changes contain invalid fields")
    updated: dict[str, object] = dict(current)
    updated.update(changes)
    result = validate_negative_resolution(updated)
    return result


def negative_resolution_to_dict(value: object) -> dict[str, object]:
    current = validate_negative_resolution(value)
    result = {
        "schema_version": current["schema_version"],
        "key": negative_resolution_key_to_dict(current["key"]),
        "reason": current["reason"].value,
        "created_at": current["created_at"],
        "expires_at": current["expires_at"],
        "hit_count": current["hit_count"],
    }
    return result


def negative_resolution_to_json(value: object) -> str:
    data = negative_resolution_to_dict(value)
    result = _json_text(data)
    return result


def negative_resolution_from_dict(value: object) -> NegativeResolution:
    data = _exact_mapping(value, "NegativeResolution", NEGATIVE_RESOLUTION_FIELDS)
    if not isinstance(data["key"], Mapping):
        raise InvalidRequestError("negative key must be an object")
    try:
        reason = NegativeResolutionReason(data["reason"])
    except (TypeError, ValueError) as error:
        raise InvalidRequestError("negative resolution contains an unsupported reason") from error
    result = negative_resolution(
        schema_version=_integer(data["schema_version"], "negative resolution schema_version", 0),
        key=negative_resolution_key_from_dict(data["key"]),
        reason=reason,
        created_at=canonical_utc(_parse_timestamp(data["created_at"], "negative created_at")),
        expires_at=canonical_utc(_parse_timestamp(data["expires_at"], "negative expires_at")),
        hit_count=_integer(data["hit_count"], "negative hit_count", 0),
    )
    return result


def negative_resolution_from_json(value: str) -> NegativeResolution:
    data = _load_json_mapping(value, "NegativeResolution JSON")
    result = negative_resolution_from_dict(data)
    return result


NegativeLookup = dict


def negative_lookup(hit: object, record: object = {}) -> NegativeLookup:
    validated_hit = _boolean(hit, "negative lookup hit")
    if not isinstance(record, Mapping):
        raise InvalidRequestError("negative lookup record must be NegativeResolution")
    try:
        validated_record = empty_negative_resolution() if not record else validate_negative_resolution(record)
    except InvalidRequestError as error:
        raise InvalidRequestError("negative lookup record must be NegativeResolution") from error
    if not validated_hit and validated_record != empty_negative_resolution():
        raise InvalidRequestError("negative lookup miss must use the concrete empty record")
    result: NegativeLookup = {"hit": validated_hit, "record": validated_record}
    return result


def validate_negative_lookup(value: object) -> NegativeLookup:
    data = _exact_mapping(value, "NegativeLookup", NEGATIVE_LOOKUP_FIELDS)
    result = negative_lookup(data["hit"], data["record"])
    return result


class NegativeResolutionStore:
    """Memory-only bounded, non-sliding negative-resolution owner."""

    def __init__(
        self,
        max_records: int = DEFAULT_NEGATIVE_MAX_RECORDS,
        ttl_seconds: int = DEFAULT_NEGATIVE_TTL_SECONDS,
    ) -> None:
        self.max_records = _integer(max_records, "negative max_records", 1, MAX_NEGATIVE_RECORDS)
        self.ttl_seconds = _integer(ttl_seconds, "negative ttl_seconds", 1, MAX_NEGATIVE_TTL_SECONDS)
        self._lock = threading.RLock()
        self._records: dict[str, NegativeResolution] = {}
        self._metrics = {
            "lookups": 0,
            "hits": 0,
            "misses": 0,
            "admissions": 0,
            "expiries": 0,
            "evictions": 0,
            "invalidations": 0,
        }

    def _expire(self, at: datetime) -> None:
        expired = [
            key for key, record in self._records.items() if _parse_timestamp(record["expires_at"], "negative expires_at") <= at
        ]
        for key in expired:
            del self._records[key]
            self._metrics["expiries"] += 1

    def _invalidate_related(self, key: NegativeResolutionKey) -> None:
        validated_key = validate_negative_resolution_key(key)
        relationship = negative_resolution_key_relationship_fingerprint(validated_key)
        stale = [
            record_key
            for record_key, record in self._records.items()
            if negative_resolution_key_relationship_fingerprint(record["key"]) == relationship and record["key"] != validated_key
        ]
        for record_key in stale:
            del self._records[record_key]
            self._metrics["invalidations"] += 1

    def lookup(self, key: NegativeResolutionKey, evaluation_time: str) -> NegativeLookup:
        try:
            validated_key = validate_negative_resolution_key(key)
        except InvalidRequestError as error:
            raise InvalidRequestError("negative lookup key must be NegativeResolutionKey") from error
        at = _parse_timestamp(evaluation_time, "negative lookup evaluation_time")
        with self._lock:
            self._metrics["lookups"] += 1
            self._expire(at)
            self._invalidate_related(validated_key)
            fingerprint = negative_resolution_key_fingerprint(validated_key)
            if fingerprint not in self._records:
                self._metrics["misses"] += 1
                result = negative_lookup(False)
                return result
            record = self._records[fingerprint]
            updated = negative_resolution_with_changes(record, {"hit_count": record["hit_count"] + 1})
            self._records[fingerprint] = updated
            self._metrics["hits"] += 1
            result = negative_lookup(True, updated)
            return result

    def admit(self, key: NegativeResolutionKey, evaluation_time: str) -> NegativeResolution:
        try:
            validated_key = validate_negative_resolution_key(key)
        except InvalidRequestError as error:
            raise InvalidRequestError("negative admission key must be NegativeResolutionKey") from error
        at = _parse_timestamp(evaluation_time, "negative admission evaluation_time")
        with self._lock:
            self._expire(at)
            self._invalidate_related(validated_key)
            fingerprint = negative_resolution_key_fingerprint(validated_key)
            if fingerprint in self._records:
                result = validate_negative_resolution(self._records[fingerprint])
                return result
            created_at = canonical_utc(at)
            record = negative_resolution(
                validated_key,
                NegativeResolutionReason.INSUFFICIENT_KNOWLEDGE,
                created_at,
                canonical_utc(at + timedelta(seconds=self.ttl_seconds)),
            )
            self._records[fingerprint] = record
            self._metrics["admissions"] += 1
            while len(self._records) > self.max_records:
                oldest_key = min(self._records, key=lambda value: (self._records[value]["created_at"], value))
                del self._records[oldest_key]
                self._metrics["evictions"] += 1
            result = validate_negative_resolution(record)
            return result

    def invalidate_for_key(self, key: NegativeResolutionKey) -> int:
        """Invalidate prior state for one relationship under changed policy state."""

        try:
            validated_key = validate_negative_resolution_key(key)
        except InvalidRequestError as error:
            raise InvalidRequestError("negative invalidation key must be NegativeResolutionKey") from error
        with self._lock:
            before = len(self._records)
            self._invalidate_related(validated_key)
            removed = before - len(self._records)
            return removed

    def invalidate_epoch_snapshot(self, snapshot: Mapping[str, object]) -> int:
        if not isinstance(snapshot, Mapping):
            raise InvalidRequestError("negative epoch snapshot must be an object")
        epochs = snapshot.get("epochs", {})
        if not isinstance(epochs, Mapping):
            raise InvalidRequestError("negative epoch snapshot epochs must be an object")
        with self._lock:
            stale = []
            for fingerprint, record in self._records.items():
                current = epochs.get(record["key"]["scope"]["namespace"], ())
                if isinstance(current, bool) or not isinstance(current, int) or current != record["key"]["knowledge_epoch"]:
                    stale.append(fingerprint)
            for fingerprint in stale:
                del self._records[fingerprint]
                self._metrics["invalidations"] += 1
            removed = len(stale)
            return removed

    def clear(self) -> None:
        with self._lock:
            removed = len(self._records)
            self._records.clear()
            self._metrics["invalidations"] += removed

    def inspect(self, limit: int = MAX_INSPECTION_RECORDS) -> dict[str, object]:
        _integer(limit, "negative inspection limit", 1, MAX_INSPECTION_RECORDS)
        with self._lock:
            records = sorted(self._records.items())
            result = {
                "schema_version": NEGATIVE_RESOLUTION_SCHEMA_VERSION,
                "memory_only": True,
                "ttl_seconds": self.ttl_seconds,
                "max_records": self.max_records,
                "record_count": len(records),
                **self._metrics,
                "records": [
                    {
                        "diagnostic_id": _diagnostic_id(fingerprint),
                        "reason": record["reason"].value,
                        "created_at": record["created_at"],
                        "expires_at": record["expires_at"],
                        "hit_count": record["hit_count"],
                    }
                    for fingerprint, record in records[:limit]
                ],
                "omitted_record_count": max(0, len(records) - limit),
            }
            return result
