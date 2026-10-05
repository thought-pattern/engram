"""Section 6 feedback-learning and negative-resolution conformance."""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime

from pytest import approx as pytest_approx, raises as pytest_raises

from engram.artifacts import LifecycleState, validate_cached_response_artifact
from engram.config import engram_config, sparse_config
from engram.constants import INITIAL_ARTIFACT_STATISTICS, Tier
from engram.core import Engram
from engram.errors import ConflictError, InvalidRequestError
from engram.feedback import (
    FeedbackObservationKind,
    FeedbackOutcome,
    FeedbackReferenceKind,
    FeedbackStore,
    LifecycleHandoffStatus,
    NegativeResolutionStore,
    canonical_fingerprint,
    constraint_fingerprint,
    feedback_policy,
    feedback_state,
    feedback_statistics,
    feedback_statistics_from_dict,
    feedback_statistics_to_dict,
    validate_feedback_observation,
    validate_negative_resolution_key,
)
from engram.identity import extract_standalone_identity, retrieval_representation, scope_key
from engram.mutations import mutation_receipt_to_dict
from engram.repository import ArtifactRepository
from engram.resolution import ResolutionOutcome, resolution_budget
from engram.service import EngramCore

NOW = datetime(2026, 8, 15, 12, 0, tzinfo=UTC)
NOW_TEXT = "2026-08-15T12:00:00Z"
POLICY_FINGERPRINT = "a" * 64
OBSERVATION_SCOPE = scope_key(namespace="tenant-a", context_fingerprint="account:1")
# One ACCEPTED external verdict on stmt-1 for "What is Engram?" in OBSERVATION_SCOPE. Tests pass a copy with their
# outcome and other changes to validate_feedback_observation.
ACCEPTED_OBSERVATION_FIELDS = {
    "reference_kind": FeedbackReferenceKind.RESOLUTION_REQUEST,
    "reference_id": "resolution-1",
    "kind": FeedbackObservationKind.VERDICT,
    "outcome": FeedbackOutcome.ACCEPTED,
    "query_identity": extract_standalone_identity("What is Engram?", OBSERVATION_SCOPE),
    "scope": OBSERVATION_SCOPE,
    "constraint_fingerprint": constraint_fingerprint("UNKNOWN", {}, ""),
    "statement_id": "stmt-1",
    "generation": 3,
    "generation_available": True,
    "policy_fingerprint": POLICY_FINGERPRINT,
    "observed_at": NOW_TEXT,
    "reason": "",
}
ARTIFACT_SCOPE = scope_key(namespace="tenant-a")
# The approved static answer to "What is Engram?" that the core resolution tests load into a fresh Engram.
ENGRAM_ARTIFACT_FIELDS = {
    "statement_id": "stmt-artifact",
    "generation": 1,
    "response": "Engram is a regulated memory system.",
    "query_identity": extract_standalone_identity("What is Engram?", ARTIFACT_SCOPE),
    "retrieval": retrieval_representation("What is Engram?", ("Explain Engram",)),
    "tier": Tier.STATIC,
    "lifecycle": LifecycleState.ACTIVE,
    "scope": ARTIFACT_SCOPE,
    "support_references": (),
    "valid_from": "",
    "valid_from_available": False,
    "valid_until": "",
    "valid_until_available": False,
    "superseded_by": "",
    "provenance": {"source_label": "test-source", "caller_id": "regulator", "accepted_at": NOW_TEXT},
    "statistics": INITIAL_ARTIFACT_STATISTICS,
    "metadata": {"approved": True},
}
NEGATIVE_SCOPE = scope_key(namespace="tenant-a")
# Fingerprints of one exact-plan negative-resolution key in NEGATIVE_SCOPE; tests add the request's query identity.
NEGATIVE_KEY_FIELDS = {
    "scope": NEGATIVE_SCOPE,
    "constraint_fingerprint": constraint_fingerprint("UNKNOWN", {}, ""),
    "resolver_plan_fingerprint": canonical_fingerprint("plan-a"),
    "capability_readiness_fingerprint": canonical_fingerprint("ready"),
    "policy_fingerprint": canonical_fingerprint("policy"),
}


def test_feedback_statistics_codec_preserves_the_complete_schema() -> None:
    statistics = feedback_statistics(candidate_count=3, accept_count=2, rejected_quality=1)
    encoded = feedback_statistics_to_dict(statistics)

    assert encoded == {
        "candidate_count": 3,
        "accept_count": 2,
        "rejected_quality": 1,
        "rejected_context": 0,
        "rejected_stale": 0,
        "rejected_policy": 0,
    }
    assert feedback_statistics_from_dict(encoded) == statistics


def apply(store: FeedbackStore, request_id: str, *values: dict, status=LifecycleHandoffStatus.NOT_APPLICABLE) -> dict:
    """Prepare one feedback mutation and publish it unless it replays an earlier receipt."""
    candidate = store.prepare(request_id, tuple(values), status)
    if "replayed" not in candidate:
        raise AssertionError("a prepared feedback candidate must state whether it replayed")
    if not candidate.get("replayed", False):
        store.replace_from_snapshot(candidate.get("after", {}))
    return candidate


def test_feedback_receipts_apply_once_and_conflicting_retries_fail() -> None:
    store = FeedbackStore()
    value = validate_feedback_observation(ACCEPTED_OBSERVATION_FIELDS)

    first = apply(store, "feedback-1", value)
    replay = apply(store, "feedback-1", validate_feedback_observation({**value, "observed_at": "2026-08-15T12:00:01Z"}))

    # apply() has already required the replayed field on both candidates.
    assert first.get("replayed", False) is False
    assert replay.get("replayed", False) is True
    statement_record = store.snapshot().get("statement_records", ())[0]
    assert statement_record.get("raw", {}).get("accept_count", 0) == 1
    with pytest_raises(ConflictError, match="different observation"):
        apply(store, "feedback-1", validate_feedback_observation({**value, "outcome": FeedbackOutcome.REJECTED_QUALITY}))


def test_feedback_accepts_the_declared_thousand_observation_batch() -> None:
    store = FeedbackStore()
    values = tuple(
        validate_feedback_observation({**ACCEPTED_OBSERVATION_FIELDS, "reference_id": f"resolution-{index}"})
        for index in range(1_000)
    )

    candidate = store.prepare("feedback-batch", values)

    receipt_result = mutation_receipt_to_dict(candidate.get("receipt", {})).get("result", {})
    after = candidate.get("after", {})
    assert receipt_result.get("observation_count", 0) == 1_000
    assert after.get("statement_records", ())[0].get("raw", {}).get("accept_count", 0) == 1_000
    assert after.get("relationship_records", ())[0].get("raw", {}).get("accept_count", 0) == 1_000


def test_feedback_store_snapshots_isolate_aggregate_records() -> None:
    store = FeedbackStore()
    apply(store, "feedback-immutable", validate_feedback_observation(ACCEPTED_OBSERVATION_FIELDS))
    snapshot = store.snapshot()
    record = snapshot.get("statement_records", ())[0]
    assert "raw" in record

    record["last_observed_at"] = "2026-08-15T13:00:00Z"
    record.get("raw", {})["accept_count"] = 99

    fresh = store.snapshot().get("statement_records", ())[0]
    assert "last_observed_at" in fresh
    assert fresh.get("last_observed_at", "") != record.get("last_observed_at", "")
    assert fresh.get("raw", {}).get("accept_count", 0) == 1


def test_modified_prepared_feedback_state_cannot_use_the_fast_publication_path() -> None:
    store = FeedbackStore()
    candidate = store.prepare(
        "feedback-modified",
        (validate_feedback_observation(ACCEPTED_OBSERVATION_FIELDS),),
    )
    assert "after" in candidate
    prepared_after = candidate.get("after", {})
    prepared_after["statement_evictions"] = 1

    with pytest_raises(InvalidRequestError, match="modified before publication"):
        store.replace_from_snapshot(prepared_after)

    snapshot = store.snapshot()
    assert "statement_records" in snapshot
    assert snapshot.get("statement_records", ()) == ()


def test_lifecycle_execution_status_does_not_change_feedback_retry_identity() -> None:
    store = FeedbackStore()
    value = validate_feedback_observation({**ACCEPTED_OBSERVATION_FIELDS, "outcome": FeedbackOutcome.REJECTED_STALE})
    first = apply(store, "feedback-stale", value, status=LifecycleHandoffStatus.CONFLICTED)
    replay = store.prepare("feedback-stale", (value,), LifecycleHandoffStatus.COMPLETED)

    # apply() has already required the replayed field on the first candidate.
    assert first.get("replayed", False) is False
    assert replay.get("replayed", False) is True
    replay_result = mutation_receipt_to_dict(replay.get("receipt", {})).get("result", {})
    assert replay_result.get("lifecycle_status", "") == "conflicted"


def test_feedback_partitions_isolate_scope_query_generation_and_policy() -> None:
    store = FeedbackStore()
    other_account_scope = scope_key(namespace="tenant-a", context_fingerprint="account:2")
    context_rejection = {**ACCEPTED_OBSERVATION_FIELDS, "outcome": FeedbackOutcome.REJECTED_CONTEXT}
    values = (
        validate_feedback_observation(context_rejection),
        validate_feedback_observation(
            {
                **context_rejection,
                "query_identity": extract_standalone_identity("What is Engram?", other_account_scope),
                "scope": other_account_scope,
            }
        ),
        validate_feedback_observation({**context_rejection, "generation": 4}),
        validate_feedback_observation({**context_rejection, "policy_fingerprint": "b" * 64}),
    )
    for index, value in enumerate(values):
        apply(store, f"request-{index}", value)

    state = store.snapshot()
    assert len(state.get("statement_records", ())) == 3
    assert len(state.get("relationship_records", ())) == 4
    assert all(record.get("raw", {}).get("rejected_context", 0) == 1 for record in state.get("relationship_records", ()))


def test_constraints_are_bounded_and_require_native_values() -> None:
    with pytest_raises(InvalidRequestError, match="encoded limit"):
        constraint_fingerprint("UNKNOWN", {"large": "x" * 70_000}, "")
    with pytest_raises(InvalidRequestError, match="bounded native values"):
        constraint_fingerprint("UNKNOWN", {"invalid": object()}, "")


def test_history_uses_sample_floor_priors_and_deterministic_aging() -> None:
    store = FeedbackStore()
    accepted = validate_feedback_observation(ACCEPTED_OBSERVATION_FIELDS)
    for index in range(4):
        apply(
            store,
            f"accepted-{index}",
            validate_feedback_observation({**accepted, "reference_id": f"resolution-{index}"}),
        )
    identity = accepted.get("query_identity", {})
    constraint = accepted.get("constraint_fingerprint", "")
    statement_id = accepted.get("statement_id", "")

    below_floor = store.history(identity, constraint, statement_id, 3, POLICY_FINGERPRINT, NOW_TEXT)
    apply(store, "accepted-4", validate_feedback_observation({**accepted, "reference_id": "resolution-4"}))
    available = store.history(identity, constraint, statement_id, 3, POLICY_FINGERPRINT, NOW_TEXT)
    aged = store.history(identity, constraint, statement_id, 3, POLICY_FINGERPRINT, "2026-10-14T12:00:00Z")

    assert "available" in below_floor
    assert below_floor.get("available", False) is False
    assert available.get("available", False) is True
    assert available.get("value", 0.0) == pytest_approx(6 / 9)
    assert "available" in aged
    assert aged.get("available", False) is False
    statement_record = store.snapshot().get("statement_records", ())[0]
    assert statement_record.get("raw", {}).get("accept_count", 0) == 5


def test_feedback_capacity_and_bucket_retention_are_bounded_and_inspectable() -> None:
    policy = feedback_policy(max_buckets_per_record=2, max_statement_records=2, max_relationship_records=2)
    store = FeedbackStore(feedback_state(policy=policy))
    for index in range(3):
        apply(
            store,
            f"quality-{index}",
            validate_feedback_observation(
                {
                    **ACCEPTED_OBSERVATION_FIELDS,
                    "outcome": FeedbackOutcome.REJECTED_QUALITY,
                    "statement_id": f"stmt-{index}",
                    "observed_at": f"2026-08-{13 + index:02d}T12:00:00Z",
                }
            ),
        )
    for index, day in enumerate((16, 17, 18)):
        apply(
            store,
            f"later-{index}",
            validate_feedback_observation(
                {**ACCEPTED_OBSERVATION_FIELDS, "statement_id": "stmt-2", "observed_at": f"2026-08-{day:02d}T12:00:00Z"}
            ),
        )

    state = store.snapshot()
    inspection = store.inspect(1)
    assert len(state.get("statement_records", ())) == 2
    assert len(state.get("relationship_records", ())) == 2
    assert max(len(record.get("buckets", ())) for record in state.get("statement_records", ())) == 2
    assert state.get("statement_evictions", 0) == 1
    assert state.get("relationship_evictions", 0) == 1
    assert inspection.get("omitted_statement_count", 0) == 1
    assert inspection.get("receipts", ())


def test_negative_store_has_fixed_ttl_capacity_and_exact_isolation() -> None:
    store = NegativeResolutionStore(max_records=2, ttl_seconds=10)
    first = validate_negative_resolution_key(
        {**NEGATIVE_KEY_FIELDS, "query_identity": extract_standalone_identity("first", NEGATIVE_SCOPE)}
    )
    second = validate_negative_resolution_key(
        {**NEGATIVE_KEY_FIELDS, "query_identity": extract_standalone_identity("second", NEGATIVE_SCOPE)}
    )
    third = validate_negative_resolution_key(
        {**NEGATIVE_KEY_FIELDS, "query_identity": extract_standalone_identity("third", NEGATIVE_SCOPE)}
    )
    store.admit(first, "2026-08-15T12:00:00Z")

    assert store.lookup(first, "2026-08-15T12:00:05Z").get("hit", False) is True
    expired_lookup = store.lookup(first, "2026-08-15T12:00:11Z")
    assert "hit" in expired_lookup
    assert expired_lookup.get("hit", False) is False
    store.admit(first, "2026-08-15T12:01:00Z")
    store.admit(second, "2026-08-15T12:01:01Z")
    store.admit(third, "2026-08-15T12:01:02Z")
    assert store.inspect().get("evictions", 0) == 1
    changed_plan = validate_negative_resolution_key({**third, "resolver_plan_fingerprint": canonical_fingerprint("plan-b")})
    changed_lookup = store.lookup(changed_plan, "2026-08-15T12:01:03Z")
    assert "hit" in changed_lookup
    assert changed_lookup.get("hit", False) is False
    assert store.inspect().get("invalidations", 0) >= 1
    store.admit(changed_plan, "2026-08-15T12:01:04Z")


def test_core_negative_hit_bypasses_resolvers_and_plan_changes_do_not_reuse() -> None:
    engine = Engram()
    core = EngramCore(engine, clock=lambda: NOW)

    first = core.resolve_request("Unknown concept", "miss-1", namespace="tenant-a", configured_resolvers=("exact",))
    hit = core.resolve_request("Unknown concept", "miss-2", namespace="tenant-a", configured_resolvers=("exact",))
    changed = core.resolve_request("Unknown concept", "miss-3", namespace="tenant-a", configured_resolvers=("exact", "sparse"))

    assert first.get("outcome", ResolutionOutcome.ANSWER) == ResolutionOutcome.MISS
    assert "reason_codes" in first
    assert "negative_resolution_hit" not in first.get("reason_codes", ())
    assert hit.get("reason_codes", ()) == ("negative_resolution_hit", "insufficient_knowledge")
    hit_budget = hit.get("budget", {})
    assert "resolvers" in hit_budget
    assert hit_budget.get("resolvers", 0) == 0
    assert changed.get("outcome", ResolutionOutcome.ANSWER) == ResolutionOutcome.MISS
    assert "reason_codes" in changed
    assert "negative_resolution_hit" not in changed.get("reason_codes", ())
    inspection = core.inspect_feedback_learning().get("negative_resolution", {})
    assert inspection.get("hits", 0) == 1
    assert inspection.get("invalidations", 0) >= 1


def test_non_exact_plans_do_not_cache_misses_that_can_hide_new_knowledge() -> None:
    engine = Engram(engram_config(sparse=sparse_config(enabled=True)))
    core = EngramCore(engine, clock=lambda: NOW)
    budget = resolution_budget()

    first = core.resolve_request(
        "quasar nebula",
        "sparse-miss-1",
        namespace="tenant-a",
        configured_resolvers=("sparse",),
        budget=budget,
    )
    core.learn_response(
        "quasar nebula",
        "Newly learned answer",
        "sparse-new-knowledge",
        namespace="tenant-a",
    )
    second = core.resolve_request(
        "quasar nebula",
        "sparse-miss-2",
        namespace="tenant-a",
        configured_resolvers=("sparse",),
        budget=budget,
    )

    assert first.get("outcome", ResolutionOutcome.ANSWER) == ResolutionOutcome.MISS
    negative_inspection = core.inspect_feedback_learning().get("negative_resolution", {})
    assert "admissions" in negative_inspection
    assert negative_inspection.get("admissions", 0) == 0
    assert "reason_codes" in second
    assert "negative_resolution_hit" not in second.get("reason_codes", ())
    assert any(result.get("reason_code", "") == "sparse_candidates" for result in second.get("resolver_results", ()))


def test_negative_hit_honors_zero_diagnostic_budget() -> None:
    engine = Engram()
    core = EngramCore(engine, clock=lambda: NOW)
    core.resolve_request("Unknown concept", "budget-prime", namespace="tenant-a", configured_resolvers=("exact",))

    hit = core.resolve_request(
        "Unknown concept",
        "budget-hit",
        namespace="tenant-a",
        configured_resolvers=("exact",),
        budget=resolution_budget(max_diagnostic_bytes=0),
    )

    assert hit.get("reason_codes", ()) == ("negative_resolution_hit", "insufficient_knowledge")
    assert "frame_diagnostics" in hit
    assert hit.get("frame_diagnostics", {}) == {}
    hit_budget = hit.get("budget", {})
    assert "diagnostic_bytes" in hit_budget
    assert hit_budget.get("diagnostic_bytes", 0) == 0
    assert "diagnostic_bytes" in hit_budget.get("exhausted_dimensions", ())


def test_negative_cache_requires_no_durable_graph_generation() -> None:
    core = EngramCore(Engram(), clock=lambda: NOW)
    first = core.resolve_request("Unknown concept", "negative-1", namespace="tenant-a", configured_resolvers=("exact",))
    second = core.resolve_request("Unknown concept", "negative-2", namespace="tenant-a", configured_resolvers=("exact",))

    assert "reason_codes" in first
    assert "negative_resolution_hit" not in first.get("reason_codes", ())
    assert "negative_resolution_hit" in second.get("reason_codes", ())


def test_policy_filtered_exact_miss_is_never_negative_admitted() -> None:
    engine = Engram()
    engine.response_repository = ArtifactRepository((validate_cached_response_artifact(ENGRAM_ARTIFACT_FIELDS),))
    core = EngramCore(engine, clock=lambda: NOW)

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

    first_exact = first.get("resolver_results", ())[0]
    assert first_exact.get("reason_code", "") == "exact_required_filter_excluded"
    assert "reason_codes" in second
    assert "negative_resolution_hit" not in second.get("reason_codes", ())
    negative_inspection = core.inspect_feedback_learning().get("negative_resolution", {})
    assert "admissions" in negative_inspection
    assert negative_inspection.get("admissions", 0) == 0


def test_core_feedback_candidacy_verdict_retry_conflict_and_stale_handoff() -> None:
    engine = Engram()
    engine.response_repository = ArtifactRepository((validate_cached_response_artifact(ENGRAM_ARTIFACT_FIELDS),))
    core = EngramCore(engine, clock=lambda: NOW)
    result = core.resolve_request(
        "What is Engram?", "resolution-1", namespace="tenant-a", configured_resolvers=("exact",), accept_exact=True
    )
    statement_id = result.get("selected_candidate", {}).get("statement_id", "")
    assert statement_id

    first = core.record_resolution_feedback("resolution-1", "feedback-1", "rejected_stale", statement_id, "outdated")
    replay = core.record_resolution_feedback("resolution-1", "feedback-1", "rejected_stale", statement_id, "outdated")

    assert first.get("lifecycle_status", "") == "completed"
    assert replay.get("idempotent", False) is True
    artifact = core.engram.response_repository.get_artifact(statement_id)
    assert artifact.get("lifecycle", LifecycleState.ACTIVE) == LifecycleState.INVALIDATED
    assert core.engram.feedback_store.stale_excluded(statement_id, 2) is True
    inspection = core.inspect_feedback_learning().get("feedback", {})
    assert inspection.get("statement_record_count", 0) == 1
    statistics = inspection.get("statements", ())[0].get("statistics", {})
    assert statistics.get("candidate_count", 0) == 1
    assert statistics.get("rejected_stale", 0) == 1
    with pytest_raises(ConflictError, match="different observation"):
        core.record_resolution_feedback("resolution-1", "feedback-1", "rejected_quality", statement_id, "outdated")


def test_policy_feedback_suppresses_only_matching_namespace_and_policy_partition() -> None:
    engine = Engram()
    engine.response_repository = ArtifactRepository((validate_cached_response_artifact(ENGRAM_ARTIFACT_FIELDS),))
    core = EngramCore(engine, clock=lambda: NOW)
    result = core.resolve_request(
        "What is Engram?", "resolution-policy", namespace="tenant-a", configured_resolvers=("exact",), accept_exact=True
    )
    statement_id = result.get("selected_candidate", {}).get("statement_id", "")
    assert statement_id
    core.record_resolution_feedback("resolution-policy", "feedback-policy", "rejected_policy", statement_id)

    suppressed = core.resolve_request(
        "What is Engram?",
        "resolution-policy-after",
        namespace="tenant-a",
        configured_resolvers=("exact",),
    )

    assert suppressed.get("outcome", ResolutionOutcome.ANSWER) == ResolutionOutcome.MISS
    fusion_report = suppressed.get("frame_diagnostics", {}).get("fusion", {})
    eligibility = fusion_report.get("candidates", ())[0].get("eligibility", {})
    assert eligibility.get("reason_codes", ()) == ("feedback_policy_suppressed",)


def test_feedback_history_is_produced_for_fusion_without_weakening_hard_gates() -> None:
    engine = Engram()
    engine.response_repository = ArtifactRepository((validate_cached_response_artifact(ENGRAM_ARTIFACT_FIELDS),))
    core = EngramCore(engine, clock=lambda: NOW)
    first = core.resolve_request("What is Engram?", "history-source", namespace="tenant-a", configured_resolvers=("exact",))
    statement_id = first.get("response_candidates", ())[0].get("statement_id", "")
    assert statement_id
    for index in range(5):
        core.record_resolution_feedback(
            "history-source",
            f"history-feedback-{index}",
            "rejected_quality",
            statement_id,
        )

    evaluated = core.resolve_request(
        "What is Engram?", "history-evaluated", namespace="tenant-a", configured_resolvers=("exact",), accept_exact=True
    )
    fusion_candidate = evaluated.get("frame_diagnostics", {}).get("fusion", {}).get("candidates", ())[0]
    normalized = fusion_candidate.get("normalized_features", {})

    assert evaluated.get("outcome", ResolutionOutcome.MISS) == ResolutionOutcome.ANSWER
    assert "history" in normalized.get("available", ())
    assert 0.0 < normalized.get("values", {}).get("history", 0.0) < 0.2


def test_concurrent_external_verdicts_are_serialized_without_lost_updates() -> None:
    engine = Engram()
    engine.response_repository = ArtifactRepository((validate_cached_response_artifact(ENGRAM_ARTIFACT_FIELDS),))
    core = EngramCore(engine, clock=lambda: NOW)
    first = core.resolve_request(
        "What is Engram?", "concurrent-source", namespace="tenant-a", configured_resolvers=("exact",), accept_exact=True
    )
    statement_id = first.get("selected_candidate", {}).get("statement_id", "")
    assert statement_id

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

    record = core.engram.feedback_store.snapshot().get("statement_records", ())[0]
    assert all("idempotent" in result for result in results)
    assert all(result.get("idempotent", False) is False for result in results)
    assert record.get("raw", {}).get("candidate_count", 0) == 1
    assert record.get("raw", {}).get("rejected_context", 0) == 20


def test_feedback_replay_is_process_local_and_does_not_claim_durability() -> None:
    engine = Engram()
    engine.response_repository = ArtifactRepository((validate_cached_response_artifact(ENGRAM_ARTIFACT_FIELDS),))
    core = EngramCore(engine, clock=lambda: NOW)
    result = core.resolve_request(
        "What is Engram?",
        "process-feedback-source",
        namespace="tenant-a",
        configured_resolvers=("exact",),
        accept_exact=True,
    )
    statement_id = result.get("selected_candidate", {}).get("statement_id", "")
    assert statement_id

    first = core.record_resolution_feedback(
        "process-feedback-source",
        "process-feedback",
        "accepted",
        statement_id,
    )
    replay = core.record_resolution_feedback(
        "process-feedback-source",
        "process-feedback",
        "accepted",
        statement_id,
    )

    assert "idempotent" in first
    assert first.get("idempotent", False) is False
    assert replay.get("idempotent", False) is True
    assert "durable" not in first
    assert core.status().get("memory_only", False) is True


def test_proposal_path_uses_shared_feedback_owner() -> None:
    core = EngramCore(clock=lambda: NOW)
    learned = core.learn_response("What is cached?", "A regulated answer.", "learn-1", namespace="tenant-a")
    proposal = core.propose("What is cached?", "proposal-1", namespace="tenant-a")
    resolved = core.resolve(proposal.get("proposal_id", ""), "rejected_context", learned.get("statement_id", ""), "wrong context")

    inspection = core.inspect_feedback_learning().get("feedback", {})
    assert resolved.get("resolved", False) is True
    assert inspection.get("statement_record_count", 0) == 1
    statistics = inspection.get("statements", ())[0].get("statistics", {})
    assert statistics.get("candidate_count", 0) == 1
    assert statistics.get("rejected_context", 0) == 1


def test_accepting_a_candidate_retired_after_the_proposal_is_rejected_as_stale() -> None:
    core = EngramCore(clock=lambda: NOW)
    learned = core.learn_response("What is cached?", "A regulated answer.", "learn-before-retired-accept", namespace="tenant-a")
    statement_id = learned.get("statement_id", "")
    assert statement_id
    proposal = core.propose("What is cached?", "proposal-before-retired-accept", namespace="tenant-a")
    core.retire_response(statement_id, "support became stale", "retire-before-accept")

    with pytest_raises(ConflictError, match="no longer current"):
        core.resolve(proposal.get("proposal_id", ""), "accepted", statement_id, "looked right")

    artifact = core.engram.response_repository.get_artifact(statement_id)
    assert artifact.get("lifecycle", LifecycleState.ACTIVE) == LifecycleState.RETIRED
    artifact_statistics = artifact.get("statistics", {})
    assert "hit_count" in artifact_statistics
    assert artifact_statistics.get("hit_count", 0) == 0


def test_stale_resolution_leaves_dynamic_response_for_explicit_retirement() -> None:
    core = EngramCore(clock=lambda: NOW)
    learned = core.learn_response(
        "What is cached?",
        "A regulated answer.",
        "learn-before-stale-retirement",
        namespace="tenant-a",
    )
    statement_id = learned.get("statement_id", "")
    assert statement_id
    proposal = core.propose("What is cached?", "proposal-before-stale-retirement", namespace="tenant-a")

    resolved = core.resolve(proposal.get("proposal_id", ""), "rejected_stale", statement_id, "support became stale")
    artifact_after_resolution = core.engram.response_repository.get_artifact(statement_id)
    retired = core.retire_response(statement_id, "support became stale", "explicit-stale-retirement")
    artifact_after_retirement = core.engram.response_repository.get_artifact(statement_id)

    assert resolved.get("lifecycle_status", "") == "not_applicable"
    assert artifact_after_resolution.get("lifecycle", LifecycleState.RETIRED) == LifecycleState.ACTIVE
    assert retired.get("retired", False) is True
    assert artifact_after_retirement.get("lifecycle", LifecycleState.ACTIVE) == LifecycleState.RETIRED
