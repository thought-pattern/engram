"""Sequential bounded fail-soft execution of one resolution plan, bounding each result to its resolver lease."""

from logging import getLogger as logging_getLogger

from engram.constants_resolution import CandidateSource, ResolverState
from engram.errors import (
    InvalidRequestError,
    ResolutionCancelledError,
)
from engram.resolution import (
    resolver_result,
    trusted_candidate_to_dict,
    trusted_candidate_with_changes,
    trusted_evidence_reference_to_dict,
    trusted_resolver_result_with_changes,
    validate_resolver_result,
)
from engram.resolution_budget import (
    BudgetLedger,
    budget_consumption,
    trusted_budget_consumption_with_changes,
)
from engram.resolution_evidence import (
    trusted_proposition_evidence_record_to_dict,
)
from engram.resolution_frame import validate_query_frame
from engram.resolution_sizing import json_array_size, json_size
from engram.resolver_plan import (
    execute_resolver,
    resolver_budget,
    resolver_clock_ns,
    resolver_contract,
    resolver_reservation,
    run_cooperative_check,
    validate_resolution_plan,
)

# Resolver execution failures keep logging on the resolvers channel.
logger = logging_getLogger("engram.resolvers")


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
            except Exception as err:
                logger.warning("Resolver %s failed", resolver_name, exc_info=err)
                finished = resolver_clock_ns(self.internal_clock_ns)
                elapsed = max(0, finished - started)
                result = resolver_result(
                    resolver=resolver_name,
                    state=ResolverState.FAILED,
                    reason_code="resolver_exception",
                    diagnostics={"exception_type": type(err).__name__},
                    consumption=budget_consumption(elapsed_ns=elapsed, resolvers=1),
                )
            else:
                finished = resolver_clock_ns(self.internal_clock_ns)
                elapsed = max(0, finished - started)
                try:
                    current_raw = validate_resolver_result(raw)
                except InvalidRequestError as err:
                    logger.warning("Resolver %s returned an invalid result", resolver_name, exc_info=err)
                    result = resolver_result(
                        resolver=resolver_name,
                        state=ResolverState.FAILED,
                        reason_code="invalid_resolver_result",
                        diagnostics={"exception_type": type(err).__name__},
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
