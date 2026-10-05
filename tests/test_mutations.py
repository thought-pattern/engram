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

EXACT_PAYLOAD_SIGNATURE = canonical_payload_signature({"response": "Exact response", "tier": "DYNAMIC"})
# mutation_receipt arguments for one completed COMMIT_RESPONSE; tests override the fields they vary and the
# validator copies every value, so these read-only constants are never shared with a receipt.
COMPLETED_RECEIPT_FIELDS = {
    "sequence": 1,
    "request_id": "request-1",
    "operation": MutationOperation.COMMIT_RESPONSE,
    "payload_signature": EXACT_PAYLOAD_SIGNATURE,
    "result_code": MutationResultCode.CREATED,
    "affected_generations": (artifact_generation_change("stmt-1", 0, 1),),
    "result": {"statement_id": "stmt-1", "generation": 1},
    "completion_state": ReceiptCompletionState.COMPLETED,
    "created_at": "2026-08-12T16:00:00Z",
}
PREPARED_RECEIPT_FIELDS = {
    **COMPLETED_RECEIPT_FIELDS,
    "result_code": MutationResultCode.REJECTED_CAPACITY,
    "affected_generations": (),
    "result": {},
    "completion_state": ReceiptCompletionState.PREPARED,
}
RECEIPT_IDENTITY_FIELDS = {"request_id", "operation", "payload_signature"}


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
    source_items = [1, "é"]
    source = {"statement_id": "stmt-1", "nested": {"items": source_items}}
    receipt = mutation_receipt(**{**COMPLETED_RECEIPT_FIELDS, "result": source})
    encoded = mutation_receipt_to_json(receipt)
    source_items.append("late")
    restored = mutation_receipt_from_json(encoded)

    assert restored == receipt
    assert mutation_receipt_to_json(restored) == encoded
    assert mutation_receipt_to_dict(restored).get("result", {}) == {"nested": {"items": [1, "é"]}, "statement_id": "stmt-1"}
    assert "\\u00e9" not in encoded
    assert "result" in receipt
    assert "result" in restored
    receipt_result = receipt.get("result", {})
    receipt_result["new"] = "value"
    assert "new" not in restored.get("result", {})


def test_exact_completed_retry_replays_original_receipt() -> None:
    ledger = MutationReceiptLedger()
    receipt = ledger.record(mutation_receipt(**COMPLETED_RECEIPT_FIELDS))
    assert receipt.keys() >= RECEIPT_IDENTITY_FIELDS

    lookup = ledger.lookup(
        receipt.get("request_id", ""),
        receipt.get("operation", MutationOperation.COMMIT_RESPONSE),
        receipt.get("payload_signature", ""),
    )

    assert type(lookup) is dict
    assert lookup.get("outcome", ReceiptLookupOutcome.NEW) == ReceiptLookupOutcome.REPLAY
    assert lookup.get("receipt_available", False) is True
    assert receipt_lookup_receipt(lookup) == receipt
    assert ledger.record(receipt) == receipt


def test_changed_payload_or_operation_conflicts_without_receipt_disclosure() -> None:
    ledger = MutationReceiptLedger()
    receipt = ledger.record(mutation_receipt(**COMPLETED_RECEIPT_FIELDS))
    assert receipt.keys() >= RECEIPT_IDENTITY_FIELDS
    request_id = receipt.get("request_id", "")

    changed_payload = ledger.lookup(
        request_id,
        receipt.get("operation", MutationOperation.COMMIT_RESPONSE),
        canonical_payload_signature({"response": "Changed response"}),
    )
    changed_operation = ledger.lookup(request_id, MutationOperation.RETIRE_RESPONSE, receipt.get("payload_signature", ""))

    assert changed_payload.get("outcome", ReceiptLookupOutcome.NEW) == ReceiptLookupOutcome.CONFLICT
    assert changed_operation.get("outcome", ReceiptLookupOutcome.NEW) == ReceiptLookupOutcome.CONFLICT
    assert "receipt_available" in changed_payload
    assert changed_payload.get("receipt_available", False) is False
    with pytest_raises(ResourceNotFoundError, match="not available"):
        receipt_lookup_receipt(changed_payload)


def test_prepared_receipt_reports_in_progress_and_can_only_advance_to_completed() -> None:
    ledger = MutationReceiptLedger()
    prepared = ledger.record(mutation_receipt(**PREPARED_RECEIPT_FIELDS))
    assert prepared.keys() >= RECEIPT_IDENTITY_FIELDS
    prepared_signature = prepared.get("payload_signature", "")
    lookup = ledger.lookup(
        prepared.get("request_id", ""),
        prepared.get("operation", MutationOperation.COMMIT_RESPONSE),
        prepared_signature,
    )

    assert lookup.get("outcome", ReceiptLookupOutcome.NEW) == ReceiptLookupOutcome.IN_PROGRESS
    completed = mutation_receipt(**{**COMPLETED_RECEIPT_FIELDS, "payload_signature": prepared_signature})
    assert ledger.record(completed) == completed
    completed_signature = completed.get("payload_signature", "")
    replay = ledger.lookup(
        completed.get("request_id", ""),
        completed.get("operation", MutationOperation.COMMIT_RESPONSE),
        completed_signature,
    )
    assert replay.get("outcome", ReceiptLookupOutcome.NEW) == ReceiptLookupOutcome.REPLAY

    with pytest_raises(ConflictError, match="cannot be changed"):
        ledger.record(
            mutation_receipt(
                **{
                    **COMPLETED_RECEIPT_FIELDS,
                    "payload_signature": completed_signature,
                    "result_code": MutationResultCode.CREATED_WITH_EVICTION,
                }
            )
        )


def test_new_request_requires_exact_next_sequence() -> None:
    ledger = MutationReceiptLedger()
    receipt = mutation_receipt(**{**COMPLETED_RECEIPT_FIELDS, "sequence": 2})
    with pytest_raises(ConflictError, match="expected 1, received 2"):
        ledger.record(receipt)
    assert receipt.keys() >= RECEIPT_IDENTITY_FIELDS
    lookup = ledger.lookup(
        receipt.get("request_id", ""),
        receipt.get("operation", MutationOperation.COMMIT_RESPONSE),
        receipt.get("payload_signature", ""),
    )
    assert "outcome" in lookup
    assert lookup.get("outcome", ReceiptLookupOutcome.NEW) == ReceiptLookupOutcome.NEW


def test_bounded_retention_creates_tombstone_and_then_expires_tombstone_horizon() -> None:
    ledger = MutationReceiptLedger(max_receipts=2, max_tombstones=1)
    receipts = [
        mutation_receipt(**{**COMPLETED_RECEIPT_FIELDS, "request_id": f"request-{index}", "sequence": index})
        for index in range(1, 5)
    ]
    for receipt in receipts:
        ledger.record(receipt)

    snapshot = ledger.snapshot()
    receipts_state = snapshot.get("receipts", [])
    tombstones_state = snapshot.get("tombstones", [])
    assert [item.get("request_id", "") for item in receipts_state] == ["request-3", "request-4"]
    assert [item.get("request_id", "") for item in tombstones_state] == ["request-2"]
    assert receipts[0].keys() >= RECEIPT_IDENTITY_FIELDS
    assert receipts[1].keys() >= RECEIPT_IDENTITY_FIELDS
    expired = ledger.lookup(
        "request-2",
        receipts[1].get("operation", MutationOperation.COMMIT_RESPONSE),
        receipts[1].get("payload_signature", ""),
    )
    beyond_horizon = ledger.lookup(
        "request-1",
        receipts[0].get("operation", MutationOperation.COMMIT_RESPONSE),
        receipts[0].get("payload_signature", ""),
    )
    assert expired.get("outcome", ReceiptLookupOutcome.NEW) == ReceiptLookupOutcome.EXPIRED
    assert "outcome" in beyond_horizon
    assert beyond_horizon.get("outcome", ReceiptLookupOutcome.NEW) == ReceiptLookupOutcome.NEW
    with pytest_raises(ConflictError, match="pruned"):
        ledger.record(mutation_receipt(**{**COMPLETED_RECEIPT_FIELDS, "request_id": "request-2", "sequence": 5}))


def test_concurrent_same_sequence_record_has_one_identity_winner() -> None:
    ledger = MutationReceiptLedger()
    first = mutation_receipt(**{**COMPLETED_RECEIPT_FIELDS, "request_id": "request-a"})
    second = mutation_receipt(**{**COMPLETED_RECEIPT_FIELDS, "request_id": "request-b"})
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
    receipt = mutation_receipt(**COMPLETED_RECEIPT_FIELDS)
    generation = receipt.get("affected_generations", ())[0]
    assert artifact_generation_change_to_dict(generation) == {
        "statement_id": "stmt-1",
        "before_generation": 0,
        "after_generation": 1,
    }
    with pytest_raises(InvalidRequestError, match="exist before or after"):
        artifact_generation_change("stmt-1", 0, 0)
    with pytest_raises(InvalidRequestError, match="unique"):
        mutation_receipt(**{**COMPLETED_RECEIPT_FIELDS, "affected_generations": (generation, generation)})


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
            lambda: mutation_receipt(**{**COMPLETED_RECEIPT_FIELDS, "payload_signature": "sha256:BAD"}),
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
