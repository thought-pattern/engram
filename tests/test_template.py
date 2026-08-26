"""Tests for template processing."""

from datetime import datetime

from engram.constants import VERSION
from engram.template import (
    TemplateProcessor,
    get_input,
    get_map,
    get_response,
    get_star,
    process_template,
    template_context,
)


class TestTemplateContext:
    """Tests for TemplateContext."""

    def test_get_star(self):
        """Test star capture retrieval."""
        ctx = template_context(stars=["alice", "pizza"])
        assert get_star(ctx, 1) == "alice"
        assert get_star(ctx, 2) == "pizza"
        assert get_star(ctx, 3) == ""
        assert get_star(ctx, 0) == ""
        return False

    def test_get_predicate(self):
        """Test predicate retrieval via direct access."""
        ctx = template_context(predicates={"name": "Alice", "mood": "happy"})
        assert ctx.get("predicates", {}).get("name", "") == "Alice"
        assert ctx.get("predicates", {}).get("mood", "") == "happy"
        assert ctx.get("predicates", {}).get("missing", "") == ""
        assert ctx.get("predicates", {}).get("missing", "default") == "default"
        return False

    def test_set_predicate(self):
        """Test predicate setting via direct access."""
        ctx = template_context()
        ctx.get("predicates", {})["name"] = "Bob"
        assert ctx.get("predicates", {}).get("name", "") == "Bob"
        return False

    def test_topic_in_predicates(self):
        """Test topic from predicates."""
        ctx = template_context(predicates={"topic": "WEATHER"})
        assert ctx.get("predicates", {}).get("topic", "") == "WEATHER"
        return False

    def test_topic_empty(self):
        """Test topic when not set."""
        ctx = template_context()
        assert ctx.get("predicates", {}).get("topic", "") == ""
        return False

    def test_that_from_history(self):
        """Test getting that from history."""
        ctx = template_context(that_history=[["HELLO", "HOW ARE YOU"], ["GOODBYE"]])
        # Most recent bot response is first sentence of first history entry
        assert ctx.get("that_history", [])[0][0] == "HELLO"
        return False

    def test_that_empty(self):
        """Test that when empty."""
        ctx = template_context()
        assert len(ctx.get("that_history", [])) == 0
        return False

    def test_get_input(self):
        """Test input history retrieval."""
        ctx = template_context(input_history=["latest", "previous", "oldest"])
        assert get_input(ctx, 1) == "latest"
        assert get_input(ctx, 2) == "previous"
        assert get_input(ctx, 3) == "oldest"
        assert get_input(ctx, 4) == ""
        return False

    def test_get_response(self):
        """Test response history retrieval."""
        ctx = template_context(response_history=["last", "before"])
        assert get_response(ctx, 1) == "last"
        assert get_response(ctx, 2) == "before"
        assert get_response(ctx, 3) == ""
        return False

    def test_get_bot(self):
        """Test bot property retrieval via direct access."""
        ctx = template_context(bot={"name": "TestBot", "version": "1.0"})
        assert ctx.get("bot", {}).get("name", "") == "TestBot"
        assert ctx.get("bot", {}).get("missing", "") == ""
        assert ctx.get("bot", {}).get("missing", "default") == "default"
        return False

    def test_get_map(self):
        """Test map lookup."""
        ctx = template_context(maps={"capital": {"france": "paris", "germany": "berlin"}})
        assert get_map(ctx, "capital", "france") == "paris"
        assert get_map(ctx, "capital", "GERMANY") == "berlin"  # Case insensitive
        assert get_map(ctx, "capital", "unknown") == ""
        assert get_map(ctx, "capital", "unknown", "n/a") == "n/a"
        assert get_map(ctx, "missing_map", "key") == ""
        return False


class TestTemplateProcessorBasic:
    """Tests for basic template processing."""

    def test_plain_string(self):
        """Test processing plain string."""
        processor = TemplateProcessor()
        ctx = template_context()
        assert processor.process("Hello, world!", ctx) == "Hello, world!"
        return False

    def test_text_template(self):
        """Test text template."""
        processor = TemplateProcessor()
        ctx = template_context()
        assert processor.process({"text": "Hello!"}, ctx) == "Hello!"
        return False

    def test_star_substitution(self):
        """Test star wildcard substitution."""
        processor = TemplateProcessor()
        ctx = template_context(stars=["Alice", "pizza"])
        result = processor.process("Hello, {star1}! You like {star2}.", ctx)
        assert result == "Hello, Alice! You like pizza."
        return False

    def test_star_missing(self):
        """Test missing star returns empty."""
        processor = TemplateProcessor()
        ctx = template_context(stars=["Alice"])
        result = processor.process("Hello, {star1} and {star2}!", ctx)
        assert result == "Hello, Alice and !"
        return False

    def test_get_predicate(self):
        """Test predicate retrieval in template."""
        processor = TemplateProcessor()
        ctx = template_context(predicates={"username": "Bob"})
        result = processor.process("Hello, {get:username}!", ctx)
        assert result == "Hello, Bob!"
        return False

    def test_get_predicate_with_default(self):
        """Test predicate with default value."""
        processor = TemplateProcessor()
        ctx = template_context()
        result = processor.process("Mood: {get:mood:neutral}", ctx)
        assert result == "Mood: neutral"
        return False

    def test_bot_property(self):
        """Test bot property access."""
        processor = TemplateProcessor()
        ctx = template_context(bot={"name": "TestBot"})
        result = processor.process("I am {bot:name}.", ctx)
        assert result == "I am TestBot."
        return False

    def test_map_lookup(self):
        """Test map lookup."""
        processor = TemplateProcessor()
        ctx = template_context(stars=["france"], maps={"capital": {"france": "Paris"}})
        result = processor.process("Capital: {map:capital:{star1}}", ctx)
        assert result == "Capital: Paris"
        return False


class TestTemplateProcessorRandom:
    """Tests for random template."""

    def test_random_selection(self):
        """Test random selects from list."""
        processor = TemplateProcessor()
        ctx = template_context()
        choices = ["Hi!", "Hello!", "Hey!"]
        results = set()
        for _ in range(50):
            result = processor.process({"random": choices}, ctx)
            results.add(result)
        # Should see at least 2 different results
        assert len(results) >= 2
        return False

    def test_random_with_template(self):
        """Test random with nested template."""
        processor = TemplateProcessor()
        ctx = template_context(stars=["Alice"])
        template = {"random": [{"text": "Hello, {star1}!"}, {"text": "Hi, {star1}!"}]}
        result = processor.process(template, ctx)
        assert result in ["Hello, Alice!", "Hi, Alice!"]
        return False


class TestTemplateProcessorCondition:
    """Tests for conditional templates."""

    def test_condition_exists(self):
        """Test condition by existence."""
        processor = TemplateProcessor()

        # Variable exists
        ctx = template_context(predicates={"name": "Alice"})
        template = {
            "condition": {
                "var": "name",
                "exists": {"text": "Hello, {get:name}!"},
                "missing": {"text": "What's your name?"},
            }
        }
        assert processor.process(template, ctx) == "Hello, Alice!"

        # Variable missing
        ctx2 = template_context()
        assert processor.process(template, ctx2) == "What's your name?"
        return False

    def test_condition_by_value(self):
        """Test condition by value."""
        processor = TemplateProcessor()
        ctx = template_context(predicates={"mood": "happy"})
        template = {
            "condition": {
                "var": "mood",
                "cases": [
                    {"value": "happy", "template": "Great!"},
                    {"value": "sad", "template": "Sorry to hear."},
                    {"default": "I see."},
                ],
            }
        }
        assert processor.process(template, ctx) == "Great!"

        ctx2 = template_context(predicates={"mood": "sad"})
        assert processor.process(template, ctx2) == "Sorry to hear."

        ctx3 = template_context(predicates={"mood": "neutral"})
        assert processor.process(template, ctx3) == "I see."
        return False

    def test_condition_by_pattern(self):
        """Test condition by regex pattern."""
        processor = TemplateProcessor()
        ctx = template_context(predicates={"age": "25"})
        template = {
            "condition": {
                "var": "age",
                "pattern": r"^\d+$",
                "match": {"text": "You are {get:age}."},
                "nomatch": {"text": "Invalid age."},
            }
        }
        assert processor.process(template, ctx) == "You are 25."

        ctx2 = template_context(predicates={"age": "twenty"})
        assert processor.process(template, ctx2) == "Invalid age."
        return False

    def test_condition_loop_counts_down(self):
        """A loop whose predicate changes each pass runs until the terminal case."""
        processor = TemplateProcessor()
        ctx = template_context(predicates={"count": "3"})
        template = {
            "condition": {
                "var": "count",
                "cases": [
                    {"value": "3", "then": {"sequence": [{"set": {"name": "count", "value": "2"}}, {"text": "3"}], "loop": True}},
                    {"value": "2", "then": {"sequence": [{"set": {"name": "count", "value": "1"}}, {"text": "2"}], "loop": True}},
                    {"value": "1", "then": {"text": "liftoff"}},
                ],
            }
        }
        result = processor.process(template, ctx)
        assert result == "32liftoff"
        return False

    def test_condition_loop_never_changing_terminates(self):
        """A loop whose predicate never changes stops at the depth cap instead of recursing forever."""
        processor = TemplateProcessor(srai_limit=10)
        ctx = template_context(predicates={"stuck": "yes"})
        template = {
            "condition": {
                "var": "stuck",
                "cases": [
                    {"value": "yes", "then": {"text": "again", "loop": True}},
                ],
            }
        }
        result = processor.process(template, ctx)
        assert result.startswith("again")
        return False


class TestTemplateProcessorSequence:
    """Tests for sequence templates."""

    def test_sequence_basic(self):
        """Test basic sequence."""
        processor = TemplateProcessor()
        ctx = template_context()
        template = {
            "sequence": [
                {"set": {"name": "topic", "value": "weather"}},
                {"text": "Let's talk about weather!"},
            ]
        }
        result = processor.process(template, ctx)
        assert result == "Let's talk about weather!"
        assert ctx.get("predicates", {}).get("topic", "") == "weather"
        return False

    def test_sequence_multiple_text(self):
        """Test sequence with multiple text outputs."""
        processor = TemplateProcessor()
        ctx = template_context()
        template = {"sequence": [{"text": "First."}, {"text": "Second."}]}
        result = processor.process(template, ctx)
        assert result == "First. Second."
        return False


class TestTemplateProcessorThink:
    """Tests for think (silent processing)."""

    def test_think_no_output(self):
        """Test think produces no output."""
        processor = TemplateProcessor()
        ctx = template_context()
        template = {"think": [{"set": {"name": "secret", "value": "hidden"}}]}
        result = processor.process(template, ctx)
        assert result == ""
        assert ctx.get("predicates", {}).get("secret", "") == "hidden"
        return False

    def test_think_in_sequence(self):
        """Test think in sequence."""
        processor = TemplateProcessor()
        ctx = template_context()
        template = {
            "sequence": [
                {"think": [{"set": {"name": "name", "value": "Alice"}}]},
                {"text": "Hello, {get:name}!"},
            ]
        }
        result = processor.process(template, ctx)
        assert result == "Hello, Alice!"
        return False


class TestTemplateProcessorTransforms:
    """Tests for text transforms."""

    def test_upper(self):
        """Test uppercase transform."""
        processor = TemplateProcessor()
        ctx = template_context(stars=["hello"])
        result = processor.process("{upper:{star1}}", ctx)
        assert result == "HELLO"
        return False

    def test_lower(self):
        """Test lowercase transform."""
        processor = TemplateProcessor()
        ctx = template_context(stars=["HELLO"])
        result = processor.process("{lower:{star1}}", ctx)
        assert result == "hello"
        return False

    def test_capitalize(self):
        """Test capitalize transform."""
        processor = TemplateProcessor()
        ctx = template_context(stars=["hello world"])
        result = processor.process("{capitalize:{star1}}", ctx)
        assert result == "Hello world"
        return False

    def test_formal(self):
        """Test formal (title case) transform."""
        processor = TemplateProcessor()
        ctx = template_context(stars=["hello world"])
        result = processor.process("{formal:{star1}}", ctx)
        assert result == "Hello World"
        return False

    def test_explode(self):
        """Test explode transform."""
        processor = TemplateProcessor()
        ctx = template_context(stars=["abc"])
        result = processor.process("{explode:{star1}}", ctx)
        assert result == "a b c"
        return False


class TestStringUtilities:
    """Tests for string utility transforms."""

    def test_first_word(self):
        """Test first word extraction."""
        processor = TemplateProcessor()
        ctx = template_context(stars=["hello world there"])
        result = processor.process("{first:{star1}}", ctx)
        assert result == "hello"
        return False

    def test_first_single_word(self):
        """Test first with single word."""
        processor = TemplateProcessor()
        ctx = template_context(stars=["hello"])
        result = processor.process("{first:{star1}}", ctx)
        assert result == "hello"
        return False

    def test_first_empty(self):
        """Test first with empty string."""
        processor = TemplateProcessor()
        ctx = template_context(stars=[""])
        result = processor.process("{first:{star1}}", ctx)
        assert result == ""
        return False

    def test_rest_multiple_words(self):
        """Test rest with multiple words."""
        processor = TemplateProcessor()
        ctx = template_context(stars=["hello world there"])
        result = processor.process("{rest:{star1}}", ctx)
        assert result == "world there"
        return False

    def test_rest_two_words(self):
        """Test rest with two words."""
        processor = TemplateProcessor()
        ctx = template_context(stars=["hello world"])
        result = processor.process("{rest:{star1}}", ctx)
        assert result == "world"
        return False

    def test_rest_single_word(self):
        """Test rest with single word returns empty."""
        processor = TemplateProcessor()
        ctx = template_context(stars=["hello"])
        result = processor.process("{rest:{star1}}", ctx)
        assert result == ""
        return False

    def test_rest_empty(self):
        """Test rest with empty string."""
        processor = TemplateProcessor()
        ctx = template_context(stars=[""])
        result = processor.process("{rest:{star1}}", ctx)
        assert result == ""
        return False

    def test_uniq_with_duplicates(self):
        """Test unique word removal."""
        processor = TemplateProcessor()
        ctx = template_context(stars=["the the cat sat on the mat"])
        result = processor.process("{uniq:{star1}}", ctx)
        assert result == "the cat sat on mat"
        return False

    def test_uniq_no_duplicates(self):
        """Test uniq with no duplicates."""
        processor = TemplateProcessor()
        ctx = template_context(stars=["hello world"])
        result = processor.process("{uniq:{star1}}", ctx)
        assert result == "hello world"
        return False

    def test_uniq_all_same(self):
        """Test uniq with all same words."""
        processor = TemplateProcessor()
        ctx = template_context(stars=["a a a a"])
        result = processor.process("{uniq:{star1}}", ctx)
        assert result == "a"
        return False

    def test_uniq_empty(self):
        """Test uniq with empty string."""
        processor = TemplateProcessor()
        ctx = template_context(stars=[""])
        result = processor.process("{uniq:{star1}}", ctx)
        assert result == ""
        return False

    def test_wordcount_multiple(self):
        """Test word counting."""
        processor = TemplateProcessor()
        ctx = template_context(stars=["hello world"])
        result = processor.process("{wordcount:{star1}}", ctx)
        assert result == "2"
        return False

    def test_wordcount_single(self):
        """Test word count with single word."""
        processor = TemplateProcessor()
        ctx = template_context(stars=["hello"])
        result = processor.process("{wordcount:{star1}}", ctx)
        assert result == "1"
        return False

    def test_wordcount_empty(self):
        """Test word count with empty string."""
        processor = TemplateProcessor()
        ctx = template_context(stars=[""])
        result = processor.process("{wordcount:{star1}}", ctx)
        assert result == "0"
        return False

    def test_wordcount_many(self):
        """Test word count with many words."""
        processor = TemplateProcessor()
        ctx = template_context(stars=["one two three four five"])
        result = processor.process("{wordcount:{star1}}", ctx)
        assert result == "5"
        return False

    def test_nested_first_upper(self):
        """Test nested transforms: first then upper."""
        processor = TemplateProcessor()
        ctx = template_context(stars=["hello world"])
        result = processor.process("{upper:{first:{star1}}}", ctx)
        assert result == "HELLO"
        return False


class TestTemplateProcessorRedirect:
    """Tests for redirect (SRAI)."""

    def test_redirect_callback(self):
        """Test redirect uses callback."""
        processor = TemplateProcessor()

        def redirect_fn(pattern):
            if pattern == "HELLO":
                return "Hi there!"
            return ""

        ctx = template_context(redirect_fn=redirect_fn)
        result = processor.process({"redirect": "HELLO"}, ctx)
        assert result == "Hi there!"
        return False

    def test_redirect_with_star(self):
        """Test redirect with star substitution."""
        processor = TemplateProcessor()

        def redirect_fn(pattern):
            # Pattern is substituted as-is (lowercase star value)
            if pattern == "WHAT IS python":
                return "Python is a programming language."
            return ""

        ctx = template_context(stars=["python"], redirect_fn=redirect_fn)
        result = processor.process({"redirect": "WHAT IS {star1}"}, ctx)
        assert result == "Python is a programming language."
        return False

    def test_sr_shorthand(self):
        """Test sr shorthand redirect."""
        processor = TemplateProcessor()

        def redirect_fn(pattern):
            if pattern == "help me":
                return "How can I help?"
            return ""

        ctx = template_context(stars=["help me"], redirect_fn=redirect_fn)
        result = processor.process({"sr": True}, ctx)
        assert result == "How can I help?"
        return False

    def test_redirect_depth_limit(self):
        """Test redirect depth limit."""
        processor = TemplateProcessor(srai_limit=3)

        call_count = [0]

        def redirect_fn(pattern):
            call_count[0] += 1
            # Infinite recursion attempt
            _return_value = processor.process({"redirect": "LOOP"}, template_context(redirect_fn=redirect_fn))
            return _return_value

        ctx = template_context(redirect_fn=redirect_fn)
        processor.process({"redirect": "START"}, ctx)
        # Should stop at limit
        assert call_count[0] <= 4  # Start + 3 redirects
        return False


class TestTemplateProcessorLearn:
    """Tests for learn element."""

    def test_learn_callback(self):
        """Test learn uses callback."""
        processor = TemplateProcessor()

        learned = []

        def learn_fn(data):
            learned.append(data)
            return False

        ctx = template_context(stars=["the sky", "blue"], learn_fn=learn_fn)
        template = {"learn": {"pattern": "{upper:{star1}}", "template": {"text": "{star2}"}}}
        processor.process(template, ctx)

        assert len(learned) == 1
        assert learned[0].get("pattern", "") == "THE SKY"
        # Variables in templates are now resolved at learn time
        assert learned[0].get("template", {}) == {"text": "blue"}
        return False


class TestProcessTemplateFunction:
    """Tests for process_template convenience function."""

    def test_basic_usage(self):
        """Test basic function usage."""
        ctx = template_context(stars=["world"])
        result = process_template("Hello, {star1}!", ctx)
        assert result == "Hello, world!"
        return False

    def test_with_srai_limit(self):
        """Test with custom SRAI limit."""
        ctx = template_context()
        result = process_template({"text": "Test"}, ctx, srai_limit=50)
        assert result == "Test"
        return False


class TestTemplateIntegration:
    """Integration tests for templates."""

    def test_spec_example_my_name_is(self):
        """Test spec example: MY NAME IS pattern."""
        processor = TemplateProcessor()
        ctx = template_context(stars=["Alice"])
        template = {
            "sequence": [
                {"set": {"name": "username", "value": "{star1}"}},
                {"text": "Nice to meet you, {star1}!"},
            ]
        }
        result = processor.process(template, ctx)
        assert result == "Nice to meet you, Alice!"
        assert ctx.get("predicates", {}).get("username", "") == "Alice"
        return False

    def test_spec_example_what_is_my_name(self):
        """Test spec example: WHAT IS MY NAME pattern."""
        processor = TemplateProcessor()

        # When name is set
        ctx1 = template_context(predicates={"username": "Alice"})
        template = {
            "condition": {
                "var": "username",
                "exists": {"text": "Your name is {get:username}."},
                "missing": {"text": "I don't know your name yet."},
            }
        }
        assert processor.process(template, ctx1) == "Your name is Alice."

        # When name is not set
        ctx2 = template_context()
        assert processor.process(template, ctx2) == "I don't know your name yet."
        return False

    def test_nested_template(self):
        """Test deeply nested template."""
        processor = TemplateProcessor()
        ctx = template_context(predicates={"mood": "happy"}, stars=["Alice"])
        template = {
            "sequence": [
                {"set": {"name": "greeted", "value": "true"}},
                {
                    "condition": {
                        "var": "mood",
                        "cases": [
                            {
                                "value": "happy",
                                "template": {"text": "Hello, {star1}! You seem happy!"},
                            },
                            {"default": "Hello, {star1}."},
                        ],
                    }
                },
            ]
        }
        result = processor.process(template, ctx)
        assert result == "Hello, Alice! You seem happy!"
        assert ctx.get("predicates", {}).get("greeted", "") == "true"
        return False


class TestSystemVariables:
    """Tests for system variables."""

    def test_program_variable(self):
        """Test {program} returns bot name."""
        processor = TemplateProcessor()
        ctx = template_context(bot={"name": "TestBot", "version": "1.0"})
        result = processor.process("My name is {program}", ctx)
        assert result == "My name is TestBot"
        return False

    def test_version_variable(self):
        """Test {version} returns bot version."""
        processor = TemplateProcessor()
        ctx = template_context(bot={"name": "TestBot", "version": "2.5.1"})
        result = processor.process("Version {version}", ctx)
        assert result == "Version 2.5.1"
        return False

    def test_program_default(self):
        """Test {program} returns ENGRAM when bot name not set."""
        processor = TemplateProcessor()
        ctx = template_context(bot={})
        result = processor.process("{program}", ctx)
        assert result == "ENGRAM"
        return False

    def test_version_default(self):
        """Test {version} falls back to the package version when bot version not set."""
        processor = TemplateProcessor()
        ctx = template_context(bot={})
        result = processor.process("{version}", ctx)
        assert result == VERSION
        return False

    def test_id_variable(self):
        """Test {id} returns session ID."""
        processor = TemplateProcessor()
        ctx = template_context(session_id="user_12345")
        result = processor.process("Session: {id}", ctx)
        assert result == "Session: user_12345"
        return False

    def test_date_formatted(self):
        """Test {date:format} with custom format."""
        processor = TemplateProcessor()
        ctx = template_context()
        # Use a format that's easy to verify
        result = processor.process("{date:%Y}", ctx)

        assert result == datetime.now().strftime("%Y")
        return False

    def test_date_formatted_complex(self):
        """Test {date:format} with complex format."""
        processor = TemplateProcessor()
        ctx = template_context()
        result = processor.process("{date:%Y-%m-%d}", ctx)

        assert result == datetime.now().strftime("%Y-%m-%d")
        return False

    def test_date_formatted_time(self):
        """Test {date:format} can include time components."""
        processor = TemplateProcessor()
        ctx = template_context()
        result = processor.process("{date:%H:%M}", ctx)

        # Just check format is correct (time may differ by seconds)
        assert len(result) == 5
        assert ":" in result
        return False

    def test_size_variable(self):
        """Test {size} returns category count."""
        processor = TemplateProcessor()
        ctx = template_context(category_count=42)
        result = processor.process("I know {size} things", ctx)
        assert result == "I know 42 things"
        return False

    def test_vocabulary_variable(self):
        """Test {vocabulary} returns vocabulary count."""
        processor = TemplateProcessor()
        ctx = template_context(vocabulary_count=1000)
        result = processor.process("My vocabulary is {vocabulary} words", ctx)
        assert result == "My vocabulary is 1000 words"
        return False


class TestClauseTransform:
    """Tests for the {clause:...} transform."""

    def test_clause_trims_capture(self):
        processor = TemplateProcessor()
        ctx = template_context(stars=["tired i have been working really hard"])
        result = processor.process("You're {clause:{star1}}.", ctx)
        assert result == "You're tired."
        return False

    def test_clause_keeps_single_clause(self):
        processor = TemplateProcessor()
        ctx = template_context(stars=["really happy about the results"])
        result = processor.process("You're {clause:{star1}}.", ctx)
        assert result == "You're really happy about the results."
        return False


class TestQtypeTransform:
    """Tests for the {qtype:...} intent transform."""

    def test_question(self):
        processor = TemplateProcessor()
        ctx = template_context(request_text="What is this?")
        assert processor.process("{qtype:{request}}", ctx) == "question"
        return False

    def test_statement(self):
        processor = TemplateProcessor()
        ctx = template_context(request_text="I like turtles")
        assert processor.process("{qtype:{request}}", ctx) == "statement"
        return False

    def test_command(self):
        processor = TemplateProcessor()
        ctx = template_context(request_text="tell me a story")
        assert processor.process("{qtype:{request}}", ctx) == "command"
        return False


class TestInputVariable:
    """Bare {input} is the current input; {input:N} reads history."""

    def test_bare_input_is_current_input(self):
        processor = TemplateProcessor()
        ctx = template_context(input_text="current words", input_history=["previous turn words"])
        assert processor.process("{input}", ctx) == "current words"
        return False

    def test_indexed_input_reads_history(self):
        processor = TemplateProcessor()
        ctx = template_context(input_text="current words", input_history=["previous turn words", "older words"])
        assert processor.process("{input:1}", ctx) == "previous turn words"
        assert processor.process("{input:2}", ctx) == "older words"
        return False

    def test_bare_input_without_history(self):
        processor = TemplateProcessor()
        ctx = template_context(input_text="current words")
        assert processor.process("{input}", ctx) == "current words"
        return False


class TestNestedPersonClause:
    def test_person_wraps_clause(self):
        """{person:{clause:{star1}}} echoes captures from the bot's point of view."""
        processor = TemplateProcessor()
        ctx = template_context(
            stars=["thrilled about my new project"],
            person_subs={"my": "your", "i": "you", "am": "are"},
        )
        result = processor.process("You're {person:{clause:{star1}}}!", ctx)
        assert result == "You're thrilled about your new project!"
        return False


class TestNameTransform:
    def test_name_extracts_from_capture(self):
        processor = TemplateProcessor()
        ctx = template_context(stars=["still jason by the way"])
        assert processor.process("{name:{star1}}", ctx) == "jason"
        return False

    def test_name_keeps_plain_names(self):
        processor = TemplateProcessor()
        ctx = template_context(stars=["mary jane"])
        assert processor.process("Hello, {name:{star1}}!", ctx) == "Hello, mary jane!"
        return False
