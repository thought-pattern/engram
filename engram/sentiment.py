"""Sentiment analysis for ENGRAM using NLTK VADER.

Provides a coarse sentiment label (positive / negative / neutral) for a piece of
text. This lets templates respond with an appropriate tone to open-ended input
without enumerating every emotion word as its own pattern.

``VADER_ANALYZER`` owns the VADER analyzer for the process: the lexicon is
checked and the analyzer built on first use, once. If the lexicon cannot be
obtained, analysis degrades gracefully to a neutral verdict.
"""

from threading import Lock

from nltk.sentiment import SentimentIntensityAnalyzer

from engram.constants import (
    NEGATIVE,
    NEGATIVE_THRESHOLD,
    NEUTRAL,
    NEUTRAL_SCORES,
    POSITIVE,
    POSITIVE_THRESHOLD,
)
from engram.nltk_data import ensure_resource


class VaderAnalyzer:
    """Own the lazily built VADER analyzer and the lexicon's availability.

    ``resolved`` records that the lexicon check has run. ``analyzer`` is the
    built analyzer, or ``()`` when the lexicon is unavailable; the absence is
    retained, so the check is not repeated.
    """

    def __init__(self) -> None:
        self.lock = Lock()
        self.resolved = False
        self.analyzer: object = ()

    def current(self):
        """Return the VADER analyzer, or falsy () when the lexicon is unavailable."""
        with self.lock:
            if not self.resolved:
                if ensure_resource("sentiment/vader_lexicon", "vader_lexicon"):
                    self.analyzer = SentimentIntensityAnalyzer()
                self.resolved = True
            analyzer = self.analyzer
        return analyzer


VADER_ANALYZER = VaderAnalyzer()


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
    analyzer = VADER_ANALYZER.current()
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
    compound = sentiment_scores(text).get("compound", 0.0)
    if compound >= POSITIVE_THRESHOLD:
        return POSITIVE
    if compound <= NEGATIVE_THRESHOLD:
        return NEGATIVE
    return NEUTRAL
