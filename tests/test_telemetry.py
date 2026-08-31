"""Bounded Section 15 operational telemetry tests."""

from json import dumps as json_dumps

from engram.constants import ResolutionOutcome, ResolverState
from engram.resolution import budget_consumption, empty_candidate, resolution_result, resolver_result
from engram.service import EngramCore
from engram.telemetry import operational_telemetry, record_resolution, telemetry_snapshot


def test_resolution_telemetry_aggregates_fixed_outcomes_contributions_and_resources() -> None:
    request = "Does Sarah like private sushi?"
    namespace = "private-customer-namespace"
    core = EngramCore()
    learned = core.learn_response(request, "Sarah likes sushi.", "telemetry-learn", user_id="Sarah", namespace=namespace)

    answer = core.resolve_request(
        request,
        "telemetry-answer",
        user_id="Sarah",
        namespace=namespace,
        configured_resolvers=("exact",),
        accept_exact=True,
    )
    replay = core.resolve_request(
        request,
        "telemetry-answer",
        user_id="Sarah",
        namespace=namespace,
        configured_resolvers=("exact",),
        accept_exact=True,
    )
    miss = core.resolve_request(
        "Does Sarah like private dogs?",
        "telemetry-miss",
        user_id="Sarah",
        namespace=namespace,
        configured_resolvers=("exact",),
    )
    feedback = core.record_resolution_feedback(
        "telemetry-answer",
        "telemetry-feedback",
        "rejected_context",
        learned["statement_id"],
        "private feedback detail",
    )
    telemetry = core.status()["telemetry"]

    assert answer["outcome"] == ResolutionOutcome.ANSWER
    assert replay == answer
    assert miss["outcome"] == ResolutionOutcome.MISS
    assert feedback["idempotent"] is False
    assert telemetry["resolution"]["requests"] == 3
    assert telemetry["resolution"]["executions"] == 2
    assert telemetry["resolution"]["replays"] == 1
    assert telemetry["resolution"]["outcomes"] == {"ANSWER": 2, "EVIDENCE": 0, "MISS": 1}
    assert sum(telemetry["resolution"]["latency"]["buckets"].values()) == 2
    assert telemetry["resolution"]["resources"]["output_bytes"]["total"] > 0
    assert telemetry["resolvers"]["exact"]["invocations"] == 2
    assert telemetry["resolvers"]["exact"]["candidate_contributions"] == 1
    assert telemetry["resolvers"]["exact"]["selected_contributions"] == 1
    assert telemetry["regulator_outcomes"]["rejected_context"] == 1

    encoded = json_dumps(telemetry, sort_keys=True)
    for sensitive in (request, namespace, "Sarah", learned["statement_id"], "private feedback detail"):
        assert sensitive not in encoded


def test_unknown_resolver_and_exhaustion_values_collapse_into_fixed_other_buckets() -> None:
    telemetry = operational_telemetry()
    result = resolution_result(
        outcome=ResolutionOutcome.MISS,
        selected_candidate=empty_candidate(),
        selected_candidate_available=False,
        response_candidates=(),
        evidence=(),
        confidence=0.0,
        confidence_available=False,
        reason_codes=("insufficient_knowledge",),
        frame_diagnostics={},
        resolver_results=(
            resolver_result(
                "private-user-derived-resolver",
                ResolverState.EXHAUSTED,
                reason_code="private-user-derived-reason",
                consumption=budget_consumption(
                    elapsed_ns=5,
                    resolvers=1,
                    exhausted_dimensions=("private-user-derived-dimension",),
                ),
            ),
        ),
        budget=budget_consumption(
            elapsed_ns=7,
            resolvers=1,
            exhausted_dimensions=("private-user-derived-dimension",),
        ),
    )

    record_resolution(telemetry, result, replayed=False)
    snapshot = telemetry_snapshot(telemetry)

    assert snapshot["resolvers"]["other"]["invocations"] == 1
    assert snapshot["resolvers"]["other"]["states"]["exhausted"] == 1
    assert snapshot["resolution"]["budget_exhaustion"]["other"] == 1
    assert "private-user-derived" not in json_dumps(snapshot, sort_keys=True)
