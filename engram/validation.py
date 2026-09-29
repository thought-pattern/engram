"""Shared validators for text, identifiers, booleans, and canonical UTC timestamps.

Every module checks its fields through these, so a size limit or character
rule means the same thing everywhere and each failure reads the same way.
"""

from datetime import datetime
from enum import StrEnum
from functools import lru_cache
from re import compile as re_compile

from engram.constants import MAX_TIMESTAMP_BYTES
from engram.errors import InvalidRequestError

CONTROL_CHARACTERS = re_compile("[\x00-\x1f\x7f]")
CONTROL_CHARACTERS_EXCEPT_LINE_BREAKS = re_compile("[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


class Characters(StrEnum):
    """Which characters a text field accepts."""

    ANY = "any"
    """Any valid Unicode text."""
    TEXT = "text"
    """No control characters: C0 controls and DEL."""
    LINES = "lines"
    """No control characters except tab, line feed, and carriage return."""
    IDENTIFIER = "identifier"
    """No control characters and no whitespace."""


def require_text(
    value: object,
    name: str,
    maximum_bytes: int,
    *,
    allow_empty: bool = False,
    blank_is_empty: bool = False,
    characters: Characters = Characters.TEXT,
    error: type[InvalidRequestError] = InvalidRequestError,
) -> str:
    """Return ``value`` when it is text that fits the field, or raise ``error``.

    ``blank_is_empty`` treats whitespace-only text as empty.
    """
    if not isinstance(value, str):
        raise error(f"{name} must be a string")
    if not allow_empty:
        if not value:
            raise error(f"{name} must not be empty")
        if blank_is_empty and not value.strip():
            raise error(f"{name} must contain non-whitespace text")
    try:
        size = len(value.encode("utf-8"))
    except UnicodeEncodeError as encoding_error:
        raise error(f"{name} must contain valid Unicode") from encoding_error
    if size > maximum_bytes:
        raise error(f"{name} exceeds the limit of {maximum_bytes} UTF-8 bytes")
    if characters == Characters.TEXT and CONTROL_CHARACTERS.search(value):
        raise error(f"{name} contains a control character")
    if characters == Characters.LINES and CONTROL_CHARACTERS_EXCEPT_LINE_BREAKS.search(value):
        raise error(f"{name} contains a control character")
    if characters == Characters.IDENTIFIER and (CONTROL_CHARACTERS.search(value) or any(item.isspace() for item in value)):
        raise error(f"{name} must not contain whitespace or control characters")
    return value


def require_any_text(
    value: object,
    name: str,
    maximum_bytes: int,
    *,
    allow_empty: bool = False,
    blank_is_empty: bool = False,
) -> str:
    """Like ``require_text``, but any valid Unicode is accepted, control characters included.

    For free text such as responses, which may span lines.
    """
    result = require_text(
        value,
        name,
        maximum_bytes,
        allow_empty=allow_empty,
        blank_is_empty=blank_is_empty,
        characters=Characters.ANY,
    )
    return result


@lru_cache(maxsize=16_384)
def valid_identifier(value: str, name: str, maximum_bytes: int, allow_empty: bool) -> str:
    """Validate one plain-string identifier; only successful results are cached."""
    result = require_text(value, name, maximum_bytes, allow_empty=allow_empty, characters=Characters.IDENTIFIER)
    return result


def require_identifier(value: object, name: str, maximum_bytes: int, *, allow_empty: bool = False) -> str:
    """Return ``value`` when it is an identifier: text with no whitespace or control characters."""
    # The same identifiers are checked many times per request, so plain
    # strings go through a cache; anything else is checked directly.
    if type(value) is str and type(name) is str and type(maximum_bytes) is int:
        result = valid_identifier(value, name, maximum_bytes, allow_empty)
        return result
    result = require_text(value, name, maximum_bytes, allow_empty=allow_empty, characters=Characters.IDENTIFIER)
    return result


def require_bool(value: object, name: str) -> bool:
    """Return ``value`` when it is a boolean."""
    if not isinstance(value, bool):
        raise InvalidRequestError(f"{name} must be a boolean")
    return value


def parse_utc_timestamp(text: str, name: str) -> datetime:
    """Parse a canonical RFC 3339 UTC timestamp such as ``2026-09-28T12:00:00Z``."""
    if not text.endswith("Z"):
        raise InvalidRequestError(f"{name} must be a canonical RFC 3339 UTC timestamp ending in Z")
    try:
        parsed = datetime.fromisoformat(text[:-1] + "+00:00")
    except ValueError as error:
        raise InvalidRequestError(f"{name} must be a canonical RFC 3339 UTC timestamp") from error
    if parsed.isoformat().replace("+00:00", "Z") != text:
        raise InvalidRequestError(f"{name} must use the canonical RFC 3339 UTC representation")
    return parsed


def utc_datetime(text: str) -> datetime:
    """Parse a timestamp already validated as canonical RFC 3339 UTC."""
    result = datetime.fromisoformat(text[:-1] + "+00:00")
    return result


def require_utc_timestamp(
    value: object,
    name: str,
    *,
    allow_empty: bool = False,
    maximum_bytes: int = MAX_TIMESTAMP_BYTES,
) -> str:
    """Return ``value`` when it is a canonical RFC 3339 UTC timestamp, or empty if allowed."""
    text = require_text(value, name, maximum_bytes, allow_empty=allow_empty)
    if text:
        parse_utc_timestamp(text, name)
    return text


def require_utc_datetime(value: object, name: str, *, maximum_bytes: int = MAX_TIMESTAMP_BYTES) -> datetime:
    """Validate a canonical RFC 3339 UTC timestamp and return it parsed."""
    result = parse_utc_timestamp(require_text(value, name, maximum_bytes), name)
    return result


def require_available_utc_timestamp(
    value: object,
    available: object,
    name: str,
    *,
    maximum_bytes: int = MAX_TIMESTAMP_BYTES,
) -> tuple[str, bool]:
    """Validate a timestamp paired with its ``<name>_available`` flag."""
    presence = require_bool(available, f"{name}_available")
    text = require_utc_timestamp(value, name, allow_empty=True, maximum_bytes=maximum_bytes)
    if presence and not text:
        raise InvalidRequestError(f"{name} must not be empty when {name}_available is true")
    if not presence and text:
        raise InvalidRequestError(f"{name} must be empty when {name}_available is false")
    result = (text, presence)
    return result
