"""Knowledge Graph integration for ENGRAM.

Connects to a MemGraph instance using the pymgclient driver. Runtime access is
strictly read-only: every public execution path rejects mutating Cypher before
opening a connection.

Degrades gracefully when MemGraph is unreachable: read calls return an empty
list. A backend swap
(e.g. to a different Bolt-speaking store) is a sibling module with the same
method names — duck typing is the contract, so there is no abstract base class.
"""

from logging import getLogger as logging_getLogger
from re import IGNORECASE as re_IGNORECASE, compile as re_compile
from threading import RLock as threading_RLock
from time import monotonic as time_monotonic
from uuid import UUID

from mgclient import (
    InterfaceError as mgclient_InterfaceError,
    OperationalError as mgclient_OperationalError,
    connect as mgclient_connect,
)

_DEFAULT_ARGUMENT_DICT = {}

logger = logging_getLogger(__name__)

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
    if isinstance(err, (mgclient_InterfaceError, ConnectionError, BrokenPipeError, OSError)):
        return True
    if isinstance(err, mgclient_OperationalError):
        err_str = str(err).lower()
        _return_value = any(marker in err_str for marker in CONNECTION_LOST_MARKERS)
        return _return_value
    return False


# Cypher clauses that can mutate the graph. ENGRAM's graph role is recall, so
# the read-only template path refuses any query carrying one of these. CALL is
# included because stored procedures can write regardless of the surrounding
# query's shape, and LOAD because LOAD CSV imports data; a read-only path that
# allowed either would not be read-only.
WRITE_CLAUSE = re_compile(
    r"\b(CREATE|MERGE|DELETE|SET|REMOVE|DROP|DETACH|FOREACH|CALL|LOAD|" r"GRANT|DENY|REVOKE|ALTER|COPY|FREE)\b",
    re_IGNORECASE,
)
VECTOR_INDEX_NAME = re_compile(r"^[A-Za-z][A-Za-z0-9_]{0,127}$")


def is_write_cypher(cypher: str) -> bool:
    """Return True if the Cypher can mutate the graph.

    Detects the write clauses (CREATE / MERGE / DELETE / SET / REMOVE / ...)
    plus procedure invocation (CALL) and data import (LOAD) as whole words,
    case-insensitively. Conservative: an ambiguous query is treated as a
    write, so the recall-only path refuses it rather than risk a silent
    mutation -- this also refuses read-only procedures, which is the accepted
    cost of a blocklist that cannot inspect procedure bodies.
    """
    _return_value = bool(WRITE_CLAUSE.search(cypher or ""))
    return _return_value


def coerce_params(parameters):
    """Coerce values that mgclient cannot accept as Cypher parameters.

    UUID objects fail with "value of type 'UUID' can't be used as query
    parameter". This is the single chokepoint where every query hits the driver,
    so coerce here defensively. Recurses into dicts, lists, and tuples so UUIDs
    nested inside batch payloads are coerced too.
    """
    if isinstance(parameters, UUID):
        _return_value = str(parameters)
        return _return_value
    if isinstance(parameters, dict):
        _return_value = {key: coerce_params(value) for key, value in parameters.items()}
        return _return_value
    if isinstance(parameters, list):
        _return_value = [coerce_params(item) for item in parameters]
        return _return_value
    if isinstance(parameters, tuple):
        _return_value = tuple(coerce_params(item) for item in parameters)
        return _return_value
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
        self.conn = False
        self.available = False  # True = connected; False = unavailable.
        self.last_connect_attempt = 0.0
        # pymgclient connections may be shared between threads but not used
        # concurrently. Serialize all connection and cursor access.
        self._lock = threading_RLock()

    def connect(self):
        """Establish a connection to MemGraph.

        Returns the connection on success, or ``False`` on failure. Sets ``available``
        so callers can check without retrying.
        """
        with self._lock:
            _return_value = self._connect_unlocked()
            return _return_value

    def _connect_unlocked(self):
        """Establish a connection while the caller holds the connection lock."""
        if self.conn is not False:
            return self.conn

        # Respect cooldown after a failed attempt
        if self.available is False:
            elapsed = time_monotonic() - self.last_connect_attempt
            if elapsed < RECONNECT_COOLDOWN_SECONDS:
                return False

        self.last_connect_attempt = time_monotonic()

        try:
            connect_params = {
                "host": self.host,
                "port": self.port,
            }
            if self.username:
                connect_params["username"] = self.username
            if self.password:
                connect_params["password"] = self.password

            self.conn = mgclient_connect(**connect_params)
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
            return False
        except Exception as err:
            self.available = False
            logger.warning(
                "MemGraph unavailable at %s:%d: %s -- graph queries will return empty results",
                self.host,
                self.port,
                err,
            )
            return False

    def disconnect(self):
        """Close the connection to MemGraph."""
        with self._lock:
            if self.conn is not False:
                self.conn.close()
                self.conn = False
                self.available = False
                logger.info("Disconnected from MemGraph")
        return False

    def is_connected(self) -> bool:
        """Check if the connection is active."""
        with self._lock:
            if self.conn is False:
                return False
            try:
                cursor = self.conn.cursor()
                cursor.execute("RETURN 1")
                cursor.fetchall()
                return True
            except Exception:
                self.conn = False
                self.available = False
                return False

    def execute(self, query: str, parameters: dict = _DEFAULT_ARGUMENT_DICT) -> list:
        """Execute a Cypher query and return results as a list of dicts.

        Returns an empty list if MemGraph is unreachable; raises RuntimeError on
        a query-level failure so a real error is never mistaken for "no rows".
        Mutating or ambiguous Cypher is rejected before connecting.
        """
        if parameters is _DEFAULT_ARGUMENT_DICT:
            parameters = _DEFAULT_ARGUMENT_DICT.copy()
        if is_write_cypher(query):
            raise ValueError("ENGRAM graph access is read-only")

        _return_value = self._execute_read_query(query, parameters)
        return _return_value

    def _execute_read_query(self, query: str, parameters: dict = _DEFAULT_ARGUMENT_DICT) -> list:
        """Execute a query already constrained to a read-only internal shape."""

        if parameters is _DEFAULT_ARGUMENT_DICT:
            parameters = _DEFAULT_ARGUMENT_DICT.copy()
        with self._lock:
            if self.conn is False and self._connect_unlocked() is False:
                return []

            try:
                cursor = self.conn.cursor()
                cursor.execute(query, coerce_params(parameters) or {})
                columns = [desc.name for desc in cursor.description] if cursor.description else []
                rows = cursor.fetchall()
                _return_value = [dict(zip(columns, row, strict=False)) for row in rows]
                return _return_value
            except Exception as err:
                err_str = str(err).lower()
                if "does not exist" in err_str or "not found" in err_str or "no procedure named" in err_str:
                    logger.debug("Query failed (expected): %s: %s", type(err).__name__, err)
                else:
                    logger.error("Query failed: %s: %s", type(err).__name__, err)
                    if is_connection_error(err):
                        self.conn = False
                        self.available = False
                raise RuntimeError(f"Query failed: {err}") from err
        return []

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
        _return_value = self._execute_read_query(
            query,
            {
                "index_name": index_name,
                "limit": limit,
                "query_embedding": embedding,
                "min_similarity": float(min_similarity),
            },
        )
        return _return_value

    def execute_read(self, query: str, parameters: dict = _DEFAULT_ARGUMENT_DICT) -> list:
        """Read-only alias for execute()."""
        if parameters is _DEFAULT_ARGUMENT_DICT:
            parameters = _DEFAULT_ARGUMENT_DICT.copy()
        _return_value = self.execute(query, parameters)
        return _return_value


def create_graph_client(
    host: str = "localhost",
    port: int = 7687,
    username: str = "",
    password: str = "",
) -> MemGraphConnection:
    """Create a MemGraph connection.

    The connection is established lazily on the first query, so this never
    blocks and never raises on an unreachable host.
    """
    client = MemGraphConnection(host=host, port=port, username=username, password=password)
    return client
