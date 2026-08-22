"""Tests for Knowledge Graph integration.

The runtime graph layer is read-only: queries return a list of row dicts, reads
degrade to an empty list, and every public execution path rejects mutations.
MockGraphClient is an in-memory stand-in keyed on query parameters.
"""

from unittest.mock import Mock, patch

import pytest

from engram.config import engram_config, graph_config
from engram.constants import Tier
from engram.core import Engram
from engram.graph import MemGraphConnection, graph_is_empty, graph_single, is_write_cypher
from engram.service import EngramCore
from engram.template import TemplateProcessor, template_context


def same(a, b) -> bool:
    """Case-insensitive surface comparison, matching the canonical queries."""
    result = bool(a) and bool(b) and str(a).lower() == str(b).lower()
    return result


class MockGraphClient:
    """In-memory canonical graph stand-in.

    Stores claims as (subject, predicate, object) triples and resolves the
    canonical triple/entity queries by their parameters. Returns list[dict] and
    never raises (the connection's raise-on-failure path is exercised by the
    live smoke test).
    """

    def __init__(self) -> None:
        """Initialize the mock store."""
        self.available = True
        self.claims: list[dict] = []  # {"subject","predicate","object"}
        self.entities: set[str] = set()
        self.vector_rows: list[dict] = []

    def execute(self, query: str, params=()) -> list:
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
            result = []
            return result

        if "MATCH" in query_upper and "RETURN" in query_upper:
            # Triple query, object unknown: subject + predicate -> object.
            if subject and predicate and not obj:
                for claim in self.claims:
                    if same(claim["subject"], subject) and same(claim["predicate"], predicate):
                        result = [{"result": claim["object"]}]
                        return result
                result = []
                return result
            # Triple query / graph_query, subject unknown: predicate + object -> subject.
            if obj and predicate and not subject:
                result = [
                    {"result": claim["subject"]}
                    for claim in self.claims
                    if same(claim["predicate"], predicate) and same(claim["object"], obj)
                ]
                return result
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
                result = [dict(claim) for claim in self.claims if keyword.lower() in claim["subject"].lower()]
                return result
            result = []
            return result

        if "DELETE" in query_upper:
            if name:
                self.claims = [claim for claim in self.claims if not (same(claim["subject"], name) or same(claim["object"], name))]
                self.entities.discard(name)
            result = []
            return result

        result = []
        return result

    def execute_read(self, query: str, params=()) -> list:
        """Read alias, matching the real connection's execute_read."""
        result = self.execute(query, params)
        return result

    def vector_search_claims(self, embedding, **kwargs) -> list:
        """Return configured ANN rows for vector-recall tests."""
        result = list(self.vector_rows)
        return result

    def close(self) -> None:
        """Close the mock connection."""
        pass


def fake_embedding_model() -> Mock:
    """Build a tracked embedding test double with the configured dimension."""
    model = Mock()

    def encode(texts: list[str], **kwargs) -> list[Mock]:
        del kwargs
        vectors = []
        for _text in texts:
            vector = Mock()
            vector.tolist.return_value = [0.0] * 384
            vectors.append(vector)
        result = vectors
        return result

    model.encode.side_effect = encode
    result = model
    return result


"""Tests for the list-row helpers."""


def test_graph_helpers_non_empty():
    records = [{"name": "Alice"}]
    assert not graph_is_empty(records)
    assert graph_single(records) == {"name": "Alice"}


def test_graph_helpers_empty():
    records = []
    assert graph_is_empty(records)
    assert graph_single(records) == {}


"""Tests for graph operations in templates."""


def _template_graph_operations_make_graph_fn(client: MockGraphClient):
    """Create a graph function that wraps the client."""

    def graph_fn(query: str, params: dict):
        result = client.execute(query, params)
        return result

    return graph_fn


def test_template_graph_operations_graph_query_success():
    client = MockGraphClient()
    client.execute(
        "MERGE (s:Entity {primary_label: $subject}) CREATE (c:Claim)",
        {"subject": "Paris", "predicate": "CAPITAL_OF", "object": "France"},
    )

    processor = TemplateProcessor()
    ctx = template_context(stars=["capital", "France"], graph_fn=_template_graph_operations_make_graph_fn(client))

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


def test_template_graph_operations_graph_query_not_found():
    client = MockGraphClient()
    processor = TemplateProcessor()
    ctx = template_context(stars=["capital", "Unknown"], graph_fn=_template_graph_operations_make_graph_fn(client))

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


def test_template_graph_operations_graph_query_no_client():
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


def test_template_graph_operations_authoring_template_operations_are_not_supported():
    calls = []

    def graph_fn(query, params):
        calls.append((query, params))
        result = []
        return result

    processor = TemplateProcessor()
    ctx = template_context(graph_fn=graph_fn)

    for operation in ("graph_write", "graph_delete", "triple_add"):
        assert processor.process({operation: {"query": "CREATE (n)"}}, ctx) == ""

    assert calls == []


def test_template_graph_operations_graph_query_rejects_mutation_before_callback():
    calls = []

    def graph_fn(query, params):
        calls.append((query, params))
        result = []
        return result

    processor = TemplateProcessor()
    ctx = template_context(graph_fn=graph_fn)
    template = {
        "graph_query": {
            "query": "MATCH (n) DETACH DELETE n",
            "on_failure": {"text": "Read-only."},
        }
    }

    assert processor.process(template, ctx) == "Read-only."
    assert calls == []


def test_template_graph_operations_triple_query_object():
    client = MockGraphClient()
    client.execute(
        "MERGE (s:Entity {primary_label: $subject}) CREATE (c:Claim)",
        {"subject": "Paris", "predicate": "CAPITAL_OF", "object": "France"},
    )

    processor = TemplateProcessor()
    ctx = template_context(stars=["Paris"], graph_fn=_template_graph_operations_make_graph_fn(client))

    template = {"triple_query": {"subject": "{star1}", "predicate": "CAPITAL_OF", "object": "?"}}

    result = processor.process(template, ctx)
    assert result == "France"


def test_template_graph_operations_triple_query_subject():
    client = MockGraphClient()
    client.execute(
        "MERGE (s:Entity {primary_label: $subject}) CREATE (c:Claim)",
        {"subject": "Paris", "predicate": "CAPITAL_OF", "object": "France"},
    )

    processor = TemplateProcessor()
    ctx = template_context(graph_fn=_template_graph_operations_make_graph_fn(client))

    template = {"triple_query": {"subject": "?", "predicate": "CAPITAL_OF", "object": "France"}}

    result = processor.process(template, ctx)
    assert result == "Paris"


def test_template_graph_operations_graph_query_list_format():
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
    ctx = template_context(stars=["Alice"], graph_fn=_template_graph_operations_make_graph_fn(client))

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


"""ENGRAM exposes graph recall only through every runtime path."""


def _read_only_graph_wiring_engram_with_graph(client):
    """An ENGRAM with the graph enabled and a mock client injected."""
    with patch("engram.core.create_graph_client", return_value=client) as create_client:
        engram = Engram(config=engram_config(graph=graph_config(enabled=True)))
    create_client.assert_called_once()
    return engram


def test_read_only_graph_wiring_is_write_cypher_detects_mutations():
    assert is_write_cypher("MATCH (c:Claim) CREATE (x:Claim) RETURN x")
    assert is_write_cypher("MERGE (n:Entity {primary_label: 'X'})")
    assert is_write_cypher("MATCH (n) DETACH DELETE n")
    assert is_write_cypher("MATCH (c) SET c.x = 1")
    assert not is_write_cypher("MATCH (c:Claim)-[:HAS_SUBJECT]->(e:Entity) RETURN c LIMIT 1")
    assert not is_write_cypher("")


def test_read_only_graph_wiring_is_write_cypher_refuses_procedures_and_imports():
    # Stored procedures can mutate regardless of the query's shape, and
    # LOAD CSV imports data -- both are refused on the recall-only path.
    assert is_write_cypher("CALL mg.load_all()")
    assert is_write_cypher("call db.labels() YIELD label RETURN label")
    assert is_write_cypher("LOAD CSV FROM 'file:///x.csv' AS row RETURN row")
    # 'called'/'loading' as plain words in string literals do not trip the
    # whole-word guard.
    assert not is_write_cypher("MATCH (c:Claim) WHERE c.subject = 'so-called expert' RETURN c")


def test_read_only_graph_wiring_graph_read_fn_passes_reads():
    client = MockGraphClient()
    client.claims.append({"subject": "Athens", "predicate": "located in", "object": "Greece"})
    engram = _read_only_graph_wiring_engram_with_graph(client)
    rows = engram.graph_read_fn(
        "MATCH (c:Claim) RETURN c.object AS result",
        {"subject": "Athens", "predicate": "located in"},
    )
    assert rows == [{"result": "Greece"}]


def test_read_only_graph_wiring_unavailable_enabled_graph_is_optional():
    client = MockGraphClient()
    client.available = False
    with patch("engram.core.create_graph_client", return_value=client):
        engram = Engram(config=engram_config(graph=graph_config(enabled=True)))

    assert engram.component_status["graph"] == {"enabled": True, "ready": False}
    assert engram.graph_query("RETURN 1") == []
    assert engram.query("ordinary local request")["matches"] == []


def test_read_only_graph_wiring_transport_neutral_status_reports_enabled_component_readiness():
    client = MockGraphClient()
    with patch("engram.core.create_graph_client", return_value=client):
        engram = Engram(config=engram_config(graph=graph_config(enabled=True)))

    core = EngramCore(engram)

    assert core.status()["components"] == {
        "nltk": {"enabled": True, "ready": True},
        "graph": {"enabled": True, "ready": True},
        "vector": {"enabled": False, "ready": False},
        "sparse": {"enabled": False, "ready": False},
        "spacy": {"enabled": True, "ready": True},
    }


def test_read_only_graph_wiring_graph_read_fn_refuses_writes():
    client = MockGraphClient()
    engram = _read_only_graph_wiring_engram_with_graph(client)
    before = len(client.claims)
    with pytest.raises(ValueError, match="read-only"):
        engram.graph_read_fn(
            "MERGE (s:Entity {primary_label: $subject}) CREATE (c:Claim)",
            {"subject": "Paris", "predicate": "located in", "object": "France"},
        )
    assert len(client.claims) == before  # the write never reached the graph


def test_read_only_graph_wiring_connection_has_no_writer_and_rejects_before_connecting():
    client = MemGraphConnection()

    with pytest.raises(ValueError, match="read-only"):
        client.execute("CREATE (n)")
    assert client.conn == ()


def test_read_only_graph_wiring_internal_vector_search_is_fixed_and_generic_call_stays_refused():
    client = MemGraphConnection()
    captured = {}

    def execute(query: str, parameters=()) -> list[dict]:
        captured.update({"query": query, "params": parameters})
        result = [{"claim_id": "claim-1", "similarity": 0.8}]
        return result

    client._execute_read_query = execute

    rows = client.vector_search_claims(
        [0.0, 1.0],
        index_name="claim_premise_embeddings",
        limit=25,
        min_similarity=0.5,
    )

    assert rows[0]["claim_id"] == "claim-1"
    assert "CALL vector_search.search" in captured["query"]
    assert captured["params"]["limit"] == 25
    with pytest.raises(ValueError, match="read-only"):
        client.execute("CALL vector_search.search('x', 1, [1.0]) YIELD node RETURN node")


def test_read_only_graph_wiring_vector_graph_fallback_phrases_semantic_claim():
    client = MockGraphClient()
    client.vector_rows = [
        {
            "claim_id": "claim-1",
            "subject": "Water",
            "predicate": "boils at",
            "object": "100 degrees Celsius",
            "similarity": 0.81,
        }
    ]
    config = engram_config(graph=graph_config(enabled=True, vector_enabled=True))
    with (
        patch("engram.core.create_graph_client", return_value=client),
        patch("engram.core.SentenceTransformer", return_value=fake_embedding_model()) as load_model,
    ):
        engram = Engram(config=config)
    load_model.assert_called_once()
    engram._encode_graph_query = lambda text: [0.0] * 384

    result = engram.graph_lookup("Explain the phase transition temperature")

    assert "Water" in result
    assert "100 degrees Celsius" in result


def test_read_only_graph_wiring_vector_support_retrieves_scoped_response_on_keyword_miss():
    client = MockGraphClient()
    client.vector_rows = [
        {
            "claim_id": "support-1",
            "subject": "Western Roman Empire",
            "predicate": "declined because",
            "object": "overlapping pressures",
            "similarity": 0.84,
        }
    ]
    config = engram_config(
        graph=graph_config(
            enabled=True,
            vector_enabled=True,
            vector_weight=0.75,
        )
    )
    with (
        patch("engram.core.create_graph_client", return_value=client),
        patch("engram.core.SentenceTransformer", return_value=fake_embedding_model()) as load_model,
    ):
        engram = Engram(config=config)
    load_model.assert_called_once()
    engram._encode_graph_query = lambda text: [0.0] * 384
    core = EngramCore(engram)
    core.learn_response(
        "Why did Rome fall?",
        "Rome declined through overlapping political and military pressures.",
        request_id="learn-1",
        namespace="tapestry",
        context_fingerprint="local-v1",
        metadata={"support": [{"claim_id": "support-1", "trust": 1.0}]},
    )

    proposal = core.propose(
        "zygomatic quasar lattice",
        request_id="proposal-1",
        namespace="tapestry",
        context_fingerprint="local-v1",
    )

    assert len(proposal["candidates"]) == 1
    candidate = proposal["candidates"][0]
    assert candidate["retrieval"]["selected"] == "vector"
    assert candidate["retrieval"]["keyword_score"] == 0.0
    assert candidate["retrieval"]["vector_score"] == pytest.approx(0.63)


def test_read_only_graph_wiring_triple_query_wired_through_response_path():
    """A stored `<triple_query>` statement resolves through pattern_query."""
    client = MockGraphClient()
    client.claims.append({"subject": "Athens", "predicate": "located in", "object": "Greece"})
    engram = _read_only_graph_wiring_engram_with_graph(client)
    engram.store(
        text="",
        pattern="WHERE IS ATHENS",
        template={"triple_query": {"subject": "Athens", "predicate": "located in", "object": "?"}},
        tier=Tier.STATIC,
    )
    result = engram.pattern_query("where is athens")
    assert result and result[2] == "Greece"


def test_read_only_graph_wiring_removed_authoring_template_is_inert_through_response_path():
    client = MockGraphClient()
    engram = _read_only_graph_wiring_engram_with_graph(client)
    engram.store(
        text="",
        pattern="ADD PARIS",
        template={"triple_add": {"subject": "Paris", "predicate": "located in", "object": "France"}},
        tier=Tier.STATIC,
    )
    before = len(client.claims)
    engram.pattern_query("add paris")
    assert len(client.claims) == before
