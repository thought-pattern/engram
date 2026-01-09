"""Configuration for ENGRAM."""

from dataclasses import dataclass, field
from enum import Enum


class SessionOverflow(Enum):
    """Behavior when session limit is reached."""

    REJECT = "reject"
    EXPIRE_OLDEST = "expire_oldest"
    LRU = "lru"


class EvictionPolicy(Enum):
    """Policy for evicting DYNAMIC categories when at capacity."""

    FIFO = "fifo"  # First-in, first-out (oldest evicted first)
    LRU = "lru"  # Least recently used (oldest last-hit evicted)
    LFU = "lfu"  # Least frequently used (lowest hit count evicted)
    HIT_RATE = "hit_rate"  # Lowest hit rate (hits/queries) evicted


@dataclass
class GraphConfig:
    """Configuration for Knowledge Graph connection."""

    driver: str = "memgraph"  # memgraph, neo4j
    uri: str = "bolt://localhost:7687"
    username: str = ""
    password: str = ""
    database: str = ""
    enabled: bool = False


# Default stopwords as specified
DEFAULT_STOPWORDS: frozenset[str] = frozenset(
    [
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
    ]
)


@dataclass
class EngramConfig:
    """Configuration for an ENGRAM instance."""

    # Capacity settings
    capacity: int = 10000
    max_sessions: int = 10000
    session_ttl_seconds: float = 86400.0  # 24 hours

    # Scoring weights
    weight_base: float = 0.5
    weight_recency: float = 0.3
    weight_hit_rate: float = 0.2

    # Session overflow behavior
    session_overflow: SessionOverflow = SessionOverflow.LRU

    # Stopwords
    stopwords: frozenset[str] = field(default_factory=lambda: DEFAULT_STOPWORDS)

    # Input processing
    expand_contractions: bool = True
    srai_depth_limit: int = 100

    # Eviction settings
    eviction_policy: EvictionPolicy = EvictionPolicy.FIFO
    protect_static: bool = True  # Never evict STATIC tier
    min_hit_rate: float = 0.0  # Protect categories above this hit rate

    # Matching enhancements
    use_stemming: bool = True  # Enable stemmed matching (run matches running)
    use_synonyms: bool = True  # Enable synonym expansion at query time
    max_synonyms_per_word: int = 3  # Maximum synonyms to consider per word

    # Fallback response when no pattern matches
    fallback_response: str = ""  # Empty means return None on no match

    # Knowledge Graph settings
    graph: object = None

    def __post_init__(self) -> None:
        """Validate configuration."""
        if self.capacity < 1:
            raise ValueError("capacity must be at least 1")
        if self.max_sessions < 1:
            raise ValueError("max_sessions must be at least 1")
        if self.session_ttl_seconds <= 0:
            raise ValueError("session_ttl_seconds must be positive")

        # Validate weights sum reasonably
        total_weight = self.weight_base + self.weight_recency + self.weight_hit_rate
        if total_weight <= 0:
            raise ValueError("scoring weights must sum to a positive value")
