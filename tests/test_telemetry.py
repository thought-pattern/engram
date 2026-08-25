"""Bounded Section 15 operational telemetry tests."""

import json

import pytest

from engram import service as service_module
from engram.constants import ResolutionOutcome, ResolverState
from engram.core import Engram
from engram.errors import InvalidRequestError, PersistenceError
from engram.resolution import budget_consumption, empty_candidate, resolution_result, resolver_result
from engram.service import EngramCore
from engram.telemetry import operational_telemetry, record_resolution, telemetry_snapshot


def test_resolution_telemetry_aggregates_fixed_outcomes_contributions_and_resources() -> None:
    request = "Does Sarah like private sushi?"
    namespace = "private-customer-namespace"
    core = EngramCore()
    learned = core.learn_response(request, "Sarah likes sushi.", "telemetry-learn", user_id="Sarah", namespace=namespace)

    answer = core.resolve_request(
        request,
        "telemetry-answer",
        user_id="Sarah",
        namespace=namespace,
        configured_resolvers=("exact",),
        accept_exact=True,
    )
    replay = core.resolve_request(
        request,
        "telemetry-answer",
        user_id="Sarah",
        namespace=namespace,
        configured_resolvers=("exact",),
        accept_exact=True,
    )
    miss = core.resolve_request(
        "Does Sarah like private dogs?",
        "telemetry-miss",
        user_id="Sarah",
        namespace=namespace,
        configured_resolvers=("exact",),
    )
    feedback = core.record_resolution_feedback(
        "telemetry-answer",
        "telemetry-feedback",
        "rejected_context",
        learned["statement_id"],
        "private feedback detail",
    )
    telemetry = core.status()["telemetry"]

    assert answer["outcome"] == ResolutionOutcome.ANSWER
    assert replay == answer
    assert miss["outcome"] == ResolutionOutcome.MISS
    assert feedback["idempotent"] is False
    assert telemetry["resolution"]["requests"] == 3
    assert telemetry["resolution"]["executions"] == 2
    assert telemetry["resolution"]["replays"] == 1
    assert telemetry["resolution"]["outcomes"] == {"ANSWER": 2, "EVIDENCE": 0, "MISS": 1}
    assert sum(telemetry["resolution"]["latency"]["buckets"].values()) == 2
    assert telemetry["resolution"]["resources"]["output_bytes"]["total"] > 0
    assert telemetry["resolvers"]["exact"]["invocations"] == 2
    assert telemetry["resolvers"]["exact"]["candidate_contributions"] == 1
    assert telemetry["resolvers"]["exact"]["selected_contributions"] == 1
    assert telemetry["regulator_outcomes"]["rejected_context"] == 1

    encoded = json.dumps(telemetry, sort_keys=True)
    for sensitive in (request, namespace, "Sarah", learned["statement_id"], "private feedback detail"):
        assert sensitive not in encoded


def test_unknown_resolver_and_exhaustion_values_collapse_into_fixed_other_buckets() -> None:
    telemetry = operational_telemetry()
    result = resolution_result(
        outcome=ResolutionOutcome.MISS,
        selected_candidate=empty_candidate(),
        selected_candidate_available=False,
        response_candidates=(),
        evidence=(),
        confidence=0.0,
        confidence_available=False,
        reason_codes=("insufficient_knowledge",),
        frame_diagnostics={},
        resolver_results=(
            resolver_result(
                "private-user-derived-resolver",
                ResolverState.EXHAUSTED,
                reason_code="private-user-derived-reason",
                consumption=budget_consumption(
                    elapsed_ns=5,
                    resolvers=1,
                    exhausted_dimensions=("private-user-derived-dimension",),
                ),
            ),
        ),
        budget=budget_consumption(
            elapsed_ns=7,
            resolvers=1,
            exhausted_dimensions=("private-user-derived-dimension",),
        ),
    )

    record_resolution(telemetry, result, replayed=False)
    snapshot = telemetry_snapshot(telemetry)

    assert snapshot["resolvers"]["other"]["invocations"] == 1
    assert snapshot["resolvers"]["other"]["states"]["exhausted"] == 1
    assert snapshot["resolution"]["budget_exhaustion"]["other"] == 1
    assert "private-user-derived" not in json.dumps(snapshot, sort_keys=True)


def test_rebuild_telemetry_records_success_failure_and_dry_run() -> None:
    engine = Engram()

    engine.rebuild_indexes(apply=False)
    engine.rebuild_indexes(apply=True)
    engine.rebuild_sparse_index()
    with pytest.raises(InvalidRequestError, match="semantic index is unavailable"):
        engine.rebuild_semantic_index()
    telemetry = engine.operational_telemetry_snapshot()

    assert telemetry["rebuilds"]["primary"]["attempts"] == 2
    assert telemetry["rebuilds"]["primary"]["dry_runs"] == 1
    assert telemetry["rebuilds"]["primary"]["successes"] == 1
    assert telemetry["rebuilds"]["sparse"]["successes"] == 1
    assert telemetry["rebuilds"]["semantic"]["failures"] == 1
    assert all(metrics["latency"]["observations"] == metrics["attempts"] for metrics in telemetry["rebuilds"].values())


def test_durability_telemetry_and_status_redact_checkpoint_error_content(tmp_path, monkeypatch) -> None:
    secret = "private path and credential detail"
    store = tmp_path / "engram.json"
    core = EngramCore(Engram(), store_path=store, checkpoint_on_mutation=False)
    core.add_fact("Pending telemetry state.")
    real_save = service_module.persistence.save

    def fail_save(_engram, _path) -> None:
        raise OSError(secret)

    monkeypatch.setattr(service_module.persistence, "save", fail_save)
    with pytest.raises(PersistenceError):
        core.flush()
    degraded = core.status()

    assert degraded["last_persistence_error"] == "OSError"
    assert secret not in json.dumps(degraded, sort_keys=True)
    assert degraded["telemetry"]["durability"]["checkpoint_attempts"] == 1
    assert degraded["telemetry"]["durability"]["checkpoint_failures"] == 1
    assert degraded["telemetry"]["durability"]["current_state"] == "degraded"

    monkeypatch.setattr(service_module.persistence, "save", real_save)
    assert core.flush() is True
    recovered = core.status()["telemetry"]["durability"]
    assert recovered["checkpoint_attempts"] == 2
    assert recovered["checkpoint_successes"] == 1
    assert recovered["current_state"] == "healthy"
