"""Atomic process-memory accepted-response mutation coordination tests."""

from pytest import raises as pytest_raises

from engram.coordination import (
    AtomicMutationCoordinator,
    MutationCoordinationError,
    validate_coordinated_mutation_candidate,
    validate_mutation_execution_result,
)
from engram.errors import ConflictError, InvalidRequestError
from engram.mutations import (
    MutationOperation,
    MutationReceiptLedger,
    MutationResultCode,
    ReceiptCompletionState,
    ReceiptLookupOutcome,
    artifact_generation_change,
    canonical_payload_signature,
    mutation_receipt,
)
from engram.repository import ArtifactRepository
from tests.support_fixtures import accepted_artifact


def receipt(request_id: str, statement_id: str, sequence: int = 1) -> dict:
    result = mutation_receipt(
        sequence=sequence,
        request_id=request_id,
        operation=MutationOperation.COMMIT_RESPONSE,
        payload_signature=canonical_payload_signature({"statement_id": statement_id}),
        result_code=MutationResultCode.CREATED,
        affected_generations=(artifact_generation_change(statement_id, 0, 1),),
        result={"statement_id": statement_id, "result_code": MutationResultCode.CREATED.value},
        completion_state=ReceiptCompletionState.COMPLETED,
        created_at="2026-08-12T16:00:00Z",
    )
    return result


def candidate_for(coordinator: AtomicMutationCoordinator, statement_id: str, request_id: str = "request-1") -> dict:
    artifact = accepted_artifact(statement_id=statement_id, request=f"Question for {statement_id}?")
    repository_candidate = coordinator.repository.candidate_with_artifact(artifact)
    result = coordinator.build_candidate(
        repository_candidate,
        receipt(request_id, statement_id, coordinator.next_receipt_sequence),
    )
    return result


def test_candidate_is_off_live_state_until_atomic_publication() -> None:
    repository = ArtifactRepository()
    ledger = MutationReceiptLedger()
    coordinator = AtomicMutationCoordinator(repository, ledger)
    candidate = candidate_for(coordinator, "stmt-1")

    assert repository.snapshot().get("artifacts") == {}
    assert ledger.next_sequence == 1

    execution = coordinator.execute(candidate)

    assert execution.get("published") is True
    assert execution.get("receipt") == candidate.get("receipt")
    assert set(repository.snapshot().get("artifacts", {})) == {"stmt-1"}
    assert (
        ledger.lookup(
            candidate.get("receipt", {}).get("request_id", ""),
            candidate.get("receipt", {}).get("operation"),
            candidate.get("receipt", {}).get("payload_signature", ""),
        ).get("outcome")
        == ReceiptLookupOutcome.REPLAY
    )


def test_publication_hook_observes_one_complete_process_state() -> None:
    repository = ArtifactRepository()
    observed = []

    def observe(state: dict) -> None:
        observed.append((repository.snapshot(), state))

    coordinator = AtomicMutationCoordinator(repository, MutationReceiptLedger(), publication_hook=observe)
    candidate = candidate_for(coordinator, "stmt-1")

    coordinator.execute(candidate)

    assert len(observed) == 1
    assert observed[0][0] == candidate.get("after", {}).get("repository")
    assert observed[0][1] == candidate.get("after")


def test_failed_projection_publication_rolls_back_repository_and_receipt() -> None:
    repository = ArtifactRepository()
    ledger = MutationReceiptLedger()

    def reject(state: dict) -> None:
        if state.get("repository", {}).get("artifacts"):
            raise RuntimeError("projection rejected")

    coordinator = AtomicMutationCoordinator(repository, ledger, publication_hook=reject)
    candidate = candidate_for(coordinator, "stmt-1")

    with pytest_raises(MutationCoordinationError, match="was rolled back") as failure:
        coordinator.execute(candidate)

    assert failure.value.live_state_changed is False
    assert repository.snapshot().get("artifacts") == {}
    assert ledger.next_sequence == 1


def test_stale_candidate_is_rejected_without_changing_live_state() -> None:
    coordinator = AtomicMutationCoordinator(ArtifactRepository(), MutationReceiptLedger())
    stale = candidate_for(coordinator, "stmt-stale", "request-stale")
    coordinator.execute(candidate_for(coordinator, "stmt-current", "request-current"))

    with pytest_raises(ConflictError, match="stale"):
        coordinator.execute(stale)

    assert set(coordinator.snapshot().get("repository", {}).get("artifacts", {})) == {"stmt-current"}


def test_candidate_and_execution_contracts_reject_extra_or_unpublished_fields() -> None:
    coordinator = AtomicMutationCoordinator(ArtifactRepository(), MutationReceiptLedger())
    candidate = candidate_for(coordinator, "stmt-1")
    malformed_candidate = dict(candidate)
    malformed_candidate["unexpected"] = False
    with pytest_raises(InvalidRequestError, match="fields are malformed"):
        validate_coordinated_mutation_candidate(malformed_candidate)

    execution = coordinator.execute(candidate)
    malformed_execution = dict(execution)
    malformed_execution["published"] = False
    with pytest_raises(InvalidRequestError, match="must be published"):
        validate_mutation_execution_result(malformed_execution)
