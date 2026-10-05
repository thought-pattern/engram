"""Atomic process-memory accepted-response mutation coordination tests."""

from logging import ERROR

from pytest import raises as pytest_raises

from engram.coordination import (
    AtomicMutationCoordinator,
    MutationCoordinationError,
    validate_coordinated_mutation_candidate,
    validate_mutation_execution_result,
)
from engram.errors import ConflictError, InvalidRequestError
from engram.identity import extract_standalone_identity, retrieval_representation
from engram.mutations import (
    MutationOperation,
    MutationReceiptLedger,
    MutationResultCode,
    ReceiptCompletionState,
    ReceiptLookupOutcome,
    canonical_payload_signature,
)
from engram.repository import ArtifactRepository
from tests.support_fixtures import ACCEPTED_ARTIFACT_FIELDS, TENANT_SCOPE

# Fields shared by the completed COMMIT_RESPONSE receipts that create the accepted artifacts these tests commit.
COMMITTED_RECEIPT_FIELDS = {
    "operation": MutationOperation.COMMIT_RESPONSE,
    "result_code": MutationResultCode.CREATED,
    "completion_state": ReceiptCompletionState.COMPLETED,
    "created_at": "2026-08-12T16:00:00Z",
}


def test_candidate_is_off_live_state_until_atomic_publication() -> None:
    repository = ArtifactRepository()
    ledger = MutationReceiptLedger()
    coordinator = AtomicMutationCoordinator(repository, ledger)
    repository_candidate = coordinator.repository.candidate_with_artifact(
        {
            **ACCEPTED_ARTIFACT_FIELDS,
            "statement_id": "stmt-1",
            "query_identity": extract_standalone_identity("Question for stmt-1?", TENANT_SCOPE),
            "retrieval": retrieval_representation("Question for stmt-1?", ("GitHub acquirer",)),
        }
    )
    candidate = coordinator.build_candidate(
        repository_candidate,
        {
            **COMMITTED_RECEIPT_FIELDS,
            "sequence": coordinator.next_receipt_sequence,
            "request_id": "request-1",
            "payload_signature": canonical_payload_signature({"statement_id": "stmt-1"}),
            "affected_generations": ({"statement_id": "stmt-1", "before_generation": 0, "after_generation": 1},),
            "result": {"statement_id": "stmt-1", "result_code": MutationResultCode.CREATED.value},
        },
    )

    snapshot = repository.snapshot()
    assert "artifacts" in snapshot
    assert snapshot.get("artifacts", {}) == {}
    assert ledger.next_sequence == 1

    execution = coordinator.execute(candidate)

    candidate_receipt = candidate.get("receipt", {})
    assert candidate_receipt
    assert execution.get("published", False) is True
    assert execution.get("receipt", {}) == candidate_receipt
    assert set(repository.snapshot().get("artifacts", {})) == {"stmt-1"}
    assert "operation" in candidate_receipt
    lookup = ledger.lookup(
        candidate_receipt.get("request_id", ""),
        candidate_receipt.get("operation", MutationOperation.COMMIT_RESPONSE),
        candidate_receipt.get("payload_signature", ""),
    )
    assert lookup.get("outcome", ReceiptLookupOutcome.NEW) == ReceiptLookupOutcome.REPLAY


def test_publication_hook_observes_one_complete_process_state() -> None:
    repository = ArtifactRepository()
    observed = []

    def observe(state: dict) -> None:
        observed.append((repository.snapshot(), state))

    coordinator = AtomicMutationCoordinator(repository, MutationReceiptLedger(), publication_hook=observe)
    repository_candidate = coordinator.repository.candidate_with_artifact(
        {
            **ACCEPTED_ARTIFACT_FIELDS,
            "statement_id": "stmt-1",
            "query_identity": extract_standalone_identity("Question for stmt-1?", TENANT_SCOPE),
            "retrieval": retrieval_representation("Question for stmt-1?", ("GitHub acquirer",)),
        }
    )
    candidate = coordinator.build_candidate(
        repository_candidate,
        {
            **COMMITTED_RECEIPT_FIELDS,
            "sequence": coordinator.next_receipt_sequence,
            "request_id": "request-1",
            "payload_signature": canonical_payload_signature({"statement_id": "stmt-1"}),
            "affected_generations": ({"statement_id": "stmt-1", "before_generation": 0, "after_generation": 1},),
            "result": {"statement_id": "stmt-1", "result_code": MutationResultCode.CREATED.value},
        },
    )

    coordinator.execute(candidate)

    # Both observed values are populated process states, so an absent candidate field cannot match its default.
    assert len(observed) == 1
    assert observed[0][0] == candidate.get("after", {}).get("repository", {})
    assert observed[0][1] == candidate.get("after", {})


def test_failed_projection_publication_rolls_back_repository_and_receipt(caplog) -> None:
    repository = ArtifactRepository()
    ledger = MutationReceiptLedger()

    def reject(state: dict) -> None:
        if state.get("repository", {}).get("artifacts", {}):
            raise RuntimeError("projection rejected")

    coordinator = AtomicMutationCoordinator(repository, ledger, publication_hook=reject)
    repository_candidate = coordinator.repository.candidate_with_artifact(
        {
            **ACCEPTED_ARTIFACT_FIELDS,
            "statement_id": "stmt-1",
            "query_identity": extract_standalone_identity("Question for stmt-1?", TENANT_SCOPE),
            "retrieval": retrieval_representation("Question for stmt-1?", ("GitHub acquirer",)),
        }
    )
    candidate = coordinator.build_candidate(
        repository_candidate,
        {
            **COMMITTED_RECEIPT_FIELDS,
            "sequence": coordinator.next_receipt_sequence,
            "request_id": "request-1",
            "payload_signature": canonical_payload_signature({"statement_id": "stmt-1"}),
            "affected_generations": ({"statement_id": "stmt-1", "before_generation": 0, "after_generation": 1},),
            "result": {"statement_id": "stmt-1", "result_code": MutationResultCode.CREATED.value},
        },
    )

    with (
        caplog.at_level(ERROR, logger="engram.coordination"),
        pytest_raises(MutationCoordinationError, match="was rolled back") as failure,
    ):
        coordinator.execute(candidate)

    # The caller-facing message is fixed; the cause is in the log.
    assert "projection rejected" not in str(failure.value)
    assert "projection rejected" in caplog.text
    assert failure.value.live_state_changed is False
    snapshot = repository.snapshot()
    assert "artifacts" in snapshot
    assert snapshot.get("artifacts", {}) == {}
    assert ledger.next_sequence == 1


def test_stale_candidate_is_rejected_without_changing_live_state() -> None:
    coordinator = AtomicMutationCoordinator(ArtifactRepository(), MutationReceiptLedger())
    stale_repository_candidate = coordinator.repository.candidate_with_artifact(
        {
            **ACCEPTED_ARTIFACT_FIELDS,
            "statement_id": "stmt-stale",
            "query_identity": extract_standalone_identity("Question for stmt-stale?", TENANT_SCOPE),
            "retrieval": retrieval_representation("Question for stmt-stale?", ("GitHub acquirer",)),
        }
    )
    stale = coordinator.build_candidate(
        stale_repository_candidate,
        {
            **COMMITTED_RECEIPT_FIELDS,
            "sequence": coordinator.next_receipt_sequence,
            "request_id": "request-stale",
            "payload_signature": canonical_payload_signature({"statement_id": "stmt-stale"}),
            "affected_generations": ({"statement_id": "stmt-stale", "before_generation": 0, "after_generation": 1},),
            "result": {"statement_id": "stmt-stale", "result_code": MutationResultCode.CREATED.value},
        },
    )
    current_repository_candidate = coordinator.repository.candidate_with_artifact(
        {
            **ACCEPTED_ARTIFACT_FIELDS,
            "statement_id": "stmt-current",
            "query_identity": extract_standalone_identity("Question for stmt-current?", TENANT_SCOPE),
            "retrieval": retrieval_representation("Question for stmt-current?", ("GitHub acquirer",)),
        }
    )
    current = coordinator.build_candidate(
        current_repository_candidate,
        {
            **COMMITTED_RECEIPT_FIELDS,
            "sequence": coordinator.next_receipt_sequence,
            "request_id": "request-current",
            "payload_signature": canonical_payload_signature({"statement_id": "stmt-current"}),
            "affected_generations": ({"statement_id": "stmt-current", "before_generation": 0, "after_generation": 1},),
            "result": {"statement_id": "stmt-current", "result_code": MutationResultCode.CREATED.value},
        },
    )
    coordinator.execute(current)

    with pytest_raises(ConflictError, match="stale"):
        coordinator.execute(stale)

    assert set(coordinator.snapshot().get("repository", {}).get("artifacts", {})) == {"stmt-current"}


def test_candidate_and_execution_contracts_reject_extra_or_unpublished_fields() -> None:
    coordinator = AtomicMutationCoordinator(ArtifactRepository(), MutationReceiptLedger())
    repository_candidate = coordinator.repository.candidate_with_artifact(
        {
            **ACCEPTED_ARTIFACT_FIELDS,
            "statement_id": "stmt-1",
            "query_identity": extract_standalone_identity("Question for stmt-1?", TENANT_SCOPE),
            "retrieval": retrieval_representation("Question for stmt-1?", ("GitHub acquirer",)),
        }
    )
    candidate = coordinator.build_candidate(
        repository_candidate,
        {
            **COMMITTED_RECEIPT_FIELDS,
            "sequence": coordinator.next_receipt_sequence,
            "request_id": "request-1",
            "payload_signature": canonical_payload_signature({"statement_id": "stmt-1"}),
            "affected_generations": ({"statement_id": "stmt-1", "before_generation": 0, "after_generation": 1},),
            "result": {"statement_id": "stmt-1", "result_code": MutationResultCode.CREATED.value},
        },
    )
    malformed_candidate = dict(candidate)
    malformed_candidate["unexpected"] = False
    with pytest_raises(InvalidRequestError, match="fields are malformed"):
        validate_coordinated_mutation_candidate(malformed_candidate)

    execution = coordinator.execute(candidate)
    malformed_execution = dict(execution)
    malformed_execution["published"] = False
    with pytest_raises(InvalidRequestError, match="must be published"):
        validate_mutation_execution_result(malformed_execution)
