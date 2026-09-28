"""Exact temporal-query contracts and conservative Section 9 parsing."""

from collections.abc import Mapping
from datetime import UTC, date, datetime, time, timedelta
from math import isfinite as math_isfinite
from re import IGNORECASE as IGNORECASE, Match as re_Match, compile as re_compile, fullmatch as re_fullmatch

from engram.constants import (
    MAX_TEMPORAL_SOURCE_BYTES,
    TEMPORAL_QUERY_FIELDS,
    TEMPORAL_QUERY_SCHEMA_VERSION,
    TemporalAxis,
    TemporalQueryOperator,
)
from engram.errors import InvalidRequestError

DATE_TOKEN = r"(?:\d{4}-\d{2}-\d{2}|\d{4})"
BETWEEN_RE = re_compile(rf"\bbetween\s+({DATE_TOKEN})\s+(?:and|to)\s+({DATE_TOKEN})\b", IGNORECASE)
AS_OF_RE = re_compile(rf"\b(?:as\s+of|as\s+(?:known|recorded)\s+(?:on|at))\s+({DATE_TOKEN})\b", IGNORECASE)
# A year followed by -MM or /MM is part of a full date, not the whole year.
IN_YEAR_RE = re_compile(r"\b(?:in|during)\s+(?:the\s+)?(?:year\s+)?(\d{4})\b(?![-/]\d)", IGNORECASE)
BEFORE_RE = re_compile(rf"\bbefore\s+({DATE_TOKEN})\b", IGNORECASE)
AFTER_RE = re_compile(rf"\bafter\s+({DATE_TOKEN})\b", IGNORECASE)
LATEST_RE = re_compile(r"\b(?:latest|most\s+recent)\b", IGNORECASE)
CURRENT_RE = re_compile(r"\b(?:current|currently|presently|at\s+present)\b", IGNORECASE)
NOW_RE = re_compile(r"\b(?:now|right\s+now)\b", IGNORECASE)
BARE_YEAR_RE = re_compile(r"^\s*(\d{4})\s*[?!.]?\s*$")
SYSTEM_AXIS_RE = re_compile(r"\b(?:system\s+time|transaction\s+time|as\s+(?:known|recorded)|recorded)\b", IGNORECASE)
WHITESPACE_CONTROL_RE = re_compile(r"[\t\n\r\v\f]")
# A bare four-digit number is a year only in this range and when no unit or
# technical count word follows it: "in 1500 byte frames" and "after 2000
# requests" are quantities. Full dates (YYYY-MM-DD) are always dates. Words
# that often follow a real year ("in 1969 people...") are not listed.
PLAUSIBLE_YEARS = range(1_000, 3_000)
QUANTITY_WORDS = frozenset(
    {
        "bit", "byte", "kb", "kib", "mb", "mib", "gb", "gib", "tb", "tib",
        "ms", "millisecond", "second", "sec", "minute", "min", "hour", "hr",
        "day", "week", "month", "year", "decade", "century",
        "hz", "khz", "mhz", "ghz", "rpm", "fps", "px", "pixel", "dpi",
        "frame", "page", "row", "column", "line", "word", "character", "char",
        "token", "item", "unit", "record", "request", "query", "step",
        "iteration", "retry", "attempt", "file", "entry", "message", "packet",
        "connection", "thread", "node", "copy",
        "percent", "%", "degree", "volt", "watt", "mah", "calorie",
        "dollar", "usd", "eur", "euro", "cent",
        "meter", "metre", "km", "cm", "mm", "mile", "foot", "feet", "ft", "inch",
        "kg", "gram", "lb", "ton", "liter", "litre", "ml",
    }
)  # fmt: skip
FOLLOWING_WORD_RE = re_compile(r"\s*-?\s*([^\W\d_]+|%)")
UNRESOLVED_RE = re_compile(
    r"\b(?:as\s+of|as\s+(?:known|recorded)\s+(?:on|at)|before|after|between|during|latest|most\s+recent|"
    r"current|currently|presently|right\s+now|now)\b[^?.,;]*",
    IGNORECASE,
)


def internal_text(value: object, name: str, maximum_bytes: int, *, allow_empty: bool) -> str:
    if not isinstance(value, str):
        raise InvalidRequestError(f"{name} must be a string")
    if not allow_empty and not value:
        raise InvalidRequestError(f"{name} must not be empty")
    if len(value.encode("utf-8")) > maximum_bytes:
        raise InvalidRequestError(f"{name} exceeds {maximum_bytes} UTF-8 bytes")
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise InvalidRequestError(f"{name} contains a control character")
    return value


def internal_confidence(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise InvalidRequestError("temporal query confidence must be numeric")
    result = float(value)
    if not math_isfinite(result) or not 0.0 <= result <= 1.0:
        raise InvalidRequestError("temporal query confidence must be finite and from 0 through 1")
    return result


def internal_timestamp(value: object, available: object, name: str) -> tuple[str, bool]:
    if not isinstance(available, bool):
        raise InvalidRequestError(f"temporal query {name}_available must be a boolean")
    text = internal_text(value, f"temporal query {name}", 64, allow_empty=not available)
    if not available:
        if text:
            raise InvalidRequestError(f"temporal query {name} must be empty when unavailable")
        result = text, available
        return result
    if not text or not text.endswith("Z"):
        raise InvalidRequestError(f"temporal query {name} must be a canonical UTC timestamp")
    try:
        parsed = datetime.fromisoformat(text[:-1] + "+00:00")
    except ValueError as error:
        raise InvalidRequestError(f"temporal query {name} must be a canonical UTC timestamp") from error
    if parsed.isoformat().replace("+00:00", "Z") != text:
        raise InvalidRequestError(f"temporal query {name} must use canonical RFC 3339 form")
    result = text, available
    return result


def temporal_query(
    operator: object = TemporalQueryOperator.UNSPECIFIED,
    axis: object = TemporalAxis.VALID_TIME,
    source_text: object = "",
    start: object = "",
    start_available: object = False,
    end: object = "",
    end_available: object = False,
    confidence: object = 0.0,
    resolved: object = True,
    schema_version: object = TEMPORAL_QUERY_SCHEMA_VERSION,
) -> dict:
    """Build one exact temporal interpretation without inferring missing bounds."""
    if isinstance(schema_version, bool) or not isinstance(schema_version, int) or schema_version != TEMPORAL_QUERY_SCHEMA_VERSION:
        raise InvalidRequestError(f"unsupported temporal query schema_version: {schema_version}")
    if not isinstance(operator, TemporalQueryOperator):
        raise InvalidRequestError("temporal query operator is unsupported")
    if not isinstance(axis, TemporalAxis):
        raise InvalidRequestError("temporal query axis is unsupported")
    if not isinstance(resolved, bool):
        raise InvalidRequestError("temporal query resolved must be a boolean")
    source = internal_text(source_text, "temporal query source_text", MAX_TEMPORAL_SOURCE_BYTES, allow_empty=True)
    normalized_start, normalized_start_available = internal_timestamp(start, start_available, "start")
    normalized_end, normalized_end_available = internal_timestamp(end, end_available, "end")
    normalized_confidence = internal_confidence(confidence)
    if normalized_start_available and normalized_end_available:
        start_value = datetime.fromisoformat(normalized_start[:-1] + "+00:00")
        end_value = datetime.fromisoformat(normalized_end[:-1] + "+00:00")
        if start_value >= end_value:
            raise InvalidRequestError("temporal query start must be earlier than end")
    bounds = normalized_start_available, normalized_end_available
    expected_bounds = {
        TemporalQueryOperator.UNSPECIFIED: (False, False),
        TemporalQueryOperator.CURRENT: (False, False),
        TemporalQueryOperator.NOW: (False, False),
        TemporalQueryOperator.AS_OF: (True, False),
        TemporalQueryOperator.IN_YEAR: (True, True),
        TemporalQueryOperator.BEFORE: (False, True),
        TemporalQueryOperator.AFTER: (True, False),
        TemporalQueryOperator.BETWEEN: (True, True),
        TemporalQueryOperator.LATEST: (False, False),
    }
    if resolved and bounds != expected_bounds.get(operator, ()):
        raise InvalidRequestError("resolved temporal query bounds conflict with its operator")
    if not resolved and bounds != (False, False):
        raise InvalidRequestError("unresolved temporal query must not carry normalized bounds")
    if operator == TemporalQueryOperator.UNSPECIFIED:
        if source or normalized_confidence or not resolved:
            raise InvalidRequestError("unspecified temporal query must use concrete empty resolved state")
    elif not source:
        raise InvalidRequestError("explicit temporal query requires preserved source text")
    result: dict = {
        "schema_version": TEMPORAL_QUERY_SCHEMA_VERSION,
        "operator": operator,
        "axis": axis,
        "source_text": source,
        "start": normalized_start,
        "start_available": normalized_start_available,
        "end": normalized_end,
        "end_available": normalized_end_available,
        "confidence": normalized_confidence,
        "resolved": resolved,
    }
    return result


def validate_temporal_query(value: object) -> dict:
    if not isinstance(value, Mapping) or set(value) != TEMPORAL_QUERY_FIELDS:
        raise InvalidRequestError("TemporalQuery has invalid fields")
    result = temporal_query(
        operator=value["operator"],
        axis=value["axis"],
        source_text=value["source_text"],
        start=value["start"],
        start_available=value["start_available"],
        end=value["end"],
        end_available=value["end_available"],
        confidence=value["confidence"],
        resolved=value["resolved"],
        schema_version=value["schema_version"],
    )
    return result


def temporal_query_to_dict(value: object) -> dict[str, object]:
    current = validate_temporal_query(value)
    result = {
        "schema_version": current["schema_version"],
        "operator": current["operator"].value,
        "axis": current["axis"].value,
        "source_text": current["source_text"],
        "start": current["start"],
        "start_available": current["start_available"],
        "end": current["end"],
        "end_available": current["end_available"],
        "confidence": current["confidence"],
        "resolved": current["resolved"],
    }
    return result


def temporal_query_from_dict(value: object) -> dict:
    if not isinstance(value, Mapping) or set(value) != TEMPORAL_QUERY_FIELDS:
        raise InvalidRequestError("serialized TemporalQuery has invalid fields")
    try:
        operator = TemporalQueryOperator(str(value["operator"]))
        axis = TemporalAxis(str(value["axis"]))
    except ValueError as error:
        raise InvalidRequestError("serialized TemporalQuery enum is unsupported") from error
    result = temporal_query(
        operator=operator,
        axis=axis,
        source_text=value["source_text"],
        start=value["start"],
        start_available=value["start_available"],
        end=value["end"],
        end_available=value["end_available"],
        confidence=value["confidence"],
        resolved=value["resolved"],
        schema_version=value["schema_version"],
    )
    return result


def internal_canonical(value: datetime) -> str:
    result = value.astimezone(UTC).isoformat().replace("+00:00", "Z")
    return result


def period(value: str) -> tuple[datetime, datetime]:
    if re_fullmatch(r"\d{4}", value):
        year = int(value)
        if not 1 <= year <= 9_998:
            raise ValueError("temporal year is outside the supported range")
        result = datetime(year, 1, 1, tzinfo=UTC), datetime(year + 1, 1, 1, tzinfo=UTC)
        return result
    parsed = date.fromisoformat(value)
    start = datetime.combine(parsed, time.min, tzinfo=UTC)
    result = start, start + timedelta(days=1)
    return result


def bounded_source(text: str) -> str:
    """Keep an unresolved expression within the source_text limit on a character boundary."""
    encoded = text.encode("utf-8")
    if len(encoded) <= MAX_TEMPORAL_SOURCE_BYTES:
        return text
    result = encoded[:MAX_TEMPORAL_SOURCE_BYTES].decode("utf-8", errors="ignore").rstrip()
    return result


def is_quantity(text: str, match: re_Match[str]) -> bool:
    """Return whether a bare four-digit number in a match reads as a quantity, not a year."""
    for group in range(1, match.re.groups + 1):
        value = match.group(group)
        if not value or not re_fullmatch(r"\d{4}", value):
            continue
        if int(value) not in PLAUSIBLE_YEARS:
            return True
        following = FOLLOWING_WORD_RE.match(text, match.end(group))
        if following:
            word = following.group(1).casefold()
            if word in QUANTITY_WORDS or word.removesuffix("s") in QUANTITY_WORDS:
                return True
    return False


def internal_unresolved(operator: TemporalQueryOperator, axis: TemporalAxis, source: str) -> dict:
    # An unresolved expression runs to the next clause mark and can be long.
    # It is kept for diagnostics, so it is shortened rather than rejected.
    result = temporal_query(operator=operator, axis=axis, source_text=bounded_source(source), confidence=0.0, resolved=False)
    return result


def parse_temporal_query(request: object) -> dict:
    """Parse only explicit supported dates and years; retain uncertain expressions.

    Tabs and line breaks are whitespace to the request layers above, so they
    are folded to spaces here. Other control characters are still rejected.
    """
    if isinstance(request, str):
        request = WHITESPACE_CONTROL_RE.sub(" ", request)
    text = internal_text(request, "temporal request", 4_096, allow_empty=False)
    axis = TemporalAxis.SYSTEM_TIME if SYSTEM_AXIS_RE.search(text) else TemporalAxis.VALID_TIME
    candidates: list[tuple[TemporalQueryOperator, re_Match[str]]] = []
    quantity_starts: set[int] = set()
    for operator, pattern in (
        (TemporalQueryOperator.BETWEEN, BETWEEN_RE),
        (TemporalQueryOperator.AS_OF, AS_OF_RE),
        (TemporalQueryOperator.IN_YEAR, IN_YEAR_RE),
        (TemporalQueryOperator.BEFORE, BEFORE_RE),
        (TemporalQueryOperator.AFTER, AFTER_RE),
        (TemporalQueryOperator.LATEST, LATEST_RE),
        (TemporalQueryOperator.CURRENT, CURRENT_RE),
        (TemporalQueryOperator.NOW, NOW_RE),
    ):
        match = pattern.search(text)
        if match and is_quantity(text, match):
            quantity_starts.add(match.start())
        elif match:
            candidates.append((operator, match))
    bare_year = BARE_YEAR_RE.fullmatch(text)
    if bare_year and not is_quantity(text, bare_year):
        candidates = [(TemporalQueryOperator.IN_YEAR, bare_year)]
    if not candidates:
        # A phrase already read as a quantity is not an unresolved date either.
        uncertain = next(
            (found for found in UNRESOLVED_RE.finditer(text) if found.start() not in quantity_starts),
            (),
        )
        if not uncertain:
            result = temporal_query()
            return result
        source = uncertain.group(0).strip()
        normalized = source.casefold()
        operator = TemporalQueryOperator.AS_OF
        for prefix, selected in (
            ("before", TemporalQueryOperator.BEFORE),
            ("after", TemporalQueryOperator.AFTER),
            ("between", TemporalQueryOperator.BETWEEN),
            ("during", TemporalQueryOperator.IN_YEAR),
            ("latest", TemporalQueryOperator.LATEST),
            ("most recent", TemporalQueryOperator.LATEST),
            ("current", TemporalQueryOperator.CURRENT),
            ("present", TemporalQueryOperator.CURRENT),
            ("now", TemporalQueryOperator.NOW),
        ):
            if normalized.startswith(prefix):
                operator = selected
                break
        result = internal_unresolved(operator, axis, source)
        return result
    if len(candidates) != 1:
        source = " | ".join(match.group(0) for _, match in candidates)
        result = internal_unresolved(candidates[0][0], axis, source)
        return result
    operator, match = candidates[0]
    source = match.group(0)
    try:
        if operator == TemporalQueryOperator.BETWEEN:
            first_start, _ = period(match.group(1))
            _, second_end = period(match.group(2))
            result = temporal_query(
                operator=operator,
                axis=axis,
                source_text=source,
                start=internal_canonical(first_start),
                start_available=True,
                end=internal_canonical(second_end),
                end_available=True,
                confidence=1.0,
            )
            return result
        if operator in {TemporalQueryOperator.AS_OF, TemporalQueryOperator.IN_YEAR}:
            period_start, period_end = period(match.group(1))
            if operator == TemporalQueryOperator.AS_OF:
                result = temporal_query(
                    operator=operator,
                    axis=axis,
                    source_text=source,
                    start=internal_canonical(period_end - timedelta(microseconds=1)),
                    start_available=True,
                    confidence=1.0,
                )
                return result
            result = temporal_query(
                operator=operator,
                axis=axis,
                source_text=source,
                start=internal_canonical(period_start),
                start_available=True,
                end=internal_canonical(period_end),
                end_available=True,
                confidence=1.0,
            )
            return result
        if operator == TemporalQueryOperator.BEFORE:
            period_start, _ = period(match.group(1))
            result = temporal_query(
                operator=operator,
                axis=axis,
                source_text=source,
                end=internal_canonical(period_start),
                end_available=True,
                confidence=1.0,
            )
            return result
        if operator == TemporalQueryOperator.AFTER:
            _, period_end = period(match.group(1))
            result = temporal_query(
                operator=operator,
                axis=axis,
                source_text=source,
                start=internal_canonical(period_end),
                start_available=True,
                confidence=1.0,
            )
            return result
    except (OverflowError, ValueError):
        result = internal_unresolved(operator, axis, source)
        return result
    result = temporal_query(operator=operator, axis=axis, source_text=source, confidence=1.0)
    return result
