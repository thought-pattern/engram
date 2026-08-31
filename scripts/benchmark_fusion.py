"""Reproducible Section 5 fusion latency, scaling, memory, and acceptance benchmark."""

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

from engram.artifacts import LifecycleState
from engram.core import Engram
from engram.fusion import CandidateFusionEngine, fusion_policy, fusion_policy_to_dict, permissive_candidate_authority
from engram.identity import scope_key
from engram.resolution import (
    CandidateSource,
    EvidenceKind,
    QueryFrameBuilder,
    ResolutionOutcome,
    candidate as resolution_candidate,
    capture_resolution_budget,
    evidence_reference,
    feature_set,
)
from scripts.benchmark_metadata import benchmark_source_state, recorded_at

DEFAULT_OUTPUT = Path("eval/results/fusion/benchmark-2026-08-19.json")
START_NS = 1_000_000_000
NOW = datetime(2026, 8, 15, 12, 0, tzinfo=UTC)
SCOPE = scope_key(namespace="fusion-benchmark")


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
        "p50_ms": statistics_median(values),
        "p95_ms": percentile(values, 0.95),
        "p99_ms": percentile(values, 0.99),
        "max_ms": max(values),
    }
    return result


def candidate(
    statement_id: str,
    source: CandidateSource,
    features: dict[str, float],
    *,
    response: str = "Bounded benchmark response",
    evidence: tuple[dict, ...] = (),
) -> dict:
    """Build one content-neutral benchmark candidate."""
    result = resolution_candidate(
        candidate_id=f"candidate:{source.value}:{statement_id}",
        statement_id=statement_id,
        response=response,
        source=source,
        features=feature_set(values=features),
        evidence=evidence,
        scope=SCOPE,
        lifecycle=LifecycleState.ACTIVE,
    )
    return result


def supported_pair(statement_id: str, semantic: float = 0.92, lexical: float = 0.95) -> tuple[dict, dict]:
    """Build independent sparse and support-semantic contributions."""
    support = evidence_reference(
        f"proposition:{statement_id}",
        "support_semantic",
        EvidenceKind.SUPPORT,
        SCOPE,
    )
    result = (
        candidate(statement_id, CandidateSource.SPARSE, {"sparse_score": lexical}),
        candidate(
            statement_id,
            CandidateSource.SUPPORT_SEMANTIC,
            {"semantic_score": semantic, "support_coverage": 1.0},
            evidence=(support,),
        ),
    )
    return result


def measure_fusion(samples: int) -> dict[str, object]:
    """Run warmed benchmarks and return the evidence artifact."""
    engine = Engram()
    budget = capture_resolution_budget(lambda: START_NS)
    frame = QueryFrameBuilder(engine, lambda: START_NS, lambda: NOW).build(
        "Which benchmark response is supported?",
        SCOPE,
        diagnostic_seed="fusion-benchmark",
        budget=budget,
    )
    fusion = CandidateFusionEngine(authority=permissive_candidate_authority)
    exact = (candidate("exact", CandidateSource.EXACT, {"exact_match": 1.0}),)
    reinforced = supported_pair("reinforced")
    ambiguous = (*supported_pair("ambiguous-a"), *supported_pair("ambiguous-b", semantic=0.91, lexical=0.94))
    unsupported = (
        candidate("unsupported", CandidateSource.SPARSE, {"sparse_score": 0.98}),
        candidate("unsupported", CandidateSource.STANDALONE_SEMANTIC, {"semantic_score": 0.98}),
    )
    noise = (candidate("noise", CandidateSource.SPARSE, {"sparse_score": 0.2}),)
    scenarios = {
        "exact": (exact, "ANSWER"),
        "reinforced": (reinforced, "ANSWER"),
        "ambiguous": (ambiguous, "EVIDENCE"),
        "unsupported": (unsupported, "EVIDENCE"),
        "noise": (noise, "MISS"),
        "empty": ((), "MISS"),
    }
    acceptance = []
    for name, (values, expected) in scenarios.items():
        acceptance.append(
            {
                "scenario": name,
                "expected": expected,
                "outcome": fusion.decide(frame, values).get("outcome", ResolutionOutcome.MISS).value,
            }
        )
    scaling = {}
    for count, selected_samples in ((1, samples), (10, samples), (100, max(20, samples // 2)), (1_000, max(10, samples // 10))):
        values = tuple(
            candidate(f"noise-{index:04d}", CandidateSource.SPARSE, {"sparse_score": 0.1 + index % 20 / 100})
            for index in range(count)
        )
        scaling[str(count)] = {
            "samples": selected_samples,
            **measure(lambda values=values: fusion.decide(frame, values), selected_samples),
        }
    tracemalloc_start()
    peak_values = tuple(candidate(f"memory-{index:04d}", CandidateSource.SPARSE, {"sparse_score": 0.2}) for index in range(1_000))
    peak_decision = fusion.decide(frame, peak_values)
    _, peak_bytes = tracemalloc_get_traced_memory()
    tracemalloc_stop()
    reinforced_latency = measure(lambda: fusion.decide(frame, reinforced), samples)
    gates = {
        "thousand_candidates_peak_under_64_mib": peak_bytes < 67_108_864,
        "thousand_candidates_estimate_within_frame_budget": (
            peak_decision.get("working_memory_bytes", 0)
            <= frame.get("budget", {}).get("max_working_memory_bytes", 0)
        ),
        "acceptance_scenarios_correct": all(
            value.get("outcome", "") == value.get("expected", "") for value in acceptance
        ),
    }
    result = {
        "schema_version": 1,
        "benchmark_version": "fusion-benchmark-current-1",
        "recorded_at": recorded_at(),
        "source": benchmark_source_state(),
        "environment": {"python": platform_python_version(), "platform": platform_platform()},
        "policy": fusion_policy_to_dict(fusion_policy()),
        "policy_provenance": {
            "selection": "hand_authored_conservative_unfitted",
            "unit_or_conformance_fixtures_used_for_parameter_selection": False,
            "empirical_calibration_owner": "release qualification",
            "fixture_authority": "explicit_permissive_conformance_only",
        },
        "reinforced_pair": reinforced_latency,
        "timing_assessment": "reported observations; no pass/fail threshold",
        "acceptance": acceptance,
        "scaling": scaling,
        "thousand_candidates_peak_bytes": peak_bytes,
        "thousand_candidates_fusion_estimated_bytes": peak_decision.get("working_memory_bytes", 0),
        "gates": gates,
        "all_gates_passed": all(gates.values()),
    }
    return result


def main() -> int:
    """Run the benchmark and optionally write its JSON artifact."""
    parser = argparse_ArgumentParser(description=__doc__)
    parser.add_argument("--samples", type=int, default=200)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--stdout", action="store_true")
    args = parser.parse_args()
    if args.samples < 10:
        parser.error("--samples must be at least 10")
    result = measure_fusion(args.samples)
    text = json_dumps(result, indent=2, sort_keys=True) + "\n"
    if args.stdout:
        print(text, end="")
    else:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text, encoding="utf-8")
        print(args.output)
    result = 0 if result.get("all_gates_passed", False) else 1
    return result


if __name__ == "__main__":
    raise SystemExit(main())
