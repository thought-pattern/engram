"""Configuration for ENGRAM.

Configurations are plain dicts built by the factory functions below.
"""

from enum import Enum

from engram.nltk_data import DEFAULT_STOPWORDS


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
    use_lemmatization: bool = True,  # Enable WordNet-lemmatized matching (precise)
    use_synonyms: bool = True,  # Enable synonym expansion at query time
    max_synonyms_per_word: int = 3,  # Maximum synonyms to consider per word
    use_spacy_facts: bool = False,  # Opt-in spaCy dependency-parse fact extraction
    use_spacy_lemmatization: bool = False,  # spaCy POS-aware lemmas in the matcher
    use_phrase_keywords: bool = False,  # Noun-chunk phrase keywords for retrieval
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
        "use_lemmatization": use_lemmatization,
        "use_synonyms": use_synonyms,
        "max_synonyms_per_word": max_synonyms_per_word,
        "use_spacy_facts": use_spacy_facts,
        "use_spacy_lemmatization": use_spacy_lemmatization,
        "use_phrase_keywords": use_phrase_keywords,
        "fallback_response": fallback_response,
        "graph": graph,
    }


# Scalar/bool/numeric config keys that map straight from a YAML file.
_YAML_SCALAR_KEYS = (
    "capacity",
    "max_sessions",
    "session_ttl_seconds",
    "weight_base",
    "weight_recency",
    "weight_hit_rate",
    "expand_contractions",
    "srai_depth_limit",
    "protect_static",
    "min_hit_rate",
    "use_stemming",
    "use_lemmatization",
    "use_synonyms",
    "max_synonyms_per_word",
    "use_spacy_facts",
    "use_spacy_lemmatization",
    "use_phrase_keywords",
    "fallback_response",
)


def load_config(path: str = "config.yml") -> dict:
    """Build an EngramConfig dict from a YAML file.

    A missing file, an empty file, or any omitted key falls back to the
    EngramConfig defaults. Enum fields are given by their string value
    (``eviction_policy``, ``session_overflow``); a ``graph`` mapping is built
    into a GraphConfig.

    Args:
        path: Path to the YAML configuration file.

    Returns:
        A validated EngramConfig dict.
    """
    import os

    if not os.path.exists(path):
        return EngramConfig()

    import yaml

    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f)
    if not data:
        return EngramConfig()

    kwargs = {key: data[key] for key in _YAML_SCALAR_KEYS if key in data}
    if "eviction_policy" in data:
        kwargs["eviction_policy"] = EvictionPolicy(data["eviction_policy"])
    if "session_overflow" in data:
        kwargs["session_overflow"] = SessionOverflow(data["session_overflow"])
    if data.get("graph"):
        kwargs["graph"] = GraphConfig(**data["graph"])

    return EngramConfig(**kwargs)
