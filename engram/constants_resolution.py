"""Resolution budgets, query frame, candidate and resolver result contracts, resolver plans, and telemetry dimensions."""

from enum import StrEnum


class CostClass(StrEnum):
    """Closed cost classes used by deterministic resolver plans."""

    EXACT = "exact"
    CHEAP = "cheap"
    STANDARD = "standard"
    EXPENSIVE = "expensive"


RESOLUTION_BUDGET_FIELDS = set(
    {
        "max_resolvers",
        "max_candidates",
        "max_graph_rows",
        "max_vector_results",
        "max_evidence",
        "max_evidence_bytes",
        "max_output_bytes",
        "max_diagnostic_bytes",
        "max_working_memory_bytes",
        "allowed_cost_classes",
        "started_ns",
    }
)
DEFAULT_RESOLUTION_MAX_RESOLVERS = 8
DEFAULT_RESOLUTION_MAX_CANDIDATES = 10
DEFAULT_RESOLUTION_MAX_GRAPH_ROWS = 100
DEFAULT_RESOLUTION_MAX_VECTOR_RESULTS = 100
DEFAULT_RESOLUTION_MAX_EVIDENCE = 20
DEFAULT_RESOLUTION_MAX_EVIDENCE_BYTES = 32_768
DEFAULT_RESOLUTION_MAX_OUTPUT_BYTES = 65_536
DEFAULT_RESOLUTION_MAX_DIAGNOSTIC_BYTES = 16_384
DEFAULT_RESOLUTION_MAX_WORKING_MEMORY_BYTES = 16_777_216
DEFAULT_RESOLUTION_ALLOWED_COST_CLASSES = tuple(CostClass)
MIN_RESOLUTION_RESOLVERS = 1
MAX_RESOLUTION_RESOLVERS = 64
MIN_RESOLUTION_CANDIDATES = 1
MAX_RESOLUTION_CANDIDATES = 1_000
MAX_RESOLUTION_GRAPH_ROWS = 100_000
# One graph read may materialize no more rows than the largest per-request
# graph-row budget; generic template and recall reads have no narrower owner.
MAX_GRAPH_READ_ROWS = MAX_RESOLUTION_GRAPH_ROWS
MAX_RESOLUTION_VECTOR_RESULTS = 100_000
MAX_RESOLUTION_EVIDENCE = 1_000
MAX_RESOLUTION_EVIDENCE_BYTES = 1_048_576
MIN_RESOLUTION_OUTPUT_BYTES = 4_096
MAX_RESOLUTION_OUTPUT_BYTES = 2_097_152
MAX_RESOLUTION_DIAGNOSTIC_BYTES = 1_048_576
MAX_RESOLUTION_WORKING_MEMORY_BYTES = 1_073_741_824
MAX_RESOURCE_COUNTER = 9_223_372_036_854_775_807
BUDGET_CONSUMPTION_FIELDS = set(
    {
        "elapsed_ns",
        "resolvers",
        "candidates",
        "graph_rows",
        "vector_results",
        "evidence",
        "evidence_bytes",
        "output_bytes",
        "diagnostic_bytes",
        "working_memory_bytes",
        "exhausted_dimensions",
        "measurement_available",
    }
)
MAX_EXHAUSTED_DIMENSIONS = 64
MAX_EXHAUSTED_DIMENSION_BYTES = 64
QUERY_FRAME_FIELDS = set(
    {
        "original_text",
        "resolved_text",
        "identity",
        "expected_object_type",
        "temporal_query",
        "inheritance",
        "rewrite_chain",
        "scope",
        "required_metadata",
        "required_source_label",
        "budget",
        "eligibility_context",
        "diagnostic_id",
    }
)
MAX_REQUIRED_SOURCE_LABEL_BYTES = 256
INHERITANCE_PROVENANCE_FIELDS = set({"field_name", "source_turn"})
MAX_INHERITANCE_FIELD_NAME_BYTES = 64
MAX_INHERITANCE_SOURCE_TURN = 1_000_000
FEATURE_SET_FIELDS = set({"values", "unavailable"})
MAX_FEATURE_NAME_BYTES = 96
# Feature values are bounded to this magnitude in either direction.
MAX_FEATURE_MAGNITUDE = 1_000_000.0
# Key bound of a JSON value frozen into a resolution contract.
MAX_FROZEN_JSON_KEY_BYTES = 128
EVIDENCE_REFERENCE_FIELDS = set({"evidence_id", "resolver", "kind", "scope", "provenance", "diagnostics"})
CANDIDATE_FIELDS = set(
    {
        "candidate_id",
        "statement_id",
        "response",
        "source",
        "features",
        "evidence",
        "scope",
        "lifecycle",
        "provenance",
        "diagnostics",
    }
)
MAX_CANDIDATE_ID_BYTES = 256
ACCOUNTING_OBSERVATION_FIELDS = set({"statement_id", "keywords"})
MAX_ACCOUNTING_KEYWORDS = 256
MAX_ACCOUNTING_KEYWORD_BYTES = 256
RESOLVER_RESULT_FIELDS = set(
    {
        "resolver",
        "state",
        "reason_code",
        "candidates",
        "evidence",
        "proposition_evidence",
        "accounting",
        "diagnostics",
        "consumption",
    }
)
RESOLUTION_RESULT_FIELDS = set(
    {
        "outcome",
        "selected_candidate",
        "selected_candidate_available",
        "response_candidates",
        "evidence",
        "confidence",
        "confidence_available",
        "reason_codes",
        "frame_diagnostics",
        "resolver_results",
        "budget",
        "evidence_package_available",
        "evidence_package",
    }
)
MAX_RESOLUTION_REASON_CODES = 64
MAX_DIAGNOSTIC_ID_BYTES = 256
MAX_RESOLVER_NAME_BYTES = 96
EXACT_RESOLVER_NAME = "exact"
EXACT_RESOLVER_COST_CLASS = CostClass.EXACT
SPARSE_RESOLVER_NAME = "sparse"
SPARSE_RESOLVER_COST_CLASS = CostClass.CHEAP
UTILITY_RESOLVER_NAME = "utility"
UTILITY_RESOLVER_COST_CLASS = CostClass.CHEAP
UTILITY_RESOLVER_PRODUCER = "utility-resolver"
UTILITY_PLUGIN_NAMES = ("arithmetic", "boolean", "set", "date_time", "unit_conversion", "version", "identifier")
UTILITY_MAX_INPUT_BYTES = 4_096
UTILITY_MAX_OUTPUT_BYTES = 2_048
UTILITY_MAX_TOKENS = 128
UTILITY_MAX_OPERATIONS = 32
UTILITY_MAX_NESTING = 16
UTILITY_MAX_NUMERIC_DIGITS = 128
UTILITY_MAX_ABSOLUTE_EXPONENT = 100
UTILITY_MAX_POWER = 12
UTILITY_MAX_COLLECTION_ITEMS = 64
UTILITY_MAX_COLLECTION_ITEM_BYTES = 64
UTILITY_NUMERIC_PRECISION_DIGITS = 34
UTILITY_UNIT_PRECISION_DIGITS = 16
STANDALONE_SEMANTIC_RESOLVER_NAME = "standalone_semantic"
STANDALONE_SEMANTIC_RESOLVER_COST_CLASS = CostClass.EXPENSIVE
STRUCTURED_GRAPH_RESOLVER_NAME = "structured_graph"
STRUCTURED_GRAPH_RESOLVER_COST_CLASS = CostClass.STANDARD
SUPPORT_SEMANTIC_RESOLVER_NAME = "support_semantic"
SUPPORT_SEMANTIC_RESOLVER_COST_CLASS = CostClass.EXPENSIVE
OPERATIONAL_RESOLVER_NAMES = (
    EXACT_RESOLVER_NAME,
    UTILITY_RESOLVER_NAME,
    SPARSE_RESOLVER_NAME,
    STANDALONE_SEMANTIC_RESOLVER_NAME,
    STRUCTURED_GRAPH_RESOLVER_NAME,
    SUPPORT_SEMANTIC_RESOLVER_NAME,
    "other",
)
OPERATIONAL_RESOLVER_STATES = ("completed", "unavailable", "skipped", "exhausted", "failed")
OPERATIONAL_RESOLUTION_OUTCOMES = ("ANSWER", "EVIDENCE", "MISS")
OPERATIONAL_RESOURCE_DIMENSIONS = (
    "resolvers",
    "candidates",
    "graph_rows",
    "vector_results",
    "evidence",
    "evidence_bytes",
    "output_bytes",
    "diagnostic_bytes",
    "working_memory_bytes",
)
OPERATIONAL_EXHAUSTION_DIMENSIONS = (*OPERATIONAL_RESOURCE_DIMENSIONS, "other")
OPERATIONAL_LATENCY_BUCKETS = (
    (1_000_000, "le_1_ms"),
    (10_000_000, "le_10_ms"),
    (100_000_000, "le_100_ms"),
    (1_000_000_000, "le_1_s"),
    (10_000_000_000, "le_10_s"),
)
MAX_REASON_CODE_BYTES = 96
MAX_FEATURES = 64
MAX_TRACE_STEPS = 32
MAX_JSON_DEPTH = 8
MAX_JSON_ITEMS = 4_096
MAX_JSON_STRING_BYTES = 16_384
MAX_JSON_BYTES = 65_536
MAX_RESOLUTION_VALUES = 1_000
MAX_EVIDENCE_ID_BYTES = 256
MAX_PLAN_RESOLVERS = 64
RESOLUTION_PLAN_ENTRY_FIELDS = set({"resolver", "order", "configured", "available", "reason_code"})
RESOLUTION_PLAN_FIELDS = set({"entries"})
ACCOUNTING_FINALIZATION_FIELDS = set(
    {"candidate_statement_ids", "accepted_statement_id", "candidacy_applied", "success_applied", "idempotent"}
)
MAX_ACCOUNTING_VISIBLE_STATEMENT_IDS = 64
RESOLVER_BUDGET_FIELDS = set(
    {
        "max_candidates",
        "max_graph_rows",
        "max_vector_results",
        "max_evidence",
        "max_evidence_bytes",
        "max_output_bytes",
        "max_diagnostic_bytes",
        "max_working_memory_bytes",
    }
)
RESOLVER_RESERVATION_FIELDS = set({"resolver", "order", "lease", "consumption"})
PROPOSITION_EVIDENCE_PRODUCERS = set({STRUCTURED_GRAPH_RESOLVER_NAME, SUPPORT_SEMANTIC_RESOLVER_NAME})


class EvidenceKind(StrEnum):
    """Kinds safe for the minimal Section 4 evidence-reference contract."""

    PROPOSITION = "proposition"
    GRAPH_FACT = "graph_fact"
    SUPPORT = "support"


class CandidateSource(StrEnum):
    """Candidate-producing resolver sources."""

    EXACT = "exact"
    SPARSE = "sparse"
    SUPPORT_SEMANTIC = "support_semantic"
    STANDALONE_SEMANTIC = "standalone_semantic"
    UTILITY = "utility"


class ResolverState(StrEnum):
    """Stable resolver completion vocabulary."""

    COMPLETED = "completed"
    UNAVAILABLE = "unavailable"
    SKIPPED = "skipped"
    EXHAUSTED = "exhausted"
    FAILED = "failed"


class ResolutionOutcome(StrEnum):
    """Closed unified core outcomes."""

    ANSWER = "ANSWER"
    EVIDENCE = "EVIDENCE"
    MISS = "MISS"
