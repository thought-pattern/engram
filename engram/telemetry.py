"""Fixed-cardinality process-lifetime operational telemetry."""

from copy import deepcopy

from engram.constants import (
    OPERATIONAL_EXHAUSTION_DIMENSIONS,
    OPERATIONAL_LATENCY_BUCKETS,
    OPERATIONAL_RESOLUTION_OUTCOMES,
    OPERATIONAL_RESOLVER_NAMES,
    OPERATIONAL_RESOLVER_STATES,
    OPERATIONAL_RESOURCE_DIMENSIONS,
    OPERATIONAL_TELEMETRY_SCHEMA_VERSION,
    REGULATOR_OUTCOMES,
    ResolutionOutcome,
)


def latency_buckets() -> dict[str, int]:
    result = {name: 0 for _, name in OPERATIONAL_LATENCY_BUCKETS}
    result["gt_10_s"] = 0
    return result


def duration_metrics() -> dict[str, object]:
    result = {
        "observations": 0,
        "total_ns": 0,
        "max_ns": 0,
        "buckets": latency_buckets(),
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
            "latency": duration_metrics(),
        }
        for resolver in OPERATIONAL_RESOLVER_NAMES
    }
    resource_metrics = {name: {"total": 0, "max": 0} for name in OPERATIONAL_RESOURCE_DIMENSIONS}
    result = {
        "schema_version": OPERATIONAL_TELEMETRY_SCHEMA_VERSION,
        "resolution": {
            "requests": 0,
            "executions": 0,
            "replays": 0,
            "outcomes": dict.fromkeys(OPERATIONAL_RESOLUTION_OUTCOMES, 0),
            "latency": duration_metrics(),
            "budget_exhaustion": dict.fromkeys(OPERATIONAL_EXHAUSTION_DIMENSIONS, 0),
            "resources": resource_metrics,
        },
        "resolvers": resolver_metrics,
        "regulator_outcomes": dict.fromkeys(sorted(REGULATOR_OUTCOMES), 0),
        "graph_recall": {
            "consultations": 0,
            "hits": 0,
            "misses": 0,
            "failures": 0,
            "latency": duration_metrics(),
        },
    }
    return result


def record_duration(metrics: dict, elapsed_ns: int) -> None:
    elapsed = max(0, elapsed_ns)
    metrics["observations"] += 1
    metrics["total_ns"] += elapsed
    metrics["max_ns"] = max(metrics.get("max_ns", 0), elapsed)
    bucket = "gt_10_s"
    for limit, name in OPERATIONAL_LATENCY_BUCKETS:
        if elapsed <= limit:
            bucket = name
            break
    metrics.get("buckets", {})[bucket] += 1


def record_resolution(telemetry: dict, resolution: dict, *, replayed: bool) -> bool:
    """Add one completed request without retaining request-scoped values."""
    metrics = telemetry.get("resolution", {})
    metrics["requests"] += 1
    outcome = resolution.get("outcome", ResolutionOutcome.MISS).value
    metrics["outcomes"][outcome] += 1
    if replayed:
        metrics["replays"] += 1
        return False

    metrics["executions"] += 1
    consumption = resolution.get("budget", {})
    record_duration(metrics["latency"], consumption["elapsed_ns"])
    for name in OPERATIONAL_RESOURCE_DIMENSIONS:
        value = consumption[name]
        metrics["resources"][name]["total"] += value
        metrics["resources"][name]["max"] = max(metrics["resources"][name]["max"], value)
    for dimension in consumption["exhausted_dimensions"]:
        key = dimension if dimension in OPERATIONAL_RESOURCE_DIMENSIONS else "other"
        metrics["budget_exhaustion"][key] += 1

    selected_id = ""
    if resolution.get("selected_candidate_available", False):
        selected_id = resolution.get("selected_candidate", {})["statement_id"]
    for result in resolution.get("resolver_results", []):
        name = result["resolver"] if result["resolver"] in OPERATIONAL_RESOLVER_NAMES else "other"
        resolver = telemetry.get("resolvers", {})[name]
        state = result["state"].value
        resolver["invocations"] += 1
        resolver["states"][state] += 1
        resolver["candidate_contributions"] += len(result["candidates"])
        resolver["evidence_contributions"] += len(result["evidence"]) + len(result["proposition_evidence"])
        if selected_id and any(candidate["statement_id"] == selected_id for candidate in result["candidates"]):
            resolver["selected_contributions"] += 1
        record_duration(resolver["latency"], result["consumption"]["elapsed_ns"])
    return True


def record_regulator_outcome(telemetry: dict, outcome: str) -> None:
    """Count one non-replayed fixed Regulator verdict."""
    telemetry.get("regulator_outcomes", {})[outcome] += 1


def record_graph_recall(telemetry: dict, outcome: str, elapsed_ns: int) -> None:
    """Count one conversational graph consultation without request labels."""
    if outcome not in {"hit", "miss", "failure"}:
        raise ValueError("graph recall outcome must be hit, miss, or failure")
    metrics = telemetry.get("graph_recall", {})
    counters = {"hit": "hits", "miss": "misses", "failure": "failures"}
    metrics["consultations"] += 1
    metrics[counters.get(outcome, "failures")] += 1
    record_duration(metrics["latency"], elapsed_ns)


def telemetry_snapshot(telemetry: dict) -> dict:
    """Return an isolated JSON-ready aggregate."""
    result = deepcopy(telemetry)
    return result
