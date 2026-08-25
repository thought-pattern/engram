"""Reproducible Section 5 fusion latency, scaling, memory, and acceptance benchmark."""

import argparse
import json
import platform
import statistics
import sys
import time
import tracemalloc
from datetime import UTC, datetime
from pathlib import Path

REPOSITORY = Path(__file__).resolve().parent.parent
if str(REPOSITORY) not in sys.path:
    sys.path.insert(0, str(REPOSITORY))

from engram.artifacts import LifecycleState
from engram.core import Engram
from engram.fusion import CandidateFusionEngine, fusion_policy, fusion_policy_to_dict, permissive_candidate_authority
from engram.identity import scope_key
from engram.resolution import (
    Candidate,
    CandidateSource,
    EvidenceKind,
    EvidenceReference,
    QueryFrameBuilder,
    ResolutionOutcome,
    candidate as resolution_candidate,
    capture_resolution_budget,
    evidence_reference,
    feature_set,
)
from scripts.benchmark_metadata import benchmark_source_state, recorded_at

DEFAULT_OUTPUT = Path("documentation/fusion/benchmark-2026-08-19.json")
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
        started = time.perf_counter_ns()
        operation()
        values.append((time.perf_counter_ns() - started) / 1_000_000)
    result = {
        "p50_ms": statistics.median(values),
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
    evidence: tuple[EvidenceReference, ...] = (),
) -> Candidate:
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


def supported_pair(statement_id: str, semantic: float = 0.92, lexical: float = 0.95) -> tuple[Candidate, Candidate]:
    """Build independent lexical and support-semantic contributions."""
    support = evidence_reference(
        f"claim:{statement_id}",
        "support_semantic",
        EvidenceKind.SUPPORT,
        SCOPE,
    )
    result = (
        candidate(statement_id, CandidateSource.LEXICAL, {"lexical_score": lexical, "recency": 0.9}),
        candidate(
            statement_id,
            CandidateSource.SUPPORT_SEMANTIC,
            {"semantic_score": semantic, "support_coverage": 1.0},
            evidence=(support,),
        ),
    )
    return result


def section4_baseline(candidates: tuple[Candidate, ...]) -> ResolutionOutcome:
    """Represent the prior exact-only direct-selection rule without execution cost."""
    unique = {}
    for value in candidates:
        unique.setdefault(value["statement_id"], value)
    exact = tuple(value for value in unique.values() if value["source"] == CandidateSource.EXACT)
    if len(exact) == 1:
        result = ResolutionOutcome.ANSWER
        return result
    if unique:
        result = ResolutionOutcome.EVIDENCE
        return result
    result = ResolutionOutcome.MISS
    return result


def build_result(samples: int) -> dict[str, object]:
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
        candidate("unsupported", CandidateSource.LEXICAL, {"lexical_score": 0.98}),
        candidate("unsupported", CandidateSource.PATTERN, {"pattern_specificity": 20.0}),
    )
    noise = (candidate("noise", CandidateSource.LEXICAL, {"lexical_score": 0.2}),)
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
                "section4_baseline": section4_baseline(values).value,
                "section5_fusion_v1": fusion.decide(frame, values)["outcome"].value,
            }
        )
    scaling = {}
    for count, selected_samples in ((1, samples), (10, samples), (100, max(20, samples // 2)), (1_000, max(10, samples // 10))):
        values = tuple(
            candidate(f"noise-{index:04d}", CandidateSource.LEXICAL, {"lexical_score": 0.1 + index % 20 / 100})
            for index in range(count)
        )
        scaling[str(count)] = {
            "samples": selected_samples,
            **measure(lambda values=values: fusion.decide(frame, values), selected_samples),
        }
    tracemalloc.start()
    peak_values = tuple(candidate(f"memory-{index:04d}", CandidateSource.LEXICAL, {"lexical_score": 0.2}) for index in range(1_000))
    peak_decision = fusion.decide(frame, peak_values)
    _, peak_bytes = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    before = measure(lambda: section4_baseline(reinforced), samples)
    after = measure(lambda: fusion.decide(frame, reinforced), samples)
    gates = {
        "thousand_candidates_peak_under_64_mib": peak_bytes < 67_108_864,
        "thousand_candidates_estimate_within_frame_budget": (
            peak_decision["working_memory_bytes"] <= frame["budget"]["max_working_memory_bytes"]
        ),
        "acceptance_scenarios_correct": all(value["section5_fusion_v1"] == value["expected"] for value in acceptance),
    }
    result = {
        "schema_version": 1,
        "benchmark_version": "section5-fusion-benchmark-v1.1",
        "recorded_at": recorded_at(),
        "source": benchmark_source_state(),
        "environment": {"python": platform.python_version(), "platform": platform.platform()},
        "policy": fusion_policy_to_dict(fusion_policy()),
        "policy_provenance": {
            "selection": "hand_authored_conservative_unfitted",
            "unit_or_conformance_fixtures_used_for_parameter_selection": False,
            "empirical_calibration_owner": "Section 16",
            "fixture_authority": "explicit_permissive_conformance_only",
        },
        "before_after_reinforced_pair": {"section4_baseline": before, "section5_fusion_v1": after},
        "timing_assessment": "reported observations; no pass/fail threshold",
        "acceptance": acceptance,
        "scaling": scaling,
        "thousand_candidates_peak_bytes": peak_bytes,
        "thousand_candidates_fusion_estimated_bytes": peak_decision["working_memory_bytes"],
        "gates": gates,
        "all_gates_passed": all(gates.values()),
    }
    return result


def main() -> int:
    """Run the benchmark and optionally write its JSON artifact."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--samples", type=int, default=200)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--stdout", action="store_true")
    args = parser.parse_args()
    if args.samples < 10:
        parser.error("--samples must be at least 10")
    result = build_result(args.samples)
    text = json.dumps(result, indent=2, sort_keys=True) + "\n"
    if args.stdout:
        print(text, end="")
    else:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text, encoding="utf-8")
        print(args.output)
    result = 0 if result["all_gates_passed"] else 1
    return result


if __name__ == "__main__":
    raise SystemExit(main())
