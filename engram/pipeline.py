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

from engram import sessions as sessions_mod
from engram.nlp import is_question
from engram.pattern import is_pure_wildcard


def pipeline_result(
    response: str,
    source: str,
    score: float = 0.0,
    matches=None,
    keywords=None,
) -> dict:
    """Build a pipeline response dict.

    source is "pattern" (scripted match), "cache" (confident keyword
    retrieval), "llm" (generated via llm_fn), or "none" (nothing confident and
    no llm_fn). matches and keywords carry the keyword retrieval outcome so a
    "none" caller can still inspect what was found.
    """
    result = {
        "response": response,
        "source": source,
        "score": score,
        "matches": matches if matches is not None else [],
        "keywords": keywords if keywords is not None else [],
    }
    return result


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
    pattern_result = engram.pattern_query(text, session_id=session_id)
    if pattern_result:
        stmt, _, response = pattern_result
        is_fallback = not stmt and response == engram.config["fallback_response"]
        if response and not is_fallback:
            is_catchall_question = bool(stmt) and is_pure_wildcard(stmt["pattern"]) and is_question(text)
            if is_catchall_question:
                deferred_shrug = response
            else:
                tier1 = pipeline_result(response, "pattern", score=1.0)
                return tier1

    # Tier 2: confident cached answer via keyword retrieval.
    retrieval = engram.query(text, session_id=session_id, limit=max(context_limit, 1))
    matches = retrieval["matches"]
    keywords = retrieval["keywords"]
    if matches:
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
    # silence; otherwise hand the retrieval back to the caller.
    if deferred_shrug:
        deferred = pipeline_result(deferred_shrug, "pattern", score=1.0, matches=matches, keywords=keywords)
        return deferred
    top_score = matches[0][1] if matches else 0.0
    tier4 = pipeline_result("", "none", score=top_score, matches=matches, keywords=keywords)
    return tier4
