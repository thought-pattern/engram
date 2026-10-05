"""Fixed-cardinality process-lifetime operational telemetry."""

from copy import deepcopy

from engram.constants import (
    OPERATIONAL_EXHAUSTION_DIMENSIONS,
    OPERATIONAL_LATENCY_BUCKETS,
    OPERATIONAL_RESOLUTION_OUTCOMES,
    OPERATIONAL_RESOLVER_NAMES,
    OPERATIONAL_RESOLVER_STATES,
    OPERATIONAL_RESOURCE_DIMENSIONS,
    REGULATOR_OUTCOMES,
    ResolutionOutcome,
    ResolverState,
)

LATENCY_BUCKET_NAMES = (*(name for _, name in OPERATIONAL_LATENCY_BUCKETS), "gt_10_s")
# The empty process aggregate. It is a read-only template: its owner deep-copies
# it once and mutates only the copy. Every nested map is a distinct object, so the
# copy never shares a counter between two dimensions.
EMPTY_OPERATIONAL_TELEMETRY = {
    "resolution": {
        "requests": 0,
        "executions": 0,
        "replays": 0,
        "outcomes": dict.fromkeys(OPERATIONAL_RESOLUTION_OUTCOMES, 0),
        "latency": {"observations": 0, "total_ns": 0, "max_ns": 0, "buckets": dict.fromkeys(LATENCY_BUCKET_NAMES, 0)},
        "budget_exhaustion": dict.fromkeys(OPERATIONAL_EXHAUSTION_DIMENSIONS, 0),
        "resources": {name: {"total": 0, "max": 0} for name in OPERATIONAL_RESOURCE_DIMENSIONS},
    },
    "resolvers": {
        resolver: {
            "invocations": 0,
            "states": dict.fromkeys(OPERATIONAL_RESOLVER_STATES, 0),
            "candidate_contributions": 0,
            "evidence_contributions": 0,
            "selected_contributions": 0,
            "latency": {"observations": 0, "total_ns": 0, "max_ns": 0, "buckets": dict.fromkeys(LATENCY_BUCKET_NAMES, 0)},
        }
        for resolver in OPERATIONAL_RESOLVER_NAMES
    },
    "regulator_outcomes": dict.fromkeys(sorted(REGULATOR_OUTCOMES), 0),
    "graph_recall": {
        "consultations": 0,
        "hits": 0,
        "misses": 0,
        "failures": 0,
        "latency": {"observations": 0, "total_ns": 0, "max_ns": 0, "buckets": dict.fromkeys(LATENCY_BUCKET_NAMES, 0)},
    },
}


def record_duration(metrics: dict, elapsed_ns: int) -> None:
    elapsed = max(0, elapsed_ns)
    metrics["observations"] = metrics.get("observations", 0) + 1
    metrics["total_ns"] = metrics.get("total_ns", 0) + elapsed
    metrics["max_ns"] = max(metrics.get("max_ns", 0), elapsed)
    bucket = "gt_10_s"
    for limit, name in OPERATIONAL_LATENCY_BUCKETS:
        if elapsed <= limit:
            bucket = name
            break
    buckets = metrics.get("buckets", {})
    buckets[bucket] = buckets.get(bucket, 0) + 1


def record_resolution(telemetry: dict, resolution: dict, *, replayed: bool) -> bool:
    """Add one completed request without retaining request-scoped values.

    Every counter map below comes from EMPTY_OPERATIONAL_TELEMETRY, and every
    key is an enum value or is collapsed to a fixed "other" bucket first, so
    the reads find existing counters and the aggregate never gains a key.
    """
    metrics = telemetry.get("resolution", {})
    metrics["requests"] = metrics.get("requests", 0) + 1
    outcome = resolution.get("outcome", ResolutionOutcome.MISS).value
    outcomes = metrics.get("outcomes", {})
    outcomes[outcome] = outcomes.get(outcome, 0) + 1
    if replayed:
        metrics["replays"] = metrics.get("replays", 0) + 1
        return False

    metrics["executions"] = metrics.get("executions", 0) + 1
    consumption = resolution.get("budget", {})
    record_duration(metrics.get("latency", {}), consumption.get("elapsed_ns", 0))
    resources = metrics.get("resources", {})
    for name in OPERATIONAL_RESOURCE_DIMENSIONS:
        value = consumption.get(name, 0)
        resource = resources.get(name, {})
        resource["total"] = resource.get("total", 0) + value
        resource["max"] = max(resource.get("max", 0), value)
    budget_exhaustion = metrics.get("budget_exhaustion", {})
    for dimension in consumption.get("exhausted_dimensions", ()):
        key = dimension if dimension in OPERATIONAL_RESOURCE_DIMENSIONS else "other"
        budget_exhaustion[key] = budget_exhaustion.get(key, 0) + 1

    selected_id = ""
    if resolution.get("selected_candidate_available", False):
        selected_id = resolution.get("selected_candidate", {}).get("statement_id", "")
    resolvers = telemetry.get("resolvers", {})
    for result in resolution.get("resolver_results", []):
        resolver_name = result.get("resolver", "")
        name = resolver_name if resolver_name in OPERATIONAL_RESOLVER_NAMES else "other"
        resolver = resolvers.get(name, {})
        state = result.get("state", ResolverState.FAILED).value
        candidates = result.get("candidates", ())
        evidence_count = len(result.get("evidence", ())) + len(result.get("proposition_evidence", ()))
        states = resolver.get("states", {})
        resolver["invocations"] = resolver.get("invocations", 0) + 1
        states[state] = states.get(state, 0) + 1
        resolver["candidate_contributions"] = resolver.get("candidate_contributions", 0) + len(candidates)
        resolver["evidence_contributions"] = resolver.get("evidence_contributions", 0) + evidence_count
        if selected_id and any(candidate.get("statement_id", "") == selected_id for candidate in candidates):
            resolver["selected_contributions"] = resolver.get("selected_contributions", 0) + 1
        record_duration(resolver.get("latency", {}), result.get("consumption", {}).get("elapsed_ns", 0))
    return True


def record_regulator_outcome(telemetry: dict, outcome: str) -> None:
    """Count one non-replayed fixed Regulator verdict."""
    outcomes = telemetry.get("regulator_outcomes", {})
    if outcome not in outcomes:
        raise ValueError("regulator outcome is not a registered telemetry outcome")
    outcomes[outcome] = outcomes.get(outcome, 0) + 1


def record_graph_recall(telemetry: dict, outcome: str, elapsed_ns: int) -> None:
    """Count one conversational graph consultation without request labels."""
    if outcome not in {"hit", "miss", "failure"}:
        raise ValueError("graph recall outcome must be hit, miss, or failure")
    metrics = telemetry.get("graph_recall", {})
    counters = {"hit": "hits", "miss": "misses", "failure": "failures"}
    counter = counters.get(outcome, "failures")
    metrics["consultations"] = metrics.get("consultations", 0) + 1
    metrics[counter] = metrics.get(counter, 0) + 1
    record_duration(metrics.get("latency", {}), elapsed_ns)


def telemetry_snapshot(telemetry: dict) -> dict:
    """Return an isolated JSON-ready aggregate."""
    result = deepcopy(telemetry)
    return result
