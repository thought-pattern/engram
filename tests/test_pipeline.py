"""Tests for the tiered response pipeline."""

from engram import pipeline
from engram.config import engram_config
from engram.constants import Tier
from engram.core import Engram


def _counting_llm(response: str = "Generated answer."):
    """A stub LLM that records its calls; returns (fn, calls list)."""
    calls: list[tuple[str, list[str]]] = []

    def llm_fn(text: str, context_statements: list[str]) -> str:
        calls.append((text, context_statements))
        return response

    return llm_fn, calls


class TestPatternTier:
    def test_pattern_match_answers_first(self) -> None:
        engram = Engram()
        engram.store("Support is available 9 to 5.", pattern="SUPPORT HOURS", tier=Tier.STATIC)
        llm_fn, calls = _counting_llm()

        result = pipeline.respond(engram, "support hours", llm_fn=llm_fn)

        assert result["source"] == "pattern"
        assert result["response"] == "Support is available 9 to 5."
        assert calls == []

    def test_configured_fallback_does_not_preempt(self) -> None:
        # The store-level fallback text must not stop the pipeline from
        # reaching the cache and LLM tiers.
        config = engram_config(fallback_response="Tell me more.")
        engram = Engram(config=config)
        llm_fn, calls = _counting_llm()

        result = pipeline.respond(engram, "something entirely new", llm_fn=llm_fn)

        assert result["source"] == "llm"
        assert len(calls) == 1


class TestCacheTier:
    def test_confident_match_answers_without_llm(self) -> None:
        engram = Engram()
        stmt_id = engram.store("Paris is the capital of France.")
        llm_fn, calls = _counting_llm()

        result = pipeline.respond(engram, "paris capital france", llm_fn=llm_fn)

        assert result["source"] == "cache"
        assert result["response"] == "Paris is the capital of France."
        assert result["score"] >= 0.7
        assert calls == []
        # The hit was recorded against the answering statement.
        assert engram.get_statement(stmt_id)["hit_count"] == 1

    def test_weak_match_goes_to_llm_with_context(self) -> None:
        engram = Engram()
        engram.store("Paris is the capital of France.")
        llm_fn, calls = _counting_llm()

        # Only partial keyword overlap: below the confidence threshold.
        result = pipeline.respond(engram, "france pastries and wine culture", llm_fn=llm_fn)

        assert result["source"] == "llm"
        assert len(calls) == 1
        _, context_statements = calls[0]
        assert "Paris is the capital of France." in context_statements


class TestLlmTier:
    def test_llm_response_is_learned_and_cached_next_time(self) -> None:
        engram = Engram()
        llm_fn, calls = _counting_llm("The boiling point is 100 Celsius.")

        first = pipeline.respond(engram, "boiling point of water", llm_fn=llm_fn)
        second = pipeline.respond(engram, "boiling point of water", llm_fn=llm_fn)

        assert first["source"] == "llm"
        assert second["source"] == "cache"
        assert second["response"] == "The boiling point is 100 Celsius."
        assert len(calls) == 1  # the LLM was consulted exactly once

    def test_learn_false_skips_caching(self) -> None:
        engram = Engram()
        llm_fn, calls = _counting_llm()

        pipeline.respond(engram, "boiling point of water", llm_fn=llm_fn, learn=False)
        result = pipeline.respond(engram, "boiling point of water", llm_fn=llm_fn, learn=False)

        assert result["source"] == "llm"
        assert len(calls) == 2

    def test_llm_updates_session_context(self) -> None:
        engram = Engram()
        llm_fn, calls = _counting_llm("Paris is lovely in spring.")

        pipeline.respond(engram, "tell me about paris", session_id="user1", llm_fn=llm_fn)

        session = engram.sessions["user1"]
        assert session["previous_response"] == "Paris is lovely in spring."


class TestNoneTier:
    def test_no_answer_returns_retrieval(self) -> None:
        engram = Engram()
        engram.store("Paris is the capital of France.")

        result = pipeline.respond(engram, "france pastries and wine culture")

        assert result["source"] == "none"
        assert result["response"] == ""
        assert result["matches"]  # the weak retrieval is handed back
        assert 0.0 < result["score"] < 0.7

    def test_empty_store_no_llm(self) -> None:
        engram = Engram()

        result = pipeline.respond(engram, "anything at all")

        assert result["source"] == "none"
        assert result["matches"] == []
        assert result["score"] == 0.0


class TestQuestionRouting:
    """A question that only hits the catch-all consults retrieval before shrugging."""

    def test_question_hitting_catchall_consults_retrieval(self) -> None:
        engram = Engram()
        engram.store("Tell me more.", pattern="*", tier=Tier.STATIC)
        engram.store("Python is a versatile programming language.")

        result = pipeline.respond(engram, "python programming language?")

        assert result["source"] == "cache"
        assert result["response"] == "Python is a versatile programming language."

    def test_question_prefers_llm_over_shrug(self) -> None:
        engram = Engram()
        engram.store("Tell me more.", pattern="*", tier=Tier.STATIC)
        llm_fn, calls = _counting_llm("A deep answer.")

        result = pipeline.respond(engram, "What is the meaning of life?", llm_fn=llm_fn)

        assert result["source"] == "llm"
        assert len(calls) == 1

    def test_question_without_answer_gets_deferred_shrug(self) -> None:
        engram = Engram()
        engram.store("Tell me more.", pattern="*", tier=Tier.STATIC)

        result = pipeline.respond(engram, "What is the meaning of life?")

        assert result["source"] == "pattern"
        assert result["response"] == "Tell me more."

    def test_statement_hitting_catchall_answers_immediately(self) -> None:
        engram = Engram()
        engram.store("Tell me more.", pattern="*", tier=Tier.STATIC)
        engram.store("Python is a versatile programming language.")
        llm_fn, calls = _counting_llm()

        result = pipeline.respond(engram, "i enjoy the python programming language", llm_fn=llm_fn)

        assert result["source"] == "pattern"
        assert result["response"] == "Tell me more."
        assert calls == []

    def test_specific_question_pattern_still_answers_first(self) -> None:
        engram = Engram()
        engram.store("Tell me more.", pattern="*", tier=Tier.STATIC)
        engram.store("I am ENGRAM.", pattern="WHO ARE YOU", tier=Tier.STATIC)

        result = pipeline.respond(engram, "Who are you?")

        assert result["source"] == "pattern"
        assert result["response"] == "I am ENGRAM."
