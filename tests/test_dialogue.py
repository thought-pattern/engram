"""Dialogue interpretation and conversational-state regressions."""

from json import loads as json_loads
from pathlib import Path

from engram import pipeline
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
    conversational_fact_admission,
    explicit_topic,
    extract_dialogue_entities,
    infer_active_topic,
    select_turn_candidate,
    topic_from_statement_pattern,
    topic_is_referenced,
)
from engram.models import Tier, session as make_session, session_update_dialogue
from engram.nlp import extract_fact

SEED_PATH = Path(__file__).resolve().parent.parent / "engram" / "data" / "seed.json"
# Read-only: load_static_data copies every pair into its own statements.
SEED_PAIRS = json_loads(SEED_PATH.read_text(encoding="utf-8")).get("pairs", [])


def test_dialogue_acts_classifies_common_conversational_moves() -> None:
    assert classify_dialogue_act("Thank you.") == DIALOGUE_GRATITUDE
    assert classify_dialogue_act("That is enough for today.") == DIALOGUE_CLOSING
    assert classify_dialogue_act("Can we talk about cities?") == DIALOGUE_TOPIC_SHIFT
    assert classify_dialogue_act("Let's return to Kyoto.") == DIALOGUE_TOPIC_SHIFT
    assert classify_dialogue_act("What is Kyoto?") == DIALOGUE_QUESTION
    assert classify_dialogue_act("Exactly.") == DIALOGUE_ACKNOWLEDGMENT
    assert classify_dialogue_act("That helps.") == DIALOGUE_ACKNOWLEDGMENT
    assert classify_dialogue_act("The cache helps the compiler.") == DIALOGUE_STATEMENT


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
    assert {entity.get("text", "") for entity in entities} == {"Kyoto"}


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
        assert all("text" in entity for entity in entities)
        assert not {"To", "If", "In", "Allow", "With", "One"} & {entity.get("text", "") for entity in entities}
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

    assert session.get("entities", []) == [{"text": "Alice", "label": "SUBJECT"}]


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


def test_topic_and_entity_interpretation_recalled_topic_requires_original_text() -> None:
    assert topic_from_statement_pattern("EARLY COMPUTING") == ""


def test_conversational_fact_admission_allows_plain_durable_assertion() -> None:
    fact = extract_fact("Sushi is good.")

    assert conversational_fact_admission(fact, "Sushi is good.").get("admitted", False)


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
        admission = conversational_fact_admission(fact, text)
        assert "admitted" in admission
        assert not admission.get("admitted", False)


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
    engram = Engram()
    engram.load_static_data(SEED_PAIRS)

    result = pipeline.chat(engram, "Let us talk about cities for a while.", user_id="Robin")

    assert result.get("pattern", "") == "LET US TALK ABOUT *"
    assert result.get("active_topic", "") == "cities"
    assert "cities" in result.get("response", "").lower()


def test_conversation_integration_return_to_is_an_explicit_topic_shift() -> None:
    engram = Engram()
    engram.load_static_data(SEED_PAIRS)
    pipeline.chat(engram, "Kyoto is beautiful in spring.", user_id="Robin")
    pipeline.chat(engram, "Dune is a science fiction novel.", user_id="Robin")

    result = pipeline.chat(engram, "Let's return to Kyoto.", user_id="Robin")

    assert result.get("dialogue_act", "") == DIALOGUE_TOPIC_SHIFT
    assert result.get("active_topic", "") == "Kyoto"
    assert result.get("source", "") == "pattern"


def test_conversation_integration_discourse_frames_do_not_displace_an_established_topic() -> None:
    engram = Engram()
    engram.load_static_data(SEED_PAIRS)
    result = pipeline.chat(engram, "What first comes to mind when you consider gardens?", user_id="Robin")
    assert result.get("active_topic", "") == "gardens"
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
        assert result.get("active_topic", "") == "gardens"

    result = pipeline.chat(engram, "Kyoto is beautiful in spring.", user_id="Robin")
    assert result.get("active_topic", "") == "Kyoto"


def test_conversation_integration_closing_keeps_the_matched_category() -> None:
    engram = Engram()
    engram.load_static_data(SEED_PAIRS)

    result = pipeline.chat(engram, "Thank you. That is enough for today.", user_id="Robin")

    assert result.get("dialogue_act", "") == DIALOGUE_CLOSING
    assert result.get("pattern", "") == "THAT IS *"
    assert result.get("source", "") == "pattern"


def test_conversation_integration_closing_survives_a_trailing_farewell_statement() -> None:
    engram = Engram()
    engram.load_static_data(SEED_PAIRS)

    result = pipeline.chat(
        engram,
        "Goodbye, Engram. You have been lovely company; may your patterns stay lively.",
        user_id="Robin",
    )

    assert result.get("dialogue_act", "") == DIALOGUE_CLOSING
    assert result.get("source", "") == "pattern"
    assert result.get("response", "")


def test_conversation_integration_a_request_after_goodbye_reopens_the_turn() -> None:
    engram = Engram()
    engram.load_static_data(SEED_PAIRS)

    result = pipeline.chat(engram, "Goodbye. What is your name?", user_id="Robin")

    assert result.get("dialogue_act", "") == DIALOGUE_QUESTION
    assert result.get("pattern", "") == "WHAT IS YOUR NAME"
    assert "ENGRAM" in result.get("response", "")


def test_conversation_integration_trailing_gratitude_does_not_hide_an_earlier_question() -> None:
    engram = Engram()
    engram.load_static_data(SEED_PAIRS)

    result = pipeline.chat(engram, "What should I call you? Thank you.", user_id="Robin")

    assert result.get("dialogue_act", "") == DIALOGUE_QUESTION
    assert result.get("pattern", "") == "THANK YOU"
    assert "ENGRAM" in result.get("response", "")


def test_conversation_integration_topic_and_entities_are_per_user_in_process() -> None:
    engram = Engram()
    engram.load_static_data(SEED_PAIRS)
    pipeline.chat(engram, "Kyoto is beautiful in spring.", user_id="Alice")
    pipeline.chat(engram, "Hello.", user_id="Carol")

    alice_session = engram.sessions.get("Alice", {})
    carol_session = engram.sessions.get("Carol", {})
    assert alice_session.get("active_topic", "") == "Kyoto"
    assert any(entity.get("text", "") == "Kyoto" for entity in alice_session.get("entities", []))
    assert "active_topic" in carol_session
    assert carol_session.get("active_topic", "") == ""

    alice_session = engram.sessions.get("Alice", {})
    assert alice_session.get("active_topic", "") == "Kyoto"
    assert any(entity.get("text", "") == "Kyoto" for entity in alice_session.get("entities", []))


def test_conversation_integration_category_is_spoken_while_the_fact_stays_stored() -> None:
    engram = Engram()
    engram.load_static_data(SEED_PAIRS)
    pipeline.chat(engram, "Cherry blossoms are ephemeral.", user_id="Robin")

    result = pipeline.chat(engram, "Their brevity makes them memorable.", user_id="Robin")

    assert result.get("active_topic", "") == "Cherry blossoms"
    assert result.get("source", "") == "pattern"
    learned = [statement for statement in engram.statements if statement.get("tier", Tier.STATIC) == Tier.DYNAMIC]
    assert any("ephemeral" in statement.get("text", "") for statement in learned)


def test_conversation_integration_unrelated_question_replaces_stale_topic() -> None:
    engram = Engram()
    engram.load_static_data(SEED_PAIRS)
    pipeline.chat(engram, "Saturn is less dense than water.", user_id="Robin")

    result = pipeline.chat(engram, "How large is the universe?", user_id="Robin")

    assert result.get("active_topic", "") == "universe"
    assert "response" in result
    assert "Saturn" not in result.get("response", "")


def test_conversation_integration_unresolved_unrelated_question_clears_topic() -> None:
    engram = Engram()
    engram.load_static_data(SEED_PAIRS)
    pipeline.chat(engram, "Writing is a form of thinking.", user_id="Robin")

    result = pipeline.chat(engram, "What would you create if you could make one small tool?", user_id="Robin")

    assert "active_topic" in result
    assert result.get("active_topic", "") == ""
    assert "response" in result
    assert "writing" not in result.get("response", "").lower()


def test_conversation_integration_fact_recall_promotes_its_subject_to_active_topic() -> None:
    engram = Engram()
    engram.load_static_data(SEED_PAIRS)
    pipeline.chat(engram, "Sushi is good.", user_id="Robin")
    pipeline.chat(engram, "Writing is a form of thinking.", user_id="Robin")

    result = pipeline.chat(engram, "What is good?", user_id="Robin")

    assert result.get("response", "") == "Sushi is good."
    assert result.get("active_topic", "") == "Sushi"


def test_conversation_integration_fact_recall_preserves_acronym_topic_casing() -> None:
    engram = Engram()
    engram.load_static_data(SEED_PAIRS)
    pipeline.chat(engram, "ENIAC is an early electronic computer.", user_id="Robin")

    result = pipeline.chat(engram, "What is ENIAC?", user_id="Robin")

    assert result.get("response", "") == "ENIAC is an early electronic computer."
    assert result.get("active_topic", "") == "ENIAC"


def test_conversation_integration_natural_memory_question_uses_fact_recall() -> None:
    engram = Engram()
    engram.load_static_data(SEED_PAIRS)
    pipeline.chat(engram, "Alice is an architect.", user_id="Robin")
    pipeline.chat(engram, "Carol is a biologist.", user_id="Robin")

    result = pipeline.chat(engram, "What do you remember about Alice?", user_id="Robin")

    assert result.get("response", "") == "Alice is an architect."
    assert result.get("active_topic", "") == "Alice"


def test_conversation_integration_broad_that_is_pattern_keeps_its_category() -> None:
    engram = Engram()
    engram.load_static_data(SEED_PAIRS)
    pipeline.chat(engram, "Kyoto is beautiful in spring.", user_id="Robin")

    result = pipeline.chat(engram, "That is the detail I wanted you to retain.", user_id="Robin")

    assert result.get("pattern", "") == "THAT IS *"
    assert result.get("source", "") == "pattern"
    assert result.get("active_topic", "") == "Kyoto"


def test_conversation_integration_exact_pattern_repetition_repeats_the_category() -> None:
    engram = Engram()
    engram.load_static_data(SEED_PAIRS)
    first = pipeline.chat(engram, "Exactly.", user_id="Robin")
    second = pipeline.chat(engram, "Exactly.", user_id="Robin")

    assert second.get("pattern", "") == "EXACTLY"
    assert "response" in first
    assert "response" in second
    assert second.get("response", "") == first.get("response", "")


def test_conversation_integration_repeated_category_survives_later_turns() -> None:
    engram = Engram()
    engram.load_static_data(SEED_PAIRS)
    first = pipeline.chat(engram, "Exactly.", user_id="Robin")
    for text in ("Hello.", "Thank you.", "What should I call you?", "How are you?"):
        pipeline.chat(engram, text, user_id="Robin")

    repeated = pipeline.chat(engram, "Exactly.", user_id="Robin")

    assert repeated.get("pattern", "") == "EXACTLY"
    assert "response" in first
    assert "response" in repeated
    assert repeated.get("response", "") == first.get("response", "")


def test_conversation_integration_repeated_ordinary_input_keeps_the_category() -> None:
    engram = Engram()
    engram.load_static_data(SEED_PAIRS)
    text = "I like quiet libraries."
    first = pipeline.chat(engram, text, user_id="Robin")

    repeated = pipeline.chat(engram, text, user_id="Robin")

    assert first.get("pattern", "") == "I LIKE *"
    assert "libraries" in first.get("response", "").lower()
    assert repeated.get("pattern", "") == "I LIKE *"
    assert "response" in repeated
    assert repeated.get("response", "") != first.get("response", "")
    assert "what do you like about" not in repeated.get("response", "").lower()


def test_sentence_initial_word_is_not_a_name() -> None:
    staying = "Staying with the interpreter for a minute."
    assert extract_dialogue_entities(staying) == []
    assert infer_active_topic(staying) == ""
    assert infer_active_topic(staying, previous_topic="Python") == ""

    alice = extract_dialogue_entities("Alice left.")
    assert alice == [{"text": "Alice", "label": "PROPER_NOUN"}]
    assert infer_active_topic("Alice left.", entities=alice) == "Alice"
    assert any(entity.get("text", "") == "Pacific" for entity in extract_dialogue_entities("The Pacific is the largest ocean."))
    assert extract_dialogue_entities("New York is a city.") == [{"text": "New York", "label": "PROPER_NOUN"}]
    assert explicit_topic("Let us talk about If.") == "If"

    echoed = (
        "Then I tried Continue on one small example.",
        "The first small example of Continue.",
        "Yeah. Continue.",
    )
    for text in echoed:
        entities = extract_dialogue_entities(text)
        assert entities == [], text
        assert infer_active_topic(text, entities=entities, previous_topic="harbor") == ""


def test_discourse_subjects_are_not_facts_or_topics() -> None:
    rejected = {
        "The part about blue is the part to write down.": "vague_subject",
        "The next try is a small example of expressions.": "vague_subject",
        "The next note is about requirements.": "vague_subject",
        "Yesterday's draft was about testing.": "vague_subject",
        "The hard part of Python is knowing when to stop.": "vague_subject",
        "The hard part of whenever is knowing when to stop.": "wh_subject",
    }

    for text, reason in rejected.items():
        fact = extract_fact(text)
        assert fact, text
        assert conversational_fact_admission(fact, text) == {"admitted": False, "reason": reason}
        assert infer_active_topic(text, fact=fact) == ""

    assert explicit_topic("Let us talk about whenever.") == ""


def test_plain_world_facts_still_store_and_name_the_topic() -> None:
    cases = {
        "Sushi is good.": "Sushi",
        "The sky is blue.": "sky",
        "Kyoto is especially beautiful during cherry blossom season.": "Kyoto",
        "The harbor is quiet in the morning.": "harbor",
        "The capital of France is Paris.": "capital of France",
    }

    for text, topic in cases.items():
        fact = extract_fact(text)
        assert fact, text
        assert conversational_fact_admission(fact, text).get("admitted", False)
        assert infer_active_topic(text, fact=fact) == topic

    fact = extract_fact("Maybe Mars is easy to imagine.")
    hedged_admission = conversational_fact_admission(fact, "Maybe Mars is easy to imagine.")
    assert "admitted" in hedged_admission
    assert not hedged_admission.get("admitted", False)
    assert infer_active_topic("Maybe Mars is easy to imagine.", fact=fact) == "Mars"


def test_discourse_sentence_is_not_learned_and_a_world_fact_is() -> None:
    engram = Engram()
    engram.load_static_data(SEED_PAIRS)
    before = len(engram.statements)
    refused = pipeline.chat(engram, "The hard part of whenever is knowing when to stop.", user_id="Robin")

    refused_admissions = refused.get("fact_admissions", [])
    assert len(engram.statements) == before
    assert refused_admissions
    assert "admitted" in refused_admissions[0]
    assert refused_admissions[0].get("admitted", False) is False
    assert "active_topic" in refused
    assert refused.get("active_topic", "") != "whenever"

    admitted = pipeline.chat(engram, "The sky is blue.", user_id="Robin")

    assert any(item.get("admitted", False) for item in admitted.get("fact_admissions", []))
    assert len(engram.statements) > before
    assert admitted.get("active_topic", "") == "sky"


def test_unreferenced_statement_clears_the_topic() -> None:
    engram = Engram()
    engram.load_static_data(SEED_PAIRS)
    pipeline.chat(engram, "Let us talk about Python.", user_id="Robin")

    echoed = pipeline.chat(engram, "The first small example of Continue.", user_id="Robin")
    later = pipeline.chat(engram, "Staying with the interpreter for a minute.", user_id="Robin")

    assert "active_topic" in echoed
    assert echoed.get("active_topic", "") != "Continue"
    assert "active_topic" in later
    assert later.get("active_topic", "") == ""
    assert "entities" in later
    later_entities = later.get("entities", [])
    assert all("text" in entity for entity in later_entities)
    names = {entity.get("text", "") for entity in later_entities}
    assert "Continue" not in names
    assert "Staying" not in names


def test_wildcard_question_follow_up_matches_that() -> None:
    cases = (
        ("I like quiet libraries.", "I like the quiet.", "what do you like about", "Yeah. That's the part that matters."),
        ("I love quiet libraries.", "I love the reading rooms.", "what is it about", "That's a strong one. I heard you."),
        (
            "I think the tutorial is enough.",
            "I think the examples are short.",
            "what makes you think",
            "Okay. I'll take that as your read.",
        ),
        ("I want a small boat.", "I want a harbor slip.", "what would it take", "Okay. That's the aim."),
        ("I need a quieter keyboard.", "I need a small one.", "what kind of", "Okay. That narrows it."),
    )

    for opening, answer, question, follow_up in cases:
        engram = Engram()
        engram.load_static_data(SEED_PAIRS)
        first = pipeline.chat(engram, opening, user_id="Robin")
        second = pipeline.chat(engram, answer, user_id="Robin")
        pipeline.chat(engram, "Hello.", user_id="Robin")
        third = pipeline.chat(engram, opening, user_id="Robin")

        assert "pattern" in first
        assert "pattern" in second
        assert first.get("pattern", "") == second.get("pattern", "")
        assert question in first.get("response", "").lower()
        assert second.get("response", "") == follow_up
        assert question not in second.get("response", "").lower()
        assert third.get("response", "") == first.get("response", "")


def test_exact_question_keeps_its_sentence_when_the_previous_reply_matches() -> None:
    engram = Engram()
    engram.load_static_data(
        [
            {"pattern": "WHAT IS THE PLAN", "response": "The plan paragraph."},
            {"pattern": "HOW SHOULD I START", "response": "The plan paragraph."},
        ]
    )

    first = pipeline.chat(engram, "How should I start?", user_id="Robin")
    second = pipeline.chat(engram, "What is the plan?", user_id="Robin")

    assert second.get("pattern", "") == "WHAT IS THE PLAN"
    assert "response" in first
    assert "response" in second
    assert second.get("response", "") == first.get("response", "")


def test_distinct_wildcard_fills_are_not_replaced_by_a_topic_line() -> None:
    engram = Engram()
    engram.load_static_data(
        [
            {
                "pattern": "HOW DO I *",
                "response": "Tried.",
                "template": {"text": "What have you already tried with {star1}?"},
            }
        ]
    )

    first = pipeline.chat(engram, "How do I design the pieces?", user_id="Robin")
    second = pipeline.chat(engram, "How do I test the pieces?", user_id="Robin")

    assert first.get("pattern", "") == "HOW DO I *"
    assert second.get("pattern", "") == "HOW DO I *"
    assert "already tried" in first.get("response", "").lower()
    assert "already tried" in second.get("response", "").lower()
    assert "repeat myself" not in second.get("response", "").lower()
    assert first.get("response", "") != second.get("response", "")


def test_unmatched_statement_keeps_the_catchall_when_the_topic_has_no_fact() -> None:
    engram = Engram()
    engram.load_static_data(SEED_PAIRS)
    pipeline.chat(engram, "Hello.", user_id="Robin")
    robin_session = engram.sessions.get("Robin", {})
    assert robin_session
    robin_session["active_topic"] = "Python"

    result = pipeline.chat(engram, "It stayed quiet after lunch.", user_id="Robin")

    assert result.get("pattern", "") == "*"
    assert "response" in result
    assert "another angle" not in result.get("response", "").lower()


def test_trailing_courtesy_does_not_replace_an_earlier_question() -> None:
    engram = Engram()
    engram.load_static_data(SEED_PAIRS)

    result = pipeline.chat(engram, "What is your name? That helps.", user_id="Robin")

    assert result.get("pattern", "") == "THAT HELPS"
    assert result.get("dialogue_act", "") == DIALOGUE_QUESTION
    assert "ENGRAM" in result.get("response", "")


def test_thanks_that_helps_matches_the_courtesy_pattern() -> None:
    engram = Engram()
    engram.load_static_data(SEED_PAIRS)

    result = pipeline.chat(engram, "Thanks. That helps.", user_id="Robin")

    assert result.get("pattern", "") == "THAT HELPS"
    assert result.get("dialogue_act", "") == DIALOGUE_ACKNOWLEDGMENT


def test_conversation_integration_different_topic_shifts_are_not_treated_as_repetition() -> None:
    engram = Engram()
    engram.load_static_data(SEED_PAIRS)
    first = pipeline.chat(engram, "Let's talk about oceans.", user_id="Robin")
    second = pipeline.chat(engram, "Let's talk about moons.", user_id="Robin")

    assert "oceans" in first.get("response", "").lower()
    assert "moons" in second.get("response", "").lower()
    assert first.get("response", "") != second.get("response", "")


def test_conversation_integration_repeated_name_recall_remains_repeatable() -> None:
    engram = Engram()
    engram.load_static_data(SEED_PAIRS)
    pipeline.chat(engram, "My name is Mira.", user_id="Robin")
    first = pipeline.chat(engram, "What is my name?", user_id="Robin")
    second = pipeline.chat(engram, "What is my name?", user_id="Robin")

    assert "Mira" in first.get("response", "")
    assert second.get("response", "") == first.get("response", "")


def test_conversation_integration_meta_thought_is_not_learned_and_reason_is_visible() -> None:
    engram = Engram()
    engram.load_static_data(SEED_PAIRS)

    result = pipeline.chat(engram, "The thought is that taste can become a map of memory.", user_id="Robin")

    assert all("tier" in statement for statement in engram.statements)
    assert not [statement for statement in engram.statements if statement.get("tier", Tier.STATIC) == Tier.DYNAMIC]
    assert result.get("fact_admissions", []) == [
        {
            "text": "The thought is that taste can become a map of memory.",
            "subject": "thought",
            "admitted": False,
            "reason": "meta_subject",
        }
    ]


def test_conversation_integration_transient_chat_assertion_is_not_learned() -> None:
    engram = Engram()
    engram.load_static_data(SEED_PAIRS)

    result = pipeline.chat(engram, "Lunch is good today.", user_id="Robin")

    assert all("tier" in statement for statement in engram.statements)
    assert not [statement for statement in engram.statements if statement.get("tier", Tier.STATIC) == Tier.DYNAMIC]
    assert result.get("fact_admissions", [])[0].get("reason", "") == "transient"


def test_conversation_integration_discourse_and_qualified_assertions_are_not_learned() -> None:
    cases = {
        "Yes, sushi was what I meant.": "discourse_subject",
        "Sometimes simple food is better.": "qualified_subject",
    }

    for text, expected_reason in cases.items():
        engram = Engram()
        engram.load_static_data(SEED_PAIRS)
        result = pipeline.chat(engram, text, user_id="Robin")

        assert all("tier" in statement for statement in engram.statements)
        assert not [statement for statement in engram.statements if statement.get("tier", Tier.STATIC) == Tier.DYNAMIC]
        assert result.get("fact_admissions", [])[0].get("reason", "") == expected_reason


def test_conversation_integration_unattributed_fact_api_is_intentionally_not_filtered() -> None:
    engram = Engram()

    statement_id = engram.add_fact("Lunch is good today.", source_label="research")

    assert statement_id
    assert engram.get_statement(statement_id).get("source_label", "") == "research"
