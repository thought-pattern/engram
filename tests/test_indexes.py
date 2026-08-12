"""Section 2 exact, alias, support, mutation, and repair invariants."""

import json
import threading
from dataclasses import replace
from pathlib import Path
from types import MappingProxyType

import pytest

from engram import persistence
from engram.core import Engram
from engram.errors import ConflictError, InvalidRequestError
from engram.identity import RetrievalOrigin, RetrievalRepresentation, ScopedRetrievalKey, ScopeKey
from engram.indexes import (
    MAX_INDEX_REPORT_ITEMS,
    MAX_INDEX_SUPPORT_SCAN_EDGES,
    ExactLookupOutcome,
    IndexCheckCategory,
    IndexIssueReason,
    IndexOwner,
    IndexProjection,
    add_index_projection,
    build_index_state,
    check_index_state,
    check_index_state_against,
    projection_from_statement,
    remove_index_projection,
    replace_index_projection,
    update_index_support,
)
from engram.models import statement


def projection(
    statement_id: str,
    canonical: str = "",
    aliases: tuple[str, ...] = (),
    support: tuple[str, ...] = (),
    *,
    namespace: str = "",
    eligible: bool = True,
    exclusion_reason: str = "",
) -> IndexProjection:
    scope = ScopeKey(namespace=namespace)
    bindings = RetrievalRepresentation(canonical, aliases).bindings(scope) if canonical else ()
    return IndexProjection(
        statement_id=statement_id,
        generation=1,
        retrieval_keys=bindings,
        support_claim_ids=support,
        direct_answer_eligible=eligible,
        exclusion_reason=exclusion_reason,
    )


def state_signature(state) -> tuple[object, ...]:
    return (
        state.retrieval_to_owners,
        state.statement_to_retrieval,
        state.claim_to_statements,
        state.statement_to_claims,
        state.direct_retrieval,
        state.projections,
    )


def mutate_mapping(mapping, key, value) -> None:
    mapping[key] = value


def call_support_lookup(state, claim_ids):
    return state.support_lookup(claim_ids)


def test_projection_codec_round_trip_is_deterministic() -> None:
    original = projection("stmt-1", "Who acquired GitHub?", ("GitHub acquirer",), ("claim-2", "claim-1"))

    decoded = IndexProjection.from_json(original.to_json())

    assert decoded == original
    assert decoded.to_json() == original.to_json()
    assert decoded.support_claim_ids == ("claim-1", "claim-2")


def test_exact_and_alias_maps_retain_provenance_and_scope() -> None:
    first = projection("stmt-1", "Who acquired GitHub?", ("GitHub acquirer",), namespace="one")
    second = projection("stmt-2", "Who acquired GitHub?", namespace="two")
    state = build_index_state((first, second))

    canonical = ScopedRetrievalKey.build(ScopeKey(namespace="one"), "Who acquired GitHub?")
    alias = ScopedRetrievalKey.build(ScopeKey(namespace="one"), "GitHub acquirer")
    other_scope = ScopedRetrievalKey.build(ScopeKey(namespace="two"), "Who acquired GitHub?")

    assert state.exact_lookup(canonical).outcome == ExactLookupOutcome.FOUND
    assert state.exact_lookup(canonical).provenance == RetrievalOrigin.CANONICAL.value
    assert state.exact_lookup(alias).provenance == RetrievalOrigin.ALIAS.value
    assert state.exact_lookup(other_scope).statement_id == "stmt-2"
    assert set(state.statement_to_retrieval["stmt-1"]) == set(first.retrieval_keys)


def test_within_artifact_duplicates_are_reported_and_canonical_wins() -> None:
    binding = RetrievalRepresentation("same key").bindings(ScopeKey())[0]
    duplicate_alias = replace(binding, origin=RetrievalOrigin.ALIAS)
    item = replace(projection("stmt-1", "same key"), retrieval_keys=(duplicate_alias, binding))

    state = build_index_state((item,))

    reasons = {issue.reason for issue in state.build_report.issues}
    assert reasons == {IndexIssueReason.WITHIN_ARTIFACT_DUPLICATE}
    result = state.exact_lookup(binding.key)
    assert result.outcome == ExactLookupOutcome.FOUND
    assert result.provenance == RetrievalOrigin.CANONICAL.value
    assert len(state.retrieval_to_owners[binding.key]) == 1


def test_cross_artifact_collision_never_selects_a_winner() -> None:
    first = projection("stmt-a", "same key")
    second = projection("stmt-b", "same key")
    state = build_index_state((second, first))
    key = first.retrieval_keys[0].key

    result = state.exact_lookup(key)

    assert result.outcome == ExactLookupOutcome.COLLISION
    assert result.statement_id == ""
    assert result.owner_statement_ids == ("stmt-a", "stmt-b")
    assert state.build_report.collisions[0].statement_ids == ("stmt-a", "stmt-b")
    assert IndexCheckCategory.CONFLICTING in {issue.category for issue in check_index_state(state).issues}


def test_ineligible_owner_is_auditable_but_not_directly_retrievable() -> None:
    item = projection("stmt-1", "private key", eligible=False, exclusion_reason="retired")
    state = build_index_state((item,))
    key = item.retrieval_keys[0].key

    assert state.retrieval_to_owners[key][0].statement_id == "stmt-1"
    assert state.exact_lookup(key).outcome == ExactLookupOutcome.MISS
    assert key not in state.direct_retrieval


def test_support_maps_and_matched_claim_lookup_are_bidirectional() -> None:
    state = build_index_state(
        (
            projection("stmt-a", "a", support=("claim-1", "claim-2")),
            projection("stmt-b", "b", support=("claim-2", "claim-3")),
        )
    )

    result = state.support_lookup(("claim-3", "claim-2", "claim-2"))

    assert state.claim_to_statements["claim-2"] == ("stmt-a", "stmt-b")
    assert state.statement_to_claims["stmt-a"] == ("claim-1", "claim-2")
    assert result.queried_claim_ids == ("claim-2", "claim-3")
    assert result.matches[0].matched_claim_ids == ("claim-2",)
    assert result.matches[1].matched_claim_ids == ("claim-2", "claim-3")
    assert result.scanned_edge_count == 3
    assert result.complete is True
    assert result.reason == ""


def test_support_lookup_reports_output_truncation_only_after_a_complete_scan() -> None:
    projections = tuple(
        projection(f"stmt-{index:04d}", support=("claim-shared",), eligible=False, exclusion_reason="missing_identity")
        for index in range(1_001)
    )
    state = build_index_state(projections)

    result = state.support_lookup(("claim-shared",))

    assert result.complete is True
    assert result.scanned_edge_count == 1_001
    assert len(result.matches) == 1_000
    assert result.omitted_match_count == 1


def test_support_lookup_abstains_before_partial_output_when_scan_limit_is_exceeded() -> None:
    state = build_index_state(
        (
            projection("stmt-a", support=("claim-shared",)),
            projection("stmt-b", support=("claim-shared",)),
        )
    )

    incomplete = state.support_lookup(("claim-shared",), scan_limit=1)
    complete = state.support_lookup(("claim-shared",), scan_limit=2)

    assert incomplete.complete is False
    assert incomplete.matches == ()
    assert incomplete.scanned_edge_count == 0
    assert incomplete.omitted_match_count == 0
    assert incomplete.omitted_edge_count == 2
    assert incomplete.reason == "scan_limit_exceeded"
    assert complete.complete is True
    assert {match.statement_id for match in complete.matches} == {"stmt-a", "stmt-b"}


def test_builder_classifies_invalid_and_legacy_inputs_without_guessing() -> None:
    missing = projection("legacy", eligible=False, exclusion_reason=IndexIssueReason.MISSING_IDENTITY.value)
    unsupported_schema = missing.to_dict()
    unsupported_schema["statement_id"] = "schema"
    unsupported_schema["schema_version"] = 99
    unsupported_normalization = missing.to_dict()
    unsupported_normalization["statement_id"] = "normalization"
    unsupported_normalization["normalization_version"] = 99
    malformed_support = missing.to_dict()
    malformed_support["statement_id"] = "support"
    malformed_support["support_claim_ids"] = "claim-1"

    state = build_index_state((missing, unsupported_schema, unsupported_normalization, malformed_support, 42))
    reasons = {issue.reason for issue in state.build_report.issues}

    assert reasons == {
        IndexIssueReason.MISSING_IDENTITY,
        IndexIssueReason.UNSUPPORTED_SCHEMA_VERSION,
        IndexIssueReason.UNSUPPORTED_NORMALIZATION_VERSION,
        IndexIssueReason.MALFORMED_SUPPORT,
        IndexIssueReason.MALFORMED_PROJECTION,
    }
    assert not state.retrieval_to_owners


def test_classification_fixture() -> None:
    fixture_path = Path(__file__).parents[1] / "documentation" / "indexes" / "classification-v1.json"
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))

    for case in fixture["cases"]:
        state = build_index_state(case["projections"])
        key = ScopedRetrievalKey.build(
            ScopeKey(namespace="test", context_fingerprint="fixture-v1"),
            case["lookup_representation"],
        )
        reasons = {issue.reason.value for issue in state.build_report.issues}
        assert reasons == set(case["expected_issue_reasons"]), case["name"]
        assert len(state.build_report.collisions) == case["expected_collision_count"], case["name"]
        assert state.exact_lookup(key).outcome == ExactLookupOutcome(case["expected_direct_lookup"]), case["name"]


def test_malformed_current_support_is_explicitly_excluded() -> None:
    item = projection_from_statement({"id": "legacy", "template": {"tapestry": {"support": "claim-1"}}})
    state = build_index_state((item,))

    assert item.exclusion_reason == IndexIssueReason.MALFORMED_SUPPORT.value
    assert IndexIssueReason.MALFORMED_SUPPORT in {issue.reason for issue in state.build_report.issues}


def test_reports_are_bounded() -> None:
    state = build_index_state(tuple(42 for _ in range(MAX_INDEX_REPORT_ITEMS + 17)))

    assert len(state.build_report.issues) == MAX_INDEX_REPORT_ITEMS
    assert state.build_report.omitted_issue_count == 17


def test_checker_reports_corruption_and_expected_safe_exclusions() -> None:
    active = projection("active", "key", support=("claim-1",))
    legacy = projection("legacy", eligible=False, exclusion_reason=IndexIssueReason.MISSING_IDENTITY.value)
    state = build_index_state((active, legacy))
    corrupt = replace(
        state,
        claim_to_statements=MappingProxyType({"claim-extra": ("active",)}),
        statement_to_claims=MappingProxyType({"active": ("claim-wrong",), "legacy": ()}),
        direct_retrieval=MappingProxyType({}),
    )

    report = check_index_state(corrupt)
    categories = {issue.category for issue in report.issues}

    assert report.consistent is False
    assert IndexCheckCategory.MISSING in categories
    assert IndexCheckCategory.EXTRA in categories
    assert IndexCheckCategory.ASYMMETRIC in categories
    assert IndexCheckCategory.UNINDEXABLE in categories
    assert IndexCheckCategory.INELIGIBLE in categories


def test_explicit_empty_projection_set_is_not_self_check_fallback() -> None:
    item = projection("stmt-a", "a", support=("claim-a",))
    state = build_index_state((item,))

    report = check_index_state_against(state, ())

    assert report.consistent is False
    assert IndexCheckCategory.EXTRA in {issue.category for issue in report.issues if issue.error}


def test_empty_projection_repair_is_dry_run_safe_and_atomically_clears() -> None:
    item = projection("stmt-a", "a", support=("claim-a",))
    owner = IndexOwner((item,))
    before = owner.snapshot()

    dry_run = owner.repair((), dry_run=True)

    assert dry_run.changed is True
    assert dry_run.live_check.consistent is False
    assert owner.snapshot() is before

    applied = owner.repair((), dry_run=False)

    assert applied.applied is True
    assert owner.snapshot().projections == {}
    assert owner.snapshot().retrieval_to_owners == {}
    assert owner.snapshot().claim_to_statements == {}


def test_checker_and_atomic_swap_reject_corrupt_build_report() -> None:
    item = projection("stmt-a", "a", support=("claim-a",))
    owner = IndexOwner((item,))
    before = owner.snapshot()
    candidate = build_index_state((item,), before.state_generation + 1)
    corrupt = replace(candidate, build_report=replace(candidate.build_report, exact_key_count=999))

    report = check_index_state(corrupt)

    assert report.consistent is False
    assert any(issue.index_name == "build_report" and issue.key == "exact_key_count" for issue in report.issues)
    with pytest.raises(ConflictError, match="failed consistency checking"):
        owner.atomic_swap(corrupt, before.state_generation)
    assert owner.snapshot() is before


def test_checker_rejects_missing_collision_diagnostics() -> None:
    first = projection("stmt-a", "same key")
    second = projection("stmt-b", "same key")
    state = build_index_state((first, second))
    corrupt = replace(state, build_report=replace(state.build_report, collisions=()))

    report = check_index_state(corrupt)

    assert report.consistent is False
    assert any(issue.index_name == "build_report" and issue.key == "collisions" for issue in report.issues)


def test_checker_rejects_each_reproducible_report_field_mismatch() -> None:
    active = projection("stmt-a", "same key", support=("claim-a",))
    collision = projection("stmt-b", "same key")
    legacy = projection("legacy", eligible=False, exclusion_reason=IndexIssueReason.MISSING_IDENTITY.value)
    state = build_index_state((active, collision, legacy))
    report = state.build_report
    corrupt_reports = (
        replace(report, projection_count=report.projection_count + 1),
        replace(report, exact_key_count=report.exact_key_count + 1),
        replace(report, support_edge_count=report.support_edge_count + 1),
        replace(report, issues=()),
        replace(report, collisions=()),
        replace(report, omitted_issue_count=report.omitted_issue_count + 1),
        replace(report, omitted_collision_count=report.omitted_collision_count + 1),
    )

    for corrupt_report in corrupt_reports:
        check = check_index_state(replace(state, build_report=corrupt_report))
        assert check.consistent is False
        assert any(issue.index_name == "build_report" and issue.error for issue in check.issues)


def test_input_only_build_classifications_do_not_make_valid_state_unpublishable() -> None:
    owner = IndexOwner((projection("stmt-a", "a"), 42))

    assert owner.snapshot().build_report.issues[0].input_only is True
    assert owner.check().consistent is True


def test_omitted_input_only_classifications_do_not_make_valid_state_unpublishable() -> None:
    owner = IndexOwner((projection("stmt-a", "a"), *(42 for _ in range(MAX_INDEX_REPORT_ITEMS + 1))))

    assert owner.snapshot().build_report.omitted_issue_count == 1
    assert owner.check().consistent is True


def test_index_state_maps_are_immutable() -> None:
    state = build_index_state((projection("stmt-1", "key"),))

    with pytest.raises(TypeError):
        mutate_mapping(state.claim_to_statements, "claim-1", ("stmt-1",))


def test_generic_mutations_equal_clean_rebuild() -> None:
    first = projection("stmt-a", "a", support=("claim-a",))
    second = projection("stmt-b", "b", support=("claim-b",))
    replacement = projection("stmt-a", "new a", support=("claim-c",))
    state = build_index_state((first,))

    added = add_index_projection(state, second)
    assert state_signature(added) == state_signature(build_index_state((first, second), added.state_generation))

    replaced = replace_index_projection(added, replacement)
    assert state_signature(replaced) == state_signature(build_index_state((replacement, second), replaced.state_generation))

    supported = update_index_support(replaced, "stmt-b", ("claim-d",))
    expected_second = replace(second, generation=2, support_claim_ids=("claim-d",))
    assert state_signature(supported) == state_signature(
        build_index_state((replacement, expected_second), supported.state_generation)
    )

    removed = remove_index_projection(supported, "stmt-a")
    assert state_signature(removed) == state_signature(build_index_state((expected_second,), removed.state_generation))


def test_atomic_owner_rejects_stale_swap_and_abandoned_build_is_invisible() -> None:
    owner = IndexOwner((projection("stmt-a", "a"),))
    before = owner.snapshot()
    abandoned = build_index_state((projection("stmt-b", "b"),), before.state_generation + 1)

    assert owner.snapshot() is before
    owner.add(projection("stmt-b", "b"))
    with pytest.raises(ConflictError, match="stale index state generation"):
        owner.atomic_swap(abandoned, before.state_generation)


def test_checked_repair_has_bounded_dry_run_and_atomic_apply() -> None:
    expected = (projection("stmt-a", "a", support=("claim-a",)),)
    owner = IndexOwner(expected)
    original = owner.snapshot()
    owner._state = replace(original, claim_to_statements=MappingProxyType({}))

    dry_run = owner.repair(expected, dry_run=True)
    assert dry_run.applied is False
    assert dry_run.changed is True
    assert owner.snapshot().claim_to_statements == {}

    applied = owner.repair(expected, dry_run=False)
    assert applied.applied is True
    assert owner.snapshot().claim_to_statements == {"claim-a": ("stmt-a",)}
    assert check_index_state(owner.snapshot()).consistent is True


def test_concurrent_readers_observe_only_complete_checked_states() -> None:
    base = projection("base", "base", support=("claim-base",))
    changing = projection("changing", "changing", support=("claim-changing",))
    owner = IndexOwner((base,))
    start = threading.Barrier(3)
    errors = []

    def writer() -> None:
        start.wait()
        for _ in range(30):
            owner.add(changing)
            owner.remove(changing.statement_id)

    def reader() -> None:
        start.wait()
        for _ in range(100):
            snapshot = owner.snapshot()
            if not check_index_state(snapshot).consistent:
                errors.append(snapshot.state_generation)

    writer_thread = threading.Thread(target=writer)
    first_reader = threading.Thread(target=reader)
    second_reader = threading.Thread(target=reader)
    writer_thread.start()
    first_reader.start()
    second_reader.start()
    writer_thread.join()
    first_reader.join()
    second_reader.join()

    assert errors == []


def test_current_statement_support_is_indexed_but_exact_identity_is_not_invented() -> None:
    engram = Engram()
    statement_id = engram.store(
        "A response",
        template={"tapestry": {"support": [{"claim_id": "claim-1"}]}},
        keyword_source="question words",
    )
    state = engram.index_snapshot()

    assert state.claim_to_statements == {"claim-1": (statement_id,)}
    assert state.statement_to_retrieval[statement_id] == ()
    assert IndexIssueReason.MISSING_IDENTITY in {issue.reason for issue in state.build_report.issues}


def test_current_support_metadata_updates_and_eviction_update_both_maps() -> None:
    engram = Engram()
    statement_id = engram.learn_from_response(
        "question",
        "first",
        template={"tapestry": {"support": [{"claim_id": "claim-1"}]}},
    )
    same_id = engram.learn_from_response(
        "question",
        "second",
        template={"tapestry": {"support": [{"claim_id": "claim-2"}]}},
    )

    assert same_id == statement_id
    assert engram.index_snapshot().claim_to_statements == {"claim-2": (statement_id,)}

    engram.retire_statement(statement_id)
    assert engram.index_snapshot().claim_to_statements == {}
    assert engram.index_snapshot().statement_to_claims == {}


def test_current_support_metadata_rebuilds_after_persistence_load() -> None:
    engram = Engram()
    statement_id = engram.store(
        "A persisted response",
        template={"tapestry": {"support": [{"claim_id": "claim-persisted"}]}},
    )

    loaded = persistence.load_engram_json(persistence.save_json(engram))

    assert loaded.index_snapshot().claim_to_statements == {"claim-persisted": (statement_id,)}
    assert loaded.index_snapshot().statement_to_claims == {statement_id: ("claim-persisted",)}


def test_vector_support_path_does_not_iterate_statement_corpus() -> None:
    class NoIterationList(list):
        def __iter__(self):
            raise AssertionError("vector support retrieval scanned the statement corpus")

    engram = Engram()
    statement_id = engram.store(
        "A response",
        priority=3,
        template={"tapestry": {"support": [{"claim_id": "claim-1"}]}},
    )
    engram.statements = NoIterationList(engram.statements)
    engram.config["graph"] = {"vector_weight": 0.65}
    engram.graph_vector_claims = lambda text, *, limit=0: [{"claim_id": "claim-1", "similarity": 0.8}]

    matches = engram.vector_supported_matches("query", limit=1)

    assert matches[0][0]["id"] == statement_id
    assert matches[0][1] == pytest.approx(3.52)


def test_vector_support_filters_scope_before_top_k_across_more_than_lookup_output_limit() -> None:
    engram = Engram()
    statements = []
    for index in range(1_001):
        namespace = "target" if index == 1_000 else "other"
        statements.append(
            statement(
                f"Response {index}",
                statement_id=f"stmt-{index:04d}",
                priority=100 if index == 1_000 else 0,
                template={
                    "tapestry": {
                        "namespace": namespace,
                        "context_fingerprint": "scope-v1",
                        "support": [{"claim_id": "claim-shared"}],
                    }
                },
            )
        )
    engram.statements = statements
    engram.statement_index = {item["id"]: index for index, item in enumerate(statements)}
    engram._index_owner = IndexOwner(tuple(projection_from_statement(item) for item in statements))
    engram.config["graph"] = {"vector_weight": 1.0, "vector_support_scan_limit": 2_000}
    engram.graph_vector_claims = lambda text, *, limit=0: [{"claim_id": "claim-shared", "similarity": 1.0}]

    matches = engram.vector_supported_matches(
        "query",
        limit=1,
        statement_filter=lambda item: item["template"]["tapestry"]["namespace"] == "target",
    )

    assert len(matches) == 1
    assert matches[0][0]["id"] == "stmt-1000"
    assert matches[0][1] == pytest.approx(101.0)


def test_vector_support_scan_exhaustion_abstains_without_partial_ranking(caplog) -> None:
    engram = Engram()
    first = statement(
        "First",
        statement_id="stmt-a",
        priority=100,
        template={"tapestry": {"support": [{"claim_id": "claim-shared"}]}},
    )
    second = statement(
        "Second",
        statement_id="stmt-b",
        template={"tapestry": {"support": [{"claim_id": "claim-shared"}]}},
    )
    engram.statements = [first, second]
    engram.statement_index = {"stmt-a": 0, "stmt-b": 1}
    engram._index_owner = IndexOwner((projection_from_statement(first), projection_from_statement(second)))
    engram.config["graph"] = {"vector_weight": 1.0, "vector_support_scan_limit": 1}
    engram.graph_vector_claims = lambda text, *, limit=0: [{"claim_id": "claim-shared", "similarity": 1.0}]

    matches = engram.vector_supported_matches("query", limit=1)

    assert matches == []
    assert "vector_support_scan_incomplete" in caplog.text


def test_vector_support_deduplicates_multi_claim_paths_and_keeps_score_top_k() -> None:
    engram = Engram()
    first = statement(
        "First",
        statement_id="stmt-a",
        template={"tapestry": {"support": [{"claim_id": "claim-low"}, {"claim_id": "claim-high"}]}},
    )
    second = statement(
        "Second",
        statement_id="stmt-b",
        template={"tapestry": {"support": [{"claim_id": "claim-middle"}]}},
    )
    engram.statements = [first, second]
    engram.statement_index = {"stmt-a": 0, "stmt-b": 1}
    engram._index_owner = IndexOwner((projection_from_statement(first), projection_from_statement(second)))
    engram.config["graph"] = {"vector_weight": 1.0, "vector_support_scan_limit": 10}
    engram.graph_vector_claims = lambda text, *, limit=0: [
        {"claim_id": "claim-low", "similarity": 0.1},
        {"claim_id": "claim-middle", "similarity": 0.5},
        {"claim_id": "claim-high", "similarity": 0.9},
    ]

    matches = engram.vector_supported_matches("query", limit=1)

    assert len(matches) == 1
    assert matches[0][0]["id"] == "stmt-a"
    assert matches[0][1] == pytest.approx(0.9)


def test_core_explicit_empty_projection_operations_preserve_authoritative_statements() -> None:
    engram = Engram()
    statement_id = engram.store(
        "A response",
        template={"tapestry": {"support": [{"claim_id": "claim-1"}]}},
    )

    assert engram.check_index_projections(()).consistent is False
    dry_run = engram.repair_index_projections((), dry_run=True)
    applied = engram.repair_index_projections((), dry_run=False)

    assert dry_run.changed is True
    assert applied.applied is True
    assert engram.get_statement(statement_id)["text"] == "A response"
    assert engram.index_snapshot().projections == {}
    assert engram.check_indexes().consistent is False

    restored = engram.repair_indexes(dry_run=False)
    assert restored.applied is True
    assert engram.check_indexes().consistent is True


def test_engram_repair_does_not_change_authoritative_statement_content() -> None:
    engram = Engram()
    engram.store("A response", template={"tapestry": {"support": [{"claim_id": "claim-1"}]}})
    before = [dict(statement) for statement in engram.statements]
    state = engram.index_snapshot()
    engram._index_owner._state = replace(state, claim_to_statements=MappingProxyType({}))

    dry_run = engram.repair_indexes(dry_run=True)
    repaired = engram.repair_indexes(dry_run=False)

    assert dry_run.applied is False
    assert repaired.applied is True
    assert engram.statements == before
    assert engram.check_indexes().consistent is True


def test_generic_mutation_validation_is_strict() -> None:
    state = build_index_state(())
    with pytest.raises(InvalidRequestError):
        call_support_lookup(state, ["claim-1"])
    with pytest.raises(InvalidRequestError):
        remove_index_projection(state, "missing")
    with pytest.raises(InvalidRequestError):
        state.support_lookup(("claim-1",), scan_limit=0)


def test_default_support_scan_bound_is_explicit() -> None:
    assert MAX_INDEX_SUPPORT_SCAN_EDGES == 100_000
