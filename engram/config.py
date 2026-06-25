"""Configuration for ENGRAM.

Configurations are plain dicts built by the factory functions below.
"""

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


def GraphConfig(
    driver: str = "memgraph",  # memgraph, neo4j
    uri: str = "bolt://localhost:7687",
    username: str = "",
    password: str = "",
    database: str = "",
    enabled: bool = False,
) -> dict:
    """Build a Knowledge Graph connection configuration dict."""
    return {
        "driver": driver,
        "uri": uri,
        "username": username,
        "password": password,
        "database": database,
        "enabled": enabled,
    }


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


def EngramConfig(
    # Capacity settings
    capacity: int = 10000,
    max_sessions: int = 10000,
    session_ttl_seconds: float = 86400.0,  # 24 hours
    # Scoring weights
    weight_base: float = 0.5,
    weight_recency: float = 0.3,
    weight_hit_rate: float = 0.2,
    # Session overflow behavior
    session_overflow: SessionOverflow = SessionOverflow.LRU,
    # Stopwords
    stopwords=None,
    # Input processing
    expand_contractions: bool = True,
    srai_depth_limit: int = 100,
    # Eviction settings
    eviction_policy: EvictionPolicy = EvictionPolicy.FIFO,
    protect_static: bool = True,  # Never evict STATIC tier
    min_hit_rate: float = 0.0,  # Protect categories above this hit rate
    # Matching enhancements
    use_stemming: bool = True,  # Enable stemmed matching (run matches running)
    use_synonyms: bool = True,  # Enable synonym expansion at query time
    max_synonyms_per_word: int = 3,  # Maximum synonyms to consider per word
    # Fallback response when no pattern matches
    fallback_response: str = "",  # Empty means return None on no match
    # Knowledge Graph settings
    graph: object = None,
) -> dict:
    """Build (and validate) a configuration dict for an ENGRAM instance."""
    if capacity < 1:
        raise ValueError("capacity must be at least 1")
    if max_sessions < 1:
        raise ValueError("max_sessions must be at least 1")
    if session_ttl_seconds <= 0:
        raise ValueError("session_ttl_seconds must be positive")

    # Validate weights sum reasonably
    total_weight = weight_base + weight_recency + weight_hit_rate
    if total_weight <= 0:
        raise ValueError("scoring weights must sum to a positive value")

    return {
        "capacity": capacity,
        "max_sessions": max_sessions,
        "session_ttl_seconds": session_ttl_seconds,
        "weight_base": weight_base,
        "weight_recency": weight_recency,
        "weight_hit_rate": weight_hit_rate,
        "session_overflow": session_overflow,
        "stopwords": stopwords if stopwords is not None else DEFAULT_STOPWORDS,
        "expand_contractions": expand_contractions,
        "srai_depth_limit": srai_depth_limit,
        "eviction_policy": eviction_policy,
        "protect_static": protect_static,
        "min_hit_rate": min_hit_rate,
        "use_stemming": use_stemming,
        "use_synonyms": use_synonyms,
        "max_synonyms_per_word": max_synonyms_per_word,
        "fallback_response": fallback_response,
        "graph": graph,
    }
