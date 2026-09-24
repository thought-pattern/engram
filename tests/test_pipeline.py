"""Tests for the tiered response pipeline."""

from engram import pipeline
from engram.config import engram_config
from engram.constants import Tier
from engram.core import Engram


def counting_llm(response: str = "Generated answer."):
    """A stub LLM that records its calls; returns (fn, calls list)."""
    calls: list[tuple[str, list[str]]] = []

    def llm_fn(text: str, context_statements: list[str]) -> str:
        calls.append((text, context_statements))
        return response

    result = llm_fn, calls
    return result


def test_pattern_tier_pattern_match_answers_first() -> None:
    engram = Engram()
    engram.store("Support is available 9 to 5.", pattern="SUPPORT HOURS", tier=Tier.STATIC)
    llm_fn, calls = counting_llm()

    result = pipeline.respond(engram, "support hours", llm_fn=llm_fn)

    assert result["source"] == "pattern"
    assert result["response"] == "Support is available 9 to 5."
    assert calls == []


def test_pattern_tier_configured_fallback_does_not_preempt() -> None:
    # The store-level fallback text must not stop the pipeline from
    # reaching conversational retrieval and the LLM.
    config = engram_config(fallback_response="Tell me more.")
    engram = Engram(config=config)
    llm_fn, calls = counting_llm()

    result = pipeline.respond(engram, "something entirely new", llm_fn=llm_fn)

    assert result["source"] == "llm"
    assert len(calls) == 1


def test_statement_tier_confident_match_answers_without_llm() -> None:
    engram = Engram()
    stmt_id = engram.store("Paris is the capital of France.")
    llm_fn, calls = counting_llm()

    result = pipeline.respond(engram, "paris capital france", llm_fn=llm_fn)

    assert result["source"] == "statement"
    assert result["response"] == "Paris is the capital of France."
    assert result["score"] >= 0.7
    assert calls == []
    # The hit was recorded against the answering statement.
    assert engram.get_statement(stmt_id)["hit_count"] == 1


def test_statement_tier_weak_match_goes_to_llm_with_context() -> None:
    engram = Engram()
    engram.store("Paris is the capital of France.")
    llm_fn, calls = counting_llm()

    # Only partial keyword overlap: below the confidence threshold.
    result = pipeline.respond(engram, "france pastries and wine culture", llm_fn=llm_fn)

    assert result["source"] == "llm"
    assert len(calls) == 1
    _, context_statements = calls[0]
    assert "Paris is the capital of France." in context_statements


def test_llm_tier_does_not_store_generated_responses_as_statements() -> None:
    engram = Engram()
    llm_fn, calls = counting_llm("The boiling point is 100 Celsius.")

    first = pipeline.respond(engram, "boiling point of water", llm_fn=llm_fn)
    second = pipeline.respond(engram, "boiling point of water", llm_fn=llm_fn)

    assert first["source"] == "llm"
    assert len(calls) == 2
    assert second["source"] == "llm"
    assert engram.statements == []


def test_llm_tier_llm_updates_session_context() -> None:
    engram = Engram()
    llm_fn, calls = counting_llm("Paris is lovely in spring.")

    pipeline.respond(engram, "tell me about paris", context_id="user1", llm_fn=llm_fn)

    session = engram.sessions["user1"]
    assert session["previous_response"] == "Paris is lovely in spring."


def test_none_tier_no_answer_returns_retrieval() -> None:
    engram = Engram()
    engram.store("Paris is the capital of France.")

    result = pipeline.respond(engram, "france pastries and wine culture")

    assert result["source"] == "none"
    assert result["response"] == ""
    assert result["matches"]  # the weak retrieval is handed back
    assert 0.0 < result["score"] < 0.7


def test_none_tier_empty_store_no_llm() -> None:
    engram = Engram()

    result = pipeline.respond(engram, "anything at all")

    assert result["source"] == "none"
    assert result["matches"] == []
    assert result["score"] == 0.0


def test_user_aware_chat_missing_user_uses_default_context() -> None:
    engram = Engram()
    engram.store("Hello!", pattern="HELLO", tier=Tier.STATIC)

    result = pipeline.chat(engram, "hello", user_id="")

    assert result["user_id"] == "0"
    assert "0" in engram.sessions


def test_user_aware_chat_context_is_isolated_but_learned_facts_are_shared() -> None:
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


def test_user_aware_chat_user_labels_are_case_sensitive_and_caller_owned() -> None:
    engram = Engram()
    engram.store("Hello!", pattern="HELLO", tier=Tier.STATIC)

    pipeline.chat(engram, "hello", user_id="Alice")
    pipeline.chat(engram, "hello", user_id="alice")

    assert "Alice" in engram.sessions
    assert "alice" in engram.sessions


"""A question that only hits the catch-all consults retrieval before shrugging."""


def test_question_routing_question_hitting_catchall_consults_retrieval() -> None:
    engram = Engram()
    engram.store("Tell me more.", pattern="*", tier=Tier.STATIC)
    engram.store("Python is a versatile programming language.")

    result = pipeline.respond(engram, "python programming language?")

    assert result["source"] == "statement"
    assert result["response"] == "Python is a versatile programming language."


def test_question_routing_question_prefers_llm_over_shrug() -> None:
    engram = Engram()
    engram.store("Tell me more.", pattern="*", tier=Tier.STATIC)
    llm_fn, calls = counting_llm("A deep answer.")

    result = pipeline.respond(engram, "What is the meaning of life?", llm_fn=llm_fn)

    assert result["source"] == "llm"
    assert len(calls) == 1


def test_question_routing_question_without_answer_gets_deferred_shrug() -> None:
    engram = Engram()
    engram.store("Tell me more.", pattern="*", tier=Tier.STATIC)

    result = pipeline.respond(engram, "What is the meaning of life?")

    assert result["source"] == "pattern"
    assert result["response"] == "Tell me more."


def test_graph_backed_pattern_tier_reports_graph_provenance(monkeypatch) -> None:
    engram = Engram()
    monkeypatch.setattr(engram, "pattern_query", lambda *internal_args, **internal_kwargs: ({}, [], "Sarah is married to Abraham."))

    result = pipeline.respond(engram, "Who is Sarah married to?")

    assert result["source"] == "graph"
    assert result["response"] == "Sarah is married to Abraham."


def test_question_routing_statement_hitting_catchall_answers_immediately() -> None:
    engram = Engram()
    engram.store("Tell me more.", pattern="*", tier=Tier.STATIC)
    engram.store("Python is a versatile programming language.")
    llm_fn, calls = counting_llm()

    result = pipeline.respond(engram, "i enjoy the python programming language", llm_fn=llm_fn)

    assert result["source"] == "pattern"
    assert result["response"] == "Tell me more."
    assert calls == []


def test_question_routing_specific_question_pattern_still_answers_first() -> None:
    engram = Engram()
    engram.store("Tell me more.", pattern="*", tier=Tier.STATIC)
    engram.store("I am ENGRAM.", pattern="WHO ARE YOU", tier=Tier.STATIC)

    result = pipeline.respond(engram, "Who are you?")

    assert result["source"] == "pattern"
    assert result["response"] == "I am ENGRAM."


def test_pipeline_result_detail_pattern_tier_carries_pattern_and_captures() -> None:
    engram = Engram()
    engram.store("Nice to meet you, {star1}!", pattern="MY NAME IS *", tier=Tier.STATIC)

    result = pipeline.respond(engram, "my name is alice")

    assert result["pattern"] == "MY NAME IS *"
    assert result["captured"] == ["alice"]


def test_pipeline_result_detail_statement_tier_has_empty_pattern_fields() -> None:
    engram = Engram()
    engram.store("Paris is the capital of France.")

    result = pipeline.respond(engram, "paris capital france")

    assert result["source"] == "statement"
    assert result["pattern"] == ""
    assert result["captured"] == []


def test_conversational_composition_chat_uses_final_matched_sentence_instead_of_concatenating() -> None:
    engram = Engram()
    engram.store("First reply.", pattern="FIRST", tier=Tier.STATIC)
    engram.store("Second reply.", pattern="SECOND", tier=Tier.STATIC)

    result = pipeline.chat(engram, "First. Second.", user_id="speaker")

    assert result["response"] == "Second reply."
    assert result["pattern"] == "SECOND"


def test_conversational_composition_pattern_query_selects_one_turn_candidate() -> None:
    engram = Engram()
    engram.store("First reply.", pattern="FIRST", tier=Tier.STATIC)
    engram.store("Second reply.", pattern="SECOND", tier=Tier.STATIC)

    result = engram.pattern_query("First. Second.")

    assert result[2] == "Second reply."


def test_conversational_composition_repetition_feedback_gets_an_acknowledgment_not_another_probe() -> None:
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


def test_deferred_shrug_retraction_phantom_shrug_removed_when_statement_answers() -> None:
    engram = Engram()
    engram.store("Tell me more.", pattern="*", tier=Tier.STATIC)
    engram.store("Python is a versatile programming language.")

    result = pipeline.respond(engram, "python programming language?", context_id="s1")

    assert result["source"] == "statement"
    session = engram.sessions["s1"]
    # Only the answer the user actually saw is in the history
    assert session["response_history"] == ["Python is a versatile programming language."]
    assert session["previous_response"] == "Python is a versatile programming language."


def test_deferred_shrug_retraction_shrug_stays_in_history_when_actually_shown() -> None:
    engram = Engram()
    engram.store("Tell me more.", pattern="*", tier=Tier.STATIC)

    result = pipeline.respond(engram, "What is the meaning of life?", context_id="s2")

    assert result["source"] == "pattern"
    assert engram.sessions["s2"]["response_history"] == ["Tell me more."]


def test_content_keyword_gate_question_words_alone_are_no_evidence() -> None:
    """A keyword set of only question words must not clear the statement bar."""
    engram = Engram()
    engram.store("Alright!", pattern="WHY NOT", tier=Tier.STATIC)

    result = pipeline.respond(engram, "why why why why why")

    assert result["source"] != "statement"


def test_content_keyword_gate_single_content_keyword_still_retrieves_statement() -> None:
    engram = Engram()
    engram.store("It boils at 100 C.", keyword_source="boiling point of water")

    result = pipeline.respond(engram, "water?")

    assert result["source"] == "statement"
    assert result["response"] == "It boils at 100 C."
