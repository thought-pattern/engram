import copy
from typing import Any, cast

import pytest

import eval.run_release_gate_foundation as foundation_module
from eval.run_release_gate_foundation import (
    ADVERSARIAL_DIMENSIONS,
    PARTITION_NAMES,
    WORKLOAD_FAMILIES,
    aggregate_quality,
    execute_partition_contrasts,
    load_manifest,
    run_foundation,
    validate_manifest,
)


def quality_run(partition: str, *, passed: bool = True, false_answers: int = 0) -> dict:
    evidence_families = {"paraphrase", "multi_turn", "technical"}
    answer_families = {"exact", "alias"}
    probes = tuple(
        {
            "id": f"{partition}-{family}",
            "family": family,
            "latency_ms": float(index + 1),
            "correct": passed,
            "observed_outcome": "ANSWER" if family in answer_families else "EVIDENCE" if family in evidence_families else "MISS",
        }
        for index, family in enumerate(WORKLOAD_FAMILIES)
    )
    result = {
        "partition": partition,
        "probes": probes,
        "metrics": {
            "direct_answer_acceptance": {"accepted": 2, "eligible": 2, "rate": 1.0},
            "false_direct_answers": {
                "count": false_answers,
                "evaluated": len(probes),
                "rate": false_answers / len(probes),
            },
            "useful_evidence": {"useful": 3, "eligible": 3, "rate": 1.0},
            "proposal_acceptance": {"accepted": 1, "proposed": 1, "rate": 1.0},
            "typed_rejection_distribution": {
                "rejected_context": 1,
                "rejected_policy": 1,
                "rejected_quality": 1,
                "rejected_stale": 1,
            },
            "later_correction": {"passed": 1, "evaluated": 1},
            "abstention_quality": {"correct": 10, "eligible": 10, "rate": 1.0},
        },
        "performance": {
            "peak_working_memory_bytes": 4096,
            "maximum_evidence_bytes": 1024,
            "cpu_ms": 1.0,
        },
        "passed": passed,
    }
    return result


def evidence_item(name: str, data: dict) -> dict[str, object]:
    result = {
        "path": f"documentation/evaluation/{name}.json",
        "available": True,
        "current": True,
        "passed": True,
        "governed_source_sha256": "a" * 64,
        "artifact_sha256": name.encode("utf-8").hex().ljust(64, "0")[:64],
        "data": data,
    }
    return result


def supporting_evidence() -> dict[str, dict[str, object]]:
    performance = {
        "startup": {
            "cold_process_import_and_construction": {
                "p50_ms": 100.0,
                "p95_ms": 110.0,
                "p99_ms": 115.0,
                "maximum_ms": 120.0,
            }
        },
        "lexical": [
            {
                "persistence": {"serialized_bytes": 1000},
                "memory": {"peak_bytes": 2000},
            }
        ],
    }
    semantic = {
        "artifact": {"size_bytes": 1000},
        "backends": {
            "native": {
                "index_build_ms": 10.0,
                "metrics": {"engineering_holdout": {"recall_at_1": 0.9}},
                "throughput_queries_per_second": 100.0,
                "rss_before_mib": 400.0,
                "rss_after_model_mib": 440.0,
                "rss_after_index_mib": 450.0,
            }
        },
    }
    mcp = {
        "requested_turns": 1000,
        "configuration": {"memgraph_probe_every": 200},
        "sources": {"graph": 5},
    }
    result = {
        "performance": evidence_item("performance", performance),
        "semantic": evidence_item("semantic", semantic),
        "contextual": evidence_item("contextual", {}),
        "temporal": evidence_item("temporal", {}),
        "composition": evidence_item("composition", {}),
        "evidence": evidence_item("evidence", {"ten_record_package_bytes": 12530}),
        "chaos": evidence_item("chaos", {}),
        "mcp": evidence_item("mcp", mcp),
        "engineering": evidence_item("engineering", {}),
    }
    return result


def test_manifest_defines_three_complete_project_owned_partitions() -> None:
    validation = validate_manifest(load_manifest())
    coverage = cast(dict[str, tuple[str, ...]], validation["partition_coverage"])

    assert validation["partition_count"] == 3
    assert validation["case_count"] == 45
    assert validation["cases_per_partition"] == 15
    assert validation["contrast_count"] == 15
    assert validation["requests_disjoint"] is True
    assert validation["numerical_gate_status"] == "approved"
    assert set(coverage) == set(PARTITION_NAMES)
    assert all(len(value) == len(WORKLOAD_FAMILIES) for value in coverage.values())


@pytest.mark.parametrize("partition", PARTITION_NAMES)
def test_every_partition_executes_all_adversarial_identity_contrasts(partition: str) -> None:
    results = execute_partition_contrasts(load_manifest(), partition)

    assert {value["dimension"] for value in results} == set(ADVERSARIAL_DIMENSIONS)
    assert all(value["distinct_identity"] for value in results)


def test_manifest_rejects_cross_partition_request_reuse() -> None:
    manifest = cast(dict[str, Any], copy.deepcopy(load_manifest()))
    manifest["cases"][15]["request"] = manifest["cases"][0]["request"]

    with pytest.raises(ValueError, match="requests must be disjoint"):
        validate_manifest(manifest)


def test_manifest_rejects_external_or_independent_partition_ownership() -> None:
    manifest = cast(dict[str, Any], copy.deepcopy(load_manifest()))
    manifest["partitions"][1]["ownership"] = "independent_custodian"

    with pytest.raises(ValueError, match="project ownership"):
        validate_manifest(manifest)


def test_manifest_rejects_unapproved_numerical_gates() -> None:
    manifest = cast(dict[str, Any], copy.deepcopy(load_manifest()))
    manifest["numerical_gates"]["status"] = "pending"

    with pytest.raises(ValueError, match="must be approved"):
        validate_manifest(manifest)


def test_quality_aggregation_uses_all_trials_and_reports_dispersion() -> None:
    result = aggregate_quality(tuple(quality_run("release_gate") for _ in range(3)))

    assert result["trial_count"] == 3
    assert result["metrics"]["false_direct_answers"] == {"count": 0, "evaluated": 45, "rate": 0.0}
    assert result["metrics"]["useful_evidence"] == {"useful": 9, "eligible": 9, "rate": 1.0}
    assert result["metrics"]["typed_rejection_distribution"]["rejected_policy"] == 3
    assert result["dispersion"]["false_direct_answer_rate_standard_deviation"] == 0.0


def test_project_qualification_executes_in_order_and_approves_release(monkeypatch) -> None:
    calls = []

    def run_quality(partition: str, case_requests: dict) -> dict:
        calls.append(partition)
        assert set(case_requests) == set(WORKLOAD_FAMILIES)
        return quality_run(partition)

    monkeypatch.setattr(foundation_module, "run_quality_probes", run_quality)
    result = cast(
        dict[str, Any],
        run_foundation(
            load_manifest(),
            supporting_evidence(),
            {"governed_source_sha256": "a" * 64},
            "b" * 64,
        ),
    )

    assert calls == ["tuning"] * 3 + ["release_gate"] * 3 + ["final_test"] * 3
    assert result["partition_execution_order"] == PARTITION_NAMES
    assert result["release_ready"] is True
    assert result["release_approved"] is True
    assert result["release_decision"]["status"] == "approved_for_staged_rollout"
    assert result["partition_results"]["final_test"]["resources"]["evidence_package_maximum_bytes"] == 12530


def test_failed_release_gate_prevents_final_test(monkeypatch) -> None:
    calls = []

    def run_quality(partition: str, case_requests: dict) -> dict:
        calls.append(partition)
        return quality_run(partition, false_answers=int(partition == "release_gate"))

    monkeypatch.setattr(foundation_module, "run_quality_probes", run_quality)
    result = cast(
        dict[str, Any],
        run_foundation(
            load_manifest(),
            supporting_evidence(),
            {"governed_source_sha256": "a" * 64},
            "b" * 64,
        ),
    )

    assert "final_test" not in calls
    assert result["partition_results"]["release_gate"]["passed"] is False
    assert result["partition_results"]["final_test"] == {}
    assert result["release_approved"] is False


def test_stale_supporting_evidence_blocks_release(monkeypatch) -> None:
    evidence = supporting_evidence()
    evidence["semantic"]["current"] = False
    monkeypatch.setattr(
        foundation_module,
        "run_quality_probes",
        lambda partition, case_requests: quality_run(partition),
    )

    result = cast(
        dict[str, Any],
        run_foundation(
            load_manifest(),
            evidence,
            {"governed_source_sha256": "a" * 64},
            "b" * 64,
        ),
    )

    assert result["partition_results"]["release_gate"]["gate_checks"]["supporting_evidence_current_and_passed"] is False
    assert result["release_approved"] is False


def test_release_decision_binds_source_configuration_gates_and_evidence(monkeypatch) -> None:
    monkeypatch.setattr(
        foundation_module,
        "run_quality_probes",
        lambda partition, case_requests: quality_run(partition),
    )
    result = cast(
        dict[str, Any],
        run_foundation(
            load_manifest(),
            supporting_evidence(),
            {"governed_source_sha256": "a" * 64},
            "b" * 64,
        ),
    )
    decision = result["release_decision"]

    assert decision["governed_source_sha256"] == "a" * 64
    assert decision["configuration_sha256"] == "b" * 64
    assert decision["numerical_gates_sha256"] == result["validation"]["numerical_gates_sha256"]
    assert set(decision["evidence_artifacts_sha256"]) == set(supporting_evidence())
    assert len(decision["decision_sha256"]) == 64
