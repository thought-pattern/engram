"""Conversation-engine semantics: re-teaching, long input, years, captures, and seeded turns."""

from pytest import mark as pytest_mark, raises as pytest_raises

from engram.constants import MAX_FACT_SENTENCE_WORDS, MAX_PATTERN_WORDS, MAX_REQUEST_BYTES, TemporalQueryOperator, Tier
from engram.conversation import ConversationRuntime
from engram.core import Engram
from engram.errors import InvalidRequestError
from engram.identity import extract_standalone_identity
from engram.pattern import PatternMatcher
from engram.temporal import parse_temporal_query

LEARN_THAT = {
    "pattern": "LEARN THAT * IS *",
    "response": "Got it.",
    "template": {
        "sequence": [
            {"learn": {"pattern": "{upper:{star1}}", "template": {"text": "{star2}"}}},
            {"text": "Got it. {star1} is {star2}."},
        ]
    },
}


def learned_texts(engram: Engram, pattern: str) -> list[str]:
    result = [statement.get("text", "") for statement in engram.statements if statement.get("pattern", "") == pattern]
    return result


# R4.1 --------------------------------------------------------------------


def test_reteaching_a_subject_changes_the_answer_and_keeps_one_statement() -> None:
    engram = Engram()
    engram.load_static_data([LEARN_THAT, {"pattern": "*", "response": "Go on."}])

    engram.pattern_query("Learn that zorblax is blue")
    assert engram.pattern_query("zorblax")[2] == "Blue"
    engram.pattern_query("Learn that zorblax is green")

    assert engram.pattern_query("zorblax")[2] == "Green"
    assert learned_texts(engram, "ZORBLAX") == ["green"]


def test_teaching_never_replaces_a_seed_answer() -> None:
    engram = Engram()
    engram.load_static_data(
        [LEARN_THAT, {"pattern": "*", "response": "Go on."}, {"pattern": "ZORBLAX", "response": "A seed answer."}]
    )

    engram.pattern_query("Learn that zorblax is blue")

    assert engram.pattern_query("zorblax")[2] == "A seed answer."


def test_a_learned_fact_never_replaces_a_hand_stored_statement() -> None:
    engram = Engram()
    engram.store("Go on.", pattern="*", tier=Tier.STATIC)
    engram.store("Custom response", pattern="CATS")

    engram.pattern_query("Cats are mammals.")
    engram.pattern_query("Cats are reptiles.")

    assert engram.pattern_query("cats")[2] == "Custom response"


# R4.5 --------------------------------------------------------------------


def test_patterns_are_capped_so_the_walk_depth_is_bounded() -> None:
    matcher = PatternMatcher()
    with pytest_raises(ValueError, match=f"limit of {MAX_PATTERN_WORDS} words"):
        matcher.add_pattern(" ".join(["WORD"] * (MAX_PATTERN_WORDS + 1)), "too long")
    with pytest_raises(ValueError, match="that exceeds"):
        matcher.add_pattern("HELLO", "too long", that=" ".join(["WORD"] * (MAX_PATTERN_WORDS + 1)))

    matcher.add_pattern(" ".join(["WORD"] * MAX_PATTERN_WORDS), "at the limit")
    assert matcher.match(" ".join(["word"] * MAX_PATTERN_WORDS))[0] == "at the limit"


def test_teaching_a_very_long_subject_is_skipped_and_matching_stays_safe() -> None:
    engram = Engram()
    engram.load_static_data([LEARN_THAT, {"pattern": "*", "response": "Go on."}])
    subject = " ".join(f"w{index}" for index in range(2_000))

    engram.pattern_query(f"Learn that {subject} is purple")

    assert not [statement for statement in engram.statements if statement.get("text", "") == "purple"]
    assert engram.pattern_query(subject)[2] == "Go on."


def test_long_input_captures_are_exact() -> None:
    matcher = PatternMatcher()
    matcher.add_pattern("HELLO * GOODBYE *", "both")
    words = [f"w{index}" for index in range(2_500)]

    result = matcher.match(f"hello {' '.join(words)} goodbye tail")

    assert result[1] == [" ".join(words), "tail"]


def test_a_sentence_too_long_to_be_a_fact_is_not_read_for_facts(monkeypatch) -> None:
    def no_extraction(sentence: str) -> dict:
        raise AssertionError("a long sentence was read for facts")

    monkeypatch.setattr("engram.core.extract_fact", no_extraction)
    engram = Engram()
    engram.load_static_data([LEARN_THAT, {"pattern": "*", "response": "Go on."}])
    long_sentence = " ".join(f"w{index}" for index in range(MAX_FACT_SENTENCE_WORDS)) + " is purple."

    assert engram.pattern_query(long_sentence)[2] == "Go on."


def test_pattern_query_takes_no_more_than_a_chat_message() -> None:
    engram = Engram()
    with pytest_raises(InvalidRequestError, match=f"limit of {MAX_REQUEST_BYTES} bytes"):
        engram.pattern_query("x" * (MAX_REQUEST_BYTES + 1))


# R4.6 --------------------------------------------------------------------


def test_lemma_fallback_captures_the_original_words() -> None:
    engram = Engram()
    engram.load_static_data([{"pattern": "I SAW THE * WHERE * BE BARK", "response": "Seen."}])

    assert engram.pattern_query("I saw the store where dogs were barking")[1] == ["store", "dogs"]
    assert engram.pattern_query("I saw the store where the dogs are barking")[1] == ["store", "the dogs"]


# R4.3 --------------------------------------------------------------------


@pytest_mark.parametrize(
    "text",
    [
        "MTU limit in 1500 byte frames",
        "Store it in 4096 byte pages",
        "What will happen in 2000 years?",
        "after 2000 requests",
        "during 1500-byte transfers",
        "between 1000 and 2000 requests",
        "Run it in 8080",
        "4096",
    ],
)
def test_numbers_with_units_or_out_of_range_are_not_years(text: str) -> None:
    parsed = parse_temporal_query(text)
    assert "operator" in parsed
    assert parsed.get("operator", TemporalQueryOperator.UNSPECIFIED) == TemporalQueryOperator.UNSPECIFIED
    assert "source_text" in parsed
    assert parsed.get("source_text", "") == ""


@pytest_mark.parametrize(
    ("text", "operator"),
    [
        ("What happened in 1969?", TemporalQueryOperator.IN_YEAR),
        ("What happened in 1969 in Europe?", TemporalQueryOperator.IN_YEAR),
        ("in the year 1500", TemporalQueryOperator.IN_YEAR),
        ("before 2020", TemporalQueryOperator.BEFORE),
        ("between 2023 and 2024?", TemporalQueryOperator.BETWEEN),
        ("1969?", TemporalQueryOperator.IN_YEAR),
    ],
)
def test_real_years_are_still_years(text: str, operator: TemporalQueryOperator) -> None:
    parsed = parse_temporal_query(text)
    assert parsed.get("operator", TemporalQueryOperator.UNSPECIFIED) == operator
    assert parsed.get("resolved", False) is True


def test_a_quantity_stays_a_lexical_term() -> None:
    assert "1500" in extract_standalone_identity("MTU limit in 1500 byte frames").get("lexical_terms", ())


# R4.4 --------------------------------------------------------------------


def test_a_seeded_turn_uses_its_own_generator_and_replays_exactly(monkeypatch) -> None:
    def shared_generator(choices: list) -> object:
        raise AssertionError("a seeded turn drew from the shared generator")

    monkeypatch.setattr("engram.template.random_choice", shared_generator)
    replies = []
    for _ in range(2):
        engram = Engram()
        engram.load_static_data([{"pattern": "PICK", "response": "", "template": {"random": list("abcdefgh")}}])
        runtime = ConversationRuntime(engram, user_id="alice", random_seed=7, random_seed_present=True)
        replies.append([runtime.send("pick").get("response", "") for _ in range(6)])

    assert replies[0] == replies[1]
    assert len(set(replies[0])) > 1
