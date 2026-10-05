"""Reproducible offline Section 6 feedback and negative-resolution benchmark."""

from argparse import ArgumentParser as argparse_ArgumentParser
from datetime import UTC, datetime
from json import dumps as json_dumps
from pathlib import Path
from platform import platform as platform_platform, python_version as platform_python_version
from statistics import median as statistics_median
from sys import path as sys_path
from time import perf_counter_ns as time_perf_counter_ns
from tracemalloc import get_traced_memory as tracemalloc_get_traced_memory, start as tracemalloc_start, stop as tracemalloc_stop

REPOSITORY = Path(__file__).resolve().parent.parent
if str(REPOSITORY) not in sys_path:
    sys_path.insert(0, str(REPOSITORY))

from engram.constants import ResolutionOutcome
from engram.core import Engram
from engram.feedback import (
    FeedbackObservationKind,
    FeedbackOutcome,
    FeedbackReferenceKind,
    FeedbackStore,
    NegativeResolutionStore,
    canonical_fingerprint,
    constraint_fingerprint,
    feedback_observation,
    feedback_state,
    feedback_state_from_json,
    feedback_state_to_json,
    negative_resolution_key,
)
from engram.identity import extract_standalone_identity, scope_key
from engram.service import EngramCore
from scripts.benchmark_metadata import benchmark_source_state, recorded_at

DEFAULT_OUTPUT = Path("eval/results/feedback/benchmark.json")
NOW = datetime(2026, 8, 16, 12, 0, tzinfo=UTC)
NOW_TEXT = "2026-08-16T12:00:00Z"
POLICY_FINGERPRINT = canonical_fingerprint("section6-benchmark-policy")


def internal_latency(values: list[float]) -> dict[str, float]:
    ordered = sorted(values)
    p95_index = min(len(ordered) - 1, max(0, int(len(ordered) * 0.95 + 0.999999) - 1))
    p99_index = min(len(ordered) - 1, max(0, int(len(ordered) * 0.99 + 0.999999) - 1))
    result = {
        "p50_ms": round(statistics_median(ordered), 4),
        "p95_ms": round(ordered[p95_index], 4),
        "p99_ms": round(ordered[p99_index], 4),
        "max_ms": round(ordered[-1], 4),
    }
    return result


def internal_time(call) -> tuple[object, float]:
    started = time_perf_counter_ns()
    value = call()
    result = value, (time_perf_counter_ns() - started) / 1_000_000
    return result


def internal_require_miss(result: dict, negative_hit: bool) -> None:
    """Refuse a timed resolution whose native attribution differs from its label."""
    reasons = tuple(result.get("reason_codes", ()))
    if result.get("outcome", "") != ResolutionOutcome.MISS or ("negative_resolution_hit" in reasons) != negative_hit:
        expected = "negative-store hit" if negative_hit else "completed resolver miss"
        raise ValueError(f"benchmark sample is not a {expected}: outcome {result.get('outcome', '')}, reasons {reasons}")


def internal_apply(store: FeedbackStore, request_id: str, value: dict) -> None:
    candidate = store.prepare(request_id, (value,))
    if not candidate.get("replayed", False):
        store.replace_from_snapshot(candidate.get("after", {}))


def ingest_benchmark_verdicts(store: FeedbackStore, record_count: int, prefix: str) -> None:
    """Admit alternating accepted/rejected synthetic verdicts in thousand-observation batches."""
    for start in range(0, record_count, 1_000):
        stop = min(record_count, start + 1_000)
        # feedback_observation validates and copies the scope, so one scope serves the batch.
        scope = scope_key(namespace="section6-benchmark")
        observations = tuple(
            feedback_observation(
                reference_kind=FeedbackReferenceKind.RESOLUTION_REQUEST,
                reference_id=f"resolution-{index}",
                kind=FeedbackObservationKind.VERDICT,
                outcome=FeedbackOutcome.ACCEPTED if index % 2 == 0 else FeedbackOutcome.REJECTED_CONTEXT,
                query_identity=extract_standalone_identity("What is the benchmark response?", scope),
                scope=scope,
                constraint_fingerprint=constraint_fingerprint("UNKNOWN", {}, ""),
                statement_id=f"{prefix}-statement-{index}",
                generation=1,
                generation_available=True,
                policy_fingerprint=POLICY_FINGERPRINT,
                observed_at=NOW_TEXT,
            )
            for index in range(start, stop)
        )
        candidate = store.prepare(f"{prefix}-batch-{start // 1_000}", observations)
        store.replace_from_snapshot(candidate.get("after", {}))


def run_benchmark(samples: int, memory_records: int, scale_records: int) -> dict:
    store = FeedbackStore()
    ingestion = []
    for index in range(samples):
        # Each timed sample builds and ingests one alternating verdict on the shared statement.
        started = time_perf_counter_ns()
        scope = scope_key(namespace="section6-benchmark")
        verdict = feedback_observation(
            reference_kind=FeedbackReferenceKind.RESOLUTION_REQUEST,
            reference_id=f"resolution-{index}",
            kind=FeedbackObservationKind.VERDICT,
            outcome=FeedbackOutcome.ACCEPTED if index % 2 == 0 else FeedbackOutcome.REJECTED_CONTEXT,
            query_identity=extract_standalone_identity("What is the benchmark response?", scope),
            scope=scope,
            constraint_fingerprint=constraint_fingerprint("UNKNOWN", {}, ""),
            statement_id="statement-benchmark",
            generation=1,
            generation_available=True,
            policy_fingerprint=POLICY_FINGERPRINT,
            observed_at=NOW_TEXT,
        )
        internal_apply(store, f"feedback-{index}", verdict)
        ingestion.append((time_perf_counter_ns() - started) / 1_000_000)
    target_scope = scope_key(namespace="section6-benchmark")
    target_identity = extract_standalone_identity("What is the benchmark response?", target_scope)
    target_constraint = constraint_fingerprint("UNKNOWN", {}, "")
    history = []
    for _ in range(samples):
        _, elapsed = internal_time(
            lambda: store.history(
                target_identity,
                target_constraint,
                "statement-benchmark",
                1,
                POLICY_FINGERPRINT,
                NOW_TEXT,
            )
        )
        history.append(elapsed)

    state = store.snapshot()
    encoded = feedback_state_to_json(state)
    round_trip = []
    for _ in range(samples):
        _, elapsed = internal_time(lambda: feedback_state_from_json(encoded))
        round_trip.append(elapsed)

    scale_store = FeedbackStore()
    ingest_benchmark_verdicts(scale_store, scale_records, "scale")
    scale_started_ns = time_perf_counter_ns()
    scale_scope = scope_key(namespace="section6-benchmark")
    scale_verdict = feedback_observation(
        reference_kind=FeedbackReferenceKind.RESOLUTION_REQUEST,
        reference_id=f"resolution-{scale_records}",
        kind=FeedbackObservationKind.VERDICT,
        outcome=FeedbackOutcome.ACCEPTED if scale_records % 2 == 0 else FeedbackOutcome.REJECTED_CONTEXT,
        query_identity=extract_standalone_identity("What is the benchmark response?", scale_scope),
        scope=scale_scope,
        constraint_fingerprint=constraint_fingerprint("UNKNOWN", {}, ""),
        statement_id="scale-probe-statement",
        generation=1,
        generation_available=True,
        policy_fingerprint=POLICY_FINGERPRINT,
        observed_at=NOW_TEXT,
    )
    scale_probe = scale_store.prepare("scale-probe", (scale_verdict,))
    scale_prepare_ms = (time_perf_counter_ns() - scale_started_ns) / 1_000_000
    scale_state = scale_probe.get("after", {})
    scale_encoded = feedback_state_to_json(scale_state)
    _, scale_round_trip_ms = internal_time(lambda: feedback_state_from_json(scale_encoded))

    ordinary_engine = Engram()
    ordinary = EngramCore(ordinary_engine, clock=lambda: NOW)
    cached_engine = Engram()
    cached = EngramCore(cached_engine, clock=lambda: NOW)
    request = "Section six repeated benchmark miss"
    # An exact-only miss is admitted to the negative store, so repeating one
    # request on the ordinary core would time negative hits after its first
    # sample. Each ordinary sample therefore resolves a request no earlier
    # sample admitted, while the cached core repeats one primed request of the
    # same form. Every timed result's native attribution is checked.
    cached_request = f"{request} {samples}"
    cached.resolve_request(cached_request, "cached-prime", namespace="section6-benchmark", configured_resolvers=("exact",))
    ordinary_misses = []
    negative_hits = []
    for index in range(samples):
        outcome, elapsed = internal_time(
            lambda index=index: ordinary.resolve_request(
                f"{request} {index}",
                f"ordinary-{index}",
                namespace="section6-benchmark",
                configured_resolvers=("exact",),
            )
        )
        internal_require_miss(outcome, False)
        ordinary_misses.append(elapsed)
        outcome, elapsed = internal_time(
            lambda index=index: cached.resolve_request(
                cached_request,
                f"cached-{index}",
                namespace="section6-benchmark",
                configured_resolvers=("exact",),
            )
        )
        internal_require_miss(outcome, True)
        negative_hits.append(elapsed)

    tracemalloc_start()
    feedback_store = FeedbackStore()
    ingest_benchmark_verdicts(feedback_store, memory_records, "memory")
    _, feedback_peak = tracemalloc_get_traced_memory()
    del feedback_store
    tracemalloc_stop()

    tracemalloc_start()
    negatives = NegativeResolutionStore(max_records=memory_records, ttl_seconds=300)
    for index in range(memory_records):
        negative_scope = scope_key(namespace="section6-benchmark")
        negatives.admit(
            negative_resolution_key(
                query_identity=extract_standalone_identity(f"negative benchmark request {index}", negative_scope),
                scope=negative_scope,
                constraint_fingerprint=constraint_fingerprint("UNKNOWN", {}, ""),
                resolver_plan_fingerprint=canonical_fingerprint("exact-only"),
                capability_readiness_fingerprint=canonical_fingerprint("ready"),
                policy_fingerprint=POLICY_FINGERPRINT,
            ),
            NOW_TEXT,
        )
    _, negative_peak = tracemalloc_get_traced_memory()
    tracemalloc_stop()

    ordinary_latency = internal_latency(ordinary_misses)
    negative_latency = internal_latency(negative_hits)
    ingestion_latency = internal_latency(ingestion)
    history_latency = internal_latency(history)
    round_trip_latency = internal_latency(round_trip)
    negative_hit_p95_ratio = round(negative_latency.get("p95_ms", 0.0) / ordinary_latency.get("p95_ms", 0.0), 4)
    gates = {
        "feedback_peak_under_64_mib": feedback_peak < 64 * 1024 * 1024,
        "negative_peak_under_32_mib": negative_peak < 32 * 1024 * 1024,
        "scale_state_under_64_mib": len(scale_encoded.encode("utf-8")) < 64 * 1024 * 1024,
    }
    result = {
        "benchmark_id": "section6-feedback-negative",
        "recorded_at": recorded_at(),
        "evaluation_time": NOW_TEXT,
        "source": benchmark_source_state(),
        "provenance": "synthetic offline engineering regression; no formula or threshold was fitted from these samples",
        "environment": {
            "python": platform_python_version(),
            "platform": platform_platform(),
        },
        "parameters": {"samples": samples, "memory_records": memory_records, "scale_records": scale_records},
        "latency": {
            "feedback_ingestion": ingestion_latency,
            "feedback_history_lookup": history_latency,
            "feedback_state_json_round_trip": round_trip_latency,
            "ordinary_completed_exact_miss": ordinary_latency,
            "negative_resolution_hit": negative_latency,
            "negative_hit_p95_ratio": negative_hit_p95_ratio,
            "scale_feedback_prepare_ms": round(scale_prepare_ms, 4),
            "scale_feedback_state_round_trip_ms": round(scale_round_trip_ms, 4),
        },
        "timing_assessment": "reported observations; no pass/fail threshold",
        "serialization": {
            "empty_feedback_state_bytes": len(feedback_state_to_json(feedback_state()).encode("utf-8")),
            "populated_feedback_state_bytes": len(encoded.encode("utf-8")),
            "statement_records": len(state.get("statement_records", ())),
            "relationship_records": len(state.get("relationship_records", ())),
            "receipts": store.inspect().get("receipt_count", 0),
            "scale_feedback_state_bytes": len(scale_encoded.encode("utf-8")),
            "scale_statement_records": len(scale_state.get("statement_records", ())),
            "scale_relationship_records": len(scale_state.get("relationship_records", ())),
        },
        "memory": {
            "feedback_records": memory_records,
            "feedback_peak_bytes": feedback_peak,
            "negative_records": memory_records,
            "negative_peak_bytes": negative_peak,
        },
        "negative_inspection": cached.inspect_feedback_learning().get("negative_resolution", {}),
        "engineering_gates": gates,
        "all_engineering_gates_passed": all(gates.values()),
    }
    return result


def main() -> None:
    parser = argparse_ArgumentParser(description=__doc__)
    parser.add_argument("--samples", type=int, default=100)
    parser.add_argument("--memory-records", type=int, default=100)
    parser.add_argument("--scale-records", type=int, default=5_000)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    arguments = parser.parse_args()
    if arguments.samples < 10 or arguments.memory_records < 10 or not 1_000 <= arguments.scale_records <= 9_999:
        raise SystemExit("samples and memory-records must be at least 10; scale-records must be from 1,000 through 9,999")
    result = run_benchmark(arguments.samples, arguments.memory_records, arguments.scale_records)
    arguments.output.parent.mkdir(parents=True, exist_ok=True)
    arguments.output.write_text(json_dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json_dumps(result, indent=2, sort_keys=True))
    if not result.get("all_engineering_gates_passed", False):
        raise SystemExit("one or more Section 6 engineering gates failed")


if __name__ == "__main__":
    main()
