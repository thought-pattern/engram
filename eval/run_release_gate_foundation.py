"""Validate the public tuning foundation and protected-partition custody slots.

Release-gate and final-test content must not be committed to this repository.
Their independent custodians provision and authorize those partitions later.
"""

import argparse
import json
import platform
import sys
from datetime import UTC, datetime
from pathlib import Path

REPOSITORY = Path(__file__).resolve().parent.parent
if str(REPOSITORY) not in sys.path:
    sys.path.insert(0, str(REPOSITORY))

from engram.identity import build_standalone_identity, query_identity_to_json, scope_key
from scripts.benchmark_metadata import benchmark_source_state

DEFAULT_MANIFEST = REPOSITORY / "eval" / "release-gate-foundation-v1.json"
DEFAULT_OUTPUT = REPOSITORY / "documentation" / "evaluation" / "foundation-2026-08-19.json"
PARTITION_NAMES = ("tuning", "release_gate", "final_test")
ADVERSARIAL_DIMENSIONS = ("when_where", "current_historical", "positive_negative", "relation", "scope")


def load_manifest(path: Path = DEFAULT_MANIFEST) -> dict[str, object]:
    """Load one concrete evaluation-foundation manifest."""
    decoded = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(decoded, dict):
        raise ValueError("evaluation manifest must be an object")
    result = decoded
    return result


def _require_list(value: object, name: str) -> list[object]:
    if not isinstance(value, list):
        raise ValueError(f"{name} must be an array")
    result = value
    return result


def _require_mapping(value: object, name: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ValueError(f"{name} must be an object")
    result = value
    return result


def _require_text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{name} must be a nonempty string")
    result = value
    return result


def _require_positive_integer(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{name} must be a positive integer")
    result = value
    return result


def validate_manifest(manifest: dict[str, object]) -> dict[str, object]:
    """Validate tuning coverage, protected custody, and proposed gates."""
    if manifest.get("schema_version") != 1:
        raise ValueError("evaluation manifest schema_version must be 1")
    if manifest.get("corpus_version") != "engram-release-gate-foundation-v1":
        raise ValueError("evaluation corpus_version is unsupported")

    partition_records = _require_list(manifest.get("partitions", {}), "partitions")
    partitions = tuple(_require_mapping(value, "partition") for value in partition_records)
    partition_names = tuple(_require_text(value.get("name", ""), "partition name") for value in partitions)
    if partition_names != PARTITION_NAMES:
        raise ValueError("evaluation partitions must be tuning, release_gate, and final_test in order")
    expected_custody = {
        "tuning": ("open_engineering", True, True),
        "release_gate": ("awaiting_independent_custodian", False, False),
        "final_test": ("awaiting_independent_custodian", False, False),
    }
    for partition in partitions:
        name = _require_text(partition.get("name", ""), "partition name")
        actual = (
            _require_text(partition.get("custody_status", ""), "partition custody_status"),
            partition.get("content_available"),
            partition.get("execution_authorized"),
        )
        if actual != expected_custody[name]:
            raise ValueError(f"{name} partition custody metadata is invalid")

    families = tuple(
        _require_text(value, "workload family")
        for value in _require_list(manifest.get("workload_families", {}), "workload_families")
    )
    if len(families) != len(set(families)) or not families:
        raise ValueError("workload families must be nonempty and unique")

    cases = _require_list(manifest.get("cases", {}), "cases")
    case_ids = []
    request_owners = {}
    coverage = {name: set() for name in PARTITION_NAMES}
    for raw_case in cases:
        case = _require_mapping(raw_case, "case")
        case_id = _require_text(case.get("id", ""), "case id")
        partition = _require_text(case.get("partition", ""), "case partition")
        family = _require_text(case.get("family", ""), "case family")
        request = _require_text(case.get("request", ""), "case request")
        _require_text(case.get("expected_class", ""), "expected_class")
        _require_text(case.get("label_source", ""), "label_source")
        if partition not in PARTITION_NAMES or family not in families:
            raise ValueError("case partition and family must use declared values")
        if partition != "tuning":
            raise ValueError("repository manifest must not include protected partition case content")
        normalized_request = " ".join(request.casefold().split())
        if normalized_request in request_owners and request_owners[normalized_request] != partition:
            raise ValueError("normalized requests must not cross evaluation partitions")
        request_owners[normalized_request] = partition
        case_ids.append(case_id)
        coverage[partition].add(family)
    if len(case_ids) != len(set(case_ids)):
        raise ValueError("evaluation case identifiers must be unique")
    if coverage["tuning"] != set(families):
        raise ValueError("tuning partition must cover every declared workload family")
    if coverage["release_gate"] or coverage["final_test"]:
        raise ValueError("protected partition case content must remain outside the repository")

    contrasts = _require_list(manifest.get("adversarial_contrasts", {}), "adversarial_contrasts")
    contrast_ids = []
    contrast_coverage = {name: set() for name in PARTITION_NAMES}
    for raw_contrast in contrasts:
        contrast = _require_mapping(raw_contrast, "adversarial contrast")
        contrast_id = _require_text(contrast.get("id", ""), "contrast id")
        partition = _require_text(contrast.get("partition", ""), "contrast partition")
        dimension = _require_text(contrast.get("dimension", ""), "contrast dimension")
        _require_mapping(contrast.get("left", {}), "contrast left")
        _require_mapping(contrast.get("right", {}), "contrast right")
        if partition not in PARTITION_NAMES or dimension not in ADVERSARIAL_DIMENSIONS:
            raise ValueError("contrast partition and dimension must use declared values")
        if partition != "tuning":
            raise ValueError("repository manifest must not include protected partition contrast content")
        contrast_ids.append(contrast_id)
        contrast_coverage[partition].add(dimension)
    if len(contrast_ids) != len(set(contrast_ids)):
        raise ValueError("adversarial contrast identifiers must be unique")
    if contrast_coverage["tuning"] != set(ADVERSARIAL_DIMENSIONS):
        raise ValueError("tuning partition must cover every adversarial dimension")
    if contrast_coverage["release_gate"] or contrast_coverage["final_test"]:
        raise ValueError("protected partition contrasts must remain outside the repository")

    numerical_gates = _require_mapping(manifest.get("numerical_gates", {}), "numerical_gates")
    if numerical_gates.get("status") != "proposed_pending_release_owner_approval":
        raise ValueError("initial numerical gates must remain pending release-owner approval")
    if numerical_gates.get("approved_by") != "":
        raise ValueError("unapproved numerical gates must have an empty approved_by field")
    turn_lengths = _require_mapping(numerical_gates.get("turn_length_reporting", {}), "turn_length_reporting")
    turn_length_metrics = _require_list(turn_lengths.get("metrics", []), "turn_length_reporting metrics")
    if turn_length_metrics != ["p50_ms", "p95_ms", "p99_ms", "max_ms"] or turn_lengths.get("pass_fail") is not False:
        raise ValueError("turn lengths must be reported without a pass/fail threshold")
    false_answer_gate = _require_mapping(numerical_gates.get("false_direct_answer_rate", {}), "false_direct_answer_rate")
    useful_evidence_gate = _require_mapping(numerical_gates.get("useful_evidence_rate", {}), "useful_evidence_rate")
    false_answer_minimum = _require_positive_integer(
        false_answer_gate.get("minimum_cases"),
        "false_direct_answer_rate minimum_cases",
    )
    useful_evidence_minimum = _require_positive_integer(
        useful_evidence_gate.get("minimum_cases"),
        "useful_evidence_rate minimum_cases",
    )

    result = {
        "partition_count": len(partition_names),
        "case_count": len(case_ids),
        "workload_family_count": len(families),
        "contrast_count": len(contrast_ids),
        "adversarial_dimension_count": len(ADVERSARIAL_DIMENSIONS),
        "partition_coverage": {name: tuple(sorted(coverage[name])) for name in PARTITION_NAMES},
        "contrast_coverage": {name: tuple(sorted(contrast_coverage[name])) for name in PARTITION_NAMES},
        "protected_partition_content_absent": not coverage["release_gate"]
        and not coverage["final_test"]
        and not contrast_coverage["release_gate"]
        and not contrast_coverage["final_test"],
        "protected_partition_custody": {
            name: _require_text(partitions[index].get("custody_status", ""), "partition custody_status")
            for index, name in enumerate(PARTITION_NAMES)
            if name != "tuning"
        },
        "numerical_gate_status": numerical_gates["status"],
        "false_answer_minimum_cases": false_answer_minimum,
        "useful_evidence_minimum_cases": useful_evidence_minimum,
        "turn_length_metrics": tuple(turn_length_metrics),
        "turn_length_pass_fail": turn_lengths["pass_fail"],
    }
    return result


def execute_tuning_contrasts(manifest: dict[str, object]) -> tuple[dict[str, object], ...]:
    """Execute only public tuning identity contrasts."""
    results = []
    contrasts = _require_list(manifest.get("adversarial_contrasts", {}), "adversarial_contrasts")
    for raw_contrast in contrasts:
        contrast = _require_mapping(raw_contrast, "adversarial contrast")
        if contrast.get("partition") != "tuning":
            continue
        left = _require_mapping(contrast.get("left", {}), "contrast left")
        right = _require_mapping(contrast.get("right", {}), "contrast right")
        left_scope = scope_key(
            _require_text(left.get("namespace", ""), "left namespace"),
            _require_text(left.get("context_fingerprint", ""), "left context_fingerprint"),
        )
        right_scope = scope_key(
            _require_text(right.get("namespace", ""), "right namespace"),
            _require_text(right.get("context_fingerprint", ""), "right context_fingerprint"),
        )
        left_identity = build_standalone_identity(_require_text(left.get("request", ""), "left request"), left_scope)
        right_identity = build_standalone_identity(_require_text(right.get("request", ""), "right request"), right_scope)
        distinct = query_identity_to_json(left_identity) != query_identity_to_json(right_identity)
        results.append(
            {
                "id": contrast["id"],
                "dimension": contrast["dimension"],
                "partition": "tuning",
                "distinct_identity": distinct,
            }
        )
    result = tuple(results)
    return result


def run_foundation(manifest: dict[str, object]) -> dict[str, object]:
    """Build a non-release result without claiming protected evidence exists."""
    validation = validate_manifest(manifest)
    contrasts = execute_tuning_contrasts(manifest)
    case_count = _require_positive_integer(validation["case_count"], "validated case_count")
    false_answer_minimum = _require_positive_integer(
        validation["false_answer_minimum_cases"],
        "validated false_answer_minimum_cases",
    )
    useful_evidence_minimum = _require_positive_integer(
        validation["useful_evidence_minimum_cases"],
        "validated useful_evidence_minimum_cases",
    )
    integrity_gates = {
        "partition_structure_valid": validation["partition_count"] == 3,
        "all_tuning_workload_families_covered": validation["workload_family_count"] == 15,
        "all_tuning_adversarial_dimensions_declared": validation["adversarial_dimension_count"] == 5,
        "all_tuning_adversarial_identities_distinct": bool(contrasts) and all(value["distinct_identity"] for value in contrasts),
        "protected_partition_content_absent": validation["protected_partition_content_absent"],
        "numerical_gates_pending_approval": validation["numerical_gate_status"] == "proposed_pending_release_owner_approval",
    }
    release_readiness_gates = {
        "release_gate_partition_independently_provisioned": False,
        "final_test_partition_independently_provisioned": False,
        "numerical_gates_approved": False,
        "false_answer_sample_floor_met": case_count >= false_answer_minimum,
        "useful_evidence_sample_floor_met": case_count >= useful_evidence_minimum,
    }
    result = {
        "schema_version": 1,
        "foundation_version": manifest["corpus_version"],
        "recorded_at": datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "environment": {"python": platform.python_version(), "platform": platform.platform()},
        "source": benchmark_source_state(),
        "validation": validation,
        "executed_partitions": ("tuning",),
        "unprovisioned_partitions": ("release_gate", "final_test"),
        "tuning_contrasts": contrasts,
        "integrity_gates": integrity_gates,
        "release_readiness_gates": release_readiness_gates,
        "foundation_integrity_passed": all(integrity_gates.values()),
        "release_ready": all(release_readiness_gates.values()),
        "release_approved": False,
    }
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    arguments = parser.parse_args()
    result = run_foundation(load_manifest(arguments.manifest))
    arguments.output.parent.mkdir(parents=True, exist_ok=True)
    arguments.output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(arguments.output)
    exit_code = 0 if result["foundation_integrity_passed"] else 1
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
