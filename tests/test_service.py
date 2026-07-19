"""Transport-neutral facade tests shared by CLI, MCP, and future adapters."""

import pytest

from engram.constants import Tier
from engram.core import Engram
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
