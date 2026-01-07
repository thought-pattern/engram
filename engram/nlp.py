"""Natural Language Processing for ENGRAM.

This module provides NLP-based fact extraction using NLTK.
It parses declarative sentences and extracts subject-predicate-object
relationships for dynamic learning.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

import nltk
from nltk.tokenize import word_tokenize
from nltk.tag import pos_tag


# Ensure required NLTK data is available
def ensure_nltk_data() -> None:
    """Download required NLTK data if not present."""
    required = ['punkt', 'averaged_perceptron_tagger', 'punkt_tab', 'averaged_perceptron_tagger_eng']
    for package in required:
        try:
            nltk.data.find(f'tokenizers/{package}' if 'punkt' in package else f'taggers/{package}')
        except LookupError:
            nltk.download(package, quiet=True)


@dataclass
class ExtractedFact:
    """A fact extracted from natural language."""

    subject: str  # The subject of the statement (e.g., "Cats")
    predicate: Literal["is", "are", "was", "were"]  # The copula verb
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


class FactExtractor:
    """Extracts facts from declarative sentences using NLTK."""

    # Copula verbs that indicate definitional statements
    COPULAS = {'is', 'are', 'was', 'were'}

    # Words that indicate a question (should not extract facts)
    QUESTION_WORDS = {'what', 'who', 'where', 'when', 'why', 'how', 'which', 'whose'}

    # Words that indicate a command (should not extract facts)
    COMMAND_WORDS = {'learn', 'remember', 'forget', 'tell', 'say', 'repeat', 'echo'}

    def __init__(self):
        """Initialize the fact extractor."""
        ensure_nltk_data()

    def extract(self, text: str) -> ExtractedFact:
        """Extract a fact from a declarative sentence.

        Args:
            text: Input text to analyze.

        Returns:
            ExtractedFact if a fact was extracted, None otherwise.
        """
        # Clean and normalize
        text = text.strip()
        if not text:
            return None

        # Skip questions
        if self._is_question(text):
            return None

        # Skip commands
        if self._is_command(text):
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
        return self._extract_copula_fact(tokens, tagged, text)

    def _is_question(self, text: str) -> bool:
        """Check if text is a question."""
        # Ends with question mark
        if text.rstrip().endswith('?'):
            return True

        # Starts with question word
        first_word = text.split()[0].lower() if text.split() else ""
        if first_word in self.QUESTION_WORDS:
            return True

        # Inverted subject-verb (e.g., "Is it...")
        words = text.lower().split()
        if len(words) >= 2 and words[0] in self.COPULAS:
            return True

        return False

    def _is_command(self, text: str) -> bool:
        """Check if text is a command."""
        first_word = text.split()[0].lower() if text.split() else ""
        return first_word in self.COMMAND_WORDS

    def _extract_copula_fact(
        self,
        tokens: list[str],
        tagged: list[tuple[str, str]],
        original: str
    ) -> ExtractedFact:
        """Extract fact from a copula sentence (X is/are Y)."""
        # Find the copula
        copula_idx = None
        copula = None

        for i, (word, pos) in enumerate(tagged):
            if word.lower() in self.COPULAS and pos.startswith('VB'):
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
        subject = self._clean_subject(subject_tokens)
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

    def _clean_subject(self, tokens: list[str]) -> str:
        """Clean subject tokens for use as pattern."""
        if not tokens:
            return ""

        # Remove leading articles (a, an, the)
        while tokens and tokens[0].lower() in ('a', 'an', 'the'):
            tokens = tokens[1:]

        return ' '.join(tokens)


# Module-level instance for convenience
_extractor = None


def get_extractor() -> FactExtractor:
    """Get or create the module-level fact extractor."""
    global _extractor
    if _extractor is None:
        _extractor = FactExtractor()
    return _extractor


def extract_fact(text: str) -> ExtractedFact:
    """Extract a fact from text using the module-level extractor.

    Args:
        text: Input text to analyze.

    Returns:
        ExtractedFact if a fact was extracted, None otherwise.
    """
    return get_extractor().extract(text)
