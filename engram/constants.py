"""Shared constants and enumerations for ENGRAM.

This module is the single home for the package's enums and literal data
constants (stopword lists, substitution maps, POS-tag sets, scoring weights, and
so on). It depends only on the standard library, so it sits at the root of the
import graph: every other ENGRAM module may import from it without risk of a
cycle.
"""

import hashlib
import re
from collections.abc import Mapping
from datetime import UTC, datetime
from enum import Enum, StrEnum
from pathlib import Path
from types import MappingProxyType

# =============================================================================
# Package metadata
# =============================================================================

# Canonical package version. pyproject.toml derives the distribution version
# from this via setuptools' dynamic ``attr``, so the version lives in exactly one
# place, and core.py exposes it as the bot's ``version`` property.
VERSION = "1.1.11"
DEFAULT_USER_ID = "0"
EMPTY_MAPPING = MappingProxyType({})
EMPTY_CONFIG: dict = {}
EMPTY_METADATA: dict = {}
PROPOSAL_TTL_SECONDS = 300
MAX_TRANSIENT_RECORDS = 1_000
REGULATOR_OUTCOMES = set(
    {
        "accepted",
        "rejected_quality",
        "rejected_context",
        "rejected_stale",
        "rejected_policy",
    }
)
DEFAULT_BIND_ADDRESS = "127.0.0.1:50051"
DEFAULT_GRACE_SECONDS = 10.0
DEFAULT_MAX_WORKERS = 10
CONVERSATION_REPORT_VERSION = 1
DIALOGUE_ACKNOWLEDGMENT = "acknowledgment"
DIALOGUE_CLOSING = "closing"
DIALOGUE_COMMAND = "command"
DIALOGUE_EMOTION = "emotion"
DIALOGUE_FACT = "fact"
DIALOGUE_GRATITUDE = "gratitude"
DIALOGUE_GREETING = "greeting"
DIALOGUE_OPINION = "opinion"
DIALOGUE_QUESTION = "question"
DIALOGUE_SELF_INTRODUCTION = "self_introduction"
DIALOGUE_STATEMENT = "statement"
DIALOGUE_TOPIC_SHIFT = "topic_shift"
EARLIEST_UTC = datetime.min.replace(tzinfo=UTC)
VOWELS = set("aeiou")
FRAME_OVERRIDES = MappingProxyType(
    {
        "precedes": "{s} precedes {o}",
        "dissolved_date": "{s} was dissolved in {o}",
        "date_of_birth": "{s} was born on {o}",
        "date_of_death": "{s} died on {o}",
        "born_in": "{s} was born in {o}",
        "inception": "{s} was founded in {o}",
        "publication_date": "{s} was published on {o}",
        "point_in_time": "{s} occurred on {o}",
        "capital_of": "{s} is the capital of {o}",
        "has_capital": "{s}'s capital is {o}",
        "present_in_work": "{s} appears in {o}",
        "award_received": "{s} received {o}",
        "contains_location": "{s} contains {o}",
    }
)
GRAPH_ENTITY_FACTS_QUERY = (
    "MATCH (c:Claim)-[:HAS_SUBJECT]->(proof_subject:Entity) "
    "MATCH (c)-[:USES_PREDICATE]->(proof_predicate:Predicate) "
    "MATCH (c)-[rel:HAS_SUBJECT|HAS_OBJECT]->(e:Entity) "
    "OPTIONAL MATCH (c)-[:HAS_OBJECT]->(proof_object:Entity) "
    "WITH c, proof_subject, proof_predicate, proof_object, rel, e "
    "WHERE (toLower(e.primary_label) = toLower($name) "
    "OR toLower($name) IN [a IN e.aliases | toLower(a)] "
    "OR toLower(rel.surface_form) = toLower($name)) "
    "AND c.invalidated_at IS NULL AND c.system_to IS NULL "
    "AND proof_predicate.canonical_id <> 'generic_relation' "
    "AND coalesce(c.predicate_canonical, true) = true "
    "AND (trim(coalesce(c.object, '')) = '' OR proof_object.canonical_id IS NOT NULL) "
    "RETURN DISTINCT c.id AS claim_id, c.subject AS subject, c.predicate AS predicate, c.object AS object "
    "LIMIT 5"
)
GRAPH_KEYWORD_FACTS_QUERY = (
    "MATCH (c:Claim)-[:HAS_SUBJECT]->(e:Entity) "
    "MATCH (c)-[:USES_PREDICATE]->(proof_predicate:Predicate) "
    "OPTIONAL MATCH (c)-[:HAS_OBJECT]->(proof_object:Entity) "
    "WITH c, e, proof_predicate, proof_object "
    "WHERE toLower(e.primary_label) CONTAINS toLower($keyword) "
    "AND c.invalidated_at IS NULL AND c.system_to IS NULL "
    "AND proof_predicate.canonical_id <> 'generic_relation' "
    "AND coalesce(c.predicate_canonical, true) = true "
    "AND (trim(coalesce(c.object, '')) = '' OR proof_object.canonical_id IS NOT NULL) "
    "RETURN DISTINCT c.id AS claim_id, c.subject AS subject, c.predicate AS predicate, c.object AS object "
    "LIMIT 3"
)
TRIPLE_QUERY_OBJECT = (
    "MATCH (c:Claim)-[hs:HAS_SUBJECT]->(s:Entity), "
    "(c)-[:USES_PREDICATE]->(p:Predicate), (c)-[ho:HAS_OBJECT]->(o:Entity) "
    "WHERE (toLower(s.primary_label) = toLower($subject) "
    "OR toLower($subject) IN [a IN s.aliases | toLower(a)] "
    "OR toLower(hs.surface_form) = toLower($subject)) "
    "AND (toLower(p.label) = toLower($predicate) "
    "OR toLower($predicate) IN [y IN p.synonyms | toLower(y)]) "
    "AND c.invalidated_at IS NULL "
    "RETURN ho.surface_form AS result LIMIT 1"
)
TRIPLE_QUERY_SUBJECT = (
    "MATCH (c:Claim)-[hs:HAS_SUBJECT]->(s:Entity), "
    "(c)-[:USES_PREDICATE]->(p:Predicate), (c)-[ho:HAS_OBJECT]->(o:Entity) "
    "WHERE (toLower(o.primary_label) = toLower($object) "
    "OR toLower($object) IN [a IN o.aliases | toLower(a)] "
    "OR toLower(ho.surface_form) = toLower($object)) "
    "AND (toLower(p.label) = toLower($predicate) "
    "OR toLower($predicate) IN [y IN p.synonyms | toLower(y)]) "
    "AND c.invalidated_at IS NULL "
    "RETURN hs.surface_form AS result LIMIT 1"
)
ARTIFACT_SCHEMA_VERSION = 1
ARTIFACT_PROVENANCE_SCHEMA_VERSION = 1
ARTIFACT_STATISTICS_SCHEMA_VERSION = 1
MAX_ARTIFACT_ID_BYTES = 256
MAX_RESPONSE_BYTES = 1_048_576
MAX_SOURCE_LABEL_BYTES = 256
MAX_CALLER_ID_BYTES = 256
MAX_SUPPORT_CLAIM_IDS = 256
MAX_SUPPORT_CLAIM_ID_BYTES = 256
MAX_TIMESTAMP_BYTES = 40
MAX_METADATA_BYTES = 65_536
MAX_METADATA_DEPTH = 8
MAX_METADATA_ITEMS = 1_024
MAX_METADATA_KEY_BYTES = 256
MAX_METADATA_STRING_BYTES = 16_384
MAX_ARTIFACT_ENUM_BYTES = 16
ARTIFACT_PROVENANCE_FIELDS = set({"schema_version", "source_label", "caller_id", "accepted_at"})
ARTIFACT_STATISTICS_FIELDS = set(
    {
        "schema_version",
        "hit_count",
        "query_count",
        "last_hit",
        "last_hit_available",
    }
)
CACHED_RESPONSE_ARTIFACT_FIELDS = set(
    {
        "schema_version",
        "statement_id",
        "generation",
        "response",
        "query_identity",
        "retrieval",
        "tier",
        "lifecycle",
        "scope",
        "support_claim_ids",
        "valid_from",
        "valid_from_available",
        "valid_until",
        "valid_until_available",
        "knowledge_epoch",
        "knowledge_epoch_available",
        "superseded_by",
        "provenance",
        "statistics",
        "metadata",
    }
)
LIFECYCLE_BASE_DECISION_FIELDS = set({"lifecycle", "direct_answer_eligible", "reason"})
LIFECYCLE_TRANSITION_DECISION_FIELDS = set({"current", "target", "operation", "allowed", "reason"})
HISTORICAL_KEY_REUSE_DECISION_FIELDS = set({"allowed", "reason"})
SCOPE_SCHEMA_VERSION = 1
IDENTITY_SCHEMA_VERSION = 1
RETRIEVAL_NORMALIZATION_VERSION = 1
SCOPED_RETRIEVAL_KEY_SCHEMA_VERSION = 1
RETRIEVAL_REPRESENTATION_SCHEMA_VERSION = 1
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
MAX_IDENTITY_JSON_BYTES = 262_144
SCOPE_KEY_FIELDS = set({"schema_version", "namespace", "context_fingerprint"})
SCOPED_RETRIEVAL_KEY_FIELDS = set({"schema_version", "normalization_version", "scope", "normalized_key"})
EMPTY_SCOPE_KEY = MappingProxyType(
    {
        "schema_version": SCOPE_SCHEMA_VERSION,
        "namespace": "",
        "context_fingerprint": "",
    }
)
ENTITY_REFERENCE_FIELDS = set({"surface", "canonical_id"})
RELATION_REFERENCE_FIELDS = set({"surface", "canonical_id"})
IDENTITY_QUALIFIER_FIELDS = set({"kind", "value"})
RETRIEVAL_KEY_BINDING_FIELDS = set({"key", "origin", "representation"})
RETRIEVAL_REPRESENTATION_FIELDS = set(
    {
        "schema_version",
        "normalization_version",
        "canonical",
        "aliases",
    }
)
QUERY_IDENTITY_FIELDS = set(
    {
        "schema_version",
        "normalization_version",
        "canonical_form",
        "operator",
        "entities",
        "relation",
        "qualifiers",
        "lexical_terms",
        "scope",
    }
)
EMPTY_RELATION_REFERENCE = MappingProxyType({"surface": "", "canonical_id": ""})
ELIGIBILITY_CONTEXT_SCHEMA_VERSION = 1
NAMESPACE_EPOCH_STATE_SCHEMA_VERSION = 1
NAMESPACE_EPOCH_FIELDS = set({"namespace", "knowledge_epoch", "knowledge_epoch_available"})
EPOCH_INCREMENT_FIELDS = set({"namespace", "previous_epoch", "knowledge_epoch", "reason"})
TRUSTED_ELIGIBILITY_INPUT_FIELDS = set(
    {
        "namespace",
        "evaluation_time",
        "evaluation_time_available",
        "knowledge_epoch",
        "knowledge_epoch_available",
        "source_label",
    }
)
ELIGIBILITY_CONTEXT_FIELDS = set(
    {
        "schema_version",
        "evaluation_time",
        "evaluation_time_available",
        "namespace",
        "knowledge_epoch",
        "knowledge_epoch_available",
        "artifact_repository_available",
        "epoch_source",
    }
)
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
        "knowledge_epoch",
        "knowledge_epoch_available",
        "artifact_repository_available",
        "epoch_policy",
    }
)
CONTEXTUAL_EXACT_LOOKUP_RESULT_FIELDS = set(
    {
        "lookup",
        "decisions",
        "context_signature",
        "index_refreshed",
        "index_state_generation",
    }
)
MAX_EPOCH = 9_223_372_036_854_775_807
MAX_EPOCH_SOURCE_LABEL_BYTES = 256
MAX_EPOCH_SOURCE_BYTES = 32
MAX_ELIGIBILITY_TIMESTAMP_BYTES = 40
MAX_ELIGIBILITY_STATEMENT_ID_BYTES = 256
MAX_ELIGIBILITY_CONTEXT_SIGNATURE_BYTES = 4_096
INDEX_PROJECTION_SCHEMA_VERSION = 1
INDEX_STATE_SCHEMA_VERSION = 1
INDEX_STATE_FIELDS = set(
    {
        "state_generation",
        "retrieval_to_owners",
        "statement_to_retrieval",
        "claim_to_statements",
        "statement_to_claims",
        "direct_retrieval",
        "projections",
        "build_report",
        "normalization_version",
        "schema_version",
    }
)
INDEX_PROJECTION_FIELDS = set(
    {
        "schema_version",
        "statement_id",
        "generation",
        "retrieval_keys",
        "support_claim_ids",
        "direct_answer_eligible",
        "exclusion_reason",
        "normalization_version",
    }
)
INDEX_PROJECTION_BINDING_FIELDS = set({"key", "provenance", "representation"})
MAX_INDEX_STATEMENT_ID_BYTES = 256
MAX_INDEX_RETRIEVAL_KEYS = 33
MAX_INDEX_SUPPORT_IDS = 256
MAX_INDEX_EXCLUSION_REASON_BYTES = 128
MAX_INDEX_REPORT_ITEMS = 1_000
MAX_INDEX_REPORT_DETAIL_BYTES = 512
MAX_INDEX_LOOKUP_OWNERS = 1_000
MAX_INDEX_SUPPORT_SCAN_EDGES = 100_000
SUPPORT_MATCH_FIELDS = set({"statement_id", "matched_claim_ids"})
SUPPORT_LOOKUP_RESULT_FIELDS = set(
    {
        "queried_claim_ids",
        "matches",
        "omitted_match_count",
        "omitted_edge_count",
        "scanned_edge_count",
        "complete",
        "reason",
    }
)
SUPPORT_SCAN_PLAN_FIELDS = set({"queried_claim_ids", "edge_count", "scan_limit", "complete", "reason"})
INDEX_SUPPORT_SCAN_LIMIT_REASON = "scan_limit_exceeded"
MAX_INDEX_SUPPORT_REASON_BYTES = 64
EXACT_LOOKUP_RESULT_FIELDS = set(
    {
        "outcome",
        "key",
        "statement_id",
        "generation",
        "provenance",
        "representation",
        "owner_statement_ids",
        "truncated",
    }
)
MAX_INDEX_PROVENANCE_BYTES = 32
MAX_INDEX_REPRESENTATION_BYTES = 1_024
RETRIEVAL_OWNER_FIELDS = set({"statement_id", "generation", "provenance", "representation", "direct_answer_eligible"})
INDEX_COLLISION_REPORT_FIELDS = set({"key", "statement_ids", "truncated"})
INDEX_BUILD_ISSUE_FIELDS = set({"reason", "statement_id", "position", "detail", "input_only"})
INDEX_BUILD_REPORT_FIELDS = set(
    {
        "input_count",
        "projection_count",
        "exact_key_count",
        "support_edge_count",
        "issues",
        "collisions",
        "omitted_issue_count",
        "omitted_collision_count",
    }
)
INDEX_CHECK_ISSUE_FIELDS = set({"category", "index_name", "key", "expected", "actual", "error"})
INDEX_CHECK_REPORT_FIELDS = set({"consistent", "checked_state_generation", "issues", "omitted_issue_count"})
INDEX_REPAIR_RESULT_FIELDS = set({"applied", "changed", "before_generation", "after_generation", "candidate_report", "live_check"})
MAX_INDEX_CHECK_NAME_BYTES = 128
MAX_INDEX_CHECK_KEY_BYTES = 4_096
MAX_INDEX_CHECK_VALUE_BYTES = 4_096


class CostClass(StrEnum):
    """Closed cost classes used by deterministic resolver plans."""

    EXACT = "exact"
    CHEAP = "cheap"
    STANDARD = "standard"
    EXPENSIVE = "expensive"


RESOLUTION_BUDGET_SCHEMA_VERSION = 2
RESOLUTION_BUDGET_FIELDS = set(
    {
        "schema_version",
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
MAX_RESOLUTION_VECTOR_RESULTS = 100_000
MAX_RESOLUTION_EVIDENCE = 1_000
MAX_RESOLUTION_EVIDENCE_BYTES = 1_048_576
MIN_RESOLUTION_OUTPUT_BYTES = 4_096
MAX_RESOLUTION_OUTPUT_BYTES = 2_097_152
MAX_RESOLUTION_DIAGNOSTIC_BYTES = 1_048_576
MAX_RESOLUTION_WORKING_MEMORY_BYTES = 1_073_741_824
MAX_RESOURCE_COUNTER = 9_223_372_036_854_775_807
BUDGET_CONSUMPTION_SCHEMA_VERSION = 1
BUDGET_CONSUMPTION_FIELDS = set(
    {
        "schema_version",
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
QUERY_FRAME_SCHEMA_VERSION = 2
QUERY_FRAME_FIELDS = set(
    {
        "schema_version",
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
REWRITE_TRACE_STEP_FIELDS = set({"rule_id", "input_text", "output_text"})
COMPACT_QUERY_FRAME_SCHEMA_VERSION = 2
COMPACT_QUERY_FRAME_FIELDS = set(
    {
        "schema_version",
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
MAX_CONTEXTUAL_TURN_DISTANCE = 2
MIN_CONTEXTUAL_INHERITANCE_CONFIDENCE = 0.55
MAX_CONTEXTUAL_TOPIC_BYTES = 256
TEMPORAL_QUERY_SCHEMA_VERSION = 1
TEMPORAL_QUERY_FIELDS = set(
    {
        "schema_version",
        "operator",
        "axis",
        "source_text",
        "start",
        "start_available",
        "end",
        "end_available",
        "confidence",
        "resolved",
    }
)
MAX_TEMPORAL_SOURCE_BYTES = 512
RELATION_CONTRACT_SCHEMA_VERSION = 2
MAX_RELATION_SURFACES = 12
MAX_RELATION_CANDIDATES = 8
MAX_RELATION_PLAN_ROWS = 10
MAX_RELATION_LABEL_BYTES = 256
CANONICAL_ENTITY_MATCH_FIELDS = set({"canonical_id", "primary_label", "aliases", "edge_surfaces", "entity_type"})
CANONICAL_PREDICATE_MATCH_FIELDS = set({"canonical_id", "primary_label", "synonyms", "object_type"})
CANONICAL_RESOLUTION_FIELDS = set(
    {
        "schema_version",
        "status",
        "canonical_id",
        "primary_label",
        "object_type",
        "score",
        "candidate_ids",
        "evidence",
    }
)
ONE_HOP_QUERY_PLAN_FIELDS = set(
    {
        "schema_version",
        "template_id",
        "subject_entity_id",
        "predicate_id",
        "expected_object_type",
        "max_rows",
    }
)
COMPOSITION_CONTRACT_SCHEMA_VERSION = 1
COMPOSITION_STEP_FIELDS = set(
    {
        "schema_version",
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
        "schema_version",
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
        "max_path_claims",
    }
)
MAX_COMPOSITION_HOPS = 2
MAX_COMPOSITION_ROWS = 64
MAX_COMPOSITION_BRANCHES = 4
MAX_COMPOSITION_CANDIDATES_PER_STEP = 8
MAX_COMPOSITION_PATH_CLAIMS = 2
MAX_COMPOSITION_BINDING_BYTES = 64
MAX_COMPOSITION_PREDICATE_SURFACES = 12
FEATURE_SET_SCHEMA_VERSION = 1
FEATURE_SET_FIELDS = set({"schema_version", "values", "unavailable"})
CANONICAL_CLAIM_REFERENCES_SCHEMA_VERSION = 1
CLAIM_VALIDITY_INPUTS_SCHEMA_VERSION = 2
CLAIM_TRUST_INPUTS_SCHEMA_VERSION = 1
DISCLOSURE_DECISION_SCHEMA_VERSION = 1
CANONICAL_CLAIM_REFERENCES_FIELDS = set({"schema_version", "subject_entity_id", "predicate_id", "object_entity_id"})
CLAIM_VALIDITY_INPUTS_FIELDS = set(
    {
        "schema_version",
        "evaluation_time",
        "active",
        "system_current",
        "valid_time_current",
        "eligible_for_request",
        "system_time_match",
        "valid_time_match",
        "valid_time_match_available",
        "temporal_operator",
        "temporal_axis",
        "requested_start",
        "requested_start_available",
        "requested_end",
        "requested_end_available",
        "system_from",
        "system_from_available",
        "system_to",
        "system_to_available",
        "invalidated_at",
        "invalidated_at_available",
        "valid_from",
        "valid_from_available",
        "valid_to",
        "valid_to_available",
    }
)
CLAIM_TRUST_INPUTS_FIELDS = set(
    {
        "schema_version",
        "trust_category",
        "trust_category_available",
        "supplied_trust",
        "supplied_trust_available",
        "supplied_trust_version",
        "supplied_trust_version_available",
    }
)
DISCLOSURE_DECISION_FIELDS = set(
    {"schema_version", "ownership", "basis", "scope", "policy_version", "authority", "authority_available"}
)
CLAIM_EVIDENCE_RECORD_SCHEMA_VERSION = 2
CLAIM_EVIDENCE_PATH_SCHEMA_VERSION = 1
CLAIM_EVIDENCE_PATH_STEP_FIELDS = set(
    {
        "schema_version",
        "position",
        "claim_id",
        "subject_entity_id",
        "predicate_id",
        "object_entity_id",
        "operator",
        "input_binding",
        "output_binding",
        "filters",
        "aggregation_inputs",
    }
)
CLAIM_EVIDENCE_RECORD_FIELDS = set(
    {
        "schema_version",
        "claim_id",
        "source_resolver",
        "source_contributions",
        "features",
        "canonical_references",
        "validity",
        "trust",
        "disclosure",
        "path",
        "selection_reasons",
    }
)
EVIDENCE_PACKAGE_WIRE_VERSION = 2
EVIDENCE_PACKAGE_FIELDS = set({"wire_version", "records", "retained_count", "omitted_count", "truncated", "truncation_reasons"})
EVIDENCE_USEFULNESS_DECISION_FIELDS = set({"policy_version", "claim_id", "included", "reasons"})
EVIDENCE_USEFULNESS_POLICY_FIELDS = set(
    {
        "policy_version",
        "canonical_completeness_floor",
        "structured_match_floor",
        "semantic_similarity_floor",
        "source_agreement_floor",
        "supplied_trust_floor",
        "supplied_trust_floor_available",
    }
)
VISIBILITY_AUTHORIZATION_FIELDS = set({"allowed", "scope", "ownership", "authority_id", "policy_version", "reason_code"})
VISIBILITY_GRANT_FIELDS = set({"scope", "ownership"})
CLAIM_ELIGIBILITY_DECISION_FIELDS = set({"projection", "eligible", "reason", "disclosure", "disclosure_available", "revalidated"})
EVIDENCE_REFERENCE_SCHEMA_VERSION = 1
EVIDENCE_REFERENCE_FIELDS = set({"schema_version", "evidence_id", "resolver", "kind", "scope", "provenance", "diagnostics"})
CANDIDATE_SCHEMA_VERSION = 1
CANDIDATE_FIELDS = set(
    {
        "schema_version",
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
ACCOUNTING_OBSERVATION_SCHEMA_VERSION = 1
ACCOUNTING_OBSERVATION_FIELDS = set({"schema_version", "statement_id", "keywords"})
MAX_ACCOUNTING_KEYWORDS = 256
MAX_ACCOUNTING_KEYWORD_BYTES = 256
RESOLVER_RESULT_SCHEMA_VERSION = 1
RESOLUTION_RESULT_SCHEMA_VERSION = 1
RESOLVER_RESULT_FIELDS = set(
    {
        "schema_version",
        "resolver",
        "state",
        "reason_code",
        "candidates",
        "evidence",
        "claim_evidence",
        "accounting",
        "diagnostics",
        "consumption",
    }
)
RESOLUTION_RESULT_FIELDS = set(
    {
        "schema_version",
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
MAX_REQUEST_BYTES = 16_384
MAX_DIAGNOSTIC_ID_BYTES = 256
MAX_RESOLVER_NAME_BYTES = 96
EXACT_RESOLVER_NAME = "exact"
EXACT_RESOLVER_COST_CLASS = CostClass.EXACT
PATTERN_RESOLVER_NAME = "pattern"
PATTERN_RESOLVER_COST_CLASS = CostClass.CHEAP
LEXICAL_RESOLVER_NAME = "lexical"
LEXICAL_RESOLVER_COST_CLASS = CostClass.CHEAP
SPARSE_RESOLVER_NAME = "sparse"
SPARSE_RESOLVER_COST_CLASS = CostClass.CHEAP
SPARSE_DOCUMENT_SCHEMA_VERSION = 1
SPARSE_INDEX_SCHEMA_VERSION = 1
SPARSE_INDEX_VERSION = 1
SPARSE_TOKENIZER_VERSION = 1
UTILITY_RESOLVER_NAME = "utility"
UTILITY_RESOLVER_COST_CLASS = CostClass.CHEAP
UTILITY_CONTRACT_VERSION = "utility-plugin-v1"
UTILITY_RESOLVER_VERSION = "utility-resolver-v1"
UTILITY_PLUGIN_NAMES = (
    "arithmetic_v1",
    "boolean_v1",
    "set_v1",
    "date_time_v1",
    "unit_conversion_v1",
    "version_v1",
    "identifier_v1",
)
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
APPROVED_SEMANTIC_MODEL_ID = "sentence-transformers/all-MiniLM-L6-v2"
APPROVED_SEMANTIC_MODEL_VERSION = "826711e54e001c83835913827a843d8dd0a1def9"
APPROVED_SEMANTIC_LICENSE_ID = "apache-2.0"
APPROVED_SEMANTIC_ARTIFACT_SHA256 = "ff12d37a18ee862cd4a5b8476466bc84f06d0801da9b749023eed74251cefcb8"
APPROVED_SEMANTIC_BACKEND = "native"
APPROVED_SEMANTIC_DIMENSION = 384
SEMANTIC_RECORD_SCHEMA_VERSION = 1
SEMANTIC_INDEX_SCHEMA_VERSION = 1
SEMANTIC_INDEX_VERSION = 1
SEMANTIC_ARTIFACT_HASH_VERSION = 1
STRUCTURED_GRAPH_RESOLVER_NAME = "structured_graph"
STRUCTURED_GRAPH_RESOLVER_COST_CLASS = CostClass.STANDARD
SUPPORT_SEMANTIC_RESOLVER_NAME = "support_semantic"
SUPPORT_SEMANTIC_RESOLVER_COST_CLASS = CostClass.EXPENSIVE
OPERATIONAL_TELEMETRY_SCHEMA_VERSION = 1
OPERATIONAL_RESOLVER_NAMES = (
    EXACT_RESOLVER_NAME,
    UTILITY_RESOLVER_NAME,
    PATTERN_RESOLVER_NAME,
    LEXICAL_RESOLVER_NAME,
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
OPERATIONAL_REBUILD_KINDS = ("primary", "sparse", "semantic")
MAX_REASON_CODE_BYTES = 96
MAX_FEATURES = 64
MAX_TRACE_STEPS = 32
MAX_JSON_DEPTH = 8
MAX_JSON_ITEMS = 4_096
MAX_JSON_STRING_BYTES = 16_384
MAX_JSON_BYTES = 65_536
MAX_RESOLUTION_VALUES = 1_000
MAX_CLAIM_IDENTIFIER_BYTES = 256
MAX_CLAIM_SOURCE_CONTRIBUTIONS = 8
MAX_CLAIM_SELECTION_REASONS = 16
MAX_CLAIM_TIMESTAMP_BYTES = 40
MAX_CLAIM_TRUST_CATEGORY_BYTES = 96
MAX_DISCLOSURE_AUTHORITY_BYTES = 256
MAX_DISCLOSURE_ENUM_BYTES = 32
MAX_EVIDENCE_PACKAGE_RECORDS = 10
MAX_EVIDENCE_PACKAGE_INPUT_RECORDS = 1_000
MAX_EVIDENCE_PACKAGE_BYTES = 65_536
MAX_EVIDENCE_PACKAGE_TRUNCATION_REASONS = 8
FUSION_POLICY_SCHEMA_VERSION = 1
NORMALIZED_FEATURE_SCHEMA_VERSION = 1
CANDIDATE_ELIGIBILITY_SCHEMA_VERSION = 1
FUSION_CONTRIBUTION_SCHEMA_VERSION = 1
FUSED_CANDIDATE_SCHEMA_VERSION = 1
FUSION_DECISION_SCHEMA_VERSION = 1
FEATURE_DEFINITION_FIELDS = set(
    {
        "feature",
        "minimum",
        "maximum",
        "higher_is_better",
        "meaning",
        "unavailable_meaning",
        "producer",
        "owner_section",
        "trust_boundary",
        "raw_range",
        "combination_rule",
        "role",
    }
)
NORMALIZED_FEATURE_SET_FIELDS = set({"schema_version", "values", "available"})
FUSION_POLICY_FIELDS = set(
    {
        "schema_version",
        "policy_version",
        "formula_version",
        "weights",
        "answer_threshold",
        "evidence_threshold",
        "ambiguity_margin",
        "minimum_independent_sources",
        "require_support_for_non_exact",
        "max_report_candidates",
    }
)
CANDIDATE_ELIGIBILITY_FIELDS = set(
    {
        "schema_version",
        "score_eligible",
        "evidence_eligible",
        "answer_eligible",
        "reason_codes",
        "feature_values",
        "feature_available",
    }
)
FUSION_CONTRIBUTION_FIELDS = set({"schema_version", "candidate", "normalized", "eligibility"})
FUSED_CANDIDATE_FIELDS = set(
    {"schema_version", "candidate", "contributions", "normalized", "score", "score_contributions", "eligibility"}
)
FUSION_DECISION_FIELDS = set(
    {
        "schema_version",
        "outcome",
        "selected_candidate",
        "selected_candidate_available",
        "response_candidates",
        "evidence",
        "confidence",
        "confidence_available",
        "reason_codes",
        "report",
        "working_memory_bytes",
    }
)
FUSION_POLICY_VERSION = "fusion-v1.0.0"
FUSION_FORMULA_VERSION = 1
FUSION_ANSWER_THRESHOLD = 0.78
FUSION_EVIDENCE_THRESHOLD = 0.35
FUSION_AMBIGUITY_MARGIN = 0.12
FUSION_MINIMUM_INDEPENDENT_SOURCES = 2
MIN_FUSION_INDEPENDENT_SOURCES = 2
MAX_FUSION_INDEPENDENT_SOURCES = 6
MAX_FUSION_POLICY_VERSION_BYTES = 96
FUSION_REQUIRE_SUPPORT_FOR_NON_EXACT = True
MAX_FUSION_CONTRIBUTIONS = 1_000
MAX_FUSION_REPORT_CANDIDATES = 8
MAX_FUSION_REPORT_CONTRIBUTIONS = 8
MAX_FUSION_REPORT_BYTES = 16_384
MAX_FUSION_REASON_CODES = 64
FEEDBACK_POLICY_SCHEMA_VERSION = 1
FEEDBACK_POLICY_FIELDS = set(
    {
        "schema_version",
        "policy_version",
        "minimum_verdict_samples",
        "prior_accept",
        "prior_reject",
        "half_life_seconds",
        "bucket_seconds",
        "max_buckets_per_record",
        "max_statement_records",
        "max_relationship_records",
    }
)
FEEDBACK_POLICY_VERSION = "feedback-history-v1.0.0"
FEEDBACK_MINIMUM_VERDICT_SAMPLES = 5
FEEDBACK_PRIOR_ACCEPT = 1.0
FEEDBACK_PRIOR_REJECT = 3.0
FEEDBACK_HALF_LIFE_SECONDS = 2_592_000
FEEDBACK_BUCKET_SECONDS = 86_400
FEEDBACK_MAX_BUCKETS_PER_RECORD = 32
FEEDBACK_MAX_STATEMENT_RECORDS = 10_000
FEEDBACK_MAX_RELATIONSHIP_RECORDS = 50_000
MAX_FEEDBACK_POLICY_SAMPLES = 1_000_000
MAX_FEEDBACK_PRIOR_MASS = 1_000_000.0
MAX_FEEDBACK_HALF_LIFE_SECONDS = 315_576_000
MAX_FEEDBACK_BUCKET_SECONDS = 31_557_600
MAX_FEEDBACK_POLICY_RECORDS = 1_000_000
FEEDBACK_STATISTICS_SCHEMA_VERSION = 1
FEEDBACK_KEY_SCHEMA_VERSION = 1
FEEDBACK_OBSERVATION_SCHEMA_VERSION = 1
STATEMENT_FEEDBACK_KEY_FIELDS = set(
    {"schema_version", "statement_id", "generation", "generation_available", "policy_fingerprint", "contract_fingerprint"}
)
RELATIONSHIP_FEEDBACK_KEY_FIELDS = set({"schema_version", "query_identity", "scope", "constraint_fingerprint", "statement"})
FEEDBACK_OBSERVATION_FIELDS = set(
    {
        "schema_version",
        "reference_kind",
        "reference_id",
        "kind",
        "outcome",
        "query_identity",
        "scope",
        "constraint_fingerprint",
        "statement_id",
        "generation",
        "generation_available",
        "policy_fingerprint",
        "contract_fingerprint",
        "observed_at",
        "reason",
    }
)
FEEDBACK_BUCKET_SCHEMA_VERSION = 1
FEEDBACK_RECORD_SCHEMA_VERSION = 1
FEEDBACK_STATISTICS_FIELDS = set(
    {
        "schema_version",
        "candidate_count",
        "accept_count",
        "rejected_quality",
        "rejected_context",
        "rejected_stale",
        "rejected_policy",
    }
)
FEEDBACK_BUCKET_FIELDS = set({"schema_version", "start_at", "statistics"})
FEEDBACK_RECORD_FIELDS = set({"schema_version", "key", "raw", "buckets", "last_outcome", "last_observed_at"})
FEEDBACK_HISTORY_SCHEMA_VERSION = 1
POLICY_SUPPRESSION_FIELDS = set({"statement_id", "namespace", "policy_fingerprint", "observed_at"})
STALE_EXCLUSION_FIELDS = set({"statement_id", "generation", "generation_available", "observed_at"})
FEEDBACK_HISTORY_FIELDS = set(
    {
        "schema_version",
        "value",
        "available",
        "statement_value",
        "statement_available",
        "relationship_value",
        "relationship_available",
        "statement_samples",
        "relationship_samples",
        "policy_fingerprint",
        "feedback_policy_fingerprint",
    }
)
FEEDBACK_STATE_SCHEMA_VERSION = 1
FEEDBACK_STATE_FIELDS = set(
    {
        "schema_version",
        "policy",
        "statement_records",
        "relationship_records",
        "policy_suppressions",
        "stale_exclusions",
        "receipts",
        "statement_evictions",
        "relationship_evictions",
        "policy_suppression_evictions",
        "stale_exclusion_evictions",
    }
)
NEGATIVE_RESOLUTION_SCHEMA_VERSION = 1
NEGATIVE_KEY_SCHEMA_VERSION = 1
NEGATIVE_RESOLUTION_KEY_FIELDS = set(
    {
        "schema_version",
        "query_identity",
        "scope",
        "constraint_fingerprint",
        "knowledge_epoch",
        "knowledge_epoch_available",
        "normalization_version",
        "resolver_plan_fingerprint",
        "capability_readiness_fingerprint",
        "policy_fingerprint",
    }
)
NEGATIVE_RESOLUTION_FIELDS = set({"schema_version", "key", "reason", "created_at", "expires_at", "hit_count"})
NEGATIVE_LOOKUP_FIELDS = set({"hit", "record"})
EMPTY_FINGERPRINT = "0" * 64
EMPTY_NEGATIVE_CREATED_AT = "1970-01-01T00:00:00Z"
EMPTY_NEGATIVE_EXPIRES_AT = "1970-01-01T00:00:01Z"
DEFAULT_NEGATIVE_MAX_RECORDS = 1_000
DEFAULT_NEGATIVE_TTL_SECONDS = 300
FEEDBACK_CONTRACT_VERSION = "feedback-v1.0.0"
FEEDBACK_CONTRACT_FINGERPRINT = hashlib.sha256(FEEDBACK_CONTRACT_VERSION.encode("utf-8")).hexdigest()
MAX_REFERENCE_ID_BYTES = 256
MAX_FEEDBACK_REASON_BYTES = 512
MAX_STATEMENT_ID_BYTES = 256
MAX_VERSION_BYTES = 96
MAX_FINGERPRINT_BYTES = 64
MAX_FEEDBACK_OBSERVATIONS = 1_000
MAX_FEEDBACK_SIGNATURE_BYTES = 67_108_864
MAX_FEEDBACK_BUCKETS = 64
MAX_INSPECTION_RECORDS = 64
MAX_NEGATIVE_RECORDS = 10_000
MAX_NEGATIVE_TTL_SECONDS = 86_400
MAX_CONSTRAINT_JSON_BYTES = 65_536
MAX_PLAN_RESOLVERS = 64
RESOLUTION_PLAN_ENTRY_FIELDS = set({"resolver", "order", "configured", "available", "reason_code"})
RESOLUTION_PLAN_FIELDS = set({"entries"})
EXECUTION_REPORT_FIELDS = set({"results", "consumption", "exact_short_circuited", "reservations"})
ACCOUNTING_FINALIZATION_FIELDS = set(
    {
        "candidate_statement_ids",
        "accepted_statement_id",
        "candidacy_applied",
        "success_applied",
        "idempotent",
    }
)
MAX_ACCOUNTING_VISIBLE_STATEMENT_IDS = 64
RESOLVER_BUDGET_SCHEMA_VERSION = 2
RESOLVER_BUDGET_FIELDS = set(
    {
        "schema_version",
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
RESOLVER_RESERVATION_SCHEMA_VERSION = 1
RESOLVER_RESERVATION_FIELDS = set({"schema_version", "resolver", "order", "lease", "consumption"})
CLAIM_DISCLOSURE_POLICY_VERSION = "claim-disclosure-v1"
CLAIM_EVIDENCE_USEFULNESS_POLICY_VERSION = "claim-evidence-usefulness-v1"
CANONICAL_COMPLETENESS_FLOOR_V1 = 1.0
STRUCTURED_MATCH_FLOOR_V1 = 1.0
SEMANTIC_SIMILARITY_FLOOR_V1 = 0.60
SOURCE_AGREEMENT_FLOOR_V1 = 1.0
MAX_VISIBILITY_GRANTS = 4_096
RECONNECT_COOLDOWN_SECONDS = 60
CONNECTION_LOST_MARKERS = (
    "connection",
    "socket",
    "broken pipe",
    "reset by peer",
    "closed",
    "timed out",
    "refused",
    "unreachable",
)
WRITE_CLAUSE = re.compile(
    r"\b(CREATE|MERGE|DELETE|SET|REMOVE|DROP|DETACH|FOREACH|CALL|LOAD|" r"GRANT|DENY|REVOKE|ALTER|COPY|FREE)\b",
    re.IGNORECASE,
)
VECTOR_INDEX_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,127}$")
MAX_CLAIM_PROJECTION_ROWS = 1_000
MAX_CLAIM_PROJECTION_EMBEDDING_DIMENSIONS = 65_536
MAX_CLAIM_PROJECTION_IDENTIFIER_BYTES = 256
MAX_CLAIM_PROJECTION_TERM_BYTES = 4_096
MAX_CLAIM_PROJECTION_TIMESTAMP_BYTES = 40
CLAIM_PROJECTION_FIELDS = set(
    {
        "claim_id",
        "subject_entity_id",
        "predicate_id",
        "object_entity_id",
        "invalidated_at",
        "invalidated_at_available",
        "system_from",
        "system_from_available",
        "system_to",
        "system_to_available",
        "valid_from",
        "valid_from_available",
        "valid_to",
        "valid_to_available",
        "predicate_canonical",
        "ownership_category",
        "trust_category",
        "trust_category_available",
        "supplied_trust",
        "supplied_trust_available",
        "supplied_trust_version",
        "supplied_trust_version_available",
        "structured_match",
        "structured_match_available",
        "semantic_similarity",
        "semantic_similarity_available",
    }
)
CLAIM_PROJECTION_RECORD_FIELDS = CLAIM_PROJECTION_FIELDS | set(
    {
        "projection_id",
        "vector_index_id",
        "vector_index_id_available",
    }
)
RELATION_ONE_HOP_RESULT_FIELDS = CLAIM_PROJECTION_FIELDS | {
    "object_label",
    "object_type",
    "predicate_cardinality",
}
CANONICAL_ENTITY_MATCH_QUERY = (
    "MATCH (entity:Entity) "
    "OPTIONAL MATCH (:Claim)-[edge:HAS_SUBJECT|HAS_OBJECT]->(entity) "
    "WITH entity, coalesce(entity.aliases, [])[0..12] AS aliases, "
    "[surface IN collect(DISTINCT edge.surface_form) WHERE surface IS NOT NULL][0..12] AS edge_surfaces "
    "WHERE toLower(entity.primary_label) = toLower($surface) "
    "OR toLower($surface) IN [alias IN aliases | toLower(alias)] "
    "OR toLower($surface) IN [value IN edge_surfaces | toLower(value)] "
    "RETURN entity.canonical_id AS canonical_id, entity.primary_label AS primary_label, "
    "aliases, edge_surfaces, coalesce(entity.entity_type, 'UNKNOWN') AS entity_type "
    "ORDER BY entity.canonical_id LIMIT $limit"
)
CANONICAL_PREDICATE_MATCH_QUERY = (
    "MATCH (predicate:Predicate) "
    "WITH predicate, coalesce(predicate.synonyms, [])[0..12] AS synonyms "
    "WHERE toLower(predicate.canonical_id) = toLower($surface) "
    "OR toLower(coalesce(predicate.primary_label, predicate.label, predicate.canonical_id)) = toLower($surface) "
    "OR toLower($surface) IN [synonym IN synonyms | toLower(synonym)] "
    "RETURN predicate.canonical_id AS canonical_id, "
    "coalesce(predicate.primary_label, predicate.label, predicate.canonical_id) AS primary_label, "
    "synonyms, coalesce(predicate.object_type, 'UNKNOWN') AS object_type "
    "ORDER BY predicate.canonical_id LIMIT $limit"
)
CLAIM_PROJECTION_RETURN = (
    "RETURN DISTINCT c.id AS claim_id, "
    "subject.canonical_id AS subject_entity_id, "
    "predicate.canonical_id AS predicate_id, "
    "object.canonical_id AS object_entity_id, "
    "c.invalidated_at AS invalidated_at, "
    "c.invalidated_at IS NOT NULL AS invalidated_at_available, "
    "c.system_from AS system_from, "
    "c.system_from IS NOT NULL AS system_from_available, "
    "c.system_to AS system_to, "
    "c.system_to IS NOT NULL AS system_to_available, "
    "c.valid_from AS valid_from, "
    "c.valid_from IS NOT NULL AS valid_from_available, "
    "c.valid_to AS valid_to, "
    "c.valid_to IS NOT NULL AS valid_to_available, "
    "c.predicate_canonical AS predicate_canonical, "
    "c.ownership_category AS ownership_category, "
    "c.trust_category AS trust_category, "
    "c.trust_category IS NOT NULL AS trust_category_available, "
    "c.source_calibrated_trust AS supplied_trust, "
    "c.source_calibrated_trust IS NOT NULL AS supplied_trust_available, "
    "c.source_trust_score_version AS supplied_trust_version, "
    "c.source_trust_score_version IS NOT NULL AS supplied_trust_version_available, "
)
STRUCTURED_ENTITY_CLAIM_PROJECTION_QUERY = (
    "MATCH (c:Claim)-[:HAS_SUBJECT]->(subject:Entity) "
    "MATCH (c)-[:USES_PREDICATE]->(predicate:Predicate) "
    "MATCH (c)-[:HAS_OBJECT]->(object:Entity) "
    "MATCH (c)-[matched_rel:HAS_SUBJECT|HAS_OBJECT]->(matched_entity:Entity) "
    "WHERE (toLower(matched_entity.primary_label) = toLower($value) "
    "OR toLower($value) IN [alias IN matched_entity.aliases | toLower(alias)] "
    "OR toLower(matched_rel.surface_form) = toLower($value)) "
    "AND c.invalidated_at IS NULL AND c.system_to IS NULL "
    "AND c.predicate_canonical = true AND predicate.canonical_id <> 'generic_relation' "
    + CLAIM_PROJECTION_RETURN
    + "1.0 AS structured_match, true AS structured_match_available, "
    "0.0 AS semantic_similarity, false AS semantic_similarity_available "
    "ORDER BY c.id LIMIT $limit"
)
STRUCTURED_KEYWORD_CLAIM_PROJECTION_QUERY = (
    "MATCH (c:Claim)-[:HAS_SUBJECT]->(subject:Entity) "
    "MATCH (c)-[:USES_PREDICATE]->(predicate:Predicate) "
    "MATCH (c)-[:HAS_OBJECT]->(object:Entity) "
    "WHERE toLower(subject.primary_label) CONTAINS toLower($value) "
    "AND c.invalidated_at IS NULL AND c.system_to IS NULL "
    "AND c.predicate_canonical = true AND predicate.canonical_id <> 'generic_relation' "
    + CLAIM_PROJECTION_RETURN
    + "1.0 AS structured_match, true AS structured_match_available, "
    "0.0 AS semantic_similarity, false AS semantic_similarity_available "
    "ORDER BY c.id LIMIT $limit"
)
VECTOR_CLAIM_PROJECTION_QUERY = (
    "CALL vector_search.search($index_name, $limit, $query_embedding) YIELD node, distance "
    "WITH node AS c, 1.0 - distance AS similarity "
    "MATCH (c)-[:HAS_SUBJECT]->(subject:Entity) "
    "MATCH (c)-[:USES_PREDICATE]->(predicate:Predicate) "
    "MATCH (c)-[:HAS_OBJECT]->(object:Entity) "
    "WHERE similarity >= $min_similarity "
    "AND c.invalidated_at IS NULL AND c.system_to IS NULL "
    "AND c.predicate_canonical = true AND predicate.canonical_id <> 'generic_relation' "
    + CLAIM_PROJECTION_RETURN
    + "0.0 AS structured_match, false AS structured_match_available, "
    "similarity AS semantic_similarity, true AS semantic_similarity_available "
    "ORDER BY semantic_similarity DESC, c.id"
)
CLAIM_PROJECTION_BY_ID_QUERY = (
    "MATCH (c:Claim)-[:HAS_SUBJECT]->(subject:Entity) "
    "MATCH (c)-[:USES_PREDICATE]->(predicate:Predicate) "
    "MATCH (c)-[:HAS_OBJECT]->(object:Entity) "
    "WHERE c.id = $claim_id " + CLAIM_PROJECTION_RETURN + "0.0 AS structured_match, false AS structured_match_available, "
    "0.0 AS semantic_similarity, false AS semantic_similarity_available "
    "ORDER BY c.id LIMIT 2"
)
RELATION_ONE_HOP_CLAIM_PROJECTION_QUERY = (
    "MATCH (c:Claim)-[:HAS_SUBJECT]->(subject:Entity) "
    "MATCH (c)-[:USES_PREDICATE]->(predicate:Predicate) "
    "MATCH (c)-[:HAS_OBJECT]->(object:Entity) "
    "WHERE subject.canonical_id = $subject_entity_id AND predicate.canonical_id = $predicate_id "
    "AND ($include_historical = true OR (c.invalidated_at IS NULL AND c.system_to IS NULL)) "
    "AND c.predicate_canonical = true AND predicate.canonical_id <> 'generic_relation' "
    + CLAIM_PROJECTION_RETURN
    + "1.0 AS structured_match, true AS structured_match_available, "
    "0.0 AS semantic_similarity, false AS semantic_similarity_available, "
    "object.primary_label AS object_label, coalesce(object.entity_type, 'UNKNOWN') AS object_type, "
    "coalesce(predicate.cardinality, 'UNKNOWN') AS predicate_cardinality "
    "ORDER BY c.id LIMIT $limit"
)
MAX_STRUCTURED_CLAIM_PROJECTION_TERMS = 3
CLAIM_EVIDENCE_PRODUCERS = set({"structured_graph", "support_semantic"})
MCP_CONFORMANCE_MINIMUM_TURNS = 1_000
COORDINATED_RESPONSE_STATE_FIELDS = set(
    {
        "repository",
        "namespace_epochs",
        "mutation_receipts",
    }
)
COORDINATED_MUTATION_CANDIDATE_FIELDS = set(
    {
        "before",
        "after",
        "receipt",
        "affected_epoch_namespaces",
    }
)
MUTATION_EXECUTION_RESULT_FIELDS = set(
    {
        "receipt",
        "checkpoint_count",
        "durable",
        "published",
        "recovered",
    }
)
RESPONSE_STATE_SCHEMA_VERSION = 1
LEGACY_PERSISTENCE_VERSION = 1
MAX_QUARANTINE_DETAIL_BYTES = 512
MAX_QUARANTINE_RECORDS = 100_000
RESPONSE_QUARANTINE_RECORD_FIELDS = set(
    {
        "statement_id",
        "reason",
        "detail",
    }
)
TIER_ADMISSION_POLICY_FIELDS = set(
    {
        "dynamic_capacity",
        "eviction_policy",
        "minimum_protected_hit_rate",
    }
)
ADMISSION_PLAN_FIELDS = set(
    {
        "outcome",
        "candidate",
        "admitted_statement_id",
        "evicted_statement_ids",
        "affected_epoch_namespaces",
        "residency_changed",
        "lifecycle_changed",
    }
)
REPOSITORY_CHECK_REPORT_FIELDS = set(
    {
        "consistent",
        "state_generation",
        "artifact_count",
        "issues",
        "omitted_issue_count",
    }
)
REPOSITORY_STATE_FIELDS = set(
    {
        "state_generation",
        "artifacts",
        "statements",
        "index_state",
    }
)
MAX_REPOSITORY_ARTIFACTS = 100_000
MAX_REPOSITORY_CHECK_ISSUES = 1_000
MUTATION_RECEIPT_SCHEMA_VERSION = 1
MUTATION_LEDGER_SCHEMA_VERSION = 1
MAX_REQUEST_ID_BYTES = 256
MAX_RECEIPT_STATEMENT_ID_BYTES = 256
MAX_RECEIPT_TIMESTAMP_BYTES = 40
MAX_RESULT_BYTES = 65_536
MAX_RECEIPT_JSON_BYTES = 1_048_576
MAX_SIGNATURE_INPUT_BYTES = 1_048_576
MAX_RESULT_DEPTH = 8
MAX_RESULT_ITEMS = 1_024
MAX_RESULT_KEY_BYTES = 256
MAX_RESULT_STRING_BYTES = 16_384
MAX_AFFECTED_GENERATIONS = 1_024
MAX_RECEIPTS = 100_000
MAX_TOMBSTONES = 100_000
ARTIFACT_GENERATION_CHANGE_FIELDS = set(
    {
        "statement_id",
        "before_generation",
        "after_generation",
    }
)
RECEIPT_TOMBSTONE_FIELDS = set(
    {
        "sequence",
        "request_id",
        "operation",
        "payload_signature",
    }
)
MUTATION_RECEIPT_FIELDS = set(
    {
        "schema_version",
        "sequence",
        "request_id",
        "operation",
        "payload_signature",
        "result_code",
        "affected_generations",
        "result",
        "completion_state",
        "created_at",
    }
)
RECEIPT_LOOKUP_FIELDS = set(
    {
        "outcome",
        "request_id",
        "receipt_json",
        "receipt_available",
    }
)
MCP_CONFORMANCE_MESSAGES = (
    "hello",
    "How are you?",
    "What can you remember?",
    "Tell me more.",
    "Why?",
    "Where are we?",
    "When is now?",
    "Who are you?",
    "What did I say?",
    "Continue.",
)
MCP_TURN_EVENT_FIELDS = set(
    {
        "turn",
        "input",
        "response",
        "user_id",
        "source",
        "score",
        "pattern",
        "captured",
        "dialogue_act",
        "active_topic",
        "entities",
        "fact_admissions",
        "elapsed_seconds",
        "context_changes",
        "learned_statements",
    }
)
MCP_TURN_EVALUATION_CHECKS = set(
    {
        "exact_fields",
        "turn_sequence",
        "input_continuity",
        "user_continuity",
        "response_nonempty",
        "source_nonempty",
        "score_finite",
        "pattern_string",
        "captured_list",
        "dialogue_act_string",
        "active_topic_string",
        "entities_list",
        "fact_admissions_list",
        "elapsed_nonnegative",
        "context_changes_object",
        "learned_statements_list",
    }
)

# =============================================================================
# Enumerations
# =============================================================================


class CheckpointFailureKind(StrEnum):
    """Whether a failed checkpoint is known not to have committed."""

    DEFINITE = "DEFINITE"
    INDETERMINATE = "INDETERMINATE"


class ClaimEligibilityReason(StrEnum):
    """Closed temporal Claim evidence eligibility outcomes."""

    ELIGIBLE_PUBLIC = "eligible_public"
    ELIGIBLE_TRUSTED_SCOPE = "eligible_trusted_scope"
    EVALUATION_TIME_UNAVAILABLE = "evaluation_time_unavailable"
    TEMPORAL_QUERY_UNRESOLVED = "temporal_query_unresolved"
    CLAIM_INACTIVE = "claim_inactive"
    SYSTEM_TIME_UNAVAILABLE = "system_time_unavailable"
    SYSTEM_NOT_YET_CURRENT = "system_not_yet_current"
    SYSTEM_NO_LONGER_CURRENT = "system_no_longer_current"
    VALID_TIME_NOT_YET_CURRENT = "valid_time_not_yet_current"
    VALID_TIME_NO_LONGER_CURRENT = "valid_time_no_longer_current"
    RETRIEVAL_ONLY = "retrieval_only"
    VISIBILITY_AUTHORITY_UNAVAILABLE = "visibility_authority_unavailable"
    VISIBILITY_AUTHORITY_FAILED = "visibility_authority_failed"
    VISIBILITY_SCOPE_MISMATCH = "visibility_scope_mismatch"
    VISIBILITY_OWNERSHIP_MISMATCH = "visibility_ownership_mismatch"
    VISIBILITY_DENIED = "visibility_denied"
    REVALIDATION_UNAVAILABLE = "revalidation_unavailable"
    REVALIDATION_MISSING = "revalidation_missing"
    REVALIDATION_IDENTITY_CONFLICT = "revalidation_identity_conflict"


class EvidenceUsefulnessReason(StrEnum):
    """Stable inspectable reasons from the unfitted version-1 inclusion policy."""

    CANONICAL_COMPLETENESS_UNAVAILABLE = "canonical_completeness_unavailable"
    CANONICAL_COMPLETENESS_BELOW_FLOOR = "canonical_completeness_below_floor"
    STRUCTURED_MATCH_QUALIFIED = "structured_match_qualified"
    SEMANTIC_SIMILARITY_QUALIFIED = "semantic_similarity_qualified"
    RETRIEVAL_SIGNAL_UNAVAILABLE = "retrieval_signal_unavailable"
    RETRIEVAL_SIGNAL_BELOW_FLOOR = "retrieval_signal_below_floor"
    SOURCE_AGREEMENT_QUALIFIED = "source_agreement_qualified"
    SUPPLIED_TRUST_AVAILABLE = "supplied_trust_available"
    SUPPLIED_TRUST_UNAVAILABLE = "supplied_trust_unavailable"
    SUPPLIED_TRUST_FLOOR_SATISFIED = "supplied_trust_floor_satisfied"
    SUPPLIED_TRUST_REQUIRED_UNAVAILABLE = "supplied_trust_required_unavailable"
    SUPPLIED_TRUST_BELOW_FLOOR = "supplied_trust_below_floor"


class ExactLookupOutcome(StrEnum):
    """The complete set of exact lookup outcomes."""

    FOUND = "FOUND"
    MISS = "MISS"
    COLLISION = "COLLISION"


class IndexIssueReason(StrEnum):
    """Stable build and validation classifications."""

    MISSING_IDENTITY = "missing_identity"
    UNSUPPORTED_SCHEMA_VERSION = "unsupported_schema_version"
    UNSUPPORTED_NORMALIZATION_VERSION = "unsupported_normalization_version"
    MALFORMED_PROJECTION = "malformed_projection"
    MALFORMED_SUPPORT = "malformed_support"
    INELIGIBLE = "ineligible"
    WITHIN_ARTIFACT_DUPLICATE = "within_artifact_duplicate"
    DUPLICATE_STATEMENT_ID = "duplicate_statement_id"
    CROSS_ARTIFACT_COLLISION = "cross_artifact_collision"


class IndexCheckCategory(StrEnum):
    """Categories emitted by the consistency checker."""

    MISSING = "missing"
    EXTRA = "extra"
    ASYMMETRIC = "asymmetric"
    INELIGIBLE = "ineligible"
    UNINDEXABLE = "unindexable"
    CONFLICTING = "conflicting"


class MutationOperation(StrEnum):
    """Transport-neutral accepted-response mutations."""

    COMMIT_RESPONSE = "COMMIT_RESPONSE"
    INVALIDATE_RESPONSE = "INVALIDATE_RESPONSE"
    RETIRE_RESPONSE = "RETIRE_RESPONSE"
    SUPERSEDE_RESPONSE = "SUPERSEDE_RESPONSE"
    RECORD_RESPONSE_QUERY = "RECORD_RESPONSE_QUERY"
    RECORD_RESPONSE_HIT = "RECORD_RESPONSE_HIT"
    FINALIZE_RESOLUTION_ACCOUNTING = "FINALIZE_RESOLUTION_ACCOUNTING"
    RECORD_FEEDBACK = "RECORD_FEEDBACK"


class MutationResultCode(StrEnum):
    """Stable results that can be replayed from a receipt."""

    CREATED = "CREATED"
    CREATED_WITH_EVICTION = "CREATED_WITH_EVICTION"
    INVALIDATED = "INVALIDATED"
    RETIRED = "RETIRED"
    SUPERSEDED = "SUPERSEDED"
    REJECTED_CAPACITY = "REJECTED_CAPACITY"
    QUERY_RECORDED = "QUERY_RECORDED"
    HIT_RECORDED = "HIT_RECORDED"
    RESOLUTION_ACCOUNTING_RECORDED = "RESOLUTION_ACCOUNTING_RECORDED"
    FEEDBACK_RECORDED = "FEEDBACK_RECORDED"


class ReceiptCompletionState(StrEnum):
    """Durable progress state of one mutation identity."""

    PREPARED = "PREPARED"
    COMPLETED = "COMPLETED"


class ReceiptLookupOutcome(StrEnum):
    """Complete outcomes before applying a mutation request."""

    NEW = "NEW"
    REPLAY = "REPLAY"
    IN_PROGRESS = "IN_PROGRESS"
    EXPIRED = "EXPIRED"
    CONFLICT = "CONFLICT"


class ExpectedObjectType(StrEnum):
    """Base object-type vocabulary populated further by Section 8."""

    UNKNOWN = "UNKNOWN"
    PERSON = "PERSON"
    PLACE = "PLACE"
    DATE = "DATE"
    NUMBER = "NUMBER"
    BOOLEAN = "BOOLEAN"
    ENTITY = "ENTITY"


class EvidenceKind(StrEnum):
    """Kinds safe for the minimal Section 4 evidence-reference contract."""

    CLAIM = "claim"
    GRAPH_FACT = "graph_fact"
    SUPPORT = "support"


class ClaimOwnership(StrEnum):
    """Allow-listed canonical Claim ownership categories."""

    PUBLIC = "PUBLIC"
    COMPANY = "COMPANY"
    CUSTOMER = "CUSTOMER"


class DisclosureBasis(StrEnum):
    """Stable provenance for a successful Claim visibility decision."""

    PUBLIC_RULE = "public_rule"
    TRUSTED_SCOPE_AUTHORITY = "trusted_scope_authority"


class EvidencePackageTruncationReason(StrEnum):
    """Stable reasons that a package retained fewer records than supplied."""

    DUPLICATE_CLAIM_ID = "duplicate_claim_id"
    RECORD_LIMIT = "record_limit"
    SERIALIZED_SIZE_LIMIT = "serialized_size_limit"


class CandidateSource(StrEnum):
    """Initial and future candidate-producing resolver sources."""

    EXACT = "exact"
    PATTERN = "pattern"
    LEXICAL = "lexical"
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


class RolloutMode(StrEnum):
    """Namespace-selectable unified-resolution rollout modes."""

    DISABLED = "disabled"
    SHADOW = "shadow"
    EVIDENCE_ONLY = "evidence_only"
    REGULATED_DIRECT_ANSWER = "regulated_direct_answer"
    ROLLBACK = "rollback"


class LifecycleMutationReason(StrEnum):
    """Closed caller-supplied reasons for terminal lifecycle mutations."""

    SOURCE_RETRACTED = "SOURCE_RETRACTED"
    POLICY = "POLICY"
    STALE = "STALE"
    USER_REQUEST = "USER_REQUEST"
    ADMINISTRATIVE = "ADMINISTRATIVE"


class CoreState(StrEnum):
    """Lifecycle state of the single owned application core."""

    RUNNING = "running"
    CLOSING = "closing"
    CLOSED = "closed"


class DurabilityState(StrEnum):
    """Current relationship between live state and configured persistence."""

    DISABLED = "disabled"
    HEALTHY = "healthy"
    DEGRADED = "degraded"


class SessionOverflow(Enum):
    """Behavior when session limit is reached."""

    REJECT = "reject"
    EXPIRE_OLDEST = "expire_oldest"
    LRU = "lru"


class ClaimProjectionQuery(StrEnum):
    """Allow-listed fixed query identifiers for full Claim projection."""

    STRUCTURED_ENTITY_V1 = "structured_entity_claim_projection_v1"
    STRUCTURED_KEYWORD_V1 = "structured_keyword_claim_projection_v1"
    RELATION_ONE_HOP_V1 = "relation_one_hop_claim_projection_v1"
    VECTOR_V1 = "vector_claim_projection_v1"
    BY_ID_V1 = "claim_projection_by_id_v1"


class CanonicalResolutionStatus(StrEnum):
    """Complete outcomes for canonical entity or predicate resolution."""

    SELECTED = "selected"
    AMBIGUOUS = "ambiguous"
    MISS = "miss"


class RelationPlanTemplate(StrEnum):
    """Allow-listed internal Section 8 query-plan templates."""

    ONE_HOP_CLAIM_V1 = "one_hop_claim_v1"


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

    NO_ELIGIBLE_CLAIM = "relation_no_eligible_claim"
    SELECTED_UNIQUE = "relation_selected_unique"
    SELECTED_LATEST = "relation_selected_latest"
    SELECTED_TRUST_RANKED = "relation_selected_trust_ranked"
    TEMPORAL_BOUNDS_OPEN = "relation_temporal_bounds_open"
    LATEST_BOUND_UNAVAILABLE = "relation_latest_bound_unavailable"
    LATEST_TIE = "relation_latest_tie"
    TRUST_UNAVAILABLE = "relation_trust_unavailable"
    TRUST_VERSION_INCOMPARABLE = "relation_trust_version_incomparable"
    CARDINALITY_UNKNOWN = "relation_cardinality_unknown"
    CONFLICT_SINGLE_VALUE = "relation_conflict_single_value"
    VALID_MULTI_VALUE = "relation_valid_multi_value"
    BOUNDED_MULTIPLE_PERIODS = "relation_bounded_multiple_periods"


class EpochSource(StrEnum):
    """Authority that supplied the namespace epoch in a context."""

    STANDALONE = "standalone"
    TRUSTED_INTEGRATION = "trusted_integration"
    UNAVAILABLE = "unavailable"


class EpochChangeReason(StrEnum):
    """Operations allowed to advance a standalone namespace epoch."""

    ACCEPTED_ARTIFACT_ELIGIBILITY = "accepted_artifact_eligibility"
    GRAPH_SNAPSHOT_ACTIVATED = "graph_snapshot_activated"


class EpochEligibilityPolicy(StrEnum):
    """Caller-configured artifact epoch interpretation."""

    REQUIRE_MATCH = "require_match"
    MATCH_WHEN_ARTIFACT_AVAILABLE = "match_when_artifact_available"


class EligibilityExclusionReason(StrEnum):
    """Stable complete outcomes for direct accepted-response eligibility."""

    ELIGIBLE = "eligible"
    ARTIFACT_REPOSITORY_UNAVAILABLE = "artifact_repository_unavailable"
    EVALUATION_TIME_UNAVAILABLE = "evaluation_time_unavailable"
    SCOPE_NAMESPACE_MISMATCH = "scope_namespace_mismatch"
    LIFECYCLE_SUPERSEDED = "lifecycle_superseded"
    LIFECYCLE_INVALIDATED = "lifecycle_invalidated"
    LIFECYCLE_RETIRED = "lifecycle_retired"
    VALIDITY_INTERVAL_INVALID = "validity_interval_invalid"
    NOT_YET_VALID = "not_yet_valid"
    EXPIRED = "expired"
    ARTIFACT_EPOCH_UNAVAILABLE = "artifact_epoch_unavailable"
    CONTEXT_EPOCH_UNAVAILABLE = "context_epoch_unavailable"
    KNOWLEDGE_EPOCH_MISMATCH = "knowledge_epoch_mismatch"


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


class FusionFeature(StrEnum):
    """Closed, comparable feature vocabulary for fusion policy version 1."""

    EXACT = "exact"
    PATTERN = "pattern"
    LEXICAL = "lexical"
    SEMANTIC = "semantic"
    ENTITY = "entity"
    RELATION = "relation"
    OBJECT_TYPE = "object_type"
    SUPPORT = "support"
    HISTORY = "history"
    FRESHNESS = "freshness"
    AUTHORITY = "authority"
    AGREEMENT = "agreement"
    MARGIN = "margin"


class FusionFeatureRole(StrEnum):
    """Closed policy-stage role for one canonical fusion feature."""

    SCORING = "scoring"
    SCORING_AND_GATE = "scoring_and_gate"
    DERIVED_SCORING = "derived_scoring"
    DECISION_ONLY = "decision_only"


class FusionPolicyReason(StrEnum):
    """Stable non-content-bearing fusion outcome and eligibility reasons."""

    ANSWER_EXACT_ELIGIBLE = "answer_exact_eligible"
    ANSWER_FUSION_THRESHOLD = "answer_fusion_threshold"
    ANSWER_MARGIN_CLEAR = "answer_margin_clear"
    ANSWER_NO_RUNNER_UP = "answer_no_runner_up"
    AMBIGUOUS_TOP_CANDIDATES = "ambiguous_top_candidates"
    ANSWER_THRESHOLD_NOT_MET = "answer_threshold_not_met"
    ANSWER_ELIGIBILITY_PREVENTED = "answer_eligibility_prevented"
    EVIDENCE_THRESHOLD_MET = "evidence_threshold_met"
    GRAPH_EVIDENCE_AVAILABLE = "graph_evidence_available"
    EVIDENCE_THRESHOLD_NOT_MET = "evidence_threshold_not_met"
    NO_ELIGIBLE_CANDIDATES = "no_eligible_candidates"
    CANDIDATE_SCOPE_MISMATCH = "candidate_scope_mismatch"
    CANDIDATE_LIFECYCLE_INELIGIBLE = "candidate_lifecycle_ineligible"
    CANDIDATE_STATEMENT_CONFLICT = "candidate_statement_conflict"
    CANDIDATE_ID_CONFLICT = "candidate_id_conflict"
    CANDIDATE_EVIDENCE_SCOPE_MISMATCH = "candidate_evidence_scope_mismatch"
    EVIDENCE_REFERENCE_CONFLICT = "evidence_reference_conflict"
    AUTHORITATIVE_STATEMENT_MISSING = "authoritative_statement_missing"
    AUTHORITATIVE_RESPONSE_MISMATCH = "authoritative_response_mismatch"
    AUTHORITATIVE_GENERATION_MISMATCH = "authoritative_generation_mismatch"
    ARTIFACT_INELIGIBLE = "artifact_ineligible"
    REQUIRED_METADATA_MISMATCH = "required_metadata_mismatch"
    REQUIRED_SOURCE_MISMATCH = "required_source_mismatch"
    OWNERSHIP_VISIBILITY_MISMATCH = "ownership_visibility_mismatch"
    SUPPORT_INCOMPLETE = "support_incomplete"
    SUPPORT_REFERENCE_STALE = "support_reference_stale"
    INDEPENDENT_SOURCES_MISSING = "independent_sources_missing"
    IDENTITY_FEATURE_MISMATCH = "identity_feature_mismatch"
    OBJECT_TYPE_FEATURE_MISMATCH = "object_type_feature_mismatch"
    EXPLICIT_CONFLICT = "explicit_conflict"
    FUSION_MEMORY_EXHAUSTED = "fusion_memory_exhausted"
    FEEDBACK_STALE_EXCLUDED = "feedback_stale_excluded"
    FEEDBACK_POLICY_SUPPRESSED = "feedback_policy_suppressed"


class NegativeResolutionReason(StrEnum):
    """Only reusable first-generation negative-resolution reason."""

    INSUFFICIENT_KNOWLEDGE = "insufficient_knowledge"


class FeedbackReferenceKind(StrEnum):
    """Stable owner of the request reference carried by feedback."""

    RESOLUTION_REQUEST = "resolution_request"
    REGULATED_PROPOSAL = "regulated_proposal"


class FeedbackObservationKind(StrEnum):
    """Whether an observation records candidacy or an external verdict."""

    CANDIDACY = "candidacy"
    VERDICT = "verdict"


class FeedbackOutcome(StrEnum):
    """Closed first-generation feedback outcome vocabulary."""

    CANDIDATE = "candidate"
    ACCEPTED = "accepted"
    REJECTED_QUALITY = "rejected_quality"
    REJECTED_CONTEXT = "rejected_context"
    REJECTED_STALE = "rejected_stale"
    REJECTED_POLICY = "rejected_policy"


FEEDBACK_OUTCOME_COUNTER_FIELDS = MappingProxyType(
    {
        FeedbackOutcome.CANDIDATE: "candidate_count",
        FeedbackOutcome.ACCEPTED: "accept_count",
        FeedbackOutcome.REJECTED_QUALITY: "rejected_quality",
        FeedbackOutcome.REJECTED_CONTEXT: "rejected_context",
        FeedbackOutcome.REJECTED_STALE: "rejected_stale",
        FeedbackOutcome.REJECTED_POLICY: "rejected_policy",
    }
)


class LifecycleHandoffStatus(StrEnum):
    """Inspectable result of a stale-feedback lifecycle handoff."""

    NOT_APPLICABLE = "not_applicable"
    PENDING = "pending"
    COMPLETED = "completed"
    REPLAYED = "replayed"
    CONFLICTED = "conflicted"
    FAILED = "failed"


DEFAULT_FUSION_WEIGHTS = MappingProxyType(
    {
        FusionFeature.EXACT: 1.0,
        FusionFeature.PATTERN: 0.75,
        FusionFeature.LEXICAL: 1.0,
        FusionFeature.SEMANTIC: 1.0,
        FusionFeature.ENTITY: 0.4,
        FusionFeature.RELATION: 0.5,
        FusionFeature.OBJECT_TYPE: 0.3,
        FusionFeature.SUPPORT: 0.8,
        FusionFeature.HISTORY: 0.2,
        FusionFeature.FRESHNESS: 0.2,
        FusionFeature.AUTHORITY: 0.5,
        FusionFeature.AGREEMENT: 0.9,
        FusionFeature.MARGIN: 0.0,
    }
)
TEMPLATE_STAR_EXPRESSION = r"\{star(\d+)\}"
TEMPLATE_THATSTAR_EXPRESSION = r"\{thatstar(\d+)\}"
TEMPLATE_TOPICSTAR_EXPRESSION = r"\{topicstar(\d+)\}"
TEMPLATE_GET_EXPRESSION = r"\{get:([^:}]+)(?::([^}]*))?\}"
TEMPLATE_BOT_EXPRESSION = r"\{bot:([^}]+)\}"
TEMPLATE_MAP_EXPRESSION = r"\{map:([^:}]+):([^:}]+)(?::([^}]*))?\}"
TEMPLATE_INPUT_EXPRESSION = r"\{input:(\d+)\}"
TEMPLATE_RESPONSE_EXPRESSION = r"\{response(?::(\d+))?\}"
TEMPLATE_THAT_EXPRESSION = r"\{that(?::(\d+)(?::(\d+))?)?\}"
TEMPLATE_TRANSFORM_EXPRESSION = r"\{(upper|lower|capitalize|formal|sentence|person|person2|gender|normalize|denormalize|explode|first|rest|uniq|wordcount|sentiment|clause|qtype|name):([^{}]*)\}"
TEMPLATE_DATE_FORMAT_EXPRESSION = r"\{date:([^}]+)\}"
TEMPLATE_SIMPLE_VARIABLE_TOKENS = MappingProxyType(
    {
        "topic": "{topic}",
        "input": "{input}",
        "request": "{request}",
        "id": "{id}",
        "size": "{size}",
        "vocabulary": "{vocabulary}",
        "date": "{date}",
        "time": "{time}",
        "program": "{program}",
        "version": "{version}",
    }
)
GRPC_REGULATOR_OUTCOME_NAMES = MappingProxyType(
    {
        "REGULATOR_OUTCOME_ACCEPTED": "accepted",
        "REGULATOR_OUTCOME_REJECTED_QUALITY": "rejected_quality",
        "REGULATOR_OUTCOME_REJECTED_CONTEXT": "rejected_context",
        "REGULATOR_OUTCOME_REJECTED_STALE": "rejected_stale",
        "REGULATOR_OUTCOME_REJECTED_POLICY": "rejected_policy",
    }
)
FUSION_SOURCE_ORDER = MappingProxyType(
    {
        CandidateSource.EXACT: 0,
        CandidateSource.SUPPORT_SEMANTIC: 1,
        CandidateSource.PATTERN: 2,
        CandidateSource.LEXICAL: 3,
        CandidateSource.SPARSE: 4,
        CandidateSource.STANDALONE_SEMANTIC: 5,
        CandidateSource.UTILITY: 6,
    }
)
FUSION_SOURCE_FAMILY = MappingProxyType(
    {
        CandidateSource.EXACT: "exact",
        CandidateSource.SUPPORT_SEMANTIC: "support_semantic",
        CandidateSource.PATTERN: "pattern",
        CandidateSource.LEXICAL: "lexical",
        # Both resolvers derive lexical evidence from the same authoritative
        # request representations.  They must not manufacture independent
        # agreement merely by using different scoring functions.
        CandidateSource.SPARSE: "lexical",
        CandidateSource.STANDALONE_SEMANTIC: "standalone_semantic",
        CandidateSource.UTILITY: "utility",
    }
)
FUSION_CONSERVATIVE_MINIMUM = set(
    {
        FusionFeature.ENTITY,
        FusionFeature.RELATION,
        FusionFeature.OBJECT_TYPE,
        FusionFeature.SUPPORT,
        FusionFeature.FRESHNESS,
        FusionFeature.AUTHORITY,
    }
)
FusionFeatureDefinitionSpec = tuple[
    float,
    float,
    bool,
    str,
    str,
    str,
    str,
    str,
    str,
    str,
    FusionFeatureRole,
]
FUSION_FEATURE_DEFINITION_SPECS: Mapping[FusionFeature, FusionFeatureDefinitionSpec] = MappingProxyType(
    {
        FusionFeature.EXACT: (
            0.0,
            1.0,
            True,
            "scoped request equality or revalidated deterministic utility execution",
            "neither exact retrieval nor an allow-listed utility measured a deterministic match",
            "ExactResolver.exact_match or UtilityResolver.utility_match",
            "§§3-5, 14",
            "authoritative artifact revalidation or repeat execution of the named built-in utility",
            "exact_match or utility_match in [0, 1] from its dedicated source only",
            "maximum compatible deterministic observation",
            FusionFeatureRole.SCORING_AND_GATE,
        ),
        FusionFeature.PATTERN: (
            0.0,
            1.0,
            True,
            "bounded pattern specificity",
            "no pattern contribution",
            "PatternResolver.pattern_specificity",
            "§§4-5",
            "pattern source only",
            "finite nonnegative pattern word count",
            "maximum compatible pattern observation",
            FusionFeatureRole.SCORING,
        ),
        FusionFeature.LEXICAL: (
            0.0,
            1.0,
            True,
            "calibrated lexical relevance",
            "no lexical contribution",
            "LexicalResolver lexical score or SparseResolver sparse score",
            "§§4-5, 12",
            "lexical or sparse source only",
            "finite lexical score in the resolver's documented scale",
            "maximum compatible lexical observation",
            FusionFeatureRole.SCORING,
        ),
        FusionFeature.SEMANTIC: (
            0.0,
            1.0,
            True,
            "bounded semantic similarity",
            "no semantic model observation",
            "support or standalone semantic resolver semantic_score",
            "§§4-5, 13",
            "semantic candidate sources only",
            "finite similarity in [0, 1]",
            "maximum compatible semantic observation",
            FusionFeatureRole.SCORING,
        ),
        FusionFeature.ENTITY: (
            0.0,
            1.0,
            True,
            "query/candidate entity identity agreement",
            "entity identity was not measured",
            "contextual resolver entity_match",
            "§8",
            "trusted contextual identity producer",
            "finite agreement in [0, 1]",
            "minimum available agreement so a mismatch cannot be hidden",
            FusionFeatureRole.SCORING_AND_GATE,
        ),
        FusionFeature.RELATION: (
            0.0,
            1.0,
            True,
            "query/candidate relation agreement",
            "relation identity was not measured",
            "contextual resolver relation_match",
            "§8",
            "trusted contextual identity producer",
            "finite agreement in [0, 1]",
            "minimum available agreement so a mismatch cannot be hidden",
            FusionFeatureRole.SCORING_AND_GATE,
        ),
        FusionFeature.OBJECT_TYPE: (
            0.0,
            1.0,
            True,
            "expected/candidate object-type agreement",
            "object type was not measured",
            "contextual resolver object_type_match",
            "§8",
            "trusted contextual type producer",
            "finite agreement in [0, 1]",
            "minimum available agreement so a mismatch cannot be hidden",
            FusionFeatureRole.SCORING_AND_GATE,
        ),
        FusionFeature.SUPPORT: (
            0.0,
            1.0,
            True,
            "presence and completeness of linked response support",
            "support was not inspected",
            "retained SUPPORT references plus current artifact support state",
            "§§3-5",
            "current authoritative artifact and scope-consistent evidence only",
            "binary presence or finite coverage in [0, 1]",
            "authoritative current state with minimum explicit coverage",
            FusionFeatureRole.SCORING_AND_GATE,
        ),
        FusionFeature.HISTORY: (
            0.0,
            1.0,
            True,
            "bounded accepted-use history",
            "history was not observed",
            "current artifact statistics and later §6 feedback features",
            "§§3, 6",
            "authoritative statistics or versioned feedback producer; never statement priority",
            "finite rate in [0, 1]",
            "authoritative current observation",
            FusionFeatureRole.SCORING,
        ),
        FusionFeature.FRESHNESS: (
            0.0,
            1.0,
            True,
            "bounded time-recency signal",
            "freshness was not observed",
            "current resolver recency",
            "§§4-5",
            "resolver observation after Section 3 validity remains a hard gate",
            "finite recency in [0, 1]",
            "minimum available freshness",
            FusionFeatureRole.SCORING,
        ),
        FusionFeature.AUTHORITY: (
            0.0,
            1.0,
            True,
            "explicit source trust or authority input",
            "authority was not supplied",
            "explicit artifact metadata and later graph trust producer",
            "§§5, 9",
            "explicit finite trusted input only; source labels do not imply rank",
            "finite value in [0, 1]",
            "minimum available authority",
            FusionFeatureRole.SCORING,
        ),
        FusionFeature.AGREEMENT: (
            0.0,
            1.0,
            True,
            "independent resolver-family agreement for one statement",
            "deduplication was not run",
            "Section 5 candidate grouping",
            "§5",
            "derived only from eligible configured resolver families",
            "distinct family count",
            "derived once after deduplication",
            FusionFeatureRole.DERIVED_SCORING,
        ),
        FusionFeature.MARGIN: (
            0.0,
            1.0,
            True,
            "leading score minus runner-up score",
            "fewer than two score-eligible candidates",
            "Section 5 ranked distinct statements",
            "§5",
            "derived from the final deterministic ranking only",
            "difference between two bounded scores",
            "decision-only; never enters the score",
            FusionFeatureRole.DECISION_ONLY,
        ),
    }
)


class QualifierKind(StrEnum):
    """Identity-bearing qualifier categories recognized by the first builder."""

    NEGATION = "negation"
    QUANTITY = "quantity"
    COMPARISON = "comparison"
    TEMPORAL = "temporal"
    LOCATION = "location"
    CURRENT = "current"
    HISTORICAL = "historical"


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


class RetrievalOrigin(StrEnum):
    """The representation that produced one scoped retrieval key."""

    CANONICAL = "canonical"
    ALIAS = "alias"


class EvictionPolicy(Enum):
    """Policy for evicting DYNAMIC categories when at capacity."""

    FIFO = "fifo"  # First-in, first-out (oldest evicted first)
    LRU = "lru"  # Least recently used (oldest last-hit evicted)
    LFU = "lfu"  # Least frequently used (lowest hit count evicted)
    HIT_RATE = "hit_rate"  # Lowest hit rate (hits/queries) evicted


class Tier(Enum):
    """Statement tier classification."""

    STATIC = "STATIC"
    DYNAMIC = "DYNAMIC"


class LifecycleState(StrEnum):
    """Persisted lifecycle states for accepted-response artifacts."""

    ACTIVE = "ACTIVE"
    SUPERSEDED = "SUPERSEDED"
    INVALIDATED = "INVALIDATED"
    RETIRED = "RETIRED"


class LifecycleOperation(StrEnum):
    """Operations that may request a lifecycle transition."""

    SUPERSEDE = "SUPERSEDE"
    INVALIDATE = "INVALIDATE"
    RETIRE = "RETIRE"


class LifecycleDecisionReason(StrEnum):
    """Stable policy reasons for lifecycle eligibility and transitions."""

    ELIGIBLE = "eligible"
    SUPERSEDED = "lifecycle_superseded"
    INVALIDATED = "lifecycle_invalidated"
    RETIRED = "lifecycle_retired"
    LEGAL_TRANSITION = "legal_transition"
    SAME_STATE_NOT_A_TRANSITION = "same_state_not_a_transition"
    TERMINAL_STATE = "terminal_state"
    OPERATION_TARGET_MISMATCH = "operation_target_mismatch"


class HistoricalKeyReuseReason(StrEnum):
    """Stable decisions for historical retrieval-key reuse."""

    ALLOWED_EXPLICIT_REPLACEMENT = "allowed_explicit_replacement"
    BASE_COMMIT_FORBIDDEN = "base_commit_forbidden"
    EXPECTED_STATEMENT_ID_REQUIRED = "expected_statement_id_required"
    EXPECTED_GENERATION_REQUIRED = "expected_generation_required"


LEGAL_LIFECYCLE_TRANSITIONS = MappingProxyType(
    {
        LifecycleState.ACTIVE: MappingProxyType(
            {
                LifecycleOperation.SUPERSEDE: LifecycleState.SUPERSEDED,
                LifecycleOperation.INVALIDATE: LifecycleState.INVALIDATED,
                LifecycleOperation.RETIRE: LifecycleState.RETIRED,
            }
        ),
        LifecycleState.SUPERSEDED: EMPTY_MAPPING,
        LifecycleState.INVALIDATED: EMPTY_MAPPING,
        LifecycleState.RETIRED: EMPTY_MAPPING,
    }
)
TERMINAL_LIFECYCLE_STATES = set(
    {
        LifecycleState.SUPERSEDED,
        LifecycleState.INVALIDATED,
        LifecycleState.RETIRED,
    }
)
LIFECYCLE_INELIGIBLE_REASONS = MappingProxyType(
    {
        LifecycleState.SUPERSEDED: LifecycleDecisionReason.SUPERSEDED,
        LifecycleState.INVALIDATED: LifecycleDecisionReason.INVALIDATED,
        LifecycleState.RETIRED: LifecycleDecisionReason.RETIRED,
    }
)


class RepositoryRemovalReason(StrEnum):
    """Physical repository removal reasons, separate from lifecycle."""

    EXPLICIT_DELETE = "explicit_delete"
    CAPACITY_EVICTION = "capacity_eviction"


class AdmissionOutcome(StrEnum):
    """Complete bounded accepted-response admission outcomes."""

    ADMITTED = "ADMITTED"
    ADMITTED_WITH_EVICTION = "ADMITTED_WITH_EVICTION"
    REJECTED_CAPACITY = "REJECTED_CAPACITY"


class ResponseQuarantineReason(StrEnum):
    """Stable legacy-response migration exclusion reasons."""

    MISSING_IDENTITY = "missing_identity"
    MALFORMED_IDENTITY = "malformed_identity"
    AMBIGUOUS_IDENTITY = "ambiguous_identity"


# =============================================================================
# NLTK data
# =============================================================================

NLTK_DATA_DIR = str(Path(__file__).resolve().parent.parent / "data" / "nltk_data")

# Required packages as (find_path, download_name) pairs. find_path is what
# nltk.data.find expects; download_name is what nltk.download expects.
REQUIRED_PACKAGES = (
    ("tokenizers/punkt", "punkt"),
    ("tokenizers/punkt_tab", "punkt_tab"),
    ("taggers/averaged_perceptron_tagger", "averaged_perceptron_tagger"),
    ("taggers/averaged_perceptron_tagger_eng", "averaged_perceptron_tagger_eng"),
    ("chunkers/maxent_ne_chunker", "maxent_ne_chunker"),
    ("chunkers/maxent_ne_chunker_tab", "maxent_ne_chunker_tab"),
    ("corpora/words", "words"),
    ("corpora/wordnet", "wordnet"),
    ("corpora/omw-1.4", "omw-1.4"),
    ("sentiment/vader_lexicon", "vader_lexicon"),
)

# Stopwords filtered out during keyword extraction. A set for O(1) membership.
DEFAULT_STOPWORDS: set[str] = {
    "a",
    "an",
    "the",
    "is",
    "are",
    "was",
    "were",
    "be",
    "been",
    "being",
    "have",
    "has",
    "had",
    "do",
    "does",
    "did",
    "will",
    "would",
    "could",
    "should",
    "may",
    "might",
    "must",
    "shall",
    "can",
    "need",
    "dare",
    "ought",
    "used",
    "to",
    "of",
    "in",
    "for",
    "on",
    "with",
    "at",
    "by",
    "from",
    "as",
    "into",
    "through",
    "during",
    "before",
    "after",
    "above",
    "below",
    "between",
    "under",
    "again",
    "further",
    "then",
    "once",
    "here",
    "there",
    "when",
    "where",
    "why",
    "how",
    "all",
    "each",
    "few",
    "more",
    "most",
    "other",
    "some",
    "such",
    "no",
    "nor",
    "not",
    "only",
    "own",
    "same",
    "so",
    "than",
    "too",
    "very",
    "just",
    "also",
}


# =============================================================================
# spaCy
# =============================================================================

MODEL_NAME = "en_core_web_sm"


# =============================================================================
# Text processing
# =============================================================================

# POS tags for nouns and proper nouns (the referents a follow-up query's
# pronouns can point back to; used for session context expansion)
NOUN_POS_TAGS = {"NN", "NNS", "NNP", "NNPS"}

# Spelling correction (input cleanup). Correction is deliberately timid: only
# tokens at least MIN_SPELL_TOKEN_LENGTH long that are neither in the target
# vocabulary nor real English words are candidates, and only a unique nearest
# neighbor within the allowed Damerau-Levenshtein distance replaces them.
MIN_SPELL_TOKEN_LENGTH = 4
SPELL_LONG_TOKEN_LENGTH = 6  # Tokens this long or longer allow distance 2 (else 1)

# Conjunctions stripped from the end of a clause cut ("tired and" -> "tired")
CLAUSE_BOUNDARY_TRAILERS = {"and", "but", "or", "because", "so", "then"}

# Question words that also open a new clause mid-capture when followed by a
# verb ("the sky | what is the moon"). Relative pronouns (who / which / whose)
# are excluded: "the man who is tall" is one phrase, not two clauses.
CLAUSE_QUESTION_BOUNDARIES = {"what", "where", "when", "why", "how"}

# Name extraction from self-introduction captures ("still jason by the way").
# Leading fillers are skipped; a stop marker ends the name span.
NAME_LEADING_FILLERS = {"still", "actually", "really", "just", "now", "officially", "basically", "technically"}
NAME_STOP_MARKERS = {"by", "the", "way", "though", "btw", "anyway", "and", "but", "because", "for", "if", "these", "days"}
MAX_NAME_TOKENS = 3

# Personal subject pronouns that mark the start of a new clause when followed
# by a verb. Matched by word, not POS tag: NLTK tags a lowercase "i" as a
# noun or adjective, never PRP.
SUBJECT_PRONOUNS = {"i", "you", "he", "she", "it", "we", "they"}

# Tokens shorter than this are never Porter-stemmed: they are already near
# their root, and stemming mangles them into false matches ("his" -> "hi"
# would greet a possessive).
MIN_STEM_TOKEN_LENGTH = 4

# Acknowledgments rotated when a fact is learned from conversation, so a
# teaching session does not answer with the same phrase every turn.
LEARNED_ACKNOWLEDGMENTS = (
    "I see.",
    "Noted.",
    "Got it - I'll remember that.",
    "Understood.",
    "Okay, I'll keep that in mind.",
)

# Conversational escape used when a catch-all would repeat a recent prompt or
# the caller explicitly points out that the bot is looping.
REPETITION_ESCAPE_RESPONSE = "You're right - I was repeating myself. Let's take a different approach."
REPETITION_FEEDBACK_MARKERS = (
    "same question",
    "you are repeating",
    "youre repeating",
    "you keep repeating",
    "repeat yourself",
    "already explained",
    "just explained",
    "already answered",
    "asked that already",
)
RESPONSE_SIMILARITY_THRESHOLD = 0.72
REPETITION_HISTORY_SIZE = 8

# Responses when a stated fact matches what is already stored ({existing} is
# replaced with the stored statement text).
KNOWN_FACT_RESPONSES = (
    "Yes - {existing}",
    "Right, that matches what I have: {existing}",
)

# Responses when a stated fact contradicts what is already stored. The stored
# belief is protected (no overwrite), but silence would read as agreement, so
# the conflict is surfaced.
CONFLICTING_FACT_RESPONSES = (
    "Hmm, I have it differently: {existing}",
    "That differs from what I know: {existing}",
)

# Output polish: the pronoun I and its contractions are always capitalized
STANDALONE_I_FORMS = {"i": "I", "i'm": "I'm", "i've": "I've", "i'll": "I'll", "i'd": "I'd"}

# POS tags that indicate content words (nouns, verbs, adjectives, adverbs)
CONTENT_POS_TAGS = {
    "NN",
    "NNS",
    "NNP",
    "NNPS",  # Nouns
    "VB",
    "VBD",
    "VBG",
    "VBN",
    "VBP",
    "VBZ",  # Verbs
    "JJ",
    "JJR",
    "JJS",  # Adjectives
    "RB",
    "RBR",
    "RBS",  # Adverbs
}


# =============================================================================
# NLP fact extraction
# =============================================================================

# Copula verbs that indicate definitional statements
COPULAS = {"is", "are", "was", "were"}

# Words that indicate a question (should not extract facts)
QUESTION_WORDS = {"what", "who", "where", "when", "why", "how", "which", "whose"}

# Input kinds: the intent classification templates branch on via {qtype:...}
# and the pipeline routes on (questions get retrieval before a shrug).
KIND_QUESTION = "question"
KIND_COMMAND = "command"
KIND_STATEMENT = "statement"

# Words that indicate a command (should not extract facts)
COMMAND_WORDS = {"learn", "remember", "forget", "tell", "say", "repeat", "echo"}

# Personal pronouns excluded as relational-triple subjects or objects
PRONOUNS = {"i", "you", "he", "she", "it", "we", "they", "this", "that", "these", "those"}

# Pronouns that refer back to earlier conversation ("what is ITS population").
# Session context expansion only fires when the query carries one -- expanding
# every query would flood unrelated follow-ups with the previous response's
# nouns.
REFERRING_PRONOUNS = {
    "it",
    "its",
    "they",
    "them",
    "their",
    "theirs",
    "he",
    "she",
    "him",
    "her",
    "his",
    "hers",
    "this",
    "that",
    "these",
    "those",
}

# Guardrails for copula fact extraction: a subject longer than this, one that
# contains a verb or modal, or one led by a possessive pronoun is conversation
# about something, not a definitional statement worth learning.
MAX_FACT_SUBJECT_TOKENS = 4
POSSESSIVE_PRONOUNS = {"my", "your", "our", "their", "his", "her", "its"}

# spaCy dependency labels marking subjects and objects
SUBJECT_DEPS = {"nsubj", "nsubjpass"}
OBJECT_DEPS = {"dobj", "attr", "acomp", "oprd", "dative"}

# Articles dropped from the front of an extracted span
ARTICLES = {"a", "an", "the"}


# =============================================================================
# Sentiment (VADER)
# =============================================================================

# VADER compound-score thresholds (the standard cutoffs from the VADER paper).
POSITIVE_THRESHOLD = 0.05
NEGATIVE_THRESHOLD = -0.05

NEUTRAL = "neutral"
POSITIVE = "positive"
NEGATIVE = "negative"

NEUTRAL_SCORES = {"compound": 0.0, "pos": 0.0, "neu": 1.0, "neg": 0.0}


# =============================================================================
# Substitution maps
# =============================================================================

# Default contractions expansion map
DEFAULT_CONTRACTIONS: dict[str, str] = {
    "i'm": "i am",
    "i've": "i have",
    "i'll": "i will",
    "i'd": "i would",
    "you're": "you are",
    "you've": "you have",
    "you'll": "you will",
    "you'd": "you would",
    "he's": "he is",
    "she's": "she is",
    "it's": "it is",
    "we're": "we are",
    "we've": "we have",
    "we'll": "we will",
    "we'd": "we would",
    "they're": "they are",
    "they've": "they have",
    "they'll": "they will",
    "they'd": "they would",
    "that's": "that is",
    "there's": "there is",
    "here's": "here is",
    "what's": "what is",
    "who's": "who is",
    "where's": "where is",
    "when's": "when is",
    "why's": "why is",
    "how's": "how is",
    "isn't": "is not",
    "aren't": "are not",
    "wasn't": "was not",
    "weren't": "were not",
    "haven't": "have not",
    "hasn't": "has not",
    "hadn't": "had not",
    "won't": "will not",
    "wouldn't": "would not",
    "don't": "do not",
    "doesn't": "does not",
    "didn't": "did not",
    "can't": "cannot",
    "couldn't": "could not",
    "shouldn't": "should not",
    "mightn't": "might not",
    "mustn't": "must not",
    "let's": "let us",
    "ain't": "is not",
    "y'all": "you all",
    "gonna": "going to",
    "gotta": "got to",
    "wanna": "want to",
    "gimme": "give me",
    "lemme": "let me",
    "kinda": "kind of",
    "sorta": "sort of",
    "coulda": "could have",
    "woulda": "would have",
    "shoulda": "should have",
    "musta": "must have",
    # Apostrophe-less variants. Only forms that are not valid standalone English
    # words are included, to avoid corrupting normal text. Deliberately omitted:
    # "its", "were", "well", "ill", "id", "shed", "wed", "lets" (all real words).
    "im": "i am",
    "ive": "i have",
    "youre": "you are",
    "youve": "you have",
    "youll": "you will",
    "hes": "he is",
    "shes": "she is",
    "weve": "we have",
    "theyre": "they are",
    "theyve": "they have",
    "theyll": "they will",
    "thats": "that is",
    "theres": "there is",
    "heres": "here is",
    "whats": "what is",
    "whos": "who is",
    "wheres": "where is",
    "hows": "how is",
    "isnt": "is not",
    "arent": "are not",
    "wasnt": "was not",
    "werent": "were not",
    "havent": "have not",
    "hasnt": "has not",
    "hadnt": "had not",
    "dont": "do not",
    "doesnt": "does not",
    "didnt": "did not",
    "couldnt": "could not",
    "wouldnt": "would not",
    "shouldnt": "should not",
    "cant": "cannot",
    "wont": "will not",
}

# Person substitution (first person <-> second person)
# Used for transforming user input when echoing back
DEFAULT_PERSON: dict[str, str] = {
    "i": "you",
    "me": "you",
    "my": "your",
    "mine": "yours",
    "myself": "yourself",
    "am": "are",
    "was": "were",
    "i'm": "you are",
    "i've": "you have",
    "i'll": "you will",
    "i'd": "you would",
}

# Person2 substitution (second person -> first person)
# Reverse of person substitution
DEFAULT_PERSON2: dict[str, str] = {
    "you": "i",
    "your": "my",
    "yours": "mine",
    "yourself": "myself",
    "you're": "i am",
    "you've": "i have",
    "you'll": "i will",
    "you'd": "i would",
}

# Gender substitution: gendered pronouns map to singular they/them, so
# apply_gender rewrites text into gender-neutral pronouns rather than swapping
# he <-> she. Object "her" and possessive "her" both map to "them" (the
# possessive case is the less common one and a perfect split is not possible
# with a flat word map).
DEFAULT_GENDER: dict[str, str] = {
    "he": "they",
    "she": "they",
    "him": "them",
    "her": "them",
    "his": "their",
    "hers": "theirs",
    "himself": "themself",
    "herself": "themself",
}


# =============================================================================
# Scoring
# =============================================================================

# Overlap credit for a query keyword matched only through a WordNet synonym,
# relative to the 1.0 credit of an exact keyword match.
SYNONYM_OVERLAP_WEIGHT = 0.5


# =============================================================================
# Pattern matching
# =============================================================================

TOPIC_PRIORITY = 1000  # Having topic match adds significant priority
THAT_PRIORITY = 500  # Having that match adds priority

WILDCARD_TOKENS = {"*", "_", "#", "^"}


# =============================================================================
# Persistence
# =============================================================================

# Version constant for persistence format
PERSISTENCE_VERSION = 2
PERSISTENCE_MANIFEST_SCHEMA_VERSION = 1
PERSISTENCE_STATUS_SCHEMA_VERSION = 1
PERSISTENCE_MANIFEST_FIELDS = set(
    {
        "schema_version",
        "persistence_version",
        "response_state_schema_version",
        "feedback_state_schema_version",
        "identity_schema_version",
        "retrieval_normalization_version",
        "index_state_schema_version",
        "sparse_index_schema_version",
        "sparse_index_version",
        "semantic_index_schema_version",
        "semantic_index_version",
        "fusion_policy_schema_version",
        "fusion_policy_version",
        "feedback_policy_schema_version",
        "feedback_policy_version",
        "semantic_model_id",
        "semantic_model_version",
        "semantic_normalization_version",
        "reranker_model_version",
        "graph_vector_model_id",
    }
)


# =============================================================================
# Identity parsing
# =============================================================================

IDENTITY_CANONICAL_ID_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:[^\s\x00-\x1f\x7f]+$")
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
IDENTITY_CONTRACTION_RE = re.compile(
    r"(?<!\w)(?:" + "|".join(re.escape(key) for key in sorted(DEFAULT_CONTRACTIONS, key=len, reverse=True)) + r")(?!\w)"
)
IDENTITY_TECHNICAL_PUNCTUATION = set("._:/\\-+#@'<>=%|&*$")
IDENTITY_ALLOWED_RAW_WHITESPACE = set("\t\n\r")
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
IDENTITY_COMPARISON_OPERATOR_RE = re.compile(r"(?<![<>=!])(?:<=|>=|==|!=|<|>)(?![<>=])")
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
IDENTITY_TITLE_SEQUENCE_RE = re.compile(r"(?<![\w])(?:[A-Z][\w'’+#.-]*)(?:\s+(?:[A-Z][\w'’+#.-]*)){0,4}")
IDENTITY_QUOTED_SPAN_RE = re.compile(r"[\"“]([^\"”]{1,512})[\"”]")
IDENTITY_TECHNICAL_PATTERNS = (
    re.compile(r"(?<!\w)(?:RFC|ISO|IEC|IEEE|ECMA|PEP)\s*[-:]?\s*\d+(?:[.-]\d+)*(?!\w)", re.IGNORECASE),
    re.compile(r"(?<!\w)v?\d+(?:\.\d+){1,}(?!\w)", re.IGNORECASE),
    re.compile(r"(?<!\w)[A-Z][A-Z0-9]+(?:[-_][A-Z0-9]+)+(?!\w)"),
    re.compile(r"(?<!\w)[A-Za-z][A-Za-z0-9]*(?:\+\+|#)(?!\w)"),
    re.compile(r"(?<!\w)@[A-Za-z0-9_.-]+"),
    re.compile(r"(?<!\w)[A-Za-z][A-Za-z0-9_-]*(?:[.:/][A-Za-z0-9][A-Za-z0-9_-]*)+(?!\w)"),
)


# =============================================================================
# Dialogue interpretation
# =============================================================================

DIALOGUE_CLOSING_RE = re.compile(
    r"\b(?:goodbye|bye|farewell|good night|see you|talk (?:to you )?later|catch you later|"
    r"enough for (?:today|now)|done for (?:today|now)|stop here|leave it there)\b",
    re.IGNORECASE,
)
DIALOGUE_GRATITUDE_RE = re.compile(r"\b(?:thank you|thanks|much appreciated|appreciate it)\b", re.IGNORECASE)
DIALOGUE_GREETING_RE = re.compile(r"^\s*(?:hello|hi|hey|greetings|good morning|good afternoon|good evening)\b", re.IGNORECASE)
DIALOGUE_SELF_INTRODUCTION_RE = re.compile(r"\b(?:my name is|i am called|call me)\b", re.IGNORECASE)
DIALOGUE_TOPIC_SHIFT_RE = re.compile(
    r"\b(?:talk|speak|chat|discuss)\s+about\s+(.+)$|"
    r"\b(?:change|switch)\s+(?:the\s+)?topic\s+to\s+(.+)$|"
    r"\b(?:move|switch)\s+(?:on\s+)?to\s+(.+)$|"
    r"\b(?:return|go\s+back|come\s+back)\s+to\s+(.+)$",
    re.IGNORECASE,
)
DIALOGUE_QUESTION_TOPIC_RES = (
    re.compile(r"^\s*(?:what|who|where)\s+(?:is|are|was|were)\s+(.+?)\s*[?!.]*$", re.IGNORECASE),
    re.compile(r"^\s*how\s+\w+\s+(?:is|are|was|were)\s+(.+?)\s*[?!.]*$", re.IGNORECASE),
    re.compile(r"^\s*what\b.*\bwhen\s+you\s+(?:consider|think\s+about)\s+(.+?)\s*[?!.]*$", re.IGNORECASE),
)
DIALOGUE_EMOTION_RE = re.compile(
    r"\b(?:feel|feeling|felt|happy|sad|angry|anxious|excited|worried|wistful|afraid|upset|glad|lonely)\b",
    re.IGNORECASE,
)
DIALOGUE_OPINION_RE = re.compile(r"\b(?:i think|i believe|in my opinion|i prefer|i like|i dislike|seems to me)\b", re.IGNORECASE)
DIALOGUE_ACKNOWLEDGMENT_RE = re.compile(
    r"^\s*(?:yes|yeah|yep|no|nope|okay|ok|right|exactly|sure|agreed|understood|i see|got it|fair enough)" r"[.!\s]*$",
    re.IGNORECASE,
)
DIALOGUE_REFERRING_RE = re.compile(
    r"\b(?:he|her|hers|herself|him|himself|his|it|its|she|they|them|their|theirs|this|that|these|those)\b",
    re.IGNORECASE,
)
DIALOGUE_DISCOURSE_TOPIC_PREFIX_RE = re.compile(
    r"^\s*(?:after\b[^:\r\n]{1,80}:|because\b|before\s+we\b|for\s+my\s+part\b|"
    r"for\b[^:\r\n]{1,80}:|here\s+(?:is|are|was|were)\b|"
    r"one\s+more(?:\s+[\w'-]+){0,3}\s+thought\b|there\s+(?:is|are|was|were)\b|to\s+me\b)",
    re.IGNORECASE,
)
DIALOGUE_HEDGE_RE = re.compile(
    r"\b(?:maybe|perhaps|possibly|probably|supposedly|apparently|i guess|i suppose|might|could|would)\b",
    re.IGNORECASE,
)
DIALOGUE_TRANSIENT_RE = re.compile(
    r"\b(?:right now|at the moment|for now|today|tonight|currently|temporarily|lately|this morning|"
    r"this afternoon|this evening|this week|this month|this year)\b",
    re.IGNORECASE,
)
DIALOGUE_META_FACT_WORDS = set(
    {
        "answer",
        "chat",
        "claim",
        "conversation",
        "detail",
        "discussion",
        "example",
        "feeling",
        "idea",
        "message",
        "observation",
        "point",
        "prompt",
        "question",
        "remark",
        "reply",
        "response",
        "sentence",
        "statement",
        "test",
        "thought",
        "topic",
        "turn",
    }
)
DIALOGUE_VAGUE_FACT_SUBJECTS = set({"anything", "everything", "nothing", "something", "stuff", "thing", "things"})
DIALOGUE_TOPIC_TRAILERS = set({"again", "broadly", "instead", "next", "now", "please", "specifically"})
DIALOGUE_TOPIC_LEADING_MODIFIERS = set(
    {
        "actually",
        "apparently",
        "currently",
        "generally",
        "maybe",
        "no",
        "occasionally",
        "often",
        "okay",
        "perhaps",
        "possibly",
        "probably",
        "right",
        "sometimes",
        "supposedly",
        "today",
        "tonight",
        "typically",
        "usually",
        "well",
        "which",
        "whom",
        "whose",
        "yes",
    }
)
DIALOGUE_GRAMMATICAL_TOPIC_WORDS = set(
    {
        "am",
        "are",
        "be",
        "been",
        "being",
        "can",
        "could",
        "did",
        "do",
        "does",
        "had",
        "has",
        "have",
        "he",
        "her",
        "hers",
        "him",
        "his",
        "i",
        "it",
        "its",
        "me",
        "mine",
        "must",
        "my",
        "our",
        "ours",
        "shall",
        "she",
        "should",
        "that",
        "their",
        "theirs",
        "them",
        "these",
        "they",
        "this",
        "those",
        "us",
        "was",
        "we",
        "were",
        "will",
        "would",
        "you",
        "your",
        "yours",
    }
)
DIALOGUE_INVALID_TOPIC_WORDS = DIALOGUE_META_FACT_WORDS | DIALOGUE_VAGUE_FACT_SUBJECTS | DIALOGUE_GRAMMATICAL_TOPIC_WORDS
DIALOGUE_AMBIGUOUS_CAPITALIZED_LEADS = set(
    {"after", "allow", "because", "before", "for", "here", "if", "in", "one", "there", "to", "which", "with"}
)
DIALOGUE_ENTITY_LEADS = set(
    {
        "a",
        "an",
        "answer",
        "conversation",
        "exactly",
        "good",
        "goodbye",
        "hello",
        "hi",
        "how",
        "let",
        "let's",
        "my",
        "prompt",
        "question",
        "tell",
        "thank",
        "thanks",
        "the",
        "what",
        "when",
        "where",
        "who",
        "why",
    }
    | DIALOGUE_GRAMMATICAL_TOPIC_WORDS
    | DIALOGUE_TOPIC_LEADING_MODIFIERS
    | DIALOGUE_AMBIGUOUS_CAPITALIZED_LEADS
)
DIALOGUE_DISCOURSE_FACT_SUBJECT_LEADS = set({"actually", "no", "okay", "right", "well", "yes"})
DIALOGUE_QUALIFIED_FACT_SUBJECT_LEADS = set({"generally", "occasionally", "often", "sometimes", "typically", "usually"})
DIALOGUE_PERSONAL_FACT_OBJECT_WORDS = set({"i", "me", "mine", "my", "our", "ours", "us", "we", "you", "your", "yours"})
DIALOGUE_ENTITY_LABEL_PRIORITY = MappingProxyType({"PROPER_NOUN": 1, "TOPIC": 2, "SUBJECT": 3})
DIALOGUE_BROAD_PATTERNS = set({"THAT *", "THAT IS *", "THE *"})
LIFECYCLE_EXCLUSION_REASONS = MappingProxyType(
    {
        LifecycleDecisionReason.SUPERSEDED: EligibilityExclusionReason.LIFECYCLE_SUPERSEDED,
        LifecycleDecisionReason.INVALIDATED: EligibilityExclusionReason.LIFECYCLE_INVALIDATED,
        LifecycleDecisionReason.RETIRED: EligibilityExclusionReason.LIFECYCLE_RETIRED,
    }
)
