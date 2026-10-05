"""Authoritative accepted-response artifact domain contracts.

Section 3 owns these transport-neutral types.  This first slice establishes
the lifecycle vocabulary and policies without depending on storage tier,
residency, indexes, or adapters.
"""

from json import dumps as json_dumps
from math import isfinite as math_isfinite

from engram.constants import (
    ARTIFACT_PROVENANCE_FIELDS,
    ARTIFACT_STATISTICS_FIELDS,
    CACHED_RESPONSE_ARTIFACT_FIELDS,
    LEGAL_LIFECYCLE_TRANSITIONS,
    LIFECYCLE_AUDIT_FIELDS,
    LIFECYCLE_AUDIT_KEY,
    LIFECYCLE_BASE_DECISION_FIELDS,
    LIFECYCLE_INELIGIBLE_REASONS,
    LIFECYCLE_TRANSITION_DECISION_FIELDS,
    MAX_ARTIFACT_ENUM_BYTES,
    MAX_ARTIFACT_ID_BYTES,
    MAX_CALLER_ID_BYTES,
    MAX_LIFECYCLE_AUDIT_DETAIL_BYTES,
    MAX_METADATA_BYTES,
    MAX_METADATA_DEPTH,
    MAX_METADATA_ITEMS,
    MAX_METADATA_KEY_BYTES,
    MAX_METADATA_STRING_BYTES,
    MAX_REQUEST_ID_BYTES,
    MAX_RESPONSE_BYTES,
    MAX_SOURCE_LABEL_BYTES,
    MAX_SUPPORT_REFERENCES,
    TERMINAL_LIFECYCLE_STATES as TERMINAL_LIFECYCLE_STATES,
    LifecycleDecisionReason,
    LifecycleMutationReason,
    LifecycleOperation,
    LifecycleState,
    MutationOperation,
    Tier,
)
from engram.errors import IdentityValidationError, InvalidRequestError, LifecycleError
from engram.identity import (
    query_identity_from_dict,
    query_identity_to_dict,
    retrieval_representation_from_dict,
    retrieval_representation_to_dict,
    scope_key_from_dict,
    scope_key_to_dict,
    validate_authoritative_identity,
    validate_query_identity,
    validate_retrieval_representation,
    validate_scope_key,
)
from engram.support import validate_support_references, validate_support_visibility
from engram.validation import (
    Characters,
    require_available_utc_timestamp,
    require_bool,
    require_text,
    require_utc_timestamp,
)


def require_exact_mapping(value: object, name: str, keys: set[str]) -> dict:
    if not isinstance(value, dict):
        raise InvalidRequestError(f"{name} must be an object")
    actual = set(value)
    if actual != keys:
        missing = sorted(keys - actual)
        extra = sorted(actual - keys)
        raise InvalidRequestError(f"{name} has invalid fields: missing={missing}, extra={extra}")
    return value


def require_mapping(value: object, name: str) -> dict:
    if not isinstance(value, dict):
        raise InvalidRequestError(f"{name} must be an object")
    return value


def require_response(value: object) -> str:
    result = require_text(value, "artifact response", MAX_RESPONSE_BYTES, characters=Characters.LINES)
    return result


def require_positive_int(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise InvalidRequestError(f"{name} must be a positive integer")
    return value


def require_nonnegative_int(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise InvalidRequestError(f"{name} must be a nonnegative integer")
    return value


def freeze_json_value(value: object, name: str, depth: int, item_count: list[int]) -> object:
    if depth > MAX_METADATA_DEPTH:
        raise InvalidRequestError(f"{name} exceeds the metadata depth limit of {MAX_METADATA_DEPTH}")
    item_count[0] += 1
    if item_count[0] > MAX_METADATA_ITEMS:
        raise InvalidRequestError(f"{name} exceeds the metadata item limit of {MAX_METADATA_ITEMS}")
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        result = require_text(value, name, MAX_METADATA_STRING_BYTES, allow_empty=True)
        return result
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if not math_isfinite(value):
            raise InvalidRequestError(f"{name} must not contain a non-finite number")
        return value
    if isinstance(value, dict):
        validated_items = []
        for key, item in value.items():
            validated_key = require_text(key, f"{name} key", MAX_METADATA_KEY_BYTES, allow_empty=False)
            validated_items.append((validated_key, item))
        frozen = {}
        for validated_key, item in sorted(validated_items, key=lambda pair: pair[0]):
            frozen[validated_key] = freeze_json_value(item, f"{name}.{validated_key}", depth + 1, item_count)
        result = dict(frozen)
        return result
    if isinstance(value, (list, tuple)):
        result = tuple(freeze_json_value(item, f"{name}[{position}]", depth + 1, item_count) for position, item in enumerate(value))
        return result
    raise InvalidRequestError(f"{name} contains an unsupported JSON value")


def freeze_lifecycle_audit(value: object) -> dict:
    """Validate the reserved lifecycle audit a terminal transition writes into metadata."""
    fields = set(value) if isinstance(value, dict) else set()
    supersession = fields == LIFECYCLE_AUDIT_FIELDS | {"replacement_statement_id"}
    if fields != LIFECYCLE_AUDIT_FIELDS and not supersession:
        raise InvalidRequestError("artifact lifecycle_audit has invalid fields")
    try:
        operation = MutationOperation(value.get("operation", ""))
        reason = LifecycleMutationReason(value.get("reason", ""))
    except ValueError as err:
        raise InvalidRequestError("artifact lifecycle_audit operation or reason is unsupported") from err
    if supersession != (operation == MutationOperation.SUPERSEDE_RESPONSE):
        raise InvalidRequestError("artifact lifecycle_audit replacement_statement_id must match a supersession")
    result = {
        "operation": operation.value,
        "reason": reason.value,
        "caller_id": require_text(value.get("caller_id", ""), "lifecycle audit caller_id", MAX_CALLER_ID_BYTES),
        "request_id": require_text(value.get("request_id", ""), "lifecycle audit request_id", MAX_REQUEST_ID_BYTES),
        "occurred_at": require_utc_timestamp(value.get("occurred_at", ""), "lifecycle audit occurred_at"),
        "detail": require_text(
            value.get("detail", ""), "lifecycle audit detail", MAX_LIFECYCLE_AUDIT_DETAIL_BYTES, allow_empty=True
        ),
    }
    if supersession:
        result["replacement_statement_id"] = require_text(
            value.get("replacement_statement_id", ""), "lifecycle audit replacement_statement_id", MAX_ARTIFACT_ID_BYTES
        )
    result = dict(sorted(result.items()))
    return result


def freeze_metadata(value: object) -> dict:
    if not isinstance(value, dict):
        raise InvalidRequestError("artifact metadata must be an object")
    if "visibility_scope" in value:
        try:
            validate_support_visibility(value.get("visibility_scope", {}))
        except ValueError as error:
            raise InvalidRequestError(str(error)) from error
    # Caller metadata carries the item and byte limits; the reserved lifecycle audit is
    # bounded by its own fields, so writing it never pushes an admitted artifact over.
    caller_metadata = {key: item for key, item in value.items() if key != LIFECYCLE_AUDIT_KEY}
    frozen = freeze_json_value(caller_metadata, "artifact metadata", 0, [0])
    if not isinstance(frozen, dict):
        raise InvalidRequestError("artifact metadata must be an object")
    encoded = json_dumps(thaw_json_value(frozen), ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    if len(encoded.encode("utf-8")) > MAX_METADATA_BYTES:
        raise InvalidRequestError(f"artifact metadata exceeds the UTF-8 limit of {MAX_METADATA_BYTES} bytes")
    if LIFECYCLE_AUDIT_KEY in value:
        frozen[LIFECYCLE_AUDIT_KEY] = freeze_lifecycle_audit(value.get(LIFECYCLE_AUDIT_KEY, {}))
        frozen = dict(sorted(frozen.items()))
    return frozen


def thaw_json_value(value: object) -> object:
    if isinstance(value, dict):
        result = {key: thaw_json_value(item) for key, item in value.items()}
        return result
    if isinstance(value, tuple):
        result = [thaw_json_value(item) for item in value]
        return result
    return value


def json_text(value: dict) -> str:
    result = json_dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return result


def validate_artifact_provenance(value: object) -> dict:
    """Validate and copy bounded accepted-response provenance."""

    data = require_exact_mapping(value, "ArtifactProvenance", ARTIFACT_PROVENANCE_FIELDS)
    result: dict = {
        "source_label": require_text(
            data.get("source_label", ""),
            "artifact provenance source_label",
            MAX_SOURCE_LABEL_BYTES,
            allow_empty=True,
        ),
        "caller_id": require_text(
            data.get("caller_id", ""),
            "artifact provenance caller_id",
            MAX_CALLER_ID_BYTES,
            allow_empty=True,
        ),
        "accepted_at": require_utc_timestamp(
            data.get("accepted_at", ""),
            "artifact provenance accepted_at",
            allow_empty=False,
        ),
    }
    return result


def artifact_provenance_to_dict(value: object) -> dict:
    """Serialize bounded accepted-response provenance."""

    provenance = validate_artifact_provenance(value)
    result = dict(provenance)
    return result


def artifact_provenance_from_dict(value: object) -> dict:
    """Decode bounded accepted-response provenance."""

    result = validate_artifact_provenance(value)
    return result


def validate_artifact_statistics(value: object) -> dict:
    """Validate and copy authoritative response statistics."""

    data = require_exact_mapping(value, "ArtifactStatistics", ARTIFACT_STATISTICS_FIELDS)
    last_hit, last_hit_available = require_available_utc_timestamp(
        data.get("last_hit", ""),
        data.get("last_hit_available", False),
        "artifact statistics last_hit",
    )
    result: dict = {
        "hit_count": require_nonnegative_int(data.get("hit_count", 0), "artifact statistics hit_count"),
        "query_count": require_nonnegative_int(data.get("query_count", 0), "artifact statistics query_count"),
        "last_hit": last_hit,
        "last_hit_available": last_hit_available,
    }
    return result


def artifact_statistics_to_dict(value: object) -> dict:
    """Serialize authoritative response statistics."""

    statistics = validate_artifact_statistics(value)
    result = dict(statistics)
    return result


def artifact_statistics_from_dict(value: object) -> dict:
    """Decode authoritative response statistics."""

    result = validate_artifact_statistics(value)
    return result


def validate_cached_response_artifact(value: object) -> dict:
    """Validate and defensively copy one accepted-response artifact."""

    data = require_exact_mapping(value, "CachedResponseArtifact", CACHED_RESPONSE_ARTIFACT_FIELDS)
    statement_id = require_text(data.get("statement_id", ""), "artifact statement_id", MAX_ARTIFACT_ID_BYTES, allow_empty=False)
    generation = require_positive_int(data.get("generation", 0), "artifact generation")
    response = require_response(data.get("response", ""))
    try:
        query_identity = validate_query_identity(data.get("query_identity", {}))
    except IdentityValidationError as error:
        raise InvalidRequestError("artifact query_identity must be a QueryIdentity") from error
    try:
        retrieval = validate_retrieval_representation(data.get("retrieval", {}))
    except IdentityValidationError as error:
        raise InvalidRequestError("artifact retrieval must be a RetrievalRepresentation") from error
    validate_authoritative_identity(query_identity, retrieval)
    tier = data.get("tier", Tier.DYNAMIC)
    if not isinstance(tier, Tier):
        raise InvalidRequestError("artifact tier must be a Tier")
    lifecycle = require_lifecycle(data.get("lifecycle", LifecycleState.RETIRED), "artifact lifecycle")
    try:
        scope = validate_scope_key(data.get("scope", {}))
    except IdentityValidationError as error:
        raise InvalidRequestError("artifact scope must be a ScopeKey") from error
    if scope != query_identity.get("scope", {}):
        raise InvalidRequestError("artifact scope must match query_identity scope")
    raw_support = data.get("support_references", ())
    if not isinstance(raw_support, tuple):
        raise InvalidRequestError("artifact support_references must be a tuple")
    if len(raw_support) > MAX_SUPPORT_REFERENCES:
        raise InvalidRequestError(f"artifact support_references exceed the limit of {MAX_SUPPORT_REFERENCES}")
    try:
        support_references = validate_support_references(raw_support)
    except ValueError as error:
        raise InvalidRequestError(str(error)) from error
    valid_from, valid_from_available = require_available_utc_timestamp(
        data.get("valid_from", ""),
        data.get("valid_from_available", False),
        "artifact valid_from",
    )
    valid_until, valid_until_available = require_available_utc_timestamp(
        data.get("valid_until", ""),
        data.get("valid_until_available", False),
        "artifact valid_until",
    )
    superseded_by = require_text(data.get("superseded_by", ""), "artifact superseded_by", MAX_ARTIFACT_ID_BYTES, allow_empty=True)
    if lifecycle == LifecycleState.ACTIVE and superseded_by:
        raise InvalidRequestError("an ACTIVE artifact must not name superseded_by")
    if lifecycle == LifecycleState.SUPERSEDED and not superseded_by:
        raise InvalidRequestError("a SUPERSEDED artifact must name superseded_by")
    if superseded_by == statement_id:
        raise InvalidRequestError("artifact superseded_by must not reference itself")
    provenance = validate_artifact_provenance(data.get("provenance", {}))
    statistics = validate_artifact_statistics(data.get("statistics", {}))
    metadata = freeze_metadata(data.get("metadata", {}))
    result: dict = {
        "statement_id": statement_id,
        "generation": generation,
        "response": response,
        "query_identity": query_identity,
        "retrieval": retrieval,
        "tier": tier,
        "lifecycle": lifecycle,
        "scope": scope,
        "support_references": support_references,
        "valid_from": valid_from,
        "valid_from_available": valid_from_available,
        "valid_until": valid_until,
        "valid_until_available": valid_until_available,
        "superseded_by": superseded_by,
        "provenance": provenance,
        "statistics": statistics,
        "metadata": metadata,
    }
    return result


def cached_response_artifact_to_dict(value: object) -> dict:
    """Serialize one authoritative accepted-response artifact."""

    artifact = validate_cached_response_artifact(value)
    tier = artifact.get("tier", Tier.DYNAMIC)
    lifecycle = artifact.get("lifecycle", LifecycleState.RETIRED)
    result: dict = {
        "statement_id": artifact.get("statement_id", ""),
        "generation": artifact.get("generation", 0),
        "response": artifact.get("response", ""),
        "query_identity": query_identity_to_dict(artifact.get("query_identity", {})),
        "retrieval": retrieval_representation_to_dict(artifact.get("retrieval", {})),
        "tier": tier.value,
        "lifecycle": lifecycle.value,
        "scope": scope_key_to_dict(artifact.get("scope", {})),
        "support_references": [dict(reference) for reference in artifact.get("support_references", ())],
        "valid_from": artifact.get("valid_from", ""),
        "valid_from_available": artifact.get("valid_from_available", False),
        "valid_until": artifact.get("valid_until", ""),
        "valid_until_available": artifact.get("valid_until_available", False),
        "superseded_by": artifact.get("superseded_by", ""),
        "provenance": artifact_provenance_to_dict(artifact.get("provenance", {})),
        "statistics": artifact_statistics_to_dict(artifact.get("statistics", {})),
        "metadata": thaw_json_value(artifact.get("metadata", {})),
    }
    return result


def cached_response_artifact_from_dict(value: object) -> dict:
    """Decode one accepted-response artifact from its wire dictionary."""

    data = require_exact_mapping(value, "CachedResponseArtifact", CACHED_RESPONSE_ARTIFACT_FIELDS)
    tier_value = require_text(data.get("tier", ""), "artifact tier", MAX_ARTIFACT_ENUM_BYTES, allow_empty=False)
    lifecycle_value = require_text(data.get("lifecycle", ""), "artifact lifecycle", MAX_ARTIFACT_ENUM_BYTES, allow_empty=False)
    try:
        tier = Tier(tier_value)
    except ValueError as error:
        raise InvalidRequestError(f"unsupported artifact tier: {tier_value}") from error
    try:
        lifecycle = LifecycleState(lifecycle_value)
    except ValueError as error:
        raise InvalidRequestError(f"unsupported artifact lifecycle: {lifecycle_value}") from error
    raw_support = data.get("support_references", [])
    if not isinstance(raw_support, list):
        raise InvalidRequestError("artifact support_references must be an array")
    metadata = require_mapping(data.get("metadata", {}), "artifact metadata")
    query_identity = require_mapping(data.get("query_identity", {}), "artifact query_identity")
    retrieval = require_mapping(data.get("retrieval", {}), "artifact retrieval")
    scope = require_mapping(data.get("scope", {}), "artifact scope")
    provenance = require_mapping(data.get("provenance", {}), "artifact provenance")
    statistics = require_mapping(data.get("statistics", {}), "artifact statistics")
    raw_artifact = {
        "statement_id": require_text(
            data.get("statement_id", ""), "artifact statement_id", MAX_ARTIFACT_ID_BYTES, allow_empty=False
        ),
        "generation": require_positive_int(data.get("generation", 0), "artifact generation"),
        "response": require_response(data.get("response", "")),
        "query_identity": query_identity_from_dict(query_identity),
        "retrieval": retrieval_representation_from_dict(retrieval),
        "tier": tier,
        "lifecycle": lifecycle,
        "scope": scope_key_from_dict(scope),
        "support_references": tuple(raw_support),
        "valid_from": require_utc_timestamp(data.get("valid_from", ""), "artifact valid_from", allow_empty=True),
        "valid_from_available": require_bool(data.get("valid_from_available", False), "artifact valid_from_available"),
        "valid_until": require_utc_timestamp(data.get("valid_until", ""), "artifact valid_until", allow_empty=True),
        "valid_until_available": require_bool(data.get("valid_until_available", False), "artifact valid_until_available"),
        "superseded_by": require_text(
            data.get("superseded_by", ""), "artifact superseded_by", MAX_ARTIFACT_ID_BYTES, allow_empty=True
        ),
        "provenance": artifact_provenance_from_dict(provenance),
        "statistics": artifact_statistics_from_dict(statistics),
        "metadata": metadata,
    }
    result = validate_cached_response_artifact(raw_artifact)
    return result


def require_lifecycle(value: object, name: str) -> LifecycleState:
    if not isinstance(value, LifecycleState):
        raise InvalidRequestError(f"{name} must be a LifecycleState")
    return value


def require_operation(value: object) -> LifecycleOperation:
    if not isinstance(value, LifecycleOperation):
        raise InvalidRequestError("lifecycle operation must be a LifecycleOperation")
    return value


def require_lifecycle_decision_reason(value: object, name: str) -> LifecycleDecisionReason:
    if not isinstance(value, LifecycleDecisionReason):
        raise InvalidRequestError(f"{name} must be a LifecycleDecisionReason")
    return value


def validate_lifecycle_base_decision(value: object) -> dict:
    """Validate and copy one lifecycle-only eligibility decision."""

    data = require_exact_mapping(value, "LifecycleBaseDecision", LIFECYCLE_BASE_DECISION_FIELDS)
    lifecycle = require_lifecycle(data.get("lifecycle", LifecycleState.RETIRED), "lifecycle decision lifecycle")
    direct_answer_eligible = require_bool(data.get("direct_answer_eligible", False), "lifecycle decision direct_answer_eligible")
    reason = require_lifecycle_decision_reason(data.get("reason", LifecycleDecisionReason.RETIRED), "lifecycle decision reason")
    expected_eligible = lifecycle == LifecycleState.ACTIVE
    # Every non-ACTIVE state has a registered reason, so the default is unreachable.
    ineligible_reason = LIFECYCLE_INELIGIBLE_REASONS.get(lifecycle, LifecycleDecisionReason.RETIRED)
    expected_reason = LifecycleDecisionReason.ELIGIBLE if expected_eligible else ineligible_reason
    if direct_answer_eligible != expected_eligible or reason != expected_reason:
        raise InvalidRequestError("LifecycleBaseDecision fields do not match lifecycle policy")
    result: dict = {
        "lifecycle": lifecycle,
        "direct_answer_eligible": direct_answer_eligible,
        "reason": reason,
    }
    return result


def lifecycle_transition_outcome(
    current: LifecycleState,
    target: LifecycleState,
    operation: LifecycleOperation,
) -> tuple[bool, LifecycleDecisionReason]:
    if current == target:
        result = (False, LifecycleDecisionReason.SAME_STATE_NOT_A_TRANSITION)
    else:
        # Terminal states map to no operations; every state is registered.
        transitions = LEGAL_LIFECYCLE_TRANSITIONS.get(current, {})
        if operation not in transitions:
            result = (False, LifecycleDecisionReason.TERMINAL_STATE)
        elif transitions.get(operation, current) != target:
            result = (False, LifecycleDecisionReason.OPERATION_TARGET_MISMATCH)
        else:
            result = (True, LifecycleDecisionReason.LEGAL_TRANSITION)
    return result


def validate_lifecycle_transition_decision(value: object) -> dict:
    """Validate and copy one lifecycle transition decision."""

    data = require_exact_mapping(value, "LifecycleTransitionDecision", LIFECYCLE_TRANSITION_DECISION_FIELDS)
    current = require_lifecycle(data.get("current", LifecycleState.RETIRED), "current lifecycle")
    target = require_lifecycle(data.get("target", LifecycleState.RETIRED), "target lifecycle")
    operation = require_operation(data.get("operation", LifecycleOperation.RETIRE))
    allowed = require_bool(data.get("allowed", False), "lifecycle transition allowed")
    reason = require_lifecycle_decision_reason(
        data.get("reason", LifecycleDecisionReason.TERMINAL_STATE), "lifecycle transition reason"
    )
    expected_allowed, expected_reason = lifecycle_transition_outcome(current, target, operation)
    if allowed != expected_allowed or reason != expected_reason:
        raise InvalidRequestError("LifecycleTransitionDecision fields do not match transition policy")
    result: dict = {
        "current": current,
        "target": target,
        "operation": operation,
        "allowed": allowed,
        "reason": reason,
    }
    return result


def lifecycle_base_eligibility(lifecycle: object) -> dict:
    """Evaluate lifecycle alone; temporal checks are applied later."""

    state = require_lifecycle(lifecycle, "lifecycle")
    if state == LifecycleState.ACTIVE:
        raw_decision = {
            "lifecycle": state,
            "direct_answer_eligible": True,
            "reason": LifecycleDecisionReason.ELIGIBLE,
        }
    else:
        raw_decision = {
            "lifecycle": state,
            "direct_answer_eligible": False,
            "reason": LIFECYCLE_INELIGIBLE_REASONS.get(state, LifecycleDecisionReason.RETIRED),
        }
    decision = validate_lifecycle_base_decision(raw_decision)
    return decision


def lifecycle_transition_decision(
    current: object,
    target: object,
    operation: object,
) -> dict:
    """Return the legal-transition decision without mutating state."""

    current_state = require_lifecycle(current, "current lifecycle")
    target_state = require_lifecycle(target, "target lifecycle")
    named_operation = require_operation(operation)
    allowed, reason = lifecycle_transition_outcome(current_state, target_state, named_operation)
    raw_decision = {
        "current": current_state,
        "target": target_state,
        "operation": named_operation,
        "allowed": allowed,
        "reason": reason,
    }
    decision = validate_lifecycle_transition_decision(raw_decision)
    return decision


def require_lifecycle_transition(
    current: LifecycleState,
    target: LifecycleState,
    operation: LifecycleOperation,
) -> dict:
    """Return a legal decision or raise a stable lifecycle error."""

    decision = lifecycle_transition_decision(current, target, operation)
    if not decision.get("allowed", False):
        current_state = decision.get("current", LifecycleState.RETIRED)
        target_state = decision.get("target", LifecycleState.RETIRED)
        named_operation = decision.get("operation", LifecycleOperation.RETIRE)
        reason = decision.get("reason", LifecycleDecisionReason.TERMINAL_STATE)
        raise LifecycleError(
            f"illegal lifecycle transition: {current_state.value} -> {target_state.value} "
            f"via {named_operation.value} ({reason.value})"
        )
    return decision


def lifecycle_after_capacity_eviction(lifecycle: LifecycleState) -> LifecycleState:
    """Confirm that capacity eviction cannot perform a lifecycle transition."""

    state = require_lifecycle(lifecycle, "lifecycle")
    return state
