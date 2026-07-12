"""Text processing for ENGRAM."""

import re
from functools import lru_cache

from nltk.corpus import wordnet, words
from nltk.metrics.distance import edit_distance
from nltk.stem import PorterStemmer, WordNetLemmatizer
from nltk.tag import pos_tag
from nltk.tokenize import word_tokenize

from engram.constants import (
    CLAUSE_BOUNDARY_TRAILERS,
    CONTENT_POS_TAGS,
    MIN_SPELL_TOKEN_LENGTH,
    NOUN_POS_TAGS,
    SPELL_LONG_TOKEN_LENGTH,
    SUBJECT_PRONOUNS,
)
from engram.nltk_data import ensure_resource
from engram.spacy_setup import get_nlp


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
    trimmed = result.strip()
    return trimmed


def extract_keywords(
    text: str,
    stopwords: set[str],
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


def extract_keywords_spacy(text: str, stopwords: set[str]) -> list:
    """Extract keywords using spaCy, keeping noun-chunk phrases as units.

    Returns content-word lemmas plus multi-word noun-chunk phrases (e.g.
    "machine learning"), so the keyword index can match precise compound terms
    while still indexing their component words for recall. Falls back to
    extract_keywords if the spaCy model is unavailable.

    Args:
        text: Input text string.
        stopwords: Set of stopwords to filter out.

    Returns:
        List of keywords (lemmatized single words and noun-chunk phrases),
        deduplicated with order preserved.
    """
    if not text or not text.strip():
        return []

    nlp = get_nlp()
    if not nlp:
        fallback = extract_keywords(text, stopwords)
        return fallback

    doc = nlp(text)
    seen: set[str] = set()
    keywords: list = []

    def add(word: str) -> None:
        if word and word not in stopwords and word not in seen:
            seen.add(word)
            keywords.append(word)

    # Single content-word lemmas (nouns, verbs, adjectives, adverbs).
    for token in doc:
        if token.is_alpha and not token.is_stop and token.pos_ in ("NOUN", "PROPN", "VERB", "ADJ", "ADV"):
            add(token.lemma_.lower())

    # Multi-word noun-chunk phrases, with stopwords/determiners dropped.
    for chunk in doc.noun_chunks:
        words = [t.lemma_.lower() for t in chunk if t.is_alpha and not t.is_stop]
        if len(words) >= 2:
            add(" ".join(words))

    return keywords


@lru_cache(maxsize=1)
def _ensure_tagger() -> None:
    """Ensure the POS tagger data is available, fetching into the local data dir."""

    ensure_resource("taggers/averaged_perceptron_tagger", "averaged_perceptron_tagger")
    ensure_resource("taggers/averaged_perceptron_tagger_eng", "averaged_perceptron_tagger_eng")


def extract_context_terms(text: str, max_terms: int = 8) -> list[str]:
    """Extract the nouns and proper nouns from text, order preserved.

    These are the referents a follow-up query's pronouns can point back to
    ("its" -> Paris / France), which is what session context expansion needs --
    appending every word of the previous response would flood the query with
    noise keywords instead.

    Args:
        text: Input text (typically the previous response).
        max_terms: Maximum terms to return.

    Returns:
        Deduplicated nouns/proper nouns in order of appearance, [] if tagging
        is unavailable or nothing qualifies.
    """
    if not text or not text.strip():
        return []

    _ensure_tagger()
    try:
        tokens = word_tokenize(text)
        tagged = pos_tag(tokens)
    except Exception:
        return []

    seen: set[str] = set()
    terms: list[str] = []
    for word, tag in tagged:
        if tag not in NOUN_POS_TAGS or not word.isalnum():
            continue
        lower = word.lower()
        if lower in seen:
            continue
        seen.add(lower)
        terms.append(word)
        if len(terms) >= max_terms:
            break
    return terms


def expand_query(query: str, previous_response: str) -> str:
    """Expand query with referents from the previous response.

    Appends the previous response's nouns and proper nouns -- the things a
    follow-up's pronouns can refer to -- rather than the full response text,
    so expansion adds referents without drowning the query's own keywords.
    Falls back to appending the full response when no nouns can be extracted.

    Args:
        query: Current query text.
        previous_response: Previous response from session.

    Returns:
        Expanded query text.

    Example:
        >>> expand_query("What is its population?", "Paris is the capital of France")
        'What is its population? Paris capital France'
    """
    if not previous_response:
        return query
    terms = extract_context_terms(previous_response)
    if not terms:
        expanded = f"{query} {previous_response}"
        return expanded
    expanded = f"{query} {' '.join(terms)}"
    return expanded


@lru_cache(maxsize=1)
def get_stemmer() -> PorterStemmer:
    """Get or create the module-level Porter stemmer."""
    stemmer = PorterStemmer()
    return stemmer


@lru_cache(maxsize=1)
def get_lemmatizer() -> WordNetLemmatizer:
    """Get or create the module-level WordNet lemmatizer."""

    ensure_resource("corpora/wordnet", "wordnet")
    lemmatizer = WordNetLemmatizer()
    return lemmatizer


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
    stemmed = get_stemmer().stem(word.lower())
    return stemmed


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
    lemmatized = get_lemmatizer().lemmatize(word.lower(), pos=pos)
    return lemmatized


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
    stemmed = " ".join(stemmer.stem(w) for w in words)
    return stemmed


@lru_cache(maxsize=4096)
def lemmatize_text(text: str) -> str:
    """Apply WordNet lemmatization to all words in text.

    Each word is lemmatized as a verb first, then as a noun if unchanged, so
    both "running" -> "run" and "cats" -> "cat" normalize. Unlike Porter
    stemming, lemmatization preserves real dictionary forms, so it is the more
    precise normalization for matching.

    Args:
        text: Input text.

    Returns:
        Text with all words lemmatized.
    """
    lemmatizer = get_lemmatizer()
    out = []
    for word in text.split():
        lower = word.lower()
        lemma = lemmatizer.lemmatize(lower, pos="v")
        if lemma == lower:
            lemma = lemmatizer.lemmatize(lower, pos="n")
        out.append(lemma)
    lemmatized = " ".join(out)
    return lemmatized


@lru_cache(maxsize=4096)
def lemmatize_text_spacy(text: str) -> str:
    """Lemmatize text using spaCy's context-aware lemmatizer.

    More accurate than lemmatize_text's WordNet heuristic because spaCy uses POS
    context from the parse: "saw" the verb lemmatizes to "see" while "saw" the
    noun stays "saw". Falls back to the lowercased input if the model is
    unavailable.

    Args:
        text: Input text.

    Returns:
        Text with all words lemmatized.
    """

    nlp = get_nlp()
    if not nlp:
        lowered = text.lower()
        return lowered
    doc = nlp(text)
    lemmatized = " ".join(token.lemma_.lower() for token in doc)
    return lemmatized


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
    stemmed = stem_text(normalized)
    return stemmed


@lru_cache(maxsize=1)
def _known_words() -> set:
    """English word set used to gate spelling correction.

    A token found here is a real word and is never "corrected" -- unknown real
    words are the lemma/stem fallbacks' job, not the spell corrector's.
    """
    ensure_resource("corpora/words", "words")
    word_set = {w.lower() for w in words.words()}
    return word_set


def _correct_token(token: str, vocabulary: set) -> str:
    """Correct one out-of-vocabulary token toward the vocabulary, or keep it.

    Conservative by design: short tokens, vocabulary tokens, and real English
    words are kept as-is, and a replacement happens only when exactly one
    vocabulary word is nearest within the allowed Damerau-Levenshtein distance
    (a transposition like "abotu" -> "about" counts as one edit).
    """
    if len(token) < MIN_SPELL_TOKEN_LENGTH:
        return token
    if token in vocabulary:
        return token
    if token in _known_words():
        return token

    max_distance = 2 if len(token) >= SPELL_LONG_TOKEN_LENGTH else 1

    best_distance = max_distance + 1
    best_candidates: list[str] = []
    for candidate in vocabulary:
        if abs(len(candidate) - len(token)) > max_distance:
            continue
        distance = edit_distance(token, candidate, transpositions=True)
        if distance < best_distance:
            best_distance = distance
            best_candidates = [candidate]
        elif distance == best_distance:
            best_candidates.append(candidate)

    if best_distance <= max_distance and len(best_candidates) == 1:
        corrected = best_candidates[0]
        return corrected
    return token


def correct_spelling(text: str, vocabulary) -> str:
    """Correct out-of-vocabulary typos in text toward a target vocabulary.

    ENGRAM does not need general English spelling correction -- it needs
    queries to hit the store. The vocabulary is therefore the store's own
    indexed terms, so a typo is only ever corrected into a word that can
    actually match something ("abotu" -> "about" when a pattern carries
    "about"). See _correct_token for the guardrails.

    Args:
        text: Normalized (lowercase) input text.
        vocabulary: Set of vocabulary words to correct toward.

    Returns:
        Text with unambiguous typos corrected, otherwise unchanged.
    """
    if not text or not vocabulary:
        return text

    corrected = [_correct_token(token, vocabulary) for token in text.split()]
    result = " ".join(corrected)
    return result


def first_clause(text: str) -> str:
    """Truncate text at the start of a new subject-verb clause.

    A wildcard capture often swallows a whole compound sentence; echoing it
    back verbatim reads badly ("you're tired i have been working really
    hard"). A personal subject pronoun followed by a verb or modal after the
    first word marks a new clause -- everything from there on is dropped,
    along with any dangling conjunction ("tired and" -> "tired"). The pronoun
    is matched by word rather than POS tag, since the tagger misreads a
    lowercase "i". Text without such a boundary is returned unchanged.

    Args:
        text: Input text (typically a wildcard capture).

    Returns:
        The first clause of the text.
    """
    if not text or not text.strip():
        return text

    _ensure_tagger()
    try:
        tokens = word_tokenize(text)
        tagged = pos_tag(tokens)
    except Exception:
        return text

    for i in range(1, len(tagged) - 1):
        word, _ = tagged[i]
        next_tag = tagged[i + 1][1]
        if word.lower() in SUBJECT_PRONOUNS and (next_tag.startswith("VB") or next_tag == "MD"):
            clause_tokens = tokens[:i]
            while clause_tokens and clause_tokens[-1].lower() in CLAUSE_BOUNDARY_TRAILERS:
                clause_tokens.pop()
            clause = " ".join(clause_tokens).strip(" ,;")
            if clause:
                return clause
            return text
    return text


@lru_cache(maxsize=1)
def _ensure_wordnet() -> None:
    """Ensure WordNet data is available, fetching into the local data dir."""

    ensure_resource("corpora/wordnet", "wordnet")
    ensure_resource("corpora/omw-1.4", "omw-1.4")


@lru_cache(maxsize=4096)
def get_synonyms(word: str, max_synonyms: int = 5) -> tuple[str, ...]:
    """Get synonyms for a word using WordNet.

    Args:
        word: Input word.
        max_synonyms: Maximum number of synonyms to return.

    Returns:
        Tuple of synonyms (includes the original word). A tuple rather than a set
        because the result is cached and shared, so an immutable return keeps a
        caller from mutating the cached value.
    """

    _ensure_wordnet()

    synonyms = {word.lower()}
    try:
        for syn in wordnet.synsets(word):
            for lemma in syn.lemmas():
                name = lemma.name().lower().replace("_", " ")
                if name != word.lower():
                    synonyms.add(name)
                    if len(synonyms) >= max_synonyms + 1:
                        capped = tuple(synonyms)
                        return capped
    except Exception:
        pass

    result = tuple(synonyms)
    return result


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
