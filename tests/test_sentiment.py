"""Tests for VADER sentiment analysis and its template integration."""

from engram.constants import NEGATIVE, NEUTRAL, POSITIVE
from engram.sentiment import sentiment_label, sentiment_scores

"""Tests for sentiment_label."""


def test_sentiment_label_positive():
    """Clearly positive text is labeled positive."""
    assert sentiment_label("I love this, it is wonderful") == POSITIVE


def test_sentiment_label_negative():
    """Clearly negative text is labeled negative."""
    assert sentiment_label("This is awful and I hate it") == NEGATIVE


def test_sentiment_label_neutral():
    """Affectively flat text is labeled neutral."""
    assert sentiment_label("the box is on the table") == NEUTRAL


def test_sentiment_label_single_words():
    """Common emotion words route to the expected label."""
    assert sentiment_label("sad") == NEGATIVE
    assert sentiment_label("happy") == POSITIVE


def test_sentiment_label_empty_is_neutral():
    """Empty or whitespace text is neutral."""
    assert sentiment_label("") == NEUTRAL
    assert sentiment_label("   ") == NEUTRAL


"""Tests for sentiment_scores."""


def test_sentiment_scores_returns_compound():
    """Scores include a compound key in [-1, 1]."""
    scores = sentiment_scores("I love this")
    assert "compound" in scores
    assert -1.0 <= scores["compound"] <= 1.0


def test_sentiment_scores_positive_compound_higher_than_negative():
    """Positive text scores higher than negative text."""
    assert sentiment_scores("great")["compound"] > sentiment_scores("terrible")["compound"]


def test_sentiment_scores_empty_neutral_scores():
    """Empty text yields neutral scores."""
    assert sentiment_scores("")["compound"] == 0.0


"""Tests for the {sentiment:...} template transform."""


def test_sentiment_template_transform_transform_positive():
    """{sentiment:...} resolves to a label string."""
    from engram.template import process_template, template_context

    assert process_template("{sentiment:i love it}", template_context()) == POSITIVE


def test_sentiment_template_transform_transform_negative():
    """{sentiment:...} labels negative content."""
    from engram.template import process_template, template_context

    assert process_template("{sentiment:this is horrible}", template_context()) == NEGATIVE


def test_sentiment_template_transform_transform_resolves_star_first():
    """{sentiment:{star1}} analyzes the captured wildcard."""
    from engram.template import process_template, template_context

    ctx = template_context(stars=["delighted"])
    assert process_template("{sentiment:{star1}}", ctx) == POSITIVE


"""Integration tests for sentiment-routed responses in Engram."""


def test_sentiment_integration_negative_emotion_gets_sympathy():
    """A negative 'I am X' routes to a sympathetic response."""
    from engram.core import Engram

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


def test_sentiment_integration_positive_emotion_gets_cheer():
    """A positive 'I am X' routes to a cheerful response."""
    from engram.core import Engram

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
