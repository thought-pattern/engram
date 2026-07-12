"""Tests for NLP fact extraction."""

from engram.nlp import extract_fact, extracted_fact, fact_query_patterns


class TestFactExtractor:
    """Tests for FactExtractor."""

    def test_extract_simple_is(self):
        """Test extracting 'X is Y' facts."""
        fact = extract_fact("The sky is blue")
        assert fact
        assert fact["subject"] == "sky"
        assert fact["predicate"] == "is"
        assert fact["obj"] == "blue"
        assert fact["original"] == "The sky is blue."

    def test_extract_simple_are(self):
        """Test extracting 'X are Y' facts."""
        fact = extract_fact("Cats are mammals")
        assert fact
        assert fact["subject"] == "Cats"
        assert fact["predicate"] == "are"
        assert fact["obj"] == "mammals"

    def test_extract_complex_subject(self):
        """Test extracting facts with complex subjects."""
        fact = extract_fact("The capital of France is Paris")
        assert fact
        assert fact["subject"] == "capital of France"
        assert fact["obj"] == "Paris"

    def test_skip_questions(self):
        """Test that questions are not extracted as facts."""
        assert not extract_fact("What is the sky?")
        assert not extract_fact("Is the sky blue?")
        assert not extract_fact("Who is Einstein?")

    def test_skip_commands(self):
        """Test that commands are not extracted as facts."""
        assert not extract_fact("Learn that cats are mammals")
        assert not extract_fact("Remember the sky is blue")
        assert not extract_fact("Tell me about cats")

    def test_skip_pronouns(self):
        """Test that pronoun subjects are skipped."""
        assert not extract_fact("I am happy")
        assert not extract_fact("He is tall")
        assert not extract_fact("They are ready")

    def test_skip_short_input(self):
        """Test that very short input is skipped."""
        assert not extract_fact("is blue")
        assert not extract_fact("cats")
        assert not extract_fact("")

    def test_query_patterns_is(self):
        """Test query pattern generation for 'is' facts."""
        fact = extracted_fact(subject="sky", predicate="is", obj="blue", original="The sky is blue.")
        patterns = fact_query_patterns(fact)
        assert "SKY" in patterns
        assert "WHAT IS SKY" in patterns
        assert "WHAT IS THE SKY" in patterns
        assert "TELL ME ABOUT SKY" in patterns

    def test_query_patterns_are(self):
        """Test query pattern generation for 'are' facts."""
        fact = extracted_fact(subject="cats", predicate="are", obj="mammals", original="Cats are mammals.")
        patterns = fact_query_patterns(fact)
        assert "CATS" in patterns
        assert "WHAT ARE CATS" in patterns
        assert "WHAT ARE THE CATS" in patterns


class TestFactLearningIntegration:
    """Integration tests for fact learning in Engram."""

    def test_learn_and_retrieve_fact(self):
        """Test that learned facts can be retrieved."""
        from engram.core import Engram
        from engram.models import Tier

        engram = Engram()
        # Add catch-all pattern for learning to work
        engram.store("default", pattern="*", tier=Tier.STATIC)

        # Learn a fact
        result1 = engram.pattern_query("Dogs are loyal")
        # Should acknowledge learning (via catch-all with learning)
        assert result1
        stmt, captured, response = result1
        assert response == "I see."  # Acknowledgment

        # Retrieve the fact
        result2 = engram.pattern_query("What are dogs")
        assert result2
        stmt, captured, response = result2
        assert response == "Dogs are loyal."

    def test_learn_and_retrieve_with_article(self):
        """Test facts with articles."""
        from engram.core import Engram
        from engram.models import Tier

        engram = Engram()
        # Add catch-all pattern for learning to work
        engram.store("default", pattern="*", tier=Tier.STATIC)

        # Learn a fact
        engram.pattern_query("The moon is bright")

        # Retrieve with article
        result = engram.pattern_query("What is the moon")
        assert result
        stmt, captured, response = result
        assert response == "The moon is bright."

    def test_no_overwrite_existing(self):
        """Test that existing patterns are not overwritten."""
        from engram.core import Engram
        from engram.models import Tier

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


class TestFactExtractionGuardrails:
    """The pre-copula span must look like a plain noun phrase."""

    def test_reject_embedded_clause_copula(self):
        # The copula belongs to an embedded clause; splitting at "is" would
        # store a junk fact with an unusable retrieval pattern.
        assert not extract_fact("Your sentiment analysis should inform that tired is not nice.")
        assert not extract_fact("The report we wrote is finished")

    def test_reject_possessive_led_subject(self):
        assert not extract_fact("My dog is friendly")
        assert not extract_fact("Your car is fast")

    def test_reject_long_subject(self):
        assert not extract_fact("The old lighthouse keeper of the northern coast is retired")

    def test_accept_plain_noun_phrase_subjects(self):
        fact = extract_fact("The capital of France is Paris")
        assert fact["subject"] == "capital of France"
        fact = extract_fact("The sky is blue")
        assert fact["subject"] == "sky"


class TestQuestionDetection:
    """Tests for the public question/intent detection."""

    def test_trailing_question_mark(self):
        from engram.nlp import is_question

        assert is_question("This works?")

    def test_question_word_lead(self):
        from engram.nlp import is_question

        assert is_question("what do you think about python")

    def test_inverted_copula(self):
        from engram.nlp import is_question

        assert is_question("Is it working")

    def test_statement_is_not_question(self):
        from engram.nlp import is_question

        assert not is_question("The sky is blue")


class TestInputKind:
    """Tests for input intent classification."""

    def test_question(self):
        from engram.nlp import input_kind

        assert input_kind("Where is my hat?") == "question"

    def test_command(self):
        from engram.nlp import input_kind

        assert input_kind("tell me a story") == "command"

    def test_statement(self):
        from engram.nlp import input_kind

        assert input_kind("I lost my hat yesterday") == "statement"
