"""Text substitution maps for ENGRAM.

This module provides substitution maps for normalizing input text and
performing pronoun/person transformations in templates.
"""

from functools import lru_cache

from nltk.tokenize import sent_tokenize

from engram.nltk_data import ensure_resource


@lru_cache(maxsize=1)
def _ensure_punkt() -> None:
    """Ensure the punkt tokenizers are present (cached, runs once)."""
    ensure_resource("tokenizers/punkt", "punkt")
    ensure_resource("tokenizers/punkt_tab", "punkt_tab")


# Default contractions expansion map
DEFAULT_CONTRACTIONS: dict[str, str] = {
    "i'm": "i am",
    "i've": "i have",
    "i'll": "i will",
    "i'd": "i would",
    "you're": "you are",
    "you've": "you have",
    "you'll": "you will",
    "you'd": "you would",
    "he's": "he is",
    "she's": "she is",
    "it's": "it is",
    "we're": "we are",
    "we've": "we have",
    "we'll": "we will",
    "we'd": "we would",
    "they're": "they are",
    "they've": "they have",
    "they'll": "they will",
    "they'd": "they would",
    "that's": "that is",
    "there's": "there is",
    "here's": "here is",
    "what's": "what is",
    "who's": "who is",
    "where's": "where is",
    "when's": "when is",
    "why's": "why is",
    "how's": "how is",
    "isn't": "is not",
    "aren't": "are not",
    "wasn't": "was not",
    "weren't": "were not",
    "haven't": "have not",
    "hasn't": "has not",
    "hadn't": "had not",
    "won't": "will not",
    "wouldn't": "would not",
    "don't": "do not",
    "doesn't": "does not",
    "didn't": "did not",
    "can't": "cannot",
    "couldn't": "could not",
    "shouldn't": "should not",
    "mightn't": "might not",
    "mustn't": "must not",
    "let's": "let us",
    "ain't": "is not",
    "y'all": "you all",
    "gonna": "going to",
    "gotta": "got to",
    "wanna": "want to",
    "gimme": "give me",
    "lemme": "let me",
    "kinda": "kind of",
    "sorta": "sort of",
    "coulda": "could have",
    "woulda": "would have",
    "shoulda": "should have",
    "musta": "must have",
    # Apostrophe-less variants. Only forms that are not valid standalone English
    # words are included, to avoid corrupting normal text. Deliberately omitted:
    # "its", "were", "well", "ill", "id", "shed", "wed", "lets" (all real words).
    "im": "i am",
    "ive": "i have",
    "youre": "you are",
    "youve": "you have",
    "youll": "you will",
    "hes": "he is",
    "shes": "she is",
    "weve": "we have",
    "theyre": "they are",
    "theyve": "they have",
    "theyll": "they will",
    "thats": "that is",
    "theres": "there is",
    "heres": "here is",
    "whats": "what is",
    "whos": "who is",
    "wheres": "where is",
    "hows": "how is",
    "isnt": "is not",
    "arent": "are not",
    "wasnt": "was not",
    "werent": "were not",
    "havent": "have not",
    "hasnt": "has not",
    "hadnt": "had not",
    "dont": "do not",
    "doesnt": "does not",
    "didnt": "did not",
    "couldnt": "could not",
    "wouldnt": "would not",
    "shouldnt": "should not",
    "cant": "cannot",
    "wont": "will not",
}

# Person substitution (first person <-> second person)
# Used for transforming user input when echoing back
DEFAULT_PERSON: dict[str, str] = {
    "i": "you",
    "me": "you",
    "my": "your",
    "mine": "yours",
    "myself": "yourself",
    "am": "are",
    "was": "were",
    "i'm": "you are",
    "i've": "you have",
    "i'll": "you will",
    "i'd": "you would",
}

# Person2 substitution (second person -> first person)
# Reverse of person substitution
DEFAULT_PERSON2: dict[str, str] = {
    "you": "i",
    "your": "my",
    "yours": "mine",
    "yourself": "myself",
    "you're": "i am",
    "you've": "i have",
    "you'll": "i will",
    "you'd": "i would",
}

# Gender substitution (he <-> she)
DEFAULT_GENDER: dict[str, str] = {
    "he": "she",
    "she": "he",
    "him": "her",
    "her": "him",
    "his": "her",
    "hers": "his",
    "himself": "herself",
    "herself": "himself",
}


def substitution_maps(
    contractions=None,
    person=None,
    person2=None,
    gender=None,
    custom=None,
) -> dict:
    """Build a container dict holding all substitution maps."""
    maps = {
        "contractions": contractions if contractions is not None else DEFAULT_CONTRACTIONS.copy(),
        "person": person if person is not None else DEFAULT_PERSON.copy(),
        "person2": person2 if person2 is not None else DEFAULT_PERSON2.copy(),
        "gender": gender if gender is not None else DEFAULT_GENDER.copy(),
        "custom": custom if custom is not None else {},
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
            replacement = subs[lower_core]
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


def expand_contractions(text: str, contractions=None) -> str:
    """Expand contractions in text.

    Args:
        text: Input text.
        contractions: Optional custom contractions map. Uses defaults if None.

    Returns:
        Text with contractions expanded.
    """
    if contractions is None:
        contractions = DEFAULT_CONTRACTIONS
    expanded = apply_substitutions(text, contractions)
    return expanded


def apply_person(text: str, person_map=None) -> str:
    """Apply person substitution (I/me -> you).

    Args:
        text: Input text.
        person_map: Optional custom person map. Uses defaults if None.

    Returns:
        Text with person substitutions applied.
    """
    if person_map is None:
        person_map = DEFAULT_PERSON
    substituted = apply_substitutions(text, person_map)
    return substituted


def apply_person2(text: str, person2_map=None) -> str:
    """Apply person2 substitution (you -> I/me).

    Args:
        text: Input text.
        person2_map: Optional custom person2 map. Uses defaults if None.

    Returns:
        Text with person2 substitutions applied.
    """
    if person2_map is None:
        person2_map = DEFAULT_PERSON2
    substituted = apply_substitutions(text, person2_map)
    return substituted


def apply_gender(text: str, gender_map=None) -> str:
    """Apply gender substitution (he <-> she).

    Args:
        text: Input text.
        gender_map: Optional custom gender map. Uses defaults if None.

    Returns:
        Text with gender substitutions applied.
    """
    if gender_map is None:
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
