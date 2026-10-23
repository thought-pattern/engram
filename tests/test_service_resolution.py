"""EngramCore resolution through the service boundary: exact answers, transient accounting, and receipts."""

from collections.abc import Mapping

from pytest import raises as pytest_raises

from engram import service as service_module
from engram.artifacts import validate_cached_response_artifact
from engram.constants_resolution import RESOLUTION_RESULT_FIELDS, ResolutionOutcome
from engram.core import Engram
from engram.errors import ConflictError
from engram.repository import ArtifactRepository
from engram.resolution_budget import resolution_budget
from engram.service import EngramCore
from tests.resolver_fixtures import (
    ACCEPTED_ARTIFACT,
)


def test_core_orchestration_exact_answer_is_deterministic_and_retry_safe() -> None:
    engine = Engram()
    engine.response_repository = ArtifactRepository((ACCEPTED_ARTIFACT,))
    core = EngramCore(engine)

    first = core.resolve_request("Explain Engram", "request-exact", namespace="tenant-a", accept_exact=True)
    replay = core.resolve_request("Explain Engram", "request-exact", namespace="tenant-a", accept_exact=True)

    assert first == replay
    assert first is not replay
    assert set(first) == RESOLUTION_RESULT_FIELDS
    assert first.get("outcome", ResolutionOutcome.MISS) == ResolutionOutcome.ANSWER
    assert first.get("selected_candidate", {}).get("response", "") == "Engram preserves exact text: café ☕."
    assert first.get("evidence_package_available", False) is False
    assert first.get("evidence_package", {}).get("records", ()) == ()
    assert [result.get("resolver", "") for result in first.get("resolver_results", ())] == ["exact"]
    updated_statistics = core.engram.response_repository.get_artifact("stmt-accepted").get("statistics", {})
    assert updated_statistics.get("query_count", 0) == 1
    assert updated_statistics.get("hit_count", 0) == 1
    assert core.engram.get_statement("stmt-accepted") == {}
    first["reason_codes"] = ("caller_mutation",)
    first_budget = first.get("budget", {})
    first_budget["candidates"] = 999
    isolated_replay = core.resolve_request("Explain Engram", "request-exact", namespace="tenant-a", accept_exact=True)
    assert isolated_replay == replay
    with pytest_raises(ConflictError, match="different input"):
        core.resolve_request("Different request", "request-exact", namespace="tenant-a", accept_exact=True)


def test_core_result_cache_eviction_discards_matching_transient_accounting(monkeypatch) -> None:
    monkeypatch.setattr(service_module, "MAX_TRANSIENT_RECORDS", 1)
    core = EngramCore(Engram())

    core.resolve_request("first miss", "request-first", configured_resolvers=("exact",))
    core.resolve_request("second miss", "request-second", configured_resolvers=("exact",))
    replay = core.resolve_request("first miss", "request-first", configured_resolvers=("exact",))

    assert "outcome" in replay
    assert replay.get("outcome", ResolutionOutcome.MISS) == ResolutionOutcome.MISS
    assert tuple(core.resolution_requests) == ("request-first",)
    assert tuple(core.resolution_accounting.internal_requests) == ()


def test_output_budget_downgrade_does_not_record_accepted_success() -> None:
    engine = Engram()
    engine.response_repository = ArtifactRepository(
        (validate_cached_response_artifact({**ACCEPTED_ARTIFACT, "response": "x" * 1_000}),)
    )
    core = EngramCore(engine)

    result = core.resolve_request(
        "What is Engram?",
        "request-output-downgrade",
        namespace="tenant-a",
        accept_exact=True,
        budget=resolution_budget(max_output_bytes=4_096),
    )

    updated_statistics = core.engram.response_repository.get_artifact("stmt-accepted").get("statistics", {})
    accounting = result.get("frame_diagnostics", {}).get("accounting", {})
    assert isinstance(accounting, Mapping)
    assert set(result) == RESOLUTION_RESULT_FIELDS
    assert "success_applied" in accounting
    assert "hit_count" in updated_statistics
    assert result.get("outcome", ResolutionOutcome.MISS) == ResolutionOutcome.MISS
    assert "answer_exceeds_output_budget" in result.get("reason_codes", ())
    assert accounting.get("success_applied", False) is False
    assert updated_statistics.get("query_count", 0) == 1
    assert updated_statistics.get("hit_count", 0) == 0


def test_exact_accounting_receipt_is_scoped_to_one_process() -> None:
    first_engine = Engram()
    first_engine.response_repository = ArtifactRepository((ACCEPTED_ARTIFACT,))
    first_core = EngramCore(first_engine)
    first_core.resolve_request("Explain Engram", "request-restart", namespace="tenant-a", accept_exact=True)
    restarted_engine = Engram()
    restarted_engine.response_repository = ArtifactRepository((ACCEPTED_ARTIFACT,))
    restarted_core = EngramCore(restarted_engine)

    replay = restarted_core.resolve_request(
        "Explain Engram",
        "request-restart",
        namespace="tenant-a",
        accept_exact=True,
    )

    updated_statistics = restarted_core.engram.response_repository.get_artifact("stmt-accepted").get("statistics", {})
    assert replay.get("outcome", ResolutionOutcome.MISS) == ResolutionOutcome.ANSWER
    assert updated_statistics.get("query_count", 0) == 1
    assert updated_statistics.get("hit_count", 0) == 1
    assert restarted_core.engram.query_count == 0
    assert restarted_core.engram.hit_count == 0
