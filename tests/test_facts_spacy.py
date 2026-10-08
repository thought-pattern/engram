"""Tests for spaCy dependency-parse fact extraction."""

from engram.config import engram_config
from engram.facts_spacy import extract_facts


def internal_first(text: str) -> dict:
    """Return the first extracted fact, or {} if none."""
    facts = extract_facts(text)
    result = facts[0] if facts else {}
    return result


"""Copula sentences still extract correctly via spaCy."""


def test_copula_extraction_is_fact():
    """'X is Y' yields (X, is, Y)."""
    fact = internal_first("Paris is the capital of France.")
    assert fact.get("subject", "") == "Paris"
    assert fact.get("predicate", "") == "is"
    assert fact.get("obj", "") == "capital of France"


def test_copula_extraction_are_fact():
    """Plural copula yields the 'are' predicate."""
    fact = internal_first("Cats are mammals.")
    assert fact.get("subject", "") == "Cats"
    assert fact.get("predicate", "") == "are"
    assert fact.get("obj", "") == "mammals"


def test_copula_extraction_copula_with_preposition():
    """'X is in Y' yields the prepositional triple (X, in, Y)."""
    fact = internal_first("Paris is in France.")
    assert fact.get("subject", "") == "Paris"
    assert fact.get("predicate", "") == "in"
    assert fact.get("obj", "") == "France"


"""Action-verb sentences (impossible for the copula extractor) work."""


def test_action_verb_extraction_transitive_verb():
    """SVO with a transitive verb uses the verb lemma as predicate."""
    fact = internal_first("Einstein developed the theory of relativity.")
    assert fact.get("subject", "") == "Einstein"
    assert fact.get("predicate", "") == "develop"
    assert "theory of relativity" in fact.get("obj", "")


def test_action_verb_extraction_have_verb():
    """'has' is extracted as the lemma 'have'."""
    fact = internal_first("The Eiffel Tower has 1665 steps.")
    assert "Eiffel Tower" in fact.get("subject", "")
    assert fact.get("predicate", "") == "have"
    assert "steps" in fact.get("obj", "")


def test_action_verb_extraction_verb_with_preposition():
    """An intransitive verb plus preposition yields 'lemma prep'."""
    fact = internal_first("The book belongs to Mary.")
    assert fact.get("subject", "") == "book"
    assert fact.get("predicate", "") == "belong to"
    assert fact.get("obj", "") == "Mary"


"""Facts carry NER entity types for subject and object when recognized."""


def test_entity_types_gpe_subject():
    fact = internal_first("Paris is the capital of France.")
    assert fact.get("subject_type", "") == "GPE"


def test_entity_types_org_entities():
    fact = internal_first("Microsoft acquired GitHub.")
    assert fact.get("subject_type", "") == "ORG"
    assert fact.get("obj_type", "") == "ORG"


def test_entity_types_person_object():
    fact = internal_first("The book belongs to Mary.")
    assert fact.get("obj_type", "") == "PERSON"


def test_entity_types_unrecognized_entity_is_empty():
    # The small model does not tag every proper noun; type degrades to "".
    fact = internal_first("Cats are mammals.")
    assert "subject_type" in fact
    assert fact.get("subject_type", "") == ""


"""Questions, commands, and pronoun subjects are not extracted."""


def test_non_facts_skips_question():
    assert extract_facts("What is the capital of France?") == []


def test_non_facts_skips_command():
    assert extract_facts("Remember that dogs are loyal.") == []


def test_non_facts_skips_pronoun_subject():
    assert extract_facts("I am happy.") == []


def test_non_facts_empty_text():
    assert extract_facts("") == []
    assert extract_facts("   ") == []


"""Multiple declarative sentences yield multiple facts."""


def test_multiple_sentences_two_facts():
    facts = extract_facts("Cats are mammals. Shakespeare wrote Hamlet.")
    triples = {(fact.get("subject", ""), fact.get("predicate", ""), fact.get("obj", "")) for fact in facts}
    assert ("Cats", "are", "mammals") in triples
    assert ("Shakespeare", "write", "Hamlet") in triples


"""The use_spacy_facts flag exists and is opt-in."""


def test_config_flag_default_off():
    config = engram_config()
    assert "use_spacy_facts" in config
    assert config.get("use_spacy_facts", False) is False


def test_config_flag_can_enable():
    assert engram_config(use_spacy_facts=True).get("use_spacy_facts", False) is True
