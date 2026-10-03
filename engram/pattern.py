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


class _GraphNode:
    """One node in a pattern, that, or topic graphmaster."""

    def is_empty(self) -> bool:
        """Return whether this node holds no category, that or topic segment, or edge."""
        result = (
            self.category is None
            and self.that_root is None
            and self.topic_root is None
            and self.underscore is None
            and self.caret is None
            and self.hash is None
            and self.star is None
            and not self.dollar
            and not self.atoms
            and not self.bots
            and not self.sets
        )
        return result

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
        self.topic_root: _GraphNode | None = None
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
        self.default_trie = _GraphNode()
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
        entry = {
            "pattern": pattern,
            "response": response,
            "that": that or "",
            "topic": topic or "",
        }
        entry_id = self.internal_next_entry_id
        self.internal_next_entry_id += 1
        self.internal_entries[entry_id] = entry
        self.internal_entry_ids.setdefault((entry["pattern"], entry["that"], entry["topic"]), []).append(entry_id)
        self._index_entry(entry_id, entry)

    @staticmethod
    def _path_key(entry: dict) -> tuple[str, str, bool, str]:
        """Return the normalized topic, pattern, and that path an entry is filed under."""
        topic_key = normalize_pattern(entry["topic"]) if entry["topic"] else ""
        that_key = normalize_pattern(entry["that"]) if entry["that"] else ""
        result = (topic_key, normalize_pattern(entry["pattern"]), bool(entry["that"]), that_key)
        return result

    @staticmethod
    def _payload(entry: dict) -> dict:
        result = {
            "pattern": entry["pattern"],
            "response": entry["response"],
            "that": entry["that"],
            "topic": entry["topic"],
        }
        return result

    def _index_entry(self, entry_id: int, entry: dict) -> None:
        """File one category under its topic, pattern, and that path.

        The first category to claim a path keeps it. A later copy of the same
        path waits in the path's claim list and takes over if the first is
        removed.
        """
        key = self._path_key(entry)
        claims = self.internal_path_claims.setdefault(key, [])
        claims.append(entry_id)
        if len(claims) == 1:
            self._category_leaf(key).category = self._payload(entry)

    def _category_leaf(self, key: tuple[str, str, bool, str]) -> _GraphNode:
        """Return the node holding a path's category, creating the path if needed."""
        topic_key, pattern_key, has_that, that_key = key
        leaf = self._insert_tokens(self.default_trie, pattern_key.split())
        if has_that:
            if leaf.that_root is None:
                leaf.that_root = _GraphNode()
            leaf = self._insert_tokens(leaf.that_root, that_key.split())
        if topic_key:
            if leaf.topic_root is None:
                leaf.topic_root = _GraphNode()
            leaf = self._insert_tokens(leaf.topic_root, topic_key.split())
        return leaf

    def _unindex_entry(self, entry_id: int, entry: dict) -> None:
        """Take one entry out of the graph, handing its leaf to the next claimant."""
        key = self._path_key(entry)
        claims = self.internal_path_claims.get(key, [])
        if entry_id not in claims:
            return
        was_owner = claims[0] == entry_id
        claims.remove(entry_id)
        if claims:
            if was_owner:
                self._category_leaf(key).category = self._payload(self.internal_entries[claims[0]])
            return
        del self.internal_path_claims[key]
        self._prune_path(key)

    def _prune_path(self, key: tuple[str, str, bool, str]) -> None:
        """Clear a path's category and drop the nodes that no longer lead anywhere."""
        topic_key, pattern_key, has_that, that_key = key
        pattern_path = self._existing_path(self.default_trie, pattern_key.split())
        if pattern_path is None:
            return
        leaf = pattern_path[-1][2] if pattern_path else self.default_trie
        if has_that:
            if leaf.that_root is not None:
                that_path = self._existing_path(leaf.that_root, that_key.split())
                if that_path is not None:
                    that_leaf = that_path[-1][2] if that_path else leaf.that_root
                    self._clear_topic_category(that_leaf, topic_key)
                    self._prune_edges(that_path)
                if leaf.that_root.is_empty():
                    leaf.that_root = None
        else:
            self._clear_topic_category(leaf, topic_key)
        self._prune_edges(pattern_path)

    def _clear_topic_category(self, leaf: _GraphNode, topic_key: str) -> None:
        """Clear the category below a pattern or that leaf, through its topic segment."""
        if not topic_key:
            leaf.category = None
            return
        if leaf.topic_root is None:
            return
        topic_path = self._existing_path(leaf.topic_root, topic_key.split())
        if topic_path is not None:
            topic_leaf = topic_path[-1][2] if topic_path else leaf.topic_root
            topic_leaf.category = None
            self._prune_edges(topic_path)
        if leaf.topic_root.is_empty():
            leaf.topic_root = None

    def _existing_path(self, root: _GraphNode, words: list[str]) -> list[tuple[_GraphNode, str, _GraphNode]] | None:
        """Follow existing edges for ``words`` and return (parent, word, child) steps, or None."""
        path = []
        node = root
        for word in words:
            child = self._existing_edge(node, word)
            if child is None:
                return None
            path.append((node, word, child))
            node = child
        return path

    def _existing_edge(self, node: _GraphNode, word: str) -> _GraphNode | None:
        """Return the child for one pattern token without creating it."""
        kind, name = self._edge_kind(word)
        if kind == "underscore":
            return node.underscore
        if kind == "caret":
            return node.caret
        if kind == "hash":
            return node.hash
        if kind == "star":
            return node.star
        if kind == "dollar":
            return node.dollar.get(name)
        if kind == "bot":
            return node.bots.get(name)
        if kind == "set":
            return node.sets.get(name)
        result = node.atoms.get(word)
        return result

    def _prune_edges(self, path: list[tuple[_GraphNode, str, _GraphNode]]) -> None:
        """Detach empty nodes from the end of a path back toward its root."""
        for parent, word, child in reversed(path):
            if not child.is_empty():
                break
            self._detach(parent, word, child)

    def _detach(self, parent: _GraphNode, word: str, child: _GraphNode) -> None:
        """Remove the edge ``word`` from ``parent``, including its fallback index entries."""
        kind, name = self._edge_kind(word)
        if kind == "underscore":
            parent.underscore = None
        elif kind == "caret":
            parent.caret = None
        elif kind == "hash":
            parent.hash = None
        elif kind == "star":
            parent.star = None
        elif kind == "dollar":
            parent.dollar.pop(name, None)
            self._forget_flexible(parent.lemma_dollar, parent.stem_dollar, name, child)
        elif kind == "bot":
            parent.bots.pop(name, None)
        elif kind == "set":
            parent.sets.pop(name, None)
        else:
            parent.atoms.pop(word, None)
            self._forget_flexible(parent.lemma_atoms, parent.stem_atoms, word, child)

    @staticmethod
    def _edge_kind(word: str) -> tuple[str, str]:
        """Classify a pattern token the way ``_edge`` files it."""
        wildcards = {"_": "underscore", "^": "caret", "#": "hash", "*": "star"}
        if word in wildcards:
            result = (wildcards[word], "")
            return result
        if len(word) > 1 and word.startswith("$") and word[1:].isalnum():
            result = ("dollar", word[1:])
            return result
        if word.startswith("{bot:") and word.endswith("}") and _is_table_name(word[5:-1]):
            result = ("bot", word[5:-1])
            return result
        if word.startswith("{set:") and word.endswith("}") and _is_table_name(word[5:-1]):
            result = ("set", word[5:-1])
            return result
        result = ("atom", word)
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

    def _forget_flexible(self, lemma_index: dict, stem_index: dict, word: str, child: _GraphNode) -> None:
        """Drop a detached edge from the lemma and stem fallback indexes."""
        keys = []
        if self.internal_use_lemmatization:
            keys.append((lemma_index, self.internal_lemmatize(word)))
        if self.internal_use_stemming:
            keys.append((stem_index, stem_text(word)))
        for index, key in keys:
            bucket = index.get(key)
            if bucket and child in bucket:
                bucket.remove(child)
                if not bucket:
                    del index[key]

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
        """Rebuild the graphmaster from the current entries.

        Removal updates the graph in place, so this is only needed after the
        fallback settings change.
        """
        self.default_trie = _GraphNode()
        self.internal_path_claims = {}
        for entry_id, entry in self.internal_entries.items():
            self._index_entry(entry_id, entry)

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
        entry_ids = self.internal_entry_ids.get(key)
        if not entry_ids:
            result = False
            return result
        entry_id = entry_ids.pop(0)
        if not entry_ids:
            del self.internal_entry_ids[key]
        entry = self.internal_entries.pop(entry_id)
        self._unindex_entry(entry_id, entry)
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

        that_text = _last_sentence(that)
        that_words = prepare_pattern_text(that_text).split() if that_text else []
        topic_words = prepare_pattern_text(topic).split() if topic else []
        # Captures always come from the prepared words, including in the lemma
        # and stem walks, whose words line up with these by position.
        sources = (words, that_words, topic_words)
        result = self._match_words(words, that_words, topic_words, "exact", sources)

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
                    sources,
                )
            )

        if not result and self.internal_use_stemming:
            result = specific_pattern_result(
                self._match_words(
                    self._flexible_words(words, "stem"),
                    self._flexible_words(that_words, "stem"),
                    self._flexible_words(topic_words, "stem"),
                    "stem",
                    sources,
                )
            )

        final = result if result else catchall
        return final

    def _flexible_words(self, words: list[str], mode: str) -> list[str]:
        """Lemmatize or stem prepared words, one output word per input word.

        The whole text is converted at once so the lemmatizer sees context.
        If that changes the word count, each word is converted alone, so a
        capture's position still names the original words.
        """
        converted = self._convert(" ".join(words), mode).split()
        if len(converted) == len(words):
            return converted
        result = []
        for word in words:
            parts = self._convert(word, mode).split()
            result.append(parts[0] if len(parts) == 1 else word)
        return result

    def _convert(self, text: str, mode: str) -> str:
        result = self.internal_lemmatize(text) if mode == "lemma" else stem_text(text)
        return result

    def _match_words(
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

        def on_pattern(node: _GraphNode, stars: list[tuple[int, int]]) -> tuple:
            if node.that_root is not None:

                def on_that(that_node: _GraphNode, thatstars: list[tuple[int, int]]) -> tuple:
                    found = self._topic_category(that_node, stars, thatstars, topic_words, mode, sources)
                    return found

                matched_that = self._walk(node.that_root, that_words, 0, [], mode, on_that, set())
                if matched_that:
                    return matched_that
            found = self._topic_category(node, stars, [], topic_words, mode, sources)
            return found

        found = self._walk(self.default_trie, words, 0, [], mode, on_pattern, set())
        return found

    def _topic_category(
        self,
        node: _GraphNode,
        stars: list[tuple[int, int]],
        thatstars: list[tuple[int, int]],
        topic_words: list[str],
        mode: str,
        sources: tuple[list[str], list[str], list[str]],
    ) -> tuple:
        """Take the category below a pattern or that leaf; a matching topic beats no topic."""
        if node.topic_root is not None:

            def on_topic(topic_node: _GraphNode, topicstars: list[tuple[int, int]]) -> tuple:
                taken = self._take_category(topic_node, stars, thatstars, topicstars, sources)
                return taken

            found = self._walk(node.topic_root, topic_words, 0, [], mode, on_topic, set())
            if found:
                return found
        taken = self._take_category(node, stars, thatstars, [], sources)
        return taken

    def _take_category(
        self,
        node: _GraphNode,
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
            category["response"],
            [" ".join(words[start:end]) for start, end in stars],
            [" ".join(that_words[start:end]) for start, end in thatstars],
            [" ".join(topic_words[start:end]) for start, end in topicstars],
            category["pattern"],
            category["topic"],
            category["that"],
        )
        return result

    def _walk(
        self,
        node: _GraphNode,
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
            if node.caret is not None:
                found = self._walk(node.caret, words, pos, stars + [(pos, pos)], mode, on_leaf, failed)
                if found:
                    return found
            if node.hash is not None:
                found = self._walk(node.hash, words, pos, stars + [(pos, pos)], mode, on_leaf, failed)
                if found:
                    return found
            failed.add((node, pos))
            result = ()
            return result

        word = words[pos]
        for child in self._dollar_children(node, word, mode):
            found = self._walk(child, words, pos + 1, stars, mode, on_leaf, failed)
            if found:
                return found

        if node.underscore is not None:
            for end in range(pos + 1, len(words) + 1):
                found = self._walk(node.underscore, words, end, stars + [(pos, end)], mode, on_leaf, failed)
                if found:
                    return found

        for child in self._atom_children(node, word, mode):
            found = self._walk(child, words, pos + 1, stars, mode, on_leaf, failed)
            if found:
                return found

        for name, child in node.bots.items():
            expected = self._bound_words(_named_value(self.internal_bot_properties, name, ""), mode)
            end = self._consume_fixed(words, pos, expected)
            if end < 0:
                continue
            found = self._walk(child, words, end, stars + [(pos, end)], mode, on_leaf, failed)
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
                found = self._walk(child, words, end, stars + [(pos, end)], mode, on_leaf, failed)
                if found:
                    return found

        if node.caret is not None:
            for end in range(pos, len(words) + 1):
                found = self._walk(node.caret, words, end, stars + [(pos, end)], mode, on_leaf, failed)
                if found:
                    return found

        if node.hash is not None:
            for end in range(pos, len(words) + 1):
                found = self._walk(node.hash, words, end, stars + [(pos, end)], mode, on_leaf, failed)
                if found:
                    return found

        if node.star is not None:
            for end in range(pos + 1, len(words) + 1):
                found = self._walk(node.star, words, end, stars + [(pos, end)], mode, on_leaf, failed)
                if found:
                    return found

        failed.add((node, pos))
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
        pairs = [(p["pattern"], p["response"]) for p in self.internal_entries.values()]
        return pairs

    def get_patterns_with_context(self) -> list[tuple[str, str, str, str]]:
        """Get all pattern-response pairs with context.

        Returns:
            List of (pattern, response, that, topic) tuples.
        """
        entries = [(p["pattern"], p["response"], p["that"], p["topic"]) for p in self.internal_entries.values()]
        return entries

    def clear(self) -> None:
        """Remove all patterns."""
        self.internal_entries.clear()
        self.internal_entry_ids.clear()
        self.internal_path_claims.clear()
        self.default_trie = _GraphNode()

    def __len__(self) -> int:
        """Return number of patterns."""
        count = len(self.internal_entries)
        return count
