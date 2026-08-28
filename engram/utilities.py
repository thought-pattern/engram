"""Allow-listed deterministic utility operations.

The module deliberately accepts a small command grammar.  It does not import
plugins dynamically, execute Python expressions, or consult ambient time.
"""

import re
import uuid
from collections.abc import Callable
from datetime import date, datetime, timedelta
from decimal import Decimal, DecimalException, localcontext
from importlib.metadata import version as package_version
from importlib.resources import files
from types import MappingProxyType
from zoneinfo import ZoneInfo

from engram.constants import (
    UTILITY_CONTRACT_VERSION,
    UTILITY_MAX_ABSOLUTE_EXPONENT,
    UTILITY_MAX_COLLECTION_ITEM_BYTES,
    UTILITY_MAX_COLLECTION_ITEMS,
    UTILITY_MAX_INPUT_BYTES,
    UTILITY_MAX_NESTING,
    UTILITY_MAX_NUMERIC_DIGITS,
    UTILITY_MAX_OPERATIONS,
    UTILITY_MAX_OUTPUT_BYTES,
    UTILITY_MAX_POWER,
    UTILITY_MAX_TOKENS,
    UTILITY_NUMERIC_PRECISION_DIGITS,
    UTILITY_PLUGIN_NAMES,
    UTILITY_UNIT_PRECISION_DIGITS,
)

UtilityConfig = dict
UtilityEvaluation = dict

UTILITY_PLUGIN_VERSION = "1.0.0"
UTILITY_TZDATA_VERSION = package_version("tzdata")
UTILITY_ALLOWED_TIMEZONES = {
    "UTC",
    "America/New_York",
    "America/Los_Angeles",
    "Europe/London",
    "Asia/Tokyo",
}
UTILITY_TIMEZONE_LOOKUP = MappingProxyType({name.casefold(): name for name in UTILITY_ALLOWED_TIMEZONES})
UTILITY_ITEM_RE = re.compile(r"[A-Za-z0-9_.:-]+\Z")
UTILITY_SEMVER_RE = re.compile(
    r"(?P<major>0|[1-9][0-9]*)\.(?P<minor>0|[1-9][0-9]*)\.(?P<patch>0|[1-9][0-9]*)"
    r"(?:-(?P<pre>(?:0|[1-9][0-9]*|[0-9]*[A-Za-z-][0-9A-Za-z-]*)(?:\.(?:0|[1-9][0-9]*|[0-9]*[A-Za-z-][0-9A-Za-z-]*))*))?"
    r"(?:\+(?P<build>[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?\Z"
)
UTILITY_UUID_RE = re.compile(r"(?:[0-9A-Fa-f]{32}|[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12})\Z")
UTILITY_SLUG_RE = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*\Z")
UTILITY_RFC3339_RE = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,6})?(?:Z|[+-][0-9]{2}:[0-9]{2})\Z",
    re.IGNORECASE,
)


class UtilityInputError(ValueError):
    """Stable rejection raised only inside a matched built-in grammar."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def utility_config(enabled: bool = False, plugins=UTILITY_PLUGIN_NAMES) -> UtilityConfig:
    """Build the small utility-resolver configuration boundary."""
    if not isinstance(enabled, bool):
        raise ValueError("utility enabled must be a boolean")
    if not isinstance(plugins, (list, tuple)) or not all(isinstance(name, str) and name for name in plugins):
        raise ValueError("utility plugins must be a list or tuple of non-empty names")
    selected = tuple(plugins)
    if len(set(selected)) != len(selected):
        raise ValueError("utility plugins must not contain duplicates")
    unknown = set(selected).difference(UTILITY_PLUGIN_NAMES)
    if unknown:
        raise ValueError(f"unknown utility plugins: {sorted(unknown)}")
    if enabled and not selected:
        raise ValueError("enabled utility resolution requires at least one plugin")
    return {"enabled": enabled, "plugins": selected}


def _contract(name: str, input_schema: str, result_schema: str, errors: tuple[str, ...]) -> dict:
    return {
        "contract_version": UTILITY_CONTRACT_VERSION,
        "name": name,
        "version": UTILITY_PLUGIN_VERSION,
        "accepted_frame_types": ("direct_request",),
        "input_schema": input_schema,
        "bounds": {
            "max_input_bytes": UTILITY_MAX_INPUT_BYTES,
            "max_output_bytes": UTILITY_MAX_OUTPUT_BYTES,
            "max_tokens": UTILITY_MAX_TOKENS,
            "max_operations": UTILITY_MAX_OPERATIONS,
            "max_nesting": UTILITY_MAX_NESTING,
            "numeric_precision_digits": UTILITY_NUMERIC_PRECISION_DIGITS,
        },
        "deterministic_result": result_schema,
        "evidence": "candidate provenance only; no Proposition or learned-response evidence",
        "errors": errors,
        "health": "ready when the built-in plugin is configured and the utility resolver is enabled",
    }


UTILITY_PLUGIN_CONTRACTS = MappingProxyType(
    {
        "arithmetic_v1": _contract(
            "arithmetic_v1",
            "calculate|arithmetic followed by decimal literals, + - * / % **, and parentheses",
            "canonical decimal text",
            ("arithmetic_syntax", "arithmetic_domain", "operation_limit", "numeric_limit"),
        ),
        "boolean_v1": _contract(
            "boolean_v1",
            "boolean followed by true|false, not, and, xor, or, and parentheses",
            "lowercase true or false",
            ("boolean_syntax", "operation_limit"),
        ),
        "set_v1": _contract(
            "set_v1",
            "set union|intersection|difference|symmetric difference {items} and {items}",
            "unique items sorted by Unicode code point in braces",
            ("set_syntax", "collection_limit", "collection_item_invalid"),
        ),
        "date_time_v1": _contract(
            "date_time_v1",
            "ISO Gregorian date arithmetic, days between dates, or aware RFC3339 timestamp conversion",
            "ISO 8601 date, integer days, or timestamp preserving its fractional-second value with target zone",
            ("date_time_syntax", "date_time_domain", "timezone_not_allowed"),
        ),
        "unit_conversion_v1": _contract(
            "unit_conversion_v1",
            "convert <decimal> <allow-listed unit> to <same-dimension unit>",
            "canonical decimal and canonical target unit",
            ("unit_syntax", "unit_unknown", "dimension_mismatch", "numeric_limit"),
        ),
        "version_v1": _contract(
            "version_v1",
            "compare version <SemVer 2.0.0> and|to|with <SemVer 2.0.0>",
            "left version, one of < = >, and right version; build metadata does not affect precedence",
            ("version_syntax", "version_limit"),
        ),
        "identifier_v1": _contract(
            "identifier_v1",
            "validate uuid|slug <bounded ASCII identifier>",
            "valid/invalid label and canonical identifier when valid",
            ("identifier_syntax", "identifier_limit"),
        ),
    }
)


def utility_plugin_contracts() -> tuple[dict, ...]:
    """Return isolated descriptions of every executable built-in plugin."""
    return tuple(
        {
            **contract,
            "accepted_frame_types": tuple(contract["accepted_frame_types"]),
            "bounds": dict(contract["bounds"]),
            "errors": tuple(contract["errors"]),
        }
        for contract in (UTILITY_PLUGIN_CONTRACTS[name] for name in UTILITY_PLUGIN_NAMES)
    )


def _numeric_literal(value: str) -> Decimal:
    digits = sum(character.isdigit() for character in value)
    if not digits or digits > UTILITY_MAX_NUMERIC_DIGITS:
        raise UtilityInputError("numeric_limit")
    try:
        result = Decimal(value)
    except DecimalException as error:
        raise UtilityInputError("arithmetic_syntax") from error
    return _checked_decimal(result)


def _checked_decimal(value: Decimal) -> Decimal:
    if not value.is_finite():
        raise UtilityInputError("arithmetic_domain")
    if value and abs(value.adjusted()) > UTILITY_MAX_ABSOLUTE_EXPONENT:
        raise UtilityInputError("numeric_limit")
    return value


def _decimal_text(value: Decimal) -> str:
    checked = _checked_decimal(value)
    if not checked:
        return "0"
    rendered = format(checked, "f")
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    return "0" if rendered in {"-0", "+0", ""} else rendered


def _arithmetic_tokens(expression: str) -> tuple[str, ...]:
    tokens = []
    position = 0
    while position < len(expression):
        match = re.match(r"\s*(\*\*|[()+\-*/%]|(?:\d+(?:\.\d*)?|\.\d+))", expression[position:])
        if not match:
            raise UtilityInputError("arithmetic_syntax")
        tokens.append(match.group(1))
        position += match.end()
        if len(tokens) > UTILITY_MAX_TOKENS:
            raise UtilityInputError("operation_limit")
    if not tokens:
        raise UtilityInputError("arithmetic_syntax")
    return tuple(tokens)


def _arithmetic_atom(tokens: tuple[str, ...], index: int, operations: int, depth: int) -> tuple[Decimal, int, int]:
    if depth > UTILITY_MAX_NESTING:
        raise UtilityInputError("operation_limit")
    if index >= len(tokens):
        raise UtilityInputError("arithmetic_syntax")
    token = tokens[index]
    if token in {"+", "-"}:
        value, next_index, count = _arithmetic_atom(tokens, index + 1, operations + 1, depth + 1)
        if count > UTILITY_MAX_OPERATIONS:
            raise UtilityInputError("operation_limit")
        return (_checked_decimal(-value) if token == "-" else value), next_index, count
    if token == "(":
        value, next_index, count = _arithmetic_expression(tokens, index + 1, operations, depth + 1)
        if next_index >= len(tokens) or tokens[next_index] != ")":
            raise UtilityInputError("arithmetic_syntax")
        return value, next_index + 1, count
    if re.fullmatch(r"(?:\d+(?:\.\d*)?|\.\d+)", token):
        return _numeric_literal(token), index + 1, operations
    raise UtilityInputError("arithmetic_syntax")


def _arithmetic_power(tokens: tuple[str, ...], index: int, operations: int, depth: int) -> tuple[Decimal, int, int]:
    left, index, operations = _arithmetic_atom(tokens, index, operations, depth)
    if index < len(tokens) and tokens[index] == "**":
        right, index, operations = _arithmetic_power(tokens, index + 1, operations + 1, depth + 1)
        if operations > UTILITY_MAX_OPERATIONS or right != right.to_integral_value() or abs(right) > UTILITY_MAX_POWER:
            raise UtilityInputError("operation_limit")
        try:
            left = _checked_decimal(left ** int(right))
        except DecimalException as error:
            raise UtilityInputError("arithmetic_domain") from error
    return left, index, operations


def _arithmetic_term(tokens: tuple[str, ...], index: int, operations: int, depth: int) -> tuple[Decimal, int, int]:
    value, index, operations = _arithmetic_power(tokens, index, operations, depth)
    while index < len(tokens) and tokens[index] in {"*", "/", "%"}:
        operator = tokens[index]
        right, index, operations = _arithmetic_power(tokens, index + 1, operations + 1, depth)
        if operations > UTILITY_MAX_OPERATIONS:
            raise UtilityInputError("operation_limit")
        try:
            if operator == "*":
                value *= right
            elif operator == "/":
                value /= right
            else:
                value %= right
            value = _checked_decimal(value)
        except (DecimalException, ZeroDivisionError) as error:
            raise UtilityInputError("arithmetic_domain") from error
    return value, index, operations


def _arithmetic_expression(tokens: tuple[str, ...], index: int, operations: int, depth: int) -> tuple[Decimal, int, int]:
    value, index, operations = _arithmetic_term(tokens, index, operations, depth)
    while index < len(tokens) and tokens[index] in {"+", "-"}:
        operator = tokens[index]
        right, index, operations = _arithmetic_term(tokens, index + 1, operations + 1, depth)
        if operations > UTILITY_MAX_OPERATIONS:
            raise UtilityInputError("operation_limit")
        value = _checked_decimal(value + right if operator == "+" else value - right)
    return value, index, operations


def _evaluate_arithmetic(text: str) -> tuple[str, str, int]:
    match = re.fullmatch(r"\s*(?:calculate|arithmetic)\s+(.+?)\s*\??\s*", text, re.IGNORECASE)
    if not match:
        raise UtilityInputError("arithmetic_syntax")
    tokens = _arithmetic_tokens(match.group(1))
    with localcontext() as context:
        context.prec = UTILITY_NUMERIC_PRECISION_DIGITS
        try:
            value, index, operations = _arithmetic_expression(tokens, 0, 0, 0)
        except DecimalException as error:
            raise UtilityInputError("arithmetic_domain") from error
    if index != len(tokens):
        raise UtilityInputError("arithmetic_syntax")
    return _decimal_text(value), "calculate " + " ".join(tokens), operations


def _boolean_tokens(expression: str) -> tuple[str, ...]:
    tokens = []
    position = 0
    while position < len(expression):
        match = re.match(
            r"\s*(?:(true|false|and|or|xor|not)(?=\s|\(|\)|$)|(\(|\)))",
            expression[position:],
            re.IGNORECASE,
        )
        if not match:
            raise UtilityInputError("boolean_syntax")
        tokens.append((match.group(1) or match.group(2)).casefold())
        position += match.end()
        if len(tokens) > UTILITY_MAX_TOKENS:
            raise UtilityInputError("operation_limit")
    if not tokens:
        raise UtilityInputError("boolean_syntax")
    return tuple(tokens)


def _boolean_atom(tokens: tuple[str, ...], index: int, operations: int, depth: int) -> tuple[bool, int, int]:
    if depth > UTILITY_MAX_NESTING:
        raise UtilityInputError("operation_limit")
    if index >= len(tokens):
        raise UtilityInputError("boolean_syntax")
    token = tokens[index]
    if token == "not":
        value, next_index, count = _boolean_atom(tokens, index + 1, operations + 1, depth + 1)
        if count > UTILITY_MAX_OPERATIONS:
            raise UtilityInputError("operation_limit")
        return not value, next_index, count
    if token == "(":
        value, next_index, count = _boolean_or(tokens, index + 1, operations, depth + 1)
        if next_index >= len(tokens) or tokens[next_index] != ")":
            raise UtilityInputError("boolean_syntax")
        return value, next_index + 1, count
    if token in {"true", "false"}:
        return token == "true", index + 1, operations
    raise UtilityInputError("boolean_syntax")


def _boolean_binary(
    tokens: tuple[str, ...],
    index: int,
    operations: int,
    depth: int,
    operator: str,
    lower: Callable[..., tuple[bool, int, int]],
) -> tuple[bool, int, int]:
    value, index, operations = lower(tokens, index, operations, depth)
    while index < len(tokens) and tokens[index] == operator:
        right, index, operations = lower(tokens, index + 1, operations + 1, depth)
        if operations > UTILITY_MAX_OPERATIONS:
            raise UtilityInputError("operation_limit")
        if operator == "and":
            value = value and right
        elif operator == "xor":
            value = value != right
        else:
            value = value or right
    return value, index, operations


def _boolean_and(tokens: tuple[str, ...], index: int, operations: int, depth: int) -> tuple[bool, int, int]:
    return _boolean_binary(tokens, index, operations, depth, "and", _boolean_atom)


def _boolean_xor(tokens: tuple[str, ...], index: int, operations: int, depth: int) -> tuple[bool, int, int]:
    return _boolean_binary(tokens, index, operations, depth, "xor", _boolean_and)


def _boolean_or(tokens: tuple[str, ...], index: int, operations: int, depth: int) -> tuple[bool, int, int]:
    return _boolean_binary(tokens, index, operations, depth, "or", _boolean_xor)


def _evaluate_boolean(text: str) -> tuple[str, str, int]:
    match = re.fullmatch(r"\s*boolean\s+(.+?)\s*\??\s*", text, re.IGNORECASE)
    if not match:
        raise UtilityInputError("boolean_syntax")
    tokens = _boolean_tokens(match.group(1))
    value, index, operations = _boolean_or(tokens, 0, 0, 0)
    if index != len(tokens):
        raise UtilityInputError("boolean_syntax")
    response = "true" if value else "false"
    return response, "boolean " + " ".join(tokens), operations


def _set_items(value: str) -> tuple[str, ...]:
    if not value.strip():
        return ()
    raw = tuple(item.strip() for item in value.split(","))
    if len(raw) > UTILITY_MAX_COLLECTION_ITEMS:
        raise UtilityInputError("collection_limit")
    if any(
        not item or len(item.encode("utf-8")) > UTILITY_MAX_COLLECTION_ITEM_BYTES or not UTILITY_ITEM_RE.fullmatch(item)
        for item in raw
    ):
        raise UtilityInputError("collection_item_invalid")
    return tuple(sorted(set(raw)))


def _set_text(values: tuple[str, ...]) -> str:
    return "{" + ", ".join(values) + "}"


def _evaluate_set(text: str) -> tuple[str, str, int]:
    match = re.fullmatch(
        r"\s*set\s+(union|intersection|difference|symmetric\s+difference)\s+\{([^{}]*)\}\s+(?:and|with)\s+\{([^{}]*)\}\s*\??\s*",
        text,
        re.IGNORECASE,
    )
    if not match:
        raise UtilityInputError("set_syntax")
    operation = " ".join(match.group(1).casefold().split())
    left = _set_items(match.group(2))
    right = _set_items(match.group(3))
    left_values = set(left)
    right_values = set(right)
    if operation == "union":
        output = left_values | right_values
    elif operation == "intersection":
        output = left_values & right_values
    elif operation == "difference":
        output = left_values - right_values
    else:
        output = left_values ^ right_values
    response = _set_text(tuple(sorted(output)))
    canonical = f"set {operation} {_set_text(left)} and {_set_text(right)}"
    return response, canonical, 1


def _iso_date(value: str) -> date:
    try:
        parsed = date.fromisoformat(value)
    except ValueError as error:
        raise UtilityInputError("date_time_domain") from error
    return parsed


def _packaged_zone_info(zone_name: str) -> ZoneInfo:
    """Load one allow-listed zone from the declared tzdata package."""
    resource = files("tzdata.zoneinfo")
    for part in zone_name.split("/"):
        resource = resource.joinpath(part)
    with resource.open("rb") as stream:
        result = ZoneInfo.from_file(stream, key=zone_name)
    return result


def _evaluate_date_time(text: str) -> tuple[str, str, int]:
    arithmetic = re.fullmatch(
        r"\s*date\s+(\d{4}-\d{2}-\d{2})\s+(plus|minus)\s+(\d{1,6})\s+days?\s*\??\s*",
        text,
        re.IGNORECASE,
    )
    if arithmetic:
        source = _iso_date(arithmetic.group(1))
        count = int(arithmetic.group(3))
        if count > 366_000:
            raise UtilityInputError("date_time_domain")
        delta = count if arithmetic.group(2).casefold() == "plus" else -count
        try:
            response = (source + timedelta(days=delta)).isoformat()
        except OverflowError as error:
            raise UtilityInputError("date_time_domain") from error
        return response, f"date {source.isoformat()} {arithmetic.group(2).casefold()} {count} days", 1
    difference = re.fullmatch(
        r"\s*days\s+between\s+(\d{4}-\d{2}-\d{2})\s+and\s+(\d{4}-\d{2}-\d{2})\s*\??\s*",
        text,
        re.IGNORECASE,
    )
    if difference:
        left = _iso_date(difference.group(1))
        right = _iso_date(difference.group(2))
        return str((right - left).days), f"days between {left.isoformat()} and {right.isoformat()}", 1
    conversion = re.fullmatch(r"\s*convert\s+time\s+(\S+)\s+to\s+([A-Za-z_]+(?:/[A-Za-z_]+)?)\s*\??\s*", text, re.IGNORECASE)
    if conversion:
        source_text = conversion.group(1)
        if not UTILITY_RFC3339_RE.fullmatch(source_text):
            raise UtilityInputError("date_time_domain")
        if source_text.endswith(("Z", "z")):
            source_text = source_text[:-1] + "+00:00"
        try:
            source = datetime.fromisoformat(source_text)
        except ValueError as error:
            raise UtilityInputError("date_time_domain") from error
        if source.tzinfo is None or source.utcoffset() is None:
            raise UtilityInputError("date_time_domain")
        requested_zone = conversion.group(2)
        if requested_zone.casefold() not in UTILITY_TIMEZONE_LOOKUP:
            raise UtilityInputError("timezone_not_allowed")
        zone_name = UTILITY_TIMEZONE_LOOKUP[requested_zone.casefold()]
        converted = source.astimezone(_packaged_zone_info(zone_name))
        response = f"{converted.isoformat()}[{zone_name}]"
        return response, f"convert time {source.isoformat()} to {zone_name}", 1
    raise UtilityInputError("date_time_syntax")


UTILITY_UNITS = MappingProxyType(
    {
        "m": ("length", Decimal("1"), Decimal("0"), "m"),
        "km": ("length", Decimal("1000"), Decimal("0"), "km"),
        "cm": ("length", Decimal("0.01"), Decimal("0"), "cm"),
        "mm": ("length", Decimal("0.001"), Decimal("0"), "mm"),
        "in": ("length", Decimal("0.0254"), Decimal("0"), "in"),
        "ft": ("length", Decimal("0.3048"), Decimal("0"), "ft"),
        "yd": ("length", Decimal("0.9144"), Decimal("0"), "yd"),
        "mi": ("length", Decimal("1609.344"), Decimal("0"), "mi"),
        "g": ("mass", Decimal("0.001"), Decimal("0"), "g"),
        "kg": ("mass", Decimal("1"), Decimal("0"), "kg"),
        "lb": ("mass", Decimal("0.45359237"), Decimal("0"), "lb"),
        "oz": ("mass", Decimal("0.028349523125"), Decimal("0"), "oz"),
        "s": ("duration", Decimal("1"), Decimal("0"), "s"),
        "min": ("duration", Decimal("60"), Decimal("0"), "min"),
        "h": ("duration", Decimal("3600"), Decimal("0"), "h"),
        "c": ("temperature", Decimal("1"), Decimal("0"), "C"),
        "f": ("temperature", Decimal("0.5555555555555555555555555555555556"), Decimal("32"), "F"),
        "k": ("temperature", Decimal("1"), Decimal("273.15"), "K"),
    }
)


def _evaluate_unit_conversion(text: str) -> tuple[str, str, int]:
    match = re.fullmatch(
        r"\s*convert\s+([+-]?(?:\d+(?:\.\d*)?|\.\d+))\s+([A-Za-z]+)\s+to\s+([A-Za-z]+)\s*\??\s*",
        text,
        re.IGNORECASE,
    )
    if not match:
        raise UtilityInputError("unit_syntax")
    value = _numeric_literal(match.group(1))
    source_name = match.group(2).casefold()
    target_name = match.group(3).casefold()
    if source_name not in UTILITY_UNITS or target_name not in UTILITY_UNITS:
        raise UtilityInputError("unit_unknown")
    source = UTILITY_UNITS[source_name]
    target = UTILITY_UNITS[target_name]
    if source[0] != target[0]:
        raise UtilityInputError("dimension_mismatch")
    with localcontext() as context:
        context.prec = UTILITY_UNIT_PRECISION_DIGITS
        base = (value - source[2]) * source[1]
        converted = _checked_decimal(base / target[1] + target[2])
    response = f"{_decimal_text(converted)} {target[3]}"
    canonical = f"convert {_decimal_text(value)} {source[3]} to {target[3]}"
    return response, canonical, 1


def _semver(value: str) -> tuple[tuple[int, int, int], tuple[str, ...], str]:
    if len(value.encode("utf-8")) > 256:
        raise UtilityInputError("version_limit")
    match = UTILITY_SEMVER_RE.fullmatch(value)
    if not match:
        raise UtilityInputError("version_syntax")
    core = (int(match.group("major")), int(match.group("minor")), int(match.group("patch")))
    pre = tuple(match.group("pre").split(".")) if match.group("pre") else ()
    return core, pre, value


def _compare_semver(
    left: tuple[tuple[int, int, int], tuple[str, ...], str],
    right: tuple[tuple[int, int, int], tuple[str, ...], str],
) -> int:
    if left[0] != right[0]:
        return -1 if left[0] < right[0] else 1
    if not left[1] and not right[1]:
        return 0
    if not left[1]:
        return 1
    if not right[1]:
        return -1
    for left_item, right_item in zip(left[1], right[1], strict=False):
        if left_item == right_item:
            continue
        left_numeric = left_item.isdigit()
        right_numeric = right_item.isdigit()
        if left_numeric and right_numeric:
            return -1 if int(left_item) < int(right_item) else 1
        if left_numeric != right_numeric:
            return -1 if left_numeric else 1
        return -1 if left_item < right_item else 1
    if len(left[1]) == len(right[1]):
        return 0
    return -1 if len(left[1]) < len(right[1]) else 1


def _evaluate_version(text: str) -> tuple[str, str, int]:
    match = re.fullmatch(r"\s*compare\s+version\s+(\S+)\s+(?:and|to|with)\s+(\S+)\s*\??\s*", text, re.IGNORECASE)
    if not match:
        raise UtilityInputError("version_syntax")
    left = _semver(match.group(1))
    right = _semver(match.group(2))
    comparison = _compare_semver(left, right)
    operator = "<" if comparison < 0 else ">" if comparison > 0 else "="
    response = f"{left[2]} {operator} {right[2]}"
    return response, f"compare version {left[2]} and {right[2]}", 1


def _evaluate_identifier(text: str) -> tuple[str, str, int]:
    match = re.fullmatch(r"\s*validate\s+(uuid|slug)\s+(\S+)\s*\??\s*", text, re.IGNORECASE)
    if not match:
        raise UtilityInputError("identifier_syntax")
    kind = match.group(1).casefold()
    value = match.group(2)
    if len(value.encode("utf-8")) > 256:
        raise UtilityInputError("identifier_limit")
    if kind == "uuid":
        valid = bool(UTILITY_UUID_RE.fullmatch(value))
        canonical = str(uuid.UUID(value)) if valid else value
    else:
        valid = bool(UTILITY_SLUG_RE.fullmatch(value))
        canonical = value
    response = f"valid {kind}: {canonical}" if valid else f"invalid {kind}"
    return response, f"validate {kind} {value}", 1


UTILITY_PREFIXES = MappingProxyType(
    {
        "arithmetic_v1": ("calculate", "arithmetic"),
        "boolean_v1": ("boolean",),
        "set_v1": ("set ",),
        "date_time_v1": ("date ", "days between ", "convert time "),
        "unit_conversion_v1": ("convert ",),
        "version_v1": ("compare version ",),
        "identifier_v1": ("validate uuid ", "validate slug "),
    }
)
UTILITY_EVALUATORS = MappingProxyType(
    {
        "arithmetic_v1": _evaluate_arithmetic,
        "boolean_v1": _evaluate_boolean,
        "set_v1": _evaluate_set,
        "date_time_v1": _evaluate_date_time,
        "unit_conversion_v1": _evaluate_unit_conversion,
        "version_v1": _evaluate_version,
        "identifier_v1": _evaluate_identifier,
    }
)


def _accepts(plugin_name: str, text: str) -> bool:
    lowered = text.strip().casefold()
    return any(lowered == prefix.strip() or lowered.startswith(prefix) for prefix in UTILITY_PREFIXES[plugin_name])


def _evaluation(
    status: str,
    plugin_name: str = "",
    response: str = "",
    canonical_input: str = "",
    error_code: str = "",
    operations: int = 0,
) -> UtilityEvaluation:
    return {
        "status": status,
        "plugin_name": plugin_name,
        "plugin_version": UTILITY_PLUGIN_VERSION if plugin_name else "",
        "contract_version": UTILITY_CONTRACT_VERSION,
        "response": response,
        "canonical_input": canonical_input,
        "error_code": error_code,
        "operations": operations,
    }


def evaluate_named_utility(request: object, plugin_name: object) -> UtilityEvaluation:
    """Evaluate exactly one compiled-in plugin, used for authority rechecks."""
    if not isinstance(request, str) or not isinstance(plugin_name, str) or plugin_name not in UTILITY_EVALUATORS:
        return _evaluation("rejected", error_code="invalid_boundary")
    if len(request.encode("utf-8")) > UTILITY_MAX_INPUT_BYTES:
        return _evaluation("rejected", plugin_name, error_code="input_limit")
    if not _accepts(plugin_name, request):
        return _evaluation("miss")
    try:
        response, canonical_input, operations = UTILITY_EVALUATORS[plugin_name](request)
    except UtilityInputError as error:
        return _evaluation("rejected", plugin_name, error_code=error.code)
    except Exception:
        return _evaluation("failed", plugin_name, error_code="plugin_failure")
    if len(response.encode("utf-8")) > UTILITY_MAX_OUTPUT_BYTES:
        return _evaluation("failed", plugin_name, error_code="output_limit")
    return _evaluation("resolved", plugin_name, response, canonical_input, operations=operations)


class UtilityRegistry:
    """Configured view of the fixed built-in plugin table."""

    def __init__(self, config: object = {}) -> None:
        if not isinstance(config, dict):
            raise ValueError("utility registry config must be an object")
        settings = utility_config() if config == {} else utility_config(**config)
        self.enabled = settings["enabled"]
        self.plugin_names = settings["plugins"]

    def evaluate(self, request: object) -> UtilityEvaluation:
        if not self.enabled:
            return _evaluation("miss")
        if not isinstance(request, str):
            return _evaluation("rejected", error_code="invalid_boundary")
        if len(request.encode("utf-8")) > UTILITY_MAX_INPUT_BYTES:
            return _evaluation("rejected", error_code="input_limit")
        matches = tuple(name for name in self.plugin_names if _accepts(name, request))
        # Prefix overlap is intentionally resolved by the more specific grammar.
        if "date_time_v1" in matches and "unit_conversion_v1" in matches:
            matches = tuple(name for name in matches if name != "unit_conversion_v1")
        if not matches:
            return _evaluation("miss")
        if len(matches) != 1:
            return _evaluation("rejected", error_code="ambiguous_plugin")
        return evaluate_named_utility(request, matches[0])

    def available(self) -> bool:
        """Return readiness without building the detailed health payload."""
        return self.enabled and bool(self.plugin_names)

    def health(self) -> dict:
        plugins = {
            name: {
                "enabled": self.enabled and name in self.plugin_names,
                "ready": self.enabled and name in self.plugin_names,
                "version": UTILITY_PLUGIN_VERSION,
            }
            for name in UTILITY_PLUGIN_NAMES
        }
        plugins["date_time_v1"]["timezone_database_version"] = UTILITY_TZDATA_VERSION
        return {
            "enabled": self.enabled,
            "ready": self.available(),
            "contract_version": UTILITY_CONTRACT_VERSION,
            "plugins": plugins,
        }
