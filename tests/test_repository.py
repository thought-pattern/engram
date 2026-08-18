"""Section 3 authoritative live artifact repository tests."""

import threading
from collections.abc import Mapping
from types import MappingProxyType
from typing import Any, cast

import pytest

from engram.artifacts import (
    CachedResponseArtifact,
    LifecycleState,
    artifact_provenance,
    artifact_statistics,
    cached_response_artifact,
)
from engram.constants import EvictionPolicy, Tier
from engram.eligibility import EligibilityContext, EpochEligibilityPolicy, EpochSource, eligibility_context
from engram.errors import ConflictError, InvalidRequestError, ResourceNotFoundError
from engram.identity import build_retrieval_representation, build_scoped_retrieval_key, build_standalone_identity, scope_key
from engram.indexes import ExactLookupOutcome, build_index_state
from engram.repository import (
    AdmissionOutcome,
    ArtifactRepository,
    RepositoryRemovalReason,
    admission_plan_to_dict,
    build_repository_state,
    check_repository_state,
    compatibility_statement_from_artifact,
    repository_check_report_to_dict,
    repository_state,
    tier_admission_policy,
    validate_repository_check_report,
    validate_repository_state,
)


def accepted_artifact(statement_id="stmt-1", tier=Tier.DYNAMIC, **overrides) -> CachedResponseArtifact:
    scope = overrides.pop("scope", scope_key(namespace="tenant-a"))
    request = overrides.pop("request", f"Question for {statement_id}?")
    values = {
        "statement_id": statement_id,
        "generation": 1,
        "response": f"Exact response for {statement_id}.",
        "query_identity": build_standalone_identity(request, scope),
        "retrieval": build_retrieval_representation(request, (f"Alias for {statement_id}",)),
        "tier": tier,
        "lifecycle": LifecycleState.ACTIVE,
        "scope": scope,
        "support_claim_ids": (f"claim-{statement_id}",),
        "valid_from": "",
        "valid_from_available": False,
        "valid_until": "",
        "valid_until_available": False,
        "knowledge_epoch": 0,
        "knowledge_epoch_available": False,
        "superseded_by": "",
        "provenance": artifact_provenance("test", "caller", "2026-08-12T16:00:00Z"),
        "statistics": artifact_statistics(2, 3, "2026-08-12T17:00:00Z", True),
        "metadata": {"approved": True},
    }
    values.update(overrides)
    result = cached_response_artifact(**values)
    return result


def context() -> EligibilityContext:
    result = eligibility_context(
        evaluation_time="2026-08-12T18:00:00Z",
        evaluation_time_available=True,
        namespace="tenant-a",
        knowledge_epoch=0,
        knowledge_epoch_available=True,
        artifact_repository_available=True,
        epoch_source=EpochSource.STANDALONE,
    )
    return result


def object_mapping(value: object) -> Mapping[str, object]:
    assert isinstance(value, Mapping)
    return value


def test_compatibility_statement_derives_exact_text_identity_provenance_and_statistics() -> None:
    artifact = accepted_artifact()
    statement = compatibility_statement_from_artifact(artifact)

    assert statement["id"] == artifact["statement_id"]
    assert statement["text"] == artifact["response"]
    assert statement["tier"] == artifact["tier"]
    assert statement["hit_count"] == 2
    assert statement["query_count"] == 3
    assert statement["source_label"] == "test"
    assert statement["introduced_by_user_id"] == "caller"
    template = object_mapping(statement["template"])
    tapestry = object_mapping(template["tapestry"])
    assert tapestry["request"] == artifact["retrieval"]["canonical"]
    assert tapestry["support"] == ({"claim_id": "claim-stmt-1"},)
    with pytest.raises(TypeError):
        cast(dict[str, object], statement)["text"] = "rewritten"


def test_repository_build_is_conservative_and_equivalent() -> None:
    first = accepted_artifact("stmt-1")
    second = accepted_artifact("stmt-2", tier=Tier.STATIC)
    state = build_repository_state((second, first))
    report = check_repository_state(state)

    assert state.__class__ is dict
    assert type(report) is dict
    assert repository_check_report_to_dict(report) == {
        "consistent": True,
        "state_generation": 1,
        "artifact_count": 2,
        "issues": [],
        "omitted_issue_count": 0,
    }
    assert set(state["artifacts"]) == set(state["statements"]) == set(state["index_state"]["projections"])
    assert all(not projection["direct_answer_eligible"] for projection in state["index_state"]["projections"].values())
    assert state["index_state"]["projections"]["stmt-1"]["exclusion_reason"] == "eligibility_context_required"

    malformed = dict(report)
    malformed["consistent"] = False
    with pytest.raises(InvalidRequestError, match="consistency must agree"):
        validate_repository_check_report(malformed)

    malformed_state = dict(state)
    malformed_state["unexpected"] = ()
    with pytest.raises(InvalidRequestError, match="must be a RepositoryState"):
        validate_repository_state(malformed_state)


def test_repository_lookup_and_statement_views_do_not_expose_mutable_authority() -> None:
    artifact = accepted_artifact()
    repository = ArtifactRepository((artifact,))

    view = repository.get_statement(artifact["statement_id"])
    view["text"] = "caller rewrite"
    view_template = object_mapping(view["template"])
    view_tapestry = object_mapping(view_template["tapestry"])
    view_metadata = cast(dict[str, object], object_mapping(view_tapestry["metadata"]))
    view_metadata["approved"] = False
    artifact["response"] = "caller artifact rewrite"

    retained_artifact = repository.get_artifact(artifact["statement_id"])
    assert retained_artifact["response"] == "Exact response for stmt-1."
    assert retained_artifact is not artifact
    retained_artifact["response"] = "returned artifact rewrite"
    retained_artifact["statistics"]["query_count"] = 99
    fresh_artifact = repository.get_artifact(artifact["statement_id"])
    assert fresh_artifact["response"] == "Exact response for stmt-1."
    assert fresh_artifact["statistics"]["query_count"] == 3
    assert repository.get_statement(artifact["statement_id"])["text"] == "Exact response for stmt-1."
    retained_template = object_mapping(repository.get_statement(artifact["statement_id"])["template"])
    retained_tapestry = object_mapping(retained_template["tapestry"])
    assert retained_tapestry["metadata"] == {"approved": True}
    snapshot = repository.snapshot()
    snapshot["state_generation"] = 99
    assert repository.snapshot()["state_generation"] == 1
    assert repository.check()["consistent"] is True


def test_candidate_add_and_atomic_swap_publish_all_views_together() -> None:
    repository = ArtifactRepository((accepted_artifact("stmt-1"),))
    before = repository.snapshot()
    candidate = repository.candidate_with_artifact(accepted_artifact("stmt-2"))

    assert set(repository.snapshot()["artifacts"]) == {"stmt-1"}
    published = repository.atomic_replace(candidate, before["state_generation"])

    assert set(published["artifacts"]) == {"stmt-1", "stmt-2"}
    assert set(published["statements"]) == {"stmt-1", "stmt-2"}
    assert set(published["index_state"]["projections"]) == {"stmt-1", "stmt-2"}
    assert repository.check()["consistent"] is True


def test_atomic_swap_rejects_stale_generation_and_non_next_candidate() -> None:
    repository = ArtifactRepository((accepted_artifact("stmt-1"),))
    before = repository.snapshot()
    candidate = repository.candidate_with_artifact(accepted_artifact("stmt-2"))

    with pytest.raises(ConflictError, match="state generation conflict"):
        repository.atomic_replace(candidate, before["state_generation"] + 1)
    malformed_generation = repository_state(
        state_generation=before["state_generation"] + 2,
        artifacts=candidate["artifacts"],
        statements=candidate["statements"],
        index_state=candidate["index_state"],
    )
    with pytest.raises(ConflictError, match="advance by exactly one"):
        repository.atomic_replace(malformed_generation, before["state_generation"])


def test_capacity_eviction_removes_only_dynamic_without_lifecycle_mutation() -> None:
    dynamic = accepted_artifact("stmt-dynamic", lifecycle=LifecycleState.RETIRED)
    static = accepted_artifact("stmt-static", tier=Tier.STATIC)
    repository = ArtifactRepository((dynamic, static))
    before = repository.snapshot()

    candidate = repository.candidate_without_artifact(
        dynamic["statement_id"],
        dynamic["generation"],
        RepositoryRemovalReason.CAPACITY_EVICTION,
    )
    repository.atomic_replace(candidate, before["state_generation"])

    assert dynamic["lifecycle"] == LifecycleState.RETIRED
    assert set(repository.snapshot()["artifacts"]) == {"stmt-static"}
    with pytest.raises(ConflictError, match="cannot remove STATIC"):
        repository.candidate_without_artifact(
            static["statement_id"],
            static["generation"],
            RepositoryRemovalReason.CAPACITY_EVICTION,
        )


def test_explicit_delete_uses_expected_generation_and_synchronizes_views() -> None:
    artifact = accepted_artifact()
    repository = ArtifactRepository((artifact,))
    with pytest.raises(ConflictError, match="generation conflict"):
        repository.candidate_without_artifact(artifact["statement_id"], 2, RepositoryRemovalReason.EXPLICIT_DELETE)

    before = repository.snapshot()
    candidate = repository.candidate_without_artifact(
        artifact["statement_id"],
        artifact["generation"],
        RepositoryRemovalReason.EXPLICIT_DELETE,
    )
    empty = repository.atomic_replace(candidate, before["state_generation"])

    assert empty["artifacts"] == {}
    assert empty["statements"] == {}
    assert empty["index_state"]["projections"] == {}
    assert repository.check()["consistent"] is True


def test_repository_contextual_lookup_synchronizes_refreshed_index_state() -> None:
    artifact = accepted_artifact(valid_until="2026-08-12T19:00:00Z", valid_until_available=True)
    repository = ArtifactRepository((artifact,))
    key = build_scoped_retrieval_key(artifact["scope"], artifact["retrieval"]["canonical"])
    before = repository.snapshot()

    found = repository.exact_lookup(key, context(), EpochEligibilityPolicy.MATCH_WHEN_ARTIFACT_AVAILABLE)

    assert found["lookup"]["outcome"] == ExactLookupOutcome.FOUND
    assert found["index_refreshed"] is True
    after = repository.snapshot()
    assert after["state_generation"] == before["state_generation"] + 1
    assert after["index_state"]["projections"][artifact["statement_id"]]["direct_answer_eligible"] is True
    assert repository.check()["consistent"] is True


def test_repository_check_detects_view_and_projection_corruption() -> None:
    artifact = accepted_artifact()
    state = build_repository_state((artifact,))
    corrupt_statement = dict(state["statements"][artifact["statement_id"]])
    corrupt_statement["text"] = "corrupt"
    corrupt_view = repository_state(
        state["state_generation"],
        state["artifacts"],
        {artifact["statement_id"]: MappingProxyType(corrupt_statement)},
        state["index_state"],
    )
    assert "compatibility_statement_mismatch:stmt-1" in check_repository_state(corrupt_view)["issues"]

    corrupt_projection_state = build_index_state((), state["index_state"]["state_generation"])
    corrupt_projection = repository_state(
        state["state_generation"],
        state["artifacts"],
        state["statements"],
        corrupt_projection_state,
    )
    assert "artifact_projection_id_set_mismatch" in check_repository_state(corrupt_projection)["issues"]


def test_repository_concurrent_swap_has_one_winner_and_never_partial_state() -> None:
    repository = ArtifactRepository((accepted_artifact("stmt-base"),))
    expected = repository.snapshot()["state_generation"]
    candidates = [
        repository.candidate_with_artifact(accepted_artifact("stmt-a")),
        repository.candidate_with_artifact(accepted_artifact("stmt-b")),
    ]
    barrier = threading.Barrier(3)
    successes = []
    conflicts = []

    def publish(candidate) -> None:
        barrier.wait()
        try:
            successes.append(repository.atomic_replace(candidate, expected))
        except ConflictError as error:
            conflicts.append(str(error))

    threads = [threading.Thread(target=publish, args=(candidate,)) for candidate in candidates]
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join()

    assert len(successes) == 1
    assert len(conflicts) == 1
    state = repository.snapshot()
    assert set(state["artifacts"]) in ({"stmt-base", "stmt-a"}, {"stmt-base", "stmt-b"})
    assert set(state["artifacts"]) == set(state["statements"]) == set(state["index_state"]["projections"])
    assert repository.check()["consistent"] is True


def test_repository_rejects_duplicate_wrong_and_missing_inputs() -> None:
    artifact = accepted_artifact()
    with pytest.raises(ConflictError, match="duplicate"):
        build_repository_state((artifact, artifact))
    with pytest.raises(InvalidRequestError, match="iterable"):
        build_repository_state(cast(Any, {artifact["statement_id"]: artifact}))
    repository = ArtifactRepository()
    with pytest.raises(ResourceNotFoundError, match="not found"):
        repository.get_artifact("missing")
    with pytest.raises(InvalidRequestError, match="RepositoryRemovalReason"):
        repository.candidate_without_artifact("missing", 1, cast(Any, "delete"))


def test_dynamic_admission_below_capacity_changes_no_existing_residency() -> None:
    existing = accepted_artifact("stmt-existing")
    incoming = accepted_artifact("stmt-incoming")
    repository = ArtifactRepository((existing,))
    plan = repository.plan_admission(incoming, tier_admission_policy(2, EvictionPolicy.FIFO))

    assert admission_plan_to_dict(plan) == {
        "outcome": "ADMITTED",
        "candidate_state_generation": 2,
        "admitted_statement_id": "stmt-incoming",
        "evicted_statement_ids": [],
        "affected_epoch_namespaces": ["tenant-a"],
        "residency_changed": True,
        "lifecycle_changed": False,
    }
    assert set(plan["candidate"]["artifacts"]) == {"stmt-existing", "stmt-incoming"}
    assert existing["lifecycle"] == LifecycleState.ACTIVE


def test_static_admission_never_consumes_or_evicts_dynamic_capacity() -> None:
    dynamic = accepted_artifact("stmt-dynamic")
    incoming = accepted_artifact("stmt-static", tier=Tier.STATIC)
    repository = ArtifactRepository((dynamic,))

    plan = repository.plan_admission(incoming, tier_admission_policy(1, EvictionPolicy.FIFO))

    assert plan["outcome"] == AdmissionOutcome.ADMITTED
    assert plan["evicted_statement_ids"] == ()
    assert set(plan["candidate"]["artifacts"]) == {"stmt-dynamic", "stmt-static"}


def test_all_protected_dynamic_artifacts_reject_incoming_without_over_capacity_admission() -> None:
    protected = accepted_artifact(
        "stmt-protected",
        statistics=artifact_statistics(10, 10, "2026-08-12T17:00:00Z", True),
    )
    incoming = accepted_artifact("stmt-incoming")
    repository = ArtifactRepository((protected,))
    before = repository.snapshot()

    plan = repository.plan_admission(
        incoming,
        tier_admission_policy(1, EvictionPolicy.FIFO, minimum_protected_hit_rate=0.5),
    )

    assert plan["outcome"] == AdmissionOutcome.REJECTED_CAPACITY
    assert plan["candidate"] == before
    assert plan["candidate"] is not before
    assert plan["residency_changed"] is False
    assert plan["admitted_statement_id"] == ""
    assert plan["evicted_statement_ids"] == ()
    assert set(repository.snapshot()["artifacts"]) == {"stmt-protected"}


def test_unqueried_default_hit_rate_never_protects_dynamic_artifact() -> None:
    untouched = accepted_artifact("stmt-untouched", statistics=artifact_statistics())
    repository = ArtifactRepository((untouched,))

    plan = repository.plan_admission(
        accepted_artifact("stmt-incoming"),
        tier_admission_policy(1, EvictionPolicy.HIT_RATE, minimum_protected_hit_rate=0.3),
    )

    assert plan["outcome"] == AdmissionOutcome.ADMITTED_WITH_EVICTION
    assert plan["evicted_statement_ids"] == ("stmt-untouched",)


@pytest.mark.parametrize(
    ("policy", "first_statistics", "second_statistics", "expected_victim"),
    [
        (EvictionPolicy.FIFO, artifact_statistics(), artifact_statistics(), "stmt-first"),
        (
            EvictionPolicy.LRU,
            artifact_statistics(1, 1, "2026-08-12T18:00:00Z", True),
            artifact_statistics(),
            "stmt-second",
        ),
        (EvictionPolicy.LFU, artifact_statistics(4, 5, "", False), artifact_statistics(1, 5, "", False), "stmt-second"),
        (EvictionPolicy.HIT_RATE, artifact_statistics(4, 5, "", False), artifact_statistics(1, 5, "", False), "stmt-second"),
    ],
)
def test_dynamic_eviction_policies_select_expected_victim(policy, first_statistics, second_statistics, expected_victim) -> None:
    first = accepted_artifact(
        "stmt-first",
        provenance=artifact_provenance("test", "caller", "2026-08-12T16:00:00Z"),
        statistics=first_statistics,
    )
    second = accepted_artifact(
        "stmt-second",
        provenance=artifact_provenance("test", "caller", "2026-08-12T17:00:00Z"),
        statistics=second_statistics,
    )
    repository = ArtifactRepository((first, second))

    plan = repository.plan_admission(accepted_artifact("stmt-new"), tier_admission_policy(2, policy))

    assert plan["outcome"] == AdmissionOutcome.ADMITTED_WITH_EVICTION
    assert plan["evicted_statement_ids"] == (expected_victim,)
    assert expected_victim not in plan["candidate"]["artifacts"]
    assert set(plan["candidate"]["artifacts"]) == ({"stmt-first", "stmt-second", "stmt-new"} - {expected_victim})


def test_migrated_over_capacity_state_evicts_enough_unprotected_victims() -> None:
    artifacts = tuple(accepted_artifact(f"stmt-{position}") for position in range(4))
    repository = ArtifactRepository(artifacts)

    plan = repository.plan_admission(accepted_artifact("stmt-new"), tier_admission_policy(2, EvictionPolicy.FIFO))

    assert plan["evicted_statement_ids"] == ("stmt-0", "stmt-1", "stmt-2")
    assert len([artifact for artifact in plan["candidate"]["artifacts"].values() if artifact["tier"] == Tier.DYNAMIC]) == 2
    assert plan["candidate"]["artifacts"]["stmt-3"]["lifecycle"] == LifecycleState.ACTIVE


def test_admission_reports_all_affected_namespaces_without_changing_epoch_or_lifecycle() -> None:
    old_scope = scope_key(namespace="tenant-old")
    new_scope = scope_key(namespace="tenant-new")
    old = accepted_artifact("stmt-old", scope=old_scope)
    incoming = accepted_artifact("stmt-new", scope=new_scope)
    repository = ArtifactRepository((old,))

    plan = repository.plan_admission(incoming, tier_admission_policy(1, EvictionPolicy.FIFO))

    assert plan["affected_epoch_namespaces"] == ("tenant-new", "tenant-old")
    assert plan["lifecycle_changed"] is False
    assert old["lifecycle"] == LifecycleState.ACTIVE
    assert incoming["lifecycle"] == LifecycleState.ACTIVE


def test_admission_candidate_keeps_artifacts_views_and_indexes_synchronized() -> None:
    old = accepted_artifact("stmt-old")
    repository = ArtifactRepository((old,))
    before = repository.snapshot()
    plan = repository.plan_admission(accepted_artifact("stmt-new"), tier_admission_policy(1, EvictionPolicy.FIFO))

    repository.atomic_replace(plan["candidate"], before["state_generation"])

    state = repository.snapshot()
    assert set(state["artifacts"]) == set(state["statements"]) == set(state["index_state"]["projections"]) == {"stmt-new"}
    assert repository.check()["consistent"] is True


@pytest.mark.parametrize(
    ("policy", "message"),
    [
        (tier_admission_policy(1, EvictionPolicy.FIFO), "already exists"),
        ("policy", "must be a TierAdmissionPolicy"),
    ],
)
def test_admission_rejects_collision_and_wrong_policy_type(policy, message) -> None:
    artifact = accepted_artifact()
    repository = ArtifactRepository((artifact,))
    incoming = artifact if isinstance(policy, Mapping) else accepted_artifact("stmt-new")
    with pytest.raises((ConflictError, InvalidRequestError), match=message):
        repository.plan_admission(incoming, cast(Any, policy))


@pytest.mark.parametrize(
    ("args", "message"),
    [
        ((0, EvictionPolicy.FIFO), "positive integer"),
        ((1, "fifo"), "must be an EvictionPolicy"),
        ((1, EvictionPolicy.FIFO, True), "must be a number"),
        ((1, EvictionPolicy.FIFO, 1.1), "between 0 and 1"),
    ],
)
def test_tier_admission_policy_rejects_invalid_bounds(args, message) -> None:
    with pytest.raises(InvalidRequestError, match=message):
        tier_admission_policy(*args)
