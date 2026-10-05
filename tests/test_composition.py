"""Behavior-focused Section 10 bounded graph-composition tests."""

from datetime import UTC, datetime

from pytest import fail as pytest_fail, mark as pytest_mark, raises as pytest_raises

from engram.composition import (
    composition_plan,
    composition_plan_to_dict,
    composition_step,
    execute_composition_plan,
    graph_composition_operator,
    linear_composition_plan,
    phrase_composition_result,
    resolve_composition_predicates,
    typed_order_value,
)
from engram.constants import (
    CanonicalResolutionStatus,
    CompositionReason,
    ExpectedObjectType,
    GraphCompositionOperator,
    PredicateCardinality,
)
from engram.core import Engram
from engram.errors import InvalidRequestError, ResolutionCancelledError
from engram.evidence import PropositionEligibilityEvaluator, proposition_evidence_record
from engram.fusion import EngramCandidateAuthority
from engram.graph import (
    PropositionProjectionQuery,
    relation_proposition_projection_from_graph_row,
    validate_proposition_projection,
)
from engram.identity import scope_key
from engram.relation import RelationQuestion, canonical_resolution, resolve_canonical_subject
from engram.resolution import (
    QueryFrameBuilder,
    ResolutionOutcome,
    build_evidence_package,
    evidence_package_from_json,
    evidence_package_to_json,
    proposition_evidence_path_step,
    proposition_evidence_record_from_json,
    proposition_evidence_record_to_json,
    proposition_evidence_record_with_changes,
    validate_proposition_evidence_path_step,
)
from engram.resolvers import StructuredGraphResolver, resolver_budget, resolver_budget_with_changes
from engram.service import EngramCore

NOW = datetime(2026, 8, 20, 16, 0, tzinfo=UTC)
FOUNDER_BIRTHPLACE_REQUEST = "Where was Microsoft's founder born?"
PUBLIC_SCOPE = scope_key(namespace="public")

# Read-only fixtures. The composition owner validates and copies plans, resolutions, and
# projections; tests that vary one build a new dictionary from it.
MICROSOFT_ROOT = canonical_resolution(
    CanonicalResolutionStatus.SELECTED,
    canonical_id="entity:microsoft",
    primary_label="Microsoft",
    object_type=ExpectedObjectType.ENTITY,
    score=1.0,
    candidate_ids=("entity:microsoft",),
    evidence=("test_identity",),
)
FOUNDER_BIRTHPLACE_PREDICATES = (
    canonical_resolution(
        CanonicalResolutionStatus.SELECTED,
        canonical_id="predicate:founded-by",
        primary_label="founded by",
        object_type=ExpectedObjectType.PERSON,
        score=1.0,
        candidate_ids=("predicate:founded-by",),
        evidence=("test_identity",),
    ),
    canonical_resolution(
        CanonicalResolutionStatus.SELECTED,
        canonical_id="predicate:born-in",
        primary_label="born in",
        object_type=ExpectedObjectType.PLACE,
        score=1.0,
        candidate_ids=("predicate:born-in",),
        evidence=("test_identity",),
    ),
)
FOUNDER_BIRTHPLACE_PLAN = linear_composition_plan(
    MICROSOFT_ROOT,
    FOUNDER_BIRTHPLACE_PREDICATES,
    GraphCompositionOperator.LOOKUP,
    ExpectedObjectType.PLACE,
    max_rows=32,
    max_candidates_per_step=4,
)
# Shared non-identity fields of a current, trusted, single-valued public relation row; every
# relation adds its own identity, object label, and object type fields.
PUBLIC_RELATION_ROW = {
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
    "predicate_cardinality": PredicateCardinality.SINGLE.value,
}
FOUNDER = relation_proposition_projection_from_graph_row(
    {
        **PUBLIC_RELATION_ROW,
        "proposition_id": "proposition:microsoft-founder",
        "subject_entity_id": "entity:microsoft",
        "predicate_id": "predicate:founded-by",
        "object_entity_id": "entity:founder",
        "object_label": "Founder",
        "object_type": ExpectedObjectType.PERSON.value,
    }
)
BIRTHPLACE = relation_proposition_projection_from_graph_row(
    {
        **PUBLIC_RELATION_ROW,
        "proposition_id": "proposition:founder-born-in",
        "subject_entity_id": "entity:founder",
        "predicate_id": "predicate:born-in",
        "object_entity_id": "entity:london",
        "object_label": "London",
        "object_type": ExpectedObjectType.PLACE.value,
    }
)
FOUNDER_BIRTHPLACE_ROWS = {
    ("entity:microsoft", "predicate:founded-by"): [FOUNDER],
    ("entity:founder", "predicate:born-in"): [BIRTHPLACE],
}
# The fixed by-ID read returns the current record without discovery measurements or index provenance.
BY_ID_CURRENT_FIELDS = {
    "projection_id": PropositionProjectionQuery.BY_ID,
    "structured_match": 0.0,
    "structured_match_available": False,
    "semantic_similarity": 0.0,
    "semantic_similarity_available": False,
    "vector_index_id": "",
    "vector_index_id_available": False,
}


def founder_birthplace_query(subject, predicate, limit):
    result = FOUNDER_BIRTHPLACE_ROWS.get((subject, predicate), [])[:limit]
    return result


def execute(plan=FOUNDER_BIRTHPLACE_PLAN, query=founder_birthplace_query, current_items=(FOUNDER, BIRTHPLACE)):
    """Run one plan against a fake graph whose current by-ID reads return ``current_items``."""
    frame = QueryFrameBuilder(Engram(), lambda: 1, lambda: NOW).build(FOUNDER_BIRTHPLACE_REQUEST, PUBLIC_SCOPE)
    evaluator = PropositionEligibilityEvaluator()
    current = {}
    for item in current_items:
        projection = item.get("projection", {})
        current[projection.get("proposition_id", "")] = (validate_proposition_projection({**projection, **BY_ID_CURRENT_FIELDS}),)
    result = execute_composition_plan(
        plan,
        query,
        lambda projection: evaluator.evaluate(projection, frame),
        lambda projection: evaluator.revalidate(
            projection, frame, lambda proposition_id, basis_window: current.get(proposition_id, ())
        ),
    )
    return result


def test_plan_carries_only_fixed_predicates_bindings_and_non_time_limits() -> None:
    plan = FOUNDER_BIRTHPLACE_PLAN
    serialized = composition_plan_to_dict(plan)
    plan_steps = plan.get("steps", ())

    assert [step.get("predicate_id", "") for step in plan_steps] == ["predicate:founded-by", "predicate:born-in"]
    assert [step.get("subject_binding", "") for step in plan_steps] == ["$root", "$hop1"]
    assert "timeout" not in serialized
    assert "cypher" not in serialized
    with pytest_raises(InvalidRequestError):
        composition_plan_to_dict({**plan, "cypher": "MATCH (n) RETURN n"})


def test_plan_rejects_cartesian_and_cyclic_bindings() -> None:
    steps = list(FOUNDER_BIRTHPLACE_PLAN.get("steps", ()))
    cartesian = dict(steps[1])
    cartesian["subject_binding"] = "$unbound"
    with pytest_raises(InvalidRequestError):
        composition_plan(
            GraphCompositionOperator.LOOKUP,
            "entity:microsoft",
            "Microsoft",
            (steps[0], cartesian),
            "$result",
        )

    cycle = dict(steps[1])
    cycle["object_binding"] = "$root"
    with pytest_raises(InvalidRequestError):
        composition_plan(
            GraphCompositionOperator.LOOKUP,
            "entity:microsoft",
            "Microsoft",
            (steps[0], cycle),
            "$root",
        )


def test_compiler_preserves_a_repeated_predicate_at_two_distinct_positions() -> None:
    frame = QueryFrameBuilder(Engram(), lambda: 1, lambda: NOW).build(
        "Who is Sarah's married partner married to?",
        PUBLIC_SCOPE,
    )
    root = canonical_resolution(
        CanonicalResolutionStatus.SELECTED,
        canonical_id="entity:sarah",
        primary_label="Sarah",
        object_type=ExpectedObjectType.PERSON,
        score=1.0,
        candidate_ids=("entity:sarah",),
        evidence=("test_identity",),
    )

    predicates = resolve_composition_predicates(
        frame,
        root,
        lambda surface, **internal_kwargs: (
            [
                {
                    "canonical_id": "predicate:married-to",
                    "primary_label": "married to",
                    "object_type": ExpectedObjectType.PERSON,
                }
            ]
            if surface == "married"
            else []
        ),
    )

    assert tuple(value.get("canonical_id", "") for value in predicates) == (
        "predicate:married-to",
        "predicate:married-to",
    )


def test_boolean_plan_requires_explicit_bounded_branches() -> None:
    first = composition_step(
        0,
        0,
        "$root",
        "entity:microsoft",
        "predicate:founded-by",
        "founded by",
        "$result",
        ExpectedObjectType.PERSON,
    )
    second = composition_step(
        1,
        0,
        "$root",
        "entity:microsoft",
        "predicate:located-in",
        "located in",
        "$result",
        ExpectedObjectType.PLACE,
    )

    plan = composition_plan(
        GraphCompositionOperator.AND,
        "entity:microsoft",
        "Microsoft",
        (first, second),
        "$result",
    )

    assert all("branch" in step for step in plan.get("steps", ()))
    assert len({step.get("branch", 0) for step in plan.get("steps", ())}) == 2
    with pytest_raises(InvalidRequestError):
        composition_plan(
            GraphCompositionOperator.AND,
            "entity:microsoft",
            "Microsoft",
            (first,),
            "$result",
        )


def test_boolean_execution_uses_complete_branches_without_guessing() -> None:
    founder_step = composition_step(
        0,
        0,
        "$root",
        "entity:microsoft",
        "predicate:founded-by",
        "founded by",
        "$result",
        ExpectedObjectType.PERSON,
    )
    location_step = composition_step(
        1,
        0,
        "$root",
        "entity:microsoft",
        "predicate:located-in",
        "located in",
        "$result",
        ExpectedObjectType.PLACE,
    )
    location = relation_proposition_projection_from_graph_row(
        {
            **PUBLIC_RELATION_ROW,
            "proposition_id": "proposition:microsoft-location",
            "subject_entity_id": "entity:microsoft",
            "predicate_id": "predicate:located-in",
            "object_entity_id": "entity:redmond",
            "object_label": "Redmond",
            "object_type": ExpectedObjectType.PLACE.value,
        }
    )
    rows = {
        ("entity:microsoft", "predicate:founded-by"): [FOUNDER],
        ("entity:microsoft", "predicate:located-in"): [location],
    }

    def run(operator, selected_rows=rows):
        plan = composition_plan(
            operator,
            "entity:microsoft",
            "Microsoft",
            (founder_step, location_step),
            "$result",
            max_rows=16,
        )
        result = execute(
            plan=plan,
            query=lambda subject, predicate, limit: selected_rows.get((subject, predicate), [])[:limit],
            current_items=(FOUNDER, location),
        )
        return result

    conjunction = run(GraphCompositionOperator.AND)
    conjunction_flags = (
        conjunction.get("truth_available", False),
        conjunction.get("truth_value", False),
        conjunction.get("direct_result", False),
    )
    assert conjunction_flags == (True, True, True)

    only_founder = {("entity:microsoft", "predicate:founded-by"): [FOUNDER]}
    failed_conjunction = run(GraphCompositionOperator.AND, only_founder)
    disjunction = run(GraphCompositionOperator.OR, only_founder)
    assert "truth_value" in failed_conjunction
    assert (failed_conjunction.get("truth_available", False), failed_conjunction.get("truth_value", False)) == (True, False)
    assert (disjunction.get("truth_available", False), disjunction.get("truth_value", False)) == (True, True)


@pytest_mark.parametrize(
    ("operator", "expected"),
    (
        (GraphCompositionOperator.COUNT, "2"),
        (GraphCompositionOperator.MIN, "2"),
        (GraphCompositionOperator.MAX, "10"),
        (GraphCompositionOperator.ORDER, "2 | 10"),
    ),
)
def test_aggregates_require_complete_typed_distinct_results(operator, expected) -> None:
    values = (
        relation_proposition_projection_from_graph_row(
            {
                **PUBLIC_RELATION_ROW,
                "proposition_id": "proposition:founder-score-10",
                "subject_entity_id": "entity:founder",
                "predicate_id": "predicate:score",
                "object_entity_id": "number:10",
                "object_label": "10",
                "object_type": ExpectedObjectType.NUMBER.value,
                "predicate_cardinality": PredicateCardinality.MULTI.value,
            }
        ),
        relation_proposition_projection_from_graph_row(
            {
                **PUBLIC_RELATION_ROW,
                "proposition_id": "proposition:founder-score-2",
                "subject_entity_id": "entity:founder",
                "predicate_id": "predicate:score",
                "object_entity_id": "number:2",
                "object_label": "2",
                "object_type": ExpectedObjectType.NUMBER.value,
                "predicate_cardinality": PredicateCardinality.MULTI.value,
            }
        ),
    )
    score_predicate = canonical_resolution(
        CanonicalResolutionStatus.SELECTED,
        canonical_id="predicate:score",
        primary_label="score",
        object_type=ExpectedObjectType.NUMBER,
        score=1.0,
        candidate_ids=("predicate:score",),
        evidence=("test_identity",),
    )
    plan = linear_composition_plan(
        MICROSOFT_ROOT,
        (FOUNDER_BIRTHPLACE_PREDICATES[0], score_predicate),
        operator,
        ExpectedObjectType.NUMBER,
        max_rows=32,
        max_candidates_per_step=4,
    )
    rows = {
        ("entity:microsoft", "predicate:founded-by"): [FOUNDER],
        ("entity:founder", "predicate:score"): list(values),
    }
    result = execute(
        plan=plan,
        query=lambda subject, predicate, limit: rows.get((subject, predicate), [])[:limit],
        current_items=(FOUNDER, *values),
    )

    assert result.get("direct_result", False) is True
    assert result.get("aggregate_value_available", False) is True
    assert result.get("aggregate_value", "") == expected


def test_two_hop_execution_preserves_order_and_phrases_one_complete_path() -> None:
    execution = execute()
    first_path = execution.get("complete_paths", ())[0]
    path_ids = tuple(entry.get("proposition", {}).get("projection", {}).get("proposition_id", "") for entry in first_path)

    assert execution.get("direct_result", False) is True
    assert execution.get("graph_rows", 0) == 4
    assert execution.get("terminal_entity_ids", ()) == ("entity:london",)
    assert path_ids == ("proposition:microsoft-founder", "proposition:founder-born-in")
    assert phrase_composition_result(FOUNDER_BIRTHPLACE_PLAN, execution) == "Microsoft — founded by → born in: London."


def test_cycle_and_partial_dependency_failure_never_produce_a_direct_result() -> None:
    cycle = relation_proposition_projection_from_graph_row(
        {
            **PUBLIC_RELATION_ROW,
            "proposition_id": "proposition:founder-born-in",
            "subject_entity_id": "entity:founder",
            "predicate_id": "predicate:born-in",
            "object_entity_id": "entity:microsoft",
            "object_label": "Microsoft",
            "object_type": ExpectedObjectType.PLACE.value,
        }
    )

    def cycle_query(subject, predicate, internal_limit):
        assert internal_limit > 0
        result = [FOUNDER] if subject == "entity:microsoft" else [cycle] if predicate == "predicate:born-in" else []
        return result

    # A pruned cycle is a real path whose terminal value was dropped, so neither a lookup
    # nor an exhaustive absence or count may be stated directly.
    for operator in (GraphCompositionOperator.LOOKUP, GraphCompositionOperator.EXISTS, GraphCompositionOperator.COUNT):
        plan = linear_composition_plan(
            MICROSOFT_ROOT,
            FOUNDER_BIRTHPLACE_PREDICATES,
            operator,
            ExpectedObjectType.PLACE,
            max_rows=32,
            max_candidates_per_step=4,
        )
        cycle_result = execute(plan=plan, query=cycle_query, current_items=(FOUNDER, cycle))
        assert "direct_result" in cycle_result
        assert cycle_result.get("direct_result", False) is False
        assert CompositionReason.CYCLE in cycle_result.get("reasons", ())

    def failed_query(subject, internal_predicate, internal_limit):
        del internal_predicate, internal_limit
        if subject == "entity:founder":
            raise RuntimeError("graph unavailable")
        return [FOUNDER]

    failed = execute(query=failed_query)
    assert "complete_paths" in failed
    assert failed.get("complete_paths", ()) == ()
    assert len(failed.get("partial_paths", ())) == 1
    assert CompositionReason.DEPENDENCY_FAILED in failed.get("reasons", ())


def test_candidate_sentinel_marks_completeness_unknown_instead_of_counting_partial_rows() -> None:
    extras = [
        relation_proposition_projection_from_graph_row(
            {
                **PUBLIC_RELATION_ROW,
                "proposition_id": f"proposition:founder-{index}",
                "subject_entity_id": "entity:microsoft",
                "predicate_id": "predicate:founded-by",
                "object_entity_id": f"entity:founder-{index}",
                "object_label": f"Founder {index}",
                "object_type": ExpectedObjectType.PERSON.value,
            }
        )
        for index in range(5)
    ]

    def saturated_query(internal_subject, internal_predicate, limit):
        del internal_subject, internal_predicate
        result = extras[:limit]
        return result

    result = execute(query=saturated_query)

    assert result.get("truncated", False) is True
    assert "direct_result" in result
    assert result.get("direct_result", False) is False
    assert CompositionReason.CANDIDATE_LIMIT in result.get("reasons", ())
    assert CompositionReason.COMPLETENESS_UNKNOWN in result.get("reasons", ())


def test_count_refuses_unknown_or_duplicate_cardinality() -> None:
    count_plan = linear_composition_plan(
        MICROSOFT_ROOT,
        FOUNDER_BIRTHPLACE_PREDICATES,
        GraphCompositionOperator.COUNT,
        ExpectedObjectType.PLACE,
        max_rows=32,
        max_candidates_per_step=4,
    )
    unknown = relation_proposition_projection_from_graph_row(
        {
            **PUBLIC_RELATION_ROW,
            "proposition_id": "proposition:founder-born-in",
            "subject_entity_id": "entity:founder",
            "predicate_id": "predicate:born-in",
            "object_entity_id": "entity:london",
            "object_label": "London",
            "object_type": ExpectedObjectType.PLACE.value,
            "predicate_cardinality": PredicateCardinality.UNKNOWN.value,
        }
    )
    rows = {
        ("entity:microsoft", "predicate:founded-by"): [FOUNDER],
        ("entity:founder", "predicate:born-in"): [unknown],
    }
    result = execute(
        plan=count_plan,
        query=lambda subject, predicate, limit: rows.get((subject, predicate), [])[:limit],
    )

    assert "aggregate_value_available" in result
    assert result.get("aggregate_value_available", False) is False
    assert "direct_result" in result
    assert result.get("direct_result", False) is False
    assert CompositionReason.CARDINALITY_UNKNOWN in result.get("reasons", ())

    second_founder = relation_proposition_projection_from_graph_row(
        {
            **PUBLIC_RELATION_ROW,
            "proposition_id": "proposition:microsoft-founder-2",
            "subject_entity_id": "entity:microsoft",
            "predicate_id": "predicate:founded-by",
            "object_entity_id": "entity:founder-2",
            "object_label": "Founder 2",
            "object_type": ExpectedObjectType.PERSON.value,
            "predicate_cardinality": PredicateCardinality.MULTI.value,
        }
    )
    same_place = relation_proposition_projection_from_graph_row(
        {
            **PUBLIC_RELATION_ROW,
            "proposition_id": "proposition:founder-2-born-in",
            "subject_entity_id": "entity:founder-2",
            "predicate_id": "predicate:born-in",
            "object_entity_id": "entity:london",
            "object_label": "London",
            "object_type": ExpectedObjectType.PLACE.value,
        }
    )
    duplicate_rows = {
        ("entity:microsoft", "predicate:founded-by"): [FOUNDER, second_founder],
        ("entity:founder", "predicate:born-in"): [BIRTHPLACE],
        ("entity:founder-2", "predicate:born-in"): [same_place],
    }
    duplicate_count = execute(
        plan=count_plan,
        query=lambda subject, predicate, limit: duplicate_rows.get((subject, predicate), [])[:limit],
        current_items=(FOUNDER, second_founder, BIRTHPLACE, same_place),
    )
    deduplicated_lookup = execute(
        query=lambda subject, predicate, limit: duplicate_rows.get((subject, predicate), [])[:limit],
        current_items=(FOUNDER, second_founder, BIRTHPLACE, same_place),
    )

    assert "direct_result" in duplicate_count
    assert duplicate_count.get("direct_result", False) is False
    assert CompositionReason.CARDINALITY_CONFLICT in duplicate_count.get("reasons", ())
    assert deduplicated_lookup.get("terminal_entity_ids", ()) == ("entity:london",)
    assert deduplicated_lookup.get("direct_result", False) is True


def test_composed_evidence_path_round_trips_ordered_propositions_and_filters() -> None:
    execution = execute()
    frame = QueryFrameBuilder(Engram(), lambda: 1, lambda: NOW).build(FOUNDER_BIRTHPLACE_REQUEST, PUBLIC_SCOPE)
    evaluator = PropositionEligibilityEvaluator()
    first_path = execution.get("complete_paths", ())[0]
    terminal_projection = first_path[-1].get("proposition", {}).get("projection", {})
    current_birthplace = validate_proposition_projection({**BIRTHPLACE.get("projection", {}), **BY_ID_CURRENT_FIELDS})
    decision = evaluator.revalidate(
        terminal_projection,
        frame,
        lambda internal_proposition_id, internal_basis_window: (current_birthplace,),
    )
    base = proposition_evidence_record(terminal_projection, decision, frame, "structured_graph")
    steps = []
    for position, entry in enumerate(first_path):
        projection = entry.get("proposition", {}).get("projection", {})
        step = entry.get("step", {})
        steps.append(
            proposition_evidence_path_step(
                position,
                projection.get("proposition_id", ""),
                projection.get("subject_entity_id", ""),
                projection.get("predicate_id", ""),
                projection.get("object_entity_id", ""),
                GraphCompositionOperator.LOOKUP,
                step.get("subject_binding", ""),
                step.get("object_binding", ""),
                ("canonical_identity", "publication_revalidation", "temporal_eligibility", "visibility"),
            )
        )
    composed = proposition_evidence_record_with_changes(base, {"path": tuple(steps)})
    composed_ids = [validate_proposition_evidence_path_step(step).get("proposition_id", "") for step in composed.get("path", ())]

    assert proposition_evidence_record_from_json(proposition_evidence_record_to_json(composed)) == composed
    package = build_evidence_package((composed,))
    assert evidence_package_from_json(evidence_package_to_json(package)) == package
    assert composed_ids == ["proposition:microsoft-founder", "proposition:founder-born-in"]
    with pytest_raises(InvalidRequestError, match="ordered"):
        proposition_evidence_record_with_changes(base, {"path": tuple(reversed(steps))})


def test_cooperative_cancellation_propagates_before_graph_work() -> None:
    def cancel() -> bool:
        raise RuntimeError("cancelled")

    with pytest_raises(RuntimeError, match="cancelled"):
        execute_composition_plan(
            FOUNDER_BIRTHPLACE_PLAN,
            lambda *internal_args: pytest_fail("query must not run"),
            lambda internal_projection: pytest_fail("evaluate must not run"),
            lambda internal_projection: pytest_fail("revalidate must not run"),
            cancel,
        )

    def cancelled_query(subject, internal_predicate, internal_limit):
        del internal_predicate, internal_limit
        if subject == "entity:founder":
            raise ResolutionCancelledError("cancelled inside the graph read")
        return [FOUNDER]

    # Cancellation raised by the graph callback is the caller's, not a dependency failure.
    with pytest_raises(ResolutionCancelledError):
        execute(query=cancelled_query)


def test_structured_resolver_compiles_revalidates_and_publishes_two_hop_evidence(monkeypatch) -> None:
    founder = relation_proposition_projection_from_graph_row(
        {
            **PUBLIC_RELATION_ROW,
            "proposition_id": "proposition:microsoft-founder",
            "subject_entity_id": "entity:microsoft",
            "predicate_id": "predicate:founded-by",
            "object_entity_id": "entity:founder",
            "object_label": "Founder",
            "object_type": ExpectedObjectType.PERSON.value,
            "predicate_cardinality": PredicateCardinality.MULTI.value,
        }
    )

    class CompositionGraph:
        available = True

        def __init__(self) -> None:
            self.rows = {
                ("entity:microsoft", "predicate:founded-by"): [founder],
                ("entity:founder", "predicate:born-in"): [BIRTHPLACE],
            }
            self.one_hop_calls = []

        def canonical_entity_matches(self, surface, *, limit):
            row = {
                "canonical_id": "entity:microsoft",
                "primary_label": "Microsoft",
                "aliases": ("Microsoft",),
                "edge_surfaces": (),
                "entity_type": ExpectedObjectType.ENTITY,
            }
            result = [row][:limit] if surface.casefold() == "microsoft" else []
            return result

        def canonical_predicate_matches(self, surface, *, limit):
            normalized = surface.casefold()
            row = {}
            if normalized == "founder":
                row = {
                    "canonical_id": "predicate:founded-by",
                    "primary_label": "founded by",
                    "synonyms": ("founder",),
                    "object_type": ExpectedObjectType.PERSON,
                }
            elif normalized == "born":
                row = {
                    "canonical_id": "predicate:born-in",
                    "primary_label": "born in",
                    "synonyms": ("born",),
                    "object_type": ExpectedObjectType.PLACE,
                }
            result = [row][:limit] if row else []
            return result

        def relation_one_hop_proposition_projections(
            self,
            subject_entity_id,
            predicate_id,
            *,
            limit,
            include_historical=False,
            basis_window,
        ):
            del basis_window
            self.one_hop_calls.append((subject_entity_id, predicate_id, limit, include_historical))
            result = self.rows.get((subject_entity_id, predicate_id), [])[:limit]
            return result

        def proposition_projection_by_id(self, proposition_id, basis_window):
            del basis_window
            result = [
                validate_proposition_projection({**item.get("projection", {}), **BY_ID_CURRENT_FIELDS})
                for items in self.rows.values()
                for item in items
                if item.get("projection", {}).get("proposition_id", "") == proposition_id
            ]
            return result

    graph = CompositionGraph()
    engine = Engram()
    engine.internal_graph_client = graph
    monkeypatch.setattr("engram.relation.named_entity_surfaces", lambda internal_text: ("Microsoft",))
    frame = QueryFrameBuilder(engine, lambda: 1, lambda: NOW).build(FOUNDER_BIRTHPLACE_REQUEST, PUBLIC_SCOPE)
    lease = resolver_budget(
        max_candidates=4,
        max_graph_rows=64,
        max_vector_results=0,
        max_evidence=10,
        max_evidence_bytes=65_536,
        max_output_bytes=65_536,
        max_diagnostic_bytes=65_536,
        max_working_memory_bytes=1_048_576,
    )
    question = RelationQuestion(frame.get("resolved_text", ""))
    subject = resolve_canonical_subject(frame, engine.canonical_entity_matches, question=question)
    assert subject.get("status", "") == CanonicalResolutionStatus.SELECTED
    predicates = resolve_composition_predicates(frame, subject, engine.canonical_predicate_matches)
    assert tuple(value.get("canonical_id", "") for value in predicates) == ("predicate:founded-by", "predicate:born-in")
    assert graph_composition_operator(frame) == GraphCompositionOperator.LOOKUP

    result = StructuredGraphResolver(engine, lambda: 1).resolve(frame, lease)
    candidates = result.get("candidates", [])
    proposition_evidence = result.get("proposition_evidence", [])

    assert result.get("reason_code", "") == "graph_composition_candidate"
    assert len(candidates) == 1
    assert candidates[0].get("response", "") == "Microsoft — founded by → born in: London."
    assert len(proposition_evidence) == 1
    path_ids = tuple(
        validate_proposition_evidence_path_step(step).get("proposition_id", "") for step in proposition_evidence[0].get("path", ())
    )
    assert path_ids == ("proposition:microsoft-founder", "proposition:founder-born-in")
    assert [(subject, predicate) for subject, predicate, internal_limit, historical in graph.one_hop_calls] == [
        ("entity:microsoft", "predicate:founded-by"),
        ("entity:founder", "predicate:born-in"),
    ]
    assert EngramCandidateAuthority(engine).evaluate(candidates[0], frame).get("answer_eligible", False) is True

    constrained = resolver_budget_with_changes(lease, {"max_graph_rows": 6})
    exhausted = StructuredGraphResolver(engine, lambda: 1).resolve(frame, constrained)
    consumption = exhausted.get("consumption", {})
    assert exhausted.get("reason_code", "") == CompositionReason.ROW_LIMIT.value
    assert "graph_rows" in consumption
    assert "max_graph_rows" in constrained
    assert consumption.get("graph_rows", 0) <= constrained.get("max_graph_rows", 0)
    assert consumption.get("exhausted_dimensions", ()) == ("graph_rows",)

    core_result = EngramCore(engine).resolve_request(
        FOUNDER_BIRTHPLACE_REQUEST,
        "composition-core-1",
        user_id="Sarah",
        namespace="public",
        configured_resolvers=("structured_graph",),
    )
    records = core_result.get("evidence_package", {}).get("records", [])
    assert core_result.get("outcome", "") == ResolutionOutcome.EVIDENCE
    assert [candidate.get("response", "") for candidate in core_result.get("response_candidates", [])] == [
        "Microsoft — founded by → born in: London."
    ]
    assert len(records[0].get("path", ())) == 2


def test_date_order_values_read_a_missing_offset_as_utc() -> None:
    before_epoch = typed_order_value("1960-01-01", ExpectedObjectType.DATE)
    epoch = typed_order_value("1970-01-01T00:00:00Z", ExpectedObjectType.DATE)

    assert before_epoch < epoch
    assert typed_order_value("2024-05-01", ExpectedObjectType.DATE) == typed_order_value(
        "2024-05-01T00:00:00Z", ExpectedObjectType.DATE
    )
