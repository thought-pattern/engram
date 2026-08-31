"""Request-local fielded sparse retrieval over accepted-response artifacts."""

from collections import Counter
from math import log as math_log
from re import IGNORECASE as IGNORECASE, UNICODE as UNICODE, compile as re_compile
from unicodedata import normalize as unicodedata_normalize

from engram.artifacts import LifecycleState, validate_cached_response_artifact
from engram.config import sparse_config
from engram.constants import (
    DEFAULT_STOPWORDS,
    SPARSE_DOCUMENT_SCHEMA_VERSION,
    SPARSE_TOKENIZER_VERSION,
)
from engram.errors import InvalidRequestError
from engram.identity import validate_scope_key

MAX_SPARSE_FIELDS = 7
MAX_SPARSE_FIELD_TEXTS = 65
MAX_SPARSE_TOKENS_PER_FIELD = 2_048
MAX_SPARSE_TECHNICAL_IDENTIFIERS = 256
MAX_SPARSE_TOKEN_BYTES = 256
MAX_SPARSE_RESULTS = 1_000
SPARSE_DESCRIPTOR_WORKING_BYTES = 192
SPARSE_IDENTIFIER_WORKING_BYTES = 128
SPARSE_SCORE_WORKING_BYTES = 1_024
SPARSE_MATCH_WORKING_BYTES = 768
MIN_PREFIX_LENGTH = 3
MAX_PREFIX_LENGTH = 12
CHAR_NGRAM_SIZE = 3

SPARSE_FIELD_NAMES = (
    "canonical",
    "aliases",
    "entities",
    "relation",
    "keywords",
    "technical_identifiers",
    "response_text",
)
SPARSE_FIELD_WEIGHTS: dict[str, float] = {
        "canonical": 3.0,
        "aliases": 2.5,
        "entities": 2.25,
        "relation": 2.0,
        "keywords": 1.5,
        "technical_identifiers": 3.5,
        "response_text": 0.25,
    }

GENERAL_TOKEN = re_compile(r"[^\W_]+(?:['’][^\W_]+)?", UNICODE)
VERSION_TOKEN = re_compile(r"^v?\d+(?:\.\d+){1,5}(?:[-+][a-z0-9._-]+)?$", IGNORECASE)
ERROR_CODE = re_compile(r"^[A-Z][A-Z0-9_]{2,}$")
MIXED_IDENTIFIER = re_compile(r"^(?=.*[A-Za-z])(?=.*\d)[A-Za-z0-9._:/\\+#@%$|&*<>=-]+$")
TECHNICAL_PUNCTUATION = set("._:/\\-+#@%$|&*<>=")
BOUNDARY_PUNCTUATION = "\"'`()[]{};,!?"


def internal_validated_settings(value: dict[str, object]) -> dict:
    if not value:
        result = sparse_config()
        return result
    keys = {
        "enabled",
        "include_response_text",
        "max_query_terms",
        "max_posting_visits",
        "max_prefix_expansions",
    }
    if set(value) != keys:
        raise InvalidRequestError("sparse settings have invalid fields")
    enabled = value.get("enabled", False)
    include_response_text = value.get("include_response_text", False)
    max_query_terms = value.get("max_query_terms", 0)
    max_posting_visits = value.get("max_posting_visits", 0)
    max_prefix_expansions = value.get("max_prefix_expansions", 0)
    if not isinstance(enabled, bool) or not isinstance(include_response_text, bool):
        raise InvalidRequestError("sparse enablement settings must be booleans")
    if not isinstance(max_query_terms, int) or isinstance(max_query_terms, bool):
        raise InvalidRequestError("sparse max_query_terms must be an integer")
    if not isinstance(max_posting_visits, int) or isinstance(max_posting_visits, bool):
        raise InvalidRequestError("sparse max_posting_visits must be an integer")
    if not isinstance(max_prefix_expansions, int) or isinstance(max_prefix_expansions, bool):
        raise InvalidRequestError("sparse max_prefix_expansions must be an integer")
    try:
        result = sparse_config(
            enabled=enabled,
            include_response_text=include_response_text,
            max_query_terms=max_query_terms,
            max_posting_visits=max_posting_visits,
            max_prefix_expansions=max_prefix_expansions,
        )
    except ValueError as error:
        raise InvalidRequestError(str(error)) from error
    return result








SparseMatch = dict




def normalized_text(value: str) -> str:
    result = unicodedata_normalize("NFKC", value).casefold().strip()
    return result


def bounded_token(value: str) -> str:
    normalized = normalized_text(value)
    result = normalized if normalized and len(normalized.encode("utf-8")) <= MAX_SPARSE_TOKEN_BYTES else ""
    return result


def technical_identifier(value: str) -> bool:
    if not value or len(value.encode("utf-8")) > MAX_SPARSE_TOKEN_BYTES:
        return False
    if ERROR_CODE.fullmatch(value) or VERSION_TOKEN.fullmatch(value) or MIXED_IDENTIFIER.fullmatch(value):
        return True
    if any(character in TECHNICAL_PUNCTUATION for character in value):
        return True
    result = any(first.islower() and second.isupper() for first, second in zip(value, value[1:], strict=False))
    return result


def technical_identifiers(value: str) -> tuple[str, ...]:
    """Extract bounded punctuation-preserving technical identifiers."""
    if not isinstance(value, str):
        raise InvalidRequestError("sparse technical-token input must be a string")
    found = []
    seen = set()
    for raw in value.split():
        candidate = raw.strip(BOUNDARY_PUNCTUATION)
        if not technical_identifier(candidate):
            continue
        normalized = bounded_token(candidate)
        if normalized and normalized not in seen:
            seen.add(normalized)
            found.append(normalized)
        if len(found) >= MAX_SPARSE_TECHNICAL_IDENTIFIERS:
            break
    result = tuple(found)
    return result


def sparse_tokens(value: str) -> tuple[str, ...]:
    """Tokenize natural language while retaining components of technical tokens."""
    if not isinstance(value, str):
        raise InvalidRequestError("sparse token input must be a string")
    normalized = normalized_text(value)
    tokens = [bounded_token(match.group(0)) for match in GENERAL_TOKEN.finditer(normalized)]
    result = tuple(token for token in tokens if token and token not in DEFAULT_STOPWORDS)[:MAX_SPARSE_TOKENS_PER_FIELD]
    return result


def field_texts(artifact: dict, include_response_text: bool) -> dict[str, tuple[str, ...]]:
    identity = artifact.get("query_identity", {})
    entity_values = []
    for entity in identity["entities"]:
        entity_values.append(entity["surface"])
        if entity["canonical_id"]:
            entity_values.append(entity["canonical_id"])
    relation_values = tuple(value for value in (identity["relation"]["surface"], identity["relation"]["canonical_id"]) if value)
    source_values = (
        artifact.get("retrieval", {})["canonical"],
        *artifact.get("retrieval", {})["aliases"],
        *entity_values,
        *relation_values,
        *identity["lexical_terms"],
    )
    identifiers = []
    for value in source_values:
        identifiers.extend(technical_identifiers(value))
    fields = {
        "canonical": (artifact.get("retrieval", {})["canonical"],),
        "aliases": tuple(artifact.get("retrieval", {})["aliases"]),
        "entities": tuple(entity_values),
        "relation": relation_values,
        "keywords": tuple(identity["lexical_terms"]),
        "technical_identifiers": tuple(dict.fromkeys(identifiers))[:MAX_SPARSE_TECHNICAL_IDENTIFIERS],
        "response_text": (artifact.get("response", ""),) if include_response_text else (),
    }
    result = {
        name: tuple(normalized_text(value) for value in fields.get(name, ()) if value)[:MAX_SPARSE_FIELD_TEXTS]
        for name in SPARSE_FIELD_NAMES
    }
    return result


def sparse_document_from_validated_artifact(
    artifact: dict,
    include_response_text: bool,
) -> dict:
    fields = field_texts(artifact, include_response_text)
    tokens = {
        name: tuple(token for text in fields[name] for token in sparse_tokens(text))[:MAX_SPARSE_TOKENS_PER_FIELD]
        for name in SPARSE_FIELD_NAMES
    }
    result = {
            "schema_version": SPARSE_DOCUMENT_SCHEMA_VERSION,
            "statement_id": artifact.get("statement_id", ""),
            "scope": dict(validate_scope_key(artifact.get("scope", {}))),
            "lifecycle": artifact.get("lifecycle", LifecycleState.RETIRED),
            "fields": dict(fields),
            "tokens": dict(tokens),
            "technical_identifiers": fields["technical_identifiers"],
        }
    return result


def sparse_document_from_artifact(value: object, *, include_response_text: bool = False) -> dict:
    """Validate and project one authoritative accepted-response artifact."""
    if not isinstance(include_response_text, bool):
        raise InvalidRequestError("sparse include_response_text must be a boolean")
    artifact = validate_cached_response_artifact(value)
    result = sparse_document_from_validated_artifact(artifact, include_response_text)
    return result


def prefixes(token: str) -> tuple[str, ...]:
    upper = min(MAX_PREFIX_LENGTH, len(token) - 1)
    result = tuple(token[:length] for length in range(MIN_PREFIX_LENGTH, upper + 1)) if upper >= MIN_PREFIX_LENGTH else ()
    return result


def character_ngrams(token: str) -> tuple[str, ...]:
    compact = "".join(character for character in token if not character.isspace())
    if len(compact) < CHAR_NGRAM_SIZE:
        return ()
    result = tuple(dict.fromkeys(compact[index : index + CHAR_NGRAM_SIZE] for index in range(len(compact) - 2)))
    return result


def document_term_frequencies(document: dict) -> dict[str, Counter[str]]:
    frequencies: dict[str, Counter[str]] = {}
    for field_name in SPARSE_FIELD_NAMES:
        counter: Counter[str] = Counter()
        for token in document.get("tokens", ())[field_name]:
            counter[f"t:{token}"] += 1
            if field_name in {"canonical", "aliases"} and len(token) >= 6:
                for prefix in prefixes(token):
                    counter[f"p:{prefix}"] += 1
        if field_name == "technical_identifiers":
            for identifier in document.get("technical_identifiers", ()):
                counter[f"x:{identifier}"] += 1
                for prefix in prefixes(identifier):
                    counter[f"p:{prefix}"] += 1
                for ngram in character_ngrams(identifier):
                    counter[f"g:{ngram}"] += 1
        frequencies[field_name] = counter
    return frequencies


def build_sparse_working_set(
    artifacts: list[dict],
    *,
    settings: dict[str, object],
    trusted_artifacts: bool = False,
) -> dict:
    """Build immutable request-local sparse structures from current artifacts."""
    validated_settings = internal_validated_settings(settings)
    if not isinstance(trusted_artifacts, bool):
        raise InvalidRequestError("sparse trusted_artifacts must be a boolean")
    include_response_text = validated_settings["include_response_text"]
    documents: dict[str, dict] = {}
    term_documents: dict[str, dict[str, dict[str, int]]] = {}
    field_totals = dict.fromkeys(SPARSE_FIELD_NAMES, 0)
    for artifact_value in artifacts:
        artifact = artifact_value if trusted_artifacts else validate_cached_response_artifact(artifact_value)
        document = sparse_document_from_validated_artifact(artifact, include_response_text)
        statement_id = document["statement_id"]
        if statement_id in documents:
            raise InvalidRequestError(f"duplicate sparse statement_id: {statement_id}")
        documents[statement_id] = document
        for field_name in SPARSE_FIELD_NAMES:
            field_totals[field_name] += len(document["tokens"][field_name])
        for field_name, counter in document_term_frequencies(document).items():
            for term, frequency in counter.items():
                term_documents.setdefault(term, {}).setdefault(statement_id, {})[field_name] = frequency

    postings: dict[str, tuple[tuple, ...]] = {}
    document_frequencies = {}
    for term in sorted(term_documents):
        posting_values: tuple[tuple, ...] = tuple(
            (statement_id, tuple(sorted(term_documents.get(term, {})[statement_id].items())))
            for statement_id in sorted(term_documents.get(term, {}))
        )
        postings[term] = posting_values
        document_frequencies[term] = len(posting_values)
    count = len(documents)
    averages = {name: (field_totals[name] / count if count else 0.0) for name in SPARSE_FIELD_NAMES}
    frozen_documents = dict(dict(sorted(documents.items())))
    frozen_postings = dict(postings)
    result = {
            "tokenizer_version": SPARSE_TOKENIZER_VERSION,
            "documents": frozen_documents,
            "postings": frozen_postings,
            "document_frequencies": dict(document_frequencies),
            "average_field_lengths": dict(averages),
        }
    return result


def phrase_present(tokens: tuple[str, ...], query: tuple[str, ...]) -> bool:
    if not query or len(query) > len(tokens):
        return False
    result = any(tokens[index : index + len(query)] == query for index in range(len(tokens) - len(query) + 1))
    return result


def internal_minimum_proximity(tokens: tuple[str, ...], query: tuple[str, ...]) -> int:
    required = tuple(dict.fromkeys(query))
    if len(required) < 2:
        return 0
    positions = {term: [index for index, token in enumerate(tokens) if token == term] for term in required}
    if any(not values for values in positions.values()):
        return 0
    events = sorted((position, term) for term, values in positions.items() for position in values)
    counts: Counter[str] = Counter()
    left = 0
    best = 0
    for right_position, right_term in events:
        counts[right_term] += 1
        while len(counts) == len(required):
            left_position, left_term = events[left]
            width = right_position - left_position + 1
            best = width if not best else min(best, width)
            counts[left_term] -= 1
            if not counts[left_term]:
                del counts[left_term]
            left += 1
    return best


def query_descriptors(
    text: str,
    max_prefix_expansions: int,
) -> tuple[tuple[str, ...], tuple[str, ...], dict[str, float]]:
    tokens = tuple(dict.fromkeys(sparse_tokens(text)))
    identifiers = technical_identifiers(text)
    descriptors: dict[str, float] = {}
    prefix_count = 0
    for token in tokens:
        descriptors[f"t:{token}"] = 1.0
        if MIN_PREFIX_LENGTH <= len(token) <= MAX_PREFIX_LENGTH and prefix_count < max_prefix_expansions:
            descriptors[f"p:{token}"] = 0.15
            prefix_count += 1
    for identifier in identifiers:
        descriptors[f"x:{identifier}"] = 1.5
    result = (tokens, identifiers, dict(descriptors))
    return result


def search_sparse_working_set(
    state: dict,
    text: str,
    scope: dict,
    *,
    limit: int,
    max_query_terms: int,
    max_posting_visits: int,
    max_prefix_expansions: int,
    max_working_memory_bytes: int,
) -> dict:
    """Search one immutable state with complete-or-abstain resource bounds."""
    if not isinstance(text, str) or not text.strip():
        raise InvalidRequestError("sparse query text must be a non-empty string")
    validated_scope = validate_scope_key(scope)
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= MAX_SPARSE_RESULTS:
        raise InvalidRequestError(f"sparse limit must be an integer from 1 through {MAX_SPARSE_RESULTS}")
    for name, value in (
        ("max_query_terms", max_query_terms),
        ("max_posting_visits", max_posting_visits),
        ("max_prefix_expansions", max_prefix_expansions),
        ("max_working_memory_bytes", max_working_memory_bytes),
    ):
        if not isinstance(value, int) or isinstance(value, bool) or value < 1:
            raise InvalidRequestError(f"sparse {name} must be a positive integer")
    query_tokens, query_identifiers, base_descriptors = query_descriptors(text, max_prefix_expansions)
    base_term_count = len({*(f"t:{token}" for token in query_tokens), *(f"x:{value}" for value in query_identifiers)})
    if base_term_count > max_query_terms:
        result = {
            "matches": (),
            "complete": False,
            "reason": "query_term_budget",
            "query_term_count": base_term_count,
            "posting_visits": 0,
            "working_memory_bytes": 0,
        }
        return result
    descriptors = dict(base_descriptors)
    posting_visits = 0
    exact_identifier_sets = []
    exact_identifier_item_count = 0
    for identifier in query_identifiers:
        statement_ids = set()
        for posting in state.get("postings", {}).get(f"x:{identifier}", ()):
            posting_visits += 1
            if posting_visits > max_posting_visits:
                result = {
                    "matches": (),
                    "complete": False,
                    "reason": "posting_visit_budget",
                    "query_term_count": len(descriptors),
                    "posting_visits": posting_visits,
                    "working_memory_bytes": 256 + len(descriptors) * SPARSE_DESCRIPTOR_WORKING_BYTES,
                }
                return result
            if state.get("documents", ())[posting[0]]["scope"] != validated_scope:
                continue
            projected_items = exact_identifier_item_count + 1
            projected_memory = (
                256 + len(descriptors) * SPARSE_DESCRIPTOR_WORKING_BYTES + projected_items * SPARSE_IDENTIFIER_WORKING_BYTES
            )
            if projected_memory > max_working_memory_bytes:
                result = {
                    "matches": (),
                    "complete": False,
                    "reason": "working_memory_budget",
                    "query_term_count": len(descriptors),
                    "posting_visits": posting_visits,
                    "working_memory_bytes": 256
                    + len(descriptors) * SPARSE_DESCRIPTOR_WORKING_BYTES
                    + exact_identifier_item_count * SPARSE_IDENTIFIER_WORKING_BYTES,
                }
                return result
            statement_ids.add(posting[0])
            exact_identifier_item_count = projected_items
        exact_identifier_sets.append(statement_ids)
        if statement_ids:
            continue
        ngrams = character_ngrams(identifier)
        multiplier = 0.15 / max(1, len(ngrams))
        for ngram in ngrams:
            descriptors[f"g:{ngram}"] = multiplier
    if len(descriptors) > max_query_terms:
        result = {
            "matches": (),
            "complete": False,
            "reason": "query_term_budget",
            "query_term_count": len(descriptors),
            "posting_visits": posting_visits,
            "working_memory_bytes": 0,
        }
        return result
    if not descriptors:
        result = {
            "matches": (),
            "complete": True,
            "reason": "no_sparse_terms",
            "query_term_count": 0,
            "posting_visits": posting_visits,
            "working_memory_bytes": 0,
        }
        return result

    scores: dict[str, dict[str, float]] = {}
    matched_keys: dict[str, set[str]] = {}
    document_count = max(1, len(state.get("documents", ())))
    candidate_pool: set[str] = set()
    populated_identifier_sets = [value for value in exact_identifier_sets if value]
    if populated_identifier_sets:
        projected_memory = (
            256
            + len(descriptors) * SPARSE_DESCRIPTOR_WORKING_BYTES
            + (exact_identifier_item_count + len(populated_identifier_sets[0])) * SPARSE_IDENTIFIER_WORKING_BYTES
        )
        if projected_memory > max_working_memory_bytes:
            result = {
                "matches": (),
                "complete": False,
                "reason": "working_memory_budget",
                "query_term_count": len(descriptors),
                "posting_visits": posting_visits,
                "working_memory_bytes": 256
                + len(descriptors) * SPARSE_DESCRIPTOR_WORKING_BYTES
                + exact_identifier_item_count * SPARSE_IDENTIFIER_WORKING_BYTES,
            }
            return result
        candidate_pool = set(populated_identifier_sets[0])
        for statement_ids in populated_identifier_sets[1:]:
            candidate_pool.intersection_update(statement_ids)
        if not candidate_pool:
            for statement_ids in populated_identifier_sets:
                for statement_id in statement_ids:
                    if statement_id in candidate_pool:
                        continue
                    projected_size = len(candidate_pool) + 1
                    projected_memory = (
                        256
                        + len(descriptors) * SPARSE_DESCRIPTOR_WORKING_BYTES
                        + (exact_identifier_item_count + projected_size) * SPARSE_IDENTIFIER_WORKING_BYTES
                    )
                    if projected_memory > max_working_memory_bytes:
                        result = {
                            "matches": (),
                            "complete": False,
                            "reason": "working_memory_budget",
                            "query_term_count": len(descriptors),
                            "posting_visits": posting_visits,
                            "working_memory_bytes": 256
                            + len(descriptors) * SPARSE_DESCRIPTOR_WORKING_BYTES
                            + (exact_identifier_item_count + len(candidate_pool)) * SPARSE_IDENTIFIER_WORKING_BYTES,
                        }
                        return result
                    candidate_pool.add(statement_id)
    else:
        rare_limit = max(32, min(1_000, document_count // 10))
        anchor_terms = sorted(
            (state.get("document_frequencies", {}).get(term, 0), term)
            for term in descriptors
            if 0 < state.get("document_frequencies", {}).get(term, 0) <= rare_limit
        )[:4]
        for _, term in anchor_terms:
            for posting in state.get("postings", {}).get(term, ()):
                posting_visits += 1
                if posting_visits > max_posting_visits:
                    result = {
                        "matches": (),
                        "complete": False,
                        "reason": "posting_visit_budget",
                        "query_term_count": len(descriptors),
                        "posting_visits": posting_visits,
                        "working_memory_bytes": 256
                        + len(descriptors) * SPARSE_DESCRIPTOR_WORKING_BYTES
                        + len(candidate_pool) * SPARSE_IDENTIFIER_WORKING_BYTES,
                    }
                    return result
                statement_id = posting[0]
                if state.get("documents", ())[statement_id]["scope"] != validated_scope or statement_id in candidate_pool:
                    continue
                projected_memory = (
                    256
                    + len(descriptors) * SPARSE_DESCRIPTOR_WORKING_BYTES
                    + (len(candidate_pool) + 1) * SPARSE_IDENTIFIER_WORKING_BYTES
                )
                if projected_memory > max_working_memory_bytes:
                    result = {
                        "matches": (),
                        "complete": False,
                        "reason": "working_memory_budget",
                        "query_term_count": len(descriptors),
                        "posting_visits": posting_visits,
                        "working_memory_bytes": 256
                        + len(descriptors) * SPARSE_DESCRIPTOR_WORKING_BYTES
                        + len(candidate_pool) * SPARSE_IDENTIFIER_WORKING_BYTES,
                    }
                    return result
                candidate_pool.add(statement_id)
    exact_identifier_sets.clear()
    working_memory = (
        256 + len(descriptors) * SPARSE_DESCRIPTOR_WORKING_BYTES + len(candidate_pool) * SPARSE_IDENTIFIER_WORKING_BYTES
    )
    if working_memory > max_working_memory_bytes:
        result = {
            "matches": (),
            "complete": False,
            "reason": "working_memory_budget",
            "query_term_count": len(descriptors),
            "posting_visits": posting_visits,
            "working_memory_bytes": 0,
        }
        return result
    for term, multiplier in descriptors.items():
        postings = state.get("postings", {}).get(term, ())
        selected_postings = (posting for posting in postings if posting[0] in candidate_pool) if candidate_pool else postings
        document_frequency = state.get("document_frequencies", {}).get(term, 0)
        inverse_frequency = math_log(1.0 + (document_count - document_frequency + 0.5) / (document_frequency + 0.5))
        for posting in selected_postings:
            posting_visits += 1
            if posting_visits > max_posting_visits:
                result = {
                    "matches": (),
                    "complete": False,
                    "reason": "posting_visit_budget",
                    "query_term_count": len(descriptors),
                    "posting_visits": posting_visits,
                    "working_memory_bytes": working_memory,
                }
                return result
            statement_id = posting[0]
            document = state.get("documents", ())[statement_id]
            if document["scope"] != validated_scope:
                continue
            if statement_id not in scores:
                projected_memory = working_memory + SPARSE_SCORE_WORKING_BYTES
                if projected_memory > max_working_memory_bytes:
                    result = {
                        "matches": (),
                        "complete": False,
                        "reason": "working_memory_budget",
                        "query_term_count": len(descriptors),
                        "posting_visits": posting_visits,
                        "working_memory_bytes": working_memory,
                    }
                    return result
                working_memory = projected_memory
            contributions = scores.setdefault(statement_id, dict.fromkeys(SPARSE_FIELD_NAMES, 0.0))
            matched_keys.setdefault(statement_id, set()).add(term)
            for field_name, frequency in posting[1]:
                field_length = len(document["tokens"][field_name])
                average_length = state.get("average_field_lengths", {})[field_name] or 1.0
                denominator = frequency + 1.2 * (1.0 - 0.75 + 0.75 * field_length / average_length)
                bm25 = inverse_frequency * (frequency * 2.2) / denominator
                contributions[field_name] += bm25 * SPARSE_FIELD_WEIGHTS.get(field_name, 0.0) * multiplier
    query_ngrams = {f"g:{ngram}" for identifier in query_identifiers for ngram in character_ngrams(identifier)}
    retained = []
    for statement_id, raw_fields in scores.items():
        document = state.get("documents", ())[statement_id]
        raw_total = sum(raw_fields.values())
        normalization = 2.0 * max(1, len(query_tokens))
        base_score = raw_total / (raw_total + normalization) if raw_total else 0.0
        phrase_fields = tuple(name for name in SPARSE_FIELD_NAMES if phrase_present(document["tokens"][name], query_tokens))
        proximity = {name: internal_minimum_proximity(document["tokens"][name], query_tokens) for name in SPARSE_FIELD_NAMES}
        proximity_fields = tuple(name for name in SPARSE_FIELD_NAMES if proximity[name] and proximity[name] <= 8)
        minimum_proximity = min((value for value in proximity.values() if value), default=0)
        keys = matched_keys.get(statement_id, set())
        prefix_count = sum(key.startswith("p:") for key in keys)
        matched_ngrams = len(query_ngrams.intersection(keys))
        ngram_similarity = matched_ngrams / len(query_ngrams) if query_ngrams else 0.0
        technical_exact_count = sum(f"x:{identifier}" in keys for identifier in query_identifiers)
        technical_exact_ratio = technical_exact_count / len(query_identifiers) if query_identifiers else 0.0
        technical_exact = bool(technical_exact_count)
        prefix_ratio = min(1.0, prefix_count / max(1, len(query_tokens)))
        score = min(
            1.0,
            0.65 * base_score
            + (0.10 if phrase_fields else 0.0)
            + (0.05 if proximity_fields else 0.0)
            + 0.05 * prefix_ratio
            + 0.10 * ngram_similarity
            + 0.15 * technical_exact_ratio,
        )
        ranking_key = (-score, statement_id)
        worst_index = -1
        if len(retained) >= limit:
            worst_index = max(range(len(retained)), key=lambda index: (-retained[index]["score"], retained[index]["statement_id"]))
            worst = retained[worst_index]
            if ranking_key >= (-worst["score"], worst["statement_id"]):
                continue
        if worst_index < 0:
            projected_memory = working_memory + SPARSE_MATCH_WORKING_BYTES
            if projected_memory > max_working_memory_bytes:
                result = {
                    "matches": (),
                    "complete": False,
                    "reason": "working_memory_budget",
                    "query_term_count": len(descriptors),
                    "posting_visits": posting_visits,
                    "working_memory_bytes": working_memory,
                }
                return result
            working_memory = projected_memory
        field_contributions = {
                name: raw_fields[name] / (raw_fields[name] + normalization) if raw_fields[name] else 0.0
                for name in SPARSE_FIELD_NAMES
            }
        match = SparseMatch(
            statement_id=statement_id,
            score=score,
            field_contributions=field_contributions,
            phrase_fields=phrase_fields,
            proximity_fields=proximity_fields,
            minimum_proximity=minimum_proximity,
            prefix_match_count=prefix_count,
            character_ngram_similarity=ngram_similarity,
            technical_exact_match=technical_exact,
            technical_exact_ratio=technical_exact_ratio,
            matched_term_count=len(keys),
        )
        if worst_index < 0:
            retained.append(match)
        else:
            retained[worst_index] = match
    retained.sort(key=lambda match: (-match["score"], match["statement_id"]))
    retained_values = tuple(retained)
    result = {
        "matches": retained_values,
        "complete": True,
        "reason": "sparse_candidates" if retained_values else "sparse_miss",
        "query_term_count": len(descriptors),
        "posting_visits": posting_visits,
        "working_memory_bytes": working_memory,
    }
    return result


def search_sparse_artifacts(
    artifacts: list[dict],
    text: str,
    scope: dict,
    settings: dict[str, object],
    *,
    limit: int,
    max_working_memory_bytes: int,
) -> dict:
    """Build request-local sparse structures and discard them after search."""
    validated_settings = internal_validated_settings(settings)
    if not validated_settings.get("enabled", False):
        result = {
            "matches": (),
            "complete": False,
            "reason": "sparse_unavailable",
            "query_term_count": 0,
            "posting_visits": 0,
            "working_memory_bytes": 0,
        }
        return result
    active_artifacts = tuple(
        artifact
        for value in artifacts
        if (artifact := validate_cached_response_artifact(value)).get("lifecycle") == LifecycleState.ACTIVE
    )
    state = build_sparse_working_set(
        active_artifacts,
        settings=validated_settings,
        trusted_artifacts=True,
    )
    result = search_sparse_working_set(
        state,
        text,
        scope,
        limit=limit,
        max_query_terms=validated_settings.get("max_query_terms", 1),
        max_posting_visits=validated_settings.get("max_posting_visits", 1),
        max_prefix_expansions=validated_settings.get("max_prefix_expansions", 1),
        max_working_memory_bytes=max_working_memory_bytes,
    )
    return result
