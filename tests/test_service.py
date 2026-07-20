"""Transport-neutral facade tests shared by CLI, MCP, and future adapters."""

import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

import engram.service as service_module
from engram.constants import Tier
from engram.core import Engram
from engram.errors import ConflictError, InvalidRequestError, LifecycleError, PersistenceError, ResourceNotFoundError
from engram.service import EngramCore


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
    with pytest.raises(ValueError, match="Alice"):
        core.get_conversation("Alice")
    assert core.chat("Carol", "Hello")["user_id"] == "Carol"


def test_regulated_cache_does_not_require_a_chat_conversation() -> None:
    core = EngramCore()
    learned = core.learn_response(
        "When are you open?",
        "Support is open from nine to five.",
        "learn-1",
        user_id="Alice",
        namespace="support",
    )
    proposal = core.propose(
        "When are you open?",
        "proposal-1",
        user_id="Carol",
        namespace="support",
    )

    resolved = core.resolve(
        proposal["proposal_id"],
        "accepted",
        statement_id=learned["statement_id"],
    )

    assert resolved["resolved"] is True
    assert core.engram.sessions["Carol"]["previous_response"] == "Support is open from nine to five."


def test_core_flush_restores_shared_state_but_not_transient_proposals(tmp_path) -> None:
    store = tmp_path / "engram.json"
    core = EngramCore(Engram(), store_path=store)
    learned = core.learn_response("What persists?", "The answer persists.", "learn-persist")
    proposal = core.propose("What persists?", "proposal-before-restart")
    assert store.exists()

    restored = EngramCore.open(store_path=store)
    recalled = restored.propose("What persists?", "proposal-after-restart")

    assert recalled["candidates"][0]["statement_id"] == learned["statement_id"]
    with pytest.raises(ValueError, match="expired"):
        restored.resolve(proposal["proposal_id"], "accepted", statement_id=learned["statement_id"])


def test_core_without_store_reports_that_flush_was_skipped() -> None:
    assert EngramCore().flush() is False


def test_core_checkpoints_each_durable_mutation(tmp_path) -> None:
    store = tmp_path / "engram.json"
    engram = Engram()
    engram.store("Hello!", pattern="HELLO", tier=Tier.STATIC)
    core = EngramCore(engram, store_path=store)

    core.start_conversation("Alice")
    assert "Alice" in EngramCore.open(store_path=store).engram.sessions

    core.chat("Alice", "hello")
    assert EngramCore.open(store_path=store).engram.sessions["Alice"]["previous_response"] == "Hello!"

    core.set_predicate("Alice", "mood", "curious")
    assert EngramCore.open(store_path=store).engram.sessions["Alice"]["predicates"]["mood"] == "curious"

    fact = core.add_fact("Tokyo is the capital of Japan.", source_label="research")
    assert EngramCore.open(store_path=store).engram.get_statement(fact["id"])["source_label"] == "research"

    learned = core.learn_response("What is cached?", "A cached answer.", "learn-checkpoint")
    proposal = core.propose("What is cached?", "proposal-checkpoint")
    proposed_state = EngramCore.open(store_path=store).engram.get_statement(learned["statement_id"])
    assert proposed_state["query_count"] == 1

    core.resolve(proposal["proposal_id"], "accepted", statement_id=learned["statement_id"])
    accepted_state = EngramCore.open(store_path=store).engram.get_statement(learned["statement_id"])
    assert accepted_state["hit_count"] == 1

    core.retire_response(learned["statement_id"], "superseded", "retire-checkpoint")
    assert EngramCore.open(store_path=store).engram.get_statement(learned["statement_id"]) == {}


def test_context_manager_flushes_when_mutation_checkpointing_is_disabled(tmp_path) -> None:
    store = tmp_path / "engram.json"

    with EngramCore(Engram(), store_path=store, checkpoint_on_mutation=False) as core:
        fact = core.add_fact("A deferred fact.")
        assert not store.exists()

    assert EngramCore.open(store_path=store).engram.get_statement(fact["id"])["text"] == "A deferred fact."


def test_core_exposes_stable_request_and_resource_errors() -> None:
    core = EngramCore()

    with pytest.raises(ResourceNotFoundError, match="Alice"):
        core.chat("Alice", "hello")
    with pytest.raises(InvalidRequestError, match="limit"):
        core.propose("question", "bad-limit", limit=0)
    with pytest.raises(ResourceNotFoundError, match="proposal"):
        core.resolve("missing", "accepted", statement_id="missing")

    core.start_conversation("Alice")
    with pytest.raises(ConflictError, match="already active"):
        core.start_conversation("Alice")


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
    with pytest.raises(LifecycleError, match="closed"):
        core.start_conversation("Carol")
    with pytest.raises(LifecycleError, match="closed"):
        core.add_fact("A fact after closure.")
    with pytest.raises(LifecycleError, match="closed"):
        core.flush()


def test_close_waits_for_an_active_core_operation() -> None:
    engram = Engram()
    engram.store("Hello!", pattern="HELLO", tier=Tier.STATIC)
    core = EngramCore(engram)
    core.start_conversation("Alice")
    runtime = core.get_conversation("Alice")
    original_send = runtime.send
    entered = threading.Event()
    release = threading.Event()

    def delayed_send(text: str) -> dict:
        entered.set()
        assert release.wait(timeout=5)
        return original_send(text)

    runtime.send = delayed_send
    with ThreadPoolExecutor(max_workers=2) as executor:
        chat_future = executor.submit(core.chat, "Alice", "hello")
        assert entered.wait(timeout=5)
        close_future = executor.submit(core.close, flush=False)
        assert close_future.done() is False
        release.set()
        assert chat_future.result(timeout=5)["response"] == "Hello!"
        assert close_future.result(timeout=5) is True

    assert core.status()["state"] == "closed"


def test_checkpoint_failure_reports_degraded_state_and_recovers(tmp_path, monkeypatch) -> None:
    store = tmp_path / "engram.json"
    core = EngramCore(Engram(), store_path=store)
    real_save = service_module.persistence.save

    def fail_save(engram, path) -> None:
        raise OSError("disk unavailable")

    monkeypatch.setattr(service_module.persistence, "save", fail_save)
    with pytest.raises(PersistenceError) as failure:
        core.learn_response("What is cached?", "A durable answer.", "learn-degraded")

    assert failure.value.state_changed is True
    assert failure.value.operation == "store checkpoint"
    degraded = core.status()
    assert degraded["state"] == "running"
    assert degraded["ready"] is True
    assert degraded["healthy"] is False
    assert degraded["durability"] == "degraded"
    assert degraded["dirty"] is True
    assert "disk unavailable" in degraded["last_persistence_error"]

    monkeypatch.setattr(service_module.persistence, "save", real_save)
    retry = core.learn_response("What is cached?", "A durable answer.", "learn-degraded")

    assert retry["idempotent"] is True
    recovered = core.status()
    assert recovered["healthy"] is True
    assert recovered["durability"] == "healthy"
    assert recovered["dirty"] is False
    assert recovered["last_checkpoint_at"]
    assert recovered["last_persistence_error"] == ""
    assert EngramCore.open(store_path=store).engram.get_statement(retry["statement_id"])["text"] == "A durable answer."


def test_failed_close_returns_core_to_running_for_flush_recovery(tmp_path, monkeypatch) -> None:
    store = tmp_path / "engram.json"
    core = EngramCore(Engram(), store_path=store, checkpoint_on_mutation=False)
    core.add_fact("Pending state.")
    real_save = service_module.persistence.save

    def fail_save(engram, path) -> None:
        raise OSError("read only")

    monkeypatch.setattr(service_module.persistence, "save", fail_save)
    with pytest.raises(PersistenceError) as failure:
        core.close()

    assert failure.value.state_changed is True
    assert core.status()["state"] == "running"
    assert core.status()["durability"] == "degraded"

    monkeypatch.setattr(service_module.persistence, "save", real_save)
    assert core.close() is True
    assert EngramCore.open(store_path=store).engram.statements[0]["text"] == "Pending state."
