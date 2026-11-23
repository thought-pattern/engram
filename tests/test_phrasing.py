"""Tests for friendly phrasing of recalled canonical facts.

phrase_fact turns a canonical triple whose predicate is the graph slug
(``located_in``) into a sentence. The connector -- copula, passive,
possessive, or bare active -- is chosen from spaCy morphology, with a
small override map for spaCy's single-token misreads and idiomatic
temporals. The spaCy model is required for every label without an override.
"""

from pytest import raises as pytest_raises

from engram import nltk_data, phrasing
from engram.nltk_data import NltkResources
from engram.phrasing import deslug, phrase_fact, phrase_facts
from engram.spacy_setup import SPACY_PIPELINES


def test_active_finite_verb_is_bare():
    """A finite verb reads active with no connector."""
    assert phrase_fact("Alice", "owns", "Acme") == "Alice owns Acme."


def test_passive_by_predicate_takes_was():
    """A `... by` predicate reads passive."""
    assert phrase_fact("Skai TV", "owned_by", "Skai Group") == "Skai TV was owned by Skai Group."


def test_stative_participle_takes_copula():
    """A participle behind a preposition is stative."""
    assert phrase_fact("Athens", "located_in", "Greece") == "Athens is located in Greece."


def test_nominal_prepositional_takes_copula_and_article():
    """A noun head behind a preposition takes a copula and article."""
    assert phrase_fact("Alice", "member_of", "UN") == "Alice is a member of UN."


def test_noun_role_takes_possessive():
    """A bare noun role reads possessive."""
    assert phrase_fact("Greece", "head_of_state", "Constantine") == "Greece's head of state is Constantine."


def test_override_corrects_noun_misread():
    """`precedes` mis-tags as a noun; the override keeps it an active verb."""
    assert phrase_fact("SeasonB", "precedes", "SeasonC") == "SeasonB precedes SeasonC."


def test_override_gives_temporal_idiom():
    """`date_of_birth` reads as an idiom, not a robotic possessive."""
    assert phrase_fact("Einstein", "date_of_birth", "1879") == "Einstein was born on 1879."


def test_deslug_spaces_the_slug():
    """A slug turns into its surface label."""
    assert deslug("located_in") == "located in"


def test_empty_slot_returns_empty():
    """A missing slot yields no sentence."""
    assert phrase_fact("Athens", "", "Greece") == ""


def test_phrase_facts_joins_sentences():
    """Each triple becomes a sentence, joined by a space."""
    facts = [("Athens", "located_in", "Greece"), ("Skai TV", "owned_by", "Skai Group")]
    assert phrase_facts(facts) == "Athens is located in Greece. Skai TV was owned by Skai Group."


def test_a_missing_model_fails_but_overrides_never_parse(monkeypatch):
    """Parsed labels require the model; an override frame never calls spaCy."""

    def missing_model(disable=()):
        raise RuntimeError("spaCy model en_core_web_sm is not installed")

    monkeypatch.setattr(SPACY_PIPELINES, "pipeline", missing_model)
    with pytest_raises(RuntimeError, match="is not installed"):
        phrase_fact("Alice", "member_of", "UN")
    assert phrase_fact("Einstein", "date_of_birth", "1879") == "Einstein was born on 1879."


def test_missing_wordnet_fails_instead_of_reading_as_not_a_verb(monkeypatch):
    """A misread verb label needs WordNet; without it phrasing raises rather than keeping the noun reading."""
    monkeypatch.setattr(phrasing, "NLTK_RESOURCES", NltkResources())
    monkeypatch.setattr(nltk_data, "ensure_nltk_data", lambda download=False: [("corpora/wordnet", "wordnet")])

    with pytest_raises(RuntimeError, match="required NLTK resources are unavailable: wordnet"):
        phrase_fact("Vaccines", "prevents", "disease")
