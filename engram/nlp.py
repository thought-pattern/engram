"""Natural Language Processing for ENGRAM.

This module provides NLP-based fact extraction using NLTK.
It parses declarative sentences and extracts subject-predicate-object
relationships for dynamic learning.
"""

from functools import lru_cache

from nltk import ne_chunk
from nltk.tag import pos_tag
from nltk.tokenize import word_tokenize

from engram.nltk_data import ensure_resource

# Copula verbs that indicate definitional statements
_COPULAS = frozenset({"is", "are", "was", "were"})

# Words that indicate a question (should not extract facts)
_QUESTION_WORDS = frozenset({"what", "who", "where", "when", "why", "how", "which", "whose"})

# Words that indicate a command (should not extract facts)
_COMMAND_WORDS = frozenset({"learn", "remember", "forget", "tell", "say", "repeat", "echo"})


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
        patterns.append(f"WHAT {fact['predicate'].upper()} {subj}")

    # Add "TELL ME ABOUT X" form
    patterns.append(f"TELL ME ABOUT {subj}")
    patterns.append(f"TELL ME ABOUT THE {subj}")

    return patterns


def _is_question(text: str) -> bool:
    """Check if text is a question."""
    # Ends with question mark
    if text.rstrip().endswith("?"):
        return True

    # Starts with question word
    first_word = text.split()[0].lower() if text.split() else ""
    if first_word in _QUESTION_WORDS:
        return True

    # Inverted subject-verb (e.g., "Is it...")
    words = text.lower().split()
    if len(words) >= 2 and words[0] in _COPULAS:
        return True

    return False


def _is_command(text: str) -> bool:
    """Check if text is a command."""
    first_word = text.split()[0].lower() if text.split() else ""
    is_command = first_word in _COMMAND_WORDS
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
    """Extract fact from a copula sentence (X is/are Y)."""
    # Find the copula
    copula_idx = -1
    copula = ""

    for i, (word, pos) in enumerate(tagged):
        if word.lower() in _COPULAS and pos.startswith("VB"):
            copula_idx = i
            copula = word.lower()
            break

    if copula_idx <= 0:
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

    # Skip if subject is just a pronoun (I, you, he, she, it, they, we)
    if subject.lower() in ("i", "you", "he", "she", "it", "they", "we"):
        return {}

    normalized_original = original.rstrip(".") + "."  # Normalize punctuation
    fact = extracted_fact(subject=subject, predicate=copula, obj=obj, original=normalized_original)
    return fact


def extract_fact(text: str) -> dict:
    """Extract a fact from a declarative sentence.

    Args:
        text: Input text to analyze.

    Returns:
        ExtractedFact if a fact was extracted, None otherwise.
    """
    _ensure_nltk_data()

    # Clean and normalize
    text = text.strip()
    if not text:
        return {}

    # Skip questions
    if _is_question(text):
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
