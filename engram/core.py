"""Core ENGRAM implementation."""

import heapq
import logging
import random
import threading
from difflib import SequenceMatcher
from pathlib import Path

from sentence_transformers import SentenceTransformer

from engram import eviction as eviction_mod, sessions as sessions_mod
from engram.config import engram_config
from engram.constants import (
    CONFLICTING_FACT_RESPONSES,
    KIND_STATEMENT,
    KNOWN_FACT_RESPONSES,
    LEARNED_ACKNOWLEDGMENTS,
    REPETITION_ESCAPE_RESPONSE,
    REPETITION_FEEDBACK_MARKERS,
    REPETITION_HISTORY_SIZE,
    RESPONSE_SIMILARITY_THRESHOLD,
    VERSION,
    WILDCARD_TOKENS,
    Tier,
)
from engram.dialogue import (
    DIALOGUE_ACKNOWLEDGMENT,
    DIALOGUE_CLOSING,
    DIALOGUE_COMMAND,
    DIALOGUE_EMOTION,
    DIALOGUE_FACT,
    DIALOGUE_GRATITUDE,
    DIALOGUE_GREETING,
    DIALOGUE_OPINION,
    DIALOGUE_QUESTION,
    DIALOGUE_SELF_INTRODUCTION,
    DIALOGUE_TOPIC_SHIFT,
    classify_dialogue_act,
    contextual_fallback_options,
    conversational_fact_admission,
    dialogue_act_clears_unreferenced_topic,
    extract_dialogue_entities,
    infer_active_topic,
    pattern_is_broad,
    repeated_input_response_options,
    repetition_response_options,
    select_turn_candidate,
    topic_from_statement_pattern,
    topic_is_referenced,
)
from engram.facts_spacy import extract_facts
from engram.graph import create_graph_client, is_write_cypher
from engram.identity import ScopedRetrievalKey
from engram.indexes import (
    MAX_INDEX_SUPPORT_SCAN_EDGES,
    ExactLookupResult,
    IndexCheckReport,
    IndexOwner,
    IndexProjection,
    IndexRepairResult,
    IndexState,
    SupportLookupResult,
    projection_from_statement,
)
from engram.models import (
    keyword_entry,
    query_result,
    record_statement_hit,
    record_statement_query,
    session_record_input,
    session_touch,
    session_update_context,
    session_update_dialogue,
    statement,
)
from engram.nlp import extract_entities, extract_fact, fact_query_patterns, fact_subject_upper, input_kind
from engram.pattern import PatternMatcher, is_pure_wildcard
from engram.phrasing import phrase_facts
from engram.polish import polish_response
from engram.scoring import score_statement
from engram.spacy_setup import get_nlp
from engram.substitutions import expand_contractions, split_sentences, substitution_maps
from engram.template import TemplateProcessor, template_context
from engram.text import (
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
EMPTY_CONFIG: dict = {}


def _reports_repetition(text: str) -> bool:
    normalized_text = normalize(text)
    return any(marker in normalized_text for marker in REPETITION_FEEDBACK_MARKERS)


def _response_repeats(candidate: str, recent_responses: list[str], *, allow_similarity: bool = True) -> bool:
    normalized_candidate = normalize(candidate)
    if not normalized_candidate:
        return False
    for recent in recent_responses:
        normalized_recent = normalize(recent)
        if normalized_recent and (
            normalized_candidate == normalized_recent
            or (
                allow_similarity
                and SequenceMatcher(lambda _: False, normalized_candidate, normalized_recent).ratio()
                >= RESPONSE_SIMILARITY_THRESHOLD
            )
        ):
            return True
    return False


def _input_repeats(candidate: str, recent_inputs: list[str]) -> bool:
    normalized_candidate = normalize(candidate)
    return bool(normalized_candidate and any(normalized_candidate == normalize(recent_input) for recent_input in recent_inputs))


def _pattern_has_wildcard(pattern: str) -> bool:
    return any(word.lstrip("$") in WILDCARD_TOKENS for word in pattern.split())


def _response_cache_scope(template) -> tuple[str, str]:
    """Return the caller-owned namespace and context attached to a response."""
    if not isinstance(template, dict):
        return "", ""
    tapestry_metadata = template.get("tapestry")
    if not isinstance(tapestry_metadata, dict):
        return "", ""
    namespace = tapestry_metadata.get("namespace", "")
    context_fingerprint = tapestry_metadata.get("context_fingerprint", "")
    return str(namespace), str(context_fingerprint)


# Canonical-graph recall queries. Claims link to canonical Entity/Predicate
# nodes by edge; the surface triple is read from the denormalized projection on
# the Claim node (subject/predicate/object), never matched on. See schema.cypher.
GRAPH_ENTITY_FACTS_QUERY = (
    "MATCH (c:Claim)-[:HAS_SUBJECT]->(proof_subject:Entity) "
    "MATCH (c)-[:USES_PREDICATE]->(proof_predicate:Predicate) "
    "OPTIONAL MATCH (c)-[:HAS_OBJECT]->(proof_object:Entity) "
    "MATCH (c)-[rel:HAS_SUBJECT|HAS_OBJECT]->(e:Entity) "
    "WHERE (toLower(e.primary_label) = toLower($name) "
    "OR toLower($name) IN [a IN e.aliases | toLower(a)] "
    "OR toLower(rel.surface_form) = toLower($name)) "
    "AND c.invalidated_at IS NULL AND c.system_to IS NULL "
    "AND proof_predicate.canonical_id <> 'generic_relation' "
    "AND coalesce(c.predicate_canonical, true) = true "
    "AND (trim(coalesce(c.object, '')) = '' OR proof_object.canonical_id IS NOT NULL) "
    "RETURN DISTINCT c.subject AS subject, c.predicate AS predicate, c.object AS object "
    "LIMIT 5"
)
GRAPH_KEYWORD_FACTS_QUERY = (
    "MATCH (c:Claim)-[:HAS_SUBJECT]->(e:Entity) "
    "MATCH (c)-[:USES_PREDICATE]->(proof_predicate:Predicate) "
    "OPTIONAL MATCH (c)-[:HAS_OBJECT]->(proof_object:Entity) "
    "WHERE toLower(e.primary_label) CONTAINS toLower($keyword) "
    "AND c.invalidated_at IS NULL AND c.system_to IS NULL "
    "AND proof_predicate.canonical_id <> 'generic_relation' "
    "AND coalesce(c.predicate_canonical, true) = true "
    "AND (trim(coalesce(c.object, '')) = '' OR proof_object.canonical_id IS NOT NULL) "
    "RETURN DISTINCT c.subject AS subject, c.predicate AS predicate, c.object AS object "
    "LIMIT 3"
)


class Engram:
    """Keyword-indexed statement store with hit-rate tracking.

    ENGRAM stores flat statements retrieved by keyword overlap and scored by
    relevance signals. It supports multiple concurrent sessions sharing a
    common statement pool.
    """

    def __init__(self, config: dict = EMPTY_CONFIG) -> None:
        """Initialize ENGRAM instance.

        Args:
            config: Configuration options. Uses defaults if not provided.
        """
        if not isinstance(config, dict):
            raise ValueError("config must be an object")
        self.config = dict(config) if config else engram_config()

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

        # Concurrency control. Mutations acquire locks in this order:
        # mutation_lock, statement_lock, keyword_lock, then the private index
        # owner lock. Index readers retain an immutable snapshot after the
        # owner lock is released, so they never nest it with statement_lock.
        # statement_lock also guards the pattern matcher
        # and pattern_to_statement map (mutated on store/evict, read on match),
        # and thereby the shared template processor, whose recursion counters
        # are only touched while the pattern pipeline holds statement_lock.
        # count_lock guards the top-level metrics counters.
        self.mutation_lock = threading.RLock()
        self.statement_lock = threading.RLock()
        self.keyword_lock = threading.RLock()
        self.session_lock = threading.RLock()
        self.count_lock = threading.Lock()
        self._index_owner = IndexOwner()

        # Metrics
        self.query_count = 0
        self.hit_count = 0
        self.eviction_count = 0

        # Enabled graph components are initialized during construction. Request
        # paths only use already-created clients and already-loaded models.
        self._graph_client = ()
        self._graph_embedding_model = ()
        graph_config = self.config.get("graph") or {}
        if graph_config.get("enabled"):
            self._graph_client = create_graph_client(
                host=graph_config["host"],
                port=graph_config["port"],
                username=graph_config["username"],
                password=graph_config.get("password", ""),
            )
        if graph_config.get("vector_enabled"):
            self._load_graph_embedding_model()
        try:
            self.component_status = self.preflight_components()
        except RuntimeError as error:
            raise ValueError(f"component preflight failed: {error}") from error

    @property
    def graph_client(self):
        """Return the graph client created during Engram initialization."""
        return self._graph_client

    def preflight_components(self) -> dict:
        """Verify every enabled external or model-backed component before serving."""
        graph_settings = self.config.get("graph") or {}
        graph_enabled = bool(graph_settings.get("enabled"))
        vector_enabled = bool(graph_settings.get("vector_enabled"))
        spacy_full_enabled = any(
            self.config.get(name, False) for name in ("use_spacy_facts", "use_spacy_lemmatization", "use_phrase_keywords")
        )
        spacy_phrasing_enabled = graph_enabled

        if vector_enabled and not graph_enabled:
            raise RuntimeError("vector recall requires graph access to be enabled")
        if graph_enabled and (not self._graph_client or getattr(self._graph_client, "available", True) is False):
            raise RuntimeError("configured MemGraph service is unavailable")
        if vector_enabled:
            self.warm_vector_recall()
        if spacy_full_enabled and not get_nlp():
            raise RuntimeError("enabled spaCy features require the pre-provisioned English model")
        if spacy_phrasing_enabled and not get_nlp(disable=("parser", "ner")):
            raise RuntimeError("enabled graph phrasing requires the pre-provisioned spaCy English model")

        return {
            "graph": {"enabled": graph_enabled, "ready": graph_enabled},
            "vector": {"enabled": vector_enabled, "ready": vector_enabled},
            "spacy": {
                "enabled": spacy_full_enabled or spacy_phrasing_enabled,
                "ready": spacy_full_enabled or spacy_phrasing_enabled,
            },
        }

    def graph_query(self, cypher: str, params=()) -> list:
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
        if not client:
            return []
        try:
            records = client.execute_read(cypher, params)
            return records
        except RuntimeError as err:
            logger.debug("Graph query failed: %s", err)
            return []

    def graph_read_fn(self, cypher: str, params=()) -> list:
        """Read-only graph callback for template operations.

        Both this callback and the underlying connection reject mutating
        Cypher. The duplicated boundary keeps custom graph clients read-only.
        """
        return self.graph_query(cypher, params)

    def _load_graph_embedding_model(self) -> None:
        """Load the configured local embedding model during initialization."""
        graph_config = self.config.get("graph") or {}
        model_name = str(graph_config.get("vector_model") or "").strip()
        model_path = str(graph_config.get("vector_model_path") or "").strip()
        dimension = int(graph_config.get("vector_dimension") or 0)
        if not model_name or dimension < 1:
            raise ValueError("vector recall model and dimension must be configured")
        model_source = model_path or model_name
        if model_path and not Path(model_path).is_dir():
            raise FileNotFoundError(f"vector model path does not exist: {model_path}")
        self._graph_embedding_model = SentenceTransformer(
            model_source,
            device="cpu",
            local_files_only=True,
        )

    def _encode_graph_query(self, text: str) -> list[float]:
        """Encode one graph-recall query with the startup-loaded local model."""
        graph_config = self.config.get("graph") or {}
        dimension = int(graph_config.get("vector_dimension") or 0)
        if not self._graph_embedding_model:
            raise RuntimeError("vector recall model was not initialized")
        encoded = self._graph_embedding_model.encode(
            [text],
            normalize_embeddings=True,
            show_progress_bar=False,
            convert_to_numpy=True,
        )
        vector = encoded[0].tolist()
        if len(vector) != dimension:
            raise ValueError(f"query embedding dimension {len(vector)} does not match configured graph dimension {dimension}")
        return vector

    def graph_vector_claims(self, text: str, *, limit: int = 0) -> list:
        """Return active semantic Claim hits for ``text``.

        The method fails soft because vector recall augments the deterministic
        keyword path; a model, index, or graph outage must not make Engram
        unavailable.
        """
        graph_config = self.config.get("graph") or {}
        client = self.graph_client
        if not client or not graph_config.get("enabled") or not graph_config.get("vector_enabled"):
            return []
        search = getattr(client, "vector_search_claims", ())
        if not callable(search):
            return []
        try:
            embedding = self._encode_graph_query(text)
            rows = search(
                embedding,
                index_name=graph_config["vector_index_name"],
                limit=(max(1, min(1000, int(limit))) if limit else int(graph_config["vector_limit"])),
                min_similarity=float(graph_config["vector_min_similarity"]),
            )
            return rows if isinstance(rows, list) else []
        except Exception as err:
            logger.warning("Vector graph recall unavailable; using keyword fallback: %s", err)
            return []

    def warm_vector_recall(self) -> bool:
        """Load the query model and verify the configured graph ANN path.

        Unlike request-time augmentation, startup warm-up is strict: an
        enabled vector path that cannot reach its model, graph, or index must
        not advertise a healthy server and then time out on the first proposal.
        """
        graph_config = self.config.get("graph") or {}
        if not graph_config.get("vector_enabled"):
            return False
        client = self.graph_client
        search = getattr(client, "vector_search_claims", ())
        if not client or not callable(search):
            raise RuntimeError("configured graph client lacks vector Claim search")
        embedding = self._encode_graph_query("Engram vector recall readiness")
        search(
            embedding,
            index_name=graph_config["vector_index_name"],
            limit=1,
            min_similarity=0.0,
        )
        if getattr(client, "available", True) is False:
            raise RuntimeError("configured MemGraph service is unavailable")
        return True

    @staticmethod
    def _statement_support_ids(statement: dict) -> set[str]:
        template = statement.get("template")
        if not isinstance(template, dict):
            return set()
        metadata = template.get("tapestry")
        if not isinstance(metadata, dict):
            return set()
        support = metadata.get("support")
        if not isinstance(support, list):
            return set()
        return {
            str(reference.get("claim_id"))
            for reference in support
            if isinstance(reference, dict) and str(reference.get("claim_id") or "").strip()
        }

    def index_snapshot(self) -> IndexState:
        """Return the currently visible immutable index state."""
        return self._index_owner.snapshot()

    def exact_lookup(self, key: ScopedRetrievalKey) -> ExactLookupResult:
        """Look up one scoped exact key against a single immutable snapshot."""
        return self.index_snapshot().exact_lookup(key)

    def support_lookup(self, claim_ids: tuple[str, ...]) -> SupportLookupResult:
        """Find statements supported by the supplied matched Claim IDs."""
        return self.index_snapshot().support_lookup(claim_ids)

    def check_indexes(self) -> IndexCheckReport:
        """Compare live indexes with projections from current statements."""
        with self.mutation_lock:
            with self.statement_lock:
                sources = tuple(projection_from_statement(statement_value) for statement_value in self.statements)
            return self._index_owner.check_against(sources)

    def check_index_projections(self, projections) -> IndexCheckReport:
        """Compare live indexes with one explicit projection set, including empty."""
        return self._index_owner.check_against(tuple(projections))

    def repair_indexes(self, *, dry_run: bool = True) -> IndexRepairResult:
        """Repair indexes from current authoritative statements."""
        with self.mutation_lock, self.statement_lock:
            sources = tuple(projection_from_statement(statement_value) for statement_value in self.statements)
            return self._index_owner.repair(sources, dry_run=dry_run)

    def repair_index_projections(self, projections, *, dry_run: bool = True) -> IndexRepairResult:
        """Repair indexes from one explicit projection set, including empty."""
        with self.mutation_lock:
            return self._index_owner.repair(tuple(projections), dry_run=dry_run)

    def rebuild_indexes(self, *, apply: bool = False) -> IndexRepairResult:
        """Alias current-statement repair with rebuild-oriented wording."""
        if not isinstance(apply, bool):
            raise ValueError("index rebuild apply must be a boolean")
        return self.repair_indexes(dry_run=not apply)

    def add_index_projection(self, projection: IndexProjection) -> IndexState:
        """Atomically add one generic index projection."""
        if not isinstance(projection, IndexProjection):
            raise ValueError("projection must be an IndexProjection")
        with self.mutation_lock:
            return self._index_owner.add(projection)

    def replace_index_projection(self, projection: IndexProjection) -> IndexState:
        """Atomically replace one generic index projection."""
        if not isinstance(projection, IndexProjection):
            raise ValueError("projection must be an IndexProjection")
        with self.mutation_lock:
            return self._index_owner.replace(projection)

    def remove_index_projection(self, statement_id: str) -> IndexState:
        """Atomically remove one generic index projection."""
        with self.mutation_lock:
            return self._index_owner.remove(statement_id)

    def update_index_support(self, statement_id: str, support_claim_ids: tuple[str, ...]) -> IndexState:
        """Atomically replace only one projection's support Claim IDs."""
        with self.mutation_lock:
            return self._index_owner.update_support(statement_id, support_claim_ids)

    def _replace_statement_index_projection(self, statement_value: dict) -> None:
        projection = projection_from_statement(statement_value)
        if projection.statement_id in self.index_snapshot().projections:
            self._index_owner.replace(projection)
        else:
            self._index_owner.add(projection)

    def _remove_index_projection_if_present(self, statement_id: str) -> None:
        if statement_id in self.index_snapshot().projections:
            self._index_owner.remove(statement_id)

    def vector_supported_matches(
        self,
        text: str,
        *,
        limit: int,
        statement_filter=(),
    ) -> list[tuple[dict, float]]:
        """Rank scoped cached responses through their KG support Claims."""
        rows = self.graph_vector_claims(text)
        support_scores = {str(row.get("claim_id")): float(row.get("similarity") or 0.0) for row in rows if row.get("claim_id")}
        if not support_scores:
            return []
        graph_settings = self.config.get("graph") or {}
        vector_weight = float(graph_settings["vector_weight"])
        scan_limit = int(graph_settings.get("vector_support_scan_limit", MAX_INDEX_SUPPORT_SCAN_EDGES))
        state = self.index_snapshot()
        scan_plan = state.support_scan_plan(tuple(support_scores), scan_limit)
        if not scan_plan.complete:
            logger.warning(
                "vector_support_scan_incomplete: matched support fan-out %s exceeds configured limit %s; abstaining",
                scan_plan.edge_count,
                scan_plan.scan_limit,
            )
            return []
        if limit < 1:
            return []
        scored: list[tuple[float, int, dict]] = []
        seen_statement_ids = set()
        with self.statement_lock:
            for claim_id in scan_plan.queried_claim_ids:
                for statement_id in state.claim_to_statements.get(claim_id, ()):
                    if statement_id in seen_statement_ids:
                        continue
                    seen_statement_ids.add(statement_id)
                    index = self.statement_index.get(statement_id, -1)
                    if index < 0:
                        continue
                    statement_value = self.statements[index]
                    if statement_filter and not statement_filter(statement_value):
                        continue
                    similarities = [
                        support_scores[support_id]
                        for support_id in state.statement_to_claims.get(statement_id, ())
                        if support_id in support_scores
                    ]
                    if not similarities:
                        continue
                    score = max(similarities) * vector_weight + float(statement_value.get("priority", 0))
                    ranked = (score, index, statement_value)
                    if len(scored) < limit:
                        heapq.heappush(scored, ranked)
                    elif (score, index) > (scored[0][0], scored[0][1]):
                        heapq.heapreplace(scored, ranked)
        scored.sort(key=lambda item: (item[0], item[1]), reverse=True)
        return [(statement_value, score) for score, _, statement_value in scored]

    def graph_lookup(self, text: str) -> str:
        """Look up information in the knowledge graph based on input text.

        Extracts entities from the text and queries the graph for related
        information. Returns a natural language response if found.

        Args:
            text: User input text.

        Returns:
            Response string if graph has relevant info, otherwise an empty string.
        """
        client = self.graph_client
        if not client:
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
            vector_facts = self._records_to_facts(self.graph_vector_claims(text, limit=5))
            return self._format_graph_facts(vector_facts) if vector_facts else ""

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
        vector_facts = self._records_to_facts(self.graph_vector_claims(text, limit=5))
        return self._format_graph_facts(vector_facts) if vector_facts else ""

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
        template=(),
        introduced_by_user_id: str = "",
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
            template: Optional structured metadata to carry on the stored
                entry. Caller metadata is opaque except for the reserved
                `tapestry.namespace` and `tapestry.context_fingerprint` values,
                which isolate response-cache replacement.

        Returns:
            Statement ID of the stored (or updated) response.
        """
        if not isinstance(introduced_by_user_id, str):
            raise ValueError("introduced_by_user_id must be a string")
        if not isinstance(source_label, str):
            raise ValueError("source_label must be a string")

        normalized = normalize(query)
        if self.config["use_spell_correction"]:
            with self.keyword_lock:
                vocabulary = set(self.keywords)
            normalized = correct_spelling(normalized, vocabulary)
        keywords = self._extract_keywords(normalized)
        keyword_set = set(keywords)
        response_scope = _response_cache_scope(template)

        # Dedup: a previously learned entry for the same keyword set is the
        # same cached question within one caller-owned scope -- update it
        # instead of accumulating duplicates. Unscoped callers retain the
        # original behavior without colliding with scoped cache entries.
        if keyword_set:
            with self.mutation_lock, self.statement_lock, self.keyword_lock:
                candidate_ids: set[str] = set()
                for kw in keywords:
                    if kw in self.keywords:
                        candidate_ids.update(self.keywords[kw]["statement_ids"])
                for stmt_id in candidate_ids:
                    if stmt_id not in self.statement_index:
                        continue
                    idx = self.statement_index[stmt_id]
                    stmt = self.statements[idx]
                    if (
                        stmt["tier"] == Tier.DYNAMIC
                        and not stmt["pattern"]
                        and set(stmt["keywords"]) == keyword_set
                        and _response_cache_scope(stmt["template"]) == response_scope
                    ):
                        stmt["text"] = response
                        stmt["template"] = dict(template or ())
                        stmt["introduced_by_user_id"] = introduced_by_user_id
                        stmt["source_label"] = source_label
                        stmt["hit_count"] = 0
                        stmt["query_count"] = 0
                        stmt["last_hit"] = ""
                        self._replace_statement_index_projection(stmt)
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
        statement_id: str = "",
        pattern: str = "",
        pattern_aliases=(),
        that: str = "",
        topic: str = "",
        template=(),
        priority: int = 0,
        keyword_source: str = "",
        introduced_by_user_id: str = "",
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
        if not isinstance(introduced_by_user_id, str):
            raise ValueError("introduced_by_user_id must be a string")
        if not isinstance(source_label, str):
            raise ValueError("source_label must be a string")
        if not pattern_aliases:
            aliases: list[str] = []
        elif isinstance(pattern_aliases, (list, tuple)) and all(isinstance(alias, str) for alias in pattern_aliases):
            aliases = list(dict.fromkeys(alias.strip() for alias in pattern_aliases if alias.strip()))
        else:
            raise ValueError("pattern_aliases must be a sequence of strings")
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
        index_projection = projection_from_statement(stmt)

        with self.mutation_lock, self.statement_lock, self.keyword_lock:
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
            for kw in keywords:
                if kw not in self.keywords:
                    self.keywords[kw] = keyword_entry(keyword=kw)
                self.keywords[kw]["statement_ids"].add(stmt["id"])
            self._index_owner.add(index_projection)

        return stmt["id"]

    def query(
        self,
        text: str,
        session_id: str = "",
        limit: int = 5,
        user_id: str = "",
        statement_filter=(),
        record_candidates: bool = True,
    ) -> dict:
        """Retrieve matching statements.

        Args:
            text: Query text.
            session_id: Optional legacy session label for context expansion.
            limit: Maximum results (default: 5).
            user_id: Optional caller-owned user label. Missing labels supplied
                through this argument normalize to "0".
            statement_filter: Optional predicate applied before scoring and
                candidacy accounting. Intended for integrations that isolate
                caller-owned response-cache scopes.
            record_candidates: Record query accounting for returned matches.
                Regulated proposal retrieval disables this until keyword and
                vector candidates have been merged.

        Returns:
            Dict with "matches" (list of (statement, score) pairs) and
            "keywords" (extracted query keywords). "resolved_query" is the
            context-expanded query used as the cache key.
        """
        if limit < 1:
            raise ValueError("limit must be at least 1")
        if statement_filter and not callable(statement_filter):
            raise ValueError("statement_filter must be callable")
        if not isinstance(record_candidates, bool):
            raise ValueError("record_candidates must be a boolean")
        if user_id:
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
                if stmt_id not in self.statement_index:
                    continue
                idx = self.statement_index[stmt_id]
                stmt = self.statements[idx]
                if statement_filter and not statement_filter(stmt):
                    continue
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
        if record_candidates:
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
        session_id: str = "",
        user_id: str = "",
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
                false so one user turn receives one response selected using
                the complete turn's dialogue acts.

        Returns:
            Tuple of (matched_statement, captured_wildcards, response_text) or ().
            For multi-sentence input, returns first matched statement with combined response.
        """
        with self.count_lock:
            self.query_count += 1

        attributed_user_id = ""
        if user_id:
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
        session = {}
        that = ""
        topic = ""
        active_topic = ""
        if session_id:
            session = sessions_mod.get_session(self, session_id, create_if_missing=True)
            if session:
                with self.session_lock:
                    that = session["previous_response"]  # Bot's last response (normalized)
                    topic = session["predicates"].get("topic", "")  # Current topic
                    active_topic = session.get("active_topic", "")

        # Input cleanup: correct typos toward the store's vocabulary before
        # matching. Fact extraction below still sees the raw sentence, since
        # its punctuation and casing carry signal.
        vocabulary: set[str] = set()
        if self.config["use_spell_correction"]:
            with self.keyword_lock:
                vocabulary = set(self.keywords)

        # Process each sentence
        responses: list[str] = []
        first_stmt = {}
        first_captured: list[str] = []
        candidates: list[dict] = []
        turn_dialogue_acts: list[str] = []
        turn_entities: list[dict] = []
        turn_fact_admissions: list[dict] = []

        for sentence in sentences:
            match_text = sentence
            if vocabulary:
                match_text = correct_spelling(normalize(sentence), vocabulary)

            # Interpret every sentence before response selection.  Fact
            # admission is intentionally narrower than extraction: transient,
            # hedged, and conversation-meta assertions can shape the current
            # turn without becoming shared durable knowledge.
            extracted_facts = []
            if self.config["learn_user_facts"]:
                if self.config["use_spacy_facts"]:
                    extracted_facts = extract_facts(sentence)
                else:
                    extracted = extract_fact(sentence)
                    extracted_facts = [extracted] if extracted else []
            if extracted_facts and input_kind(match_text) != KIND_STATEMENT:
                extracted_facts = []
            fact_decisions = []
            for fact in extracted_facts:
                decision = conversational_fact_admission(fact, sentence)
                diagnostic = {
                    "text": fact.get("original", sentence),
                    "subject": fact.get("subject", ""),
                    "admitted": decision["admitted"],
                    "reason": decision["reason"],
                }
                fact_decisions.append((fact, decision))
                turn_fact_admissions.append(diagnostic)
            admitted_facts = [fact for fact, decision in fact_decisions if decision["admitted"]]

            prior_topic = active_topic
            sentence_act = classify_dialogue_act(sentence, extracted_facts[0] if extracted_facts else {})
            preliminary_topic = infer_active_topic(
                sentence,
                fact=extracted_facts[0] if extracted_facts else {},
                previous_topic=prior_topic,
            )
            sentence_entities = (
                extract_dialogue_entities(
                    sentence,
                    fact=admitted_facts[0] if admitted_facts else {},
                    topic=preliminary_topic,
                )
                if session
                else []
            )
            inferred_topic = infer_active_topic(
                sentence,
                fact=extracted_facts[0] if extracted_facts else {},
                entities=sentence_entities if dialogue_act_clears_unreferenced_topic(sentence_act) else [],
                previous_topic=prior_topic,
            )
            explicit_reference = topic_is_referenced(sentence, prior_topic)
            implicit_reply = sentence_act in {DIALOGUE_ACKNOWLEDGMENT, DIALOGUE_EMOTION, DIALOGUE_OPINION} and topic_is_referenced(
                that, prior_topic
            )
            topic_grounded = bool(inferred_topic or explicit_reference or implicit_reply)
            if inferred_topic:
                active_topic = inferred_topic
            elif topic_grounded:
                active_topic = prior_topic
            elif dialogue_act_clears_unreferenced_topic(sentence_act):
                active_topic = ""
            turn_dialogue_acts.append(sentence_act)
            turn_entities.extend(sentence_entities)

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

                learned = False
                known_response = ""
                for fact in admitted_facts:
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
                        catchall_render = ()
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
                                catchall_render = (selected, captured, sentence, thatstars, topicstars)
                        responses.append(final_response)
                        candidates.append(
                            {
                                "statement": selected,
                                "captured": captured,
                                "response": final_response,
                                "dialogue_act": sentence_act,
                                "topic": active_topic if topic_grounded else "",
                                "topic_grounded": topic_grounded,
                                "learned": learned,
                                "known_response": bool(known_response),
                                "catchall_render": catchall_render,
                            }
                        )

                        # Track first match for return value
                        if not first_stmt:
                            first_stmt = selected
                            first_captured = captured
                        # Update 'that' for next sentence (response becomes context)
                        that = final_response

        if not responses:
            selected_act = turn_dialogue_acts[-1] if turn_dialogue_acts else ""
            # Try graph lookup before falling back
            graph_response = self.graph_lookup(text)
            if graph_response:
                if session:
                    with self.session_lock:
                        session_update_dialogue(session, selected_act, active_topic, turn_entities, turn_fact_admissions)
                        session_update_context(session, graph_response, text)
                graph_result_tuple = ({}, [], graph_response)
                return graph_result_tuple

            # Use fallback response if configured
            if self.config["fallback_response"]:
                if session:
                    with self.session_lock:
                        session_update_dialogue(session, selected_act, active_topic, turn_entities, turn_fact_admissions)
                        session_update_context(session, self.config["fallback_response"], text)
                fallback_tuple = ({}, [], self.config["fallback_response"])
                return fallback_tuple
            if session:
                with self.session_lock:
                    session_update_dialogue(session, selected_act, active_topic, turn_entities, turn_fact_admissions)
                    session_record_input(session, text)
            return ()

        # Low-level pattern callers retain AIML-style multi-sentence
        # composition. Conversational callers select one response from the
        # final matched sentence, avoiding unrelated fragments in one reply.
        if combine_sentences:
            combined_response = " ".join(responses)
            returned_stmt = first_stmt
            returned_captured = first_captured
            selected_candidate = candidates[-1]
        else:
            selected_candidate = select_turn_candidate(candidates)
            combined_response = selected_candidate["response"]
            returned_stmt = selected_candidate["statement"]
            returned_captured = selected_candidate["captured"]

            # A successful learned-fact recall is direct evidence of the new
            # topic, even when the query used an inverse alias such as
            # "What is good?" -> Sushi.
            if returned_stmt.get("pattern_aliases"):
                recalled_topic = topic_from_statement_pattern(returned_stmt["pattern"], returned_stmt.get("text", ""))
                if recalled_topic:
                    active_topic = recalled_topic
                    selected_candidate["topic"] = recalled_topic
                    selected_candidate["topic_grounded"] = True

            recent_responses = session["response_history"][:REPETITION_HISTORY_SIZE] if session else []
            allow_similarity = selected_candidate["dialogue_act"] != DIALOGUE_TOPIC_SHIFT
            expected_fact_recall = bool(
                returned_stmt
                and returned_stmt.get("pattern_aliases")
                and selected_candidate["dialogue_act"] in {DIALOGUE_COMMAND, DIALOGUE_QUESTION}
            )
            expected_name_recall = bool(
                returned_stmt
                and returned_stmt["pattern"] in {"DO YOU REMEMBER MY NAME", "WHAT IS MY NAME"}
                and selected_candidate["dialogue_act"] == DIALOGUE_QUESTION
            )
            redirect_repeated_input = bool(
                session
                and not _reports_repetition(text)
                and _input_repeats(text, session["input_history"][:REPETITION_HISTORY_SIZE])
                and selected_candidate["dialogue_act"]
                not in {
                    DIALOGUE_ACKNOWLEDGMENT,
                    DIALOGUE_CLOSING,
                    DIALOGUE_FACT,
                    DIALOGUE_GRATITUDE,
                    DIALOGUE_GREETING,
                    DIALOGUE_SELF_INTRODUCTION,
                }
                and not (expected_fact_recall or expected_name_recall)
            )

            # Broad prompts should not override stronger dialogue evidence or
            # argue with explicit feedback that the conversation is looping.
            if session and returned_stmt and _reports_repetition(text) and _pattern_has_wildcard(returned_stmt["pattern"]):
                combined_response = REPETITION_ESCAPE_RESPONSE
            elif (
                session
                and returned_stmt
                and (
                    (is_pure_wildcard(returned_stmt["pattern"]) and bool(returned_stmt["template"]))
                    or pattern_is_broad(returned_stmt["pattern"])
                    or (
                        selected_candidate["dialogue_act"] in {DIALOGUE_CLOSING, DIALOGUE_TOPIC_SHIFT}
                        and _pattern_has_wildcard(returned_stmt["pattern"])
                    )
                )
            ):
                # Prefer a response grounded in the active per-user topic over
                # a generic therapist-style prompt.  Learned/known fact
                # acknowledgments remain authoritative.
                can_ground_fallback = selected_candidate["topic_grounded"] or selected_candidate["dialogue_act"] in {
                    DIALOGUE_CLOSING,
                    DIALOGUE_TOPIC_SHIFT,
                }
                if can_ground_fallback and not selected_candidate["learned"] and not selected_candidate["known_response"]:
                    fact_text = ""
                    if active_topic:
                        fact_id = self.pattern_to_statement.get(active_topic.upper(), "")
                        topic_fact = self.get_statement(fact_id)
                        fact_text = topic_fact.get("text", "") if topic_fact else ""
                    options = contextual_fallback_options(
                        selected_candidate["dialogue_act"],
                        topic=selected_candidate["topic"] or active_topic,
                        fact_text=fact_text,
                        had_gratitude=DIALOGUE_GRATITUDE in turn_dialogue_acts,
                    )
                    for option in options:
                        if not _response_repeats(option, recent_responses, allow_similarity=allow_similarity):
                            combined_response = option
                            break

                catchall_render = selected_candidate["catchall_render"]
                if _response_repeats(combined_response, recent_responses, allow_similarity=allow_similarity) and catchall_render:
                    selected, captured, sentence, thatstars, topicstars = catchall_render
                    for _ in range(8):
                        candidate = self._process_statement_template(
                            selected,
                            captured,
                            sentence,
                            session,
                            thatstars=thatstars,
                            topicstars=topicstars,
                        )
                        if not _response_repeats(candidate, recent_responses, allow_similarity=allow_similarity):
                            combined_response = candidate
                            break
                    else:
                        combined_response = REPETITION_ESCAPE_RESPONSE

            if redirect_repeated_input:
                for option in repeated_input_response_options():
                    if not _response_repeats(option, recent_responses):
                        combined_response = option
                        break
                else:
                    combined_response = REPETITION_ESCAPE_RESPONSE

            # Repetition control applies to every conversational response,
            # including exact authored patterns. Repeated factual recalls are
            # useful and remain exempt.
            if (
                session
                and _response_repeats(combined_response, recent_responses, allow_similarity=allow_similarity)
                and not (expected_fact_recall or expected_name_recall)
            ):
                if selected_candidate["learned"]:
                    alternatives = LEARNED_ACKNOWLEDGMENTS
                elif selected_candidate["known_response"]:
                    alternatives = ()
                else:
                    alternatives = repetition_response_options(selected_candidate["dialogue_act"], active_topic)
                for alternative in alternatives:
                    if not _response_repeats(alternative, recent_responses, allow_similarity=allow_similarity):
                        combined_response = alternative
                        break

        # Output cleanup: repair casing (sentence starts, the pronoun I) that
        # lowercase wildcard captures splice into authored text.
        if self.config["polish_responses"]:
            combined_response = polish_response(combined_response)

        # Update session context with full input and combined response
        if session:
            with self.session_lock:
                session_update_dialogue(
                    session,
                    selected_candidate["dialogue_act"],
                    active_topic,
                    turn_entities,
                    turn_fact_admissions,
                )
                session_update_context(session, combined_response, text)

        match_tuple = (returned_stmt, returned_captured, combined_response)
        return match_tuple

    def _process_statement_template(
        self,
        stmt: dict,
        captured: list[str],
        input_text: str,
        session,
        thatstars=(),
        topicstars=(),
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
            thatstars=thatstars or (),
            topicstars=topicstars or (),
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
                if statement_id in self.statement_index:
                    record_statement_hit(self.statements[self.statement_index[statement_id]])

    def learn_fact(
        self,
        fact: dict,
        introduced_by_user_id: str = "",
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
        if not isinstance(introduced_by_user_id, str):
            raise ValueError("introduced_by_user_id must be a string")
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
                    introduced_by_user_id="",
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
            introduced_by_user_id="",
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
            if statement_id in self.statement_index:
                return self.statements[self.statement_index[statement_id]]
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
        with self.mutation_lock, self.statement_lock:
            if statement_id not in self.statement_index:
                return False
            return eviction_mod.evict_statement_at(self, self.statement_index[statement_id])

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
            with self.mutation_lock, self.statement_lock:
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
                        self._replace_statement_index_projection(existing)
                        updated += 1

            if not existing:
                self.store(
                    text,
                    tier=tier,
                    pattern=pattern,
                    that=that,
                    topic=topic,
                    template=template,
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
            with self.mutation_lock, self.statement_lock:
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
        static_corpus=(),
        config=(),
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
