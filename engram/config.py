"""Configuration for ENGRAM.

Configurations are plain dictionaries returned by validating normalizers.
"""

from math import isfinite as math_isfinite
from os import path as os_path

from yaml import safe_load as yaml_safe_load

from engram.constants import DEFAULT_STOPWORDS, EMPTY_CONFIG, RolloutMode, SessionOverflow
from engram.scope import validate_visibility_scope
from engram.utilities import utility_config

EMPTY_SPARSE_CONFIG = EMPTY_CONFIG
EMPTY_SEMANTIC_CONFIG = EMPTY_CONFIG
EMPTY_RERANKER_CONFIG = EMPTY_CONFIG
EMPTY_ROLLOUT_CONFIG = EMPTY_CONFIG
EMPTY_UTILITY_CONFIG = EMPTY_CONFIG
EMPTY_CONVERSATION_CONFIG = EMPTY_CONFIG


def graph_config(
    host: str = "localhost",
    port: int = 7687,
    username: str = "",
    password: str = "",
    enabled: bool = False,
    deployment_mode: str = "",
    visibility_scope: dict = EMPTY_CONFIG,
    vector_enabled: bool = False,
    vector_index_name: str = "proposition_embeddings",
    vector_model: str = "all-MiniLM-L6-v2",
    vector_model_path: str = "",
    vector_dimension: int = 384,
    vector_limit: int = 250,
    vector_support_scan_limit: int = 100000,
    vector_min_similarity: float = 0.45,
    vector_weight: float = 0.75,
) -> dict:
    """Build a Knowledge Graph connection configuration dict.

    Connects to a Bolt graph database with the neo4j driver over host/port,
    matching the Tapestry knowledge-graph connection interface.
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
    if not isinstance(deployment_mode, str) or deployment_mode not in {
        "",
        "standalone",
        "tapestry_managed",
    }:
        raise ValueError("graph deployment_mode must be standalone or tapestry_managed")
    if enabled and deployment_mode not in {"standalone", "tapestry_managed"}:
        raise ValueError("enabled graph requires an explicit deployment_mode")
    scope = validate_visibility_scope(visibility_scope)
    if not isinstance(vector_enabled, bool):
        raise ValueError("graph vector_enabled must be a boolean")
    if vector_enabled and not enabled:
        raise ValueError("graph vector_enabled requires graph enabled")
    if vector_index_name != "proposition_embeddings":
        raise ValueError("graph vector_index_name must be proposition_embeddings")
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
        or not math_isfinite(vector_min_similarity)
        or not 0.0 <= vector_min_similarity <= 1.0
    ):
        raise ValueError("graph vector_min_similarity must be between 0 and 1")
    if (
        not isinstance(vector_weight, (int, float))
        or isinstance(vector_weight, bool)
        or not math_isfinite(vector_weight)
        or not 0.0 <= vector_weight <= 1.0
    ):
        raise ValueError("graph vector_weight must be between 0 and 1")

    config = {
        "host": host,
        "port": port,
        "username": username,
        "password": password,
        "enabled": enabled,
        "deployment_mode": deployment_mode,
        "visibility_scope": scope,
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
) -> dict:
    """Build configuration for request-local sparse retrieval."""
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
    result: dict = {
        "enabled": enabled,
        "include_response_text": include_response_text,
        "max_query_terms": max_query_terms,
        "max_posting_visits": max_posting_visits,
        "max_prefix_expansions": max_prefix_expansions,
    }
    return result


def semantic_config(
    enabled: bool = False,
    model_path: str = "",
    model_id: str = "sentence-transformers/all-MiniLM-L6-v2",
    model_version: str = "",
    license_id: str = "apache-2.0",
    artifact_sha256: str = "",
    dimension: int = 384,
    backend: str = "native",
    normalization_version: int = 1,
    batch_size: int = 32,
    max_input_bytes: int = 16_384,
    max_records: int = 100_000,
    max_scan_records: int = 100_000,
    min_similarity: float = 0.45,
) -> dict:
    """Build configuration for offline standalone semantic retrieval."""
    if not isinstance(enabled, bool):
        raise ValueError("semantic enabled must be a boolean")
    for name, value in (
        ("model_path", model_path),
        ("model_id", model_id),
        ("model_version", model_version),
        ("license_id", license_id),
        ("artifact_sha256", artifact_sha256),
        ("backend", backend),
    ):
        if not isinstance(value, str):
            raise ValueError(f"semantic {name} must be a string")
    if not model_id.strip():
        raise ValueError("semantic model_id must be a non-empty string")
    if license_id.strip().casefold() not in {"apache-2.0", "mit", "bsd-3-clause"}:
        raise ValueError("semantic license_id is not approved")
    if backend not in {"native", "onnx", "quantized"}:
        raise ValueError("semantic backend must be native, onnx, or quantized")
    if artifact_sha256 and (len(artifact_sha256) != 64 or any(value not in "0123456789abcdefABCDEF" for value in artifact_sha256)):
        raise ValueError("semantic artifact_sha256 must be a SHA-256 hex digest")
    for name, value, maximum in (
        ("dimension", dimension, 65_536),
        ("normalization_version", normalization_version, 1_000_000),
        ("batch_size", batch_size, 1_024),
        ("max_input_bytes", max_input_bytes, 1_048_576),
        ("max_records", max_records, 10_000_000),
        ("max_scan_records", max_scan_records, 10_000_000),
    ):
        if not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= maximum:
            raise ValueError(f"semantic {name} must be an integer from 1 through {maximum}")
    if (
        not isinstance(min_similarity, (int, float))
        or isinstance(min_similarity, bool)
        or not math_isfinite(min_similarity)
        or not 0.0 <= min_similarity <= 1.0
    ):
        raise ValueError("semantic min_similarity must be between 0 and 1")
    if enabled and (not model_path.strip() or not model_version.strip() or not artifact_sha256):
        raise ValueError("enabled semantic retrieval requires model_path, model_version, and artifact_sha256")
    result: dict = {
        "enabled": enabled,
        "model_path": model_path.strip(),
        "model_id": model_id.strip(),
        "model_version": model_version.strip(),
        "license_id": license_id.strip().casefold(),
        "artifact_sha256": artifact_sha256.casefold(),
        "dimension": dimension,
        "backend": backend,
        "normalization_version": normalization_version,
        "batch_size": batch_size,
        "max_input_bytes": max_input_bytes,
        "max_records": max_records,
        "max_scan_records": max_scan_records,
        "min_similarity": float(min_similarity),
    }
    return result


def reranker_config(
    enabled: bool = False,
    implementation: str = "transparent_logistic_v1",
    model_version: str = "transparent-logistic-v1",
    shortlist_size: int = 8,
    max_input_bytes: int = 65_536,
    max_model_time_ms: int = 25,
) -> dict:
    """Build the bounded optional reranker configuration."""
    if not isinstance(enabled, bool):
        raise ValueError("reranker enabled must be a boolean")
    if implementation != "transparent_logistic_v1":
        raise ValueError("reranker implementation must be transparent_logistic_v1")
    if not isinstance(model_version, str) or not model_version.strip():
        raise ValueError("reranker model_version must be a non-empty string")
    for name, value, maximum in (
        ("shortlist_size", shortlist_size, 64),
        ("max_input_bytes", max_input_bytes, 1_048_576),
        ("max_model_time_ms", max_model_time_ms, 10_000),
    ):
        if not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= maximum:
            raise ValueError(f"reranker {name} must be an integer from 1 through {maximum}")
    result: dict = {
        "enabled": enabled,
        "implementation": implementation,
        "model_version": model_version.strip(),
        "shortlist_size": shortlist_size,
        "max_input_bytes": max_input_bytes,
        "max_model_time_ms": max_model_time_ms,
    }
    return result


def rollout_config(
    policy_version: str = "rollout-v1",
    default_mode: RolloutMode = RolloutMode.REGULATED_DIRECT_ANSWER,
    namespaces: dict = EMPTY_CONFIG,
) -> dict:
    """Build the small namespace rollout policy used by unified resolution."""
    if not isinstance(policy_version, str) or not policy_version.strip():
        raise ValueError("rollout policy_version must be a non-empty string")
    if not isinstance(default_mode, RolloutMode):
        try:
            default_mode = RolloutMode(default_mode)
        except (TypeError, ValueError) as error:
            raise ValueError("rollout default_mode is invalid") from error
    if not isinstance(namespaces, dict):
        raise ValueError("rollout namespaces must be an object")
    selected_namespaces = {}
    for namespace, mode in namespaces.items():
        if not isinstance(namespace, str) or not namespace:
            raise ValueError("rollout namespace keys must be non-empty strings")
        try:
            selected_namespaces[namespace] = mode if isinstance(mode, RolloutMode) else RolloutMode(mode)
        except (TypeError, ValueError) as error:
            raise ValueError(f"rollout mode for namespace {namespace!r} is invalid") from error
    result: dict = {
        "policy_version": policy_version.strip(),
        "default_mode": default_mode,
        "namespaces": selected_namespaces,
    }
    return result


def conversation_path_list(entries, label: str) -> list:
    """Return stripped paths from a conversation file list."""
    if isinstance(entries, str) or not isinstance(entries, (list, tuple)):
        raise ValueError(f"conversation {label} must be a list of strings")
    paths = []
    for entry in entries:
        if not isinstance(entry, str) or not entry.strip():
            raise ValueError(f"conversation {label} entries must be non-empty strings")
        paths.append(entry.strip())
    return paths


def conversation_optional_path(entry, label: str) -> str:
    """Return one optional conversation path. Blank means the file is unused."""
    if not isinstance(entry, str):
        raise ValueError(f"conversation {label} must be a string")
    result = entry.strip()
    return result


def conversation_config(
    bot_name: str = "ENGRAM",
    seed_files: list | tuple = (),
    duplicate_policy: str = "error",
    set_files: list | tuple = (),
    map_files: list | tuple = (),
    properties_file: str = "",
    predicate_file: str = "",
    substitution_file: str = "",
) -> dict:
    """Build the conversation persona and the files loaded at startup.

    An empty ``seed_files`` list loads no categories. Paths stored here are
    used as given; ``load_config`` resolves paths from a YAML file before this
    runs. Set, map, property, predicate, and substitution files load before
    the categories. This function does not require the files to exist.

    ``duplicate_policy`` is ``error``, ``last``, or ``first``. ``error`` rejects
    a repeated pattern before anything is stored. ``last`` keeps the later
    file's pair. ``first`` keeps the earlier pair.
    """
    if not isinstance(bot_name, str) or not bot_name.strip():
        raise ValueError("conversation bot_name must be a non-empty string")
    if duplicate_policy not in {"error", "last", "first"}:
        raise ValueError("conversation duplicate_policy must be error, last, or first")
    config = {
        "bot_name": bot_name.strip(),
        "seed_files": conversation_path_list(seed_files, "seed_files"),
        "duplicate_policy": duplicate_policy,
        "set_files": conversation_path_list(set_files, "set_files"),
        "map_files": conversation_path_list(map_files, "map_files"),
        "properties_file": conversation_optional_path(properties_file, "properties_file"),
        "predicate_file": conversation_optional_path(predicate_file, "predicate_file"),
        "substitution_file": conversation_optional_path(substitution_file, "substitution_file"),
    }
    return config


def resolve_conversation_path_list(entries, config_path: str, key: str, kind: str) -> list:
    """Resolve a list of conversation files against the YAML file's directory.

    Absolute entries stay absolute. A missing file, a directory, or any other
    non-file raises an error that names the path and the config file.
    """
    if isinstance(entries, str) or not isinstance(entries, (list, tuple)):
        raise ValueError(f"conversation {key} in {config_path} must be a list of strings")
    config_directory = os_path.dirname(os_path.abspath(config_path))
    resolved = []
    for entry in entries:
        if not isinstance(entry, str) or not entry.strip():
            raise ValueError(f"conversation {key} in {config_path} must contain non-empty strings")
        raw_path = entry.strip()
        if os_path.isabs(raw_path):
            candidate = os_path.abspath(raw_path)
        else:
            candidate = os_path.abspath(os_path.join(config_directory, raw_path))
        if not os_path.isfile(candidate):
            raise ValueError(f"conversation {kind} {candidate} listed in {config_path} is missing or is not a file")
        resolved.append(candidate)
    return resolved


def resolve_conversation_optional_file(entry, config_path: str, key: str, kind: str) -> str:
    """Resolve one optional conversation file. A blank entry stays blank."""
    if not isinstance(entry, str):
        raise ValueError(f"conversation {key} in {config_path} must be a string")
    if not entry.strip():
        result = ""
        return result
    resolved = resolve_conversation_path_list([entry], config_path, key, kind)
    result = resolved[0]
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
    sparse: dict = EMPTY_SPARSE_CONFIG,
    # Rebuildable local standalone semantic retrieval and optional reranking
    semantic: dict = EMPTY_SEMANTIC_CONFIG,
    reranker: dict = EMPTY_RERANKER_CONFIG,
    rollout: dict = EMPTY_ROLLOUT_CONFIG,
    # Allow-listed deterministic utility operations
    utility: dict = EMPTY_UTILITY_CONFIG,
    # Persona name, categories, and AIML tables loaded once at startup
    conversation: dict = EMPTY_CONVERSATION_CONFIG,
) -> dict:
    """Build (and validate) a configuration dict for an ENGRAM instance."""
    if not isinstance(graph, dict):
        raise ValueError("graph config must be an object")
    if not isinstance(sparse, dict):
        raise ValueError("sparse config must be an object")
    if not isinstance(semantic, dict):
        raise ValueError("semantic config must be an object")
    if not isinstance(reranker, dict):
        raise ValueError("reranker config must be an object")
    if not isinstance(rollout, dict):
        raise ValueError("rollout config must be an object")
    if not isinstance(utility, dict):
        raise ValueError("utility config must be an object")
    if not isinstance(conversation, dict):
        raise ValueError("conversation config must be an object")
    if capacity < 1:
        raise ValueError("capacity must be at least 1")
    if max_sessions < 1:
        raise ValueError("max_sessions must be at least 1")
    if session_ttl_seconds <= 0:
        raise ValueError("session_ttl_seconds must be positive")

    weights = (weight_base, weight_recency, weight_hit_rate)
    if any(not math_isfinite(weight) or weight < 0 for weight in weights):
        raise ValueError("scoring weights must be finite and non-negative")
    if sum(weights) <= 0:
        raise ValueError("scoring weights must sum to a positive value")
    if not math_isfinite(recency_half_life_seconds) or recency_half_life_seconds <= 0:
        raise ValueError("recency_half_life_seconds must be finite and positive")
    if not isinstance(srai_depth_limit, int) or isinstance(srai_depth_limit, bool):
        raise ValueError("srai_depth_limit must be an integer")
    if srai_depth_limit < 1:
        raise ValueError("srai_depth_limit must be at least 1")
    if not isinstance(max_synonyms_per_word, int) or isinstance(max_synonyms_per_word, bool):
        raise ValueError("max_synonyms_per_word must be an integer")
    if max_synonyms_per_word < 0:
        raise ValueError("max_synonyms_per_word must be non-negative")
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
        "semantic": semantic_config(**semantic) if semantic else semantic_config(),
        "reranker": reranker_config(**reranker) if reranker else reranker_config(),
        "rollout": rollout_config(**rollout) if rollout else rollout_config(),
        "utility": utility_config(**utility) if utility else utility_config(),
        "conversation": conversation_config(**conversation) if conversation else conversation_config(),
    }
    return config


def config_to_dict(config: dict) -> dict:
    """Serialize a config dict to a JSON-ready dictionary.

    Enums are written by value and the stopword set as a sorted list, so the
    result round-trips through JSON. Graph credentials are runtime-only and
    deliberately omitted because runtime credentials are not configuration exports.
    """
    data = dict(config)
    data["session_overflow"] = config.get("session_overflow", SessionOverflow.REJECT).value
    data["stopwords"] = sorted(config.get("stopwords", set()))
    graph = config.get("graph", {}) or {}
    data["graph"] = {key: value for key, value in graph.items() if key != "password"}
    data["sparse"] = dict(config.get("sparse", {}) or sparse_config())
    data["semantic"] = dict(config.get("semantic", {}) or semantic_config())
    data["reranker"] = dict(config.get("reranker", {}) or reranker_config())
    rollout = config.get("rollout", {}) or rollout_config()
    data["rollout"] = {
        "policy_version": rollout["policy_version"],
        "default_mode": rollout["default_mode"].value,
        "namespaces": {namespace: mode.value for namespace, mode in rollout["namespaces"].items()},
    }
    data["utility"] = dict(config.get("utility", {}) or utility_config())
    data.get("utility", {})["plugins"] = list(data.get("utility", {})["plugins"])
    conversation = dict(config.get("conversation", {}) or conversation_config())
    conversation["seed_files"] = list(conversation.get("seed_files", ()))
    conversation["set_files"] = list(conversation.get("set_files", ()))
    conversation["map_files"] = list(conversation.get("map_files", ()))
    data["conversation"] = conversation
    return data


def config_from_dict(data: dict) -> dict:
    """Rebuild a validated config dict from its JSON-ready form.

    Inverse of ``config_to_dict``: enum values are mapped back to their enums,
    the stopword list back to a set, and nested sections revalidated. Seed paths
    are kept as stored. Missing keys fall back to ``engram_config`` defaults.
    """
    if not isinstance(data, dict):
        raise ValueError("serialized config must be an object")
    params = dict(data)
    if "session_overflow" in params:
        params["session_overflow"] = SessionOverflow(params.get("session_overflow", ""))
    if "stopwords" in params:
        params["stopwords"] = set(params.get("stopwords", set()))
    if "graph" in params:
        if not isinstance(params.get("graph", {}), dict):
            raise ValueError("serialized graph config must be an object")
        if params.get("graph", {}):
            params["graph"] = graph_config(**params.get("graph", {}))
    if "sparse" in params:
        if not isinstance(params.get("sparse", {}), dict):
            raise ValueError("serialized sparse config must be an object")
        if params.get("sparse", {}):
            params["sparse"] = sparse_config(**params.get("sparse", {}))
    if "semantic" in params:
        if not isinstance(params.get("semantic", {}), dict):
            raise ValueError("serialized semantic config must be an object")
        if params.get("semantic", {}):
            params["semantic"] = semantic_config(**params.get("semantic", {}))
    if "reranker" in params:
        if not isinstance(params.get("reranker", {}), dict):
            raise ValueError("serialized reranker config must be an object")
        if params.get("reranker", {}):
            params["reranker"] = reranker_config(**params.get("reranker", {}))
    if "rollout" in params:
        if not isinstance(params.get("rollout", {}), dict):
            raise ValueError("serialized rollout config must be an object")
        if params.get("rollout", {}):
            params["rollout"] = rollout_config(**params.get("rollout", {}))
    if "utility" in params:
        if not isinstance(params.get("utility", {}), dict):
            raise ValueError("serialized utility config must be an object")
        if params.get("utility", {}):
            params["utility"] = utility_config(**params.get("utility", {}))
    if "conversation" in params:
        if not isinstance(params.get("conversation", {}), dict):
            raise ValueError("serialized conversation config must be an object")
        if params.get("conversation", {}):
            params["conversation"] = conversation_config(**params.get("conversation", {}))
    config = engram_config(**params)
    return config


def load_config(path: str = "config.yml") -> dict:
    """Build a config dict from a YAML file.

    A missing or empty file returns the ``engram_config`` defaults. Scalar keys
    map straight through; ``session_overflow`` is given by its string value,
    and nested mappings are built with their section normalizers. Conversation
    file paths are resolved against this file's directory and stored absolute.
    A missing file, a directory, or a non-file raises ValueError naming that
    path and this file. An unknown key raises ValueError naming the key and
    the file -- a config typo should fail loudly, not be dropped.

    Args:
        path: Path to the YAML configuration file.

    Returns:
        A validated config dict.
    """
    if not os_path.exists(path):
        config = engram_config()
        return config

    with open(path, encoding="utf-8") as f:
        data = yaml_safe_load(f)
    if not data:
        config = engram_config()
        return config

    if "session_overflow" in data:
        data["session_overflow"] = SessionOverflow(data["session_overflow"])
    if "graph" in data and data["graph"]:
        graph_keys = {
            "host",
            "port",
            "username",
            "password",
            "enabled",
            "deployment_mode",
            "visibility_scope",
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
    if "semantic" in data and data["semantic"]:
        semantic_keys = set(semantic_config())
        unknown_semantic = set(data["semantic"]) - semantic_keys
        if unknown_semantic:
            raise ValueError(
                f"Unknown semantic config key(s) in {path}: {', '.join(sorted(unknown_semantic))} "
                f"(expected: {', '.join(sorted(semantic_keys))})"
            )
        data["semantic"] = semantic_config(**data["semantic"])
    if "reranker" in data and data["reranker"]:
        reranker_keys = set(reranker_config())
        unknown_reranker = set(data["reranker"]) - reranker_keys
        if unknown_reranker:
            raise ValueError(
                f"Unknown reranker config key(s) in {path}: {', '.join(sorted(unknown_reranker))} "
                f"(expected: {', '.join(sorted(reranker_keys))})"
            )
        data["reranker"] = reranker_config(**data["reranker"])
    if "rollout" in data and data["rollout"]:
        rollout_keys = set(rollout_config())
        unknown_rollout = set(data["rollout"]) - rollout_keys
        if unknown_rollout:
            raise ValueError(
                f"Unknown rollout config key(s) in {path}: {', '.join(sorted(unknown_rollout))} "
                f"(expected: {', '.join(sorted(rollout_keys))})"
            )
        data["rollout"] = rollout_config(**data["rollout"])
    if "utility" in data and data["utility"]:
        utility_keys = set(utility_config())
        unknown_utility = set(data["utility"]) - utility_keys
        if unknown_utility:
            raise ValueError(
                f"Unknown utility config key(s) in {path}: {', '.join(sorted(unknown_utility))} "
                f"(expected: {', '.join(sorted(utility_keys))})"
            )
        data["utility"] = utility_config(**data["utility"])
    if "conversation" in data and data["conversation"]:
        if not isinstance(data["conversation"], dict):
            raise ValueError(f"conversation config in {path} must be an object")
        conversation_keys = {
            "bot_name",
            "seed_files",
            "duplicate_policy",
            "set_files",
            "map_files",
            "properties_file",
            "predicate_file",
            "substitution_file",
        }
        unknown_conversation = set(data["conversation"]) - conversation_keys
        if unknown_conversation:
            raise ValueError(
                f"Unknown conversation config key(s) in {path}: {', '.join(sorted(unknown_conversation))} "
                f"(expected: {', '.join(sorted(conversation_keys))})"
            )
        section = dict(data["conversation"])
        if "seed_files" in section:
            section["seed_files"] = resolve_conversation_path_list(section["seed_files"], path, "seed_files", "seed file")
        if "set_files" in section:
            section["set_files"] = resolve_conversation_path_list(section["set_files"], path, "set_files", "set file")
        if "map_files" in section:
            section["map_files"] = resolve_conversation_path_list(section["map_files"], path, "map_files", "map file")
        for key, kind in (
            ("properties_file", "properties file"),
            ("predicate_file", "predicate file"),
            ("substitution_file", "substitution file"),
        ):
            if key in section:
                section[key] = resolve_conversation_optional_file(section[key], path, key, kind)
        data["conversation"] = conversation_config(**section)

    try:
        config = engram_config(**data)
    except TypeError as err:
        raise ValueError(f"Unknown config key in {path}: {err}") from err
    return config
