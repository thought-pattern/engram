"""Knowledge Graph integration for ENGRAM.

Connects to a MemGraph instance using the pymgclient driver. Runtime access is
strictly read-only: every public execution path rejects mutating Cypher before
opening a connection.

Graph unavailability and query failure remain explicit; an empty row list means
only that a successful read matched no records. A backend swap
(e.g. to a different Bolt-speaking store) is a sibling module with the same
method names — duck typing is the contract, so there is no abstract base class.
"""

import logging
import math
import threading
import time
from collections.abc import Mapping
from datetime import datetime, timedelta
from pathlib import Path
from types import NoneType
from uuid import UUID

import mgclient

from engram.constants import (
    CANONICAL_ENTITY_MATCH_FIELDS,
    CANONICAL_ENTITY_MATCH_QUERY,
    CANONICAL_PREDICATE_MATCH_FIELDS,
    CANONICAL_PREDICATE_MATCH_QUERY,
    CONNECTION_LOST_MARKERS,
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
    RECONNECT_COOLDOWN_SECONDS,
    RELATION_ONE_HOP_PROPOSITION_PROJECTION_QUERY,
    RELATION_ONE_HOP_RESULT_FIELDS,
    STRUCTURED_ENTITY_PROPOSITION_PROJECTION_QUERY,
    STRUCTURED_KEYWORD_PROPOSITION_PROJECTION_QUERY,
    VECTOR_INDEX_NAME,
    VECTOR_PROPOSITION_PROJECTION_QUERY,
    WRITE_CLAUSE,
    ExpectedObjectType,
    PredicateCardinality,
    PropositionProjectionQuery,
)
from engram.errors import InvalidRequestError
from engram.schema_admin import verify_schema
from engram.scope import validate_visibility_scope, visibility_parameters

logger = logging.getLogger(__name__)


def is_connection_error(err: Exception) -> bool:
    """Distinguish socket-level failures from query-level failures.

    Returns True only when the exception indicates the TCP connection is dead.
    Query-level errors (storage timeouts, lock contention, syntax errors) return
    False -- the socket is still usable and the reconnect cooldown does not apply.
    """
    error_name = type(err).__name__
    if isinstance(err, (ConnectionError, BrokenPipeError, OSError)) or error_name == "InterfaceError":
        result = True
        return result
    if error_name == "OperationalError":
        err_str = str(err).lower()
        result = any(marker in err_str for marker in CONNECTION_LOST_MARKERS)
        return result
    result = False
    return result


def _projection_text(value: object, name: str, maximum_bytes: int, *, allow_empty: bool) -> str:
    if not isinstance(value, str):
        raise InvalidRequestError(f"{name} must be a string")
    if not allow_empty and not value:
        raise InvalidRequestError(f"{name} must not be empty")
    if len(value.encode("utf-8")) > maximum_bytes:
        raise InvalidRequestError(f"{name} exceeds the limit of {maximum_bytes} UTF-8 bytes")
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise InvalidRequestError(f"{name} contains a control character")
    return value


def _projection_identifier(value: object, name: str) -> str:
    identifier = _projection_text(value, name, MAX_PROPOSITION_PROJECTION_IDENTIFIER_BYTES, allow_empty=False)
    if any(character.isspace() for character in identifier):
        raise InvalidRequestError(f"{name} must not contain whitespace")
    return identifier


def _projection_bool(value: object, name: str) -> bool:
    if not isinstance(value, bool):
        raise InvalidRequestError(f"{name} must be a boolean")
    return value


def _projection_int(value: object, name: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise InvalidRequestError(f"{name} must be an integer from {minimum} through {maximum}")
    return value


def _projection_score(value: object, available: bool, name: str) -> float:
    if not available and isinstance(value, NoneType):
        result = 0.0
        return result
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise InvalidRequestError(f"{name} must be numeric")
    score = float(value)
    if not math.isfinite(score) or not 0.0 <= score <= 1.0:
        raise InvalidRequestError(f"{name} must be finite and from 0 through 1")
    if not available and score != 0.0:
        raise InvalidRequestError(f"{name} must be zero when unavailable")
    return score


def _projection_timestamp(value: object, available: bool, name: str) -> str:
    if not available:
        if isinstance(value, NoneType) or value == "":
            result = ""
            return result
        raise InvalidRequestError(f"{name} must be empty when unavailable")
    if isinstance(value, datetime):
        if not value.tzinfo or value.utcoffset() != timedelta(0):
            raise InvalidRequestError(f"{name} must be timezone-aware UTC")
        text = value.isoformat().replace("+00:00", "Z")
    else:
        text = _projection_text(value, name, MAX_PROPOSITION_PROJECTION_TIMESTAMP_BYTES, allow_empty=False)
    if not text.endswith("Z"):
        raise InvalidRequestError(f"{name} must be a canonical RFC 3339 UTC timestamp ending in Z")
    try:
        parsed = datetime.fromisoformat(text[:-1] + "+00:00")
    except ValueError as error:
        raise InvalidRequestError(f"{name} must be a canonical RFC 3339 UTC timestamp") from error
    if parsed.isoformat().replace("+00:00", "Z") != text:
        raise InvalidRequestError(f"{name} must use the canonical RFC 3339 UTC representation")
    return text


def _optional_projection_text(value: object, available: bool, name: str, maximum_bytes: int) -> str:
    if not available:
        if isinstance(value, NoneType) or value == "":
            result = ""
            return result
        raise InvalidRequestError(f"{name} must be empty when unavailable")
    result = _projection_text(value, name, maximum_bytes, allow_empty=False)
    return result


PropositionProjection = dict


CanonicalEntityMatch = dict


CanonicalPredicateMatch = dict


RelationPropositionProjection = dict


def _projection_text_collection(value: object, name: str) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)) or len(value) > MAX_RELATION_SURFACES:
        raise InvalidRequestError(f"{name} must be a collection of at most {MAX_RELATION_SURFACES} strings")
    normalized = tuple(_projection_text(item, f"{name} value", MAX_RELATION_LABEL_BYTES, allow_empty=False) for item in value)
    if normalized != tuple(dict.fromkeys(normalized)):
        raise InvalidRequestError(f"{name} must contain unique values")
    return normalized


def _projection_object_type(value: object, name: str) -> ExpectedObjectType:
    raw = _projection_text(value, name, 32, allow_empty=False).upper()
    try:
        result = ExpectedObjectType(raw)
    except ValueError as error:
        raise InvalidRequestError(f"{name} is unsupported") from error
    return result


def _projection_cardinality(value: object, name: str) -> PredicateCardinality:
    raw = _projection_text(value, name, 32, allow_empty=False).upper()
    try:
        result = PredicateCardinality(raw)
    except ValueError as error:
        raise InvalidRequestError(f"{name} is unsupported") from error
    return result


def canonical_entity_match_from_graph_row(value: object) -> CanonicalEntityMatch:
    """Decode one exact canonical entity match row without arbitrary graph properties."""
    if not isinstance(value, Mapping) or set(value) != CANONICAL_ENTITY_MATCH_FIELDS:
        raise InvalidRequestError("canonical entity match row has invalid fields")
    result: CanonicalEntityMatch = {
        "canonical_id": _projection_identifier(value["canonical_id"], "canonical entity ID"),
        "primary_label": _projection_text(
            value["primary_label"], "canonical entity primary label", MAX_RELATION_LABEL_BYTES, allow_empty=False
        ),
        "aliases": _projection_text_collection(value["aliases"], "canonical entity aliases"),
        "edge_surfaces": _projection_text_collection(value["edge_surfaces"], "canonical entity edge surfaces"),
        "entity_type": _projection_object_type(value["entity_type"], "canonical entity type"),
    }
    return result


def canonical_predicate_match_from_graph_row(value: object) -> CanonicalPredicateMatch:
    """Decode one exact canonical Predicate match row."""
    if not isinstance(value, Mapping) or set(value) != CANONICAL_PREDICATE_MATCH_FIELDS:
        raise InvalidRequestError("canonical Predicate match row has invalid fields")
    result: CanonicalPredicateMatch = {
        "canonical_id": _projection_identifier(value["canonical_id"], "canonical Predicate ID"),
        "primary_label": _projection_text(
            value["primary_label"], "canonical Predicate primary label", MAX_RELATION_LABEL_BYTES, allow_empty=False
        ),
        "synonyms": _projection_text_collection(value["synonyms"], "canonical Predicate synonyms"),
        "object_type": _projection_object_type(value["object_type"], "canonical Predicate object type"),
    }
    return result


def proposition_projection(
    *,
    proposition_id: object,
    subject_entity_id: object,
    predicate_id: object,
    object_entity_id: object,
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
    supplied_trust_version: object,
    supplied_trust_version_available: object,
    structured_match: object,
    structured_match_available: object,
    semantic_similarity: object,
    semantic_similarity_available: object,
    projection_id: object,
    vector_index_id: object,
    vector_index_id_available: object,
) -> PropositionProjection:
    """Build one validated strict Proposition projection dictionary."""
    normalized_proposition_id = _projection_identifier(proposition_id, "Proposition projection proposition_id")
    normalized_subject_id = _projection_identifier(subject_entity_id, "Proposition projection subject_entity_id")
    normalized_predicate_id = _projection_identifier(predicate_id, "Proposition projection predicate_id")
    normalized_object_id = _projection_identifier(object_entity_id, "Proposition projection object_entity_id")
    normalized_invalidated_available = _projection_bool(invalidated_at_available, "Proposition projection invalidated_at_available")
    normalized_system_from_available = _projection_bool(system_from_available, "Proposition projection system_from_available")
    normalized_system_to_available = _projection_bool(system_to_available, "Proposition projection system_to_available")
    normalized_valid_from_available = _projection_bool(valid_from_available, "Proposition projection valid_from_available")
    normalized_valid_to_available = _projection_bool(valid_to_available, "Proposition projection valid_to_available")
    normalized_invalidated_at = _projection_timestamp(
        invalidated_at, normalized_invalidated_available, "Proposition projection invalidated_at"
    )
    normalized_system_from = _projection_timestamp(
        system_from, normalized_system_from_available, "Proposition projection system_from"
    )
    normalized_system_to = _projection_timestamp(system_to, normalized_system_to_available, "Proposition projection system_to")
    normalized_valid_from = _projection_timestamp(valid_from, normalized_valid_from_available, "Proposition projection valid_from")
    normalized_valid_to = _projection_timestamp(valid_to, normalized_valid_to_available, "Proposition projection valid_to")
    if (
        normalized_system_from_available
        and normalized_system_to_available
        and datetime.fromisoformat(normalized_system_from[:-1] + "+00:00")
        >= datetime.fromisoformat(normalized_system_to[:-1] + "+00:00")
    ):
        raise InvalidRequestError("Proposition projection system_from must be earlier than system_to")
    if (
        normalized_valid_from_available
        and normalized_valid_to_available
        and datetime.fromisoformat(normalized_valid_from[:-1] + "+00:00")
        >= datetime.fromisoformat(normalized_valid_to[:-1] + "+00:00")
    ):
        raise InvalidRequestError("Proposition projection valid_from must be earlier than valid_to")
    normalized_predicate_canonical = _projection_bool(predicate_canonical, "Proposition projection predicate_canonical")
    normalized_ownership = _projection_text(ownership_category, "Proposition projection ownership_category", 32, allow_empty=False)
    if normalized_ownership not in {"PUBLIC", "COMPANY", "CUSTOMER"}:
        raise InvalidRequestError("Proposition projection ownership_category is unsupported")
    normalized_trust_category_available = _projection_bool(
        trust_category_available, "Proposition projection trust_category_available"
    )
    normalized_trust_category = _optional_projection_text(
        trust_category,
        normalized_trust_category_available,
        "Proposition projection trust_category",
        96,
    )
    normalized_trust_available = _projection_bool(supplied_trust_available, "Proposition projection supplied_trust_available")
    normalized_trust = _projection_score(supplied_trust, normalized_trust_available, "Proposition projection supplied_trust")
    normalized_version_available = _projection_bool(
        supplied_trust_version_available, "Proposition projection supplied_trust_version_available"
    )
    normalized_version = _projection_int(supplied_trust_version, "Proposition projection supplied_trust_version", 0, 2_147_483_647)
    if not normalized_version_available and normalized_version != 0:
        raise InvalidRequestError("Proposition projection supplied_trust_version must be zero when unavailable")
    if normalized_trust_available != normalized_version_available:
        raise InvalidRequestError("Proposition projection supplied trust value and version availability must match")
    normalized_structured_available = _projection_bool(
        structured_match_available, "Proposition projection structured_match_available"
    )
    normalized_structured = _projection_score(
        structured_match, normalized_structured_available, "Proposition projection structured_match"
    )
    normalized_semantic_available = _projection_bool(
        semantic_similarity_available, "Proposition projection semantic_similarity_available"
    )
    normalized_semantic = _projection_score(
        semantic_similarity, normalized_semantic_available, "Proposition projection semantic_similarity"
    )
    if not isinstance(projection_id, PropositionProjectionQuery):
        raise InvalidRequestError("Proposition projection projection_id must be a PropositionProjectionQuery")
    normalized_vector_available = _projection_bool(vector_index_id_available, "Proposition projection vector_index_id_available")
    normalized_vector_id = _projection_text(
        vector_index_id, "Proposition projection vector_index_id", 128, allow_empty=not normalized_vector_available
    )
    if normalized_vector_available and not VECTOR_INDEX_NAME.fullmatch(normalized_vector_id):
        raise InvalidRequestError("Proposition projection vector_index_id is invalid")
    if not normalized_vector_available and normalized_vector_id:
        raise InvalidRequestError("Proposition projection vector_index_id must be empty when unavailable")
    if projection_id == PropositionProjectionQuery.VECTOR_V1:
        if normalized_structured_available or not normalized_semantic_available or not normalized_vector_available:
            raise InvalidRequestError("vector Proposition projection measurements or index provenance are inconsistent")
    elif projection_id == PropositionProjectionQuery.BY_ID_V1:
        if normalized_structured_available or normalized_semantic_available or normalized_vector_available:
            raise InvalidRequestError("by-ID Proposition projection measurements or index provenance are inconsistent")
    elif not normalized_structured_available or normalized_semantic_available or normalized_vector_available:
        raise InvalidRequestError("structured Proposition projection measurements or index provenance are inconsistent")
    result: PropositionProjection = {
        "proposition_id": normalized_proposition_id,
        "subject_entity_id": normalized_subject_id,
        "predicate_id": normalized_predicate_id,
        "object_entity_id": normalized_object_id,
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
        "supplied_trust_version": normalized_version,
        "supplied_trust_version_available": normalized_version_available,
        "structured_match": normalized_structured,
        "structured_match_available": normalized_structured_available,
        "semantic_similarity": normalized_semantic,
        "semantic_similarity_available": normalized_semantic_available,
        "projection_id": projection_id,
        "vector_index_id": normalized_vector_id,
        "vector_index_id_available": normalized_vector_available,
    }
    return result


def validate_proposition_projection(value: object) -> PropositionProjection:
    """Revalidate and copy one in-memory Proposition projection."""
    if not isinstance(value, Mapping):
        raise InvalidRequestError("Proposition projection must be an object")
    observed = set(value)
    if observed != PROPOSITION_PROJECTION_RECORD_FIELDS:
        raise InvalidRequestError(
            "Proposition projection has invalid fields: "
            f"missing={sorted(PROPOSITION_PROJECTION_RECORD_FIELDS - observed)}, "
            f"extra={sorted(observed - PROPOSITION_PROJECTION_RECORD_FIELDS)}"
        )
    result = proposition_projection(**value)
    return result


def proposition_projection_from_graph_row(
    value: object,
    projection_id: PropositionProjectionQuery,
    vector_index_id: str = "",
) -> PropositionProjection:
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
    invalidated_available = _projection_bool(value["invalidated_at_available"], "invalidated_at_available")
    system_from_available = _projection_bool(value["system_from_available"], "system_from_available")
    system_to_available = _projection_bool(value["system_to_available"], "system_to_available")
    valid_from_available = _projection_bool(value["valid_from_available"], "valid_from_available")
    valid_to_available = _projection_bool(value["valid_to_available"], "valid_to_available")
    trust_category_available = _projection_bool(value["trust_category_available"], "trust_category_available")
    trust_available = _projection_bool(value["supplied_trust_available"], "supplied_trust_available")
    version_available = _projection_bool(value["supplied_trust_version_available"], "supplied_trust_version_available")
    structured_available = _projection_bool(value["structured_match_available"], "structured_match_available")
    semantic_available = _projection_bool(value["semantic_similarity_available"], "semantic_similarity_available")
    raw_version = value["supplied_trust_version"]
    if not version_available and isinstance(raw_version, NoneType):
        supplied_version = 0
    else:
        supplied_version = _projection_int(raw_version, "supplied_trust_version", 0, 2_147_483_647)
    result = proposition_projection(
        proposition_id=value["proposition_id"],
        subject_entity_id=value["subject_entity_id"],
        predicate_id=value["predicate_id"],
        object_entity_id=value["object_entity_id"],
        invalidated_at=value["invalidated_at"],
        invalidated_at_available=invalidated_available,
        system_from=value["system_from"],
        system_from_available=system_from_available,
        system_to=value["system_to"],
        system_to_available=system_to_available,
        valid_from=value["valid_from"],
        valid_from_available=valid_from_available,
        valid_to=value["valid_to"],
        valid_to_available=valid_to_available,
        predicate_canonical=value["predicate_canonical"],
        ownership_category=value["ownership_category"],
        trust_category=value["trust_category"],
        trust_category_available=trust_category_available,
        supplied_trust=value["supplied_trust"],
        supplied_trust_available=trust_available,
        supplied_trust_version=supplied_version,
        supplied_trust_version_available=version_available,
        structured_match=value["structured_match"],
        structured_match_available=structured_available,
        semantic_similarity=value["semantic_similarity"],
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


def _decode_projection_rows(
    rows: object,
    projection_id: PropositionProjectionQuery,
    vector_index_id: str,
    limit: int,
) -> list[PropositionProjection]:
    if not isinstance(rows, list):
        raise InvalidRequestError("Proposition projection query must return a list")
    if len(rows) > limit or len(rows) > MAX_PROPOSITION_PROJECTION_ROWS:
        raise InvalidRequestError("Proposition projection query returned more rows than requested")
    decoded: dict[str, PropositionProjection] = {}
    for row in rows:
        projection = proposition_projection_from_graph_row(row, projection_id, vector_index_id)
        proposition_id = projection["proposition_id"]
        if proposition_id in decoded and decoded[proposition_id] != projection:
            raise InvalidRequestError(f"conflicting Proposition projections for Proposition ID: {proposition_id}")
        decoded[proposition_id] = projection
    result = [decoded[proposition_id] for proposition_id in sorted(decoded)]
    return result


def relation_proposition_projection_from_graph_row(value: object) -> RelationPropositionProjection:
    """Decode a one-hop row while reusing the Section 7 Proposition projection codec."""
    if not isinstance(value, Mapping) or set(value) != RELATION_ONE_HOP_RESULT_FIELDS:
        raise InvalidRequestError("relation one-hop row has invalid fields")
    projection_row = {field: value[field] for field in PROPOSITION_PROJECTION_FIELDS}
    result: RelationPropositionProjection = {
        "projection": proposition_projection_from_graph_row(projection_row, PropositionProjectionQuery.RELATION_ONE_HOP_V1),
        "object_label": _projection_text(
            value["object_label"], "relation object label", MAX_RELATION_LABEL_BYTES, allow_empty=False
        ),
        "object_type": _projection_object_type(value["object_type"], "relation object type"),
        "predicate_cardinality": _projection_cardinality(
            value["predicate_cardinality"],
            "relation Predicate cardinality",
        ),
    }
    return result


def validate_relation_proposition_projection(value: object) -> RelationPropositionProjection:
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
    result: RelationPropositionProjection = {
        "projection": validate_proposition_projection(value["projection"]),
        "object_label": _projection_text(
            value["object_label"], "relation object label", MAX_RELATION_LABEL_BYTES, allow_empty=False
        ),
        "object_type": object_type,
        "predicate_cardinality": cardinality,
    }
    if result["projection"]["projection_id"] != PropositionProjectionQuery.RELATION_ONE_HOP_V1:
        raise InvalidRequestError("relation result requires a relation one-hop projection")
    return result


def _decode_relation_projection_rows(rows: object, limit: int) -> list[RelationPropositionProjection]:
    if not isinstance(rows, list) or len(rows) > limit:
        raise InvalidRequestError("relation one-hop query returned an invalid collection")
    decoded: dict[str, RelationPropositionProjection] = {}
    for row in rows:
        item = relation_proposition_projection_from_graph_row(row)
        proposition_id = item["projection"]["proposition_id"]
        if proposition_id in decoded and decoded[proposition_id] != item:
            raise InvalidRequestError(f"conflicting relation projections for Proposition ID: {proposition_id}")
        decoded[proposition_id] = item
    return [decoded[proposition_id] for proposition_id in sorted(decoded)]


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
    """Coerce values that mgclient cannot accept as Cypher parameters.

    UUID objects fail with "value of type 'UUID' can't be used as query
    parameter". This is the single chokepoint where every query hits the driver,
    so coerce here defensively. Recurses into dicts, lists, and tuples so UUIDs
    nested inside batch payloads are coerced too.
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
    """Manages a connection to MemGraph using pymgclient.

    Retains explicit unavailable state when MemGraph cannot be reached. During
    the reconnect cooldown, reads fail immediately rather than retrying the TCP
    connection or misreporting the outage as an empty graph.
    """

    def __init__(
        self,
        host: str = "localhost",
        port: int = 7687,
        username: str = "",
        password: str = "",
        visibility_scope=(),
    ):
        self.host = host
        self.port = port
        self.username = username
        self.password = password
        self.visibility_scope = validate_visibility_scope(dict(visibility_scope) if isinstance(visibility_scope, dict) else {})
        self.schema_report = {}
        self.conn = ()
        self.available = False
        self.connection_attempted = False
        self.last_connect_attempt = 0.0
        # pymgclient connections may be shared between threads but not used
        # concurrently. Serialize all connection and cursor access.
        self._lock = threading.RLock()

    def connect(self):
        """Establish a connection to MemGraph.

        Returns the connection on success, or an empty tuple on failure. Sets
        ``available`` so callers can distinguish readiness before a read.
        """
        with self._lock:
            result = self._connect_unlocked()
            return result

    def _connect_unlocked(self):
        """Establish a connection while the caller holds the connection lock."""
        if self.conn:
            result = self.conn
            return result

        # Respect cooldown after a failed attempt
        if self.connection_attempted and not self.available:
            elapsed = time.monotonic() - self.last_connect_attempt
            if elapsed < RECONNECT_COOLDOWN_SECONDS:
                result = ()
                return result

        self.last_connect_attempt = time.monotonic()
        self.connection_attempted = True

        try:
            connect_params = {
                "host": self.host,
                "port": self.port,
            }
            if self.username:
                connect_params["username"] = self.username
            if self.password:
                connect_params["password"] = self.password

            self.conn = mgclient.connect(**connect_params)
            self.conn.autocommit = True
            self.available = True
            logger.debug("Connected to MemGraph at %s:%d", self.host, self.port)
            result = self.conn
            return result
        except ConnectionRefusedError:
            self.available = False
            logger.warning(
                "MemGraph connection refused at %s:%d",
                self.host,
                self.port,
            )
            result = ()
            return result
        except Exception as err:
            self.available = False
            logger.warning(
                "MemGraph unavailable at %s:%d (%s)",
                self.host,
                self.port,
                type(err).__name__,
            )
            result = ()
            return result

    def disconnect(self):
        """Close the connection to MemGraph."""
        with self._lock:
            if self.conn:
                self.conn.close()
                self.conn = ()
                self.available = False
                self.connection_attempted = False
                logger.info("Disconnected from MemGraph")

    def is_connected(self) -> bool:
        """Check if the connection is active."""
        with self._lock:
            if not self.conn:
                result = False
                return result
            try:
                cursor = self.conn.cursor()
                cursor.execute("RETURN 1")
                cursor.fetchall()
                result = True
                return result
            except Exception:
                self.conn = ()
                self.available = False
                result = False
                return result

    def execute(self, query: str, parameters=()) -> list:
        """Execute a Cypher query and return results as a list of dicts.

        Raises if MemGraph is unreachable or a query fails so graph absence and
        service unavailability are never conflated.
        Mutating or ambiguous Cypher is rejected before connecting.
        """
        if is_write_cypher(query):
            raise ValueError("ENGRAM graph access is read-only")

        result = self._execute_read_query(query, parameters)
        return result

    def _execute_read_query(self, query: str, parameters=()) -> list:
        """Execute a query already constrained to a read-only internal shape."""

        with self._lock:
            connection = self.conn
            if not connection:
                connection = self._connect_unlocked()
                if not connection:
                    raise RuntimeError(f"MemGraph is unavailable at {self.host}:{self.port}")

            try:
                cursor = connection.cursor()
                merged_parameters = self.visibility_parameters()
                if isinstance(parameters, Mapping):
                    merged_parameters.update(parameters)
                elif parameters:
                    raise ValueError("graph query parameters must be an object")
                cursor.execute(query, coerce_params(merged_parameters))
                columns = [desc.name for desc in cursor.description] if cursor.description else []
                rows = cursor.fetchall()
                result = [dict(zip(columns, row, strict=False)) for row in rows]
                return result
            except Exception as err:
                err_str = str(err).lower()
                if "does not exist" in err_str or "not found" in err_str or "no procedure named" in err_str:
                    logger.debug("Query failed as expected (%s)", type(err).__name__)
                else:
                    logger.error("Query failed (%s)", type(err).__name__)
                    if is_connection_error(err):
                        self.conn = ()
                        self.available = False
                raise RuntimeError(f"Query failed ({type(err).__name__})") from err

    def visibility_parameters(self) -> dict:
        """Return exact non-user visibility parameters for every graph read."""
        return visibility_parameters(self.visibility_scope)

    def vector_search_propositions(
        self,
        embedding: list[float],
        *,
        index_name: str = "proposition_embeddings",
        limit: int = 250,
        min_similarity: float = 0.45,
    ) -> list:
        """Search active proof-canonical Propositions through one fixed ANN query.

        Generic ``CALL`` remains forbidden on :meth:`execute`. This method is
        the sole procedure exception and exposes no caller-supplied Cypher;
        only a validated vector-index identifier and bounded scalar parameters
        reach the hard-coded read query.
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
        query = """
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
        result = self._execute_read_query(
            query,
            {
                "index_name": index_name,
                "limit": limit,
                "query_embedding": embedding,
                "min_similarity": float(min_similarity),
            },
        )
        return result

    def structured_proposition_projections(
        self,
        value: str,
        *,
        projection_id: PropositionProjectionQuery,
        limit: int = 10,
    ) -> list[PropositionProjection]:
        """Run one allow-listed structured Proposition projection and strictly decode its rows."""
        term = _projection_text(
            value, "Proposition projection search value", MAX_PROPOSITION_PROJECTION_TERM_BYTES, allow_empty=False
        )
        if projection_id == PropositionProjectionQuery.STRUCTURED_ENTITY_V1:
            query = STRUCTURED_ENTITY_PROPOSITION_PROJECTION_QUERY
        elif projection_id == PropositionProjectionQuery.STRUCTURED_KEYWORD_V1:
            query = STRUCTURED_KEYWORD_PROPOSITION_PROJECTION_QUERY
        else:
            raise InvalidRequestError("structured Proposition projection query identifier is unsupported")
        row_limit = _projection_int(limit, "Proposition projection limit", 1, MAX_PROPOSITION_PROJECTION_ROWS)
        rows = self._execute_read_query(query, {"value": term, "limit": row_limit})
        result = _decode_projection_rows(rows, projection_id, "", row_limit)
        return result

    def canonical_entity_matches(self, surface: str, *, limit: int = MAX_RELATION_CANDIDATES) -> list[CanonicalEntityMatch]:
        """Resolve an entity surface through one fixed, read-only query."""
        term = _projection_text(surface, "canonical entity surface", MAX_RELATION_LABEL_BYTES, allow_empty=False)
        row_limit = _projection_int(limit, "canonical entity match limit", 1, MAX_RELATION_CANDIDATES)
        rows = self._execute_read_query(CANONICAL_ENTITY_MATCH_QUERY, {"surface": term, "limit": row_limit})
        if not isinstance(rows, list) or len(rows) > row_limit:
            raise InvalidRequestError("canonical entity query returned an invalid collection")
        decoded = [canonical_entity_match_from_graph_row(row) for row in rows]
        if len({row["canonical_id"] for row in decoded}) != len(decoded):
            raise InvalidRequestError("canonical entity query returned duplicate identities")
        return sorted(decoded, key=lambda row: row["canonical_id"])

    def canonical_predicate_matches(self, surface: str, *, limit: int = MAX_RELATION_CANDIDATES) -> list[CanonicalPredicateMatch]:
        """Resolve a Predicate surface through one fixed, read-only query."""
        term = _projection_text(surface, "canonical Predicate surface", MAX_RELATION_LABEL_BYTES, allow_empty=False)
        row_limit = _projection_int(limit, "canonical Predicate match limit", 1, MAX_RELATION_CANDIDATES)
        rows = self._execute_read_query(CANONICAL_PREDICATE_MATCH_QUERY, {"surface": term, "limit": row_limit})
        if not isinstance(rows, list) or len(rows) > row_limit:
            raise InvalidRequestError("canonical Predicate query returned an invalid collection")
        decoded = [canonical_predicate_match_from_graph_row(row) for row in rows]
        if len({row["canonical_id"] for row in decoded}) != len(decoded):
            raise InvalidRequestError("canonical Predicate query returned duplicate identities")
        return sorted(decoded, key=lambda row: row["canonical_id"])

    def relation_one_hop_proposition_projections(
        self,
        subject_entity_id: str,
        predicate_id: str,
        *,
        limit: int = MAX_RELATION_PLAN_ROWS,
        include_historical: bool = False,
    ) -> list[RelationPropositionProjection]:
        """Execute only the allow-listed parameterized one-hop Proposition template."""
        subject = _projection_identifier(subject_entity_id, "relation subject_entity_id")
        predicate = _projection_identifier(predicate_id, "relation predicate_id")
        if not isinstance(include_historical, bool):
            raise InvalidRequestError("relation include_historical must be a boolean")
        row_limit = _projection_int(limit, "relation one-hop limit", 1, MAX_RELATION_PLAN_ROWS)
        rows = self._execute_read_query(
            RELATION_ONE_HOP_PROPOSITION_PROJECTION_QUERY,
            {
                "subject_entity_id": subject,
                "predicate_id": predicate,
                "include_historical": include_historical,
                "limit": row_limit,
            },
        )
        return _decode_relation_projection_rows(rows, row_limit)

    def vector_search_proposition_projections(
        self,
        embedding: list[float],
        *,
        index_name: str = "proposition_embeddings",
        limit: int = 10,
        min_similarity: float = 0.45,
    ) -> list[PropositionProjection]:
        """Run the fixed ANN Proposition projection without returning graph prose or arbitrary properties."""
        if not isinstance(index_name, str) or not VECTOR_INDEX_NAME.fullmatch(index_name):
            raise InvalidRequestError("invalid Proposition projection vector index name")
        if not isinstance(embedding, list) or not embedding:
            raise InvalidRequestError("Proposition projection embedding must be a non-empty list")
        if len(embedding) > MAX_PROPOSITION_PROJECTION_EMBEDDING_DIMENSIONS:
            raise InvalidRequestError(
                f"Proposition projection embedding exceeds the limit of {MAX_PROPOSITION_PROJECTION_EMBEDDING_DIMENSIONS} dimensions"
            )
        for component in embedding:
            if isinstance(component, bool) or not isinstance(component, (int, float)) or not math.isfinite(float(component)):
                raise InvalidRequestError("Proposition projection embedding must contain finite numeric values")
        row_limit = _projection_int(limit, "Proposition projection vector limit", 1, MAX_PROPOSITION_PROJECTION_ROWS)
        similarity = _projection_score(min_similarity, True, "Proposition projection min_similarity")
        rows = self._execute_read_query(
            VECTOR_PROPOSITION_PROJECTION_QUERY,
            {
                "index_name": index_name,
                "limit": row_limit,
                "query_embedding": embedding,
                "min_similarity": similarity,
            },
        )
        result = _decode_projection_rows(rows, PropositionProjectionQuery.VECTOR_V1, index_name, row_limit)
        return result

    def proposition_projection_by_id(self, proposition_id: str) -> list[PropositionProjection]:
        """Re-read one Proposition through the fixed canonical projection for publication revalidation."""
        identifier = _projection_identifier(proposition_id, "Proposition projection revalidation proposition_id")
        rows = self._execute_read_query(PROPOSITION_PROJECTION_BY_ID_QUERY, {"proposition_id": identifier})
        result = _decode_projection_rows(rows, PropositionProjectionQuery.BY_ID_V1, "", 1)
        return result

    def execute_read(self, query: str, parameters=()) -> list:
        """Read-only alias for execute()."""
        result = self.execute(query, parameters)
        return result


def create_graph_client(
    host: str = "localhost",
    port: int = 7687,
    username: str = "",
    password: str = "",
    deployment_mode: str = "",
    visibility_scope=(),
) -> MemGraphConnection:
    """Create a MemGraph connection.

    The connection and deployment-specific schema preflight are attempted
    immediately. An unreachable or incompatible graph fails startup rather
    than presenting an empty query result as graph readiness.
    """
    if deployment_mode not in {"standalone", "tapestry_managed"}:
        raise ValueError("enabled graph requires an explicit deployment mode")
    client = MemGraphConnection(
        host=host,
        port=port,
        username=username,
        password=password,
        visibility_scope=visibility_scope,
    )
    if not client.connect():
        raise RuntimeError(f"Memgraph is unavailable at {host}:{port}")
    schema_path = Path(__file__).resolve().parents[1] / "schema.cypher"
    report = verify_schema(client, schema_path, deployment_mode)
    if not report.get("compatible", False):
        client.disconnect()
        raise RuntimeError(f"Memgraph schema preflight failed: {report}")
    client.schema_report = report
    return client
