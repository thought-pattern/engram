"""Tiered response pipeline for ENGRAM.

Packages the integration strategy the narrative documents so callers do not
have to hand-roll it: scripted pattern match first (deterministic, instant),
then a high-confidence cached answer (no LLM call), then the caller's LLM with
retrieved context -- whose response is learned for next time. Scores are
calibrated 0.0 - 1.0, so the confidence threshold means the same thing for
every query.

Usage:

    from engram import pipeline

    def my_llm(text, context_statements):
        prompt = "\\n".join(context_statements) + "\\n\\n" + text
        return call_llm(prompt)

    result = pipeline.respond(engram, "What are the support hours?",
                              session_id=session_id, llm_fn=my_llm)
    print(result["source"], result["response"])
"""

from . import sessions as sessions_mod
from .constants import QUESTION_WORDS
from .nlp import is_question
from .pattern import is_pure_wildcard


def pipeline_result(
    response: str,
    source: str,
    score: float = 0.0,
    matches=None,
    keywords=None,
    pattern: str = "",
    captured=None,
) -> dict:
    """Build a pipeline response dict.

    source is "pattern" (scripted match), "cache" (confident keyword
    retrieval), "llm" (generated via llm_fn), or "none" (nothing confident and
    no llm_fn). matches and keywords carry the keyword retrieval outcome so a
    "none" caller can still inspect what was found; pattern and captured carry
    the pattern-match outcome for debugging ("" / [] off the pattern path).
    """
    result = {
        "response": response,
        "source": source,
        "score": score,
        "matches": matches if matches is not None else [],
        "keywords": keywords if keywords is not None else [],
        "pattern": pattern,
        "captured": captured if captured is not None else [],
    }
    return result


def _retract_response(engram, session_id: str, response: str) -> None:
    """Remove a deferred, not-yet-shown response from the session.

    pattern_query records its response into the session before the pipeline
    decides whether a better tier answers. Left in place, the held response
    poisons the retrieval tier (previous_response feeds session context
    expansion) and lingers as a phantom history entry. Retraction restores
    previous_response to the prior turn's answer; if the held response does
    end up being shown, the caller re-records it.
    """
    if not session_id or not response:
        return
    with engram.session_lock:
        session = engram.sessions.get(session_id)
        if not session:
            return
        if session["response_history"] and session["response_history"][0] == response:
            session["response_history"].pop(0)
            if session["that_history"]:
                session["that_history"].pop(0)
        if session["previous_response"] == response:
            restored = session["response_history"][0] if session["response_history"] else ""
            session["previous_response"] = restored


def _update_session(engram, session_id: str, response: str) -> None:
    """Record a response into the session context, creating the session if needed."""
    if not session_id:
        return
    sessions_mod.get_session(engram, session_id, create_if_missing=True)
    sessions_mod.update_session_context(engram, session_id, response)


def respond(
    engram,
    text: str,
    session_id: str = "",
    llm_fn=None,
    high_confidence: float = 0.7,
    context_limit: int = 3,
    learn: bool = True,
) -> dict:
    """Answer text through the tiered strategy: pattern, cache, then LLM.

    1. Pattern match -- scripted responses answer deterministically. A match
       updates the session context itself. The store's configured
       fallback_response does not count as a pattern answer; it must not
       preempt the cache and LLM tiers. A catch-all (pure-wildcard) match
       answering a *question* is a shrug, not an answer -- it is held back so
       retrieval and the LLM get to speak first, and returned only when
       neither does.
    2. Confident cache -- when the top calibrated retrieval score reaches
       high_confidence, the cached statement answers directly and the hit is
       recorded (feeding scoring and hit-rate-aware eviction).
    3. LLM -- llm_fn(text, context_statements) is called with the top
       retrieved statement texts as context. A non-empty response is learned
       for future queries (when learn is True) and recorded in the session.

    When nothing above answers, the held catch-all response (if any) is
    returned; otherwise source "none" is returned along with the retrieval
    matches so the caller can decide what to do.

    Args:
        engram: Engram instance.
        text: User input text.
        session_id: Optional session for context expansion and history.
        llm_fn: Optional callable (text, context_statements) -> response str.
        high_confidence: Calibrated score at or above which a cached statement
            answers without the LLM (scores run 0.0 - 1.0).
        context_limit: Maximum retrieved statements passed to llm_fn.
        learn: Whether to cache llm_fn responses via learn_from_response.

    Returns:
        Pipeline result dict (response, source, score, matches, keywords).
    """
    # Tier 1: scripted pattern. Accept a response backed by a matched
    # statement or by graph recall, but not the configured fallback text.
    # A catch-all deflection answering a question is held back: confident
    # retrieval (or the LLM) should speak before a shrug does.
    deferred_shrug = ""
    matched_pattern = ""
    matched_captured: list = []
    pattern_result = engram.pattern_query(text, session_id=session_id)
    if pattern_result:
        stmt, captured, response = pattern_result
        matched_pattern = stmt["pattern"] if stmt else ""
        matched_captured = captured
        is_fallback = not stmt and response == engram.config["fallback_response"]
        if response and not is_fallback:
            is_catchall_question = bool(stmt) and is_pure_wildcard(stmt["pattern"]) and is_question(text)
            if is_catchall_question:
                # Retract immediately: the shrug must not contaminate the
                # retrieval tier's session context expansion. It is
                # re-recorded if it actually ends up being shown.
                deferred_shrug = response
                _retract_response(engram, session_id, deferred_shrug)
            else:
                tier1 = pipeline_result(response, "pattern", score=1.0, pattern=matched_pattern, captured=matched_captured)
                return tier1

    # Tier 2: confident cached answer via keyword retrieval. Question words
    # carry intent, not content -- a keyword set with no content words ("why
    # why why") is no evidence, however perfectly it overlaps something.
    retrieval = engram.query(text, session_id=session_id, limit=max(context_limit, 1))
    matches = retrieval["matches"]
    keywords = retrieval["keywords"]
    content_keywords = [kw for kw in keywords if kw not in QUESTION_WORDS]
    if matches and content_keywords:
        top_stmt, top_score = matches[0]
        if top_score >= high_confidence:
            engram.record_hit(keywords, statement_id=top_stmt["id"])
            _update_session(engram, session_id, top_stmt["text"])
            tier2 = pipeline_result(top_stmt["text"], "cache", score=top_score, matches=matches, keywords=keywords)
            return tier2

    # Tier 3: the caller's LLM, with retrieved context.
    if llm_fn:
        context_statements = [stmt["text"] for stmt, _ in matches[:context_limit]]
        response = llm_fn(text, context_statements)
        if response:
            if learn:
                engram.learn_from_response(text, response)
            _update_session(engram, session_id, response)
            tier3 = pipeline_result(response, "llm", matches=matches, keywords=keywords)
            return tier3

    # Tier 4: nothing confident. A held catch-all response still beats
    # silence -- re-record it into the session since it is actually shown --
    # otherwise hand the retrieval back to the caller.
    if deferred_shrug:
        _update_session(engram, session_id, deferred_shrug)
        deferred = pipeline_result(
            deferred_shrug,
            "pattern",
            score=1.0,
            matches=matches,
            keywords=keywords,
            pattern=matched_pattern,
            captured=matched_captured,
        )
        return deferred
    top_score = matches[0][1] if matches else 0.0
    tier4 = pipeline_result("", "none", score=top_score, matches=matches, keywords=keywords)
    return tier4
