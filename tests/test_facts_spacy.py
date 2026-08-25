"""Tests for spaCy dependency-parse fact extraction."""

import pytest

from engram.config import engram_config
from engram.facts_spacy import extract_facts
from engram.spacy_setup import get_nlp

# These tests need the en_core_web_sm model. Skip cleanly if it is absent.
requires_model = pytest.mark.skipif(not get_nlp(), reason="spaCy model en_core_web_sm not installed")


def _first(text: str) -> dict:
    """Return the first extracted fact, or {} if none."""
    facts = extract_facts(text)
    result = facts[0] if facts else {}
    return result


"""Copula sentences still extract correctly via spaCy."""


@requires_model
def test_copula_extraction_is_fact():
    """'X is Y' yields (X, is, Y)."""
    fact = _first("Paris is the capital of France.")
    assert fact["subject"] == "Paris"
    assert fact["predicate"] == "is"
    assert fact["obj"] == "capital of France"


@requires_model
def test_copula_extraction_are_fact():
    """Plural copula yields the 'are' predicate."""
    fact = _first("Cats are mammals.")
    assert fact["subject"] == "Cats"
    assert fact["predicate"] == "are"
    assert fact["obj"] == "mammals"


@requires_model
def test_copula_extraction_copula_with_preposition():
    """'X is in Y' yields the prepositional triple (X, in, Y)."""
    fact = _first("Paris is in France.")
    assert fact["subject"] == "Paris"
    assert fact["predicate"] == "in"
    assert fact["obj"] == "France"


"""Action-verb sentences (impossible for the copula extractor) work."""


@requires_model
def test_action_verb_extraction_transitive_verb():
    """SVO with a transitive verb uses the verb lemma as predicate."""
    fact = _first("Einstein developed the theory of relativity.")
    assert fact["subject"] == "Einstein"
    assert fact["predicate"] == "develop"
    assert "theory of relativity" in fact["obj"]


@requires_model
def test_action_verb_extraction_have_verb():
    """'has' is extracted as the lemma 'have'."""
    fact = _first("The Eiffel Tower has 1665 steps.")
    assert "Eiffel Tower" in fact["subject"]
    assert fact["predicate"] == "have"
    assert "steps" in fact["obj"]


@requires_model
def test_action_verb_extraction_verb_with_preposition():
    """An intransitive verb plus preposition yields 'lemma prep'."""
    fact = _first("The book belongs to Mary.")
    assert fact["subject"] == "book"
    assert fact["predicate"] == "belong to"
    assert fact["obj"] == "Mary"


"""Facts carry NER entity types for subject and object when recognized."""


@requires_model
def test_entity_types_gpe_subject():
    fact = _first("Paris is the capital of France.")
    assert fact["subject_type"] == "GPE"


@requires_model
def test_entity_types_org_entities():
    fact = _first("Microsoft acquired GitHub.")
    assert fact["subject_type"] == "ORG"
    assert fact["obj_type"] == "ORG"


@requires_model
def test_entity_types_person_object():
    fact = _first("The book belongs to Mary.")
    assert fact["obj_type"] == "PERSON"


@requires_model
def test_entity_types_unrecognized_entity_is_empty():
    # The small model does not tag every proper noun; type degrades to "".
    fact = _first("Cats are mammals.")
    assert fact["subject_type"] == ""


"""Questions, commands, and pronoun subjects are not extracted."""


@requires_model
def test_non_facts_skips_question():
    assert extract_facts("What is the capital of France?") == []


@requires_model
def test_non_facts_skips_command():
    assert extract_facts("Remember that dogs are loyal.") == []


@requires_model
def test_non_facts_skips_pronoun_subject():
    assert extract_facts("I am happy.") == []


@requires_model
def test_non_facts_empty_text():
    assert extract_facts("") == []
    assert extract_facts("   ") == []


"""Multiple declarative sentences yield multiple facts."""


@requires_model
def test_multiple_sentences_two_facts():
    facts = extract_facts("Cats are mammals. Shakespeare wrote Hamlet.")
    triples = {(f["subject"], f["predicate"], f["obj"]) for f in facts}
    assert ("Cats", "are", "mammals") in triples
    assert ("Shakespeare", "write", "Hamlet") in triples


"""The use_spacy_facts flag exists and is opt-in."""


def test_config_flag_default_off():
    assert engram_config()["use_spacy_facts"] is False


def test_config_flag_can_enable():
    assert engram_config(use_spacy_facts=True)["use_spacy_facts"] is True
