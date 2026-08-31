"""Tests for AIML-style pattern matching."""

from engram.pattern import PatternMatcher, match_pattern, normalize_pattern, pattern_to_regex

"""Tests for normalize_pattern function."""


def test_normalize_pattern_lowercase():
    assert normalize_pattern("HELLO") == "hello"


def test_normalize_pattern_preserve_star_wildcard():
    assert normalize_pattern("HELLO *") == "hello *"


def test_normalize_pattern_preserve_underscore_wildcard():
    assert normalize_pattern("HELLO _") == "hello _"


def test_normalize_pattern_preserve_hash_wildcard():
    assert normalize_pattern("HELLO #") == "hello #"


def test_normalize_pattern_preserve_caret_wildcard():
    assert normalize_pattern("HELLO ^") == "hello ^"


def test_normalize_pattern_multiple_wildcards():
    assert normalize_pattern("* HELLO *") == "* hello *"


def test_normalize_pattern_multiple_zero_wildcards():
    assert normalize_pattern("# HELLO ^") == "# hello ^"


def test_normalize_pattern_remove_punctuation():
    assert normalize_pattern("HELLO!") == "hello"


def test_normalize_pattern_collapse_whitespace():
    assert normalize_pattern("HELLO   WORLD") == "hello world"


"""Tests for pattern_to_regex function."""


def test_pattern_to_regex_exact_word():
    regex, _ = pattern_to_regex("HELLO")
    assert regex.match("hello")
    assert not regex.match("hello world")


def test_pattern_to_regex_wildcard_star():
    regex, score = pattern_to_regex("HELLO *")
    _, exact_score = pattern_to_regex("HELLO WORLD")
    assert score < exact_score
    assert regex.match("hello world")
    assert regex.match("hello there friend")
    assert not regex.match("hello")  # * needs at least one word


def test_pattern_to_regex_empty_pattern():
    regex, score = pattern_to_regex("")
    assert score == 0


"""Tests for match_pattern function."""


def test_match_pattern_exact_match():
    result = match_pattern("HELLO", "hello")
    assert result["matched"] is True
    assert result["score"] > 0
    assert result["captured"] == []


def test_match_pattern_exact_match_case_insensitive():
    result = match_pattern("HELLO", "HeLLo")
    assert result["matched"] is True


def test_match_pattern_wildcard_capture():
    result = match_pattern("HELLO *", "hello world")
    assert result["matched"] is True
    assert result["captured"] == ["world"]


def test_match_pattern_wildcard_capture_multiple_words():
    result = match_pattern("HELLO *", "hello there my friend")
    assert result["matched"] is True
    assert result["captured"] == ["there my friend"]


def test_match_pattern_no_match():
    result = match_pattern("HELLO", "goodbye")
    assert result["matched"] is False


def test_match_pattern_partial_no_match():
    result = match_pattern("HELLO WORLD", "hello")
    assert result["matched"] is False


def test_match_pattern_catchall():
    result = match_pattern("*", "anything at all")
    assert result["matched"] is True
    assert result["captured"] == ["anything at all"]


def test_match_pattern_wildcard_at_start():
    result = match_pattern("* WORLD", "hello world")
    assert result["matched"] is True
    assert result["captured"] == ["hello"]


def test_match_pattern_wildcard_in_middle():
    result = match_pattern("HELLO * WORLD", "hello beautiful world")
    assert result["matched"] is True
    assert result["captured"] == ["beautiful"]


"""Tests for # and ^ wildcards (zero or more words)."""


def test_zero_or_more_wildcards_hash_matches_zero_words():
    """# should match zero words."""
    result = match_pattern("HELLO # WORLD", "hello world")
    assert result["matched"] is True
    assert result["captured"] == [""]


def test_zero_or_more_wildcards_hash_matches_one_word():
    """# should match one word."""
    result = match_pattern("HELLO # WORLD", "hello beautiful world")
    assert result["matched"] is True
    assert result["captured"] == ["beautiful"]


def test_zero_or_more_wildcards_hash_matches_multiple_words():
    """# should match multiple words."""
    result = match_pattern("HELLO # WORLD", "hello very beautiful world")
    assert result["matched"] is True
    assert result["captured"] == ["very beautiful"]


def test_zero_or_more_wildcards_caret_matches_zero_words():
    """^ should match zero words."""
    result = match_pattern("HELLO ^ WORLD", "hello world")
    assert result["matched"] is True
    assert result["captured"] == [""]


def test_zero_or_more_wildcards_caret_matches_one_word():
    """^ should match one word."""
    result = match_pattern("HELLO ^ WORLD", "hello beautiful world")
    assert result["matched"] is True
    assert result["captured"] == ["beautiful"]


def test_zero_or_more_wildcards_hash_at_start():
    """# at start matches zero or more."""
    result = match_pattern("# WORLD", "world")
    assert result["matched"] is True
    assert result["captured"] == [""]

    result = match_pattern("# WORLD", "hello world")
    assert result["matched"] is True
    assert result["captured"] == ["hello"]


def test_zero_or_more_wildcards_hash_at_end():
    """# at end matches zero or more."""
    result = match_pattern("HELLO #", "hello")
    assert result["matched"] is True
    assert result["captured"] == [""]

    result = match_pattern("HELLO #", "hello world")
    assert result["matched"] is True
    assert result["captured"] == ["world"]


def test_zero_or_more_wildcards_caret_at_start():
    """^ at start matches zero or more."""
    result = match_pattern("^ WORLD", "world")
    assert result["matched"] is True
    assert result["captured"] == [""]

    result = match_pattern("^ WORLD", "hello world")
    assert result["matched"] is True
    assert result["captured"] == ["hello"]


def test_zero_or_more_wildcards_caret_at_end():
    """^ at end matches zero or more."""
    result = match_pattern("HELLO ^", "hello")
    assert result["matched"] is True
    assert result["captured"] == [""]

    result = match_pattern("HELLO ^", "hello world")
    assert result["matched"] is True
    assert result["captured"] == ["world"]


def test_zero_or_more_wildcards_hash_only_pattern():
    """# alone should match anything including empty."""
    result = match_pattern("#", "hello world")
    assert result["matched"] is True
    assert result["captured"] == ["hello world"]

    empty_result = match_pattern("#", "")
    assert empty_result["matched"] is True
    assert empty_result["captured"] == [""]


def test_zero_or_more_wildcards_caret_priority_over_hash():
    """^ should have higher priority than #."""
    from engram.pattern import pattern_to_regex

    _, hash_score = pattern_to_regex("HELLO #")
    _, caret_score = pattern_to_regex("HELLO ^")
    assert caret_score > hash_score


def test_zero_or_more_wildcards_caret_priority_over_underscore():
    """^ should have higher priority than _."""
    from engram.pattern import pattern_to_regex

    _, underscore_score = pattern_to_regex("HELLO _")
    _, caret_score = pattern_to_regex("HELLO ^")
    assert caret_score > underscore_score


def test_zero_or_more_wildcards_star_requires_one_word():
    """* still requires at least one word."""
    result = match_pattern("HELLO * WORLD", "hello world")
    assert result["matched"] is False  # * needs at least one word


def test_zero_or_more_wildcards_underscore_requires_one_word():
    """_ still requires at least one word."""
    result = match_pattern("HELLO _ WORLD", "hello world")
    assert result["matched"] is False  # _ needs at least one word


"""Tests for PatternMatcher with # and ^ wildcards."""


def test_pattern_matcher_zero_wildcards_hash_pattern_match():
    """PatternMatcher should handle # correctly."""
    pm = PatternMatcher()
    pm.add_pattern("HELLO # WORLD", "Hash response")

    result = pm.match("hello world")
    assert result
    assert result[0] == "Hash response"
    assert result[1] == [""]

    result = pm.match("hello beautiful world")
    assert result
    assert result[0] == "Hash response"
    assert result[1] == ["beautiful"]


def test_pattern_matcher_zero_wildcards_caret_pattern_match():
    """PatternMatcher should handle ^ correctly."""
    pm = PatternMatcher()
    pm.add_pattern("HELLO ^ WORLD", "Caret response")

    result = pm.match("hello world")
    assert result
    assert result[0] == "Caret response"
    assert result[1] == [""]


def test_pattern_matcher_zero_wildcards_zero_wildcard_priority():
    """More specific patterns should win over zero wildcards."""
    pm = PatternMatcher()
    pm.add_pattern("HELLO WORLD", "Exact match")
    pm.add_pattern("HELLO # WORLD", "Hash match")
    pm.add_pattern("HELLO ^ WORLD", "Caret match")

    # Exact match should win
    result = pm.match("hello world")
    assert result[0] == "Exact match"


def test_pattern_matcher_zero_wildcards_caret_vs_hash_priority():
    """^ should win over # for same pattern."""
    pm = PatternMatcher()
    pm.add_pattern("HELLO #", "Hash match")
    pm.add_pattern("HELLO ^", "Caret match")

    # ^ should win (higher priority)
    result = pm.match("hello there")
    assert result[0] == "Caret match"


def test_pattern_matcher_zero_wildcards_hash_has_priority_over_star():
    """# should win over * when both patterns match."""
    pm = PatternMatcher()
    pm.add_pattern("HELLO #", "Zero match")
    pm.add_pattern("HELLO *", "One match")

    # For "hello" alone, only # matches
    result = pm.match("hello")
    assert result[0] == "Zero match"

    result = pm.match("hello world")
    assert result[0] == "Zero match"


"""Tests for PatternMatcher class."""


def test_pattern_matcher_add_and_match():
    pm = PatternMatcher()
    pm.add_pattern("HELLO", "Hello response")
    result = pm.match("hello")
    # Returns (response, captured, thatstars, topicstars, pattern, topic, that)
    assert result == ("Hello response", [], [], [], "HELLO", "", "")


def test_pattern_matcher_no_match_returns_none():
    pm = PatternMatcher()
    pm.add_pattern("HELLO", "Hello response")
    result = pm.match("goodbye")
    assert not result


def test_pattern_matcher_specificity_priority():
    pm = PatternMatcher()
    pm.add_pattern("HELLO", "Exact match")
    pm.add_pattern("HELLO *", "Wildcard match")

    # Exact should win over wildcard when input is just "hello"
    result = pm.match("hello")
    assert result == ("Exact match", [], [], [], "HELLO", "", "")

    # Wildcard matches when there's more
    result = pm.match("hello world")
    assert result == ("Wildcard match", ["world"], [], [], "HELLO *", "", "")


def test_pattern_matcher_more_specific_wins():
    pm = PatternMatcher()
    pm.add_pattern("HOW ARE YOU", "Three word match")
    pm.add_pattern("HOW ARE YOU DOING", "Four word match")
    pm.add_pattern("HOW ARE YOU *", "Wildcard match")

    result = pm.match("how are you")
    assert result == ("Three word match", [], [], [], "HOW ARE YOU", "", "")

    result = pm.match("how are you doing")
    assert result == ("Four word match", [], [], [], "HOW ARE YOU DOING", "", "")

    result = pm.match("how are you feeling")
    assert result == ("Wildcard match", ["feeling"], [], [], "HOW ARE YOU *", "", "")


def test_pattern_matcher_catchall_lowest_priority():
    pm = PatternMatcher()
    pm.add_pattern("*", "Catchall")
    pm.add_pattern("HELLO", "Hello")

    result = pm.match("hello")
    assert result == ("Hello", [], [], [], "HELLO", "", "")

    result = pm.match("anything else")
    assert result == ("Catchall", ["anything else"], [], [], "*", "", "")


def test_pattern_matcher_len():
    pm = PatternMatcher()
    assert len(pm) == 0
    pm.add_pattern("HELLO", "response")
    assert len(pm) == 1
    pm.add_pattern("WORLD", "response")
    assert len(pm) == 2


def test_pattern_matcher_clear():
    pm = PatternMatcher()
    pm.add_pattern("HELLO", "response")
    pm.clear()
    assert len(pm) == 0
    assert not pm.match("hello")


def test_pattern_matcher_clear_resets_lemmatized_index():
    """clear() must reset the lemmatized index too, or stale buckets point at recycled indices."""
    pm = PatternMatcher(use_lemmatization=True)
    pm.add_pattern("CATS ARE NICE", "old response")
    pm.clear()
    pm.add_pattern("DOGS BARK", "new response")

    # A stale 'cat' bucket would route this to the recycled index 0
    # (now DOGS BARK) or raise; a clean index simply finds no match.
    assert not pm.match("cats are nice")
    assert pm.match("dogs bark")[0] == "new response"


def test_pattern_matcher_remove_pattern():
    pm = PatternMatcher()
    pm.add_pattern("HELLO", "greeting")
    pm.add_pattern("GOODBYE", "farewell")

    assert pm.remove_pattern("HELLO")
    assert len(pm) == 1
    assert not pm.match("hello")
    # The surviving pattern still matches through the rebuilt index
    assert pm.match("goodbye")[0] == "farewell"


def test_pattern_matcher_remove_pattern_not_found():
    pm = PatternMatcher()
    pm.add_pattern("HELLO", "greeting")
    assert not pm.remove_pattern("MISSING")
    assert len(pm) == 1


def test_pattern_matcher_remove_pattern_respects_context():
    """Entries are keyed by (pattern, that, topic); removal must not take a sibling."""
    pm = PatternMatcher()
    pm.add_pattern("YES", "plain yes")
    pm.add_pattern("YES", "contextual yes", that="DO YOU AGREE")

    assert pm.remove_pattern("YES", that="DO YOU AGREE")
    assert len(pm) == 1
    # The context-free entry survives and still matches
    assert pm.match("yes")[0] == "plain yes"


def test_pattern_matcher_remove_pattern_rebuilds_wildcard_index():
    pm = PatternMatcher()
    pm.add_pattern("*", "catchall")
    pm.add_pattern("HELLO", "greeting")

    assert pm.remove_pattern("HELLO")
    # Wildcard entry survives at a shifted index and still matches
    assert pm.match("anything at all")[0] == "catchall"


def test_pattern_matcher_get_patterns():
    pm = PatternMatcher()
    pm.add_pattern("HELLO", "response1")
    pm.add_pattern("WORLD", "response2")
    patterns = pm.get_patterns()
    assert ("HELLO", "response1") in patterns
    assert ("WORLD", "response2") in patterns


"""Tests for pattern matching with user input containing punctuation."""


def test_pattern_matcher_with_punctuation_input_with_exclamation():
    pm = PatternMatcher()
    pm.add_pattern("HELLO", "Hello response")
    result = pm.match("Hello!")
    assert result == ("Hello response", [], [], [], "HELLO", "", "")


def test_pattern_matcher_with_punctuation_input_with_question_mark():
    pm = PatternMatcher()
    pm.add_pattern("HOW ARE YOU", "Fine thanks")
    result = pm.match("How are you?")
    assert result == ("Fine thanks", [], [], [], "HOW ARE YOU", "", "")


def test_pattern_matcher_with_punctuation_input_with_multiple_punctuation():
    pm = PatternMatcher()
    pm.add_pattern("WHAT IS YOUR NAME", "I am a bot")
    result = pm.match("What is your name?!")
    assert result == ("I am a bot", [], [], [], "WHAT IS YOUR NAME", "", "")


"""Tests for context-aware pattern matching (that/topic)."""


def test_pattern_matcher_context_matching_topic_filter_matches():
    """Pattern with topic should match when topic context matches."""
    pm = PatternMatcher()
    pm.add_pattern("WHAT IS IT", "Weather topic response", topic="WEATHER")
    pm.add_pattern("WHAT IS IT", "General response")

    # Without topic context, general response matches
    result = pm.match("what is it")
    assert result[0] == "General response"

    # With matching topic, topic-specific pattern wins
    result = pm.match("what is it", topic="weather")
    assert result[0] == "Weather topic response"


def test_pattern_matcher_context_matching_topic_filter_no_match():
    """Pattern with topic should not match when topic doesn't match."""
    pm = PatternMatcher()
    pm.add_pattern("HELLO", "Weather hello", topic="WEATHER")

    # No topic set - pattern shouldn't match
    result = pm.match("hello")
    assert not result

    # Wrong topic - pattern shouldn't match
    result = pm.match("hello", topic="sports")
    assert not result

    # Correct topic - should match
    result = pm.match("hello", topic="weather")
    assert result[0] == "Weather hello"


def test_pattern_matcher_context_matching_that_filter_matches():
    """Pattern with that should match when bot's last response matches."""
    pm = PatternMatcher()
    pm.add_pattern("YES", "Follow-up response", that="DO YOU LIKE PIZZA")
    pm.add_pattern("YES", "General yes")

    # Without that context, general response matches
    result = pm.match("yes")
    assert result[0] == "General yes"

    # With matching that context, context-specific pattern wins
    result = pm.match("yes", that="Do you like pizza?")
    assert result[0] == "Follow-up response"


def test_pattern_matcher_context_matching_that_filter_no_match():
    """Pattern with that should not match when last response doesn't match."""
    pm = PatternMatcher()
    pm.add_pattern("YES", "Pizza follow-up", that="DO YOU LIKE PIZZA")

    # No that context - pattern shouldn't match
    result = pm.match("yes")
    assert not result

    # Wrong that context - pattern shouldn't match
    result = pm.match("yes", that="how are you")
    assert not result


def test_pattern_matcher_context_matching_topic_and_that_combined():
    """Pattern with both topic and that should require both to match."""
    pm = PatternMatcher()
    pm.add_pattern("YES", "Full context response", topic="FOOD", that="DO YOU WANT MORE")
    pm.add_pattern("YES", "Topic only", topic="FOOD")
    pm.add_pattern("YES", "General yes")

    # Full context wins
    result = pm.match("yes", topic="food", that="do you want more")
    assert result[0] == "Full context response"

    # Topic only
    result = pm.match("yes", topic="food")
    assert result[0] == "Topic only"

    # No context
    result = pm.match("yes")
    assert result[0] == "General yes"


def test_pattern_matcher_context_matching_that_wildcard_capture():
    """Wildcards in that pattern should capture text."""
    pm = PatternMatcher()
    pm.add_pattern("YES", "Got thatstar", that="DO YOU LIKE *")

    result = pm.match("yes", that="do you like pizza")
    assert result
    assert result[0] == "Got thatstar"
    assert result[2] == ["pizza"]  # thatstars


def test_pattern_matcher_context_matching_topic_wildcard_capture():
    """Wildcards in topic pattern should capture text."""
    pm = PatternMatcher()
    pm.add_pattern("HELLO", "Got topicstar", topic="FAVORITE *")

    result = pm.match("hello", topic="favorite food")
    assert result
    assert result[0] == "Got topicstar"
    assert result[3] == ["food"]  # topicstars


def test_pattern_matcher_context_matching_context_priority():
    """More context constraints should give higher priority."""
    pm = PatternMatcher()
    pm.add_pattern("HI", "Topic+that", topic="GREETINGS", that="HELLO")
    pm.add_pattern("HI", "Topic only", topic="GREETINGS")
    pm.add_pattern("HI", "That only", that="HELLO")
    pm.add_pattern("HI", "No context")

    # Topic + that wins over topic only
    result = pm.match("hi", topic="greetings", that="hello")
    assert result[0] == "Topic+that"

    # Topic only wins over no context
    result = pm.match("hi", topic="greetings")
    assert result[0] == "Topic only"

    # That only wins over no context
    result = pm.match("hi", that="hello")
    assert result[0] == "That only"

    # No context gets lowest priority response
    result = pm.match("hi")
    assert result[0] == "No context"


def test_pattern_matcher_context_matching_get_patterns_with_context():
    """get_patterns_with_context should return all pattern info."""
    pm = PatternMatcher()
    pm.add_pattern("HELLO", "response1", that="HI", topic="GREET")
    pm.add_pattern("WORLD", "response2")

    patterns = pm.get_patterns_with_context()
    assert ("HELLO", "response1", "HI", "GREET") in patterns
    assert ("WORLD", "response2", "", "") in patterns


"""Tests for {set:name} pattern matching."""


def test_set_pattern_matching_set_match_basic():
    """Test basic set matching."""
    sets = {"color": ["red", "blue", "green"]}
    pm = PatternMatcher(sets=sets)
    pm.add_pattern("I LIKE {set:color}", "Nice color!")

    result = pm.match("i like blue")
    assert result
    assert result[0] == "Nice color!"
    assert result[1] == ["blue"]


def test_set_pattern_matching_set_match_captures_word():
    """Test that set match captures the matched word."""
    sets = {"greeting": ["hello", "hi", "hey"]}
    pm = PatternMatcher(sets=sets)
    pm.add_pattern("{set:greeting} THERE", "Greeting received")

    result = pm.match("hello there")
    assert result
    assert result[1] == ["hello"]

    result = pm.match("hi there")
    assert result
    assert result[1] == ["hi"]


def test_set_pattern_matching_set_match_case_insensitive():
    """Test that set matching is case-insensitive."""
    sets = {"color": ["red", "blue"]}
    pm = PatternMatcher(sets=sets)
    pm.add_pattern("I LIKE {set:color}", "Color matched")

    result = pm.match("I LIKE RED")
    assert result
    assert result[1] == ["red"]

    result = pm.match("i like BLUE")
    assert result
    assert result[1] == ["blue"]


def test_set_pattern_matching_set_no_match_if_word_not_in_set():
    """Test that non-set words don't match."""
    sets = {"color": ["red", "blue"]}
    pm = PatternMatcher(sets=sets)
    pm.add_pattern("I LIKE {set:color}", "Color matched")

    result = pm.match("i like purple")
    assert not result


def test_set_pattern_matching_unknown_set_no_match():
    """Test that unknown sets don't match anything."""
    pm = PatternMatcher(sets={})
    pm.add_pattern("I LIKE {set:unknown}", "Should not match")

    result = pm.match("i like anything")
    assert not result


def test_set_pattern_matching_set_with_wildcards():
    """Test set matching combined with wildcards."""
    sets = {"color": ["red", "blue", "green"]}
    pm = PatternMatcher(sets=sets)
    pm.add_pattern("* IS {set:color}", "Color described")

    result = pm.match("the sky is blue")
    assert result
    assert result[0] == "Color described"
    assert result[1] == ["the sky", "blue"]


def test_set_pattern_matching_set_priority_vs_wildcard():
    """Test that set match has higher priority than wildcard."""
    sets = {"color": ["red", "blue"]}
    pm = PatternMatcher(sets=sets)
    pm.add_pattern("I LIKE {set:color}", "Set match")
    pm.add_pattern("I LIKE *", "Wildcard match")

    result = pm.match("i like blue")
    assert result[0] == "Set match"

    result = pm.match("i like purple")
    assert result[0] == "Wildcard match"


def test_set_pattern_matching_multiple_sets_in_pattern():
    """Test multiple set references in one pattern."""
    sets = {
        "color": ["red", "blue"],
        "size": ["big", "small"],
    }
    pm = PatternMatcher(sets=sets)
    pm.add_pattern("A {set:size} {set:color} BALL", "Matched both")

    result = pm.match("a big red ball")
    assert result
    assert result[0] == "Matched both"
    assert result[1] == ["big", "red"]


"""Tests for {bot:name} pattern matching."""


def test_bot_pattern_matching_bot_match_basic():
    """Test basic bot property matching."""
    bot = {"name": "TestBot"}
    pm = PatternMatcher(bot_properties=bot)
    pm.add_pattern("YOUR NAME IS {bot:name}", "Yes it is!")

    result = pm.match("your name is testbot")
    assert result
    assert result[0] == "Yes it is!"


def test_bot_pattern_matching_bot_match_captures_value():
    """Test that bot match captures the matched value."""
    bot = {"name": "Alice", "version": "1.0"}
    pm = PatternMatcher(bot_properties=bot)
    pm.add_pattern("YOU ARE {bot:name}", "Correct!")

    result = pm.match("you are alice")
    assert result
    assert result[1] == ["alice"]


def test_bot_pattern_matching_bot_match_case_insensitive():
    """Test that bot matching is case-insensitive."""
    bot = {"name": "TestBot"}
    pm = PatternMatcher(bot_properties=bot)
    pm.add_pattern("HELLO {bot:name}", "Hi!")

    result = pm.match("hello TESTBOT")
    assert result
    result = pm.match("HELLO testbot")
    assert result


def test_bot_pattern_matching_unknown_bot_property_no_match():
    """Test that unknown bot properties don't match."""
    bot = {"name": "TestBot"}
    pm = PatternMatcher(bot_properties=bot)
    pm.add_pattern("YOUR {bot:unknown} IS", "Should not match")

    result = pm.match("your something is")
    assert not result


def test_bot_pattern_matching_bot_with_sets_combined():
    """Test bot properties combined with sets."""
    sets = {"color": ["red", "blue"]}
    bot = {"name": "TestBot"}
    pm = PatternMatcher(sets=sets, bot_properties=bot)
    pm.add_pattern("{bot:name} LIKES {set:color}", "Combined match")

    result = pm.match("testbot likes blue")
    assert result
    assert result[0] == "Combined match"
    assert result[1] == ["testbot", "blue"]


"""Tests for normalize_pattern with set/bot references."""


def test_normalize_pattern_with_refs_preserve_set_reference():
    """Test that {set:name} is preserved."""
    result = normalize_pattern("I LIKE {set:color}!")
    assert result == "i like {set:color}"


def test_normalize_pattern_with_refs_preserve_bot_reference():
    """Test that {bot:name} is preserved."""
    result = normalize_pattern("YOU ARE {bot:name}?")
    assert result == "you are {bot:name}"


def test_normalize_pattern_with_refs_preserve_multiple_references():
    """Test multiple refs are preserved."""
    result = normalize_pattern("{set:greeting} {bot:name}!")
    assert result == "{set:greeting} {bot:name}"


def test_normalize_pattern_with_refs_refs_with_wildcards():
    """Test refs combined with wildcards."""
    result = normalize_pattern("* IS {set:color} AND *")
    assert result == "* is {set:color} and *"


"""Tests for $ priority operator."""


def test_priority_operator_normalize_preserves_dollar():
    """Test that $ prefix is preserved during normalization."""
    result = normalize_pattern("$WHO IS *")
    assert result == "$who is *"


def test_priority_operator_normalize_multiple_dollar_words():
    """Test multiple $ words in pattern."""
    result = normalize_pattern("$HELLO $WORLD")
    assert result == "$hello $world"


def test_priority_operator_dollar_word_matches():
    """Test that $ word matches correctly."""
    regex, _ = pattern_to_regex("$HELLO")
    assert regex.match("hello")
    assert not regex.match("world")


def test_priority_operator_dollar_with_regular_words():
    """Test $ word combined with regular words."""
    regex, _ = pattern_to_regex("$WHO IS *")
    assert regex.match("who is john")
    assert not regex.match("what is john")


def test_priority_operator_dollar_word_wins_over_regular():
    """Test that $ pattern beats regular pattern."""
    result1 = match_pattern("WHO IS *", "who is john")
    result2 = match_pattern("$WHO IS *", "who is john")
    # Regular: 100 + 100 - 4 = 196
    # Priority: 1000 + 100 - 4 = 1096
    assert result2["score"] > result1["score"]


def test_priority_operator_matcher_prefers_dollar():
    """Test that PatternMatcher prefers $ patterns."""
    matcher = PatternMatcher()
    matcher.add_pattern("* IS *", "general")
    matcher.add_pattern("WHO IS *", "regular")
    matcher.add_pattern("$WHO IS *", "priority")

    result = matcher.match("who is alice")
    assert result
    assert result[0] == "priority"


def test_priority_operator_dollar_at_end():
    """Test $ word at end of pattern."""
    regex, _ = pattern_to_regex("HELLO $WORLD")
    assert regex.match("hello world")


def test_priority_operator_dollar_captures_wildcards():
    """Test capturing with $ patterns."""
    result = match_pattern("$WHO IS *", "who is john smith")
    assert result["matched"]
    assert result["captured"] == ["john smith"]


def test_priority_operator_multiple_dollar_words():
    """Test multiple $ words in same pattern."""
    regex, _ = pattern_to_regex("$HELLO $WORLD")
    assert regex.match("hello world")
    assert not regex.match("hello there")


"""Tests for stemming support in pattern matching."""


def test_stemming_support_stemming_disabled_by_default():
    """Test stemming is disabled by default."""
    pm = PatternMatcher()
    pm.add_pattern("RUN", "Running response")

    # Exact match should work
    result = pm.match("run")
    assert result
    # Variant should not match without stemming
    result = pm.match("running")
    assert not result


def test_stemming_support_stemming_enabled_matches_variants():
    """Test stemming allows matching word variants."""
    pm = PatternMatcher(use_stemming=True)
    pm.add_pattern("RUN", "Running response")

    # Exact match should work
    result = pm.match("run")
    assert result
    assert result[0] == "Running response"

    # Variants should match with stemming
    result = pm.match("running")
    assert result
    assert result[0] == "Running response"

    result = pm.match("runs")
    assert result
    assert result[0] == "Running response"


def test_stemming_support_stemming_with_wildcard_pattern():
    """Test stemming with wildcards."""
    pm = PatternMatcher(use_stemming=True)
    pm.add_pattern("I LIKE *", "You like {star1}!")

    result = pm.match("i liked pizza")
    assert result


def test_stemming_support_stemming_exact_match_preferred():
    """Test exact match is preferred over stemmed match."""
    pm = PatternMatcher(use_stemming=True)
    pm.add_pattern("RUN", "Exact run")
    pm.add_pattern("RUNNING", "Exact running")

    # "run" should match "RUN" pattern exactly
    result = pm.match("run")
    assert result
    assert result[0] == "Exact run"

    # "running" should match "RUNNING" pattern exactly
    result = pm.match("running")
    assert result
    assert result[0] == "Exact running"


def test_stemming_support_stemming_cats_mammals():
    """Test stemming with plural nouns."""
    pm = PatternMatcher(use_stemming=True)
    pm.add_pattern("CAT", "Cats are mammals.")

    result = pm.match("cats")
    assert result
    assert result[0] == "Cats are mammals."


"""Tests for WordNet lemmatization support in pattern matching."""


def test_lemmatization_support_lemmatization_disabled_by_default():
    """Lemmatization is off unless requested."""
    pm = PatternMatcher()
    pm.add_pattern("MOUSE", "A mouse!")
    assert not pm.match("mice")


def test_lemmatization_support_regular_plural_matches():
    """A regular plural lemmatizes to the singular pattern."""
    pm = PatternMatcher(use_lemmatization=True)
    pm.add_pattern("CAT", "A cat!")
    result = pm.match("cats")
    assert result
    assert result[0] == "A cat!"


def test_lemmatization_support_irregular_plural_matches():
    """Lemmatization handles irregular plurals that stemming misses."""
    pm_lemma = PatternMatcher(use_lemmatization=True, use_stemming=False)
    pm_lemma.add_pattern("MOUSE", "A mouse!")
    result = pm_lemma.match("mice")
    assert result
    assert result[0] == "A mouse!"

    # Porter stemming cannot bridge mice -> mouse
    pm_stem = PatternMatcher(use_lemmatization=False, use_stemming=True)
    pm_stem.add_pattern("MOUSE", "A mouse!")
    assert not pm_stem.match("mice")


def test_lemmatization_support_irregular_verb_matches():
    """Lemmatization maps irregular verb forms to the base verb."""
    pm = PatternMatcher(use_lemmatization=True, use_stemming=False)
    pm.add_pattern("GO", "Going!")
    result = pm.match("went")
    assert result
    assert result[0] == "Going!"


def test_lemmatization_support_exact_match_preferred():
    """Exact matches win over lemmatized matches."""
    pm = PatternMatcher(use_lemmatization=True)
    pm.add_pattern("MOUSE", "Exact mouse")
    pm.add_pattern("MICE", "Exact mice")
    result = pm.match("mice")
    assert result
    assert result[0] == "Exact mice"


def test_lemmatization_support_lemmatization_with_wildcard():
    """Lemmatization works alongside wildcard capture."""
    pm = PatternMatcher(use_lemmatization=True)
    pm.add_pattern("I SAW *", "You saw {star1}!")
    result = pm.match("i saw dogs")
    assert result


"""Short tokens are not stemmed: 'his' must not become the greeting 'hi'."""


def test_stemming_false_positives_possessive_does_not_match_greeting():
    pm = PatternMatcher(use_stemming=True)
    pm.add_pattern("HI *", "Hi there!")
    assert not pm.match("his name is rex")


def test_stemming_false_positives_greeting_still_matches():
    pm = PatternMatcher(use_stemming=True)
    pm.add_pattern("HI *", "Hi there!")
    assert pm.match("hi everyone")[0] == "Hi there!"


def test_stemming_false_positives_stemming_fallback_still_works_for_real_inflections():
    pm = PatternMatcher(use_stemming=True, use_lemmatization=False)
    pm.add_pattern("CATS ARE GREAT", "Indeed")
    assert pm.match("cats are great")
