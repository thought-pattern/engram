"""Deterministic working-memory accounting shared by request-local operations."""


def estimate_working_bytes(value: object, seen=()) -> int:
    """Return a conservative, bounded-size estimate for request working data."""
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
        result = 64 + sum(
            estimate_working_bytes(key, visited) + estimate_working_bytes(item, visited) for key, item in value.items()
        )
        return result
    if isinstance(value, (list, tuple, set)):
        result = 64 + sum(estimate_working_bytes(item, visited) for item in value)
        return result
    result = len(str(value).encode("utf-8")) + 64
    return result


def require_working_memory(estimated_bytes: int, maximum_bytes: int) -> None:
    """Raise before retaining work that exceeds a request's memory estimate."""
    if maximum_bytes and estimated_bytes > maximum_bytes:
        raise MemoryError("resolution working-memory estimate exceeded")
