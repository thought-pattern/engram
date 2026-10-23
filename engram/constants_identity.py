"""Scope, entity, relation and retrieval identity contracts, eligibility context, exact lookup, and identity parsing vocabulary."""

from enum import StrEnum
from re import IGNORECASE, compile as re_compile, escape as re_escape

from engram.constants_language import DEFAULT_CONTRACTIONS

# A serialized enum value of the identity, frame and resolution contracts.
MAX_ENUM_WIRE_BYTES = 32
MAX_NAMESPACE_BYTES = 128
MAX_CONTEXT_FINGERPRINT_BYTES = 512
MAX_CANONICAL_FORM_BYTES = 4_096
MAX_IDENTITY_SURFACE_BYTES = 512
MAX_CANONICAL_ID_BYTES = 256
MAX_QUALIFIER_VALUE_BYTES = 512
MAX_LEXICAL_TERM_BYTES = 256
MAX_ENTITIES = 32
MAX_QUALIFIERS = 32
MAX_LEXICAL_TERMS = 128
MAX_RETRIEVAL_REPRESENTATION_BYTES = 4_096
MAX_RETRIEVAL_ALIASES = 32
SCOPE_KEY_FIELDS = set({"namespace", "context_fingerprint"})
SCOPED_RETRIEVAL_KEY_FIELDS = set({"scope", "normalized_key"})
EMPTY_SCOPE_KEY = {"namespace": "", "context_fingerprint": ""}
ENTITY_REFERENCE_FIELDS = set({"surface", "canonical_id"})
RELATION_REFERENCE_FIELDS = set({"surface", "canonical_id"})
IDENTITY_QUALIFIER_FIELDS = set({"kind", "value"})
RETRIEVAL_REPRESENTATION_FIELDS = set({"canonical", "aliases"})
QUERY_IDENTITY_FIELDS = set({"canonical_form", "operator", "entities", "relation", "qualifiers", "lexical_terms", "scope"})
EMPTY_RELATION_REFERENCE = {"surface": "", "canonical_id": ""}
ELIGIBILITY_CONTEXT_FIELDS = set({"evaluation_time", "evaluation_time_available", "namespace", "artifact_repository_available"})
ELIGIBILITY_DECISION_FIELDS = set(
    {
        "statement_id",
        "generation",
        "lifecycle_base_eligible",
        "direct_answer_eligible",
        "exclusion_reason",
        "evaluation_time",
        "evaluation_time_available",
        "namespace",
        "artifact_repository_available",
    }
)
MAX_ELIGIBILITY_STATEMENT_ID_BYTES = 256
MAX_ELIGIBILITY_CONTEXT_SIGNATURE_BYTES = 4_096
MAX_EXACT_LOOKUP_STATEMENT_ID_BYTES = 256
MAX_EXACT_LOOKUP_OWNERS = 1_000
EXACT_LOOKUP_RESULT_FIELDS = set(
    {"outcome", "key", "statement_id", "generation", "provenance", "representation", "owner_statement_ids", "truncated"}
)
MAX_EXACT_LOOKUP_PROVENANCE_BYTES = 32
MAX_EXACT_LOOKUP_REPRESENTATION_BYTES = 1_024


class ExactLookupOutcome(StrEnum):
    """The complete set of exact lookup outcomes."""

    FOUND = "FOUND"
    MISS = "MISS"
    COLLISION = "COLLISION"


class QueryOperator(StrEnum):
    """Closed vocabulary for the operation requested by one query."""

    WHO = "who"
    WHAT = "what"
    WHERE = "where"
    WHEN = "when"
    WHICH = "which"
    WHY = "why"
    HOW = "how"
    HOW_MANY = "how_many"
    LOOKUP = "lookup"
    EXISTS = "exists"
    COUNT = "count"
    COMPARE = "compare"
    UNKNOWN = "unknown"


class QualifierKind(StrEnum):
    """Identity-bearing qualifier categories recognized by the first builder."""

    NEGATION = "negation"
    QUANTITY = "quantity"
    COMPARISON = "comparison"
    TEMPORAL = "temporal"
    LOCATION = "location"
    CURRENT = "current"
    HISTORICAL = "historical"


class RetrievalOrigin(StrEnum):
    """The representation that produced one scoped retrieval key."""

    CANONICAL = "canonical"
    ALIAS = "alias"


IDENTITY_CANONICAL_ID_RE = re_compile(r"^[A-Za-z][A-Za-z0-9+.-]*:[^\s\x00-\x1f\x7f]+$")
IDENTITY_PUNCTUATION_TRANSLATION = str.maketrans(
    {
        "\u2018": "'",
        "\u2019": "'",
        "\u201b": "'",
        "\u02bc": "'",
        "\uff07": "'",
        "\u2010": " ",
        "\u2011": " ",
        "\u2012": " ",
        "\u2013": " ",
        "\u2014": " ",
        "\u2212": "-",
    }
)
IDENTITY_CONTRACTION_RE = re_compile(
    r"(?<!\w)(?:" + "|".join(re_escape(key) for key in sorted(DEFAULT_CONTRACTIONS, key=len, reverse=True)) + r")(?!\w)"
)
IDENTITY_TECHNICAL_PUNCTUATION = set("._:/\\-+#@'<>=%|&*$")
IDENTITY_OPERATOR_TOKENS = set({"who", "what", "where", "when", "which", "why", "how", "many"})
IDENTITY_AUXILIARIES = set(
    {
        "am",
        "is",
        "are",
        "was",
        "were",
        "be",
        "been",
        "being",
        "do",
        "does",
        "did",
        "has",
        "have",
        "had",
        "can",
        "could",
        "will",
        "would",
        "should",
        "may",
        "might",
        "must",
    }
)
IDENTITY_DIRECT_LOOKUP_LEADS = ("find ", "lookup ", "look up ", "tell me about ", "show me ")
IDENTITY_NEGATION_TERMS = set({"not", "no", "never", "without", "neither", "nor"})
IDENTITY_CURRENT_TERMS = set({"current", "currently", "latest", "now", "today", "present"})
IDENTITY_HISTORICAL_TERMS = set({"historical", "historically", "previous", "previously", "former", "formerly", "past"})
IDENTITY_COMPARISON_PHRASES = (
    "compare",
    "compared with",
    "compared to",
    "versus",
    " vs ",
    "more than",
    "less than",
    "greater than",
    "fewer than",
    "oldest",
    "newest",
)
IDENTITY_COMPARISON_OPERATOR_RE = re_compile(r"(?<![<>=!])(?:<=|>=|==|!=|<|>)(?![<>=])")
IDENTITY_LOCATION_TERMS = set({"near", "nearby", "within", "inside", "outside"})
IDENTITY_RELATION_HINTS = set(
    {
        "acquire",
        "acquired",
        "born",
        "created",
        "developed",
        "founded",
        "invented",
        "located",
        "made",
        "maintains",
        "owns",
        "released",
        "support",
        "supports",
        "use",
        "uses",
        "wrote",
    }
)
IDENTITY_ENTITY_EXCLUDED_WORDS = set(
    {
        *IDENTITY_OPERATOR_TOKENS,
        *IDENTITY_AUXILIARIES,
        "a",
        "an",
        "the",
        "compare",
        "count",
        "find",
        "lookup",
        "show",
        "tell",
        "and",
        "also",
        "then",
        "instead",
        "current",
        "latest",
        "historical",
    }
)
IDENTITY_TITLE_SEQUENCE_RE = re_compile(r"(?<![\w])(?:[A-Z][\w'’+#.-]*)(?:\s+(?:[A-Z][\w'’+#.-]*)){0,4}")
IDENTITY_QUOTED_SPAN_RE = re_compile(r"[\"“]([^\"”]{1,512})[\"”]")
IDENTITY_TECHNICAL_PATTERNS = (
    re_compile(r"(?<!\w)(?:RFC|ISO|IEC|IEEE|ECMA|PEP)\s*[-:]?\s*\d+(?:[.-]\d+)*(?!\w)", IGNORECASE),
    re_compile(r"(?<!\w)v?\d+(?:\.\d+){1,}(?!\w)", IGNORECASE),
    re_compile(r"(?<!\w)[A-Z][A-Z0-9]+(?:[-_][A-Z0-9]+)+(?!\w)"),
    re_compile(r"(?<!\w)[A-Za-z][A-Za-z0-9]*(?:\+\+|#)(?!\w)"),
    re_compile(r"(?<!\w)@[A-Za-z0-9_.-]+"),
    re_compile(r"(?<!\w)[A-Za-z][A-Za-z0-9_-]*(?:[.:/][A-Za-z0-9][A-Za-z0-9_-]*)+(?!\w)"),
)
