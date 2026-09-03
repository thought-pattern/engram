"""Tests for the small namespace rollout control."""

from pytest import mark as pytest_mark, raises as pytest_raises

from engram.config import config_from_dict, config_to_dict, engram_config, graph_config, rollout_config
from engram.constants import ResolutionOutcome, RolloutMode
from engram.core import Engram
from engram.errors import ConflictError
from engram.service import EngramCore

from .test_relation import RelationGraph

EMPTY_NAMESPACES: dict = {}


def core_with_exact_response(mode: RolloutMode, namespaces: dict = EMPTY_NAMESPACES) -> tuple[EngramCore, str]:
    config = engram_config(rollout=rollout_config(default_mode=mode, namespaces=namespaces))
    core = EngramCore(Engram(config))
    learned = core.learn_response(
        "What does Sarah like?",
        "Sarah likes sushi.",
        "rollout-learn",
        namespace="tenant-a",
    )
    result = core, learned["statement_id"]
    return result


def resolve_exact(core: EngramCore, request_id: str, namespace: str = "tenant-a") -> dict:
    result = core.resolve_request(
        "What does Sarah like?",
        request_id,
        namespace=namespace,
        configured_resolvers=("exact",),
        accept_exact=True,
    )
    return result


def test_rollout_config_round_trip_and_fixed_cardinality_status() -> None:
    config = engram_config(
        rollout=rollout_config(
            policy_version="candidate-7",
            default_mode=RolloutMode.EVIDENCE_ONLY,
            namespaces={"tenant-a": RolloutMode.SHADOW, "tenant-b": RolloutMode.DISABLED},
        )
    )

    restored = config_from_dict(config_to_dict(config))
    status = EngramCore(Engram(restored)).status()["rollout"]

    assert restored == config
    assert status == {
        "policy_version": "candidate-7",
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
    core, statement_id = core_with_exact_response(RolloutMode.REGULATED_DIRECT_ANSWER)

    result = resolve_exact(core, "regulated-request")
    artifact = core.engram.response_repository.get_artifact(statement_id)

    assert result["outcome"] == ResolutionOutcome.ANSWER
    assert result["selected_candidate"]["response"] == "Sarah likes sushi."
    assert artifact["statistics"]["query_count"] == 1
    assert artifact["statistics"]["hit_count"] == 1


@pytest_mark.parametrize("mode", [RolloutMode.EVIDENCE_ONLY, RolloutMode.ROLLBACK])
def test_non_direct_rollout_modes_retain_exact_candidate_without_credit(mode: RolloutMode) -> None:
    core, statement_id = core_with_exact_response(mode)

    result = resolve_exact(core, f"{mode.value}-request")
    artifact = core.engram.response_repository.get_artifact(statement_id)

    assert result["outcome"] == ResolutionOutcome.EVIDENCE
    assert result["selected_candidate_available"] is False
    assert result["response_candidates"][0]["response"] == "Sarah likes sushi."
    assert f"rollout_{mode.value}" in result["reason_codes"]
    assert artifact["statistics"]["query_count"] == 1
    assert artifact["statistics"]["hit_count"] == 0


def test_shadow_executes_without_disclosing_or_credentialing_candidates() -> None:
    core, statement_id = core_with_exact_response(RolloutMode.SHADOW)

    result = resolve_exact(core, "shadow-request")
    artifact = core.engram.response_repository.get_artifact(statement_id)

    assert result["outcome"] == ResolutionOutcome.MISS
    assert result["response_candidates"] == ()
    assert result["resolver_results"] == ()
    assert result["frame_diagnostics"]["rollout"]["shadow_outcome"] == "ANSWER"
    assert artifact["statistics"]["query_count"] == 1
    assert artifact["statistics"]["hit_count"] == 0


def test_disabled_executes_resolution_without_disclosing_and_namespace_override_is_exact() -> None:
    core, statement_id = core_with_exact_response(
        RolloutMode.REGULATED_DIRECT_ANSWER,
        {"tenant-a": RolloutMode.DISABLED},
    )

    result = resolve_exact(core, "disabled-request")
    artifact = core.engram.response_repository.get_artifact(statement_id)

    assert result["outcome"] == ResolutionOutcome.MISS
    assert "rollout_disabled" in result["reason_codes"]
    assert result["resolver_results"] == ()
    assert result["frame_diagnostics"]["rollout"]["namespace_override"] is True
    assert result["frame_diagnostics"]["rollout"]["observed_outcome"] == "ANSWER"
    assert artifact["statistics"]["query_count"] == 1
    assert artifact["statistics"]["hit_count"] == 0


def test_rollout_policy_is_part_of_retry_identity() -> None:
    core, _ = core_with_exact_response(RolloutMode.EVIDENCE_ONLY)
    first = resolve_exact(core, "policy-versioned-request")
    core.engram.config["rollout"]["policy_version"] = "rollout-v2"

    with pytest_raises(ConflictError, match="different input"):
        resolve_exact(core, "policy-versioned-request")

    assert first["outcome"] == ResolutionOutcome.EVIDENCE


@pytest_mark.parametrize("mode", tuple(RolloutMode))
def test_enabled_graph_is_consulted_in_every_rollout_mode(mode: RolloutMode, monkeypatch) -> None:
    graph = RelationGraph()
    monkeypatch.setattr("engram.core.connect_graph", lambda **internal_kwargs: graph)
    config = engram_config(
        graph=graph_config(enabled=True, deployment_mode="tapestry_managed"),
        rollout=rollout_config(default_mode=mode),
    )
    core = EngramCore(Engram(config))
    core.learn_response(
        "Where was Ada Lovelace born?",
        "A cached response that must not bypass graph recall.",
        f"graph-exact-{mode.value}",
    )

    core.resolve_request(
        "Where was Ada Lovelace born?",
        f"graph-{mode.value}",
        configured_resolvers=("exact",),
        accept_exact=True,
    )

    assert graph.one_hop_calls == [("entity:ada-lovelace", "predicate:birth-place", 10, False)]
    graph_metrics = core.status()["telemetry"]["graph_recall"]
    assert graph_metrics["consultations"] > 0
    assert graph_metrics["hits"] > 0
