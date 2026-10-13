"""Native validation, isolated contract values, and public JSON codecs for resolution."""

from json import JSONDecodeError as json_JSONDecodeError, dumps as json_dumps, loads as json_loads
from math import isfinite as math_isfinite

from engram.constants import (
    MAX_FROZEN_JSON_KEY_BYTES,
    MAX_JSON_BYTES,
    MAX_JSON_DEPTH,
    MAX_JSON_ITEMS,
    MAX_JSON_STRING_BYTES,
)
from engram.errors import InvalidRequestError
from engram.validation import require_any_text


def require_int(value: object, name: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise InvalidRequestError(f"{name} must be an integer")
    if not minimum <= value <= maximum:
        raise InvalidRequestError(f"{name} must be from {minimum} through {maximum}")
    return value


def require_float(value: object, name: str, minimum: float, maximum: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise InvalidRequestError(f"{name} must be numeric")
    # Compare before converting: an integer too large for a float is a range
    # refusal, not an OverflowError. NaN fails the comparison.
    if (isinstance(value, float) and not math_isfinite(value)) or not minimum <= value <= maximum:
        raise InvalidRequestError(f"{name} must be finite and from {minimum} through {maximum}")
    result = float(value)
    return result


def exact_mapping(value: object, name: str, keys: set[str]) -> dict[str, object]:
    if not isinstance(value, dict):
        raise InvalidRequestError(f"{name} must be an object")
    # Check key types before the field report sorts them: mixed key types cannot be ordered.
    if not all(isinstance(key, str) for key in value):
        raise InvalidRequestError(f"{name} field names must be strings")
    observed = set(value)
    if observed != keys:
        raise InvalidRequestError(f"{name} has invalid fields: missing={sorted(keys - observed)}, extra={sorted(observed - keys)}")
    return value


def require_list(value: object, name: str) -> list[object]:
    if not isinstance(value, (list, tuple)):
        raise InvalidRequestError(f"{name} must be an array")
    result = list(value)
    return result


def freeze_json(value: object, name: str, depth: int = 0, count=()) -> object:
    if not count:
        count = [0]
    if depth > MAX_JSON_DEPTH:
        raise InvalidRequestError(f"{name} exceeds the maximum nesting depth of {MAX_JSON_DEPTH}")
    count[0] += 1
    if count[0] > MAX_JSON_ITEMS:
        raise InvalidRequestError(f"{name} exceeds the limit of {MAX_JSON_ITEMS} values")
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        result = require_any_text(value, name, MAX_JSON_STRING_BYTES, allow_empty=True)
        return result
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if isinstance(value, float):
        if not math_isfinite(value):
            raise InvalidRequestError(f"{name} numbers must be finite")
        return value
    if isinstance(value, dict):
        # Check key types before sorting: mixed key types cannot be ordered.
        if not all(isinstance(key, str) for key in value):
            raise InvalidRequestError(f"{name} keys must be strings")
        frozen = {}
        # Keys are unique strings, so ordering the items never compares their values.
        for key, item in sorted(value.items()):
            require_any_text(key, f"{name} key", MAX_FROZEN_JSON_KEY_BYTES, allow_empty=False)
            frozen[key] = freeze_json(item, name, depth + 1, count)
        result = dict(frozen)
        return result
    if isinstance(value, (list, tuple)):
        result = tuple(freeze_json(item, name, depth + 1, count) for item in value)
        return result
    raise InvalidRequestError(f"{name} values must be concrete JSON values")


def freeze_mapping(value: object, name: str) -> dict[str, object]:
    frozen = freeze_json(value, name)
    if not isinstance(frozen, dict):
        raise InvalidRequestError(f"{name} must be an object")
    encoded = json_dumps(thaw_json(frozen), sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    if len(encoded.encode("utf-8")) > MAX_JSON_BYTES:
        raise InvalidRequestError(f"{name} exceeds the limit of {MAX_JSON_BYTES} UTF-8 bytes")
    return frozen


def thaw_json(value: object) -> object:
    if isinstance(value, dict):
        result = {key: thaw_json(item) for key, item in value.items()}
        return result
    if isinstance(value, tuple):
        result = [thaw_json(item) for item in value]
        return result
    return value


def json_text(value: dict[str, object]) -> str:
    result = json_dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    return result


def load_json_mapping(value: str, name: str) -> dict[str, object]:
    if not isinstance(value, str):
        raise InvalidRequestError(f"{name} must be a string")
    try:
        decoded = json_loads(value)
    except json_JSONDecodeError as err:
        raise InvalidRequestError(f"{name} must be valid JSON") from err
    if not isinstance(decoded, dict):
        raise InvalidRequestError(f"{name} must contain an object")
    return decoded


def enum_tuple(values: object, enum_type, name: str, maximum: int) -> tuple:
    if not isinstance(values, tuple):
        raise InvalidRequestError(f"{name} must be a tuple")
    if len(values) > maximum:
        raise InvalidRequestError(f"{name} exceeds the limit of {maximum}")
    if not all(isinstance(value, enum_type) for value in values):
        raise InvalidRequestError(f"{name} must contain only {enum_type.__name__} values")
    if len(set(values)) != len(values):
        raise InvalidRequestError(f"{name} must not contain duplicates")
    return values
