"""Dialogue-act labels and the dialogue interpretation patterns and word lists."""

from re import IGNORECASE, compile as re_compile

DIALOGUE_ACKNOWLEDGMENT = "acknowledgment"
DIALOGUE_CLOSING = "closing"
DIALOGUE_COMMAND = "command"
DIALOGUE_EMOTION = "emotion"
DIALOGUE_FACT = "fact"
DIALOGUE_GRATITUDE = "gratitude"
DIALOGUE_GREETING = "greeting"
DIALOGUE_OPINION = "opinion"
DIALOGUE_QUESTION = "question"
DIALOGUE_SELF_INTRODUCTION = "self_introduction"
DIALOGUE_STATEMENT = "statement"
DIALOGUE_TOPIC_SHIFT = "topic_shift"


DIALOGUE_CLOSING_RE = re_compile(
    r"\b(?:goodbye|bye|farewell|good night|see you|talk (?:to you )?later|catch you later|"
    r"enough for (?:today|now)|done for (?:today|now)|stop here|leave it there)\b",
    IGNORECASE,
)
DIALOGUE_GRATITUDE_RE = re_compile(r"\b(?:thank you|thanks|much appreciated|appreciate it)\b", IGNORECASE)
DIALOGUE_GREETING_RE = re_compile(r"^\s*(?:hello|hi|hey|greetings|good morning|good afternoon|good evening)\b", IGNORECASE)
DIALOGUE_SELF_INTRODUCTION_RE = re_compile(r"\b(?:my name is|i am called|call me)\b", IGNORECASE)
DIALOGUE_TOPIC_SHIFT_RE = re_compile(
    r"\b(?:talk|speak|chat|discuss)\s+about\s+(.+)$|"
    r"\b(?:change|switch)\s+(?:the\s+)?topic\s+to\s+(.+)$|"
    r"\b(?:move|switch)\s+(?:on\s+)?to\s+(.+)$|"
    r"\b(?:return|go\s+back|come\s+back)\s+to\s+(.+)$",
    IGNORECASE,
)
DIALOGUE_QUESTION_TOPIC_RES = (
    re_compile(r"^\s*(?:what|who|where)\s+(?:is|are|was|were)\s+(.+?)\s*[?!.]*$", IGNORECASE),
    re_compile(r"^\s*how\s+\w+\s+(?:is|are|was|were)\s+(.+?)\s*[?!.]*$", IGNORECASE),
    re_compile(r"^\s*what\b.*\bwhen\s+you\s+(?:consider|think\s+about)\s+(.+?)\s*[?!.]*$", IGNORECASE),
)
DIALOGUE_EMOTION_RE = re_compile(
    r"\b(?:feel|feeling|felt|happy|sad|angry|anxious|excited|worried|wistful|afraid|upset|glad|lonely)\b", IGNORECASE
)
DIALOGUE_OPINION_RE = re_compile(r"\b(?:i think|i believe|in my opinion|i prefer|i like|i dislike|seems to me)\b", IGNORECASE)
DIALOGUE_ACKNOWLEDGMENT_RE = re_compile(
    r"^\s*(?:yes|yeah|yep|no|nope|okay|ok|right|exactly|sure|agreed|understood|i see|got it|fair enough"
    r"|that helps|that helped|this helps)"
    r"[.!\s]*$",
    IGNORECASE,
)
DIALOGUE_REFERRING_RE = re_compile(
    r"\b(?:he|her|hers|herself|him|himself|his|it|its|she|they|them|their|theirs|this|that|these|those)\b", IGNORECASE
)
DIALOGUE_DISCOURSE_TOPIC_PREFIX_RE = re_compile(
    r"^\s*(?:after\b[^:\r\n]{1,80}:|because\b|before\s+we\b|for\s+my\s+part\b|"
    r"for\b[^:\r\n]{1,80}:|here\s+(?:is|are|was|were)\b|"
    r"one\s+more(?:\s+[\w'-]+){0,3}\s+thought\b|there\s+(?:is|are|was|were)\b|to\s+me\b)",
    IGNORECASE,
)
DIALOGUE_HEDGE_RE = re_compile(
    r"\b(?:maybe|perhaps|possibly|probably|supposedly|apparently|i guess|i suppose|might|could|would)\b", IGNORECASE
)
DIALOGUE_TRANSIENT_RE = re_compile(
    r"\b(?:right now|at the moment|for now|today|tonight|currently|temporarily|lately|this morning|"
    r"this afternoon|this evening|this week|this month|this year)\b",
    IGNORECASE,
)
DIALOGUE_META_FACT_WORDS = set(
    {
        "answer",
        "chat",
        "proposition",
        "conversation",
        "detail",
        "discussion",
        "example",
        "feeling",
        "idea",
        "message",
        "observation",
        "point",
        "prompt",
        "question",
        "remark",
        "reply",
        "response",
        "sentence",
        "statement",
        "test",
        "thought",
        "topic",
        "turn",
    }
)
DIALOGUE_VAGUE_FACT_SUBJECTS = set(
    {"anything", "draft", "everything", "note", "nothing", "part", "something", "stuff", "thing", "things", "try"}
)
DIALOGUE_WH_TOPIC_WORDS = set(
    {
        "how",
        "however",
        "what",
        "whatever",
        "when",
        "whenever",
        "where",
        "wherever",
        "which",
        "whichever",
        "who",
        "whom",
        "whose",
        "why",
    }
)
DIALOGUE_TOPIC_TRAILERS = set({"again", "broadly", "instead", "next", "now", "please", "specifically"})
# Fixed trailing phrases that qualify a topic without naming it ("cities for now").
DIALOGUE_TOPIC_TRAILING_PHRASES = (("for", "a", "while"), ("for", "now"), ("in", "general"))
# A longer span is a clause, not a topic a later turn can refer back to.
MAX_DIALOGUE_TOPIC_WORDS = 8
DIALOGUE_TOPIC_LEADING_MODIFIERS = set(
    {
        "actually",
        "apparently",
        "currently",
        "generally",
        "maybe",
        "no",
        "occasionally",
        "often",
        "okay",
        "perhaps",
        "possibly",
        "probably",
        "right",
        "sometimes",
        "supposedly",
        "today",
        "tonight",
        "typically",
        "usually",
        "well",
        "which",
        "whom",
        "whose",
        "yes",
    }
)
DIALOGUE_GRAMMATICAL_TOPIC_WORDS = set(
    {
        "am",
        "are",
        "be",
        "been",
        "being",
        "can",
        "could",
        "did",
        "do",
        "does",
        "had",
        "has",
        "have",
        "he",
        "her",
        "hers",
        "him",
        "his",
        "i",
        "it",
        "its",
        "me",
        "mine",
        "must",
        "my",
        "our",
        "ours",
        "shall",
        "she",
        "should",
        "that",
        "their",
        "theirs",
        "them",
        "these",
        "they",
        "this",
        "those",
        "us",
        "was",
        "we",
        "were",
        "will",
        "would",
        "you",
        "your",
        "yours",
    }
)
DIALOGUE_INVALID_TOPIC_WORDS = (
    DIALOGUE_META_FACT_WORDS | DIALOGUE_VAGUE_FACT_SUBJECTS | DIALOGUE_GRAMMATICAL_TOPIC_WORDS | DIALOGUE_WH_TOPIC_WORDS
)
DIALOGUE_AMBIGUOUS_CAPITALIZED_LEADS = set(
    {"after", "allow", "because", "before", "for", "here", "if", "in", "one", "there", "to", "which", "with"}
)
DIALOGUE_ENTITY_LEADS = set(
    {
        "a",
        "an",
        "answer",
        "conversation",
        "exactly",
        "good",
        "goodbye",
        "hello",
        "hi",
        "how",
        "let",
        "let's",
        "my",
        "prompt",
        "question",
        "tell",
        "thank",
        "thanks",
        "the",
        "what",
        "when",
        "where",
        "who",
        "why",
    }
    | DIALOGUE_GRAMMATICAL_TOPIC_WORDS
    | DIALOGUE_TOPIC_LEADING_MODIFIERS
    | DIALOGUE_AMBIGUOUS_CAPITALIZED_LEADS
)
DIALOGUE_DISCOURSE_FACT_SUBJECT_LEADS = set({"actually", "no", "okay", "right", "well", "yes"})
DIALOGUE_QUALIFIED_FACT_SUBJECT_LEADS = set({"generally", "occasionally", "often", "sometimes", "typically", "usually"})
DIALOGUE_PERSONAL_FACT_OBJECT_WORDS = set({"i", "me", "mine", "my", "our", "ours", "us", "we", "you", "your", "yours"})
DIALOGUE_ENTITY_LABEL_PRIORITY = {"PROPER_NOUN": 1, "TOPIC": 2, "SUBJECT": 3}
# Most recent discourse entities a session keeps for reference resolution.
MAX_SESSION_ENTITIES = 20
