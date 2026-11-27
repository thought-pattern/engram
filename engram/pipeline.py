"""Tiered response pipeline for ENGRAM.

Packages the conversational strategy so callers do not have to hand-roll it:
scripted pattern match first, then a high-confidence conversational statement,
then the caller's LLM with retrieved context. Generated responses are not stored
as matcher statements. Scores are
calibrated 0.0 - 1.0, so the confidence threshold means the same thing for
every query.

Usage:

    from engram import pipeline

    def my_llm(text, context_statements):
        prompt = "\\n".join(context_statements) + "\\n\\n" + text
        return call_llm(prompt)

    result = pipeline.respond(engram, "What are the support hours?",
                              context_id=context_id, llm_fn=my_llm)
    print(result["source"], result["response"])
"""

from math import isfinite as math_isfinite

from engram import sessions as sessions_mod
from engram.constants import DEFAULT_HUMAN_ID, DEFAULT_PIPELINE_CONTEXT_LIMIT, DEFAULT_PIPELINE_HIGH_CONFIDENCE
from engram.constants_artifacts import MAX_RESPONSE_BYTES
from engram.constants_language import QUESTION_WORDS
from engram.pattern import PatternMatcher
from engram.validation import Characters, require_text


def attach_dialogue_state(engram, result: dict, session_id: str) -> dict:
    """Return a copy of an API result carrying the session's latest discourse state.

    Without a session the copy keeps the result's own discourse fields.
    """
    attached = dict(result)
    unknown = attached.get("unknown", False)
    if type(unknown) is not bool:
        raise ValueError("conversation unknown must be a Boolean")
    attached["unknown"] = unknown
    if not session_id:
        return attached
    with engram.session_lock:
        session = sessions_mod.get_session(engram, session_id, create_if_missing=False)
        if session:
            history = session.get("dialogue_act_history", [])
            attached["dialogue_act"] = history[0] if history else ""
            attached["active_topic"] = session.get("active_topic", "")
            attached["entities"] = list(session.get("entities", []))
            attached["fact_admissions"] = list(session.get("last_fact_admissions", []))
    return attached


def retract_response(engram, session_id: str, response: str) -> bool:
    """Remove a recorded, not-yet-shown response from the session.

    pattern_query records its response into the session before the pipeline
    decides whether graph recall answers instead. Left in place, the replaced
    response would poison previous_response and linger as a phantom history
    entry. Retraction restores previous_response to the prior turn's answer.
    """
    if not session_id or not response:
        return False
    with engram.session_lock:
        session = sessions_mod.get_session(engram, session_id, create_if_missing=False)
        if not session:
            return False
        history = session.get("response_history", [])
        if history and history[0] == response:
            history.pop(0)
            that_history = session.get("that_history", [])
            if that_history:
                that_history.pop(0)
        if session.get("previous_response", "") == response:
            session["previous_response"] = history[0] if history else ""
    return True


def update_session(engram, session_id: str, response: str) -> bool:
    """Record a response into the session context, creating the session if needed."""
    if not session_id:
        return False
    with engram.session_lock:
        sessions_mod.get_session(engram, session_id, create_if_missing=True)
        sessions_mod.update_session_context(engram, session_id, response)
    return True


def statement_scope_satisfied(engram, stmt: dict, current_that: str, current_topic: str) -> bool:
    """Return whether a conversational statement's ``topic`` and ``that`` scope matches the session."""
    stmt_topic = stmt.get("topic", "")
    stmt_that = stmt.get("that", "")
    if not stmt_topic.strip() and not stmt_that.strip():
        return True
    if stmt_topic.strip() and not current_topic.strip():
        return False
    if stmt_that.strip() and not current_that.strip():
        return False
    matcher = PatternMatcher(
        sets=engram.pattern_matcher.word_sets(),
        bot_properties=engram.pattern_matcher.bot_properties(),
        use_stemming=engram.config.get("use_stemming", False),
        use_lemmatization=engram.config.get("use_lemmatization", False),
        use_spacy_lemmatization=engram.config.get("use_spacy_lemmatization", False),
    )
    matcher.add_pattern("*", "ok", that=stmt_that, topic=stmt_topic)
    matched = bool(
        matcher.match(
            "x",
            that=engram.expand_for_match(current_that),
            topic=engram.expand_for_match(current_topic),
        )
    )
    return matched


def respond(
    engram,
    text: str,
    context_id: str = "",
    llm_fn=(),
    high_confidence: object = DEFAULT_PIPELINE_HIGH_CONFIDENCE,
    context_limit: object = DEFAULT_PIPELINE_CONTEXT_LIMIT,
    human_id: str = "",
    evaluation_time: str = "",
) -> dict:
    """Answer text through the conversational strategy: pattern, statement, then LLM.

    1. Pattern match -- a matched category answers, including a pure wildcard.
       The match updates the session context itself. The store's configured
       fallback_response does not count as a pattern answer; it must not
       preempt conversational retrieval or the LLM.
    2. Confident statement -- when no category matches and the top calibrated
       retrieval score reaches high_confidence, the conversational statement
       answers directly and the hit is recorded.
    3. LLM -- llm_fn(text, context_statements) is called with the top
       retrieved statement texts as context. A non-empty response is recorded
       only in the current session.

    When nothing above answers, source "none" is returned along with the
    retrieval matches so the caller can decide what to do.

    Args:
        engram: Engram instance.
        text: Human input text.
        context_id: Optional conversation context for turn state.
        llm_fn: Optional callable (text, context_statements) -> response str.
            Its response must be text of at most MAX_RESPONSE_BYTES.
        high_confidence: Calibrated score at or above which a conversational statement
            answers without the LLM (a finite number from 0.0 to 1.0).
        context_limit: Maximum retrieved statements passed to llm_fn (a
            non-negative integer).
        human_id: The speaker; empty means the anonymous human "0". Facts the
            turn teaches belong to the speaker, and no other human's facts
            answer or reach llm_fn.
        evaluation_time: Optional canonical UTC timestamp used by graph
            recall for this turn.

    Returns:
        Pipeline result dict. source is "pattern" (scripted match), "graph"
        (read-only graph recall), "statement" (confident conversational-statement
        retrieval), "llm" (generated via llm_fn), or "none" (nothing confident
        and no llm_fn). matches and keywords carry the keyword retrieval
        outcome so a "none" caller can still inspect what was found; pattern
        and captured carry the pattern-match outcome for debugging ("" / []
        off the pattern path). human_id is the turn's context, and
        dialogue_act, active_topic, entities and fact_admissions carry that
        session's latest discourse state ("" / [] without a session).
    """
    if (
        isinstance(high_confidence, bool)
        or not isinstance(high_confidence, (int, float))
        or not math_isfinite(high_confidence)
        or not 0 <= high_confidence <= 1
    ):
        raise ValueError("high_confidence must be a number between 0 and 1")
    if isinstance(context_limit, bool) or not isinstance(context_limit, int) or context_limit < 0:
        raise ValueError("context_limit must be a non-negative integer")

    # Tier 1: a matched category, including a pure wildcard, or graph recall.
    # The configured fallback text is not a category answer.
    speaker = sessions_mod.normalize_human_id(human_id)
    with engram.statement_lock:
        turn_start_sequence = engram.next_statement_sequence
    current_that = ""
    current_topic = ""
    if context_id:
        with engram.session_lock:
            existing_session = sessions_mod.get_session(engram, context_id, create_if_missing=False)
            if existing_session:
                current_that = existing_session.get("previous_response", "")
                current_topic = existing_session.get("predicates", {}).get("topic", "")

    graph_config = engram.config.get("graph", {})
    graph_enabled = isinstance(graph_config, dict) and bool(graph_config.get("enabled", False))
    pattern_result = engram.pattern_query(
        text,
        context_id=context_id,
        human_id=human_id,
        include_graph=graph_enabled,
        include_fallback=False,
        evaluation_time=evaluation_time,
    )
    stmt = {}
    captured = []
    response = ""
    if pattern_result:
        stmt, captured, response = pattern_result
        matched_pattern = stmt.get("pattern", "")
        is_fallback = not stmt and response == engram.config.get("fallback_response", "")
        if response and not is_fallback:
            source = "pattern" if stmt else "graph"
            tier1 = {
                "response": response,
                "unknown": stmt.get("unknown", False),
                "source": source,
                "score": 1.0,
                "matches": [],
                "keywords": [],
                "pattern": matched_pattern,
                "captured": list(captured),
                "human_id": context_id,
                "dialogue_act": "",
                "active_topic": "",
                "entities": [],
                "fact_admissions": [],
            }
            result = attach_dialogue_state(engram, tier1, context_id)
            return result

    # Tier 2: confident conversational statement via keyword retrieval. Question words
    # carry intent, not content -- a keyword set with no content words ("why
    # why why") is no evidence, however perfectly it overlaps something. Only
    # shared statements and the speaker's own facts stored before this turn and
    # matching the active topic/that scope are candidates.
    def eligible_conversational_statement(candidate_stmt: dict) -> bool:
        stmt_id = candidate_stmt.get("id", "")
        if engram.statement_sequence.get(stmt_id, 0) >= turn_start_sequence:
            return False
        scope_ok = statement_scope_satisfied(engram, candidate_stmt, current_that, current_topic)
        return scope_ok

    retrieval = engram.query(
        text,
        context_id=context_id,
        limit=max(context_limit, 1),
        statement_filter=eligible_conversational_statement,
        owner=speaker,
    )
    matches = retrieval.get("matches", [])
    keywords = retrieval.get("keywords", [])
    content_keywords = [kw for kw in keywords if kw not in QUESTION_WORDS]
    if matches and content_keywords:
        top_stmt, top_score = matches[0]
        if top_score >= high_confidence:
            top_template = top_stmt.get("template", {})
            tier2_unknown = False
            if top_template:
                tier2_session = {}
                if context_id:
                    with engram.session_lock:
                        tier2_session = sessions_mod.get_session(engram, context_id, create_if_missing=True)
                tier2_state = {"unknown": False}
                top_text = engram.process_statement_template(
                    top_stmt,
                    [],
                    text,
                    tier2_session,
                    that=current_that,
                    topic=current_topic,
                    evaluation_time=evaluation_time,
                    speaker=speaker,
                    response_state=tier2_state,
                )
                tier2_unknown = tier2_state.get("unknown", False)
            else:
                top_text = top_stmt.get("text", "")
            if top_text.strip() and not tier2_unknown:
                engram.record_hit(keywords, statement_id=top_stmt.get("id", ""))
                update_session(engram, context_id, top_text)
                tier2 = {
                    "response": top_text,
                    "unknown": False,
                    "source": "statement",
                    "score": top_score,
                    "matches": list(matches),
                    "keywords": list(keywords),
                    "pattern": "",
                    "captured": [],
                    "human_id": context_id,
                    "dialogue_act": "",
                    "active_topic": "",
                    "entities": [],
                    "fact_admissions": [],
                }
                result = attach_dialogue_state(engram, tier2, context_id)
                return result

    # Tier 3: the caller's LLM, with retrieved context.
    if llm_fn:
        context_statements = [context_stmt.get("text", "") for context_stmt, _ in matches[:context_limit]]
        generated = llm_fn(text, context_statements)
        response = require_text(generated, "llm_fn response", MAX_RESPONSE_BYTES, allow_empty=True, characters=Characters.LINES)
        if response:
            update_session(engram, context_id, response)
            tier3 = {
                "response": response,
                "source": "llm",
                "score": 0.0,
                "matches": list(matches),
                "keywords": list(keywords),
                "pattern": "",
                "captured": [],
                "human_id": context_id,
                "dialogue_act": "",
                "active_topic": "",
                "entities": [],
                "fact_admissions": [],
            }
            result = attach_dialogue_state(engram, tier3, context_id)
            return result

    # Tier 4: nothing matched and nothing confident.
    top_score = matches[0][1] if matches else 0.0
    tier4 = {
        "response": "",
        "unknown": True,
        "source": "none",
        "score": top_score,
        "matches": list(matches),
        "keywords": list(keywords),
        "pattern": "",
        "captured": [],
        "human_id": context_id,
        "dialogue_act": "",
        "active_topic": "",
        "entities": [],
        "fact_admissions": [],
    }
    result = attach_dialogue_state(engram, tier4, context_id)
    return result


def chat(
    engram,
    text: str,
    human_id: str = DEFAULT_HUMAN_ID,
    llm_fn=(),
    high_confidence: float = DEFAULT_PIPELINE_HIGH_CONFIDENCE,
    context_limit: int = DEFAULT_PIPELINE_CONTEXT_LIMIT,
    evaluation_time: str = "",
) -> dict:
    """Run the chatbot for one caller-owned human context."""
    normalized_human_id = sessions_mod.normalize_human_id(human_id)
    result = respond(
        engram,
        text,
        context_id=normalized_human_id,
        llm_fn=llm_fn,
        high_confidence=high_confidence,
        context_limit=context_limit,
        human_id=normalized_human_id,
        evaluation_time=evaluation_time,
    )
    return result
