"""Tests for the small namespace rollout control."""

from pytest import mark as pytest_mark, raises as pytest_raises

from engram.config import engram_config, rollout_config
from engram.constants import ResolutionOutcome, RolloutMode
from engram.core import Engram
from engram.errors import ConflictError
from engram.service import EngramCore


def resolve_exact(core: EngramCore, request_id: str, namespace: str = "tenant-a") -> dict:
    result = core.resolve_request(
        "What does Sarah like?",
        request_id,
        namespace=namespace,
        configured_resolvers=("exact",),
        accept_exact=True,
    )
    return result


def test_rollout_status_has_fixed_cardinality() -> None:
    config = engram_config(
        rollout=rollout_config(
            default_mode=RolloutMode.EVIDENCE_ONLY,
            namespaces={"tenant-a": RolloutMode.SHADOW, "tenant-b": RolloutMode.DISABLED},
        )
    )

    status = EngramCore(Engram(config)).status().get("rollout", {})

    assert status == {
        "default_mode": "evidence_only",
        "namespace_override_count": 2,
        "namespace_modes": {
            "disabled": 1,
            "shadow": 1,
            "evidence_only": 0,
            "regulated_direct_answer": 0,
            "rollback": 0,
        },
    }


def test_regulated_direct_answer_preserves_current_behavior() -> None:
    core = EngramCore(Engram(engram_config(rollout=rollout_config(default_mode=RolloutMode.REGULATED_DIRECT_ANSWER))))
    learned = core.learn_response("What does Sarah like?", "Sarah likes sushi.", "rollout-learn", namespace="tenant-a")
    statement_id = learned.get("statement_id", "")
    assert statement_id

    result = resolve_exact(core, "regulated-request")
    statistics = core.engram.response_repository.get_artifact(statement_id).get("statistics", {})

    assert result.get("outcome", ResolutionOutcome.MISS) == ResolutionOutcome.ANSWER
    assert result.get("selected_candidate", {}).get("response", "") == "Sarah likes sushi."
    assert statistics.get("query_count", 0) == 1
    assert statistics.get("hit_count", 0) == 1


@pytest_mark.parametrize("mode", [RolloutMode.EVIDENCE_ONLY, RolloutMode.ROLLBACK])
def test_non_direct_rollout_modes_retain_exact_candidate_without_credit(mode: RolloutMode) -> None:
    core = EngramCore(Engram(engram_config(rollout=rollout_config(default_mode=mode))))
    learned = core.learn_response("What does Sarah like?", "Sarah likes sushi.", "rollout-learn", namespace="tenant-a")
    statement_id = learned.get("statement_id", "")
    assert statement_id

    result = resolve_exact(core, f"{mode.value}-request")
    statistics = core.engram.response_repository.get_artifact(statement_id).get("statistics", {})
    candidates = result.get("response_candidates", ())

    assert result.get("outcome", ResolutionOutcome.MISS) == ResolutionOutcome.EVIDENCE
    assert "selected_candidate_available" in result
    assert result.get("selected_candidate_available", False) is False
    assert candidates[0].get("response", "") == "Sarah likes sushi."
    assert f"rollout_{mode.value}" in result.get("reason_codes", ())
    assert statistics.get("query_count", 0) == 1
    assert "hit_count" in statistics
    assert statistics.get("hit_count", 0) == 0


def test_shadow_executes_without_disclosing_or_credentialing_candidates() -> None:
    core = EngramCore(Engram(engram_config(rollout=rollout_config(default_mode=RolloutMode.SHADOW))))
    learned = core.learn_response("What does Sarah like?", "Sarah likes sushi.", "rollout-learn", namespace="tenant-a")
    statement_id = learned.get("statement_id", "")
    assert statement_id

    result = resolve_exact(core, "shadow-request")
    statistics = core.engram.response_repository.get_artifact(statement_id).get("statistics", {})

    assert {"outcome", "response_candidates", "resolver_results"} <= result.keys()
    assert result.get("outcome", ResolutionOutcome.MISS) == ResolutionOutcome.MISS
    assert result.get("response_candidates", ()) == ()
    assert result.get("resolver_results", ()) == ()
    assert result.get("frame_diagnostics", {}).get("rollout", {}).get("shadow_outcome", "") == "ANSWER"
    assert statistics.get("query_count", 0) == 1
    assert "hit_count" in statistics
    assert statistics.get("hit_count", 0) == 0


def test_disabled_observes_without_disclosing_and_namespace_override_is_exact() -> None:
    rollout = rollout_config(
        default_mode=RolloutMode.REGULATED_DIRECT_ANSWER,
        namespaces={"tenant-a": RolloutMode.DISABLED},
    )
    core = EngramCore(Engram(engram_config(rollout=rollout)))
    learned = core.learn_response("What does Sarah like?", "Sarah likes sushi.", "rollout-learn", namespace="tenant-a")
    statement_id = learned.get("statement_id", "")
    assert statement_id

    result = resolve_exact(core, "disabled-request")
    statistics = core.engram.response_repository.get_artifact(statement_id).get("statistics", {})
    rollout_diagnostics = result.get("frame_diagnostics", {}).get("rollout", {})

    assert {"outcome", "response_candidates", "resolver_results"} <= result.keys()
    assert result.get("outcome", ResolutionOutcome.MISS) == ResolutionOutcome.MISS
    assert result.get("reason_codes", ())[-1] == "rollout_disabled"
    assert result.get("response_candidates", ()) == ()
    assert result.get("resolver_results", ()) == ()
    assert rollout_diagnostics.get("namespace_override", False) is True
    assert rollout_diagnostics.get("observed_outcome", "") == "ANSWER"
    assert "hit_count" in statistics
    assert statistics.get("hit_count", 0) == 0


def test_rollout_mode_is_part_of_retry_identity() -> None:
    core = EngramCore(Engram(engram_config(rollout=rollout_config(default_mode=RolloutMode.EVIDENCE_ONLY))))
    core.learn_response("What does Sarah like?", "Sarah likes sushi.", "rollout-learn", namespace="tenant-a")
    first = resolve_exact(core, "mode-changed-request")
    rollout = core.engram.config.get("rollout", {})
    assert "default_mode" in rollout
    rollout["default_mode"] = RolloutMode.SHADOW

    with pytest_raises(ConflictError, match="different input"):
        resolve_exact(core, "mode-changed-request")

    assert first.get("outcome", ResolutionOutcome.MISS) == ResolutionOutcome.EVIDENCE
