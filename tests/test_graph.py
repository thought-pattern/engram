"""Tests for Knowledge Graph integration."""

import pytest
from engram.graph import GraphResult, MockGraphClient
from engram.template import TemplateContext, TemplateProcessor


class TestGraphResult:
    """Tests for GraphResult."""

    def test_success_with_records(self):
        result = GraphResult(success=True, records=[{"name": "Alice"}])
        assert result.success
        assert not result.is_empty
        assert result.single == {"name": "Alice"}

    def test_success_empty(self):
        result = GraphResult(success=True, records=[])
        assert result.success
        assert result.is_empty
        assert result.single is None

    def test_failure(self):
        result = GraphResult(success=False, records=[], error="Connection failed")
        assert not result.success
        assert result.error == "Connection failed"


class TestMockGraphClient:
    """Tests for MockGraphClient."""

    def test_create_node(self):
        client = MockGraphClient()
        result = client.execute(
            "CREATE (p:Person {name: $name})",
            {"name": "Alice"}
        )
        assert result.success

    def test_create_relationship(self):
        client = MockGraphClient()
        result = client.execute(
            "MERGE (a:Entity {name: $subject}) MERGE (b:Entity {name: $object}) CREATE (a)-[:$predicate]->(b)",
            {"subject": "Alice", "predicate": "KNOWS", "object": "Bob"}
        )
        assert result.success
        assert len(client._relationships) == 1

    def test_query_relationship(self):
        client = MockGraphClient()
        # Add relationship
        client.execute(
            "MERGE (a:Entity {name: $subject}) MERGE (b:Entity {name: $object}) CREATE (a)-[:$predicate]->(b)",
            {"subject": "Paris", "predicate": "CAPITAL_OF", "object": "France"}
        )

        # Query
        result = client.execute(
            "MATCH (a:Entity)-[:$predicate]->(b:Entity {name: $object}) RETURN a.name as result",
            {"predicate": "CAPITAL_OF", "object": "France"}
        )
        assert result.success
        assert len(result.records) == 1
        assert result.records[0]["result"] == "Paris"

    def test_query_not_found(self):
        client = MockGraphClient()
        result = client.execute(
            "MATCH (a:Entity)-[:$predicate]->(b:Entity {name: $object}) RETURN a.name as result",
            {"predicate": "CAPITAL_OF", "object": "Unknown"}
        )
        assert result.success
        assert result.is_empty

    def test_delete_node(self):
        client = MockGraphClient()
        # Add node
        client.execute("CREATE (p:Person {name: $name})", {"name": "Alice"})
        assert "Alice" in client._nodes

        # Delete
        result = client.execute(
            "MATCH (a:Entity {name: $name}) DETACH DELETE a",
            {"name": "Alice"}
        )
        assert result.success
        assert "Alice" not in client._nodes


class TestTemplateGraphOperations:
    """Tests for graph operations in templates."""

    def make_graph_fn(self, client: MockGraphClient):
        """Create a graph function that wraps the client."""
        def graph_fn(query: str, params: dict):
            return client.execute(query, params)
        return graph_fn

    def test_graph_query_success(self):
        client = MockGraphClient()
        # Add data
        client.execute(
            "MERGE (a:Entity {name: $subject}) MERGE (b:Entity {name: $object}) CREATE (a)-[:$predicate]->(b)",
            {"subject": "Paris", "predicate": "CAPITAL_OF", "object": "France"}
        )

        processor = TemplateProcessor()
        ctx = TemplateContext(
            stars=["capital", "France"],
            graph_fn=self.make_graph_fn(client)
        )

        template = {
            "graph_query": {
                "query": "MATCH (a:Entity)-[:$predicate]->(b:Entity {name: $object}) RETURN a.name as result",
                "params": {"predicate": "CAPITAL_OF", "object": "{star2}"},
                "on_success": {"text": "{result} is the capital of {star2}."},
                "on_failure": {"text": "I don't know."}
            }
        }

        result = processor.process(template, ctx)
        assert "Paris" in result
        assert "France" in result

    def test_graph_query_not_found(self):
        client = MockGraphClient()
        processor = TemplateProcessor()
        ctx = TemplateContext(
            stars=["capital", "Unknown"],
            graph_fn=self.make_graph_fn(client)
        )

        template = {
            "graph_query": {
                "query": "MATCH (a:Entity)-[:$predicate]->(b:Entity {name: $object}) RETURN a.name as result",
                "params": {"predicate": "CAPITAL_OF", "object": "{star2}"},
                "on_success": {"text": "{result} is the capital."},
                "on_failure": {"text": "I don't know."}
            }
        }

        result = processor.process(template, ctx)
        assert result == "I don't know."

    def test_graph_query_no_client(self):
        """Test graceful handling when no graph client is configured."""
        processor = TemplateProcessor()
        ctx = TemplateContext(stars=["test"])

        template = {
            "graph_query": {
                "query": "MATCH (n) RETURN n",
                "on_failure": {"text": "Graph not available."}
            }
        }

        result = processor.process(template, ctx)
        assert result == "Graph not available."

    def test_graph_write_success(self):
        client = MockGraphClient()
        processor = TemplateProcessor()
        ctx = TemplateContext(
            stars=["Alice"],
            graph_fn=self.make_graph_fn(client)
        )

        template = {
            "graph_write": {
                "query": "CREATE (p:Person {name: $name})",
                "params": {"name": "{star1}"},
                "on_success": {"text": "I'll remember {star1}."},
                "on_failure": {"text": "Could not save."}
            }
        }

        result = processor.process(template, ctx)
        assert "remember" in result
        assert "Alice" in result

    def test_graph_delete(self):
        client = MockGraphClient()
        # Add node first
        client.execute("CREATE (p:Person {name: $name})", {"name": "Alice"})

        processor = TemplateProcessor()
        ctx = TemplateContext(
            stars=["Alice"],
            graph_fn=self.make_graph_fn(client)
        )

        template = {
            "graph_delete": {
                "query": "MATCH (a:Entity {name: $name}) DETACH DELETE a",
                "params": {"name": "{star1}"},
                "on_success": {"text": "Forgotten."},
                "on_failure": {"text": "Could not delete."}
            }
        }

        result = processor.process(template, ctx)
        assert result == "Forgotten."

    def test_triple_add(self):
        client = MockGraphClient()
        processor = TemplateProcessor()
        ctx = TemplateContext(
            stars=["Alice", "friend", "Bob"],
            graph_fn=self.make_graph_fn(client)
        )

        template = {
            "triple_add": {
                "subject": "{star1}",
                "predicate": "{star2}",
                "object": "{star3}"
            }
        }

        processor.process(template, ctx)
        # Verify relationship was added
        assert len(client._relationships) == 1
        s, p, o = client._relationships[0]
        assert s == "Alice"
        assert p == "FRIEND"
        assert o == "Bob"

    def test_triple_query_object(self):
        client = MockGraphClient()
        # Add relationship
        client.execute(
            "MERGE (a:Entity {name: $subject}) MERGE (b:Entity {name: $object}) CREATE (a)-[:$predicate]->(b)",
            {"subject": "Paris", "predicate": "CAPITAL_OF", "object": "France"}
        )

        processor = TemplateProcessor()
        ctx = TemplateContext(
            stars=["Paris"],
            graph_fn=self.make_graph_fn(client)
        )

        template = {
            "triple_query": {
                "subject": "{star1}",
                "predicate": "CAPITAL_OF",
                "object": "?"
            }
        }

        result = processor.process(template, ctx)
        assert result == "France"

    def test_triple_query_subject(self):
        client = MockGraphClient()
        # Add relationship
        client.execute(
            "MERGE (a:Entity {name: $subject}) MERGE (b:Entity {name: $object}) CREATE (a)-[:$predicate]->(b)",
            {"subject": "Paris", "predicate": "CAPITAL_OF", "object": "France"}
        )

        processor = TemplateProcessor()
        ctx = TemplateContext(graph_fn=self.make_graph_fn(client))

        template = {
            "triple_query": {
                "subject": "?",
                "predicate": "CAPITAL_OF",
                "object": "France"
            }
        }

        result = processor.process(template, ctx)
        assert result == "Paris"

    def test_graph_query_list_format(self):
        client = MockGraphClient()
        # Add multiple relationships
        client.execute(
            "MERGE (a:Entity {name: $subject}) MERGE (b:Entity {name: $object}) CREATE (a)-[:$predicate]->(b)",
            {"subject": "Alice", "predicate": "KNOWS", "object": "Bob"}
        )
        client.execute(
            "MERGE (a:Entity {name: $subject}) MERGE (b:Entity {name: $object}) CREATE (a)-[:$predicate]->(b)",
            {"subject": "Alice", "predicate": "LIKES", "object": "Pizza"}
        )

        processor = TemplateProcessor()
        ctx = TemplateContext(
            stars=["Alice"],
            graph_fn=self.make_graph_fn(client)
        )

        template = {
            "graph_query": {
                "query": "MATCH (a:Entity {name: $name})-[r]->(b) RETURN type(r) as relation, b.name as target",
                "params": {"name": "{star1}"},
                "format": "list",
                "item_template": "{star1} {relation} {target}",
                "join": ". ",
                "on_success": {"text": "Info: {result}"},
                "on_empty": {"text": "Nothing known."}
            }
        }

        result = processor.process(template, ctx)
        assert "KNOWS" in result or "LIKES" in result
