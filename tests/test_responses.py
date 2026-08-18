"""Section 3 transport-neutral accepted-response command tests."""

import threading
from collections.abc import Mapping
from typing import Any, cast

import pytest

from engram import persistence
from engram.artifacts import (
    CachedResponseArtifact,
    LifecycleState,
    artifact_provenance,
    artifact_statistics,
    cached_response_artifact,
)
from engram.constants import EvictionPolicy, Tier
from engram.coordination import (
    AtomicMutationCoordinator,
    CheckpointFailureError,
    CheckpointFailureKind,
    coordinated_response_state_durable_signature,
    coordinated_response_state_signature,
)
from engram.core import Engram
from engram.eligibility import NamespaceEpochState
from engram.errors import ConflictError, InvalidRequestError, LifecycleError
from engram.identity import build_retrieval_representation, build_standalone_identity, retrieval_representation_bindings, scope_key
from engram.mutations import MutationReceiptLedger, MutationResultCode, ReceiptLookupOutcome, mutation_receipt_to_dict
from engram.repository import ArtifactRepository, tier_admission_policy
from engram.responses import (
    AcceptedResponseService,
    LifecycleMutationReason,
    response_mutation_result,
    response_mutation_result_to_dict,
)


def artifact(
    statement_id: str,
    *,
    request: str = "What is Engram?",
    response: str = "Engram preserves this response exactly: café ☕.",
    aliases: tuple[str, ...] = (),
    namespace: str = "tenant-a",
    tier: Tier = Tier.DYNAMIC,
    lifecycle: LifecycleState = LifecycleState.ACTIVE,
    generation: int = 1,
    statistics=(),
) -> CachedResponseArtifact:
    scope = scope_key(namespace=namespace)
    selected_statistics = statistics if isinstance(statistics, Mapping) and statistics else artifact_statistics()
    result = cached_response_artifact(
        statement_id=statement_id,
        generation=generation,
        response=response,
        query_identity=build_standalone_identity(request, scope),
        retrieval=build_retrieval_representation(request, aliases),
        tier=tier,
        lifecycle=lifecycle,
        scope=scope,
        support_claim_ids=(),
        valid_from="",
        valid_from_available=False,
        valid_until="",
        valid_until_available=False,
        knowledge_epoch=0,
        knowledge_epoch_available=False,
        superseded_by="",
        provenance=artifact_provenance("test", "caller-a", "2026-08-12T16:00:00Z"),
        statistics=selected_statistics,
        metadata={"approved": True},
    )
    return result


def service(
    repository=(),
    *,
    capacity: int = 10,
    protected_rate: float = 0.0,
    checkpoints=(),
) -> AcceptedResponseService:
    artifact_repository = repository if isinstance(repository, ArtifactRepository) else ArtifactRepository()
    checkpoint_configured = isinstance(checkpoints, list)
    coordinator = AtomicMutationCoordinator(
        artifact_repository,
        NamespaceEpochState(),
        MutationReceiptLedger(),
        checkpoint_configured=checkpoint_configured,
        checkpoint=checkpoints.append if checkpoint_configured else lambda _state: None,
    )
    result = AcceptedResponseService(
        coordinator,
        tier_admission_policy(capacity, EvictionPolicy.FIFO, protected_rate),
        clock=lambda: "2026-08-12T18:00:00Z",
    )
    return result


def object_mapping(value: object) -> Mapping[str, object]:
    assert isinstance(value, Mapping)
    return value


def test_base_commit_preserves_exact_unicode_and_publishes_complete_state() -> None:
    command = service()
    accepted = artifact("stmt-1")

    result = command.commit_response(accepted, "request-1")

    assert isinstance(result, dict)
    assert result["receipt"]["result_code"] == MutationResultCode.CREATED
    assert result["replayed"] is False
    assert result["checkpoint_count"] == 0
    state = command.coordinator.snapshot()
    repository = state["repository"]
    assert repository["artifacts"]["stmt-1"]["response"] == accepted["response"]
    assert repository["statements"]["stmt-1"]["text"] == accepted["response"]
    assert repository["index_state"]["statement_to_retrieval"]["stmt-1"] == retrieval_representation_bindings(
        accepted["retrieval"],
        accepted["scope"],
    )
    assert object_mapping(state["namespace_epochs"]["epochs"])["tenant-a"] == 1


def test_response_mutation_result_is_a_revalidated_plain_dictionary() -> None:
    result = service().commit_response(artifact("stmt-1"), "request-1")

    serialized = response_mutation_result_to_dict(result)

    assert type(result) is dict
    assert serialized["request_id"] == "request-1"
    assert serialized["operation"] == "COMMIT_RESPONSE"
    assert serialized["result_code"] == "CREATED"
    assert serialized["replayed"] is False
    with pytest.raises(InvalidRequestError, match="cannot perform a checkpoint"):
        response_mutation_result(
            receipt=result["receipt"],
            replayed=True,
            checkpoint_count=1,
            durable=False,
            recovered=False,
        )

    malformed = dict(result)
    malformed["checkpoint_count"] = 2
    with pytest.raises(InvalidRequestError, match="zero or one"):
        response_mutation_result_to_dict(cast(Any, malformed))


@pytest.mark.parametrize("response", ["", "IDK", "  IdK!  "])
def test_base_commit_rejects_empty_or_complete_normalized_idk(response: str) -> None:
    with pytest.raises(InvalidRequestError, match="response must not be empty|IDK"):
        service().commit_response(artifact("stmt-1", response=response), "request-1")


@pytest.mark.parametrize(
    ("lifecycle", "generation", "message"),
    [
        (LifecycleState.RETIRED, 1, "ACTIVE"),
        (LifecycleState.ACTIVE, 2, "generation 1"),
    ],
)
def test_base_commit_rejects_nonactive_or_noninitial_artifact(lifecycle, generation: int, message: str) -> None:
    with pytest.raises(InvalidRequestError, match=message):
        service().commit_response(
            artifact("stmt-1", lifecycle=lifecycle, generation=generation),
            "request-1",
        )


def test_exact_retry_replays_receipt_without_second_checkpoint() -> None:
    checkpoints = []
    command = service(checkpoints=checkpoints)
    accepted = artifact("stmt-1")

    created = command.commit_response(accepted, "request-1")
    replay = command.commit_response(accepted, "request-1")

    assert len(checkpoints) == 1
    assert created["receipt"] == replay["receipt"]
    assert replay["replayed"] is True
    assert replay["checkpoint_count"] == 0
    assert replay["durable"] is True


def test_changed_payload_retry_is_stable_conflict_without_mutation() -> None:
    command = service()
    command.commit_response(artifact("stmt-1"), "request-1")
    before = coordinated_response_state_signature(command.coordinator.snapshot())

    with pytest.raises(ConflictError, match="different mutation"):
        command.commit_response(artifact("stmt-2", request="Different request"), "request-1")

    assert coordinated_response_state_signature(command.coordinator.snapshot()) == before


def test_base_commit_names_all_scoped_canonical_and_alias_collision_owners() -> None:
    existing = ArtifactRepository(
        (
            artifact("stmt-canonical", request="Canonical owner"),
            artifact("stmt-alias", request="Another owner", aliases=("Shared alias",)),
        )
    )
    command = service(existing)
    colliding = artifact("stmt-new", request="Canonical owner", aliases=("Shared alias",))

    with pytest.raises(ConflictError) as captured:
        command.commit_response(colliding, "request-new")

    assert "stmt-alias" in str(captured.value)
    assert "stmt-canonical" in str(captured.value)
    assert command.coordinator.next_receipt_sequence == 1


def test_same_retrieval_representation_in_another_scope_is_not_a_collision() -> None:
    existing = ArtifactRepository((artifact("stmt-a", namespace="tenant-a"),))
    command = service(existing)

    result = command.commit_response(artifact("stmt-b", namespace="tenant-b"), "request-b")

    assert result["receipt"]["result_code"] == MutationResultCode.CREATED
    assert set(command.coordinator.snapshot()["repository"]["artifacts"]) == {"stmt-a", "stmt-b"}


def test_dynamic_admission_evicts_and_receipt_names_both_generation_effects() -> None:
    repository = ArtifactRepository((artifact("stmt-old", request="Old", namespace="tenant-old"),))
    command = service(repository, capacity=1)

    result = command.commit_response(artifact("stmt-new", request="New", namespace="tenant-new"), "request-new")

    assert result["receipt"]["result_code"] == MutationResultCode.CREATED_WITH_EVICTION
    assert object_mapping(mutation_receipt_to_dict(result["receipt"])["result"])["evicted_statement_ids"] == ["stmt-old"]
    assert [
        (change["statement_id"], change["before_generation"], change["after_generation"])
        for change in result["receipt"]["affected_generations"]
    ] == [
        ("stmt-new", 0, 1),
        ("stmt-old", 1, 0),
    ]
    state = command.coordinator.snapshot()
    assert set(state["repository"]["artifacts"]) == {"stmt-new"}
    assert state["namespace_epochs"]["epochs"] == {"tenant-new": 1, "tenant-old": 1}


def test_capacity_rejection_records_replayable_result_without_repository_or_epoch_change() -> None:
    protected = artifact_statistics(1, 1, "2026-08-12T17:00:00Z", True)
    repository = ArtifactRepository((artifact("stmt-protected", request="Protected", statistics=protected),))
    command = service(repository, capacity=1, protected_rate=1.0)
    before_repository = repository.snapshot()

    result = command.commit_response(artifact("stmt-new", request="New"), "request-new")
    replay = command.commit_response(artifact("stmt-new", request="New"), "request-new")

    assert result["receipt"]["result_code"] == MutationResultCode.REJECTED_CAPACITY
    assert result["receipt"]["affected_generations"] == ()
    assert replay["replayed"] is True
    assert repository.snapshot() == before_repository
    assert command.coordinator.snapshot()["namespace_epochs"]["epochs"] == {}


def test_concurrent_exact_retry_has_one_checkpoint_and_one_replay() -> None:
    checkpoints = []
    command = service(checkpoints=checkpoints)
    accepted = artifact("stmt-1")
    barrier = threading.Barrier(3)
    results = []

    def commit() -> None:
        barrier.wait()
        results.append(command.commit_response(accepted, "request-1"))

    threads = [threading.Thread(target=commit) for _ in range(2)]
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join()

    assert len(checkpoints) == 1
    assert len(results) == 2
    assert sum(result["replayed"] for result in results) == 1
    assert (
        command.coordinator.receipt_lookup(
            "request-1",
            results[0]["receipt"]["operation"],
            results[0]["receipt"]["payload_signature"],
        )["outcome"]
        == ReceiptLookupOutcome.REPLAY
    )


def test_configured_candidate_checkpoint_restarts_with_artifact_epoch_and_receipt(tmp_path) -> None:
    engine = Engram()
    store = tmp_path / "response-candidate.json"
    coordinator = AtomicMutationCoordinator(
        engine.response_repository,
        engine.namespace_epochs,
        engine.mutation_receipts,
        checkpoint_configured=True,
        checkpoint=lambda state: persistence.save_response_state(engine, state, store),
        recovery_loader=lambda: persistence.load_coordinated_response_state(store, config=engine.config),
    )
    command = AcceptedResponseService(
        coordinator,
        tier_admission_policy(10, EvictionPolicy.FIFO),
        clock=lambda: "2026-08-12T18:00:00Z",
    )
    accepted = artifact("stmt-1")

    result = command.commit_response(accepted, "request-1")
    loaded = persistence.load_engram(store, config=engine.config)
    durable = persistence.coordinated_response_state(loaded)

    assert result["checkpoint_count"] == 1
    assert coordinated_response_state_durable_signature(durable) == coordinated_response_state_durable_signature(
        coordinator.snapshot()
    )
    assert loaded.response_repository.get_artifact("stmt-1")["response"] == accepted["response"]
    assert loaded.namespace_epochs.get("tenant-a")["knowledge_epoch"] == 1
    assert (
        loaded.mutation_receipts.lookup(
            "request-1",
            result["receipt"]["operation"],
            result["receipt"]["payload_signature"],
        )["outcome"]
        == ReceiptLookupOutcome.REPLAY
    )


def test_indeterminate_real_checkpoint_recovers_durable_candidate_by_authority_signature(tmp_path) -> None:
    engine = Engram()
    store = tmp_path / "indeterminate-response.json"

    def checkpoint(state) -> None:
        persistence.save_response_state(engine, state, store)
        raise CheckpointFailureError(CheckpointFailureKind.INDETERMINATE, "write committed but acknowledgement failed")

    coordinator = AtomicMutationCoordinator(
        engine.response_repository,
        engine.namespace_epochs,
        engine.mutation_receipts,
        checkpoint_configured=True,
        checkpoint=checkpoint,
        recovery_loader=lambda: persistence.load_coordinated_response_state(store, config=engine.config),
    )
    command = AcceptedResponseService(
        coordinator,
        tier_admission_policy(10, EvictionPolicy.FIFO),
        clock=lambda: "2026-08-12T18:00:00Z",
    )

    result = command.commit_response(artifact("stmt-1"), "request-1")

    assert result["checkpoint_count"] == 1
    assert result["recovered"] is True
    assert set(coordinator.snapshot()["repository"]["artifacts"]) == {"stmt-1"}


@pytest.mark.parametrize(
    ("method_name", "reason", "expected_lifecycle", "result_code"),
    [
        (
            "invalidate_response",
            LifecycleMutationReason.SOURCE_RETRACTED,
            LifecycleState.INVALIDATED,
            MutationResultCode.INVALIDATED,
        ),
        (
            "retire_response",
            LifecycleMutationReason.ADMINISTRATIVE,
            LifecycleState.RETIRED,
            MutationResultCode.RETIRED,
        ),
    ],
)
def test_audited_lifecycle_transition_is_atomic_replayable_and_preserves_response(
    method_name: str,
    reason: LifecycleMutationReason,
    expected_lifecycle: LifecycleState,
    result_code: MutationResultCode,
) -> None:
    command = service()
    accepted = artifact("stmt-1")
    command.commit_response(accepted, "request-create")
    transition = getattr(command, method_name)

    result = transition("stmt-1", 1, reason, "operator-a", "request-transition", "reviewed evidence")
    replay = transition("stmt-1", 1, reason, "operator-a", "request-transition", "reviewed evidence")

    transitioned = command.coordinator.snapshot()["repository"]["artifacts"]["stmt-1"]
    assert result["receipt"]["result_code"] == result_code
    assert result["receipt"]["affected_generations"][0]["statement_id"] == "stmt-1"
    assert result["receipt"]["affected_generations"][0]["before_generation"] == 1
    assert result["receipt"]["affected_generations"][0]["after_generation"] == 2
    assert replay["receipt"] == result["receipt"]
    assert replay["replayed"] is True
    assert transitioned["generation"] == 2
    assert transitioned["lifecycle"] == expected_lifecycle
    assert transitioned["response"] == accepted["response"]
    assert transitioned["metadata"]["lifecycle_audit"] == {
        "operation": result["receipt"]["operation"].value,
        "reason": reason.value,
        "caller_id": "operator-a",
        "request_id": "request-transition",
        "occurred_at": "2026-08-12T18:00:00Z",
        "detail": "reviewed evidence",
    }
    state = command.coordinator.snapshot()
    repository = state["repository"]
    assert repository["index_state"]["projections"]["stmt-1"]["direct_answer_eligible"] is False
    assert repository["index_state"]["projections"]["stmt-1"]["exclusion_reason"] == (
        f"lifecycle_{expected_lifecycle.value.lower()}"
    )
    assert object_mapping(state["namespace_epochs"]["epochs"])["tenant-a"] == 2


def test_lifecycle_transition_requires_typed_reason_caller_and_expected_generation() -> None:
    command = service()
    command.commit_response(artifact("stmt-1"), "request-create")
    before = coordinated_response_state_signature(command.coordinator.snapshot())

    with pytest.raises(InvalidRequestError, match="LifecycleMutationReason"):
        command.invalidate_response("stmt-1", 1, cast(Any, "SOURCE_RETRACTED"), "operator-a", "request-invalid")
    with pytest.raises(InvalidRequestError, match="caller_id"):
        command.invalidate_response("stmt-1", 1, LifecycleMutationReason.SOURCE_RETRACTED, "", "request-invalid")
    with pytest.raises(ConflictError, match="generation conflict"):
        command.invalidate_response("stmt-1", 2, LifecycleMutationReason.SOURCE_RETRACTED, "operator-a", "request-stale")

    assert coordinated_response_state_signature(command.coordinator.snapshot()) == before


def test_changed_lifecycle_retry_conflicts_and_terminal_artifact_cannot_transition_again() -> None:
    command = service()
    command.commit_response(artifact("stmt-1"), "request-create")
    command.invalidate_response(
        "stmt-1",
        1,
        LifecycleMutationReason.SOURCE_RETRACTED,
        "operator-a",
        "request-transition",
    )

    with pytest.raises(ConflictError, match="different mutation"):
        command.invalidate_response(
            "stmt-1",
            1,
            LifecycleMutationReason.POLICY,
            "operator-a",
            "request-transition",
        )
    with pytest.raises(LifecycleError, match="terminal_state"):
        command.retire_response(
            "stmt-1",
            2,
            LifecycleMutationReason.ADMINISTRATIVE,
            "operator-a",
            "request-retire",
        )


def test_competing_invalidation_and_retirement_have_one_generation_winner() -> None:
    command = service()
    command.commit_response(artifact("stmt-1"), "request-create")
    barrier = threading.Barrier(3)
    successes = []
    conflicts = []

    def invalidate() -> None:
        barrier.wait()
        try:
            successes.append(
                command.invalidate_response(
                    "stmt-1",
                    1,
                    LifecycleMutationReason.SOURCE_RETRACTED,
                    "operator-a",
                    "request-invalidate",
                )
            )
        except ConflictError as error:
            conflicts.append(error)

    def retire() -> None:
        barrier.wait()
        try:
            successes.append(
                command.retire_response(
                    "stmt-1",
                    1,
                    LifecycleMutationReason.ADMINISTRATIVE,
                    "operator-b",
                    "request-retire",
                )
            )
        except ConflictError as error:
            conflicts.append(error)

    threads = [threading.Thread(target=invalidate), threading.Thread(target=retire)]
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join()

    assert len(successes) == 1
    assert len(conflicts) == 1
    assert command.coordinator.snapshot()["repository"]["artifacts"]["stmt-1"]["generation"] == 2


def test_audited_lifecycle_checkpoint_restores_terminal_artifact_and_receipt(tmp_path) -> None:
    engine = Engram()
    store = tmp_path / "lifecycle-response.json"
    coordinator = AtomicMutationCoordinator(
        engine.response_repository,
        engine.namespace_epochs,
        engine.mutation_receipts,
        checkpoint_configured=True,
        checkpoint=lambda state: persistence.save_response_state(engine, state, store),
        recovery_loader=lambda: persistence.load_coordinated_response_state(store, config=engine.config),
    )
    command = AcceptedResponseService(
        coordinator,
        tier_admission_policy(10, EvictionPolicy.FIFO),
        clock=lambda: "2026-08-12T18:00:00Z",
    )
    command.commit_response(artifact("stmt-1", tier=Tier.STATIC), "request-create")

    result = command.retire_response(
        "stmt-1",
        1,
        LifecycleMutationReason.ADMINISTRATIVE,
        "operator-a",
        "request-retire",
    )
    loaded = persistence.load_engram(store, config=engine.config)

    restored = loaded.response_repository.get_artifact("stmt-1")
    assert restored["lifecycle"] == LifecycleState.RETIRED
    assert restored["generation"] == 2
    assert object_mapping(restored["metadata"]["lifecycle_audit"])["caller_id"] == "operator-a"
    assert (
        loaded.mutation_receipts.lookup(
            "request-retire",
            result["receipt"]["operation"],
            result["receipt"]["payload_signature"],
        )["outcome"]
        == ReceiptLookupOutcome.REPLAY
    )


def test_explicit_supersession_links_generations_and_reuses_only_expected_owned_key() -> None:
    current = artifact("stmt-old", tier=Tier.STATIC)
    command = service(ArtifactRepository((current,)))
    replacement = artifact(
        "stmt-new",
        tier=Tier.STATIC,
        response="Replacement response preserved exactly: naïve Ω.",
    )

    result = command.supersede_response(
        "stmt-old",
        1,
        replacement,
        LifecycleMutationReason.STALE,
        "operator-a",
        "request-supersede",
        "new evidence",
    )
    replay = command.supersede_response(
        "stmt-old",
        1,
        replacement,
        LifecycleMutationReason.STALE,
        "operator-a",
        "request-supersede",
        "new evidence",
    )

    state = command.coordinator.snapshot()
    repository = state["repository"]
    old = repository["artifacts"]["stmt-old"]
    new = repository["artifacts"]["stmt-new"]
    assert result["receipt"]["result_code"] == MutationResultCode.SUPERSEDED
    assert replay["receipt"] == result["receipt"]
    assert replay["replayed"] is True
    assert old["lifecycle"] == LifecycleState.SUPERSEDED
    assert old["generation"] == 2
    assert old["superseded_by"] == "stmt-new"
    assert old["response"] == current["response"]
    assert object_mapping(old["metadata"]["lifecycle_audit"])["replacement_statement_id"] == "stmt-new"
    assert new["lifecycle"] == LifecycleState.ACTIVE
    assert new["generation"] == 1
    assert new["response"] == replacement["response"]
    assert [
        (effect["statement_id"], effect["before_generation"], effect["after_generation"])
        for effect in result["receipt"]["affected_generations"]
    ] == [
        ("stmt-new", 0, 1),
        ("stmt-old", 1, 2),
    ]
    owners = next(iter(repository["index_state"]["retrieval_to_owners"].values()))
    assert {owner["statement_id"] for owner in owners} == {"stmt-old", "stmt-new"}
    assert object_mapping(state["namespace_epochs"]["epochs"])["tenant-a"] == 1


def test_supersession_rejects_other_owner_collision_and_same_statement_id() -> None:
    repository = ArtifactRepository(
        (
            artifact("stmt-old", request="Old key", tier=Tier.STATIC),
            artifact("stmt-other", request="Other key", aliases=("Owned elsewhere",), tier=Tier.STATIC),
        )
    )
    command = service(repository)

    with pytest.raises(ConflictError, match="stmt-other"):
        command.supersede_response(
            "stmt-old",
            1,
            artifact("stmt-new", request="Old key", aliases=("Owned elsewhere",), tier=Tier.STATIC),
            LifecycleMutationReason.STALE,
            "operator-a",
            "request-collision",
        )
    with pytest.raises(InvalidRequestError, match="new statement_id"):
        command.supersede_response(
            "stmt-old",
            1,
            artifact("stmt-old", request="Old key", tier=Tier.STATIC),
            LifecycleMutationReason.STALE,
            "operator-a",
            "request-same-id",
        )

    assert command.coordinator.next_receipt_sequence == 1


def test_dynamic_supersession_rejects_capacity_when_lineage_would_be_evicted() -> None:
    current = artifact("stmt-old", tier=Tier.DYNAMIC)
    command = service(ArtifactRepository((current,)), capacity=1)
    replacement = artifact("stmt-new", tier=Tier.DYNAMIC)

    result = command.supersede_response(
        "stmt-old",
        1,
        replacement,
        LifecycleMutationReason.STALE,
        "operator-a",
        "request-supersede",
    )
    replay = command.supersede_response(
        "stmt-old",
        1,
        replacement,
        LifecycleMutationReason.STALE,
        "operator-a",
        "request-supersede",
    )

    assert result["receipt"]["result_code"] == MutationResultCode.REJECTED_CAPACITY
    assert result["receipt"]["affected_generations"] == ()
    assert replay["replayed"] is True
    assert set(command.coordinator.snapshot()["repository"]["artifacts"]) == {"stmt-old"}
    assert command.coordinator.snapshot()["repository"]["artifacts"]["stmt-old"]["lifecycle"] == LifecycleState.ACTIVE


def test_supersession_can_evict_unrelated_dynamic_while_retaining_static_lineage() -> None:
    repository = ArtifactRepository(
        (
            artifact("stmt-old", request="Old key", tier=Tier.STATIC, namespace="tenant-old"),
            artifact("stmt-victim", request="Victim key", tier=Tier.DYNAMIC, namespace="tenant-victim"),
        )
    )
    command = service(repository, capacity=1)
    replacement = artifact("stmt-new", request="Old key", tier=Tier.DYNAMIC, namespace="tenant-old")

    result = command.supersede_response(
        "stmt-old",
        1,
        replacement,
        LifecycleMutationReason.STALE,
        "operator-a",
        "request-supersede",
    )

    assert result["receipt"]["result_code"] == MutationResultCode.SUPERSEDED
    assert object_mapping(mutation_receipt_to_dict(result["receipt"])["result"])["evicted_statement_ids"] == ["stmt-victim"]
    assert set(command.coordinator.snapshot()["repository"]["artifacts"]) == {"stmt-old", "stmt-new"}
    assert command.coordinator.snapshot()["namespace_epochs"]["epochs"] == {"tenant-old": 1, "tenant-victim": 1}


def test_competing_supersessions_have_one_winner_and_stale_generation_conflict() -> None:
    command = service(ArtifactRepository((artifact("stmt-old", tier=Tier.STATIC),)))
    replacements = [
        artifact("stmt-new-a", tier=Tier.STATIC, response="Replacement A"),
        artifact("stmt-new-b", tier=Tier.STATIC, response="Replacement B"),
    ]
    barrier = threading.Barrier(3)
    successes = []
    conflicts = []

    def supersede(index: int) -> None:
        barrier.wait()
        try:
            successes.append(
                command.supersede_response(
                    "stmt-old",
                    1,
                    replacements[index],
                    LifecycleMutationReason.STALE,
                    f"operator-{index}",
                    f"request-{index}",
                )
            )
        except ConflictError as error:
            conflicts.append(error)

    threads = [threading.Thread(target=supersede, args=(index,)) for index in range(2)]
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join()

    assert len(successes) == 1
    assert len(conflicts) == 1
    state = command.coordinator.snapshot()["repository"]
    assert len(state["artifacts"]) == 2
    assert state["artifacts"]["stmt-old"]["generation"] == 2
    assert state["artifacts"]["stmt-old"]["superseded_by"] in {"stmt-new-a", "stmt-new-b"}


def test_supersession_checkpoint_restores_lineage_replacement_and_receipt(tmp_path) -> None:
    engine = Engram()
    engine.response_repository = ArtifactRepository((artifact("stmt-old", tier=Tier.STATIC),))
    coordinator = AtomicMutationCoordinator(
        engine.response_repository,
        engine.namespace_epochs,
        engine.mutation_receipts,
        checkpoint_configured=True,
        checkpoint=lambda state: persistence.save_response_state(engine, state, tmp_path / "supersession.json"),
        recovery_loader=lambda: persistence.load_coordinated_response_state(
            tmp_path / "supersession.json",
            config=engine.config,
        ),
    )
    command = AcceptedResponseService(
        coordinator,
        tier_admission_policy(10, EvictionPolicy.FIFO),
        clock=lambda: "2026-08-12T18:00:00Z",
    )
    replacement = artifact("stmt-new", tier=Tier.STATIC)

    result = command.supersede_response(
        "stmt-old",
        1,
        replacement,
        LifecycleMutationReason.STALE,
        "operator-a",
        "request-supersede",
    )
    loaded = persistence.load_engram(tmp_path / "supersession.json", config=engine.config)

    assert loaded.response_repository.get_artifact("stmt-old")["superseded_by"] == "stmt-new"
    assert loaded.response_repository.get_artifact("stmt-new")["response"] == replacement["response"]
    assert (
        loaded.mutation_receipts.lookup(
            "request-supersede",
            result["receipt"]["operation"],
            result["receipt"]["payload_signature"],
        )["outcome"]
        == ReceiptLookupOutcome.REPLAY
    )
