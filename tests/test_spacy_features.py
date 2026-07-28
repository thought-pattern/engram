"""Tests for spaCy-backed features: phrase keywords and context-aware lemmas."""

import pytest

from engram.config import engram_config
from engram.constants import DEFAULT_STOPWORDS
from engram.pattern import PatternMatcher
from engram.spacy_setup import get_nlp
from engram.text import extract_keywords_spacy, lemmatize_text_spacy

requires_model = pytest.mark.skipif(not get_nlp(), reason="en_core_web_sm not installed")


class TestConfigDefaults:
    """All spaCy-backed features are opt-in (default off)."""

    def test_defaults(self):
        cfg = engram_config()
        assert cfg["use_spacy_facts"] is False
        assert cfg["use_spacy_lemmatization"] is False
        assert cfg["use_phrase_keywords"] is False


@requires_model
class TestPhraseKeywords:
    """Tests for noun-chunk phrase keyword extraction."""

    def test_keeps_multiword_phrase(self):
        keywords = extract_keywords_spacy("machine learning models are powerful", DEFAULT_STOPWORDS)
        assert any(" " in kw for kw in keywords)
        assert "machine learning model" in keywords

    def test_includes_single_lemmas(self):
        keywords = extract_keywords_spacy("the cats are running", DEFAULT_STOPWORDS)
        assert "cat" in keywords
        assert "run" in keywords

    def test_empty(self):
        assert extract_keywords_spacy("", DEFAULT_STOPWORDS) == []


@requires_model
class TestSpacyLemmatization:
    """Tests for spaCy context-aware lemmatization."""

    def test_verb_context(self):
        # "saw" as a verb lemmatizes to "see" (WordNet heuristic cannot).
        assert "see" in lemmatize_text_spacy("i saw a movie").split()

    def test_noun_context_preserved(self):
        # "saw" as a noun stays "saw".
        assert "saw" in lemmatize_text_spacy("the saw is sharp").split()

    def test_matcher_uses_spacy_lemmas(self):
        pm = PatternMatcher(use_lemmatization=True, use_spacy_lemmatization=True)
        pm.add_pattern("I SEE *", "You see {star1}")
        result = pm.match("i saw a movie")
        assert result
        assert result[0] == "You see {star1}"


@requires_model
class TestSpacyFactLearning:
    """use_spacy_facts routes pattern_query fact learning through the dependency parser."""

    def test_learns_relational_fact(self):
        from engram.core import Engram

        config = engram_config(use_spacy_facts=True, learn_user_facts=True)
        engram = Engram(config=config)
        engram.store("Tell me more.", pattern="*")

        # No copula: the default NLTK extractor cannot learn from this.
        result = engram.pattern_query("Einstein developed the theory of relativity")

        from engram.constants import LEARNED_ACKNOWLEDGMENTS

        assert result[2] in LEARNED_ACKNOWLEDGMENTS
        patterns = [s["pattern"] for s in engram.statements]
        assert "EINSTEIN" in patterns

    def test_default_extractor_skips_relational_fact(self):
        from engram.core import Engram

        engram = Engram()
        engram.store("Tell me more.", pattern="*")

        result = engram.pattern_query("Einstein developed the theory of relativity")

        # Nothing learned, so the catch-all answers normally.
        assert result[2] == "Tell me more."
        patterns = [s["pattern"] for s in engram.statements]
        assert "EINSTEIN" not in patterns
