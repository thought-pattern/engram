"""Section 3 request eligibility context and namespace epoch tests."""

import json
import threading
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta, timezone

import pytest

from engram.artifacts import CachedResponseArtifact, LifecycleState, artifact_provenance, cached_response_artifact
from engram.constants import Tier
from engram.eligibility import (
    MAX_EPOCH,
    ContextualExactLookup,
    EligibilityContext,
    EligibilityContextFactory,
    EligibilityExclusionReason,
    EpochChangeReason,
    EpochEligibilityPolicy,
    EpochSource,
    NamespaceEpochState,
    contextual_exact_lookup_result_to_dict,
    eligibility_context as build_eligibility_context,
    eligibility_context_from_dict,
    eligibility_context_from_json,
    eligibility_context_from_trusted_input,
    eligibility_context_to_dict,
    eligibility_context_to_json,
    eligibility_decision_context_signature,
    eligibility_decision_to_dict,
    epoch_increment,
    epoch_increment_to_dict,
    evaluate_artifact_eligibility,
    index_projection_from_artifact,
    namespace_epoch_state_from_snapshot,
    namespace_epoch_to_dict,
    trusted_eligibility_input,
    trusted_eligibility_input_to_dict,
    validate_contextual_exact_lookup_result,
    validate_eligibility_context,
    validate_eligibility_decision,
    validate_epoch_increment,
    validate_namespace_epoch,
)
from engram.errors import ConflictError, InvalidRequestError, LifecycleError
from engram.identity import (
    build_retrieval_representation,
    build_scoped_retrieval_key,
    build_standalone_identity,
    retrieval_representation_bindings,
    scope_key,
)
from engram.indexes import ExactLookupOutcome, IndexOwner, index_projection, index_projection_to_dict, index_state_exact_lookup


def accepted_artifact(**overrides) -> CachedResponseArtifact:
    scope = overrides.pop("scope", scope_key(namespace="tenant-a"))
    request = "Who acquired GitHub?"
    values = {
        "statement_id": "stmt-response-1",
        "generation": 1,
        "response": "Microsoft acquired GitHub in 2018.",
        "query_identity": build_standalone_identity(request, scope),
        "retrieval": build_retrieval_representation(request),
        "tier": Tier.STATIC,
        "lifecycle": LifecycleState.ACTIVE,
        "scope": scope,
        "support_claim_ids": (),
        "valid_from": "",
        "valid_from_available": False,
        "valid_until": "",
        "valid_until_available": False,
        "knowledge_epoch": 0,
        "knowledge_epoch_available": False,
        "superseded_by": "",
        "provenance": artifact_provenance("test", "caller", "2026-08-12T15:00:00Z"),
        "metadata": {},
    }
    values.update(overrides)
    result = cached_response_artifact(**values)
    return result


def eligibility_context(**overrides) -> EligibilityContext:
    values = {
        "evaluation_time": "2026-08-12T16:00:00Z",
        "evaluation_time_available": True,
        "namespace": "tenant-a",
        "knowledge_epoch": 42,
        "knowledge_epoch_available": True,
        "artifact_repository_available": True,
        "epoch_source": EpochSource.STANDALONE,
    }
    values.update(overrides)
    result = build_eligibility_context(**values)
    return result


def test_namespace_epoch_initialization_lookup_increment_and_snapshot_round_trip() -> None:
    state = NamespaceEpochState()
    assert namespace_epoch_to_dict(state.get("tenant-a")) == {
        "namespace": "tenant-a",
        "knowledge_epoch": 0,
        "knowledge_epoch_available": False,
    }

    initialized = state.initialize("tenant-a", 3)
    repeated = state.initialize("tenant-a", 3)
    increment = state.increment("tenant-a", 3, EpochChangeReason.ACCEPTED_ARTIFACT_ELIGIBILITY)

    assert initialized == repeated
    assert epoch_increment_to_dict(increment) == {
        "namespace": "tenant-a",
        "previous_epoch": 3,
        "knowledge_epoch": 4,
        "reason": "accepted_artifact_eligibility",
    }
    restored = namespace_epoch_state_from_snapshot(state.snapshot())
    assert restored.snapshot() == state.snapshot()
    assert json.dumps(state.snapshot(), separators=(",", ":"), sort_keys=True) == ('{"epochs":{"tenant-a":4},"schema_version":1}')


def test_namespace_epoch_reinitialization_and_stale_increment_conflict() -> None:
    state = NamespaceEpochState({"tenant-a": 8})
    with pytest.raises(ConflictError, match="already initialized"):
        state.initialize("tenant-a", 7)
    with pytest.raises(ConflictError, match="expected 7, current 8"):
        state.increment("tenant-a", 7, EpochChangeReason.GRAPH_SNAPSHOT_ACTIVATED)
    assert state.get("tenant-a")["knowledge_epoch"] == 8


def test_namespace_epoch_requires_initialization_and_cannot_overflow() -> None:
    state = NamespaceEpochState()
    with pytest.raises(LifecycleError, match="not initialized"):
        state.increment("tenant-a", 0, EpochChangeReason.ACCEPTED_ARTIFACT_ELIGIBILITY)

    exhausted = NamespaceEpochState({"tenant-a": MAX_EPOCH})
    with pytest.raises(LifecycleError, match="exhausted"):
        exhausted.increment("tenant-a", MAX_EPOCH, EpochChangeReason.ACCEPTED_ARTIFACT_ELIGIBILITY)


def test_concurrent_expected_epoch_increment_has_one_winner() -> None:
    state = NamespaceEpochState({"tenant-a": 10})
    barrier = threading.Barrier(3)
    successes = []
    conflicts = []

    def increment() -> None:
        barrier.wait()
        try:
            successes.append(state.increment("tenant-a", 10, EpochChangeReason.ACCEPTED_ARTIFACT_ELIGIBILITY))
        except ConflictError as error:
            conflicts.append(str(error))

    threads = [threading.Thread(target=increment) for _ in range(2)]
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join()

    assert len(successes) == 1
    assert len(conflicts) == 1
    assert state.get("tenant-a")["knowledge_epoch"] == 11


def test_namespace_epoch_records_are_exact_validated_non_aliasing_dictionaries() -> None:
    unavailable = NamespaceEpochState().get("tenant-a")
    increment = epoch_increment("tenant-a", 3, 4, EpochChangeReason.ACCEPTED_ARTIFACT_ELIGIBILITY)

    assert type(unavailable) is dict
    assert type(increment) is dict
    assert validate_namespace_epoch(unavailable) == unavailable
    assert validate_epoch_increment(increment) == increment
    assert validate_namespace_epoch(unavailable) is not unavailable
    assert validate_epoch_increment(increment) is not increment

    unavailable["knowledge_epoch"] = 1
    increment["knowledge_epoch"] = 5
    with pytest.raises(InvalidRequestError, match="must be 0 when unavailable"):
        validate_namespace_epoch(unavailable)
    with pytest.raises(InvalidRequestError, match="must equal previous_epoch plus one"):
        validate_epoch_increment(increment)

    malformed_epoch = {"namespace": "tenant-a", "knowledge_epoch": 0}
    malformed_increment = {
        "namespace": "tenant-a",
        "previous_epoch": 3,
        "knowledge_epoch": 4,
        "reason": EpochChangeReason.ACCEPTED_ARTIFACT_ELIGIBILITY,
        "unexpected": True,
    }
    with pytest.raises(InvalidRequestError, match="invalid fields"):
        validate_namespace_epoch(malformed_epoch)
    with pytest.raises(InvalidRequestError, match="invalid fields"):
        validate_epoch_increment(malformed_increment)


def test_standalone_context_captures_injected_clock_once_and_epoch_once() -> None:
    calls = []

    def clock() -> datetime:
        calls.append("clock")
        result = datetime(2026, 8, 12, 16, 0, tzinfo=UTC)
        return result

    state = NamespaceEpochState({"tenant-a": 42})
    factory = EligibilityContextFactory(clock, state)
    context = factory.capture_standalone(scope_key(namespace="tenant-a"), True)

    assert calls == ["clock"]
    assert eligibility_context_to_dict(context) == {
        "schema_version": 1,
        "evaluation_time": "2026-08-12T16:00:00Z",
        "evaluation_time_available": True,
        "namespace": "tenant-a",
        "knowledge_epoch": 42,
        "knowledge_epoch_available": True,
        "artifact_repository_available": True,
        "epoch_source": "standalone",
    }
    encoded = eligibility_context_to_json(context)
    assert eligibility_context_from_json(encoded) == context
    assert eligibility_context_to_json(eligibility_context_from_json(encoded)) == encoded


def test_uninitialized_standalone_epoch_is_explicitly_unavailable() -> None:
    factory = EligibilityContextFactory(
        lambda: datetime(2026, 8, 12, 16, 0, tzinfo=UTC),
        NamespaceEpochState(),
    )
    context = factory.capture_standalone(scope_key(namespace="tenant-a"), False)

    assert context["knowledge_epoch"] == 0
    assert context["knowledge_epoch_available"] is False
    assert context["epoch_source"] == EpochSource.UNAVAILABLE
    assert context["artifact_repository_available"] is False


def test_trusted_capture_uses_only_explicit_trusted_record_and_not_core_clock() -> None:
    trusted = trusted_eligibility_input(
        "tenant-a",
        "2026-08-12T17:00:00Z",
        True,
        7,
        True,
        "tapestry-core",
    )

    context = eligibility_context_from_trusted_input(trusted, True)

    assert type(trusted) is dict
    assert trusted_eligibility_input_to_dict(trusted) == trusted
    assert context["evaluation_time"] == "2026-08-12T17:00:00Z"
    assert context["knowledge_epoch"] == 7
    assert context["epoch_source"] == EpochSource.TRUSTED_INTEGRATION


@pytest.mark.parametrize(
    ("clock_value", "message"),
    [
        ("2026-08-12T16:00:00Z", "must return a datetime"),
        (datetime(2026, 8, 12, 16, 0), "timezone-aware UTC"),
        (datetime(2026, 8, 12, 12, 0, tzinfo=timezone(-timedelta(hours=4))), "timezone-aware UTC"),
    ],
)
def test_standalone_clock_boundary_requires_aware_utc(clock_value, message) -> None:
    factory = EligibilityContextFactory(lambda: clock_value, NamespaceEpochState({"": 0}))
    with pytest.raises(InvalidRequestError, match=message):
        factory.capture_standalone(scope_key(), True)


@pytest.mark.parametrize(
    ("call", "message"),
    [
        (lambda: NamespaceEpochState([]), "must be an object"),
        (lambda: NamespaceEpochState({"tenant-a": True}), "64-bit integer"),
        (
            lambda: NamespaceEpochState({"tenant-a": 0}).increment("tenant-a", 0, "artifact"),
            "reason must be an EpochChangeReason",
        ),
        (
            lambda: epoch_increment("tenant-a", 3, 5, EpochChangeReason.ACCEPTED_ARTIFACT_ELIGIBILITY),
            "must equal previous_epoch plus one",
        ),
        (
            lambda: build_eligibility_context(
                evaluation_time="2026-08-12T16:00:00Z",
                evaluation_time_available=True,
                namespace="tenant-a",
                knowledge_epoch=0,
                knowledge_epoch_available=False,
                artifact_repository_available=True,
                epoch_source=EpochSource.STANDALONE,
            ),
            "unavailable knowledge_epoch must use epoch_source UNAVAILABLE",
        ),
        (
            lambda: trusted_eligibility_input(
                "tenant-a",
                "",
                False,
                0,
                False,
                "tapestry-core",
            ),
            "evaluation_time must be available",
        ),
    ],
)
def test_context_and_epoch_boundaries_reject_invalid_concrete_values(call, message) -> None:
    with pytest.raises(InvalidRequestError, match=message):
        call()


def test_context_json_loader_rejects_invalid_roots_and_fields() -> None:
    context = build_eligibility_context(
        evaluation_time="2026-08-12T16:00:00Z",
        evaluation_time_available=True,
        namespace="tenant-a",
        knowledge_epoch=0,
        knowledge_epoch_available=False,
        artifact_repository_available=False,
        epoch_source=EpochSource.UNAVAILABLE,
    )
    data = eligibility_context_to_dict(context)
    data["extra"] = False
    with pytest.raises(InvalidRequestError, match="invalid fields"):
        eligibility_context_from_dict(data)
    with pytest.raises(InvalidRequestError, match="malformed"):
        eligibility_context_from_json("{")
    with pytest.raises(InvalidRequestError, match="must contain an object"):
        eligibility_context_from_json("[]")


def test_eligibility_inputs_and_contexts_revalidate_mutation_and_copy() -> None:
    trusted = trusted_eligibility_input("tenant-a", "2026-08-12T17:00:00Z", True, 7, True, "tapestry-core")
    context = eligibility_context()

    assert type(context) is dict
    assert validate_eligibility_context(context) == context
    assert validate_eligibility_context(context) is not context

    trusted["knowledge_epoch_available"] = False
    context["epoch_source"] = EpochSource.UNAVAILABLE
    with pytest.raises(InvalidRequestError, match="must be 0 when unavailable"):
        trusted_eligibility_input_to_dict(trusted)
    with pytest.raises(InvalidRequestError, match="cannot carry an available"):
        validate_eligibility_context(context)


@pytest.mark.parametrize(
    ("lifecycle", "superseded_by", "reason"),
    [
        (LifecycleState.ACTIVE, "", EligibilityExclusionReason.ELIGIBLE),
        (LifecycleState.SUPERSEDED, "stmt-new", EligibilityExclusionReason.LIFECYCLE_SUPERSEDED),
        (LifecycleState.INVALIDATED, "", EligibilityExclusionReason.LIFECYCLE_INVALIDATED),
        (LifecycleState.RETIRED, "", EligibilityExclusionReason.LIFECYCLE_RETIRED),
    ],
)
def test_lifecycle_eligibility_truth_table(lifecycle, superseded_by, reason) -> None:
    artifact = accepted_artifact(lifecycle=lifecycle, superseded_by=superseded_by)
    decision = evaluate_artifact_eligibility(
        artifact,
        eligibility_context(),
        EpochEligibilityPolicy.MATCH_WHEN_ARTIFACT_AVAILABLE,
    )

    assert decision["exclusion_reason"] == reason
    assert decision["direct_answer_eligible"] is (reason == EligibilityExclusionReason.ELIGIBLE)
    assert decision["lifecycle_base_eligible"] is (lifecycle == LifecycleState.ACTIVE)


@pytest.mark.parametrize(
    ("evaluation_time", "reason"),
    [
        ("2026-08-12T15:59:59Z", EligibilityExclusionReason.NOT_YET_VALID),
        ("2026-08-12T16:00:00Z", EligibilityExclusionReason.ELIGIBLE),
        ("2026-08-12T16:59:59Z", EligibilityExclusionReason.ELIGIBLE),
        ("2026-08-12T17:00:00Z", EligibilityExclusionReason.EXPIRED),
        ("2026-08-12T17:00:01Z", EligibilityExclusionReason.EXPIRED),
    ],
)
def test_half_open_validity_boundaries(evaluation_time, reason) -> None:
    artifact = accepted_artifact(
        valid_from="2026-08-12T16:00:00Z",
        valid_from_available=True,
        valid_until="2026-08-12T17:00:00Z",
        valid_until_available=True,
    )
    decision = evaluate_artifact_eligibility(
        artifact,
        eligibility_context(evaluation_time=evaluation_time),
        EpochEligibilityPolicy.MATCH_WHEN_ARTIFACT_AVAILABLE,
    )
    assert decision["exclusion_reason"] == reason


@pytest.mark.parametrize(
    ("valid_from", "valid_until"),
    [
        ("2026-08-12T17:00:00Z", "2026-08-12T17:00:00Z"),
        ("2026-08-12T18:00:00Z", "2026-08-12T17:00:00Z"),
    ],
)
def test_invalid_validity_interval_precedes_time_position(valid_from, valid_until) -> None:
    artifact = accepted_artifact(
        valid_from=valid_from,
        valid_from_available=True,
        valid_until=valid_until,
        valid_until_available=True,
    )
    decision = evaluate_artifact_eligibility(
        artifact,
        eligibility_context(evaluation_time="2026-08-12T16:00:00Z"),
        EpochEligibilityPolicy.MATCH_WHEN_ARTIFACT_AVAILABLE,
    )
    assert decision["exclusion_reason"] == EligibilityExclusionReason.VALIDITY_INTERVAL_INVALID


@pytest.mark.parametrize(
    ("artifact_epoch", "artifact_available", "context_epoch", "context_available", "policy", "reason"),
    [
        (42, True, 42, True, EpochEligibilityPolicy.REQUIRE_MATCH, EligibilityExclusionReason.ELIGIBLE),
        (42, True, 43, True, EpochEligibilityPolicy.REQUIRE_MATCH, EligibilityExclusionReason.KNOWLEDGE_EPOCH_MISMATCH),
        (42, True, 0, False, EpochEligibilityPolicy.REQUIRE_MATCH, EligibilityExclusionReason.CONTEXT_EPOCH_UNAVAILABLE),
        (0, False, 42, True, EpochEligibilityPolicy.REQUIRE_MATCH, EligibilityExclusionReason.ARTIFACT_EPOCH_UNAVAILABLE),
        (
            0,
            False,
            42,
            True,
            EpochEligibilityPolicy.MATCH_WHEN_ARTIFACT_AVAILABLE,
            EligibilityExclusionReason.ELIGIBLE,
        ),
        (
            42,
            True,
            42,
            True,
            EpochEligibilityPolicy.MATCH_WHEN_ARTIFACT_AVAILABLE,
            EligibilityExclusionReason.ELIGIBLE,
        ),
    ],
)
def test_epoch_policy_truth_table(artifact_epoch, artifact_available, context_epoch, context_available, policy, reason) -> None:
    artifact = accepted_artifact(
        knowledge_epoch=artifact_epoch,
        knowledge_epoch_available=artifact_available,
    )
    source = EpochSource.STANDALONE if context_available else EpochSource.UNAVAILABLE
    context = eligibility_context(
        knowledge_epoch=context_epoch,
        knowledge_epoch_available=context_available,
        epoch_source=source,
    )
    decision = evaluate_artifact_eligibility(artifact, context, policy)
    assert decision["exclusion_reason"] == reason


@pytest.mark.parametrize(
    ("context", "artifact", "reason"),
    [
        (
            eligibility_context(artifact_repository_available=False),
            accepted_artifact(lifecycle=LifecycleState.RETIRED),
            EligibilityExclusionReason.ARTIFACT_REPOSITORY_UNAVAILABLE,
        ),
        (
            eligibility_context(evaluation_time="", evaluation_time_available=False),
            accepted_artifact(lifecycle=LifecycleState.RETIRED),
            EligibilityExclusionReason.EVALUATION_TIME_UNAVAILABLE,
        ),
        (
            eligibility_context(namespace="tenant-b"),
            accepted_artifact(lifecycle=LifecycleState.RETIRED),
            EligibilityExclusionReason.SCOPE_NAMESPACE_MISMATCH,
        ),
    ],
)
def test_exclusion_precedence_is_stable(context, artifact, reason) -> None:
    decision = evaluate_artifact_eligibility(
        artifact,
        context,
        EpochEligibilityPolicy.MATCH_WHEN_ARTIFACT_AVAILABLE,
    )
    assert decision["exclusion_reason"] == reason


def test_decision_records_exact_context_and_stable_signature() -> None:
    artifact = accepted_artifact(knowledge_epoch=42, knowledge_epoch_available=True)
    context = eligibility_context()
    decision = evaluate_artifact_eligibility(artifact, context, EpochEligibilityPolicy.REQUIRE_MATCH)

    assert type(decision) is dict
    assert eligibility_decision_to_dict(decision) == {
        "statement_id": "stmt-response-1",
        "generation": 1,
        "lifecycle_base_eligible": True,
        "direct_answer_eligible": True,
        "exclusion_reason": "eligible",
        "evaluation_time": "2026-08-12T16:00:00Z",
        "evaluation_time_available": True,
        "namespace": "tenant-a",
        "knowledge_epoch": 42,
        "knowledge_epoch_available": True,
        "artifact_repository_available": True,
        "epoch_policy": "require_match",
    }
    assert eligibility_decision_context_signature(decision) == (
        '{"artifact_repository_available":true,"epoch_policy":"require_match",'
        '"evaluation_time":"2026-08-12T16:00:00Z","evaluation_time_available":true,'
        '"knowledge_epoch":42,"knowledge_epoch_available":true,"namespace":"tenant-a"}'
    )
    copied = validate_eligibility_decision(decision)
    assert copied == decision
    assert copied is not decision
    decision["direct_answer_eligible"] = False
    with pytest.raises(InvalidRequestError, match="must agree with exclusion_reason"):
        validate_eligibility_decision(decision)


@pytest.mark.parametrize(
    ("artifact", "context", "policy", "message"),
    [
        ({}, eligibility_context(), EpochEligibilityPolicy.REQUIRE_MATCH, "must be a CachedResponseArtifact"),
        (accepted_artifact(), {}, EpochEligibilityPolicy.REQUIRE_MATCH, "must be an EligibilityContext"),
        (accepted_artifact(), eligibility_context(), "require_match", "must be an EpochEligibilityPolicy"),
    ],
)
def test_pure_eligibility_rejects_wrong_concrete_types(artifact, context, policy, message) -> None:
    with pytest.raises(InvalidRequestError, match=message):
        evaluate_artifact_eligibility(artifact, context, policy)


def test_artifact_projection_contains_only_index_fields_and_decision() -> None:
    artifact = accepted_artifact(
        support_claim_ids=("claim-2", "claim-1"),
        knowledge_epoch=42,
        knowledge_epoch_available=True,
    )
    decision = evaluate_artifact_eligibility(
        artifact,
        eligibility_context(),
        EpochEligibilityPolicy.REQUIRE_MATCH,
    )
    projection = index_projection_from_artifact(artifact, decision)

    serialized = index_projection_to_dict(projection)
    assert projection["statement_id"] == artifact["statement_id"]
    assert projection["generation"] == artifact["generation"]
    assert projection["support_claim_ids"] == ("claim-1", "claim-2")
    assert projection["direct_answer_eligible"] is True
    assert projection["exclusion_reason"] == ""
    assert projection["retrieval_keys"] == retrieval_representation_bindings(artifact["retrieval"], artifact["scope"])
    assert "response" not in serialized
    assert "lifecycle" not in serialized


def test_projection_rejects_decision_for_other_generation_or_namespace() -> None:
    artifact = accepted_artifact()
    decision = evaluate_artifact_eligibility(
        artifact,
        eligibility_context(),
        EpochEligibilityPolicy.MATCH_WHEN_ARTIFACT_AVAILABLE,
    )
    with pytest.raises(ConflictError, match="artifact generation"):
        index_projection_from_artifact(accepted_artifact(generation=2), decision)
    other_scope = scope_key(namespace="tenant-b")
    other = accepted_artifact(scope=other_scope)
    with pytest.raises(ConflictError, match="namespace"):
        index_projection_from_artifact(other, decision)


def test_contextual_lookup_refreshes_eligible_projection_at_expiration_boundary() -> None:
    artifact = accepted_artifact(
        valid_until="2026-08-12T17:00:00Z",
        valid_until_available=True,
    )
    before = eligibility_context(evaluation_time="2026-08-12T16:59:59Z")
    initial = index_projection_from_artifact(
        artifact,
        evaluate_artifact_eligibility(
            artifact,
            before,
            EpochEligibilityPolicy.MATCH_WHEN_ARTIFACT_AVAILABLE,
        ),
    )
    owner = IndexOwner((initial,))
    lookup = ContextualExactLookup({artifact["statement_id"]: artifact}, owner)
    key = build_scoped_retrieval_key(artifact["scope"], artifact["retrieval"]["canonical"])

    found = lookup.exact_lookup(key, before, EpochEligibilityPolicy.MATCH_WHEN_ARTIFACT_AVAILABLE)
    expired = lookup.exact_lookup(
        key,
        eligibility_context(evaluation_time="2026-08-12T17:00:00Z"),
        EpochEligibilityPolicy.MATCH_WHEN_ARTIFACT_AVAILABLE,
    )

    assert found["lookup"]["outcome"] == ExactLookupOutcome.FOUND
    assert found["index_refreshed"] is False
    assert expired["lookup"]["outcome"] == ExactLookupOutcome.MISS
    assert expired["index_refreshed"] is True
    assert expired["decisions"][0]["exclusion_reason"] == EligibilityExclusionReason.EXPIRED
    assert owner.snapshot()["projections"][artifact["statement_id"]]["direct_answer_eligible"] is False


def test_contextual_lookup_refreshes_epoch_stale_then_current_without_artifact_mutation() -> None:
    artifact = accepted_artifact(knowledge_epoch=5, knowledge_epoch_available=True)
    current = eligibility_context(knowledge_epoch=5)
    initial = index_projection_from_artifact(
        artifact,
        evaluate_artifact_eligibility(artifact, current, EpochEligibilityPolicy.REQUIRE_MATCH),
    )
    owner = IndexOwner((initial,))
    lookup = ContextualExactLookup({artifact["statement_id"]: artifact}, owner)
    key = build_scoped_retrieval_key(artifact["scope"], artifact["retrieval"]["canonical"])

    stale = lookup.exact_lookup(key, eligibility_context(knowledge_epoch=6), EpochEligibilityPolicy.REQUIRE_MATCH)
    restored = lookup.exact_lookup(key, current, EpochEligibilityPolicy.REQUIRE_MATCH)

    assert stale["lookup"]["outcome"] == ExactLookupOutcome.MISS
    assert stale["decisions"][0]["exclusion_reason"] == EligibilityExclusionReason.KNOWLEDGE_EPOCH_MISMATCH
    assert restored["lookup"]["outcome"] == ExactLookupOutcome.FOUND
    assert artifact["knowledge_epoch"] == 5


def test_contextual_lookup_refreshes_every_owner_before_collision_outcome() -> None:
    active = accepted_artifact(statement_id="stmt-active")
    retired = accepted_artifact(statement_id="stmt-retired", lifecycle=LifecycleState.RETIRED)
    context = eligibility_context()
    stale_retired_projection = index_projection(
        retired["statement_id"],
        retired["generation"],
        retrieval_representation_bindings(retired["retrieval"], retired["scope"]),
        (),
        True,
        "",
    )
    active_projection = index_projection_from_artifact(
        active,
        evaluate_artifact_eligibility(active, context, EpochEligibilityPolicy.MATCH_WHEN_ARTIFACT_AVAILABLE),
    )
    owner = IndexOwner((active_projection, stale_retired_projection))
    key = build_scoped_retrieval_key(active["scope"], active["retrieval"]["canonical"])
    assert index_state_exact_lookup(owner.snapshot(), key)["outcome"] == ExactLookupOutcome.COLLISION

    result = ContextualExactLookup(
        {active["statement_id"]: active, retired["statement_id"]: retired},
        owner,
    ).exact_lookup(key, context, EpochEligibilityPolicy.MATCH_WHEN_ARTIFACT_AVAILABLE)

    assert type(result) is dict
    assert result["lookup"]["outcome"] == ExactLookupOutcome.FOUND
    assert result["lookup"]["statement_id"] == active["statement_id"]
    assert len(result["decisions"]) == 2
    copied = validate_contextual_exact_lookup_result(result)
    assert copied == result
    assert copied is not result
    serialized = contextual_exact_lookup_result_to_dict(result)
    serialized_lookup = serialized["lookup"]
    assert isinstance(serialized_lookup, Mapping)
    assert serialized_lookup["outcome"] == "FOUND"


def test_contextual_lookup_abstains_when_authoritative_owner_is_missing() -> None:
    artifact = accepted_artifact()
    context = eligibility_context()
    projection = index_projection_from_artifact(
        artifact,
        evaluate_artifact_eligibility(artifact, context, EpochEligibilityPolicy.MATCH_WHEN_ARTIFACT_AVAILABLE),
    )
    owner = IndexOwner((projection,))
    key = build_scoped_retrieval_key(artifact["scope"], artifact["retrieval"]["canonical"])

    with pytest.raises(LifecycleError, match="artifact missing"):
        ContextualExactLookup({}, owner).exact_lookup(
            key,
            context,
            EpochEligibilityPolicy.MATCH_WHEN_ARTIFACT_AVAILABLE,
        )


def test_contextual_empty_lookup_returns_context_signature_without_refresh() -> None:
    context = eligibility_context()
    owner = IndexOwner()
    key = build_scoped_retrieval_key(scope_key(namespace="tenant-a"), "Who acquired GitHub?")
    result = ContextualExactLookup({}, owner).exact_lookup(
        key,
        context,
        EpochEligibilityPolicy.MATCH_WHEN_ARTIFACT_AVAILABLE,
    )

    assert result["lookup"]["outcome"] == ExactLookupOutcome.MISS
    assert result["decisions"] == ()
    assert result["index_refreshed"] is False
    assert '"evaluation_time":"2026-08-12T16:00:00Z"' in result["context_signature"]
