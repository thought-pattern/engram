"""Core ENGRAM implementation."""

import heapq
import logging
import random
import threading
import time
from collections.abc import Mapping
from difflib import SequenceMatcher
from pathlib import Path

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
    MAX_STRUCTURED_CLAIM_PROJECTION_TERMS,
    PERSISTENCE_STATUS_SCHEMA_VERSION,
    PERSISTENCE_VERSION,
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
from engram.eligibility import NamespaceEpochState
from engram.errors import InvalidRequestError
from engram.facts_spacy import extract_facts
from engram.feedback import FeedbackStore
from engram.graph import (
    CanonicalEntityMatch,
    CanonicalPredicateMatch,
    ClaimProjection,
    ClaimProjectionQuery,
    RelationClaimProjection,
    canonical_entity_match_from_graph_row,
    canonical_predicate_match_from_graph_row,
    claim_projection_to_dict,
    create_graph_client,
    is_write_cypher,
    validate_claim_projection,
    validate_relation_claim_projection,
)
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
    index_state_exact_lookup,
    index_state_support_lookup,
    index_state_support_scan_plan,
    projection_from_statement,
    validate_index_projection,
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
from engram.scoring import score_statement_components
from engram.semantic import SemanticIndexCheckReport, SemanticIndexState, SemanticSearchResult, StandaloneSemanticIndexOwner
from engram.spacy_setup import get_nlp
from engram.sparse import SparseIndexCheckReport, SparseIndexOwner, SparseIndexState, SparseSearchResult
from engram.substitutions import expand_contractions, split_sentences, substitution_maps
from engram.telemetry import operational_telemetry, record_rebuild, telemetry_snapshot
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
from engram.utilities import UtilityRegistry

logger = logging.getLogger(__name__)


def _run_cooperative_check(check=()) -> None:
    """Run an optional resolver-owned cooperative callback."""
    if check:
        if not callable(check):
            raise ValueError("cooperative_check must be callable")
        check()


def _estimate_working_bytes(value: object, seen=()) -> int:
    """Return a conservative, bounded-size estimate for resolution working data."""
    visited = seen if isinstance(seen, set) else set()
    if isinstance(value, str):
        result = len(value.encode("utf-8")) + 49
        return result
    if isinstance(value, bytes):
        result = len(value) + 33
        return result
    if isinstance(value, (bool, int, float)):
        result = 32
        return result
    identity = id(value)
    if identity in visited:
        result = 0
        return result
    visited.add(identity)
    if isinstance(value, dict):
        result = 64 + sum(
            _estimate_working_bytes(key, visited) + _estimate_working_bytes(item, visited) for key, item in value.items()
        )
        return result
    if isinstance(value, (list, tuple, set)):
        result = 64 + sum(_estimate_working_bytes(item, visited) for item in value)
        return result
    result = len(str(value).encode("utf-8")) + 64
    return result


def _require_working_memory(estimated_bytes: int, maximum_bytes: int) -> None:
    """Raise before retaining work that exceeds a resolver's memory estimate."""
    if maximum_bytes and estimated_bytes > maximum_bytes:
        raise MemoryError("resolution working-memory estimate exceeded")


def _reports_repetition(text: str) -> bool:
    normalized_text = normalize(text)
    result = any(marker in normalized_text for marker in REPETITION_FEEDBACK_MARKERS)
    return result


def _response_repeats(candidate: str, recent_responses: list[str], *, allow_similarity: bool = True) -> bool:
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


def _input_repeats(candidate: str, recent_inputs: list[str]) -> bool:
    normalized_candidate = normalize(candidate)
    result = bool(normalized_candidate and any(normalized_candidate == normalize(recent_input) for recent_input in recent_inputs))
    return result


def _pattern_has_wildcard(pattern: str) -> bool:
    result = any(word.lstrip("$") in WILDCARD_TOKENS for word in pattern.split())
    return result


def _response_cache_scope(template) -> tuple[str, str]:
    """Return the caller-owned namespace and context attached to a response."""
    if not isinstance(template, dict):
        result = "", ""
        return result
    tapestry_metadata = template.get("tapestry")
    if not isinstance(tapestry_metadata, dict):
        result = "", ""
        return result
    namespace = tapestry_metadata.get("namespace", "")
    context_fingerprint = tapestry_metadata.get("context_fingerprint", "")
    result = str(namespace), str(context_fingerprint)
    return result


def graph_records_to_facts(records: list) -> list[tuple[str, str, str]]:
    """Turn canonical graph rows into complete fact tuples."""
    facts = []
    for record in records:
        subject = record.get("subject", "")
        predicate = record.get("predicate", "")
        obj = record.get("object", "")
        if subject and predicate and obj:
            facts.append((subject, predicate, obj))
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
        self.response_repository = ArtifactRepository()
        self._sparse_index_owner = SparseIndexOwner(
            self.config.get("sparse") or {},
            self.response_repository.snapshot()["state_generation"],
        )
        self._semantic_index_owner = StandaloneSemanticIndexOwner(
            self.config.get("semantic") or {},
            self.response_repository.snapshot()["state_generation"],
        )
        self.reranker = TransparentLogisticReranker(self.config.get("reranker") or {})
        self.utility_registry = UtilityRegistry(self.config.get("utility") or {})
        self.namespace_epochs = NamespaceEpochState()
        self.mutation_receipts = MutationReceiptLedger()
        self.feedback_store = FeedbackStore()
        self.response_quarantine: tuple[object, ...] = ()
        self.persistence_status = {
            "schema_version": PERSISTENCE_STATUS_SCHEMA_VERSION,
            "ready": True,
            "source_available": False,
            "source_version": 0,
            "current_version": PERSISTENCE_VERSION,
            "migration_required": False,
            "manifest_present": False,
            "runtime_manifest_matches_source": False,
            "quarantine_count": 0,
            "quarantine_reasons": {},
            "derived_state_rebuilt": False,
            "manifest": {},
        }

        self.query_count = 0
        self.hit_count = 0
        self.eviction_count = 0
        self.operational_metrics = operational_telemetry()

        # Graph tooling is optional even when configured. Construction records
        # its readiness, but an unavailable graph must not prevent the local
        # cache, matcher, conversation, or regulated-response paths from serving.
        # Graph access is capability-based: production uses MemGraphConnection,
        # while deterministic benchmarks may provide the same narrow methods.
        self._graph_client: object = ()
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
            try:
                self._load_graph_embedding_model()
            except Exception as error:
                self._graph_embedding_model = ()
                logger.warning("Optional graph vector model is unavailable: %s", type(error).__name__)
        try:
            self.component_status = self.preflight_components()
        except RuntimeError as error:
            raise ValueError(f"component preflight failed: {error}") from error

    @property
    def graph_client(self):
        """Return the graph client created during Engram initialization."""
        result = self._graph_client
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
        if vector_enabled and not graph_enabled:
            raise RuntimeError("vector recall requires graph access to be enabled")
        graph_ready = bool(graph_enabled and self._graph_client and getattr(self._graph_client, "available", True))
        spacy_phrasing_enabled = graph_ready
        vector_ready = False
        if vector_enabled and graph_ready and self._graph_embedding_model:
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
                "enabled": self._sparse_index_owner.enabled,
                "ready": self._sparse_index_owner.available,
            },
            "semantic": self._semantic_index_owner.health(),
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
        graph["ready"] = bool(graph["enabled"] and self._graph_client and getattr(self._graph_client, "available", True))
        vector = result["vector"]
        vector["ready"] = bool(vector["enabled"] and graph["ready"] and self._graph_embedding_model and vector["ready"])
        sparse = result["sparse"]
        sparse["ready"] = bool(sparse["enabled"] and self._sparse_index_owner.available)
        result["semantic"] = self._semantic_index_owner.health()
        result["reranker"] = self.reranker.health()
        result["utility"] = self.utility_registry.health()
        return result

    def operational_telemetry_snapshot(self) -> dict:
        """Return isolated fixed-cardinality process telemetry."""
        with self.count_lock:
            result = telemetry_snapshot(self.operational_metrics)
            return result

    def _record_rebuild_telemetry(
        self,
        kind: str,
        started_ns: int,
        *,
        succeeded: bool,
        applied: bool = True,
    ) -> None:
        with self.count_lock:
            record_rebuild(
                self.operational_metrics,
                kind,
                time.monotonic_ns() - started_ns,
                succeeded=succeeded,
                applied=applied,
            )

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
            result = []
            return result
        execute_read = getattr(client, "execute_read", ())
        if not callable(execute_read):
            return []
        try:
            records = execute_read(cypher, params)
            if not isinstance(records, list):
                raise RuntimeError("graph read capability returned an invalid collection")
            return records
        except RuntimeError as err:
            logger.debug("Graph query failed (%s)", type(err).__name__)
            result = []
            return result

    def graph_read_fn(self, cypher: str, params=()) -> list:
        """Read-only graph callback for template operations.

        Both this callback and the underlying connection reject mutating
        Cypher. The duplicated boundary keeps custom graph clients read-only.
        """
        result = self.graph_query(cypher, params)
        return result

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
            result = []
            return result
        search = getattr(client, "vector_search_claims", ())
        if not callable(search):
            result = []
            return result
        try:
            embedding = self._encode_graph_query(text)
            rows = search(
                embedding,
                index_name=graph_config["vector_index_name"],
                limit=(max(1, min(1000, int(limit))) if limit else int(graph_config["vector_limit"])),
                min_similarity=float(graph_config["vector_min_similarity"]),
            )
            result = rows if isinstance(rows, list) else []
            return result
        except Exception as err:
            logger.warning("Vector graph recall unavailable; using keyword fallback (%s)", type(err).__name__)
            result = []
            return result

    def graph_vector_claim_projections(
        self,
        text: str,
        *,
        limit: int = 0,
        cooperative_check=(),
        max_working_memory_bytes: int = 0,
    ) -> list[ClaimProjection]:
        """Return strictly decoded wire-safe ANN Claim projections."""
        graph_config = self.config.get("graph") or {}
        client = self.graph_client
        if not client or not graph_config.get("enabled") or not graph_config.get("vector_enabled"):
            result = []
            return result
        search = getattr(client, "vector_search_claim_projections", ())
        if not callable(search):
            result = []
            return result
        try:
            _run_cooperative_check(cooperative_check)
            embedding = self._encode_graph_query(text)
            _require_working_memory(_estimate_working_bytes(embedding), max_working_memory_bytes)
            _run_cooperative_check(cooperative_check)
            row_limit = max(1, min(1000, int(limit))) if limit else int(graph_config["vector_limit"])
            rows = search(
                embedding,
                index_name=graph_config["vector_index_name"],
                limit=row_limit,
                min_similarity=float(graph_config["vector_min_similarity"]),
            )
            if not isinstance(rows, list) or len(rows) > row_limit:
                raise ValueError("vector Claim projection boundary returned an invalid collection")
            validated_rows = [validate_claim_projection(row) for row in rows]
            _require_working_memory(
                _estimate_working_bytes(embedding)
                + _estimate_working_bytes([claim_projection_to_dict(row) for row in validated_rows]),
                max_working_memory_bytes,
            )
            _run_cooperative_check(cooperative_check)
            return validated_rows
        except (TimeoutError, MemoryError):
            raise
        except Exception as err:
            logger.warning(
                "Vector Claim projection unavailable; omitting response-less evidence (%s)",
                type(err).__name__,
            )
            result = []
            return result

    def current_claim_projection(self, claim_id: str) -> tuple[ClaimProjection, ...]:
        """Re-read one canonical Claim through the fixed by-ID capability."""
        client = self.graph_client
        lookup = getattr(client, "claim_projection_by_id", ())
        if not client or not callable(lookup):
            result = ()
            return result
        rows = lookup(claim_id)
        if not isinstance(rows, list) or len(rows) > 1:
            raise ValueError("Claim projection revalidation boundary returned an invalid collection")
        result = tuple(validate_claim_projection(row) for row in rows)
        return result

    def warm_vector_recall(self) -> bool:
        """Load the query model and report whether the optional graph ANN path is ready."""
        graph_config = self.config.get("graph") or {}
        if not graph_config.get("vector_enabled"):
            result = False
            return result
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
            result = False
            return result
        result = True
        return result

    def index_snapshot(self) -> IndexState:
        """Return the currently visible immutable index state."""
        result = self._index_owner.snapshot()
        return result

    def exact_lookup(self, key: ScopedRetrievalKey) -> ExactLookupResult:
        """Look up one scoped exact key against a single immutable snapshot."""
        state = self.index_snapshot()
        result = index_state_exact_lookup(state, key)
        return result

    def support_lookup(self, claim_ids: tuple[str, ...]) -> SupportLookupResult:
        """Find statements supported by the supplied matched Claim IDs."""
        state = self.index_snapshot()
        result = index_state_support_lookup(state, claim_ids)
        return result

    def check_indexes(self) -> IndexCheckReport:
        """Compare live indexes with projections from current statements."""
        with self.mutation_lock:
            with self.statement_lock:
                sources = tuple(projection_from_statement(statement_value) for statement_value in self.statements)
            result = self._index_owner.check_against(sources)
            return result

    def check_index_projections(self, projections) -> IndexCheckReport:
        """Compare live indexes with one explicit projection set, including empty."""
        result = self._index_owner.check_against(tuple(projections))
        return result

    def repair_indexes(self, *, dry_run: bool = True) -> IndexRepairResult:
        """Repair indexes from current authoritative statements."""
        with self.mutation_lock, self.statement_lock:
            sources = tuple(projection_from_statement(statement_value) for statement_value in self.statements)
            result = self._index_owner.repair(sources, dry_run=dry_run)
            return result

    def repair_index_projections(self, projections, *, dry_run: bool = True) -> IndexRepairResult:
        """Repair indexes from one explicit projection set, including empty."""
        with self.mutation_lock:
            result = self._index_owner.repair(tuple(projections), dry_run=dry_run)
            return result

    def rebuild_indexes(self, *, apply: bool = False) -> IndexRepairResult:
        """Alias current-statement repair with rebuild-oriented wording."""
        if not isinstance(apply, bool):
            raise ValueError("index rebuild apply must be a boolean")
        started_ns = time.monotonic_ns()
        try:
            result = self.repair_indexes(dry_run=not apply)
        except Exception:
            self._record_rebuild_telemetry("primary", started_ns, succeeded=False, applied=apply)
            raise
        self._record_rebuild_telemetry("primary", started_ns, succeeded=True, applied=apply)
        return result

    def sparse_index_snapshot(self) -> SparseIndexState:
        """Return the current immutable sparse secondary-index state."""
        result = self._sparse_index_owner.snapshot()
        return result

    def rebuild_sparse_index(self) -> SparseIndexState:
        """Atomically rebuild sparse retrieval from authoritative artifacts."""
        repository = self.response_repository.snapshot()
        started_ns = time.monotonic_ns()
        try:
            result = self._sparse_index_owner.rebuild(
                repository["artifacts"].values(),
                repository["state_generation"],
            )
            self._record_rebuild_telemetry("sparse", started_ns, succeeded=True)
            return result
        except Exception as error:
            self._sparse_index_owner.mark_unavailable(error)
            self._record_rebuild_telemetry("sparse", started_ns, succeeded=False)
            raise

    def synchronize_sparse_index(
        self,
        repository_state: Mapping[str, object],
        changed_statement_ids: tuple[str, ...] = (),
    ) -> bool:
        """Publish a repository-derived sparse generation without affecting mutation success."""
        rebuild_started_ns = time.monotonic_ns() if not changed_statement_ids else 0
        try:
            artifacts = repository_state["artifacts"]
            generation = repository_state["state_generation"]
            if not isinstance(artifacts, Mapping) or not isinstance(generation, int):
                raise InvalidRequestError("repository state is malformed for sparse synchronization")
            if changed_statement_ids:
                self._sparse_index_owner.synchronize(artifacts, generation, changed_statement_ids)
            else:
                self._sparse_index_owner.rebuild(artifacts.values(), generation)
                self._record_rebuild_telemetry("sparse", rebuild_started_ns, succeeded=True)
            result = True
            return result
        except Exception as error:
            self._sparse_index_owner.mark_unavailable(error)
            if rebuild_started_ns:
                self._record_rebuild_telemetry("sparse", rebuild_started_ns, succeeded=False)
            logger.warning("Sparse index synchronization failed: %s", type(error).__name__)
            result = False
            return result

    def check_sparse_index(self) -> SparseIndexCheckReport:
        """Compare the live sparse index with authoritative response artifacts."""
        repository = self.response_repository.snapshot()
        result = self._sparse_index_owner.check_against(
            repository["artifacts"].values(),
            repository["state_generation"],
        )
        return result

    def sparse_candidates(
        self,
        text: str,
        scope,
        *,
        limit: int,
        max_working_memory_bytes: int,
    ) -> SparseSearchResult:
        """Search the immutable sparse index under the common memory budget."""
        result = self._sparse_index_owner.search(
            text,
            scope,
            limit=limit,
            max_working_memory_bytes=max_working_memory_bytes,
        )
        return result

    def semantic_index_snapshot(self) -> SemanticIndexState:
        """Return the immutable standalone semantic-index state."""
        return self._semantic_index_owner.snapshot()

    def rebuild_semantic_index(self) -> SemanticIndexState:
        """Atomically rebuild standalone embeddings from authoritative artifacts."""
        repository = self.response_repository.snapshot()
        started_ns = time.monotonic_ns()
        try:
            result = self._semantic_index_owner.rebuild(
                repository["artifacts"].values(),
                repository["state_generation"],
            )
            self._record_rebuild_telemetry("semantic", started_ns, succeeded=True)
            return result
        except Exception as error:
            self._semantic_index_owner.mark_unavailable(error)
            self._record_rebuild_telemetry("semantic", started_ns, succeeded=False)
            raise

    def synchronize_semantic_index(
        self,
        repository_state: Mapping[str, object],
        changed_statement_ids: tuple[str, ...] = (),
    ) -> bool:
        """Publish a repository-derived embedding generation without affecting mutation success."""
        if not self._semantic_index_owner.enabled:
            return True
        rebuild_started_ns = time.monotonic_ns() if not changed_statement_ids else 0
        try:
            artifacts = repository_state["artifacts"]
            generation = repository_state["state_generation"]
            if not isinstance(artifacts, Mapping) or not isinstance(generation, int):
                raise InvalidRequestError("repository state is malformed for semantic synchronization")
            if changed_statement_ids:
                self._semantic_index_owner.synchronize(artifacts, generation, changed_statement_ids)
            else:
                self._semantic_index_owner.rebuild(artifacts.values(), generation)
                self._record_rebuild_telemetry("semantic", rebuild_started_ns, succeeded=True)
            return True
        except Exception as error:
            self._semantic_index_owner.mark_unavailable(error)
            if rebuild_started_ns:
                self._record_rebuild_telemetry("semantic", rebuild_started_ns, succeeded=False)
            logger.warning("Standalone semantic index synchronization failed: %s", type(error).__name__)
            return False

    def check_semantic_index(self) -> SemanticIndexCheckReport:
        """Compare standalone semantic projections with authoritative artifacts."""
        repository = self.response_repository.snapshot()
        return self._semantic_index_owner.check_against(
            repository["artifacts"].values(),
            repository["state_generation"],
        )

    def semantic_candidates(
        self,
        text: str,
        scope,
        *,
        limit: int,
        max_vector_results: int,
        max_working_memory_bytes: int,
        cooperative_check=(),
    ) -> SemanticSearchResult:
        """Search standalone request embeddings under shared request budgets."""
        return self._semantic_index_owner.search(
            text,
            scope,
            limit=limit,
            max_vector_results=max_vector_results,
            max_working_memory_bytes=max_working_memory_bytes,
            cooperative_check=cooperative_check,
        )

    def add_index_projection(self, projection: IndexProjection) -> IndexState:
        """Atomically add one generic index projection."""
        try:
            validated_projection = validate_index_projection(projection)
        except InvalidRequestError as error:
            raise ValueError("projection must be an IndexProjection") from error
        with self.mutation_lock:
            result = self._index_owner.add(validated_projection)
            return result

    def replace_index_projection(self, projection: IndexProjection) -> IndexState:
        """Atomically replace one generic index projection."""
        try:
            validated_projection = validate_index_projection(projection)
        except InvalidRequestError as error:
            raise ValueError("projection must be an IndexProjection") from error
        with self.mutation_lock:
            result = self._index_owner.replace(validated_projection)
            return result

    def remove_index_projection(self, statement_id: str) -> IndexState:
        """Atomically remove one generic index projection."""
        with self.mutation_lock:
            result = self._index_owner.remove(statement_id)
            return result

    def update_index_support(self, statement_id: str, support_claim_ids: tuple[str, ...]) -> IndexState:
        """Atomically replace only one projection's support Claim IDs."""
        with self.mutation_lock:
            result = self._index_owner.update_support(statement_id, support_claim_ids)
            return result

    def _replace_statement_index_projection(self, statement_value: dict) -> None:
        projection = projection_from_statement(statement_value)
        if self._index_owner.has_projection(projection["statement_id"]):
            self._index_owner.replace_in_place(projection)
        else:
            self._index_owner.add_in_place(projection)

    def _remove_index_projection_if_present(self, statement_id: str) -> None:
        if self._index_owner.has_projection(statement_id):
            self._index_owner.remove_in_place(statement_id)

    def vector_supported_matches(
        self,
        text: str,
        *,
        limit: int,
        statement_filter=(),
    ) -> list[tuple[dict, float]]:
        """Rank scoped cached responses through their KG support Claims."""
        result = [
            (match["statement"], match["retrieval_score"])
            for match in self.vector_supported_match_components(
                text,
                limit=limit,
                statement_filter=statement_filter,
            )
        ]
        return result

    def vector_supported_match_components(
        self,
        text: str,
        *,
        limit: int,
        statement_filter=(),
        cooperative_check=(),
        max_working_memory_bytes: int = 0,
    ) -> list[dict]:
        """Return support matches with raw similarity separate from legacy ranking."""
        _run_cooperative_check(cooperative_check)
        rows = self.graph_vector_claims(text, limit=limit)
        result = self.vector_supported_claim_match_components(
            rows,
            limit=limit,
            statement_filter=statement_filter,
            cooperative_check=cooperative_check,
            max_working_memory_bytes=max_working_memory_bytes,
        )
        return result

    def vector_supported_claim_match_components(
        self,
        rows: list[dict],
        *,
        limit: int,
        statement_filter=(),
        cooperative_check=(),
        max_working_memory_bytes: int = 0,
    ) -> list[dict]:
        """Apply the legacy support intersection to already-discovered Claim hits."""
        _run_cooperative_check(cooperative_check)
        _require_working_memory(_estimate_working_bytes(rows), max_working_memory_bytes)
        support_scores = {str(row.get("claim_id")): float(row.get("similarity") or 0.0) for row in rows if row.get("claim_id")}
        result = self._vector_supported_match_components_from_scores(
            support_scores,
            source_working_bytes=_estimate_working_bytes(rows),
            limit=limit,
            statement_filter=statement_filter,
            cooperative_check=cooperative_check,
            max_working_memory_bytes=max_working_memory_bytes,
        )
        return result

    def _vector_supported_match_components_from_scores(
        self,
        support_scores: Mapping[str, float],
        *,
        source_working_bytes: int,
        limit: int,
        statement_filter=(),
        cooperative_check=(),
        max_working_memory_bytes: int = 0,
    ) -> list[dict]:
        if not support_scores:
            result = []
            return result
        graph_settings = self.config.get("graph") or {}
        vector_weight = float(graph_settings["vector_weight"])
        scan_limit = int(graph_settings.get("vector_support_scan_limit", MAX_INDEX_SUPPORT_SCAN_EDGES))
        state = self._index_owner._trusted_snapshot()
        scan_plan = index_state_support_scan_plan(state, tuple(support_scores), scan_limit)
        if not scan_plan["complete"]:
            logger.warning(
                "vector_support_scan_incomplete: matched support fan-out %s exceeds configured limit %s; abstaining",
                scan_plan["edge_count"],
                scan_plan["scan_limit"],
            )
            result = []
            return result
        if limit < 1:
            result = []
            return result
        scored: list[tuple[float, int, dict]] = []
        scored_bytes = 64
        seen_statement_ids = set()
        retained_bytes = source_working_bytes + _estimate_working_bytes(support_scores)
        with self.statement_lock:
            for claim_id in scan_plan["queried_claim_ids"]:
                for statement_id in reversed(state["claim_to_statements"].get(claim_id, ())):
                    _run_cooperative_check(cooperative_check)
                    if statement_id in seen_statement_ids:
                        continue
                    seen_statement_ids.add(statement_id)
                    retained_bytes += _estimate_working_bytes(statement_id)
                    _require_working_memory(retained_bytes, max_working_memory_bytes)
                    index = self.statement_index.get(statement_id, -1)
                    if index < 0:
                        continue
                    statement_value = self.statements[index]
                    if statement_filter and not statement_filter(statement_value):
                        continue
                    similarities = [
                        support_scores[support_id]
                        for support_id in state["statement_to_claims"].get(statement_id, ())
                        if support_id in support_scores
                    ]
                    if not similarities:
                        continue
                    semantic_similarity = max(similarities)
                    priority = float(statement_value.get("priority", 0))
                    score = semantic_similarity * vector_weight + priority
                    if len(scored) >= limit and (score, index) <= (scored[0][0], scored[0][1]):
                        continue
                    components = {
                        "statement": statement_value,
                        "retrieval_score": score,
                        "semantic_similarity": semantic_similarity,
                        "vector_weight": vector_weight,
                        "priority": priority,
                    }
                    ranked = (score, index, components)
                    if len(scored) < limit:
                        ranked_bytes = _estimate_working_bytes(ranked)
                        heapq.heappush(scored, ranked)
                        scored_bytes += ranked_bytes
                    else:
                        ranked_bytes = _estimate_working_bytes(ranked)
                        removed = heapq.heapreplace(scored, ranked)
                        scored_bytes += ranked_bytes - _estimate_working_bytes(removed)
                    _require_working_memory(retained_bytes + scored_bytes, max_working_memory_bytes)
        scored.sort(key=lambda item: (item[0], item[1]), reverse=True)
        _run_cooperative_check(cooperative_check)
        result = [components for _, _, components in scored]
        return result

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
            result = ""
            return result

        entities = extract_entities(text)
        if not entities:
            # Keyword fallback: match a canonical Entity whose primary label
            # contains a query keyword, then return the surface triples of the
            # claims it is the subject of.
            keywords = extract_keywords(normalize(text), self.config["stopwords"])
            if not keywords:
                result = ""
                return result
            facts = []
            for kw in keywords[:3]:
                records = self.graph_query(GRAPH_KEYWORD_FACTS_QUERY, {"keyword": kw})
                facts.extend(graph_records_to_facts(records))
            if facts:
                formatted = format_graph_facts(facts)
                return formatted
            vector_facts = graph_records_to_facts(self.graph_vector_claims(text, limit=5))
            result = format_graph_facts(vector_facts) if vector_facts else ""
            return result

        # Query the canonical graph for each entity. The Claim node carries the
        # rendered subject/predicate/object projection, so one query covers the
        # entity in either the subject or object role.
        facts = []
        for entity in entities:
            records = self.graph_query(GRAPH_ENTITY_FACTS_QUERY, {"name": entity["text"]})
            facts.extend(graph_records_to_facts(records))

        if facts:
            formatted = format_graph_facts(facts)
            return formatted
        vector_facts = graph_records_to_facts(self.graph_vector_claims(text, limit=5))
        result = format_graph_facts(vector_facts) if vector_facts else ""
        return result

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
                        result = stmt["id"]
                        return result

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

            if tier == Tier.DYNAMIC:
                dynamic_count = sum(1 for s in self.statements if s["tier"] == Tier.DYNAMIC)
                while dynamic_count >= self.config["capacity"]:
                    if not eviction_mod.evict_dynamic(self):
                        # Every remaining DYNAMIC statement is protected by
                        # min_hit_rate; admit the new statement over capacity
                        # rather than drop it silently.
                        break
                    dynamic_count -= 1

            self.statement_index[stmt["id"]] = len(self.statements)
            self.statements.append(stmt)
            for kw in keywords:
                if kw not in self.keywords:
                    self.keywords[kw] = keyword_entry(keyword=kw)
                self.keywords[kw]["statement_ids"].add(stmt["id"])
            self._index_owner.add_in_place(index_projection)

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

        _run_cooperative_check(cooperative_check)
        normalized = normalize(text)
        uncorrected = normalized
        if self.config["use_spell_correction"]:
            with self.keyword_lock:
                vocabulary = set(self.keywords)
            normalized = correct_spelling(normalized, vocabulary)

        keywords = self._extract_keywords(normalized)
        if not keywords:
            result = query_result(matches=[], keywords=[], resolved_query=text)
            result["features"] = {}
            result["diagnostics"] = {
                "spelling_correction_applied": normalized != uncorrected,
                "phrase_keywords_enabled": bool(self.config["use_phrase_keywords"]),
                "synonym_expansion_count": 0,
                "working_memory_bytes": _estimate_working_bytes(keywords),
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
                _run_cooperative_check(cooperative_check)
                synonyms = tuple(
                    synonym
                    for synonym in get_synonyms(keyword, max_synonyms=self.config["max_synonyms_per_word"])
                    if synonym != keyword
                )
                if synonyms:
                    synonyms_by_keyword[keyword] = synonyms

        candidate_ids: set[str] = set()
        retained_bytes = (
            _estimate_working_bytes(keywords)
            + _estimate_working_bytes(search_keywords)
            + _estimate_working_bytes(synonyms_by_keyword)
        )
        _require_working_memory(retained_bytes, max_working_memory_bytes)
        with self.keyword_lock:
            for keyword in search_keywords:
                _run_cooperative_check(cooperative_check)
                if keyword in self.keywords:
                    for statement_id in self.keywords[keyword]["statement_ids"]:
                        if statement_id in candidate_ids:
                            continue
                        candidate_ids.add(statement_id)
                        retained_bytes += _estimate_working_bytes(statement_id)
                        _require_working_memory(retained_bytes, max_working_memory_bytes)
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
                _run_cooperative_check(cooperative_check)
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
                        ranked_bytes = _estimate_working_bytes(ranked)
                        heapq.heappush(scored, ranked)
                        scored_bytes += ranked_bytes
                    elif (score, index) > (scored[0][0], scored[0][1]):
                        ranked_bytes = _estimate_working_bytes(ranked)
                        removed = heapq.heapreplace(scored, ranked)
                        scored_bytes += ranked_bytes - _estimate_working_bytes(removed)
                    _require_working_memory(retained_bytes + scored_bytes, max_working_memory_bytes)
        scored.sort(key=lambda value: (value[0], value[1]), reverse=True)
        _run_cooperative_check(cooperative_check)
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
        _run_cooperative_check(cooperative_check)
        processed_text = text
        sentences = split_sentences(processed_text) or ([processed_text] if processed_text.strip() else [])
        values = []
        retained_bytes = _estimate_working_bytes(sentences)
        _require_working_memory(retained_bytes, max_working_memory_bytes)
        for sentence in sentences:
            _run_cooperative_check(cooperative_check)
            match_text = sentence
            if self.config["use_spell_correction"]:
                with self.keyword_lock:
                    vocabulary = set(self.keywords)
                match_text = correct_spelling(normalize(sentence), vocabulary)
            with self.statement_lock:
                matched = self.pattern_matcher.match(match_text, that=that, topic=topic)
                _run_cooperative_check(cooperative_check)
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
                    if triple_match and (not selected or statement_value["priority"] > selected["priority"]):
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
                retained_bytes += _estimate_working_bytes(discovery)
                _require_working_memory(retained_bytes, max_working_memory_bytes)
                values.append(discovery)
            if len(values) >= limit:
                break
        return values

    def structured_graph_evidence(
        self,
        text: str,
        *,
        row_limit: int = 5,
        cooperative_check=(),
        max_working_memory_bytes: int = 0,
    ) -> list[dict]:
        """Return bounded raw legacy graph rows without phrasing or pattern fallback."""
        if not isinstance(row_limit, int) or isinstance(row_limit, bool) or row_limit < 0:
            raise ValueError("row_limit must be a nonnegative integer")
        if (
            isinstance(max_working_memory_bytes, bool)
            or not isinstance(max_working_memory_bytes, int)
            or max_working_memory_bytes < 0
        ):
            raise ValueError("max_working_memory_bytes must be a nonnegative integer")
        if not row_limit or not self.graph_client:
            result = []
            return result
        _run_cooperative_check(cooperative_check)
        rows = []
        entities = extract_entities(text)
        if entities:
            for entity in entities:
                _run_cooperative_check(cooperative_check)
                rows.extend(self.graph_query(GRAPH_ENTITY_FACTS_QUERY, {"name": entity["text"]}))
                _run_cooperative_check(cooperative_check)
                _require_working_memory(_estimate_working_bytes(rows), max_working_memory_bytes)
                if len(rows) >= row_limit:
                    break
        else:
            keywords = extract_keywords(normalize(text), self.config["stopwords"])
            for keyword in keywords[:3]:
                _run_cooperative_check(cooperative_check)
                rows.extend(self.graph_query(GRAPH_KEYWORD_FACTS_QUERY, {"keyword": keyword}))
                _run_cooperative_check(cooperative_check)
                _require_working_memory(_estimate_working_bytes(rows), max_working_memory_bytes)
                if len(rows) >= row_limit:
                    break
        result = [dict(row) for row in rows[:row_limit] if isinstance(row, dict)]
        return result

    def structured_claim_projections(
        self,
        text: str,
        *,
        row_limit: int = 10,
        cooperative_check=(),
        max_working_memory_bytes: int = 0,
    ) -> list[ClaimProjection]:
        """Run only fixed structured Claim projections and reject conflicting rows."""
        if not isinstance(row_limit, int) or isinstance(row_limit, bool) or not 0 <= row_limit <= 1_000:
            raise ValueError("row_limit must be an integer from 0 through 1000")
        if (
            isinstance(max_working_memory_bytes, bool)
            or not isinstance(max_working_memory_bytes, int)
            or max_working_memory_bytes < 0
        ):
            raise ValueError("max_working_memory_bytes must be a nonnegative integer")
        client = self.graph_client
        search = getattr(client, "structured_claim_projections", ())
        if not row_limit or not client or not callable(search):
            result = []
            return result
        _run_cooperative_check(cooperative_check)
        requests: list[tuple[ClaimProjectionQuery, str]] = []
        entities = extract_entities(text)
        if entities:
            requests.extend(
                (ClaimProjectionQuery.STRUCTURED_ENTITY_V1, str(entity["text"]))
                for entity in entities[:MAX_STRUCTURED_CLAIM_PROJECTION_TERMS]
            )
        else:
            keywords = extract_keywords(normalize(text), self.config["stopwords"])
            requests.extend(
                (ClaimProjectionQuery.STRUCTURED_KEYWORD_V1, keyword)
                for keyword in keywords[:MAX_STRUCTURED_CLAIM_PROJECTION_TERMS]
            )
        retained: dict[str, ClaimProjection] = {}
        for projection_id, value in requests:
            _run_cooperative_check(cooperative_check)
            remaining = row_limit - len(retained)
            if not remaining:
                break
            rows = search(value, projection_id=projection_id, limit=remaining)
            if not isinstance(rows, list) or len(rows) > remaining:
                raise ValueError("structured Claim projection boundary returned an invalid collection")
            validated_rows = [validate_claim_projection(row) for row in rows]
            for row in validated_rows:
                claim_id = row["claim_id"]
                if claim_id in retained and retained[claim_id] != row:
                    raise ValueError(f"conflicting structured Claim projections for Claim ID: {claim_id}")
                retained[claim_id] = row
            _run_cooperative_check(cooperative_check)
            _require_working_memory(
                _estimate_working_bytes([claim_projection_to_dict(projection) for projection in retained.values()]),
                max_working_memory_bytes,
            )
        result = [retained[claim_id] for claim_id in sorted(retained)]
        return result

    def canonical_entity_matches(
        self,
        surface: str,
        *,
        limit: int = MAX_RELATION_CANDIDATES,
        cooperative_check=(),
    ) -> list[CanonicalEntityMatch]:
        """Resolve an entity surface through the fixed graph capability."""
        client = self.graph_client
        search = getattr(client, "canonical_entity_matches", ())
        if not client or not callable(search):
            return []
        _run_cooperative_check(cooperative_check)
        rows = search(surface, limit=limit)
        if not isinstance(rows, list) or len(rows) > limit:
            raise ValueError("canonical entity boundary returned an invalid collection")
        result = [canonical_entity_match_from_graph_row(row) for row in rows]
        _run_cooperative_check(cooperative_check)
        return result

    def canonical_predicate_matches(
        self,
        surface: str,
        *,
        limit: int = MAX_RELATION_CANDIDATES,
        cooperative_check=(),
    ) -> list[CanonicalPredicateMatch]:
        """Resolve a Predicate surface through the fixed graph capability."""
        client = self.graph_client
        search = getattr(client, "canonical_predicate_matches", ())
        if not client or not callable(search):
            return []
        _run_cooperative_check(cooperative_check)
        rows = search(surface, limit=limit)
        if not isinstance(rows, list) or len(rows) > limit:
            raise ValueError("canonical Predicate boundary returned an invalid collection")
        result = [canonical_predicate_match_from_graph_row(row) for row in rows]
        _run_cooperative_check(cooperative_check)
        return result

    def relation_one_hop_claim_projections(
        self,
        subject_entity_id: str,
        predicate_id: str,
        *,
        row_limit: int = MAX_RELATION_PLAN_ROWS,
        include_historical: bool = False,
        cooperative_check=(),
        max_working_memory_bytes: int = 0,
    ) -> list[RelationClaimProjection]:
        """Run the fixed one-hop Claim template and enforce its output boundary."""
        if isinstance(row_limit, bool) or not isinstance(row_limit, int) or not 0 <= row_limit <= MAX_RELATION_PLAN_ROWS:
            raise ValueError(f"relation row_limit must be an integer from 0 through {MAX_RELATION_PLAN_ROWS}")
        if not isinstance(include_historical, bool):
            raise ValueError("relation include_historical must be a boolean")
        client = self.graph_client
        search = getattr(client, "relation_one_hop_claim_projections", ())
        if not row_limit or not client or not callable(search):
            return []
        _run_cooperative_check(cooperative_check)
        rows = search(
            subject_entity_id,
            predicate_id,
            limit=row_limit,
            include_historical=include_historical,
        )
        if not isinstance(rows, list) or len(rows) > row_limit:
            raise ValueError("relation one-hop boundary returned an invalid collection")
        result = [validate_relation_claim_projection(row) for row in rows]
        if any(
            row["projection"]["subject_entity_id"] != subject_entity_id or row["projection"]["predicate_id"] != predicate_id
            for row in result
        ):
            raise ValueError("relation one-hop boundary returned a Claim outside the requested canonical binding")
        _require_working_memory(
            _estimate_working_bytes(
                [
                    {
                        "projection": claim_projection_to_dict(row["projection"]),
                        "object_label": row["object_label"],
                        "object_type": row["object_type"].value,
                        "predicate_cardinality": row["predicate_cardinality"].value,
                    }
                    for row in result
                ]
            ),
            max_working_memory_bytes,
        )
        _run_cooperative_check(cooperative_check)
        return result

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
        if session_id:
            session = sessions_mod.get_session(self, session_id, create_if_missing=True)
            if session:
                with self.session_lock:
                    that = session["previous_response"]
                    topic = session["predicates"].get("topic", "")
                    active_topic = session.get("active_topic", "")

        # Input cleanup: correct typos toward the store's vocabulary before
        # matching. Fact extraction below still sees the raw sentence, since
        # its punctuation and casing carry signal.
        vocabulary: set[str] = set()
        if self.config["use_spell_correction"]:
            with self.keyword_lock:
                vocabulary = set(self.keywords)

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

                        if not first_stmt:
                            first_stmt = selected
                            first_captured = captured
                        that = final_response

        if not responses:
            selected_act = turn_dialogue_acts[-1] if turn_dialogue_acts else ""
            graph_response = self.graph_lookup(text)
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
                        if triple_match and (not redirect_stmt or s["priority"] > redirect_stmt["priority"]):
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
                        template_to_process = redirect_stmt["template"] or redirect_stmt["text"]
                        redirect_response = self.template_processor.process(template_to_process, new_context)
                        return redirect_response
            result = ""
            return result

        context["redirect_fn"] = redirect_fn

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

        template_to_process = stmt["template"] or stmt["text"]
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
                text=fact["original"],
                pattern=subject_pattern,
                pattern_aliases=aliases,
                tier=tier,
                keyword_source=fact["original"],
                introduced_by_user_id=introduced_by_user_id,
                source_label=source_label,
            )

        result = True
        return result

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


def fork_engram(parent: Engram, static_corpus=(), config=()) -> Engram:
    """Create an ENGRAM with copied dynamic statements and fresh runtime state."""
    instance = Engram(config=config or parent.config)
    if static_corpus:
        instance.load_corpus(static_corpus, tier=Tier.STATIC)
    with parent.statement_lock:
        for statement in parent.statements:
            if statement["tier"] == Tier.DYNAMIC:
                instance.store(
                    statement["text"],
                    tier=Tier.DYNAMIC,
                    pattern=statement["pattern"],
                    that=statement["that"],
                    topic=statement["topic"],
                    template=statement["template"],
                )
    result = instance
    return result
