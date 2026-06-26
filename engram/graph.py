"""Knowledge Graph integration for ENGRAM.

Provides an interface for connecting to a Bolt-protocol graph database for
knowledge storage and retrieval. Memgraph and Neo4j both speak Bolt and use the
same official ``neo4j`` Python driver, so a single client serves either backend.
"""

from neo4j import Driver, GraphDatabase


def graph_result(success: bool, records: list[dict], error: object = "") -> dict:
    """Build a graph query result dict (keys: success, records, error)."""
    result = {"success": success, "records": records, "error": error}
    return result


def graph_is_empty(result: dict) -> bool:
    """Check if a graph result has no records."""
    is_empty = len(result["records"]) == 0
    return is_empty


def graph_single(result: dict) -> dict:
    """Get the single (first) record from a graph result, or an empty dict."""
    record = result["records"][0] if result["records"] else {}
    return record


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


class BoltGraphClient(GraphClient):
    """Graph client for any Bolt-protocol database (Memgraph or Neo4j).

    Memgraph and Neo4j both speak the Bolt protocol and use the same official
    ``neo4j`` Python driver, so one client serves both. The backend is
    determined entirely by the connection URI.
    """

    def __init__(
        self,
        uri: str,
        username: str,
        password: str,
        database: str,
    ) -> None:
        """Open a Bolt connection to the graph database.

        Args:
            uri: Connection URI (e.g., bolt://localhost:7687).
            username: Database username.
            password: Database password.
            database: Database name (optional for some backends).
        """
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
                ok_result = graph_result(success=True, records=records)
                return ok_result
        except Exception as e:
            err_result = graph_result(success=False, records=[], error=str(e))
            return err_result

    def close(self) -> None:
        """Close the database connection."""
        self._driver.close()


def create_graph_client(
    uri: str,
    username: str = "",
    password: str = "",
    database: str = "",
) -> GraphClient:
    """Create a Bolt-protocol graph client (Memgraph or Neo4j).

    Both backends use the same ``neo4j`` library over Bolt, so the connection is
    determined entirely by the URI and credentials.

    Args:
        uri: Connection URI (e.g., bolt://localhost:7687).
        username: Database username.
        password: Database password.
        database: Database name.

    Returns:
        GraphClient instance.
    """
    client = BoltGraphClient(uri, username, password, database)
    return client
