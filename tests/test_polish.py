"""Tests for response output polish."""

from engram.polish import polish_response


def test_polish_response_capitalizes_standalone_i() -> None:
    assert polish_response("you're tired i have been working") == "You're tired I have been working"


def test_polish_response_capitalizes_i_contractions() -> None:
    assert polish_response("i'm sorry. i'll try again.") == "I'm sorry. I'll try again."


def test_polish_response_capitalizes_each_sentence_start() -> None:
    assert polish_response("hello there. nice to meet you!") == "Hello there. Nice to meet you!"


def test_polish_response_authored_text_unchanged() -> None:
    text = "I'm sorry to hear you're tired. Want to talk about it?"
    assert polish_response(text) == text


def test_polish_response_no_punctuation_inserted() -> None:
    assert polish_response("nice to meet you alice") == "Nice to meet you alice"


def test_polish_response_empty_text() -> None:
    assert polish_response("") == ""
    assert polish_response("   ") == "   "
