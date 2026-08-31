"""Current response persistence, strict rejection, quarantine, and rebuild tests."""

import pytest

from engram import persistence
from engram.artifacts import (
    CachedResponseArtifact,
    LifecycleState,
    artifact_provenance,
    artifact_statistics,
    cached_response_artifact,
    cached_response_artifact_to_dict,
)
from engram.constants import PERSISTENCE_VERSION, RESPONSE_STATE_SCHEMA_VERSION, ResponseQuarantineReason, Tier
from engram.core import Engram
from engram.eligibility import EligibilityContext, EpochEligibilityPolicy, EpochSource, eligibility_context
from engram.errors import InvalidRequestError
from engram.identity import (
    build_retrieval_representation,
    build_scoped_retrieval_key,
    build_standalone_identity,
    retrieval_representation_bindings,
    scope_key,
)
from engram.indexes import ExactLookupOutcome
from engram.mutations import (
    MutationOperation,
    MutationReceipt,
    MutationReceiptLedger,
    MutationResultCode,
    ReceiptCompletionState,
    ReceiptLookupOutcome,
    artifact_generation_change,
    canonical_payload_signature,
    mutation_receipt,
    receipt_lookup_receipt,
    validate_mutation_receipt,
)
from engram.persistence import response_quarantine_record, response_quarantine_record_from_dict, response_quarantine_record_to_dict
from engram.repository import ArtifactRepository

from .support_fixtures import ASSERTION_REFERENCE_A, ASSERTION_REFERENCE_B


def accepted_artifact(statement_id="stmt-artifact", response="Exact response — café.") -> CachedResponseArtifact:
    scope = scope_key(namespace="tenant-a", context_fingerprint="account:pro")
    request = "Who acquired GitHub?"
    result = cached_response_artifact(
        statement_id=statement_id,
        generation=3,
        response=response,
        query_identity=build_standalone_identity(request, scope),
        retrieval=build_retrieval_representation(request, ("GitHub acquirer",)),
        tier=Tier.STATIC,
        lifecycle=LifecycleState.ACTIVE,
        scope=scope,
        support_references=(ASSERTION_REFERENCE_B, ASSERTION_REFERENCE_A),
        valid_from="2026-08-12T15:00:00Z",
        valid_from_available=True,
        valid_until="2027-08-12T15:00:00Z",
        valid_until_available=True,
        knowledge_epoch=7,
        knowledge_epoch_available=True,
        superseded_by="",
        provenance=artifact_provenance("tapestry:released", "regulator-a", "2026-08-12T16:00:00Z"),
        statistics=artifact_statistics(4, 5, "2026-08-12T17:00:00Z", True),
        metadata={"approval": {"policy": "v1"}},
    )
    return result


def receipt() -> MutationReceipt:
    result = mutation_receipt(
        sequence=1,
        request_id="request-1",
        operation=MutationOperation.COMMIT_RESPONSE,
        payload_signature=canonical_payload_signature({"artifact": cached_response_artifact_to_dict(accepted_artifact())}),
        result_code=MutationResultCode.CREATED,
        affected_generations=(artifact_generation_change("stmt-artifact", 0, 3),),
        result={"statement_id": "stmt-artifact", "generation": 3},
        completion_state=ReceiptCompletionState.COMPLETED,
        created_at="2026-08-12T16:00:00Z",
    )
    return result


def context(namespace="tenant-a", epoch=7) -> EligibilityContext:
    result = eligibility_context(
        evaluation_time="2026-08-12T18:00:00Z",
        evaluation_time_available=True,
        namespace=namespace,
        knowledge_epoch=epoch,
        knowledge_epoch_available=True,
        artifact_repository_available=True,
        epoch_source=EpochSource.STANDALONE,
    )
    return result


def version_one_state(engram: Engram) -> dict:
    state = persistence.to_dict(engram)
    state["version"] = 1
    del state["response_state"]
    return state


def test_current_persistence_round_trip_restores_authority_epochs_receipts_and_derived_state() -> None:
    artifact = accepted_artifact()
    engram = Engram()
    engram.response_repository = ArtifactRepository((artifact,))
    engram.namespace_epochs.initialize("tenant-a", 7)
    engram.mutation_receipts.record(receipt())
    engram.response_quarantine = (response_quarantine_record("legacy-1", ResponseQuarantineReason.MISSING_IDENTITY, "no request"),)

    encoded = persistence.save_json(engram)
    restored = persistence.load_engram_json(encoded)

    assert persistence.to_dict(restored).get("version", {}) == PERSISTENCE_VERSION
    assert restored.response_repository.get_artifact(artifact["statement_id"]) == artifact
    assert restored.response_repository.check()["consistent"] is True
    assert restored.namespace_epochs.get("tenant-a")["knowledge_epoch"] == 7
    persisted_receipt = receipt()
    replay = restored.mutation_receipts.lookup(
        "request-1",
        MutationOperation.COMMIT_RESPONSE,
        persisted_receipt["payload_signature"],
    )
    assert replay["outcome"] == ReceiptLookupOutcome.REPLAY
    assert receipt_lookup_receipt(replay) == receipt()
    assert restored.response_quarantine == engram.response_quarantine
    assert restored.get_statement(artifact["statement_id"])["text"] == artifact["response"]
    assert restored.get_statement(artifact["statement_id"])["hit_count"] == artifact["statistics"]["hit_count"]

    key = build_scoped_retrieval_key(artifact["scope"], artifact["retrieval"]["canonical"])
    lookup = restored.response_repository.exact_lookup(key, context(), EpochEligibilityPolicy.REQUIRE_MATCH)
    assert lookup["lookup"]["outcome"] == ExactLookupOutcome.FOUND


def test_current_persistence_omits_derived_response_index_and_rebuilds_it_at_startup() -> None:
    artifact = accepted_artifact()
    engram = Engram()
    engram.response_repository = ArtifactRepository((artifact,))
    state = persistence.to_dict(engram)

    assert set(state["response_state"]) == set(
        {"schema_version", "artifacts", "namespace_epochs", "mutation_receipts", "quarantine"}
    )
    assert "index_state" not in state["response_state"]
    restored = persistence.load_engram_from_dict(state)
    projection = restored.response_repository.snapshot()["index_state"]["projections"][artifact["statement_id"]]
    assert projection["retrieval_keys"] == retrieval_representation_bindings(artifact["retrieval"], artifact["scope"])
    assert projection["support_references"] == artifact["support_references"]
    assert projection["direct_answer_eligible"] is False


def test_authoritative_artifact_overrides_corrupt_matching_local_view_on_load() -> None:
    artifact = accepted_artifact()
    engram = Engram()
    engram.store("Corrupt compatibility text", statement_id=artifact["statement_id"], keyword_source="wrong identity")
    engram.response_repository = ArtifactRepository((artifact,))
    state = persistence.to_dict(engram)

    restored = persistence.load_engram_from_dict(state)

    assert restored.get_statement(artifact["statement_id"])["text"] == artifact["response"]
    assert restored.get_statement(artifact["statement_id"])["keywords"] == list(artifact["query_identity"]["lexical_terms"])
    assert restored.response_repository.check()["consistent"] is True


def test_v1_persistence_is_rejected_without_interpreting_old_support() -> None:
    engram = Engram()
    engram.store(
        "Réponse exacte 👩🏽‍💻\nligne deux",
        tier=Tier.DYNAMIC,
        keyword_source="Who acquired GitHub?",
        template={
            "tapestry": {
                "request": "Who acquired GitHub?",
                "retrieval_aliases": ["GitHub acquirer"],
                "namespace": "tenant-a",
                "context_fingerprint": "account:pro",
                "support": [{"proposition_id": "proposition-1"}],
                "request_id": "legacy-request",
                "approval": "released",
            }
        },
        source_label="tapestry:actor",
    )
    source = version_one_state(engram)

    with pytest.raises(ValueError, match="Unsupported persistence version: 1"):
        persistence.load_engram_from_dict(source)


def test_current_restart_preserves_prepared_receipt_state() -> None:
    engram = Engram()
    prepared_value = dict(receipt())
    prepared_value.update(
        {
            "result_code": MutationResultCode.REJECTED_CAPACITY,
            "affected_generations": (),
            "result": {},
            "completion_state": ReceiptCompletionState.PREPARED,
        }
    )
    prepared = validate_mutation_receipt(prepared_value)
    engram.mutation_receipts = MutationReceiptLedger(receipts=(prepared,), next_sequence=2)

    restored = persistence.load_engram_json(persistence.save_json(engram))

    lookup = restored.mutation_receipts.lookup(
        prepared["request_id"],
        prepared["operation"],
        prepared["payload_signature"],
    )
    assert lookup["outcome"] == ReceiptLookupOutcome.IN_PROGRESS
    assert receipt_lookup_receipt(lookup) == prepared


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda state: state.pop("response_state"), "requires response_state"),
        (lambda state: state["response_state"].update({"extra": {}}), "invalid fields"),
        (
            lambda state: state["response_state"].update(
                {"schema_version": RESPONSE_STATE_SCHEMA_VERSION + 1}
            ),
            "unsupported response_state",
        ),
        (lambda state: state["response_state"].update({"artifacts": {}}), "must be arrays"),
    ],
)
def test_current_response_state_rejects_missing_malformed_or_unsupported_contract(mutate, message) -> None:
    state = persistence.to_dict(Engram())
    mutate(state)
    with pytest.raises(InvalidRequestError, match=message):
        persistence.load_engram_from_dict(state)


def test_quarantine_codec_is_exact_and_bounded() -> None:
    record = response_quarantine_record("stmt-1", ResponseQuarantineReason.AMBIGUOUS_IDENTITY, "collision")
    serialized = response_quarantine_record_to_dict(record)
    assert response_quarantine_record_from_dict(serialized) == record
    malformed = dict(serialized)
    malformed["extra"] = ""
    with pytest.raises(InvalidRequestError, match="exactly"):
        response_quarantine_record_from_dict(malformed)
