"""Tests for text processing."""

import pytest

from engram import text as text_module
from engram.constants import DEFAULT_STOPWORDS
from engram.text import (
    expand_query,
    extract_keywords,
    lemmatize_word,
    normalize,
    normalize_with_stemming,
    restore_capture_case,
    stem_text,
    stem_word,
)

"""Tests for text normalization."""


def test_normalize_lowercase() -> None:
    assert normalize("HELLO WORLD") == "hello world"
    assert normalize("HeLLo WoRLD") == "hello world"


def test_normalize_punctuation_removal() -> None:
    assert normalize("What's the S&P 500 price?") == "whats the sp 500 price"
    assert normalize("Hello, world!") == "hello world"
    assert normalize("test@example.com") == "testexamplecom"


def test_normalize_intra_word_hyphen_preserved() -> None:
    assert normalize("well-known fact") == "well-known fact"
    assert normalize("state-of-the-art") == "state-of-the-art"


def test_normalize_whitespace_collapse() -> None:
    assert normalize("hello    world") == "hello world"
    assert normalize("  hello   world  ") == "hello world"
    assert normalize("hello\t\nworld") == "hello world"


def test_normalize_restore_capture_case_uses_original_proper_name() -> None:
    assert restore_capture_case(["robin"], "My name is Robin.") == ["Robin"]


def test_normalize_restore_capture_case_handles_multiple_words() -> None:
    assert restore_capture_case(["mary jane"], "Please call me Mary Jane!") == ["Mary Jane"]


def test_normalize_empty_string() -> None:
    assert normalize("") == ""
    assert normalize("   ") == ""


def test_normalize_numbers_preserved() -> None:
    assert normalize("Python 3.12") == "python 312"
    assert normalize("100 dollars") == "100 dollars"


def test_normalize_spec_example() -> None:
    # Example from spec
    assert normalize("What's the S&P 500 price?") == "whats the sp 500 price"


"""Tests for keyword extraction."""


def test_extract_keywords_basic_extraction() -> None:
    result = extract_keywords("whats the capital france", DEFAULT_STOPWORDS)
    assert "whats" in result
    assert "capital" in result
    assert "france" in result
    assert "the" not in result


def test_extract_keywords_stopword_removal() -> None:
    result = extract_keywords("the quick brown fox", DEFAULT_STOPWORDS)
    assert "the" not in result
    assert "quick" in result
    assert "brown" in result
    assert "fox" in result


def test_extract_keywords_deduplication() -> None:
    result = extract_keywords("paris paris paris", DEFAULT_STOPWORDS)
    assert result == ["paris"]


def test_extract_keywords_order_preserved() -> None:
    result = extract_keywords("alpha beta gamma", DEFAULT_STOPWORDS)
    assert result == ["alpha", "beta", "gamma"]


def test_extract_keywords_empty_input() -> None:
    assert extract_keywords("", DEFAULT_STOPWORDS) == []


def test_extract_keywords_only_stopwords() -> None:
    result = extract_keywords("the is are was", DEFAULT_STOPWORDS)
    assert result == []


def test_extract_keywords_custom_stopwords() -> None:
    custom = {"custom", "stop"}
    result = extract_keywords("custom stop word", custom)
    assert result == ["word"]


def test_required_nltk_failure_is_not_silently_degraded(monkeypatch: pytest.MonkeyPatch) -> None:
    def unavailable(_text: str) -> list[str]:
        raise LookupError("required tokenizer missing")

    monkeypatch.setattr(text_module, "word_tokenize", unavailable)

    with pytest.raises(LookupError, match="required tokenizer missing"):
        extract_keywords("required tokenizer", DEFAULT_STOPWORDS)


"""Tests for query expansion."""


def test_expand_query_expands_with_nouns_only() -> None:
    # Only the previous response's nouns are appended -- the referents a
    # pronoun can point back to -- not its stopwords and verbs.
    result = expand_query("What is its population?", "Paris is the capital of France")
    assert result == "What is population? Paris capital France"


def test_expand_query_expansion_skips_non_referents() -> None:
    result = expand_query("Why is that?", "The tower was built quickly in Paris")
    assert "Paris" in result
    assert "tower" in result
    assert "quickly" not in result
    assert "built" not in result


def test_expand_query_expansion_falls_back_when_no_nouns() -> None:
    # A response with nothing taggable as a noun falls back to appending
    # the full response rather than dropping context entirely.
    result = expand_query("What is that?", "Very quickly")
    assert result == "What is Very quickly"


def test_expand_query_no_expansion_without_referring_pronoun() -> None:
    # A self-contained query must not inherit the previous response's
    # nouns -- they would dilute its own keywords.
    result = expand_query("Is the sky blue?", "Cats are mammals")
    assert result == "Is the sky blue?"
    assert expand_query("why why why", "Alright then") == "why why why"


def test_expand_query_empty_previous_response() -> None:
    assert expand_query("Hello world", "") == "Hello world"


def test_expand_query_none_like_empty() -> None:
    assert expand_query("Hello", "") == "Hello"


def test_expand_query_spec_example() -> None:
    # Example from spec
    query = "What is its population?"
    previous = "Paris is the capital of France"
    result = expand_query(query, previous)
    assert "population" in result
    assert "Paris" in result
    assert "capital" in result
    assert "France" in result


"""Tests for stemming functions."""


def test_stemming_stem_word_basic() -> None:
    """Test basic stemming."""
    assert stem_word("running") == "run"
    assert stem_word("cats") == "cat"
    assert stem_word("jumped") == "jump"


def test_stemming_stem_word_case_insensitive() -> None:
    """Test stemming is case-insensitive."""
    assert stem_word("Running") == "run"
    assert stem_word("CATS") == "cat"


def test_stemming_stem_word_already_stemmed() -> None:
    """Test words that are already in stem form."""
    assert stem_word("run") == "run"
    assert stem_word("cat") == "cat"


def test_stemming_stem_text() -> None:
    """Test stemming entire text."""
    result = stem_text("the cats are running quickly")
    assert "cat" in result
    assert "run" in result


def test_stemming_normalize_with_stemming() -> None:
    """Test combined normalization and stemming."""
    result = normalize_with_stemming("The CATS are RUNNING!")
    # Should be lowercase, no punctuation, and stemmed
    assert "cat" in result
    assert "run" in result
    assert "!" not in result


"""Tests for lemmatization functions."""


def test_lemmatization_lemmatize_noun() -> None:
    """Test lemmatizing nouns."""
    assert lemmatize_word("cats", "n") == "cat"
    assert lemmatize_word("dogs", "n") == "dog"
    assert lemmatize_word("children", "n") == "child"


def test_lemmatization_lemmatize_verb() -> None:
    """Test lemmatizing verbs."""
    assert lemmatize_word("running", "v") == "run"
    assert lemmatize_word("ran", "v") == "run"


def test_lemmatization_lemmatize_adjective() -> None:
    """Test lemmatizing adjectives."""
    assert lemmatize_word("better", "a") == "good"  # WordNet handles comparatives
    assert lemmatize_word("fastest", "a") == "fast"


def test_lemmatization_lemmatize_default_noun() -> None:
    """Test default POS is noun."""
    assert lemmatize_word("cats") == "cat"


"""Tests for store-vocabulary spelling correction."""


def test_correct_spelling_corrects_transposition_typo() -> None:
    from engram.text import correct_spelling

    vocabulary = {"about", "capital", "france"}
    assert correct_spelling("tell me abotu france", vocabulary) == "tell me about france"


def test_correct_spelling_never_corrects_real_english_words() -> None:
    from engram.text import correct_spelling

    # "abort" is not in the store, but it is a real word -- leave it alone.
    vocabulary = {"about"}
    assert correct_spelling("abort", vocabulary) == "abort"


def test_correct_spelling_never_corrects_short_tokens() -> None:
    from engram.text import correct_spelling

    vocabulary = {"cat"}
    assert correct_spelling("cta", vocabulary) == "cta"


def test_correct_spelling_keeps_vocabulary_tokens() -> None:
    from engram.text import correct_spelling

    vocabulary = {"about", "capital"}
    assert correct_spelling("about capital", vocabulary) == "about capital"


def test_correct_spelling_ambiguous_candidates_left_alone() -> None:
    from engram.text import correct_spelling

    # Two vocabulary words at the same distance: do not guess.
    vocabulary = {"gramx", "gramy"}
    assert correct_spelling("gramz", vocabulary) == "gramz"


def test_correct_spelling_empty_vocabulary_is_no_op() -> None:
    from engram.text import correct_spelling

    assert correct_spelling("abotu anything", set()) == "abotu anything"


"""Tests for clause trimming."""


def test_first_clause_cuts_new_subject_verb_clause() -> None:
    from engram.text import first_clause

    assert first_clause("tired i have been working really hard") == "tired"


def test_first_clause_strips_dangling_conjunction() -> None:
    from engram.text import first_clause

    assert first_clause("exhausted and i want to sleep") == "exhausted"


def test_first_clause_single_clause_unchanged() -> None:
    from engram.text import first_clause

    assert first_clause("really happy about the results") == "really happy about the results"


def test_first_clause_short_capture_unchanged() -> None:
    from engram.text import first_clause

    assert first_clause("alice") == "alice"
    assert first_clause("") == ""


"""A pronoun right after a noun is a relative clause, not a new sentence."""


def test_first_clause_relative_clauses_keeps_relative_clause_after_noun() -> None:
    from engram.text import first_clause

    assert first_clause("a friend you can trust") == "a friend you can trust"
    assert first_clause("the movie i saw yesterday") == "the movie i saw yesterday"


def test_first_clause_relative_clauses_still_cuts_after_non_noun() -> None:
    from engram.text import first_clause

    assert first_clause("tired i have been working really hard") == "tired"


def test_stem_short_tokens_short_tokens_not_stemmed() -> None:
    assert stem_text("his") == "his"
    assert stem_text("was") == "was"


def test_stem_short_tokens_longer_tokens_still_stem() -> None:
    assert "run" in stem_text("running quickly")
    assert "cat" in stem_text("cats everywhere")


"""The English-word gate must recognize inflections the words corpus lacks."""


def test_spell_correction_inflections_inflected_real_words_never_corrected() -> None:
    from engram.text import correct_spelling

    # "died", "asking", "notes" are absent from the words corpus but are
    # real inflections; correcting them corrupts valid input.
    vocabulary = {"die", "sing", "note", "ask"}
    assert correct_spelling("my hard drive died", vocabulary) == "my hard drive died"
    assert correct_spelling("thanks for asking", vocabulary) == "thanks for asking"
    assert correct_spelling("i lost my notes", vocabulary) == "i lost my notes"


def test_spell_correction_inflections_genuine_typos_still_corrected() -> None:
    from engram.text import correct_spelling

    vocabulary = {"gravity", "about"}
    assert correct_spelling("gravty is strong", vocabulary) == "gravity is strong"


def test_spell_correction_inflections_is_known_word_covers_lemmas() -> None:
    from engram.text import is_known_word

    assert is_known_word("died")
    assert is_known_word("tests")
    assert is_known_word("working")
    assert not is_known_word("waht")
    assert not is_known_word("gravty")


"""Tests for name extraction from self-introduction captures."""


def test_extract_name_strips_filler_and_trailing_markers() -> None:
    from engram.text import extract_name

    assert extract_name("still jason by the way") == "jason"
    assert extract_name("actually bob") == "bob"


def test_extract_name_plain_and_multiword_names_kept() -> None:
    from engram.text import extract_name

    assert extract_name("jason") == "jason"
    assert extract_name("mary jane") == "mary jane"


def test_extract_name_falls_back_to_input_when_nothing_namelike() -> None:
    from engram.text import extract_name

    assert extract_name("12345") == "12345"
    assert extract_name("") == ""


def test_first_clause_question_boundary_cuts_at_embedded_question() -> None:
    from engram.text import first_clause

    assert first_clause("the sky what is the moon") == "the sky"


def test_first_clause_question_boundary_relative_pronoun_after_noun_kept() -> None:
    from engram.text import first_clause

    assert first_clause("the man who is tall") == "the man who is tall"
