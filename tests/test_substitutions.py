"""Tests for text substitution maps."""

from engram.substitutions import (
    apply_gender,
    apply_person,
    apply_person2,
    apply_substitutions,
    expand_contractions,
    normalize_for_matching,
    split_sentences,
    substitution_maps,
)

"""Tests for apply_substitutions function."""


def test_apply_substitutions_basic_substitution():
    """Test basic word substitution."""
    subs = {"hello": "hi", "world": "earth"}
    result = apply_substitutions("hello world", subs)
    assert result == "hi earth"


def test_apply_substitutions_case_preservation_lower():
    """Test lowercase is preserved."""
    subs = {"hello": "hi"}
    result = apply_substitutions("hello", subs)
    assert result == "hi"


def test_apply_substitutions_case_preservation_upper():
    """Test uppercase is preserved."""
    subs = {"hello": "hi"}
    result = apply_substitutions("HELLO", subs)
    assert result == "HI"


def test_apply_substitutions_case_preservation_title():
    """Test title case is preserved."""
    subs = {"hello": "hi there"}
    result = apply_substitutions("Hello", subs)
    assert result == "Hi there"


def test_apply_substitutions_punctuation_preserved():
    """Test punctuation around words is preserved."""
    subs = {"hello": "hi"}
    result = apply_substitutions("hello!", subs)
    assert result == "hi!"

    result = apply_substitutions("(hello)", subs)
    assert result == "(hi)"

    result = apply_substitutions('"hello"', subs)
    assert result == '"hi"'


def test_apply_substitutions_no_partial_match():
    """Test that only whole words are matched."""
    subs = {"the": "a"}
    result = apply_substitutions("there", subs)
    assert result == "there"  # Not "aere"


def test_apply_substitutions_empty_text():
    """Test empty text returns empty."""
    subs = {"hello": "hi"}
    result = apply_substitutions("", subs)
    assert result == ""


def test_apply_substitutions_empty_subs():
    """Test empty substitution map returns original."""
    result = apply_substitutions("hello world", {})
    assert result == "hello world"


def test_apply_substitutions_no_match():
    """Test text with no matches returns original."""
    subs = {"hello": "hi"}
    result = apply_substitutions("goodbye world", subs)
    assert result == "goodbye world"


"""Tests for contractions expansion."""


def test_expand_contractions_common_contractions():
    """Test common contractions are expanded."""
    assert "i am" in expand_contractions("I'm").lower()
    assert "do not" in expand_contractions("don't").lower()
    assert "cannot" in expand_contractions("can't").lower()
    assert "will not" in expand_contractions("won't").lower()
    assert "is not" in expand_contractions("isn't").lower()


def test_expand_contractions_contraction_in_sentence():
    """Test contraction expansion in full sentence."""
    result = expand_contractions("I'm going to the store")
    assert "i am" in result.lower()


def test_expand_contractions_multiple_contractions():
    """Test multiple contractions in one text."""
    result = expand_contractions("I'm sure you're right")
    assert "i am" in result.lower()
    assert "you are" in result.lower()


def test_expand_contractions_case_preserved():
    """Test case is preserved after expansion."""
    result = expand_contractions("I'M HAPPY")
    assert "I AM" in result


def test_expand_contractions_custom_contractions():
    """Test custom contractions map."""
    custom = {"howdy": "hello there"}
    result = expand_contractions("Howdy partner", custom)
    assert "Hello there" in result


def test_expand_contractions_no_contractions():
    """Test text without contractions."""
    text = "Hello there friend"
    result = expand_contractions(text)
    assert result == text


"""Tests for person substitution (I -> you)."""


def test_apply_person_basic_person():
    """Test basic person substitution."""
    result = apply_person("I am happy")
    assert "you" in result.lower()
    assert "are" in result.lower()


def test_apply_person_my_to_your():
    """Test my -> your."""
    result = apply_person("my name is Bob")
    assert "your" in result.lower()


def test_apply_person_me_to_you():
    """Test me -> you."""
    result = apply_person("help me please")
    assert "you" in result.lower()


def test_apply_person_custom_person_map():
    """Test custom person map."""
    custom = {"we": "they"}
    result = apply_person("we are here", custom)
    assert "they" in result.lower()


"""Tests for person2 substitution (you -> I)."""


def test_apply_person2_basic_person2():
    """Test basic person2 substitution."""
    result = apply_person2("you are happy")
    assert "i" in result.lower()


def test_apply_person2_your_to_my():
    """Test your -> my."""
    result = apply_person2("your name is Bob")
    assert "my" in result.lower()


def test_apply_person2_custom_person2_map():
    """Test custom person2 map."""
    custom = {"they": "we"}
    result = apply_person2("they are here", custom)
    assert "we" in result.lower()


"""Tests for gender substitution (gendered pronouns -> singular they/them)."""


def test_apply_gender_he_to_they():
    """Test he -> they."""
    result = apply_gender("he is here")
    assert "they" in result.lower()


def test_apply_gender_she_to_they():
    """Test she -> they."""
    result = apply_gender("she is here")
    assert "they" in result.lower()


def test_apply_gender_him_her_to_them():
    """Test him -> them and her -> them."""
    result = apply_gender("I saw him")
    assert "them" in result.lower()

    result = apply_gender("I saw her")
    assert "them" in result.lower()


def test_apply_gender_himself_herself_to_themself():
    """Test himself -> themself and herself -> themself."""
    result = apply_gender("he did it himself")
    assert "themself" in result.lower()


def test_apply_gender_custom_gender_map():
    """Test custom gender map."""
    custom = {"male": "female", "female": "male"}
    result = apply_gender("the male cat", custom)
    assert "female" in result.lower()


"""Tests for sentence splitting."""


def test_split_sentences_single_sentence():
    """Test single sentence without punctuation."""
    result = split_sentences("Hello world")
    assert result == ["Hello world"]


def test_split_sentences_period_split():
    """Test splitting on period (NLTK preserves punctuation)."""
    result = split_sentences("Hello. World.")
    assert result == ["Hello.", "World."]


def test_split_sentences_exclamation_split():
    """Test splitting on exclamation mark (NLTK preserves punctuation)."""
    result = split_sentences("Hello! World!")
    assert result == ["Hello!", "World!"]


def test_split_sentences_question_split():
    """Test splitting on question mark (NLTK preserves punctuation)."""
    result = split_sentences("Hello? World?")
    assert result == ["Hello?", "World?"]


def test_split_sentences_mixed_punctuation():
    """Test splitting on mixed punctuation (NLTK preserves punctuation)."""
    result = split_sentences("Hi! How are you? I'm fine.")
    assert result == ["Hi!", "How are you?", "I'm fine."]


def test_split_sentences_multiple_punctuation():
    """Test multiple consecutive punctuation marks."""
    result = split_sentences("Really?! Yes!")
    assert len(result) >= 2
    assert "Really" in result[0]
    assert "Yes" in result[1]


def test_split_sentences_empty_string():
    """Test empty string returns empty list."""
    result = split_sentences("")
    assert result == []


def test_split_sentences_whitespace_trimmed():
    """Test whitespace is trimmed from sentences."""
    result = split_sentences("  Hello.   World.  ")
    assert result == ["Hello.", "World."]


def test_split_sentences_abbreviations_handled():
    """Test NLTK handles abbreviations correctly."""
    result = split_sentences("Dr. Smith went home. He was tired.")
    assert len(result) == 2
    assert "Dr. Smith" in result[0]


def test_split_sentences_decimals_handled():
    """Test NLTK handles decimal numbers correctly."""
    result = split_sentences("The value is 3.14. That's pi.")
    assert len(result) == 2


"""Tests for normalize_for_matching function."""


def test_normalize_for_matching_expands_contractions():
    """Test contractions are expanded by default."""
    result = normalize_for_matching("I'm here")
    assert "i am" in result.lower()


def test_normalize_for_matching_skip_contractions():
    """Test contractions expansion can be skipped."""
    result = normalize_for_matching("I'm here", expand_contr=False)
    assert "i'm" in result.lower()


def test_normalize_for_matching_normalizes_whitespace():
    """Test whitespace is normalized."""
    result = normalize_for_matching("hello   world")
    assert result == "hello world"


"""Tests for SubstitutionMaps class."""


def test_substitution_maps_custom_maps_override():
    """Test custom maps can be provided."""
    custom_contractions = {"yo": "hello"}
    maps = substitution_maps(contractions=custom_contractions)
    assert maps["contractions"] == {"yo": "hello"}


"""Integration tests for contractions with Engram."""


def test_contraction_integration_contractions_expanded_in_pattern_query():
    """Test contractions are expanded before pattern matching."""
    from engram.core import Engram

    engram = Engram()
    engram.store("I know you do not like pizza", pattern="I KNOW YOU DO NOT LIKE *")

    # Query with contraction - should match expanded pattern
    result = engram.pattern_query("I know you don't like pizza")
    assert result
    assert "pizza" in result[1][0].lower()


def test_contraction_integration_contractions_disabled():
    """Test contractions expansion can be disabled."""
    from engram.config import engram_config
    from engram.core import Engram

    config = engram_config(expand_contractions=False)
    engram = Engram(config=config)
    engram.store("You do not like pizza", pattern="YOU DO NOT LIKE *")

    # Query with contraction - should NOT match since expansion disabled
    result = engram.pattern_query("you don't like pizza")
    assert not result  # Won't match because "don't" != "do not"
