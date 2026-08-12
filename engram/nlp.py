"""Natural Language Processing for ENGRAM.

This module provides NLP-based fact extraction using NLTK.
It parses declarative sentences and extracts subject-predicate-object
relationships for dynamic learning.
"""

from functools import lru_cache

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
from engram.text import is_known_word


@lru_cache(maxsize=1)
def _ensure_nltk_data() -> None:
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


def extracted_fact(subject: str, predicate: str, obj: str, original: str) -> dict:
    """Build a fact dict extracted from natural language.

    Keys: subject, predicate (copula verb), obj (complement), original sentence.
    """
    fact = {
        "subject": subject,
        "predicate": predicate,
        "obj": obj,
        "original": original,
    }
    return fact


def fact_subject_upper(fact: dict) -> str:
    """Subject in uppercase for pattern matching."""
    subject_upper = fact["subject"].upper()
    return subject_upper


def fact_query_patterns(fact: dict) -> list[str]:
    """Generate patterns that should retrieve this fact."""
    subj = fact_subject_upper(fact)
    obj = fact["obj"].upper()
    patterns = [subj]  # Direct query: "CATS"

    # Question forms based on predicate
    if fact["predicate"] in ("are", "were"):
        patterns.append(f"WHAT ARE {subj}")
        patterns.append(f"WHAT ARE THE {subj}")
        patterns.append(f"WHAT {fact['predicate'].upper()} {subj}")
    else:
        patterns.append(f"WHAT IS {subj}")
        patterns.append(f"WHAT IS THE {subj}")
        patterns.append(f"WHAT IS A {subj}")
        patterns.append(f"WHO IS {subj}")
        patterns.append(f"WHAT {fact['predicate'].upper()} {subj}")

    # Add "TELL ME ABOUT X" form
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
    # Ends with question mark
    if text.rstrip().endswith("?"):
        return True

    # Starts with question word
    first_word = text.split()[0].lower() if text.split() else ""
    if first_word in QUESTION_WORDS:
        return True

    # Starts with a typo'd question word: a leading token that is not a real
    # word but sits one edit from a question word ("waht", "whta") reads as a
    # question even though it defeats the exact check -- without this, typo'd
    # questions get statement deflections and are learned as junk facts.
    if first_word and not is_known_word(first_word):
        for question_word in QUESTION_WORDS:
            if edit_distance(first_word, question_word, transpositions=True) <= 1:
                return True

    # Inverted subject-verb (e.g., "Is it...")
    words = text.lower().split()
    return len(words) >= 2 and words[0] in COPULAS


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
    if _is_command(text):
        return KIND_COMMAND
    return KIND_STATEMENT


def _is_command(text: str) -> bool:
    """Check if text is a command."""
    first_word = text.split()[0].lower() if text.split() else ""
    is_command = first_word in COMMAND_WORDS
    return is_command


def _clean_subject(tokens: list[str]) -> str:
    """Clean subject tokens for use as pattern."""
    if not tokens:
        return ""

    # Remove leading articles (a, an, the)
    while tokens and tokens[0].lower() in ("a", "an", "the"):
        tokens = tokens[1:]

    cleaned = " ".join(tokens)
    return cleaned


def _extract_copula_fact(tokens: list[str], tagged: list[tuple[str, str]], original: str) -> dict:
    """Extract fact from a copula sentence (X is/are Y).

    The span before the copula must look like a plain noun phrase; anything
    else is conversation about something rather than a definitional statement,
    and learning it would store junk facts with unusable retrieval patterns
    (e.g. "Your sentiment analysis should inform that tired is not nice"
    splitting at "is").
    """
    # Find the copula
    copula_idx = -1
    copula = ""

    for i, (word, pos) in enumerate(tagged):
        if word.lower() in COPULAS and pos.startswith("VB"):
            copula_idx = i
            copula = word.lower()
            break

    if copula_idx <= 0:
        return {}

    # Guardrail: a verb or modal before the copula means the copula belongs to
    # an embedded clause, not "subject is object".
    for index, (_, pos) in enumerate(tagged[:copula_idx]):
        noun_like_ing_subject = index == 0 and copula_idx == 1 and pos == "VBG"
        if (pos.startswith("VB") and not noun_like_ing_subject) or pos == "MD":
            return {}

    # Extract subject (everything before copula)
    subject_tokens = tokens[:copula_idx]

    # Extract object (everything after copula)
    obj_tokens = tokens[copula_idx + 1 :]

    # Filter out articles from subject start for cleaner patterns
    subject = _clean_subject(subject_tokens)
    obj = " ".join(obj_tokens).rstrip(".")

    if not subject or not obj:
        return {}

    subject_words = subject.split()

    # Guardrail: long subjects are clauses, not names of things.
    if len(subject_words) > MAX_FACT_SUBJECT_TOKENS:
        return {}

    # Guardrail: a pronoun or possessive anywhere in the subject means it is
    # conversational reference, not the name of a thing -- "you all",
    # "lol that", "sorry my typing", or a bare "that".
    for word in subject_words:
        if word.lower() in PRONOUNS or word.lower() in POSSESSIVE_PRONOUNS:
            return {}

    # Guardrail: a possessive in the object ("waht is your name") marks a
    # personal exchange, not a world fact worth retrieval patterns.
    for word in obj.split():
        if word.lower() in POSSESSIVE_PRONOUNS:
            return {}

    normalized_original = original.rstrip(".") + "."  # Normalize punctuation
    fact = extracted_fact(subject=subject, predicate=copula, obj=obj, original=normalized_original)
    return fact


def extract_fact(text: str) -> dict:
    """Extract a fact from a declarative sentence.

    Args:
        text: Input text to analyze.

    Returns:
        ExtractedFact if a fact was extracted, otherwise an empty dict.
    """
    _ensure_nltk_data()

    # Clean and normalize
    text = text.strip()
    if not text:
        return {}

    # Skip questions
    if is_question(text):
        return {}

    # Skip commands
    if _is_command(text):
        return {}

    # Tokenize and tag
    try:
        tokens = word_tokenize(text)
        tagged = pos_tag(tokens)
    except Exception:
        return {}

    if len(tokens) < 3:
        return {}

    # Find copula and extract subject/object
    fact = _extract_copula_fact(tokens, tagged, text)
    return fact


def extracted_entity(text: str, label: str, start: int, end: int) -> dict:
    """Build a named-entity dict.

    Keys: text, label (PERSON/ORGANIZATION/GPE/...), start and end positions.
    """
    entity = {"text": text, "label": label, "start": start, "end": end}
    return entity


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
        List of ExtractedEntity objects.
    """
    _ensure_nltk_data()

    if not text or not text.strip():
        return []

    try:
        tokens = word_tokenize(text)
        tagged = pos_tag(tokens)
        tree = ne_chunk(tagged)

        entities = []
        current_pos = 0

        for subtree in tree:
            if hasattr(subtree, "label"):
                # This is a named entity
                entity_text = " ".join(word for word, tag in subtree)
                label = subtree.label()

                # Find position in original text
                start = text.find(entity_text, current_pos)
                if start == -1:
                    # Try case-insensitive search
                    start = text.lower().find(entity_text.lower(), current_pos)
                if start != -1:
                    end = start + len(entity_text)
                    current_pos = end
                else:
                    start = current_pos
                    end = current_pos + len(entity_text)

                entities.append(
                    extracted_entity(
                        text=entity_text,
                        label=label,
                        start=start,
                        end=end,
                    )
                )

        return entities

    except Exception:
        return []


def extract_entities_by_type(text: str) -> dict[str, list[str]]:
    """Extract named entities grouped by type.

    Args:
        text: Input text to analyze.

    Returns:
        Dict mapping entity types to lists of entity texts.
        Example: {"PERSON": ["John Smith"], "GPE": ["New York", "France"]}
    """
    entities = extract_entities(text)
    by_type: dict[str, list[str]] = {}

    for entity in entities:
        if entity["label"] not in by_type:
            by_type[entity["label"]] = []
        if entity["text"] not in by_type[entity["label"]]:
            by_type[entity["label"]].append(entity["text"])

    return by_type


def get_people(text: str) -> list[str]:
    """Extract person names from text.

    Args:
        text: Input text to analyze.

    Returns:
        List of person names found.
    """
    entities = extract_entities(text)
    people = [e["text"] for e in entities if e["label"] == "PERSON"]
    return people


def get_places(text: str) -> list[str]:
    """Extract place names from text.

    Args:
        text: Input text to analyze.

    Returns:
        List of place names found (GPE and FACILITY entities).
    """
    entities = extract_entities(text)
    places = [e["text"] for e in entities if e["label"] in ("GPE", "FACILITY", "GSP")]
    return places


def get_organizations(text: str) -> list[str]:
    """Extract organization names from text.

    Args:
        text: Input text to analyze.

    Returns:
        List of organization names found.
    """
    entities = extract_entities(text)
    organizations = [e["text"] for e in entities if e["label"] == "ORGANIZATION"]
    return organizations
