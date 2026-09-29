"""Copies of already validated in-process values."""


def structural_copy(value: object) -> object:
    """Copy nested dicts, lists, and tuples, keeping their types.

    Validated state is shared inside the package and never changed in place.
    Public reads return a structural copy so a caller cannot reach live
    state. Copying is much cheaper than validating the value again.
    """
    if isinstance(value, dict):
        result: object = {key: structural_copy(item) for key, item in value.items()}
        return result
    if isinstance(value, list):
        result = [structural_copy(item) for item in value]
        return result
    if isinstance(value, tuple):
        result = tuple(structural_copy(item) for item in value)
        return result
    return value
