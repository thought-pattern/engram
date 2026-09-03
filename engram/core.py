"""Core ENGRAM implementation."""

from datetime import UTC, datetime
from difflib import SequenceMatcher
from heapq import heappush as heapq_heappush, heapreplace as heapq_heapreplace
from logging import getLogger as logging_getLogger
from pathlib import Path
from random import choice as random_choice
from threading import Lock as threading_Lock, RLock as threading_RLock
from time import monotonic_ns as time_monotonic_ns

from sentence_transformers import SentenceTransformer

from engram import eviction as eviction_mod, sessions as sessions_mod
from engram.config import engram_config
from engram.constants import (
    CONFLICTING_FACT_RESPONSES,
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
    EMPTY_CONFIG,
    GRAPH_ENTITY_FACTS_QUERY,
    GRAPH_KEYWORD_FACTS_QUERY,
    KIND_STATEMENT,
    KNOWN_FACT_RESPONSES,
    LEARNED_ACKNOWLEDGMENTS,
    MAX_RELATION_CANDIDATES,
    MAX_RELATION_PLAN_ROWS,
    MAX_STRUCTURED_PROPOSITION_PROJECTION_TERMS,
    REPETITION_ESCAPE_RESPONSE,
    REPETITION_FEEDBACK_MARKERS,
    REPETITION_HISTORY_SIZE,
    RESPONSE_SIMILARITY_THRESHOLD,
    VERSION,
    WILDCARD_TOKENS,
    Tier,
)
from engram.dialogue import (
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
from engram.feedback import FeedbackStore
from engram.graph import (
    PropositionProjectionQuery,
    canonical_entity_match_from_graph_row,
    canonical_predicate_match_from_graph_row,
    connect_graph,
    is_write_cypher,
    projection_timestamp,
    proposition_projection_to_dict,
    validate_proposition_projection,
    validate_relation_proposition_projection,
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
from engram.mutations import MutationReceiptLedger
from engram.nlp import extract_entities, extract_fact, fact_query_patterns, fact_subject_upper, input_kind
from engram.nltk_data import ensure_nltk_data
from engram.pattern import PatternMatcher, is_pure_wildcard
from engram.phrasing import phrase_facts
from engram.polish import polish_response
from engram.repository import ArtifactRepository
from engram.reranking import TransparentLogisticReranker
from engram.resources import estimate_working_bytes, require_working_memory
from engram.scoring import score_statement_components
from engram.semantic import StandaloneSemanticRetriever
from engram.spacy_setup import get_nlp
from engram.sparse import search_sparse_artifacts
from engram.substitutions import expand_contractions, split_sentences, substitution_maps
from engram.telemetry import operational_telemetry, record_graph_recall, telemetry_snapshot
from engram.template import TemplateProcessor, template_context
from engram.text import (
    correct_spelling,
    expand_query,
    expand_with_synonyms,
    extract_keywords,
    extract_keywords_spacy,
    get_synonyms,
    initialize_nltk_readers,
    normalize,
    restore_capture_case,
)
from engram.utilities import UtilityRegistry

logger = logging_getLogger(__name__)


def run_cooperative_check(check=()) -> None:
    """Run an optional resolver-owned cooperative callback."""
    if check:
        if not callable(check):
            raise ValueError("cooperative_check must be callable")
        check()


def reports_repetition(text: str) -> bool:
    normalized_text = normalize(text)
    result = any(marker in normalized_text for marker in REPETITION_FEEDBACK_MARKERS)
    return result


def response_repeats(candidate: str, recent_responses: list[str], *, allow_similarity: bool = True) -> bool:
    normalized_candidate = normalize(candidate)
    if not normalized_candidate:
        result = False
        return result
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
            result = True
            return result
    result = False
    return result


def input_repeats(candidate: str, recent_inputs: list[str]) -> bool:
    normalized_candidate = normalize(candidate)
    result = bool(normalized_candidate and any(normalized_candidate == normalize(recent_input) for recent_input in recent_inputs))
    return result


def pattern_has_wildcard(pattern: str) -> bool:
    result = any(word.lstrip("$") in WILDCARD_TOKENS for word in pattern.split())
    return result


def graph_records_to_facts(records: list) -> list[tuple[str, str, str]]:
    """Turn canonical graph rows into complete fact tuples."""
    facts = []
    seen: set[tuple[str, str, str]] = set()
    for record in records:
        subject = record.get("subject", "")
        predicate = record.get("predicate", "")
        obj = record.get("object", "")
        fact = (subject, predicate, obj)
        identity = tuple(value.casefold() for value in fact)
        if subject and predicate and obj and identity not in seen:
            seen.add(identity)
            facts.append(fact)
    result = facts
    return result


def format_graph_facts(facts: list[tuple[str, str, str]]) -> str:
    """Phrase graph fact tuples as natural-language sentences."""
    result = phrase_facts(facts)
    return result


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
        # mutation_lock, statement_lock, then keyword_lock. statement_lock also guards the pattern matcher
        # and pattern_to_statement map (mutated on store/evict, read on match),
        # and thereby the shared template processor, whose recursion counters
        # are only touched while the pattern pipeline holds statement_lock.
        # count_lock guards the top-level metrics counters.
        self.mutation_lock = threading_RLock()
        self.statement_lock = threading_RLock()
        self.keyword_lock = threading_RLock()
        self.session_lock = threading_RLock()
        self.count_lock = threading_Lock()
        self.response_repository = ArtifactRepository()
        self.semantic_retriever = StandaloneSemanticRetriever(self.config.get("semantic") or {})
        self.reranker = TransparentLogisticReranker(self.config.get("reranker") or {})
        self.utility_registry = UtilityRegistry(self.config.get("utility") or {})
        self.mutation_receipts = MutationReceiptLedger()
        self.feedback_store = FeedbackStore()

        self.query_count = 0
        self.hit_count = 0
        self.eviction_count = 0
        self.operational_metrics = operational_telemetry()

        # Graph tooling is optional even when configured. Construction records
        # its readiness, but an unavailable graph must not prevent the local
        # cache, matcher, conversation, or regulated-response paths from serving.
        # Graph access is capability-based: production uses MemGraphConnection,
        # while deterministic benchmarks may provide the same narrow methods.
        self.internal_graph_client: object = ()
        self.graph_embedding_model = ()
        graph_config = self.config.get("graph") or {}
        if graph_config.get("enabled"):
            self.internal_graph_client = connect_graph(
                host=graph_config["host"],
                port=graph_config["port"],
                username=graph_config["username"],
                password=graph_config.get("password", ""),
                deployment_mode=graph_config.get("deployment_mode", ""),
                visibility_scope=graph_config.get("visibility_scope", {}),
            )
        if graph_config.get("vector_enabled"):
            try:
                self.load_graph_embedding_model()
            except Exception as error:
                self.graph_embedding_model = ()
                logger.warning("Optional graph vector model is unavailable: %s", type(error).__name__)
        try:
            self.component_status = self.preflight_components()
        except RuntimeError as error:
            raise ValueError(f"component preflight failed: {error}") from error

    @property
    def graph_client(self):
        """Return the graph client created during Engram initialization."""
        result = self.internal_graph_client
        return result

    def preflight_components(self) -> dict:
        """Verify required components and report optional component readiness."""
        graph_settings = self.config.get("graph") or {}
        graph_enabled = bool(graph_settings.get("enabled"))
        vector_enabled = bool(graph_settings.get("vector_enabled"))
        spacy_full_enabled = any(
            self.config.get(name, False) for name in ("use_spacy_facts", "use_spacy_lemmatization", "use_phrase_keywords")
        )
        missing_nltk = ensure_nltk_data(download=False)

        if missing_nltk:
            missing_names = ", ".join(download_name for _, download_name in missing_nltk)
            raise RuntimeError(f"required NLTK resources are unavailable: {missing_names}")
        initialize_nltk_readers()
        if vector_enabled and not graph_enabled:
            raise RuntimeError("vector recall requires graph access to be enabled")
        graph_ready = bool(graph_enabled and self.internal_graph_client and getattr(self.internal_graph_client, "available", True))
        spacy_phrasing_enabled = graph_ready
        vector_ready = False
        if vector_enabled and graph_ready and self.graph_embedding_model:
            try:
                vector_ready = self.warm_vector_recall()
            except Exception as error:
                logger.warning("Optional graph vector recall is unavailable: %s", type(error).__name__)
        if spacy_full_enabled and not get_nlp():
            raise RuntimeError("enabled spaCy features require the pre-provisioned English model")
        spacy_ready = bool(get_nlp()) if spacy_full_enabled or spacy_phrasing_enabled else False

        result = {
            "nltk": {"enabled": True, "ready": True},
            "graph": {"enabled": graph_enabled, "ready": graph_ready},
            "vector": {"enabled": vector_enabled, "ready": vector_ready},
            "sparse": {
                "enabled": bool((self.config.get("sparse") or {}).get("enabled", False)),
                "ready": bool((self.config.get("sparse") or {}).get("enabled", False)),
            },
            "semantic": self.semantic_retriever.health(),
            "reranker": self.reranker.health(),
            "utility": self.utility_registry.health(),
            "spacy": {
                "enabled": spacy_full_enabled or graph_enabled,
                "ready": spacy_ready,
            },
        }
        return result

    def component_status_snapshot(self) -> dict:
        """Return current readiness without contacting optional dependencies."""
        result = {name: dict(value) for name, value in self.component_status.items()}
        graph = result["graph"]
        graph["ready"] = bool(
            graph["enabled"] and self.internal_graph_client and getattr(self.internal_graph_client, "available", True)
        )
        vector = result["vector"]
        vector["ready"] = bool(vector["enabled"] and graph["ready"] and self.graph_embedding_model and vector["ready"])
        sparse = result["sparse"]
        sparse["ready"] = bool(sparse["enabled"])
        result["semantic"] = self.semantic_retriever.health()
        result["reranker"] = self.reranker.health()
        result["utility"] = self.utility_registry.health()
        return result

    def operational_telemetry_snapshot(self) -> dict:
        """Return isolated fixed-cardinality process telemetry."""
        with self.count_lock:
            result = telemetry_snapshot(self.operational_metrics)
            return result

    def graph_query(self, cypher: str, params=(), *, raise_on_failure: bool = False) -> list:
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
            result = []
            return result
        execute = getattr(client, "execute", ())
        if not callable(execute):
            return []
        try:
            records = execute(cypher, params)
            if not isinstance(records, list):
                raise RuntimeError("graph read capability returned an invalid collection")
            return records
        except RuntimeError as err:
            logger.debug("Graph query failed (%s)", type(err).__name__)
            if raise_on_failure:
                raise
            result = []
            return result

    def graph_read_fn(self, cypher: str, params=()) -> list:
        """Read-only graph callback for template operations.

        Both this callback and the underlying connection reject mutating
        Cypher. The duplicated boundary keeps custom graph clients read-only.
        """
        result = self.graph_query(cypher, params)
        return result

    def load_graph_embedding_model(self) -> None:
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
        self.graph_embedding_model = SentenceTransformer(
            model_source,
            device="cpu",
            local_files_only=True,
        )

    def encode_graph_query(self, text: str) -> list[float]:
        """Encode one graph-recall query with the startup-loaded local model."""
        graph_config = self.config.get("graph") or {}
        dimension = int(graph_config.get("vector_dimension") or 0)
        if not self.graph_embedding_model:
            raise RuntimeError("vector recall model was not initialized")
        encoded = self.graph_embedding_model.encode(
            [text],
            normalize_embeddings=True,
            show_progress_bar=False,
            convert_to_numpy=True,
        )
        vector = encoded[0].tolist()
        if len(vector) != dimension:
            raise ValueError(f"query embedding dimension {len(vector)} does not match configured graph dimension {dimension}")
        return vector

    def graph_vector_propositions(self, text: str, *, limit: int = 0, evaluation_time: str = "") -> list:
        """Return active semantic Proposition hits for ``text``.

        The method fails soft because vector recall augments the deterministic
        keyword path; a model, index, or graph outage must not make Engram
        unavailable.
        """
        graph_config = self.config.get("graph") or {}
        client = self.graph_client
        if not client or not graph_config.get("enabled") or not graph_config.get("vector_enabled"):
            result = []
            return result
        search = getattr(client, "vector_search_propositions", ())
        if not callable(search):
            result = []
            return result
        try:
            embedding = self.encode_graph_query(text)
            rows = search(
                embedding,
                index_name=graph_config["vector_index_name"],
                limit=(max(1, min(1000, int(limit))) if limit else int(graph_config["vector_limit"])),
                min_similarity=float(graph_config["vector_min_similarity"]),
                evaluation_time=evaluation_time,
            )
            result = rows if isinstance(rows, list) else []
            return result
        except Exception as err:
            logger.warning("Vector graph recall unavailable; using keyword fallback (%s)", type(err).__name__)
            result = []
            return result

    def graph_vector_proposition_projections(
        self,
        text: str,
        *,
        limit: int = 0,
        cooperative_check=(),
        max_working_memory_bytes: int = 0,
        evaluation_time: str = "",
    ) -> list[dict]:
        """Return strictly decoded wire-safe ANN Proposition projections."""
        graph_config = self.config.get("graph") or {}
        client = self.graph_client
        if not client or not graph_config.get("enabled") or not graph_config.get("vector_enabled"):
            result = []
            return result
        search = getattr(client, "vector_search_proposition_projections", ())
        if not callable(search):
            result = []
            return result
        try:
            run_cooperative_check(cooperative_check)
            embedding = self.encode_graph_query(text)
            require_working_memory(estimate_working_bytes(embedding), max_working_memory_bytes)
            run_cooperative_check(cooperative_check)
            row_limit = max(1, min(1000, int(limit))) if limit else int(graph_config["vector_limit"])
            rows = search(
                embedding,
                index_name=graph_config["vector_index_name"],
                limit=row_limit,
                min_similarity=float(graph_config["vector_min_similarity"]),
                evaluation_time=evaluation_time,
            )
            if not isinstance(rows, list) or len(rows) > row_limit:
                raise ValueError("vector Proposition projection boundary returned an invalid collection")
            validated_rows = [validate_proposition_projection(row) for row in rows]
            require_working_memory(
                estimate_working_bytes(embedding)
                + estimate_working_bytes([proposition_projection_to_dict(row) for row in validated_rows]),
                max_working_memory_bytes,
            )
            run_cooperative_check(cooperative_check)
            return validated_rows
        except (TimeoutError, MemoryError):
            raise
        except Exception as err:
            logger.warning(
                "Vector Proposition projection unavailable; omitting response-less evidence (%s)",
                type(err).__name__,
            )
            result = []
            return result

    def current_proposition_projection(self, proposition_id: str) -> tuple[dict, ...]:
        """Re-read one canonical Proposition through the fixed by-ID capability."""
        client = self.graph_client
        lookup = getattr(client, "proposition_projection_by_id", ())
        if not client or not callable(lookup):
            result = ()
            return result
        rows = lookup(proposition_id)
        if not isinstance(rows, list) or len(rows) > 1:
            raise ValueError("Proposition projection revalidation boundary returned an invalid collection")
        result = tuple(validate_proposition_projection(row) for row in rows)
        return result

    def warm_vector_recall(self) -> bool:
        """Load the query model and report whether the optional graph ANN path is ready."""
        graph_config = self.config.get("graph") or {}
        if not graph_config.get("vector_enabled"):
            result = False
            return result
        client = self.graph_client
        search = getattr(client, "vector_search_propositions", ())
        if not client or not callable(search):
            raise RuntimeError("configured graph client lacks vector Proposition search")
        embedding = self.encode_graph_query("Engram vector recall readiness")
        search(
            embedding,
            index_name=graph_config["vector_index_name"],
            limit=1,
            min_similarity=0.0,
        )
        if getattr(client, "available", True) is False:
            result = False
            return result
        result = True
        return result

    def sparse_candidates(
        self,
        text: str,
        scope,
        *,
        limit: int,
        max_working_memory_bytes: int,
    ) -> dict:
        """Search request-local sparse structures derived from the current artifacts."""
        repository = self.response_repository.snapshot()
        result = search_sparse_artifacts(
            tuple(repository.get("artifacts", {}).values()),
            text,
            scope,
            self.config.get("sparse") or {},
            limit=limit,
            max_working_memory_bytes=max_working_memory_bytes,
        )
        return result

    def semantic_candidates(
        self,
        text: str,
        scope,
        *,
        limit: int,
        max_vector_results: int,
        max_working_memory_bytes: int,
        cooperative_check=(),
    ) -> dict:
        """Search request-local embeddings derived from the current artifacts."""
        repository = self.response_repository.snapshot()
        result = self.semantic_retriever.search(
            text,
            scope,
            tuple(repository.get("artifacts", {}).values()),
            limit=limit,
            max_vector_results=max_vector_results,
            max_working_memory_bytes=max_working_memory_bytes,
            cooperative_check=cooperative_check,
        )
        return result

    def vector_supported_matches(
        self,
        text: str,
        *,
        limit: int,
        artifact_filter=(),
    ) -> list[tuple[dict, float]]:
        """Rank scoped cached responses through their KG support Propositions."""
        result = [
            (match["artifact"], match["retrieval_score"])
            for match in self.vector_supported_match_components(
                text,
                limit=limit,
                artifact_filter=artifact_filter,
            )
        ]
        return result

    def vector_supported_match_components(
        self,
        text: str,
        *,
        limit: int,
        artifact_filter=(),
        cooperative_check=(),
        max_working_memory_bytes: int = 0,
    ) -> list[dict]:
        """Return support matches with raw similarity separate from response ranking."""
        run_cooperative_check(cooperative_check)
        rows = self.graph_vector_propositions(text, limit=limit)
        result = self.vector_supported_proposition_match_components(
            rows,
            limit=limit,
            artifact_filter=artifact_filter,
            cooperative_check=cooperative_check,
            max_working_memory_bytes=max_working_memory_bytes,
        )
        return result

    def vector_supported_proposition_match_components(
        self,
        rows: list[dict],
        *,
        limit: int,
        artifact_filter=(),
        cooperative_check=(),
        max_working_memory_bytes: int = 0,
    ) -> list[dict]:
        """Intersect discovered Proposition hits with typed response support."""
        run_cooperative_check(cooperative_check)
        require_working_memory(estimate_working_bytes(rows), max_working_memory_bytes)
        support_scores = {
            str(row.get("proposition_id")): float(row.get("similarity") or 0.0) for row in rows if row.get("proposition_id")
        }
        result = self.vector_supported_match_components_from_scores(
            support_scores,
            source_working_bytes=estimate_working_bytes(rows),
            limit=limit,
            artifact_filter=artifact_filter,
            cooperative_check=cooperative_check,
            max_working_memory_bytes=max_working_memory_bytes,
        )
        return result

    def vector_supported_match_components_from_scores(
        self,
        support_scores: dict[str, float],
        *,
        source_working_bytes: int,
        limit: int,
        artifact_filter=(),
        cooperative_check=(),
        max_working_memory_bytes: int = 0,
    ) -> list[dict]:
        if not support_scores:
            result = []
            return result
        graph_settings = self.config.get("graph") or {}
        vector_weight = float(graph_settings.get("vector_weight", 0.0))
        scan_limit = int(graph_settings.get("vector_support_scan_limit", 100_000))
        if limit < 1:
            result = []
            return result
        scored: list[tuple[float, int, dict]] = []
        scored_bytes = 64
        retained_bytes = source_working_bytes + estimate_working_bytes(support_scores)
        repository = self.response_repository.snapshot()
        edge_count = 0
        for index, artifact in enumerate(repository.get("artifacts", {}).values()):
            run_cooperative_check(cooperative_check)
            if artifact_filter and not artifact_filter(artifact):
                continue
            similarities = []
            for reference in artifact.get("support_references", ()):
                reference_id = reference.get("id", "")
                if reference_id not in support_scores:
                    continue
                edge_count += 1
                if edge_count > scan_limit:
                    logger.warning(
                        "vector_support_scan_incomplete: relevant response support fan-out %s exceeds configured limit %s",
                        edge_count,
                        scan_limit,
                    )
                    return []
                similarities.append(support_scores.get(reference_id, 0.0))
            if not similarities:
                continue
            retained_bytes += estimate_working_bytes(artifact.get("statement_id", ""))
            require_working_memory(retained_bytes, max_working_memory_bytes)
            semantic_similarity = max(similarities)
            priority = 0.0
            score = semantic_similarity * vector_weight
            if len(scored) >= limit and (score, index) <= (scored[0][0], scored[0][1]):
                continue
            components = {
                "artifact": artifact,
                "retrieval_score": score,
                "semantic_similarity": semantic_similarity,
                "vector_weight": vector_weight,
                "priority": priority,
            }
            ranked = (score, index, components)
            if len(scored) < limit:
                ranked_bytes = estimate_working_bytes(ranked)
                heapq_heappush(scored, ranked)
                scored_bytes += ranked_bytes
            else:
                ranked_bytes = estimate_working_bytes(ranked)
                removed = heapq_heapreplace(scored, ranked)
                scored_bytes += ranked_bytes - estimate_working_bytes(removed)
            require_working_memory(retained_bytes + scored_bytes, max_working_memory_bytes)
        scored.sort(key=lambda item: (item[0], item[1]), reverse=True)
        run_cooperative_check(cooperative_check)
        result = [components for _, _, components in scored]
        return result

    def graph_lookup(self, text: str, *, evaluation_time: str = "") -> str:
        """Look up information in the knowledge graph based on input text.

        Extracts entities from the text and queries the graph for related
        information. Returns a natural language response if found.

        Args:
            text: User input text.
            evaluation_time: Optional canonical UTC evaluation timestamp. The
                shared runtime supplies this so every interface evaluates
                temporal graph assertions against the same request clock.

        Returns:
            Response string if graph has relevant info, otherwise an empty string.
        """
        client = self.graph_client
        if not client:
            result = ""
            return result
        started_ns = time_monotonic_ns()
        selected_evaluation_time = evaluation_time or datetime.now(UTC).isoformat().replace("+00:00", "Z")
        projection_timestamp(selected_evaluation_time, True, "graph lookup evaluation time")
        result = ""
        failed = False
        try:
            entities = extract_entities(text)
            facts = []
            if entities:
                # Query the canonical graph for each entity. The Proposition node
                # carries the rendered subject/predicate/object projection, so one
                # query covers the entity in either the subject or object role.
                for entity in entities:
                    records = self.graph_query(
                        GRAPH_ENTITY_FACTS_QUERY,
                        {"name": entity["text"], "evaluation_time": selected_evaluation_time},
                        raise_on_failure=True,
                    )
                    facts.extend(graph_records_to_facts(records))
            else:
                # Keyword fallback: match a canonical Entity whose primary label
                # contains a query keyword, then return its current surface triples.
                keywords = extract_keywords(normalize(text), self.config["stopwords"])
                for keyword in keywords[:3]:
                    records = self.graph_query(
                        GRAPH_KEYWORD_FACTS_QUERY,
                        {"keyword": keyword, "evaluation_time": selected_evaluation_time},
                        raise_on_failure=True,
                    )
                    facts.extend(graph_records_to_facts(records))
            facts = graph_records_to_facts(
                [{"subject": subject, "predicate": predicate, "object": obj} for subject, predicate, obj in facts]
            )
            if facts:
                result = format_graph_facts(facts)
                return result
            vector_facts = graph_records_to_facts(
                self.graph_vector_propositions(text, limit=5, evaluation_time=selected_evaluation_time)
            )
            result = format_graph_facts(vector_facts) if vector_facts else ""
            return result
        except RuntimeError:
            failed = True
            result = ""
            return result
        finally:
            available = bool(getattr(client, "available", True))
            outcome = "failure" if failed or not available else "hit" if result else "miss"
            with self.count_lock:
                record_graph_recall(self.operational_metrics, outcome, time_monotonic_ns() - started_ns)

    # =========================================================================
    # Statement Operations
    # =========================================================================

    def internal_extract_keywords(self, normalized_text: str) -> list:
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
            keyword_source: Optional text to index the conversational statement
                under instead of its pattern or rendered text.

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
        keywords = self.internal_extract_keywords(normalized)

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
        with self.mutation_lock, self.statement_lock, self.keyword_lock:
            if stmt["id"] in self.statement_index:
                raise ValueError(f"duplicate statement id: {stmt['id']}")

            # Register the pattern under the same lock that guards matching,
            # so a concurrent pattern_query never sees a half-updated matcher.
            if pattern:
                for registered_pattern in [pattern, *aliases]:
                    self.pattern_matcher.add_pattern(registered_pattern, text, that=that or "", topic=topic or "")
                    self.pattern_to_statement[registered_pattern] = stmt["id"]

            if tier == Tier.DYNAMIC:
                dynamic_count = sum(1 for s in self.statements if s["tier"] == Tier.DYNAMIC)
                while dynamic_count >= self.config["capacity"]:
                    if not eviction_mod.evict_dynamic(self):
                        break
                    dynamic_count -= 1

            self.statement_index[stmt["id"]] = len(self.statements)
            self.statements.append(stmt)
            for kw in keywords:
                if kw not in self.keywords:
                    self.keywords[kw] = keyword_entry(keyword=kw)
                self.keywords[kw]["statement_ids"].add(stmt["id"])

        result = stmt["id"]
        return result

    def query_candidates(
        self,
        text: str,
        limit: int = 5,
        statement_filter=(),
        reference_time=(),
        cooperative_check=(),
        max_working_memory_bytes: int = 0,
    ) -> dict:
        """Discover lexical candidates without accounting or session mutation."""
        if limit < 1:
            raise ValueError("limit must be at least 1")
        if statement_filter and not callable(statement_filter):
            raise ValueError("statement_filter must be callable")
        if (
            isinstance(max_working_memory_bytes, bool)
            or not isinstance(max_working_memory_bytes, int)
            or max_working_memory_bytes < 0
        ):
            raise ValueError("max_working_memory_bytes must be a nonnegative integer")

        run_cooperative_check(cooperative_check)
        normalized = normalize(text)
        uncorrected = normalized
        if self.config["use_spell_correction"]:
            with self.keyword_lock:
                vocabulary = set(self.keywords)
            normalized = correct_spelling(normalized, vocabulary)

        keywords = self.internal_extract_keywords(normalized)
        if not keywords:
            result = query_result(matches=[], keywords=[], resolved_query=text)
            result["features"] = {}
            result["diagnostics"] = {
                "spelling_correction_applied": normalized != uncorrected,
                "phrase_keywords_enabled": bool(self.config["use_phrase_keywords"]),
                "synonym_expansion_count": 0,
                "working_memory_bytes": estimate_working_bytes(keywords),
            }
            return result

        search_keywords = keywords
        synonyms_by_keyword: dict[str, tuple] = {}
        if self.config["use_synonyms"]:
            search_keywords = expand_with_synonyms(
                keywords,
                max_synonyms_per_word=self.config["max_synonyms_per_word"],
            )
            for keyword in keywords:
                run_cooperative_check(cooperative_check)
                synonyms = tuple(
                    synonym
                    for synonym in get_synonyms(keyword, max_synonyms=self.config["max_synonyms_per_word"])
                    if synonym != keyword
                )
                if synonyms:
                    synonyms_by_keyword[keyword] = synonyms

        candidate_ids: set[str] = set()
        retained_bytes = (
            estimate_working_bytes(keywords) + estimate_working_bytes(search_keywords) + estimate_working_bytes(synonyms_by_keyword)
        )
        require_working_memory(retained_bytes, max_working_memory_bytes)
        with self.keyword_lock:
            for keyword in search_keywords:
                run_cooperative_check(cooperative_check)
                if keyword in self.keywords:
                    for statement_id in self.keywords[keyword]["statement_ids"]:
                        if statement_id in candidate_ids:
                            continue
                        candidate_ids.add(statement_id)
                        retained_bytes += estimate_working_bytes(statement_id)
                        require_working_memory(retained_bytes, max_working_memory_bytes)
        if not candidate_ids:
            result = query_result(matches=[], keywords=keywords, resolved_query=text)
            result["features"] = {}
            result["diagnostics"] = {
                "spelling_correction_applied": normalized != uncorrected,
                "phrase_keywords_enabled": bool(self.config["use_phrase_keywords"]),
                "synonym_expansion_count": sum(len(values) for values in synonyms_by_keyword.values()),
                "working_memory_bytes": retained_bytes,
            }
            return result

        scored: list[tuple[float, int, dict, dict[str, float]]] = []
        scored_bytes = 64
        with self.statement_lock, self.keyword_lock:
            total = len(self.statements)
            for statement_id in candidate_ids:
                run_cooperative_check(cooperative_check)
                if statement_id not in self.statement_index:
                    continue
                index = self.statement_index[statement_id]
                statement_value = self.statements[index]
                if statement_filter and not statement_filter(statement_value):
                    continue
                components = score_statement_components(
                    statement=statement_value,
                    query_keywords=keywords,
                    keyword_index=self.keywords,
                    total_statements=total,
                    weight_base=self.config["weight_base"],
                    weight_recency=self.config["weight_recency"],
                    weight_hit_rate=self.config["weight_hit_rate"],
                    recency_half_life_seconds=self.config["recency_half_life_seconds"],
                    synonyms=synonyms_by_keyword,
                    current_time=reference_time,
                )
                score = components["score"]
                if score > 0:
                    ranked = (score, index, statement_value, components)
                    if len(scored) < limit:
                        ranked_bytes = estimate_working_bytes(ranked)
                        heapq_heappush(scored, ranked)
                        scored_bytes += ranked_bytes
                    elif (score, index) > (scored[0][0], scored[0][1]):
                        ranked_bytes = estimate_working_bytes(ranked)
                        removed = heapq_heapreplace(scored, ranked)
                        scored_bytes += ranked_bytes - estimate_working_bytes(removed)
                    require_working_memory(retained_bytes + scored_bytes, max_working_memory_bytes)
        scored.sort(key=lambda value: (value[0], value[1]), reverse=True)
        run_cooperative_check(cooperative_check)
        result = query_result(
            matches=[(statement_value, score) for score, _, statement_value, _ in scored],
            keywords=keywords,
            resolved_query=text,
        )
        result["features"] = {statement_value["id"]: components for _, _, statement_value, components in scored}
        result["diagnostics"] = {
            "spelling_correction_applied": normalized != uncorrected,
            "phrase_keywords_enabled": bool(self.config["use_phrase_keywords"]),
            "synonym_expansion_count": sum(len(values) for values in synonyms_by_keyword.values()),
            "working_memory_bytes": retained_bytes + scored_bytes,
        }
        return result

    def pattern_candidates(
        self,
        text: str,
        *,
        limit: int = 5,
        that: str = "",
        topic: str = "",
        cooperative_check=(),
        max_working_memory_bytes: int = 0,
    ) -> list[dict]:
        """Discover statement-backed pattern candidates without graph fallback or mutation."""
        if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
            raise ValueError("limit must be a positive integer")
        if (
            isinstance(max_working_memory_bytes, bool)
            or not isinstance(max_working_memory_bytes, int)
            or max_working_memory_bytes < 0
        ):
            raise ValueError("max_working_memory_bytes must be a nonnegative integer")
        run_cooperative_check(cooperative_check)
        processed_text = text
        sentences = split_sentences(processed_text) or ([processed_text] if processed_text.strip() else [])
        values = []
        retained_bytes = estimate_working_bytes(sentences)
        require_working_memory(retained_bytes, max_working_memory_bytes)
        for sentence in sentences:
            run_cooperative_check(cooperative_check)
            match_text = sentence
            if self.config["use_spell_correction"]:
                with self.keyword_lock:
                    vocabulary = set(self.keywords)
                match_text = correct_spelling(normalize(sentence), vocabulary)
            with self.statement_lock:
                matched = self.pattern_matcher.match(match_text, that=that, topic=topic)
                run_cooperative_check(cooperative_check)
                if not matched:
                    continue
                response_text, captured, thatstars, topicstars, matched_pattern, matched_topic, matched_that = matched
                selected: dict = {}
                for statement_value in self.statements:
                    carries_pattern = (
                        statement_value["pattern"] == matched_pattern or matched_pattern in statement_value["pattern_aliases"]
                    )
                    triple_match = (
                        carries_pattern and statement_value["topic"] == matched_topic and statement_value["that"] == matched_that
                    )
                    if triple_match and (not selected or statement_value["priority"] > selected.get("priority", 0)):
                        selected = statement_value
                if not selected:
                    continue
                discovery = {
                    "statement": selected,
                    "response": response_text,
                    "captured": restore_capture_case(captured, sentence),
                    "thatstars": list(thatstars),
                    "topicstars": list(topicstars),
                    "pattern": matched_pattern,
                    "topic": matched_topic,
                    "that": matched_that,
                }
                retained_bytes += estimate_working_bytes(discovery)
                require_working_memory(retained_bytes, max_working_memory_bytes)
                values.append(discovery)
            if len(values) >= limit:
                break
        return values

    def structured_proposition_projections(
        self,
        text: str,
        *,
        row_limit: int = 10,
        cooperative_check=(),
        max_working_memory_bytes: int = 0,
    ) -> list[dict]:
        """Run only fixed structured Proposition projections and reject conflicting rows."""
        if not isinstance(row_limit, int) or isinstance(row_limit, bool) or not 0 <= row_limit <= 1_000:
            raise ValueError("row_limit must be an integer from 0 through 1000")
        if (
            isinstance(max_working_memory_bytes, bool)
            or not isinstance(max_working_memory_bytes, int)
            or max_working_memory_bytes < 0
        ):
            raise ValueError("max_working_memory_bytes must be a nonnegative integer")
        client = self.graph_client
        search = getattr(client, "structured_proposition_projections", ())
        if not row_limit or not client or not callable(search):
            result = []
            return result
        run_cooperative_check(cooperative_check)
        requests: list[tuple[PropositionProjectionQuery, str]] = []
        entities = extract_entities(text)
        if entities:
            requests.extend(
                (PropositionProjectionQuery.STRUCTURED_ENTITY_V1, str(entity["text"]))
                for entity in entities[:MAX_STRUCTURED_PROPOSITION_PROJECTION_TERMS]
            )
        else:
            keywords = extract_keywords(normalize(text), self.config["stopwords"])
            requests.extend(
                (PropositionProjectionQuery.STRUCTURED_KEYWORD_V1, keyword)
                for keyword in keywords[:MAX_STRUCTURED_PROPOSITION_PROJECTION_TERMS]
            )
        retained: dict[str, dict] = {}
        for projection_id, value in requests:
            run_cooperative_check(cooperative_check)
            remaining = row_limit - len(retained)
            if not remaining:
                break
            rows = search(value, projection_id=projection_id, limit=remaining)
            if not isinstance(rows, list) or len(rows) > remaining:
                raise ValueError("structured Proposition projection boundary returned an invalid collection")
            validated_rows = [validate_proposition_projection(row) for row in rows]
            for row in validated_rows:
                proposition_id = row["proposition_id"]
                if proposition_id in retained and retained.get(proposition_id, {}) != row:
                    raise ValueError(f"conflicting structured Proposition projections for Proposition ID: {proposition_id}")
                retained[proposition_id] = row
            run_cooperative_check(cooperative_check)
            require_working_memory(
                estimate_working_bytes([proposition_projection_to_dict(projection) for projection in retained.values()]),
                max_working_memory_bytes,
            )
        result = [retained.get(proposition_id, {}) for proposition_id in sorted(retained)]
        return result

    def canonical_entity_matches(
        self,
        surface: str,
        *,
        limit: int = MAX_RELATION_CANDIDATES,
        cooperative_check=(),
    ) -> list[dict]:
        """Resolve an entity surface through the fixed graph capability."""
        client = self.graph_client
        search = getattr(client, "canonical_entity_matches", ())
        if not client or not callable(search):
            return []
        run_cooperative_check(cooperative_check)
        rows = search(surface, limit=limit)
        if not isinstance(rows, list) or len(rows) > limit:
            raise ValueError("canonical entity boundary returned an invalid collection")
        result = [canonical_entity_match_from_graph_row(row) for row in rows]
        run_cooperative_check(cooperative_check)
        return result

    def canonical_predicate_matches(
        self,
        surface: str,
        *,
        limit: int = MAX_RELATION_CANDIDATES,
        cooperative_check=(),
    ) -> list[dict]:
        """Resolve a Predicate surface through the fixed graph capability."""
        client = self.graph_client
        search = getattr(client, "canonical_predicate_matches", ())
        if not client or not callable(search):
            return []
        run_cooperative_check(cooperative_check)
        rows = search(surface, limit=limit)
        if not isinstance(rows, list) or len(rows) > limit:
            raise ValueError("canonical Predicate boundary returned an invalid collection")
        result = [canonical_predicate_match_from_graph_row(row) for row in rows]
        run_cooperative_check(cooperative_check)
        return result

    def relation_one_hop_proposition_projections(
        self,
        subject_entity_id: str,
        predicate_id: str,
        *,
        row_limit: int = MAX_RELATION_PLAN_ROWS,
        include_historical: bool = False,
        cooperative_check=(),
        max_working_memory_bytes: int = 0,
    ) -> list[dict]:
        """Run the fixed one-hop Proposition template and enforce its output boundary."""
        if isinstance(row_limit, bool) or not isinstance(row_limit, int) or not 0 <= row_limit <= MAX_RELATION_PLAN_ROWS:
            raise ValueError(f"relation row_limit must be an integer from 0 through {MAX_RELATION_PLAN_ROWS}")
        if not isinstance(include_historical, bool):
            raise ValueError("relation include_historical must be a boolean")
        client = self.graph_client
        search = getattr(client, "relation_one_hop_proposition_projections", ())
        if not row_limit or not client or not callable(search):
            return []
        run_cooperative_check(cooperative_check)
        rows = search(
            subject_entity_id,
            predicate_id,
            limit=row_limit,
            include_historical=include_historical,
        )
        if not isinstance(rows, list) or len(rows) > row_limit:
            raise ValueError("relation one-hop boundary returned an invalid collection")
        result = [validate_relation_proposition_projection(row) for row in rows]
        if any(
            row["projection"]["subject_entity_id"] != subject_entity_id or row["projection"]["predicate_id"] != predicate_id
            for row in result
        ):
            raise ValueError("relation one-hop boundary returned a Proposition outside the requested canonical binding")
        require_working_memory(
            estimate_working_bytes(
                [
                    {
                        "projection": proposition_projection_to_dict(row["projection"]),
                        "object_label": row["object_label"],
                        "object_type": row["object_type"].value,
                        "predicate_cardinality": row["predicate_cardinality"].value,
                    }
                    for row in result
                ]
            ),
            max_working_memory_bytes,
        )
        run_cooperative_check(cooperative_check)
        return result

    def query(
        self,
        text: str,
        context_id: str = "",
        limit: int = 5,
        statement_filter=(),
        record_candidates: bool = True,
    ) -> dict:
        """Retrieve matching statements.

        Args:
            text: Query text.
            context_id: Optional conversation context for query expansion.
            limit: Maximum results (default: 5).
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
        if context_id:
            session = sessions_mod.get_session(self, context_id, create_if_missing=True)
            if session:
                with self.session_lock:
                    session_touch(session)
                    expanded_text = expand_query(
                        expanded_text,
                        session["previous_response"],
                    )

        discovery = self.query_candidates(expanded_text, limit=limit, statement_filter=statement_filter)
        keywords = discovery["keywords"]
        with self.keyword_lock:
            for keyword in keywords:
                if keyword in self.keywords:
                    self.keywords[keyword]["query_count"] += 1
        if record_candidates:
            with self.statement_lock:
                for statement_value, _ in discovery["matches"]:
                    record_statement_query(statement_value)
        result = query_result(
            matches=discovery["matches"],
            keywords=keywords,
            resolved_query=discovery["resolved_query"],
        )
        return result

    def pattern_query(
        self,
        text: str,
        context_id: str = "",
        user_id: str = "",
        *,
        include_graph: bool = True,
        evaluation_time: str = "",
    ) -> tuple:
        """Query using AIML-style pattern matching.

        Supports multi-sentence input: sentences are split, matched independently,
        and responses are combined.

        Args:
            text: User input text (may contain multiple sentences).
            context_id: Optional conversation context used for turn state.
            user_id: Optional caller-owned user label. When supplied, learned
                conversational facts record this attribution.
            include_graph: Whether a no-pattern result may fall back to graph
                recall. The shared response pipeline disables this and applies
                graph precedence once for the complete turn.
            evaluation_time: Optional canonical UTC timestamp for graph recall.

        Returns:
            Tuple of (matched_statement, captured_wildcards, response_text) or ().
            For multi-sentence input, returns the candidate selected for the complete turn.
        """
        with self.count_lock:
            self.query_count += 1

        attributed_user_id = sessions_mod.normalize_user_id(user_id) if user_id else ""

        processed_text = text
        if self.config["expand_contractions"]:
            processed_text = expand_contractions(text, self.substitution_maps["contractions"])

        sentences = split_sentences(processed_text)
        if not sentences:
            # No sentences found, treat as single input
            sentences = [processed_text] if processed_text.strip() else []

        if not sentences:
            result = ()
            return result

        session = {}
        that = ""
        topic = ""
        active_topic = ""
        if context_id:
            session = sessions_mod.get_session(self, context_id, create_if_missing=True)
            if session:
                with self.session_lock:
                    that = session.get("previous_response", "")
                    topic = session.get("predicates", {}).get("topic", "")
                    active_topic = session.get("active_topic", "")

        # Input cleanup: correct typos toward the store's vocabulary before
        # matching. Fact extraction below still sees the raw sentence, since
        # its punctuation and casing carry signal.
        vocabulary: set[str] = set()
        if self.config["use_spell_correction"]:
            with self.keyword_lock:
                vocabulary = set(self.keywords)

        responses: list[str] = []
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

            # Fact admission belongs to the observed user turn, not to the
            # availability of a scripted response. Match first so a newly
            # learned fact cannot answer the sentence that introduced it,
            # then persist every admitted fact regardless of match outcome.
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
                # stored fact, but staying silent about a contradiction would
                # read as agreement when a catch-all response is available.
                with self.statement_lock:
                    existing_id = self.pattern_to_statement.get(fact_subject_upper(fact), "")
                existing = self.get_statement(existing_id)
                if existing and existing["text"]:
                    if normalize(existing["text"]) == normalize(fact["original"]):
                        reply = random_choice(KNOWN_FACT_RESPONSES)
                    else:
                        reply = random_choice(CONFLICTING_FACT_RESPONSES)
                    known_response = reply.replace("{existing}", existing["text"])

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

                # Find the statement carrying this (pattern, topic, that).
                # Among duplicates the highest priority wins, ties going to
                # the earliest stored.
                with self.statement_lock:
                    selected: dict = {}
                    for stmt in self.statements:
                        carries_pattern = stmt["pattern"] == matched_pattern or matched_pattern in stmt["pattern_aliases"]
                        triple_match = carries_pattern and stmt["topic"] == matched_topic and stmt["that"] == matched_that
                        if triple_match and (not selected or stmt["priority"] > selected.get("priority", 0)):
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
                            final_response = random_choice(LEARNED_ACKNOWLEDGMENTS)
                        elif known_response and matched_pattern == "*":
                            final_response = known_response
                        else:
                            final_response = self.process_statement_template(
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

                        that = final_response

        if not responses:
            selected_act = turn_dialogue_acts[-1] if turn_dialogue_acts else ""
            graph_response = self.graph_lookup(text, evaluation_time=evaluation_time) if include_graph else ""
            if graph_response:
                if session:
                    with self.session_lock:
                        session_update_dialogue(session, selected_act, active_topic, turn_entities, turn_fact_admissions)
                        session_update_context(session, graph_response, text)
                graph_result_tuple = ({}, [], graph_response)
                return graph_result_tuple

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
            result = ()
            return result

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

        recent_responses = session.get("response_history", [])[:REPETITION_HISTORY_SIZE] if session else []
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
            and not reports_repetition(text)
            and input_repeats(text, session.get("input_history", [])[:REPETITION_HISTORY_SIZE])
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
        if session and returned_stmt and reports_repetition(text) and pattern_has_wildcard(returned_stmt["pattern"]):
            combined_response = REPETITION_ESCAPE_RESPONSE
        elif (
            session
            and returned_stmt
            and (
                (is_pure_wildcard(returned_stmt["pattern"]) and bool(returned_stmt["template"]))
                or pattern_is_broad(returned_stmt["pattern"])
                or (
                    selected_candidate["dialogue_act"] in {DIALOGUE_CLOSING, DIALOGUE_TOPIC_SHIFT}
                    and pattern_has_wildcard(returned_stmt["pattern"])
                )
            )
        ):
            # Prefer a response grounded in the active per-user topic over
            # a generic therapist-style prompt. Learned/known fact
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
                    if not response_repeats(option, recent_responses, allow_similarity=allow_similarity):
                        combined_response = option
                        break

            catchall_render = selected_candidate["catchall_render"]
            if response_repeats(combined_response, recent_responses, allow_similarity=allow_similarity) and catchall_render:
                selected, captured, sentence, thatstars, topicstars = catchall_render
                for _ in range(8):
                    candidate = self.process_statement_template(
                        selected,
                        captured,
                        sentence,
                        session,
                        thatstars=thatstars,
                        topicstars=topicstars,
                    )
                    if not response_repeats(candidate, recent_responses, allow_similarity=allow_similarity):
                        combined_response = candidate
                        break
                else:
                    combined_response = REPETITION_ESCAPE_RESPONSE

        if redirect_repeated_input:
            for option in repeated_input_response_options():
                if not response_repeats(option, recent_responses):
                    combined_response = option
                    break
            else:
                combined_response = REPETITION_ESCAPE_RESPONSE

        # Repetition control applies to every conversational response,
        # including exact authored patterns. Repeated factual recalls are
        # useful and remain exempt.
        if (
            session
            and response_repeats(combined_response, recent_responses, allow_similarity=allow_similarity)
            and not (expected_fact_recall or expected_name_recall)
        ):
            if selected_candidate["learned"]:
                alternatives = LEARNED_ACKNOWLEDGMENTS
            elif selected_candidate["known_response"]:
                alternatives = ()
            else:
                alternatives = repetition_response_options(selected_candidate["dialogue_act"], active_topic)
            for alternative in alternatives:
                if not response_repeats(alternative, recent_responses, allow_similarity=allow_similarity):
                    combined_response = alternative
                    break

        # Output cleanup: repair casing (sentence starts, the pronoun I) that
        # lowercase wildcard captures splice into authored text.
        if self.config["polish_responses"]:
            combined_response = polish_response(combined_response)

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

    def process_statement_template(
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

        if session:
            with self.session_lock:
                context["session_id"] = session["session_id"]
                context["predicates"] = session["predicates"].copy()
                context["input_history"] = session["input_history"].copy()
                context["response_history"] = session["response_history"].copy()
                context["that_history"] = [s.copy() for s in session["that_history"]]

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
                        if triple_match and (not redirect_stmt or s["priority"] > redirect_stmt.get("priority", 0)):
                            redirect_stmt = s
                    if redirect_stmt:
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
                        template_to_process = redirect_stmt.get("template", {}) or redirect_stmt.get("text", "")
                        redirect_response = self.template_processor.process(template_to_process, new_context)
                        return redirect_response
            result = ""
            return result

        context["redirect_fn"] = redirect_fn

        def learn_fn(learn_data: dict) -> None:
            pattern = learn_data.get("pattern", "")
            template = learn_data.get("template", {})
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

        template_to_process = stmt.get("template", {}) or stmt.get("text", "")
        response = self.template_processor.process(template_to_process, context)

        # Synchronize public predicates and remove template-local scratch keys.
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
                which is what least-recently-used eviction reads.
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
                    result = False
                    return result

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
                text=fact.get("original", ""),
                pattern=subject_pattern,
                pattern_aliases=aliases,
                tier=tier,
                keyword_source=fact.get("original", ""),
                introduced_by_user_id=introduced_by_user_id,
                source_label=source_label,
            )

        result = True
        return result

    def add_fact(
        self,
        text: str,
        source_label: object = "",
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
                result = self.pattern_to_statement.get(primary_pattern, "")
                return result

        normalized_text = normalize(fact_text)
        with self.statement_lock:
            for stmt in self.statements:
                if not stmt["pattern"] and normalize(stmt["text"]) == normalized_text:
                    result = stmt["id"]
                    return result

        result = self.store(
            text=fact_text,
            tier=tier,
            introduced_by_user_id="",
            source_label=source_label,
        )
        return result

    def get_statement(self, statement_id: str) -> dict:
        """Get a statement by ID.

        Args:
            statement_id: Statement ID.

        Returns:
            Statement dict if found, {} otherwise.
        """
        with self.statement_lock:
            if statement_id in self.statement_index:
                result = self.statements[self.statement_index[statement_id]]
                return result
        result = {}
        return result

    def retire_statement(self, statement_id: str) -> bool:
        """Remove a statement from the store by id.

        Unlike capacity eviction, this is a deliberate removal of a specific
        conversational statement.
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
                result = False
                return result
            result = eviction_mod.evict_statement_at(self, self.statement_index[statement_id])
            return result

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

    def load_static_data(self, pairs: list[dict]) -> int:
        """Load the provided structured STATIC corpus into a fresh Engram.

        Static data is startup input, not recovered state. Loading fails after
        any statement, session, accepted response, or mutation receipt exists,
        so this operation cannot synchronize a running process or carry dynamic
        memory into a new one.
        """
        if not isinstance(pairs, list):
            raise ValueError("static data must be a list of objects")

        normalized_pairs = []
        for pair in pairs:
            if not isinstance(pair, dict):
                raise ValueError("each static data entry must be an object")
            text = pair.get("response", "")
            pattern = pair.get("pattern", "")
            that = pair.get("that", "")
            topic = pair.get("topic", "")
            template = pair.get("template", {})
            if not isinstance(text, str):
                raise ValueError("static responses must be strings")
            if not all(isinstance(value, str) for value in (pattern, that, topic)):
                raise ValueError("static pattern, that, and topic values must be strings")
            if not isinstance(template, dict):
                raise ValueError("static templates must be objects")
            if not text.strip() and not template:
                raise ValueError("each static data entry requires a response or template")
            normalized_pairs.append(
                {
                    "text": text,
                    "pattern": pattern,
                    "that": that,
                    "topic": topic,
                    "template": dict(template),
                }
            )

        with self.mutation_lock, self.statement_lock, self.session_lock:
            artifacts = self.response_repository.snapshot().get("artifacts", {})
            has_process_state = (
                bool(self.statements)
                or bool(self.sessions)
                or bool(artifacts)
                or self.mutation_receipts.next_sequence != 1
                or self.query_count != 0
                or self.hit_count != 0
                or self.eviction_count != 0
            )
            if has_process_state:
                raise ValueError("static data can only be loaded into a fresh Engram")
            for pair in normalized_pairs:
                self.store(
                    pair.get("text", ""),
                    tier=Tier.STATIC,
                    pattern=pair.get("pattern", ""),
                    that=pair.get("that", ""),
                    topic=pair.get("topic", ""),
                    template=pair.get("template", {}),
                )
        result = len(normalized_pairs)
        return result
