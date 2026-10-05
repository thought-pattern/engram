"""Pure resolver adapters, bounded execution, accounting, and baseline policy."""

from hashlib import sha256 as hashlib_sha256
from json import dumps as json_dumps
from logging import getLogger as logging_getLogger
from threading import RLock as threading_RLock

from engram.artifacts import LifecycleState
from engram.composition import (
    composition_plan_to_dict,
    execute_composition_plan,
    graph_composition_operator,
    linear_composition_plan,
    phrase_composition_result,
    resolve_composition_predicates,
)
from engram.constants import (
    ACCOUNTING_FINALIZATION_FIELDS,
    EXACT_RESOLVER_COST_CLASS,
    EXACT_RESOLVER_NAME,
    MAX_ACCOUNTING_VISIBLE_STATEMENT_IDS,
    MAX_PLAN_RESOLVERS,
    MAX_REASON_CODE_BYTES,
    MAX_RELATION_PLAN_ROWS,
    MAX_RESOLVER_NAME_BYTES,
    MAX_RESOURCE_COUNTER,
    MAX_STATEMENT_ID_BYTES,
    PROPOSITION_EVIDENCE_PRODUCERS,
    RESOLUTION_PLAN_ENTRY_FIELDS,
    RESOLUTION_PLAN_FIELDS,
    RESOLVER_BUDGET_FIELDS,
    RESOLVER_RESERVATION_FIELDS,
    SPARSE_RESOLVER_COST_CLASS,
    SPARSE_RESOLVER_NAME,
    STANDALONE_SEMANTIC_RESOLVER_COST_CLASS,
    STANDALONE_SEMANTIC_RESOLVER_NAME,
    STRUCTURED_GRAPH_RESOLVER_COST_CLASS,
    STRUCTURED_GRAPH_RESOLVER_NAME,
    SUPPORT_SEMANTIC_RESOLVER_COST_CLASS,
    SUPPORT_SEMANTIC_RESOLVER_NAME,
    UTILITY_RESOLVER_COST_CLASS,
    UTILITY_RESOLVER_NAME,
    UTILITY_RESOLVER_PRODUCER,
    CanonicalResolutionStatus,
    CompositionReason,
    ExactLookupOutcome,
    ExpectedObjectType,
    PredicateCardinality,
    PropositionEligibilityReason,
    RelationPlanTemplate,
    RelationSelectionReason,
    TemporalAxis,
    TemporalQueryOperator,
)
from engram.errors import ConflictError, InvalidRequestError, ResolutionCancelledError, ResourceNotFoundError
from engram.evidence import (
    PropositionEligibilityEvaluator,
    assertion_basis_window,
    canonicalize_proposition_evidence,
    evaluate_evidence_usefulness,
    evidence_usefulness_policy,
    proposition_evidence_record,
    validate_evidence_usefulness_policy,
)
from engram.feedback import canonical_fingerprint
from engram.fusion import CandidateFusionEngine, EngramCandidateAuthority
from engram.graph import proposition_projection_to_dict
from engram.identity import scoped_retrieval_key_from_text
from engram.relation import (
    RelationQuestion,
    object_type_match,
    one_hop_query_plan,
    phrase_relation_result,
    resolve_canonical_predicate,
    resolve_canonical_subject,
    select_relation_propositions,
)
from engram.resolution import (
    BudgetLedger,
    CandidateSource,
    CostClass,
    EvidenceKind,
    ResolutionOutcome,
    ResolverState,
    accounting_observation,
    budget_consumption,
    budget_consumption_from_dict,
    budget_consumption_to_dict,
    budget_consumption_with_changes,
    build_evidence_package,
    candidate as resolution_candidate,
    candidate_to_dict,
    empty_candidate,
    empty_evidence_package,
    evidence_reference,
    evidence_reference_to_dict,
    feature_set,
    proposition_evidence_path_step,
    proposition_evidence_record_with_changes,
    resolver_result,
    resolver_result_to_dict,
    trusted_budget_consumption_with_changes,
    trusted_candidate_to_dict,
    trusted_candidate_with_changes,
    trusted_evidence_package_to_json,
    trusted_evidence_reference_to_dict,
    trusted_proposition_evidence_record_to_dict,
    trusted_resolution_result,
    trusted_resolution_result_to_json,
    trusted_resolver_result_to_dict,
    trusted_resolver_result_with_changes,
    validate_budget_consumption,
    validate_query_frame,
    validate_resolver_result,
)
from engram.utilities import UtilityRegistry

logger = logging_getLogger(__name__)


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
    except InvalidRequestError as error:
        raise InvalidRequestError("reservation lease and consumption have invalid types") from error
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


def json_value(value: object) -> object:
    if isinstance(value, dict):
        result = {key: json_value(item) for key, item in value.items()}
        return result
    if isinstance(value, tuple):
        result = [json_value(item) for item in value]
        return result
    if isinstance(value, list):
        result = [json_value(item) for item in value]
        return result
    return value


def json_size(value: object) -> int:
    result = len(
        json_dumps(
            json_value(value),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    )
    return result


def json_array_bytes(item_bytes: int, count: int) -> int:
    """Return ``json_size`` of a list from its items' total size, or 0 for an empty list.

    Compact JSON adds two brackets and one comma between items, so trimming
    can keep a running total instead of re-encoding the remaining list.
    ``json_array_size`` is the same rule over a list of item sizes.
    """
    result = 2 + item_bytes + count - 1 if count else 0
    return result


def working_size(value: object, seen=()) -> int:
    """Conservatively estimate retained runtime values, including non-wire types."""
    visited = seen if isinstance(seen, set) else set()
    if isinstance(value, str):
        result = len(value.encode("utf-8")) + 49
        return result
    if isinstance(value, bytes):
        result = len(value) + 33
        return result
    if isinstance(value, (bool, int, float)):
        result = 32
        return result
    identity = id(value)
    if identity in visited:
        result = 0
        return result
    visited.add(identity)
    if isinstance(value, dict):
        result = 64 + sum(working_size(key, visited) + working_size(item, visited) for key, item in value.items())
        return result
    if isinstance(value, (list, tuple, set)):
        result = 64 + sum(working_size(item, visited) for item in value)
        return result
    result = len(str(value).encode("utf-8")) + 64
    return result


def internal_candidate_id(source: CandidateSource, statement_id: str, diagnostic_id: str) -> str:
    digest = hashlib_sha256(f"{diagnostic_id}:{source.value}:{statement_id}".encode()).hexdigest()
    result = f"candidate:sha256:{digest}"
    return result


def composition_candidate_id(operator: str, proposition_ids: tuple[str, ...], diagnostic_id: str) -> str:
    """Name one request-local composition proposal from exact ordered native fields."""
    digest = hashlib_sha256(b"engram-composition-candidate-v1\0")
    for value in (operator, diagnostic_id, *proposition_ids):
        encoded = value.encode("utf-8")
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
    result = "composition:" + digest.hexdigest()
    return result


def artifact_matches_frame(artifact: dict, frame: dict) -> bool:
    if (
        artifact.get("scope", {}) != frame.get("scope", {})
        or artifact.get("lifecycle", LifecycleState.RETIRED) != LifecycleState.ACTIVE
    ):
        result = False
        return result
    required_source_label = frame.get("required_source_label", "")
    if required_source_label and artifact.get("provenance", {}).get("source_label", "") != required_source_label:
        result = False
        return result
    metadata = artifact.get("metadata", {})
    result = all(key in metadata and metadata.get(key, ()) == value for key, value in frame.get("required_metadata", {}).items())
    return result


def exhausted_result(name: str, dimensions: tuple[str, ...]) -> dict:
    result = resolver_result(
        resolver=name,
        state=ResolverState.EXHAUSTED,
        reason_code=f"{dimensions[0]}_budget",
        consumption=budget_consumption(resolvers=1, exhausted_dimensions=dimensions),
    )
    return result


COMPOSITION_NOT_APPLICABLE_REASONS = {
    CompositionReason.IDENTITY_MISS.value,
    CompositionReason.UNSUPPORTED_QUERY.value,
    CompositionReason.UNDERCONSTRAINED.value,
}


def composition_not_applicable(result: dict) -> bool:
    """Return whether a composition result only says the request is not two-hop.

    The subject resolved, but no two-predicate plan compiled. A subject miss
    or ambiguity is a real answer from the graph and is not retried.
    """
    diagnostics = result.get("diagnostics", {})
    selected = diagnostics.get("entity_status", "") == CanonicalResolutionStatus.SELECTED.value
    outcome = result.get("reason_code", "") in COMPOSITION_NOT_APPLICABLE_REASONS and selected
    return outcome


def with_prior_graph_rows(result: dict, prior_rows: int) -> dict:
    """Add graph rows spent by an earlier path of the same resolver call."""
    if not prior_rows:
        return result
    current_consumption = result.get("consumption", {})
    consumption = trusted_budget_consumption_with_changes(
        current_consumption,
        {"graph_rows": current_consumption.get("graph_rows", 0) + prior_rows},
    )
    updated = trusted_resolver_result_with_changes(result, {"consumption": consumption})
    return updated


class ExactResolver:
    """Adapter over the Section 3 contextual exact repository."""

    def __init__(self, engram, clock_ns: object) -> None:
        self.name = EXACT_RESOLVER_NAME
        self.cost_class = EXACT_RESOLVER_COST_CLASS
        self.internal_engram = engram
        self.internal_clock_ns = clock_ns

    def available(self, frame: dict) -> bool:
        result = frame.get("eligibility_context", {}).get("artifact_repository_available", False)
        return result

    def resolve(
        self,
        frame: dict,
        budget: dict,
        cooperative_check: object = (),
    ) -> dict:
        run_cooperative_check(cooperative_check)
        if not budget.get("max_candidates", 0) or not budget.get("max_working_memory_bytes", 0):
            dimension = "candidates" if not budget.get("max_candidates", 0) else "working_memory_bytes"
            result = exhausted_result(self.name, (dimension,))
            return result
        started = resolver_clock_ns(self.internal_clock_ns)
        key = scoped_retrieval_key_from_text(frame.get("scope", {}), frame.get("resolved_text", ""))
        contextual = self.internal_engram.response_repository.exact_lookup(
            key,
            frame.get("eligibility_context", {}),
        )
        lookup = contextual.get("lookup", {})
        outcome = lookup.get("outcome", ExactLookupOutcome.MISS)
        if outcome != ExactLookupOutcome.FOUND:
            result = resolver_result(
                resolver=self.name,
                state=ResolverState.COMPLETED,
                reason_code=f"exact_{outcome.value}",
                diagnostics={
                    "owner_count": len(lookup.get("owner_statement_ids", ())),
                    "truncated": lookup.get("truncated", False),
                },
                consumption=budget_consumption(elapsed_ns=max(0, resolver_clock_ns(self.internal_clock_ns) - started), resolvers=1),
            )
            return result
        artifact = self.internal_engram.response_repository.get_artifact(lookup.get("statement_id", ""))
        if not artifact_matches_frame(artifact, frame):
            result = resolver_result(
                resolver=self.name,
                state=ResolverState.COMPLETED,
                reason_code="exact_required_filter_excluded",
                consumption=budget_consumption(elapsed_ns=max(0, resolver_clock_ns(self.internal_clock_ns) - started), resolvers=1),
            )
            return result
        statement_id = artifact.get("statement_id", "")
        scope = artifact.get("scope", {})
        candidate = resolution_candidate(
            candidate_id=internal_candidate_id(CandidateSource.EXACT, statement_id, frame.get("diagnostic_id", "")),
            statement_id=statement_id,
            response=artifact.get("response", ""),
            source=CandidateSource.EXACT,
            features=feature_set(values={"exact_match": 1.0}, unavailable=()),
            evidence=tuple(
                evidence_reference(
                    evidence_id=reference.get("id", ""),
                    resolver=self.name,
                    kind=EvidenceKind.SUPPORT,
                    scope=scope,
                    provenance={"support_linked": True},
                )
                for reference in artifact.get("support_references", ())[: budget.get("max_evidence", 0)]
            ),
            scope=scope,
            lifecycle=artifact.get("lifecycle", LifecycleState.RETIRED),
            provenance={
                "retrieval_origin": lookup.get("provenance", ""),
                "representation": lookup.get("representation", ""),
                "generation": artifact.get("generation", 0),
                "source_label": artifact.get("provenance", {}).get("source_label", ""),
            },
            diagnostics={"context_signature": contextual.get("context_signature", "")},
        )
        if json_size(candidate_to_dict(candidate)) > budget.get("max_working_memory_bytes", 0):
            result = exhausted_result(self.name, ("working_memory_bytes",))
            return result
        result = resolver_result(
            resolver=self.name,
            state=ResolverState.COMPLETED,
            reason_code="exact_found",
            candidates=(candidate,),
            accounting=(accounting_observation(statement_id),),
            consumption=budget_consumption(
                elapsed_ns=max(0, resolver_clock_ns(self.internal_clock_ns) - started),
                resolvers=1,
                candidates=1,
                evidence=len(candidate.get("evidence", ())),
            ),
        )
        return result


class UtilityResolver:
    """Adapter over the configured fixed registry of deterministic operations."""

    def __init__(self, registry: UtilityRegistry, clock_ns: object) -> None:
        if not isinstance(registry, UtilityRegistry):
            raise InvalidRequestError("utility resolver requires a UtilityRegistry")
        if not callable(clock_ns):
            raise InvalidRequestError("utility resolver clock_ns must be callable")
        self.name = UTILITY_RESOLVER_NAME
        self.cost_class = UTILITY_RESOLVER_COST_CLASS
        self.internal_registry = registry
        self.internal_clock_ns = clock_ns

    def available(self, frame: dict) -> bool:
        del frame
        result = self.internal_registry.available()
        return result

    def resolve(
        self,
        frame: dict,
        budget: dict,
        cooperative_check: object = (),
    ) -> dict:
        run_cooperative_check(cooperative_check)
        if not budget.get("max_candidates", 0) or not budget.get("max_working_memory_bytes", 0):
            dimension = "candidates" if not budget.get("max_candidates", 0) else "working_memory_bytes"
            result = exhausted_result(self.name, (dimension,))
            return result
        started = resolver_clock_ns(self.internal_clock_ns)
        if frame.get("required_metadata", {}) or frame.get("required_source_label", ""):
            result = resolver_result(
                resolver=self.name,
                state=ResolverState.COMPLETED,
                reason_code="utility_filters_unsupported",
                consumption=budget_consumption(elapsed_ns=max(0, resolver_clock_ns(self.internal_clock_ns) - started), resolvers=1),
            )
            return result
        evaluation = self.internal_registry.evaluate(frame.get("original_text", ""))
        status = evaluation.get("status", "failed")
        plugin_name = evaluation.get("plugin_name", "")
        canonical_input = evaluation.get("canonical_input", "")
        operations = evaluation.get("operations", 0)
        diagnostics = {
            "plugin_name": plugin_name,
            "error_code": evaluation.get("error_code", ""),
            "operations": operations,
        }
        if status == "miss":
            result = resolver_result(
                resolver=self.name,
                state=ResolverState.COMPLETED,
                reason_code="utility_miss",
                diagnostics=diagnostics,
                consumption=budget_consumption(elapsed_ns=max(0, resolver_clock_ns(self.internal_clock_ns) - started), resolvers=1),
            )
            return result
        if status == "rejected":
            result = resolver_result(
                resolver=self.name,
                state=ResolverState.COMPLETED,
                reason_code="utility_rejected",
                diagnostics=diagnostics,
                consumption=budget_consumption(elapsed_ns=max(0, resolver_clock_ns(self.internal_clock_ns) - started), resolvers=1),
            )
            return result
        if status == "failed":
            result = resolver_result(
                resolver=self.name,
                state=ResolverState.FAILED,
                reason_code="utility_plugin_failure",
                diagnostics=diagnostics,
                consumption=budget_consumption(elapsed_ns=max(0, resolver_clock_ns(self.internal_clock_ns) - started), resolvers=1),
            )
            return result
        digest = hashlib_sha256(f"{plugin_name}:{canonical_input}".encode()).hexdigest()
        statement_id = f"utility:{plugin_name}:sha256:{digest}"
        current = resolution_candidate(
            candidate_id=internal_candidate_id(CandidateSource.UTILITY, statement_id, frame.get("diagnostic_id", "")),
            statement_id=statement_id,
            response=evaluation.get("response", ""),
            source=CandidateSource.UTILITY,
            features=feature_set(values={"utility_match": 1.0}, unavailable=()),
            evidence=(),
            scope=frame.get("scope", {}),
            lifecycle=LifecycleState.ACTIVE,
            provenance={
                "producer": UTILITY_RESOLVER_PRODUCER,
                "plugin_name": plugin_name,
                "canonical_input": canonical_input,
                "learnable": False,
            },
            diagnostics={"operations": operations},
        )
        working_memory_bytes = json_size(candidate_to_dict(current))
        if working_memory_bytes > budget.get("max_working_memory_bytes", 0):
            result = exhausted_result(self.name, ("working_memory_bytes",))
            return result
        result = resolver_result(
            resolver=self.name,
            state=ResolverState.COMPLETED,
            reason_code="utility_resolved",
            candidates=(current,),
            diagnostics=diagnostics,
            consumption=budget_consumption(
                elapsed_ns=max(0, resolver_clock_ns(self.internal_clock_ns) - started),
                resolvers=1,
                candidates=1,
                working_memory_bytes=working_memory_bytes,
            ),
        )
        return result


class StandaloneSemanticResolver:
    """Bounded adapter over canonical-request and alias embeddings."""

    def __init__(self, engram, clock_ns: object) -> None:
        self.name = STANDALONE_SEMANTIC_RESOLVER_NAME
        self.cost_class = STANDALONE_SEMANTIC_RESOLVER_COST_CLASS
        self.internal_engram = engram
        self.internal_clock_ns = clock_ns

    def available(self, frame: dict) -> bool:
        settings = self.internal_engram.config.get("semantic", {})
        result = bool(settings.get("enabled", False) and self.internal_engram.semantic_retriever.available)
        return result

    def resolve(
        self,
        frame: dict,
        budget: dict,
        cooperative_check: object = (),
    ) -> dict:
        run_cooperative_check(cooperative_check)
        if (
            not budget.get("max_candidates", 0)
            or not budget.get("max_vector_results", 0)
            or not budget.get("max_working_memory_bytes", 0)
        ):
            exhausted = tuple(
                name
                for present, name in (
                    (budget.get("max_candidates", 0), "candidates"),
                    (budget.get("max_vector_results", 0), "vector_results"),
                    (budget.get("max_working_memory_bytes", 0), "working_memory_bytes"),
                )
                if not present
            )
            result = exhausted_result(self.name, exhausted)
            return result
        started = resolver_clock_ns(self.internal_clock_ns)
        discovery = self.internal_engram.semantic_candidates(
            frame.get("resolved_text", ""),
            frame.get("scope", {}),
            limit=budget.get("max_candidates", 0),
            max_vector_results=budget.get("max_vector_results", 0),
            max_working_memory_bytes=budget.get("max_working_memory_bytes", 0),
            cooperative_check=cooperative_check,
        )
        scanned_records = discovery.get("scanned_records", 0)
        if not discovery.get("complete", False):
            reason = discovery.get("reason", "")
            if reason == "semantic_unavailable":
                result = resolver_result(resolver=self.name, state=ResolverState.UNAVAILABLE, reason_code=reason)
                return result
            dimension = {
                "semantic_input_too_large": "input_bytes",
                "semantic_scan_budget": "semantic_scan_records",
                "vector_result_budget": "vector_results",
                "working_memory_budget": "working_memory_bytes",
            }.get(reason, "semantic_resources")
            result = resolver_result(
                resolver=self.name,
                state=ResolverState.EXHAUSTED,
                reason_code=reason,
                diagnostics={"scanned_records": scanned_records},
                consumption=budget_consumption(
                    elapsed_ns=max(0, resolver_clock_ns(self.internal_clock_ns) - started),
                    resolvers=1,
                    working_memory_bytes=discovery.get("working_memory_bytes", 0),
                    exhausted_dimensions=(dimension,),
                ),
            )
            return result
        matches = discovery.get("matches", ())
        artifact_identity = self.internal_engram.semantic_retriever.health().get("artifact_identity", {})
        lexical_terms = frame.get("identity", {}).get("lexical_terms", ())
        candidates = []
        accounting = []
        retained_bytes = discovery.get("working_memory_bytes", 0)
        for match in matches:
            try:
                artifact = self.internal_engram.response_repository.get_artifact(match.get("statement_id", ""))
            except ResourceNotFoundError:
                continue
            generation = artifact.get("generation", 0)
            if generation != match.get("generation", 0) or not artifact_matches_frame(artifact, frame):
                continue
            statement_id = artifact.get("statement_id", "")
            origin = match.get("origin", "")
            candidate = resolution_candidate(
                candidate_id=internal_candidate_id(
                    CandidateSource.STANDALONE_SEMANTIC, statement_id, frame.get("diagnostic_id", "")
                ),
                statement_id=statement_id,
                response=artifact.get("response", ""),
                source=CandidateSource.STANDALONE_SEMANTIC,
                features=feature_set(
                    values={
                        "semantic_score": match.get("similarity", 0.0),
                        "semantic_alias_match": float(origin == "alias"),
                    },
                    unavailable=(),
                ),
                evidence=(),
                scope=artifact.get("scope", {}),
                lifecycle=artifact.get("lifecycle", LifecycleState.RETIRED),
                provenance={
                    "generation": generation,
                    "source_label": artifact.get("provenance", {}).get("source_label", ""),
                    "semantic_model_id": artifact_identity.get("model_id", ""),
                    "semantic_model_version": artifact_identity.get("model_version", ""),
                    "matched_representation_id": match.get("representation_id", ""),
                    "matched_representation_origin": origin,
                    "matched_alias_ordinal": match.get("ordinal", -1) if origin == "alias" else -1,
                },
                diagnostics={},
            )
            candidate_bytes = json_size(candidate_to_dict(candidate))
            if retained_bytes + candidate_bytes > budget.get("max_working_memory_bytes", 0):
                result = resolver_result(
                    resolver=self.name,
                    state=ResolverState.EXHAUSTED,
                    reason_code="semantic_candidate_memory_budget",
                    diagnostics={"scanned_records": scanned_records},
                    consumption=budget_consumption(
                        elapsed_ns=max(0, resolver_clock_ns(self.internal_clock_ns) - started),
                        resolvers=1,
                        vector_results=len(matches),
                        working_memory_bytes=min(retained_bytes, budget.get("max_working_memory_bytes", 0)),
                        exhausted_dimensions=("working_memory_bytes",),
                    ),
                )
                return result
            retained_bytes += candidate_bytes
            candidates.append(candidate)
            accounting.append(accounting_observation(statement_id, lexical_terms))
        result = resolver_result(
            resolver=self.name,
            state=ResolverState.COMPLETED,
            reason_code="semantic_candidates" if candidates else "semantic_miss",
            candidates=tuple(candidates),
            accounting=tuple(accounting),
            diagnostics={
                "scanned_records": scanned_records,
                "model_version": artifact_identity.get("model_version", ""),
                "backend": artifact_identity.get("backend", ""),
            },
            consumption=budget_consumption(
                elapsed_ns=max(0, resolver_clock_ns(self.internal_clock_ns) - started),
                resolvers=1,
                candidates=len(candidates),
                vector_results=len(matches),
                working_memory_bytes=retained_bytes,
            ),
        )
        return result


class SparseResolver:
    """Pure adapter over request-local fielded sparse retrieval."""

    def __init__(self, engram, clock_ns: object) -> None:
        self.name = SPARSE_RESOLVER_NAME
        self.cost_class = SPARSE_RESOLVER_COST_CLASS
        self.internal_engram = engram
        self.internal_clock_ns = clock_ns

    def available(self, frame: dict) -> bool:
        settings = self.internal_engram.config.get("sparse", {})
        result = bool(settings.get("enabled", False))
        return result

    def resolve(
        self,
        frame: dict,
        budget: dict,
        cooperative_check: object = (),
    ) -> dict:
        run_cooperative_check(cooperative_check)
        if not budget.get("max_candidates", 0) or not budget.get("max_working_memory_bytes", 0):
            dimension = "candidates" if not budget.get("max_candidates", 0) else "working_memory_bytes"
            result = exhausted_result(self.name, (dimension,))
            return result
        started = resolver_clock_ns(self.internal_clock_ns)
        discovery = self.internal_engram.sparse_candidates(
            frame.get("resolved_text", ""),
            frame.get("scope", {}),
            limit=budget.get("max_candidates", 0),
            max_working_memory_bytes=budget.get("max_working_memory_bytes", 0),
        )
        posting_visits = discovery.get("posting_visits", 0)
        if not discovery.get("complete", False):
            reason = discovery.get("reason", "")
            if reason == "sparse_unavailable":
                result = resolver_result(
                    resolver=self.name,
                    state=ResolverState.UNAVAILABLE,
                    reason_code=reason,
                )
                return result
            exhausted_dimension = {
                "query_term_budget": "query_terms",
                "posting_visit_budget": "posting_visits",
                "working_memory_budget": "working_memory_bytes",
            }.get(reason, "sparse_resources")
            result = resolver_result(
                resolver=self.name,
                state=ResolverState.EXHAUSTED,
                reason_code=reason,
                diagnostics={"posting_visits": posting_visits},
                consumption=budget_consumption(
                    elapsed_ns=max(0, resolver_clock_ns(self.internal_clock_ns) - started),
                    resolvers=1,
                    working_memory_bytes=discovery.get("working_memory_bytes", 0),
                    exhausted_dimensions=(exhausted_dimension,),
                ),
            )
            return result

        query_term_count = discovery.get("query_term_count", 0)
        lexical_terms = frame.get("identity", {}).get("lexical_terms", ())
        candidates = []
        accounting = []
        retained_bytes = discovery.get("working_memory_bytes", 0)
        for match in discovery.get("matches", ()):
            try:
                artifact = self.internal_engram.response_repository.get_artifact(match.get("statement_id", ""))
            except ResourceNotFoundError:
                continue
            if not artifact_matches_frame(artifact, frame):
                continue
            statement_id = artifact.get("statement_id", "")
            field_contributions = match.get("field_contributions", {})
            phrase_fields = match.get("phrase_fields", ())
            proximity_fields = match.get("proximity_fields", ())
            prefix_match_count = match.get("prefix_match_count", 0)
            features = {
                "sparse_score": match.get("score", 0.0),
                "sparse_phrase_match": float(bool(phrase_fields)),
                "sparse_proximity_match": float(bool(proximity_fields)),
                "sparse_prefix_match": float(bool(prefix_match_count)),
                "sparse_character_ngram_similarity": match.get("character_ngram_similarity", 0.0),
                "sparse_technical_exact_match": float(match.get("technical_exact_match", False)),
                "sparse_technical_exact_ratio": match.get("technical_exact_ratio", 0.0),
            }
            features.update({f"sparse_field_{name}": contribution for name, contribution in field_contributions.items()})
            candidate = resolution_candidate(
                candidate_id=internal_candidate_id(CandidateSource.SPARSE, statement_id, frame.get("diagnostic_id", "")),
                statement_id=statement_id,
                response=artifact.get("response", ""),
                source=CandidateSource.SPARSE,
                features=feature_set(values=features, unavailable=()),
                evidence=(),
                scope=artifact.get("scope", {}),
                lifecycle=artifact.get("lifecycle", LifecycleState.RETIRED),
                provenance={
                    "generation": artifact.get("generation", 0),
                    "source_label": artifact.get("provenance", {}).get("source_label", ""),
                },
                diagnostics={
                    "field_contributions": dict(field_contributions),
                    "phrase_fields": list(phrase_fields),
                    "proximity_fields": list(proximity_fields),
                    "minimum_proximity": match.get("minimum_proximity", 0),
                    "prefix_match_count": prefix_match_count,
                    "matched_term_count": match.get("matched_term_count", 0),
                },
            )
            candidate_bytes = json_size(candidate_to_dict(candidate))
            if retained_bytes + candidate_bytes > budget.get("max_working_memory_bytes", 0):
                result = resolver_result(
                    resolver=self.name,
                    state=ResolverState.EXHAUSTED,
                    reason_code="sparse_candidate_memory_budget",
                    diagnostics={
                        "query_term_count": query_term_count,
                        "posting_visits": posting_visits,
                    },
                    consumption=budget_consumption(
                        elapsed_ns=max(0, resolver_clock_ns(self.internal_clock_ns) - started),
                        resolvers=1,
                        working_memory_bytes=min(retained_bytes, budget.get("max_working_memory_bytes", 0)),
                        exhausted_dimensions=("working_memory_bytes",),
                    ),
                )
                return result
            retained_bytes += candidate_bytes
            candidates.append(candidate)
            accounting.append(accounting_observation(statement_id, lexical_terms))
        result = resolver_result(
            resolver=self.name,
            state=ResolverState.COMPLETED,
            reason_code="sparse_candidates" if candidates else "sparse_miss",
            candidates=tuple(candidates),
            accounting=tuple(accounting),
            diagnostics={
                "query_term_count": query_term_count,
                "posting_visits": posting_visits,
            },
            consumption=budget_consumption(
                elapsed_ns=max(0, resolver_clock_ns(self.internal_clock_ns) - started),
                resolvers=1,
                candidates=len(candidates),
                working_memory_bytes=retained_bytes,
            ),
        )
        return result


class StructuredGraphResolver:
    """Pure full-Proposition evidence adapter over fixed structured graph projections."""

    def __init__(self, engram, clock_ns: object, eligibility_evaluator: object = ()) -> None:
        self.name = STRUCTURED_GRAPH_RESOLVER_NAME
        self.cost_class = STRUCTURED_GRAPH_RESOLVER_COST_CLASS
        self.internal_engram = engram
        self.internal_clock_ns = clock_ns
        if eligibility_evaluator == ():
            selected_evaluator = PropositionEligibilityEvaluator(getattr(engram, "proposition_visibility_authority", ()))
        elif isinstance(eligibility_evaluator, PropositionEligibilityEvaluator):
            selected_evaluator = eligibility_evaluator
        else:
            raise InvalidRequestError("structured graph eligibility_evaluator must be PropositionEligibilityEvaluator")
        self.internal_eligibility_evaluator: PropositionEligibilityEvaluator = selected_evaluator

    def available(self, frame: dict) -> bool:
        client = self.internal_engram.graph_client
        result = bool(client and getattr(client, "available", True))
        return result

    def internal_composition_result(
        self,
        frame: dict,
        budget: dict,
        started: int,
        question: RelationQuestion,
        cooperative_check: object = (),
    ) -> tuple[dict, ...]:
        """Compile and execute a conservative composed request through fixed one-hop reads."""
        normalized = frame.get("resolved_text", "").casefold()
        if "'s" not in normalized and " of " not in normalized:
            result = ()
            return result
        graph_rows = 0

        def check() -> None:
            run_cooperative_check(cooperative_check)

        def entity_lookup(surface: str, *, limit: int, cooperative_check=()) -> list:
            nonlocal graph_rows
            remaining = max(0, budget.get("max_graph_rows", 0) - graph_rows)
            if not remaining:
                return []
            rows = self.internal_engram.canonical_entity_matches(
                surface,
                limit=min(limit, remaining),
                cooperative_check=cooperative_check,
            )
            graph_rows += len(rows)
            return rows

        def predicate_lookup(surface: str, *, limit: int, cooperative_check=()) -> list:
            nonlocal graph_rows
            remaining = max(0, budget.get("max_graph_rows", 0) - graph_rows)
            if not remaining:
                return []
            rows = self.internal_engram.canonical_predicate_matches(
                surface,
                limit=min(limit, remaining),
                cooperative_check=cooperative_check,
            )
            graph_rows += len(rows)
            return rows

        subject = resolve_canonical_subject(frame, entity_lookup, question=question, cooperative_check=check)
        subject_status = subject.get("status", CanonicalResolutionStatus.MISS)
        if subject_status != CanonicalResolutionStatus.SELECTED:
            reason = (
                CompositionReason.IDENTITY_AMBIGUOUS
                if subject_status == CanonicalResolutionStatus.AMBIGUOUS
                else CompositionReason.IDENTITY_MISS
            )
            result = resolver_result(
                resolver=self.name,
                state=ResolverState.COMPLETED,
                reason_code=reason.value,
                diagnostics={"composition": True, "entity_status": subject_status.value},
                consumption=budget_consumption(
                    elapsed_ns=max(0, resolver_clock_ns(self.internal_clock_ns) - started),
                    resolvers=1,
                    graph_rows=graph_rows,
                ),
            )
            return (result,)
        try:
            predicates = resolve_composition_predicates(frame, subject, predicate_lookup, check)
            operator = graph_composition_operator(frame)
            remaining_rows = budget.get("max_graph_rows", 0) - graph_rows
            if remaining_rows < 2 * len(predicates):
                result = resolver_result(
                    resolver=self.name,
                    state=ResolverState.COMPLETED,
                    reason_code=CompositionReason.ROW_LIMIT.value,
                    diagnostics={"composition": True, "entity_status": subject_status.value},
                    consumption=budget_consumption(
                        elapsed_ns=max(0, resolver_clock_ns(self.internal_clock_ns) - started),
                        resolvers=1,
                        graph_rows=graph_rows,
                        exhausted_dimensions=("graph_rows",),
                    ),
                )
                return (result,)
            plan = linear_composition_plan(
                subject,
                predicates,
                operator,
                frame.get("expected_object_type", ExpectedObjectType.UNKNOWN),
                max_rows=min(64, remaining_rows),
            )
        except InvalidRequestError as error:
            reason = str(error)
            if reason not in {value.value for value in CompositionReason}:
                reason = CompositionReason.UNSUPPORTED_QUERY.value
            result = resolver_result(
                resolver=self.name,
                state=ResolverState.COMPLETED,
                reason_code=reason,
                diagnostics={"composition": True, "entity_status": subject_status.value},
                consumption=budget_consumption(
                    elapsed_ns=max(0, resolver_clock_ns(self.internal_clock_ns) - started),
                    resolvers=1,
                    graph_rows=graph_rows,
                ),
            )
            return (result,)

        temporal = frame.get("temporal_query", {})
        temporal_axis = temporal.get("axis", TemporalAxis.VALID_TIME)
        include_historical = temporal.get("operator", TemporalQueryOperator.UNSPECIFIED) not in {
            TemporalQueryOperator.UNSPECIFIED,
            TemporalQueryOperator.CURRENT,
            TemporalQueryOperator.NOW,
        }

        def query(subject_id: str, predicate_id: str, limit: int) -> list[dict]:
            result = self.internal_engram.relation_one_hop_proposition_projections(
                subject_id,
                predicate_id,
                row_limit=limit,
                include_historical=include_historical,
                cooperative_check=check,
                max_working_memory_bytes=budget.get("max_working_memory_bytes", 0),
                basis_window=assertion_basis_window(frame),
            )
            return result

        checked_frame = validate_query_frame(frame)
        execution = execute_composition_plan(
            plan,
            query,
            lambda projection: self.internal_eligibility_evaluator.evaluate(projection, checked_frame, trusted_frame=True),
            lambda projection: self.internal_eligibility_evaluator.revalidate(
                projection,
                checked_frame,
                self.internal_engram.current_proposition_projection,
                trusted_frame=True,
            ),
            check,
        )
        complete_paths = execution.get("complete_paths", ())
        execution_reasons = execution.get("reasons", ())
        plan_operator = plan.get("operator", operator)
        graph_rows += execution.get("graph_rows", 0)
        composition_direct = execution.get("direct_result", False)
        direct_suppression_reasons: set[str] = set()
        for path in complete_paths:
            for entry in path:
                proposition = entry.get("proposition", {})
                projection = proposition.get("projection", {})
                if not projection.get("supplied_trust_available", False):
                    composition_direct = False
                    direct_suppression_reasons.add(CompositionReason.TRUST_UNAVAILABLE.value)
                if proposition.get("predicate_cardinality", PredicateCardinality.UNKNOWN) == PredicateCardinality.UNKNOWN:
                    composition_direct = False
                    direct_suppression_reasons.add(CompositionReason.CARDINALITY_UNKNOWN.value)
                if include_historical:
                    if temporal_axis == TemporalAxis.VALID_TIME:
                        lower_available = projection.get("valid_from_available", False)
                        upper_available = projection.get("valid_to_available", False)
                    else:
                        lower_available = projection.get("system_from_available", False)
                        upper_available = projection.get("system_to_available", False)
                    if not (lower_available and upper_available):
                        composition_direct = False
                        direct_suppression_reasons.add(CompositionReason.TEMPORAL_BOUNDS_OPEN.value)
        selected_paths = complete_paths or execution.get("partial_paths", ())
        aggregation_inputs = plan.get("aggregation_inputs", ())
        records = []
        for path in selected_paths:
            if not path:
                continue
            terminal = path[-1]
            base = proposition_evidence_record(
                terminal.get("proposition", {}).get("projection", {}),
                terminal.get("decision", {}),
                frame,
                self.name,
                trusted=True,
            )
            path_steps = []
            for position, entry in enumerate(path):
                step_projection = entry.get("proposition", {}).get("projection", {})
                step = entry.get("step", {})
                path_steps.append(
                    proposition_evidence_path_step(
                        position,
                        step_projection.get("proposition_id", ""),
                        step_projection.get("subject_entity_id", ""),
                        step_projection.get("predicate_id", ""),
                        step_projection.get("object_entity_id", ""),
                        plan_operator,
                        step.get("subject_binding", ""),
                        step.get("object_binding", ""),
                        (
                            "canonical_identity",
                            "object_type",
                            "publication_revalidation",
                            "temporal_eligibility",
                            "visibility",
                        ),
                        aggregation_inputs if position == len(path) - 1 else (),
                    )
                )
            reasons = {
                *base.get("selection_reasons", ()),
                "composition_plan_match",
                *[reason.value for reason in execution_reasons],
                *direct_suppression_reasons,
            }
            records.append(
                proposition_evidence_record_with_changes(
                    base,
                    {
                        "path": tuple(path_steps),
                        "selection_reasons": tuple(sorted(reasons)),
                    },
                )
            )
        records.sort(key=lambda record: record.get("proposition_id", ""))
        if len(records) > budget.get("max_evidence", 0):
            records = records[: budget.get("max_evidence", 0)]
        candidates = []
        if composition_direct and complete_paths and budget.get("max_candidates", 0):
            response = phrase_composition_result(plan, execution)
            selected_path = complete_paths[0]
            selected_projections = tuple(entry.get("proposition", {}).get("projection", {}) for entry in selected_path)
            proposition_ids = tuple(projection.get("proposition_id", "") for projection in selected_projections)
            identity_chain = tuple(
                (
                    projection.get("subject_entity_id", ""),
                    projection.get("predicate_id", ""),
                    projection.get("object_entity_id", ""),
                )
                for projection in selected_projections
            )
            trust_chain = tuple(projection.get("supplied_trust", 0.0) for projection in selected_projections)
            references = tuple(
                evidence_reference(
                    evidence_id=proposition_id,
                    resolver=self.name,
                    kind=EvidenceKind.PROPOSITION,
                    scope=frame.get("scope", {}),
                    provenance={"composition_path": True},
                    diagnostics={},
                )
                for proposition_id in proposition_ids
            )
            composition_id = composition_candidate_id(plan_operator.value, proposition_ids, frame.get("diagnostic_id", ""))
            candidates.append(
                resolution_candidate(
                    candidate_id=composition_id,
                    statement_id=composition_id,
                    response=response,
                    source=CandidateSource.UTILITY,
                    features=feature_set(
                        values={"entity_match": subject.get("score", 0.0), "relation_match": 1.0, "object_type_match": 1.0},
                    ),
                    evidence=references,
                    scope=frame.get("scope", {}),
                    lifecycle=LifecycleState.ACTIVE,
                    provenance={
                        "producer": "graph_composition",
                        "operator": plan_operator.value,
                        "root_entity_id": plan.get("root_entity_id", ""),
                        "root_label": plan.get("root_label", ""),
                        "predicate_labels": tuple(entry.get("step", {}).get("predicate_label", "") for entry in selected_path),
                        "proposition_ids": proposition_ids,
                        "identity_chain": identity_chain,
                        "trust_chain": trust_chain,
                        "terminal_labels": execution.get("terminal_labels", ()),
                        "terminal_types": tuple(value.value for value in execution.get("terminal_types", ())),
                        "truth_value": execution.get("truth_value", False),
                        "truth_available": execution.get("truth_available", False),
                        "aggregate_value": execution.get("aggregate_value", ""),
                        "aggregate_value_available": execution.get("aggregate_value_available", False),
                    },
                    diagnostics={
                        "plan": composition_plan_to_dict(plan),
                        "reasons": tuple(reason.value for reason in execution_reasons),
                    },
                )
            )

        exhausted = set()
        record_sizes = [json_size(trusted_proposition_evidence_record_to_dict(record)) for record in records]
        record_total = sum(record_sizes)

        def record_bytes() -> int:
            result = json_array_bytes(record_total, len(records))
            return result

        while records and record_bytes() > budget.get("max_evidence_bytes", 0):
            records.pop()
            record_total -= record_sizes.pop()
            exhausted.add("evidence_bytes")
        output_bytes = record_bytes() + sum(json_size(candidate_to_dict(value)) for value in candidates)
        while candidates and output_bytes > budget.get("max_output_bytes", 0):
            candidates.pop()
            exhausted.add("output_bytes")
            output_bytes = record_bytes()
        if candidates and not records:
            candidates.clear()
            output_bytes = 0
        working_memory = working_size(execution) + record_bytes() + output_bytes
        if working_memory > budget.get("max_working_memory_bytes", 0):
            raise MemoryError("composition working-memory estimate exceeded")
        result = resolver_result(
            resolver=self.name,
            state=ResolverState.COMPLETED,
            reason_code=(
                "graph_composition_candidate"
                if candidates
                else "graph_composition_evidence" if records else "graph_composition_miss"
            ),
            candidates=tuple(candidates),
            proposition_evidence=tuple(records),
            diagnostics={
                "composition": True,
                "operator": plan_operator.value,
                "complete_paths": len(complete_paths),
                "partial_paths": len(execution.get("partial_paths", ())),
                "truncated": execution.get("truncated", False),
                "reasons": tuple(reason.value for reason in execution_reasons),
                "plan": composition_plan_to_dict(plan),
            },
            consumption=budget_consumption(
                elapsed_ns=max(0, resolver_clock_ns(self.internal_clock_ns) - started),
                resolvers=1,
                candidates=len(candidates),
                graph_rows=graph_rows,
                evidence=len(records),
                evidence_bytes=record_bytes(),
                output_bytes=output_bytes,
                working_memory_bytes=working_memory,
                exhausted_dimensions=tuple(sorted(exhausted)),
            ),
        )
        return (result,)

    def internal_relation_result(
        self,
        frame: dict,
        budget: dict,
        started: int,
        question: RelationQuestion,
        cooperative_check: object = (),
    ) -> tuple[dict, ...]:
        """Interpret and execute one canonical one-hop relation plan."""
        if not self.internal_engram.graph_client:
            result = ()
            return result
        graph_rows = 0

        def check() -> None:
            run_cooperative_check(cooperative_check)

        def entity_lookup(surface: str, *, limit: int, cooperative_check=()) -> list:
            nonlocal graph_rows
            remaining = max(0, budget.get("max_graph_rows", 0) - graph_rows - 2)
            if not remaining:
                return []
            rows = self.internal_engram.canonical_entity_matches(
                surface,
                limit=min(limit, remaining),
                cooperative_check=cooperative_check,
            )
            graph_rows += len(rows)
            return rows

        def predicate_lookup(surface: str, *, limit: int, cooperative_check=()) -> list:
            nonlocal graph_rows
            remaining = max(0, budget.get("max_graph_rows", 0) - graph_rows - 2)
            if not remaining:
                return []
            rows = self.internal_engram.canonical_predicate_matches(
                surface,
                limit=min(limit, remaining),
                cooperative_check=cooperative_check,
            )
            graph_rows += len(rows)
            return rows

        subject = resolve_canonical_subject(frame, entity_lookup, question=question, cooperative_check=check)
        predicate = resolve_canonical_predicate(frame, predicate_lookup, question=question, cooperative_check=check)
        subject_status = subject.get("status", CanonicalResolutionStatus.MISS)
        predicate_status = predicate.get("status", CanonicalResolutionStatus.MISS)
        statuses = (subject_status, predicate_status)
        if statuses == (CanonicalResolutionStatus.MISS, CanonicalResolutionStatus.MISS):
            result = ()
            return result
        if CanonicalResolutionStatus.AMBIGUOUS in statuses or CanonicalResolutionStatus.MISS in statuses:
            reason = "relation_identity_ambiguous" if CanonicalResolutionStatus.AMBIGUOUS in statuses else "relation_identity_miss"
            result = resolver_result(
                resolver=self.name,
                state=ResolverState.COMPLETED,
                reason_code=reason,
                diagnostics={
                    "entity_status": subject_status.value,
                    "entity_candidates": len(subject.get("candidate_ids", ())),
                    "predicate_status": predicate_status.value,
                    "predicate_candidates": len(predicate.get("candidate_ids", ())),
                },
                consumption=budget_consumption(
                    elapsed_ns=max(0, resolver_clock_ns(self.internal_clock_ns) - started),
                    resolvers=1,
                    graph_rows=graph_rows,
                ),
            )
            return (result,)
        remaining_rows = budget.get("max_graph_rows", 0) - graph_rows
        if remaining_rows < 2:
            result = exhausted_result(self.name, ("graph_rows",))
            return (result,)
        plan = one_hop_query_plan(
            subject,
            predicate,
            frame.get("expected_object_type", ExpectedObjectType.UNKNOWN),
            max_rows=min(MAX_RELATION_PLAN_ROWS, budget.get("max_evidence", 0), max(1, remaining_rows // 2)),
        )
        template_id = plan.get("template_id", RelationPlanTemplate.ONE_HOP_PROPOSITION)
        expected_object_type = plan.get("expected_object_type", ExpectedObjectType.UNKNOWN)
        include_historical = frame.get("temporal_query", {}).get("operator", TemporalQueryOperator.UNSPECIFIED) not in {
            TemporalQueryOperator.UNSPECIFIED,
            TemporalQueryOperator.CURRENT,
            TemporalQueryOperator.NOW,
        }

        results = self.internal_engram.relation_one_hop_proposition_projections(
            plan.get("subject_entity_id", ""),
            plan.get("predicate_id", ""),
            row_limit=plan.get("max_rows", 0),
            include_historical=include_historical,
            cooperative_check=check,
            max_working_memory_bytes=budget.get("max_working_memory_bytes", 0),
            basis_window=assertion_basis_window(frame),
        )
        graph_rows += len(results)
        retained: list[tuple[dict, dict, float, bool]] = []
        exclusion_counts: dict[str, int] = {}
        revalidation_rows = 0
        checked_frame = validate_query_frame(frame)
        for item in results:
            check()
            projection = item.get("projection", {})
            initial = self.internal_eligibility_evaluator.evaluate(projection, checked_frame, trusted_frame=True)
            if not initial.get("eligible", False):
                reason = initial.get("reason", PropositionEligibilityReason.REVALIDATION_UNAVAILABLE).value
                exclusion_counts[reason] = exclusion_counts.get(reason, 0) + 1
                continue
            if graph_rows >= budget.get("max_graph_rows", 0):
                break
            decision = self.internal_eligibility_evaluator.revalidate(
                projection, checked_frame, self.internal_engram.current_proposition_projection, trusted_frame=True
            )
            graph_rows += 1
            if decision.get("revalidated", False):
                revalidation_rows += 1
            if not decision.get("eligible", False):
                reason = decision.get("reason", PropositionEligibilityReason.REVALIDATION_UNAVAILABLE).value
                exclusion_counts[reason] = exclusion_counts.get(reason, 0) + 1
                continue
            object_type = item.get("object_type", ExpectedObjectType.UNKNOWN)
            type_match, type_match_available = object_type_match(expected_object_type, object_type)
            retained.append((item, decision, type_match, type_match_available))

        selection = select_relation_propositions(
            tuple(item for item, internal_decision, internal_type_match, internal_type_match_available in retained),
            frame.get("temporal_query", {}),
        )
        direct_answer = selection.get("direct_answer", False)
        selection_reason = selection.get("reason", RelationSelectionReason.NO_ELIGIBLE_PROPOSITION)
        selection_cardinality = selection.get("cardinality", PredicateCardinality.UNKNOWN)
        conflict_proposition_ids = selection.get("conflict_proposition_ids", ())
        ranking_proposition_ids = selection.get("ranking_proposition_ids", ())
        subject_score = subject.get("score", 0.0)
        predicate_score = predicate.get("score", 0.0)
        records = []
        ambiguous_result = not direct_answer
        for item, decision, type_match, type_match_available in retained:
            item_projection = item.get("projection", {})
            base = proposition_evidence_record(item_projection, decision, frame, self.name, trusted=True)
            base_features = base.get("features", {})
            values = dict(base_features.get("values", {}))
            values.update({"entity_match": subject_score, "relation_match": predicate_score})
            unavailable = set(base_features.get("unavailable", ()))
            reasons = set(base.get("selection_reasons", ()))
            reasons.update({"entity_resolved", "predicate_resolved", "relation_plan_match"})
            reasons.add(selection_reason.value)
            if item_projection.get("proposition_id", "") in conflict_proposition_ids:
                reasons.add("relation_conflicting_proposition")
            if not type_match_available:
                unavailable.add("object_type_match")
                reasons.add("object_type_unavailable")
            else:
                values["object_type_match"] = type_match
                reasons.add("object_type_match" if type_match else "object_type_mismatch")
            reasons.add("relation_result_ambiguous" if ambiguous_result else "relation_result_unique")
            records.append(
                proposition_evidence_record_with_changes(
                    base,
                    {
                        "features": feature_set(values, tuple(sorted(unavailable - set(values)))),
                        "selection_reasons": tuple(sorted(reasons)),
                    },
                )
            )
        records.sort(key=lambda record: record.get("proposition_id", ""))
        candidates = []
        if direct_answer and budget.get("max_candidates", 0):
            selected_proposition_id = selection.get("selected_proposition_id", "")
            item, _, type_match, type_match_available = next(
                value for value in retained if value[0].get("projection", {}).get("proposition_id", "") == selected_proposition_id
            )
            if not type_match_available or type_match != 0.0:
                item_projection = item.get("projection", {})
                proposition_id = item_projection.get("proposition_id", "")
                reference = evidence_reference(
                    evidence_id=proposition_id,
                    resolver=self.name,
                    kind=EvidenceKind.PROPOSITION,
                    scope=frame.get("scope", {}),
                    provenance={"relation_plan": True},
                    diagnostics={},
                )
                unavailable = () if type_match_available else ("object_type_match",)
                values = {"entity_match": subject_score, "relation_match": predicate_score}
                if type_match_available:
                    values["object_type_match"] = type_match
                subject_label = subject.get("primary_label", "")
                predicate_label = predicate.get("primary_label", "")
                object_label = item.get("object_label", "")
                response = phrase_relation_result(subject_label, predicate_label, object_label)
                candidates.append(
                    resolution_candidate(
                        candidate_id=internal_candidate_id(CandidateSource.UTILITY, proposition_id, frame.get("diagnostic_id", "")),
                        statement_id=proposition_id,
                        response=response,
                        source=CandidateSource.UTILITY,
                        features=feature_set(values, unavailable),
                        evidence=(reference,),
                        scope=frame.get("scope", {}),
                        lifecycle=LifecycleState.ACTIVE,
                        provenance={
                            "producer": "relation_one_hop",
                            "subject_entity_id": plan.get("subject_entity_id", ""),
                            "subject_label": subject_label,
                            "predicate_id": plan.get("predicate_id", ""),
                            "predicate_label": predicate_label,
                            "object_entity_id": item_projection.get("object_entity_id", ""),
                            "object_label": object_label,
                            "object_type": item.get("object_type", ExpectedObjectType.UNKNOWN).value,
                            "predicate_cardinality": selection_cardinality.value,
                            "selection_reason": selection_reason.value,
                            "supplied_trust": item_projection.get("supplied_trust", 0.0),
                        },
                        diagnostics={
                            "template_id": template_id.value,
                            "ranking_proposition_ids": ranking_proposition_ids,
                        },
                    )
                )
        exhausted = set()
        record_sizes = [json_size(trusted_proposition_evidence_record_to_dict(record)) for record in records]
        record_total = sum(record_sizes)

        def record_bytes() -> int:
            result = json_array_bytes(record_total, len(records))
            return result

        while records and record_bytes() > budget.get("max_evidence_bytes", 0):
            records.pop()
            record_total -= record_sizes.pop()
            exhausted.add("evidence_bytes")
        output_bytes = record_bytes() + sum(json_size(candidate_to_dict(value)) for value in candidates)
        while candidates and output_bytes > budget.get("max_output_bytes", 0):
            candidates.pop()
            exhausted.add("output_bytes")
            output_bytes = record_bytes()
        if candidates and not records:
            candidates.clear()
            output_bytes = 0
        working_memory = working_size(results) + record_bytes() + output_bytes
        if working_memory > budget.get("max_working_memory_bytes", 0):
            raise MemoryError("relation resolution working-memory estimate exceeded")
        result = resolver_result(
            resolver=self.name,
            state=ResolverState.COMPLETED,
            reason_code=(
                "relation_proposition_candidate"
                if candidates
                else (
                    "relation_proposition_conflict"
                    if conflict_proposition_ids
                    else "relation_proposition_evidence" if records else "relation_graph_miss"
                )
            ),
            candidates=tuple(candidates),
            proposition_evidence=tuple(records),
            diagnostics={
                "entity_status": subject_status.value,
                "predicate_status": predicate_status.value,
                "template_id": template_id.value,
                "discovery_rows": len(results),
                "revalidation_rows": revalidation_rows,
                "exclusion_counts": exclusion_counts,
                "ambiguous_result": ambiguous_result,
                "selection_reason": selection_reason.value,
                "predicate_cardinality": selection_cardinality.value,
                "conflict_proposition_ids": conflict_proposition_ids,
                "ranking_proposition_ids": ranking_proposition_ids,
            },
            consumption=budget_consumption(
                elapsed_ns=max(0, resolver_clock_ns(self.internal_clock_ns) - started),
                resolvers=1,
                candidates=len(candidates),
                graph_rows=graph_rows,
                evidence=len(records),
                evidence_bytes=record_bytes(),
                output_bytes=output_bytes,
                working_memory_bytes=working_memory,
                exhausted_dimensions=tuple(sorted(exhausted)),
            ),
        )
        return (result,)

    def resolve(
        self,
        frame: dict,
        budget: dict,
        cooperative_check: object = (),
    ) -> dict:
        """Resolve while honoring a transient caller cancellation check."""
        run_cooperative_check(cooperative_check)
        if (
            budget.get("max_graph_rows", 0) < 2
            or not budget.get("max_evidence", 0)
            or not budget.get("max_evidence_bytes", 0)
            or not budget.get("max_output_bytes", 0)
            or not budget.get("max_working_memory_bytes", 0)
        ):
            dimensions = tuple(
                name
                for name, value in (
                    ("evidence", budget.get("max_evidence", 0)),
                    ("evidence_bytes", budget.get("max_evidence_bytes", 0)),
                    ("graph_rows", budget.get("max_graph_rows", 0) if budget.get("max_graph_rows", 0) >= 2 else 0),
                    ("output_bytes", budget.get("max_output_bytes", 0)),
                    ("working_memory_bytes", budget.get("max_working_memory_bytes", 0)),
                )
                if not value
            )
            result = exhausted_result(self.name, dimensions)
            return result
        started = resolver_clock_ns(self.internal_clock_ns)
        composition_rows = 0
        # Composition and one-hop discovery read one parse of the same question.
        question = RelationQuestion(frame.get("resolved_text", ""))
        try:
            composition_result = self.internal_composition_result(frame, budget, started, question, cooperative_check)
            if composition_result and not composition_not_applicable(composition_result[0]):
                result = composition_result[0]
                return result
            if composition_result:
                # "of" and "'s" also appear in one-hop questions. When the
                # subject resolved but no two-hop path compiled, the one-hop
                # and structured paths answer from the rows still unspent.
                composition_rows = composition_result[0].get("consumption", {}).get("graph_rows", 0)
                budget = resolver_budget_with_changes(
                    budget,
                    {"max_graph_rows": max(0, budget.get("max_graph_rows", 0) - composition_rows)},
                )
            relation_result = self.internal_relation_result(frame, budget, started, question, cooperative_check)
            if relation_result:
                result = with_prior_graph_rows(relation_result[0], composition_rows)
                return result
            projections = self.internal_engram.structured_proposition_projections(
                frame.get("resolved_text", ""),
                row_limit=min(budget.get("max_graph_rows", 0) // 2, budget.get("max_evidence", 0)),
                cooperative_check=cooperative_check,
                max_working_memory_bytes=budget.get("max_working_memory_bytes", 0),
                basis_window=assertion_basis_window(frame),
            )
        except MemoryError:
            result = exhausted_result(self.name, ("working_memory_bytes",))
            return result
        records = []
        exclusion_counts: dict[str, int] = {}
        revalidation_rows = 0
        checked_frame = validate_query_frame(frame)
        for projection in projections:
            run_cooperative_check(cooperative_check)
            initial = self.internal_eligibility_evaluator.evaluate(projection, checked_frame, trusted_frame=True)
            if not initial.get("eligible", False):
                reason = initial.get("reason", PropositionEligibilityReason.REVALIDATION_UNAVAILABLE).value
                exclusion_counts[reason] = exclusion_counts.get(reason, 0) + 1
                continue
            decision = self.internal_eligibility_evaluator.revalidate(
                projection, checked_frame, self.internal_engram.current_proposition_projection, trusted_frame=True
            )
            if decision.get("revalidated", False):
                revalidation_rows += 1
            if not decision.get("eligible", False):
                reason = decision.get("reason", PropositionEligibilityReason.REVALIDATION_UNAVAILABLE).value
                exclusion_counts[reason] = exclusion_counts.get(reason, 0) + 1
                continue
            records.append(proposition_evidence_record(projection, decision, frame, self.name, trusted=True))
        records.sort(key=lambda record: record.get("proposition_id", ""))
        exhausted = set()
        record_sizes = [json_size(trusted_proposition_evidence_record_to_dict(record)) for record in records]
        record_total = sum(record_sizes)

        def record_bytes() -> int:
            result = json_array_bytes(record_total, len(records))
            return result

        while records and record_bytes() > budget.get("max_evidence_bytes", 0):
            records.pop()
            record_total -= record_sizes.pop()
            exhausted.add("evidence_bytes")
        while records and record_bytes() > budget.get("max_output_bytes", 0):
            records.pop()
            record_total -= record_sizes.pop()
            exhausted.add("output_bytes")
        working_memory = json_size([proposition_projection_to_dict(projection) for projection in projections]) + record_bytes()
        if working_memory > budget.get("max_working_memory_bytes", 0):
            result = exhausted_result(self.name, ("working_memory_bytes",))
            return result
        evidence_bytes = record_bytes()
        result = resolver_result(
            resolver=self.name,
            state=ResolverState.COMPLETED,
            reason_code="structured_proposition_evidence" if records else "structured_graph_miss",
            proposition_evidence=tuple(records),
            diagnostics={
                "discovery_rows": len(projections),
                "revalidation_rows": revalidation_rows,
                "exclusion_counts": exclusion_counts,
            },
            consumption=budget_consumption(
                elapsed_ns=max(0, resolver_clock_ns(self.internal_clock_ns) - started),
                resolvers=1,
                graph_rows=composition_rows + len(projections) + revalidation_rows,
                evidence=len(records),
                evidence_bytes=evidence_bytes,
                output_bytes=evidence_bytes,
                working_memory_bytes=working_memory,
                exhausted_dimensions=tuple(sorted(exhausted)),
            ),
        )
        run_cooperative_check(cooperative_check)
        return result


class SupportSemanticResolver:
    """Pure fixed-vector adapter for support candidates and full Proposition evidence."""

    def __init__(self, engram, clock_ns: object, eligibility_evaluator: object = ()) -> None:
        self.name = SUPPORT_SEMANTIC_RESOLVER_NAME
        self.cost_class = SUPPORT_SEMANTIC_RESOLVER_COST_CLASS
        self.internal_engram = engram
        self.internal_clock_ns = clock_ns
        if eligibility_evaluator == ():
            selected_evaluator = PropositionEligibilityEvaluator(getattr(engram, "proposition_visibility_authority", ()))
        elif isinstance(eligibility_evaluator, PropositionEligibilityEvaluator):
            selected_evaluator = eligibility_evaluator
        else:
            raise InvalidRequestError("support semantic eligibility_evaluator must be PropositionEligibilityEvaluator")
        self.internal_eligibility_evaluator: PropositionEligibilityEvaluator = selected_evaluator

    def available(self, frame: dict) -> bool:
        graph = self.internal_engram.config.get("graph", {})
        client = self.internal_engram.graph_client
        result = bool(
            graph.get("enabled", False)
            and graph.get("vector_enabled", False)
            and client
            and getattr(client, "available", True)
            and self.internal_engram.graph_embedding_model
        )
        return result

    def resolve(
        self,
        frame: dict,
        budget: dict,
        cooperative_check: object = (),
    ) -> dict:
        """Resolve semantic candidates while honoring caller cancellation."""
        run_cooperative_check(cooperative_check)
        proposition_capacity = bool(
            budget.get("max_graph_rows", 0)
            and budget.get("max_evidence", 0)
            and budget.get("max_evidence_bytes", 0)
            and budget.get("max_output_bytes", 0)
        )
        if (
            not budget.get("max_vector_results", 0)
            or not budget.get("max_working_memory_bytes", 0)
            or (not budget.get("max_candidates", 0) and not proposition_capacity)
        ):
            dimensions = tuple(
                name
                for name, value in (
                    ("candidates", budget.get("max_candidates", 0)),
                    ("evidence", budget.get("max_evidence", 0)),
                    ("evidence_bytes", budget.get("max_evidence_bytes", 0)),
                    ("graph_rows", budget.get("max_graph_rows", 0)),
                    ("output_bytes", budget.get("max_output_bytes", 0)),
                    ("vector_results", budget.get("max_vector_results", 0)),
                    ("working_memory_bytes", budget.get("max_working_memory_bytes", 0)),
                )
                if not value
            )
            result = exhausted_result(self.name, dimensions)
            return result
        started = resolver_clock_ns(self.internal_clock_ns)

        def artifact_filter(artifact: dict[str, object]) -> bool:
            result = artifact_matches_frame(artifact, frame)
            return result

        try:
            candidate_limit = min(budget.get("max_candidates", 0), budget.get("max_vector_results", 0))
            run_cooperative_check(cooperative_check)
            graph_rows = (
                self.internal_engram.graph_vector_propositions(
                    frame.get("resolved_text", ""),
                    limit=candidate_limit,
                    evaluation_time=frame.get("eligibility_context", {}).get("evaluation_time", ""),
                )[:candidate_limit]
                if candidate_limit
                else []
            )
            run_cooperative_check(cooperative_check)
            matches = (
                self.internal_engram.vector_supported_proposition_match_components(
                    graph_rows,
                    limit=candidate_limit,
                    artifact_filter=artifact_filter,
                    cooperative_check=cooperative_check,
                    max_working_memory_bytes=budget.get("max_working_memory_bytes", 0),
                )
                if candidate_limit
                else []
            )
            remaining_vector_results = max(0, budget.get("max_vector_results", 0) - len(graph_rows))
            projections = (
                self.internal_engram.graph_vector_proposition_projections(
                    frame.get("resolved_text", ""),
                    limit=remaining_vector_results,
                    cooperative_check=cooperative_check,
                    max_working_memory_bytes=budget.get("max_working_memory_bytes", 0),
                    basis_window=assertion_basis_window(frame),
                )
                if remaining_vector_results
                else []
            )
        except MemoryError:
            result = exhausted_result(self.name, ("working_memory_bytes",))
            return result
        candidates = []
        accounting = []
        evidence_count = 0
        for match in matches:
            run_cooperative_check(cooperative_check)
            artifact = match.get("artifact", {})
            statement_id = artifact.get("statement_id", "")
            support_references = artifact.get("support_references", ())
            references = tuple(
                evidence_reference(
                    evidence_id=reference.get("id", ""),
                    resolver=self.name,
                    kind=EvidenceKind.SUPPORT,
                    scope=frame.get("scope", {}),
                    provenance={"support_linked": True},
                    diagnostics={"semantic_match": True},
                )
                for reference in support_references[: max(0, budget.get("max_evidence", 0) - evidence_count)]
            )
            evidence_count += len(references)
            candidate = resolution_candidate(
                candidate_id=internal_candidate_id(CandidateSource.SUPPORT_SEMANTIC, statement_id, frame.get("diagnostic_id", "")),
                statement_id=statement_id,
                response=artifact.get("response", ""),
                source=CandidateSource.SUPPORT_SEMANTIC,
                features=feature_set(
                    values={
                        "retrieval_score": float(match.get("retrieval_score", 0.0)),
                        "priority": float(match.get("priority", 0.0)),
                        "semantic_score": float(match.get("semantic_similarity", 0.0)),
                        "support_coverage": float(bool(references)),
                        "vector_weight": float(match.get("vector_weight", 0.0)),
                    },
                    unavailable=("lexical_score",),
                ),
                evidence=references,
                scope=artifact.get("scope", {}),
                lifecycle=artifact.get("lifecycle", LifecycleState.RETIRED),
                provenance={
                    "generation": artifact.get("generation", 0),
                    "source_label": artifact.get("provenance", {}).get("source_label", ""),
                },
                diagnostics={"support_count": len(support_references)},
            )
            candidates.append(candidate)
            accounting.append(accounting_observation(candidate.get("statement_id", "")))
        records = []
        exclusion_counts: dict[str, int] = {}
        revalidation_attempts = 0
        revalidation_rows = 0
        exhausted = set()
        remaining_evidence = max(0, budget.get("max_evidence", 0) - evidence_count)
        if proposition_capacity and remaining_evidence:
            checked_frame = validate_query_frame(frame)
            for projection in projections:
                run_cooperative_check(cooperative_check)
                initial = self.internal_eligibility_evaluator.evaluate(projection, checked_frame, trusted_frame=True)
                if not initial.get("eligible", False):
                    reason = initial.get("reason", PropositionEligibilityReason.REVALIDATION_UNAVAILABLE).value
                    exclusion_counts[reason] = exclusion_counts.get(reason, 0) + 1
                    continue
                if revalidation_attempts >= budget.get("max_graph_rows", 0):
                    exhausted.add("graph_rows")
                    break
                revalidation_attempts += 1
                decision = self.internal_eligibility_evaluator.revalidate(
                    projection, checked_frame, self.internal_engram.current_proposition_projection, trusted_frame=True
                )
                if decision.get("revalidated", False):
                    revalidation_rows += 1
                if not decision.get("eligible", False):
                    reason = decision.get("reason", PropositionEligibilityReason.REVALIDATION_UNAVAILABLE).value
                    exclusion_counts[reason] = exclusion_counts.get(reason, 0) + 1
                    continue
                records.append(proposition_evidence_record(projection, decision, frame, self.name, trusted=True))
                if len(records) >= remaining_evidence:
                    break
        records.sort(key=lambda record: record.get("proposition_id", ""))

        # Candidates are fixed while records are trimmed, so their sizes are
        # computed once and each record's size is subtracted as it goes.
        reference_sizes = [
            json_size(evidence_reference_to_dict(reference))
            for candidate in candidates
            for reference in candidate.get("evidence", ())
        ]
        candidate_sizes = [json_size(candidate_to_dict(candidate)) for candidate in candidates]
        record_sizes = [json_size(trusted_proposition_evidence_record_to_dict(record)) for record in records]
        record_total = sum(record_sizes)

        def evidence_bytes() -> int:
            result = json_array_bytes(sum(reference_sizes) + record_total, len(reference_sizes) + len(records))
            return result

        def output_bytes() -> int:
            result = json_array_bytes(sum(candidate_sizes) + record_total, len(candidate_sizes) + len(records))
            return result

        while records and evidence_bytes() > budget.get("max_evidence_bytes", 0):
            records.pop()
            record_total -= record_sizes.pop()
            exhausted.add("evidence_bytes")
        while records and output_bytes() > budget.get("max_output_bytes", 0):
            records.pop()
            record_total -= record_sizes.pop()
            exhausted.add("output_bytes")
        fixed_working_memory = (
            working_size(graph_rows) + working_size(projections) + working_size(matches) + working_size(candidates)
        )
        # working_size counts a shared object once, in list order, so each
        # record's share is fixed by the records before it and dropping from
        # the end subtracts exactly that share.
        record_visited = {id(records)}
        record_shares = [working_size(record, record_visited) for record in records]
        record_working = 64 + sum(record_shares)
        working_memory = fixed_working_memory + record_working
        while records and working_memory > budget.get("max_working_memory_bytes", 0):
            records.pop()
            record_working -= record_shares.pop()
            exhausted.add("working_memory_bytes")
            working_memory = fixed_working_memory + record_working
        if working_memory > budget.get("max_working_memory_bytes", 0):
            result = exhausted_result(self.name, ("working_memory_bytes",))
            return result
        selected_reason = "support_semantic_candidates" if candidates else "semantic_proposition_evidence"
        if not candidates and not records:
            selected_reason = "support_semantic_miss"
        result = resolver_result(
            resolver=self.name,
            state=ResolverState.COMPLETED,
            reason_code=selected_reason,
            candidates=tuple(candidates),
            proposition_evidence=tuple(records),
            accounting=tuple(accounting),
            diagnostics={
                "projection_rows": len(projections),
                "revalidation_attempts": revalidation_attempts,
                "revalidation_rows": revalidation_rows,
                "exclusion_counts": exclusion_counts,
            },
            consumption=budget_consumption(
                elapsed_ns=max(0, resolver_clock_ns(self.internal_clock_ns) - started),
                resolvers=1,
                candidates=len(candidates),
                graph_rows=revalidation_attempts,
                vector_results=len(graph_rows) + len(projections),
                evidence=evidence_count + len(records),
                evidence_bytes=evidence_bytes(),
                output_bytes=output_bytes(),
                working_memory_bytes=working_memory,
                exhausted_dimensions=tuple(sorted(exhausted)),
            ),
        )
        run_cooperative_check(cooperative_check)
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
            except Exception as error:
                logger.warning("Resolver %s availability check failed", name, exc_info=error)
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


def json_array_size(item_sizes: list[int]) -> int:
    """Return exact compact-JSON bytes for an array of pre-sized values."""
    result = 2 + sum(item_sizes) + max(0, len(item_sizes) - 1)
    return result


def bound_validated_resolver_result(result: dict, lease: dict) -> dict:
    """Bound one executor-validated result without revalidating its nested records."""
    current_consumption = result.get("consumption", {})
    candidates = list(result.get("candidates", ())[: lease.get("max_candidates", 0)])
    evidence = list(result.get("evidence", ()))
    proposition_evidence = list(result.get("proposition_evidence", ()))
    exhausted = set(current_consumption.get("exhausted_dimensions", ()))
    if len(candidates) < len(result.get("candidates", ())):
        exhausted.add("candidates")
    remaining_evidence = lease.get("max_evidence", 0)
    for index, candidate in enumerate(candidates):
        candidate_evidence = candidate.get("evidence", ())
        retained = candidate_evidence[:remaining_evidence]
        if len(retained) < len(candidate_evidence):
            exhausted.add("evidence")
        if retained != candidate_evidence:
            candidates[index] = trusted_candidate_with_changes(candidate, {"evidence": retained})
        remaining_evidence -= len(retained)
    retained_evidence = evidence[:remaining_evidence]
    if len(retained_evidence) < len(evidence):
        exhausted.add("evidence")
    evidence = retained_evidence
    remaining_evidence -= len(evidence)
    retained_proposition_evidence = proposition_evidence[:remaining_evidence]
    if len(retained_proposition_evidence) < len(proposition_evidence):
        exhausted.add("evidence")
    proposition_evidence = retained_proposition_evidence

    candidate_evidence_sizes = [
        [json_size(trusted_evidence_reference_to_dict(reference)) for reference in candidate.get("evidence", ())]
        for candidate in candidates
    ]
    evidence_sizes = [json_size(trusted_evidence_reference_to_dict(reference)) for reference in evidence]
    proposition_evidence_sizes = [json_size(trusted_proposition_evidence_record_to_dict(record)) for record in proposition_evidence]
    flattened_evidence_sizes = [
        *[size for values in candidate_evidence_sizes for size in values],
        *evidence_sizes,
        *proposition_evidence_sizes,
    ]
    evidence_size = json_array_size(flattened_evidence_sizes) if flattened_evidence_sizes else 0
    evidence_count = len(flattened_evidence_sizes)
    while evidence_size > lease.get("max_evidence_bytes", 0):
        if proposition_evidence:
            proposition_evidence.pop()
            removed_size = proposition_evidence_sizes.pop()
        elif evidence:
            evidence.pop()
            removed_size = evidence_sizes.pop()
        else:
            candidate_index = next(
                (index for index in range(len(candidates) - 1, -1, -1) if candidate_evidence_sizes[index]),
                -1,
            )
            if candidate_index < 0:
                break
            removed_size = candidate_evidence_sizes[candidate_index].pop()
        evidence_size -= removed_size + int(evidence_count > 1)
        evidence_count -= 1
        exhausted.add("evidence_bytes")
    for index, sizes in enumerate(candidate_evidence_sizes):
        candidate = candidates[index]
        candidate_evidence = candidate.get("evidence", ())
        if len(sizes) != len(candidate_evidence):
            candidates[index] = trusted_candidate_with_changes(candidate, {"evidence": candidate_evidence[: len(sizes)]})

    candidate_sizes = [json_size(trusted_candidate_to_dict(candidate)) for candidate in candidates]
    output_item_sizes = [*candidate_sizes, *evidence_sizes, *proposition_evidence_sizes]
    bounded_output_size = json_array_size(output_item_sizes) if output_item_sizes else 0
    output_count = len(output_item_sizes)
    while bounded_output_size > lease.get("max_output_bytes", 0) and output_count:
        if proposition_evidence:
            proposition_evidence.pop()
            removed_size = proposition_evidence_sizes.pop()
        elif evidence:
            evidence.pop()
            removed_size = evidence_sizes.pop()
        else:
            candidates.pop()
            candidate_evidence_sizes.pop()
            removed_size = candidate_sizes.pop()
        bounded_output_size -= removed_size + int(output_count > 1)
        output_count -= 1
        exhausted.add("output_bytes")
    diagnostics = result.get("diagnostics", {})
    if diagnostics and json_size(dict(diagnostics)) > lease.get("max_diagnostic_bytes", 0):
        marker = {"truncated": True}
        diagnostics = marker if json_size(dict(marker)) <= lease.get("max_diagnostic_bytes", 0) else {}
        exhausted.add("diagnostic_bytes")
    accounting_ids = {candidate.get("statement_id", "") for candidate in candidates}
    accounting = tuple(value for value in result.get("accounting", ()) if value.get("statement_id", "") in accounting_ids)
    candidate_bytes = json_array_size(candidate_sizes) if candidate_sizes else 0
    retained_evidence_sizes = [
        *[size for values in candidate_evidence_sizes for size in values],
        *evidence_sizes,
        *proposition_evidence_sizes,
    ]
    evidence_bytes = json_array_size(retained_evidence_sizes) if retained_evidence_sizes else 0
    diagnostic_bytes = json_size(dict(diagnostics)) if diagnostics else 0
    reported_graph_rows = current_consumption.get("graph_rows", 0)
    reported_vector_results = current_consumption.get("vector_results", 0)
    graph_rows = min(reported_graph_rows, lease.get("max_graph_rows", 0))
    vector_results = min(reported_vector_results, lease.get("max_vector_results", 0))
    if graph_rows < reported_graph_rows:
        exhausted.add("graph_rows")
    if vector_results < reported_vector_results:
        exhausted.add("vector_results")
    estimated_memory = max(
        candidate_bytes
        + (json_array_size(evidence_sizes) if evidence_sizes else 0)
        + (json_array_size(proposition_evidence_sizes) if proposition_evidence_sizes else 0)
        + diagnostic_bytes,
        current_consumption.get("working_memory_bytes", 0),
    )
    if estimated_memory > lease.get("max_working_memory_bytes", 0):
        exhausted.add("working_memory_bytes")
        candidates = []
        evidence = []
        proposition_evidence = []
        candidate_sizes = []
        candidate_evidence_sizes = []
        evidence_sizes = []
        proposition_evidence_sizes = []
        accounting = ()
        diagnostics = {}
        candidate_bytes = 0
        evidence_bytes = 0
        diagnostic_bytes = 0
    working_memory = min(estimated_memory, lease.get("max_working_memory_bytes", 0))
    consumption = budget_consumption(
        elapsed_ns=current_consumption.get("elapsed_ns", 0),
        resolvers=1,
        candidates=len(candidates),
        graph_rows=graph_rows,
        vector_results=vector_results,
        evidence=len(evidence) + len(proposition_evidence) + sum(len(candidate.get("evidence", ())) for candidate in candidates),
        evidence_bytes=evidence_bytes,
        output_bytes=(
            json_array_size([*candidate_sizes, *evidence_sizes, *proposition_evidence_sizes])
            if candidate_sizes or evidence_sizes or proposition_evidence_sizes
            else 0
        ),
        diagnostic_bytes=diagnostic_bytes,
        working_memory_bytes=working_memory,
        exhausted_dimensions=tuple(sorted(exhausted)),
        measurement_available=current_consumption.get("measurement_available", False),
    )
    result = trusted_resolver_result_with_changes(
        result,
        {
            "candidates": tuple(candidates),
            "evidence": tuple(evidence),
            "proposition_evidence": tuple(proposition_evidence),
            "accounting": accounting,
            "diagnostics": diagnostics,
            "consumption": consumption,
        },
    )
    return result


class ResolverExecutor:
    """Sequential bounded fail-soft executor for one deterministic plan."""

    def __init__(self, clock_ns: object) -> None:
        if not callable(clock_ns):
            raise InvalidRequestError("executor clock_ns must be callable")
        self.internal_clock_ns = clock_ns

    @property
    def clock_ns(self) -> object:
        """Expose the injected request clock to post-execution policy stages."""
        result = self.internal_clock_ns
        return result

    def internal_lease(self, frame: dict, ledger: BudgetLedger) -> dict:
        consumed = ledger.snapshot()
        budget = frame.get("budget", {})
        result = resolver_budget(
            max_candidates=ledger.remaining_candidates(),
            max_graph_rows=max(0, budget.get("max_graph_rows", 0) - consumed.get("graph_rows", 0)),
            max_vector_results=max(0, budget.get("max_vector_results", 0) - consumed.get("vector_results", 0)),
            max_evidence=ledger.remaining_evidence(),
            max_evidence_bytes=max(0, budget.get("max_evidence_bytes", 0) - consumed.get("evidence_bytes", 0)),
            max_output_bytes=max(0, budget.get("max_output_bytes", 0) - consumed.get("output_bytes", 0)),
            max_diagnostic_bytes=max(0, budget.get("max_diagnostic_bytes", 0) - consumed.get("diagnostic_bytes", 0)),
            max_working_memory_bytes=max(0, budget.get("max_working_memory_bytes", 0) - consumed.get("working_memory_bytes", 0)),
        )
        return result

    def execute(self, frame: dict, plan: dict, cooperative_check: object = ()) -> dict:
        frame = validate_query_frame(frame)
        current_plan = validate_resolution_plan(plan)
        run_cooperative_check(cooperative_check)
        ledger = BudgetLedger(frame.get("budget", {}))
        max_resolvers = frame.get("budget", {}).get("max_resolvers", 0)
        results = []
        reservations = []
        exact_short_circuited = False
        for entry in current_plan.get("entries", ()):
            run_cooperative_check(cooperative_check)
            resolver_name, _, _, resolve_operation = resolver_contract(entry.get("resolver", ()))
            if ledger.snapshot().get("resolvers", 0) >= max_resolvers:
                exhausted = resolver_result(
                    resolver=resolver_name,
                    state=ResolverState.EXHAUSTED,
                    reason_code="resolver_budget",
                    consumption=budget_consumption(exhausted_dimensions=("resolvers",)),
                )
                results.append(exhausted)
                ledger.add(exhausted.get("consumption", {}))
                break
            if not entry.get("configured", False):
                continue
            if not entry.get("available", False):
                results.append(
                    resolver_result(
                        resolver=resolver_name,
                        state=ResolverState.UNAVAILABLE,
                        reason_code=entry.get("reason_code", ""),
                    )
                )
                continue
            lease = self.internal_lease(frame, ledger)
            if not lease.get("max_candidates", 0) and resolver_name not in {"structured_graph", "support_semantic"}:
                results.append(
                    resolver_result(
                        resolver=resolver_name,
                        state=ResolverState.EXHAUSTED,
                        reason_code="candidate_budget",
                    )
                )
                continue
            started = resolver_clock_ns(self.internal_clock_ns)
            try:
                raw = execute_resolver(resolve_operation, frame, lease, cooperative_check)
                run_cooperative_check(cooperative_check)
            except ResolutionCancelledError:
                raise
            except Exception as error:
                logger.warning("Resolver %s failed", resolver_name, exc_info=error)
                finished = resolver_clock_ns(self.internal_clock_ns)
                elapsed = max(0, finished - started)
                result = resolver_result(
                    resolver=resolver_name,
                    state=ResolverState.FAILED,
                    reason_code="resolver_exception",
                    diagnostics={"exception_type": type(error).__name__},
                    consumption=budget_consumption(elapsed_ns=elapsed, resolvers=1),
                )
            else:
                finished = resolver_clock_ns(self.internal_clock_ns)
                elapsed = max(0, finished - started)
                try:
                    current_raw = validate_resolver_result(raw)
                except InvalidRequestError as error:
                    logger.warning("Resolver %s returned an invalid result", resolver_name, exc_info=error)
                    result = resolver_result(
                        resolver=resolver_name,
                        state=ResolverState.FAILED,
                        reason_code="invalid_resolver_result",
                        diagnostics={"exception_type": type(error).__name__},
                        consumption=budget_consumption(elapsed_ns=elapsed, resolvers=1),
                    )
                else:
                    updated_consumption = trusted_budget_consumption_with_changes(
                        current_raw.get("consumption", {}),
                        {"elapsed_ns": elapsed},
                    )
                    raw = trusted_resolver_result_with_changes(
                        current_raw,
                        {"consumption": updated_consumption},
                    )
                    result = bound_validated_resolver_result(raw, lease)
            result_consumption = result.get("consumption", {})
            result_candidates = result.get("candidates", ())
            results.append(result)
            ledger.add(result_consumption)
            reservations.append(resolver_reservation(resolver_name, entry.get("order", 0), lease, result_consumption))
            if (
                resolver_name == "exact"
                and result.get("state", ResolverState.FAILED) == ResolverState.COMPLETED
                and len(result_candidates) == 1
                and result_candidates[0].get("source", CandidateSource.UTILITY) == CandidateSource.EXACT
                and not frame.get("rewrite_chain", ())
            ):
                exact_short_circuited = True
                break
        # The executor owns every record in this report, so none is revalidated here.
        report = {
            "results": tuple(results),
            "consumption": ledger.snapshot(),
            "exact_short_circuited": exact_short_circuited,
            "reservations": tuple(reservations),
        }
        return report


def accounting_finalization(
    candidate_statement_ids: object,
    accepted_statement_id: object,
    candidacy_applied: object,
    success_applied: object,
    idempotent: object,
) -> dict:
    """Build an inspectable exactly-once accounting result."""
    if not isinstance(candidate_statement_ids, tuple) or not all(
        isinstance(value, str) and value and len(value.encode("utf-8")) <= MAX_STATEMENT_ID_BYTES
        for value in candidate_statement_ids
    ):
        raise InvalidRequestError("candidate_statement_ids must be a tuple of bounded non-empty strings")
    if len(candidate_statement_ids) > MAX_RESOURCE_COUNTER:
        raise InvalidRequestError("candidate_statement_ids exceed the resource limit")
    if tuple(sorted(set(candidate_statement_ids))) != candidate_statement_ids:
        raise InvalidRequestError("candidate_statement_ids must be sorted and unique")
    if not isinstance(accepted_statement_id, str) or len(accepted_statement_id.encode("utf-8")) > MAX_STATEMENT_ID_BYTES:
        raise InvalidRequestError("accepted_statement_id must be a bounded string")
    if accepted_statement_id and accepted_statement_id not in candidate_statement_ids:
        raise InvalidRequestError("accepted_statement_id must identify an observed candidate")
    if not isinstance(candidacy_applied, bool) or not isinstance(success_applied, bool) or not isinstance(idempotent, bool):
        raise InvalidRequestError("accounting finalization flags must be booleans")
    if success_applied and not accepted_statement_id:
        raise InvalidRequestError("success accounting requires an accepted statement")
    result: dict = {
        "candidate_statement_ids": candidate_statement_ids,
        "accepted_statement_id": accepted_statement_id,
        "candidacy_applied": candidacy_applied,
        "success_applied": success_applied,
        "idempotent": idempotent,
    }
    return result


def validate_accounting_finalization(value: object) -> dict:
    if not isinstance(value, dict) or set(value) != ACCOUNTING_FINALIZATION_FIELDS:
        raise InvalidRequestError("AccountingFinalization has invalid fields")
    result = accounting_finalization(
        value.get("candidate_statement_ids", ()),
        value.get("accepted_statement_id", ""),
        value.get("candidacy_applied", False),
        value.get("success_applied", False),
        value.get("idempotent", False),
    )
    return result


def accounting_finalization_to_dict(value: object) -> dict[str, object]:
    current = validate_accounting_finalization(value)
    candidate_statement_ids = current.get("candidate_statement_ids", ())
    visible_ids = candidate_statement_ids[:MAX_ACCOUNTING_VISIBLE_STATEMENT_IDS]
    result = {
        "candidate_statement_ids": list(visible_ids),
        "omitted_candidate_statement_id_count": len(candidate_statement_ids) - len(visible_ids),
        "accepted_statement_id": current.get("accepted_statement_id", ""),
        "candidacy_applied": current.get("candidacy_applied", False),
        "success_applied": current.get("success_applied", False),
        "idempotent": current.get("idempotent", False),
    }
    return result


def accounting_diagnostics(finalization: dict, compact: bool) -> dict[str, object]:
    """Report accounting in resolution diagnostics; the compact form omits statement ids."""
    if compact:
        result = {
            "candidate_count": len(finalization.get("candidate_statement_ids", ())),
            "accepted_present": bool(finalization.get("accepted_statement_id", "")),
            "candidacy_applied": finalization.get("candidacy_applied", False),
            "success_applied": finalization.get("success_applied", False),
            "idempotent": finalization.get("idempotent", False),
        }
    else:
        result = accounting_finalization_to_dict(finalization)
    return result


def accounting_signature(results: tuple[dict, ...], accepted_statement_id: str) -> str:
    """Return the retry identity for one stable accounting observation set."""
    stable_results = []
    for result in results:
        value = resolver_result_to_dict(result)
        raw_consumption = value.get("consumption", {})
        if not isinstance(raw_consumption, dict):
            raise InvalidRequestError("resolver result consumption must be an object")
        consumption = dict(raw_consumption)
        consumption["elapsed_ns"] = 0
        value["consumption"] = consumption
        stable_results.append(value)
    payload = {
        "accepted_statement_id": accepted_statement_id,
        "results": stable_results,
    }
    result = canonical_fingerprint(payload)
    return result


def accounting_receipt_id(kind: str, request_id: str) -> str:
    """Return a bounded non-disclosing receipt identity."""
    digest = hashlib_sha256(request_id.encode("utf-8")).hexdigest()
    result = f"resolution:{kind}:sha256:{digest}"
    return result


class ResolutionAccountingFinalizer:
    """Exactly-once boundary for authoritative response-artifact accounting."""

    def __init__(self, engram, accepted_response_service, max_requests: int = 1_000) -> None:
        self.internal_engram = engram
        if not callable(getattr(accepted_response_service, "finalize_resolution_accounting", ())):
            raise InvalidRequestError("accepted-response accounting service is required")
        self.internal_accepted_response_service = accepted_response_service
        if isinstance(max_requests, bool) or not isinstance(max_requests, int) or max_requests < 1:
            raise InvalidRequestError("accounting max_requests must be a positive integer")
        self.internal_max_requests = max_requests
        self.internal_lock = threading_RLock()
        self.internal_requests: dict[str, tuple[str, dict]] = {}

    @property
    def candidate_authority(self) -> EngramCandidateAuthority:
        """Return current-state authority bound to the same Engram as accounting."""
        result = EngramCandidateAuthority(self.internal_engram)
        return result

    def clear(self) -> None:
        """Forget every transient retry record when the owning result cache is cleared."""
        with self.internal_lock:
            self.internal_requests.clear()

    def discard(self, request_id: str) -> None:
        """Forget one transient retry record after the owning result cache evicts it."""
        if not isinstance(request_id, str) or not request_id:
            raise InvalidRequestError("accounting request_id must be a non-empty string")
        with self.internal_lock:
            if request_id in self.internal_requests:
                del self.internal_requests[request_id]

    def finalize(
        self,
        request_id: str,
        results: tuple[dict, ...],
        accepted_statement_id: str = "",
    ) -> dict:
        if not isinstance(request_id, str) or not request_id:
            raise InvalidRequestError("accounting request_id must be a non-empty string")
        if not isinstance(results, tuple):
            raise InvalidRequestError("accounting results must be a tuple of ResolverResult values")
        try:
            results = tuple(validate_resolver_result(value) for value in results)
        except InvalidRequestError as error:
            raise InvalidRequestError("accounting results must be a tuple of ResolverResult values") from error
        if not isinstance(accepted_statement_id, str):
            raise InvalidRequestError("accepted_statement_id must be a string")
        signature = accounting_signature(results, accepted_statement_id)
        with self.internal_lock:
            if request_id in self.internal_requests:
                previous_signature, previous = self.internal_requests.get(request_id, ("", {}))
                if previous_signature != signature:
                    raise ConflictError("accounting request_id is associated with different observations")
                result = accounting_finalization(
                    previous.get("candidate_statement_ids", ()),
                    previous.get("accepted_statement_id", ""),
                    previous.get("candidacy_applied", False),
                    previous.get("success_applied", False),
                    True,
                )
                return result
            observations = {
                observation.get("statement_id", ""): observation
                for resolver_value in results
                for observation in resolver_value.get("accounting", ())
            }
            candidate_ids = tuple(sorted(observations))
            repository = self.internal_engram.response_repository
            missing_ids = tuple(statement_id for statement_id in candidate_ids if not repository.has_artifact(statement_id))
            if missing_ids:
                raise InvalidRequestError("observed accepted response no longer exists")
            if accepted_statement_id and accepted_statement_id not in observations:
                raise InvalidRequestError("accepted_statement_id was not an observed candidate")
            accounting_replayed = False
            if candidate_ids:
                accounting_mutation = self.internal_accepted_response_service.finalize_resolution_accounting(
                    candidate_ids,
                    accepted_statement_id,
                    accounting_receipt_id("finalize", request_id),
                )
                accounting_replayed = bool(accounting_mutation.get("replayed", False))
            finalization = accounting_finalization(
                candidate_ids,
                accepted_statement_id,
                True,
                bool(accepted_statement_id),
                accounting_replayed,
            )
            self.internal_requests[request_id] = (signature, finalization)
            while len(self.internal_requests) > self.internal_max_requests:
                self.internal_requests.pop(next(iter(self.internal_requests)))
            return finalization


class ResolutionOrchestrator:
    """Section 5 fused ANSWER/EVIDENCE/MISS orchestration."""

    def __init__(
        self,
        registry: ResolverRegistry,
        executor: ResolverExecutor,
        accounting: ResolutionAccountingFinalizer,
        fusion: object = (),
        evidence_policy: object = (),
    ) -> None:
        if not isinstance(registry, ResolverRegistry) or not isinstance(executor, ResolverExecutor):
            raise InvalidRequestError("orchestrator registry and executor have invalid types")
        if not isinstance(accounting, ResolutionAccountingFinalizer):
            raise InvalidRequestError("orchestrator accounting has an invalid type")
        if fusion != () and not isinstance(fusion, CandidateFusionEngine):
            raise InvalidRequestError("orchestrator fusion has an invalid type")
        if evidence_policy == ():
            selected_evidence_policy = evidence_usefulness_policy()
        else:
            try:
                selected_evidence_policy = validate_evidence_usefulness_policy(evidence_policy)
            except InvalidRequestError as error:
                raise InvalidRequestError("orchestrator evidence_policy has an invalid type") from error
        self.internal_registry = registry
        self.internal_executor = executor
        self.internal_accounting = accounting
        self.internal_clock_ns = executor.clock_ns
        self.internal_fusion = (
            fusion if isinstance(fusion, CandidateFusionEngine) else CandidateFusionEngine(authority=accounting.candidate_authority)
        )
        self.internal_evidence_policy = selected_evidence_policy

    def resolve(
        self,
        frame: dict,
        request_id: str,
        configured_names: tuple[str, ...] = (),
        accept_exact: bool = False,
        cooperative_check: object = (),
    ) -> tuple[dict, dict]:
        plan = self.internal_registry.plan(frame, configured_names)
        result = self.resolve_with_plan(frame, request_id, plan, accept_exact, cooperative_check)
        return result

    def resolve_with_plan(
        self,
        frame: dict,
        request_id: str,
        plan: dict,
        accept_exact: bool = False,
        cooperative_check: object = (),
    ) -> tuple[dict, dict]:
        """Resolve using a plan already created for this frame by the registry."""
        if not isinstance(accept_exact, bool):
            raise InvalidRequestError("accept_exact must be a boolean")
        execution = self.internal_executor.execute(frame, plan, cooperative_check)
        run_cooperative_check(cooperative_check)
        execution_results = execution.get("results", ())
        execution_consumption = execution.get("consumption", {})
        budget_limits = frame.get("budget", {})
        candidates = []
        evidence = []
        for resolver_value in execution_results:
            candidates.extend(resolver_value.get("candidates", ()))
            evidence.extend(resolver_value.get("evidence", ()))
        fusion_memory_limit = max(
            0,
            budget_limits.get("max_working_memory_bytes", 0) - execution_consumption.get("working_memory_bytes", 0),
        )
        decision = self.internal_fusion.decide(
            frame,
            tuple(candidates),
            tuple(evidence),
            working_memory_limit=fusion_memory_limit,
            working_memory_limit_available=True,
            cooperative_check=cooperative_check,
        )
        run_cooperative_check(cooperative_check)
        decision_report = decision.get("report", {})
        decision_working_memory = decision.get("working_memory_bytes", 0)
        outcome = decision.get("outcome", ResolutionOutcome.MISS)
        selected = decision.get("selected_candidate", {})
        selected_available = decision.get("selected_candidate_available", False)
        response_candidates = decision.get("response_candidates", ())
        reason_codes = [*decision.get("reason_codes", ()), "accounting_finalized"]
        confidence = decision.get("confidence", 0.0)
        confidence_available = decision.get("confidence_available", False)
        selected_exact = selected.get("source", CandidateSource.UTILITY) == CandidateSource.EXACT
        if outcome == ResolutionOutcome.ANSWER and selected_exact and not accept_exact:
            # Fusion decides that an exact result is eligible; releasing it as a direct
            # ANSWER needs the caller's explicit permission. Without it the candidate is
            # kept as evidence and earns no success credit.
            outcome = ResolutionOutcome.EVIDENCE
            response_candidates = (selected,)
            selected = empty_candidate()
            selected_available = False
            confidence = 0.0
            confidence_available = False
            reason_codes.append("exact_answer_not_permitted")
        evidence_package_available = False
        evidence_package = empty_evidence_package()
        package_source_records = ()
        package_byte_limit = 256
        minimal_evidence_values = tuple(
            reference
            for result in execution_results
            for reference in result.get("evidence", ())
            + tuple(nested for candidate in result.get("candidates", ()) for nested in candidate.get("evidence", ()))
        )
        minimal_evidence_count = len(minimal_evidence_values)
        minimal_evidence_bytes = (
            json_size([evidence_reference_to_dict(reference) for reference in minimal_evidence_values])
            if minimal_evidence_values
            else 0
        )
        evidence_working_memory = 0
        orchestration_exhausted = set()
        evidence_diagnostics: dict[str, object] = {
            "available": False,
            "input_count": 0,
            "normalized_count": 0,
            "included_count": 0,
            "excluded_count": 0,
            "reason_counts": {},
        }

        def append_reason(reason: str) -> None:
            if reason not in reason_codes:
                reason_codes.append(reason)

        def evidence_check() -> None:
            run_cooperative_check(cooperative_check)

        unexpected_proposition_records = any(
            result.get("proposition_evidence", ()) and result.get("resolver", "") not in PROPOSITION_EVIDENCE_PRODUCERS
            for result in execution_results
        )
        if unexpected_proposition_records:
            append_reason("proposition_evidence_untrusted_producer")
        raw_proposition_records = tuple(
            record
            for result in execution_results
            if result.get("resolver", "") in PROPOSITION_EVIDENCE_PRODUCERS
            for record in result.get("proposition_evidence", ())
        )
        full_producer_available = any(
            result.get("resolver", "") in PROPOSITION_EVIDENCE_PRODUCERS
            and result.get("state", ResolverState.FAILED) == ResolverState.COMPLETED
            and result.get("proposition_evidence", ())
            for result in execution_results
        )
        if outcome != ResolutionOutcome.ANSWER and full_producer_available:
            evidence_diagnostics["input_count"] = len(raw_proposition_records)
            remaining_working_memory = max(
                0,
                budget_limits.get("max_working_memory_bytes", 0)
                - execution_consumption.get("working_memory_bytes", 0)
                - decision_working_memory,
            )
            if working_size(raw_proposition_records) * 2 > remaining_working_memory:
                orchestration_exhausted.add("working_memory_bytes")
                append_reason("proposition_evidence_memory_exhausted")
            else:
                try:
                    normalized_records = canonicalize_proposition_evidence(raw_proposition_records, evidence_check)
                    evaluation_time = frame.get("eligibility_context", {}).get("evaluation_time", "")
                    if any(
                        record.get("disclosure", {}).get("scope", {}) != frame.get("scope", {})
                        or record.get("validity", {}).get("evaluation_time", "") != evaluation_time
                        for record in normalized_records
                    ):
                        raise InvalidRequestError("Proposition evidence is not bound to the current frame")
                    usefulness_decisions = []
                    included_records = []
                    reason_counts: dict[str, int] = {}
                    for record in normalized_records:
                        evidence_check()
                        usefulness = evaluate_evidence_usefulness(self.internal_evidence_policy, record)
                        usefulness_decisions.append(usefulness)
                        for reason in usefulness.get("reasons", ()):
                            reason_counts[reason.value] = reason_counts.get(reason.value, 0) + 1
                        if usefulness.get("included", False):
                            included_records.append(record)
                    evidence_check()
                except InvalidRequestError:
                    append_reason("proposition_evidence_conflict")
                else:
                    package_source_records = tuple(included_records)
                    evidence_working_memory = (
                        working_size(normalized_records) + working_size(usefulness_decisions) + working_size(included_records)
                    )
                    evidence_diagnostics.update(
                        {
                            "normalized_count": len(normalized_records),
                            "included_count": len(included_records),
                            "excluded_count": len(normalized_records) - len(included_records),
                            "reason_counts": reason_counts,
                        }
                    )
                    remaining_evidence_bytes = max(
                        0,
                        budget_limits.get("max_evidence_bytes", 0) - minimal_evidence_bytes,
                    )
                    package_byte_limit = min(65_536, max(256, remaining_evidence_bytes))
                    candidate_package = build_evidence_package(
                        package_source_records,
                        max_records=min(10, max(0, budget_limits.get("max_evidence", 0) - minimal_evidence_count)),
                        max_bytes=package_byte_limit,
                    )
                    candidate_package_bytes = len(trusted_evidence_package_to_json(candidate_package).encode("utf-8"))
                    if candidate_package_bytes <= remaining_evidence_bytes:
                        evidence_package_available = True
                        evidence_package = candidate_package
                        evidence_working_memory += candidate_package_bytes
                        evidence_diagnostics["available"] = True
                    else:
                        orchestration_exhausted.add("evidence_bytes")
                        append_reason("proposition_evidence_bytes_exhausted")
                    if evidence_working_memory > remaining_working_memory:
                        evidence_package_available = False
                        evidence_package = empty_evidence_package()
                        orchestration_exhausted.add("working_memory_bytes")
                        append_reason("proposition_evidence_memory_exhausted")
                        evidence_diagnostics["available"] = False
                    elif evidence_package.get("records", ()):
                        append_reason("proposition_evidence_included")
                        if outcome == ResolutionOutcome.MISS:
                            outcome = ResolutionOutcome.EVIDENCE
                    elif normalized_records:
                        append_reason("proposition_evidence_excluded")
        accepted_statement_id = (
            selected.get("statement_id", "")
            if outcome == ResolutionOutcome.ANSWER
            and selected.get("source", CandidateSource.UTILITY) == CandidateSource.EXACT
            and accept_exact
            else ""
        )
        preview_ids = tuple(
            sorted(
                {
                    observation.get("statement_id", "")
                    for result in execution_results
                    for observation in result.get("accounting", ())
                }
            )
        )
        output_limit = budget_limits.get("max_output_bytes", 0)
        working_memory_bytes = (
            execution_consumption.get("working_memory_bytes", 0) + decision_working_memory + evidence_working_memory
        )
        output_truncated = False
        evidence_diagnostics.update(
            {
                "available": evidence_package_available,
                "retained_count": evidence_package.get("retained_count", 0),
                "omitted_count": evidence_package.get("omitted_count", 0),
                "truncated": evidence_package.get("truncated", False),
            }
        )

        def bound_frame_diagnostics(values: dict[str, object]) -> dict[str, object]:
            remaining = max(
                0,
                budget_limits.get("max_diagnostic_bytes", 0) - execution_consumption.get("diagnostic_bytes", 0),
            )
            if not values or json_size(values) <= remaining:
                return values
            orchestration_exhausted.add("diagnostic_bytes")
            append_reason("diagnostics_truncated")
            compact = {
                "diagnostic_id": frame.get("diagnostic_id", ""),
                "proposition_evidence": {
                    "available": evidence_package_available,
                    "input_count": evidence_diagnostics.get("input_count", 0),
                    "included_count": evidence_diagnostics.get("included_count", 0),
                    "retained_count": evidence_package.get("retained_count", 0),
                    "omitted_count": evidence_package.get("omitted_count", 0),
                },
                "truncated": True,
            }
            if json_size(compact) <= remaining:
                return compact
            marker: dict[str, object] = {"truncated": True}
            result = marker if json_size(marker) <= remaining else {}
            return result

        def sized_accounting(compact: bool) -> dict[str, object]:
            """Report accounting as wide as the accounting write can make it.

            The write always applies candidacy and decides only the success and replay
            flags. JSON ``false`` is wider than ``true``, so those flags are sized as false,
            and the compact form is sized as if nothing were accepted.
            """
            finalization = accounting_finalization(
                preview_ids,
                "" if compact else accepted_statement_id,
                True,
                False,
                False,
            )
            result = accounting_diagnostics(finalization, compact)
            return result

        def sync_evidence_diagnostics() -> None:
            evidence_diagnostics.update(
                {
                    "available": evidence_package_available,
                    "retained_count": evidence_package.get("retained_count", 0),
                    "omitted_count": evidence_package.get("omitted_count", 0),
                    "truncated": evidence_package.get("truncated", False),
                }
            )
            visible_evidence_diagnostics = frame_diagnostics.get("proposition_evidence", {})
            if isinstance(visible_evidence_diagnostics, dict):
                for name in ("available", "retained_count", "omitted_count"):
                    if name in visible_evidence_diagnostics:
                        visible_evidence_diagnostics[name] = evidence_diagnostics.get(name, False)

        def complete_consumption(elapsed_ns: int, reserve: bool) -> dict:
            """Return the result's resource use for its current diagnostics and evidence package.

            With ``reserve``, the values settled only after the accounting write are sized at
            their widest: output bytes at the output limit and working memory at its limit.
            """
            max_evidence_bytes = budget_limits.get("max_evidence_bytes", 0)
            max_diagnostic_bytes = budget_limits.get("max_diagnostic_bytes", 0)
            max_working_memory_bytes = budget_limits.get("max_working_memory_bytes", 0)
            exhausted = set(execution_consumption.get("exhausted_dimensions", ())).union(orchestration_exhausted)
            if output_truncated:
                exhausted.add("output_bytes")
            if decision_report.get("budget_exhausted", "") == "fusion_memory_exhausted":
                exhausted.add("working_memory_bytes")
            package_evidence_bytes = (
                len(trusted_evidence_package_to_json(evidence_package).encode("utf-8")) if evidence_package_available else 0
            )
            evidence_count = minimal_evidence_count + evidence_package.get("retained_count", 0)
            evidence_bytes = minimal_evidence_bytes + package_evidence_bytes
            if evidence_count > budget_limits.get("max_evidence", 0):
                exhausted.add("evidence")
            if evidence_bytes > max_evidence_bytes:
                exhausted.add("evidence_bytes")
            frame_diagnostic_bytes = json_size(frame_diagnostics) if frame_diagnostics else 0
            diagnostic_bytes = execution_consumption.get("diagnostic_bytes", 0) + frame_diagnostic_bytes
            if diagnostic_bytes > max_diagnostic_bytes:
                exhausted.add("diagnostic_bytes")
            if working_memory_bytes > max_working_memory_bytes:
                exhausted.add("working_memory_bytes")
            if reserve:
                output_bytes = output_limit
                reported_working_memory = max_working_memory_bytes
                if output_limit > max_working_memory_bytes:
                    exhausted.add("working_memory_bytes")
            else:
                output_bytes = 0
                reported_working_memory = min(working_memory_bytes, max_working_memory_bytes)
            result = budget_consumption_with_changes(
                execution_consumption,
                {
                    "elapsed_ns": elapsed_ns,
                    "evidence": evidence_count,
                    "evidence_bytes": min(evidence_bytes, max_evidence_bytes),
                    "output_bytes": output_bytes,
                    "diagnostic_bytes": min(diagnostic_bytes, max_diagnostic_bytes),
                    "working_memory_bytes": reported_working_memory,
                    "exhausted_dimensions": tuple(sorted(exhausted)),
                },
            )
            return result

        def sized_output_bytes(elapsed_ns: int) -> int:
            """Encode the result as large as it can be once accounting is written."""
            value = trusted_resolution_result(**result_fields, budget=complete_consumption(elapsed_ns, True))
            result = len(trusted_resolution_result_to_json(value).encode("utf-8"))
            return result

        def trailing_item_bytes(payload: dict, count: int) -> int:
            """Bytes the last of ``count`` encoded list items occupies, with its separating comma."""
            result = json_size(payload) + (1 if count > 1 else 0)
            return result

        def package_field_bytes() -> int:
            """Encode the only top-level fields a package trim changes: package, diagnostics, budget."""
            budget_payload = budget_consumption_to_dict(complete_consumption(MAX_RESOURCE_COUNTER, True))
            result = (
                len(trusted_evidence_package_to_json(evidence_package).encode("utf-8"))
                + json_size(frame_diagnostics)
                + json_size(budget_payload)
            )
            return result

        frame_diagnostics = bound_frame_diagnostics(
            {
                "diagnostic_id": frame.get("diagnostic_id", ""),
                "plan": trusted_resolution_plan_to_dict(plan),
                "reservations": [resolver_reservation_to_dict(reservation) for reservation in execution.get("reservations", ())],
                "fusion": decision_report,
                "proposition_evidence": evidence_diagnostics,
                "accounting": sized_accounting(False),
            }
        )
        resolver_results = tuple(
            (
                trusted_resolver_result_with_changes(result, {"proposition_evidence": ()})
                if result.get("proposition_evidence", ())
                else result
            )
            for result in execution_results
        )
        response_evidence = decision.get("evidence", ())

        result_fields = {
            "outcome": outcome,
            "selected_candidate": selected,
            "selected_candidate_available": selected_available,
            "response_candidates": response_candidates,
            "evidence": response_evidence,
            "confidence": confidence,
            "confidence_available": confidence_available,
            "reason_codes": tuple(reason_codes),
            "frame_diagnostics": frame_diagnostics,
            "resolver_results": resolver_results,
            "evidence_package_available": evidence_package_available,
            "evidence_package": evidence_package,
        }
        # Nothing may fail or grow after the accounting write, so the result is fitted to the
        # output limit here. Trimming sizes against the widest elapsed time, because the real
        # one is measured just before the write.
        if sized_output_bytes(MAX_RESOURCE_COUNTER) > output_limit:
            output_truncated = True
            append_reason("output_truncated")
            frame_diagnostics = bound_frame_diagnostics(
                {
                    "diagnostic_id": frame.get("diagnostic_id", ""),
                    "fusion": {
                        "candidate_count": decision_report.get("candidate_count", 0),
                        "output_truncated": True,
                    },
                    "proposition_evidence": {
                        "available": evidence_package_available,
                        "input_count": evidence_diagnostics.get("input_count", 0),
                        "included_count": evidence_diagnostics.get("included_count", 0),
                        "retained_count": evidence_package.get("retained_count", 0),
                        "omitted_count": evidence_package.get("omitted_count", 0),
                        "output_truncated": True,
                    },
                    "accounting": sized_accounting(True),
                    "output_truncated": True,
                }
            )
            result_fields.update(
                {
                    "reason_codes": tuple(reason_codes),
                    "frame_diagnostics": frame_diagnostics,
                }
            )
            # The result is encoded once here; each trim then subtracts the exact bytes it
            # removes, since a JSON list item's encoding does not depend on its neighbours.
            # The complete encoding below re-checks the fitted result before accounting.
            sized = sized_output_bytes(MAX_RESOURCE_COUNTER)
            while resolver_results and sized > output_limit:
                evidence_check()
                sized -= trailing_item_bytes(trusted_resolver_result_to_dict(resolver_results[-1]), len(resolver_results))
                resolver_results = resolver_results[:-1]
                result_fields["resolver_results"] = resolver_results
            while evidence_package.get("records", ()) and sized > output_limit:
                evidence_check()
                before = package_field_bytes()
                evidence_package = build_evidence_package(
                    package_source_records,
                    max_records=len(evidence_package.get("records", ())) - 1,
                    max_bytes=package_byte_limit,
                )
                result_fields["evidence_package"] = evidence_package
                sync_evidence_diagnostics()
                sized += package_field_bytes() - before
            while response_evidence and sized > output_limit:
                evidence_check()
                sized -= trailing_item_bytes(trusted_evidence_reference_to_dict(response_evidence[-1]), len(response_evidence))
                response_evidence = response_evidence[:-1]
                result_fields["evidence"] = response_evidence
            while response_candidates and sized > output_limit:
                evidence_check()
                sized -= trailing_item_bytes(trusted_candidate_to_dict(response_candidates[-1]), len(response_candidates))
                response_candidates = response_candidates[:-1]
                result_fields["response_candidates"] = response_candidates
            if outcome == ResolutionOutcome.ANSWER and not response_candidates:
                outcome = ResolutionOutcome.MISS
                selected = empty_candidate()
                selected_available = False
                confidence = 0.0
                confidence_available = False
                accepted_statement_id = ""
                append_reason("answer_exceeds_output_budget")
            if outcome == ResolutionOutcome.EVIDENCE and not (
                response_candidates or response_evidence or evidence_package.get("records", ())
            ):
                outcome = ResolutionOutcome.MISS
                append_reason("no_usable_output_after_truncation")

        sync_evidence_diagnostics()
        result_fields.update(
            {
                "outcome": outcome,
                "selected_candidate": selected,
                "selected_candidate_available": selected_available,
                "confidence": confidence,
                "confidence_available": confidence_available,
                "reason_codes": tuple(reason_codes),
            }
        )
        elapsed_ns = execution_consumption.get("elapsed_ns", 0)
        started_ns = budget_limits.get("started_ns", 0)
        if started_ns:
            current_ns = resolver_clock_ns(self.internal_clock_ns)
            if isinstance(current_ns, bool) or not isinstance(current_ns, int) or current_ns < 0:
                raise InvalidRequestError("orchestrator clock_ns must return a nonnegative integer")
            elapsed_ns = max(elapsed_ns, max(0, current_ns - started_ns))
        if sized_output_bytes(elapsed_ns) > output_limit:
            raise InvalidRequestError("minimum resolution result exceeds max_output_bytes")

        run_cooperative_check(cooperative_check)
        finalization = self.internal_accounting.finalize(request_id, execution_results, accepted_statement_id)
        # The write settles only the accounting flags, each sized at its widest above, so the
        # result below is no larger than the one just checked.
        if "accounting" in frame_diagnostics:
            frame_diagnostics = {**frame_diagnostics, "accounting": accounting_diagnostics(finalization, output_truncated)}
            result_fields["frame_diagnostics"] = frame_diagnostics
        consumption = complete_consumption(elapsed_ns, False)
        for _ in range(8):
            result = trusted_resolution_result(**result_fields, budget=consumption)
            encoded_size = len(trusted_resolution_result_to_json(result).encode("utf-8"))
            fixed_exhausted = set(consumption.get("exhausted_dimensions", ()))
            max_working_memory_bytes = budget_limits.get("max_working_memory_bytes", 0)
            if encoded_size > max_working_memory_bytes:
                fixed_exhausted.add("working_memory_bytes")
            updated_consumption = budget_consumption_with_changes(
                consumption,
                {
                    "output_bytes": encoded_size,
                    "working_memory_bytes": min(
                        max_working_memory_bytes,
                        max(consumption.get("working_memory_bytes", 0), encoded_size),
                    ),
                    "exhausted_dimensions": tuple(sorted(fixed_exhausted)),
                },
            )
            if updated_consumption == consumption:
                break
            consumption = updated_consumption
        result = trusted_resolution_result(**result_fields, budget=consumption)
        if len(trusted_resolution_result_to_json(result).encode("utf-8")) > output_limit:
            raise RuntimeError("resolution result outgrew the output size checked before accounting")
        result = result, finalization
        return result
