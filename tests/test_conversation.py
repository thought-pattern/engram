"""Tests for the shared persistent conversation runtime."""

from json import loads as json_loads

from pytest import raises as pytest_raises

from engram.constants import Tier
from engram.conversation import ConversationRuntime, ConversationTurnPlanner
from engram.core import Engram


def test_runtime_records_initial_bot_text_and_one_observable_turn(tmp_path) -> bool:
    engram = Engram()
    engram.store("Hello!", pattern="HELLO", tier=Tier.STATIC)
    transcript = tmp_path / "runtime-transcript.json"
    runtime = ConversationRuntime(
        engram,
        user_id="Codex",
        initial_bot_text=".",
        transcript_path=transcript,
    )

    event = runtime.send("hello")

    assert event.get("turn", 0) == 1
    assert event.get("response", "") == "Hello!"
    assert event.get("context_changes", {}).get("previous_response", {}).get("before", "") == "."
    assert runtime.inspect().get("session", {}).get("response_history", []) == ["Hello!", "."]
    saved = json_loads(transcript.read_text(encoding="utf-8"))
    assert saved.get("turns", []) == [event]
    return False


def test_runtime_rejects_empty_or_batch_input() -> bool:
    runtime = ConversationRuntime(Engram(), user_id="0")

    with pytest_raises(ValueError, match="one non-empty string"):
        runtime.send("")
    with pytest_raises(ValueError, match="one non-empty string"):
        runtime.send(["first", "second"])
    return False


def test_runtime_exposes_learned_fact_provenance_and_recall() -> bool:
    engram = Engram()
    engram.store("Go on.", pattern="*", tier=Tier.STATIC)
    runtime = ConversationRuntime(engram, user_id="Alice")

    teaching = runtime.send("Sushi is good.")
    recall = runtime.send("What's good?")
    snapshot = runtime.inspect()

    assert teaching.get("learned_statements", [])
    assert all(statement.get("introduced_by_user_id", "") == "Alice" for statement in teaching.get("learned_statements", []))
    assert recall.get("response", "") == "Sushi is good."
    assert snapshot.get("turn_count", 0) == 2
    assert snapshot.get("learned_unique_texts", []) == ["Sushi is good."]
    return False


def test_runtime_writes_json_and_markdown_reports(tmp_path) -> bool:
    engram = Engram()
    engram.store("Hello!", pattern="HELLO", tier=Tier.STATIC)
    runtime = ConversationRuntime(engram, user_id="agent", initial_bot_text=".")
    runtime.send("hello")

    output = runtime.write_report(tmp_path / "reports" / "adaptive-chat")

    report = json_loads((tmp_path / "reports" / "adaptive-chat.json").read_text(encoding="utf-8"))
    markdown = (tmp_path / "reports" / "adaptive-chat.md").read_text(encoding="utf-8")
    assert output.get("summary", {}).get("exchanges", 0) == 1
    assert report.get("user_id", "") == "agent"
    assert "**Interlocutor:** hello" in markdown
    return False


def test_turn_planner_preserves_messages_and_reserves_the_farewell() -> bool:
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
    return False


def test_turn_planner_rejects_exhaustion_and_unapproved_repeats() -> bool:
    with pytest_raises(ValueError, match="unapproved repeated"):
        ConversationTurnPlanner(["Echo.", "echo"], total_turns=3, farewell="Goodbye.")

    planner = ConversationTurnPlanner(["Only planned thought."], total_turns=4, farewell="Goodbye.")
    assert planner.next_message() == "Only planned thought."
    with pytest_raises(RuntimeError, match="exhausted"):
        planner.next_message()
    return False


def test_turn_planner_allows_intentional_recall_repetition() -> bool:
    planner = ConversationTurnPlanner(
        ["What is my name?", "What is my name?"],
        total_turns=3,
        farewell="Goodbye.",
        allowed_repeats=["What is my name?"],
    )

    assert planner.next_message() == "What is my name?"
    assert planner.next_message() == "What is my name?"
    assert planner.next_message() == "Goodbye."
    return False
