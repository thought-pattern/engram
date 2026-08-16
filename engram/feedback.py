"""Versioned feedback learning and bounded negative-resolution state."""

from __future__ import annotations

import hashlib
import json
import math
import threading
from collections.abc import Mapping
from dataclasses import InitVar, dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from functools import lru_cache
from types import MappingProxyType, NoneType
from typing import Protocol, cast

from engram.errors import ConflictError, InvalidRequestError
from engram.identity import QueryIdentity, ScopeKey
from engram.mutations import (
    MutationOperation,
    MutationReceipt,
    MutationReceiptLedger,
    MutationResultCode,
    ReceiptCompletionState,
    ReceiptLookupOutcome,
)

FEEDBACK_POLICY_SCHEMA_VERSION = 1
FEEDBACK_STATISTICS_SCHEMA_VERSION = 1
FEEDBACK_KEY_SCHEMA_VERSION = 1
FEEDBACK_OBSERVATION_SCHEMA_VERSION = 1
FEEDBACK_BUCKET_SCHEMA_VERSION = 1
FEEDBACK_RECORD_SCHEMA_VERSION = 1
FEEDBACK_HISTORY_SCHEMA_VERSION = 1
FEEDBACK_STATE_SCHEMA_VERSION = 1
NEGATIVE_RESOLUTION_SCHEMA_VERSION = 1
NEGATIVE_KEY_SCHEMA_VERSION = 1

FEEDBACK_CONTRACT_VERSION = "feedback-v1.0.0"
FEEDBACK_CONTRACT_FINGERPRINT = hashlib.sha256(FEEDBACK_CONTRACT_VERSION.encode("utf-8")).hexdigest()

MAX_REFERENCE_ID_BYTES = 256
MAX_FEEDBACK_REASON_BYTES = 512
MAX_STATEMENT_ID_BYTES = 256
MAX_VERSION_BYTES = 96
MAX_FINGERPRINT_BYTES = 64
MAX_TIMESTAMP_BYTES = 40
MAX_FEEDBACK_OBSERVATIONS = 1_000
MAX_FEEDBACK_SIGNATURE_BYTES = 67_108_864
MAX_FEEDBACK_BUCKETS = 64
MAX_INSPECTION_RECORDS = 64
MAX_NEGATIVE_RECORDS = 10_000
MAX_NEGATIVE_TTL_SECONDS = 86_400
MAX_CONSTRAINT_JSON_BYTES = 65_536


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


def _exact_mapping(value: object, name: str, fields: frozenset[str]) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise InvalidRequestError(f"{name} must be an object")
    actual = frozenset(value)
    if actual != fields:
        raise InvalidRequestError(f"{name} has invalid fields: missing={sorted(fields - actual)}, extra={sorted(actual - fields)}")
    return value


def _json_text(value: object) -> str:
    return json.dumps(_thaw_json(value), sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def _thaw_json(value: object) -> object:
    if isinstance(value, Mapping):
        return {str(key): _thaw_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw_json(item) for item in value]
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

    return hashlib.sha256(_json_text(value).encode("utf-8")).hexdigest()


def _feedback_payload_signature(observations: tuple[FeedbackObservation, ...]) -> str:
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
        complete = observation.to_dict()
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
    return f"sha256:{digest.hexdigest()}"


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
    return _parse_timestamp_text(value, name)


def canonical_utc(value: datetime) -> str:
    """Serialize an aware UTC datetime using the repository's canonical form."""

    if not isinstance(value, datetime) or isinstance(value.tzinfo, NoneType):
        raise InvalidRequestError("feedback clock must return an aware datetime")
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _bucket_start(value: str, bucket_seconds: int) -> str:
    parsed = _parse_timestamp(value, "feedback observed_at")
    epoch_seconds = int(parsed.timestamp())
    start_seconds = epoch_seconds - (epoch_seconds % bucket_seconds)
    return canonical_utc(datetime.fromtimestamp(start_seconds, UTC))


def _diagnostic_id(value: str) -> str:
    return f"sha256:{hashlib.sha256(value.encode('utf-8')).hexdigest()[:24]}"


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
    return hashlib.sha256(encoded).hexdigest()


class FeedbackReferenceKind(StrEnum):
    """Stable owner of the request reference carried by feedback."""

    RESOLUTION_REQUEST = "resolution_request"
    REGULATED_PROPOSAL = "regulated_proposal"


class FeedbackObservationKind(StrEnum):
    """Whether an observation records candidacy or an external verdict."""

    CANDIDACY = "candidacy"
    VERDICT = "verdict"


class FeedbackOutcome(StrEnum):
    """Closed first-generation feedback outcome vocabulary."""

    CANDIDATE = "candidate"
    ACCEPTED = "accepted"
    REJECTED_QUALITY = "rejected_quality"
    REJECTED_CONTEXT = "rejected_context"
    REJECTED_STALE = "rejected_stale"
    REJECTED_POLICY = "rejected_policy"


class LifecycleHandoffStatus(StrEnum):
    """Inspectable result of a stale-feedback lifecycle handoff."""

    NOT_APPLICABLE = "not_applicable"
    PENDING = "pending"
    COMPLETED = "completed"
    REPLAYED = "replayed"
    CONFLICTED = "conflicted"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class FeedbackPolicy:
    """Hand-authored, unfitted aging and history policy."""

    policy_version: str = "feedback-history-v1.0.0"
    minimum_verdict_samples: int = 5
    prior_accept: float = 1.0
    prior_reject: float = 3.0
    half_life_seconds: int = 2_592_000
    bucket_seconds: int = 86_400
    max_buckets_per_record: int = 32
    max_statement_records: int = 10_000
    max_relationship_records: int = 50_000
    schema_version: int = FEEDBACK_POLICY_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != FEEDBACK_POLICY_SCHEMA_VERSION:
            raise InvalidRequestError(f"unsupported feedback policy schema_version: {self.schema_version}")
        _text(self.policy_version, "feedback policy_version", MAX_VERSION_BYTES)
        _integer(self.minimum_verdict_samples, "feedback minimum_verdict_samples", 1, 1_000_000)
        _number(self.prior_accept, "feedback prior_accept", 0.0, 1_000_000.0)
        _number(self.prior_reject, "feedback prior_reject", 0.0, 1_000_000.0)
        if self.prior_accept + self.prior_reject <= 0:
            raise InvalidRequestError("feedback priors must have positive total mass")
        _integer(self.half_life_seconds, "feedback half_life_seconds", 1, 315_576_000)
        _integer(self.bucket_seconds, "feedback bucket_seconds", 1, 31_557_600)
        _integer(self.max_buckets_per_record, "feedback max_buckets_per_record", 1, MAX_FEEDBACK_BUCKETS)
        _integer(self.max_statement_records, "feedback max_statement_records", 1, 1_000_000)
        _integer(self.max_relationship_records, "feedback max_relationship_records", 1, 1_000_000)

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "policy_version": self.policy_version,
            "minimum_verdict_samples": self.minimum_verdict_samples,
            "prior_accept": self.prior_accept,
            "prior_reject": self.prior_reject,
            "half_life_seconds": self.half_life_seconds,
            "bucket_seconds": self.bucket_seconds,
            "max_buckets_per_record": self.max_buckets_per_record,
            "max_statement_records": self.max_statement_records,
            "max_relationship_records": self.max_relationship_records,
        }

    def to_json(self) -> str:
        return _json_text(self.to_dict())

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> FeedbackPolicy:
        fields = frozenset(cls().to_dict())
        data = _exact_mapping(value, "FeedbackPolicy", fields)
        return cls(
            schema_version=_integer(data["schema_version"], "feedback policy schema_version", 1, 1),
            policy_version=_text(data["policy_version"], "feedback policy_version", MAX_VERSION_BYTES),
            minimum_verdict_samples=_integer(data["minimum_verdict_samples"], "feedback minimum_verdict_samples", 1, 1_000_000),
            prior_accept=_number(data["prior_accept"], "feedback prior_accept", 0.0, 1_000_000.0),
            prior_reject=_number(data["prior_reject"], "feedback prior_reject", 0.0, 1_000_000.0),
            half_life_seconds=_integer(data["half_life_seconds"], "feedback half_life_seconds", 1, 315_576_000),
            bucket_seconds=_integer(data["bucket_seconds"], "feedback bucket_seconds", 1, 31_557_600),
            max_buckets_per_record=_integer(
                data["max_buckets_per_record"], "feedback max_buckets_per_record", 1, MAX_FEEDBACK_BUCKETS
            ),
            max_statement_records=_integer(data["max_statement_records"], "feedback max_statement_records", 1, 1_000_000),
            max_relationship_records=_integer(data["max_relationship_records"], "feedback max_relationship_records", 1, 1_000_000),
        )

    @classmethod
    def from_json(cls, value: str) -> FeedbackPolicy:
        return cls.from_dict(_load_json_mapping(value, "FeedbackPolicy JSON"))


DEFAULT_FEEDBACK_POLICY = FeedbackPolicy()


def feedback_policy_fingerprint(policy: FeedbackPolicy = DEFAULT_FEEDBACK_POLICY) -> str:
    if not isinstance(policy, FeedbackPolicy):
        raise InvalidRequestError("feedback policy fingerprint requires FeedbackPolicy")
    return canonical_fingerprint(policy.to_dict())


@dataclass(frozen=True, order=True, slots=True)
class StatementFeedbackKey:
    """One immutable statement lineage observation partition."""

    statement_id: str
    generation: int
    generation_available: bool
    policy_fingerprint: str
    contract_fingerprint: str = FEEDBACK_CONTRACT_FINGERPRINT
    schema_version: int = FEEDBACK_KEY_SCHEMA_VERSION
    _cached_fingerprint: str = field(init=False, repr=False, compare=False)
    _fingerprint_seed: InitVar[str] = ""

    def __post_init__(self, _fingerprint_seed: str) -> None:
        if self.schema_version != FEEDBACK_KEY_SCHEMA_VERSION:
            raise InvalidRequestError(f"unsupported statement feedback key schema_version: {self.schema_version}")
        _text(self.statement_id, "feedback statement_id", MAX_STATEMENT_ID_BYTES)
        _integer(self.generation, "feedback generation", 0)
        _boolean(self.generation_available, "feedback generation_available")
        if self.generation_available and self.generation < 1:
            raise InvalidRequestError("available feedback generation must be positive")
        if not self.generation_available and self.generation != 0:
            raise InvalidRequestError("unavailable feedback generation must be zero")
        _fingerprint(self.policy_fingerprint, "feedback policy_fingerprint")
        _fingerprint(self.contract_fingerprint, "feedback contract_fingerprint")
        seeded = _fingerprint(_fingerprint_seed, "statement feedback cached fingerprint") if _fingerprint_seed else ""
        object.__setattr__(self, "_cached_fingerprint", seeded or canonical_fingerprint(self.to_dict()))

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "statement_id": self.statement_id,
            "generation": self.generation,
            "generation_available": self.generation_available,
            "policy_fingerprint": self.policy_fingerprint,
            "contract_fingerprint": self.contract_fingerprint,
        }

    def fingerprint(self) -> str:
        return self._cached_fingerprint

    def to_json(self) -> str:
        return _json_text(self.to_dict())

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> StatementFeedbackKey:
        fields = frozenset(
            {"schema_version", "statement_id", "generation", "generation_available", "policy_fingerprint", "contract_fingerprint"}
        )
        data = _exact_mapping(value, "StatementFeedbackKey", fields)
        return cls(
            schema_version=_integer(data["schema_version"], "statement feedback key schema_version", 1, 1),
            statement_id=_text(data["statement_id"], "feedback statement_id", MAX_STATEMENT_ID_BYTES),
            generation=_integer(data["generation"], "feedback generation", 0),
            generation_available=_boolean(data["generation_available"], "feedback generation_available"),
            policy_fingerprint=_fingerprint(data["policy_fingerprint"], "feedback policy_fingerprint"),
            contract_fingerprint=_fingerprint(data["contract_fingerprint"], "feedback contract_fingerprint"),
            _fingerprint_seed=canonical_fingerprint(data),
        )

    @classmethod
    def from_json(cls, value: str) -> StatementFeedbackKey:
        return cls.from_dict(_load_json_mapping(value, "StatementFeedbackKey JSON"))


@dataclass(frozen=True, slots=True)
class RelationshipFeedbackKey:
    """One exact query/scope/constraint-to-statement feedback partition."""

    query_identity: QueryIdentity
    scope: ScopeKey
    constraint_fingerprint: str
    statement: StatementFeedbackKey
    schema_version: int = FEEDBACK_KEY_SCHEMA_VERSION
    _cached_fingerprint: str = field(init=False, repr=False, compare=False)
    _fingerprint_seed: InitVar[str] = ""

    def __post_init__(self, _fingerprint_seed: str) -> None:
        if self.schema_version != FEEDBACK_KEY_SCHEMA_VERSION:
            raise InvalidRequestError(f"unsupported relationship feedback key schema_version: {self.schema_version}")
        if not isinstance(self.query_identity, QueryIdentity):
            raise InvalidRequestError("relationship query_identity must be a QueryIdentity")
        if not isinstance(self.scope, ScopeKey) or self.query_identity.scope != self.scope:
            raise InvalidRequestError("relationship scope must match query identity scope")
        _fingerprint(self.constraint_fingerprint, "relationship constraint_fingerprint")
        if not isinstance(self.statement, StatementFeedbackKey):
            raise InvalidRequestError("relationship statement must be a StatementFeedbackKey")
        seeded = _fingerprint(_fingerprint_seed, "relationship feedback cached fingerprint") if _fingerprint_seed else ""
        object.__setattr__(self, "_cached_fingerprint", seeded or canonical_fingerprint(self.to_dict()))

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "query_identity": self.query_identity.to_dict(),
            "scope": self.scope.to_dict(),
            "constraint_fingerprint": self.constraint_fingerprint,
            "statement": self.statement.to_dict(),
        }

    def fingerprint(self) -> str:
        return self._cached_fingerprint

    def to_json(self) -> str:
        return _json_text(self.to_dict())

    @classmethod
    def from_dict(
        cls,
        value: Mapping[str, object],
        identity_cache: object = (),
        scope_cache: object = (),
        statement_cache: object = (),
    ) -> RelationshipFeedbackKey:
        fields = frozenset({"schema_version", "query_identity", "scope", "constraint_fingerprint", "statement"})
        data = _exact_mapping(value, "RelationshipFeedbackKey", fields)
        for name in ("query_identity", "scope", "statement"):
            if not isinstance(data[name], Mapping):
                raise InvalidRequestError(f"relationship {name} must be an object")
        if identity_cache != () and not isinstance(identity_cache, dict):
            raise InvalidRequestError("relationship identity_cache must be a dictionary")
        if scope_cache != () and not isinstance(scope_cache, dict):
            raise InvalidRequestError("relationship scope_cache must be a dictionary")
        if statement_cache != () and not isinstance(statement_cache, dict):
            raise InvalidRequestError("relationship statement_cache must be a dictionary")
        identities = cast(dict[str, QueryIdentity], identity_cache) if isinstance(identity_cache, dict) else {}
        scopes = cast(dict[str, ScopeKey], scope_cache) if isinstance(scope_cache, dict) else {}
        statements = cast(dict[str, StatementFeedbackKey], statement_cache) if isinstance(statement_cache, dict) else {}
        identity_fingerprint = canonical_fingerprint(data["query_identity"])
        scope_fingerprint = canonical_fingerprint(data["scope"])
        statement_fingerprint = canonical_fingerprint(data["statement"])
        query_identity = identities.get(identity_fingerprint, ())
        if not isinstance(query_identity, QueryIdentity):
            query_identity = QueryIdentity.from_dict(cast(Mapping[str, object], data["query_identity"]))
            identities[identity_fingerprint] = query_identity
        scope = scopes.get(scope_fingerprint, ())
        if not isinstance(scope, ScopeKey):
            scope = ScopeKey.from_dict(cast(Mapping[str, object], data["scope"]))
            scopes[scope_fingerprint] = scope
        statement = statements.get(statement_fingerprint, ())
        if not isinstance(statement, StatementFeedbackKey):
            statement = StatementFeedbackKey.from_dict(cast(Mapping[str, object], data["statement"]))
            statements[statement_fingerprint] = statement
        return cls(
            schema_version=_integer(data["schema_version"], "relationship feedback key schema_version", 1, 1),
            query_identity=query_identity,
            scope=scope,
            constraint_fingerprint=_fingerprint(data["constraint_fingerprint"], "relationship constraint_fingerprint"),
            statement=statement,
            _fingerprint_seed=canonical_fingerprint(data),
        )

    @classmethod
    def from_json(cls, value: str) -> RelationshipFeedbackKey:
        return cls.from_dict(_load_json_mapping(value, "RelationshipFeedbackKey JSON"))


@dataclass(frozen=True, slots=True)
class FeedbackObservation:
    """One exactly targeted candidacy or external verdict observation."""

    reference_kind: FeedbackReferenceKind
    reference_id: str
    kind: FeedbackObservationKind
    outcome: FeedbackOutcome
    query_identity: QueryIdentity
    scope: ScopeKey
    constraint_fingerprint: str
    statement_id: str
    generation: int
    generation_available: bool
    policy_fingerprint: str
    observed_at: str
    reason: str = ""
    contract_fingerprint: str = FEEDBACK_CONTRACT_FINGERPRINT
    schema_version: int = FEEDBACK_OBSERVATION_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != FEEDBACK_OBSERVATION_SCHEMA_VERSION:
            raise InvalidRequestError(f"unsupported feedback observation schema_version: {self.schema_version}")
        if not isinstance(self.reference_kind, FeedbackReferenceKind):
            raise InvalidRequestError("feedback reference_kind must be a FeedbackReferenceKind")
        _text(self.reference_id, "feedback reference_id", MAX_REFERENCE_ID_BYTES)
        if not isinstance(self.kind, FeedbackObservationKind) or not isinstance(self.outcome, FeedbackOutcome):
            raise InvalidRequestError("feedback kind and outcome must use the closed vocabularies")
        if self.kind == FeedbackObservationKind.CANDIDACY and self.outcome != FeedbackOutcome.CANDIDATE:
            raise InvalidRequestError("candidacy feedback must use the candidate outcome")
        if self.kind == FeedbackObservationKind.VERDICT and self.outcome == FeedbackOutcome.CANDIDATE:
            raise InvalidRequestError("verdict feedback cannot use the candidate outcome")
        if not isinstance(self.query_identity, QueryIdentity):
            raise InvalidRequestError("feedback query_identity must be a QueryIdentity")
        if not isinstance(self.scope, ScopeKey) or self.query_identity.scope != self.scope:
            raise InvalidRequestError("feedback scope must match query identity scope")
        _fingerprint(self.constraint_fingerprint, "feedback constraint_fingerprint")
        StatementFeedbackKey(
            self.statement_id,
            self.generation,
            self.generation_available,
            self.policy_fingerprint,
            self.contract_fingerprint,
        )
        _parse_timestamp(self.observed_at, "feedback observed_at")
        _text(self.reason, "feedback reason", MAX_FEEDBACK_REASON_BYTES, allow_empty=True)

    @property
    def statement_key(self) -> StatementFeedbackKey:
        return StatementFeedbackKey(
            self.statement_id,
            self.generation,
            self.generation_available,
            self.policy_fingerprint,
            self.contract_fingerprint,
        )

    @property
    def relationship_key(self) -> RelationshipFeedbackKey:
        return RelationshipFeedbackKey(self.query_identity, self.scope, self.constraint_fingerprint, self.statement_key)

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "reference_kind": self.reference_kind.value,
            "reference_id": self.reference_id,
            "kind": self.kind.value,
            "outcome": self.outcome.value,
            "query_identity": self.query_identity.to_dict(),
            "scope": self.scope.to_dict(),
            "constraint_fingerprint": self.constraint_fingerprint,
            "statement_id": self.statement_id,
            "generation": self.generation,
            "generation_available": self.generation_available,
            "policy_fingerprint": self.policy_fingerprint,
            "contract_fingerprint": self.contract_fingerprint,
            "observed_at": self.observed_at,
            "reason": self.reason,
        }

    def to_json(self) -> str:
        return _json_text(self.to_dict())

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> FeedbackObservation:
        fields = frozenset(
            {
                "schema_version",
                "reference_kind",
                "reference_id",
                "kind",
                "outcome",
                "query_identity",
                "scope",
                "constraint_fingerprint",
                "statement_id",
                "generation",
                "generation_available",
                "policy_fingerprint",
                "contract_fingerprint",
                "observed_at",
                "reason",
            }
        )
        data = _exact_mapping(value, "FeedbackObservation", fields)
        if not isinstance(data["query_identity"], Mapping) or not isinstance(data["scope"], Mapping):
            raise InvalidRequestError("feedback observation identity and scope must be objects")
        try:
            reference_kind = FeedbackReferenceKind(data["reference_kind"])
            kind = FeedbackObservationKind(data["kind"])
            outcome = FeedbackOutcome(data["outcome"])
        except (TypeError, ValueError) as error:
            raise InvalidRequestError("feedback observation contains an unsupported enum value") from error
        return cls(
            schema_version=_integer(data["schema_version"], "feedback observation schema_version", 1, 1),
            reference_kind=reference_kind,
            reference_id=_text(data["reference_id"], "feedback reference_id", MAX_REFERENCE_ID_BYTES),
            kind=kind,
            outcome=outcome,
            query_identity=QueryIdentity.from_dict(data["query_identity"]),
            scope=ScopeKey.from_dict(data["scope"]),
            constraint_fingerprint=_fingerprint(data["constraint_fingerprint"], "feedback constraint_fingerprint"),
            statement_id=_text(data["statement_id"], "feedback statement_id", MAX_STATEMENT_ID_BYTES),
            generation=_integer(data["generation"], "feedback generation", 0),
            generation_available=_boolean(data["generation_available"], "feedback generation_available"),
            policy_fingerprint=_fingerprint(data["policy_fingerprint"], "feedback policy_fingerprint"),
            contract_fingerprint=_fingerprint(data["contract_fingerprint"], "feedback contract_fingerprint"),
            observed_at=canonical_utc(_parse_timestamp(data["observed_at"], "feedback observed_at")),
            reason=_text(data["reason"], "feedback reason", MAX_FEEDBACK_REASON_BYTES, allow_empty=True),
        )

    @classmethod
    def from_json(cls, value: str) -> FeedbackObservation:
        return cls.from_dict(_load_json_mapping(value, "FeedbackObservation JSON"))


@dataclass(frozen=True, slots=True)
class FeedbackStatistics:
    """Inspectable raw aggregate feedback counters."""

    candidate_count: int = 0
    accept_count: int = 0
    rejected_quality: int = 0
    rejected_context: int = 0
    rejected_stale: int = 0
    rejected_policy: int = 0
    schema_version: int = FEEDBACK_STATISTICS_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != FEEDBACK_STATISTICS_SCHEMA_VERSION:
            raise InvalidRequestError(f"unsupported feedback statistics schema_version: {self.schema_version}")
        for name in (
            "candidate_count",
            "accept_count",
            "rejected_quality",
            "rejected_context",
            "rejected_stale",
            "rejected_policy",
        ):
            _integer(getattr(self, name), f"feedback statistics {name}", 0)

    @property
    def verdict_count(self) -> int:
        return self.accept_count + self.rejected_quality + self.rejected_context + self.rejected_stale + self.rejected_policy

    def increment(self, outcome: FeedbackOutcome) -> FeedbackStatistics:
        if not isinstance(outcome, FeedbackOutcome):
            raise InvalidRequestError("feedback increment outcome must be a FeedbackOutcome")
        values = self.to_dict()
        del values["schema_version"]
        field_name = {
            FeedbackOutcome.CANDIDATE: "candidate_count",
            FeedbackOutcome.ACCEPTED: "accept_count",
            FeedbackOutcome.REJECTED_QUALITY: "rejected_quality",
            FeedbackOutcome.REJECTED_CONTEXT: "rejected_context",
            FeedbackOutcome.REJECTED_STALE: "rejected_stale",
            FeedbackOutcome.REJECTED_POLICY: "rejected_policy",
        }[outcome]
        values[field_name] += 1
        return FeedbackStatistics(**values)

    def add(self, other: FeedbackStatistics) -> FeedbackStatistics:
        if not isinstance(other, FeedbackStatistics):
            raise InvalidRequestError("feedback statistics can only add FeedbackStatistics")
        return FeedbackStatistics(
            candidate_count=self.candidate_count + other.candidate_count,
            accept_count=self.accept_count + other.accept_count,
            rejected_quality=self.rejected_quality + other.rejected_quality,
            rejected_context=self.rejected_context + other.rejected_context,
            rejected_stale=self.rejected_stale + other.rejected_stale,
            rejected_policy=self.rejected_policy + other.rejected_policy,
        )

    def to_dict(self) -> dict[str, int]:
        return {
            "schema_version": self.schema_version,
            "candidate_count": self.candidate_count,
            "accept_count": self.accept_count,
            "rejected_quality": self.rejected_quality,
            "rejected_context": self.rejected_context,
            "rejected_stale": self.rejected_stale,
            "rejected_policy": self.rejected_policy,
        }

    def to_json(self) -> str:
        return _json_text(self.to_dict())

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> FeedbackStatistics:
        fields = frozenset(cls().to_dict())
        data = _exact_mapping(value, "FeedbackStatistics", fields)
        return cls(**{name: _integer(data[name], f"feedback statistics {name}", 0) for name in fields})

    @classmethod
    def from_json(cls, value: str) -> FeedbackStatistics:
        return cls.from_dict(_load_json_mapping(value, "FeedbackStatistics JSON"))


@dataclass(frozen=True, slots=True)
class FeedbackBucket:
    """One bounded deterministic time bucket of raw counters."""

    start_at: str
    statistics: FeedbackStatistics
    schema_version: int = FEEDBACK_BUCKET_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != FEEDBACK_BUCKET_SCHEMA_VERSION:
            raise InvalidRequestError(f"unsupported feedback bucket schema_version: {self.schema_version}")
        _parse_timestamp(self.start_at, "feedback bucket start_at")
        if not isinstance(self.statistics, FeedbackStatistics):
            raise InvalidRequestError("feedback bucket statistics must be FeedbackStatistics")

    def to_dict(self) -> dict[str, object]:
        return {"schema_version": self.schema_version, "start_at": self.start_at, "statistics": self.statistics.to_dict()}

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> FeedbackBucket:
        data = _exact_mapping(value, "FeedbackBucket", frozenset({"schema_version", "start_at", "statistics"}))
        if not isinstance(data["statistics"], Mapping):
            raise InvalidRequestError("feedback bucket statistics must be an object")
        return cls(
            schema_version=_integer(data["schema_version"], "feedback bucket schema_version", 1, 1),
            start_at=canonical_utc(_parse_timestamp(data["start_at"], "feedback bucket start_at")),
            statistics=FeedbackStatistics.from_dict(data["statistics"]),
        )


def _apply_buckets(
    buckets: tuple[FeedbackBucket, ...],
    outcome: FeedbackOutcome,
    observed_at: str,
    policy: FeedbackPolicy,
) -> tuple[FeedbackBucket, ...]:
    start = _bucket_start(observed_at, policy.bucket_seconds)
    values = {bucket.start_at: bucket for bucket in buckets}
    current = values.get(start, FeedbackBucket(start, FeedbackStatistics()))
    values[start] = FeedbackBucket(start, current.statistics.increment(outcome))
    ordered = tuple(values[key] for key in sorted(values))
    return ordered[-policy.max_buckets_per_record :]


@dataclass(frozen=True, slots=True)
class StatementFeedbackRecord:
    key: StatementFeedbackKey
    raw: FeedbackStatistics
    buckets: tuple[FeedbackBucket, ...]
    last_outcome: FeedbackOutcome
    last_observed_at: str
    schema_version: int = FEEDBACK_RECORD_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != FEEDBACK_RECORD_SCHEMA_VERSION:
            raise InvalidRequestError(f"unsupported statement feedback record schema_version: {self.schema_version}")
        if not isinstance(self.key, StatementFeedbackKey) or not isinstance(self.raw, FeedbackStatistics):
            raise InvalidRequestError("statement feedback record key and raw statistics have invalid types")
        if not isinstance(self.buckets, tuple) or not all(isinstance(bucket, FeedbackBucket) for bucket in self.buckets):
            raise InvalidRequestError("statement feedback buckets must be a tuple of FeedbackBucket values")
        if len(self.buckets) > MAX_FEEDBACK_BUCKETS or tuple(sorted(bucket.start_at for bucket in self.buckets)) != tuple(
            bucket.start_at for bucket in self.buckets
        ):
            raise InvalidRequestError("statement feedback buckets must be bounded and ordered")
        if len({bucket.start_at for bucket in self.buckets}) != len(self.buckets):
            raise InvalidRequestError("statement feedback bucket starts must be unique")
        if not isinstance(self.last_outcome, FeedbackOutcome):
            raise InvalidRequestError("statement feedback last_outcome must be a FeedbackOutcome")
        _parse_timestamp(self.last_observed_at, "statement feedback last_observed_at")

    def apply(self, observation: FeedbackObservation, policy: FeedbackPolicy) -> StatementFeedbackRecord:
        if observation.statement_key != self.key:
            raise ConflictError("feedback observation does not match statement aggregate key")
        last_observed_at, last_outcome = max(
            (self.last_observed_at, self.last_outcome),
            (observation.observed_at, observation.outcome),
            key=lambda value: (value[0], value[1].value),
        )
        return StatementFeedbackRecord(
            self.key,
            self.raw.increment(observation.outcome),
            _apply_buckets(self.buckets, observation.outcome, observation.observed_at, policy),
            last_outcome,
            last_observed_at,
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "key": self.key.to_dict(),
            "raw": self.raw.to_dict(),
            "buckets": [bucket.to_dict() for bucket in self.buckets],
            "last_outcome": self.last_outcome.value,
            "last_observed_at": self.last_observed_at,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> StatementFeedbackRecord:
        fields = frozenset({"schema_version", "key", "raw", "buckets", "last_outcome", "last_observed_at"})
        data = _exact_mapping(value, "StatementFeedbackRecord", fields)
        if not isinstance(data["key"], Mapping) or not isinstance(data["raw"], Mapping):
            raise InvalidRequestError("statement feedback key and raw values must be objects")
        if not isinstance(data["buckets"], list) or not all(isinstance(item, Mapping) for item in data["buckets"]):
            raise InvalidRequestError("statement feedback buckets must be an array of objects")
        try:
            last_outcome = FeedbackOutcome(data["last_outcome"])
        except (TypeError, ValueError) as error:
            raise InvalidRequestError("statement feedback contains an unsupported last_outcome") from error
        return cls(
            schema_version=_integer(data["schema_version"], "statement feedback record schema_version", 1, 1),
            key=StatementFeedbackKey.from_dict(data["key"]),
            raw=FeedbackStatistics.from_dict(data["raw"]),
            buckets=tuple(FeedbackBucket.from_dict(item) for item in data["buckets"]),
            last_outcome=last_outcome,
            last_observed_at=canonical_utc(_parse_timestamp(data["last_observed_at"], "statement feedback last_observed_at")),
        )


@dataclass(frozen=True, slots=True)
class RelationshipFeedbackRecord:
    key: RelationshipFeedbackKey
    raw: FeedbackStatistics
    buckets: tuple[FeedbackBucket, ...]
    last_outcome: FeedbackOutcome
    last_observed_at: str
    schema_version: int = FEEDBACK_RECORD_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != FEEDBACK_RECORD_SCHEMA_VERSION:
            raise InvalidRequestError(f"unsupported relationship feedback record schema_version: {self.schema_version}")
        if not isinstance(self.key, RelationshipFeedbackKey) or not isinstance(self.raw, FeedbackStatistics):
            raise InvalidRequestError("relationship feedback record key and raw statistics have invalid types")
        if not isinstance(self.buckets, tuple) or not all(isinstance(bucket, FeedbackBucket) for bucket in self.buckets):
            raise InvalidRequestError("relationship feedback buckets must be a tuple of FeedbackBucket values")
        if len(self.buckets) > MAX_FEEDBACK_BUCKETS or tuple(sorted(bucket.start_at for bucket in self.buckets)) != tuple(
            bucket.start_at for bucket in self.buckets
        ):
            raise InvalidRequestError("relationship feedback buckets must be bounded and ordered")
        if len({bucket.start_at for bucket in self.buckets}) != len(self.buckets):
            raise InvalidRequestError("relationship feedback bucket starts must be unique")
        if not isinstance(self.last_outcome, FeedbackOutcome):
            raise InvalidRequestError("relationship feedback last_outcome must be a FeedbackOutcome")
        _parse_timestamp(self.last_observed_at, "relationship feedback last_observed_at")

    def apply(self, observation: FeedbackObservation, policy: FeedbackPolicy) -> RelationshipFeedbackRecord:
        if observation.relationship_key != self.key:
            raise ConflictError("feedback observation does not match relationship aggregate key")
        last_observed_at, last_outcome = max(
            (self.last_observed_at, self.last_outcome),
            (observation.observed_at, observation.outcome),
            key=lambda value: (value[0], value[1].value),
        )
        return RelationshipFeedbackRecord(
            self.key,
            self.raw.increment(observation.outcome),
            _apply_buckets(self.buckets, observation.outcome, observation.observed_at, policy),
            last_outcome,
            last_observed_at,
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "key": self.key.to_dict(),
            "raw": self.raw.to_dict(),
            "buckets": [bucket.to_dict() for bucket in self.buckets],
            "last_outcome": self.last_outcome.value,
            "last_observed_at": self.last_observed_at,
        }

    @classmethod
    def from_dict(
        cls,
        value: Mapping[str, object],
        identity_cache: object = (),
        scope_cache: object = (),
        statement_cache: object = (),
    ) -> RelationshipFeedbackRecord:
        fields = frozenset({"schema_version", "key", "raw", "buckets", "last_outcome", "last_observed_at"})
        data = _exact_mapping(value, "RelationshipFeedbackRecord", fields)
        if not isinstance(data["key"], Mapping) or not isinstance(data["raw"], Mapping):
            raise InvalidRequestError("relationship feedback key and raw values must be objects")
        if not isinstance(data["buckets"], list) or not all(isinstance(item, Mapping) for item in data["buckets"]):
            raise InvalidRequestError("relationship feedback buckets must be an array of objects")
        try:
            last_outcome = FeedbackOutcome(data["last_outcome"])
        except (TypeError, ValueError) as error:
            raise InvalidRequestError("relationship feedback contains an unsupported last_outcome") from error
        return cls(
            schema_version=_integer(data["schema_version"], "relationship feedback record schema_version", 1, 1),
            key=RelationshipFeedbackKey.from_dict(data["key"], identity_cache, scope_cache, statement_cache),
            raw=FeedbackStatistics.from_dict(data["raw"]),
            buckets=tuple(FeedbackBucket.from_dict(item) for item in data["buckets"]),
            last_outcome=last_outcome,
            last_observed_at=canonical_utc(_parse_timestamp(data["last_observed_at"], "relationship feedback last_observed_at")),
        )


@dataclass(frozen=True, order=True, slots=True)
class PolicySuppression:
    statement_id: str
    namespace: str
    policy_fingerprint: str
    observed_at: str

    def __post_init__(self) -> None:
        _text(self.statement_id, "policy suppression statement_id", MAX_STATEMENT_ID_BYTES)
        _text(self.namespace, "policy suppression namespace", 512, allow_empty=True)
        _fingerprint(self.policy_fingerprint, "policy suppression policy_fingerprint")
        _parse_timestamp(self.observed_at, "policy suppression observed_at")

    def to_dict(self) -> dict[str, object]:
        return {
            "statement_id": self.statement_id,
            "namespace": self.namespace,
            "policy_fingerprint": self.policy_fingerprint,
            "observed_at": self.observed_at,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> PolicySuppression:
        data = _exact_mapping(
            value, "PolicySuppression", frozenset({"statement_id", "namespace", "policy_fingerprint", "observed_at"})
        )
        return cls(
            statement_id=_text(data["statement_id"], "policy suppression statement_id", MAX_STATEMENT_ID_BYTES),
            namespace=_text(data["namespace"], "policy suppression namespace", 512, allow_empty=True),
            policy_fingerprint=_fingerprint(data["policy_fingerprint"], "policy suppression policy_fingerprint"),
            observed_at=canonical_utc(_parse_timestamp(data["observed_at"], "policy suppression observed_at")),
        )


@dataclass(frozen=True, order=True, slots=True)
class StaleExclusion:
    statement_id: str
    generation: int
    generation_available: bool
    observed_at: str

    def __post_init__(self) -> None:
        StatementFeedbackKey(
            self.statement_id,
            self.generation,
            self.generation_available,
            "0" * 64,
            FEEDBACK_CONTRACT_FINGERPRINT,
        )
        _parse_timestamp(self.observed_at, "stale exclusion observed_at")

    def to_dict(self) -> dict[str, object]:
        return {
            "statement_id": self.statement_id,
            "generation": self.generation,
            "generation_available": self.generation_available,
            "observed_at": self.observed_at,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> StaleExclusion:
        data = _exact_mapping(
            value, "StaleExclusion", frozenset({"statement_id", "generation", "generation_available", "observed_at"})
        )
        return cls(
            statement_id=_text(data["statement_id"], "stale exclusion statement_id", MAX_STATEMENT_ID_BYTES),
            generation=_integer(data["generation"], "stale exclusion generation", 0),
            generation_available=_boolean(data["generation_available"], "stale exclusion generation_available"),
            observed_at=canonical_utc(_parse_timestamp(data["observed_at"], "stale exclusion observed_at")),
        )


@dataclass(frozen=True, slots=True)
class FeedbackHistory:
    """One inspectable derived feedback history contribution."""

    value: float = 0.0
    available: bool = False
    statement_value: float = 0.0
    statement_available: bool = False
    relationship_value: float = 0.0
    relationship_available: bool = False
    statement_samples: float = 0.0
    relationship_samples: float = 0.0
    policy_fingerprint: str = "0" * 64
    feedback_policy_fingerprint: str = field(default_factory=feedback_policy_fingerprint)
    schema_version: int = FEEDBACK_HISTORY_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != FEEDBACK_HISTORY_SCHEMA_VERSION:
            raise InvalidRequestError(f"unsupported feedback history schema_version: {self.schema_version}")
        for name in ("value", "statement_value", "relationship_value"):
            _number(getattr(self, name), f"feedback history {name}", 0.0, 1.0)
        for name in ("available", "statement_available", "relationship_available"):
            _boolean(getattr(self, name), f"feedback history {name}")
        for name in ("statement_samples", "relationship_samples"):
            _number(getattr(self, name), f"feedback history {name}", 0.0, 1.0e18)
        _fingerprint(self.policy_fingerprint, "feedback history policy_fingerprint")
        _fingerprint(self.feedback_policy_fingerprint, "feedback history feedback_policy_fingerprint")
        if not self.available and self.value != 0.0:
            raise InvalidRequestError("unavailable feedback history must use zero value")

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "value": self.value,
            "available": self.available,
            "statement_value": self.statement_value,
            "statement_available": self.statement_available,
            "relationship_value": self.relationship_value,
            "relationship_available": self.relationship_available,
            "statement_samples": self.statement_samples,
            "relationship_samples": self.relationship_samples,
            "policy_fingerprint": self.policy_fingerprint,
            "feedback_policy_fingerprint": self.feedback_policy_fingerprint,
        }

    def to_json(self) -> str:
        return _json_text(self.to_dict())

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> FeedbackHistory:
        fields = frozenset(
            {
                "schema_version",
                "value",
                "available",
                "statement_value",
                "statement_available",
                "relationship_value",
                "relationship_available",
                "statement_samples",
                "relationship_samples",
                "policy_fingerprint",
                "feedback_policy_fingerprint",
            }
        )
        data = _exact_mapping(value, "FeedbackHistory", fields)
        return cls(
            schema_version=_integer(data["schema_version"], "feedback history schema_version", 1, 1),
            value=_number(data["value"], "feedback history value", 0.0, 1.0),
            available=_boolean(data["available"], "feedback history available"),
            statement_value=_number(data["statement_value"], "feedback history statement_value", 0.0, 1.0),
            statement_available=_boolean(data["statement_available"], "feedback history statement_available"),
            relationship_value=_number(data["relationship_value"], "feedback history relationship_value", 0.0, 1.0),
            relationship_available=_boolean(data["relationship_available"], "feedback history relationship_available"),
            statement_samples=_number(data["statement_samples"], "feedback history statement_samples", 0.0, 1.0e18),
            relationship_samples=_number(data["relationship_samples"], "feedback history relationship_samples", 0.0, 1.0e18),
            policy_fingerprint=_fingerprint(data["policy_fingerprint"], "feedback history policy_fingerprint"),
            feedback_policy_fingerprint=_fingerprint(
                data["feedback_policy_fingerprint"], "feedback history feedback_policy_fingerprint"
            ),
        )

    @classmethod
    def from_json(cls, value: str) -> FeedbackHistory:
        return cls.from_dict(_load_json_mapping(value, "FeedbackHistory JSON"))


@dataclass(frozen=True, slots=True)
class FeedbackState:
    """Complete deterministic persisted feedback state."""

    policy: FeedbackPolicy = DEFAULT_FEEDBACK_POLICY
    statement_records: tuple[StatementFeedbackRecord, ...] = ()
    relationship_records: tuple[RelationshipFeedbackRecord, ...] = ()
    policy_suppressions: tuple[PolicySuppression, ...] = ()
    stale_exclusions: tuple[StaleExclusion, ...] = ()
    receipts: Mapping[str, object] = field(default_factory=lambda: MappingProxyType(MutationReceiptLedger().snapshot()))
    statement_evictions: int = 0
    relationship_evictions: int = 0
    policy_suppression_evictions: int = 0
    stale_exclusion_evictions: int = 0
    schema_version: int = FEEDBACK_STATE_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != FEEDBACK_STATE_SCHEMA_VERSION:
            raise InvalidRequestError(f"unsupported feedback state schema_version: {self.schema_version}")
        if not isinstance(self.policy, FeedbackPolicy):
            raise InvalidRequestError("feedback state policy must be FeedbackPolicy")
        if not isinstance(self.statement_records, tuple) or not all(
            isinstance(record, StatementFeedbackRecord) for record in self.statement_records
        ):
            raise InvalidRequestError("feedback statement_records must be a tuple of StatementFeedbackRecord values")
        if not isinstance(self.relationship_records, tuple) or not all(
            isinstance(record, RelationshipFeedbackRecord) for record in self.relationship_records
        ):
            raise InvalidRequestError("feedback relationship_records must be a tuple of RelationshipFeedbackRecord values")
        if len(self.statement_records) > self.policy.max_statement_records:
            raise InvalidRequestError("feedback statement records exceed policy capacity")
        if len(self.relationship_records) > self.policy.max_relationship_records:
            raise InvalidRequestError("feedback relationship records exceed policy capacity")
        statement_fingerprints = tuple(record.key.fingerprint() for record in self.statement_records)
        relationship_fingerprints = tuple(record.key.fingerprint() for record in self.relationship_records)
        if len(set(statement_fingerprints)) != len(statement_fingerprints):
            raise InvalidRequestError("feedback statement record keys must be unique")
        if len(set(relationship_fingerprints)) != len(relationship_fingerprints):
            raise InvalidRequestError("feedback relationship record keys must be unique")
        if statement_fingerprints != tuple(sorted(statement_fingerprints)):
            raise InvalidRequestError("feedback statement records must use canonical key order")
        if relationship_fingerprints != tuple(sorted(relationship_fingerprints)):
            raise InvalidRequestError("feedback relationship records must use canonical key order")
        if any(len(record.buckets) > self.policy.max_buckets_per_record for record in self.statement_records):
            raise InvalidRequestError("feedback statement record buckets exceed policy retention")
        if any(len(record.buckets) > self.policy.max_buckets_per_record for record in self.relationship_records):
            raise InvalidRequestError("feedback relationship record buckets exceed policy retention")
        if not isinstance(self.policy_suppressions, tuple) or not all(
            isinstance(value, PolicySuppression) for value in self.policy_suppressions
        ):
            raise InvalidRequestError("feedback policy_suppressions must be a tuple of PolicySuppression values")
        if not isinstance(self.stale_exclusions, tuple) or not all(
            isinstance(value, StaleExclusion) for value in self.stale_exclusions
        ):
            raise InvalidRequestError("feedback stale_exclusions must be a tuple of StaleExclusion values")
        if len(self.policy_suppressions) > self.policy.max_statement_records:
            raise InvalidRequestError("feedback policy suppressions exceed policy capacity")
        if len(self.stale_exclusions) > self.policy.max_statement_records:
            raise InvalidRequestError("feedback stale exclusions exceed policy capacity")
        if len({(value.statement_id, value.namespace, value.policy_fingerprint) for value in self.policy_suppressions}) != len(
            self.policy_suppressions
        ):
            raise InvalidRequestError("feedback policy suppressions must be unique")
        if len({(value.statement_id, value.generation, value.generation_available) for value in self.stale_exclusions}) != len(
            self.stale_exclusions
        ):
            raise InvalidRequestError("feedback stale exclusions must be unique")
        if self.policy_suppressions != tuple(sorted(self.policy_suppressions)):
            raise InvalidRequestError("feedback policy suppressions must use canonical order")
        if self.stale_exclusions != tuple(sorted(self.stale_exclusions)):
            raise InvalidRequestError("feedback stale exclusions must use canonical order")
        if not isinstance(self.receipts, Mapping):
            raise InvalidRequestError("feedback receipts must be an object")
        MutationReceiptLedger.from_snapshot(self.receipts)
        _integer(self.statement_evictions, "feedback statement_evictions", 0)
        _integer(self.relationship_evictions, "feedback relationship_evictions", 0)
        _integer(self.policy_suppression_evictions, "feedback policy_suppression_evictions", 0)
        _integer(self.stale_exclusion_evictions, "feedback stale_exclusion_evictions", 0)
        object.__setattr__(self, "receipts", MappingProxyType(json.loads(_json_text(self.receipts))))

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "policy": self.policy.to_dict(),
            "statement_records": [record.to_dict() for record in self.statement_records],
            "relationship_records": [record.to_dict() for record in self.relationship_records],
            "policy_suppressions": [value.to_dict() for value in self.policy_suppressions],
            "stale_exclusions": [value.to_dict() for value in self.stale_exclusions],
            "receipts": json.loads(_json_text(self.receipts)),
            "statement_evictions": self.statement_evictions,
            "relationship_evictions": self.relationship_evictions,
            "policy_suppression_evictions": self.policy_suppression_evictions,
            "stale_exclusion_evictions": self.stale_exclusion_evictions,
        }

    def signature(self) -> str:
        return canonical_fingerprint(self.to_dict())

    def to_json(self) -> str:
        return _json_text(self.to_dict())

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> FeedbackState:
        fields = frozenset(
            {
                "schema_version",
                "policy",
                "statement_records",
                "relationship_records",
                "policy_suppressions",
                "stale_exclusions",
                "receipts",
                "statement_evictions",
                "relationship_evictions",
                "policy_suppression_evictions",
                "stale_exclusion_evictions",
            }
        )
        data = _exact_mapping(value, "FeedbackState", fields)
        for name in ("policy", "receipts"):
            if not isinstance(data[name], Mapping):
                raise InvalidRequestError(f"feedback state {name} must be an object")
        for name in ("statement_records", "relationship_records", "policy_suppressions", "stale_exclusions"):
            values = data[name]
            if not isinstance(values, list) or not all(isinstance(item, Mapping) for item in values):
                raise InvalidRequestError(f"feedback state {name} must be an array of objects")
        policy = cast(Mapping[str, object], data["policy"])
        receipts = cast(Mapping[str, object], data["receipts"])
        statement_records = cast(list[Mapping[str, object]], data["statement_records"])
        relationship_records = cast(list[Mapping[str, object]], data["relationship_records"])
        policy_suppressions = cast(list[Mapping[str, object]], data["policy_suppressions"])
        stale_exclusions = cast(list[Mapping[str, object]], data["stale_exclusions"])
        parsed_statement_records = tuple(StatementFeedbackRecord.from_dict(item) for item in statement_records)
        statement_cache = {record.key.fingerprint(): record.key for record in parsed_statement_records}
        identity_cache: dict[str, QueryIdentity] = {}
        scope_cache: dict[str, ScopeKey] = {}
        parsed_relationship_records = tuple(
            RelationshipFeedbackRecord.from_dict(item, identity_cache, scope_cache, statement_cache)
            for item in relationship_records
        )
        return cls(
            schema_version=_integer(data["schema_version"], "feedback state schema_version", 1, 1),
            policy=FeedbackPolicy.from_dict(policy),
            statement_records=parsed_statement_records,
            relationship_records=parsed_relationship_records,
            policy_suppressions=tuple(PolicySuppression.from_dict(item) for item in policy_suppressions),
            stale_exclusions=tuple(StaleExclusion.from_dict(item) for item in stale_exclusions),
            receipts=receipts,
            statement_evictions=_integer(data["statement_evictions"], "feedback statement_evictions", 0),
            relationship_evictions=_integer(data["relationship_evictions"], "feedback relationship_evictions", 0),
            policy_suppression_evictions=_integer(data["policy_suppression_evictions"], "feedback policy_suppression_evictions", 0),
            stale_exclusion_evictions=_integer(data["stale_exclusion_evictions"], "feedback stale_exclusion_evictions", 0),
        )

    @classmethod
    def from_json(cls, value: str) -> FeedbackState:
        return cls.from_dict(_load_json_mapping(value, "FeedbackState JSON"))


@dataclass(frozen=True, slots=True)
class FeedbackMutationCandidate:
    before: FeedbackState
    after: FeedbackState
    receipt: MutationReceipt
    replayed: bool

    def __post_init__(self) -> None:
        if not isinstance(self.before, FeedbackState) or not isinstance(self.after, FeedbackState):
            raise InvalidRequestError("feedback mutation states must be FeedbackState")
        if not isinstance(self.receipt, MutationReceipt):
            raise InvalidRequestError("feedback mutation receipt must be MutationReceipt")
        _boolean(self.replayed, "feedback mutation replayed")


def _new_statement_record(observation: FeedbackObservation, policy: FeedbackPolicy) -> StatementFeedbackRecord:
    bucket = FeedbackBucket(
        _bucket_start(observation.observed_at, policy.bucket_seconds), FeedbackStatistics().increment(observation.outcome)
    )
    return StatementFeedbackRecord(
        observation.statement_key,
        FeedbackStatistics().increment(observation.outcome),
        (bucket,),
        observation.outcome,
        observation.observed_at,
    )


def _new_relationship_record(observation: FeedbackObservation, policy: FeedbackPolicy) -> RelationshipFeedbackRecord:
    bucket = FeedbackBucket(
        _bucket_start(observation.observed_at, policy.bucket_seconds), FeedbackStatistics().increment(observation.outcome)
    )
    return RelationshipFeedbackRecord(
        observation.relationship_key,
        FeedbackStatistics().increment(observation.outcome),
        (bucket,),
        observation.outcome,
        observation.observed_at,
    )


class FeedbackRecordView(Protocol):
    """Common bounded bucket view used by both aggregate partitions."""

    @property
    def buckets(self) -> tuple[FeedbackBucket, ...]: ...


def _aged_values(
    records: tuple[FeedbackRecordView, ...],
    at: str,
    policy: FeedbackPolicy,
) -> dict[FeedbackOutcome, float]:
    evaluation = _parse_timestamp(at, "feedback history evaluation time")
    values = dict.fromkeys(FeedbackOutcome, 0.0)
    for record in records:
        for bucket in record.buckets:
            start = _parse_timestamp(bucket.start_at, "feedback bucket start_at")
            # A bucket represents its complete interval. Using its deterministic
            # end prevents same-bucket samples from falling below the integer
            # floor merely because they arrived after the bucket boundary.
            effective_at = start + timedelta(seconds=policy.bucket_seconds)
            age_seconds = max(0.0, (evaluation - effective_at).total_seconds())
            weight = 0.5 ** (age_seconds / policy.half_life_seconds)
            stats = bucket.statistics
            values[FeedbackOutcome.CANDIDATE] += stats.candidate_count * weight
            values[FeedbackOutcome.ACCEPTED] += stats.accept_count * weight
            values[FeedbackOutcome.REJECTED_QUALITY] += stats.rejected_quality * weight
            values[FeedbackOutcome.REJECTED_CONTEXT] += stats.rejected_context * weight
            values[FeedbackOutcome.REJECTED_STALE] += stats.rejected_stale * weight
            values[FeedbackOutcome.REJECTED_POLICY] += stats.rejected_policy * weight
    return values


class FeedbackStore:
    """Thread-safe authoritative owner for persisted Section 6 feedback state."""

    def __init__(self, state: object = ()) -> None:
        if state == ():
            state = FeedbackState()
        if not isinstance(state, FeedbackState):
            raise InvalidRequestError("feedback store state must be FeedbackState")
        self._lock = threading.RLock()
        self._install(state)

    def _install(self, state: FeedbackState) -> None:
        self._state = state
        self._state_dirty = False
        self._policy = state.policy
        self._statement_records = {record.key.fingerprint(): record for record in state.statement_records}
        self._relationship_records = {record.key.fingerprint(): record for record in state.relationship_records}
        self._policy_suppressions = {
            (value.statement_id, value.namespace, value.policy_fingerprint): value for value in state.policy_suppressions
        }
        self._stale_exclusions = {
            (value.statement_id, value.generation, value.generation_available): value for value in state.stale_exclusions
        }
        self._receipts = MutationReceiptLedger.from_snapshot(state.receipts)
        self._statement_evictions = state.statement_evictions
        self._relationship_evictions = state.relationship_evictions
        self._policy_suppression_evictions = state.policy_suppression_evictions
        self._stale_exclusion_evictions = state.stale_exclusion_evictions

    @property
    def policy(self) -> FeedbackPolicy:
        return self._policy

    def snapshot(self) -> FeedbackState:
        with self._lock:
            if not self._state_dirty:
                return self._state
            state = FeedbackState(
                policy=self._policy,
                statement_records=tuple(sorted(self._statement_records.values(), key=lambda record: record.key.fingerprint())),
                relationship_records=tuple(
                    sorted(self._relationship_records.values(), key=lambda record: record.key.fingerprint())
                ),
                policy_suppressions=tuple(sorted(self._policy_suppressions.values())),
                stale_exclusions=tuple(sorted(self._stale_exclusions.values())),
                receipts=self._receipts.snapshot(),
                statement_evictions=self._statement_evictions,
                relationship_evictions=self._relationship_evictions,
                policy_suppression_evictions=self._policy_suppression_evictions,
                stale_exclusion_evictions=self._stale_exclusion_evictions,
            )
            self._state = state
            self._state_dirty = False
            return state

    def _candidate_copy(self) -> FeedbackStore:
        """Create one shallow off-live owner while sharing immutable records."""

        candidate = FeedbackStore()
        candidate._state = self._state
        candidate._state_dirty = False
        candidate._policy = self._policy
        candidate._statement_records = dict(self._statement_records)
        candidate._relationship_records = dict(self._relationship_records)
        candidate._policy_suppressions = dict(self._policy_suppressions)
        candidate._stale_exclusions = dict(self._stale_exclusions)
        candidate._receipts = MutationReceiptLedger.from_snapshot(self._receipts.snapshot())
        candidate._statement_evictions = self._statement_evictions
        candidate._relationship_evictions = self._relationship_evictions
        candidate._policy_suppression_evictions = self._policy_suppression_evictions
        candidate._stale_exclusion_evictions = self._stale_exclusion_evictions
        return candidate

    def replace_from_snapshot(self, state: FeedbackState) -> None:
        if not isinstance(state, FeedbackState):
            raise InvalidRequestError("feedback replacement must be FeedbackState")
        with self._lock:
            self._install(state)

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> FeedbackStore:
        return cls(FeedbackState.from_dict(value))

    def _apply_observation(self, observation: FeedbackObservation) -> None:
        self._state_dirty = True
        statement_fingerprint = observation.statement_key.fingerprint()
        statement = self._statement_records.get(statement_fingerprint)
        self._statement_records[statement_fingerprint] = (
            statement.apply(observation, self._policy) if statement else _new_statement_record(observation, self._policy)
        )
        relationship_fingerprint = observation.relationship_key.fingerprint()
        relationship = self._relationship_records.get(relationship_fingerprint)
        self._relationship_records[relationship_fingerprint] = (
            relationship.apply(observation, self._policy) if relationship else _new_relationship_record(observation, self._policy)
        )
        suppression_key = (observation.statement_id, observation.scope.namespace, observation.policy_fingerprint)
        if observation.outcome == FeedbackOutcome.REJECTED_POLICY:
            self._policy_suppressions[suppression_key] = PolicySuppression(*suppression_key, observation.observed_at)
        elif observation.outcome == FeedbackOutcome.ACCEPTED:
            self._policy_suppressions.pop(suppression_key, {})
        if observation.outcome == FeedbackOutcome.REJECTED_STALE:
            exclusion = StaleExclusion(
                observation.statement_id,
                observation.generation,
                observation.generation_available,
                observation.observed_at,
            )
            self._stale_exclusions[(exclusion.statement_id, exclusion.generation, exclusion.generation_available)] = exclusion

    def _enforce_capacity(self) -> None:
        while len(self._statement_records) > self._policy.max_statement_records:
            oldest_key = min(
                self._statement_records,
                key=lambda key: (self._statement_records[key].last_observed_at, key),
            )
            del self._statement_records[oldest_key]
            self._statement_evictions += 1
        while len(self._relationship_records) > self._policy.max_relationship_records:
            oldest_key = min(
                self._relationship_records,
                key=lambda key: (self._relationship_records[key].last_observed_at, key),
            )
            del self._relationship_records[oldest_key]
            self._relationship_evictions += 1
        while len(self._policy_suppressions) > self._policy.max_statement_records:
            oldest_key = min(
                self._policy_suppressions,
                key=lambda key: (self._policy_suppressions[key].observed_at, key),
            )
            del self._policy_suppressions[oldest_key]
            self._policy_suppression_evictions += 1
        while len(self._stale_exclusions) > self._policy.max_statement_records:
            oldest_key = min(
                self._stale_exclusions,
                key=lambda key: (self._stale_exclusions[key].observed_at, key),
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
        if (
            not isinstance(observations, tuple)
            or not observations
            or not all(isinstance(observation, FeedbackObservation) for observation in observations)
        ):
            raise InvalidRequestError("feedback observations must be a non-empty tuple of FeedbackObservation values")
        if len(observations) > MAX_FEEDBACK_OBSERVATIONS:
            raise InvalidRequestError(f"feedback observations exceed the limit of {MAX_FEEDBACK_OBSERVATIONS}")
        if not isinstance(lifecycle_status, LifecycleHandoffStatus):
            raise InvalidRequestError("feedback lifecycle_status must be LifecycleHandoffStatus")
        signature = _feedback_payload_signature(observations)
        with self._lock:
            before = self.snapshot()
            lookup = self._receipts.lookup(request_id, MutationOperation.RECORD_FEEDBACK, signature)
            if lookup.outcome == ReceiptLookupOutcome.REPLAY:
                return FeedbackMutationCandidate(before, before, lookup.receipt(), True)
            if lookup.outcome == ReceiptLookupOutcome.CONFLICT:
                raise ConflictError(f"feedback request_id is associated with a different observation: {request_id}")
            if lookup.outcome == ReceiptLookupOutcome.IN_PROGRESS:
                raise ConflictError(f"feedback request is in progress and requires recovery: {request_id}")
            if lookup.outcome == ReceiptLookupOutcome.EXPIRED:
                raise ConflictError(f"feedback request result expired and cannot be reapplied safely: {request_id}")
            candidate = self._candidate_copy()
            for observation in observations:
                candidate._apply_observation(observation)
            candidate._enforce_capacity()
            outcomes = {outcome.value: 0 for outcome in FeedbackOutcome}
            for observation in observations:
                outcomes[observation.outcome.value] += 1
            receipt = MutationReceipt(
                sequence=candidate._receipts.next_sequence,
                request_id=request_id,
                operation=MutationOperation.RECORD_FEEDBACK,
                payload_signature=signature,
                result_code=MutationResultCode.FEEDBACK_RECORDED,
                affected_generations=(),
                result={
                    "observation_count": len(observations),
                    "outcomes": outcomes,
                    "lifecycle_status": lifecycle_status.value,
                },
                completion_state=ReceiptCompletionState.COMPLETED,
                created_at=max(observation.observed_at for observation in observations),
            )
            candidate._receipts.record(receipt)
            after = candidate.snapshot()
            return FeedbackMutationCandidate(before, after, receipt, False)

    def history(
        self,
        query_identity: QueryIdentity,
        constraint: str,
        statement_id: str,
        current_generation: int,
        policy_fingerprint: str,
        evaluation_time: str,
    ) -> FeedbackHistory:
        if not isinstance(query_identity, QueryIdentity):
            raise InvalidRequestError("feedback history query_identity must be QueryIdentity")
        _fingerprint(constraint, "feedback history constraint_fingerprint")
        _text(statement_id, "feedback history statement_id", MAX_STATEMENT_ID_BYTES)
        _integer(current_generation, "feedback history current_generation", 1)
        policy_value = _fingerprint(policy_fingerprint, "feedback history policy_fingerprint")
        _parse_timestamp(evaluation_time, "feedback history evaluation_time")
        with self._lock:
            statement_records = tuple(
                record
                for record in self._statement_records.values()
                if record.key.statement_id == statement_id
                and record.key.policy_fingerprint == policy_value
                and record.key.contract_fingerprint == FEEDBACK_CONTRACT_FINGERPRINT
                and record.key.generation_available
                and record.key.generation <= current_generation
            )
            relationship_records = tuple(
                record
                for record in self._relationship_records.values()
                if record.key.statement.statement_id == statement_id
                and record.key.statement.policy_fingerprint == policy_value
                and record.key.statement.contract_fingerprint == FEEDBACK_CONTRACT_FINGERPRINT
                and record.key.statement.generation_available
                and record.key.statement.generation <= current_generation
                and record.key.query_identity == query_identity
                and record.key.scope == query_identity.scope
                and record.key.constraint_fingerprint == constraint
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
        statement_available = statement_samples >= self._policy.minimum_verdict_samples
        relationship_available = relationship_samples >= self._policy.minimum_verdict_samples

        def posterior(accepted: float, rejected: float) -> float:
            return (accepted + self._policy.prior_accept) / (
                accepted + rejected + self._policy.prior_accept + self._policy.prior_reject
            )

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
        return FeedbackHistory(
            value=value,
            available=available,
            statement_value=statement_value,
            statement_available=statement_available,
            relationship_value=relationship_value,
            relationship_available=relationship_available,
            statement_samples=statement_samples,
            relationship_samples=relationship_samples,
            policy_fingerprint=policy_value,
            feedback_policy_fingerprint=feedback_policy_fingerprint(self._policy),
        )

    def stale_excluded(self, statement_id: str, current_generation: int, generation_available: bool = True) -> bool:
        _text(statement_id, "stale exclusion statement_id", MAX_STATEMENT_ID_BYTES)
        _integer(current_generation, "stale exclusion current_generation", 0)
        _boolean(generation_available, "stale exclusion generation_available")
        with self._lock:
            for exclusion in self._stale_exclusions.values():
                if exclusion.statement_id != statement_id:
                    continue
                if not exclusion.generation_available:
                    return not generation_available
                if generation_available and current_generation >= exclusion.generation:
                    return True
            return False

    def policy_suppressed(self, statement_id: str, namespace: str, policy_fingerprint: str) -> bool:
        _text(statement_id, "policy suppression statement_id", MAX_STATEMENT_ID_BYTES)
        _text(namespace, "policy suppression namespace", 512, allow_empty=True)
        policy_value = _fingerprint(policy_fingerprint, "policy suppression policy_fingerprint")
        with self._lock:
            return (statement_id, namespace, policy_value) in self._policy_suppressions

    def inspect(self, limit: int = MAX_INSPECTION_RECORDS) -> dict[str, object]:
        _integer(limit, "feedback inspection limit", 1, MAX_INSPECTION_RECORDS)
        with self._lock:
            statement_values = sorted(self._statement_records.values(), key=lambda record: record.key.fingerprint())
            relationship_values = sorted(self._relationship_records.values(), key=lambda record: record.key.fingerprint())
            receipt_snapshot = self._receipts.snapshot()
            receipt_values = sorted(
                cast(list[dict[str, object]], receipt_snapshot["receipts"]),
                key=lambda value: cast(int, value["sequence"]),
                reverse=True,
            )
            receipt_tombstones = cast(list[object], receipt_snapshot["tombstones"])
            return {
                "schema_version": FEEDBACK_STATE_SCHEMA_VERSION,
                "policy_version": self._policy.policy_version,
                "policy_fingerprint": feedback_policy_fingerprint(self._policy),
                "statement_record_count": len(statement_values),
                "relationship_record_count": len(relationship_values),
                "policy_suppression_count": len(self._policy_suppressions),
                "stale_exclusion_count": len(self._stale_exclusions),
                "receipt_count": len(receipt_values),
                "receipt_tombstone_count": len(receipt_tombstones),
                "receipts": [
                    {
                        "diagnostic_id": _diagnostic_id(cast(str, value["request_id"])),
                        "sequence": value["sequence"],
                        "result_code": value["result_code"],
                        "completion_state": value["completion_state"],
                        "observation_count": cast(Mapping[str, object], value["result"]).get("observation_count", 0),
                        "outcomes": cast(Mapping[str, object], value["result"]).get("outcomes", {}),
                        "lifecycle_status": cast(Mapping[str, object], value["result"]).get("lifecycle_status", ""),
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
                        "diagnostic_id": _diagnostic_id(record.key.fingerprint()),
                        "statistics": record.raw.to_dict(),
                        "bucket_count": len(record.buckets),
                        "last_outcome": record.last_outcome.value,
                        "last_observed_at": record.last_observed_at,
                    }
                    for record in statement_values[:limit]
                ],
                "omitted_statement_count": max(0, len(statement_values) - limit),
                "relationships": [
                    {
                        "diagnostic_id": _diagnostic_id(record.key.fingerprint()),
                        "statistics": record.raw.to_dict(),
                        "bucket_count": len(record.buckets),
                        "last_outcome": record.last_outcome.value,
                        "last_observed_at": record.last_observed_at,
                    }
                    for record in relationship_values[:limit]
                ],
                "omitted_relationship_count": max(0, len(relationship_values) - limit),
            }


class NegativeResolutionReason(StrEnum):
    """Only reusable first-generation negative reason."""

    INSUFFICIENT_KNOWLEDGE = "insufficient_knowledge"


@dataclass(frozen=True, slots=True)
class NegativeResolutionKey:
    """Exact knowledge and policy state under which one miss was observed."""

    query_identity: QueryIdentity
    scope: ScopeKey
    constraint_fingerprint: str
    knowledge_epoch: int
    knowledge_epoch_available: bool
    normalization_version: int
    resolver_plan_fingerprint: str
    capability_readiness_fingerprint: str
    policy_fingerprint: str
    schema_version: int = NEGATIVE_KEY_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != NEGATIVE_KEY_SCHEMA_VERSION:
            raise InvalidRequestError(f"unsupported negative key schema_version: {self.schema_version}")
        if not isinstance(self.query_identity, QueryIdentity):
            raise InvalidRequestError("negative query_identity must be QueryIdentity")
        if not isinstance(self.scope, ScopeKey) or self.query_identity.scope != self.scope:
            raise InvalidRequestError("negative scope must match query identity scope")
        _fingerprint(self.constraint_fingerprint, "negative constraint_fingerprint")
        _integer(self.knowledge_epoch, "negative knowledge_epoch", 0)
        _boolean(self.knowledge_epoch_available, "negative knowledge_epoch_available")
        if not self.knowledge_epoch_available:
            raise InvalidRequestError("negative resolution requires an available knowledge epoch")
        _integer(self.normalization_version, "negative normalization_version", 1)
        _fingerprint(self.resolver_plan_fingerprint, "negative resolver_plan_fingerprint")
        _fingerprint(self.capability_readiness_fingerprint, "negative capability_readiness_fingerprint")
        _fingerprint(self.policy_fingerprint, "negative policy_fingerprint")

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "query_identity": self.query_identity.to_dict(),
            "scope": self.scope.to_dict(),
            "constraint_fingerprint": self.constraint_fingerprint,
            "knowledge_epoch": self.knowledge_epoch,
            "knowledge_epoch_available": self.knowledge_epoch_available,
            "normalization_version": self.normalization_version,
            "resolver_plan_fingerprint": self.resolver_plan_fingerprint,
            "capability_readiness_fingerprint": self.capability_readiness_fingerprint,
            "policy_fingerprint": self.policy_fingerprint,
        }

    def fingerprint(self) -> str:
        return canonical_fingerprint(self.to_dict())

    def to_json(self) -> str:
        return _json_text(self.to_dict())

    def relationship_fingerprint(self) -> str:
        return canonical_fingerprint(
            {
                "query_identity": self.query_identity.to_dict(),
                "scope": self.scope.to_dict(),
                "constraint_fingerprint": self.constraint_fingerprint,
            }
        )

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> NegativeResolutionKey:
        fields = frozenset(
            {
                "schema_version",
                "query_identity",
                "scope",
                "constraint_fingerprint",
                "knowledge_epoch",
                "knowledge_epoch_available",
                "normalization_version",
                "resolver_plan_fingerprint",
                "capability_readiness_fingerprint",
                "policy_fingerprint",
            }
        )
        data = _exact_mapping(value, "NegativeResolutionKey", fields)
        if not isinstance(data["query_identity"], Mapping) or not isinstance(data["scope"], Mapping):
            raise InvalidRequestError("negative identity and scope must be objects")
        return cls(
            schema_version=_integer(data["schema_version"], "negative key schema_version", 1, 1),
            query_identity=QueryIdentity.from_dict(data["query_identity"]),
            scope=ScopeKey.from_dict(data["scope"]),
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

    @classmethod
    def from_json(cls, value: str) -> NegativeResolutionKey:
        return cls.from_dict(_load_json_mapping(value, "NegativeResolutionKey JSON"))


@dataclass(frozen=True, slots=True)
class NegativeResolution:
    key: NegativeResolutionKey
    reason: NegativeResolutionReason
    created_at: str
    expires_at: str
    hit_count: int = 0
    schema_version: int = NEGATIVE_RESOLUTION_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != NEGATIVE_RESOLUTION_SCHEMA_VERSION:
            raise InvalidRequestError(f"unsupported negative resolution schema_version: {self.schema_version}")
        if not isinstance(self.key, NegativeResolutionKey) or not isinstance(self.reason, NegativeResolutionReason):
            raise InvalidRequestError("negative resolution key and reason have invalid types")
        created = _parse_timestamp(self.created_at, "negative created_at")
        expires = _parse_timestamp(self.expires_at, "negative expires_at")
        if expires <= created:
            raise InvalidRequestError("negative expires_at must be after created_at")
        _integer(self.hit_count, "negative hit_count", 0)

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "key": self.key.to_dict(),
            "reason": self.reason.value,
            "created_at": self.created_at,
            "expires_at": self.expires_at,
            "hit_count": self.hit_count,
        }

    def to_json(self) -> str:
        return _json_text(self.to_dict())

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> NegativeResolution:
        data = _exact_mapping(
            value, "NegativeResolution", frozenset({"schema_version", "key", "reason", "created_at", "expires_at", "hit_count"})
        )
        if not isinstance(data["key"], Mapping):
            raise InvalidRequestError("negative key must be an object")
        try:
            reason = NegativeResolutionReason(data["reason"])
        except (TypeError, ValueError) as error:
            raise InvalidRequestError("negative resolution contains an unsupported reason") from error
        return cls(
            schema_version=_integer(data["schema_version"], "negative resolution schema_version", 1, 1),
            key=NegativeResolutionKey.from_dict(data["key"]),
            reason=reason,
            created_at=canonical_utc(_parse_timestamp(data["created_at"], "negative created_at")),
            expires_at=canonical_utc(_parse_timestamp(data["expires_at"], "negative expires_at")),
            hit_count=_integer(data["hit_count"], "negative hit_count", 0),
        )

    @classmethod
    def from_json(cls, value: str) -> NegativeResolution:
        return cls.from_dict(_load_json_mapping(value, "NegativeResolution JSON"))


@dataclass(frozen=True, slots=True)
class NegativeLookup:
    hit: bool
    record: NegativeResolution = field(
        default_factory=lambda: NegativeResolution(
            NegativeResolutionKey(
                QueryIdentity("unavailable", scope=ScopeKey()),
                ScopeKey(),
                "0" * 64,
                0,
                True,
                1,
                "0" * 64,
                "0" * 64,
                "0" * 64,
            ),
            NegativeResolutionReason.INSUFFICIENT_KNOWLEDGE,
            "1970-01-01T00:00:00Z",
            "1970-01-01T00:00:01Z",
        )
    )

    def __post_init__(self) -> None:
        _boolean(self.hit, "negative lookup hit")
        if not isinstance(self.record, NegativeResolution):
            raise InvalidRequestError("negative lookup record must be NegativeResolution")


class NegativeResolutionStore:
    """Memory-only bounded, non-sliding negative-resolution owner."""

    def __init__(self, max_records: int = 1_000, ttl_seconds: int = 300) -> None:
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
        expired = [key for key, record in self._records.items() if _parse_timestamp(record.expires_at, "negative expires_at") <= at]
        for key in expired:
            del self._records[key]
            self._metrics["expiries"] += 1

    def _invalidate_related(self, key: NegativeResolutionKey) -> None:
        relationship = key.relationship_fingerprint()
        stale = [
            record_key
            for record_key, record in self._records.items()
            if record.key.relationship_fingerprint() == relationship and record.key != key
        ]
        for record_key in stale:
            del self._records[record_key]
            self._metrics["invalidations"] += 1

    def lookup(self, key: NegativeResolutionKey, evaluation_time: str) -> NegativeLookup:
        if not isinstance(key, NegativeResolutionKey):
            raise InvalidRequestError("negative lookup key must be NegativeResolutionKey")
        at = _parse_timestamp(evaluation_time, "negative lookup evaluation_time")
        with self._lock:
            self._metrics["lookups"] += 1
            self._expire(at)
            self._invalidate_related(key)
            fingerprint = key.fingerprint()
            if fingerprint not in self._records:
                self._metrics["misses"] += 1
                return NegativeLookup(False)
            record = self._records[fingerprint]
            updated = NegativeResolution(record.key, record.reason, record.created_at, record.expires_at, record.hit_count + 1)
            self._records[fingerprint] = updated
            self._metrics["hits"] += 1
            return NegativeLookup(True, updated)

    def admit(self, key: NegativeResolutionKey, evaluation_time: str) -> NegativeResolution:
        if not isinstance(key, NegativeResolutionKey):
            raise InvalidRequestError("negative admission key must be NegativeResolutionKey")
        at = _parse_timestamp(evaluation_time, "negative admission evaluation_time")
        with self._lock:
            self._expire(at)
            self._invalidate_related(key)
            fingerprint = key.fingerprint()
            if fingerprint in self._records:
                return self._records[fingerprint]
            created_at = canonical_utc(at)
            record = NegativeResolution(
                key,
                NegativeResolutionReason.INSUFFICIENT_KNOWLEDGE,
                created_at,
                canonical_utc(at + timedelta(seconds=self.ttl_seconds)),
            )
            self._records[fingerprint] = record
            self._metrics["admissions"] += 1
            while len(self._records) > self.max_records:
                oldest_key = min(self._records, key=lambda value: (self._records[value].created_at, value))
                del self._records[oldest_key]
                self._metrics["evictions"] += 1
            return record

    def invalidate_for_key(self, key: NegativeResolutionKey) -> int:
        """Invalidate prior state for one relationship under changed policy state."""

        if not isinstance(key, NegativeResolutionKey):
            raise InvalidRequestError("negative invalidation key must be NegativeResolutionKey")
        with self._lock:
            before = len(self._records)
            self._invalidate_related(key)
            return before - len(self._records)

    def invalidate_epoch_snapshot(self, snapshot: Mapping[str, object]) -> int:
        if not isinstance(snapshot, Mapping):
            raise InvalidRequestError("negative epoch snapshot must be an object")
        epochs = snapshot.get("epochs", {})
        if not isinstance(epochs, Mapping):
            raise InvalidRequestError("negative epoch snapshot epochs must be an object")
        with self._lock:
            stale = []
            for fingerprint, record in self._records.items():
                current = epochs.get(record.key.scope.namespace, ())
                if isinstance(current, bool) or not isinstance(current, int) or current != record.key.knowledge_epoch:
                    stale.append(fingerprint)
            for fingerprint in stale:
                del self._records[fingerprint]
                self._metrics["invalidations"] += 1
            return len(stale)

    def clear(self) -> None:
        with self._lock:
            removed = len(self._records)
            self._records.clear()
            self._metrics["invalidations"] += removed

    def inspect(self, limit: int = MAX_INSPECTION_RECORDS) -> dict[str, object]:
        _integer(limit, "negative inspection limit", 1, MAX_INSPECTION_RECORDS)
        with self._lock:
            records = sorted(self._records.items())
            return {
                "schema_version": NEGATIVE_RESOLUTION_SCHEMA_VERSION,
                "memory_only": True,
                "ttl_seconds": self.ttl_seconds,
                "max_records": self.max_records,
                "record_count": len(records),
                **self._metrics,
                "records": [
                    {
                        "diagnostic_id": _diagnostic_id(fingerprint),
                        "reason": record.reason.value,
                        "created_at": record.created_at,
                        "expires_at": record.expires_at,
                        "hit_count": record.hit_count,
                    }
                    for fingerprint, record in records[:limit]
                ],
                "omitted_record_count": max(0, len(records) - limit),
            }
