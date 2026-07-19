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
    assert core.flush() is True

    restored = EngramCore.open(store_path=store)
    recalled = restored.propose("What persists?", "proposal-after-restart")

    assert recalled["candidates"][0]["statement_id"] == learned["statement_id"]
    with pytest.raises(ValueError, match="expired"):
        restored.resolve(proposal["proposal_id"], "accepted", statement_id=learned["statement_id"])


def test_core_without_store_reports_that_flush_was_skipped() -> None:
    assert EngramCore().flush() is False
