"""Reproducible offline benchmark for the remediated Section 1 identity path."""

from argparse import ArgumentParser as argparse_ArgumentParser
from datetime import UTC, datetime
from json import dumps as json_dumps
from pathlib import Path
from platform import platform as platform_platform, processor as platform_processor, python_version as platform_python_version
from sys import argv as sys_argv, path as sys_path
from time import perf_counter_ns as time_perf_counter_ns
from tracemalloc import get_traced_memory as tracemalloc_get_traced_memory, start as tracemalloc_start, stop as tracemalloc_stop

REPOSITORY = Path(__file__).resolve().parents[1]
if str(REPOSITORY) not in sys_path:
    sys_path.insert(0, str(REPOSITORY))

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


def internal_percentile(samples: list[float], fraction: float) -> float:
    ordered = sorted(samples)
    index = min(len(ordered) - 1, max(0, int(round((len(ordered) - 1) * fraction))))
    result = ordered[index]
    return result


def internal_measure(operation: object, iterations: int) -> dict:
    if not callable(operation):
        raise ValueError("benchmark operation must be callable")
    samples = []
    for _ in range(iterations):
        started = time_perf_counter_ns()
        operation()
        samples.append((time_perf_counter_ns() - started) / 1_000_000)
    result = {
        "iterations": iterations,
        "minimum_ms": round(min(samples), 6),
        "p50_ms": round(internal_percentile(samples, 0.50), 6),
        "p95_ms": round(internal_percentile(samples, 0.95), 6),
        "p99_ms": round(internal_percentile(samples, 0.99), 6),
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

    normalization = internal_measure(lambda: normalize_retrieval_key(next_request()), iterations)
    identity = internal_measure(lambda: build_standalone_identity(next_request(), scope), iterations)
    scoped_key = internal_measure(lambda: build_scoped_retrieval_key(scope, next_request()), iterations)

    tracemalloc_start()
    identities = [build_standalone_identity(REQUESTS[index % len(REQUESTS)], scope) for index in range(memory_objects)]
    current, peak = tracemalloc_get_traced_memory()
    tracemalloc_stop()
    assert len(identities) == memory_objects

    result = {
        "artifact_schema_version": 1,
        "captured_at": datetime.now(UTC).isoformat(),
        "source": benchmark_source_state(),
        "environment": {
            "python": platform_python_version(),
            "platform": platform_platform(),
            "processor": platform_processor(),
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


def main(argv: tuple[str, ...] = ()) -> int:
    parser = argparse_ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--iterations", type=int, default=10000)
    parser.add_argument("--memory-objects", type=int, default=1000)
    args = parser.parse_args(argv)
    if args.iterations < 100:
        raise ValueError("iterations must be at least 100")
    if args.memory_objects < 1:
        raise ValueError("memory_objects must be positive")
    result = run_benchmark(args.iterations, args.memory_objects)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json_dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(args.output)
    result = 0
    return result


if __name__ == "__main__":
    raise SystemExit(main(tuple(sys_argv[1:])))
