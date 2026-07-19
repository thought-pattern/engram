"""Deterministic turn interpretation for conversational Engram callers.

The pattern engine remains the response authority.  This module supplies the
small amount of discourse state that pattern matching alone cannot express:
what kind of conversational move a sentence makes, which topic/entities it
references, whether a conversationally extracted fact is durable enough to
cache, and which sentence should represent a multi-sentence turn.
"""

import re

from engram.constants import KIND_COMMAND, KIND_QUESTION
from engram.nlp import input_kind

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

_CLOSING_RE = re.compile(
    r"\b(?:goodbye|bye|farewell|good night|see you|talk (?:to you )?later|catch you later|"
    r"enough for (?:today|now)|done for (?:today|now)|stop here|leave it there)\b",
    re.IGNORECASE,
)
_GRATITUDE_RE = re.compile(r"\b(?:thank you|thanks|much appreciated|appreciate it)\b", re.IGNORECASE)
_GREETING_RE = re.compile(r"^\s*(?:hello|hi|hey|greetings|good morning|good afternoon|good evening)\b", re.IGNORECASE)
_SELF_INTRODUCTION_RE = re.compile(r"\b(?:my name is|i am called|call me)\b", re.IGNORECASE)
_TOPIC_SHIFT_RE = re.compile(
    r"\b(?:talk|speak|chat|discuss)\s+about\s+(.+)$|"
    r"\b(?:change|switch)\s+(?:the\s+)?topic\s+to\s+(.+)$|"
    r"\b(?:move|switch)\s+(?:on\s+)?to\s+(.+)$|"
    r"\b(?:return|go\s+back|come\s+back)\s+to\s+(.+)$",
    re.IGNORECASE,
)
_QUESTION_TOPIC_RES = (
    re.compile(r"^\s*(?:what|who|where)\s+(?:is|are|was|were)\s+(.+?)\s*[?!.]*$", re.IGNORECASE),
    re.compile(r"^\s*how\s+\w+\s+(?:is|are|was|were)\s+(.+?)\s*[?!.]*$", re.IGNORECASE),
    re.compile(r"^\s*what\b.*\bwhen\s+you\s+(?:consider|think\s+about)\s+(.+?)\s*[?!.]*$", re.IGNORECASE),
)
_EMOTION_RE = re.compile(
    r"\b(?:feel|feeling|felt|happy|sad|angry|anxious|excited|worried|wistful|afraid|upset|glad|lonely)\b",
    re.IGNORECASE,
)
_OPINION_RE = re.compile(r"\b(?:i think|i believe|in my opinion|i prefer|i like|i dislike|seems to me)\b", re.IGNORECASE)
_ACKNOWLEDGMENT_RE = re.compile(
    r"^\s*(?:yes|yeah|yep|no|nope|okay|ok|right|exactly|sure|agreed|understood|i see|got it|fair enough)" r"[.!\s]*$",
    re.IGNORECASE,
)
_REFERRING_RE = re.compile(
    r"\b(?:he|her|hers|herself|him|himself|his|it|its|she|they|them|their|theirs|this|that|these|those)\b",
    re.IGNORECASE,
)
_DISCOURSE_TOPIC_PREFIX_RE = re.compile(
    r"^\s*(?:after\b[^:\r\n]{1,80}:|because\b|before\s+we\b|for\s+my\s+part\b|"
    r"for\b[^:\r\n]{1,80}:|here\s+(?:is|are|was|were)\b|"
    r"one\s+more(?:\s+[\w'-]+){0,3}\s+thought\b|there\s+(?:is|are|was|were)\b|to\s+me\b)",
    re.IGNORECASE,
)

_HEDGE_RE = re.compile(
    r"\b(?:maybe|perhaps|possibly|probably|supposedly|apparently|i guess|i suppose|might|could|would)\b",
    re.IGNORECASE,
)
_TRANSIENT_RE = re.compile(
    r"\b(?:right now|at the moment|for now|today|tonight|currently|temporarily|lately|this morning|"
    r"this afternoon|this evening|this week|this month|this year)\b",
    re.IGNORECASE,
)
_META_FACT_WORDS = {
    "answer",
    "chat",
    "claim",
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
_VAGUE_FACT_SUBJECTS = {"anything", "everything", "nothing", "something", "stuff", "thing", "things"}
_TOPIC_TRAILERS = {"again", "broadly", "instead", "next", "now", "please", "specifically"}
_TOPIC_LEADING_MODIFIERS = {
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
_GRAMMATICAL_TOPIC_WORDS = {
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
_INVALID_TOPIC_WORDS = _META_FACT_WORDS | _VAGUE_FACT_SUBJECTS | _GRAMMATICAL_TOPIC_WORDS
_AMBIGUOUS_CAPITALIZED_LEADS = {
    "after",
    "allow",
    "because",
    "before",
    "for",
    "here",
    "if",
    "in",
    "one",
    "there",
    "to",
    "which",
    "with",
}
_ENTITY_LEADS = (
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
    | _GRAMMATICAL_TOPIC_WORDS
    | _TOPIC_LEADING_MODIFIERS
    | _AMBIGUOUS_CAPITALIZED_LEADS
)
_DISCOURSE_FACT_SUBJECT_LEADS = {"actually", "no", "okay", "right", "well", "yes"}
_QUALIFIED_FACT_SUBJECT_LEADS = {"generally", "occasionally", "often", "sometimes", "typically", "usually"}
_PERSONAL_FACT_OBJECT_WORDS = {
    "i",
    "me",
    "mine",
    "my",
    "our",
    "ours",
    "us",
    "we",
    "you",
    "your",
    "yours",
}
_ENTITY_LABEL_PRIORITY = {"PROPER_NOUN": 1, "TOPIC": 2, "SUBJECT": 3}
_BROAD_DIALOGUE_PATTERNS = {"THAT *", "THAT IS *", "THE *"}


def classify_dialogue_act(text: str, fact: dict | None = None) -> str:
    """Classify one sentence into a stable, caller-visible dialogue act."""
    stripped = text.strip()
    if _CLOSING_RE.search(stripped):
        return DIALOGUE_CLOSING
    if _TOPIC_SHIFT_RE.search(stripped):
        return DIALOGUE_TOPIC_SHIFT
    if _GRATITUDE_RE.search(stripped):
        return DIALOGUE_GRATITUDE
    if _GREETING_RE.search(stripped):
        return DIALOGUE_GREETING
    if _SELF_INTRODUCTION_RE.search(stripped):
        return DIALOGUE_SELF_INTRODUCTION

    kind = input_kind(stripped)
    if kind == KIND_QUESTION:
        return DIALOGUE_QUESTION
    if kind == KIND_COMMAND:
        return DIALOGUE_COMMAND
    if fact:
        return DIALOGUE_FACT
    if _EMOTION_RE.search(stripped):
        return DIALOGUE_EMOTION
    if _ACKNOWLEDGMENT_RE.match(stripped):
        return DIALOGUE_ACKNOWLEDGMENT
    if _OPINION_RE.search(stripped):
        return DIALOGUE_OPINION
    return DIALOGUE_STATEMENT


def _clean_topic(value: str) -> str:
    value = value.strip(" \t\r\n.,!?;:'\"")
    words = value.split()
    while words:
        leading_word = re.sub(r"(^[^\w'-]+|[^\w'-]+$)", "", words[0]).lower()
        if leading_word and leading_word not in _TOPIC_LEADING_MODIFIERS and leading_word not in {"a", "an", "the"}:
            break
        words.pop(0)
    lowered = [word.lower() for word in words]
    if len(lowered) >= 3 and lowered[-3:] == ["for", "a", "while"]:
        words = words[:-3]
    elif len(lowered) >= 2 and lowered[-2:] in (["for", "now"], ["in", "general"]):
        words = words[:-2]
    while words and words[-1].lower() in _TOPIC_TRAILERS:
        words.pop()
    if not words or len(words) > 8:
        return ""
    topic = " ".join(words).strip(" \t\r\n.,!?;:'\"")
    topic_words = {word.lower() for word in re.findall(r"[\w'-]+", topic)}
    if not topic_words or topic_words & _INVALID_TOPIC_WORDS:
        return ""
    return topic


def explicit_topic(text: str) -> str:
    """Return a topic explicitly named by a topic-change/about construction."""
    match = _TOPIC_SHIFT_RE.search(text.strip())
    if not match:
        about_match = re.search(r"\b(?:know|tell me|learn|think)\s+about\s+(.+)$", text, re.IGNORECASE)
        if about_match:
            return _clean_topic(about_match.group(1))
        for question_re in _QUESTION_TOPIC_RES:
            question_match = question_re.match(text)
            if question_match:
                return _clean_topic(question_match.group(1))
        return ""
    value = next((group for group in match.groups() if group), "")
    return _clean_topic(value)


def infer_active_topic(
    text: str,
    fact: dict | None = None,
    entities: list[dict] | None = None,
    previous_topic: str = "",
) -> str:
    """Infer a durable turn topic without treating every noun as a topic."""
    named_topic = explicit_topic(text)
    if named_topic:
        return named_topic
    # A named reference to the established topic is stronger evidence than a
    # newly extracted noun phrase.  This keeps a discourse frame such as
    # "If gardens ..." or "a patient observer of gardens ..." from replacing
    # the durable topic with ``If`` or the larger descriptive subject.
    if previous_topic and _topic_is_named(text, previous_topic):
        return previous_topic
    if fact and fact.get("subject") and not _DISCOURSE_TOPIC_PREFIX_RE.match(text):
        subject = _clean_topic(str(fact["subject"]))
        subject_words = {word.lower() for word in re.findall(r"[\w'-]+", subject)}
        if subject and not subject_words & _INVALID_TOPIC_WORDS:
            return subject
    for entity in reversed(entities or []):
        if entity.get("label") in {
            "FACILITY",
            "GPE",
            "GSP",
            "ORGANIZATION",
            "PERSON",
            "PROPER_NOUN",
            "SUBJECT",
            "TOPIC",
        }:
            candidate = _clean_topic(str(entity.get("text", "")))
            if candidate:
                return candidate
    if previous_topic and _REFERRING_RE.search(text):
        return previous_topic
    return ""


def extract_dialogue_entities(text: str, fact: dict | None = None, topic: str = "") -> list[dict]:
    """Extract lightweight references suitable for per-turn tracking.

    Full NLTK named-entity chunking remains available through
    :func:`engram.nlp.extract_entities`.  Conversation routing only needs
    stable surface references, so it uses fact subjects, explicit topics, and
    capitalized names without paying the chunker's per-turn cost.
    """
    entities: list[dict] = []

    def add(value: str, label: str) -> None:
        value = _clean_topic(value)
        if not value:
            return
        for existing in entities:
            if existing["text"].casefold() != value.casefold():
                continue
            if _ENTITY_LABEL_PRIORITY.get(label, 0) > _ENTITY_LABEL_PRIORITY.get(existing["label"], 0):
                existing["label"] = label
                existing["text"] = value
            return
        entities.append({"text": value, "label": label})

    if fact and fact.get("subject"):
        add(str(fact["subject"]), "SUBJECT")
    if topic:
        add(topic, "TOPIC")
    discourse_prefix = _DISCOURSE_TOPIC_PREFIX_RE.match(text)
    for match in re.finditer(r"\b[A-Z][\w'-]*(?:\s+[A-Z][\w'-]*)*\b", text):
        if discourse_prefix and match.start() < discourse_prefix.end():
            continue
        parts = match.group(0).split()
        while parts and parts[0].casefold() in _ENTITY_LEADS:
            parts.pop(0)
        if not parts:
            continue
        value = " ".join(parts)
        add(value, "PROPER_NOUN")
    return entities


def conversational_fact_admission(fact: dict, text: str) -> dict:
    """Return a conversational fact-admission decision with a reason.

    Explicit ``Engram.add_fact`` ingestion intentionally does not use this
    gate: a research/calling application has already decided that its input is
    knowledge.  This gate applies only to facts inferred from casual chat.
    """
    if not fact:
        return {"admitted": False, "reason": "missing_fact"}
    subject = str(fact.get("subject", "")).strip()
    obj = str(fact.get("obj", fact.get("object", ""))).strip()
    if not subject or not obj:
        return {"admitted": False, "reason": "missing_fields"}
    if _DISCOURSE_TOPIC_PREFIX_RE.match(text):
        return {"admitted": False, "reason": "discourse_subject"}
    if _HEDGE_RE.search(text):
        return {"admitted": False, "reason": "hedged"}
    if _TRANSIENT_RE.search(text):
        return {"admitted": False, "reason": "transient"}

    subject_tokens = [word.lower() for word in re.findall(r"[\w'-]+", subject)]
    subject_words = set(subject_tokens)
    object_words = {word.lower() for word in re.findall(r"[\w'-]+", obj)}
    if subject_tokens[0] in _DISCOURSE_FACT_SUBJECT_LEADS:
        return {"admitted": False, "reason": "discourse_subject"}
    if subject_tokens[0] in _QUALIFIED_FACT_SUBJECT_LEADS:
        return {"admitted": False, "reason": "qualified_subject"}
    if subject_words & _META_FACT_WORDS:
        return {"admitted": False, "reason": "meta_subject"}
    if subject_words & _VAGUE_FACT_SUBJECTS:
        return {"admitted": False, "reason": "vague_subject"}
    if object_words & _PERSONAL_FACT_OBJECT_WORDS:
        return {"admitted": False, "reason": "personal_object"}
    if object_words & {"temporary", "unknown", "unsure"}:
        return {"admitted": False, "reason": "unstable_object"}
    return {"admitted": True, "reason": "admitted"}


def conversational_fact_is_admissible(fact: dict, text: str) -> bool:
    """Compatibility Boolean for the reasoned admission decision."""
    return conversational_fact_admission(fact, text)["admitted"]


def topic_is_referenced(text: str, topic: str) -> bool:
    """Return whether a turn explicitly or pronominally continues a topic."""
    if not topic:
        return False
    return _topic_is_named(text, topic) or bool(_REFERRING_RE.search(text))


def _topic_is_named(text: str, topic: str) -> bool:
    """Return whether *topic* occurs as a complete token sequence in *text*."""
    text_words = re.findall(r"[\w'-]+", text.casefold())
    topic_words = re.findall(r"[\w'-]+", topic.casefold())
    if not topic_words or len(topic_words) > len(text_words):
        return False
    width = len(topic_words)
    return any(text_words[start : start + width] == topic_words for start in range(len(text_words) - width + 1))


def dialogue_act_clears_unreferenced_topic(dialogue_act: str) -> bool:
    """Return whether an unrelated act starts a new substantive thread."""
    return dialogue_act in {
        DIALOGUE_COMMAND,
        DIALOGUE_EMOTION,
        DIALOGUE_FACT,
        DIALOGUE_OPINION,
        DIALOGUE_QUESTION,
        DIALOGUE_STATEMENT,
        DIALOGUE_TOPIC_SHIFT,
    }


def topic_from_statement_pattern(pattern: str, statement_text: str = "") -> str:
    """Recover a learned fact's case-preserving subject topic.

    Learned patterns are normalized to uppercase, so the pattern alone cannot
    distinguish an acronym such as ``ENIAC`` from an ordinary name such as
    ``Alice``.  Prefer the matching surface span from the original statement
    and retain title-casing only as a compatibility fallback.
    """
    if not pattern or "*" in pattern or "_" in pattern or "{" in pattern:
        return ""

    pattern_words = [word.casefold() for word in re.findall(r"[\w'-]+", pattern)]
    if not pattern_words:
        return ""
    statement_words = list(re.finditer(r"[\w'-]+", statement_text))
    for start in range(len(statement_words) - len(pattern_words) + 1):
        matches = statement_words[start : start + len(pattern_words)]
        if [match.group(0).casefold() for match in matches] != pattern_words:
            continue
        surface = statement_text[matches[0].start() : matches[-1].end()]
        topic = _clean_topic(surface)
        if topic:
            return topic
    return _clean_topic(pattern.title())


def pattern_is_broad(pattern: str) -> bool:
    """Return whether a pattern expresses little conversational intent."""
    return pattern.upper().strip() in _BROAD_DIALOGUE_PATTERNS


def repetition_response_options(dialogue_act: str, topic: str = "") -> tuple[str, ...]:
    """Alternatives when any authored response repeats recent output."""
    if dialogue_act == DIALOGUE_ACKNOWLEDGMENT:
        return ("Right - I heard you.", "Understood; let's keep moving.")
    if dialogue_act == DIALOGUE_GREETING:
        return ("Hello again.",)
    if dialogue_act == DIALOGUE_GRATITUDE:
        return ("Glad to help.",)
    if dialogue_act == DIALOGUE_CLOSING:
        return ("Take care.",)
    if topic:
        return (f"I don't want to repeat myself about {topic}; let's move the conversation forward.",)
    return ("I don't want to repeat the same line; let's move the conversation forward.",)


def repeated_input_response_options() -> tuple[str, ...]:
    """Topic-neutral continuations when an ordinary input is repeated."""
    return (
        "We've returned to that idea. Which part would you like to explore further?",
        "That thought has come up before. What new angle should we take?",
        "We're circling back to that. What feels unfinished about it?",
    )


def select_turn_candidate(candidates: list[dict]) -> dict:
    """Choose the response-bearing sentence that represents the whole turn.

    The final substantive sentence normally wins.  A trailing courtesy or
    acknowledgment does not discard an earlier question, command, topic
    change, fact, or self-introduction.
    """
    if not candidates:
        return {}

    # An explicit farewell remains the turn intent when followed by a
    # compliment or well-wish. A later request genuinely reopens the turn and
    # takes precedence over the closing.
    for index, candidate in enumerate(candidates):
        if candidate["dialogue_act"] != DIALOGUE_CLOSING:
            continue
        later_requests = [
            later
            for later in candidates[index + 1 :]
            if later["dialogue_act"] in {DIALOGUE_COMMAND, DIALOGUE_QUESTION, DIALOGUE_TOPIC_SHIFT}
        ]
        return later_requests[-1] if later_requests else candidate

    final = candidates[-1]
    if final["dialogue_act"] not in {DIALOGUE_ACKNOWLEDGMENT, DIALOGUE_GRATITUDE, DIALOGUE_GREETING}:
        return final
    substantive = {
        DIALOGUE_CLOSING,
        DIALOGUE_COMMAND,
        DIALOGUE_FACT,
        DIALOGUE_QUESTION,
        DIALOGUE_SELF_INTRODUCTION,
        DIALOGUE_TOPIC_SHIFT,
    }
    for candidate in reversed(candidates[:-1]):
        if candidate["dialogue_act"] in substantive:
            return candidate
    return final


def contextual_fallback_options(
    dialogue_act: str,
    topic: str = "",
    fact_text: str = "",
    had_gratitude: bool = False,
) -> tuple[str, ...]:
    """Return ordered, topic-aware alternatives to a generic catch-all."""
    if dialogue_act == DIALOGUE_CLOSING:
        if had_gratitude:
            return ("You're welcome. We can stop here for today.", "Of course. We can leave it there for now.")
        return ("Of course. We can stop here for today.", "Understood. We can leave it there for now.")
    if dialogue_act == DIALOGUE_TOPIC_SHIFT and topic:
        return (f"Sure - let's talk about {topic}.",)
    if not topic:
        return ()
    if dialogue_act == DIALOGUE_QUESTION:
        return (f"I don't know enough about {topic} to answer that yet.",)
    if dialogue_act == DIALOGUE_ACKNOWLEDGMENT:
        return (f"Right - {topic} is the thread we're following.",)
    if dialogue_act == DIALOGUE_EMOTION:
        options = [f"That adds a personal angle to what we're saying about {topic}."]
        if fact_text:
            options.insert(0, f"That feeling connects with what you said about {topic}: {fact_text}")
        return tuple(options)
    if dialogue_act in {DIALOGUE_FACT, DIALOGUE_OPINION, DIALOGUE_STATEMENT}:
        options = [f"Staying with {topic}, that adds another angle to the conversation."]
        if fact_text:
            options.insert(0, f"That connects with what you said about {topic}: {fact_text}")
        return tuple(options)
    return ()
