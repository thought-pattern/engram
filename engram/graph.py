"""Knowledge Graph integration for ENGRAM.

Provides an interface for connecting to graph databases (Memgraph, Neo4j)
for knowledge storage and retrieval.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from neo4j import Driver


@dataclass
class GraphResult:
    """Result from a graph query."""

    success: bool
    records: list[dict]
    error: object = None

    @property
    def is_empty(self) -> bool:
        """Check if result has no records."""
        return len(self.records) == 0

    @property
    def single(self) -> dict:
        """Get single record or None."""
        return self.records[0] if self.records else None


class GraphClient:
    """Base class for graph database clients.

    Implement this interface to connect ENGRAM to a graph database.
    """

    def execute(self, query: str, params: dict | None = None) -> GraphResult:
        """Execute a Cypher query.

        Args:
            query: Cypher query string.
            params: Query parameters.

        Returns:
            GraphResult with success status and records.
        """
        raise NotImplementedError

    def close(self) -> None:
        """Close the connection."""
        raise NotImplementedError


class MockGraphClient(GraphClient):
    """Mock graph client for testing.

    Stores data in memory using a simple dict structure.
    """

    def __init__(self) -> None:
        """Initialize mock client."""
        self._nodes: dict[str, dict] = {}  # name -> properties
        self._relationships: list[tuple[str, str, str]] = []  # (from, type, to)

    def execute(
        self,
        query: str,
        params: dict | None = None,
    ) -> GraphResult:
        """Execute a mock query.

        Supports basic MERGE, CREATE, MATCH, and DELETE operations.
        """
        params = params or {}
        query_upper = query.upper()

        try:
            # Handle CREATE/MERGE for nodes
            if "MERGE" in query_upper or "CREATE" in query_upper:
                if ":Entity" in query or ":Person" in query:
                    name = params.get("name") or params.get("subject") or params.get("object")
                    if name:
                        self._nodes[name] = {"name": name}

                # Handle relationship creation
                if "->(" in query or ")->" in query:
                    subject = params.get("subject")
                    predicate = params.get("predicate", "RELATED_TO")
                    obj = params.get("object")
                    if subject and obj:
                        # Ensure nodes exist
                        self._nodes[subject] = {"name": subject}
                        self._nodes[obj] = {"name": obj}
                        # Add relationship
                        self._relationships.append((subject, predicate, obj))

                return GraphResult(success=True, records=[])

            # Handle MATCH queries
            if "MATCH" in query_upper and "RETURN" in query_upper:
                records = []

                # Query for specific relationship
                if params.get("predicate") and params.get("object"):
                    predicate = params["predicate"]
                    obj = params["object"]
                    for s, p, o in self._relationships:
                        if p == predicate and o == obj:
                            records.append({"result": s})

                # Query for specific subject
                elif params.get("subject") and params.get("predicate"):
                    subject = params["subject"]
                    predicate = params["predicate"]
                    for s, p, o in self._relationships:
                        if s == subject and p == predicate:
                            records.append({"result": o})

                # Query all relationships for a node
                elif params.get("name"):
                    name = params["name"]
                    for s, p, o in self._relationships:
                        if s == name:
                            records.append({"relation": p, "target": o})
                        elif o == name:
                            records.append({"relation": p, "target": s})

                return GraphResult(success=True, records=records)

            # Handle DELETE
            if "DELETE" in query_upper:
                name = params.get("name")
                if name:
                    self._nodes.pop(name, None)
                    self._relationships = [
                        (s, p, o) for s, p, o in self._relationships
                        if s != name and o != name
                    ]
                return GraphResult(success=True, records=[])

            return GraphResult(success=True, records=[])

        except Exception as e:
            return GraphResult(success=False, records=[], error=str(e))

    def close(self) -> None:
        """Close the mock connection."""
        pass


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
        self._database = database or None

    def execute(
        self,
        query: str,
        params: dict | None = None,
    ) -> GraphResult:
        """Execute a Cypher query.

        Args:
            query: Cypher query string.
            params: Query parameters.

        Returns:
            GraphResult with success status and records.
        """
        try:
            with self._driver.session(database=self._database) as session:
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
        driver: Driver type (memgraph, neo4j, mock).
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
    if driver == "mock":
        return MockGraphClient()

    if driver in ("memgraph", "neo4j"):
        try:
            return Neo4jClient(uri, username, password, database)
        except ImportError:
            raise ImportError(
                f"neo4j library required for {driver} driver. "
                "Install with: pip install neo4j"
            )

    raise ValueError(f"Unsupported graph driver: {driver}")
