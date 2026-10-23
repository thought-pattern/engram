"""Feedback learning contracts and explicit interchange codecs."""

from enum import Enum
from hashlib import sha256 as hashlib_sha256

from engram.constants_feedback import (
    EMPTY_FINGERPRINT,
    FEEDBACK_BUCKET_FIELDS,
    FEEDBACK_BUCKET_SECONDS,
    FEEDBACK_HALF_LIFE_SECONDS,
    FEEDBACK_MAX_BUCKETS_PER_RECORD,
    FEEDBACK_MAX_RELATIONSHIP_RECORDS,
    FEEDBACK_MAX_STATEMENT_RECORDS,
    FEEDBACK_MINIMUM_VERDICT_SAMPLES,
    FEEDBACK_OBSERVATION_FIELDS,
    FEEDBACK_OUTCOME_COUNTER_FIELDS,
    FEEDBACK_POLICY_FIELDS,
    FEEDBACK_PRIOR_ACCEPT,
    FEEDBACK_PRIOR_REJECT,
    FEEDBACK_RECORD_FIELDS,
    FEEDBACK_STATE_FIELDS,
    FEEDBACK_STATISTICS_FIELDS,
    MAX_FEEDBACK_BUCKET_SECONDS,
    MAX_FEEDBACK_BUCKETS,
    MAX_FEEDBACK_HALF_LIFE_SECONDS,
    MAX_FEEDBACK_POLICY_RECORDS,
    MAX_FEEDBACK_POLICY_SAMPLES,
    MAX_FEEDBACK_PRIOR_MASS,
    MAX_FEEDBACK_REASON_BYTES,
    MAX_FEEDBACK_SIGNATURE_BYTES,
    MAX_REFERENCE_ID_BYTES,
    MAX_STATEMENT_ID_BYTES,
    POLICY_SUPPRESSION_FIELDS,
    RELATIONSHIP_FEEDBACK_KEY_FIELDS,
    STALE_EXCLUSION_FIELDS,
    STATEMENT_FEEDBACK_KEY_FIELDS,
    FeedbackObservationKind,
    FeedbackOutcome,
    FeedbackReferenceKind,
)
from engram.constants_identity import MAX_NAMESPACE_BYTES
from engram.copies import structural_copy
from engram.errors import ConflictError, IdentityValidationError, InvalidRequestError
from engram.feedback_values import (
    bucket_start,
    canonical_binary_encoding,
    canonical_fingerprint,
    canonical_utc,
    exact_mapping,
    internal_fingerprint,
    internal_integer,
    internal_number,
    json_text,
    load_json_mapping,
    thaw_native_value,
)
from engram.identity import (
    query_identity_from_dict,
    query_identity_to_dict,
    scope_key_from_dict,
    scope_key_to_dict,
    validate_query_identity,
    validate_scope_key,
)
from engram.mutations import (
    MutationReceiptLedger,
    validate_mutation_receipt_ledger_state,
)
from engram.validation import require_bool, require_text, require_utc_datetime, utc_datetime


def feedback_payload_signature(observations) -> str:
    """Hash a bounded observation sequence without the generic receipt item ceiling.

    Feedback observations are already strict, bounded contracts.  Hashing each
    canonical observation first keeps the receipt input fixed-size while the
    byte counter retains one explicit bound on the complete validated payload.
    Observation time is execution metadata and is deliberately omitted from
    retry identity.  Receipts live only in the process-memory feedback store,
    so the canonical binary encoding is never compared with a persisted
    signature from another process.
    """

    digest = hashlib_sha256(b"engram-feedback-observations\0")
    seen_observations: set[str] = set()
    total_bytes = 0
    for observation in observations:
        complete = thaw_native_value(trusted_feedback_observation_to_dict(observation))
        if not isinstance(complete, dict):
            raise InvalidRequestError("feedback observation did not serialize to an object")
        complete_fingerprint = canonical_fingerprint(complete)
        if complete_fingerprint in seen_observations:
            raise InvalidRequestError("feedback observations must be unique")
        seen_observations.add(complete_fingerprint)

        del complete["observed_at"]
        encoded = canonical_binary_encoding(complete)
        total_bytes += len(encoded)
        if total_bytes > MAX_FEEDBACK_SIGNATURE_BYTES:
            raise InvalidRequestError(
                f"feedback observations exceed the signature input limit of {MAX_FEEDBACK_SIGNATURE_BYTES} bytes"
            )
        digest.update(hashlib_sha256(encoded).digest())
    digest.update(len(observations).to_bytes(8, "big"))
    result = f"sha256:{digest.hexdigest()}"
    return result


def feedback_policy(
    minimum_verdict_samples: object = FEEDBACK_MINIMUM_VERDICT_SAMPLES,
    prior_accept: object = FEEDBACK_PRIOR_ACCEPT,
    prior_reject: object = FEEDBACK_PRIOR_REJECT,
    half_life_seconds: object = FEEDBACK_HALF_LIFE_SECONDS,
    bucket_seconds: object = FEEDBACK_BUCKET_SECONDS,
    max_buckets_per_record: object = FEEDBACK_MAX_BUCKETS_PER_RECORD,
    max_statement_records: object = FEEDBACK_MAX_STATEMENT_RECORDS,
    max_relationship_records: object = FEEDBACK_MAX_RELATIONSHIP_RECORDS,
) -> dict:
    """Build the hand-authored, unfitted aging and history policy."""
    validated_samples = internal_integer(
        minimum_verdict_samples, "feedback minimum_verdict_samples", 1, MAX_FEEDBACK_POLICY_SAMPLES
    )
    validated_prior_accept = internal_number(prior_accept, "feedback prior_accept", 0.0, MAX_FEEDBACK_PRIOR_MASS)
    validated_prior_reject = internal_number(prior_reject, "feedback prior_reject", 0.0, MAX_FEEDBACK_PRIOR_MASS)
    if validated_prior_accept + validated_prior_reject <= 0:
        raise InvalidRequestError("feedback priors must have positive total mass")
    validated_half_life = internal_integer(half_life_seconds, "feedback half_life_seconds", 1, MAX_FEEDBACK_HALF_LIFE_SECONDS)
    validated_bucket_seconds = internal_integer(bucket_seconds, "feedback bucket_seconds", 1, MAX_FEEDBACK_BUCKET_SECONDS)
    validated_max_buckets = internal_integer(max_buckets_per_record, "feedback max_buckets_per_record", 1, MAX_FEEDBACK_BUCKETS)
    validated_statement_records = internal_integer(
        max_statement_records, "feedback max_statement_records", 1, MAX_FEEDBACK_POLICY_RECORDS
    )
    validated_relationship_records = internal_integer(
        max_relationship_records, "feedback max_relationship_records", 1, MAX_FEEDBACK_POLICY_RECORDS
    )
    result: dict = {
        "minimum_verdict_samples": validated_samples,
        "prior_accept": validated_prior_accept,
        "prior_reject": validated_prior_reject,
        "half_life_seconds": validated_half_life,
        "bucket_seconds": validated_bucket_seconds,
        "max_buckets_per_record": validated_max_buckets,
        "max_statement_records": validated_statement_records,
        "max_relationship_records": validated_relationship_records,
    }
    return result


def validate_feedback_policy(value: object) -> dict:
    data = exact_mapping(value, "FeedbackPolicy", FEEDBACK_POLICY_FIELDS)
    result = feedback_policy(
        data.get("minimum_verdict_samples", 0),
        data.get("prior_accept", 0.0),
        data.get("prior_reject", 0.0),
        data.get("half_life_seconds", 0),
        data.get("bucket_seconds", 0),
        data.get("max_buckets_per_record", 0),
        data.get("max_statement_records", 0),
        data.get("max_relationship_records", 0),
    )
    return result


def feedback_policy_to_dict(value: object) -> dict:
    current = validate_feedback_policy(value)
    result = dict(current)
    return result


def feedback_policy_from_dict(value: object) -> dict:
    data = exact_mapping(value, "FeedbackPolicy", FEEDBACK_POLICY_FIELDS)
    result = feedback_policy(
        minimum_verdict_samples=internal_integer(
            data.get("minimum_verdict_samples", 0), "feedback minimum_verdict_samples", 1, MAX_FEEDBACK_POLICY_SAMPLES
        ),
        prior_accept=internal_number(data.get("prior_accept", 0.0), "feedback prior_accept", 0.0, MAX_FEEDBACK_PRIOR_MASS),
        prior_reject=internal_number(data.get("prior_reject", 0.0), "feedback prior_reject", 0.0, MAX_FEEDBACK_PRIOR_MASS),
        half_life_seconds=internal_integer(
            data.get("half_life_seconds", 0), "feedback half_life_seconds", 1, MAX_FEEDBACK_HALF_LIFE_SECONDS
        ),
        bucket_seconds=internal_integer(data.get("bucket_seconds", 0), "feedback bucket_seconds", 1, MAX_FEEDBACK_BUCKET_SECONDS),
        max_buckets_per_record=internal_integer(
            data.get("max_buckets_per_record", 0), "feedback max_buckets_per_record", 1, MAX_FEEDBACK_BUCKETS
        ),
        max_statement_records=internal_integer(
            data.get("max_statement_records", 0), "feedback max_statement_records", 1, MAX_FEEDBACK_POLICY_RECORDS
        ),
        max_relationship_records=internal_integer(
            data.get("max_relationship_records", 0), "feedback max_relationship_records", 1, MAX_FEEDBACK_POLICY_RECORDS
        ),
    )
    return result


def feedback_policy_fingerprint(policy: object = {}) -> str:
    policy_value = feedback_policy() if policy == {} else policy
    try:
        data = feedback_policy_to_dict(policy_value)
    except InvalidRequestError as err:
        raise InvalidRequestError("feedback policy fingerprint requires FeedbackPolicy") from err
    result = canonical_fingerprint(data)
    return result


def statement_feedback_key(
    statement_id: object,
    generation: object,
    generation_available: object,
    policy_fingerprint: object,
) -> dict:
    """Build one statement-lineage observation partition."""
    validated_statement_id = require_text(statement_id, "feedback statement_id", MAX_STATEMENT_ID_BYTES)
    validated_generation = internal_integer(generation, "feedback generation", 0)
    generation_is_available = require_bool(generation_available, "feedback generation_available")
    if generation_is_available and validated_generation < 1:
        raise InvalidRequestError("available feedback generation must be positive")
    if not generation_is_available and validated_generation != 0:
        raise InvalidRequestError("unavailable feedback generation must be zero")
    validated_policy = internal_fingerprint(policy_fingerprint, "feedback policy_fingerprint")
    result: dict = {
        "statement_id": validated_statement_id,
        "generation": validated_generation,
        "generation_available": generation_is_available,
        "policy_fingerprint": validated_policy,
    }
    return result


def validate_statement_feedback_key(value: object) -> dict:
    data = exact_mapping(value, "StatementFeedbackKey", STATEMENT_FEEDBACK_KEY_FIELDS)
    result = statement_feedback_key(
        data.get("statement_id", ""),
        data.get("generation", 0),
        data.get("generation_available", False),
        data.get("policy_fingerprint", ""),
    )
    return result


def statement_feedback_key_to_dict(value: object) -> dict:
    current = validate_statement_feedback_key(value)
    result = dict(current)
    return result


def statement_feedback_key_fingerprint(value: object) -> str:
    data = statement_feedback_key_to_dict(value)
    result = canonical_fingerprint(data)
    return result


def statement_feedback_key_from_dict(value: object) -> dict:
    data = exact_mapping(value, "StatementFeedbackKey", STATEMENT_FEEDBACK_KEY_FIELDS)
    result = statement_feedback_key(
        statement_id=require_text(data.get("statement_id", ""), "feedback statement_id", MAX_STATEMENT_ID_BYTES),
        generation=internal_integer(data.get("generation", 0), "feedback generation", 0),
        generation_available=require_bool(data.get("generation_available", False), "feedback generation_available"),
        policy_fingerprint=internal_fingerprint(data.get("policy_fingerprint", ""), "feedback policy_fingerprint"),
    )
    return result


def relationship_feedback_key(
    query_identity: object,
    scope: object,
    constraint_fingerprint: object,
    statement: object,
) -> dict:
    """Build one exact query, scope, constraint, and statement partition."""
    try:
        validated_query_identity = validate_query_identity(query_identity)
    except IdentityValidationError as err:
        raise InvalidRequestError("relationship query_identity must be a QueryIdentity") from err
    try:
        validated_scope = validate_scope_key(scope)
    except IdentityValidationError as err:
        raise InvalidRequestError("relationship scope must match query identity scope") from err
    if validated_query_identity.get("scope", {}) != validated_scope:
        raise InvalidRequestError("relationship scope must match query identity scope")
    validated_constraint = internal_fingerprint(constraint_fingerprint, "relationship constraint_fingerprint")
    try:
        validated_statement = validate_statement_feedback_key(statement)
    except InvalidRequestError as err:
        raise InvalidRequestError("relationship statement must be a StatementFeedbackKey") from err
    result: dict = {
        "query_identity": validated_query_identity,
        "scope": validated_scope,
        "constraint_fingerprint": validated_constraint,
        "statement": validated_statement,
    }
    return result


def validate_relationship_feedback_key(value: object) -> dict:
    data = exact_mapping(value, "RelationshipFeedbackKey", RELATIONSHIP_FEEDBACK_KEY_FIELDS)
    result = relationship_feedback_key(
        data.get("query_identity", {}),
        data.get("scope", {}),
        data.get("constraint_fingerprint", ""),
        data.get("statement", {}),
    )
    return result


def relationship_feedback_key_to_dict(value: object) -> dict:
    current = validate_relationship_feedback_key(value)
    result = {
        "query_identity": feedback_wire_value(current.get("query_identity", {})),
        "scope": feedback_wire_value(current.get("scope", {})),
        "constraint_fingerprint": current.get("constraint_fingerprint", ""),
        "statement": statement_feedback_key_to_dict(current.get("statement", {})),
    }
    return result


def relationship_feedback_key_fingerprint(value: object) -> str:
    data = relationship_feedback_key_to_dict(value)
    result = canonical_fingerprint(data)
    return result


class FeedbackDecodeMemo:
    """Decoded query identities, scopes and statement keys shared across one state decode.

    A feedback snapshot repeats the same identity, scope and statement key across many
    relationship records; decoding each distinct value once keeps the decode linear in
    the distinct values. One decode creates the memo and discards it when it finishes.
    """

    def __init__(self, statements: dict) -> None:
        if not isinstance(statements, dict):
            raise InvalidRequestError("feedback decode memo statements must be a dictionary")
        self.internal_identities: dict[str, dict] = {}
        self.internal_scopes: dict[str, dict] = {}
        self.internal_statements: dict[str, dict] = dict(statements)

    def query_identity(self, value: dict) -> dict:
        """Return the decoded query identity for ``value``, decoding it on first sight."""
        fingerprint = canonical_fingerprint(value)
        if fingerprint not in self.internal_identities:
            self.internal_identities[fingerprint] = query_identity_from_dict(value)
        result = self.internal_identities.get(fingerprint, {})
        return result

    def scope(self, value: dict) -> dict:
        """Return the decoded scope key for ``value``, decoding it on first sight."""
        fingerprint = canonical_fingerprint(value)
        if fingerprint not in self.internal_scopes:
            self.internal_scopes[fingerprint] = scope_key_from_dict(value)
        result = self.internal_scopes.get(fingerprint, {})
        return result

    def statement(self, value: dict) -> dict:
        """Return the decoded statement feedback key for ``value``, decoding it on first sight."""
        fingerprint = canonical_fingerprint(value)
        if fingerprint not in self.internal_statements:
            self.internal_statements[fingerprint] = statement_feedback_key_from_dict(value)
        result = self.internal_statements.get(fingerprint, {})
        return result


def relationship_feedback_key_from_dict(value: object, memo: object = ()) -> dict:
    """Validate and decode one relationship feedback key.

    ``memo`` is the ``FeedbackDecodeMemo`` of the state decode this key belongs to, or
    ``()`` to decode the key on its own.
    """
    data = exact_mapping(value, "RelationshipFeedbackKey", RELATIONSHIP_FEEDBACK_KEY_FIELDS)
    for name in ("query_identity", "scope", "statement"):
        if not isinstance(data.get(name, {}), dict):
            raise InvalidRequestError(f"relationship {name} must be an object")
    if isinstance(memo, FeedbackDecodeMemo):
        decoder = memo
    elif type(memo) is tuple and not memo:
        decoder = FeedbackDecodeMemo({})
    else:
        raise InvalidRequestError("relationship memo must be a FeedbackDecodeMemo")
    query_identity = decoder.query_identity(data.get("query_identity", {}))
    scope = decoder.scope(data.get("scope", {}))
    statement = decoder.statement(data.get("statement", {}))
    if query_identity.get("scope", {}) != scope:
        raise InvalidRequestError("relationship scope must match query identity scope")
    constraint = internal_fingerprint(data.get("constraint_fingerprint", ""), "relationship constraint_fingerprint")
    result: dict = {
        "query_identity": query_identity,
        "scope": scope,
        "constraint_fingerprint": constraint,
        "statement": statement,
    }
    return result


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
) -> dict:
    """Build one exactly targeted candidacy or external verdict observation."""
    if not isinstance(reference_kind, FeedbackReferenceKind):
        raise InvalidRequestError("feedback reference_kind must be a FeedbackReferenceKind")
    validated_reference_id = require_text(reference_id, "feedback reference_id", MAX_REFERENCE_ID_BYTES)
    if not isinstance(kind, FeedbackObservationKind) or not isinstance(outcome, FeedbackOutcome):
        raise InvalidRequestError("feedback kind and outcome must use the closed vocabularies")
    if kind == FeedbackObservationKind.CANDIDACY and outcome != FeedbackOutcome.CANDIDATE:
        raise InvalidRequestError("candidacy feedback must use the candidate outcome")
    if kind == FeedbackObservationKind.VERDICT and outcome == FeedbackOutcome.CANDIDATE:
        raise InvalidRequestError("verdict feedback cannot use the candidate outcome")
    try:
        validated_query_identity = validate_query_identity(query_identity)
    except IdentityValidationError as err:
        raise InvalidRequestError("feedback query_identity must be a QueryIdentity") from err
    try:
        validated_scope = validate_scope_key(scope)
    except IdentityValidationError as err:
        raise InvalidRequestError("feedback scope must match query identity scope") from err
    if validated_query_identity.get("scope", {}) != validated_scope:
        raise InvalidRequestError("feedback scope must match query identity scope")
    validated_constraint = internal_fingerprint(constraint_fingerprint, "feedback constraint_fingerprint")
    statement = statement_feedback_key(
        statement_id,
        generation,
        generation_available,
        policy_fingerprint,
    )
    validated_observed_at = canonical_utc(require_utc_datetime(observed_at, "feedback observed_at"))
    validated_reason = require_text(reason, "feedback reason", MAX_FEEDBACK_REASON_BYTES, allow_empty=True)
    result: dict = {
        "reference_kind": reference_kind,
        "reference_id": validated_reference_id,
        "kind": kind,
        "outcome": outcome,
        "query_identity": validated_query_identity,
        "scope": validated_scope,
        "constraint_fingerprint": validated_constraint,
        "statement_id": statement.get("statement_id", ""),
        "generation": statement.get("generation", 0),
        "generation_available": statement.get("generation_available", False),
        "policy_fingerprint": statement.get("policy_fingerprint", ""),
        "observed_at": validated_observed_at,
        "reason": validated_reason,
    }
    return result


def validate_feedback_observation(value: object) -> dict:
    data = exact_mapping(value, "FeedbackObservation", FEEDBACK_OBSERVATION_FIELDS)
    result = feedback_observation(
        data.get("reference_kind", FeedbackReferenceKind.RESOLUTION_REQUEST),
        data.get("reference_id", ""),
        data.get("kind", FeedbackObservationKind.CANDIDACY),
        data.get("outcome", FeedbackOutcome.CANDIDATE),
        data.get("query_identity", {}),
        data.get("scope", {}),
        data.get("constraint_fingerprint", ""),
        data.get("statement_id", ""),
        data.get("generation", 0),
        data.get("generation_available", False),
        data.get("policy_fingerprint", ""),
        data.get("observed_at", ""),
        data.get("reason", ""),
    )
    return result


def trusted_feedback_observation_to_dict(current: dict) -> dict:
    """Serialize an observation already validated by the feedback-store boundary."""
    result = {
        "reference_kind": current.get("reference_kind", FeedbackReferenceKind.RESOLUTION_REQUEST).value,
        "reference_id": current.get("reference_id", ""),
        "kind": current.get("kind", FeedbackObservationKind.CANDIDACY).value,
        "outcome": current.get("outcome", FeedbackOutcome.CANDIDATE).value,
        "query_identity": query_identity_to_dict(current.get("query_identity", {})),
        "scope": scope_key_to_dict(current.get("scope", {})),
        "constraint_fingerprint": current.get("constraint_fingerprint", ""),
        "statement_id": current.get("statement_id", ""),
        "generation": current.get("generation", 0),
        "generation_available": current.get("generation_available", False),
        "policy_fingerprint": current.get("policy_fingerprint", ""),
        "observed_at": current.get("observed_at", ""),
        "reason": current.get("reason", ""),
    }
    return result


def feedback_statistics(
    candidate_count: object = 0,
    accept_count: object = 0,
    rejected_quality: object = 0,
    rejected_context: object = 0,
    rejected_stale: object = 0,
    rejected_policy: object = 0,
) -> dict:
    """Build inspectable raw aggregate feedback counters."""
    values = {
        "candidate_count": candidate_count,
        "accept_count": accept_count,
        "rejected_quality": rejected_quality,
        "rejected_context": rejected_context,
        "rejected_stale": rejected_stale,
        "rejected_policy": rejected_policy,
    }
    validated = {name: internal_integer(value, f"feedback statistics {name}", 0) for name, value in values.items()}
    result: dict = {
        "candidate_count": validated.get("candidate_count", 0),
        "accept_count": validated.get("accept_count", 0),
        "rejected_quality": validated.get("rejected_quality", 0),
        "rejected_context": validated.get("rejected_context", 0),
        "rejected_stale": validated.get("rejected_stale", 0),
        "rejected_policy": validated.get("rejected_policy", 0),
    }
    return result


def validate_feedback_statistics(value: object) -> dict:
    data = exact_mapping(value, "FeedbackStatistics", FEEDBACK_STATISTICS_FIELDS)
    result = feedback_statistics(
        data.get("candidate_count", 0),
        data.get("accept_count", 0),
        data.get("rejected_quality", 0),
        data.get("rejected_context", 0),
        data.get("rejected_stale", 0),
        data.get("rejected_policy", 0),
    )
    return result


def feedback_statistics_increment(value: object, outcome: object) -> dict:
    current = validate_feedback_statistics(value)
    if not isinstance(outcome, FeedbackOutcome):
        raise InvalidRequestError("feedback increment outcome must be a FeedbackOutcome")
    updated: dict = dict(current)
    field_name = FEEDBACK_OUTCOME_COUNTER_FIELDS.get(outcome, "")
    if not field_name:
        raise InvalidRequestError(f"feedback outcome has no statistics counter: {outcome.value}")
    updated[field_name] = current.get(field_name, 0) + 1
    result = validate_feedback_statistics(updated)
    return result


def feedback_statistics_to_dict(value: object) -> dict[str, int]:
    current = validate_feedback_statistics(value)
    result = {
        "candidate_count": current.get("candidate_count", 0),
        "accept_count": current.get("accept_count", 0),
        "rejected_quality": current.get("rejected_quality", 0),
        "rejected_context": current.get("rejected_context", 0),
        "rejected_stale": current.get("rejected_stale", 0),
        "rejected_policy": current.get("rejected_policy", 0),
    }
    return result


def feedback_statistics_from_dict(value: object) -> dict:
    data = exact_mapping(value, "FeedbackStatistics", FEEDBACK_STATISTICS_FIELDS)
    values = {name: internal_integer(data.get(name, 0), f"feedback statistics {name}", 0) for name in FEEDBACK_STATISTICS_FIELDS}
    result = feedback_statistics(**values)
    return result


def feedback_bucket(
    start_at: object,
    statistics: object,
) -> dict:
    """Build one bounded deterministic time bucket of raw counters."""
    validated_start_at = canonical_utc(require_utc_datetime(start_at, "feedback bucket start_at"))
    try:
        validated_statistics = validate_feedback_statistics(statistics)
    except InvalidRequestError as err:
        raise InvalidRequestError("feedback bucket statistics must be FeedbackStatistics") from err
    result: dict = {
        "start_at": validated_start_at,
        "statistics": validated_statistics,
    }
    return result


def validate_feedback_bucket(value: object) -> dict:
    data = exact_mapping(value, "FeedbackBucket", FEEDBACK_BUCKET_FIELDS)
    result = feedback_bucket(data.get("start_at", ""), data.get("statistics", {}))
    return result


def feedback_bucket_from_dict(value: object) -> dict:
    data = exact_mapping(value, "FeedbackBucket", FEEDBACK_BUCKET_FIELDS)
    if not isinstance(data.get("statistics", {}), dict):
        raise InvalidRequestError("feedback bucket statistics must be an object")
    result = feedback_bucket(
        start_at=canonical_utc(require_utc_datetime(data.get("start_at", ""), "feedback bucket start_at")),
        statistics=feedback_statistics_from_dict(data.get("statistics", {})),
    )
    return result


def apply_buckets(
    buckets: tuple[dict, ...],
    outcome: FeedbackOutcome,
    observed_at: str,
    policy: dict,
) -> tuple[dict, ...]:
    start = bucket_start(observed_at, policy.get("bucket_seconds", 0))
    values = {bucket.get("start_at", ""): bucket for bucket in buckets}
    current = values.get(start, feedback_bucket(start, feedback_statistics()))
    updated_statistics = feedback_statistics_increment(current.get("statistics", {}), outcome)
    values[start] = feedback_bucket(start, updated_statistics)
    ordered = tuple(values.get(key, {}) for key in sorted(values, key=utc_datetime))
    result = ordered[-policy.get("max_buckets_per_record", 0) :]
    return result


def statement_feedback_record(
    key: object,
    raw: object,
    buckets: object,
    last_outcome: object,
    last_observed_at: object,
) -> dict:
    try:
        validated_key = validate_statement_feedback_key(key)
        validated_raw = validate_feedback_statistics(raw)
    except InvalidRequestError as err:
        raise InvalidRequestError("statement feedback record key and raw statistics have invalid types") from err
    if not isinstance(buckets, tuple):
        raise InvalidRequestError("statement feedback buckets must be a tuple of FeedbackBucket values")
    try:
        validated_buckets = tuple(validate_feedback_bucket(bucket) for bucket in buckets)
    except InvalidRequestError as err:
        raise InvalidRequestError("statement feedback buckets must be a tuple of FeedbackBucket values") from err
    bucket_starts = tuple(bucket.get("start_at", "") for bucket in validated_buckets)
    bucket_instants = tuple(utc_datetime(start) for start in bucket_starts)
    if len(validated_buckets) > MAX_FEEDBACK_BUCKETS or tuple(sorted(bucket_instants)) != bucket_instants:
        raise InvalidRequestError("statement feedback buckets must be bounded and ordered")
    if len(set(bucket_starts)) != len(bucket_starts):
        raise InvalidRequestError("statement feedback bucket starts must be unique")
    if not isinstance(last_outcome, FeedbackOutcome):
        raise InvalidRequestError("statement feedback last_outcome must be a FeedbackOutcome")
    validated_last_observed_at = canonical_utc(require_utc_datetime(last_observed_at, "statement feedback last_observed_at"))
    result: dict = {
        "key": validated_key,
        "raw": validated_raw,
        "buckets": validated_buckets,
        "last_outcome": last_outcome,
        "last_observed_at": validated_last_observed_at,
    }
    return result


def validate_statement_feedback_record(value: object) -> dict:
    data = exact_mapping(value, "StatementFeedbackRecord", FEEDBACK_RECORD_FIELDS)
    result = statement_feedback_record(
        data.get("key", {}),
        data.get("raw", {}),
        data.get("buckets", ()),
        data.get("last_outcome", FeedbackOutcome.CANDIDATE),
        data.get("last_observed_at", ""),
    )
    return result


def trusted_feedback_record_apply(
    current: dict,
    observation_key: dict,
    validated_observation: dict,
    validated_policy: dict,
) -> dict:
    """Fold one observation into a statement or relationship aggregate.

    The store validated every input and derived ``observation_key`` from the
    observation; the key comparison guards against folding the observation
    into an aggregate stored under another key.
    """
    if observation_key != current.get("key", {}):
        raise ConflictError("feedback observation does not match its aggregate key")
    outcome = validated_observation.get("outcome", FeedbackOutcome.CANDIDATE)
    observed_at = validated_observation.get("observed_at", "")
    last_observed_at, last_outcome = max(
        (current.get("last_observed_at", ""), current.get("last_outcome", FeedbackOutcome.CANDIDATE)),
        (observed_at, outcome),
        key=lambda item: (utc_datetime(item[0]), item[1].value),
    )
    raw = feedback_statistics_increment(current.get("raw", {}), outcome)
    buckets = apply_buckets(current.get("buckets", ()), outcome, observed_at, validated_policy)
    result: dict = {
        "key": current.get("key", {}),
        "raw": raw,
        "buckets": buckets,
        "last_outcome": last_outcome,
        "last_observed_at": last_observed_at,
    }
    return result


def statement_feedback_record_from_dict(value: object) -> dict:
    data = exact_mapping(value, "StatementFeedbackRecord", FEEDBACK_RECORD_FIELDS)
    if not isinstance(data.get("key", {}), dict) or not isinstance(data.get("raw", {}), dict):
        raise InvalidRequestError("statement feedback key and raw values must be objects")
    if not isinstance(data.get("buckets", ()), list) or not all(isinstance(item, dict) for item in data.get("buckets", ())):
        raise InvalidRequestError("statement feedback buckets must be an array of objects")
    try:
        last_outcome = FeedbackOutcome(data.get("last_outcome", ""))
    except (TypeError, ValueError) as err:
        raise InvalidRequestError("statement feedback contains an unsupported last_outcome") from err
    buckets = tuple(feedback_bucket_from_dict(item) for item in data.get("buckets", []))
    bucket_starts = tuple(bucket.get("start_at", "") for bucket in buckets)
    bucket_instants = tuple(utc_datetime(start) for start in bucket_starts)
    if len(buckets) > MAX_FEEDBACK_BUCKETS or tuple(sorted(bucket_instants)) != bucket_instants:
        raise InvalidRequestError("statement feedback buckets must be bounded and ordered")
    if len(set(bucket_starts)) != len(bucket_starts):
        raise InvalidRequestError("statement feedback bucket starts must be unique")
    key = statement_feedback_key_from_dict(data.get("key", {}))
    raw = feedback_statistics_from_dict(data.get("raw", {}))
    last_observed_at = require_utc_datetime(data.get("last_observed_at", ""), "statement feedback last_observed_at")
    result: dict = {
        "key": key,
        "raw": raw,
        "buckets": buckets,
        "last_outcome": last_outcome,
        "last_observed_at": canonical_utc(last_observed_at),
    }
    return result


def relationship_feedback_record(
    key: object,
    raw: object,
    buckets: object,
    last_outcome: object,
    last_observed_at: object,
) -> dict:
    try:
        validated_key = validate_relationship_feedback_key(key)
        validated_raw = validate_feedback_statistics(raw)
    except InvalidRequestError as err:
        raise InvalidRequestError("relationship feedback record key and raw statistics have invalid types") from err
    if not isinstance(buckets, tuple):
        raise InvalidRequestError("relationship feedback buckets must be a tuple of FeedbackBucket values")
    try:
        validated_buckets = tuple(validate_feedback_bucket(bucket) for bucket in buckets)
    except InvalidRequestError as err:
        raise InvalidRequestError("relationship feedback buckets must be a tuple of FeedbackBucket values") from err
    bucket_starts = tuple(bucket.get("start_at", "") for bucket in validated_buckets)
    bucket_instants = tuple(utc_datetime(start) for start in bucket_starts)
    if len(validated_buckets) > MAX_FEEDBACK_BUCKETS or tuple(sorted(bucket_instants)) != bucket_instants:
        raise InvalidRequestError("relationship feedback buckets must be bounded and ordered")
    if len(set(bucket_starts)) != len(bucket_starts):
        raise InvalidRequestError("relationship feedback bucket starts must be unique")
    if not isinstance(last_outcome, FeedbackOutcome):
        raise InvalidRequestError("relationship feedback last_outcome must be a FeedbackOutcome")
    validated_last_observed_at = canonical_utc(require_utc_datetime(last_observed_at, "relationship feedback last_observed_at"))
    result: dict = {
        "key": validated_key,
        "raw": validated_raw,
        "buckets": validated_buckets,
        "last_outcome": last_outcome,
        "last_observed_at": validated_last_observed_at,
    }
    return result


def validate_relationship_feedback_record(value: object) -> dict:
    data = exact_mapping(value, "RelationshipFeedbackRecord", FEEDBACK_RECORD_FIELDS)
    result = relationship_feedback_record(
        data.get("key", {}),
        data.get("raw", {}),
        data.get("buckets", ()),
        data.get("last_outcome", FeedbackOutcome.CANDIDATE),
        data.get("last_observed_at", ""),
    )
    return result


def relationship_feedback_record_from_dict(value: object, memo: object = ()) -> dict:
    """Validate and decode one relationship feedback record; ``memo`` is as for its key."""
    data = exact_mapping(value, "RelationshipFeedbackRecord", FEEDBACK_RECORD_FIELDS)
    if not isinstance(data.get("key", {}), dict) or not isinstance(data.get("raw", {}), dict):
        raise InvalidRequestError("relationship feedback key and raw values must be objects")
    if not isinstance(data.get("buckets", ()), list) or not all(isinstance(item, dict) for item in data.get("buckets", ())):
        raise InvalidRequestError("relationship feedback buckets must be an array of objects")
    try:
        last_outcome = FeedbackOutcome(data.get("last_outcome", ""))
    except (TypeError, ValueError) as err:
        raise InvalidRequestError("relationship feedback contains an unsupported last_outcome") from err
    buckets = tuple(feedback_bucket_from_dict(item) for item in data.get("buckets", []))
    bucket_starts = tuple(bucket.get("start_at", "") for bucket in buckets)
    bucket_instants = tuple(utc_datetime(start) for start in bucket_starts)
    if len(buckets) > MAX_FEEDBACK_BUCKETS or tuple(sorted(bucket_instants)) != bucket_instants:
        raise InvalidRequestError("relationship feedback buckets must be bounded and ordered")
    if len(set(bucket_starts)) != len(bucket_starts):
        raise InvalidRequestError("relationship feedback bucket starts must be unique")
    key = relationship_feedback_key_from_dict(data.get("key", {}), memo)
    raw = feedback_statistics_from_dict(data.get("raw", {}))
    last_observed_at = require_utc_datetime(data.get("last_observed_at", ""), "relationship feedback last_observed_at")
    result: dict = {
        "key": key,
        "raw": raw,
        "buckets": buckets,
        "last_outcome": last_outcome,
        "last_observed_at": canonical_utc(last_observed_at),
    }
    return result


def policy_suppression(statement_id: object, namespace: object, policy_fingerprint: object, observed_at: object) -> dict:
    validated_statement_id = require_text(statement_id, "policy suppression statement_id", MAX_STATEMENT_ID_BYTES)
    validated_namespace = require_text(namespace, "policy suppression namespace", MAX_NAMESPACE_BYTES, allow_empty=True)
    validated_policy = internal_fingerprint(policy_fingerprint, "policy suppression policy_fingerprint")
    validated_observed_at = canonical_utc(require_utc_datetime(observed_at, "policy suppression observed_at"))
    result: dict = {
        "statement_id": validated_statement_id,
        "namespace": validated_namespace,
        "policy_fingerprint": validated_policy,
        "observed_at": validated_observed_at,
    }
    return result


def validate_policy_suppression(value: object) -> dict:
    data = exact_mapping(value, "PolicySuppression", POLICY_SUPPRESSION_FIELDS)
    result = policy_suppression(
        data.get("statement_id", ""),
        data.get("namespace", ""),
        data.get("policy_fingerprint", ""),
        data.get("observed_at", ""),
    )
    return result


def policy_suppression_signature(value: object) -> tuple[str, str, str, str]:
    current = validate_policy_suppression(value)
    result = (
        current.get("statement_id", ""),
        current.get("namespace", ""),
        current.get("policy_fingerprint", ""),
        current.get("observed_at", ""),
    )
    return result


def policy_suppression_from_dict(value: object) -> dict:
    result = validate_policy_suppression(value)
    return result


def stale_exclusion(statement_id: object, generation: object, generation_available: object, observed_at: object) -> dict:
    key = statement_feedback_key(
        statement_id,
        generation,
        generation_available,
        EMPTY_FINGERPRINT,
    )
    validated_observed_at = canonical_utc(require_utc_datetime(observed_at, "stale exclusion observed_at"))
    result: dict = {
        "statement_id": key.get("statement_id", ""),
        "generation": key.get("generation", 0),
        "generation_available": key.get("generation_available", False),
        "observed_at": validated_observed_at,
    }
    return result


def validate_stale_exclusion(value: object) -> dict:
    data = exact_mapping(value, "StaleExclusion", STALE_EXCLUSION_FIELDS)
    result = stale_exclusion(
        data.get("statement_id", ""),
        data.get("generation", 0),
        data.get("generation_available", False),
        data.get("observed_at", ""),
    )
    return result


def stale_exclusion_signature(value: object) -> tuple[str, int, bool, str]:
    current = validate_stale_exclusion(value)
    result = (
        current.get("statement_id", ""),
        current.get("generation", 0),
        current.get("generation_available", False),
        current.get("observed_at", ""),
    )
    return result


def stale_exclusion_from_dict(value: object) -> dict:
    result = validate_stale_exclusion(value)
    return result


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
) -> dict:
    validated_value = internal_number(value, "feedback history value", 0.0, 1.0)
    validated_available = require_bool(available, "feedback history available")
    validated_statement_value = internal_number(statement_value, "feedback history statement_value", 0.0, 1.0)
    validated_statement_available = require_bool(statement_available, "feedback history statement_available")
    validated_relationship_value = internal_number(relationship_value, "feedback history relationship_value", 0.0, 1.0)
    validated_relationship_available = require_bool(relationship_available, "feedback history relationship_available")
    validated_statement_samples = internal_number(statement_samples, "feedback history statement_samples", 0.0, 1.0e18)
    validated_relationship_samples = internal_number(relationship_samples, "feedback history relationship_samples", 0.0, 1.0e18)
    validated_policy = internal_fingerprint(policy_fingerprint, "feedback history policy_fingerprint")
    if feedback_policy_value == "":
        feedback_policy_value = feedback_policy_fingerprint()
    validated_feedback_policy = internal_fingerprint(feedback_policy_value, "feedback history feedback_policy_fingerprint")
    if not validated_available and validated_value != 0.0:
        raise InvalidRequestError("unavailable feedback history must use zero value")
    result: dict = {
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


def freeze_feedback_value(value: object) -> object:
    """Recursively freeze one validated feedback contract for structural sharing."""
    if isinstance(value, dict):
        result: object = {key: freeze_feedback_value(nested) for key, nested in value.items()}
    elif isinstance(value, (tuple, list)):
        result = tuple(freeze_feedback_value(nested) for nested in value)
    else:
        result = value
    return result


def freeze_feedback_record(value: dict) -> dict:
    """Freeze a validated record while retaining its concrete record contract."""
    frozen = freeze_feedback_value(value)
    if not isinstance(frozen, dict):
        raise InvalidRequestError("validated feedback record did not remain an object")
    return frozen


def trusted_feedback_state_copy(value: dict) -> dict:
    """Defensively copy state whose invariants are already established.

    The records were validated when they entered the store, so a structural
    copy is enough; validating them again made every snapshot cost as much
    as loading the whole store.
    """
    copied = structural_copy(value)
    if not isinstance(copied, dict):
        raise InvalidRequestError("feedback state did not remain an object")
    result: dict = {
        "policy": copied.get("policy", {}),
        "statement_records": copied.get("statement_records", ()),
        "relationship_records": copied.get("relationship_records", ()),
        "policy_suppressions": copied.get("policy_suppressions", ()),
        "stale_exclusions": copied.get("stale_exclusions", ()),
        "receipts": copied.get("receipts", {}),
        "statement_evictions": value.get("statement_evictions", 0),
        "relationship_evictions": value.get("relationship_evictions", 0),
        "policy_suppression_evictions": value.get("policy_suppression_evictions", 0),
        "stale_exclusion_evictions": value.get("stale_exclusion_evictions", 0),
    }
    return result


def feedback_wire_value(value: object) -> object:
    """Project an already validated feedback value into deterministic JSON types."""
    if isinstance(value, dict):
        result: object = {key: feedback_wire_value(nested) for key, nested in value.items()}
    elif isinstance(value, (tuple, list)):
        result = [feedback_wire_value(nested) for nested in value]
    elif isinstance(value, Enum):
        result = value.value
    else:
        result = value
    return result


def feedback_wire_record(value: dict) -> dict:
    """Project a validated record while retaining its external object contract."""
    projected = feedback_wire_value(value)
    if not isinstance(projected, dict):
        raise InvalidRequestError("validated feedback record did not project to an object")
    return projected


def trusted_feedback_key_fingerprint(value: dict) -> str:
    """Fingerprint a key already validated by its record decoder or store."""
    result = canonical_fingerprint(feedback_wire_value(value))
    return result


def feedback_state_from_validated_components(
    policy: dict,
    statement_records: tuple[dict, ...],
    relationship_records: tuple[dict, ...],
    policy_suppressions: tuple[dict, ...],
    stale_exclusions: tuple[dict, ...],
    receipts: dict,
    statement_evictions: object,
    relationship_evictions: object,
    policy_suppression_evictions: object,
    stale_exclusion_evictions: object,
) -> dict:
    """Enforce state-wide invariants after every nested value was validated once."""
    if len(statement_records) > policy.get("max_statement_records", 0):
        raise InvalidRequestError("feedback statement records exceed policy capacity")
    if len(relationship_records) > policy.get("max_relationship_records", 0):
        raise InvalidRequestError("feedback relationship records exceed policy capacity")
    statement_fingerprints = tuple(trusted_feedback_key_fingerprint(record.get("key", {})) for record in statement_records)
    relationship_fingerprints = tuple(trusted_feedback_key_fingerprint(record.get("key", {})) for record in relationship_records)
    if len(set(statement_fingerprints)) != len(statement_fingerprints):
        raise InvalidRequestError("feedback statement record keys must be unique")
    if len(set(relationship_fingerprints)) != len(relationship_fingerprints):
        raise InvalidRequestError("feedback relationship record keys must be unique")
    if statement_fingerprints != tuple(sorted(statement_fingerprints)):
        raise InvalidRequestError("feedback statement records must use canonical key order")
    if relationship_fingerprints != tuple(sorted(relationship_fingerprints)):
        raise InvalidRequestError("feedback relationship records must use canonical key order")
    maximum_buckets = policy.get("max_buckets_per_record", 0)
    if any(len(record.get("buckets", ())) > maximum_buckets for record in statement_records):
        raise InvalidRequestError("feedback statement record buckets exceed policy retention")
    if any(len(record.get("buckets", ())) > maximum_buckets for record in relationship_records):
        raise InvalidRequestError("feedback relationship record buckets exceed policy retention")
    if len(policy_suppressions) > policy.get("max_statement_records", 0):
        raise InvalidRequestError("feedback policy suppressions exceed policy capacity")
    if len(stale_exclusions) > policy.get("max_statement_records", 0):
        raise InvalidRequestError("feedback stale exclusions exceed policy capacity")
    suppression_keys = tuple(
        (value.get("statement_id", ""), value.get("namespace", ""), value.get("policy_fingerprint", ""))
        for value in policy_suppressions
    )
    exclusion_keys = tuple(
        (value.get("statement_id", ""), value.get("generation", 0), value.get("generation_available", False))
        for value in stale_exclusions
    )
    if len(set(suppression_keys)) != len(suppression_keys):
        raise InvalidRequestError("feedback policy suppressions must be unique")
    if len(set(exclusion_keys)) != len(exclusion_keys):
        raise InvalidRequestError("feedback stale exclusions must be unique")
    if policy_suppressions != tuple(sorted(policy_suppressions, key=policy_suppression_signature)):
        raise InvalidRequestError("feedback policy suppressions must use canonical order")
    if stale_exclusions != tuple(sorted(stale_exclusions, key=stale_exclusion_signature)):
        raise InvalidRequestError("feedback stale exclusions must use canonical order")
    validate_mutation_receipt_ledger_state(receipts)
    receipt_copy = feedback_wire_record(receipts)
    result: dict = {
        "policy": policy,
        "statement_records": statement_records,
        "relationship_records": relationship_records,
        "policy_suppressions": policy_suppressions,
        "stale_exclusions": stale_exclusions,
        "receipts": receipt_copy,
        "statement_evictions": internal_integer(statement_evictions, "feedback statement_evictions", 0),
        "relationship_evictions": internal_integer(relationship_evictions, "feedback relationship_evictions", 0),
        "policy_suppression_evictions": internal_integer(policy_suppression_evictions, "feedback policy_suppression_evictions", 0),
        "stale_exclusion_evictions": internal_integer(stale_exclusion_evictions, "feedback stale_exclusion_evictions", 0),
    }
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
) -> dict:
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
    if not isinstance(receipts, dict):
        raise InvalidRequestError("feedback receipts must be an object")
    receipt_source = MutationReceiptLedger().snapshot() if receipts == {} else receipts
    result = feedback_state_from_validated_components(
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
    )
    return result


def validate_feedback_state(value: object) -> dict:
    data = exact_mapping(value, "FeedbackState", FEEDBACK_STATE_FIELDS)
    result = feedback_state(
        policy=data.get("policy", {}),
        statement_records=data.get("statement_records", ()),
        relationship_records=data.get("relationship_records", ()),
        policy_suppressions=data.get("policy_suppressions", ()),
        stale_exclusions=data.get("stale_exclusions", ()),
        receipts=data.get("receipts", {}),
        statement_evictions=data.get("statement_evictions", 0),
        relationship_evictions=data.get("relationship_evictions", 0),
        policy_suppression_evictions=data.get("policy_suppression_evictions", 0),
        stale_exclusion_evictions=data.get("stale_exclusion_evictions", 0),
    )
    return result


def feedback_state_to_dict(value: object) -> dict:
    current = validate_feedback_state(value)
    result = feedback_wire_record(current)
    return result


def feedback_state_to_json(value: object) -> str:
    data = feedback_state_to_dict(value)
    result = json_text(data)
    return result


def feedback_state_from_dict(value: object) -> dict:
    data = exact_mapping(value, "FeedbackState", FEEDBACK_STATE_FIELDS)
    for name in ("policy", "receipts"):
        if not isinstance(data.get(name, {}), dict):
            raise InvalidRequestError(f"feedback state {name} must be an object")
    for name in ("statement_records", "relationship_records", "policy_suppressions", "stale_exclusions"):
        values = data.get(name, ())
        if not isinstance(values, list) or not all(isinstance(item, dict) for item in values):
            raise InvalidRequestError(f"feedback state {name} must be an array of objects")
    statement_values = data.get("statement_records", [])
    relationship_values = data.get("relationship_records", [])
    suppression_values = data.get("policy_suppressions", [])
    exclusion_values = data.get("stale_exclusions", [])
    parsed_statements = tuple(
        sorted(
            (statement_feedback_record_from_dict(item) for item in statement_values),
            key=lambda record: trusted_feedback_key_fingerprint(record.get("key", {})),
        )
    )
    memo = FeedbackDecodeMemo(
        {trusted_feedback_key_fingerprint(record.get("key", {})): record.get("key", {}) for record in parsed_statements}
    )
    parsed_relationships = tuple(
        sorted(
            (relationship_feedback_record_from_dict(item, memo) for item in relationship_values),
            key=lambda record: trusted_feedback_key_fingerprint(record.get("key", {})),
        )
    )
    result = feedback_state_from_validated_components(
        feedback_policy_from_dict(data.get("policy", {})),
        parsed_statements,
        parsed_relationships,
        tuple(policy_suppression_from_dict(item) for item in suppression_values),
        tuple(stale_exclusion_from_dict(item) for item in exclusion_values),
        data.get("receipts", {}),
        data.get("statement_evictions", 0),
        data.get("relationship_evictions", 0),
        data.get("policy_suppression_evictions", 0),
        data.get("stale_exclusion_evictions", 0),
    )
    return result


def feedback_state_from_json(value: str) -> dict:
    data = load_json_mapping(value, "FeedbackState JSON")
    result = feedback_state_from_dict(data)
    return result
