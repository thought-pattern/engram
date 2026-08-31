"""Reproducible offline benchmark for the remediated Section 1 identity path."""

import argparse
import json
import platform
import sys
import time
import tracemalloc
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path

REPOSITORY = Path(__file__).resolve().parents[1]
if str(REPOSITORY) not in sys.path:
    sys.path.insert(0, str(REPOSITORY))

from engram.identity import build_scoped_retrieval_key, build_standalone_identity, normalize_retrieval_key, scope_key
from scripts.benchmark_metadata import benchmark_source_state

DEFAULT_OUTPUT = Path("eval/results/identity/benchmark-2026-08-19.json")
REQUESTS = (
    "When was Ada Lovelace born?",
    "Where was Ada Lovelace born?",
    "Is latency < 100 ms?",
    "Is latency > 100 ms?",
    "Does version == 1.2?",
    "Does version != 1.2?",
    "What features does Engram support in v1?",
    "Read config/engram.yml and evaluate A|B at load=75%.",
)


def _percentile(samples: list[float], fraction: float) -> float:
    ordered = sorted(samples)
    index = min(len(ordered) - 1, max(0, int(round((len(ordered) - 1) * fraction))))
    result = ordered[index]
    return result


def _measure(operation: Callable[[], object], iterations: int) -> dict:
    samples = []
    for _ in range(iterations):
        started = time.perf_counter_ns()
        operation()
        samples.append((time.perf_counter_ns() - started) / 1_000_000)
    result = {
        "iterations": iterations,
        "minimum_ms": round(min(samples), 6),
        "p50_ms": round(_percentile(samples, 0.50), 6),
        "p95_ms": round(_percentile(samples, 0.95), 6),
        "p99_ms": round(_percentile(samples, 0.99), 6),
        "maximum_ms": round(max(samples), 6),
    }
    return result


def run_benchmark(iterations: int, memory_objects: int) -> dict:
    sequence = [0]
    scope = scope_key("benchmark", "identity-v1")

    def next_request() -> str:
        request = REQUESTS[sequence[0] % len(REQUESTS)]
        sequence[0] += 1
        return request

    normalization = _measure(lambda: normalize_retrieval_key(next_request()), iterations)
    identity = _measure(lambda: build_standalone_identity(next_request(), scope), iterations)
    scoped_key = _measure(lambda: build_scoped_retrieval_key(scope, next_request()), iterations)

    tracemalloc.start()
    identities = [build_standalone_identity(REQUESTS[index % len(REQUESTS)], scope) for index in range(memory_objects)]
    current, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    assert len(identities) == memory_objects

    result = {
        "artifact_schema_version": 1,
        "captured_at": datetime.now(UTC).isoformat(),
        "source": benchmark_source_state(),
        "environment": {
            "python": platform.python_version(),
            "platform": platform.platform(),
            "processor": platform.processor(),
        },
        "method": {
            "clock": "time.perf_counter_ns",
            "latency_unit": "milliseconds",
            "percentiles": "nearest observed rank over per-call samples",
            "request_count": len(REQUESTS),
            "iterations": iterations,
            "memory_objects": memory_objects,
            "external_services": "none",
        },
        "normalization": normalization,
        "standalone_identity": identity,
        "scoped_key": scoped_key,
        "identity_memory": {"current_bytes": current, "peak_bytes": peak},
    }
    return result


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--iterations", type=int, default=10000)
    parser.add_argument("--memory-objects", type=int, default=1000)
    return parser


def main(argv: Sequence[str] = ()) -> int:
    args = _parser().parse_args(argv)
    if args.iterations < 100:
        raise ValueError("iterations must be at least 100")
    if args.memory_objects < 1:
        raise ValueError("memory_objects must be positive")
    result = run_benchmark(args.iterations, args.memory_objects)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(args.output)
    result = 0
    return result


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
