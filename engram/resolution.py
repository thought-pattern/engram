"""Transport-neutral contracts for the bounded unified resolution pipeline."""

import hashlib
import json
import math
import threading
from collections.abc import Callable, Mapping
from datetime import datetime
from types import MappingProxyType
from typing import TypedDict, cast

from engram.artifacts import LifecycleState
from engram.constants import (
    ACCOUNTING_OBSERVATION_FIELDS,
    ACCOUNTING_OBSERVATION_SCHEMA_VERSION,
    BUDGET_CONSUMPTION_FIELDS,
    BUDGET_CONSUMPTION_SCHEMA_VERSION,
    CANDIDATE_FIELDS,
    CANDIDATE_SCHEMA_VERSION,
    CANONICAL_CLAIM_REFERENCES_FIELDS,
    CANONICAL_CLAIM_REFERENCES_SCHEMA_VERSION,
    CLAIM_EVIDENCE_RECORD_FIELDS,
    CLAIM_EVIDENCE_RECORD_SCHEMA_VERSION,
    CLAIM_TRUST_INPUTS_FIELDS,
    CLAIM_TRUST_INPUTS_SCHEMA_VERSION,
    CLAIM_VALIDITY_INPUTS_FIELDS,
    CLAIM_VALIDITY_INPUTS_SCHEMA_VERSION,
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
    DEFAULT_RESOLUTION_RESOLVER_TIME_MS,
    DEFAULT_RESOLUTION_TOTAL_TIME_MS,
    DISCLOSURE_DECISION_FIELDS,
    DISCLOSURE_DECISION_SCHEMA_VERSION,
    EMPTY_MAPPING,
    EMPTY_SCOPE_KEY,
    EVIDENCE_PACKAGE_FIELDS,
    EVIDENCE_PACKAGE_WIRE_VERSION,
    EVIDENCE_REFERENCE_FIELDS,
    EVIDENCE_REFERENCE_SCHEMA_VERSION,
    FEATURE_SET_FIELDS,
    FEATURE_SET_SCHEMA_VERSION,
    INHERITANCE_PROVENANCE_FIELDS,
    MAX_ACCOUNTING_KEYWORD_BYTES,
    MAX_ACCOUNTING_KEYWORDS,
    MAX_CANDIDATE_ID_BYTES,
    MAX_CLAIM_IDENTIFIER_BYTES,
    MAX_CLAIM_SELECTION_REASONS,
    MAX_CLAIM_SOURCE_CONTRIBUTIONS,
    MAX_CLAIM_TIMESTAMP_BYTES,
    MAX_CLAIM_TRUST_CATEGORY_BYTES,
    MAX_DIAGNOSTIC_ID_BYTES,
    MAX_DISCLOSURE_AUTHORITY_BYTES,
    MAX_DISCLOSURE_ENUM_BYTES,
    MAX_EVIDENCE_PACKAGE_BYTES,
    MAX_EVIDENCE_PACKAGE_INPUT_RECORDS,
    MAX_EVIDENCE_PACKAGE_RECORDS,
    MAX_EVIDENCE_PACKAGE_TRUNCATION_REASONS,
    MAX_EXHAUSTED_DIMENSION_BYTES,
    MAX_EXHAUSTED_DIMENSIONS,
    MAX_FEATURES,
    MAX_JSON_BYTES,
    MAX_JSON_DEPTH,
    MAX_JSON_ITEMS,
    MAX_JSON_STRING_BYTES,
    MAX_REASON_CODE_BYTES,
    MAX_REQUEST_BYTES,
    MAX_REQUIRED_SOURCE_LABEL_BYTES,
    MAX_RESOLUTION_CANDIDATES,
    MAX_RESOLUTION_DIAGNOSTIC_BYTES,
    MAX_RESOLUTION_EVIDENCE,
    MAX_RESOLUTION_EVIDENCE_BYTES,
    MAX_RESOLUTION_GRAPH_ROWS,
    MAX_RESOLUTION_OUTPUT_BYTES,
    MAX_RESOLUTION_REASON_CODES,
    MAX_RESOLUTION_RESOLVERS,
    MAX_RESOLUTION_TIME_MS,
    MAX_RESOLUTION_VALUES,
    MAX_RESOLUTION_VECTOR_RESULTS,
    MAX_RESOLUTION_WORKING_MEMORY_BYTES,
    MAX_RESOLVER_NAME_BYTES,
    MAX_RESOURCE_COUNTER,
    MAX_RESPONSE_BYTES,
    MAX_STATEMENT_ID_BYTES,
    MAX_TRACE_STEPS,
    MIN_RESOLUTION_CANDIDATES,
    MIN_RESOLUTION_OUTPUT_BYTES,
    MIN_RESOLUTION_RESOLVERS,
    MIN_RESOLUTION_TIME_MS,
    QUERY_FRAME_FIELDS,
    QUERY_FRAME_SCHEMA_VERSION,
    RESOLUTION_BUDGET_FIELDS,
    RESOLUTION_BUDGET_SCHEMA_VERSION,
    RESOLUTION_RESULT_FIELDS,
    RESOLUTION_RESULT_SCHEMA_VERSION,
    RESOLVER_RESULT_FIELDS,
    RESOLVER_RESULT_SCHEMA_VERSION,
    REWRITE_TRACE_STEP_FIELDS,
    CandidateSource,
    ClaimOwnership,
    CostClass,
    DisclosureBasis,
    EvidenceKind,
    EvidencePackageTruncationReason,
    ExpectedObjectType,
    ResolutionOutcome,
    ResolverState,
)
from engram.eligibility import (
    EligibilityContext,
    EligibilityContextFactory,
    eligibility_context_from_dict,
    eligibility_context_to_dict,
    validate_eligibility_context,
)
from engram.errors import IdentityValidationError, InvalidRequestError
from engram.identity import (
    QueryIdentity,
    ScopeKey,
    build_retrieval_representation,
    build_standalone_identity,
    query_identity_from_dict,
    query_identity_to_dict,
    query_identity_to_json,
    scope_key_from_dict,
    scope_key_to_dict,
    validate_authoritative_identity,
    validate_query_identity,
    validate_scope_key,
)
from engram.substitutions import expand_contractions


def _require_text(value: object, name: str, maximum_bytes: int, *, allow_empty: bool) -> str:
    if not isinstance(value, str):
        raise InvalidRequestError(f"{name} must be a string")
    if not allow_empty and not value:
        raise InvalidRequestError(f"{name} must not be empty")
    if len(value.encode("utf-8")) > maximum_bytes:
        raise InvalidRequestError(f"{name} exceeds the limit of {maximum_bytes} UTF-8 bytes")
    return value


def _require_int(value: object, name: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise InvalidRequestError(f"{name} must be an integer")
    if not minimum <= value <= maximum:
        raise InvalidRequestError(f"{name} must be from {minimum} through {maximum}")
    return value


def _require_float(value: object, name: str, minimum: float, maximum: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise InvalidRequestError(f"{name} must be numeric")
    result = float(value)
    if not math.isfinite(result) or not minimum <= result <= maximum:
        raise InvalidRequestError(f"{name} must be finite and from {minimum} through {maximum}")
    return result


def _require_bool(value: object, name: str) -> bool:
    if not isinstance(value, bool):
        raise InvalidRequestError(f"{name} must be a boolean")
    return value


def _require_identifier(value: object, name: str, maximum_bytes: int = MAX_CLAIM_IDENTIFIER_BYTES) -> str:
    identifier = _require_text(value, name, maximum_bytes, allow_empty=False)
    if any(character.isspace() or ord(character) < 32 or ord(character) == 127 for character in identifier):
        raise InvalidRequestError(f"{name} must not contain whitespace or control characters")
    return identifier


def _require_claim_timestamp(value: object, available: object, name: str) -> tuple[str, bool]:
    presence = _require_bool(available, f"{name}_available")
    text = _require_text(value, name, MAX_CLAIM_TIMESTAMP_BYTES, allow_empty=not presence)
    if not presence:
        if text:
            raise InvalidRequestError(f"{name} must be empty when unavailable")
        result = text, presence
        return result
    if not text or not text.endswith("Z"):
        raise InvalidRequestError(f"{name} must be a canonical RFC 3339 UTC timestamp ending in Z")
    try:
        parsed = datetime.fromisoformat(text[:-1] + "+00:00")
    except ValueError as error:
        raise InvalidRequestError(f"{name} must be a canonical RFC 3339 UTC timestamp") from error
    if parsed.isoformat().replace("+00:00", "Z") != text:
        raise InvalidRequestError(f"{name} must use the canonical RFC 3339 UTC representation")
    result = text, presence
    return result


def _exact_mapping(value: object, name: str, keys: frozenset[str]) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise InvalidRequestError(f"{name} must be an object")
    observed = frozenset(value)
    if observed != keys:
        raise InvalidRequestError(f"{name} has invalid fields: missing={sorted(keys - observed)}, extra={sorted(observed - keys)}")
    return value


def _require_list(value: object, name: str) -> list[object]:
    if not isinstance(value, (list, tuple)):
        raise InvalidRequestError(f"{name} must be an array")
    result = list(value)
    return result


def _freeze_json(value: object, name: str, depth: int = 0, count=()) -> object:
    if not count:
        count = [0]
    if depth > MAX_JSON_DEPTH:
        raise InvalidRequestError(f"{name} exceeds the maximum nesting depth of {MAX_JSON_DEPTH}")
    count[0] += 1
    if count[0] > MAX_JSON_ITEMS:
        raise InvalidRequestError(f"{name} exceeds the limit of {MAX_JSON_ITEMS} values")
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        result = _require_text(value, name, MAX_JSON_STRING_BYTES, allow_empty=True)
        return result
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise InvalidRequestError(f"{name} numbers must be finite")
        return value
    if isinstance(value, Mapping):
        frozen = {}
        for key in sorted(value):
            if not isinstance(key, str):
                raise InvalidRequestError(f"{name} keys must be strings")
            _require_text(key, f"{name} key", 128, allow_empty=False)
            frozen[key] = _freeze_json(value[key], name, depth + 1, count)
        result = MappingProxyType(frozen)
        return result
    if isinstance(value, (list, tuple)):
        result = tuple(_freeze_json(item, name, depth + 1, count) for item in value)
        return result
    raise InvalidRequestError(f"{name} values must be concrete JSON values")


def _freeze_mapping(value: object, name: str) -> Mapping[str, object]:
    frozen = _freeze_json(value, name)
    if not isinstance(frozen, Mapping):
        raise InvalidRequestError(f"{name} must be an object")
    encoded = json.dumps(_thaw_json(frozen), sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    if len(encoded.encode("utf-8")) > MAX_JSON_BYTES:
        raise InvalidRequestError(f"{name} exceeds the limit of {MAX_JSON_BYTES} UTF-8 bytes")
    return frozen


def _thaw_json(value: object) -> object:
    if isinstance(value, Mapping):
        result = {key: _thaw_json(item) for key, item in value.items()}
        return result
    if isinstance(value, tuple):
        result = [_thaw_json(item) for item in value]
        return result
    return value


def _json_text(value: Mapping[str, object]) -> str:
    result = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    return result


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


def _enum_tuple(values: object, enum_type, name: str, maximum: int) -> tuple:
    if not isinstance(values, tuple):
        raise InvalidRequestError(f"{name} must be a tuple")
    if len(values) > maximum:
        raise InvalidRequestError(f"{name} exceeds the limit of {maximum}")
    if not all(isinstance(value, enum_type) for value in values):
        raise InvalidRequestError(f"{name} must contain only {enum_type.__name__} values")
    if len(set(values)) != len(values):
        raise InvalidRequestError(f"{name} must not contain duplicates")
    return values


ResolutionBudget = TypedDict(
    "ResolutionBudget",
    {
        "schema_version": int,
        "total_time_ms": int,
        "resolver_time_ms": int,
        "max_resolvers": int,
        "max_candidates": int,
        "max_graph_rows": int,
        "max_vector_results": int,
        "max_evidence": int,
        "max_evidence_bytes": int,
        "max_output_bytes": int,
        "max_diagnostic_bytes": int,
        "max_working_memory_bytes": int,
        "allowed_cost_classes": tuple[CostClass, ...],
        "started_ns": int,
        "deadline_ns": int,
    },
)


def resolution_budget(
    total_time_ms: object = DEFAULT_RESOLUTION_TOTAL_TIME_MS,
    resolver_time_ms: object = DEFAULT_RESOLUTION_RESOLVER_TIME_MS,
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
    deadline_ns: object = 0,
    schema_version: object = RESOLUTION_BUDGET_SCHEMA_VERSION,
) -> ResolutionBudget:
    """Build immutable limits and a captured monotonic deadline for one resolution."""
    version = _require_int(schema_version, "schema_version", 0, MAX_RESOURCE_COUNTER)
    if version != RESOLUTION_BUDGET_SCHEMA_VERSION:
        raise InvalidRequestError(f"unsupported resolution budget schema_version: {version}")
    normalized_total_time = _require_int(
        total_time_ms,
        "total_time_ms",
        MIN_RESOLUTION_TIME_MS,
        MAX_RESOLUTION_TIME_MS,
    )
    normalized_resolver_time = _require_int(
        resolver_time_ms,
        "resolver_time_ms",
        MIN_RESOLUTION_TIME_MS,
        normalized_total_time,
    )
    normalized_costs = _enum_tuple(allowed_cost_classes, CostClass, "allowed_cost_classes", len(CostClass))
    if not normalized_costs:
        raise InvalidRequestError("allowed_cost_classes must not be empty")
    normalized_started = _require_int(started_ns, "started_ns", 0, MAX_RESOURCE_COUNTER)
    normalized_deadline = _require_int(deadline_ns, "deadline_ns", 0, MAX_RESOURCE_COUNTER)
    if (normalized_started == 0) != (normalized_deadline == 0):
        raise InvalidRequestError("started_ns and deadline_ns must be present or absent together")
    if normalized_started and normalized_deadline <= normalized_started:
        raise InvalidRequestError("deadline_ns must be after started_ns")
    result: ResolutionBudget = {
        "schema_version": version,
        "total_time_ms": normalized_total_time,
        "resolver_time_ms": normalized_resolver_time,
        "max_resolvers": _require_int(max_resolvers, "max_resolvers", MIN_RESOLUTION_RESOLVERS, MAX_RESOLUTION_RESOLVERS),
        "max_candidates": _require_int(
            max_candidates,
            "max_candidates",
            MIN_RESOLUTION_CANDIDATES,
            MAX_RESOLUTION_CANDIDATES,
        ),
        "max_graph_rows": _require_int(max_graph_rows, "max_graph_rows", 0, MAX_RESOLUTION_GRAPH_ROWS),
        "max_vector_results": _require_int(max_vector_results, "max_vector_results", 0, MAX_RESOLUTION_VECTOR_RESULTS),
        "max_evidence": _require_int(max_evidence, "max_evidence", 0, MAX_RESOLUTION_EVIDENCE),
        "max_evidence_bytes": _require_int(max_evidence_bytes, "max_evidence_bytes", 0, MAX_RESOLUTION_EVIDENCE_BYTES),
        "max_output_bytes": _require_int(
            max_output_bytes,
            "max_output_bytes",
            MIN_RESOLUTION_OUTPUT_BYTES,
            MAX_RESOLUTION_OUTPUT_BYTES,
        ),
        "max_diagnostic_bytes": _require_int(
            max_diagnostic_bytes,
            "max_diagnostic_bytes",
            0,
            MAX_RESOLUTION_DIAGNOSTIC_BYTES,
        ),
        "max_working_memory_bytes": _require_int(
            max_working_memory_bytes,
            "max_working_memory_bytes",
            1,
            MAX_RESOLUTION_WORKING_MEMORY_BYTES,
        ),
        "allowed_cost_classes": normalized_costs,
        "started_ns": normalized_started,
        "deadline_ns": normalized_deadline,
    }
    return result


def validate_resolution_budget(value: object) -> ResolutionBudget:
    data = _exact_mapping(value, "ResolutionBudget", RESOLUTION_BUDGET_FIELDS)
    result = resolution_budget(
        total_time_ms=data["total_time_ms"],
        resolver_time_ms=data["resolver_time_ms"],
        max_resolvers=data["max_resolvers"],
        max_candidates=data["max_candidates"],
        max_graph_rows=data["max_graph_rows"],
        max_vector_results=data["max_vector_results"],
        max_evidence=data["max_evidence"],
        max_evidence_bytes=data["max_evidence_bytes"],
        max_output_bytes=data["max_output_bytes"],
        max_diagnostic_bytes=data["max_diagnostic_bytes"],
        max_working_memory_bytes=data["max_working_memory_bytes"],
        allowed_cost_classes=data["allowed_cost_classes"],
        started_ns=data["started_ns"],
        deadline_ns=data["deadline_ns"],
        schema_version=data["schema_version"],
    )
    return result


def resolution_budget_with_changes(value: object, changes: object) -> ResolutionBudget:
    budget = validate_resolution_budget(value)
    if not isinstance(changes, Mapping):
        raise InvalidRequestError("resolution budget changes must be an object")
    if not frozenset(changes).issubset(RESOLUTION_BUDGET_FIELDS):
        raise InvalidRequestError("resolution budget changes contain an unknown field")
    updated: dict[str, object] = dict(budget)
    updated.update(changes)
    result = validate_resolution_budget(updated)
    return result


def capture_resolution_budget(clock_ns: Callable[[], int], **limits: object) -> ResolutionBudget:
    if not callable(clock_ns):
        raise InvalidRequestError("budget clock_ns must be callable")
    if not frozenset(limits).issubset(RESOLUTION_BUDGET_FIELDS):
        raise InvalidRequestError("resolution budget limits contain an unknown field")
    started = clock_ns()
    if isinstance(started, bool) or not isinstance(started, int) or started < 1:
        raise InvalidRequestError("budget clock_ns must return a positive integer")
    total_time_ms = limits.get("total_time_ms", DEFAULT_RESOLUTION_TOTAL_TIME_MS)
    normalized_total_time = _require_int(
        total_time_ms,
        "total_time_ms",
        MIN_RESOLUTION_TIME_MS,
        MAX_RESOLUTION_TIME_MS,
    )
    values = dict(limits)
    values["total_time_ms"] = normalized_total_time
    values["started_ns"] = started
    values["deadline_ns"] = started + normalized_total_time * 1_000_000
    result = resolution_budget(**values)
    return result


def recapture_resolution_budget(value: object, clock_ns: Callable[[], int]) -> ResolutionBudget:
    """Capture a fresh deadline from immutable configured limits."""
    budget = validate_resolution_budget(value)
    result = capture_resolution_budget(
        clock_ns,
        total_time_ms=budget["total_time_ms"],
        resolver_time_ms=budget["resolver_time_ms"],
        max_resolvers=budget["max_resolvers"],
        max_candidates=budget["max_candidates"],
        max_graph_rows=budget["max_graph_rows"],
        max_vector_results=budget["max_vector_results"],
        max_evidence=budget["max_evidence"],
        max_evidence_bytes=budget["max_evidence_bytes"],
        max_output_bytes=budget["max_output_bytes"],
        max_diagnostic_bytes=budget["max_diagnostic_bytes"],
        max_working_memory_bytes=budget["max_working_memory_bytes"],
        allowed_cost_classes=budget["allowed_cost_classes"],
        schema_version=budget["schema_version"],
    )
    return result


def resolution_budget_to_dict(value: object) -> dict[str, object]:
    budget = validate_resolution_budget(value)
    result = {
        "schema_version": budget["schema_version"],
        "total_time_ms": budget["total_time_ms"],
        "resolver_time_ms": budget["resolver_time_ms"],
        "max_resolvers": budget["max_resolvers"],
        "max_candidates": budget["max_candidates"],
        "max_graph_rows": budget["max_graph_rows"],
        "max_vector_results": budget["max_vector_results"],
        "max_evidence": budget["max_evidence"],
        "max_evidence_bytes": budget["max_evidence_bytes"],
        "max_output_bytes": budget["max_output_bytes"],
        "max_diagnostic_bytes": budget["max_diagnostic_bytes"],
        "max_working_memory_bytes": budget["max_working_memory_bytes"],
        "allowed_cost_classes": [cost.value for cost in budget["allowed_cost_classes"]],
        "started_ns": budget["started_ns"],
        "deadline_ns": budget["deadline_ns"],
    }
    return result


def resolution_budget_from_dict(value: object) -> ResolutionBudget:
    data = _exact_mapping(value, "ResolutionBudget", RESOLUTION_BUDGET_FIELDS)
    raw_costs = _require_list(data["allowed_cost_classes"], "allowed_cost_classes")
    try:
        costs = tuple(CostClass(_require_text(item, "cost class", 32, allow_empty=False)) for item in raw_costs)
    except ValueError as error:
        raise InvalidRequestError("allowed_cost_classes contains an unsupported value") from error
    result = resolution_budget(
        schema_version=data["schema_version"],
        total_time_ms=data["total_time_ms"],
        resolver_time_ms=data["resolver_time_ms"],
        max_resolvers=data["max_resolvers"],
        max_candidates=data["max_candidates"],
        max_graph_rows=data["max_graph_rows"],
        max_vector_results=data["max_vector_results"],
        max_evidence=data["max_evidence"],
        max_evidence_bytes=data["max_evidence_bytes"],
        max_output_bytes=data["max_output_bytes"],
        max_diagnostic_bytes=data["max_diagnostic_bytes"],
        max_working_memory_bytes=data["max_working_memory_bytes"],
        allowed_cost_classes=costs,
        started_ns=data["started_ns"],
        deadline_ns=data["deadline_ns"],
    )
    return result


def resolution_budget_to_json(value: object) -> str:
    payload = resolution_budget_to_dict(value)
    result = _json_text(payload)
    return result


def resolution_budget_from_json(value: str) -> ResolutionBudget:
    data = _load_json_mapping(value, "ResolutionBudget JSON")
    result = resolution_budget_from_dict(data)
    return result


BudgetConsumption = TypedDict(
    "BudgetConsumption",
    {
        "schema_version": int,
        "elapsed_ns": int,
        "resolvers": int,
        "candidates": int,
        "graph_rows": int,
        "vector_results": int,
        "evidence": int,
        "evidence_bytes": int,
        "output_bytes": int,
        "diagnostic_bytes": int,
        "working_memory_bytes": int,
        "exhausted_dimensions": tuple[str, ...],
        "measurement_available": bool,
    },
)


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
    schema_version: object = BUDGET_CONSUMPTION_SCHEMA_VERSION,
) -> BudgetConsumption:
    """Build concrete resource use for a resolver or complete resolution."""
    version = _require_int(schema_version, "schema_version", 0, MAX_RESOURCE_COUNTER)
    if version != BUDGET_CONSUMPTION_SCHEMA_VERSION:
        raise InvalidRequestError(f"unsupported budget consumption schema_version: {version}")
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
        name: _require_int(field_value, name, 0, MAX_RESOURCE_COUNTER) for name, field_value in numeric_values.items()
    }
    if not isinstance(exhausted_dimensions, tuple):
        raise InvalidRequestError("exhausted_dimensions must be a tuple")
    if len(exhausted_dimensions) > MAX_EXHAUSTED_DIMENSIONS:
        raise InvalidRequestError(f"exhausted_dimensions exceeds the limit of {MAX_EXHAUSTED_DIMENSIONS}")
    normalized_exhausted = tuple(
        _require_text(value, "exhausted dimension", MAX_EXHAUSTED_DIMENSION_BYTES, allow_empty=False)
        for value in exhausted_dimensions
    )
    if normalized_exhausted != tuple(sorted(set(normalized_exhausted))):
        raise InvalidRequestError("exhausted_dimensions must be unique and sorted")
    if not isinstance(measurement_available, bool):
        raise InvalidRequestError("measurement_available must be a boolean")
    result: BudgetConsumption = {
        "schema_version": version,
        "elapsed_ns": normalized_numbers["elapsed_ns"],
        "resolvers": normalized_numbers["resolvers"],
        "candidates": normalized_numbers["candidates"],
        "graph_rows": normalized_numbers["graph_rows"],
        "vector_results": normalized_numbers["vector_results"],
        "evidence": normalized_numbers["evidence"],
        "evidence_bytes": normalized_numbers["evidence_bytes"],
        "output_bytes": normalized_numbers["output_bytes"],
        "diagnostic_bytes": normalized_numbers["diagnostic_bytes"],
        "working_memory_bytes": normalized_numbers["working_memory_bytes"],
        "exhausted_dimensions": normalized_exhausted,
        "measurement_available": measurement_available,
    }
    return result


def validate_budget_consumption(value: object) -> BudgetConsumption:
    data = _exact_mapping(value, "BudgetConsumption", BUDGET_CONSUMPTION_FIELDS)
    result = budget_consumption(
        schema_version=data["schema_version"],
        elapsed_ns=data["elapsed_ns"],
        resolvers=data["resolvers"],
        candidates=data["candidates"],
        graph_rows=data["graph_rows"],
        vector_results=data["vector_results"],
        evidence=data["evidence"],
        evidence_bytes=data["evidence_bytes"],
        output_bytes=data["output_bytes"],
        diagnostic_bytes=data["diagnostic_bytes"],
        working_memory_bytes=data["working_memory_bytes"],
        exhausted_dimensions=data["exhausted_dimensions"],
        measurement_available=data["measurement_available"],
    )
    return result


def budget_consumption_with_changes(value: object, changes: object) -> BudgetConsumption:
    consumption = validate_budget_consumption(value)
    if not isinstance(changes, Mapping):
        raise InvalidRequestError("budget consumption changes must be an object")
    if not frozenset(changes).issubset(BUDGET_CONSUMPTION_FIELDS):
        raise InvalidRequestError("budget consumption changes contain an unknown field")
    updated: dict[str, object] = dict(consumption)
    updated.update(changes)
    result = validate_budget_consumption(updated)
    return result


def budget_consumption_to_dict(value: object) -> dict[str, object]:
    consumption = validate_budget_consumption(value)
    result = {
        "schema_version": consumption["schema_version"],
        "elapsed_ns": consumption["elapsed_ns"],
        "resolvers": consumption["resolvers"],
        "candidates": consumption["candidates"],
        "graph_rows": consumption["graph_rows"],
        "vector_results": consumption["vector_results"],
        "evidence": consumption["evidence"],
        "evidence_bytes": consumption["evidence_bytes"],
        "output_bytes": consumption["output_bytes"],
        "diagnostic_bytes": consumption["diagnostic_bytes"],
        "working_memory_bytes": consumption["working_memory_bytes"],
        "exhausted_dimensions": list(consumption["exhausted_dimensions"]),
        "measurement_available": consumption["measurement_available"],
    }
    return result


def budget_consumption_from_dict(value: object) -> BudgetConsumption:
    data = _exact_mapping(value, "BudgetConsumption", BUDGET_CONSUMPTION_FIELDS)
    exhausted = _require_list(data["exhausted_dimensions"], "exhausted_dimensions")
    result = budget_consumption(
        schema_version=data["schema_version"],
        elapsed_ns=data["elapsed_ns"],
        resolvers=data["resolvers"],
        candidates=data["candidates"],
        graph_rows=data["graph_rows"],
        vector_results=data["vector_results"],
        evidence=data["evidence"],
        evidence_bytes=data["evidence_bytes"],
        output_bytes=data["output_bytes"],
        diagnostic_bytes=data["diagnostic_bytes"],
        working_memory_bytes=data["working_memory_bytes"],
        exhausted_dimensions=tuple(exhausted),
        measurement_available=data["measurement_available"],
    )
    return result


def budget_consumption_to_json(value: object) -> str:
    payload = budget_consumption_to_dict(value)
    result = _json_text(payload)
    return result


def budget_consumption_from_json(value: str) -> BudgetConsumption:
    data = _load_json_mapping(value, "BudgetConsumption JSON")
    result = budget_consumption_from_dict(data)
    return result


InheritanceProvenance = TypedDict("InheritanceProvenance", {"field_name": str, "source_turn": int})


def inheritance_provenance(field_name: object, source_turn: object) -> InheritanceProvenance:
    """Build concrete empty-capable contextual field provenance owned by Section 8."""
    result: InheritanceProvenance = {
        "field_name": _require_text(field_name, "inheritance field_name", 64, allow_empty=False),
        "source_turn": _require_int(source_turn, "inheritance source_turn", 1, 1_000_000),
    }
    return result


def validate_inheritance_provenance(value: object) -> InheritanceProvenance:
    data = _exact_mapping(value, "InheritanceProvenance", INHERITANCE_PROVENANCE_FIELDS)
    result = inheritance_provenance(data["field_name"], data["source_turn"])
    return result


def inheritance_provenance_signature(value: object) -> tuple[str, int]:
    provenance = validate_inheritance_provenance(value)
    result = (provenance["field_name"], provenance["source_turn"])
    return result


def inheritance_provenance_to_dict(value: object) -> dict[str, object]:
    provenance = validate_inheritance_provenance(value)
    result: dict[str, object] = dict(provenance)
    return result


def inheritance_provenance_from_dict(value: object) -> InheritanceProvenance:
    result = validate_inheritance_provenance(value)
    return result


RewriteTraceStep = TypedDict("RewriteTraceStep", {"rule_id": str, "input_text": str, "output_text": str})


def rewrite_trace_step(rule_id: object, input_text: object, output_text: object) -> RewriteTraceStep:
    """Build an empty-capable rewrite trace whose rule semantics belong to Section 11."""
    result: RewriteTraceStep = {
        "rule_id": _require_text(rule_id, "rewrite rule_id", 128, allow_empty=False),
        "input_text": _require_text(input_text, "rewrite input_text", MAX_REQUEST_BYTES, allow_empty=False),
        "output_text": _require_text(output_text, "rewrite output_text", MAX_REQUEST_BYTES, allow_empty=False),
    }
    return result


def validate_rewrite_trace_step(value: object) -> RewriteTraceStep:
    data = _exact_mapping(value, "RewriteTraceStep", REWRITE_TRACE_STEP_FIELDS)
    result = rewrite_trace_step(data["rule_id"], data["input_text"], data["output_text"])
    return result


def rewrite_trace_step_to_dict(value: object) -> dict[str, object]:
    step = validate_rewrite_trace_step(value)
    result: dict[str, object] = dict(step)
    return result


def rewrite_trace_step_from_dict(value: object) -> RewriteTraceStep:
    result = validate_rewrite_trace_step(value)
    return result


QueryFrame = TypedDict(
    "QueryFrame",
    {
        "original_text": str,
        "resolved_text": str,
        "identity": QueryIdentity,
        "expected_object_type": ExpectedObjectType,
        "inheritance": tuple[InheritanceProvenance, ...],
        "rewrite_chain": tuple[RewriteTraceStep, ...],
        "scope": ScopeKey,
        "required_metadata": Mapping[str, object],
        "required_source_label": str,
        "budget": ResolutionBudget,
        "eligibility_context": EligibilityContext,
        "diagnostic_id": str,
        "schema_version": int,
    },
)


def query_frame(
    original_text: object,
    resolved_text: object,
    identity: object,
    expected_object_type: object,
    inheritance: object,
    rewrite_chain: object,
    scope: object,
    required_metadata: object,
    required_source_label: object,
    budget: object,
    eligibility_context: object,
    diagnostic_id: object,
    schema_version: object = QUERY_FRAME_SCHEMA_VERSION,
) -> QueryFrame:
    """Build the immutable base interpretation passed to every resolver."""
    validated_schema_version = _require_int(schema_version, "schema_version", 0, MAX_RESOURCE_COUNTER)
    if validated_schema_version != QUERY_FRAME_SCHEMA_VERSION:
        raise InvalidRequestError(f"unsupported query frame schema_version: {validated_schema_version}")
    original = _require_text(original_text, "frame original_text", MAX_REQUEST_BYTES, allow_empty=False)
    resolved = _require_text(resolved_text, "frame resolved_text", MAX_REQUEST_BYTES, allow_empty=False)
    try:
        validated_identity = validate_query_identity(identity)
    except IdentityValidationError as error:
        raise InvalidRequestError("frame identity must be a QueryIdentity") from error
    if not isinstance(expected_object_type, ExpectedObjectType):
        raise InvalidRequestError("frame expected_object_type must be an ExpectedObjectType")
    if not isinstance(inheritance, tuple):
        raise InvalidRequestError("frame inheritance must be a tuple of InheritanceProvenance values")
    try:
        validated_inheritance = tuple(validate_inheritance_provenance(value) for value in inheritance)
    except InvalidRequestError as error:
        raise InvalidRequestError("frame inheritance must be a tuple of InheritanceProvenance values") from error
    inheritance_signatures = tuple(inheritance_provenance_signature(value) for value in validated_inheritance)
    if len(validated_inheritance) > MAX_TRACE_STEPS or len(set(inheritance_signatures)) != len(validated_inheritance):
        raise InvalidRequestError("frame inheritance must be bounded and unique")
    if not isinstance(rewrite_chain, tuple):
        raise InvalidRequestError("frame rewrite_chain must be a tuple of RewriteTraceStep values")
    try:
        validated_rewrite_chain = tuple(validate_rewrite_trace_step(value) for value in rewrite_chain)
    except InvalidRequestError as error:
        raise InvalidRequestError("frame rewrite_chain must be a tuple of RewriteTraceStep values") from error
    if len(validated_rewrite_chain) > MAX_TRACE_STEPS:
        raise InvalidRequestError(f"frame rewrite_chain exceeds the limit of {MAX_TRACE_STEPS}")
    try:
        validated_scope = validate_scope_key(scope)
    except IdentityValidationError as error:
        raise InvalidRequestError("frame scope must match identity scope") from error
    if validated_identity["scope"] != validated_scope:
        raise InvalidRequestError("frame scope must match identity scope")
    frozen_metadata = _freeze_mapping(required_metadata, "frame required_metadata")
    source_label = _require_text(
        required_source_label,
        "frame required_source_label",
        MAX_REQUIRED_SOURCE_LABEL_BYTES,
        allow_empty=True,
    )
    try:
        validated_budget = validate_resolution_budget(budget)
    except InvalidRequestError as error:
        raise InvalidRequestError("frame budget must be a ResolutionBudget") from error
    try:
        validated_context = validate_eligibility_context(eligibility_context)
    except InvalidRequestError as error:
        raise InvalidRequestError("frame eligibility_context must be an EligibilityContext") from error
    if validated_context["namespace"] != validated_scope["namespace"]:
        raise InvalidRequestError("frame eligibility context namespace must match scope")
    validated_diagnostic_id = _require_text(
        diagnostic_id,
        "frame diagnostic_id",
        MAX_DIAGNOSTIC_ID_BYTES,
        allow_empty=False,
    )
    result: QueryFrame = {
        "original_text": original,
        "resolved_text": resolved,
        "identity": validated_identity,
        "expected_object_type": expected_object_type,
        "inheritance": validated_inheritance,
        "rewrite_chain": validated_rewrite_chain,
        "scope": validated_scope,
        "required_metadata": frozen_metadata,
        "required_source_label": source_label,
        "budget": validated_budget,
        "eligibility_context": validated_context,
        "diagnostic_id": validated_diagnostic_id,
        "schema_version": validated_schema_version,
    }
    return result


def validate_query_frame(value: object) -> QueryFrame:
    data = _exact_mapping(value, "QueryFrame", QUERY_FRAME_FIELDS)
    result = query_frame(
        data["original_text"],
        data["resolved_text"],
        data["identity"],
        data["expected_object_type"],
        data["inheritance"],
        data["rewrite_chain"],
        data["scope"],
        data["required_metadata"],
        data["required_source_label"],
        data["budget"],
        data["eligibility_context"],
        data["diagnostic_id"],
        data["schema_version"],
    )
    return result


def query_frame_with_changes(value: object, changes: object) -> QueryFrame:
    current = validate_query_frame(value)
    if not isinstance(changes, Mapping) or not frozenset(changes).issubset(QUERY_FRAME_FIELDS):
        raise InvalidRequestError("query frame changes contain invalid fields")
    updated: dict[str, object] = dict(current)
    updated.update(changes)
    result = validate_query_frame(updated)
    return result


def query_frame_to_dict(value: object) -> dict[str, object]:
    frame = validate_query_frame(value)
    result = {
        "schema_version": frame["schema_version"],
        "original_text": frame["original_text"],
        "resolved_text": frame["resolved_text"],
        "identity": query_identity_to_dict(frame["identity"]),
        "expected_object_type": frame["expected_object_type"].value,
        "inheritance": [inheritance_provenance_to_dict(item) for item in frame["inheritance"]],
        "rewrite_chain": [rewrite_trace_step_to_dict(item) for item in frame["rewrite_chain"]],
        "scope": scope_key_to_dict(frame["scope"]),
        "required_metadata": _thaw_json(frame["required_metadata"]),
        "required_source_label": frame["required_source_label"],
        "budget": resolution_budget_to_dict(frame["budget"]),
        "eligibility_context": eligibility_context_to_dict(frame["eligibility_context"]),
        "diagnostic_id": frame["diagnostic_id"],
    }
    return result


def query_frame_to_json(value: object) -> str:
    payload = query_frame_to_dict(value)
    result = _json_text(payload)
    return result


def query_frame_from_dict(value: object) -> QueryFrame:
    data = _exact_mapping(value, "QueryFrame", QUERY_FRAME_FIELDS)
    inheritance = _require_list(data["inheritance"], "frame inheritance")
    rewrites = _require_list(data["rewrite_chain"], "frame rewrite_chain")
    try:
        expected_type = ExpectedObjectType(
            _require_text(data["expected_object_type"], "expected_object_type", 32, allow_empty=False)
        )
    except ValueError as error:
        raise InvalidRequestError("unsupported expected_object_type") from error
    result = query_frame(
        schema_version=_require_int(data["schema_version"], "schema_version", 1, 1),
        original_text=_require_text(data["original_text"], "frame original_text", MAX_REQUEST_BYTES, allow_empty=False),
        resolved_text=_require_text(data["resolved_text"], "frame resolved_text", MAX_REQUEST_BYTES, allow_empty=False),
        identity=query_identity_from_dict(
            cast(Mapping[str, object], _thaw_json(_freeze_mapping(data["identity"], "frame identity")))
        ),
        expected_object_type=expected_type,
        inheritance=tuple(inheritance_provenance_from_dict(_freeze_mapping(item, "inheritance item")) for item in inheritance),
        rewrite_chain=tuple(rewrite_trace_step_from_dict(_freeze_mapping(item, "rewrite item")) for item in rewrites),
        scope=scope_key_from_dict(_freeze_mapping(data["scope"], "frame scope")),
        required_metadata=_freeze_mapping(data["required_metadata"], "frame required_metadata"),
        required_source_label=_require_text(
            data["required_source_label"],
            "frame required_source_label",
            MAX_REQUIRED_SOURCE_LABEL_BYTES,
            allow_empty=True,
        ),
        budget=resolution_budget_from_dict(_freeze_mapping(data["budget"], "frame budget")),
        eligibility_context=eligibility_context_from_dict(
            _freeze_mapping(data["eligibility_context"], "frame eligibility_context")
        ),
        diagnostic_id=_require_text(
            data["diagnostic_id"],
            "frame diagnostic_id",
            MAX_DIAGNOSTIC_ID_BYTES,
            allow_empty=False,
        ),
    )
    return result


def query_frame_from_json(value: str) -> QueryFrame:
    decoded = _load_json_mapping(value, "QueryFrame JSON")
    result = query_frame_from_dict(decoded)
    return result


FeatureSet = TypedDict(
    "FeatureSet",
    {"values": Mapping[str, float], "unavailable": tuple[str, ...], "schema_version": int},
)


def feature_set(
    values: object = EMPTY_MAPPING,
    unavailable: object = (),
    schema_version: object = FEATURE_SET_SCHEMA_VERSION,
) -> FeatureSet:
    """Build a generic feature value/availability dictionary; Section 5 owns semantics."""
    version = _require_int(schema_version, "schema_version", 0, 2_147_483_647)
    if version != FEATURE_SET_SCHEMA_VERSION:
        raise InvalidRequestError(f"unsupported feature set schema_version: {version}")
    if not isinstance(values, Mapping):
        raise InvalidRequestError("feature values must be an object")
    validated_values = {}
    for name in sorted(values):
        key = _require_text(name, "feature name", 96, allow_empty=False)
        validated_values[key] = _require_float(values[name], f"feature {key}", -1_000_000.0, 1_000_000.0)
    if len(validated_values) > MAX_FEATURES:
        raise InvalidRequestError(f"feature values exceed the limit of {MAX_FEATURES}")
    if not isinstance(unavailable, tuple):
        raise InvalidRequestError("unavailable features must be a tuple")
    normalized_unavailable = tuple(_require_text(name, "unavailable feature", 96, allow_empty=False) for name in unavailable)
    if normalized_unavailable != tuple(sorted(set(normalized_unavailable))):
        raise InvalidRequestError("unavailable features must be unique and sorted")
    if set(validated_values).intersection(normalized_unavailable):
        raise InvalidRequestError("a feature cannot be both available and unavailable")
    if len(validated_values) + len(normalized_unavailable) > MAX_FEATURES:
        raise InvalidRequestError(f"features exceed the limit of {MAX_FEATURES}")
    result: FeatureSet = {
        "schema_version": version,
        "values": MappingProxyType(validated_values),
        "unavailable": normalized_unavailable,
    }
    return result


def validate_feature_set(value: object) -> FeatureSet:
    """Revalidate and defensively copy one feature-set dictionary."""
    data = _exact_mapping(value, "FeatureSet", FEATURE_SET_FIELDS)
    result = feature_set(data["values"], data["unavailable"], data["schema_version"])
    return result


def feature_set_with_changes(value: object, changes: object) -> FeatureSet:
    """Apply named fields and revalidate one feature-set dictionary."""
    features = validate_feature_set(value)
    if not isinstance(changes, Mapping):
        raise InvalidRequestError("feature set changes must be an object")
    if not frozenset(changes).issubset(FEATURE_SET_FIELDS):
        raise InvalidRequestError("feature set changes contain an unknown field")
    updated: dict[str, object] = dict(features)
    updated.update(changes)
    result = validate_feature_set(updated)
    return result


def feature_set_to_dict(value: object) -> dict[str, object]:
    """Serialize one feature set."""
    features = validate_feature_set(value)
    result = {
        "schema_version": features["schema_version"],
        "values": dict(features["values"]),
        "unavailable": list(features["unavailable"]),
    }
    return result


def feature_set_from_dict(value: object) -> FeatureSet:
    """Decode one feature set from its exact serialized form."""
    data = _exact_mapping(value, "FeatureSet", FEATURE_SET_FIELDS)
    unavailable = _require_list(data["unavailable"], "unavailable features")
    raw_values = _freeze_mapping(data["values"], "feature values")
    values = {name: _require_float(item, f"feature {name}", -1_000_000.0, 1_000_000.0) for name, item in raw_values.items()}
    normalized_unavailable = tuple(_require_text(item, "unavailable feature", 96, allow_empty=False) for item in unavailable)
    result = feature_set(values, normalized_unavailable, data["schema_version"])
    return result


def feature_set_to_json(value: object) -> str:
    """Serialize one feature set deterministically."""
    payload = feature_set_to_dict(value)
    result = _json_text(payload)
    return result


def feature_set_from_json(value: str) -> FeatureSet:
    """Decode one feature set from deterministic JSON."""
    data = _load_json_mapping(value, "FeatureSet JSON")
    result = feature_set_from_dict(data)
    return result


CanonicalClaimReferences = TypedDict(
    "CanonicalClaimReferences",
    {
        "subject_entity_id": str,
        "predicate_id": str,
        "object_entity_id": str,
        "schema_version": int,
    },
)


def canonical_claim_references(
    subject_entity_id: object,
    predicate_id: object,
    object_entity_id: object,
    schema_version: object = CANONICAL_CLAIM_REFERENCES_SCHEMA_VERSION,
) -> CanonicalClaimReferences:
    """Build canonical graph identifiers for one full Claim record."""
    version = _require_int(schema_version, "schema_version", 1, 1)
    result: CanonicalClaimReferences = {
        "schema_version": version,
        "subject_entity_id": _require_identifier(subject_entity_id, "Claim subject_entity_id"),
        "predicate_id": _require_identifier(predicate_id, "Claim predicate_id"),
        "object_entity_id": _require_identifier(object_entity_id, "Claim object_entity_id"),
    }
    return result


def validate_canonical_claim_references(value: object) -> CanonicalClaimReferences:
    """Revalidate and copy one canonical Claim-reference dictionary."""
    data = _exact_mapping(value, "CanonicalClaimReferences", CANONICAL_CLAIM_REFERENCES_FIELDS)
    result = canonical_claim_references(
        data["subject_entity_id"],
        data["predicate_id"],
        data["object_entity_id"],
        data["schema_version"],
    )
    return result


def canonical_claim_references_with_changes(value: object, changes: object) -> CanonicalClaimReferences:
    """Apply named fields and revalidate canonical Claim references."""
    references = validate_canonical_claim_references(value)
    if not isinstance(changes, Mapping):
        raise InvalidRequestError("canonical Claim reference changes must be an object")
    if not frozenset(changes).issubset(CANONICAL_CLAIM_REFERENCES_FIELDS):
        raise InvalidRequestError("canonical Claim reference changes contain an unknown field")
    updated: dict[str, object] = dict(references)
    updated.update(changes)
    result = validate_canonical_claim_references(updated)
    return result


def canonical_claim_references_to_dict(value: object) -> dict[str, object]:
    """Serialize canonical Claim references."""
    references = validate_canonical_claim_references(value)
    result: dict[str, object] = dict(references)
    return result


def canonical_claim_references_from_dict(value: object) -> CanonicalClaimReferences:
    """Decode canonical Claim references from their exact serialized form."""
    result = validate_canonical_claim_references(value)
    return result


ClaimValidityInputs = TypedDict(
    "ClaimValidityInputs",
    {
        "evaluation_time": str,
        "active": bool,
        "system_current": bool,
        "valid_time_current": bool,
        "valid_from": str,
        "valid_from_available": bool,
        "valid_to": str,
        "valid_to_available": bool,
        "schema_version": int,
    },
)


def claim_validity_inputs(
    evaluation_time: object,
    active: object,
    system_current: object,
    valid_time_current: object,
    valid_from: object = "",
    valid_from_available: object = False,
    valid_to: object = "",
    valid_to_available: object = False,
    schema_version: object = CLAIM_VALIDITY_INPUTS_SCHEMA_VERSION,
) -> ClaimValidityInputs:
    """Build current-time validity inputs for one eligible Claim."""
    version = _require_int(schema_version, "schema_version", 1, 1)
    evaluation, _evaluation_available = _require_claim_timestamp(evaluation_time, True, "Claim evaluation_time")
    normalized_active = _require_bool(active, "Claim active")
    normalized_system_current = _require_bool(system_current, "Claim system_current")
    normalized_valid_time_current = _require_bool(valid_time_current, "Claim valid_time_current")
    lower, lower_available = _require_claim_timestamp(valid_from, valid_from_available, "Claim valid_from")
    upper, upper_available = _require_claim_timestamp(valid_to, valid_to_available, "Claim valid_to")
    if lower_available and upper_available:
        lower_time = datetime.fromisoformat(lower[:-1] + "+00:00")
        upper_time = datetime.fromisoformat(upper[:-1] + "+00:00")
        if lower_time >= upper_time:
            raise InvalidRequestError("Claim valid_from must be earlier than valid_to")
    evaluated_at = datetime.fromisoformat(evaluation[:-1] + "+00:00")
    observed_current = (not lower_available or evaluated_at >= datetime.fromisoformat(lower[:-1] + "+00:00")) and (
        not upper_available or evaluated_at < datetime.fromisoformat(upper[:-1] + "+00:00")
    )
    if normalized_valid_time_current != observed_current:
        raise InvalidRequestError("Claim valid_time_current conflicts with the disclosed validity bounds")
    if not (normalized_active and normalized_system_current and normalized_valid_time_current):
        raise InvalidRequestError("Claim evidence validity inputs must describe a currently eligible Claim")
    result: ClaimValidityInputs = {
        "schema_version": version,
        "evaluation_time": evaluation,
        "active": normalized_active,
        "system_current": normalized_system_current,
        "valid_time_current": normalized_valid_time_current,
        "valid_from": lower,
        "valid_from_available": lower_available,
        "valid_to": upper,
        "valid_to_available": upper_available,
    }
    return result


def validate_claim_validity_inputs(value: object) -> ClaimValidityInputs:
    """Revalidate and copy one Claim-validity input dictionary."""
    data = _exact_mapping(value, "ClaimValidityInputs", CLAIM_VALIDITY_INPUTS_FIELDS)
    result = claim_validity_inputs(
        data["evaluation_time"],
        data["active"],
        data["system_current"],
        data["valid_time_current"],
        data["valid_from"],
        data["valid_from_available"],
        data["valid_to"],
        data["valid_to_available"],
        data["schema_version"],
    )
    return result


def claim_validity_inputs_with_changes(value: object, changes: object) -> ClaimValidityInputs:
    """Apply named fields and revalidate complete Claim-validity inputs."""
    validity = validate_claim_validity_inputs(value)
    if not isinstance(changes, Mapping):
        raise InvalidRequestError("Claim validity changes must be an object")
    if not frozenset(changes).issubset(CLAIM_VALIDITY_INPUTS_FIELDS):
        raise InvalidRequestError("Claim validity changes contain an unknown field")
    updated: dict[str, object] = dict(validity)
    updated.update(changes)
    result = validate_claim_validity_inputs(updated)
    return result


def claim_validity_inputs_to_dict(value: object) -> dict[str, object]:
    """Serialize Claim-validity inputs."""
    validity = validate_claim_validity_inputs(value)
    result: dict[str, object] = dict(validity)
    return result


def claim_validity_inputs_from_dict(value: object) -> ClaimValidityInputs:
    """Decode Claim-validity inputs from their exact serialized form."""
    result = validate_claim_validity_inputs(value)
    return result


ClaimTrustInputs = TypedDict(
    "ClaimTrustInputs",
    {
        "trust_category": str,
        "trust_category_available": bool,
        "supplied_trust": float,
        "supplied_trust_available": bool,
        "supplied_trust_version": int,
        "supplied_trust_version_available": bool,
        "schema_version": int,
    },
)


def claim_trust_inputs(
    trust_category: object = "",
    trust_category_available: object = False,
    supplied_trust: object = 0.0,
    supplied_trust_available: object = False,
    supplied_trust_version: object = 0,
    supplied_trust_version_available: object = False,
    schema_version: object = CLAIM_TRUST_INPUTS_SCHEMA_VERSION,
) -> ClaimTrustInputs:
    """Build supplied Claim trust values with concrete availability."""
    version = _require_int(schema_version, "schema_version", 1, 1)
    category_available = _require_bool(trust_category_available, "Claim trust_category_available")
    category = _require_text(
        trust_category,
        "Claim trust_category",
        MAX_CLAIM_TRUST_CATEGORY_BYTES,
        allow_empty=not category_available,
    )
    if not category_available and category:
        raise InvalidRequestError("Claim trust_category must be empty when unavailable")
    supplied_available = _require_bool(supplied_trust_available, "Claim supplied_trust_available")
    supplied = _require_float(supplied_trust, "Claim supplied_trust", 0.0, 1.0)
    if not supplied_available and supplied != 0.0:
        raise InvalidRequestError("Claim supplied_trust must be zero when unavailable")
    version_available = _require_bool(supplied_trust_version_available, "Claim supplied_trust_version_available")
    trust_version = _require_int(supplied_trust_version, "Claim supplied_trust_version", 0, 2_147_483_647)
    if not version_available and trust_version != 0:
        raise InvalidRequestError("Claim supplied_trust_version must be zero when unavailable")
    if supplied_available != version_available:
        raise InvalidRequestError("Claim supplied trust value and version availability must match")
    if version_available and trust_version == 0:
        raise InvalidRequestError("Claim supplied_trust_version must be positive when available")
    result: ClaimTrustInputs = {
        "schema_version": version,
        "trust_category": category,
        "trust_category_available": category_available,
        "supplied_trust": supplied,
        "supplied_trust_available": supplied_available,
        "supplied_trust_version": trust_version,
        "supplied_trust_version_available": version_available,
    }
    return result


def validate_claim_trust_inputs(value: object) -> ClaimTrustInputs:
    """Revalidate and copy one Claim-trust input dictionary."""
    data = _exact_mapping(value, "ClaimTrustInputs", CLAIM_TRUST_INPUTS_FIELDS)
    result = claim_trust_inputs(
        data["trust_category"],
        data["trust_category_available"],
        data["supplied_trust"],
        data["supplied_trust_available"],
        data["supplied_trust_version"],
        data["supplied_trust_version_available"],
        data["schema_version"],
    )
    return result


def claim_trust_inputs_with_changes(value: object, changes: object) -> ClaimTrustInputs:
    """Apply named fields and revalidate complete Claim-trust inputs."""
    trust = validate_claim_trust_inputs(value)
    if not isinstance(changes, Mapping):
        raise InvalidRequestError("Claim trust changes must be an object")
    if not frozenset(changes).issubset(CLAIM_TRUST_INPUTS_FIELDS):
        raise InvalidRequestError("Claim trust changes contain an unknown field")
    updated: dict[str, object] = dict(trust)
    updated.update(changes)
    result = validate_claim_trust_inputs(updated)
    return result


def claim_trust_inputs_to_dict(value: object) -> dict[str, object]:
    """Serialize Claim-trust inputs."""
    trust = validate_claim_trust_inputs(value)
    result: dict[str, object] = dict(trust)
    return result


def claim_trust_inputs_from_dict(value: object) -> ClaimTrustInputs:
    """Decode Claim-trust inputs from their exact serialized form."""
    result = validate_claim_trust_inputs(value)
    return result


DisclosureDecision = TypedDict(
    "DisclosureDecision",
    {
        "ownership": ClaimOwnership,
        "basis": DisclosureBasis,
        "scope": ScopeKey,
        "policy_version": str,
        "authority": str,
        "authority_available": bool,
        "schema_version": int,
    },
)


def disclosure_decision(
    ownership: object,
    basis: object,
    scope: object,
    policy_version: object,
    authority: object = "",
    authority_available: object = False,
    schema_version: object = DISCLOSURE_DECISION_SCHEMA_VERSION,
) -> DisclosureDecision:
    """Build an exact scoped Claim-disclosure decision."""
    version = _require_int(schema_version, "schema_version", 1, 1)
    if not isinstance(ownership, ClaimOwnership):
        raise InvalidRequestError("disclosure ownership must be a ClaimOwnership")
    if not isinstance(basis, DisclosureBasis):
        raise InvalidRequestError("disclosure basis must be a DisclosureBasis")
    try:
        validated_scope = validate_scope_key(scope)
    except IdentityValidationError as error:
        raise InvalidRequestError("disclosure scope must be a ScopeKey") from error
    normalized_policy_version = _require_identifier(policy_version, "disclosure policy_version")
    normalized_authority_available = _require_bool(authority_available, "disclosure authority_available")
    if normalized_authority_available:
        normalized_authority = _require_identifier(authority, "disclosure authority")
    else:
        normalized_authority = _require_text(
            authority,
            "disclosure authority",
            MAX_DISCLOSURE_AUTHORITY_BYTES,
            allow_empty=True,
        )
    if not normalized_authority_available and normalized_authority:
        raise InvalidRequestError("disclosure authority must be empty when unavailable")
    if ownership == ClaimOwnership.PUBLIC:
        if basis != DisclosureBasis.PUBLIC_RULE or normalized_authority_available:
            raise InvalidRequestError("PUBLIC Claim disclosure requires the public rule without an authority")
    elif basis != DisclosureBasis.TRUSTED_SCOPE_AUTHORITY or not normalized_authority_available:
        raise InvalidRequestError("non-PUBLIC Claim disclosure requires an available trusted scope authority")
    result: DisclosureDecision = {
        "schema_version": version,
        "ownership": ownership,
        "basis": basis,
        "scope": validated_scope,
        "policy_version": normalized_policy_version,
        "authority": normalized_authority,
        "authority_available": normalized_authority_available,
    }
    return result


def validate_disclosure_decision(value: object) -> DisclosureDecision:
    """Revalidate and copy one disclosure-decision dictionary."""
    data = _exact_mapping(value, "DisclosureDecision", DISCLOSURE_DECISION_FIELDS)
    result = disclosure_decision(
        data["ownership"],
        data["basis"],
        data["scope"],
        data["policy_version"],
        data["authority"],
        data["authority_available"],
        data["schema_version"],
    )
    return result


def disclosure_decision_with_changes(value: object, changes: object) -> DisclosureDecision:
    """Apply named fields and revalidate one disclosure decision."""
    decision = validate_disclosure_decision(value)
    if not isinstance(changes, Mapping):
        raise InvalidRequestError("disclosure decision changes must be an object")
    if not frozenset(changes).issubset(DISCLOSURE_DECISION_FIELDS):
        raise InvalidRequestError("disclosure decision changes contain an unknown field")
    updated: dict[str, object] = dict(decision)
    updated.update(changes)
    result = validate_disclosure_decision(updated)
    return result


def disclosure_decision_to_dict(value: object) -> dict[str, object]:
    """Serialize one disclosure decision."""
    decision = validate_disclosure_decision(value)
    result = {
        "schema_version": decision["schema_version"],
        "ownership": decision["ownership"].value,
        "basis": decision["basis"].value,
        "scope": scope_key_to_dict(decision["scope"]),
        "policy_version": decision["policy_version"],
        "authority": decision["authority"],
        "authority_available": decision["authority_available"],
    }
    return result


def disclosure_decision_from_dict(value: object) -> DisclosureDecision:
    """Decode one disclosure decision from its exact serialized form."""
    data = _exact_mapping(value, "DisclosureDecision", DISCLOSURE_DECISION_FIELDS)
    try:
        ownership = ClaimOwnership(
            _require_text(data["ownership"], "disclosure ownership", MAX_DISCLOSURE_ENUM_BYTES, allow_empty=False)
        )
        basis = DisclosureBasis(_require_text(data["basis"], "disclosure basis", MAX_DISCLOSURE_ENUM_BYTES, allow_empty=False))
    except ValueError as error:
        raise InvalidRequestError("unsupported disclosure ownership or basis") from error
    result = disclosure_decision(
        ownership,
        basis,
        scope_key_from_dict(_freeze_mapping(data["scope"], "disclosure scope")),
        data["policy_version"],
        data["authority"],
        data["authority_available"],
        data["schema_version"],
    )
    return result


ClaimEvidenceRecord = TypedDict(
    "ClaimEvidenceRecord",
    {
        "claim_id": str,
        "source_resolver": str,
        "source_contributions": tuple[str, ...],
        "features": FeatureSet,
        "canonical_references": CanonicalClaimReferences,
        "validity": ClaimValidityInputs,
        "trust": ClaimTrustInputs,
        "disclosure": DisclosureDecision,
        "path": tuple[str, ...],
        "selection_reasons": tuple[str, ...],
        "schema_version": int,
    },
)


def claim_evidence_record(
    claim_id: object,
    source_resolver: object,
    source_contributions: object,
    features: object,
    canonical_references: object,
    validity: object,
    trust: object,
    disclosure: object,
    path: object,
    selection_reasons: object,
    schema_version: object = CLAIM_EVIDENCE_RECORD_SCHEMA_VERSION,
) -> ClaimEvidenceRecord:
    """Build strict wire-safe full-Claim evidence without unrestricted graph content."""
    version = _require_int(schema_version, "schema_version", 1, CLAIM_EVIDENCE_RECORD_SCHEMA_VERSION)
    normalized_claim_id = _require_identifier(claim_id, "Claim evidence claim_id")
    source = _require_identifier(source_resolver, "Claim evidence source_resolver", MAX_RESOLVER_NAME_BYTES)
    if not isinstance(source_contributions, tuple):
        raise InvalidRequestError("Claim evidence source_contributions must be a tuple")
    contributions = tuple(
        _require_identifier(value, "Claim evidence source contribution", MAX_RESOLVER_NAME_BYTES) for value in source_contributions
    )
    if not contributions or len(contributions) > MAX_CLAIM_SOURCE_CONTRIBUTIONS:
        raise InvalidRequestError(
            f"Claim evidence source_contributions must contain 1 through {MAX_CLAIM_SOURCE_CONTRIBUTIONS} values"
        )
    if contributions != tuple(sorted(set(contributions))):
        raise InvalidRequestError("Claim evidence source_contributions must be unique and sorted")
    if source not in contributions:
        raise InvalidRequestError("Claim evidence source_resolver must be present in source_contributions")
    try:
        validated_features = validate_feature_set(features)
    except InvalidRequestError as error:
        raise InvalidRequestError("Claim evidence features must be a FeatureSet") from error
    try:
        validated_references = validate_canonical_claim_references(canonical_references)
    except InvalidRequestError as error:
        raise InvalidRequestError("Claim evidence canonical_references must be CanonicalClaimReferences") from error
    try:
        validated_validity = validate_claim_validity_inputs(validity)
    except InvalidRequestError as error:
        raise InvalidRequestError("Claim evidence validity must be ClaimValidityInputs") from error
    try:
        validated_trust = validate_claim_trust_inputs(trust)
    except InvalidRequestError as error:
        raise InvalidRequestError("Claim evidence trust must be ClaimTrustInputs") from error
    try:
        validated_disclosure = validate_disclosure_decision(disclosure)
    except InvalidRequestError as error:
        raise InvalidRequestError("Claim evidence disclosure must be DisclosureDecision") from error
    if not isinstance(path, tuple):
        raise InvalidRequestError("Claim evidence path must be a tuple")
    normalized_path = tuple(_require_identifier(value, "Claim evidence path identifier") for value in path)
    if normalized_path != (normalized_claim_id,):
        raise InvalidRequestError("Section 7 Claim evidence path must be the singleton claim_id")
    if not isinstance(selection_reasons, tuple):
        raise InvalidRequestError("Claim evidence selection_reasons must be a tuple")
    reasons = tuple(
        _require_identifier(value, "Claim evidence selection reason", MAX_REASON_CODE_BYTES) for value in selection_reasons
    )
    if not reasons or len(reasons) > MAX_CLAIM_SELECTION_REASONS:
        raise InvalidRequestError(
            "Claim evidence selection_reasons must contain " f"1 through {MAX_CLAIM_SELECTION_REASONS} values"
        )
    if reasons != tuple(sorted(set(reasons))):
        raise InvalidRequestError("Claim evidence selection_reasons must be unique and sorted")
    result: ClaimEvidenceRecord = {
        "schema_version": version,
        "claim_id": normalized_claim_id,
        "source_resolver": source,
        "source_contributions": contributions,
        "features": validated_features,
        "canonical_references": validated_references,
        "validity": validated_validity,
        "trust": validated_trust,
        "disclosure": validated_disclosure,
        "path": normalized_path,
        "selection_reasons": reasons,
    }
    return result


def validate_claim_evidence_record(value: object) -> ClaimEvidenceRecord:
    """Revalidate and defensively copy one full-Claim evidence dictionary."""
    data = _exact_mapping(value, "ClaimEvidenceRecord", CLAIM_EVIDENCE_RECORD_FIELDS)
    result = claim_evidence_record(
        data["claim_id"],
        data["source_resolver"],
        data["source_contributions"],
        data["features"],
        data["canonical_references"],
        data["validity"],
        data["trust"],
        data["disclosure"],
        data["path"],
        data["selection_reasons"],
        data["schema_version"],
    )
    return result


def claim_evidence_record_with_changes(value: object, changes: object) -> ClaimEvidenceRecord:
    """Apply named fields and revalidate one full-Claim evidence dictionary."""
    record = validate_claim_evidence_record(value)
    if not isinstance(changes, Mapping):
        raise InvalidRequestError("Claim evidence changes must be an object")
    if not frozenset(changes).issubset(CLAIM_EVIDENCE_RECORD_FIELDS):
        raise InvalidRequestError("Claim evidence changes contain an unknown field")
    updated: dict[str, object] = dict(record)
    updated.update(changes)
    result = validate_claim_evidence_record(updated)
    return result


def claim_evidence_record_to_dict(value: object) -> dict[str, object]:
    """Serialize one full-Claim evidence record."""
    record = validate_claim_evidence_record(value)
    result = {
        "schema_version": record["schema_version"],
        "claim_id": record["claim_id"],
        "source_resolver": record["source_resolver"],
        "source_contributions": list(record["source_contributions"]),
        "features": feature_set_to_dict(record["features"]),
        "canonical_references": canonical_claim_references_to_dict(record["canonical_references"]),
        "validity": claim_validity_inputs_to_dict(record["validity"]),
        "trust": claim_trust_inputs_to_dict(record["trust"]),
        "disclosure": disclosure_decision_to_dict(record["disclosure"]),
        "path": list(record["path"]),
        "selection_reasons": list(record["selection_reasons"]),
    }
    return result


def claim_evidence_record_from_dict(value: object) -> ClaimEvidenceRecord:
    """Decode one full-Claim evidence record from its exact serialized form."""
    data = _exact_mapping(value, "ClaimEvidenceRecord", CLAIM_EVIDENCE_RECORD_FIELDS)
    contributions = _require_list(data["source_contributions"], "Claim evidence source_contributions")
    path = _require_list(data["path"], "Claim evidence path")
    reasons = _require_list(data["selection_reasons"], "Claim evidence selection_reasons")
    normalized_contributions = tuple(
        _require_identifier(item, "Claim evidence source contribution", MAX_RESOLVER_NAME_BYTES) for item in contributions
    )
    normalized_path = tuple(_require_identifier(item, "Claim evidence path identifier") for item in path)
    normalized_reasons = tuple(
        _require_identifier(item, "Claim evidence selection reason", MAX_REASON_CODE_BYTES) for item in reasons
    )
    validated_features = feature_set_from_dict(_freeze_mapping(data["features"], "Claim evidence features"))
    validated_references = canonical_claim_references_from_dict(
        _freeze_mapping(data["canonical_references"], "Claim evidence canonical_references")
    )
    validated_validity = claim_validity_inputs_from_dict(_freeze_mapping(data["validity"], "Claim evidence validity"))
    validated_trust = claim_trust_inputs_from_dict(_freeze_mapping(data["trust"], "Claim evidence trust"))
    validated_disclosure = disclosure_decision_from_dict(_freeze_mapping(data["disclosure"], "Claim evidence disclosure"))
    result = claim_evidence_record(
        data["claim_id"],
        data["source_resolver"],
        normalized_contributions,
        validated_features,
        validated_references,
        validated_validity,
        validated_trust,
        validated_disclosure,
        normalized_path,
        normalized_reasons,
        data["schema_version"],
    )
    return result


def claim_evidence_record_to_json(value: object) -> str:
    """Serialize one full-Claim evidence record deterministically."""
    payload = claim_evidence_record_to_dict(value)
    result = _json_text(payload)
    return result


def claim_evidence_record_from_json(value: object) -> ClaimEvidenceRecord:
    """Decode one full-Claim evidence record from deterministic JSON."""
    if not isinstance(value, str):
        raise InvalidRequestError("ClaimEvidenceRecord JSON must be a string")
    data = _load_json_mapping(value, "ClaimEvidenceRecord JSON")
    result = claim_evidence_record_from_dict(data)
    return result


EvidencePackage = TypedDict(
    "EvidencePackage",
    {
        "records": tuple[ClaimEvidenceRecord, ...],
        "retained_count": int,
        "omitted_count": int,
        "truncated": bool,
        "truncation_reasons": tuple[EvidencePackageTruncationReason, ...],
        "wire_version": int,
    },
)


def _evidence_package_payload(value: EvidencePackage) -> dict[str, object]:
    result = {
        "wire_version": value["wire_version"],
        "records": [claim_evidence_record_to_dict(record) for record in value["records"]],
        "retained_count": value["retained_count"],
        "omitted_count": value["omitted_count"],
        "truncated": value["truncated"],
        "truncation_reasons": [reason.value for reason in value["truncation_reasons"]],
    }
    return result


def evidence_package(
    records: object,
    retained_count: object,
    omitted_count: object,
    truncated: object,
    truncation_reasons: object,
    wire_version: object = EVIDENCE_PACKAGE_WIRE_VERSION,
) -> EvidencePackage:
    """Build one canonical, count- and byte-bounded full-Claim package."""
    version = _require_int(wire_version, "wire_version", 1, EVIDENCE_PACKAGE_WIRE_VERSION)
    if not isinstance(records, tuple):
        raise InvalidRequestError("evidence package records must be a tuple of ClaimEvidenceRecord values")
    try:
        validated_records = tuple(validate_claim_evidence_record(record) for record in records)
    except InvalidRequestError as error:
        raise InvalidRequestError("evidence package records must be a tuple of ClaimEvidenceRecord values") from error
    if len(validated_records) > MAX_EVIDENCE_PACKAGE_RECORDS:
        raise InvalidRequestError(f"evidence package records exceeds the limit of {MAX_EVIDENCE_PACKAGE_RECORDS}")
    identifiers = tuple(record["claim_id"] for record in validated_records)
    if identifiers != tuple(sorted(identifiers)):
        raise InvalidRequestError("evidence package records must use canonical Claim-ID order")
    if len(set(identifiers)) != len(identifiers):
        raise InvalidRequestError("evidence package records must have unique Claim IDs")
    retained = _require_int(retained_count, "evidence package retained_count", 0, 2_147_483_647)
    omitted = _require_int(omitted_count, "evidence package omitted_count", 0, 2_147_483_647)
    if retained != len(validated_records):
        raise InvalidRequestError("evidence package retained_count must equal the number of records")
    normalized_truncated = _require_bool(truncated, "evidence package truncated")
    if not isinstance(truncation_reasons, tuple):
        raise InvalidRequestError("evidence package truncation_reasons must be a tuple")
    if len(truncation_reasons) > MAX_EVIDENCE_PACKAGE_TRUNCATION_REASONS:
        raise InvalidRequestError(
            "evidence package truncation_reasons exceeds the limit of " f"{MAX_EVIDENCE_PACKAGE_TRUNCATION_REASONS}"
        )
    if not all(isinstance(reason, EvidencePackageTruncationReason) for reason in truncation_reasons):
        raise InvalidRequestError("evidence package truncation_reasons must contain EvidencePackageTruncationReason values")
    normalized_reasons = tuple(truncation_reasons)
    reason_values = tuple(reason.value for reason in normalized_reasons)
    if reason_values != tuple(sorted(set(reason_values))):
        raise InvalidRequestError("evidence package truncation_reasons must be unique and sorted")
    if normalized_truncated != (omitted > 0):
        raise InvalidRequestError("evidence package truncated must equal whether omitted_count is positive")
    if normalized_truncated != bool(normalized_reasons):
        raise InvalidRequestError("evidence package truncation_reasons must be present exactly when truncated")
    result: EvidencePackage = {
        "wire_version": version,
        "records": validated_records,
        "retained_count": retained,
        "omitted_count": omitted,
        "truncated": normalized_truncated,
        "truncation_reasons": normalized_reasons,
    }
    encoded = _json_text(_evidence_package_payload(result)).encode("utf-8")
    if len(encoded) > MAX_EVIDENCE_PACKAGE_BYTES:
        raise InvalidRequestError(f"evidence package exceeds the limit of {MAX_EVIDENCE_PACKAGE_BYTES} UTF-8 bytes")
    return result


def empty_evidence_package() -> EvidencePackage:
    """Return one isolated concrete empty evidence package."""
    result = evidence_package((), 0, 0, False, ())
    return result


def validate_evidence_package(value: object) -> EvidencePackage:
    """Revalidate and defensively copy one evidence-package dictionary."""
    data = _exact_mapping(value, "EvidencePackage", EVIDENCE_PACKAGE_FIELDS)
    result = evidence_package(
        data["records"],
        data["retained_count"],
        data["omitted_count"],
        data["truncated"],
        data["truncation_reasons"],
        data["wire_version"],
    )
    return result


def evidence_package_with_changes(value: object, changes: object) -> EvidencePackage:
    """Apply named fields and revalidate one evidence-package dictionary."""
    package = validate_evidence_package(value)
    if not isinstance(changes, Mapping):
        raise InvalidRequestError("evidence package changes must be an object")
    if not frozenset(changes).issubset(EVIDENCE_PACKAGE_FIELDS):
        raise InvalidRequestError("evidence package changes contain an unknown field")
    updated: dict[str, object] = dict(package)
    updated.update(changes)
    result = validate_evidence_package(updated)
    return result


def _canonical_claim_evidence_records(records: object) -> tuple[tuple[ClaimEvidenceRecord, ...], int]:
    if not isinstance(records, tuple):
        raise InvalidRequestError("evidence package input must be a tuple of ClaimEvidenceRecord values")
    if len(records) > MAX_EVIDENCE_PACKAGE_INPUT_RECORDS:
        raise InvalidRequestError(f"evidence package input exceeds the limit of {MAX_EVIDENCE_PACKAGE_INPUT_RECORDS}")
    try:
        validated_records = tuple(validate_claim_evidence_record(record) for record in records)
    except InvalidRequestError as error:
        raise InvalidRequestError("evidence package input must be a tuple of ClaimEvidenceRecord values") from error
    by_claim_id: dict[str, ClaimEvidenceRecord] = {}
    duplicate_count = 0
    for record in validated_records:
        claim_id = record["claim_id"]
        if claim_id in by_claim_id:
            previous = by_claim_id[claim_id]
            if previous != record:
                raise InvalidRequestError(f"conflicting Claim evidence projections for Claim ID: {claim_id}")
            duplicate_count += 1
            continue
        by_claim_id[claim_id] = record
    canonical = tuple(by_claim_id[claim_id] for claim_id in sorted(by_claim_id))
    result = (canonical, duplicate_count)
    return result


def build_evidence_package(
    records: object,
    *,
    max_records: object = MAX_EVIDENCE_PACKAGE_RECORDS,
    max_bytes: object = MAX_EVIDENCE_PACKAGE_BYTES,
) -> EvidencePackage:
    """Canonicalize, deduplicate, and fit full-Claim evidence to configured bounds."""
    retained_limit = _require_int(max_records, "evidence package max_records", 0, MAX_EVIDENCE_PACKAGE_RECORDS)
    byte_limit = _require_int(max_bytes, "evidence package max_bytes", 256, MAX_EVIDENCE_PACKAGE_BYTES)
    canonical, duplicate_count = _canonical_claim_evidence_records(records)
    reasons: set[EvidencePackageTruncationReason] = set()
    omitted = duplicate_count
    if duplicate_count:
        reasons.add(EvidencePackageTruncationReason.DUPLICATE_CLAIM_ID)
    retained = canonical[:retained_limit]
    if len(canonical) > retained_limit:
        omitted += len(canonical) - retained_limit
        reasons.add(EvidencePackageTruncationReason.RECORD_LIMIT)

    while True:
        ordered_reasons = tuple(sorted(reasons, key=lambda reason: reason.value))
        candidate: EvidencePackage = {
            "wire_version": EVIDENCE_PACKAGE_WIRE_VERSION,
            "records": retained,
            "retained_count": len(retained),
            "omitted_count": omitted,
            "truncated": omitted > 0,
            "truncation_reasons": ordered_reasons,
        }
        payload = _evidence_package_payload(candidate)
        if len(_json_text(payload).encode("utf-8")) <= byte_limit:
            result = evidence_package(retained, len(retained), omitted, omitted > 0, ordered_reasons)
            break
        if not retained:
            raise InvalidRequestError("evidence package max_bytes cannot contain the empty package envelope")
        retained = retained[:-1]
        omitted += 1
        reasons.add(EvidencePackageTruncationReason.SERIALIZED_SIZE_LIMIT)
    return result


def evidence_package_to_dict(value: object) -> dict[str, object]:
    """Serialize one evidence package."""
    package = validate_evidence_package(value)
    result = _evidence_package_payload(package)
    return result


def evidence_package_from_dict(value: object) -> EvidencePackage:
    """Decode one evidence package from its exact serialized form."""
    data = _exact_mapping(value, "EvidencePackage", EVIDENCE_PACKAGE_FIELDS)
    records = _require_list(data["records"], "evidence package records")
    if len(records) > MAX_EVIDENCE_PACKAGE_RECORDS:
        raise InvalidRequestError(f"evidence package records exceeds the limit of {MAX_EVIDENCE_PACKAGE_RECORDS}")
    raw_reasons = _require_list(data["truncation_reasons"], "evidence package truncation_reasons")
    reasons = []
    for reason_value in raw_reasons:
        try:
            reason = EvidencePackageTruncationReason(
                _require_text(
                    reason_value,
                    "evidence package truncation reason",
                    MAX_REASON_CODE_BYTES,
                    allow_empty=False,
                )
            )
        except ValueError as error:
            raise InvalidRequestError("unsupported evidence package truncation reason") from error
        reasons.append(reason)
    decoded_records = tuple(
        claim_evidence_record_from_dict(_freeze_mapping(record, "evidence package record")) for record in records
    )
    result = evidence_package(
        decoded_records,
        data["retained_count"],
        data["omitted_count"],
        data["truncated"],
        tuple(reasons),
        data["wire_version"],
    )
    return result


def evidence_package_to_json(value: object) -> str:
    """Serialize one evidence package deterministically."""
    payload = evidence_package_to_dict(value)
    result = _json_text(payload)
    return result


def evidence_package_from_json(value: str) -> EvidencePackage:
    """Decode one bounded evidence package from deterministic JSON."""
    if not isinstance(value, str):
        raise InvalidRequestError("EvidencePackage JSON must be a string")
    if len(value.encode("utf-8")) > MAX_EVIDENCE_PACKAGE_BYTES:
        raise InvalidRequestError(f"evidence package exceeds the limit of {MAX_EVIDENCE_PACKAGE_BYTES} UTF-8 bytes")
    data = _load_json_mapping(value, "EvidencePackage JSON")
    result = evidence_package_from_dict(data)
    return result


EvidenceReference = TypedDict(
    "EvidenceReference",
    {
        "evidence_id": str,
        "resolver": str,
        "kind": EvidenceKind,
        "scope": ScopeKey,
        "provenance": Mapping[str, object],
        "diagnostics": Mapping[str, object],
        "schema_version": int,
    },
)


def evidence_reference(
    evidence_id: object,
    resolver: object,
    kind: object,
    scope: object,
    provenance: object = EMPTY_MAPPING,
    diagnostics: object = EMPTY_MAPPING,
    schema_version: object = EVIDENCE_REFERENCE_SCHEMA_VERSION,
) -> EvidenceReference:
    """Build one minimal stable evidence reference safe for Section 4 results."""
    version = _require_int(schema_version, "schema_version", 0, 2_147_483_647)
    if version != EVIDENCE_REFERENCE_SCHEMA_VERSION:
        raise InvalidRequestError(f"unsupported evidence reference schema_version: {version}")
    normalized_id = _require_text(evidence_id, "evidence_id", 256, allow_empty=False)
    normalized_resolver = _require_text(resolver, "evidence resolver", MAX_RESOLVER_NAME_BYTES, allow_empty=False)
    if not isinstance(kind, EvidenceKind):
        raise InvalidRequestError("evidence kind must be an EvidenceKind")
    try:
        validated_scope = validate_scope_key(scope)
    except IdentityValidationError as error:
        raise InvalidRequestError("evidence scope must be a ScopeKey") from error
    result: EvidenceReference = {
        "schema_version": version,
        "evidence_id": normalized_id,
        "resolver": normalized_resolver,
        "kind": kind,
        "scope": validated_scope,
        "provenance": _freeze_mapping(provenance, "evidence provenance"),
        "diagnostics": _freeze_mapping(diagnostics, "evidence diagnostics"),
    }
    return result


def validate_evidence_reference(value: object) -> EvidenceReference:
    """Revalidate and defensively copy one evidence-reference dictionary."""
    data = _exact_mapping(value, "EvidenceReference", EVIDENCE_REFERENCE_FIELDS)
    result = evidence_reference(
        data["evidence_id"],
        data["resolver"],
        data["kind"],
        data["scope"],
        data["provenance"],
        data["diagnostics"],
        data["schema_version"],
    )
    return result


def evidence_reference_with_changes(value: object, changes: object) -> EvidenceReference:
    reference = validate_evidence_reference(value)
    if not isinstance(changes, Mapping):
        raise InvalidRequestError("evidence reference changes must be an object")
    if not frozenset(changes).issubset(EVIDENCE_REFERENCE_FIELDS):
        raise InvalidRequestError("evidence reference changes contain an unknown field")
    updated: dict[str, object] = dict(reference)
    updated.update(changes)
    result = validate_evidence_reference(updated)
    return result


def evidence_reference_to_dict(value: object) -> dict[str, object]:
    """Serialize one evidence reference."""
    reference = validate_evidence_reference(value)
    result = {
        "schema_version": reference["schema_version"],
        "evidence_id": reference["evidence_id"],
        "resolver": reference["resolver"],
        "kind": reference["kind"].value,
        "scope": scope_key_to_dict(reference["scope"]),
        "provenance": _thaw_json(reference["provenance"]),
        "diagnostics": _thaw_json(reference["diagnostics"]),
    }
    return result


def evidence_reference_from_dict(value: object) -> EvidenceReference:
    """Decode one evidence reference from its exact serialized form."""
    data = _exact_mapping(value, "EvidenceReference", EVIDENCE_REFERENCE_FIELDS)
    try:
        kind = EvidenceKind(_require_text(data["kind"], "evidence kind", 32, allow_empty=False))
    except ValueError as error:
        raise InvalidRequestError("unsupported evidence kind") from error
    result = evidence_reference(
        data["evidence_id"],
        data["resolver"],
        kind,
        scope_key_from_dict(_freeze_mapping(data["scope"], "evidence scope")),
        _freeze_mapping(data["provenance"], "evidence provenance"),
        _freeze_mapping(data["diagnostics"], "evidence diagnostics"),
        data["schema_version"],
    )
    return result


def evidence_reference_to_json(value: object) -> str:
    payload = evidence_reference_to_dict(value)
    result = _json_text(payload)
    return result


def evidence_reference_from_json(value: str) -> EvidenceReference:
    data = _load_json_mapping(value, "EvidenceReference JSON")
    result = evidence_reference_from_dict(data)
    return result


Candidate = TypedDict(
    "Candidate",
    {
        "schema_version": int,
        "candidate_id": str,
        "statement_id": str,
        "response": str,
        "source": CandidateSource,
        "features": FeatureSet,
        "evidence": tuple[EvidenceReference, ...],
        "scope": ScopeKey,
        "lifecycle": LifecycleState,
        "provenance": Mapping[str, object],
        "diagnostics": Mapping[str, object],
    },
)


def candidate(
    candidate_id: object,
    statement_id: object,
    response: object,
    source: object,
    features: object,
    evidence: object,
    scope: object,
    lifecycle: object,
    provenance: object = EMPTY_MAPPING,
    diagnostics: object = EMPTY_MAPPING,
    schema_version: object = CANDIDATE_SCHEMA_VERSION,
) -> Candidate:
    """Build one response candidate emitted by a resolver without selecting it."""
    version = _require_int(schema_version, "schema_version", 0, MAX_RESOURCE_COUNTER)
    if version != CANDIDATE_SCHEMA_VERSION:
        raise InvalidRequestError(f"unsupported candidate schema_version: {version}")
    if not isinstance(source, CandidateSource):
        raise InvalidRequestError("candidate source must be a CandidateSource")
    try:
        validated_features = validate_feature_set(features)
    except InvalidRequestError as error:
        raise InvalidRequestError("candidate features must be a FeatureSet") from error
    if not isinstance(evidence, tuple):
        raise InvalidRequestError("candidate evidence must be a tuple of EvidenceReference values")
    try:
        validated_evidence = tuple(validate_evidence_reference(item) for item in evidence)
    except InvalidRequestError as error:
        raise InvalidRequestError("candidate evidence must be a tuple of EvidenceReference values") from error
    if len(validated_evidence) > MAX_RESOLUTION_VALUES:
        raise InvalidRequestError(f"candidate evidence exceeds the limit of {MAX_RESOLUTION_VALUES}")
    try:
        validated_scope = validate_scope_key(scope)
    except IdentityValidationError as error:
        raise InvalidRequestError("candidate scope must be a ScopeKey") from error
    if not isinstance(lifecycle, LifecycleState):
        raise InvalidRequestError("candidate lifecycle must be a LifecycleState")
    result: Candidate = {
        "schema_version": version,
        "candidate_id": _require_text(candidate_id, "candidate_id", MAX_CANDIDATE_ID_BYTES, allow_empty=False),
        "statement_id": _require_text(
            statement_id,
            "candidate statement_id",
            MAX_STATEMENT_ID_BYTES,
            allow_empty=False,
        ),
        "response": _require_text(response, "candidate response", MAX_RESPONSE_BYTES, allow_empty=False),
        "source": source,
        "features": validated_features,
        "evidence": validated_evidence,
        "scope": validated_scope,
        "lifecycle": lifecycle,
        "provenance": _freeze_mapping(provenance, "candidate provenance"),
        "diagnostics": _freeze_mapping(diagnostics, "candidate diagnostics"),
    }
    return result


def validate_candidate(value: object) -> Candidate:
    data = _exact_mapping(value, "Candidate", CANDIDATE_FIELDS)
    result = candidate(
        candidate_id=data["candidate_id"],
        statement_id=data["statement_id"],
        response=data["response"],
        source=data["source"],
        features=data["features"],
        evidence=data["evidence"],
        scope=data["scope"],
        lifecycle=data["lifecycle"],
        provenance=data["provenance"],
        diagnostics=data["diagnostics"],
        schema_version=data["schema_version"],
    )
    return result


def candidate_with_changes(value: object, changes: object) -> Candidate:
    current = validate_candidate(value)
    if not isinstance(changes, Mapping):
        raise InvalidRequestError("candidate changes must be an object")
    if not frozenset(changes).issubset(CANDIDATE_FIELDS):
        raise InvalidRequestError("candidate changes contain an unknown field")
    updated: dict[str, object] = dict(current)
    updated.update(changes)
    result = validate_candidate(updated)
    return result


def candidate_to_dict(value: object) -> dict[str, object]:
    current = validate_candidate(value)
    result = {
        "schema_version": current["schema_version"],
        "candidate_id": current["candidate_id"],
        "statement_id": current["statement_id"],
        "response": current["response"],
        "source": current["source"].value,
        "features": feature_set_to_dict(current["features"]),
        "evidence": [evidence_reference_to_dict(item) for item in current["evidence"]],
        "scope": scope_key_to_dict(current["scope"]),
        "lifecycle": current["lifecycle"].value,
        "provenance": _thaw_json(current["provenance"]),
        "diagnostics": _thaw_json(current["diagnostics"]),
    }
    return result


def candidate_from_dict(value: object) -> Candidate:
    data = _exact_mapping(value, "Candidate", CANDIDATE_FIELDS)
    try:
        source = CandidateSource(_require_text(data["source"], "candidate source", 32, allow_empty=False))
        lifecycle = LifecycleState(_require_text(data["lifecycle"], "candidate lifecycle", 32, allow_empty=False))
    except ValueError as error:
        raise InvalidRequestError("candidate source or lifecycle is unsupported") from error
    evidence = _require_list(data["evidence"], "candidate evidence")
    result = candidate(
        schema_version=data["schema_version"],
        candidate_id=data["candidate_id"],
        statement_id=data["statement_id"],
        response=data["response"],
        source=source,
        features=feature_set_from_dict(_freeze_mapping(data["features"], "candidate features")),
        evidence=tuple(evidence_reference_from_dict(_freeze_mapping(item, "candidate evidence item")) for item in evidence),
        scope=scope_key_from_dict(_freeze_mapping(data["scope"], "candidate scope")),
        lifecycle=lifecycle,
        provenance=_freeze_mapping(data["provenance"], "candidate provenance"),
        diagnostics=_freeze_mapping(data["diagnostics"], "candidate diagnostics"),
    )
    return result


def candidate_to_json(value: object) -> str:
    payload = candidate_to_dict(value)
    result = _json_text(payload)
    return result


def candidate_from_json(value: str) -> Candidate:
    data = _load_json_mapping(value, "Candidate JSON")
    result = candidate_from_dict(data)
    return result


def empty_candidate() -> Candidate:
    result = candidate(
        candidate_id="empty",
        statement_id="empty",
        response="empty",
        source=CandidateSource.EXACT,
        features=feature_set(),
        evidence=(),
        scope=EMPTY_SCOPE_KEY,
        lifecycle=LifecycleState.ACTIVE,
    )
    return result


AccountingObservation = TypedDict(
    "AccountingObservation",
    {
        "schema_version": int,
        "statement_id": str,
        "keywords": tuple[str, ...],
    },
)


def accounting_observation(
    statement_id: object,
    keywords: object = (),
    schema_version: object = ACCOUNTING_OBSERVATION_SCHEMA_VERSION,
) -> AccountingObservation:
    """Build one pure resolver observation applied only by the finalizer."""
    version = _require_int(schema_version, "schema_version", 0, MAX_RESOURCE_COUNTER)
    if version != ACCOUNTING_OBSERVATION_SCHEMA_VERSION:
        raise InvalidRequestError(f"unsupported accounting observation schema_version: {version}")
    if not isinstance(keywords, tuple):
        raise InvalidRequestError("accounting keywords must be a tuple")
    if len(keywords) > MAX_ACCOUNTING_KEYWORDS:
        raise InvalidRequestError(f"accounting keywords exceeds the limit of {MAX_ACCOUNTING_KEYWORDS}")
    normalized_keywords = tuple(
        _require_text(keyword, "accounting keyword", MAX_ACCOUNTING_KEYWORD_BYTES, allow_empty=False) for keyword in keywords
    )
    if len(set(normalized_keywords)) != len(normalized_keywords):
        raise InvalidRequestError("accounting keywords must be unique")
    result: AccountingObservation = {
        "schema_version": version,
        "statement_id": _require_text(
            statement_id,
            "accounting statement_id",
            MAX_STATEMENT_ID_BYTES,
            allow_empty=False,
        ),
        "keywords": normalized_keywords,
    }
    return result


def validate_accounting_observation(value: object) -> AccountingObservation:
    data = _exact_mapping(value, "AccountingObservation", ACCOUNTING_OBSERVATION_FIELDS)
    result = accounting_observation(data["statement_id"], data["keywords"], data["schema_version"])
    return result


def accounting_observation_with_changes(value: object, changes: object) -> AccountingObservation:
    observation = validate_accounting_observation(value)
    if not isinstance(changes, Mapping):
        raise InvalidRequestError("accounting observation changes must be an object")
    if not frozenset(changes).issubset(ACCOUNTING_OBSERVATION_FIELDS):
        raise InvalidRequestError("accounting observation changes contain an unknown field")
    updated: dict[str, object] = dict(observation)
    updated.update(changes)
    result = validate_accounting_observation(updated)
    return result


def accounting_observation_to_dict(value: object) -> dict[str, object]:
    observation = validate_accounting_observation(value)
    result = {
        "schema_version": observation["schema_version"],
        "statement_id": observation["statement_id"],
        "keywords": list(observation["keywords"]),
    }
    return result


def accounting_observation_from_dict(value: object) -> AccountingObservation:
    data = _exact_mapping(value, "AccountingObservation", ACCOUNTING_OBSERVATION_FIELDS)
    keywords = _require_list(data["keywords"], "accounting keywords")
    result = accounting_observation(data["statement_id"], tuple(keywords), data["schema_version"])
    return result


ResolverResult = TypedDict(
    "ResolverResult",
    {
        "resolver": str,
        "state": ResolverState,
        "reason_code": str,
        "candidates": tuple[Candidate, ...],
        "evidence": tuple[EvidenceReference, ...],
        "claim_evidence": tuple[ClaimEvidenceRecord, ...],
        "accounting": tuple[AccountingObservation, ...],
        "diagnostics": Mapping[str, object],
        "consumption": BudgetConsumption,
        "schema_version": int,
    },
)


def resolver_result(
    resolver: object,
    state: object,
    reason_code: object = "",
    candidates: object = (),
    evidence: object = (),
    claim_evidence: object = (),
    accounting: object = (),
    diagnostics: object = EMPTY_MAPPING,
    consumption: object = EMPTY_MAPPING,
    schema_version: object = RESOLVER_RESULT_SCHEMA_VERSION,
) -> ResolverResult:
    """Build the bounded output from one side-effect-free resolver invocation."""
    validated_schema_version = _require_int(schema_version, "schema_version", 0, MAX_RESOURCE_COUNTER)
    if validated_schema_version != RESOLVER_RESULT_SCHEMA_VERSION:
        raise InvalidRequestError(f"unsupported resolver result schema_version: {validated_schema_version}")
    resolver_name = _require_text(resolver, "resolver result resolver", MAX_RESOLVER_NAME_BYTES, allow_empty=False)
    if not isinstance(state, ResolverState):
        raise InvalidRequestError("resolver result state must be a ResolverState")
    validated_reason_code = _require_text(
        reason_code,
        "resolver result reason_code",
        MAX_REASON_CODE_BYTES,
        allow_empty=True,
    )
    if (
        not isinstance(candidates, tuple)
        or not isinstance(evidence, tuple)
        or not isinstance(claim_evidence, tuple)
        or not isinstance(accounting, tuple)
    ):
        raise InvalidRequestError("resolver outputs and accounting must be tuples")
    try:
        validated_candidates = tuple(validate_candidate(value) for value in candidates)
    except InvalidRequestError as error:
        raise InvalidRequestError("resolver candidates must be a tuple of Candidate values") from error
    try:
        validated_evidence = tuple(validate_evidence_reference(value) for value in evidence)
    except InvalidRequestError as error:
        raise InvalidRequestError("resolver evidence must be a tuple of EvidenceReference values") from error
    try:
        validated_claim_evidence = tuple(validate_claim_evidence_record(value) for value in claim_evidence)
    except InvalidRequestError as error:
        raise InvalidRequestError("resolver claim_evidence must be a tuple of ClaimEvidenceRecord values") from error
    try:
        validated_accounting = tuple(validate_accounting_observation(value) for value in accounting)
    except InvalidRequestError as error:
        raise InvalidRequestError("resolver accounting must be a tuple of AccountingObservation values") from error
    if state != ResolverState.COMPLETED and (
        validated_candidates or validated_evidence or validated_claim_evidence or validated_accounting
    ):
        raise InvalidRequestError("non-completed resolver results cannot contain output or accounting")
    if any(
        value["source_resolver"] != resolver_name or value["source_contributions"] != (resolver_name,)
        for value in validated_claim_evidence
    ):
        raise InvalidRequestError("resolver claim_evidence source must match its producing resolver")
    if (
        max(
            len(validated_candidates),
            len(validated_evidence),
            len(validated_claim_evidence),
            len(validated_accounting),
        )
        > MAX_RESOLUTION_VALUES
    ):
        raise InvalidRequestError(f"resolver output exceeds the item limit of {MAX_RESOLUTION_VALUES}")
    frozen_diagnostics = _freeze_mapping(diagnostics, "resolver diagnostics")
    if not isinstance(consumption, Mapping):
        raise InvalidRequestError("resolver consumption must be a BudgetConsumption")
    try:
        validated_consumption = budget_consumption() if not consumption else validate_budget_consumption(consumption)
    except InvalidRequestError as error:
        raise InvalidRequestError("resolver consumption must be a BudgetConsumption") from error
    result: ResolverResult = {
        "resolver": resolver_name,
        "state": state,
        "reason_code": validated_reason_code,
        "candidates": validated_candidates,
        "evidence": validated_evidence,
        "claim_evidence": validated_claim_evidence,
        "accounting": validated_accounting,
        "diagnostics": frozen_diagnostics,
        "consumption": validated_consumption,
        "schema_version": validated_schema_version,
    }
    return result


def validate_resolver_result(value: object) -> ResolverResult:
    data = _exact_mapping(value, "ResolverResult", RESOLVER_RESULT_FIELDS)
    result = resolver_result(
        data["resolver"],
        data["state"],
        data["reason_code"],
        data["candidates"],
        data["evidence"],
        data["claim_evidence"],
        data["accounting"],
        data["diagnostics"],
        data["consumption"],
        data["schema_version"],
    )
    return result


def resolver_result_with_changes(value: object, changes: object) -> ResolverResult:
    current = validate_resolver_result(value)
    if not isinstance(changes, Mapping) or not frozenset(changes).issubset(RESOLVER_RESULT_FIELDS):
        raise InvalidRequestError("resolver result changes contain invalid fields")
    updated: dict[str, object] = dict(current)
    updated.update(changes)
    result = validate_resolver_result(updated)
    return result


def resolver_result_to_dict(value: object) -> dict[str, object]:
    current = validate_resolver_result(value)
    result = {
        "schema_version": current["schema_version"],
        "resolver": current["resolver"],
        "state": current["state"].value,
        "reason_code": current["reason_code"],
        "candidates": [candidate_to_dict(item) for item in current["candidates"]],
        "evidence": [evidence_reference_to_dict(item) for item in current["evidence"]],
        "claim_evidence": [claim_evidence_record_to_dict(item) for item in current["claim_evidence"]],
        "accounting": [accounting_observation_to_dict(item) for item in current["accounting"]],
        "diagnostics": _thaw_json(current["diagnostics"]),
        "consumption": budget_consumption_to_dict(current["consumption"]),
    }
    return result


def resolver_result_to_json(value: object) -> str:
    payload = resolver_result_to_dict(value)
    result = _json_text(payload)
    return result


def resolver_result_from_dict(value: object) -> ResolverResult:
    data = _exact_mapping(value, "ResolverResult", RESOLVER_RESULT_FIELDS)
    try:
        state = ResolverState(_require_text(data["state"], "resolver state", 32, allow_empty=False))
    except ValueError as error:
        raise InvalidRequestError("unsupported resolver state") from error
    candidates = _require_list(data["candidates"], "resolver candidates")
    evidence = _require_list(data["evidence"], "resolver evidence")
    claim_evidence = _require_list(data["claim_evidence"], "resolver Claim evidence")
    accounting = _require_list(data["accounting"], "resolver accounting")
    result = resolver_result(
        schema_version=_require_int(data["schema_version"], "schema_version", 1, 1),
        resolver=_require_text(data["resolver"], "resolver result resolver", MAX_RESOLVER_NAME_BYTES, allow_empty=False),
        state=state,
        reason_code=_require_text(data["reason_code"], "resolver result reason_code", MAX_REASON_CODE_BYTES, allow_empty=True),
        candidates=tuple(candidate_from_dict(_freeze_mapping(item, "resolver candidate")) for item in candidates),
        evidence=tuple(evidence_reference_from_dict(_freeze_mapping(item, "resolver evidence item")) for item in evidence),
        claim_evidence=tuple(
            claim_evidence_record_from_dict(_freeze_mapping(item, "resolver Claim evidence item")) for item in claim_evidence
        ),
        accounting=tuple(
            accounting_observation_from_dict(_freeze_mapping(item, "resolver accounting item")) for item in accounting
        ),
        diagnostics=_freeze_mapping(data["diagnostics"], "resolver diagnostics"),
        consumption=budget_consumption_from_dict(_freeze_mapping(data["consumption"], "resolver consumption")),
    )
    return result


def resolver_result_from_json(value: str) -> ResolverResult:
    decoded = _load_json_mapping(value, "ResolverResult JSON")
    result = resolver_result_from_dict(decoded)
    return result


ResolutionResult = TypedDict(
    "ResolutionResult",
    {
        "outcome": ResolutionOutcome,
        "selected_candidate": Candidate,
        "selected_candidate_available": bool,
        "response_candidates": tuple[Candidate, ...],
        "evidence": tuple[EvidenceReference, ...],
        "confidence": float,
        "confidence_available": bool,
        "reason_codes": tuple[str, ...],
        "frame_diagnostics": Mapping[str, object],
        "resolver_results": tuple[ResolverResult, ...],
        "budget": BudgetConsumption,
        "evidence_package_available": bool,
        "evidence_package": EvidencePackage,
        "schema_version": int,
    },
)


def resolution_result(
    outcome: object,
    selected_candidate: object,
    selected_candidate_available: object,
    response_candidates: object,
    evidence: object,
    confidence: object,
    confidence_available: object,
    reason_codes: object,
    frame_diagnostics: object,
    resolver_results: object,
    budget: object,
    evidence_package_available: object = False,
    evidence_package: object = EMPTY_MAPPING,
    schema_version: object = RESOLUTION_RESULT_SCHEMA_VERSION,
) -> ResolutionResult:
    """Build a strict unified result with concrete ANSWER/EVIDENCE/MISS invariants."""
    validated_schema_version = _require_int(schema_version, "schema_version", 0, MAX_RESOURCE_COUNTER)
    if validated_schema_version != RESOLUTION_RESULT_SCHEMA_VERSION:
        raise InvalidRequestError(f"unsupported resolution result schema_version: {validated_schema_version}")
    if not isinstance(outcome, ResolutionOutcome):
        raise InvalidRequestError("resolution outcome must be a ResolutionOutcome")
    try:
        validated_selected_candidate = validate_candidate(selected_candidate)
    except InvalidRequestError as error:
        raise InvalidRequestError("selected_candidate must be a Candidate") from error
    if (
        not isinstance(selected_candidate_available, bool)
        or not isinstance(confidence_available, bool)
        or not isinstance(evidence_package_available, bool)
    ):
        raise InvalidRequestError("resolution availability fields must be booleans")
    if not isinstance(response_candidates, tuple):
        raise InvalidRequestError("response_candidates must be a tuple of Candidate values")
    try:
        validated_response_candidates = tuple(validate_candidate(value) for value in response_candidates)
    except InvalidRequestError as error:
        raise InvalidRequestError("response_candidates must be a tuple of Candidate values") from error
    if not isinstance(evidence, tuple):
        raise InvalidRequestError("resolution evidence must be a tuple of EvidenceReference values")
    try:
        validated_evidence = tuple(validate_evidence_reference(value) for value in evidence)
    except InvalidRequestError as error:
        raise InvalidRequestError("resolution evidence must be a tuple of EvidenceReference values") from error
    validated_confidence = _require_float(confidence, "resolution confidence", 0.0, 1.0)
    if not isinstance(reason_codes, tuple):
        raise InvalidRequestError("reason_codes must be a tuple")
    if len(reason_codes) > MAX_RESOLUTION_REASON_CODES:
        raise InvalidRequestError(f"reason_codes exceeds the limit of {MAX_RESOLUTION_REASON_CODES}")
    validated_reason_codes = tuple(
        _require_text(value, "reason code", MAX_REASON_CODE_BYTES, allow_empty=False) for value in reason_codes
    )
    if validated_reason_codes != tuple(dict.fromkeys(validated_reason_codes)):
        raise InvalidRequestError("reason_codes must be unique and ordered")
    frozen_diagnostics = _freeze_mapping(frame_diagnostics, "frame diagnostics")
    if not isinstance(resolver_results, tuple):
        raise InvalidRequestError("resolver_results must be a tuple of ResolverResult values")
    try:
        validated_resolver_results = tuple(validate_resolver_result(value) for value in resolver_results)
    except InvalidRequestError as error:
        raise InvalidRequestError("resolver_results must be a tuple of ResolverResult values") from error
    if len(validated_response_candidates) > MAX_RESOLUTION_VALUES or len(validated_evidence) > MAX_RESOLUTION_VALUES:
        raise InvalidRequestError(f"resolution output exceeds the item limit of {MAX_RESOLUTION_VALUES}")
    if len(validated_resolver_results) > MAX_RESOLUTION_REASON_CODES:
        raise InvalidRequestError(f"resolver_results exceeds the limit of {MAX_RESOLUTION_REASON_CODES}")
    if any(value["claim_evidence"] for value in validated_resolver_results):
        raise InvalidRequestError("resolution resolver_results cannot expose unpackaged Claim evidence")
    try:
        validated_budget = validate_budget_consumption(budget)
    except InvalidRequestError as error:
        raise InvalidRequestError("resolution budget must be a BudgetConsumption") from error
    if not isinstance(evidence_package, Mapping):
        raise InvalidRequestError("evidence_package must be an EvidencePackage")
    try:
        validated_evidence_package = (
            empty_evidence_package() if not evidence_package else validate_evidence_package(evidence_package)
        )
    except InvalidRequestError as error:
        raise InvalidRequestError("evidence_package must be an EvidencePackage") from error
    concrete_empty_package = empty_evidence_package()
    if not evidence_package_available and validated_evidence_package != concrete_empty_package:
        raise InvalidRequestError("unavailable evidence_package must use the concrete empty package")
    if outcome == ResolutionOutcome.ANSWER:
        if not selected_candidate_available:
            raise InvalidRequestError("ANSWER requires one selected candidate")
        if validated_response_candidates != (validated_selected_candidate,):
            raise InvalidRequestError("ANSWER response_candidates must contain only the selected candidate")
        if validated_evidence:
            raise InvalidRequestError("ANSWER cannot contain top-level evidence")
        if not confidence_available or validated_confidence <= 0.0:
            raise InvalidRequestError("ANSWER requires available positive confidence")
        if evidence_package_available or validated_evidence_package != concrete_empty_package:
            raise InvalidRequestError("ANSWER cannot contain a response-less evidence package")
    else:
        if selected_candidate_available:
            raise InvalidRequestError("only ANSWER can make selected_candidate available")
        if validated_selected_candidate != empty_candidate():
            raise InvalidRequestError("an unavailable selected_candidate must be the concrete empty candidate")
        if confidence_available or validated_confidence != 0.0:
            raise InvalidRequestError("non-ANSWER confidence must be unavailable and zero")
    retained_evidence_available = bool(validated_response_candidates or validated_evidence or validated_evidence_package["records"])
    if outcome == ResolutionOutcome.EVIDENCE and not retained_evidence_available:
        raise InvalidRequestError("EVIDENCE requires response candidates, evidence references, or package records")
    if outcome == ResolutionOutcome.MISS and retained_evidence_available:
        raise InvalidRequestError("MISS cannot contain response candidates or retained evidence")
    result: ResolutionResult = {
        "outcome": outcome,
        "selected_candidate": validated_selected_candidate,
        "selected_candidate_available": selected_candidate_available,
        "response_candidates": validated_response_candidates,
        "evidence": validated_evidence,
        "confidence": validated_confidence,
        "confidence_available": confidence_available,
        "reason_codes": validated_reason_codes,
        "frame_diagnostics": frozen_diagnostics,
        "resolver_results": validated_resolver_results,
        "budget": validated_budget,
        "evidence_package_available": evidence_package_available,
        "evidence_package": validated_evidence_package,
        "schema_version": validated_schema_version,
    }
    return result


def validate_resolution_result(value: object) -> ResolutionResult:
    data = _exact_mapping(value, "ResolutionResult", RESOLUTION_RESULT_FIELDS)
    result = resolution_result(
        data["outcome"],
        data["selected_candidate"],
        data["selected_candidate_available"],
        data["response_candidates"],
        data["evidence"],
        data["confidence"],
        data["confidence_available"],
        data["reason_codes"],
        data["frame_diagnostics"],
        data["resolver_results"],
        data["budget"],
        data["evidence_package_available"],
        data["evidence_package"],
        data["schema_version"],
    )
    return result


def resolution_result_with_changes(value: object, changes: object) -> ResolutionResult:
    current = validate_resolution_result(value)
    if not isinstance(changes, Mapping) or not frozenset(changes).issubset(RESOLUTION_RESULT_FIELDS):
        raise InvalidRequestError("resolution result changes contain invalid fields")
    updated: dict[str, object] = dict(current)
    updated.update(changes)
    result = validate_resolution_result(updated)
    return result


def resolution_result_to_dict(value: object) -> dict[str, object]:
    current = validate_resolution_result(value)
    selected = candidate_to_dict(current["selected_candidate"]) if current["selected_candidate_available"] else {}
    result = {
        "schema_version": current["schema_version"],
        "outcome": current["outcome"].value,
        "selected_candidate": selected,
        "selected_candidate_available": current["selected_candidate_available"],
        "response_candidates": [candidate_to_dict(item) for item in current["response_candidates"]],
        "evidence": [evidence_reference_to_dict(item) for item in current["evidence"]],
        "confidence": current["confidence"],
        "confidence_available": current["confidence_available"],
        "reason_codes": list(current["reason_codes"]),
        "frame_diagnostics": _thaw_json(current["frame_diagnostics"]),
        "resolver_results": [resolver_result_to_dict(item) for item in current["resolver_results"]],
        "budget": budget_consumption_to_dict(current["budget"]),
        "evidence_package_available": current["evidence_package_available"],
        "evidence_package": evidence_package_to_dict(current["evidence_package"]),
    }
    return result


def resolution_result_to_json(value: object) -> str:
    payload = resolution_result_to_dict(value)
    result = _json_text(payload)
    return result


def resolution_result_from_dict(value: object) -> ResolutionResult:
    data = _exact_mapping(value, "ResolutionResult", RESOLUTION_RESULT_FIELDS)
    try:
        outcome = ResolutionOutcome(_require_text(data["outcome"], "resolution outcome", 32, allow_empty=False))
    except ValueError as error:
        raise InvalidRequestError("unsupported resolution outcome") from error
    if not isinstance(data["selected_candidate_available"], bool):
        raise InvalidRequestError("selected_candidate_available must be a boolean")
    selected_available = data["selected_candidate_available"]
    selected_mapping = _freeze_mapping(data["selected_candidate"], "selected_candidate")
    selected = candidate_from_dict(selected_mapping) if selected_available else empty_candidate()
    if not selected_available and selected_mapping:
        raise InvalidRequestError("unavailable selected_candidate must be an empty object")
    if not isinstance(data["confidence_available"], bool):
        raise InvalidRequestError("confidence_available must be a boolean")
    response_candidates = _require_list(data["response_candidates"], "response_candidates")
    evidence = _require_list(data["evidence"], "resolution evidence")
    reasons = _require_list(data["reason_codes"], "reason_codes")
    resolver_results = _require_list(data["resolver_results"], "resolver_results")
    if not isinstance(data["evidence_package_available"], bool):
        raise InvalidRequestError("evidence_package_available must be a boolean")
    result = resolution_result(
        schema_version=_require_int(data["schema_version"], "schema_version", 1, 1),
        outcome=outcome,
        selected_candidate=selected,
        selected_candidate_available=selected_available,
        response_candidates=tuple(candidate_from_dict(_freeze_mapping(item, "response candidate")) for item in response_candidates),
        evidence=tuple(evidence_reference_from_dict(_freeze_mapping(item, "resolution evidence item")) for item in evidence),
        confidence=_require_float(data["confidence"], "resolution confidence", 0.0, 1.0),
        confidence_available=data["confidence_available"],
        reason_codes=tuple(_require_text(item, "reason code", MAX_REASON_CODE_BYTES, allow_empty=False) for item in reasons),
        frame_diagnostics=_freeze_mapping(data["frame_diagnostics"], "frame diagnostics"),
        resolver_results=tuple(resolver_result_from_dict(_freeze_mapping(item, "resolver result")) for item in resolver_results),
        budget=budget_consumption_from_dict(_freeze_mapping(data["budget"], "resolution budget")),
        evidence_package_available=data["evidence_package_available"],
        evidence_package=evidence_package_from_dict(_freeze_mapping(data["evidence_package"], "evidence package")),
    )
    return result


def resolution_result_from_json(value: str) -> ResolutionResult:
    decoded = _load_json_mapping(value, "ResolutionResult JSON")
    result = resolution_result_from_dict(decoded)
    return result


class QueryFrameBuilder:
    """Trusted transport-neutral base-frame construction boundary."""

    def __init__(self, engram, monotonic_clock_ns: Callable[[], int], utc_clock: Callable[[], datetime]) -> None:
        if not callable(monotonic_clock_ns) or not callable(utc_clock):
            raise InvalidRequestError("frame builder clocks must be callable")
        self._engram = engram
        self._monotonic_clock_ns = monotonic_clock_ns
        self._utc_clock = utc_clock

    def build(
        self,
        request: str,
        scope: object = EMPTY_SCOPE_KEY,
        identity=(),
        required_metadata: Mapping[str, object] = MappingProxyType({}),
        required_source_label: str = "",
        diagnostic_seed: str = "",
        budget=(),
    ) -> QueryFrame:
        original = _require_text(request, "request", MAX_REQUEST_BYTES, allow_empty=False)
        try:
            scope = validate_scope_key(scope)
        except IdentityValidationError as error:
            raise InvalidRequestError("scope must be a ScopeKey") from error
        if identity:
            try:
                selected_identity = validate_query_identity(identity)
            except IdentityValidationError as error:
                raise InvalidRequestError("identity must be a QueryIdentity") from error
            validate_authoritative_identity(selected_identity, build_retrieval_representation(original))
        else:
            selected_identity = build_standalone_identity(original, scope)
        if selected_identity["scope"] != scope:
            raise InvalidRequestError("identity scope must match frame scope")
        if budget:
            try:
                selected_budget = recapture_resolution_budget(budget, self._monotonic_clock_ns)
            except InvalidRequestError as error:
                raise InvalidRequestError("budget must be a ResolutionBudget") from error
        else:
            selected_budget = capture_resolution_budget(self._monotonic_clock_ns)
        resolved = original
        if self._engram.config["expand_contractions"]:
            resolved = expand_contractions(original, self._engram.substitution_maps["contractions"])
        eligibility = EligibilityContextFactory(self._utc_clock, self._engram.namespace_epochs).capture_standalone(scope, True)
        seed = diagnostic_seed or f"{query_identity_to_json(selected_identity)}:{resolved}"
        _require_text(seed, "diagnostic_seed", MAX_REQUEST_BYTES * 4, allow_empty=False)
        diagnostic_id = f"resolution:sha256:{hashlib.sha256(seed.encode('utf-8')).hexdigest()}"
        result = query_frame(
            original_text=original,
            resolved_text=resolved,
            identity=selected_identity,
            expected_object_type=ExpectedObjectType.UNKNOWN,
            inheritance=(),
            rewrite_chain=(),
            scope=scope,
            required_metadata=required_metadata,
            required_source_label=required_source_label,
            budget=selected_budget,
            eligibility_context=eligibility,
            diagnostic_id=diagnostic_id,
        )
        return result


class BudgetLedger:
    """Thread-safe deterministic aggregate consumption and allowance checker."""

    def __init__(self, budget: ResolutionBudget, clock_ns: Callable[[], int]) -> None:
        try:
            validated_budget = validate_resolution_budget(budget)
        except InvalidRequestError as error:
            raise InvalidRequestError("ledger budget must be a ResolutionBudget") from error
        if not callable(clock_ns):
            raise InvalidRequestError("ledger clock_ns must be callable")
        self._budget = validated_budget
        self._clock_ns = clock_ns
        self._lock = threading.RLock()
        self._totals = budget_consumption()

    @property
    def budget(self) -> ResolutionBudget:
        result = validate_resolution_budget(self._budget)
        return result

    def deadline_exhausted(self) -> bool:
        result = bool(self._budget["deadline_ns"] and self._clock_ns() >= self._budget["deadline_ns"])
        return result

    def remaining_candidates(self) -> int:
        with self._lock:
            result = max(0, self._budget["max_candidates"] - self._totals["candidates"])
            return result

    def remaining_evidence(self) -> int:
        with self._lock:
            result = max(0, self._budget["max_evidence"] - self._totals["evidence"])
            return result

    def add(self, consumption: BudgetConsumption) -> BudgetConsumption:
        try:
            validated_consumption = validate_budget_consumption(consumption)
        except InvalidRequestError as error:
            raise InvalidRequestError("ledger consumption must be a BudgetConsumption") from error
        with self._lock:
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
                values[name] = self._totals[name] + validated_consumption[name]
            exhausted = set(self._totals["exhausted_dimensions"]).union(validated_consumption["exhausted_dimensions"])
            limits = {
                "resolvers": self._budget["max_resolvers"],
                "candidates": self._budget["max_candidates"],
                "graph_rows": self._budget["max_graph_rows"],
                "vector_results": self._budget["max_vector_results"],
                "evidence": self._budget["max_evidence"],
                "evidence_bytes": self._budget["max_evidence_bytes"],
                "output_bytes": self._budget["max_output_bytes"],
                "diagnostic_bytes": self._budget["max_diagnostic_bytes"],
                "working_memory_bytes": self._budget["max_working_memory_bytes"],
            }
            for name, limit in limits.items():
                if values[name] > limit:
                    exhausted.add(name)
            if self.deadline_exhausted():
                exhausted.add("total_time")
            values["exhausted_dimensions"] = tuple(sorted(exhausted))
            values["measurement_available"] = (
                self._totals["measurement_available"] and validated_consumption["measurement_available"]
            )
            self._totals = budget_consumption(**values)
            result = validate_budget_consumption(self._totals)
            return result

    def snapshot(self) -> BudgetConsumption:
        with self._lock:
            result = validate_budget_consumption(self._totals)
            return result
