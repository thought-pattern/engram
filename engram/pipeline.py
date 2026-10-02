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

from engram import sessions as sessions_mod
from engram.constants import KIND_COMMAND, KIND_QUESTION, QUESTION_WORDS
from engram.nlp import input_kind


def pipeline_result(
    response: str,
    source: str,
    score: float = 0.0,
    matches=(),
    keywords=(),
    pattern: str = "",
    captured=(),
    user_id: str = "",
) -> dict:
    """Build a pipeline response dict.

    source is "pattern" (scripted match), "graph" (read-only graph recall),
    "statement" (confident conversational-statement retrieval), "llm" (generated via llm_fn), or
    "none" (nothing confident and no llm_fn). matches and keywords carry the keyword retrieval outcome so a
    "none" caller can still inspect what was found; pattern and captured carry
    the pattern-match outcome for debugging ("" / [] off the pattern path).
    """
    result = {
        "response": response,
        "source": source,
        "score": score,
        "matches": list(matches or ()),
        "keywords": list(keywords or ()),
        "pattern": pattern,
        "captured": list(captured or ()),
        "user_id": user_id,
        "dialogue_act": "",
        "active_topic": "",
        "entities": [],
        "fact_admissions": [],
    }
    return result


def attach_dialogue_state(engram, result: dict, session_id: str) -> dict:
    """Attach the latest per-user discourse state to an API result."""
    if not session_id:
        return result
    with engram.session_lock:
        session = engram.sessions.get(session_id, {})
        if not session:
            return result
        history = session.get("dialogue_act_history", [])
        result["dialogue_act"] = history[0] if history else ""
        result["active_topic"] = session.get("active_topic", "")
        result["entities"] = list(session.get("entities", []))
        result["fact_admissions"] = list(session.get("last_fact_admissions", []))
    return result


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
        session = engram.sessions.get(session_id, {})
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
    sessions_mod.get_session(engram, session_id, create_if_missing=True)
    sessions_mod.update_session_context(engram, session_id, response)
    return True


def respond(
    engram,
    text: str,
    context_id: str = "",
    llm_fn=(),
    high_confidence: float = 0.7,
    context_limit: int = 3,
    user_id: str = "",
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
        text: User input text.
        context_id: Optional conversation context for turn state.
        llm_fn: Optional callable (text, context_statements) -> response str.
        high_confidence: Calibrated score at or above which a conversational statement
            answers without the LLM (scores run 0.0 - 1.0).
        context_limit: Maximum retrieved statements passed to llm_fn.
        user_id: Optional caller-owned identity for learned-fact attribution.
        evaluation_time: Optional canonical UTC timestamp used by graph
            recall for this turn.

    Returns:
        Pipeline result dict (response, source, score, matches, keywords).
    """
    if not 0 <= high_confidence <= 1:
        raise ValueError("high_confidence must be between 0 and 1")
    if context_limit < 0:
        raise ValueError("context_limit must be non-negative")

    # Tier 1: a matched category, including a pure wildcard, or graph recall.
    # The configured fallback text is not a category answer.
    pattern_result = engram.pattern_query(
        text,
        context_id=context_id,
        user_id=user_id,
        include_graph=False,
    )
    stmt = {}
    captured = []
    response = ""
    if pattern_result:
        stmt, captured, response = pattern_result

    # Graph recall takes precedence for questions and commands, applied once
    # for the complete turn with the turn's evaluation clock.
    graph_enabled = bool((engram.config.get("graph") or {}).get("enabled"))
    graph_eligible = input_kind(text) in {KIND_COMMAND, KIND_QUESTION}
    graph_response = engram.graph_lookup(text, evaluation_time=evaluation_time) if graph_enabled and graph_eligible else ""
    if graph_response:
        if response:
            retract_response(engram, context_id, response)
        update_session(engram, context_id, graph_response)
        graph_result = pipeline_result(
            graph_response,
            "graph",
            score=1.0,
            user_id=context_id,
        )
        result = attach_dialogue_state(engram, graph_result, context_id)
        return result

    if pattern_result:
        matched_pattern = stmt["pattern"] if stmt else ""
        is_fallback = not stmt and response == engram.config["fallback_response"]
        if response and not is_fallback:
            source = "pattern" if stmt else "graph"
            tier1 = pipeline_result(
                response,
                source,
                score=1.0,
                pattern=matched_pattern,
                captured=captured,
                user_id=context_id,
            )
            result = attach_dialogue_state(engram, tier1, context_id)
            return result

    # Tier 2: confident conversational statement via keyword retrieval. Question words
    # carry intent, not content -- a keyword set with no content words ("why
    # why why") is no evidence, however perfectly it overlaps something.
    retrieval = engram.query(text, context_id=context_id, limit=max(context_limit, 1))
    matches = retrieval["matches"]
    keywords = retrieval["keywords"]
    content_keywords = [kw for kw in keywords if kw not in QUESTION_WORDS]
    if matches and content_keywords:
        top_stmt, top_score = matches[0]
        if top_score >= high_confidence:
            engram.record_hit(keywords, statement_id=top_stmt["id"])
            update_session(engram, context_id, top_stmt["text"])
            tier2 = pipeline_result(
                top_stmt["text"],
                "statement",
                score=top_score,
                matches=matches,
                keywords=keywords,
                user_id=context_id,
            )
            result = attach_dialogue_state(engram, tier2, context_id)
            return result

    # Tier 3: the caller's LLM, with retrieved context.
    if llm_fn:
        context_statements = [stmt["text"] for stmt, _ in matches[:context_limit]]
        response = llm_fn(text, context_statements)
        if response:
            update_session(engram, context_id, response)
            tier3 = pipeline_result(
                response,
                "llm",
                matches=matches,
                keywords=keywords,
                user_id=context_id,
            )
            result = attach_dialogue_state(engram, tier3, context_id)
            return result

    # Tier 4: nothing matched and nothing confident.
    top_score = matches[0][1] if matches else 0.0
    tier4 = pipeline_result(
        "",
        "none",
        score=top_score,
        matches=matches,
        keywords=keywords,
        user_id=context_id,
    )
    result = attach_dialogue_state(engram, tier4, context_id)
    return result


def chat(
    engram,
    text: str,
    user_id: str = "0",
    llm_fn=(),
    high_confidence: float = 0.7,
    context_limit: int = 3,
) -> dict:
    """Run the chatbot for one caller-owned user context."""
    normalized_user_id = sessions_mod.normalize_user_id(user_id)
    result = respond(
        engram,
        text,
        context_id=normalized_user_id,
        llm_fn=llm_fn,
        high_confidence=high_confidence,
        context_limit=context_limit,
        user_id=normalized_user_id,
    )
    return result
