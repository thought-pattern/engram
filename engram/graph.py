"""Knowledge Graph integration for ENGRAM.

Connects to a Bolt graph database (Memgraph, Neo4j, or another Bolt/Cypher
store) through the neo4j driver. Runtime access is strictly read-only: every
public execution path rejects mutating Cypher before reaching the database.

Graph unavailability and query failure remain explicit; an empty row list means
only that a successful read matched no records. Every operation is bounded by
``GRAPH_TIMEOUT_SECONDS``; see ``MemGraphConnection``.
"""

from asyncio import (
    all_tasks as asyncio_all_tasks,
    current_task as asyncio_current_task,
    get_running_loop as asyncio_get_running_loop,
    new_event_loop as asyncio_new_event_loop,
    run_coroutine_threadsafe as asyncio_run_coroutine_threadsafe,
    wait as asyncio_wait,
    wait_for as asyncio_wait_for,
)
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from logging import getLogger as logging_getLogger
from math import isfinite as math_isfinite
from threading import Lock as threading_Lock, RLock as threading_RLock, Thread as threading_Thread
from uuid import UUID

from neo4j import AsyncGraphDatabase
from neo4j.exceptions import ServiceUnavailable, SessionExpired
from neo4j.time import Date as neo4j_Date, DateTime as neo4j_DateTime, Time as neo4j_Time

from engram.constants import (
    ASSERTION_BASIS_WINDOW_FIELDS,
    CANONICAL_ENTITY_MATCH_FIELDS,
    CANONICAL_ENTITY_MATCH_QUERY,
    CANONICAL_PREDICATE_MATCH_FIELDS,
    CANONICAL_PREDICATE_MATCH_QUERY,
    GRAPH_SPO_MEANING_GUARD,
    GRAPH_TIMEOUT_SECONDS,
    MAX_GRAPH_READ_ROWS,
    MAX_PROPOSITION_PROJECTION_EMBEDDING_DIMENSIONS,
    MAX_PROPOSITION_PROJECTION_IDENTIFIER_BYTES,
    MAX_PROPOSITION_PROJECTION_ROWS,
    MAX_PROPOSITION_PROJECTION_TERM_BYTES,
    MAX_PROPOSITION_PROJECTION_TIMESTAMP_BYTES,
    MAX_RELATION_CANDIDATES,
    MAX_RELATION_LABEL_BYTES,
    MAX_RELATION_PLAN_ROWS,
    MAX_RELATION_SURFACES,
    PROPOSITION_PROJECTION_BY_ID_QUERY,
    PROPOSITION_PROJECTION_FIELDS,
    PROPOSITION_PROJECTION_RECORD_FIELDS,
    RELATION_ONE_HOP_PROPOSITION_PROJECTION_QUERY,
    RELATION_ONE_HOP_RESULT_FIELDS,
    STRUCTURED_ENTITY_PROPOSITION_PROJECTION_QUERY,
    STRUCTURED_KEYWORD_PROPOSITION_PROJECTION_QUERY,
    UNCONSTRAINED_ASSERTION_BASIS,
    VECTOR_INDEX_NAME,
    VECTOR_PROPOSITION_PROJECTION_QUERY,
    WRITE_CLAUSE,
    ExpectedObjectType,
    PredicateCardinality,
    PropositionProjectionQuery,
)
from engram.errors import InvalidRequestError, ResourceExhaustedError
from engram.schema_admin import verify_schema
from engram.schema_catalog import packaged_schema
from engram.scope import validate_visibility_scope, visibility_parameters
from engram.validation import parse_utc_timestamp, require_bool, require_identifier, require_text, utc_datetime

logger = logging_getLogger(__name__)


VECTOR_SEARCH_PROPOSITIONS_QUERY = (
    """
    CALL vector_search.search(
        $index_name, $limit, $query_embedding
    ) YIELD node, distance
    WITH node AS proposition, 1.0 - distance AS similarity
    MATCH (proposition)-[:USES_PREDICATE]->(predicate:Predicate)
    MATCH (proposition)-[:HAS_ARGUMENT]->(subject_binding:SemanticBinding)-[:BINDS_ENTITY]->(subject:Entity)
    MATCH (proposition)-[:HAS_ARGUMENT]->(object_binding:SemanticBinding)-[:BINDS_ENTITY]->(object:Entity)
    MATCH (proposition)-[support:SUPPORTED_BY]->(assertion:Assertion)
    WHERE subject_binding.role = 'subject' AND object_binding.role = 'object'
      AND similarity >= $min_similarity
      AND proposition.lifecycle_disposition = 'active'
      AND proposition.retired_at IS NULL
      AND assertion.lifecycle_disposition = 'active'
      AND assertion.retired_at IS NULL
      AND support.retired_at IS NULL
    """
    + GRAPH_SPO_MEANING_GUARD
    + """
      AND (assertion.valid_time_start IS NULL
        OR assertion.valid_time_start <= datetime($evaluation_time))
      AND (assertion.valid_time_end IS NULL
        OR datetime($evaluation_time) < assertion.valid_time_end)
      AND predicate.canonical_id <> 'generic_relation'
      AND (proposition.visibility_kind = 'global'
        OR ($visibility_kind IN ['company', 'engagement']
          AND proposition.visibility_kind = 'company'
          AND proposition.company_id = $company_id)
        OR ($visibility_kind = 'engagement'
          AND proposition.visibility_kind = 'engagement'
          AND proposition.company_id = $company_id
          AND proposition.customer_id = $customer_id
          AND proposition.engagement_id = $engagement_id))
    RETURN DISTINCT proposition.id AS proposition_id,
           subject.primary_label AS subject,
           coalesce(predicate.label, predicate.canonical_id) AS predicate,
           object.primary_label AS object,
           similarity
    ORDER BY similarity DESC, proposition.id
"""
)

FIXED_READ_PROCEDURE_QUERIES = (
    VECTOR_PROPOSITION_PROJECTION_QUERY,
    VECTOR_SEARCH_PROPOSITIONS_QUERY,
)


def is_connection_error(err: BaseException) -> bool:
    """Distinguish a lost or unresponsive connection from a failed query.

    True means the connection cannot be trusted and is replaced after the
    turn; this includes timeouts. Query-level errors (syntax, missing
    procedure, constraint) arrive over a healthy connection and return False.
    """
    result = isinstance(err, (ServiceUnavailable, SessionExpired, OSError))
    return result


def native_value(value: object) -> object:
    """Return driver temporal values as Python ``datetime``, ``date``, and ``time``.

    Projection decoding expects Python types; the driver returns its own
    nanosecond-precision classes.
    """
    if isinstance(value, list):
        result = [native_value(item) for item in value]
        return result
    if isinstance(value, dict):
        result = {key: native_value(item) for key, item in value.items()}
        return result
    if isinstance(value, (neo4j_DateTime, neo4j_Date, neo4j_Time)):
        result = value.to_native()
        return result
    return value


async def read_rows(driver, statement: str, parameters: dict, max_rows: int) -> list[dict]:
    """Run one auto-commit statement and return its rows as column -> value dicts.

    Records are consumed one at a time, so a read that would exceed ``max_rows``
    is refused before its remaining rows are fetched, decoded or rendered.
    """
    rows = []
    async with driver.session() as session:
        result = await session.run(statement, parameters)
        columns = list(result.keys())
        async for record in result:
            if len(rows) >= max_rows:
                raise ResourceExhaustedError(f"graph read exceeded the allowance of {max_rows} rows")
            rows.append(dict(zip(columns, (native_value(value) for value in record.values()), strict=False)))
    return rows


def projection_int(value: object, name: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise InvalidRequestError(f"{name} must be an integer from {minimum} through {maximum}")
    return value


def projection_score(value: object, available: bool, name: str) -> float:
    if not available and value is None:
        result = 0.0
        return result
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise InvalidRequestError(f"{name} must be numeric")
    score = float(value)
    if not math_isfinite(score) or not 0.0 <= score <= 1.0:
        raise InvalidRequestError(f"{name} must be finite and from 0 through 1")
    if not available and score != 0.0:
        raise InvalidRequestError(f"{name} must be zero when unavailable")
    return score


def projection_timestamp(value: object, available: bool, name: str) -> str:
    if not available:
        if value is None or value == "":
            result = ""
            return result
        raise InvalidRequestError(f"{name} must be empty when unavailable")
    if isinstance(value, datetime):
        if not value.tzinfo or value.utcoffset() != timedelta(0):
            raise InvalidRequestError(f"{name} must be timezone-aware UTC")
        text = value.isoformat().replace("+00:00", "Z")
    else:
        text = require_text(value, name, MAX_PROPOSITION_PROJECTION_TIMESTAMP_BYTES, allow_empty=False)
    parse_utc_timestamp(text, name)
    return text


def assertion_basis_parameters(window: object) -> dict:
    """Validate a request's Assertion basis window as fixed projection query parameters."""
    if not isinstance(window, dict) or set(window) != ASSERTION_BASIS_WINDOW_FIELDS:
        raise InvalidRequestError("Assertion basis window has an invalid shape")
    flags = {name: window.get(name, False) for name in ("basis_start_available", "basis_end_available", "basis_end_inclusive")}
    if not all(isinstance(value, bool) for value in flags.values()):
        raise InvalidRequestError("Assertion basis window flags must be booleans")
    basis_start = projection_timestamp(
        window.get("basis_start", ""), flags.get("basis_start_available", False), "Assertion basis start"
    )
    basis_end = projection_timestamp(window.get("basis_end", ""), flags.get("basis_end_available", False), "Assertion basis end")
    result = {**flags, "basis_start": basis_start, "basis_end": basis_end}
    return result


def optional_projection_text(value: object, available: bool, name: str, maximum_bytes: int) -> str:
    if not available:
        if value is None or value == "":
            result = ""
            return result
        raise InvalidRequestError(f"{name} must be empty when unavailable")
    result = require_text(value, name, maximum_bytes, allow_empty=False)
    return result


def projection_text_collection(value: object, name: str) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)) or len(value) > MAX_RELATION_SURFACES:
        raise InvalidRequestError(f"{name} must be a collection of at most {MAX_RELATION_SURFACES} strings")
    normalized = tuple(require_text(item, f"{name} value", MAX_RELATION_LABEL_BYTES, allow_empty=False) for item in value)
    if normalized != tuple(dict.fromkeys(normalized)):
        raise InvalidRequestError(f"{name} must contain unique values")
    return normalized


def projection_object_type(value: object, name: str) -> ExpectedObjectType:
    raw = require_text(value, name, 32, allow_empty=False).upper()
    try:
        result = ExpectedObjectType(raw)
    except ValueError as error:
        raise InvalidRequestError(f"{name} is unsupported") from error
    return result


def projection_entity_object_type(value: object, name: str) -> ExpectedObjectType:
    """Map the graph's open entity taxonomy onto Engram's coarse answer types."""
    raw = require_text(value, name, 32, allow_empty=False).upper()
    try:
        result = ExpectedObjectType(raw)
    except ValueError:
        result = ExpectedObjectType.ENTITY
    return result


def projection_cardinality(value: object, name: str) -> PredicateCardinality:
    raw = require_text(value, name, 32, allow_empty=False).upper()
    try:
        result = PredicateCardinality(raw)
    except ValueError as error:
        raise InvalidRequestError(f"{name} is unsupported") from error
    return result


def canonical_entity_match_from_graph_row(value: object) -> dict:
    """Decode one exact canonical entity match row without arbitrary graph properties."""
    if not isinstance(value, Mapping) or set(value) != CANONICAL_ENTITY_MATCH_FIELDS:
        raise InvalidRequestError("canonical entity match row has invalid fields")
    # The exact field set is checked above, so every typed default below is unreachable.
    result: dict = {
        "canonical_id": require_identifier(
            value.get("canonical_id", ""), "canonical entity ID", maximum_bytes=MAX_PROPOSITION_PROJECTION_IDENTIFIER_BYTES
        ),
        "primary_label": require_text(
            value.get("primary_label", ""), "canonical entity primary label", MAX_RELATION_LABEL_BYTES, allow_empty=False
        ),
        "aliases": projection_text_collection(value.get("aliases", []), "canonical entity aliases"),
        "edge_surfaces": projection_text_collection(value.get("edge_surfaces", []), "canonical entity edge surfaces"),
        "entity_type": projection_entity_object_type(value.get("entity_type", ""), "canonical entity type"),
    }
    return result


def canonical_predicate_match_from_graph_row(value: object) -> dict:
    """Decode one exact canonical Predicate match row."""
    if not isinstance(value, Mapping) or set(value) != CANONICAL_PREDICATE_MATCH_FIELDS:
        raise InvalidRequestError("canonical Predicate match row has invalid fields")
    # The exact field set is checked above, so every typed default below is unreachable.
    result: dict = {
        "canonical_id": require_identifier(
            value.get("canonical_id", ""), "canonical Predicate ID", maximum_bytes=MAX_PROPOSITION_PROJECTION_IDENTIFIER_BYTES
        ),
        "primary_label": require_text(
            value.get("primary_label", ""), "canonical Predicate primary label", MAX_RELATION_LABEL_BYTES, allow_empty=False
        ),
        "synonyms": projection_text_collection(value.get("synonyms", []), "canonical Predicate synonyms"),
        "object_type": projection_object_type(value.get("object_type", ""), "canonical Predicate object type"),
    }
    return result


def proposition_projection(
    *,
    proposition_id: object,
    subject_entity_id: object,
    predicate_id: object,
    object_entity_id: object,
    polarity: object,
    modality_family: object,
    modality_operator: object,
    argument_count: object,
    qualification_count: object,
    context_count: object,
    applicability_count: object,
    invalidated_at: object,
    invalidated_at_available: object,
    system_from: object,
    system_from_available: object,
    system_to: object,
    system_to_available: object,
    valid_from: object,
    valid_from_available: object,
    valid_to: object,
    valid_to_available: object,
    predicate_canonical: object,
    ownership_category: object,
    trust_category: object,
    trust_category_available: object,
    supplied_trust: object,
    supplied_trust_available: object,
    structured_match: object,
    structured_match_available: object,
    semantic_similarity: object,
    semantic_similarity_available: object,
    projection_id: object,
    vector_index_id: object,
    vector_index_id_available: object,
) -> dict:
    """Build one validated strict Proposition projection dictionary."""
    normalized_proposition_id = require_identifier(
        proposition_id, "Proposition projection proposition_id", maximum_bytes=MAX_PROPOSITION_PROJECTION_IDENTIFIER_BYTES
    )
    normalized_subject_id = require_identifier(
        subject_entity_id, "Proposition projection subject_entity_id", maximum_bytes=MAX_PROPOSITION_PROJECTION_IDENTIFIER_BYTES
    )
    normalized_predicate_id = require_identifier(
        predicate_id, "Proposition projection predicate_id", maximum_bytes=MAX_PROPOSITION_PROJECTION_IDENTIFIER_BYTES
    )
    normalized_object_id = require_identifier(
        object_entity_id, "Proposition projection object_entity_id", maximum_bytes=MAX_PROPOSITION_PROJECTION_IDENTIFIER_BYTES
    )
    normalized_polarity = require_text(polarity, "Proposition projection polarity", 16, allow_empty=False)
    if normalized_polarity not in {"positive", "negative"}:
        raise InvalidRequestError("Proposition projection polarity is unsupported")
    normalized_modality_family = require_text(modality_family, "Proposition projection modality family", 16, allow_empty=False)
    normalized_modality_operator = require_text(
        modality_operator, "Proposition projection modality operator", 32, allow_empty=False
    )
    modal_operators = {
        "none": {"none"},
        "alethic": {"possible", "necessary", "impossible"},
        "epistemic": {"possible", "probable", "certain"},
        "deontic": {"obligation", "permission", "prohibition", "recommendation"},
    }
    if normalized_modality_operator not in modal_operators.get(normalized_modality_family, set()):
        raise InvalidRequestError("Proposition projection modality is unsupported")
    normalized_argument_count = projection_int(argument_count, "Proposition projection argument_count", 0, 1_000_000)
    normalized_qualification_count = projection_int(qualification_count, "Proposition projection qualification_count", 0, 1_000_000)
    normalized_context_count = projection_int(context_count, "Proposition projection context_count", 0, 1_000_000)
    normalized_applicability_count = projection_int(applicability_count, "Proposition projection applicability_count", 0, 1_000_000)
    normalized_invalidated_available = require_bool(invalidated_at_available, "Proposition projection invalidated_at_available")
    normalized_system_from_available = require_bool(system_from_available, "Proposition projection system_from_available")
    normalized_system_to_available = require_bool(system_to_available, "Proposition projection system_to_available")
    normalized_valid_from_available = require_bool(valid_from_available, "Proposition projection valid_from_available")
    normalized_valid_to_available = require_bool(valid_to_available, "Proposition projection valid_to_available")
    normalized_invalidated_at = projection_timestamp(
        invalidated_at, normalized_invalidated_available, "Proposition projection invalidated_at"
    )
    normalized_system_from = projection_timestamp(
        system_from, normalized_system_from_available, "Proposition projection system_from"
    )
    normalized_system_to = projection_timestamp(system_to, normalized_system_to_available, "Proposition projection system_to")
    normalized_valid_from = projection_timestamp(valid_from, normalized_valid_from_available, "Proposition projection valid_from")
    normalized_valid_to = projection_timestamp(valid_to, normalized_valid_to_available, "Proposition projection valid_to")
    if (
        normalized_system_from_available
        and normalized_system_to_available
        and utc_datetime(normalized_system_from) >= utc_datetime(normalized_system_to)
    ):
        raise InvalidRequestError("Proposition projection system_from must be earlier than system_to")
    if (
        normalized_valid_from_available
        and normalized_valid_to_available
        and utc_datetime(normalized_valid_from) >= utc_datetime(normalized_valid_to)
    ):
        raise InvalidRequestError("Proposition projection valid_from must be earlier than valid_to")
    normalized_predicate_canonical = require_bool(predicate_canonical, "Proposition projection predicate_canonical")
    normalized_ownership = require_text(ownership_category, "Proposition projection ownership_category", 32, allow_empty=False)
    if normalized_ownership not in {"PUBLIC", "COMPANY", "CUSTOMER"}:
        raise InvalidRequestError("Proposition projection ownership_category is unsupported")
    normalized_trust_category_available = require_bool(trust_category_available, "Proposition projection trust_category_available")
    normalized_trust_category = optional_projection_text(
        trust_category,
        normalized_trust_category_available,
        "Proposition projection trust_category",
        96,
    )
    normalized_trust_available = require_bool(supplied_trust_available, "Proposition projection supplied_trust_available")
    normalized_trust = projection_score(supplied_trust, normalized_trust_available, "Proposition projection supplied_trust")
    normalized_structured_available = require_bool(structured_match_available, "Proposition projection structured_match_available")
    normalized_structured = projection_score(
        structured_match, normalized_structured_available, "Proposition projection structured_match"
    )
    normalized_semantic_available = require_bool(
        semantic_similarity_available, "Proposition projection semantic_similarity_available"
    )
    normalized_semantic = projection_score(
        semantic_similarity, normalized_semantic_available, "Proposition projection semantic_similarity"
    )
    if not isinstance(projection_id, PropositionProjectionQuery):
        raise InvalidRequestError("Proposition projection projection_id must be a PropositionProjectionQuery")
    normalized_vector_available = require_bool(vector_index_id_available, "Proposition projection vector_index_id_available")
    normalized_vector_id = require_text(
        vector_index_id, "Proposition projection vector_index_id", 128, allow_empty=not normalized_vector_available
    )
    if normalized_vector_available and not VECTOR_INDEX_NAME.fullmatch(normalized_vector_id):
        raise InvalidRequestError("Proposition projection vector_index_id is invalid")
    if not normalized_vector_available and normalized_vector_id:
        raise InvalidRequestError("Proposition projection vector_index_id must be empty when unavailable")
    if projection_id == PropositionProjectionQuery.VECTOR:
        if normalized_structured_available or not normalized_semantic_available or not normalized_vector_available:
            raise InvalidRequestError("vector Proposition projection measurements or index provenance are inconsistent")
    elif projection_id == PropositionProjectionQuery.BY_ID:
        if normalized_structured_available or normalized_semantic_available or normalized_vector_available:
            raise InvalidRequestError("by-ID Proposition projection measurements or index provenance are inconsistent")
    elif not normalized_structured_available or normalized_semantic_available or normalized_vector_available:
        raise InvalidRequestError("structured Proposition projection measurements or index provenance are inconsistent")
    result: dict = {
        "proposition_id": normalized_proposition_id,
        "subject_entity_id": normalized_subject_id,
        "predicate_id": normalized_predicate_id,
        "object_entity_id": normalized_object_id,
        "polarity": normalized_polarity,
        "modality_family": normalized_modality_family,
        "modality_operator": normalized_modality_operator,
        "argument_count": normalized_argument_count,
        "qualification_count": normalized_qualification_count,
        "context_count": normalized_context_count,
        "applicability_count": normalized_applicability_count,
        "invalidated_at": normalized_invalidated_at,
        "invalidated_at_available": normalized_invalidated_available,
        "system_from": normalized_system_from,
        "system_from_available": normalized_system_from_available,
        "system_to": normalized_system_to,
        "system_to_available": normalized_system_to_available,
        "valid_from": normalized_valid_from,
        "valid_from_available": normalized_valid_from_available,
        "valid_to": normalized_valid_to,
        "valid_to_available": normalized_valid_to_available,
        "predicate_canonical": normalized_predicate_canonical,
        "ownership_category": normalized_ownership,
        "trust_category": normalized_trust_category,
        "trust_category_available": normalized_trust_category_available,
        "supplied_trust": normalized_trust,
        "supplied_trust_available": normalized_trust_available,
        "structured_match": normalized_structured,
        "structured_match_available": normalized_structured_available,
        "semantic_similarity": normalized_semantic,
        "semantic_similarity_available": normalized_semantic_available,
        "projection_id": projection_id,
        "vector_index_id": normalized_vector_id,
        "vector_index_id_available": normalized_vector_available,
    }
    return result


# Validated projections by exact input: every field, value, and value type.
# A projection is a flat map of scalars and validation is a pure function of
# them, so an equal input always validates to an equal result. Including the
# type keeps True apart from 1 and an enum apart from its string value.
VALIDATED_PROJECTIONS: dict[tuple, dict] = {}
MAX_VALIDATED_PROJECTIONS = 4_096


def validate_proposition_projection(value: object) -> dict:
    """Revalidate and copy one in-memory Proposition projection.

    Evidence handling validates the same projection several times per record,
    so results are remembered by exact input; each call still returns its own copy.
    """
    if not isinstance(value, Mapping):
        raise InvalidRequestError("Proposition projection must be an object")
    key = ()
    cached = {}
    cacheable = False
    try:
        key = (tuple(value), tuple(value.values()), tuple(map(type, value.values())))
        cached = VALIDATED_PROJECTIONS.get(key, {})
        cacheable = True
    except TypeError:
        # An unhashable field value makes the input uncacheable, not invalid; validation below still runs.
        cacheable = False
    # A remembered projection always carries its full field set, so an empty value means a cache miss.
    if cached:
        result = dict(cached)
        return result
    observed = set(value)
    if observed != PROPOSITION_PROJECTION_RECORD_FIELDS:
        raise InvalidRequestError(
            "Proposition projection has invalid fields: "
            f"missing={sorted(PROPOSITION_PROJECTION_RECORD_FIELDS - observed)}, "
            f"extra={sorted(observed - PROPOSITION_PROJECTION_RECORD_FIELDS)}"
        )
    result = proposition_projection(**value)
    if cacheable:
        if len(VALIDATED_PROJECTIONS) >= MAX_VALIDATED_PROJECTIONS:
            VALIDATED_PROJECTIONS.pop(next(iter(VALIDATED_PROJECTIONS)))
        VALIDATED_PROJECTIONS[key] = dict(result)
    return result


def proposition_projection_from_graph_row(
    value: object,
    projection_id: PropositionProjectionQuery,
    vector_index_id: str = "",
) -> dict:
    """Decode one exact external graph row into a concrete projection."""
    if not isinstance(value, Mapping):
        raise InvalidRequestError("Proposition projection row must be an object")
    observed = set(value)
    if observed != PROPOSITION_PROJECTION_FIELDS:
        raise InvalidRequestError(
            "Proposition projection row has invalid fields: "
            f"missing={sorted(PROPOSITION_PROJECTION_FIELDS - observed)}, "
            f"extra={sorted(observed - PROPOSITION_PROJECTION_FIELDS)}"
        )
    if not isinstance(projection_id, PropositionProjectionQuery):
        raise InvalidRequestError("Proposition projection query identifier is unsupported")
    # The exact field set is checked above, so every typed default below is unreachable. A driver null
    # in a present field is still returned as None and refused or normalized by the field validators.
    invalidated_available = require_bool(value.get("invalidated_at_available", False), "invalidated_at_available")
    system_from_available = require_bool(value.get("system_from_available", False), "system_from_available")
    system_to_available = require_bool(value.get("system_to_available", False), "system_to_available")
    valid_from_available = require_bool(value.get("valid_from_available", False), "valid_from_available")
    valid_to_available = require_bool(value.get("valid_to_available", False), "valid_to_available")
    trust_category_available = require_bool(value.get("trust_category_available", False), "trust_category_available")
    trust_available = require_bool(value.get("supplied_trust_available", False), "supplied_trust_available")
    structured_available = require_bool(value.get("structured_match_available", False), "structured_match_available")
    semantic_available = require_bool(value.get("semantic_similarity_available", False), "semantic_similarity_available")
    result = proposition_projection(
        proposition_id=value.get("proposition_id", ""),
        subject_entity_id=value.get("subject_entity_id", ""),
        predicate_id=value.get("predicate_id", ""),
        object_entity_id=value.get("object_entity_id", ""),
        polarity=value.get("polarity", ""),
        modality_family=value.get("modality_family", ""),
        modality_operator=value.get("modality_operator", ""),
        argument_count=value.get("argument_count", 0),
        qualification_count=value.get("qualification_count", 0),
        context_count=value.get("context_count", 0),
        applicability_count=value.get("applicability_count", 0),
        invalidated_at=value.get("invalidated_at", ""),
        invalidated_at_available=invalidated_available,
        system_from=value.get("system_from", ""),
        system_from_available=system_from_available,
        system_to=value.get("system_to", ""),
        system_to_available=system_to_available,
        valid_from=value.get("valid_from", ""),
        valid_from_available=valid_from_available,
        valid_to=value.get("valid_to", ""),
        valid_to_available=valid_to_available,
        predicate_canonical=value.get("predicate_canonical", False),
        ownership_category=value.get("ownership_category", ""),
        trust_category=value.get("trust_category", ""),
        trust_category_available=trust_category_available,
        supplied_trust=value.get("supplied_trust", 0.0),
        supplied_trust_available=trust_available,
        structured_match=value.get("structured_match", 0.0),
        structured_match_available=structured_available,
        semantic_similarity=value.get("semantic_similarity", 0.0),
        semantic_similarity_available=semantic_available,
        projection_id=projection_id,
        vector_index_id=vector_index_id,
        vector_index_id_available=bool(vector_index_id),
    )
    return result


def proposition_projection_to_dict(value: object) -> dict[str, object]:
    """Serialize one projection to a concrete dictionary."""
    projection = validate_proposition_projection(value)
    result: dict[str, object] = dict(projection)
    result["projection_id"] = projection["projection_id"].value
    return result


def decode_projection_rows(
    rows: object,
    projection_id: PropositionProjectionQuery,
    vector_index_id: str,
    limit: int,
) -> list[dict]:
    if not isinstance(rows, list):
        raise InvalidRequestError("Proposition projection query must return a list")
    if len(rows) > limit or len(rows) > MAX_PROPOSITION_PROJECTION_ROWS:
        raise InvalidRequestError("Proposition projection query returned more rows than requested")
    decoded: dict[str, dict] = {}
    for row in rows:
        projection = proposition_projection_from_graph_row(row, projection_id, vector_index_id)
        proposition_id = projection["proposition_id"]
        if proposition_id in decoded and decoded.get(proposition_id, {}) != projection:
            raise InvalidRequestError(f"conflicting Proposition projections for Proposition ID: {proposition_id}")
        decoded[proposition_id] = projection
    result = [decoded.get(proposition_id, {}) for proposition_id in sorted(decoded)]
    return result


def relation_proposition_projection_from_graph_row(value: object) -> dict:
    """Decode a one-hop row while reusing the Section 7 Proposition projection codec."""
    if not isinstance(value, Mapping) or set(value) != RELATION_ONE_HOP_RESULT_FIELDS:
        raise InvalidRequestError("relation one-hop row has invalid fields")
    projection_row = {field: value[field] for field in PROPOSITION_PROJECTION_FIELDS}
    result: dict = {
        "projection": proposition_projection_from_graph_row(projection_row, PropositionProjectionQuery.RELATION_ONE_HOP),
        "object_label": require_text(value["object_label"], "relation object label", MAX_RELATION_LABEL_BYTES, allow_empty=False),
        "object_type": projection_entity_object_type(value["object_type"], "relation object type"),
        "predicate_cardinality": projection_cardinality(
            value["predicate_cardinality"],
            "relation Predicate cardinality",
        ),
    }
    return result


def validate_relation_proposition_projection(value: object) -> dict:
    """Validate and copy one in-memory relation result."""
    if not isinstance(value, Mapping) or set(value) != {
        "projection",
        "object_label",
        "object_type",
        "predicate_cardinality",
    }:
        raise InvalidRequestError("RelationPropositionProjection has invalid fields")
    object_type = value["object_type"]
    if not isinstance(object_type, ExpectedObjectType):
        raise InvalidRequestError("relation result object_type must be an ExpectedObjectType")
    cardinality = value["predicate_cardinality"]
    if not isinstance(cardinality, PredicateCardinality):
        raise InvalidRequestError("relation result predicate_cardinality must be a PredicateCardinality")
    result: dict = {
        "projection": validate_proposition_projection(value["projection"]),
        "object_label": require_text(value["object_label"], "relation object label", MAX_RELATION_LABEL_BYTES, allow_empty=False),
        "object_type": object_type,
        "predicate_cardinality": cardinality,
    }
    if result.get("projection", {}).get("projection_id") != PropositionProjectionQuery.RELATION_ONE_HOP:
        raise InvalidRequestError("relation result requires a relation one-hop projection")
    return result


def decode_relation_projection_rows(rows: object, limit: int) -> list[dict]:
    if not isinstance(rows, list) or len(rows) > limit:
        raise InvalidRequestError("relation one-hop query returned an invalid collection")
    decoded: dict[str, dict] = {}
    for row in rows:
        item = relation_proposition_projection_from_graph_row(row)
        proposition_id = item["projection"]["proposition_id"]
        if proposition_id in decoded and decoded.get(proposition_id, {}) != item:
            raise InvalidRequestError(f"conflicting relation projections for Proposition ID: {proposition_id}")
        decoded[proposition_id] = item
    result = [decoded.get(proposition_id, {}) for proposition_id in sorted(decoded)]
    return result


def is_write_cypher(cypher: str) -> bool:
    """Return True if the Cypher can mutate the graph.

    Detects the write clauses (CREATE / MERGE / DELETE / SET / REMOVE / ...)
    plus procedure invocation (CALL) and data import (LOAD) as whole words,
    case-insensitively. Conservative: an ambiguous query is treated as a
    write, so the recall-only path refuses it rather than risk a silent
    mutation -- this also refuses read-only procedures, which is the accepted
    cost of a blocklist that cannot inspect procedure bodies.
    """
    result = bool(WRITE_CLAUSE.search(cypher or ""))
    return result


def coerce_params(parameters):
    """Coerce values that the driver cannot send as Cypher parameters.

    The Bolt protocol has no UUID type. This is the single chokepoint where
    every query hits the driver, so coerce here defensively. Recurses into
    dicts, lists, and tuples so UUIDs nested inside batch payloads are coerced
    too.
    """
    if isinstance(parameters, UUID):
        result = str(parameters)
        return result
    if isinstance(parameters, dict):
        result = {key: coerce_params(value) for key, value in parameters.items()}
        return result
    if isinstance(parameters, list):
        result = [coerce_params(item) for item in parameters]
        return result
    if isinstance(parameters, tuple):
        result = tuple(coerce_params(item) for item in parameters)
        return result
    return parameters


def graph_is_empty(records: list) -> bool:
    """Check if a result row list has no records."""
    is_empty = not records
    return is_empty


def graph_single(records: list) -> dict:
    """Get the single (first) record from a result row list, or an empty dict."""
    record = records[0] if records else {}
    return record


class MemGraphConnection:
    """Manages a connection to a Bolt graph database through the neo4j async driver.

    The driver runs on a private event-loop thread, and every operation,
    connecting included, is bounded by ``timeout_seconds`` from the caller's
    side: a caller never waits longer, and an overrunning query is cancelled,
    which kills its connection. After a timeout or a lost connection, reads
    fail immediately (``available`` is False) until ``reconnect_after_turn``
    opens a new driver. The service calls it once the current turn is
    complete, so an unresponsive database costs a turn at most one timeout.
    """

    def __init__(
        self,
        host: str = "localhost",
        port: int = 7687,
        username: str = "",
        password: str = "",
        visibility_scope=(),
        timeout_seconds: float = GRAPH_TIMEOUT_SECONDS,
    ):
        if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float)) or not timeout_seconds > 0:
            raise ValueError("graph timeout must be a positive number of seconds")
        self.host = host
        self.port = port
        self.username = username
        self.password = password
        self.timeout_seconds = float(timeout_seconds)
        self.visibility_scope = validate_visibility_scope(dict(visibility_scope) if isinstance(visibility_scope, dict) else {})
        self.schema_report = {}
        self.driver = ()
        self.available = False
        self.reconnect_needed = False
        self.reconnect_failures = 0
        self.reconnect_future = ()
        self.loop = ()
        self.loop_thread = ()
        # Guards the fields above. It is never held while waiting on the loop.
        self.internal_lock = threading_RLock()
        # Serializes connect() so concurrent callers cannot open two drivers.
        self.connect_lock = threading_Lock()

    def driver_uri(self) -> str:
        """Return the direct (non-routing) Bolt URI for the configured host."""
        host = f"[{self.host}]" if ":" in self.host and not self.host.startswith("[") else self.host
        result = f"bolt://{host}:{self.port}"
        return result

    async def open_driver(self):
        """Create a driver and prove it can reach the database."""
        driver = AsyncGraphDatabase.driver(
            self.driver_uri(),
            auth=(self.username, self.password),
            connection_timeout=self.timeout_seconds,
            connection_acquisition_timeout=self.timeout_seconds,
            connection_write_timeout=self.timeout_seconds,
        )
        try:
            await driver.verify_connectivity()
        except BaseException:
            await driver.close()
            raise
        return driver

    def start_loop(self):
        """Start the private event loop that runs every driver call; the caller holds the lock."""
        if not self.loop:
            loop = asyncio_new_event_loop()
            thread = threading_Thread(target=loop.run_forever, name="engram-graph", daemon=True)
            thread.start()
            self.loop = loop
            self.loop_thread = thread
        return self.loop

    def connect(self):
        """Connect within the timeout.

        Returns the driver on success, or an empty tuple when the database
        cannot be reached in time. Sets ``available`` so callers can
        distinguish readiness before a read.
        """
        with self.connect_lock:
            with self.internal_lock:
                if self.driver:
                    result = self.driver
                    return result
                loop = self.start_loop()
            future = asyncio_run_coroutine_threadsafe(self.open_driver(), loop)
            try:
                driver = future.result(timeout=self.timeout_seconds)
            except Exception as err:
                self.abandon_open(future, loop)
                with self.internal_lock:
                    self.available = False
                    self.reconnect_needed = True
                logger.warning("Graph database unavailable at %s:%d", self.host, self.port, exc_info=err)
                result = ()
                return result
            with self.internal_lock:
                self.driver = driver
                self.available = True
                self.reconnect_needed = False
            logger.debug("Connected to graph database at %s:%d", self.host, self.port)
            return driver

    def abandon_open(self, future, loop) -> None:
        """Cancel a driver open that overran; close its driver if it finished anyway."""
        if future.cancel() or future.cancelled() or future.exception() is not None:
            return
        asyncio_run_coroutine_threadsafe(future.result().close(), loop)

    def reconnect_after_turn(self) -> bool:
        """Start replacing a lost connection; return whether an attempt started.

        The service calls this when a turn completes. The attempt runs on the
        driver's loop, so the finished turn is not delayed. Reads keep failing
        immediately until it succeeds, and a failed attempt is retried after
        the next turn.
        """
        with self.internal_lock:
            in_progress = bool(self.reconnect_future) and not self.reconnect_future.done()
            if not self.reconnect_needed or not self.loop or in_progress:
                result = False
                return result
            lost_driver = self.driver
            self.driver = ()
            self.reconnect_future = asyncio_run_coroutine_threadsafe(self.replace_driver(lost_driver), self.loop)
            result = True
            return result

    async def replace_driver(self, lost_driver) -> bool:
        """Close the lost driver and open a new one, each within the timeout."""
        if lost_driver:
            try:
                await asyncio_wait_for(lost_driver.close(), self.timeout_seconds)
            except Exception as err:
                logger.warning("Closing the lost graph driver failed", exc_info=err)
        try:
            driver = await asyncio_wait_for(self.open_driver(), self.timeout_seconds)
        except Exception as err:
            with self.internal_lock:
                self.reconnect_failures += 1
                first_failure = self.reconnect_failures == 1
            # Warn once per outage; the retry after every turn would flood the log.
            log = logger.warning if first_failure else logger.debug
            log("Graph database reconnect failed at %s:%d", self.host, self.port, exc_info=err)
            result = False
            return result
        with self.internal_lock:
            disconnected = self.loop is not asyncio_get_running_loop()
            if not disconnected:
                self.driver = driver
                self.available = True
                self.reconnect_needed = False
                self.reconnect_failures = 0
        if disconnected:
            await driver.close()
            result = False
            return result
        logger.info("Reconnected to graph database at %s:%d", self.host, self.port)
        result = True
        return result

    def disconnect(self):
        """Close the driver and stop its event loop."""
        with self.internal_lock:
            loop, thread = self.loop, self.loop_thread
            self.loop = ()
            self.loop_thread = ()
            self.reconnect_future = ()
            self.available = False
            self.reconnect_needed = False
        if not loop or not thread:
            return
        future = asyncio_run_coroutine_threadsafe(self.shutdown(), loop)
        try:
            future.result(timeout=2 * self.timeout_seconds)
        except Exception as err:
            future.cancel()
            logger.warning("Graph driver shutdown did not finish", exc_info=err)
        loop.call_soon_threadsafe(loop.stop)
        thread.join(self.timeout_seconds)
        if not thread.is_alive():
            loop.close()
        logger.info("Disconnected from graph database")

    async def shutdown(self) -> None:
        """Cancel outstanding work on the loop, then close the driver."""
        current = asyncio_current_task()
        pending = [task for task in asyncio_all_tasks() if task is not current]
        for task in pending:
            task.cancel()
        if pending:
            await asyncio_wait(pending, timeout=self.timeout_seconds)
        with self.internal_lock:
            driver = self.driver
            self.driver = ()
        if driver:
            await driver.close()

    def current_driver(self) -> tuple:
        """Return (loop, driver) for a statement, or raise when reads must fail fast."""
        with self.internal_lock:
            if not self.available or not self.driver:
                raise RuntimeError(f"graph database is unavailable at {self.host}:{self.port}")
            result = (self.loop, self.driver)
            return result

    def run_on(self, loop, driver, statement: str, parameters: dict) -> list:
        """Run one statement within the timeout and return its rows.

        A timeout or connection-level failure marks the connection lost, so
        later reads fail immediately until the after-turn reconnect.
        """
        sendable = {key: coerce_params(value) for key, value in parameters.items()}
        future = asyncio_run_coroutine_threadsafe(read_rows(driver, statement, sendable, MAX_GRAPH_READ_ROWS), loop)
        try:
            result = future.result(timeout=self.timeout_seconds)
            return result
        except Exception as err:
            # Cancelling kills the query's connection instead of leaving it
            # waiting on the database.
            future.cancel()
            if is_connection_error(err):
                with self.internal_lock:
                    if self.driver is driver:
                        self.available = False
                        self.reconnect_needed = True
            raise

    def execute(self, query: str, parameters=()) -> list:
        """Execute a Cypher query and return results as a list of dicts.

        Raises if the database is unavailable, times out, or a query fails so
        graph absence and service unavailability are never conflated.
        Mutating or ambiguous Cypher is rejected before reaching the database.
        """
        if is_write_cypher(query) and query not in FIXED_READ_PROCEDURE_QUERIES:
            raise ValueError("ENGRAM graph access is read-only")

        loop, driver = self.current_driver()
        try:
            visibility = self.visibility_parameters()
            if isinstance(parameters, Mapping):
                caller_parameters = dict(parameters)
            elif parameters:
                raise ValueError("graph query parameters must be an object")
            else:
                caller_parameters = {}
            # The configured visibility scope is the owner's authority; a caller
            # or authored template may not rebind it for a filtered read.
            collisions = sorted(set(caller_parameters) & set(visibility))
            if collisions:
                raise ValueError(f"graph query parameters cannot rebind visibility bindings: {', '.join(collisions)}")
            merged_parameters = {**caller_parameters, **visibility}
            result = self.run_on(loop, driver, query, merged_parameters)
            return result
        except Exception as err:
            err_str = str(err).lower()
            if isinstance(err, TimeoutError):
                logger.error("Query timed out after %.0f ms", self.timeout_seconds * 1000, exc_info=err)
            elif "does not exist" in err_str or "not found" in err_str or "no procedure named" in err_str:
                logger.debug("Query failed as expected", exc_info=err)
            else:
                logger.error("Query failed", exc_info=err)
            # Only the exception type crosses this boundary; the log has the rest.
            raise RuntimeError(f"Query failed ({type(err).__name__})") from err

    def execute_admin(self, statement: str, parameters=()) -> list:
        """Run one schema administration statement for the schema tooling.

        Unlike ``execute`` it permits DDL and raises the
        driver's own error, so an operator sees why a statement failed.
        Runtime reads never use it.
        """
        loop, driver = self.current_driver()
        result = self.run_on(loop, driver, statement, dict(parameters) if parameters else {})
        return result

    def visibility_parameters(self) -> dict:
        """Return exact non-user visibility parameters for every graph read."""
        result = visibility_parameters(self.visibility_scope)
        return result

    def vector_search_propositions(
        self,
        embedding: list[float],
        *,
        index_name: str = "proposition_embeddings",
        limit: int = 250,
        min_similarity: float = 0.45,
        evaluation_time: str = "",
    ) -> list:
        """Search active proof-canonical Propositions through one fixed ANN query.

        Generic ``CALL`` remains forbidden on :meth:`execute`. This method
        exposes no caller-supplied Cypher; only a validated vector-index
        identifier and bounded scalar parameters reach its fixed read query.
        """
        if not isinstance(index_name, str) or not VECTOR_INDEX_NAME.fullmatch(index_name):
            raise ValueError("invalid vector index name")
        if not isinstance(embedding, list) or not embedding:
            raise ValueError("embedding must be a non-empty list")
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 1000:
            raise ValueError("limit must be an integer from 1 through 1000")
        if (
            not isinstance(min_similarity, (int, float))
            or isinstance(min_similarity, bool)
            or not 0.0 <= float(min_similarity) <= 1.0
        ):
            raise ValueError("min_similarity must be between 0 and 1")
        selected_evaluation_time = evaluation_time or datetime.now(UTC).isoformat().replace("+00:00", "Z")
        projection_timestamp(selected_evaluation_time, True, "graph vector evaluation time")
        result = self.execute(
            VECTOR_SEARCH_PROPOSITIONS_QUERY,
            {
                "index_name": index_name,
                "limit": limit,
                "query_embedding": embedding,
                "min_similarity": float(min_similarity),
                "evaluation_time": selected_evaluation_time,
            },
        )
        return result

    def structured_proposition_projections(
        self,
        value: str,
        *,
        projection_id: PropositionProjectionQuery,
        limit: int = 10,
        basis_window: dict = UNCONSTRAINED_ASSERTION_BASIS,
    ) -> list[dict]:
        """Run one allow-listed structured Proposition projection and strictly decode its rows."""
        term = require_text(value, "Proposition projection search value", MAX_PROPOSITION_PROJECTION_TERM_BYTES, allow_empty=False)
        if projection_id == PropositionProjectionQuery.STRUCTURED_ENTITY:
            query = STRUCTURED_ENTITY_PROPOSITION_PROJECTION_QUERY
        elif projection_id == PropositionProjectionQuery.STRUCTURED_KEYWORD:
            query = STRUCTURED_KEYWORD_PROPOSITION_PROJECTION_QUERY
        else:
            raise InvalidRequestError("structured Proposition projection query identifier is unsupported")
        row_limit = projection_int(limit, "Proposition projection limit", 1, MAX_PROPOSITION_PROJECTION_ROWS)
        rows = self.execute(query, {"value": term, "limit": row_limit, **assertion_basis_parameters(basis_window)})
        result = decode_projection_rows(rows, projection_id, "", row_limit)
        return result

    def canonical_entity_matches(self, surface: str, *, limit: int = MAX_RELATION_CANDIDATES) -> list[dict]:
        """Resolve an entity surface through one fixed, read-only query."""
        term = require_text(surface, "canonical entity surface", MAX_RELATION_LABEL_BYTES, allow_empty=False)
        row_limit = projection_int(limit, "canonical entity match limit", 1, MAX_RELATION_CANDIDATES)
        rows = self.execute(CANONICAL_ENTITY_MATCH_QUERY, {"surface": term, "limit": row_limit})
        if not isinstance(rows, list) or len(rows) > row_limit:
            raise InvalidRequestError("canonical entity query returned an invalid collection")
        decoded = [canonical_entity_match_from_graph_row(row) for row in rows]
        if len({row["canonical_id"] for row in decoded}) != len(decoded):
            raise InvalidRequestError("canonical entity query returned duplicate identities")
        result = sorted(decoded, key=lambda row: row["canonical_id"])
        return result

    def canonical_predicate_matches(self, surface: str, *, limit: int = MAX_RELATION_CANDIDATES) -> list[dict]:
        """Resolve a Predicate surface through one fixed, read-only query."""
        term = require_text(surface, "canonical Predicate surface", MAX_RELATION_LABEL_BYTES, allow_empty=False)
        row_limit = projection_int(limit, "canonical Predicate match limit", 1, MAX_RELATION_CANDIDATES)
        rows = self.execute(CANONICAL_PREDICATE_MATCH_QUERY, {"surface": term, "limit": row_limit})
        if not isinstance(rows, list) or len(rows) > row_limit:
            raise InvalidRequestError("canonical Predicate query returned an invalid collection")
        decoded = [canonical_predicate_match_from_graph_row(row) for row in rows]
        if len({row["canonical_id"] for row in decoded}) != len(decoded):
            raise InvalidRequestError("canonical Predicate query returned duplicate identities")
        result = sorted(decoded, key=lambda row: row["canonical_id"])
        return result

    def relation_one_hop_proposition_projections(
        self,
        subject_entity_id: str,
        predicate_id: str,
        *,
        limit: int = MAX_RELATION_PLAN_ROWS,
        include_historical: bool = False,
        basis_window: dict = UNCONSTRAINED_ASSERTION_BASIS,
    ) -> list[dict]:
        """Execute only the allow-listed parameterized one-hop Proposition template."""
        subject = require_identifier(
            subject_entity_id, "relation subject_entity_id", maximum_bytes=MAX_PROPOSITION_PROJECTION_IDENTIFIER_BYTES
        )
        predicate = require_identifier(
            predicate_id, "relation predicate_id", maximum_bytes=MAX_PROPOSITION_PROJECTION_IDENTIFIER_BYTES
        )
        if not isinstance(include_historical, bool):
            raise InvalidRequestError("relation include_historical must be a boolean")
        row_limit = projection_int(limit, "relation one-hop limit", 1, MAX_RELATION_PLAN_ROWS)
        rows = self.execute(
            RELATION_ONE_HOP_PROPOSITION_PROJECTION_QUERY,
            {
                "subject_entity_id": subject,
                "predicate_id": predicate,
                "include_historical": include_historical,
                "limit": row_limit,
                **assertion_basis_parameters(basis_window),
            },
        )
        result = decode_relation_projection_rows(rows, row_limit)
        return result

    def vector_search_proposition_projections(
        self,
        embedding: list[float],
        *,
        index_name: str = "proposition_embeddings",
        limit: int = 10,
        min_similarity: float = 0.45,
        basis_window: dict = UNCONSTRAINED_ASSERTION_BASIS,
    ) -> list[dict]:
        """Run the fixed ANN Proposition projection without returning graph prose or arbitrary properties.

        Only Propositions with an active Assertion inside the request's basis
        window are returned, each projected from its lowest-ID such basis.
        """
        if not isinstance(index_name, str) or not VECTOR_INDEX_NAME.fullmatch(index_name):
            raise InvalidRequestError("invalid Proposition projection vector index name")
        if not isinstance(embedding, list) or not embedding:
            raise InvalidRequestError("Proposition projection embedding must be a non-empty list")
        if len(embedding) > MAX_PROPOSITION_PROJECTION_EMBEDDING_DIMENSIONS:
            raise InvalidRequestError(
                "Proposition projection embedding exceeds the limit of "
                f"{MAX_PROPOSITION_PROJECTION_EMBEDDING_DIMENSIONS} dimensions"
            )
        for component in embedding:
            if isinstance(component, bool) or not isinstance(component, (int, float)) or not math_isfinite(float(component)):
                raise InvalidRequestError("Proposition projection embedding must contain finite numeric values")
        row_limit = projection_int(limit, "Proposition projection vector limit", 1, MAX_PROPOSITION_PROJECTION_ROWS)
        similarity = projection_score(min_similarity, True, "Proposition projection min_similarity")
        rows = self.execute(
            VECTOR_PROPOSITION_PROJECTION_QUERY,
            {
                "index_name": index_name,
                "limit": row_limit,
                "query_embedding": embedding,
                "min_similarity": similarity,
                **assertion_basis_parameters(basis_window),
            },
        )
        result = decode_projection_rows(rows, PropositionProjectionQuery.VECTOR, index_name, row_limit)
        return result

    def proposition_projection_by_id(
        self,
        proposition_id: str,
        basis_window: dict = UNCONSTRAINED_ASSERTION_BASIS,
    ) -> list[dict]:
        """Re-read one Proposition through the fixed canonical projection for publication revalidation.

        The request's basis window selects the same Assertion basis discovery used.
        """
        identifier = require_identifier(
            proposition_id,
            "Proposition projection revalidation proposition_id",
            maximum_bytes=MAX_PROPOSITION_PROJECTION_IDENTIFIER_BYTES,
        )
        rows = self.execute(
            PROPOSITION_PROJECTION_BY_ID_QUERY,
            {"proposition_id": identifier, **assertion_basis_parameters(basis_window)},
        )
        result = decode_projection_rows(rows, PropositionProjectionQuery.BY_ID, "", 1)
        return result


def connect_graph(
    host: str = "localhost",
    port: int = 7687,
    username: str = "",
    password: str = "",
    visibility_scope=(),
) -> MemGraphConnection:
    """Connect to the graph database and check that its schema is compatible.

    The connection and schema check are attempted immediately. An
    unreachable or incompatible graph fails startup rather than presenting
    an empty query result as graph readiness.
    """
    client = MemGraphConnection(
        host=host,
        port=port,
        username=username,
        password=password,
        visibility_scope=visibility_scope,
    )
    # Ownership passes to the caller only after validation; every unsuccessful
    # connection or preflight outcome, including a raised catalog read, stops
    # the client's loop and driver here.
    try:
        if not client.connect():
            raise RuntimeError(f"graph database is unavailable at {host}:{port}")
        report = verify_schema(client, packaged_schema())
        if not report.get("valid", False):
            raise RuntimeError(f"graph database schema preflight failed: {report}")
    except BaseException:
        client.disconnect()
        raise
    client.schema_report = report
    return client
