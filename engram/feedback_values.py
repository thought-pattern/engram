"""Canonical native feedback values and explicit interchange codecs."""

from datetime import UTC, datetime
from hashlib import sha256 as hashlib_sha256
from json import JSONDecodeError as json_JSONDecodeError, dumps as json_dumps, loads as json_loads
from math import isfinite as math_isfinite

from msgpack import ExtType as msgpack_ExtType, packb as msgpack_packb

from engram.constants import (
    MAX_CONSTRAINT_ENCODED_BYTES,
    MAX_ENUM_WIRE_BYTES,
    MAX_FINGERPRINT_BYTES,
    MAX_REQUIRED_SOURCE_LABEL_BYTES,
    MAX_RESOURCE_COUNTER,
)
from engram.errors import InvalidRequestError
from engram.validation import require_text, require_utc_datetime


def internal_integer(value: object, name: str, minimum: int = 0, maximum: int = MAX_RESOURCE_COUNTER) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise InvalidRequestError(f"{name} must be an integer from {minimum} through {maximum}")
    return value


def internal_number(value: object, name: str, minimum: float, maximum: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise InvalidRequestError(f"{name} must be a number")
    normalized = float(value)
    if not math_isfinite(normalized) or not minimum <= normalized <= maximum:
        raise InvalidRequestError(f"{name} must be finite and from {minimum} through {maximum}")
    return normalized


def exact_mapping(value: object, name: str, fields: set[str]) -> dict:
    if not isinstance(value, dict):
        raise InvalidRequestError(f"{name} must be an object")
    actual = set(value)
    if actual != fields:
        raise InvalidRequestError(f"{name} has invalid fields: missing={sorted(fields - actual)}, extra={sorted(actual - fields)}")
    return value


def json_text(value: object) -> str:
    result = json_dumps(thaw_native_value(value), sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    return result


def thaw_native_value(value: object) -> object:
    """Give frozen tuples and their thawed lists one shared native form."""
    if isinstance(value, dict):
        result = {str(key): thaw_native_value(item) for key, item in value.items()}
        return result
    if isinstance(value, tuple):
        result = [thaw_native_value(item) for item in value]
        return result
    return value


def load_json_mapping(value: str, name: str) -> dict:
    if not isinstance(value, str):
        raise InvalidRequestError(f"{name} must be a string")
    try:
        decoded = json_loads(value)
    except json_JSONDecodeError as err:
        raise InvalidRequestError(f"{name} must be valid JSON") from err
    if not isinstance(decoded, dict):
        raise InvalidRequestError(f"{name} must contain an object")
    return decoded


def canonical_binary_value(value: object) -> object:
    """Sort native mappings and reserve extension tags for tuples and large integers.

    Engram-owned fingerprint inputs carry no null token, so ``None`` is
    rejected with every other unsupported value instead of being encoded.
    """
    if isinstance(value, str) and type(value) is str:
        return value
    if isinstance(value, bool) and type(value) is bool:
        return value
    if isinstance(value, int) and type(value) is int:
        if -(1 << 63) <= value < (1 << 64):
            return value
        large_integer = msgpack_ExtType(2, str(value).encode("ascii"))
        return large_integer
    if isinstance(value, float) and type(value) is float:
        if not math_isfinite(value):
            raise InvalidRequestError("feedback fingerprint values must be finite")
        return value
    if isinstance(value, list) and type(value) is list:
        items = [canonical_binary_value(item) for item in value]
        return items
    if isinstance(value, dict) and type(value) is dict:
        if any(type(key) is not str for key in value):
            raise InvalidRequestError("feedback fingerprint mapping keys must be strings")
        # Keys are unique strings, so ordering the items never compares values.
        mapping = {key: canonical_binary_value(item) for key, item in sorted(value.items())}
        return mapping
    if isinstance(value, tuple) and type(value) is tuple:
        elements = [canonical_binary_value(item) for item in value]
        encoded = msgpack_packb(elements, use_bin_type=True, strict_types=True, use_single_float=False)
        if not isinstance(encoded, bytes):
            raise InvalidRequestError("feedback fingerprint tuple encoding failed")
        tagged_tuple = msgpack_ExtType(1, encoded)
        return tagged_tuple
    raise InvalidRequestError("feedback fingerprint contains an unsupported native value")


def canonical_binary_encoding(value: object) -> bytes:
    """Encode one native value as canonical MessagePack bytes for hashing and bounds."""
    try:
        encoded = msgpack_packb(canonical_binary_value(value), use_bin_type=True, strict_types=True, use_single_float=False)
    except InvalidRequestError:
        raise
    except (OverflowError, ValueError, TypeError, UnicodeError, RecursionError) as err:
        raise InvalidRequestError("feedback fingerprint contains an invalid native value") from err
    if not isinstance(encoded, bytes):
        raise InvalidRequestError("feedback fingerprint encoding failed")
    return encoded


def canonical_fingerprint(value: object) -> str:
    """Return one domain-separated, canonical binary SHA-256 fingerprint."""
    encoded = canonical_binary_encoding(value)
    result = hashlib_sha256(b"engram-feedback-fingerprint\0" + encoded).hexdigest()
    return result


def internal_fingerprint(value: object, name: str) -> str:
    if not isinstance(value, str):
        raise InvalidRequestError(f"{name} must be a string")
    if len(value) != MAX_FINGERPRINT_BYTES or any(character not in "0123456789abcdef" for character in value):
        raise InvalidRequestError(f"{name} must be a lowercase SHA-256 fingerprint")
    return value


def canonical_utc(value: datetime) -> str:
    """Serialize an aware UTC datetime using the repository's canonical form."""

    if not isinstance(value, datetime) or value.tzinfo is None:
        raise InvalidRequestError("feedback clock must return an aware datetime")
    result = value.astimezone(UTC).isoformat().replace("+00:00", "Z")
    return result


def bucket_start(value: str, bucket_seconds: int) -> str:
    parsed = require_utc_datetime(value, "feedback observed_at")
    unix_seconds = int(parsed.timestamp())
    start_seconds = unix_seconds - (unix_seconds % bucket_seconds)
    result = canonical_utc(datetime.fromtimestamp(start_seconds, UTC))
    return result


def internal_diagnostic_id(value: str) -> str:
    result = f"sha256:{hashlib_sha256(value.encode('utf-8')).hexdigest()[:24]}"
    return result


def constraint_fingerprint(
    expected_object_type: str,
    required_metadata: dict,
    required_source_label: str,
) -> str:
    """Fingerprint the bounded request constraints that affect eligibility.

    Frozen (tuple) and thawed (list) metadata describe the same constraint, so
    both are thawed to one form before the canonical binary encoding.  The
    fingerprint keys only process-memory feedback and negative-resolution
    state, so no persisted identity depends on its exact bytes.
    """

    if not isinstance(required_metadata, dict):
        raise InvalidRequestError("feedback required_metadata must be an object")
    require_text(expected_object_type, "feedback expected_object_type", MAX_ENUM_WIRE_BYTES)
    require_text(required_source_label, "feedback required_source_label", MAX_REQUIRED_SOURCE_LABEL_BYTES, allow_empty=True)
    value = {
        "expected_object_type": expected_object_type,
        "required_metadata": dict(required_metadata),
        "required_source_label": required_source_label,
    }
    try:
        encoded = canonical_binary_encoding(thaw_native_value(value))
    except (InvalidRequestError, RecursionError) as err:
        raise InvalidRequestError("feedback constraints must contain bounded native values") from err
    if len(encoded) > MAX_CONSTRAINT_ENCODED_BYTES:
        raise InvalidRequestError(f"feedback constraints exceed the encoded limit of {MAX_CONSTRAINT_ENCODED_BYTES} bytes")
    result = hashlib_sha256(encoded).hexdigest()
    return result
