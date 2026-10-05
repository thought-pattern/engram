"""Regressions taken directly from the adaptive MCP conversation."""

from engram import pipeline
from engram.constants import Tier
from engram.conversation_seed import load_bundled_conversation_pairs
from engram.core import Engram


def test_compound_introduction_answers_the_actual_question_once() -> None:
    engram = Engram()
    engram.load_static_data(load_bundled_conversation_pairs())

    result = pipeline.chat(engram, "I'm Codex. What should I call you?", user_id="Codex")

    assert result.get("pattern", "") == "WHAT SHOULD I CALL YOU"
    assert "ENGRAM" in result.get("response", "")


def test_explicit_name_introduction_preserves_case() -> None:
    engram = Engram()
    engram.load_static_data(load_bundled_conversation_pairs())

    result = pipeline.chat(engram, "My name is Robin.", user_id="Robin")

    assert "Robin" in result.get("response", "")
    assert engram.sessions.get("Robin", {}).get("predicates", {}).get("username", "") == "Robin"


def test_reminder_request_returns_the_previous_user_message() -> None:
    engram = Engram()
    engram.load_static_data(load_bundled_conversation_pairs())
    fact = "A simple example is seasonal food: people appreciate a fruit more when it is available only briefly."
    pipeline.chat(engram, fact, user_id="Codex")

    result = pipeline.chat(engram, "Can you remind me what example I just gave?", user_id="Codex")

    assert fact in result.get("response", "")


def test_one_learned_fact_is_one_dynamic_statement() -> None:
    engram = Engram()
    engram.load_static_data(load_bundled_conversation_pairs())

    pipeline.chat(engram, "Kyoto is especially interesting in autumn.", user_id="Codex")

    learned = [statement for statement in engram.statements if statement.get("tier", Tier.STATIC).value == "DYNAMIC"]
    assert len(learned) == 1
    assert learned[0].get("introduced_by_user_id", "") == "Codex"
    assert len(learned[0].get("pattern_aliases", [])) >= 1


def test_learned_fact_supports_natural_knowledge_question() -> None:
    engram = Engram()
    engram.load_static_data(load_bundled_conversation_pairs())
    pipeline.chat(engram, "Kyoto is especially beautiful during cherry blossom season.", user_id="Codex")

    result = pipeline.chat(engram, "What do you know about Kyoto?", user_id="Codex")

    assert result.get("response", "") == "Kyoto is especially beautiful during cherry blossom season."


def test_repetition_feedback_keeps_the_you_are_category() -> None:
    engram = Engram()
    engram.load_static_data(load_bundled_conversation_pairs())
    pipeline.chat(engram, "Limited time creates urgency.", user_id="Codex")

    result = pipeline.chat(
        engram,
        "I've already explained the feeling. You're repeating the same kind of prompt.",
        user_id="Codex",
    )

    assert result.get("pattern", "") == "YOU ARE *"
    assert result.get("source", "") == "pattern"
    assert result.get("response", "")


def test_explicit_topic_change_gets_a_relevant_transition() -> None:
    engram = Engram()
    engram.load_static_data(load_bundled_conversation_pairs())

    result = pipeline.chat(engram, "Let us change direction and talk about food.", user_id="Codex")

    assert result.get("pattern", "") == "LET US * TALK ABOUT *"
    assert "food" in result.get("response", "").lower()
