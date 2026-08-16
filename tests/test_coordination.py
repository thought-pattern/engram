"""Section 3 checkpoint-before-publication mutation coordination tests."""

import threading

import pytest

from engram.artifacts import ArtifactProvenance, ArtifactStatistics, CachedResponseArtifact, LifecycleState
from engram.constants import Tier
from engram.coordination import (
    AtomicMutationCoordinator,
    CheckpointFailureError,
    CheckpointFailureKind,
    CoordinatedResponseState,
    MutationCoordinationError,
    mutation_execution_result_to_dict,
    validate_mutation_execution_result,
)
from engram.eligibility import NamespaceEpochState
from engram.errors import ConflictError, InvalidRequestError
from engram.identity import ScopeKey, build_retrieval_representation, build_standalone_identity
from engram.mutations import (
    ArtifactGenerationChange,
    MutationOperation,
    MutationReceipt,
    MutationReceiptLedger,
    MutationResultCode,
    ReceiptCompletionState,
    ReceiptLookupOutcome,
    canonical_payload_signature,
)
from engram.repository import ArtifactRepository


def artifact(statement_id: str) -> CachedResponseArtifact:
    scope = ScopeKey(namespace="tenant-a")
    request = f"Question for {statement_id}?"
    return CachedResponseArtifact(
        statement_id=statement_id,
        generation=1,
        response=f"Response for {statement_id}.",
        query_identity=build_standalone_identity(request, scope),
        retrieval=build_retrieval_representation(request, (f"Alias for {statement_id}",)),
        tier=Tier.STATIC,
        lifecycle=LifecycleState.ACTIVE,
        scope=scope,
        support_claim_ids=(),
        valid_from="",
        valid_from_available=False,
        valid_until="",
        valid_until_available=False,
        knowledge_epoch=0,
        knowledge_epoch_available=False,
        superseded_by="",
        provenance=ArtifactProvenance("test", "caller", "2026-08-12T16:00:00Z"),
        statistics=ArtifactStatistics(),
        metadata={},
    )


def receipt(
    request_id: str,
    statement_id: str,
    *,
    sequence: int = 1,
    affected: tuple[ArtifactGenerationChange, ...] | None = None,
    result_code: MutationResultCode = MutationResultCode.CREATED,
) -> MutationReceipt:
    effects = (ArtifactGenerationChange(statement_id, 0, 1),) if affected is None else affected
    return MutationReceipt(
        sequence=sequence,
        request_id=request_id,
        operation=MutationOperation.COMMIT_RESPONSE,
        payload_signature=canonical_payload_signature({"statement_id": statement_id}),
        result_code=result_code,
        affected_generations=effects,
        result={"statement_id": statement_id, "result_code": result_code.value},
        completion_state=ReceiptCompletionState.COMPLETED,
        created_at="2026-08-12T16:00:00Z",
    )


def candidate_for(coordinator: AtomicMutationCoordinator, statement_id: str, request_id: str = "request-1"):
    candidate = coordinator.repository.candidate_with_artifact(artifact(statement_id))
    mutation_receipt = receipt(request_id, statement_id, sequence=coordinator.next_receipt_sequence)
    return coordinator.build_candidate(candidate, ("tenant-a",), mutation_receipt)


def test_candidate_build_is_off_live_and_in_memory_publication_is_atomic() -> None:
    repository = ArtifactRepository()
    epochs = NamespaceEpochState()
    ledger = MutationReceiptLedger()
    coordinator = AtomicMutationCoordinator(repository, epochs, ledger)
    candidate = candidate_for(coordinator, "stmt-1")

    assert repository.snapshot().artifacts == {}
    assert epochs.get("tenant-a").knowledge_epoch_available is False
    assert ledger.next_sequence == 1

    result = coordinator.execute(candidate)

    assert type(result) is dict
    assert result["checkpoint_count"] == 0
    assert result["durable"] is False
    assert result["published"] is True
    assert result["recovered"] is False
    assert set(repository.snapshot().artifacts) == {"stmt-1"}
    assert epochs.get("tenant-a").knowledge_epoch == 1
    assert (
        ledger.lookup(
            candidate.receipt.request_id,
            candidate.receipt.operation,
            candidate.receipt.payload_signature,
        ).outcome
        == ReceiptLookupOutcome.REPLAY
    )
    assert coordinator.snapshot().signature() == candidate.after.signature()


def test_execution_result_revalidates_plain_dictionary_state() -> None:
    coordinator = AtomicMutationCoordinator(ArtifactRepository(), NamespaceEpochState(), MutationReceiptLedger())
    result = coordinator.execute(candidate_for(coordinator, "stmt-1"))

    serialized = mutation_execution_result_to_dict(result)

    assert serialized["receipt"] == result["receipt"].to_dict()
    malformed = dict(result)
    malformed["published"] = False
    with pytest.raises(InvalidRequestError, match="must be published"):
        validate_mutation_execution_result(malformed)
    malformed["unexpected"] = False
    with pytest.raises(InvalidRequestError, match="fields are malformed"):
        validate_mutation_execution_result(malformed)


def test_checkpoint_happens_once_before_any_live_publication() -> None:
    repository = ArtifactRepository()
    observed = []

    def checkpoint(state: CoordinatedResponseState) -> None:
        observed.append((repository.snapshot(), state))

    coordinator = AtomicMutationCoordinator(
        repository,
        NamespaceEpochState(),
        MutationReceiptLedger(),
        checkpoint_configured=True,
        checkpoint=checkpoint,
    )
    candidate = candidate_for(coordinator, "stmt-1")

    result = coordinator.execute(candidate)

    assert len(observed) == 1
    assert observed[0][0].artifacts == {}
    assert observed[0][1].signature() == candidate.after.signature()
    assert result["checkpoint_count"] == 1
    assert result["durable"] is True


def test_definite_checkpoint_failure_leaves_all_live_state_unchanged() -> None:
    attempts = []

    def checkpoint(state: CoordinatedResponseState) -> None:
        attempts.append(state)
        raise CheckpointFailureError(CheckpointFailureKind.DEFINITE, "disk rejected write")

    coordinator = AtomicMutationCoordinator(
        ArtifactRepository(),
        NamespaceEpochState(),
        MutationReceiptLedger(),
        checkpoint_configured=True,
        checkpoint=checkpoint,
    )
    candidate = candidate_for(coordinator, "stmt-1")

    with pytest.raises(MutationCoordinationError, match="definitely failed") as captured:
        coordinator.execute(candidate)

    assert len(attempts) == 1
    assert captured.value.checkpoint_count == 1
    assert captured.value.durable_candidate is False
    assert captured.value.live_state_changed is False
    assert captured.value.recovery_required is False
    assert coordinator.snapshot().signature() == candidate.before.signature()


def test_indeterminate_checkpoint_recovers_committed_candidate_without_retrying_write() -> None:
    attempts = []
    durable = []

    def checkpoint(state: CoordinatedResponseState) -> None:
        attempts.append(state)
        durable.append(state)
        raise CheckpointFailureError(CheckpointFailureKind.INDETERMINATE, "acknowledgement lost")

    coordinator = AtomicMutationCoordinator(
        ArtifactRepository(),
        NamespaceEpochState(),
        MutationReceiptLedger(),
        checkpoint_configured=True,
        checkpoint=checkpoint,
        recovery_loader=lambda: durable[0],
    )
    candidate = candidate_for(coordinator, "stmt-1")

    result = coordinator.execute(candidate)

    assert len(attempts) == 1
    assert result["checkpoint_count"] == 1
    assert result["recovered"] is True
    assert coordinator.snapshot().signature() == candidate.after.signature()


def test_indeterminate_checkpoint_that_recovered_before_state_reports_safe_failure() -> None:
    before = []

    def checkpoint(_state: CoordinatedResponseState) -> None:
        raise CheckpointFailureError(CheckpointFailureKind.INDETERMINATE, "connection reset")

    coordinator = AtomicMutationCoordinator(
        ArtifactRepository(),
        NamespaceEpochState(),
        MutationReceiptLedger(),
        checkpoint_configured=True,
        checkpoint=checkpoint,
        recovery_loader=lambda: before[0],
    )
    candidate = candidate_for(coordinator, "stmt-1")
    before.append(candidate.before)

    with pytest.raises(MutationCoordinationError, match="unchanged pre-mutation") as captured:
        coordinator.execute(candidate)

    assert captured.value.recovery_required is False
    assert captured.value.durable_candidate is False
    assert coordinator.snapshot().signature() == candidate.before.signature()


def test_indeterminate_checkpoint_divergence_requires_operator_recovery() -> None:
    recovered = []

    def checkpoint(_state: CoordinatedResponseState) -> None:
        raise CheckpointFailureError(CheckpointFailureKind.INDETERMINATE, "connection reset")

    coordinator = AtomicMutationCoordinator(
        ArtifactRepository(),
        NamespaceEpochState(),
        MutationReceiptLedger(),
        checkpoint_configured=True,
        checkpoint=checkpoint,
        recovery_loader=lambda: recovered[0],
    )
    candidate = candidate_for(coordinator, "stmt-1")
    divergent_repository = ArtifactRepository((artifact("stmt-other"),)).snapshot()
    recovered.append(
        CoordinatedResponseState(divergent_repository, candidate.before.namespace_epochs, candidate.before.mutation_receipts)
    )

    with pytest.raises(MutationCoordinationError, match="divergent durable state") as captured:
        coordinator.execute(candidate)

    assert captured.value.recovery_required is True
    assert coordinator.snapshot().signature() == candidate.before.signature()


def test_post_checkpoint_publication_fault_converges_from_durable_candidate() -> None:
    attempts = []
    hook_calls = []

    def checkpoint(state: CoordinatedResponseState) -> None:
        attempts.append(state)

    def fail_publication(state: CoordinatedResponseState) -> None:
        hook_calls.append(state)
        raise RuntimeError("simulated post-repository fault")

    coordinator = AtomicMutationCoordinator(
        ArtifactRepository(),
        NamespaceEpochState(),
        MutationReceiptLedger(),
        checkpoint_configured=True,
        checkpoint=checkpoint,
        publication_hook=fail_publication,
    )
    candidate = candidate_for(coordinator, "stmt-1")

    result = coordinator.execute(candidate)

    assert len(attempts) == 1
    assert len(hook_calls) == 1
    assert result["recovered"] is True
    assert coordinator.snapshot().signature() == candidate.after.signature()


def test_post_checkpoint_recovery_failure_reports_durable_partial_live_state(monkeypatch) -> None:
    repository = ArtifactRepository()

    def fail_publication(_state: CoordinatedResponseState) -> None:
        raise RuntimeError("publication failed")

    def fail_recovery(_state) -> None:
        raise RuntimeError("recovery failed")

    coordinator = AtomicMutationCoordinator(
        repository,
        NamespaceEpochState(),
        MutationReceiptLedger(),
        checkpoint_configured=True,
        checkpoint=lambda _state: None,
        publication_hook=fail_publication,
    )
    candidate = candidate_for(coordinator, "stmt-1")
    monkeypatch.setattr(repository, "recover_from_durable_state", fail_recovery)

    with pytest.raises(MutationCoordinationError, match="live recovery failed") as captured:
        coordinator.execute(candidate)

    assert captured.value.checkpoint_count == 1
    assert captured.value.durable_candidate is True
    assert captured.value.live_state_changed is True
    assert captured.value.recovery_required is True


def test_unchanged_repository_candidate_can_atomically_record_capacity_rejection() -> None:
    repository = ArtifactRepository()
    epochs = NamespaceEpochState()
    ledger = MutationReceiptLedger()
    coordinator = AtomicMutationCoordinator(repository, epochs, ledger)
    rejected = receipt(
        "request-rejected",
        "stmt-rejected",
        affected=(),
        result_code=MutationResultCode.REJECTED_CAPACITY,
    )
    candidate = coordinator.build_candidate(repository.snapshot(), (), rejected)

    coordinator.execute(candidate)

    assert repository.snapshot().state_generation == candidate.before.repository.state_generation
    assert epochs.get("tenant-a").knowledge_epoch_available is False
    assert ledger.next_sequence == 2


def test_candidate_rejects_receipt_effect_and_epoch_namespace_mismatch() -> None:
    coordinator = AtomicMutationCoordinator(ArtifactRepository(), NamespaceEpochState(), MutationReceiptLedger())
    repository_candidate = coordinator.repository.candidate_with_artifact(artifact("stmt-1"))

    with pytest.raises(ConflictError, match="affected generations"):
        coordinator.build_candidate(
            repository_candidate,
            ("tenant-a",),
            receipt("request-1", "stmt-1", affected=()),
        )
    with pytest.raises(ConflictError, match="epoch namespaces"):
        coordinator.build_candidate(repository_candidate, (), receipt("request-1", "stmt-1"))


def test_concurrent_candidates_have_one_checkpointed_winner_and_one_stale_conflict() -> None:
    checkpoints = []
    coordinator = AtomicMutationCoordinator(
        ArtifactRepository(),
        NamespaceEpochState(),
        MutationReceiptLedger(),
        checkpoint_configured=True,
        checkpoint=lambda state: checkpoints.append(state),
    )
    candidates = [candidate_for(coordinator, "stmt-a", "request-a"), candidate_for(coordinator, "stmt-b", "request-b")]
    barrier = threading.Barrier(3)
    successes = []
    conflicts = []

    def execute(candidate) -> None:
        barrier.wait()
        try:
            successes.append(coordinator.execute(candidate))
        except ConflictError as error:
            conflicts.append(error)

    threads = [threading.Thread(target=execute, args=(candidate,)) for candidate in candidates]
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join()

    assert len(successes) == 1
    assert len(conflicts) == 1
    assert len(checkpoints) == 1
    assert len(coordinator.snapshot().repository.artifacts) == 1
