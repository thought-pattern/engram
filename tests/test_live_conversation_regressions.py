"""Regressions taken directly from the adaptive MCP conversation."""

from json import loads as json_loads
from pathlib import Path

from engram import pipeline
from engram.constants import Tier
from engram.core import Engram

SEED_PATH = Path(__file__).resolve().parent.parent / "data" / "seed.json"


def _seeded_engram() -> Engram:
    engram = Engram()
    seed = json_loads(SEED_PATH.read_text(encoding="utf-8"))
    engram.sync_corpus(seed.get("pairs", []))
    return engram


def test_compound_introduction_answers_the_actual_question_once() -> bool:
    engram = _seeded_engram()

    result = pipeline.chat(engram, "I'm Codex. What should I call you?", user_id="Codex")

    assert result.get("response", "") == "You can call me ENGRAM."
    assert result.get("pattern", "") == "WHAT SHOULD I CALL YOU"
    return False


def test_explicit_name_introduction_preserves_case() -> bool:
    engram = _seeded_engram()

    result = pipeline.chat(engram, "My name is Robin.", user_id="Robin")

    assert result.get("response", "") == "Nice to meet you, Robin! I'll remember that."
    assert engram.sessions.get("Robin", {}).get("predicates", {}).get("username", "") == "Robin"
    return False


def test_reminder_request_returns_the_previous_user_message() -> bool:
    engram = _seeded_engram()
    fact = "A simple example is seasonal food: people appreciate a fruit more when it is available only briefly."
    pipeline.chat(engram, fact, user_id="Codex")

    result = pipeline.chat(engram, "Can you remind me what example I just gave?", user_id="Codex")

    assert result.get("response", "") == f"Your previous message was: {fact}"
    return False


def test_one_learned_fact_is_one_dynamic_statement() -> bool:
    engram = _seeded_engram()

    pipeline.chat(engram, "Kyoto is especially interesting in autumn.", user_id="Codex")

    learned = [statement for statement in engram.statements if statement.get("tier", Tier.DYNAMIC).value == "DYNAMIC"]
    assert len(learned) == 1
    assert learned[0].get("introduced_by_user_id", "") == "Codex"
    assert len(learned[0].get("pattern_aliases", [])) >= 1
    return False


def test_learned_fact_supports_natural_knowledge_question() -> bool:
    engram = _seeded_engram()
    pipeline.chat(engram, "Kyoto is especially beautiful during cherry blossom season.", user_id="Codex")

    result = pipeline.chat(engram, "What do you know about Kyoto?", user_id="Codex")

    assert result.get("response", "") == "Kyoto is especially beautiful during cherry blossom season."
    return False


def test_repetition_feedback_overrides_the_broad_you_are_pattern() -> bool:
    engram = _seeded_engram()
    pipeline.chat(engram, "Limited time creates urgency.", user_id="Codex")

    result = pipeline.chat(
        engram,
        "I've already explained the feeling. You're repeating the same kind of prompt.",
        user_id="Codex",
    )

    assert result.get("pattern", "") == "YOU ARE *"
    assert result.get("response", "") == "You're right - I was repeating myself. Let's take a different approach."
    return False


def test_explicit_topic_change_gets_a_relevant_transition() -> bool:
    engram = _seeded_engram()

    result = pipeline.chat(engram, "Let us change direction and talk about food.", user_id="Codex")

    assert result.get("pattern", "") == "LET US * TALK ABOUT *"
    assert result.get("response", "") == "Sure - let's talk about food."
    return False
