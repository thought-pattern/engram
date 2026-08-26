"""Text substitution maps for ENGRAM.

This module provides substitution maps for normalizing input text and
performing pronoun/person transformations in templates.
"""

from functools import lru_cache

from nltk.tokenize import sent_tokenize

from engram.constants import DEFAULT_CONTRACTIONS, DEFAULT_GENDER, DEFAULT_PERSON, DEFAULT_PERSON2
from engram.nltk_data import ensure_resource


@lru_cache(maxsize=1)
def _ensure_punkt() -> None:
    """Ensure the punkt tokenizers are present (cached, runs once)."""
    ensure_resource("tokenizers/punkt", "punkt")
    ensure_resource("tokenizers/punkt_tab", "punkt_tab")


def substitution_maps(
    contractions=DEFAULT_CONTRACTIONS,
    person=DEFAULT_PERSON,
    person2=DEFAULT_PERSON2,
    gender=DEFAULT_GENDER,
    custom=(),
) -> dict:
    """Build a container dict holding all substitution maps."""
    maps = {
        "contractions": dict(contractions),
        "person": dict(person),
        "person2": dict(person2),
        "gender": dict(gender),
        "custom": dict(custom or ()),
    }
    return maps


def get_all_input_subs(maps: dict) -> dict[str, str]:
    """Get combined substitution map for input normalization.

    Returns contractions and custom substitutions merged.
    """
    result = {}
    result.update(maps["contractions"])
    result.update(maps["custom"])
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
        prefix = ""
        suffix = ""
        core = word

        while core and not core[0].isalnum():
            prefix += core[0]
            core = core[1:]

        while core and not core[-1].isalnum():
            suffix = core[-1] + suffix
            core = core[:-1]

        if not core:
            result.append(word)
            continue

        lower_core = core.lower()
        if lower_core in subs:
            replacement = subs[lower_core]
            if core.isupper():
                replacement = replacement.upper()
            elif core[0].isupper():
                replacement = replacement.capitalize()
            result.append(prefix + replacement + suffix)
        else:
            result.append(word)

    substituted = " ".join(result)
    return substituted


def expand_contractions(text: str, contractions=DEFAULT_CONTRACTIONS) -> str:
    """Expand contractions in text.

    Args:
        text: Input text.
        contractions: Custom contractions map; defaults to the standard map.

    Returns:
        Text with contractions expanded.
    """
    expanded = apply_substitutions(text, contractions)
    return expanded


def apply_person(text: str, person_map=DEFAULT_PERSON) -> str:
    """Apply person substitution (I/me -> you).

    Args:
        text: Input text.
        person_map: Custom person map; defaults to the standard map.

    Returns:
        Text with person substitutions applied.
    """
    substituted = apply_substitutions(text, person_map)
    return substituted


def apply_person2(text: str, person2_map=DEFAULT_PERSON2) -> str:
    """Apply person2 substitution (you -> I/me).

    Args:
        text: Input text.
        person2_map: Custom person2 map; defaults to the standard map.

    Returns:
        Text with person2 substitutions applied.
    """
    substituted = apply_substitutions(text, person2_map)
    return substituted


def apply_gender(text: str, gender_map=DEFAULT_GENDER) -> str:
    """Apply gender substitution (gendered pronouns -> singular they/them).

    Args:
        text: Input text.
        gender_map: Custom gender map; defaults to the standard map.

    Returns:
        Text with gender substitutions applied.
    """
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
        result = []
        return result

    _ensure_punkt()
    sentences = sent_tokenize(text)

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

    result = " ".join(result.split())

    return result
