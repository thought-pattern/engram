"""Fixed-cardinality process-lifetime operational telemetry."""

from copy import deepcopy

from engram.constants import (
    OPERATIONAL_EXHAUSTION_DIMENSIONS,
    OPERATIONAL_LATENCY_BUCKETS,
    OPERATIONAL_REBUILD_KINDS,
    OPERATIONAL_RESOLUTION_OUTCOMES,
    OPERATIONAL_RESOLVER_NAMES,
    OPERATIONAL_RESOLVER_STATES,
    OPERATIONAL_RESOURCE_DIMENSIONS,
    OPERATIONAL_TELEMETRY_SCHEMA_VERSION,
    REGULATOR_OUTCOMES,
)


def _latency_buckets() -> dict[str, int]:
    result = {name: 0 for _, name in OPERATIONAL_LATENCY_BUCKETS}
    result["gt_10_s"] = 0
    return result


def _duration_metrics() -> dict[str, object]:
    result = {
        "observations": 0,
        "total_ns": 0,
        "max_ns": 0,
        "buckets": _latency_buckets(),
    }
    return result


def operational_telemetry() -> dict:
    """Return an empty fixed-key process telemetry aggregate."""
    resolver_metrics = {
        resolver: {
            "invocations": 0,
            "states": dict.fromkeys(OPERATIONAL_RESOLVER_STATES, 0),
            "candidate_contributions": 0,
            "evidence_contributions": 0,
            "selected_contributions": 0,
            "latency": _duration_metrics(),
        }
        for resolver in OPERATIONAL_RESOLVER_NAMES
    }
    resource_metrics = {name: {"total": 0, "max": 0} for name in OPERATIONAL_RESOURCE_DIMENSIONS}
    rebuild_metrics = {
        kind: {
            "attempts": 0,
            "successes": 0,
            "failures": 0,
            "dry_runs": 0,
            "latency": _duration_metrics(),
        }
        for kind in OPERATIONAL_REBUILD_KINDS
    }
    result = {
        "schema_version": OPERATIONAL_TELEMETRY_SCHEMA_VERSION,
        "resolution": {
            "requests": 0,
            "executions": 0,
            "replays": 0,
            "outcomes": dict.fromkeys(OPERATIONAL_RESOLUTION_OUTCOMES, 0),
            "latency": _duration_metrics(),
            "budget_exhaustion": dict.fromkeys(OPERATIONAL_EXHAUSTION_DIMENSIONS, 0),
            "resources": resource_metrics,
        },
        "resolvers": resolver_metrics,
        "regulator_outcomes": dict.fromkeys(sorted(REGULATOR_OUTCOMES), 0),
        "rebuilds": rebuild_metrics,
        "durability": {
            "checkpoint_attempts": 0,
            "checkpoint_successes": 0,
            "checkpoint_failures": 0,
            "recovered_after_state": 0,
            "latency": _duration_metrics(),
        },
    }
    return result


def _record_duration(metrics: dict, elapsed_ns: int) -> None:
    elapsed = max(0, elapsed_ns)
    metrics["observations"] += 1
    metrics["total_ns"] += elapsed
    metrics["max_ns"] = max(metrics["max_ns"], elapsed)
    bucket = "gt_10_s"
    for limit, name in OPERATIONAL_LATENCY_BUCKETS:
        if elapsed <= limit:
            bucket = name
            break
    metrics["buckets"][bucket] += 1


def record_resolution(telemetry: dict, resolution: dict, *, replayed: bool) -> None:
    """Add one completed request without retaining request-scoped values."""
    metrics = telemetry["resolution"]
    metrics["requests"] += 1
    outcome = resolution["outcome"].value
    metrics["outcomes"][outcome] += 1
    if replayed:
        metrics["replays"] += 1
        return

    metrics["executions"] += 1
    consumption = resolution["budget"]
    _record_duration(metrics["latency"], consumption["elapsed_ns"])
    for name in OPERATIONAL_RESOURCE_DIMENSIONS:
        value = consumption[name]
        metrics["resources"][name]["total"] += value
        metrics["resources"][name]["max"] = max(metrics["resources"][name]["max"], value)
    for dimension in consumption["exhausted_dimensions"]:
        key = dimension if dimension in OPERATIONAL_RESOURCE_DIMENSIONS else "other"
        metrics["budget_exhaustion"][key] += 1

    selected_id = ""
    if resolution["selected_candidate_available"]:
        selected_id = resolution["selected_candidate"]["statement_id"]
    for result in resolution["resolver_results"]:
        name = result["resolver"] if result["resolver"] in OPERATIONAL_RESOLVER_NAMES else "other"
        resolver = telemetry["resolvers"][name]
        state = result["state"].value
        resolver["invocations"] += 1
        resolver["states"][state] += 1
        resolver["candidate_contributions"] += len(result["candidates"])
        resolver["evidence_contributions"] += len(result["evidence"]) + len(result["proposition_evidence"])
        if selected_id and any(candidate["statement_id"] == selected_id for candidate in result["candidates"]):
            resolver["selected_contributions"] += 1
        _record_duration(resolver["latency"], result["consumption"]["elapsed_ns"])


def record_regulator_outcome(telemetry: dict, outcome: str) -> None:
    """Count one non-replayed fixed Regulator verdict."""
    telemetry["regulator_outcomes"][outcome] += 1


def record_rebuild(telemetry: dict, kind: str, elapsed_ns: int, *, succeeded: bool, applied: bool = True) -> None:
    """Record one fixed-kind rebuild or dry-run attempt."""
    metrics = telemetry["rebuilds"][kind]
    metrics["attempts"] += 1
    if not applied:
        metrics["dry_runs"] += 1
    elif succeeded:
        metrics["successes"] += 1
    else:
        metrics["failures"] += 1
    _record_duration(metrics["latency"], elapsed_ns)


def record_checkpoint(telemetry: dict, elapsed_ns: int, *, succeeded: bool, recovered_after_state: bool = False) -> None:
    """Record one persistence attempt and its bounded recovery class."""
    metrics = telemetry["durability"]
    metrics["checkpoint_attempts"] += 1
    if succeeded:
        metrics["checkpoint_successes"] += 1
    else:
        metrics["checkpoint_failures"] += 1
    if recovered_after_state:
        metrics["recovered_after_state"] += 1
    _record_duration(metrics["latency"], elapsed_ns)


def telemetry_snapshot(telemetry: dict) -> dict:
    """Return an isolated JSON-ready aggregate."""
    result = deepcopy(telemetry)
    return result
