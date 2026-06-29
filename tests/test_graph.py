"""Tests for Knowledge Graph integration.

The graph layer mirrors the Tapestry connection interface: queries return a list
of row dicts (not a status wrapper), reads degrade to an empty list, and writes
raise on failure. Triples are stored as canonical Claim nodes linked by
HAS_SUBJECT / USES_PREDICATE / HAS_OBJECT edges. MockGraphClient is an in-memory
stand-in keyed on query parameters (the real Cypher is exercised by the live
smoke test, not by the mock).
"""

from engram.graph import graph_is_empty, graph_single
from engram.template import TemplateProcessor, template_context


def same(a, b) -> bool:
    """Case-insensitive surface comparison, matching the canonical queries."""
    return bool(a) and bool(b) and str(a).lower() == str(b).lower()


class MockGraphClient:
    """In-memory canonical graph stand-in.

    Stores claims as (subject, predicate, object) triples and resolves the
    canonical triple/entity queries by their parameters. Returns list[dict] and
    never raises (the connection's raise-on-failure path is exercised by the
    live smoke test).
    """

    def __init__(self) -> None:
        """Initialize the mock store."""
        self.claims: list[dict] = []  # {"subject","predicate","object"}
        self.entities: set[str] = set()

    def execute(self, query: str, params=None) -> list:
        """Execute a mock query, returning a list of row dicts."""
        params = params or {}
        query_upper = query.upper()

        subject = params.get("subject")
        predicate = params.get("predicate")
        obj = params.get("object")
        name = params.get("name")
        keyword = params.get("keyword")

        if "CREATE" in query_upper or "MERGE" in query_upper:
            # Triple write (canonical Claim or a generic relationship create).
            if subject and predicate and obj:
                self.claims.append({"subject": subject, "predicate": predicate, "object": obj})
                self.entities.update({subject, obj})
            elif name:
                self.entities.add(name)
            return []

        if "MATCH" in query_upper and "RETURN" in query_upper:
            # Triple query, object unknown: subject + predicate -> object.
            if subject and predicate and not obj:
                for claim in self.claims:
                    if same(claim["subject"], subject) and same(claim["predicate"], predicate):
                        return [{"result": claim["object"]}]
                return []
            # Triple query / graph_query, subject unknown: predicate + object -> subject.
            if obj and predicate and not subject:
                return [{"result": claim["subject"]} for claim in self.claims if same(claim["predicate"], predicate) and same(claim["object"], obj)]
            # Facts by entity name (list-format graph_query and entity recall).
            if name:
                rows = []
                for claim in self.claims:
                    if same(claim["subject"], name):
                        rows.append({"relation": claim["predicate"], "target": claim["object"], **claim})
                    elif same(claim["object"], name):
                        rows.append({"relation": claim["predicate"], "target": claim["subject"], **claim})
                return rows
            # Keyword fallback: entity whose label contains the keyword.
            if keyword:
                return [dict(claim) for claim in self.claims if keyword.lower() in claim["subject"].lower()]
            return []

        if "DELETE" in query_upper:
            if name:
                self.claims = [claim for claim in self.claims if not (same(claim["subject"], name) or same(claim["object"], name))]
                self.entities.discard(name)
            return []

        return []

    def close(self) -> None:
        """Close the mock connection."""
        pass


class TestGraphHelpers:
    """Tests for the list-row helpers."""

    def test_non_empty(self):
        records = [{"name": "Alice"}]
        assert not graph_is_empty(records)
        assert graph_single(records) == {"name": "Alice"}

    def test_empty(self):
        records = []
        assert graph_is_empty(records)
        assert graph_single(records) == {}


class TestMockGraphClient:
    """Tests for MockGraphClient."""

    def test_create_node(self):
        client = MockGraphClient()
        result = client.execute("CREATE (p:Entity {primary_label: $name})", {"name": "Alice"})
        assert result == []
        assert "Alice" in client.entities

    def test_create_relationship(self):
        client = MockGraphClient()
        client.execute(
            "MERGE (s:Entity {primary_label: $subject}) CREATE (c:Claim)-[:HAS_SUBJECT]->(s)",
            {"subject": "Alice", "predicate": "KNOWS", "object": "Bob"},
        )
        assert len(client.claims) == 1

    def test_query_relationship(self):
        client = MockGraphClient()
        client.execute(
            "MERGE (s:Entity {primary_label: $subject}) CREATE (c:Claim)",
            {"subject": "Paris", "predicate": "CAPITAL_OF", "object": "France"},
        )
        result = client.execute(
            "MATCH (c:Claim)-[:USES_PREDICATE]->(p:Predicate) RETURN hs.surface_form as result",
            {"predicate": "CAPITAL_OF", "object": "France"},
        )
        assert len(result) == 1
        assert result[0]["result"] == "Paris"

    def test_query_not_found(self):
        client = MockGraphClient()
        result = client.execute(
            "MATCH (c:Claim) RETURN hs.surface_form as result",
            {"predicate": "CAPITAL_OF", "object": "Unknown"},
        )
        assert graph_is_empty(result)

    def test_delete_node(self):
        client = MockGraphClient()
        client.execute("CREATE (e:Entity {primary_label: $name})", {"name": "Alice"})
        assert "Alice" in client.entities

        result = client.execute("MATCH (e:Entity {primary_label: $name}) DETACH DELETE e", {"name": "Alice"})
        assert result == []
        assert "Alice" not in client.entities


class TestTemplateGraphOperations:
    """Tests for graph operations in templates."""

    def make_graph_fn(self, client: MockGraphClient):
        """Create a graph function that wraps the client."""

        def graph_fn(query: str, params: dict):
            return client.execute(query, params)

        return graph_fn

    def test_graph_query_success(self):
        client = MockGraphClient()
        client.execute(
            "MERGE (s:Entity {primary_label: $subject}) CREATE (c:Claim)",
            {"subject": "Paris", "predicate": "CAPITAL_OF", "object": "France"},
        )

        processor = TemplateProcessor()
        ctx = template_context(stars=["capital", "France"], graph_fn=self.make_graph_fn(client))

        template = {
            "graph_query": {
                "query": "MATCH (c:Claim) RETURN hs.surface_form as result",
                "params": {"predicate": "CAPITAL_OF", "object": "{star2}"},
                "on_success": {"text": "{result} is the capital of {star2}."},
                "on_failure": {"text": "I don't know."},
            }
        }

        result = processor.process(template, ctx)
        assert "Paris" in result
        assert "France" in result

    def test_graph_query_not_found(self):
        client = MockGraphClient()
        processor = TemplateProcessor()
        ctx = template_context(stars=["capital", "Unknown"], graph_fn=self.make_graph_fn(client))

        template = {
            "graph_query": {
                "query": "MATCH (c:Claim) RETURN hs.surface_form as result",
                "params": {"predicate": "CAPITAL_OF", "object": "{star2}"},
                "on_success": {"text": "{result} is the capital."},
                "on_failure": {"text": "I don't know."},
            }
        }

        result = processor.process(template, ctx)
        assert result == "I don't know."

    def test_graph_query_no_client(self):
        """Test graceful handling when no graph client is configured."""
        processor = TemplateProcessor()
        ctx = template_context(stars=["test"])

        template = {
            "graph_query": {
                "query": "MATCH (n) RETURN n",
                "on_failure": {"text": "Graph not available."},
            }
        }

        result = processor.process(template, ctx)
        assert result == "Graph not available."

    def test_graph_write_success(self):
        client = MockGraphClient()
        processor = TemplateProcessor()
        ctx = template_context(stars=["Alice"], graph_fn=self.make_graph_fn(client))

        template = {
            "graph_write": {
                "query": "CREATE (e:Entity {primary_label: $name})",
                "params": {"name": "{star1}"},
                "on_success": {"text": "I'll remember {star1}."},
                "on_failure": {"text": "Could not save."},
            }
        }

        result = processor.process(template, ctx)
        assert "remember" in result
        assert "Alice" in result

    def test_graph_write_failure(self):
        """A write that raises is reported via on_failure, not on_success."""

        def failing_graph_fn(query, params):
            raise RuntimeError("Write query failed: MemGraph unreachable")

        processor = TemplateProcessor()
        ctx = template_context(stars=["Alice"], graph_fn=failing_graph_fn)

        template = {
            "graph_write": {
                "query": "CREATE (e:Entity {primary_label: $name})",
                "params": {"name": "{star1}"},
                "on_success": {"text": "I'll remember {star1}."},
                "on_failure": {"text": "Could not save."},
            }
        }

        result = processor.process(template, ctx)
        assert result == "Could not save."

    def test_graph_delete(self):
        client = MockGraphClient()
        client.execute("CREATE (e:Entity {primary_label: $name})", {"name": "Alice"})

        processor = TemplateProcessor()
        ctx = template_context(stars=["Alice"], graph_fn=self.make_graph_fn(client))

        template = {
            "graph_delete": {
                "query": "MATCH (e:Entity {primary_label: $name}) DETACH DELETE e",
                "params": {"name": "{star1}"},
                "on_success": {"text": "Forgotten."},
                "on_failure": {"text": "Could not delete."},
            }
        }

        result = processor.process(template, ctx)
        assert result == "Forgotten."

    def test_triple_add(self):
        client = MockGraphClient()
        processor = TemplateProcessor()
        ctx = template_context(stars=["Alice", "friend", "Bob"], graph_fn=self.make_graph_fn(client))

        template = {"triple_add": {"subject": "{star1}", "predicate": "{star2}", "object": "{star3}"}}

        processor.process(template, ctx)
        # The triple is stored as a canonical claim with its surface projection.
        assert len(client.claims) == 1
        claim = client.claims[0]
        assert claim["subject"] == "Alice"
        assert claim["predicate"] == "friend"
        assert claim["object"] == "Bob"

    def test_triple_query_object(self):
        client = MockGraphClient()
        client.execute(
            "MERGE (s:Entity {primary_label: $subject}) CREATE (c:Claim)",
            {"subject": "Paris", "predicate": "CAPITAL_OF", "object": "France"},
        )

        processor = TemplateProcessor()
        ctx = template_context(stars=["Paris"], graph_fn=self.make_graph_fn(client))

        template = {"triple_query": {"subject": "{star1}", "predicate": "CAPITAL_OF", "object": "?"}}

        result = processor.process(template, ctx)
        assert result == "France"

    def test_triple_query_subject(self):
        client = MockGraphClient()
        client.execute(
            "MERGE (s:Entity {primary_label: $subject}) CREATE (c:Claim)",
            {"subject": "Paris", "predicate": "CAPITAL_OF", "object": "France"},
        )

        processor = TemplateProcessor()
        ctx = template_context(graph_fn=self.make_graph_fn(client))

        template = {"triple_query": {"subject": "?", "predicate": "CAPITAL_OF", "object": "France"}}

        result = processor.process(template, ctx)
        assert result == "Paris"

    def test_graph_query_list_format(self):
        client = MockGraphClient()
        client.execute(
            "MERGE (s:Entity {primary_label: $subject}) CREATE (c:Claim)",
            {"subject": "Alice", "predicate": "KNOWS", "object": "Bob"},
        )
        client.execute(
            "MERGE (s:Entity {primary_label: $subject}) CREATE (c:Claim)",
            {"subject": "Alice", "predicate": "LIKES", "object": "Pizza"},
        )

        processor = TemplateProcessor()
        ctx = template_context(stars=["Alice"], graph_fn=self.make_graph_fn(client))

        template = {
            "graph_query": {
                "query": "MATCH (c:Claim)-[:HAS_SUBJECT]->(e:Entity {primary_label: $name}) RETURN c.predicate as relation, c.object as target",
                "params": {"name": "{star1}"},
                "format": "list",
                "item_template": "{star1} {relation} {target}",
                "join": ". ",
                "on_success": {"text": "Info: {result}"},
                "on_empty": {"text": "Nothing known."},
            }
        }

        result = processor.process(template, ctx)
        assert "KNOWS" in result or "LIKES" in result
