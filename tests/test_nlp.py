"""Tests for NLP fact extraction."""

from engram.config import engram_config
from engram.constants import LEARNED_ACKNOWLEDGMENTS
from engram.core import Engram
from engram.models import Tier
from engram.nlp import extract_fact, extracted_fact, fact_query_patterns, input_kind, is_question


class TestFactExtractor:
    """Tests for FactExtractor."""

    def test_extract_simple_is(self):
        """Test extracting 'X is Y' facts."""
        fact = extract_fact("The sky is blue")
        assert fact
        assert fact.get("subject", "") == "sky"
        assert fact.get("predicate", "") == "is"
        assert fact.get("obj", "") == "blue"
        assert fact.get("original", "") == "The sky is blue."
        return False

    def test_extract_simple_are(self):
        """Test extracting 'X are Y' facts."""
        fact = extract_fact("Cats are mammals")
        assert fact
        assert fact.get("subject", "") == "Cats"
        assert fact.get("predicate", "") == "are"
        assert fact.get("obj", "") == "mammals"
        return False

    def test_extract_complex_subject(self):
        """Test extracting facts with complex subjects."""
        fact = extract_fact("The capital of France is Paris")
        assert fact
        assert fact.get("subject", "") == "capital of France"
        assert fact.get("obj", "") == "Paris"
        return False

    def test_skip_questions(self):
        """Test that questions are not extracted as facts."""
        assert not extract_fact("What is the sky?")
        assert not extract_fact("Is the sky blue?")
        assert not extract_fact("Who is Einstein?")
        return False

    def test_skip_commands(self):
        """Test that commands are not extracted as facts."""
        assert not extract_fact("Learn that cats are mammals")
        assert not extract_fact("Remember the sky is blue")
        assert not extract_fact("Tell me about cats")
        return False

    def test_skip_pronouns(self):
        """Test that pronoun subjects are skipped."""
        assert not extract_fact("I am happy")
        assert not extract_fact("He is tall")
        assert not extract_fact("They are ready")
        return False

    def test_skip_short_input(self):
        """Test that very short input is skipped."""
        assert not extract_fact("is blue")
        assert not extract_fact("cats")
        assert not extract_fact("")
        return False

    def test_query_patterns_is(self):
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
        return False

    def test_query_patterns_are(self):
        """Test query pattern generation for 'are' facts."""
        fact = extracted_fact(subject="cats", predicate="are", obj="mammals", original="Cats are mammals.")
        patterns = fact_query_patterns(fact)
        assert "CATS" in patterns
        assert "WHAT ARE CATS" in patterns
        assert "WHAT ARE THE CATS" in patterns
        assert "WHAT IS MAMMALS" in patterns
        return False


class TestFactLearningIntegration:
    """Integration tests for fact learning in Engram."""

    def test_learn_and_retrieve_fact(self):
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
        return False

    def test_learn_and_retrieve_with_article(self):
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
        return False

    def test_no_overwrite_existing(self):
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
        return False


class TestFactExtractionGuardrails:
    """The pre-copula span must look like a plain noun phrase."""

    def test_reject_embedded_clause_copula(self):
        # The copula belongs to an embedded clause; splitting at "is" would
        # store a junk fact with an unusable retrieval pattern.
        assert not extract_fact("Your sentiment analysis should inform that tired is not nice.")
        assert not extract_fact("The report we wrote is finished")
        return False

    def test_reject_possessive_led_subject(self):
        assert not extract_fact("My dog is friendly")
        assert not extract_fact("Your car is fast")
        return False

    def test_reject_long_subject(self):
        assert not extract_fact("The old lighthouse keeper of the northern coast is retired")
        return False

    def test_accept_plain_noun_phrase_subjects(self):
        fact = extract_fact("The capital of France is Paris")
        assert fact.get("subject", "") == "capital of France"
        fact = extract_fact("The sky is blue")
        assert fact.get("subject", "") == "sky"
        return False

    def test_accept_single_noun_like_ing_subject(self):
        fact = extract_fact("Lightning is an electrical discharge")

        assert fact.get("subject", "") == "Lightning"
        assert fact.get("predicate", "") == "is"
        assert fact.get("obj", "") == "an electrical discharge"
        return False


class TestQuestionDetection:
    """Tests for the public question/intent detection."""

    def test_trailing_question_mark(self):
        assert is_question("This works?")
        return False

    def test_question_word_lead(self):
        assert is_question("what do you think about python")
        return False

    def test_inverted_copula(self):
        assert is_question("Is it working")
        return False

    def test_statement_is_not_question(self):
        assert not is_question("The sky is blue")
        return False


class TestInputKind:
    """Tests for input intent classification."""

    def test_question(self):
        assert input_kind("Where is my hat?") == "question"
        return False

    def test_command(self):
        assert input_kind("tell me a story") == "command"
        return False

    def test_statement(self):
        assert input_kind("I lost my hat yesterday") == "statement"
        return False


class TestFactExtractionSoakRegressions:
    """Junk-fact families the 100-turn conversation soak surfaced."""

    def test_reject_pronoun_anywhere_in_subject(self):
        # "y'all" expands to "you all"; "lol that" carries a demonstrative.
        assert not extract_fact("you all are pretty helpful")
        assert not extract_fact("lol that was funny")
        return False

    def test_reject_demonstrative_subject(self):
        assert not extract_fact("That is not true at all")
        return False

    def test_reject_possessive_anywhere_in_subject(self):
        assert not extract_fact("sorry my typing is terrible today")
        return False

    def test_reject_possessive_object(self):
        # A typo'd question word reads as a statement; the possessive object
        # ("your name") marks it as a personal exchange, not a world fact.
        assert not extract_fact("waht is your name")
        assert not extract_fact("The password is my birthday")
        return False

    def test_legitimate_facts_still_learn(self):
        assert extract_fact("Honey is made by bees").get("subject", "") == "Honey"
        assert extract_fact("Rex is a golden retriever").get("subject", "") == "Rex"
        return False


class TestTypoQuestionDetection:
    """A leading near-miss of a question word is a typo'd question."""

    def test_typo_question_words_detected(self):
        assert is_question("waht is the ocean")
        assert is_question("whta is gravity")
        assert is_question("waht is your name")
        return False

    def test_real_words_near_question_words_unaffected(self):
        # "hat" and "cow" are one edit from question words but are real words.
        assert not is_question("hat is my favorite word")
        assert not is_question("cow tipping is not real")
        return False

    def test_typo_questions_never_learned_as_facts(self):
        assert not extract_fact("waht is the ocean")
        assert not extract_fact("whta is gravity")
        return False

    def test_real_word_subjects_still_learn(self):
        assert extract_fact("The cow is a farm animal").get("subject", "") == "cow"
        return False
