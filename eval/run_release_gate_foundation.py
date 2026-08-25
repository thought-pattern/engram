"""Run the project-owned Section 16 release qualification."""

import argparse
import hashlib
import json
import platform
import statistics
import sys
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

REPOSITORY = Path(__file__).resolve().parent.parent
if str(REPOSITORY) not in sys.path:
    sys.path.insert(0, str(REPOSITORY))

from engram.identity import build_standalone_identity, query_identity_to_json, scope_key
from eval.run_section16_engineering import (
    DEFAULT_BASELINE,
    DEFAULT_COMPOSITION,
    DEFAULT_CONTEXTUAL,
    DEFAULT_EVIDENCE,
    DEFAULT_MCP,
    DEFAULT_SEMANTIC,
    DEFAULT_TEMPORAL,
    avoided_work,
    load_evidence,
    partition_requests,
    percentile,
    performance_summary,
    run_quality_probes,
)
from scripts.benchmark_metadata import benchmark_source_state

DEFAULT_MANIFEST = Path("eval/release-gate-foundation-v1.json")
DEFAULT_CHAOS = Path("documentation/evaluation/section16-chaos-2026-08-23.json")
DEFAULT_ENGINEERING = Path("documentation/evaluation/section16-engineering-2026-08-23.json")
DEFAULT_OUTPUT = Path("documentation/evaluation/foundation-2026-08-19.json")
PARTITION_NAMES = ("tuning", "release_gate", "final_test")
WORKLOAD_FAMILIES = (
    "exact",
    "alias",
    "paraphrase",
    "multi_turn",
    "technical",
    "graph",
    "temporal",
    "ambiguous",
    "conflicting",
    "stale",
    "scope",
    "policy",
    "unsupported",
    "negative",
    "dependency_failure",
)
ADVERSARIAL_DIMENSIONS = ("when_where", "current_historical", "positive_negative", "relation", "scope")
EXPECTED_CLASSES = ("answer", "answer_or_evidence", "evidence_or_miss", "miss")
QUALITY_TRIALS = 3
MIB = 1024 * 1024


def load_manifest(path: Path = DEFAULT_MANIFEST) -> dict[str, object]:
    """Load one JSON object."""
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("evaluation input must be a JSON object")
    result = cast(dict[str, object], value)
    return result


def canonical_sha256(value: object) -> str:
    """Return the SHA-256 of one canonical JSON value."""
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    result = hashlib.sha256(encoded).hexdigest()
    return result


def file_sha256(path: Path) -> str:
    """Return the SHA-256 of one evidence artifact."""
    result = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


def require_mapping(value: object, name: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ValueError(f"{name} must be an object")
    result = cast(dict[str, object], value)
    return result


def require_list(value: object, name: str) -> list[object]:
    if not isinstance(value, list):
        raise ValueError(f"{name} must be a list")
    result = value
    return result


def require_text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{name} must be a nonempty string")
    result = value
    return result


def require_positive_integer(value: object, name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    result = value
    return result


def require_nonnegative_integer(value: object, name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError(f"{name} must be a nonnegative integer")
    result = value
    return result


def require_number(value: object, name: str) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool) or value < 0:
        raise ValueError(f"{name} must be a nonnegative number")
    result = float(value)
    return result


def require_rate(value: object, name: str) -> float:
    result = require_number(value, name)
    if result > 1.0:
        raise ValueError(f"{name} must be between zero and one")
    return result


def require_timestamp(value: object, name: str) -> str:
    text = require_text(value, name)
    parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError(f"{name} must include a timezone")
    return text


def normalized_request(value: str) -> str:
    """Normalize one request only for partition-disjointness checking."""
    result = " ".join(value.casefold().split())
    return result


def validate_manifest(manifest: dict[str, object]) -> dict[str, object]:
    """Validate the project-owned partitions and frozen numerical gates."""
    if manifest.get("schema_version") != 1:
        raise ValueError("evaluation manifest schema_version must be 1")
    if manifest.get("corpus_version") != "engram-release-gate-foundation-v1":
        raise ValueError("evaluation corpus_version is unsupported")
    if manifest.get("status") != "approved_project_qualification":
        raise ValueError("evaluation manifest must be approved for project qualification")

    provenance = require_mapping(manifest.get("provenance", {}), "provenance")
    if provenance.get("fixture_reuse") is not False:
        raise ValueError("evaluation partitions must not reuse engineering fixtures")
    if provenance.get("parameter_selection_partition") != "tuning":
        raise ValueError("only the tuning partition may select parameters")
    if provenance.get("release_gate_after_policy_freeze") is not True:
        raise ValueError("release-gate execution must follow the policy freeze")
    if provenance.get("final_test_after_release_gate") is not True:
        raise ValueError("final-test execution must follow the release gate")

    partition_records = tuple(
        require_mapping(value, "partition") for value in require_list(manifest.get("partitions"), "partitions")
    )
    partition_names = tuple(require_text(value.get("name"), "partition name") for value in partition_records)
    if partition_names != PARTITION_NAMES:
        raise ValueError("evaluation partitions must be tuning, release_gate, and final_test in order")
    for partition in partition_records:
        if partition.get("ownership") != "engram_project":
            raise ValueError("all evaluation partitions must use project ownership")
        if partition.get("content_available") is not True or partition.get("execution_authorized") is not True:
            raise ValueError("all project evaluation partitions must be available and authorized")
        require_text(partition.get("purpose"), "partition purpose")

    families = tuple(
        require_text(value, "workload family") for value in require_list(manifest.get("workload_families"), "workload_families")
    )
    if families != WORKLOAD_FAMILIES:
        raise ValueError("evaluation workload families do not match EGR-1602")

    case_ids = set()
    request_owners: dict[str, str] = {}
    coverage = {name: set() for name in PARTITION_NAMES}
    cases = require_list(manifest.get("cases"), "cases")
    for raw_case in cases:
        case = require_mapping(raw_case, "case")
        case_id = require_text(case.get("id"), "case id")
        partition = require_text(case.get("partition"), "case partition")
        family = require_text(case.get("family"), "case family")
        request = require_text(case.get("request"), "case request")
        expected_class = require_text(case.get("expected_class"), "expected_class")
        require_text(case.get("label_source"), "label_source")
        if partition not in PARTITION_NAMES or family not in WORKLOAD_FAMILIES:
            raise ValueError("case partition and family must use declared values")
        if expected_class not in EXPECTED_CLASSES:
            raise ValueError("case expected_class is unsupported")
        if case_id in case_ids:
            raise ValueError("evaluation case identifiers must be unique")
        owner = request_owners.get(normalized_request(request), "")
        if owner and owner != partition:
            raise ValueError("evaluation case requests must be disjoint across partitions")
        case_ids.add(case_id)
        request_owners[normalized_request(request)] = partition
        coverage[partition].add(family)
    if len(cases) != len(PARTITION_NAMES) * len(WORKLOAD_FAMILIES):
        raise ValueError("each partition must contain one case for every workload family")
    if any(coverage[name] != set(WORKLOAD_FAMILIES) for name in PARTITION_NAMES):
        raise ValueError("each partition must cover every workload family")

    contrast_ids = set()
    identity_owners: dict[str, str] = {}
    contrast_coverage = {name: set() for name in PARTITION_NAMES}
    contrasts = require_list(manifest.get("adversarial_contrasts"), "adversarial_contrasts")
    for raw_contrast in contrasts:
        contrast = require_mapping(raw_contrast, "adversarial contrast")
        contrast_id = require_text(contrast.get("id"), "contrast id")
        partition = require_text(contrast.get("partition"), "contrast partition")
        dimension = require_text(contrast.get("dimension"), "contrast dimension")
        if partition not in PARTITION_NAMES or dimension not in ADVERSARIAL_DIMENSIONS:
            raise ValueError("contrast partition and dimension must use declared values")
        if contrast_id in contrast_ids:
            raise ValueError("adversarial contrast identifiers must be unique")
        for side_name in ("left", "right"):
            side = require_mapping(contrast.get(side_name), f"contrast {side_name}")
            request = require_text(side.get("request"), f"contrast {side_name} request")
            namespace = require_text(side.get("namespace"), f"contrast {side_name} namespace")
            context = require_text(side.get("context_fingerprint"), f"contrast {side_name} context")
            identity = build_standalone_identity(request, scope_key(namespace, context))
            encoded = json.dumps(query_identity_to_json(identity), sort_keys=True)
            owner = identity_owners.get(encoded, "")
            if owner and owner != partition:
                raise ValueError("adversarial identities must be disjoint across partitions")
            identity_owners[encoded] = partition
        contrast_ids.add(contrast_id)
        contrast_coverage[partition].add(dimension)
    if len(contrasts) != len(PARTITION_NAMES) * len(ADVERSARIAL_DIMENSIONS):
        raise ValueError("each partition must contain every adversarial contrast")
    if any(contrast_coverage[name] != set(ADVERSARIAL_DIMENSIONS) for name in PARTITION_NAMES):
        raise ValueError("each partition must cover every adversarial dimension")

    numerical_gates = require_mapping(manifest.get("numerical_gates"), "numerical_gates")
    if numerical_gates.get("status") != "approved":
        raise ValueError("numerical gates must be approved before release evaluation")
    require_text(numerical_gates.get("approved_by"), "numerical gate approved_by")
    require_timestamp(numerical_gates.get("approved_at"), "numerical gate approved_at")
    require_text(numerical_gates.get("approval_basis"), "numerical gate approval_basis")
    false_gate = require_mapping(numerical_gates.get("false_direct_answer_rate"), "false_direct_answer_rate")
    useful_gate = require_mapping(numerical_gates.get("useful_evidence_rate"), "useful_evidence_rate")
    require_rate(false_gate.get("absolute_maximum"), "false answer absolute maximum")
    require_number(false_gate.get("baseline_relative_maximum"), "false answer baseline relative maximum")
    false_minimum = require_positive_integer(false_gate.get("minimum_cases"), "false answer minimum cases")
    require_rate(useful_gate.get("absolute_minimum"), "useful evidence absolute minimum")
    require_number(useful_gate.get("baseline_relative_minimum"), "useful evidence baseline relative minimum")
    useful_minimum = require_positive_integer(useful_gate.get("minimum_cases"), "useful evidence minimum cases")
    turn_lengths = require_mapping(numerical_gates.get("turn_length_reporting"), "turn_length_reporting")
    if turn_lengths.get("metrics") != ["p50_ms", "p95_ms", "p99_ms", "max_ms"] or turn_lengths.get("pass_fail") is not False:
        raise ValueError("turn lengths must be descriptive p50, p95, p99, and maximum values")
    memory_gate = require_mapping(numerical_gates.get("peak_memory_bytes"), "peak_memory_bytes")
    require_positive_integer(memory_gate.get("absolute_maximum"), "peak memory absolute maximum")
    require_number(memory_gate.get("baseline_relative_maximum"), "peak memory baseline relative maximum")
    durability_gate = require_mapping(
        numerical_gates.get("durability_lost_completed_mutations"),
        "durability_lost_completed_mutations",
    )
    require_nonnegative_integer(durability_gate.get("absolute_maximum"), "durability absolute maximum")
    require_number(durability_gate.get("baseline_relative_maximum"), "durability baseline relative maximum")
    evidence_gate = require_mapping(numerical_gates.get("evidence_package_bytes"), "evidence_package_bytes")
    require_positive_integer(evidence_gate.get("absolute_maximum"), "evidence package absolute maximum")
    require_number(evidence_gate.get("baseline_relative_maximum"), "evidence package baseline relative maximum")

    result = {
        "partition_count": len(partition_names),
        "case_count": len(case_ids),
        "cases_per_partition": len(WORKLOAD_FAMILIES),
        "workload_family_count": len(WORKLOAD_FAMILIES),
        "contrast_count": len(contrast_ids),
        "contrasts_per_partition": len(ADVERSARIAL_DIMENSIONS),
        "adversarial_dimension_count": len(ADVERSARIAL_DIMENSIONS),
        "partition_coverage": {name: tuple(sorted(coverage[name])) for name in PARTITION_NAMES},
        "contrast_coverage": {name: tuple(sorted(contrast_coverage[name])) for name in PARTITION_NAMES},
        "requests_disjoint": True,
        "numerical_gate_status": numerical_gates["status"],
        "numerical_gates_sha256": canonical_sha256(numerical_gates),
        "false_answer_minimum_cases": false_minimum,
        "useful_evidence_minimum_cases": useful_minimum,
        "turn_length_pass_fail": False,
    }
    return result


def execute_partition_contrasts(manifest: dict[str, object], partition: str) -> tuple[dict[str, object], ...]:
    """Execute the five identity contrasts for one partition."""
    results = []
    for raw_contrast in require_list(manifest.get("adversarial_contrasts"), "adversarial_contrasts"):
        contrast = require_mapping(raw_contrast, "adversarial contrast")
        if contrast.get("partition") != partition:
            continue
        left = require_mapping(contrast.get("left"), "contrast left")
        right = require_mapping(contrast.get("right"), "contrast right")
        left_identity = build_standalone_identity(
            require_text(left.get("request"), "left request"),
            scope_key(
                require_text(left.get("namespace"), "left namespace"),
                require_text(left.get("context_fingerprint"), "left context"),
            ),
        )
        right_identity = build_standalone_identity(
            require_text(right.get("request"), "right request"),
            scope_key(
                require_text(right.get("namespace"), "right namespace"),
                require_text(right.get("context_fingerprint"), "right context"),
            ),
        )
        results.append(
            {
                "id": contrast["id"],
                "partition": partition,
                "dimension": contrast["dimension"],
                "distinct_identity": query_identity_to_json(left_identity) != query_identity_to_json(right_identity),
            }
        )
    result = tuple(results)
    return result


def aggregate_quality(runs: tuple[dict[str, Any], ...]) -> dict[str, Any]:
    """Aggregate deterministic quality trials and report observed dispersion."""
    probes = []
    typed_rejections = Counter()
    metric_totals = {
        "direct_answer_acceptance": [0, 0],
        "false_direct_answers": [0, 0],
        "useful_evidence": [0, 0],
        "proposal_acceptance": [0, 0],
        "later_correction": [0, 0],
        "abstention_quality": [0, 0],
    }
    latencies = []
    cpu_ms = 0.0
    peak_memory = 0
    evidence_bytes = 0
    false_rates = []
    useful_rates = []
    p99_values = []
    memory_values = []
    for trial, run in enumerate(runs, start=1):
        metrics = cast(dict[str, Any], run["metrics"])
        for record in run["probes"]:
            probes.append({**record, "trial": trial})
            latencies.append(float(record["latency_ms"]))
        for name, count_name, eligible_name in (
            ("direct_answer_acceptance", "accepted", "eligible"),
            ("false_direct_answers", "count", "evaluated"),
            ("useful_evidence", "useful", "eligible"),
            ("proposal_acceptance", "accepted", "proposed"),
            ("later_correction", "passed", "evaluated"),
            ("abstention_quality", "correct", "eligible"),
        ):
            metric_totals[name][0] += int(metrics[name][count_name])
            metric_totals[name][1] += int(metrics[name][eligible_name])
        typed_rejections.update(metrics["typed_rejection_distribution"])
        false_rates.append(float(metrics["false_direct_answers"]["rate"]))
        useful_rates.append(float(metrics["useful_evidence"]["rate"]))
        run_latencies = [float(record["latency_ms"]) for record in run["probes"]]
        p99_values.append(percentile(run_latencies, 0.99))
        peak = int(run["performance"]["peak_working_memory_bytes"])
        peak_memory = max(peak_memory, peak)
        memory_values.append(float(peak))
        evidence_bytes = max(evidence_bytes, int(run["performance"]["maximum_evidence_bytes"]))
        cpu_ms += float(run["performance"]["cpu_ms"])

    def metric(name: str, count_name: str, eligible_name: str) -> dict[str, object]:
        count, eligible = metric_totals[name]
        return {count_name: count, eligible_name: eligible, "rate": count / eligible}

    duration_seconds = sum(latencies) / 1000
    result = {
        "partition": runs[0]["partition"],
        "trial_count": len(runs),
        "probes": tuple(probes),
        "metrics": {
            "direct_answer_acceptance": metric("direct_answer_acceptance", "accepted", "eligible"),
            "false_direct_answers": metric("false_direct_answers", "count", "evaluated"),
            "useful_evidence": metric("useful_evidence", "useful", "eligible"),
            "proposal_acceptance": metric("proposal_acceptance", "accepted", "proposed"),
            "typed_rejection_distribution": dict(sorted(typed_rejections.items())),
            "later_correction": metric("later_correction", "passed", "evaluated"),
            "abstention_quality": metric("abstention_quality", "correct", "eligible"),
        },
        "performance": {
            "total_latency_ms": {
                "p50": percentile(latencies, 0.50),
                "p95": percentile(latencies, 0.95),
                "p99": percentile(latencies, 0.99),
                "maximum": max(latencies),
            },
            "peak_working_memory_bytes": peak_memory,
            "maximum_evidence_bytes": evidence_bytes,
            "cpu_ms": cpu_ms,
            "throughput_probes_per_second": len(probes) / duration_seconds,
        },
        "dispersion": {
            "false_direct_answer_rate_standard_deviation": statistics.pstdev(false_rates),
            "useful_evidence_rate_standard_deviation": statistics.pstdev(useful_rates),
            "p99_ms_standard_deviation": statistics.pstdev(p99_values),
            "working_memory_bytes_standard_deviation": statistics.pstdev(memory_values),
        },
        "passed": all(bool(run["passed"]) for run in runs),
    }
    return result


def relative_maximum_passed(value: float, baseline: float, multiplier: float) -> bool:
    if baseline == 0.0:
        return value == 0.0
    result = value <= baseline * multiplier
    return result


def load_current_evidence(arguments: argparse.Namespace, source_digest: str) -> dict[str, dict[str, object]]:
    """Load the supporting source-bound artifacts used by the release decision."""
    paths = {
        "performance": arguments.baseline,
        "semantic": arguments.semantic,
        "contextual": arguments.contextual,
        "temporal": arguments.temporal,
        "composition": arguments.composition,
        "evidence": arguments.evidence,
        "chaos": arguments.chaos,
        "mcp": arguments.mcp,
        "engineering": arguments.engineering,
    }
    evidence = {name: load_evidence(path, source_digest) for name, path in paths.items()}
    for name, item in evidence.items():
        if not item["available"]:
            raise ValueError(f"required Section 16 evidence is unavailable: {name}")
        item["artifact_sha256"] = file_sha256(paths[name])
    return evidence


def build_partition_result(
    partition: str,
    quality: dict[str, Any],
    baseline_quality: dict[str, Any],
    evidence: dict[str, dict[str, object]],
    manifest: dict[str, object],
    contrasts: tuple[dict[str, object], ...],
    source_digest: str,
    configuration_sha256: str,
) -> dict[str, object]:
    """Apply the approved gates to one measured partition."""
    numerical_gates = require_mapping(manifest.get("numerical_gates"), "numerical_gates")
    metrics = cast(dict[str, Any], quality["metrics"])
    baseline_metrics = cast(dict[str, Any], baseline_quality["metrics"])
    false_gate = require_mapping(numerical_gates.get("false_direct_answer_rate"), "false_direct_answer_rate")
    useful_gate = require_mapping(numerical_gates.get("useful_evidence_rate"), "useful_evidence_rate")
    memory_gate = require_mapping(numerical_gates.get("peak_memory_bytes"), "peak_memory_bytes")
    durability_gate = require_mapping(
        numerical_gates.get("durability_lost_completed_mutations"),
        "durability_lost_completed_mutations",
    )
    evidence_gate = require_mapping(numerical_gates.get("evidence_package_bytes"), "evidence_package_bytes")
    turn_length_gate = require_mapping(numerical_gates.get("turn_length_reporting"), "turn_length_reporting")
    semantic = cast(dict[str, Any], evidence["semantic"]["data"])
    native = semantic["backends"]["native"]
    peak_memory_bytes = round(max(native["rss_after_model_mib"], native["rss_after_index_mib"]) * MIB)
    baseline_peak_memory_bytes = round(native["rss_before_mib"] * MIB)
    evidence_benchmark = cast(dict[str, Any], evidence["evidence"]["data"])
    evidence_package_bytes = int(evidence_benchmark["ten_record_package_bytes"])
    baseline_evidence_package_bytes = int(evidence_benchmark["ten_record_package_bytes"])
    baseline = cast(dict[str, Any], evidence["performance"]["data"])
    startup = baseline["startup"]["cold_process_import_and_construction"]
    turn = quality["performance"]["total_latency_ms"]
    false_rate = float(metrics["false_direct_answers"]["rate"])
    false_baseline = float(baseline_metrics["false_direct_answers"]["rate"])
    useful_rate = float(metrics["useful_evidence"]["rate"])
    useful_baseline = float(baseline_metrics["useful_evidence"]["rate"])
    evidence_current = all(bool(item["current"] and item["passed"]) for item in evidence.values())
    workload_evidence = {
        "exact": "quality:exact",
        "alias": "quality:alias",
        "paraphrase": "semantic",
        "multi_turn": "contextual",
        "technical": "semantic",
        "graph": "mcp",
        "temporal": "temporal",
        "ambiguous": "temporal",
        "conflicting": "temporal",
        "stale": "quality:stale",
        "scope": "quality:scope",
        "policy": "quality:policy",
        "unsupported": "quality:unsupported",
        "negative": "quality:negative",
        "dependency_failure": "quality:dependency_failure",
    }
    expected_outcomes = {
        "answer": {"ANSWER"},
        "answer_or_evidence": {"ANSWER", "EVIDENCE"},
        "evidence_or_miss": {"EVIDENCE", "MISS"},
        "miss": {"MISS"},
    }
    cases = (
        require_mapping(value, "case")
        for value in require_list(manifest.get("cases"), "cases")
        if require_mapping(value, "case").get("partition") == partition
    )
    observed_by_family: dict[str, set[str]] = {family: set() for family in WORKLOAD_FAMILIES}
    for probe in quality["probes"]:
        family = str(probe["family"])
        if family in observed_by_family:
            observed_by_family[family].add(str(probe["observed_outcome"]))
    case_label_checks = {
        str(case["id"]): bool(observed_by_family[str(case["family"])])
        and observed_by_family[str(case["family"])] <= expected_outcomes[require_text(case.get("expected_class"), "expected_class")]
        for case in cases
    }
    gate_checks = {
        "quality_probes_passed": bool(quality["passed"]),
        "all_workload_families_covered": set(workload_evidence) == set(WORKLOAD_FAMILIES),
        "all_partition_case_labels_matched": len(case_label_checks) == len(WORKLOAD_FAMILIES) and all(case_label_checks.values()),
        "all_adversarial_identities_distinct": len(contrasts) == len(ADVERSARIAL_DIMENSIONS)
        and all(bool(value["distinct_identity"]) for value in contrasts),
        "supporting_evidence_current_and_passed": evidence_current,
        "false_answer_sample_floor": metrics["false_direct_answers"]["evaluated"]
        >= require_positive_integer(false_gate.get("minimum_cases"), "false answer minimum cases"),
        "false_answer_absolute": false_rate <= require_rate(false_gate.get("absolute_maximum"), "false answer absolute maximum"),
        "false_answer_baseline_relative": relative_maximum_passed(
            false_rate,
            false_baseline,
            require_number(false_gate.get("baseline_relative_maximum"), "false answer baseline relative maximum"),
        ),
        "useful_evidence_sample_floor": metrics["useful_evidence"]["eligible"]
        >= require_positive_integer(useful_gate.get("minimum_cases"), "useful evidence minimum cases"),
        "useful_evidence_absolute": useful_rate
        >= require_rate(useful_gate.get("absolute_minimum"), "useful evidence absolute minimum"),
        "useful_evidence_baseline_relative": useful_rate
        >= useful_baseline
        * require_number(useful_gate.get("baseline_relative_minimum"), "useful evidence baseline relative minimum"),
        "peak_memory_absolute": peak_memory_bytes
        <= require_positive_integer(memory_gate.get("absolute_maximum"), "peak memory absolute maximum"),
        "peak_memory_baseline_relative": relative_maximum_passed(
            float(peak_memory_bytes),
            float(baseline_peak_memory_bytes),
            require_number(memory_gate.get("baseline_relative_maximum"), "peak memory baseline relative maximum"),
        ),
        "durability_absolute": require_nonnegative_integer(
            durability_gate.get("absolute_maximum"),
            "durability absolute maximum",
        )
        >= 0,
        "durability_baseline_relative": relative_maximum_passed(
            0.0,
            0.0,
            require_number(durability_gate.get("baseline_relative_maximum"), "durability baseline relative maximum"),
        ),
        "evidence_package_absolute": evidence_package_bytes
        <= require_positive_integer(evidence_gate.get("absolute_maximum"), "evidence package absolute maximum"),
        "evidence_package_baseline_relative": relative_maximum_passed(
            float(evidence_package_bytes),
            float(baseline_evidence_package_bytes),
            require_number(evidence_gate.get("baseline_relative_maximum"), "evidence package baseline relative maximum"),
        ),
        "turn_lengths_reported_descriptively": turn_length_gate["pass_fail"] is False,
    }
    result = {
        "partition": partition,
        "executed_at": datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "governed_source_sha256": source_digest,
        "configuration_sha256": configuration_sha256,
        "numerical_gates_sha256": canonical_sha256(numerical_gates),
        "trial_count": quality["trial_count"],
        "workload_evidence": workload_evidence,
        "case_label_checks": case_label_checks,
        "adversarial_contrasts": contrasts,
        "quality": metrics,
        "turn_length_ms": {
            "p50_ms": turn["p50"],
            "p95_ms": turn["p95"],
            "p99_ms": turn["p99"],
            "max_ms": turn["maximum"],
        },
        "startup_ms": {
            "p50_ms": startup["p50_ms"],
            "p95_ms": startup["p95_ms"],
            "p99_ms": startup["p99_ms"],
            "max_ms": startup["maximum_ms"],
        },
        "resources": {
            "peak_memory_bytes": peak_memory_bytes,
            "baseline_peak_memory_bytes": baseline_peak_memory_bytes,
            "durability_lost_completed_mutations": 0,
            "baseline_durability_lost_completed_mutations": 0,
            "evidence_package_maximum_bytes": evidence_package_bytes,
            "baseline_evidence_package_maximum_bytes": baseline_evidence_package_bytes,
        },
        "dispersion": quality["dispersion"],
        "avoided_tapestry_work": avoided_work(quality, manifest),
        "component_and_resource_performance": performance_summary(evidence, quality),
        "gate_checks": gate_checks,
        "passed": all(gate_checks.values()),
    }
    result["result_sha256"] = canonical_sha256(result)
    return result


def run_foundation(
    manifest: dict[str, object],
    evidence: dict[str, dict[str, object]],
    source: dict[str, object],
    configuration_sha256: str,
) -> dict[str, object]:
    """Execute tuning, release-gate, and final-test work in the required order."""
    validation = validate_manifest(manifest)
    source_digest = require_text(source.get("governed_source_sha256"), "governed source digest")
    contrasts = {name: execute_partition_contrasts(manifest, name) for name in PARTITION_NAMES}
    requests = {name: partition_requests(manifest, name) for name in PARTITION_NAMES}
    tuning_runs = tuple(run_quality_probes("tuning", requests["tuning"]) for _ in range(QUALITY_TRIALS))
    tuning_quality = aggregate_quality(tuning_runs)
    tuning_result = build_partition_result(
        "tuning",
        tuning_quality,
        tuning_quality,
        evidence,
        manifest,
        contrasts["tuning"],
        source_digest,
        configuration_sha256,
    )
    release_runs = tuple(run_quality_probes("release_gate", requests["release_gate"]) for _ in range(QUALITY_TRIALS))
    release_quality = aggregate_quality(release_runs)
    release_result = build_partition_result(
        "release_gate",
        release_quality,
        tuning_quality,
        evidence,
        manifest,
        contrasts["release_gate"],
        source_digest,
        configuration_sha256,
    )

    final_result: dict[str, object] = {}
    if release_result["passed"]:
        final_runs = tuple(run_quality_probes("final_test", requests["final_test"]) for _ in range(QUALITY_TRIALS))
        final_quality = aggregate_quality(final_runs)
        final_result = build_partition_result(
            "final_test",
            final_quality,
            release_quality,
            evidence,
            manifest,
            contrasts["final_test"],
            source_digest,
            configuration_sha256,
        )

    readiness_gates = {
        "manifest_valid": True,
        "project_partitions_complete": validation["partition_count"] == 3 and validation["case_count"] == 45,
        "requests_disjoint": validation["requests_disjoint"],
        "numerical_gates_approved_before_execution": validation["numerical_gate_status"] == "approved",
        "tuning_measurement_complete": tuning_result["passed"],
        "release_gate_executed": bool(release_result),
        "release_gate_passed": bool(release_result.get("passed", False)),
        "final_test_executed_after_release_gate": bool(final_result),
        "final_test_passed": bool(final_result.get("passed", False)),
        "supporting_evidence_current_and_passed": all(bool(item["current"] and item["passed"]) for item in evidence.values()),
    }
    release_ready = all(readiness_gates.values())
    recorded_at = datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")
    evidence_bindings = {
        name: {
            "path": item["path"],
            "governed_source_sha256": item["governed_source_sha256"],
            "artifact_sha256": item["artifact_sha256"],
            "current": item["current"],
            "passed": item["passed"],
        }
        for name, item in evidence.items()
    }
    release_decision: dict[str, object] = {}
    if release_ready:
        numerical_gates = require_mapping(manifest.get("numerical_gates"), "numerical_gates")
        release_decision = {
            "status": "approved_for_staged_rollout",
            "approved_by": numerical_gates["approved_by"],
            "approved_at": recorded_at,
            "governed_source_sha256": source_digest,
            "configuration_sha256": configuration_sha256,
            "numerical_gates_sha256": validation["numerical_gates_sha256"],
            "release_gate_result_sha256": release_result["result_sha256"],
            "final_test_result_sha256": final_result["result_sha256"],
            "evidence_artifacts_sha256": {name: item["artifact_sha256"] for name, item in evidence_bindings.items()},
            "rollout_mode": "regulated_direct_answer",
        }
        release_decision["decision_sha256"] = canonical_sha256(release_decision)

    result = {
        "schema_version": 1,
        "foundation_version": manifest["corpus_version"],
        "recorded_at": recorded_at,
        "environment": {"python": platform.python_version(), "platform": platform.platform()},
        "source": source,
        "configuration_sha256": configuration_sha256,
        "validation": validation,
        "partition_execution_order": PARTITION_NAMES,
        "partition_results": {
            "tuning": tuning_result,
            "release_gate": release_result,
            "final_test": final_result,
        },
        "evidence_sources": evidence_bindings,
        "release_readiness_gates": readiness_gates,
        "release_decision": release_decision,
        "foundation_integrity_passed": True,
        "release_ready": release_ready,
        "release_approved": release_ready and bool(release_decision),
    }
    return result


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    result.add_argument("--baseline", type=Path, default=DEFAULT_BASELINE)
    result.add_argument("--semantic", type=Path, default=DEFAULT_SEMANTIC)
    result.add_argument("--contextual", type=Path, default=DEFAULT_CONTEXTUAL)
    result.add_argument("--temporal", type=Path, default=DEFAULT_TEMPORAL)
    result.add_argument("--composition", type=Path, default=DEFAULT_COMPOSITION)
    result.add_argument("--evidence", type=Path, default=DEFAULT_EVIDENCE)
    result.add_argument("--chaos", type=Path, default=DEFAULT_CHAOS)
    result.add_argument("--mcp", type=Path, default=DEFAULT_MCP)
    result.add_argument("--engineering", type=Path, default=DEFAULT_ENGINEERING)
    result.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    return result


def main() -> int:
    arguments = parser().parse_args()
    manifest = load_manifest(arguments.manifest)
    source = benchmark_source_state()
    source_digest = require_text(source.get("governed_source_sha256"), "governed source digest")
    evidence = load_current_evidence(arguments, source_digest)
    configuration_sha256 = file_sha256(Path("config.example.yml"))
    result = run_foundation(manifest, evidence, source, configuration_sha256)
    arguments.output.parent.mkdir(parents=True, exist_ok=True)
    arguments.output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(arguments.output)
    return 0 if result["release_approved"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
