"""Durable idempotent mutation receipt contracts and bounded ledger."""

import hashlib
import json
import math
import threading
import unicodedata
from collections.abc import Mapping
from datetime import datetime
from types import MappingProxyType
from typing import TypedDict

from engram.constants import (
    ARTIFACT_GENERATION_CHANGE_FIELDS,
    MAX_AFFECTED_GENERATIONS,
    MAX_RECEIPT_JSON_BYTES,
    MAX_RECEIPT_STATEMENT_ID_BYTES,
    MAX_RECEIPT_TIMESTAMP_BYTES,
    MAX_RECEIPTS,
    MAX_REQUEST_ID_BYTES,
    MAX_RESULT_BYTES,
    MAX_RESULT_DEPTH,
    MAX_RESULT_ITEMS,
    MAX_RESULT_KEY_BYTES,
    MAX_RESULT_STRING_BYTES,
    MAX_SIGNATURE_INPUT_BYTES,
    MAX_TOMBSTONES,
    MUTATION_LEDGER_SCHEMA_VERSION,
    MUTATION_RECEIPT_FIELDS,
    MUTATION_RECEIPT_SCHEMA_VERSION,
    RECEIPT_LOOKUP_FIELDS,
    RECEIPT_TOMBSTONE_FIELDS,
    MutationOperation,
    MutationResultCode,
    ReceiptCompletionState,
    ReceiptLookupOutcome,
)
from engram.errors import ConflictError, InvalidRequestError, ResourceNotFoundError


def _require_text(value: object, name: str, maximum_bytes: int, *, allow_empty: bool) -> str:
    if not isinstance(value, str):
        raise InvalidRequestError(f"{name} must be a string")
    if not allow_empty and not value:
        raise InvalidRequestError(f"{name} must not be empty")
    if len(value.encode("utf-8")) > maximum_bytes:
        raise InvalidRequestError(f"{name} exceeds the UTF-8 limit of {maximum_bytes} bytes")
    if any(unicodedata.category(character) in {"Cc", "Cs"} for character in value):
        raise InvalidRequestError(f"{name} contains a control or surrogate character")
    return value


def _positive_int(value: object, name: str, maximum: int = 9_223_372_036_854_775_807) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1 or value > maximum:
        raise InvalidRequestError(f"{name} must be a positive bounded integer")
    return value


def _nonnegative_int(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise InvalidRequestError(f"{name} must be a nonnegative integer")
    return value


def _exact_mapping(value: object, name: str, keys: frozenset[str]) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise InvalidRequestError(f"{name} must be an object")
    actual = frozenset(value)
    if actual != keys:
        raise InvalidRequestError(f"{name} has invalid fields: missing={sorted(keys - actual)}, extra={sorted(actual - keys)}")
    return value


def _timestamp(value: object, name: str) -> str:
    text = _require_text(value, name, MAX_RECEIPT_TIMESTAMP_BYTES, allow_empty=False)
    if not text.endswith("Z"):
        raise InvalidRequestError(f"{name} must be a canonical RFC 3339 UTC timestamp ending in Z")
    try:
        parsed = datetime.fromisoformat(text[:-1] + "+00:00")
    except ValueError as error:
        raise InvalidRequestError(f"{name} must be a canonical RFC 3339 UTC timestamp") from error
    if parsed.isoformat().replace("+00:00", "Z") != text:
        raise InvalidRequestError(f"{name} must use the canonical RFC 3339 UTC representation")
    return text


def _freeze_json(value: object, name: str, depth: int, count: list[int]) -> object:
    if depth > MAX_RESULT_DEPTH:
        raise InvalidRequestError(f"{name} exceeds the depth limit of {MAX_RESULT_DEPTH}")
    count[0] += 1
    if count[0] > MAX_RESULT_ITEMS:
        raise InvalidRequestError(f"{name} exceeds the item limit of {MAX_RESULT_ITEMS}")
    if isinstance(value, (bool, int, str)):
        if isinstance(value, str):
            text = _require_text(value, name, MAX_RESULT_STRING_BYTES, allow_empty=True)
            return text
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise InvalidRequestError(f"{name} contains a non-finite number")
        return value
    if isinstance(value, Mapping):
        pairs = []
        for key, item in value.items():
            normalized_key = _require_text(key, f"{name} key", MAX_RESULT_KEY_BYTES, allow_empty=False)
            pairs.append((normalized_key, item))
        result = MappingProxyType(
            {key: _freeze_json(item, f"{name}.{key}", depth + 1, count) for key, item in sorted(pairs, key=lambda pair: pair[0])}
        )
        return result
    if isinstance(value, (list, tuple)):
        result = tuple(_freeze_json(item, f"{name}[{position}]", depth + 1, count) for position, item in enumerate(value))
        return result
    raise InvalidRequestError(f"{name} contains an unsupported JSON value")


def _thaw_json(value: object) -> object:
    if isinstance(value, Mapping):
        result = {key: _thaw_json(item) for key, item in value.items()}
        return result
    if isinstance(value, tuple):
        result = [_thaw_json(item) for item in value]
        return result
    return value


def _freeze_result(value: object) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise InvalidRequestError("mutation result must be an object")
    frozen = _freeze_json(value, "mutation result", 0, [0])
    if not isinstance(frozen, Mapping):
        raise InvalidRequestError("mutation result must be an object")
    encoded = _json_text(_thaw_json(frozen))
    if len(encoded.encode("utf-8")) > MAX_RESULT_BYTES:
        raise InvalidRequestError(f"mutation result exceeds the UTF-8 limit of {MAX_RESULT_BYTES} bytes")
    return frozen


def _json_text(value: object) -> str:
    text = json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return text


def canonical_payload_signature(payload: Mapping[str, object]) -> str:
    """Return the stable signature for one concrete canonical mutation payload."""

    if not isinstance(payload, Mapping):
        raise InvalidRequestError("mutation payload must be an object")
    frozen = _freeze_json(payload, "mutation payload", 0, [0])
    encoded = _json_text(_thaw_json(frozen)).encode("utf-8")
    if len(encoded) > MAX_SIGNATURE_INPUT_BYTES:
        raise InvalidRequestError(f"mutation payload exceeds the UTF-8 limit of {MAX_SIGNATURE_INPUT_BYTES} bytes")
    signature = f"sha256:{hashlib.sha256(encoded).hexdigest()}"
    return signature


def _signature(value: object) -> str:
    text = _require_text(value, "mutation payload_signature", 71, allow_empty=False)
    if len(text) != 71 or not text.startswith("sha256:") or any(character not in "0123456789abcdef" for character in text[7:]):
        raise InvalidRequestError("mutation payload_signature must be a lowercase SHA-256 signature")
    return text


ArtifactGenerationChange = TypedDict(
    "ArtifactGenerationChange",
    {
        "statement_id": str,
        "before_generation": int,
        "after_generation": int,
    },
)


def artifact_generation_change(
    statement_id: str,
    before_generation: int,
    after_generation: int,
) -> ArtifactGenerationChange:
    """Build one validated artifact generation-change dictionary."""
    normalized_statement_id = _require_text(
        statement_id,
        "affected statement_id",
        MAX_RECEIPT_STATEMENT_ID_BYTES,
        allow_empty=False,
    )
    before = _nonnegative_int(before_generation, "affected before_generation")
    after = _nonnegative_int(after_generation, "affected after_generation")
    if before == 0 and after == 0:
        raise InvalidRequestError("an affected generation must exist before or after")
    result: ArtifactGenerationChange = {
        "statement_id": normalized_statement_id,
        "before_generation": before,
        "after_generation": after,
    }
    return result


def validate_artifact_generation_change(value: object) -> ArtifactGenerationChange:
    """Validate and copy one artifact generation-change dictionary."""
    data = _exact_mapping(value, "ArtifactGenerationChange", ARTIFACT_GENERATION_CHANGE_FIELDS)
    statement_id = data.get("statement_id", ())
    before_generation = data.get("before_generation", ())
    after_generation = data.get("after_generation", ())
    if not isinstance(statement_id, str):
        raise InvalidRequestError("affected statement_id must be a string")
    if not isinstance(before_generation, int) or not isinstance(after_generation, int):
        raise InvalidRequestError("affected generations must be integers")
    result = artifact_generation_change(statement_id, before_generation, after_generation)
    return result


def artifact_generation_change_to_dict(value: object) -> dict[str, object]:
    """Return one validated serializable generation-change dictionary."""
    validated = validate_artifact_generation_change(value)
    result = dict(validated)
    return result


def artifact_generation_change_from_dict(value: object) -> ArtifactGenerationChange:
    """Decode one generation change from its exact dictionary form."""
    result = validate_artifact_generation_change(value)
    return result


def _artifact_generation_change_key(value: ArtifactGenerationChange) -> tuple[str, int, int]:
    key = (value["statement_id"], value["before_generation"], value["after_generation"])
    return key


def normalize_artifact_generation_changes(value: object) -> tuple[ArtifactGenerationChange, ...]:
    """Validate, deduplicate-check, and deterministically order generation changes."""
    if not isinstance(value, tuple):
        raise InvalidRequestError("affected_generations must be a tuple of ArtifactGenerationChange values")
    if len(value) > MAX_AFFECTED_GENERATIONS:
        raise InvalidRequestError(f"affected_generations exceed the limit of {MAX_AFFECTED_GENERATIONS}")
    validated = tuple(validate_artifact_generation_change(change) for change in value)
    keys = tuple(_artifact_generation_change_key(change) for change in validated)
    if len(set(keys)) != len(keys):
        raise InvalidRequestError("affected_generations must be unique")
    ordered = tuple(sorted(validated, key=_artifact_generation_change_key))
    return ordered


MutationReceipt = TypedDict(
    "MutationReceipt",
    {
        "sequence": int,
        "request_id": str,
        "operation": MutationOperation,
        "payload_signature": str,
        "result_code": MutationResultCode,
        "affected_generations": tuple[ArtifactGenerationChange, ...],
        "result": Mapping[str, object],
        "completion_state": ReceiptCompletionState,
        "created_at": str,
        "schema_version": int,
    },
)


def mutation_receipt(
    sequence: int,
    request_id: str,
    operation: MutationOperation,
    payload_signature: str,
    result_code: MutationResultCode,
    affected_generations: tuple[ArtifactGenerationChange, ...],
    result: Mapping[str, object],
    completion_state: ReceiptCompletionState,
    created_at: str,
    schema_version: int = MUTATION_RECEIPT_SCHEMA_VERSION,
) -> MutationReceipt:
    """Build one validated durable mutation-receipt dictionary."""
    if schema_version != MUTATION_RECEIPT_SCHEMA_VERSION:
        raise InvalidRequestError(f"unsupported mutation receipt schema_version: {schema_version}")
    normalized_sequence = _positive_int(sequence, "mutation receipt sequence")
    normalized_request_id = _require_text(request_id, "mutation request_id", MAX_REQUEST_ID_BYTES, allow_empty=False)
    if not isinstance(operation, MutationOperation):
        raise InvalidRequestError("mutation operation must be a MutationOperation")
    normalized_signature = _signature(payload_signature)
    if not isinstance(result_code, MutationResultCode):
        raise InvalidRequestError("mutation result_code must be a MutationResultCode")
    ordered_generations = normalize_artifact_generation_changes(affected_generations)
    frozen_result = _freeze_result(result)
    if not isinstance(completion_state, ReceiptCompletionState):
        raise InvalidRequestError("receipt completion_state must be a ReceiptCompletionState")
    normalized_created_at = _timestamp(created_at, "mutation receipt created_at")
    if completion_state == ReceiptCompletionState.PREPARED and (
        ordered_generations or frozen_result or result_code != MutationResultCode.REJECTED_CAPACITY
    ):
        raise InvalidRequestError("a PREPARED receipt must carry empty effects and the concrete placeholder result code")
    receipt: MutationReceipt = {
        "schema_version": schema_version,
        "sequence": normalized_sequence,
        "request_id": normalized_request_id,
        "operation": operation,
        "payload_signature": normalized_signature,
        "result_code": result_code,
        "affected_generations": ordered_generations,
        "result": frozen_result,
        "completion_state": completion_state,
        "created_at": normalized_created_at,
    }
    return receipt


def validate_mutation_receipt(value: object) -> MutationReceipt:
    """Validate and copy one internal mutation-receipt dictionary."""
    data = _exact_mapping(value, "MutationReceipt", MUTATION_RECEIPT_FIELDS)
    sequence = data.get("sequence", ())
    request_id = data.get("request_id", ())
    operation = data.get("operation", ())
    payload_signature = data.get("payload_signature", ())
    result_code = data.get("result_code", ())
    affected_generations = data.get("affected_generations", ())
    result = data.get("result", ())
    completion_state = data.get("completion_state", ())
    created_at = data.get("created_at", ())
    schema_version = data.get("schema_version", ())
    if not isinstance(sequence, int) or not isinstance(request_id, str):
        raise InvalidRequestError("mutation receipt fields are malformed")
    if not isinstance(operation, MutationOperation) or not isinstance(payload_signature, str):
        raise InvalidRequestError("mutation receipt fields are malformed")
    if not isinstance(result_code, MutationResultCode) or not isinstance(affected_generations, tuple):
        raise InvalidRequestError("mutation receipt fields are malformed")
    if not isinstance(result, Mapping) or not isinstance(completion_state, ReceiptCompletionState):
        raise InvalidRequestError("mutation receipt fields are malformed")
    if not isinstance(created_at, str) or not isinstance(schema_version, int):
        raise InvalidRequestError("mutation receipt fields are malformed")
    receipt = mutation_receipt(
        sequence=sequence,
        request_id=request_id,
        operation=operation,
        payload_signature=payload_signature,
        result_code=result_code,
        affected_generations=affected_generations,
        result=result,
        completion_state=completion_state,
        created_at=created_at,
        schema_version=schema_version,
    )
    return receipt


def mutation_receipt_to_dict(value: object) -> dict[str, object]:
    """Return the exact persistent dictionary for one mutation receipt."""
    receipt = validate_mutation_receipt(value)
    result = _trusted_mutation_receipt_to_dict(receipt)
    return result


def _trusted_mutation_receipt_to_dict(receipt: MutationReceipt) -> dict[str, object]:
    """Serialize a ledger-owned receipt without redundant semantic validation."""
    affected = [artifact_generation_change_to_dict(change) for change in receipt["affected_generations"]]
    result_value = _thaw_json(receipt["result"])
    result = {
        "schema_version": receipt["schema_version"],
        "sequence": receipt["sequence"],
        "request_id": receipt["request_id"],
        "operation": receipt["operation"].value,
        "payload_signature": receipt["payload_signature"],
        "result_code": receipt["result_code"].value,
        "affected_generations": affected,
        "result": result_value,
        "completion_state": receipt["completion_state"].value,
        "created_at": receipt["created_at"],
    }
    return result


def mutation_receipt_to_json(value: object) -> str:
    """Return the canonical JSON representation of one mutation receipt."""
    data = mutation_receipt_to_dict(value)
    text = _json_text(data)
    return text


def mutation_receipt_from_dict(value: object) -> MutationReceipt:
    """Decode one mutation receipt from its exact persistent dictionary."""
    data = _exact_mapping(value, "MutationReceipt", MUTATION_RECEIPT_FIELDS)
    try:
        operation = MutationOperation(data["operation"])
        result_code = MutationResultCode(data["result_code"])
        completion_state = ReceiptCompletionState(data["completion_state"])
    except (TypeError, ValueError) as error:
        raise InvalidRequestError("mutation receipt contains an unsupported enum value") from error
    affected = data["affected_generations"]
    if not isinstance(affected, list):
        raise InvalidRequestError("mutation receipt affected_generations must be an array")
    result = data["result"]
    if not isinstance(result, Mapping):
        raise InvalidRequestError("mutation receipt result must be an object")
    receipt = mutation_receipt(
        schema_version=_positive_int(data["schema_version"], "mutation receipt schema_version"),
        sequence=_positive_int(data["sequence"], "mutation receipt sequence"),
        request_id=_require_text(data["request_id"], "mutation request_id", MAX_REQUEST_ID_BYTES, allow_empty=False),
        operation=operation,
        payload_signature=_signature(data["payload_signature"]),
        result_code=result_code,
        affected_generations=tuple(artifact_generation_change_from_dict(change) for change in affected),
        result=result,
        completion_state=completion_state,
        created_at=_timestamp(data["created_at"], "mutation receipt created_at"),
    )
    return receipt


def mutation_receipt_from_json(value: str) -> MutationReceipt:
    """Decode one mutation receipt from canonical JSON."""
    if not isinstance(value, str):
        raise InvalidRequestError("MutationReceipt JSON must be a string")
    try:
        decoded = json.loads(value)
    except json.JSONDecodeError as error:
        raise InvalidRequestError("MutationReceipt JSON is malformed") from error
    if not isinstance(decoded, Mapping):
        raise InvalidRequestError("MutationReceipt JSON must contain an object")
    receipt = mutation_receipt_from_dict(decoded)
    return receipt


ReceiptTombstone = TypedDict(
    "ReceiptTombstone",
    {
        "sequence": int,
        "request_id": str,
        "operation": MutationOperation,
        "payload_signature": str,
    },
)


def receipt_tombstone(
    sequence: int,
    request_id: str,
    operation: MutationOperation,
    payload_signature: str,
) -> ReceiptTombstone:
    """Build one validated receipt-tombstone dictionary."""
    normalized_sequence = _positive_int(sequence, "receipt tombstone sequence")
    normalized_request_id = _require_text(request_id, "mutation request_id", MAX_REQUEST_ID_BYTES, allow_empty=False)
    if not isinstance(operation, MutationOperation):
        raise InvalidRequestError("mutation operation must be a MutationOperation")
    normalized_signature = _signature(payload_signature)
    result: ReceiptTombstone = {
        "sequence": normalized_sequence,
        "request_id": normalized_request_id,
        "operation": operation,
        "payload_signature": normalized_signature,
    }
    return result


def validate_receipt_tombstone(value: object) -> ReceiptTombstone:
    """Validate and copy one receipt-tombstone dictionary."""
    data = _exact_mapping(value, "ReceiptTombstone", RECEIPT_TOMBSTONE_FIELDS)
    sequence = data.get("sequence", ())
    request_id = data.get("request_id", ())
    operation = data.get("operation", ())
    payload_signature = data.get("payload_signature", ())
    if not isinstance(sequence, int) or not isinstance(request_id, str):
        raise InvalidRequestError("receipt tombstone fields are malformed")
    if not isinstance(operation, MutationOperation) or not isinstance(payload_signature, str):
        raise InvalidRequestError("receipt tombstone fields are malformed")
    result = receipt_tombstone(sequence, request_id, operation, payload_signature)
    return result


def receipt_tombstone_to_dict(value: object) -> dict[str, object]:
    """Return the exact serializable form of one receipt tombstone."""
    validated = validate_receipt_tombstone(value)
    result = {
        "sequence": validated["sequence"],
        "request_id": validated["request_id"],
        "operation": validated["operation"].value,
        "payload_signature": validated["payload_signature"],
    }
    return result


def receipt_tombstone_from_dict(value: object) -> ReceiptTombstone:
    """Decode one receipt tombstone from its exact wire dictionary."""
    data = _exact_mapping(value, "ReceiptTombstone", RECEIPT_TOMBSTONE_FIELDS)
    try:
        operation = MutationOperation(data["operation"])
    except (TypeError, ValueError) as error:
        raise InvalidRequestError("receipt tombstone contains an unsupported operation") from error
    result = receipt_tombstone(
        sequence=_positive_int(data["sequence"], "receipt tombstone sequence"),
        request_id=_require_text(data["request_id"], "mutation request_id", MAX_REQUEST_ID_BYTES, allow_empty=False),
        operation=operation,
        payload_signature=_signature(data["payload_signature"]),
    )
    return result


ReceiptLookup = TypedDict(
    "ReceiptLookup",
    {
        "outcome": ReceiptLookupOutcome,
        "request_id": str,
        "receipt_json": str,
        "receipt_available": bool,
    },
)


def receipt_lookup(
    outcome: ReceiptLookupOutcome,
    request_id: str,
    receipt_json: str,
    receipt_available: bool,
) -> ReceiptLookup:
    """Build one validated concrete receipt-lookup dictionary."""
    if not isinstance(outcome, ReceiptLookupOutcome):
        raise InvalidRequestError("receipt lookup outcome must be a ReceiptLookupOutcome")
    normalized_request_id = _require_text(request_id, "mutation request_id", MAX_REQUEST_ID_BYTES, allow_empty=False)
    normalized_receipt_json = _require_text(receipt_json, "receipt_json", MAX_RECEIPT_JSON_BYTES, allow_empty=True)
    if not isinstance(receipt_available, bool):
        raise InvalidRequestError("receipt_available must be a boolean")
    if receipt_available != bool(normalized_receipt_json):
        raise InvalidRequestError("receipt availability must agree with receipt_json")
    carries_receipt = outcome in {ReceiptLookupOutcome.REPLAY, ReceiptLookupOutcome.IN_PROGRESS}
    if carries_receipt and not receipt_available:
        raise InvalidRequestError("replay or in-progress lookup must carry a receipt")
    if not carries_receipt and receipt_available:
        raise InvalidRequestError("new, expired, or conflict lookup must not carry a receipt")
    result: ReceiptLookup = {
        "outcome": outcome,
        "request_id": normalized_request_id,
        "receipt_json": normalized_receipt_json,
        "receipt_available": receipt_available,
    }
    return result


def validate_receipt_lookup(value: object) -> ReceiptLookup:
    """Validate and copy one receipt-lookup dictionary."""
    data = _exact_mapping(value, "ReceiptLookup", RECEIPT_LOOKUP_FIELDS)
    outcome = data.get("outcome", ())
    request_id = data.get("request_id", ())
    receipt_json = data.get("receipt_json", ())
    receipt_available = data.get("receipt_available", ())
    if not isinstance(outcome, ReceiptLookupOutcome):
        raise InvalidRequestError("receipt lookup outcome must be a ReceiptLookupOutcome")
    if not isinstance(request_id, str) or not isinstance(receipt_json, str) or not isinstance(receipt_available, bool):
        raise InvalidRequestError("receipt lookup fields are malformed")
    result = receipt_lookup(outcome, request_id, receipt_json, receipt_available)
    return result


def receipt_lookup_receipt(value: object) -> MutationReceipt:
    """Decode the available receipt carried by one validated lookup."""
    lookup = validate_receipt_lookup(value)
    if not lookup["receipt_available"]:
        outcome = lookup["outcome"]
        raise ResourceNotFoundError(f"mutation receipt is not available for lookup outcome: {outcome.value}")
    receipt = mutation_receipt_from_json(lookup["receipt_json"])
    return receipt


class MutationReceiptLedger:
    """Thread-safe bounded live receipt and tombstone owner."""

    def __init__(
        self,
        max_receipts: int = 10_000,
        max_tombstones: int = 10_000,
        receipts: tuple[MutationReceipt, ...] = (),
        tombstones: tuple[ReceiptTombstone, ...] = (),
        next_sequence: int = 1,
    ) -> None:
        self.max_receipts = _positive_int(max_receipts, "max_receipts", MAX_RECEIPTS)
        self.max_tombstones = _positive_int(max_tombstones, "max_tombstones", MAX_TOMBSTONES)
        self._next_sequence = _positive_int(next_sequence, "next receipt sequence")
        if not isinstance(receipts, tuple):
            raise InvalidRequestError("receipts must be a tuple of MutationReceipt values")
        validated_receipts = tuple(validate_mutation_receipt(receipt) for receipt in receipts)
        if not isinstance(tombstones, tuple):
            raise InvalidRequestError("tombstones must be a tuple of ReceiptTombstone values")
        validated_tombstones = tuple(validate_receipt_tombstone(tombstone) for tombstone in tombstones)
        if len(receipts) > self.max_receipts or len(tombstones) > self.max_tombstones:
            raise InvalidRequestError("receipt ledger state exceeds its configured retention bounds")
        all_sequences = [receipt["sequence"] for receipt in validated_receipts] + [
            tombstone["sequence"] for tombstone in validated_tombstones
        ]
        if len(set(all_sequences)) != len(all_sequences):
            raise InvalidRequestError("receipt ledger sequences must be unique")
        request_ids = [receipt["request_id"] for receipt in validated_receipts] + [
            tombstone["request_id"] for tombstone in validated_tombstones
        ]
        if len(set(request_ids)) != len(request_ids):
            raise InvalidRequestError("receipt ledger request IDs must be unique")
        if all_sequences and self._next_sequence <= max(all_sequences):
            raise InvalidRequestError("next receipt sequence must exceed every retained sequence")
        self._lock = threading.RLock()
        self._receipts = {receipt["request_id"]: receipt for receipt in validated_receipts}
        self._tombstones = {tombstone["request_id"]: tombstone for tombstone in validated_tombstones}

    def _trusted_clone(self) -> "MutationReceiptLedger":
        """Clone indexes while sharing immutable receipt values."""
        with self._lock:
            result = object.__new__(MutationReceiptLedger)
            result.max_receipts = self.max_receipts
            result.max_tombstones = self.max_tombstones
            result._next_sequence = self._next_sequence
            result._lock = threading.RLock()
            result._receipts = dict(self._receipts)
            result._tombstones = dict(self._tombstones)
            return result

    @property
    def next_sequence(self) -> int:
        with self._lock:
            sequence = self._next_sequence
            return sequence

    def lookup(self, request_id: str, operation: MutationOperation, payload_signature: str) -> ReceiptLookup:
        normalized_id = _require_text(request_id, "mutation request_id", MAX_REQUEST_ID_BYTES, allow_empty=False)
        if not isinstance(operation, MutationOperation):
            raise InvalidRequestError("mutation operation must be a MutationOperation")
        normalized_signature = _signature(payload_signature)
        with self._lock:
            if normalized_id in self._receipts:
                receipt = self._receipts[normalized_id]
                if receipt["operation"] != operation or receipt["payload_signature"] != normalized_signature:
                    result = receipt_lookup(ReceiptLookupOutcome.CONFLICT, normalized_id, "", False)
                    return result
                outcome = (
                    ReceiptLookupOutcome.REPLAY
                    if receipt["completion_state"] == ReceiptCompletionState.COMPLETED
                    else ReceiptLookupOutcome.IN_PROGRESS
                )
                receipt_json = mutation_receipt_to_json(receipt)
                result = receipt_lookup(outcome, normalized_id, receipt_json, True)
                return result
            if normalized_id in self._tombstones:
                tombstone = self._tombstones[normalized_id]
                outcome = (
                    ReceiptLookupOutcome.EXPIRED
                    if tombstone["operation"] == operation and tombstone["payload_signature"] == normalized_signature
                    else ReceiptLookupOutcome.CONFLICT
                )
                result = receipt_lookup(outcome, normalized_id, "", False)
                return result
            result = receipt_lookup(ReceiptLookupOutcome.NEW, normalized_id, "", False)
            return result

    def record(self, receipt: MutationReceipt) -> MutationReceipt:
        validated_receipt = validate_mutation_receipt(receipt)
        request_id = validated_receipt["request_id"]
        operation = validated_receipt["operation"]
        payload_signature = validated_receipt["payload_signature"]
        with self._lock:
            lookup = self.lookup(request_id, operation, payload_signature)
            if lookup["outcome"] == ReceiptLookupOutcome.CONFLICT:
                raise ConflictError(f"request_id is already associated with a different mutation: {request_id}")
            if lookup["outcome"] == ReceiptLookupOutcome.EXPIRED:
                raise ConflictError(f"request_id result was pruned and cannot be replayed safely: {request_id}")
            if lookup["outcome"] == ReceiptLookupOutcome.REPLAY:
                existing = receipt_lookup_receipt(lookup)
                if existing != validated_receipt:
                    raise ConflictError(f"completed mutation receipt cannot be changed: {request_id}")
                return existing
            if lookup["outcome"] == ReceiptLookupOutcome.IN_PROGRESS:
                existing = receipt_lookup_receipt(lookup)
                if (
                    validated_receipt["completion_state"] != ReceiptCompletionState.COMPLETED
                    or validated_receipt["sequence"] != existing["sequence"]
                ):
                    raise ConflictError(f"prepared mutation receipt can only advance to COMPLETED: {request_id}")
                self._receipts[request_id] = validated_receipt
                return validated_receipt
            if validated_receipt["sequence"] != self._next_sequence:
                received_sequence = validated_receipt["sequence"]
                raise ConflictError(f"receipt sequence conflict: expected {self._next_sequence}, received {received_sequence}")
            self._receipts[request_id] = validated_receipt
            self._next_sequence += 1
            self._prune()
            return validated_receipt

    def _prune(self) -> None:
        while len(self._receipts) > self.max_receipts:
            oldest = min(self._receipts.values(), key=lambda receipt: receipt["sequence"])
            oldest_request_id = oldest["request_id"]
            del self._receipts[oldest_request_id]
            tombstone = receipt_tombstone(
                oldest["sequence"],
                oldest_request_id,
                oldest["operation"],
                oldest["payload_signature"],
            )
            self._tombstones[oldest_request_id] = tombstone
        while len(self._tombstones) > self.max_tombstones:
            oldest = min(self._tombstones.values(), key=lambda tombstone: tombstone["sequence"])
            del self._tombstones[oldest["request_id"]]

    def snapshot(self) -> dict[str, object]:
        with self._lock:
            result = {
                "schema_version": MUTATION_LEDGER_SCHEMA_VERSION,
                "max_receipts": self.max_receipts,
                "max_tombstones": self.max_tombstones,
                "next_sequence": self._next_sequence,
                "receipts": [
                    _trusted_mutation_receipt_to_dict(receipt)
                    for receipt in sorted(self._receipts.values(), key=lambda item: item["sequence"])
                ],
                "tombstones": [
                    receipt_tombstone_to_dict(tombstone)
                    for tombstone in sorted(self._tombstones.values(), key=lambda item: item["sequence"])
                ],
            }
            return result

    def replace_from_snapshot(self, value: Mapping[str, object]) -> None:
        """Atomically restore a validated ledger without changing owner identity."""

        replacement = mutation_receipt_ledger_from_snapshot(value)
        with self._lock:
            self.max_receipts = replacement.max_receipts
            self.max_tombstones = replacement.max_tombstones
            self._next_sequence = replacement._next_sequence
            self._receipts = dict(replacement._receipts)
            self._tombstones = dict(replacement._tombstones)


def _mutation_receipt_ledger_from_validated(
    max_receipts: int,
    max_tombstones: int,
    receipts: tuple[MutationReceipt, ...],
    tombstones: tuple[ReceiptTombstone, ...],
    next_sequence: int,
) -> MutationReceiptLedger:
    """Assemble a ledger from values parsed and validated by the snapshot decoder."""
    if len(receipts) > max_receipts or len(tombstones) > max_tombstones:
        raise InvalidRequestError("receipt ledger state exceeds its configured retention bounds")
    sequences = [receipt["sequence"] for receipt in receipts] + [value["sequence"] for value in tombstones]
    request_ids = [receipt["request_id"] for receipt in receipts] + [value["request_id"] for value in tombstones]
    if len(set(sequences)) != len(sequences):
        raise InvalidRequestError("receipt ledger sequences must be unique")
    if len(set(request_ids)) != len(request_ids):
        raise InvalidRequestError("receipt ledger request IDs must be unique")
    if sequences and next_sequence <= max(sequences):
        raise InvalidRequestError("next receipt sequence must exceed every retained sequence")
    result = object.__new__(MutationReceiptLedger)
    result.max_receipts = max_receipts
    result.max_tombstones = max_tombstones
    result._next_sequence = next_sequence
    result._lock = threading.RLock()
    result._receipts = {receipt["request_id"]: receipt for receipt in receipts}
    result._tombstones = {value["request_id"]: value for value in tombstones}
    return result


def mutation_receipt_ledger_from_snapshot(value: Mapping[str, object]) -> MutationReceiptLedger:
    """Construct a mutation receipt ledger from one exact snapshot."""
    data = _exact_mapping(
        value,
        "MutationReceiptLedger",
        frozenset(
            {
                "schema_version",
                "max_receipts",
                "max_tombstones",
                "next_sequence",
                "receipts",
                "tombstones",
            }
        ),
    )
    if data["schema_version"] != MUTATION_LEDGER_SCHEMA_VERSION:
        raise InvalidRequestError(f"unsupported mutation ledger schema_version: {data['schema_version']}")
    receipts = data["receipts"]
    tombstones = data["tombstones"]
    if not isinstance(receipts, list) or not isinstance(tombstones, list):
        raise InvalidRequestError("mutation ledger receipts and tombstones must be arrays")
    result = _mutation_receipt_ledger_from_validated(
        _positive_int(data["max_receipts"], "max_receipts", MAX_RECEIPTS),
        _positive_int(data["max_tombstones"], "max_tombstones", MAX_TOMBSTONES),
        tuple(mutation_receipt_from_dict(receipt) for receipt in receipts),
        tuple(receipt_tombstone_from_dict(tombstone) for tombstone in tombstones),
        _positive_int(data["next_sequence"], "next receipt sequence"),
    )
    return result
