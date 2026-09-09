"""Tests for spaCy-backed features: phrase keywords and context-aware lemmas."""

from pytest import mark as pytest_mark, raises as pytest_raises

from engram import spacy_setup
from engram.config import engram_config
from engram.constants import DEFAULT_STOPWORDS, LEARNED_ACKNOWLEDGMENTS
from engram.core import Engram
from engram.pattern import PatternMatcher
from engram.spacy_setup import get_nlp
from engram.text import extract_keywords_spacy, lemmatize_text_spacy

requires_model = pytest_mark.skipif(not get_nlp(), reason="en_core_web_sm not installed")


def test_missing_model_does_not_trigger_runtime_download(monkeypatch):
    calls = []

    def missing_model(model_name, *, disable):
        calls.append((model_name, disable))
        raise OSError("model is not provisioned")

    monkeypatch.setattr(spacy_setup, "spacy_load", missing_model)

    assert spacy_setup.internal_load("missing_model", ("ner",)) == ()
    assert calls == [("missing_model", ["ner"])]


def test_enabled_spacy_feature_fails_transport_neutral_preflight(monkeypatch):
    monkeypatch.setattr("engram.core.get_nlp", lambda disable=(): ())

    with pytest_raises(ValueError, match="pre-provisioned English model"):
        Engram(config=engram_config(use_spacy_facts=True))


"""Tests for noun-chunk phrase keyword extraction."""


@requires_model
def test_phrase_keywords_keeps_multiword_phrase():
    keywords = extract_keywords_spacy("machine learning models are powerful", DEFAULT_STOPWORDS)
    assert any(" " in kw for kw in keywords)
    assert "machine learning model" in keywords


@requires_model
def test_phrase_keywords_includes_single_lemmas():
    keywords = extract_keywords_spacy("the cats are running", DEFAULT_STOPWORDS)
    assert "cat" in keywords
    assert "run" in keywords


@requires_model
def test_phrase_keywords_empty():
    assert extract_keywords_spacy("", DEFAULT_STOPWORDS) == []


"""Tests for spaCy context-aware lemmatization."""


@requires_model
def test_spacy_lemmatization_verb_context():
    # "saw" as a verb lemmatizes to "see" (WordNet heuristic cannot).
    assert "see" in lemmatize_text_spacy("i saw a movie").split()


@requires_model
def test_spacy_lemmatization_noun_context_preserved():
    # "saw" as a noun stays "saw".
    assert "saw" in lemmatize_text_spacy("the saw is sharp").split()


@requires_model
def test_spacy_lemmatization_matcher_uses_spacy_lemmas():
    pm = PatternMatcher(use_lemmatization=True, use_spacy_lemmatization=True)
    pm.add_pattern("I SEE *", "You see {star1}")
    result = pm.match("i saw a movie")
    assert result
    assert result[0] == "You see {star1}"


"""use_spacy_facts routes pattern_query fact learning through the dependency parser."""


@requires_model
def test_spacy_fact_learning_learns_relational_fact():

    config = engram_config(use_spacy_facts=True, learn_user_facts=True)
    engram = Engram(config=config)
    engram.store("Tell me more.", pattern="*")

    # No copula: the default NLTK extractor cannot learn from this.
    result = engram.pattern_query("Einstein developed the theory of relativity")

    assert result[2] in LEARNED_ACKNOWLEDGMENTS
    patterns = [s["pattern"] for s in engram.statements]
    assert "EINSTEIN" in patterns


@requires_model
def test_spacy_fact_learning_default_extractor_skips_relational_fact():

    engram = Engram()
    engram.store("Tell me more.", pattern="*")

    result = engram.pattern_query("Einstein developed the theory of relativity")

    # Nothing learned, so the catch-all answers normally.
    assert result[2] == "Tell me more."
    patterns = [s["pattern"] for s in engram.statements]
    assert "EINSTEIN" not in patterns
