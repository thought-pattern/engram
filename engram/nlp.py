"""Natural Language Processing for ENGRAM.

This module provides NLP-based fact extraction using NLTK.
It parses declarative sentences and extracts subject-predicate-object
relationships for dynamic learning.
"""

from functools import lru_cache
from logging import getLogger

from nltk import ne_chunk
from nltk.metrics.distance import edit_distance
from nltk.tag import pos_tag
from nltk.tokenize import word_tokenize

from engram.constants import (
    COMMAND_WORDS,
    COPULAS,
    KIND_COMMAND,
    KIND_QUESTION,
    KIND_STATEMENT,
    MAX_FACT_SUBJECT_TOKENS,
    POSSESSIVE_PRONOUNS,
    PRONOUNS,
    QUESTION_WORDS,
)
from engram.nltk_data import ensure_resource
from engram.text import is_known_word, verb_only_word

LOGGER = getLogger(__name__)


@lru_cache(maxsize=1)
def ensure_nltk_data() -> None:
    """Ensure required NLTK data is present, fetching into the local data dir."""
    required = [
        ("tokenizers/punkt", "punkt"),
        ("tokenizers/punkt_tab", "punkt_tab"),
        ("taggers/averaged_perceptron_tagger", "averaged_perceptron_tagger"),
        ("taggers/averaged_perceptron_tagger_eng", "averaged_perceptron_tagger_eng"),
        ("chunkers/maxent_ne_chunker", "maxent_ne_chunker"),
        ("chunkers/maxent_ne_chunker_tab", "maxent_ne_chunker_tab"),
        ("corpora/words", "words"),
    ]
    for path, package in required:
        ensure_resource(path, package)


PROPER_NOUN_TAGS = {"NNP", "NNPS"}


def span_is_proper_noun(text: str, surface: str, *, require_tag: bool = True) -> bool:
    """Return whether a capitalized span is a name rather than an ordinary verb.

    WordNet verb-only tokens such as ``Continue`` are ordinary words even when
    a capital makes the tagger call them proper nouns. ``require_tag`` applies
    the NNP/NNPS gate used for a span that opens the sentence. A capital later
    in the sentence skips that gate, so a mid-sentence name still counts.
    """
    ensure_nltk_data()
    wanted = surface.split()
    if not wanted or any(verb_only_word(word) for word in wanted):
        result = False
        return result
    if not require_tag:
        result = True
        return result
    tagged = pos_tag(word_tokenize(text))
    width = len(wanted)
    for start in range(0, len(tagged) - width + 1):
        window = tagged[start : start + width]
        if [word for word, _ in window] != wanted:
            continue
        result = all(pos in PROPER_NOUN_TAGS for _, pos in window)
        return result
    result = False
    return result


def fact_subject_upper(fact: dict) -> str:
    """Subject in uppercase for pattern matching."""
    subject_upper = fact.get("subject", "").upper()
    return subject_upper


def fact_query_patterns(fact: dict) -> list[str]:
    """Generate patterns that should retrieve this fact."""
    subj = fact_subject_upper(fact)
    obj = fact.get("obj", "").upper()
    patterns = [subj]  # Direct query: "CATS"

    if fact.get("predicate", "") in ("are", "were"):
        patterns.append(f"WHAT ARE {subj}")
        patterns.append(f"WHAT ARE THE {subj}")
        patterns.append(f"WHAT {fact.get('predicate', "").upper()} {subj}")
    else:
        patterns.append(f"WHAT IS {subj}")
        patterns.append(f"WHAT IS THE {subj}")
        patterns.append(f"WHAT IS A {subj}")
        patterns.append(f"WHO IS {subj}")
        patterns.append(f"WHAT {fact.get('predicate', "").upper()} {subj}")

    patterns.append(f"TELL ME ABOUT {subj}")
    patterns.append(f"TELL ME ABOUT THE {subj}")
    patterns.append(f"WHAT DO YOU KNOW ABOUT {subj}")
    patterns.append(f"WHAT DO YOU KNOW ABOUT THE {subj}")
    patterns.append(f"WHAT DO YOU REMEMBER ABOUT {subj}")
    patterns.append(f"WHAT DO YOU REMEMBER ABOUT THE {subj}")
    patterns.append(f"WHAT DID I SAY ABOUT {subj}")
    patterns.append(f"WHAT DID I SAY ABOUT THE {subj}")
    patterns.append(f"DO YOU REMEMBER {subj}")
    patterns.append(f"DO YOU REMEMBER THE {subj}")

    # Inverse lookup: "Sushi is good" should answer "What's good?"
    # while retaining the full original sentence as the response.
    patterns.append(f"WHAT IS {obj}")
    patterns.append(f"WHAT ARE {obj}")

    return patterns


def is_question(text: str) -> bool:
    """Check if text is a question.

    Three detectors: a trailing question mark, a question-word lead
    (what/who/where/...), or an inverted copula ("Is it ...").
    """
    if text.rstrip().endswith("?"):
        result = True
        return result

    first_word = text.split()[0].lower() if text.split() else ""
    if first_word in QUESTION_WORDS:
        result = True
        return result

    # Starts with a typo'd question word: a leading token that is not a real
    # word but sits one edit from a question word ("waht", "whta") reads as a
    # question even though it defeats the exact check -- without this, typo'd
    # questions get statement deflections and are learned as junk facts.
    if first_word and not is_known_word(first_word):
        for question_word in QUESTION_WORDS:
            if edit_distance(first_word, question_word, transpositions=True) <= 1:
                result = True
                return result

    words = text.lower().split()
    result = len(words) >= 2 and words[0] in COPULAS
    return result


def input_kind(text: str) -> str:
    """Classify input as a question, command, or statement.

    This is the routing signal for intent-aware responses: a catch-all
    deflection authored for statements ("Why do you say that?") reads absurd
    after a question, so templates branch on this via the {qtype:...}
    transform, and the pipeline routes unanswered questions through keyword
    retrieval before falling back.
    """
    if is_question(text):
        return KIND_QUESTION
    if internal_is_command(text):
        return KIND_COMMAND
    return KIND_STATEMENT


def internal_is_command(text: str) -> bool:
    """Check if text is a command."""
    first_word = text.split()[0].lower() if text.split() else ""
    is_command = first_word in COMMAND_WORDS
    return is_command


def clean_subject(tokens: list[str]) -> str:
    """Clean subject tokens for use as pattern."""
    if not tokens:
        result = ""
        return result

    while tokens and tokens[0].lower() in ("a", "an", "the"):
        tokens = tokens[1:]

    cleaned = " ".join(tokens)
    return cleaned


def extract_copula_fact(tokens: list[str], tagged: list[tuple[str, str]], original: str) -> dict:
    """Extract fact from a copula sentence (X is/are Y).

    The span before the copula must look like a plain noun phrase; anything
    else is conversation about something rather than a definitional statement,
    and learning it would store junk facts with unusable retrieval patterns
    (e.g. "Your sentiment analysis should inform that tired is not nice"
    splitting at "is").
    """
    copula_idx = -1
    copula = ""

    for i, (word, pos) in enumerate(tagged):
        if word.lower() in COPULAS and pos.startswith("VB"):
            copula_idx = i
            copula = word.lower()
            break

    if copula_idx <= 0:
        result = {}
        return result

    # Guardrail: a verb or modal before the copula means the copula belongs to
    # an embedded clause, not "subject is object".
    for index, (_, pos) in enumerate(tagged[:copula_idx]):
        noun_like_ing_subject = index == 0 and copula_idx == 1 and pos == "VBG"
        if (pos.startswith("VB") and not noun_like_ing_subject) or pos == "MD":
            result = {}
            return result

    subject_tokens = tokens[:copula_idx]

    obj_tokens = tokens[copula_idx + 1 :]

    subject = clean_subject(subject_tokens)
    obj = " ".join(obj_tokens).rstrip(".")

    if not subject or not obj:
        result = {}
        return result

    subject_words = subject.split()

    # Guardrail: long subjects are clauses, not names of things.
    if len(subject_words) > MAX_FACT_SUBJECT_TOKENS:
        result = {}
        return result

    # Guardrail: a pronoun or possessive anywhere in the subject means it is
    # conversational reference, not the name of a thing -- "you all",
    # "lol that", "sorry my typing", or a bare "that".
    for word in subject_words:
        if word.lower() in PRONOUNS or word.lower() in POSSESSIVE_PRONOUNS:
            result = {}
            return result

    # Guardrail: a possessive in the object ("waht is your name") marks a
    # personal exchange, not a world fact worth retrieval patterns.
    for word in obj.split():
        if word.lower() in POSSESSIVE_PRONOUNS:
            result = {}
            return result

    normalized_original = original.rstrip(".") + "."
    # An extracted fact carries the subject, the copula verb as predicate, the
    # complement as obj, and the original sentence.
    fact = {"subject": subject, "predicate": copula, "obj": obj, "original": normalized_original}
    return fact


def extract_fact(text: str) -> dict:
    """Extract a fact from a declarative sentence.

    Args:
        text: Input text to analyze.

    Returns:
        The fact dict (subject, predicate, obj, original) if a fact was
        extracted, otherwise an empty dict.
    """
    ensure_nltk_data()

    text = text.strip()
    if not text:
        result = {}
        return result

    if is_question(text):
        result = {}
        return result

    if internal_is_command(text):
        result = {}
        return result

    tokens = word_tokenize(text)
    tagged = pos_tag(tokens)

    if len(tokens) < 3:
        result = {}
        return result

    fact = extract_copula_fact(tokens, tagged, text)
    return fact


def token_offsets(text: str, tokens: list[str]) -> list[tuple[int, int]]:
    """Return each tokenizer token's exact [start, end) range in text, in order.

    Tokens are verbatim slices separated only by whitespace, except that the
    Treebank tokenizer rewrites a straight double quote as `` or ''. An empty
    list means the tokens could not be aligned to the text.
    """
    offsets = []
    position = 0
    for token in tokens:
        while position < len(text) and text[position].isspace():
            position += 1
        surfaces = (token, '"') if token in ("``", "''") else (token,)
        surface = next((candidate for candidate in surfaces if text.startswith(candidate, position)), "")
        if not surface:
            offsets = []
            return offsets
        offsets.append((position, position + len(surface)))
        position += len(surface)
    return offsets


def extract_entities(text: str) -> list[dict]:
    """Extract named entities from text using NLTK NER.

    Recognizes:
    - PERSON: People's names
    - ORGANIZATION: Companies, institutions
    - GPE: Geopolitical entities (countries, cities, states)
    - FACILITY: Buildings, airports, highways
    - GSP: Geo-socio-political groups

    Args:
        text: Input text to analyze.

    Returns:
        List of entity dicts: text (the entity's tokens joined by single
        spaces), label (PERSON/ORGANIZATION/GPE/...), and the exact
        [start, end) character range those tokens occupy in text.
    """
    ensure_nltk_data()

    if not text or not text.strip():
        result = []
        return result

    tokens = word_tokenize(text)
    offsets = token_offsets(text, tokens)
    if len(offsets) != len(tokens):
        # Coordinates come only from aligned tokens; none are invented.
        LOGGER.warning("named-entity tokens do not align with their source text; no entities extracted")
        result = []
        return result
    tagged = pos_tag(tokens)
    tree = ne_chunk(tagged)

    entities = []
    index = 0

    for subtree in tree:
        if hasattr(subtree, "label"):
            words = [word for word, tag in subtree.leaves()]
            entity = {
                "text": " ".join(words),
                "label": subtree.label(),
                "start": offsets[index][0],
                "end": offsets[index + len(words) - 1][1],
            }
            entities.append(entity)
            index += len(words)
        else:
            index += 1

    return entities
