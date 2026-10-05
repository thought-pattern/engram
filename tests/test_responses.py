"""Regulator-approved response mutations in process memory."""

from pytest import mark as pytest_mark, raises as pytest_raises

from engram import repository as repository_module
from engram.artifacts import validate_cached_response_artifact
from engram.constants import INITIAL_ARTIFACT_STATISTICS, LifecycleMutationReason, LifecycleState, Tier
from engram.coordination import AtomicMutationCoordinator
from engram.errors import ConflictError, InvalidRequestError
from engram.identity import extract_standalone_identity, retrieval_representation
from engram.mutations import MutationReceiptLedger, MutationResultCode
from engram.repository import ArtifactRepository, tier_admission_policy
from engram.responses import AcceptedResponseService, response_mutation_result_to_dict
from tests.support_fixtures import ACCEPTED_ARTIFACT_FIELDS, TENANT_SCOPE

NOW = "2026-08-12T18:00:00Z"


def test_commit_publishes_artifact_and_process_local_receipt() -> None:
    service = AcceptedResponseService(
        AtomicMutationCoordinator(ArtifactRepository(()), MutationReceiptLedger()),
        tier_admission_policy(10),
        clock=lambda: NOW,
    )
    artifact = validate_cached_response_artifact({**ACCEPTED_ARTIFACT_FIELDS, "tier": Tier.DYNAMIC})

    result = service.commit_response(artifact, "commit-1")
    external = response_mutation_result_to_dict(result)

    assert "replayed" in result
    assert result.get("replayed", False) is False
    assert external.get("result_code", "") == MutationResultCode.CREATED.value
    assert set(service.coordinator.snapshot().get("repository", {}).get("artifacts", {})) == {artifact.get("statement_id", "")}
    assert set(external) == {
        "request_id",
        "operation",
        "result_code",
        "result",
        "receipt_sequence",
        "replayed",
    }


@pytest_mark.parametrize("stored", [10, 60])
def test_mutations_validate_only_the_artifacts_they_change(monkeypatch, stored) -> None:
    artifacts = tuple(
        validate_cached_response_artifact(
            {
                **ACCEPTED_ARTIFACT_FIELDS,
                "statement_id": f"stmt-{index}",
                "query_identity": extract_standalone_identity(f"Stored question number {index}?", TENANT_SCOPE),
                "retrieval": retrieval_representation(f"Stored question number {index}?"),
            }
        )
        for index in range(stored)
    )
    service = AcceptedResponseService(
        AtomicMutationCoordinator(ArtifactRepository(artifacts), MutationReceiptLedger()),
        tier_admission_policy(stored + 10),
        clock=lambda: NOW,
    )
    original = repository_module.validate_cached_response_artifact
    calls = []

    def counting(value):
        calls.append(value)
        result = original(value)
        return result

    monkeypatch.setattr(repository_module, "validate_cached_response_artifact", counting)

    new_artifact = validate_cached_response_artifact(
        {
            **ACCEPTED_ARTIFACT_FIELDS,
            "statement_id": "stmt-new",
            "query_identity": extract_standalone_identity("A brand new question?", TENANT_SCOPE),
            "retrieval": retrieval_representation("A brand new question?"),
        }
    )
    service.commit_response(new_artifact, "commit-new")
    service.record_response_queries(("stmt-0", "stmt-1"), "query-two")

    # Three changed artifacts, each validated when built, planned, and executed.
    # The count does not grow with the number stored.
    stored_ids = {stored_artifact.get("statement_id", "") for stored_artifact in artifacts}
    assert len(calls) <= 9
    assert set(service.coordinator.snapshot().get("repository", {}).get("artifacts", {})) == {*stored_ids, "stmt-new"}


def test_exact_retry_replays_without_a_second_mutation() -> None:
    service = AcceptedResponseService(
        AtomicMutationCoordinator(ArtifactRepository(()), MutationReceiptLedger()),
        tier_admission_policy(10),
        clock=lambda: NOW,
    )
    artifact = validate_cached_response_artifact({**ACCEPTED_ARTIFACT_FIELDS, "tier": Tier.DYNAMIC})

    first = service.commit_response(artifact, "commit-1")
    replay = service.commit_response(artifact, "commit-1")

    assert replay.get("replayed", False) is True
    assert first.get("receipt", {})
    assert replay.get("receipt", {}) == first.get("receipt", {})
    assert service.coordinator.next_receipt_sequence == 2


def test_request_id_reuse_with_different_payload_is_rejected() -> None:
    service = AcceptedResponseService(
        AtomicMutationCoordinator(ArtifactRepository(()), MutationReceiptLedger()),
        tier_admission_policy(10),
        clock=lambda: NOW,
    )
    service.commit_response(validate_cached_response_artifact({**ACCEPTED_ARTIFACT_FIELDS, "tier": Tier.DYNAMIC}), "commit-1")

    with pytest_raises(ConflictError, match="different mutation"):
        service.commit_response(
            validate_cached_response_artifact(
                {
                    **ACCEPTED_ARTIFACT_FIELDS,
                    "statement_id": "stmt-response-2",
                    "query_identity": extract_standalone_identity("A different question?", TENANT_SCOPE),
                    "retrieval": retrieval_representation("A different question?", ("GitHub acquirer",)),
                    "tier": Tier.DYNAMIC,
                }
            ),
            "commit-1",
        )


def test_dynamic_admission_evicts_the_least_recently_used_artifact() -> None:
    recent = validate_cached_response_artifact(
        {
            **ACCEPTED_ARTIFACT_FIELDS,
            "statement_id": "stmt-recent",
            "query_identity": extract_standalone_identity("Recent question?", TENANT_SCOPE),
            "retrieval": retrieval_representation("Recent question?"),
            "tier": Tier.DYNAMIC,
            "statistics": {"hit_count": 1, "query_count": 1, "last_hit": "2026-08-12T17:00:00Z", "last_hit_available": True},
        }
    )
    unused = validate_cached_response_artifact(
        {
            **ACCEPTED_ARTIFACT_FIELDS,
            "statement_id": "stmt-unused",
            "query_identity": extract_standalone_identity("Unused question?", TENANT_SCOPE),
            "retrieval": retrieval_representation("Unused question?"),
            "tier": Tier.DYNAMIC,
            "statistics": INITIAL_ARTIFACT_STATISTICS,
        }
    )
    service = AcceptedResponseService(
        AtomicMutationCoordinator(ArtifactRepository((recent, unused)), MutationReceiptLedger()),
        tier_admission_policy(2),
        clock=lambda: NOW,
    )

    result = service.commit_response(
        validate_cached_response_artifact(
            {
                **ACCEPTED_ARTIFACT_FIELDS,
                "statement_id": "stmt-new",
                "query_identity": extract_standalone_identity("New question?", TENANT_SCOPE),
                "retrieval": retrieval_representation("New question?"),
                "tier": Tier.DYNAMIC,
            }
        ),
        "commit-new",
    )

    receipt_code = result.get("receipt", {}).get("result_code", MutationResultCode.REJECTED_CAPACITY)
    assert receipt_code == MutationResultCode.CREATED_WITH_EVICTION
    artifacts = service.coordinator.snapshot().get("repository", {}).get("artifacts", {})
    assert set(artifacts) == {"stmt-recent", "stmt-new"}


def test_learn_response_accepts_regulator_answer_as_dynamic_memory() -> None:
    service = AcceptedResponseService(
        AtomicMutationCoordinator(ArtifactRepository(()), MutationReceiptLedger()),
        tier_admission_policy(10),
        clock=lambda: NOW,
    )

    result = service.learn_response(
        "When is support open?",
        "Support is open from nine to five.",
        "learn-1",
        "regulator",
        "tenant-a",
        "",
        "regulator",
        {},
    )

    statement_id = result.get("receipt", {}).get("result", {}).get("statement_id", "")
    artifact = service.coordinator.repository.get_artifact(statement_id)
    assert artifact.get("tier", Tier.STATIC) == Tier.DYNAMIC
    assert artifact.get("provenance", {}).get("caller_id", "") == "regulator"


def test_idk_is_never_learned() -> None:
    service = AcceptedResponseService(
        AtomicMutationCoordinator(ArtifactRepository(()), MutationReceiptLedger()),
        tier_admission_policy(10),
        clock=lambda: NOW,
    )

    with pytest_raises(InvalidRequestError, match="not a cacheable response"):
        service.learn_response(
            "Unknown?",
            "IDK",
            "learn-idk",
            "regulator",
            "tenant-a",
            "",
            "regulator",
            {},
        )


def test_resolution_accounting_updates_recency_and_is_idempotent() -> None:
    artifact = validate_cached_response_artifact({**ACCEPTED_ARTIFACT_FIELDS, "tier": Tier.DYNAMIC})
    service = AcceptedResponseService(
        AtomicMutationCoordinator(ArtifactRepository((artifact,)), MutationReceiptLedger()),
        tier_admission_policy(10),
        clock=lambda: NOW,
    )
    statement_id = artifact.get("statement_id", "")

    first = service.finalize_resolution_accounting((statement_id,), statement_id, "account-1")
    replay = service.finalize_resolution_accounting((statement_id,), statement_id, "account-1")
    statistics = service.coordinator.repository.get_artifact(statement_id).get("statistics", {})

    assert "replayed" in first
    assert first.get("replayed", False) is False
    assert replay.get("replayed", False) is True
    assert statistics.get("query_count", 0) == 1
    assert statistics.get("hit_count", 0) == 1
    assert statistics.get("last_hit", "") == NOW


@pytest_mark.parametrize(
    ("operation", "expected"),
    [
        ("invalidate_response", LifecycleState.INVALIDATED),
        ("retire_response", LifecycleState.RETIRED),
    ],
)
def test_terminal_lifecycle_mutations_remain_auditable_in_memory(operation, expected) -> None:
    artifact = validate_cached_response_artifact({**ACCEPTED_ARTIFACT_FIELDS, "tier": Tier.DYNAMIC})
    service = AcceptedResponseService(
        AtomicMutationCoordinator(ArtifactRepository((artifact,)), MutationReceiptLedger()),
        tier_admission_policy(10),
        clock=lambda: NOW,
    )
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

    assert "lifecycle" in current
    assert current.get("lifecycle", LifecycleState.RETIRED) == expected
    assert current.get("generation", 0) == 2
    assert current.get("metadata", {}).get("lifecycle_audit", {}).get("caller_id", "") == "regulator"


def test_supersession_is_one_atomic_lineage_change() -> None:
    original = validate_cached_response_artifact({**ACCEPTED_ARTIFACT_FIELDS, "tier": Tier.DYNAMIC})
    replacement_request = original.get("retrieval", {}).get("canonical", "")
    replacement = validate_cached_response_artifact(
        {
            **ACCEPTED_ARTIFACT_FIELDS,
            "statement_id": "stmt-response-2",
            "query_identity": extract_standalone_identity(replacement_request, TENANT_SCOPE),
            "retrieval": retrieval_representation(replacement_request, ("GitHub acquirer",)),
            "response": "The corrected response.",
            "tier": Tier.DYNAMIC,
        }
    )
    service = AcceptedResponseService(
        AtomicMutationCoordinator(ArtifactRepository((original,)), MutationReceiptLedger()),
        tier_admission_policy(2),
        clock=lambda: NOW,
    )

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
    new_id = new.get("statement_id", "")
    assert new_id
    assert old.get("lifecycle", LifecycleState.RETIRED) == LifecycleState.SUPERSEDED
    assert old.get("superseded_by", "") == new_id
    assert new.get("lifecycle", LifecycleState.RETIRED) == LifecycleState.ACTIVE
