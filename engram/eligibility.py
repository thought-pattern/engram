"""Request-scoped time, availability, and namespace epoch contracts."""

import json
import threading
from collections.abc import Callable, Mapping
from datetime import datetime, timedelta
from types import MappingProxyType
from typing import TypedDict

from engram.artifacts import CachedResponseArtifact, lifecycle_base_eligibility, validate_cached_response_artifact
from engram.constants import (
    CONTEXTUAL_EXACT_LOOKUP_RESULT_FIELDS,
    ELIGIBILITY_CONTEXT_FIELDS,
    ELIGIBILITY_CONTEXT_SCHEMA_VERSION,
    ELIGIBILITY_DECISION_FIELDS,
    EPOCH_INCREMENT_FIELDS,
    LIFECYCLE_EXCLUSION_REASONS as _LIFECYCLE_EXCLUSION_REASONS,
    MAX_ELIGIBILITY_CONTEXT_SIGNATURE_BYTES,
    MAX_ELIGIBILITY_STATEMENT_ID_BYTES,
    MAX_ELIGIBILITY_TIMESTAMP_BYTES,
    MAX_EPOCH,
    MAX_EPOCH_SOURCE_BYTES,
    MAX_EPOCH_SOURCE_LABEL_BYTES,
    NAMESPACE_EPOCH_FIELDS,
    NAMESPACE_EPOCH_STATE_SCHEMA_VERSION,
    TRUSTED_ELIGIBILITY_INPUT_FIELDS,
    EligibilityExclusionReason,
    EpochChangeReason,
    EpochEligibilityPolicy,
    EpochSource,
)
from engram.errors import ConflictError, IdentityValidationError, InvalidRequestError, LifecycleError
from engram.identity import (
    MAX_NAMESPACE_BYTES,
    ScopedRetrievalKey,
    ScopeKey,
    retrieval_representation_bindings,
    scope_key,
    scoped_retrieval_key_signature,
    validate_scope_key,
    validate_scoped_retrieval_key,
)
from engram.indexes import (
    MAX_INDEX_LOOKUP_OWNERS,
    ExactLookupResult,
    IndexOwner,
    IndexProjection,
    exact_lookup_result_to_dict,
    index_projection,
    validate_exact_lookup_result,
)


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
    scope_key(namespace=namespace)
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
        result = (text, presence)
        return result
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
    result = (text, presence)
    return result


def _datetime_to_timestamp(value: object) -> str:
    if not isinstance(value, datetime):
        raise InvalidRequestError("eligibility clock must return a datetime")
    if not value.tzinfo or value.utcoffset() != timedelta(0):
        raise InvalidRequestError("eligibility clock must return a timezone-aware UTC datetime")
    text = value.isoformat().replace("+00:00", "Z")
    _require_timestamp(text, True, "eligibility evaluation_time")
    return text


def _json_text(value: Mapping[str, object]) -> str:
    result = json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return result


NamespaceEpoch = TypedDict(
    "NamespaceEpoch",
    {
        "namespace": str,
        "knowledge_epoch": int,
        "knowledge_epoch_available": bool,
    },
)


def namespace_epoch(namespace: object, knowledge_epoch: object, knowledge_epoch_available: object) -> NamespaceEpoch:
    """Build one concrete namespace epoch lookup dictionary."""
    normalized_namespace = _require_namespace(namespace)
    normalized_epoch = _require_nonnegative_epoch(knowledge_epoch, "namespace knowledge_epoch")
    normalized_available = _require_bool(knowledge_epoch_available, "namespace knowledge_epoch_available")
    if not normalized_available and normalized_epoch != 0:
        raise InvalidRequestError("namespace knowledge_epoch must be 0 when unavailable")
    result: NamespaceEpoch = {
        "namespace": normalized_namespace,
        "knowledge_epoch": normalized_epoch,
        "knowledge_epoch_available": normalized_available,
    }
    return result


def validate_namespace_epoch(value: object) -> NamespaceEpoch:
    """Revalidate and copy one namespace epoch record."""
    data = _require_exact_mapping(value, "NamespaceEpoch", NAMESPACE_EPOCH_FIELDS)
    result = namespace_epoch(data["namespace"], data["knowledge_epoch"], data["knowledge_epoch_available"])
    return result


def namespace_epoch_to_dict(value: object) -> dict[str, object]:
    """Serialize one namespace epoch record."""
    result = dict(validate_namespace_epoch(value))
    return result


EpochIncrement = TypedDict(
    "EpochIncrement",
    {
        "namespace": str,
        "previous_epoch": int,
        "knowledge_epoch": int,
        "reason": EpochChangeReason,
    },
)


def epoch_increment(
    namespace: object,
    previous_epoch: object,
    knowledge_epoch: object,
    reason: object,
) -> EpochIncrement:
    """Build one auditable monotonic epoch increment dictionary."""
    normalized_namespace = _require_namespace(namespace)
    normalized_previous = _require_nonnegative_epoch(previous_epoch, "previous knowledge_epoch")
    normalized_current = _require_nonnegative_epoch(knowledge_epoch, "incremented knowledge_epoch")
    if normalized_current != normalized_previous + 1:
        raise InvalidRequestError("incremented knowledge_epoch must equal previous_epoch plus one")
    if not isinstance(reason, EpochChangeReason):
        raise InvalidRequestError("epoch increment reason must be an EpochChangeReason")
    result: EpochIncrement = {
        "namespace": normalized_namespace,
        "previous_epoch": normalized_previous,
        "knowledge_epoch": normalized_current,
        "reason": reason,
    }
    return result


def validate_epoch_increment(value: object) -> EpochIncrement:
    """Revalidate and copy one epoch increment record."""
    data = _require_exact_mapping(value, "EpochIncrement", EPOCH_INCREMENT_FIELDS)
    result = epoch_increment(data["namespace"], data["previous_epoch"], data["knowledge_epoch"], data["reason"])
    return result


def epoch_increment_to_dict(value: object) -> dict[str, object]:
    """Serialize one epoch increment record."""
    increment = validate_epoch_increment(value)
    result: dict[str, object] = dict(increment)
    result["reason"] = increment["reason"].value
    return result


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
                result = namespace_epoch(normalized_namespace, current, True)
                return result
            self._epochs[normalized_namespace] = normalized_epoch
            result = namespace_epoch(normalized_namespace, normalized_epoch, True)
            return result

    def get(self, namespace: str) -> NamespaceEpoch:
        normalized_namespace = _require_namespace(namespace)
        with self._lock:
            if normalized_namespace not in self._epochs:
                result = namespace_epoch(normalized_namespace, 0, False)
                return result
            result = namespace_epoch(normalized_namespace, self._epochs[normalized_namespace], True)
            return result

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
            result = epoch_increment(normalized_namespace, current, updated, reason)
            return result

    def snapshot(self) -> dict[str, object]:
        with self._lock:
            result = {
                "schema_version": NAMESPACE_EPOCH_STATE_SCHEMA_VERSION,
                "epochs": {namespace: self._epochs[namespace] for namespace in sorted(self._epochs)},
            }
            return result

    def replace_from_snapshot(self, value: Mapping[str, object]) -> None:
        """Atomically restore validated epoch state without changing owner identity."""

        replacement = namespace_epoch_state_from_snapshot(value)
        with self._lock:
            self._epochs = dict(replacement._epochs)


def namespace_epoch_state_from_snapshot(value: Mapping[str, object]) -> NamespaceEpochState:
    """Construct namespace epoch state from one exact snapshot."""
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
    result = NamespaceEpochState(epochs)
    return result


TrustedEligibilityInput = TypedDict(
    "TrustedEligibilityInput",
    {
        "namespace": str,
        "evaluation_time": str,
        "evaluation_time_available": bool,
        "knowledge_epoch": int,
        "knowledge_epoch_available": bool,
        "source_label": str,
    },
)


def trusted_eligibility_input(
    namespace: object,
    evaluation_time: object,
    evaluation_time_available: object,
    knowledge_epoch: object,
    knowledge_epoch_available: object,
    source_label: object,
) -> TrustedEligibilityInput:
    """Build values accepted only from a configured trusted boundary."""
    normalized_namespace = _require_namespace(namespace)
    normalized_time, time_available = _require_timestamp(
        evaluation_time,
        evaluation_time_available,
        "trusted evaluation_time",
    )
    if not time_available:
        raise InvalidRequestError("trusted evaluation_time must be available")
    epoch = _require_nonnegative_epoch(knowledge_epoch, "trusted knowledge_epoch")
    epoch_available = _require_bool(knowledge_epoch_available, "trusted knowledge_epoch_available")
    if not epoch_available and epoch != 0:
        raise InvalidRequestError("trusted knowledge_epoch must be 0 when unavailable")
    normalized_source = _require_text(
        source_label,
        "trusted eligibility source_label",
        MAX_EPOCH_SOURCE_LABEL_BYTES,
        allow_empty=False,
    )
    result: TrustedEligibilityInput = {
        "namespace": normalized_namespace,
        "evaluation_time": normalized_time,
        "evaluation_time_available": time_available,
        "knowledge_epoch": epoch,
        "knowledge_epoch_available": epoch_available,
        "source_label": normalized_source,
    }
    return result


def validate_trusted_eligibility_input(value: object) -> TrustedEligibilityInput:
    """Revalidate and copy one trusted eligibility input."""
    data = _require_exact_mapping(value, "TrustedEligibilityInput", TRUSTED_ELIGIBILITY_INPUT_FIELDS)
    result = trusted_eligibility_input(
        data["namespace"],
        data["evaluation_time"],
        data["evaluation_time_available"],
        data["knowledge_epoch"],
        data["knowledge_epoch_available"],
        data["source_label"],
    )
    return result


def trusted_eligibility_input_to_dict(value: object) -> dict[str, object]:
    """Serialize one trusted eligibility input."""
    result = dict(validate_trusted_eligibility_input(value))
    return result


EligibilityContext = TypedDict(
    "EligibilityContext",
    {
        "schema_version": int,
        "evaluation_time": str,
        "evaluation_time_available": bool,
        "namespace": str,
        "knowledge_epoch": int,
        "knowledge_epoch_available": bool,
        "artifact_repository_available": bool,
        "epoch_source": EpochSource,
    },
)


def eligibility_context(
    evaluation_time: object,
    evaluation_time_available: object,
    namespace: object,
    knowledge_epoch: object,
    knowledge_epoch_available: object,
    artifact_repository_available: object,
    epoch_source: object,
    schema_version: object = ELIGIBILITY_CONTEXT_SCHEMA_VERSION,
) -> EligibilityContext:
    """Build one immutable-by-convention request eligibility reference."""
    version = _require_positive_version(
        schema_version,
        ELIGIBILITY_CONTEXT_SCHEMA_VERSION,
        "eligibility context schema_version",
    )
    normalized_time, time_available = _require_timestamp(
        evaluation_time,
        evaluation_time_available,
        "eligibility evaluation_time",
    )
    normalized_namespace = _require_namespace(namespace)
    epoch = _require_nonnegative_epoch(knowledge_epoch, "eligibility knowledge_epoch")
    epoch_available = _require_bool(knowledge_epoch_available, "eligibility knowledge_epoch_available")
    if not epoch_available and epoch != 0:
        raise InvalidRequestError("eligibility knowledge_epoch must be 0 when unavailable")
    repository_available = _require_bool(artifact_repository_available, "artifact_repository_available")
    if not isinstance(epoch_source, EpochSource):
        raise InvalidRequestError("eligibility epoch_source must be an EpochSource")
    if epoch_source == EpochSource.UNAVAILABLE and epoch_available:
        raise InvalidRequestError("unavailable epoch_source cannot carry an available knowledge_epoch")
    if epoch_source != EpochSource.UNAVAILABLE and not epoch_available:
        raise InvalidRequestError("an unavailable knowledge_epoch must use epoch_source UNAVAILABLE")
    result: EligibilityContext = {
        "schema_version": version,
        "evaluation_time": normalized_time,
        "evaluation_time_available": time_available,
        "namespace": normalized_namespace,
        "knowledge_epoch": epoch,
        "knowledge_epoch_available": epoch_available,
        "artifact_repository_available": repository_available,
        "epoch_source": epoch_source,
    }
    return result


def validate_eligibility_context(value: object) -> EligibilityContext:
    """Revalidate and copy one eligibility context."""
    data = _require_exact_mapping(value, "EligibilityContext", ELIGIBILITY_CONTEXT_FIELDS)
    result = eligibility_context(
        data["evaluation_time"],
        data["evaluation_time_available"],
        data["namespace"],
        data["knowledge_epoch"],
        data["knowledge_epoch_available"],
        data["artifact_repository_available"],
        data["epoch_source"],
        data["schema_version"],
    )
    return result


def eligibility_context_to_dict(value: object) -> dict[str, object]:
    """Serialize one eligibility context."""
    context = validate_eligibility_context(value)
    result: dict[str, object] = dict(context)
    result["epoch_source"] = context["epoch_source"].value
    return result


def eligibility_context_to_json(value: object) -> str:
    """Encode one eligibility context as canonical JSON."""
    data = eligibility_context_to_dict(value)
    result = _json_text(data)
    return result


def eligibility_context_from_dict(value: object) -> EligibilityContext:
    """Decode one eligibility context from its serialized dictionary."""
    data = _require_exact_mapping(value, "EligibilityContext", ELIGIBILITY_CONTEXT_FIELDS)
    source_value = _require_text(
        data["epoch_source"],
        "eligibility epoch_source",
        MAX_EPOCH_SOURCE_BYTES,
        allow_empty=False,
    )
    try:
        source = EpochSource(source_value)
    except ValueError as error:
        raise InvalidRequestError(f"unsupported eligibility epoch_source: {source_value}") from error
    result = eligibility_context(
        data["evaluation_time"],
        data["evaluation_time_available"],
        data["namespace"],
        data["knowledge_epoch"],
        data["knowledge_epoch_available"],
        data["artifact_repository_available"],
        source,
        data["schema_version"],
    )
    return result


def eligibility_context_from_json(value: object) -> EligibilityContext:
    """Decode one eligibility context from canonical JSON."""
    if not isinstance(value, str):
        raise InvalidRequestError("EligibilityContext JSON must be a string")
    try:
        data = json.loads(value)
    except json.JSONDecodeError as error:
        raise InvalidRequestError("EligibilityContext JSON is malformed") from error
    if not isinstance(data, Mapping):
        raise InvalidRequestError("EligibilityContext JSON must contain an object")
    result = eligibility_context_from_dict(data)
    return result


EligibilityDecision = TypedDict(
    "EligibilityDecision",
    {
        "statement_id": str,
        "generation": int,
        "lifecycle_base_eligible": bool,
        "direct_answer_eligible": bool,
        "exclusion_reason": EligibilityExclusionReason,
        "evaluation_time": str,
        "evaluation_time_available": bool,
        "namespace": str,
        "knowledge_epoch": int,
        "knowledge_epoch_available": bool,
        "artifact_repository_available": bool,
        "epoch_policy": EpochEligibilityPolicy,
    },
)


def eligibility_decision(
    statement_id: object,
    generation: object,
    lifecycle_base_eligible: object,
    direct_answer_eligible: object,
    exclusion_reason: object,
    evaluation_time: object,
    evaluation_time_available: object,
    namespace: object,
    knowledge_epoch: object,
    knowledge_epoch_available: object,
    artifact_repository_available: object,
    epoch_policy: object,
) -> EligibilityDecision:
    """Build one pure eligibility result for an artifact and request context."""
    normalized_statement_id = _require_text(
        statement_id,
        "eligibility statement_id",
        MAX_ELIGIBILITY_STATEMENT_ID_BYTES,
        allow_empty=False,
    )
    if isinstance(generation, bool) or not isinstance(generation, int) or generation < 1:
        raise InvalidRequestError("eligibility generation must be a positive integer")
    base_eligible = _require_bool(lifecycle_base_eligible, "lifecycle_base_eligible")
    answer_eligible = _require_bool(direct_answer_eligible, "direct_answer_eligible")
    if not isinstance(exclusion_reason, EligibilityExclusionReason):
        raise InvalidRequestError("exclusion_reason must be an EligibilityExclusionReason")
    if answer_eligible != (exclusion_reason == EligibilityExclusionReason.ELIGIBLE):
        raise InvalidRequestError("direct_answer_eligible must agree with exclusion_reason")
    normalized_time, time_available = _require_timestamp(
        evaluation_time,
        evaluation_time_available,
        "eligibility evaluation_time",
    )
    normalized_namespace = _require_namespace(namespace)
    epoch = _require_nonnegative_epoch(knowledge_epoch, "eligibility knowledge_epoch")
    epoch_available = _require_bool(knowledge_epoch_available, "eligibility knowledge_epoch_available")
    if not epoch_available and epoch != 0:
        raise InvalidRequestError("eligibility knowledge_epoch must be 0 when unavailable")
    repository_available = _require_bool(artifact_repository_available, "artifact_repository_available")
    if not isinstance(epoch_policy, EpochEligibilityPolicy):
        raise InvalidRequestError("epoch_policy must be an EpochEligibilityPolicy")
    result: EligibilityDecision = {
        "statement_id": normalized_statement_id,
        "generation": generation,
        "lifecycle_base_eligible": base_eligible,
        "direct_answer_eligible": answer_eligible,
        "exclusion_reason": exclusion_reason,
        "evaluation_time": normalized_time,
        "evaluation_time_available": time_available,
        "namespace": normalized_namespace,
        "knowledge_epoch": epoch,
        "knowledge_epoch_available": epoch_available,
        "artifact_repository_available": repository_available,
        "epoch_policy": epoch_policy,
    }
    return result


def validate_eligibility_decision(value: object) -> EligibilityDecision:
    """Revalidate and copy one eligibility decision."""
    data = _require_exact_mapping(value, "EligibilityDecision", ELIGIBILITY_DECISION_FIELDS)
    result = eligibility_decision(
        data["statement_id"],
        data["generation"],
        data["lifecycle_base_eligible"],
        data["direct_answer_eligible"],
        data["exclusion_reason"],
        data["evaluation_time"],
        data["evaluation_time_available"],
        data["namespace"],
        data["knowledge_epoch"],
        data["knowledge_epoch_available"],
        data["artifact_repository_available"],
        data["epoch_policy"],
    )
    return result


def eligibility_decision_to_dict(value: object) -> dict[str, object]:
    """Serialize one eligibility decision."""
    decision = validate_eligibility_decision(value)
    result: dict[str, object] = dict(decision)
    result["exclusion_reason"] = decision["exclusion_reason"].value
    result["epoch_policy"] = decision["epoch_policy"].value
    return result


def eligibility_decision_context_signature(value: object) -> str:
    """Return the stable request and policy signature for one decision."""
    decision = validate_eligibility_decision(value)
    data = {
        "evaluation_time": decision["evaluation_time"],
        "evaluation_time_available": decision["evaluation_time_available"],
        "namespace": decision["namespace"],
        "knowledge_epoch": decision["knowledge_epoch"],
        "knowledge_epoch_available": decision["knowledge_epoch_available"],
        "artifact_repository_available": decision["artifact_repository_available"],
        "epoch_policy": decision["epoch_policy"].value,
    }
    result = _json_text(data)
    return result


def _timestamp_to_datetime(value: str) -> datetime:
    result = datetime.fromisoformat(value[:-1] + "+00:00")
    return result


def _decision(
    artifact: CachedResponseArtifact,
    context: EligibilityContext,
    epoch_policy: EpochEligibilityPolicy,
    lifecycle_base_eligible: bool,
    reason: EligibilityExclusionReason,
) -> EligibilityDecision:
    result = eligibility_decision(
        artifact["statement_id"],
        artifact["generation"],
        lifecycle_base_eligible,
        reason == EligibilityExclusionReason.ELIGIBLE,
        reason,
        context["evaluation_time"],
        context["evaluation_time_available"],
        context["namespace"],
        context["knowledge_epoch"],
        context["knowledge_epoch_available"],
        context["artifact_repository_available"],
        epoch_policy,
    )
    return result


def evaluate_artifact_eligibility(
    artifact: CachedResponseArtifact,
    context: EligibilityContext,
    epoch_policy: EpochEligibilityPolicy,
) -> EligibilityDecision:
    """Evaluate one artifact without ambient time, I/O, mutation, or inference."""

    try:
        artifact = validate_cached_response_artifact(artifact)
    except InvalidRequestError as error:
        raise InvalidRequestError("artifact must be a CachedResponseArtifact") from error
    try:
        context = validate_eligibility_context(context)
    except InvalidRequestError as error:
        raise InvalidRequestError("context must be an EligibilityContext") from error
    if not isinstance(epoch_policy, EpochEligibilityPolicy):
        raise InvalidRequestError("epoch_policy must be an EpochEligibilityPolicy")

    lifecycle = lifecycle_base_eligibility(artifact["lifecycle"])
    if not context["artifact_repository_available"]:
        result = _decision(
            artifact,
            context,
            epoch_policy,
            lifecycle["direct_answer_eligible"],
            EligibilityExclusionReason.ARTIFACT_REPOSITORY_UNAVAILABLE,
        )
        return result
    if not context["evaluation_time_available"]:
        result = _decision(
            artifact,
            context,
            epoch_policy,
            lifecycle["direct_answer_eligible"],
            EligibilityExclusionReason.EVALUATION_TIME_UNAVAILABLE,
        )
        return result
    if artifact["scope"]["namespace"] != context["namespace"]:
        result = _decision(
            artifact,
            context,
            epoch_policy,
            lifecycle["direct_answer_eligible"],
            EligibilityExclusionReason.SCOPE_NAMESPACE_MISMATCH,
        )
        return result
    if not lifecycle["direct_answer_eligible"]:
        result = _decision(
            artifact,
            context,
            epoch_policy,
            False,
            _LIFECYCLE_EXCLUSION_REASONS[lifecycle["reason"]],
        )
        return result

    evaluation_time = _timestamp_to_datetime(context["evaluation_time"])
    valid_from = _timestamp_to_datetime(artifact["valid_from"]) if artifact["valid_from_available"] else evaluation_time
    valid_until = _timestamp_to_datetime(artifact["valid_until"]) if artifact["valid_until_available"] else evaluation_time
    if artifact["valid_from_available"] and artifact["valid_until_available"] and valid_from >= valid_until:
        result = _decision(
            artifact,
            context,
            epoch_policy,
            True,
            EligibilityExclusionReason.VALIDITY_INTERVAL_INVALID,
        )
        return result
    if artifact["valid_from_available"] and evaluation_time < valid_from:
        result = _decision(
            artifact,
            context,
            epoch_policy,
            True,
            EligibilityExclusionReason.NOT_YET_VALID,
        )
        return result
    if artifact["valid_until_available"] and evaluation_time >= valid_until:
        result = _decision(
            artifact,
            context,
            epoch_policy,
            True,
            EligibilityExclusionReason.EXPIRED,
        )
        return result

    if epoch_policy == EpochEligibilityPolicy.REQUIRE_MATCH and not artifact["knowledge_epoch_available"]:
        result = _decision(
            artifact,
            context,
            epoch_policy,
            True,
            EligibilityExclusionReason.ARTIFACT_EPOCH_UNAVAILABLE,
        )
        return result
    if artifact["knowledge_epoch_available"]:
        if not context["knowledge_epoch_available"]:
            result = _decision(
                artifact,
                context,
                epoch_policy,
                True,
                EligibilityExclusionReason.CONTEXT_EPOCH_UNAVAILABLE,
            )
            return result
        if artifact["knowledge_epoch"] != context["knowledge_epoch"]:
            result = _decision(
                artifact,
                context,
                epoch_policy,
                True,
                EligibilityExclusionReason.KNOWLEDGE_EPOCH_MISMATCH,
            )
            return result
    result = _decision(
        artifact,
        context,
        epoch_policy,
        True,
        EligibilityExclusionReason.ELIGIBLE,
    )
    return result


def index_projection_from_artifact(
    artifact: CachedResponseArtifact,
    decision: EligibilityDecision,
) -> IndexProjection:
    """Derive the disposable Section 2 projection for one exact decision."""

    try:
        artifact = validate_cached_response_artifact(artifact)
    except InvalidRequestError as error:
        raise InvalidRequestError("artifact must be a CachedResponseArtifact") from error
    try:
        decision = validate_eligibility_decision(decision)
    except InvalidRequestError as error:
        raise InvalidRequestError("decision must be an EligibilityDecision") from error
    if artifact["statement_id"] != decision["statement_id"] or artifact["generation"] != decision["generation"]:
        raise ConflictError("eligibility decision does not identify the artifact generation")
    if artifact["scope"]["namespace"] != decision["namespace"]:
        raise ConflictError("eligibility decision namespace does not match the artifact")
    exclusion_reason = "" if decision["direct_answer_eligible"] else decision["exclusion_reason"].value
    result = index_projection(
        artifact["statement_id"],
        artifact["generation"],
        retrieval_representation_bindings(artifact["retrieval"], artifact["scope"]),
        artifact["support_claim_ids"],
        decision["direct_answer_eligible"],
        exclusion_reason,
    )
    return result


ContextualExactLookupResult = TypedDict(
    "ContextualExactLookupResult",
    {
        "lookup": ExactLookupResult,
        "decisions": tuple[EligibilityDecision, ...],
        "context_signature": str,
        "index_refreshed": bool,
        "index_state_generation": int,
    },
)


def contextual_exact_lookup_result(
    lookup: object,
    decisions: object,
    context_signature: object,
    index_refreshed: object,
    index_state_generation: object,
) -> ContextualExactLookupResult:
    """Build one context-revalidated exact lookup result."""
    try:
        normalized_lookup = validate_exact_lookup_result(lookup)
    except InvalidRequestError as error:
        raise InvalidRequestError("contextual exact lookup must be an ExactLookupResult") from error
    if not isinstance(decisions, tuple):
        raise InvalidRequestError("contextual exact decisions must be a tuple")
    normalized_decisions = tuple(validate_eligibility_decision(decision) for decision in decisions)
    normalized_signature = _require_text(
        context_signature,
        "contextual exact context_signature",
        MAX_ELIGIBILITY_CONTEXT_SIGNATURE_BYTES,
        allow_empty=False,
    )
    refreshed = _require_bool(index_refreshed, "contextual exact index_refreshed")
    if isinstance(index_state_generation, bool) or not isinstance(index_state_generation, int) or index_state_generation < 1:
        raise InvalidRequestError("contextual exact index_state_generation must be a positive integer")
    result: ContextualExactLookupResult = {
        "lookup": normalized_lookup,
        "decisions": normalized_decisions,
        "context_signature": normalized_signature,
        "index_refreshed": refreshed,
        "index_state_generation": index_state_generation,
    }
    return result


def validate_contextual_exact_lookup_result(value: object) -> ContextualExactLookupResult:
    """Revalidate and copy one contextual exact lookup result."""
    data = _require_exact_mapping(value, "ContextualExactLookupResult", CONTEXTUAL_EXACT_LOOKUP_RESULT_FIELDS)
    result = contextual_exact_lookup_result(
        data["lookup"],
        data["decisions"],
        data["context_signature"],
        data["index_refreshed"],
        data["index_state_generation"],
    )
    return result


def contextual_exact_lookup_result_to_dict(value: object) -> dict[str, object]:
    """Serialize one contextual exact lookup result."""
    lookup_result = validate_contextual_exact_lookup_result(value)
    result = {
        "lookup": exact_lookup_result_to_dict(lookup_result["lookup"]),
        "decisions": [eligibility_decision_to_dict(decision) for decision in lookup_result["decisions"]],
        "context_signature": lookup_result["context_signature"],
        "index_refreshed": lookup_result["index_refreshed"],
        "index_state_generation": lookup_result["index_state_generation"],
    }
    return result


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
            try:
                validated_artifact = validate_cached_response_artifact(artifact)
            except InvalidRequestError as error:
                raise InvalidRequestError("contextual exact artifact values must be CachedResponseArtifact values") from error
            if validated_artifact["statement_id"] != statement_id:
                raise InvalidRequestError("contextual exact artifact key must match its statement_id")
            validated[statement_id] = validated_artifact
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
        try:
            key = validate_scoped_retrieval_key(key)
        except IdentityValidationError as error:
            raise InvalidRequestError("contextual exact key must be a ScopedRetrievalKey") from error
        try:
            context = validate_eligibility_context(context)
        except InvalidRequestError as error:
            raise InvalidRequestError("context must be an EligibilityContext") from error
        if not isinstance(epoch_policy, EpochEligibilityPolicy):
            raise InvalidRequestError("epoch_policy must be an EpochEligibilityPolicy")
        for _attempt in range(self._max_refresh_retries):
            state = self._indexes.snapshot()
            key_signature = scoped_retrieval_key_signature(key)
            owner_ids = tuple(dict.fromkeys(owner["statement_id"] for owner in state["retrieval_to_owners"].get(key_signature, ())))
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
                    state["state_generation"],
                )
            except ConflictError:
                continue
            signature = (
                eligibility_decision_context_signature(decisions[0])
                if decisions
                else _json_text(
                    {
                        "evaluation_time": context["evaluation_time"],
                        "evaluation_time_available": context["evaluation_time_available"],
                        "namespace": context["namespace"],
                        "knowledge_epoch": context["knowledge_epoch"],
                        "knowledge_epoch_available": context["knowledge_epoch_available"],
                        "artifact_repository_available": context["artifact_repository_available"],
                        "epoch_policy": epoch_policy.value,
                    }
                )
            )
            result = contextual_exact_lookup_result(
                lookup,
                tuple(decisions),
                signature,
                refreshed,
                state_generation,
            )
            return result
        raise ConflictError("exact eligibility refresh did not converge within the retry bound")


def eligibility_context_from_trusted_input(
    trusted_input: TrustedEligibilityInput,
    artifact_repository_available: bool,
) -> EligibilityContext:
    """Construct an eligibility context from one trusted integration snapshot."""
    try:
        trusted_input = validate_trusted_eligibility_input(trusted_input)
    except InvalidRequestError as error:
        raise InvalidRequestError("trusted_input must be a TrustedEligibilityInput") from error
    repository_available = _require_bool(artifact_repository_available, "artifact_repository_available")
    source = EpochSource.TRUSTED_INTEGRATION if trusted_input["knowledge_epoch_available"] else EpochSource.UNAVAILABLE
    result = eligibility_context(
        trusted_input["evaluation_time"],
        trusted_input["evaluation_time_available"],
        trusted_input["namespace"],
        trusted_input["knowledge_epoch"],
        trusted_input["knowledge_epoch_available"],
        repository_available,
        source,
    )
    return result


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
        try:
            scope = validate_scope_key(scope)
        except IdentityValidationError as error:
            raise InvalidRequestError("eligibility scope must be a ScopeKey") from error
        repository_available = _require_bool(artifact_repository_available, "artifact_repository_available")
        evaluation_time = _datetime_to_timestamp(self._clock())
        epoch = self._namespace_epochs.get(scope["namespace"])
        source = EpochSource.STANDALONE if epoch["knowledge_epoch_available"] else EpochSource.UNAVAILABLE
        result = eligibility_context(
            evaluation_time,
            True,
            scope["namespace"],
            epoch["knowledge_epoch"],
            epoch["knowledge_epoch_available"],
            repository_available,
            source,
        )
        return result
