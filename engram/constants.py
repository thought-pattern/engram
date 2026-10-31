"""Shared sentinels, service and deployment defaults, and configuration defaults for ENGRAM.

Domain constants live in ``constants_<domain>.py`` modules beside this one. This module
keeps the configuration defaults together with every value they are built from, depends
only on the standard library, and imports no domain module, so every ENGRAM module may
import from it without risk of a cycle. A domain module may import this module or a lower
domain module, never a higher one.
"""

from datetime import UTC, datetime
from enum import Enum, StrEnum
from pathlib import Path

DEFAULT_USER_ID = "0"
# Service-boundary defaults shared by EngramCore, the gRPC servicer and the MCP tools.
DEFAULT_PROPOSE_LIMIT = 1
DEFAULT_LEARNED_SOURCE_LABEL = "unknown"
# Tiered-pipeline defaults: the calibrated score at which a conversational statement answers
# without the LLM, and how many retrieved statements the LLM receives as context.
DEFAULT_PIPELINE_HIGH_CONFIDENCE = 0.7
DEFAULT_PIPELINE_CONTEXT_LIMIT = 3
# An abandoned anonymous conversation (for example a crashed client) stops blocking new ones after this idle lease.
ANONYMOUS_CONVERSATION_LEASE_SECONDS = 300.0
# A request queued behind another turn's slot re-checks its caller's cancellation at this interval,
# because a transport cancellation cannot notify the slot condition itself.
RESOLUTION_SLOT_CANCELLATION_POLL_SECONDS = 0.05
EMPTY_MAPPING = {}
EMPTY_CONFIG: dict = {}
EMPTY_METADATA: dict = {}
MAX_TRANSIENT_RECORDS = 1_000
REGULATOR_OUTCOMES = set({"accepted", "rejected_quality", "rejected_context", "rejected_stale", "rejected_policy"})
DEFAULT_BIND_ADDRESS = "127.0.0.1:50051"
DEFAULT_GRACE_SECONDS = 10.0
# Shutdown waits this long past the grace period for gRPC's own drain to finish.
SHUTDOWN_DRAIN_MARGIN_SECONDS = 5.0
# A client's keepalive pings are its watchdog over a long silent call, so the server accepts them at any interval instead
# of answering repeated pings on a quiet call with GOAWAY.
GRPC_SERVER_OPTIONS = (("grpc.http2.max_ping_strikes", 0),)
DEFAULT_MAX_WORKERS = 10
EARLIEST_UTC = datetime.min.replace(tzinfo=UTC)
FRAME_OVERRIDES_PATH = Path(__file__).with_name("data") / "fact-phrasing.toml"
APPROVED_SEMANTIC_MODEL_ID = "sentence-transformers/all-MiniLM-L6-v2"
APPROVED_SEMANTIC_MODEL_REVISION = "826711e54e001c83835913827a843d8dd0a1def9"
APPROVED_SEMANTIC_LICENSE_ID = "apache-2.0"
APPROVED_SEMANTIC_ARTIFACT_SHA256 = "ff12d37a18ee862cd4a5b8476466bc84f06d0801da9b749023eed74251cefcb8"
APPROVED_SEMANTIC_BACKEND = "native"
APPROVED_SEMANTIC_DIMENSION = 384


# engram.config validates every section against these values; runtime readers of a
# validated configuration import them instead of re-spelling a fallback.
PROPOSITION_VECTOR_INDEX_NAME = "proposition_embeddings"
PROPOSITION_VECTOR_DIMENSION = 384
PROPOSITION_VECTOR_METRIC = "cos"
DEFAULT_GRAPH_HOST = "localhost"
DEFAULT_GRAPH_PORT = 7687
DEFAULT_GRAPH_VECTOR_MODEL = "all-MiniLM-L6-v2"
DEFAULT_GRAPH_VECTOR_LIMIT = 250
MAX_GRAPH_VECTOR_LIMIT = 1_000
DEFAULT_GRAPH_VECTOR_SUPPORT_SCAN_LIMIT = 100_000
MAX_GRAPH_VECTOR_SUPPORT_SCAN_LIMIT = 1_000_000
DEFAULT_VECTOR_MIN_SIMILARITY = 0.45
DEFAULT_GRAPH_VECTOR_WEIGHT = 0.75
DEFAULT_SPARSE_MAX_QUERY_TERMS = 64
MAX_SPARSE_QUERY_TERMS = 256
DEFAULT_SPARSE_MAX_POSTING_VISITS = 100_000
MAX_SPARSE_POSTING_VISITS = 10_000_000
DEFAULT_SPARSE_MAX_PREFIX_EXPANSIONS = 64
MAX_SPARSE_PREFIX_EXPANSIONS = 1_024
SEMANTIC_BACKENDS = set({"native", "onnx", "quantized"})
DEFAULT_SEMANTIC_BATCH_SIZE = 32
MAX_SEMANTIC_BATCH_SIZE = 1_024
DEFAULT_SEMANTIC_MAX_INPUT_BYTES = 16_384
MAX_SEMANTIC_INPUT_BYTES = 1_048_576
DEFAULT_SEMANTIC_MAX_RECORDS = 100_000
MAX_SEMANTIC_RECORDS = 10_000_000
DEFAULT_SEMANTIC_MAX_SCAN_RECORDS = 100_000
MAX_SEMANTIC_SCAN_RECORDS = 10_000_000
MAX_SEMANTIC_DIMENSION = 65_536
RERANKER_IMPLEMENTATION = "transparent_logistic"
DEFAULT_RERANKER_SHORTLIST_SIZE = 8
MAX_RERANKER_SHORTLIST_SIZE = 64
DEFAULT_RERANKER_MAX_INPUT_BYTES = 65_536
MAX_RERANKER_INPUT_BYTES = 1_048_576
DEFAULT_RERANKER_MAX_MODEL_TIME_MS = 25
MAX_RERANKER_MODEL_TIME_MS = 10_000
DEFAULT_BOT_NAME = "ENGRAM"
DEFAULT_CONVERSATION_DUPLICATE_POLICY = "error"
CONVERSATION_DUPLICATE_POLICIES = set({"error", "last", "first"})
DEFAULT_CAPACITY = 10_000
DEFAULT_MAX_SESSIONS = 10_000
DEFAULT_SESSION_TTL_SECONDS = 86_400.0
DEFAULT_WEIGHT_BASE = 0.5
DEFAULT_WEIGHT_RECENCY = 0.3
DEFAULT_WEIGHT_HIT_RATE = 0.2
DEFAULT_RECENCY_HALF_LIFE_SECONDS = 604_800.0
DEFAULT_SRAI_DEPTH_LIMIT = 100
DEFAULT_MAX_SYNONYMS_PER_WORD = 3
# The health page and API listener. Loopback by default; a configured address must be loopback or a private
# unicast address in one of HTTP_LAN_NETWORKS. The listener binds exactly this port and never moves to another.
DEFAULT_HTTP_ENABLED = True
DEFAULT_HTTP_HOST = "127.0.0.1"
DEFAULT_HTTP_PORT = 8101
DEFAULT_HTTP_REFRESH_SECONDS = 5.0
HTTP_LAN_NETWORKS = ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "fc00::/7")


class RolloutMode(StrEnum):
    """Namespace-selectable unified-resolution rollout modes."""

    DISABLED = "disabled"
    SHADOW = "shadow"
    EVIDENCE_ONLY = "evidence_only"
    REGULATED_DIRECT_ANSWER = "regulated_direct_answer"
    ROLLBACK = "rollback"


class SessionOverflow(Enum):
    """Behavior when session limit is reached."""

    REJECT = "reject"
    EXPIRE_OLDEST = "expire_oldest"
    LRU = "lru"


# Stopwords filtered out during keyword extraction. A set for O(1) membership.
DEFAULT_STOPWORDS: set[str] = {
    "a",
    "an",
    "the",
    "is",
    "are",
    "was",
    "were",
    "be",
    "been",
    "being",
    "have",
    "has",
    "had",
    "do",
    "does",
    "did",
    "will",
    "would",
    "could",
    "should",
    "may",
    "might",
    "must",
    "shall",
    "can",
    "need",
    "dare",
    "ought",
    "used",
    "to",
    "of",
    "in",
    "for",
    "on",
    "with",
    "at",
    "by",
    "from",
    "as",
    "into",
    "through",
    "during",
    "before",
    "after",
    "above",
    "below",
    "between",
    "under",
    "again",
    "further",
    "then",
    "once",
    "here",
    "there",
    "when",
    "where",
    "why",
    "how",
    "all",
    "each",
    "few",
    "more",
    "most",
    "other",
    "some",
    "such",
    "no",
    "nor",
    "not",
    "only",
    "own",
    "same",
    "so",
    "than",
    "too",
    "very",
    "just",
    "also",
}
