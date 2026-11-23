"""Fact learning for an Engram.

Extracted facts become one DYNAMIC or STATIC statement with subject and question
patterns, owned by the person who stated them or shared. Each function that takes
``engram`` is bound on ``engram.core.Engram`` as the method of the same name.
"""

from logging import getLogger as logging_getLogger

from engram.constants_artifacts import Tier
from engram.constants_language import MAX_PATTERN_WORDS
from engram.errors import InvalidRequestError
from engram.facts_spacy import extract_facts
from engram.nlp import extract_fact, fact_query_patterns, fact_subject_upper
from engram.pattern import pattern_word_count
from engram.statement_store import visible_to
from engram.text import normalize

logger = logging_getLogger("engram.core")


def learn_fact(engram, fact: dict, introduced_by_user_id: str = "", source_label: str = "", tier: Tier = Tier.DYNAMIC) -> bool:
    """Learn a fact extracted from natural language.

    Creates patterns for the subject and common query forms so the fact
    can be retrieved later.

    Args:
        fact: The extracted fact to learn.
        introduced_by_user_id: The person who stated the fact in chat. The
            fact belongs to them and answers only their chat; empty learns
            shared knowledge.
        source_label: Optional non-user source label.
        tier: Storage tier for the generated statements.

    Returns:
        True if the fact was learned. False if it is already known, if the
        subject's pattern belongs to a seed or hand-stored statement, if a
        person's subject is one a shared statement registers, or if the
        subject is longer than MAX_PATTERN_WORDS. A new fact about a subject
        replaces the fact its owner already taught about it.
    """
    if not isinstance(introduced_by_user_id, str):
        raise ValueError("introduced_by_user_id must be a string")
    if not isinstance(source_label, str):
        raise ValueError("source_label must be a string")

    # Check and store under the re-entrant mutation and statement locks so
    # concurrent speakers cannot admit duplicate copies of the same fact.
    # store() takes them in this order too.
    owner = introduced_by_user_id
    subject_pattern = fact_subject_upper(fact)
    if pattern_word_count(subject_pattern) > MAX_PATTERN_WORDS:
        logger.info("Not learning a fact whose subject is longer than %d words", MAX_PATTERN_WORDS)
        result = False
        return result
    with engram.mutation_lock, engram.statement_lock:
        # A person's fact never shadows a shared category in their chat.
        if owner and engram.pattern_matcher.holds_category(subject_pattern):
            result = False
            return result
        carriers = [
            engram.statement_by_id.get(statement_id, {})
            for statement_id in engram.pattern_statements.get(subject_pattern, ())
            if statement_id in engram.statement_by_id
        ]
        for existing in carriers:
            if existing.get("introduced_by_user_id", "") != owner or existing.get("pattern", "") != subject_pattern:
                continue
            # Restating a fact changes nothing, and only a learned fact about
            # this subject is replaced: never a seed or hand-stored statement.
            if (
                existing.get("tier", Tier.STATIC) != Tier.DYNAMIC
                or existing.get("text", "") == fact.get("original", "")
                or subject_pattern not in engram.fact_subject_patterns(existing.get("text", ""))
            ):
                result = False
                return result
        replaced_ids = set(engram.replaceable_statement_ids(subject_pattern, "", "", owner))

        # Store one fact statement with alternate retrieval patterns.
        # Aliases live in the matcher and map to this one statement, so a
        # fact does not consume capacity once per query phrasing. The
        # statement being replaced gives up its aliases to the new one; an
        # alias a shared statement or another of the owner's facts already
        # registers is left to it.
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

        # Keyword-index under the original sentence so retrieval sees the
        # fact's content ("Is the sky blue?" needs [sky, blue], not just
        # [sky]). store() safely re-enters statement_lock here.
        engram.store(
            text=fact.get("original", ""),
            pattern=subject_pattern,
            pattern_aliases=aliases,
            tier=tier,
            keyword_source=fact.get("original", ""),
            introduced_by_user_id=introduced_by_user_id,
            source_label=source_label,
            replace_learned=True,
        )

    result = True
    return result


def fact_subject_patterns(engram, text: str) -> set[str]:
    """Return the subject patterns of the facts extracted from ``text``, as learning would."""
    facts = extract_facts(text) if engram.config.get("use_spacy_facts", False) else []
    if not facts:
        fact = extract_fact(text)
        facts = [fact] if fact else []
    result = {fact_subject_upper(fact) for fact in facts}
    return result


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
        primary_pattern = fact_subject_upper(facts[0])
        for fact in facts:
            engram.learn_fact(fact, introduced_by_user_id="", source_label=source_label, tier=tier)
        with engram.statement_lock:
            shared_carriers = [
                statement_id
                for statement_id in engram.pattern_statements.get(primary_pattern, ())
                if statement_id in engram.statement_by_id
                and not engram.statement_by_id.get(statement_id, {}).get("introduced_by_user_id", "")
            ]
            result = shared_carriers[-1] if shared_carriers else ""
            return result

    normalized_text = normalize(fact_text)
    with engram.statement_lock:
        for stmt in engram.statements:
            if not stmt.get("pattern", "") and normalize(stmt.get("text", "")) == normalized_text:
                result = stmt.get("id", "")
                return result

    result = engram.store(text=fact_text, tier=tier, introduced_by_user_id="", source_label=source_label)
    return result
