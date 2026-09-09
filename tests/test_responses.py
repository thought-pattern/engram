"""Regulator-approved response mutations in process memory."""

from pytest import mark as pytest_mark, raises as pytest_raises

from engram.artifacts import LifecycleState, artifact_statistics
from engram.constants import LifecycleMutationReason, Tier
from engram.coordination import AtomicMutationCoordinator
from engram.errors import ConflictError, InvalidRequestError
from engram.identity import build_retrieval_representation
from engram.mutations import MutationReceiptLedger, MutationResultCode
from engram.repository import ArtifactRepository, tier_admission_policy
from engram.responses import AcceptedResponseService, response_mutation_result_to_dict
from tests.support_fixtures import accepted_artifact

NOW = "2026-08-12T18:00:00Z"


def response_service(capacity: int = 10, artifacts: tuple[dict, ...] = ()) -> AcceptedResponseService:
    coordinator = AtomicMutationCoordinator(
        ArtifactRepository(artifacts),
        MutationReceiptLedger(),
    )
    result = AcceptedResponseService(
        coordinator,
        tier_admission_policy(capacity),
        clock=lambda: NOW,
    )
    return result


def test_commit_publishes_artifact_and_process_local_receipt() -> None:
    service = response_service()
    artifact = accepted_artifact(tier=Tier.DYNAMIC)

    result = service.commit_response(artifact, "commit-1")
    external = response_mutation_result_to_dict(result)

    assert result.get("replayed") is False
    assert external.get("result_code") == MutationResultCode.CREATED.value
    assert set(service.coordinator.snapshot().get("repository", {}).get("artifacts", {})) == {artifact.get("statement_id", "")}
    assert set(external) == {
        "request_id",
        "operation",
        "result_code",
        "result",
        "receipt_sequence",
        "replayed",
    }


def test_exact_retry_replays_without_a_second_mutation() -> None:
    service = response_service()
    artifact = accepted_artifact(tier=Tier.DYNAMIC)

    first = service.commit_response(artifact, "commit-1")
    replay = service.commit_response(artifact, "commit-1")

    assert replay.get("replayed") is True
    assert replay.get("receipt") == first.get("receipt")
    assert service.coordinator.next_receipt_sequence == 2


def test_request_id_reuse_with_different_payload_is_rejected() -> None:
    service = response_service()
    service.commit_response(accepted_artifact(tier=Tier.DYNAMIC), "commit-1")

    with pytest_raises(ConflictError, match="different mutation"):
        service.commit_response(
            accepted_artifact(
                statement_id="stmt-response-2",
                request="A different question?",
                tier=Tier.DYNAMIC,
            ),
            "commit-1",
        )


def test_dynamic_admission_evicts_the_least_recently_used_artifact() -> None:
    recent = accepted_artifact(
        statement_id="stmt-recent",
        request="Recent question?",
        retrieval=build_retrieval_representation("Recent question?"),
        tier=Tier.DYNAMIC,
        statistics=artifact_statistics(1, 1, "2026-08-12T17:00:00Z", True),
    )
    unused = accepted_artifact(
        statement_id="stmt-unused",
        request="Unused question?",
        retrieval=build_retrieval_representation("Unused question?"),
        tier=Tier.DYNAMIC,
        statistics=artifact_statistics(),
    )
    service = response_service(2, (recent, unused))

    result = service.commit_response(
        accepted_artifact(
            statement_id="stmt-new",
            request="New question?",
            retrieval=build_retrieval_representation("New question?"),
            tier=Tier.DYNAMIC,
        ),
        "commit-new",
    )

    assert result.get("receipt", {}).get("result_code") == MutationResultCode.CREATED_WITH_EVICTION
    artifacts = service.coordinator.snapshot().get("repository", {}).get("artifacts", {})
    assert set(artifacts) == {"stmt-recent", "stmt-new"}


def test_learn_response_accepts_regulator_answer_as_dynamic_memory() -> None:
    service = response_service()

    result = service.learn_response(
        "When is support open?",
        "Support is open from nine to five.",
        "learn-1",
        "regulator",
        "tenant-a",
        "",
        "tapestry:regulator",
        {},
    )

    statement_id = result.get("receipt", {}).get("result", {}).get("statement_id", "")
    artifact = service.coordinator.repository.get_artifact(statement_id)
    assert artifact.get("tier") == Tier.DYNAMIC
    assert artifact.get("provenance", {}).get("caller_id") == "regulator"


def test_idk_is_never_learned() -> None:
    service = response_service()

    with pytest_raises(InvalidRequestError, match="not a cacheable response"):
        service.learn_response(
            "Unknown?",
            "IDK",
            "learn-idk",
            "regulator",
            "tenant-a",
            "",
            "tapestry:regulator",
            {},
        )


def test_resolution_accounting_updates_recency_and_is_idempotent() -> None:
    artifact = accepted_artifact(tier=Tier.DYNAMIC)
    service = response_service(artifacts=(artifact,))
    statement_id = artifact.get("statement_id", "")

    first = service.finalize_resolution_accounting((statement_id,), statement_id, "account-1")
    replay = service.finalize_resolution_accounting((statement_id,), statement_id, "account-1")
    current = service.coordinator.repository.get_artifact(statement_id)

    assert first.get("replayed") is False
    assert replay.get("replayed") is True
    assert current.get("statistics", {}).get("query_count") == 1
    assert current.get("statistics", {}).get("hit_count") == 1
    assert current.get("statistics", {}).get("last_hit") == NOW


@pytest_mark.parametrize(
    ("operation", "expected"),
    [
        ("invalidate_response", LifecycleState.INVALIDATED),
        ("retire_response", LifecycleState.RETIRED),
    ],
)
def test_terminal_lifecycle_mutations_remain_auditable_in_memory(operation, expected) -> None:
    artifact = accepted_artifact(tier=Tier.DYNAMIC)
    service = response_service(artifacts=(artifact,))
    mutate = getattr(service, operation)

    mutate(
        artifact.get("statement_id", ""),
        1,
        LifecycleMutationReason.STALE,
        "regulator",
        f"{operation}-1",
        "support changed",
    )
    current = service.coordinator.repository.get_artifact(artifact.get("statement_id", ""))

    assert current.get("lifecycle") == expected
    assert current.get("generation") == 2
    assert current.get("metadata", {}).get("lifecycle_audit", {}).get("caller_id") == "regulator"


def test_supersession_is_one_atomic_lineage_change() -> None:
    original = accepted_artifact(tier=Tier.DYNAMIC)
    replacement = accepted_artifact(
        statement_id="stmt-response-2",
        request=original.get("retrieval", {}).get("canonical", ""),
        response="The corrected response.",
        tier=Tier.DYNAMIC,
    )
    service = response_service(2, (original,))

    service.supersede_response(
        original.get("statement_id", ""),
        1,
        replacement,
        LifecycleMutationReason.STALE,
        "regulator",
        "supersede-1",
        "corrected answer",
    )

    old = service.coordinator.repository.get_artifact(original.get("statement_id", ""))
    new = service.coordinator.repository.get_artifact(replacement.get("statement_id", ""))
    assert old.get("lifecycle") == LifecycleState.SUPERSEDED
    assert old.get("superseded_by") == new.get("statement_id")
    assert new.get("lifecycle") == LifecycleState.ACTIVE
