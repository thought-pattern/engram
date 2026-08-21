"""Pure resolver adapters, bounded execution, accounting, and baseline policy."""

import hashlib
import json
import threading
from collections.abc import Callable, Mapping
from datetime import datetime
from types import MappingProxyType
from typing import TypedDict, cast

from engram.artifacts import CachedResponseArtifact, LifecycleState
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
    CLAIM_EVIDENCE_PRODUCERS,
    EXACT_RESOLVER_COST_CLASS,
    EXACT_RESOLVER_NAME,
    EXECUTION_REPORT_FIELDS,
    LEXICAL_RESOLVER_COST_CLASS,
    LEXICAL_RESOLVER_NAME,
    MAX_ACCOUNTING_VISIBLE_STATEMENT_IDS,
    MAX_PLAN_RESOLVERS,
    MAX_REASON_CODE_BYTES,
    MAX_RELATION_PLAN_ROWS,
    MAX_RESOLVER_NAME_BYTES,
    MAX_RESOURCE_COUNTER,
    MAX_STATEMENT_ID_BYTES,
    PATTERN_RESOLVER_COST_CLASS,
    PATTERN_RESOLVER_NAME,
    RESOLUTION_PLAN_ENTRY_FIELDS,
    RESOLUTION_PLAN_FIELDS,
    RESOLVER_BUDGET_FIELDS,
    RESOLVER_BUDGET_SCHEMA_VERSION,
    RESOLVER_RESERVATION_FIELDS,
    RESOLVER_RESERVATION_SCHEMA_VERSION,
    STRUCTURED_GRAPH_RESOLVER_COST_CLASS,
    STRUCTURED_GRAPH_RESOLVER_NAME,
    SUPPORT_SEMANTIC_RESOLVER_COST_CLASS,
    SUPPORT_SEMANTIC_RESOLVER_NAME,
    CanonicalResolutionStatus,
    CompositionReason,
    TemporalQueryOperator,
)
from engram.eligibility import EpochEligibilityPolicy
from engram.errors import ConflictError, InvalidRequestError, ResolutionCancelledError, ResourceNotFoundError
from engram.evidence import (
    ClaimEligibilityDecision,
    ClaimEligibilityEvaluator,
    canonicalize_claim_evidence,
    claim_evidence_record,
    evaluate_evidence_usefulness,
    evidence_usefulness_policy,
    validate_evidence_usefulness_policy,
)
from engram.fusion import CandidateFusionEngine, EngramCandidateAuthority
from engram.graph import RelationClaimProjection, claim_projection_to_dict
from engram.identity import ScopeKey, build_scoped_retrieval_key, scope_key
from engram.indexes import ExactLookupOutcome
from engram.models import record_statement_hit, record_statement_query
from engram.relation import (
    object_type_match,
    one_hop_query_plan,
    phrase_relation_result,
    resolve_canonical_predicate,
    resolve_canonical_subject,
    select_relation_claims,
)
from engram.resolution import (
    BudgetConsumption,
    BudgetLedger,
    Candidate,
    CandidateSource,
    CostClass,
    EvidenceKind,
    EvidenceReference,
    FeatureSet,
    QueryFrame,
    ResolutionOutcome,
    ResolutionResult,
    ResolverResult,
    ResolverState,
    _trusted_evidence_package_to_json,
    _trusted_resolution_result,
    _trusted_resolution_result_to_json,
    _trusted_resolver_result_with_changes,
    accounting_observation,
    budget_consumption,
    budget_consumption_from_dict,
    budget_consumption_to_dict,
    budget_consumption_with_changes,
    build_evidence_package,
    candidate as resolution_candidate,
    candidate_to_dict,
    candidate_with_changes,
    claim_evidence_path_step,
    claim_evidence_record_to_dict,
    claim_evidence_record_with_changes,
    empty_candidate,
    empty_evidence_package,
    evidence_reference,
    evidence_reference_to_dict,
    feature_set,
    resolver_result,
    resolver_result_to_dict,
    resolver_result_with_changes,
    validate_budget_consumption,
    validate_query_frame,
    validate_resolver_result,
)

ResolverBudget = TypedDict(
    "ResolverBudget",
    {
        "schema_version": int,
        "max_candidates": int,
        "max_graph_rows": int,
        "max_vector_results": int,
        "max_evidence": int,
        "max_evidence_bytes": int,
        "max_output_bytes": int,
        "max_diagnostic_bytes": int,
        "max_working_memory_bytes": int,
    },
)


def resolver_contract(value: object) -> tuple[str, CostClass, Callable[..., object], Callable[..., object]]:
    """Validate and expose the operations of one stateful resolver object."""

    name = getattr(value, "name", ())
    cost_class = getattr(value, "cost_class", ())
    available_operation = getattr(value, "available", ())
    resolve_operation = getattr(value, "resolve", ())
    if not isinstance(name, str) or not name or len(name.encode("utf-8")) > MAX_RESOLVER_NAME_BYTES:
        raise InvalidRequestError("resolver must have a bounded non-empty name")
    if not isinstance(cost_class, CostClass) or not callable(available_operation) or not callable(resolve_operation):
        raise InvalidRequestError("resolver must implement resolver operations")
    result = (name, cost_class, cast(Callable[..., object], available_operation), cast(Callable[..., object], resolve_operation))
    return result


def _run_cooperative_check(check: object) -> None:
    """Run one validated transient cancellation check."""
    if check == ():
        return
    if not callable(check):
        raise InvalidRequestError("cooperative_check must be callable")
    check()


def resolver_budget(
    max_candidates: object,
    max_graph_rows: object,
    max_vector_results: object,
    max_evidence: object,
    max_evidence_bytes: object,
    max_output_bytes: object,
    max_diagnostic_bytes: object,
    max_working_memory_bytes: object,
    schema_version: object = RESOLVER_BUDGET_SCHEMA_VERSION,
) -> ResolverBudget:
    """Build one read-only bounded lease passed to a resolver."""
    if isinstance(schema_version, bool) or not isinstance(schema_version, int):
        raise InvalidRequestError("resolver budget schema_version must be an integer")
    if schema_version != RESOLVER_BUDGET_SCHEMA_VERSION:
        raise InvalidRequestError(f"unsupported resolver budget schema_version: {schema_version}")
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
    result: ResolverBudget = {
        "schema_version": schema_version,
        "max_candidates": values["max_candidates"],
        "max_graph_rows": values["max_graph_rows"],
        "max_vector_results": values["max_vector_results"],
        "max_evidence": values["max_evidence"],
        "max_evidence_bytes": values["max_evidence_bytes"],
        "max_output_bytes": values["max_output_bytes"],
        "max_diagnostic_bytes": values["max_diagnostic_bytes"],
        "max_working_memory_bytes": values["max_working_memory_bytes"],
    }
    return result


def validate_resolver_budget(value: object) -> ResolverBudget:
    if not isinstance(value, Mapping) or set(value) != RESOLVER_BUDGET_FIELDS:
        raise InvalidRequestError("ResolverBudget has invalid fields")
    result = resolver_budget(
        schema_version=value["schema_version"],
        max_candidates=value["max_candidates"],
        max_graph_rows=value["max_graph_rows"],
        max_vector_results=value["max_vector_results"],
        max_evidence=value["max_evidence"],
        max_evidence_bytes=value["max_evidence_bytes"],
        max_output_bytes=value["max_output_bytes"],
        max_diagnostic_bytes=value["max_diagnostic_bytes"],
        max_working_memory_bytes=value["max_working_memory_bytes"],
    )
    return result


def resolver_budget_with_changes(value: object, changes: object) -> ResolverBudget:
    current = validate_resolver_budget(value)
    if not isinstance(changes, Mapping) or not set(changes).issubset(RESOLVER_BUDGET_FIELDS):
        raise InvalidRequestError("resolver budget changes contain invalid fields")
    updated: dict[str, object] = dict(current)
    updated.update(changes)
    result = validate_resolver_budget(updated)
    return result


def resolver_budget_to_dict(value: object) -> dict[str, object]:
    current = validate_resolver_budget(value)
    result = dict(current)
    return result


def resolver_budget_from_dict(value: object) -> ResolverBudget:
    if not isinstance(value, Mapping):
        raise InvalidRequestError("ResolverBudget must be an object")
    result = validate_resolver_budget(value)
    return result


def resolver_budget_to_json(value: object) -> str:
    payload = resolver_budget_to_dict(value)
    result = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return result


def resolver_budget_from_json(value: str) -> ResolverBudget:
    try:
        decoded = json.loads(value)
    except (TypeError, json.JSONDecodeError) as error:
        raise InvalidRequestError("ResolverBudget JSON must be valid JSON") from error
    result = resolver_budget_from_dict(decoded)
    return result


ResolverReservation = TypedDict(
    "ResolverReservation",
    {
        "schema_version": int,
        "resolver": str,
        "order": int,
        "lease": ResolverBudget,
        "consumption": BudgetConsumption,
    },
)


def resolver_reservation(
    resolver: object,
    order: object,
    lease: object,
    consumption: object,
    schema_version: object = RESOLVER_RESERVATION_SCHEMA_VERSION,
) -> ResolverReservation:
    """Build one inspectable lease and resulting consumption."""
    if isinstance(schema_version, bool) or not isinstance(schema_version, int):
        raise InvalidRequestError("ResolverReservation schema_version must be an integer")
    if schema_version != RESOLVER_RESERVATION_SCHEMA_VERSION:
        raise InvalidRequestError(f"unsupported resolver reservation schema_version: {schema_version}")
    if not isinstance(resolver, str) or not resolver or len(resolver.encode("utf-8")) > MAX_RESOLVER_NAME_BYTES:
        raise InvalidRequestError("reservation resolver must be a bounded non-empty string")
    if isinstance(order, bool) or not isinstance(order, int) or not 0 <= order < MAX_PLAN_RESOLVERS:
        raise InvalidRequestError("reservation order is out of bounds")
    try:
        validated_lease = validate_resolver_budget(lease)
        validated_consumption = validate_budget_consumption(consumption)
    except InvalidRequestError as error:
        raise InvalidRequestError("reservation lease and consumption have invalid types") from error
    result: ResolverReservation = {
        "schema_version": schema_version,
        "resolver": resolver,
        "order": order,
        "lease": validated_lease,
        "consumption": validated_consumption,
    }
    return result


def validate_resolver_reservation(value: object) -> ResolverReservation:
    if not isinstance(value, Mapping) or frozenset(value) != RESOLVER_RESERVATION_FIELDS:
        raise InvalidRequestError("ResolverReservation has invalid fields")
    result = resolver_reservation(
        value["resolver"],
        value["order"],
        value["lease"],
        value["consumption"],
        value["schema_version"],
    )
    return result


def resolver_reservation_with_changes(value: object, changes: object) -> ResolverReservation:
    current = validate_resolver_reservation(value)
    if not isinstance(changes, Mapping) or not frozenset(changes).issubset(RESOLVER_RESERVATION_FIELDS):
        raise InvalidRequestError("resolver reservation changes contain invalid fields")
    updated: dict[str, object] = dict(current)
    updated.update(changes)
    result = validate_resolver_reservation(updated)
    return result


def resolver_reservation_to_dict(value: object) -> dict[str, object]:
    current = validate_resolver_reservation(value)
    result = {
        "schema_version": current["schema_version"],
        "resolver": current["resolver"],
        "order": current["order"],
        "lease": resolver_budget_to_dict(current["lease"]),
        "consumption": budget_consumption_to_dict(current["consumption"]),
    }
    return result


def resolver_reservation_from_dict(value: object) -> ResolverReservation:
    if not isinstance(value, Mapping) or frozenset(value) != RESOLVER_RESERVATION_FIELDS:
        raise InvalidRequestError("ResolverReservation has invalid fields")
    lease = value["lease"]
    consumption = value["consumption"]
    if not isinstance(lease, Mapping) or not isinstance(consumption, Mapping):
        raise InvalidRequestError("ResolverReservation nested records must be objects")
    result = resolver_reservation(
        value["resolver"],
        value["order"],
        resolver_budget_from_dict(lease),
        budget_consumption_from_dict(consumption),
        value["schema_version"],
    )
    return result


def resolver_reservation_to_json(value: object) -> str:
    payload = resolver_reservation_to_dict(value)
    result = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return result


def resolver_reservation_from_json(value: str) -> ResolverReservation:
    try:
        decoded = json.loads(value)
    except (TypeError, json.JSONDecodeError) as error:
        raise InvalidRequestError("ResolverReservation JSON must be valid JSON") from error
    result = resolver_reservation_from_dict(decoded)
    return result


ResolutionPlanEntry = TypedDict(
    "ResolutionPlanEntry",
    {
        "resolver": object,
        "order": int,
        "configured": bool,
        "available": bool,
        "reason_code": str,
    },
)


def resolution_plan_entry(
    resolver: object,
    order: object,
    configured: object,
    available: object,
    reason_code: object,
) -> ResolutionPlanEntry:
    """Build one inspectable configured resolver decision."""
    resolver_contract(resolver)
    if isinstance(order, bool) or not isinstance(order, int) or not 0 <= order < MAX_PLAN_RESOLVERS:
        raise InvalidRequestError("plan order is out of bounds")
    if not isinstance(configured, bool) or not isinstance(available, bool):
        raise InvalidRequestError("plan configured and available must be booleans")
    if not isinstance(reason_code, str) or len(reason_code.encode("utf-8")) > MAX_REASON_CODE_BYTES:
        raise InvalidRequestError("plan reason_code must be a bounded string")
    result: ResolutionPlanEntry = {
        "resolver": resolver,
        "order": order,
        "configured": configured,
        "available": available,
        "reason_code": reason_code,
    }
    return result


def validate_resolution_plan_entry(value: object) -> ResolutionPlanEntry:
    if not isinstance(value, Mapping) or frozenset(value) != RESOLUTION_PLAN_ENTRY_FIELDS:
        raise InvalidRequestError("ResolutionPlanEntry has invalid fields")
    result = resolution_plan_entry(
        value["resolver"],
        value["order"],
        value["configured"],
        value["available"],
        value["reason_code"],
    )
    return result


def resolution_plan_entry_to_dict(value: object) -> dict[str, object]:
    current = validate_resolution_plan_entry(value)
    resolver = current["resolver"]
    name, cost_class, _, _ = resolver_contract(resolver)
    result = {
        "resolver": name,
        "cost_class": cost_class.value,
        "order": current["order"],
        "configured": current["configured"],
        "available": current["available"],
        "reason_code": current["reason_code"],
    }
    return result


ResolutionPlan = TypedDict("ResolutionPlan", {"entries": tuple[ResolutionPlanEntry, ...]})


def resolution_plan(entries: object) -> ResolutionPlan:
    """Build a deterministic plan retaining configured skip and availability decisions."""
    if not isinstance(entries, tuple):
        raise InvalidRequestError("plan entries must be a tuple")
    if len(entries) > MAX_PLAN_RESOLVERS:
        raise InvalidRequestError(f"plan entries exceed the limit of {MAX_PLAN_RESOLVERS}")
    validated_entries = tuple(validate_resolution_plan_entry(value) for value in entries)
    if tuple(value["order"] for value in validated_entries) != tuple(range(len(validated_entries))):
        raise InvalidRequestError("plan entry order must be contiguous")
    names = []
    for entry in validated_entries:
        name, _, _, _ = resolver_contract(entry["resolver"])
        names.append(name)
    if len(set(names)) != len(names):
        raise InvalidRequestError("plan resolver names must be unique")
    result: ResolutionPlan = {"entries": validated_entries}
    return result


def validate_resolution_plan(value: object) -> ResolutionPlan:
    if not isinstance(value, Mapping) or frozenset(value) != RESOLUTION_PLAN_FIELDS:
        raise InvalidRequestError("ResolutionPlan has invalid fields")
    result = resolution_plan(value["entries"])
    return result


def resolution_plan_to_dict(value: object) -> dict[str, object]:
    current = validate_resolution_plan(value)
    entries = [resolution_plan_entry_to_dict(entry) for entry in current["entries"]]
    result: dict[str, object] = {"entries": entries}
    return result


def _json_value(value: object) -> object:
    if isinstance(value, Mapping):
        result = {key: _json_value(item) for key, item in value.items()}
        return result
    if isinstance(value, tuple):
        result = [_json_value(item) for item in value]
        return result
    if isinstance(value, list):
        result = [_json_value(item) for item in value]
        return result
    return value


def _json_size(value: object) -> int:
    result = len(
        json.dumps(
            _json_value(value),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    )
    return result


def _working_size(value: object, seen=()) -> int:
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
    if isinstance(value, Mapping):
        result = 64 + sum(_working_size(key, visited) + _working_size(item, visited) for key, item in value.items())
        return result
    if isinstance(value, (list, tuple, set, frozenset)):
        result = 64 + sum(_working_size(item, visited) for item in value)
        return result
    result = len(str(value).encode("utf-8")) + 64
    return result


def _candidate_id(source: CandidateSource, statement_id: str, diagnostic_id: str) -> str:
    digest = hashlib.sha256(f"{diagnostic_id}:{source.value}:{statement_id}".encode()).hexdigest()
    result = f"candidate:sha256:{digest}"
    return result


def _statement_metadata(statement: Mapping[str, object]) -> tuple[Mapping[str, object], ScopeKey, LifecycleState]:
    template = statement.get("template", {})
    if not isinstance(template, Mapping):
        result = (MappingProxyType({}), scope_key(), LifecycleState.ACTIVE)
        return result
    tapestry = template.get("tapestry", {})
    if not isinstance(tapestry, Mapping):
        tapestry = MappingProxyType({})
    metadata = tapestry.get("metadata", {})
    if not isinstance(metadata, Mapping):
        metadata = MappingProxyType({})
    namespace = tapestry.get("namespace", "")
    context_fingerprint = tapestry.get("context_fingerprint", "")
    if not isinstance(namespace, str) or not isinstance(context_fingerprint, str):
        result = (metadata, scope_key(), LifecycleState.ACTIVE)
        return result
    response_artifact = template.get("response_artifact", {})
    lifecycle = LifecycleState.ACTIVE
    if isinstance(response_artifact, Mapping):
        lifecycle_value = response_artifact.get("lifecycle", "ACTIVE")
        if isinstance(lifecycle_value, str):
            try:
                lifecycle = LifecycleState(lifecycle_value)
            except ValueError:
                lifecycle = LifecycleState.RETIRED
    try:
        scope = scope_key(namespace=namespace, context_fingerprint=context_fingerprint)
    except InvalidRequestError:
        scope = scope_key()
    result = metadata, scope, lifecycle
    return result


def _matches_frame(statement: Mapping[str, object], frame: QueryFrame, *, allow_pattern: bool) -> bool:
    if bool(statement.get("pattern", "")) != allow_pattern:
        result = False
        return result
    template = statement.get("template", {})
    tapestry: Mapping[str, object] = MappingProxyType({})
    metadata: Mapping[str, object] = MappingProxyType({})
    if isinstance(template, Mapping):
        tapestry_value = template.get("tapestry", {})
        if isinstance(tapestry_value, Mapping):
            tapestry = tapestry_value
        metadata_value = tapestry.get("metadata", {})
        if isinstance(metadata_value, Mapping):
            metadata = metadata_value
        namespace = tapestry.get("namespace", "")
        context_fingerprint = tapestry.get("context_fingerprint", "")
        if isinstance(namespace, str) and isinstance(context_fingerprint, str) and (namespace or context_fingerprint):
            try:
                statement_scope = scope_key(namespace=namespace, context_fingerprint=context_fingerprint)
            except InvalidRequestError:
                statement_scope = scope_key()
            if statement_scope != scope_key() and statement_scope != frame["scope"]:
                result = False
                return result
    lifecycle = LifecycleState.ACTIVE
    response_artifact = template.get("response_artifact", {}) if isinstance(template, Mapping) else {}
    if isinstance(response_artifact, Mapping):
        lifecycle_value = response_artifact.get("lifecycle", "ACTIVE")
        if isinstance(lifecycle_value, str):
            try:
                lifecycle = LifecycleState(lifecycle_value)
            except ValueError:
                lifecycle = LifecycleState.RETIRED
    if lifecycle != LifecycleState.ACTIVE:
        result = False
        return result
    source_label = statement.get("source_label", "")
    if frame["required_source_label"] and source_label != frame["required_source_label"]:
        result = False
        return result
    result = all(key in metadata and metadata[key] == value for key, value in frame["required_metadata"].items())
    return result


def _artifact_matches_frame(artifact: CachedResponseArtifact, frame: QueryFrame) -> bool:
    if artifact["scope"] != frame["scope"] or artifact["lifecycle"] != LifecycleState.ACTIVE:
        result = False
        return result
    if frame["required_source_label"] and artifact["provenance"]["source_label"] != frame["required_source_label"]:
        result = False
        return result
    result = all(
        key in artifact["metadata"] and artifact["metadata"][key] == value for key, value in frame["required_metadata"].items()
    )
    return result


def _pattern_input(frame: QueryFrame) -> str:
    """Return the representation that existed before retrieval-only rewrites."""
    result = frame["rewrite_chain"][0]["input_text"] if frame["rewrite_chain"] else frame["resolved_text"]
    return result


def _legacy_candidate(
    statement: Mapping[str, object],
    frame: QueryFrame,
    source: CandidateSource,
    features: FeatureSet,
    evidence: tuple[EvidenceReference, ...] = (),
    diagnostics: Mapping[str, object] = MappingProxyType({}),
    response: str = "",
) -> Candidate:
    statement_id = str(statement["id"])
    metadata, statement_scope, lifecycle = _statement_metadata(statement)
    selected_scope = frame["scope"] if statement_scope == scope_key() else statement_scope
    selected_response = response or str(statement["text"])
    result = resolution_candidate(
        candidate_id=_candidate_id(source, statement_id, frame["diagnostic_id"]),
        statement_id=statement_id,
        response=selected_response,
        source=source,
        features=features,
        evidence=evidence,
        scope=selected_scope,
        lifecycle=lifecycle,
        provenance={
            "source_label": str(statement.get("source_label", "")),
            "introduced_by_user_id": str(statement.get("introduced_by_user_id", "")),
            "metadata_present": bool(metadata),
        },
        diagnostics=diagnostics,
    )
    return result


def _exhausted_result(name: str, dimensions: tuple[str, ...]) -> ResolverResult:
    result = resolver_result(
        resolver=name,
        state=ResolverState.EXHAUSTED,
        reason_code=f"{dimensions[0]}_budget",
        consumption=budget_consumption(resolvers=1, exhausted_dimensions=dimensions),
    )
    return result


def _memory_exhausted_result(name: str) -> ResolverResult:
    result = _exhausted_result(name, ("working_memory_bytes",))
    return result


class ExactResolver:
    """Adapter over the Section 3 contextual exact repository."""

    def __init__(self, engram, clock_ns: Callable[[], int]) -> None:
        self.name = EXACT_RESOLVER_NAME
        self.cost_class = EXACT_RESOLVER_COST_CLASS
        self._engram = engram
        self._clock_ns = clock_ns

    def available(self, frame: QueryFrame) -> bool:
        result = frame["eligibility_context"]["artifact_repository_available"]
        return result

    def resolve(self, frame: QueryFrame, budget: ResolverBudget) -> ResolverResult:
        if not budget["max_candidates"] or not budget["max_working_memory_bytes"]:
            dimension = "candidates" if not budget["max_candidates"] else "working_memory_bytes"
            result = _exhausted_result(self.name, (dimension,))
            return result
        started = self._clock_ns()
        key = build_scoped_retrieval_key(frame["scope"], frame["resolved_text"])
        contextual = self._engram.response_repository.exact_lookup(
            key,
            frame["eligibility_context"],
            EpochEligibilityPolicy.MATCH_WHEN_ARTIFACT_AVAILABLE,
        )
        lookup = contextual["lookup"]
        if lookup["outcome"] != ExactLookupOutcome.FOUND:
            result = resolver_result(
                resolver=self.name,
                state=ResolverState.COMPLETED,
                reason_code=f"exact_{lookup['outcome'].value}",
                diagnostics={
                    "owner_count": len(lookup["owner_statement_ids"]),
                    "truncated": lookup["truncated"],
                    "index_refreshed": contextual["index_refreshed"],
                },
                consumption=budget_consumption(elapsed_ns=max(0, self._clock_ns() - started), resolvers=1),
            )
            return result
        artifact = self._engram.response_repository._trusted_get_artifact(lookup["statement_id"])
        if not _artifact_matches_frame(artifact, frame):
            result = resolver_result(
                resolver=self.name,
                state=ResolverState.COMPLETED,
                reason_code="exact_required_filter_excluded",
                consumption=budget_consumption(elapsed_ns=max(0, self._clock_ns() - started), resolvers=1),
            )
            return result
        candidate = resolution_candidate(
            candidate_id=_candidate_id(CandidateSource.EXACT, artifact["statement_id"], frame["diagnostic_id"]),
            statement_id=artifact["statement_id"],
            response=artifact["response"],
            source=CandidateSource.EXACT,
            features=feature_set(values={"exact_match": 1.0}, unavailable=()),
            evidence=tuple(
                evidence_reference(
                    evidence_id=claim_id,
                    resolver=self.name,
                    kind=EvidenceKind.SUPPORT,
                    scope=artifact["scope"],
                    provenance={"support_linked": True},
                )
                for claim_id in artifact["support_claim_ids"][: budget["max_evidence"]]
            ),
            scope=artifact["scope"],
            lifecycle=artifact["lifecycle"],
            provenance={
                "retrieval_origin": lookup["provenance"],
                "representation": lookup["representation"],
                "generation": artifact["generation"],
                "source_label": artifact["provenance"]["source_label"],
            },
            diagnostics={"context_signature": contextual["context_signature"]},
        )
        if _json_size(candidate_to_dict(candidate)) > budget["max_working_memory_bytes"]:
            result = _exhausted_result(self.name, ("working_memory_bytes",))
            return result
        result = resolver_result(
            resolver=self.name,
            state=ResolverState.COMPLETED,
            reason_code="exact_found",
            candidates=(candidate,),
            accounting=(accounting_observation(artifact["statement_id"]),),
            consumption=budget_consumption(
                elapsed_ns=max(0, self._clock_ns() - started),
                resolvers=1,
                candidates=1,
                evidence=len(candidate["evidence"]),
            ),
        )
        return result


class PatternResolver:
    """Pure adapter over statement-backed AIML matching."""

    def __init__(self, engram, clock_ns: Callable[[], int]) -> None:
        self.name = PATTERN_RESOLVER_NAME
        self.cost_class = PATTERN_RESOLVER_COST_CLASS
        self._engram = engram
        self._clock_ns = clock_ns

    def available(self, frame: QueryFrame) -> bool:
        result = bool(len(self._engram.pattern_matcher))
        return result

    def resolve(self, frame: QueryFrame, budget: ResolverBudget) -> ResolverResult:
        if not budget["max_candidates"] or not budget["max_working_memory_bytes"]:
            dimension = "candidates" if not budget["max_candidates"] else "working_memory_bytes"
            result = _exhausted_result(self.name, (dimension,))
            return result
        started = self._clock_ns()
        try:
            discoveries = self._engram.pattern_candidates(
                _pattern_input(frame),
                limit=budget["max_candidates"],
                max_working_memory_bytes=budget["max_working_memory_bytes"],
            )
        except MemoryError:
            result = _memory_exhausted_result(self.name)
            return result
        candidates = []
        accounting = []
        for discovery in discoveries:
            statement = discovery["statement"]
            if not _matches_frame(statement, frame, allow_pattern=True):
                continue
            pattern = str(discovery["pattern"])
            words = pattern.split()
            wildcard_count = sum(1 for word in words if word.lstrip("$") in {"*", "_", "#", "^"})
            specificity = float(max(0, len(words) - wildcard_count))
            candidate = _legacy_candidate(
                statement,
                frame,
                CandidateSource.PATTERN,
                feature_set(values={"pattern_specificity": specificity}, unavailable=()),
                diagnostics={
                    "pattern": pattern,
                    "captures": discovery["captured"],
                    "that_captures": discovery["thatstars"],
                    "topic_captures": discovery["topicstars"],
                    "that": discovery["that"],
                    "topic": discovery["topic"],
                },
                response=str(discovery["response"]),
            )
            candidates.append(candidate)
            accounting.append(accounting_observation(candidate["statement_id"]))
            if len(candidates) >= budget["max_candidates"]:
                break
        result = resolver_result(
            resolver=self.name,
            state=ResolverState.COMPLETED,
            reason_code="pattern_candidates" if candidates else "pattern_miss",
            candidates=tuple(candidates),
            accounting=tuple(accounting),
            consumption=budget_consumption(elapsed_ns=max(0, self._clock_ns() - started), resolvers=1, candidates=len(candidates)),
        )
        return result


class LexicalResolver:
    """Pure adapter over existing lexical scoring and diagnostics."""

    def __init__(self, engram, clock_ns: Callable[[], int]) -> None:
        self.name = LEXICAL_RESOLVER_NAME
        self.cost_class = LEXICAL_RESOLVER_COST_CLASS
        self._engram = engram
        self._clock_ns = clock_ns

    def available(self, frame: QueryFrame) -> bool:
        result = bool(self._engram.keywords)
        return result

    def resolve(self, frame: QueryFrame, budget: ResolverBudget) -> ResolverResult:
        if not budget["max_candidates"] or not budget["max_working_memory_bytes"]:
            dimension = "candidates" if not budget["max_candidates"] else "working_memory_bytes"
            result = _exhausted_result(self.name, (dimension,))
            return result
        started = self._clock_ns()
        try:
            discovered = self._engram.query_candidates(
                frame["resolved_text"],
                limit=budget["max_candidates"],
                statement_filter=lambda statement: _matches_frame(statement, frame, allow_pattern=False),
                reference_time=datetime.fromisoformat(frame["eligibility_context"]["evaluation_time"].replace("Z", "+00:00")),
                max_working_memory_bytes=budget["max_working_memory_bytes"],
            )
        except MemoryError:
            result = _memory_exhausted_result(self.name)
            return result
        keywords = tuple(dict.fromkeys(str(keyword) for keyword in discovered["keywords"]))
        discovery_features = discovered["features"]
        discovery_diagnostics = discovered["diagnostics"]
        candidates = tuple(
            _legacy_candidate(
                statement,
                frame,
                CandidateSource.LEXICAL,
                feature_set(
                    values={
                        "exact_match_ratio": discovery_features[statement["id"]]["exact_match_ratio"],
                        "keyword_hit_rate": discovery_features[statement["id"]]["keyword_hit_rate"],
                        "lexical_overlap": discovery_features[statement["id"]]["overlap"],
                        "lexical_score": float(score),
                        "phrase_keywords_enabled": float(discovery_diagnostics["phrase_keywords_enabled"]),
                        "priority": discovery_features[statement["id"]]["priority"],
                        "recency": discovery_features[statement["id"]]["recency"],
                        "spelling_correction_applied": float(discovery_diagnostics["spelling_correction_applied"]),
                        "synonym_match_ratio": discovery_features[statement["id"]]["synonym_match_ratio"],
                    },
                    unavailable=("lemma_match_ratio", "stem_match_ratio"),
                ),
                diagnostics={
                    "keywords": list(keywords),
                    "synonym_expansion_count": discovery_diagnostics["synonym_expansion_count"],
                },
            )
            for statement, score in discovered["matches"]
        )
        result = resolver_result(
            resolver=self.name,
            state=ResolverState.COMPLETED,
            reason_code="lexical_candidates" if candidates else "lexical_miss",
            candidates=candidates,
            accounting=tuple(accounting_observation(candidate["statement_id"], keywords) for candidate in candidates),
            diagnostics={"keywords": list(keywords)},
            consumption=budget_consumption(
                elapsed_ns=max(0, self._clock_ns() - started),
                resolvers=1,
                candidates=len(candidates),
                working_memory_bytes=int(discovery_diagnostics["working_memory_bytes"]),
            ),
        )
        return result


class StructuredGraphResolver:
    """Pure full-Claim evidence adapter over fixed structured graph projections."""

    def __init__(self, engram, clock_ns: Callable[[], int], eligibility_evaluator: object = ()) -> None:
        self.name = STRUCTURED_GRAPH_RESOLVER_NAME
        self.cost_class = STRUCTURED_GRAPH_RESOLVER_COST_CLASS
        self._engram = engram
        self._clock_ns = clock_ns
        if eligibility_evaluator == ():
            selected_evaluator = ClaimEligibilityEvaluator(getattr(engram, "claim_visibility_authority", ()))
        elif isinstance(eligibility_evaluator, ClaimEligibilityEvaluator):
            selected_evaluator = eligibility_evaluator
        else:
            raise InvalidRequestError("structured graph eligibility_evaluator must be ClaimEligibilityEvaluator")
        self._eligibility_evaluator: ClaimEligibilityEvaluator = selected_evaluator

    def available(self, frame: QueryFrame) -> bool:
        result = bool(self._engram.graph_client)
        return result

    def _composition_result(
        self,
        frame: QueryFrame,
        budget: ResolverBudget,
        started: int,
        cooperative_check: object = (),
    ) -> tuple[ResolverResult, ...]:
        """Compile and execute a conservative composed request through fixed one-hop reads."""
        normalized = frame["resolved_text"].casefold()
        if "'s" not in normalized and " of " not in normalized:
            result = ()
            return result
        graph_rows = 0

        def check() -> None:
            _run_cooperative_check(cooperative_check)

        def entity_lookup(surface: str, *, limit: int, cooperative_check=()) -> list:
            nonlocal graph_rows
            remaining = max(0, budget["max_graph_rows"] - graph_rows)
            if not remaining:
                return []
            rows = self._engram.canonical_entity_matches(
                surface,
                limit=min(limit, remaining),
                cooperative_check=cooperative_check,
            )
            graph_rows += len(rows)
            return rows

        def predicate_lookup(surface: str, *, limit: int, cooperative_check=()) -> list:
            nonlocal graph_rows
            remaining = max(0, budget["max_graph_rows"] - graph_rows)
            if not remaining:
                return []
            rows = self._engram.canonical_predicate_matches(
                surface,
                limit=min(limit, remaining),
                cooperative_check=cooperative_check,
            )
            graph_rows += len(rows)
            return rows

        subject = resolve_canonical_subject(frame, entity_lookup, cooperative_check=check)
        if subject["status"] != CanonicalResolutionStatus.SELECTED:
            reason = (
                CompositionReason.IDENTITY_AMBIGUOUS
                if subject["status"] == CanonicalResolutionStatus.AMBIGUOUS
                else CompositionReason.IDENTITY_MISS
            )
            result = resolver_result(
                resolver=self.name,
                state=ResolverState.COMPLETED,
                reason_code=reason.value,
                diagnostics={"composition": True, "entity_status": subject["status"].value},
                consumption=budget_consumption(
                    elapsed_ns=max(0, self._clock_ns() - started),
                    resolvers=1,
                    graph_rows=graph_rows,
                ),
            )
            return (result,)
        try:
            predicates = resolve_composition_predicates(frame, subject, predicate_lookup, check)
            operator = graph_composition_operator(frame)
            remaining_rows = budget["max_graph_rows"] - graph_rows
            if remaining_rows < 2 * len(predicates):
                result = resolver_result(
                    resolver=self.name,
                    state=ResolverState.COMPLETED,
                    reason_code=CompositionReason.ROW_LIMIT.value,
                    diagnostics={"composition": True, "entity_status": subject["status"].value},
                    consumption=budget_consumption(
                        elapsed_ns=max(0, self._clock_ns() - started),
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
                frame["expected_object_type"],
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
                diagnostics={"composition": True, "entity_status": subject["status"].value},
                consumption=budget_consumption(
                    elapsed_ns=max(0, self._clock_ns() - started),
                    resolvers=1,
                    graph_rows=graph_rows,
                ),
            )
            return (result,)

        def query(subject_id: str, predicate_id: str, limit: int) -> list[RelationClaimProjection]:
            return self._engram.relation_one_hop_claim_projections(
                subject_id,
                predicate_id,
                row_limit=limit,
                include_historical=frame["temporal_query"]["operator"]
                not in {
                    TemporalQueryOperator.UNSPECIFIED,
                    TemporalQueryOperator.CURRENT,
                    TemporalQueryOperator.NOW,
                },
                cooperative_check=check,
                max_working_memory_bytes=budget["max_working_memory_bytes"],
            )

        execution = execute_composition_plan(
            plan,
            query,
            lambda projection: self._eligibility_evaluator.evaluate(projection, frame),
            lambda projection: self._eligibility_evaluator.revalidate(
                projection,
                frame,
                self._engram.current_claim_projection,
            ),
            check,
        )
        graph_rows += execution["graph_rows"]
        composition_direct = execution["direct_result"]
        direct_suppression_reasons: set[str] = set()
        for path in execution["complete_paths"]:
            for entry in path:
                projection = entry["claim"]["projection"]
                if not projection["supplied_trust_available"] or not projection["supplied_trust_version_available"]:
                    composition_direct = False
                    direct_suppression_reasons.add(CompositionReason.TRUST_UNAVAILABLE.value)
                if entry["claim"]["predicate_cardinality"].value == "UNKNOWN":
                    composition_direct = False
                    direct_suppression_reasons.add(CompositionReason.CARDINALITY_UNKNOWN.value)
                temporal = frame["temporal_query"]
                if temporal["operator"] not in {
                    TemporalQueryOperator.UNSPECIFIED,
                    TemporalQueryOperator.CURRENT,
                    TemporalQueryOperator.NOW,
                }:
                    bounds_available = (
                        projection["valid_from_available"] and projection["valid_to_available"]
                        if temporal["axis"].value == "valid_time"
                        else projection["system_from_available"] and projection["system_to_available"]
                    )
                    if not bounds_available:
                        composition_direct = False
                        direct_suppression_reasons.add(CompositionReason.TEMPORAL_BOUNDS_OPEN.value)
        selected_paths = execution["complete_paths"] or execution["partial_paths"]
        records = []
        for path in selected_paths:
            if not path:
                continue
            terminal = path[-1]
            base = claim_evidence_record(terminal["claim"]["projection"], terminal["decision"], frame, self.name)
            aggregation_inputs = plan["aggregation_inputs"]
            path_steps = tuple(
                claim_evidence_path_step(
                    position,
                    entry["claim"]["projection"]["claim_id"],
                    entry["claim"]["projection"]["subject_entity_id"],
                    entry["claim"]["projection"]["predicate_id"],
                    entry["claim"]["projection"]["object_entity_id"],
                    plan["operator"],
                    entry["step"]["subject_binding"],
                    entry["step"]["object_binding"],
                    (
                        "canonical_identity",
                        "object_type",
                        "publication_revalidation",
                        "temporal_eligibility",
                        "visibility",
                    ),
                    aggregation_inputs if position == len(path) - 1 else (),
                )
                for position, entry in enumerate(path)
            )
            reasons = {
                *base["selection_reasons"],
                "composition_plan_match",
                *[reason.value for reason in execution["reasons"]],
                *direct_suppression_reasons,
            }
            records.append(
                claim_evidence_record_with_changes(
                    base,
                    {
                        "schema_version": 2,
                        "path": path_steps,
                        "selection_reasons": tuple(sorted(reasons)),
                    },
                )
            )
        records.sort(key=lambda record: record["claim_id"])
        if len(records) > budget["max_evidence"]:
            records = records[: budget["max_evidence"]]
        candidates = []
        if composition_direct and execution["complete_paths"] and budget["max_candidates"]:
            response = phrase_composition_result(plan, execution)
            selected_path = execution["complete_paths"][0]
            claim_ids = tuple(entry["claim"]["projection"]["claim_id"] for entry in selected_path)
            identity_chain = tuple(
                (
                    entry["claim"]["projection"]["subject_entity_id"],
                    entry["claim"]["projection"]["predicate_id"],
                    entry["claim"]["projection"]["object_entity_id"],
                )
                for entry in selected_path
            )
            trust_chain = tuple(
                (
                    entry["claim"]["projection"]["supplied_trust"],
                    entry["claim"]["projection"]["supplied_trust_version"],
                )
                for entry in selected_path
            )
            references = tuple(
                evidence_reference(
                    evidence_id=claim_id,
                    resolver=self.name,
                    kind=EvidenceKind.CLAIM,
                    scope=frame["scope"],
                    provenance={"composition_path": True},
                    diagnostics={},
                )
                for claim_id in claim_ids
            )
            composition_id = (
                "composition:"
                + hashlib.sha256(
                    json.dumps(
                        {
                            "operator": plan["operator"].value,
                            "claim_ids": claim_ids,
                            "diagnostic_id": frame["diagnostic_id"],
                        },
                        sort_keys=True,
                    ).encode("utf-8")
                ).hexdigest()
            )
            candidates.append(
                resolution_candidate(
                    candidate_id=composition_id,
                    statement_id=composition_id,
                    response=response,
                    source=CandidateSource.UTILITY,
                    features=feature_set(
                        values={"entity_match": subject["score"], "relation_match": 1.0, "object_type_match": 1.0},
                    ),
                    evidence=references,
                    scope=frame["scope"],
                    lifecycle=LifecycleState.ACTIVE,
                    provenance={
                        "producer": "graph_composition_v1",
                        "operator": plan["operator"].value,
                        "root_entity_id": plan["root_entity_id"],
                        "root_label": plan["root_label"],
                        "predicate_labels": tuple(entry["step"]["predicate_label"] for entry in selected_path),
                        "claim_ids": claim_ids,
                        "identity_chain": identity_chain,
                        "trust_chain": trust_chain,
                        "terminal_labels": execution["terminal_labels"],
                        "terminal_types": tuple(value.value for value in execution["terminal_types"]),
                        "truth_value": execution["truth_value"],
                        "truth_available": execution["truth_available"],
                        "aggregate_value": execution["aggregate_value"],
                        "aggregate_value_available": execution["aggregate_value_available"],
                    },
                    diagnostics={
                        "plan": composition_plan_to_dict(plan),
                        "reasons": tuple(reason.value for reason in execution["reasons"]),
                    },
                )
            )

        exhausted = set()

        def record_bytes() -> int:
            return _json_size([claim_evidence_record_to_dict(record) for record in records]) if records else 0

        while records and record_bytes() > budget["max_evidence_bytes"]:
            records.pop()
            exhausted.add("evidence_bytes")
        output_bytes = record_bytes() + sum(_json_size(candidate_to_dict(value)) for value in candidates)
        while candidates and output_bytes > budget["max_output_bytes"]:
            candidates.pop()
            exhausted.add("output_bytes")
            output_bytes = record_bytes()
        if candidates and not records:
            candidates.clear()
            output_bytes = 0
        working_memory = _working_size(execution) + record_bytes() + output_bytes
        if working_memory > budget["max_working_memory_bytes"]:
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
            claim_evidence=tuple(records),
            diagnostics={
                "composition": True,
                "operator": plan["operator"].value,
                "complete_paths": len(execution["complete_paths"]),
                "partial_paths": len(execution["partial_paths"]),
                "truncated": execution["truncated"],
                "reasons": tuple(reason.value for reason in execution["reasons"]),
                "plan": composition_plan_to_dict(plan),
            },
            consumption=budget_consumption(
                elapsed_ns=max(0, self._clock_ns() - started),
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

    def _relation_result(
        self,
        frame: QueryFrame,
        budget: ResolverBudget,
        started: int,
        cooperative_check: object = (),
    ) -> tuple[ResolverResult, ...]:
        """Interpret and execute one canonical one-hop plan, or select legacy fallback."""
        if not self._engram.graph_client:
            result = ()
            return result
        graph_rows = 0

        def check() -> None:
            _run_cooperative_check(cooperative_check)

        def entity_lookup(surface: str, *, limit: int, cooperative_check=()) -> list:
            nonlocal graph_rows
            remaining = max(0, budget["max_graph_rows"] - graph_rows - 2)
            if not remaining:
                return []
            rows = self._engram.canonical_entity_matches(
                surface,
                limit=min(limit, remaining),
                cooperative_check=cooperative_check,
            )
            graph_rows += len(rows)
            return rows

        def predicate_lookup(surface: str, *, limit: int, cooperative_check=()) -> list:
            nonlocal graph_rows
            remaining = max(0, budget["max_graph_rows"] - graph_rows - 2)
            if not remaining:
                return []
            rows = self._engram.canonical_predicate_matches(
                surface,
                limit=min(limit, remaining),
                cooperative_check=cooperative_check,
            )
            graph_rows += len(rows)
            return rows

        subject = resolve_canonical_subject(frame, entity_lookup, cooperative_check=check)
        predicate = resolve_canonical_predicate(frame, predicate_lookup, cooperative_check=check)
        statuses = (subject["status"], predicate["status"])
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
                    "entity_status": subject["status"].value,
                    "entity_candidates": len(subject["candidate_ids"]),
                    "predicate_status": predicate["status"].value,
                    "predicate_candidates": len(predicate["candidate_ids"]),
                },
                consumption=budget_consumption(
                    elapsed_ns=max(0, self._clock_ns() - started),
                    resolvers=1,
                    graph_rows=graph_rows,
                ),
            )
            return (result,)
        remaining_rows = budget["max_graph_rows"] - graph_rows
        if remaining_rows < 2:
            result = _exhausted_result(self.name, ("graph_rows",))
            return (result,)
        plan = one_hop_query_plan(
            subject,
            predicate,
            frame["expected_object_type"],
            max_rows=min(MAX_RELATION_PLAN_ROWS, budget["max_evidence"], max(1, remaining_rows // 2)),
        )

        results = self._engram.relation_one_hop_claim_projections(
            plan["subject_entity_id"],
            plan["predicate_id"],
            row_limit=plan["max_rows"],
            include_historical=frame["temporal_query"]["operator"]
            not in {
                TemporalQueryOperator.UNSPECIFIED,
                TemporalQueryOperator.CURRENT,
                TemporalQueryOperator.NOW,
            },
            cooperative_check=check,
            max_working_memory_bytes=budget["max_working_memory_bytes"],
        )
        graph_rows += len(results)
        retained: list[tuple[RelationClaimProjection, ClaimEligibilityDecision, float, bool]] = []
        exclusion_counts: dict[str, int] = {}
        revalidation_rows = 0
        for item in results:
            check()
            projection = item["projection"]
            initial = self._eligibility_evaluator.evaluate(projection, frame)
            if not initial["eligible"]:
                reason = initial["reason"].value
                exclusion_counts[reason] = exclusion_counts.get(reason, 0) + 1
                continue
            if graph_rows >= budget["max_graph_rows"]:
                break
            decision = self._eligibility_evaluator.revalidate(projection, frame, self._engram.current_claim_projection)
            graph_rows += 1
            if decision["revalidated"]:
                revalidation_rows += 1
            if not decision["eligible"]:
                reason = decision["reason"].value
                exclusion_counts[reason] = exclusion_counts.get(reason, 0) + 1
                continue
            type_match, type_match_available = object_type_match(plan["expected_object_type"], item["object_type"])
            retained.append((item, decision, type_match, type_match_available))

        selection = select_relation_claims(
            tuple(item for item, _decision, _type_match, _type_match_available in retained),
            frame["temporal_query"],
        )
        records = []
        ambiguous_result = not selection["direct_answer"]
        for item, decision, type_match, type_match_available in retained:
            base = claim_evidence_record(item["projection"], decision, frame, self.name)
            values = dict(base["features"]["values"])
            values.update({"entity_match": subject["score"], "relation_match": predicate["score"]})
            unavailable = set(base["features"]["unavailable"])
            reasons = set(base["selection_reasons"])
            reasons.update({"entity_resolved", "predicate_resolved", "relation_plan_match"})
            reasons.add(selection["reason"].value)
            if item["projection"]["claim_id"] in selection["conflict_claim_ids"]:
                reasons.add("relation_conflicting_claim")
            if not type_match_available:
                unavailable.add("object_type_match")
                reasons.add("object_type_unavailable")
            else:
                values["object_type_match"] = type_match
                reasons.add("object_type_match" if type_match else "object_type_mismatch")
            reasons.add("relation_result_ambiguous" if ambiguous_result else "relation_result_unique")
            records.append(
                claim_evidence_record_with_changes(
                    base,
                    {
                        "features": feature_set(values, tuple(sorted(unavailable - set(values)))),
                        "selection_reasons": tuple(sorted(reasons)),
                    },
                )
            )
        records.sort(key=lambda record: record["claim_id"])
        candidates = []
        if selection["direct_answer"] and budget["max_candidates"]:
            item, _, type_match, type_match_available = next(
                value for value in retained if value[0]["projection"]["claim_id"] == selection["selected_claim_id"]
            )
            if not type_match_available or type_match != 0.0:
                reference = evidence_reference(
                    evidence_id=item["projection"]["claim_id"],
                    resolver=self.name,
                    kind=EvidenceKind.CLAIM,
                    scope=frame["scope"],
                    provenance={"relation_plan": True},
                    diagnostics={},
                )
                unavailable = () if type_match_available else ("object_type_match",)
                values = {"entity_match": subject["score"], "relation_match": predicate["score"]}
                if type_match_available:
                    values["object_type_match"] = type_match
                response = phrase_relation_result(
                    subject["primary_label"],
                    predicate["primary_label"],
                    item["object_label"],
                )
                candidates.append(
                    resolution_candidate(
                        candidate_id=_candidate_id(CandidateSource.UTILITY, item["projection"]["claim_id"], frame["diagnostic_id"]),
                        statement_id=item["projection"]["claim_id"],
                        response=response,
                        source=CandidateSource.UTILITY,
                        features=feature_set(values, unavailable),
                        evidence=(reference,),
                        scope=frame["scope"],
                        lifecycle=LifecycleState.ACTIVE,
                        provenance={
                            "producer": "relation_one_hop_v1",
                            "subject_entity_id": plan["subject_entity_id"],
                            "subject_label": subject["primary_label"],
                            "predicate_id": plan["predicate_id"],
                            "predicate_label": predicate["primary_label"],
                            "object_entity_id": item["projection"]["object_entity_id"],
                            "object_label": item["object_label"],
                            "object_type": item["object_type"].value,
                            "predicate_cardinality": selection["cardinality"].value,
                            "selection_reason": selection["reason"].value,
                            "supplied_trust": item["projection"]["supplied_trust"],
                            "supplied_trust_version": item["projection"]["supplied_trust_version"],
                        },
                        diagnostics={
                            "template_id": plan["template_id"].value,
                            "ranking_claim_ids": selection["ranking_claim_ids"],
                            "trust_version": selection["trust_version"],
                            "trust_version_available": selection["trust_version_available"],
                        },
                    )
                )
        exhausted = set()

        def record_bytes() -> int:
            return _json_size([claim_evidence_record_to_dict(record) for record in records]) if records else 0

        while records and record_bytes() > budget["max_evidence_bytes"]:
            records.pop()
            exhausted.add("evidence_bytes")
        output_bytes = record_bytes() + sum(_json_size(candidate_to_dict(value)) for value in candidates)
        while candidates and output_bytes > budget["max_output_bytes"]:
            candidates.pop()
            exhausted.add("output_bytes")
            output_bytes = record_bytes()
        if candidates and not records:
            candidates.clear()
            output_bytes = 0
        working_memory = _working_size(results) + record_bytes() + output_bytes
        if working_memory > budget["max_working_memory_bytes"]:
            raise MemoryError("relation resolution working-memory estimate exceeded")
        result = resolver_result(
            resolver=self.name,
            state=ResolverState.COMPLETED,
            reason_code=(
                "relation_claim_candidate"
                if candidates
                else (
                    "relation_claim_conflict"
                    if selection["conflict_claim_ids"]
                    else "relation_claim_evidence" if records else "relation_graph_miss"
                )
            ),
            candidates=tuple(candidates),
            claim_evidence=tuple(records),
            diagnostics={
                "entity_status": subject["status"].value,
                "predicate_status": predicate["status"].value,
                "template_id": plan["template_id"].value,
                "discovery_rows": len(results),
                "revalidation_rows": revalidation_rows,
                "exclusion_counts": exclusion_counts,
                "ambiguous_result": ambiguous_result,
                "selection_reason": selection["reason"].value,
                "predicate_cardinality": selection["cardinality"].value,
                "conflict_claim_ids": selection["conflict_claim_ids"],
                "ranking_claim_ids": selection["ranking_claim_ids"],
                "trust_version": selection["trust_version"],
                "trust_version_available": selection["trust_version_available"],
            },
            consumption=budget_consumption(
                elapsed_ns=max(0, self._clock_ns() - started),
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

    def resolve(self, frame: QueryFrame, budget: ResolverBudget) -> ResolverResult:
        result = self.resolve_with_cancellation(frame, budget)
        return result

    def resolve_with_cancellation(
        self,
        frame: QueryFrame,
        budget: ResolverBudget,
        cooperative_check: object = (),
    ) -> ResolverResult:
        """Resolve while honoring a transient caller cancellation check."""
        _run_cooperative_check(cooperative_check)
        if (
            budget["max_graph_rows"] < 2
            or not budget["max_evidence"]
            or not budget["max_evidence_bytes"]
            or not budget["max_output_bytes"]
            or not budget["max_working_memory_bytes"]
        ):
            dimensions = tuple(
                name
                for name, value in (
                    ("evidence", budget["max_evidence"]),
                    ("evidence_bytes", budget["max_evidence_bytes"]),
                    ("graph_rows", budget["max_graph_rows"] if budget["max_graph_rows"] >= 2 else 0),
                    ("output_bytes", budget["max_output_bytes"]),
                    ("working_memory_bytes", budget["max_working_memory_bytes"]),
                )
                if not value
            )
            result = _exhausted_result(self.name, dimensions)
            return result
        started = self._clock_ns()
        try:
            composition_result = self._composition_result(frame, budget, started, cooperative_check)
            if composition_result:
                result = composition_result[0]
                return result
            relation_result = self._relation_result(frame, budget, started, cooperative_check)
            if relation_result:
                result = relation_result[0]
                return result
            projections = self._engram.structured_claim_projections(
                frame["resolved_text"],
                row_limit=min(budget["max_graph_rows"] // 2, budget["max_evidence"]),
                cooperative_check=cooperative_check,
                max_working_memory_bytes=budget["max_working_memory_bytes"],
            )
        except MemoryError:
            result = _memory_exhausted_result(self.name)
            return result
        records = []
        exclusion_counts: dict[str, int] = {}
        revalidation_rows = 0
        for projection in projections:
            _run_cooperative_check(cooperative_check)
            initial = self._eligibility_evaluator.evaluate(projection, frame)
            if not initial["eligible"]:
                reason = initial["reason"].value
                exclusion_counts[reason] = exclusion_counts.get(reason, 0) + 1
                continue
            decision = self._eligibility_evaluator.revalidate(projection, frame, self._engram.current_claim_projection)
            if decision["revalidated"]:
                revalidation_rows += 1
            if not decision["eligible"]:
                reason = decision["reason"].value
                exclusion_counts[reason] = exclusion_counts.get(reason, 0) + 1
                continue
            records.append(claim_evidence_record(projection, decision, frame, self.name))
        records.sort(key=lambda record: record["claim_id"])
        exhausted = set()

        def record_bytes() -> int:
            result = _json_size([claim_evidence_record_to_dict(record) for record in records]) if records else 0
            return result

        while records and record_bytes() > budget["max_evidence_bytes"]:
            records.pop()
            exhausted.add("evidence_bytes")
        while records and record_bytes() > budget["max_output_bytes"]:
            records.pop()
            exhausted.add("output_bytes")
        working_memory = _json_size([claim_projection_to_dict(projection) for projection in projections]) + record_bytes()
        if working_memory > budget["max_working_memory_bytes"]:
            result = _memory_exhausted_result(self.name)
            return result
        evidence_bytes = record_bytes()
        result = resolver_result(
            resolver=self.name,
            state=ResolverState.COMPLETED,
            reason_code="structured_claim_evidence" if records else "structured_graph_miss",
            claim_evidence=tuple(records),
            diagnostics={
                "discovery_rows": len(projections),
                "revalidation_rows": revalidation_rows,
                "exclusion_counts": exclusion_counts,
            },
            consumption=budget_consumption(
                elapsed_ns=max(0, self._clock_ns() - started),
                resolvers=1,
                graph_rows=len(projections) + revalidation_rows,
                evidence=len(records),
                evidence_bytes=evidence_bytes,
                output_bytes=evidence_bytes,
                working_memory_bytes=working_memory,
                exhausted_dimensions=tuple(sorted(exhausted)),
            ),
        )
        _run_cooperative_check(cooperative_check)
        return result


class SupportSemanticResolver:
    """Pure fixed-vector adapter for support candidates and full Claim evidence."""

    def __init__(self, engram, clock_ns: Callable[[], int], eligibility_evaluator: object = ()) -> None:
        self.name = SUPPORT_SEMANTIC_RESOLVER_NAME
        self.cost_class = SUPPORT_SEMANTIC_RESOLVER_COST_CLASS
        self._engram = engram
        self._clock_ns = clock_ns
        if eligibility_evaluator == ():
            selected_evaluator = ClaimEligibilityEvaluator(getattr(engram, "claim_visibility_authority", ()))
        elif isinstance(eligibility_evaluator, ClaimEligibilityEvaluator):
            selected_evaluator = eligibility_evaluator
        else:
            raise InvalidRequestError("support semantic eligibility_evaluator must be ClaimEligibilityEvaluator")
        self._eligibility_evaluator: ClaimEligibilityEvaluator = selected_evaluator

    def available(self, frame: QueryFrame) -> bool:
        graph = self._engram.config.get("graph") or {}
        result = bool(graph.get("enabled") and graph.get("vector_enabled"))
        return result

    def resolve(self, frame: QueryFrame, budget: ResolverBudget) -> ResolverResult:
        result = self.resolve_with_cancellation(frame, budget)
        return result

    def resolve_with_cancellation(
        self,
        frame: QueryFrame,
        budget: ResolverBudget,
        cooperative_check: object = (),
    ) -> ResolverResult:
        """Resolve semantic candidates while honoring caller cancellation."""
        _run_cooperative_check(cooperative_check)
        claim_capacity = bool(
            budget["max_graph_rows"] and budget["max_evidence"] and budget["max_evidence_bytes"] and budget["max_output_bytes"]
        )
        if (
            not budget["max_vector_results"]
            or not budget["max_working_memory_bytes"]
            or (not budget["max_candidates"] and not claim_capacity)
        ):
            dimensions = tuple(
                name
                for name, value in (
                    ("candidates", budget["max_candidates"]),
                    ("evidence", budget["max_evidence"]),
                    ("evidence_bytes", budget["max_evidence_bytes"]),
                    ("graph_rows", budget["max_graph_rows"]),
                    ("output_bytes", budget["max_output_bytes"]),
                    ("vector_results", budget["max_vector_results"]),
                    ("working_memory_bytes", budget["max_working_memory_bytes"]),
                )
                if not value
            )
            result = _exhausted_result(self.name, dimensions)
            return result
        started = self._clock_ns()

        def statement_filter(statement: Mapping[str, object]) -> bool:
            statement_id = str(statement.get("id", ""))
            try:
                artifact = self._engram.response_repository.get_artifact(statement_id)
            except ResourceNotFoundError:
                result = False
                return result
            result = _artifact_matches_frame(artifact, frame)
            return result

        try:
            candidate_limit = min(budget["max_candidates"], budget["max_vector_results"])
            _run_cooperative_check(cooperative_check)
            legacy_rows = (
                self._engram.graph_vector_claims(frame["resolved_text"], limit=candidate_limit)[:candidate_limit]
                if candidate_limit
                else []
            )
            _run_cooperative_check(cooperative_check)
            matches = (
                self._engram.vector_supported_claim_match_components(
                    legacy_rows,
                    limit=candidate_limit,
                    statement_filter=statement_filter,
                    cooperative_check=cooperative_check,
                    max_working_memory_bytes=budget["max_working_memory_bytes"],
                )
                if candidate_limit
                else []
            )
            remaining_vector_results = max(0, budget["max_vector_results"] - len(legacy_rows))
            projections = (
                self._engram.graph_vector_claim_projections(
                    frame["resolved_text"],
                    limit=remaining_vector_results,
                    cooperative_check=cooperative_check,
                    max_working_memory_bytes=budget["max_working_memory_bytes"],
                )
                if remaining_vector_results
                else []
            )
        except MemoryError:
            result = _memory_exhausted_result(self.name)
            return result
        candidates = []
        accounting = []
        evidence_count = 0
        for match in matches:
            _run_cooperative_check(cooperative_check)
            statement = match["statement"]
            artifact = self._engram.response_repository.get_artifact(str(statement["id"]))
            references = tuple(
                evidence_reference(
                    evidence_id=claim_id,
                    resolver=self.name,
                    kind=EvidenceKind.SUPPORT,
                    scope=frame["scope"],
                    provenance={"support_linked": True},
                    diagnostics={"semantic_match": True},
                )
                for claim_id in artifact["support_claim_ids"][: max(0, budget["max_evidence"] - evidence_count)]
            )
            evidence_count += len(references)
            candidate = resolution_candidate(
                candidate_id=_candidate_id(CandidateSource.SUPPORT_SEMANTIC, artifact["statement_id"], frame["diagnostic_id"]),
                statement_id=artifact["statement_id"],
                response=artifact["response"],
                source=CandidateSource.SUPPORT_SEMANTIC,
                features=feature_set(
                    values={
                        "legacy_retrieval_score": float(match["retrieval_score"]),
                        "priority": float(match["priority"]),
                        "semantic_score": float(match["semantic_similarity"]),
                        "support_coverage": float(bool(references)),
                        "vector_weight": float(match["vector_weight"]),
                    },
                    unavailable=("lexical_score",),
                ),
                evidence=references,
                scope=artifact["scope"],
                lifecycle=artifact["lifecycle"],
                provenance={
                    "generation": artifact["generation"],
                    "source_label": artifact["provenance"]["source_label"],
                },
                diagnostics={"support_count": len(artifact["support_claim_ids"])},
            )
            candidates.append(candidate)
            accounting.append(accounting_observation(candidate["statement_id"]))
        records = []
        exclusion_counts: dict[str, int] = {}
        revalidation_attempts = 0
        revalidation_rows = 0
        exhausted = set()
        remaining_evidence = max(0, budget["max_evidence"] - evidence_count)
        if claim_capacity and remaining_evidence:
            for projection in projections:
                _run_cooperative_check(cooperative_check)
                initial = self._eligibility_evaluator.evaluate(projection, frame)
                if not initial["eligible"]:
                    reason = initial["reason"].value
                    exclusion_counts[reason] = exclusion_counts.get(reason, 0) + 1
                    continue
                if revalidation_attempts >= budget["max_graph_rows"]:
                    exhausted.add("graph_rows")
                    break
                revalidation_attempts += 1
                decision = self._eligibility_evaluator.revalidate(projection, frame, self._engram.current_claim_projection)
                if decision["revalidated"]:
                    revalidation_rows += 1
                if not decision["eligible"]:
                    reason = decision["reason"].value
                    exclusion_counts[reason] = exclusion_counts.get(reason, 0) + 1
                    continue
                records.append(claim_evidence_record(projection, decision, frame, self.name))
                if len(records) >= remaining_evidence:
                    break
        records.sort(key=lambda record: record["claim_id"])

        def evidence_values() -> list[dict[str, object]]:
            result = [evidence_reference_to_dict(reference) for candidate in candidates for reference in candidate["evidence"]] + [
                claim_evidence_record_to_dict(record) for record in records
            ]
            return result

        def evidence_bytes() -> int:
            values = evidence_values()
            result = _json_size(values) if values else 0
            return result

        def output_bytes() -> int:
            values = [candidate_to_dict(candidate) for candidate in candidates] + [
                claim_evidence_record_to_dict(record) for record in records
            ]
            result = _json_size(values) if values else 0
            return result

        while records and evidence_bytes() > budget["max_evidence_bytes"]:
            records.pop()
            exhausted.add("evidence_bytes")
        while records and output_bytes() > budget["max_output_bytes"]:
            records.pop()
            exhausted.add("output_bytes")
        working_memory = (
            _working_size(legacy_rows)
            + _working_size(projections)
            + _working_size(matches)
            + _working_size(candidates)
            + _working_size(records)
        )
        while records and working_memory > budget["max_working_memory_bytes"]:
            records.pop()
            exhausted.add("working_memory_bytes")
            working_memory = (
                _working_size(legacy_rows)
                + _working_size(projections)
                + _working_size(matches)
                + _working_size(candidates)
                + _working_size(records)
            )
        if working_memory > budget["max_working_memory_bytes"]:
            result = _memory_exhausted_result(self.name)
            return result
        selected_reason = "support_semantic_candidates" if candidates else "semantic_claim_evidence"
        if not candidates and not records:
            selected_reason = "support_semantic_miss"
        result = resolver_result(
            resolver=self.name,
            state=ResolverState.COMPLETED,
            reason_code=selected_reason,
            candidates=tuple(candidates),
            claim_evidence=tuple(records),
            accounting=tuple(accounting),
            diagnostics={
                "projection_rows": len(projections),
                "revalidation_attempts": revalidation_attempts,
                "revalidation_rows": revalidation_rows,
                "exclusion_counts": exclusion_counts,
            },
            consumption=budget_consumption(
                elapsed_ns=max(0, self._clock_ns() - started),
                resolvers=1,
                candidates=len(candidates),
                graph_rows=revalidation_attempts,
                vector_results=len(legacy_rows) + len(projections),
                evidence=evidence_count + len(records),
                evidence_bytes=evidence_bytes(),
                output_bytes=output_bytes(),
                working_memory_bytes=working_memory,
                exhausted_dimensions=tuple(sorted(exhausted)),
            ),
        )
        _run_cooperative_check(cooperative_check)
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
        self._resolvers = resolvers

    def plan(self, frame: QueryFrame, configured_names: tuple[str, ...] = ()) -> ResolutionPlan:
        frame = validate_query_frame(frame)
        if not isinstance(configured_names, tuple):
            raise InvalidRequestError("configured_names must be a tuple")
        if not all(isinstance(name, str) and name for name in configured_names):
            raise InvalidRequestError("configured_names must contain non-empty strings")
        if len(set(configured_names)) != len(configured_names):
            raise InvalidRequestError("configured_names must not contain duplicates")
        resolver_names = {resolver_contract(resolver)[0] for resolver in self._resolvers}
        configured = set(configured_names) if configured_names else resolver_names
        unknown = configured.difference(resolver_names)
        if unknown:
            raise InvalidRequestError(f"unknown configured resolvers: {sorted(unknown)}")
        entries = []
        for order, resolver in enumerate(self._resolvers):
            name, cost_class, available_operation, _ = resolver_contract(resolver)
            selected = name in configured
            cost_allowed = cost_class in frame["budget"]["allowed_cost_classes"]
            availability_failed = False
            try:
                available = bool(selected and cost_allowed and available_operation(frame))
            except Exception:
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
        result = resolution_plan(tuple(entries))
        return result


ExecutionReport = TypedDict(
    "ExecutionReport",
    {
        "results": tuple[ResolverResult, ...],
        "consumption": BudgetConsumption,
        "exact_short_circuited": bool,
        "reservations": tuple[ResolverReservation, ...],
    },
)


def execution_report(
    results: object,
    consumption: object,
    exact_short_circuited: object,
    reservations: object,
) -> ExecutionReport:
    """Build the raw executor output consumed by orchestration."""
    if not isinstance(results, tuple):
        raise InvalidRequestError("execution results must be a tuple of ResolverResult values")
    if not isinstance(exact_short_circuited, bool):
        raise InvalidRequestError("execution exact_short_circuited must be a boolean")
    if not isinstance(reservations, tuple):
        raise InvalidRequestError("execution reservations must be a tuple")
    try:
        validated_results = tuple(validate_resolver_result(value) for value in results)
        validated_consumption = validate_budget_consumption(consumption)
        validated_reservations = tuple(validate_resolver_reservation(value) for value in reservations)
    except InvalidRequestError as error:
        raise InvalidRequestError("execution budget records are malformed") from error
    result: ExecutionReport = {
        "results": validated_results,
        "consumption": validated_consumption,
        "exact_short_circuited": exact_short_circuited,
        "reservations": validated_reservations,
    }
    return result


def validate_execution_report(value: object) -> ExecutionReport:
    if not isinstance(value, Mapping) or frozenset(value) != EXECUTION_REPORT_FIELDS:
        raise InvalidRequestError("ExecutionReport has invalid fields")
    result = execution_report(
        value["results"],
        value["consumption"],
        value["exact_short_circuited"],
        value["reservations"],
    )
    return result


def execution_report_with_changes(value: object, changes: object) -> ExecutionReport:
    current = validate_execution_report(value)
    if not isinstance(changes, Mapping) or not frozenset(changes).issubset(EXECUTION_REPORT_FIELDS):
        raise InvalidRequestError("execution report changes contain invalid fields")
    updated: dict[str, object] = dict(current)
    updated.update(changes)
    result = validate_execution_report(updated)
    return result


def execution_report_canonical_claim_evidence(value: object, cooperative_check=()) -> tuple:
    """Return deterministic Claim-only evidence without creating candidacy or accounting."""
    current = validate_execution_report(value)
    if cooperative_check != () and not callable(cooperative_check):
        raise InvalidRequestError("cooperative_check must be callable")
    records = tuple(
        record
        for resolver_result in current["results"]
        if resolver_result["resolver"] in CLAIM_EVIDENCE_PRODUCERS
        for record in resolver_result["claim_evidence"]
    )
    result = canonicalize_claim_evidence(records, cooperative_check) if cooperative_check else canonicalize_claim_evidence(records)
    return result


def bound_resolver_result(result: ResolverResult, lease: ResolverBudget) -> ResolverResult:
    candidates = list(result["candidates"][: lease["max_candidates"]])
    evidence = list(result["evidence"])
    claim_evidence = list(result["claim_evidence"])
    exhausted = set(result["consumption"]["exhausted_dimensions"])
    if len(candidates) < len(result["candidates"]):
        exhausted.add("candidates")
    remaining_evidence = lease["max_evidence"]
    for index, candidate in enumerate(candidates):
        retained = candidate["evidence"][:remaining_evidence]
        if len(retained) < len(candidate["evidence"]):
            exhausted.add("evidence")
        if retained != candidate["evidence"]:
            candidates[index] = candidate_with_changes(candidate, {"evidence": retained})
        remaining_evidence -= len(retained)
    retained_evidence = evidence[:remaining_evidence]
    if len(retained_evidence) < len(evidence):
        exhausted.add("evidence")
    evidence = retained_evidence
    remaining_evidence -= len(evidence)
    retained_claim_evidence = claim_evidence[:remaining_evidence]
    if len(retained_claim_evidence) < len(claim_evidence):
        exhausted.add("evidence")
    claim_evidence = retained_claim_evidence

    def evidence_values() -> list[dict[str, object]]:
        result = (
            [evidence_reference_to_dict(reference) for candidate in candidates for reference in candidate["evidence"]]
            + [evidence_reference_to_dict(reference) for reference in evidence]
            + [claim_evidence_record_to_dict(record) for record in claim_evidence]
        )
        return result

    def values_size(values: list[dict[str, object]]) -> int:
        result = _json_size(values) if values else 0
        return result

    while values_size(evidence_values()) > lease["max_evidence_bytes"]:
        if claim_evidence:
            claim_evidence.pop()
        elif evidence:
            evidence.pop()
        else:
            candidate_index = next(
                (index for index in range(len(candidates) - 1, -1, -1) if candidates[index]["evidence"]),
                -1,
            )
            if candidate_index < 0:
                break
            candidate = candidates[candidate_index]
            candidates[candidate_index] = candidate_with_changes(candidate, {"evidence": candidate["evidence"][:-1]})
        exhausted.add("evidence_bytes")

    def output_size() -> int:
        values = (
            [candidate_to_dict(candidate) for candidate in candidates]
            + [evidence_reference_to_dict(reference) for reference in evidence]
            + [claim_evidence_record_to_dict(record) for record in claim_evidence]
        )
        result = values_size(values)
        return result

    while output_size() > lease["max_output_bytes"] and (candidates or evidence or claim_evidence):
        if claim_evidence:
            claim_evidence.pop()
        elif evidence:
            evidence.pop()
        else:
            candidates.pop()
        exhausted.add("output_bytes")
    diagnostics = result["diagnostics"]
    if diagnostics and _json_size(dict(diagnostics)) > lease["max_diagnostic_bytes"]:
        marker = MappingProxyType({"truncated": True})
        diagnostics = marker if _json_size(dict(marker)) <= lease["max_diagnostic_bytes"] else MappingProxyType({})
        exhausted.add("diagnostic_bytes")
    accounting_ids = {candidate["statement_id"] for candidate in candidates}
    accounting = tuple(value for value in result["accounting"] if value["statement_id"] in accounting_ids)
    candidate_bytes = values_size([candidate_to_dict(value) for value in candidates])
    evidence_bytes = values_size(evidence_values())
    diagnostic_bytes = _json_size(dict(diagnostics)) if diagnostics else 0
    graph_rows = min(result["consumption"]["graph_rows"], lease["max_graph_rows"])
    vector_results = min(result["consumption"]["vector_results"], lease["max_vector_results"])
    if graph_rows < result["consumption"]["graph_rows"]:
        exhausted.add("graph_rows")
    if vector_results < result["consumption"]["vector_results"]:
        exhausted.add("vector_results")
    estimated_memory = max(
        candidate_bytes
        + values_size([evidence_reference_to_dict(value) for value in evidence])
        + values_size([claim_evidence_record_to_dict(value) for value in claim_evidence])
        + diagnostic_bytes,
        result["consumption"]["working_memory_bytes"],
    )
    if estimated_memory > lease["max_working_memory_bytes"]:
        exhausted.add("working_memory_bytes")
        candidates = []
        evidence = []
        claim_evidence = []
        accounting = ()
        diagnostics = MappingProxyType({})
        candidate_bytes = 0
        evidence_bytes = 0
        diagnostic_bytes = 0
    working_memory = min(estimated_memory, lease["max_working_memory_bytes"])
    consumption = budget_consumption(
        elapsed_ns=result["consumption"]["elapsed_ns"],
        resolvers=1,
        candidates=len(candidates),
        graph_rows=graph_rows,
        vector_results=vector_results,
        evidence=len(evidence) + len(claim_evidence) + sum(len(candidate["evidence"]) for candidate in candidates),
        evidence_bytes=evidence_bytes,
        output_bytes=output_size(),
        diagnostic_bytes=diagnostic_bytes,
        working_memory_bytes=working_memory,
        exhausted_dimensions=tuple(sorted(exhausted)),
        measurement_available=result["consumption"]["measurement_available"],
    )
    result = resolver_result(
        resolver=result["resolver"],
        state=result["state"],
        reason_code=result["reason_code"],
        candidates=tuple(candidates),
        evidence=tuple(evidence),
        claim_evidence=tuple(claim_evidence),
        accounting=accounting,
        diagnostics=diagnostics,
        consumption=consumption,
        schema_version=result["schema_version"],
    )
    return result


class ResolverExecutor:
    """Sequential bounded fail-soft executor for one deterministic plan."""

    def __init__(self, clock_ns: Callable[[], int]) -> None:
        if not callable(clock_ns):
            raise InvalidRequestError("executor clock_ns must be callable")
        self._clock_ns = clock_ns

    @property
    def clock_ns(self) -> Callable[[], int]:
        """Expose the injected request clock to post-execution policy stages."""
        result = self._clock_ns
        return result

    def _lease(self, frame: QueryFrame, ledger: BudgetLedger) -> ResolverBudget:
        consumed = ledger.snapshot()
        result = resolver_budget(
            max_candidates=ledger.remaining_candidates(),
            max_graph_rows=max(0, frame["budget"]["max_graph_rows"] - consumed["graph_rows"]),
            max_vector_results=max(0, frame["budget"]["max_vector_results"] - consumed["vector_results"]),
            max_evidence=ledger.remaining_evidence(),
            max_evidence_bytes=max(0, frame["budget"]["max_evidence_bytes"] - consumed["evidence_bytes"]),
            max_output_bytes=max(0, frame["budget"]["max_output_bytes"] - consumed["output_bytes"]),
            max_diagnostic_bytes=max(0, frame["budget"]["max_diagnostic_bytes"] - consumed["diagnostic_bytes"]),
            max_working_memory_bytes=max(0, frame["budget"]["max_working_memory_bytes"] - consumed["working_memory_bytes"]),
        )
        return result

    def execute(self, frame: QueryFrame, plan: ResolutionPlan, cooperative_check: object = ()) -> ExecutionReport:
        frame = validate_query_frame(frame)
        current_plan = validate_resolution_plan(plan)
        _run_cooperative_check(cooperative_check)
        ledger = BudgetLedger(frame["budget"])
        results = []
        reservations = []
        exact_short_circuited = False
        for entry in current_plan["entries"]:
            _run_cooperative_check(cooperative_check)
            resolver_name, _, _, resolve_operation = resolver_contract(entry["resolver"])
            if ledger.snapshot()["resolvers"] >= frame["budget"]["max_resolvers"]:
                exhausted = resolver_result(
                    resolver=resolver_name,
                    state=ResolverState.EXHAUSTED,
                    reason_code="resolver_budget",
                    consumption=budget_consumption(exhausted_dimensions=("resolvers",)),
                )
                results.append(exhausted)
                ledger.add(exhausted["consumption"])
                break
            if not entry["configured"]:
                results.append(
                    resolver_result(
                        resolver=resolver_name,
                        state=ResolverState.SKIPPED,
                        reason_code=entry["reason_code"],
                    )
                )
                continue
            if not entry["available"]:
                results.append(
                    resolver_result(
                        resolver=resolver_name,
                        state=ResolverState.UNAVAILABLE,
                        reason_code=entry["reason_code"],
                    )
                )
                continue
            lease = self._lease(frame, ledger)
            if not lease["max_candidates"] and resolver_name not in {"structured_graph", "support_semantic"}:
                results.append(
                    resolver_result(
                        resolver=resolver_name,
                        state=ResolverState.EXHAUSTED,
                        reason_code="candidate_budget",
                    )
                )
                continue
            started = self._clock_ns()
            try:
                cancellation_operation = getattr(entry["resolver"], "resolve_with_cancellation", ())
                if callable(cancellation_operation):
                    raw = cancellation_operation(frame, lease, cooperative_check)
                else:
                    raw = resolve_operation(frame, lease)
                _run_cooperative_check(cooperative_check)
            except ResolutionCancelledError:
                raise
            except Exception as error:
                finished = self._clock_ns()
                elapsed = max(0, finished - started)
                result = resolver_result(
                    resolver=resolver_name,
                    state=ResolverState.FAILED,
                    reason_code="resolver_exception",
                    diagnostics={"exception_type": type(error).__name__},
                    consumption=budget_consumption(elapsed_ns=elapsed, resolvers=1),
                )
            else:
                finished = self._clock_ns()
                elapsed = max(0, finished - started)
                current_raw = validate_resolver_result(raw)
                raw = resolver_result_with_changes(
                    current_raw,
                    {"consumption": budget_consumption_with_changes(current_raw["consumption"], {"elapsed_ns": elapsed})},
                )
                result = bound_resolver_result(raw, lease)
            results.append(result)
            ledger.add(result["consumption"])
            reservations.append(resolver_reservation(resolver_name, entry["order"], lease, result["consumption"]))
            if (
                resolver_name == "exact"
                and result["state"] == ResolverState.COMPLETED
                and len(result["candidates"]) == 1
                and result["candidates"][0]["source"] == CandidateSource.EXACT
                and not frame["rewrite_chain"]
            ):
                exact_short_circuited = True
                break
        report = execution_report(tuple(results), ledger.snapshot(), exact_short_circuited, tuple(reservations))
        return report


AccountingFinalization = TypedDict(
    "AccountingFinalization",
    {
        "candidate_statement_ids": tuple[str, ...],
        "accepted_statement_id": str,
        "candidacy_applied": bool,
        "success_applied": bool,
        "idempotent": bool,
    },
)


def accounting_finalization(
    candidate_statement_ids: object,
    accepted_statement_id: object,
    candidacy_applied: object,
    success_applied: object,
    idempotent: object,
) -> AccountingFinalization:
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
    result: AccountingFinalization = {
        "candidate_statement_ids": candidate_statement_ids,
        "accepted_statement_id": accepted_statement_id,
        "candidacy_applied": candidacy_applied,
        "success_applied": success_applied,
        "idempotent": idempotent,
    }
    return result


def validate_accounting_finalization(value: object) -> AccountingFinalization:
    if not isinstance(value, Mapping) or frozenset(value) != ACCOUNTING_FINALIZATION_FIELDS:
        raise InvalidRequestError("AccountingFinalization has invalid fields")
    result = accounting_finalization(
        value["candidate_statement_ids"],
        value["accepted_statement_id"],
        value["candidacy_applied"],
        value["success_applied"],
        value["idempotent"],
    )
    return result


def accounting_finalization_to_dict(value: object) -> dict[str, object]:
    current = validate_accounting_finalization(value)
    visible_ids = current["candidate_statement_ids"][:MAX_ACCOUNTING_VISIBLE_STATEMENT_IDS]
    result = {
        "candidate_statement_ids": list(visible_ids),
        "omitted_candidate_statement_id_count": len(current["candidate_statement_ids"]) - len(visible_ids),
        "accepted_statement_id": current["accepted_statement_id"],
        "candidacy_applied": current["candidacy_applied"],
        "success_applied": current["success_applied"],
        "idempotent": current["idempotent"],
    }
    return result


def accounting_signature(results: tuple[ResolverResult, ...], accepted_statement_id: str) -> str:
    """Return the retry identity for one stable accounting observation set."""
    stable_results = []
    for result in results:
        value = resolver_result_to_dict(result)
        consumption = dict(cast(Mapping[str, object], value["consumption"]))
        consumption["elapsed_ns"] = 0
        value["consumption"] = consumption
        stable_results.append(value)
    payload = {
        "accepted_statement_id": accepted_statement_id,
        "results": stable_results,
    }
    result = hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()
    return result


def accounting_receipt_id(kind: str, request_id: str) -> str:
    """Return a bounded non-disclosing receipt identity."""
    digest = hashlib.sha256(request_id.encode("utf-8")).hexdigest()
    result = f"resolution:{kind}:sha256:{digest}"
    return result


class ResolutionAccountingFinalizer:
    """Exactly-once boundary for aggregate candidacy and accepted success."""

    def __init__(self, engram, accepted_response_service=(), compatibility_sync=(), max_requests: int = 1_000) -> None:
        self._engram = engram
        self._accepted_response_service = accepted_response_service
        if compatibility_sync and not callable(compatibility_sync):
            raise InvalidRequestError("compatibility_sync must be callable")
        if isinstance(max_requests, bool) or not isinstance(max_requests, int) or max_requests < 1:
            raise InvalidRequestError("accounting max_requests must be a positive integer")
        self._compatibility_sync = compatibility_sync
        self._max_requests = max_requests
        self._lock = threading.RLock()
        self._requests: dict[str, tuple[str, AccountingFinalization]] = {}

    @property
    def candidate_authority(self) -> EngramCandidateAuthority:
        """Return current-state authority bound to the same Engram as accounting."""
        result = EngramCandidateAuthority(self._engram)
        return result

    def discard(self, request_id: str) -> None:
        """Forget one transient retry record after the owning result cache evicts it."""
        if not isinstance(request_id, str) or not request_id:
            raise InvalidRequestError("accounting request_id must be a non-empty string")
        with self._lock:
            if request_id in self._requests:
                del self._requests[request_id]

    def finalize(
        self,
        request_id: str,
        results: tuple[ResolverResult, ...],
        accepted_statement_id: str = "",
    ) -> AccountingFinalization:
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
        with self._lock:
            if request_id in self._requests:
                previous_signature, previous = self._requests[request_id]
                if previous_signature != signature:
                    raise ConflictError("accounting request_id is associated with different observations")
                result = accounting_finalization(
                    previous["candidate_statement_ids"],
                    previous["accepted_statement_id"],
                    previous["candidacy_applied"],
                    previous["success_applied"],
                    True,
                )
                return result
            observations = {}
            keywords = set()
            for result in results:
                for observation in result["accounting"]:
                    observations.setdefault(observation["statement_id"], observation)
                    keywords.update(observation["keywords"])
            candidate_ids = tuple(sorted(observations))
            repository_ids = set(self._engram.response_repository.snapshot()["artifacts"])
            artifact_ids = tuple(statement_id for statement_id in candidate_ids if statement_id in repository_ids)
            legacy_ids = tuple(statement_id for statement_id in candidate_ids if statement_id not in repository_ids)
            artifact_query_replayed = False
            if accepted_statement_id:
                if accepted_statement_id not in observations:
                    raise InvalidRequestError("accepted_statement_id was not an observed candidate")
                if accepted_statement_id not in repository_ids and accepted_statement_id not in self._engram.statement_index:
                    raise InvalidRequestError("accepted statement no longer exists")
            if artifact_ids and not self._accepted_response_service:
                raise InvalidRequestError("accepted-response accounting service is required for artifact candidates")
            accepted_artifact_id = accepted_statement_id if accepted_statement_id in repository_ids else ""
            if artifact_ids:
                accounting_mutation = self._accepted_response_service.finalize_resolution_accounting(
                    artifact_ids,
                    accepted_artifact_id,
                    accounting_receipt_id("finalize", request_id),
                )
                artifact_query_replayed = accounting_mutation["replayed"]
                if not accounting_mutation["replayed"] and self._compatibility_sync:
                    self._compatibility_sync(tuple(sorted(repository_ids)))
            with self._engram.statement_lock:
                for statement_id in legacy_ids:
                    if statement_id in self._engram.statement_index:
                        record_statement_query(self._engram.statements[self._engram.statement_index[statement_id]])
            if legacy_ids or not artifact_query_replayed:
                with self._engram.count_lock:
                    self._engram.query_count += 1
            if legacy_ids or not artifact_query_replayed:
                with self._engram.keyword_lock:
                    for keyword in keywords:
                        if keyword in self._engram.keywords:
                            self._engram.keywords[keyword]["query_count"] += 1
            success_applied = False
            if accepted_statement_id:
                if accepted_statement_id not in repository_ids:
                    with self._engram.statement_lock:
                        record_statement_hit(self._engram.statements[self._engram.statement_index[accepted_statement_id]])
                    with self._engram.count_lock:
                        self._engram.hit_count += 1
                    with self._engram.keyword_lock:
                        for keyword in observations[accepted_statement_id]["keywords"]:
                            if keyword in self._engram.keywords:
                                self._engram.keywords[keyword]["hit_count"] += 1
                elif not artifact_query_replayed:
                    with self._engram.count_lock:
                        self._engram.hit_count += 1
                    with self._engram.keyword_lock:
                        for keyword in observations[accepted_statement_id]["keywords"]:
                            if keyword in self._engram.keywords:
                                self._engram.keywords[keyword]["hit_count"] += 1
                success_applied = True
            finalization = accounting_finalization(
                candidate_ids,
                accepted_statement_id,
                True,
                success_applied,
                bool(artifact_ids and artifact_query_replayed and not legacy_ids),
            )
            self._requests[request_id] = (signature, finalization)
            while len(self._requests) > self._max_requests:
                self._requests.pop(next(iter(self._requests)))
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
        self._registry = registry
        self._executor = executor
        self._accounting = accounting
        self._clock_ns = executor.clock_ns
        self._fusion = (
            fusion if isinstance(fusion, CandidateFusionEngine) else CandidateFusionEngine(authority=accounting.candidate_authority)
        )
        self._evidence_policy = selected_evidence_policy

    def resolve(
        self,
        frame: QueryFrame,
        request_id: str,
        configured_names: tuple[str, ...] = (),
        accept_exact: bool = False,
        cooperative_check: object = (),
    ) -> tuple[ResolutionResult, AccountingFinalization]:
        if not isinstance(accept_exact, bool):
            raise InvalidRequestError("accept_exact must be a boolean")
        plan = self._registry.plan(frame, configured_names)
        execution = self._executor.execute(frame, plan, cooperative_check)
        _run_cooperative_check(cooperative_check)
        candidates = []
        evidence = []
        for result in execution["results"]:
            candidates.extend(result["candidates"])
            evidence.extend(result["evidence"])
        fusion_memory_limit = max(
            0,
            frame["budget"]["max_working_memory_bytes"] - execution["consumption"]["working_memory_bytes"],
        )
        decision = self._fusion.decide(
            frame,
            tuple(candidates),
            tuple(evidence),
            working_memory_limit=fusion_memory_limit,
            working_memory_limit_available=True,
        )
        _run_cooperative_check(cooperative_check)
        outcome = decision["outcome"]
        selected = decision["selected_candidate"]
        selected_available = decision["selected_candidate_available"]
        response_candidates = decision["response_candidates"]
        reason_codes = [*decision["reason_codes"], "accounting_finalized"]
        confidence = decision["confidence"]
        confidence_available = decision["confidence_available"]
        evidence_package_available = False
        evidence_package = empty_evidence_package()
        package_source_records = ()
        package_byte_limit = 256
        minimal_evidence_values = tuple(
            reference
            for result in execution["results"]
            for reference in result["evidence"]
            + tuple(nested for candidate in result["candidates"] for nested in candidate["evidence"])
        )
        minimal_evidence_count = len(minimal_evidence_values)
        minimal_evidence_bytes = (
            _json_size([evidence_reference_to_dict(reference) for reference in minimal_evidence_values])
            if minimal_evidence_values
            else 0
        )
        evidence_working_memory = 0
        orchestration_exhausted = set()
        evidence_diagnostics: dict[str, object] = {
            "policy_version": self._evidence_policy["policy_version"],
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
            _run_cooperative_check(cooperative_check)

        unexpected_claim_records = any(
            result["claim_evidence"] and result["resolver"] not in CLAIM_EVIDENCE_PRODUCERS for result in execution["results"]
        )
        if unexpected_claim_records:
            append_reason("claim_evidence_untrusted_producer")
        raw_claim_records = tuple(
            record
            for result in execution["results"]
            if result["resolver"] in CLAIM_EVIDENCE_PRODUCERS
            for record in result["claim_evidence"]
        )
        full_producer_available = any(
            result["resolver"] in CLAIM_EVIDENCE_PRODUCERS
            and result["state"] == ResolverState.COMPLETED
            and result["claim_evidence"]
            for result in execution["results"]
        )
        if outcome != ResolutionOutcome.ANSWER and full_producer_available:
            evidence_diagnostics["input_count"] = len(raw_claim_records)
            remaining_working_memory = max(
                0,
                frame["budget"]["max_working_memory_bytes"]
                - execution["consumption"]["working_memory_bytes"]
                - decision["working_memory_bytes"],
            )
            if _working_size(raw_claim_records) * 2 > remaining_working_memory:
                orchestration_exhausted.add("working_memory_bytes")
                append_reason("claim_evidence_memory_exhausted")
            else:
                try:
                    normalized_records = canonicalize_claim_evidence(raw_claim_records, evidence_check)
                    if any(
                        record["disclosure"]["scope"] != frame["scope"]
                        or record["validity"]["evaluation_time"] != frame["eligibility_context"]["evaluation_time"]
                        for record in normalized_records
                    ):
                        raise InvalidRequestError("Claim evidence is not bound to the current frame")
                    usefulness_decisions = []
                    included_records = []
                    reason_counts: dict[str, int] = {}
                    for record in normalized_records:
                        evidence_check()
                        usefulness = evaluate_evidence_usefulness(self._evidence_policy, record)
                        usefulness_decisions.append(usefulness)
                        for reason in usefulness["reasons"]:
                            reason_counts[reason.value] = reason_counts.get(reason.value, 0) + 1
                        if usefulness["included"]:
                            included_records.append(record)
                    evidence_check()
                except InvalidRequestError:
                    append_reason("claim_evidence_conflict")
                else:
                    package_source_records = tuple(included_records)
                    evidence_working_memory = (
                        _working_size(normalized_records) + _working_size(usefulness_decisions) + _working_size(included_records)
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
                        frame["budget"]["max_evidence_bytes"] - minimal_evidence_bytes,
                    )
                    package_byte_limit = min(65_536, max(256, remaining_evidence_bytes))
                    candidate_package = build_evidence_package(
                        package_source_records,
                        max_records=min(10, max(0, frame["budget"]["max_evidence"] - minimal_evidence_count)),
                        max_bytes=package_byte_limit,
                    )
                    candidate_package_bytes = len(_trusted_evidence_package_to_json(candidate_package).encode("utf-8"))
                    if candidate_package_bytes <= remaining_evidence_bytes:
                        evidence_package_available = True
                        evidence_package = candidate_package
                        evidence_working_memory += candidate_package_bytes
                        evidence_diagnostics["available"] = True
                    else:
                        orchestration_exhausted.add("evidence_bytes")
                        append_reason("claim_evidence_bytes_exhausted")
                    if evidence_working_memory > remaining_working_memory:
                        evidence_package_available = False
                        evidence_package = empty_evidence_package()
                        orchestration_exhausted.add("working_memory_bytes")
                        append_reason("claim_evidence_memory_exhausted")
                        evidence_diagnostics["available"] = False
                    elif evidence_package["records"]:
                        append_reason("claim_evidence_included")
                        if outcome == ResolutionOutcome.MISS:
                            outcome = ResolutionOutcome.EVIDENCE
                    elif normalized_records:
                        append_reason("claim_evidence_excluded")
        accepted_statement_id = (
            selected["statement_id"]
            if outcome == ResolutionOutcome.ANSWER and selected["source"] == CandidateSource.EXACT and accept_exact
            else ""
        )
        preview_ids = tuple(
            sorted({observation["statement_id"] for result in execution["results"] for observation in result["accounting"]})
        )
        accounting_preview = accounting_finalization(
            preview_ids,
            accepted_statement_id,
            False,
            False,
            False,
        )
        evidence_diagnostics.update(
            {
                "available": evidence_package_available,
                "retained_count": evidence_package["retained_count"],
                "omitted_count": evidence_package["omitted_count"],
                "truncated": evidence_package["truncated"],
            }
        )

        def bound_frame_diagnostics(values: dict[str, object]) -> dict[str, object]:
            remaining = max(
                0,
                frame["budget"]["max_diagnostic_bytes"] - execution["consumption"]["diagnostic_bytes"],
            )
            if not values or _json_size(values) <= remaining:
                return values
            orchestration_exhausted.add("diagnostic_bytes")
            append_reason("diagnostics_truncated")
            compact = {
                "diagnostic_id": frame["diagnostic_id"],
                "claim_evidence": {
                    "policy_version": self._evidence_policy["policy_version"],
                    "available": evidence_package_available,
                    "input_count": evidence_diagnostics["input_count"],
                    "included_count": evidence_diagnostics["included_count"],
                    "retained_count": evidence_package["retained_count"],
                    "omitted_count": evidence_package["omitted_count"],
                },
                "truncated": True,
            }
            if _json_size(compact) <= remaining:
                return compact
            marker: dict[str, object] = {"truncated": True}
            result = marker if _json_size(marker) <= remaining else {}
            return result

        frame_diagnostics = bound_frame_diagnostics(
            {
                "diagnostic_id": frame["diagnostic_id"],
                "plan": resolution_plan_to_dict(plan),
                "reservations": [resolver_reservation_to_dict(reservation) for reservation in execution["reservations"]],
                "fusion": decision["report"],
                "claim_evidence": evidence_diagnostics,
                "accounting": accounting_finalization_to_dict(accounting_preview),
            }
        )
        resolver_results = tuple(
            _trusted_resolver_result_with_changes(result, {"claim_evidence": ()}) if result["claim_evidence"] else result
            for result in execution["results"]
        )
        response_evidence = decision["evidence"]

        def make_result(consumption: BudgetConsumption) -> ResolutionResult:
            value = _trusted_resolution_result(
                outcome=outcome,
                selected_candidate=selected,
                selected_candidate_available=selected_available,
                response_candidates=response_candidates,
                evidence=response_evidence,
                confidence=confidence,
                confidence_available=confidence_available,
                reason_codes=tuple(reason_codes),
                frame_diagnostics=frame_diagnostics,
                resolver_results=resolver_results,
                budget=consumption,
                evidence_package_available=evidence_package_available,
                evidence_package=evidence_package,
            )
            return value

        result = make_result(execution["consumption"])
        if len(_trusted_resolution_result_to_json(result).encode("utf-8")) > frame["budget"]["max_output_bytes"]:
            append_reason("output_truncated")
            frame_diagnostics = bound_frame_diagnostics(
                {
                    "diagnostic_id": frame["diagnostic_id"],
                    "fusion": {
                        "policy_version": self._fusion.policy["policy_version"],
                        "candidate_count": decision["report"]["candidate_count"],
                        "output_truncated": True,
                    },
                    "claim_evidence": {
                        "policy_version": self._evidence_policy["policy_version"],
                        "available": evidence_package_available,
                        "input_count": evidence_diagnostics["input_count"],
                        "included_count": evidence_diagnostics["included_count"],
                        "retained_count": evidence_package["retained_count"],
                        "omitted_count": evidence_package["omitted_count"],
                        "output_truncated": True,
                    },
                    "accounting": {
                        "candidate_count": len(accounting_preview["candidate_statement_ids"]),
                        "accepted_present": False,
                        "candidacy_applied": accounting_preview["candidacy_applied"],
                        "success_applied": accounting_preview["success_applied"],
                        "idempotent": accounting_preview["idempotent"],
                    },
                    "output_truncated": True,
                }
            )
            while (
                resolver_results
                and len(_trusted_resolution_result_to_json(make_result(execution["consumption"])).encode("utf-8"))
                > frame["budget"]["max_output_bytes"]
            ):
                evidence_check()
                resolver_results = resolver_results[:-1]
            while (
                evidence_package["records"]
                and len(_trusted_resolution_result_to_json(make_result(execution["consumption"])).encode("utf-8"))
                > frame["budget"]["max_output_bytes"]
            ):
                evidence_check()
                evidence_package = build_evidence_package(
                    package_source_records,
                    max_records=len(evidence_package["records"]) - 1,
                    max_bytes=package_byte_limit,
                )
            while (
                response_evidence
                and len(_trusted_resolution_result_to_json(make_result(execution["consumption"])).encode("utf-8"))
                > frame["budget"]["max_output_bytes"]
            ):
                evidence_check()
                response_evidence = response_evidence[:-1]
            while (
                response_candidates
                and len(_trusted_resolution_result_to_json(make_result(execution["consumption"])).encode("utf-8"))
                > frame["budget"]["max_output_bytes"]
            ):
                evidence_check()
                response_candidates = response_candidates[:-1]
            if outcome == ResolutionOutcome.ANSWER and not response_candidates:
                outcome = ResolutionOutcome.MISS
                selected = empty_candidate()
                selected_available = False
                confidence = 0.0
                confidence_available = False
                accepted_statement_id = ""
                append_reason("answer_exceeds_output_budget")
            if outcome == ResolutionOutcome.EVIDENCE and not (
                response_candidates or response_evidence or evidence_package["records"]
            ):
                outcome = ResolutionOutcome.MISS
                append_reason("no_usable_output_after_truncation")

        evidence_diagnostics.update(
            {
                "available": evidence_package_available,
                "retained_count": evidence_package["retained_count"],
                "omitted_count": evidence_package["omitted_count"],
                "truncated": evidence_package["truncated"],
            }
        )
        visible_evidence_diagnostics = frame_diagnostics.get("claim_evidence", {})
        if isinstance(visible_evidence_diagnostics, dict):
            for name in ("available", "retained_count", "omitted_count"):
                if name in visible_evidence_diagnostics:
                    visible_evidence_diagnostics[name] = evidence_diagnostics[name]

        _run_cooperative_check(cooperative_check)
        finalization = self._accounting.finalize(request_id, execution["results"], accepted_statement_id)
        if frame_diagnostics:
            final_accounting_diagnostics: object
            if "output_truncated" in reason_codes:
                final_accounting_diagnostics = {
                    "candidate_count": len(finalization["candidate_statement_ids"]),
                    "accepted_present": bool(finalization["accepted_statement_id"]),
                    "candidacy_applied": finalization["candidacy_applied"],
                    "success_applied": finalization["success_applied"],
                    "idempotent": finalization["idempotent"],
                }
            else:
                final_accounting_diagnostics = accounting_finalization_to_dict(finalization)
            revised_diagnostics = dict(frame_diagnostics)
            revised_diagnostics["accounting"] = final_accounting_diagnostics
            frame_diagnostics = bound_frame_diagnostics(revised_diagnostics)

        exhausted = set(execution["consumption"]["exhausted_dimensions"]).union(orchestration_exhausted)
        if "output_truncated" in reason_codes:
            exhausted.add("output_bytes")
        fusion_exhaustion = decision["report"].get("budget_exhausted", "")
        if fusion_exhaustion == "fusion_memory_exhausted":
            exhausted.add("working_memory_bytes")
        package_evidence_bytes = (
            len(_trusted_evidence_package_to_json(evidence_package).encode("utf-8")) if evidence_package_available else 0
        )
        evidence_count = minimal_evidence_count + evidence_package["retained_count"]
        evidence_bytes = minimal_evidence_bytes + package_evidence_bytes
        if evidence_count > frame["budget"]["max_evidence"]:
            exhausted.add("evidence")
        if evidence_bytes > frame["budget"]["max_evidence_bytes"]:
            exhausted.add("evidence_bytes")
        frame_diagnostic_bytes = _json_size(frame_diagnostics) if frame_diagnostics else 0
        diagnostic_bytes = execution["consumption"]["diagnostic_bytes"] + frame_diagnostic_bytes
        if diagnostic_bytes > frame["budget"]["max_diagnostic_bytes"]:
            exhausted.add("diagnostic_bytes")
        working_memory_bytes = (
            execution["consumption"]["working_memory_bytes"] + decision["working_memory_bytes"] + evidence_working_memory
        )
        if working_memory_bytes > frame["budget"]["max_working_memory_bytes"]:
            exhausted.add("working_memory_bytes")
        elapsed_ns = execution["consumption"]["elapsed_ns"]
        if frame["budget"]["started_ns"]:
            current_ns = self._clock_ns()
            if isinstance(current_ns, bool) or not isinstance(current_ns, int) or current_ns < 0:
                raise InvalidRequestError("orchestrator clock_ns must return a nonnegative integer")
            elapsed_ns = max(elapsed_ns, max(0, current_ns - frame["budget"]["started_ns"]))
        consumption = budget_consumption_with_changes(
            execution["consumption"],
            {
                "elapsed_ns": elapsed_ns,
                "evidence": evidence_count,
                "evidence_bytes": min(evidence_bytes, frame["budget"]["max_evidence_bytes"]),
                "output_bytes": 0,
                "diagnostic_bytes": min(diagnostic_bytes, frame["budget"]["max_diagnostic_bytes"]),
                "working_memory_bytes": min(working_memory_bytes, frame["budget"]["max_working_memory_bytes"]),
                "exhausted_dimensions": tuple(sorted(exhausted)),
            },
        )
        for _ in range(8):
            result = make_result(consumption)
            encoded_size = len(_trusted_resolution_result_to_json(result).encode("utf-8"))
            fixed_exhausted = set(consumption["exhausted_dimensions"])
            if encoded_size > frame["budget"]["max_working_memory_bytes"]:
                fixed_exhausted.add("working_memory_bytes")
            updated_consumption = budget_consumption_with_changes(
                consumption,
                {
                    "output_bytes": encoded_size,
                    "working_memory_bytes": min(
                        frame["budget"]["max_working_memory_bytes"],
                        max(consumption["working_memory_bytes"], encoded_size),
                    ),
                    "exhausted_dimensions": tuple(sorted(fixed_exhausted)),
                },
            )
            if updated_consumption == consumption:
                break
            consumption = updated_consumption
        result = make_result(consumption)
        if len(_trusted_resolution_result_to_json(result).encode("utf-8")) > frame["budget"]["max_output_bytes"]:
            raise InvalidRequestError("minimum resolution result exceeds max_output_bytes")
        result = result, finalization
        return result
