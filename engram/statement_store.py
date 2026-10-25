"""The Engram statement store.

Storing statements with their keyword index and pattern registrations, per-person
matchers, store-order positions, hit statistics, and removal and least-recently-used
eviction of DYNAMIC statements. Each function that takes ``engram`` is bound on
``engram.core.Engram`` as the method of the same name.
"""

from bisect import bisect_left
from copy import deepcopy
from heapq import heappop as heapq_heappop, heappush as heapq_heappush, heapreplace as heapq_heapreplace

from engram.constants import DEFAULT_CAPACITY, EARLIEST_UTC
from engram.constants_artifacts import Tier
from engram.constants_language import MAX_PATTERN_WORDS
from engram.errors import ResourceExhaustedError
from engram.models import record_statement_hit, statement
from engram.pattern import PatternMatcher, pattern_word_count
from engram.text import extract_keywords, extract_keywords_spacy, normalize


def lru_entry(stmt: dict, sequence: int) -> tuple:
    """Return a DYNAMIC statement's LRU heap entry: its eviction key, then its id.

    The key is (last used, whether it was ever hit, store order). Equal
    timestamps keep store order, as list positions did.
    """
    hit = stmt.get("last_hit_available", False)
    last_used = stmt.get("last_hit", EARLIEST_UTC) if hit else stmt.get("created_at", EARLIEST_UTC)
    result = (last_used, 1 if hit else 0, sequence, stmt.get("id", ""))
    return result


def visible_to(stmt: dict, owner: str) -> bool:
    """Return whether a statement may answer ``owner``'s chat.

    Shared statements (no introduced_by_user_id) answer everyone. A fact a
    person taught answers only that person; an empty owner sees shared
    statements alone.
    """
    statement_owner = stmt.get("introduced_by_user_id", "")
    result = not statement_owner or statement_owner == owner
    return result


def internal_extract_keywords(engram, normalized_text: str) -> list:
    """Extract keywords using the configured extractor (token or phrase).

    Both store-time indexing and query-time retrieval go through here so the
    keyword index and queries always use the same extraction.
    """
    stopwords = engram.config.get("stopwords", set())
    if engram.config.get("use_phrase_keywords", False):
        phrase_keywords = extract_keywords_spacy(normalized_text, stopwords)
        return phrase_keywords
    token_keywords = extract_keywords(normalized_text, stopwords)
    return token_keywords


def store(
    engram,
    text: str,
    tier: Tier = Tier.DYNAMIC,
    statement_id: str = "",
    pattern: str = "",
    pattern_aliases=(),
    that: str = "",
    topic: str = "",
    template=(),
    priority: int = 0,
    keyword_source: str = "",
    introduced_by_user_id: str = "",
    source_label: str = "",
    replace_learned: bool = False,
) -> str:
    """Add a statement to the store.

    Args:
        text: Statement content (response text for plain templates).
        tier: STATIC or DYNAMIC (default).
        statement_id: Optional specific ID.
        pattern: Optional pattern for matching (AIML-style).
        pattern_aliases: Optional alternate patterns that resolve to the
            same stored statement without duplicating its content.
        that: Optional pattern for bot's previous response.
        topic: Optional topic scope.
        template: Optional structured template (JSON/dict).
        priority: Optional priority override (added to the calibrated
            keyword score; preferred among equal pattern matches).
        keyword_source: Optional text to index the conversational statement
            under instead of its pattern or rendered text.
        introduced_by_user_id: The person who taught the statement in chat.
            A taught statement belongs to that person and answers only
            their chat (``visible_to``); empty stores a shared statement.
        source_label: Optional non-user source label.
        replace_learned: When True, a DYNAMIC statement replaces the DYNAMIC
            statements of the same owner already filed under the same
            pattern, that, and topic, so re-teaching changes the answer.
            Learning uses it; STATIC statements are never replaced.

    Returns:
        Assigned statement ID.
    """
    if not isinstance(introduced_by_user_id, str):
        raise ValueError("introduced_by_user_id must be a string")
    if not isinstance(source_label, str):
        raise ValueError("source_label must be a string")
    if not pattern_aliases:
        aliases: list[str] = []
    elif isinstance(pattern_aliases, (list, tuple)) and all(isinstance(alias, str) for alias in pattern_aliases):
        aliases = list(dict.fromkeys(alias.strip() for alias in pattern_aliases if alias.strip()))
    else:
        raise ValueError("pattern_aliases must be a sequence of strings")
    if aliases and not pattern:
        raise ValueError("pattern_aliases require a primary pattern")
    aliases = [alias for alias in aliases if alias != pattern]
    # The matcher refuses an over-long entry only when it is added. Checking
    # every entry first keeps a refused alias from leaving earlier entries
    # registered, or replaced statements evicted, without a stored statement.
    if pattern:
        for name, value in (*(("pattern", entry) for entry in [pattern, *aliases]), ("that", that), ("topic", topic)):
            if value and pattern_word_count(value) > MAX_PATTERN_WORDS:
                raise ValueError(f"{name} exceeds the limit of {MAX_PATTERN_WORDS} words")

    # Index under keyword_source when given, else the pattern, else the text
    source = keyword_source or pattern or text
    normalized = normalize(source)
    keywords = engram.internal_extract_keywords(normalized)

    stmt = statement(
        text=text,
        statement_id=statement_id or f"stmt_{engram.identifier_source()}",
        created_at=engram.clock(),
        tier=tier,
        keywords=keywords,
        pattern=pattern or "",
        pattern_aliases=aliases,
        that=that or "",
        topic=topic or "",
        template=template,
        priority=priority,
        introduced_by_user_id=introduced_by_user_id,
        source_label=source_label,
    )
    stmt_id = stmt.get("id", "")
    with engram.mutation_lock, engram.statement_lock, engram.keyword_lock:
        if stmt_id in engram.statement_by_id:
            raise ValueError(f"duplicate statement id: {stmt_id}")

        if replace_learned and pattern and tier == Tier.DYNAMIC:
            for replaced_id in engram.replaceable_statement_ids(pattern, that or "", topic or "", introduced_by_user_id):
                engram.evict_statement(replaced_id)

        # Capacity is made before anything is registered, so a refusal leaves no partial statement.
        if tier == Tier.DYNAMIC:
            while len(engram.dynamic_statement_ids) >= engram.config.get("capacity", DEFAULT_CAPACITY):
                if not engram.evict_least_recent_dynamic():
                    raise ResourceExhaustedError("DYNAMIC statement capacity is full and no statement can be evicted")

        # Register the pattern under the same lock that guards matching,
        # so a concurrent pattern_query never sees a half-updated matcher.
        if pattern:
            matcher = engram.owned_matcher(introduced_by_user_id)
            for registered_pattern in [pattern, *aliases]:
                matcher.add_pattern(registered_pattern, text, that=that or "", topic=topic or "")

        sequence = engram.next_statement_sequence
        engram.next_statement_sequence += 1
        engram.statements.append(stmt)
        engram.statement_sequences.append(sequence)
        engram.statement_by_id[stmt_id] = stmt
        engram.statement_sequence[stmt_id] = sequence
        if tier == Tier.DYNAMIC:
            engram.dynamic_statement_ids.add(stmt_id)
            heapq_heappush(engram.dynamic_lru, lru_entry(stmt, sequence))
        if pattern:
            for registered_pattern in [pattern, *aliases]:
                engram.pattern_statements.setdefault(registered_pattern, []).append(stmt_id)
        for kw in keywords:
            keyword_entry = engram.keywords.setdefault(
                kw, {"keyword": kw, "statement_ids": set(), "query_count": 0, "hit_count": 0}
            )
            keyword_entry.setdefault("statement_ids", set()).add(stmt_id)

    result = stmt_id
    return result


def replaceable_statement_ids(engram, pattern: str, that: str, topic: str, owner: str) -> list[str]:
    """Return ``owner``'s learned (DYNAMIC) statements filed under exactly this pattern, that, and topic.

    An empty owner means the shared statements; one person's teaching never replaces another's.
    """
    with engram.statement_lock:
        result = []
        for statement_id in engram.pattern_statements.get(pattern, ()):
            # pattern_statements lists only stored statements (store and eviction keep them in step).
            existing = engram.statement_by_id.get(statement_id, {})
            if (
                existing.get("tier", "") == Tier.DYNAMIC
                and existing.get("introduced_by_user_id", "") == owner
                and existing.get("pattern", "") == pattern
                and existing.get("that", "") == that
                and existing.get("topic", "") == topic
            ):
                result.append(statement_id)
        return result


def owned_matcher(engram, owner: str) -> PatternMatcher:
    """Return the matcher that files ``owner``'s categories, creating a person's on first use.

    Shared statements (empty owner) live in ``pattern_matcher``. A person's
    matcher takes the shared word sets, bot properties, and fallback
    settings. The caller holds statement_lock.
    """
    if not owner:
        result = engram.pattern_matcher
        return result
    if owner not in engram.person_matchers:
        engram.person_matchers[owner] = PatternMatcher(
            sets=engram.pattern_matcher.word_sets(),
            bot_properties=engram.pattern_matcher.bot_properties(),
            use_stemming=engram.config.get("use_stemming", False),
            use_lemmatization=engram.config.get("use_lemmatization", False),
            use_spacy_lemmatization=engram.config.get("use_spacy_lemmatization", False),
        )
    result = engram.person_matchers.get(owner, engram.pattern_matcher)
    return result


def statement_position(engram, statement_id: str) -> int:
    """Return a stored statement's position in ``statements``.

    Callers pass the id of a stored statement; any other id is refused
    rather than resolved to a fabricated position.
    """
    if statement_id not in engram.statement_sequence:
        raise ValueError(f"statement {statement_id!r} is not stored")
    result = bisect_left(engram.statement_sequences, engram.statement_sequence.get(statement_id, 0))
    return result


def get_statement(engram, statement_id: str) -> dict:
    """Get a statement by ID.

    Args:
        statement_id: Statement ID.

    Returns:
        An isolated copy of the statement dict if found, {} otherwise.
    """
    with engram.statement_lock:
        result = deepcopy(engram.statement_by_id.get(statement_id, {}))
    return result


def retire_statement(engram, statement_id: str) -> bool:
    """Remove a statement from the store by id.

    Unlike capacity eviction, this is a deliberate removal of a specific
    conversational statement. ``evict_statement`` cleans the keyword index,
    the pattern matcher, and the pattern map alongside the statement itself.
    Returns False when no statement carries the id.

    Args:
        statement_id: The id of the statement to remove.

    Returns:
        True if a statement was removed, False if the id was not present.
    """
    with engram.mutation_lock, engram.statement_lock:
        if statement_id not in engram.statement_by_id:
            result = False
            return result
        engram.evict_statement(statement_id)
        result = True
        return result


def forget_person_facts(engram, owner: str) -> int:
    """Remove every statement ``owner`` taught in chat; return how many went.

    Shared statements are untouched. The anonymous conversation uses this
    so what an anonymous speaker said ends with their conversation.
    """
    if not isinstance(owner, str) or not owner:
        raise ValueError("owner must name a person")
    with engram.mutation_lock, engram.statement_lock:
        owned = [stmt.get("id", "") for stmt in engram.statements if stmt.get("introduced_by_user_id", "") == owner]
        for statement_id in owned:
            engram.evict_statement(statement_id)
    result = len(owned)
    return result


def require_store_locks(engram) -> None:
    """Refuse a statement-store change unless the caller holds mutation_lock and statement_lock."""
    if not (engram.mutation_lock.held_by_current_thread() and engram.statement_lock.held_by_current_thread()):
        raise RuntimeError("statement store changes require mutation_lock and statement_lock")


def evict_statement(engram, statement_id: str) -> None:
    """Remove one stored statement and every index entry that refers to it.

    The caller holds ``mutation_lock`` and ``statement_lock`` and passes the id of a
    stored statement. Positions come from the store-order sequence, so nothing is
    renumbered; the statement's LRU heap entry is dropped when it reaches the top.
    """
    engram.require_store_locks()
    position = engram.statement_position(statement_id)
    stmt = engram.statements[position]
    engram.detach_statement(stmt)
    engram.statements.pop(position)
    engram.statement_sequences.pop(position)
    del engram.statement_by_id[statement_id]
    del engram.statement_sequence[statement_id]
    engram.dynamic_statement_ids.discard(statement_id)
    engram.eviction_count += 1


def detach_statement(engram, stmt: dict) -> None:
    """Remove a statement's keywords and pattern registrations, leaving the statement list alone.

    The caller holds ``mutation_lock`` and ``statement_lock``. store() registered one
    entry per pattern in the owner's matcher, so exactly one goes; a surviving
    statement carrying the same pattern keeps its own entry. A person's matcher goes
    with their last category.
    """
    engram.require_store_locks()
    stmt_id = stmt.get("id", "")
    with engram.keyword_lock:
        for keyword in stmt.get("keywords", []):
            if keyword in engram.keywords:
                statement_ids = engram.keywords.get(keyword, {}).get("statement_ids", set())
                statement_ids.discard(stmt_id)
                if not statement_ids:
                    del engram.keywords[keyword]
    registered_patterns = [pattern for pattern in [stmt.get("pattern", ""), *stmt.get("pattern_aliases", [])] if pattern]
    if registered_patterns:
        owner = stmt.get("introduced_by_user_id", "")
        matcher = engram.owned_matcher(owner)
        for pattern in registered_patterns:
            matcher.remove_pattern(pattern, that=stmt.get("that", ""), topic=stmt.get("topic", ""))
        if owner and not matcher:
            del engram.person_matchers[owner]
    for pattern in registered_patterns:
        carriers = engram.pattern_statements.get(pattern, [])
        if stmt_id in carriers:
            carriers.remove(stmt_id)
            if not carriers:
                del engram.pattern_statements[pattern]


def evict_least_recent_dynamic(engram) -> bool:
    """Evict the least-recently-used DYNAMIC statement; return False when there is none.

    The caller holds ``mutation_lock`` and ``statement_lock``. The heap holds one entry
    per DYNAMIC statement, recorded when it was stored or last checked. Hits only move a
    statement's key later, so an entry whose key is still current at the top of the heap
    is the true minimum; a stale entry is refreshed and pushed back, and an entry for a
    removed statement is dropped.
    """
    engram.require_store_locks()
    heap = engram.dynamic_lru
    while heap:
        entry = heap[0]
        statement_id = entry[3]
        sequence = entry[2]
        live = (
            statement_id in engram.dynamic_statement_ids
            and statement_id in engram.statement_sequence
            and engram.statement_sequence.get(statement_id, 0) == sequence
        )
        if not live:
            heapq_heappop(heap)
            continue
        current = lru_entry(engram.statement_by_id.get(statement_id, {}), sequence)
        if current != entry:
            heapq_heapreplace(heap, current)
            continue
        heapq_heappop(heap)
        engram.evict_statement(statement_id)
        result = True
        return result
    result = False
    return result


def record_hit(engram, keywords: list[str], statement_id: str = "") -> None:
    """Update statistics after successful retrieval.

    Args:
        keywords: Query keywords that led to a hit.
        statement_id: Optional id of the statement that answered the query.
            When given, that statement's own hit statistics are updated too,
            which is what least-recently-used eviction reads.
    """
    with engram.count_lock:
        engram.hit_count += 1
    with engram.keyword_lock:
        for kw in keywords:
            if kw in engram.keywords:
                entry = engram.keywords.get(kw, {})
                entry["hit_count"] = entry.get("hit_count", 0) + 1
    if statement_id:
        with engram.statement_lock:
            if statement_id in engram.statement_by_id:
                record_statement_hit(engram.statement_by_id.get(statement_id, {}), engram.clock())
