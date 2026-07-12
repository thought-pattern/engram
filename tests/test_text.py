"""Tests for text processing."""

from engram.constants import DEFAULT_STOPWORDS
from engram.text import (
    expand_query,
    extract_keywords,
    lemmatize_word,
    normalize,
    normalize_with_stemming,
    stem_text,
    stem_word,
)


class TestNormalize:
    """Tests for text normalization."""

    def test_lowercase(self) -> None:
        assert normalize("HELLO WORLD") == "hello world"
        assert normalize("HeLLo WoRLD") == "hello world"

    def test_punctuation_removal(self) -> None:
        assert normalize("What's the S&P 500 price?") == "whats the sp 500 price"
        assert normalize("Hello, world!") == "hello world"
        assert normalize("test@example.com") == "testexamplecom"

    def test_intra_word_hyphen_preserved(self) -> None:
        assert normalize("well-known fact") == "well-known fact"
        assert normalize("state-of-the-art") == "state-of-the-art"

    def test_whitespace_collapse(self) -> None:
        assert normalize("hello    world") == "hello world"
        assert normalize("  hello   world  ") == "hello world"
        assert normalize("hello\t\nworld") == "hello world"

    def test_empty_string(self) -> None:
        assert normalize("") == ""
        assert normalize("   ") == ""

    def test_numbers_preserved(self) -> None:
        assert normalize("Python 3.12") == "python 312"
        assert normalize("100 dollars") == "100 dollars"

    def test_spec_example(self) -> None:
        # Example from spec
        assert normalize("What's the S&P 500 price?") == "whats the sp 500 price"


class TestExtractKeywords:
    """Tests for keyword extraction."""

    def test_basic_extraction(self) -> None:
        result = extract_keywords("whats the capital france", DEFAULT_STOPWORDS)
        assert "whats" in result
        assert "capital" in result
        assert "france" in result
        assert "the" not in result

    def test_stopword_removal(self) -> None:
        result = extract_keywords("the quick brown fox", DEFAULT_STOPWORDS)
        assert "the" not in result
        assert "quick" in result
        assert "brown" in result
        assert "fox" in result

    def test_deduplication(self) -> None:
        result = extract_keywords("paris paris paris", DEFAULT_STOPWORDS)
        assert result == ["paris"]

    def test_order_preserved(self) -> None:
        result = extract_keywords("alpha beta gamma", DEFAULT_STOPWORDS)
        assert result == ["alpha", "beta", "gamma"]

    def test_empty_input(self) -> None:
        assert extract_keywords("", DEFAULT_STOPWORDS) == []

    def test_only_stopwords(self) -> None:
        result = extract_keywords("the is are was", DEFAULT_STOPWORDS)
        assert result == []

    def test_custom_stopwords(self) -> None:
        custom = {"custom", "stop"}
        result = extract_keywords("custom stop word", custom)
        assert result == ["word"]


class TestExpandQuery:
    """Tests for query expansion."""

    def test_expands_with_nouns_only(self) -> None:
        # Only the previous response's nouns are appended -- the referents a
        # pronoun can point back to -- not its stopwords and verbs.
        result = expand_query("What is its population?", "Paris is the capital of France")
        assert result == "What is its population? Paris capital France"

    def test_expansion_skips_non_referents(self) -> None:
        result = expand_query("Why?", "The tower was built quickly in Paris")
        assert "Paris" in result
        assert "tower" in result
        assert "quickly" not in result
        assert "built" not in result

    def test_expansion_falls_back_when_no_nouns(self) -> None:
        # A response with nothing taggable as a noun falls back to appending
        # the full response rather than dropping context entirely.
        result = expand_query("What?", "Very quickly")
        assert result.startswith("What? ")

    def test_empty_previous_response(self) -> None:
        assert expand_query("Hello world", "") == "Hello world"

    def test_none_like_empty(self) -> None:
        assert expand_query("Hello", "") == "Hello"

    def test_spec_example(self) -> None:
        # Example from spec
        query = "What is its population?"
        previous = "Paris is the capital of France"
        result = expand_query(query, previous)
        assert "population" in result
        assert "Paris" in result
        assert "capital" in result
        assert "France" in result


class TestStemming:
    """Tests for stemming functions."""

    def test_stem_word_basic(self) -> None:
        """Test basic stemming."""
        assert stem_word("running") == "run"
        assert stem_word("cats") == "cat"
        assert stem_word("jumped") == "jump"

    def test_stem_word_case_insensitive(self) -> None:
        """Test stemming is case-insensitive."""
        assert stem_word("Running") == "run"
        assert stem_word("CATS") == "cat"

    def test_stem_word_already_stemmed(self) -> None:
        """Test words that are already in stem form."""
        assert stem_word("run") == "run"
        assert stem_word("cat") == "cat"

    def test_stem_text(self) -> None:
        """Test stemming entire text."""
        result = stem_text("the cats are running quickly")
        assert "cat" in result
        assert "run" in result

    def test_normalize_with_stemming(self) -> None:
        """Test combined normalization and stemming."""
        result = normalize_with_stemming("The CATS are RUNNING!")
        # Should be lowercase, no punctuation, and stemmed
        assert "cat" in result
        assert "run" in result
        assert "!" not in result


class TestLemmatization:
    """Tests for lemmatization functions."""

    def test_lemmatize_noun(self) -> None:
        """Test lemmatizing nouns."""
        assert lemmatize_word("cats", "n") == "cat"
        assert lemmatize_word("dogs", "n") == "dog"
        assert lemmatize_word("children", "n") == "child"

    def test_lemmatize_verb(self) -> None:
        """Test lemmatizing verbs."""
        assert lemmatize_word("running", "v") == "run"
        assert lemmatize_word("ran", "v") == "run"

    def test_lemmatize_adjective(self) -> None:
        """Test lemmatizing adjectives."""
        assert lemmatize_word("better", "a") == "good"  # WordNet handles comparatives
        assert lemmatize_word("fastest", "a") == "fast"

    def test_lemmatize_default_noun(self) -> None:
        """Test default POS is noun."""
        assert lemmatize_word("cats") == "cat"


class TestCorrectSpelling:
    """Tests for store-vocabulary spelling correction."""

    def test_corrects_transposition_typo(self) -> None:
        from engram.text import correct_spelling

        vocabulary = {"about", "capital", "france"}
        assert correct_spelling("tell me abotu france", vocabulary) == "tell me about france"

    def test_never_corrects_real_english_words(self) -> None:
        from engram.text import correct_spelling

        # "abort" is not in the store, but it is a real word -- leave it alone.
        vocabulary = {"about"}
        assert correct_spelling("abort", vocabulary) == "abort"

    def test_never_corrects_short_tokens(self) -> None:
        from engram.text import correct_spelling

        vocabulary = {"cat"}
        assert correct_spelling("cta", vocabulary) == "cta"

    def test_keeps_vocabulary_tokens(self) -> None:
        from engram.text import correct_spelling

        vocabulary = {"about", "capital"}
        assert correct_spelling("about capital", vocabulary) == "about capital"

    def test_ambiguous_candidates_left_alone(self) -> None:
        from engram.text import correct_spelling

        # Two vocabulary words at the same distance: do not guess.
        vocabulary = {"gramx", "gramy"}
        assert correct_spelling("gramz", vocabulary) == "gramz"

    def test_empty_vocabulary_is_no_op(self) -> None:
        from engram.text import correct_spelling

        assert correct_spelling("abotu anything", set()) == "abotu anything"


class TestFirstClause:
    """Tests for clause trimming."""

    def test_cuts_new_subject_verb_clause(self) -> None:
        from engram.text import first_clause

        assert first_clause("tired i have been working really hard") == "tired"

    def test_strips_dangling_conjunction(self) -> None:
        from engram.text import first_clause

        assert first_clause("exhausted and i want to sleep") == "exhausted"

    def test_single_clause_unchanged(self) -> None:
        from engram.text import first_clause

        assert first_clause("really happy about the results") == "really happy about the results"

    def test_short_capture_unchanged(self) -> None:
        from engram.text import first_clause

        assert first_clause("alice") == "alice"
        assert first_clause("") == ""
