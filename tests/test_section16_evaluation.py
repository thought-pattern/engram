"""Section 16 open-engineering evaluation contracts."""

from eval.run_release_gate_foundation import WORKLOAD_FAMILIES
from eval.run_section16_chaos import SCENARIOS
from eval.run_section16_engineering import run_quality_probes


def test_section16_quality_probes_measure_every_required_quality_category() -> None:
    result = run_quality_probes()
    metrics = result["metrics"]

    assert result["passed"] is True
    assert metrics["direct_answer_acceptance"] == {"accepted": 2, "eligible": 2, "rate": 1.0}
    assert metrics["false_direct_answers"]["count"] == 0
    assert metrics["useful_evidence"] == {"useful": 3, "eligible": 3, "rate": 1.0}
    assert metrics["proposal_acceptance"] == {"accepted": 1, "proposed": 1, "rate": 1.0}
    assert set(metrics["typed_rejection_distribution"]) == {
        "rejected_quality",
        "rejected_context",
        "rejected_stale",
        "rejected_policy",
    }
    assert metrics["later_correction"]["passed"] is True
    assert metrics["abstention_quality"]["rate"] == 1.0
    assert len(result["probes"]) == len(WORKLOAD_FAMILIES)
    assert {probe["family"] for probe in result["probes"]} == set(WORKLOAD_FAMILIES)
    assert set(result["performance"]["total_latency_ms"]) == {"p50", "p95", "p99"}
    assert all(
        set(observations) == {"p50", "p95", "p99", "observations"}
        for observations in result["performance"]["resolver_latency_ms"].values()
    )


def test_section16_chaos_matrix_is_exact_and_complete() -> None:
    identifiers = tuple(scenario["id"] for scenario in SCENARIOS)

    assert identifiers == (
        "unavailable_graph",
        "missing_model",
        "bad_index",
        "dimension_mismatch",
        "persistence_degradation",
        "restart",
        "timeout",
        "cancellation",
        "partial_evidence",
        "adapter_outage",
    )
    assert all(scenario["node"].startswith("tests/") for scenario in SCENARIOS)
