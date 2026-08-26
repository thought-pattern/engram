"""Tests for text processing."""

from engram.constants import DEFAULT_STOPWORDS
from engram.text import (
    correct_spelling,
    expand_query,
    extract_keywords,
    extract_name,
    first_clause,
    is_known_word,
    lemmatize_word,
    normalize,
    normalize_with_stemming,
    restore_capture_case,
    stem_text,
    stem_word,
)


class TestNormalize:
    """Tests for text normalization."""

    def test_lowercase(self) -> bool:
        assert normalize("HELLO WORLD") == "hello world"
        assert normalize("HeLLo WoRLD") == "hello world"
        return False

    def test_punctuation_removal(self) -> bool:
        assert normalize("What's the S&P 500 price?") == "whats the sp 500 price"
        assert normalize("Hello, world!") == "hello world"
        assert normalize("test@example.com") == "testexamplecom"
        return False

    def test_intra_word_hyphen_preserved(self) -> bool:
        assert normalize("well-known fact") == "well-known fact"
        assert normalize("state-of-the-art") == "state-of-the-art"
        return False

    def test_whitespace_collapse(self) -> bool:
        assert normalize("hello    world") == "hello world"
        assert normalize("  hello   world  ") == "hello world"
        assert normalize("hello\t\nworld") == "hello world"
        return False

    def test_restore_capture_case_uses_original_proper_name(self) -> bool:
        assert restore_capture_case(["robin"], "My name is Robin.") == ["Robin"]
        return False

    def test_restore_capture_case_handles_multiple_words(self) -> bool:
        assert restore_capture_case(["mary jane"], "Please call me Mary Jane!") == ["Mary Jane"]
        return False

    def test_empty_string(self) -> bool:
        assert normalize("") == ""
        assert normalize("   ") == ""
        return False

    def test_numbers_preserved(self) -> bool:
        assert normalize("Python 3.12") == "python 312"
        assert normalize("100 dollars") == "100 dollars"
        return False

    def test_spec_example(self) -> bool:
        # Example from spec
        assert normalize("What's the S&P 500 price?") == "whats the sp 500 price"
        return False


class TestExtractKeywords:
    """Tests for keyword extraction."""

    def test_basic_extraction(self) -> bool:
        result = extract_keywords("whats the capital france", DEFAULT_STOPWORDS)
        assert "whats" in result
        assert "capital" in result
        assert "france" in result
        assert "the" not in result
        return False

    def test_stopword_removal(self) -> bool:
        result = extract_keywords("the quick brown fox", DEFAULT_STOPWORDS)
        assert "the" not in result
        assert "quick" in result
        assert "brown" in result
        assert "fox" in result
        return False

    def test_deduplication(self) -> bool:
        result = extract_keywords("paris paris paris", DEFAULT_STOPWORDS)
        assert result == ["paris"]
        return False

    def test_order_preserved(self) -> bool:
        result = extract_keywords("alpha beta gamma", DEFAULT_STOPWORDS)
        assert result == ["alpha", "beta", "gamma"]
        return False

    def test_empty_input(self) -> bool:
        assert extract_keywords("", DEFAULT_STOPWORDS) == []
        return False

    def test_only_stopwords(self) -> bool:
        result = extract_keywords("the is are was", DEFAULT_STOPWORDS)
        assert result == []
        return False

    def test_custom_stopwords(self) -> bool:
        custom = {"custom", "stop"}
        result = extract_keywords("custom stop word", custom)
        assert result == ["word"]
        return False


class TestExpandQuery:
    """Tests for query expansion."""

    def test_expands_with_nouns_only(self) -> bool:
        # Only the previous response's nouns are appended -- the referents a
        # pronoun can point back to -- not its stopwords and verbs.
        result = expand_query("What is its population?", "Paris is the capital of France")
        assert result == "What is population? Paris capital France"
        return False

    def test_expansion_skips_non_referents(self) -> bool:
        result = expand_query("Why is that?", "The tower was built quickly in Paris")
        assert "Paris" in result
        assert "tower" in result
        assert "quickly" not in result
        assert "built" not in result
        return False

    def test_expansion_falls_back_when_no_nouns(self) -> bool:
        # A response with nothing taggable as a noun falls back to appending
        # the full response rather than dropping context entirely.
        result = expand_query("What is that?", "Very quickly")
        assert result == "What is Very quickly"
        return False

    def test_no_expansion_without_referring_pronoun(self) -> bool:
        # A self-contained query must not inherit the previous response's
        # nouns -- they would dilute its own keywords.
        result = expand_query("Is the sky blue?", "Cats are mammals")
        assert result == "Is the sky blue?"
        assert expand_query("why why why", "Alright then") == "why why why"
        return False

    def test_empty_previous_response(self) -> bool:
        assert expand_query("Hello world", "") == "Hello world"
        return False

    def test_none_like_empty(self) -> bool:
        assert expand_query("Hello", "") == "Hello"
        return False

    def test_spec_example(self) -> bool:
        # Example from spec
        query = "What is its population?"
        previous = "Paris is the capital of France"
        result = expand_query(query, previous)
        assert "population" in result
        assert "Paris" in result
        assert "capital" in result
        assert "France" in result
        return False


class TestStemming:
    """Tests for stemming functions."""

    def test_stem_word_basic(self) -> bool:
        """Test basic stemming."""
        assert stem_word("running") == "run"
        assert stem_word("cats") == "cat"
        assert stem_word("jumped") == "jump"
        return False

    def test_stem_word_case_insensitive(self) -> bool:
        """Test stemming is case-insensitive."""
        assert stem_word("Running") == "run"
        assert stem_word("CATS") == "cat"
        return False

    def test_stem_word_already_stemmed(self) -> bool:
        """Test words that are already in stem form."""
        assert stem_word("run") == "run"
        assert stem_word("cat") == "cat"
        return False

    def test_stem_text(self) -> bool:
        """Test stemming entire text."""
        result = stem_text("the cats are running quickly")
        assert "cat" in result
        assert "run" in result
        return False

    def test_normalize_with_stemming(self) -> bool:
        """Test combined normalization and stemming."""
        result = normalize_with_stemming("The CATS are RUNNING!")
        # Should be lowercase, no punctuation, and stemmed
        assert "cat" in result
        assert "run" in result
        assert "!" not in result
        return False


class TestLemmatization:
    """Tests for lemmatization functions."""

    def test_lemmatize_noun(self) -> bool:
        """Test lemmatizing nouns."""
        assert lemmatize_word("cats", "n") == "cat"
        assert lemmatize_word("dogs", "n") == "dog"
        assert lemmatize_word("children", "n") == "child"
        return False

    def test_lemmatize_verb(self) -> bool:
        """Test lemmatizing verbs."""
        assert lemmatize_word("running", "v") == "run"
        assert lemmatize_word("ran", "v") == "run"
        return False

    def test_lemmatize_adjective(self) -> bool:
        """Test lemmatizing adjectives."""
        assert lemmatize_word("better", "a") == "good"  # WordNet handles comparatives
        assert lemmatize_word("fastest", "a") == "fast"
        return False

    def test_lemmatize_default_noun(self) -> bool:
        """Test default POS is noun."""
        assert lemmatize_word("cats") == "cat"
        return False


class TestCorrectSpelling:
    """Tests for store-vocabulary spelling correction."""

    def test_corrects_transposition_typo(self) -> bool:
        vocabulary = {"about", "capital", "france"}
        assert correct_spelling("tell me abotu france", vocabulary) == "tell me about france"
        return False

    def test_never_corrects_real_english_words(self) -> bool:
        # "abort" is not in the store, but it is a real word -- leave it alone.
        vocabulary = {"about"}
        assert correct_spelling("abort", vocabulary) == "abort"
        return False

    def test_never_corrects_short_tokens(self) -> bool:
        vocabulary = {"cat"}
        assert correct_spelling("cta", vocabulary) == "cta"
        return False

    def test_keeps_vocabulary_tokens(self) -> bool:
        vocabulary = {"about", "capital"}
        assert correct_spelling("about capital", vocabulary) == "about capital"
        return False

    def test_ambiguous_candidates_left_alone(self) -> bool:
        # Two vocabulary words at the same distance: do not guess.
        vocabulary = {"gramx", "gramy"}
        assert correct_spelling("gramz", vocabulary) == "gramz"
        return False

    def test_empty_vocabulary_is_no_op(self) -> bool:
        assert correct_spelling("abotu anything", set()) == "abotu anything"
        return False


class TestFirstClause:
    """Tests for clause trimming."""

    def test_cuts_new_subject_verb_clause(self) -> bool:
        assert first_clause("tired i have been working really hard") == "tired"
        return False

    def test_strips_dangling_conjunction(self) -> bool:
        assert first_clause("exhausted and i want to sleep") == "exhausted"
        return False

    def test_single_clause_unchanged(self) -> bool:
        assert first_clause("really happy about the results") == "really happy about the results"
        return False

    def test_short_capture_unchanged(self) -> bool:
        assert first_clause("alice") == "alice"
        assert first_clause("") == ""
        return False


class TestFirstClauseRelativeClauses:
    """A pronoun right after a noun is a relative clause, not a new sentence."""

    def test_keeps_relative_clause_after_noun(self) -> bool:
        assert first_clause("a friend you can trust") == "a friend you can trust"
        assert first_clause("the movie i saw yesterday") == "the movie i saw yesterday"
        return False

    def test_still_cuts_after_non_noun(self) -> bool:
        assert first_clause("tired i have been working really hard") == "tired"
        return False


class TestStemShortTokens:
    def test_short_tokens_not_stemmed(self) -> bool:
        assert stem_text("his") == "his"
        assert stem_text("was") == "was"
        return False

    def test_longer_tokens_still_stem(self) -> bool:
        assert "run" in stem_text("running quickly")
        assert "cat" in stem_text("cats everywhere")
        return False


class TestSpellCorrectionInflections:
    """The English-word gate must recognize inflections the words corpus lacks."""

    def test_inflected_real_words_never_corrected(self) -> bool:
        # "died", "asking", "notes" are absent from the words corpus but are
        # real inflections; correcting them corrupts valid input.
        vocabulary = {"die", "sing", "note", "ask"}
        assert correct_spelling("my hard drive died", vocabulary) == "my hard drive died"
        assert correct_spelling("thanks for asking", vocabulary) == "thanks for asking"
        assert correct_spelling("i lost my notes", vocabulary) == "i lost my notes"
        return False

    def test_genuine_typos_still_corrected(self) -> bool:
        vocabulary = {"gravity", "about"}
        assert correct_spelling("gravty is strong", vocabulary) == "gravity is strong"
        return False

    def test_is_known_word_covers_lemmas(self) -> bool:
        assert is_known_word("died")
        assert is_known_word("tests")
        assert is_known_word("working")
        assert not is_known_word("waht")
        assert not is_known_word("gravty")
        return False


class TestExtractName:
    """Tests for name extraction from self-introduction captures."""

    def test_strips_filler_and_trailing_markers(self) -> bool:
        assert extract_name("still jason by the way") == "jason"
        assert extract_name("actually bob") == "bob"
        return False

    def test_plain_and_multiword_names_kept(self) -> bool:
        assert extract_name("jason") == "jason"
        assert extract_name("mary jane") == "mary jane"
        return False

    def test_falls_back_to_input_when_nothing_namelike(self) -> bool:
        assert extract_name("12345") == "12345"
        assert extract_name("") == ""
        return False


class TestFirstClauseQuestionBoundary:
    def test_cuts_at_embedded_question(self) -> bool:
        assert first_clause("the sky what is the moon") == "the sky"
        return False

    def test_relative_pronoun_after_noun_kept(self) -> bool:
        assert first_clause("the man who is tall") == "the man who is tall"
        return False
