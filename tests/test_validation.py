"""Shared validators: one meaning for each limit and character rule."""

from datetime import UTC, datetime

from pytest import mark as pytest_mark, raises as pytest_raises

from engram.errors import IdentityValidationError, InvalidRequestError
from engram.validation import (
    Characters,
    require_any_text,
    require_available_utc_timestamp,
    require_bool,
    require_identifier,
    require_text,
    require_utc_datetime,
    require_utc_timestamp,
    utc_datetime,
)


def test_text_must_be_a_string_and_not_empty_unless_allowed() -> None:
    with pytest_raises(InvalidRequestError, match="field must be a string"):
        require_text(7, "field", 16)
    with pytest_raises(InvalidRequestError, match="field must not be empty"):
        require_text("", "field", 16)
    assert require_text("", "field", 16, allow_empty=True) == ""


def test_blank_text_counts_as_empty_only_when_asked() -> None:
    assert require_text("   ", "field", 16) == "   "
    with pytest_raises(InvalidRequestError, match="non-whitespace"):
        require_text("   ", "field", 16, blank_is_empty=True)


def test_the_limit_counts_utf8_bytes() -> None:
    assert require_text("é" * 8, "field", 16) == "é" * 8
    with pytest_raises(InvalidRequestError, match="field exceeds the limit of 16 UTF-8 bytes"):
        require_text("é" * 9, "field", 16)


def test_a_lone_surrogate_is_rejected_as_invalid_unicode_not_a_crash() -> None:
    with pytest_raises(InvalidRequestError, match="valid Unicode"):
        require_any_text("bad \ud800 text", "field", 64)


@pytest_mark.parametrize("character", ["\x00", "\x07", "\t", "\n", "\r", "\x1f", "\x7f"])
def test_text_rejects_control_characters(character: str) -> None:
    with pytest_raises(InvalidRequestError, match="control character"):
        require_text(f"a{character}b", "field", 64)


def test_text_accepts_other_unicode() -> None:
    assert require_text("café ☕ \x85  ", "field", 64) == "café ☕ \x85  "


def test_lines_allow_tab_and_line_breaks_but_no_other_controls() -> None:
    assert require_text("one\ttwo\r\nthree", "field", 64, characters=Characters.LINES) == "one\ttwo\r\nthree"
    with pytest_raises(InvalidRequestError, match="control character"):
        require_text("bell\x07", "field", 64, characters=Characters.LINES)


def test_any_text_accepts_control_characters() -> None:
    assert require_any_text("a\x00b\nc", "field", 64) == "a\x00b\nc"


def test_identifiers_reject_whitespace_and_controls() -> None:
    assert require_identifier("prp_abc-123", "field", 64) == "prp_abc-123"
    for value in ("two words", "tab\there", "nbsp here", "nul\x00"):
        with pytest_raises(InvalidRequestError, match="whitespace or control"):
            require_identifier(value, "field", 64)
    assert require_identifier("", "field", 64, allow_empty=True) == ""


def test_a_rejected_identifier_is_not_cached_as_valid() -> None:
    for _ in range(2):
        with pytest_raises(InvalidRequestError):
            require_identifier("not valid", "cached field", 64)


def test_the_error_type_can_be_chosen() -> None:
    with pytest_raises(IdentityValidationError):
        require_text("", "field", 16, error=IdentityValidationError)


def test_booleans_must_be_booleans() -> None:
    assert require_bool(False, "flag") is False
    with pytest_raises(InvalidRequestError, match="flag must be a boolean"):
        require_bool(0, "flag")


@pytest_mark.parametrize(
    ("value", "message"),
    [
        ("2026-09-28T12:00:00+00:00", "ending in Z"),
        ("2026-13-28T12:00:00Z", "canonical RFC 3339 UTC timestamp"),
        ("2026-09-28T12:00:00.000Z", "canonical RFC 3339 UTC representation"),
    ],
)
def test_timestamps_must_be_canonical_utc(value: str, message: str) -> None:
    with pytest_raises(InvalidRequestError, match=message):
        require_utc_timestamp(value, "at")


def test_canonical_timestamps_are_accepted_and_parsed() -> None:
    assert require_utc_timestamp("2026-09-28T12:00:00.123456Z", "at") == "2026-09-28T12:00:00.123456Z"
    assert require_utc_timestamp("", "at", allow_empty=True) == ""
    expected = datetime(2026, 9, 28, 12, tzinfo=UTC)
    assert require_utc_datetime("2026-09-28T12:00:00Z", "at") == expected
    assert utc_datetime("2026-09-28T12:00:00Z") == expected


def test_available_timestamps_follow_their_flag() -> None:
    assert require_available_utc_timestamp("2026-09-28T12:00:00Z", True, "at") == ("2026-09-28T12:00:00Z", True)
    assert require_available_utc_timestamp("", False, "at") == ("", False)
    with pytest_raises(InvalidRequestError, match="at must not be empty when at_available is true"):
        require_available_utc_timestamp("", True, "at")
    with pytest_raises(InvalidRequestError, match="at must be empty when at_available is false"):
        require_available_utc_timestamp("2026-09-28T12:00:00Z", False, "at")
    with pytest_raises(InvalidRequestError, match="at_available must be a boolean"):
        require_available_utc_timestamp("", "no", "at")
