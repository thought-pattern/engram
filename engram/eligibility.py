"""Request-scoped time, availability, and cache eligibility contracts."""

from datetime import datetime, timedelta
from json import JSONDecodeError as json_JSONDecodeError, dumps as json_dumps, loads as json_loads

from engram.artifacts import lifecycle_base_eligibility, validate_cached_response_artifact
from engram.constants import (
    CONTEXTUAL_EXACT_LOOKUP_RESULT_FIELDS,
    ELIGIBILITY_CONTEXT_FIELDS,
    ELIGIBILITY_CONTEXT_SCHEMA_VERSION,
    ELIGIBILITY_DECISION_FIELDS,
    EXACT_LOOKUP_RESULT_FIELDS,
    LIFECYCLE_EXCLUSION_REASONS,
    MAX_ELIGIBILITY_CONTEXT_SIGNATURE_BYTES,
    MAX_ELIGIBILITY_STATEMENT_ID_BYTES,
    MAX_ELIGIBILITY_TIMESTAMP_BYTES,
    MAX_EXACT_LOOKUP_OWNERS,
    MAX_EXACT_LOOKUP_PROVENANCE_BYTES,
    MAX_EXACT_LOOKUP_REPRESENTATION_BYTES,
    MAX_EXACT_LOOKUP_STATEMENT_ID_BYTES,
    EligibilityExclusionReason,
    ExactLookupOutcome,
    LifecycleDecisionReason,
    LifecycleState,
    RetrievalOrigin,
)
from engram.errors import IdentityValidationError, InvalidRequestError
from engram.identity import (
    MAX_NAMESPACE_BYTES,
    retrieval_representation_bindings,
    scope_key,
    scoped_retrieval_key_to_dict,
    validate_scope_key,
    validate_scoped_retrieval_key,
)


def require_exact_mapping(value: object, name: str, keys: set[str]) -> dict:
    """Require one mapping with exactly the declared keys."""
    if not isinstance(value, dict):
        raise InvalidRequestError(f"{name} must be an object")
    actual = set(value)
    if actual != keys:
        missing = sorted(keys - actual)
        extra = sorted(actual - keys)
        raise InvalidRequestError(f"{name} has invalid fields: missing={missing}, extra={extra}")
    return value


def require_text(value: object, name: str, maximum_bytes: int, *, allow_empty: bool) -> str:
    """Validate bounded Unicode contract text."""
    if not isinstance(value, str):
        raise InvalidRequestError(f"{name} must be a string")
    if not allow_empty and not value:
        raise InvalidRequestError(f"{name} must not be empty")
    if len(value.encode("utf-8")) > maximum_bytes:
        raise InvalidRequestError(f"{name} exceeds the UTF-8 limit of {maximum_bytes} bytes")
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise InvalidRequestError(f"{name} contains a control character")
    return value


def require_namespace(value: object) -> str:
    """Validate one scope namespace."""
    namespace = require_text(value, "eligibility namespace", MAX_NAMESPACE_BYTES, allow_empty=True)
    scope_key(namespace=namespace)
    return namespace


def require_bool(value: object, name: str) -> bool:
    """Validate one exact boolean."""
    if not isinstance(value, bool):
        raise InvalidRequestError(f"{name} must be a boolean")
    return value


def require_positive_version(value: object, expected: int, name: str) -> int:
    """Validate one exact positive schema version."""
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise InvalidRequestError(f"{name} must be a positive integer")
    if value != expected:
        raise InvalidRequestError(f"unsupported {name}: {value}; expected {expected}")
    return value


def require_timestamp(value: object, available: object, name: str) -> tuple[str, bool]:
    """Validate one availability-tagged canonical UTC timestamp."""
    presence = require_bool(available, f"{name}_available")
    text = require_text(value, name, MAX_ELIGIBILITY_TIMESTAMP_BYTES, allow_empty=not presence)
    if not presence:
        if text:
            raise InvalidRequestError(f"{name} must be empty when unavailable")
        return text, presence
    if not text.endswith("Z"):
        raise InvalidRequestError(f"{name} must be a canonical RFC 3339 UTC timestamp ending in Z")
    try:
        parsed = datetime.fromisoformat(text[:-1] + "+00:00")
    except ValueError as error:
        raise InvalidRequestError(f"{name} must be a canonical RFC 3339 UTC timestamp") from error
    if parsed.isoformat().replace("+00:00", "Z") != text:
        raise InvalidRequestError(f"{name} must use the canonical RFC 3339 UTC representation")
    return text, presence


def datetime_to_timestamp(value: object) -> str:
    """Convert a timezone-aware UTC datetime to canonical contract text."""
    if not isinstance(value, datetime):
        raise InvalidRequestError("eligibility clock must return a datetime")
    if not value.tzinfo or value.utcoffset() != timedelta(0):
        raise InvalidRequestError("eligibility clock must return a timezone-aware UTC datetime")
    text = value.isoformat().replace("+00:00", "Z")
    require_timestamp(text, True, "eligibility evaluation_time")
    return text


def eligibility_context(
    evaluation_time: object,
    evaluation_time_available: object,
    namespace: object,
    artifact_repository_available: object,
    schema_version: object = ELIGIBILITY_CONTEXT_SCHEMA_VERSION,
) -> dict:
    """Build one request-scoped cache-eligibility context."""
    result: dict = {
        "schema_version": require_positive_version(
            schema_version,
            ELIGIBILITY_CONTEXT_SCHEMA_VERSION,
            "eligibility context schema_version",
        ),
        "evaluation_time": "",
        "evaluation_time_available": False,
        "namespace": require_namespace(namespace),
        "artifact_repository_available": require_bool(
            artifact_repository_available,
            "artifact_repository_available",
        ),
    }
    timestamp, available = require_timestamp(
        evaluation_time,
        evaluation_time_available,
        "eligibility evaluation_time",
    )
    result["evaluation_time"] = timestamp
    result["evaluation_time_available"] = available
    return result


def validate_eligibility_context(value: object) -> dict:
    """Validate and copy one eligibility context."""
    data = require_exact_mapping(value, "EligibilityContext", ELIGIBILITY_CONTEXT_FIELDS)
    result = eligibility_context(
        data.get("evaluation_time", ()),
        data.get("evaluation_time_available", ()),
        data.get("namespace", ()),
        data.get("artifact_repository_available", ()),
        data.get("schema_version", ()),
    )
    return result


def eligibility_context_to_dict(value: object) -> dict:
    """Return the external dictionary for one eligibility context."""
    result = dict(validate_eligibility_context(value))
    return result


def eligibility_context_to_json(value: object) -> str:
    """Encode one eligibility context for an external JSON interface."""
    result = json_dumps(eligibility_context_to_dict(value), ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return result


def eligibility_context_from_dict(value: object) -> dict:
    """Decode one external eligibility-context dictionary."""
    result = validate_eligibility_context(value)
    return result


def eligibility_context_from_json(value: object) -> dict:
    """Decode one external eligibility-context JSON value."""
    if not isinstance(value, str):
        raise InvalidRequestError("EligibilityContext JSON must be a string")
    try:
        data = json_loads(value)
    except json_JSONDecodeError as error:
        raise InvalidRequestError("EligibilityContext JSON is malformed") from error
    result = eligibility_context_from_dict(data)
    return result


def eligibility_decision(
    statement_id: object,
    generation: object,
    lifecycle_base_eligible: object,
    direct_answer_eligible: object,
    exclusion_reason: object,
    evaluation_time: object,
    evaluation_time_available: object,
    namespace: object,
    artifact_repository_available: object,
) -> dict:
    """Build one complete cache-eligibility decision."""
    normalized_statement_id = require_text(
        statement_id,
        "eligibility statement_id",
        MAX_ELIGIBILITY_STATEMENT_ID_BYTES,
        allow_empty=False,
    )
    if isinstance(generation, bool) or not isinstance(generation, int) or generation < 1:
        raise InvalidRequestError("eligibility generation must be a positive integer")
    base_eligible = require_bool(lifecycle_base_eligible, "lifecycle_base_eligible")
    answer_eligible = require_bool(direct_answer_eligible, "direct_answer_eligible")
    if not isinstance(exclusion_reason, EligibilityExclusionReason):
        raise InvalidRequestError("exclusion_reason must be an EligibilityExclusionReason")
    if answer_eligible != (exclusion_reason == EligibilityExclusionReason.ELIGIBLE):
        raise InvalidRequestError("direct_answer_eligible must agree with exclusion_reason")
    timestamp, available = require_timestamp(
        evaluation_time,
        evaluation_time_available,
        "eligibility evaluation_time",
    )
    result = {
        "statement_id": normalized_statement_id,
        "generation": generation,
        "lifecycle_base_eligible": base_eligible,
        "direct_answer_eligible": answer_eligible,
        "exclusion_reason": exclusion_reason,
        "evaluation_time": timestamp,
        "evaluation_time_available": available,
        "namespace": require_namespace(namespace),
        "artifact_repository_available": require_bool(
            artifact_repository_available,
            "artifact_repository_available",
        ),
    }
    return result


def validate_eligibility_decision(value: object) -> dict:
    """Validate and copy one eligibility decision."""
    data = require_exact_mapping(value, "EligibilityDecision", ELIGIBILITY_DECISION_FIELDS)
    result = eligibility_decision(
        data.get("statement_id", ()),
        data.get("generation", ()),
        data.get("lifecycle_base_eligible", ()),
        data.get("direct_answer_eligible", ()),
        data.get("exclusion_reason", ()),
        data.get("evaluation_time", ()),
        data.get("evaluation_time_available", ()),
        data.get("namespace", ()),
        data.get("artifact_repository_available", ()),
    )
    return result


def eligibility_decision_to_dict(value: object) -> dict:
    """Return the external dictionary for one eligibility decision."""
    decision = validate_eligibility_decision(value)
    result: dict = dict(decision)
    result["exclusion_reason"] = decision.get("exclusion_reason", EligibilityExclusionReason.ELIGIBLE).value
    return result


def eligibility_context_signature(values: tuple[object, ...]) -> str:
    """Encode context values as printable, unambiguous length-prefixed text."""
    if not isinstance(values, tuple):
        raise InvalidRequestError("eligibility context signature values must be a tuple")
    parts = []
    for value in values:
        text = str(value)
        parts.append(f"{len(text.encode('utf-8'))}:{text}")
    result = "|".join(parts)
    require_text(
        result,
        "eligibility context signature",
        MAX_ELIGIBILITY_CONTEXT_SIGNATURE_BYTES,
        allow_empty=False,
    )
    return result


def eligibility_decision_context_signature(value: object) -> str:
    """Return a stable request-context signature without internal JSON."""
    decision = validate_eligibility_decision(value)
    result = eligibility_context_signature(
        (
            str(decision.get("evaluation_time", "")),
            str(decision.get("evaluation_time_available", False)),
            str(decision.get("namespace", "")),
            str(decision.get("artifact_repository_available", False)),
        )
    )
    return result


def timestamp_to_datetime(value: str) -> datetime:
    """Parse one previously validated canonical timestamp."""
    result = datetime.fromisoformat(value[:-1] + "+00:00")
    return result


def eligibility_result(
    artifact: dict,
    context: dict,
    lifecycle_base_eligible: bool,
    reason: EligibilityExclusionReason,
) -> dict:
    """Build a decision from already validated values."""
    result = eligibility_decision(
        artifact.get("statement_id", ""),
        artifact.get("generation", 0),
        lifecycle_base_eligible,
        reason == EligibilityExclusionReason.ELIGIBLE,
        reason,
        context.get("evaluation_time", ""),
        context.get("evaluation_time_available", False),
        context.get("namespace", ""),
        context.get("artifact_repository_available", False),
    )
    return result


def evaluate_artifact_eligibility(
    artifact: dict,
    context: dict,
) -> dict:
    """Evaluate one cache artifact without I/O, mutation, or inference."""
    try:
        current_artifact = validate_cached_response_artifact(artifact)
    except InvalidRequestError as error:
        raise InvalidRequestError("artifact must be a CachedResponseArtifact") from error
    try:
        current_context = validate_eligibility_context(context)
    except InvalidRequestError as error:
        raise InvalidRequestError("context must be an EligibilityContext") from error
    lifecycle = lifecycle_base_eligibility(current_artifact.get("lifecycle", LifecycleState.RETIRED))
    lifecycle_eligible = lifecycle.get("direct_answer_eligible", False)
    if not current_context.get("artifact_repository_available", False):
        result = eligibility_result(
            current_artifact,
            current_context,
            lifecycle_eligible,
            EligibilityExclusionReason.ARTIFACT_REPOSITORY_UNAVAILABLE,
        )
        return result
    if not current_context.get("evaluation_time_available", False):
        result = eligibility_result(
            current_artifact,
            current_context,
            lifecycle_eligible,
            EligibilityExclusionReason.EVALUATION_TIME_UNAVAILABLE,
        )
        return result
    if current_artifact.get("scope", {}).get("namespace", "") != current_context.get("namespace", ""):
        result = eligibility_result(
            current_artifact,
            current_context,
            lifecycle_eligible,
            EligibilityExclusionReason.SCOPE_NAMESPACE_MISMATCH,
        )
        return result
    if not lifecycle_eligible:
        result = eligibility_result(
            current_artifact,
            current_context,
            False,
            LIFECYCLE_EXCLUSION_REASONS.get(
                lifecycle.get("reason", LifecycleDecisionReason.RETIRED),
                EligibilityExclusionReason.LIFECYCLE_RETIRED,
            ),
        )
        return result
    evaluation_time = timestamp_to_datetime(current_context.get("evaluation_time", ""))
    valid_from = (
        timestamp_to_datetime(current_artifact.get("valid_from", ""))
        if current_artifact.get("valid_from_available", False)
        else evaluation_time
    )
    valid_until = (
        timestamp_to_datetime(current_artifact.get("valid_until", ""))
        if current_artifact.get("valid_until_available", False)
        else evaluation_time
    )
    if (
        current_artifact.get("valid_from_available", False)
        and current_artifact.get("valid_until_available", False)
        and valid_from >= valid_until
    ):
        result = eligibility_result(
            current_artifact,
            current_context,
            True,
            EligibilityExclusionReason.VALIDITY_INTERVAL_INVALID,
        )
        return result
    if current_artifact.get("valid_from_available", False) and evaluation_time < valid_from:
        result = eligibility_result(current_artifact, current_context, True, EligibilityExclusionReason.NOT_YET_VALID)
        return result
    if current_artifact.get("valid_until_available", False) and evaluation_time >= valid_until:
        result = eligibility_result(current_artifact, current_context, True, EligibilityExclusionReason.EXPIRED)
        return result
    result = eligibility_result(current_artifact, current_context, True, EligibilityExclusionReason.ELIGIBLE)
    return result


def exact_lookup_result(
    outcome: object,
    key: object,
    statement_id: object,
    generation: object,
    provenance: object,
    representation: object,
    owner_statement_ids: object,
    truncated: object,
) -> dict:
    """Build one direct artifact lookup result without an implicit collision winner."""
    if not isinstance(outcome, ExactLookupOutcome):
        raise InvalidRequestError("exact lookup outcome must be an ExactLookupOutcome")
    try:
        normalized_key = validate_scoped_retrieval_key(key)
    except IdentityValidationError as error:
        raise InvalidRequestError("exact lookup key must be a ScopedRetrievalKey") from error
    selected = outcome == ExactLookupOutcome.FOUND
    normalized_statement_id = require_text(
        statement_id,
        "exact lookup statement_id",
        MAX_EXACT_LOOKUP_STATEMENT_ID_BYTES,
        allow_empty=not selected,
    )
    if isinstance(generation, bool) or not isinstance(generation, int) or generation < (1 if selected else 0):
        raise InvalidRequestError("exact lookup generation is invalid")
    if not selected and (generation or normalized_statement_id):
        raise InvalidRequestError("non-found exact lookup must not select an artifact")
    normalized_provenance = require_text(
        provenance,
        "exact lookup provenance",
        MAX_EXACT_LOOKUP_PROVENANCE_BYTES,
        allow_empty=not selected,
    )
    normalized_representation = require_text(
        representation,
        "exact lookup representation",
        MAX_EXACT_LOOKUP_REPRESENTATION_BYTES,
        allow_empty=not selected,
    )
    if selected:
        try:
            RetrievalOrigin(normalized_provenance)
        except ValueError as error:
            raise InvalidRequestError("exact lookup provenance must be a RetrievalOrigin value") from error
    elif normalized_provenance or normalized_representation:
        raise InvalidRequestError("non-found exact lookup selected fields must be empty")
    if not isinstance(owner_statement_ids, tuple):
        raise InvalidRequestError("exact lookup owner_statement_ids must be a tuple")
    if len(owner_statement_ids) > MAX_EXACT_LOOKUP_OWNERS:
        raise InvalidRequestError("exact lookup owner count exceeds the request bound")
    owners = tuple(
        require_text(owner, "exact lookup owner", MAX_EXACT_LOOKUP_STATEMENT_ID_BYTES, allow_empty=False)
        for owner in owner_statement_ids
    )
    if len(set(owners)) != len(owners):
        raise InvalidRequestError("exact lookup owner_statement_ids must be unique")
    if not isinstance(truncated, bool):
        raise InvalidRequestError("exact lookup truncated must be a boolean")
    if selected and not truncated and normalized_statement_id not in owners:
        raise InvalidRequestError("found exact lookup owners must include the selected artifact")
    return {
        "outcome": outcome,
        "key": normalized_key,
        "statement_id": normalized_statement_id,
        "generation": generation,
        "provenance": normalized_provenance,
        "representation": normalized_representation,
        "owner_statement_ids": owners,
        "truncated": truncated,
    }


def validate_exact_lookup_result(value: object) -> dict:
    """Validate and copy one direct artifact lookup result."""
    data = require_exact_mapping(value, "ExactLookupResult", EXACT_LOOKUP_RESULT_FIELDS)
    result = exact_lookup_result(
        data.get("outcome", ()),
        data.get("key", ()),
        data.get("statement_id", ()),
        data.get("generation", ()),
        data.get("provenance", ()),
        data.get("representation", ()),
        data.get("owner_statement_ids", ()),
        data.get("truncated", ()),
    )
    return result


def exact_lookup_result_to_dict(value: object) -> dict:
    """Return the external dictionary for one direct artifact lookup."""
    lookup = validate_exact_lookup_result(value)
    result = {
        "outcome": lookup.get("outcome", ExactLookupOutcome.MISS).value,
        "key": scoped_retrieval_key_to_dict(lookup.get("key", {})),
        "statement_id": lookup.get("statement_id", ""),
        "generation": lookup.get("generation", 0),
        "provenance": lookup.get("provenance", ""),
        "representation": lookup.get("representation", ""),
        "owner_statement_ids": list(lookup.get("owner_statement_ids", ())),
        "truncated": lookup.get("truncated", False),
    }
    return result


def contextual_exact_lookup_result(
    lookup: object,
    decisions: object,
    context_signature: object,
) -> dict:
    """Build one context-revalidated exact lookup result."""
    try:
        normalized_lookup = validate_exact_lookup_result(lookup)
    except InvalidRequestError as error:
        raise InvalidRequestError("contextual exact lookup must be an ExactLookupResult") from error
    if not isinstance(decisions, tuple):
        raise InvalidRequestError("contextual exact decisions must be a tuple")
    normalized_decisions = tuple(validate_eligibility_decision(decision) for decision in decisions)
    normalized_signature = require_text(
        context_signature,
        "contextual exact context_signature",
        MAX_ELIGIBILITY_CONTEXT_SIGNATURE_BYTES,
        allow_empty=False,
    )
    return {
        "lookup": normalized_lookup,
        "decisions": normalized_decisions,
        "context_signature": normalized_signature,
    }


def validate_contextual_exact_lookup_result(value: object) -> dict:
    """Validate and copy one contextual exact lookup result."""
    data = require_exact_mapping(value, "ContextualExactLookupResult", CONTEXTUAL_EXACT_LOOKUP_RESULT_FIELDS)
    result = contextual_exact_lookup_result(
        data.get("lookup", ()),
        data.get("decisions", ()),
        data.get("context_signature", ()),
    )
    return result


def contextual_exact_lookup_result_to_dict(value: object) -> dict:
    """Return the external dictionary for one contextual exact lookup."""
    lookup_result = validate_contextual_exact_lookup_result(value)
    result = {
        "lookup": exact_lookup_result_to_dict(lookup_result.get("lookup", {})),
        "decisions": [eligibility_decision_to_dict(decision) for decision in lookup_result.get("decisions", ())],
        "context_signature": lookup_result.get("context_signature", ""),
    }
    return result


class ContextualExactLookup:
    """Inspect the current artifact snapshot for one eligible exact result."""

    def __init__(
        self,
        artifacts: dict[str, dict],
        trusted_artifacts: bool = False,
    ) -> None:
        if not isinstance(artifacts, dict):
            raise InvalidRequestError("contextual exact artifacts must be an object")
        if not isinstance(trusted_artifacts, bool):
            raise InvalidRequestError("trusted_artifacts must be a boolean")
        if trusted_artifacts:
            self.artifacts = artifacts
        else:
            validated = {}
            for statement_id, artifact in artifacts.items():
                if not isinstance(statement_id, str) or not statement_id:
                    raise InvalidRequestError("contextual exact artifact keys must be non-empty strings")
                validated_artifact = validate_cached_response_artifact(artifact)
                if validated_artifact.get("statement_id") != statement_id:
                    raise InvalidRequestError("contextual exact artifact key must match its statement_id")
                validated[statement_id] = validated_artifact
            self.artifacts = dict(validated)

    def exact_lookup(
        self,
        key: dict,
        context: dict,
    ) -> dict:
        try:
            current_key = validate_scoped_retrieval_key(key)
        except IdentityValidationError as error:
            raise InvalidRequestError("contextual exact key must be a ScopedRetrievalKey") from error
        current_context = validate_eligibility_context(context)
        owners = []
        eligible = []
        decisions = []
        for statement_id in sorted(self.artifacts):
            artifact = self.artifacts.get(statement_id, {})
            matched_binding = ()
            for binding in retrieval_representation_bindings(artifact.get("retrieval", {}), artifact.get("scope", {})):
                if binding.get("key") == current_key:
                    matched_binding = binding
                    break
            if not matched_binding:
                continue
            decision = evaluate_artifact_eligibility(artifact, current_context)
            decisions.append(decision)
            owners.append(statement_id)
            if decision.get("direct_answer_eligible", False):
                eligible.append((artifact, matched_binding))
        bounded_owners = tuple(owners[:MAX_EXACT_LOOKUP_OWNERS])
        truncated = len(owners) > len(bounded_owners)
        if len(eligible) == 1:
            artifact, binding = eligible[0]
            lookup = exact_lookup_result(
                ExactLookupOutcome.FOUND,
                current_key,
                artifact.get("statement_id", ""),
                artifact.get("generation", 0),
                binding.get("origin", RetrievalOrigin.CANONICAL).value,
                binding.get("representation", ""),
                bounded_owners,
                truncated,
            )
        else:
            outcome = ExactLookupOutcome.COLLISION if len(eligible) > 1 else ExactLookupOutcome.MISS
            lookup = exact_lookup_result(outcome, current_key, "", 0, "", "", bounded_owners, truncated)
        signature = (
            eligibility_decision_context_signature(decisions[0])
            if decisions
            else eligibility_context_signature(
                (
                    current_context.get("evaluation_time", ""),
                    current_context.get("evaluation_time_available", False),
                    current_context.get("namespace", ""),
                    current_context.get("artifact_repository_available", False),
                )
            )
        )
        result = contextual_exact_lookup_result(lookup, tuple(decisions), signature)
        return result


class EligibilityContextCapture:
    """Capture exactly one trusted time snapshot per request."""

    def __init__(self, clock: object) -> None:
        if not callable(clock):
            raise InvalidRequestError("eligibility clock must be callable")
        self.clock = clock

    def current_time(self) -> datetime:
        """Read and validate the configured request clock boundary."""
        value = self.clock()
        if not isinstance(value, datetime):
            raise InvalidRequestError("eligibility clock must return a datetime")
        return value

    def capture_standalone(self, scope: dict, artifact_repository_available: bool) -> dict:
        try:
            current_scope = validate_scope_key(scope)
        except IdentityValidationError as error:
            raise InvalidRequestError("eligibility scope must be a ScopeKey") from error
        repository_available = require_bool(artifact_repository_available, "artifact_repository_available")
        result = eligibility_context(
            datetime_to_timestamp(self.current_time()),
            True,
            current_scope.get("namespace", ""),
            repository_available,
        )
        return result
