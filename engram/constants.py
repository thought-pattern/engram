"""Shared constants and enumerations for ENGRAM.

This module is the single home for the package's enums and literal data
constants (stopword lists, substitution maps, POS-tag sets, scoring weights, and
so on). It depends only on the standard library, so it sits at the root of the
import graph: every other ENGRAM module may import from it without risk of a
cycle.
"""

import os
from enum import Enum

# =============================================================================
# Package metadata
# =============================================================================

# Canonical package version. pyproject.toml derives the distribution version
# from this via setuptools' dynamic ``attr``, so the version lives in exactly one
# place, and core.py exposes it as the bot's ``version`` property.
VERSION = "1.1.11"
DEFAULT_USER_ID = "0"

# =============================================================================
# Enumerations
# =============================================================================


class SessionOverflow(Enum):
    """Behavior when session limit is reached."""

    REJECT = "reject"
    EXPIRE_OLDEST = "expire_oldest"
    LRU = "lru"


class EvictionPolicy(Enum):
    """Policy for evicting DYNAMIC categories when at capacity."""

    FIFO = "fifo"  # First-in, first-out (oldest evicted first)
    LRU = "lru"  # Least recently used (oldest last-hit evicted)
    LFU = "lfu"  # Least frequently used (lowest hit count evicted)
    HIT_RATE = "hit_rate"  # Lowest hit rate (hits/queries) evicted


class Tier(Enum):
    """Statement tier classification."""

    STATIC = "STATIC"
    DYNAMIC = "DYNAMIC"


# =============================================================================
# NLTK data
# =============================================================================

# Local data directory: <repo root>/data/nltk_data
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(_THIS_DIR)
NLTK_DATA_DIR = os.path.join(_REPO_ROOT, "data", "nltk_data")

# Required packages as (find_path, download_name) pairs. find_path is what
# nltk.data.find expects; download_name is what nltk.download expects.
REQUIRED_PACKAGES = (
    ("tokenizers/punkt", "punkt"),
    ("tokenizers/punkt_tab", "punkt_tab"),
    ("taggers/averaged_perceptron_tagger", "averaged_perceptron_tagger"),
    ("taggers/averaged_perceptron_tagger_eng", "averaged_perceptron_tagger_eng"),
    ("chunkers/maxent_ne_chunker", "maxent_ne_chunker"),
    ("chunkers/maxent_ne_chunker_tab", "maxent_ne_chunker_tab"),
    ("corpora/words", "words"),
    ("corpora/wordnet", "wordnet"),
    ("corpora/omw-1.4", "omw-1.4"),
    ("sentiment/vader_lexicon", "vader_lexicon"),
)

# Stopwords filtered out during keyword extraction. A set for O(1) membership.
DEFAULT_STOPWORDS: set[str] = {
    "a",
    "an",
    "the",
    "is",
    "are",
    "was",
    "were",
    "be",
    "been",
    "being",
    "have",
    "has",
    "had",
    "do",
    "does",
    "did",
    "will",
    "would",
    "could",
    "should",
    "may",
    "might",
    "must",
    "shall",
    "can",
    "need",
    "dare",
    "ought",
    "used",
    "to",
    "of",
    "in",
    "for",
    "on",
    "with",
    "at",
    "by",
    "from",
    "as",
    "into",
    "through",
    "during",
    "before",
    "after",
    "above",
    "below",
    "between",
    "under",
    "again",
    "further",
    "then",
    "once",
    "here",
    "there",
    "when",
    "where",
    "why",
    "how",
    "all",
    "each",
    "few",
    "more",
    "most",
    "other",
    "some",
    "such",
    "no",
    "nor",
    "not",
    "only",
    "own",
    "same",
    "so",
    "than",
    "too",
    "very",
    "just",
    "also",
}


# =============================================================================
# spaCy
# =============================================================================

MODEL_NAME = "en_core_web_sm"


# =============================================================================
# Text processing
# =============================================================================

# POS tags for nouns and proper nouns (the referents a follow-up query's
# pronouns can point back to; used for session context expansion)
NOUN_POS_TAGS = {"NN", "NNS", "NNP", "NNPS"}

# Spelling correction (input cleanup). Correction is deliberately timid: only
# tokens at least MIN_SPELL_TOKEN_LENGTH long that are neither in the target
# vocabulary nor real English words are candidates, and only a unique nearest
# neighbor within the allowed Damerau-Levenshtein distance replaces them.
MIN_SPELL_TOKEN_LENGTH = 4
SPELL_LONG_TOKEN_LENGTH = 6  # Tokens this long or longer allow distance 2 (else 1)

# Conjunctions stripped from the end of a clause cut ("tired and" -> "tired")
CLAUSE_BOUNDARY_TRAILERS = {"and", "but", "or", "because", "so", "then"}

# Question words that also open a new clause mid-capture when followed by a
# verb ("the sky | what is the moon"). Relative pronouns (who / which / whose)
# are excluded: "the man who is tall" is one phrase, not two clauses.
CLAUSE_QUESTION_BOUNDARIES = {"what", "where", "when", "why", "how"}

# Name extraction from self-introduction captures ("still jason by the way").
# Leading fillers are skipped; a stop marker ends the name span.
NAME_LEADING_FILLERS = {"still", "actually", "really", "just", "now", "officially", "basically", "technically"}
NAME_STOP_MARKERS = {"by", "the", "way", "though", "btw", "anyway", "and", "but", "because", "for", "if", "these", "days"}
MAX_NAME_TOKENS = 3

# Personal subject pronouns that mark the start of a new clause when followed
# by a verb. Matched by word, not POS tag: NLTK tags a lowercase "i" as a
# noun or adjective, never PRP.
SUBJECT_PRONOUNS = {"i", "you", "he", "she", "it", "we", "they"}

# Tokens shorter than this are never Porter-stemmed: they are already near
# their root, and stemming mangles them into false matches ("his" -> "hi"
# would greet a possessive).
MIN_STEM_TOKEN_LENGTH = 4

# Acknowledgments rotated when a fact is learned from conversation, so a
# teaching session does not answer with the same phrase every turn.
LEARNED_ACKNOWLEDGMENTS = (
    "I see.",
    "Noted.",
    "Got it - I'll remember that.",
    "Understood.",
    "Okay, I'll keep that in mind.",
)

# Conversational escape used when a catch-all would repeat a recent prompt or
# the caller explicitly points out that the bot is looping.
REPETITION_ESCAPE_RESPONSE = "You're right - I was repeating myself. Let's take a different approach."
REPETITION_FEEDBACK_MARKERS = (
    "same question",
    "you are repeating",
    "youre repeating",
    "you keep repeating",
    "repeat yourself",
    "already explained",
    "just explained",
    "already answered",
    "asked that already",
)
RESPONSE_SIMILARITY_THRESHOLD = 0.72
REPETITION_HISTORY_SIZE = 8

# Responses when a stated fact matches what is already stored ({existing} is
# replaced with the stored statement text).
KNOWN_FACT_RESPONSES = (
    "Yes - {existing}",
    "Right, that matches what I have: {existing}",
)

# Responses when a stated fact contradicts what is already stored. The stored
# belief is protected (no overwrite), but silence would read as agreement, so
# the conflict is surfaced.
CONFLICTING_FACT_RESPONSES = (
    "Hmm, I have it differently: {existing}",
    "That differs from what I know: {existing}",
)

# Output polish: the pronoun I and its contractions are always capitalized
STANDALONE_I_FORMS = {"i": "I", "i'm": "I'm", "i've": "I've", "i'll": "I'll", "i'd": "I'd"}

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


# =============================================================================
# NLP fact extraction
# =============================================================================

# Copula verbs that indicate definitional statements
COPULAS = {"is", "are", "was", "were"}

# Words that indicate a question (should not extract facts)
QUESTION_WORDS = {"what", "who", "where", "when", "why", "how", "which", "whose"}

# Input kinds: the intent classification templates branch on via {qtype:...}
# and the pipeline routes on (questions get retrieval before a shrug).
KIND_QUESTION = "question"
KIND_COMMAND = "command"
KIND_STATEMENT = "statement"

# Words that indicate a command (should not extract facts)
COMMAND_WORDS = {"learn", "remember", "forget", "tell", "say", "repeat", "echo"}

# Personal pronouns excluded as relational-triple subjects or objects
PRONOUNS = {"i", "you", "he", "she", "it", "we", "they", "this", "that", "these", "those"}

# Pronouns that refer back to earlier conversation ("what is ITS population").
# Session context expansion only fires when the query carries one -- expanding
# every query would flood unrelated follow-ups with the previous response's
# nouns.
REFERRING_PRONOUNS = {
    "it",
    "its",
    "they",
    "them",
    "their",
    "theirs",
    "he",
    "she",
    "him",
    "her",
    "his",
    "hers",
    "this",
    "that",
    "these",
    "those",
}

# Guardrails for copula fact extraction: a subject longer than this, one that
# contains a verb or modal, or one led by a possessive pronoun is conversation
# about something, not a definitional statement worth learning.
MAX_FACT_SUBJECT_TOKENS = 4
POSSESSIVE_PRONOUNS = {"my", "your", "our", "their", "his", "her", "its"}

# spaCy dependency labels marking subjects and objects
SUBJECT_DEPS = {"nsubj", "nsubjpass"}
OBJECT_DEPS = {"dobj", "attr", "acomp", "oprd", "dative"}

# Articles dropped from the front of an extracted span
ARTICLES = {"a", "an", "the"}


# =============================================================================
# Sentiment (VADER)
# =============================================================================

# VADER compound-score thresholds (the standard cutoffs from the VADER paper).
POSITIVE_THRESHOLD = 0.05
NEGATIVE_THRESHOLD = -0.05

NEUTRAL = "neutral"
POSITIVE = "positive"
NEGATIVE = "negative"

NEUTRAL_SCORES = {"compound": 0.0, "pos": 0.0, "neu": 1.0, "neg": 0.0}


# =============================================================================
# Substitution maps
# =============================================================================

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

# Gender substitution: gendered pronouns map to singular they/them, so
# apply_gender rewrites text into gender-neutral pronouns rather than swapping
# he <-> she. Object "her" and possessive "her" both map to "them" (the
# possessive case is the less common one and a perfect split is not possible
# with a flat word map).
DEFAULT_GENDER: dict[str, str] = {
    "he": "they",
    "she": "they",
    "him": "them",
    "her": "them",
    "his": "their",
    "hers": "theirs",
    "himself": "themself",
    "herself": "themself",
}


# =============================================================================
# Scoring
# =============================================================================

# Overlap credit for a query keyword matched only through a WordNet synonym,
# relative to the 1.0 credit of an exact keyword match.
SYNONYM_OVERLAP_WEIGHT = 0.5


# =============================================================================
# Pattern matching
# =============================================================================

TOPIC_PRIORITY = 1000  # Having topic match adds significant priority
THAT_PRIORITY = 500  # Having that match adds priority

WILDCARD_TOKENS = {"*", "_", "#", "^"}


# =============================================================================
# Persistence
# =============================================================================

# Version constant for persistence format
PERSISTENCE_VERSION = 1
