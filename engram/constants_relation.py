"""Contextual frames, temporal queries, canonical relation resolution, and graph composition vocabulary."""

from enum import StrEnum
from re import compile as re_compile

COMPACT_QUERY_FRAME_FIELDS = set(
    {
        "operator",
        "subjects",
        "relation",
        "expected_object_type",
        "temporal_query",
        "qualifiers",
        "source_turn",
        "confidence",
        "topic",
    }
)
MAX_CONTEXTUAL_SUBJECTS = 4
# A conversation's turn counter restarts after this many turns; frames older than
# MAX_CONTEXTUAL_TURN_DISTANCE are never inherited, so a restart only drops context.
MAX_CONTEXTUAL_TURN = 1_000_000
MAX_CONTEXTUAL_TURN_DISTANCE = 2
MIN_CONTEXTUAL_INHERITANCE_CONFIDENCE = 0.55
MAX_CONTEXTUAL_TOPIC_BYTES = 256
TEMPORAL_QUERY_FIELDS = set(
    {"operator", "axis", "source_text", "start", "start_available", "end", "end_available", "confidence", "resolved"}
)
MAX_TEMPORAL_SOURCE_BYTES = 512
MAX_RELATION_SURFACES = 12
MAX_RELATION_CANDIDATES = 8
MAX_RELATION_PLAN_ROWS = 10
MAX_RELATION_LABEL_BYTES = 256
CANONICAL_ENTITY_MATCH_FIELDS = set({"canonical_id", "primary_label", "aliases", "edge_surfaces", "entity_type"})
CANONICAL_PREDICATE_MATCH_FIELDS = set({"canonical_id", "primary_label", "synonyms", "object_type"})
CANONICAL_RESOLUTION_FIELDS = set({"status", "canonical_id", "primary_label", "object_type", "score", "candidate_ids", "evidence"})
COMPOSITION_STEP_FIELDS = set(
    {
        "branch",
        "hop",
        "subject_binding",
        "subject_entity_id",
        "predicate_id",
        "predicate_label",
        "object_binding",
        "expected_object_type",
        "max_candidates",
    }
)
COMPOSITION_PLAN_FIELDS = set(
    {
        "operator",
        "root_entity_id",
        "root_label",
        "steps",
        "terminal_binding",
        "aggregation_inputs",
        "descending",
        "max_hops",
        "max_rows",
        "max_branches",
        "max_candidates_per_step",
        "max_path_propositions",
    }
)
# Fields a Predicate lookup row must carry before composition reads it.
COMPOSITION_PREDICATE_ROW_FIELDS = set({"canonical_id", "primary_label", "object_type"})
MAX_COMPOSITION_HOPS = 2
MAX_COMPOSITION_ROWS = 64
MAX_COMPOSITION_BRANCHES = 4
MAX_COMPOSITION_CANDIDATES_PER_STEP = 8
MAX_COMPOSITION_PATH_PROPOSITIONS = 2
MAX_COMPOSITION_BINDING_BYTES = 64
MAX_COMPOSITION_TEXT_BYTES = 256
# Seconds fraction of a composition DATE label's time component. datetime keeps
# six fractional digits, so ordering compares any further digits separately; the
# lookbehind skips the fractional seconds of a UTC offset.
COMPOSITION_DATE_SECONDS_FRACTION = re_compile(r"(?<![+\-0-9])[0-9]{2}:?[0-9]{2}:?[0-9]{2}[.,]([0-9]+)")
DATETIME_FRACTION_DIGITS = 6
# Producers of deterministic graph candidates and the exact provenance each records, so
# the candidate authority can rebuild and revalidate them against current Proposition state.
RELATION_ONE_HOP_PRODUCER = "relation_one_hop"
GRAPH_COMPOSITION_PRODUCER = "graph_composition"
RELATION_CANDIDATE_PROVENANCE_FIELDS = set(
    {
        "producer",
        "subject_entity_id",
        "subject_label",
        "predicate_id",
        "predicate_label",
        "object_entity_id",
        "object_label",
        "object_type",
        "predicate_cardinality",
        "selection_reason",
        "supplied_trust",
    }
)
COMPOSITION_CANDIDATE_PROVENANCE_FIELDS = set(
    {
        "producer",
        "operator",
        "root_entity_id",
        "root_label",
        "predicate_labels",
        "proposition_ids",
        "identity_chain",
        "trust_chain",
        "terminal_labels",
        "terminal_types",
        "truth_value",
        "truth_available",
        "aggregate_value",
        "aggregate_value_available",
    }
)


class ExpectedObjectType(StrEnum):
    """Base object-type vocabulary populated further by Section 8."""

    UNKNOWN = "UNKNOWN"
    PERSON = "PERSON"
    PLACE = "PLACE"
    DATE = "DATE"
    NUMBER = "NUMBER"
    BOOLEAN = "BOOLEAN"
    ENTITY = "ENTITY"


class CanonicalResolutionStatus(StrEnum):
    """Complete outcomes for canonical entity or predicate resolution."""

    SELECTED = "selected"
    AMBIGUOUS = "ambiguous"
    MISS = "miss"


class RelationPlanTemplate(StrEnum):
    """Allow-listed internal Section 8 query-plan templates."""

    ONE_HOP_PROPOSITION = "one_hop_proposition"


class GraphCompositionOperator(StrEnum):
    """Closed Section 10 graph-algebra operations."""

    LOOKUP = "LOOKUP"
    EXISTS = "EXISTS"
    COUNT = "COUNT"
    AND = "AND"
    OR = "OR"
    NOT = "NOT"
    MIN = "MIN"
    MAX = "MAX"
    ORDER = "ORDER"


class CompositionReason(StrEnum):
    """Stable compiler and execution outcomes for bounded composition."""

    COMPLETE_UNIQUE = "composition_complete_unique"
    COMPLETE_MULTIPLE = "composition_complete_multiple"
    PARTIAL_PATH = "composition_partial_path"
    NO_PATH = "composition_no_path"
    IDENTITY_MISS = "composition_identity_miss"
    IDENTITY_AMBIGUOUS = "composition_identity_ambiguous"
    UNSUPPORTED_QUERY = "composition_unsupported_query"
    UNDERCONSTRAINED = "composition_underconstrained"
    CYCLE = "composition_cycle"
    ROW_LIMIT = "composition_row_limit"
    BRANCH_LIMIT = "composition_branch_limit"
    PATH_LIMIT = "composition_path_limit"
    CANDIDATE_LIMIT = "composition_candidate_limit"
    CANCELLED = "composition_cancelled"
    DEPENDENCY_FAILED = "composition_dependency_failed"
    COMPLETENESS_UNKNOWN = "composition_completeness_unknown"
    CARDINALITY_UNKNOWN = "composition_cardinality_unknown"
    CARDINALITY_CONFLICT = "composition_cardinality_conflict"
    TRUST_UNAVAILABLE = "composition_trust_unavailable"
    TEMPORAL_BOUNDS_OPEN = "composition_temporal_bounds_open"
    TYPE_UNAVAILABLE = "composition_type_unavailable"
    TYPE_MISMATCH = "composition_type_mismatch"
    AGGREGATE_UNSAFE = "composition_aggregate_unsafe"


class PredicateCardinality(StrEnum):
    """Canonical Predicate object-cardinality policy supplied by the graph."""

    UNKNOWN = "UNKNOWN"
    SINGLE = "SINGLE"
    MULTI = "MULTI"


class RelationSelectionReason(StrEnum):
    """Stable one-hop temporal, trust, and conflict selection outcomes."""

    NO_ELIGIBLE_PROPOSITION = "relation_no_eligible_proposition"
    SELECTED_UNIQUE = "relation_selected_unique"
    SELECTED_LATEST = "relation_selected_latest"
    SELECTED_TRUST_RANKED = "relation_selected_trust_ranked"
    TEMPORAL_BOUNDS_OPEN = "relation_temporal_bounds_open"
    LATEST_BOUND_UNAVAILABLE = "relation_latest_bound_unavailable"
    LATEST_TIE = "relation_latest_tie"
    TRUST_UNAVAILABLE = "relation_trust_unavailable"
    CARDINALITY_UNKNOWN = "relation_cardinality_unknown"
    CONFLICT_SINGLE_VALUE = "relation_conflict_single_value"
    VALID_MULTI_VALUE = "relation_valid_multi_value"
    BOUNDED_MULTIPLE_PERIODS = "relation_bounded_multiple_periods"


class TemporalQueryOperator(StrEnum):
    """Closed temporal interpretations supported by Section 9."""

    UNSPECIFIED = "unspecified"
    CURRENT = "current"
    NOW = "now"
    AS_OF = "as_of"
    IN_YEAR = "in_year"
    BEFORE = "before"
    AFTER = "after"
    BETWEEN = "between"
    LATEST = "latest"


class TemporalAxis(StrEnum):
    """Whether a temporal request addresses world-validity or observation time."""

    VALID_TIME = "valid_time"
    SYSTEM_TIME = "system_time"
