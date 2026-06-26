"""Template processing for ENGRAM.

This module provides template parsing and evaluation for dynamic response generation.
Templates can be simple strings or structured JSON objects with variable substitution,
random selection, conditionals, redirects, and more.

The evaluation context is a plain dict built by ``TemplateContext``.
"""

import random
import re
from datetime import datetime

from engram.graph import graph_is_empty, graph_single
from engram.sentiment import sentiment_label


def template_context(
    # Wildcard captures from pattern matching
    stars=None,
    thatstars=None,
    topicstars=None,
    # Session state
    predicates=None,
    input_history=None,
    response_history=None,
    that_history=None,
    # Current input
    input_text: str = "",
    request_text: str = "",
    # Bot properties
    bot=None,
    # Maps for lookups
    maps=None,
    # Substitution maps
    person_subs=None,
    person2_subs=None,
    gender_subs=None,
    # System info
    session_id: str = "",
    category_count: int = 0,
    vocabulary_count: int = 0,
    # Callbacks (set by processor)
    redirect_fn=None,
    learn_fn=None,
    graph_fn=None,
) -> dict:
    """Build a context dict for template evaluation.

    Contains all data needed to evaluate a template, including wildcard captures,
    session predicates, bot properties, and history.
    """
    context = {
        "stars": stars if stars is not None else [],
        "thatstars": thatstars if thatstars is not None else [],
        "topicstars": topicstars if topicstars is not None else [],
        "predicates": predicates if predicates is not None else {},
        "input_history": input_history if input_history is not None else [],
        "response_history": response_history if response_history is not None else [],
        "that_history": that_history if that_history is not None else [],
        "input_text": input_text,
        "request_text": request_text,
        "bot": bot if bot is not None else {},
        "maps": maps if maps is not None else {},
        "person_subs": person_subs if person_subs is not None else {},
        "person2_subs": person2_subs if person2_subs is not None else {},
        "gender_subs": gender_subs if gender_subs is not None else {},
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
    return ""


def get_thatstar(ctx: dict, index: int) -> str:
    """Get thatstar capture by 1-based index."""
    if 1 <= index <= len(ctx["thatstars"]):
        thatstar = ctx["thatstars"][index - 1]
        return thatstar
    return ""


def get_topicstar(ctx: dict, index: int) -> str:
    """Get topicstar capture by 1-based index."""
    if 1 <= index <= len(ctx["topicstars"]):
        topicstar = ctx["topicstars"][index - 1]
        return topicstar
    return ""


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
    return ""


def get_response(ctx: dict, index: int = 1) -> str:
    """Get response from history (1-based, 1=most recent)."""
    if 1 <= index <= len(ctx["response_history"]):
        value = ctx["response_history"][index - 1]
        return value
    return ""


def get_that(ctx: dict, response_idx: int = 1, sentence_idx: int = 1) -> str:
    """Get that by response and sentence index (1-based)."""
    if 1 <= response_idx <= len(ctx["that_history"]):
        sentences = ctx["that_history"][response_idx - 1]
        if 1 <= sentence_idx <= len(sentences):
            sentence = sentences[sentence_idx - 1]
            return sentence
    return ""


class TemplateProcessor:
    """Processes templates and substitutes variables."""

    # Regex patterns for variable substitution
    STAR_PATTERN = re.compile(r"\{star(\d+)\}")
    THATSTAR_PATTERN = re.compile(r"\{thatstar(\d+)\}")
    TOPICSTAR_PATTERN = re.compile(r"\{topicstar(\d+)\}")
    GET_PATTERN = re.compile(r"\{get:([^:}]+)(?::([^}]*))?\}")
    BOT_PATTERN = re.compile(r"\{bot:([^}]+)\}")
    MAP_PATTERN = re.compile(r"\{map:([^:}]+):([^:}]+)(?::([^}]*))?\}")
    INPUT_PATTERN = re.compile(r"\{input(?::(\d+))?\}")
    RESPONSE_PATTERN = re.compile(r"\{response(?::(\d+))?\}")
    THAT_PATTERN = re.compile(r"\{that(?::(\d+)(?::(\d+))?)?\}")
    # Match transforms with content that doesn't contain braces - processes innermost first
    TRANSFORM_PATTERN = re.compile(
        r"\{(upper|lower|capitalize|formal|sentence|person|person2|gender|normalize|denormalize|explode|first|rest|uniq|wordcount|sentiment):([^{}]*)\}"
    )

    # Simple variable patterns
    SIMPLE_VARS = {
        "{topic}": lambda ctx: ctx["predicates"].get("topic", ""),
        "{input}": lambda ctx: ctx["input_text"],
        "{request}": lambda ctx: ctx["request_text"],
        "{id}": lambda ctx: ctx["session_id"],
        "{size}": lambda ctx: str(ctx["category_count"]),
        "{vocabulary}": lambda ctx: str(ctx["vocabulary_count"]),
        "{date}": lambda ctx: datetime.now().strftime("%B %d, %Y"),
        "{time}": lambda ctx: datetime.now().strftime("%H:%M:%S"),
        "{program}": lambda ctx: ctx["bot"].get("name", "ENGRAM"),
        "{version}": lambda ctx: ctx["bot"].get("version", "0.1.5"),
    }

    # Pattern for formatted date: {date:format}
    DATE_FORMAT_PATTERN = re.compile(r"\{date:([^}]+)\}")

    def __init__(self, srai_limit: int = 100):
        """Initialize processor.

        Args:
            srai_limit: Maximum redirect recursion depth.
        """
        self.srai_limit = srai_limit
        self._srai_depth = 0

    def process(self, template, context: dict) -> str:
        """Process a template and return the output string.

        Args:
            template: Template to process (string or dict).
            context: Evaluation context.

        Returns:
            Processed output string.
        """
        if template is None:
            return ""

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
            return ""

        # Think (silent processing)
        if "think" in template:
            self._process_think(template["think"], context)
            return ""

        # Set variable
        if "set" in template:
            self._process_set(template["set"], context)
            return ""

        # Learn new category
        if "learn" in template:
            self._process_learn(template["learn"], context)
            return ""

        # Loop (returns to condition evaluation)
        if "loop" in template and template["loop"]:
            # Loop is handled in condition processing
            return ""

        # Graph query
        if "graph_query" in template:
            result = self._process_graph_query(template["graph_query"], context)
            return result

        # Graph write
        if "graph_write" in template:
            result = self._process_graph_write(template["graph_write"], context)
            return result

        # Graph delete
        if "graph_delete" in template:
            result = self._process_graph_delete(template["graph_delete"], context)
            return result

        # Triple add shorthand
        if "triple_add" in template:
            result = self._process_triple_add(template["triple_add"], context)
            return result

        # Triple query shorthand
        if "triple_query" in template:
            result = self._process_triple_query(template["triple_query"], context)
            return result

        return ""

    def _process_random(self, choices: list, context: dict) -> str:
        """Process random selection."""
        if not choices:
            return ""
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
                        # Check for loop
                        if isinstance(result_template, dict) and "loop" in result_template:
                            combined = result + self._process_condition(condition, context)
                            return combined
                        return result
                elif "default" in case or "then" in case:
                    # Default case (no value specified)
                    result_template = case.get("default", case.get("then", case.get("template", "")))
                    result = self.process(result_template, context)
                    # Check for loop in default
                    if isinstance(result_template, dict) and "loop" in result_template:
                        combined = result + self._process_condition(condition, context)
                        return combined
                    return result

        return ""

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
            return ""

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

        return ""

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
        params = {}
        for key, value in query_data.get("params", {}).items():
            params[key] = self._substitute_variables(str(value), context)

        # Execute query
        result = context["graph_fn"](query, params)

        if result is None or not result["success"]:
            output = self.process(query_data.get("on_failure", ""), context)
            return output

        if graph_is_empty(result):
            output = self.process(query_data.get("on_empty", query_data.get("on_failure", "")), context)
            return output

        # Format results
        format_type = query_data.get("format", "single")

        if format_type == "list":
            # Format multiple results using item_template
            item_template = query_data.get("item_template", "{result}")
            join_str = query_data.get("join", ", ")
            items = []
            for record in result["records"]:
                # Add record values to context for substitution
                item_text = item_template
                for key, value in record.items():
                    item_text = item_text.replace(f"{{{key}}}", str(value))
                items.append(item_text)
            result_str = join_str.join(items)
        else:
            # Single result - use first record
            record = graph_single(result) or {}
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

    def _process_graph_write(self, write_data: dict, context: dict) -> str:
        """Process graph write operation."""
        if not context["graph_fn"]:
            output = self.process(write_data.get("on_failure", ""), context)
            return output

        # Resolve query and parameters
        query = self._substitute_variables(write_data.get("query", ""), context)
        params = {}
        for key, value in write_data.get("params", {}).items():
            params[key] = self._substitute_variables(str(value), context)

        # Execute write
        result = context["graph_fn"](query, params)

        if result is None or not result["success"]:
            output = self.process(write_data.get("on_failure", ""), context)
            return output

        output = self.process(write_data.get("on_success", ""), context)
        return output

    def _process_graph_delete(self, delete_data: dict, context: dict) -> str:
        """Process graph delete operation."""
        if not context["graph_fn"]:
            output = self.process(delete_data.get("on_failure", ""), context)
            return output

        # Resolve query and parameters
        query = self._substitute_variables(delete_data.get("query", ""), context)
        params = {}
        for key, value in delete_data.get("params", {}).items():
            params[key] = self._substitute_variables(str(value), context)

        # Execute delete
        result = context["graph_fn"](query, params)

        if result is None or not result["success"]:
            output = self.process(delete_data.get("on_failure", ""), context)
            return output

        output = self.process(delete_data.get("on_success", ""), context)
        return output

    def _process_triple_add(self, triple_data: dict, context: dict) -> str:
        """Process triple add shorthand operation."""
        if not context["graph_fn"]:
            return ""

        # Resolve triple components
        subject = self._substitute_variables(triple_data.get("subject", ""), context)
        predicate = self._substitute_variables(triple_data.get("predicate", ""), context)
        obj = self._substitute_variables(triple_data.get("object", ""), context)

        if not subject or not predicate or not obj:
            return ""

        # Build standard triple query
        query = """
            MERGE (s:Entity {name: $subject})
            MERGE (o:Entity {name: $object})
            MERGE (s)-[:$predicate]->(o)
        """
        params = {"subject": subject, "predicate": predicate.upper(), "object": obj}

        context["graph_fn"](query, params)
        return ""

    def _process_triple_query(self, triple_data: dict, context: dict) -> str:
        """Process triple query shorthand operation."""
        if not context["graph_fn"]:
            return ""

        subject = self._substitute_variables(triple_data.get("subject", ""), context)
        predicate = triple_data.get("predicate", "")
        obj = triple_data.get("object", "")

        # Determine query direction based on which is "?"
        if obj == "?":
            # Query for object: (subject)-[predicate]->(?)
            query = """
                MATCH (s:Entity {name: $subject})-[:$predicate]->(o:Entity)
                RETURN o.name as result
            """
            params = {"subject": subject, "predicate": predicate}
        elif subject == "?":
            # Query for subject: (?)-[predicate]->(object)
            query = """
                MATCH (s:Entity)-[:$predicate]->(o:Entity {name: $object})
                RETURN s.name as result
            """
            params = {"predicate": predicate, "object": obj}
        else:
            return ""

        result = context["graph_fn"](query, params)
        if result and result["success"] and graph_single(result):
            value = str(graph_single(result).get("result", ""))
            return value
        return ""

    def _substitute_variables(self, text: str, context: dict) -> str:
        """Substitute all variables in text."""
        result = text

        # Star captures: {star1}, {star2}, etc.
        result = self.STAR_PATTERN.sub(lambda m: get_star(context, int(m.group(1))), result)

        # Thatstar captures: {thatstar1}, etc.
        result = self.THATSTAR_PATTERN.sub(lambda m: get_thatstar(context, int(m.group(1))), result)

        # Topicstar captures: {topicstar1}, etc.
        result = self.TOPICSTAR_PATTERN.sub(lambda m: get_topicstar(context, int(m.group(1))), result)

        # Get predicates: {get:name} or {get:name:default}
        result = self.GET_PATTERN.sub(lambda m: context["predicates"].get(m.group(1), m.group(2) or ""), result)

        # Bot properties: {bot:name}
        result = self.BOT_PATTERN.sub(lambda m: context["bot"].get(m.group(1), ""), result)

        # Map lookups: {map:name:key} or {map:name:key:default}
        result = self.MAP_PATTERN.sub(lambda m: get_map(context, m.group(1), m.group(2), m.group(3) or ""), result)

        # Input history: {input} or {input:N}
        result = self.INPUT_PATTERN.sub(lambda m: get_input(context, int(m.group(1)) if m.group(1) else 1), result)

        # Response history: {response} or {response:N}
        result = self.RESPONSE_PATTERN.sub(lambda m: get_response(context, int(m.group(1)) if m.group(1) else 1), result)

        # That history: {that} or {that:M} or {that:M:N}
        def that_sub(m):
            if m.group(1) is None:
                # Get most recent bot response
                if context["that_history"] and context["that_history"][0]:
                    return context["that_history"][0][0]
                return ""
            resp_idx = int(m.group(1))
            sent_idx = int(m.group(2)) if m.group(2) else 1
            that_value = get_that(context, resp_idx, sent_idx)
            return that_value

        result = self.THAT_PATTERN.sub(that_sub, result)

        # Simple variables
        for pattern, fn in self.SIMPLE_VARS.items():
            if pattern in result:
                result = result.replace(pattern, fn(context))

        # Formatted date: {date:format}
        def date_format_sub(m):
            fmt = m.group(1)
            try:
                formatted = datetime.now().strftime(fmt)
                return formatted
            except ValueError:
                original = m.group(0)  # Return original if invalid format
                return original

        result = self.DATE_FORMAT_PATTERN.sub(date_format_sub, result)

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
                transformed = self._apply_substitution(resolved, context["person_subs"])
                return transformed
            elif fn_name == "person2":
                transformed = self._apply_substitution(resolved, context["person2_subs"])
                return transformed
            elif fn_name == "gender":
                transformed = self._apply_substitution(resolved, context["gender_subs"])
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
            return resolved

        # Keep applying until no more transforms
        prev = None
        while prev != text:
            prev = text
            text = self.TRANSFORM_PATTERN.sub(transform, text)

        return text

    def _apply_substitution(self, text: str, subs: dict[str, str]) -> str:
        """Apply word-by-word substitution."""
        if not subs:
            return text

        words = text.split()
        result = []
        for word in words:
            lower = word.lower()
            if lower in subs:
                # Preserve case
                replacement = subs[lower]
                if word.isupper():
                    replacement = replacement.upper()
                elif word[0].isupper():
                    replacement = replacement.capitalize()
                result.append(replacement)
            else:
                result.append(word)
        joined = " ".join(result)
        return joined


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
