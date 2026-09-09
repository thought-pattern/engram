"""Reproducible offline benchmark for the Section 4 resolution substrate."""

from argparse import ArgumentParser as argparse_ArgumentParser
from datetime import UTC, datetime
from json import dumps as json_dumps
from pathlib import Path
from statistics import median as statistics_median
from sys import path as sys_path
from time import perf_counter_ns as time_perf_counter_ns
from tracemalloc import get_traced_memory as tracemalloc_get_traced_memory, start as tracemalloc_start, stop as tracemalloc_stop

REPOSITORY = Path(__file__).resolve().parent.parent
if str(REPOSITORY) not in sys_path:
    sys_path.insert(0, str(REPOSITORY))

from engram.artifacts import LifecycleState, artifact_provenance, artifact_statistics, cached_response_artifact
from engram.constants import Tier
from engram.core import Engram
from engram.identity import build_retrieval_representation, build_standalone_identity, scope_key
from engram.repository import ArtifactRepository
from engram.resolution import (
    CostClass,
    QueryFrameBuilder,
    ResolverState,
    budget_consumption,
    resolver_result,
    resolver_result_from_json,
    resolver_result_to_json,
)
from engram.resolvers import ExactResolver, ResolverExecutor, ResolverRegistry, SparseResolver, resolver_budget
from scripts.benchmark_metadata import benchmark_source_state

DEFAULT_OUTPUT = Path("eval/results/artifacts/section4-benchmark-2026-08-19.json")
START_NS = 1_000_000_000
NOW = datetime(2026, 8, 12, 18, 0, tzinfo=UTC)


def percentile(values: list[float], fraction: float) -> float:
    """Return a deterministic nearest-rank percentile."""
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(len(ordered) * fraction + 0.999999) - 1))
    result = ordered[index]
    return result


def measure(operation, samples: int) -> dict[str, float]:
    """Measure one warmed operation in milliseconds."""
    operation()
    values = []
    for _ in range(samples):
        started = time_perf_counter_ns()
        operation()
        values.append((time_perf_counter_ns() - started) / 1_000_000)
    result = {
        "median_ms": statistics_median(values),
        "p95_ms": percentile(values, 0.95),
        "p99_ms": percentile(values, 0.99),
        "max_ms": max(values),
    }
    return result


def accepted_artifact() -> dict:
    """Build one exact artifact without external dependencies."""
    scope = scope_key(namespace="benchmark")
    request = "What is the Section 4 benchmark answer?"
    artifact = cached_response_artifact(
        statement_id="stmt-resolution-benchmark",
        generation=1,
        response="This is the exact Section 4 benchmark answer.",
        query_identity=build_standalone_identity(request, scope),
        retrieval=build_retrieval_representation(request, ("Explain the Section 4 benchmark answer",)),
        tier=Tier.STATIC,
        lifecycle=LifecycleState.ACTIVE,
        scope=scope,
        support_references=(),
        valid_from="",
        valid_from_available=False,
        valid_until="",
        valid_until_available=False,
        superseded_by="",
        provenance=artifact_provenance("benchmark", "section4", "2026-08-12T16:00:00Z"),
        statistics=artifact_statistics(),
        metadata={},
    )
    return artifact


class BenchmarkResolver:
    """Configured benchmark resolver that records invocations across calls."""

    cost_class = CostClass.CHEAP

    def __init__(self, name: str, fail: bool = False) -> None:
        self.name = name
        self.fail = fail
        self.calls = 0

    def available(self, frame) -> bool:
        del frame
        result = True
        return result

    def resolve(self, frame, budget: dict, cooperative_check=()) -> dict:
        del frame
        if cooperative_check:
            cooperative_check()
        self.calls += 1
        if self.fail:
            raise RuntimeError("benchmark failure")
        result = resolver_result(
            resolver=self.name,
            state=ResolverState.COMPLETED,
            consumption=budget_consumption(resolvers=1),
        )
        return result


def run_benchmark(samples: int, corpus_size: int) -> dict[str, object]:
    """Report operation lengths and evaluate the retained memory bound."""
    exact_engine = Engram()
    exact_engine.response_repository = ArtifactRepository((accepted_artifact(),))
    builder = QueryFrameBuilder(exact_engine, lambda: START_NS, lambda: NOW)
    scope = scope_key(namespace="benchmark")

    def build_frame():
        result = builder.build("Explain the Section 4 benchmark answer", scope, diagnostic_seed="benchmark")
        return result

    exact_frame = build_frame()
    exact_budget = resolver_budget(
        max_candidates=exact_frame["budget"]["max_candidates"],
        max_graph_rows=exact_frame["budget"]["max_graph_rows"],
        max_vector_results=exact_frame["budget"]["max_vector_results"],
        max_evidence=exact_frame["budget"]["max_evidence"],
        max_evidence_bytes=exact_frame["budget"]["max_evidence_bytes"],
        max_output_bytes=exact_frame["budget"]["max_output_bytes"],
        max_diagnostic_bytes=exact_frame["budget"]["max_diagnostic_bytes"],
        max_working_memory_bytes=exact_frame["budget"]["max_working_memory_bytes"],
    )
    exact_resolver = ExactResolver(exact_engine, lambda: START_NS)

    lexical_engine = Engram()
    lexical_artifacts = []
    for index in range(corpus_size):
        request = f"section four benchmark topic {index} shared retrieval token"
        lexical_artifacts.append(
            cached_response_artifact(
                statement_id=f"stmt-sparse-benchmark-{index}",
                generation=1,
                response=f"Section 4 sparse benchmark response {index}",
                query_identity=build_standalone_identity(request, scope),
                retrieval=build_retrieval_representation(request),
                tier=Tier.STATIC,
                lifecycle=LifecycleState.ACTIVE,
                scope=scope,
                support_references=(),
                valid_from="",
                valid_from_available=False,
                valid_until="",
                valid_until_available=False,
                superseded_by="",
                provenance=artifact_provenance("benchmark", "section4", "2026-08-12T16:00:00Z"),
                statistics=artifact_statistics(),
                metadata={},
            )
        )
    lexical_engine.response_repository = ArtifactRepository(tuple(lexical_artifacts))
    lexical_frame = QueryFrameBuilder(lexical_engine, lambda: START_NS, lambda: NOW).build(
        "benchmark topic shared retrieval",
        diagnostic_seed="lexical-benchmark",
    )
    lexical_resolver = SparseResolver(lexical_engine, lambda: START_NS)
    lexical_budget = resolver_budget(
        max_candidates=10,
        max_graph_rows=0,
        max_vector_results=0,
        max_evidence=0,
        max_evidence_bytes=0,
        max_output_bytes=lexical_frame["budget"]["max_output_bytes"],
        max_diagnostic_bytes=lexical_frame["budget"]["max_diagnostic_bytes"],
        max_working_memory_bytes=lexical_frame["budget"]["max_working_memory_bytes"],
    )
    completed_plan = ResolverRegistry((BenchmarkResolver("completed"),)).plan(lexical_frame)
    failed_plan = ResolverRegistry((BenchmarkResolver("failed", fail=True),)).plan(lexical_frame)
    executor = ResolverExecutor(lambda: START_NS)
    exact_result = exact_resolver.resolve(exact_frame, exact_budget)

    measurements = {
        "frame_build": measure(build_frame, samples),
        "exact_adapter": measure(lambda: exact_resolver.resolve(exact_frame, exact_budget), samples),
        "sparse_adapter": measure(lambda: lexical_resolver.resolve(lexical_frame, lexical_budget), samples),
        "executor_completed": measure(lambda: executor.execute(lexical_frame, completed_plan), samples),
        "executor_failed": measure(lambda: executor.execute(lexical_frame, failed_plan), samples),
        "result_codec": measure(lambda: resolver_result_from_json(resolver_result_to_json(exact_result)), samples),
    }

    tracemalloc_start()
    for _ in range(min(samples, 100)):
        lexical_resolver.resolve(lexical_frame, lexical_budget)
    _, peak_bytes = tracemalloc_get_traced_memory()
    tracemalloc_stop()

    limits = {"peak_traced_bytes": 16_777_216}
    gates = {
        "peak_traced_memory": peak_bytes <= limits.get("peak_traced_bytes", 0),
    }
    result = {
        "schema_version": 1,
        "recorded_at": datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "source": benchmark_source_state(),
        "samples": samples,
        "corpus_size": corpus_size,
        "measurements": measurements,
        "timing_assessment": "reported observations; no pass/fail threshold",
        "peak_traced_bytes": peak_bytes,
        "limits": limits,
        "gates": gates,
        "passed": all(gates.values()),
    }
    return result


def main() -> int:
    parser = argparse_ArgumentParser(description=__doc__)
    parser.add_argument("--samples", type=int, default=200)
    parser.add_argument("--corpus-size", type=int, default=1_000)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    arguments = parser.parse_args()
    if arguments.samples < 10 or arguments.corpus_size < 100:
        raise ValueError("samples must be at least 10 and corpus-size at least 100")
    result = run_benchmark(arguments.samples, arguments.corpus_size)
    arguments.output.parent.mkdir(parents=True, exist_ok=True)
    arguments.output.write_text(json_dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json_dumps(result, indent=2, sort_keys=True))
    result = 0 if result["passed"] else 1
    return result


if __name__ == "__main__":
    raise SystemExit(main())
