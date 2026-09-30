"""Process-memory mutation receipt behavior tests."""

from json import loads as json_loads
from threading import Barrier as threading_Barrier, Thread as threading_Thread

from pytest import mark as pytest_mark, raises as pytest_raises

from engram.errors import ConflictError, InvalidRequestError, ResourceNotFoundError
from engram.mutations import (
    MutationOperation,
    MutationReceiptLedger,
    MutationResultCode,
    ReceiptCompletionState,
    ReceiptLookupOutcome,
    artifact_generation_change,
    artifact_generation_change_to_dict,
    canonical_payload_signature,
    mutation_receipt,
    mutation_receipt_from_json,
    mutation_receipt_to_dict,
    mutation_receipt_to_json,
    receipt_lookup_receipt,
)


def completed_receipt(
    request_id="request-1",
    sequence=1,
    payload_signature="",
    result_code=MutationResultCode.CREATED,
) -> dict:
    signature = payload_signature or canonical_payload_signature({"response": "Exact response", "tier": "DYNAMIC"})
    receipt = mutation_receipt(
        sequence=sequence,
        request_id=request_id,
        operation=MutationOperation.COMMIT_RESPONSE,
        payload_signature=signature,
        result_code=result_code,
        affected_generations=(artifact_generation_change("stmt-1", 0, 1),),
        result={"statement_id": "stmt-1", "generation": 1},
        completion_state=ReceiptCompletionState.COMPLETED,
        created_at="2026-08-12T16:00:00Z",
    )
    return receipt


def prepared_receipt(request_id="request-1", sequence=1) -> dict:
    receipt = mutation_receipt(
        sequence=sequence,
        request_id=request_id,
        operation=MutationOperation.COMMIT_RESPONSE,
        payload_signature=canonical_payload_signature({"response": "Exact response", "tier": "DYNAMIC"}),
        result_code=MutationResultCode.REJECTED_CAPACITY,
        affected_generations=(),
        result={},
        completion_state=ReceiptCompletionState.PREPARED,
        created_at="2026-08-12T16:00:00Z",
    )
    return receipt


def test_canonical_payload_signature_is_order_independent_unicode_exact_and_concrete() -> None:
    first = canonical_payload_signature({"z": ["é", True], "a": {"count": 1}})
    second = canonical_payload_signature({"a": {"count": 1}, "z": ["é", True]})

    assert first == second
    assert first.startswith("sha256:")
    assert len(first) == 71
    assert first != canonical_payload_signature({"z": ["e", True], "a": {"count": 1}})
    with pytest_raises(InvalidRequestError, match="unsupported JSON"):
        canonical_payload_signature(json_loads('{"missing": null}'))


def test_receipt_codec_round_trip_is_deterministic_and_deeply_isolated() -> None:
    source = {"statement_id": "stmt-1", "nested": {"items": [1, "é"]}}
    receipt = completed_receipt()
    receipt = mutation_receipt(
        receipt["sequence"],
        receipt["request_id"],
        receipt["operation"],
        receipt["payload_signature"],
        receipt["result_code"],
        receipt["affected_generations"],
        source,
        receipt["completion_state"],
        receipt["created_at"],
    )
    encoded = mutation_receipt_to_json(receipt)
    source.get("nested", {})["items"].append("late")
    restored = mutation_receipt_from_json(encoded)

    assert restored == receipt
    assert mutation_receipt_to_json(restored) == encoded
    assert mutation_receipt_to_dict(restored)["result"] == {"nested": {"items": [1, "é"]}, "statement_id": "stmt-1"}
    assert "\\u00e9" not in encoded
    receipt["result"]["new"] = "value"
    assert "new" not in restored["result"]


def test_exact_completed_retry_replays_original_receipt() -> None:
    ledger = MutationReceiptLedger()
    receipt = ledger.record(completed_receipt())

    lookup = ledger.lookup(receipt["request_id"], receipt["operation"], receipt["payload_signature"])

    assert type(lookup) is dict
    assert lookup["outcome"] == ReceiptLookupOutcome.REPLAY
    assert lookup["receipt_available"] is True
    assert receipt_lookup_receipt(lookup) == receipt
    assert ledger.record(receipt) == receipt


def test_changed_payload_or_operation_conflicts_without_receipt_disclosure() -> None:
    ledger = MutationReceiptLedger()
    receipt = ledger.record(completed_receipt())

    changed_payload = ledger.lookup(
        receipt["request_id"],
        receipt["operation"],
        canonical_payload_signature({"response": "Changed response"}),
    )
    changed_operation = ledger.lookup(receipt["request_id"], MutationOperation.RETIRE_RESPONSE, receipt["payload_signature"])

    assert changed_payload["outcome"] == ReceiptLookupOutcome.CONFLICT
    assert changed_operation["outcome"] == ReceiptLookupOutcome.CONFLICT
    assert changed_payload["receipt_available"] is False
    with pytest_raises(ResourceNotFoundError, match="not available"):
        receipt_lookup_receipt(changed_payload)


def test_prepared_receipt_reports_in_progress_and_can_only_advance_to_completed() -> None:
    ledger = MutationReceiptLedger()
    prepared = ledger.record(prepared_receipt())
    lookup = ledger.lookup(prepared["request_id"], prepared["operation"], prepared["payload_signature"])

    assert lookup["outcome"] == ReceiptLookupOutcome.IN_PROGRESS
    completed = completed_receipt(payload_signature=prepared["payload_signature"])
    assert ledger.record(completed) == completed
    assert ledger.lookup(completed["request_id"], completed["operation"], completed["payload_signature"])["outcome"] == (
        ReceiptLookupOutcome.REPLAY
    )

    with pytest_raises(ConflictError, match="cannot be changed"):
        ledger.record(
            completed_receipt(
                payload_signature=completed["payload_signature"],
                result_code=MutationResultCode.CREATED_WITH_EVICTION,
            )
        )


def test_new_request_requires_exact_next_sequence() -> None:
    ledger = MutationReceiptLedger()
    receipt = completed_receipt(sequence=2)
    with pytest_raises(ConflictError, match="expected 1, received 2"):
        ledger.record(receipt)
    assert ledger.lookup(receipt["request_id"], receipt["operation"], receipt["payload_signature"])["outcome"] == (
        ReceiptLookupOutcome.NEW
    )


def test_bounded_retention_creates_tombstone_and_then_expires_tombstone_horizon() -> None:
    ledger = MutationReceiptLedger(max_receipts=2, max_tombstones=1)
    receipts = [completed_receipt(f"request-{index}", index) for index in range(1, 5)]
    for receipt in receipts:
        ledger.record(receipt)

    snapshot = ledger.snapshot()
    receipts_state = snapshot["receipts"]
    tombstones_state = snapshot["tombstones"]
    assert [item["request_id"] for item in receipts_state] == ["request-3", "request-4"]
    assert [item["request_id"] for item in tombstones_state] == ["request-2"]
    assert (
        ledger.lookup("request-2", receipts[1]["operation"], receipts[1]["payload_signature"])["outcome"]
        == ReceiptLookupOutcome.EXPIRED
    )
    assert ledger.lookup("request-1", receipts[0]["operation"], receipts[0]["payload_signature"])["outcome"] == (
        ReceiptLookupOutcome.NEW
    )
    with pytest_raises(ConflictError, match="pruned"):
        ledger.record(completed_receipt("request-2", 5))


def test_concurrent_same_sequence_record_has_one_identity_winner() -> None:
    ledger = MutationReceiptLedger()
    first = completed_receipt("request-a", 1)
    second = completed_receipt("request-b", 1)
    barrier = threading_Barrier(3)
    successes = []
    conflicts = []

    def record(receipt) -> None:
        barrier.wait()
        try:
            successes.append(ledger.record(receipt))
        except ConflictError as error:
            conflicts.append(str(error))

    threads = [threading_Thread(target=record, args=(receipt,)) for receipt in (first, second)]
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join()

    assert len(successes) == 1
    assert len(conflicts) == 1
    assert ledger.next_sequence == 2


def test_affected_generations_are_concrete_unique_and_ordered() -> None:
    receipt = completed_receipt()
    assert artifact_generation_change_to_dict(receipt["affected_generations"][0]) == {
        "statement_id": "stmt-1",
        "before_generation": 0,
        "after_generation": 1,
    }
    with pytest_raises(InvalidRequestError, match="exist before or after"):
        artifact_generation_change("stmt-1", 0, 0)
    with pytest_raises(InvalidRequestError, match="unique"):
        mutation_receipt(
            receipt["sequence"],
            receipt["request_id"],
            receipt["operation"],
            receipt["payload_signature"],
            receipt["result_code"],
            (receipt["affected_generations"][0], receipt["affected_generations"][0]),
            receipt["result"],
            receipt["completion_state"],
            receipt["created_at"],
        )


@pytest_mark.parametrize(
    ("call", "message"),
    [
        (lambda: MutationReceiptLedger(max_receipts=0), "positive bounded integer"),
        (lambda: MutationReceiptLedger(receipts=[]), "tuple"),
        (
            lambda: mutation_receipt_from_json("[]"),
            "must contain an object",
        ),
        (
            lambda: completed_receipt(payload_signature="sha256:BAD"),
            "lowercase SHA-256",
        ),
        (
            lambda: canonical_payload_signature([]),
            "payload must be an object",
        ),
    ],
)
def test_receipt_boundaries_reject_wrong_or_malformed_values(call, message) -> None:
    with pytest_raises(InvalidRequestError, match=message):
        call()
