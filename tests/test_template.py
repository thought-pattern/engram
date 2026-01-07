"""Tests for template processing."""

import pytest

from engram.template import TemplateContext, TemplateProcessor, process_template


class TestTemplateContext:
    """Tests for TemplateContext."""

    def test_get_star(self):
        """Test star capture retrieval."""
        ctx = TemplateContext(stars=["alice", "pizza"])
        assert ctx.get_star(1) == "alice"
        assert ctx.get_star(2) == "pizza"
        assert ctx.get_star(3) == ""
        assert ctx.get_star(0) == ""

    def test_get_predicate(self):
        """Test predicate retrieval."""
        ctx = TemplateContext(predicates={"name": "Alice", "mood": "happy"})
        assert ctx.get_predicate("name") == "Alice"
        assert ctx.get_predicate("mood") == "happy"
        assert ctx.get_predicate("missing") == ""
        assert ctx.get_predicate("missing", "default") == "default"

    def test_set_predicate(self):
        """Test predicate setting."""
        ctx = TemplateContext()
        ctx.set_predicate("name", "Bob")
        assert ctx.predicates["name"] == "Bob"

    def test_topic_property(self):
        """Test topic property."""
        ctx = TemplateContext(predicates={"topic": "WEATHER"})
        assert ctx.topic == "WEATHER"

    def test_topic_empty(self):
        """Test topic when not set."""
        ctx = TemplateContext()
        assert ctx.topic == ""

    def test_that_property(self):
        """Test that property."""
        ctx = TemplateContext(that_history=[["HELLO", "HOW ARE YOU"], ["GOODBYE"]])
        assert ctx.that == "HELLO"

    def test_that_empty(self):
        """Test that when empty."""
        ctx = TemplateContext()
        assert ctx.that == ""

    def test_get_input(self):
        """Test input history retrieval."""
        ctx = TemplateContext(input_history=["latest", "previous", "oldest"])
        assert ctx.get_input(1) == "latest"
        assert ctx.get_input(2) == "previous"
        assert ctx.get_input(3) == "oldest"
        assert ctx.get_input(4) == ""

    def test_get_response(self):
        """Test response history retrieval."""
        ctx = TemplateContext(response_history=["last", "before"])
        assert ctx.get_response(1) == "last"
        assert ctx.get_response(2) == "before"
        assert ctx.get_response(3) == ""

    def test_get_bot(self):
        """Test bot property retrieval."""
        ctx = TemplateContext(bot={"name": "TestBot", "version": "1.0"})
        assert ctx.get_bot("name") == "TestBot"
        assert ctx.get_bot("missing") == ""
        assert ctx.get_bot("missing", "default") == "default"

    def test_get_map(self):
        """Test map lookup."""
        ctx = TemplateContext(maps={"capital": {"france": "paris", "germany": "berlin"}})
        assert ctx.get_map("capital", "france") == "paris"
        assert ctx.get_map("capital", "GERMANY") == "berlin"  # Case insensitive
        assert ctx.get_map("capital", "unknown") == ""
        assert ctx.get_map("capital", "unknown", "n/a") == "n/a"
        assert ctx.get_map("missing_map", "key") == ""


class TestTemplateProcessorBasic:
    """Tests for basic template processing."""

    def test_plain_string(self):
        """Test processing plain string."""
        processor = TemplateProcessor()
        ctx = TemplateContext()
        assert processor.process("Hello, world!", ctx) == "Hello, world!"

    def test_text_template(self):
        """Test text template."""
        processor = TemplateProcessor()
        ctx = TemplateContext()
        assert processor.process({"text": "Hello!"}, ctx) == "Hello!"

    def test_star_substitution(self):
        """Test star wildcard substitution."""
        processor = TemplateProcessor()
        ctx = TemplateContext(stars=["Alice", "pizza"])
        result = processor.process("Hello, {star1}! You like {star2}.", ctx)
        assert result == "Hello, Alice! You like pizza."

    def test_star_missing(self):
        """Test missing star returns empty."""
        processor = TemplateProcessor()
        ctx = TemplateContext(stars=["Alice"])
        result = processor.process("Hello, {star1} and {star2}!", ctx)
        assert result == "Hello, Alice and !"

    def test_get_predicate(self):
        """Test predicate retrieval in template."""
        processor = TemplateProcessor()
        ctx = TemplateContext(predicates={"username": "Bob"})
        result = processor.process("Hello, {get:username}!", ctx)
        assert result == "Hello, Bob!"

    def test_get_predicate_with_default(self):
        """Test predicate with default value."""
        processor = TemplateProcessor()
        ctx = TemplateContext()
        result = processor.process("Mood: {get:mood:neutral}", ctx)
        assert result == "Mood: neutral"

    def test_bot_property(self):
        """Test bot property access."""
        processor = TemplateProcessor()
        ctx = TemplateContext(bot={"name": "TestBot"})
        result = processor.process("I am {bot:name}.", ctx)
        assert result == "I am TestBot."

    def test_map_lookup(self):
        """Test map lookup."""
        processor = TemplateProcessor()
        ctx = TemplateContext(
            stars=["france"],
            maps={"capital": {"france": "Paris"}}
        )
        result = processor.process("Capital: {map:capital:{star1}}", ctx)
        assert result == "Capital: Paris"


class TestTemplateProcessorRandom:
    """Tests for random template."""

    def test_random_selection(self):
        """Test random selects from list."""
        processor = TemplateProcessor()
        ctx = TemplateContext()
        choices = ["Hi!", "Hello!", "Hey!"]
        results = set()
        for _ in range(50):
            result = processor.process({"random": choices}, ctx)
            results.add(result)
        # Should see at least 2 different results
        assert len(results) >= 2

    def test_random_with_template(self):
        """Test random with nested template."""
        processor = TemplateProcessor()
        ctx = TemplateContext(stars=["Alice"])
        template = {
            "random": [
                {"text": "Hello, {star1}!"},
                {"text": "Hi, {star1}!"}
            ]
        }
        result = processor.process(template, ctx)
        assert result in ["Hello, Alice!", "Hi, Alice!"]


class TestTemplateProcessorCondition:
    """Tests for conditional templates."""

    def test_condition_exists(self):
        """Test condition by existence."""
        processor = TemplateProcessor()

        # Variable exists
        ctx = TemplateContext(predicates={"name": "Alice"})
        template = {
            "condition": {
                "var": "name",
                "exists": {"text": "Hello, {get:name}!"},
                "missing": {"text": "What's your name?"}
            }
        }
        assert processor.process(template, ctx) == "Hello, Alice!"

        # Variable missing
        ctx2 = TemplateContext()
        assert processor.process(template, ctx2) == "What's your name?"

    def test_condition_by_value(self):
        """Test condition by value."""
        processor = TemplateProcessor()
        ctx = TemplateContext(predicates={"mood": "happy"})
        template = {
            "condition": {
                "var": "mood",
                "cases": [
                    {"value": "happy", "template": "Great!"},
                    {"value": "sad", "template": "Sorry to hear."},
                    {"default": "I see."}
                ]
            }
        }
        assert processor.process(template, ctx) == "Great!"

        ctx2 = TemplateContext(predicates={"mood": "sad"})
        assert processor.process(template, ctx2) == "Sorry to hear."

        ctx3 = TemplateContext(predicates={"mood": "neutral"})
        assert processor.process(template, ctx3) == "I see."

    def test_condition_by_pattern(self):
        """Test condition by regex pattern."""
        processor = TemplateProcessor()
        ctx = TemplateContext(predicates={"age": "25"})
        template = {
            "condition": {
                "var": "age",
                "pattern": r"^\d+$",
                "match": {"text": "You are {get:age}."},
                "nomatch": {"text": "Invalid age."}
            }
        }
        assert processor.process(template, ctx) == "You are 25."

        ctx2 = TemplateContext(predicates={"age": "twenty"})
        assert processor.process(template, ctx2) == "Invalid age."


class TestTemplateProcessorSequence:
    """Tests for sequence templates."""

    def test_sequence_basic(self):
        """Test basic sequence."""
        processor = TemplateProcessor()
        ctx = TemplateContext()
        template = {
            "sequence": [
                {"set": {"name": "topic", "value": "weather"}},
                {"text": "Let's talk about weather!"}
            ]
        }
        result = processor.process(template, ctx)
        assert result == "Let's talk about weather!"
        assert ctx.predicates["topic"] == "weather"

    def test_sequence_multiple_text(self):
        """Test sequence with multiple text outputs."""
        processor = TemplateProcessor()
        ctx = TemplateContext()
        template = {
            "sequence": [
                {"text": "First."},
                {"text": "Second."}
            ]
        }
        result = processor.process(template, ctx)
        assert result == "First. Second."


class TestTemplateProcessorThink:
    """Tests for think (silent processing)."""

    def test_think_no_output(self):
        """Test think produces no output."""
        processor = TemplateProcessor()
        ctx = TemplateContext()
        template = {
            "think": [
                {"set": {"name": "secret", "value": "hidden"}}
            ]
        }
        result = processor.process(template, ctx)
        assert result == ""
        assert ctx.predicates["secret"] == "hidden"

    def test_think_in_sequence(self):
        """Test think in sequence."""
        processor = TemplateProcessor()
        ctx = TemplateContext()
        template = {
            "sequence": [
                {"think": [{"set": {"name": "name", "value": "Alice"}}]},
                {"text": "Hello, {get:name}!"}
            ]
        }
        result = processor.process(template, ctx)
        assert result == "Hello, Alice!"


class TestTemplateProcessorTransforms:
    """Tests for text transforms."""

    def test_upper(self):
        """Test uppercase transform."""
        processor = TemplateProcessor()
        ctx = TemplateContext(stars=["hello"])
        result = processor.process("{upper:{star1}}", ctx)
        assert result == "HELLO"

    def test_lower(self):
        """Test lowercase transform."""
        processor = TemplateProcessor()
        ctx = TemplateContext(stars=["HELLO"])
        result = processor.process("{lower:{star1}}", ctx)
        assert result == "hello"

    def test_capitalize(self):
        """Test capitalize transform."""
        processor = TemplateProcessor()
        ctx = TemplateContext(stars=["hello world"])
        result = processor.process("{capitalize:{star1}}", ctx)
        assert result == "Hello world"

    def test_formal(self):
        """Test formal (title case) transform."""
        processor = TemplateProcessor()
        ctx = TemplateContext(stars=["hello world"])
        result = processor.process("{formal:{star1}}", ctx)
        assert result == "Hello World"

    def test_explode(self):
        """Test explode transform."""
        processor = TemplateProcessor()
        ctx = TemplateContext(stars=["abc"])
        result = processor.process("{explode:{star1}}", ctx)
        assert result == "a b c"


class TestStringUtilities:
    """Tests for string utility transforms."""

    def test_first_word(self):
        """Test first word extraction."""
        processor = TemplateProcessor()
        ctx = TemplateContext(stars=["hello world there"])
        result = processor.process("{first:{star1}}", ctx)
        assert result == "hello"

    def test_first_single_word(self):
        """Test first with single word."""
        processor = TemplateProcessor()
        ctx = TemplateContext(stars=["hello"])
        result = processor.process("{first:{star1}}", ctx)
        assert result == "hello"

    def test_first_empty(self):
        """Test first with empty string."""
        processor = TemplateProcessor()
        ctx = TemplateContext(stars=[""])
        result = processor.process("{first:{star1}}", ctx)
        assert result == ""

    def test_rest_multiple_words(self):
        """Test rest with multiple words."""
        processor = TemplateProcessor()
        ctx = TemplateContext(stars=["hello world there"])
        result = processor.process("{rest:{star1}}", ctx)
        assert result == "world there"

    def test_rest_two_words(self):
        """Test rest with two words."""
        processor = TemplateProcessor()
        ctx = TemplateContext(stars=["hello world"])
        result = processor.process("{rest:{star1}}", ctx)
        assert result == "world"

    def test_rest_single_word(self):
        """Test rest with single word returns empty."""
        processor = TemplateProcessor()
        ctx = TemplateContext(stars=["hello"])
        result = processor.process("{rest:{star1}}", ctx)
        assert result == ""

    def test_rest_empty(self):
        """Test rest with empty string."""
        processor = TemplateProcessor()
        ctx = TemplateContext(stars=[""])
        result = processor.process("{rest:{star1}}", ctx)
        assert result == ""

    def test_uniq_with_duplicates(self):
        """Test unique word removal."""
        processor = TemplateProcessor()
        ctx = TemplateContext(stars=["the the cat sat on the mat"])
        result = processor.process("{uniq:{star1}}", ctx)
        assert result == "the cat sat on mat"

    def test_uniq_no_duplicates(self):
        """Test uniq with no duplicates."""
        processor = TemplateProcessor()
        ctx = TemplateContext(stars=["hello world"])
        result = processor.process("{uniq:{star1}}", ctx)
        assert result == "hello world"

    def test_uniq_all_same(self):
        """Test uniq with all same words."""
        processor = TemplateProcessor()
        ctx = TemplateContext(stars=["a a a a"])
        result = processor.process("{uniq:{star1}}", ctx)
        assert result == "a"

    def test_uniq_empty(self):
        """Test uniq with empty string."""
        processor = TemplateProcessor()
        ctx = TemplateContext(stars=[""])
        result = processor.process("{uniq:{star1}}", ctx)
        assert result == ""

    def test_wordcount_multiple(self):
        """Test word counting."""
        processor = TemplateProcessor()
        ctx = TemplateContext(stars=["hello world"])
        result = processor.process("{wordcount:{star1}}", ctx)
        assert result == "2"

    def test_wordcount_single(self):
        """Test word count with single word."""
        processor = TemplateProcessor()
        ctx = TemplateContext(stars=["hello"])
        result = processor.process("{wordcount:{star1}}", ctx)
        assert result == "1"

    def test_wordcount_empty(self):
        """Test word count with empty string."""
        processor = TemplateProcessor()
        ctx = TemplateContext(stars=[""])
        result = processor.process("{wordcount:{star1}}", ctx)
        assert result == "0"

    def test_wordcount_many(self):
        """Test word count with many words."""
        processor = TemplateProcessor()
        ctx = TemplateContext(stars=["one two three four five"])
        result = processor.process("{wordcount:{star1}}", ctx)
        assert result == "5"

    def test_nested_first_upper(self):
        """Test nested transforms: first then upper."""
        processor = TemplateProcessor()
        ctx = TemplateContext(stars=["hello world"])
        result = processor.process("{upper:{first:{star1}}}", ctx)
        assert result == "HELLO"


class TestTemplateProcessorRedirect:
    """Tests for redirect (SRAI)."""

    def test_redirect_callback(self):
        """Test redirect uses callback."""
        processor = TemplateProcessor()

        def redirect_fn(pattern):
            if pattern == "HELLO":
                return "Hi there!"
            return ""

        ctx = TemplateContext(redirect_fn=redirect_fn)
        result = processor.process({"redirect": "HELLO"}, ctx)
        assert result == "Hi there!"

    def test_redirect_with_star(self):
        """Test redirect with star substitution."""
        processor = TemplateProcessor()

        def redirect_fn(pattern):
            # Pattern is substituted as-is (lowercase star value)
            if pattern == "WHAT IS python":
                return "Python is a programming language."
            return ""

        ctx = TemplateContext(
            stars=["python"],
            redirect_fn=redirect_fn
        )
        result = processor.process({"redirect": "WHAT IS {star1}"}, ctx)
        assert result == "Python is a programming language."

    def test_sr_shorthand(self):
        """Test sr shorthand redirect."""
        processor = TemplateProcessor()

        def redirect_fn(pattern):
            if pattern == "help me":
                return "How can I help?"
            return ""

        ctx = TemplateContext(
            stars=["help me"],
            redirect_fn=redirect_fn
        )
        result = processor.process({"sr": True}, ctx)
        assert result == "How can I help?"

    def test_redirect_depth_limit(self):
        """Test redirect depth limit."""
        processor = TemplateProcessor(srai_limit=3)

        call_count = [0]
        def redirect_fn(pattern):
            call_count[0] += 1
            # Infinite recursion attempt
            return processor.process({"redirect": "LOOP"},
                TemplateContext(redirect_fn=redirect_fn))

        ctx = TemplateContext(redirect_fn=redirect_fn)
        processor.process({"redirect": "START"}, ctx)
        # Should stop at limit
        assert call_count[0] <= 4  # Start + 3 redirects


class TestTemplateProcessorLearn:
    """Tests for learn element."""

    def test_learn_callback(self):
        """Test learn uses callback."""
        processor = TemplateProcessor()

        learned = []
        def learn_fn(data):
            learned.append(data)

        ctx = TemplateContext(
            stars=["the sky", "blue"],
            learn_fn=learn_fn
        )
        template = {
            "learn": {
                "pattern": "{upper:{star1}}",
                "template": {"text": "{star2}"}
            }
        }
        processor.process(template, ctx)

        assert len(learned) == 1
        assert learned[0]["pattern"] == "THE SKY"
        # Variables in templates are now resolved at learn time
        assert learned[0]["template"] == {"text": "blue"}


class TestProcessTemplateFunction:
    """Tests for process_template convenience function."""

    def test_basic_usage(self):
        """Test basic function usage."""
        ctx = TemplateContext(stars=["world"])
        result = process_template("Hello, {star1}!", ctx)
        assert result == "Hello, world!"

    def test_with_srai_limit(self):
        """Test with custom SRAI limit."""
        ctx = TemplateContext()
        result = process_template({"text": "Test"}, ctx, srai_limit=50)
        assert result == "Test"


class TestTemplateIntegration:
    """Integration tests for templates."""

    def test_spec_example_my_name_is(self):
        """Test spec example: MY NAME IS pattern."""
        processor = TemplateProcessor()
        ctx = TemplateContext(stars=["Alice"])
        template = {
            "sequence": [
                {"set": {"name": "username", "value": "{star1}"}},
                {"text": "Nice to meet you, {star1}!"}
            ]
        }
        result = processor.process(template, ctx)
        assert result == "Nice to meet you, Alice!"
        assert ctx.predicates["username"] == "Alice"

    def test_spec_example_what_is_my_name(self):
        """Test spec example: WHAT IS MY NAME pattern."""
        processor = TemplateProcessor()

        # When name is set
        ctx1 = TemplateContext(predicates={"username": "Alice"})
        template = {
            "condition": {
                "var": "username",
                "exists": {"text": "Your name is {get:username}."},
                "missing": {"text": "I don't know your name yet."}
            }
        }
        assert processor.process(template, ctx1) == "Your name is Alice."

        # When name is not set
        ctx2 = TemplateContext()
        assert processor.process(template, ctx2) == "I don't know your name yet."

    def test_nested_template(self):
        """Test deeply nested template."""
        processor = TemplateProcessor()
        ctx = TemplateContext(
            predicates={"mood": "happy"},
            stars=["Alice"]
        )
        template = {
            "sequence": [
                {"set": {"name": "greeted", "value": "true"}},
                {
                    "condition": {
                        "var": "mood",
                        "cases": [
                            {
                                "value": "happy",
                                "template": {"text": "Hello, {star1}! You seem happy!"}
                            },
                            {"default": "Hello, {star1}."}
                        ]
                    }
                }
            ]
        }
        result = processor.process(template, ctx)
        assert result == "Hello, Alice! You seem happy!"
        assert ctx.predicates["greeted"] == "true"


class TestSystemVariables:
    """Tests for system variables."""

    def test_program_variable(self):
        """Test {program} returns bot name."""
        processor = TemplateProcessor()
        ctx = TemplateContext(bot={"name": "TestBot", "version": "1.0"})
        result = processor.process("My name is {program}", ctx)
        assert result == "My name is TestBot"

    def test_version_variable(self):
        """Test {version} returns bot version."""
        processor = TemplateProcessor()
        ctx = TemplateContext(bot={"name": "TestBot", "version": "2.5.1"})
        result = processor.process("Version {version}", ctx)
        assert result == "Version 2.5.1"

    def test_program_default(self):
        """Test {program} returns ENGRAM when bot name not set."""
        processor = TemplateProcessor()
        ctx = TemplateContext(bot={})
        result = processor.process("{program}", ctx)
        assert result == "ENGRAM"

    def test_version_default(self):
        """Test {version} returns default when bot version not set."""
        processor = TemplateProcessor()
        ctx = TemplateContext(bot={})
        result = processor.process("{version}", ctx)
        assert result == "0.1.5"

    def test_id_variable(self):
        """Test {id} returns session ID."""
        processor = TemplateProcessor()
        ctx = TemplateContext(session_id="user_12345")
        result = processor.process("Session: {id}", ctx)
        assert result == "Session: user_12345"

    def test_date_formatted(self):
        """Test {date:format} with custom format."""
        processor = TemplateProcessor()
        ctx = TemplateContext()
        # Use a format that's easy to verify
        result = processor.process("{date:%Y}", ctx)
        from datetime import datetime
        assert result == datetime.now().strftime("%Y")

    def test_date_formatted_complex(self):
        """Test {date:format} with complex format."""
        processor = TemplateProcessor()
        ctx = TemplateContext()
        result = processor.process("{date:%Y-%m-%d}", ctx)
        from datetime import datetime
        assert result == datetime.now().strftime("%Y-%m-%d")

    def test_date_formatted_time(self):
        """Test {date:format} can include time components."""
        processor = TemplateProcessor()
        ctx = TemplateContext()
        result = processor.process("{date:%H:%M}", ctx)
        from datetime import datetime
        # Just check format is correct (time may differ by seconds)
        assert len(result) == 5
        assert ":" in result

    def test_size_variable(self):
        """Test {size} returns category count."""
        processor = TemplateProcessor()
        ctx = TemplateContext(category_count=42)
        result = processor.process("I know {size} things", ctx)
        assert result == "I know 42 things"

    def test_vocabulary_variable(self):
        """Test {vocabulary} returns vocabulary count."""
        processor = TemplateProcessor()
        ctx = TemplateContext(vocabulary_count=1000)
        result = processor.process("My vocabulary is {vocabulary} words", ctx)
        assert result == "My vocabulary is 1000 words"
