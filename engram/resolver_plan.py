"""Resolver operation contract, bounded resolver leases and reservations, and the registry's deterministic plan."""

from logging import getLogger as logging_getLogger

from engram.constants_resolution import (
    MAX_PLAN_RESOLVERS,
    MAX_REASON_CODE_BYTES,
    MAX_RESOLVER_NAME_BYTES,
    MAX_RESOURCE_COUNTER,
    RESOLUTION_PLAN_ENTRY_FIELDS,
    RESOLUTION_PLAN_FIELDS,
    RESOLVER_BUDGET_FIELDS,
    RESOLVER_RESERVATION_FIELDS,
    CostClass,
)
from engram.errors import (
    InvalidRequestError,
)
from engram.resolution_budget import (
    budget_consumption_from_dict,
    budget_consumption_to_dict,
    validate_budget_consumption,
)
from engram.resolution_frame import validate_query_frame

# Registry availability failures keep logging on the resolvers channel.
logger = logging_getLogger("engram.resolvers")


def resolver_contract(value: object) -> tuple[str, CostClass, object, object]:
    """Validate and expose the operations of one stateful resolver object."""

    name = getattr(value, "name", ())
    cost_class = getattr(value, "cost_class", ())
    available_operation = getattr(value, "available", ())
    resolve_operation = getattr(value, "resolve", ())
    if not isinstance(name, str) or not name or len(name.encode("utf-8")) > MAX_RESOLVER_NAME_BYTES:
        raise InvalidRequestError("resolver must have a bounded non-empty name")
    if not isinstance(cost_class, CostClass) or not callable(available_operation) or not callable(resolve_operation):
        raise InvalidRequestError("resolver must implement resolver operations")
    result = (name, cost_class, available_operation, resolve_operation)
    return result


def run_cooperative_check(check: object) -> bool:
    """Run one validated transient cancellation check."""
    if check == ():
        return False
    if not callable(check):
        raise InvalidRequestError("cooperative_check must be callable")
    check()
    return True


def resolver_clock_ns(clock: object) -> int:
    """Read one injected monotonic clock and reject an invalid result."""
    if not callable(clock):
        raise InvalidRequestError("resolver clock_ns must be callable")
    value = clock()
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise InvalidRequestError("resolver clock_ns must return a nonnegative integer")
    return value


def resolver_available(operation: object, frame: dict) -> bool:
    """Invoke one validated availability operation."""
    if not callable(operation):
        raise InvalidRequestError("resolver availability operation must be callable")
    result = bool(operation(frame))
    return result


def execute_resolver(operation: object, frame: dict, lease: dict, cooperative_check: object) -> object:
    """Invoke one validated resolver operation."""
    if not callable(operation):
        raise InvalidRequestError("resolver operation must be callable")
    result = operation(frame, lease, cooperative_check)
    return result


def resolver_budget(
    max_candidates: object,
    max_graph_rows: object,
    max_vector_results: object,
    max_evidence: object,
    max_evidence_bytes: object,
    max_output_bytes: object,
    max_diagnostic_bytes: object,
    max_working_memory_bytes: object,
) -> dict:
    """Build one read-only bounded lease passed to a resolver."""
    raw_values = {
        "max_candidates": max_candidates,
        "max_graph_rows": max_graph_rows,
        "max_vector_results": max_vector_results,
        "max_evidence": max_evidence,
        "max_evidence_bytes": max_evidence_bytes,
        "max_output_bytes": max_output_bytes,
        "max_diagnostic_bytes": max_diagnostic_bytes,
        "max_working_memory_bytes": max_working_memory_bytes,
    }
    values = {}
    for name, raw in raw_values.items():
        if isinstance(raw, bool) or not isinstance(raw, int) or not 0 <= raw <= MAX_RESOURCE_COUNTER:
            raise InvalidRequestError(f"resolver budget {name} must be a nonnegative integer")
        values[name] = raw
    result: dict = {
        "max_candidates": values.get("max_candidates", 0),
        "max_graph_rows": values.get("max_graph_rows", 0),
        "max_vector_results": values.get("max_vector_results", 0),
        "max_evidence": values.get("max_evidence", 0),
        "max_evidence_bytes": values.get("max_evidence_bytes", 0),
        "max_output_bytes": values.get("max_output_bytes", 0),
        "max_diagnostic_bytes": values.get("max_diagnostic_bytes", 0),
        "max_working_memory_bytes": values.get("max_working_memory_bytes", 0),
    }
    return result


def validate_resolver_budget(value: object) -> dict:
    if not isinstance(value, dict) or set(value) != RESOLVER_BUDGET_FIELDS:
        raise InvalidRequestError("ResolverBudget has invalid fields")
    result = resolver_budget(
        max_candidates=value.get("max_candidates", 0),
        max_graph_rows=value.get("max_graph_rows", 0),
        max_vector_results=value.get("max_vector_results", 0),
        max_evidence=value.get("max_evidence", 0),
        max_evidence_bytes=value.get("max_evidence_bytes", 0),
        max_output_bytes=value.get("max_output_bytes", 0),
        max_diagnostic_bytes=value.get("max_diagnostic_bytes", 0),
        max_working_memory_bytes=value.get("max_working_memory_bytes", 0),
    )
    return result


def resolver_budget_with_changes(value: object, changes: object) -> dict:
    current = validate_resolver_budget(value)
    if not isinstance(changes, dict) or not set(changes).issubset(RESOLVER_BUDGET_FIELDS):
        raise InvalidRequestError("resolver budget changes contain invalid fields")
    updated: dict[str, object] = dict(current)
    updated.update(changes)
    result = validate_resolver_budget(updated)
    return result


def resolver_budget_to_dict(value: object) -> dict[str, object]:
    current = validate_resolver_budget(value)
    result = dict(current)
    return result


def resolver_budget_from_dict(value: object) -> dict:
    if not isinstance(value, dict):
        raise InvalidRequestError("ResolverBudget must be an object")
    result = validate_resolver_budget(value)
    return result


def resolver_reservation(
    resolver: object,
    order: object,
    lease: object,
    consumption: object,
) -> dict:
    """Build one inspectable lease and resulting consumption."""
    if not isinstance(resolver, str) or not resolver or len(resolver.encode("utf-8")) > MAX_RESOLVER_NAME_BYTES:
        raise InvalidRequestError("reservation resolver must be a bounded non-empty string")
    if isinstance(order, bool) or not isinstance(order, int) or not 0 <= order < MAX_PLAN_RESOLVERS:
        raise InvalidRequestError("reservation order is out of bounds")
    try:
        validated_lease = validate_resolver_budget(lease)
        validated_consumption = validate_budget_consumption(consumption)
    except InvalidRequestError as err:
        raise InvalidRequestError("reservation lease and consumption have invalid types") from err
    result: dict = {
        "resolver": resolver,
        "order": order,
        "lease": validated_lease,
        "consumption": validated_consumption,
    }
    return result


def validate_resolver_reservation(value: object) -> dict:
    if not isinstance(value, dict) or set(value) != RESOLVER_RESERVATION_FIELDS:
        raise InvalidRequestError("ResolverReservation has invalid fields")
    result = resolver_reservation(
        value.get("resolver", ""),
        value.get("order", 0),
        value.get("lease", {}),
        value.get("consumption", {}),
    )
    return result


def resolver_reservation_to_dict(value: object) -> dict[str, object]:
    current = validate_resolver_reservation(value)
    result = {
        "resolver": current.get("resolver", ""),
        "order": current.get("order", 0),
        "lease": resolver_budget_to_dict(current.get("lease", {})),
        "consumption": budget_consumption_to_dict(current.get("consumption", {})),
    }
    return result


def resolver_reservation_from_dict(value: object) -> dict:
    if not isinstance(value, dict) or set(value) != RESOLVER_RESERVATION_FIELDS:
        raise InvalidRequestError("ResolverReservation has invalid fields")
    lease = value.get("lease", False)
    consumption = value.get("consumption", False)
    if not isinstance(lease, dict) or not isinstance(consumption, dict):
        raise InvalidRequestError("ResolverReservation nested records must be objects")
    result = resolver_reservation(
        value.get("resolver", ""),
        value.get("order", 0),
        resolver_budget_from_dict(lease),
        budget_consumption_from_dict(consumption),
    )
    return result


def resolution_plan_entry(
    resolver: object,
    order: object,
    configured: object,
    available: object,
    reason_code: object,
) -> dict:
    """Build one inspectable configured resolver decision."""
    resolver_contract(resolver)
    if isinstance(order, bool) or not isinstance(order, int) or not 0 <= order < MAX_PLAN_RESOLVERS:
        raise InvalidRequestError("plan order is out of bounds")
    if not isinstance(configured, bool) or not isinstance(available, bool):
        raise InvalidRequestError("plan configured and available must be booleans")
    if not isinstance(reason_code, str) or len(reason_code.encode("utf-8")) > MAX_REASON_CODE_BYTES:
        raise InvalidRequestError("plan reason_code must be a bounded string")
    result: dict = {
        "resolver": resolver,
        "order": order,
        "configured": configured,
        "available": available,
        "reason_code": reason_code,
    }
    return result


def validate_resolution_plan_entry(value: object) -> dict:
    if not isinstance(value, dict) or set(value) != RESOLUTION_PLAN_ENTRY_FIELDS:
        raise InvalidRequestError("ResolutionPlanEntry has invalid fields")
    result = resolution_plan_entry(
        value.get("resolver", ()),
        value.get("order", 0),
        value.get("configured", False),
        value.get("available", False),
        value.get("reason_code", ""),
    )
    return result


def trusted_resolution_plan_entry_to_dict(current: dict) -> dict[str, object]:
    """Serialize an entry already produced by this module."""
    resolver = current.get("resolver", {})
    name, cost_class, _, _ = resolver_contract(resolver)
    result = {
        "resolver": name,
        "cost_class": cost_class.value,
        "order": current.get("order", 0),
        "configured": current.get("configured", False),
        "available": current.get("available", False),
        "reason_code": current.get("reason_code", ""),
    }
    return result


def resolution_plan(entries: object) -> dict:
    """Build a deterministic plan retaining configured skip and availability decisions."""
    if not isinstance(entries, tuple):
        raise InvalidRequestError("plan entries must be a tuple")
    if len(entries) > MAX_PLAN_RESOLVERS:
        raise InvalidRequestError(f"plan entries exceed the limit of {MAX_PLAN_RESOLVERS}")
    validated_entries = tuple(validate_resolution_plan_entry(value) for value in entries)
    if tuple(value.get("order", 0) for value in validated_entries) != tuple(range(len(validated_entries))):
        raise InvalidRequestError("plan entry order must be contiguous")
    names = []
    for entry in validated_entries:
        name, _, _, _ = resolver_contract(entry.get("resolver", ()))
        names.append(name)
    if len(set(names)) != len(names):
        raise InvalidRequestError("plan resolver names must be unique")
    result: dict = {"entries": validated_entries}
    return result


def validate_resolution_plan(value: object) -> dict:
    if not isinstance(value, dict) or set(value) != RESOLUTION_PLAN_FIELDS:
        raise InvalidRequestError("ResolutionPlan has invalid fields")
    result = resolution_plan(value.get("entries", ()))
    return result


def trusted_resolution_plan_to_dict(current: dict) -> dict[str, object]:
    """Serialize a plan already produced or validated by this module."""
    entries = [trusted_resolution_plan_entry_to_dict(entry) for entry in current.get("entries", ())]
    result: dict[str, object] = {"entries": entries}
    return result


class ResolverRegistry:
    """Allow-listed deterministic resolver registry."""

    def __init__(self, resolvers: tuple[object, ...]) -> None:
        if not isinstance(resolvers, tuple):
            raise InvalidRequestError("resolvers must be a tuple")
        if len(resolvers) > MAX_PLAN_RESOLVERS:
            raise InvalidRequestError(f"resolvers exceed the limit of {MAX_PLAN_RESOLVERS}")
        names = []
        for resolver in resolvers:
            name, _, _, _ = resolver_contract(resolver)
            names.append(name)
        if len(set(names)) != len(names):
            raise InvalidRequestError("resolver names must be unique")
        self.internal_resolvers = resolvers

    def plan(self, frame: dict, configured_names: tuple[str, ...] = ()) -> dict:
        frame = validate_query_frame(frame)
        if not isinstance(configured_names, tuple):
            raise InvalidRequestError("configured_names must be a tuple")
        if not all(isinstance(name, str) and name for name in configured_names):
            raise InvalidRequestError("configured_names must contain non-empty strings")
        if len(set(configured_names)) != len(configured_names):
            raise InvalidRequestError("configured_names must not contain duplicates")
        resolver_names = {resolver_contract(resolver)[0] for resolver in self.internal_resolvers}
        configured = set(configured_names) if configured_names else resolver_names
        unknown = configured.difference(resolver_names)
        if unknown:
            raise InvalidRequestError(f"unknown configured resolvers: {sorted(unknown)}")
        entries = []
        for order, resolver in enumerate(self.internal_resolvers):
            name, cost_class, available_operation, _ = resolver_contract(resolver)
            selected = name in configured
            cost_allowed = cost_class in frame.get("budget", {}).get("allowed_cost_classes", ())
            availability_failed = False
            try:
                available = bool(selected and cost_allowed and resolver_available(available_operation, frame))
            except Exception as err:
                logger.warning("Resolver %s availability check failed", name, exc_info=err)
                available = False
                availability_failed = True
            reason = ""
            if not selected:
                reason = "not_configured"
            elif not cost_allowed:
                reason = "cost_class_disabled"
            elif availability_failed:
                reason = "availability_check_failed"
            elif not available:
                reason = "dependency_unavailable"
            entries.append(resolution_plan_entry(resolver, order, selected, available, reason))
        # The registry owns every entry above: ordering comes from enumerate,
        # and resolver uniqueness was established during construction.
        result: dict = {"entries": tuple(entries)}
        return result
