"""Section 2 exact, alias, support, mutation, and repair invariants."""

import json
import threading
from pathlib import Path
from types import MappingProxyType

import pytest

from engram import persistence
from engram.core import Engram
from engram.errors import ConflictError, InvalidRequestError
from engram.identity import (
    RetrievalOrigin,
    build_scoped_retrieval_key,
    retrieval_key_binding,
    retrieval_representation,
    retrieval_representation_bindings,
    scope_key,
    scoped_retrieval_key_signature,
)
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
    exact_lookup_result_to_dict,
    index_build_report_to_dict,
    index_check_report_to_dict,
    index_projection,
    index_projection_from_json,
    index_projection_to_dict,
    index_projection_to_json,
    index_projection_with_changes,
    index_repair_result_to_dict,
    index_state_exact_lookup,
    index_state_support_lookup,
    index_state_support_scan_plan,
    projection_from_statement,
    remove_index_projection,
    replace_index_projection,
    retrieval_owner_to_dict,
    support_lookup_result_to_dict,
    support_scan_plan_to_dict,
    update_index_support,
    validate_exact_lookup_result,
    validate_index_build_report,
    validate_index_check_report,
    validate_index_projection,
    validate_index_repair_result,
    validate_index_state,
    validate_retrieval_owner,
    validate_support_lookup_result,
    validate_support_scan_plan,
)
from engram.models import statement
from .support_fixtures import (
    ASSERTION_REFERENCE_A,
    ASSERTION_REFERENCE_B,
    ASSERTION_REFERENCE_C,
    ASSERTION_REFERENCE_D,
    PROPOSITION_REFERENCE_A,
    PROPOSITION_REFERENCE_B,
    PROPOSITION_REFERENCE_C,
    REFERENCE_IDS,
)


def projection(
    statement_id: str,
    canonical: str = "",
    aliases: tuple[str, ...] = (),
    support: tuple[dict, ...] = (),
    *,
    namespace: str = "",
    eligible: bool = True,
    exclusion_reason: str = "",
) -> IndexProjection:
    scope = scope_key(namespace=namespace)
    representation = retrieval_representation(canonical, aliases) if canonical else {}
    bindings = retrieval_representation_bindings(representation, scope) if representation else ()
    result = index_projection(
        statement_id,
        1,
        bindings,
        support,
        eligible,
        exclusion_reason,
    )
    return result


def state_signature(state) -> tuple[object, ...]:
    result = (
        state["retrieval_to_owners"],
        state["statement_to_retrieval"],
        state["record_to_statements"],
        state["statement_to_references"],
        state["direct_retrieval"],
        state["projections"],
    )
    return result


def mutate_mapping(mapping, key, value) -> None:
    mapping[key] = value


def call_support_lookup(state, record_ids):
    result = index_state_support_lookup(state, record_ids)
    return result


def test_projection_codec_round_trip_is_deterministic() -> None:
    original = projection(
        "stmt-1",
        "Who acquired GitHub?",
        ("GitHub acquirer",),
        (ASSERTION_REFERENCE_B, ASSERTION_REFERENCE_A),
    )

    encoded = index_projection_to_json(original)
    decoded = index_projection_from_json(encoded)
    copied = validate_index_projection(original)

    assert type(original) is dict
    assert decoded == original
    assert index_projection_to_json(decoded) == encoded
    assert decoded["support_references"] == (
        ASSERTION_REFERENCE_B,
        ASSERTION_REFERENCE_A,
    )
    assert copied == original
    assert copied is not original
    assert copied["retrieval_keys"][0] is not original["retrieval_keys"][0]

    original["generation"] = 0
    with pytest.raises(InvalidRequestError, match="positive integer"):
        validate_index_projection(original)


def test_atomic_exact_refresh_changes_only_eligibility_fields() -> None:
    original = projection("stmt-1", "Who acquired GitHub?")
    owner = IndexOwner((original,))
    key = build_scoped_retrieval_key(scope_key(), "Who acquired GitHub?")
    generation = owner.snapshot()["state_generation"]
    ineligible = index_projection_with_changes(
        original,
        {"direct_answer_eligible": False, "exclusion_reason": "expired"},
    )

    lookup, changed, updated_generation = owner.atomic_refresh_exact_lookup(key, (ineligible,), generation)

    assert lookup["outcome"] == ExactLookupOutcome.MISS
    assert changed is True
    assert updated_generation == generation + 1
    assert owner.snapshot()["projections"]["stmt-1"] == ineligible


def test_atomic_exact_refresh_noop_keeps_generation() -> None:
    original = projection("stmt-1", "Who acquired GitHub?")
    owner = IndexOwner((original,))
    key = build_scoped_retrieval_key(scope_key(), "Who acquired GitHub?")
    generation = owner.snapshot()["state_generation"]

    lookup, changed, updated_generation = owner.atomic_refresh_exact_lookup(key, (original,), generation)

    assert lookup["outcome"] == ExactLookupOutcome.FOUND
    assert changed is False
    assert updated_generation == generation


def test_atomic_exact_refresh_requires_all_and_only_current_owners() -> None:
    first = projection("stmt-1", "Who acquired GitHub?")
    second = projection("stmt-2", "Who acquired GitHub?", eligible=False, exclusion_reason="retired")
    owner = IndexOwner((first, second))
    key = build_scoped_retrieval_key(scope_key(), "Who acquired GitHub?")

    with pytest.raises(ConflictError, match="cover every current owner"):
        owner.atomic_refresh_exact_lookup(key, (first,), owner.snapshot()["state_generation"])
    with pytest.raises(InvalidRequestError, match="unique statement IDs"):
        owner.atomic_refresh_exact_lookup(key, (first, first), owner.snapshot()["state_generation"])


def test_atomic_exact_refresh_rejects_non_eligibility_changes_and_stale_generation() -> None:
    original = projection("stmt-1", "Who acquired GitHub?")
    owner = IndexOwner((original,))
    key = build_scoped_retrieval_key(scope_key(), "Who acquired GitHub?")

    changed_support = index_projection_with_changes(
        original, {"support_references": (ASSERTION_REFERENCE_D,)}
    )
    with pytest.raises(InvalidRequestError, match="may change only"):
        owner.atomic_refresh_exact_lookup(key, (changed_support,), owner.snapshot()["state_generation"])
    with pytest.raises(ConflictError, match="stale index state generation"):
        owner.atomic_refresh_exact_lookup(key, (original,), owner.snapshot()["state_generation"] + 1)


def test_exact_and_alias_maps_retain_provenance_and_scope() -> None:
    first = projection("stmt-1", "Who acquired GitHub?", ("GitHub acquirer",), namespace="one")
    second = projection("stmt-2", "Who acquired GitHub?", namespace="two")
    state = build_index_state((first, second))

    canonical = build_scoped_retrieval_key(scope_key(namespace="one"), "Who acquired GitHub?")
    alias = build_scoped_retrieval_key(scope_key(namespace="one"), "GitHub acquirer")
    other_scope = build_scoped_retrieval_key(scope_key(namespace="two"), "Who acquired GitHub?")

    assert index_state_exact_lookup(state, canonical)["outcome"] == ExactLookupOutcome.FOUND
    assert index_state_exact_lookup(state, canonical)["provenance"] == RetrievalOrigin.CANONICAL.value
    assert index_state_exact_lookup(state, alias)["provenance"] == RetrievalOrigin.ALIAS.value
    assert index_state_exact_lookup(state, other_scope)["statement_id"] == "stmt-2"
    retained = {
        (scoped_retrieval_key_signature(binding["key"]), binding["origin"], binding["representation"])
        for binding in state["statement_to_retrieval"]["stmt-1"]
    }
    expected = {
        (scoped_retrieval_key_signature(binding["key"]), binding["origin"], binding["representation"])
        for binding in first["retrieval_keys"]
    }
    assert retained == expected


def test_exact_lookup_result_is_an_exact_validated_non_aliasing_dictionary() -> None:
    item = projection("stmt-1", "Who acquired GitHub?")
    key = item["retrieval_keys"][0]["key"]
    lookup = index_state_exact_lookup(build_index_state((item,)), key)

    assert type(lookup) is dict
    assert type(lookup["key"]) is dict
    assert exact_lookup_result_to_dict(lookup) == {
        "outcome": "FOUND",
        "key": {
            "schema_version": 1,
            "normalization_version": 1,
            "scope": {"schema_version": 1, "namespace": "", "context_fingerprint": ""},
            "normalized_key": "who acquired github",
        },
        "statement_id": "stmt-1",
        "generation": 1,
        "provenance": "canonical",
        "representation": "Who acquired GitHub?",
        "owner_statement_ids": ["stmt-1"],
        "truncated": False,
    }
    copied = validate_exact_lookup_result(lookup)
    assert copied == lookup
    assert copied is not lookup
    assert copied["key"] is not lookup["key"]

    lookup["outcome"] = ExactLookupOutcome.MISS
    with pytest.raises(InvalidRequestError, match="must be 0"):
        validate_exact_lookup_result(lookup)


def test_retrieval_owner_is_validated_and_snapshot_mutation_is_isolated() -> None:
    item = projection("stmt-1", "Who acquired GitHub?")
    index_owner = IndexOwner((item,))
    snapshot = index_owner.snapshot()
    owner = next(iter(snapshot["retrieval_to_owners"].values()))[0]

    assert type(owner) is dict
    assert retrieval_owner_to_dict(owner) == {
        "statement_id": "stmt-1",
        "generation": 1,
        "provenance": "canonical",
        "representation": "Who acquired GitHub?",
        "direct_answer_eligible": True,
    }
    copied = validate_retrieval_owner(owner)
    assert copied == owner
    assert copied is not owner

    owner["generation"] = 0
    with pytest.raises(InvalidRequestError, match="positive integer"):
        validate_retrieval_owner(owner)
    current_owner = next(iter(index_owner.snapshot()["retrieval_to_owners"].values()))[0]
    assert current_owner["generation"] == 1


def test_within_artifact_duplicates_are_reported_and_canonical_wins() -> None:
    representation = retrieval_representation("same key")
    binding = retrieval_representation_bindings(representation, scope_key())[0]
    duplicate_alias = retrieval_key_binding(binding["key"], RetrievalOrigin.ALIAS, binding["representation"])
    item = index_projection_with_changes(
        projection("stmt-1", "same key"),
        {"retrieval_keys": (duplicate_alias, binding)},
    )

    state = build_index_state((item,))

    reasons = {issue["reason"] for issue in state["build_report"]["issues"]}
    assert reasons == {IndexIssueReason.WITHIN_ARTIFACT_DUPLICATE}
    result = index_state_exact_lookup(state, binding["key"])
    assert result["outcome"] == ExactLookupOutcome.FOUND
    assert result["provenance"] == RetrievalOrigin.CANONICAL.value
    key_signature = scoped_retrieval_key_signature(binding["key"])
    assert len(state["retrieval_to_owners"][key_signature]) == 1


def test_cross_artifact_collision_never_selects_a_winner() -> None:
    first = projection("stmt-a", "same key")
    second = projection("stmt-b", "same key")
    state = build_index_state((second, first))
    key = first["retrieval_keys"][0]["key"]

    result = index_state_exact_lookup(state, key)

    assert result["outcome"] == ExactLookupOutcome.COLLISION
    assert result["statement_id"] == ""
    assert result["owner_statement_ids"] == ("stmt-a", "stmt-b")
    assert state["build_report"]["collisions"][0]["statement_ids"] == ("stmt-a", "stmt-b")
    assert IndexCheckCategory.CONFLICTING in {issue["category"] for issue in check_index_state(state)["issues"]}


def test_ineligible_owner_is_auditable_but_not_directly_retrievable() -> None:
    item = projection("stmt-1", "private key", eligible=False, exclusion_reason="retired")
    state = build_index_state((item,))
    key = item["retrieval_keys"][0]["key"]
    key_signature = scoped_retrieval_key_signature(key)

    assert state["retrieval_to_owners"][key_signature][0]["statement_id"] == "stmt-1"
    assert index_state_exact_lookup(state, key)["outcome"] == ExactLookupOutcome.MISS
    assert key_signature not in state["direct_retrieval"]


def test_support_maps_and_matched_record_lookup_are_bidirectional() -> None:
    state = build_index_state(
        (
            projection("stmt-a", "a", support=(ASSERTION_REFERENCE_A, ASSERTION_REFERENCE_B)),
            projection("stmt-b", "b", support=(ASSERTION_REFERENCE_B, ASSERTION_REFERENCE_C)),
        )
    )

    result = index_state_support_lookup(
        state,
        (
            REFERENCE_IDS.get("c", ""),
            REFERENCE_IDS.get("b", ""),
            REFERENCE_IDS.get("b", ""),
        ),
    )

    assert state["record_to_statements"][REFERENCE_IDS.get("b", "")] == (
        "stmt-a",
        "stmt-b",
    )
    assert state["statement_to_references"]["stmt-a"] == (
        ASSERTION_REFERENCE_A,
        ASSERTION_REFERENCE_B,
    )
    assert result["queried_record_ids"] == (
        REFERENCE_IDS.get("b", ""),
        REFERENCE_IDS.get("c", ""),
    )
    assert result["matches"][0]["matched_record_ids"] == (
        REFERENCE_IDS.get("b", ""),
    )
    assert result["matches"][1]["matched_record_ids"] == (
        REFERENCE_IDS.get("b", ""),
        REFERENCE_IDS.get("c", ""),
    )
    assert result["scanned_edge_count"] == 3
    assert result["complete"] is True
    assert result["reason"] == ""


def test_support_lookup_reports_output_truncation_only_after_a_complete_scan() -> None:
    projections = tuple(
        projection(f"stmt-{index:04d}", support=(ASSERTION_REFERENCE_D,), eligible=False, exclusion_reason="missing_identity")
        for index in range(1_001)
    )
    state = build_index_state(projections)

    result = index_state_support_lookup(state, (REFERENCE_IDS.get("d", ""),))

    assert result["complete"] is True
    assert result["scanned_edge_count"] == 1_001
    assert len(result["matches"]) == 1_000
    assert result["omitted_match_count"] == 1


def test_support_lookup_abstains_before_partial_output_when_scan_limit_is_exceeded() -> None:
    state = build_index_state(
        (
            projection("stmt-a", support=(ASSERTION_REFERENCE_D,)),
            projection("stmt-b", support=(ASSERTION_REFERENCE_D,)),
        )
    )

    incomplete = index_state_support_lookup(state, (REFERENCE_IDS.get("d", ""),), scan_limit=1)
    complete = index_state_support_lookup(state, (REFERENCE_IDS.get("d", ""),), scan_limit=2)

    assert incomplete["complete"] is False
    assert incomplete["matches"] == ()
    assert incomplete["scanned_edge_count"] == 0
    assert incomplete["omitted_match_count"] == 0
    assert incomplete["omitted_edge_count"] == 2
    assert incomplete["reason"] == "scan_limit_exceeded"
    assert complete["complete"] is True
    assert {match["statement_id"] for match in complete["matches"]} == {"stmt-a", "stmt-b"}


def test_support_records_are_exact_validated_non_aliasing_dictionaries() -> None:
    state = build_index_state((projection("stmt-a", support=(ASSERTION_REFERENCE_A,)),))
    plan = index_state_support_scan_plan(state, (REFERENCE_IDS.get("a", ""),))
    lookup = index_state_support_lookup(state, (REFERENCE_IDS.get("a", ""),))

    assert type(plan) is dict
    assert type(lookup) is dict
    assert type(lookup["matches"][0]) is dict
    assert support_scan_plan_to_dict(plan) == {
        "queried_record_ids": [REFERENCE_IDS.get("a", "")],
        "edge_count": 1,
        "scan_limit": MAX_INDEX_SUPPORT_SCAN_EDGES,
        "complete": True,
        "reason": "",
    }
    assert support_lookup_result_to_dict(lookup)["matches"] == [
        {
            "statement_id": "stmt-a",
            "matched_record_ids": [REFERENCE_IDS.get("a", "")],
        }
    ]
    assert validate_support_scan_plan(plan) is not plan
    assert validate_support_lookup_result(lookup) is not lookup

    lookup["matches"][0]["matched_record_ids"] = ()
    with pytest.raises(InvalidRequestError, match="must not be empty"):
        validate_support_lookup_result(lookup)

    malformed_plan = dict(plan)
    malformed_plan["unexpected"] = True
    with pytest.raises(InvalidRequestError, match="invalid fields"):
        validate_support_scan_plan(malformed_plan)


def test_builder_classifies_invalid_and_legacy_inputs_without_guessing() -> None:
    missing = projection("legacy", eligible=False, exclusion_reason=IndexIssueReason.MISSING_IDENTITY.value)
    unsupported_schema = index_projection_to_dict(missing)
    unsupported_schema["statement_id"] = "schema"
    unsupported_schema["schema_version"] = 99
    unsupported_normalization = index_projection_to_dict(missing)
    unsupported_normalization["statement_id"] = "normalization"
    unsupported_normalization["normalization_version"] = 99
    malformed_support = index_projection_to_dict(missing)
    malformed_support["statement_id"] = "support"
    malformed_support["support_references"] = "not-a-reference-vector"

    state = build_index_state((missing, unsupported_schema, unsupported_normalization, malformed_support, 42))
    reasons = {issue["reason"] for issue in state["build_report"]["issues"]}

    assert reasons == {
        IndexIssueReason.MISSING_IDENTITY,
        IndexIssueReason.UNSUPPORTED_SCHEMA_VERSION,
        IndexIssueReason.UNSUPPORTED_NORMALIZATION_VERSION,
        IndexIssueReason.MALFORMED_SUPPORT,
        IndexIssueReason.MALFORMED_PROJECTION,
    }
    assert not state["retrieval_to_owners"]


def test_classification_fixture() -> None:
    fixture_path = Path(__file__).parent / "fixtures" / "indexes" / "classification-v1.json"
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))

    for case in fixture["cases"]:
        state = build_index_state(case["projections"])
        key = build_scoped_retrieval_key(
            scope_key(namespace="test", context_fingerprint="fixture-v1"),
            case["lookup_representation"],
        )
        reasons = {issue["reason"].value for issue in state["build_report"]["issues"]}
        assert reasons == set(case["expected_issue_reasons"]), case["name"]
        assert len(state["build_report"]["collisions"]) == case["expected_collision_count"], case["name"]
        assert index_state_exact_lookup(state, key)["outcome"] == ExactLookupOutcome(case["expected_direct_lookup"]), case["name"]


def test_malformed_current_support_is_explicitly_excluded() -> None:
    item = projection_from_statement(
        {"id": "malformed", "template": {"tapestry": {"support": "not-an-array"}}}
    )
    state = build_index_state((item,))

    assert item["exclusion_reason"] == IndexIssueReason.MALFORMED_SUPPORT.value
    assert IndexIssueReason.MALFORMED_SUPPORT in {issue["reason"] for issue in state["build_report"]["issues"]}


def test_reports_are_bounded() -> None:
    state = build_index_state(tuple(42 for _ in range(MAX_INDEX_REPORT_ITEMS + 17)))

    assert len(state["build_report"]["issues"]) == MAX_INDEX_REPORT_ITEMS
    assert state["build_report"]["omitted_issue_count"] == 17


def test_index_reports_are_exact_validated_non_aliasing_dictionaries() -> None:
    state = build_index_state((projection("stmt-a", "same"), projection("stmt-b", "same"), 42))
    build_report = state["build_report"]
    check_report = check_index_state(state)

    assert type(build_report) is dict
    assert type(build_report["issues"][0]) is dict
    assert type(build_report["collisions"][0]) is dict
    assert type(check_report) is dict
    assert type(check_report["issues"][0]) is dict
    assert index_build_report_to_dict(build_report)["projection_count"] == 2
    assert index_check_report_to_dict(check_report)["consistent"] is True
    assert validate_index_build_report(build_report) is not build_report
    assert validate_index_check_report(check_report) is not check_report

    build_report["issues"][0]["reason"] = "malformed"
    with pytest.raises(InvalidRequestError, match="must be an IndexIssueReason"):
        validate_index_build_report(build_report)

    check_report["consistent"] = False
    with pytest.raises(InvalidRequestError, match="retain or omit an error"):
        validate_index_check_report(check_report)


def test_checker_reports_corruption_and_expected_safe_exclusions() -> None:
    active = projection("active", "key", support=(ASSERTION_REFERENCE_A,))
    legacy = projection("legacy", eligible=False, exclusion_reason=IndexIssueReason.MISSING_IDENTITY.value)
    state = build_index_state((active, legacy))
    corrupt = state.copy()
    corrupt["record_to_statements"] = MappingProxyType(
        {REFERENCE_IDS.get("d", ""): ("active",)}
    )
    corrupt["statement_to_references"] = MappingProxyType(
        {"active": (ASSERTION_REFERENCE_D,), "malformed": ()}
    )
    corrupt["direct_retrieval"] = MappingProxyType({})

    report = check_index_state(corrupt)
    categories = {issue["category"] for issue in report["issues"]}

    assert report["consistent"] is False
    assert IndexCheckCategory.MISSING in categories
    assert IndexCheckCategory.EXTRA in categories
    assert IndexCheckCategory.ASYMMETRIC in categories
    assert IndexCheckCategory.UNINDEXABLE in categories
    assert IndexCheckCategory.INELIGIBLE in categories


def test_explicit_empty_projection_set_is_not_self_check_fallback() -> None:
    item = projection("stmt-a", "a", support=(ASSERTION_REFERENCE_A,))
    state = build_index_state((item,))

    report = check_index_state_against(state, ())

    assert report["consistent"] is False
    assert IndexCheckCategory.EXTRA in {issue["category"] for issue in report["issues"] if issue["error"]}


def test_empty_projection_repair_is_dry_run_safe_and_atomically_clears() -> None:
    item = projection("stmt-a", "a", support=(ASSERTION_REFERENCE_A,))
    owner = IndexOwner((item,))
    before = owner.snapshot()

    dry_run = owner.repair((), dry_run=True)

    assert type(dry_run) is dict
    assert validate_index_repair_result(dry_run) == dry_run
    assert validate_index_repair_result(dry_run) is not dry_run
    assert index_repair_result_to_dict(dry_run)["applied"] is False
    assert dry_run["changed"] is True
    assert dry_run["live_check"]["consistent"] is False
    assert owner.snapshot() == before

    applied = owner.repair((), dry_run=False)

    assert applied["applied"] is True
    assert owner.snapshot()["projections"] == {}
    assert owner.snapshot()["retrieval_to_owners"] == {}
    assert owner.snapshot()["record_to_statements"] == {}


def test_checker_and_atomic_swap_reject_corrupt_build_report() -> None:
    item = projection("stmt-a", "a", support=(ASSERTION_REFERENCE_A,))
    owner = IndexOwner((item,))
    before = owner.snapshot()
    candidate = build_index_state((item,), before["state_generation"] + 1)
    corrupt = validate_index_state(candidate)
    corrupt["build_report"]["exact_key_count"] = 999

    report = check_index_state(corrupt)

    assert report["consistent"] is False
    assert any(issue["index_name"] == "build_report" and issue["key"] == "exact_key_count" for issue in report["issues"])
    with pytest.raises(ConflictError, match="failed consistency checking"):
        owner.atomic_swap(corrupt, before["state_generation"])
    assert owner.snapshot() == before


def test_checker_rejects_missing_collision_diagnostics() -> None:
    first = projection("stmt-a", "same key")
    second = projection("stmt-b", "same key")
    state = build_index_state((first, second))
    corrupt = validate_index_state(state)
    corrupt["build_report"]["collisions"] = ()

    report = check_index_state(corrupt)

    assert report["consistent"] is False
    assert any(issue["index_name"] == "build_report" and issue["key"] == "collisions" for issue in report["issues"])


def test_checker_rejects_each_reproducible_report_field_mismatch() -> None:
    active = projection("stmt-a", "same key", support=(ASSERTION_REFERENCE_A,))
    collision = projection("stmt-b", "same key")
    legacy = projection("legacy", eligible=False, exclusion_reason=IndexIssueReason.MISSING_IDENTITY.value)
    state = build_index_state((active, collision, legacy))
    report = state["build_report"]
    corrupt_values = (
        ("projection_count", report["projection_count"] + 1),
        ("exact_key_count", report["exact_key_count"] + 1),
        ("support_edge_count", report["support_edge_count"] + 1),
        ("issues", ()),
        ("collisions", ()),
        ("omitted_issue_count", report["omitted_issue_count"] + 1),
        ("omitted_collision_count", report["omitted_collision_count"] + 1),
    )

    for field_name, value in corrupt_values:
        corrupt_state = validate_index_state(state)
        corrupt_state["build_report"][field_name] = value
        check = check_index_state(corrupt_state)
        assert check["consistent"] is False
        assert any(issue["index_name"] == "build_report" and issue["error"] for issue in check["issues"])


def test_input_only_build_classifications_do_not_make_valid_state_unpublishable() -> None:
    owner = IndexOwner((projection("stmt-a", "a"), 42))

    assert owner.snapshot()["build_report"]["issues"][0]["input_only"] is True
    assert owner.check()["consistent"] is True


def test_omitted_input_only_classifications_do_not_make_valid_state_unpublishable() -> None:
    owner = IndexOwner((projection("stmt-a", "a"), *(42 for _ in range(MAX_INDEX_REPORT_ITEMS + 1))))

    assert owner.snapshot()["build_report"]["omitted_issue_count"] == 1
    assert owner.check()["consistent"] is True


def test_index_state_maps_are_immutable() -> None:
    state = build_index_state((projection("stmt-1", "key"),))

    with pytest.raises(TypeError):
        mutate_mapping(
            state["record_to_statements"],
            REFERENCE_IDS.get("a", ""),
            ("stmt-1",),
        )


def test_index_state_is_an_exact_validated_non_aliasing_dictionary() -> None:
    state = build_index_state(
        (projection("stmt-1", "key", support=(ASSERTION_REFERENCE_A,)),)
    )
    copied = validate_index_state(state)

    assert type(state) is dict
    assert copied == state
    assert copied is not state
    assert copied["retrieval_to_owners"] is not state["retrieval_to_owners"]
    assert copied["retrieval_to_owners"] != {}
    assert next(iter(copied["retrieval_to_owners"].values()))[0] is not next(iter(state["retrieval_to_owners"].values()))[0]
    assert copied["projections"]["stmt-1"] is not state["projections"]["stmt-1"]
    assert copied["build_report"] is not state["build_report"]

    malformed: dict[str, object] = dict(state)
    malformed["unexpected"] = True
    with pytest.raises(InvalidRequestError, match="invalid fields"):
        validate_index_state(malformed)

    state["state_generation"] = 0
    with pytest.raises(InvalidRequestError, match="positive integer"):
        validate_index_state(state)


def test_pure_index_mutation_does_not_alias_its_input_state() -> None:
    original = build_index_state((projection("stmt-a", "a"),))
    added = add_index_projection(original, projection("stmt-b", "b"))

    assert added["projections"]["stmt-a"] is not original["projections"]["stmt-a"]
    assert next(iter(added["retrieval_to_owners"].values()))[0] is not next(iter(original["retrieval_to_owners"].values()))[0]

    original["projections"]["stmt-a"]["generation"] = 99
    assert added["projections"]["stmt-a"]["generation"] == 1


def test_generic_mutations_equal_clean_rebuild() -> None:
    first = projection("stmt-a", "a", support=(ASSERTION_REFERENCE_A,))
    second = projection("stmt-b", "b", support=(ASSERTION_REFERENCE_B,))
    replacement = projection("stmt-a", "new a", support=(ASSERTION_REFERENCE_C,))
    state = build_index_state((first,))

    added = add_index_projection(state, second)
    assert state_signature(added) == state_signature(build_index_state((first, second), added["state_generation"]))

    replaced = replace_index_projection(added, replacement)
    assert state_signature(replaced) == state_signature(build_index_state((replacement, second), replaced["state_generation"]))

    supported = update_index_support(replaced, "stmt-b", (ASSERTION_REFERENCE_D,))
    expected_second = index_projection_with_changes(
        second,
        {"generation": 2, "support_references": (ASSERTION_REFERENCE_D,)},
    )
    assert state_signature(supported) == state_signature(
        build_index_state((replacement, expected_second), supported["state_generation"])
    )

    removed = remove_index_projection(supported, "stmt-a")
    assert state_signature(removed) == state_signature(build_index_state((expected_second,), removed["state_generation"]))


def test_atomic_owner_rejects_stale_swap_and_abandoned_build_is_invisible() -> None:
    owner = IndexOwner((projection("stmt-a", "a"),))
    before = owner.snapshot()
    abandoned = build_index_state((projection("stmt-b", "b"),), before["state_generation"] + 1)

    assert owner.snapshot() == before
    owner.add(projection("stmt-b", "b"))
    with pytest.raises(ConflictError, match="stale index state generation"):
        owner.atomic_swap(abandoned, before["state_generation"])


def test_checked_repair_has_bounded_dry_run_and_atomic_apply() -> None:
    expected = (projection("stmt-a", "a", support=(ASSERTION_REFERENCE_A,)),)
    owner = IndexOwner(expected)
    original = owner.snapshot()
    corrupt = original.copy()
    corrupt["record_to_statements"] = MappingProxyType({})
    owner._state = corrupt

    dry_run = owner.repair(expected, dry_run=True)
    assert dry_run["applied"] is False
    assert dry_run["changed"] is True
    assert owner.snapshot()["record_to_statements"] == {}

    applied = owner.repair(expected, dry_run=False)
    assert applied["applied"] is True
    assert owner.snapshot()["record_to_statements"] == {
        REFERENCE_IDS.get("a", ""): ("stmt-a",)
    }
    assert check_index_state(owner.snapshot())["consistent"] is True


def test_concurrent_readers_observe_only_complete_checked_states() -> None:
    base = projection("base", "base", support=(ASSERTION_REFERENCE_A,))
    changing = projection("changing", "changing", support=(ASSERTION_REFERENCE_B,))
    owner = IndexOwner((base,))
    start = threading.Barrier(3)
    errors = []

    def writer() -> None:
        start.wait()
        for _ in range(30):
            owner.add(changing)
            owner.remove(changing["statement_id"])

    def reader() -> None:
        start.wait()
        for _ in range(100):
            snapshot = owner.snapshot()
            if not check_index_state(snapshot)["consistent"]:
                errors.append(snapshot["state_generation"])

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
        template={"tapestry": {"support": [ASSERTION_REFERENCE_A]}},
        keyword_source="question words",
    )
    state = engram.index_snapshot()

    assert state["record_to_statements"] == {
        REFERENCE_IDS.get("a", ""): (statement_id,)
    }
    assert state["statement_to_retrieval"][statement_id] == ()
    assert IndexIssueReason.MISSING_IDENTITY in {issue["reason"] for issue in state["build_report"]["issues"]}


def test_current_support_metadata_updates_and_eviction_update_both_maps() -> None:
    engram = Engram()
    statement_id = engram.learn_from_response(
        "question",
        "first",
        template={"tapestry": {"support": [ASSERTION_REFERENCE_A]}},
    )
    same_id = engram.learn_from_response(
        "question",
        "second",
        template={"tapestry": {"support": [ASSERTION_REFERENCE_B]}},
    )

    assert same_id == statement_id
    assert engram.index_snapshot()["record_to_statements"] == {
        REFERENCE_IDS.get("b", ""): (statement_id,)
    }

    engram.retire_statement(statement_id)
    assert engram.index_snapshot()["record_to_statements"] == {}
    assert engram.index_snapshot()["statement_to_references"] == {}


def test_current_support_metadata_rebuilds_after_persistence_load() -> None:
    engram = Engram()
    statement_id = engram.store(
        "A persisted response",
        template={"tapestry": {"support": [ASSERTION_REFERENCE_C]}},
    )

    loaded = persistence.load_engram_json(persistence.save_json(engram))

    assert loaded.index_snapshot()["record_to_statements"] == {
        REFERENCE_IDS.get("c", ""): (statement_id,)
    }
    assert loaded.index_snapshot()["statement_to_references"] == {
        statement_id: (ASSERTION_REFERENCE_C,)
    }


def test_vector_support_path_does_not_iterate_statement_corpus() -> None:
    class NoIterationList(list):
        def __iter__(self):
            raise AssertionError("vector support retrieval scanned the statement corpus")

    engram = Engram()
    statement_id = engram.store(
        "A response",
        priority=3,
        template={"tapestry": {"support": [PROPOSITION_REFERENCE_A]}},
    )
    engram.statements = NoIterationList(engram.statements)
    engram.config["graph"] = {"vector_weight": 0.65}
    engram.graph_vector_propositions = lambda text, *, limit=0: [
        {"proposition_id": REFERENCE_IDS.get("proposition_a", ""), "similarity": 0.8}
    ]

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
                        "support": [PROPOSITION_REFERENCE_A],
                    }
                },
            )
        )
    engram.statements = statements
    engram.statement_index = {item["id"]: index for index, item in enumerate(statements)}
    engram._index_owner = IndexOwner(tuple(projection_from_statement(item) for item in statements))
    engram.config["graph"] = {"vector_weight": 1.0, "vector_support_scan_limit": 2_000}
    engram.graph_vector_propositions = lambda text, *, limit=0: [
        {"proposition_id": REFERENCE_IDS.get("proposition_a", ""), "similarity": 1.0}
    ]

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
        template={"tapestry": {"support": [PROPOSITION_REFERENCE_A]}},
    )
    second = statement(
        "Second",
        statement_id="stmt-b",
        template={"tapestry": {"support": [PROPOSITION_REFERENCE_A]}},
    )
    engram.statements = [first, second]
    engram.statement_index = {"stmt-a": 0, "stmt-b": 1}
    engram._index_owner = IndexOwner((projection_from_statement(first), projection_from_statement(second)))
    engram.config["graph"] = {"vector_weight": 1.0, "vector_support_scan_limit": 1}
    engram.graph_vector_propositions = lambda text, *, limit=0: [
        {"proposition_id": REFERENCE_IDS.get("proposition_a", ""), "similarity": 1.0}
    ]

    matches = engram.vector_supported_matches("query", limit=1)

    assert matches == []
    assert "vector_support_scan_incomplete" in caplog.text


def test_vector_support_deduplicates_multi_proposition_paths_and_keeps_score_top_k() -> None:
    engram = Engram()
    first = statement(
        "First",
        statement_id="stmt-a",
        template={"tapestry": {"support": [PROPOSITION_REFERENCE_A, PROPOSITION_REFERENCE_C]}},
    )
    second = statement(
        "Second",
        statement_id="stmt-b",
        template={"tapestry": {"support": [PROPOSITION_REFERENCE_B]}},
    )
    engram.statements = [first, second]
    engram.statement_index = {"stmt-a": 0, "stmt-b": 1}
    engram._index_owner = IndexOwner((projection_from_statement(first), projection_from_statement(second)))
    engram.config["graph"] = {"vector_weight": 1.0, "vector_support_scan_limit": 10}
    engram.graph_vector_propositions = lambda text, *, limit=0: [
        {"proposition_id": REFERENCE_IDS.get("proposition_a", ""), "similarity": 0.1},
        {"proposition_id": REFERENCE_IDS.get("proposition_b", ""), "similarity": 0.5},
        {"proposition_id": REFERENCE_IDS.get("proposition_c", ""), "similarity": 0.9},
    ]

    matches = engram.vector_supported_matches("query", limit=1)

    assert len(matches) == 1
    assert matches[0][0]["id"] == "stmt-a"
    assert matches[0][1] == pytest.approx(0.9)


def test_core_explicit_empty_projection_operations_preserve_authoritative_statements() -> None:
    engram = Engram()
    statement_id = engram.store(
        "A response",
        template={"tapestry": {"support": [PROPOSITION_REFERENCE_A]}},
    )

    assert engram.check_index_projections(())["consistent"] is False
    dry_run = engram.repair_index_projections((), dry_run=True)
    applied = engram.repair_index_projections((), dry_run=False)

    assert dry_run["changed"] is True
    assert applied["applied"] is True
    assert engram.get_statement(statement_id)["text"] == "A response"
    assert engram.index_snapshot()["projections"] == {}
    assert engram.check_indexes()["consistent"] is False

    restored = engram.repair_indexes(dry_run=False)
    assert restored["applied"] is True
    assert engram.check_indexes()["consistent"] is True


def test_engram_repair_does_not_change_authoritative_statement_content() -> None:
    engram = Engram()
    engram.store(
        "A response",
        template={"tapestry": {"support": [PROPOSITION_REFERENCE_A]}},
    )
    before = [dict(statement) for statement in engram.statements]
    state = engram.index_snapshot()
    corrupt = state.copy()
    corrupt["record_to_statements"] = MappingProxyType({})
    engram._index_owner._state = corrupt

    dry_run = engram.repair_indexes(dry_run=True)
    repaired = engram.repair_indexes(dry_run=False)

    assert dry_run["applied"] is False
    assert repaired["applied"] is True
    assert engram.statements == before
    assert engram.check_indexes()["consistent"] is True


def test_generic_mutation_validation_is_strict() -> None:
    state = build_index_state(())
    with pytest.raises(InvalidRequestError):
        call_support_lookup(state, ["proposition-1"])
    with pytest.raises(InvalidRequestError):
        remove_index_projection(state, "missing")
    with pytest.raises(InvalidRequestError):
        index_state_support_lookup(state, ("proposition-1",), scan_limit=0)
