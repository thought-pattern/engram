"""Versioned, bounded, retrieval-only symbolic rewrites."""

import json
import re
import time
import unicodedata
from collections.abc import Callable, Mapping
from enum import StrEnum
from importlib.resources import files

from engram.constants import MAX_REQUEST_BYTES, MAX_TRACE_STEPS, QueryOperator
from engram.errors import InvalidRequestError, RewriteLimitError
from engram.resolution import QueryFrame, query_frame_with_changes, rewrite_trace_step, validate_query_frame

REWRITE_RULE_SCHEMA_VERSION = 1
REWRITE_CORPUS_SCHEMA_VERSION = 1
MAX_REWRITE_RULES = 256
MAX_REWRITE_RULE_ID_BYTES = 96
MAX_REWRITE_PATTERN_BYTES = 512
MAX_REWRITE_PROVENANCE_BYTES = 512
MAX_REWRITE_PRIORITY = 10_000
MAX_REWRITE_APPLICATIONS_PER_RULE = 8
DEFAULT_REWRITE_MAX_DEPTH = 8
DEFAULT_REWRITE_MAX_EXPANSIONS = 16
DEFAULT_REWRITE_MAX_ELAPSED_NS = 50_000_000

_RULE_FIELDS = set(
    {
        "schema_version",
        "rule_id",
        "rule_version",
        "category",
        "input_constraints",
        "output_template",
        "priority",
        "scope",
        "max_applications",
        "provenance",
    }
)
_INPUT_FIELDS = set(
    {
        "match_mode",
        "pattern",
        "min_tokens",
        "max_tokens",
        "required_operators",
        "requires_inherited_subject",
    }
)
_PROVENANCE_FIELDS = set({"author", "origin", "license", "created_at"})
_CORPUS_FIELDS = set({"schema_version", "corpus_id", "corpus_version", "rules"})
_SAFE_TEMPLATE_FIELDS = set({"subject"})
_TOKEN_RE = re.compile(r"[^\W_]+(?:['’][^\W_]+)?", re.UNICODE)


class RewriteMatchMode(StrEnum):
    """Closed matching operations; none can invoke a response template."""

    EXACT = "exact"
    PREFIX = "prefix"
    SUFFIX = "suffix"
    TOKEN_SEQUENCE = "token_sequence"


class RewriteScope(StrEnum):
    """Whether a rule may depend on inherited conversational context."""

    GLOBAL = "global"
    CONTEXTUAL = "contextual"


class RewriteStopReason(StrEnum):
    """Concrete terminal state for one bounded rewrite execution."""

    FIXED_POINT = "fixed_point"
    DEPTH_LIMIT = "depth_limit"
    EXPANSION_LIMIT = "expansion_limit"
    CYCLE = "cycle"
    OUTPUT_LIMIT = "output_limit"
    TIME_LIMIT = "time_limit"


RewriteInputConstraints = dict
RewriteProvenance = dict
RewriteRule = dict
RewriteExecution = dict
RewriteLintFinding = dict


def _mapping(value: object, name: str, fields: set[str]) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or set(value) != fields:
        raise InvalidRequestError(f"{name} has invalid fields")
    return value


def _text(value: object, name: str, maximum: int, *, empty: bool = False) -> str:
    if not isinstance(value, str) or (not empty and not value.strip()) or len(value.encode("utf-8")) > maximum:
        qualifier = "bounded string" if empty else "bounded non-empty string"
        raise InvalidRequestError(f"{name} must be a {qualifier}")
    return value.strip()


def _integer(value: object, name: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise InvalidRequestError(f"{name} must be an integer from {minimum} through {maximum}")
    return value


def _normalized_text(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).replace("’", "'")
    result = " ".join(normalized.split())
    return result


def _tokens(value: str) -> tuple[str, ...]:
    result = tuple(match.group(0).casefold() for match in _TOKEN_RE.finditer(value))
    return result


def rewrite_rule(value: object) -> RewriteRule:
    """Validate and copy one exact version-1 rule record."""
    data = _mapping(value, "RewriteRule", _RULE_FIELDS)
    if data["schema_version"] != REWRITE_RULE_SCHEMA_VERSION:
        raise InvalidRequestError(f"unsupported rewrite rule schema_version: {data['schema_version']}")
    constraint_data = _mapping(data["input_constraints"], "RewriteInputConstraints", _INPUT_FIELDS)
    provenance_data = _mapping(data["provenance"], "RewriteProvenance", _PROVENANCE_FIELDS)
    try:
        mode = RewriteMatchMode(_text(constraint_data["match_mode"], "rewrite match_mode", 32))
        scope = RewriteScope(_text(data["scope"], "rewrite scope", 32))
    except ValueError as error:
        raise InvalidRequestError("rewrite rule uses an unsupported enum value") from error
    raw_operators = constraint_data["required_operators"]
    if not isinstance(raw_operators, (list, tuple)) or len(raw_operators) > len(QueryOperator):
        raise InvalidRequestError("rewrite required_operators must be a bounded list")
    try:
        operators = tuple(QueryOperator(_text(value, "rewrite required operator", 32)) for value in raw_operators)
    except ValueError as error:
        raise InvalidRequestError("rewrite required_operators contains an unsupported operator") from error
    if len(set(operators)) != len(operators):
        raise InvalidRequestError("rewrite required_operators must be unique")
    requires_subject = constraint_data["requires_inherited_subject"]
    if not isinstance(requires_subject, bool):
        raise InvalidRequestError("rewrite requires_inherited_subject must be a boolean")
    if requires_subject and scope != RewriteScope.CONTEXTUAL:
        raise InvalidRequestError("inherited-subject rewrite rules must be contextual")
    template = _text(data["output_template"], "rewrite output_template", MAX_REWRITE_PATTERN_BYTES, empty=True)
    fields = {match.group(1) for match in re.finditer(r"\{([^{}]+)\}", template)}
    if fields.difference(_SAFE_TEMPLATE_FIELDS) or ("{subject}" in template) != requires_subject:
        raise InvalidRequestError("rewrite output_template uses unsupported or inconsistent fields")
    constraint: RewriteInputConstraints = {
        "match_mode": mode,
        "pattern": _text(constraint_data["pattern"], "rewrite pattern", MAX_REWRITE_PATTERN_BYTES),
        "min_tokens": _integer(constraint_data["min_tokens"], "rewrite min_tokens", 1, 512),
        "max_tokens": _integer(constraint_data["max_tokens"], "rewrite max_tokens", 1, 512),
        "required_operators": operators,
        "requires_inherited_subject": requires_subject,
    }
    if constraint["min_tokens"] > constraint["max_tokens"]:
        raise InvalidRequestError("rewrite min_tokens must not exceed max_tokens")
    provenance: RewriteProvenance = {
        "author": _text(provenance_data["author"], "rewrite provenance author", MAX_REWRITE_PROVENANCE_BYTES),
        "origin": _text(provenance_data["origin"], "rewrite provenance origin", MAX_REWRITE_PROVENANCE_BYTES),
        "license": _text(provenance_data["license"], "rewrite provenance license", 96),
        "created_at": _text(provenance_data["created_at"], "rewrite provenance created_at", 40),
    }
    result: RewriteRule = {
        "schema_version": REWRITE_RULE_SCHEMA_VERSION,
        "rule_id": _text(data["rule_id"], "rewrite rule_id", MAX_REWRITE_RULE_ID_BYTES),
        "rule_version": _integer(data["rule_version"], "rewrite rule_version", 1, 1_000_000),
        "category": _text(data["category"], "rewrite category", 64),
        "input_constraints": constraint,
        "output_template": template,
        "priority": _integer(data["priority"], "rewrite priority", 0, MAX_REWRITE_PRIORITY),
        "scope": scope,
        "max_applications": _integer(
            data["max_applications"],
            "rewrite max_applications",
            1,
            MAX_REWRITE_APPLICATIONS_PER_RULE,
        ),
        "provenance": provenance,
    }
    return result


def rewrite_rule_to_dict(value: object) -> dict[str, object]:
    """Serialize one validated rule to plain JSON-ready values."""
    rule = rewrite_rule(value)
    result = {
        "schema_version": rule["schema_version"],
        "rule_id": rule["rule_id"],
        "rule_version": rule["rule_version"],
        "category": rule["category"],
        "input_constraints": {
            "match_mode": rule["input_constraints"]["match_mode"].value,
            "pattern": rule["input_constraints"]["pattern"],
            "min_tokens": rule["input_constraints"]["min_tokens"],
            "max_tokens": rule["input_constraints"]["max_tokens"],
            "required_operators": [value.value for value in rule["input_constraints"]["required_operators"]],
            "requires_inherited_subject": rule["input_constraints"]["requires_inherited_subject"],
        },
        "output_template": rule["output_template"],
        "priority": rule["priority"],
        "scope": rule["scope"].value,
        "max_applications": rule["max_applications"],
        "provenance": dict(rule["provenance"]),
    }
    return result


def load_rewrite_corpus_text(value: str) -> tuple[RewriteRule, ...]:
    """Load one exact corpus document without accepting unknown fields."""
    try:
        decoded = json.loads(value)
    except (TypeError, json.JSONDecodeError) as error:
        raise InvalidRequestError("rewrite corpus must be valid JSON") from error
    data = _mapping(decoded, "RewriteCorpus", _CORPUS_FIELDS)
    if data["schema_version"] != REWRITE_CORPUS_SCHEMA_VERSION:
        raise InvalidRequestError(f"unsupported rewrite corpus schema_version: {data['schema_version']}")
    _text(data["corpus_id"], "rewrite corpus_id", 128)
    _integer(data["corpus_version"], "rewrite corpus_version", 1, 1_000_000)
    raw_rules = data["rules"]
    if not isinstance(raw_rules, list) or not 1 <= len(raw_rules) <= MAX_REWRITE_RULES:
        raise InvalidRequestError(f"rewrite corpus rules must contain 1 through {MAX_REWRITE_RULES} items")
    rules = tuple(rewrite_rule(rule) for rule in raw_rules)
    identities = tuple((rule["rule_id"], rule["rule_version"]) for rule in rules)
    if len(set(identities)) != len(identities):
        raise InvalidRequestError("rewrite corpus contains duplicate rule identity/version pairs")
    result = tuple(sorted(rules, key=lambda rule: (-rule["priority"], rule["rule_id"], rule["rule_version"])))
    return result


def load_default_rewrite_corpus() -> tuple[RewriteRule, ...]:
    """Eagerly load the package-owned, independently authored version-1 corpus."""
    resource = files("engram").joinpath("data/rewrite-rules-v1.json")
    result = load_rewrite_corpus_text(resource.read_text(encoding="utf-8"))
    return result


def _match_span(text: str, rule: RewriteRule) -> tuple[int, int]:
    constraint = rule["input_constraints"]
    pattern = re.escape(_normalized_text(constraint["pattern"]))
    pattern = pattern.replace(r"\ ", r"\s+").replace("'", "['’]")
    mode = constraint["match_mode"]
    if mode == RewriteMatchMode.EXACT:
        expression = rf"^{pattern}[?.!]*$"
    elif mode == RewriteMatchMode.PREFIX:
        expression = rf"^{pattern}(?=\W|$)"
    elif mode == RewriteMatchMode.SUFFIX:
        expression = rf"(?<!\w){pattern}$"
    else:
        expression = rf"(?<!\w){pattern}(?!\w)"
    matched = re.search(expression, text, flags=re.IGNORECASE)
    result = matched.span() if matched else (-1, -1)
    return result


def _candidate_output(text: str, rule: RewriteRule, subject: str) -> str:
    start, end = _match_span(text, rule)
    if start < 0:
        return ""
    replacement = rule["output_template"].replace("{subject}", subject)
    rewritten = f"{text[:start]}{replacement}{text[end:]}"
    rewritten = re.sub(r"\s+([?.!,;:])", r"\1", " ".join(rewritten.split()))
    result = rewritten.strip(" ,;:")
    return result


def _eligible(rule: RewriteRule, text: str, operator: QueryOperator, subject: str, inherited_subject: bool) -> bool:
    constraint = rule["input_constraints"]
    count = len(_tokens(text))
    if not constraint["min_tokens"] <= count <= constraint["max_tokens"]:
        return False
    if constraint["required_operators"] and operator not in constraint["required_operators"]:
        return False
    if rule["scope"] == RewriteScope.CONTEXTUAL and not inherited_subject:
        return False
    if constraint["requires_inherited_subject"] and not subject:
        return False
    return _match_span(text, rule)[0] >= 0


class RewriteEngine:
    """Apply a deterministic linear rewrite chain within fixed resource bounds."""

    def __init__(
        self,
        rules: tuple[RewriteRule, ...],
        *,
        max_depth: int = DEFAULT_REWRITE_MAX_DEPTH,
        max_expansions: int = DEFAULT_REWRITE_MAX_EXPANSIONS,
        max_output_bytes: int = MAX_REQUEST_BYTES,
        max_elapsed_ns: int = DEFAULT_REWRITE_MAX_ELAPSED_NS,
        clock_ns: Callable[[], int] = time.perf_counter_ns,
    ) -> None:
        if not isinstance(rules, tuple):
            raise InvalidRequestError("rewrite rules must be a tuple")
        if not 1 <= len(rules) <= MAX_REWRITE_RULES:
            raise InvalidRequestError(f"rewrite rules must contain 1 through {MAX_REWRITE_RULES} items")
        self.rules = tuple(
            sorted(
                (rewrite_rule(rule) for rule in rules), key=lambda rule: (-rule["priority"], rule["rule_id"], rule["rule_version"])
            )
        )
        rule_identities = tuple((rule["rule_id"], rule["rule_version"]) for rule in self.rules)
        if len(set(rule_identities)) != len(rule_identities):
            raise InvalidRequestError("rewrite rules must have unique identity/version pairs")
        self.max_depth = _integer(max_depth, "rewrite max_depth", 1, MAX_TRACE_STEPS)
        self.max_expansions = _integer(max_expansions, "rewrite max_expansions", 1, MAX_REWRITE_RULES)
        self.max_output_bytes = _integer(max_output_bytes, "rewrite max_output_bytes", 1, MAX_REQUEST_BYTES)
        self.max_elapsed_ns = _integer(max_elapsed_ns, "rewrite max_elapsed_ns", 1, 10_000_000_000)
        if not callable(clock_ns):
            raise InvalidRequestError("rewrite clock_ns must be callable")
        self._clock_ns = clock_ns

    def rewrite(
        self,
        text: str,
        *,
        operator: QueryOperator = QueryOperator.UNKNOWN,
        subject: str = "",
        inherited_subject: bool = False,
        cooperative_check: object = (),
    ) -> RewriteExecution:
        """Return a bounded trace; the result is retrieval data, never an executable pattern."""
        if not isinstance(text, str) or not text or len(text.encode("utf-8")) > MAX_REQUEST_BYTES:
            raise InvalidRequestError("rewrite input must be a bounded non-empty string")
        original = text
        if not isinstance(operator, QueryOperator):
            raise InvalidRequestError("rewrite operator must be a QueryOperator")
        selected_subject = _text(subject, "rewrite subject", MAX_REWRITE_PATTERN_BYTES, empty=True)
        if not isinstance(inherited_subject, bool):
            raise InvalidRequestError("rewrite inherited_subject must be a boolean")
        if cooperative_check != () and not callable(cooperative_check):
            raise InvalidRequestError("rewrite cooperative_check must be callable")
        check = cooperative_check if callable(cooperative_check) else lambda: False
        started = self._clock_ns()
        current = original
        seen = {current.casefold()}
        applications: dict[tuple[str, int], int] = {}
        chain: list[tuple[str, str, str]] = []
        expansions = 0
        stop_reason = RewriteStopReason.FIXED_POINT
        while len(chain) < self.max_depth:
            check()
            if max(0, self._clock_ns() - started) > self.max_elapsed_ns:
                stop_reason = RewriteStopReason.TIME_LIMIT
                break
            candidates = []
            for rule in self.rules:
                identity = (rule["rule_id"], rule["rule_version"])
                if applications.get(identity, 0) >= rule["max_applications"]:
                    continue
                if _eligible(rule, current, operator, selected_subject, inherited_subject):
                    output = _candidate_output(current, rule, selected_subject)
                    if output and output != current:
                        candidates.append((rule, output))
            if not candidates:
                break
            if expansions + len(candidates) > self.max_expansions:
                stop_reason = RewriteStopReason.EXPANSION_LIMIT
                break
            expansions += len(candidates)
            rule, output = candidates[0]
            if len(output.encode("utf-8")) > self.max_output_bytes:
                stop_reason = RewriteStopReason.OUTPUT_LIMIT
                break
            signature = output.casefold()
            if signature in seen:
                stop_reason = RewriteStopReason.CYCLE
                break
            identity = (rule["rule_id"], rule["rule_version"])
            applications[identity] = applications.get(identity, 0) + 1
            chain.append((f"{rule['rule_id']}@{rule['rule_version']}", current, output))
            current = output
            seen.add(signature)
        else:
            stop_reason = RewriteStopReason.DEPTH_LIMIT
        elapsed = max(0, self._clock_ns() - started)
        if stop_reason == RewriteStopReason.FIXED_POINT and elapsed > self.max_elapsed_ns:
            stop_reason = RewriteStopReason.TIME_LIMIT
        result: RewriteExecution = {
            "original_text": original,
            "final_text": current,
            "chain": tuple(chain),
            "stop_reason": stop_reason,
            "expansions": expansions,
            "elapsed_ns": elapsed,
        }
        return result


def apply_rewrites_to_frame(
    value: object,
    engine: RewriteEngine,
    cooperative_check: object = (),
) -> QueryFrame:
    """Populate the reserved QueryFrame trace without changing authoritative identity."""
    frame = validate_query_frame(value)
    inherited_subject = any(item["field_name"] == "subjects" for item in frame["inheritance"])
    subject = frame["identity"]["entities"][0]["surface"] if frame["identity"]["entities"] else ""
    execution = engine.rewrite(
        frame["resolved_text"],
        operator=frame["identity"]["operator"],
        subject=subject,
        inherited_subject=inherited_subject,
        cooperative_check=cooperative_check,
    )
    if execution["stop_reason"] != RewriteStopReason.FIXED_POINT:
        raise RewriteLimitError(f"rewrite stopped at {execution['stop_reason'].value} before reaching a fixed point")
    trace = tuple(rewrite_trace_step(rule_id, input_text, output_text) for rule_id, input_text, output_text in execution["chain"])
    result = query_frame_with_changes(frame, {"resolved_text": execution["final_text"], "rewrite_chain": trace})
    return result


def lint_rewrite_corpus(rules: tuple[RewriteRule, ...]) -> tuple[RewriteLintFinding, ...]:
    """Detect structural collisions, shadows, cycles, broad rules, and shared outputs."""
    validated = tuple(rewrite_rule(rule) for rule in rules)
    findings: list[RewriteLintFinding] = []
    signatures: dict[tuple[object, ...], RewriteRule] = {}
    outputs: dict[str, list[str]] = {}
    for rule in validated:
        constraint = rule["input_constraints"]
        signature = (
            rule["scope"],
            constraint["match_mode"],
            constraint["pattern"].casefold(),
            constraint["required_operators"],
            constraint["requires_inherited_subject"],
        )
        prior = signatures.get(signature)
        if prior:
            code = "unreachable_rule" if prior["output_template"] == rule["output_template"] else "rule_collision"
            severity = "error"
            findings.append(
                {
                    "severity": severity,
                    "code": code,
                    "rule_ids": (prior["rule_id"], rule["rule_id"]),
                    "detail": "rules share the same effective input constraints",
                }
            )
        else:
            signatures[signature] = rule
        pattern_tokens = _tokens(constraint["pattern"])
        overbroad = (
            rule["category"] != "contractions"
            and rule["scope"] == RewriteScope.GLOBAL
            and (
                (constraint["match_mode"] == RewriteMatchMode.TOKEN_SEQUENCE and len(pattern_tokens) < 2)
                or (constraint["match_mode"] == RewriteMatchMode.PREFIX and len(pattern_tokens) < 3)
                or (constraint["match_mode"] == RewriteMatchMode.EXACT and len(pattern_tokens) < 3)
            )
        )
        if overbroad:
            findings.append(
                {
                    "severity": "error",
                    "code": "overbroad_rule",
                    "rule_ids": (rule["rule_id"],),
                    "detail": "global non-contraction rule has an insufficiently constrained input",
                }
            )
        output_key = rule["output_template"].casefold()
        outputs.setdefault(output_key, []).append(rule["rule_id"])
    for rule_ids in outputs.values():
        if len(rule_ids) > 1:
            findings.append(
                {
                    "severity": "warning",
                    "code": "duplicate_output",
                    "rule_ids": tuple(sorted(rule_ids)),
                    "detail": "multiple rules intentionally or accidentally share one output template",
                }
            )
    cycle_engine = RewriteEngine(validated, max_depth=MAX_TRACE_STEPS, max_expansions=MAX_REWRITE_RULES)
    for rule in validated:
        constraint = rule["input_constraints"]
        if constraint["requires_inherited_subject"]:
            continue
        execution = cycle_engine.rewrite(
            constraint["pattern"],
            operator=constraint["required_operators"][0] if constraint["required_operators"] else QueryOperator.UNKNOWN,
        )
        if execution["stop_reason"] == RewriteStopReason.CYCLE:
            findings.append(
                {
                    "severity": "error",
                    "code": "rewrite_cycle",
                    "rule_ids": tuple(step[0].split("@", maxsplit=1)[0] for step in execution["chain"]),
                    "detail": f"rule input reaches a cycle from {rule['rule_id']}",
                }
            )
    findings.sort(key=lambda finding: (finding["severity"], finding["code"], finding["rule_ids"]))
    return tuple(findings)
