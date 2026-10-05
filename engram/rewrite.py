"""Versioned, bounded, retrieval-only symbolic rewrites."""

from enum import StrEnum
from importlib.resources import files
from json import JSONDecodeError as json_JSONDecodeError, loads as json_loads
from logging import getLogger as logging_getLogger
from re import (
    IGNORECASE as IGNORECASE,
    UNICODE as UNICODE,
    compile as re_compile,
    escape as re_escape,
    finditer as re_finditer,
    search as re_search,
    sub as re_sub,
)
from time import perf_counter_ns as time_perf_counter_ns
from unicodedata import normalize as unicodedata_normalize

from engram.constants import MAX_REQUEST_BYTES, MAX_TRACE_STEPS, QueryOperator
from engram.errors import InvalidRequestError
from engram.resolution import query_frame_with_changes, rewrite_trace_step, validate_query_frame
from engram.validation import require_any_text

logger = logging_getLogger(__name__)
MAX_REWRITE_RULES = 256
MAX_REWRITE_RULE_ID_BYTES = 96
MAX_REWRITE_PATTERN_BYTES = 512
MAX_REWRITE_PROVENANCE_BYTES = 512
MAX_REWRITE_PRIORITY = 10_000
MAX_REWRITE_APPLICATIONS_PER_RULE = 8
DEFAULT_REWRITE_MAX_DEPTH = 8
DEFAULT_REWRITE_MAX_EXPANSIONS = 16
DEFAULT_REWRITE_MAX_ELAPSED_NS = 50_000_000

RULE_FIELDS = set(
    {
        "rule_id",
        "category",
        "input_constraints",
        "output_template",
        "priority",
        "scope",
        "max_applications",
        "provenance",
    }
)
INPUT_FIELDS = set(
    {
        "match_mode",
        "pattern",
        "min_tokens",
        "max_tokens",
        "required_operators",
        "requires_inherited_subject",
    }
)
PROVENANCE_FIELDS = set({"author", "origin", "license", "created_at"})
CORPUS_FIELDS = set({"corpus_id", "rules"})
SAFE_TEMPLATE_FIELDS = set({"subject"})
TOKEN_RE = re_compile(r"[^\W_]+(?:['’][^\W_]+)?", UNICODE)


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


def internal_mapping(value: object, name: str, fields: set[str]) -> dict[str, object]:
    if not isinstance(value, dict) or set(value) != fields:
        raise InvalidRequestError(f"{name} has invalid fields")
    return value


def internal_text(value: object, name: str, maximum: int, *, empty: bool = False) -> str:
    """Validate rewrite corpus text and return it trimmed."""
    result = require_any_text(value, name, maximum, allow_empty=empty, blank_is_empty=True).strip()
    return result


def internal_integer(value: object, name: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise InvalidRequestError(f"{name} must be an integer from {minimum} through {maximum}")
    return value


def normalized_text(value: str) -> str:
    normalized = unicodedata_normalize("NFKC", value).replace("’", "'")
    result = " ".join(normalized.split())
    return result


def internal_tokens(value: str) -> tuple[str, ...]:
    result = tuple(match.group(0).casefold() for match in TOKEN_RE.finditer(value))
    return result


def rule_precedence(rule: dict) -> tuple[int, str]:
    """Order validated rules by descending priority, then by rule identity."""
    result = (-rule.get("priority", 0), rule.get("rule_id", ""))
    return result


def rewrite_rule(value: object) -> dict:
    """Validate and copy one exact version-1 rule record."""
    data = internal_mapping(value, "RewriteRule", RULE_FIELDS)
    constraint_data = internal_mapping(data.get("input_constraints", {}), "RewriteInputConstraints", INPUT_FIELDS)
    provenance_data = internal_mapping(data.get("provenance", {}), "RewriteProvenance", PROVENANCE_FIELDS)
    try:
        mode = RewriteMatchMode(internal_text(constraint_data.get("match_mode", ""), "rewrite match_mode", 32))
        scope = RewriteScope(internal_text(data.get("scope", ""), "rewrite scope", 32))
    except ValueError as error:
        raise InvalidRequestError("rewrite rule uses an unsupported enum value") from error
    raw_operators = constraint_data.get("required_operators", [])
    if not isinstance(raw_operators, (list, tuple)) or len(raw_operators) > len(QueryOperator):
        raise InvalidRequestError("rewrite required_operators must be a bounded list")
    try:
        operators = tuple(QueryOperator(internal_text(value, "rewrite required operator", 32)) for value in raw_operators)
    except ValueError as error:
        raise InvalidRequestError("rewrite required_operators contains an unsupported operator") from error
    if len(set(operators)) != len(operators):
        raise InvalidRequestError("rewrite required_operators must be unique")
    requires_subject = constraint_data.get("requires_inherited_subject", False)
    if not isinstance(requires_subject, bool):
        raise InvalidRequestError("rewrite requires_inherited_subject must be a boolean")
    if requires_subject and scope != RewriteScope.CONTEXTUAL:
        raise InvalidRequestError("inherited-subject rewrite rules must be contextual")
    template = internal_text(data.get("output_template", ""), "rewrite output_template", MAX_REWRITE_PATTERN_BYTES, empty=True)
    fields = {match.group(1) for match in re_finditer(r"\{([^{}]+)\}", template)}
    if fields.difference(SAFE_TEMPLATE_FIELDS) or ("{subject}" in template) != requires_subject:
        raise InvalidRequestError("rewrite output_template uses unsupported or inconsistent fields")
    constraint: dict = {
        "match_mode": mode,
        "pattern": internal_text(constraint_data.get("pattern", ""), "rewrite pattern", MAX_REWRITE_PATTERN_BYTES),
        "min_tokens": internal_integer(constraint_data.get("min_tokens", 0), "rewrite min_tokens", 1, 512),
        "max_tokens": internal_integer(constraint_data.get("max_tokens", 0), "rewrite max_tokens", 1, 512),
        "required_operators": operators,
        "requires_inherited_subject": requires_subject,
    }
    if constraint.get("min_tokens", 0) > constraint.get("max_tokens", 0):
        raise InvalidRequestError("rewrite min_tokens must not exceed max_tokens")
    provenance: dict = {
        "author": internal_text(provenance_data.get("author", ""), "rewrite provenance author", MAX_REWRITE_PROVENANCE_BYTES),
        "origin": internal_text(provenance_data.get("origin", ""), "rewrite provenance origin", MAX_REWRITE_PROVENANCE_BYTES),
        "license": internal_text(provenance_data.get("license", ""), "rewrite provenance license", 96),
        "created_at": internal_text(provenance_data.get("created_at", ""), "rewrite provenance created_at", 40),
    }
    result: dict = {
        "rule_id": internal_text(data.get("rule_id", ""), "rewrite rule_id", MAX_REWRITE_RULE_ID_BYTES),
        "category": internal_text(data.get("category", ""), "rewrite category", 64),
        "input_constraints": constraint,
        "output_template": template,
        "priority": internal_integer(data.get("priority", 0), "rewrite priority", 0, MAX_REWRITE_PRIORITY),
        "scope": scope,
        "max_applications": internal_integer(
            data.get("max_applications", 0),
            "rewrite max_applications",
            1,
            MAX_REWRITE_APPLICATIONS_PER_RULE,
        ),
        "provenance": provenance,
    }
    return result


def load_rewrite_corpus_text(value: str) -> tuple[dict, ...]:
    """Load one exact corpus document without accepting unknown fields."""
    try:
        decoded = json_loads(value)
    except (TypeError, json_JSONDecodeError) as error:
        raise InvalidRequestError("rewrite corpus must be valid JSON") from error
    data = internal_mapping(decoded, "RewriteCorpus", CORPUS_FIELDS)
    internal_text(data.get("corpus_id", ""), "rewrite corpus_id", 128)
    raw_rules = data.get("rules", [])
    if not isinstance(raw_rules, list) or not 1 <= len(raw_rules) <= MAX_REWRITE_RULES:
        raise InvalidRequestError(f"rewrite corpus rules must contain 1 through {MAX_REWRITE_RULES} items")
    rules = tuple(rewrite_rule(rule) for rule in raw_rules)
    identities = tuple(rule.get("rule_id", "") for rule in rules)
    if len(set(identities)) != len(identities):
        raise InvalidRequestError("rewrite corpus contains duplicate rule identity/version pairs")
    result = tuple(sorted(rules, key=rule_precedence))
    return result


def load_default_rewrite_corpus() -> tuple[dict, ...]:
    """Eagerly load the package-owned, independently authored version-1 corpus."""
    resource = files("engram").joinpath("data/rewrite-rules.json")
    result = load_rewrite_corpus_text(resource.read_text(encoding="utf-8"))
    return result


def match_span(text: str, rule: dict) -> tuple[int, int]:
    constraint = rule.get("input_constraints", {})
    pattern = re_escape(normalized_text(constraint.get("pattern", "")))
    pattern = pattern.replace(r"\ ", r"\s+").replace("'", "['’]")
    mode = constraint.get("match_mode", RewriteMatchMode.EXACT)
    if mode == RewriteMatchMode.EXACT:
        expression = rf"^{pattern}[?.!]*$"
    elif mode == RewriteMatchMode.PREFIX:
        expression = rf"^{pattern}(?=\W|$)"
    elif mode == RewriteMatchMode.SUFFIX:
        expression = rf"(?<!\w){pattern}$"
    else:
        expression = rf"(?<!\w){pattern}(?!\w)"
    matched = re_search(expression, text, flags=IGNORECASE)
    result = matched.span() if matched else (-1, -1)
    return result


def candidate_output(text: str, rule: dict, subject: str) -> str:
    start, end = match_span(text, rule)
    if start < 0:
        return ""
    replacement = rule.get("output_template", "").replace("{subject}", subject)
    rewritten = f"{text[:start]}{replacement}{text[end:]}"
    rewritten = re_sub(r"\s+([?.!,;:])", r"\1", " ".join(rewritten.split()))
    result = rewritten.strip(" ,;:")
    return result


def eligible(rule: dict, text: str, operator: QueryOperator, subject: str, inherited_subject: bool) -> bool:
    constraint = rule.get("input_constraints", {})
    count = len(internal_tokens(text))
    if not constraint.get("min_tokens", 0) <= count <= constraint.get("max_tokens", 0):
        return False
    required_operators = constraint.get("required_operators", ())
    if required_operators and operator not in required_operators:
        return False
    if rule.get("scope", RewriteScope.GLOBAL) == RewriteScope.CONTEXTUAL and not inherited_subject:
        return False
    if constraint.get("requires_inherited_subject", False) and not subject:
        return False
    result = match_span(text, rule)[0] >= 0
    return result


class RewriteEngine:
    """Apply a deterministic linear rewrite chain within fixed resource bounds."""

    def __init__(
        self,
        rules: tuple[dict, ...],
        *,
        max_depth: int = DEFAULT_REWRITE_MAX_DEPTH,
        max_expansions: int = DEFAULT_REWRITE_MAX_EXPANSIONS,
        max_output_bytes: int = MAX_REQUEST_BYTES,
        max_elapsed_ns: int = DEFAULT_REWRITE_MAX_ELAPSED_NS,
        clock_ns: object = time_perf_counter_ns,
    ) -> None:
        if not isinstance(rules, tuple):
            raise InvalidRequestError("rewrite rules must be a tuple")
        if not 1 <= len(rules) <= MAX_REWRITE_RULES:
            raise InvalidRequestError(f"rewrite rules must contain 1 through {MAX_REWRITE_RULES} items")
        self.rules = tuple(sorted((rewrite_rule(rule) for rule in rules), key=rule_precedence))
        rule_identities = tuple(rule.get("rule_id", "") for rule in self.rules)
        if len(set(rule_identities)) != len(rule_identities):
            raise InvalidRequestError("rewrite rules must have unique identity/version pairs")
        self.max_depth = internal_integer(max_depth, "rewrite max_depth", 1, MAX_TRACE_STEPS)
        self.max_expansions = internal_integer(max_expansions, "rewrite max_expansions", 1, MAX_REWRITE_RULES)
        self.max_output_bytes = internal_integer(max_output_bytes, "rewrite max_output_bytes", 1, MAX_REQUEST_BYTES)
        self.max_elapsed_ns = internal_integer(max_elapsed_ns, "rewrite max_elapsed_ns", 1, 10_000_000_000)
        if not callable(clock_ns):
            raise InvalidRequestError("rewrite clock_ns must be callable")
        self.internal_clock_ns = clock_ns

    def current_time_ns(self) -> int:
        value = self.internal_clock_ns()
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise InvalidRequestError("rewrite clock_ns must return a non-negative integer")
        return value

    def rewrite(
        self,
        text: str,
        *,
        operator: QueryOperator = QueryOperator.UNKNOWN,
        subject: str = "",
        inherited_subject: bool = False,
        cooperative_check: object = (),
    ) -> dict:
        """Return a bounded trace; the result is retrieval data, never an executable pattern."""
        if not isinstance(text, str) or not text or len(text.encode("utf-8")) > MAX_REQUEST_BYTES:
            raise InvalidRequestError("rewrite input must be a bounded non-empty string")
        original = text
        if not isinstance(operator, QueryOperator):
            raise InvalidRequestError("rewrite operator must be a QueryOperator")
        selected_subject = internal_text(subject, "rewrite subject", MAX_REWRITE_PATTERN_BYTES, empty=True)
        if not isinstance(inherited_subject, bool):
            raise InvalidRequestError("rewrite inherited_subject must be a boolean")
        if cooperative_check != () and not callable(cooperative_check):
            raise InvalidRequestError("rewrite cooperative_check must be callable")
        check = cooperative_check if callable(cooperative_check) else lambda: False
        started = self.current_time_ns()
        current = original
        seen = {current.casefold()}
        applications: dict[tuple[str, int], int] = {}
        chain: list[tuple[str, str, str]] = []
        expansions = 0
        stop_reason = RewriteStopReason.FIXED_POINT
        while len(chain) < self.max_depth:
            check()
            if max(0, self.current_time_ns() - started) > self.max_elapsed_ns:
                stop_reason = RewriteStopReason.TIME_LIMIT
                break
            candidates = []
            for rule in self.rules:
                identity = rule.get("rule_id", "")
                if applications.get(identity, 0) >= rule.get("max_applications", 0):
                    continue
                if eligible(rule, current, operator, selected_subject, inherited_subject):
                    output = candidate_output(current, rule, selected_subject)
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
            identity = rule.get("rule_id", "")
            applications[identity] = applications.get(identity, 0) + 1
            chain.append((identity, current, output))
            current = output
            seen.add(signature)
        else:
            stop_reason = RewriteStopReason.DEPTH_LIMIT
        elapsed = max(0, self.current_time_ns() - started)
        if stop_reason == RewriteStopReason.FIXED_POINT and elapsed > self.max_elapsed_ns:
            stop_reason = RewriteStopReason.TIME_LIMIT
        result: dict = {
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
) -> dict:
    """Populate the reserved QueryFrame trace without changing authoritative identity.

    Only a complete fixed point reaches resolver planning. A chain stopped by
    a depth, expansion, cycle, output, or time limit is discarded and the
    frame keeps its original representation, so an optional rewrite cannot
    fail the request.
    """
    frame = validate_query_frame(value)
    inherited_subject = any(item.get("field_name", "") == "subjects" for item in frame.get("inheritance", ()))
    identity = frame.get("identity", {})
    entities = identity.get("entities", ())
    subject = entities[0].get("surface", "") if entities else ""
    execution = engine.rewrite(
        frame.get("resolved_text", ""),
        operator=identity.get("operator", QueryOperator.UNKNOWN),
        subject=subject,
        inherited_subject=inherited_subject,
        cooperative_check=cooperative_check,
    )
    stop_reason = execution.get("stop_reason", RewriteStopReason.FIXED_POINT)
    if stop_reason != RewriteStopReason.FIXED_POINT:
        logger.info("Rewrite stopped at %s; resolving the original representation", stop_reason.value)
        result = frame
        return result
    trace = tuple(
        rewrite_trace_step(rule_id, input_text, output_text) for rule_id, input_text, output_text in execution.get("chain", ())
    )
    result = query_frame_with_changes(frame, {"resolved_text": execution.get("final_text", ""), "rewrite_chain": trace})
    return result


def rewrite_rule_population(rule: dict) -> tuple[int, int, set, set]:
    """Return the token range, operators, and (inherited, subject present) states ``eligible`` admits."""
    constraint = rule.get("input_constraints", {})
    operators = set(constraint.get("required_operators", ())) or set(QueryOperator)
    inherited_states = (True,) if rule.get("scope", RewriteScope.GLOBAL) == RewriteScope.CONTEXTUAL else (False, True)
    subject_states = (True,) if constraint.get("requires_inherited_subject", False) else (False, True)
    contexts = {(inherited, subject) for inherited in inherited_states for subject in subject_states}
    result = (constraint.get("min_tokens", 0), constraint.get("max_tokens", 0), operators, contexts)
    return result


def rewrite_populations_overlap(first: dict, second: dict) -> bool:
    """Return whether two rules with one match mode and pattern admit a common input."""
    first_min, first_max, first_operators, first_contexts = rewrite_rule_population(first)
    second_min, second_max, second_operators, second_contexts = rewrite_rule_population(second)
    result = (
        max(first_min, second_min) <= min(first_max, second_max)
        and bool(first_operators & second_operators)
        and bool(first_contexts & second_contexts)
    )
    return result


def rewrite_population_covers(outer: dict, inner: dict) -> bool:
    """Return whether ``outer`` admits every input ``inner`` admits under one match mode and pattern."""
    outer_min, outer_max, outer_operators, outer_contexts = rewrite_rule_population(outer)
    inner_min, inner_max, inner_operators, inner_contexts = rewrite_rule_population(inner)
    result = (
        outer_min <= inner_min
        and inner_max <= outer_max
        and inner_operators <= outer_operators
        and inner_contexts <= outer_contexts
    )
    return result


def lint_rewrite_corpus(rules: tuple[dict, ...]) -> tuple[dict, ...]:
    """Detect structural collisions, shadows, cycles, broad rules, and shared outputs.

    Rules with one match mode and pattern are compared by the inputs ``eligible`` admits:
    token count, required operators, scope, and inherited-subject requirement. Different
    outputs over a common input are a collision. A rule whose every input a higher-precedence
    rule with the same output admits is unreachable. Disjoint populations are both reachable.
    """
    validated = tuple(rewrite_rule(rule) for rule in rules)
    findings: list[dict] = []
    patterns: dict[tuple[object, ...], list[dict]] = {}
    outputs: dict[str, list[str]] = {}
    for rule in validated:
        rule_id = rule.get("rule_id", "")
        output_template = rule.get("output_template", "")
        constraint = rule.get("input_constraints", {})
        match_mode = constraint.get("match_mode", RewriteMatchMode.EXACT)
        pattern = constraint.get("pattern", "")
        same_pattern = patterns.setdefault((match_mode, pattern.casefold()), [])
        for prior in same_pattern:
            if not rewrite_populations_overlap(prior, rule):
                continue
            prior_rule_id = prior.get("rule_id", "")
            if prior.get("output_template", "") != output_template:
                findings.append(
                    {
                        "severity": "error",
                        "code": "rule_collision",
                        "rule_ids": (prior_rule_id, rule_id),
                        "detail": "rules admit a common input under one pattern with different outputs",
                    }
                )
                continue
            higher, lower = sorted((prior, rule), key=rule_precedence)
            if rewrite_population_covers(higher, lower):
                higher_rule_id = higher.get("rule_id", "")
                lower_rule_id = lower.get("rule_id", "")
                findings.append(
                    {
                        "severity": "error",
                        "code": "unreachable_rule",
                        "rule_ids": (prior_rule_id, rule_id),
                        "detail": f"{higher_rule_id} has the same output and admits every input of {lower_rule_id} first",
                    }
                )
        same_pattern.append(rule)
        pattern_tokens = internal_tokens(pattern)
        overbroad = (
            rule.get("category", "") != "contractions"
            and rule.get("scope", RewriteScope.GLOBAL) == RewriteScope.GLOBAL
            and (
                (match_mode == RewriteMatchMode.TOKEN_SEQUENCE and len(pattern_tokens) < 2)
                or (match_mode == RewriteMatchMode.PREFIX and len(pattern_tokens) < 3)
                or (match_mode == RewriteMatchMode.EXACT and len(pattern_tokens) < 3)
            )
        )
        if overbroad:
            findings.append(
                {
                    "severity": "error",
                    "code": "overbroad_rule",
                    "rule_ids": (rule_id,),
                    "detail": "global non-contraction rule has an insufficiently constrained input",
                }
            )
        output_key = output_template.casefold()
        outputs.setdefault(output_key, []).append(rule_id)
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
        constraint = rule.get("input_constraints", {})
        if constraint.get("requires_inherited_subject", False):
            continue
        required_operators = constraint.get("required_operators", ())
        execution = cycle_engine.rewrite(
            constraint.get("pattern", ""),
            operator=required_operators[0] if required_operators else QueryOperator.UNKNOWN,
        )
        if execution.get("stop_reason", RewriteStopReason.FIXED_POINT) == RewriteStopReason.CYCLE:
            rule_id = rule.get("rule_id", "")
            findings.append(
                {
                    "severity": "error",
                    "code": "rewrite_cycle",
                    "rule_ids": tuple(step[0].split("@", maxsplit=1)[0] for step in execution.get("chain", ())),
                    "detail": f"rule input reaches a cycle from {rule_id}",
                }
            )
    findings.sort(key=lambda finding: (finding.get("severity", ""), finding.get("code", ""), finding.get("rule_ids", ())))
    result = tuple(findings)
    return result
