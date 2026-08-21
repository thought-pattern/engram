"""Section 16 evaluation-foundation partition and adversarial gates."""

from copy import deepcopy
from pathlib import Path

import pytest

from eval.run_release_gate_foundation import load_manifest, run_foundation, validate_manifest

REPOSITORY = Path(__file__).resolve().parent.parent
MANIFEST = REPOSITORY / "eval" / "release-gate-foundation-v1.json"


def mapping(value: object) -> dict[str, object]:
    assert isinstance(value, dict)
    return value


def sequence(value: object) -> tuple[object, ...]:
    assert isinstance(value, (list, tuple))
    result = tuple(value)
    return result


def test_release_gate_foundation_executes_all_tuning_collision_dimensions() -> None:
    result = run_foundation(load_manifest(MANIFEST))
    contrasts = sequence(result["tuning_contrasts"])

    assert len(contrasts) == 5
    assert {mapping(value)["dimension"] for value in contrasts} == {
        "when_where",
        "current_historical",
        "positive_negative",
        "relation",
        "scope",
    }
    assert all(mapping(value)["distinct_identity"] for value in contrasts)
    assert result["foundation_integrity_passed"] is True
    assert result["release_ready"] is False
    assert sequence(result["unprovisioned_partitions"]) == ("release_gate", "final_test")
    readiness = mapping(result["release_readiness_gates"])
    assert readiness["false_answer_sample_floor_met"] is False
    assert readiness["useful_evidence_sample_floor_met"] is False


def test_release_gate_foundation_rejects_protected_partition_content() -> None:
    manifest = deepcopy(load_manifest(MANIFEST))
    cases = sequence(manifest["cases"])
    leaked = deepcopy(mapping(cases[0]))
    leaked["id"] = "release-leak-001"
    leaked["partition"] = "release_gate"
    assert isinstance(manifest["cases"], list)
    manifest["cases"].append(leaked)

    with pytest.raises(ValueError, match="must not include protected partition case content"):
        validate_manifest(manifest)
