"""Friendly phrasing of recalled canonical facts.

A recalled fact is a canonical triple whose predicate is the graph's
slug (``located_in``). The raw projection reads ``Athens located_in
Greece``; this renders ``Athens is located in Greece.``. The wooden part
is the connector -- whether a predicate wants a copula (``is located
in``), a passive (``was authored by``), a possessive noun role (``'s
performer is``), or a bare active verb (``owns``). That split is a
grammatical property of each predicate, and spaCy's morphology recovers
it where a word list or NLTK's context-free tags cannot: ``VerbForm=Fin``
marks an active finite verb (``owns``, ``leads to``), ``VerbForm=Part`` a
stative participle (``located in``), and a nominal head a noun role
(``performer``). NLTK collapses ``located`` and ``founded`` to one
``VBN``; spaCy separates them and gives the head POS that picks the
article (``a member of`` vs ``located in``).

The frame is derived from each predicate without retaining hidden module
state. A small override map keyed by slug corrects spaCy's residual
single-token misreads (``precedes`` reads as a noun) and gives the temporal
predicates an idiom (``date of birth`` -> ``was born on``). An override never
calls spaCy, so those phrase even if the model is unavailable; everything
else degrades to a bare active verb rather than crashing -- ENGRAM's recall
is best-effort.

This is presentation only. ENGRAM keeps its own copy of the frame logic;
it does not depend on Tapestry.
"""

from engram.constants import FRAME_OVERRIDES, VOWELS
from engram.spacy_setup import get_nlp


def deslug(predicate: str) -> str:
    """Turn a canonical slug into its surface label (``located_in`` -> ``located in``)."""
    result = predicate.replace("_", " ").strip()
    return result


def frame_for_label(label: str) -> str:
    """Derive a ``{s} ... {o}`` frame from a label's grammar via spaCy morphology.

    Degrades to a bare active frame if the model is unavailable, so a lost
    spaCy install weakens phrasing rather than breaking recall.
    """
    nlp = get_nlp(disable=("parser", "ner"))
    normalized = label.lower().strip()
    if not nlp:
        result = "{s} " + label + " {o}"
        return result

    doc = nlp(normalized)
    head = doc[0]
    ends_prep = doc[-1].pos_ == "ADP"
    verb_form = head.morph.get("VerbForm", [])

    if normalized.endswith(" by"):
        result = "{s} was " + label + " {o}"
        return result

    if verb_form == ["Fin"]:
        result = "{s} " + label + " {o}"
        return result

    if verb_form == ["Part"]:
        # A participle behind a preposition is stative (`located in`); a
        # bare participle is a past-tense active verb (`created`).
        if ends_prep:
            result = "{s} is " + label + " {o}"
            return result
        result = "{s} " + label + " {o}"
        return result

    if verb_form == ["Inf"]:
        # spaCy reads a bare standalone noun (`genre`) as a base-form verb;
        # in this vocabulary those are noun roles.
        result = "{s}'s " + label + " is {o}"
        return result

    if head.pos_ in ("NOUN", "PROPN", "ADJ"):
        if ends_prep:
            article = ""
            if head.pos_ in ("NOUN", "PROPN"):
                article = "an " if normalized[:1] in VOWELS else "a "
            result = "{s} is " + article + label + " {o}"
            return result
        result = "{s}'s " + label + " is {o}"
        return result

    result = "{s} " + label + " {o}"
    return result


def frame_for_predicate(predicate: str) -> str:
    """Return the configured or grammatically derived frame for a predicate slug."""
    override = FRAME_OVERRIDES.get(predicate, "")
    if override:
        result = override
        return result
    label = deslug(predicate)
    result = frame_for_label(label)
    return result


def phrase_fact(subject: str, predicate: str, obj: str) -> str:
    """Phrase one canonical triple as a friendly sentence, or "" if a slot is empty."""
    if not (subject and predicate and obj):
        result = ""
        return result
    frame = frame_for_predicate(predicate)
    result = frame.format(s=subject, o=obj) + "."
    return result


def phrase_facts(facts: list) -> str:
    """Phrase a list of (subject, predicate, object) triples into prose."""
    sentences = []
    for subject, predicate, obj in facts:
        sentence = phrase_fact(subject, predicate, obj)
        if sentence:
            sentences.append(sentence)
    result = " ".join(sentences)
    return result
