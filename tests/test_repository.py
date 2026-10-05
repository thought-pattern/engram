"""Section 3 authoritative live artifact repository tests."""

from threading import Barrier as threading_Barrier, Thread as threading_Thread

from pytest import mark as pytest_mark, raises as pytest_raises

from engram.artifacts import validate_cached_response_artifact
from engram.constants import (
    INITIAL_ARTIFACT_STATISTICS,
    AdmissionOutcome,
    ExactLookupOutcome,
    LifecycleState,
    RepositoryRemovalReason,
    Tier,
)
from engram.eligibility import ContextualExactLookup, eligibility_context
from engram.errors import ConflictError, InvalidRequestError, ResourceNotFoundError
from engram.identity import extract_standalone_identity, retrieval_representation, scope_key, scoped_retrieval_key_from_text
from engram.repository import (
    ArtifactRepository,
    normalize_repository_state,
    repository_state,
    tier_admission_policy,
    validate_repository_state,
)
from tests.support_fixtures import ASSERTION_REFERENCE_A

TENANT_A_SCOPE = scope_key(namespace="tenant-a")
# Accepted DYNAMIC tenant-a artifact fields with prior use; each test adds the statement id, response, identity and
# retrieval representation ("Question for <id>?" with alias "Alias for <id>" unless it shares a request).
# validate_cached_response_artifact copies its input, so this constant stays read-only.
REPOSITORY_ARTIFACT_FIELDS = {
    "generation": 1,
    "tier": Tier.DYNAMIC,
    "lifecycle": LifecycleState.ACTIVE,
    "scope": TENANT_A_SCOPE,
    "support_references": (ASSERTION_REFERENCE_A,),
    "valid_from": "",
    "valid_from_available": False,
    "valid_until": "",
    "valid_until_available": False,
    "superseded_by": "",
    "provenance": {"source_label": "test", "caller_id": "caller", "accepted_at": "2026-08-12T16:00:00Z"},
    "statistics": {"hit_count": 2, "query_count": 3, "last_hit": "2026-08-12T17:00:00Z", "last_hit_available": True},
    "metadata": {"approved": True},
}
# eligibility_context arguments for an evaluation in tenant-a with the repository available.
LOOKUP_CONTEXT_VALUES = {
    "evaluation_time": "2026-08-12T18:00:00Z",
    "evaluation_time_available": True,
    "namespace": "tenant-a",
    "artifact_repository_available": True,
}
PLAN_FIELDS = {"outcome", "candidate", "admitted_statement_id", "evicted_statement_ids", "residency_changed", "lifecycle_changed"}


def test_repository_state_has_one_accepted_response_representation() -> None:
    artifact = validate_cached_response_artifact(
        {
            **REPOSITORY_ARTIFACT_FIELDS,
            "statement_id": "stmt-1",
            "response": "Exact response for stmt-1.",
            "query_identity": extract_standalone_identity("Question for stmt-1?", TENANT_A_SCOPE),
            "retrieval": retrieval_representation("Question for stmt-1?", ("Alias for stmt-1",)),
        }
    )
    state = normalize_repository_state((artifact,))

    assert set(state) == {"state_generation", "artifacts"}
    assert set(state.get("artifacts", {})) == {"stmt-1"}


def test_repository_build_validates_the_authoritative_collection() -> None:
    first = validate_cached_response_artifact(
        {
            **REPOSITORY_ARTIFACT_FIELDS,
            "statement_id": "stmt-1",
            "response": "Exact response for stmt-1.",
            "query_identity": extract_standalone_identity("Question for stmt-1?", TENANT_A_SCOPE),
            "retrieval": retrieval_representation("Question for stmt-1?", ("Alias for stmt-1",)),
        }
    )
    second = validate_cached_response_artifact(
        {
            **REPOSITORY_ARTIFACT_FIELDS,
            "statement_id": "stmt-2",
            "response": "Exact response for stmt-2.",
            "query_identity": extract_standalone_identity("Question for stmt-2?", TENANT_A_SCOPE),
            "retrieval": retrieval_representation("Question for stmt-2?", ("Alias for stmt-2",)),
            "tier": Tier.STATIC,
        }
    )
    state = normalize_repository_state((second, first))

    assert state.__class__ is dict
    assert set(state) == {"state_generation", "artifacts"}

    malformed_state = dict(state)
    malformed_state["unexpected"] = ()
    with pytest_raises(InvalidRequestError, match="must be a RepositoryState"):
        validate_repository_state(malformed_state)


def test_repository_lookup_does_not_expose_mutable_authority() -> None:
    artifact = validate_cached_response_artifact(
        {
            **REPOSITORY_ARTIFACT_FIELDS,
            "statement_id": "stmt-1",
            "response": "Exact response for stmt-1.",
            "query_identity": extract_standalone_identity("Question for stmt-1?", TENANT_A_SCOPE),
            "retrieval": retrieval_representation("Question for stmt-1?", ("Alias for stmt-1",)),
        }
    )
    repository = ArtifactRepository((artifact,))

    artifact["response"] = "caller artifact rewrite"

    retained_artifact = repository.get_artifact("stmt-1")
    assert retained_artifact.get("response", "") == "Exact response for stmt-1."
    assert retained_artifact is not artifact
    retained_artifact["response"] = "returned artifact rewrite"
    assert "query_count" in retained_artifact.get("statistics", {})
    retained_statistics = retained_artifact.get("statistics", {})
    retained_statistics["query_count"] = 99
    fresh_artifact = repository.get_artifact("stmt-1")
    assert fresh_artifact.get("response", "") == "Exact response for stmt-1."
    assert fresh_artifact.get("statistics", {}).get("query_count", 0) == 3
    snapshot = repository.snapshot()
    snapshot["state_generation"] = 99
    assert repository.snapshot().get("state_generation", 0) == 1


def test_candidate_add_and_atomic_swap_publish_artifacts_atomically() -> None:
    first = validate_cached_response_artifact(
        {
            **REPOSITORY_ARTIFACT_FIELDS,
            "statement_id": "stmt-1",
            "response": "Exact response for stmt-1.",
            "query_identity": extract_standalone_identity("Question for stmt-1?", TENANT_A_SCOPE),
            "retrieval": retrieval_representation("Question for stmt-1?", ("Alias for stmt-1",)),
        }
    )
    second = validate_cached_response_artifact(
        {
            **REPOSITORY_ARTIFACT_FIELDS,
            "statement_id": "stmt-2",
            "response": "Exact response for stmt-2.",
            "query_identity": extract_standalone_identity("Question for stmt-2?", TENANT_A_SCOPE),
            "retrieval": retrieval_representation("Question for stmt-2?", ("Alias for stmt-2",)),
        }
    )
    repository = ArtifactRepository((first,))
    before = repository.snapshot()
    candidate = repository.candidate_with_artifact(second)

    assert set(repository.snapshot().get("artifacts", {})) == {"stmt-1"}
    published = repository.atomic_replace(candidate, before.get("state_generation", 0))

    assert set(published.get("artifacts", {})) == {"stmt-1", "stmt-2"}


def test_atomic_swap_rejects_stale_generation_and_non_next_candidate() -> None:
    first = validate_cached_response_artifact(
        {
            **REPOSITORY_ARTIFACT_FIELDS,
            "statement_id": "stmt-1",
            "response": "Exact response for stmt-1.",
            "query_identity": extract_standalone_identity("Question for stmt-1?", TENANT_A_SCOPE),
            "retrieval": retrieval_representation("Question for stmt-1?", ("Alias for stmt-1",)),
        }
    )
    second = validate_cached_response_artifact(
        {
            **REPOSITORY_ARTIFACT_FIELDS,
            "statement_id": "stmt-2",
            "response": "Exact response for stmt-2.",
            "query_identity": extract_standalone_identity("Question for stmt-2?", TENANT_A_SCOPE),
            "retrieval": retrieval_representation("Question for stmt-2?", ("Alias for stmt-2",)),
        }
    )
    repository = ArtifactRepository((first,))
    before = repository.snapshot()
    candidate = repository.candidate_with_artifact(second)
    before_generation = before.get("state_generation", 0)
    assert before_generation == 1

    with pytest_raises(ConflictError, match="state generation conflict"):
        repository.atomic_replace(candidate, before_generation + 1)
    malformed_generation = repository_state(
        state_generation=before_generation + 2,
        artifacts=candidate.get("artifacts", {}),
    )
    with pytest_raises(ConflictError, match="advance by exactly one"):
        repository.atomic_replace(malformed_generation, before_generation)


def test_capacity_eviction_removes_only_dynamic_without_lifecycle_mutation() -> None:
    dynamic = validate_cached_response_artifact(
        {
            **REPOSITORY_ARTIFACT_FIELDS,
            "statement_id": "stmt-dynamic",
            "response": "Exact response for stmt-dynamic.",
            "query_identity": extract_standalone_identity("Question for stmt-dynamic?", TENANT_A_SCOPE),
            "retrieval": retrieval_representation("Question for stmt-dynamic?", ("Alias for stmt-dynamic",)),
            "lifecycle": LifecycleState.RETIRED,
        }
    )
    static = validate_cached_response_artifact(
        {
            **REPOSITORY_ARTIFACT_FIELDS,
            "statement_id": "stmt-static",
            "response": "Exact response for stmt-static.",
            "query_identity": extract_standalone_identity("Question for stmt-static?", TENANT_A_SCOPE),
            "retrieval": retrieval_representation("Question for stmt-static?", ("Alias for stmt-static",)),
            "tier": Tier.STATIC,
        }
    )
    repository = ArtifactRepository((dynamic, static))
    before = repository.snapshot()

    candidate = repository.candidate_without_artifact(
        "stmt-dynamic",
        dynamic.get("generation", 0),
        RepositoryRemovalReason.CAPACITY_EVICTION,
    )
    repository.atomic_replace(candidate, before.get("state_generation", 0))

    assert "lifecycle" in dynamic
    assert dynamic.get("lifecycle", LifecycleState.RETIRED) == LifecycleState.RETIRED
    assert set(repository.snapshot().get("artifacts", {})) == {"stmt-static"}
    with pytest_raises(ConflictError, match="cannot remove STATIC"):
        repository.candidate_without_artifact(
            "stmt-static",
            static.get("generation", 0),
            RepositoryRemovalReason.CAPACITY_EVICTION,
        )


def test_explicit_delete_uses_expected_generation() -> None:
    artifact = validate_cached_response_artifact(
        {
            **REPOSITORY_ARTIFACT_FIELDS,
            "statement_id": "stmt-1",
            "response": "Exact response for stmt-1.",
            "query_identity": extract_standalone_identity("Question for stmt-1?", TENANT_A_SCOPE),
            "retrieval": retrieval_representation("Question for stmt-1?", ("Alias for stmt-1",)),
        }
    )
    repository = ArtifactRepository((artifact,))
    with pytest_raises(ConflictError, match="generation conflict"):
        repository.candidate_without_artifact("stmt-1", 2, RepositoryRemovalReason.EXPLICIT_DELETE)

    before = repository.snapshot()
    candidate = repository.candidate_without_artifact(
        "stmt-1",
        artifact.get("generation", 0),
        RepositoryRemovalReason.EXPLICIT_DELETE,
    )
    empty = repository.atomic_replace(candidate, before.get("state_generation", 0))

    assert "artifacts" in empty
    assert empty.get("artifacts", {}) == {}


def test_repository_contextual_lookup_does_not_mutate_repository_state() -> None:
    artifact = validate_cached_response_artifact(
        {
            **REPOSITORY_ARTIFACT_FIELDS,
            "statement_id": "stmt-1",
            "response": "Exact response for stmt-1.",
            "query_identity": extract_standalone_identity("Question for stmt-1?", TENANT_A_SCOPE),
            "retrieval": retrieval_representation("Question for stmt-1?", ("Alias for stmt-1",)),
            "valid_until": "2026-08-12T19:00:00Z",
            "valid_until_available": True,
        }
    )
    repository = ArtifactRepository((artifact,))
    canonical = artifact.get("retrieval", {}).get("canonical", "")
    assert canonical
    key = scoped_retrieval_key_from_text(artifact.get("scope", {}), canonical)
    before = repository.snapshot()

    found = repository.exact_lookup(key, eligibility_context(**LOOKUP_CONTEXT_VALUES))

    assert found.get("lookup", {}).get("outcome", ExactLookupOutcome.MISS) == ExactLookupOutcome.FOUND
    after = repository.snapshot()
    assert "state_generation" in after
    assert after.get("state_generation", 0) == before.get("state_generation", 0)


def test_repository_state_rejects_secondary_representations() -> None:
    artifact = validate_cached_response_artifact(
        {
            **REPOSITORY_ARTIFACT_FIELDS,
            "statement_id": "stmt-1",
            "response": "Exact response for stmt-1.",
            "query_identity": extract_standalone_identity("Question for stmt-1?", TENANT_A_SCOPE),
            "retrieval": retrieval_representation("Question for stmt-1?", ("Alias for stmt-1",)),
        }
    )
    state = normalize_repository_state((artifact,))
    extra = {**state, "statements": {}}
    with pytest_raises(InvalidRequestError, match="RepositoryState"):
        validate_repository_state(extra)


def test_repository_concurrent_swap_has_one_winner_and_never_partial_state() -> None:
    base = validate_cached_response_artifact(
        {
            **REPOSITORY_ARTIFACT_FIELDS,
            "statement_id": "stmt-base",
            "response": "Exact response for stmt-base.",
            "query_identity": extract_standalone_identity("Question for stmt-base?", TENANT_A_SCOPE),
            "retrieval": retrieval_representation("Question for stmt-base?", ("Alias for stmt-base",)),
        }
    )
    first = validate_cached_response_artifact(
        {
            **REPOSITORY_ARTIFACT_FIELDS,
            "statement_id": "stmt-a",
            "response": "Exact response for stmt-a.",
            "query_identity": extract_standalone_identity("Question for stmt-a?", TENANT_A_SCOPE),
            "retrieval": retrieval_representation("Question for stmt-a?", ("Alias for stmt-a",)),
        }
    )
    second = validate_cached_response_artifact(
        {
            **REPOSITORY_ARTIFACT_FIELDS,
            "statement_id": "stmt-b",
            "response": "Exact response for stmt-b.",
            "query_identity": extract_standalone_identity("Question for stmt-b?", TENANT_A_SCOPE),
            "retrieval": retrieval_representation("Question for stmt-b?", ("Alias for stmt-b",)),
        }
    )
    repository = ArtifactRepository((base,))
    expected = repository.snapshot().get("state_generation", 0)
    candidates = [
        repository.candidate_with_artifact(first),
        repository.candidate_with_artifact(second),
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
    assert set(state.get("artifacts", {})) in ({"stmt-base", "stmt-a"}, {"stmt-base", "stmt-b"})
    assert set(state) == {"state_generation", "artifacts"}


def test_repository_rejects_duplicate_wrong_and_missing_inputs() -> None:
    artifact = validate_cached_response_artifact(
        {
            **REPOSITORY_ARTIFACT_FIELDS,
            "statement_id": "stmt-1",
            "response": "Exact response for stmt-1.",
            "query_identity": extract_standalone_identity("Question for stmt-1?", TENANT_A_SCOPE),
            "retrieval": retrieval_representation("Question for stmt-1?", ("Alias for stmt-1",)),
        }
    )
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
    existing = validate_cached_response_artifact(
        {
            **REPOSITORY_ARTIFACT_FIELDS,
            "statement_id": "stmt-existing",
            "response": "Exact response for stmt-existing.",
            "query_identity": extract_standalone_identity("Question for stmt-existing?", TENANT_A_SCOPE),
            "retrieval": retrieval_representation("Question for stmt-existing?", ("Alias for stmt-existing",)),
        }
    )
    incoming = validate_cached_response_artifact(
        {
            **REPOSITORY_ARTIFACT_FIELDS,
            "statement_id": "stmt-incoming",
            "response": "Exact response for stmt-incoming.",
            "query_identity": extract_standalone_identity("Question for stmt-incoming?", TENANT_A_SCOPE),
            "retrieval": retrieval_representation("Question for stmt-incoming?", ("Alias for stmt-incoming",)),
        }
    )
    repository = ArtifactRepository((existing,))
    plan = repository.plan_admission(incoming, tier_admission_policy(2))
    candidate = plan.get("candidate", {})

    assert plan.keys() == PLAN_FIELDS
    assert plan.get("outcome", AdmissionOutcome.REJECTED_CAPACITY) == AdmissionOutcome.ADMITTED
    assert candidate.get("state_generation", 0) == 2
    assert plan.get("admitted_statement_id", "") == "stmt-incoming"
    assert plan.get("evicted_statement_ids", ()) == ()
    assert plan.get("residency_changed", False) is True
    assert plan.get("lifecycle_changed", False) is False
    assert set(candidate.get("artifacts", {})) == {"stmt-existing", "stmt-incoming"}
    assert existing.get("lifecycle", LifecycleState.RETIRED) == LifecycleState.ACTIVE


def test_static_admission_never_consumes_or_evicts_dynamic_capacity() -> None:
    dynamic = validate_cached_response_artifact(
        {
            **REPOSITORY_ARTIFACT_FIELDS,
            "statement_id": "stmt-dynamic",
            "response": "Exact response for stmt-dynamic.",
            "query_identity": extract_standalone_identity("Question for stmt-dynamic?", TENANT_A_SCOPE),
            "retrieval": retrieval_representation("Question for stmt-dynamic?", ("Alias for stmt-dynamic",)),
        }
    )
    incoming = validate_cached_response_artifact(
        {
            **REPOSITORY_ARTIFACT_FIELDS,
            "statement_id": "stmt-static",
            "response": "Exact response for stmt-static.",
            "query_identity": extract_standalone_identity("Question for stmt-static?", TENANT_A_SCOPE),
            "retrieval": retrieval_representation("Question for stmt-static?", ("Alias for stmt-static",)),
            "tier": Tier.STATIC,
        }
    )
    repository = ArtifactRepository((dynamic,))

    plan = repository.plan_admission(incoming, tier_admission_policy(1))

    assert plan.keys() == PLAN_FIELDS
    assert plan.get("outcome", AdmissionOutcome.REJECTED_CAPACITY) == AdmissionOutcome.ADMITTED
    assert plan.get("evicted_statement_ids", ()) == ()
    assert set(plan.get("candidate", {}).get("artifacts", {})) == {"stmt-dynamic", "stmt-static"}


def test_frequently_used_dynamic_artifact_remains_lru_evictable() -> None:
    existing = validate_cached_response_artifact(
        {
            **REPOSITORY_ARTIFACT_FIELDS,
            "statement_id": "stmt-existing",
            "response": "Exact response for stmt-existing.",
            "query_identity": extract_standalone_identity("Question for stmt-existing?", TENANT_A_SCOPE),
            "retrieval": retrieval_representation("Question for stmt-existing?", ("Alias for stmt-existing",)),
            "statistics": {"hit_count": 10, "query_count": 10, "last_hit": "2026-08-12T17:00:00Z", "last_hit_available": True},
        }
    )
    incoming = validate_cached_response_artifact(
        {
            **REPOSITORY_ARTIFACT_FIELDS,
            "statement_id": "stmt-incoming",
            "response": "Exact response for stmt-incoming.",
            "query_identity": extract_standalone_identity("Question for stmt-incoming?", TENANT_A_SCOPE),
            "retrieval": retrieval_representation("Question for stmt-incoming?", ("Alias for stmt-incoming",)),
        }
    )
    repository = ArtifactRepository((existing,))

    plan = repository.plan_admission(incoming, tier_admission_policy(1))

    assert plan.get("outcome", AdmissionOutcome.REJECTED_CAPACITY) == AdmissionOutcome.ADMITTED_WITH_EVICTION
    assert plan.get("evicted_statement_ids", ()) == ("stmt-existing",)


def test_unqueried_default_hit_rate_never_protects_dynamic_artifact() -> None:
    untouched = validate_cached_response_artifact(
        {
            **REPOSITORY_ARTIFACT_FIELDS,
            "statement_id": "stmt-untouched",
            "response": "Exact response for stmt-untouched.",
            "query_identity": extract_standalone_identity("Question for stmt-untouched?", TENANT_A_SCOPE),
            "retrieval": retrieval_representation("Question for stmt-untouched?", ("Alias for stmt-untouched",)),
            "statistics": INITIAL_ARTIFACT_STATISTICS,
        }
    )
    incoming = validate_cached_response_artifact(
        {
            **REPOSITORY_ARTIFACT_FIELDS,
            "statement_id": "stmt-incoming",
            "response": "Exact response for stmt-incoming.",
            "query_identity": extract_standalone_identity("Question for stmt-incoming?", TENANT_A_SCOPE),
            "retrieval": retrieval_representation("Question for stmt-incoming?", ("Alias for stmt-incoming",)),
        }
    )
    repository = ArtifactRepository((untouched,))

    plan = repository.plan_admission(incoming, tier_admission_policy(1))

    assert plan.get("outcome", AdmissionOutcome.REJECTED_CAPACITY) == AdmissionOutcome.ADMITTED_WITH_EVICTION
    assert plan.get("evicted_statement_ids", ()) == ("stmt-untouched",)


@pytest_mark.parametrize(
    ("first_statistics", "second_statistics", "expected_victim"),
    [
        (
            {"hit_count": 1, "query_count": 1, "last_hit": "2026-08-12T18:00:00Z", "last_hit_available": True},
            INITIAL_ARTIFACT_STATISTICS,
            "stmt-second",
        ),
        (INITIAL_ARTIFACT_STATISTICS, INITIAL_ARTIFACT_STATISTICS, "stmt-first"),
    ],
)
def test_dynamic_lru_selects_expected_victim(first_statistics, second_statistics, expected_victim) -> None:
    first = validate_cached_response_artifact(
        {
            **REPOSITORY_ARTIFACT_FIELDS,
            "statement_id": "stmt-first",
            "response": "Exact response for stmt-first.",
            "query_identity": extract_standalone_identity("Question for stmt-first?", TENANT_A_SCOPE),
            "retrieval": retrieval_representation("Question for stmt-first?", ("Alias for stmt-first",)),
            "provenance": {"source_label": "test", "caller_id": "caller", "accepted_at": "2026-08-12T16:00:00Z"},
            "statistics": first_statistics,
        }
    )
    second = validate_cached_response_artifact(
        {
            **REPOSITORY_ARTIFACT_FIELDS,
            "statement_id": "stmt-second",
            "response": "Exact response for stmt-second.",
            "query_identity": extract_standalone_identity("Question for stmt-second?", TENANT_A_SCOPE),
            "retrieval": retrieval_representation("Question for stmt-second?", ("Alias for stmt-second",)),
            "provenance": {"source_label": "test", "caller_id": "caller", "accepted_at": "2026-08-12T17:00:00Z"},
            "statistics": second_statistics,
        }
    )
    incoming = validate_cached_response_artifact(
        {
            **REPOSITORY_ARTIFACT_FIELDS,
            "statement_id": "stmt-new",
            "response": "Exact response for stmt-new.",
            "query_identity": extract_standalone_identity("Question for stmt-new?", TENANT_A_SCOPE),
            "retrieval": retrieval_representation("Question for stmt-new?", ("Alias for stmt-new",)),
        }
    )
    repository = ArtifactRepository((first, second))

    plan = repository.plan_admission(incoming, tier_admission_policy(2))
    candidate_artifacts = plan.get("candidate", {}).get("artifacts", {})

    assert plan.get("outcome", AdmissionOutcome.REJECTED_CAPACITY) == AdmissionOutcome.ADMITTED_WITH_EVICTION
    assert plan.get("evicted_statement_ids", ()) == (expected_victim,)
    assert expected_victim not in candidate_artifacts
    assert set(candidate_artifacts) == ({"stmt-first", "stmt-second", "stmt-new"} - {expected_victim})


def test_preloaded_over_capacity_state_evicts_enough_unprotected_victims() -> None:
    artifacts = tuple(
        validate_cached_response_artifact(
            {
                **REPOSITORY_ARTIFACT_FIELDS,
                "statement_id": f"stmt-{position}",
                "response": f"Exact response for stmt-{position}.",
                "query_identity": extract_standalone_identity(f"Question for stmt-{position}?", TENANT_A_SCOPE),
                "retrieval": retrieval_representation(f"Question for stmt-{position}?", (f"Alias for stmt-{position}",)),
            }
        )
        for position in range(4)
    )
    incoming = validate_cached_response_artifact(
        {
            **REPOSITORY_ARTIFACT_FIELDS,
            "statement_id": "stmt-new",
            "response": "Exact response for stmt-new.",
            "query_identity": extract_standalone_identity("Question for stmt-new?", TENANT_A_SCOPE),
            "retrieval": retrieval_representation("Question for stmt-new?", ("Alias for stmt-new",)),
        }
    )
    repository = ArtifactRepository(artifacts)

    plan = repository.plan_admission(incoming, tier_admission_policy(2))
    candidate_artifacts = plan.get("candidate", {}).get("artifacts", {})
    dynamic_ids = [
        statement_id
        for statement_id, artifact in candidate_artifacts.items()
        if artifact.get("tier", Tier.STATIC) == Tier.DYNAMIC
    ]

    assert plan.get("evicted_statement_ids", ()) == ("stmt-0", "stmt-1", "stmt-2")
    assert len(dynamic_ids) == 2
    assert candidate_artifacts.get("stmt-3", {}).get("lifecycle", LifecycleState.RETIRED) == LifecycleState.ACTIVE


def test_admission_across_namespaces_does_not_change_lifecycle() -> None:
    old_scope = scope_key(namespace="tenant-old")
    new_scope = scope_key(namespace="tenant-new")
    old = validate_cached_response_artifact(
        {
            **REPOSITORY_ARTIFACT_FIELDS,
            "statement_id": "stmt-old",
            "response": "Exact response for stmt-old.",
            "query_identity": extract_standalone_identity("Question for stmt-old?", old_scope),
            "retrieval": retrieval_representation("Question for stmt-old?", ("Alias for stmt-old",)),
            "scope": old_scope,
        }
    )
    incoming = validate_cached_response_artifact(
        {
            **REPOSITORY_ARTIFACT_FIELDS,
            "statement_id": "stmt-new",
            "response": "Exact response for stmt-new.",
            "query_identity": extract_standalone_identity("Question for stmt-new?", new_scope),
            "retrieval": retrieval_representation("Question for stmt-new?", ("Alias for stmt-new",)),
            "scope": new_scope,
        }
    )
    repository = ArtifactRepository((old,))

    plan = repository.plan_admission(incoming, tier_admission_policy(1))

    assert plan.keys() == PLAN_FIELDS
    assert plan.get("lifecycle_changed", False) is False
    assert old.get("lifecycle", LifecycleState.RETIRED) == LifecycleState.ACTIVE
    assert incoming.get("lifecycle", LifecycleState.RETIRED) == LifecycleState.ACTIVE


def test_admission_candidate_publishes_only_artifacts() -> None:
    old = validate_cached_response_artifact(
        {
            **REPOSITORY_ARTIFACT_FIELDS,
            "statement_id": "stmt-old",
            "response": "Exact response for stmt-old.",
            "query_identity": extract_standalone_identity("Question for stmt-old?", TENANT_A_SCOPE),
            "retrieval": retrieval_representation("Question for stmt-old?", ("Alias for stmt-old",)),
        }
    )
    incoming = validate_cached_response_artifact(
        {
            **REPOSITORY_ARTIFACT_FIELDS,
            "statement_id": "stmt-new",
            "response": "Exact response for stmt-new.",
            "query_identity": extract_standalone_identity("Question for stmt-new?", TENANT_A_SCOPE),
            "retrieval": retrieval_representation("Question for stmt-new?", ("Alias for stmt-new",)),
        }
    )
    repository = ArtifactRepository((old,))
    before = repository.snapshot()
    plan = repository.plan_admission(incoming, tier_admission_policy(1))

    repository.atomic_replace(plan.get("candidate", {}), before.get("state_generation", 0))

    state = repository.snapshot()
    assert set(state.get("artifacts", {})) == {"stmt-new"}
    assert set(state) == {"state_generation", "artifacts"}


@pytest_mark.parametrize(
    ("policy", "message"),
    [
        (tier_admission_policy(1), "already exists"),
        ("policy", "must be a TierAdmissionPolicy"),
    ],
)
def test_admission_rejects_collision_and_wrong_policy_type(policy, message) -> None:
    artifact = validate_cached_response_artifact(
        {
            **REPOSITORY_ARTIFACT_FIELDS,
            "statement_id": "stmt-1",
            "response": "Exact response for stmt-1.",
            "query_identity": extract_standalone_identity("Question for stmt-1?", TENANT_A_SCOPE),
            "retrieval": retrieval_representation("Question for stmt-1?", ("Alias for stmt-1",)),
        }
    )
    repository = ArtifactRepository((artifact,))
    incoming = artifact
    if not isinstance(policy, dict):
        incoming = validate_cached_response_artifact(
            {
                **REPOSITORY_ARTIFACT_FIELDS,
                "statement_id": "stmt-new",
                "response": "Exact response for stmt-new.",
                "query_identity": extract_standalone_identity("Question for stmt-new?", TENANT_A_SCOPE),
                "retrieval": retrieval_representation("Question for stmt-new?", ("Alias for stmt-new",)),
            }
        )
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


def test_exact_key_index_follows_every_state_change_and_matches_a_full_scan() -> None:
    shared = "Shared question?"
    first = validate_cached_response_artifact(
        {
            **REPOSITORY_ARTIFACT_FIELDS,
            "statement_id": "stmt-1",
            "response": "Exact response for stmt-1.",
            "query_identity": extract_standalone_identity("Question for stmt-1?", TENANT_A_SCOPE),
            "retrieval": retrieval_representation("Question for stmt-1?", ("Alias for stmt-1",)),
        }
    )
    second = validate_cached_response_artifact(
        {
            **REPOSITORY_ARTIFACT_FIELDS,
            "statement_id": "stmt-2",
            "response": "Exact response for stmt-2.",
            "query_identity": extract_standalone_identity(shared, TENANT_A_SCOPE),
            "retrieval": retrieval_representation(shared, ("Alias for stmt-2",)),
        }
    )
    third = validate_cached_response_artifact(
        {
            **REPOSITORY_ARTIFACT_FIELDS,
            "statement_id": "stmt-3",
            "response": "Exact response for stmt-3.",
            "query_identity": extract_standalone_identity("Question for stmt-3?", TENANT_A_SCOPE),
            "retrieval": retrieval_representation("Question for stmt-3?", ("Alias for stmt-3",)),
        }
    )
    fourth = validate_cached_response_artifact(
        {
            **REPOSITORY_ARTIFACT_FIELDS,
            "statement_id": "stmt-4",
            "response": "Exact response for stmt-4.",
            "query_identity": extract_standalone_identity(shared, TENANT_A_SCOPE),
            "retrieval": retrieval_representation(shared, ("Alias for stmt-4",)),
        }
    )
    updated = validate_cached_response_artifact(
        {
            **REPOSITORY_ARTIFACT_FIELDS,
            "statement_id": "stmt-1",
            "response": "Exact response for stmt-1.",
            "query_identity": extract_standalone_identity("Question for stmt-1 changed?", TENANT_A_SCOPE),
            "retrieval": retrieval_representation("Question for stmt-1 changed?", ("Alias for stmt-1",)),
            "generation": 2,
        }
    )
    repository = ArtifactRepository((first, second, third))
    before_generation = repository.snapshot().get("state_generation", 0)
    assert before_generation == 1
    added = repository.candidate_with_artifact(fourth)
    repository.atomic_replace(added, before_generation)
    repository.atomic_replace(repository.candidate_with_artifacts((updated,)), before_generation + 1)
    removed = repository.candidate_without_artifact("stmt-3", 1, RepositoryRemovalReason.EXPLICIT_DELETE)
    repository.atomic_replace(removed, before_generation + 2)
    rolled_back_to = repository.snapshot()
    repository.atomic_replace(
        repository.candidate_without_artifact("stmt-2", 1, RepositoryRemovalReason.EXPLICIT_DELETE),
        rolled_back_to.get("state_generation", 0),
    )
    repository.restore_state(rolled_back_to)

    rebuilt = ArtifactRepository(tuple(repository.snapshot().get("artifacts", {}).values()))
    assert repository.internal_key_owners == rebuilt.internal_key_owners
    assert repository.internal_dynamic_ids == rebuilt.internal_dynamic_ids
    requests = (shared, "Question for stmt-1?", "Question for stmt-1 changed?", "Question for stmt-3?", "Alias for stmt-4")
    for request in requests:
        key = scoped_retrieval_key_from_text(TENANT_A_SCOPE, request)
        scanned = ContextualExactLookup(repository.trusted_artifacts(), trusted_artifacts=True).exact_lookup(
            key, eligibility_context(**LOOKUP_CONTEXT_VALUES)
        )
        assert repository.exact_lookup(key, eligibility_context(**LOOKUP_CONTEXT_VALUES)) == scanned
    shared_key = scoped_retrieval_key_from_text(TENANT_A_SCOPE, shared)
    shared_lookup = repository.exact_lookup(shared_key, eligibility_context(**LOOKUP_CONTEXT_VALUES)).get("lookup", {})
    assert shared_lookup.get("outcome", ExactLookupOutcome.MISS) == ExactLookupOutcome.COLLISION
