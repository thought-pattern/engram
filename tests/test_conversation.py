"""Tests for the shared process-local conversation runtime."""

from pytest import raises as pytest_raises

from engram.constants import Tier
from engram.conversation import ConversationRuntime
from engram.core import Engram


def test_runtime_records_initial_bot_text_and_one_observable_turn() -> None:
    engram = Engram()
    engram.store("Hello!", pattern="HELLO", tier=Tier.STATIC)
    runtime = ConversationRuntime(
        engram,
        user_id="Codex",
        initial_bot_text=".",
    )

    event = runtime.send("hello")

    assert event.get("turn", 0) == 1
    assert event.get("response", "") == "Hello!"
    assert event.get("context_changes", {}).get("previous_response", {}).get("before", "") == "."
    assert runtime.inspect().get("session", {}).get("response_history", []) == ["Hello!", "."]
    assert runtime.inspect().get("latest_turn", {}) == event
    # No transcript is kept: responses use the session's bounded history.
    assert "turns" not in runtime.report()


def test_runtime_rejects_empty_or_batch_input() -> None:
    runtime = ConversationRuntime(Engram(), user_id="0")

    with pytest_raises(ValueError, match="one non-empty string"):
        runtime.send("")
    with pytest_raises(ValueError, match="one non-empty string"):
        runtime.send(["first", "second"])


def test_runtime_exposes_learned_fact_provenance_and_recall() -> None:
    engram = Engram()
    engram.store("Go on.", pattern="*", tier=Tier.STATIC)
    runtime = ConversationRuntime(engram, user_id="Alice")

    teaching = runtime.send("Sushi is good.")
    recall = runtime.send("What's good?")
    snapshot = runtime.inspect()

    learned_statements = teaching.get("learned_statements", [])
    assert learned_statements
    assert all(statement.get("introduced_by_user_id", "") == "Alice" for statement in learned_statements)
    assert recall.get("response", "") == "Sushi is good."
    assert snapshot.get("turn_count", 0) == 2
    assert snapshot.get("learned_unique_texts", []) == ["Sushi is good."]


def test_runtime_returns_report_without_writing_files() -> None:
    engram = Engram()
    engram.store("Hello!", pattern="HELLO", tier=Tier.STATIC)
    runtime = ConversationRuntime(engram, user_id="agent", initial_bot_text=".")
    runtime.send("hello")

    report = runtime.report()

    assert report.get("summary", {}).get("exchanges", 0) == 1
    assert report.get("user_id", "") == "agent"
