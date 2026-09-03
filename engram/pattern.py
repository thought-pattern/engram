"""AIML-style pattern matching for ENGRAM.

Supports wildcards:
    * - matches one or more words (lower priority)
    _ - matches one or more words (higher priority)
    # - matches zero or more words (lower priority)
    ^ - matches zero or more words (higher priority)

Pattern priority (highest to lowest):
    1. topic + that + pattern (most specific context)
    2. that + pattern
    3. topic + pattern
    4. pattern only
    5. ^ wildcard (0+ words, high priority)
    6. _ wildcard (1+ words, high priority)
    7. Exact word match
    8. # wildcard (0+ words, low priority)
    9. * wildcard (1+ words, low priority)

More specific patterns (fewer wildcards, more exact words) take precedence.
Context matching (topic/that) adds priority multipliers.

Stemming support:
    When enabled, patterns and input are stemmed before matching, allowing
    "running" to match "run", "cats" to match "cat", etc.
"""

from functools import lru_cache
from re import (
    IGNORECASE as IGNORECASE,
    Match as re_Match,
    Pattern as re_Pattern,
    compile as re_compile,
    escape as re_escape,
    sub as re_sub,
)

from engram.constants import THAT_PRIORITY, TOPIC_PRIORITY, WILDCARD_TOKENS
from engram.text import lemmatize_text, lemmatize_text_spacy, normalize, stem_text


def match_result(
    matched: bool,
    pattern: str,
    score: int,  # Higher = more specific match
    captured: list[str],  # Text captured by wildcards
    thatstars=(),  # Captures from that pattern
    topicstars=(),  # Captures from topic pattern
) -> dict:
    """Build a pattern-match result dict."""
    result = {
        "matched": matched,
        "pattern": pattern,
        "score": score,
        "captured": captured,
        "thatstars": list(thatstars or ()),
        "topicstars": list(topicstars or ()),
    }
    return result


@lru_cache(maxsize=2048)
def normalize_pattern(pattern: str) -> str:
    """Normalize pattern while preserving wildcards, set references, and $ prefix.

    Args:
        pattern: AIML pattern string.

    Returns:
        Normalized pattern with wildcards, {set:...}, and $prefix preserved.
    """
    # Preserve {set:name} and {bot:name} tokens BEFORE lowercasing

    set_pattern = re_compile(r"\{set:(\w+)\}", IGNORECASE)
    bot_pattern = re_compile(r"\{bot:(\w+)\}", IGNORECASE)

    # Store set/bot references and replace with placeholders
    set_refs: list[str] = []
    bot_refs: list[str] = []

    def save_set(m: re_Match) -> str:
        set_refs.append(m.group(1).lower())
        placeholder = f"\x05{len(set_refs) - 1}\x05"
        return placeholder

    def save_bot(m: re_Match) -> str:
        bot_refs.append(m.group(1).lower())
        placeholder = f"\x06{len(bot_refs) - 1}\x06"
        return placeholder

    result = set_pattern.sub(save_set, pattern)
    result = bot_pattern.sub(save_bot, result)

    result = result.lower()

    # Replace wildcards with placeholders (use control chars that won't appear in text)
    result = result.replace("*", "\x01").replace("_", "\x02")
    result = result.replace("#", "\x03").replace("^", "\x04")

    # Preserve $ prefix by replacing $word with placeholder
    # \x07 is placeholder for $
    result = re_sub(r"\$(\w)", lambda m: "\x07" + m.group(1), result)

    # Remove punctuation (but keep placeholders and digits for index)
    result = "".join(c for c in result if c.isalnum() or c.isspace() or c in "\x01\x02\x03\x04\x05\x06\x07")

    result = result.replace("\x01", "*").replace("\x02", "_")
    result = result.replace("\x03", "#").replace("\x04", "^")

    result = result.replace("\x07", "$")

    for i, name in enumerate(set_refs):
        result = result.replace(f"\x05{i}\x05", f"{{set:{name}}}")
    for i, name in enumerate(bot_refs):
        result = result.replace(f"\x06{i}\x06", f"{{bot:{name}}}")

    result = " ".join(result.split())
    return result


def pattern_to_regex(
    pattern: str,
    sets=(),
    bot_properties=(),
) -> tuple[re_Pattern, int]:
    """Convert AIML-style pattern to regex.

    Args:
        pattern: AIML pattern with *, _, #, ^ wildcards.
        sets: Optional dictionary of named word sets for {set:name} matching.
        bot_properties: Optional bot properties for {bot:name} matching.

    Returns:
        Tuple of (compiled regex, specificity score).
    """
    normalized = normalize_pattern(pattern)
    words = normalized.split()

    if not words:
        empty_regex = re_compile(r"^$")
        empty_result = (empty_regex, 0)
        return empty_result

    regex_parts = []
    specificity = 0
    can_be_empty = []

    set_ref_pattern = re_compile(r"^\{set:(\w+)\}$")
    bot_ref_pattern = re_compile(r"^\{bot:(\w+)\}$")

    for word in words:
        if word == "^":
            # ^ matches zero or more words, high priority
            # Wildcards subtract from specificity so exact matches win
            # ^ has smallest penalty (highest wildcard priority)
            regex_parts.append(r"(.*?)")
            can_be_empty.append(True)
            specificity -= 1  # Smallest penalty (highest priority wildcard)
        elif word == "_":
            # _ matches one or more words, high priority
            regex_parts.append(r"(.+?)")
            can_be_empty.append(False)
            specificity -= 2  # Small penalty (high priority wildcard)
        elif word == "#":
            # # matches zero or more words, low priority
            regex_parts.append(r"(.*?)")
            can_be_empty.append(True)
            specificity -= 3  # Larger penalty (low priority wildcard)
        elif word == "*":
            # * matches one or more words, lowest priority
            regex_parts.append(r"(.+?)")
            can_be_empty.append(False)
            specificity -= 4  # Largest penalty (lowest priority wildcard)
        elif set_match := set_ref_pattern.match(word):
            # {set:name} - match any word from the named set
            set_name = set_match.group(1)
            if sets and set_name in sets and sets[set_name]:
                set_words = [re_escape(w.lower()) for w in sets[set_name]]
                regex_parts.append(f"({('|'.join(set_words))})")
            else:
                # Unknown set - match nothing (use impossible pattern)
                regex_parts.append(r"(?!.)")
            can_be_empty.append(False)
            specificity += 90  # High but less than exact word match
        elif bot_match := bot_ref_pattern.match(word):
            # {bot:name} - match the bot property value
            prop_name = bot_match.group(1)
            if bot_properties and prop_name in bot_properties:
                prop_value = bot_properties[prop_name].lower()
                regex_parts.append(f"({re_escape(prop_value)})")
            else:
                # Unknown property - match nothing
                regex_parts.append(r"(?!.)")
            can_be_empty.append(False)
            specificity += 90  # High but less than exact word match
        elif word.startswith("$"):
            # Priority word - exact match with highest priority
            actual_word = word[1:]
            regex_parts.append(re_escape(actual_word))
            can_be_empty.append(False)
            specificity += 1000  # Highest priority for $ words
        else:
            # Exact word match
            regex_parts.append(re_escape(word))
            can_be_empty.append(False)
            specificity += 100  # High specificity for exact matches

    # Join with flexible spacing: parts that can be empty (# and ^) get
    # optional surrounding whitespace, everything else requires a separator.
    regex_str = r"^\s*"
    for i, part in enumerate(regex_parts):
        if i > 0:
            if can_be_empty[i] or can_be_empty[i - 1]:
                regex_str += r"\s*"
            else:
                regex_str += r"\s+"
        regex_str += part
    regex_str += r"\s*$"

    compiled = re_compile(regex_str, IGNORECASE)
    result = (compiled, specificity)
    return result


def match_pattern(pattern: str, text: str) -> dict:
    """Match text against an AIML-style pattern.

    Args:
        pattern: AIML pattern (e.g., "HELLO *", "WHAT IS YOUR *")
        text: User input text.

    Returns:
        MatchResult indicating if matched and captured groups.
    """
    regex, specificity = pattern_to_regex(pattern)
    normalized_text = normalize(text)

    match = regex.match(normalized_text)

    if match:
        captured = list(match.groups())
        matched_result = match_result(
            matched=True,
            pattern=pattern,
            score=specificity,
            captured=captured,
        )
        return matched_result

    unmatched_result = match_result(
        matched=False,
        pattern=pattern,
        score=0,
        captured=[],
    )
    return unmatched_result


def find_best_match(patterns: list[tuple[str, str]], text: str) -> tuple:
    """Find the best matching pattern for input text.

    Args:
        patterns: List of (pattern, response) tuples.
        text: User input text.

    Returns:
        Tuple of (pattern, response, captured), or an empty tuple if no match.
    """
    best_match: tuple = ()

    for pattern, response in patterns:
        result = match_pattern(pattern, text)

        if result["matched"] and (not best_match or result["score"] > best_match[3]):
            best_match = (pattern, response, result["captured"], result["score"])

    if best_match:
        best = (best_match[0], best_match[1], best_match[2])
        return best

    result = ()
    return result


def pattern_entry(
    pattern: str,
    response: str,
    regex,
    specificity: int,
    that: str = "",  # Pattern for bot's previous response
    that_regex=(),
    that_specificity: int = 0,
    topic: str = "",  # Topic scope (exact match or pattern)
    topic_regex=(),
    topic_specificity: int = 0,
) -> dict:
    """Build a pattern entry dict with optional context constraints."""
    entry = {
        "pattern": pattern,
        "response": response,
        "regex": regex,
        "specificity": specificity,
        "that": that,
        "that_regex": that_regex,
        "that_specificity": that_specificity,
        "topic": topic,
        "topic_regex": topic_regex,
        "topic_specificity": topic_specificity,
    }
    return entry


def is_pure_wildcard(pattern: str) -> bool:
    """Return True if a pattern is only wildcard tokens (e.g. '*' or '* *')."""
    words = pattern.split()
    is_wildcard = bool(words) and all(word.lstrip("$") in WILDCARD_TOKENS for word in words)
    return is_wildcard


def specific_pattern_result(result: tuple) -> tuple:
    """Discard a pure-wildcard match while retaining any specific match."""
    if result and is_pure_wildcard(result[4]):
        result = ()
        return result
    return result


class PatternMatcher:
    """AIML-style pattern matcher with indexed patterns and context matching."""

    def __init__(
        self,
        sets=(),
        bot_properties=(),
        use_stemming: bool = False,
        use_lemmatization: bool = False,
        use_spacy_lemmatization: bool = False,
    ) -> None:
        """Initialize pattern matcher.

        Args:
            sets: Optional dictionary of named word sets for {set:name} matching.
            bot_properties: Optional bot properties for {bot:name} matching.
            use_stemming: If True, use stemmed matching as fallback when exact match fails.
            use_lemmatization: If True, try lemmatized matching as a fallback
                (more precise than stemming) before stemmed matching.
            use_spacy_lemmatization: If True, lemmatize with spaCy's context-aware
                lemmatizer instead of the WordNet heuristic.
        """
        self.internal_patterns: list[dict] = []
        # Index: first word -> list of pattern indices for faster lookup
        self.first_word_index: dict[str, list[int]] = {}
        # Index for stemmed first words (when stemming enabled)
        self.stemmed_first_word_index: dict[str, list[int]] = {}
        # Index for lemmatized first words (when lemmatization enabled)
        self.lemmatized_first_word_index: dict[str, list[int]] = {}
        self.wildcard_patterns: list[int] = []  # Patterns whose first token can match arbitrary input
        # Preserve caller-owned dict references, including explicitly empty maps.
        self.internal_sets = sets if isinstance(sets, dict) else {}
        self.internal_bot_properties = bot_properties if isinstance(bot_properties, dict) else {}
        self.internal_use_stemming = use_stemming
        self.internal_use_lemmatization = use_lemmatization
        self.internal_lemmatize = lemmatize_text_spacy if use_spacy_lemmatization else lemmatize_text

    def add_pattern(
        self,
        pattern: str,
        response: str,
        that: str = "",
        topic: str = "",
    ) -> None:
        """Add a pattern-response pair with optional context.

        Args:
            pattern: AIML-style pattern.
            response: Response template.
            that: Optional pattern for bot's previous response.
            topic: Optional topic scope.
        """
        regex, specificity = pattern_to_regex(pattern, self.internal_sets, self.internal_bot_properties)

        that_regex = ()
        that_specificity = 0
        if that:
            that_regex, that_specificity = pattern_to_regex(that, self.internal_sets, self.internal_bot_properties)

        topic_regex = ()
        topic_specificity = 0
        if topic:
            topic_regex, topic_specificity = pattern_to_regex(topic, self.internal_sets, self.internal_bot_properties)

        entry = pattern_entry(
            pattern=pattern,
            response=response,
            regex=regex,
            specificity=specificity,
            that=that,
            that_regex=that_regex,
            that_specificity=that_specificity,
            topic=topic,
            topic_regex=topic_regex,
            topic_specificity=topic_specificity,
        )

        idx = len(self.internal_patterns)
        self.internal_patterns.append(entry)
        self.index_pattern(idx, pattern)

    def index_pattern(self, idx: int, pattern: str) -> bool:
        """Index a pattern by its first word for faster candidate lookup.

        Args:
            idx: Index of the pattern entry in _patterns.
            pattern: The AIML-style pattern to index.
        """
        normalized = normalize_pattern(pattern)
        words = normalized.split()
        if not words:
            return False

        first = words[0]
        # Strip $ prefix for indexing ($ is priority operator, not part of the word)
        index_word = first.lstrip("$")
        # Treat wildcards and variable references as "any first word"
        # since they can match multiple possible inputs
        if first in ("*", "_", "#", "^") or first.startswith("{set:") or first.startswith("{bot:"):
            self.wildcard_patterns.append(idx)
            return True

        if index_word not in self.first_word_index:
            self.first_word_index[index_word] = []
        self.first_word_index[index_word].append(idx)

        if self.internal_use_stemming:
            stemmed_first = stem_text(index_word)
            if stemmed_first not in self.stemmed_first_word_index:
                self.stemmed_first_word_index[stemmed_first] = []
            if idx not in self.stemmed_first_word_index[stemmed_first]:
                self.stemmed_first_word_index[stemmed_first].append(idx)

        if self.internal_use_lemmatization:
            lemma_first = self.internal_lemmatize(index_word)
            if lemma_first not in self.lemmatized_first_word_index:
                self.lemmatized_first_word_index[lemma_first] = []
            if idx not in self.lemmatized_first_word_index[lemma_first]:
                self.lemmatized_first_word_index[lemma_first].append(idx)
        return True

    def rebuild_indexes(self) -> None:
        """Rebuild every first-word index from the current pattern list.

        Entry indices shift when a pattern is removed, so all index buckets are
        rebuilt from scratch rather than patched in place.
        """
        self.first_word_index.clear()
        self.stemmed_first_word_index.clear()
        self.lemmatized_first_word_index.clear()
        self.wildcard_patterns.clear()
        for idx, entry in enumerate(self.internal_patterns):
            self.index_pattern(idx, entry["pattern"])

    def remove_pattern(self, pattern: str, that: str = "", topic: str = "") -> bool:
        """Remove the first entry matching (pattern, that, topic).

        Called when the statement backing a pattern is evicted or retired, so a
        dead pattern cannot keep matching (and shadowing live patterns) after
        its statement is gone.

        Args:
            pattern: AIML-style pattern to remove.
            that: The entry's that-context ("" when none).
            topic: The entry's topic scope ("" when none).

        Returns:
            True if an entry was removed, False if no entry matched.
        """
        for i, entry in enumerate(self.internal_patterns):
            if entry["pattern"] == pattern and entry["that"] == that and entry["topic"] == topic:
                del self.internal_patterns[i]
                self.rebuild_indexes()
                result = True
                return result
        result = False
        return result

    def match(
        self,
        text: str,
        that: str = "",
        topic: str = "",
    ) -> tuple:
        """Find best matching response for input with context.

        Args:
            text: User input text.
            that: Bot's previous response (normalized).
            topic: Current topic.

        Returns:
            Tuple of (response, captured, thatstars, topicstars, pattern, topic, that), or ().
        """
        normalized = normalize(text)
        words = normalized.split()

        if not words:
            result = ()
            return result

        that_normalized = normalize(that) if that else ""
        topic_normalized = normalize(topic) if topic else ""

        first_word = words[0]
        result = self.match_internal(normalized, that_normalized, topic_normalized, first_word, index_kind="exact")

        # A pure-wildcard (catch-all) match must not block the flexible
        # fallbacks - set it aside and try for something more specific. The
        # fallback stages ignore pure-wildcard results (via _specific) so they
        # don't simply re-return the catch-all.
        catchall = ()
        if result and is_pure_wildcard(result[4]):
            catchall, result = result, ()

        if not result and self.internal_use_lemmatization:
            lemmatized = self.internal_lemmatize(normalized)
            lemma_first = self.internal_lemmatize(first_word)
            result = specific_pattern_result(
                self.match_internal(
                    lemmatized,
                    that_normalized,
                    topic_normalized,
                    lemma_first,
                    index_kind="lemmatized",
                )
            )

        if not result and self.internal_use_stemming:
            stemmed = stem_text(normalized)
            stemmed_first = stem_text(first_word)
            result = specific_pattern_result(
                self.match_internal(stemmed, that_normalized, topic_normalized, stemmed_first, index_kind="stemmed")
            )

        final = result if result else catchall
        return final

    def get_candidate_indices(
        self,
        first_word: str,
        index_kind: str = "exact",
    ) -> list[int]:
        """Get pattern indices that could match based on first word.

        Args:
            first_word: First word of input text.
            index_kind: Which first-word index to use: "exact", "lemmatized",
                or "stemmed".

        Returns:
            List of pattern indices to check.
        """
        candidates: set[int] = set()

        candidates.update(self.wildcard_patterns)

        if index_kind == "stemmed":
            candidates.update(self.stemmed_first_word_index.get(first_word, []))
        elif index_kind == "lemmatized":
            candidates.update(self.lemmatized_first_word_index.get(first_word, []))
        else:
            candidates.update(self.first_word_index.get(first_word, []))

        ordered = sorted(candidates)
        return ordered

    def match_internal(
        self,
        normalized: str,
        that_normalized: str,
        topic_normalized: str,
        first_word: str,
        index_kind: str = "exact",
    ) -> tuple:
        """Internal matching logic.

        Args:
            normalized: Normalized input text.
            that_normalized: Normalized previous response.
            topic_normalized: Normalized topic.
            first_word: First word of input (for index lookup).
            index_kind: Which first-word index to use: "exact", "lemmatized",
                or "stemmed".

        Returns:
            Tuple of (response, captured, thatstars, topicstars, pattern, topic, that), or ().
        """
        candidate_indices = self.get_candidate_indices(first_word, index_kind)
        best: tuple = ()

        for idx in candidate_indices:
            entry = self.internal_patterns[idx]
            match = entry["regex"].match(normalized)
            if not match:
                continue

            captured = list(match.groups())
            thatstars: list[str] = []
            topicstars: list[str] = []
            score = entry["specificity"]

            if entry["topic"] and entry["topic_regex"]:
                if not topic_normalized:
                    continue
                topic_match = entry["topic_regex"].match(topic_normalized)
                if not topic_match:
                    continue
                score += TOPIC_PRIORITY + entry["topic_specificity"]
                topicstars = list(topic_match.groups())

            if entry["that"] and entry["that_regex"]:
                if not that_normalized:
                    continue
                that_match = entry["that_regex"].match(that_normalized)
                if not that_match:
                    continue
                score += THAT_PRIORITY + entry["that_specificity"]
                thatstars = list(that_match.groups())

            if not best or score > best[4]:
                best = (
                    entry["response"],
                    captured,
                    thatstars,
                    topicstars,
                    score,
                    entry["pattern"],
                    entry["topic"],
                    entry["that"],
                )

        if best:
            # Return (response, captured, thatstars, topicstars, pattern, topic, that)
            best_result = (best[0], best[1], best[2], best[3], best[5], best[6], best[7])
            return best_result

        result = ()
        return result

    def get_patterns(self) -> list[tuple[str, str]]:
        """Get all pattern-response pairs.

        Returns:
            List of (pattern, response) tuples.
        """
        pairs = [(p["pattern"], p["response"]) for p in self.internal_patterns]
        return pairs

    def get_patterns_with_context(self) -> list[tuple[str, str, str, str]]:
        """Get all pattern-response pairs with context.

        Returns:
            List of (pattern, response, that, topic) tuples.
        """
        entries = [(p["pattern"], p["response"], p["that"], p["topic"]) for p in self.internal_patterns]
        return entries

    def clear(self) -> None:
        """Remove all patterns."""
        self.internal_patterns.clear()
        self.first_word_index.clear()
        self.stemmed_first_word_index.clear()
        self.lemmatized_first_word_index.clear()
        self.wildcard_patterns.clear()

    def __len__(self) -> int:
        """Return number of patterns."""
        count = len(self.internal_patterns)
        return count
