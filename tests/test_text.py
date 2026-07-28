"""Tests for text processing."""

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

    def test_restore_capture_case_uses_original_proper_name(self) -> None:
        assert restore_capture_case(["robin"], "My name is Robin.") == ["Robin"]

    def test_restore_capture_case_handles_multiple_words(self) -> None:
        assert restore_capture_case(["mary jane"], "Please call me Mary Jane!") == ["Mary Jane"]

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
        assert result == "What is population? Paris capital France"

    def test_expansion_skips_non_referents(self) -> None:
        result = expand_query("Why is that?", "The tower was built quickly in Paris")
        assert "Paris" in result
        assert "tower" in result
        assert "quickly" not in result
        assert "built" not in result

    def test_expansion_falls_back_when_no_nouns(self) -> None:
        # A response with nothing taggable as a noun falls back to appending
        # the full response rather than dropping context entirely.
        result = expand_query("What is that?", "Very quickly")
        assert result == "What is Very quickly"

    def test_no_expansion_without_referring_pronoun(self) -> None:
        # A self-contained query must not inherit the previous response's
        # nouns -- they would dilute its own keywords.
        result = expand_query("Is the sky blue?", "Cats are mammals")
        assert result == "Is the sky blue?"
        assert expand_query("why why why", "Alright then") == "why why why"

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


class TestFirstClauseRelativeClauses:
    """A pronoun right after a noun is a relative clause, not a new sentence."""

    def test_keeps_relative_clause_after_noun(self) -> None:
        from engram.text import first_clause

        assert first_clause("a friend you can trust") == "a friend you can trust"
        assert first_clause("the movie i saw yesterday") == "the movie i saw yesterday"

    def test_still_cuts_after_non_noun(self) -> None:
        from engram.text import first_clause

        assert first_clause("tired i have been working really hard") == "tired"


class TestStemShortTokens:
    def test_short_tokens_not_stemmed(self) -> None:
        assert stem_text("his") == "his"
        assert stem_text("was") == "was"

    def test_longer_tokens_still_stem(self) -> None:
        assert "run" in stem_text("running quickly")
        assert "cat" in stem_text("cats everywhere")


class TestSpellCorrectionInflections:
    """The English-word gate must recognize inflections the words corpus lacks."""

    def test_inflected_real_words_never_corrected(self) -> None:
        from engram.text import correct_spelling

        # "died", "asking", "notes" are absent from the words corpus but are
        # real inflections; correcting them corrupts valid input.
        vocabulary = {"die", "sing", "note", "ask"}
        assert correct_spelling("my hard drive died", vocabulary) == "my hard drive died"
        assert correct_spelling("thanks for asking", vocabulary) == "thanks for asking"
        assert correct_spelling("i lost my notes", vocabulary) == "i lost my notes"

    def test_genuine_typos_still_corrected(self) -> None:
        from engram.text import correct_spelling

        vocabulary = {"gravity", "about"}
        assert correct_spelling("gravty is strong", vocabulary) == "gravity is strong"

    def test_is_known_word_covers_lemmas(self) -> None:
        from engram.text import is_known_word

        assert is_known_word("died")
        assert is_known_word("tests")
        assert is_known_word("working")
        assert not is_known_word("waht")
        assert not is_known_word("gravty")


class TestExtractName:
    """Tests for name extraction from self-introduction captures."""

    def test_strips_filler_and_trailing_markers(self) -> None:
        from engram.text import extract_name

        assert extract_name("still jason by the way") == "jason"
        assert extract_name("actually bob") == "bob"

    def test_plain_and_multiword_names_kept(self) -> None:
        from engram.text import extract_name

        assert extract_name("jason") == "jason"
        assert extract_name("mary jane") == "mary jane"

    def test_falls_back_to_input_when_nothing_namelike(self) -> None:
        from engram.text import extract_name

        assert extract_name("12345") == "12345"
        assert extract_name("") == ""


class TestFirstClauseQuestionBoundary:
    def test_cuts_at_embedded_question(self) -> None:
        from engram.text import first_clause

        assert first_clause("the sky what is the moon") == "the sky"

    def test_relative_pronoun_after_noun_kept(self) -> None:
        from engram.text import first_clause

        assert first_clause("the man who is tall") == "the man who is tall"
