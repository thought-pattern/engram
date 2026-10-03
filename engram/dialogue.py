"""Deterministic turn interpretation for conversational Engram callers.

The pattern engine remains the response authority.  This module supplies the
small amount of discourse state that pattern matching alone cannot express:
what kind of conversational move a sentence makes, which topic/entities it
references, whether a conversationally extracted fact is durable enough to
cache, and which sentence should represent a multi-sentence turn.
"""

from re import IGNORECASE as IGNORECASE, findall as re_findall, finditer as re_finditer, search as re_search, sub as re_sub

from engram.constants import (
    DIALOGUE_ACKNOWLEDGMENT,
    DIALOGUE_ACKNOWLEDGMENT_RE,
    DIALOGUE_CLOSING,
    DIALOGUE_CLOSING_RE,
    DIALOGUE_COMMAND,
    DIALOGUE_DISCOURSE_FACT_SUBJECT_LEADS,
    DIALOGUE_DISCOURSE_TOPIC_PREFIX_RE,
    DIALOGUE_EMOTION,
    DIALOGUE_EMOTION_RE,
    DIALOGUE_ENTITY_LABEL_PRIORITY,
    DIALOGUE_ENTITY_LEADS,
    DIALOGUE_FACT,
    DIALOGUE_GRATITUDE,
    DIALOGUE_GRATITUDE_RE,
    DIALOGUE_GREETING,
    DIALOGUE_GREETING_RE,
    DIALOGUE_HEDGE_RE,
    DIALOGUE_INVALID_TOPIC_WORDS,
    DIALOGUE_META_FACT_WORDS,
    DIALOGUE_OPINION,
    DIALOGUE_OPINION_RE,
    DIALOGUE_PERSONAL_FACT_OBJECT_WORDS,
    DIALOGUE_QUALIFIED_FACT_SUBJECT_LEADS,
    DIALOGUE_QUESTION,
    DIALOGUE_QUESTION_TOPIC_RES,
    DIALOGUE_REFERRING_RE,
    DIALOGUE_SELF_INTRODUCTION,
    DIALOGUE_SELF_INTRODUCTION_RE,
    DIALOGUE_STATEMENT,
    DIALOGUE_TOPIC_LEADING_MODIFIERS,
    DIALOGUE_TOPIC_SHIFT,
    DIALOGUE_TOPIC_SHIFT_RE,
    DIALOGUE_TOPIC_TRAILERS,
    DIALOGUE_TRANSIENT_RE,
    DIALOGUE_VAGUE_FACT_SUBJECTS,
    DIALOGUE_WH_TOPIC_WORDS,
    KIND_COMMAND,
    KIND_QUESTION,
)
from engram.nlp import input_kind, span_is_proper_noun


def classify_dialogue_act(text: str, fact=()) -> str:
    """Classify one sentence into a stable, caller-visible dialogue act."""
    stripped = text.strip()
    if DIALOGUE_CLOSING_RE.search(stripped):
        return DIALOGUE_CLOSING
    if DIALOGUE_TOPIC_SHIFT_RE.search(stripped):
        return DIALOGUE_TOPIC_SHIFT
    if DIALOGUE_GRATITUDE_RE.search(stripped):
        return DIALOGUE_GRATITUDE
    if DIALOGUE_GREETING_RE.search(stripped):
        return DIALOGUE_GREETING
    if DIALOGUE_SELF_INTRODUCTION_RE.search(stripped):
        return DIALOGUE_SELF_INTRODUCTION

    kind = input_kind(stripped)
    if kind == KIND_QUESTION:
        return DIALOGUE_QUESTION
    if kind == KIND_COMMAND:
        return DIALOGUE_COMMAND
    if fact:
        return DIALOGUE_FACT
    if DIALOGUE_EMOTION_RE.search(stripped):
        return DIALOGUE_EMOTION
    if DIALOGUE_ACKNOWLEDGMENT_RE.match(stripped):
        return DIALOGUE_ACKNOWLEDGMENT
    if DIALOGUE_OPINION_RE.search(stripped):
        return DIALOGUE_OPINION
    return DIALOGUE_STATEMENT


def clean_topic(value: str) -> str:
    value = value.strip(" \t\r\n.,!?;:'\"")
    words = value.split()
    while words:
        leading_word = re_sub(r"(^[^\w'-]+|[^\w'-]+$)", "", words[0]).lower()
        if leading_word and leading_word not in DIALOGUE_TOPIC_LEADING_MODIFIERS and leading_word not in {"a", "an", "the"}:
            break
        words.pop(0)
    lowered = [word.lower() for word in words]
    if len(lowered) >= 3 and lowered[-3:] == ["for", "a", "while"]:
        words = words[:-3]
    elif len(lowered) >= 2 and lowered[-2:] in (["for", "now"], ["in", "general"]):
        words = words[:-2]
    while words and words[-1].lower() in DIALOGUE_TOPIC_TRAILERS:
        words.pop()
    if not words or len(words) > 8:
        result = ""
        return result
    topic = " ".join(words).strip(" \t\r\n.,!?;:'\"")
    topic_words = {word.lower() for word in re_findall(r"[\w'-]+", topic)}
    if not topic_words or topic_words & DIALOGUE_INVALID_TOPIC_WORDS:
        result = ""
        return result
    return topic


def explicit_topic(text: str) -> str:
    """Return a topic explicitly named by a topic-change/about construction."""
    match = DIALOGUE_TOPIC_SHIFT_RE.search(text.strip())
    if not match:
        about_match = re_search(r"\b(?:know|tell me|learn|think)\s+about\s+(.+)$", text, IGNORECASE)
        if about_match:
            result = clean_topic(about_match.group(1))
            return result
        for question_re in DIALOGUE_QUESTION_TOPIC_RES:
            question_match = question_re.match(text)
            if question_match:
                result = clean_topic(question_match.group(1))
                return result
        result = ""
        return result
    value = next((group for group in match.groups() if group), "")
    result = clean_topic(value)
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
    if previous_topic and topic_is_named(text, previous_topic):
        return previous_topic
    # ``fact`` is the extracted fact dictionary, or the empty tuple when the turn has none.
    fact_subject = fact.get("subject", "") if fact else ""
    if fact_subject and not DIALOGUE_DISCOURSE_TOPIC_PREFIX_RE.match(text):
        subject = clean_topic(str(fact_subject))
        subject_words = {word.lower() for word in re_findall(r"[\w'-]+", subject)}
        if subject and not subject_words & DIALOGUE_INVALID_TOPIC_WORDS:
            return subject
    for entity in reversed(entities or []):
        if entity.get("label", "") in {
            "FACILITY",
            "GPE",
            "GSP",
            "ORGANIZATION",
            "PERSON",
            "PROPER_NOUN",
            "SUBJECT",
            "TOPIC",
        }:
            candidate = clean_topic(str(entity.get("text", "")))
            if candidate:
                return candidate
    if previous_topic and DIALOGUE_REFERRING_RE.search(text):
        return previous_topic
    # No new name and no pronoun. Leave the topic unset so a statement cannot
    # carry a stale topic into the turns that follow.
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

    def add(value: str, label: str) -> bool:
        value = clean_topic(value)
        if not value:
            return False
        for existing in entities:
            if existing.get("text", "").casefold() != value.casefold():
                continue
            existing_priority = DIALOGUE_ENTITY_LABEL_PRIORITY.get(existing.get("label", ""), 0)
            if DIALOGUE_ENTITY_LABEL_PRIORITY.get(label, 0) > existing_priority:
                existing["label"] = label
                existing["text"] = value
            return False
        entities.append({"text": value, "label": label})
        return True

    fact_subject = fact.get("subject", "") if fact else ""
    if fact_subject:
        add(str(fact_subject), "SUBJECT")
    if topic:
        add(topic, "TOPIC")
    discourse_prefix = DIALOGUE_DISCOURSE_TOPIC_PREFIX_RE.match(text)
    for match in re_finditer(r"\b[A-Z][\w'-]*(?:\s+[A-Z][\w'-]*)*\b", text):
        if discourse_prefix and match.start() < discourse_prefix.end():
            continue
        original = match.group(0)
        opens_sentence = match.start() == 0 or not re_search(r"[A-Za-z0-9]", text[: match.start()])
        parts = original.split()
        while parts and parts[0].casefold() in DIALOGUE_ENTITY_LEADS:
            parts.pop(0)
        if not parts:
            continue
        value = " ".join(parts)
        sentence_initial = opens_sentence and value == original
        if not span_is_proper_noun(text, value, require_tag=sentence_initial):
            continue
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
    obj = str(fact.get("obj", "")).strip()
    if not subject or not obj:
        result = {"admitted": False, "reason": "missing_fields"}
        return result
    if DIALOGUE_DISCOURSE_TOPIC_PREFIX_RE.match(text):
        result = {"admitted": False, "reason": "discourse_subject"}
        return result
    if DIALOGUE_HEDGE_RE.search(text):
        result = {"admitted": False, "reason": "hedged"}
        return result
    if DIALOGUE_TRANSIENT_RE.search(text):
        result = {"admitted": False, "reason": "transient"}
        return result

    subject_tokens = [word.lower() for word in re_findall(r"[\w'-]+", subject)]
    # A punctuation-only subject names nothing a later question could ask about.
    if not subject_tokens:
        result = {"admitted": False, "reason": "nonlexical_subject"}
        return result
    subject_words = set(subject_tokens)
    object_words = {word.lower() for word in re_findall(r"[\w'-]+", obj)}
    if subject_tokens[0] in DIALOGUE_DISCOURSE_FACT_SUBJECT_LEADS:
        result = {"admitted": False, "reason": "discourse_subject"}
        return result
    if subject_tokens[0] in DIALOGUE_QUALIFIED_FACT_SUBJECT_LEADS:
        result = {"admitted": False, "reason": "qualified_subject"}
        return result
    if subject_words & DIALOGUE_META_FACT_WORDS:
        result = {"admitted": False, "reason": "meta_subject"}
        return result
    if subject_words & DIALOGUE_WH_TOPIC_WORDS:
        result = {"admitted": False, "reason": "wh_subject"}
        return result
    if subject_words & DIALOGUE_VAGUE_FACT_SUBJECTS:
        result = {"admitted": False, "reason": "vague_subject"}
        return result
    if object_words & DIALOGUE_PERSONAL_FACT_OBJECT_WORDS:
        result = {"admitted": False, "reason": "personal_object"}
        return result
    if object_words & {"temporary", "unknown", "unsure"}:
        result = {"admitted": False, "reason": "unstable_object"}
        return result
    result = {"admitted": True, "reason": "admitted"}
    return result


def topic_is_referenced(text: str, topic: str) -> bool:
    """Return whether a turn explicitly or pronominally continues a topic."""
    if not topic:
        result = False
        return result
    result = topic_is_named(text, topic) or bool(DIALOGUE_REFERRING_RE.search(text))
    return result


def topic_is_named(text: str, topic: str) -> bool:
    """Return whether *topic* occurs as a complete token sequence in *text*."""
    text_words = re_findall(r"[\w'-]+", text.casefold())
    topic_words = re_findall(r"[\w'-]+", topic.casefold())
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
    ``Alice``. The original statement is therefore required.
    """
    if not pattern or "*" in pattern or "_" in pattern or "{" in pattern:
        result = ""
        return result

    pattern_words = [word.casefold() for word in re_findall(r"[\w'-]+", pattern)]
    if not pattern_words:
        result = ""
        return result
    statement_words = list(re_finditer(r"[\w'-]+", statement_text))
    for start in range(len(statement_words) - len(pattern_words) + 1):
        matches = statement_words[start : start + len(pattern_words)]
        if [match.group(0).casefold() for match in matches] != pattern_words:
            continue
        surface = statement_text[matches[0].start() : matches[-1].end()]
        topic = clean_topic(surface)
        if topic:
            return topic
    result = ""
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
    # compliment, well-wish or further farewell. A later request genuinely
    # reopens the turn and takes precedence, until a closing after that
    # request ends the turn again.
    for index, candidate in enumerate(candidates):
        if candidate.get("dialogue_act", "") != DIALOGUE_CLOSING:
            continue
        selected = candidate
        for later in candidates[index + 1 :]:
            later_act = later.get("dialogue_act", "")
            reopens = later_act in {DIALOGUE_COMMAND, DIALOGUE_QUESTION, DIALOGUE_TOPIC_SHIFT}
            closes_again = later_act == DIALOGUE_CLOSING and selected.get("dialogue_act", "") != DIALOGUE_CLOSING
            if reopens or closes_again:
                selected = later
        return selected

    final = candidates[-1]
    if final.get("dialogue_act", "") not in {DIALOGUE_ACKNOWLEDGMENT, DIALOGUE_GRATITUDE, DIALOGUE_GREETING}:
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
        if candidate.get("dialogue_act", "") in substantive:
            return candidate
    return final
