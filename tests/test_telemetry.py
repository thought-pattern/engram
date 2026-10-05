"""Bounded Section 15 operational telemetry tests."""

from copy import deepcopy
from json import dumps as json_dumps

from engram.constants import ResolutionOutcome, ResolverState
from engram.resolution import budget_consumption, empty_candidate, resolution_result, resolver_result
from engram.service import EngramCore
from engram.telemetry import EMPTY_OPERATIONAL_TELEMETRY, record_resolution, telemetry_snapshot


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
    learned_id = learned.get("statement_id", "")
    assert learned_id
    feedback = core.record_resolution_feedback(
        "telemetry-answer",
        "telemetry-feedback",
        "rejected_context",
        learned_id,
        "private feedback detail",
    )
    telemetry = core.status().get("telemetry", {})
    resolution = telemetry.get("resolution", {})
    exact = telemetry.get("resolvers", {}).get("exact", {})

    assert answer.get("outcome", ResolutionOutcome.MISS) == ResolutionOutcome.ANSWER
    assert replay == answer
    assert "outcome" in miss
    assert miss.get("outcome", ResolutionOutcome.MISS) == ResolutionOutcome.MISS
    assert "idempotent" in feedback
    assert feedback.get("idempotent", False) is False
    assert resolution.get("requests", 0) == 3
    assert resolution.get("executions", 0) == 2
    assert resolution.get("replays", 0) == 1
    assert resolution.get("outcomes", {}) == {"ANSWER": 2, "EVIDENCE": 0, "MISS": 1}
    assert sum(resolution.get("latency", {}).get("buckets", {}).values()) == 2
    assert resolution.get("resources", {}).get("output_bytes", {}).get("total", 0) > 0
    assert exact.get("invocations", 0) == 2
    assert exact.get("candidate_contributions", 0) == 1
    assert exact.get("selected_contributions", 0) == 1
    assert telemetry.get("regulator_outcomes", {}).get("rejected_context", 0) == 1

    encoded = json_dumps(telemetry, sort_keys=True)
    for sensitive in (request, namespace, "Sarah", learned_id, "private feedback detail"):
        assert sensitive not in encoded


def test_unknown_resolver_and_exhaustion_values_collapse_into_fixed_other_buckets() -> None:
    telemetry = deepcopy(EMPTY_OPERATIONAL_TELEMETRY)
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

    other_resolver = snapshot.get("resolvers", {}).get("other", {})
    assert other_resolver.get("invocations", 0) == 1
    assert other_resolver.get("states", {}).get("exhausted", 0) == 1
    assert snapshot.get("resolution", {}).get("budget_exhaustion", {}).get("other", 0) == 1
    assert "private-user-derived" not in json_dumps(snapshot, sort_keys=True)
