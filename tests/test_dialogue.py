"""Dialogue interpretation and conversational-state regressions."""

import json
from pathlib import Path

from engram import persistence, pipeline
from engram.core import Engram
from engram.dialogue import (
    DIALOGUE_ACKNOWLEDGMENT,
    DIALOGUE_CLOSING,
    DIALOGUE_FACT,
    DIALOGUE_GRATITUDE,
    DIALOGUE_QUESTION,
    DIALOGUE_STATEMENT,
    DIALOGUE_TOPIC_SHIFT,
    classify_dialogue_act,
    contextual_fallback_options,
    conversational_fact_admission,
    conversational_fact_is_admissible,
    explicit_topic,
    extract_dialogue_entities,
    infer_active_topic,
    repeated_input_response_options,
    select_turn_candidate,
    topic_from_statement_pattern,
    topic_is_referenced,
)
from engram.models import Tier, session as make_session, session_update_dialogue
from engram.nlp import extract_fact

SEED_PATH = Path(__file__).resolve().parent.parent / "data" / "seed.json"


def _seeded_engram() -> Engram:
    engram = Engram()
    seed = json.loads(SEED_PATH.read_text(encoding="utf-8"))
    engram.sync_corpus(seed["pairs"])
    return engram


def test_dialogue_acts_classifies_common_conversational_moves() -> None:
    assert classify_dialogue_act("Thank you.") == DIALOGUE_GRATITUDE
    assert classify_dialogue_act("That is enough for today.") == DIALOGUE_CLOSING
    assert classify_dialogue_act("Can we talk about cities?") == DIALOGUE_TOPIC_SHIFT
    assert classify_dialogue_act("Let's return to Kyoto.") == DIALOGUE_TOPIC_SHIFT
    assert classify_dialogue_act("What is Kyoto?") == DIALOGUE_QUESTION
    assert classify_dialogue_act("Exactly.") == DIALOGUE_ACKNOWLEDGMENT


def test_dialogue_acts_extracted_fact_is_a_fact_act() -> None:
    fact = extract_fact("Sushi is good.")

    assert classify_dialogue_act("Sushi is good.", fact) == DIALOGUE_FACT


def test_dialogue_acts_whole_turn_selection_does_not_let_courtesy_hide_question() -> None:
    question = {"dialogue_act": DIALOGUE_QUESTION, "response": "ENGRAM"}
    gratitude = {"dialogue_act": DIALOGUE_GRATITUDE, "response": "Welcome"}

    assert select_turn_candidate([question, gratitude]) is question


def test_dialogue_acts_closing_remains_sticky_through_farewell_elaboration() -> None:
    closing = {"dialogue_act": DIALOGUE_CLOSING, "response": "Goodbye"}
    statement = {"dialogue_act": DIALOGUE_STATEMENT, "response": "Kind words"}
    question = {"dialogue_act": DIALOGUE_QUESTION, "response": "Answer"}

    assert select_turn_candidate([closing, statement]) is closing
    assert select_turn_candidate([closing, statement, question]) is question


def test_topic_and_entity_interpretation_fact_subject_becomes_topic_and_entity() -> None:
    fact = extract_fact("Kyoto is beautiful in spring.")
    topic = infer_active_topic("Kyoto is beautiful in spring.", fact=fact)
    entities = extract_dialogue_entities("Kyoto is beautiful in spring.", fact=fact, topic=topic)

    assert topic == "Kyoto"
    assert {entity["text"] for entity in entities} == {"Kyoto"}


def test_topic_and_entity_interpretation_pronoun_keeps_previous_topic() -> None:
    assert infer_active_topic("Their brevity makes them memorable.", previous_topic="Cherry blossoms") == "Cherry blossoms"
    assert infer_active_topic("It can sound improvised.", previous_topic="Jazz") == "Jazz"
    assert infer_active_topic("Its scale is appealing.", previous_topic="Dune") == "Dune"
    assert infer_active_topic("She works carefully.", previous_topic="Alice") == "Alice"
    assert infer_active_topic("He works carefully.", previous_topic="Bob") == "Bob"


def test_topic_and_entity_interpretation_topic_cleanup_removes_hedges_and_discourse_trailers() -> None:
    assert explicit_topic("Let us talk about cities for a while.") == "cities"
    fact = extract_fact("Maybe Mars is easy to imagine.")
    assert infer_active_topic("Maybe Mars is easy to imagine.", fact=fact) == "Mars"


def test_topic_and_entity_interpretation_topic_cleanup_removes_qualifiers_discourse_words_and_articles() -> None:
    cases = {
        "Usually sourdough is tangy.": "sourdough",
        "Well, the project is stable.": "project",
        "Today the project is stable.": "project",
    }

    for text, expected_topic in cases.items():
        fact = extract_fact(text)
        assert infer_active_topic(text, fact=fact) == expected_topic
        assert extract_dialogue_entities(text, fact=fact, topic=expected_topic) == [{"text": expected_topic, "label": "SUBJECT"}]


def test_topic_and_entity_interpretation_question_forms_supply_a_new_topic() -> None:
    assert explicit_topic("How large is the universe?") == "universe"
    assert explicit_topic("What first comes to mind when you consider gardens?") == "gardens"
    assert explicit_topic("What is my name?") == ""


def test_topic_and_entity_interpretation_entity_extraction_filters_discourse_words() -> None:
    entities = extract_dialogue_entities("Tell me about Alice. Goodbye. They agree.", topic="Alice")

    assert entities == [{"text": "Alice", "label": "TOPIC"}]


def test_topic_and_entity_interpretation_auxiliary_and_discourse_leads_are_not_topics_or_entities() -> None:
    examples = (
        "Do you like jazz?",
        "Does that make sense?",
        "Are you repeating yourself?",
        "Have a great day.",
        "No, that was better.",
    )

    for text in examples:
        assert infer_active_topic(text) == ""
        assert extract_dialogue_entities(text) == []


def test_topic_and_entity_interpretation_discourse_prefixes_do_not_become_topics_or_entities() -> None:
    examples = (
        "Because the detail connects feeling with structure.",
        "Before we finish, tell me one small thing.",
        "For my part, I liked the recurring idea.",
        "One more thought before we close.",
        "There is something charming about precise machines.",
        "To me, old streets feel carefully edited.",
        "Which of our topics feels most vivid?",
    )

    for text in examples:
        fact = extract_fact(text)
        entities = extract_dialogue_entities(text)
        assert entities == []
        assert infer_active_topic(text, fact=fact, entities=entities) == ""

    text = "For my part, Kyoto still feels vivid."
    entities = extract_dialogue_entities(text)
    assert entities == [{"text": "Kyoto", "label": "PROPER_NOUN"}]
    assert infer_active_topic(text, entities=entities) == "Kyoto"


def test_topic_and_entity_interpretation_capitalized_discourse_frames_preserve_the_referenced_topic() -> None:
    examples = (
        "To answer with brevity and warmth: gardens reward patient attention.",
        "If gardens had a chair, they would choose the one by the window.",
        "In the spirit of useful curiosity: gardens still reward attention.",
        "Allow me one bright sentence in reply: gardens reward attention.",
        "With a small flourish: gardens reward attention.",
        "One compact answer: gardens reward attention.",
    )

    for text in examples:
        entities = extract_dialogue_entities(text)
        assert not {"To", "If", "In", "Allow", "With", "One"} & {entity["text"] for entity in entities}
        assert infer_active_topic(text, entities=entities, previous_topic="gardens") == "gardens"


def test_topic_and_entity_interpretation_named_topic_reference_requires_complete_words() -> None:
    assert not topic_is_referenced("For my part, I prefer symmetry.", "art")
    assert topic_is_referenced("Art rewards patient attention.", "art")


def test_topic_and_entity_interpretation_explicit_ambiguous_topic_name_remains_available() -> None:
    assert explicit_topic("Let us talk about If.") == "If"


def test_topic_and_entity_interpretation_session_entities_are_canonical_by_surface() -> None:
    session = make_session("speaker")
    session_update_dialogue(session, DIALOGUE_STATEMENT, entities=[{"text": "Alice", "label": "PROPER_NOUN"}])
    session_update_dialogue(session, DIALOGUE_FACT, active_topic="Alice", entities=[{"text": "Alice", "label": "SUBJECT"}])

    assert session["entities"] == [{"text": "Alice", "label": "SUBJECT"}]


def test_topic_and_entity_interpretation_recalled_topic_uses_the_original_subject_casing() -> None:
    cases = (
        ("ENIAC", "ENIAC is an early electronic computer.", "ENIAC"),
        ("GRACE HOPPER", "Grace Hopper was a computer scientist.", "Grace Hopper"),
        ("EBAY", "eBay is an online marketplace.", "eBay"),
        ("PACIFIC", "The Pacific is the largest ocean.", "Pacific"),
        ("CAPITAL OF FRANCE", "The capital of France is Paris.", "capital of France"),
    )

    for pattern, statement_text, expected_topic in cases:
        assert topic_from_statement_pattern(pattern, statement_text) == expected_topic


def test_topic_and_entity_interpretation_recalled_topic_retains_compatibility_fallback() -> None:
    assert topic_from_statement_pattern("EARLY COMPUTING") == "Early Computing"


def test_conversational_fact_admission_allows_plain_durable_assertion() -> None:
    fact = extract_fact("Sushi is good.")

    assert conversational_fact_is_admissible(fact, "Sushi is good.")


def test_conversational_fact_admission_rejects_hedged_transient_and_meta_assertions() -> None:
    examples = (
        "Maybe sushi is good.",
        "Lunch is good today.",
        "The current topic is food.",
        "The answer is obvious.",
    )

    for text in examples:
        fact = extract_fact(text)
        assert fact
        assert not conversational_fact_is_admissible(fact, text)


def test_conversational_fact_admission_admission_explains_rejection_reason() -> None:
    cases = {
        "Maybe sushi is good.": "hedged",
        "Lunch is good today.": "transient",
        "The thought is useful.": "meta_subject",
        "Yes, sushi was what I meant.": "discourse_subject",
        "Sometimes simple food is better.": "qualified_subject",
        "There is something hopeful about drawing a route.": "discourse_subject",
        "Because platforms are temporary meeting places.": "discourse_subject",
    }

    for text, expected_reason in cases.items():
        assert conversational_fact_admission(extract_fact(text), text) == {
            "admitted": False,
            "reason": expected_reason,
        }


def test_conversation_integration_topic_shift_uses_the_normalized_topic() -> None:
    engram = _seeded_engram()

    result = pipeline.chat(engram, "Let us talk about cities for a while.", user_id="Robin")

    assert result["pattern"] == "LET US TALK ABOUT *"
    assert result["active_topic"] == "cities"
    assert result["response"] == "Sure - let's talk about cities."


def test_conversation_integration_return_to_is_an_explicit_topic_shift() -> None:
    engram = _seeded_engram()
    pipeline.chat(engram, "Kyoto is beautiful in spring.", user_id="Robin")
    pipeline.chat(engram, "Dune is a science fiction novel.", user_id="Robin")

    result = pipeline.chat(engram, "Let's return to Kyoto.", user_id="Robin")

    assert result["dialogue_act"] == DIALOGUE_TOPIC_SHIFT
    assert result["active_topic"] == "Kyoto"
    assert result["response"] == "Sure - let's talk about Kyoto."


def test_conversation_integration_discourse_frames_do_not_displace_an_established_topic() -> None:
    engram = _seeded_engram()
    result = pipeline.chat(engram, "What first comes to mind when you consider gardens?", user_id="Robin")
    assert result["active_topic"] == "gardens"
    examples = (
        "To answer with brevity and warmth: gardens reward patient attention.",
        "If gardens had a chair, they would choose the one by the window.",
        "In the spirit of useful curiosity: gardens still reward attention.",
        "Allow me one bright sentence in reply: gardens reward attention.",
        "With a small flourish: gardens reward attention.",
        "One compact answer: gardens reward attention.",
        "A patient observer of gardens notices their smaller details.",
    )

    for text in examples:
        result = pipeline.chat(engram, text, user_id="Robin")
        assert result["active_topic"] == "gardens"

    result = pipeline.chat(engram, "Kyoto is beautiful in spring.", user_id="Robin")
    assert result["active_topic"] == "Kyoto"


def test_conversation_integration_closing_act_overrides_broad_that_is_pattern() -> None:
    engram = _seeded_engram()

    result = pipeline.chat(engram, "Thank you. That is enough for today.", user_id="Robin")

    assert result["dialogue_act"] == DIALOGUE_CLOSING
    assert result["response"] == "You're welcome. We can stop here for today."


def test_conversation_integration_closing_survives_a_trailing_farewell_statement() -> None:
    engram = _seeded_engram()

    result = pipeline.chat(
        engram,
        "Goodbye, Engram. You have been lovely company; may your patterns stay lively.",
        user_id="Robin",
    )

    assert result["dialogue_act"] == DIALOGUE_CLOSING
    assert result["response"] == "Of course. We can stop here for today."


def test_conversation_integration_a_request_after_goodbye_reopens_the_turn() -> None:
    engram = _seeded_engram()

    result = pipeline.chat(engram, "Goodbye. What is your name?", user_id="Robin")

    assert result["dialogue_act"] == DIALOGUE_QUESTION
    assert result["response"] == "I'm ENGRAM, an AIML-style chatbot."


def test_conversation_integration_trailing_gratitude_does_not_hide_an_earlier_question() -> None:
    engram = _seeded_engram()

    result = pipeline.chat(engram, "What should I call you? Thank you.", user_id="Robin")

    assert result["dialogue_act"] == DIALOGUE_QUESTION
    assert result["pattern"] == "WHAT SHOULD I CALL YOU"
    assert result["response"] == "You can call me ENGRAM."


def test_conversation_integration_topic_and_entities_are_per_user_and_persistent() -> None:
    engram = _seeded_engram()
    pipeline.chat(engram, "Kyoto is beautiful in spring.", user_id="Alice")
    pipeline.chat(engram, "Hello.", user_id="Carol")

    assert engram.sessions["Alice"]["active_topic"] == "Kyoto"
    assert any(entity["text"] == "Kyoto" for entity in engram.sessions["Alice"]["entities"])
    assert engram.sessions["Carol"]["active_topic"] == ""

    loaded = persistence.load_engram_from_dict(persistence.to_dict(engram))
    assert loaded.sessions["Alice"]["active_topic"] == "Kyoto"
    assert any(entity["text"] == "Kyoto" for entity in loaded.sessions["Alice"]["entities"])


def test_conversation_integration_contextual_fallback_uses_active_topic_and_known_fact() -> None:
    engram = _seeded_engram()
    pipeline.chat(engram, "Cherry blossoms are ephemeral.", user_id="Robin")

    result = pipeline.chat(engram, "Their brevity makes them memorable.", user_id="Robin")

    assert result["active_topic"] == "Cherry blossoms"
    assert "Cherry blossoms" in result["response"]
    assert "Cherry blossoms are ephemeral." in result["response"]


def test_conversation_integration_unrelated_question_replaces_stale_topic() -> None:
    engram = _seeded_engram()
    pipeline.chat(engram, "Saturn is less dense than water.", user_id="Robin")

    result = pipeline.chat(engram, "How large is the universe?", user_id="Robin")

    assert result["active_topic"] == "universe"
    assert "Saturn" not in result["response"]


def test_conversation_integration_unresolved_unrelated_question_clears_topic() -> None:
    engram = _seeded_engram()
    pipeline.chat(engram, "Writing is a form of thinking.", user_id="Robin")

    result = pipeline.chat(engram, "What would you create if you could make one small tool?", user_id="Robin")

    assert result["active_topic"] == ""
    assert "writing" not in result["response"].lower()


def test_conversation_integration_fact_recall_promotes_its_subject_to_active_topic() -> None:
    engram = _seeded_engram()
    pipeline.chat(engram, "Sushi is good.", user_id="Robin")
    pipeline.chat(engram, "Writing is a form of thinking.", user_id="Robin")

    result = pipeline.chat(engram, "What is good?", user_id="Robin")

    assert result["response"] == "Sushi is good."
    assert result["active_topic"] == "Sushi"


def test_conversation_integration_fact_recall_preserves_acronym_topic_casing() -> None:
    engram = _seeded_engram()
    pipeline.chat(engram, "ENIAC is an early electronic computer.", user_id="Robin")

    result = pipeline.chat(engram, "What is ENIAC?", user_id="Robin")

    assert result["response"] == "ENIAC is an early electronic computer."
    assert result["active_topic"] == "ENIAC"


def test_conversation_integration_natural_memory_question_uses_fact_recall() -> None:
    engram = _seeded_engram()
    pipeline.chat(engram, "Alice is an architect.", user_id="Robin")
    pipeline.chat(engram, "Carol is a biologist.", user_id="Robin")

    result = pipeline.chat(engram, "What do you remember about Alice?", user_id="Robin")

    assert result["response"] == "Alice is an architect."
    assert result["active_topic"] == "Alice"


def test_conversation_integration_broad_that_is_pattern_yields_to_grounded_dialogue() -> None:
    engram = _seeded_engram()
    pipeline.chat(engram, "Kyoto is beautiful in spring.", user_id="Robin")

    result = pipeline.chat(engram, "That is the detail I wanted you to retain.", user_id="Robin")

    assert result["pattern"] == "THAT IS *"
    assert result["response"] == "That connects with what you said about Kyoto: Kyoto is beautiful in spring."


def test_conversation_integration_exact_pattern_repetition_uses_an_alternative() -> None:
    engram = _seeded_engram()
    first = pipeline.chat(engram, "Exactly.", user_id="Robin")
    second = pipeline.chat(engram, "Exactly.", user_id="Robin")

    assert first["response"] == "Exactly!"
    assert second["response"] == "Right - I heard you."


def test_conversation_integration_repetition_control_reaches_beyond_three_responses() -> None:
    engram = _seeded_engram()
    first = pipeline.chat(engram, "Exactly.", user_id="Robin")
    for text in ("Hello.", "Thank you.", "What should I call you?", "How are you?"):
        pipeline.chat(engram, text, user_id="Robin")

    repeated = pipeline.chat(engram, "Exactly.", user_id="Robin")

    assert first["response"] == "Exactly!"
    assert repeated["response"] == "Right - I heard you."


def test_conversation_integration_repeated_ordinary_input_gets_a_topic_neutral_continuation() -> None:
    engram = _seeded_engram()
    text = "I like quiet libraries."
    pipeline.chat(engram, text, user_id="Robin")

    repeated = pipeline.chat(engram, text, user_id="Robin")

    assert repeated["response"] == repeated_input_response_options()[0]
    assert "libraries" not in repeated["response"].lower()


def test_conversation_integration_different_topic_shifts_are_not_treated_as_repetition() -> None:
    engram = _seeded_engram()
    first = pipeline.chat(engram, "Let's talk about oceans.", user_id="Robin")
    second = pipeline.chat(engram, "Let's talk about moons.", user_id="Robin")

    assert first["response"] == "Sure - let's talk about oceans."
    assert second["response"] == "Sure - let's talk about moons."


def test_conversation_integration_repeated_name_recall_remains_repeatable() -> None:
    engram = _seeded_engram()
    pipeline.chat(engram, "My name is Mira.", user_id="Robin")
    first = pipeline.chat(engram, "What is my name?", user_id="Robin")
    second = pipeline.chat(engram, "What is my name?", user_id="Robin")

    assert first["response"] == "Your name is Mira."
    assert second["response"] == "Your name is Mira."


def test_conversation_integration_meta_thought_is_not_learned_and_reason_is_visible() -> None:
    engram = _seeded_engram()

    result = pipeline.chat(engram, "The thought is that taste can become a map of memory.", user_id="Robin")

    assert not [statement for statement in engram.statements if statement["tier"] == Tier.DYNAMIC]
    assert result["fact_admissions"] == [
        {
            "text": "The thought is that taste can become a map of memory.",
            "subject": "thought",
            "admitted": False,
            "reason": "meta_subject",
        }
    ]


def test_conversation_integration_transient_chat_assertion_is_not_learned() -> None:
    engram = _seeded_engram()

    result = pipeline.chat(engram, "Lunch is good today.", user_id="Robin")

    assert not [statement for statement in engram.statements if statement["tier"] == Tier.DYNAMIC]
    assert result["fact_admissions"][0]["reason"] == "transient"


def test_conversation_integration_discourse_and_qualified_assertions_are_not_learned() -> None:
    cases = {
        "Yes, sushi was what I meant.": "discourse_subject",
        "Sometimes simple food is better.": "qualified_subject",
    }

    for text, expected_reason in cases.items():
        engram = _seeded_engram()
        result = pipeline.chat(engram, text, user_id="Robin")

        assert not [statement for statement in engram.statements if statement["tier"] == Tier.DYNAMIC]
        assert result["fact_admissions"][0]["reason"] == expected_reason


def test_conversation_integration_unattributed_fact_api_is_intentionally_not_filtered() -> None:
    engram = Engram()

    statement_id = engram.add_fact("Lunch is good today.", source_label="research")

    assert statement_id
    assert engram.get_statement(statement_id)["source_label"] == "research"


def test_contextual_fallback_has_topic_grounded_option() -> None:
    options = contextual_fallback_options(
        DIALOGUE_STATEMENT,
        topic="Kyoto",
        fact_text="Kyoto is beautiful in spring.",
    )

    assert options[0] == "That connects with what you said about Kyoto: Kyoto is beautiful in spring."
