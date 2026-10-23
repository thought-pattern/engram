"""Conservative composed graph requests compiled and executed through fixed one-hop Proposition reads."""

from hashlib import sha256 as hashlib_sha256

from engram.composition import (
    composition_plan_to_dict,
    execute_composition_plan,
    graph_composition_operator,
    linear_composition_plan,
    phrase_composition_result,
    resolve_composition_predicates,
)
from engram.constants_artifacts import LifecycleState
from engram.constants_evidence import PROPOSITION_PATH_FILTERS
from engram.constants_relation import (
    GRAPH_COMPOSITION_PRODUCER,
    CanonicalResolutionStatus,
    CompositionReason,
    ExpectedObjectType,
    GraphCompositionOperator,
    PredicateCardinality,
    TemporalAxis,
    TemporalQueryOperator,
)
from engram.constants_resolution import CandidateSource, EvidenceKind, ResolverState
from engram.errors import InvalidRequestError, WorkingMemoryExceededError
from engram.evidence import assertion_basis_window, proposition_evidence_record
from engram.relation import RelationQuestion, resolve_canonical_subject
from engram.resolution import (
    candidate as resolution_candidate,
    candidate_to_dict,
    evidence_reference,
    resolver_result,
    trusted_resolver_result_with_changes,
)
from engram.resolution_budget import budget_consumption, trusted_budget_consumption_with_changes
from engram.resolution_evidence import (
    feature_set,
    proposition_evidence_path_step,
    proposition_evidence_record_with_changes,
    trusted_proposition_evidence_record_to_dict,
)
from engram.resolution_frame import validate_query_frame
from engram.resolution_sizing import json_array_bytes, json_size, working_size
from engram.resolver_plan import resolver_clock_ns, run_cooperative_check

COMPOSITION_NOT_APPLICABLE_REASONS = {
    CompositionReason.IDENTITY_MISS.value,
    CompositionReason.UNSUPPORTED_QUERY.value,
    CompositionReason.UNDERCONSTRAINED.value,
}


def composition_candidate_id(operator: str, proposition_ids: tuple[str, ...], diagnostic_id: str) -> str:
    """Name one request-local composition proposal from exact ordered native fields."""
    digest = hashlib_sha256(b"engram-composition-candidate\0")
    for value in (operator, diagnostic_id, *proposition_ids):
        encoded = value.encode("utf-8")
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
    result = "composition:" + digest.hexdigest()
    return result


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


def counted_canonical_lookups(resolver, budget: dict, usage: dict) -> tuple:
    """Bind canonical entity and predicate lookups that spend the resolver call's graph-row budget."""

    def entity_lookup(surface: str, *, limit: int, cooperative_check=()) -> list:
        remaining = max(0, budget.get("max_graph_rows", 0) - usage.get("graph_rows", 0))
        if not remaining:
            return []
        rows = resolver.internal_engram.canonical_entity_matches(
            surface,
            limit=min(limit, remaining),
            cooperative_check=cooperative_check,
        )
        usage["graph_rows"] = usage.get("graph_rows", 0) + len(rows)
        return rows

    def predicate_lookup(surface: str, *, limit: int, cooperative_check=()) -> list:
        remaining = max(0, budget.get("max_graph_rows", 0) - usage.get("graph_rows", 0))
        if not remaining:
            return []
        rows = resolver.internal_engram.canonical_predicate_matches(
            surface,
            limit=min(limit, remaining),
            cooperative_check=cooperative_check,
        )
        usage["graph_rows"] = usage.get("graph_rows", 0) + len(rows)
        return rows

    result = (entity_lookup, predicate_lookup)
    return result


def composition_refusal(
    resolver,
    started: int,
    reason_code: str,
    subject_status: CanonicalResolutionStatus,
    graph_rows: int,
    exhausted_dimensions: tuple[str, ...] = (),
) -> tuple[dict, ...]:
    """Complete a composition attempt that compiled no executable plan."""
    result = resolver_result(
        resolver=resolver.name,
        state=ResolverState.COMPLETED,
        reason_code=reason_code,
        diagnostics={"composition": True, "entity_status": subject_status.value},
        consumption=budget_consumption(
            elapsed_ns=max(0, resolver_clock_ns(resolver.internal_clock_ns) - started),
            resolvers=1,
            graph_rows=graph_rows,
            exhausted_dimensions=exhausted_dimensions,
        ),
    )
    return (result,)


def compile_composition_plan(resolver, frame: dict, budget: dict, started: int, question: RelationQuestion, check) -> dict:
    """Resolve the composed subject and predicates into one bounded linear plan, or a completed refusal."""
    usage = {"graph_rows": 0}
    entity_lookup, predicate_lookup = counted_canonical_lookups(resolver, budget, usage)
    subject = resolve_canonical_subject(frame, entity_lookup, question=question, cooperative_check=check)
    subject_status = subject.get("status", CanonicalResolutionStatus.MISS)
    if subject_status != CanonicalResolutionStatus.SELECTED:
        reason = (
            CompositionReason.IDENTITY_AMBIGUOUS
            if subject_status == CanonicalResolutionStatus.AMBIGUOUS
            else CompositionReason.IDENTITY_MISS
        )
        compiled = {"refusal": composition_refusal(resolver, started, reason.value, subject_status, usage.get("graph_rows", 0))}
        return compiled
    try:
        predicates = resolve_composition_predicates(frame, subject, predicate_lookup, check)
        operator = graph_composition_operator(frame)
        remaining_rows = budget.get("max_graph_rows", 0) - usage.get("graph_rows", 0)
        if remaining_rows < 2 * len(predicates):
            refusal = composition_refusal(
                resolver,
                started,
                CompositionReason.ROW_LIMIT.value,
                subject_status,
                usage.get("graph_rows", 0),
                exhausted_dimensions=("graph_rows",),
            )
            compiled = {"refusal": refusal}
            return compiled
        plan = linear_composition_plan(
            subject,
            predicates,
            operator,
            frame.get("expected_object_type", ExpectedObjectType.UNKNOWN),
            max_rows=min(64, remaining_rows),
        )
    except InvalidRequestError as err:
        reason = str(err)
        if reason not in {value.value for value in CompositionReason}:
            reason = CompositionReason.UNSUPPORTED_QUERY.value
        compiled = {"refusal": composition_refusal(resolver, started, reason, subject_status, usage.get("graph_rows", 0))}
        return compiled
    compiled = {
        "refusal": (),
        "subject": subject,
        "operator": operator,
        "plan": plan,
        "graph_rows": usage.get("graph_rows", 0),
    }
    return compiled


def execute_composition(resolver, frame: dict, budget: dict, plan: dict, include_historical: bool, check) -> dict:
    """Execute one compiled plan through the resolver's fixed one-hop Proposition reads and eligibility checks."""

    def query(subject_id: str, predicate_id: str, limit: int) -> list[dict]:
        result = resolver.internal_engram.relation_one_hop_proposition_projections(
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
        lambda projection: resolver.internal_eligibility_evaluator.evaluate(projection, checked_frame, trusted_frame=True),
        lambda projection: resolver.internal_eligibility_evaluator.revalidate(
            projection,
            checked_frame,
            resolver.internal_engram.current_proposition_projection,
            trusted_frame=True,
        ),
        check,
    )
    return execution


def composition_direct_suppression(complete_paths: tuple, include_historical: bool, temporal_axis: TemporalAxis) -> set[str]:
    """Name every reason a complete path may not be phrased as a direct composed answer."""
    direct_suppression_reasons: set[str] = set()
    for path in complete_paths:
        for entry in path:
            proposition = entry.get("proposition", {})
            projection = proposition.get("projection", {})
            if not projection.get("supplied_trust_available", False):
                direct_suppression_reasons.add(CompositionReason.TRUST_UNAVAILABLE.value)
            if proposition.get("predicate_cardinality", PredicateCardinality.UNKNOWN) == PredicateCardinality.UNKNOWN:
                direct_suppression_reasons.add(CompositionReason.CARDINALITY_UNKNOWN.value)
            if include_historical:
                if temporal_axis == TemporalAxis.VALID_TIME:
                    lower_available = projection.get("valid_from_available", False)
                    upper_available = projection.get("valid_to_available", False)
                else:
                    lower_available = projection.get("system_from_available", False)
                    upper_available = projection.get("system_to_available", False)
                if not (lower_available and upper_available):
                    direct_suppression_reasons.add(CompositionReason.TEMPORAL_BOUNDS_OPEN.value)
    return direct_suppression_reasons


def composition_evidence_records(
    resolver_name: str,
    frame: dict,
    plan: dict,
    execution: dict,
    plan_operator: GraphCompositionOperator,
    direct_suppression_reasons: set[str],
) -> list:
    """Package each selected path's terminal Proposition as path-annotated evidence, ordered by Proposition id."""
    selected_paths = execution.get("complete_paths", ()) or execution.get("partial_paths", ())
    execution_reasons = execution.get("reasons", ())
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
            resolver_name,
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
                    PROPOSITION_PATH_FILTERS,
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
    return records


def composition_candidate(
    resolver_name: str,
    frame: dict,
    plan: dict,
    execution: dict,
    plan_operator: GraphCompositionOperator,
    subject: dict,
) -> dict:
    """Phrase the first complete path as one request-local composed answer candidate."""
    response = phrase_composition_result(plan, execution)
    selected_path = execution.get("complete_paths", ())[0]
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
            resolver=resolver_name,
            kind=EvidenceKind.PROPOSITION,
            scope=frame.get("scope", {}),
            provenance={"composition_path": True},
            diagnostics={},
        )
        for proposition_id in proposition_ids
    )
    composition_id = composition_candidate_id(plan_operator.value, proposition_ids, frame.get("diagnostic_id", ""))
    result = resolution_candidate(
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
            "producer": GRAPH_COMPOSITION_PRODUCER,
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
            "reasons": tuple(reason.value for reason in execution.get("reasons", ())),
        },
    )
    return result


def fit_composition_output(records: list, candidates: list, budget: dict) -> dict:
    """Trim records to the evidence-byte budget and candidates to the output budget, in place."""
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
    fitted = {"record_bytes": record_bytes(), "output_bytes": output_bytes, "exhausted": exhausted}
    return fitted


def internal_composition_result(
    resolver,
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

    def check() -> None:
        run_cooperative_check(cooperative_check)

    compiled = compile_composition_plan(resolver, frame, budget, started, question, check)
    refusal = compiled.get("refusal", ())
    if refusal:
        return refusal
    plan = compiled.get("plan", {})
    temporal = frame.get("temporal_query", {})
    temporal_axis = temporal.get("axis", TemporalAxis.VALID_TIME)
    include_historical = temporal.get("operator", TemporalQueryOperator.UNSPECIFIED) not in {
        TemporalQueryOperator.UNSPECIFIED,
        TemporalQueryOperator.CURRENT,
        TemporalQueryOperator.NOW,
    }
    execution = execute_composition(resolver, frame, budget, plan, include_historical, check)
    complete_paths = execution.get("complete_paths", ())
    execution_reasons = execution.get("reasons", ())
    plan_operator = plan.get("operator", compiled.get("operator", GraphCompositionOperator.LOOKUP))
    graph_rows = compiled.get("graph_rows", 0) + execution.get("graph_rows", 0)
    direct_suppression_reasons = composition_direct_suppression(complete_paths, include_historical, temporal_axis)
    composition_direct = False if direct_suppression_reasons else execution.get("direct_result", False)
    records = composition_evidence_records(resolver.name, frame, plan, execution, plan_operator, direct_suppression_reasons)
    if len(records) > budget.get("max_evidence", 0):
        records = records[: budget.get("max_evidence", 0)]
    candidates = []
    if composition_direct and complete_paths and budget.get("max_candidates", 0):
        candidates.append(composition_candidate(resolver.name, frame, plan, execution, plan_operator, compiled.get("subject", {})))
    fitted = fit_composition_output(records, candidates, budget)
    output_bytes = fitted.get("output_bytes", 0)
    working_memory = working_size(execution) + fitted.get("record_bytes", 0) + output_bytes
    if working_memory > budget.get("max_working_memory_bytes", 0):
        raise WorkingMemoryExceededError("composition working-memory estimate exceeded")
    result = resolver_result(
        resolver=resolver.name,
        state=ResolverState.COMPLETED,
        reason_code=(
            "graph_composition_candidate" if candidates else "graph_composition_evidence" if records else "graph_composition_miss"
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
            elapsed_ns=max(0, resolver_clock_ns(resolver.internal_clock_ns) - started),
            resolvers=1,
            candidates=len(candidates),
            graph_rows=graph_rows,
            evidence=len(records),
            evidence_bytes=fitted.get("record_bytes", 0),
            output_bytes=output_bytes,
            working_memory_bytes=working_memory,
            exhausted_dimensions=tuple(sorted(fitted.get("exhausted", set()))),
        ),
    )
    return (result,)
