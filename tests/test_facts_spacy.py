"""Tests for spaCy dependency-parse fact extraction."""

import pytest

from engram.config import EngramConfig
from engram.facts_spacy import extract_facts
from engram.spacy_setup import get_nlp

# These tests need the en_core_web_sm model. Skip cleanly if it is absent.
requires_model = pytest.mark.skipif(not get_nlp(), reason="spaCy model en_core_web_sm not installed")


def _first(text: str) -> dict:
    """Return the first extracted fact, or {} if none."""
    facts = extract_facts(text)
    return facts[0] if facts else {}


@requires_model
class TestCopulaExtraction:
    """Copula sentences still extract correctly via spaCy."""

    def test_is_fact(self):
        """'X is Y' yields (X, is, Y)."""
        fact = _first("Paris is the capital of France.")
        assert fact["subject"] == "Paris"
        assert fact["predicate"] == "is"
        assert fact["obj"] == "capital of France"

    def test_are_fact(self):
        """Plural copula yields the 'are' predicate."""
        fact = _first("Cats are mammals.")
        assert fact["subject"] == "Cats"
        assert fact["predicate"] == "are"
        assert fact["obj"] == "mammals"

    def test_copula_with_preposition(self):
        """'X is in Y' yields the prepositional triple (X, in, Y)."""
        fact = _first("Paris is in France.")
        assert fact["subject"] == "Paris"
        assert fact["predicate"] == "in"
        assert fact["obj"] == "France"


@requires_model
class TestActionVerbExtraction:
    """Action-verb sentences (impossible for the copula extractor) work."""

    def test_transitive_verb(self):
        """SVO with a transitive verb uses the verb lemma as predicate."""
        fact = _first("Einstein developed the theory of relativity.")
        assert fact["subject"] == "Einstein"
        assert fact["predicate"] == "develop"
        assert "theory of relativity" in fact["obj"]

    def test_have_verb(self):
        """'has' is extracted as the lemma 'have'."""
        fact = _first("The Eiffel Tower has 1665 steps.")
        assert "Eiffel Tower" in fact["subject"]
        assert fact["predicate"] == "have"
        assert "steps" in fact["obj"]

    def test_verb_with_preposition(self):
        """An intransitive verb plus preposition yields 'lemma prep'."""
        fact = _first("The book belongs to Mary.")
        assert fact["subject"] == "book"
        assert fact["predicate"] == "belong to"
        assert fact["obj"] == "Mary"


@requires_model
class TestEntityTypes:
    """Facts carry NER entity types for subject and object when recognized."""

    def test_gpe_subject(self):
        fact = _first("Paris is the capital of France.")
        assert fact["subject_type"] == "GPE"

    def test_org_entities(self):
        fact = _first("Microsoft acquired GitHub.")
        assert fact["subject_type"] == "ORG"
        assert fact["obj_type"] == "ORG"

    def test_person_object(self):
        fact = _first("The book belongs to Mary.")
        assert fact["obj_type"] == "PERSON"

    def test_unrecognized_entity_is_empty(self):
        # The small model does not tag every proper noun; type degrades to "".
        fact = _first("Cats are mammals.")
        assert fact["subject_type"] == ""


@requires_model
class TestNonFacts:
    """Questions, commands, and pronoun subjects are not extracted."""

    def test_skips_question(self):
        assert extract_facts("What is the capital of France?") == []

    def test_skips_command(self):
        assert extract_facts("Remember that dogs are loyal.") == []

    def test_skips_pronoun_subject(self):
        assert extract_facts("I am happy.") == []

    def test_empty_text(self):
        assert extract_facts("") == []
        assert extract_facts("   ") == []


@requires_model
class TestMultipleSentences:
    """Multiple declarative sentences yield multiple facts."""

    def test_two_facts(self):
        facts = extract_facts("Cats are mammals. Shakespeare wrote Hamlet.")
        triples = {(f["subject"], f["predicate"], f["obj"]) for f in facts}
        assert ("Cats", "are", "mammals") in triples
        assert ("Shakespeare", "write", "Hamlet") in triples


class TestConfigFlag:
    """The use_spacy_facts flag exists and is opt-in."""

    def test_default_off(self):
        assert EngramConfig()["use_spacy_facts"] is False

    def test_can_enable(self):
        assert EngramConfig(use_spacy_facts=True)["use_spacy_facts"] is True
