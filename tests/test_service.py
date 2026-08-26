"""Transport-neutral facade tests shared by CLI, MCP, and future adapters."""

from concurrent.futures import ThreadPoolExecutor
from threading import Event as threading_Event

from pytest import raises as pytest_raises

from engram import service as service_module
from engram.config import engram_config
from engram.constants import Tier
from engram.core import Engram
from engram.errors import (
    ConflictError,
    InvalidRequestError,
    LifecycleError,
    PersistenceError,
    ResourceNotFoundError,
)
from engram.service import EngramCore


def test_core_shares_knowledge_while_isolating_user_context() -> bool:
    engram = Engram()
    engram.store("Go on.", pattern="*", tier=Tier.STATIC)
    core = EngramCore(engram)
    core.start_conversation("Alice")
    core.start_conversation("Carol")

    core.chat("Alice", "Sushi is good.")
    alice_context = core.inspect_conversation("Alice").get("session", {})
    carol_turn = core.chat("Carol", "What's good?")

    assert carol_turn.get("response", "") == "Sushi is good."
    assert core.inspect_conversation("Alice").get("session", {}) == alice_context
    assert core.inspect_conversation("Carol").get("session", {}).get("previous_response", "") == "Sushi is good."
    assert core.inspect_conversation("Carol").get("core_status", {}).get("state", "") == "running"
    return False


def test_stopping_one_conversation_leaves_other_users_active() -> bool:
    core = EngramCore()
    core.start_conversation("Alice")
    core.start_conversation("Carol")

    stopped = core.stop_conversation("Alice")

    assert stopped.get("user_id", "") == "Alice"
    with pytest_raises(ValueError, match="Alice"):
        core.get_conversation("Alice")
    assert core.chat("Carol", "Hello").get("user_id", "") == "Carol"
    return False


def test_regulated_cache_does_not_require_a_chat_conversation() -> bool:
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
        proposal.get("proposal_id", ""),
        "accepted",
        statement_id=learned.get("statement_id", ""),
    )

    assert resolved.get("resolved", False) is True
    assert core.engram.sessions.get("Carol", {}).get("previous_response", "") == "Support is open from nine to five."
    return False


def test_core_flush_restores_shared_state_but_not_transient_proposals(tmp_path) -> bool:
    store = tmp_path / "engram.json"
    core = EngramCore(Engram(), store_path=store)
    learned = core.learn_response("What persists?", "The answer persists.", "learn-persist")
    proposal = core.propose("What persists?", "proposal-before-restart")
    assert store.exists()

    restored = EngramCore.open(store_path=store)
    recalled = restored.propose("What persists?", "proposal-after-restart")

    assert recalled.get("candidates", [])[0].get("statement_id", "") == learned.get("statement_id", "")
    with pytest_raises(ValueError, match="expired"):
        restored.resolve(proposal.get("proposal_id", ""), "accepted", statement_id=learned.get("statement_id", ""))
    return False


def test_core_open_restores_stored_config_unless_explicitly_overridden(tmp_path) -> bool:
    store = tmp_path / "engram.json"
    stored_config = engram_config(capacity=37, use_synonyms=False)
    core = EngramCore(Engram(config=stored_config), store_path=store)
    assert core.flush() is True

    restored = EngramCore.open(store_path=store)

    assert restored.engram.config.get("capacity", 0) == 37
    assert restored.engram.config.get("use_synonyms", False) is False

    override = engram_config(capacity=41, use_synonyms=True)
    overridden = EngramCore.open(config=override, store_path=store)

    assert overridden.engram.config.get("capacity", 0) == 41
    assert overridden.engram.config.get("use_synonyms", False) is True
    return False


def test_core_without_store_reports_that_flush_was_skipped() -> bool:
    assert EngramCore().flush() is False
    return False


def test_core_checkpoints_each_durable_mutation(tmp_path) -> bool:
    store = tmp_path / "engram.json"
    engram = Engram()
    engram.store("Hello!", pattern="HELLO", tier=Tier.STATIC)
    core = EngramCore(engram, store_path=store)

    core.start_conversation("Alice")
    assert "Alice" in EngramCore.open(store_path=store).engram.sessions

    core.chat("Alice", "hello")
    assert EngramCore.open(store_path=store).engram.sessions.get("Alice", {}).get("previous_response", "") == "Hello!"

    core.set_predicate("Alice", "mood", "curious")
    assert EngramCore.open(store_path=store).engram.sessions.get("Alice", {}).get("predicates", {}).get("mood", "") == "curious"

    fact = core.add_fact("Tokyo is the capital of Japan.", source_label="research")
    assert EngramCore.open(store_path=store).engram.get_statement(fact.get("id", "")).get("source_label", "") == "research"

    learned = core.learn_response("What is cached?", "A cached answer.", "learn-checkpoint")
    proposal = core.propose("What is cached?", "proposal-checkpoint")
    proposed_state = EngramCore.open(store_path=store).engram.get_statement(learned.get("statement_id", ""))
    assert proposed_state.get("query_count", 0) == 1

    core.resolve(proposal.get("proposal_id", ""), "accepted", statement_id=learned.get("statement_id", ""))
    accepted_state = EngramCore.open(store_path=store).engram.get_statement(learned.get("statement_id", ""))
    assert accepted_state.get("hit_count", 0) == 1

    core.retire_response(learned.get("statement_id", ""), "superseded", "retire-checkpoint")
    assert EngramCore.open(store_path=store).engram.get_statement(learned.get("statement_id", "")) == {}
    return False


def test_context_manager_flushes_when_mutation_checkpointing_is_disabled(tmp_path) -> bool:
    store = tmp_path / "engram.json"

    with EngramCore(Engram(), store_path=store, checkpoint_on_mutation=False) as core:
        fact = core.add_fact("A deferred fact.")
        assert not store.exists()

    assert EngramCore.open(store_path=store).engram.get_statement(fact.get("id", "")).get("text", "") == "A deferred fact."
    return False


def test_core_exposes_stable_request_and_resource_errors() -> bool:
    core = EngramCore()

    with pytest_raises(ResourceNotFoundError, match="Alice"):
        core.chat("Alice", "hello")
    with pytest_raises(InvalidRequestError, match="limit"):
        core.propose("question", "bad-limit", limit=0)
    with pytest_raises(ResourceNotFoundError, match="proposal"):
        core.resolve("missing", "accepted", statement_id="missing")

    core.start_conversation("Alice")
    with pytest_raises(ConflictError, match="already active"):
        core.start_conversation("Alice")
    return False


def test_close_is_idempotent_and_blocks_subsequent_operations() -> bool:
    core = EngramCore()
    core.start_conversation("Alice")

    assert core.status().get("state", "") == "running"
    assert core.status().get("ready", False) is True
    assert core.close() is True
    assert core.close() is False

    status = core.status()
    assert status.get("state", "") == "closed"
    assert status.get("ready", False) is False
    assert status.get("healthy", False) is False
    with pytest_raises(LifecycleError, match="closed"):
        core.start_conversation("Carol")
    with pytest_raises(LifecycleError, match="closed"):
        core.add_fact("A fact after closure.")
    with pytest_raises(LifecycleError, match="closed"):
        core.flush()
    return False


def test_close_waits_for_an_active_core_operation() -> bool:
    engram = Engram()
    engram.store("Hello!", pattern="HELLO", tier=Tier.STATIC)
    core = EngramCore(engram)
    core.start_conversation("Alice")
    runtime = core.get_conversation("Alice")
    original_send = runtime.send
    entered = threading_Event()
    release = threading_Event()

    def delayed_send(text: str) -> dict:
        entered.set()
        assert release.wait(timeout=5)
        _return_value = original_send(text)
        return _return_value

    runtime.send = delayed_send
    with ThreadPoolExecutor(max_workers=2) as executor:
        chat_future = executor.submit(core.chat, "Alice", "hello")
        assert entered.wait(timeout=5)
        close_future = executor.submit(core.close, flush=False)
        assert close_future.done() is False
        release.set()
        assert chat_future.result(timeout=5).get("response", "") == "Hello!"
        assert close_future.result(timeout=5) is True

    assert core.status().get("state", "") == "closed"
    return False


def test_checkpoint_failure_reports_degraded_state_and_recovers(tmp_path, monkeypatch) -> bool:
    store = tmp_path / "engram.json"
    core = EngramCore(Engram(), store_path=store)
    real_save = service_module.persistence.save

    def fail_save(engram, path) -> bool:
        raise OSError("disk unavailable")

    monkeypatch.setattr(service_module.persistence, "save", fail_save)
    with pytest_raises(PersistenceError) as failure:
        core.learn_response("What is cached?", "A durable answer.", "learn-degraded")

    assert failure.value.state_changed is True
    assert failure.value.operation == "store checkpoint"
    degraded = core.status()
    assert degraded.get("state", "") == "running"
    assert degraded.get("ready", False) is True
    assert degraded.get("healthy", False) is False
    assert degraded.get("durability", "") == "degraded"
    assert degraded.get("dirty", False) is True
    assert "disk unavailable" in degraded.get("last_persistence_error", "")

    monkeypatch.setattr(service_module.persistence, "save", real_save)
    retry = core.learn_response("What is cached?", "A durable answer.", "learn-degraded")

    assert retry.get("idempotent", False) is True
    recovered = core.status()
    assert recovered.get("healthy", False) is True
    assert recovered.get("durability", "") == "healthy"
    assert recovered.get("dirty", False) is False
    assert recovered.get("last_checkpoint_at", False)
    assert recovered.get("last_persistence_error", "") == ""
    assert (
        EngramCore.open(store_path=store).engram.get_statement(retry.get("statement_id", "")).get("text", "") == "A durable answer."
    )
    return False


def test_failed_close_returns_core_to_running_for_flush_recovery(tmp_path, monkeypatch) -> bool:
    store = tmp_path / "engram.json"
    core = EngramCore(Engram(), store_path=store, checkpoint_on_mutation=False)
    core.add_fact("Pending state.")
    real_save = service_module.persistence.save

    def fail_save(engram, path) -> bool:
        raise OSError("read only")

    monkeypatch.setattr(service_module.persistence, "save", fail_save)
    with pytest_raises(PersistenceError) as failure:
        core.close()

    assert failure.value.state_changed is True
    assert core.status().get("state", "") == "running"
    assert core.status().get("durability", "") == "degraded"

    monkeypatch.setattr(service_module.persistence, "save", real_save)
    assert core.close() is True
    assert EngramCore.open(store_path=store).engram.statements[0].get("text", "") == "Pending state."
    return False
