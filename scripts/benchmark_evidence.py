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
from engram.evidence import EvidenceUsefulnessPolicy, canonicalize_claim_evidence
from engram.identity import ScopeKey
from engram.resolution import (
    CanonicalClaimReferences,
    ClaimEvidenceRecord,
    ClaimOwnership,
    ClaimTrustInputs,
    ClaimValidityInputs,
    DisclosureBasis,
    DisclosureDecision,
    EvidencePackage,
    FeatureSet,
    QueryFrame,
    QueryFrameBuilder,
    ResolutionBudget,
    ResolutionOutcome,
    ResolverResult,
    ResolverState,
)
from engram.resolvers import (
    ResolutionAccountingFinalizer,
    ResolutionOrchestrator,
    ResolverBudget,
    ResolverExecutor,
    ResolverRegistry,
)

DEFAULT_OUTPUT = REPOSITORY / "documentation" / "evidence" / "benchmark-2026-08-16.json"
START_NS = 1_000_000_000
NOW = datetime(2026, 8, 16, 16, 0, tzinfo=UTC)
SCOPE = ScopeKey(namespace="section7-benchmark")


def percentile(values: list[float], fraction: float) -> float:
    """Return a deterministic nearest-rank percentile."""
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(len(ordered) * fraction + 0.999999) - 1))
    return ordered[index]


def measure(operation, samples: int) -> dict[str, float]:
    """Measure one warmed operation in milliseconds."""
    operation()
    values = []
    for _ in range(samples):
        started = time.perf_counter_ns()
        operation()
        values.append((time.perf_counter_ns() - started) / 1_000_000)
    return {
        "p50_ms": statistics.median(values),
        "p95_ms": percentile(values, 0.95),
        "max_ms": max(values),
    }


def record(index: int) -> ClaimEvidenceRecord:
    """Construct one content-neutral, currently eligible synthetic record."""
    claim_id = f"claim-benchmark-{index:04d}"
    return ClaimEvidenceRecord(
        claim_id=claim_id,
        source_resolver="structured_graph",
        source_contributions=("structured_graph",),
        features=FeatureSet(
            values={"canonical_completeness": 1.0, "structured_match": 1.0},
            unavailable=("semantic_similarity", "source_agreement", "supplied_trust"),
        ),
        canonical_references=CanonicalClaimReferences(
            subject_entity_id=f"entity:subject-{index:04d}",
            predicate_id="predicate:benchmark",
            object_entity_id=f"entity:object-{index:04d}",
        ),
        validity=ClaimValidityInputs(
            evaluation_time="2026-08-16T16:00:00Z",
            active=True,
            system_current=True,
            valid_time_current=True,
        ),
        trust=ClaimTrustInputs(),
        disclosure=DisclosureDecision(
            ownership=ClaimOwnership.PUBLIC,
            basis=DisclosureBasis.PUBLIC_RULE,
            scope=SCOPE,
            policy_version="claim-disclosure-v1",
        ),
        path=(claim_id,),
        selection_reasons=("canonical_complete", "public", "structured_match"),
    )


class SyntheticResolver:
    """Bounded synthetic resolver used only by the offline benchmark."""

    cost_class = next(iter(ResolutionBudget().allowed_cost_classes))

    def __init__(self, name: str, result: ResolverResult, *, fail: bool = False) -> None:
        self.name = name
        self._result = result
        self._fail = fail

    def available(self, frame: QueryFrame) -> bool:
        return True

    def resolve(self, frame: QueryFrame, budget: ResolverBudget) -> ResolverResult:
        if self._fail:
            raise RuntimeError("synthetic dependency failure")
        return self._result


def build_result(samples: int) -> dict[str, object]:
    """Run warmed engineering benchmarks and return a bounded evidence artifact."""
    records = tuple(record(index) for index in range(1_000))
    ten_records = records[:10]
    policy = EvidenceUsefulnessPolicy()
    encoded_record = records[0].to_json()

    codec = measure(lambda: ClaimEvidenceRecord.from_json(encoded_record).to_json(), samples)
    package = measure(
        lambda: EvidencePackage.from_json(EvidencePackage.build(ten_records).to_json()),
        samples,
    )
    normalization_samples = max(10, samples // 5)
    normalization = measure(
        lambda: canonicalize_claim_evidence(records),
        normalization_samples,
    )
    usefulness = measure(
        lambda: tuple(policy.evaluate(value) for value in records),
        normalization_samples,
    )

    canonical_forward = canonicalize_claim_evidence(records)
    canonical_reverse = canonicalize_claim_evidence(tuple(reversed(records)))
    canonical_digest = hashlib.sha256(
        json.dumps(
            [value.to_dict() for value in canonical_forward],
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    reverse_digest = hashlib.sha256(
        json.dumps(
            [value.to_dict() for value in canonical_reverse],
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()

    tracemalloc.start()
    memory_result = canonicalize_claim_evidence(records)
    memory_package = EvidencePackage.build(memory_result, max_records=10)
    _, peak_bytes = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    engine = Engram()
    budget = ResolutionBudget.capture(lambda: START_NS, total_time_ms=100, resolver_time_ms=25)
    frame = QueryFrameBuilder(engine, lambda: START_NS, lambda: NOW).build(
        "Which benchmark Claims are useful?",
        SCOPE,
        diagnostic_seed="section7-evidence-benchmark",
        budget=budget,
    )
    successful = SyntheticResolver(
        "structured_graph",
        ResolverResult(
            "structured_graph",
            ResolverState.COMPLETED,
            claim_evidence=ten_records,
        ),
    )
    failed = SyntheticResolver(
        "support_semantic",
        ResolverResult("support_semantic", ResolverState.COMPLETED),
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
        return orchestrator.resolve(frame, f"section7-benchmark-{request_number}")

    orchestration = measure(resolve_partial_failure, samples)
    partial_result, partial_finalization = resolve_partial_failure()
    package_value = EvidencePackage.build(ten_records)
    package_bytes = len(package_value.to_json().encode("utf-8"))
    all_included = all(policy.evaluate(value).included for value in records)
    gates = {
        "record_codec_p95_under_10_ms": codec["p95_ms"] < 10.0,
        "ten_record_package_p95_under_50_ms": package["p95_ms"] < 50.0,
        "thousand_record_normalization_p95_under_1500_ms": normalization["p95_ms"] < 1_500.0,
        "thousand_record_policy_p95_under_500_ms": usefulness["p95_ms"] < 500.0,
        "partial_failure_orchestration_p95_under_100_ms": orchestration["p95_ms"] < 100.0,
        "thousand_record_peak_under_64_mib": peak_bytes < 67_108_864,
        "package_within_64_kib": package_bytes <= 65_536,
        "package_retains_ten_records": package_value.retained_count == 10,
        "normalization_is_order_independent": canonical_digest == reverse_digest,
        "frozen_policy_includes_qualified_synthetic_records": all_included,
        "partial_failure_returns_evidence": partial_result.outcome == ResolutionOutcome.EVIDENCE,
        "partial_failure_is_visible": any(value.state == ResolverState.FAILED for value in partial_result.resolver_results),
        "partial_failure_contains_full_records_only_in_package": (
            partial_result.evidence_package.retained_count == 10
            and all(not value.claim_evidence for value in partial_result.resolver_results)
        ),
        "claim_only_has_no_response_accounting": (
            partial_finalization.candidate_statement_ids == () and not partial_finalization.success_applied
        ),
        "complete_output_accounting_is_exact": (
            partial_result.budget.output_bytes == len(partial_result.to_json().encode("utf-8"))
        ),
        "working_memory_within_frame_budget": (partial_result.budget.working_memory_bytes <= frame.budget.max_working_memory_bytes),
    }
    return {
        "schema_version": 1,
        "benchmark_version": "section7-evidence-benchmark-current-1",
        "recorded_at": "2026-08-16",
        "environment": {"python": platform.python_version(), "platform": platform.platform()},
        "policy": policy.to_dict(),
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
        "thousand_record_peak_bytes": peak_bytes,
        "thousand_record_normalized_count": len(memory_result),
        "ten_record_package_bytes": package_bytes,
        "ten_record_package_retained": memory_package.retained_count,
        "ten_record_package_omitted_from_thousand": memory_package.omitted_count,
        "canonical_digest": canonical_digest,
        "partial_failure_outcome": partial_result.outcome.value,
        "partial_failure_resolver_states": [value.state.value for value in partial_result.resolver_results],
        "partial_failure_budget": partial_result.budget.to_dict(),
        "gates": gates,
        "all_gates_passed": all(gates.values()),
    }


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
    return 0 if result["all_gates_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
