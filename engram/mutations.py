"""Durable idempotent mutation receipt contracts and bounded ledger."""

import hashlib
import json
import math
import threading
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from types import MappingProxyType

from engram.errors import ConflictError, InvalidRequestError, ResourceNotFoundError

MUTATION_RECEIPT_SCHEMA_VERSION = 1
MUTATION_LEDGER_SCHEMA_VERSION = 1
MAX_REQUEST_ID_BYTES = 256
MAX_RECEIPT_STATEMENT_ID_BYTES = 256
MAX_RECEIPT_TIMESTAMP_BYTES = 40
MAX_RESULT_BYTES = 65_536
MAX_RECEIPT_JSON_BYTES = 1_048_576
MAX_SIGNATURE_INPUT_BYTES = 1_048_576
MAX_RESULT_DEPTH = 8
MAX_RESULT_ITEMS = 1_024
MAX_RESULT_KEY_BYTES = 256
MAX_RESULT_STRING_BYTES = 16_384
MAX_AFFECTED_GENERATIONS = 1_024
MAX_RECEIPTS = 100_000
MAX_TOMBSTONES = 100_000


class MutationOperation(StrEnum):
    """Transport-neutral accepted-response mutations."""

    COMMIT_RESPONSE = "COMMIT_RESPONSE"
    INVALIDATE_RESPONSE = "INVALIDATE_RESPONSE"
    RETIRE_RESPONSE = "RETIRE_RESPONSE"
    SUPERSEDE_RESPONSE = "SUPERSEDE_RESPONSE"
    RECORD_RESPONSE_QUERY = "RECORD_RESPONSE_QUERY"
    RECORD_RESPONSE_HIT = "RECORD_RESPONSE_HIT"
    FINALIZE_RESOLUTION_ACCOUNTING = "FINALIZE_RESOLUTION_ACCOUNTING"
    RECORD_FEEDBACK = "RECORD_FEEDBACK"


class MutationResultCode(StrEnum):
    """Stable results that can be replayed from a receipt."""

    CREATED = "CREATED"
    CREATED_WITH_EVICTION = "CREATED_WITH_EVICTION"
    INVALIDATED = "INVALIDATED"
    RETIRED = "RETIRED"
    SUPERSEDED = "SUPERSEDED"
    REJECTED_CAPACITY = "REJECTED_CAPACITY"
    QUERY_RECORDED = "QUERY_RECORDED"
    HIT_RECORDED = "HIT_RECORDED"
    RESOLUTION_ACCOUNTING_RECORDED = "RESOLUTION_ACCOUNTING_RECORDED"
    FEEDBACK_RECORDED = "FEEDBACK_RECORDED"


class ReceiptCompletionState(StrEnum):
    """Durable progress state of one mutation identity."""

    PREPARED = "PREPARED"
    COMPLETED = "COMPLETED"


class ReceiptLookupOutcome(StrEnum):
    """Complete outcomes before applying a mutation request."""

    NEW = "NEW"
    REPLAY = "REPLAY"
    IN_PROGRESS = "IN_PROGRESS"
    EXPIRED = "EXPIRED"
    CONFLICT = "CONFLICT"


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
    if isinstance(value, bool | int | str):
        if isinstance(value, str):
            return _require_text(value, name, MAX_RESULT_STRING_BYTES, allow_empty=True)
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
        return MappingProxyType(
            {key: _freeze_json(item, f"{name}.{key}", depth + 1, count) for key, item in sorted(pairs, key=lambda pair: pair[0])}
        )
    if isinstance(value, list | tuple):
        return tuple(_freeze_json(item, f"{name}[{position}]", depth + 1, count) for position, item in enumerate(value))
    raise InvalidRequestError(f"{name} contains an unsupported JSON value")


def _thaw_json(value: object) -> object:
    if isinstance(value, Mapping):
        return {key: _thaw_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw_json(item) for item in value]
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
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def canonical_payload_signature(payload: Mapping[str, object]) -> str:
    """Return the stable signature for one concrete canonical mutation payload."""

    if not isinstance(payload, Mapping):
        raise InvalidRequestError("mutation payload must be an object")
    frozen = _freeze_json(payload, "mutation payload", 0, [0])
    encoded = _json_text(_thaw_json(frozen)).encode("utf-8")
    if len(encoded) > MAX_SIGNATURE_INPUT_BYTES:
        raise InvalidRequestError(f"mutation payload exceeds the UTF-8 limit of {MAX_SIGNATURE_INPUT_BYTES} bytes")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def _signature(value: object) -> str:
    text = _require_text(value, "mutation payload_signature", 71, allow_empty=False)
    if len(text) != 71 or not text.startswith("sha256:") or any(character not in "0123456789abcdef" for character in text[7:]):
        raise InvalidRequestError("mutation payload_signature must be a lowercase SHA-256 signature")
    return text


@dataclass(frozen=True, order=True, slots=True)
class ArtifactGenerationChange:
    """One affected artifact generation; zero denotes concrete absence."""

    statement_id: str
    before_generation: int
    after_generation: int

    def __post_init__(self) -> None:
        _require_text(self.statement_id, "affected statement_id", MAX_RECEIPT_STATEMENT_ID_BYTES, allow_empty=False)
        before = _nonnegative_int(self.before_generation, "affected before_generation")
        after = _nonnegative_int(self.after_generation, "affected after_generation")
        if before == 0 and after == 0:
            raise InvalidRequestError("an affected generation must exist before or after")

    def to_dict(self) -> dict[str, object]:
        return {
            "statement_id": self.statement_id,
            "before_generation": self.before_generation,
            "after_generation": self.after_generation,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> "ArtifactGenerationChange":
        data = _exact_mapping(
            value,
            "ArtifactGenerationChange",
            frozenset({"statement_id", "before_generation", "after_generation"}),
        )
        return cls(
            statement_id=_require_text(
                data["statement_id"],
                "affected statement_id",
                MAX_RECEIPT_STATEMENT_ID_BYTES,
                allow_empty=False,
            ),
            before_generation=_nonnegative_int(data["before_generation"], "affected before_generation"),
            after_generation=_nonnegative_int(data["after_generation"], "affected after_generation"),
        )


@dataclass(frozen=True, slots=True)
class MutationReceipt:
    """Durable replay authority for one mutation request identity."""

    sequence: int
    request_id: str
    operation: MutationOperation
    payload_signature: str
    result_code: MutationResultCode
    affected_generations: tuple[ArtifactGenerationChange, ...]
    result: Mapping[str, object]
    completion_state: ReceiptCompletionState
    created_at: str
    schema_version: int = MUTATION_RECEIPT_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != MUTATION_RECEIPT_SCHEMA_VERSION:
            raise InvalidRequestError(f"unsupported mutation receipt schema_version: {self.schema_version}")
        _positive_int(self.sequence, "mutation receipt sequence")
        _require_text(self.request_id, "mutation request_id", MAX_REQUEST_ID_BYTES, allow_empty=False)
        if not isinstance(self.operation, MutationOperation):
            raise InvalidRequestError("mutation operation must be a MutationOperation")
        _signature(self.payload_signature)
        if not isinstance(self.result_code, MutationResultCode):
            raise InvalidRequestError("mutation result_code must be a MutationResultCode")
        if not isinstance(self.affected_generations, tuple) or not all(
            isinstance(change, ArtifactGenerationChange) for change in self.affected_generations
        ):
            raise InvalidRequestError("affected_generations must be a tuple of ArtifactGenerationChange values")
        if len(self.affected_generations) > MAX_AFFECTED_GENERATIONS:
            raise InvalidRequestError(f"affected_generations exceed the limit of {MAX_AFFECTED_GENERATIONS}")
        ordered = tuple(sorted(set(self.affected_generations)))
        if len(ordered) != len(self.affected_generations):
            raise InvalidRequestError("affected_generations must be unique")
        if tuple(sorted(change.statement_id for change in ordered)) != tuple(change.statement_id for change in ordered):
            raise InvalidRequestError("affected_generations must be ordered by statement_id")
        object.__setattr__(self, "affected_generations", ordered)
        object.__setattr__(self, "result", _freeze_result(self.result))
        if not isinstance(self.completion_state, ReceiptCompletionState):
            raise InvalidRequestError("receipt completion_state must be a ReceiptCompletionState")
        _timestamp(self.created_at, "mutation receipt created_at")
        if self.completion_state == ReceiptCompletionState.PREPARED and (
            self.affected_generations or self.result or self.result_code != MutationResultCode.REJECTED_CAPACITY
        ):
            raise InvalidRequestError("a PREPARED receipt must carry empty effects and the concrete placeholder result code")

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "sequence": self.sequence,
            "request_id": self.request_id,
            "operation": self.operation.value,
            "payload_signature": self.payload_signature,
            "result_code": self.result_code.value,
            "affected_generations": [change.to_dict() for change in self.affected_generations],
            "result": _thaw_json(self.result),
            "completion_state": self.completion_state.value,
            "created_at": self.created_at,
        }

    def to_json(self) -> str:
        return _json_text(self.to_dict())

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> "MutationReceipt":
        keys = frozenset(
            {
                "schema_version",
                "sequence",
                "request_id",
                "operation",
                "payload_signature",
                "result_code",
                "affected_generations",
                "result",
                "completion_state",
                "created_at",
            }
        )
        data = _exact_mapping(value, "MutationReceipt", keys)
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
        return cls(
            schema_version=_positive_int(data["schema_version"], "mutation receipt schema_version"),
            sequence=_positive_int(data["sequence"], "mutation receipt sequence"),
            request_id=_require_text(data["request_id"], "mutation request_id", MAX_REQUEST_ID_BYTES, allow_empty=False),
            operation=operation,
            payload_signature=_signature(data["payload_signature"]),
            result_code=result_code,
            affected_generations=tuple(ArtifactGenerationChange.from_dict(change) for change in affected),
            result=result,
            completion_state=completion_state,
            created_at=_timestamp(data["created_at"], "mutation receipt created_at"),
        )

    @classmethod
    def from_json(cls, value: str) -> "MutationReceipt":
        if not isinstance(value, str):
            raise InvalidRequestError("MutationReceipt JSON must be a string")
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError as error:
            raise InvalidRequestError("MutationReceipt JSON is malformed") from error
        if not isinstance(decoded, Mapping):
            raise InvalidRequestError("MutationReceipt JSON must contain an object")
        return cls.from_dict(decoded)


@dataclass(frozen=True, order=True, slots=True)
class ReceiptTombstone:
    """Bounded protection against ambiguous reuse after result pruning."""

    sequence: int
    request_id: str
    operation: MutationOperation
    payload_signature: str

    def __post_init__(self) -> None:
        _positive_int(self.sequence, "receipt tombstone sequence")
        _require_text(self.request_id, "mutation request_id", MAX_REQUEST_ID_BYTES, allow_empty=False)
        if not isinstance(self.operation, MutationOperation):
            raise InvalidRequestError("mutation operation must be a MutationOperation")
        _signature(self.payload_signature)

    def to_dict(self) -> dict[str, object]:
        return {
            "sequence": self.sequence,
            "request_id": self.request_id,
            "operation": self.operation.value,
            "payload_signature": self.payload_signature,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> "ReceiptTombstone":
        data = _exact_mapping(
            value,
            "ReceiptTombstone",
            frozenset({"sequence", "request_id", "operation", "payload_signature"}),
        )
        try:
            operation = MutationOperation(data["operation"])
        except (TypeError, ValueError) as error:
            raise InvalidRequestError("receipt tombstone contains an unsupported operation") from error
        return cls(
            sequence=_positive_int(data["sequence"], "receipt tombstone sequence"),
            request_id=_require_text(data["request_id"], "mutation request_id", MAX_REQUEST_ID_BYTES, allow_empty=False),
            operation=operation,
            payload_signature=_signature(data["payload_signature"]),
        )


@dataclass(frozen=True, slots=True)
class ReceiptLookup:
    """Concrete pre-mutation lookup without an optional receipt object."""

    outcome: ReceiptLookupOutcome
    request_id: str
    receipt_json: str
    receipt_available: bool

    def __post_init__(self) -> None:
        if not isinstance(self.outcome, ReceiptLookupOutcome):
            raise InvalidRequestError("receipt lookup outcome must be a ReceiptLookupOutcome")
        _require_text(self.request_id, "mutation request_id", MAX_REQUEST_ID_BYTES, allow_empty=False)
        _require_text(self.receipt_json, "receipt_json", MAX_RECEIPT_JSON_BYTES, allow_empty=True)
        if not isinstance(self.receipt_available, bool):
            raise InvalidRequestError("receipt_available must be a boolean")
        if self.receipt_available != bool(self.receipt_json):
            raise InvalidRequestError("receipt availability must agree with receipt_json")
        if self.outcome in {ReceiptLookupOutcome.REPLAY, ReceiptLookupOutcome.IN_PROGRESS} and not self.receipt_available:
            raise InvalidRequestError("replay or in-progress lookup must carry a receipt")
        if self.outcome not in {ReceiptLookupOutcome.REPLAY, ReceiptLookupOutcome.IN_PROGRESS} and self.receipt_available:
            raise InvalidRequestError("new, expired, or conflict lookup must not carry a receipt")

    def receipt(self) -> MutationReceipt:
        if not self.receipt_available:
            raise ResourceNotFoundError(f"mutation receipt is not available for lookup outcome: {self.outcome.value}")
        return MutationReceipt.from_json(self.receipt_json)


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
        if not isinstance(receipts, tuple) or not all(isinstance(receipt, MutationReceipt) for receipt in receipts):
            raise InvalidRequestError("receipts must be a tuple of MutationReceipt values")
        if not isinstance(tombstones, tuple) or not all(isinstance(tombstone, ReceiptTombstone) for tombstone in tombstones):
            raise InvalidRequestError("tombstones must be a tuple of ReceiptTombstone values")
        if len(receipts) > self.max_receipts or len(tombstones) > self.max_tombstones:
            raise InvalidRequestError("receipt ledger state exceeds its configured retention bounds")
        all_sequences = [receipt.sequence for receipt in receipts] + [tombstone.sequence for tombstone in tombstones]
        if len(set(all_sequences)) != len(all_sequences):
            raise InvalidRequestError("receipt ledger sequences must be unique")
        request_ids = [receipt.request_id for receipt in receipts] + [tombstone.request_id for tombstone in tombstones]
        if len(set(request_ids)) != len(request_ids):
            raise InvalidRequestError("receipt ledger request IDs must be unique")
        if all_sequences and self._next_sequence <= max(all_sequences):
            raise InvalidRequestError("next receipt sequence must exceed every retained sequence")
        self._lock = threading.RLock()
        self._receipts = {receipt.request_id: receipt for receipt in receipts}
        self._tombstones = {tombstone.request_id: tombstone for tombstone in tombstones}

    @property
    def next_sequence(self) -> int:
        with self._lock:
            return self._next_sequence

    def lookup(self, request_id: str, operation: MutationOperation, payload_signature: str) -> ReceiptLookup:
        normalized_id = _require_text(request_id, "mutation request_id", MAX_REQUEST_ID_BYTES, allow_empty=False)
        if not isinstance(operation, MutationOperation):
            raise InvalidRequestError("mutation operation must be a MutationOperation")
        normalized_signature = _signature(payload_signature)
        with self._lock:
            if normalized_id in self._receipts:
                receipt = self._receipts[normalized_id]
                if receipt.operation != operation or receipt.payload_signature != normalized_signature:
                    return ReceiptLookup(ReceiptLookupOutcome.CONFLICT, normalized_id, "", False)
                outcome = (
                    ReceiptLookupOutcome.REPLAY
                    if receipt.completion_state == ReceiptCompletionState.COMPLETED
                    else ReceiptLookupOutcome.IN_PROGRESS
                )
                return ReceiptLookup(outcome, normalized_id, receipt.to_json(), True)
            if normalized_id in self._tombstones:
                tombstone = self._tombstones[normalized_id]
                outcome = (
                    ReceiptLookupOutcome.EXPIRED
                    if tombstone.operation == operation and tombstone.payload_signature == normalized_signature
                    else ReceiptLookupOutcome.CONFLICT
                )
                return ReceiptLookup(outcome, normalized_id, "", False)
            return ReceiptLookup(ReceiptLookupOutcome.NEW, normalized_id, "", False)

    def record(self, receipt: MutationReceipt) -> MutationReceipt:
        if not isinstance(receipt, MutationReceipt):
            raise InvalidRequestError("receipt must be a MutationReceipt")
        with self._lock:
            lookup = self.lookup(receipt.request_id, receipt.operation, receipt.payload_signature)
            if lookup.outcome == ReceiptLookupOutcome.CONFLICT:
                raise ConflictError(f"request_id is already associated with a different mutation: {receipt.request_id}")
            if lookup.outcome == ReceiptLookupOutcome.EXPIRED:
                raise ConflictError(f"request_id result was pruned and cannot be replayed safely: {receipt.request_id}")
            if lookup.outcome == ReceiptLookupOutcome.REPLAY:
                existing = lookup.receipt()
                if existing != receipt:
                    raise ConflictError(f"completed mutation receipt cannot be changed: {receipt.request_id}")
                return existing
            if lookup.outcome == ReceiptLookupOutcome.IN_PROGRESS:
                existing = lookup.receipt()
                if receipt.completion_state != ReceiptCompletionState.COMPLETED or receipt.sequence != existing.sequence:
                    raise ConflictError(f"prepared mutation receipt can only advance to COMPLETED: {receipt.request_id}")
                self._receipts[receipt.request_id] = receipt
                return receipt
            if receipt.sequence != self._next_sequence:
                raise ConflictError(f"receipt sequence conflict: expected {self._next_sequence}, received {receipt.sequence}")
            self._receipts[receipt.request_id] = receipt
            self._next_sequence += 1
            self._prune()
            return receipt

    def _prune(self) -> None:
        while len(self._receipts) > self.max_receipts:
            oldest = min(self._receipts.values(), key=lambda receipt: receipt.sequence)
            del self._receipts[oldest.request_id]
            self._tombstones[oldest.request_id] = ReceiptTombstone(
                oldest.sequence,
                oldest.request_id,
                oldest.operation,
                oldest.payload_signature,
            )
        while len(self._tombstones) > self.max_tombstones:
            oldest = min(self._tombstones.values(), key=lambda tombstone: tombstone.sequence)
            del self._tombstones[oldest.request_id]

    def snapshot(self) -> dict[str, object]:
        with self._lock:
            return {
                "schema_version": MUTATION_LEDGER_SCHEMA_VERSION,
                "max_receipts": self.max_receipts,
                "max_tombstones": self.max_tombstones,
                "next_sequence": self._next_sequence,
                "receipts": [receipt.to_dict() for receipt in sorted(self._receipts.values(), key=lambda item: item.sequence)],
                "tombstones": [
                    tombstone.to_dict() for tombstone in sorted(self._tombstones.values(), key=lambda item: item.sequence)
                ],
            }

    def replace_from_snapshot(self, value: Mapping[str, object]) -> None:
        """Atomically restore a validated ledger without changing owner identity."""

        replacement = self.from_snapshot(value)
        with self._lock:
            self.max_receipts = replacement.max_receipts
            self.max_tombstones = replacement.max_tombstones
            self._next_sequence = replacement._next_sequence
            self._receipts = dict(replacement._receipts)
            self._tombstones = dict(replacement._tombstones)

    @classmethod
    def from_snapshot(cls, value: Mapping[str, object]) -> "MutationReceiptLedger":
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
        return cls(
            max_receipts=_positive_int(data["max_receipts"], "max_receipts", MAX_RECEIPTS),
            max_tombstones=_positive_int(data["max_tombstones"], "max_tombstones", MAX_TOMBSTONES),
            receipts=tuple(MutationReceipt.from_dict(receipt) for receipt in receipts),
            tombstones=tuple(ReceiptTombstone.from_dict(tombstone) for tombstone in tombstones),
            next_sequence=_positive_int(data["next_sequence"], "next receipt sequence"),
        )
