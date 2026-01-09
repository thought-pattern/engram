"""Tests for NLP fact extraction."""

import pytest
from engram.nlp import ExtractedFact, extract_fact


class TestFactExtractor:
    """Tests for FactExtractor."""

    def test_extract_simple_is(self):
        """Test extracting 'X is Y' facts."""
        fact = extract_fact("The sky is blue")
        assert fact is not None
        assert fact.subject == "sky"
        assert fact.predicate == "is"
        assert fact.obj == "blue"
        assert fact.original == "The sky is blue."

    def test_extract_simple_are(self):
        """Test extracting 'X are Y' facts."""
        fact = extract_fact("Cats are mammals")
        assert fact is not None
        assert fact.subject == "Cats"
        assert fact.predicate == "are"
        assert fact.obj == "mammals"

    def test_extract_complex_subject(self):
        """Test extracting facts with complex subjects."""
        fact = extract_fact("The capital of France is Paris")
        assert fact is not None
        assert fact.subject == "capital of France"
        assert fact.obj == "Paris"

    def test_skip_questions(self):
        """Test that questions are not extracted as facts."""
        assert extract_fact("What is the sky?") is None
        assert extract_fact("Is the sky blue?") is None
        assert extract_fact("Who is Einstein?") is None

    def test_skip_commands(self):
        """Test that commands are not extracted as facts."""
        assert extract_fact("Learn that cats are mammals") is None
        assert extract_fact("Remember the sky is blue") is None
        assert extract_fact("Tell me about cats") is None

    def test_skip_pronouns(self):
        """Test that pronoun subjects are skipped."""
        assert extract_fact("I am happy") is None
        assert extract_fact("He is tall") is None
        assert extract_fact("They are ready") is None

    def test_skip_short_input(self):
        """Test that very short input is skipped."""
        assert extract_fact("is blue") is None
        assert extract_fact("cats") is None
        assert extract_fact("") is None

    def test_query_patterns_is(self):
        """Test query pattern generation for 'is' facts."""
        fact = ExtractedFact(
            subject="sky",
            predicate="is",
            obj="blue",
            original="The sky is blue."
        )
        patterns = fact.query_patterns
        assert "SKY" in patterns
        assert "WHAT IS SKY" in patterns
        assert "WHAT IS THE SKY" in patterns
        assert "TELL ME ABOUT SKY" in patterns

    def test_query_patterns_are(self):
        """Test query pattern generation for 'are' facts."""
        fact = ExtractedFact(
            subject="cats",
            predicate="are",
            obj="mammals",
            original="Cats are mammals."
        )
        patterns = fact.query_patterns
        assert "CATS" in patterns
        assert "WHAT ARE CATS" in patterns
        assert "WHAT ARE THE CATS" in patterns


class TestFactLearningIntegration:
    """Integration tests for fact learning in Engram."""

    def test_learn_and_retrieve_fact(self):
        """Test that learned facts can be retrieved."""
        from engram import Engram
        from engram.models import Tier

        engram = Engram()
        # Add catch-all pattern for learning to work
        engram.store("default", pattern="*", tier=Tier.STATIC)

        # Learn a fact
        result1 = engram.pattern_query("Dogs are loyal")
        # Should acknowledge learning (via catch-all with learning)
        assert result1 is not None
        stmt, captured, response = result1
        assert response == "I see."  # Acknowledgment

        # Retrieve the fact
        result2 = engram.pattern_query("What are dogs")
        assert result2 is not None
        stmt, captured, response = result2
        assert response == "Dogs are loyal."

    def test_learn_and_retrieve_with_article(self):
        """Test facts with articles."""
        from engram import Engram
        from engram.models import Tier

        engram = Engram()
        # Add catch-all pattern for learning to work
        engram.store("default", pattern="*", tier=Tier.STATIC)

        # Learn a fact
        engram.pattern_query("The moon is bright")

        # Retrieve with article
        result = engram.pattern_query("What is the moon")
        assert result is not None
        stmt, captured, response = result
        assert response == "The moon is bright."

    def test_no_overwrite_existing(self):
        """Test that existing patterns are not overwritten."""
        from engram import Engram
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
        assert result is not None
        stmt, captured, response = result
        assert response == "Custom response"
