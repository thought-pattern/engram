"""Template processing for ENGRAM.

This module provides template parsing and evaluation for dynamic response generation.
Templates can be simple strings or structured JSON objects with variable substitution,
random selection, conditionals, redirects, and more.

The evaluation context is a plain dict normalized by ``template_context``.
"""

from contextvars import ContextVar
from datetime import UTC, datetime
from random import choice as random_choice
from re import compile as re_compile, match as re_match

from engram.constants import (
    TEMPLATE_BOT_EXPRESSION,
    TEMPLATE_DATE_FORMAT_EXPRESSION,
    TEMPLATE_ESCAPABLE_CHARACTERS,
    TEMPLATE_ESCAPE_CHARACTER,
    TEMPLATE_GET_EXPRESSION,
    TEMPLATE_INPUT_EXPRESSION,
    TEMPLATE_MAP_EXPRESSION,
    TEMPLATE_RESPONSE_EXPRESSION,
    TEMPLATE_SIMPLE_VARIABLE_TOKENS,
    TEMPLATE_STAR_EXPRESSION,
    TEMPLATE_THAT_EXPRESSION,
    TEMPLATE_THATSTAR_EXPRESSION,
    TEMPLATE_TOPICSTAR_EXPRESSION,
    TEMPLATE_TRANSFORM_EXPRESSION,
    TRIPLE_QUERY_OBJECT,
    TRIPLE_QUERY_SUBJECT,
    VERSION,
)
from engram.graph import graph_is_empty, graph_single, is_write_cypher, projection_timestamp
from engram.nlp import input_kind
from engram.sentiment import sentiment_label
from engram.text import extract_name, first_clause

star_pattern = re_compile(TEMPLATE_STAR_EXPRESSION)
thatstar_pattern = re_compile(TEMPLATE_THATSTAR_EXPRESSION)
topicstar_pattern = re_compile(TEMPLATE_TOPICSTAR_EXPRESSION)
get_pattern = re_compile(TEMPLATE_GET_EXPRESSION)
bot_pattern = re_compile(TEMPLATE_BOT_EXPRESSION)
map_pattern = re_compile(TEMPLATE_MAP_EXPRESSION)
input_pattern = re_compile(TEMPLATE_INPUT_EXPRESSION)
response_pattern = re_compile(TEMPLATE_RESPONSE_EXPRESSION)
that_pattern = re_compile(TEMPLATE_THAT_EXPRESSION)
transform_pattern = re_compile(TEMPLATE_TRANSFORM_EXPRESSION)
date_format_pattern = re_compile(TEMPLATE_DATE_FORMAT_EXPRESSION)

# The generator for <random>. A conversation turn with a fixed seed sets its
# own here for the length of the turn; everything else uses the shared one.
TEMPLATE_RANDOM: ContextVar = ContextVar("template_random", default=())


def template_context(
    # Wildcard captures from pattern matching
    stars=(),
    thatstars=(),
    topicstars=(),
    # Session state
    predicates=(),
    input_history=(),
    response_history=(),
    that_history=(),
    # Current input
    input_text: str = "",
    request_text: str = "",
    # Bot properties
    bot=(),
    # Maps for lookups
    maps=(),
    # Substitution maps
    person_subs=(),
    person2_subs=(),
    gender_subs=(),
    # System info
    session_id: str = "",
    category_count: int = 0,
    vocabulary_count: int = 0,
    evaluation_time: str = "",
    # Callbacks (set by processor)
    redirect_fn=(),
    learn_fn=(),
    graph_fn=(),
) -> dict:
    """Validate and normalize the evaluation context for one template turn.

    Contains all data needed to evaluate a template, including wildcard captures,
    session predicates, bot properties, and history. Captures and histories are
    copied to lists, a non-dict mapping argument becomes an empty map, and the
    evaluation time defaults to the current UTC instant and must parse as a UTC
    timestamp. The predicates dict is kept by reference so ``set`` writes
    reach the session that owns it.
    """
    selected_evaluation_time = evaluation_time or datetime.now(UTC).isoformat().replace("+00:00", "Z")
    projection_timestamp(selected_evaluation_time, True, "template evaluation time")
    context = {
        "stars": list(stars or ()),
        "thatstars": list(thatstars or ()),
        "topicstars": list(topicstars or ()),
        "predicates": predicates if isinstance(predicates, dict) else {},
        "input_history": list(input_history or ()),
        "response_history": list(response_history or ()),
        "that_history": list(that_history or ()),
        "input_text": input_text,
        "request_text": request_text,
        "bot": bot if isinstance(bot, dict) else {},
        "maps": maps if isinstance(maps, dict) else {},
        "person_subs": person_subs if isinstance(person_subs, dict) else {},
        "person2_subs": person2_subs if isinstance(person2_subs, dict) else {},
        "gender_subs": gender_subs if isinstance(gender_subs, dict) else {},
        "session_id": session_id,
        "category_count": category_count,
        "vocabulary_count": vocabulary_count,
        "evaluation_time": selected_evaluation_time,
        "redirect_fn": redirect_fn,
        "learn_fn": learn_fn,
        "graph_fn": graph_fn,
    }
    return context


# Helper functions for 1-based index access (used by TemplateProcessor)


def get_star(ctx: dict, index: int) -> str:
    """Get star capture by 1-based index."""
    if 1 <= index <= len(ctx.get("stars", [])):
        star = ctx.get("stars", [])[index - 1]
        return star
    result = ""
    return result


def get_thatstar(ctx: dict, index: int) -> str:
    """Get thatstar capture by 1-based index."""
    if 1 <= index <= len(ctx.get("thatstars", [])):
        thatstar = ctx.get("thatstars", [])[index - 1]
        return thatstar
    result = ""
    return result


def get_topicstar(ctx: dict, index: int) -> str:
    """Get topicstar capture by 1-based index."""
    if 1 <= index <= len(ctx.get("topicstars", [])):
        topicstar = ctx.get("topicstars", [])[index - 1]
        return topicstar
    result = ""
    return result


def get_map(ctx: dict, map_name: str, key: str, default: str = "") -> str:
    """Get value from named map."""
    maps = ctx.get("maps", {})
    if map_name not in maps:
        return default
    named_map = maps.get(map_name, {})
    if not isinstance(named_map, dict):
        raise TypeError("a template map must be a dictionary")
    value = named_map.get(key.lower(), default)
    return value


def get_input(ctx: dict, index: int = 1) -> str:
    """Get input from history (1-based, 1=most recent)."""
    if 1 <= index <= len(ctx.get("input_history", [])):
        value = ctx.get("input_history", [])[index - 1]
        return value
    result = ""
    return result


def get_response(ctx: dict, index: int = 1) -> str:
    """Get response from history (1-based, 1=most recent)."""
    if 1 <= index <= len(ctx.get("response_history", [])):
        value = ctx.get("response_history", [])[index - 1]
        return value
    result = ""
    return result


def get_that(ctx: dict, response_idx: int = 1, sentence_idx: int = 1) -> str:
    """Get that by response and sentence index (1-based)."""
    if 1 <= response_idx <= len(ctx.get("that_history", [])):
        sentences = ctx.get("that_history", [])[response_idx - 1]
        if 1 <= sentence_idx <= len(sentences):
            sentence = sentences[sentence_idx - 1]
            return sentence
    result = ""
    return result


def simple_variable_values(context: dict) -> dict[str, str]:
    """Render the current value of each centralized simple-variable token."""

    result = {
        TEMPLATE_SIMPLE_VARIABLE_TOKENS.get("topic", ""): context.get("predicates", {}).get("topic", ""),
        TEMPLATE_SIMPLE_VARIABLE_TOKENS.get("input", ""): context.get("input_text", ""),
        TEMPLATE_SIMPLE_VARIABLE_TOKENS.get("request", ""): context.get("request_text", ""),
        TEMPLATE_SIMPLE_VARIABLE_TOKENS.get("id", ""): context.get("session_id", ""),
        TEMPLATE_SIMPLE_VARIABLE_TOKENS.get("size", ""): str(context.get("category_count", 0)),
        TEMPLATE_SIMPLE_VARIABLE_TOKENS.get("vocabulary", ""): str(context.get("vocabulary_count", 0)),
        TEMPLATE_SIMPLE_VARIABLE_TOKENS.get("date", ""): datetime.now().strftime("%B %d, %Y"),
        TEMPLATE_SIMPLE_VARIABLE_TOKENS.get("time", ""): datetime.now().strftime("%H:%M:%S"),
        TEMPLATE_SIMPLE_VARIABLE_TOKENS.get("program", ""): context.get("bot", {}).get("name", "ENGRAM"),
        TEMPLATE_SIMPLE_VARIABLE_TOKENS.get("version", ""): context.get("bot", {}).get("version", VERSION),
    }
    return result


def format_template_date(date_format: str, token: str) -> str:
    """Render a ``{date:...}`` token; a format strftime rejects leaves the token as written."""
    try:
        formatted = datetime.now().strftime(date_format)
    except ValueError:
        return token
    return formatted


def template_token_end(text: str, start: int) -> int:
    """Return the index of the brace closing the token opened at ``start``, or -1 when it is unclosed."""
    depth = 0
    position = start
    while position < len(text):
        character = text[position]
        if character == TEMPLATE_ESCAPE_CHARACTER:
            position += 2
            continue
        if character == "{":
            depth += 1
        elif character == "}":
            depth -= 1
            if not depth:
                return position
        position += 1
    result = -1
    return result


def escape_template_text(text: str) -> str:
    """Escape rendered text so that rendering it again reproduces it literally."""
    result = "".join(
        TEMPLATE_ESCAPE_CHARACTER + character if character in TEMPLATE_ESCAPABLE_CHARACTERS else character for character in text
    )
    return result


def apply_word_substitution(text: str, substitutions: dict[str, str]) -> str:
    """Apply a case-preserving word substitution map."""
    if not substitutions:
        result = text
        return result
    words = text.split()
    replaced = []
    for word in words:
        lower = word.lower()
        if lower in substitutions:
            replacement = substitutions.get(lower, "")
            if word.isupper():
                replacement = replacement.upper()
            elif word[0].isupper():
                replacement = replacement.capitalize()
            replaced.append(replacement)
        else:
            replaced.append(word)
    result = " ".join(replaced)
    return result


class TemplateProcessor:
    """Processes templates and substitutes variables."""

    def __init__(self, srai_limit: int = 100):
        """Initialize processor.

        Args:
            srai_limit: Maximum redirect recursion depth. Also caps condition
                loop re-evaluation, so a loop whose predicate never changes
                terminates instead of recursing without bound.
        """
        self.srai_limit = srai_limit
        self.srai_depth = 0
        self.loop_depth = 0

    def process(self, template, context: dict) -> str:
        """Process a template and return the output string.

        Args:
            template: Template to process (string or dict).
            context: Evaluation context.

        Returns:
            Processed output string.
        """
        if not template:
            result = ""
            return result

        if isinstance(template, str):
            result = self.substitute_variables(template, context)
            return result

        if isinstance(template, dict):
            result = self.process_dict_template(template, context)
            return result

        if isinstance(template, list):
            # List is treated as sequence
            result = self.process_sequence(template, context)
            return result

        result = str(template)
        return result

    def process_dict_template(self, template: dict, context: dict) -> str:
        """Process a dictionary template."""

        if "text" in template:
            result = self.substitute_variables(str(template.get("text", "")), context)
            return result

        if "random" in template:
            result = self.process_random(template.get("random", []), context)
            return result

        if "condition" in template:
            result = self.process_condition(template.get("condition", {}), context)
            return result

        if "sequence" in template:
            result = self.process_sequence(template.get("sequence", []), context)
            return result

        if "redirect" in template:
            result = self.process_redirect(self.substitute_variables(template.get("redirect", ""), context), context)
            return result

        if "sr" in template and template.get("sr", False):
            # The capture is redirected as input text, never rendered as a template.
            if context.get("stars", []):
                result = self.process_redirect(context.get("stars", [])[0], context)
                return result
            result = ""
            return result

        if "think" in template:
            self.process_think(template.get("think", []), context)
            result = ""
            return result

        if "set" in template:
            self.process_set(template.get("set", {}), context)
            result = ""
            return result

        if "learn" in template:
            self.process_learn(template.get("learn", {}), context)
            result = ""
            return result

        if "loop" in template and template.get("loop", False):
            result = ""
            return result

        if "graph_query" in template:
            result = self.process_graph_query(template.get("graph_query", {}), context)
            return result

        if "triple_query" in template:
            result = self.process_triple_query(template.get("triple_query", {}), context)
            return result

        result = ""
        return result

    def process_random(self, choices: list, context: dict) -> str:
        """Process random selection."""
        if not choices:
            result = ""
            return result
        generator = TEMPLATE_RANDOM.get()
        choice = generator.choice(choices) if generator else random_choice(choices)
        result = self.process(choice, context)
        return result

    def process_condition(self, condition: dict, context: dict) -> str:
        """Process conditional template."""
        var_name = condition.get("name", "")
        var_value = context.get("predicates", {}).get(var_name, "")

        if "exists" in condition or "missing" in condition:
            if var_value:
                result = self.process(condition.get("exists", ""), context)
                return result
            else:
                result = self.process(condition.get("missing", ""), context)
                return result

        if "pattern" in condition:
            pattern = condition.get("pattern", "")
            if re_match(pattern, var_value):
                result = self.process(condition.get("match", ""), context)
                return result
            else:
                result = self.process(condition.get("nomatch", ""), context)
                return result

        branches = condition.get("branches", [])
        if branches:
            for case in branches:
                if "value" in case:
                    if var_value == case.get("value", ""):
                        result_template = case.get("then", "")
                        result = self.process(result_template, context)
                        # Check for loop (bounded so a predicate that never
                        # changes cannot recurse forever)
                        if isinstance(result_template, dict) and "loop" in result_template and self.loop_depth < self.srai_limit:
                            self.loop_depth += 1
                            try:
                                combined = result + self.process_condition(condition, context)
                            finally:
                                self.loop_depth -= 1
                            return combined
                        return result
                elif "then" in case:
                    result_template = case.get("then", "")
                    result = self.process(result_template, context)
                    # Check for loop in default (same bound as above)
                    if isinstance(result_template, dict) and "loop" in result_template and self.loop_depth < self.srai_limit:
                        self.loop_depth += 1
                        try:
                            combined = result + self.process_condition(condition, context)
                        finally:
                            self.loop_depth -= 1
                        return combined
                    return result

        result = ""
        return result

    def process_sequence(self, sequence: list, context: dict) -> str:
        """Process sequence of templates, returning last text output."""
        output_parts = []
        for item in sequence:
            result = self.process(item, context)
            if result:
                output_parts.append(result)
        output = " ".join(output_parts) if output_parts else ""
        return output

    def process_redirect(self, resolved_pattern: str, context: dict) -> str:
        """Redirect (SRAI) already rendered input text."""
        if self.srai_depth >= self.srai_limit:
            result = ""
            return result

        redirect = context.get("redirect_fn", ())
        if redirect:
            self.srai_depth += 1
            try:
                response = redirect(resolved_pattern)
                return response
            finally:
                self.srai_depth -= 1

        result = ""
        return result

    def process_think(self, items: list, context: dict) -> None:
        """Process think elements (silent, no output)."""
        for item in items:
            self.process(item, context)

    def process_set(self, set_data: dict, context: dict) -> None:
        """Process set variable."""
        name = set_data.get("name", "")
        value = set_data.get("value", "")
        if name:
            resolved_value = self.substitute_variables(value, context)
            context.get("predicates", {})[name] = resolved_value

    def process_learn(self, learn_data: dict, context: dict) -> None:
        """Process learn element."""
        learn = context.get("learn_fn", ())
        if learn:
            resolved = {}
            if "pattern" in learn_data:
                resolved["pattern"] = self.substitute_variables(learn_data.get("pattern", ""), context).upper()
            if "template" in learn_data:
                resolved["template"] = self.resolve_template_vars(learn_data.get("template", {}), context)
            if "that" in learn_data:
                resolved["that"] = self.substitute_variables(learn_data.get("that", ""), context)
            if "topic" in learn_data:
                resolved["topic"] = self.substitute_variables(learn_data.get("topic", ""), context)
            resolved["tier"] = learn_data.get("tier", "DYNAMIC")

            learn(resolved)

    def resolve_template_vars(self, template, context: dict):
        """Render every text leaf of a template that ``learn`` stores.

        Each leaf is rendered now and escaped, so the learned template later
        reproduces this text literally; a capture taught into it never acts as
        template syntax when the learned category fires.
        """
        if isinstance(template, str):
            resolved = escape_template_text(self.substitute_variables(template, context))
            return resolved
        elif isinstance(template, dict):
            resolved = {k: self.resolve_template_vars(v, context) for k, v in template.items()}
            return resolved
        elif isinstance(template, list):
            resolved = [self.resolve_template_vars(item, context) for item in template]
            return resolved
        return template

    def process_graph_query(self, query_data: dict, context: dict) -> str:
        """Process graph query operation."""
        graph = context.get("graph_fn", ())
        if not graph:
            output = self.process(query_data.get("on_failure", ""), context)
            return output

        query = self.substitute_variables(query_data.get("query", ""), context)
        if is_write_cypher(query):
            output = self.process(query_data.get("on_failure", ""), context)
            return output
        params = {}
        for key, value in query_data.get("params", {}).items():
            params[key] = self.substitute_variables(str(value), context)

        # Expected graph unavailability is normalized by the graph owner. A
        # callback defect remains visible to its caller.
        records = graph(query, params)
        if not records:
            records = []

        if graph_is_empty(records):
            output = self.process(query_data.get("on_empty", query_data.get("on_failure", "")), context)
            return output

        format_type = query_data.get("format", "single")

        # Graph values are bound as literal data: record fields in the item
        # template and the result in the success text are never rendered as
        # template syntax.
        if format_type == "list":
            item_template = query_data.get("item_template", "{result}")
            join_str = query_data.get("join", ", ")
            items = []
            for record in records:
                bindings = {str(key): str(value) for key, value in record.items()}
                items.append(self.substitute_variables(item_template, context, bindings))
            result_str = join_str.join(items)
        else:
            record = graph_single(records) or {}
            result_str = str(record.get("result", ""))

        success_template = query_data.get("on_success", {"text": "{result}"})
        if isinstance(success_template, dict):
            context.get("predicates", {})["_graph_result"] = result_str
            if "text" in success_template:
                output = self.substitute_variables(str(success_template.get("text", "")), context, {"result": result_str})
                return output
            output = self.process(success_template, context)
            return output
        return result_str

    def process_triple_query(self, triple_data: dict, context: dict) -> str:
        """Process triple query shorthand operation."""
        graph = context.get("graph_fn", ())
        if not graph:
            result = ""
            return result

        authored_subject = triple_data.get("subject", "")
        authored_object = triple_data.get("object", "")
        predicate = self.substitute_variables(triple_data.get("predicate", ""), context)

        # The authored "?" marks the unknown slot and fixes the query direction;
        # a rendered capture cannot change it. The predicate resolves to a
        # canonical Predicate node, the known slot to a canonical Entity, and
        # the unknown slot is read from the edge surface form.
        if authored_object == "?":
            query = TRIPLE_QUERY_OBJECT
            params = {"subject": self.substitute_variables(authored_subject, context), "predicate": predicate}
        elif authored_subject == "?":
            query = TRIPLE_QUERY_SUBJECT
            params = {"object": self.substitute_variables(authored_object, context), "predicate": predicate}
        else:
            result = ""
            return result

        evaluation_time = projection_timestamp(context.get("evaluation_time", ""), True, "template evaluation time")
        params["evaluation_time"] = evaluation_time

        records = graph(query, params)
        if records and graph_single(records):
            value = str(graph_single(records).get("result", ""))
            return value
        result = ""
        return result

    def substitute_variables(self, text: str, context: dict, bindings=()) -> str:
        """Render authored template text in one pass.

        Tokens are recognized only in the authored text. A value a token
        inserts (a capture, predicate, history entry, graph result or other
        context value) is emitted as literal data and never scanned for further
        tokens, so captured text cannot act as template syntax. A token's
        argument is itself authored text and is rendered first, which keeps
        intentional nesting such as ``{upper:{star1}}``. ``bindings`` maps token
        names such as ``result`` to literal values for this rendering. A
        backslash before ``{``, ``}`` or another backslash emits that character.
        """
        values = bindings or {}
        output = []
        position = 0
        while position < len(text):
            character = text[position]
            following = text[position + 1 : position + 2]
            if character == TEMPLATE_ESCAPE_CHARACTER and following and following in TEMPLATE_ESCAPABLE_CHARACTERS:
                output.append(following)
                position += 2
                continue
            end = template_token_end(text, position) if character == "{" else -1
            if end < 0:
                output.append(character)
                position += 1
                continue
            output.append(self.render_token(text[position + 1 : end], context, values))
            position = end + 1
        result = "".join(output)
        return result

    def render_token(self, inner: str, context: dict, bindings: dict) -> str:
        """Return the value of one authored token whose text between braces is ``inner``."""
        if inner in bindings:
            bound = bindings.get(inner, "")
            return bound
        name, separator, argument = inner.partition(":")
        # The transform expression accepts an empty argument, so it identifies transform names.
        if separator and transform_pattern.fullmatch(f"{{{name}:}}"):
            transformed = self.apply_transform(name, self.substitute_variables(argument, context, bindings), context)
            return transformed
        token = "{" + self.substitute_variables(inner, context, bindings) + "}"
        match = star_pattern.fullmatch(token)
        if match:
            value = get_star(context, int(match.group(1)))
            return value
        match = thatstar_pattern.fullmatch(token)
        if match:
            value = get_thatstar(context, int(match.group(1)))
            return value
        match = topicstar_pattern.fullmatch(token)
        if match:
            value = get_topicstar(context, int(match.group(1)))
            return value
        match = get_pattern.fullmatch(token)
        if match:
            value = context.get("predicates", {}).get(match.group(1), match.group(2) or "")
            return value
        match = bot_pattern.fullmatch(token)
        if match:
            value = context.get("bot", {}).get(match.group(1), "")
            return value
        match = map_pattern.fullmatch(token)
        if match:
            value = get_map(context, match.group(1), match.group(2), match.group(3) or "")
            return value
        match = input_pattern.fullmatch(token)
        if match:
            value = get_input(context, int(match.group(1)))
            return value
        match = response_pattern.fullmatch(token)
        if match:
            value = get_response(context, int(match.group(1)) if match.group(1) else 1)
            return value
        match = that_pattern.fullmatch(token)
        if match and match.group(1):
            value = get_that(context, int(match.group(1)), int(match.group(2)) if match.group(2) else 1)
            return value
        if match:
            history = context.get("that_history", [])
            value = history[0][0] if history and history[0] else ""
            return value
        if token in TEMPLATE_SIMPLE_VARIABLE_TOKENS.values():
            value = simple_variable_values(context).get(token, "")
            return value
        match = date_format_pattern.fullmatch(token)
        if match:
            value = format_template_date(match.group(1), token)
            return value
        # Unknown braces stay literal text.
        return token

    def apply_transform(self, fn_name: str, resolved: str, context: dict) -> str:
        """Apply one named text transform to an already rendered value."""
        if fn_name == "upper":
            transformed = resolved.upper()
            return transformed
        elif fn_name == "lower":
            transformed = resolved.lower()
            return transformed
        elif fn_name == "capitalize":
            transformed = resolved.capitalize()
            return transformed
        elif fn_name == "formal":
            transformed = resolved.title()
            return transformed
        elif fn_name == "sentence":
            transformed = resolved.capitalize()
            return transformed
        elif fn_name == "person":
            transformed = apply_word_substitution(resolved, context.get("person_subs", {}))
            return transformed
        elif fn_name == "person2":
            transformed = apply_word_substitution(resolved, context.get("person2_subs", {}))
            return transformed
        elif fn_name == "gender":
            transformed = apply_word_substitution(resolved, context.get("gender_subs", {}))
            return transformed
        elif fn_name == "normalize":
            transformed = resolved.upper()
            return transformed
        elif fn_name == "denormalize":
            return resolved
        elif fn_name == "explode":
            transformed = " ".join(resolved)
            return transformed
        elif fn_name == "first":
            words = resolved.split()
            transformed = words[0] if words else ""
            return transformed
        elif fn_name == "rest":
            words = resolved.split()
            transformed = " ".join(words[1:]) if len(words) > 1 else ""
            return transformed
        elif fn_name == "uniq":
            words = resolved.split()
            seen = set()
            unique = []
            for word in words:
                if word not in seen:
                    seen.add(word)
                    unique.append(word)
            transformed = " ".join(unique)
            return transformed
        elif fn_name == "wordcount":
            transformed = str(len(resolved.split()))
            return transformed
        elif fn_name == "sentiment":
            transformed = sentiment_label(resolved)
            return transformed
        elif fn_name == "clause":
            transformed = first_clause(resolved)
            return transformed
        elif fn_name == "qtype":
            transformed = input_kind(resolved)
            return transformed
        elif fn_name == "name":
            transformed = extract_name(resolved)
            return transformed
        return resolved
