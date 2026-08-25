"""Small policy-versioned rollout controls for unified resolution."""

from collections import Counter
from collections.abc import Mapping
from typing import cast

from engram.config import rollout_config
from engram.constants import RolloutMode
from engram.resolution import (
    ResolutionOutcome,
    ResolutionResult,
    budget_consumption_with_changes,
    empty_candidate,
    empty_evidence_package,
    resolution_result_to_json,
    resolution_result_with_changes,
    validate_resolution_result,
)


def select_rollout(config: Mapping[str, object], namespace: str) -> dict[str, object]:
    """Select the exact namespace override or the configured default."""
    policy = cast(dict, config.get("rollout") or rollout_config())
    namespaces = cast(dict, policy["namespaces"])
    result = {
        "policy_version": policy["policy_version"],
        "mode": namespaces.get(namespace, policy["default_mode"]),
        "namespace_override": namespace in namespaces,
    }
    return result


def rollout_status(config: Mapping[str, object]) -> dict[str, object]:
    """Return fixed-cardinality rollout state without namespace labels."""
    policy = cast(dict, config.get("rollout") or rollout_config())
    namespaces = cast(dict, policy["namespaces"])
    default_mode = cast(RolloutMode, policy["default_mode"])
    counts = Counter(namespaces.values())
    result = {
        "policy_version": policy["policy_version"],
        "default_mode": default_mode.value,
        "namespace_override_count": len(namespaces),
        "namespace_modes": {mode.value: counts[mode] for mode in RolloutMode},
    }
    return result


def apply_rollout(result: ResolutionResult, selection: Mapping[str, object]) -> ResolutionResult:
    """Apply output visibility for a selected rollout mode."""
    current = validate_resolution_result(result)
    mode = cast(RolloutMode, selection["mode"])
    if mode == RolloutMode.REGULATED_DIRECT_ANSWER:
        return current

    reason = f"rollout_{mode.value}"
    diagnostics = {
        "policy_version": selection["policy_version"],
        "mode": mode.value,
        "namespace_override": selection["namespace_override"],
    }
    reasons = tuple(dict.fromkeys((*current["reason_codes"], reason)))
    changes: dict[str, object] = {
        "reason_codes": reasons,
        "frame_diagnostics": {**current["frame_diagnostics"], "rollout": diagnostics},
    }
    if mode == RolloutMode.SHADOW:
        diagnostics["shadow_outcome"] = current["outcome"].value
        diagnostics["shadow_candidate_count"] = len(current["response_candidates"])
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
    elif current["outcome"] == ResolutionOutcome.ANSWER:
        changes.update(
            {
                "outcome": ResolutionOutcome.EVIDENCE,
                "selected_candidate": empty_candidate(),
                "selected_candidate_available": False,
                "response_candidates": (current["selected_candidate"],),
                "confidence": 0.0,
                "confidence_available": False,
            }
        )

    updated = resolution_result_with_changes(current, changes)
    for _ in range(4):
        size = len(resolution_result_to_json(updated).encode("utf-8"))
        consumption = budget_consumption_with_changes(
            updated["budget"],
            {
                "output_bytes": size,
                "working_memory_bytes": max(updated["budget"]["working_memory_bytes"], size),
            },
        )
        if consumption == updated["budget"]:
            return updated
        updated = resolution_result_with_changes(updated, {"budget": consumption})
    return updated
