"""Sentiment analysis for ENGRAM using NLTK VADER.

Provides a coarse sentiment label (positive / negative / neutral) for a piece of
text. This lets templates respond with an appropriate tone to open-ended input
without enumerating every emotion word as its own pattern.

The VADER lexicon is loaded lazily and cached. If the lexicon cannot be
obtained, analysis degrades gracefully to a neutral verdict.
"""

from functools import lru_cache

# VADER compound-score thresholds (the standard cutoffs from the VADER paper).
POSITIVE_THRESHOLD = 0.05
NEGATIVE_THRESHOLD = -0.05

NEUTRAL = "neutral"
POSITIVE = "positive"
NEGATIVE = "negative"

_NEUTRAL_SCORES = {"compound": 0.0, "pos": 0.0, "neu": 1.0, "neg": 0.0}


@lru_cache(maxsize=1)
def _get_analyzer():
    """Build and cache the VADER analyzer, or return falsy if unavailable."""
    from engram.nltk_data import ensure_resource

    if not ensure_resource("sentiment/vader_lexicon", "vader_lexicon"):
        return ()
    from nltk.sentiment import SentimentIntensityAnalyzer

    return SentimentIntensityAnalyzer()


def sentiment_scores(text: str) -> dict:
    """Return VADER polarity scores for text.

    Args:
        text: Input text.

    Returns:
        Dict with compound, pos, neu, neg keys. Neutral scores if text is empty
        or the lexicon is unavailable.
    """
    if not text or not text.strip():
        return dict(_NEUTRAL_SCORES)
    analyzer = _get_analyzer()
    if not analyzer:
        return dict(_NEUTRAL_SCORES)
    return analyzer.polarity_scores(text)


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
