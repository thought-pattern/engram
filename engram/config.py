"""Configuration boundary for ENGRAM.

This module owns validation of Engram's configuration: each section normalizer
returns a plain dictionary with a fixed field set, and unknown keys are refused.
Defaults and limits come from ``engram.constants``; the module sits just above
the constants, scope and utility contracts and below every runtime component.
"""

from datetime import timedelta
from math import isfinite as math_isfinite
from os import path as os_path

from yaml import safe_load as yaml_safe_load

from engram.constants import (
    APPROVED_SEMANTIC_BACKEND,
    APPROVED_SEMANTIC_DIMENSION,
    APPROVED_SEMANTIC_LICENSE_ID,
    APPROVED_SEMANTIC_LICENSE_IDS,
    APPROVED_SEMANTIC_MODEL_ID,
    CONVERSATION_DUPLICATE_POLICIES,
    DEFAULT_BOT_NAME,
    DEFAULT_CAPACITY,
    DEFAULT_CONVERSATION_DUPLICATE_POLICY,
    DEFAULT_GRAPH_HOST,
    DEFAULT_GRAPH_PORT,
    DEFAULT_GRAPH_VECTOR_LIMIT,
    DEFAULT_GRAPH_VECTOR_MODEL,
    DEFAULT_GRAPH_VECTOR_SUPPORT_SCAN_LIMIT,
    DEFAULT_GRAPH_VECTOR_WEIGHT,
    DEFAULT_MAX_SESSIONS,
    DEFAULT_MAX_SYNONYMS_PER_WORD,
    DEFAULT_RECENCY_HALF_LIFE_SECONDS,
    DEFAULT_RERANKER_MAX_INPUT_BYTES,
    DEFAULT_RERANKER_MAX_MODEL_TIME_MS,
    DEFAULT_RERANKER_SHORTLIST_SIZE,
    DEFAULT_SEMANTIC_BATCH_SIZE,
    DEFAULT_SEMANTIC_MAX_INPUT_BYTES,
    DEFAULT_SEMANTIC_MAX_RECORDS,
    DEFAULT_SEMANTIC_MAX_SCAN_RECORDS,
    DEFAULT_SESSION_TTL_SECONDS,
    DEFAULT_SPARSE_MAX_POSTING_VISITS,
    DEFAULT_SPARSE_MAX_PREFIX_EXPANSIONS,
    DEFAULT_SPARSE_MAX_QUERY_TERMS,
    DEFAULT_SRAI_DEPTH_LIMIT,
    DEFAULT_STOPWORDS,
    DEFAULT_VECTOR_MIN_SIMILARITY,
    DEFAULT_WEIGHT_BASE,
    DEFAULT_WEIGHT_HIT_RATE,
    DEFAULT_WEIGHT_RECENCY,
    EMPTY_CONFIG,
    MAX_GRAPH_VECTOR_LIMIT,
    MAX_GRAPH_VECTOR_SUPPORT_SCAN_LIMIT,
    MAX_RERANKER_INPUT_BYTES,
    MAX_RERANKER_MODEL_TIME_MS,
    MAX_RERANKER_SHORTLIST_SIZE,
    MAX_SEMANTIC_BATCH_SIZE,
    MAX_SEMANTIC_DIMENSION,
    MAX_SEMANTIC_INPUT_BYTES,
    MAX_SEMANTIC_RECORDS,
    MAX_SEMANTIC_SCAN_RECORDS,
    MAX_SPARSE_POSTING_VISITS,
    MAX_SPARSE_PREFIX_EXPANSIONS,
    MAX_SPARSE_QUERY_TERMS,
    PROPOSITION_VECTOR_DIMENSION,
    PROPOSITION_VECTOR_INDEX_NAME,
    RERANKER_IMPLEMENTATION,
    SEMANTIC_BACKENDS,
    RolloutMode,
    SessionOverflow,
)
from engram.utilities import utility_config

# The config.yml beside the engram package: the repository root in a source checkout.
DEFAULT_CONFIG_PATH = os_path.join(os_path.dirname(os_path.dirname(os_path.abspath(__file__))), "config.yml")


def graph_config(
    host: str = DEFAULT_GRAPH_HOST,
    port: int = DEFAULT_GRAPH_PORT,
    username: str = "",
    password: str = "",
    enabled: bool = False,
    vector_enabled: bool = False,
    vector_index_name: str = PROPOSITION_VECTOR_INDEX_NAME,
    vector_model: str = DEFAULT_GRAPH_VECTOR_MODEL,
    vector_model_path: str = "",
    vector_dimension: int = PROPOSITION_VECTOR_DIMENSION,
    vector_limit: int = DEFAULT_GRAPH_VECTOR_LIMIT,
    vector_support_scan_limit: int = DEFAULT_GRAPH_VECTOR_SUPPORT_SCAN_LIMIT,
    vector_min_similarity: float = DEFAULT_VECTOR_MIN_SIMILARITY,
    vector_weight: float = DEFAULT_GRAPH_VECTOR_WEIGHT,
) -> dict:
    """Validate and normalize a Knowledge Graph connection configuration.

    Connects to a Bolt graph database with the neo4j driver over host/port.
    Startup checks that the graph's schema is compatible with Engram's.
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
    if vector_index_name != PROPOSITION_VECTOR_INDEX_NAME:
        raise ValueError(f"graph vector_index_name must be {PROPOSITION_VECTOR_INDEX_NAME}")
    if not isinstance(vector_model, str) or not vector_model.strip():
        raise ValueError("graph vector_model must be a non-empty string")
    if not isinstance(vector_model_path, str):
        raise ValueError("graph vector_model_path must be a string")
    if not isinstance(vector_dimension, int) or isinstance(vector_dimension, bool) or vector_dimension < 1:
        raise ValueError("graph vector_dimension must be a positive integer")
    if not isinstance(vector_limit, int) or isinstance(vector_limit, bool) or not 1 <= vector_limit <= MAX_GRAPH_VECTOR_LIMIT:
        raise ValueError(f"graph vector_limit must be an integer from 1 through {MAX_GRAPH_VECTOR_LIMIT}")
    if (
        not isinstance(vector_support_scan_limit, int)
        or isinstance(vector_support_scan_limit, bool)
        or not 1 <= vector_support_scan_limit <= MAX_GRAPH_VECTOR_SUPPORT_SCAN_LIMIT
    ):
        raise ValueError(f"graph vector_support_scan_limit must be an integer from 1 through {MAX_GRAPH_VECTOR_SUPPORT_SCAN_LIMIT}")
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
    max_query_terms: int = DEFAULT_SPARSE_MAX_QUERY_TERMS,
    max_posting_visits: int = DEFAULT_SPARSE_MAX_POSTING_VISITS,
    max_prefix_expansions: int = DEFAULT_SPARSE_MAX_PREFIX_EXPANSIONS,
) -> dict:
    """Validate configuration for sparse keyword retrieval."""
    if not isinstance(enabled, bool):
        raise ValueError("sparse enabled must be a boolean")
    if not isinstance(include_response_text, bool):
        raise ValueError("sparse include_response_text must be a boolean")
    for name, value, maximum in (
        ("max_query_terms", max_query_terms, MAX_SPARSE_QUERY_TERMS),
        ("max_posting_visits", max_posting_visits, MAX_SPARSE_POSTING_VISITS),
        ("max_prefix_expansions", max_prefix_expansions, MAX_SPARSE_PREFIX_EXPANSIONS),
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
    model_id: str = APPROVED_SEMANTIC_MODEL_ID,
    model_revision: str = "",
    license_id: str = APPROVED_SEMANTIC_LICENSE_ID,
    artifact_sha256: str = "",
    dimension: int = APPROVED_SEMANTIC_DIMENSION,
    backend: str = APPROVED_SEMANTIC_BACKEND,
    batch_size: int = DEFAULT_SEMANTIC_BATCH_SIZE,
    max_input_bytes: int = DEFAULT_SEMANTIC_MAX_INPUT_BYTES,
    max_records: int = DEFAULT_SEMANTIC_MAX_RECORDS,
    max_scan_records: int = DEFAULT_SEMANTIC_MAX_SCAN_RECORDS,
    min_similarity: float = DEFAULT_VECTOR_MIN_SIMILARITY,
) -> dict:
    """Validate and normalize configuration for offline standalone semantic retrieval."""
    if not isinstance(enabled, bool):
        raise ValueError("semantic enabled must be a boolean")
    for name, value in (
        ("model_path", model_path),
        ("model_id", model_id),
        ("model_revision", model_revision),
        ("license_id", license_id),
        ("artifact_sha256", artifact_sha256),
        ("backend", backend),
    ):
        if not isinstance(value, str):
            raise ValueError(f"semantic {name} must be a string")
    if not model_id.strip():
        raise ValueError("semantic model_id must be a non-empty string")
    if license_id.strip().casefold() not in APPROVED_SEMANTIC_LICENSE_IDS:
        raise ValueError("semantic license_id is not approved")
    if backend not in SEMANTIC_BACKENDS:
        raise ValueError(f"semantic backend must be one of {', '.join(sorted(SEMANTIC_BACKENDS))}")
    if artifact_sha256 and (len(artifact_sha256) != 64 or any(value not in "0123456789abcdefABCDEF" for value in artifact_sha256)):
        raise ValueError("semantic artifact_sha256 must be a SHA-256 hex digest")
    for name, value, maximum in (
        ("dimension", dimension, MAX_SEMANTIC_DIMENSION),
        ("batch_size", batch_size, MAX_SEMANTIC_BATCH_SIZE),
        ("max_input_bytes", max_input_bytes, MAX_SEMANTIC_INPUT_BYTES),
        ("max_records", max_records, MAX_SEMANTIC_RECORDS),
        ("max_scan_records", max_scan_records, MAX_SEMANTIC_SCAN_RECORDS),
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
    if enabled and (not model_path.strip() or not model_revision.strip() or not artifact_sha256):
        raise ValueError("enabled semantic retrieval requires model_path, model_revision, and artifact_sha256")
    result: dict = {
        "enabled": enabled,
        "model_path": model_path.strip(),
        "model_id": model_id.strip(),
        "model_revision": model_revision.strip(),
        "license_id": license_id.strip().casefold(),
        "artifact_sha256": artifact_sha256.casefold(),
        "dimension": dimension,
        "backend": backend,
        "batch_size": batch_size,
        "max_input_bytes": max_input_bytes,
        "max_records": max_records,
        "max_scan_records": max_scan_records,
        "min_similarity": float(min_similarity),
    }
    return result


def reranker_config(
    enabled: bool = False,
    implementation: str = RERANKER_IMPLEMENTATION,
    shortlist_size: int = DEFAULT_RERANKER_SHORTLIST_SIZE,
    max_input_bytes: int = DEFAULT_RERANKER_MAX_INPUT_BYTES,
    max_model_time_ms: int = DEFAULT_RERANKER_MAX_MODEL_TIME_MS,
) -> dict:
    """Validate the bounded optional reranker configuration."""
    if not isinstance(enabled, bool):
        raise ValueError("reranker enabled must be a boolean")
    if implementation != RERANKER_IMPLEMENTATION:
        raise ValueError(f"reranker implementation must be {RERANKER_IMPLEMENTATION}")
    for name, value, maximum in (
        ("shortlist_size", shortlist_size, MAX_RERANKER_SHORTLIST_SIZE),
        ("max_input_bytes", max_input_bytes, MAX_RERANKER_INPUT_BYTES),
        ("max_model_time_ms", max_model_time_ms, MAX_RERANKER_MODEL_TIME_MS),
    ):
        if not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= maximum:
            raise ValueError(f"reranker {name} must be an integer from 1 through {maximum}")
    result: dict = {
        "enabled": enabled,
        "implementation": implementation,
        "shortlist_size": shortlist_size,
        "max_input_bytes": max_input_bytes,
        "max_model_time_ms": max_model_time_ms,
    }
    return result


def rollout_config(default_mode=RolloutMode.REGULATED_DIRECT_ANSWER, namespaces: dict = EMPTY_CONFIG) -> dict:
    """Validate the small namespace rollout policy used by unified resolution.

    ``default_mode`` and each namespace mode accept a ``RolloutMode`` or its
    string value from YAML; both normalize to the enum member.
    """
    if not isinstance(default_mode, RolloutMode):
        try:
            default_mode = RolloutMode(default_mode)
        except (TypeError, ValueError) as err:
            raise ValueError("rollout default_mode is invalid") from err
    if not isinstance(namespaces, dict):
        raise ValueError("rollout namespaces must be an object")
    selected_namespaces = {}
    for namespace, mode in namespaces.items():
        if not isinstance(namespace, str) or not namespace:
            raise ValueError("rollout namespace keys must be non-empty strings")
        try:
            selected_namespaces[namespace] = mode if isinstance(mode, RolloutMode) else RolloutMode(mode)
        except (TypeError, ValueError) as err:
            raise ValueError(f"rollout mode for namespace {namespace!r} is invalid") from err
    result: dict = {"default_mode": default_mode, "namespaces": selected_namespaces}
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
    bot_name: str = DEFAULT_BOT_NAME,
    seed_files=(),
    duplicate_policy: str = DEFAULT_CONVERSATION_DUPLICATE_POLICY,
    set_files=(),
    map_files=(),
    properties_file: str = "",
    predicate_file: str = "",
    substitution_file: str = "",
) -> dict:
    """Validate the conversation persona and the files loaded at startup.

    ``seed_files``, ``set_files`` and ``map_files`` accept a list or tuple of
    non-empty strings and normalize to a list of stripped paths. An empty
    ``seed_files`` list loads no categories. Paths stored here are
    used as given; ``load_config`` resolves paths from a YAML file before this
    runs. Set, map, property, predicate, and substitution files load before
    the categories. This function does not require the files to exist.

    ``duplicate_policy`` is ``error``, ``last``, or ``first``. ``error`` rejects
    a repeated pattern before anything is stored. ``last`` keeps the later
    file's pair. ``first`` keeps the earlier pair.
    """
    if not isinstance(bot_name, str) or not bot_name.strip():
        raise ValueError("conversation bot_name must be a non-empty string")
    if duplicate_policy not in CONVERSATION_DUPLICATE_POLICIES:
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


SECTION_BUILDERS = (
    ("graph", graph_config),
    ("sparse", sparse_config),
    ("semantic", semantic_config),
    ("reranker", reranker_config),
    ("rollout", rollout_config),
    ("utility", utility_config),
    ("conversation", conversation_config),
)


def require_finite_number(value: object, name: str) -> float:
    """Return a finite native int or float as a float; Booleans and other types are rejected."""
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math_isfinite(value):
        raise ValueError(f"{name} must be a finite number")
    result = float(value)
    return result


def require_stopwords(value: object) -> set[str]:
    """Return a copy of a stopword collection; an empty collection means no stopwords."""
    if not isinstance(value, (set, list, tuple)) or not all(isinstance(word, str) and word for word in value):
        raise ValueError("stopwords must be a collection of non-empty strings")
    result = set(value)
    return result


def engram_config(
    capacity: int = DEFAULT_CAPACITY,
    max_sessions: int = DEFAULT_MAX_SESSIONS,
    session_ttl_seconds: float = DEFAULT_SESSION_TTL_SECONDS,
    weight_base: float = DEFAULT_WEIGHT_BASE,
    weight_recency: float = DEFAULT_WEIGHT_RECENCY,
    weight_hit_rate: float = DEFAULT_WEIGHT_HIT_RATE,
    recency_half_life_seconds: float = DEFAULT_RECENCY_HALF_LIFE_SECONDS,
    session_overflow: SessionOverflow = SessionOverflow.LRU,
    stopwords=DEFAULT_STOPWORDS,
    expand_contractions: bool = True,
    retrieval_rewrites_enabled: bool = False,
    learn_user_facts: bool = True,
    srai_depth_limit: int = DEFAULT_SRAI_DEPTH_LIMIT,
    use_stemming: bool = True,
    use_lemmatization: bool = True,
    use_synonyms: bool = True,
    max_synonyms_per_word: int = DEFAULT_MAX_SYNONYMS_PER_WORD,
    use_spell_correction: bool = True,
    use_spacy_facts: bool = False,
    use_spacy_lemmatization: bool = False,
    use_phrase_keywords: bool = False,
    polish_responses: bool = True,
    fallback_response: str = "",
    graph: dict = EMPTY_CONFIG,
    sparse: dict = EMPTY_CONFIG,
    semantic: dict = EMPTY_CONFIG,
    reranker: dict = EMPTY_CONFIG,
    rollout: dict = EMPTY_CONFIG,
    utility: dict = EMPTY_CONFIG,
    conversation: dict = EMPTY_CONFIG,
) -> dict:
    """Validate and normalize the configuration dict for an ENGRAM instance.

    Every section is built by its own normalizer, so the result has the same
    complete shape whether a section was supplied or left at its defaults.
    ``fallback_response`` is the reply when nothing matches; empty means none.
    """
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
    # Store and session counts compare against these limits on every admission,
    # so only a native positive integer bounds them; NaN and infinity never trip.
    if not isinstance(capacity, int) or isinstance(capacity, bool) or capacity < 1:
        raise ValueError("capacity must be an integer of at least 1")
    if not isinstance(max_sessions, int) or isinstance(max_sessions, bool) or max_sessions < 1:
        raise ValueError("max_sessions must be an integer of at least 1")
    if type(session_ttl_seconds) not in {int, float} or not math_isfinite(session_ttl_seconds) or session_ttl_seconds <= 0:
        raise ValueError("session_ttl_seconds must be a finite positive number")
    try:
        timedelta(seconds=session_ttl_seconds)
    except OverflowError as err:
        raise ValueError("session_ttl_seconds exceeds the representable session interval") from err

    weights = tuple(
        require_finite_number(weight, name)
        for name, weight in (("weight_base", weight_base), ("weight_recency", weight_recency), ("weight_hit_rate", weight_hit_rate))
    )
    if any(weight < 0 for weight in weights):
        raise ValueError("scoring weights must be finite and non-negative")
    if sum(weights) <= 0:
        raise ValueError("scoring weights must sum to a positive value")
    half_life = require_finite_number(recency_half_life_seconds, "recency_half_life_seconds")
    if half_life <= 0:
        raise ValueError("recency_half_life_seconds must be finite and positive")
    if not isinstance(srai_depth_limit, int) or isinstance(srai_depth_limit, bool):
        raise ValueError("srai_depth_limit must be an integer")
    if srai_depth_limit < 1:
        raise ValueError("srai_depth_limit must be at least 1")
    if not isinstance(max_synonyms_per_word, int) or isinstance(max_synonyms_per_word, bool):
        raise ValueError("max_synonyms_per_word must be an integer")
    if max_synonyms_per_word < 0:
        raise ValueError("max_synonyms_per_word must be non-negative")
    if not isinstance(session_overflow, SessionOverflow):
        raise ValueError("session_overflow must be a SessionOverflow")
    if not isinstance(fallback_response, str):
        raise ValueError("fallback_response must be a string")
    flags = {
        "expand_contractions": expand_contractions,
        "retrieval_rewrites_enabled": retrieval_rewrites_enabled,
        "learn_user_facts": learn_user_facts,
        "use_stemming": use_stemming,
        "use_lemmatization": use_lemmatization,
        "use_synonyms": use_synonyms,
        "use_spell_correction": use_spell_correction,
        "use_spacy_facts": use_spacy_facts,
        "use_spacy_lemmatization": use_spacy_lemmatization,
        "use_phrase_keywords": use_phrase_keywords,
        "polish_responses": polish_responses,
    }
    for name, flag in flags.items():
        if not isinstance(flag, bool):
            raise ValueError(f"{name} must be a boolean")
    sections = {
        "graph": graph,
        "sparse": sparse,
        "semantic": semantic,
        "reranker": reranker,
        "rollout": rollout,
        "utility": utility,
        "conversation": conversation,
    }
    built_sections = {
        name: builder(**require_known_keys(sections.get(name, {}), set(builder()), name, "engram_config"))
        for name, builder in SECTION_BUILDERS
    }

    config = {
        "capacity": capacity,
        "max_sessions": max_sessions,
        "session_ttl_seconds": session_ttl_seconds,
        "weight_base": weights[0],
        "weight_recency": weights[1],
        "weight_hit_rate": weights[2],
        "recency_half_life_seconds": half_life,
        "session_overflow": session_overflow,
        "stopwords": require_stopwords(stopwords),
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
        **built_sections,
    }
    return config


def require_known_keys(values: object, allowed: set, section: str, source: str) -> dict:
    """Return a copy of ``values`` after rejecting every key this release does not read."""
    label = f"{section} config" if section else "config"
    if not isinstance(values, dict):
        raise ValueError(f"{label} in {source} must be an object")
    unknown = sorted(str(key) for key in values if key not in allowed)
    if unknown:
        raise ValueError(f"unknown {label} key(s) in {source}: {', '.join(unknown)}")
    result = dict(values)
    return result


def read_config_mapping(path: str) -> dict:
    """Read one YAML configuration file into a mapping of the keys this release reads.

    A missing or empty file is an empty mapping. An unknown top-level or section
    key raises ``ValueError`` naming the key and this file. Conversation file
    paths are resolved against this file's directory and stored absolute, so the
    mapping no longer depends on the file's location. Values are not yet validated.
    """
    if not os_path.exists(path):
        result: dict = {}
        return result
    with open(path, encoding="utf-8") as handle:
        loaded = yaml_safe_load(handle)
    if not loaded:
        result = {}
        return result
    data = require_known_keys(loaded, set(engram_config()), "", path)
    for name, builder in SECTION_BUILDERS:
        section_values = data.get(name, {})
        if section_values:
            data[name] = require_known_keys(section_values, set(builder()), name, path)
    section = data.get("conversation", {})
    if section:
        for key, kind in (("seed_files", "seed file"), ("set_files", "set file"), ("map_files", "map file")):
            if key in section:
                section[key] = resolve_conversation_path_list(section.get(key, []), path, key, kind)
        for key, kind in (
            ("properties_file", "properties file"),
            ("predicate_file", "predicate file"),
            ("substitution_file", "substitution file"),
        ):
            if key in section:
                section[key] = resolve_conversation_optional_file(section.get(key, ""), path, key, kind)
    return data


def validated_config(data: dict, source: str) -> dict:
    """Build a validated config dict from a config.yml-shaped mapping.

    ``session_overflow`` may be its string value and ``stopwords`` a list of
    words. Nested mappings are built with their section normalizers; missing
    keys fall back to the ``engram_config`` defaults, and an unknown key raises
    ``ValueError`` naming ``source``.
    """
    params = require_known_keys(data, set(engram_config()), "", source)
    if "session_overflow" in params:
        try:
            params["session_overflow"] = SessionOverflow(params.get("session_overflow", ""))
        except ValueError as err:
            raise ValueError(f"session_overflow in {source} is invalid") from err
    for name, builder in SECTION_BUILDERS:
        section = params.get(name, {})
        if name in params and section:
            params[name] = require_known_keys(section, set(builder()), name, source)
    config = engram_config(**params)
    return config


def config_from_dict(data: dict, base_path: str = DEFAULT_CONFIG_PATH) -> dict:
    """Build a validated config from Engram's config.yml overridden by a host mapping.

    For a host, such as Tapestry, that supplies Engram's settings in memory.
    The configuration file at ``base_path`` (by default the ``config.yml``
    beside the ``engram`` package) is read first, with its conversation paths
    resolved against that file. ``data`` then overrides it: a top-level value
    replaces the file's value, and a section such as ``graph`` or
    ``conversation`` overrides the file's section key by key. Paths in ``data``
    are used as given. A missing file, or an empty ``base_path``, leaves only
    the ``engram_config`` defaults underneath. An unknown key raises
    ``ValueError`` naming it.
    """
    if not isinstance(data, dict):
        raise ValueError("config must be an object")
    source = "config mapping"
    merged = read_config_mapping(base_path) if base_path else {}
    overrides = require_known_keys(data, set(engram_config()), "", source)
    sections = {name for name, _ in SECTION_BUILDERS}
    for key, value in overrides.items():
        current = merged.get(key, {})
        if key in sections and isinstance(current, dict) and isinstance(value, dict):
            merged[key] = {**current, **value}
        else:
            merged[key] = value
    config = validated_config(merged, source)
    return config


def load_config(path: str = DEFAULT_CONFIG_PATH) -> dict:
    """Build a config dict from a YAML file.

    A missing or empty file returns the ``engram_config`` defaults. Scalar keys
    map straight through; ``session_overflow`` is given by its string value,
    and nested mappings are built with their section normalizers. Conversation
    file paths are resolved against this file's directory and stored absolute.
    A missing file, a directory, or a non-file raises ValueError naming that
    path and this file. An unknown key raises ValueError naming the key, so a
    misspelled setting stops startup instead of silently keeping its default.

    Args:
        path: Path to the YAML configuration file.

    Returns:
        A validated config dict.
    """
    config = validated_config(read_config_mapping(path), path)
    return config
