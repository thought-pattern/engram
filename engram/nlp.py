"""Natural Language Processing for ENGRAM.

This module provides NLP-based fact extraction using NLTK.
It parses declarative sentences and extracts subject-predicate-object
relationships for dynamic learning.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache

import nltk
from nltk.tokenize import word_tokenize
from nltk.tag import pos_tag


# Copula verbs that indicate definitional statements
_COPULAS = frozenset({'is', 'are', 'was', 'were'})

# Words that indicate a question (should not extract facts)
_QUESTION_WORDS = frozenset({'what', 'who', 'where', 'when', 'why', 'how', 'which', 'whose'})

# Words that indicate a command (should not extract facts)
_COMMAND_WORDS = frozenset({'learn', 'remember', 'forget', 'tell', 'say', 'repeat', 'echo'})


@lru_cache(maxsize=1)
def _ensure_nltk_data() -> None:
    """Download required NLTK data if not present."""
    required = [
        ('tokenizers/punkt', 'punkt'),
        ('tokenizers/punkt_tab', 'punkt_tab'),
        ('taggers/averaged_perceptron_tagger', 'averaged_perceptron_tagger'),
        ('taggers/averaged_perceptron_tagger_eng', 'averaged_perceptron_tagger_eng'),
        ('chunkers/maxent_ne_chunker', 'maxent_ne_chunker'),
        ('chunkers/maxent_ne_chunker_tab', 'maxent_ne_chunker_tab'),
        ('corpora/words', 'words'),
    ]
    for path, package in required:
        try:
            nltk.data.find(path)
        except LookupError:
            nltk.download(package, quiet=True)


@dataclass
class ExtractedFact:
    """A fact extracted from natural language."""

    subject: str  # The subject of the statement (e.g., "Cats")
    predicate: str  # The copula verb (is, are, was, were)
    obj: str  # The object/complement (e.g., "mammals")
    original: str  # The original sentence

    @property
    def subject_upper(self) -> str:
        """Subject in uppercase for pattern matching."""
        return self.subject.upper()

    @property
    def query_patterns(self) -> list[str]:
        """Generate patterns that should retrieve this fact."""
        subj = self.subject_upper
        patterns = [subj]  # Direct query: "CATS"

        # Question forms based on predicate
        if self.predicate in ("are", "were"):
            patterns.append(f"WHAT ARE {subj}")
            patterns.append(f"WHAT ARE THE {subj}")
            patterns.append(f"WHAT {self.predicate.upper()} {subj}")
        else:
            patterns.append(f"WHAT IS {subj}")
            patterns.append(f"WHAT IS THE {subj}")
            patterns.append(f"WHAT IS A {subj}")
            patterns.append(f"WHAT {self.predicate.upper()} {subj}")

        # Add "TELL ME ABOUT X" form
        patterns.append(f"TELL ME ABOUT {subj}")
        patterns.append(f"TELL ME ABOUT THE {subj}")

        return patterns


def _is_question(text: str) -> bool:
    """Check if text is a question."""
    # Ends with question mark
    if text.rstrip().endswith('?'):
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
    return first_word in _COMMAND_WORDS


def _clean_subject(tokens: list[str]) -> str:
    """Clean subject tokens for use as pattern."""
    if not tokens:
        return ""

    # Remove leading articles (a, an, the)
    while tokens and tokens[0].lower() in ('a', 'an', 'the'):
        tokens = tokens[1:]

    return ' '.join(tokens)


def _extract_copula_fact(
    tokens: list[str],
    tagged: list[tuple[str, str]],
    original: str
) -> ExtractedFact | None:
    """Extract fact from a copula sentence (X is/are Y)."""
    # Find the copula
    copula_idx = None
    copula = None

    for i, (word, pos) in enumerate(tagged):
        if word.lower() in _COPULAS and pos.startswith('VB'):
            copula_idx = i
            copula = word.lower()
            break

    if copula_idx is None or copula_idx == 0:
        return None

    # Extract subject (everything before copula)
    subject_tokens = tokens[:copula_idx]

    # Extract object (everything after copula)
    obj_tokens = tokens[copula_idx + 1:]

    # Filter out articles from subject start for cleaner patterns
    subject = _clean_subject(subject_tokens)
    obj = ' '.join(obj_tokens).rstrip('.')

    if not subject or not obj:
        return None

    # Skip if subject is just a pronoun (I, you, he, she, it, they, we)
    if subject.lower() in ('i', 'you', 'he', 'she', 'it', 'they', 'we'):
        return None

    return ExtractedFact(
        subject=subject,
        predicate=copula,
        obj=obj,
        original=original.rstrip('.') + '.'  # Normalize punctuation
    )


def extract_fact(text: str) -> ExtractedFact | None:
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
        return None

    # Skip questions
    if _is_question(text):
        return None

    # Skip commands
    if _is_command(text):
        return None

    # Tokenize and tag
    try:
        tokens = word_tokenize(text)
        tagged = pos_tag(tokens)
    except Exception:
        return None

    if len(tokens) < 3:
        return None

    # Find copula and extract subject/object
    return _extract_copula_fact(tokens, tagged, text)


@dataclass
class ExtractedEntity:
    """A named entity extracted from text."""

    text: str  # The entity text (e.g., "John Smith")
    label: str  # Entity type (PERSON, ORGANIZATION, GPE, etc.)
    start: int  # Start position in original text
    end: int  # End position in original text


def extract_entities(text: str) -> list[ExtractedEntity]:
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
        from nltk import ne_chunk

        tokens = word_tokenize(text)
        tagged = pos_tag(tokens)
        tree = ne_chunk(tagged)

        entities = []
        current_pos = 0

        for subtree in tree:
            if hasattr(subtree, 'label'):
                # This is a named entity
                entity_text = ' '.join(word for word, tag in subtree)
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

                entities.append(ExtractedEntity(
                    text=entity_text,
                    label=label,
                    start=start,
                    end=end,
                ))

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
        if entity.label not in by_type:
            by_type[entity.label] = []
        if entity.text not in by_type[entity.label]:
            by_type[entity.label].append(entity.text)

    return by_type


def get_people(text: str) -> list[str]:
    """Extract person names from text.

    Args:
        text: Input text to analyze.

    Returns:
        List of person names found.
    """
    entities = extract_entities(text)
    return [e.text for e in entities if e.label == 'PERSON']


def get_places(text: str) -> list[str]:
    """Extract place names from text.

    Args:
        text: Input text to analyze.

    Returns:
        List of place names found (GPE and FACILITY entities).
    """
    entities = extract_entities(text)
    return [e.text for e in entities if e.label in ('GPE', 'FACILITY', 'GSP')]


def get_organizations(text: str) -> list[str]:
    """Extract organization names from text.

    Args:
        text: Input text to analyze.

    Returns:
        List of organization names found.
    """
    entities = extract_entities(text)
    return [e.text for e in entities if e.label == 'ORGANIZATION']
