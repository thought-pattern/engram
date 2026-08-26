"""Execute the ten Section 16 failure scenarios through their focused tests."""

import argparse
import hashlib
import json
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

REPOSITORY = Path(__file__).resolve().parent.parent
if str(REPOSITORY) not in sys.path:
    sys.path.insert(0, str(REPOSITORY))

from scripts.benchmark_metadata import benchmark_source_state

DEFAULT_OUTPUT = Path("eval/results/evaluation/section16-chaos-2026-08-23.json")
SCENARIOS = (
    {
        "id": "unavailable_graph",
        "node": "tests/test_graph.py::test_read_only_graph_wiring_unavailable_enabled_graph_is_optional",
        "expected_fallback": "local Engram remains ready while graph reports unavailable",
    },
    {
        "id": "missing_model",
        "node": "tests/test_semantic.py::test_enabled_missing_model_fails_soft_without_blocking_engram_startup",
        "expected_fallback": "semantic reports unavailable without blocking startup",
    },
    {
        "id": "bad_index",
        "node": "tests/test_sparse.py::test_sparse_index_check_detects_corruption_and_atomic_rebuild_repairs_it",
        "expected_fallback": "corruption is detected and an atomic rebuild restores consistency",
    },
    {
        "id": "dimension_mismatch",
        "node": "tests/test_semantic.py::test_local_artifact_policy_is_offline_checksum_and_dimension_gated",
        "expected_fallback": "semantic capability remains unavailable",
    },
    {
        "id": "persistence_degradation",
        "node": "tests/test_service.py::test_checkpoint_failure_reports_degraded_state_and_recovers",
        "expected_fallback": "degraded durability is reported and recovery restores health",
    },
    {
        "id": "restart",
        "node": "tests/test_resolvers.py::test_exact_accounting_receipt_replays_after_restart_without_double_credit",
        "expected_fallback": "durable replay avoids duplicate accounting",
    },
    {
        "id": "timeout",
        "node": "tests/test_grpc_server.py::test_v2_resolution_propagates_cancellation_without_caching_partial_work[deadline]",
        "expected_fallback": "deadline is transient and the same request can retry",
    },
    {
        "id": "cancellation",
        "node": "tests/test_service.py::test_unified_resolution_cancellation_is_transient_and_not_cached",
        "expected_fallback": "cancellation publishes no partial result and retry remains available",
    },
    {
        "id": "partial_evidence",
        "node": "tests/test_resolvers.py::test_orchestrator_trims_claim_package_to_complete_output_budget",
        "expected_fallback": "the evidence package is complete, bounded, and explicitly truncated",
    },
    {
        "id": "adapter_outage",
        "node": "tests/test_grpc_server.py::test_unhandled_grpc_failure_redacts_exception_content",
        "expected_fallback": "the adapter returns a redacted typed transport failure",
    },
)


def execute_scenario(scenario: dict[str, str]) -> dict[str, object]:
    """Run one focused scenario and retain non-sensitive execution evidence."""
    command = [sys.executable, "-m", "pytest", scenario["node"], "-q", "--tb=short"]
    started = time.perf_counter_ns()
    completed = subprocess.run(command, cwd=REPOSITORY, capture_output=True, text=True, encoding="utf-8", errors="replace")
    elapsed_ms = (time.perf_counter_ns() - started) / 1_000_000
    combined = completed.stdout + completed.stderr
    result = {
        "id": scenario["id"],
        "node": scenario["node"],
        "expected_fallback": scenario["expected_fallback"],
        "exit_code": completed.returncode,
        "passed": completed.returncode == 0,
        "elapsed_ms": round(elapsed_ms, 6),
        "output_sha256": hashlib.sha256(combined.encode("utf-8")).hexdigest(),
        "failure_tail": "" if completed.returncode == 0 else combined[-2000:],
    }
    return result


def run_chaos() -> dict[str, object]:
    """Run every mandatory failure scenario and build one source-bound result."""
    results = tuple(execute_scenario(scenario) for scenario in SCENARIOS)
    result = {
        "schema_version": 1,
        "evaluation_version": "section16-chaos-v1",
        "recorded_at": datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "source": benchmark_source_state(),
        "scenario_count": len(results),
        "passed_count": sum(1 for value in results if value["passed"] is True),
        "scenarios": results,
        "passed": all(value["passed"] for value in results),
        "release_eligible": False,
    }
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    arguments = parser.parse_args()
    result = run_chaos()
    arguments.output.parent.mkdir(parents=True, exist_ok=True)
    arguments.output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(arguments.output)
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
