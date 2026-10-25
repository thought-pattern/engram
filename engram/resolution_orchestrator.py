"""Section 5 fused ANSWER/EVIDENCE/MISS orchestration over one request-local result assembly.

The assembly is a plain dict owned by one ``resolve_with_plan`` call. Each stage reads
the values earlier stages settled and replaces the ones it changes, in the order the
result is built: fusion, Proposition evidence, result fields, output fitting, accounting.
"""

from engram.constants_resolution import (
    MAX_RESOURCE_COUNTER,
    PROPOSITION_EVIDENCE_PRODUCERS,
    CandidateSource,
    ResolutionOutcome,
    ResolverState,
)
from engram.errors import InvalidRequestError
from engram.evidence import (
    canonicalize_proposition_evidence,
    evaluate_evidence_usefulness,
    evidence_usefulness_policy,
    validate_evidence_usefulness_policy,
)
from engram.fusion import CandidateFusionEngine
from engram.resolution import (
    empty_candidate,
    evidence_reference_to_dict,
    trusted_candidate_to_dict,
    trusted_evidence_reference_to_dict,
    trusted_resolution_result,
    trusted_resolution_result_to_json,
    trusted_resolver_result_to_dict,
    trusted_resolver_result_with_changes,
)
from engram.resolution_accounting import ResolutionAccountingFinalizer, accounting_diagnostics, accounting_finalization
from engram.resolution_budget import budget_consumption_to_dict, budget_consumption_with_changes
from engram.resolution_package import build_evidence_package, empty_evidence_package, trusted_evidence_package_to_json
from engram.resolution_sizing import json_size, working_size
from engram.resolver_execution import ResolverExecutor
from engram.resolver_plan import (
    ResolverRegistry,
    resolver_clock_ns,
    resolver_reservation_to_dict,
    run_cooperative_check,
    trusted_resolution_plan_to_dict,
)


def append_reason(assembly: dict, reason: str) -> None:
    reason_codes = assembly.get("reason_codes", [])
    if reason not in reason_codes:
        reason_codes.append(reason)


def evidence_check(assembly: dict) -> None:
    """Run the request's cooperative cancellation check between evidence steps."""
    run_cooperative_check(assembly.get("cooperative_check", ()))


def fused_assembly(orchestrator, frame: dict, plan: dict, accept_exact: bool, cooperative_check: object) -> dict:
    """Execute the plan, fuse its candidates and evidence, and open the request-local result assembly."""
    execution = orchestrator.internal_executor.execute(frame, plan, cooperative_check)
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
    decision = orchestrator.internal_fusion.decide(
        frame,
        tuple(candidates),
        tuple(evidence),
        working_memory_limit=fusion_memory_limit,
        working_memory_limit_available=True,
        cooperative_check=cooperative_check,
    )
    run_cooperative_check(cooperative_check)
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
    assembly = {
        "frame": frame,
        "plan": plan,
        "cooperative_check": cooperative_check,
        "execution": execution,
        "execution_results": execution_results,
        "execution_consumption": execution_consumption,
        "budget_limits": budget_limits,
        "decision": decision,
        "decision_report": decision.get("report", {}),
        "decision_working_memory": decision.get("working_memory_bytes", 0),
        "outcome": outcome,
        "selected": selected,
        "selected_available": selected_available,
        "response_candidates": response_candidates,
        "reason_codes": reason_codes,
        "confidence": confidence,
        "confidence_available": confidence_available,
    }
    return assembly


def start_evidence_assembly(assembly: dict) -> None:
    """Size the resolvers' own evidence references and open an empty Proposition evidence package."""
    minimal_evidence_values = tuple(
        reference
        for result in assembly.get("execution_results", ())
        for reference in result.get("evidence", ())
        + tuple(nested for candidate in result.get("candidates", ()) for nested in candidate.get("evidence", ()))
    )
    minimal_evidence_bytes = (
        json_size([evidence_reference_to_dict(reference) for reference in minimal_evidence_values])
        if minimal_evidence_values
        else 0
    )
    evidence_diagnostics: dict[str, object] = {
        "available": False,
        "input_count": 0,
        "normalized_count": 0,
        "included_count": 0,
        "excluded_count": 0,
        "reason_counts": {},
    }
    assembly.update(
        {
            "evidence_package_available": False,
            "evidence_package": empty_evidence_package(),
            "package_source_records": (),
            "package_byte_limit": 256,
            "minimal_evidence_count": len(minimal_evidence_values),
            "minimal_evidence_bytes": minimal_evidence_bytes,
            "evidence_working_memory": 0,
            "orchestration_exhausted": set(),
            "evidence_diagnostics": evidence_diagnostics,
        }
    )


def attach_proposition_evidence(assembly: dict, evidence_policy: dict) -> None:
    """Screen trusted producers' Proposition evidence and package what is useful, when fusion found no ANSWER."""
    execution_results = assembly.get("execution_results", ())
    unexpected_proposition_records = any(
        result.get("proposition_evidence", ()) and result.get("resolver", "") not in PROPOSITION_EVIDENCE_PRODUCERS
        for result in execution_results
    )
    if unexpected_proposition_records:
        append_reason(assembly, "proposition_evidence_untrusted_producer")
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
    if assembly.get("outcome", ResolutionOutcome.MISS) != ResolutionOutcome.ANSWER and full_producer_available:
        admit_proposition_evidence(assembly, raw_proposition_records, evidence_policy)


def admit_proposition_evidence(assembly: dict, raw_proposition_records: tuple, evidence_policy: dict) -> None:
    """Screen and package the trusted records when twice their size fits the working memory fusion left."""
    assembly.get("evidence_diagnostics", {})["input_count"] = len(raw_proposition_records)
    budget_limits = assembly.get("budget_limits", {})
    remaining_working_memory = max(
        0,
        budget_limits.get("max_working_memory_bytes", 0)
        - assembly.get("execution_consumption", {}).get("working_memory_bytes", 0)
        - assembly.get("decision_working_memory", 0),
    )
    if working_size(raw_proposition_records) * 2 > remaining_working_memory:
        assembly.get("orchestration_exhausted", set()).add("working_memory_bytes")
        append_reason(assembly, "proposition_evidence_memory_exhausted")
    else:
        try:
            screened = screen_proposition_evidence(assembly, raw_proposition_records, evidence_policy)
        except InvalidRequestError:
            append_reason(assembly, "proposition_evidence_conflict")
        else:
            package_proposition_evidence(assembly, screened, remaining_working_memory)


def screen_proposition_evidence(assembly: dict, raw_proposition_records: tuple, evidence_policy: dict) -> dict:
    """Canonicalize the records, require them bound to the current frame, and apply the usefulness policy."""
    frame = assembly.get("frame", {})
    normalized_records = canonicalize_proposition_evidence(raw_proposition_records, lambda: evidence_check(assembly))
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
        evidence_check(assembly)
        usefulness = evaluate_evidence_usefulness(evidence_policy, record)
        usefulness_decisions.append(usefulness)
        for reason in usefulness.get("reasons", ()):
            reason_counts[reason.value] = reason_counts.get(reason.value, 0) + 1
        if usefulness.get("included", False):
            included_records.append(record)
    evidence_check(assembly)
    screened = {
        "normalized_records": normalized_records,
        "usefulness_decisions": usefulness_decisions,
        "included_records": included_records,
        "reason_counts": reason_counts,
    }
    return screened


def package_proposition_evidence(assembly: dict, screened: dict, remaining_working_memory: int) -> None:
    """Package the included records within the evidence and working-memory budgets left to this request."""
    budget_limits = assembly.get("budget_limits", {})
    evidence_diagnostics = assembly.get("evidence_diagnostics", {})
    orchestration_exhausted = assembly.get("orchestration_exhausted", set())
    normalized_records = screened.get("normalized_records", ())
    included_records = screened.get("included_records", [])
    package_source_records = tuple(included_records)
    evidence_working_memory = (
        working_size(normalized_records) + working_size(screened.get("usefulness_decisions", [])) + working_size(included_records)
    )
    evidence_diagnostics.update(
        {
            "normalized_count": len(normalized_records),
            "included_count": len(included_records),
            "excluded_count": len(normalized_records) - len(included_records),
            "reason_counts": screened.get("reason_counts", {}),
        }
    )
    remaining_evidence_bytes = max(
        0,
        budget_limits.get("max_evidence_bytes", 0) - assembly.get("minimal_evidence_bytes", 0),
    )
    package_byte_limit = min(65_536, max(256, remaining_evidence_bytes))
    assembly.update({"package_source_records": package_source_records, "package_byte_limit": package_byte_limit})
    candidate_package = build_evidence_package(
        package_source_records,
        max_records=min(10, max(0, budget_limits.get("max_evidence", 0) - assembly.get("minimal_evidence_count", 0))),
        max_bytes=package_byte_limit,
    )
    candidate_package_bytes = len(trusted_evidence_package_to_json(candidate_package).encode("utf-8"))
    if candidate_package_bytes <= remaining_evidence_bytes:
        assembly.update({"evidence_package_available": True, "evidence_package": candidate_package})
        evidence_working_memory += candidate_package_bytes
        evidence_diagnostics["available"] = True
    else:
        orchestration_exhausted.add("evidence_bytes")
        append_reason(assembly, "proposition_evidence_bytes_exhausted")
    assembly["evidence_working_memory"] = evidence_working_memory
    if evidence_working_memory > remaining_working_memory:
        assembly.update({"evidence_package_available": False, "evidence_package": empty_evidence_package()})
        orchestration_exhausted.add("working_memory_bytes")
        append_reason(assembly, "proposition_evidence_memory_exhausted")
        evidence_diagnostics["available"] = False
    elif assembly.get("evidence_package", {}).get("records", ()):
        append_reason(assembly, "proposition_evidence_included")
        if assembly.get("outcome", ResolutionOutcome.MISS) == ResolutionOutcome.MISS:
            assembly["outcome"] = ResolutionOutcome.EVIDENCE
    elif normalized_records:
        append_reason(assembly, "proposition_evidence_excluded")


def bound_frame_diagnostics(assembly: dict, values: dict[str, object]) -> dict[str, object]:
    budget_limits = assembly.get("budget_limits", {})
    remaining = max(
        0,
        budget_limits.get("max_diagnostic_bytes", 0) - assembly.get("execution_consumption", {}).get("diagnostic_bytes", 0),
    )
    if not values or json_size(values) <= remaining:
        return values
    assembly.get("orchestration_exhausted", set()).add("diagnostic_bytes")
    append_reason(assembly, "diagnostics_truncated")
    evidence_diagnostics = assembly.get("evidence_diagnostics", {})
    evidence_package = assembly.get("evidence_package", {})
    compact = {
        "diagnostic_id": assembly.get("frame", {}).get("diagnostic_id", ""),
        "proposition_evidence": {
            "available": assembly.get("evidence_package_available", False),
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


def sized_accounting(assembly: dict, compact: bool) -> dict[str, object]:
    """Report accounting as wide as the accounting write can make it.

    The write always applies candidacy and decides only the success and replay
    flags. JSON ``false`` is wider than ``true``, so those flags are sized as false,
    and the compact form is sized as if nothing were accepted.
    """
    finalization = accounting_finalization(
        assembly.get("preview_ids", ()),
        "" if compact else assembly.get("accepted_statement_id", ""),
        True,
        False,
        False,
    )
    result = accounting_diagnostics(finalization, compact)
    return result


def sync_evidence_diagnostics(assembly: dict) -> None:
    evidence_diagnostics = assembly.get("evidence_diagnostics", {})
    evidence_package = assembly.get("evidence_package", {})
    evidence_diagnostics.update(
        {
            "available": assembly.get("evidence_package_available", False),
            "retained_count": evidence_package.get("retained_count", 0),
            "omitted_count": evidence_package.get("omitted_count", 0),
            "truncated": evidence_package.get("truncated", False),
        }
    )
    visible_evidence_diagnostics = assembly.get("frame_diagnostics", {}).get("proposition_evidence", {})
    if isinstance(visible_evidence_diagnostics, dict):
        for name in ("available", "retained_count", "omitted_count"):
            if name in visible_evidence_diagnostics:
                visible_evidence_diagnostics[name] = evidence_diagnostics.get(name, False)


def complete_consumption(assembly: dict, elapsed_ns: int, reserve: bool) -> dict:
    """Return the result's resource use for its current diagnostics and evidence package.

    With ``reserve``, the values settled only after the accounting write are sized at
    their widest: output bytes at the output limit and working memory at its limit.
    """
    budget_limits = assembly.get("budget_limits", {})
    execution_consumption = assembly.get("execution_consumption", {})
    evidence_package = assembly.get("evidence_package", {})
    evidence_package_available = assembly.get("evidence_package_available", False)
    frame_diagnostics = assembly.get("frame_diagnostics", {})
    working_memory_bytes = assembly.get("working_memory_bytes", 0)
    output_limit = assembly.get("output_limit", 0)
    max_evidence_bytes = budget_limits.get("max_evidence_bytes", 0)
    max_diagnostic_bytes = budget_limits.get("max_diagnostic_bytes", 0)
    max_working_memory_bytes = budget_limits.get("max_working_memory_bytes", 0)
    exhausted = set(execution_consumption.get("exhausted_dimensions", ())).union(assembly.get("orchestration_exhausted", set()))
    if assembly.get("output_truncated", False):
        exhausted.add("output_bytes")
    if assembly.get("decision_report", {}).get("budget_exhausted", "") == "fusion_memory_exhausted":
        exhausted.add("working_memory_bytes")
    package_evidence_bytes = (
        len(trusted_evidence_package_to_json(evidence_package).encode("utf-8")) if evidence_package_available else 0
    )
    evidence_count = assembly.get("minimal_evidence_count", 0) + evidence_package.get("retained_count", 0)
    evidence_bytes = assembly.get("minimal_evidence_bytes", 0) + package_evidence_bytes
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


def sized_output_bytes(assembly: dict, elapsed_ns: int) -> int:
    """Encode the result as large as it can be once accounting is written."""
    value = trusted_resolution_result(**assembly.get("result_fields", {}), budget=complete_consumption(assembly, elapsed_ns, True))
    result = len(trusted_resolution_result_to_json(value).encode("utf-8"))
    return result


def trailing_item_bytes(payload: dict, count: int) -> int:
    """Bytes the last of ``count`` encoded list items occupies, with its separating comma."""
    result = json_size(payload) + (1 if count > 1 else 0)
    return result


def package_field_bytes(assembly: dict) -> int:
    """Encode the only top-level fields a package trim changes: package, diagnostics, budget."""
    budget_payload = budget_consumption_to_dict(complete_consumption(assembly, MAX_RESOURCE_COUNTER, True))
    result = (
        len(trusted_evidence_package_to_json(assembly.get("evidence_package", {})).encode("utf-8"))
        + json_size(assembly.get("frame_diagnostics", {}))
        + json_size(budget_payload)
    )
    return result


def start_result_fields(assembly: dict, accept_exact: bool, credit_success: bool) -> None:
    """Settle the accepted statement, frame diagnostics, and the result fields the output budget fits."""
    frame = assembly.get("frame", {})
    selected = assembly.get("selected", {})
    execution_results = assembly.get("execution_results", ())
    budget_limits = assembly.get("budget_limits", {})
    evidence_diagnostics = assembly.get("evidence_diagnostics", {})
    evidence_package = assembly.get("evidence_package", {})
    accepted_statement_id = (
        selected.get("statement_id", "")
        if assembly.get("outcome", ResolutionOutcome.MISS) == ResolutionOutcome.ANSWER
        and selected.get("source", CandidateSource.UTILITY) == CandidateSource.EXACT
        and accept_exact
        and credit_success
        else ""
    )
    preview_ids = tuple(
        sorted(
            {observation.get("statement_id", "") for result in execution_results for observation in result.get("accounting", ())}
        )
    )
    working_memory_bytes = (
        assembly.get("execution_consumption", {}).get("working_memory_bytes", 0)
        + assembly.get("decision_working_memory", 0)
        + assembly.get("evidence_working_memory", 0)
    )
    assembly.update(
        {
            "accepted_statement_id": accepted_statement_id,
            "preview_ids": preview_ids,
            "output_limit": budget_limits.get("max_output_bytes", 0),
            "working_memory_bytes": working_memory_bytes,
            "output_truncated": False,
        }
    )
    evidence_diagnostics.update(
        {
            "available": assembly.get("evidence_package_available", False),
            "retained_count": evidence_package.get("retained_count", 0),
            "omitted_count": evidence_package.get("omitted_count", 0),
            "truncated": evidence_package.get("truncated", False),
        }
    )
    frame_diagnostics = bound_frame_diagnostics(
        assembly,
        {
            "diagnostic_id": frame.get("diagnostic_id", ""),
            "plan": trusted_resolution_plan_to_dict(assembly.get("plan", {})),
            "reservations": [
                resolver_reservation_to_dict(reservation) for reservation in assembly.get("execution", {}).get("reservations", ())
            ],
            "fusion": assembly.get("decision_report", {}),
            "proposition_evidence": evidence_diagnostics,
            "accounting": sized_accounting(assembly, False),
        },
    )
    assembly["frame_diagnostics"] = frame_diagnostics
    resolver_results = tuple(
        (
            trusted_resolver_result_with_changes(result, {"proposition_evidence": ()})
            if result.get("proposition_evidence", ())
            else result
        )
        for result in execution_results
    )
    assembly["result_fields"] = {
        "outcome": assembly.get("outcome", ResolutionOutcome.MISS),
        "selected_candidate": selected,
        "selected_candidate_available": assembly.get("selected_available", False),
        "response_candidates": assembly.get("response_candidates", ()),
        "evidence": assembly.get("decision", {}).get("evidence", ()),
        "confidence": assembly.get("confidence", 0.0),
        "confidence_available": assembly.get("confidence_available", False),
        "reason_codes": tuple(assembly.get("reason_codes", [])),
        "frame_diagnostics": frame_diagnostics,
        "resolver_results": resolver_results,
        "evidence_package_available": assembly.get("evidence_package_available", False),
        "evidence_package": evidence_package,
    }


def fit_output_budget(assembly: dict) -> None:
    """Truncate diagnostics, then trim resolver results, package records, evidence, and candidates to the output limit."""
    frame = assembly.get("frame", {})
    decision_report = assembly.get("decision_report", {})
    evidence_diagnostics = assembly.get("evidence_diagnostics", {})
    result_fields = assembly.get("result_fields", {})
    output_limit = assembly.get("output_limit", 0)
    evidence_package = assembly.get("evidence_package", {})
    assembly["output_truncated"] = True
    append_reason(assembly, "output_truncated")
    frame_diagnostics = bound_frame_diagnostics(
        assembly,
        {
            "diagnostic_id": frame.get("diagnostic_id", ""),
            "fusion": {
                "candidate_count": decision_report.get("candidate_count", 0),
                "output_truncated": True,
            },
            "proposition_evidence": {
                "available": assembly.get("evidence_package_available", False),
                "input_count": evidence_diagnostics.get("input_count", 0),
                "included_count": evidence_diagnostics.get("included_count", 0),
                "retained_count": evidence_package.get("retained_count", 0),
                "omitted_count": evidence_package.get("omitted_count", 0),
                "output_truncated": True,
            },
            "accounting": sized_accounting(assembly, True),
            "output_truncated": True,
        },
    )
    assembly["frame_diagnostics"] = frame_diagnostics
    result_fields.update(
        {
            "reason_codes": tuple(assembly.get("reason_codes", [])),
            "frame_diagnostics": frame_diagnostics,
        }
    )
    # The result is encoded once here; each trim then subtracts the exact bytes it
    # removes, since a JSON list item's encoding does not depend on its neighbours.
    # The complete encoding below re-checks the fitted result before accounting.
    sized = sized_output_bytes(assembly, MAX_RESOURCE_COUNTER)
    resolver_results = result_fields.get("resolver_results", ())
    while resolver_results and sized > output_limit:
        evidence_check(assembly)
        sized -= trailing_item_bytes(trusted_resolver_result_to_dict(resolver_results[-1]), len(resolver_results))
        resolver_results = resolver_results[:-1]
        result_fields["resolver_results"] = resolver_results
    while evidence_package.get("records", ()) and sized > output_limit:
        evidence_check(assembly)
        before = package_field_bytes(assembly)
        evidence_package = build_evidence_package(
            assembly.get("package_source_records", ()),
            max_records=len(evidence_package.get("records", ())) - 1,
            max_bytes=assembly.get("package_byte_limit", 256),
        )
        assembly["evidence_package"] = evidence_package
        result_fields["evidence_package"] = evidence_package
        sync_evidence_diagnostics(assembly)
        sized += package_field_bytes(assembly) - before
    response_evidence = result_fields.get("evidence", ())
    while response_evidence and sized > output_limit:
        evidence_check(assembly)
        sized -= trailing_item_bytes(trusted_evidence_reference_to_dict(response_evidence[-1]), len(response_evidence))
        response_evidence = response_evidence[:-1]
        result_fields["evidence"] = response_evidence
    response_candidates = result_fields.get("response_candidates", ())
    while response_candidates and sized > output_limit:
        evidence_check(assembly)
        sized -= trailing_item_bytes(trusted_candidate_to_dict(response_candidates[-1]), len(response_candidates))
        response_candidates = response_candidates[:-1]
        result_fields["response_candidates"] = response_candidates
    if assembly.get("outcome", ResolutionOutcome.MISS) == ResolutionOutcome.ANSWER and not response_candidates:
        assembly.update(
            {
                "outcome": ResolutionOutcome.MISS,
                "selected": empty_candidate(),
                "selected_available": False,
                "confidence": 0.0,
                "confidence_available": False,
                "accepted_statement_id": "",
            }
        )
        append_reason(assembly, "answer_exceeds_output_budget")
    if assembly.get("outcome", ResolutionOutcome.MISS) == ResolutionOutcome.EVIDENCE and not (
        response_candidates or response_evidence or evidence_package.get("records", ())
    ):
        assembly["outcome"] = ResolutionOutcome.MISS
        append_reason(assembly, "no_usable_output_after_truncation")


def settle_result_fields(assembly: dict) -> None:
    """Copy the settled decision fields into the result after any output fitting."""
    sync_evidence_diagnostics(assembly)
    assembly.get("result_fields", {}).update(
        {
            "outcome": assembly.get("outcome", ResolutionOutcome.MISS),
            "selected_candidate": assembly.get("selected", {}),
            "selected_candidate_available": assembly.get("selected_available", False),
            "confidence": assembly.get("confidence", 0.0),
            "confidence_available": assembly.get("confidence_available", False),
            "reason_codes": tuple(assembly.get("reason_codes", [])),
        }
    )


def settled_resolution_result(assembly: dict, finalization: dict, elapsed_ns: int) -> dict:
    """Build the released result from the written accounting, converging its own encoded size."""
    result_fields = assembly.get("result_fields", {})
    budget_limits = assembly.get("budget_limits", {})
    frame_diagnostics = assembly.get("frame_diagnostics", {})
    # The write settles only the accounting flags, each sized at its widest above, so the
    # result below is no larger than the one just checked.
    if "accounting" in frame_diagnostics:
        frame_diagnostics = {
            **frame_diagnostics,
            "accounting": accounting_diagnostics(finalization, assembly.get("output_truncated", False)),
        }
        assembly["frame_diagnostics"] = frame_diagnostics
        result_fields["frame_diagnostics"] = frame_diagnostics
    consumption = complete_consumption(assembly, elapsed_ns, False)
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
    if len(trusted_resolution_result_to_json(result).encode("utf-8")) > assembly.get("output_limit", 0):
        raise RuntimeError("resolution result outgrew the output size checked before accounting")
    return result


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
            except InvalidRequestError as err:
                raise InvalidRequestError("orchestrator evidence_policy has an invalid type") from err
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
        credit_success: bool = True,
    ) -> tuple[dict, dict]:
        """Resolve using a plan already created for this frame by the registry.

        ``credit_success`` False still decides the exact ANSWER a released result
        would carry, but records it as observed candidacy without success credit.
        """
        if not isinstance(accept_exact, bool):
            raise InvalidRequestError("accept_exact must be a boolean")
        if not isinstance(credit_success, bool):
            raise InvalidRequestError("credit_success must be a boolean")
        assembly = fused_assembly(self, frame, plan, accept_exact, cooperative_check)
        start_evidence_assembly(assembly)
        attach_proposition_evidence(assembly, self.internal_evidence_policy)
        start_result_fields(assembly, accept_exact, credit_success)
        # Nothing may fail or grow after the accounting write, so the result is fitted to the
        # output limit here. Trimming sizes against the widest elapsed time, because the real
        # one is measured just before the write.
        if sized_output_bytes(assembly, MAX_RESOURCE_COUNTER) > assembly.get("output_limit", 0):
            fit_output_budget(assembly)
        settle_result_fields(assembly)
        budget_limits = assembly.get("budget_limits", {})
        elapsed_ns = assembly.get("execution_consumption", {}).get("elapsed_ns", 0)
        started_ns = budget_limits.get("started_ns", 0)
        if started_ns:
            current_ns = resolver_clock_ns(self.internal_clock_ns)
            if isinstance(current_ns, bool) or not isinstance(current_ns, int) or current_ns < 0:
                raise InvalidRequestError("orchestrator clock_ns must return a nonnegative integer")
            elapsed_ns = max(elapsed_ns, 0, current_ns - started_ns)
        if sized_output_bytes(assembly, elapsed_ns) > assembly.get("output_limit", 0):
            raise InvalidRequestError("minimum resolution result exceeds max_output_bytes")

        run_cooperative_check(cooperative_check)
        finalization = self.internal_accounting.finalize(
            request_id,
            assembly.get("execution_results", ()),
            assembly.get("accepted_statement_id", ""),
        )
        result = settled_resolution_result(assembly, finalization, elapsed_ns), finalization
        return result
