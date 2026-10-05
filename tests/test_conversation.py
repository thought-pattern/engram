"""Tests for the shared process-local conversation runtime."""

from pytest import raises as pytest_raises

from engram.constants import Tier
from engram.conversation import ConversationRuntime, ConversationTurnPlanner
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


def test_turn_planner_preserves_messages_and_reserves_the_farewell() -> None:
    planner = ConversationTurnPlanner(
        ["First planned thought.", "Second planned thought."],
        total_turns=4,
        farewell="Goodbye, and thank you.",
    )

    assert planner.next_message() == "First planned thought."
    assert planner.next_message("An adaptive answer.") == "An adaptive answer."
    assert planner.next_message("This answer no longer fits the budget.") == "Second planned thought."
    assert planner.next_message("Nor does this one.") == "Goodbye, and thank you."
    assert planner.turn_count == 4
    assert planner.remaining_turns == 0
    with pytest_raises(StopIteration):
        planner.next_message()


def test_turn_planner_rejects_exhaustion_and_unapproved_repeats() -> None:
    with pytest_raises(ValueError, match="unapproved repeated"):
        ConversationTurnPlanner(["Echo.", "echo"], total_turns=3, farewell="Goodbye.")

    planner = ConversationTurnPlanner(["Only planned thought."], total_turns=4, farewell="Goodbye.")
    assert planner.next_message() == "Only planned thought."
    with pytest_raises(RuntimeError, match="exhausted"):
        planner.next_message()


def test_turn_planner_allows_intentional_recall_repetition() -> None:
    planner = ConversationTurnPlanner(
        ["What is my name?", "What is my name?"],
        total_turns=3,
        farewell="Goodbye.",
        allowed_repeats=["What is my name?"],
    )

    assert planner.next_message() == "What is my name?"
    assert planner.next_message() == "What is my name?"
    assert planner.next_message() == "Goodbye."
