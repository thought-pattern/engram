"""Core ENGRAM implementation."""

import logging
import random
import threading
from difflib import SequenceMatcher

from . import eviction as eviction_mod
from . import sessions as sessions_mod
from .config import engram_config
from .constants import (
    CONFLICTING_FACT_RESPONSES,
    KIND_STATEMENT,
    KNOWN_FACT_RESPONSES,
    LEARNED_ACKNOWLEDGMENTS,
    REPETITION_ESCAPE_RESPONSE,
    REPETITION_FEEDBACK_MARKERS,
    RESPONSE_SIMILARITY_THRESHOLD,
    VERSION,
    WILDCARD_TOKENS,
    Tier,
)
from .facts_spacy import extract_facts
from .graph import create_graph_client, is_write_cypher
from .models import (
    keyword_entry,
    query_result,
    record_statement_hit,
    record_statement_query,
    session_touch,
    session_update_context,
    statement,
)
from .nlp import extract_entities, extract_fact, fact_query_patterns, fact_subject_upper, input_kind
from .pattern import PatternMatcher, is_pure_wildcard
from .phrasing import phrase_facts
from .polish import polish_response
from .scoring import score_statement
from .substitutions import expand_contractions, split_sentences, substitution_maps
from .template import TemplateProcessor, template_context
from .text import (
    correct_spelling,
    expand_query,
    expand_with_synonyms,
    extract_keywords,
    extract_keywords_spacy,
    get_synonyms,
    normalize,
    restore_capture_case,
)

logger = logging.getLogger(__name__)


def _reports_repetition(text: str) -> bool:
    normalized_text = normalize(text)
    return any(marker in normalized_text for marker in REPETITION_FEEDBACK_MARKERS)


def _response_repeats(candidate: str, recent_responses: list[str]) -> bool:
    normalized_candidate = normalize(candidate)
    if not normalized_candidate:
        return False
    for recent in recent_responses:
        normalized_recent = normalize(recent)
        if (
            normalized_recent
            and SequenceMatcher(None, normalized_candidate, normalized_recent).ratio() >= RESPONSE_SIMILARITY_THRESHOLD
        ):
            return True
    return False


def _pattern_has_wildcard(pattern: str) -> bool:
    return any(word.lstrip("$") in WILDCARD_TOKENS for word in pattern.split())


# Canonical-graph recall queries. Claims link to canonical Entity/Predicate
# nodes by edge; the surface triple is read from the denormalized projection on
# the Claim node (subject/predicate/object), never matched on. See schema.cypher.
GRAPH_ENTITY_FACTS_QUERY = (
    "MATCH (c:Claim)-[rel:HAS_SUBJECT|HAS_OBJECT]->(e:Entity) "
    "WHERE (toLower(e.primary_label) = toLower($name) "
    "OR toLower($name) IN [a IN e.aliases | toLower(a)] "
    "OR toLower(rel.surface_form) = toLower($name)) "
    "AND c.invalidated_at IS NULL "
    "RETURN DISTINCT c.subject AS subject, c.predicate AS predicate, c.object AS object "
    "LIMIT 5"
)
GRAPH_KEYWORD_FACTS_QUERY = (
    "MATCH (c:Claim)-[:HAS_SUBJECT]->(e:Entity) "
    "WHERE toLower(e.primary_label) CONTAINS toLower($keyword) "
    "AND c.invalidated_at IS NULL "
    "RETURN DISTINCT c.subject AS subject, c.predicate AS predicate, c.object AS object "
    "LIMIT 3"
)


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
        self.config = config or engram_config()

        # Core data structures
        self.statements: list[dict] = []
        self.statement_index: dict[str, int] = {}  # id -> list index
        self.keywords: dict[str, dict] = {}
        self.sessions: dict[str, dict] = {}
        self.pattern_to_statement: dict[str, str] = {}  # pattern -> statement_id

        # Bot properties and data (public for direct access)
        self.bot_properties: dict[str, str] = {
            "name": "ENGRAM",
            "version": VERSION,
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
        self.substitution_maps = substitution_maps()
        self.default_predicates: dict[str, str] = {}

        # Template processor
        self.template_processor = TemplateProcessor(srai_limit=self.config.get("srai_depth_limit", 100))

        # Concurrency control. statement_lock also guards the pattern matcher
        # and pattern_to_statement map (mutated on store/evict, read on match),
        # and thereby the shared template processor, whose recursion counters
        # are only touched while the pattern pipeline holds statement_lock.
        # count_lock guards the top-level metrics counters.
        self.statement_lock = threading.RLock()
        self.keyword_lock = threading.RLock()
        self.session_lock = threading.RLock()
        self.count_lock = threading.Lock()

        # Metrics
        self.query_count = 0
        self.hit_count = 0
        self.eviction_count = 0

        # Graph client (lazy initialization)
        self._graph_client = None
        self._graph_client_lock = threading.Lock()

    @property
    def graph_client(self):
        """Get the graph client, initializing if needed."""
        if self._graph_client is None and self.config["graph"]:
            with self._graph_client_lock:
                graph_config = self.config["graph"]
                if self._graph_client is None and isinstance(graph_config, dict) and graph_config["enabled"]:
                    self._graph_client = create_graph_client(
                        host=graph_config["host"],
                        port=graph_config["port"],
                        username=graph_config["username"],
                        password=graph_config.get("password", ""),
                    )
        return self._graph_client

    def graph_query(self, cypher: str, params=None) -> list:
        """Execute a read-only Cypher query against the knowledge graph.

        Args:
            cypher: Cypher query string.
            params: Optional query parameters.

        Returns:
            List of row dicts. Empty list when the graph is not configured,
            unreachable, or the query fails -- recall degrades gracefully rather
            than raising into the template/response path.
        """
        if is_write_cypher(cypher):
            raise ValueError("ENGRAM graph access is read-only")

        client = self.graph_client
        if client is None:
            return []
        try:
            records = client.execute_read(cypher, params)
            return records
        except RuntimeError as err:
            logger.debug("Graph query failed: %s", err)
            return []

    def graph_read_fn(self, cypher: str, params=None) -> list:
        """Read-only graph callback for template operations.

        Both this callback and the underlying connection reject mutating
        Cypher. The duplicated boundary keeps custom graph clients read-only.
        """
        return self.graph_query(cypher, params)

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
            # Keyword fallback: match a canonical Entity whose primary label
            # contains a query keyword, then return the surface triples of the
            # claims it is the subject of.
            keywords = extract_keywords(normalize(text), self.config["stopwords"])
            if not keywords:
                return ""
            facts = []
            for kw in keywords[:3]:  # Limit to top 3 keywords
                records = self.graph_query(GRAPH_KEYWORD_FACTS_QUERY, {"keyword": kw})
                facts.extend(self._records_to_facts(records))
            if facts:
                formatted = self._format_graph_facts(facts)
                return formatted
            return ""

        # Query the canonical graph for each entity. The Claim node carries the
        # rendered subject/predicate/object projection, so one query covers the
        # entity in either the subject or object role.
        facts = []
        for entity in entities:
            records = self.graph_query(GRAPH_ENTITY_FACTS_QUERY, {"name": entity["text"]})
            facts.extend(self._records_to_facts(records))

        if facts:
            # Format facts as natural language
            formatted = self._format_graph_facts(facts)
            return formatted
        return ""

    def _records_to_facts(self, records: list) -> list:
        """Turn canonical claim-projection rows into (subject, predicate, object) tuples."""
        facts = []
        for record in records:
            subj = record.get("subject", "")
            pred = record.get("predicate", "")
            obj = record.get("object", "")
            if subj and pred and obj:
                facts.append((subj, pred, obj))
        return facts

    def _format_graph_facts(self, facts: list[tuple[str, str, str]]) -> str:
        """Phrase graph facts as friendly natural-language sentences.

        Delegates to `phrasing.phrase_facts`, which picks a grammatical
        frame per predicate (copula / passive / possessive / active) from
        spaCy morphology, so a recalled triple reads as a sentence rather
        than the wooden `subject slug object` projection.

        Args:
            facts: List of (subject, predicate, object) tuples.

        Returns:
            Natural language string ("" when there are no facts).
        """
        return phrase_facts(facts)

    def learn_from_response(
        self,
        query: str,
        response: str,
        tier: Tier = Tier.DYNAMIC,
        template=None,
        introduced_by_user_id: str | None = None,
        source_label: str = "",
    ) -> str:
        """Learn from an LLM response by storing it for future retrieval.

        This is the primary mechanism for ENGRAM to grow its knowledge base.
        When the high-cost LLM provides a response, call this method to cache
        it for future similar queries.

        The response is indexed under the query's keywords (not its own), so
        future phrasings of the same question retrieve it through the keyword
        path -- no brittle prefix pattern is generated. The query gets the
        same spelling correction retrieval applies, so a typo'd learn and a
        clean retrieval key the same entry. Re-learning a query with the same
        keyword set replaces the cached response in place and resets its hit
        statistics, since the new content is unproven.

        Args:
            query: The original user query.
            response: The LLM's response to cache.
            tier: Storage tier (default DYNAMIC for evictable).
            template: Optional structured metadata to carry on the stored entry
                (an opaque dict Engram does not interpret), for callers that attach
                their own provenance to a cached conclusion.

        Returns:
            Statement ID of the stored (or updated) response.
        """
        if introduced_by_user_id is not None and not isinstance(introduced_by_user_id, str):
            raise ValueError("introduced_by_user_id must be a string or None")
        if not isinstance(source_label, str):
            raise ValueError("source_label must be a string")

        normalized = normalize(query)
        if self.config["use_spell_correction"]:
            with self.keyword_lock:
                vocabulary = set(self.keywords)
            normalized = correct_spelling(normalized, vocabulary)
        keywords = self._extract_keywords(normalized)
        keyword_set = set(keywords)

        # Dedup: a previously learned entry for the same keyword set is the
        # same cached question -- update it instead of accumulating duplicates.
        if keyword_set:
            with self.statement_lock, self.keyword_lock:
                candidate_ids: set[str] = set()
                for kw in keywords:
                    if kw in self.keywords:
                        candidate_ids.update(self.keywords[kw]["statement_ids"])
                for stmt_id in candidate_ids:
                    idx = self.statement_index.get(stmt_id)
                    if idx is None:
                        continue
                    stmt = self.statements[idx]
                    if stmt["tier"] == Tier.DYNAMIC and not stmt["pattern"] and set(stmt["keywords"]) == keyword_set:
                        stmt["text"] = response
                        stmt["template"] = template or {}
                        stmt["introduced_by_user_id"] = introduced_by_user_id
                        stmt["source_label"] = source_label
                        stmt["hit_count"] = 0
                        stmt["query_count"] = 0
                        stmt["last_hit"] = ""
                        return stmt["id"]

        stmt_id = self.store(
            text=response,
            tier=tier,
            template=template,
            keyword_source=normalized,
            introduced_by_user_id=introduced_by_user_id,
            source_label=source_label,
        )
        return stmt_id

    # =========================================================================
    # Statement Operations
    # =========================================================================

    def _extract_keywords(self, normalized_text: str) -> list:
        """Extract keywords using the configured extractor (token or phrase).

        Both store-time indexing and query-time retrieval go through here so the
        keyword index and queries always use the same extraction.
        """
        if self.config["use_phrase_keywords"]:
            phrase_keywords = extract_keywords_spacy(normalized_text, self.config["stopwords"])
            return phrase_keywords
        token_keywords = extract_keywords(normalized_text, self.config["stopwords"])
        return token_keywords

    def store(
        self,
        text: str,
        tier: Tier = Tier.DYNAMIC,
        statement_id=None,
        pattern=None,
        pattern_aliases=None,
        that=None,
        topic=None,
        template=None,
        priority: int = 0,
        keyword_source: str = "",
        introduced_by_user_id: str | None = None,
        source_label: str = "",
    ) -> str:
        """Add a statement to the store.

        Args:
            text: Statement content (response text for plain templates).
            tier: STATIC or DYNAMIC (default).
            statement_id: Optional specific ID.
            pattern: Optional pattern for matching (AIML-style).
            pattern_aliases: Optional alternate patterns that resolve to the
                same stored statement without duplicating its content.
            that: Optional pattern for bot's previous response.
            topic: Optional topic scope.
            template: Optional structured template (JSON/dict).
            priority: Optional priority override (added to the calibrated
                keyword score; preferred among equal pattern matches).
            keyword_source: Optional text to index the statement under instead
                of the pattern/text -- e.g. the question a cached response
                answers, so the answer is retrieved by the question's terms.

        Returns:
            Assigned statement ID.
        """
        if introduced_by_user_id is not None and not isinstance(introduced_by_user_id, str):
            raise ValueError("introduced_by_user_id must be a string or None")
        if not isinstance(source_label, str):
            raise ValueError("source_label must be a string")
        if pattern_aliases is None:
            aliases: list[str] = []
        elif isinstance(pattern_aliases, list | tuple) and all(isinstance(alias, str) for alias in pattern_aliases):
            aliases = list(dict.fromkeys(alias.strip() for alias in pattern_aliases if alias.strip()))
        else:
            raise ValueError("pattern_aliases must be a sequence of strings or None")
        if aliases and not pattern:
            raise ValueError("pattern_aliases require a primary pattern")
        aliases = [alias for alias in aliases if alias != pattern]

        # Index under keyword_source when given, else the pattern, else the text
        source = keyword_source or pattern or text
        normalized = normalize(source)
        keywords = self._extract_keywords(normalized)

        # Create statement
        stmt = statement(
            text=text,
            tier=tier,
            keywords=keywords,
            statement_id=statement_id,
            pattern=pattern or "",
            pattern_aliases=aliases,
            that=that or "",
            topic=topic or "",
            template=template,
            priority=priority,
            introduced_by_user_id=introduced_by_user_id,
            source_label=source_label,
        )

        with self.statement_lock:
            if stmt["id"] in self.statement_index:
                raise ValueError(f"duplicate statement id: {stmt['id']}")

            # Register the pattern under the same lock that guards matching,
            # so a concurrent pattern_query never sees a half-updated matcher.
            if pattern:
                for registered_pattern in [pattern, *aliases]:
                    self.pattern_matcher.add_pattern(registered_pattern, text, that=that or "", topic=topic or "")
                    self.pattern_to_statement[registered_pattern] = stmt["id"]

            # Check capacity for DYNAMIC statements
            if tier == Tier.DYNAMIC:
                dynamic_count = sum(1 for s in self.statements if s["tier"] == Tier.DYNAMIC)
                while dynamic_count >= self.config["capacity"]:
                    if not eviction_mod.evict_dynamic(self):
                        # Every remaining DYNAMIC statement is protected by
                        # min_hit_rate; admit the new statement over capacity
                        # rather than drop it silently.
                        break
                    dynamic_count -= 1

            # Add statement
            self.statement_index[stmt["id"]] = len(self.statements)
            self.statements.append(stmt)

        # Index keywords
        with self.keyword_lock:
            for kw in keywords:
                if kw not in self.keywords:
                    self.keywords[kw] = keyword_entry(keyword=kw)
                self.keywords[kw]["statement_ids"].add(stmt["id"])

        return stmt["id"]

    def query(
        self,
        text: str,
        session_id=None,
        limit: int = 5,
        user_id: str | None = None,
    ) -> dict:
        """Retrieve matching statements.

        Args:
            text: Query text.
            session_id: Optional legacy session label for context expansion.
            limit: Maximum results (default: 5).
            user_id: Optional caller-owned user label. Missing labels supplied
                through this argument normalize to "0".

        Returns:
            Dict with "matches" (list of (statement, score) pairs) and
            "keywords" (extracted query keywords). "resolved_query" is the
            context-expanded query used as the cache key.
        """
        if limit < 1:
            raise ValueError("limit must be at least 1")
        if user_id is not None:
            normalized_user_id = sessions_mod.normalize_user_id(user_id)
            if session_id and session_id != normalized_user_id:
                raise ValueError("session_id and user_id must identify the same context")
            session_id = normalized_user_id

        with self.count_lock:
            self.query_count += 1

        # Get session context if provided. The session is created when missing,
        # matching pattern_query -- a fresh session id must not silently skip
        # context tracking.
        expanded_text = text
        if self.config["expand_contractions"]:
            expanded_text = expand_contractions(
                expanded_text,
                self.substitution_maps["contractions"],
            )
        if session_id:
            session = sessions_mod.get_session(self, session_id, create_if_missing=True)
            if session:
                with self.session_lock:
                    session_touch(session)
                    expanded_text = expand_query(
                        expanded_text,
                        session["previous_response"],
                    )

        # Normalize and extract keywords
        normalized = normalize(expanded_text)

        # Input cleanup: correct out-of-vocabulary typos toward the store's
        # own vocabulary, so a near-miss like "abotu" still retrieves.
        if self.config["use_spell_correction"]:
            with self.keyword_lock:
                vocabulary = set(self.keywords)
            normalized = correct_spelling(normalized, vocabulary)

        keywords = self._extract_keywords(normalized)

        if not keywords:
            empty_result = query_result(matches=[], keywords=[], resolved_query=expanded_text)
            return empty_result

        # Expand the candidate search with synonyms if enabled. The synonym
        # map also feeds scoring, where a synonym-only match earns partial
        # overlap credit (SYNONYM_OVERLAP_WEIGHT) instead of scoring zero.
        search_keywords = keywords
        synonyms_by_keyword: dict[str, tuple] = {}
        if self.config["use_synonyms"]:
            search_keywords = expand_with_synonyms(
                keywords,
                max_synonyms_per_word=self.config["max_synonyms_per_word"],
            )
            for kw in keywords:
                syns = tuple(s for s in get_synonyms(kw, max_synonyms=self.config["max_synonyms_per_word"]) if s != kw)
                if syns:
                    synonyms_by_keyword[kw] = syns

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
            no_candidates = query_result(
                matches=[],
                keywords=keywords,
                resolved_query=expanded_text,
            )
            return no_candidates

        # Score candidates
        scored: list[tuple[dict, float, int]] = []
        with self.statement_lock, self.keyword_lock:
            total = len(self.statements)
            for stmt_id in candidate_ids:
                idx = self.statement_index.get(stmt_id)
                if idx is not None:
                    stmt = self.statements[idx]
                    score = score_statement(
                        statement=stmt,
                        query_keywords=keywords,
                        keyword_index=self.keywords,
                        total_statements=total,
                        weight_base=self.config["weight_base"],
                        weight_recency=self.config["weight_recency"],
                        weight_hit_rate=self.config["weight_hit_rate"],
                        recency_half_life_seconds=self.config["recency_half_life_seconds"],
                        synonyms=synonyms_by_keyword,
                    )
                    if score > 0:
                        scored.append((stmt, score, idx))

        # Sort by score descending; break exact ties toward the newer
        # statement (higher store index) so ranking stays deterministic even
        # when statement timestamps collide within one clock tick.
        scored.sort(key=lambda x: (x[1], x[2]), reverse=True)
        matches = [(stmt, score) for stmt, score, _ in scored[:limit]]

        # Statement-level candidacy stats: each returned match was presented to
        # the caller, so it counts as a query against that statement. Paired
        # with record_hit(statement_id=...), this feeds the hit-rate-aware
        # eviction policies (LRU / LFU / HIT_RATE) and min_hit_rate protection.
        with self.statement_lock:
            for stmt, _ in matches:
                record_statement_query(stmt)

        result = query_result(
            matches=matches,
            keywords=keywords,
            resolved_query=expanded_text,
        )
        return result

    def pattern_query(
        self,
        text: str,
        session_id=None,
        user_id: str | None = None,
        combine_sentences: bool = True,
    ) -> tuple:
        """Query using AIML-style pattern matching.

        Supports multi-sentence input: sentences are split, matched independently,
        and responses are combined.

        Args:
            text: User input text (may contain multiple sentences).
            session_id: Optional legacy session label for context.
            user_id: Optional caller-owned user label. When supplied, learned
                conversational facts record this attribution.
            combine_sentences: Combine every matched sentence response when
                true (the legacy low-level behavior). Chat callers set this
                false so one user turn receives one response, selected from
                the final matched sentence.

        Returns:
            Tuple of (matched_statement, captured_wildcards, response_text) or ().
            For multi-sentence input, returns first matched statement with combined response.
        """
        with self.count_lock:
            self.query_count += 1

        attributed_user_id = None
        if user_id is not None:
            attributed_user_id = sessions_mod.normalize_user_id(user_id)
            if session_id and session_id != attributed_user_id:
                raise ValueError("session_id and user_id must identify the same context")
            session_id = attributed_user_id

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
                with self.session_lock:
                    that = session["previous_response"]  # Bot's last response (normalized)
                    topic = session["predicates"].get("topic", "")  # Current topic

        # Input cleanup: correct typos toward the store's vocabulary before
        # matching. Fact extraction below still sees the raw sentence, since
        # its punctuation and casing carry signal.
        vocabulary: set[str] = set()
        if self.config["use_spell_correction"]:
            with self.keyword_lock:
                vocabulary = set(self.keywords)

        # Process each sentence
        responses: list[str] = []
        first_stmt = None
        first_captured: list[str] = []
        last_stmt = None
        last_captured: list[str] = []
        last_catchall_render = None

        for sentence in sentences:
            match_text = sentence
            if vocabulary:
                match_text = correct_spelling(normalize(sentence), vocabulary)
            with self.statement_lock:
                result = self.pattern_matcher.match(match_text, that=that, topic=topic)
            if result:
                (
                    response_text,
                    captured,
                    thatstars,
                    topicstars,
                    matched_pattern,
                    matched_topic,
                    matched_that,
                ) = result
                captured = restore_capture_case(captured, sentence)

                # Extract and learn facts from declarative sentences when
                # configured. Learned knowledge is shared, while its user
                # attribution and each user's conversation context remain
                # distinct.
                facts = []
                if self.config["learn_user_facts"]:
                    if self.config["use_spacy_facts"]:
                        facts = extract_facts(sentence)
                    else:
                        fact = extract_fact(sentence)
                        facts = [fact] if fact else []
                # Gate on the match text's intent too: a typo can defeat the
                # raw-text question gate ("waht is your name" reads as a
                # statement), and the spell-corrected text reveals it.
                if facts and input_kind(match_text) != KIND_STATEMENT:
                    facts = []
                learned = False
                known_response = ""
                for fact in facts:
                    if self.learn_fact(
                        fact,
                        introduced_by_user_id=attributed_user_id,
                    ):
                        learned = True
                        continue
                    # Already known. Surface the stored belief instead of a
                    # generic deflection: the no-overwrite rule protects the
                    # stored fact, but staying silent about a contradiction
                    # would read as agreement.
                    existing_id = self.pattern_to_statement.get(fact_subject_upper(fact), "")
                    existing = self.get_statement(existing_id)
                    if existing and existing["text"]:
                        if normalize(existing["text"]) == normalize(fact["original"]):
                            reply = random.choice(KNOWN_FACT_RESPONSES)
                        else:
                            reply = random.choice(CONFLICTING_FACT_RESPONSES)
                        known_response = reply.replace("{existing}", existing["text"])

                # Find the statement carrying this (pattern, topic, that).
                # Among duplicates the highest priority wins, ties going to
                # the earliest stored.
                with self.statement_lock:
                    selected: dict = {}
                    for stmt in self.statements:
                        carries_pattern = stmt["pattern"] == matched_pattern or matched_pattern in stmt["pattern_aliases"]
                        triple_match = carries_pattern and stmt["topic"] == matched_topic and stmt["that"] == matched_that
                        if triple_match and (not selected or stmt["priority"] > selected["priority"]):
                            selected = stmt
                    if selected:
                        # Pattern selection is a query and a hit in one step
                        # (there is no later confirmation on this path), so
                        # record both -- this is what keeps frequently used
                        # patterns alive under LRU / LFU / HIT_RATE eviction.
                        record_statement_query(selected)
                        record_statement_hit(selected)

                        # If we learned a fact and matched catch-all, acknowledge
                        # instead -- rotating the phrasing so a teaching session
                        # does not answer identically every turn. A restated or
                        # contradicted known fact surfaces the stored belief.
                        last_catchall_render = None
                        if learned and matched_pattern == "*":
                            final_response = random.choice(LEARNED_ACKNOWLEDGMENTS)
                        elif known_response and matched_pattern == "*":
                            final_response = known_response
                        else:
                            # Process template if present
                            final_response = self._process_statement_template(
                                selected,
                                captured,
                                sentence,
                                session,
                                thatstars=thatstars,
                                topicstars=topicstars,
                            )
                            if is_pure_wildcard(matched_pattern):
                                last_catchall_render = (selected, captured, sentence, thatstars, topicstars)
                        responses.append(final_response)

                        # Track first match for return value
                        if first_stmt is None:
                            first_stmt = selected
                            first_captured = captured
                        last_stmt = selected
                        last_captured = captured

                        # Update 'that' for next sentence (response becomes context)
                        that = final_response

        if not responses:
            # Try graph lookup before falling back
            graph_response = self.graph_lookup(text)
            if graph_response:
                if session:
                    with self.session_lock:
                        session_update_context(session, graph_response, text)
                graph_result_tuple = (None, [], graph_response)
                return graph_result_tuple

            # Use fallback response if configured
            if self.config["fallback_response"]:
                if session:
                    with self.session_lock:
                        session_update_context(session, self.config["fallback_response"], text)
                fallback_tuple = (None, [], self.config["fallback_response"])
                return fallback_tuple
            return ()

        # Low-level pattern callers retain AIML-style multi-sentence
        # composition. Conversational callers select one response from the
        # final matched sentence, avoiding unrelated fragments in one reply.
        if combine_sentences:
            combined_response = " ".join(responses)
            returned_stmt = first_stmt
            returned_captured = first_captured
        else:
            combined_response = responses[-1]
            returned_stmt = last_stmt
            returned_captured = last_captured

            # Catch-all prompts should not echo a recent wording or argue with
            # explicit feedback that the conversation is looping. Re-render a
            # randomized template a few times before using the honest escape.
            if session and returned_stmt and _reports_repetition(text) and _pattern_has_wildcard(returned_stmt["pattern"]):
                combined_response = REPETITION_ESCAPE_RESPONSE
            elif last_catchall_render and returned_stmt is last_catchall_render[0] and session:
                recent_responses = session["response_history"][:3]
                if _response_repeats(combined_response, recent_responses):
                    selected, captured, sentence, thatstars, topicstars = last_catchall_render
                    for _ in range(8):
                        candidate = self._process_statement_template(
                            selected,
                            captured,
                            sentence,
                            session,
                            thatstars=thatstars,
                            topicstars=topicstars,
                        )
                        if not _response_repeats(candidate, recent_responses):
                            combined_response = candidate
                            break
                    else:
                        combined_response = REPETITION_ESCAPE_RESPONSE

        # Output cleanup: repair casing (sentence starts, the pronoun I) that
        # lowercase wildcard captures splice into authored text.
        if self.config["polish_responses"]:
            combined_response = polish_response(combined_response)

        # Update session context with full input and combined response
        if session:
            with self.session_lock:
                session_update_context(session, combined_response, text)

        match_tuple = (returned_stmt, returned_captured, combined_response)
        return match_tuple

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
        context = template_context(
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
            graph_fn=self.graph_read_fn,
        )

        # Add session context
        if session:
            with self.session_lock:
                context["session_id"] = session["session_id"]
                context["predicates"] = session["predicates"].copy()
                context["input_history"] = session["input_history"].copy()
                context["response_history"] = session["response_history"].copy()
                context["that_history"] = [s.copy() for s in session["that_history"]]

        # Set redirect callback
        def redirect_fn(pattern: str) -> str:
            with self.statement_lock:
                result = self.pattern_matcher.match(pattern)
            if result:
                (
                    response_text,
                    new_captured,
                    new_thatstars,
                    new_topicstars,
                    matched_pattern,
                    matched_topic,
                    matched_that,
                ) = result
                with self.statement_lock:
                    # Same selection rule as pattern_query: highest priority
                    # among statements sharing the matched (pattern, topic, that).
                    redirect_stmt: dict = {}
                    for s in self.statements:
                        triple_match = s["pattern"] == matched_pattern and s["topic"] == matched_topic and s["that"] == matched_that
                        if triple_match and (not redirect_stmt or s["priority"] > redirect_stmt["priority"]):
                            redirect_stmt = s
                    if redirect_stmt:
                        # Create new context for redirect
                        new_context = template_context(
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
                            graph_fn=self.graph_read_fn,
                        )
                        template_to_process = redirect_stmt["template"] or redirect_stmt["text"]
                        redirect_response = self.template_processor.process(template_to_process, new_context)
                        return redirect_response
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

        # Update session predicates from context. Underscore-prefixed
        # predicates (e.g. _mood, _kind) are template-local scratch: they
        # never persist into the session, and any that leaked in previously
        # are purged.
        if session:
            with self.session_lock:
                for name, value in context["predicates"].items():
                    if not name.startswith("_"):
                        session["predicates"][name] = value
                leaked_scratch = [name for name in session["predicates"] if name.startswith("_")]
                for name in leaked_scratch:
                    del session["predicates"][name]

        return response

    def record_hit(self, keywords: list[str], statement_id: str = "") -> None:
        """Update statistics after successful retrieval.

        Args:
            keywords: Query keywords that led to a hit.
            statement_id: Optional id of the statement that answered the query.
                When given, that statement's own hit statistics are updated too,
                which is what the hit-rate-aware eviction policies (LRU / LFU /
                HIT_RATE) and min_hit_rate protection read.
        """
        with self.count_lock:
            self.hit_count += 1
        with self.keyword_lock:
            for kw in keywords:
                if kw in self.keywords:
                    self.keywords[kw]["hit_count"] += 1
        if statement_id:
            with self.statement_lock:
                idx = self.statement_index.get(statement_id)
                if idx is not None:
                    record_statement_hit(self.statements[idx])

    def learn_fact(
        self,
        fact: dict,
        introduced_by_user_id: str | None = None,
        source_label: str = "",
        tier: Tier = Tier.DYNAMIC,
    ) -> bool:
        """Learn a fact extracted from natural language.

        Creates patterns for the subject and common query forms so the fact
        can be retrieved later.

        Args:
            fact: The extracted fact to learn.
            introduced_by_user_id: Optional conversational user attribution.
            source_label: Optional non-user source label.
            tier: Storage tier for the generated statements.

        Returns:
            True if the fact was learned, False if it was already known.
        """
        if introduced_by_user_id is not None and not isinstance(introduced_by_user_id, str):
            raise ValueError("introduced_by_user_id must be a string or None")
        if not isinstance(source_label, str):
            raise ValueError("source_label must be a string")

        # Check and store under one re-entrant statement lock so concurrent
        # speakers cannot admit duplicate copies of the same fact.
        subject_pattern = fact_subject_upper(fact)
        with self.statement_lock:
            for stmt in self.statements:
                if stmt["pattern"] == subject_pattern:
                    # Already know this - don't overwrite
                    return False

            # Store one fact statement with alternate retrieval patterns.
            # Aliases live in the matcher and map to this one statement, so a
            # fact does not consume capacity once per query phrasing.
            aliases = []
            for query_pattern in fact_query_patterns(fact)[1:]:
                query_pattern = query_pattern.strip()
                if query_pattern and query_pattern != subject_pattern and query_pattern not in self.pattern_to_statement:
                    aliases.append(query_pattern)
            aliases = list(dict.fromkeys(aliases))

            # Keyword-index under the original sentence so retrieval sees the
            # fact's content ("Is the sky blue?" needs [sky, blue], not just
            # [sky]). store() safely re-enters statement_lock here.
            self.store(
                text=fact["original"],
                pattern=subject_pattern,
                pattern_aliases=aliases,
                tier=tier,
                keyword_source=fact["original"],
                introduced_by_user_id=introduced_by_user_id,
                source_label=source_label,
            )

        return True

    def add_fact(
        self,
        text: str,
        source_label: str = "",
        tier: Tier = Tier.DYNAMIC,
    ) -> str:
        """Add shared knowledge without assigning it to a conversational user.

        The fact is indexed and, when extraction succeeds, receives the usual
        subject and question patterns. It never creates or updates user
        context. The returned id identifies the primary stored statement.
        """
        if not isinstance(text, str) or not text.strip():
            raise ValueError("fact text must be a non-empty string")
        if not isinstance(source_label, str):
            raise ValueError("source_label must be a string")

        fact_text = text.strip()
        facts = extract_facts(fact_text) if self.config["use_spacy_facts"] else []
        if not facts:
            fact = extract_fact(fact_text)
            facts = [fact] if fact else []

        if facts:
            primary_pattern = fact_subject_upper(facts[0])
            for fact in facts:
                self.learn_fact(
                    fact,
                    introduced_by_user_id=None,
                    source_label=source_label,
                    tier=tier,
                )
            with self.statement_lock:
                return self.pattern_to_statement.get(primary_pattern, "")

        normalized_text = normalize(fact_text)
        with self.statement_lock:
            for stmt in self.statements:
                if not stmt["pattern"] and normalize(stmt["text"]) == normalized_text:
                    return stmt["id"]

        return self.store(
            text=fact_text,
            tier=tier,
            introduced_by_user_id=None,
            source_label=source_label,
        )

    def get_statement(self, statement_id: str) -> dict:
        """Get a statement by ID.

        Args:
            statement_id: Statement ID.

        Returns:
            Statement dict if found, {} otherwise.
        """
        with self.statement_lock:
            idx = self.statement_index.get(statement_id)
            if idx is not None:
                return self.statements[idx]
        return {}

    def retire_statement(self, statement_id: str) -> bool:
        """Remove a statement from the store by id.

        Unlike capacity eviction, this is a deliberate removal of a specific entry --
        a caller retiring a cached response it has decided is no longer valid.
        evict_statement_at cleans the keyword index, the pattern matcher, and the
        pattern map alongside the statement itself. Returns False when no
        statement carries the id.

        Args:
            statement_id: The id of the statement to remove.

        Returns:
            True if a statement was removed, False if the id was not present.
        """
        with self.statement_lock:
            idx = self.statement_index.get(statement_id)
            if idx is None:
                return False
            return eviction_mod.evict_statement_at(self, idx)

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
        count = len(statements)
        return count

    def sync_corpus(self, pairs: list, tier: Tier = Tier.STATIC, prune: bool = False) -> dict:
        """Upsert pattern/template pairs into the store (seed refresh).

        A store is seeded once at creation; without this, later improvements
        to the seed corpus never reach an existing store and its templates go
        stale. For each pair, an existing statement of the given tier with the
        same (pattern, that, topic) has its text and template replaced in
        place -- id and hit statistics are preserved -- while pairs with no
        existing statement are stored new. Statements of other tiers are never
        touched, so learned DYNAMIC content survives a refresh.

        With prune, statements of the tier that no pair accounts for are
        retired, making the store's tier mirror the corpus: entries deleted
        from the seed stop lingering. Prune only with the complete corpus --
        syncing a partial pair list with prune would retire everything the
        list omits.

        Args:
            pairs: List of pair dicts (pattern, response/text, template, that,
                topic), the shape of data/seed.json's "pairs".
            tier: Tier to sync into (default STATIC, the seeded tier).
            prune: Retire statements of the tier absent from pairs.

        Returns:
            Dict with "added", "updated", "unchanged", and "pruned" counts.
        """
        added = 0
        updated = 0
        unchanged = 0

        for pair in pairs:
            pattern = pair.get("pattern", "")
            text = pair.get("response") or pair.get("text") or ""
            template = pair.get("template", {}) or {}
            that = pair.get("that", "")
            topic = pair.get("topic", "")

            existing: dict = {}
            with self.statement_lock:
                for stmt in self.statements:
                    if stmt["tier"] != tier:
                        continue
                    if pattern:
                        if stmt["pattern"] == pattern and stmt["that"] == that and stmt["topic"] == topic:
                            existing = stmt
                            break
                    elif not stmt["pattern"] and stmt["text"] == text:
                        # Plain statements have no pattern key; same text = same entry
                        existing = stmt
                        break

                if existing:
                    if existing["text"] == text and existing["template"] == template:
                        unchanged += 1
                    else:
                        existing["text"] = text
                        existing["template"] = template
                        updated += 1

            if not existing:
                self.store(
                    text,
                    tier=tier,
                    pattern=pattern or None,
                    that=that or None,
                    topic=topic or None,
                    template=template or None,
                )
                added += 1

        pruned = 0
        if prune:
            desired_triples = set()
            desired_texts = set()
            for pair in pairs:
                pattern = pair.get("pattern", "")
                if pattern:
                    desired_triples.add((pattern, pair.get("that", ""), pair.get("topic", "")))
                else:
                    desired_texts.add(pair.get("response") or pair.get("text") or "")
            with self.statement_lock:
                stale_ids = []
                for stmt in self.statements:
                    if stmt["tier"] != tier:
                        continue
                    if stmt["pattern"]:
                        if (stmt["pattern"], stmt["that"], stmt["topic"]) not in desired_triples:
                            stale_ids.append(stmt["id"])
                    elif stmt["text"] not in desired_texts:
                        stale_ids.append(stmt["id"])
                for stmt_id in stale_ids:
                    if self.retire_statement(stmt_id):
                        pruned += 1

        result = {"added": added, "updated": updated, "unchanged": unchanged, "pruned": pruned}
        return result

    @classmethod
    def fork(
        cls,
        parent: "Engram",
        static_corpus=None,
        config=None,
    ) -> "Engram":
        """Create a new ENGRAM forked from a parent.

        The child gets the parent's DYNAMIC statements copied in full (text,
        pattern, that, topic, template), a fresh STATIC corpus if one is given,
        and a fresh session registry. Hit statistics are not inherited -- the
        child earns its own.

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

        # Copy parent's DYNAMIC statements, including their patterns and
        # templates so scripted responses survive the fork.
        with parent.statement_lock:
            for stmt in parent.statements:
                if stmt["tier"] == Tier.DYNAMIC:
                    instance.store(
                        stmt["text"],
                        tier=Tier.DYNAMIC,
                        pattern=stmt["pattern"],
                        that=stmt["that"],
                        topic=stmt["topic"],
                        template=stmt["template"],
                    )

        # Fresh session registry (no inheritance)
        return instance
