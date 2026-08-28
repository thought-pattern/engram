"""Reproducible Section 7 evidence codec, package, normalization, and orchestration benchmark."""

import argparse
import hashlib
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

from engram.core import Engram
from engram.evidence import (
    canonicalize_proposition_evidence,
    evaluate_evidence_usefulness,
    evidence_usefulness_policy,
    evidence_usefulness_policy_to_dict,
)
from engram.identity import scope_key
from engram.resolution import (
    PropositionEvidenceRecord,
    PropositionOwnership,
    DisclosureBasis,
    QueryFrame,
    QueryFrameBuilder,
    ResolutionOutcome,
    ResolverResult,
    ResolverState,
    budget_consumption_to_dict,
    build_evidence_package,
    canonical_proposition_references,
    capture_resolution_budget,
    proposition_evidence_record,
    proposition_evidence_record_from_json,
    proposition_evidence_record_to_dict,
    proposition_evidence_record_to_json,
    proposition_trust_inputs,
    proposition_validity_inputs,
    disclosure_decision,
    evidence_package_from_json,
    evidence_package_to_json,
    feature_set,
    resolution_budget,
    resolution_result_to_json,
    resolver_result,
)
from engram.resolvers import (
    ResolutionAccountingFinalizer,
    ResolutionOrchestrator,
    ResolverBudget,
    ResolverExecutor,
    ResolverRegistry,
)
from scripts.benchmark_metadata import benchmark_source_state, recorded_at

DEFAULT_OUTPUT = Path("eval/results/evidence/benchmark-2026-08-19.json")
START_NS = 1_000_000_000
NOW = datetime(2026, 8, 16, 16, 0, tzinfo=UTC)
SCOPE = scope_key(namespace="section7-benchmark")


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


def record(index: int) -> PropositionEvidenceRecord:
    """Construct one content-neutral, currently eligible synthetic record."""
    proposition_id = f"proposition-benchmark-{index:04d}"
    result = proposition_evidence_record(
        proposition_id=proposition_id,
        source_resolver="structured_graph",
        source_contributions=("structured_graph",),
        features=feature_set(
            values={"canonical_completeness": 1.0, "structured_match": 1.0},
            unavailable=("semantic_similarity", "source_agreement", "supplied_trust"),
        ),
        canonical_references=canonical_proposition_references(
            f"entity:subject-{index:04d}",
            "predicate:benchmark",
            f"entity:object-{index:04d}",
        ),
        validity=proposition_validity_inputs(
            "2026-08-16T16:00:00Z",
            True,
            True,
            True,
        ),
        trust=proposition_trust_inputs(),
        disclosure=disclosure_decision(
            PropositionOwnership.PUBLIC,
            DisclosureBasis.PUBLIC_RULE,
            SCOPE,
            "proposition-disclosure-v1",
        ),
        path=(proposition_id,),
        selection_reasons=("canonical_complete", "public", "structured_match"),
    )
    return result


class SyntheticResolver:
    """Bounded synthetic resolver used only by the offline benchmark."""

    cost_class = next(iter(resolution_budget()["allowed_cost_classes"]))

    def __init__(self, name: str, result: ResolverResult, *, fail: bool = False) -> None:
        self.name = name
        self._result = result
        self._fail = fail

    def available(self, frame: QueryFrame) -> bool:
        result = True
        return result

    def resolve(self, frame: QueryFrame, budget: ResolverBudget) -> ResolverResult:
        if self._fail:
            raise RuntimeError("synthetic dependency failure")
        result = self._result
        return result


def build_result(samples: int) -> dict[str, object]:
    """Run warmed engineering benchmarks and return a bounded evidence artifact."""
    records = tuple(record(index) for index in range(1_000))
    ten_records = records[:10]
    policy = evidence_usefulness_policy()
    encoded_record = proposition_evidence_record_to_json(records[0])

    codec = measure(lambda: proposition_evidence_record_to_json(proposition_evidence_record_from_json(encoded_record)), samples)
    package = measure(
        lambda: evidence_package_from_json(evidence_package_to_json(build_evidence_package(ten_records))),
        samples,
    )
    normalization_samples = max(10, samples // 5)
    normalization = measure(
        lambda: canonicalize_proposition_evidence(records),
        normalization_samples,
    )
    usefulness = measure(
        lambda: tuple(evaluate_evidence_usefulness(policy, value) for value in records),
        normalization_samples,
    )

    canonical_forward = canonicalize_proposition_evidence(records)
    canonical_reverse = canonicalize_proposition_evidence(tuple(reversed(records)))
    canonical_digest = hashlib.sha256(
        json.dumps(
            [proposition_evidence_record_to_dict(value) for value in canonical_forward],
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    reverse_digest = hashlib.sha256(
        json.dumps(
            [proposition_evidence_record_to_dict(value) for value in canonical_reverse],
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()

    tracemalloc.start()
    memory_result = canonicalize_proposition_evidence(records)
    memory_package = build_evidence_package(memory_result, max_records=10)
    _, peak_bytes = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    engine = Engram()
    budget = capture_resolution_budget(lambda: START_NS)
    frame = QueryFrameBuilder(engine, lambda: START_NS, lambda: NOW).build(
        "Which benchmark Propositions are useful?",
        SCOPE,
        diagnostic_seed="section7-evidence-benchmark",
        budget=budget,
    )
    successful = SyntheticResolver(
        "structured_graph",
        resolver_result(
            "structured_graph",
            ResolverState.COMPLETED,
            proposition_evidence=ten_records,
        ),
    )
    failed = SyntheticResolver(
        "support_semantic",
        resolver_result("support_semantic", ResolverState.COMPLETED),
        fail=True,
    )
    registry = ResolverRegistry((successful, failed))
    executor = ResolverExecutor(lambda: START_NS)
    finalizer = ResolutionAccountingFinalizer(engine, max_requests=max(1_000, samples + 2))
    orchestrator = ResolutionOrchestrator(registry, executor, finalizer)
    request_number = 0

    def resolve_partial_failure():
        nonlocal request_number
        request_number += 1
        result = orchestrator.resolve(frame, f"section7-benchmark-{request_number}")
        return result

    orchestration = measure(resolve_partial_failure, samples)
    partial_result, partial_finalization = resolve_partial_failure()
    package_value = build_evidence_package(ten_records)
    package_bytes = len(evidence_package_to_json(package_value).encode("utf-8"))
    all_included = all(evaluate_evidence_usefulness(policy, value)["included"] for value in records)
    gates = {
        "thousand_record_peak_under_64_mib": peak_bytes < 67_108_864,
        "package_within_64_kib": package_bytes <= 65_536,
        "package_retains_ten_records": package_value["retained_count"] == 10,
        "normalization_is_order_independent": canonical_digest == reverse_digest,
        "frozen_policy_includes_qualified_synthetic_records": all_included,
        "partial_failure_returns_evidence": partial_result["outcome"] == ResolutionOutcome.EVIDENCE,
        "partial_failure_is_visible": any(value["state"] == ResolverState.FAILED for value in partial_result["resolver_results"]),
        "partial_failure_contains_full_records_only_in_package": (
            partial_result["evidence_package"]["retained_count"] == 10
            and all(not value["proposition_evidence"] for value in partial_result["resolver_results"])
        ),
        "proposition_only_has_no_response_accounting": (
            partial_finalization["candidate_statement_ids"] == () and not partial_finalization["success_applied"]
        ),
        "complete_output_accounting_is_exact": (
            partial_result["budget"]["output_bytes"] == len(resolution_result_to_json(partial_result).encode("utf-8"))
        ),
        "working_memory_within_frame_budget": (
            partial_result["budget"]["working_memory_bytes"] <= frame["budget"]["max_working_memory_bytes"]
        ),
    }
    result = {
        "schema_version": 1,
        "benchmark_version": "section7-evidence-benchmark-current-1",
        "recorded_at": recorded_at(),
        "source": benchmark_source_state(),
        "environment": {"python": platform.python_version(), "platform": platform.platform()},
        "policy": evidence_usefulness_policy_to_dict(policy),
        "policy_provenance": {
            "selection": "hand_authored_conservative_unfitted",
            "unit_conformance_or_benchmark_data_used_for_parameter_selection": False,
            "empirical_calibration_owner": "Section 16",
        },
        "samples": samples,
        "normalization_samples": normalization_samples,
        "record_codec": codec,
        "ten_record_package": package,
        "thousand_record_normalization": normalization,
        "thousand_record_policy": usefulness,
        "partial_failure_orchestration": orchestration,
        "timing_assessment": "reported observations; no pass/fail threshold",
        "thousand_record_peak_bytes": peak_bytes,
        "thousand_record_normalized_count": len(memory_result),
        "ten_record_package_bytes": package_bytes,
        "ten_record_package_retained": memory_package["retained_count"],
        "ten_record_package_omitted_from_thousand": memory_package["omitted_count"],
        "canonical_digest": canonical_digest,
        "partial_failure_outcome": partial_result["outcome"].value,
        "partial_failure_resolver_states": [value["state"].value for value in partial_result["resolver_results"]],
        "partial_failure_budget": budget_consumption_to_dict(partial_result["budget"]),
        "gates": gates,
        "all_gates_passed": all(gates.values()),
    }
    return result


def main() -> int:
    """Run the benchmark and optionally write its JSON artifact."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--samples", type=int, default=100)
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
