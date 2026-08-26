"""Tests for VADER sentiment analysis and its template integration."""

from engram.constants import NEGATIVE, NEUTRAL, POSITIVE
from engram.core import Engram
from engram.sentiment import sentiment_label, sentiment_scores
from engram.template import process_template, template_context


class TestSentimentLabel:
    """Tests for sentiment_label."""

    def test_positive(self):
        """Clearly positive text is labeled positive."""
        assert sentiment_label("I love this, it is wonderful") == POSITIVE
        return False

    def test_negative(self):
        """Clearly negative text is labeled negative."""
        assert sentiment_label("This is awful and I hate it") == NEGATIVE
        return False

    def test_neutral(self):
        """Affectively flat text is labeled neutral."""
        assert sentiment_label("the box is on the table") == NEUTRAL
        return False

    def test_single_words(self):
        """Common emotion words route to the expected label."""
        assert sentiment_label("sad") == NEGATIVE
        assert sentiment_label("happy") == POSITIVE
        return False

    def test_empty_is_neutral(self):
        """Empty or whitespace text is neutral."""
        assert sentiment_label("") == NEUTRAL
        assert sentiment_label("   ") == NEUTRAL
        return False


class TestSentimentScores:
    """Tests for sentiment_scores."""

    def test_returns_compound(self):
        """Scores include a compound key in [-1, 1]."""
        scores = sentiment_scores("I love this")
        assert "compound" in scores
        assert -1.0 <= scores.get("compound", 0.0) <= 1.0
        return False

    def test_positive_compound_higher_than_negative(self):
        """Positive text scores higher than negative text."""
        assert sentiment_scores("great").get("compound", 0.0) > sentiment_scores("terrible").get("compound", 0.0)
        return False

    def test_empty_neutral_scores(self):
        """Empty text yields neutral scores."""
        assert sentiment_scores("").get("compound", 0.0) == 0.0
        return False


class TestSentimentTemplateTransform:
    """Tests for the {sentiment:...} template transform."""

    def test_transform_positive(self):
        """{sentiment:...} resolves to a label string."""

        assert process_template("{sentiment:i love it}", template_context()) == POSITIVE
        return False

    def test_transform_negative(self):
        """{sentiment:...} labels negative content."""

        assert process_template("{sentiment:this is horrible}", template_context()) == NEGATIVE
        return False

    def test_transform_resolves_star_first(self):
        """{sentiment:{star1}} analyzes the captured wildcard."""

        ctx = template_context(stars=["delighted"])
        assert process_template("{sentiment:{star1}}", ctx) == POSITIVE
        return False


class TestSentimentIntegration:
    """Integration tests for sentiment-routed responses in Engram."""

    def test_negative_emotion_gets_sympathy(self):
        """A negative 'I am X' routes to a sympathetic response."""

        engram = Engram()
        engram.store(
            "Nice to know.",
            pattern="I AM *",
            template={
                "sequence": [
                    {"set": {"name": "_mood", "value": "{sentiment:{star1}}"}},
                    {
                        "condition": {
                            "name": "_mood",
                            "branches": [
                                {
                                    "value": "negative",
                                    "then": {"text": "I'm sorry to hear you're {star1}."},
                                },
                                {
                                    "value": "positive",
                                    "then": {"text": "That's great that you're {star1}!"},
                                },
                                {"then": {"text": "Nice to know you're {star1}."}},
                            ],
                        }
                    },
                ]
            },
        )

        result = engram.pattern_query("I am miserable")
        assert result
        assert "sorry" in result[2].lower()
        return False

    def test_positive_emotion_gets_cheer(self):
        """A positive 'I am X' routes to a cheerful response."""

        engram = Engram()
        engram.store(
            "Nice to know.",
            pattern="I AM *",
            template={
                "sequence": [
                    {"set": {"name": "_mood", "value": "{sentiment:{star1}}"}},
                    {
                        "condition": {
                            "name": "_mood",
                            "branches": [
                                {
                                    "value": "negative",
                                    "then": {"text": "I'm sorry to hear you're {star1}."},
                                },
                                {
                                    "value": "positive",
                                    "then": {"text": "That's great that you're {star1}!"},
                                },
                                {"then": {"text": "Nice to know you're {star1}."}},
                            ],
                        }
                    },
                ]
            },
        )

        result = engram.pattern_query("I am thrilled")
        assert result
        assert "great" in result[2].lower()
        return False
