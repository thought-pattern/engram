"""Tests for the tiered response pipeline."""

from engram import pipeline, sessions
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

    def test_llm_response_has_non_user_provenance(self) -> None:
        engram = Engram()
        llm_fn, _ = _counting_llm("A generated answer.")

        pipeline.chat(engram, "a novel question", user_id="alice", llm_fn=llm_fn)

        stmt = next(s for s in engram.statements if s["text"] == "A generated answer.")
        assert stmt["introduced_by_user_id"] is None
        assert stmt["source_label"] == "llm"

    def test_contextual_cache_key_does_not_leak_to_fresh_session(self) -> None:
        engram = Engram()
        sessions.create_session(engram, "paris")
        sessions.update_session_context(
            engram,
            "paris",
            "Paris is the capital of France.",
        )
        paris_llm, _ = _counting_llm("Paris has about 2.1 million residents.")

        first = pipeline.respond(
            engram,
            "What is its population?",
            session_id="paris",
            llm_fn=paris_llm,
        )

        fresh_llm, fresh_calls = _counting_llm("Which place do you mean?")
        fresh = pipeline.respond(
            engram,
            "What is its population?",
            session_id="fresh",
            llm_fn=fresh_llm,
        )

        assert first["source"] == "llm"
        assert fresh["source"] == "llm"
        assert fresh["response"] == "Which place do you mean?"
        assert len(fresh_calls) == 1

        sessions.create_session(engram, "same-context")
        sessions.update_session_context(
            engram,
            "same-context",
            "Paris is the capital of France.",
        )
        same = pipeline.respond(
            engram,
            "What is its population?",
            session_id="same-context",
        )
        assert same["source"] == "cache"
        assert same["response"] == "Paris has about 2.1 million residents."


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


class TestUserAwareChat:
    def test_missing_user_uses_default_context(self) -> None:
        engram = Engram()
        engram.store("Hello!", pattern="HELLO", tier=Tier.STATIC)

        result = pipeline.chat(engram, "hello", user_id="")

        assert result["user_id"] == "0"
        assert "0" in engram.sessions

    def test_context_is_isolated_but_learned_facts_are_shared(self) -> None:
        engram = Engram()
        engram.store("Go on.", pattern="*", tier=Tier.STATIC)

        alice = pipeline.chat(engram, "Sushi is good.", user_id="Alice")
        carol = pipeline.chat(engram, "What's good?", user_id="Carol")

        assert alice["source"] == "pattern"
        assert carol["source"] == "pattern"
        assert carol["response"] == "Sushi is good."
        learned_id = engram.pattern_to_statement["SUSHI"]
        assert engram.get_statement(learned_id)["introduced_by_user_id"] == "Alice"
        assert engram.sessions["Alice"]["input_history"] == ["Sushi is good."]
        assert engram.sessions["Carol"]["input_history"] == ["What's good?"]

    def test_user_labels_are_case_sensitive_and_caller_owned(self) -> None:
        engram = Engram()
        engram.store("Hello!", pattern="HELLO", tier=Tier.STATIC)

        pipeline.chat(engram, "hello", user_id="Alice")
        pipeline.chat(engram, "hello", user_id="alice")

        assert "Alice" in engram.sessions
        assert "alice" in engram.sessions


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


class TestPipelineResultDetail:
    def test_pattern_tier_carries_pattern_and_captures(self) -> None:
        engram = Engram()
        engram.store("Nice to meet you, {star1}!", pattern="MY NAME IS *", tier=Tier.STATIC)

        result = pipeline.respond(engram, "my name is alice")

        assert result["pattern"] == "MY NAME IS *"
        assert result["captured"] == ["alice"]

    def test_cache_tier_has_empty_pattern_fields(self) -> None:
        engram = Engram()
        engram.store("Paris is the capital of France.")

        result = pipeline.respond(engram, "paris capital france")

        assert result["source"] == "cache"
        assert result["pattern"] == ""
        assert result["captured"] == []


class TestConversationalComposition:
    def test_chat_uses_final_matched_sentence_instead_of_concatenating(self) -> None:
        engram = Engram()
        engram.store("First reply.", pattern="FIRST", tier=Tier.STATIC)
        engram.store("Second reply.", pattern="SECOND", tier=Tier.STATIC)

        result = pipeline.chat(engram, "First. Second.", user_id="speaker")

        assert result["response"] == "Second reply."
        assert result["pattern"] == "SECOND"

    def test_low_level_pattern_query_keeps_legacy_combination(self) -> None:
        engram = Engram()
        engram.store("First reply.", pattern="FIRST", tier=Tier.STATIC)
        engram.store("Second reply.", pattern="SECOND", tier=Tier.STATIC)

        result = engram.pattern_query("First. Second.")

        assert result[2] == "First reply. Second reply."

    def test_repetition_feedback_gets_an_acknowledgment_not_another_probe(self) -> None:
        engram = Engram()
        engram.store("Why do you say that?", pattern="*", tier=Tier.STATIC)

        pipeline.chat(engram, "Limited time creates urgency.", user_id="speaker")
        result = pipeline.chat(
            engram,
            "I just explained why. You're asking the same question.",
            user_id="speaker",
        )

        assert "repeating myself" in result["response"]
        assert result["response"] != "Why do you say that?"


class TestDeferredShrugRetraction:
    def test_phantom_shrug_removed_when_cache_answers(self) -> None:
        engram = Engram()
        engram.store("Tell me more.", pattern="*", tier=Tier.STATIC)
        engram.store("Python is a versatile programming language.")

        result = pipeline.respond(engram, "python programming language?", session_id="s1")

        assert result["source"] == "cache"
        session = engram.sessions["s1"]
        # Only the answer the user actually saw is in the history
        assert session["response_history"] == ["Python is a versatile programming language."]
        assert session["previous_response"] == "Python is a versatile programming language."

    def test_shrug_stays_in_history_when_actually_shown(self) -> None:
        engram = Engram()
        engram.store("Tell me more.", pattern="*", tier=Tier.STATIC)

        result = pipeline.respond(engram, "What is the meaning of life?", session_id="s2")

        assert result["source"] == "pattern"
        assert engram.sessions["s2"]["response_history"] == ["Tell me more."]


class TestContentKeywordGate:
    def test_question_words_alone_are_no_evidence(self) -> None:
        """A keyword set of only question words must not clear the cache bar."""
        engram = Engram()
        engram.store("Alright!", pattern="WHY NOT", tier=Tier.STATIC)

        result = pipeline.respond(engram, "why why why why why")

        assert result["source"] != "cache"

    def test_single_content_keyword_still_caches(self) -> None:
        engram = Engram()
        engram.learn_from_response("boiling point of water", "It boils at 100 C.")

        result = pipeline.respond(engram, "water?")

        assert result["source"] == "cache"
        assert result["response"] == "It boils at 100 C."
