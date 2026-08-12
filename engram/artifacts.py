"""Authoritative accepted-response artifact domain contracts.

Section 3 owns these transport-neutral types.  This first slice establishes
the lifecycle vocabulary and policies without depending on storage tier,
residency, persistence, indexes, or adapters.
"""

import json
import math
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from types import MappingProxyType

from engram.constants import Tier
from engram.errors import InvalidRequestError, LifecycleError
from engram.identity import QueryIdentity, RetrievalRepresentation, ScopeKey, validate_authoritative_identity

ARTIFACT_SCHEMA_VERSION = 1
ARTIFACT_PROVENANCE_SCHEMA_VERSION = 1
ARTIFACT_STATISTICS_SCHEMA_VERSION = 1
MAX_ARTIFACT_ID_BYTES = 256
MAX_RESPONSE_BYTES = 1_048_576
MAX_SOURCE_LABEL_BYTES = 256
MAX_CALLER_ID_BYTES = 256
MAX_SUPPORT_CLAIM_IDS = 256
MAX_SUPPORT_CLAIM_ID_BYTES = 256
MAX_TIMESTAMP_BYTES = 40
MAX_METADATA_BYTES = 65_536
MAX_METADATA_DEPTH = 8
MAX_METADATA_ITEMS = 1_024
MAX_METADATA_KEY_BYTES = 256
MAX_METADATA_STRING_BYTES = 16_384


class LifecycleState(StrEnum):
    """Persisted lifecycle states for accepted-response artifacts."""

    ACTIVE = "ACTIVE"
    SUPERSEDED = "SUPERSEDED"
    INVALIDATED = "INVALIDATED"
    RETIRED = "RETIRED"


class LifecycleOperation(StrEnum):
    """Named operations allowed to request a lifecycle transition."""

    SUPERSEDE = "SUPERSEDE"
    INVALIDATE = "INVALIDATE"
    RETIRE = "RETIRE"


class LifecycleDecisionReason(StrEnum):
    """Stable reasons returned by lifecycle policy decisions."""

    ELIGIBLE = "eligible"
    SUPERSEDED = "lifecycle_superseded"
    INVALIDATED = "lifecycle_invalidated"
    RETIRED = "lifecycle_retired"
    LEGAL_TRANSITION = "legal_transition"
    SAME_STATE_NOT_A_TRANSITION = "same_state_not_a_transition"
    OPERATION_TARGET_MISMATCH = "operation_target_mismatch"
    TERMINAL_STATE = "terminal_state"


class HistoricalKeyReuseReason(StrEnum):
    """Stable outcomes for the version 1 historical retrieval-key policy."""

    ALLOWED_EXPLICIT_REPLACEMENT = "allowed_explicit_replacement"
    BASE_COMMIT_FORBIDDEN = "base_commit_forbidden"
    EXPECTED_STATEMENT_ID_REQUIRED = "expected_statement_id_required"
    EXPECTED_GENERATION_REQUIRED = "expected_generation_required"


def _require_exact_mapping(value: object, name: str, keys: frozenset[str]) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise InvalidRequestError(f"{name} must be an object")
    actual = frozenset(value)
    if actual != keys:
        missing = sorted(keys - actual)
        extra = sorted(actual - keys)
        raise InvalidRequestError(f"{name} has invalid fields: missing={missing}, extra={extra}")
    return value


def _require_mapping(value: object, name: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise InvalidRequestError(f"{name} must be an object")
    return value


def _require_text(value: object, name: str, maximum_bytes: int, *, allow_empty: bool) -> str:
    if not isinstance(value, str):
        raise InvalidRequestError(f"{name} must be a string")
    if not allow_empty and not value:
        raise InvalidRequestError(f"{name} must not be empty")
    if len(value.encode("utf-8")) > maximum_bytes:
        raise InvalidRequestError(f"{name} exceeds the UTF-8 limit of {maximum_bytes} bytes")
    if any(unicodedata.category(character) in {"Cc", "Cs"} for character in value):
        raise InvalidRequestError(f"{name} contains a control or surrogate character")
    return value


def _require_response(value: object) -> str:
    if not isinstance(value, str):
        raise InvalidRequestError("artifact response must be a string")
    if not value:
        raise InvalidRequestError("artifact response must not be empty")
    if len(value.encode("utf-8")) > MAX_RESPONSE_BYTES:
        raise InvalidRequestError(f"artifact response exceeds the UTF-8 limit of {MAX_RESPONSE_BYTES} bytes")
    for character in value:
        if unicodedata.category(character) == "Cs":
            raise InvalidRequestError("artifact response contains a surrogate character")
        if unicodedata.category(character) == "Cc" and character not in "\t\n\r":
            raise InvalidRequestError("artifact response contains an unsupported control character")
    return value


def _require_bool(value: object, name: str) -> bool:
    if not isinstance(value, bool):
        raise InvalidRequestError(f"{name} must be a boolean")
    return value


def _require_positive_int(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise InvalidRequestError(f"{name} must be a positive integer")
    return value


def _require_nonnegative_int(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise InvalidRequestError(f"{name} must be a nonnegative integer")
    return value


def _require_schema_version(value: object, expected: int, name: str) -> int:
    version = _require_positive_int(value, name)
    if version != expected:
        raise InvalidRequestError(f"unsupported {name}: {version}; expected {expected}")
    return version


def _require_canonical_utc_timestamp(value: object, name: str, *, allow_empty: bool) -> str:
    text = _require_text(value, name, MAX_TIMESTAMP_BYTES, allow_empty=allow_empty)
    if not text:
        return text
    if not text.endswith("Z"):
        raise InvalidRequestError(f"{name} must be a canonical RFC 3339 UTC timestamp ending in Z")
    try:
        parsed = datetime.fromisoformat(text[:-1] + "+00:00")
    except ValueError as error:
        raise InvalidRequestError(f"{name} must be a canonical RFC 3339 UTC timestamp") from error
    canonical = parsed.isoformat().replace("+00:00", "Z")
    if canonical != text:
        raise InvalidRequestError(f"{name} must use the canonical RFC 3339 UTC representation")
    return text


def _require_present_timestamp(value: object, available: object, name: str) -> tuple[str, bool]:
    presence = _require_bool(available, f"{name}_available")
    text = _require_canonical_utc_timestamp(value, name, allow_empty=not presence)
    if presence and not text:
        raise InvalidRequestError(f"{name} must not be empty when {name}_available is true")
    if not presence and text:
        raise InvalidRequestError(f"{name} must be empty when {name}_available is false")
    return text, presence


def _freeze_json_value(value: object, name: str, depth: int, item_count: list[int]) -> object:
    if depth > MAX_METADATA_DEPTH:
        raise InvalidRequestError(f"{name} exceeds the metadata depth limit of {MAX_METADATA_DEPTH}")
    item_count[0] += 1
    if item_count[0] > MAX_METADATA_ITEMS:
        raise InvalidRequestError(f"{name} exceeds the metadata item limit of {MAX_METADATA_ITEMS}")
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return _require_text(value, name, MAX_METADATA_STRING_BYTES, allow_empty=True)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise InvalidRequestError(f"{name} must not contain a non-finite number")
        return value
    if isinstance(value, Mapping):
        validated_items = []
        for key, item in value.items():
            validated_key = _require_text(key, f"{name} key", MAX_METADATA_KEY_BYTES, allow_empty=False)
            validated_items.append((validated_key, item))
        frozen = {}
        for validated_key, item in sorted(validated_items, key=lambda pair: pair[0]):
            frozen[validated_key] = _freeze_json_value(item, f"{name}.{validated_key}", depth + 1, item_count)
        return MappingProxyType(frozen)
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_json_value(item, f"{name}[{position}]", depth + 1, item_count) for position, item in enumerate(value))
    raise InvalidRequestError(f"{name} contains an unsupported JSON value")


def _freeze_metadata(value: object) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise InvalidRequestError("artifact metadata must be an object")
    frozen = _freeze_json_value(value, "artifact metadata", 0, [0])
    if not isinstance(frozen, Mapping):
        raise InvalidRequestError("artifact metadata must be an object")
    encoded = json.dumps(_thaw_json_value(frozen), ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    if len(encoded.encode("utf-8")) > MAX_METADATA_BYTES:
        raise InvalidRequestError(f"artifact metadata exceeds the UTF-8 limit of {MAX_METADATA_BYTES} bytes")
    return frozen


def _thaw_json_value(value: object) -> object:
    if isinstance(value, Mapping):
        return {key: _thaw_json_value(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw_json_value(item) for item in value]
    return value


def _json_text(value: Mapping[str, object]) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


@dataclass(frozen=True, slots=True)
class ArtifactProvenance:
    """Bounded provenance recorded with one accepted response."""

    source_label: str
    caller_id: str
    accepted_at: str
    schema_version: int = ARTIFACT_PROVENANCE_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _require_schema_version(
            self.schema_version,
            ARTIFACT_PROVENANCE_SCHEMA_VERSION,
            "artifact provenance schema_version",
        )
        _require_text(self.source_label, "artifact provenance source_label", MAX_SOURCE_LABEL_BYTES, allow_empty=True)
        _require_text(self.caller_id, "artifact provenance caller_id", MAX_CALLER_ID_BYTES, allow_empty=True)
        _require_canonical_utc_timestamp(self.accepted_at, "artifact provenance accepted_at", allow_empty=False)

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "source_label": self.source_label,
            "caller_id": self.caller_id,
            "accepted_at": self.accepted_at,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> "ArtifactProvenance":
        keys = frozenset({"schema_version", "source_label", "caller_id", "accepted_at"})
        data = _require_exact_mapping(value, "ArtifactProvenance", keys)
        return cls(
            schema_version=_require_schema_version(
                data["schema_version"],
                ARTIFACT_PROVENANCE_SCHEMA_VERSION,
                "artifact provenance schema_version",
            ),
            source_label=_require_text(
                data["source_label"],
                "artifact provenance source_label",
                MAX_SOURCE_LABEL_BYTES,
                allow_empty=True,
            ),
            caller_id=_require_text(
                data["caller_id"],
                "artifact provenance caller_id",
                MAX_CALLER_ID_BYTES,
                allow_empty=True,
            ),
            accepted_at=_require_canonical_utc_timestamp(
                data["accepted_at"],
                "artifact provenance accepted_at",
                allow_empty=False,
            ),
        )


@dataclass(frozen=True, slots=True)
class ArtifactStatistics:
    """Authoritative response statistics carried through compatibility views."""

    hit_count: int = 0
    query_count: int = 0
    last_hit: str = ""
    last_hit_available: bool = False
    schema_version: int = ARTIFACT_STATISTICS_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _require_schema_version(
            self.schema_version,
            ARTIFACT_STATISTICS_SCHEMA_VERSION,
            "artifact statistics schema_version",
        )
        _require_nonnegative_int(self.hit_count, "artifact statistics hit_count")
        _require_nonnegative_int(self.query_count, "artifact statistics query_count")
        _require_present_timestamp(self.last_hit, self.last_hit_available, "artifact statistics last_hit")

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "hit_count": self.hit_count,
            "query_count": self.query_count,
            "last_hit": self.last_hit,
            "last_hit_available": self.last_hit_available,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> "ArtifactStatistics":
        keys = frozenset({"schema_version", "hit_count", "query_count", "last_hit", "last_hit_available"})
        data = _require_exact_mapping(value, "ArtifactStatistics", keys)
        return cls(
            schema_version=_require_schema_version(
                data["schema_version"],
                ARTIFACT_STATISTICS_SCHEMA_VERSION,
                "artifact statistics schema_version",
            ),
            hit_count=_require_nonnegative_int(data["hit_count"], "artifact statistics hit_count"),
            query_count=_require_nonnegative_int(data["query_count"], "artifact statistics query_count"),
            last_hit=_require_canonical_utc_timestamp(
                data["last_hit"],
                "artifact statistics last_hit",
                allow_empty=True,
            ),
            last_hit_available=_require_bool(
                data["last_hit_available"],
                "artifact statistics last_hit_available",
            ),
        )


@dataclass(frozen=True, slots=True)
class CachedResponseArtifact:
    """Authoritative version 1 record for one accepted response."""

    statement_id: str
    generation: int
    response: str
    query_identity: QueryIdentity
    retrieval: RetrievalRepresentation
    tier: Tier
    lifecycle: LifecycleState
    scope: ScopeKey
    support_claim_ids: tuple[str, ...]
    valid_from: str
    valid_from_available: bool
    valid_until: str
    valid_until_available: bool
    knowledge_epoch: int
    knowledge_epoch_available: bool
    superseded_by: str
    provenance: ArtifactProvenance
    statistics: ArtifactStatistics = field(default_factory=ArtifactStatistics)
    metadata: Mapping[str, object] = field(default_factory=dict)
    schema_version: int = ARTIFACT_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _require_schema_version(self.schema_version, ARTIFACT_SCHEMA_VERSION, "artifact schema_version")
        statement_id = _require_text(
            self.statement_id,
            "artifact statement_id",
            MAX_ARTIFACT_ID_BYTES,
            allow_empty=False,
        )
        _require_positive_int(self.generation, "artifact generation")
        _require_response(self.response)
        if not isinstance(self.query_identity, QueryIdentity):
            raise InvalidRequestError("artifact query_identity must be a QueryIdentity")
        if not isinstance(self.retrieval, RetrievalRepresentation):
            raise InvalidRequestError("artifact retrieval must be a RetrievalRepresentation")
        validate_authoritative_identity(self.query_identity, self.retrieval)
        if not isinstance(self.tier, Tier):
            raise InvalidRequestError("artifact tier must be a Tier")
        lifecycle = _require_lifecycle(self.lifecycle, "artifact lifecycle")
        if not isinstance(self.scope, ScopeKey):
            raise InvalidRequestError("artifact scope must be a ScopeKey")
        if self.scope != self.query_identity.scope:
            raise InvalidRequestError("artifact scope must match query_identity scope")
        if not isinstance(self.support_claim_ids, tuple):
            raise InvalidRequestError("artifact support_claim_ids must be a tuple")
        if len(self.support_claim_ids) > MAX_SUPPORT_CLAIM_IDS:
            raise InvalidRequestError(f"artifact support_claim_ids exceed the limit of {MAX_SUPPORT_CLAIM_IDS}")
        support = tuple(
            sorted(
                {
                    _require_text(
                        claim_id,
                        "artifact support Claim ID",
                        MAX_SUPPORT_CLAIM_ID_BYTES,
                        allow_empty=False,
                    )
                    for claim_id in self.support_claim_ids
                }
            )
        )
        object.__setattr__(self, "support_claim_ids", support)
        _require_present_timestamp(self.valid_from, self.valid_from_available, "artifact valid_from")
        _require_present_timestamp(self.valid_until, self.valid_until_available, "artifact valid_until")
        epoch = _require_nonnegative_int(self.knowledge_epoch, "artifact knowledge_epoch")
        epoch_available = _require_bool(self.knowledge_epoch_available, "artifact knowledge_epoch_available")
        if not epoch_available and epoch != 0:
            raise InvalidRequestError("artifact knowledge_epoch must be 0 when unavailable")
        superseded_by = _require_text(
            self.superseded_by,
            "artifact superseded_by",
            MAX_ARTIFACT_ID_BYTES,
            allow_empty=True,
        )
        if lifecycle == LifecycleState.ACTIVE and superseded_by:
            raise InvalidRequestError("an ACTIVE artifact must not name superseded_by")
        if lifecycle == LifecycleState.SUPERSEDED and not superseded_by:
            raise InvalidRequestError("a SUPERSEDED artifact must name superseded_by")
        if superseded_by == statement_id:
            raise InvalidRequestError("artifact superseded_by must not reference itself")
        if not isinstance(self.provenance, ArtifactProvenance):
            raise InvalidRequestError("artifact provenance must be an ArtifactProvenance")
        if not isinstance(self.statistics, ArtifactStatistics):
            raise InvalidRequestError("artifact statistics must be ArtifactStatistics")
        object.__setattr__(self, "metadata", _freeze_metadata(self.metadata))

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "statement_id": self.statement_id,
            "generation": self.generation,
            "response": self.response,
            "query_identity": self.query_identity.to_dict(),
            "retrieval": self.retrieval.to_dict(),
            "tier": self.tier.value,
            "lifecycle": self.lifecycle.value,
            "scope": self.scope.to_dict(),
            "support_claim_ids": list(self.support_claim_ids),
            "valid_from": self.valid_from,
            "valid_from_available": self.valid_from_available,
            "valid_until": self.valid_until,
            "valid_until_available": self.valid_until_available,
            "knowledge_epoch": self.knowledge_epoch,
            "knowledge_epoch_available": self.knowledge_epoch_available,
            "superseded_by": self.superseded_by,
            "provenance": self.provenance.to_dict(),
            "statistics": self.statistics.to_dict(),
            "metadata": _thaw_json_value(self.metadata),
        }

    def to_json(self) -> str:
        return _json_text(self.to_dict())

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> "CachedResponseArtifact":
        keys = frozenset(
            {
                "schema_version",
                "statement_id",
                "generation",
                "response",
                "query_identity",
                "retrieval",
                "tier",
                "lifecycle",
                "scope",
                "support_claim_ids",
                "valid_from",
                "valid_from_available",
                "valid_until",
                "valid_until_available",
                "knowledge_epoch",
                "knowledge_epoch_available",
                "superseded_by",
                "provenance",
                "statistics",
                "metadata",
            }
        )
        data = _require_exact_mapping(value, "CachedResponseArtifact", keys)
        tier_value = _require_text(data["tier"], "artifact tier", 16, allow_empty=False)
        lifecycle_value = _require_text(data["lifecycle"], "artifact lifecycle", 16, allow_empty=False)
        try:
            tier = Tier(tier_value)
        except ValueError as error:
            raise InvalidRequestError(f"unsupported artifact tier: {tier_value}") from error
        try:
            lifecycle = LifecycleState(lifecycle_value)
        except ValueError as error:
            raise InvalidRequestError(f"unsupported artifact lifecycle: {lifecycle_value}") from error
        raw_support = data["support_claim_ids"]
        if not isinstance(raw_support, list):
            raise InvalidRequestError("artifact support_claim_ids must be an array")
        metadata = data["metadata"]
        if not isinstance(metadata, Mapping):
            raise InvalidRequestError("artifact metadata must be an object")
        query_identity = _require_mapping(data["query_identity"], "artifact query_identity")
        retrieval = _require_mapping(data["retrieval"], "artifact retrieval")
        scope = _require_mapping(data["scope"], "artifact scope")
        provenance = _require_mapping(data["provenance"], "artifact provenance")
        statistics = _require_mapping(data["statistics"], "artifact statistics")
        return cls(
            schema_version=_require_schema_version(data["schema_version"], ARTIFACT_SCHEMA_VERSION, "artifact schema_version"),
            statement_id=_require_text(
                data["statement_id"],
                "artifact statement_id",
                MAX_ARTIFACT_ID_BYTES,
                allow_empty=False,
            ),
            generation=_require_positive_int(data["generation"], "artifact generation"),
            response=_require_response(data["response"]),
            query_identity=QueryIdentity.from_dict(query_identity),
            retrieval=RetrievalRepresentation.from_dict(retrieval),
            tier=tier,
            lifecycle=lifecycle,
            scope=ScopeKey.from_dict(scope),
            support_claim_ids=tuple(raw_support),
            valid_from=_require_canonical_utc_timestamp(data["valid_from"], "artifact valid_from", allow_empty=True),
            valid_from_available=_require_bool(data["valid_from_available"], "artifact valid_from_available"),
            valid_until=_require_canonical_utc_timestamp(data["valid_until"], "artifact valid_until", allow_empty=True),
            valid_until_available=_require_bool(data["valid_until_available"], "artifact valid_until_available"),
            knowledge_epoch=_require_nonnegative_int(data["knowledge_epoch"], "artifact knowledge_epoch"),
            knowledge_epoch_available=_require_bool(
                data["knowledge_epoch_available"],
                "artifact knowledge_epoch_available",
            ),
            superseded_by=_require_text(
                data["superseded_by"],
                "artifact superseded_by",
                MAX_ARTIFACT_ID_BYTES,
                allow_empty=True,
            ),
            provenance=ArtifactProvenance.from_dict(provenance),
            statistics=ArtifactStatistics.from_dict(statistics),
            metadata=metadata,
        )

    @classmethod
    def from_json(cls, value: str) -> "CachedResponseArtifact":
        if not isinstance(value, str):
            raise InvalidRequestError("CachedResponseArtifact JSON must be a string")
        try:
            data = json.loads(value)
        except json.JSONDecodeError as error:
            raise InvalidRequestError("CachedResponseArtifact JSON is malformed") from error
        if not isinstance(data, Mapping):
            raise InvalidRequestError("CachedResponseArtifact JSON must contain an object")
        return cls.from_dict(data)


@dataclass(frozen=True, slots=True)
class LifecycleBaseDecision:
    """Lifecycle-only direct-answer eligibility, independent of tier."""

    lifecycle: LifecycleState
    direct_answer_eligible: bool
    reason: LifecycleDecisionReason

    def to_dict(self) -> dict[str, object]:
        return {
            "lifecycle": self.lifecycle.value,
            "direct_answer_eligible": self.direct_answer_eligible,
            "reason": self.reason.value,
        }


@dataclass(frozen=True, slots=True)
class LifecycleTransitionDecision:
    """Result of checking one named lifecycle transition."""

    current: LifecycleState
    target: LifecycleState
    operation: LifecycleOperation
    allowed: bool
    reason: LifecycleDecisionReason

    def to_dict(self) -> dict[str, object]:
        return {
            "current": self.current.value,
            "target": self.target.value,
            "operation": self.operation.value,
            "allowed": self.allowed,
            "reason": self.reason.value,
        }


@dataclass(frozen=True, slots=True)
class HistoricalKeyReuseDecision:
    """Result of checking whether an owned retrieval key may be reused."""

    allowed: bool
    reason: HistoricalKeyReuseReason

    def to_dict(self) -> dict[str, object]:
        return {"allowed": self.allowed, "reason": self.reason.value}


_ACTIVE_TRANSITIONS = MappingProxyType(
    {
        LifecycleOperation.SUPERSEDE: LifecycleState.SUPERSEDED,
        LifecycleOperation.INVALIDATE: LifecycleState.INVALIDATED,
        LifecycleOperation.RETIRE: LifecycleState.RETIRED,
    }
)
_NO_TRANSITIONS = MappingProxyType({})
LEGAL_LIFECYCLE_TRANSITIONS = MappingProxyType(
    {
        LifecycleState.ACTIVE: _ACTIVE_TRANSITIONS,
        LifecycleState.SUPERSEDED: _NO_TRANSITIONS,
        LifecycleState.INVALIDATED: _NO_TRANSITIONS,
        LifecycleState.RETIRED: _NO_TRANSITIONS,
    }
)
TERMINAL_LIFECYCLE_STATES = frozenset(
    {
        LifecycleState.SUPERSEDED,
        LifecycleState.INVALIDATED,
        LifecycleState.RETIRED,
    }
)

_INELIGIBLE_REASONS = MappingProxyType(
    {
        LifecycleState.SUPERSEDED: LifecycleDecisionReason.SUPERSEDED,
        LifecycleState.INVALIDATED: LifecycleDecisionReason.INVALIDATED,
        LifecycleState.RETIRED: LifecycleDecisionReason.RETIRED,
    }
)


def _require_lifecycle(value: object, name: str) -> LifecycleState:
    if not isinstance(value, LifecycleState):
        raise InvalidRequestError(f"{name} must be a LifecycleState")
    return value


def _require_operation(value: object) -> LifecycleOperation:
    if not isinstance(value, LifecycleOperation):
        raise InvalidRequestError("lifecycle operation must be a LifecycleOperation")
    return value


def lifecycle_base_eligibility(lifecycle: LifecycleState) -> LifecycleBaseDecision:
    """Evaluate lifecycle alone; temporal and epoch checks are later stages."""

    state = _require_lifecycle(lifecycle, "lifecycle")
    if state == LifecycleState.ACTIVE:
        return LifecycleBaseDecision(state, True, LifecycleDecisionReason.ELIGIBLE)
    return LifecycleBaseDecision(state, False, _INELIGIBLE_REASONS[state])


def lifecycle_transition_decision(
    current: LifecycleState,
    target: LifecycleState,
    operation: LifecycleOperation,
) -> LifecycleTransitionDecision:
    """Return the version 1 legal-transition decision without mutating state."""

    current_state = _require_lifecycle(current, "current lifecycle")
    target_state = _require_lifecycle(target, "target lifecycle")
    named_operation = _require_operation(operation)
    if current_state == target_state:
        return LifecycleTransitionDecision(
            current_state,
            target_state,
            named_operation,
            False,
            LifecycleDecisionReason.SAME_STATE_NOT_A_TRANSITION,
        )
    transitions = LEGAL_LIFECYCLE_TRANSITIONS[current_state]
    if named_operation not in transitions:
        return LifecycleTransitionDecision(
            current_state,
            target_state,
            named_operation,
            False,
            LifecycleDecisionReason.TERMINAL_STATE,
        )
    expected_target = transitions[named_operation]
    if expected_target != target_state:
        return LifecycleTransitionDecision(
            current_state,
            target_state,
            named_operation,
            False,
            LifecycleDecisionReason.OPERATION_TARGET_MISMATCH,
        )
    return LifecycleTransitionDecision(
        current_state,
        target_state,
        named_operation,
        True,
        LifecycleDecisionReason.LEGAL_TRANSITION,
    )


def require_lifecycle_transition(
    current: LifecycleState,
    target: LifecycleState,
    operation: LifecycleOperation,
) -> LifecycleTransitionDecision:
    """Return a legal decision or raise a stable lifecycle error."""

    decision = lifecycle_transition_decision(current, target, operation)
    if not decision.allowed:
        raise LifecycleError(
            f"illegal lifecycle transition: {decision.current.value} -> {decision.target.value} "
            f"via {decision.operation.value} ({decision.reason.value})"
        )
    return decision


def historical_key_reuse_decision(
    *,
    explicit_replacement: bool,
    expected_statement_id: str,
    expected_generation: int,
) -> HistoricalKeyReuseDecision:
    """Apply the v1 policy for a retrieval key already present in history.

    Repository collision checks remain responsible for verifying that the
    expected owner is the applicable current or historical owner.  This policy
    merely prevents blind base commit from reusing an owned key.
    """

    if not isinstance(explicit_replacement, bool):
        raise InvalidRequestError("explicit_replacement must be a boolean")
    if not isinstance(expected_statement_id, str):
        raise InvalidRequestError("expected_statement_id must be a string")
    if isinstance(expected_generation, bool) or not isinstance(expected_generation, int):
        raise InvalidRequestError("expected_generation must be an integer")
    if not explicit_replacement:
        return HistoricalKeyReuseDecision(False, HistoricalKeyReuseReason.BASE_COMMIT_FORBIDDEN)
    if not expected_statement_id:
        return HistoricalKeyReuseDecision(False, HistoricalKeyReuseReason.EXPECTED_STATEMENT_ID_REQUIRED)
    if expected_generation < 1:
        return HistoricalKeyReuseDecision(False, HistoricalKeyReuseReason.EXPECTED_GENERATION_REQUIRED)
    return HistoricalKeyReuseDecision(True, HistoricalKeyReuseReason.ALLOWED_EXPLICIT_REPLACEMENT)


def lifecycle_after_capacity_eviction(lifecycle: LifecycleState) -> LifecycleState:
    """Confirm that capacity eviction cannot perform a lifecycle transition."""

    return _require_lifecycle(lifecycle, "lifecycle")
