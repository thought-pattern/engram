"""The Engram engine: the statement store and the conversational pattern pipeline.

``Engram`` owns the statement store and its keyword and pattern indexes, the
session map, the graph client and its embedding model, and the local retrieval
components (sparse index, standalone semantic retriever, reranker, utility
registry). It sits above the retrieval, graph, template and dialogue modules and
below ``engram.service``, which is its only transport-neutral owner.
"""

from copy import deepcopy
from datetime import datetime, timedelta
from logging import getLogger as logging_getLogger
from threading import Lock as threading_Lock, RLock as threading_RLock, get_ident as threading_get_ident

from engram.config import validated_config
from engram.constants import DEFAULT_BOT_NAME, DEFAULT_CONVERSATION_DUPLICATE_POLICY, DEFAULT_SRAI_DEPTH_LIMIT, EMPTY_CONFIG
from engram.constants_language import DEFAULT_CONTRACTIONS, DEFAULT_GENDER, DEFAULT_PERSON, DEFAULT_PERSON2
from engram.errors import failure_origin
from engram.fact_learning import add_fact, fact_subject_patterns, learn_fact
from engram.feedback_store import FeedbackStore
from engram.graph import connect_graph
from engram.graph_recall import (
    canonical_entity_matches,
    canonical_predicate_matches,
    current_proposition_projection,
    encode_graph_query,
    graph_lookup,
    graph_query,
    graph_read_fn,
    graph_vector_proposition_projections,
    graph_vector_propositions,
    load_graph_embedding_model,
    relation_one_hop_proposition_projections,
    structured_proposition_projections,
    warm_vector_recall,
)
from engram.models import random_identifier
from engram.mutations import MutationReceiptLedger
from engram.nltk_data import NLTK_RESOURCES
from engram.pattern import PatternMatcher
from engram.pattern_query import expand_for_match, match_for, pattern_query, process_statement_template, statement_for_match
from engram.repository import ArtifactRepository
from engram.reranking import TransparentLogisticReranker
from engram.response_candidates import (
    semantic_candidates,
    sparse_candidates,
    vector_supported_match_components,
    vector_supported_match_components_from_scores,
    vector_supported_matches,
    vector_supported_proposition_match_components,
)
from engram.semantic import StandaloneSemanticRetriever
from engram.spacy_setup import SPACY_PIPELINES
from engram.sparse import SparseIndex
from engram.startup_data import load_conversation_tables, load_corpus, load_seed_files, load_static_data
from engram.statement_retrieval import query, query_candidates
from engram.statement_store import (
    detach_statement,
    evict_least_recent_dynamic,
    evict_statement,
    forget_person_facts,
    get_statement,
    internal_extract_keywords,
    owned_matcher,
    record_hit,
    replaceable_statement_ids,
    require_store_locks,
    retire_statement,
    statement_position,
    store,
)
from engram.telemetry import EMPTY_OPERATIONAL_TELEMETRY, telemetry_snapshot
from engram.template import TemplateProcessor
from engram.utilities import UtilityRegistry
from engram.validation import utc_now

logger = logging_getLogger(__name__)


class OwnedRLock:
    """Re-entrant lock that can tell whether the calling thread holds it.

    The service releases its core-wide lock around graph I/O. It must not do
    that while the same thread holds an Engram state lock, or a second thread
    holding the core lock and waiting for that state lock deadlocks with it.
    """

    def __init__(self) -> None:
        self.internal_lock = threading_RLock()
        self.internal_owner = 0
        self.internal_depth = 0

    def acquire(self, blocking: bool = True, timeout: float = -1) -> bool:
        acquired = self.internal_lock.acquire(blocking, timeout)
        if acquired:
            self.internal_owner = threading_get_ident()
            self.internal_depth += 1
        result = acquired
        return result

    def release(self) -> None:
        if self.internal_owner != threading_get_ident():
            raise RuntimeError("cannot release un-acquired lock")
        self.internal_depth -= 1
        if not self.internal_depth:
            self.internal_owner = 0
        self.internal_lock.release()

    def held_by_current_thread(self) -> bool:
        result = self.internal_owner == threading_get_ident()
        return result

    def __enter__(self) -> bool:
        result = self.acquire()
        return result

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.release()


class Engram:
    """Keyword-indexed statement store with hit-rate tracking.

    ENGRAM stores flat statements retrieved by keyword overlap and scored by
    relevance signals. It supports multiple concurrent sessions sharing a
    common statement pool.
    """

    def __init__(self, config: dict = EMPTY_CONFIG, clock: object = utc_now, identifier_source: object = random_identifier) -> None:
        """Initialize ENGRAM instance.

        Args:
            config: Configuration options, validated here through ``validated_config``;
                missing keys take their defaults and an unknown key is refused.
                ``conversation.seed_files`` is read once here, before serving.
            clock: Returns the current aware UTC datetime. Statement and session
                timestamps, hits, graph lookup and the pattern pipeline read it.
            identifier_source: Returns a fresh identifier for a statement or session
                that the caller did not name.
        """
        if not isinstance(config, dict):
            raise ValueError("config must be an object")
        if not callable(clock):
            raise ValueError("clock must be callable")
        if not callable(identifier_source):
            raise ValueError("identifier_source must be callable")
        self.config = validated_config(config, "Engram config")
        self.clock_source = clock
        self.identifier_source = identifier_source

        # Core data structures
        self.statements: list[dict] = []
        # Statement bookkeeping. ``statements`` keeps store order. Lookups go
        # through ``statement_by_id``; a position comes from the store-order
        # sequence (``statement_position``), so removing one statement does not
        # renumber the rest. DYNAMIC statements sit in a lazily checked LRU heap.
        self.statement_by_id: dict[str, dict] = {}
        self.statement_sequence: dict[str, int] = {}
        self.statement_sequences: list[int] = []
        self.next_statement_sequence = 0
        self.dynamic_statement_ids: set[str] = set()
        self.dynamic_lru: list[tuple] = []
        self.keywords: dict[str, dict] = {}
        self.sessions: dict[str, dict] = {}
        # Registered pattern or alias -> ids of the statements carrying it, in
        # store order, so matching and eviction never scan every statement.
        self.pattern_statements: dict[str, list[str]] = {}
        # A fact a person teaches in chat belongs to that person (its
        # introduced_by_user_id) and is filed in that person's own matcher, which
        # only that person's chat consults. Shared statements stay in
        # pattern_matcher. A person's matcher goes when their last category does.
        self.person_matchers: dict[str, PatternMatcher] = {}

        # The pattern matcher owns the word sets and bot properties; load_conversation_tables
        # publishes the configured ones. The persona name is applied before patterns are
        # added, so {bot:name} follows config even when no seed file is loaded.
        conversation_settings = self.config.get("conversation", {})
        seed_files = conversation_settings.get("seed_files", [])
        duplicate_policy = conversation_settings.get("duplicate_policy", DEFAULT_CONVERSATION_DUPLICATE_POLICY)
        self.maps: dict[str, dict[str, str]] = {}

        self.pattern_matcher = PatternMatcher(
            bot_properties={"name": conversation_settings.get("bot_name", DEFAULT_BOT_NAME)},
            use_stemming=self.config.get("use_stemming", False),
            use_lemmatization=self.config.get("use_lemmatization", False),
            use_spacy_lemmatization=self.config.get("use_spacy_lemmatization", False),
        )
        # Copies of the authored maps, so a loaded substitution file never edits the shared defaults.
        self.substitution_maps = {
            "contractions": dict(DEFAULT_CONTRACTIONS),
            "person": dict(DEFAULT_PERSON),
            "person2": dict(DEFAULT_PERSON2),
            "gender": dict(DEFAULT_GENDER),
            "custom": {},
        }
        self.default_predicates: dict[str, str] = {}

        # Template processor
        self.template_processor = TemplateProcessor(srai_limit=self.config.get("srai_depth_limit", DEFAULT_SRAI_DEPTH_LIMIT))

        # Concurrency control. Mutations acquire locks in this order:
        # mutation_lock, statement_lock, then keyword_lock. statement_lock also guards the pattern matchers
        # (mutated on store/evict, read on match),
        # and thereby the shared template processor, whose recursion counters
        # are only touched while the pattern pipeline holds statement_lock.
        # Template rendering can <learn>, which stores, so the pattern pipeline
        # takes mutation_lock before statement_lock as well.
        # count_lock guards the top-level metrics counters.
        self.mutation_lock = OwnedRLock()
        self.statement_lock = OwnedRLock()
        self.keyword_lock = OwnedRLock()
        self.session_lock = OwnedRLock()
        self.count_lock = threading_Lock()
        self.response_repository = ArtifactRepository()
        self.semantic_retriever = StandaloneSemanticRetriever(self.config.get("semantic", {}))
        # Per-scope sparse index, synced to the artifact snapshot on each search.
        self.sparse_index = SparseIndex()
        self.reranker = TransparentLogisticReranker(self.config.get("reranker", {}))
        self.utility_registry = UtilityRegistry(self.config.get("utility", {}))
        self.mutation_receipts = MutationReceiptLedger()
        self.feedback_store = FeedbackStore()

        self.query_count = 0
        self.hit_count = 0
        self.eviction_count = 0
        self.operational_metrics = deepcopy(EMPTY_OPERATIONAL_TELEMETRY)

        # Reject a missing file or a repeated pattern before opening a graph
        # connection and before any statement is stored. Tables load first so
        # categories can use them. Indexing waits until preflight has confirmed
        # the NLTK readers.
        self.load_conversation_tables(conversation_settings)
        static_pairs = load_seed_files(seed_files, duplicate_policy=duplicate_policy) if seed_files else []

        # Graph tooling is optional even when configured. Construction records
        # its readiness, but an unavailable graph must not prevent the local
        # cache, matcher, conversation, or regulated-response paths from serving.
        # Graph access is capability-based: production uses MemGraphConnection,
        # while deterministic benchmarks may provide the same narrow methods.
        self.internal_graph_client: object = ()
        self.graph_embedding_model: object = ()
        graph_config = self.config.get("graph", {})
        if graph_config.get("enabled", False):
            missing_connection_fields = sorted({"host", "port", "username"} - set(graph_config))
            if missing_connection_fields:
                raise ValueError(f"enabled graph config requires {', '.join(missing_connection_fields)}")
            self.internal_graph_client = connect_graph(
                host=graph_config.get("host", ""),
                port=graph_config.get("port", 0),
                username=graph_config.get("username", ""),
                password=graph_config.get("password", ""),
            )
        # A failed constructor returns no owner that could close the client,
        # so every later failure releases it here before propagating.
        try:
            if graph_config.get("vector_enabled", False):
                # A missing or unloadable local model leaves vector recall unready, which
                # component status reports; graph and keyword recall keep serving.
                try:
                    self.load_graph_embedding_model()
                except (OSError, ValueError, RuntimeError) as err:
                    self.graph_embedding_model = ()
                    logger.warning("Optional graph vector model is unavailable (%s)", failure_origin(err))
            try:
                self.component_status = self.preflight_components()
            except RuntimeError as err:
                raise ValueError(f"component preflight failed: {err}") from err
            if static_pairs:
                self.load_static_data(static_pairs)
        except BaseException:
            if self.internal_graph_client:
                self.graph_operation("disconnect")()
            self.internal_graph_client = ()
            raise

    @property
    def graph_client(self):
        """Return the graph client created during Engram initialization."""
        result = self.internal_graph_client
        return result

    def graph_operation(self, name: str):
        """Return the present graph client's ``name`` operation.

        Every graph client implements the full ``MemGraphConnection`` contract, so a client
        that lacks an operation is a defect and raises ``TypeError`` instead of reading as
        an empty graph.
        """
        operation = getattr(self.graph_client, name, ())
        if not callable(operation):
            raise TypeError(f"graph client does not implement {name}")
        return operation

    def graph_client_available(self) -> bool:
        """Return whether a graph client is present and reports its connection available."""
        client = self.graph_client
        if not client:
            result = False
            return result
        available = getattr(client, "available", ())
        if not isinstance(available, bool):
            raise TypeError("graph client does not report a boolean available")
        result = available
        return result

    def clock(self) -> datetime:
        """Read the injected clock and admit its documented aware UTC datetime."""
        moment = self.clock_source()
        if not isinstance(moment, datetime) or moment.tzinfo is None or moment.utcoffset() != timedelta(0):
            raise ValueError("clock must return an aware UTC datetime")
        return moment

    def preflight_components(self) -> dict:
        """Verify required components and report optional component readiness."""
        graph_settings = self.config.get("graph", {})
        graph_enabled = bool(graph_settings.get("enabled", False))
        vector_enabled = bool(graph_settings.get("vector_enabled", False))
        sparse_enabled = bool(self.config.get("sparse", {}).get("enabled", False))
        spacy_full_enabled = any(
            self.config.get(name, False) for name in ("use_spacy_facts", "use_spacy_lemmatization", "use_phrase_keywords")
        )
        # Availability is re-checked for every construction; the shared readers load once per
        # process, before any concurrent request can race their lazy first load.
        NLTK_RESOURCES.verify()
        NLTK_RESOURCES.require()
        if vector_enabled and not graph_enabled:
            raise RuntimeError("vector recall requires graph access to be enabled")
        graph_ready = graph_enabled and self.graph_client_available()
        vector_ready = False
        if vector_enabled and graph_ready and self.graph_embedding_model:
            try:
                vector_ready = self.warm_vector_recall()
            except (RuntimeError, ValueError) as err:
                logger.warning("Optional graph vector recall is unavailable (%s)", failure_origin(err))
        # Every spaCy consumer requires the pre-provisioned model; loading it here makes a
        # missing install fail Engram construction instead of the first request using it.
        spacy_ready = spacy_full_enabled or graph_enabled
        if spacy_ready:
            SPACY_PIPELINES.pipeline()

        result = {
            "nltk": {"enabled": True, "ready": True},
            "graph": {"enabled": graph_enabled, "ready": graph_ready},
            "vector": {"enabled": vector_enabled, "ready": vector_ready},
            "sparse": {"enabled": sparse_enabled, "ready": sparse_enabled},
            "semantic": self.semantic_retriever.health(),
            "reranker": self.reranker.health(),
            "utility": self.utility_registry.health(),
            "spacy": {"enabled": spacy_full_enabled or graph_enabled, "ready": spacy_ready},
        }
        return result

    def component_status_snapshot(self) -> dict:
        """Return current readiness without contacting optional dependencies."""
        result = {name: dict(value) for name, value in self.component_status.items()}
        graph = result.get("graph", {})
        graph["ready"] = bool(graph.get("enabled", False)) and self.graph_client_available()
        vector = result.get("vector", {})
        vector_was_ready = vector.get("ready", False)
        vector["ready"] = bool(
            vector.get("enabled", False) and graph.get("ready", False) and self.graph_embedding_model and vector_was_ready
        )
        sparse = result.get("sparse", {})
        sparse["ready"] = bool(sparse.get("enabled", False))
        result["graph"] = graph
        result["vector"] = vector
        result["sparse"] = sparse
        result["semantic"] = self.semantic_retriever.health()
        result["reranker"] = self.reranker.health()
        result["utility"] = self.utility_registry.health()
        return result

    def operational_telemetry_snapshot(self) -> dict:
        """Return isolated fixed-cardinality process telemetry."""
        with self.count_lock:
            result = telemetry_snapshot(self.operational_metrics)
            return result

    @property
    def statement_index(self) -> dict[str, int]:
        """Return statement id -> position in ``statements``, built on request."""
        with self.statement_lock:
            result = {stmt.get("id", ""): position for position, stmt in enumerate(self.statements)}
            return result

    def holds_state_lock(self) -> bool:
        """Return whether the calling thread holds any Engram state lock."""
        result = any(
            lock.held_by_current_thread()
            for lock in (self.mutation_lock, self.statement_lock, self.keyword_lock, self.session_lock)
        )
        return result

    # Startup data loading, in engram.startup_data.
    load_conversation_tables = load_conversation_tables
    load_corpus = load_corpus
    load_static_data = load_static_data

    # Graph recall, in engram.graph_recall.
    graph_query = graph_query
    graph_read_fn = graph_read_fn
    load_graph_embedding_model = load_graph_embedding_model
    encode_graph_query = encode_graph_query
    graph_vector_propositions = graph_vector_propositions
    graph_vector_proposition_projections = graph_vector_proposition_projections
    current_proposition_projection = current_proposition_projection
    warm_vector_recall = warm_vector_recall
    structured_proposition_projections = structured_proposition_projections
    canonical_entity_matches = canonical_entity_matches
    canonical_predicate_matches = canonical_predicate_matches
    relation_one_hop_proposition_projections = relation_one_hop_proposition_projections
    graph_lookup = graph_lookup

    # Accepted-response candidate discovery, in engram.response_candidates.
    sparse_candidates = sparse_candidates
    semantic_candidates = semantic_candidates
    vector_supported_matches = vector_supported_matches
    vector_supported_match_components = vector_supported_match_components
    vector_supported_proposition_match_components = vector_supported_proposition_match_components
    vector_supported_match_components_from_scores = vector_supported_match_components_from_scores

    # Statement store, in engram.statement_store.
    internal_extract_keywords = internal_extract_keywords
    store = store
    replaceable_statement_ids = replaceable_statement_ids
    owned_matcher = owned_matcher
    statement_position = statement_position
    get_statement = get_statement
    retire_statement = retire_statement
    forget_person_facts = forget_person_facts
    require_store_locks = require_store_locks
    evict_statement = evict_statement
    detach_statement = detach_statement
    evict_least_recent_dynamic = evict_least_recent_dynamic
    record_hit = record_hit

    # Keyword retrieval, in engram.statement_retrieval.
    query_candidates = query_candidates
    query = query

    # Pattern pipeline, in engram.pattern_query.
    match_for = match_for
    statement_for_match = statement_for_match
    expand_for_match = expand_for_match
    pattern_query = pattern_query
    process_statement_template = process_statement_template

    # Fact learning, in engram.fact_learning.
    learn_fact = learn_fact
    fact_subject_patterns = fact_subject_patterns
    add_fact = add_fact
