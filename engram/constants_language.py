"""NLTK and spaCy setup, fact phrasing, text normalization, fact extraction, sentiment, and pattern syntax."""

from pathlib import Path
from string import Formatter
from tomllib import loads as toml_loads

from engram.constants import FRAME_OVERRIDES_PATH

VOWELS = set("aeiou")
MAX_FRAME_RESOURCE_BYTES = 65536
MAX_FRAME_OVERRIDES = 256
MAX_FRAME_TEMPLATE_CHARS = 1024


def read_frame_overrides(path: Path) -> dict:
    """Validate the packaged presentation contract before publishing native frames."""
    with path.open("rb") as source:
        payload = source.read(MAX_FRAME_RESOURCE_BYTES + 1)
    if len(payload) > MAX_FRAME_RESOURCE_BYTES:
        raise ValueError("Fact-phrasing resource exceeds its byte limit")
    data = toml_loads(payload.decode("utf-8"))
    if set(data) != {"frames"}:
        raise ValueError("Fact-phrasing resource must contain only frames")
    frames = data.get("frames", {})
    if not isinstance(frames, dict) or not 1 <= len(frames) <= MAX_FRAME_OVERRIDES:
        raise ValueError("Fact-phrasing frames must be a nonempty bounded mapping")
    for key, template in frames.items():
        if not isinstance(key, str) or not key.strip() or len(key) > MAX_FRAME_TEMPLATE_CHARS:
            raise ValueError("Fact-phrasing frame key must be a bounded nonempty string")
        if not isinstance(template, str) or not template.strip() or len(template) > MAX_FRAME_TEMPLATE_CHARS:
            raise ValueError("Fact-phrasing template must be a bounded nonempty string")
        fields = []
        bare_parts = []
        for literal, field, spec, conversion in Formatter().parse(template):
            bare_parts.append(literal.replace("{", "{{").replace("}", "}}"))
            if field is not None:
                if field not in ("s", "o") or spec or conversion is not None:
                    raise ValueError("Fact-phrasing template permits only bare subject/object placeholders")
                fields.append(field)
                bare_parts.append("{" + field + "}")
        if fields != ["s", "o"] or "".join(bare_parts) != template:
            raise ValueError("Fact-phrasing template requires one subject then one object")
    validated = dict(frames)
    return validated


FRAME_OVERRIDES = read_frame_overrides(FRAME_OVERRIDES_PATH)
# Words in one pattern, that, or topic. The matcher descends one stack frame
# per pattern word; the longest shipped seed pattern has 12.
MAX_PATTERN_WORDS = 64
# A sentence longer than this is not read for facts: its subject could not
# become a pattern, and tagging it would hold the engine for hundreds of ms.
MAX_FACT_SENTENCE_WORDS = 2 * MAX_PATTERN_WORDS
TEMPLATE_STAR_EXPRESSION = r"\{star(\d+)\}"
TEMPLATE_THATSTAR_EXPRESSION = r"\{thatstar(\d+)\}"
TEMPLATE_TOPICSTAR_EXPRESSION = r"\{topicstar(\d+)\}"
TEMPLATE_GET_EXPRESSION = r"\{get:([^:}]+)(?::([^}]*))?\}"
TEMPLATE_BOT_EXPRESSION = r"\{bot:([^}]+)\}"
TEMPLATE_MAP_EXPRESSION = r"\{map:([^:}]+):([^:}]+)(?::([^}]*))?\}"
TEMPLATE_INPUT_EXPRESSION = r"\{input:(\d+)\}"
TEMPLATE_RESPONSE_EXPRESSION = r"\{response(?::(\d+))?\}"
TEMPLATE_THAT_EXPRESSION = r"\{that(?::(\d+)(?::(\d+))?)?\}"
TEMPLATE_TRANSFORM_EXPRESSION = (
    r"\{(upper|lower|capitalize|formal|sentence|person|person2|gender|"
    r"normalize|denormalize|explode|first|rest|uniq|wordcount|sentiment|"
    r"clause|qtype|name):([^{}]*)\}"
)
TEMPLATE_DATE_FORMAT_EXPRESSION = r"\{date:([^}]+)\}"
# A backslash before one of these characters renders that character literally.
TEMPLATE_ESCAPE_CHARACTER = "\\"
TEMPLATE_ESCAPABLE_CHARACTERS = "\\{}"
TEMPLATE_SIMPLE_VARIABLE_TOKENS = {
    "topic": "{topic}",
    "input": "{input}",
    "request": "{request}",
    "id": "{id}",
    "size": "{size}",
    "vocabulary": "{vocabulary}",
    "date": "{date}",
    "time": "{time}",
    "program": "{program}",
}


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


MODEL_NAME = "en_core_web_sm"


# POS tags for nouns and proper nouns (the referents a follow-up query's
# pronouns can point back to; used for session context expansion)
NOUN_POS_TAGS = {"NN", "NNS", "NNP", "NNPS"}
# The proper-noun subset: a sentence-opening span counts as a name only under these tags.
PROPER_NOUN_POS_TAGS = {"NNP", "NNPS"}

# Spelling correction (input cleanup). Correction is deliberately timid: only
# tokens at least MIN_SPELL_TOKEN_LENGTH long that are neither in the target
# vocabulary nor real English words are candidates, and only a unique nearest
# neighbor within the allowed Damerau-Levenshtein distance replaces them.
MIN_SPELL_TOKEN_LENGTH = 4
SPELL_LONG_TOKEN_LENGTH = 6  # Tokens this long or longer allow MAX_SPELL_EDIT_DISTANCE (else 1)
MAX_SPELL_EDIT_DISTANCE = 2

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


# VADER compound-score thresholds (the standard cutoffs from the VADER paper).
POSITIVE_THRESHOLD = 0.05
NEGATIVE_THRESHOLD = -0.05

NEUTRAL = "neutral"
POSITIVE = "positive"
NEGATIVE = "negative"

NEUTRAL_SCORES = {"compound": 0.0, "pos": 0.0, "neu": 1.0, "neg": 0.0}


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


# Overlap credit for a query keyword matched only through a WordNet synonym,
# relative to the 1.0 credit of an exact keyword match.
SYNONYM_OVERLAP_WEIGHT = 0.5
# Hit-rate prior for a keyword or statement match with no query observations yet.
NEUTRAL_HIT_RATE = 0.5


WILDCARD_TOKENS = {"*", "_", "#", "^"}
