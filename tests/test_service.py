"""Transport-neutral facade tests shared by CLI, MCP, and future adapters."""

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from threading import Event as threading_Event

from pytest import mark as pytest_mark, raises as pytest_raises

from engram import metrics, sessions
from engram.constants import ANONYMOUS_CONVERSATION_LEASE_SECONDS, MAX_RESPONSE_BYTES, Tier
from engram.core import Engram
from engram.errors import (
    ConflictError,
    ConversationOwnershipError,
    InvalidRequestError,
    LifecycleError,
    ResolutionCancelledError,
    ResourceNotFoundError,
)
from engram.service import EngramCore


def test_optional_graph_execution_does_not_hold_the_core_lock() -> None:
    engine = Engram()
    entered = threading_Event()
    release = threading_Event()

    class BlockingGraph:
        available = True

        def structured_proposition_projections(self, internal_value, *, projection_id, limit):
            del internal_value
            entered.set()
            assert release.wait(timeout=5)
            return []

    engine.internal_graph_client = BlockingGraph()
    core = EngramCore(engine)

    with ThreadPoolExecutor(max_workers=5) as executor:
        graph_future = executor.submit(
            core.resolve_request,
            "Ada",
            "blocking-graph",
            user_id="graph-user",
            configured_resolvers=("structured_graph",),
        )
        assert entered.wait(timeout=5)

        status_future = executor.submit(core.status)
        local_future = executor.submit(
            core.resolve_request,
            "ordinary local request",
            "local-during-graph",
            user_id="local-user",
            configured_resolvers=("exact",),
        )
        same_user_future = executor.submit(
            core.resolve_request,
            "same user local request",
            "same-user-during-graph",
            user_id="graph-user",
            configured_resolvers=("exact",),
        )
        conflicting_retry_future = executor.submit(
            core.resolve_request,
            "different input for the same request ID",
            "blocking-graph",
            user_id="other-user",
            configured_resolvers=("exact",),
        )
        try:
            status = status_future.result(timeout=1)
            local = local_future.result(timeout=1)
            assert same_user_future.done() is False
            assert conflicting_retry_future.done() is False
        finally:
            release.set()

        graph = graph_future.result(timeout=5)
        same_user = same_user_future.result(timeout=5)
        with pytest_raises(ConflictError, match="different input"):
            conflicting_retry_future.result(timeout=5)

    assert status["ready"] is True
    assert local["outcome"].value == "MISS"
    assert graph["outcome"].value == "MISS"
    assert same_user["outcome"].value == "MISS"
    assert graph_future.done() is True


def test_conversation_graph_execution_does_not_hold_the_core_lock() -> None:
    engine = Engram()
    entered = threading_Event()
    release = threading_Event()

    class BlockingGraph:
        available = True

        def execute(self, internal_query, internal_parameters=()):
            del internal_query, internal_parameters
            entered.set()
            assert release.wait(timeout=5)
            return []

    engine.internal_graph_client = BlockingGraph()
    engine.config["graph"]["enabled"] = True
    engine.pattern_matcher.clear()
    core = EngramCore(engine)
    core.start_conversation(user_id="graph-user")

    with ThreadPoolExecutor(max_workers=4) as executor:
        graph_future = executor.submit(core.chat, "graph-user", "What do you know about Ada Lovelace?")
        assert entered.wait(timeout=20)

        status_future = executor.submit(core.status)
        local_future = executor.submit(
            core.resolve_request,
            "ordinary local request",
            "local-during-conversation-graph",
            user_id="local-user",
            configured_resolvers=("exact",),
        )
        same_user_future = executor.submit(
            core.resolve_request,
            "same user local request",
            "same-user-during-conversation-graph",
            user_id="graph-user",
            configured_resolvers=("exact",),
        )
        try:
            status = status_future.result(timeout=1)
            local = local_future.result(timeout=1)
            assert same_user_future.done() is False
        finally:
            release.set()

        graph = graph_future.result(timeout=5)
        same_user = same_user_future.result(timeout=5)

    assert status["ready"] is True
    assert local["outcome"].value == "MISS"
    assert graph["input"] == "What do you know about Ada Lovelace?"
    assert same_user["outcome"].value == "MISS"


def test_unified_resolution_cancellation_is_transient_and_not_cached() -> None:
    core = EngramCore()

    def cancel() -> None:
        raise ResolutionCancelledError("caller cancelled resolution")

    with pytest_raises(ResolutionCancelledError, match="caller cancelled"):
        core.resolve_request("What is Engram?", "resolution-cancelled", cancellation_check=cancel)

    assert "resolution-cancelled" not in core.resolution_requests
    retry = core.resolve_request("What is Engram?", "resolution-cancelled", configured_resolvers=("exact",))
    assert retry["outcome"].value == "MISS"


def test_core_shares_knowledge_while_isolating_user_context() -> None:
    engram = Engram()
    engram.store("Go on.", pattern="*", tier=Tier.STATIC)
    core = EngramCore(engram)
    core.start_conversation("Alice")
    core.start_conversation("Carol")

    core.chat("Alice", "Sushi is good.")
    alice_context = core.inspect_conversation("Alice")["session"]
    carol_turn = core.chat("Carol", "What's good?")

    assert carol_turn["response"] == "Sushi is good."
    assert core.inspect_conversation("Alice")["session"] == alice_context
    assert core.inspect_conversation("Carol")["session"]["previous_response"] == "Sushi is good."
    assert core.inspect_conversation("Carol")["core_status"]["state"] == "running"


def test_stopping_one_conversation_leaves_other_users_active() -> None:
    core = EngramCore()
    core.start_conversation("Alice")
    core.start_conversation("Carol")

    stopped = core.stop_conversation("Alice")

    assert stopped["user_id"] == "Alice"
    with pytest_raises(ValueError, match="Alice"):
        core.get_conversation("Alice")
    assert core.chat("Carol", "Hello")["user_id"] == "Carol"


def test_unknown_user_conversations_use_zero_and_fresh_context() -> None:
    core = EngramCore()
    named_id = " named:π "
    core.start_conversation(named_id, initial_bot_text="Named context.")
    first = core.start_conversation("0", initial_bot_text="Explicit zero context.")
    first_token = first.get("conversation_token", "")
    assert first_token
    with pytest_raises(ConflictError):
        core.start_conversation("")
    with pytest_raises(ConversationOwnershipError):
        core.stop_conversation("0")
    with pytest_raises(ConversationOwnershipError):
        core.chat("0", "Taking over someone else's anonymous conversation.", conversation_token="wrong")
    assert (
        core.inspect_conversation("0", conversation_token=first_token).get("session", {}).get("previous_response", "")
        == "Explicit zero context."
    )
    core.stop_conversation("0", conversation_token=first_token)

    started = core.start_conversation("")
    runtime = core.get_conversation("")
    token = started.get("conversation_token", "")

    assert started.get("user_id", "") == "0"
    assert token and token != first_token
    assert runtime.session_id == "0"
    assert core.inspect_conversation("", conversation_token=token).get("session", {}).get("previous_response", "") == ""
    assert core.inspect_conversation("0", conversation_token=token).get("session", {}).get("previous_response", "") == ""
    assert core.finish_conversation("0", conversation_token=token).get("metrics_baseline", {}).get("session_count", 0) == 2

    core.conversation_activity["0"] -= ANONYMOUS_CONVERSATION_LEASE_SECONDS
    expired_replacement = core.start_conversation("")
    assert expired_replacement.get("conversation_token", "") not in {"", token}
    core.stop_conversation("", conversation_token=expired_replacement.get("conversation_token", ""))
    core.set_predicate("0", "preserved", "value")
    with pytest_raises(InvalidRequestError):
        core.start_conversation("", initial_bot_text="x" * (MAX_RESPONSE_BYTES + 1))
    assert core.get_predicate("0", "preserved", "missing") == "value"
    assert core.inspect_conversation(named_id).get("user_id", "") == named_id
    core.stop_conversation(named_id)
    core.start_conversation(named_id)
    assert core.inspect_conversation(named_id).get("session", {}).get("previous_response", "") == "Named context."


@pytest_mark.parametrize("failure_site", ["context", "metrics"])
def test_unknown_user_start_restores_existing_session_after_initialization_failure(monkeypatch, failure_site: str) -> None:
    core = EngramCore()
    prior_session = sessions.get_session(core.engram, "0")
    prior_session["previous_response"] = "prior response"
    prior_session["predicates"] = {"preserved": "value"}
    prior_values = deepcopy(prior_session)
    expected = RuntimeError(f"{failure_site} initialization failed")
    cause = ValueError(f"{failure_site} cause")
    replacement_sessions = []
    original_update = sessions.update_session_context

    def failing_update(engram, session_id, previous_response):
        replacement = engram.sessions.get(session_id, {})
        replacement_sessions.append(replacement)
        original_update(engram, session_id, previous_response)
        raise expected from cause

    def failing_metrics(engram):
        raise expected from cause

    if failure_site == "context":
        monkeypatch.setattr(sessions, "update_session_context", failing_update)
    else:
        monkeypatch.setattr(metrics, "get_metrics", failing_metrics)

    with pytest_raises(RuntimeError) as caught:
        core.start_conversation("0", initial_bot_text="replacement response")

    restored = core.engram.sessions.get("0", {})
    assert caught.value is expected
    assert caught.value.__cause__ is cause
    assert restored is prior_session
    assert restored == prior_values
    assert "0" not in core.conversations
    if failure_site == "context":
        assert replacement_sessions
        assert replacement_sessions[0] is not prior_session
        assert replacement_sessions[0].get("previous_response", "") == "replacement response"


def test_restart_discards_receipts_responses_and_conversations() -> None:
    first_engram = Engram()
    first_engram.load_static_data([{"pattern": "HELLO", "response": "Hello from static data."}])
    first = EngramCore(first_engram)
    created = first.learn_response("What is cached?", "Process-local response.", "learn-restart", user_id="Alice")
    first.engram.store("Process-local statement.", tier=Tier.DYNAMIC)
    first.start_conversation("Alice")

    restarted_engram = Engram()
    restarted_engram.load_static_data([{"pattern": "HELLO", "response": "Hello from static data."}])
    restarted = EngramCore(restarted_engram)

    assert restarted.engram.pattern_query("hello")[2] == "Hello from static data."
    assert [statement for statement in restarted.engram.statements if statement.get("tier") == Tier.DYNAMIC] == []
    assert restarted.engram.response_repository.snapshot()["artifacts"] == {}
    assert restarted.engram.mutation_receipts.next_sequence == 1
    assert restarted.engram.sessions == {}
    assert restarted.proposals == {}
    assert restarted.proposal_requests == {}
    recreated = restarted.learn_response("What is cached?", "Process-local response.", "learn-restart", user_id="Alice")
    assert recreated["statement_id"] == created["statement_id"]
    assert recreated["idempotent"] is False


def test_transient_proposals_do_not_cross_process_restart() -> None:
    core = EngramCore()
    learned = core.learn_response("What is cached?", "The process-local answer.", "learn-process")
    proposal = core.propose("What is cached?", "proposal-before-restart")

    restarted = EngramCore()

    assert restarted.engram.response_repository.snapshot().get("artifacts") == {}
    with pytest_raises(ResourceNotFoundError, match="proposal"):
        restarted.resolve(
            proposal.get("proposal_id", ""),
            "accepted",
            statement_id=learned.get("statement_id", ""),
        )


def test_close_is_idempotent_and_blocks_subsequent_operations() -> None:
    core = EngramCore()
    core.start_conversation("Alice")

    assert core.status()["state"] == "running"
    assert core.status()["ready"] is True
    assert core.close() is True
    assert core.close() is False

    status = core.status()
    assert status["state"] == "closed"
    assert status["ready"] is False
    assert status["healthy"] is False
    with pytest_raises(LifecycleError, match="closed"):
        core.start_conversation("Carol")
    with pytest_raises(LifecycleError, match="closed"):
        core.add_fact("A fact after closure.")


def test_close_waits_for_an_active_core_operation() -> None:
    engram = Engram()
    engram.store("Hello!", pattern="HELLO", tier=Tier.STATIC)
    core = EngramCore(engram)
    core.start_conversation("Alice")
    runtime = core.get_conversation("Alice")
    original_send = runtime.send
    entered = threading_Event()
    release = threading_Event()

    def delayed_send(text: object) -> dict:
        entered.set()
        assert release.wait(timeout=5)
        result = original_send(text)
        return result

    runtime.send = delayed_send
    with ThreadPoolExecutor(max_workers=2) as executor:
        chat_future = executor.submit(core.chat, "Alice", "hello")
        assert entered.wait(timeout=5)
        close_future = executor.submit(core.close)
        assert close_future.done() is False
        release.set()
        assert chat_future.result(timeout=5)["response"] == "Hello!"
        assert close_future.result(timeout=5) is True

    assert core.status()["state"] == "closed"
