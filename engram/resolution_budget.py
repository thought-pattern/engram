"""Resolution resource limits, consumption contracts, and lock-owned accounting.

This module owns no request execution or evidence state. It depends on native
value validation below the frame, result, and resolver orchestration layers.
"""

from threading import RLock as threading_RLock

from engram.constants import (
    BUDGET_CONSUMPTION_FIELDS,
    DEFAULT_RESOLUTION_ALLOWED_COST_CLASSES,
    DEFAULT_RESOLUTION_MAX_CANDIDATES,
    DEFAULT_RESOLUTION_MAX_DIAGNOSTIC_BYTES,
    DEFAULT_RESOLUTION_MAX_EVIDENCE,
    DEFAULT_RESOLUTION_MAX_EVIDENCE_BYTES,
    DEFAULT_RESOLUTION_MAX_GRAPH_ROWS,
    DEFAULT_RESOLUTION_MAX_OUTPUT_BYTES,
    DEFAULT_RESOLUTION_MAX_RESOLVERS,
    DEFAULT_RESOLUTION_MAX_VECTOR_RESULTS,
    DEFAULT_RESOLUTION_MAX_WORKING_MEMORY_BYTES,
    MAX_ENUM_WIRE_BYTES,
    MAX_EXHAUSTED_DIMENSION_BYTES,
    MAX_EXHAUSTED_DIMENSIONS,
    MAX_RESOLUTION_CANDIDATES,
    MAX_RESOLUTION_DIAGNOSTIC_BYTES,
    MAX_RESOLUTION_EVIDENCE,
    MAX_RESOLUTION_EVIDENCE_BYTES,
    MAX_RESOLUTION_GRAPH_ROWS,
    MAX_RESOLUTION_OUTPUT_BYTES,
    MAX_RESOLUTION_RESOLVERS,
    MAX_RESOLUTION_VECTOR_RESULTS,
    MAX_RESOLUTION_WORKING_MEMORY_BYTES,
    MAX_RESOURCE_COUNTER,
    MIN_RESOLUTION_CANDIDATES,
    MIN_RESOLUTION_OUTPUT_BYTES,
    MIN_RESOLUTION_RESOLVERS,
    RESOLUTION_BUDGET_FIELDS,
    CostClass,
)
from engram.errors import InvalidRequestError
from engram.resolution_values import enum_tuple, exact_mapping, require_int, require_list
from engram.validation import require_any_text


def resolution_budget(
    max_resolvers: object = DEFAULT_RESOLUTION_MAX_RESOLVERS,
    max_candidates: object = DEFAULT_RESOLUTION_MAX_CANDIDATES,
    max_graph_rows: object = DEFAULT_RESOLUTION_MAX_GRAPH_ROWS,
    max_vector_results: object = DEFAULT_RESOLUTION_MAX_VECTOR_RESULTS,
    max_evidence: object = DEFAULT_RESOLUTION_MAX_EVIDENCE,
    max_evidence_bytes: object = DEFAULT_RESOLUTION_MAX_EVIDENCE_BYTES,
    max_output_bytes: object = DEFAULT_RESOLUTION_MAX_OUTPUT_BYTES,
    max_diagnostic_bytes: object = DEFAULT_RESOLUTION_MAX_DIAGNOSTIC_BYTES,
    max_working_memory_bytes: object = DEFAULT_RESOLUTION_MAX_WORKING_MEMORY_BYTES,
    allowed_cost_classes: object = DEFAULT_RESOLUTION_ALLOWED_COST_CLASSES,
    started_ns: object = 0,
) -> dict:
    """Build immutable non-time resource limits and a measurement start."""
    normalized_costs = enum_tuple(allowed_cost_classes, CostClass, "allowed_cost_classes", len(CostClass))
    if not normalized_costs:
        raise InvalidRequestError("allowed_cost_classes must not be empty")
    normalized_started = require_int(started_ns, "started_ns", 0, MAX_RESOURCE_COUNTER)
    result: dict = {
        "max_resolvers": require_int(max_resolvers, "max_resolvers", MIN_RESOLUTION_RESOLVERS, MAX_RESOLUTION_RESOLVERS),
        "max_candidates": require_int(
            max_candidates,
            "max_candidates",
            MIN_RESOLUTION_CANDIDATES,
            MAX_RESOLUTION_CANDIDATES,
        ),
        "max_graph_rows": require_int(max_graph_rows, "max_graph_rows", 0, MAX_RESOLUTION_GRAPH_ROWS),
        "max_vector_results": require_int(max_vector_results, "max_vector_results", 0, MAX_RESOLUTION_VECTOR_RESULTS),
        "max_evidence": require_int(max_evidence, "max_evidence", 0, MAX_RESOLUTION_EVIDENCE),
        "max_evidence_bytes": require_int(max_evidence_bytes, "max_evidence_bytes", 0, MAX_RESOLUTION_EVIDENCE_BYTES),
        "max_output_bytes": require_int(
            max_output_bytes,
            "max_output_bytes",
            MIN_RESOLUTION_OUTPUT_BYTES,
            MAX_RESOLUTION_OUTPUT_BYTES,
        ),
        "max_diagnostic_bytes": require_int(
            max_diagnostic_bytes,
            "max_diagnostic_bytes",
            0,
            MAX_RESOLUTION_DIAGNOSTIC_BYTES,
        ),
        "max_working_memory_bytes": require_int(
            max_working_memory_bytes,
            "max_working_memory_bytes",
            1,
            MAX_RESOLUTION_WORKING_MEMORY_BYTES,
        ),
        "allowed_cost_classes": normalized_costs,
        "started_ns": normalized_started,
    }
    return result


def validate_resolution_budget(value: object) -> dict:
    data = exact_mapping(value, "ResolutionBudget", RESOLUTION_BUDGET_FIELDS)
    result = resolution_budget(
        max_resolvers=data.get("max_resolvers", 0),
        max_candidates=data.get("max_candidates", 0),
        max_graph_rows=data.get("max_graph_rows", 0),
        max_vector_results=data.get("max_vector_results", 0),
        max_evidence=data.get("max_evidence", 0),
        max_evidence_bytes=data.get("max_evidence_bytes", 0),
        max_output_bytes=data.get("max_output_bytes", 0),
        max_diagnostic_bytes=data.get("max_diagnostic_bytes", 0),
        max_working_memory_bytes=data.get("max_working_memory_bytes", 0),
        allowed_cost_classes=data.get("allowed_cost_classes", ()),
        started_ns=data.get("started_ns", 0),
    )
    return result


def capture_resolution_budget(clock_ns: object, **limits: object) -> dict:
    if not callable(clock_ns):
        raise InvalidRequestError("budget clock_ns must be callable")
    if not set(limits).issubset(RESOLUTION_BUDGET_FIELDS):
        raise InvalidRequestError("resolution budget limits contain an unknown field")
    started = clock_ns()
    if isinstance(started, bool) or not isinstance(started, int) or started < 1:
        raise InvalidRequestError("budget clock_ns must return a positive integer")
    values = dict(limits)
    values["started_ns"] = started
    result = resolution_budget(**values)
    return result


def recapture_resolution_budget(value: object, clock_ns: object) -> dict:
    """Capture a fresh measurement start from immutable resource limits."""
    budget = validate_resolution_budget(value)
    result = capture_resolution_budget(
        clock_ns,
        max_resolvers=budget.get("max_resolvers", 0),
        max_candidates=budget.get("max_candidates", 0),
        max_graph_rows=budget.get("max_graph_rows", 0),
        max_vector_results=budget.get("max_vector_results", 0),
        max_evidence=budget.get("max_evidence", 0),
        max_evidence_bytes=budget.get("max_evidence_bytes", 0),
        max_output_bytes=budget.get("max_output_bytes", 0),
        max_diagnostic_bytes=budget.get("max_diagnostic_bytes", 0),
        max_working_memory_bytes=budget.get("max_working_memory_bytes", 0),
        allowed_cost_classes=budget.get("allowed_cost_classes", ()),
    )
    return result


def resolution_budget_to_dict(value: object) -> dict[str, object]:
    budget = validate_resolution_budget(value)
    result = {
        "max_resolvers": budget.get("max_resolvers", 0),
        "max_candidates": budget.get("max_candidates", 0),
        "max_graph_rows": budget.get("max_graph_rows", 0),
        "max_vector_results": budget.get("max_vector_results", 0),
        "max_evidence": budget.get("max_evidence", 0),
        "max_evidence_bytes": budget.get("max_evidence_bytes", 0),
        "max_output_bytes": budget.get("max_output_bytes", 0),
        "max_diagnostic_bytes": budget.get("max_diagnostic_bytes", 0),
        "max_working_memory_bytes": budget.get("max_working_memory_bytes", 0),
        "allowed_cost_classes": [cost.value for cost in budget.get("allowed_cost_classes", ())],
        "started_ns": budget.get("started_ns", 0),
    }
    return result


def resolution_budget_from_dict(value: object) -> dict:
    data = exact_mapping(value, "ResolutionBudget", RESOLUTION_BUDGET_FIELDS)
    raw_costs = require_list(data.get("allowed_cost_classes", []), "allowed_cost_classes")
    try:
        costs = tuple(CostClass(require_any_text(item, "cost class", MAX_ENUM_WIRE_BYTES, allow_empty=False)) for item in raw_costs)
    except ValueError as err:
        raise InvalidRequestError("allowed_cost_classes contains an unsupported value") from err
    result = resolution_budget(
        max_resolvers=data.get("max_resolvers", 0),
        max_candidates=data.get("max_candidates", 0),
        max_graph_rows=data.get("max_graph_rows", 0),
        max_vector_results=data.get("max_vector_results", 0),
        max_evidence=data.get("max_evidence", 0),
        max_evidence_bytes=data.get("max_evidence_bytes", 0),
        max_output_bytes=data.get("max_output_bytes", 0),
        max_diagnostic_bytes=data.get("max_diagnostic_bytes", 0),
        max_working_memory_bytes=data.get("max_working_memory_bytes", 0),
        allowed_cost_classes=costs,
        started_ns=data.get("started_ns", 0),
    )
    return result


def budget_consumption(
    elapsed_ns: object = 0,
    resolvers: object = 0,
    candidates: object = 0,
    graph_rows: object = 0,
    vector_results: object = 0,
    evidence: object = 0,
    evidence_bytes: object = 0,
    output_bytes: object = 0,
    diagnostic_bytes: object = 0,
    working_memory_bytes: object = 0,
    exhausted_dimensions: object = (),
    measurement_available: object = True,
) -> dict:
    """Build concrete resource use for a resolver or complete resolution."""
    numeric_values = {
        "elapsed_ns": elapsed_ns,
        "resolvers": resolvers,
        "candidates": candidates,
        "graph_rows": graph_rows,
        "vector_results": vector_results,
        "evidence": evidence,
        "evidence_bytes": evidence_bytes,
        "output_bytes": output_bytes,
        "diagnostic_bytes": diagnostic_bytes,
        "working_memory_bytes": working_memory_bytes,
    }
    normalized_numbers = {
        name: require_int(field_value, name, 0, MAX_RESOURCE_COUNTER) for name, field_value in numeric_values.items()
    }
    if not isinstance(exhausted_dimensions, tuple):
        raise InvalidRequestError("exhausted_dimensions must be a tuple")
    if len(exhausted_dimensions) > MAX_EXHAUSTED_DIMENSIONS:
        raise InvalidRequestError(f"exhausted_dimensions exceeds the limit of {MAX_EXHAUSTED_DIMENSIONS}")
    normalized_exhausted = tuple(
        require_any_text(value, "exhausted dimension", MAX_EXHAUSTED_DIMENSION_BYTES, allow_empty=False)
        for value in exhausted_dimensions
    )
    if normalized_exhausted != tuple(sorted(set(normalized_exhausted))):
        raise InvalidRequestError("exhausted_dimensions must be unique and sorted")
    if not isinstance(measurement_available, bool):
        raise InvalidRequestError("measurement_available must be a boolean")
    result: dict = {
        "elapsed_ns": normalized_numbers.get("elapsed_ns", 0),
        "resolvers": normalized_numbers.get("resolvers", 0),
        "candidates": normalized_numbers.get("candidates", 0),
        "graph_rows": normalized_numbers.get("graph_rows", 0),
        "vector_results": normalized_numbers.get("vector_results", 0),
        "evidence": normalized_numbers.get("evidence", 0),
        "evidence_bytes": normalized_numbers.get("evidence_bytes", 0),
        "output_bytes": normalized_numbers.get("output_bytes", 0),
        "diagnostic_bytes": normalized_numbers.get("diagnostic_bytes", 0),
        "working_memory_bytes": normalized_numbers.get("working_memory_bytes", 0),
        "exhausted_dimensions": normalized_exhausted,
        "measurement_available": measurement_available,
    }
    return result


def validate_budget_consumption(value: object) -> dict:
    data = exact_mapping(value, "BudgetConsumption", BUDGET_CONSUMPTION_FIELDS)
    result = budget_consumption(
        elapsed_ns=data.get("elapsed_ns", 0),
        resolvers=data.get("resolvers", 0),
        candidates=data.get("candidates", 0),
        graph_rows=data.get("graph_rows", 0),
        vector_results=data.get("vector_results", 0),
        evidence=data.get("evidence", 0),
        evidence_bytes=data.get("evidence_bytes", 0),
        output_bytes=data.get("output_bytes", 0),
        diagnostic_bytes=data.get("diagnostic_bytes", 0),
        working_memory_bytes=data.get("working_memory_bytes", 0),
        exhausted_dimensions=data.get("exhausted_dimensions", ()),
        measurement_available=data.get("measurement_available", False),
    )
    return result


def budget_consumption_with_changes(value: object, changes: object) -> dict:
    consumption = validate_budget_consumption(value)
    if not isinstance(changes, dict):
        raise InvalidRequestError("budget consumption changes must be an object")
    if not set(changes).issubset(BUDGET_CONSUMPTION_FIELDS):
        raise InvalidRequestError("budget consumption changes contain an unknown field")
    updated: dict[str, object] = dict(consumption)
    updated.update(changes)
    result = validate_budget_consumption(updated)
    return result


def trusted_budget_consumption_with_changes(
    value: dict,
    changes: dict[str, object],
) -> dict:
    """Copy executor-owned consumption and apply already bounded observations."""
    updated: dict[str, object] = dict(value)
    updated.update(changes)
    result = updated
    return result


def budget_consumption_to_dict(value: object) -> dict[str, object]:
    consumption = validate_budget_consumption(value)
    result = {
        "elapsed_ns": consumption.get("elapsed_ns", 0),
        "resolvers": consumption.get("resolvers", 0),
        "candidates": consumption.get("candidates", 0),
        "graph_rows": consumption.get("graph_rows", 0),
        "vector_results": consumption.get("vector_results", 0),
        "evidence": consumption.get("evidence", 0),
        "evidence_bytes": consumption.get("evidence_bytes", 0),
        "output_bytes": consumption.get("output_bytes", 0),
        "diagnostic_bytes": consumption.get("diagnostic_bytes", 0),
        "working_memory_bytes": consumption.get("working_memory_bytes", 0),
        "exhausted_dimensions": list(consumption.get("exhausted_dimensions", ())),
        "measurement_available": consumption.get("measurement_available", False),
    }
    return result


def budget_consumption_from_dict(value: object) -> dict:
    data = exact_mapping(value, "BudgetConsumption", BUDGET_CONSUMPTION_FIELDS)
    exhausted = require_list(data.get("exhausted_dimensions", []), "exhausted_dimensions")
    result = budget_consumption(
        elapsed_ns=data.get("elapsed_ns", 0),
        resolvers=data.get("resolvers", 0),
        candidates=data.get("candidates", 0),
        graph_rows=data.get("graph_rows", 0),
        vector_results=data.get("vector_results", 0),
        evidence=data.get("evidence", 0),
        evidence_bytes=data.get("evidence_bytes", 0),
        output_bytes=data.get("output_bytes", 0),
        diagnostic_bytes=data.get("diagnostic_bytes", 0),
        working_memory_bytes=data.get("working_memory_bytes", 0),
        exhausted_dimensions=tuple(exhausted),
        measurement_available=data.get("measurement_available", False),
    )
    return result


class BudgetLedger:
    """Thread-safe deterministic aggregate consumption and allowance checker."""

    def __init__(self, budget: dict) -> None:
        try:
            validated_budget = validate_resolution_budget(budget)
        except InvalidRequestError as err:
            raise InvalidRequestError("ledger budget must be a ResolutionBudget") from err
        self.internal_budget = validated_budget
        self.internal_lock = threading_RLock()
        self.totals = budget_consumption()

    @property
    def budget(self) -> dict:
        result = validate_resolution_budget(self.internal_budget)
        return result

    def remaining_candidates(self) -> int:
        with self.internal_lock:
            result = max(0, self.internal_budget.get("max_candidates", 0) - self.totals.get("candidates", 0))
            return result

    def remaining_evidence(self) -> int:
        with self.internal_lock:
            result = max(0, self.internal_budget.get("max_evidence", 0) - self.totals.get("evidence", 0))
            return result

    def add(self, consumption: dict) -> dict:
        """Add one BudgetConsumption to the running totals and return a copy of the new totals.

        Each counted dimension is summed; a dimension whose total passes its
        budget limit is marked exhausted, and exhausted dimensions accumulate.
        The totals stay measured only while every added consumption was measured.

        Raises:
            InvalidRequestError: ``consumption`` is not a BudgetConsumption.
        """
        try:
            validated_consumption = validate_budget_consumption(consumption)
        except InvalidRequestError as err:
            raise InvalidRequestError("ledger consumption must be a BudgetConsumption") from err
        with self.internal_lock:
            values = {}
            for name in (
                "elapsed_ns",
                "resolvers",
                "candidates",
                "graph_rows",
                "vector_results",
                "evidence",
                "evidence_bytes",
                "output_bytes",
                "diagnostic_bytes",
                "working_memory_bytes",
            ):
                values[name] = self.totals.get(name, 0) + validated_consumption.get(name, 0)
            prior_exhausted = self.totals.get("exhausted_dimensions", ())
            exhausted = set(prior_exhausted).union(validated_consumption.get("exhausted_dimensions", ()))
            limits = {
                "resolvers": self.internal_budget.get("max_resolvers", 0),
                "candidates": self.internal_budget.get("max_candidates", 0),
                "graph_rows": self.internal_budget.get("max_graph_rows", 0),
                "vector_results": self.internal_budget.get("max_vector_results", 0),
                "evidence": self.internal_budget.get("max_evidence", 0),
                "evidence_bytes": self.internal_budget.get("max_evidence_bytes", 0),
                "output_bytes": self.internal_budget.get("max_output_bytes", 0),
                "diagnostic_bytes": self.internal_budget.get("max_diagnostic_bytes", 0),
                "working_memory_bytes": self.internal_budget.get("max_working_memory_bytes", 0),
            }
            for name, limit in limits.items():
                if values.get(name, 0) > limit:
                    exhausted.add(name)
            values["exhausted_dimensions"] = tuple(sorted(exhausted))
            totals_measured = self.totals.get("measurement_available", False)
            consumption_measured = validated_consumption.get("measurement_available", False)
            values["measurement_available"] = totals_measured and consumption_measured
            self.totals = budget_consumption(**values)
            result = validate_budget_consumption(self.totals)
            return result

    def snapshot(self) -> dict:
        with self.internal_lock:
            result = validate_budget_consumption(self.totals)
            return result
