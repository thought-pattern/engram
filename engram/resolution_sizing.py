"""Interchange and retained working-memory sizes for resolution budgets, computed from native values.

Engram's resolution budgets are part of its request contract and count bytes of a value's compact interchange encoding:
UTF-8, keys sorted, no separator spaces, no ASCII escaping. `encoded_size` computes that length from the native value by
arithmetic, so internal budget, trimming and memory decisions never serialize what they measure; the value is encoded
only where it leaves Engram.
"""

from math import isfinite as math_isfinite
from re import compile as re_compile

# Characters the interchange encoding writes as a two-character escape, and the other control characters, written as a
# six-character \u escape.
SHORT_ESCAPES = ('"', "\\", "\n", "\r", "\t", "\b", "\f")
LONG_ESCAPES = re_compile("[\x00-\x07\x0b\x0e-\x1f]")


def text_size(value: str) -> int:
    """Bytes of one string in the interchange encoding: its UTF-8 bytes, its quotes and its escapes."""
    result = len(value.encode("utf-8")) + 2
    result += sum(value.count(character) for character in SHORT_ESCAPES)
    result += 5 * len(LONG_ESCAPES.findall(value))
    return result


def number_size(value: object) -> int:
    """Bytes of one Boolean, integer or finite float, written as the interchange encoding writes it."""
    if isinstance(value, bool):
        result = 4 if value else 5
        return result
    if isinstance(value, int):
        result = len(int.__repr__(value))
        return result
    if not math_isfinite(value):
        raise ValueError("Out of range float values are not JSON compliant")
    result = len(float.__repr__(value))
    return result


def key_size(key: object) -> int:
    """Bytes of one mapping key, which the interchange encoding always writes as a string."""
    if isinstance(key, str):
        result = text_size(key)
        return result
    if key is None:
        result = 6
        return result
    if isinstance(key, (bool, int, float)):
        result = number_size(key) + 2
        return result
    raise TypeError(f"keys must be str, int, float, bool or None, not {type(key).__name__}")


def encoded_size(value: object) -> int:
    """Bytes of `value`'s compact interchange encoding, computed without encoding it.

    Mappings, lists and tuples are measured item by item; strings, numbers, Booleans and None as written. Non-finite
    floats raise ValueError and any other value TypeError, as encoding them would.
    """
    if isinstance(value, str):
        result = text_size(value)
        return result
    if value is None:
        result = 4
        return result
    if isinstance(value, (bool, int, float)):
        result = number_size(value)
        return result
    if isinstance(value, dict):
        result = array_size([key_size(key) + 1 + encoded_size(item) for key, item in value.items()])
        return result
    if isinstance(value, (list, tuple)):
        result = array_size([encoded_size(item) for item in value])
        return result
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


def array_bytes(item_bytes: int, count: int) -> int:
    """Return ``encoded_size`` of a list from its items' total size, or 0 for an empty list.

    The encoding adds two brackets and one comma between items, so trimming can keep a running total instead of
    re-measuring the remaining list. ``array_size`` is the same rule over a list of item sizes.
    """
    result = 2 + item_bytes + count - 1 if count else 0
    return result


def working_size(value: object, seen=()) -> int:
    """Conservatively estimate retained runtime values, including non-wire types."""
    visited = seen if isinstance(seen, set) else set()
    if isinstance(value, str):
        result = len(value.encode("utf-8")) + 49
        return result
    if isinstance(value, bytes):
        result = len(value) + 33
        return result
    if isinstance(value, (bool, int, float)):
        result = 32
        return result
    identity = id(value)
    if identity in visited:
        result = 0
        return result
    visited.add(identity)
    if isinstance(value, dict):
        result = 64 + sum(working_size(key, visited) + working_size(item, visited) for key, item in value.items())
        return result
    if isinstance(value, (list, tuple, set)):
        result = 64 + sum(working_size(item, visited) for item in value)
        return result
    result = len(str(value).encode("utf-8")) + 64
    return result


def array_size(item_sizes: list[int]) -> int:
    """Return the exact encoded bytes of an array, or of a mapping's members, from its pre-sized items."""
    result = 2 + sum(item_sizes) + max(0, len(item_sizes) - 1)
    return result
