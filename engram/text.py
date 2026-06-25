"""Text processing for ENGRAM."""

import re
from functools import lru_cache

from nltk.tokenize import word_tokenize
from nltk.tag import pos_tag
from nltk.stem import WordNetLemmatizer, PorterStemmer

# POS tags that indicate content words (nouns, verbs, adjectives, adverbs)
CONTENT_POS_TAGS = {
    "NN",
    "NNS",
    "NNP",
    "NNPS",  # Nouns
    "VB",
    "VBD",
    "VBG",
    "VBN",
    "VBP",
    "VBZ",  # Verbs
    "JJ",
    "JJR",
    "JJS",  # Adjectives
    "RB",
    "RBR",
    "RBS",  # Adverbs
}


@lru_cache(maxsize=4096)
def normalize(text: str) -> str:
    """Normalize text for consistent processing.

    Steps:
    1. Convert to lowercase
    2. Remove punctuation (preserve intra-word hyphens)
    3. Collapse whitespace to single spaces
    4. Trim leading and trailing whitespace

    Args:
        text: Raw text string.

    Returns:
        Normalized text string.

    Example:
        >>> normalize("What's the S&P 500 price?")
        'whats the sp 500 price'
    """
    # Convert to lowercase
    result = text.lower()

    # Remove punctuation except intra-word hyphens
    # First, protect intra-word hyphens by replacing word-hyphen-word with placeholder
    placeholder = "\x00"
    result = re.sub(r"([a-z0-9])-([a-z0-9])", rf"\1{placeholder}\2", result)

    # Remove all non-alphanumeric except spaces and placeholder
    result = "".join(c for c in result if c.isalnum() or c.isspace() or c == placeholder)

    # Restore protected hyphens
    result = result.replace(placeholder, "-")

    # Collapse whitespace to single spaces
    result = re.sub(r"\s+", " ", result)

    # Trim
    return result.strip()


def extract_keywords(
    text: str,
    stopwords: frozenset[str],
    use_pos_filter: bool = False,
) -> list[str]:
    """Extract keywords from text using NLTK tokenization.

    Steps:
    1. Tokenize using NLTK word_tokenize
    2. Normalize to lowercase
    3. Remove stopwords and non-alphanumeric tokens
    4. Optionally filter by POS tags (nouns, verbs, adjectives)
    5. Return remaining terms (deduplicated, order preserved)

    Args:
        text: Input text string.
        stopwords: Set of stopwords to filter out.
        use_pos_filter: If True, only keep content words (nouns, verbs, etc.)

    Returns:
        List of keywords.

    Example:
        >>> extract_keywords("What's the capital of France?", DEFAULT_STOPWORDS)
        ['whats', 'capital', 'france']
    """
    if not text or not text.strip():
        return []

    # Tokenize using NLTK
    try:
        tokens = word_tokenize(text)
    except Exception:
        # Fallback to simple split if NLTK fails
        tokens = text.split()

    # Optional POS filtering
    if use_pos_filter:
        try:
            tagged = pos_tag(tokens)
            tokens = [word for word, tag in tagged if tag in CONTENT_POS_TAGS]
        except Exception:
            pass  # Fall through to regular filtering

    seen: set[str] = set()
    keywords: list[str] = []

    for token in tokens:
        # Normalize to lowercase
        word = token.lower()

        # Skip if not alphanumeric, is stopword, or already seen
        if not word.isalnum():
            continue
        if word in stopwords:
            continue
        if word in seen:
            continue

        keywords.append(word)
        seen.add(word)

    return keywords


def expand_query(query: str, previous_response: str) -> str:
    """Expand query with previous response context.

    Args:
        query: Current query text.
        previous_response: Previous response from session.

    Returns:
        Expanded query text.

    Example:
        >>> expand_query("What is its population?", "Paris is the capital of France")
        'What is its population? Paris is the capital of France'
    """
    if not previous_response:
        return query
    return f"{query} {previous_response}"


@lru_cache(maxsize=1)
def get_stemmer() -> PorterStemmer:
    """Get or create the module-level Porter stemmer."""
    return PorterStemmer()


@lru_cache(maxsize=1)
def get_lemmatizer() -> WordNetLemmatizer:
    """Get or create the module-level WordNet lemmatizer."""
    import nltk

    # Ensure wordnet data is available
    try:
        nltk.data.find("corpora/wordnet")
    except LookupError:
        nltk.download("wordnet", quiet=True)
    return WordNetLemmatizer()


@lru_cache(maxsize=8192)
def stem_word(word: str) -> str:
    """Apply Porter stemming to a word.

    Stemming reduces words to their root form by removing suffixes.
    Example: "running" -> "run", "cats" -> "cat"

    Args:
        word: Input word.

    Returns:
        Stemmed word.
    """
    return get_stemmer().stem(word.lower())


@lru_cache(maxsize=8192)
def lemmatize_word(word: str, pos: str = "n") -> str:
    """Apply WordNet lemmatization to a word.

    Lemmatization reduces words to their dictionary form.
    More accurate than stemming but requires POS information.

    Args:
        word: Input word.
        pos: Part of speech ('n' for noun, 'v' for verb, 'a' for adjective, 'r' for adverb).

    Returns:
        Lemmatized word.
    """
    return get_lemmatizer().lemmatize(word.lower(), pos=pos)


@lru_cache(maxsize=4096)
def stem_text(text: str) -> str:
    """Apply Porter stemming to all words in text.

    Args:
        text: Input text.

    Returns:
        Text with all words stemmed.
    """
    words = text.split()
    stemmer = get_stemmer()
    return " ".join(stemmer.stem(w) for w in words)


@lru_cache(maxsize=4096)
def normalize_with_stemming(text: str) -> str:
    """Normalize text and apply stemming for flexible matching.

    Combines standard normalization with stemming to allow
    matching of different word forms (e.g., "running" matches "run").

    Args:
        text: Input text.

    Returns:
        Normalized and stemmed text.
    """
    normalized = normalize(text)
    return stem_text(normalized)


@lru_cache(maxsize=1)
def _ensure_wordnet() -> None:
    """Ensure WordNet data is available."""
    import nltk

    try:
        nltk.data.find("corpora/wordnet")
    except LookupError:
        nltk.download("wordnet", quiet=True)
    try:
        nltk.data.find("corpora/omw-1.4")
    except LookupError:
        nltk.download("omw-1.4", quiet=True)


@lru_cache(maxsize=4096)
def get_synonyms(word: str, max_synonyms: int = 5) -> frozenset[str]:
    """Get synonyms for a word using WordNet.

    Args:
        word: Input word.
        max_synonyms: Maximum number of synonyms to return.

    Returns:
        Frozenset of synonyms (includes the original word).
    """
    from nltk.corpus import wordnet

    _ensure_wordnet()

    synonyms = {word.lower()}
    try:
        for syn in wordnet.synsets(word):
            for lemma in syn.lemmas():
                name = lemma.name().lower().replace("_", " ")
                if name != word.lower():
                    synonyms.add(name)
                    if len(synonyms) >= max_synonyms + 1:
                        return frozenset(synonyms)
    except Exception:
        pass

    return frozenset(synonyms)


def expand_with_synonyms(
    keywords: list[str],
    max_synonyms_per_word: int = 3,
) -> list[str]:
    """Expand a list of keywords with their synonyms.

    Args:
        keywords: List of keywords to expand.
        max_synonyms_per_word: Maximum synonyms to add per keyword.

    Returns:
        Expanded list with original keywords first, then synonyms.
    """
    expanded = list(keywords)
    seen = set(keywords)

    for word in keywords:
        synonyms = get_synonyms(word, max_synonyms=max_synonyms_per_word)
        for syn in synonyms:
            if syn not in seen:
                expanded.append(syn)
                seen.add(syn)

    return expanded
