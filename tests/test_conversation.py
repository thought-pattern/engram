"""Tests for the shared persistent conversation runtime."""

import json

import pytest

from engram.constants import Tier
from engram.conversation import ConversationRuntime, ConversationTurnPlanner
from engram.core import Engram


def test_runtime_records_initial_bot_text_and_one_observable_turn(tmp_path) -> None:
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

    assert event["turn"] == 1
    assert event["response"] == "Hello!"
    assert event["context_changes"]["previous_response"]["before"] == "."
    assert runtime.inspect()["session"]["response_history"] == ["Hello!", "."]
    saved = json.loads(transcript.read_text(encoding="utf-8"))
    assert saved["turns"] == [event]


def test_runtime_rejects_empty_or_batch_input() -> None:
    runtime = ConversationRuntime(Engram(), user_id="0")

    with pytest.raises(ValueError, match="one non-empty string"):
        runtime.send("")
    with pytest.raises(ValueError, match="one non-empty string"):
        runtime.send(["first", "second"])


def test_runtime_exposes_learned_fact_provenance_and_recall() -> None:
    engram = Engram()
    engram.store("Go on.", pattern="*", tier=Tier.STATIC)
    runtime = ConversationRuntime(engram, user_id="Alice")

    teaching = runtime.send("Sushi is good.")
    recall = runtime.send("What's good?")
    snapshot = runtime.inspect()

    assert teaching["learned_statements"]
    assert all(statement["introduced_by_user_id"] == "Alice" for statement in teaching["learned_statements"])
    assert recall["response"] == "Sushi is good."
    assert snapshot["turn_count"] == 2
    assert snapshot["learned_unique_texts"] == ["Sushi is good."]


def test_runtime_writes_json_and_markdown_reports(tmp_path) -> None:
    engram = Engram()
    engram.store("Hello!", pattern="HELLO", tier=Tier.STATIC)
    runtime = ConversationRuntime(engram, user_id="agent", initial_bot_text=".")
    runtime.send("hello")

    output = runtime.write_report(tmp_path / "reports" / "adaptive-chat")

    report = json.loads((tmp_path / "reports" / "adaptive-chat.json").read_text(encoding="utf-8"))
    markdown = (tmp_path / "reports" / "adaptive-chat.md").read_text(encoding="utf-8")
    assert output["summary"]["exchanges"] == 1
    assert report["user_id"] == "agent"
    assert "**Interlocutor:** hello" in markdown


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
    with pytest.raises(StopIteration):
        planner.next_message()


def test_turn_planner_rejects_exhaustion_and_unapproved_repeats() -> None:
    with pytest.raises(ValueError, match="unapproved repeated"):
        ConversationTurnPlanner(["Echo.", "echo"], total_turns=3, farewell="Goodbye.")

    planner = ConversationTurnPlanner(["Only planned thought."], total_turns=4, farewell="Goodbye.")
    assert planner.next_message() == "Only planned thought."
    with pytest.raises(RuntimeError, match="exhausted"):
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
