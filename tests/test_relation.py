"""Behavior-focused Section 8 canonical one-hop relation tests."""

from datetime import UTC, datetime

from pytest import fail as pytest_fail, raises as pytest_raises

from engram.constants import (
    PROPOSITION_PROJECTION_FIELDS,
    RESOLVER_BUDGET_FIELDS,
    UNCONSTRAINED_ASSERTION_BASIS,
    CanonicalResolutionStatus,
    ExpectedObjectType,
    PropositionProjectionQuery,
    RelationPlanTemplate,
    ResolutionOutcome,
    ResolverState,
)
from engram.contextual import enrich_query_frame
from engram.core import Engram
from engram.errors import InvalidRequestError
from engram.graph import MemGraphConnection, proposition_projection, relation_proposition_projection_from_graph_row
from engram.identity import entity_reference, query_identity, scope_key
from engram.relation import (
    RelationQuestion,
    canonical_resolution,
    one_hop_query_plan,
    resolve_canonical_predicate,
    resolve_canonical_subject,
)
from engram.resolution import QueryFrameBuilder, capture_resolution_budget, query_frame_with_changes
from engram.resolvers import StructuredGraphResolver, validate_resolver_budget
from engram.service import EngramCore

NOW = datetime(2026, 8, 20, 16, 0, tzinfo=UTC)
START_NS = 1_000_000_000
TENANT_A_SCOPE = scope_key(namespace="tenant-a")
# Full proposition projection row for Ada Lovelace's birth place (London); tests override the id and object.
# relation_proposition_projection_from_graph_row reads without mutating and the graph test doubles return copies,
# so these module constants stay read-only.
ADA_BIRTHPLACE_ROW = {
    "proposition_id": "proposition:ada-birthplace",
    "subject_entity_id": "entity:ada-lovelace",
    "predicate_id": "predicate:birth-place",
    "object_entity_id": "entity:london",
    "polarity": "positive",
    "modality_family": "none",
    "modality_operator": "none",
    "argument_count": 2,
    "qualification_count": 0,
    "context_count": 0,
    "applicability_count": 0,
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
    "structured_match": 1.0,
    "structured_match_available": True,
    "semantic_similarity": 0.0,
    "semantic_similarity_available": False,
}
# The one-hop graph row adds the object label/type and predicate cardinality to the projection row.
ADA_BIRTHPLACE_RELATION_ROW = {
    **ADA_BIRTHPLACE_ROW,
    "object_label": "London",
    "object_type": "PLACE",
    "predicate_cardinality": "SINGLE",
}
# A by-id current read carries the BY_ID query and no discovery match scores.
BY_ID_PROJECTION_CHANGES = {
    "projection_id": PropositionProjectionQuery.BY_ID,
    "structured_match": 0.0,
    "structured_match_available": False,
    "semantic_similarity": 0.0,
    "semantic_similarity_available": False,
    "vector_index_id": "",
    "vector_index_id_available": False,
}
ADA_ENTITY_MATCH = {
    "canonical_id": "entity:ada-lovelace",
    "primary_label": "Ada Lovelace",
    "aliases": ("Ada",),
    "edge_surfaces": ("Lovelace",),
    "entity_type": ExpectedObjectType.PERSON,
}
BIRTH_PLACE_PREDICATE_MATCH = {
    "canonical_id": "predicate:birth-place",
    "primary_label": "birth place",
    "synonyms": ("born", "born in"),
    "object_type": ExpectedObjectType.PLACE,
}


class RelationGraph:
    available = True

    def __init__(self, results: tuple[dict, ...] = ()) -> None:
        self.results = list(results or (relation_proposition_projection_from_graph_row(ADA_BIRTHPLACE_RELATION_ROW),))
        self.one_hop_calls: list[tuple[str, str, int, bool]] = []

    def canonical_entity_matches(self, surface, *, limit):
        result = [dict(ADA_ENTITY_MATCH)][:limit] if surface.casefold() in {"ada lovelace", "ada"} else []
        return result

    def canonical_predicate_matches(self, surface, *, limit):
        result = [dict(BIRTH_PLACE_PREDICATE_MATCH)][:limit] if surface.casefold() in {"born", "bear", "born in"} else []
        return result

    def relation_one_hop_proposition_projections(
        self, subject_entity_id, predicate_id, *, limit, include_historical=False, basis_window=UNCONSTRAINED_ASSERTION_BASIS
    ):
        del basis_window
        self.one_hop_calls.append((subject_entity_id, predicate_id, limit, include_historical))
        result = self.results[:limit]
        return result

    def proposition_projection_by_id(self, proposition_id, basis_window=UNCONSTRAINED_ASSERTION_BASIS):
        del basis_window
        result = [
            proposition_projection(**{**relation.get("projection", {}), **BY_ID_PROJECTION_CHANGES})
            for relation in self.results
            if relation.get("projection", {}).get("proposition_id", "") == proposition_id
        ]
        return result


class ChangingRelationGraph(RelationGraph):
    def __init__(self) -> None:
        super().__init__()
        self.current_reads = 0

    def proposition_projection_by_id(self, proposition_id, basis_window=UNCONSTRAINED_ASSERTION_BASIS):
        self.current_reads += 1
        rows = super().proposition_projection_by_id(proposition_id, basis_window)
        if self.current_reads < 2 or not rows:
            return rows
        values = dict(rows[0])
        values["object_entity_id"] = "entity:changed"
        result = [proposition_projection(**values)]
        return result


def test_subject_resolution_reports_tied_canonical_entities_as_ambiguous() -> None:
    frame = enrich_query_frame(
        QueryFrameBuilder(Engram(), lambda: START_NS, lambda: NOW).build(
            "Where was Ada Lovelace born?", TENANT_A_SCOPE, budget=capture_resolution_budget(lambda: START_NS)
        ),
        current_turn=1,
    )

    result = resolve_canonical_subject(
        frame,
        lambda internal_surface, **internal_kwargs: [
            dict(ADA_ENTITY_MATCH),
            {**ADA_ENTITY_MATCH, "canonical_id": "entity:ada-byron"},
        ],
        question=RelationQuestion(frame.get("resolved_text", "")),
    )

    assert result.get("status", CanonicalResolutionStatus.MISS) == CanonicalResolutionStatus.AMBIGUOUS
    assert result.get("candidate_ids", ()) == ("entity:ada-byron", "entity:ada-lovelace")
    assert "canonical_id" in result
    assert result.get("canonical_id", "") == ""


def test_subject_resolution_uses_named_entity_and_alias_evidence(monkeypatch) -> None:
    frame = enrich_query_frame(
        QueryFrameBuilder(Engram(), lambda: START_NS, lambda: NOW).build(
            "Where was she born?", TENANT_A_SCOPE, budget=capture_resolution_budget(lambda: START_NS)
        ),
        current_turn=1,
    )
    monkeypatch.setattr("engram.relation.named_entity_surfaces", lambda internal_text: ("Ada",))

    result = resolve_canonical_subject(
        frame,
        lambda internal_surface, **internal_kwargs: [dict(ADA_ENTITY_MATCH)],
        question=RelationQuestion(frame.get("resolved_text", "")),
    )

    assert result.get("status", CanonicalResolutionStatus.MISS) == CanonicalResolutionStatus.SELECTED
    assert result.get("canonical_id", "") == "entity:ada-lovelace"
    assert {"alias", "named_entity"}.issubset(result.get("evidence", ()))


def test_subject_resolution_accepts_proposition_edge_surface_evidence() -> None:
    frame = enrich_query_frame(
        QueryFrameBuilder(Engram(), lambda: START_NS, lambda: NOW).build(
            "Where was Lovelace born?", TENANT_A_SCOPE, budget=capture_resolution_budget(lambda: START_NS)
        ),
        current_turn=1,
    )

    result = resolve_canonical_subject(
        frame,
        lambda internal_surface, **internal_kwargs: [dict(ADA_ENTITY_MATCH)],
        question=RelationQuestion(frame.get("resolved_text", "")),
    )

    assert result.get("status", CanonicalResolutionStatus.MISS) == CanonicalResolutionStatus.SELECTED
    assert "edge_surface" in result.get("evidence", ())


def test_explicit_subject_identity_bypasses_surface_lookup() -> None:
    base = enrich_query_frame(
        QueryFrameBuilder(Engram(), lambda: START_NS, lambda: NOW).build(
            "Ada", TENANT_A_SCOPE, budget=capture_resolution_budget(lambda: START_NS)
        ),
        current_turn=1,
    )
    # The explicit identity keeps every base identity field and binds the subject entity.
    explicit = query_identity(
        **{**base.get("identity", {}), "entities": (entity_reference("Ada Lovelace", "entity:ada-lovelace"),)}
    )
    frame = query_frame_with_changes(base, {"identity": explicit})

    result = resolve_canonical_subject(
        frame,
        lambda *internal_args, **internal_kwargs: pytest_fail("lookup must not run"),
        question=RelationQuestion(frame.get("resolved_text", "")),
    )

    assert result.get("status", CanonicalResolutionStatus.MISS) == CanonicalResolutionStatus.SELECTED
    assert result.get("score", 0.0) == 1.0
    assert result.get("evidence", ()) == ("caller_entity_identity",)


def test_predicate_synonyms_and_expected_type_disambiguate_same_surface() -> None:
    frame = enrich_query_frame(
        QueryFrameBuilder(Engram(), lambda: START_NS, lambda: NOW).build(
            "Where was Ada Lovelace born?", TENANT_A_SCOPE, budget=capture_resolution_budget(lambda: START_NS)
        ),
        current_turn=1,
    )
    date = {
        **BIRTH_PLACE_PREDICATE_MATCH,
        "canonical_id": "predicate:birth-date",
        "primary_label": "birth date",
        "object_type": ExpectedObjectType.DATE,
    }
    place = dict(BIRTH_PLACE_PREDICATE_MATCH)

    result = resolve_canonical_predicate(
        frame,
        lambda internal_surface, **internal_kwargs: [date, place],
        question=RelationQuestion(frame.get("resolved_text", "")),
    )

    assert result.get("status", CanonicalResolutionStatus.MISS) == CanonicalResolutionStatus.SELECTED
    assert result.get("canonical_id", "") == "predicate:birth-place"
    assert "predicate_synonym" in result.get("evidence", ())


def test_dependency_preposition_paraphrase_resolves_predicate() -> None:
    frame = enrich_query_frame(
        QueryFrameBuilder(Engram(), lambda: START_NS, lambda: NOW).build(
            "Where did Ada Lovelace work at the Admiralty?", TENANT_A_SCOPE, budget=capture_resolution_budget(lambda: START_NS)
        ),
        current_turn=1,
    )
    match: dict = {
        "canonical_id": "predicate:employer",
        "primary_label": "employer",
        "synonyms": ("work at",),
        "object_type": ExpectedObjectType.ENTITY,
    }

    result = resolve_canonical_predicate(
        frame,
        lambda surface, **internal_kwargs: [match] if surface == "work at" else [],
        question=RelationQuestion(frame.get("resolved_text", "")),
    )

    assert result.get("status", CanonicalResolutionStatus.MISS) == CanonicalResolutionStatus.SELECTED
    assert result.get("canonical_id", "") == "predicate:employer"
    assert "dependency_preposition" in result.get("evidence", ())


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

    assert plan.get("template_id", "") == RelationPlanTemplate.ONE_HOP_PROPOSITION
    assert set(plan) == {
        "template_id",
        "subject_entity_id",
        "predicate_id",
        "expected_object_type",
        "max_rows",
    }
    with pytest_raises(InvalidRequestError):
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
        result = [dict(ADA_BIRTHPLACE_RELATION_ROW)]
        return result

    client.execute = execute

    result = client.relation_one_hop_proposition_projections(
        "entity:ada-lovelace",
        "predicate:birth-place",
        limit=10,
    )

    assert len(result) == 1
    assert result[0].get("projection", {}).get("projection_id", "") == PropositionProjectionQuery.RELATION_ONE_HOP
    assert captured.get("parameters", {}) == {
        "subject_entity_id": "entity:ada-lovelace",
        "predicate_id": "predicate:birth-place",
        "include_historical": False,
        "limit": 10,
        **UNCONSTRAINED_ASSERTION_BASIS,
    }
    assert "entity:ada-lovelace" not in captured.get("query", "")
    assert "predicate:birth-place" not in captured.get("query", "")
    assert "CALL " not in captured.get("query", "")
    assert set(ADA_BIRTHPLACE_ROW) == PROPOSITION_PROJECTION_FIELDS


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

    client.execute = execute

    entities = client.canonical_entity_matches("Ada", limit=2)
    predicates = client.canonical_predicate_matches("born", limit=2)

    assert entities[0].get("entity_type", ExpectedObjectType.UNKNOWN) == ExpectedObjectType.PERSON
    assert predicates[0].get("object_type", ExpectedObjectType.UNKNOWN) == ExpectedObjectType.PLACE
    assert [parameters for _, parameters in captured] == [
        {"surface": "Ada", "limit": 2},
        {"surface": "born", "limit": 2},
    ]
    assert all(parameters.get("surface", "") not in query for query, parameters in captured)
    assert "binding.surface_form" in captured[0][0]
    assert "predicate.synonyms" in captured[1][0]
    assert "predicate.label" in captured[1][0]


def test_core_one_hop_boundary_rejects_a_result_outside_the_requested_binding() -> None:
    engine = Engram()
    engine.internal_graph_client = RelationGraph()

    with pytest_raises(ValueError, match="requested canonical binding"):
        engine.relation_one_hop_proposition_projections(
            "entity:other",
            "predicate:birth-place",
            row_limit=10,
        )


def test_relation_resolver_phrases_one_revalidated_type_match_and_enriches_evidence() -> None:
    engine = Engram()
    graph = RelationGraph()
    engine.internal_graph_client = graph
    frame = enrich_query_frame(
        QueryFrameBuilder(engine, lambda: START_NS, lambda: NOW).build(
            "Where was Ada Lovelace born?", TENANT_A_SCOPE, budget=capture_resolution_budget(lambda: START_NS)
        ),
        current_turn=1,
    )
    lease = validate_resolver_budget({name: frame.get("budget", {}).get(name, 0) for name in RESOLVER_BUDGET_FIELDS})

    result = StructuredGraphResolver(engine, lambda: START_NS).resolve(frame, lease)
    candidates = result.get("candidates", ())
    evidence = result.get("proposition_evidence", ())

    assert result.get("reason_code", "") == "relation_proposition_candidate"
    assert len(candidates) == 1
    assert candidates[0].get("response", "") == "Ada Lovelace — birth place: London."
    assert candidates[0].get("features", {}).get("values", {}).get("object_type_match", 0.0) == 1.0
    assert len(evidence) == 1
    record_values = evidence[0].get("features", {}).get("values", {})
    assert record_values.get("entity_match", 0.0) == 1.0
    assert record_values.get("relation_match", 0.0) == 0.92
    assert {"relation_plan_match", "relation_result_unique", "object_type_match"}.issubset(
        evidence[0].get("selection_reasons", ())
    )
    assert graph.one_hop_calls == [("entity:ada-lovelace", "predicate:birth-place", 10, False)]


def test_relation_resolver_answers_a_one_hop_question_that_contains_of() -> None:
    engine = Engram()
    graph = RelationGraph()
    engine.internal_graph_client = graph
    frame = enrich_query_frame(
        QueryFrameBuilder(engine, lambda: START_NS, lambda: NOW).build(
            "Where was Ada Lovelace born, in terms of city?", TENANT_A_SCOPE, budget=capture_resolution_budget(lambda: START_NS)
        ),
        current_turn=1,
    )
    lease = validate_resolver_budget({name: frame.get("budget", {}).get(name, 0) for name in RESOLVER_BUDGET_FIELDS})

    result = StructuredGraphResolver(engine, lambda: START_NS).resolve(frame, lease)
    plain_frame = enrich_query_frame(
        QueryFrameBuilder(engine, lambda: START_NS, lambda: NOW).build(
            "Where was Ada Lovelace born?", TENANT_A_SCOPE, budget=capture_resolution_budget(lambda: START_NS)
        ),
        current_turn=1,
    )
    plain_lease = validate_resolver_budget(
        {name: plain_frame.get("budget", {}).get(name, 0) for name in RESOLVER_BUDGET_FIELDS}
    )
    plain = StructuredGraphResolver(engine, lambda: START_NS).resolve(plain_frame, plain_lease)
    plain_rows = plain.get("consumption", {}).get("graph_rows", 0)
    result_rows = result.get("consumption", {}).get("graph_rows", 0)

    assert result.get("reason_code", "") == "relation_proposition_candidate"
    assert result.get("candidates", ())[0].get("response", "") == "Ada Lovelace — birth place: London."
    # The rows composition spent resolving the subject still count.
    assert plain_rows < result_rows <= lease.get("max_graph_rows", 0)


def test_relation_resolver_accepts_a_request_longer_than_a_predicate_label() -> None:
    engine = Engram()
    graph = RelationGraph()
    engine.internal_graph_client = graph
    request = "Where was Ada Lovelace born? " + "I am asking for a history report about early computing. " * 6
    assert len(request.encode("utf-8")) > 256
    frame = enrich_query_frame(
        QueryFrameBuilder(engine, lambda: START_NS, lambda: NOW).build(
            request, TENANT_A_SCOPE, budget=capture_resolution_budget(lambda: START_NS)
        ),
        current_turn=1,
    )
    lease = validate_resolver_budget({name: frame.get("budget", {}).get(name, 0) for name in RESOLVER_BUDGET_FIELDS})

    result = StructuredGraphResolver(engine, lambda: START_NS).resolve(frame, lease)

    assert result.get("state", ResolverState.FAILED) == ResolverState.COMPLETED
    assert graph.one_hop_calls


def test_relation_resolver_requests_history_and_keeps_open_bounds_as_evidence() -> None:
    engine = Engram()
    graph = RelationGraph()
    engine.internal_graph_client = graph
    frame = enrich_query_frame(
        QueryFrameBuilder(engine, lambda: START_NS, lambda: NOW).build(
            "Where was Ada Lovelace born in 2024?", TENANT_A_SCOPE, budget=capture_resolution_budget(lambda: START_NS)
        ),
        current_turn=1,
    )
    lease = validate_resolver_budget({name: frame.get("budget", {}).get(name, 0) for name in RESOLVER_BUDGET_FIELDS})

    result = StructuredGraphResolver(engine, lambda: START_NS).resolve(frame, lease)
    validity = result.get("proposition_evidence", ())[0].get("validity", {})

    assert graph.one_hop_calls == [("entity:ada-lovelace", "predicate:birth-place", 10, True)]
    assert "candidates" in result
    assert result.get("candidates", ()) == ()
    assert result.get("diagnostics", {}).get("selection_reason", "") == "relation_temporal_bounds_open"
    assert validity.get("requested_start", "") == "2024-01-01T00:00:00Z"
    assert validity.get("requested_end", "") == "2025-01-01T00:00:00Z"


def test_relation_resolver_suppresses_phrase_for_multiple_or_type_mismatched_results() -> None:
    first = relation_proposition_projection_from_graph_row(ADA_BIRTHPLACE_RELATION_ROW)
    second = relation_proposition_projection_from_graph_row(
        {
            **ADA_BIRTHPLACE_RELATION_ROW,
            "proposition_id": "proposition:ada-other-place",
            "object_entity_id": "entity:oxford",
            "object_label": "Oxford",
        }
    )
    engine = Engram()
    engine.internal_graph_client = RelationGraph((first, second))
    frame = enrich_query_frame(
        QueryFrameBuilder(engine, lambda: START_NS, lambda: NOW).build(
            "Where was Ada Lovelace born?", TENANT_A_SCOPE, budget=capture_resolution_budget(lambda: START_NS)
        ),
        current_turn=1,
    )
    lease = validate_resolver_budget({name: frame.get("budget", {}).get(name, 0) for name in RESOLVER_BUDGET_FIELDS})

    ambiguous = StructuredGraphResolver(engine, lambda: START_NS).resolve(frame, lease)
    ambiguous_diagnostics = ambiguous.get("diagnostics", {})
    ambiguous_evidence = ambiguous.get("proposition_evidence", ())

    assert "candidates" in ambiguous
    assert ambiguous.get("candidates", ()) == ()
    assert ambiguous.get("reason_code", "") == "relation_proposition_conflict"
    assert ambiguous_diagnostics.get("selection_reason", "") == "relation_conflict_single_value"
    assert ambiguous_diagnostics.get("conflict_proposition_ids", ()) == (
        "proposition:ada-birthplace",
        "proposition:ada-other-place",
    )
    assert len(ambiguous_evidence) == 2
    assert all(
        {"relation_result_ambiguous", "relation_conflict_single_value", "relation_conflicting_proposition"}.issubset(
            record.get("selection_reasons", ())
        )
        for record in ambiguous_evidence
    )

    mismatch_engine = Engram()
    mismatch_engine.internal_graph_client = RelationGraph(
        (relation_proposition_projection_from_graph_row({**ADA_BIRTHPLACE_RELATION_ROW, "object_type": "DATE"}),)
    )
    mismatch_frame = enrich_query_frame(
        QueryFrameBuilder(mismatch_engine, lambda: START_NS, lambda: NOW).build(
            "Where was Ada Lovelace born?", TENANT_A_SCOPE, budget=capture_resolution_budget(lambda: START_NS)
        ),
        current_turn=1,
    )
    mismatch_lease = validate_resolver_budget(
        {name: mismatch_frame.get("budget", {}).get(name, 0) for name in RESOLVER_BUDGET_FIELDS}
    )
    mismatch = StructuredGraphResolver(mismatch_engine, lambda: START_NS).resolve(mismatch_frame, mismatch_lease)
    mismatch_record = mismatch.get("proposition_evidence", ())[0]
    mismatch_values = mismatch_record.get("features", {}).get("values", {})

    assert "candidates" in mismatch
    assert mismatch.get("candidates", ()) == ()
    assert "object_type_match" in mismatch_values
    assert mismatch_values.get("object_type_match", 0.0) == 0.0
    assert "object_type_mismatch" in mismatch_record.get("selection_reasons", ())


def test_core_keeps_unique_graph_phrase_as_evidence_not_an_unsupported_answer() -> None:
    engine = Engram()
    engine.internal_graph_client = RelationGraph()
    core = EngramCore(engine)

    result = core.resolve_request(
        "Where was Ada Lovelace born?",
        "relation-core-1",
        user_id="sarah",
        namespace="tenant-a",
        configured_resolvers=("structured_graph",),
    )

    response_candidates = result.get("response_candidates", ())

    assert result.get("outcome", ResolutionOutcome.MISS) == ResolutionOutcome.EVIDENCE
    assert len(response_candidates) == 1
    assert response_candidates[0].get("response", "") == "Ada Lovelace — birth place: London."
    assert result.get("evidence_package_available", False) is True
    assert result.get("evidence_package", {}).get("retained_count", 0) == 1


def test_fusion_suppresses_relation_phrase_if_current_proposition_changes_after_discovery() -> None:
    engine = Engram()
    graph = ChangingRelationGraph()
    engine.internal_graph_client = graph
    core = EngramCore(engine)

    result = core.resolve_request(
        "Where was Ada Lovelace born?",
        "relation-toctou-1",
        user_id="sarah",
        namespace="tenant-a",
        configured_resolvers=("structured_graph",),
    )

    assert graph.current_reads == 2
    assert result.get("outcome", ResolutionOutcome.MISS) == ResolutionOutcome.EVIDENCE
    assert "response_candidates" in result
    assert result.get("response_candidates", ()) == ()
    assert result.get("evidence_package", {}).get("retained_count", 0) == 1
