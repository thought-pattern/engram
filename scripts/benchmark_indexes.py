"""Reproducible offline benchmark for Section 2 derived indexes."""

import argparse
import gc
import json
import platform
import sys
import time
import tracemalloc
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

REPOSITORY = Path(__file__).resolve().parents[1]
if str(REPOSITORY) not in sys.path:
    sys.path.insert(0, str(REPOSITORY))

from engram.core import Engram
from engram.identity import RetrievalRepresentation, ScopeKey
from engram.indexes import (
    MAX_INDEX_LOOKUP_OWNERS,
    ExactLookupOutcome,
    IndexOwner,
    IndexProjection,
    IndexState,
    add_index_projection,
    build_index_state,
    check_index_state,
    projection_from_statement,
    remove_index_projection,
    replace_index_projection,
    update_index_support,
)
from engram.models import statement
from engram.service import EngramCore

DEFAULT_OUTPUT = REPOSITORY / "documentation" / "indexes" / "benchmark-2026-08-12.json"
BASELINE_INPUT = REPOSITORY / "documentation" / "baseline" / "benchmark-2026-08-11.json"
EXACT_CORPUS_SIZES = (10_000, 100_000)
SUPPORT_FANOUTS = (1, 10, 100)
FULL_PROPOSAL_CORPUS_SIZE = 5_000
FULL_PROPOSAL_LIMIT = 10
REBUILD_CORPUS_SIZE = 5_000
ADR_EXACT_P95_MS = 5.0
ADR_EXACT_SLOPE = 1.5
ADR_REBUILD_P95_MS = 1_000.0
ADR_SUPPORT_P95_MS = 30.0
ADR_BUILD_MEMORY_BYTES = 11 * 1024 * 1024


def _percentile(samples: list[float], fraction: float) -> float:
    ordered = sorted(samples)
    index = min(len(ordered) - 1, max(0, int(round((len(ordered) - 1) * fraction))))
    return ordered[index]


def _measure(operation: Callable[[], object], samples: int, batch_size: int = 1) -> dict[str, object]:
    for _ in range(3):
        operation()
    measurements = []
    for _ in range(samples):
        started = time.perf_counter_ns()
        for _ in range(batch_size):
            operation()
        elapsed_ms = (time.perf_counter_ns() - started) / 1_000_000 / batch_size
        measurements.append(elapsed_ms)
    return {
        "samples": samples,
        "batch_size": batch_size,
        "minimum_ms": round(min(measurements), 6),
        "p50_ms": round(_percentile(measurements, 0.50), 6),
        "p95_ms": round(_percentile(measurements, 0.95), 6),
        "maximum_ms": round(max(measurements), 6),
    }


def _projection(index: int, support: tuple[str, ...] = ()) -> IndexProjection:
    scope = ScopeKey(namespace="benchmark", context_fingerprint="indexes-v1")
    retrieval = RetrievalRepresentation(f"synthetic exact request {index}")
    return IndexProjection(
        statement_id=f"stmt-{index:06d}",
        generation=1,
        retrieval_keys=retrieval.bindings(scope),
        support_claim_ids=support,
        direct_answer_eligible=True,
        exclusion_reason="",
    )


def _projections(count: int) -> tuple[IndexProjection, ...]:
    values = []
    for index in range(count):
        support = []
        for fanout in SUPPORT_FANOUTS:
            if index < fanout:
                support.append(f"fanout-{fanout}")
        values.append(_projection(index, tuple(support)))
    return tuple(values)


def _full_proposal_result(support_fanout: int, samples: int) -> dict[str, object]:
    """Measure the complete regulated proposal path over an off-live-built index."""
    claim_id = "claim-section2-remediation"
    engram = Engram()
    statements = []
    for index in range(FULL_PROPOSAL_CORPUS_SIZE):
        support_id = claim_id if index >= FULL_PROPOSAL_CORPUS_SIZE - support_fanout else f"noise-claim-{index}"
        statements.append(
            statement(
                f"Supported response {index}.",
                statement_id=f"proposal-stmt-{index:05d}",
                source_label="section2:benchmark",
                template={
                    "tapestry": {
                        "namespace": "benchmark",
                        "context_fingerprint": "indexes-v1-remediated",
                        "support": [{"claim_id": support_id}],
                    }
                },
            )
        )
    benchmark_engram = cast(Any, engram)
    benchmark_engram.statements = statements
    benchmark_engram.statement_index = {item["id"]: index for index, item in enumerate(statements)}
    benchmark_engram._index_owner = IndexOwner(tuple(projection_from_statement(item) for item in statements))
    benchmark_engram.config["graph"] = {"vector_weight": 0.65, "vector_support_scan_limit": 100_000}
    benchmark_engram.graph_vector_claims = lambda text, *, limit=0: [{"claim_id": claim_id, "similarity": 0.8}]
    core = EngramCore(engram, checkpoint_on_mutation=False)
    sequence = [0]
    candidate_counts = []
    candidate_support_valid = []
    supported_ids = {item["id"] for item in statements[-support_fanout:]}

    def propose() -> object:
        request_id = f"proposal-{support_fanout}-{sequence[0]}"
        sequence[0] += 1
        result = core.propose(
            "Semantic probe with orthogonal vocabulary",
            request_id=request_id,
            namespace="benchmark",
            context_fingerprint="indexes-v1-remediated",
            limit=FULL_PROPOSAL_LIMIT,
        )
        candidate_ids = {candidate["statement_id"] for candidate in result["candidates"]}
        candidate_counts.append(len(candidate_ids))
        candidate_support_valid.append(candidate_ids.issubset(supported_ids))
        return result

    measurement = _measure(propose, samples)
    expected_count = min(support_fanout, FULL_PROPOSAL_LIMIT)
    return {
        "corpus_size": FULL_PROPOSAL_CORPUS_SIZE,
        "support_fanout": support_fanout,
        "candidate_limit": FULL_PROPOSAL_LIMIT,
        "candidate_count": expected_count,
        "candidate_count_stable": set(candidate_counts) == {expected_count},
        "all_candidates_supported": all(candidate_support_valid),
        "proposal": measurement,
    }


def _peak_build_memory(projections: tuple[IndexProjection, ...]) -> tuple[IndexState, dict[str, int]]:
    gc.collect()
    tracemalloc.start()
    state = build_index_state(projections)
    current, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    return state, {"current_bytes": current, "peak_bytes": peak}


def _latency(result: dict[str, object], name: str) -> float:
    value = result[name]
    if not isinstance(value, (int, float)):
        raise ValueError(f"benchmark result {name} must be numeric")
    return float(value)


def _baseline_support_p95() -> dict[int, float]:
    baseline = json.loads(BASELINE_INPUT.read_text(encoding="utf-8"))
    results = {}
    for item in baseline["support_vector"]:
        if item["corpus_size"] == FULL_PROPOSAL_CORPUS_SIZE and item["support_fanout"] in SUPPORT_FANOUTS:
            results[int(item["support_fanout"])] = float(item["proposal"]["p95_ms"])
    if set(results) != set(SUPPORT_FANOUTS):
        raise ValueError("baseline support proposal artifact does not contain every required fan-out")
    return results


def run_benchmark(samples: int, lookup_batch_size: int) -> dict[str, object]:
    rebuild_projections = _projections(REBUILD_CORPUS_SIZE)
    rebuild_state, memory = _peak_build_memory(rebuild_projections)
    rebuild = _measure(lambda: build_index_state(rebuild_projections), samples)

    replacement = _projection(REBUILD_CORPUS_SIZE - 1, ("replacement-claim",))
    addition = _projection(REBUILD_CORPUS_SIZE, ("addition-claim",))
    mutations = {
        "add": _measure(lambda: add_index_projection(rebuild_state, addition), samples),
        "replace": _measure(lambda: replace_index_projection(rebuild_state, replacement), samples),
        "remove": _measure(lambda: remove_index_projection(rebuild_state, replacement.statement_id), samples),
        "support_update": _measure(
            lambda: update_index_support(rebuild_state, replacement.statement_id, ("updated-claim",)),
            samples,
        ),
    }

    exact_results: dict[str, dict[str, object]] = {}
    support_results: dict[str, dict[str, object]] = {}
    correctness = []
    largest_state = rebuild_state
    for corpus_size in EXACT_CORPUS_SIZES:
        corpus = _projections(corpus_size)
        state = build_index_state(corpus)
        key = corpus[-1].retrieval_keys[0].key
        exact_results[str(corpus_size)] = _measure(
            lambda state=state, key=key: state.exact_lookup(key),
            samples,
            lookup_batch_size,
        )
        correctness.append(state.exact_lookup(key).outcome == ExactLookupOutcome.FOUND)
        if corpus_size == EXACT_CORPUS_SIZES[-1]:
            largest_state = state
            for fanout in SUPPORT_FANOUTS:
                claim_id = f"fanout-{fanout}"
                support_results[str(fanout)] = _measure(
                    lambda claim_id=claim_id, state=state: state.support_lookup((claim_id,)),
                    samples,
                    lookup_batch_size,
                )
                correctness.append(len(state.support_lookup((claim_id,)).matches) == fanout)

    remediation_state = build_index_state(
        tuple(_projection(index, ("remediation-fanout",)) for index in range(MAX_INDEX_LOOKUP_OWNERS + 1))
    )
    remediation_lookup = remediation_state.support_lookup(("remediation-fanout",))
    scan_exhausted = remediation_state.support_lookup(
        ("remediation-fanout",),
        scan_limit=MAX_INDEX_LOOKUP_OWNERS,
    )
    full_proposal = [_full_proposal_result(fanout, samples) for fanout in SUPPORT_FANOUTS]
    baseline_support = _baseline_support_p95()
    support_proposal_comparison = {}
    for result in full_proposal:
        fanout_value = result["support_fanout"]
        if isinstance(fanout_value, bool) or not isinstance(fanout_value, int):
            raise ValueError("full proposal support_fanout must be an integer")
        fanout = fanout_value
        proposal = result["proposal"]
        if not isinstance(proposal, dict):
            raise ValueError("full proposal benchmark result must be an object")
        observed_p95 = _latency(proposal, "p95_ms")
        relative_limit = baseline_support[fanout] * 1.25
        support_proposal_comparison[str(fanout)] = {
            "baseline_p95_ms": baseline_support[fanout],
            "baseline_relative_limit_ms": round(relative_limit, 6),
            "absolute_limit_ms": ADR_SUPPORT_P95_MS,
            "observed_p95_ms": observed_p95,
            "absolute_gate_passed": observed_p95 <= ADR_SUPPORT_P95_MS,
            "baseline_relative_gate_passed": observed_p95 <= relative_limit,
        }

    exact_10k_p95 = _latency(exact_results["10000"], "p95_ms")
    exact_100k_p95 = _latency(exact_results["100000"], "p95_ms")
    slope = exact_100k_p95 / exact_10k_p95 if exact_10k_p95 else 0.0
    support_gate_passed = all(_latency(result, "p95_ms") <= ADR_SUPPORT_P95_MS for result in support_results.values())
    consistency = check_index_state(largest_state)

    return {
        "artifact_schema_version": 1,
        "captured_at": datetime.now(UTC).isoformat(),
        "environment": {
            "python": platform.python_version(),
            "platform": platform.platform(),
            "processor": platform.processor(),
        },
        "method": {
            "clock": "time.perf_counter_ns",
            "latency_unit": "milliseconds per operation",
            "percentiles": "nearest observed rank over per-batch operation averages",
            "samples": samples,
            "lookup_batch_size": lookup_batch_size,
            "exact_corpus_sizes": list(EXACT_CORPUS_SIZES),
            "support_fanouts": list(SUPPORT_FANOUTS),
            "rebuild_and_mutation_corpus_size": REBUILD_CORPUS_SIZE,
            "external_services": "none",
        },
        "exact_lookup": exact_results,
        "support_lookup": support_results,
        "support_proposal": full_proposal,
        "rebuild": rebuild,
        "incremental_mutations": mutations,
        "build_memory_at_5000": memory,
        "correctness": {
            "all_lookup_expectations_met": all(correctness),
            "largest_state_consistent": consistency.consistent,
            "checker_omitted_issue_count": consistency.omitted_issue_count,
            "lookup_output_truncates_after_complete_scan": (
                remediation_lookup.complete
                and remediation_lookup.scanned_edge_count == MAX_INDEX_LOOKUP_OWNERS + 1
                and remediation_lookup.omitted_match_count == 1
            ),
            "scan_exhaustion_abstains": not scan_exhausted.complete and not scan_exhausted.matches,
            "full_proposal_candidates_correct": all(
                result["candidate_count_stable"] and result["all_candidates_supported"] for result in full_proposal
            ),
        },
        "adr_0004_comparison": {
            "exact_100000_p95_limit_ms": ADR_EXACT_P95_MS,
            "exact_100000_p95_passed": exact_100k_p95 <= ADR_EXACT_P95_MS,
            "exact_10000_to_100000_p95_slope": round(slope, 4),
            "exact_slope_limit": ADR_EXACT_SLOPE,
            "exact_slope_passed": slope <= ADR_EXACT_SLOPE,
            "support_p95_limit_ms": ADR_SUPPORT_P95_MS,
            "support_engineering_gate_passed": support_gate_passed,
            "support_proposal": support_proposal_comparison,
            "support_proposal_gate_passed": all(
                comparison["absolute_gate_passed"] and comparison["baseline_relative_gate_passed"]
                for comparison in support_proposal_comparison.values()
            ),
            "rebuild_p95_limit_ms": ADR_REBUILD_P95_MS,
            "rebuild_gate_passed": _latency(rebuild, "p95_ms") <= ADR_REBUILD_P95_MS,
            "build_memory_limit_bytes": ADR_BUILD_MEMORY_BYTES,
            "build_memory_gate_passed": memory["peak_bytes"] <= ADR_BUILD_MEMORY_BYTES,
            "mutation_latency": "informational; ADR 0004 defines no mutation latency threshold",
        },
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--samples", type=int, default=30)
    parser.add_argument("--lookup-batch-size", type=int, default=1_000)
    return parser


def main(argv: Sequence[str] = ()) -> int:
    args = _parser().parse_args(argv)
    if args.samples < 30:
        raise ValueError("samples must be at least 30")
    if args.lookup_batch_size < 1:
        raise ValueError("lookup batch size must be positive")
    result = run_benchmark(args.samples, args.lookup_batch_size)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
