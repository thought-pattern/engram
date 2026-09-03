"""Behavior-focused Section 10 bounded graph-composition tests."""

from datetime import UTC, datetime

from pytest import fail as pytest_fail, mark as pytest_mark, raises as pytest_raises

from engram.composition import (
    composition_plan,
    composition_plan_from_json,
    composition_plan_to_dict,
    composition_plan_to_json,
    composition_step,
    execute_composition_plan,
    graph_composition_operator,
    linear_composition_plan,
    phrase_composition_result,
    resolve_composition_predicates,
)
from engram.constants import (
    CanonicalResolutionStatus,
    CompositionReason,
    ExpectedObjectType,
    GraphCompositionOperator,
    PredicateCardinality,
)
from engram.core import Engram
from engram.errors import InvalidRequestError
from engram.evidence import PropositionEligibilityEvaluator, proposition_evidence_record
from engram.fusion import EngramCandidateAuthority
from engram.graph import PropositionProjectionQuery, proposition_projection, relation_proposition_projection_from_graph_row
from engram.identity import scope_key
from engram.relation import canonical_resolution, resolve_canonical_subject
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


def internal_canonical(canonical_id: str, label: str, object_type: ExpectedObjectType):
    result = canonical_resolution(
        CanonicalResolutionStatus.SELECTED,
        canonical_id=canonical_id,
        primary_label=label,
        object_type=object_type,
        score=1.0,
        candidate_ids=(canonical_id,),
        evidence=("test_identity",),
    )
    return result


def internal_plan(operator: GraphCompositionOperator = GraphCompositionOperator.LOOKUP, final=ExpectedObjectType.PLACE):
    result = linear_composition_plan(
        internal_canonical("entity:microsoft", "Microsoft", ExpectedObjectType.ENTITY),
        (
            internal_canonical("predicate:founded-by", "founded by", ExpectedObjectType.PERSON),
            internal_canonical("predicate:born-in", "born in", final),
        ),
        operator,
        final,
        max_rows=32,
        max_candidates_per_step=4,
    )
    return result


def internal_relation(
    proposition_id: str,
    subject_id: str,
    predicate_id: str,
    object_id: str,
    object_label: str,
    object_type: ExpectedObjectType,
    *,
    cardinality: PredicateCardinality = PredicateCardinality.SINGLE,
    trust_available: bool = True,
):
    result = relation_proposition_projection_from_graph_row(
        {
            "proposition_id": proposition_id,
            "subject_entity_id": subject_id,
            "predicate_id": predicate_id,
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
            "supplied_trust": 0.8 if trust_available else 0.0,
            "supplied_trust_available": trust_available,
            "supplied_trust_version": 1 if trust_available else 0,
            "supplied_trust_version_available": trust_available,
            "structured_match": 1.0,
            "structured_match_available": True,
            "semantic_similarity": 0.0,
            "semantic_similarity_available": False,
            "object_label": object_label,
            "object_type": object_type.value,
            "predicate_cardinality": cardinality.value,
        }
    )
    return result


FOUNDER = internal_relation(
    "proposition:microsoft-founder",
    "entity:microsoft",
    "predicate:founded-by",
    "entity:founder",
    "Founder",
    ExpectedObjectType.PERSON,
)
BIRTHPLACE = internal_relation(
    "proposition:founder-born-in",
    "entity:founder",
    "predicate:born-in",
    "entity:london",
    "London",
    ExpectedObjectType.PLACE,
)


def internal_frame():
    result = QueryFrameBuilder(Engram(), lambda: 1, lambda: NOW).build(
        "Where was Microsoft's founder born?",
        scope_key(namespace="public"),
    )
    return result


def internal_current(item):
    values = dict(item["projection"])
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
    result = proposition_projection(**values)
    return result


def execute(plan=(), query=(), current_items=(FOUNDER, BIRTHPLACE)):
    selected_plan = plan or internal_plan()
    rows = {
        ("entity:microsoft", "predicate:founded-by"): [FOUNDER],
        ("entity:founder", "predicate:born-in"): [BIRTHPLACE],
    }
    selected_query = query or (lambda subject, predicate, limit: rows.get((subject, predicate), [])[:limit])
    frame = internal_frame()
    evaluator = PropositionEligibilityEvaluator()
    current = {item["projection"]["proposition_id"]: internal_current(item) for item in current_items}
    result = execute_composition_plan(
        selected_plan,
        selected_query,
        lambda projection: evaluator.evaluate(projection, frame),
        lambda projection: evaluator.revalidate(projection, frame, lambda proposition_id: (current[proposition_id],)),
    )
    return result


def test_plan_codec_carries_only_fixed_predicates_bindings_and_non_time_limits() -> None:
    plan = internal_plan()
    serialized = composition_plan_to_dict(plan)

    assert composition_plan_from_json(composition_plan_to_json(plan)) == plan
    assert [step["predicate_id"] for step in plan["steps"]] == ["predicate:founded-by", "predicate:born-in"]
    assert [step["subject_binding"] for step in plan["steps"]] == ["$root", "$hop1"]
    assert "timeout" not in serialized
    assert "cypher" not in serialized
    with pytest_raises(InvalidRequestError):
        composition_plan_to_json({**plan, "cypher": "MATCH (n) RETURN n"})


def test_plan_rejects_cartesian_and_cyclic_bindings() -> None:
    steps = list(internal_plan()["steps"])
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
        scope_key(namespace="public"),
    )
    root = internal_canonical("entity:sarah", "Sarah", ExpectedObjectType.PERSON)

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

    assert tuple(value["canonical_id"] for value in predicates) == (
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

    assert len({step["branch"] for step in plan["steps"]}) == 2
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
    location = internal_relation(
        "proposition:microsoft-location",
        "entity:microsoft",
        "predicate:located-in",
        "entity:redmond",
        "Redmond",
        ExpectedObjectType.PLACE,
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
    assert (conjunction["truth_available"], conjunction["truth_value"], conjunction["direct_result"]) == (
        True,
        True,
        True,
    )

    only_founder = {("entity:microsoft", "predicate:founded-by"): [FOUNDER]}
    failed_conjunction = run(GraphCompositionOperator.AND, only_founder)
    disjunction = run(GraphCompositionOperator.OR, only_founder)
    assert (failed_conjunction["truth_available"], failed_conjunction["truth_value"]) == (True, False)
    assert (disjunction["truth_available"], disjunction["truth_value"]) == (True, True)


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
        internal_relation(
            "proposition:founder-score-10",
            "entity:founder",
            "predicate:score",
            "number:10",
            "10",
            ExpectedObjectType.NUMBER,
            cardinality=PredicateCardinality.MULTI,
        ),
        internal_relation(
            "proposition:founder-score-2",
            "entity:founder",
            "predicate:score",
            "number:2",
            "2",
            ExpectedObjectType.NUMBER,
            cardinality=PredicateCardinality.MULTI,
        ),
    )
    plan = linear_composition_plan(
        internal_canonical("entity:microsoft", "Microsoft", ExpectedObjectType.ENTITY),
        (
            internal_canonical("predicate:founded-by", "founded by", ExpectedObjectType.PERSON),
            internal_canonical("predicate:score", "score", ExpectedObjectType.NUMBER),
        ),
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

    assert result["direct_result"] is True
    assert result["aggregate_value_available"] is True
    assert result["aggregate_value"] == expected


def test_two_hop_execution_preserves_order_and_phrases_one_complete_path() -> None:
    execution = execute()

    assert execution["direct_result"] is True
    assert execution["graph_rows"] == 4
    assert execution["terminal_entity_ids"] == ("entity:london",)
    assert tuple(entry["proposition"]["projection"]["proposition_id"] for entry in execution["complete_paths"][0]) == (
        "proposition:microsoft-founder",
        "proposition:founder-born-in",
    )
    assert phrase_composition_result(internal_plan(), execution) == "Microsoft — founded by → born in: London."


def test_cycle_and_partial_dependency_failure_never_produce_a_direct_result() -> None:
    cycle = internal_relation(
        "proposition:founder-born-in",
        "entity:founder",
        "predicate:born-in",
        "entity:microsoft",
        "Microsoft",
        ExpectedObjectType.PLACE,
    )

    def cycle_query(subject, predicate, internal_limit):
        assert internal_limit > 0
        result = [FOUNDER] if subject == "entity:microsoft" else [cycle] if predicate == "predicate:born-in" else []
        return result

    cycle_result = execute(query=cycle_query, current_items=(FOUNDER, cycle))
    assert cycle_result["direct_result"] is False
    assert CompositionReason.CYCLE in cycle_result["reasons"]

    def failed_query(subject, internal_predicate, internal_limit):
        del internal_predicate, internal_limit
        if subject == "entity:founder":
            raise RuntimeError("graph unavailable")
        return [FOUNDER]

    failed = execute(query=failed_query)
    assert failed["complete_paths"] == ()
    assert len(failed["partial_paths"]) == 1
    assert CompositionReason.DEPENDENCY_FAILED in failed["reasons"]


def test_candidate_sentinel_marks_completeness_unknown_instead_of_counting_partial_rows() -> None:
    extras = [
        internal_relation(
            f"proposition:founder-{index}",
            "entity:microsoft",
            "predicate:founded-by",
            f"entity:founder-{index}",
            f"Founder {index}",
            ExpectedObjectType.PERSON,
        )
        for index in range(5)
    ]

    def saturated_query(internal_subject, internal_predicate, limit):
        del internal_subject, internal_predicate
        result = extras[:limit]
        return result

    result = execute(query=saturated_query)

    assert result["truncated"] is True
    assert result["direct_result"] is False
    assert CompositionReason.CANDIDATE_LIMIT in result["reasons"]
    assert CompositionReason.COMPLETENESS_UNKNOWN in result["reasons"]


def test_count_refuses_unknown_or_duplicate_cardinality() -> None:
    unknown = internal_relation(
        "proposition:founder-born-in",
        "entity:founder",
        "predicate:born-in",
        "entity:london",
        "London",
        ExpectedObjectType.PLACE,
        cardinality=PredicateCardinality.UNKNOWN,
    )
    rows = {
        ("entity:microsoft", "predicate:founded-by"): [FOUNDER],
        ("entity:founder", "predicate:born-in"): [unknown],
    }
    result = execute(
        plan=internal_plan(GraphCompositionOperator.COUNT),
        query=lambda subject, predicate, limit: rows.get((subject, predicate), [])[:limit],
    )

    assert result["aggregate_value_available"] is False
    assert result["direct_result"] is False
    assert CompositionReason.CARDINALITY_UNKNOWN in result["reasons"]

    second_founder = internal_relation(
        "proposition:microsoft-founder-2",
        "entity:microsoft",
        "predicate:founded-by",
        "entity:founder-2",
        "Founder 2",
        ExpectedObjectType.PERSON,
        cardinality=PredicateCardinality.MULTI,
    )
    same_place = internal_relation(
        "proposition:founder-2-born-in",
        "entity:founder-2",
        "predicate:born-in",
        "entity:london",
        "London",
        ExpectedObjectType.PLACE,
    )
    duplicate_rows = {
        ("entity:microsoft", "predicate:founded-by"): [FOUNDER, second_founder],
        ("entity:founder", "predicate:born-in"): [BIRTHPLACE],
        ("entity:founder-2", "predicate:born-in"): [same_place],
    }
    duplicate_count = execute(
        plan=internal_plan(GraphCompositionOperator.COUNT),
        query=lambda subject, predicate, limit: duplicate_rows.get((subject, predicate), [])[:limit],
        current_items=(FOUNDER, second_founder, BIRTHPLACE, same_place),
    )
    deduplicated_lookup = execute(
        query=lambda subject, predicate, limit: duplicate_rows.get((subject, predicate), [])[:limit],
        current_items=(FOUNDER, second_founder, BIRTHPLACE, same_place),
    )

    assert duplicate_count["direct_result"] is False
    assert CompositionReason.CARDINALITY_CONFLICT in duplicate_count["reasons"]
    assert deduplicated_lookup["terminal_entity_ids"] == ("entity:london",)
    assert deduplicated_lookup["direct_result"] is True


def test_composed_evidence_path_round_trips_ordered_propositions_and_filters() -> None:
    execution = execute()
    frame = internal_frame()
    evaluator = PropositionEligibilityEvaluator()
    terminal = execution["complete_paths"][0][-1]
    decision = evaluator.revalidate(
        terminal["proposition"]["projection"],
        frame,
        lambda internal_proposition_id: (internal_current(BIRTHPLACE),),
    )
    base = proposition_evidence_record(terminal["proposition"]["projection"], decision, frame, "structured_graph")
    steps = tuple(
        proposition_evidence_path_step(
            position,
            entry["proposition"]["projection"]["proposition_id"],
            entry["proposition"]["projection"]["subject_entity_id"],
            entry["proposition"]["projection"]["predicate_id"],
            entry["proposition"]["projection"]["object_entity_id"],
            GraphCompositionOperator.LOOKUP,
            entry["step"]["subject_binding"],
            entry["step"]["object_binding"],
            ("canonical_identity", "publication_revalidation", "temporal_eligibility", "visibility"),
        )
        for position, entry in enumerate(execution["complete_paths"][0])
    )
    composed = proposition_evidence_record_with_changes(base, {"schema_version": 2, "path": steps})

    assert proposition_evidence_record_from_json(proposition_evidence_record_to_json(composed)) == composed
    package = build_evidence_package((composed,))
    assert package["wire_version"] == 2
    assert evidence_package_from_json(evidence_package_to_json(package)) == package
    assert [validate_proposition_evidence_path_step(step)["proposition_id"] for step in composed["path"]] == [
        "proposition:microsoft-founder",
        "proposition:founder-born-in",
    ]
    with pytest_raises(InvalidRequestError, match="ordered"):
        proposition_evidence_record_with_changes(base, {"schema_version": 2, "path": tuple(reversed(steps))})


def test_cooperative_cancellation_propagates_before_graph_work() -> None:
    with pytest_raises(RuntimeError, match="cancelled"):
        execute_composition_plan(
            internal_plan(),
            lambda *internal_args: pytest_fail("query must not run"),
            lambda internal_projection: pytest_fail("evaluate must not run"),
            lambda internal_projection: pytest_fail("revalidate must not run"),
            lambda: (_ for _ in ()).throw(RuntimeError("cancelled")),
        )


def test_structured_resolver_compiles_revalidates_and_publishes_two_hop_evidence(monkeypatch) -> None:
    founder = internal_relation(
        "proposition:microsoft-founder",
        "entity:microsoft",
        "predicate:founded-by",
        "entity:founder",
        "Founder",
        ExpectedObjectType.PERSON,
        cardinality=PredicateCardinality.MULTI,
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
            row = ()
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
        ):
            self.one_hop_calls.append((subject_entity_id, predicate_id, limit, include_historical))
            result = self.rows.get((subject_entity_id, predicate_id), [])[:limit]
            return result

        def proposition_projection_by_id(self, proposition_id):
            result = [
                internal_current(item)
                for items in self.rows.values()
                for item in items
                if item["projection"]["proposition_id"] == proposition_id
            ]
            return result

    graph = CompositionGraph()
    engine = Engram()
    engine.internal_graph_client = graph
    monkeypatch.setattr("engram.relation.named_entity_surfaces", lambda internal_text: ("Microsoft",))
    frame = QueryFrameBuilder(engine, lambda: 1, lambda: NOW).build(
        "Where was Microsoft's founder born?",
        scope_key(namespace="public"),
    )
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
    subject = resolve_canonical_subject(frame, engine.canonical_entity_matches)
    assert subject["status"] == CanonicalResolutionStatus.SELECTED
    assert tuple(
        value["canonical_id"] for value in resolve_composition_predicates(frame, subject, engine.canonical_predicate_matches)
    ) == ("predicate:founded-by", "predicate:born-in")
    assert graph_composition_operator(frame) == GraphCompositionOperator.LOOKUP

    result = StructuredGraphResolver(engine, lambda: 1).resolve(frame, lease)

    assert result["reason_code"] == "graph_composition_candidate"
    assert len(result["candidates"]) == 1
    assert result["candidates"][0]["response"] == "Microsoft — founded by → born in: London."
    assert len(result["proposition_evidence"]) == 1
    assert result["proposition_evidence"][0]["schema_version"] == 2
    assert tuple(
        validate_proposition_evidence_path_step(step)["proposition_id"] for step in result["proposition_evidence"][0]["path"]
    ) == (
        "proposition:microsoft-founder",
        "proposition:founder-born-in",
    )
    assert [(subject, predicate) for subject, predicate, internal_limit, historical in graph.one_hop_calls] == [
        ("entity:microsoft", "predicate:founded-by"),
        ("entity:founder", "predicate:born-in"),
    ]
    assert EngramCandidateAuthority(engine).evaluate(result["candidates"][0], frame)["answer_eligible"] is True

    constrained = resolver_budget_with_changes(lease, {"max_graph_rows": 6})
    exhausted = StructuredGraphResolver(engine, lambda: 1).resolve(frame, constrained)
    assert exhausted["reason_code"] == CompositionReason.ROW_LIMIT.value
    assert exhausted["consumption"]["graph_rows"] <= constrained["max_graph_rows"]
    assert exhausted["consumption"]["exhausted_dimensions"] == ("graph_rows",)

    core_result = EngramCore(engine).resolve_request(
        "Where was Microsoft's founder born?",
        "composition-core-1",
        user_id="Sarah",
        namespace="public",
        configured_resolvers=("structured_graph",),
    )
    assert core_result["outcome"] == ResolutionOutcome.EVIDENCE
    assert [candidate["response"] for candidate in core_result["response_candidates"]] == [
        "Microsoft — founded by → born in: London."
    ]
    assert core_result["evidence_package"]["wire_version"] == 2
    assert len(core_result["evidence_package"]["records"][0]["path"]) == 2
