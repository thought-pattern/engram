"""Compact-JSON and retained working-memory size estimates for resolution budgets."""

from json import dumps as json_dumps


def json_value(value: object) -> object:
    if isinstance(value, dict):
        result = {key: json_value(item) for key, item in value.items()}
        return result
    if isinstance(value, tuple):
        result = [json_value(item) for item in value]
        return result
    if isinstance(value, list):
        result = [json_value(item) for item in value]
        return result
    return value


def json_size(value: object) -> int:
    result = len(
        json_dumps(
            json_value(value),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    )
    return result


def json_array_bytes(item_bytes: int, count: int) -> int:
    """Return ``json_size`` of a list from its items' total size, or 0 for an empty list.

    Compact JSON adds two brackets and one comma between items, so trimming
    can keep a running total instead of re-encoding the remaining list.
    ``json_array_size`` is the same rule over a list of item sizes.
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


def json_array_size(item_sizes: list[int]) -> int:
    """Return exact compact-JSON bytes for an array of pre-sized values."""
    result = 2 + sum(item_sizes) + max(0, len(item_sizes) - 1)
    return result
