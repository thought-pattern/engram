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

Matching follows AIML order: the pattern first, then ``that``, then the
topic. At the end of a pattern path a matching ``that`` is tried before the
empty ``that``, and at the end of that a matching topic before the empty
topic, so a specific pattern outranks any topic's catch-all. The first
complete path wins.

Captures are kept as positions in the input and joined into text only for the
chosen category. The walk descends one frame per pattern token, so patterns
are capped at ``MAX_PATTERN_WORDS`` words to bound its depth.

Stemming and lemmatization remain fallbacks used only when the exact walk
misses or finds only a pure wildcard. Their captures are the original words.
"""

from functools import lru_cache
from re import (
    IGNORECASE as IGNORECASE,
    Match as re_Match,
    compile as re_compile,
    sub as re_sub,
)

from engram.constants import MAX_PATTERN_WORDS, WILDCARD_TOKENS
from engram.substitutions import split_sentences
from engram.text import lemmatize_text, lemmatize_text_spacy, normalize, stem_text


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


def is_table_name(name: str) -> bool:
    """Return whether a {set:...} or {bot:...} name is word characters, as normalize_pattern keeps it."""
    result = bool(name) and name.replace("_", "").isalnum()
    return result


def named_value(values: dict, name: str, default):
    """Look up a set or bot property by its pattern name.

    Pattern names are lowercased by ``normalize_pattern``, while set and
    properties files keep the case they were written in.
    """
    if name in values:
        result = values.get(name, default)
        return result
    for key, value in values.items():
        if isinstance(key, str) and key.lower() == name:
            result = value
            return result
    result = default
    return result


def last_sentence(text: str) -> str:
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


def pattern_word_count(pattern: str) -> int:
    """Return how many graph edges a pattern, that, or topic occupies."""
    result = len(normalize_pattern(pattern).split())
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


class GraphNode:
    """One node in a pattern, that, or topic graphmaster.

    ``wildcards`` holds the ``_``, ``^``, ``#`` and ``*`` children keyed by
    their token, and ``segments`` holds the ``that`` and ``topic`` segment
    roots keyed by segment name; a missing key means the node has no such
    edge or segment. ``category`` is the category filed at this node, or
    ``{}`` when none is.
    """

    def __init__(self) -> None:
        self.dollar: dict[str, GraphNode] = {}
        self.wildcards: dict[str, GraphNode] = {}
        self.atoms: dict[str, GraphNode] = {}
        self.bots: dict[str, GraphNode] = {}
        self.sets: dict[str, GraphNode] = {}
        self.category: dict = {}
        self.segments: dict[str, GraphNode] = {}
        self.lemma_dollar: dict[str, list[GraphNode]] = {}
        self.stem_dollar: dict[str, list[GraphNode]] = {}
        self.lemma_atoms: dict[str, list[GraphNode]] = {}
        self.stem_atoms: dict[str, list[GraphNode]] = {}

    def is_empty(self) -> bool:
        """Return whether this node holds no category, that or topic segment, or edge."""
        result = not (self.category or self.segments or self.wildcards or self.dollar or self.atoms or self.bots or self.sets)
        return result


# The read default for a child or segment lookup. Every lookup checks the key
# first, so this node is never returned to a walk and nothing writes to it.
EMPTY_GRAPH_NODE = GraphNode()


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
        # Entries by insertion id; dict order is insertion order.
        self.internal_entries: dict[int, dict] = {}
        self.internal_next_entry_id = 0
        # (pattern, that, topic) -> entry ids, so removal finds its entry directly.
        self.internal_entry_ids: dict[tuple[str, str, str], list[int]] = {}
        # Normalized graph path -> ids of the entries filed there, in insertion
        # order. The first one owns the leaf; the next takes over when it goes.
        self.internal_path_claims: dict[tuple[str, str, bool, str], list[int]] = {}
        # Pattern paths lead to an optional that segment and then an optional
        # topic segment; a category with neither sits at the pattern leaf.
        self.default_trie = GraphNode()
        # Preserve caller-owned dict references, including explicitly empty maps.
        self.internal_sets = sets if isinstance(sets, dict) else {}
        self.internal_bot_properties = bot_properties if isinstance(bot_properties, dict) else {}
        self.internal_use_stemming = use_stemming
        self.internal_use_lemmatization = use_lemmatization
        self.internal_lemmatize = lemmatize_text_spacy if use_spacy_lemmatization else lemmatize_text

    @property
    def internal_patterns(self) -> list[dict]:
        """Entries in insertion order."""
        result = list(self.internal_entries.values())
        return result

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

        Raises:
            ValueError: If the pattern, that, or topic exceeds MAX_PATTERN_WORDS.
        """
        for name, value in (("pattern", pattern), ("that", that), ("topic", topic)):
            if value and pattern_word_count(value) > MAX_PATTERN_WORDS:
                raise ValueError(f"{name} exceeds the limit of {MAX_PATTERN_WORDS} words")
        entry_that = that or ""
        entry_topic = topic or ""
        entry = {
            "pattern": pattern,
            "response": response,
            "that": entry_that,
            "topic": entry_topic,
        }
        entry_id = self.internal_next_entry_id
        self.internal_next_entry_id += 1
        self.internal_entries[entry_id] = entry
        self.internal_entry_ids.setdefault((pattern, entry_that, entry_topic), []).append(entry_id)
        self.index_entry(entry_id, entry)

    @staticmethod
    def path_key(entry: dict) -> tuple[str, str, bool, str]:
        """Return the normalized topic, pattern, and that path an entry is filed under."""
        topic = entry.get("topic", "")
        that = entry.get("that", "")
        topic_key = normalize_pattern(topic) if topic else ""
        that_key = normalize_pattern(that) if that else ""
        result = (topic_key, normalize_pattern(entry.get("pattern", "")), bool(that), that_key)
        return result

    def index_entry(self, entry_id: int, entry: dict) -> None:
        """File one category under its topic, pattern, and that path.

        The first category to claim a path keeps it. A later copy of the same
        path waits in the path's claim list and takes over if the first is
        removed. The leaf holds its own copy of the entry.
        """
        key = self.path_key(entry)
        claims = self.internal_path_claims.setdefault(key, [])
        claims.append(entry_id)
        if len(claims) == 1:
            self.category_leaf(key).category = dict(entry)

    def category_leaf(self, key: tuple[str, str, bool, str]) -> GraphNode:
        """Return the node holding a path's category, creating the path if needed."""
        topic_key, pattern_key, has_that, that_key = key
        leaf = self.insert_tokens(self.default_trie, pattern_key.split())
        if has_that:
            if "that" not in leaf.segments:
                leaf.segments["that"] = GraphNode()
            leaf = self.insert_tokens(leaf.segments.get("that", EMPTY_GRAPH_NODE), that_key.split())
        if topic_key:
            if "topic" not in leaf.segments:
                leaf.segments["topic"] = GraphNode()
            leaf = self.insert_tokens(leaf.segments.get("topic", EMPTY_GRAPH_NODE), topic_key.split())
        return leaf

    def unindex_entry(self, entry_id: int, entry: dict) -> None:
        """Take one entry out of the graph, handing its leaf to the next claimant."""
        key = self.path_key(entry)
        claims = self.internal_path_claims.get(key, [])
        if entry_id in claims:
            was_owner = claims[0] == entry_id
            claims.remove(entry_id)
            if not claims:
                del self.internal_path_claims[key]
                self.prune_path(key)
            elif was_owner:
                successor = self.internal_entries.get(claims[0], {})
                self.category_leaf(key).category = dict(successor)

    def prune_path(self, key: tuple[str, str, bool, str]) -> None:
        """Clear a path's category and drop the nodes that no longer lead anywhere."""
        topic_key, pattern_key, has_that, that_key = key
        pattern_found, pattern_path = self.existing_path(self.default_trie, pattern_key.split())
        if pattern_found:
            leaf = pattern_path[-1][2] if pattern_path else self.default_trie
            if has_that:
                if "that" in leaf.segments:
                    that_root = leaf.segments.get("that", EMPTY_GRAPH_NODE)
                    that_found, that_path = self.existing_path(that_root, that_key.split())
                    if that_found:
                        that_leaf = that_path[-1][2] if that_path else that_root
                        self.clear_topic_category(that_leaf, topic_key)
                        self.prune_edges(that_path)
                    if that_root.is_empty():
                        del leaf.segments["that"]
            else:
                self.clear_topic_category(leaf, topic_key)
            self.prune_edges(pattern_path)

    def clear_topic_category(self, leaf: GraphNode, topic_key: str) -> None:
        """Clear the category below a pattern or that leaf, through its topic segment."""
        if not topic_key:
            leaf.category = {}
        elif "topic" in leaf.segments:
            topic_root = leaf.segments.get("topic", EMPTY_GRAPH_NODE)
            topic_found, topic_path = self.existing_path(topic_root, topic_key.split())
            if topic_found:
                topic_leaf = topic_path[-1][2] if topic_path else topic_root
                topic_leaf.category = {}
                self.prune_edges(topic_path)
            if topic_root.is_empty():
                del leaf.segments["topic"]

    def existing_path(self, root: GraphNode, words: list[str]) -> tuple:
        """Follow existing edges for ``words`` without creating any.

        Returns ``(found, steps)``: the (parent, word, child) steps taken, or
        ``(False, [])`` when an edge is missing. An empty ``words`` is found
        with no steps.
        """
        path = []
        node = root
        for word in words:
            present, child = self.existing_edge(node, word)
            if not present:
                missing = (False, [])
                return missing
            path.append((node, word, child))
            node = child
        found = (True, path)
        return found

    def existing_edge(self, node: GraphNode, word: str) -> tuple:
        """Return ``(present, child)`` for one pattern token without creating the child."""
        kind, name = self.edge_kind(word)
        table = self.edge_table(node, kind)
        present = name in table
        child = table.get(name, EMPTY_GRAPH_NODE)
        result = (present, child)
        return result

    def prune_edges(self, path: list[tuple[GraphNode, str, GraphNode]]) -> None:
        """Detach empty nodes from the end of a path back toward its root."""
        for parent, word, child in reversed(path):
            if not child.is_empty():
                break
            self.detach(parent, word, child)

    def detach(self, parent: GraphNode, word: str, child: GraphNode) -> None:
        """Remove the edge ``word`` from ``parent``, including its fallback index entries."""
        kind, name = self.edge_kind(word)
        table = self.edge_table(parent, kind)
        if name in table:
            del table[name]
        if kind == "dollar":
            self.forget_flexible(parent.lemma_dollar, parent.stem_dollar, name, child)
        elif kind == "atom":
            self.forget_flexible(parent.lemma_atoms, parent.stem_atoms, name, child)

    @staticmethod
    def edge_kind(word: str) -> tuple[str, str]:
        """Classify a pattern token the way ``edge`` files it and name the key it is filed under."""
        if word in WILDCARD_TOKENS:
            result = ("wildcard", word)
            return result
        if len(word) > 1 and word.startswith("$") and word[1:].isalnum():
            result = ("dollar", word[1:])
            return result
        if word.startswith("{bot:") and word.endswith("}") and is_table_name(word[5:-1]):
            result = ("bot", word[5:-1])
            return result
        if word.startswith("{set:") and word.endswith("}") and is_table_name(word[5:-1]):
            result = ("set", word[5:-1])
            return result
        result = ("atom", word)
        return result

    @staticmethod
    def edge_table(node: GraphNode, kind: str) -> dict:
        """Return the node's child table that holds edges of one ``edge_kind`` kind."""
        if kind == "wildcard":
            table = node.wildcards
        elif kind == "dollar":
            table = node.dollar
        elif kind == "bot":
            table = node.bots
        elif kind == "set":
            table = node.sets
        else:
            table = node.atoms
        return table

    def insert_tokens(self, root: GraphNode, words: list[str]) -> GraphNode:
        """Walk or create the edges for one pattern and return the leaf."""
        node = root
        for word in words:
            node = self.edge(node, word)
        return node

    def edge(self, node: GraphNode, word: str) -> GraphNode:
        """Return the child reached by one pattern token, creating it if needed."""
        kind, name = self.edge_kind(word)
        table = self.edge_table(node, kind)
        if name not in table:
            created = GraphNode()
            table[name] = created
            if kind == "dollar":
                self.remember_flexible(node.lemma_dollar, node.stem_dollar, name, created)
            elif kind == "atom":
                self.remember_flexible(node.lemma_atoms, node.stem_atoms, name, created)
        child = table.get(name, EMPTY_GRAPH_NODE)
        return child

    def forget_flexible(self, lemma_index: dict, stem_index: dict, word: str, child: GraphNode) -> None:
        """Drop a detached edge from the lemma and stem fallback indexes."""
        keys = []
        if self.internal_use_lemmatization:
            keys.append((lemma_index, self.internal_lemmatize(word)))
        if self.internal_use_stemming:
            keys.append((stem_index, stem_text(word)))
        for index, key in keys:
            bucket = index.get(key, [])
            if bucket and child in bucket:
                bucket.remove(child)
                if not bucket:
                    del index[key]

    def remember_flexible(self, lemma_index: dict, stem_index: dict, word: str, child: GraphNode) -> None:
        """Index an exact edge under its lemma and stem for the fallback walks."""
        if self.internal_use_lemmatization:
            lemma = self.internal_lemmatize(word)
            if lemma not in lemma_index:
                lemma_index[lemma] = [child]
            else:
                bucket = lemma_index.get(lemma, [])
                if child not in bucket:
                    bucket.append(child)
        if self.internal_use_stemming:
            stemmed = stem_text(word)
            if stemmed not in stem_index:
                stem_index[stemmed] = [child]
            else:
                bucket = stem_index.get(stemmed, [])
                if child not in bucket:
                    bucket.append(child)

    def rebuild_indexes(self) -> None:
        """Rebuild the graphmaster from the current entries.

        Removal updates the graph in place, so this is only needed after the
        fallback settings change.
        """
        self.default_trie = GraphNode()
        self.internal_path_claims = {}
        for entry_id, entry in self.internal_entries.items():
            self.index_entry(entry_id, entry)

    def remove_pattern(self, pattern: str, that: str = "", topic: str = "") -> bool:
        """Remove the first entry matching (pattern, that, topic).

        Called when the statement backing a pattern is evicted or retired, so a
        dead pattern cannot keep matching (and shadowing live patterns) after
        its statement is gone. Only that entry's graph path changes.

        Args:
            pattern: AIML-style pattern to remove.
            that: The entry's that-context ("" when none).
            topic: The entry's topic scope ("" when none).

        Returns:
            True if an entry was removed, False if no entry matched.
        """
        key = (pattern, that, topic)
        entry_ids = self.internal_entry_ids.get(key, [])
        if not entry_ids:
            result = False
            return result
        entry_id = entry_ids.pop(0)
        if not entry_ids:
            del self.internal_entry_ids[key]
        entry = self.internal_entries.pop(entry_id)
        self.unindex_entry(entry_id, entry)
        result = True
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
        input. Matching follows AIML order: pattern, then that, then topic.

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

        that_text = last_sentence(that)
        that_words = prepare_pattern_text(that_text).split() if that_text else []
        topic_words = prepare_pattern_text(topic).split() if topic else []
        # Captures always come from the prepared words, including in the lemma
        # and stem walks, whose words line up with these by position.
        sources = (words, that_words, topic_words)
        result = self.match_words(words, that_words, topic_words, "exact", sources)

        # A pure-wildcard match must not block the flexible fallbacks. Set it
        # aside and try for something more specific. The fallback stages ignore
        # pure-wildcard results so they do not simply re-return the catch-all.
        catchall = ()
        if result and is_pure_wildcard(result[4]):
            catchall, result = result, ()

        if not result and self.internal_use_lemmatization:
            result = specific_pattern_result(
                self.match_words(
                    self.flexible_words(words, "lemma"),
                    self.flexible_words(that_words, "lemma"),
                    self.flexible_words(topic_words, "lemma"),
                    "lemma",
                    sources,
                )
            )

        if not result and self.internal_use_stemming:
            result = specific_pattern_result(
                self.match_words(
                    self.flexible_words(words, "stem"),
                    self.flexible_words(that_words, "stem"),
                    self.flexible_words(topic_words, "stem"),
                    "stem",
                    sources,
                )
            )

        final = result if result else catchall
        return final

    def flexible_words(self, words: list[str], mode: str) -> list[str]:
        """Lemmatize or stem prepared words, one output word per input word.

        The whole text is converted at once so the lemmatizer sees context.
        If that changes the word count, each word is converted alone, so a
        capture's position still names the original words.
        """
        converted = self.convert(" ".join(words), mode).split()
        if len(converted) == len(words):
            return converted
        result = []
        for word in words:
            parts = self.convert(word, mode).split()
            result.append(parts[0] if len(parts) == 1 else word)
        return result

    def convert(self, text: str, mode: str) -> str:
        """Lemmatize text in ``lemma`` mode, otherwise stem it."""
        result = self.internal_lemmatize(text) if mode == "lemma" else stem_text(text)
        return result

    def match_words(
        self,
        words: list[str],
        that_words: list[str],
        topic_words: list[str],
        mode: str,
        sources: tuple[list[str], list[str], list[str]],
    ) -> tuple:
        """Walk the pattern, then its that segment, then its topic segment.

        ``sources`` are the prepared input, that, and topic words that
        captures are read from.
        """

        def on_pattern(node: GraphNode, stars: list[tuple[int, int]]) -> tuple:
            if "that" in node.segments:

                def on_that(that_node: GraphNode, thatstars: list[tuple[int, int]]) -> tuple:
                    found = self.topic_category(that_node, stars, thatstars, topic_words, mode, sources)
                    return found

                that_root = node.segments.get("that", EMPTY_GRAPH_NODE)
                matched_that = self.walk(that_root, that_words, 0, [], mode, on_that, set())
                if matched_that:
                    return matched_that
            found = self.topic_category(node, stars, [], topic_words, mode, sources)
            return found

        found = self.walk(self.default_trie, words, 0, [], mode, on_pattern, set())
        return found

    def topic_category(
        self,
        node: GraphNode,
        stars: list[tuple[int, int]],
        thatstars: list[tuple[int, int]],
        topic_words: list[str],
        mode: str,
        sources: tuple[list[str], list[str], list[str]],
    ) -> tuple:
        """Take the category below a pattern or that leaf; a matching topic beats no topic."""
        if "topic" in node.segments:

            def on_topic(topic_node: GraphNode, topicstars: list[tuple[int, int]]) -> tuple:
                taken = self.take_category(topic_node, stars, thatstars, topicstars, sources)
                return taken

            topic_root = node.segments.get("topic", EMPTY_GRAPH_NODE)
            found = self.walk(topic_root, topic_words, 0, [], mode, on_topic, set())
            if found:
                return found
        taken = self.take_category(node, stars, thatstars, [], sources)
        return taken

    def take_category(
        self,
        node: GraphNode,
        stars: list[tuple[int, int]],
        thatstars: list[tuple[int, int]],
        topicstars: list[tuple[int, int]],
        sources: tuple[list[str], list[str], list[str]],
    ) -> tuple:
        """Return the match tuple for a leaf category, or empty when the leaf is bare."""
        category = node.category
        if not category:
            result = ()
            return result
        words, that_words, topic_words = sources
        result = (
            category.get("response", ""),
            [" ".join(words[start:end]) for start, end in stars],
            [" ".join(that_words[start:end]) for start, end in thatstars],
            [" ".join(topic_words[start:end]) for start, end in topicstars],
            category.get("pattern", ""),
            category.get("topic", ""),
            category.get("that", ""),
        )
        return result

    def walk(
        self,
        node: GraphNode,
        words: list[str],
        pos: int,
        stars: list[tuple[int, int]],
        mode: str,
        on_leaf,
        failed: set,
    ) -> tuple:
        """Try this node's branches in AIML order. The first path that finishes wins.

        Each capture is a (start, end) span of ``words``. Whether a walk from a
        node at a position succeeds does not depend on the captures taken to get
        there, so ``failed`` remembers (node, position) states that already
        failed during this walk. Consecutive wildcards then try each suffix once
        instead of once per capture partition, and branch order is unchanged.
        """
        if (node, pos) in failed:
            result = ()
            return result
        if pos == len(words):
            found = on_leaf(node, stars)
            if found:
                return found
            for token in ("^", "#"):
                if token in node.wildcards:
                    child = node.wildcards.get(token, EMPTY_GRAPH_NODE)
                    found = self.walk(child, words, pos, stars + [(pos, pos)], mode, on_leaf, failed)
                    if found:
                        return found
            failed.add((node, pos))
            result = ()
            return result

        word = words[pos]
        for child in self.dollar_children(node, word, mode):
            found = self.walk(child, words, pos + 1, stars, mode, on_leaf, failed)
            if found:
                return found

        if "_" in node.wildcards:
            underscore = node.wildcards.get("_", EMPTY_GRAPH_NODE)
            for end in range(pos + 1, len(words) + 1):
                found = self.walk(underscore, words, end, stars + [(pos, end)], mode, on_leaf, failed)
                if found:
                    return found

        for child in self.atom_children(node, word, mode):
            found = self.walk(child, words, pos + 1, stars, mode, on_leaf, failed)
            if found:
                return found

        for name, child in node.bots.items():
            expected = self.bound_words(named_value(self.internal_bot_properties, name, ""), mode)
            end = self.consume_fixed(words, pos, expected)
            if end < 0:
                continue
            found = self.walk(child, words, end, stars + [(pos, end)], mode, on_leaf, failed)
            if found:
                return found

        for name, child in node.sets.items():
            members = []
            for raw in named_value(self.internal_sets, name, []) or []:
                member_words = self.bound_words(raw, mode)
                if member_words:
                    members.append(member_words)
            members.sort(key=len, reverse=True)
            for member_words in members:
                end = self.consume_fixed(words, pos, member_words)
                if end < 0:
                    continue
                found = self.walk(child, words, end, stars + [(pos, end)], mode, on_leaf, failed)
                if found:
                    return found

        # Zero-or-more wildcards may consume nothing; ``*`` takes at least one word.
        for token, first_end in (("^", pos), ("#", pos), ("*", pos + 1)):
            if token in node.wildcards:
                child = node.wildcards.get(token, EMPTY_GRAPH_NODE)
                for end in range(first_end, len(words) + 1):
                    found = self.walk(child, words, end, stars + [(pos, end)], mode, on_leaf, failed)
                    if found:
                        return found

        failed.add((node, pos))
        result = ()
        return result

    def dollar_children(self, node: GraphNode, word: str, mode: str) -> list[GraphNode]:
        """Return the priority-word edges that match this input word."""
        if mode == "lemma":
            result = list(node.lemma_dollar.get(word, []))
            return result
        if mode == "stem":
            result = list(node.stem_dollar.get(word, []))
            return result
        if word not in node.dollar:
            result = []
            return result
        result = [node.dollar.get(word, EMPTY_GRAPH_NODE)]
        return result

    def atom_children(self, node: GraphNode, word: str, mode: str) -> list[GraphNode]:
        """Return the exact-word edges that match this input word."""
        if mode == "lemma":
            result = list(node.lemma_atoms.get(word, []))
            return result
        if mode == "stem":
            result = list(node.stem_atoms.get(word, []))
            return result
        if word not in node.atoms:
            result = []
            return result
        result = [node.atoms.get(word, EMPTY_GRAPH_NODE)]
        return result

    def bound_words(self, value: str, mode: str) -> list[str]:
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

    def consume_fixed(self, words: list[str], pos: int, expected: list[str]) -> int:
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
        pairs = [(entry.get("pattern", ""), entry.get("response", "")) for entry in self.internal_entries.values()]
        return pairs

    def get_patterns_with_context(self) -> list[tuple[str, str, str, str]]:
        """Get all pattern-response pairs with context.

        Returns:
            List of (pattern, response, that, topic) tuples.
        """
        entries = [
            (entry.get("pattern", ""), entry.get("response", ""), entry.get("that", ""), entry.get("topic", ""))
            for entry in self.internal_entries.values()
        ]
        return entries

    def clear(self) -> None:
        """Remove all patterns."""
        self.internal_entries.clear()
        self.internal_entry_ids.clear()
        self.internal_path_claims.clear()
        self.default_trie = GraphNode()

    def __len__(self) -> int:
        """Return number of patterns."""
        count = len(self.internal_entries)
        return count
