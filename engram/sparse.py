"""Request-local fielded sparse retrieval over accepted-response artifacts."""

from collections import Counter
from math import log as math_log
from re import IGNORECASE as IGNORECASE, UNICODE as UNICODE, compile as re_compile
from threading import Lock as threading_Lock
from unicodedata import normalize as unicodedata_normalize

from engram.artifacts import LifecycleState, validate_cached_response_artifact
from engram.config import sparse_config
from engram.constants import (
    DEFAULT_STOPWORDS,
)
from engram.errors import InvalidRequestError
from engram.identity import validate_scope_key
from engram.resources import estimate_working_bytes

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
    token_fields = document.get("tokens", {})
    if not isinstance(token_fields, dict):
        raise InvalidRequestError("sparse document tokens must be an object")
    for field_name in SPARSE_FIELD_NAMES:
        counter: Counter[str] = Counter()
        for token in token_fields.get(field_name, ()):
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


# Working memory a request charges for the structures it builds. Cached
# per-artifact documents are shared, not rebuilt, so they are not charged.
SPARSE_DOCUMENT_REFERENCE_BYTES = 128
SPARSE_POSTING_REFERENCE_BYTES = 64
SPARSE_TERM_WORKING_BYTES = 128


def sparse_document_parts(artifact: dict, include_response_text: bool) -> dict:
    """Derive the document, token counts, and per-term postings of one validated artifact."""
    document = sparse_document_from_validated_artifact(artifact, include_response_text)
    statement_id = document.get("statement_id", "")
    frequencies = document_term_frequencies(document)
    token_fields = document.get("tokens", {})
    if not isinstance(token_fields, dict):
        raise InvalidRequestError("sparse document tokens must be an object")
    term_fields: dict[str, dict[str, int]] = {}
    for field_name, counter in frequencies.items():
        for term, frequency in counter.items():
            term_fields.setdefault(term, {})[field_name] = frequency
    result = {
        "statement_id": statement_id,
        "document": document,
        "token_lengths": {name: len(token_fields.get(name, ())) for name in SPARSE_FIELD_NAMES},
        "postings": {term: (statement_id, tuple(sorted(fields.items()))) for term, fields in term_fields.items()},
    }
    return result


def sparse_scope_key(scope: dict) -> tuple:
    """Return a hashable key for a validated scope; equal scopes give equal keys."""
    result = tuple(sorted(scope.items()))
    return result


def sparse_content(artifact: dict, include_response_text: bool) -> tuple:
    """Return the artifact values a sparse document is derived from.

    Statistics and generation are left out: they change on every resolve and
    do not affect the document.
    """
    result = (
        artifact.get("retrieval", {}),
        artifact.get("query_identity", {}),
        artifact.get("scope", {}),
        artifact.get("lifecycle", LifecycleState.RETIRED),
        artifact.get("response", "") if include_response_text else "",
    )
    return result


class SparseIndex:
    """Persistent per-scope inverted index over active accepted-response artifacts.

    ``sync`` brings the index in line with the artifact snapshot a request is
    about to score. Artifacts are compared by identity; one whose sparse
    content changed is re-indexed, and one whose only change is statistics is
    not. Searches read postings for the query's terms only, so their cost
    follows the query instead of the store. Results match a full per-request
    build (``build_sparse_working_set`` and ``search_sparse_working_set``).
    """

    def __init__(self) -> None:
        self.internal_lock = threading_Lock()
        self.internal_include_response_text: bool | None = None
        # statement_id -> {"artifact", "content", "parts", "scope_key", "active"}
        self.internal_entries: dict[str, dict] = {}
        # scope key -> {"documents": {id: document}, "postings": {term: {id: posting}}, "field_totals": {name: int}}
        self.internal_partitions: dict[tuple, dict] = {}

    def clear(self) -> None:
        self.internal_entries.clear()
        self.internal_partitions.clear()

    def add(self, statement_id: str, artifact: dict, content: tuple, include_response_text: bool) -> None:
        parts = sparse_document_parts(artifact, include_response_text)
        scope_key = sparse_scope_key(artifact.get("scope", {}))
        active = artifact.get("lifecycle", LifecycleState.RETIRED) == LifecycleState.ACTIVE
        self.internal_entries[statement_id] = {
            "artifact": artifact,
            "content": content,
            "parts": parts,
            "scope_key": scope_key,
            "active": active,
        }
        if not active:
            return
        partition = self.internal_partitions.setdefault(
            scope_key,
            {"documents": {}, "postings": {}, "field_totals": dict.fromkeys(SPARSE_FIELD_NAMES, 0)},
        )
        partition["documents"][statement_id] = parts["document"]
        for field_name in SPARSE_FIELD_NAMES:
            partition["field_totals"][field_name] += parts["token_lengths"][field_name]
        for term, posting in parts["postings"].items():
            partition["postings"].setdefault(term, {})[statement_id] = posting

    def remove(self, statement_id: str) -> None:
        entry = self.internal_entries.pop(statement_id)
        if not entry["active"]:
            return
        partition = self.internal_partitions[entry["scope_key"]]
        del partition["documents"][statement_id]
        parts = entry["parts"]
        for field_name in SPARSE_FIELD_NAMES:
            partition["field_totals"][field_name] -= parts["token_lengths"][field_name]
        for term in parts["postings"]:
            postings = partition["postings"][term]
            del postings[statement_id]
            if not postings:
                del partition["postings"][term]
        if not partition["documents"]:
            del self.internal_partitions[entry["scope_key"]]

    def sync(self, artifacts: dict[str, dict], include_response_text: bool) -> None:
        """Index exactly ``artifacts``; the caller holds ``internal_lock``."""
        if include_response_text != self.internal_include_response_text:
            self.clear()
            self.internal_include_response_text = include_response_text
        for statement_id in [value for value in self.internal_entries if value not in artifacts]:
            self.remove(statement_id)
        for statement_id, artifact in artifacts.items():
            entry = self.internal_entries.get(statement_id)
            if entry is not None and entry["artifact"] is artifact:
                continue
            content = sparse_content(artifact, include_response_text)
            if entry is not None and entry["content"] == content:
                entry["artifact"] = artifact
                continue
            if entry is not None:
                self.remove(statement_id)
            self.add(statement_id, artifact, content, include_response_text)

    def query_state(self, scope: dict, terms: set[str]) -> tuple[dict, int]:
        """Return a scorer state holding only ``terms``, and the bytes this request builds for it.

        The caller holds ``internal_lock`` until scoring is done, because the
        state refers to the partition's live documents.
        """
        partition = self.internal_partitions.get(sparse_scope_key(scope))
        documents = partition["documents"] if partition else {}
        stored = partition["postings"] if partition else {}
        postings = {term: tuple(sorted(stored[term].values())) for term in sorted(terms) if term in stored}
        count = len(documents)
        totals = partition["field_totals"] if partition else dict.fromkeys(SPARSE_FIELD_NAMES, 0)
        state = {
            "documents": documents,
            "postings": postings,
            "document_frequencies": {term: len(values) for term, values in postings.items()},
            "average_field_lengths": {name: (totals[name] / count if count else 0.0) for name in SPARSE_FIELD_NAMES},
        }
        working_memory_bytes = (
            512
            + len(postings) * SPARSE_TERM_WORKING_BYTES
            + sum(len(values) for values in postings.values()) * SPARSE_POSTING_REFERENCE_BYTES
        )
        result = (state, working_memory_bytes)
        return result


def build_sparse_working_set(
    artifacts: tuple[dict, ...],
    *,
    scope: dict,
    settings: dict[str, object],
    max_working_memory_bytes: int,
    trusted_artifacts: bool = False,
) -> dict:
    """Build every sparse structure for one scope from scratch.

    This is the reference for ``SparseIndex``: searching this state gives the
    same matches as an index-backed search. The budget charges the maps,
    postings, and state the build creates.
    """
    validated_settings = internal_validated_settings(settings)
    validated_scope = validate_scope_key(scope)
    if not isinstance(max_working_memory_bytes, int) or isinstance(max_working_memory_bytes, bool) or max_working_memory_bytes < 1:
        raise InvalidRequestError("sparse working-memory budget must be a positive integer")
    if not isinstance(trusted_artifacts, bool):
        raise InvalidRequestError("sparse trusted_artifacts must be a boolean")
    include_response_text = validated_settings.get("include_response_text", False)
    documents: dict[str, dict] = {}
    term_postings: dict[str, list[tuple]] = {}
    field_totals = dict.fromkeys(SPARSE_FIELD_NAMES, 0)
    working_memory_bytes = estimate_working_bytes((documents, term_postings, field_totals))

    def exhausted() -> dict:
        result = {
            "complete": False,
            "reason": "working_memory_budget",
            "working_memory_bytes": min(working_memory_bytes, max_working_memory_bytes),
            "state_working_memory_bytes": 0,
            "state": {},
        }
        return result

    if working_memory_bytes > max_working_memory_bytes:
        result = exhausted()
        return result
    for artifact_value in artifacts:
        artifact = artifact_value if trusted_artifacts else validate_cached_response_artifact(artifact_value)
        if artifact.get("lifecycle", LifecycleState.RETIRED) != LifecycleState.ACTIVE:
            continue
        if artifact.get("scope", {}) != validated_scope:
            continue
        parts = sparse_document_parts(artifact, include_response_text)
        statement_id = parts["statement_id"]
        if statement_id in documents:
            raise InvalidRequestError(f"duplicate sparse statement_id: {statement_id}")
        increment = SPARSE_DOCUMENT_REFERENCE_BYTES + SPARSE_POSTING_REFERENCE_BYTES * len(parts["postings"])
        if working_memory_bytes + increment > max_working_memory_bytes:
            result = exhausted()
            return result
        working_memory_bytes += increment
        documents[statement_id] = parts["document"]
        for field_name in SPARSE_FIELD_NAMES:
            field_totals[field_name] = field_totals.get(field_name, 0) + parts["token_lengths"][field_name]
        for term, posting in parts["postings"].items():
            term_postings.setdefault(term, []).append(posting)

    count = len(documents)
    final_increment = 512 + count * 64 + len(term_postings) * SPARSE_TERM_WORKING_BYTES
    if working_memory_bytes + final_increment > max_working_memory_bytes:
        result = exhausted()
        return result
    working_memory_bytes += final_increment
    postings = {term: tuple(sorted(term_postings[term])) for term in sorted(term_postings)}
    averages = {name: (field_totals.get(name, 0) / count if count else 0.0) for name in SPARSE_FIELD_NAMES}
    state = {
        "documents": dict(sorted(documents.items())),
        "postings": postings,
        "document_frequencies": {term: len(values) for term, values in postings.items()},
        "average_field_lengths": averages,
    }
    result = {
        "complete": True,
        "reason": "",
        "working_memory_bytes": working_memory_bytes,
        "state_working_memory_bytes": working_memory_bytes,
        "state": state,
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
            name: raw_fields[name] / (raw_fields[name] + normalization) if raw_fields[name] else 0.0 for name in SPARSE_FIELD_NAMES
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


def search_sparse_index(
    index: SparseIndex,
    artifacts: tuple[dict, ...],
    text: str,
    scope: dict,
    settings: dict,
    *,
    query_identifiers: tuple[str, ...],
    descriptors: dict[str, float],
    limit: int,
    max_working_memory_bytes: int,
    trusted_artifacts: bool,
) -> dict:
    """Sync the index to ``artifacts`` and score the query's postings."""
    current = {}
    for artifact_value in artifacts:
        artifact = artifact_value if trusted_artifacts else validate_cached_response_artifact(artifact_value)
        statement_id = artifact.get("statement_id", "")
        if statement_id in current:
            raise InvalidRequestError(f"duplicate sparse statement_id: {statement_id}")
        current[statement_id] = artifact
    # Every term the scorer can look up: the query descriptors, and the
    # trigrams it adds for an identifier with no exact posting.
    terms = set(descriptors)
    for identifier in query_identifiers:
        terms.update(f"g:{ngram}" for ngram in character_ngrams(identifier))
    with index.internal_lock:
        index.sync(current, settings.get("include_response_text", False))
        state, state_memory = index.query_state(scope, terms)
        remaining_memory = max_working_memory_bytes - state_memory
        if remaining_memory < 1:
            result = {
                "matches": (),
                "complete": False,
                "reason": "working_memory_budget",
                "query_term_count": 0,
                "posting_visits": 0,
                "working_memory_bytes": min(state_memory, max_working_memory_bytes),
            }
            return result
        result = search_sparse_working_set(
            state,
            text,
            scope,
            limit=limit,
            max_query_terms=settings.get("max_query_terms", 1),
            max_posting_visits=settings.get("max_posting_visits", 1),
            max_prefix_expansions=settings.get("max_prefix_expansions", 1),
            max_working_memory_bytes=remaining_memory,
        )
    result["working_memory_bytes"] = state_memory + result.get("working_memory_bytes", 0)
    return result


def search_sparse_artifacts(
    artifacts: tuple[dict, ...],
    text: str,
    scope: dict,
    settings: dict[str, object],
    *,
    limit: int,
    max_working_memory_bytes: int,
    trusted_artifacts: bool = False,
    index: object = (),
) -> dict:
    """Search the artifacts' sparse representations for one scope.

    With a ``SparseIndex`` the index is synced to ``artifacts`` and only the
    query's postings are read; without one every structure is built for this
    request. ``trusted_artifacts`` skips per-artifact validation for
    artifacts that a repository snapshot has just validated.
    """
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
    if not isinstance(text, str) or not text.strip():
        raise InvalidRequestError("sparse query text must be a non-empty string")
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= MAX_SPARSE_RESULTS:
        raise InvalidRequestError(f"sparse limit must be an integer from 1 through {MAX_SPARSE_RESULTS}")
    if not isinstance(max_working_memory_bytes, int) or isinstance(max_working_memory_bytes, bool) or max_working_memory_bytes < 1:
        raise InvalidRequestError("sparse working-memory budget must be a positive integer")
    validated_scope = validate_scope_key(scope)
    query_tokens, query_identifiers, descriptors = query_descriptors(
        text,
        validated_settings.get("max_prefix_expansions", 1),
    )
    query_term_count = len({*(f"t:{token}" for token in query_tokens), *(f"x:{value}" for value in query_identifiers)})
    if query_term_count > validated_settings.get("max_query_terms", 1):
        result = {
            "matches": (),
            "complete": False,
            "reason": "query_term_budget",
            "query_term_count": query_term_count,
            "posting_visits": 0,
            "working_memory_bytes": 0,
        }
        return result
    query_working_memory = estimate_working_bytes((query_tokens, query_identifiers, descriptors))
    if query_working_memory > max_working_memory_bytes:
        result = {
            "matches": (),
            "complete": False,
            "reason": "working_memory_budget",
            "query_term_count": query_term_count,
            "posting_visits": 0,
            "working_memory_bytes": max_working_memory_bytes,
        }
        return result
    if isinstance(index, SparseIndex):
        result = search_sparse_index(
            index,
            artifacts,
            text,
            validated_scope,
            validated_settings,
            query_identifiers=query_identifiers,
            descriptors=descriptors,
            limit=limit,
            max_working_memory_bytes=max_working_memory_bytes,
            trusted_artifacts=trusted_artifacts,
        )
        return result
    construction = build_sparse_working_set(
        artifacts,
        scope=validated_scope,
        settings=validated_settings,
        max_working_memory_bytes=max_working_memory_bytes,
        trusted_artifacts=trusted_artifacts,
    )
    construction_peak_memory = construction.get("working_memory_bytes", 0)
    if not construction.get("complete", False):
        result = {
            "matches": (),
            "complete": False,
            "reason": construction.get("reason", "working_memory_budget"),
            "query_term_count": 0,
            "posting_visits": 0,
            "working_memory_bytes": construction_peak_memory,
        }
        return result
    state_memory = construction.get("state_working_memory_bytes", 0)
    remaining_memory = max_working_memory_bytes - state_memory
    if remaining_memory < 1:
        result = {
            "matches": (),
            "complete": False,
            "reason": "working_memory_budget",
            "query_term_count": 0,
            "posting_visits": 0,
            "working_memory_bytes": max(construction_peak_memory, state_memory),
        }
        return result
    state = construction.get("state", {})
    if not isinstance(state, dict):
        raise InvalidRequestError("sparse construction state must be an object")
    result = search_sparse_working_set(
        state,
        text,
        validated_scope,
        limit=limit,
        max_query_terms=validated_settings.get("max_query_terms", 1),
        max_posting_visits=validated_settings.get("max_posting_visits", 1),
        max_prefix_expansions=validated_settings.get("max_prefix_expansions", 1),
        max_working_memory_bytes=remaining_memory,
    )
    result["working_memory_bytes"] = max(
        construction_peak_memory,
        state_memory + result.get("working_memory_bytes", 0),
    )
    return result
