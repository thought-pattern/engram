"""Prometheus text exposition of an owner's existing health snapshot.

This protocol library uses only the standard library. Engram, Tapestry and the isolated Workspace share it without
constructing a peer runtime. It reads only the explicit fields below; content, identifiers of work, paths, recent
observations and arbitrary nested members are never metric labels. Counters retain their owner's process lifetime.
"""

from datetime import datetime
from math import isfinite

METRICS_CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"


def append_metric(lines: list, name: str, kind: str, description: str, samples: list) -> None:
    """Append one metric family, omitting missing or invalid observations rather than synthesizing zero."""
    rendered = []
    for value, labels in samples:
        if type(value) not in (bool, int, float) or not isfinite(value):
            continue
        escaped = {key: str(label).replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"') for key, label in labels.items()}
        suffix = ",".join(key + '="' + label + '"' for key, label in sorted(escaped.items()))
        number = str(int(value)) if type(value) is bool else str(value)
        rendered.append(name + "{" + suffix + "} " + number)
    if rendered:
        lines.extend(["# HELP " + name + " " + description, "# TYPE " + name + " " + kind, *rendered])


def health_exposition(snapshot: dict) -> str:
    """Format the bounded public process metrics; the caller owns and obtains the snapshot."""
    lines = []
    labels = {"component": snapshot.get("component", "")}
    incarnation = snapshot.get("incarnation", "")
    if incarnation:
        append_metric(
            lines, "tapestry_component_info", "gauge", "Current process incarnation.", [(1, {**labels, "incarnation": incarnation})]
        )
    for field, name, description in (
        ("ready", "tapestry_component_ready", "Whether the owner is ready for work."),
        ("stopping", "tapestry_component_stopping", "Whether graceful owner shutdown has begun."),
        ("process_id", "tapestry_component_process_id", "Process identifier in the owner's host namespace."),
        ("uptime_seconds", "tapestry_component_uptime_seconds", "Seconds since the owner started."),
        ("queue_depth", "tapestry_queue_depth", "Waiting items visible to the owning queue."),
        ("oldest_wait_seconds", "tapestry_queue_oldest_wait_seconds", "Age of the oldest item in the owning queue."),
    ):
        append_metric(lines, name, "gauge", description, [(snapshot.get(field), labels)])
    started_at = snapshot.get("started_at", "")
    if type(started_at) is str and started_at:
        try:
            started = datetime.fromisoformat(started_at)
        except ValueError:
            pass
        else:
            if started.tzinfo is not None:
                append_metric(
                    lines,
                    "tapestry_component_start_time_seconds",
                    "gauge",
                    "Owner start time since Unix epoch.",
                    [(started.timestamp(), labels)],
                )
    active = snapshot.get("active_work")
    if type(active) is list:
        append_metric(
            lines,
            "tapestry_active_work",
            "gauge",
            "Owner work items currently in progress, including their executing requests.",
            [(len(active), labels)],
        )
    requests = snapshot.get("requests", {})
    for field in ("received", "responses"):
        append_metric(
            lines,
            "tapestry_requests_" + field + "_total",
            "counter",
            "Process request " + field + ".",
            [(requests.get(field), labels)],
        )
    operations = requests.get("by_operation", {})
    for field in ("received", "responses"):
        samples = [(row.get(field), {**labels, "operation": operation}) for operation, row in operations.items()]
        append_metric(
            lines,
            "tapestry_operation_requests_" + field + "_total",
            "counter",
            "Process request " + field + " by operation.",
            samples,
        )
    samples = [
        (count, {**labels, "operation": operation, "outcome": outcome})
        for operation, row in operations.items()
        for outcome, count in row.get("outcomes", {}).items()
    ]
    append_metric(
        lines,
        "tapestry_request_outcomes_total",
        "counter",
        "Process request outcomes, including Regulator verdict-stage ACCEPT and REJECT.",
        samples,
    )
    work = snapshot.get("work", {}).get("by_operation", {})
    for field in ("started", "finished"):
        samples = [(row.get(field), {**labels, "operation": operation}) for operation, row in work.items()]
        append_metric(
            lines,
            "tapestry_work_" + field + "_total",
            "counter",
            "Work handed to other components, " + field + " by operation.",
            samples,
        )
    samples = [
        (count, {**labels, "operation": operation, "outcome": outcome})
        for operation, row in work.items()
        for outcome, count in row.get("outcomes", {}).items()
    ]
    append_metric(lines, "tapestry_work_outcomes_total", "counter", "Outcomes of work handed to other components.", samples)
    timing = snapshot.get("timing", {})
    for metric, suffix in (("seconds", "seconds"), ("samples", "count")):
        samples = [
            (timing.get(field + "_" + suffix), {**labels, "phase": phase})
            for phase, field in (
                ("total", "total" if metric == "seconds" else "terminal"),
                ("execution", "execution"),
                ("queue_only", "queue_only"),
            )
        ]
        append_metric(
            lines,
            "tapestry_request_elapsed_" + metric + "_total",
            "counter",
            "Request elapsed " + metric + " by phase; each phase has its own sample denominator.",
            samples,
        )
    errors = snapshot.get("errors", {}).get("by_kind", {})
    append_metric(
        lines,
        "tapestry_errors_total",
        "counter",
        "Process errors by kind.",
        [(count, {**labels, "kind": kind}) for kind, count in errors.items()],
    )
    usage = snapshot.get("usage", {})
    usage_labels = {**labels, "scope": usage.get("scope", "")}
    for field, name, description in (
        ("total_calls", "tapestry_provider_calls_total", "Completed provider exchanges in the owner's accounting scope."),
        ("total_provider_failures", "tapestry_provider_failures_total", "Provider failures in the owner's accounting scope."),
        (
            "total_prompt_tokens",
            "tapestry_provider_input_tokens_total",
            "Reported provider input tokens in the owner's accounting scope.",
        ),
        (
            "total_completion_tokens",
            "tapestry_provider_output_tokens_total",
            "Reported provider output tokens in the owner's accounting scope.",
        ),
    ):
        append_metric(lines, name, "counter", description, [(usage.get(field), usage_labels)])
    for axis, label in (("by_model", "model"), ("by_operation", "operation")):
        for field, name in (("calls", "calls"), ("prompt_tokens", "input_tokens"), ("completion_tokens", "output_tokens")):
            samples = [(row.get(field), {**usage_labels, label: value}) for value, row in usage.get(axis, {}).items()]
            append_metric(
                lines,
                "tapestry_provider_" + label + "_" + name + "_total",
                "counter",
                "Provider " + name.replace("_", " ") + " by " + label + ".",
                samples,
            )
    provider_cache = usage.get("provider_cache", {})
    for direction in ("read", "write"):
        cache_labels = {**usage_labels, "direction": direction}
        reported = provider_cache.get(direction + "_reported_calls")
        append_metric(
            lines,
            "tapestry_provider_cache_" + direction + "_reports_total",
            "counter",
            "Provider calls reporting cache " + direction + " usage.",
            [(reported, cache_labels)],
        )
        if type(reported) is int and reported > 0:
            append_metric(
                lines,
                "tapestry_provider_cache_" + direction + "_tokens_total",
                "counter",
                "Reported provider cache " + direction + " input tokens.",
                [(provider_cache.get(direction + "_input_tokens"), cache_labels)],
            )
    cache = snapshot.get("cache", {})
    for field, metric, kind in (
        ("enabled", "enabled", "gauge"),
        ("reads", "reads_total", "counter"),
        ("lifetime_hits", "hits_total", "counter"),
        ("lifetime_misses", "misses_total", "counter"),
        ("writes", "writes_total", "counter"),
        ("replacements", "replacements_total", "counter"),
        ("size", "entries", "gauge"),
        ("payload_bytes", "payload_bytes", "gauge"),
        ("max_entries", "entries_limit", "gauge"),
        ("max_payload_bytes", "payload_bytes_limit", "gauge"),
    ):
        append_metric(
            lines,
            "tapestry_local_cache_" + metric,
            kind,
            "Local inference cache " + metric.replace("_", " ") + ".",
            [(cache.get(field), labels)],
        )
    return "\n".join(lines) + "\n"
