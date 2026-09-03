"""Transport-neutral contracts for the bounded unified resolution pipeline."""

from datetime import datetime
from hashlib import sha256 as hashlib_sha256
from json import JSONDecodeError as json_JSONDecodeError, dumps as json_dumps, loads as json_loads
from math import isfinite as math_isfinite
from threading import RLock as threading_RLock

from engram.artifacts import LifecycleState
from engram.constants import (
    ACCOUNTING_OBSERVATION_FIELDS,
    ACCOUNTING_OBSERVATION_SCHEMA_VERSION,
    BUDGET_CONSUMPTION_FIELDS,
    BUDGET_CONSUMPTION_SCHEMA_VERSION,
    CANDIDATE_FIELDS,
    CANDIDATE_SCHEMA_VERSION,
    CANONICAL_PROPOSITION_REFERENCES_FIELDS,
    CANONICAL_PROPOSITION_REFERENCES_SCHEMA_VERSION,
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
    MAX_COMPOSITION_BINDING_BYTES,
    MAX_COMPOSITION_PATH_PROPOSITIONS,
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
    MAX_PROPOSITION_IDENTIFIER_BYTES,
    MAX_PROPOSITION_SELECTION_REASONS,
    MAX_PROPOSITION_SOURCE_CONTRIBUTIONS,
    MAX_PROPOSITION_TIMESTAMP_BYTES,
    MAX_PROPOSITION_TRUST_CATEGORY_BYTES,
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
    PROPOSITION_EVIDENCE_PATH_SCHEMA_VERSION,
    PROPOSITION_EVIDENCE_PATH_STEP_FIELDS,
    PROPOSITION_EVIDENCE_RECORD_FIELDS,
    PROPOSITION_EVIDENCE_RECORD_SCHEMA_VERSION,
    PROPOSITION_TRUST_INPUTS_FIELDS,
    PROPOSITION_TRUST_INPUTS_SCHEMA_VERSION,
    PROPOSITION_VALIDITY_INPUTS_FIELDS,
    PROPOSITION_VALIDITY_INPUTS_SCHEMA_VERSION,
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
    CostClass,
    DisclosureBasis,
    EvidenceKind,
    EvidencePackageTruncationReason,
    ExpectedObjectType,
    GraphCompositionOperator,
    PropositionOwnership,
    ResolutionOutcome,
    ResolverState,
    TemporalAxis,
    TemporalQueryOperator,
)
from engram.eligibility import (
    EligibilityContextCapture,
    eligibility_context_from_dict,
    eligibility_context_to_dict,
    validate_eligibility_context,
)
from engram.errors import IdentityValidationError, InvalidRequestError
from engram.identity import (
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
from engram.temporal import (
    parse_temporal_query,
    temporal_query,
    temporal_query_from_dict,
    temporal_query_to_dict,
    validate_temporal_query,
)


def require_text(value: object, name: str, maximum_bytes: int, *, allow_empty: bool) -> str:
    if not isinstance(value, str):
        raise InvalidRequestError(f"{name} must be a string")
    if not allow_empty and not value:
        raise InvalidRequestError(f"{name} must not be empty")
    if len(value.encode("utf-8")) > maximum_bytes:
        raise InvalidRequestError(f"{name} exceeds the limit of {maximum_bytes} UTF-8 bytes")
    return value


def require_int(value: object, name: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise InvalidRequestError(f"{name} must be an integer")
    if not minimum <= value <= maximum:
        raise InvalidRequestError(f"{name} must be from {minimum} through {maximum}")
    return value


def require_float(value: object, name: str, minimum: float, maximum: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise InvalidRequestError(f"{name} must be numeric")
    result = float(value)
    if not math_isfinite(result) or not minimum <= result <= maximum:
        raise InvalidRequestError(f"{name} must be finite and from {minimum} through {maximum}")
    return result


def require_bool(value: object, name: str) -> bool:
    if not isinstance(value, bool):
        raise InvalidRequestError(f"{name} must be a boolean")
    return value


def require_identifier(value: object, name: str, maximum_bytes: int = MAX_PROPOSITION_IDENTIFIER_BYTES) -> str:
    identifier = require_text(value, name, maximum_bytes, allow_empty=False)
    if any(character.isspace() or ord(character) < 32 or ord(character) == 127 for character in identifier):
        raise InvalidRequestError(f"{name} must not contain whitespace or control characters")
    return identifier


def require_proposition_timestamp(value: object, available: object, name: str) -> tuple[str, bool]:
    presence = require_bool(available, f"{name}_available")
    text = require_text(value, name, MAX_PROPOSITION_TIMESTAMP_BYTES, allow_empty=not presence)
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


def exact_mapping(value: object, name: str, keys: set[str]) -> dict[str, object]:
    if not isinstance(value, dict):
        raise InvalidRequestError(f"{name} must be an object")
    observed = set(value)
    if observed != keys:
        raise InvalidRequestError(f"{name} has invalid fields: missing={sorted(keys - observed)}, extra={sorted(observed - keys)}")
    return value


def require_list(value: object, name: str) -> list[object]:
    if not isinstance(value, (list, tuple)):
        raise InvalidRequestError(f"{name} must be an array")
    result = list(value)
    return result


def freeze_json(value: object, name: str, depth: int = 0, count=()) -> object:
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
        result = require_text(value, name, MAX_JSON_STRING_BYTES, allow_empty=True)
        return result
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if isinstance(value, float):
        if not math_isfinite(value):
            raise InvalidRequestError(f"{name} numbers must be finite")
        return value
    if isinstance(value, dict):
        frozen = {}
        for key in sorted(value):
            if not isinstance(key, str):
                raise InvalidRequestError(f"{name} keys must be strings")
            require_text(key, f"{name} key", 128, allow_empty=False)
            frozen[key] = freeze_json(value[key], name, depth + 1, count)
        result = dict(frozen)
        return result
    if isinstance(value, (list, tuple)):
        result = tuple(freeze_json(item, name, depth + 1, count) for item in value)
        return result
    raise InvalidRequestError(f"{name} values must be concrete JSON values")


def freeze_mapping(value: object, name: str) -> dict[str, object]:
    frozen = freeze_json(value, name)
    if not isinstance(frozen, dict):
        raise InvalidRequestError(f"{name} must be an object")
    encoded = json_dumps(thaw_json(frozen), sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    if len(encoded.encode("utf-8")) > MAX_JSON_BYTES:
        raise InvalidRequestError(f"{name} exceeds the limit of {MAX_JSON_BYTES} UTF-8 bytes")
    return frozen


def thaw_json(value: object) -> object:
    if isinstance(value, dict):
        result = {key: thaw_json(item) for key, item in value.items()}
        return result
    if isinstance(value, tuple):
        result = [thaw_json(item) for item in value]
        return result
    return value


def json_text(value: dict[str, object]) -> str:
    result = json_dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    return result


def load_json_mapping(value: str, name: str) -> dict[str, object]:
    if not isinstance(value, str):
        raise InvalidRequestError(f"{name} must be a string")
    try:
        decoded = json_loads(value)
    except json_JSONDecodeError as error:
        raise InvalidRequestError(f"{name} must be valid JSON") from error
    if not isinstance(decoded, dict):
        raise InvalidRequestError(f"{name} must contain an object")
    return decoded


def enum_tuple(values: object, enum_type, name: str, maximum: int) -> tuple:
    if not isinstance(values, tuple):
        raise InvalidRequestError(f"{name} must be a tuple")
    if len(values) > maximum:
        raise InvalidRequestError(f"{name} exceeds the limit of {maximum}")
    if not all(isinstance(value, enum_type) for value in values):
        raise InvalidRequestError(f"{name} must contain only {enum_type.__name__} values")
    if len(set(values)) != len(values):
        raise InvalidRequestError(f"{name} must not contain duplicates")
    return values


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
    schema_version: object = RESOLUTION_BUDGET_SCHEMA_VERSION,
) -> dict:
    """Build immutable non-time resource limits and a measurement start."""
    version = require_int(schema_version, "schema_version", 0, MAX_RESOURCE_COUNTER)
    if version != RESOLUTION_BUDGET_SCHEMA_VERSION:
        raise InvalidRequestError(f"unsupported resolution budget schema_version: {version}")
    normalized_costs = enum_tuple(allowed_cost_classes, CostClass, "allowed_cost_classes", len(CostClass))
    if not normalized_costs:
        raise InvalidRequestError("allowed_cost_classes must not be empty")
    normalized_started = require_int(started_ns, "started_ns", 0, MAX_RESOURCE_COUNTER)
    result: dict = {
        "schema_version": version,
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
        schema_version=data["schema_version"],
    )
    return result


def resolution_budget_with_changes(value: object, changes: object) -> dict:
    budget = validate_resolution_budget(value)
    if not isinstance(changes, dict):
        raise InvalidRequestError("resolution budget changes must be an object")
    if not set(changes).issubset(RESOLUTION_BUDGET_FIELDS):
        raise InvalidRequestError("resolution budget changes contain an unknown field")
    updated: dict[str, object] = dict(budget)
    updated.update(changes)
    result = validate_resolution_budget(updated)
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
    }
    return result


def resolution_budget_from_dict(value: object) -> dict:
    data = exact_mapping(value, "ResolutionBudget", RESOLUTION_BUDGET_FIELDS)
    raw_costs = require_list(data["allowed_cost_classes"], "allowed_cost_classes")
    try:
        costs = tuple(CostClass(require_text(item, "cost class", 32, allow_empty=False)) for item in raw_costs)
    except ValueError as error:
        raise InvalidRequestError("allowed_cost_classes contains an unsupported value") from error
    result = resolution_budget(
        schema_version=data["schema_version"],
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
    )
    return result


def resolution_budget_to_json(value: object) -> str:
    payload = resolution_budget_to_dict(value)
    result = json_text(payload)
    return result


def resolution_budget_from_json(value: str) -> dict:
    data = load_json_mapping(value, "ResolutionBudget JSON")
    result = resolution_budget_from_dict(data)
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
    schema_version: object = BUDGET_CONSUMPTION_SCHEMA_VERSION,
) -> dict:
    """Build concrete resource use for a resolver or complete resolution."""
    version = require_int(schema_version, "schema_version", 0, MAX_RESOURCE_COUNTER)
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
        name: require_int(field_value, name, 0, MAX_RESOURCE_COUNTER) for name, field_value in numeric_values.items()
    }
    if not isinstance(exhausted_dimensions, tuple):
        raise InvalidRequestError("exhausted_dimensions must be a tuple")
    if len(exhausted_dimensions) > MAX_EXHAUSTED_DIMENSIONS:
        raise InvalidRequestError(f"exhausted_dimensions exceeds the limit of {MAX_EXHAUSTED_DIMENSIONS}")
    normalized_exhausted = tuple(
        require_text(value, "exhausted dimension", MAX_EXHAUSTED_DIMENSION_BYTES, allow_empty=False)
        for value in exhausted_dimensions
    )
    if normalized_exhausted != tuple(sorted(set(normalized_exhausted))):
        raise InvalidRequestError("exhausted_dimensions must be unique and sorted")
    if not isinstance(measurement_available, bool):
        raise InvalidRequestError("measurement_available must be a boolean")
    result: dict = {
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


def validate_budget_consumption(value: object) -> dict:
    data = exact_mapping(value, "BudgetConsumption", BUDGET_CONSUMPTION_FIELDS)
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


def budget_consumption_from_dict(value: object) -> dict:
    data = exact_mapping(value, "BudgetConsumption", BUDGET_CONSUMPTION_FIELDS)
    exhausted = require_list(data["exhausted_dimensions"], "exhausted_dimensions")
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
    result = json_text(payload)
    return result


def budget_consumption_from_json(value: str) -> dict:
    data = load_json_mapping(value, "BudgetConsumption JSON")
    result = budget_consumption_from_dict(data)
    return result


def inheritance_provenance(field_name: object, source_turn: object) -> dict:
    """Build concrete empty-capable contextual field provenance owned by Section 8."""
    result: dict = {
        "field_name": require_text(field_name, "inheritance field_name", 64, allow_empty=False),
        "source_turn": require_int(source_turn, "inheritance source_turn", 1, 1_000_000),
    }
    return result


def validate_inheritance_provenance(value: object) -> dict:
    data = exact_mapping(value, "InheritanceProvenance", INHERITANCE_PROVENANCE_FIELDS)
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


def inheritance_provenance_from_dict(value: object) -> dict:
    result = validate_inheritance_provenance(value)
    return result


def rewrite_trace_step(rule_id: object, input_text: object, output_text: object) -> dict:
    """Build an empty-capable rewrite trace whose rule semantics belong to Section 11."""
    result: dict = {
        "rule_id": require_text(rule_id, "rewrite rule_id", 128, allow_empty=False),
        "input_text": require_text(input_text, "rewrite input_text", MAX_REQUEST_BYTES, allow_empty=False),
        "output_text": require_text(output_text, "rewrite output_text", MAX_REQUEST_BYTES, allow_empty=False),
    }
    return result


def validate_rewrite_trace_step(value: object) -> dict:
    data = exact_mapping(value, "RewriteTraceStep", REWRITE_TRACE_STEP_FIELDS)
    result = rewrite_trace_step(data["rule_id"], data["input_text"], data["output_text"])
    return result


def rewrite_trace_step_to_dict(value: object) -> dict[str, object]:
    step = validate_rewrite_trace_step(value)
    result: dict[str, object] = dict(step)
    return result


def rewrite_trace_step_from_dict(value: object) -> dict:
    result = validate_rewrite_trace_step(value)
    return result


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
    temporal_query_value: object = (),
    schema_version: object = QUERY_FRAME_SCHEMA_VERSION,
) -> dict:
    """Build the immutable base interpretation passed to every resolver."""
    validated_schema_version = require_int(schema_version, "schema_version", 0, MAX_RESOURCE_COUNTER)
    if validated_schema_version != QUERY_FRAME_SCHEMA_VERSION:
        raise InvalidRequestError(f"unsupported query frame schema_version: {validated_schema_version}")
    original = require_text(original_text, "frame original_text", MAX_REQUEST_BYTES, allow_empty=False)
    resolved = require_text(resolved_text, "frame resolved_text", MAX_REQUEST_BYTES, allow_empty=False)
    try:
        validated_identity = validate_query_identity(identity)
    except IdentityValidationError as error:
        raise InvalidRequestError("frame identity must be a QueryIdentity") from error
    if not isinstance(expected_object_type, ExpectedObjectType):
        raise InvalidRequestError("frame expected_object_type must be an ExpectedObjectType")
    try:
        validated_temporal = temporal_query() if temporal_query_value == () else validate_temporal_query(temporal_query_value)
    except InvalidRequestError as error:
        raise InvalidRequestError("frame temporal_query must be a TemporalQuery") from error
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
    frozen_metadata = freeze_mapping(required_metadata, "frame required_metadata")
    source_label = require_text(
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
    validated_diagnostic_id = require_text(
        diagnostic_id,
        "frame diagnostic_id",
        MAX_DIAGNOSTIC_ID_BYTES,
        allow_empty=False,
    )
    result: dict = {
        "original_text": original,
        "resolved_text": resolved,
        "identity": validated_identity,
        "expected_object_type": expected_object_type,
        "temporal_query": validated_temporal,
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


def validate_query_frame(value: object) -> dict:
    data = exact_mapping(value, "QueryFrame", QUERY_FRAME_FIELDS)
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
        data["temporal_query"],
        data["schema_version"],
    )
    return result


def query_frame_with_changes(value: object, changes: object) -> dict:
    current = validate_query_frame(value)
    if not isinstance(changes, dict) or not set(changes).issubset(QUERY_FRAME_FIELDS):
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
        "temporal_query": temporal_query_to_dict(frame["temporal_query"]),
        "inheritance": [inheritance_provenance_to_dict(item) for item in frame["inheritance"]],
        "rewrite_chain": [rewrite_trace_step_to_dict(item) for item in frame["rewrite_chain"]],
        "scope": scope_key_to_dict(frame["scope"]),
        "required_metadata": thaw_json(frame["required_metadata"]),
        "required_source_label": frame["required_source_label"],
        "budget": resolution_budget_to_dict(frame["budget"]),
        "eligibility_context": eligibility_context_to_dict(frame["eligibility_context"]),
        "diagnostic_id": frame["diagnostic_id"],
    }
    return result


def query_frame_to_json(value: object) -> str:
    payload = query_frame_to_dict(value)
    result = json_text(payload)
    return result


def query_frame_from_dict(value: object) -> dict:
    data = exact_mapping(value, "QueryFrame", QUERY_FRAME_FIELDS)
    inheritance = require_list(data["inheritance"], "frame inheritance")
    rewrites = require_list(data["rewrite_chain"], "frame rewrite_chain")
    try:
        expected_type = ExpectedObjectType(
            require_text(data["expected_object_type"], "expected_object_type", 32, allow_empty=False)
        )
    except ValueError as error:
        raise InvalidRequestError("unsupported expected_object_type") from error
    result = query_frame(
        schema_version=require_int(
            data["schema_version"], "schema_version", QUERY_FRAME_SCHEMA_VERSION, QUERY_FRAME_SCHEMA_VERSION
        ),
        original_text=require_text(data["original_text"], "frame original_text", MAX_REQUEST_BYTES, allow_empty=False),
        resolved_text=require_text(data["resolved_text"], "frame resolved_text", MAX_REQUEST_BYTES, allow_empty=False),
        identity=query_identity_from_dict(thaw_json(freeze_mapping(data["identity"], "frame identity"))),
        expected_object_type=expected_type,
        temporal_query_value=temporal_query_from_dict(freeze_mapping(data["temporal_query"], "frame temporal_query")),
        inheritance=tuple(inheritance_provenance_from_dict(freeze_mapping(item, "inheritance item")) for item in inheritance),
        rewrite_chain=tuple(rewrite_trace_step_from_dict(freeze_mapping(item, "rewrite item")) for item in rewrites),
        scope=scope_key_from_dict(freeze_mapping(data["scope"], "frame scope")),
        required_metadata=freeze_mapping(data["required_metadata"], "frame required_metadata"),
        required_source_label=require_text(
            data["required_source_label"],
            "frame required_source_label",
            MAX_REQUIRED_SOURCE_LABEL_BYTES,
            allow_empty=True,
        ),
        budget=resolution_budget_from_dict(freeze_mapping(data["budget"], "frame budget")),
        eligibility_context=eligibility_context_from_dict(freeze_mapping(data["eligibility_context"], "frame eligibility_context")),
        diagnostic_id=require_text(
            data["diagnostic_id"],
            "frame diagnostic_id",
            MAX_DIAGNOSTIC_ID_BYTES,
            allow_empty=False,
        ),
    )
    return result


def query_frame_from_json(value: str) -> dict:
    decoded = load_json_mapping(value, "QueryFrame JSON")
    result = query_frame_from_dict(decoded)
    return result


def feature_set(
    values: object = EMPTY_MAPPING,
    unavailable: object = (),
    schema_version: object = FEATURE_SET_SCHEMA_VERSION,
) -> dict:
    """Build a generic feature value/availability dictionary; Section 5 owns semantics."""
    version = require_int(schema_version, "schema_version", 0, 2_147_483_647)
    if version != FEATURE_SET_SCHEMA_VERSION:
        raise InvalidRequestError(f"unsupported feature set schema_version: {version}")
    if not isinstance(values, dict):
        raise InvalidRequestError("feature values must be an object")
    validated_values = {}
    for name in sorted(values):
        key = require_text(name, "feature name", 96, allow_empty=False)
        validated_values[key] = require_float(values[name], f"feature {key}", -1_000_000.0, 1_000_000.0)
    if len(validated_values) > MAX_FEATURES:
        raise InvalidRequestError(f"feature values exceed the limit of {MAX_FEATURES}")
    if not isinstance(unavailable, tuple):
        raise InvalidRequestError("unavailable features must be a tuple")
    normalized_unavailable = tuple(require_text(name, "unavailable feature", 96, allow_empty=False) for name in unavailable)
    if normalized_unavailable != tuple(sorted(set(normalized_unavailable))):
        raise InvalidRequestError("unavailable features must be unique and sorted")
    if set(validated_values).intersection(normalized_unavailable):
        raise InvalidRequestError("a feature cannot be both available and unavailable")
    if len(validated_values) + len(normalized_unavailable) > MAX_FEATURES:
        raise InvalidRequestError(f"features exceed the limit of {MAX_FEATURES}")
    result: dict = {
        "schema_version": version,
        "values": dict(validated_values),
        "unavailable": normalized_unavailable,
    }
    return result


def validate_feature_set(value: object) -> dict:
    """Revalidate and defensively copy one feature-set dictionary."""
    data = exact_mapping(value, "FeatureSet", FEATURE_SET_FIELDS)
    result = feature_set(data["values"], data["unavailable"], data["schema_version"])
    return result


def trusted_feature_set(values: dict[str, float], unavailable: tuple[str, ...]) -> dict:
    """Build features whose bounds and ordering the resolution engine established."""
    result: dict = {
        "schema_version": FEATURE_SET_SCHEMA_VERSION,
        "values": dict(dict(values)),
        "unavailable": unavailable,
    }
    return result


def feature_set_with_changes(value: object, changes: object) -> dict:
    """Apply named fields and revalidate one feature-set dictionary."""
    features = validate_feature_set(value)
    if not isinstance(changes, dict):
        raise InvalidRequestError("feature set changes must be an object")
    if not set(changes).issubset(FEATURE_SET_FIELDS):
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


def feature_set_from_dict(value: object) -> dict:
    """Decode one feature set from its exact serialized form."""
    data = exact_mapping(value, "FeatureSet", FEATURE_SET_FIELDS)
    unavailable = require_list(data["unavailable"], "unavailable features")
    raw_values = freeze_mapping(data["values"], "feature values")
    values = {name: require_float(item, f"feature {name}", -1_000_000.0, 1_000_000.0) for name, item in raw_values.items()}
    normalized_unavailable = tuple(require_text(item, "unavailable feature", 96, allow_empty=False) for item in unavailable)
    result = feature_set(values, normalized_unavailable, data["schema_version"])
    return result


def feature_set_to_json(value: object) -> str:
    """Serialize one feature set deterministically."""
    payload = feature_set_to_dict(value)
    result = json_text(payload)
    return result


def feature_set_from_json(value: str) -> dict:
    """Decode one feature set from deterministic JSON."""
    data = load_json_mapping(value, "FeatureSet JSON")
    result = feature_set_from_dict(data)
    return result


def canonical_proposition_references(
    subject_entity_id: object,
    predicate_id: object,
    object_entity_id: object,
    schema_version: object = CANONICAL_PROPOSITION_REFERENCES_SCHEMA_VERSION,
) -> dict:
    """Build canonical graph identifiers for one full Proposition record."""
    version = require_int(schema_version, "schema_version", 1, 1)
    result: dict = {
        "schema_version": version,
        "subject_entity_id": require_identifier(subject_entity_id, "Proposition subject_entity_id"),
        "predicate_id": require_identifier(predicate_id, "Proposition predicate_id"),
        "object_entity_id": require_identifier(object_entity_id, "Proposition object_entity_id"),
    }
    return result


def validate_canonical_proposition_references(value: object) -> dict:
    """Revalidate and copy one canonical Proposition-reference dictionary."""
    data = exact_mapping(value, "CanonicalPropositionReferences", CANONICAL_PROPOSITION_REFERENCES_FIELDS)
    result = canonical_proposition_references(
        data["subject_entity_id"],
        data["predicate_id"],
        data["object_entity_id"],
        data["schema_version"],
    )
    return result


def canonical_proposition_references_with_changes(value: object, changes: object) -> dict:
    """Apply named fields and revalidate canonical Proposition references."""
    references = validate_canonical_proposition_references(value)
    if not isinstance(changes, dict):
        raise InvalidRequestError("canonical Proposition reference changes must be an object")
    if not set(changes).issubset(CANONICAL_PROPOSITION_REFERENCES_FIELDS):
        raise InvalidRequestError("canonical Proposition reference changes contain an unknown field")
    updated: dict[str, object] = dict(references)
    updated.update(changes)
    result = validate_canonical_proposition_references(updated)
    return result


def canonical_proposition_references_to_dict(value: object) -> dict[str, object]:
    """Serialize canonical Proposition references."""
    references = validate_canonical_proposition_references(value)
    result: dict[str, object] = dict(references)
    return result


def canonical_proposition_references_from_dict(value: object) -> dict:
    """Decode canonical Proposition references from their exact serialized form."""
    result = validate_canonical_proposition_references(value)
    return result


def proposition_validity_inputs(
    evaluation_time: object,
    active: object,
    system_current: object,
    valid_time_current: object,
    valid_from: object = "",
    valid_from_available: object = False,
    valid_to: object = "",
    valid_to_available: object = False,
    schema_version: object = PROPOSITION_VALIDITY_INPUTS_SCHEMA_VERSION,
    temporal_operator: object = TemporalQueryOperator.UNSPECIFIED,
    temporal_axis: object = TemporalAxis.VALID_TIME,
    requested_start: object = "",
    requested_start_available: object = False,
    requested_end: object = "",
    requested_end_available: object = False,
    system_from: object = "",
    system_from_available: object = False,
    system_to: object = "",
    system_to_available: object = False,
    invalidated_at: object = "",
    invalidated_at_available: object = False,
    eligible_for_request: object = True,
    system_time_match: object = True,
    valid_time_match: object = True,
    valid_time_match_available: object = True,
) -> dict:
    """Build inspectable temporal inputs for one eligible Proposition."""
    version = require_int(
        schema_version,
        "schema_version",
        PROPOSITION_VALIDITY_INPUTS_SCHEMA_VERSION,
        PROPOSITION_VALIDITY_INPUTS_SCHEMA_VERSION,
    )
    evaluation, evaluation_available = require_proposition_timestamp(evaluation_time, True, "Proposition evaluation_time")
    normalized_active = require_bool(active, "Proposition active")
    normalized_system_current = require_bool(system_current, "Proposition system_current")
    normalized_valid_time_current = require_bool(valid_time_current, "Proposition valid_time_current")
    if not isinstance(temporal_operator, TemporalQueryOperator):
        raise InvalidRequestError("Proposition temporal_operator is unsupported")
    if not isinstance(temporal_axis, TemporalAxis):
        raise InvalidRequestError("Proposition temporal_axis is unsupported")
    requested_lower, requested_lower_available = require_proposition_timestamp(
        requested_start,
        requested_start_available,
        "Proposition requested_start",
    )
    requested_upper, requested_upper_available = require_proposition_timestamp(
        requested_end,
        requested_end_available,
        "Proposition requested_end",
    )
    system_lower, system_lower_available = require_proposition_timestamp(
        system_from, system_from_available, "Proposition system_from"
    )
    system_upper, system_upper_available = require_proposition_timestamp(system_to, system_to_available, "Proposition system_to")
    invalidated, invalidated_available = require_proposition_timestamp(
        invalidated_at,
        invalidated_at_available,
        "Proposition invalidated_at",
    )
    lower, lower_available = require_proposition_timestamp(valid_from, valid_from_available, "Proposition valid_from")
    upper, upper_available = require_proposition_timestamp(valid_to, valid_to_available, "Proposition valid_to")
    for name, interval_lower, interval_lower_available, interval_upper, interval_upper_available in (
        ("requested_start", requested_lower, requested_lower_available, requested_upper, requested_upper_available),
        ("system_from", system_lower, system_lower_available, system_upper, system_upper_available),
        ("valid_from", lower, lower_available, upper, upper_available),
    ):
        if interval_lower_available and interval_upper_available:
            lower_time = datetime.fromisoformat(interval_lower[:-1] + "+00:00")
            upper_time = datetime.fromisoformat(interval_upper[:-1] + "+00:00")
            if lower_time >= upper_time:
                raise InvalidRequestError(f"Proposition {name} must be earlier than its upper bound")
    expected_request_bounds = {
        TemporalQueryOperator.UNSPECIFIED: (False, False),
        TemporalQueryOperator.CURRENT: (False, False),
        TemporalQueryOperator.NOW: (False, False),
        TemporalQueryOperator.AS_OF: (True, False),
        TemporalQueryOperator.IN_YEAR: (True, True),
        TemporalQueryOperator.BEFORE: (False, True),
        TemporalQueryOperator.AFTER: (True, False),
        TemporalQueryOperator.BETWEEN: (True, True),
        TemporalQueryOperator.LATEST: (False, False),
    }
    if (requested_lower_available, requested_upper_available) != expected_request_bounds.get(temporal_operator, ()):
        raise InvalidRequestError("Proposition requested temporal bounds conflict with temporal_operator")
    evaluated_at = datetime.fromisoformat(evaluation[:-1] + "+00:00")
    effective_system_upper = system_upper
    effective_system_upper_available = system_upper_available
    if invalidated_available and (
        not effective_system_upper_available
        or datetime.fromisoformat(invalidated[:-1] + "+00:00") < datetime.fromisoformat(effective_system_upper[:-1] + "+00:00")
    ):
        effective_system_upper = invalidated
        effective_system_upper_available = True
    observed_active = not invalidated_available
    observed_system_current = (
        not system_lower_available or evaluated_at >= datetime.fromisoformat(system_lower[:-1] + "+00:00")
    ) and (not effective_system_upper_available or evaluated_at < datetime.fromisoformat(effective_system_upper[:-1] + "+00:00"))
    observed_valid_current = (not lower_available or evaluated_at >= datetime.fromisoformat(lower[:-1] + "+00:00")) and (
        not upper_available or evaluated_at < datetime.fromisoformat(upper[:-1] + "+00:00")
    )
    if normalized_active != observed_active:
        raise InvalidRequestError("Proposition active conflicts with the disclosed invalidation boundary")
    if normalized_system_current != observed_system_current:
        raise InvalidRequestError("Proposition system_current conflicts with the disclosed system interval")
    if normalized_valid_time_current != observed_valid_current:
        raise InvalidRequestError("Proposition valid_time_current conflicts with the disclosed valid interval")
    normalized_request_eligible = require_bool(eligible_for_request, "Proposition eligible_for_request")
    normalized_system_match = require_bool(system_time_match, "Proposition system_time_match")
    normalized_valid_match = require_bool(valid_time_match, "Proposition valid_time_match")
    normalized_valid_match_available = require_bool(
        valid_time_match_available,
        "Proposition valid_time_match_available",
    )
    if not normalized_request_eligible or not normalized_system_match:
        raise InvalidRequestError("Proposition evidence validity inputs must describe a Proposition eligible for the request")
    if normalized_valid_match_available != (temporal_axis == TemporalAxis.VALID_TIME):
        raise InvalidRequestError("Proposition valid_time_match availability conflicts with temporal_axis")
    if normalized_valid_match_available != normalized_valid_match:
        raise InvalidRequestError("available Proposition valid_time_match must be true and unavailable match must be false")
    result: dict = {
        "schema_version": version,
        "evaluation_time": evaluation,
        "active": normalized_active,
        "system_current": normalized_system_current,
        "valid_time_current": normalized_valid_time_current,
        "eligible_for_request": normalized_request_eligible,
        "system_time_match": normalized_system_match,
        "valid_time_match": normalized_valid_match,
        "valid_time_match_available": normalized_valid_match_available,
        "temporal_operator": temporal_operator,
        "temporal_axis": temporal_axis,
        "requested_start": requested_lower,
        "requested_start_available": requested_lower_available,
        "requested_end": requested_upper,
        "requested_end_available": requested_upper_available,
        "system_from": system_lower,
        "system_from_available": system_lower_available,
        "system_to": system_upper,
        "system_to_available": system_upper_available,
        "invalidated_at": invalidated,
        "invalidated_at_available": invalidated_available,
        "valid_from": lower,
        "valid_from_available": lower_available,
        "valid_to": upper,
        "valid_to_available": upper_available,
    }
    return result


def validate_proposition_validity_inputs(value: object) -> dict:
    """Revalidate and copy one Proposition-validity input dictionary."""
    data = exact_mapping(value, "PropositionValidityInputs", PROPOSITION_VALIDITY_INPUTS_FIELDS)
    result = proposition_validity_inputs(
        data["evaluation_time"],
        data["active"],
        data["system_current"],
        data["valid_time_current"],
        data["valid_from"],
        data["valid_from_available"],
        data["valid_to"],
        data["valid_to_available"],
        data["schema_version"],
        data["temporal_operator"],
        data["temporal_axis"],
        data["requested_start"],
        data["requested_start_available"],
        data["requested_end"],
        data["requested_end_available"],
        data["system_from"],
        data["system_from_available"],
        data["system_to"],
        data["system_to_available"],
        data["invalidated_at"],
        data["invalidated_at_available"],
        data["eligible_for_request"],
        data["system_time_match"],
        data["valid_time_match"],
        data["valid_time_match_available"],
    )
    return result


def proposition_validity_inputs_with_changes(value: object, changes: object) -> dict:
    """Apply named fields and revalidate complete Proposition-validity inputs."""
    validity = validate_proposition_validity_inputs(value)
    if not isinstance(changes, dict):
        raise InvalidRequestError("Proposition validity changes must be an object")
    if not set(changes).issubset(PROPOSITION_VALIDITY_INPUTS_FIELDS):
        raise InvalidRequestError("Proposition validity changes contain an unknown field")
    updated: dict[str, object] = dict(validity)
    updated.update(changes)
    result = validate_proposition_validity_inputs(updated)
    return result


def proposition_validity_inputs_to_dict(value: object) -> dict[str, object]:
    """Serialize Proposition-validity inputs."""
    validity = validate_proposition_validity_inputs(value)
    result: dict[str, object] = dict(validity)
    result["temporal_operator"] = validity["temporal_operator"].value
    result["temporal_axis"] = validity["temporal_axis"].value
    return result


def proposition_validity_inputs_from_dict(value: object) -> dict:
    """Decode Proposition-validity inputs from their exact serialized form."""
    if not isinstance(value, dict):
        raise InvalidRequestError("PropositionValidityInputs must be an object")
    decoded = dict(value)
    try:
        decoded["temporal_operator"] = TemporalQueryOperator(str(decoded.get("temporal_operator", "")))
        decoded["temporal_axis"] = TemporalAxis(str(decoded.get("temporal_axis", "")))
    except ValueError as error:
        raise InvalidRequestError("Proposition validity temporal enum is unsupported") from error
    result = validate_proposition_validity_inputs(decoded)
    return result


def proposition_trust_inputs(
    trust_category: object = "",
    trust_category_available: object = False,
    supplied_trust: object = 0.0,
    supplied_trust_available: object = False,
    supplied_trust_version: object = 0,
    supplied_trust_version_available: object = False,
    schema_version: object = PROPOSITION_TRUST_INPUTS_SCHEMA_VERSION,
) -> dict:
    """Build supplied Proposition trust values with concrete availability."""
    version = require_int(schema_version, "schema_version", 1, 1)
    category_available = require_bool(trust_category_available, "Proposition trust_category_available")
    category = require_text(
        trust_category,
        "Proposition trust_category",
        MAX_PROPOSITION_TRUST_CATEGORY_BYTES,
        allow_empty=not category_available,
    )
    if not category_available and category:
        raise InvalidRequestError("Proposition trust_category must be empty when unavailable")
    supplied_available = require_bool(supplied_trust_available, "Proposition supplied_trust_available")
    supplied = require_float(supplied_trust, "Proposition supplied_trust", 0.0, 1.0)
    if not supplied_available and supplied != 0.0:
        raise InvalidRequestError("Proposition supplied_trust must be zero when unavailable")
    version_available = require_bool(supplied_trust_version_available, "Proposition supplied_trust_version_available")
    trust_version = require_int(supplied_trust_version, "Proposition supplied_trust_version", 0, 2_147_483_647)
    if not version_available and trust_version != 0:
        raise InvalidRequestError("Proposition supplied_trust_version must be zero when unavailable")
    if supplied_available != version_available:
        raise InvalidRequestError("Proposition supplied trust value and version availability must match")
    if version_available and trust_version == 0:
        raise InvalidRequestError("Proposition supplied_trust_version must be positive when available")
    result: dict = {
        "schema_version": version,
        "trust_category": category,
        "trust_category_available": category_available,
        "supplied_trust": supplied,
        "supplied_trust_available": supplied_available,
        "supplied_trust_version": trust_version,
        "supplied_trust_version_available": version_available,
    }
    return result


def validate_proposition_trust_inputs(value: object) -> dict:
    """Revalidate and copy one Proposition-trust input dictionary."""
    data = exact_mapping(value, "PropositionTrustInputs", PROPOSITION_TRUST_INPUTS_FIELDS)
    result = proposition_trust_inputs(
        data["trust_category"],
        data["trust_category_available"],
        data["supplied_trust"],
        data["supplied_trust_available"],
        data["supplied_trust_version"],
        data["supplied_trust_version_available"],
        data["schema_version"],
    )
    return result


def proposition_trust_inputs_with_changes(value: object, changes: object) -> dict:
    """Apply named fields and revalidate complete Proposition-trust inputs."""
    trust = validate_proposition_trust_inputs(value)
    if not isinstance(changes, dict):
        raise InvalidRequestError("Proposition trust changes must be an object")
    if not set(changes).issubset(PROPOSITION_TRUST_INPUTS_FIELDS):
        raise InvalidRequestError("Proposition trust changes contain an unknown field")
    updated: dict[str, object] = dict(trust)
    updated.update(changes)
    result = validate_proposition_trust_inputs(updated)
    return result


def proposition_trust_inputs_to_dict(value: object) -> dict[str, object]:
    """Serialize Proposition-trust inputs."""
    trust = validate_proposition_trust_inputs(value)
    result: dict[str, object] = dict(trust)
    return result


def proposition_trust_inputs_from_dict(value: object) -> dict:
    """Decode Proposition-trust inputs from their exact serialized form."""
    result = validate_proposition_trust_inputs(value)
    return result


def disclosure_decision(
    ownership: object,
    basis: object,
    scope: object,
    policy_version: object,
    authority: object = "",
    authority_available: object = False,
    schema_version: object = DISCLOSURE_DECISION_SCHEMA_VERSION,
) -> dict:
    """Build an exact scoped Proposition-disclosure decision."""
    version = require_int(schema_version, "schema_version", 1, 1)
    if not isinstance(ownership, PropositionOwnership):
        raise InvalidRequestError("disclosure ownership must be a PropositionOwnership")
    if not isinstance(basis, DisclosureBasis):
        raise InvalidRequestError("disclosure basis must be a DisclosureBasis")
    try:
        validated_scope = validate_scope_key(scope)
    except IdentityValidationError as error:
        raise InvalidRequestError("disclosure scope must be a ScopeKey") from error
    normalized_policy_version = require_identifier(policy_version, "disclosure policy_version")
    normalized_authority_available = require_bool(authority_available, "disclosure authority_available")
    if normalized_authority_available:
        normalized_authority = require_identifier(authority, "disclosure authority")
    else:
        normalized_authority = require_text(
            authority,
            "disclosure authority",
            MAX_DISCLOSURE_AUTHORITY_BYTES,
            allow_empty=True,
        )
    if not normalized_authority_available and normalized_authority:
        raise InvalidRequestError("disclosure authority must be empty when unavailable")
    if ownership == PropositionOwnership.PUBLIC:
        if basis != DisclosureBasis.PUBLIC_RULE or normalized_authority_available:
            raise InvalidRequestError("PUBLIC Proposition disclosure requires the public rule without an authority")
    elif basis != DisclosureBasis.TRUSTED_SCOPE_AUTHORITY or not normalized_authority_available:
        raise InvalidRequestError("non-PUBLIC Proposition disclosure requires an available trusted scope authority")
    result: dict = {
        "schema_version": version,
        "ownership": ownership,
        "basis": basis,
        "scope": validated_scope,
        "policy_version": normalized_policy_version,
        "authority": normalized_authority,
        "authority_available": normalized_authority_available,
    }
    return result


def validate_disclosure_decision(value: object) -> dict:
    """Revalidate and copy one disclosure-decision dictionary."""
    data = exact_mapping(value, "DisclosureDecision", DISCLOSURE_DECISION_FIELDS)
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


def disclosure_decision_with_changes(value: object, changes: object) -> dict:
    """Apply named fields and revalidate one disclosure decision."""
    decision = validate_disclosure_decision(value)
    if not isinstance(changes, dict):
        raise InvalidRequestError("disclosure decision changes must be an object")
    if not set(changes).issubset(DISCLOSURE_DECISION_FIELDS):
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


def disclosure_decision_from_dict(value: object) -> dict:
    """Decode one disclosure decision from its exact serialized form."""
    data = exact_mapping(value, "DisclosureDecision", DISCLOSURE_DECISION_FIELDS)
    try:
        ownership = PropositionOwnership(
            require_text(data["ownership"], "disclosure ownership", MAX_DISCLOSURE_ENUM_BYTES, allow_empty=False)
        )
        basis = DisclosureBasis(require_text(data["basis"], "disclosure basis", MAX_DISCLOSURE_ENUM_BYTES, allow_empty=False))
    except ValueError as error:
        raise InvalidRequestError("unsupported disclosure ownership or basis") from error
    result = disclosure_decision(
        ownership,
        basis,
        scope_key_from_dict(freeze_mapping(data["scope"], "disclosure scope")),
        data["policy_version"],
        data["authority"],
        data["authority_available"],
        data["schema_version"],
    )
    return result


PROPOSITION_PATH_FILTERS = {
    "canonical_identity",
    "temporal_eligibility",
    "visibility",
    "publication_revalidation",
    "object_type",
}


def path_binding(value: object, name: str) -> str:
    binding = require_identifier(value, name, MAX_COMPOSITION_BINDING_BYTES)
    if not binding.startswith("$") or len(binding) == 1:
        raise InvalidRequestError(f"{name} must start with $ and contain a name")
    return binding


def proposition_evidence_path_step(
    position: object,
    proposition_id: object,
    subject_entity_id: object,
    predicate_id: object,
    object_entity_id: object,
    operator: object,
    input_binding: object,
    output_binding: object,
    filters: object,
    aggregation_inputs: object = (),
    schema_version: object = PROPOSITION_EVIDENCE_PATH_SCHEMA_VERSION,
) -> dict:
    """Build one closed Proposition-path step without unrestricted graph content."""
    version = require_int(schema_version, "schema_version", 1, PROPOSITION_EVIDENCE_PATH_SCHEMA_VERSION)
    normalized_position = require_int(position, "Proposition evidence path position", 0, MAX_COMPOSITION_PATH_PROPOSITIONS - 1)
    if not isinstance(operator, GraphCompositionOperator):
        raise InvalidRequestError("Proposition evidence path operator is unsupported")
    if not isinstance(filters, tuple):
        raise InvalidRequestError("Proposition evidence path filters must be a tuple")
    normalized_filters = tuple(require_identifier(value, "Proposition evidence path filter") for value in filters)
    if not normalized_filters or normalized_filters != tuple(sorted(set(normalized_filters))):
        raise InvalidRequestError("Proposition evidence path filters must be non-empty, unique, and sorted")
    if not set(normalized_filters).issubset(PROPOSITION_PATH_FILTERS):
        raise InvalidRequestError("Proposition evidence path filter is unsupported")
    if not isinstance(aggregation_inputs, tuple):
        raise InvalidRequestError("Proposition evidence path aggregation_inputs must be a tuple")
    normalized_aggregation = tuple(
        path_binding(value, "Proposition evidence path aggregation input") for value in aggregation_inputs
    )
    if normalized_aggregation != tuple(sorted(set(normalized_aggregation))):
        raise InvalidRequestError("Proposition evidence path aggregation_inputs must be unique and sorted")
    result: dict = {
        "schema_version": version,
        "position": normalized_position,
        "proposition_id": require_identifier(proposition_id, "Proposition evidence path proposition_id"),
        "subject_entity_id": require_identifier(subject_entity_id, "Proposition evidence path subject_entity_id"),
        "predicate_id": require_identifier(predicate_id, "Proposition evidence path predicate_id"),
        "object_entity_id": require_identifier(object_entity_id, "Proposition evidence path object_entity_id"),
        "operator": operator,
        "input_binding": path_binding(input_binding, "Proposition evidence path input_binding"),
        "output_binding": path_binding(output_binding, "Proposition evidence path output_binding"),
        "filters": normalized_filters,
        "aggregation_inputs": normalized_aggregation,
    }
    if result.get("input_binding", "") == result.get("output_binding", ""):
        raise InvalidRequestError("Proposition evidence path input and output bindings must differ")
    return result


def validate_proposition_evidence_path_step(value: object) -> dict:
    data = exact_mapping(value, "PropositionEvidencePathStep", PROPOSITION_EVIDENCE_PATH_STEP_FIELDS)
    result = proposition_evidence_path_step(
        data["position"],
        data["proposition_id"],
        data["subject_entity_id"],
        data["predicate_id"],
        data["object_entity_id"],
        data["operator"],
        data["input_binding"],
        data["output_binding"],
        data["filters"],
        data["aggregation_inputs"],
        data["schema_version"],
    )
    return result


def proposition_evidence_path_step_to_dict(value: object) -> dict[str, object]:
    step = validate_proposition_evidence_path_step(value)
    result = {
        "schema_version": step["schema_version"],
        "position": step["position"],
        "proposition_id": step["proposition_id"],
        "subject_entity_id": step["subject_entity_id"],
        "predicate_id": step["predicate_id"],
        "object_entity_id": step["object_entity_id"],
        "operator": step["operator"].value,
        "input_binding": step["input_binding"],
        "output_binding": step["output_binding"],
        "filters": list(step["filters"]),
        "aggregation_inputs": list(step["aggregation_inputs"]),
    }
    return result


def proposition_evidence_path_step_from_dict(value: object) -> dict:
    data = exact_mapping(value, "PropositionEvidencePathStep", PROPOSITION_EVIDENCE_PATH_STEP_FIELDS)
    try:
        operator = GraphCompositionOperator(
            require_text(data["operator"], "Proposition evidence path operator", 16, allow_empty=False)
        )
    except ValueError as error:
        raise InvalidRequestError("Proposition evidence path operator is unsupported") from error
    raw_filters = require_list(data["filters"], "Proposition evidence path filters")
    raw_aggregation = require_list(data["aggregation_inputs"], "Proposition evidence path aggregation_inputs")
    result = proposition_evidence_path_step(
        data["position"],
        data["proposition_id"],
        data["subject_entity_id"],
        data["predicate_id"],
        data["object_entity_id"],
        operator,
        data["input_binding"],
        data["output_binding"],
        tuple(raw_filters),
        tuple(raw_aggregation),
        data["schema_version"],
    )
    return result


def proposition_evidence_record(
    proposition_id: object,
    source_resolver: object,
    source_contributions: object,
    features: object,
    canonical_references: object,
    validity: object,
    trust: object,
    disclosure: object,
    path: object,
    selection_reasons: object,
    schema_version: object = 1,
) -> dict:
    """Build strict wire-safe full-Proposition evidence without unrestricted graph content."""
    version = require_int(schema_version, "schema_version", 1, PROPOSITION_EVIDENCE_RECORD_SCHEMA_VERSION)
    normalized_proposition_id = require_identifier(proposition_id, "Proposition evidence proposition_id")
    source = require_identifier(source_resolver, "Proposition evidence source_resolver", MAX_RESOLVER_NAME_BYTES)
    if not isinstance(source_contributions, tuple):
        raise InvalidRequestError("Proposition evidence source_contributions must be a tuple")
    contributions = tuple(
        require_identifier(value, "Proposition evidence source contribution", MAX_RESOLVER_NAME_BYTES)
        for value in source_contributions
    )
    if not contributions or len(contributions) > MAX_PROPOSITION_SOURCE_CONTRIBUTIONS:
        raise InvalidRequestError(
            f"Proposition evidence source_contributions must contain 1 through {MAX_PROPOSITION_SOURCE_CONTRIBUTIONS} values"
        )
    if contributions != tuple(sorted(set(contributions))):
        raise InvalidRequestError("Proposition evidence source_contributions must be unique and sorted")
    if source not in contributions:
        raise InvalidRequestError("Proposition evidence source_resolver must be present in source_contributions")
    try:
        validated_features = validate_feature_set(features)
    except InvalidRequestError as error:
        raise InvalidRequestError("Proposition evidence features must be a FeatureSet") from error
    try:
        validated_references = validate_canonical_proposition_references(canonical_references)
    except InvalidRequestError as error:
        raise InvalidRequestError("Proposition evidence canonical_references must be CanonicalPropositionReferences") from error
    try:
        validated_validity = validate_proposition_validity_inputs(validity)
    except InvalidRequestError as error:
        raise InvalidRequestError("Proposition evidence validity must be PropositionValidityInputs") from error
    try:
        validated_trust = validate_proposition_trust_inputs(trust)
    except InvalidRequestError as error:
        raise InvalidRequestError("Proposition evidence trust must be PropositionTrustInputs") from error
    try:
        validated_disclosure = validate_disclosure_decision(disclosure)
    except InvalidRequestError as error:
        raise InvalidRequestError("Proposition evidence disclosure must be DisclosureDecision") from error
    if not isinstance(path, tuple):
        raise InvalidRequestError("Proposition evidence path must be a tuple")
    if version == 1:
        normalized_path: tuple[object, ...] = tuple(
            require_identifier(value, "Proposition evidence path identifier") for value in path
        )
        if normalized_path != (normalized_proposition_id,):
            raise InvalidRequestError("Section 7 Proposition evidence path must be the singleton proposition_id")
    else:
        normalized_path = tuple(validate_proposition_evidence_path_step(value) for value in path)
        if not 1 <= len(normalized_path) <= MAX_COMPOSITION_PATH_PROPOSITIONS:
            raise InvalidRequestError(
                f"composed Proposition evidence path must contain 1 through {MAX_COMPOSITION_PATH_PROPOSITIONS} steps"
            )
        steps = normalized_path
        if tuple(step["position"] for step in steps) != tuple(range(len(steps))):
            raise InvalidRequestError("composed Proposition evidence path positions must be contiguous and ordered")
        proposition_ids = tuple(step["proposition_id"] for step in steps)
        if len(set(proposition_ids)) != len(proposition_ids) or normalized_proposition_id not in proposition_ids:
            raise InvalidRequestError(
                "composed Proposition evidence path must contain unique Propositions including proposition_id"
            )
        entity_ids = [steps[0]["subject_entity_id"]]
        for index, step in enumerate(steps):
            entity_ids.append(step["object_entity_id"])
            if index and (
                steps[index - 1]["object_entity_id"] != step["subject_entity_id"]
                or steps[index - 1]["output_binding"] != step["input_binding"]
            ):
                raise InvalidRequestError("composed Proposition evidence path bindings are not contiguous")
        if len(set(entity_ids)) != len(entity_ids):
            raise InvalidRequestError("composed Proposition evidence path contains a cycle")
    if not isinstance(selection_reasons, tuple):
        raise InvalidRequestError("Proposition evidence selection_reasons must be a tuple")
    reasons = tuple(
        require_identifier(value, "Proposition evidence selection reason", MAX_REASON_CODE_BYTES) for value in selection_reasons
    )
    if not reasons or len(reasons) > MAX_PROPOSITION_SELECTION_REASONS:
        raise InvalidRequestError(
            f"Proposition evidence selection_reasons must contain 1 through {MAX_PROPOSITION_SELECTION_REASONS} values"
        )
    if reasons != tuple(sorted(set(reasons))):
        raise InvalidRequestError("Proposition evidence selection_reasons must be unique and sorted")
    result: dict = {
        "schema_version": version,
        "proposition_id": normalized_proposition_id,
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


def validate_proposition_evidence_record(value: object) -> dict:
    """Revalidate and defensively copy one full-Proposition evidence dictionary."""
    data = exact_mapping(value, "PropositionEvidenceRecord", PROPOSITION_EVIDENCE_RECORD_FIELDS)
    result = proposition_evidence_record(
        data["proposition_id"],
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


def proposition_evidence_record_with_changes(value: object, changes: object) -> dict:
    """Apply named fields and revalidate one full-Proposition evidence dictionary."""
    record = validate_proposition_evidence_record(value)
    if not isinstance(changes, dict):
        raise InvalidRequestError("Proposition evidence changes must be an object")
    if not set(changes).issubset(PROPOSITION_EVIDENCE_RECORD_FIELDS):
        raise InvalidRequestError("Proposition evidence changes contain an unknown field")
    updated: dict[str, object] = dict(record)
    updated.update(changes)
    result = validate_proposition_evidence_record(updated)
    return result


def proposition_evidence_record_to_dict(value: object) -> dict[str, object]:
    """Serialize one full-Proposition evidence record."""
    record = validate_proposition_evidence_record(value)
    result = trusted_proposition_evidence_record_to_dict(record)
    return result


def trusted_proposition_evidence_record_to_dict(record: dict) -> dict[str, object]:
    """Serialize a Proposition-evidence record already validated at a public boundary."""
    result = {
        "schema_version": record.get("schema_version", 0),
        "proposition_id": record.get("proposition_id", ""),
        "source_resolver": record.get("source_resolver", ""),
        "source_contributions": list(record.get("source_contributions", ())),
        "features": {
            "schema_version": record.get("features", {})["schema_version"],
            "values": dict(record.get("features", {})["values"]),
            "unavailable": list(record.get("features", {})["unavailable"]),
        },
        "canonical_references": dict(record.get("canonical_references", {})),
        "validity": proposition_validity_inputs_to_dict(record.get("validity", {})),
        "trust": dict(record.get("trust", {})),
        "disclosure": {
            "schema_version": record.get("disclosure", {})["schema_version"],
            "ownership": record.get("disclosure", {})["ownership"].value,
            "basis": record.get("disclosure", {})["basis"].value,
            "scope": dict(record.get("disclosure", {})["scope"]),
            "policy_version": record.get("disclosure", {})["policy_version"],
            "authority": record.get("disclosure", {})["authority"],
            "authority_available": record.get("disclosure", {})["authority_available"],
        },
        "path": (
            list(record.get("path", ()))
            if record.get("schema_version", 0) == 1
            else [proposition_evidence_path_step_to_dict(step) for step in record.get("path", ())]
        ),
        "selection_reasons": list(record.get("selection_reasons", ())),
    }
    return result


def proposition_evidence_record_from_dict(value: object) -> dict:
    """Decode one full-Proposition evidence record from its exact serialized form."""
    data = exact_mapping(value, "PropositionEvidenceRecord", PROPOSITION_EVIDENCE_RECORD_FIELDS)
    contributions = require_list(data["source_contributions"], "Proposition evidence source_contributions")
    path = require_list(data["path"], "Proposition evidence path")
    reasons = require_list(data["selection_reasons"], "Proposition evidence selection_reasons")
    normalized_contributions = tuple(
        require_identifier(item, "Proposition evidence source contribution", MAX_RESOLVER_NAME_BYTES) for item in contributions
    )
    version = require_int(data["schema_version"], "schema_version", 1, PROPOSITION_EVIDENCE_RECORD_SCHEMA_VERSION)
    normalized_path: tuple[object, ...]
    if version == 1:
        normalized_path = tuple(require_identifier(item, "Proposition evidence path identifier") for item in path)
    else:
        normalized_path = tuple(
            proposition_evidence_path_step_from_dict(freeze_mapping(item, "Proposition evidence path step")) for item in path
        )
    normalized_reasons = tuple(
        require_identifier(item, "Proposition evidence selection reason", MAX_REASON_CODE_BYTES) for item in reasons
    )
    validated_features = feature_set_from_dict(freeze_mapping(data["features"], "Proposition evidence features"))
    validated_references = canonical_proposition_references_from_dict(
        freeze_mapping(data["canonical_references"], "Proposition evidence canonical_references")
    )
    validated_validity = proposition_validity_inputs_from_dict(freeze_mapping(data["validity"], "Proposition evidence validity"))
    validated_trust = proposition_trust_inputs_from_dict(freeze_mapping(data["trust"], "Proposition evidence trust"))
    validated_disclosure = disclosure_decision_from_dict(freeze_mapping(data["disclosure"], "Proposition evidence disclosure"))
    result = proposition_evidence_record(
        data["proposition_id"],
        data["source_resolver"],
        normalized_contributions,
        validated_features,
        validated_references,
        validated_validity,
        validated_trust,
        validated_disclosure,
        normalized_path,
        normalized_reasons,
        version,
    )
    return result


def proposition_evidence_record_to_json(value: object) -> str:
    """Serialize one full-Proposition evidence record deterministically."""
    payload = proposition_evidence_record_to_dict(value)
    result = json_text(payload)
    return result


def proposition_evidence_record_from_json(value: object) -> dict:
    """Decode one full-Proposition evidence record from deterministic JSON."""
    if not isinstance(value, str):
        raise InvalidRequestError("PropositionEvidenceRecord JSON must be a string")
    data = load_json_mapping(value, "PropositionEvidenceRecord JSON")
    result = proposition_evidence_record_from_dict(data)
    return result


def evidence_package_payload(value: dict) -> dict[str, object]:
    result = {
        "wire_version": value.get("wire_version", 0),
        "records": [trusted_proposition_evidence_record_to_dict(record) for record in value.get("records", ())],
        "retained_count": value.get("retained_count", 0),
        "omitted_count": value.get("omitted_count", 0),
        "truncated": value.get("truncated", False),
        "truncation_reasons": [reason.value for reason in value.get("truncation_reasons", ())],
    }
    return result


def evidence_package(
    records: object,
    retained_count: object,
    omitted_count: object,
    truncated: object,
    truncation_reasons: object,
    wire_version: object = EVIDENCE_PACKAGE_WIRE_VERSION,
) -> dict:
    """Build one canonical, count- and byte-bounded full-Proposition package."""
    version = require_int(wire_version, "wire_version", 1, EVIDENCE_PACKAGE_WIRE_VERSION)
    if not isinstance(records, tuple):
        raise InvalidRequestError("evidence package records must be a tuple of PropositionEvidenceRecord values")
    try:
        validated_records = tuple(validate_proposition_evidence_record(record) for record in records)
    except InvalidRequestError as error:
        raise InvalidRequestError("evidence package records must be a tuple of PropositionEvidenceRecord values") from error
    if version == 1 and any(record["schema_version"] != 1 for record in validated_records):
        raise InvalidRequestError("evidence package wire_version 1 cannot contain composed Proposition paths")
    if len(validated_records) > MAX_EVIDENCE_PACKAGE_RECORDS:
        raise InvalidRequestError(f"evidence package records exceeds the limit of {MAX_EVIDENCE_PACKAGE_RECORDS}")
    identifiers = tuple(record["proposition_id"] for record in validated_records)
    if identifiers != tuple(sorted(identifiers)):
        raise InvalidRequestError("evidence package records must use canonical Proposition-ID order")
    if len(set(identifiers)) != len(identifiers):
        raise InvalidRequestError("evidence package records must have unique Proposition IDs")
    retained = require_int(retained_count, "evidence package retained_count", 0, 2_147_483_647)
    omitted = require_int(omitted_count, "evidence package omitted_count", 0, 2_147_483_647)
    if retained != len(validated_records):
        raise InvalidRequestError("evidence package retained_count must equal the number of records")
    normalized_truncated = require_bool(truncated, "evidence package truncated")
    if not isinstance(truncation_reasons, tuple):
        raise InvalidRequestError("evidence package truncation_reasons must be a tuple")
    if len(truncation_reasons) > MAX_EVIDENCE_PACKAGE_TRUNCATION_REASONS:
        raise InvalidRequestError(
            f"evidence package truncation_reasons exceeds the limit of {MAX_EVIDENCE_PACKAGE_TRUNCATION_REASONS}"
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
    result: dict = {
        "wire_version": version,
        "records": validated_records,
        "retained_count": retained,
        "omitted_count": omitted,
        "truncated": normalized_truncated,
        "truncation_reasons": normalized_reasons,
    }
    encoded = json_text(evidence_package_payload(result)).encode("utf-8")
    if len(encoded) > MAX_EVIDENCE_PACKAGE_BYTES:
        raise InvalidRequestError(f"evidence package exceeds the limit of {MAX_EVIDENCE_PACKAGE_BYTES} UTF-8 bytes")
    return result


def empty_evidence_package() -> dict:
    """Return one isolated concrete empty evidence package."""
    result = trusted_evidence_package((), 0, ())
    return result


def trusted_evidence_package(
    records: tuple[dict, ...],
    omitted_count: int,
    truncation_reasons: tuple[EvidencePackageTruncationReason, ...],
) -> dict:
    """Build a package from canonical, validated, byte-bounded records."""
    result: dict = {
        "wire_version": EVIDENCE_PACKAGE_WIRE_VERSION,
        "records": records,
        "retained_count": len(records),
        "omitted_count": omitted_count,
        "truncated": omitted_count > 0,
        "truncation_reasons": truncation_reasons,
    }
    return result


def validate_evidence_package(value: object) -> dict:
    """Revalidate and defensively copy one evidence-package dictionary."""
    data = exact_mapping(value, "EvidencePackage", EVIDENCE_PACKAGE_FIELDS)
    result = evidence_package(
        data["records"],
        data["retained_count"],
        data["omitted_count"],
        data["truncated"],
        data["truncation_reasons"],
        data["wire_version"],
    )
    return result


def evidence_package_with_changes(value: object, changes: object) -> dict:
    """Apply named fields and revalidate one evidence-package dictionary."""
    package = validate_evidence_package(value)
    if not isinstance(changes, dict):
        raise InvalidRequestError("evidence package changes must be an object")
    if not set(changes).issubset(EVIDENCE_PACKAGE_FIELDS):
        raise InvalidRequestError("evidence package changes contain an unknown field")
    updated: dict[str, object] = dict(package)
    updated.update(changes)
    result = validate_evidence_package(updated)
    return result


def canonical_proposition_evidence_records(records: object) -> tuple[tuple[dict, ...], int]:
    if not isinstance(records, tuple):
        raise InvalidRequestError("evidence package input must be a tuple of PropositionEvidenceRecord values")
    if len(records) > MAX_EVIDENCE_PACKAGE_INPUT_RECORDS:
        raise InvalidRequestError(f"evidence package input exceeds the limit of {MAX_EVIDENCE_PACKAGE_INPUT_RECORDS}")
    try:
        validated_records = tuple(validate_proposition_evidence_record(record) for record in records)
    except InvalidRequestError as error:
        raise InvalidRequestError("evidence package input must be a tuple of PropositionEvidenceRecord values") from error
    by_proposition_id: dict[str, dict] = {}
    duplicate_count = 0
    for record in validated_records:
        proposition_id = record["proposition_id"]
        if proposition_id in by_proposition_id:
            previous = by_proposition_id.get(proposition_id, {})
            if previous != record:
                raise InvalidRequestError(f"conflicting Proposition evidence projections for Proposition ID: {proposition_id}")
            duplicate_count += 1
            continue
        by_proposition_id[proposition_id] = record
    canonical = tuple(by_proposition_id.get(proposition_id, {}) for proposition_id in sorted(by_proposition_id))
    result = (canonical, duplicate_count)
    return result


def build_evidence_package(
    records: object,
    *,
    max_records: object = MAX_EVIDENCE_PACKAGE_RECORDS,
    max_bytes: object = MAX_EVIDENCE_PACKAGE_BYTES,
) -> dict:
    """Canonicalize, deduplicate, and fit full-Proposition evidence to configured bounds."""
    retained_limit = require_int(max_records, "evidence package max_records", 0, MAX_EVIDENCE_PACKAGE_RECORDS)
    byte_limit = require_int(max_bytes, "evidence package max_bytes", 256, MAX_EVIDENCE_PACKAGE_BYTES)
    canonical, duplicate_count = canonical_proposition_evidence_records(records)
    reasons: set[EvidencePackageTruncationReason] = set()
    omitted = duplicate_count
    if duplicate_count:
        reasons.add(EvidencePackageTruncationReason.DUPLICATE_PROPOSITION_ID)
    retained = canonical[:retained_limit]
    if len(canonical) > retained_limit:
        omitted += len(canonical) - retained_limit
        reasons.add(EvidencePackageTruncationReason.RECORD_LIMIT)

    while True:
        ordered_reasons = tuple(sorted(reasons, key=lambda reason: reason.value))
        candidate: dict = {
            "wire_version": EVIDENCE_PACKAGE_WIRE_VERSION,
            "records": retained,
            "retained_count": len(retained),
            "omitted_count": omitted,
            "truncated": omitted > 0,
            "truncation_reasons": ordered_reasons,
        }
        payload = evidence_package_payload(candidate)
        if len(json_text(payload).encode("utf-8")) <= byte_limit:
            result = trusted_evidence_package(retained, omitted, ordered_reasons)
            break
        if not retained:
            raise InvalidRequestError("evidence package max_bytes cannot contain the empty package envelope")
        retained = retained[:-1]
        omitted += 1
        reasons.add(EvidencePackageTruncationReason.SERIALIZED_SIZE_LIMIT)
    return result


def evidence_package_to_dict(value: object) -> dict:
    """Serialize one evidence package."""
    package = validate_evidence_package(value)
    result = evidence_package_payload(package)
    return result


def evidence_package_from_dict(value: object) -> dict:
    """Decode one evidence package from its exact serialized form."""
    data = exact_mapping(value, "EvidencePackage", EVIDENCE_PACKAGE_FIELDS)
    records = require_list(data["records"], "evidence package records")
    if len(records) > MAX_EVIDENCE_PACKAGE_RECORDS:
        raise InvalidRequestError(f"evidence package records exceeds the limit of {MAX_EVIDENCE_PACKAGE_RECORDS}")
    raw_reasons = require_list(data["truncation_reasons"], "evidence package truncation_reasons")
    reasons = []
    for reason_value in raw_reasons:
        try:
            reason = EvidencePackageTruncationReason(
                require_text(
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
        proposition_evidence_record_from_dict(freeze_mapping(record, "evidence package record")) for record in records
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
    result = json_text(payload)
    return result


def trusted_evidence_package_to_json(value: dict) -> str:
    """Serialize an orchestrator-owned evidence package."""
    payload = evidence_package_payload(value)
    result = json_text(payload)
    return result


def evidence_package_from_json(value: str) -> dict:
    """Decode one bounded evidence package from deterministic JSON."""
    if not isinstance(value, str):
        raise InvalidRequestError("EvidencePackage JSON must be a string")
    if len(value.encode("utf-8")) > MAX_EVIDENCE_PACKAGE_BYTES:
        raise InvalidRequestError(f"evidence package exceeds the limit of {MAX_EVIDENCE_PACKAGE_BYTES} UTF-8 bytes")
    data = load_json_mapping(value, "EvidencePackage JSON")
    result = evidence_package_from_dict(data)
    return result


def evidence_reference(
    evidence_id: object,
    resolver: object,
    kind: object,
    scope: object,
    provenance: object = EMPTY_MAPPING,
    diagnostics: object = EMPTY_MAPPING,
    schema_version: object = EVIDENCE_REFERENCE_SCHEMA_VERSION,
) -> dict:
    """Build one minimal stable evidence reference safe for Section 4 results."""
    version = require_int(schema_version, "schema_version", 0, 2_147_483_647)
    if version != EVIDENCE_REFERENCE_SCHEMA_VERSION:
        raise InvalidRequestError(f"unsupported evidence reference schema_version: {version}")
    normalized_id = require_text(evidence_id, "evidence_id", 256, allow_empty=False)
    normalized_resolver = require_text(resolver, "evidence resolver", MAX_RESOLVER_NAME_BYTES, allow_empty=False)
    if not isinstance(kind, EvidenceKind):
        raise InvalidRequestError("evidence kind must be an EvidenceKind")
    try:
        validated_scope = validate_scope_key(scope)
    except IdentityValidationError as error:
        raise InvalidRequestError("evidence scope must be a ScopeKey") from error
    result: dict = {
        "schema_version": version,
        "evidence_id": normalized_id,
        "resolver": normalized_resolver,
        "kind": kind,
        "scope": validated_scope,
        "provenance": freeze_mapping(provenance, "evidence provenance"),
        "diagnostics": freeze_mapping(diagnostics, "evidence diagnostics"),
    }
    return result


def validate_evidence_reference(value: object) -> dict:
    """Revalidate and defensively copy one evidence-reference dictionary."""
    data = exact_mapping(value, "EvidenceReference", EVIDENCE_REFERENCE_FIELDS)
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


def evidence_reference_with_changes(value: object, changes: object) -> dict:
    reference = validate_evidence_reference(value)
    if not isinstance(changes, dict):
        raise InvalidRequestError("evidence reference changes must be an object")
    if not set(changes).issubset(EVIDENCE_REFERENCE_FIELDS):
        raise InvalidRequestError("evidence reference changes contain an unknown field")
    updated: dict[str, object] = dict(reference)
    updated.update(changes)
    result = validate_evidence_reference(updated)
    return result


def evidence_reference_to_dict(value: object) -> dict[str, object]:
    """Serialize one evidence reference."""
    reference = validate_evidence_reference(value)
    result = trusted_evidence_reference_to_dict(reference)
    return result


def trusted_evidence_reference_to_dict(reference: dict) -> dict[str, object]:
    """Serialize an evidence reference already validated at a public boundary."""
    result = {
        "schema_version": reference.get("schema_version", 0),
        "evidence_id": reference.get("evidence_id", ""),
        "resolver": reference.get("resolver", {}),
        "kind": reference.get("kind", EvidenceKind.PROPOSITION).value,
        "scope": scope_key_to_dict(reference.get("scope", {})),
        "provenance": thaw_json(reference.get("provenance", {})),
        "diagnostics": thaw_json(reference.get("diagnostics", {})),
    }
    return result


def evidence_reference_from_dict(value: object) -> dict:
    """Decode one evidence reference from its exact serialized form."""
    data = exact_mapping(value, "EvidenceReference", EVIDENCE_REFERENCE_FIELDS)
    try:
        kind = EvidenceKind(require_text(data["kind"], "evidence kind", 32, allow_empty=False))
    except ValueError as error:
        raise InvalidRequestError("unsupported evidence kind") from error
    result = evidence_reference(
        data["evidence_id"],
        data["resolver"],
        kind,
        scope_key_from_dict(freeze_mapping(data["scope"], "evidence scope")),
        freeze_mapping(data["provenance"], "evidence provenance"),
        freeze_mapping(data["diagnostics"], "evidence diagnostics"),
        data["schema_version"],
    )
    return result


def evidence_reference_to_json(value: object) -> str:
    payload = evidence_reference_to_dict(value)
    result = json_text(payload)
    return result


def evidence_reference_from_json(value: str) -> dict:
    data = load_json_mapping(value, "EvidenceReference JSON")
    result = evidence_reference_from_dict(data)
    return result


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
) -> dict:
    """Build one response candidate emitted by a resolver without selecting it."""
    version = require_int(schema_version, "schema_version", 0, MAX_RESOURCE_COUNTER)
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
    result: dict = {
        "schema_version": version,
        "candidate_id": require_text(candidate_id, "candidate_id", MAX_CANDIDATE_ID_BYTES, allow_empty=False),
        "statement_id": require_text(
            statement_id,
            "candidate statement_id",
            MAX_STATEMENT_ID_BYTES,
            allow_empty=False,
        ),
        "response": require_text(response, "candidate response", MAX_RESPONSE_BYTES, allow_empty=False),
        "source": source,
        "features": validated_features,
        "evidence": validated_evidence,
        "scope": validated_scope,
        "lifecycle": lifecycle,
        "provenance": freeze_mapping(provenance, "candidate provenance"),
        "diagnostics": freeze_mapping(diagnostics, "candidate diagnostics"),
    }
    return result


def validate_candidate(value: object) -> dict:
    data = exact_mapping(value, "Candidate", CANDIDATE_FIELDS)
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


def trusted_candidate(
    candidate_id: str,
    statement_id: str,
    response: str,
    source: CandidateSource,
    features: dict,
    evidence: tuple[dict, ...],
    scope: dict,
    lifecycle: LifecycleState,
    provenance: dict[str, object] = EMPTY_MAPPING,
    diagnostics: dict[str, object] = EMPTY_MAPPING,
) -> dict:
    """Build a candidate from values established inside the resolution engine."""
    result: dict = {
        "schema_version": CANDIDATE_SCHEMA_VERSION,
        "candidate_id": candidate_id,
        "statement_id": statement_id,
        "response": response,
        "source": source,
        "features": features,
        "evidence": evidence,
        "scope": scope,
        "lifecycle": lifecycle,
        "provenance": freeze_mapping(provenance, "candidate provenance"),
        "diagnostics": freeze_mapping(diagnostics, "candidate diagnostics"),
    }
    return result


def candidate_with_changes(value: object, changes: object) -> dict:
    current = validate_candidate(value)
    if not isinstance(changes, dict):
        raise InvalidRequestError("candidate changes must be an object")
    if not set(changes).issubset(CANDIDATE_FIELDS):
        raise InvalidRequestError("candidate changes contain an unknown field")
    updated: dict[str, object] = dict(current)
    updated.update(changes)
    result = validate_candidate(updated)
    return result


def trusted_candidate_with_changes(value: dict, changes: dict[str, object]) -> dict:
    """Copy an engine-owned candidate and apply engine-established fields."""
    updated: dict[str, object] = dict(value)
    updated.update(changes)
    result = updated
    return result


def candidate_to_dict(value: object) -> dict[str, object]:
    current = validate_candidate(value)
    result = trusted_candidate_to_dict(current)
    return result


def trusted_candidate_to_dict(current: dict) -> dict[str, object]:
    """Serialize a candidate already validated at a public boundary."""
    result = {
        "schema_version": current.get("schema_version", 0),
        "candidate_id": current.get("candidate_id", ""),
        "statement_id": current.get("statement_id", ""),
        "response": current.get("response", ""),
        "source": current.get("source", CandidateSource.EXACT).value,
        "features": {
            "schema_version": current.get("features", {})["schema_version"],
            "values": dict(current.get("features", {})["values"]),
            "unavailable": list(current.get("features", {})["unavailable"]),
        },
        "evidence": [trusted_evidence_reference_to_dict(item) for item in current.get("evidence", ())],
        "scope": dict(current.get("scope", {})),
        "lifecycle": current.get("lifecycle", LifecycleState.RETIRED).value,
        "provenance": thaw_json(current.get("provenance", {})),
        "diagnostics": thaw_json(current.get("diagnostics", {})),
    }
    return result


def candidate_from_dict(value: object) -> dict:
    data = exact_mapping(value, "Candidate", CANDIDATE_FIELDS)
    try:
        source = CandidateSource(require_text(data["source"], "candidate source", 32, allow_empty=False))
        lifecycle = LifecycleState(require_text(data["lifecycle"], "candidate lifecycle", 32, allow_empty=False))
    except ValueError as error:
        raise InvalidRequestError("candidate source or lifecycle is unsupported") from error
    evidence = require_list(data["evidence"], "candidate evidence")
    result = candidate(
        schema_version=data["schema_version"],
        candidate_id=data["candidate_id"],
        statement_id=data["statement_id"],
        response=data["response"],
        source=source,
        features=feature_set_from_dict(freeze_mapping(data["features"], "candidate features")),
        evidence=tuple(evidence_reference_from_dict(freeze_mapping(item, "candidate evidence item")) for item in evidence),
        scope=scope_key_from_dict(freeze_mapping(data["scope"], "candidate scope")),
        lifecycle=lifecycle,
        provenance=freeze_mapping(data["provenance"], "candidate provenance"),
        diagnostics=freeze_mapping(data["diagnostics"], "candidate diagnostics"),
    )
    return result


def candidate_to_json(value: object) -> str:
    payload = candidate_to_dict(value)
    result = json_text(payload)
    return result


def trusted_candidate_to_json(value: dict) -> str:
    """Serialize a candidate already validated at a public boundary."""
    payload = trusted_candidate_to_dict(value)
    result = json_text(payload)
    return result


def candidate_from_json(value: str) -> dict:
    data = load_json_mapping(value, "Candidate JSON")
    result = candidate_from_dict(data)
    return result


def empty_candidate() -> dict:
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


def accounting_observation(
    statement_id: object,
    keywords: object = (),
    schema_version: object = ACCOUNTING_OBSERVATION_SCHEMA_VERSION,
) -> dict:
    """Build one pure resolver observation applied only by the finalizer."""
    version = require_int(schema_version, "schema_version", 0, MAX_RESOURCE_COUNTER)
    if version != ACCOUNTING_OBSERVATION_SCHEMA_VERSION:
        raise InvalidRequestError(f"unsupported accounting observation schema_version: {version}")
    if not isinstance(keywords, tuple):
        raise InvalidRequestError("accounting keywords must be a tuple")
    if len(keywords) > MAX_ACCOUNTING_KEYWORDS:
        raise InvalidRequestError(f"accounting keywords exceeds the limit of {MAX_ACCOUNTING_KEYWORDS}")
    normalized_keywords = tuple(
        require_text(keyword, "accounting keyword", MAX_ACCOUNTING_KEYWORD_BYTES, allow_empty=False) for keyword in keywords
    )
    if len(set(normalized_keywords)) != len(normalized_keywords):
        raise InvalidRequestError("accounting keywords must be unique")
    result: dict = {
        "schema_version": version,
        "statement_id": require_text(
            statement_id,
            "accounting statement_id",
            MAX_STATEMENT_ID_BYTES,
            allow_empty=False,
        ),
        "keywords": normalized_keywords,
    }
    return result


def validate_accounting_observation(value: object) -> dict:
    data = exact_mapping(value, "AccountingObservation", ACCOUNTING_OBSERVATION_FIELDS)
    result = accounting_observation(data["statement_id"], data["keywords"], data["schema_version"])
    return result


def accounting_observation_with_changes(value: object, changes: object) -> dict:
    observation = validate_accounting_observation(value)
    if not isinstance(changes, dict):
        raise InvalidRequestError("accounting observation changes must be an object")
    if not set(changes).issubset(ACCOUNTING_OBSERVATION_FIELDS):
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


def accounting_observation_from_dict(value: object) -> dict:
    data = exact_mapping(value, "AccountingObservation", ACCOUNTING_OBSERVATION_FIELDS)
    keywords = require_list(data["keywords"], "accounting keywords")
    result = accounting_observation(data["statement_id"], tuple(keywords), data["schema_version"])
    return result


def resolver_result(
    resolver: object,
    state: object,
    reason_code: object = "",
    candidates: object = (),
    evidence: object = (),
    proposition_evidence: object = (),
    accounting: object = (),
    diagnostics: object = EMPTY_MAPPING,
    consumption: object = EMPTY_MAPPING,
    schema_version: object = RESOLVER_RESULT_SCHEMA_VERSION,
) -> dict:
    """Build the bounded output from one side-effect-free resolver invocation."""
    validated_schema_version = require_int(schema_version, "schema_version", 0, MAX_RESOURCE_COUNTER)
    if validated_schema_version != RESOLVER_RESULT_SCHEMA_VERSION:
        raise InvalidRequestError(f"unsupported resolver result schema_version: {validated_schema_version}")
    resolver_name = require_text(resolver, "resolver result resolver", MAX_RESOLVER_NAME_BYTES, allow_empty=False)
    if not isinstance(state, ResolverState):
        raise InvalidRequestError("resolver result state must be a ResolverState")
    validated_reason_code = require_text(
        reason_code,
        "resolver result reason_code",
        MAX_REASON_CODE_BYTES,
        allow_empty=True,
    )
    if (
        not isinstance(candidates, tuple)
        or not isinstance(evidence, tuple)
        or not isinstance(proposition_evidence, tuple)
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
        validated_proposition_evidence = tuple(validate_proposition_evidence_record(value) for value in proposition_evidence)
    except InvalidRequestError as error:
        raise InvalidRequestError("resolver proposition_evidence must be a tuple of PropositionEvidenceRecord values") from error
    try:
        validated_accounting = tuple(validate_accounting_observation(value) for value in accounting)
    except InvalidRequestError as error:
        raise InvalidRequestError("resolver accounting must be a tuple of AccountingObservation values") from error
    if state != ResolverState.COMPLETED and (
        validated_candidates or validated_evidence or validated_proposition_evidence or validated_accounting
    ):
        raise InvalidRequestError("non-completed resolver results cannot contain output or accounting")
    if any(
        value["source_resolver"] != resolver_name or value["source_contributions"] != (resolver_name,)
        for value in validated_proposition_evidence
    ):
        raise InvalidRequestError("resolver proposition_evidence source must match its producing resolver")
    if (
        max(
            len(validated_candidates),
            len(validated_evidence),
            len(validated_proposition_evidence),
            len(validated_accounting),
        )
        > MAX_RESOLUTION_VALUES
    ):
        raise InvalidRequestError(f"resolver output exceeds the item limit of {MAX_RESOLUTION_VALUES}")
    frozen_diagnostics = freeze_mapping(diagnostics, "resolver diagnostics")
    if not isinstance(consumption, dict):
        raise InvalidRequestError("resolver consumption must be a BudgetConsumption")
    try:
        validated_consumption = budget_consumption() if not consumption else validate_budget_consumption(consumption)
    except InvalidRequestError as error:
        raise InvalidRequestError("resolver consumption must be a BudgetConsumption") from error
    result: dict = {
        "resolver": resolver_name,
        "state": state,
        "reason_code": validated_reason_code,
        "candidates": validated_candidates,
        "evidence": validated_evidence,
        "proposition_evidence": validated_proposition_evidence,
        "accounting": validated_accounting,
        "diagnostics": frozen_diagnostics,
        "consumption": validated_consumption,
        "schema_version": validated_schema_version,
    }
    return result


def validate_resolver_result(value: object) -> dict:
    data = exact_mapping(value, "ResolverResult", RESOLVER_RESULT_FIELDS)
    result = resolver_result(
        data["resolver"],
        data["state"],
        data["reason_code"],
        data["candidates"],
        data["evidence"],
        data["proposition_evidence"],
        data["accounting"],
        data["diagnostics"],
        data["consumption"],
        data["schema_version"],
    )
    return result


def resolver_result_with_changes(value: object, changes: object) -> dict:
    current = validate_resolver_result(value)
    if not isinstance(changes, dict) or not set(changes).issubset(RESOLVER_RESULT_FIELDS):
        raise InvalidRequestError("resolver result changes contain invalid fields")
    updated: dict[str, object] = dict(current)
    updated.update(changes)
    result = validate_resolver_result(updated)
    return result


def trusted_resolver_result_with_changes(
    value: dict,
    changes: dict[str, object],
) -> dict:
    """Copy an executor-owned resolver result and apply orchestrator fields."""
    updated: dict[str, object] = dict(value)
    updated.update(changes)
    result = updated
    return result


def resolver_result_to_dict(value: object) -> dict[str, object]:
    current = validate_resolver_result(value)
    result = trusted_resolver_result_to_dict(current)
    return result


def trusted_resolver_result_to_dict(current: dict) -> dict[str, object]:
    """Serialize a resolver result already validated by the executor."""
    result = {
        "schema_version": current.get("schema_version", 0),
        "resolver": current.get("resolver", {}),
        "state": current.get("state", ResolverState.FAILED).value,
        "reason_code": current.get("reason_code", ""),
        "candidates": [trusted_candidate_to_dict(item) for item in current.get("candidates", ())],
        "evidence": [trusted_evidence_reference_to_dict(item) for item in current.get("evidence", ())],
        "proposition_evidence": [
            trusted_proposition_evidence_record_to_dict(item) for item in current.get("proposition_evidence", ())
        ],
        "accounting": [accounting_observation_to_dict(item) for item in current.get("accounting", {})],
        "diagnostics": thaw_json(current.get("diagnostics", {})),
        "consumption": budget_consumption_to_dict(current.get("consumption", {})),
    }
    return result


def resolver_result_to_json(value: object) -> str:
    payload = resolver_result_to_dict(value)
    result = json_text(payload)
    return result


def resolver_result_from_dict(value: object) -> dict:
    data = exact_mapping(value, "ResolverResult", RESOLVER_RESULT_FIELDS)
    try:
        state = ResolverState(require_text(data["state"], "resolver state", 32, allow_empty=False))
    except ValueError as error:
        raise InvalidRequestError("unsupported resolver state") from error
    candidates = require_list(data["candidates"], "resolver candidates")
    evidence = require_list(data["evidence"], "resolver evidence")
    proposition_evidence = require_list(data["proposition_evidence"], "resolver Proposition evidence")
    accounting = require_list(data["accounting"], "resolver accounting")
    result = resolver_result(
        schema_version=require_int(data["schema_version"], "schema_version", 1, 1),
        resolver=require_text(data["resolver"], "resolver result resolver", MAX_RESOLVER_NAME_BYTES, allow_empty=False),
        state=state,
        reason_code=require_text(data["reason_code"], "resolver result reason_code", MAX_REASON_CODE_BYTES, allow_empty=True),
        candidates=tuple(candidate_from_dict(freeze_mapping(item, "resolver candidate")) for item in candidates),
        evidence=tuple(evidence_reference_from_dict(freeze_mapping(item, "resolver evidence item")) for item in evidence),
        proposition_evidence=tuple(
            proposition_evidence_record_from_dict(freeze_mapping(item, "resolver Proposition evidence item"))
            for item in proposition_evidence
        ),
        accounting=tuple(accounting_observation_from_dict(freeze_mapping(item, "resolver accounting item")) for item in accounting),
        diagnostics=freeze_mapping(data["diagnostics"], "resolver diagnostics"),
        consumption=budget_consumption_from_dict(freeze_mapping(data["consumption"], "resolver consumption")),
    )
    return result


def resolver_result_from_json(value: str) -> dict:
    decoded = load_json_mapping(value, "ResolverResult JSON")
    result = resolver_result_from_dict(decoded)
    return result


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
) -> dict:
    """Build a strict unified result with concrete ANSWER/EVIDENCE/MISS invariants."""
    validated_schema_version = require_int(schema_version, "schema_version", 0, MAX_RESOURCE_COUNTER)
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
    validated_confidence = require_float(confidence, "resolution confidence", 0.0, 1.0)
    if not isinstance(reason_codes, tuple):
        raise InvalidRequestError("reason_codes must be a tuple")
    if len(reason_codes) > MAX_RESOLUTION_REASON_CODES:
        raise InvalidRequestError(f"reason_codes exceeds the limit of {MAX_RESOLUTION_REASON_CODES}")
    validated_reason_codes = tuple(
        require_text(value, "reason code", MAX_REASON_CODE_BYTES, allow_empty=False) for value in reason_codes
    )
    if validated_reason_codes != tuple(dict.fromkeys(validated_reason_codes)):
        raise InvalidRequestError("reason_codes must be unique and ordered")
    frozen_diagnostics = freeze_mapping(frame_diagnostics, "frame diagnostics")
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
    if any(value["proposition_evidence"] for value in validated_resolver_results):
        raise InvalidRequestError("resolution resolver_results cannot expose unpackaged Proposition evidence")
    try:
        validated_budget = validate_budget_consumption(budget)
    except InvalidRequestError as error:
        raise InvalidRequestError("resolution budget must be a BudgetConsumption") from error
    if not isinstance(evidence_package, dict):
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
    result: dict = {
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


def trusted_resolution_result(
    outcome: ResolutionOutcome,
    selected_candidate: dict,
    selected_candidate_available: bool,
    response_candidates: tuple[dict, ...],
    evidence: tuple[dict, ...],
    confidence: float,
    confidence_available: bool,
    reason_codes: tuple[str, ...],
    frame_diagnostics: dict,
    resolver_results: tuple[dict, ...],
    budget: dict,
    evidence_package_available: bool,
    evidence_package: dict,
) -> dict:
    """Build a result from values whose invariants the orchestrator established."""
    result: dict = {
        "outcome": outcome,
        "selected_candidate": trusted_candidate_with_changes(selected_candidate, {}),
        "selected_candidate_available": selected_candidate_available,
        "response_candidates": tuple(trusted_candidate_with_changes(value, {}) for value in response_candidates),
        "evidence": evidence,
        "confidence": confidence,
        "confidence_available": confidence_available,
        "reason_codes": reason_codes,
        "frame_diagnostics": freeze_mapping(frame_diagnostics, "frame diagnostics"),
        "resolver_results": resolver_results,
        "budget": budget,
        "evidence_package_available": evidence_package_available,
        "evidence_package": evidence_package,
        "schema_version": RESOLUTION_RESULT_SCHEMA_VERSION,
    }
    return result


def validate_resolution_result(value: object) -> dict:
    data = exact_mapping(value, "ResolutionResult", RESOLUTION_RESULT_FIELDS)
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


def resolution_result_with_changes(value: object, changes: object) -> dict:
    current = validate_resolution_result(value)
    if not isinstance(changes, dict) or not set(changes).issubset(RESOLUTION_RESULT_FIELDS):
        raise InvalidRequestError("resolution result changes contain invalid fields")
    updated: dict[str, object] = dict(current)
    updated.update(changes)
    result = validate_resolution_result(updated)
    return result


def resolution_result_to_dict(value: object) -> dict:
    current = validate_resolution_result(value)
    result = trusted_resolution_result_to_dict(current)
    return result


def trusted_resolution_result_to_dict(current: dict) -> dict:
    """Serialize a result already validated or built by the orchestrator."""
    selected = (
        trusted_candidate_to_dict(current.get("selected_candidate", {}))
        if current.get("selected_candidate_available", False)
        else {}
    )
    result = {
        "schema_version": current.get("schema_version", 0),
        "outcome": current.get("outcome", ResolutionOutcome.MISS).value,
        "selected_candidate": selected,
        "selected_candidate_available": current.get("selected_candidate_available", False),
        "response_candidates": [trusted_candidate_to_dict(item) for item in current.get("response_candidates", ())],
        "evidence": [trusted_evidence_reference_to_dict(item) for item in current.get("evidence", ())],
        "confidence": current.get("confidence", 0.0),
        "confidence_available": current.get("confidence_available", False),
        "reason_codes": list(current.get("reason_codes", ())),
        "frame_diagnostics": thaw_json(current.get("frame_diagnostics", {})),
        "resolver_results": [trusted_resolver_result_to_dict(item) for item in current.get("resolver_results", ())],
        "budget": budget_consumption_to_dict(current.get("budget", {})),
        "evidence_package_available": current.get("evidence_package_available", False),
        "evidence_package": evidence_package_payload(current.get("evidence_package", {})),
    }
    return result


def resolution_result_to_json(value: object) -> str:
    payload = resolution_result_to_dict(value)
    result = json_text(payload)
    return result


def trusted_resolution_result_to_json(value: dict) -> str:
    """Serialize an orchestrator-owned result without redundant revalidation."""
    payload = trusted_resolution_result_to_dict(value)
    result = json_text(payload)
    return result


def resolution_result_from_dict(value: object) -> dict:
    data = exact_mapping(value, "ResolutionResult", RESOLUTION_RESULT_FIELDS)
    try:
        outcome = ResolutionOutcome(require_text(data["outcome"], "resolution outcome", 32, allow_empty=False))
    except ValueError as error:
        raise InvalidRequestError("unsupported resolution outcome") from error
    if not isinstance(data["selected_candidate_available"], bool):
        raise InvalidRequestError("selected_candidate_available must be a boolean")
    selected_available = data["selected_candidate_available"]
    selected_mapping = freeze_mapping(data["selected_candidate"], "selected_candidate")
    selected = candidate_from_dict(selected_mapping) if selected_available else empty_candidate()
    if not selected_available and selected_mapping:
        raise InvalidRequestError("unavailable selected_candidate must be an empty object")
    if not isinstance(data["confidence_available"], bool):
        raise InvalidRequestError("confidence_available must be a boolean")
    response_candidates = require_list(data["response_candidates"], "response_candidates")
    evidence = require_list(data["evidence"], "resolution evidence")
    reasons = require_list(data["reason_codes"], "reason_codes")
    resolver_results = require_list(data["resolver_results"], "resolver_results")
    if not isinstance(data["evidence_package_available"], bool):
        raise InvalidRequestError("evidence_package_available must be a boolean")
    result = resolution_result(
        schema_version=require_int(data["schema_version"], "schema_version", 1, 1),
        outcome=outcome,
        selected_candidate=selected,
        selected_candidate_available=selected_available,
        response_candidates=tuple(candidate_from_dict(freeze_mapping(item, "response candidate")) for item in response_candidates),
        evidence=tuple(evidence_reference_from_dict(freeze_mapping(item, "resolution evidence item")) for item in evidence),
        confidence=require_float(data["confidence"], "resolution confidence", 0.0, 1.0),
        confidence_available=data["confidence_available"],
        reason_codes=tuple(require_text(item, "reason code", MAX_REASON_CODE_BYTES, allow_empty=False) for item in reasons),
        frame_diagnostics=freeze_mapping(data["frame_diagnostics"], "frame diagnostics"),
        resolver_results=tuple(resolver_result_from_dict(freeze_mapping(item, "resolver result")) for item in resolver_results),
        budget=budget_consumption_from_dict(freeze_mapping(data["budget"], "resolution budget")),
        evidence_package_available=data["evidence_package_available"],
        evidence_package=evidence_package_from_dict(freeze_mapping(data["evidence_package"], "evidence package")),
    )
    return result


def resolution_result_from_json(value: str) -> dict:
    decoded = load_json_mapping(value, "ResolutionResult JSON")
    result = resolution_result_from_dict(decoded)
    return result


class QueryFrameBuilder:
    """Trusted transport-neutral base-frame construction boundary."""

    def __init__(self, engram, monotonic_clock_ns: object, utc_clock: object) -> None:
        if not callable(monotonic_clock_ns) or not callable(utc_clock):
            raise InvalidRequestError("frame builder clocks must be callable")
        self.internal_engram = engram
        self.internal_monotonic_clock_ns = monotonic_clock_ns
        self.internal_utc_clock = utc_clock

    def build(
        self,
        request: str,
        scope: object = EMPTY_SCOPE_KEY,
        identity: dict[str, object] = EMPTY_MAPPING,
        required_metadata: dict[str, object] = EMPTY_MAPPING,
        required_source_label: str = "",
        diagnostic_seed: str = "",
        budget: dict[str, object] = EMPTY_MAPPING,
    ) -> dict:
        original = require_text(request, "request", MAX_REQUEST_BYTES, allow_empty=False)
        try:
            scope = validate_scope_key(scope)
        except IdentityValidationError as error:
            raise InvalidRequestError("scope must be a ScopeKey") from error
        if not isinstance(identity, dict):
            raise InvalidRequestError("identity must be an object")
        if not isinstance(budget, dict):
            raise InvalidRequestError("budget must be an object")
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
                selected_budget = recapture_resolution_budget(budget, self.internal_monotonic_clock_ns)
            except InvalidRequestError as error:
                raise InvalidRequestError("budget must be a ResolutionBudget") from error
        else:
            selected_budget = capture_resolution_budget(self.internal_monotonic_clock_ns)
        resolved = original
        if self.internal_engram.config["expand_contractions"]:
            resolved = expand_contractions(original, self.internal_engram.substitution_maps["contractions"])
        eligibility = EligibilityContextCapture(self.internal_utc_clock).capture_standalone(scope, True)
        seed = diagnostic_seed or f"{query_identity_to_json(selected_identity)}:{resolved}"
        require_text(seed, "diagnostic_seed", MAX_REQUEST_BYTES * 4, allow_empty=False)
        diagnostic_id = f"resolution:sha256:{hashlib_sha256(seed.encode('utf-8')).hexdigest()}"
        result = query_frame(
            original_text=original,
            resolved_text=resolved,
            identity=selected_identity,
            expected_object_type=ExpectedObjectType.UNKNOWN,
            temporal_query_value=parse_temporal_query(original),
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

    def __init__(self, budget: dict) -> None:
        try:
            validated_budget = validate_resolution_budget(budget)
        except InvalidRequestError as error:
            raise InvalidRequestError("ledger budget must be a ResolutionBudget") from error
        self.internal_budget = validated_budget
        self.internal_lock = threading_RLock()
        self.totals = budget_consumption()

    @property
    def budget(self) -> dict:
        result = validate_resolution_budget(self.internal_budget)
        return result

    def remaining_candidates(self) -> int:
        with self.internal_lock:
            result = max(0, self.internal_budget["max_candidates"] - self.totals["candidates"])
            return result

    def remaining_evidence(self) -> int:
        with self.internal_lock:
            result = max(0, self.internal_budget["max_evidence"] - self.totals["evidence"])
            return result

    def add(self, consumption: dict) -> dict:
        try:
            validated_consumption = validate_budget_consumption(consumption)
        except InvalidRequestError as error:
            raise InvalidRequestError("ledger consumption must be a BudgetConsumption") from error
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
                values[name] = self.totals[name] + validated_consumption[name]
            exhausted = set(self.totals["exhausted_dimensions"]).union(validated_consumption["exhausted_dimensions"])
            limits = {
                "resolvers": self.internal_budget["max_resolvers"],
                "candidates": self.internal_budget["max_candidates"],
                "graph_rows": self.internal_budget["max_graph_rows"],
                "vector_results": self.internal_budget["max_vector_results"],
                "evidence": self.internal_budget["max_evidence"],
                "evidence_bytes": self.internal_budget["max_evidence_bytes"],
                "output_bytes": self.internal_budget["max_output_bytes"],
                "diagnostic_bytes": self.internal_budget["max_diagnostic_bytes"],
                "working_memory_bytes": self.internal_budget["max_working_memory_bytes"],
            }
            for name, limit in limits.items():
                if values.get(name, 0.0) > limit:
                    exhausted.add(name)
            values["exhausted_dimensions"] = tuple(sorted(exhausted))
            values["measurement_available"] = (
                self.totals["measurement_available"] and validated_consumption["measurement_available"]
            )
            self.totals = budget_consumption(**values)
            result = validate_budget_consumption(self.totals)
            return result

    def snapshot(self) -> dict:
        with self.internal_lock:
            result = validate_budget_consumption(self.totals)
            return result
