"""Core ENGRAM implementation."""

import threading

from engram.config import EngramConfig
from engram import eviction as eviction_mod
from engram import sessions as sessions_mod
from engram.graph import GraphClient, GraphResult, create_graph_client
from engram.models import (
    KeywordEntry,
    QueryResult,
    Statement,
    Tier,
    session_touch,
    session_update_context,
)
from engram.nlp import (
    extract_fact,
    extract_entities,
    fact_query_patterns,
    fact_subject_upper,
)
from engram.pattern import PatternMatcher
from engram.scoring import score_statement
from engram.substitutions import SubstitutionMaps, expand_contractions, split_sentences
from engram.template import TemplateContext, TemplateProcessor
from engram.text import expand_query, expand_with_synonyms, extract_keywords, extract_keywords_spacy, normalize

# Re-export for backward compatibility
from engram.sessions import SessionLimitExceeded, SessionNotFound


class Engram:
    """Keyword-indexed statement store with hit-rate tracking.

    ENGRAM stores flat statements retrieved by keyword overlap and scored by
    relevance signals. It supports multiple concurrent sessions sharing a
    common statement pool.
    """

    def __init__(self, config=None) -> None:
        """Initialize ENGRAM instance.

        Args:
            config: Configuration options. Uses defaults if not provided.
        """
        self.config = config or EngramConfig()

        # Core data structures
        self.statements: list[dict] = []
        self.statement_index: dict[str, int] = {}  # id -> list index
        self.keywords: dict[str, dict] = {}
        self.sessions: dict[str, dict] = {}
        self.pattern_to_statement: dict[str, str] = {}  # pattern -> statement_id

        # Bot properties and data (public for direct access)
        self.bot_properties: dict[str, str] = {
            "name": "ENGRAM",
            "version": "0.1.6",
        }
        self.sets: dict[str, list[str]] = {}
        self.maps: dict[str, dict[str, str]] = {}

        # Pattern matcher with access to sets and bot properties
        self.pattern_matcher = PatternMatcher(
            sets=self.sets,
            bot_properties=self.bot_properties,
            use_stemming=self.config["use_stemming"],
            use_lemmatization=self.config["use_lemmatization"],
            use_spacy_lemmatization=self.config["use_spacy_lemmatization"],
        )
        self.substitution_maps = SubstitutionMaps()
        self.default_predicates: dict[str, str] = {}

        # Template processor
        self.template_processor = TemplateProcessor(srai_limit=self.config.get("srai_depth_limit", 100))

        # Concurrency control
        self.statement_lock = threading.RLock()
        self.keyword_lock = threading.RLock()
        self.session_lock = threading.RLock()

        # Metrics
        self.query_count = 0
        self.hit_count = 0
        self.eviction_count = 0

        # Graph client (lazy initialization)
        self._graph_client = None

    @property
    def graph_client(self):
        """Get the graph client, initializing if needed."""
        if self._graph_client is None and self.config["graph"]:
            graph_config = self.config["graph"]
            if isinstance(graph_config, dict) and graph_config["enabled"]:
                self._graph_client = create_graph_client(
                    driver=graph_config["driver"],
                    uri=graph_config["uri"],
                    username=graph_config["username"],
                    password=graph_config["password"],
                    database=graph_config["database"],
                )
        return self._graph_client

    def graph_query(self, cypher: str, params=None) -> dict:
        """Execute a Cypher query against the knowledge graph.

        Args:
            cypher: Cypher query string.
            params: Optional query parameters.

        Returns:
            GraphResult with success status and records.
        """
        client = self.graph_client
        if client is None:
            return GraphResult(success=False, records=[], error="Graph not configured")
        return client.execute(cypher, params)

    def graph_lookup(self, text: str) -> str:
        """Look up information in the knowledge graph based on input text.

        Extracts entities from the text and queries the graph for related
        information. Returns a natural language response if found.

        Args:
            text: User input text.

        Returns:
            Response string if graph has relevant info, None otherwise.
        """
        client = self.graph_client
        if client is None:
            return ""

        # Extract entities from the input
        entities = extract_entities(text)
        if not entities:
            # Try keyword-based lookup
            keywords = extract_keywords(normalize(text), self.config["stopwords"])
            if not keywords:
                return ""
            # Query for any node matching keywords
            for kw in keywords[:3]:  # Limit to top 3 keywords
                result = client.execute(
                    "MATCH (n) WHERE toLower(n.name) CONTAINS toLower($keyword) "
                    "RETURN n.name as name, labels(n) as labels, properties(n) as props LIMIT 3",
                    {"keyword": kw},
                )
                if result["success"] and result["records"]:
                    # Format response from graph data in natural language
                    responses = []
                    for record in result["records"]:
                        name = record.get("name", "")
                        props = record.get("props", {})
                        if name and props:
                            # Build human-readable property descriptions
                            prop_parts = []
                            for k, v in props.items():
                                if k != "name" and v:
                                    # Convert property names to readable format
                                    readable_key = k.replace("_", " ")
                                    prop_parts.append(f"its {readable_key} is {v}")
                            if prop_parts:
                                responses.append(f"{name}: {', '.join(prop_parts)}")
                    if responses:
                        return " ".join(responses)
            return ""

        # Query graph for each entity
        facts = []
        for entity in entities:
            # Query for relationships involving this entity
            result = client.execute(
                "MATCH (n)-[r]->(m) WHERE toLower(n.name) = toLower($name) "
                "RETURN n.name as subject, type(r) as relation, m.name as object LIMIT 5",
                {"name": entity["text"]},
            )
            if result["success"] and result["records"]:
                for record in result["records"]:
                    subj = record.get("subject", "")
                    rel = record.get("relation", "").replace("_", " ").lower()
                    obj = record.get("object", "")
                    if subj and rel and obj:
                        facts.append((subj, rel, obj))

            # Also try reverse relationships
            result = client.execute(
                "MATCH (n)<-[r]-(m) WHERE toLower(n.name) = toLower($name) "
                "RETURN m.name as subject, type(r) as relation, n.name as object LIMIT 5",
                {"name": entity["text"]},
            )
            if result["success"] and result["records"]:
                for record in result["records"]:
                    subj = record.get("subject", "")
                    rel = record.get("relation", "").replace("_", " ").lower()
                    obj = record.get("object", "")
                    if subj and rel and obj:
                        facts.append((subj, rel, obj))

        if facts:
            # Format facts as natural language
            return self._format_graph_facts(facts)
        return ""

    def _format_graph_facts(self, facts: list[tuple[str, str, str]]) -> str:
        """Format graph facts as natural human-readable text.

        Args:
            facts: List of (subject, relation, object) tuples.

        Returns:
            Natural language string.
        """
        if not facts:
            return ""

        # Group facts by subject for more natural responses
        by_subject: dict[str, list[tuple[str, str]]] = {}
        for subj, rel, obj in facts:
            if subj not in by_subject:
                by_subject[subj] = []
            by_subject[subj].append((rel, obj))

        sentences = []
        for subj, relations in by_subject.items():
            if len(relations) == 1:
                rel, obj = relations[0]
                sentences.append(f"{subj} {rel} {obj}")
            else:
                # Combine multiple facts about same subject
                parts = [f"{rel} {obj}" for rel, obj in relations]
                if len(parts) == 2:
                    sentences.append(f"{subj} {parts[0]} and {parts[1]}")
                else:
                    last = parts.pop()
                    sentences.append(f"{subj} {', '.join(parts)}, and {last}")

        return ". ".join(sentences) + "."

    def learn_from_response(
        self,
        query: str,
        response: str,
        tier: Tier = Tier.DYNAMIC,
    ) -> str:
        """Learn from an LLM response by storing it for future retrieval.

        This is the primary mechanism for ENGRAM to grow its knowledge base.
        When the high-cost LLM provides a response, call this method to cache
        it for future similar queries.

        Args:
            query: The original user query.
            response: The LLM's response to cache.
            tier: Storage tier (default DYNAMIC for evictable).

        Returns:
            Statement ID of the stored response.
        """
        # Normalize the query into a pattern
        normalized = normalize(query)

        # Create a pattern that will match similar queries
        # Use the full normalized query as the pattern with wildcards for flexibility
        words = normalized.split()
        if len(words) > 5:
            # For longer queries, create a more flexible pattern
            # Keep first few significant words + wildcard
            pattern = " ".join(words[:4]) + " *"
        else:
            # For shorter queries, use exact match
            pattern = normalized.upper()

        return self.store(
            text=response,
            pattern=pattern,
            tier=tier,
        )

    # =========================================================================
    # Statement Operations
    # =========================================================================

    def _extract_keywords(self, normalized_text: str) -> list:
        """Extract keywords using the configured extractor (token or phrase).

        Both store-time indexing and query-time retrieval go through here so the
        keyword index and queries always use the same extraction.
        """
        if self.config["use_phrase_keywords"]:
            return extract_keywords_spacy(normalized_text, self.config["stopwords"])
        return extract_keywords(normalized_text, self.config["stopwords"])

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
        keywords = self._extract_keywords(normalized)

        # Create statement
        statement = Statement(
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
            self.pattern_matcher.add_pattern(pattern, text, that=that or "", topic=topic or "")
            self.pattern_to_statement[pattern] = statement["id"]

        with self.statement_lock:
            # Check capacity for DYNAMIC statements
            if tier == Tier.DYNAMIC:
                dynamic_count = sum(1 for s in self.statements if s["tier"] == Tier.DYNAMIC)
                while dynamic_count >= self.config["capacity"]:
                    eviction_mod.evict_dynamic(self)
                    dynamic_count -= 1

            # Add statement
            self.statement_index[statement["id"]] = len(self.statements)
            self.statements.append(statement)

        # Index keywords
        with self.keyword_lock:
            for kw in keywords:
                if kw not in self.keywords:
                    self.keywords[kw] = KeywordEntry(keyword=kw)
                self.keywords[kw]["statement_ids"].add(statement["id"])

        return statement["id"]

    def query(
        self,
        text: str,
        session_id=None,
        limit: int = 5,
    ) -> dict:
        """Retrieve matching statements.

        Args:
            text: Query text.
            session_id: Optional session for context expansion.
            limit: Maximum results (default: 5).

        Returns:
            QueryResult with matches and extracted keywords.
        """
        self.query_count += 1

        # Get session context if provided
        expanded_text = text
        if session_id:
            with self.session_lock:
                session = self.sessions.get(session_id)
                if session:
                    session_touch(session)
                    expanded_text = expand_query(text, session["previous_response"])

        # Normalize and extract keywords
        normalized = normalize(expanded_text)
        keywords = self._extract_keywords(normalized)

        if not keywords:
            return QueryResult(matches=[], keywords=[])

        # Expand keywords with synonyms if enabled
        search_keywords = keywords
        if self.config["use_synonyms"]:
            search_keywords = expand_with_synonyms(
                keywords,
                max_synonyms_per_word=self.config["max_synonyms_per_word"],
            )

        # Increment query counts for original keywords only
        with self.keyword_lock:
            for kw in keywords:
                if kw in self.keywords:
                    self.keywords[kw]["query_count"] += 1

        # Find candidates using expanded keywords
        candidate_ids: set[str] = set()
        with self.keyword_lock:
            for kw in search_keywords:
                if kw in self.keywords:
                    candidate_ids.update(self.keywords[kw]["statement_ids"])

        if not candidate_ids:
            return QueryResult(matches=[], keywords=keywords)

        # Score candidates
        scored: list[tuple[dict, float]] = []
        with self.statement_lock:
            total = len(self.statements)
            for stmt_id in candidate_ids:
                idx = self.statement_index.get(stmt_id)
                if idx is not None:
                    stmt = self.statements[idx]
                    with self.keyword_lock:
                        score = score_statement(
                            statement=stmt,
                            statement_index=idx,
                            total_statements=total,
                            query_keywords=keywords,
                            keyword_index=self.keywords,
                            weight_base=self.config["weight_base"],
                            weight_recency=self.config["weight_recency"],
                            weight_hit_rate=self.config["weight_hit_rate"],
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
    ) -> tuple:
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
        self.query_count += 1

        # Apply contractions expansion if enabled
        processed_text = text
        if self.config["expand_contractions"]:
            processed_text = expand_contractions(text, self.substitution_maps["contractions"])

        # Split into sentences
        sentences = split_sentences(processed_text)
        if not sentences:
            # No sentences found, treat as single input
            sentences = [processed_text] if processed_text.strip() else []

        if not sentences:
            return ()

        # Get session if provided
        session = None
        that = ""
        topic = ""
        if session_id:
            session = sessions_mod.get_session(self, session_id, create_if_missing=True)
            if session:
                that = session["previous_response"]  # Bot's last response (normalized)
                topic = session["predicates"].get("topic", "")  # Current topic

        # Process each sentence
        responses: list[str] = []
        first_stmt = None
        first_captured: list[str] = []

        for sentence in sentences:
            result = self.pattern_matcher.match(sentence, that=that, topic=topic)
            if result:
                response_text, captured, thatstars, topicstars, matched_pattern, matched_topic, matched_that = result

                # Try to extract and learn facts from declarative sentences
                # Do this before responding so we acknowledge learning
                fact = extract_fact(sentence)
                learned = False
                if fact:
                    learned = self.learn_fact(fact)

                # Find the statement with this pattern, topic, and that
                with self.statement_lock:
                    for stmt in self.statements:
                        if stmt["pattern"] == matched_pattern and stmt["topic"] == matched_topic and stmt["that"] == matched_that:
                            # If we learned a fact and matched catch-all, acknowledge instead
                            if learned and matched_pattern == "*":
                                final_response = "I see."
                            else:
                                # Process template if present
                                final_response = self._process_statement_template(
                                    stmt, captured, sentence, session, thatstars=thatstars, topicstars=topicstars
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
            # Try graph lookup before falling back
            graph_response = self.graph_lookup(text)
            if graph_response:
                if session:
                    session_update_context(session, graph_response, text)
                return (None, [], graph_response)

            # Use fallback response if configured
            if self.config["fallback_response"]:
                if session:
                    session_update_context(session, self.config["fallback_response"], text)
                return (None, [], self.config["fallback_response"])
            return ()

        # Combine responses
        combined_response = " ".join(responses)

        # Update session context with full input and combined response
        if session:
            session_update_context(session, combined_response, text)

        return (first_stmt, first_captured, combined_response)

    def _process_statement_template(
        self,
        stmt: dict,
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
            bot=self.bot_properties,
            maps=self.maps,
            person_subs=self.substitution_maps["person"],
            person2_subs=self.substitution_maps["person2"],
            gender_subs=self.substitution_maps["gender"],
            category_count=len(self.statements),
        )

        # Add session context
        if session:
            context["session_id"] = session["session_id"]
            context["predicates"] = session["predicates"].copy()
            context["input_history"] = session["input_history"].copy()
            context["response_history"] = session["response_history"].copy()
            context["that_history"] = [s.copy() for s in session["that_history"]]

        # Set redirect callback
        def redirect_fn(pattern: str) -> str:
            result = self.pattern_matcher.match(pattern)
            if result:
                response_text, new_captured, new_thatstars, new_topicstars, matched_pattern, matched_topic, matched_that = result
                with self.statement_lock:
                    for s in self.statements:
                        if s["pattern"] == matched_pattern and s["topic"] == matched_topic and s["that"] == matched_that:
                            # Create new context for redirect
                            new_context = TemplateContext(
                                stars=new_captured,
                                thatstars=new_thatstars,
                                topicstars=new_topicstars,
                                input_text=pattern,
                                request_text=input_text,
                                bot=context["bot"],
                                maps=context["maps"],
                                person_subs=context["person_subs"],
                                person2_subs=context["person2_subs"],
                                gender_subs=context["gender_subs"],
                                predicates=context["predicates"],
                                input_history=context["input_history"],
                                response_history=context["response_history"],
                                that_history=context["that_history"],
                                session_id=context["session_id"],
                                category_count=context["category_count"],
                                redirect_fn=redirect_fn,
                                learn_fn=context["learn_fn"],
                            )
                            template_to_process = s["template"] or s["text"]
                            return self.template_processor.process(template_to_process, new_context)
            return ""

        context["redirect_fn"] = redirect_fn

        # Set learn callback
        def learn_fn(learn_data: dict) -> None:
            pattern = learn_data.get("pattern", "")
            template = learn_data["template"]
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

        context["learn_fn"] = learn_fn

        # Process template (use response text if no explicit template)
        template_to_process = stmt["template"] or stmt["text"]
        response = self.template_processor.process(template_to_process, context)

        # Update session predicates from context
        if session:
            session["predicates"].update(context["predicates"])

        return response

    def record_hit(self, keywords: list[str]) -> None:
        """Update statistics after successful retrieval.

        Args:
            keywords: Query keywords that led to a hit.
        """
        self.hit_count += 1
        with self.keyword_lock:
            for kw in keywords:
                if kw in self.keywords:
                    self.keywords[kw]["hit_count"] += 1

    def learn_fact(self, fact: dict) -> bool:
        """Learn a fact extracted from natural language.

        Creates patterns for the subject and common query forms so the fact
        can be retrieved later.

        Args:
            fact: The extracted fact to learn.

        Returns:
            True if the fact was learned, False if it was already known.
        """
        # Check if we already have a pattern for the primary subject
        subject_pattern = fact_subject_upper(fact)
        with self.statement_lock:
            for stmt in self.statements:
                if stmt["pattern"] == subject_pattern:
                    # Already know this - don't overwrite
                    return False

        # Store the fact with multiple retrieval patterns
        # The response is the full original sentence
        response = fact["original"]

        # Store primary pattern (just the subject)
        self.store(
            text=response,
            pattern=subject_pattern,
            tier=Tier.DYNAMIC,
        )

        # Store question patterns
        for pattern in fact_query_patterns(fact)[1:]:  # Skip first (already stored)
            # Check if pattern already exists
            exists = False
            with self.statement_lock:
                for stmt in self.statements:
                    if stmt["pattern"] == pattern:
                        exists = True
                        break
            if not exists:
                self.store(
                    text=response,
                    pattern=pattern,
                    tier=Tier.DYNAMIC,
                )

        return True

    def get_statement(self, statement_id: str) -> dict:
        """Get a statement by ID.

        Args:
            statement_id: Statement ID.

        Returns:
            Statement if found, None otherwise.
        """
        with self.statement_lock:
            idx = self.statement_index.get(statement_id)
            if idx is not None:
                return self.statements[idx]
        return {}

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
        with parent.statement_lock:
            for stmt in parent.statements:
                if stmt["tier"] == Tier.DYNAMIC:
                    instance.store(stmt["text"], tier=Tier.DYNAMIC)

        # Fresh session registry (no inheritance)
        return instance
