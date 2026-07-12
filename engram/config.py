"""Configuration for ENGRAM.

Configurations are plain dicts built by the factory functions below.
"""

import os

import yaml

from engram.constants import DEFAULT_STOPWORDS, EvictionPolicy, SessionOverflow


def graph_config(
    host: str = "localhost",
    port: int = 7687,
    username: str = "",
    password: str = "",
    enabled: bool = False,
) -> dict:
    """Build a Knowledge Graph connection configuration dict.

    Connects to MemGraph with the pymgclient driver over host/port, matching
    the Tapestry knowledge-graph connection interface.
    """
    config = {
        "host": host,
        "port": port,
        "username": username,
        "password": password,
        "enabled": enabled,
    }
    return config


def engram_config(
    # Capacity settings
    capacity: int = 10000,
    max_sessions: int = 10000,
    session_ttl_seconds: float = 86400.0,  # 24 hours
    # Scoring weights
    weight_base: float = 0.5,
    weight_recency: float = 0.3,
    weight_hit_rate: float = 0.2,
    recency_half_life_seconds: float = 604800.0,  # 7 days: recency decay half-life
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
    use_spell_correction: bool = True,  # Correct input typos toward the store vocabulary
    use_spacy_facts: bool = False,  # Opt-in spaCy dependency-parse fact extraction
    use_spacy_lemmatization: bool = False,  # spaCy POS-aware lemmas in the matcher
    use_phrase_keywords: bool = False,  # Noun-chunk phrase keywords for retrieval
    # Output processing
    polish_responses: bool = True,  # Repair casing in pattern-path responses
    # Fallback response when no pattern matches
    fallback_response: str = "",  # Empty means return None on no match
    # Knowledge Graph settings
    graph=None,  # dict from graph_config(), or None for no graph
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
    if recency_half_life_seconds <= 0:
        raise ValueError("recency_half_life_seconds must be positive")

    if stopwords is None:
        stopwords = DEFAULT_STOPWORDS

    config = {
        "capacity": capacity,
        "max_sessions": max_sessions,
        "session_ttl_seconds": session_ttl_seconds,
        "weight_base": weight_base,
        "weight_recency": weight_recency,
        "weight_hit_rate": weight_hit_rate,
        "recency_half_life_seconds": recency_half_life_seconds,
        "session_overflow": session_overflow,
        "stopwords": stopwords,
        "expand_contractions": expand_contractions,
        "srai_depth_limit": srai_depth_limit,
        "eviction_policy": eviction_policy,
        "protect_static": protect_static,
        "min_hit_rate": min_hit_rate,
        "use_stemming": use_stemming,
        "use_lemmatization": use_lemmatization,
        "use_synonyms": use_synonyms,
        "max_synonyms_per_word": max_synonyms_per_word,
        "use_spell_correction": use_spell_correction,
        "use_spacy_facts": use_spacy_facts,
        "use_spacy_lemmatization": use_spacy_lemmatization,
        "use_phrase_keywords": use_phrase_keywords,
        "polish_responses": polish_responses,
        "fallback_response": fallback_response,
        "graph": graph,
    }
    return config


def config_to_dict(config: dict) -> dict:
    """Serialize a config dict to a JSON-ready dictionary.

    Enums are written by value and the stopword set as a sorted list, so the
    result round-trips through JSON. The graph section is already a plain dict.
    """
    data = dict(config)
    data["eviction_policy"] = config["eviction_policy"].value
    data["session_overflow"] = config["session_overflow"].value
    data["stopwords"] = sorted(config["stopwords"])
    return data


def config_from_dict(data: dict) -> dict:
    """Rebuild a validated config dict from its JSON-ready form.

    Inverse of ``config_to_dict``: enum values are mapped back to their enums,
    the stopword list back to a set, and the graph section revalidated through
    ``graph_config``. Missing keys fall back to ``engram_config`` defaults.
    """
    params = dict(data)
    if "eviction_policy" in params:
        params["eviction_policy"] = EvictionPolicy(params["eviction_policy"])
    if "session_overflow" in params:
        params["session_overflow"] = SessionOverflow(params["session_overflow"])
    if "stopwords" in params:
        params["stopwords"] = set(params["stopwords"])
    if params.get("graph"):
        params["graph"] = graph_config(**params["graph"])
    config = engram_config(**params)
    return config


def load_config(path: str = "config.yml") -> dict:
    """Build a config dict from a YAML file.

    A missing or empty file returns the ``engram_config`` defaults. Scalar keys
    map straight through; ``eviction_policy`` and ``session_overflow`` are given
    by their string value, and a ``graph`` mapping is built with ``graph_config``.
    An unknown key raises ValueError naming the key and the file -- a config
    typo should fail loudly, not be dropped.

    Args:
        path: Path to the YAML configuration file.

    Returns:
        A validated config dict.
    """
    if not os.path.exists(path):
        config = engram_config()
        return config

    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f)
    if not data:
        config = engram_config()
        return config

    if "eviction_policy" in data:
        data["eviction_policy"] = EvictionPolicy(data["eviction_policy"])
    if "session_overflow" in data:
        data["session_overflow"] = SessionOverflow(data["session_overflow"])
    if "graph" in data and data["graph"]:
        graph_keys = {"host", "port", "username", "password", "enabled"}
        unknown_graph = set(data["graph"]) - graph_keys
        if unknown_graph:
            raise ValueError(
                f"Unknown graph config key(s) in {path}: {', '.join(sorted(unknown_graph))} "
                f"(expected: {', '.join(sorted(graph_keys))})"
            )
        data["graph"] = graph_config(**data["graph"])

    try:
        config = engram_config(**data)
    except TypeError as err:
        raise ValueError(f"Unknown config key in {path}: {err}") from err
    return config
