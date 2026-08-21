"""Section 6 feedback-learning and negative-resolution conformance."""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from typing import Any, cast

import pytest

from engram import persistence, service as service_module
from engram.artifacts import (
    CachedResponseArtifact,
    LifecycleState,
    artifact_provenance,
    artifact_statistics,
    cached_response_artifact,
)
from engram.constants import Tier
from engram.core import Engram
from engram.errors import ConflictError, InvalidRequestError, PersistenceError
from engram.feedback import (
    FeedbackObservation,
    FeedbackObservationKind,
    FeedbackOutcome,
    FeedbackReferenceKind,
    FeedbackStore,
    LifecycleHandoffStatus,
    NegativeResolutionKey,
    NegativeResolutionStore,
    canonical_fingerprint,
    constraint_fingerprint,
    feedback_history_from_json,
    feedback_history_to_json,
    feedback_observation,
    feedback_observation_from_dict,
    feedback_observation_from_json,
    feedback_observation_relationship_key,
    feedback_observation_statement_key,
    feedback_observation_to_dict,
    feedback_observation_to_json,
    feedback_observation_with_changes,
    feedback_policy,
    feedback_state,
    feedback_state_signature,
    feedback_statistics,
    feedback_statistics_from_dict,
    feedback_statistics_from_json,
    feedback_statistics_to_dict,
    feedback_statistics_to_json,
    negative_resolution_key,
    negative_resolution_key_from_json,
    negative_resolution_key_to_json,
    negative_resolution_key_with_changes,
    relationship_feedback_key_from_json,
    relationship_feedback_key_to_json,
    statement_feedback_key_from_json,
    statement_feedback_key_to_json,
)
from engram.identity import build_retrieval_representation, build_standalone_identity, scope_key
from engram.mutations import mutation_receipt_to_dict
from engram.repository import ArtifactRepository
from engram.resolution import ResolutionOutcome, resolution_budget
from engram.service import EngramCore, open_engram_core

NOW = datetime(2026, 8, 15, 12, 0, tzinfo=UTC)
NOW_TEXT = "2026-08-15T12:00:00Z"
POLICY_FINGERPRINT = "a" * 64


def observation(
    request_id: str,
    outcome: FeedbackOutcome,
    *,
    statement_id: str = "stmt-1",
    namespace: str = "tenant-a",
    context_fingerprint: str = "account:1",
    observed_at: str = NOW_TEXT,
    generation: int = 3,
    reason: str = "",
) -> FeedbackObservation:
    scope = scope_key(namespace=namespace, context_fingerprint=context_fingerprint)
    identity = build_standalone_identity("What is Engram?", scope)
    result = feedback_observation(
        reference_kind=FeedbackReferenceKind.RESOLUTION_REQUEST,
        reference_id="resolution-1",
        kind=(FeedbackObservationKind.CANDIDACY if outcome == FeedbackOutcome.CANDIDATE else FeedbackObservationKind.VERDICT),
        outcome=outcome,
        query_identity=identity,
        scope=scope,
        constraint_fingerprint=constraint_fingerprint("UNKNOWN", {}, ""),
        statement_id=statement_id,
        generation=generation,
        generation_available=True,
        policy_fingerprint=POLICY_FINGERPRINT,
        observed_at=observed_at,
        reason=reason,
    )
    return result


def test_feedback_statistics_codec_preserves_the_complete_schema() -> None:
    statistics = feedback_statistics(candidate_count=3, accept_count=2, rejected_quality=1)
    encoded = feedback_statistics_to_dict(statistics)

    assert encoded == {
        "schema_version": 1,
        "candidate_count": 3,
        "accept_count": 2,
        "rejected_quality": 1,
        "rejected_context": 0,
        "rejected_stale": 0,
        "rejected_policy": 0,
    }
    assert feedback_statistics_from_dict(encoded) == statistics
    assert feedback_statistics_from_json(feedback_statistics_to_json(statistics)) == statistics


def apply(store: FeedbackStore, request_id: str, *values: FeedbackObservation, status=LifecycleHandoffStatus.NOT_APPLICABLE):
    candidate = store.prepare(request_id, tuple(values), status)
    if not candidate["replayed"]:
        store.replace_from_snapshot(candidate["after"])
    return candidate


def artifact(statement_id: str = "stmt-artifact") -> CachedResponseArtifact:
    scope = scope_key(namespace="tenant-a")
    request = "What is Engram?"
    result = cached_response_artifact(
        statement_id=statement_id,
        generation=1,
        response="Engram is a regulated memory system.",
        query_identity=build_standalone_identity(request, scope),
        retrieval=build_retrieval_representation(request, ("Explain Engram",)),
        tier=Tier.STATIC,
        lifecycle=LifecycleState.ACTIVE,
        scope=scope,
        support_claim_ids=(),
        valid_from="",
        valid_from_available=False,
        valid_until="",
        valid_until_available=False,
        knowledge_epoch=1,
        knowledge_epoch_available=True,
        superseded_by="",
        provenance=artifact_provenance("tapestry:test", "regulator", NOW_TEXT),
        statistics=artifact_statistics(),
        metadata={"approved": True},
    )
    return result


def engine_with_artifact() -> Engram:
    engine = Engram()
    engine.response_repository = ArtifactRepository((artifact(),))
    engine.namespace_epochs.initialize("tenant-a", 1)
    persistence.synchronize_response_compatibility_views(engine, ())
    return engine


def negative_key(
    *,
    request: str = "unknown request",
    namespace: str = "tenant-a",
    epoch: int = 1,
    plan: str = "plan-a",
) -> NegativeResolutionKey:
    scope = scope_key(namespace=namespace)
    result = negative_resolution_key(
        query_identity=build_standalone_identity(request, scope),
        scope=scope,
        constraint_fingerprint=constraint_fingerprint("UNKNOWN", {}, ""),
        knowledge_epoch=epoch,
        knowledge_epoch_available=True,
        normalization_version=1,
        resolver_plan_fingerprint=canonical_fingerprint(plan),
        capability_readiness_fingerprint=canonical_fingerprint("ready"),
        policy_fingerprint=canonical_fingerprint("policy"),
    )
    return result


def test_feedback_contract_round_trip_is_deterministic_and_strict() -> None:
    value = observation("feedback-1", FeedbackOutcome.REJECTED_CONTEXT, reason="wrong account")

    relationship_key = feedback_observation_relationship_key(value)
    statement_key = feedback_observation_statement_key(value)
    assert feedback_observation_from_json(feedback_observation_to_json(value)) == value
    assert relationship_feedback_key_from_json(relationship_feedback_key_to_json(relationship_key)) == relationship_key
    assert statement_feedback_key_from_json(statement_feedback_key_to_json(statement_key)) == statement_key
    malformed = feedback_observation_to_dict(value)
    malformed["schema_version"] = 2
    with pytest.raises(InvalidRequestError, match="schema_version"):
        feedback_observation_from_dict(malformed)
    with pytest.raises(InvalidRequestError, match="must contain an object"):
        feedback_observation_from_json("[]")


def test_feedback_receipts_apply_once_and_conflicting_retries_fail() -> None:
    store = FeedbackStore()
    value = observation("feedback-1", FeedbackOutcome.ACCEPTED)

    first = apply(store, "feedback-1", value)
    replay = apply(store, "feedback-1", feedback_observation_with_changes(value, {"observed_at": "2026-08-15T12:00:01Z"}))

    assert first["replayed"] is False
    assert replay["replayed"] is True
    assert store.snapshot()["statement_records"][0]["raw"]["accept_count"] == 1
    with pytest.raises(ConflictError, match="different observation"):
        apply(store, "feedback-1", feedback_observation_with_changes(value, {"outcome": FeedbackOutcome.REJECTED_QUALITY}))


def test_feedback_accepts_the_declared_thousand_observation_batch() -> None:
    store = FeedbackStore()
    value = observation("feedback-batch", FeedbackOutcome.ACCEPTED)
    values = tuple(feedback_observation_with_changes(value, {"reference_id": f"resolution-{index}"}) for index in range(1_000))

    candidate = store.prepare("feedback-batch", values)

    receipt_result = cast(Any, mutation_receipt_to_dict(candidate["receipt"])["result"])
    assert receipt_result["observation_count"] == 1_000
    assert candidate["after"]["statement_records"][0]["raw"]["accept_count"] == 1_000
    assert candidate["after"]["relationship_records"][0]["raw"]["accept_count"] == 1_000


def test_feedback_store_snapshots_share_only_immutable_aggregate_records() -> None:
    store = FeedbackStore()
    apply(store, "feedback-immutable", observation("feedback-immutable", FeedbackOutcome.ACCEPTED))
    snapshot = store.snapshot()
    record = snapshot["statement_records"][0]

    with pytest.raises(TypeError):
        cast(Any, record)["last_observed_at"] = "2026-08-15T13:00:00Z"
    with pytest.raises(TypeError):
        cast(Any, record["raw"])["accept_count"] = 99

    assert store.snapshot()["statement_records"][0]["raw"]["accept_count"] == 1


def test_modified_prepared_feedback_state_cannot_use_the_fast_publication_path() -> None:
    store = FeedbackStore()
    candidate = store.prepare(
        "feedback-modified",
        (observation("feedback-modified", FeedbackOutcome.ACCEPTED),),
    )
    candidate["after"]["statement_evictions"] = 1

    with pytest.raises(InvalidRequestError, match="modified before publication"):
        store.replace_from_snapshot(candidate["after"])

    assert store.snapshot()["statement_records"] == ()


def test_lifecycle_execution_status_does_not_change_feedback_retry_identity() -> None:
    store = FeedbackStore()
    value = observation("feedback-stale", FeedbackOutcome.REJECTED_STALE)
    first = apply(store, "feedback-stale", value, status=LifecycleHandoffStatus.CONFLICTED)
    replay = store.prepare("feedback-stale", (value,), LifecycleHandoffStatus.COMPLETED)

    assert first["replayed"] is False
    assert replay["replayed"] is True
    assert cast(Any, mutation_receipt_to_dict(replay["receipt"])["result"])["lifecycle_status"] == "conflicted"


def test_feedback_partitions_isolate_scope_query_generation_and_policy() -> None:
    store = FeedbackStore()
    values = (
        observation("a", FeedbackOutcome.REJECTED_CONTEXT),
        observation("b", FeedbackOutcome.REJECTED_CONTEXT, context_fingerprint="account:2"),
        observation("c", FeedbackOutcome.REJECTED_CONTEXT, generation=4),
        feedback_observation_with_changes(observation("d", FeedbackOutcome.REJECTED_CONTEXT), {"policy_fingerprint": "b" * 64}),
    )
    for index, value in enumerate(values):
        apply(store, f"request-{index}", value)

    state = store.snapshot()
    assert len(state["statement_records"]) == 3
    assert len(state["relationship_records"]) == 4
    assert all(record["raw"]["rejected_context"] == 1 for record in state["relationship_records"])


def test_constraints_are_bounded_and_require_json_values() -> None:
    with pytest.raises(InvalidRequestError, match="JSON limit"):
        constraint_fingerprint("UNKNOWN", {"large": "x" * 70_000}, "")
    with pytest.raises(InvalidRequestError, match="JSON values"):
        constraint_fingerprint("UNKNOWN", {"invalid": object()}, "")


def test_history_uses_sample_floor_priors_and_deterministic_aging() -> None:
    store = FeedbackStore()
    accepted = observation("feedback", FeedbackOutcome.ACCEPTED)
    for index in range(4):
        apply(
            store,
            f"accepted-{index}",
            feedback_observation_with_changes(accepted, {"reference_id": f"resolution-{index}"}),
        )
    identity = accepted["query_identity"]
    constraint = accepted["constraint_fingerprint"]

    below_floor = store.history(identity, constraint, accepted["statement_id"], 3, POLICY_FINGERPRINT, NOW_TEXT)
    apply(store, "accepted-4", feedback_observation_with_changes(accepted, {"reference_id": "resolution-4"}))
    available = store.history(identity, constraint, accepted["statement_id"], 3, POLICY_FINGERPRINT, NOW_TEXT)
    aged = store.history(identity, constraint, accepted["statement_id"], 3, POLICY_FINGERPRINT, "2026-10-14T12:00:00Z")

    assert below_floor["available"] is False
    assert available["available"] is True
    assert available["value"] == pytest.approx(6 / 9)
    assert aged["available"] is False
    assert store.snapshot()["statement_records"][0]["raw"]["accept_count"] == 5
    assert feedback_history_from_json(feedback_history_to_json(available)) == available


def test_feedback_capacity_and_bucket_retention_are_bounded_and_inspectable() -> None:
    policy = feedback_policy(max_buckets_per_record=2, max_statement_records=2, max_relationship_records=2)
    store = FeedbackStore(feedback_state(policy=policy))
    for index in range(3):
        apply(
            store,
            f"quality-{index}",
            observation(
                f"quality-{index}",
                FeedbackOutcome.REJECTED_QUALITY,
                statement_id=f"stmt-{index}",
                observed_at=f"2026-08-{13 + index:02d}T12:00:00Z",
            ),
        )
    for index, day in enumerate((16, 17, 18)):
        apply(
            store,
            f"later-{index}",
            observation(
                f"later-{index}",
                FeedbackOutcome.ACCEPTED,
                statement_id="stmt-2",
                observed_at=f"2026-08-{day:02d}T12:00:00Z",
            ),
        )

    state = store.snapshot()
    inspection = store.inspect(1)
    assert len(state["statement_records"]) == 2
    assert len(state["relationship_records"]) == 2
    assert max(len(record["buckets"]) for record in state["statement_records"]) == 2
    assert state["statement_evictions"] == 1
    assert state["relationship_evictions"] == 1
    assert inspection["omitted_statement_count"] == 1
    assert inspection["receipts"]


def test_feedback_persistence_round_trip_and_legacy_statistics_remain_separate() -> None:
    engine = Engram()
    value = observation("feedback-1", FeedbackOutcome.ACCEPTED)
    candidate = engine.feedback_store.prepare("feedback-1", (value,))
    engine.feedback_store.replace_from_snapshot(candidate["after"])
    encoded = persistence.to_dict(engine)

    restored = persistence.load_engram_from_dict(encoded)
    without_feedback = dict(encoded)
    without_feedback.pop("feedback_state")
    migrated = persistence.load_engram_from_dict(without_feedback)

    assert restored.feedback_store.snapshot() == engine.feedback_store.snapshot()
    assert restored.feedback_store.prepare("feedback-1", (value,))["replayed"] is True
    assert migrated.feedback_store.snapshot()["statement_records"] == ()
    malformed = dict(encoded)
    malformed["feedback_state"] = dict(encoded["feedback_state"])
    malformed["feedback_state"]["schema_version"] = 2
    with pytest.raises(InvalidRequestError, match="feedback state schema_version"):
        persistence.load_engram_from_dict(malformed)


def test_negative_store_has_fixed_ttl_capacity_exact_isolation_and_invalidation() -> None:
    store = NegativeResolutionStore(max_records=2, ttl_seconds=10)
    first = negative_key(request="first")
    second = negative_key(request="second")
    third = negative_key(request="third")
    store.admit(first, "2026-08-15T12:00:00Z")

    assert store.lookup(first, "2026-08-15T12:00:05Z")["hit"] is True
    assert store.lookup(first, "2026-08-15T12:00:11Z")["hit"] is False
    store.admit(first, "2026-08-15T12:01:00Z")
    store.admit(second, "2026-08-15T12:01:01Z")
    store.admit(third, "2026-08-15T12:01:02Z")
    assert store.inspect()["evictions"] == 1
    changed_plan = negative_resolution_key_with_changes(third, {"resolver_plan_fingerprint": canonical_fingerprint("plan-b")})
    assert store.lookup(changed_plan, "2026-08-15T12:01:03Z")["hit"] is False
    assert cast(int, store.inspect()["invalidations"]) >= 1
    store.admit(changed_plan, "2026-08-15T12:01:04Z")
    assert store.invalidate_epoch_snapshot({"epochs": {"tenant-a": 2}}) == 2


def test_negative_contract_requires_available_epoch_and_round_trips() -> None:
    key = negative_key()
    assert negative_resolution_key_from_json(negative_resolution_key_to_json(key)) == key
    with pytest.raises(InvalidRequestError, match="available knowledge epoch"):
        negative_resolution_key_with_changes(key, {"knowledge_epoch_available": False})


def test_core_negative_hit_bypasses_resolvers_and_plan_changes_do_not_reuse() -> None:
    engine = Engram()
    engine.namespace_epochs.initialize("tenant-a", 1)
    core = EngramCore(engine, clock=lambda: NOW)

    first = core.resolve_request("Unknown concept", "miss-1", namespace="tenant-a", configured_resolvers=("exact",))
    hit = core.resolve_request("Unknown concept", "miss-2", namespace="tenant-a", configured_resolvers=("exact",))
    changed = core.resolve_request("Unknown concept", "miss-3", namespace="tenant-a", configured_resolvers=("exact", "pattern"))

    assert first["outcome"] == ResolutionOutcome.MISS
    assert "negative_resolution_hit" not in first["reason_codes"]
    assert hit["reason_codes"] == ("negative_resolution_hit", "insufficient_knowledge")
    assert hit["budget"]["resolvers"] == 0
    assert changed["outcome"] == ResolutionOutcome.MISS
    assert "negative_resolution_hit" not in changed["reason_codes"]
    inspection = cast(Any, core.inspect_feedback_learning()["negative_resolution"])
    assert inspection["hits"] == 1
    assert inspection["invalidations"] >= 1


def test_non_exact_plans_do_not_cache_misses_that_can_hide_new_knowledge() -> None:
    engine = Engram()
    engine.namespace_epochs.initialize("tenant-a", 1)
    engine.store("Unrelated answer", keyword_source="orchid tulip")
    core = EngramCore(engine, clock=lambda: NOW)
    budget = resolution_budget()

    first = core.resolve_request(
        "quasar nebula",
        "lexical-miss-1",
        namespace="tenant-a",
        configured_resolvers=("lexical",),
        budget=budget,
    )
    engine.store("Newly learned answer", keyword_source="quasar nebula")
    second = core.resolve_request(
        "quasar nebula",
        "lexical-miss-2",
        namespace="tenant-a",
        configured_resolvers=("lexical",),
        budget=budget,
    )

    assert first["outcome"] == ResolutionOutcome.MISS
    assert cast(Any, core.inspect_feedback_learning()["negative_resolution"])["admissions"] == 0
    assert "negative_resolution_hit" not in second["reason_codes"]
    assert any(result["reason_code"] == "lexical_candidates" for result in second["resolver_results"])


def test_negative_hit_honors_zero_diagnostic_budget() -> None:
    engine = Engram()
    engine.namespace_epochs.initialize("tenant-a", 1)
    core = EngramCore(engine, clock=lambda: NOW)
    core.resolve_request("Unknown concept", "budget-prime", namespace="tenant-a", configured_resolvers=("exact",))

    hit = core.resolve_request(
        "Unknown concept",
        "budget-hit",
        namespace="tenant-a",
        configured_resolvers=("exact",),
        budget=resolution_budget(max_diagnostic_bytes=0),
    )

    assert hit["reason_codes"] == ("negative_resolution_hit", "insufficient_knowledge")
    assert hit["frame_diagnostics"] == {}
    assert hit["budget"]["diagnostic_bytes"] == 0
    assert "diagnostic_bytes" in hit["budget"]["exhausted_dimensions"]


def test_negative_owner_abstains_without_epoch_and_fails_open() -> None:
    core = EngramCore(Engram(), clock=lambda: NOW)
    first = core.resolve_request("Unknown concept", "no-epoch-1", namespace="tenant-a", configured_resolvers=("exact",))
    second = core.resolve_request("Unknown concept", "no-epoch-2", namespace="tenant-a", configured_resolvers=("exact",))
    core.engram.namespace_epochs.initialize("tenant-a", 1)
    core._negative_resolutions.lookup = lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("injected"))
    fail_open = core.resolve_request("Another unknown", "fail-open", namespace="tenant-a", configured_resolvers=("exact",))

    assert "negative_resolution_hit" not in first["reason_codes"]
    assert "negative_resolution_hit" not in second["reason_codes"]
    assert fail_open["budget"]["resolvers"] == 1


def test_policy_filtered_exact_miss_is_never_negative_admitted() -> None:
    core = EngramCore(engine_with_artifact(), clock=lambda: NOW)

    first = core.resolve_request(
        "What is Engram?",
        "filtered-1",
        namespace="tenant-a",
        required_metadata={"approved": False},
        configured_resolvers=("exact",),
    )
    second = core.resolve_request(
        "What is Engram?",
        "filtered-2",
        namespace="tenant-a",
        required_metadata={"approved": False},
        configured_resolvers=("exact",),
    )

    assert first["resolver_results"][0]["reason_code"] == "exact_required_filter_excluded"
    assert "negative_resolution_hit" not in second["reason_codes"]
    assert cast(Any, core.inspect_feedback_learning()["negative_resolution"])["admissions"] == 0


def test_core_feedback_candidacy_verdict_retry_conflict_and_stale_handoff() -> None:
    core = EngramCore(engine_with_artifact(), clock=lambda: NOW)
    result = core.resolve_request("What is Engram?", "resolution-1", namespace="tenant-a", configured_resolvers=("exact",))
    statement_id = result["selected_candidate"]["statement_id"]

    first = core.record_resolution_feedback("resolution-1", "feedback-1", "rejected_stale", statement_id, "outdated")
    replay = core.record_resolution_feedback("resolution-1", "feedback-1", "rejected_stale", statement_id, "outdated")

    assert first["lifecycle_status"] == "completed"
    assert replay["idempotent"] is True
    assert core.engram.response_repository.get_artifact(statement_id)["lifecycle"] == LifecycleState.INVALIDATED
    assert core.engram.feedback_store.stale_excluded(statement_id, 2) is True
    inspection = cast(Any, core.inspect_feedback_learning()["feedback"])
    assert inspection["statement_record_count"] == 1
    assert inspection["statements"][0]["statistics"]["candidate_count"] == 1
    assert inspection["statements"][0]["statistics"]["rejected_stale"] == 1
    with pytest.raises(ConflictError, match="different observation"):
        core.record_resolution_feedback("resolution-1", "feedback-1", "rejected_quality", statement_id, "outdated")


def test_policy_feedback_suppresses_only_matching_namespace_and_policy_partition() -> None:
    core = EngramCore(engine_with_artifact(), clock=lambda: NOW)
    result = core.resolve_request("What is Engram?", "resolution-policy", namespace="tenant-a", configured_resolvers=("exact",))
    statement_id = result["selected_candidate"]["statement_id"]
    core.record_resolution_feedback("resolution-policy", "feedback-policy", "rejected_policy", statement_id)

    suppressed = core.resolve_request(
        "What is Engram?",
        "resolution-policy-after",
        namespace="tenant-a",
        configured_resolvers=("exact",),
    )

    assert suppressed["outcome"] == ResolutionOutcome.MISS
    fusion_report = cast(Any, suppressed["frame_diagnostics"]["fusion"])
    assert fusion_report["candidates"][0]["eligibility"]["reason_codes"] == ("feedback_policy_suppressed",)


def test_feedback_history_is_produced_for_fusion_without_weakening_hard_gates() -> None:
    core = EngramCore(engine_with_artifact(), clock=lambda: NOW)
    first = core.resolve_request("What is Engram?", "history-source", namespace="tenant-a", configured_resolvers=("exact",))
    statement_id = first["selected_candidate"]["statement_id"]
    for index in range(5):
        core.record_resolution_feedback(
            "history-source",
            f"history-feedback-{index}",
            "rejected_quality",
            statement_id,
        )

    evaluated = core.resolve_request("What is Engram?", "history-evaluated", namespace="tenant-a", configured_resolvers=("exact",))
    normalized = cast(Any, evaluated["frame_diagnostics"]["fusion"])["candidates"][0]["normalized_features"]

    assert evaluated["outcome"] == ResolutionOutcome.ANSWER
    assert "history" in normalized["available"]
    assert 0.0 < normalized["values"]["history"] < 0.2


def test_concurrent_external_verdicts_are_serialized_without_lost_updates() -> None:
    core = EngramCore(engine_with_artifact(), clock=lambda: NOW)
    first = core.resolve_request("What is Engram?", "concurrent-source", namespace="tenant-a", configured_resolvers=("exact",))
    statement_id = first["selected_candidate"]["statement_id"]

    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(
            executor.map(
                lambda index: core.record_resolution_feedback(
                    "concurrent-source",
                    f"concurrent-feedback-{index}",
                    "rejected_context",
                    statement_id,
                ),
                range(20),
            )
        )

    record = core.engram.feedback_store.snapshot()["statement_records"][0]
    assert all(result["idempotent"] is False for result in results)
    assert record["raw"]["candidate_count"] == 1
    assert record["raw"]["rejected_context"] == 20


def test_feedback_checkpoint_failure_retries_cached_resolution_without_double_credit(tmp_path, monkeypatch) -> None:
    store_path = tmp_path / "feedback-recovery.json"
    engine = engine_with_artifact()
    persistence.save(engine, store_path)
    core = open_engram_core(store_path=store_path)
    real_save = service_module.persistence.save_feedback_state

    def fail_save(*_args, **_kwargs):
        raise OSError("injected feedback checkpoint failure")

    monkeypatch.setattr(service_module.persistence, "save_feedback_state", fail_save)
    with pytest.raises(PersistenceError, match="feedback checkpoint"):
        core.resolve_request("What is Engram?", "recovery-source", namespace="tenant-a", configured_resolvers=("exact",))
    assert core.engram.feedback_store.snapshot()["statement_records"] == ()
    assert core.status()["durability"] == "healthy"

    monkeypatch.setattr(service_module.persistence, "save_feedback_state", real_save)
    replay = core.resolve_request("What is Engram?", "recovery-source", namespace="tenant-a", configured_resolvers=("exact",))

    assert replay["outcome"] == ResolutionOutcome.ANSWER
    restored = persistence.load_engram(store_path)
    feedback_record = restored.feedback_store.snapshot()["statement_records"][0]
    assert feedback_record["raw"]["candidate_count"] == 1


def test_feedback_checkpoint_recovers_a_committed_after_state(tmp_path, monkeypatch) -> None:
    store_path = tmp_path / "feedback-after-recovery.json"
    engine = engine_with_artifact()
    persistence.save(engine, store_path)
    core = open_engram_core(store_path=store_path)
    real_save = service_module.persistence.save_feedback_state

    def commit_then_fail(engram, state, path):
        real_save(engram, state, path)
        raise OSError("injected post-commit feedback checkpoint failure")

    monkeypatch.setattr(service_module.persistence, "save_feedback_state", commit_then_fail)
    result = core.resolve_request(
        "What is Engram?",
        "after-recovery-source",
        namespace="tenant-a",
        configured_resolvers=("exact",),
    )

    assert result["outcome"] == ResolutionOutcome.ANSWER
    assert core.status()["durability"] == "healthy"
    assert core.status()["dirty"] is False
    assert core.engram.feedback_store.snapshot()["statement_records"][0]["raw"]["candidate_count"] == 1
    assert persistence.load_feedback_state(store_path)["statement_records"][0]["raw"]["candidate_count"] == 1


def test_feedback_checkpoint_marks_divergent_recovery_degraded(tmp_path, monkeypatch) -> None:
    store_path = tmp_path / "feedback-divergent-recovery.json"
    engine = engine_with_artifact()
    persistence.save(engine, store_path)
    core = open_engram_core(store_path=store_path)
    real_save = service_module.persistence.save_feedback_state
    divergent = FeedbackStore()
    apply(divergent, "divergent-feedback", observation("divergent-source", FeedbackOutcome.ACCEPTED))
    divergent_state = divergent.snapshot()

    def diverge_then_fail(engram, _state, path):
        real_save(engram, divergent_state, path)
        raise OSError("injected divergent feedback checkpoint failure")

    monkeypatch.setattr(service_module.persistence, "save_feedback_state", diverge_then_fail)
    with pytest.raises(PersistenceError, match="feedback checkpoint"):
        core.resolve_request(
            "What is Engram?",
            "divergent-recovery-source",
            namespace="tenant-a",
            configured_resolvers=("exact",),
        )

    assert core.status()["durability"] == "degraded"
    assert core.engram.feedback_store.snapshot()["statement_records"] == ()
    assert feedback_state_signature(persistence.load_feedback_state(store_path)) == feedback_state_signature(divergent_state)


def test_uncheckpointed_feedback_replay_does_not_claim_durability(tmp_path) -> None:
    core = EngramCore(
        engine_with_artifact(),
        store_path=str(tmp_path / "not-yet-checkpointed.json"),
        checkpoint_on_mutation=False,
        clock=lambda: NOW,
    )
    result = core.resolve_request(
        "What is Engram?",
        "durability-source",
        namespace="tenant-a",
        configured_resolvers=("exact",),
    )
    statement_id = result["selected_candidate"]["statement_id"]

    first = core.record_resolution_feedback("durability-source", "durability-feedback", "accepted", statement_id)
    replay = core.record_resolution_feedback("durability-source", "durability-feedback", "accepted", statement_id)

    assert first["durable"] is False
    assert replay["durable"] is False
    assert core.status()["dirty"] is True


def test_legacy_proposal_path_uses_shared_feedback_owner() -> None:
    core = EngramCore(clock=lambda: NOW, checkpoint_on_mutation=False)
    learned = core.learn_response("What is cached?", "A regulated answer.", "learn-1", namespace="tenant-a")
    proposal = core.propose("What is cached?", "proposal-1", namespace="tenant-a")
    resolved = core.resolve(proposal["proposal_id"], "rejected_context", learned["statement_id"], "wrong context")

    inspection = cast(Any, core.inspect_feedback_learning()["feedback"])
    assert resolved["resolved"] is True
    assert inspection["statement_record_count"] == 1
    assert inspection["statements"][0]["statistics"]["candidate_count"] == 1
    assert inspection["statements"][0]["statistics"]["rejected_context"] == 1


def test_generation_unavailable_legacy_stale_feedback_excludes_only_legacy_candidate() -> None:
    engine = Engram()
    statement_id = engine.store(
        "A legacy regulated response.",
        keyword_source="What is legacy?",
        template={"tapestry": {"namespace": "tenant-a", "context_fingerprint": "", "metadata": {}}},
    )
    core = EngramCore(engine, clock=lambda: NOW, checkpoint_on_mutation=False)
    proposal = core.propose("What is legacy?", "legacy-proposal-1", namespace="tenant-a")

    resolved = core.resolve(proposal["proposal_id"], "rejected_stale", statement_id)
    excluded = core.propose("What is legacy?", "legacy-proposal-2", namespace="tenant-a")

    assert resolved["lifecycle_status"] == "pending"
    assert excluded["candidates"] == []
    assert core.engram.feedback_store.stale_excluded(statement_id, 0, False) is True
    assert core.engram.feedback_store.stale_excluded(statement_id, 1, True) is False
