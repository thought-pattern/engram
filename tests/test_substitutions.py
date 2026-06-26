"""Tests for text substitution maps."""

from engram.substitutions import (
    DEFAULT_CONTRACTIONS,
    DEFAULT_GENDER,
    DEFAULT_PERSON,
    DEFAULT_PERSON2,
    apply_gender,
    apply_person,
    apply_person2,
    apply_substitutions,
    expand_contractions,
    get_all_input_subs,
    normalize_for_matching,
    split_sentences,
    substitution_maps,
)


class TestApplySubstitutions:
    """Tests for apply_substitutions function."""

    def test_basic_substitution(self):
        """Test basic word substitution."""
        subs = {"hello": "hi", "world": "earth"}
        result = apply_substitutions("hello world", subs)
        assert result == "hi earth"

    def test_case_preservation_lower(self):
        """Test lowercase is preserved."""
        subs = {"hello": "hi"}
        result = apply_substitutions("hello", subs)
        assert result == "hi"

    def test_case_preservation_upper(self):
        """Test uppercase is preserved."""
        subs = {"hello": "hi"}
        result = apply_substitutions("HELLO", subs)
        assert result == "HI"

    def test_case_preservation_title(self):
        """Test title case is preserved."""
        subs = {"hello": "hi there"}
        result = apply_substitutions("Hello", subs)
        assert result == "Hi there"

    def test_punctuation_preserved(self):
        """Test punctuation around words is preserved."""
        subs = {"hello": "hi"}
        result = apply_substitutions("hello!", subs)
        assert result == "hi!"

        result = apply_substitutions("(hello)", subs)
        assert result == "(hi)"

        result = apply_substitutions('"hello"', subs)
        assert result == '"hi"'

    def test_no_partial_match(self):
        """Test that only whole words are matched."""
        subs = {"the": "a"}
        result = apply_substitutions("there", subs)
        assert result == "there"  # Not "aere"

    def test_empty_text(self):
        """Test empty text returns empty."""
        subs = {"hello": "hi"}
        result = apply_substitutions("", subs)
        assert result == ""

    def test_empty_subs(self):
        """Test empty substitution map returns original."""
        result = apply_substitutions("hello world", {})
        assert result == "hello world"

    def test_no_match(self):
        """Test text with no matches returns original."""
        subs = {"hello": "hi"}
        result = apply_substitutions("goodbye world", subs)
        assert result == "goodbye world"


class TestExpandContractions:
    """Tests for contractions expansion."""

    def test_common_contractions(self):
        """Test common contractions are expanded."""
        assert "i am" in expand_contractions("I'm").lower()
        assert "do not" in expand_contractions("don't").lower()
        assert "cannot" in expand_contractions("can't").lower()
        assert "will not" in expand_contractions("won't").lower()
        assert "is not" in expand_contractions("isn't").lower()

    def test_contraction_in_sentence(self):
        """Test contraction expansion in full sentence."""
        result = expand_contractions("I'm going to the store")
        assert "i am" in result.lower()

    def test_multiple_contractions(self):
        """Test multiple contractions in one text."""
        result = expand_contractions("I'm sure you're right")
        assert "i am" in result.lower()
        assert "you are" in result.lower()

    def test_case_preserved(self):
        """Test case is preserved after expansion."""
        result = expand_contractions("I'M HAPPY")
        assert "I AM" in result

    def test_custom_contractions(self):
        """Test custom contractions map."""
        custom = {"howdy": "hello there"}
        result = expand_contractions("Howdy partner", custom)
        assert "Hello there" in result

    def test_no_contractions(self):
        """Test text without contractions."""
        text = "Hello there friend"
        result = expand_contractions(text)
        assert result == text


class TestApplyPerson:
    """Tests for person substitution (I -> you)."""

    def test_basic_person(self):
        """Test basic person substitution."""
        result = apply_person("I am happy")
        assert "you" in result.lower()
        assert "are" in result.lower()

    def test_my_to_your(self):
        """Test my -> your."""
        result = apply_person("my name is Bob")
        assert "your" in result.lower()

    def test_me_to_you(self):
        """Test me -> you."""
        result = apply_person("help me please")
        assert "you" in result.lower()

    def test_custom_person_map(self):
        """Test custom person map."""
        custom = {"we": "they"}
        result = apply_person("we are here", custom)
        assert "they" in result.lower()


class TestApplyPerson2:
    """Tests for person2 substitution (you -> I)."""

    def test_basic_person2(self):
        """Test basic person2 substitution."""
        result = apply_person2("you are happy")
        assert "i" in result.lower()

    def test_your_to_my(self):
        """Test your -> my."""
        result = apply_person2("your name is Bob")
        assert "my" in result.lower()

    def test_custom_person2_map(self):
        """Test custom person2 map."""
        custom = {"they": "we"}
        result = apply_person2("they are here", custom)
        assert "we" in result.lower()


class TestApplyGender:
    """Tests for gender substitution (he <-> she)."""

    def test_he_to_she(self):
        """Test he -> she."""
        result = apply_gender("he is here")
        assert "she" in result.lower()

    def test_she_to_he(self):
        """Test she -> he."""
        result = apply_gender("she is here")
        assert "he" in result.lower()

    def test_him_her(self):
        """Test him <-> her."""
        result = apply_gender("I saw him")
        assert "her" in result.lower()

        result = apply_gender("I saw her")
        assert "him" in result.lower()

    def test_himself_herself(self):
        """Test himself <-> herself."""
        result = apply_gender("he did it himself")
        assert "herself" in result.lower()

    def test_custom_gender_map(self):
        """Test custom gender map."""
        custom = {"male": "female", "female": "male"}
        result = apply_gender("the male cat", custom)
        assert "female" in result.lower()


class TestSplitSentences:
    """Tests for sentence splitting."""

    def test_single_sentence(self):
        """Test single sentence without punctuation."""
        result = split_sentences("Hello world")
        assert result == ["Hello world"]

    def test_period_split(self):
        """Test splitting on period (NLTK preserves punctuation)."""
        result = split_sentences("Hello. World.")
        assert result == ["Hello.", "World."]

    def test_exclamation_split(self):
        """Test splitting on exclamation mark (NLTK preserves punctuation)."""
        result = split_sentences("Hello! World!")
        assert result == ["Hello!", "World!"]

    def test_question_split(self):
        """Test splitting on question mark (NLTK preserves punctuation)."""
        result = split_sentences("Hello? World?")
        assert result == ["Hello?", "World?"]

    def test_mixed_punctuation(self):
        """Test splitting on mixed punctuation (NLTK preserves punctuation)."""
        result = split_sentences("Hi! How are you? I'm fine.")
        assert result == ["Hi!", "How are you?", "I'm fine."]

    def test_multiple_punctuation(self):
        """Test multiple consecutive punctuation marks."""
        result = split_sentences("Really?! Yes!")
        assert len(result) >= 2
        assert "Really" in result[0]
        assert "Yes" in result[1]

    def test_empty_string(self):
        """Test empty string returns empty list."""
        result = split_sentences("")
        assert result == []

    def test_whitespace_trimmed(self):
        """Test whitespace is trimmed from sentences."""
        result = split_sentences("  Hello.   World.  ")
        assert result == ["Hello.", "World."]

    def test_abbreviations_handled(self):
        """Test NLTK handles abbreviations correctly."""
        result = split_sentences("Dr. Smith went home. He was tired.")
        assert len(result) == 2
        assert "Dr. Smith" in result[0]

    def test_decimals_handled(self):
        """Test NLTK handles decimal numbers correctly."""
        result = split_sentences("The value is 3.14. That's pi.")
        assert len(result) == 2


class TestNormalizeForMatching:
    """Tests for normalize_for_matching function."""

    def test_expands_contractions(self):
        """Test contractions are expanded by default."""
        result = normalize_for_matching("I'm here")
        assert "i am" in result.lower()

    def test_skip_contractions(self):
        """Test contractions expansion can be skipped."""
        result = normalize_for_matching("I'm here", expand_contr=False)
        assert "i'm" in result.lower()

    def test_normalizes_whitespace(self):
        """Test whitespace is normalized."""
        result = normalize_for_matching("hello   world")
        assert result == "hello world"


class TestSubstitutionMaps:
    """Tests for SubstitutionMaps class."""

    def test_default_maps(self):
        """Test default maps are initialized."""
        maps = substitution_maps()
        assert len(maps["contractions"]) > 0
        assert len(maps["person"]) > 0
        assert len(maps["person2"]) > 0
        assert len(maps["gender"]) > 0
        assert len(maps["custom"]) == 0

    def test_get_all_input_subs(self):
        """Test get_all_input_subs combines maps."""
        maps = substitution_maps()
        maps["custom"]["foo"] = "bar"

        all_subs = get_all_input_subs(maps)
        assert "don't" in all_subs
        assert "foo" in all_subs

    def test_custom_maps_override(self):
        """Test custom maps can be provided."""
        custom_contractions = {"yo": "hello"}
        maps = substitution_maps(contractions=custom_contractions)
        assert maps["contractions"] == {"yo": "hello"}


class TestDefaultMaps:
    """Tests for default substitution maps."""

    def test_contractions_completeness(self):
        """Test that common contractions are present."""
        common = ["i'm", "don't", "can't", "won't", "isn't", "aren't"]
        for c in common:
            assert c in DEFAULT_CONTRACTIONS

    def test_person_map_completeness(self):
        """Test that common person substitutions are present."""
        common = ["i", "me", "my", "mine", "myself"]
        for p in common:
            assert p in DEFAULT_PERSON

    def test_person2_map_completeness(self):
        """Test that common person2 substitutions are present."""
        common = ["you", "your", "yours", "yourself"]
        for p in common:
            assert p in DEFAULT_PERSON2

    def test_gender_map_completeness(self):
        """Test that common gender substitutions are present."""
        common = ["he", "she", "him", "her", "his", "hers"]
        for g in common:
            assert g in DEFAULT_GENDER


class TestContractionIntegration:
    """Integration tests for contractions with Engram."""

    def test_contractions_expanded_in_pattern_query(self):
        """Test contractions are expanded before pattern matching."""
        from engram.core import Engram

        engram = Engram()
        engram.store("I know you do not like pizza", pattern="I KNOW YOU DO NOT LIKE *")

        # Query with contraction - should match expanded pattern
        result = engram.pattern_query("I know you don't like pizza")
        assert result
        assert "pizza" in result[1][0].lower()

    def test_contractions_disabled(self):
        """Test contractions expansion can be disabled."""
        from engram.config import engram_config
        from engram.core import Engram

        config = engram_config(expand_contractions=False)
        engram = Engram(config=config)
        engram.store("You do not like pizza", pattern="YOU DO NOT LIKE *")

        # Query with contraction - should NOT match since expansion disabled
        result = engram.pattern_query("you don't like pizza")
        assert not result  # Won't match because "don't" != "do not"
