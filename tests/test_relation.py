"""Behavior-focused Section 8 canonical one-hop relation tests."""

from datetime import UTC, datetime

import pytest

from engram.constants import PROPOSITION_PROJECTION_FIELDS, CanonicalResolutionStatus, ExpectedObjectType, RelationPlanTemplate
from engram.contextual import enrich_query_frame
from engram.core import Engram
from engram.errors import InvalidRequestError
from engram.graph import (
    CanonicalEntityMatch,
    CanonicalPredicateMatch,
    MemGraphConnection,
    PropositionProjection,
    PropositionProjectionQuery,
    RelationPropositionProjection,
    proposition_projection,
    relation_proposition_projection_from_graph_row,
)
from engram.identity import entity_reference, query_identity, scope_key
from engram.relation import (
    canonical_resolution,
    one_hop_query_plan,
    resolve_canonical_predicate,
    resolve_canonical_subject,
    validate_one_hop_query_plan,
)
from engram.resolution import QueryFrameBuilder, ResolutionOutcome, capture_resolution_budget, query_frame_with_changes
from engram.resolvers import StructuredGraphResolver, resolver_budget as build_resolver_budget
from engram.service import EngramCore

NOW = datetime(2026, 8, 20, 16, 0, tzinfo=UTC)
START_NS = 1_000_000_000


def _proposition_row(proposition_id: str = "proposition:ada-birthplace", object_id: str = "entity:london") -> dict[str, object]:
    return {
        "proposition_id": proposition_id,
        "subject_entity_id": "entity:ada-lovelace",
        "predicate_id": "predicate:birth-place",
        "object_entity_id": object_id,
        "invalidated_at": "",
        "invalidated_at_available": False,
        "system_from": "2026-01-01T00:00:00Z",
        "system_from_available": True,
        "system_to": "",
        "system_to_available": False,
        "valid_from": "",
        "valid_from_available": False,
        "valid_to": "",
        "valid_to_available": False,
        "predicate_canonical": True,
        "ownership_category": "PUBLIC",
        "trust_category": "",
        "trust_category_available": False,
        "supplied_trust": 0.8,
        "supplied_trust_available": True,
        "supplied_trust_version": 1,
        "supplied_trust_version_available": True,
        "structured_match": 1.0,
        "structured_match_available": True,
        "semantic_similarity": 0.0,
        "semantic_similarity_available": False,
    }


def _relation_result(
    proposition_id: str = "proposition:ada-birthplace",
    object_id: str = "entity:london",
    object_label: str = "London",
    object_type: str = "PLACE",
    predicate_cardinality: str = "SINGLE",
) -> RelationPropositionProjection:
    row = {
        **_proposition_row(proposition_id, object_id),
        "object_label": object_label,
        "object_type": object_type,
        "predicate_cardinality": predicate_cardinality,
    }
    return relation_proposition_projection_from_graph_row(row)


def _current(result: RelationPropositionProjection) -> PropositionProjection:
    values = dict(result["projection"])
    values.update(
        {
            "projection_id": PropositionProjectionQuery.BY_ID_V1,
            "structured_match": 0.0,
            "structured_match_available": False,
            "semantic_similarity": 0.0,
            "semantic_similarity_available": False,
            "vector_index_id": "",
            "vector_index_id_available": False,
        }
    )
    return proposition_projection(**values)


def _frame(engine: Engram, text: str):
    budget = capture_resolution_budget(lambda: START_NS)
    base = QueryFrameBuilder(engine, lambda: START_NS, lambda: NOW).build(
        text,
        scope_key(namespace="tenant-a"),
        budget=budget,
    )
    return enrich_query_frame(base, current_turn=1)


def _lease(frame):
    budget = frame["budget"]
    return build_resolver_budget(
        max_candidates=budget["max_candidates"],
        max_graph_rows=budget["max_graph_rows"],
        max_vector_results=budget["max_vector_results"],
        max_evidence=budget["max_evidence"],
        max_evidence_bytes=budget["max_evidence_bytes"],
        max_output_bytes=budget["max_output_bytes"],
        max_diagnostic_bytes=budget["max_diagnostic_bytes"],
        max_working_memory_bytes=budget["max_working_memory_bytes"],
    )


def _entity_match(
    canonical_id: str = "entity:ada-lovelace",
    label: str = "Ada Lovelace",
) -> CanonicalEntityMatch:
    result: CanonicalEntityMatch = {
        "canonical_id": canonical_id,
        "primary_label": label,
        "aliases": ("Ada",),
        "edge_surfaces": ("Lovelace",),
        "entity_type": ExpectedObjectType.PERSON,
    }
    return result


def _predicate_match(
    canonical_id: str = "predicate:birth-place",
    label: str = "birth place",
    object_type: ExpectedObjectType = ExpectedObjectType.PLACE,
) -> CanonicalPredicateMatch:
    result: CanonicalPredicateMatch = {
        "canonical_id": canonical_id,
        "primary_label": label,
        "synonyms": ("born", "born in"),
        "object_type": object_type,
    }
    return result


class RelationGraph:
    available = True

    def __init__(self, results: tuple[RelationPropositionProjection, ...] = ()) -> None:
        self.results = list(results or (_relation_result(),))
        self.one_hop_calls: list[tuple[str, str, int, bool]] = []

    def canonical_entity_matches(self, surface, *, limit):
        return [_entity_match()][:limit] if surface.casefold() in {"ada lovelace", "ada"} else []

    def canonical_predicate_matches(self, surface, *, limit):
        return [_predicate_match()][:limit] if surface.casefold() in {"born", "bear", "born in"} else []

    def relation_one_hop_proposition_projections(self, subject_entity_id, predicate_id, *, limit, include_historical=False):
        self.one_hop_calls.append((subject_entity_id, predicate_id, limit, include_historical))
        return self.results[:limit]

    def proposition_projection_by_id(self, proposition_id):
        return [_current(result) for result in self.results if result["projection"]["proposition_id"] == proposition_id]


class ChangingRelationGraph(RelationGraph):
    def __init__(self) -> None:
        super().__init__()
        self.current_reads = 0

    def proposition_projection_by_id(self, proposition_id):
        self.current_reads += 1
        rows = super().proposition_projection_by_id(proposition_id)
        if self.current_reads < 2 or not rows:
            return rows
        values = dict(rows[0])
        values["object_entity_id"] = "entity:changed"
        return [proposition_projection(**values)]


def test_subject_resolution_reports_tied_canonical_entities_as_ambiguous() -> None:
    engine = Engram()
    frame = _frame(engine, "Where was Ada Lovelace born?")

    result = resolve_canonical_subject(
        frame,
        lambda _surface, **_kwargs: [
            _entity_match("entity:ada-lovelace"),
            _entity_match("entity:ada-byron", "Ada Lovelace"),
        ],
    )

    assert result["status"] == CanonicalResolutionStatus.AMBIGUOUS
    assert result["candidate_ids"] == ("entity:ada-byron", "entity:ada-lovelace")
    assert result["canonical_id"] == ""


def test_subject_resolution_uses_named_entity_and_alias_evidence(monkeypatch) -> None:
    engine = Engram()
    frame = _frame(engine, "Where was she born?")
    monkeypatch.setattr("engram.relation._named_entity_surfaces", lambda _text: ("Ada",))

    result = resolve_canonical_subject(frame, lambda _surface, **_kwargs: [_entity_match()])

    assert result["status"] == CanonicalResolutionStatus.SELECTED
    assert result["canonical_id"] == "entity:ada-lovelace"
    assert {"alias", "named_entity"}.issubset(result["evidence"])


def test_subject_resolution_accepts_proposition_edge_surface_evidence() -> None:
    engine = Engram()
    frame = _frame(engine, "Where was Lovelace born?")

    result = resolve_canonical_subject(frame, lambda _surface, **_kwargs: [_entity_match()])

    assert result["status"] == CanonicalResolutionStatus.SELECTED
    assert "edge_surface" in result["evidence"]


def test_explicit_subject_identity_bypasses_surface_lookup() -> None:
    engine = Engram()
    base = _frame(engine, "Ada")
    identity = base["identity"]
    explicit = query_identity(
        canonical_form=identity["canonical_form"],
        operator=identity["operator"],
        entities=(entity_reference("Ada Lovelace", "entity:ada-lovelace"),),
        relation=identity["relation"],
        qualifiers=identity["qualifiers"],
        lexical_terms=identity["lexical_terms"],
        scope=identity["scope"],
    )
    frame = query_frame_with_changes(base, {"identity": explicit})

    result = resolve_canonical_subject(frame, lambda *_args, **_kwargs: pytest.fail("lookup must not run"))

    assert result["status"] == CanonicalResolutionStatus.SELECTED
    assert result["score"] == 1.0
    assert result["evidence"] == ("caller_entity_identity",)


def test_predicate_synonyms_and_expected_type_disambiguate_same_surface() -> None:
    engine = Engram()
    frame = _frame(engine, "Where was Ada Lovelace born?")
    date = _predicate_match("predicate:birth-date", "birth date", ExpectedObjectType.DATE)
    place = _predicate_match()

    result = resolve_canonical_predicate(frame, lambda _surface, **_kwargs: [date, place])

    assert result["status"] == CanonicalResolutionStatus.SELECTED
    assert result["canonical_id"] == "predicate:birth-place"
    assert "predicate_synonym" in result["evidence"]


def test_dependency_preposition_paraphrase_resolves_predicate() -> None:
    engine = Engram()
    frame = _frame(engine, "Where did Ada Lovelace work at the Admiralty?")
    match: CanonicalPredicateMatch = {
        "canonical_id": "predicate:employer",
        "primary_label": "employer",
        "synonyms": ("work at",),
        "object_type": ExpectedObjectType.ENTITY,
    }

    result = resolve_canonical_predicate(frame, lambda surface, **_kwargs: [match] if surface == "work at" else [])

    assert result["status"] == CanonicalResolutionStatus.SELECTED
    assert result["canonical_id"] == "predicate:employer"
    assert "dependency_preposition" in result["evidence"]


def test_one_hop_plan_accepts_only_selected_identity_and_allowlisted_fields() -> None:
    subject = canonical_resolution(
        CanonicalResolutionStatus.SELECTED,
        canonical_id="entity:ada-lovelace",
        primary_label="Ada Lovelace",
        score=1.0,
        candidate_ids=("entity:ada-lovelace",),
        evidence=("caller_entity_identity",),
    )
    predicate = canonical_resolution(
        CanonicalResolutionStatus.SELECTED,
        canonical_id="predicate:birth-place",
        primary_label="birth place",
        object_type=ExpectedObjectType.PLACE,
        score=0.92,
        candidate_ids=("predicate:birth-place",),
        evidence=("predicate_synonym",),
    )

    plan = one_hop_query_plan(subject, predicate, ExpectedObjectType.PLACE, max_rows=3)

    assert plan["template_id"] == RelationPlanTemplate.ONE_HOP_PROPOSITION_V1
    assert set(plan) == {
        "schema_version",
        "template_id",
        "subject_entity_id",
        "predicate_id",
        "expected_object_type",
        "max_rows",
    }
    with pytest.raises(InvalidRequestError):
        validate_one_hop_query_plan({**plan, "cypher": "MATCH (n) RETURN n"})
    with pytest.raises(InvalidRequestError):
        one_hop_query_plan(
            canonical_resolution(
                CanonicalResolutionStatus.AMBIGUOUS,
                candidate_ids=("entity:a", "entity:b"),
                evidence=("alias",),
            ),
            predicate,
            ExpectedObjectType.PLACE,
        )


def test_graph_one_hop_uses_fixed_query_and_parameter_values_only() -> None:
    client = MemGraphConnection()
    captured = {}

    def execute(query, parameters=()):
        captured.update({"query": query, "parameters": parameters})
        return [
            {
                **_proposition_row(),
                "object_label": "London",
                "object_type": "PLACE",
                "predicate_cardinality": "SINGLE",
            }
        ]

    client._execute_read_query = execute

    result = client.relation_one_hop_proposition_projections(
        "entity:ada-lovelace",
        "predicate:birth-place",
        limit=10,
    )

    assert len(result) == 1
    assert result[0]["projection"]["projection_id"] == PropositionProjectionQuery.RELATION_ONE_HOP_V1
    assert captured["parameters"] == {
        "subject_entity_id": "entity:ada-lovelace",
        "predicate_id": "predicate:birth-place",
        "include_historical": False,
        "limit": 10,
    }
    assert "entity:ada-lovelace" not in captured["query"]
    assert "predicate:birth-place" not in captured["query"]
    assert "CALL " not in captured["query"]
    assert set(_proposition_row()) == PROPOSITION_PROJECTION_FIELDS


def test_graph_identity_resolution_uses_fixed_parameterized_capabilities() -> None:
    client = MemGraphConnection()
    captured = []

    def execute(query, parameters=()):
        captured.append((query, parameters))
        if "predicate:Predicate" in query:
            return [
                {
                    "canonical_id": "predicate:birth-place",
                    "primary_label": "birth place",
                    "synonyms": ["born"],
                    "object_type": "PLACE",
                }
            ]
        return [
            {
                "canonical_id": "entity:ada-lovelace",
                "primary_label": "Ada Lovelace",
                "aliases": ["Ada"],
                "edge_surfaces": ["Lovelace"],
                "entity_type": "PERSON",
            }
        ]

    client._execute_read_query = execute

    entities = client.canonical_entity_matches("Ada", limit=2)
    predicates = client.canonical_predicate_matches("born", limit=2)

    assert entities[0]["entity_type"] == ExpectedObjectType.PERSON
    assert predicates[0]["object_type"] == ExpectedObjectType.PLACE
    assert [parameters for _, parameters in captured] == [
        {"surface": "Ada", "limit": 2},
        {"surface": "born", "limit": 2},
    ]
    assert all(parameters["surface"] not in query for query, parameters in captured)
    assert "binding.surface_form" in captured[0][0]
    assert "predicate.synonyms" in captured[1][0]
    assert "predicate.label" in captured[1][0]


def test_core_one_hop_boundary_rejects_a_result_outside_the_requested_binding() -> None:
    engine = Engram()
    engine._graph_client = RelationGraph()

    with pytest.raises(ValueError, match="requested canonical binding"):
        engine.relation_one_hop_proposition_projections(
            "entity:other",
            "predicate:birth-place",
            row_limit=10,
        )


def test_relation_resolver_phrases_one_revalidated_type_match_and_enriches_evidence() -> None:
    engine = Engram()
    graph = RelationGraph()
    engine._graph_client = graph
    frame = _frame(engine, "Where was Ada Lovelace born?")

    result = StructuredGraphResolver(engine, lambda: START_NS).resolve(frame, _lease(frame))

    assert result["reason_code"] == "relation_proposition_candidate"
    assert len(result["candidates"]) == 1
    assert result["candidates"][0]["response"] == "Ada Lovelace — birth place: London."
    assert result["candidates"][0]["features"]["values"]["object_type_match"] == 1.0
    assert len(result["proposition_evidence"]) == 1
    record = result["proposition_evidence"][0]
    assert record["features"]["values"]["entity_match"] == 1.0
    assert record["features"]["values"]["relation_match"] == 0.92
    assert {"relation_plan_match", "relation_result_unique", "object_type_match"}.issubset(record["selection_reasons"])
    assert graph.one_hop_calls == [("entity:ada-lovelace", "predicate:birth-place", 10, False)]


def test_relation_resolver_requests_history_and_keeps_open_bounds_as_evidence() -> None:
    engine = Engram()
    graph = RelationGraph()
    engine._graph_client = graph
    frame = _frame(engine, "Where was Ada Lovelace born in 2024?")

    result = StructuredGraphResolver(engine, lambda: START_NS).resolve(frame, _lease(frame))

    assert graph.one_hop_calls == [("entity:ada-lovelace", "predicate:birth-place", 10, True)]
    assert result["candidates"] == ()
    assert result["diagnostics"]["selection_reason"] == "relation_temporal_bounds_open"
    assert result["proposition_evidence"][0]["validity"]["requested_start"] == "2024-01-01T00:00:00Z"
    assert result["proposition_evidence"][0]["validity"]["requested_end"] == "2025-01-01T00:00:00Z"


def test_relation_resolver_suppresses_phrase_for_multiple_or_type_mismatched_results() -> None:
    second = _relation_result("proposition:ada-other-place", "entity:oxford", "Oxford")
    engine = Engram()
    engine._graph_client = RelationGraph((_relation_result(), second))
    frame = _frame(engine, "Where was Ada Lovelace born?")

    ambiguous = StructuredGraphResolver(engine, lambda: START_NS).resolve(frame, _lease(frame))

    assert ambiguous["candidates"] == ()
    assert ambiguous["reason_code"] == "relation_proposition_conflict"
    assert ambiguous["diagnostics"]["selection_reason"] == "relation_conflict_single_value"
    assert ambiguous["diagnostics"]["conflict_proposition_ids"] == (
        "proposition:ada-birthplace",
        "proposition:ada-other-place",
    )
    assert len(ambiguous["proposition_evidence"]) == 2
    assert all(
        {"relation_result_ambiguous", "relation_conflict_single_value", "relation_conflicting_proposition"}.issubset(
            record["selection_reasons"]
        )
        for record in ambiguous["proposition_evidence"]
    )

    mismatch_engine = Engram()
    mismatch_engine._graph_client = RelationGraph((_relation_result(object_type="DATE"),))
    mismatch_frame = _frame(mismatch_engine, "Where was Ada Lovelace born?")
    mismatch = StructuredGraphResolver(mismatch_engine, lambda: START_NS).resolve(mismatch_frame, _lease(mismatch_frame))

    assert mismatch["candidates"] == ()
    assert mismatch["proposition_evidence"][0]["features"]["values"]["object_type_match"] == 0.0
    assert "object_type_mismatch" in mismatch["proposition_evidence"][0]["selection_reasons"]


def test_core_keeps_unique_graph_phrase_as_evidence_not_an_unsupported_answer() -> None:
    engine = Engram()
    engine._graph_client = RelationGraph()
    core = EngramCore(engine)

    result = core.resolve_request(
        "Where was Ada Lovelace born?",
        "relation-core-1",
        user_id="sarah",
        namespace="tenant-a",
        configured_resolvers=("structured_graph",),
    )

    assert result["outcome"] == ResolutionOutcome.EVIDENCE
    assert len(result["response_candidates"]) == 1
    assert result["response_candidates"][0]["response"] == "Ada Lovelace — birth place: London."
    assert result["evidence_package_available"] is True
    assert result["evidence_package"]["retained_count"] == 1


def test_fusion_suppresses_relation_phrase_if_current_proposition_changes_after_discovery() -> None:
    engine = Engram()
    graph = ChangingRelationGraph()
    engine._graph_client = graph
    core = EngramCore(engine)

    result = core.resolve_request(
        "Where was Ada Lovelace born?",
        "relation-toctou-1",
        user_id="sarah",
        namespace="tenant-a",
        configured_resolvers=("structured_graph",),
    )

    assert graph.current_reads == 2
    assert result["outcome"] == ResolutionOutcome.EVIDENCE
    assert result["response_candidates"] == ()
    assert result["evidence_package"]["retained_count"] == 1
