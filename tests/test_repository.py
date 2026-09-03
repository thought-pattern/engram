"""Section 3 authoritative live artifact repository tests."""

from threading import Barrier as threading_Barrier, Thread as threading_Thread

from pytest import mark as pytest_mark, raises as pytest_raises

from engram.artifacts import LifecycleState, artifact_provenance, artifact_statistics, cached_response_artifact
from engram.constants import ExactLookupOutcome, Tier
from engram.eligibility import eligibility_context
from engram.errors import ConflictError, InvalidRequestError, ResourceNotFoundError
from engram.identity import build_retrieval_representation, build_scoped_retrieval_key, build_standalone_identity, scope_key
from engram.repository import (
    AdmissionOutcome,
    ArtifactRepository,
    RepositoryRemovalReason,
    admission_plan_to_dict,
    normalize_repository_state,
    repository_state,
    tier_admission_policy,
    validate_repository_state,
)

from .support_fixtures import ASSERTION_REFERENCE_A


def accepted_artifact(statement_id="stmt-1", tier=Tier.DYNAMIC, **overrides) -> dict:
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
        "support_references": (ASSERTION_REFERENCE_A,),
        "valid_from": "",
        "valid_from_available": False,
        "valid_until": "",
        "valid_until_available": False,
        "superseded_by": "",
        "provenance": artifact_provenance("test", "caller", "2026-08-12T16:00:00Z"),
        "statistics": artifact_statistics(2, 3, "2026-08-12T17:00:00Z", True),
        "metadata": {"approved": True},
    }
    values.update(overrides)
    result = cached_response_artifact(**values)
    return result


def context() -> dict:
    result = eligibility_context(
        evaluation_time="2026-08-12T18:00:00Z",
        evaluation_time_available=True,
        namespace="tenant-a",
        artifact_repository_available=True,
    )
    return result


def test_repository_state_has_one_accepted_response_representation() -> None:
    state = normalize_repository_state((accepted_artifact(),))

    assert set(state) == {"state_generation", "artifacts"}
    assert set(state["artifacts"]) == {"stmt-1"}


def test_repository_build_validates_the_authoritative_collection() -> None:
    first = accepted_artifact("stmt-1")
    second = accepted_artifact("stmt-2", tier=Tier.STATIC)
    state = normalize_repository_state((second, first))

    assert state.__class__ is dict
    assert set(state) == {"state_generation", "artifacts"}

    malformed_state = dict(state)
    malformed_state["unexpected"] = ()
    with pytest_raises(InvalidRequestError, match="must be a RepositoryState"):
        validate_repository_state(malformed_state)


def test_repository_lookup_does_not_expose_mutable_authority() -> None:
    artifact = accepted_artifact()
    repository = ArtifactRepository((artifact,))

    artifact["response"] = "caller artifact rewrite"

    retained_artifact = repository.get_artifact(artifact["statement_id"])
    assert retained_artifact["response"] == "Exact response for stmt-1."
    assert retained_artifact is not artifact
    retained_artifact["response"] = "returned artifact rewrite"
    retained_artifact["statistics"]["query_count"] = 99
    fresh_artifact = repository.get_artifact(artifact["statement_id"])
    assert fresh_artifact["response"] == "Exact response for stmt-1."
    assert fresh_artifact["statistics"]["query_count"] == 3
    snapshot = repository.snapshot()
    snapshot["state_generation"] = 99
    assert repository.snapshot()["state_generation"] == 1


def test_candidate_add_and_atomic_swap_publish_artifacts_atomically() -> None:
    repository = ArtifactRepository((accepted_artifact("stmt-1"),))
    before = repository.snapshot()
    candidate = repository.candidate_with_artifact(accepted_artifact("stmt-2"))

    assert set(repository.snapshot()["artifacts"]) == {"stmt-1"}
    published = repository.atomic_replace(candidate, before["state_generation"])

    assert set(published["artifacts"]) == {"stmt-1", "stmt-2"}


def test_atomic_swap_rejects_stale_generation_and_non_next_candidate() -> None:
    repository = ArtifactRepository((accepted_artifact("stmt-1"),))
    before = repository.snapshot()
    candidate = repository.candidate_with_artifact(accepted_artifact("stmt-2"))

    with pytest_raises(ConflictError, match="state generation conflict"):
        repository.atomic_replace(candidate, before["state_generation"] + 1)
    malformed_generation = repository_state(
        state_generation=before["state_generation"] + 2,
        artifacts=candidate["artifacts"],
    )
    with pytest_raises(ConflictError, match="advance by exactly one"):
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
    with pytest_raises(ConflictError, match="cannot remove STATIC"):
        repository.candidate_without_artifact(
            static["statement_id"],
            static["generation"],
            RepositoryRemovalReason.CAPACITY_EVICTION,
        )


def test_explicit_delete_uses_expected_generation() -> None:
    artifact = accepted_artifact()
    repository = ArtifactRepository((artifact,))
    with pytest_raises(ConflictError, match="generation conflict"):
        repository.candidate_without_artifact(artifact["statement_id"], 2, RepositoryRemovalReason.EXPLICIT_DELETE)

    before = repository.snapshot()
    candidate = repository.candidate_without_artifact(
        artifact["statement_id"],
        artifact["generation"],
        RepositoryRemovalReason.EXPLICIT_DELETE,
    )
    empty = repository.atomic_replace(candidate, before["state_generation"])

    assert empty["artifacts"] == {}


def test_repository_contextual_lookup_does_not_mutate_repository_state() -> None:
    artifact = accepted_artifact(valid_until="2026-08-12T19:00:00Z", valid_until_available=True)
    repository = ArtifactRepository((artifact,))
    key = build_scoped_retrieval_key(artifact["scope"], artifact["retrieval"]["canonical"])
    before = repository.snapshot()

    found = repository.exact_lookup(key, context())

    assert found["lookup"]["outcome"] == ExactLookupOutcome.FOUND
    after = repository.snapshot()
    assert after["state_generation"] == before["state_generation"]


def test_repository_state_rejects_secondary_representations() -> None:
    artifact = accepted_artifact()
    state = normalize_repository_state((artifact,))
    extra = {**state, "statements": {}}
    with pytest_raises(InvalidRequestError, match="RepositoryState"):
        validate_repository_state(extra)


def test_repository_concurrent_swap_has_one_winner_and_never_partial_state() -> None:
    repository = ArtifactRepository((accepted_artifact("stmt-base"),))
    expected = repository.snapshot()["state_generation"]
    candidates = [
        repository.candidate_with_artifact(accepted_artifact("stmt-a")),
        repository.candidate_with_artifact(accepted_artifact("stmt-b")),
    ]
    barrier = threading_Barrier(3)
    successes = []
    conflicts = []

    def publish(candidate) -> None:
        barrier.wait()
        try:
            successes.append(repository.atomic_replace(candidate, expected))
        except ConflictError as error:
            conflicts.append(str(error))

    threads = [threading_Thread(target=publish, args=(candidate,)) for candidate in candidates]
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join()

    assert len(successes) == 1
    assert len(conflicts) == 1
    state = repository.snapshot()
    assert set(state["artifacts"]) in ({"stmt-base", "stmt-a"}, {"stmt-base", "stmt-b"})
    assert set(state) == {"state_generation", "artifacts"}


def test_repository_rejects_duplicate_wrong_and_missing_inputs() -> None:
    artifact = accepted_artifact()
    with pytest_raises(ConflictError, match="duplicate"):
        normalize_repository_state((artifact, artifact))
    with pytest_raises(InvalidRequestError):
        normalize_repository_state({artifact.get("statement_id", ""): artifact})
    repository = ArtifactRepository()
    with pytest_raises(ResourceNotFoundError, match="not found"):
        repository.get_artifact("missing")
    with pytest_raises(InvalidRequestError, match="RepositoryRemovalReason"):
        repository.candidate_without_artifact("missing", 1, "delete")


def test_dynamic_admission_below_capacity_changes_no_existing_residency() -> None:
    existing = accepted_artifact("stmt-existing")
    incoming = accepted_artifact("stmt-incoming")
    repository = ArtifactRepository((existing,))
    plan = repository.plan_admission(incoming, tier_admission_policy(2))

    assert admission_plan_to_dict(plan) == {
        "outcome": "ADMITTED",
        "candidate_state_generation": 2,
        "admitted_statement_id": "stmt-incoming",
        "evicted_statement_ids": [],
        "residency_changed": True,
        "lifecycle_changed": False,
    }
    assert set(plan["candidate"]["artifacts"]) == {"stmt-existing", "stmt-incoming"}
    assert existing["lifecycle"] == LifecycleState.ACTIVE


def test_static_admission_never_consumes_or_evicts_dynamic_capacity() -> None:
    dynamic = accepted_artifact("stmt-dynamic")
    incoming = accepted_artifact("stmt-static", tier=Tier.STATIC)
    repository = ArtifactRepository((dynamic,))

    plan = repository.plan_admission(incoming, tier_admission_policy(1))

    assert plan["outcome"] == AdmissionOutcome.ADMITTED
    assert plan["evicted_statement_ids"] == ()
    assert set(plan["candidate"]["artifacts"]) == {"stmt-dynamic", "stmt-static"}


def test_frequently_used_dynamic_artifact_remains_lru_evictable() -> None:
    existing = accepted_artifact(
        "stmt-existing",
        statistics=artifact_statistics(10, 10, "2026-08-12T17:00:00Z", True),
    )
    incoming = accepted_artifact("stmt-incoming")
    repository = ArtifactRepository((existing,))

    plan = repository.plan_admission(incoming, tier_admission_policy(1))

    assert plan["outcome"] == AdmissionOutcome.ADMITTED_WITH_EVICTION
    assert plan["evicted_statement_ids"] == ("stmt-existing",)


def test_unqueried_default_hit_rate_never_protects_dynamic_artifact() -> None:
    untouched = accepted_artifact("stmt-untouched", statistics=artifact_statistics())
    repository = ArtifactRepository((untouched,))

    plan = repository.plan_admission(
        accepted_artifact("stmt-incoming"),
        tier_admission_policy(1),
    )

    assert plan["outcome"] == AdmissionOutcome.ADMITTED_WITH_EVICTION
    assert plan["evicted_statement_ids"] == ("stmt-untouched",)


@pytest_mark.parametrize(
    ("first_statistics", "second_statistics", "expected_victim"),
    [
        (
            artifact_statistics(1, 1, "2026-08-12T18:00:00Z", True),
            artifact_statistics(),
            "stmt-second",
        ),
        (artifact_statistics(), artifact_statistics(), "stmt-first"),
    ],
)
def test_dynamic_lru_selects_expected_victim(first_statistics, second_statistics, expected_victim) -> None:
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

    plan = repository.plan_admission(accepted_artifact("stmt-new"), tier_admission_policy(2))

    assert plan["outcome"] == AdmissionOutcome.ADMITTED_WITH_EVICTION
    assert plan["evicted_statement_ids"] == (expected_victim,)
    assert expected_victim not in plan["candidate"]["artifacts"]
    assert set(plan["candidate"]["artifacts"]) == ({"stmt-first", "stmt-second", "stmt-new"} - {expected_victim})


def test_preloaded_over_capacity_state_evicts_enough_unprotected_victims() -> None:
    artifacts = tuple(accepted_artifact(f"stmt-{position}") for position in range(4))
    repository = ArtifactRepository(artifacts)

    plan = repository.plan_admission(accepted_artifact("stmt-new"), tier_admission_policy(2))

    assert plan["evicted_statement_ids"] == ("stmt-0", "stmt-1", "stmt-2")
    assert len([artifact for artifact in plan["candidate"]["artifacts"].values() if artifact["tier"] == Tier.DYNAMIC]) == 2
    assert plan["candidate"]["artifacts"]["stmt-3"]["lifecycle"] == LifecycleState.ACTIVE


def test_admission_across_namespaces_does_not_change_lifecycle() -> None:
    old_scope = scope_key(namespace="tenant-old")
    new_scope = scope_key(namespace="tenant-new")
    old = accepted_artifact("stmt-old", scope=old_scope)
    incoming = accepted_artifact("stmt-new", scope=new_scope)
    repository = ArtifactRepository((old,))

    plan = repository.plan_admission(incoming, tier_admission_policy(1))

    assert plan["lifecycle_changed"] is False
    assert old["lifecycle"] == LifecycleState.ACTIVE
    assert incoming["lifecycle"] == LifecycleState.ACTIVE


def test_admission_candidate_publishes_only_artifacts() -> None:
    old = accepted_artifact("stmt-old")
    repository = ArtifactRepository((old,))
    before = repository.snapshot()
    plan = repository.plan_admission(accepted_artifact("stmt-new"), tier_admission_policy(1))

    repository.atomic_replace(plan["candidate"], before["state_generation"])

    state = repository.snapshot()
    assert set(state["artifacts"]) == {"stmt-new"}
    assert set(state) == {"state_generation", "artifacts"}


@pytest_mark.parametrize(
    ("policy", "message"),
    [
        (tier_admission_policy(1), "already exists"),
        ("policy", "must be a TierAdmissionPolicy"),
    ],
)
def test_admission_rejects_collision_and_wrong_policy_type(policy, message) -> None:
    artifact = accepted_artifact()
    repository = ArtifactRepository((artifact,))
    incoming = artifact if isinstance(policy, dict) else accepted_artifact("stmt-new")
    with pytest_raises((ConflictError, InvalidRequestError), match=message):
        repository.plan_admission(incoming, policy)


@pytest_mark.parametrize(
    ("args", "message"),
    [
        ((0,), "positive integer"),
        (("1",), "positive integer"),
    ],
)
def test_tier_admission_policy_rejects_invalid_bounds(args, message) -> None:
    with pytest_raises(InvalidRequestError, match=message):
        tier_admission_policy(*args)
