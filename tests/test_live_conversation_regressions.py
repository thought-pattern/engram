"""Regressions taken directly from the adaptive MCP conversation."""

from json import loads as json_loads
from pathlib import Path

from engram import pipeline
from engram.core import Engram

SEED_PATH = Path(__file__).resolve().parent.parent / "data" / "seed.json"


def seeded_engram() -> Engram:
    engram = Engram()
    seed = json_loads(SEED_PATH.read_text(encoding="utf-8"))
    engram.load_static_data(seed.get("pairs", []))
    return engram


def test_compound_introduction_answers_the_actual_question_once() -> None:
    engram = seeded_engram()

    result = pipeline.chat(engram, "I'm Codex. What should I call you?", user_id="Codex")

    assert result["response"] == "You can call me ENGRAM."
    assert result["pattern"] == "WHAT SHOULD I CALL YOU"


def test_explicit_name_introduction_preserves_case() -> None:
    engram = seeded_engram()

    result = pipeline.chat(engram, "My name is Robin.", user_id="Robin")

    assert result["response"] == "Nice to meet you, Robin! I'll remember that."
    assert engram.sessions["Robin"]["predicates"]["username"] == "Robin"


def test_reminder_request_returns_the_previous_user_message() -> None:
    engram = seeded_engram()
    fact = "A simple example is seasonal food: people appreciate a fruit more when it is available only briefly."
    pipeline.chat(engram, fact, user_id="Codex")

    result = pipeline.chat(engram, "Can you remind me what example I just gave?", user_id="Codex")

    assert result["response"] == f"Your previous message was: {fact}"


def test_one_learned_fact_is_one_dynamic_statement() -> None:
    engram = seeded_engram()

    pipeline.chat(engram, "Kyoto is especially interesting in autumn.", user_id="Codex")

    learned = [statement for statement in engram.statements if statement["tier"].value == "DYNAMIC"]
    assert len(learned) == 1
    assert learned[0]["introduced_by_user_id"] == "Codex"
    assert len(learned[0]["pattern_aliases"]) >= 1


def test_learned_fact_supports_natural_knowledge_question() -> None:
    engram = seeded_engram()
    pipeline.chat(engram, "Kyoto is especially beautiful during cherry blossom season.", user_id="Codex")

    result = pipeline.chat(engram, "What do you know about Kyoto?", user_id="Codex")

    assert result["response"] == "Kyoto is especially beautiful during cherry blossom season."


def test_repetition_feedback_overrides_the_broad_you_are_pattern() -> None:
    engram = seeded_engram()
    pipeline.chat(engram, "Limited time creates urgency.", user_id="Codex")

    result = pipeline.chat(
        engram,
        "I've already explained the feeling. You're repeating the same kind of prompt.",
        user_id="Codex",
    )

    assert result["pattern"] == "YOU ARE *"
    assert result["response"] == "You're right - I was repeating myself. Let's take a different approach."


def test_explicit_topic_change_gets_a_relevant_transition() -> None:
    engram = seeded_engram()

    result = pipeline.chat(engram, "Let us change direction and talk about food.", user_id="Codex")

    assert result["pattern"] == "LET US * TALK ABOUT *"
    assert result["response"] == "Sure - let's talk about food."
