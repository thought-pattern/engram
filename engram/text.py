"""Text processing for ENGRAM."""

from functools import lru_cache
from re import UNICODE as UNICODE, finditer as re_finditer, sub as re_sub
from threading import Lock

from nltk.corpus import wordnet, words
from nltk.metrics.distance import edit_distance
from nltk.stem import PorterStemmer, WordNetLemmatizer
from nltk.tag import pos_tag
from nltk.tokenize import word_tokenize

from engram.constants import (
    CLAUSE_BOUNDARY_TRAILERS,
    CLAUSE_QUESTION_BOUNDARIES,
    CONTENT_POS_TAGS,
    MAX_NAME_TOKENS,
    MIN_SPELL_TOKEN_LENGTH,
    MIN_STEM_TOKEN_LENGTH,
    NAME_LEADING_FILLERS,
    NAME_STOP_MARKERS,
    NOUN_POS_TAGS,
    REFERRING_PRONOUNS,
    SPELL_LONG_TOKEN_LENGTH,
    SUBJECT_PRONOUNS,
)
from engram.lexical import select_lexical_terms
from engram.nltk_data import ensure_resource
from engram.spacy_setup import get_nlp

nltk_reader_lock = Lock()


@lru_cache(maxsize=1)
def initialize_nltk_readers() -> bool:
    """Load shared lazy corpus readers before concurrent request handling."""
    ensure_resource("corpora/words", "words")
    ensure_resource("corpora/wordnet", "wordnet")
    ensure_resource("corpora/omw-1.4", "omw-1.4")
    with nltk_reader_lock:
        words.ensure_loaded()
        wordnet.ensure_loaded()
    return True


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
    result = text.lower()

    # Protect intra-word hyphens while removing other punctuation.
    placeholder = "\x00"
    result = re_sub(r"([a-z0-9])-([a-z0-9])", rf"\1{placeholder}\2", result)
    result = "".join(c for c in result if c.isalnum() or c.isspace() or c == placeholder)
    result = result.replace(placeholder, "-")
    result = re_sub(r"\s+", " ", result)
    trimmed = result.strip()
    return trimmed


def restore_capture_case(captures: list[str], source_text: str) -> list[str]:
    """Restore wildcard capture casing from the caller's original sentence.

    Pattern matching intentionally normalizes input, but template output and
    caller-owned labels should not inherit that lowercase representation. This
    aligns each normalized capture with a contiguous word span in the original
    sentence and returns the original spellings when possible.
    """
    if not captures or not source_text:
        return captures

    source_matches = list(re_finditer(r"[^\W_]+(?:-[^\W_]+)*", source_text, flags=UNICODE))
    source_words = [normalize(match.group(0)) for match in source_matches]
    restored: list[str] = []
    search_start = 0

    for capture in captures:
        capture_words = normalize(capture).split()
        found = -1
        if capture_words:
            last_start = len(source_words) - len(capture_words)
            for index in range(search_start, last_start + 1):
                if source_words[index : index + len(capture_words)] == capture_words:
                    found = index
                    break
        if found < 0:
            restored.append(capture)
            continue

        end = found + len(capture_words)
        restored.append(" ".join(match.group(0) for match in source_matches[found:end]))
        search_start = end

    return restored


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
        result = []
        return result

    tokens = word_tokenize(text)

    if use_pos_filter:
        tagged = pos_tag(tokens)
        tokens = [word for word, tag in tagged if tag in CONTENT_POS_TAGS]

    result = select_lexical_terms(tokens, stopwords)
    return result


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
        result = []
        return result

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
def ensure_tagger() -> None:
    """Check the locally provisioned POS tagger data used after startup preflight."""

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
        result = []
        return result

    ensure_tagger()
    tokens = word_tokenize(text)
    tagged = pos_tag(tokens)

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

    Expansion exists to resolve pronouns, so it only fires when the query
    carries a referring pronoun ("its", "they", "that", ...). A query without
    one is self-contained; appending the previous response's nouns to it would
    dilute its own keywords and let the bot's last answer distort unrelated
    retrieval.

    When it fires, referring pronouns are removed and the previous response's
    nouns and proper nouns are appended. Removing the unresolved pronoun makes
    the resulting cache key context-specific instead of teaching a globally
    reusable ambiguous query.

    Args:
        query: Current query text.
        previous_response: Previous response from session.

    Returns:
        Expanded query text.

    Example:
        >>> expand_query("What is its population?", "Paris is the capital of France")
        'What is population? Paris capital France'
    """
    if not previous_response:
        return query

    query_words = {word.strip(".,!?;:'\"").lower() for word in query.split()}
    if not query_words & REFERRING_PRONOUNS:
        return query

    resolved_tokens = [token for token in query.split() if token.strip(".,!?;:'\"").lower() not in REFERRING_PRONOUNS]
    resolved_query = " ".join(resolved_tokens).strip() or query

    terms = extract_context_terms(previous_response)
    if not terms:
        expanded = f"{resolved_query} {previous_response}"
        return expanded
    expanded = f"{resolved_query} {' '.join(terms)}"
    return expanded


@lru_cache(maxsize=1)
def get_stemmer() -> PorterStemmer:
    """Get or create the module-level Porter stemmer."""
    stemmer = PorterStemmer()
    return stemmer


@lru_cache(maxsize=1)
def get_lemmatizer() -> WordNetLemmatizer:
    """Get or create the module-level WordNet lemmatizer."""

    initialize_nltk_readers()
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

    Tokens shorter than MIN_STEM_TOKEN_LENGTH are lowercased but left
    unstemmed: they are already near their root, and Porter mangles them into
    false matches -- "his" stems to "hi", turning a possessive into a greeting
    in the stemmed matcher fallback.

    Args:
        text: Input text.

    Returns:
        Text with all words stemmed.
    """
    stemmer = get_stemmer()
    out = []
    for word in text.split():
        if len(word) < MIN_STEM_TOKEN_LENGTH:
            out.append(word.lower())
        else:
            out.append(stemmer.stem(word))
    stemmed = " ".join(out)
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
def known_words() -> set:
    """English word set used to gate spelling correction.

    A token found here is a real word and is never "corrected" -- unknown real
    words are the lemma/stem fallbacks' job, not the spell corrector's.
    """
    initialize_nltk_readers()
    word_set = {w.lower() for w in words.words()}
    return word_set


def is_known_word(word: str) -> bool:
    """True when the word or its lemma is in the English word list.

    The words corpus carries base forms but few inflections ("work" but not
    "tests" or "died"), so a bare membership test reads real inflected words
    as typos. Checking the verb and noun lemmas closes that gap.
    """
    lowered = word.lower()
    if lowered in known_words():
        result = True
        return result
    if lemmatize_word(lowered, "v") in known_words():
        result = True
        return result
    known = lemmatize_word(lowered, "n") in known_words()
    return known


def correct_token(token: str, vocabulary: set) -> str:
    """Correct one out-of-vocabulary token toward the vocabulary, or keep it.

    Conservative by design: short tokens, vocabulary tokens, and real English
    words (including inflections, via is_known_word) are kept as-is, and a
    replacement happens only when exactly one vocabulary word is nearest
    within the allowed Damerau-Levenshtein distance (a transposition like
    "abotu" -> "about" counts as one edit).
    """
    if len(token) < MIN_SPELL_TOKEN_LENGTH:
        return token
    if token in vocabulary:
        return token
    if is_known_word(token):
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

    corrected = [correct_token(token, vocabulary) for token in text.split()]
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
    lowercase "i".

    A pronoun directly after a noun is a relative clause modifying that noun
    ("a friend you can trust", "the movie i saw") -- those stay intact rather
    than being cut mid-phrase. Text without a boundary is returned unchanged.

    Args:
        text: Input text (typically a wildcard capture).

    Returns:
        The first clause of the text.
    """
    if not text or not text.strip():
        return text

    ensure_tagger()
    tokens = word_tokenize(text)
    tagged = pos_tag(tokens)

    for i in range(1, len(tagged) - 1):
        word, _ = tagged[i]
        prev_tag = tagged[i - 1][1]
        next_tag = tagged[i + 1][1]
        lowered = word.lower()
        verb_follows = next_tag.startswith("VB") or next_tag == "MD"
        if not verb_follows:
            continue
        # A subject pronoun opens a new clause -- unless it trails a noun,
        # which reads as a relative clause ("a friend you can trust"). A
        # non-relative question word opens one even after a noun ("the sky
        # what is the moon").
        pronoun_boundary = lowered in SUBJECT_PRONOUNS and not prev_tag.startswith("NN")
        question_boundary = lowered in CLAUSE_QUESTION_BOUNDARIES
        if pronoun_boundary or question_boundary:
            clause_tokens = tokens[:i]
            while clause_tokens and clause_tokens[-1].lower() in CLAUSE_BOUNDARY_TRAILERS:
                clause_tokens.pop()
            clause = " ".join(clause_tokens).strip(" ,;")
            if clause:
                return clause
            return text
    return text


def extract_name(text: str) -> str:
    """Extract the person name from a self-introduction capture.

    "still jason by the way" -> "jason"; "mary jane" -> "mary jane". This is a
    heuristic, not NER (captures arrive lowercased, which starves NER of its
    casing signal): leading filler adverbs are skipped, then name tokens are
    collected until a stop marker, a non-word token, or the length cap.
    Returns the original text when nothing name-like is found, so the caller
    degrades to today's behavior rather than storing an empty name.

    Args:
        text: The captured introduction span.

    Returns:
        The extracted name span, or the input text unchanged.
    """
    if not text or not text.strip():
        return text

    tokens = text.split()
    index = 0
    while index < len(tokens) and tokens[index].lower() in NAME_LEADING_FILLERS:
        index += 1

    name_tokens: list[str] = []
    while index < len(tokens) and len(name_tokens) < MAX_NAME_TOKENS:
        token = tokens[index].strip(".,!?;:'\"")
        if not token.isalpha() or token.lower() in NAME_STOP_MARKERS:
            break
        name_tokens.append(token)
        index += 1

    if not name_tokens:
        return text
    name = " ".join(name_tokens)
    return name


@lru_cache(maxsize=1)
def ensure_wordnet() -> None:
    """Ensure WordNet data is available, fetching into the local data dir."""

    initialize_nltk_readers()


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

    synonyms = {word.lower()}
    # NLTK's shared reader opens and closes its zipped corpus around each read;
    # concurrent access can trip its internal file-handle assertion.
    with nltk_reader_lock:
        ensure_wordnet()
        wordnet_reader = wordnet
        for syn in wordnet_reader.synsets(word):
            for lemma in syn.lemmas():
                name = lemma.name().lower().replace("_", " ")
                if name != word.lower():
                    synonyms.add(name)
                    if len(synonyms) >= max_synonyms + 1:
                        capped = tuple(synonyms)
                        return capped

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
