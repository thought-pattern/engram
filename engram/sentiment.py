"""Sentiment analysis for ENGRAM using NLTK VADER.

Provides a coarse sentiment label (positive / negative / neutral) for a piece of
text. This lets templates respond with an appropriate tone to open-ended input
without enumerating every emotion word as its own pattern.

The VADER lexicon is loaded lazily and cached. If the lexicon cannot be
obtained, analysis degrades gracefully to a neutral verdict.
"""

from functools import lru_cache

from nltk.sentiment import SentimentIntensityAnalyzer

from engram.constants import NEGATIVE, NEGATIVE_THRESHOLD, NEUTRAL, NEUTRAL_SCORES, POSITIVE, POSITIVE_THRESHOLD
from engram.nltk_data import ensure_resource


@lru_cache(maxsize=1)
def _get_analyzer():
    """Build and cache the VADER analyzer, or return falsy if unavailable."""
    if not ensure_resource("sentiment/vader_lexicon", "vader_lexicon"):
        return ()
    analyzer = SentimentIntensityAnalyzer()
    return analyzer


def sentiment_scores(text: str) -> dict:
    """Return VADER polarity scores for text.

    Args:
        text: Input text.

    Returns:
        Dict with compound, pos, neu, neg keys. Neutral scores if text is empty
        or the lexicon is unavailable.
    """
    if not text or not text.strip():
        neutral_scores = dict(NEUTRAL_SCORES)
        return neutral_scores
    analyzer = _get_analyzer()
    if not analyzer:
        neutral_scores = dict(NEUTRAL_SCORES)
        return neutral_scores
    scores = analyzer.polarity_scores(text)
    return scores


def sentiment_label(text: str) -> str:
    """Return a coarse sentiment label for text.

    Args:
        text: Input text.

    Returns:
        One of "positive", "negative", or "neutral".
    """
    compound = sentiment_scores(text)["compound"]
    if compound >= POSITIVE_THRESHOLD:
        return POSITIVE
    if compound <= NEGATIVE_THRESHOLD:
        return NEGATIVE
    return NEUTRAL
