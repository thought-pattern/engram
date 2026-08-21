"""Exact temporal-query contracts and conservative Section 9 parsing."""

import math
import re
from collections.abc import Mapping
from datetime import UTC, date, datetime, time, timedelta
from typing import TypedDict

from engram.constants import (
    MAX_TEMPORAL_SOURCE_BYTES,
    TEMPORAL_QUERY_FIELDS,
    TEMPORAL_QUERY_SCHEMA_VERSION,
    TemporalAxis,
    TemporalQueryOperator,
)
from engram.errors import InvalidRequestError

TemporalQuery = TypedDict(
    "TemporalQuery",
    {
        "schema_version": int,
        "operator": TemporalQueryOperator,
        "axis": TemporalAxis,
        "source_text": str,
        "start": str,
        "start_available": bool,
        "end": str,
        "end_available": bool,
        "confidence": float,
        "resolved": bool,
    },
)

_DATE_TOKEN = r"(?:\d{4}-\d{2}-\d{2}|\d{4})"
_BETWEEN_RE = re.compile(rf"\bbetween\s+({_DATE_TOKEN})\s+(?:and|to)\s+({_DATE_TOKEN})\b", re.IGNORECASE)
_AS_OF_RE = re.compile(rf"\b(?:as\s+of|as\s+(?:known|recorded)\s+(?:on|at))\s+({_DATE_TOKEN})\b", re.IGNORECASE)
_IN_YEAR_RE = re.compile(r"\b(?:in|during)\s+(?:the\s+)?(?:year\s+)?(\d{4})\b", re.IGNORECASE)
_BEFORE_RE = re.compile(rf"\bbefore\s+({_DATE_TOKEN})\b", re.IGNORECASE)
_AFTER_RE = re.compile(rf"\bafter\s+({_DATE_TOKEN})\b", re.IGNORECASE)
_LATEST_RE = re.compile(r"\b(?:latest|most\s+recent)\b", re.IGNORECASE)
_CURRENT_RE = re.compile(r"\b(?:current|currently|presently|at\s+present)\b", re.IGNORECASE)
_NOW_RE = re.compile(r"\b(?:now|right\s+now)\b", re.IGNORECASE)
_BARE_YEAR_RE = re.compile(r"^\s*(\d{4})\s*[?!.]?\s*$")
_SYSTEM_AXIS_RE = re.compile(r"\b(?:system\s+time|transaction\s+time|as\s+(?:known|recorded)|recorded)\b", re.IGNORECASE)
_UNRESOLVED_RE = re.compile(
    r"\b(?:as\s+of|as\s+(?:known|recorded)\s+(?:on|at)|before|after|between|during|latest|most\s+recent|"
    r"current|currently|presently|right\s+now|now)\b[^?.,;]*",
    re.IGNORECASE,
)


def _text(value: object, name: str, maximum_bytes: int, *, allow_empty: bool) -> str:
    if not isinstance(value, str):
        raise InvalidRequestError(f"{name} must be a string")
    if not allow_empty and not value:
        raise InvalidRequestError(f"{name} must not be empty")
    if len(value.encode("utf-8")) > maximum_bytes:
        raise InvalidRequestError(f"{name} exceeds {maximum_bytes} UTF-8 bytes")
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise InvalidRequestError(f"{name} contains a control character")
    return value


def _confidence(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise InvalidRequestError("temporal query confidence must be numeric")
    result = float(value)
    if not math.isfinite(result) or not 0.0 <= result <= 1.0:
        raise InvalidRequestError("temporal query confidence must be finite and from 0 through 1")
    return result


def _timestamp(value: object, available: object, name: str) -> tuple[str, bool]:
    if not isinstance(available, bool):
        raise InvalidRequestError(f"temporal query {name}_available must be a boolean")
    text = _text(value, f"temporal query {name}", 64, allow_empty=not available)
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
) -> TemporalQuery:
    """Build one exact temporal interpretation without inferring missing bounds."""
    if isinstance(schema_version, bool) or not isinstance(schema_version, int) or schema_version != TEMPORAL_QUERY_SCHEMA_VERSION:
        raise InvalidRequestError(f"unsupported temporal query schema_version: {schema_version}")
    if not isinstance(operator, TemporalQueryOperator):
        raise InvalidRequestError("temporal query operator is unsupported")
    if not isinstance(axis, TemporalAxis):
        raise InvalidRequestError("temporal query axis is unsupported")
    if not isinstance(resolved, bool):
        raise InvalidRequestError("temporal query resolved must be a boolean")
    source = _text(source_text, "temporal query source_text", MAX_TEMPORAL_SOURCE_BYTES, allow_empty=True)
    normalized_start, normalized_start_available = _timestamp(start, start_available, "start")
    normalized_end, normalized_end_available = _timestamp(end, end_available, "end")
    normalized_confidence = _confidence(confidence)
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
    if resolved and bounds != expected_bounds[operator]:
        raise InvalidRequestError("resolved temporal query bounds conflict with its operator")
    if not resolved and bounds != (False, False):
        raise InvalidRequestError("unresolved temporal query must not carry normalized bounds")
    if operator == TemporalQueryOperator.UNSPECIFIED:
        if source or normalized_confidence or not resolved:
            raise InvalidRequestError("unspecified temporal query must use concrete empty resolved state")
    elif not source:
        raise InvalidRequestError("explicit temporal query requires preserved source text")
    result: TemporalQuery = {
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


def validate_temporal_query(value: object) -> TemporalQuery:
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


def temporal_query_from_dict(value: object) -> TemporalQuery:
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


def _canonical(value: datetime) -> str:
    result = value.astimezone(UTC).isoformat().replace("+00:00", "Z")
    return result


def _period(value: str) -> tuple[datetime, datetime]:
    if re.fullmatch(r"\d{4}", value):
        year = int(value)
        if not 1 <= year <= 9_998:
            raise ValueError("temporal year is outside the supported range")
        result = datetime(year, 1, 1, tzinfo=UTC), datetime(year + 1, 1, 1, tzinfo=UTC)
        return result
    parsed = date.fromisoformat(value)
    start = datetime.combine(parsed, time.min, tzinfo=UTC)
    result = start, start + timedelta(days=1)
    return result


def _unresolved(operator: TemporalQueryOperator, axis: TemporalAxis, source: str) -> TemporalQuery:
    result = temporal_query(operator=operator, axis=axis, source_text=source, confidence=0.0, resolved=False)
    return result


def parse_temporal_query(request: object) -> TemporalQuery:
    """Parse only explicit supported dates and years; retain uncertain expressions."""
    text = _text(request, "temporal request", 4_096, allow_empty=False)
    axis = TemporalAxis.SYSTEM_TIME if _SYSTEM_AXIS_RE.search(text) else TemporalAxis.VALID_TIME
    candidates: list[tuple[TemporalQueryOperator, re.Match[str]]] = []
    for operator, pattern in (
        (TemporalQueryOperator.BETWEEN, _BETWEEN_RE),
        (TemporalQueryOperator.AS_OF, _AS_OF_RE),
        (TemporalQueryOperator.IN_YEAR, _IN_YEAR_RE),
        (TemporalQueryOperator.BEFORE, _BEFORE_RE),
        (TemporalQueryOperator.AFTER, _AFTER_RE),
        (TemporalQueryOperator.LATEST, _LATEST_RE),
        (TemporalQueryOperator.CURRENT, _CURRENT_RE),
        (TemporalQueryOperator.NOW, _NOW_RE),
    ):
        match = pattern.search(text)
        if match:
            candidates.append((operator, match))
    bare_year = _BARE_YEAR_RE.fullmatch(text)
    if bare_year:
        candidates = [(TemporalQueryOperator.IN_YEAR, bare_year)]
    if not candidates:
        uncertain = _UNRESOLVED_RE.search(text)
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
        result = _unresolved(operator, axis, source)
        return result
    if len(candidates) != 1:
        source = " | ".join(match.group(0) for _, match in candidates)
        result = _unresolved(candidates[0][0], axis, source)
        return result
    operator, match = candidates[0]
    source = match.group(0)
    try:
        if operator == TemporalQueryOperator.BETWEEN:
            first_start, _ = _period(match.group(1))
            _, second_end = _period(match.group(2))
            result = temporal_query(
                operator=operator,
                axis=axis,
                source_text=source,
                start=_canonical(first_start),
                start_available=True,
                end=_canonical(second_end),
                end_available=True,
                confidence=1.0,
            )
            return result
        if operator in {TemporalQueryOperator.AS_OF, TemporalQueryOperator.IN_YEAR}:
            period_start, period_end = _period(match.group(1))
            if operator == TemporalQueryOperator.AS_OF:
                result = temporal_query(
                    operator=operator,
                    axis=axis,
                    source_text=source,
                    start=_canonical(period_end - timedelta(microseconds=1)),
                    start_available=True,
                    confidence=1.0,
                )
                return result
            result = temporal_query(
                operator=operator,
                axis=axis,
                source_text=source,
                start=_canonical(period_start),
                start_available=True,
                end=_canonical(period_end),
                end_available=True,
                confidence=1.0,
            )
            return result
        if operator == TemporalQueryOperator.BEFORE:
            period_start, _ = _period(match.group(1))
            result = temporal_query(
                operator=operator,
                axis=axis,
                source_text=source,
                end=_canonical(period_start),
                end_available=True,
                confidence=1.0,
            )
            return result
        if operator == TemporalQueryOperator.AFTER:
            _, period_end = _period(match.group(1))
            result = temporal_query(
                operator=operator,
                axis=axis,
                source_text=source,
                start=_canonical(period_end),
                start_available=True,
                confidence=1.0,
            )
            return result
    except (OverflowError, ValueError):
        result = _unresolved(operator, axis, source)
        return result
    result = temporal_query(operator=operator, axis=axis, source_text=source, confidence=1.0)
    return result
