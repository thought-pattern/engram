"""Fact learning for an Engram.

Extracted facts become one DYNAMIC or STATIC statement with subject and question
patterns, owned by the person who stated them or shared. Each function that takes
``engram`` is bound on ``engram.core.Engram`` as the method of the same name.
"""

from logging import getLogger as logging_getLogger

from engram.constants_artifacts import Tier
from engram.constants_language import MAX_PATTERN_WORDS
from engram.errors import InvalidRequestError, ResourceExhaustedError
from engram.facts_spacy import extract_facts
from engram.nlp import extract_fact, fact_query_patterns, fact_subject_upper
from engram.pattern import pattern_word_count
from engram.statement_store import visible_to

logger = logging_getLogger("engram.core")


def learn_fact_with_id(
    engram,
    fact: dict,
    introduced_by_user_id: str = "",
    source_label: str = "",
    tier: Tier = Tier.DYNAMIC,
) -> tuple[bool, str]:
    """Learn one extracted fact and return ``(learned, statement_id)``.

    When an identical fact already exists for the same owner, ``(False, existing_id)``
    is returned; when learning is declined (protected seed/hand-stored pattern or
    overlong subject), ``(False, "")`` is returned.
    """
    if not isinstance(introduced_by_user_id, str):
        raise ValueError("introduced_by_user_id must be a string")
    if not isinstance(source_label, str):
        raise ValueError("source_label must be a string")

    owner = introduced_by_user_id
    subject_pattern = fact_subject_upper(fact)
    if pattern_word_count(subject_pattern) > MAX_PATTERN_WORDS:
        logger.info("Not learning a fact whose subject is longer than %d words", MAX_PATTERN_WORDS)
        declined = (False, "")
        return declined
    with engram.mutation_lock, engram.statement_lock:
        if owner and engram.pattern_matcher.holds_category(subject_pattern):
            declined = (False, "")
            return declined
        carriers = [
            engram.statement_by_id.get(statement_id, {})
            for statement_id in engram.pattern_statements.get(subject_pattern, ())
            if statement_id in engram.statement_by_id
        ]
        for existing in carriers:
            if existing.get("introduced_by_user_id", "") != owner or existing.get("pattern", "") != subject_pattern:
                continue
            if existing.get("text", "") == fact.get("original", ""):
                unchanged = (False, existing.get("id", ""))
                return unchanged
            if existing.get("tier", Tier.STATIC) != Tier.DYNAMIC or subject_pattern not in engram.fact_subject_patterns(
                existing.get("text", "")
            ):
                declined = (False, "")
                return declined
        replaced_ids = set(engram.replaceable_statement_ids(subject_pattern, "", "", owner))

        aliases = []
        for query_pattern in fact_query_patterns(fact)[1:]:
            query_pattern = query_pattern.strip()
            claimed = {
                statement_id
                for statement_id in engram.pattern_statements.get(query_pattern, ())
                if visible_to(engram.statement_by_id.get(statement_id, {}), owner)
            }
            shadows_shared = bool(owner) and engram.pattern_matcher.holds_category(query_pattern)
            if (
                query_pattern
                and query_pattern != subject_pattern
                and pattern_word_count(query_pattern) <= MAX_PATTERN_WORDS
                and claimed <= replaced_ids
                and not shadows_shared
            ):
                aliases.append(query_pattern)
        aliases = list(dict.fromkeys(aliases))

        stored_id = engram.store(
            text=fact.get("original", ""),
            pattern=subject_pattern,
            pattern_aliases=aliases,
            tier=tier,
            keyword_source=fact.get("original", ""),
            introduced_by_user_id=introduced_by_user_id,
            source_label=source_label,
            replace_learned=True,
        )

    outcome = (True, stored_id)
    return outcome


def learn_fact(engram, fact: dict, introduced_by_user_id: str = "", source_label: str = "", tier: Tier = Tier.DYNAMIC) -> bool:
    """Learn a fact extracted from natural language."""
    learned, _ = learn_fact_with_id(
        engram,
        fact,
        introduced_by_user_id=introduced_by_user_id,
        source_label=source_label,
        tier=tier,
    )
    return learned


def fact_subject_patterns(engram, text: str) -> set[str]:
    """Return the subject patterns of the facts extracted from ``text``, as learning would."""
    facts = extract_facts(text) if engram.config.get("use_spacy_facts", False) else []
    if not facts:
        fact = extract_fact(text)
        facts = [fact] if fact else []
    result = {fact_subject_upper(fact) for fact in facts}
    return result


def store_unpatterned_shared_fact(engram, fact_text: str, source_label: str, tier: Tier) -> str:
    """Deduplicate against shared unpatterned statements by exact collapsed text and store if new."""
    collapsed_target = " ".join(fact_text.split())
    with engram.mutation_lock, engram.statement_lock:
        for stmt in engram.statements:
            if (
                not stmt.get("pattern", "")
                and not stmt.get("introduced_by_user_id", "")
                and " ".join(stmt.get("text", "").split()) == collapsed_target
            ):
                existing_id = stmt.get("id", "")
                return existing_id
        stored_id = engram.store(text=fact_text, tier=tier, introduced_by_user_id="", source_label=source_label)
    return stored_id


def add_fact(engram, text: str, source_label: object = "", tier: Tier = Tier.DYNAMIC) -> str:
    """Add shared knowledge without assigning it to a conversational user.

    The fact is indexed and, when extraction succeeds, receives the usual
    subject and question patterns. It never creates or updates user
    context. The returned id identifies the primary stored statement.
    """
    if not isinstance(text, str) or not text.strip():
        raise InvalidRequestError("fact text must be a non-empty string")
    if not isinstance(source_label, str):
        raise InvalidRequestError("source_label must be a string")

    fact_text = text.strip()
    facts = extract_facts(fact_text) if engram.config.get("use_spacy_facts", False) else []
    if not facts:
        fact = extract_fact(fact_text)
        facts = [fact] if fact else []

    if facts:
        stored_ids: list[str] = []
        batch_ids: set[str] = set()
        for fact in facts:
            subject_pattern = fact_subject_upper(fact)
            with engram.statement_lock:
                same_batch_subject = any(
                    statement_id in batch_ids and statement_id in engram.statement_by_id
                    for statement_id in engram.pattern_statements.get(subject_pattern, ())
                )
            if same_batch_subject:
                fallback_id = store_unpatterned_shared_fact(
                    engram,
                    fact.get("original", fact_text),
                    source_label,
                    tier,
                )
                if fallback_id:
                    stored_ids.append(fallback_id)
                    batch_ids.add(fallback_id)
                continue
            _, stmt_id = learn_fact_with_id(
                engram,
                fact,
                introduced_by_user_id="",
                source_label=source_label,
                tier=tier,
            )
            if not stmt_id:
                stmt_id = store_unpatterned_shared_fact(
                    engram,
                    fact.get("original", fact_text),
                    source_label,
                    tier,
                )
            if stmt_id:
                stored_ids.append(stmt_id)
                batch_ids.add(stmt_id)
        with engram.statement_lock:
            live_ids = [sid for sid in stored_ids if sid in engram.statement_by_id]
        if not live_ids:
            raise ResourceExhaustedError("dynamic statement capacity could not admit the fact")
        primary_id = live_ids[0]
        return primary_id

    result = store_unpatterned_shared_fact(engram, fact_text, source_label, tier)
    if not result:
        raise ResourceExhaustedError("dynamic statement capacity could not admit the fact")
    return result
