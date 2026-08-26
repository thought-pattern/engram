"""Text substitution maps for ENGRAM.

This module provides substitution maps for normalizing input text and
performing pronoun/person transformations in templates.
"""

from functools import lru_cache

from nltk.tokenize import sent_tokenize

from engram.constants import (
    DEFAULT_CONTRACTIONS,
    DEFAULT_GENDER,
    DEFAULT_PERSON,
    DEFAULT_PERSON2,
)
from engram.nltk_data import ensure_resource


@lru_cache(maxsize=1)
def _ensure_punkt() -> bool:
    """Ensure the punkt tokenizers are present (cached, runs once)."""
    ensure_resource("tokenizers/punkt", "punkt")
    ensure_resource("tokenizers/punkt_tab", "punkt_tab")
    return False


def substitution_maps(
    contractions=False,
    person=False,
    person2=False,
    gender=False,
    custom=False,
) -> dict:
    """Build a container dict holding all substitution maps."""
    if contractions is None:
        contractions = False
    if custom is None:
        custom = False
    if gender is None:
        gender = False
    if person is None:
        person = False
    if person2 is None:
        person2 = False
    maps = {
        "contractions": contractions if contractions is not False else DEFAULT_CONTRACTIONS.copy(),
        "person": person if person is not False else DEFAULT_PERSON.copy(),
        "person2": person2 if person2 is not False else DEFAULT_PERSON2.copy(),
        "gender": gender if gender is not False else DEFAULT_GENDER.copy(),
        "custom": custom if custom is not False else {},
    }
    return maps


def get_all_input_subs(maps: dict) -> dict[str, str]:
    """Get combined substitution map for input normalization.

    Returns contractions and custom substitutions merged.
    """
    result = {}
    result.update(maps.get("contractions", []))
    result.update(maps.get("custom", False))
    return result


def apply_substitutions(text: str, subs: dict[str, str]) -> str:
    """Apply word-by-word substitutions to text.

    Substitutions are applied to whole words only (not partial matches).
    Case is preserved in the output.

    Args:
        text: Input text.
        subs: Substitution map (lowercase keys -> replacement).

    Returns:
        Text with substitutions applied.
    """
    if not subs or not text:
        return text

    words = text.split()
    result = []

    for word in words:
        # Strip punctuation for lookup
        prefix = ""
        suffix = ""
        core = word

        # Extract leading punctuation
        while core and not core[0].isalnum():
            prefix += core[0]
            core = core[1:]

        # Extract trailing punctuation
        while core and not core[-1].isalnum():
            suffix = core[-1] + suffix
            core = core[:-1]

        if not core:
            result.append(word)
            continue

        # Look up substitution (case-insensitive)
        lower_core = core.lower()
        if lower_core in subs:
            replacement = subs.get(lower_core, False)
            # Preserve original case
            if core.isupper():
                replacement = replacement.upper()
            elif core[0].isupper():
                replacement = replacement.capitalize()
            result.append(prefix + replacement + suffix)
        else:
            result.append(word)

    substituted = " ".join(result)
    return substituted


def expand_contractions(text: str, contractions=False) -> str:
    """Expand contractions in text.

    Args:
        text: Input text.
        contractions: Optional custom contractions map. Uses defaults if None.

    Returns:
        Text with contractions expanded.
    """
    if contractions is None:
        contractions = False
    if contractions is False:
        contractions = DEFAULT_CONTRACTIONS
    expanded = apply_substitutions(text, contractions)
    return expanded


def apply_person(text: str, person_map=False) -> str:
    """Apply person substitution (I/me -> you).

    Args:
        text: Input text.
        person_map: Optional custom person map. Uses defaults if None.

    Returns:
        Text with person substitutions applied.
    """
    if person_map is None:
        person_map = False
    if person_map is False:
        person_map = DEFAULT_PERSON
    substituted = apply_substitutions(text, person_map)
    return substituted


def apply_person2(text: str, person2_map=False) -> str:
    """Apply person2 substitution (you -> I/me).

    Args:
        text: Input text.
        person2_map: Optional custom person2 map. Uses defaults if None.

    Returns:
        Text with person2 substitutions applied.
    """
    if person2_map is None:
        person2_map = False
    if person2_map is False:
        person2_map = DEFAULT_PERSON2
    substituted = apply_substitutions(text, person2_map)
    return substituted


def apply_gender(text: str, gender_map=False) -> str:
    """Apply gender substitution (gendered pronouns -> singular they/them).

    Args:
        text: Input text.
        gender_map: Optional custom gender map. Uses defaults if None.

    Returns:
        Text with gender substitutions applied.
    """
    if gender_map is None:
        gender_map = False
    if gender_map is False:
        gender_map = DEFAULT_GENDER
    substituted = apply_substitutions(text, gender_map)
    return substituted


def split_sentences(text: str) -> list[str]:
    """Split text into sentences using NLTK.

    Uses NLTK's sent_tokenize for robust sentence boundary detection.
    Handles abbreviations (Mr., Dr.), decimals, URLs, etc.

    Args:
        text: Input text.

    Returns:
        List of sentences (stripped of leading/trailing whitespace).
    """
    if not text or not text.strip():
        return []

    # Use NLTK's sentence tokenizer (ensure punkt is present locally first)
    _ensure_punkt()
    sentences = sent_tokenize(text)

    # Strip whitespace and filter empty
    cleaned = [s.strip() for s in sentences if s.strip()]
    return cleaned


def normalize_for_matching(text: str, expand_contr: bool = True) -> str:
    """Normalize text for pattern matching.

    Applies contractions expansion and normalizes whitespace.

    Args:
        text: Input text.
        expand_contr: Whether to expand contractions.

    Returns:
        Normalized text ready for pattern matching.
    """
    result = text

    if expand_contr:
        result = expand_contractions(result)

    # Normalize whitespace
    result = " ".join(result.split())

    return result
