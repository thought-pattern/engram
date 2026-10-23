"""Graph fact-recall reads, Proposition projection contracts and queries, and graph operation bounds."""

from enum import StrEnum
from re import IGNORECASE, compile as re_compile

GRAPH_SPO_MEANING_GUARD = (
    "AND proposition.polarity = 'positive' "
    "AND proposition.modality_family = 'none' AND proposition.modality_operator = 'none' "
    "AND size([(proposition)-[:HAS_ARGUMENT]->(argument) | argument]) = 2 "
    "AND NOT exists((proposition)-[:HAS_QUALIFICATION]->()) "
    "AND NOT exists((proposition)-[:HAS_CONTEXT]->()) "
    "AND NOT exists((proposition)-[:HAS_APPLICABILITY_SCOPE]->()) "
)
# Shared clauses of the phrased fact-recall reads, which bind ``proposition``,
# ``predicate``, ``subject``, ``object``, ``support`` and ``assertion``.
GRAPH_FACT_ARGUMENTS = (
    "MATCH (proposition)-[:HAS_ARGUMENT]->(subject_binding:SemanticBinding)-[:BINDS_ENTITY]->(subject:Entity) "
    "MATCH (proposition)-[:HAS_ARGUMENT]->(object_binding:SemanticBinding)-[:BINDS_ENTITY]->(object:Entity) "
    "MATCH (proposition)-[support:SUPPORTED_BY]->(assertion:Assertion) "
    "WHERE subject_binding.role = 'subject' AND object_binding.role = 'object' "
)
GRAPH_FACT_ACTIVE = (
    "AND proposition.lifecycle_disposition = 'active' AND proposition.retired_at IS NULL "
    "AND assertion.lifecycle_disposition = 'active' AND assertion.retired_at IS NULL "
    "AND support.retired_at IS NULL "
)
GRAPH_FACT_VALID_AT_EVALUATION = (
    "AND (assertion.valid_time_start IS NULL "
    "OR assertion.valid_time_start <= datetime($evaluation_time)) "
    "AND (assertion.valid_time_end IS NULL "
    "OR datetime($evaluation_time) < assertion.valid_time_end) "
)
GRAPH_FACT_RETURN = (
    "RETURN DISTINCT proposition.id AS proposition_id, "
    "subject.primary_label AS subject, "
    "coalesce(predicate.label, predicate.canonical_id) AS predicate, "
    "object.primary_label AS object"
)
GRAPH_ENTITY_FACTS_QUERY = (
    "MATCH (proposition:Proposition)-[:USES_PREDICATE]->(predicate:Predicate) "
    + GRAPH_FACT_ARGUMENTS
    + "AND (toLower(subject.primary_label) = toLower($name) "
    "OR toLower($name) IN [alias IN coalesce(subject.aliases, []) | toLower(alias)] "
    "OR toLower(object.primary_label) = toLower($name) "
    "OR toLower($name) IN [alias IN coalesce(object.aliases, []) | toLower(alias)]) "
    + GRAPH_FACT_ACTIVE
    + GRAPH_SPO_MEANING_GUARD
    + GRAPH_FACT_VALID_AT_EVALUATION
    + GRAPH_FACT_RETURN
    + " LIMIT 5"
)
GRAPH_KEYWORD_FACTS_QUERY = (
    "MATCH (proposition:Proposition)-[:USES_PREDICATE]->(predicate:Predicate) "
    + GRAPH_FACT_ARGUMENTS
    + "AND (toLower(subject.primary_label) CONTAINS toLower($keyword) "
    "OR toLower(object.primary_label) CONTAINS toLower($keyword) "
    "OR toLower(coalesce(predicate.label, predicate.canonical_id)) CONTAINS toLower($keyword)) "
    + GRAPH_FACT_ACTIVE
    + GRAPH_SPO_MEANING_GUARD
    + GRAPH_FACT_VALID_AT_EVALUATION
    + GRAPH_FACT_RETURN
    + " LIMIT 3"
)
# The ANN fact read: the vector procedure call is why graph.execute exempts it from
# the write-clause check, which matches CALL.
VECTOR_SEARCH_PROPOSITIONS_QUERY = (
    "CALL vector_search.search($index_name, $limit, $query_embedding) YIELD node, distance "
    "WITH node AS proposition, 1.0 - distance AS similarity "
    "MATCH (proposition)-[:USES_PREDICATE]->(predicate:Predicate) "
    + GRAPH_FACT_ARGUMENTS
    + "AND similarity >= $min_similarity "
    + GRAPH_FACT_ACTIVE
    + GRAPH_SPO_MEANING_GUARD
    + GRAPH_FACT_VALID_AT_EVALUATION
    + "AND predicate.canonical_id <> 'generic_relation' "
    + GRAPH_FACT_RETURN
    + ", similarity ORDER BY similarity DESC, proposition.id"
)
TRIPLE_QUERY_OBJECT = (
    "MATCH (proposition:Proposition)-[:HAS_ARGUMENT]->(sb:SemanticBinding)-[:BINDS_ENTITY]->(s:Entity), "
    "(proposition)-[:USES_PREDICATE]->(p:Predicate), "
    "(proposition)-[:HAS_ARGUMENT]->(ob:SemanticBinding)-[:BINDS_ENTITY]->(o:Entity) "
    "MATCH (proposition)-[support:SUPPORTED_BY]->(assertion:Assertion) "
    "WHERE sb.role = 'subject' AND ob.role = 'object' "
    "AND (toLower(s.primary_label) = toLower($subject) "
    "OR toLower($subject) IN [a IN s.aliases | toLower(a)] "
    ") "
    "AND (toLower(p.label) = toLower($predicate) "
    "OR toLower($predicate) IN [y IN p.synonyms | toLower(y)]) "
    "AND proposition.lifecycle_disposition = 'active' AND proposition.retired_at IS NULL "
    "AND assertion.lifecycle_disposition = 'active' AND assertion.retired_at IS NULL "
    "AND support.retired_at IS NULL "
    + GRAPH_SPO_MEANING_GUARD
    + "AND (assertion.valid_time_start IS NULL OR assertion.valid_time_start <= datetime($evaluation_time)) "
    "AND (assertion.valid_time_end IS NULL OR datetime($evaluation_time) < assertion.valid_time_end) "
    "RETURN o.primary_label AS result LIMIT 1"
)
TRIPLE_QUERY_SUBJECT = (
    "MATCH (proposition:Proposition)-[:HAS_ARGUMENT]->(sb:SemanticBinding)-[:BINDS_ENTITY]->(s:Entity), "
    "(proposition)-[:USES_PREDICATE]->(p:Predicate), "
    "(proposition)-[:HAS_ARGUMENT]->(ob:SemanticBinding)-[:BINDS_ENTITY]->(o:Entity) "
    "MATCH (proposition)-[support:SUPPORTED_BY]->(assertion:Assertion) "
    "WHERE sb.role = 'subject' AND ob.role = 'object' "
    "AND (toLower(o.primary_label) = toLower($object) "
    "OR toLower($object) IN [a IN o.aliases | toLower(a)] "
    ") "
    "AND (toLower(p.label) = toLower($predicate) "
    "OR toLower($predicate) IN [y IN p.synonyms | toLower(y)]) "
    "AND proposition.lifecycle_disposition = 'active' AND proposition.retired_at IS NULL "
    "AND assertion.lifecycle_disposition = 'active' AND assertion.retired_at IS NULL "
    "AND support.retired_at IS NULL "
    + GRAPH_SPO_MEANING_GUARD
    + "AND (assertion.valid_time_start IS NULL OR assertion.valid_time_start <= datetime($evaluation_time)) "
    "AND (assertion.valid_time_end IS NULL OR datetime($evaluation_time) < assertion.valid_time_end) "
    "RETURN s.primary_label AS result LIMIT 1"
)
# Every runtime graph operation, connecting included, must finish within this
# time. The schema check runs outside the request path and gets longer.
GRAPH_TIMEOUT_SECONDS = 0.5
GRAPH_SCHEMA_CHECK_TIMEOUT_SECONDS = 30.0
WRITE_CLAUSE = re_compile(
    r"\b(CREATE|MERGE|DELETE|SET|REMOVE|DROP|DETACH|FOREACH|CALL|LOAD|" r"GRANT|DENY|REVOKE|ALTER|COPY|FREE)\b", IGNORECASE
)
VECTOR_INDEX_NAME = re_compile(r"^[A-Za-z][A-Za-z0-9_]{0,127}$")
MAX_PROPOSITION_PROJECTION_ROWS = 1_000
MAX_PROPOSITION_PROJECTION_EMBEDDING_DIMENSIONS = 65_536
MAX_PROPOSITION_PROJECTION_IDENTIFIER_BYTES = 256
MAX_PROPOSITION_PROJECTION_TERM_BYTES = 4_096
MAX_PROPOSITION_PROJECTION_TIMESTAMP_BYTES = 40
# Argument, qualification, context and applicability counts a projection may report.
MAX_PROPOSITION_PROJECTION_STRUCTURE_COUNT = 1_000_000
# Matches the 128-character ceiling of the VECTOR_INDEX_NAME pattern.
MAX_VECTOR_INDEX_NAME_BYTES = 128
PROPOSITION_POLARITIES = set({"positive", "negative"})
# Modality family -> the operators that family admits.
PROPOSITION_MODALITY_OPERATORS = {
    "none": set({"none"}),
    "alethic": set({"possible", "necessary", "impossible"}),
    "epistemic": set({"possible", "probable", "certain"}),
    "deontic": set({"obligation", "permission", "prohibition", "recommendation"}),
}
PROPOSITION_SEMANTIC_PROJECTION_FIELDS = (
    "polarity",
    "modality_family",
    "modality_operator",
    "argument_count",
    "qualification_count",
    "context_count",
    "applicability_count",
)
PROPOSITION_PROJECTION_FIELDS = set(
    {
        "proposition_id",
        "subject_entity_id",
        "predicate_id",
        "object_entity_id",
        "polarity",
        "modality_family",
        "modality_operator",
        "argument_count",
        "qualification_count",
        "context_count",
        "applicability_count",
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
        "structured_match",
        "structured_match_available",
        "semantic_similarity",
        "semantic_similarity_available",
    }
)
PROPOSITION_PROJECTION_RECORD_FIELDS = PROPOSITION_PROJECTION_FIELDS | set(
    {"projection_id", "vector_index_id", "vector_index_id_available"}
)
RELATION_ONE_HOP_RESULT_FIELDS = PROPOSITION_PROJECTION_FIELDS | {"object_label", "object_type", "predicate_cardinality"}
CANONICAL_ENTITY_MATCH_QUERY = (
    "MATCH (entity:Entity) "
    "OPTIONAL MATCH (:Proposition)-[:HAS_ARGUMENT]->(binding:SemanticBinding)-[:BINDS_ENTITY]->(entity) "
    "WITH entity, coalesce(entity.aliases, [])[0..12] AS aliases, "
    "[surface IN collect(DISTINCT binding.surface_form) WHERE surface IS NOT NULL][0..12] AS edge_surfaces "
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
PROPOSITION_PROJECTION_RETURN = (
    "RETURN DISTINCT c.id AS proposition_id, "
    "subject.canonical_id AS subject_entity_id, "
    "predicate.canonical_id AS predicate_id, "
    "object.canonical_id AS object_entity_id, "
    "c.polarity AS polarity, c.modality_family AS modality_family, "
    "c.modality_operator AS modality_operator, "
    "size([(c)-[:HAS_ARGUMENT]->(argument) | argument]) AS argument_count, "
    "size([(c)-[:HAS_QUALIFICATION]->(qualification) | qualification]) AS qualification_count, "
    "size([(c)-[:HAS_CONTEXT]->(context) | context]) AS context_count, "
    "size([(c)-[:HAS_APPLICABILITY_SCOPE]->(applicability) | applicability]) AS applicability_count, "
    "CASE WHEN c.lifecycle_disposition = 'invalidated' THEN c.retired_at ELSE null END AS invalidated_at, "
    "c.lifecycle_disposition = 'invalidated' AND c.retired_at IS NOT NULL AS invalidated_at_available, "
    "c.recorded_at AS system_from, c.recorded_at IS NOT NULL AS system_from_available, "
    "c.retired_at AS system_to, c.retired_at IS NOT NULL AS system_to_available, "
    "assertion.valid_time_start AS valid_from, "
    "assertion.valid_time_start IS NOT NULL AS valid_from_available, "
    "assertion.valid_time_end AS valid_to, "
    "assertion.valid_time_end IS NOT NULL AS valid_to_available, "
    "true AS predicate_canonical, "
    "c.ownership_category AS ownership_category, "
    "assertion.trust_category AS trust_category, "
    "assertion.trust_category IS NOT NULL AS trust_category_available, "
    "assertion.trust_score AS supplied_trust, "
    "assertion.trust_score IS NOT NULL AS supplied_trust_available, "
)
# A request's Assertion basis window (engram.evidence.assertion_basis_window):
# the basis valid time must end after basis_start and begin before basis_end
# (or at it, when inclusive). An unavailable bound leaves that side open.
ASSERTION_BASIS_WINDOW_FIELDS = set(
    {"basis_start", "basis_start_available", "basis_end", "basis_end_available", "basis_end_inclusive"}
)
UNCONSTRAINED_ASSERTION_BASIS = {
    "basis_start": "",
    "basis_start_available": False,
    "basis_end": "",
    "basis_end_available": False,
    "basis_end_inclusive": False,
}
ASSERTION_BASIS_ELIGIBLE = (
    "(CASE WHEN $basis_start_available "
    "THEN assertion.valid_time_end IS NULL OR assertion.valid_time_end > datetime($basis_start) ELSE true END) "
    "AND (CASE WHEN $basis_end_available "
    "THEN assertion.valid_time_start IS NULL OR assertion.valid_time_start < datetime($basis_end) "
    "OR ($basis_end_inclusive AND assertion.valid_time_start = datetime($basis_end)) ELSE true END)"
)
# Each Proposition projects the lowest-ID active Assertion eligible for the
# request's window, so discovery and by-ID revalidation agree on one basis. With
# no eligible basis it projects the lowest-ID active one, which native
# eligibility then excludes with its temporal reason.
PROPOSITION_PROJECTION_ASSERTION_SELECTION = (
    "WITH c, subject, predicate, object, "
    "min(CASE WHEN " + ASSERTION_BASIS_ELIGIBLE + " THEN assertion.id END) AS eligible_assertion_id, "
    "min(assertion.id) AS first_assertion_id "
    "MATCH (assertion:Assertion) WHERE assertion.id = coalesce(eligible_assertion_id, first_assertion_id) "
)
VECTOR_PROPOSITION_ASSERTION_SELECTION = (
    "WITH c, subject, predicate, object, similarity, min(assertion.id) AS selected_assertion_id "
    "MATCH (assertion:Assertion) WHERE assertion.id = selected_assertion_id "
)
STRUCTURED_ENTITY_PROPOSITION_PROJECTION_QUERY = (
    "MATCH (c:Proposition)-[:HAS_ARGUMENT]->(subject_binding:SemanticBinding)-[:BINDS_ENTITY]->(subject:Entity) "
    "MATCH (c)-[:USES_PREDICATE]->(predicate:Predicate) "
    "MATCH (c)-[:HAS_ARGUMENT]->(object_binding:SemanticBinding)-[:BINDS_ENTITY]->(object:Entity) "
    "MATCH (c)-[support:SUPPORTED_BY]->(assertion:Assertion) "
    "WHERE subject_binding.role = 'subject' AND object_binding.role = 'object' "
    "AND (toLower(subject.primary_label) = toLower($value) "
    "OR toLower($value) IN [alias IN coalesce(subject.aliases, []) | toLower(alias)] "
    "OR toLower(object.primary_label) = toLower($value) "
    "OR toLower($value) IN [alias IN coalesce(object.aliases, []) | toLower(alias)]) "
    "AND c.lifecycle_disposition = 'active' AND c.retired_at IS NULL "
    "AND assertion.lifecycle_disposition = 'active' AND assertion.retired_at IS NULL "
    "AND support.retired_at IS NULL AND predicate.canonical_id <> 'generic_relation' "
    + PROPOSITION_PROJECTION_ASSERTION_SELECTION
    + PROPOSITION_PROJECTION_RETURN
    + "1.0 AS structured_match, true AS structured_match_available, "
    "0.0 AS semantic_similarity, false AS semantic_similarity_available "
    "ORDER BY c.id LIMIT $limit"
)
STRUCTURED_KEYWORD_PROPOSITION_PROJECTION_QUERY = (
    "MATCH (c:Proposition)-[:HAS_ARGUMENT]->(subject_binding:SemanticBinding)-[:BINDS_ENTITY]->(subject:Entity) "
    "MATCH (c)-[:USES_PREDICATE]->(predicate:Predicate) "
    "MATCH (c)-[:HAS_ARGUMENT]->(object_binding:SemanticBinding)-[:BINDS_ENTITY]->(object:Entity) "
    "MATCH (c)-[support:SUPPORTED_BY]->(assertion:Assertion) "
    "WHERE subject_binding.role = 'subject' AND object_binding.role = 'object' "
    "AND (toLower(subject.primary_label) CONTAINS toLower($value) "
    "OR toLower(object.primary_label) CONTAINS toLower($value) "
    "OR toLower(coalesce(predicate.label, predicate.canonical_id)) "
    "CONTAINS toLower($value)) "
    "AND c.lifecycle_disposition = 'active' AND c.retired_at IS NULL "
    "AND assertion.lifecycle_disposition = 'active' AND assertion.retired_at IS NULL "
    "AND support.retired_at IS NULL AND predicate.canonical_id <> 'generic_relation' "
    + PROPOSITION_PROJECTION_ASSERTION_SELECTION
    + PROPOSITION_PROJECTION_RETURN
    + "1.0 AS structured_match, true AS structured_match_available, "
    "0.0 AS semantic_similarity, false AS semantic_similarity_available "
    "ORDER BY c.id LIMIT $limit"
)
VECTOR_PROPOSITION_PROJECTION_QUERY = (
    "CALL vector_search.search($index_name, $limit, $query_embedding) YIELD node, distance "
    "WITH node AS c, 1.0 - distance AS similarity "
    "MATCH (c)-[:HAS_ARGUMENT]->(subject_binding:SemanticBinding)-[:BINDS_ENTITY]->(subject:Entity) "
    "MATCH (c)-[:USES_PREDICATE]->(predicate:Predicate) "
    "MATCH (c)-[:HAS_ARGUMENT]->(object_binding:SemanticBinding)-[:BINDS_ENTITY]->(object:Entity) "
    "MATCH (c)-[support:SUPPORTED_BY]->(assertion:Assertion) "
    "WHERE subject_binding.role = 'subject' AND object_binding.role = 'object' "
    "AND similarity >= $min_similarity "
    "AND c.lifecycle_disposition = 'active' AND c.retired_at IS NULL "
    "AND assertion.lifecycle_disposition = 'active' AND assertion.retired_at IS NULL "
    "AND support.retired_at IS NULL "
    "AND " + ASSERTION_BASIS_ELIGIBLE + " "
    "AND predicate.canonical_id <> 'generic_relation' "
    + VECTOR_PROPOSITION_ASSERTION_SELECTION
    + PROPOSITION_PROJECTION_RETURN
    + "0.0 AS structured_match, false AS structured_match_available, "
    "similarity AS semantic_similarity, true AS semantic_similarity_available "
    "ORDER BY semantic_similarity DESC, c.id"
)
PROPOSITION_PROJECTION_BY_ID_QUERY = (
    "MATCH (c:Proposition)-[:HAS_ARGUMENT]->(subject_binding:SemanticBinding)-[:BINDS_ENTITY]->(subject:Entity) "
    "MATCH (c)-[:USES_PREDICATE]->(predicate:Predicate) "
    "MATCH (c)-[:HAS_ARGUMENT]->(object_binding:SemanticBinding)-[:BINDS_ENTITY]->(object:Entity) "
    "MATCH (c)-[support:SUPPORTED_BY]->(assertion:Assertion) "
    "WHERE subject_binding.role = 'subject' AND object_binding.role = 'object' "
    "AND c.id = $proposition_id AND c.lifecycle_disposition = 'active' "
    "AND c.retired_at IS NULL AND assertion.lifecycle_disposition = 'active' "
    "AND assertion.retired_at IS NULL AND support.retired_at IS NULL "
    + PROPOSITION_PROJECTION_ASSERTION_SELECTION
    + PROPOSITION_PROJECTION_RETURN
    + "0.0 AS structured_match, false AS structured_match_available, "
    "0.0 AS semantic_similarity, false AS semantic_similarity_available "
    "ORDER BY c.id LIMIT 2"
)
RELATION_ONE_HOP_PROPOSITION_PROJECTION_QUERY = (
    "MATCH (c:Proposition)-[:HAS_ARGUMENT]->(subject_binding:SemanticBinding)-[:BINDS_ENTITY]->(subject:Entity) "
    "MATCH (c)-[:USES_PREDICATE]->(predicate:Predicate) "
    "MATCH (c)-[:HAS_ARGUMENT]->(object_binding:SemanticBinding)-[:BINDS_ENTITY]->(object:Entity) "
    "MATCH (c)-[support:SUPPORTED_BY]->(assertion:Assertion) "
    "WHERE subject_binding.role = 'subject' AND object_binding.role = 'object' "
    "AND subject.canonical_id = $subject_entity_id AND predicate.canonical_id = $predicate_id "
    "AND ($include_historical = true OR (c.lifecycle_disposition = 'active' AND c.retired_at IS NULL)) "
    "AND assertion.lifecycle_disposition = 'active' AND assertion.retired_at IS NULL "
    "AND support.retired_at IS NULL AND predicate.canonical_id <> 'generic_relation' "
    + PROPOSITION_PROJECTION_ASSERTION_SELECTION
    + PROPOSITION_PROJECTION_RETURN
    + "1.0 AS structured_match, true AS structured_match_available, "
    "0.0 AS semantic_similarity, false AS semantic_similarity_available, "
    "object.primary_label AS object_label, coalesce(object.entity_type, 'UNKNOWN') AS object_type, "
    "coalesce(predicate.cardinality, 'UNKNOWN') AS predicate_cardinality "
    "ORDER BY c.id LIMIT $limit"
)
MAX_STRUCTURED_PROPOSITION_PROJECTION_TERMS = 3


class PropositionProjectionQuery(StrEnum):
    """Allow-listed fixed query identifiers for full Proposition projection."""

    STRUCTURED_ENTITY = "structured_entity_proposition_projection"
    STRUCTURED_KEYWORD = "structured_keyword_proposition_projection"
    RELATION_ONE_HOP = "relation_one_hop_proposition_projection"
    VECTOR = "vector_proposition_projection"
    BY_ID = "proposition_projection_by_id"
