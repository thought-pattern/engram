"""Tests for template processing."""

from engram.constants import VERSION
from engram.template import TemplateProcessor, get_input, get_map, get_response, get_star, process_template, template_context

"""Tests for TemplateContext."""


def test_template_context_get_star():
    """Test star capture retrieval."""
    ctx = template_context(stars=["alice", "pizza"])
    assert get_star(ctx, 1) == "alice"
    assert get_star(ctx, 2) == "pizza"
    assert get_star(ctx, 3) == ""
    assert get_star(ctx, 0) == ""


def test_template_context_get_predicate():
    """Test predicate retrieval via direct access."""
    ctx = template_context(predicates={"name": "Alice", "mood": "happy"})
    assert ctx["predicates"].get("name") == "Alice"
    assert ctx["predicates"].get("mood") == "happy"
    assert ctx["predicates"].get("missing", "") == ""
    assert ctx["predicates"].get("missing", "default") == "default"


def test_template_context_set_predicate():
    """Test predicate setting via direct access."""
    ctx = template_context()
    ctx["predicates"]["name"] = "Bob"
    assert ctx["predicates"]["name"] == "Bob"


def test_template_context_topic_in_predicates():
    """Test topic from predicates."""
    ctx = template_context(predicates={"topic": "WEATHER"})
    assert ctx["predicates"].get("topic", "") == "WEATHER"


def test_template_context_topic_empty():
    """Test topic when not set."""
    ctx = template_context()
    assert ctx["predicates"].get("topic", "") == ""


def test_template_context_that_from_history():
    """Test getting that from history."""
    ctx = template_context(that_history=[["HELLO", "HOW ARE YOU"], ["GOODBYE"]])
    # Most recent bot response is first sentence of first history entry
    assert ctx["that_history"][0][0] == "HELLO"


def test_template_context_that_empty():
    """Test that when empty."""
    ctx = template_context()
    assert len(ctx["that_history"]) == 0


def test_template_context_get_input():
    """Test input history retrieval."""
    ctx = template_context(input_history=["latest", "previous", "oldest"])
    assert get_input(ctx, 1) == "latest"
    assert get_input(ctx, 2) == "previous"
    assert get_input(ctx, 3) == "oldest"
    assert get_input(ctx, 4) == ""


def test_template_context_get_response():
    """Test response history retrieval."""
    ctx = template_context(response_history=["last", "before"])
    assert get_response(ctx, 1) == "last"
    assert get_response(ctx, 2) == "before"
    assert get_response(ctx, 3) == ""


def test_template_context_get_bot():
    """Test bot property retrieval via direct access."""
    ctx = template_context(bot={"name": "TestBot", "version": "1.0"})
    assert ctx["bot"].get("name") == "TestBot"
    assert ctx["bot"].get("missing", "") == ""
    assert ctx["bot"].get("missing", "default") == "default"


def test_template_context_get_map():
    """Test map lookup."""
    ctx = template_context(maps={"capital": {"france": "paris", "germany": "berlin"}})
    assert get_map(ctx, "capital", "france") == "paris"
    assert get_map(ctx, "capital", "GERMANY") == "berlin"  # Case insensitive
    assert get_map(ctx, "capital", "unknown") == ""
    assert get_map(ctx, "capital", "unknown", "n/a") == "n/a"
    assert get_map(ctx, "missing_map", "key") == ""


"""Tests for basic template processing."""


def test_template_processor_basic_plain_string():
    """Test processing plain string."""
    processor = TemplateProcessor()
    ctx = template_context()
    assert processor.process("Hello, world!", ctx) == "Hello, world!"


def test_template_processor_basic_text_template():
    """Test text template."""
    processor = TemplateProcessor()
    ctx = template_context()
    assert processor.process({"text": "Hello!"}, ctx) == "Hello!"


def test_template_processor_basic_star_substitution():
    """Test star wildcard substitution."""
    processor = TemplateProcessor()
    ctx = template_context(stars=["Alice", "pizza"])
    result = processor.process("Hello, {star1}! You like {star2}.", ctx)
    assert result == "Hello, Alice! You like pizza."


def test_template_processor_basic_star_missing():
    """Test missing star returns empty."""
    processor = TemplateProcessor()
    ctx = template_context(stars=["Alice"])
    result = processor.process("Hello, {star1} and {star2}!", ctx)
    assert result == "Hello, Alice and !"


def test_template_processor_basic_get_predicate():
    """Test predicate retrieval in template."""
    processor = TemplateProcessor()
    ctx = template_context(predicates={"username": "Bob"})
    result = processor.process("Hello, {get:username}!", ctx)
    assert result == "Hello, Bob!"


def test_template_processor_basic_get_predicate_with_default():
    """Test predicate with default value."""
    processor = TemplateProcessor()
    ctx = template_context()
    result = processor.process("Mood: {get:mood:neutral}", ctx)
    assert result == "Mood: neutral"


def test_template_processor_basic_bot_property():
    """Test bot property access."""
    processor = TemplateProcessor()
    ctx = template_context(bot={"name": "TestBot"})
    result = processor.process("I am {bot:name}.", ctx)
    assert result == "I am TestBot."


def test_template_processor_basic_map_lookup():
    """Test map lookup."""
    processor = TemplateProcessor()
    ctx = template_context(stars=["france"], maps={"capital": {"france": "Paris"}})
    result = processor.process("Capital: {map:capital:{star1}}", ctx)
    assert result == "Capital: Paris"


"""Tests for random template."""


def test_template_processor_random_random_selection():
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


def test_template_processor_random_random_with_template():
    """Test random with nested template."""
    processor = TemplateProcessor()
    ctx = template_context(stars=["Alice"])
    template = {"random": [{"text": "Hello, {star1}!"}, {"text": "Hi, {star1}!"}]}
    result = processor.process(template, ctx)
    assert result in ["Hello, Alice!", "Hi, Alice!"]


"""Tests for conditional templates."""


def test_template_processor_condition_condition_exists():
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


def test_template_processor_condition_condition_by_value():
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


def test_template_processor_condition_condition_by_pattern():
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


def test_template_processor_condition_condition_loop_counts_down():
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
    assert result == "3" "2" "liftoff"


def test_template_processor_condition_condition_loop_never_changing_terminates():
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


"""Tests for sequence templates."""


def test_template_processor_sequence_sequence_basic():
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
    assert ctx["predicates"]["topic"] == "weather"


def test_template_processor_sequence_sequence_multiple_text():
    """Test sequence with multiple text outputs."""
    processor = TemplateProcessor()
    ctx = template_context()
    template = {"sequence": [{"text": "First."}, {"text": "Second."}]}
    result = processor.process(template, ctx)
    assert result == "First. Second."


"""Tests for think (silent processing)."""


def test_template_processor_think_think_no_output():
    """Test think produces no output."""
    processor = TemplateProcessor()
    ctx = template_context()
    template = {"think": [{"set": {"name": "secret", "value": "hidden"}}]}
    result = processor.process(template, ctx)
    assert result == ""
    assert ctx["predicates"]["secret"] == "hidden"


def test_template_processor_think_think_in_sequence():
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


"""Tests for text transforms."""


def test_template_processor_transforms_upper():
    """Test uppercase transform."""
    processor = TemplateProcessor()
    ctx = template_context(stars=["hello"])
    result = processor.process("{upper:{star1}}", ctx)
    assert result == "HELLO"


def test_template_processor_transforms_lower():
    """Test lowercase transform."""
    processor = TemplateProcessor()
    ctx = template_context(stars=["HELLO"])
    result = processor.process("{lower:{star1}}", ctx)
    assert result == "hello"


def test_template_processor_transforms_capitalize():
    """Test capitalize transform."""
    processor = TemplateProcessor()
    ctx = template_context(stars=["hello world"])
    result = processor.process("{capitalize:{star1}}", ctx)
    assert result == "Hello world"


def test_template_processor_transforms_formal():
    """Test formal (title case) transform."""
    processor = TemplateProcessor()
    ctx = template_context(stars=["hello world"])
    result = processor.process("{formal:{star1}}", ctx)
    assert result == "Hello World"


def test_template_processor_transforms_explode():
    """Test explode transform."""
    processor = TemplateProcessor()
    ctx = template_context(stars=["abc"])
    result = processor.process("{explode:{star1}}", ctx)
    assert result == "a b c"


"""Tests for string utility transforms."""


def test_string_utilities_first_word():
    """Test first word extraction."""
    processor = TemplateProcessor()
    ctx = template_context(stars=["hello world there"])
    result = processor.process("{first:{star1}}", ctx)
    assert result == "hello"


def test_string_utilities_first_single_word():
    """Test first with single word."""
    processor = TemplateProcessor()
    ctx = template_context(stars=["hello"])
    result = processor.process("{first:{star1}}", ctx)
    assert result == "hello"


def test_string_utilities_first_empty():
    """Test first with empty string."""
    processor = TemplateProcessor()
    ctx = template_context(stars=[""])
    result = processor.process("{first:{star1}}", ctx)
    assert result == ""


def test_string_utilities_rest_multiple_words():
    """Test rest with multiple words."""
    processor = TemplateProcessor()
    ctx = template_context(stars=["hello world there"])
    result = processor.process("{rest:{star1}}", ctx)
    assert result == "world there"


def test_string_utilities_rest_two_words():
    """Test rest with two words."""
    processor = TemplateProcessor()
    ctx = template_context(stars=["hello world"])
    result = processor.process("{rest:{star1}}", ctx)
    assert result == "world"


def test_string_utilities_rest_single_word():
    """Test rest with single word returns empty."""
    processor = TemplateProcessor()
    ctx = template_context(stars=["hello"])
    result = processor.process("{rest:{star1}}", ctx)
    assert result == ""


def test_string_utilities_rest_empty():
    """Test rest with empty string."""
    processor = TemplateProcessor()
    ctx = template_context(stars=[""])
    result = processor.process("{rest:{star1}}", ctx)
    assert result == ""


def test_string_utilities_uniq_with_duplicates():
    """Test unique word removal."""
    processor = TemplateProcessor()
    ctx = template_context(stars=["the the cat sat on the mat"])
    result = processor.process("{uniq:{star1}}", ctx)
    assert result == "the cat sat on mat"


def test_string_utilities_uniq_no_duplicates():
    """Test uniq with no duplicates."""
    processor = TemplateProcessor()
    ctx = template_context(stars=["hello world"])
    result = processor.process("{uniq:{star1}}", ctx)
    assert result == "hello world"


def test_string_utilities_uniq_all_same():
    """Test uniq with all same words."""
    processor = TemplateProcessor()
    ctx = template_context(stars=["a a a a"])
    result = processor.process("{uniq:{star1}}", ctx)
    assert result == "a"


def test_string_utilities_uniq_empty():
    """Test uniq with empty string."""
    processor = TemplateProcessor()
    ctx = template_context(stars=[""])
    result = processor.process("{uniq:{star1}}", ctx)
    assert result == ""


def test_string_utilities_wordcount_multiple():
    """Test word counting."""
    processor = TemplateProcessor()
    ctx = template_context(stars=["hello world"])
    result = processor.process("{wordcount:{star1}}", ctx)
    assert result == "2"


def test_string_utilities_wordcount_single():
    """Test word count with single word."""
    processor = TemplateProcessor()
    ctx = template_context(stars=["hello"])
    result = processor.process("{wordcount:{star1}}", ctx)
    assert result == "1"


def test_string_utilities_wordcount_empty():
    """Test word count with empty string."""
    processor = TemplateProcessor()
    ctx = template_context(stars=[""])
    result = processor.process("{wordcount:{star1}}", ctx)
    assert result == "0"


def test_string_utilities_wordcount_many():
    """Test word count with many words."""
    processor = TemplateProcessor()
    ctx = template_context(stars=["one two three four five"])
    result = processor.process("{wordcount:{star1}}", ctx)
    assert result == "5"


def test_string_utilities_nested_first_upper():
    """Test nested transforms: first then upper."""
    processor = TemplateProcessor()
    ctx = template_context(stars=["hello world"])
    result = processor.process("{upper:{first:{star1}}}", ctx)
    assert result == "HELLO"


"""Tests for redirect (SRAI)."""


def test_template_processor_redirect_redirect_callback():
    """Test redirect uses callback."""
    processor = TemplateProcessor()

    def redirect_fn(pattern):
        if pattern == "HELLO":
            result = "Hi there!"
            return result
        result = ""
        return result

    ctx = template_context(redirect_fn=redirect_fn)
    result = processor.process({"redirect": "HELLO"}, ctx)
    assert result == "Hi there!"


def test_template_processor_redirect_redirect_with_star():
    """Test redirect with star substitution."""
    processor = TemplateProcessor()

    def redirect_fn(pattern):
        # Pattern is substituted as-is (lowercase star value)
        if pattern == "WHAT IS python":
            result = "Python is a programming language."
            return result
        result = ""
        return result

    ctx = template_context(stars=["python"], redirect_fn=redirect_fn)
    result = processor.process({"redirect": "WHAT IS {star1}"}, ctx)
    assert result == "Python is a programming language."


def test_template_processor_redirect_sr_shorthand():
    """Test sr shorthand redirect."""
    processor = TemplateProcessor()

    def redirect_fn(pattern):
        if pattern == "help me":
            result = "How can I help?"
            return result
        result = ""
        return result

    ctx = template_context(stars=["help me"], redirect_fn=redirect_fn)
    result = processor.process({"sr": True}, ctx)
    assert result == "How can I help?"


def test_template_processor_redirect_redirect_depth_limit():
    """Test redirect depth limit."""
    processor = TemplateProcessor(srai_limit=3)

    call_count = [0]

    def redirect_fn(pattern):
        call_count[0] += 1
        # Infinite recursion attempt
        result = processor.process({"redirect": "LOOP"}, template_context(redirect_fn=redirect_fn))
        return result

    ctx = template_context(redirect_fn=redirect_fn)
    processor.process({"redirect": "START"}, ctx)
    # Should stop at limit
    assert call_count[0] <= 4  # Start + 3 redirects


"""Tests for learn element."""


def test_template_processor_learn_learn_callback():
    """Test learn uses callback."""
    processor = TemplateProcessor()

    learned = []

    def learn_fn(data):
        learned.append(data)

    ctx = template_context(stars=["the sky", "blue"], learn_fn=learn_fn)
    template = {"learn": {"pattern": "{upper:{star1}}", "template": {"text": "{star2}"}}}
    processor.process(template, ctx)

    assert len(learned) == 1
    assert learned[0]["pattern"] == "THE SKY"
    # Variables in templates are now resolved at learn time
    assert learned[0]["template"] == {"text": "blue"}


"""Tests for process_template convenience function."""


def test_process_template_function_basic_usage():
    """Test basic function usage."""
    ctx = template_context(stars=["world"])
    result = process_template("Hello, {star1}!", ctx)
    assert result == "Hello, world!"


def test_process_template_function_with_srai_limit():
    """Test with custom SRAI limit."""
    ctx = template_context()
    result = process_template({"text": "Test"}, ctx, srai_limit=50)
    assert result == "Test"


"""Integration tests for templates."""


def test_template_integration_spec_example_my_name_is():
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
    assert ctx["predicates"]["username"] == "Alice"


def test_template_integration_spec_example_what_is_my_name():
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


def test_template_integration_nested_template():
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
    assert ctx["predicates"]["greeted"] == "true"


"""Tests for system variables."""


def test_system_variables_program_variable():
    """Test {program} returns bot name."""
    processor = TemplateProcessor()
    ctx = template_context(bot={"name": "TestBot", "version": "1.0"})
    result = processor.process("My name is {program}", ctx)
    assert result == "My name is TestBot"


def test_system_variables_version_variable():
    """Test {version} returns bot version."""
    processor = TemplateProcessor()
    ctx = template_context(bot={"name": "TestBot", "version": "2.5.1"})
    result = processor.process("Version {version}", ctx)
    assert result == "Version 2.5.1"


def test_system_variables_program_default():
    """Test {program} returns ENGRAM when bot name not set."""
    processor = TemplateProcessor()
    ctx = template_context(bot={})
    result = processor.process("{program}", ctx)
    assert result == "ENGRAM"


def test_system_variables_version_default():
    """Test {version} falls back to the package version when bot version not set."""
    processor = TemplateProcessor()
    ctx = template_context(bot={})
    result = processor.process("{version}", ctx)
    assert result == VERSION


def test_system_variables_id_variable():
    """Test {id} returns session ID."""
    processor = TemplateProcessor()
    ctx = template_context(session_id="user_12345")
    result = processor.process("Session: {id}", ctx)
    assert result == "Session: user_12345"


def test_system_variables_date_formatted():
    """Test {date:format} with custom format."""
    processor = TemplateProcessor()
    ctx = template_context()
    # Use a format that's easy to verify
    result = processor.process("{date:%Y}", ctx)
    from datetime import datetime

    assert result == datetime.now().strftime("%Y")


def test_system_variables_date_formatted_complex():
    """Test {date:format} with complex format."""
    processor = TemplateProcessor()
    ctx = template_context()
    result = processor.process("{date:%Y-%m-%d}", ctx)
    from datetime import datetime

    assert result == datetime.now().strftime("%Y-%m-%d")


def test_system_variables_date_formatted_time():
    """Test {date:format} can include time components."""
    processor = TemplateProcessor()
    ctx = template_context()
    result = processor.process("{date:%H:%M}", ctx)

    # Just check format is correct (time may differ by seconds)
    assert len(result) == 5
    assert ":" in result


def test_system_variables_size_variable():
    """Test {size} returns category count."""
    processor = TemplateProcessor()
    ctx = template_context(category_count=42)
    result = processor.process("I know {size} things", ctx)
    assert result == "I know 42 things"


def test_system_variables_vocabulary_variable():
    """Test {vocabulary} returns vocabulary count."""
    processor = TemplateProcessor()
    ctx = template_context(vocabulary_count=1000)
    result = processor.process("My vocabulary is {vocabulary} words", ctx)
    assert result == "My vocabulary is 1000 words"


"""Tests for the {clause:...} transform."""


def test_clause_transform_clause_trims_capture():
    processor = TemplateProcessor()
    ctx = template_context(stars=["tired i have been working really hard"])
    result = processor.process("You're {clause:{star1}}.", ctx)
    assert result == "You're tired."


def test_clause_transform_clause_keeps_single_clause():
    processor = TemplateProcessor()
    ctx = template_context(stars=["really happy about the results"])
    result = processor.process("You're {clause:{star1}}.", ctx)
    assert result == "You're really happy about the results."


"""Tests for the {qtype:...} intent transform."""


def test_qtype_transform_question():
    processor = TemplateProcessor()
    ctx = template_context(request_text="What is this?")
    assert processor.process("{qtype:{request}}", ctx) == "question"


def test_qtype_transform_statement():
    processor = TemplateProcessor()
    ctx = template_context(request_text="I like turtles")
    assert processor.process("{qtype:{request}}", ctx) == "statement"


def test_qtype_transform_command():
    processor = TemplateProcessor()
    ctx = template_context(request_text="tell me a story")
    assert processor.process("{qtype:{request}}", ctx) == "command"


"""Bare {input} is the current input; {input:N} reads history."""


def test_input_variable_bare_input_is_current_input():
    processor = TemplateProcessor()
    ctx = template_context(input_text="current words", input_history=["previous turn words"])
    assert processor.process("{input}", ctx) == "current words"


def test_input_variable_indexed_input_reads_history():
    processor = TemplateProcessor()
    ctx = template_context(input_text="current words", input_history=["previous turn words", "older words"])
    assert processor.process("{input:1}", ctx) == "previous turn words"
    assert processor.process("{input:2}", ctx) == "older words"


def test_input_variable_bare_input_without_history():
    processor = TemplateProcessor()
    ctx = template_context(input_text="current words")
    assert processor.process("{input}", ctx) == "current words"


def test_nested_person_clause_person_wraps_clause():
    """{person:{clause:{star1}}} echoes captures from the bot's point of view."""
    processor = TemplateProcessor()
    ctx = template_context(
        stars=["thrilled about my new project"],
        person_subs={"my": "your", "i": "you", "am": "are"},
    )
    result = processor.process("You're {person:{clause:{star1}}}!", ctx)
    assert result == "You're thrilled about your new project!"


def test_name_transform_name_extracts_from_capture():
    processor = TemplateProcessor()
    ctx = template_context(stars=["still jason by the way"])
    assert processor.process("{name:{star1}}", ctx) == "jason"


def test_name_transform_name_keeps_plain_names():
    processor = TemplateProcessor()
    ctx = template_context(stars=["mary jane"])
    assert processor.process("Hello, {name:{star1}}!", ctx) == "Hello, mary jane!"
