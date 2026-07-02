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

The frame is derived once per predicate and memoized, so phrasing is a
dict lookup and a format after warm-up. A small override map keyed by
slug corrects spaCy's residual single-token misreads (``precedes`` reads
as a noun) and gives the temporal predicates an idiom (``date of birth``
-> ``was born on``). An override never calls spaCy, so those phrase even
if the model is unavailable; everything else degrades to a bare active
verb rather than crashing -- ENGRAM's recall is best-effort.

This is presentation only. ENGRAM keeps its own copy of the frame logic;
it does not depend on Tapestry.
"""

from engram.spacy_setup import get_nlp

VOWELS = set("aeiou")

# Predicate slug -> frame template with {s} / {o} slots. Two purposes:
#  - correct spaCy's context-free single-token misreads: `precedes` tags
#    as a plural noun, `dissolved date` reads its participle as active.
#  - give the frequent temporal predicates an idiom instead of the
#    grammatically-fine but robotic possessive (`X's date of birth is Y`).
FRAME_OVERRIDES = {
    "precedes": "{s} precedes {o}",
    "dissolved_date": "{s} was dissolved in {o}",
    "date_of_birth": "{s} was born on {o}",
    "date_of_death": "{s} died on {o}",
    "born_in": "{s} was born in {o}",
    "inception": "{s} was founded in {o}",
    "publication_date": "{s} was published on {o}",
    "point_in_time": "{s} occurred on {o}",
    "capital_of": "{s} is the capital of {o}",
    "has_capital": "{s}'s capital is {o}",
    "present_in_work": "{s} appears in {o}",
    "award_received": "{s} received {o}",
    "contains_location": "{s} contains {o}",
}

# slug -> frame template, memoized after the first spaCy parse.
_frame_cache: dict[str, str] = {}


def deslug(predicate: str) -> str:
    """Turn a canonical slug into its surface label (``located_in`` -> ``located in``)."""
    return predicate.replace("_", " ").strip()


def frame_for_label(label: str) -> str:
    """Derive a ``{s} ... {o}`` frame from a label's grammar via spaCy morphology.

    Degrades to a bare active frame if the model is unavailable, so a lost
    spaCy install weakens phrasing rather than breaking recall.
    """
    nlp = get_nlp(disable=("parser", "ner"))
    normalized = label.lower().strip()
    if not nlp:
        return "{s} " + label + " {o}"

    doc = nlp(normalized)
    head = doc[0]
    ends_prep = doc[-1].pos_ == "ADP"
    verb_form = head.morph.get("VerbForm")

    if normalized.endswith(" by"):
        return "{s} was " + label + " {o}"

    if verb_form == ["Fin"]:
        return "{s} " + label + " {o}"

    if verb_form == ["Part"]:
        # A participle behind a preposition is stative (`located in`); a
        # bare participle is a past-tense active verb (`created`).
        if ends_prep:
            return "{s} is " + label + " {o}"
        return "{s} " + label + " {o}"

    if verb_form == ["Inf"]:
        # spaCy reads a bare standalone noun (`genre`) as a base-form verb;
        # in this vocabulary those are noun roles.
        return "{s}'s " + label + " is {o}"

    if head.pos_ in ("NOUN", "PROPN", "ADJ"):
        if ends_prep:
            article = ""
            if head.pos_ in ("NOUN", "PROPN"):
                article = "an " if normalized[:1] in VOWELS else "a "
            return "{s} is " + article + label + " {o}"
        return "{s}'s " + label + " is {o}"

    return "{s} " + label + " {o}"


def frame_for_predicate(predicate: str) -> str:
    """Return the frame for a predicate slug, override first then memoized spaCy."""
    override = FRAME_OVERRIDES.get(predicate, "")
    if override:
        return override
    cached = _frame_cache.get(predicate, "")
    if not cached:
        cached = frame_for_label(deslug(predicate))
        _frame_cache[predicate] = cached
    return cached


def phrase_fact(subject: str, predicate: str, obj: str) -> str:
    """Phrase one canonical triple as a friendly sentence, or "" if a slot is empty."""
    if not (subject and predicate and obj):
        return ""
    frame = frame_for_predicate(predicate)
    return frame.format(s=subject, o=obj) + "."


def phrase_facts(facts: list) -> str:
    """Phrase a list of (subject, predicate, object) triples into prose."""
    sentences = []
    for subject, predicate, obj in facts:
        sentence = phrase_fact(subject, predicate, obj)
        if sentence:
            sentences.append(sentence)
    return " ".join(sentences)
