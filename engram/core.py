"""Core ENGRAM implementation."""

import json
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from engram.config import DEFAULT_STOPWORDS, EngramConfig, EvictionPolicy, SessionOverflow
from engram.eviction import EvictionMixin
from engram.metrics import MetricsMixin
from engram.models import KeywordEntry, QueryResult, Session, Statement, Tier
from engram.nlp import ExtractedFact, extract_fact
from engram.pattern import PatternMatcher, match_pattern
from engram.persistence import PersistenceMixin, load_engram, load_engram_from_dict, load_engram_json
from engram.sessions import SessionLimitExceeded, SessionMixin, SessionNotFound
from engram.scoring import score_statement
from engram.substitutions import (
    DEFAULT_CONTRACTIONS,
    DEFAULT_GENDER,
    DEFAULT_PERSON,
    DEFAULT_PERSON2,
    SubstitutionMaps,
    expand_contractions,
    split_sentences,
)
from engram.template import TemplateContext, TemplateProcessor
from engram.text import expand_query, extract_keywords, normalize


class Engram(MetricsMixin, SessionMixin, PersistenceMixin, EvictionMixin):
    """Keyword-indexed statement store with hit-rate tracking.

    ENGRAM stores flat statements retrieved by keyword overlap and scored by
    relevance signals. It supports multiple concurrent sessions sharing a
    common statement pool.

    Inherits metrics and coverage analysis methods from MetricsMixin.
    """

    PERSISTENCE_VERSION = 1

    def __init__(self, config=None) -> None:
        """Initialize ENGRAM instance.

        Args:
            config: Configuration options. Uses defaults if not provided.
        """
        self.config = config or EngramConfig()

        # Core data structures
        self._statements: list[Statement] = []
        self._statement_index: dict[str, int] = {}  # id -> list index
        self._keywords: dict[str, KeywordEntry] = {}
        self._sessions: dict[str, Session] = {}
        self._pattern_to_statement: dict[str, str] = {}  # pattern -> statement_id

        # Bot properties and data
        self._bot_properties: dict[str, str] = {
            "name": "ENGRAM",
            "version": "0.1.6",
        }
        self._sets: dict[str, list[str]] = {}
        self._maps: dict[str, dict[str, str]] = {}

        # Pattern matcher with access to sets and bot properties
        self._pattern_matcher = PatternMatcher(
            sets=self._sets,
            bot_properties=self._bot_properties,
        )
        self._substitution_maps = SubstitutionMaps()
        self._default_predicates: dict[str, str] = {}

        # Template processor
        self._template_processor = TemplateProcessor(
            srai_limit=self.config.srai_depth_limit
            if hasattr(self.config, "srai_depth_limit")
            else 100
        )

        # Concurrency control
        self._statement_lock = threading.RLock()
        self._keyword_lock = threading.RLock()
        self._session_lock = threading.RLock()

        # Metrics
        self._query_count = 0
        self._hit_count = 0
        self._eviction_count = 0

    # =========================================================================
    # Statement Operations
    # =========================================================================

    def store(
        self,
        text: str,
        tier: Tier = Tier.DYNAMIC,
        statement_id=None,
        pattern=None,
        that=None,
        topic=None,
        template=None,
        priority: int = 0,
    ) -> str:
        """Add a statement to the store.

        Args:
            text: Statement content (response text for plain templates).
            tier: STATIC or DYNAMIC (default).
            statement_id: Optional specific ID.
            pattern: Optional pattern for matching (AIML-style).
            that: Optional pattern for bot's previous response.
            topic: Optional topic scope.
            template: Optional structured template (JSON/dict).
            priority: Optional priority override.

        Returns:
            Assigned statement ID.
        """
        # Normalize and extract keywords from pattern if provided, else from text
        keyword_source = pattern if pattern else text
        normalized = normalize(keyword_source)
        keywords = extract_keywords(normalized, self.config.stopwords)

        # Create statement
        statement = Statement.create(
            text=text,
            tier=tier,
            keywords=keywords,
            statement_id=statement_id,
            pattern=pattern or "",
            that=that or "",
            topic=topic or "",
            template=template,
            priority=priority,
        )

        # Add to pattern matcher if pattern provided
        if pattern:
            self._pattern_matcher.add_pattern(
                pattern, text, that=that or "", topic=topic or ""
            )
            self._pattern_to_statement[pattern] = statement.id

        with self._statement_lock:
            # Check capacity for DYNAMIC statements
            if tier == Tier.DYNAMIC:
                dynamic_count = sum(1 for s in self._statements if s.tier == Tier.DYNAMIC)
                while dynamic_count >= self.config.capacity:
                    self._evict_oldest_dynamic()
                    dynamic_count -= 1

            # Add statement
            self._statement_index[statement.id] = len(self._statements)
            self._statements.append(statement)

        # Index keywords
        with self._keyword_lock:
            for kw in keywords:
                if kw not in self._keywords:
                    self._keywords[kw] = KeywordEntry(keyword=kw)
                self._keywords[kw].add_statement(statement.id)

        return statement.id

    def query(
        self,
        text: str,
        session_id=None,
        limit: int = 5,
    ) -> QueryResult:
        """Retrieve matching statements.

        Args:
            text: Query text.
            session_id: Optional session for context expansion.
            limit: Maximum results (default: 5).

        Returns:
            QueryResult with matches and extracted keywords.
        """
        self._query_count += 1

        # Get session context if provided
        expanded_text = text
        if session_id:
            with self._session_lock:
                session = self._sessions.get(session_id)
                if session:
                    session.touch()
                    expanded_text = expand_query(text, session.previous_response)

        # Normalize and extract keywords
        normalized = normalize(expanded_text)
        keywords = extract_keywords(normalized, self.config.stopwords)

        if not keywords:
            return QueryResult(matches=[], keywords=[])

        # Increment query counts
        with self._keyword_lock:
            for kw in keywords:
                if kw in self._keywords:
                    self._keywords[kw].increment_query()

        # Find candidates
        candidate_ids: set[str] = set()
        with self._keyword_lock:
            for kw in keywords:
                if kw in self._keywords:
                    candidate_ids.update(self._keywords[kw].statement_ids)

        if not candidate_ids:
            return QueryResult(matches=[], keywords=keywords)

        # Score candidates
        scored: list[tuple[Statement, float]] = []
        with self._statement_lock:
            total = len(self._statements)
            for stmt_id in candidate_ids:
                idx = self._statement_index.get(stmt_id)
                if idx is not None:
                    stmt = self._statements[idx]
                    with self._keyword_lock:
                        score = score_statement(
                            statement=stmt,
                            statement_index=idx,
                            total_statements=total,
                            query_keywords=keywords,
                            keyword_index=self._keywords,
                            weight_base=self.config.weight_base,
                            weight_recency=self.config.weight_recency,
                            weight_hit_rate=self.config.weight_hit_rate,
                        )
                    if score > 0:
                        scored.append((stmt, score))

        # Sort by score descending and limit
        scored.sort(key=lambda x: x[1], reverse=True)
        matches = scored[:limit]

        return QueryResult(matches=matches, keywords=keywords)

    def pattern_query(
        self,
        text: str,
        session_id=None,
    ) -> tuple[Statement, list[str], str]:
        """Query using AIML-style pattern matching.

        Supports multi-sentence input: sentences are split, matched independently,
        and responses are combined.

        Args:
            text: User input text (may contain multiple sentences).
            session_id: Optional session for context.

        Returns:
            Tuple of (matched_statement, captured_wildcards, response_text) or None.
            For multi-sentence input, returns first matched statement with combined response.
        """
        self._query_count += 1

        # Apply contractions expansion if enabled
        processed_text = text
        if self.config.expand_contractions:
            processed_text = expand_contractions(
                text, self._substitution_maps.contractions
            )

        # Split into sentences
        sentences = split_sentences(processed_text)
        if not sentences:
            # No sentences found, treat as single input
            sentences = [processed_text] if processed_text.strip() else []

        if not sentences:
            return None

        # Get session if provided
        session = None
        that = ""
        topic = ""
        if session_id:
            session = self.get_session(session_id, create_if_missing=True)
            if session:
                that = session.that  # Bot's last response (normalized)
                topic = session.topic  # Current topic

        # Process each sentence
        responses: list[str] = []
        first_stmt = None
        first_captured: list[str] = []

        for sentence in sentences:
            result = self._pattern_matcher.match(sentence, that=that, topic=topic)
            if result:
                response_text, captured, thatstars, topicstars, matched_pattern, matched_topic, matched_that = result

                # Try to extract and learn facts from declarative sentences
                # Do this before responding so we acknowledge learning
                fact = extract_fact(sentence)
                learned = False
                if fact:
                    learned = self.learn_fact(fact)

                # Find the statement with this pattern, topic, and that
                with self._statement_lock:
                    for stmt in self._statements:
                        if stmt.pattern == matched_pattern and stmt.topic == matched_topic and stmt.that == matched_that:
                            # If we learned a fact and matched catch-all, acknowledge instead
                            if learned and matched_pattern == "*":
                                final_response = "I see."
                            else:
                                # Process template if present
                                final_response = self._process_statement_template(
                                    stmt, captured, sentence, session,
                                    thatstars=thatstars, topicstars=topicstars
                                )
                            responses.append(final_response)

                            # Track first match for return value
                            if first_stmt is None:
                                first_stmt = stmt
                                first_captured = captured

                            # Update 'that' for next sentence (response becomes context)
                            that = final_response
                            break

        if not responses:
            return None

        # Combine responses
        combined_response = " ".join(responses)

        # Update session context with full input and combined response
        if session:
            session.update_context(combined_response, text)

        return (first_stmt, first_captured, combined_response)

    def _process_statement_template(
        self,
        stmt: Statement,
        captured: list[str],
        input_text: str,
        session,
        thatstars=None,
        topicstars=None,
    ) -> str:
        """Process a statement's template with context.

        Args:
            stmt: The matched statement.
            captured: Wildcard captures from pattern matching.
            input_text: Original user input.
            session: Optional session for context.
            thatstars: Wildcard captures from that pattern.
            topicstars: Wildcard captures from topic pattern.

        Returns:
            Processed response string.
        """
        # Build template context (even for plain text to support {bot:name} etc.)
        context = TemplateContext(
            stars=captured,
            thatstars=thatstars or [],
            topicstars=topicstars or [],
            input_text=input_text,
            request_text=input_text,
            bot=self._bot_properties,
            maps=self._maps,
            person_subs=self._substitution_maps.person,
            person2_subs=self._substitution_maps.person2,
            gender_subs=self._substitution_maps.gender,
            category_count=len(self._statements),
        )

        # Add session context
        if session:
            context.session_id = session.session_id
            context.predicates = session.predicates.copy()
            context.input_history = session.input_history.copy()
            context.response_history = session.response_history.copy()
            context.that_history = [s.copy() for s in session.that_history]

        # Set redirect callback
        def redirect_fn(pattern: str) -> str:
            result = self._pattern_matcher.match(pattern)
            if result:
                response_text, new_captured, new_thatstars, new_topicstars, matched_pattern, matched_topic, matched_that = result
                with self._statement_lock:
                    for s in self._statements:
                        if s.pattern == matched_pattern and s.topic == matched_topic and s.that == matched_that:
                            # Create new context for redirect
                            new_context = TemplateContext(
                                stars=new_captured,
                                thatstars=new_thatstars,
                                topicstars=new_topicstars,
                                input_text=pattern,
                                request_text=input_text,
                                bot=context.bot,
                                maps=context.maps,
                                person_subs=context.person_subs,
                                person2_subs=context.person2_subs,
                                gender_subs=context.gender_subs,
                                predicates=context.predicates,
                                input_history=context.input_history,
                                response_history=context.response_history,
                                that_history=context.that_history,
                                session_id=context.session_id,
                                category_count=context.category_count,
                                redirect_fn=redirect_fn,
                                learn_fn=context.learn_fn,
                            )
                            template_to_process = s.template if s.template is not None else s.text
                            return self._template_processor.process(
                                template_to_process, new_context
                            )
            return ""

        context.redirect_fn = redirect_fn

        # Set learn callback
        def learn_fn(learn_data: dict) -> None:
            pattern = learn_data.get("pattern", "")
            template = learn_data.get("template")
            if pattern:
                text = ""
                if isinstance(template, dict) and "text" in template:
                    text = template["text"]
                elif isinstance(template, str):
                    text = template
                self.store(
                    text=text,
                    pattern=pattern,
                    template=template,
                    that=learn_data.get("that", ""),
                    topic=learn_data.get("topic", ""),
                    tier=Tier.DYNAMIC,
                )

        context.learn_fn = learn_fn

        # Process template (use response text if no explicit template)
        template_to_process = stmt.template if stmt.template is not None else stmt.text
        response = self._template_processor.process(template_to_process, context)

        # Update session predicates from context
        if session:
            session.predicates.update(context.predicates)

        return response

    def record_hit(self, keywords: list[str]) -> None:
        """Update statistics after successful retrieval.

        Args:
            keywords: Query keywords that led to a hit.
        """
        self._hit_count += 1
        with self._keyword_lock:
            for kw in keywords:
                if kw in self._keywords:
                    self._keywords[kw].increment_hit()

    def learn_fact(self, fact: ExtractedFact) -> bool:
        """Learn a fact extracted from natural language.

        Creates patterns for the subject and common query forms so the fact
        can be retrieved later.

        Args:
            fact: The extracted fact to learn.

        Returns:
            True if the fact was learned, False if it was already known.
        """
        # Check if we already have a pattern for the primary subject
        subject_pattern = fact.subject_upper
        with self._statement_lock:
            for stmt in self._statements:
                if stmt.pattern == subject_pattern:
                    # Already know this - don't overwrite
                    return False

        # Store the fact with multiple retrieval patterns
        # The response is the full original sentence
        response = fact.original

        # Store primary pattern (just the subject)
        self.store(
            text=response,
            pattern=subject_pattern,
            tier=Tier.DYNAMIC,
        )

        # Store question patterns
        for pattern in fact.query_patterns[1:]:  # Skip first (already stored)
            # Check if pattern already exists
            exists = False
            with self._statement_lock:
                for stmt in self._statements:
                    if stmt.pattern == pattern:
                        exists = True
                        break
            if not exists:
                self.store(
                    text=response,
                    pattern=pattern,
                    tier=Tier.DYNAMIC,
                )

        return True

    def get_statement(self, statement_id: str) -> Statement:
        """Get a statement by ID.

        Args:
            statement_id: Statement ID.

        Returns:
            Statement if found, None otherwise.
        """
        with self._statement_lock:
            idx = self._statement_index.get(statement_id)
            if idx is not None:
                return self._statements[idx]
        return None

    # =========================================================================
    # Persistence (classmethods - delegate to module functions)
    # =========================================================================

    @classmethod
    def load(cls, path: str, config=None) -> "Engram":
        """Load state from JSON file.

        Args:
            path: File path to read.
            config: Optional configuration override.

        Returns:
            Engram instance.
        """
        return load_engram(path, config, cls)

    @classmethod
    def load_json(cls, json_str: str, config=None) -> "Engram":
        """Load state from JSON string.

        Args:
            json_str: JSON string.
            config: Optional configuration override.

        Returns:
            Engram instance.
        """
        return load_engram_json(json_str, config, cls)

    @classmethod
    def from_dict(cls, data: dict[str, Any], config=None) -> "Engram":
        """Deserialize state from dictionary.

        Args:
            data: Dictionary containing serialized state.
            config: Optional configuration override.

        Returns:
            Engram instance.
        """
        return load_engram_from_dict(data, config, cls)

    # =========================================================================
    # Initialization Patterns
    # =========================================================================

    def load_corpus(self, statements: list[str], tier: Tier = Tier.STATIC) -> int:
        """Load a corpus of statements.

        Args:
            statements: List of statement texts.
            tier: Tier for all statements.

        Returns:
            Number of statements loaded.
        """
        for text in statements:
            self.store(text, tier=tier)
        return len(statements)

    @classmethod
    def fork(
        cls,
        parent: "Engram",
        static_corpus=None,
        config=None,
    ) -> "Engram":
        """Create a new ENGRAM forked from a parent.

        Args:
            parent: Parent ENGRAM to fork from.
            static_corpus: Optional new static statements.
            config: Optional configuration override.

        Returns:
            New Engram instance.
        """
        instance = cls(config=config or parent.config)

        # Load static corpus if provided
        if static_corpus:
            instance.load_corpus(static_corpus, tier=Tier.STATIC)

        # Copy parent's DYNAMIC statements
        with parent._statement_lock:
            for stmt in parent._statements:
                if stmt.tier == Tier.DYNAMIC:
                    instance.store(stmt.text, tier=Tier.DYNAMIC)

        # Fresh session registry (no inheritance)
        return instance

    # =========================================================================
    # Sets and Bot Properties
    # =========================================================================

    def add_set(self, name: str, words: list[str]) -> None:
        """Add or update a named word set.

        Args:
            name: Set name (used in patterns as {set:name}).
            words: List of words in the set.
        """
        self._sets[name] = words

    def get_set(self, name: str) -> list[str]:
        """Get a named word set.

        Args:
            name: Set name.

        Returns:
            List of words or None if not found.
        """
        return self._sets.get(name)

    def remove_set(self, name: str) -> bool:
        """Remove a named word set.

        Args:
            name: Set name to remove.

        Returns:
            True if removed, False if not found.
        """
        if name in self._sets:
            del self._sets[name]
            return True
        return False

    def list_sets(self) -> list[str]:
        """List all set names.

        Returns:
            List of set names.
        """
        return list(self._sets.keys())

    def set_bot_property(self, name: str, value: str) -> None:
        """Set a bot property.

        Args:
            name: Property name (used in patterns as {bot:name}).
            value: Property value.
        """
        self._bot_properties[name] = value

    def get_bot_property(self, name: str) -> str:
        """Get a bot property.

        Args:
            name: Property name.

        Returns:
            Property value or None if not found.
        """
        return self._bot_properties.get(name)

    def get_bot_properties(self) -> dict[str, str]:
        """Get all bot properties.

        Returns:
            Dictionary of bot properties.
        """
        return self._bot_properties.copy()
