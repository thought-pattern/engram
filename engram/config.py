"""Configuration for ENGRAM.

Configurations are plain dicts built by the factory functions below.
"""

import math
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
    if not isinstance(host, str) or not host.strip():
        raise ValueError("graph host must be a non-empty string")
    if not isinstance(port, int) or isinstance(port, bool) or port < 1 or port > 65535:
        raise ValueError("graph port must be an integer between 1 and 65535")
    if not isinstance(username, str):
        raise ValueError("graph username must be a string")
    if not isinstance(password, str):
        raise ValueError("graph password must be a string")
    if not isinstance(enabled, bool):
        raise ValueError("graph enabled must be a boolean")

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
    learn_user_facts: bool = False,
    srai_depth_limit: int = 100,
    # Eviction settings
    eviction_policy: EvictionPolicy = EvictionPolicy.FIFO,
    protect_static: bool | None = None,  # Legacy option; STATIC is always protected
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

    weights = (weight_base, weight_recency, weight_hit_rate)
    if any(not math.isfinite(weight) or weight < 0 for weight in weights):
        raise ValueError("scoring weights must be finite and non-negative")
    if sum(weights) <= 0:
        raise ValueError("scoring weights must sum to a positive value")
    if not math.isfinite(recency_half_life_seconds) or recency_half_life_seconds <= 0:
        raise ValueError("recency_half_life_seconds must be finite and positive")
    if not isinstance(srai_depth_limit, int) or isinstance(srai_depth_limit, bool):
        raise ValueError("srai_depth_limit must be an integer")
    if srai_depth_limit < 1:
        raise ValueError("srai_depth_limit must be at least 1")
    if not isinstance(max_synonyms_per_word, int) or isinstance(max_synonyms_per_word, bool):
        raise ValueError("max_synonyms_per_word must be an integer")
    if max_synonyms_per_word < 0:
        raise ValueError("max_synonyms_per_word must be non-negative")
    if not math.isfinite(min_hit_rate) or not 0 <= min_hit_rate <= 1:
        raise ValueError("min_hit_rate must be between 0 and 1")
    if protect_static is False:
        raise ValueError("static statements are always protected from eviction")

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
        "learn_user_facts": learn_user_facts,
        "srai_depth_limit": srai_depth_limit,
        "eviction_policy": eviction_policy,
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
    result round-trips through JSON. Graph credentials are runtime-only and
    deliberately omitted so cache persistence cannot retain secrets.
    """
    data = dict(config)
    data["eviction_policy"] = config["eviction_policy"].value
    data["session_overflow"] = config["session_overflow"].value
    data["stopwords"] = sorted(config["stopwords"])
    if config.get("graph") is not None:
        data["graph"] = {
            key: value for key, value in config["graph"].items() if key != "password"
        }
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
