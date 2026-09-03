"""Strict fixed-query Proposition projection boundary tests for EGR-704."""

import json
from copy import deepcopy

import pytest

from engram.core import Engram
from engram.errors import InvalidRequestError
from engram.graph import (
    PROPOSITION_PROJECTION_FIELDS,
    MemGraphConnection,
    PropositionProjectionQuery,
    proposition_projection_from_graph_row,
    proposition_projection_to_dict,
    validate_proposition_projection,
)


def _row(*, semantic: bool = False) -> dict[str, object]:
    result = {
        "proposition_id": "proposition:01J5M6Q9J8",
        "subject_entity_id": "entity:alan-turing",
        "predicate_id": "predicate:birth-date",
        "object_entity_id": "entity:1912-06-23",
        "invalidated_at": "",
        "invalidated_at_available": False,
        "system_from": "2026-08-01T00:00:00Z",
        "system_from_available": True,
        "system_to": "",
        "system_to_available": False,
        "valid_from": "1912-06-23T00:00:00Z",
        "valid_from_available": True,
        "valid_to": "",
        "valid_to_available": False,
        "predicate_canonical": True,
        "ownership_category": "PUBLIC",
        "trust_category": "verified_public",
        "trust_category_available": True,
        "supplied_trust": 0.84,
        "supplied_trust_available": True,
        "supplied_trust_version": 3,
        "supplied_trust_version_available": True,
        "structured_match": 0.0 if semantic else 1.0,
        "structured_match_available": not semantic,
        "semantic_similarity": 0.81 if semantic else 0.0,
        "semantic_similarity_available": semantic,
    }
    return result


def test_structured_projection_uses_fixed_query_and_safe_exact_fields() -> None:
    client = MemGraphConnection()
    captured = {}

    def execute(query: str, parameters=()) -> list[dict[str, object]]:
        captured.update({"query": query, "parameters": parameters})
        result = [_row()]
        return result

    client._execute_read_query = execute
    projections = client.structured_proposition_projections(
        "Alan Turing",
        projection_id=PropositionProjectionQuery.STRUCTURED_ENTITY_V1,
        limit=3,
    )

    assert len(projections) == 1
    projection = projections[0]
    assert type(projection) is dict
    assert projection["projection_id"] == PropositionProjectionQuery.STRUCTURED_ENTITY_V1
    assert projection["vector_index_id_available"] is False
    assert projection["structured_match"] == 1.0
    assert projection["semantic_similarity_available"] is False
    assert set(_row()) == PROPOSITION_PROJECTION_FIELDS
    assert captured["parameters"] == {"value": "Alan Turing", "limit": 3}
    assert "Alan Turing" not in captured["query"]
    assert "subject.canonical_id AS subject_entity_id" in captured["query"]
    assert "predicate.canonical_id AS predicate_id" in captured["query"]
    assert "object.canonical_id AS object_entity_id" in captured["query"]
    assert "c.subject AS subject" not in captured["query"]
    assert "c.predicate AS predicate" not in captured["query"]
    assert "c.object AS object" not in captured["query"]
    assert "properties(" not in captured["query"].lower()
    assert "null" not in json.dumps(proposition_projection_to_dict(projection), sort_keys=True)


def test_projection_accepts_available_zero_trust_revision() -> None:
    """Tapestry trust revisions are non-negative and begin at zero."""
    row = _row()
    row["supplied_trust_version"] = 0

    projection = proposition_projection_from_graph_row(row, PropositionProjectionQuery.STRUCTURED_ENTITY_V1)

    assert projection["supplied_trust_version"] == 0
    assert projection["supplied_trust_version_available"] is True


def test_vector_projection_preserves_fixed_index_and_raw_similarity() -> None:
    client = MemGraphConnection()
    captured = {}

    def execute(query: str, parameters=()) -> list[dict[str, object]]:
        captured.update({"query": query, "parameters": parameters})
        result = [_row(semantic=True)]
        return result

    client._execute_read_query = execute
    projections = client.vector_search_proposition_projections(
        [0.0, 1.0],
        index_name="proposition_embeddings",
        limit=7,
        min_similarity=0.45,
    )

    projection = projections[0]
    assert projection["projection_id"] == PropositionProjectionQuery.VECTOR_V1
    assert projection["vector_index_id"] == "proposition_embeddings"
    assert projection["vector_index_id_available"] is True
    assert projection["semantic_similarity"] == pytest.approx(0.81)
    assert projection["structured_match_available"] is False
    assert "CALL vector_search.search" in captured["query"]
    assert captured["parameters"] == {
        "index_name": "proposition_embeddings",
        "limit": 7,
        "query_embedding": [0.0, 1.0],
        "min_similarity": 0.45,
    }


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda row: row.pop("proposition_id"), "invalid fields"),
        (lambda row: row.update({"raw_proposition": "secret"}), "invalid fields"),
        (lambda row: row.update({"proposition_id": "x" * 257}), "256 UTF-8 bytes"),
        (lambda row: row.update({"proposition_id": "proposition id"}), "whitespace"),
        (lambda row: row.update({"ownership_category": "INTERNAL"}), "unsupported"),
        (lambda row: row.update({"system_to": "2026-08-02T00:00:00Z"}), "empty when unavailable"),
        (lambda row: row.update({"valid_from": "2026-08-01T00:00:00+00:00"}), "ending in Z"),
        (
            lambda row: row.update(
                {
                    "valid_to": "1900-01-01T00:00:00Z",
                    "valid_to_available": True,
                }
            ),
            "valid_from must be earlier",
        ),
        (lambda row: row.update({"supplied_trust": float("nan")}), "finite"),
        (
            lambda row: row.update({"supplied_trust_version": 0, "supplied_trust_version_available": False}),
            "availability must match",
        ),
        (lambda row: row.update({"structured_match": 0.0, "structured_match_available": False}), "measurements"),
    ],
)
def test_projection_decoder_rejects_malformed_or_content_bearing_rows(mutate, message) -> None:
    row = _row()
    mutate(row)

    with pytest.raises(InvalidRequestError, match=message):
        proposition_projection_from_graph_row(row, PropositionProjectionQuery.STRUCTURED_ENTITY_V1)


def test_projection_decoder_normalizes_external_nulls_at_boundary() -> None:
    row = _row()
    for field in ("invalidated_at", "system_to", "valid_to"):
        row[field] = None
    row.update(
        {
            "trust_category": None,
            "trust_category_available": False,
            "supplied_trust": None,
            "supplied_trust_available": False,
            "supplied_trust_version": None,
            "supplied_trust_version_available": False,
        }
    )

    projection = proposition_projection_from_graph_row(row, PropositionProjectionQuery.STRUCTURED_ENTITY_V1)

    assert projection["invalidated_at"] == projection["system_to"] == projection["valid_to"] == ""
    assert projection["trust_category"] == ""
    assert projection["supplied_trust"] == 0.0
    assert projection["supplied_trust_version"] == 0
    assert "null" not in json.dumps(proposition_projection_to_dict(projection), sort_keys=True)


def test_projection_validation_revalidates_and_copies_mutable_records() -> None:
    source = proposition_projection_from_graph_row(_row(), PropositionProjectionQuery.STRUCTURED_ENTITY_V1)
    validated = validate_proposition_projection(source)

    assert type(validated) is dict
    assert validated == source
    assert validated is not source
    source["proposition_id"] = "proposition:mutated"
    assert validated["proposition_id"] == "proposition:01J5M6Q9J8"

    malformed = dict(validated)
    malformed["unexpected"] = "value"
    with pytest.raises(InvalidRequestError, match="invalid fields"):
        validate_proposition_projection(malformed)


def test_projection_boundary_deduplicates_identical_rows_and_rejects_conflicts() -> None:
    client = MemGraphConnection()
    client._execute_read_query = lambda query, parameters=(): [_row(), deepcopy(_row())]

    assert (
        len(
            client.structured_proposition_projections(
                "Turing", projection_id=PropositionProjectionQuery.STRUCTURED_KEYWORD_V1, limit=2
            )
        )
        == 1
    )

    conflict = _row()
    conflict["object_entity_id"] = "entity:conflict"
    client._execute_read_query = lambda query, parameters=(): [_row(), conflict]
    with pytest.raises(InvalidRequestError, match="conflicting Proposition projections"):
        client.structured_proposition_projections("Turing", projection_id=PropositionProjectionQuery.STRUCTURED_KEYWORD_V1, limit=2)


def test_projection_boundary_rejects_excess_rows_and_untrusted_identifiers() -> None:
    client = MemGraphConnection()
    client._execute_read_query = lambda query, parameters=(): [_row(), deepcopy(_row())]

    with pytest.raises(InvalidRequestError, match="more rows than requested"):
        client.structured_proposition_projections("Turing", projection_id=PropositionProjectionQuery.STRUCTURED_KEYWORD_V1, limit=1)
    with pytest.raises(InvalidRequestError, match="unsupported"):
        client.structured_proposition_projections("Turing", projection_id=PropositionProjectionQuery.VECTOR_V1)
    with pytest.raises(InvalidRequestError, match="vector index"):
        client.vector_search_proposition_projections([0.0], index_name="bad index")
    with pytest.raises(InvalidRequestError, match="finite numeric"):
        client.vector_search_proposition_projections([float("inf")])
    with pytest.raises(InvalidRequestError, match="65536 dimensions"):
        client.vector_search_proposition_projections([0.0] * 65_537)


def test_transport_neutral_structured_projection_boundary_is_bounded(monkeypatch) -> None:
    projection = proposition_projection_from_graph_row(_row(), PropositionProjectionQuery.STRUCTURED_ENTITY_V1)

    def structured_proposition_projections(value, *, projection_id, limit=10):
        assert value == "Alan Turing"
        assert projection_id == PropositionProjectionQuery.STRUCTURED_ENTITY_V1
        assert limit == 1
        result = [projection]
        return result

    engine = Engram()
    client = MemGraphConnection()
    monkeypatch.setattr(client, "structured_proposition_projections", structured_proposition_projections)
    engine._graph_client = client
    monkeypatch.setattr("engram.core.extract_entities", lambda _text: [{"text": "Alan Turing"}])

    result = engine.structured_proposition_projections("Who was Alan Turing?", row_limit=1)

    assert result == [projection]
    with pytest.raises(ValueError, match="0 through 1000"):
        engine.structured_proposition_projections("query", row_limit=1_001)


def test_transport_neutral_structured_projection_caps_zero_row_query_attempts(monkeypatch) -> None:
    calls = []

    def structured_proposition_projections(value, *, projection_id, limit=10):
        calls.append((value, projection_id, limit))
        result = []
        return result

    engine = Engram()
    client = MemGraphConnection()
    monkeypatch.setattr(client, "structured_proposition_projections", structured_proposition_projections)
    engine._graph_client = client
    monkeypatch.setattr(
        "engram.core.extract_entities",
        lambda _text: [{"text": f"entity-{index}"} for index in range(5)],
    )

    result = engine.structured_proposition_projections("query", row_limit=10)

    assert result == []
    assert tuple(value for value, _projection_id, _limit in calls) == (
        "entity-0",
        "entity-1",
        "entity-2",
    )


def test_transport_neutral_vector_projection_fails_soft_without_logging_proposition_content(monkeypatch, caplog) -> None:
    sensitive_proposition_id = "proposition:sensitive-customer-identifier"
    calls = 0

    def broken_vector_search(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        raise InvalidRequestError(f"malformed graph row for {sensitive_proposition_id}")

    engine = Engram()
    client = MemGraphConnection()
    monkeypatch.setattr(client, "vector_search_proposition_projections", broken_vector_search)
    engine._graph_client = client
    engine.config["graph"].update(
        {
            "enabled": True,
            "vector_enabled": True,
            "vector_index_name": "proposition_premise_embeddings",
            "vector_min_similarity": 0.0,
            "vector_limit": 3,
        }
    )
    monkeypatch.setattr(engine, "_encode_graph_query", lambda _text: [0.0] * 384)

    assert engine.graph_vector_proposition_projections("query", limit=3) == []
    assert calls == 1
    assert "InvalidRequestError" in caplog.text
    assert sensitive_proposition_id not in caplog.text

    projection = proposition_projection_from_graph_row(
        _row(semantic=True),
        PropositionProjectionQuery.VECTOR_V1,
        "proposition_premise_embeddings",
    )

    def over_returning_vector_search(*_args, **_kwargs):
        result = [projection, projection, projection, projection]
        return result

    over_returning_client = MemGraphConnection()
    monkeypatch.setattr(over_returning_client, "vector_search_proposition_projections", over_returning_vector_search)
    engine._graph_client = over_returning_client

    assert engine.graph_vector_proposition_projections("query", limit=3) == []


def test_fixed_by_id_projection_supports_publication_revalidation() -> None:
    client = MemGraphConnection()
    captured = {}
    row = _row()
    row.update(
        {
            "structured_match": 0.0,
            "structured_match_available": False,
            "semantic_similarity": 0.0,
            "semantic_similarity_available": False,
        }
    )

    def execute(query: str, parameters=()) -> list[dict[str, object]]:
        captured.update({"query": query, "parameters": parameters})
        result = [row]
        return result

    client._execute_read_query = execute
    projections = client.proposition_projection_by_id("proposition:01J5M6Q9J8")

    assert len(projections) == 1
    assert projections[0]["projection_id"] == PropositionProjectionQuery.BY_ID_V1
    assert projections[0]["structured_match_available"] is False
    assert projections[0]["semantic_similarity_available"] is False
    assert captured["parameters"] == {"proposition_id": "proposition:01J5M6Q9J8"}
    assert "c.id = $proposition_id" in captured["query"]
    assert "LIMIT 2" in captured["query"]
    assert "c.subject AS subject" not in captured["query"]

    conflicting = deepcopy(row)
    conflicting["object_entity_id"] = "entity:conflicting-object"
    client._execute_read_query = lambda query, parameters=(): [row, conflicting]
    with pytest.raises(InvalidRequestError, match="more rows than requested"):
        client.proposition_projection_by_id("proposition:01J5M6Q9J8")

    engine = Engram()
    engine._graph_client = client
    client._execute_read_query = execute
    assert engine.current_proposition_projection("proposition:01J5M6Q9J8") == tuple(projections)
