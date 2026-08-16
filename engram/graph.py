"""Knowledge Graph integration for ENGRAM.

Connects to a MemGraph instance using the pymgclient driver. Runtime access is
strictly read-only: every public execution path rejects mutating Cypher before
opening a connection.

Degrades gracefully when MemGraph is unreachable: read calls return an empty
list. A backend swap
(e.g. to a different Bolt-speaking store) is a sibling module with the same
method names — duck typing is the contract, so there is no abstract base class.
"""

import logging
import math
import re
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from uuid import UUID

import mgclient

from engram.errors import InvalidRequestError

logger = logging.getLogger(__name__)

# Seconds to wait before retrying after a failed connection attempt. A single
# process hitting an unreachable host repeatedly should not block on TCP
# timeouts for every query.
RECONNECT_COOLDOWN_SECONDS = 60

# Substrings that mark an OperationalError as a lost socket rather than a
# server-side query rejection (storage timeout, lock contention, syntax error).
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


def is_connection_error(err: Exception) -> bool:
    """Distinguish socket-level failures from query-level failures.

    Returns True only when the exception indicates the TCP connection is dead.
    Query-level errors (storage timeouts, lock contention, syntax errors) return
    False -- the socket is still usable and the reconnect cooldown does not apply.
    """
    error_name = type(err).__name__
    if isinstance(err, (ConnectionError, BrokenPipeError, OSError)) or error_name == "InterfaceError":
        return True
    if error_name == "OperationalError":
        err_str = str(err).lower()
        return any(marker in err_str for marker in CONNECTION_LOST_MARKERS)
    return False


# Cypher clauses that can mutate the graph. ENGRAM's graph role is recall, so
# the read-only template path refuses any query carrying one of these. CALL is
# included because stored procedures can write regardless of the surrounding
# query's shape, and LOAD because LOAD CSV imports data; a read-only path that
# allowed either would not be read-only.
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
_EXTERNAL_NULL = {}.get("missing")


class ClaimProjectionQuery(StrEnum):
    """Allow-listed fixed query identifiers for full Claim projection."""

    STRUCTURED_ENTITY_V1 = "structured_entity_claim_projection_v1"
    STRUCTURED_KEYWORD_V1 = "structured_keyword_claim_projection_v1"
    VECTOR_V1 = "vector_claim_projection_v1"
    BY_ID_V1 = "claim_projection_by_id_v1"


CLAIM_PROJECTION_FIELDS = frozenset(
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
    identifier = _projection_text(value, name, MAX_CLAIM_PROJECTION_IDENTIFIER_BYTES, allow_empty=False)
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
    if not available and value is _EXTERNAL_NULL:
        return 0.0
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
        if value is _EXTERNAL_NULL or value == "":
            return ""
        raise InvalidRequestError(f"{name} must be empty when unavailable")
    if isinstance(value, datetime):
        if not value.tzinfo or value.utcoffset() != timedelta(0):
            raise InvalidRequestError(f"{name} must be timezone-aware UTC")
        text = value.isoformat().replace("+00:00", "Z")
    else:
        text = _projection_text(value, name, MAX_CLAIM_PROJECTION_TIMESTAMP_BYTES, allow_empty=False)
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
        if value is _EXTERNAL_NULL or value == "":
            return ""
        raise InvalidRequestError(f"{name} must be empty when unavailable")
    return _projection_text(value, name, maximum_bytes, allow_empty=False)


@dataclass(frozen=True, slots=True)
class ClaimProjection:
    """Strict allow-listed Claim row decoded at the graph capability boundary."""

    claim_id: str
    subject_entity_id: str
    predicate_id: str
    object_entity_id: str
    invalidated_at: str
    invalidated_at_available: bool
    system_from: str
    system_from_available: bool
    system_to: str
    system_to_available: bool
    valid_from: str
    valid_from_available: bool
    valid_to: str
    valid_to_available: bool
    predicate_canonical: bool
    ownership_category: str
    trust_category: str
    trust_category_available: bool
    supplied_trust: float
    supplied_trust_available: bool
    supplied_trust_version: int
    supplied_trust_version_available: bool
    structured_match: float
    structured_match_available: bool
    semantic_similarity: float
    semantic_similarity_available: bool
    projection_id: ClaimProjectionQuery
    vector_index_id: str
    vector_index_id_available: bool

    def __post_init__(self) -> None:
        _projection_identifier(self.claim_id, "Claim projection claim_id")
        _projection_identifier(self.subject_entity_id, "Claim projection subject_entity_id")
        _projection_identifier(self.predicate_id, "Claim projection predicate_id")
        _projection_identifier(self.object_entity_id, "Claim projection object_entity_id")
        invalidated_available = _projection_bool(self.invalidated_at_available, "Claim projection invalidated_at_available")
        system_from_available = _projection_bool(self.system_from_available, "Claim projection system_from_available")
        system_to_available = _projection_bool(self.system_to_available, "Claim projection system_to_available")
        valid_from_available = _projection_bool(self.valid_from_available, "Claim projection valid_from_available")
        valid_to_available = _projection_bool(self.valid_to_available, "Claim projection valid_to_available")
        _projection_timestamp(self.invalidated_at, invalidated_available, "Claim projection invalidated_at")
        system_from = _projection_timestamp(self.system_from, system_from_available, "Claim projection system_from")
        system_to = _projection_timestamp(self.system_to, system_to_available, "Claim projection system_to")
        valid_from = _projection_timestamp(self.valid_from, valid_from_available, "Claim projection valid_from")
        valid_to = _projection_timestamp(self.valid_to, valid_to_available, "Claim projection valid_to")
        if (
            system_from_available
            and system_to_available
            and datetime.fromisoformat(system_from[:-1] + "+00:00") >= datetime.fromisoformat(system_to[:-1] + "+00:00")
        ):
            raise InvalidRequestError("Claim projection system_from must be earlier than system_to")
        if (
            valid_from_available
            and valid_to_available
            and datetime.fromisoformat(valid_from[:-1] + "+00:00") >= datetime.fromisoformat(valid_to[:-1] + "+00:00")
        ):
            raise InvalidRequestError("Claim projection valid_from must be earlier than valid_to")
        _projection_bool(self.predicate_canonical, "Claim projection predicate_canonical")
        if self.ownership_category not in {"PUBLIC", "COMPANY", "CUSTOMER"}:
            raise InvalidRequestError("Claim projection ownership_category is unsupported")
        trust_category_available = _projection_bool(self.trust_category_available, "Claim projection trust_category_available")
        _optional_projection_text(self.trust_category, trust_category_available, "Claim projection trust_category", 96)
        trust_available = _projection_bool(self.supplied_trust_available, "Claim projection supplied_trust_available")
        _projection_score(self.supplied_trust, trust_available, "Claim projection supplied_trust")
        version_available = _projection_bool(
            self.supplied_trust_version_available, "Claim projection supplied_trust_version_available"
        )
        version = _projection_int(self.supplied_trust_version, "Claim projection supplied_trust_version", 0, 2_147_483_647)
        if not version_available and version != 0:
            raise InvalidRequestError("Claim projection supplied_trust_version must be zero when unavailable")
        if trust_available != version_available:
            raise InvalidRequestError("Claim projection supplied trust value and version availability must match")
        if version_available and not version:
            raise InvalidRequestError("Claim projection supplied_trust_version must be positive when available")
        structured_available = _projection_bool(self.structured_match_available, "Claim projection structured_match_available")
        _projection_score(self.structured_match, structured_available, "Claim projection structured_match")
        semantic_available = _projection_bool(self.semantic_similarity_available, "Claim projection semantic_similarity_available")
        _projection_score(self.semantic_similarity, semantic_available, "Claim projection semantic_similarity")
        if not isinstance(self.projection_id, ClaimProjectionQuery):
            raise InvalidRequestError("Claim projection projection_id must be a ClaimProjectionQuery")
        vector_available = _projection_bool(self.vector_index_id_available, "Claim projection vector_index_id_available")
        if vector_available:
            if not isinstance(self.vector_index_id, str) or not VECTOR_INDEX_NAME.fullmatch(self.vector_index_id):
                raise InvalidRequestError("Claim projection vector_index_id is invalid")
        elif self.vector_index_id:
            raise InvalidRequestError("Claim projection vector_index_id must be empty when unavailable")
        if self.projection_id == ClaimProjectionQuery.VECTOR_V1:
            if structured_available or not semantic_available or not vector_available:
                raise InvalidRequestError("vector Claim projection measurements or index provenance are inconsistent")
        elif self.projection_id == ClaimProjectionQuery.BY_ID_V1:
            if structured_available or semantic_available or vector_available:
                raise InvalidRequestError("by-ID Claim projection measurements or index provenance are inconsistent")
        elif not structured_available or semantic_available or vector_available:
            raise InvalidRequestError("structured Claim projection measurements or index provenance are inconsistent")

    @classmethod
    def from_graph_row(
        cls,
        value: Mapping[str, object],
        projection_id: ClaimProjectionQuery,
        vector_index_id: str = "",
    ) -> "ClaimProjection":
        if not isinstance(value, Mapping):
            raise InvalidRequestError("Claim projection row must be an object")
        observed = frozenset(value)
        if observed != CLAIM_PROJECTION_FIELDS:
            raise InvalidRequestError(
                "Claim projection row has invalid fields: "
                f"missing={sorted(CLAIM_PROJECTION_FIELDS - observed)}, "
                f"extra={sorted(observed - CLAIM_PROJECTION_FIELDS)}"
            )
        if not isinstance(projection_id, ClaimProjectionQuery):
            raise InvalidRequestError("Claim projection query identifier is unsupported")
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
        supplied_version = (
            0
            if not version_available and raw_version is _EXTERNAL_NULL
            else _projection_int(raw_version, "supplied_trust_version", 0, 2_147_483_647)
        )
        return cls(
            claim_id=_projection_identifier(value["claim_id"], "Claim projection claim_id"),
            subject_entity_id=_projection_identifier(value["subject_entity_id"], "Claim projection subject_entity_id"),
            predicate_id=_projection_identifier(value["predicate_id"], "Claim projection predicate_id"),
            object_entity_id=_projection_identifier(value["object_entity_id"], "Claim projection object_entity_id"),
            invalidated_at=_projection_timestamp(value["invalidated_at"], invalidated_available, "Claim projection invalidated_at"),
            invalidated_at_available=invalidated_available,
            system_from=_projection_timestamp(value["system_from"], system_from_available, "Claim projection system_from"),
            system_from_available=system_from_available,
            system_to=_projection_timestamp(value["system_to"], system_to_available, "Claim projection system_to"),
            system_to_available=system_to_available,
            valid_from=_projection_timestamp(value["valid_from"], valid_from_available, "Claim projection valid_from"),
            valid_from_available=valid_from_available,
            valid_to=_projection_timestamp(value["valid_to"], valid_to_available, "Claim projection valid_to"),
            valid_to_available=valid_to_available,
            predicate_canonical=_projection_bool(value["predicate_canonical"], "predicate_canonical"),
            ownership_category=_projection_text(value["ownership_category"], "ownership_category", 32, allow_empty=False),
            trust_category=_optional_projection_text(value["trust_category"], trust_category_available, "trust_category", 96),
            trust_category_available=trust_category_available,
            supplied_trust=_projection_score(value["supplied_trust"], trust_available, "supplied_trust"),
            supplied_trust_available=trust_available,
            supplied_trust_version=supplied_version,
            supplied_trust_version_available=version_available,
            structured_match=_projection_score(value["structured_match"], structured_available, "structured_match"),
            structured_match_available=structured_available,
            semantic_similarity=_projection_score(value["semantic_similarity"], semantic_available, "semantic_similarity"),
            semantic_similarity_available=semantic_available,
            projection_id=projection_id,
            vector_index_id=vector_index_id,
            vector_index_id_available=bool(vector_index_id),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "claim_id": self.claim_id,
            "subject_entity_id": self.subject_entity_id,
            "predicate_id": self.predicate_id,
            "object_entity_id": self.object_entity_id,
            "invalidated_at": self.invalidated_at,
            "invalidated_at_available": self.invalidated_at_available,
            "system_from": self.system_from,
            "system_from_available": self.system_from_available,
            "system_to": self.system_to,
            "system_to_available": self.system_to_available,
            "valid_from": self.valid_from,
            "valid_from_available": self.valid_from_available,
            "valid_to": self.valid_to,
            "valid_to_available": self.valid_to_available,
            "predicate_canonical": self.predicate_canonical,
            "ownership_category": self.ownership_category,
            "trust_category": self.trust_category,
            "trust_category_available": self.trust_category_available,
            "supplied_trust": self.supplied_trust,
            "supplied_trust_available": self.supplied_trust_available,
            "supplied_trust_version": self.supplied_trust_version,
            "supplied_trust_version_available": self.supplied_trust_version_available,
            "structured_match": self.structured_match,
            "structured_match_available": self.structured_match_available,
            "semantic_similarity": self.semantic_similarity,
            "semantic_similarity_available": self.semantic_similarity_available,
            "projection_id": self.projection_id.value,
            "vector_index_id": self.vector_index_id,
            "vector_index_id_available": self.vector_index_id_available,
        }


def _decode_projection_rows(
    rows: object,
    projection_id: ClaimProjectionQuery,
    vector_index_id: str,
    limit: int,
) -> list[ClaimProjection]:
    if not isinstance(rows, list):
        raise InvalidRequestError("Claim projection query must return a list")
    if len(rows) > limit or len(rows) > MAX_CLAIM_PROJECTION_ROWS:
        raise InvalidRequestError("Claim projection query returned more rows than requested")
    decoded: dict[str, ClaimProjection] = {}
    for row in rows:
        projection = ClaimProjection.from_graph_row(row, projection_id, vector_index_id)
        if projection.claim_id in decoded and decoded[projection.claim_id] != projection:
            raise InvalidRequestError(f"conflicting Claim projections for Claim ID: {projection.claim_id}")
        decoded[projection.claim_id] = projection
    return [decoded[claim_id] for claim_id in sorted(decoded)]


def is_write_cypher(cypher: str) -> bool:
    """Return True if the Cypher can mutate the graph.

    Detects the write clauses (CREATE / MERGE / DELETE / SET / REMOVE / ...)
    plus procedure invocation (CALL) and data import (LOAD) as whole words,
    case-insensitively. Conservative: an ambiguous query is treated as a
    write, so the recall-only path refuses it rather than risk a silent
    mutation -- this also refuses read-only procedures, which is the accepted
    cost of a blocklist that cannot inspect procedure bodies.
    """
    return bool(WRITE_CLAUSE.search(cypher or ""))


def coerce_params(parameters):
    """Coerce values that mgclient cannot accept as Cypher parameters.

    UUID objects fail with "value of type 'UUID' can't be used as query
    parameter". This is the single chokepoint where every query hits the driver,
    so coerce here defensively. Recurses into dicts, lists, and tuples so UUIDs
    nested inside batch payloads are coerced too.
    """
    if isinstance(parameters, UUID):
        return str(parameters)
    if isinstance(parameters, dict):
        return {key: coerce_params(value) for key, value in parameters.items()}
    if isinstance(parameters, list):
        return [coerce_params(item) for item in parameters]
    if isinstance(parameters, tuple):
        return tuple(coerce_params(item) for item in parameters)
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

    Degrades gracefully when MemGraph is unreachable. After a failed connection
    attempt, further execute() calls return empty results for
    RECONNECT_COOLDOWN_SECONDS instead of blocking on repeated TCP timeouts. The
    cooldown resets after a successful reconnect.
    """

    def __init__(
        self,
        host: str = "localhost",
        port: int = 7687,
        username: str = "",
        password: str = "",
    ):
        self.host = host
        self.port = port
        self.username = username
        self.password = password
        self.conn = ()
        self.available = False
        self.connection_attempted = False
        self.last_connect_attempt = 0.0
        # pymgclient connections may be shared between threads but not used
        # concurrently. Serialize all connection and cursor access.
        self._lock = threading.RLock()

    def connect(self):
        """Establish a connection to MemGraph.

        Returns the connection on success, or an empty tuple on failure. Sets self.available
        so callers can check without retrying.
        """
        with self._lock:
            return self._connect_unlocked()

    def _connect_unlocked(self):
        """Establish a connection while the caller holds the connection lock."""
        if self.conn:
            return self.conn

        # Respect cooldown after a failed attempt
        if self.connection_attempted and not self.available:
            elapsed = time.monotonic() - self.last_connect_attempt
            if elapsed < RECONNECT_COOLDOWN_SECONDS:
                return ()

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
            return self.conn
        except ConnectionRefusedError:
            self.available = False
            logger.warning(
                "MemGraph connection refused at %s:%d -- graph queries will return empty results",
                self.host,
                self.port,
            )
            return ()
        except Exception as err:
            self.available = False
            logger.warning(
                "MemGraph unavailable at %s:%d: %s -- graph queries will return empty results",
                self.host,
                self.port,
                err,
            )
            return ()

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
                return False
            try:
                cursor = self.conn.cursor()
                cursor.execute("RETURN 1")
                cursor.fetchall()
                return True
            except Exception:
                self.conn = ()
                self.available = False
                return False

    def execute(self, query: str, parameters=()) -> list:
        """Execute a Cypher query and return results as a list of dicts.

        Returns an empty list if MemGraph is unreachable; raises RuntimeError on
        a query-level failure so a real error is never mistaken for "no rows".
        Mutating or ambiguous Cypher is rejected before connecting.
        """
        if is_write_cypher(query):
            raise ValueError("ENGRAM graph access is read-only")

        return self._execute_read_query(query, parameters)

    def _execute_read_query(self, query: str, parameters=()) -> list:
        """Execute a query already constrained to a read-only internal shape."""

        with self._lock:
            connection = self.conn
            if not connection:
                connection = self._connect_unlocked()
                if not connection:
                    return []

            try:
                cursor = connection.cursor()
                cursor.execute(query, coerce_params(parameters) or {})
                columns = [desc.name for desc in cursor.description] if cursor.description else []
                rows = cursor.fetchall()
                return [dict(zip(columns, row, strict=False)) for row in rows]
            except Exception as err:
                err_str = str(err).lower()
                if "does not exist" in err_str or "not found" in err_str or "no procedure named" in err_str:
                    logger.debug("Query failed (expected): %s: %s", type(err).__name__, err)
                else:
                    logger.error("Query failed: %s: %s", type(err).__name__, err)
                    if is_connection_error(err):
                        self.conn = ()
                        self.available = False
                raise RuntimeError(f"Query failed: {err}") from err

    def vector_search_claims(
        self,
        embedding: list[float],
        *,
        index_name: str = "claim_premise_embeddings",
        limit: int = 250,
        min_similarity: float = 0.45,
    ) -> list:
        """Search active proof-canonical Claims through one fixed ANN query.

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
            WITH node AS claim, 1.0 - distance AS similarity
            MATCH (claim)-[:USES_PREDICATE]->(predicate:Predicate)
            OPTIONAL MATCH (claim)-[:HAS_OBJECT]->(object:Entity)
            WHERE similarity >= $min_similarity
              AND claim.invalidated_at IS NULL
              AND claim.system_to IS NULL
              AND predicate.canonical_id <> 'generic_relation'
              AND coalesce(claim.predicate_canonical, true) = true
              AND (trim(coalesce(claim.object, '')) = '' OR object IS NOT NULL)
            RETURN DISTINCT claim.id AS claim_id,
                   claim.subject AS subject,
                   claim.predicate AS predicate,
                   claim.object AS object,
                   similarity
            ORDER BY similarity DESC, claim.id
        """
        return self._execute_read_query(
            query,
            {
                "index_name": index_name,
                "limit": limit,
                "query_embedding": embedding,
                "min_similarity": float(min_similarity),
            },
        )

    def structured_claim_projections(
        self,
        value: str,
        *,
        projection_id: ClaimProjectionQuery,
        limit: int = 10,
    ) -> list[ClaimProjection]:
        """Run one allow-listed structured Claim projection and strictly decode its rows."""
        term = _projection_text(value, "Claim projection search value", MAX_CLAIM_PROJECTION_TERM_BYTES, allow_empty=False)
        if projection_id == ClaimProjectionQuery.STRUCTURED_ENTITY_V1:
            query = STRUCTURED_ENTITY_CLAIM_PROJECTION_QUERY
        elif projection_id == ClaimProjectionQuery.STRUCTURED_KEYWORD_V1:
            query = STRUCTURED_KEYWORD_CLAIM_PROJECTION_QUERY
        else:
            raise InvalidRequestError("structured Claim projection query identifier is unsupported")
        row_limit = _projection_int(limit, "Claim projection limit", 1, MAX_CLAIM_PROJECTION_ROWS)
        rows = self._execute_read_query(query, {"value": term, "limit": row_limit})
        return _decode_projection_rows(rows, projection_id, "", row_limit)

    def vector_search_claim_projections(
        self,
        embedding: list[float],
        *,
        index_name: str = "claim_premise_embeddings",
        limit: int = 10,
        min_similarity: float = 0.45,
    ) -> list[ClaimProjection]:
        """Run the fixed ANN Claim projection without returning graph prose or arbitrary properties."""
        if not isinstance(index_name, str) or not VECTOR_INDEX_NAME.fullmatch(index_name):
            raise InvalidRequestError("invalid Claim projection vector index name")
        if not isinstance(embedding, list) or not embedding:
            raise InvalidRequestError("Claim projection embedding must be a non-empty list")
        if len(embedding) > MAX_CLAIM_PROJECTION_EMBEDDING_DIMENSIONS:
            raise InvalidRequestError(
                "Claim projection embedding exceeds the limit of " f"{MAX_CLAIM_PROJECTION_EMBEDDING_DIMENSIONS} dimensions"
            )
        for component in embedding:
            if isinstance(component, bool) or not isinstance(component, (int, float)) or not math.isfinite(float(component)):
                raise InvalidRequestError("Claim projection embedding must contain finite numeric values")
        row_limit = _projection_int(limit, "Claim projection vector limit", 1, MAX_CLAIM_PROJECTION_ROWS)
        similarity = _projection_score(min_similarity, True, "Claim projection min_similarity")
        rows = self._execute_read_query(
            VECTOR_CLAIM_PROJECTION_QUERY,
            {
                "index_name": index_name,
                "limit": row_limit,
                "query_embedding": embedding,
                "min_similarity": similarity,
            },
        )
        return _decode_projection_rows(rows, ClaimProjectionQuery.VECTOR_V1, index_name, row_limit)

    def claim_projection_by_id(self, claim_id: str) -> list[ClaimProjection]:
        """Re-read one Claim through the fixed canonical projection for publication revalidation."""
        identifier = _projection_identifier(claim_id, "Claim projection revalidation claim_id")
        rows = self._execute_read_query(CLAIM_PROJECTION_BY_ID_QUERY, {"claim_id": identifier})
        return _decode_projection_rows(rows, ClaimProjectionQuery.BY_ID_V1, "", 1)

    def execute_read(self, query: str, parameters=()) -> list:
        """Read-only alias for execute()."""
        return self.execute(query, parameters)


def create_graph_client(
    host: str = "localhost",
    port: int = 7687,
    username: str = "",
    password: str = "",
) -> MemGraphConnection:
    """Create a MemGraph connection.

    The connection is attempted immediately so enabled graph readiness is
    established during startup. An unreachable host leaves a concrete,
    unavailable client whose reads fail soft with empty lists.
    """
    client = MemGraphConnection(host=host, port=port, username=username, password=password)
    client.connect()
    return client
