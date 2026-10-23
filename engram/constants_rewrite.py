"""Retrieval-only symbolic rewrite corpus limits, engine defaults, contract field sets and outcomes."""

from enum import StrEnum

REWRITE_TRACE_STEP_FIELDS = set({"rule_id", "input_text", "output_text"})

# Retrieval-only symbolic rewrite corpus limits, engine defaults and contract field sets.
MAX_REWRITE_RULES = 256
MAX_REWRITE_RULE_ID_BYTES = 96
MAX_REWRITE_PATTERN_BYTES = 512
MAX_REWRITE_PROVENANCE_BYTES = 512
MAX_REWRITE_LICENSE_BYTES = 96
MAX_REWRITE_CATEGORY_BYTES = 64
MAX_REWRITE_CORPUS_ID_BYTES = 128
MAX_REWRITE_RULE_TOKENS = 512
MAX_REWRITE_PRIORITY = 10_000
MAX_REWRITE_APPLICATIONS_PER_RULE = 8
DEFAULT_REWRITE_MAX_DEPTH = 8
DEFAULT_REWRITE_MAX_EXPANSIONS = 16
DEFAULT_REWRITE_MAX_ELAPSED_NS = 50_000_000
MAX_REWRITE_ELAPSED_NS = 10_000_000_000
REWRITE_RULE_FIELDS = set(
    {"rule_id", "category", "input_constraints", "output_template", "priority", "scope", "max_applications", "provenance"}
)
REWRITE_INPUT_FIELDS = set(
    {"match_mode", "pattern", "min_tokens", "max_tokens", "required_operators", "requires_inherited_subject"}
)
REWRITE_PROVENANCE_FIELDS = set({"author", "origin", "license", "created_at"})
REWRITE_CORPUS_FIELDS = set({"corpus_id", "rules"})
REWRITE_SAFE_TEMPLATE_FIELDS = set({"subject"})


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
