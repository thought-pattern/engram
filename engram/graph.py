"""Knowledge Graph integration for ENGRAM.

Connects to a MemGraph instance using the same interface Tapestry uses: the
pymgclient driver, a host/port connection, and execute / execute_read /
execute_write methods that return a list of row dicts. ENGRAM has read-only
access to the graph in its recall role, but the connection class exposes the
full method surface so a standalone deployment can also author triples.

Degrades gracefully when MemGraph is unreachable: read calls return an empty
list and write calls raise so a lost write is never silent. A backend swap
(e.g. to a different Bolt-speaking store) is a sibling module with the same
method names — duck typing is the contract, so there is no abstract base class.
"""

import logging
import re
import time
from uuid import UUID

import mgclient

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
    if isinstance(err, (mgclient.InterfaceError, ConnectionError, BrokenPipeError, OSError)):  # noqa: UP038
        return True
    if isinstance(err, mgclient.OperationalError):
        err_str = str(err).lower()
        return any(marker in err_str for marker in CONNECTION_LOST_MARKERS)
    return False


# Cypher clauses that can mutate the graph. ENGRAM's graph role is recall, so
# the read-only template path refuses any query carrying one of these. CALL is
# included because stored procedures can write regardless of the surrounding
# query's shape, and LOAD because LOAD CSV imports data; a read-only path that
# allowed either would not be read-only.
WRITE_CLAUSE = re.compile(r"\b(CREATE|MERGE|DELETE|SET|REMOVE|DROP|DETACH|FOREACH|CALL|LOAD)\b", re.IGNORECASE)


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
        self.conn = None
        self.available = None  # None = unknown, True = connected, False = failed
        self.last_connect_attempt = 0.0

    def connect(self):
        """Establish a connection to MemGraph.

        Returns the connection on success, None on failure. Sets self.available
        so callers can check without retrying.
        """
        if self.conn is not None:
            return self.conn

        # Respect cooldown after a failed attempt
        if self.available is False:
            elapsed = time.monotonic() - self.last_connect_attempt
            if elapsed < RECONNECT_COOLDOWN_SECONDS:
                return None

        self.last_connect_attempt = time.monotonic()

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
            return None
        except Exception as err:
            self.available = False
            logger.warning(
                "MemGraph unavailable at %s:%d: %s -- graph queries will return empty results",
                self.host,
                self.port,
                err,
            )
            return None

    def disconnect(self):
        """Close the connection to MemGraph."""
        if self.conn is not None:
            self.conn.close()
            self.conn = None
            self.available = None
            logger.info("Disconnected from MemGraph")

    def is_connected(self) -> bool:
        """Check if the connection is active."""
        if self.conn is None:
            return False
        try:
            cursor = self.conn.cursor()
            cursor.execute("RETURN 1")
            cursor.fetchall()
            return True
        except Exception:
            self.conn = None
            self.available = False
            return False

    def execute(self, query: str, parameters: dict = None) -> list:
        """Execute a Cypher query and return results as a list of dicts.

        Returns an empty list if MemGraph is unreachable; raises RuntimeError on
        a query-level failure so a real error is never mistaken for "no rows".
        """
        if self.conn is None and self.connect() is None:
            return []

        try:
            cursor = self.conn.cursor()
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
                    self.conn = None
                    self.available = False
            raise RuntimeError(f"Query failed: {err}") from err

    def execute_read(self, query: str, parameters: dict = None) -> list:
        """Read-only alias for execute().

        MemGraph does not distinguish read/write transactions at the driver
        level, so this is a thin alias provided so callers can signal intent.
        """
        return self.execute(query, parameters)

    def execute_write(self, query: str, parameters: dict = None) -> list:
        """Execute a write query; return RETURN rows if the query has any.

        Raises RuntimeError if MemGraph is unreachable -- writes must not
        silently vanish. Callers that should survive a graph outage must catch
        RuntimeError at the call site and log the skip explicitly.
        """
        if self.conn is None and self.connect() is None:
            logger.error(
                "Write query skipped: MemGraph unreachable at %s:%d",
                self.host,
                self.port,
            )
            raise RuntimeError(f"Write query failed: MemGraph unreachable at {self.host}:{self.port}")

        try:
            cursor = self.conn.cursor()
            cursor.execute(query, coerce_params(parameters) or {})
            columns = [desc.name for desc in cursor.description] if cursor.description else []
            rows = cursor.fetchall()
            return [dict(zip(columns, row, strict=False)) for row in rows]
        except Exception as err:
            err_str = str(err).lower()
            if "does not exist" in err_str or "not found" in err_str or "no procedure named" in err_str:
                logger.debug("Write query failed (expected): %s", err)
            else:
                logger.error("Write query failed: %s", err)
                if is_connection_error(err):
                    self.conn = None
                    self.available = False
            raise RuntimeError(f"Write query failed: {err}") from err


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
