"""Tests for NLP fact extraction."""

from pytest import mark as pytest_mark, raises as pytest_raises

from engram import nlp
from engram.config import engram_config
from engram.constants import LEARNED_ACKNOWLEDGMENTS
from engram.core import Engram
from engram.models import Tier
from engram.nlp import extract_entities, extract_fact, extracted_fact, fact_query_patterns, input_kind, is_question


def test_fact_extractor_extract_simple_is():
    """Test extracting 'X is Y' facts."""
    fact = extract_fact("The sky is blue")
    assert fact
    assert fact["subject"] == "sky"
    assert fact["predicate"] == "is"
    assert fact["obj"] == "blue"
    assert fact["original"] == "The sky is blue."


def test_fact_extractor_extract_simple_are():
    """Test extracting 'X are Y' facts."""
    fact = extract_fact("Cats are mammals")
    assert fact
    assert fact["subject"] == "Cats"
    assert fact["predicate"] == "are"
    assert fact["obj"] == "mammals"


def test_fact_extractor_extract_complex_subject():
    """Test extracting facts with complex subjects."""
    fact = extract_fact("The capital of France is Paris")
    assert fact
    assert fact["subject"] == "capital of France"
    assert fact["obj"] == "Paris"


def test_fact_extractor_skip_questions():
    """Test that questions are not extracted as facts."""
    assert not extract_fact("What is the sky?")
    assert not extract_fact("Is the sky blue?")
    assert not extract_fact("Who is Einstein?")


def test_fact_extractor_skip_commands():
    """Test that commands are not extracted as facts."""
    assert not extract_fact("Learn that cats are mammals")
    assert not extract_fact("Remember the sky is blue")
    assert not extract_fact("Tell me about cats")


def test_fact_extractor_skip_pronouns():
    """Test that pronoun subjects are skipped."""
    assert not extract_fact("I am happy")
    assert not extract_fact("He is tall")
    assert not extract_fact("They are ready")


def test_fact_extractor_skip_short_input():
    """Test that very short input is skipped."""
    assert not extract_fact("is blue")
    assert not extract_fact("cats")
    assert not extract_fact("")


def test_fact_extractor_query_patterns_is():
    """Test query pattern generation for 'is' facts."""
    fact = extracted_fact(subject="sky", predicate="is", obj="blue", original="The sky is blue.")
    patterns = fact_query_patterns(fact)
    assert "SKY" in patterns
    assert "WHAT IS SKY" in patterns
    assert "WHAT IS THE SKY" in patterns
    assert "WHAT IS BLUE" in patterns
    assert "TELL ME ABOUT SKY" in patterns
    assert "WHAT DO YOU KNOW ABOUT SKY" in patterns
    assert "WHAT DO YOU REMEMBER ABOUT SKY" in patterns
    assert "WHAT DID I SAY ABOUT SKY" in patterns
    assert "DO YOU REMEMBER SKY" in patterns


def test_fact_extractor_query_patterns_are():
    """Test query pattern generation for 'are' facts."""
    fact = extracted_fact(subject="cats", predicate="are", obj="mammals", original="Cats are mammals.")
    patterns = fact_query_patterns(fact)
    assert "CATS" in patterns
    assert "WHAT ARE CATS" in patterns
    assert "WHAT ARE THE CATS" in patterns
    assert "WHAT IS MAMMALS" in patterns


"""Integration tests for fact learning in Engram."""


def test_fact_learning_integration_learn_and_retrieve_fact():
    """Test that learned facts can be retrieved."""

    engram = Engram(config=engram_config(learn_user_facts=True))
    # Add catch-all pattern for learning to work
    engram.store("default", pattern="*", tier=Tier.STATIC)

    # Learn a fact
    result1 = engram.pattern_query("Dogs are loyal")
    # Should acknowledge learning (via catch-all with learning)
    assert result1
    stmt, captured, response = result1

    assert response in LEARNED_ACKNOWLEDGMENTS  # Acknowledgment

    # Retrieve the fact
    result2 = engram.pattern_query("What are dogs")
    assert result2
    stmt, captured, response = result2
    assert response == "Dogs are loyal."


def test_fact_learning_integration_learn_and_retrieve_with_article():
    """Test facts with articles."""

    engram = Engram(config=engram_config(learn_user_facts=True))
    # Add catch-all pattern for learning to work
    engram.store("default", pattern="*", tier=Tier.STATIC)

    # Learn a fact
    engram.pattern_query("The moon is bright")

    # Retrieve with article
    result = engram.pattern_query("What is the moon")
    assert result
    stmt, captured, response = result
    assert response == "The moon is bright."


def test_fact_learning_integration_no_overwrite_existing():
    """Test that existing patterns are not overwritten."""

    engram = Engram()
    # Add catch-all pattern
    engram.store("default", pattern="*", tier=Tier.STATIC)

    # Store a pattern manually
    engram.store("Custom response", pattern="CATS")

    # Try to learn a fact about cats
    engram.pattern_query("Cats are mammals")

    # Should still return custom response
    result = engram.pattern_query("cats")
    assert result
    stmt, captured, response = result
    assert response == "Custom response"


"""The pre-copula span must look like a plain noun phrase."""


def test_fact_extraction_guardrails_reject_embedded_clause_copula():
    # The copula belongs to an embedded clause; splitting at "is" would
    # store a junk fact with an unusable retrieval pattern.
    assert not extract_fact("Your sentiment analysis should inform that tired is not nice.")
    assert not extract_fact("The report we wrote is finished")


def test_fact_extraction_guardrails_reject_possessive_led_subject():
    assert not extract_fact("My dog is friendly")
    assert not extract_fact("Your car is fast")


def test_fact_extraction_guardrails_reject_long_subject():
    assert not extract_fact("The old lighthouse keeper of the northern coast is retired")


def test_fact_extraction_guardrails_accept_plain_noun_phrase_subjects():
    fact = extract_fact("The capital of France is Paris")
    assert fact["subject"] == "capital of France"
    fact = extract_fact("The sky is blue")
    assert fact["subject"] == "sky"


def test_fact_extraction_guardrails_accept_single_noun_like_ing_subject():
    fact = extract_fact("Lightning is an electrical discharge")

    assert fact["subject"] == "Lightning"
    assert fact["predicate"] == "is"
    assert fact["obj"] == "an electrical discharge"


"""Tests for the public question/intent detection."""


def test_question_detection_trailing_question_mark():

    assert is_question("This works?")


def test_question_detection_question_word_lead():

    assert is_question("what do you think about python")


def test_question_detection_inverted_copula():

    assert is_question("Is it working")


def test_question_detection_statement_is_not_question():

    assert not is_question("The sky is blue")


"""Tests for input intent classification."""


def test_input_kind_question():

    assert input_kind("Where is my hat?") == "question"


def test_input_kind_command():

    assert input_kind("tell me a story") == "command"


def test_input_kind_statement():

    assert input_kind("I lost my hat yesterday") == "statement"


"""Junk-fact families the 100-turn conversation soak surfaced."""


def test_fact_extraction_soak_regressions_reject_pronoun_anywhere_in_subject():
    # "y'all" expands to "you all"; "lol that" carries a demonstrative.
    assert not extract_fact("you all are pretty helpful")
    assert not extract_fact("lol that was funny")


def test_fact_extraction_soak_regressions_reject_demonstrative_subject():
    assert not extract_fact("That is not true at all")


def test_fact_extraction_soak_regressions_reject_possessive_anywhere_in_subject():
    assert not extract_fact("sorry my typing is terrible today")


def test_fact_extraction_soak_regressions_reject_possessive_object():
    # A typo'd question word reads as a statement; the possessive object
    # ("your name") marks it as a personal exchange, not a world fact.
    assert not extract_fact("waht is your name")
    assert not extract_fact("The password is my birthday")


def test_fact_extraction_soak_regressions_legitimate_facts_still_learn():
    assert extract_fact("Honey is made by bees")["subject"] == "Honey"
    assert extract_fact("Rex is a golden retriever")["subject"] == "Rex"


"""A leading near-miss of a question word is a typo'd question."""


def test_typo_question_detection_typo_question_words_detected():

    assert is_question("waht is the ocean")
    assert is_question("whta is gravity")
    assert is_question("waht is your name")


def test_typo_question_detection_real_words_near_question_words_unaffected():

    # "hat" and "cow" are one edit from question words but are real words.
    assert not is_question("hat is my favorite word")
    assert not is_question("cow tipping is not real")


def test_typo_question_detection_typo_questions_never_learned_as_facts():
    assert not extract_fact("waht is the ocean")
    assert not extract_fact("whta is gravity")


def test_typo_question_detection_real_word_subjects_still_learn():
    assert extract_fact("The cow is a farm animal")["subject"] == "cow"


@pytest_mark.parametrize("extractor", (extract_fact, extract_entities))
def test_nlp_extraction_does_not_hide_dependency_failures(monkeypatch, extractor):
    def fail_tokenization(internal_text):
        raise RuntimeError("injected tokenizer failure")

    monkeypatch.setattr(nlp, "ensure_nltk_data", lambda: False)
    monkeypatch.setattr(nlp, "word_tokenize", fail_tokenization)

    with pytest_raises(RuntimeError, match="injected tokenizer failure"):
        extractor("Paris is in France")
