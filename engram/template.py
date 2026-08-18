"""Template processing for ENGRAM.

This module provides template parsing and evaluation for dynamic response generation.
Templates can be simple strings or structured JSON objects with variable substitution,
random selection, conditionals, redirects, and more.

The evaluation context is a plain dict built by ``TemplateContext``.
"""

import random
import re
from datetime import datetime

from engram.constants import (
    TEMPLATE_BOT_EXPRESSION,
    TEMPLATE_DATE_FORMAT_EXPRESSION,
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
from engram.graph import graph_is_empty, graph_single, is_write_cypher
from engram.nlp import input_kind
from engram.sentiment import sentiment_label
from engram.text import extract_name, first_clause

star_pattern = re.compile(TEMPLATE_STAR_EXPRESSION)
thatstar_pattern = re.compile(TEMPLATE_THATSTAR_EXPRESSION)
topicstar_pattern = re.compile(TEMPLATE_TOPICSTAR_EXPRESSION)
get_pattern = re.compile(TEMPLATE_GET_EXPRESSION)
bot_pattern = re.compile(TEMPLATE_BOT_EXPRESSION)
map_pattern = re.compile(TEMPLATE_MAP_EXPRESSION)
input_pattern = re.compile(TEMPLATE_INPUT_EXPRESSION)
response_pattern = re.compile(TEMPLATE_RESPONSE_EXPRESSION)
that_pattern = re.compile(TEMPLATE_THAT_EXPRESSION)
transform_pattern = re.compile(TEMPLATE_TRANSFORM_EXPRESSION)
date_format_pattern = re.compile(TEMPLATE_DATE_FORMAT_EXPRESSION)


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
    # Callbacks (set by processor)
    redirect_fn=(),
    learn_fn=(),
    graph_fn=(),
) -> dict:
    """Build a context dict for template evaluation.

    Contains all data needed to evaluate a template, including wildcard captures,
    session predicates, bot properties, and history.
    """
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
        "redirect_fn": redirect_fn,
        "learn_fn": learn_fn,
        "graph_fn": graph_fn,
    }
    return context


# Helper functions for 1-based index access (used by TemplateProcessor)


def get_star(ctx: dict, index: int) -> str:
    """Get star capture by 1-based index."""
    if 1 <= index <= len(ctx["stars"]):
        star = ctx["stars"][index - 1]
        return star
    result = ""
    return result


def get_thatstar(ctx: dict, index: int) -> str:
    """Get thatstar capture by 1-based index."""
    if 1 <= index <= len(ctx["thatstars"]):
        thatstar = ctx["thatstars"][index - 1]
        return thatstar
    result = ""
    return result


def get_topicstar(ctx: dict, index: int) -> str:
    """Get topicstar capture by 1-based index."""
    if 1 <= index <= len(ctx["topicstars"]):
        topicstar = ctx["topicstars"][index - 1]
        return topicstar
    result = ""
    return result


def get_map(ctx: dict, map_name: str, key: str, default: str = "") -> str:
    """Get value from named map."""
    if map_name in ctx["maps"]:
        value = ctx["maps"][map_name].get(key.lower(), default)
        return value
    return default


def get_input(ctx: dict, index: int = 1) -> str:
    """Get input from history (1-based, 1=most recent)."""
    if 1 <= index <= len(ctx["input_history"]):
        value = ctx["input_history"][index - 1]
        return value
    result = ""
    return result


def get_response(ctx: dict, index: int = 1) -> str:
    """Get response from history (1-based, 1=most recent)."""
    if 1 <= index <= len(ctx["response_history"]):
        value = ctx["response_history"][index - 1]
        return value
    result = ""
    return result


def get_that(ctx: dict, response_idx: int = 1, sentence_idx: int = 1) -> str:
    """Get that by response and sentence index (1-based)."""
    if 1 <= response_idx <= len(ctx["that_history"]):
        sentences = ctx["that_history"][response_idx - 1]
        if 1 <= sentence_idx <= len(sentences):
            sentence = sentences[sentence_idx - 1]
            return sentence
    result = ""
    return result


def simple_variable_values(context: dict) -> dict[str, str]:
    """Collect concrete values for the centralized simple-variable tokens."""

    result = {
        TEMPLATE_SIMPLE_VARIABLE_TOKENS["topic"]: context["predicates"].get("topic", ""),
        TEMPLATE_SIMPLE_VARIABLE_TOKENS["input"]: context["input_text"],
        TEMPLATE_SIMPLE_VARIABLE_TOKENS["request"]: context["request_text"],
        TEMPLATE_SIMPLE_VARIABLE_TOKENS["id"]: context["session_id"],
        TEMPLATE_SIMPLE_VARIABLE_TOKENS["size"]: str(context["category_count"]),
        TEMPLATE_SIMPLE_VARIABLE_TOKENS["vocabulary"]: str(context["vocabulary_count"]),
        TEMPLATE_SIMPLE_VARIABLE_TOKENS["date"]: datetime.now().strftime("%B %d, %Y"),
        TEMPLATE_SIMPLE_VARIABLE_TOKENS["time"]: datetime.now().strftime("%H:%M:%S"),
        TEMPLATE_SIMPLE_VARIABLE_TOKENS["program"]: context["bot"].get("name", "ENGRAM"),
        TEMPLATE_SIMPLE_VARIABLE_TOKENS["version"]: context["bot"].get("version", VERSION),
    }
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
            replacement = substitutions[lower]
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
        self._srai_depth = 0
        self._loop_depth = 0

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
            result = self._substitute_variables(template, context)
            return result

        if isinstance(template, dict):
            result = self._process_dict_template(template, context)
            return result

        if isinstance(template, list):
            # List is treated as sequence
            result = self._process_sequence(template, context)
            return result

        result = str(template)
        return result

    def _process_dict_template(self, template: dict, context: dict) -> str:
        """Process a dictionary template."""

        # Text template
        if "text" in template:
            result = self._substitute_variables(str(template["text"]), context)
            return result

        # Random selection
        if "random" in template:
            result = self._process_random(template["random"], context)
            return result

        # Condition
        if "condition" in template:
            result = self._process_condition(template["condition"], context)
            return result

        # Sequence
        if "sequence" in template:
            result = self._process_sequence(template["sequence"], context)
            return result

        # Redirect (SRAI)
        if "redirect" in template:
            result = self._process_redirect(template["redirect"], context)
            return result

        # Shorthand redirect
        if "sr" in template and template["sr"]:
            # Redirect to first star capture
            if context["stars"]:
                result = self._process_redirect(context["stars"][0], context)
                return result
            result = ""
            return result

        # Think (silent processing)
        if "think" in template:
            self._process_think(template["think"], context)
            result = ""
            return result

        # Set variable
        if "set" in template:
            self._process_set(template["set"], context)
            result = ""
            return result

        # Learn new category
        if "learn" in template:
            self._process_learn(template["learn"], context)
            result = ""
            return result

        # Loop (returns to condition evaluation)
        if "loop" in template and template["loop"]:
            # Loop is handled in condition processing
            result = ""
            return result

        # Graph query
        if "graph_query" in template:
            result = self._process_graph_query(template["graph_query"], context)
            return result

        # Triple query shorthand
        if "triple_query" in template:
            result = self._process_triple_query(template["triple_query"], context)
            return result

        result = ""
        return result

    def _process_random(self, choices: list, context: dict) -> str:
        """Process random selection."""
        if not choices:
            result = ""
            return result
        choice = random.choice(choices)
        result = self.process(choice, context)
        return result

    def _process_condition(self, condition: dict, context: dict) -> str:
        """Process conditional template."""
        # Support both "var" and "name" for variable name
        var_name = condition.get("var", condition.get("name", ""))
        var_value = context["predicates"].get(var_name, "")

        # Existence check
        if "exists" in condition or "missing" in condition:
            if var_value:
                result = self.process(condition.get("exists", ""), context)
                return result
            else:
                result = self.process(condition.get("missing", ""), context)
                return result

        # Pattern match check
        if "pattern" in condition:
            pattern = condition["pattern"]
            if re.match(pattern, var_value):
                result = self.process(condition.get("match", ""), context)
                return result
            else:
                result = self.process(condition.get("nomatch", ""), context)
                return result

        # Support both "cases" and "branches" for value matching
        cases = condition.get("cases", condition.get("branches", []))
        if cases:
            for case in cases:
                if "value" in case:
                    if var_value == case["value"]:
                        # Support both "template" and "then" for the result
                        result_template = case.get("template", case.get("then", ""))
                        result = self.process(result_template, context)
                        # Check for loop (bounded so a predicate that never
                        # changes cannot recurse forever)
                        if isinstance(result_template, dict) and "loop" in result_template and self._loop_depth < self.srai_limit:
                            self._loop_depth += 1
                            try:
                                combined = result + self._process_condition(condition, context)
                            finally:
                                self._loop_depth -= 1
                            return combined
                        return result
                elif "default" in case or "then" in case:
                    # Default case (no value specified)
                    result_template = case.get("default", case.get("then", case.get("template", "")))
                    result = self.process(result_template, context)
                    # Check for loop in default (same bound as above)
                    if isinstance(result_template, dict) and "loop" in result_template and self._loop_depth < self.srai_limit:
                        self._loop_depth += 1
                        try:
                            combined = result + self._process_condition(condition, context)
                        finally:
                            self._loop_depth -= 1
                        return combined
                    return result

        result = ""
        return result

    def _process_sequence(self, sequence: list, context: dict) -> str:
        """Process sequence of templates, returning last text output."""
        output_parts = []
        for item in sequence:
            result = self.process(item, context)
            if result:
                output_parts.append(result)
        # Return all non-empty outputs joined
        output = " ".join(output_parts) if output_parts else ""
        return output

    def _process_redirect(self, pattern: str, context: dict) -> str:
        """Process redirect (SRAI)."""
        if self._srai_depth >= self.srai_limit:
            result = ""
            return result

        # Substitute variables in the redirect pattern
        resolved_pattern = self._substitute_variables(pattern, context)

        # Call redirect function if available
        if context["redirect_fn"]:
            self._srai_depth += 1
            try:
                response = context["redirect_fn"](resolved_pattern)
                return response
            finally:
                self._srai_depth -= 1

        result = ""
        return result

    def _process_think(self, items: list, context: dict) -> None:
        """Process think elements (silent, no output)."""
        for item in items:
            self.process(item, context)

    def _process_set(self, set_data: dict, context: dict) -> None:
        """Process set variable."""
        name = set_data.get("name", "")
        value = set_data.get("value", "")
        if name:
            resolved_value = self._substitute_variables(value, context)
            context["predicates"][name] = resolved_value

    def _process_learn(self, learn_data: dict, context: dict) -> None:
        """Process learn element."""
        if context["learn_fn"]:
            # Resolve variables in learn data
            resolved = {}
            if "pattern" in learn_data:
                resolved["pattern"] = self._substitute_variables(learn_data["pattern"], context).upper()
            if "template" in learn_data:
                # Resolve variables inside template
                resolved["template"] = self._resolve_template_vars(learn_data["template"], context)
            if "that" in learn_data:
                resolved["that"] = self._substitute_variables(learn_data["that"], context)
            if "topic" in learn_data:
                resolved["topic"] = self._substitute_variables(learn_data["topic"], context)
            resolved["tier"] = learn_data.get("tier", "DYNAMIC")

            context["learn_fn"](resolved)

    def _resolve_template_vars(self, template, context: dict):
        """Recursively resolve variables in a template structure."""
        if isinstance(template, str):
            resolved = self._substitute_variables(template, context)
            return resolved
        elif isinstance(template, dict):
            resolved = {k: self._resolve_template_vars(v, context) for k, v in template.items()}
            return resolved
        elif isinstance(template, list):
            resolved = [self._resolve_template_vars(item, context) for item in template]
            return resolved
        return template

    def _process_graph_query(self, query_data: dict, context: dict) -> str:
        """Process graph query operation."""
        if not context["graph_fn"]:
            output = self.process(query_data.get("on_failure", ""), context)
            return output

        # Resolve query and parameters
        query = self._substitute_variables(query_data.get("query", ""), context)
        if is_write_cypher(query):
            output = self.process(query_data.get("on_failure", ""), context)
            return output
        params = {}
        for key, value in query_data.get("params", {}).items():
            params[key] = self._substitute_variables(str(value), context)

        # Execute query. The graph layer raises on a query-level failure and
        # degrades to an empty list when unreachable; either way recall falls
        # through to on_empty / on_failure rather than surfacing an error.
        try:
            records = context["graph_fn"](query, params)
        except Exception:
            records = []
        if not records:
            records = []

        if graph_is_empty(records):
            output = self.process(query_data.get("on_empty", query_data.get("on_failure", "")), context)
            return output

        # Format results
        format_type = query_data.get("format", "single")

        if format_type == "list":
            # Format multiple results using item_template
            item_template = query_data.get("item_template", "{result}")
            join_str = query_data.get("join", ", ")
            items = []
            for record in records:
                # Add record values to context for substitution
                item_text = item_template
                for key, value in record.items():
                    item_text = item_text.replace(f"{{{key}}}", str(value))
                items.append(item_text)
            result_str = join_str.join(items)
        else:
            # Single result - use first record
            record = graph_single(records) or {}
            result_str = str(record.get("result", ""))

        # Substitute {result} in success template
        success_template = query_data.get("on_success", {"text": "{result}"})
        if isinstance(success_template, dict):
            # Inject result into context for template processing
            context["predicates"]["_graph_result"] = result_str
            # Process with result placeholder replaced
            template_copy = success_template.copy()
            if "text" in template_copy:
                template_copy["text"] = template_copy["text"].replace("{result}", result_str)
            output = self.process(template_copy, context)
            return output
        return result_str

    def _process_triple_query(self, triple_data: dict, context: dict) -> str:
        """Process triple query shorthand operation."""
        if not context["graph_fn"]:
            result = ""
            return result

        subject = self._substitute_variables(triple_data.get("subject", ""), context)
        predicate = triple_data.get("predicate", "")
        obj = triple_data.get("object", "")

        # Determine query direction based on which slot is "?". The predicate
        # resolves to a canonical Predicate node; the known slot resolves to a
        # canonical Entity; the unknown slot is read from the edge surface form.
        if obj == "?":
            query = TRIPLE_QUERY_OBJECT
            params = {"subject": subject, "predicate": predicate}
        elif subject == "?":
            query = TRIPLE_QUERY_SUBJECT
            params = {"object": obj, "predicate": predicate}
        else:
            result = ""
            return result

        try:
            records = context["graph_fn"](query, params)
        except Exception:
            records = []
        if records and graph_single(records):
            value = str(graph_single(records).get("result", ""))
            return value
        result = ""
        return result

    def _substitute_variables(self, text: str, context: dict) -> str:
        """Substitute all variables in text."""
        result = text

        # Star captures: {star1}, {star2}, etc.
        result = star_pattern.sub(lambda m: get_star(context, int(m.group(1))), result)

        # Thatstar captures: {thatstar1}, etc.
        result = thatstar_pattern.sub(lambda m: get_thatstar(context, int(m.group(1))), result)

        # Topicstar captures: {topicstar1}, etc.
        result = topicstar_pattern.sub(lambda m: get_topicstar(context, int(m.group(1))), result)

        # Get predicates: {get:name} or {get:name:default}
        result = get_pattern.sub(lambda m: context["predicates"].get(m.group(1), m.group(2) or ""), result)

        # Bot properties: {bot:name}
        result = bot_pattern.sub(lambda m: context["bot"].get(m.group(1), ""), result)

        # Map lookups: {map:name:key} or {map:name:key:default}
        result = map_pattern.sub(lambda m: get_map(context, m.group(1), m.group(2), m.group(3) or ""), result)

        # Input history: {input:N} (bare {input} is the current input, below)
        result = input_pattern.sub(lambda m: get_input(context, int(m.group(1))), result)

        # Response history: {response} or {response:N}
        result = response_pattern.sub(lambda m: get_response(context, int(m.group(1)) if m.group(1) else 1), result)

        # That history: {that} or {that:M} or {that:M:N}
        def that_sub(m):
            if not m.group(1):
                # Get most recent bot response
                if context["that_history"] and context["that_history"][0]:
                    result = context["that_history"][0][0]
                    return result
                result = ""
                return result
            resp_idx = int(m.group(1))
            sent_idx = int(m.group(2)) if m.group(2) else 1
            that_value = get_that(context, resp_idx, sent_idx)
            return that_value

        result = that_pattern.sub(that_sub, result)

        # Simple variables
        for pattern, value in simple_variable_values(context).items():
            if pattern in result:
                result = result.replace(pattern, value)

        # Formatted date: {date:format}
        def date_format_sub(m):
            fmt = m.group(1)
            try:
                formatted = datetime.now().strftime(fmt)
                return formatted
            except ValueError:
                original = m.group(0)  # Return original if invalid format
                return original

        result = date_format_pattern.sub(date_format_sub, result)

        # Text transforms: {upper:...}, {lower:...}, etc.
        result = self._apply_transforms(result, context)

        return result

    def _apply_transforms(self, text: str, context: dict) -> str:
        """Apply text transformation functions."""

        def transform(m):
            fn_name = m.group(1)
            content = m.group(2)

            # Recursively substitute variables in content
            resolved = self._substitute_variables(content, context)

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
                transformed = apply_word_substitution(resolved, context["person_subs"])
                return transformed
            elif fn_name == "person2":
                transformed = apply_word_substitution(resolved, context["person2_subs"])
                return transformed
            elif fn_name == "gender":
                transformed = apply_word_substitution(resolved, context["gender_subs"])
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

        # Keep applying until no more transforms
        while True:
            transformed_text = transform_pattern.sub(transform, text)
            if transformed_text == text:
                break
            text = transformed_text

        return text


def parse_template(data):
    """Parse template from JSON/dict representation.

    This is a pass-through for now since templates are already in dict form.
    Future versions may add validation.

    Args:
        data: Template data (string or dict).

    Returns:
        Template object (currently same as input).
    """
    return data


def process_template(
    template,
    context: dict,
    srai_limit: int = 100,
) -> str:
    """Process a template with the given context.

    Convenience function that creates a processor and evaluates.

    Args:
        template: Template to process.
        context: Evaluation context.
        srai_limit: Maximum redirect depth.

    Returns:
        Processed output string.
    """
    processor = TemplateProcessor(srai_limit=srai_limit)
    result = processor.process(template, context)
    return result
