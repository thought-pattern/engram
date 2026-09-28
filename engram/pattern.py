"""AIML-style pattern matching for ENGRAM.

The matcher walks a graphmaster. At each word it tries branches in order and
backtracks when the rest of the input does not fit:

    1. ``$word``
    2. ``_`` (one or more words, shortest first)
    3. an exact word
    4. ``{bot:name}`` from the live bot properties
    5. ``{set:name}`` from the live word sets
    6. ``^`` (zero or more words, shortest first)
    7. ``#`` (zero or more words, shortest first)
    8. ``*`` (one or more words, shortest first)

A category with a topic is filed under that topic. Matching topics are tried
before the empty topic, and a ``that`` pattern is tried at the end of a
pattern path before the empty ``that``. The first complete path wins.

Stemming and lemmatization remain fallbacks used only when the exact walk
misses or finds only a pure wildcard.
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

from engram.constants import WILDCARD_TOKENS
from engram.substitutions import split_sentences
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


def break_intra_word_marks(text: str) -> str:
    """Turn hyphens and underscores inside a word into spaces.

    A wildcard that is the whole token stays a wildcard. ``MIL-STD-498`` and
    ``MIL_STD_498`` become the same words as ``MIL STD 498``.
    """
    pieces = []
    for word in text.split():
        if word in {"*", "_", "#", "^"} or (len(word) > 1 and word.startswith("$") and word[1:].isalnum()):
            pieces.append(word)
            continue
        pieces.append(word.replace("-", " ").replace("_", " "))
    result = " ".join(" ".join(pieces).split())
    return result


def _is_table_name(name: str) -> bool:
    """Return whether a {set:...} or {bot:...} name is word characters, as normalize_pattern keeps it."""
    result = bool(name) and name.replace("_", "").isalnum()
    return result


def _named_value(values: dict, name: str, default):
    """Look up a set or bot property by its pattern name.

    Pattern names are lowercased by ``normalize_pattern``, while set and
    properties files keep the case they were written in.
    """
    if name in values:
        result = values[name]
        return result
    for key, value in values.items():
        if isinstance(key, str) and key.lower() == name:
            result = value
            return result
    result = default
    return result


def _last_sentence(text: str) -> str:
    """Return the last sentence of a previous reply, or the whole text.

    As in AIML, ``that`` is the last sentence the bot said, which is where a
    joined multi-sentence reply ends with its question.
    """
    if not text or not text.strip():
        result = ""
        return result
    sentences = split_sentences(text)
    if sentences:
        result = sentences[-1]
        return result
    result = text.strip()
    return result


def prepare_pattern_text(text: str) -> str:
    """Normalize user text with the same word breaks patterns use.

    ``normalize`` keeps an intra-word hyphen and drops underscores. Matching
    breaks both marks first, so a hyphenated question and a spaced pattern
    share one token sequence. Stored sentences and repetition checks still
    use ``normalize`` unchanged.
    """
    result = normalize(break_intra_word_marks(text))
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
    result = break_intra_word_marks(result)

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
    normalized_text = prepare_pattern_text(text)

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


class _GraphNode:
    """One node in a pattern, that, or topic graphmaster."""

    def __init__(self) -> None:
        self.dollar: dict[str, _GraphNode] = {}
        self.underscore: _GraphNode | None = None
        self.atoms: dict[str, _GraphNode] = {}
        self.bots: dict[str, _GraphNode] = {}
        self.sets: dict[str, _GraphNode] = {}
        self.caret: _GraphNode | None = None
        self.hash: _GraphNode | None = None
        self.star: _GraphNode | None = None
        self.category: dict | None = None
        self.that_root: _GraphNode | None = None
        self.subtree: _GraphNode | None = None
        self.lemma_dollar: dict[str, list[_GraphNode]] = {}
        self.stem_dollar: dict[str, list[_GraphNode]] = {}
        self.lemma_atoms: dict[str, list[_GraphNode]] = {}
        self.stem_atoms: dict[str, list[_GraphNode]] = {}


class PatternMatcher:
    """AIML-style pattern matcher. Categories are selected by a graphmaster walk."""

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
        self.default_trie = _GraphNode()
        self.topic_router = _GraphNode()
        self.topic_tries: dict[str, _GraphNode] = {}
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
        entry = {
            "pattern": pattern,
            "response": response,
            "that": that or "",
            "topic": topic or "",
        }
        self.internal_patterns.append(entry)
        self._index_entry(entry)

    def _index_entry(self, entry: dict) -> None:
        """File one category under its topic, pattern, and that path.

        The first category to claim a path keeps it. A later copy of the same
        path stays in the list for removal, and does not change the walk.
        """
        trie = self._trie_for_topic(entry["topic"])
        leaf = self._insert_tokens(trie, normalize_pattern(entry["pattern"]).split())
        payload = {
            "pattern": entry["pattern"],
            "response": entry["response"],
            "that": entry["that"],
            "topic": entry["topic"],
        }
        if entry["that"]:
            if leaf.that_root is None:
                leaf.that_root = _GraphNode()
            that_leaf = self._insert_tokens(leaf.that_root, normalize_pattern(entry["that"]).split())
            if that_leaf.category is None:
                that_leaf.category = payload
            return
        if leaf.category is None:
            leaf.category = payload

    def _trie_for_topic(self, topic: str) -> _GraphNode:
        """Return the category trie for a topic pattern, creating it on first use."""
        key = normalize_pattern(topic) if topic else ""
        if not key:
            result = self.default_trie
            return result
        existing = self.topic_tries.get(key)
        if existing is not None:
            result = existing
            return result
        trie = _GraphNode()
        self.topic_tries[key] = trie
        leaf = self._insert_tokens(self.topic_router, key.split())
        if leaf.subtree is None:
            leaf.subtree = trie
        result = trie
        return result

    def _insert_tokens(self, root: _GraphNode, words: list[str]) -> _GraphNode:
        """Walk or create the edges for one pattern and return the leaf."""
        node = root
        for word in words:
            node = self._edge(node, word)
        return node

    def _edge(self, node: _GraphNode, word: str) -> _GraphNode:
        """Return the child reached by one pattern token, creating it if needed."""
        if word == "_":
            if node.underscore is None:
                node.underscore = _GraphNode()
            result = node.underscore
            return result
        if word == "^":
            if node.caret is None:
                node.caret = _GraphNode()
            result = node.caret
            return result
        if word == "#":
            if node.hash is None:
                node.hash = _GraphNode()
            result = node.hash
            return result
        if word == "*":
            if node.star is None:
                node.star = _GraphNode()
            result = node.star
            return result
        if len(word) > 1 and word.startswith("$") and word[1:].isalnum():
            key = word[1:]
            child = node.dollar.get(key)
            if child is None:
                child = _GraphNode()
                node.dollar[key] = child
                self._remember_flexible(node.lemma_dollar, node.stem_dollar, key, child)
            result = child
            return result
        if word.startswith("{bot:") and word.endswith("}") and _is_table_name(word[5:-1]):
            name = word[5:-1]
            child = node.bots.get(name)
            if child is None:
                child = _GraphNode()
                node.bots[name] = child
            result = child
            return result
        if word.startswith("{set:") and word.endswith("}") and _is_table_name(word[5:-1]):
            name = word[5:-1]
            child = node.sets.get(name)
            if child is None:
                child = _GraphNode()
                node.sets[name] = child
            result = child
            return result
        child = node.atoms.get(word)
        if child is None:
            child = _GraphNode()
            node.atoms[word] = child
            self._remember_flexible(node.lemma_atoms, node.stem_atoms, word, child)
        result = child
        return result

    def _remember_flexible(self, lemma_index: dict, stem_index: dict, word: str, child: _GraphNode) -> None:
        """Index an exact edge under its lemma and stem for the fallback walks."""
        if self.internal_use_lemmatization:
            lemma = self.internal_lemmatize(word)
            bucket = lemma_index.get(lemma)
            if bucket is None:
                lemma_index[lemma] = [child]
            elif child not in bucket:
                bucket.append(child)
        if self.internal_use_stemming:
            stemmed = stem_text(word)
            bucket = stem_index.get(stemmed)
            if bucket is None:
                stem_index[stemmed] = [child]
            elif child not in bucket:
                bucket.append(child)

    def rebuild_indexes(self) -> None:
        """Rebuild the graphmaster from the current pattern list.

        Entry identity shifts when a pattern is removed, so the tries are
        rebuilt from scratch rather than patched in place.
        """
        self.default_trie = _GraphNode()
        self.topic_router = _GraphNode()
        self.topic_tries = {}
        for entry in self.internal_patterns:
            self._index_entry(entry)

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
        """Find the graphmaster path for input with that and topic context.

        ``that`` is the previous reply. Only its last sentence is matched,
        and it is prepared with the same hyphen and punctuation rules as the
        input. Topic categories are tried before the empty topic.

        Args:
            text: User input text.
            that: Bot's previous response.
            topic: Current topic predicate.

        Returns:
            Tuple of (response, captured, thatstars, topicstars, pattern, topic, that), or ().
        """
        words = prepare_pattern_text(text).split()
        if not words:
            result = ()
            return result

        that_text = _last_sentence(that)
        that_words = prepare_pattern_text(that_text).split() if that_text else []
        topic_words = prepare_pattern_text(topic).split() if topic else []
        result = self._match_words(words, that_words, topic_words, "exact")

        # A pure-wildcard match must not block the flexible fallbacks. Set it
        # aside and try for something more specific. The fallback stages ignore
        # pure-wildcard results so they do not simply re-return the catch-all.
        catchall = ()
        if result and is_pure_wildcard(result[4]):
            catchall, result = result, ()

        if not result and self.internal_use_lemmatization:
            result = specific_pattern_result(
                self._match_words(
                    self._flexible_words(words, "lemma"),
                    self._flexible_words(that_words, "lemma"),
                    self._flexible_words(topic_words, "lemma"),
                    "lemma",
                )
            )

        if not result and self.internal_use_stemming:
            result = specific_pattern_result(
                self._match_words(
                    self._flexible_words(words, "stem"),
                    self._flexible_words(that_words, "stem"),
                    self._flexible_words(topic_words, "stem"),
                    "stem",
                )
            )

        final = result if result else catchall
        return final

    def _flexible_words(self, words: list[str], mode: str) -> list[str]:
        """Lemmatize or stem words that were already prepared for an exact walk."""
        text = " ".join(words)
        converted = self.internal_lemmatize(text) if mode == "lemma" else stem_text(text)
        result = converted.split()
        return result

    def _match_words(self, words: list[str], that_words: list[str], topic_words: list[str], mode: str) -> tuple:
        """Walk eligible topic tries, then the default topic."""

        def on_topic(node: _GraphNode, topicstars: list[str]) -> tuple:
            if node.subtree is None:
                result = ()
                return result
            found = self._match_categories(node.subtree, words, that_words, topicstars, mode)
            return found

        routed = self._walk(self.topic_router, topic_words, 0, [], mode, on_topic)
        if routed:
            return routed
        found = self._match_categories(self.default_trie, words, that_words, [], mode)
        return found

    def _match_categories(
        self,
        trie: _GraphNode,
        words: list[str],
        that_words: list[str],
        topicstars: list[str],
        mode: str,
    ) -> tuple:
        """Walk one topic's categories. A matching that wins over an empty that."""

        def on_leaf(node: _GraphNode, stars: list[str]) -> tuple:
            if node.that_root is not None:

                def on_that(that_node: _GraphNode, thatstars: list[str]) -> tuple:
                    taken = self._take_category(that_node, stars, thatstars, topicstars)
                    return taken

                matched_that = self._walk(node.that_root, that_words, 0, [], mode, on_that)
                if matched_that:
                    return matched_that
            taken = self._take_category(node, stars, [], topicstars)
            return taken

        found = self._walk(trie, words, 0, [], mode, on_leaf)
        return found

    def _take_category(
        self,
        node: _GraphNode,
        stars: list[str],
        thatstars: list[str],
        topicstars: list[str],
    ) -> tuple:
        """Return the match tuple for a leaf category, or empty when the leaf is bare."""
        category = node.category
        if not category:
            result = ()
            return result
        result = (
            category["response"],
            list(stars),
            list(thatstars),
            list(topicstars),
            category["pattern"],
            category["topic"],
            category["that"],
        )
        return result

    def _walk(self, node: _GraphNode, words: list[str], pos: int, stars: list[str], mode: str, on_leaf) -> tuple:
        """Try this node's branches in AIML order. The first path that finishes wins."""
        if pos == len(words):
            found = on_leaf(node, stars)
            if found:
                return found
            if node.caret is not None:
                found = self._walk(node.caret, words, pos, stars + [""], mode, on_leaf)
                if found:
                    return found
            if node.hash is not None:
                found = self._walk(node.hash, words, pos, stars + [""], mode, on_leaf)
                if found:
                    return found
            result = ()
            return result

        word = words[pos]
        for child in self._dollar_children(node, word, mode):
            found = self._walk(child, words, pos + 1, stars, mode, on_leaf)
            if found:
                return found

        if node.underscore is not None:
            for end in range(pos + 1, len(words) + 1):
                captured = " ".join(words[pos:end])
                found = self._walk(node.underscore, words, end, stars + [captured], mode, on_leaf)
                if found:
                    return found

        for child in self._atom_children(node, word, mode):
            found = self._walk(child, words, pos + 1, stars, mode, on_leaf)
            if found:
                return found

        for name, child in node.bots.items():
            expected = self._bound_words(_named_value(self.internal_bot_properties, name, ""), mode)
            end = self._consume_fixed(words, pos, expected)
            if end < 0:
                continue
            captured = " ".join(words[pos:end])
            found = self._walk(child, words, end, stars + [captured], mode, on_leaf)
            if found:
                return found

        for name, child in node.sets.items():
            members = []
            for raw in _named_value(self.internal_sets, name, ()) or ():
                member_words = self._bound_words(raw, mode)
                if member_words:
                    members.append(member_words)
            members.sort(key=len, reverse=True)
            for member_words in members:
                end = self._consume_fixed(words, pos, member_words)
                if end < 0:
                    continue
                captured = " ".join(words[pos:end])
                found = self._walk(child, words, end, stars + [captured], mode, on_leaf)
                if found:
                    return found

        if node.caret is not None:
            for end in range(pos, len(words) + 1):
                captured = " ".join(words[pos:end])
                found = self._walk(node.caret, words, end, stars + [captured], mode, on_leaf)
                if found:
                    return found

        if node.hash is not None:
            for end in range(pos, len(words) + 1):
                captured = " ".join(words[pos:end])
                found = self._walk(node.hash, words, end, stars + [captured], mode, on_leaf)
                if found:
                    return found

        if node.star is not None:
            for end in range(pos + 1, len(words) + 1):
                captured = " ".join(words[pos:end])
                found = self._walk(node.star, words, end, stars + [captured], mode, on_leaf)
                if found:
                    return found

        result = ()
        return result

    def _dollar_children(self, node: _GraphNode, word: str, mode: str) -> list[_GraphNode]:
        """Return the priority-word edges that match this input word."""
        if mode == "lemma":
            result = list(node.lemma_dollar.get(word, ()))
            return result
        if mode == "stem":
            result = list(node.stem_dollar.get(word, ()))
            return result
        child = node.dollar.get(word)
        if child is None:
            result = []
            return result
        result = [child]
        return result

    def _atom_children(self, node: _GraphNode, word: str, mode: str) -> list[_GraphNode]:
        """Return the exact-word edges that match this input word."""
        if mode == "lemma":
            result = list(node.lemma_atoms.get(word, ()))
            return result
        if mode == "stem":
            result = list(node.stem_atoms.get(word, ()))
            return result
        child = node.atoms.get(word)
        if child is None:
            result = []
            return result
        result = [child]
        return result

    def _bound_words(self, value: str, mode: str) -> list[str]:
        """Prepare a set member or bot property the same way as the input."""
        prepared = prepare_pattern_text(str(value))
        if not prepared:
            result = []
            return result
        if mode == "lemma":
            prepared = self.internal_lemmatize(prepared)
        elif mode == "stem":
            prepared = stem_text(prepared)
        result = prepared.split()
        return result

    def _consume_fixed(self, words: list[str], pos: int, expected: list[str]) -> int:
        """Return the index after a fixed word sequence, or -1 when it does not fit."""
        count = len(expected)
        if not count or pos + count > len(words):
            result = -1
            return result
        if words[pos : pos + count] != expected:
            result = -1
            return result
        result = pos + count
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
        self.default_trie = _GraphNode()
        self.topic_router = _GraphNode()
        self.topic_tries = {}

    def __len__(self) -> int:
        """Return number of patterns."""
        count = len(self.internal_patterns)
        return count
