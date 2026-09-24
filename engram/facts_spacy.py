"""Relational fact extraction using spaCy dependency parsing.

NLTK has no dependency parser, so the NLTK-based extractor in engram.nlp only
handles copula sentences ("X is Y"). This module uses spaCy's dependency parse
to pull subject-predicate-object triples from arbitrary declarative sentences,
which can feed opt-in local fact learning and read-only graph recall.

Opt-in via the use_spacy_facts config flag. The model is loaded lazily; if it
cannot be loaded, extraction returns an empty list rather than failing.

Triple conventions:
    "Paris is the capital of France"      -> (Paris, is, capital of France)
    "Paris is in France"                  -> (Paris, in, France)
    "Einstein developed the theory ..."   -> (Einstein, develop, theory of relativity)
Copulas keep their surface form (is/are); prepositional links use the
preposition; action verbs use the verb lemma so relations are normalized.
"""

from spacy.tokens import Token

from engram.constants import ARTICLES, COMMAND_WORDS, OBJECT_DEPS, PRONOUNS, QUESTION_WORDS, SUBJECT_DEPS
from engram.nlp import extracted_fact
from engram.spacy_setup import get_nlp


def clean_span(tokens) -> str:
    """Join tokens in document order, dropping a single leading article."""
    ordered = sorted(tokens, key=lambda t: t.i)
    words = [t.text for t in ordered]
    if words and words[0].lower() in ARTICLES:
        words = words[1:]
    span = " ".join(words).strip()
    return span


def internal_phrase(token) -> str:
    """Full subtree text for a token (captures 'capital of France', etc.)."""
    phrase = clean_span(list(token.subtree))
    return phrase


def first_child(token, deps) -> object:
    """Return the first child of token whose dependency is in deps, else ()."""
    for child in token.children:
        if child.dep_ in deps:
            return child
    result = ()
    return result


def prep_link(verb) -> tuple[str, object]:
    """Return (preposition_text, pobj_token) for a verb's first prep child.

    Returns ("", ()) when there is no prepositional object.
    """
    for child in verb.children:
        if child.dep_ == "prep":
            pobj = first_child(child, {"pobj"})
            if pobj:
                link = (child.text, pobj)
                return link
    empty_link = ("", ())
    return empty_link


def extract_from_sentence(sent) -> dict:
    """Extract a single triple from one parsed sentence, or {} if none."""
    first = sent[0].text.lower()
    if first in QUESTION_WORDS or first in COMMAND_WORDS:
        result = {}
        return result
    if sent.text.strip().endswith("?"):
        result = {}
        return result

    root = sent.root
    if root.pos_ not in ("VERB", "AUX"):
        # Broken or non-declarative parse (e.g. terse SVO the small model
        # mistags) - skip rather than emit a garbage triple.
        result = {}
        return result

    subject_token = first_child(root, SUBJECT_DEPS)
    if not isinstance(subject_token, Token):
        result = {}
        return result

    subject = internal_phrase(subject_token)
    if not subject or subject.lower() in PRONOUNS:
        result = {}
        return result

    is_copula = root.pos_ == "AUX" or root.lemma_ == "be"

    if is_copula:
        obj_token = first_child(root, OBJECT_DEPS)
        if isinstance(obj_token, Token):
            predicate = root.text  # keep surface "is"/"are"/"was"/"were"
        else:
            prep_text, obj_token = prep_link(root)
            if not isinstance(obj_token, Token):
                result = {}
                return result
            predicate = prep_text  # "Paris is in France" -> (Paris, in, France)
    else:
        obj_token = first_child(root, OBJECT_DEPS)
        if isinstance(obj_token, Token):
            predicate = root.lemma_  # normalized relation: develop, have, chase
        else:
            prep_text, obj_token = prep_link(root)
            if not isinstance(obj_token, Token):
                result = {}
                return result
            predicate = f"{root.lemma_} {prep_text}"  # "belong to"

    obj = internal_phrase(obj_token)
    if not obj or obj.lower() in PRONOUNS:
        result = {}
        return result

    fact = extracted_fact(
        subject=subject,
        predicate=predicate,
        obj=obj,
        original=sent.text.strip(),
    )
    # Entity types from NER (PERSON/GPE/ORG/DATE/...), "" when not an entity.
    fact["subject_type"] = subject_token.ent_type_
    fact["obj_type"] = obj_token.ent_type_
    return fact


def extract_facts(text: str) -> list:
    """Extract relational triples from text using spaCy dependency parsing.

    Args:
        text: Input text (may contain multiple sentences).

    Returns:
        List of fact dicts (subject, predicate, obj, original). Empty list if
        nothing is extractable or the spaCy model is unavailable.
    """
    if not text or not text.strip():
        result = []
        return result
    nlp = get_nlp()
    if not nlp:
        result = []
        return result
    doc = nlp(text)
    facts = []
    for sent in doc.sents:
        fact = extract_from_sentence(sent)
        if fact:
            facts.append(fact)
    return facts
