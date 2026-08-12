"""Request-scoped time, availability, and namespace epoch contracts."""

import json
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from types import MappingProxyType

from engram.artifacts import CachedResponseArtifact, LifecycleDecisionReason, lifecycle_base_eligibility
from engram.errors import ConflictError, InvalidRequestError, LifecycleError
from engram.identity import MAX_NAMESPACE_BYTES, ScopedRetrievalKey, ScopeKey
from engram.indexes import MAX_INDEX_LOOKUP_OWNERS, ExactLookupResult, IndexOwner, IndexProjection

ELIGIBILITY_CONTEXT_SCHEMA_VERSION = 1
NAMESPACE_EPOCH_STATE_SCHEMA_VERSION = 1
MAX_EPOCH = 9_223_372_036_854_775_807
MAX_EPOCH_SOURCE_LABEL_BYTES = 256
MAX_ELIGIBILITY_TIMESTAMP_BYTES = 40


class EpochSource(StrEnum):
    """Authority that supplied the namespace epoch in a context."""

    STANDALONE = "standalone"
    TRUSTED_INTEGRATION = "trusted_integration"
    UNAVAILABLE = "unavailable"


class EpochChangeReason(StrEnum):
    """Operations allowed to advance a standalone namespace epoch."""

    ACCEPTED_ARTIFACT_ELIGIBILITY = "accepted_artifact_eligibility"
    GRAPH_SNAPSHOT_ACTIVATED = "graph_snapshot_activated"


class EpochEligibilityPolicy(StrEnum):
    """Caller-configured artifact epoch interpretation."""

    REQUIRE_MATCH = "require_match"
    MATCH_WHEN_ARTIFACT_AVAILABLE = "match_when_artifact_available"


class EligibilityExclusionReason(StrEnum):
    """Stable complete outcomes for direct accepted-response eligibility."""

    ELIGIBLE = "eligible"
    ARTIFACT_REPOSITORY_UNAVAILABLE = "artifact_repository_unavailable"
    EVALUATION_TIME_UNAVAILABLE = "evaluation_time_unavailable"
    SCOPE_NAMESPACE_MISMATCH = "scope_namespace_mismatch"
    LIFECYCLE_SUPERSEDED = "lifecycle_superseded"
    LIFECYCLE_INVALIDATED = "lifecycle_invalidated"
    LIFECYCLE_RETIRED = "lifecycle_retired"
    VALIDITY_INTERVAL_INVALID = "validity_interval_invalid"
    NOT_YET_VALID = "not_yet_valid"
    EXPIRED = "expired"
    ARTIFACT_EPOCH_UNAVAILABLE = "artifact_epoch_unavailable"
    CONTEXT_EPOCH_UNAVAILABLE = "context_epoch_unavailable"
    KNOWLEDGE_EPOCH_MISMATCH = "knowledge_epoch_mismatch"


def _require_exact_mapping(value: object, name: str, keys: frozenset[str]) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise InvalidRequestError(f"{name} must be an object")
    actual = frozenset(value)
    if actual != keys:
        missing = sorted(keys - actual)
        extra = sorted(actual - keys)
        raise InvalidRequestError(f"{name} has invalid fields: missing={missing}, extra={extra}")
    return value


def _require_text(value: object, name: str, maximum_bytes: int, *, allow_empty: bool) -> str:
    if not isinstance(value, str):
        raise InvalidRequestError(f"{name} must be a string")
    if not allow_empty and not value:
        raise InvalidRequestError(f"{name} must not be empty")
    if len(value.encode("utf-8")) > maximum_bytes:
        raise InvalidRequestError(f"{name} exceeds the UTF-8 limit of {maximum_bytes} bytes")
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise InvalidRequestError(f"{name} contains a control character")
    return value


def _require_namespace(value: object) -> str:
    namespace = _require_text(value, "eligibility namespace", MAX_NAMESPACE_BYTES, allow_empty=True)
    ScopeKey(namespace=namespace)
    return namespace


def _require_bool(value: object, name: str) -> bool:
    if not isinstance(value, bool):
        raise InvalidRequestError(f"{name} must be a boolean")
    return value


def _require_nonnegative_epoch(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0 or value > MAX_EPOCH:
        raise InvalidRequestError(f"{name} must be a nonnegative 64-bit integer")
    return value


def _require_positive_version(value: object, expected: int, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise InvalidRequestError(f"{name} must be a positive integer")
    if value != expected:
        raise InvalidRequestError(f"unsupported {name}: {value}; expected {expected}")
    return value


def _require_timestamp(value: object, available: object, name: str) -> tuple[str, bool]:
    presence = _require_bool(available, f"{name}_available")
    text = _require_text(value, name, MAX_ELIGIBILITY_TIMESTAMP_BYTES, allow_empty=not presence)
    if not presence:
        if text:
            raise InvalidRequestError(f"{name} must be empty when unavailable")
        return text, presence
    if not text:
        raise InvalidRequestError(f"{name} must not be empty when available")
    if not text.endswith("Z"):
        raise InvalidRequestError(f"{name} must be a canonical RFC 3339 UTC timestamp ending in Z")
    try:
        parsed = datetime.fromisoformat(text[:-1] + "+00:00")
    except ValueError as error:
        raise InvalidRequestError(f"{name} must be a canonical RFC 3339 UTC timestamp") from error
    if parsed.isoformat().replace("+00:00", "Z") != text:
        raise InvalidRequestError(f"{name} must use the canonical RFC 3339 UTC representation")
    return text, presence


def _datetime_to_timestamp(value: object) -> str:
    if not isinstance(value, datetime):
        raise InvalidRequestError("eligibility clock must return a datetime")
    if not value.tzinfo or value.utcoffset() != timedelta(0):
        raise InvalidRequestError("eligibility clock must return a timezone-aware UTC datetime")
    text = value.isoformat().replace("+00:00", "Z")
    _require_timestamp(text, True, "eligibility evaluation_time")
    return text


def _json_text(value: Mapping[str, object]) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


@dataclass(frozen=True, slots=True)
class NamespaceEpoch:
    """One concrete namespace epoch lookup result."""

    namespace: str
    knowledge_epoch: int
    knowledge_epoch_available: bool

    def __post_init__(self) -> None:
        _require_namespace(self.namespace)
        epoch = _require_nonnegative_epoch(self.knowledge_epoch, "namespace knowledge_epoch")
        available = _require_bool(self.knowledge_epoch_available, "namespace knowledge_epoch_available")
        if not available and epoch != 0:
            raise InvalidRequestError("namespace knowledge_epoch must be 0 when unavailable")

    def to_dict(self) -> dict[str, object]:
        return {
            "namespace": self.namespace,
            "knowledge_epoch": self.knowledge_epoch,
            "knowledge_epoch_available": self.knowledge_epoch_available,
        }


@dataclass(frozen=True, slots=True)
class EpochIncrement:
    """Auditable result of one successful monotonic epoch change."""

    namespace: str
    previous_epoch: int
    knowledge_epoch: int
    reason: EpochChangeReason

    def __post_init__(self) -> None:
        _require_namespace(self.namespace)
        previous = _require_nonnegative_epoch(self.previous_epoch, "previous knowledge_epoch")
        current = _require_nonnegative_epoch(self.knowledge_epoch, "incremented knowledge_epoch")
        if current != previous + 1:
            raise InvalidRequestError("incremented knowledge_epoch must equal previous_epoch plus one")
        if not isinstance(self.reason, EpochChangeReason):
            raise InvalidRequestError("epoch increment reason must be an EpochChangeReason")

    def to_dict(self) -> dict[str, object]:
        return {
            "namespace": self.namespace,
            "previous_epoch": self.previous_epoch,
            "knowledge_epoch": self.knowledge_epoch,
            "reason": self.reason.value,
        }


class NamespaceEpochState:
    """Thread-safe standalone namespace epoch authority."""

    def __init__(self, epochs: Mapping[str, int] = MappingProxyType({})) -> None:
        if not isinstance(epochs, Mapping):
            raise InvalidRequestError("namespace epochs must be an object")
        validated = {}
        for namespace, epoch in epochs.items():
            normalized_namespace = _require_namespace(namespace)
            validated[normalized_namespace] = _require_nonnegative_epoch(epoch, "namespace knowledge_epoch")
        self._lock = threading.RLock()
        self._epochs = validated

    def initialize(self, namespace: str, initial_epoch: int = 0) -> NamespaceEpoch:
        normalized_namespace = _require_namespace(namespace)
        normalized_epoch = _require_nonnegative_epoch(initial_epoch, "initial knowledge_epoch")
        with self._lock:
            if normalized_namespace in self._epochs:
                current = self._epochs[normalized_namespace]
                if current != normalized_epoch:
                    raise ConflictError(
                        f"namespace epoch already initialized at {current}, not requested {normalized_epoch}: "
                        f"{normalized_namespace}"
                    )
                return NamespaceEpoch(normalized_namespace, current, True)
            self._epochs[normalized_namespace] = normalized_epoch
            return NamespaceEpoch(normalized_namespace, normalized_epoch, True)

    def get(self, namespace: str) -> NamespaceEpoch:
        normalized_namespace = _require_namespace(namespace)
        with self._lock:
            if normalized_namespace not in self._epochs:
                return NamespaceEpoch(normalized_namespace, 0, False)
            return NamespaceEpoch(normalized_namespace, self._epochs[normalized_namespace], True)

    def increment(
        self,
        namespace: str,
        expected_epoch: int,
        reason: EpochChangeReason,
    ) -> EpochIncrement:
        normalized_namespace = _require_namespace(namespace)
        expected = _require_nonnegative_epoch(expected_epoch, "expected knowledge_epoch")
        if not isinstance(reason, EpochChangeReason):
            raise InvalidRequestError("epoch change reason must be an EpochChangeReason")
        with self._lock:
            if normalized_namespace not in self._epochs:
                raise LifecycleError(f"namespace epoch is not initialized: {normalized_namespace}")
            current = self._epochs[normalized_namespace]
            if current != expected:
                raise ConflictError(f"namespace epoch conflict for {normalized_namespace}: expected {expected}, current {current}")
            if current == MAX_EPOCH:
                raise LifecycleError(f"namespace epoch is exhausted: {normalized_namespace}")
            updated = current + 1
            self._epochs[normalized_namespace] = updated
            return EpochIncrement(normalized_namespace, current, updated, reason)

    def snapshot(self) -> dict[str, object]:
        with self._lock:
            return {
                "schema_version": NAMESPACE_EPOCH_STATE_SCHEMA_VERSION,
                "epochs": {namespace: self._epochs[namespace] for namespace in sorted(self._epochs)},
            }

    def replace_from_snapshot(self, value: Mapping[str, object]) -> None:
        """Atomically restore validated epoch state without changing owner identity."""

        replacement = self.from_snapshot(value)
        with self._lock:
            self._epochs = dict(replacement._epochs)

    @classmethod
    def from_snapshot(cls, value: Mapping[str, object]) -> "NamespaceEpochState":
        data = _require_exact_mapping(
            value,
            "NamespaceEpochState",
            frozenset({"schema_version", "epochs"}),
        )
        _require_positive_version(
            data["schema_version"],
            NAMESPACE_EPOCH_STATE_SCHEMA_VERSION,
            "namespace epoch state schema_version",
        )
        epochs = data["epochs"]
        if not isinstance(epochs, Mapping):
            raise InvalidRequestError("namespace epoch state epochs must be an object")
        return cls(epochs)


@dataclass(frozen=True, slots=True)
class TrustedEligibilityInput:
    """Values accepted only from a configured trusted integration boundary."""

    namespace: str
    evaluation_time: str
    evaluation_time_available: bool
    knowledge_epoch: int
    knowledge_epoch_available: bool
    source_label: str

    def __post_init__(self) -> None:
        _require_namespace(self.namespace)
        _, time_available = _require_timestamp(
            self.evaluation_time,
            self.evaluation_time_available,
            "trusted evaluation_time",
        )
        if not time_available:
            raise InvalidRequestError("trusted evaluation_time must be available")
        epoch = _require_nonnegative_epoch(self.knowledge_epoch, "trusted knowledge_epoch")
        epoch_available = _require_bool(self.knowledge_epoch_available, "trusted knowledge_epoch_available")
        if not epoch_available and epoch != 0:
            raise InvalidRequestError("trusted knowledge_epoch must be 0 when unavailable")
        _require_text(
            self.source_label,
            "trusted eligibility source_label",
            MAX_EPOCH_SOURCE_LABEL_BYTES,
            allow_empty=False,
        )


@dataclass(frozen=True, slots=True)
class EligibilityContext:
    """Immutable request reference for all accepted-response eligibility checks."""

    evaluation_time: str
    evaluation_time_available: bool
    namespace: str
    knowledge_epoch: int
    knowledge_epoch_available: bool
    artifact_repository_available: bool
    epoch_source: EpochSource
    schema_version: int = ELIGIBILITY_CONTEXT_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _require_positive_version(
            self.schema_version,
            ELIGIBILITY_CONTEXT_SCHEMA_VERSION,
            "eligibility context schema_version",
        )
        _require_timestamp(self.evaluation_time, self.evaluation_time_available, "eligibility evaluation_time")
        _require_namespace(self.namespace)
        epoch = _require_nonnegative_epoch(self.knowledge_epoch, "eligibility knowledge_epoch")
        epoch_available = _require_bool(self.knowledge_epoch_available, "eligibility knowledge_epoch_available")
        if not epoch_available and epoch != 0:
            raise InvalidRequestError("eligibility knowledge_epoch must be 0 when unavailable")
        _require_bool(self.artifact_repository_available, "artifact_repository_available")
        if not isinstance(self.epoch_source, EpochSource):
            raise InvalidRequestError("eligibility epoch_source must be an EpochSource")
        if self.epoch_source == EpochSource.UNAVAILABLE and epoch_available:
            raise InvalidRequestError("unavailable epoch_source cannot carry an available knowledge_epoch")
        if self.epoch_source != EpochSource.UNAVAILABLE and not epoch_available:
            raise InvalidRequestError("an unavailable knowledge_epoch must use epoch_source UNAVAILABLE")

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "evaluation_time": self.evaluation_time,
            "evaluation_time_available": self.evaluation_time_available,
            "namespace": self.namespace,
            "knowledge_epoch": self.knowledge_epoch,
            "knowledge_epoch_available": self.knowledge_epoch_available,
            "artifact_repository_available": self.artifact_repository_available,
            "epoch_source": self.epoch_source.value,
        }

    def to_json(self) -> str:
        return _json_text(self.to_dict())

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> "EligibilityContext":
        keys = frozenset(
            {
                "schema_version",
                "evaluation_time",
                "evaluation_time_available",
                "namespace",
                "knowledge_epoch",
                "knowledge_epoch_available",
                "artifact_repository_available",
                "epoch_source",
            }
        )
        data = _require_exact_mapping(value, "EligibilityContext", keys)
        source_value = _require_text(data["epoch_source"], "eligibility epoch_source", 32, allow_empty=False)
        try:
            source = EpochSource(source_value)
        except ValueError as error:
            raise InvalidRequestError(f"unsupported eligibility epoch_source: {source_value}") from error
        return cls(
            schema_version=_require_positive_version(
                data["schema_version"],
                ELIGIBILITY_CONTEXT_SCHEMA_VERSION,
                "eligibility context schema_version",
            ),
            evaluation_time=_require_text(
                data["evaluation_time"],
                "eligibility evaluation_time",
                MAX_ELIGIBILITY_TIMESTAMP_BYTES,
                allow_empty=True,
            ),
            evaluation_time_available=_require_bool(
                data["evaluation_time_available"],
                "eligibility evaluation_time_available",
            ),
            namespace=_require_namespace(data["namespace"]),
            knowledge_epoch=_require_nonnegative_epoch(data["knowledge_epoch"], "eligibility knowledge_epoch"),
            knowledge_epoch_available=_require_bool(
                data["knowledge_epoch_available"],
                "eligibility knowledge_epoch_available",
            ),
            artifact_repository_available=_require_bool(
                data["artifact_repository_available"],
                "artifact_repository_available",
            ),
            epoch_source=source,
        )

    @classmethod
    def from_json(cls, value: str) -> "EligibilityContext":
        if not isinstance(value, str):
            raise InvalidRequestError("EligibilityContext JSON must be a string")
        try:
            data = json.loads(value)
        except json.JSONDecodeError as error:
            raise InvalidRequestError("EligibilityContext JSON is malformed") from error
        if not isinstance(data, Mapping):
            raise InvalidRequestError("EligibilityContext JSON must contain an object")
        return cls.from_dict(data)


@dataclass(frozen=True, slots=True)
class EligibilityDecision:
    """Pure eligibility result for one artifact and captured request context."""

    statement_id: str
    generation: int
    lifecycle_base_eligible: bool
    direct_answer_eligible: bool
    exclusion_reason: EligibilityExclusionReason
    evaluation_time: str
    evaluation_time_available: bool
    namespace: str
    knowledge_epoch: int
    knowledge_epoch_available: bool
    artifact_repository_available: bool
    epoch_policy: EpochEligibilityPolicy

    def __post_init__(self) -> None:
        _require_text(self.statement_id, "eligibility statement_id", 256, allow_empty=False)
        if isinstance(self.generation, bool) or not isinstance(self.generation, int) or self.generation < 1:
            raise InvalidRequestError("eligibility generation must be a positive integer")
        _require_bool(self.lifecycle_base_eligible, "lifecycle_base_eligible")
        eligible = _require_bool(self.direct_answer_eligible, "direct_answer_eligible")
        if not isinstance(self.exclusion_reason, EligibilityExclusionReason):
            raise InvalidRequestError("exclusion_reason must be an EligibilityExclusionReason")
        if eligible != (self.exclusion_reason == EligibilityExclusionReason.ELIGIBLE):
            raise InvalidRequestError("direct_answer_eligible must agree with exclusion_reason")
        _require_timestamp(self.evaluation_time, self.evaluation_time_available, "eligibility evaluation_time")
        _require_namespace(self.namespace)
        epoch = _require_nonnegative_epoch(self.knowledge_epoch, "eligibility knowledge_epoch")
        epoch_available = _require_bool(self.knowledge_epoch_available, "eligibility knowledge_epoch_available")
        if not epoch_available and epoch != 0:
            raise InvalidRequestError("eligibility knowledge_epoch must be 0 when unavailable")
        _require_bool(self.artifact_repository_available, "artifact_repository_available")
        if not isinstance(self.epoch_policy, EpochEligibilityPolicy):
            raise InvalidRequestError("epoch_policy must be an EpochEligibilityPolicy")

    def to_dict(self) -> dict[str, object]:
        return {
            "statement_id": self.statement_id,
            "generation": self.generation,
            "lifecycle_base_eligible": self.lifecycle_base_eligible,
            "direct_answer_eligible": self.direct_answer_eligible,
            "exclusion_reason": self.exclusion_reason.value,
            "evaluation_time": self.evaluation_time,
            "evaluation_time_available": self.evaluation_time_available,
            "namespace": self.namespace,
            "knowledge_epoch": self.knowledge_epoch,
            "knowledge_epoch_available": self.knowledge_epoch_available,
            "artifact_repository_available": self.artifact_repository_available,
            "epoch_policy": self.epoch_policy.value,
        }

    def context_signature(self) -> str:
        return _json_text(
            {
                "evaluation_time": self.evaluation_time,
                "evaluation_time_available": self.evaluation_time_available,
                "namespace": self.namespace,
                "knowledge_epoch": self.knowledge_epoch,
                "knowledge_epoch_available": self.knowledge_epoch_available,
                "artifact_repository_available": self.artifact_repository_available,
                "epoch_policy": self.epoch_policy.value,
            }
        )


_LIFECYCLE_EXCLUSION_REASONS = MappingProxyType(
    {
        LifecycleDecisionReason.SUPERSEDED: EligibilityExclusionReason.LIFECYCLE_SUPERSEDED,
        LifecycleDecisionReason.INVALIDATED: EligibilityExclusionReason.LIFECYCLE_INVALIDATED,
        LifecycleDecisionReason.RETIRED: EligibilityExclusionReason.LIFECYCLE_RETIRED,
    }
)


def _timestamp_to_datetime(value: str) -> datetime:
    return datetime.fromisoformat(value[:-1] + "+00:00")


def _decision(
    artifact: CachedResponseArtifact,
    context: EligibilityContext,
    epoch_policy: EpochEligibilityPolicy,
    lifecycle_base_eligible: bool,
    reason: EligibilityExclusionReason,
) -> EligibilityDecision:
    return EligibilityDecision(
        statement_id=artifact.statement_id,
        generation=artifact.generation,
        lifecycle_base_eligible=lifecycle_base_eligible,
        direct_answer_eligible=reason == EligibilityExclusionReason.ELIGIBLE,
        exclusion_reason=reason,
        evaluation_time=context.evaluation_time,
        evaluation_time_available=context.evaluation_time_available,
        namespace=context.namespace,
        knowledge_epoch=context.knowledge_epoch,
        knowledge_epoch_available=context.knowledge_epoch_available,
        artifact_repository_available=context.artifact_repository_available,
        epoch_policy=epoch_policy,
    )


def evaluate_artifact_eligibility(
    artifact: CachedResponseArtifact,
    context: EligibilityContext,
    epoch_policy: EpochEligibilityPolicy,
) -> EligibilityDecision:
    """Evaluate one artifact without ambient time, I/O, mutation, or inference."""

    if not isinstance(artifact, CachedResponseArtifact):
        raise InvalidRequestError("artifact must be a CachedResponseArtifact")
    if not isinstance(context, EligibilityContext):
        raise InvalidRequestError("context must be an EligibilityContext")
    if not isinstance(epoch_policy, EpochEligibilityPolicy):
        raise InvalidRequestError("epoch_policy must be an EpochEligibilityPolicy")

    lifecycle = lifecycle_base_eligibility(artifact.lifecycle)
    if not context.artifact_repository_available:
        return _decision(
            artifact,
            context,
            epoch_policy,
            lifecycle.direct_answer_eligible,
            EligibilityExclusionReason.ARTIFACT_REPOSITORY_UNAVAILABLE,
        )
    if not context.evaluation_time_available:
        return _decision(
            artifact,
            context,
            epoch_policy,
            lifecycle.direct_answer_eligible,
            EligibilityExclusionReason.EVALUATION_TIME_UNAVAILABLE,
        )
    if artifact.scope.namespace != context.namespace:
        return _decision(
            artifact,
            context,
            epoch_policy,
            lifecycle.direct_answer_eligible,
            EligibilityExclusionReason.SCOPE_NAMESPACE_MISMATCH,
        )
    if not lifecycle.direct_answer_eligible:
        return _decision(
            artifact,
            context,
            epoch_policy,
            False,
            _LIFECYCLE_EXCLUSION_REASONS[lifecycle.reason],
        )

    evaluation_time = _timestamp_to_datetime(context.evaluation_time)
    valid_from = _timestamp_to_datetime(artifact.valid_from) if artifact.valid_from_available else evaluation_time
    valid_until = _timestamp_to_datetime(artifact.valid_until) if artifact.valid_until_available else evaluation_time
    if artifact.valid_from_available and artifact.valid_until_available and valid_from >= valid_until:
        return _decision(
            artifact,
            context,
            epoch_policy,
            True,
            EligibilityExclusionReason.VALIDITY_INTERVAL_INVALID,
        )
    if artifact.valid_from_available and evaluation_time < valid_from:
        return _decision(
            artifact,
            context,
            epoch_policy,
            True,
            EligibilityExclusionReason.NOT_YET_VALID,
        )
    if artifact.valid_until_available and evaluation_time >= valid_until:
        return _decision(
            artifact,
            context,
            epoch_policy,
            True,
            EligibilityExclusionReason.EXPIRED,
        )

    if epoch_policy == EpochEligibilityPolicy.REQUIRE_MATCH and not artifact.knowledge_epoch_available:
        return _decision(
            artifact,
            context,
            epoch_policy,
            True,
            EligibilityExclusionReason.ARTIFACT_EPOCH_UNAVAILABLE,
        )
    if artifact.knowledge_epoch_available:
        if not context.knowledge_epoch_available:
            return _decision(
                artifact,
                context,
                epoch_policy,
                True,
                EligibilityExclusionReason.CONTEXT_EPOCH_UNAVAILABLE,
            )
        if artifact.knowledge_epoch != context.knowledge_epoch:
            return _decision(
                artifact,
                context,
                epoch_policy,
                True,
                EligibilityExclusionReason.KNOWLEDGE_EPOCH_MISMATCH,
            )
    return _decision(
        artifact,
        context,
        epoch_policy,
        True,
        EligibilityExclusionReason.ELIGIBLE,
    )


def index_projection_from_artifact(
    artifact: CachedResponseArtifact,
    decision: EligibilityDecision,
) -> IndexProjection:
    """Derive the disposable Section 2 projection for one exact decision."""

    if not isinstance(artifact, CachedResponseArtifact):
        raise InvalidRequestError("artifact must be a CachedResponseArtifact")
    if not isinstance(decision, EligibilityDecision):
        raise InvalidRequestError("decision must be an EligibilityDecision")
    if artifact.statement_id != decision.statement_id or artifact.generation != decision.generation:
        raise ConflictError("eligibility decision does not identify the artifact generation")
    if artifact.scope.namespace != decision.namespace:
        raise ConflictError("eligibility decision namespace does not match the artifact")
    return IndexProjection(
        statement_id=artifact.statement_id,
        generation=artifact.generation,
        retrieval_keys=artifact.retrieval.bindings(artifact.scope),
        support_claim_ids=artifact.support_claim_ids,
        direct_answer_eligible=decision.direct_answer_eligible,
        exclusion_reason="" if decision.direct_answer_eligible else decision.exclusion_reason.value,
    )


@dataclass(frozen=True, slots=True)
class ContextualExactLookupResult:
    """Exact result returned only after context-specific owner revalidation."""

    lookup: ExactLookupResult
    decisions: tuple[EligibilityDecision, ...]
    context_signature: str
    index_refreshed: bool
    index_state_generation: int

    def to_dict(self) -> dict[str, object]:
        return {
            "lookup": self.lookup.to_dict(),
            "decisions": [decision.to_dict() for decision in self.decisions],
            "context_signature": self.context_signature,
            "index_refreshed": self.index_refreshed,
            "index_state_generation": self.index_state_generation,
        }


class ContextualExactLookup:
    """Revalidate every exact-key owner before exposing a direct result."""

    def __init__(
        self,
        artifacts: Mapping[str, CachedResponseArtifact],
        indexes: IndexOwner,
        max_refresh_retries: int = 4,
    ) -> None:
        if not isinstance(artifacts, Mapping):
            raise InvalidRequestError("contextual exact artifacts must be an object")
        validated = {}
        for statement_id, artifact in artifacts.items():
            if not isinstance(statement_id, str) or not statement_id:
                raise InvalidRequestError("contextual exact artifact keys must be non-empty strings")
            if not isinstance(artifact, CachedResponseArtifact):
                raise InvalidRequestError("contextual exact artifact values must be CachedResponseArtifact values")
            if artifact.statement_id != statement_id:
                raise InvalidRequestError("contextual exact artifact key must match its statement_id")
            validated[statement_id] = artifact
        if not isinstance(indexes, IndexOwner):
            raise InvalidRequestError("contextual exact indexes must be an IndexOwner")
        if isinstance(max_refresh_retries, bool) or not isinstance(max_refresh_retries, int) or max_refresh_retries < 1:
            raise InvalidRequestError("max_refresh_retries must be a positive integer")
        self._artifacts = MappingProxyType(validated)
        self._indexes = indexes
        self._max_refresh_retries = max_refresh_retries

    def exact_lookup(
        self,
        key: ScopedRetrievalKey,
        context: EligibilityContext,
        epoch_policy: EpochEligibilityPolicy,
    ) -> ContextualExactLookupResult:
        if not isinstance(key, ScopedRetrievalKey):
            raise InvalidRequestError("contextual exact key must be a ScopedRetrievalKey")
        if not isinstance(context, EligibilityContext):
            raise InvalidRequestError("context must be an EligibilityContext")
        if not isinstance(epoch_policy, EpochEligibilityPolicy):
            raise InvalidRequestError("epoch_policy must be an EpochEligibilityPolicy")
        for _attempt in range(self._max_refresh_retries):
            state = self._indexes.snapshot()
            owner_ids = tuple(dict.fromkeys(owner.statement_id for owner in state.retrieval_to_owners.get(key, ())))
            if len(owner_ids) > MAX_INDEX_LOOKUP_OWNERS:
                raise LifecycleError(f"exact refresh owner bound exceeded: {len(owner_ids)}")
            decisions = []
            projections = []
            for statement_id in owner_ids:
                if statement_id not in self._artifacts:
                    raise LifecycleError(f"authoritative artifact missing for exact owner: {statement_id}")
                artifact = self._artifacts[statement_id]
                decision = evaluate_artifact_eligibility(artifact, context, epoch_policy)
                decisions.append(decision)
                projections.append(index_projection_from_artifact(artifact, decision))
            try:
                lookup, refreshed, state_generation = self._indexes.atomic_refresh_exact_lookup(
                    key,
                    tuple(projections),
                    state.state_generation,
                )
            except ConflictError:
                continue
            signature = (
                decisions[0].context_signature()
                if decisions
                else _json_text(
                    {
                        "evaluation_time": context.evaluation_time,
                        "evaluation_time_available": context.evaluation_time_available,
                        "namespace": context.namespace,
                        "knowledge_epoch": context.knowledge_epoch,
                        "knowledge_epoch_available": context.knowledge_epoch_available,
                        "artifact_repository_available": context.artifact_repository_available,
                        "epoch_policy": epoch_policy.value,
                    }
                )
            )
            return ContextualExactLookupResult(
                lookup=lookup,
                decisions=tuple(decisions),
                context_signature=signature,
                index_refreshed=refreshed,
                index_state_generation=state_generation,
            )
        raise ConflictError("exact eligibility refresh did not converge within the retry bound")


class EligibilityContextFactory:
    """Capture exactly one trusted time and epoch snapshot per request."""

    def __init__(self, clock: Callable[[], datetime], namespace_epochs: NamespaceEpochState) -> None:
        if not callable(clock):
            raise InvalidRequestError("eligibility clock must be callable")
        if not isinstance(namespace_epochs, NamespaceEpochState):
            raise InvalidRequestError("namespace_epochs must be a NamespaceEpochState")
        self._clock = clock
        self._namespace_epochs = namespace_epochs

    def capture_standalone(self, scope: ScopeKey, artifact_repository_available: bool) -> EligibilityContext:
        if not isinstance(scope, ScopeKey):
            raise InvalidRequestError("eligibility scope must be a ScopeKey")
        repository_available = _require_bool(artifact_repository_available, "artifact_repository_available")
        evaluation_time = _datetime_to_timestamp(self._clock())
        epoch = self._namespace_epochs.get(scope.namespace)
        source = EpochSource.STANDALONE if epoch.knowledge_epoch_available else EpochSource.UNAVAILABLE
        return EligibilityContext(
            evaluation_time=evaluation_time,
            evaluation_time_available=True,
            namespace=scope.namespace,
            knowledge_epoch=epoch.knowledge_epoch,
            knowledge_epoch_available=epoch.knowledge_epoch_available,
            artifact_repository_available=repository_available,
            epoch_source=source,
        )

    def capture_trusted(
        self,
        trusted_input: TrustedEligibilityInput,
        artifact_repository_available: bool,
    ) -> EligibilityContext:
        if not isinstance(trusted_input, TrustedEligibilityInput):
            raise InvalidRequestError("trusted_input must be a TrustedEligibilityInput")
        repository_available = _require_bool(artifact_repository_available, "artifact_repository_available")
        source = EpochSource.TRUSTED_INTEGRATION if trusted_input.knowledge_epoch_available else EpochSource.UNAVAILABLE
        return EligibilityContext(
            evaluation_time=trusted_input.evaluation_time,
            evaluation_time_available=trusted_input.evaluation_time_available,
            namespace=trusted_input.namespace,
            knowledge_epoch=trusted_input.knowledge_epoch,
            knowledge_epoch_available=trusted_input.knowledge_epoch_available,
            artifact_repository_available=repository_available,
            epoch_source=source,
        )
