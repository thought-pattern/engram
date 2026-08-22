"""Configuration for ENGRAM.

Configurations are plain dicts built by the factory functions below.
"""

import math
import os
from types import NoneType
from typing import TypedDict, cast

import yaml

from engram.constants import DEFAULT_STOPWORDS, EMPTY_CONFIG, EvictionPolicy, SessionOverflow


class SparseConfig(TypedDict):
    enabled: bool
    include_response_text: bool
    max_query_terms: int
    max_posting_visits: int
    max_prefix_expansions: int


EMPTY_SPARSE_CONFIG = cast(SparseConfig, EMPTY_CONFIG)


def graph_config(
    host: str = "localhost",
    port: int = 7687,
    username: str = "",
    password: str = "",
    enabled: bool = False,
    vector_enabled: bool = False,
    vector_index_name: str = "claim_premise_embeddings",
    vector_model: str = "all-MiniLM-L6-v2",
    vector_model_path: str = "",
    vector_dimension: int = 384,
    vector_limit: int = 250,
    vector_support_scan_limit: int = 100000,
    vector_min_similarity: float = 0.45,
    vector_weight: float = 0.75,
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
    if not isinstance(vector_enabled, bool):
        raise ValueError("graph vector_enabled must be a boolean")
    if vector_enabled and not enabled:
        raise ValueError("graph vector_enabled requires graph enabled")
    if not isinstance(vector_index_name, str) or not vector_index_name.strip():
        raise ValueError("graph vector_index_name must be a non-empty string")
    if not isinstance(vector_model, str) or not vector_model.strip():
        raise ValueError("graph vector_model must be a non-empty string")
    if not isinstance(vector_model_path, str):
        raise ValueError("graph vector_model_path must be a string")
    if not isinstance(vector_dimension, int) or isinstance(vector_dimension, bool) or vector_dimension < 1:
        raise ValueError("graph vector_dimension must be a positive integer")
    if not isinstance(vector_limit, int) or isinstance(vector_limit, bool) or not 1 <= vector_limit <= 1000:
        raise ValueError("graph vector_limit must be an integer from 1 through 1000")
    if (
        not isinstance(vector_support_scan_limit, int)
        or isinstance(vector_support_scan_limit, bool)
        or not 1 <= vector_support_scan_limit <= 1_000_000
    ):
        raise ValueError("graph vector_support_scan_limit must be an integer from 1 through 1000000")
    if (
        not isinstance(vector_min_similarity, (int, float))
        or isinstance(vector_min_similarity, bool)
        or not math.isfinite(vector_min_similarity)
        or not 0.0 <= vector_min_similarity <= 1.0
    ):
        raise ValueError("graph vector_min_similarity must be between 0 and 1")
    if (
        not isinstance(vector_weight, (int, float))
        or isinstance(vector_weight, bool)
        or not math.isfinite(vector_weight)
        or not 0.0 <= vector_weight <= 1.0
    ):
        raise ValueError("graph vector_weight must be between 0 and 1")

    config = {
        "host": host,
        "port": port,
        "username": username,
        "password": password,
        "enabled": enabled,
        "vector_enabled": vector_enabled,
        "vector_index_name": vector_index_name.strip(),
        "vector_model": vector_model.strip(),
        "vector_model_path": vector_model_path.strip(),
        "vector_dimension": vector_dimension,
        "vector_limit": vector_limit,
        "vector_support_scan_limit": vector_support_scan_limit,
        "vector_min_similarity": float(vector_min_similarity),
        "vector_weight": float(vector_weight),
    }
    return config


def sparse_config(
    enabled: bool = False,
    include_response_text: bool = False,
    max_query_terms: int = 64,
    max_posting_visits: int = 100_000,
    max_prefix_expansions: int = 64,
) -> SparseConfig:
    """Build configuration for the rebuildable local sparse index."""
    if not isinstance(enabled, bool):
        raise ValueError("sparse enabled must be a boolean")
    if not isinstance(include_response_text, bool):
        raise ValueError("sparse include_response_text must be a boolean")
    for name, value, maximum in (
        ("max_query_terms", max_query_terms, 256),
        ("max_posting_visits", max_posting_visits, 10_000_000),
        ("max_prefix_expansions", max_prefix_expansions, 1_024),
    ):
        if not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= maximum:
            raise ValueError(f"sparse {name} must be an integer from 1 through {maximum}")
    result: SparseConfig = {
        "enabled": enabled,
        "include_response_text": include_response_text,
        "max_query_terms": max_query_terms,
        "max_posting_visits": max_posting_visits,
        "max_prefix_expansions": max_prefix_expansions,
    }
    return result


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
    stopwords=DEFAULT_STOPWORDS,
    # Input processing
    expand_contractions: bool = True,
    retrieval_rewrites_enabled: bool = False,
    learn_user_facts: bool = True,
    srai_depth_limit: int = 100,
    # Eviction settings
    eviction_policy: EvictionPolicy = EvictionPolicy.FIFO,
    protect_static: bool = True,  # Legacy option; STATIC is always protected
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
    fallback_response: str = "",  # Empty means an empty response on no match
    # Knowledge Graph settings
    graph: dict = EMPTY_CONFIG,
    # Rebuildable local sparse retrieval
    sparse: SparseConfig = EMPTY_SPARSE_CONFIG,
) -> dict:
    """Build (and validate) a configuration dict for an ENGRAM instance."""
    if not isinstance(graph, dict):
        raise ValueError("graph config must be an object")
    if not isinstance(sparse, dict):
        raise ValueError("sparse config must be an object")
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
    if not isinstance(retrieval_rewrites_enabled, bool):
        raise ValueError("retrieval_rewrites_enabled must be a boolean")

    config = {
        "capacity": capacity,
        "max_sessions": max_sessions,
        "session_ttl_seconds": session_ttl_seconds,
        "weight_base": weight_base,
        "weight_recency": weight_recency,
        "weight_hit_rate": weight_hit_rate,
        "recency_half_life_seconds": recency_half_life_seconds,
        "session_overflow": session_overflow,
        "stopwords": set(stopwords or DEFAULT_STOPWORDS),
        "expand_contractions": expand_contractions,
        "retrieval_rewrites_enabled": retrieval_rewrites_enabled,
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
        "graph": dict(graph),
        "sparse": sparse_config(**sparse) if sparse else sparse_config(),
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
    graph = config.get("graph") or {}
    data["graph"] = {key: value for key, value in graph.items() if key != "password"}
    data["sparse"] = dict(config.get("sparse") or sparse_config())
    return data


def config_from_dict(data: dict) -> dict:
    """Rebuild a validated config dict from its JSON-ready form.

    Inverse of ``config_to_dict``: enum values are mapped back to their enums,
    the stopword list back to a set, and the graph section revalidated through
    ``graph_config``. Missing keys fall back to ``engram_config`` defaults.
    """
    if not isinstance(data, dict):
        raise ValueError("serialized config must be an object")
    params = dict(data)
    if "eviction_policy" in params:
        params["eviction_policy"] = EvictionPolicy(params["eviction_policy"])
    if "session_overflow" in params:
        params["session_overflow"] = SessionOverflow(params["session_overflow"])
    if "stopwords" in params:
        params["stopwords"] = set(params["stopwords"])
    if "graph" in params:
        if isinstance(params["graph"], NoneType):
            params["graph"] = {}
        elif not isinstance(params["graph"], dict):
            raise ValueError("serialized graph config must be an object")
        elif params["graph"]:
            params["graph"] = graph_config(**params["graph"])
    if "sparse" in params:
        if isinstance(params["sparse"], NoneType):
            params["sparse"] = {}
        elif not isinstance(params["sparse"], dict):
            raise ValueError("serialized sparse config must be an object")
        elif params["sparse"]:
            params["sparse"] = sparse_config(**params["sparse"])
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
        graph_keys = {
            "host",
            "port",
            "username",
            "password",
            "enabled",
            "vector_enabled",
            "vector_index_name",
            "vector_model",
            "vector_model_path",
            "vector_dimension",
            "vector_limit",
            "vector_support_scan_limit",
            "vector_min_similarity",
            "vector_weight",
        }
        unknown_graph = set(data["graph"]) - graph_keys
        if unknown_graph:
            raise ValueError(
                f"Unknown graph config key(s) in {path}: {', '.join(sorted(unknown_graph))} "
                f"(expected: {', '.join(sorted(graph_keys))})"
            )
        data["graph"] = graph_config(**data["graph"])
    if "sparse" in data and data["sparse"]:
        sparse_keys = {
            "enabled",
            "include_response_text",
            "max_query_terms",
            "max_posting_visits",
            "max_prefix_expansions",
        }
        unknown_sparse = set(data["sparse"]) - sparse_keys
        if unknown_sparse:
            raise ValueError(
                f"Unknown sparse config key(s) in {path}: {', '.join(sorted(unknown_sparse))} "
                f"(expected: {', '.join(sorted(sparse_keys))})"
            )
        data["sparse"] = sparse_config(**data["sparse"])

    try:
        config = engram_config(**data)
    except TypeError as err:
        raise ValueError(f"Unknown config key in {path}: {err}") from err
    return config
