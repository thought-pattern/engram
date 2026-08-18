"""Deterministic turn interpretation for conversational Engram callers.

The pattern engine remains the response authority.  This module supplies the
small amount of discourse state that pattern matching alone cannot express:
what kind of conversational move a sentence makes, which topic/entities it
references, whether a conversationally extracted fact is durable enough to
cache, and which sentence should represent a multi-sentence turn.
"""

import re

from engram.constants import (
    DIALOGUE_ACKNOWLEDGMENT,
    DIALOGUE_ACKNOWLEDGMENT_RE as _ACKNOWLEDGMENT_RE,
    DIALOGUE_BROAD_PATTERNS as _BROAD_DIALOGUE_PATTERNS,
    DIALOGUE_CLOSING,
    DIALOGUE_CLOSING_RE as _CLOSING_RE,
    DIALOGUE_COMMAND,
    DIALOGUE_DISCOURSE_FACT_SUBJECT_LEADS as _DISCOURSE_FACT_SUBJECT_LEADS,
    DIALOGUE_DISCOURSE_TOPIC_PREFIX_RE as _DISCOURSE_TOPIC_PREFIX_RE,
    DIALOGUE_EMOTION,
    DIALOGUE_EMOTION_RE as _EMOTION_RE,
    DIALOGUE_ENTITY_LABEL_PRIORITY as _ENTITY_LABEL_PRIORITY,
    DIALOGUE_ENTITY_LEADS as _ENTITY_LEADS,
    DIALOGUE_FACT,
    DIALOGUE_GRATITUDE,
    DIALOGUE_GRATITUDE_RE as _GRATITUDE_RE,
    DIALOGUE_GREETING,
    DIALOGUE_GREETING_RE as _GREETING_RE,
    DIALOGUE_HEDGE_RE as _HEDGE_RE,
    DIALOGUE_INVALID_TOPIC_WORDS as _INVALID_TOPIC_WORDS,
    DIALOGUE_META_FACT_WORDS as _META_FACT_WORDS,
    DIALOGUE_OPINION,
    DIALOGUE_OPINION_RE as _OPINION_RE,
    DIALOGUE_PERSONAL_FACT_OBJECT_WORDS as _PERSONAL_FACT_OBJECT_WORDS,
    DIALOGUE_QUALIFIED_FACT_SUBJECT_LEADS as _QUALIFIED_FACT_SUBJECT_LEADS,
    DIALOGUE_QUESTION,
    DIALOGUE_QUESTION_TOPIC_RES as _QUESTION_TOPIC_RES,
    DIALOGUE_REFERRING_RE as _REFERRING_RE,
    DIALOGUE_SELF_INTRODUCTION,
    DIALOGUE_SELF_INTRODUCTION_RE as _SELF_INTRODUCTION_RE,
    DIALOGUE_STATEMENT,
    DIALOGUE_TOPIC_LEADING_MODIFIERS as _TOPIC_LEADING_MODIFIERS,
    DIALOGUE_TOPIC_SHIFT,
    DIALOGUE_TOPIC_SHIFT_RE as _TOPIC_SHIFT_RE,
    DIALOGUE_TOPIC_TRAILERS as _TOPIC_TRAILERS,
    DIALOGUE_TRANSIENT_RE as _TRANSIENT_RE,
    DIALOGUE_VAGUE_FACT_SUBJECTS as _VAGUE_FACT_SUBJECTS,
    KIND_COMMAND,
    KIND_QUESTION,
)
from engram.nlp import input_kind


def classify_dialogue_act(text: str, fact=()) -> str:
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
        result = ""
        return result
    topic = " ".join(words).strip(" \t\r\n.,!?;:'\"")
    topic_words = {word.lower() for word in re.findall(r"[\w'-]+", topic)}
    if not topic_words or topic_words & _INVALID_TOPIC_WORDS:
        result = ""
        return result
    return topic


def explicit_topic(text: str) -> str:
    """Return a topic explicitly named by a topic-change/about construction."""
    match = _TOPIC_SHIFT_RE.search(text.strip())
    if not match:
        about_match = re.search(r"\b(?:know|tell me|learn|think)\s+about\s+(.+)$", text, re.IGNORECASE)
        if about_match:
            result = _clean_topic(about_match.group(1))
            return result
        for question_re in _QUESTION_TOPIC_RES:
            question_match = question_re.match(text)
            if question_match:
                result = _clean_topic(question_match.group(1))
                return result
        result = ""
        return result
    value = next((group for group in match.groups() if group), "")
    result = _clean_topic(value)
    return result


def infer_active_topic(
    text: str,
    fact=(),
    entities=(),
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
    result = ""
    return result


def extract_dialogue_entities(text: str, fact=(), topic: str = "") -> list[dict]:
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
        result = {"admitted": False, "reason": "missing_fact"}
        return result
    subject = str(fact.get("subject", "")).strip()
    obj = str(fact.get("obj", fact.get("object", ""))).strip()
    if not subject or not obj:
        result = {"admitted": False, "reason": "missing_fields"}
        return result
    if _DISCOURSE_TOPIC_PREFIX_RE.match(text):
        result = {"admitted": False, "reason": "discourse_subject"}
        return result
    if _HEDGE_RE.search(text):
        result = {"admitted": False, "reason": "hedged"}
        return result
    if _TRANSIENT_RE.search(text):
        result = {"admitted": False, "reason": "transient"}
        return result

    subject_tokens = [word.lower() for word in re.findall(r"[\w'-]+", subject)]
    subject_words = set(subject_tokens)
    object_words = {word.lower() for word in re.findall(r"[\w'-]+", obj)}
    if subject_tokens[0] in _DISCOURSE_FACT_SUBJECT_LEADS:
        result = {"admitted": False, "reason": "discourse_subject"}
        return result
    if subject_tokens[0] in _QUALIFIED_FACT_SUBJECT_LEADS:
        result = {"admitted": False, "reason": "qualified_subject"}
        return result
    if subject_words & _META_FACT_WORDS:
        result = {"admitted": False, "reason": "meta_subject"}
        return result
    if subject_words & _VAGUE_FACT_SUBJECTS:
        result = {"admitted": False, "reason": "vague_subject"}
        return result
    if object_words & _PERSONAL_FACT_OBJECT_WORDS:
        result = {"admitted": False, "reason": "personal_object"}
        return result
    if object_words & {"temporary", "unknown", "unsure"}:
        result = {"admitted": False, "reason": "unstable_object"}
        return result
    result = {"admitted": True, "reason": "admitted"}
    return result


def conversational_fact_is_admissible(fact: dict, text: str) -> bool:
    """Compatibility Boolean for the reasoned admission decision."""
    result = conversational_fact_admission(fact, text)["admitted"]
    return result


def topic_is_referenced(text: str, topic: str) -> bool:
    """Return whether a turn explicitly or pronominally continues a topic."""
    if not topic:
        result = False
        return result
    result = _topic_is_named(text, topic) or bool(_REFERRING_RE.search(text))
    return result


def _topic_is_named(text: str, topic: str) -> bool:
    """Return whether *topic* occurs as a complete token sequence in *text*."""
    text_words = re.findall(r"[\w'-]+", text.casefold())
    topic_words = re.findall(r"[\w'-]+", topic.casefold())
    if not topic_words or len(topic_words) > len(text_words):
        result = False
        return result
    width = len(topic_words)
    result = any(text_words[start : start + width] == topic_words for start in range(len(text_words) - width + 1))
    return result


def dialogue_act_clears_unreferenced_topic(dialogue_act: str) -> bool:
    """Return whether an unrelated act starts a new substantive thread."""
    result = dialogue_act in {
        DIALOGUE_COMMAND,
        DIALOGUE_EMOTION,
        DIALOGUE_FACT,
        DIALOGUE_OPINION,
        DIALOGUE_QUESTION,
        DIALOGUE_STATEMENT,
        DIALOGUE_TOPIC_SHIFT,
    }
    return result


def topic_from_statement_pattern(pattern: str, statement_text: str = "") -> str:
    """Recover a learned fact's case-preserving subject topic.

    Learned patterns are normalized to uppercase, so the pattern alone cannot
    distinguish an acronym such as ``ENIAC`` from an ordinary name such as
    ``Alice``.  Prefer the matching surface span from the original statement
    and retain title-casing only as a compatibility fallback.
    """
    if not pattern or "*" in pattern or "_" in pattern or "{" in pattern:
        result = ""
        return result

    pattern_words = [word.casefold() for word in re.findall(r"[\w'-]+", pattern)]
    if not pattern_words:
        result = ""
        return result
    statement_words = list(re.finditer(r"[\w'-]+", statement_text))
    for start in range(len(statement_words) - len(pattern_words) + 1):
        matches = statement_words[start : start + len(pattern_words)]
        if [match.group(0).casefold() for match in matches] != pattern_words:
            continue
        surface = statement_text[matches[0].start() : matches[-1].end()]
        topic = _clean_topic(surface)
        if topic:
            return topic
    result = _clean_topic(pattern.title())
    return result


def pattern_is_broad(pattern: str) -> bool:
    """Return whether a pattern expresses little conversational intent."""
    result = pattern.upper().strip() in _BROAD_DIALOGUE_PATTERNS
    return result


def repetition_response_options(dialogue_act: str, topic: str = "") -> tuple[str, ...]:
    """Alternatives when any authored response repeats recent output."""
    if dialogue_act == DIALOGUE_ACKNOWLEDGMENT:
        result = ("Right - I heard you.", "Understood; let's keep moving.")
        return result
    if dialogue_act == DIALOGUE_GREETING:
        result = ("Hello again.",)
        return result
    if dialogue_act == DIALOGUE_GRATITUDE:
        result = ("Glad to help.",)
        return result
    if dialogue_act == DIALOGUE_CLOSING:
        result = ("Take care.",)
        return result
    if topic:
        result = (f"I don't want to repeat myself about {topic}; let's move the conversation forward.",)
        return result
    result = ("I don't want to repeat the same line; let's move the conversation forward.",)
    return result


def repeated_input_response_options() -> tuple[str, ...]:
    """Topic-neutral continuations when an ordinary input is repeated."""
    result = (
        "We've returned to that idea. Which part would you like to explore further?",
        "That thought has come up before. What new angle should we take?",
        "We're circling back to that. What feels unfinished about it?",
    )
    return result


def select_turn_candidate(candidates: list[dict]) -> dict:
    """Choose the response-bearing sentence that represents the whole turn.

    The final substantive sentence normally wins.  A trailing courtesy or
    acknowledgment does not discard an earlier question, command, topic
    change, fact, or self-introduction.
    """
    if not candidates:
        result = {}
        return result

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
        result = later_requests[-1] if later_requests else candidate
        return result

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
            result = ("You're welcome. We can stop here for today.", "Of course. We can leave it there for now.")
            return result
        result = ("Of course. We can stop here for today.", "Understood. We can leave it there for now.")
        return result
    if dialogue_act == DIALOGUE_TOPIC_SHIFT and topic:
        result = (f"Sure - let's talk about {topic}.",)
        return result
    if not topic:
        result = ()
        return result
    if dialogue_act == DIALOGUE_QUESTION:
        result = (f"I don't know enough about {topic} to answer that yet.",)
        return result
    if dialogue_act == DIALOGUE_ACKNOWLEDGMENT:
        result = (f"Right - {topic} is the thread we're following.",)
        return result
    if dialogue_act == DIALOGUE_EMOTION:
        options = [f"That adds a personal angle to what we're saying about {topic}."]
        if fact_text:
            options.insert(0, f"That feeling connects with what you said about {topic}: {fact_text}")
        result = tuple(options)
        return result
    if dialogue_act in {DIALOGUE_FACT, DIALOGUE_OPINION, DIALOGUE_STATEMENT}:
        options = [f"Staying with {topic}, that adds another angle to the conversation."]
        if fact_text:
            options.insert(0, f"That connects with what you said about {topic}: {fact_text}")
        result = tuple(options)
        return result
    result = ()
    return result
