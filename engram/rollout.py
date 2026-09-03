"""Small policy-versioned rollout controls for unified resolution."""

from collections import Counter

from engram.config import rollout_config
from engram.constants import RolloutMode
from engram.errors import InvalidRequestError
from engram.resolution import (
    ResolutionOutcome,
    budget_consumption_with_changes,
    empty_candidate,
    empty_evidence_package,
    resolution_result_to_json,
    resolution_result_with_changes,
    validate_resolution_result,
)


def select_rollout(config: dict, namespace: str) -> dict:
    """Select the exact namespace override or the configured default."""
    policy = config.get("rollout", {}) or rollout_config()
    if not isinstance(policy, dict):
        raise InvalidRequestError("rollout policy must be an object")
    namespaces = policy.get("namespaces", {})
    if not isinstance(namespaces, dict):
        raise InvalidRequestError("rollout namespaces must be an object")
    result = {
        "policy_version": policy.get("policy_version", ""),
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
        "policy_version": policy.get("policy_version", ""),
        "default_mode": default_mode.value,
        "namespace_override_count": len(namespaces),
        "namespace_modes": {mode.value: counts[mode] for mode in RolloutMode},
    }
    return result


def apply_rollout(result: dict, selection: dict) -> dict:
    """Apply output visibility for a selected rollout mode."""
    current = validate_resolution_result(result)
    mode = selection.get("mode", RolloutMode.DISABLED)
    if not isinstance(mode, RolloutMode):
        raise InvalidRequestError("rollout selection mode is invalid")
    if mode == RolloutMode.REGULATED_DIRECT_ANSWER:
        return current

    reason = f"rollout_{mode.value}"
    diagnostics = {
        "policy_version": selection.get("policy_version", ""),
        "mode": mode.value,
        "namespace_override": selection.get("namespace_override", False),
    }
    reasons = tuple(dict.fromkeys((*current.get("reason_codes", ()), reason)))
    changes: dict = {
        "reason_codes": reasons,
        "frame_diagnostics": {**current.get("frame_diagnostics", {}), "rollout": diagnostics},
    }
    if mode in {RolloutMode.DISABLED, RolloutMode.SHADOW}:
        diagnostics["observed_outcome"] = current.get("outcome", ResolutionOutcome.MISS).value
        diagnostics["observed_candidate_count"] = len(current.get("response_candidates", ()))
        if mode == RolloutMode.SHADOW:
            diagnostics["shadow_outcome"] = diagnostics["observed_outcome"]
            diagnostics["shadow_candidate_count"] = diagnostics["observed_candidate_count"]
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

    updated = resolution_result_with_changes(current, changes)
    for _ in range(4):
        size = len(resolution_result_to_json(updated).encode("utf-8"))
        consumption = budget_consumption_with_changes(
            updated.get("budget", {}),
            {
                "output_bytes": size,
                "working_memory_bytes": max(updated.get("budget", {}).get("working_memory_bytes", 0), size),
            },
        )
        if consumption == updated.get("budget", {}):
            return updated
        updated = resolution_result_with_changes(updated, {"budget": consumption})
    raise InvalidRequestError("rollout budget accounting did not converge")
