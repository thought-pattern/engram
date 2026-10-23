"""Keyword retrieval over the Engram statement store.

Lexical candidate discovery with optional spelling correction and synonym expansion,
scored and bounded by the request's working memory, and the accounted query built on it.
Each function that takes ``engram`` is bound on ``engram.core.Engram`` as the method of
the same name.
"""

from copy import deepcopy
from heapq import heappush as heapq_heappush, heapreplace as heapq_heapreplace

from engram import sessions as sessions_mod
from engram.constants_language import MAX_SPELL_EDIT_DISTANCE, MIN_SPELL_TOKEN_LENGTH
from engram.models import record_statement_query, session_touch
from engram.resources import estimate_working_bytes, require_working_memory, run_cooperative_check
from engram.scoring import score_statement_components
from engram.statement_store import visible_to
from engram.substitutions import expand_contractions, get_all_input_subs
from engram.text import correct_spelling, expand_query, expand_with_synonyms, get_synonyms, normalize


def query_candidates(
    engram,
    text: str,
    limit: int = 5,
    statement_filter=(),
    cooperative_check=(),
    max_working_memory_bytes: int = 0,
    owner: str = "",
) -> dict:
    """Discover lexical candidates without accounting or session mutation.

    Candidates are the statements visible to ``owner`` (``visible_to``):
    shared statements, plus that person's own facts when ``owner`` names a
    person. Spelling correction draws only on their words. Recency is
    scored against one read of the Engram clock for the whole call.
    """
    if not isinstance(owner, str):
        raise ValueError("owner must be a string")
    if limit < 1:
        raise ValueError("limit must be at least 1")
    if statement_filter and not callable(statement_filter):
        raise ValueError("statement_filter must be callable")
    if isinstance(max_working_memory_bytes, bool) or not isinstance(max_working_memory_bytes, int) or max_working_memory_bytes < 0:
        raise ValueError("max_working_memory_bytes must be a nonnegative integer")

    reference_time = engram.clock()
    run_cooperative_check(cooperative_check)
    normalized = normalize(text)
    uncorrected = normalized
    retained_bytes = 0
    phrase_keywords_enabled = bool(engram.config.get("use_phrase_keywords", False))
    if engram.config.get("use_spell_correction", False):
        # Correction can consult only store words within the largest edit
        # distance of a correctable token's length, so only that view is
        # copied, and it is charged to the request as it grows.
        lengths = {
            len(token) + offset
            for token in normalized.split()
            if len(token) >= MIN_SPELL_TOKEN_LENGTH
            for offset in range(-MAX_SPELL_EDIT_DISTANCE, MAX_SPELL_EDIT_DISTANCE + 1)
        }
        vocabulary: set[str] = set()
        retained_bytes = estimate_working_bytes(vocabulary)
        require_working_memory(retained_bytes, max_working_memory_bytes)
        with engram.statement_lock, engram.keyword_lock:
            for word, keyword_entry in engram.keywords.items():
                if len(word) not in lengths:
                    continue
                visible_word = any(
                    visible_to(engram.statement_by_id.get(statement_id, {}), owner)
                    for statement_id in keyword_entry.get("statement_ids", set())
                )
                if visible_word:
                    retained_bytes += estimate_working_bytes(word)
                    require_working_memory(retained_bytes, max_working_memory_bytes)
                    vocabulary.add(word)
        normalized = correct_spelling(normalized, vocabulary)

    keywords = engram.internal_extract_keywords(normalized)
    if not keywords:
        retained_bytes += estimate_working_bytes(keywords)
        require_working_memory(retained_bytes, max_working_memory_bytes)
        result = {
            "matches": [],
            "keywords": [],
            "resolved_query": text,
            "features": {},
            "diagnostics": {
                "spelling_correction_applied": normalized != uncorrected,
                "phrase_keywords_enabled": phrase_keywords_enabled,
                "synonym_expansion_count": 0,
                "working_memory_bytes": retained_bytes,
            },
        }
        return result

    search_keywords = keywords
    synonyms_by_keyword: dict[str, tuple] = {}
    if engram.config.get("use_synonyms", False):
        max_synonyms_per_word = engram.config.get("max_synonyms_per_word", 0)
        search_keywords = expand_with_synonyms(keywords, max_synonyms_per_word=max_synonyms_per_word)
        for keyword in keywords:
            run_cooperative_check(cooperative_check)
            synonyms = tuple(synonym for synonym in get_synonyms(keyword, max_synonyms=max_synonyms_per_word) if synonym != keyword)
            if synonyms:
                synonyms_by_keyword[keyword] = synonyms

    candidate_ids: set[str] = set()
    retained_bytes += (
        estimate_working_bytes(keywords) + estimate_working_bytes(search_keywords) + estimate_working_bytes(synonyms_by_keyword)
    )
    require_working_memory(retained_bytes, max_working_memory_bytes)
    with engram.keyword_lock:
        for keyword in search_keywords:
            run_cooperative_check(cooperative_check)
            for statement_id in engram.keywords.get(keyword, {}).get("statement_ids", set()):
                if statement_id in candidate_ids:
                    continue
                candidate_ids.add(statement_id)
                retained_bytes += estimate_working_bytes(statement_id)
                require_working_memory(retained_bytes, max_working_memory_bytes)
    synonym_expansion_count = sum(len(values) for values in synonyms_by_keyword.values())
    if not candidate_ids:
        result = {
            "matches": [],
            "keywords": keywords,
            "resolved_query": text,
            "features": {},
            "diagnostics": {
                "spelling_correction_applied": normalized != uncorrected,
                "phrase_keywords_enabled": phrase_keywords_enabled,
                "synonym_expansion_count": synonym_expansion_count,
                "working_memory_bytes": retained_bytes,
            },
        }
        return result

    scored: list[tuple[float, int, dict, dict[str, float]]] = []
    scored_bytes = 64
    with engram.statement_lock, engram.keyword_lock:
        total = len(engram.statements)
        for statement_id in candidate_ids:
            run_cooperative_check(cooperative_check)
            if statement_id not in engram.statement_by_id:
                continue
            # Store order breaks score ties, as the list position did. A stored
            # statement always has its sequence; store and eviction write both.
            index = engram.statement_sequence.get(statement_id, 0)
            statement_value = engram.statement_by_id.get(statement_id, {})
            if not visible_to(statement_value, owner):
                continue
            if statement_filter and not statement_filter(statement_value):
                continue
            components = score_statement_components(
                statement=statement_value,
                query_keywords=keywords,
                keyword_index=engram.keywords,
                total_statements=total,
                weight_base=engram.config.get("weight_base", 0.0),
                weight_recency=engram.config.get("weight_recency", 0.0),
                weight_hit_rate=engram.config.get("weight_hit_rate", 0.0),
                recency_half_life_seconds=engram.config.get("recency_half_life_seconds", 0.0),
                synonyms=synonyms_by_keyword,
                current_time=reference_time,
            )
            score = components.get("score", 0.0)
            if score > 0:
                ranked = (score, index, statement_value, components)
                if len(scored) < limit:
                    ranked_bytes = estimate_working_bytes(ranked)
                    heapq_heappush(scored, ranked)
                    scored_bytes += ranked_bytes
                elif (score, index) > (scored[0][0], scored[0][1]):
                    ranked_bytes = estimate_working_bytes(ranked)
                    removed = heapq_heapreplace(scored, ranked)
                    scored_bytes += ranked_bytes - estimate_working_bytes(removed)
                require_working_memory(retained_bytes + scored_bytes, max_working_memory_bytes)
    scored.sort(key=lambda value: (value[0], value[1]), reverse=True)
    run_cooperative_check(cooperative_check)
    result = {
        "matches": [(statement_value, score) for score, _, statement_value, _ in scored],
        "keywords": keywords,
        "resolved_query": text,
        "features": {statement_value.get("id", ""): components for _, _, statement_value, components in scored},
        "diagnostics": {
            "spelling_correction_applied": normalized != uncorrected,
            "phrase_keywords_enabled": phrase_keywords_enabled,
            "synonym_expansion_count": synonym_expansion_count,
            "working_memory_bytes": retained_bytes + scored_bytes,
        },
    }
    return result


def query(
    engram,
    text: str,
    context_id: str = "",
    limit: int = 5,
    statement_filter=(),
    record_candidates: bool = True,
    owner: str = "",
) -> dict:
    """Retrieve matching statements.

    Args:
        text: Query text.
        context_id: Optional conversation context for query expansion.
        limit: Maximum results (default: 5).
        statement_filter: Optional predicate applied before scoring and
            candidacy accounting. Intended for integrations that isolate
            caller-owned response-cache scopes.
        record_candidates: Record query accounting for returned matches.
            Regulated proposal retrieval disables this until keyword and
            vector candidates have been merged.
        owner: The person whose chat is asking. Their own facts are
            candidates alongside shared statements; no other person's
            facts ever are. Empty retrieves shared statements only.

    Returns:
        Dict with "matches" (list of (statement, score) pairs) and
        "keywords" (extracted query keywords). "resolved_query" is the
        context-expanded query used as the cache key.
    """
    if limit < 1:
        raise ValueError("limit must be at least 1")
    if statement_filter and not callable(statement_filter):
        raise ValueError("statement_filter must be callable")
    if not isinstance(record_candidates, bool):
        raise ValueError("record_candidates must be a boolean")
    with engram.count_lock:
        engram.query_count += 1

    # Get session context if provided. The session is created when missing,
    # matching pattern_query -- a fresh session id must not silently skip
    # context tracking.
    expanded_text = text
    if engram.config.get("expand_contractions", False):
        expanded_text = expand_contractions(expanded_text, get_all_input_subs(engram.substitution_maps))
    if context_id:
        with engram.session_lock:
            session = sessions_mod.get_session(engram, context_id, create_if_missing=True)
            if session:
                session_touch(session, engram.clock())
                expanded_text = expand_query(expanded_text, session.get("previous_response", ""))

    discovery = engram.query_candidates(expanded_text, limit=limit, statement_filter=statement_filter, owner=owner)
    keywords = discovery.get("keywords", [])
    matches = discovery.get("matches", [])
    with engram.keyword_lock:
        for keyword in keywords:
            if keyword in engram.keywords:
                keyword_entry = engram.keywords.get(keyword, {})
                keyword_entry["query_count"] = keyword_entry.get("query_count", 0) + 1
    with engram.statement_lock:
        if record_candidates:
            for statement_value, _ in matches:
                record_statement_query(statement_value)
        # Callers receive isolated statements; the store's own records stay owned here.
        isolated_matches = [(deepcopy(statement_value), score) for statement_value, score in matches]
    result = {"matches": isolated_matches, "keywords": keywords, "resolved_query": discovery.get("resolved_query", "")}
    return result
