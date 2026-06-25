"""Knowledge Graph integration for ENGRAM.

Provides an interface for connecting to graph databases (Memgraph, Neo4j)
for knowledge storage and retrieval.
"""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from neo4j import Driver


def GraphResult(success: bool, records: list[dict], error: object = "") -> dict:
    """Build a graph query result dict (keys: success, records, error)."""
    return {"success": success, "records": records, "error": error}


def graph_is_empty(result: dict) -> bool:
    """Check if a graph result has no records."""
    return len(result["records"]) == 0


def graph_single(result: dict) -> dict:
    """Get the single (first) record from a graph result, or an empty dict."""
    return result["records"][0] if result["records"] else {}


class GraphClient:
    """Base class for graph database clients.

    Implement this interface to connect ENGRAM to a graph database.
    """

    def execute(self, query: str, params=None) -> dict:
        """Execute a Cypher query.

        Args:
            query: Cypher query string.
            params: Query parameters.

        Returns:
            GraphResult dict with success status and records.
        """
        raise NotImplementedError

    def close(self) -> None:
        """Close the connection."""
        raise NotImplementedError


class Neo4jClient(GraphClient):
    """Neo4j/Memgraph client implementation."""

    def __init__(
        self,
        uri: str,
        username: str,
        password: str,
        database: str,
    ) -> None:
        """Initialize connection to Neo4j or Memgraph.

        Args:
            uri: Connection URI (e.g., bolt://localhost:7687).
            username: Database username.
            password: Database password.
            database: Database name (optional for some backends).
        """
        from neo4j import GraphDatabase

        auth = (username, password) if username else None
        self._driver: Driver = GraphDatabase.driver(uri, auth=auth)
        self._database = database

    def execute(
        self,
        query: str,
        params=None,
    ) -> dict:
        """Execute a Cypher query.

        Args:
            query: Cypher query string.
            params: Query parameters.

        Returns:
            GraphResult dict with success status and records.
        """
        try:
            with self._driver.session(database=self._database or None) as session:
                result = session.run(query, params or {})
                records = [dict(record) for record in result]
                return GraphResult(success=True, records=records)
        except Exception as e:
            return GraphResult(success=False, records=[], error=str(e))

    def close(self) -> None:
        """Close the database connection."""
        self._driver.close()


def create_graph_client(
    driver: str,
    uri: str,
    username: str = "",
    password: str = "",
    database: str = "",
) -> GraphClient:
    """Create a graph client based on driver type.

    Args:
        driver: Driver type (memgraph, neo4j).
        uri: Connection URI.
        username: Database username.
        password: Database password.
        database: Database name.

    Returns:
        GraphClient instance.

    Raises:
        ImportError: If required driver library is not installed.
        ValueError: If driver type is not supported.
    """
    if driver in ("memgraph", "neo4j"):
        try:
            return Neo4jClient(uri, username, password, database)
        except ImportError:
            raise ImportError(f"neo4j library required for {driver} driver. " "Install with: pip install neo4j")

    raise ValueError(f"Unsupported graph driver: {driver}")
