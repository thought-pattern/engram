"""Relational fact extraction using spaCy dependency parsing.

NLTK has no dependency parser, so the NLTK-based extractor in engram.nlp only
handles copula sentences ("X is Y"). This module uses spaCy's dependency parse
to pull subject-predicate-object triples from arbitrary declarative sentences,
which can feed the knowledge-graph triple layer (triple_add/triple_query).

Opt-in via the use_spacy_facts config flag. The model is loaded lazily; if it
cannot be loaded, extraction returns an empty list rather than failing.

Triple conventions:
    "Paris is the capital of France"      -> (Paris, is, capital of France)
    "Paris is in France"                  -> (Paris, in, France)
    "Einstein developed the theory ..."   -> (Einstein, develop, theory of relativity)
Copulas keep their surface form (is/are); prepositional links use the
preposition; action verbs use the verb lemma so relations are normalized.
"""

from engram.nlp import ExtractedFact
from engram.spacy_setup import get_nlp

_QUESTION_WORDS = frozenset({"what", "who", "where", "when", "why", "how", "which", "whose"})
_COMMAND_WORDS = frozenset({"learn", "remember", "forget", "tell", "say", "repeat", "echo"})
_PRONOUNS = frozenset({"i", "you", "he", "she", "it", "we", "they", "this", "that", "these", "those"})
_SUBJECT_DEPS = frozenset({"nsubj", "nsubjpass"})
_OBJECT_DEPS = frozenset({"dobj", "attr", "acomp", "oprd", "dative"})
_ARTICLES = frozenset({"a", "an", "the"})


def _clean_span(tokens) -> str:
    """Join tokens in document order, dropping a single leading article."""
    ordered = sorted(tokens, key=lambda t: t.i)
    words = [t.text for t in ordered]
    if words and words[0].lower() in _ARTICLES:
        words = words[1:]
    return " ".join(words).strip()


def _phrase(token) -> str:
    """Full subtree text for a token (captures 'capital of France', etc.)."""
    return _clean_span(list(token.subtree))


def _first_child(token, deps) -> object:
    """Return the first child of token whose dependency is in deps, else ()."""
    for child in token.children:
        if child.dep_ in deps:
            return child
    return ()


def _prep_link(verb):
    """Return (preposition_text, pobj_token) for a verb's first prep child.

    Returns ("", ()) when there is no prepositional object.
    """
    for child in verb.children:
        if child.dep_ == "prep":
            pobj = _first_child(child, frozenset({"pobj"}))
            if pobj:
                return child.text, pobj
    return "", ()


def _extract_from_sentence(sent) -> dict:
    """Extract a single triple from one parsed sentence, or {} if none."""
    first = sent[0].text.lower()
    if first in _QUESTION_WORDS or first in _COMMAND_WORDS:
        return {}
    if sent.text.strip().endswith("?"):
        return {}

    root = sent.root
    if root.pos_ not in ("VERB", "AUX"):
        # Broken or non-declarative parse (e.g. terse SVO the small model
        # mistags) - skip rather than emit a garbage triple.
        return {}

    subject_token = _first_child(root, _SUBJECT_DEPS)
    if not subject_token:
        return {}

    subject = _phrase(subject_token)
    if not subject or subject.lower() in _PRONOUNS:
        return {}

    is_copula = root.pos_ == "AUX" or root.lemma_ == "be"

    if is_copula:
        obj_token = _first_child(root, _OBJECT_DEPS)
        if obj_token:
            predicate = root.text  # keep surface "is"/"are"/"was"/"were"
        else:
            prep_text, obj_token = _prep_link(root)
            if not obj_token:
                return {}
            predicate = prep_text  # "Paris is in France" -> (Paris, in, France)
    else:
        obj_token = _first_child(root, _OBJECT_DEPS)
        if obj_token:
            predicate = root.lemma_  # normalized relation: develop, have, chase
        else:
            prep_text, obj_token = _prep_link(root)
            if not obj_token:
                return {}
            predicate = f"{root.lemma_} {prep_text}"  # "belong to"

    obj = _phrase(obj_token)
    if not obj or obj.lower() in _PRONOUNS:
        return {}

    fact = ExtractedFact(
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
        return []
    nlp = get_nlp()
    if not nlp:
        return []
    doc = nlp(text)
    facts = []
    for sent in doc.sents:
        fact = _extract_from_sentence(sent)
        if fact:
            facts.append(fact)
    return facts
