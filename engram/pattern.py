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

import re
from functools import lru_cache

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

    set_pattern = re.compile(r"\{set:(\w+)\}", re.IGNORECASE)
    bot_pattern = re.compile(r"\{bot:(\w+)\}", re.IGNORECASE)

    # Store set/bot references and replace with placeholders
    set_refs: list[str] = []
    bot_refs: list[str] = []

    def save_set(m: re.Match) -> str:
        set_refs.append(m.group(1).lower())  # Store lowercase name
        placeholder = f"\x05{len(set_refs) - 1}\x05"
        return placeholder

    def save_bot(m: re.Match) -> str:
        bot_refs.append(m.group(1).lower())  # Store lowercase name
        placeholder = f"\x06{len(bot_refs) - 1}\x06"
        return placeholder

    result = set_pattern.sub(save_set, pattern)
    result = bot_pattern.sub(save_bot, result)

    # Convert to lowercase
    result = result.lower()

    # Replace wildcards with placeholders (use control chars that won't appear in text)
    result = result.replace("*", "\x01").replace("_", "\x02")
    result = result.replace("#", "\x03").replace("^", "\x04")

    # Preserve $ prefix by replacing $word with placeholder
    # \x07 is placeholder for $
    result = re.sub(r"\$(\w)", lambda m: "\x07" + m.group(1), result)

    # Remove punctuation (but keep placeholders and digits for index)
    result = "".join(c for c in result if c.isalnum() or c.isspace() or c in "\x01\x02\x03\x04\x05\x06\x07")

    # Restore wildcards
    result = result.replace("\x01", "*").replace("\x02", "_")
    result = result.replace("\x03", "#").replace("\x04", "^")

    # Restore $ prefix
    result = result.replace("\x07", "$")

    # Restore set/bot references
    for i, name in enumerate(set_refs):
        result = result.replace(f"\x05{i}\x05", f"{{set:{name}}}")
    for i, name in enumerate(bot_refs):
        result = result.replace(f"\x06{i}\x06", f"{{bot:{name}}}")

    # Collapse whitespace
    result = " ".join(result.split())
    return result


def pattern_to_regex(
    pattern: str,
    sets=(),
    bot_properties=(),
) -> tuple[re.Pattern, int]:
    """Convert AIML-style pattern to regex.

    Args:
        pattern: AIML pattern with *, _, #, ^ wildcards.
        sets: Optional dictionary of named word sets for {set:name} matching.
        bot_properties: Optional bot properties for {bot:name} matching.

    Returns:
        Tuple of (compiled regex, specificity score).
    """
    # Normalize the pattern (preserving wildcards and set/bot refs)
    normalized = normalize_pattern(pattern)
    words = normalized.split()

    if not words:
        # Empty pattern matches nothing
        empty_regex = re.compile(r"^$")
        empty_result = (empty_regex, 0)
        return empty_result

    regex_parts = []
    specificity = 0
    # Track which parts can be empty (for # and ^)
    can_be_empty = []

    # Regex to detect {set:name} and {bot:name}
    set_ref_pattern = re.compile(r"^\{set:(\w+)\}$")
    bot_ref_pattern = re.compile(r"^\{bot:(\w+)\}$")

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
                # Create alternation of all words in the set (case-insensitive)
                set_words = [re.escape(w.lower()) for w in sets[set_name]]
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
                regex_parts.append(f"({re.escape(prop_value)})")
            else:
                # Unknown property - match nothing
                regex_parts.append(r"(?!.)")
            can_be_empty.append(False)
            specificity += 90  # High but less than exact word match
        elif word.startswith("$"):
            # Priority word - exact match with highest priority
            actual_word = word[1:]  # Strip $ prefix
            regex_parts.append(re.escape(actual_word))
            can_be_empty.append(False)
            specificity += 1000  # Highest priority for $ words
        else:
            # Exact word match
            regex_parts.append(re.escape(word))
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

    compiled = re.compile(regex_str, re.IGNORECASE)
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

    return ()


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
        self._patterns: list[dict] = []
        # Index: first word -> list of pattern indices for faster lookup
        self._first_word_index: dict[str, list[int]] = {}
        # Index for stemmed first words (when stemming enabled)
        self._stemmed_first_word_index: dict[str, list[int]] = {}
        # Index for lemmatized first words (when lemmatization enabled)
        self._lemmatized_first_word_index: dict[str, list[int]] = {}
        self._wildcard_patterns: list[int] = []  # Patterns starting with * or _
        # Preserve caller-owned dict references, including explicitly empty maps.
        self._sets = sets if isinstance(sets, dict) else {}
        self._bot_properties = bot_properties if isinstance(bot_properties, dict) else {}
        self._use_stemming = use_stemming
        self._use_lemmatization = use_lemmatization
        # Select the lemmatizer used for the lemmatized index and fallback.
        self._lemmatize = lemmatize_text_spacy if use_spacy_lemmatization else lemmatize_text

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
        regex, specificity = pattern_to_regex(pattern, self._sets, self._bot_properties)

        # Build that regex if provided
        that_regex = ()
        that_specificity = 0
        if that:
            that_regex, that_specificity = pattern_to_regex(that, self._sets, self._bot_properties)

        # Build topic regex if provided (topics can have wildcards too)
        topic_regex = ()
        topic_specificity = 0
        if topic:
            topic_regex, topic_specificity = pattern_to_regex(topic, self._sets, self._bot_properties)

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

        idx = len(self._patterns)
        self._patterns.append(entry)
        self._index_pattern(idx, pattern)

    def _index_pattern(self, idx: int, pattern: str) -> None:
        """Index a pattern by its first word for faster candidate lookup.

        Args:
            idx: Index of the pattern entry in _patterns.
            pattern: The AIML-style pattern to index.
        """
        normalized = normalize_pattern(pattern)
        words = normalized.split()
        if not words:
            return

        first = words[0]
        # Strip $ prefix for indexing ($ is priority operator, not part of the word)
        index_word = first.lstrip("$")
        # Treat wildcards and variable references as "any first word"
        # since they can match multiple possible inputs
        if first in ("*", "_", "#", "^") or first.startswith("{set:") or first.startswith("{bot:"):
            self._wildcard_patterns.append(idx)
            return

        if index_word not in self._first_word_index:
            self._first_word_index[index_word] = []
        self._first_word_index[index_word].append(idx)

        # Also index by stemmed first word for flexible matching
        if self._use_stemming:
            stemmed_first = stem_text(index_word)
            if stemmed_first not in self._stemmed_first_word_index:
                self._stemmed_first_word_index[stemmed_first] = []
            if idx not in self._stemmed_first_word_index[stemmed_first]:
                self._stemmed_first_word_index[stemmed_first].append(idx)

        # Also index by lemmatized first word for precise flexible matching
        if self._use_lemmatization:
            lemma_first = self._lemmatize(index_word)
            if lemma_first not in self._lemmatized_first_word_index:
                self._lemmatized_first_word_index[lemma_first] = []
            if idx not in self._lemmatized_first_word_index[lemma_first]:
                self._lemmatized_first_word_index[lemma_first].append(idx)

    def _rebuild_indexes(self) -> None:
        """Rebuild every first-word index from the current pattern list.

        Entry indices shift when a pattern is removed, so all index buckets are
        rebuilt from scratch rather than patched in place.
        """
        self._first_word_index.clear()
        self._stemmed_first_word_index.clear()
        self._lemmatized_first_word_index.clear()
        self._wildcard_patterns.clear()
        for idx, entry in enumerate(self._patterns):
            self._index_pattern(idx, entry["pattern"])

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
        for i, entry in enumerate(self._patterns):
            if entry["pattern"] == pattern and entry["that"] == that and entry["topic"] == topic:
                del self._patterns[i]
                self._rebuild_indexes()
                return True
        return False

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
            return ()

        # Normalize context
        that_normalized = normalize(that) if that else ""
        topic_normalized = normalize(topic) if topic else ""

        # Try exact matching first using the first-word index
        first_word = words[0]
        result = self._match_internal(normalized, that_normalized, topic_normalized, first_word, index_kind="exact")

        # A pure-wildcard (catch-all) match must not block the flexible
        # fallbacks - set it aside and try for something more specific. The
        # fallback stages ignore pure-wildcard results (via _specific) so they
        # don't simply re-return the catch-all.
        catchall = ()
        if result and is_pure_wildcard(result[4]):
            catchall, result = result, ()

        # If no specific match and lemmatization enabled, try lemmatized matching
        if not result and self._use_lemmatization:
            lemmatized = self._lemmatize(normalized)
            lemma_first = self._lemmatize(first_word)
            result = self._specific(
                self._match_internal(
                    lemmatized,
                    that_normalized,
                    topic_normalized,
                    lemma_first,
                    index_kind="lemmatized",
                )
            )

        # If still nothing and stemming enabled, try stemmed matching (aggressive)
        if not result and self._use_stemming:
            stemmed = stem_text(normalized)
            stemmed_first = stem_text(first_word)
            result = self._specific(
                self._match_internal(stemmed, that_normalized, topic_normalized, stemmed_first, index_kind="stemmed")
            )

        final = result if result else catchall
        return final

    @staticmethod
    def _specific(result: tuple) -> tuple:
        """Return result unless it is a pure-wildcard (catch-all) match, then ()."""
        if result and is_pure_wildcard(result[4]):
            return ()
        return result

    def _get_candidate_indices(
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

        # Always include wildcard patterns (they can match any first word)
        candidates.update(self._wildcard_patterns)

        # Add patterns matching the first word in the requested index
        if index_kind == "stemmed":
            candidates.update(self._stemmed_first_word_index.get(first_word, []))
        elif index_kind == "lemmatized":
            candidates.update(self._lemmatized_first_word_index.get(first_word, []))
        else:
            candidates.update(self._first_word_index.get(first_word, []))

        ordered = sorted(candidates)
        return ordered

    def _match_internal(
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
        # Get candidate patterns using first-word index
        candidate_indices = self._get_candidate_indices(first_word, index_kind)
        best: tuple = ()

        for idx in candidate_indices:
            entry = self._patterns[idx]
            # First check if pattern matches input
            match = entry["regex"].match(normalized)
            if not match:
                continue

            captured = list(match.groups())
            thatstars: list[str] = []
            topicstars: list[str] = []
            score = entry["specificity"]

            # Check topic constraint if pattern has one
            if entry["topic"] and entry["topic_regex"]:
                if not topic_normalized:
                    # Pattern requires topic but no topic set - skip
                    continue
                topic_match = entry["topic_regex"].match(topic_normalized)
                if not topic_match:
                    continue
                # Topic matches - add priority and capture wildcards
                score += TOPIC_PRIORITY + entry["topic_specificity"]
                topicstars = list(topic_match.groups())

            # Check that constraint if pattern has one
            if entry["that"] and entry["that_regex"]:
                if not that_normalized:
                    # Pattern requires that but no previous response - skip
                    continue
                that_match = entry["that_regex"].match(that_normalized)
                if not that_match:
                    continue
                # That matches - add priority and capture wildcards
                score += THAT_PRIORITY + entry["that_specificity"]
                thatstars = list(that_match.groups())

            # Update best if this is better
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

        return ()

    def get_patterns(self) -> list[tuple[str, str]]:
        """Get all pattern-response pairs.

        Returns:
            List of (pattern, response) tuples.
        """
        pairs = [(p["pattern"], p["response"]) for p in self._patterns]
        return pairs

    def get_patterns_with_context(self) -> list[tuple[str, str, str, str]]:
        """Get all pattern-response pairs with context.

        Returns:
            List of (pattern, response, that, topic) tuples.
        """
        entries = [(p["pattern"], p["response"], p["that"], p["topic"]) for p in self._patterns]
        return entries

    def clear(self) -> None:
        """Remove all patterns."""
        self._patterns.clear()
        self._first_word_index.clear()
        self._stemmed_first_word_index.clear()
        self._lemmatized_first_word_index.clear()
        self._wildcard_patterns.clear()

    def __len__(self) -> int:
        """Return number of patterns."""
        count = len(self._patterns)
        return count
