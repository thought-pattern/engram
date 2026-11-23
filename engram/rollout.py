"""Small policy-driven rollout controls for unified resolution."""

from collections import Counter

from engram.config import rollout_config
from engram.constants import EMPTY_METADATA, RolloutMode
from engram.constants_evidence import EvidencePackageTruncationReason
from engram.constants_resolution import ResolutionOutcome
from engram.errors import InvalidRequestError
from engram.resolution import empty_candidate, resolution_result_to_dict, resolution_result_with_changes, validate_resolution_result
from engram.resolution_budget import budget_consumption_with_changes, resolution_budget, validate_resolution_budget
from engram.resolution_package import empty_evidence_package, evidence_package_payload
from engram.resolution_sizing import encoded_size


def select_rollout(config: dict, namespace: str) -> dict:
    """Select the exact namespace override or the configured default."""
    policy = config.get("rollout", {}) or rollout_config()
    if not isinstance(policy, dict):
        raise InvalidRequestError("rollout policy must be an object")
    namespaces = policy.get("namespaces", {})
    if not isinstance(namespaces, dict):
        raise InvalidRequestError("rollout namespaces must be an object")
    result = {
        "mode": namespaces.get(namespace, policy.get("default_mode", RolloutMode.DISABLED)),
        "namespace_override": namespace in namespaces,
    }
    return result


def rollout_status(config: dict) -> dict:
    """Return fixed-cardinality rollout state without namespace labels."""
    policy = config.get("rollout", {}) or rollout_config()
    if not isinstance(policy, dict):
        raise InvalidRequestError("rollout policy must be an object")
    namespaces = policy.get("namespaces", {})
    default_mode = policy.get("default_mode", RolloutMode.DISABLED)
    if not isinstance(namespaces, dict) or not isinstance(default_mode, RolloutMode):
        raise InvalidRequestError("rollout policy fields are invalid")
    counts = Counter(namespaces.values())
    result = {
        "default_mode": default_mode.value,
        "namespace_override_count": len(namespaces),
        "namespace_modes": {mode.value: counts.get(mode, 0) for mode in RolloutMode},
    }
    return result


def settle_rollout_budget(
    updated: dict,
    limits: dict,
    *,
    output_truncated: bool,
    diagnostic_exhausted: bool,
) -> dict:
    """Converge output, diagnostic, working-memory, and evidence consumption after rollout edits."""
    max_diagnostic_bytes = limits.get("max_diagnostic_bytes", 0)
    max_working_memory_bytes = limits.get("max_working_memory_bytes", 0)
    max_evidence_bytes = limits.get("max_evidence_bytes", 0)
    for _ in range(8):
        size = encoded_size(resolution_result_to_dict(updated))
        budget = updated.get("budget", {})
        exhausted = set(budget.get("exhausted_dimensions", ()))
        if output_truncated:
            exhausted.add("output_bytes")
        if diagnostic_exhausted:
            exhausted.add("diagnostic_bytes")
        resolver_diag = sum(item.get("consumption", {}).get("diagnostic_bytes", 0) for item in updated.get("resolver_results", ()))
        frame_diag = updated.get("frame_diagnostics", {})
        frame_diag_bytes = encoded_size(frame_diag) if frame_diag else 0
        total_diag = resolver_diag + frame_diag_bytes
        if total_diag > max_diagnostic_bytes:
            exhausted.add("diagnostic_bytes")
        if size > max_working_memory_bytes:
            exhausted.add("working_memory_bytes")
        package = updated.get("evidence_package", {})
        package_available = updated.get("evidence_package_available", False)
        package_bytes = encoded_size(evidence_package_payload(package)) if package_available else 0
        ref_bytes = sum(encoded_size(ref) for ref in updated.get("evidence", ()))
        consumption = budget_consumption_with_changes(
            budget,
            {
                "evidence": len(updated.get("evidence", ())) + (package.get("retained_count", 0) if package_available else 0),
                "evidence_bytes": (
                    min(max_evidence_bytes, budget.get("evidence_bytes", 0), ref_bytes + package_bytes)
                    if output_truncated
                    else budget.get("evidence_bytes", 0)
                ),
                "output_bytes": size,
                "diagnostic_bytes": min(max_diagnostic_bytes, total_diag),
                "working_memory_bytes": min(max_working_memory_bytes, max(budget.get("working_memory_bytes", 0), size)),
                "exhausted_dimensions": tuple(sorted(exhausted)),
            },
        )
        if consumption == budget:
            return updated
        updated = resolution_result_with_changes(updated, {"budget": consumption})
    raise InvalidRequestError("rollout budget accounting did not converge")


def fit_rollout_output(updated: dict, limits: dict, *, diagnostic_exhausted: bool) -> dict:
    """Trim a rollout-adjusted ResolutionResult so its UTF-8 JSON fits max_output_bytes."""
    max_output_bytes = limits.get("max_output_bytes", 0)
    settled = settle_rollout_budget(updated, limits, output_truncated=False, diagnostic_exhausted=diagnostic_exhausted)
    if encoded_size(resolution_result_to_dict(settled)) <= max_output_bytes:
        return settled
    reasons = list(dict.fromkeys((*settled.get("reason_codes", ()), "output_truncated")))
    frame_diag = dict(settled.get("frame_diagnostics", {}))
    frame_diag.pop("rollout", ())
    if frame_diag:
        compact_diag: dict = {"output_truncated": True}
        if "diagnostic_id" in frame_diag:
            compact_diag["diagnostic_id"] = frame_diag.get("diagnostic_id", "")
        max_diag = limits.get("max_diagnostic_bytes", 0)
        frame_diag = compact_diag if encoded_size(compact_diag) <= max_diag else {}
    settled = resolution_result_with_changes(
        settled,
        {"reason_codes": tuple(reasons), "frame_diagnostics": frame_diag},
    )
    settled = settle_rollout_budget(settled, limits, output_truncated=True, diagnostic_exhausted=diagnostic_exhausted)
    while settled.get("resolver_results", ()) and encoded_size(resolution_result_to_dict(settled)) > max_output_bytes:
        settled = resolution_result_with_changes(settled, {"resolver_results": settled.get("resolver_results", ())[:-1]})
        settled = settle_rollout_budget(settled, limits, output_truncated=True, diagnostic_exhausted=diagnostic_exhausted)
    while (
        settled.get("evidence_package", {}).get("records", ())
        and encoded_size(resolution_result_to_dict(settled)) > max_output_bytes
    ):
        pkg = settled.get("evidence_package", {})
        next_records = pkg.get("records", ())[:-1]
        trunc_reasons = tuple(
            sorted(
                {*pkg.get("truncation_reasons", ()), EvidencePackageTruncationReason.SERIALIZED_SIZE_LIMIT},
                key=lambda item: item.value,
            )
        )
        next_pkg = {
            "records": next_records,
            "retained_count": len(next_records),
            "omitted_count": pkg.get("omitted_count", 0) + 1,
            "truncated": True,
            "truncation_reasons": trunc_reasons,
        }
        settled = resolution_result_with_changes(settled, {"evidence_package": next_pkg})
        settled = settle_rollout_budget(settled, limits, output_truncated=True, diagnostic_exhausted=diagnostic_exhausted)
    while settled.get("evidence", ()) and encoded_size(resolution_result_to_dict(settled)) > max_output_bytes:
        settled = resolution_result_with_changes(settled, {"evidence": settled.get("evidence", ())[:-1]})
        settled = settle_rollout_budget(settled, limits, output_truncated=True, diagnostic_exhausted=diagnostic_exhausted)
    while settled.get("response_candidates", ()) and encoded_size(resolution_result_to_dict(settled)) > max_output_bytes:
        settled = resolution_result_with_changes(settled, {"response_candidates": settled.get("response_candidates", ())[:-1]})
        settled = settle_rollout_budget(settled, limits, output_truncated=True, diagnostic_exhausted=diagnostic_exhausted)
    if settled.get("outcome", ResolutionOutcome.MISS) == ResolutionOutcome.EVIDENCE and not (
        settled.get("response_candidates", ())
        or settled.get("evidence", ())
        or settled.get("evidence_package", {}).get("records", ())
    ):
        miss_reasons = tuple(dict.fromkeys((*settled.get("reason_codes", ()), "no_usable_output_after_truncation")))
        settled = resolution_result_with_changes(
            settled,
            {"outcome": ResolutionOutcome.MISS, "reason_codes": miss_reasons},
        )
        settled = settle_rollout_budget(settled, limits, output_truncated=True, diagnostic_exhausted=diagnostic_exhausted)
    return settled


def apply_rollout(result: dict, selection: dict, budget_limits: dict = EMPTY_METADATA) -> dict:
    """Apply output visibility for a selected rollout mode within the caller's budget."""
    current = validate_resolution_result(result)
    limits = validate_resolution_budget(budget_limits) if budget_limits else resolution_budget()
    mode = selection.get("mode", RolloutMode.DISABLED)
    if not isinstance(mode, RolloutMode):
        raise InvalidRequestError("rollout selection mode is invalid")
    if mode == RolloutMode.REGULATED_DIRECT_ANSWER:
        return current

    reason = f"rollout_{mode.value}"
    diagnostics: dict[str, object] = {
        "mode": mode.value,
        "namespace_override": selection.get("namespace_override", False),
    }
    reasons = tuple(dict.fromkeys((*current.get("reason_codes", ()), reason)))
    changes: dict = {
        "reason_codes": reasons,
    }
    if mode in {RolloutMode.DISABLED, RolloutMode.SHADOW}:
        observed_outcome = current.get("outcome", ResolutionOutcome.MISS).value
        observed_candidate_count = len(current.get("response_candidates", ()))
        diagnostics["observed_outcome"] = observed_outcome
        diagnostics["observed_candidate_count"] = observed_candidate_count
        if mode == RolloutMode.SHADOW:
            diagnostics["shadow_outcome"] = observed_outcome
            diagnostics["shadow_candidate_count"] = observed_candidate_count
        changes.update(
            {
                "outcome": ResolutionOutcome.MISS,
                "selected_candidate": empty_candidate(),
                "selected_candidate_available": False,
                "response_candidates": (),
                "evidence": (),
                "confidence": 0.0,
                "confidence_available": False,
                "resolver_results": (),
                "evidence_package_available": False,
                "evidence_package": empty_evidence_package(),
            }
        )
    elif current.get("outcome", ResolutionOutcome.MISS) == ResolutionOutcome.ANSWER:
        changes.update(
            {
                "outcome": ResolutionOutcome.EVIDENCE,
                "selected_candidate": empty_candidate(),
                "selected_candidate_available": False,
                "response_candidates": (current.get("selected_candidate", empty_candidate()),),
                "confidence": 0.0,
                "confidence_available": False,
            }
        )

    retained_resolvers = changes.get("resolver_results", current.get("resolver_results", ()))
    resolver_diag_bytes = sum(item.get("consumption", {}).get("diagnostic_bytes", 0) for item in retained_resolvers)
    candidate_frame_diag = {**current.get("frame_diagnostics", {}), "rollout": diagnostics}
    diagnostic_exhausted = resolver_diag_bytes + encoded_size(candidate_frame_diag) > limits.get("max_diagnostic_bytes", 0)
    if not diagnostic_exhausted:
        changes["frame_diagnostics"] = candidate_frame_diag

    updated = resolution_result_with_changes(current, changes)
    fitted = fit_rollout_output(updated, limits, diagnostic_exhausted=diagnostic_exhausted)
    return fitted
