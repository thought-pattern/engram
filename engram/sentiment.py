"""Sentiment analysis for ENGRAM using NLTK VADER.

Provides a coarse sentiment label (positive / negative / neutral) for a piece of
text. This lets templates respond with an appropriate tone to open-ended input
without enumerating every emotion word as its own pattern.

The VADER lexicon is a required NLTK package: the process's analyzer belongs to
``nltk_data.NLTK_RESOURCES``, and a missing lexicon raises ``RuntimeError``.
"""

from engram.constants import (
    NEGATIVE,
    NEGATIVE_THRESHOLD,
    NEUTRAL,
    NEUTRAL_SCORES,
    POSITIVE,
    POSITIVE_THRESHOLD,
)
from engram.nltk_data import NLTK_RESOURCES


def sentiment_scores(text: str) -> dict:
    """Return VADER polarity scores for text.

    Args:
        text: Input text.

    Returns:
        Dict with compound, pos, neu, neg keys; neutral scores for empty text.

    Raises:
        RuntimeError: required NLTK data is not available.
    """
    if not text or not text.strip():
        neutral_scores = dict(NEUTRAL_SCORES)
        return neutral_scores
    analyzer = NLTK_RESOURCES.sentiment_analyzer()
    scores = analyzer.polarity_scores(text)
    return scores


def sentiment_label(text: str) -> str:
    """Return a coarse sentiment label for text.

    Args:
        text: Input text.

    Returns:
        One of "positive", "negative", or "neutral".
    """
    compound = sentiment_scores(text).get("compound", 0.0)
    if compound >= POSITIVE_THRESHOLD:
        return POSITIVE
    if compound <= NEGATIVE_THRESHOLD:
        return NEGATIVE
    return NEUTRAL
