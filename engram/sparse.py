"""Versioned, rebuildable fielded sparse retrieval over accepted responses.

Accepted-response artifacts remain authoritative.  This module owns only an
immutable derived index that can be discarded, checked, and rebuilt from the
repository at any time.
"""

import hashlib
import json
import math
import re
import threading
import unicodedata
from bisect import bisect_left
from collections import Counter
from collections.abc import Iterable, Mapping
from types import MappingProxyType

from engram.artifacts import CachedResponseArtifact, validate_cached_response_artifact
from engram.config import SparseConfig, sparse_config
from engram.constants import (
    DEFAULT_STOPWORDS,
    SPARSE_DOCUMENT_SCHEMA_VERSION,
    SPARSE_INDEX_SCHEMA_VERSION,
    SPARSE_INDEX_VERSION,
    SPARSE_TOKENIZER_VERSION,
)
from engram.errors import InvalidRequestError
from engram.identity import ScopeKey, validate_scope_key

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
SPARSE_FIELD_WEIGHTS: Mapping[str, float] = MappingProxyType(
    {
        "canonical": 3.0,
        "aliases": 2.5,
        "entities": 2.25,
        "relation": 2.0,
        "keywords": 1.5,
        "technical_identifiers": 3.5,
        "response_text": 0.25,
    }
)

_GENERAL_TOKEN = re.compile(r"[^\W_]+(?:['’][^\W_]+)?", re.UNICODE)
_VERSION_TOKEN = re.compile(r"^v?\d+(?:\.\d+){1,5}(?:[-+][a-z0-9._-]+)?$", re.IGNORECASE)
_ERROR_CODE = re.compile(r"^[A-Z][A-Z0-9_]{2,}$")
_MIXED_IDENTIFIER = re.compile(r"^(?=.*[A-Za-z])(?=.*\d)[A-Za-z0-9._:/\\+#@%$|&*<>=-]+$")
_TECHNICAL_PUNCTUATION = set("._:/\\-+#@%$|&*<>=")
_BOUNDARY_PUNCTUATION = "\"'`()[]{};,!?"


def _validated_settings(value: Mapping[str, object]) -> SparseConfig:
    if not value:
        return sparse_config()
    keys = {
        "enabled",
        "include_response_text",
        "max_query_terms",
        "max_posting_visits",
        "max_prefix_expansions",
    }
    if set(value) != keys:
        raise InvalidRequestError("sparse settings have invalid fields")
    enabled = value["enabled"]
    include_response_text = value["include_response_text"]
    max_query_terms = value["max_query_terms"]
    max_posting_visits = value["max_posting_visits"]
    max_prefix_expansions = value["max_prefix_expansions"]
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


SparseDocument = dict


SparsePosting = tuple[str, tuple[tuple[str, int], ...]]
EMPTY_SPARSE_POSTING: SparsePosting = ("", ())


SparseIndexState = dict


SparseMatch = dict


SparseSearchResult = dict


SparseIndexCheckReport = dict


def _normalized_text(value: str) -> str:
    result = unicodedata.normalize("NFKC", value).casefold().strip()
    return result


def _bounded_token(value: str) -> str:
    normalized = _normalized_text(value)
    result = normalized if normalized and len(normalized.encode("utf-8")) <= MAX_SPARSE_TOKEN_BYTES else ""
    return result


def _technical_identifier(value: str) -> bool:
    if not value or len(value.encode("utf-8")) > MAX_SPARSE_TOKEN_BYTES:
        return False
    if _ERROR_CODE.fullmatch(value) or _VERSION_TOKEN.fullmatch(value) or _MIXED_IDENTIFIER.fullmatch(value):
        return True
    if any(character in _TECHNICAL_PUNCTUATION for character in value):
        return True
    return any(first.islower() and second.isupper() for first, second in zip(value, value[1:], strict=False))


def technical_identifiers(value: str) -> tuple[str, ...]:
    """Extract bounded punctuation-preserving technical identifiers."""
    if not isinstance(value, str):
        raise InvalidRequestError("sparse technical-token input must be a string")
    found = []
    seen = set()
    for raw in value.split():
        candidate = raw.strip(_BOUNDARY_PUNCTUATION)
        if not _technical_identifier(candidate):
            continue
        normalized = _bounded_token(candidate)
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
    normalized = _normalized_text(value)
    tokens = [_bounded_token(match.group(0)) for match in _GENERAL_TOKEN.finditer(normalized)]
    result = tuple(token for token in tokens if token and token not in DEFAULT_STOPWORDS)[:MAX_SPARSE_TOKENS_PER_FIELD]
    return result


def _field_texts(artifact: CachedResponseArtifact, include_response_text: bool) -> dict[str, tuple[str, ...]]:
    identity = artifact["query_identity"]
    entity_values = []
    for entity in identity["entities"]:
        entity_values.append(entity["surface"])
        if entity["canonical_id"]:
            entity_values.append(entity["canonical_id"])
    relation_values = tuple(value for value in (identity["relation"]["surface"], identity["relation"]["canonical_id"]) if value)
    source_values = (
        artifact["retrieval"]["canonical"],
        *artifact["retrieval"]["aliases"],
        *entity_values,
        *relation_values,
        *identity["lexical_terms"],
    )
    identifiers = []
    for value in source_values:
        identifiers.extend(technical_identifiers(value))
    fields = {
        "canonical": (artifact["retrieval"]["canonical"],),
        "aliases": tuple(artifact["retrieval"]["aliases"]),
        "entities": tuple(entity_values),
        "relation": relation_values,
        "keywords": tuple(identity["lexical_terms"]),
        "technical_identifiers": tuple(dict.fromkeys(identifiers))[:MAX_SPARSE_TECHNICAL_IDENTIFIERS],
        "response_text": (artifact["response"],) if include_response_text else (),
    }
    result = {
        name: tuple(_normalized_text(value) for value in fields[name] if value)[:MAX_SPARSE_FIELD_TEXTS]
        for name in SPARSE_FIELD_NAMES
    }
    return result


def _sparse_document_from_validated_artifact(
    artifact: CachedResponseArtifact,
    include_response_text: bool,
) -> SparseDocument:
    fields = _field_texts(artifact, include_response_text)
    tokens = {
        name: tuple(token for text in fields[name] for token in sparse_tokens(text))[:MAX_SPARSE_TOKENS_PER_FIELD]
        for name in SPARSE_FIELD_NAMES
    }
    result = MappingProxyType(
        {
            "schema_version": SPARSE_DOCUMENT_SCHEMA_VERSION,
            "statement_id": artifact["statement_id"],
            "scope": MappingProxyType(validate_scope_key(artifact["scope"])),
            "lifecycle": artifact["lifecycle"],
            "fields": MappingProxyType(fields),
            "tokens": MappingProxyType(tokens),
            "technical_identifiers": fields["technical_identifiers"],
        }
    )
    return result


def sparse_document_from_artifact(value: object, *, include_response_text: bool = False) -> SparseDocument:
    """Validate and project one authoritative accepted-response artifact."""
    if not isinstance(include_response_text, bool):
        raise InvalidRequestError("sparse include_response_text must be a boolean")
    artifact = validate_cached_response_artifact(value)
    result = _sparse_document_from_validated_artifact(artifact, include_response_text)
    return result


def _prefixes(token: str) -> tuple[str, ...]:
    upper = min(MAX_PREFIX_LENGTH, len(token) - 1)
    result = tuple(token[:length] for length in range(MIN_PREFIX_LENGTH, upper + 1)) if upper >= MIN_PREFIX_LENGTH else ()
    return result


def _character_ngrams(token: str) -> tuple[str, ...]:
    compact = "".join(character for character in token if not character.isspace())
    if len(compact) < CHAR_NGRAM_SIZE:
        return ()
    result = tuple(dict.fromkeys(compact[index : index + CHAR_NGRAM_SIZE] for index in range(len(compact) - 2)))
    return result


def _document_term_frequencies(document: SparseDocument) -> dict[str, Counter[str]]:
    frequencies: dict[str, Counter[str]] = {}
    for field_name in SPARSE_FIELD_NAMES:
        counter: Counter[str] = Counter()
        for token in document["tokens"][field_name]:
            counter[f"t:{token}"] += 1
            if field_name in {"canonical", "aliases"} and len(token) >= 6:
                for prefix in _prefixes(token):
                    counter[f"p:{prefix}"] += 1
        if field_name == "technical_identifiers":
            for identifier in document["technical_identifiers"]:
                counter[f"x:{identifier}"] += 1
                for prefix in _prefixes(identifier):
                    counter[f"p:{prefix}"] += 1
                for ngram in _character_ngrams(identifier):
                    counter[f"g:{ngram}"] += 1
        frequencies[field_name] = counter
    return frequencies


def _config_fingerprint(settings: Mapping[str, object]) -> str:
    payload = {
        "index_version": SPARSE_INDEX_VERSION,
        "tokenizer_version": SPARSE_TOKENIZER_VERSION,
        "include_response_text": settings["include_response_text"],
        "field_weights": dict(SPARSE_FIELD_WEIGHTS),
    }
    result = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
    return result


def _document_signature(document: SparseDocument) -> dict[str, object]:
    result = {
        "statement_id": document["statement_id"],
        "scope": dict(document["scope"]),
        "lifecycle": document["lifecycle"].value,
        "fields": {name: list(document["fields"][name]) for name in SPARSE_FIELD_NAMES},
        "tokens": {name: list(document["tokens"][name]) for name in SPARSE_FIELD_NAMES},
        "technical_identifiers": list(document["technical_identifiers"]),
    }
    return result


def _document_digest(document: SparseDocument) -> int:
    encoded = json.dumps(
        _document_signature(document),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    result = int.from_bytes(hashlib.sha256(encoded).digest(), "big")
    return result


def _documents_fingerprint(documents: Mapping[str, SparseDocument], config_fingerprint: str) -> str:
    aggregate = int(config_fingerprint, 16)
    for document in documents.values():
        aggregate ^= _document_digest(document)
    result = f"{aggregate:064x}"
    return result


def _index_structure_fingerprint(
    postings: Mapping[str, tuple[SparsePosting, ...]],
    averages: Mapping[str, float],
    config_fingerprint: str,
) -> str:
    digest = hashlib.sha256()
    digest.update(config_fingerprint.encode("ascii"))
    digest.update(json.dumps(dict(averages), sort_keys=True, separators=(",", ":")).encode("utf-8"))
    for term in sorted(postings):
        digest.update(term.encode("utf-8"))
        for posting in postings[term]:
            digest.update(posting[0].encode("utf-8"))
            digest.update(
                json.dumps(
                    dict(posting[1]),
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            )
    result = digest.hexdigest()
    return result


def build_sparse_index_state(
    artifacts: Iterable[CachedResponseArtifact],
    *,
    repository_state_generation: int,
    settings: Mapping[str, object],
    state_generation: int = 1,
    trusted_artifacts: bool = False,
) -> SparseIndexState:
    """Build a complete immutable candidate index without touching live state."""
    if (
        not isinstance(repository_state_generation, int)
        or isinstance(repository_state_generation, bool)
        or repository_state_generation < 1
    ):
        raise InvalidRequestError("sparse repository_state_generation must be a positive integer")
    if not isinstance(state_generation, int) or isinstance(state_generation, bool) or state_generation < 1:
        raise InvalidRequestError("sparse state_generation must be a positive integer")
    validated_settings = _validated_settings(settings)
    if not isinstance(trusted_artifacts, bool):
        raise InvalidRequestError("sparse trusted_artifacts must be a boolean")
    include_response_text = validated_settings["include_response_text"]
    documents: dict[str, SparseDocument] = {}
    term_documents: dict[str, dict[str, dict[str, int]]] = {}
    field_totals = dict.fromkeys(SPARSE_FIELD_NAMES, 0)
    for artifact_value in artifacts:
        artifact = artifact_value if trusted_artifacts else validate_cached_response_artifact(artifact_value)
        document = _sparse_document_from_validated_artifact(artifact, include_response_text)
        statement_id = document["statement_id"]
        if statement_id in documents:
            raise InvalidRequestError(f"duplicate sparse statement_id: {statement_id}")
        documents[statement_id] = document
        for field_name in SPARSE_FIELD_NAMES:
            field_totals[field_name] += len(document["tokens"][field_name])
        for field_name, counter in _document_term_frequencies(document).items():
            for term, frequency in counter.items():
                term_documents.setdefault(term, {}).setdefault(statement_id, {})[field_name] = frequency

    postings: dict[str, tuple[SparsePosting, ...]] = {}
    document_frequencies = {}
    for term in sorted(term_documents):
        posting_values: tuple[SparsePosting, ...] = tuple(
            (statement_id, tuple(sorted(term_documents[term][statement_id].items())))
            for statement_id in sorted(term_documents[term])
        )
        postings[term] = posting_values
        document_frequencies[term] = len(posting_values)
    count = len(documents)
    averages = {name: (field_totals[name] / count if count else 0.0) for name in SPARSE_FIELD_NAMES}
    config_fingerprint = _config_fingerprint(validated_settings)
    frozen_documents = MappingProxyType(dict(sorted(documents.items())))
    frozen_postings = MappingProxyType(postings)
    frozen_averages = MappingProxyType(averages)
    fingerprint = _documents_fingerprint(frozen_documents, config_fingerprint)
    result = MappingProxyType(
        {
            "schema_version": SPARSE_INDEX_SCHEMA_VERSION,
            "index_version": SPARSE_INDEX_VERSION,
            "tokenizer_version": SPARSE_TOKENIZER_VERSION,
            "state_generation": state_generation,
            "repository_state_generation": repository_state_generation,
            "config_fingerprint": config_fingerprint,
            "documents": frozen_documents,
            "postings": frozen_postings,
            "document_frequencies": MappingProxyType(document_frequencies),
            "average_field_lengths": frozen_averages,
            "fingerprint": fingerprint,
        }
    )
    return result


def update_sparse_index_state(
    live: SparseIndexState,
    artifacts: Mapping[str, CachedResponseArtifact],
    changed_statement_ids: tuple[str, ...],
    *,
    repository_state_generation: int,
    settings: Mapping[str, object],
) -> SparseIndexState:
    """Build an immutable incremental candidate equal to a clean rebuild."""
    validated_settings = _validated_settings(settings)
    documents = dict(live["documents"])
    postings = dict(live["postings"])
    document_frequencies = dict(live["document_frequencies"])
    old_count = len(documents)
    field_totals = {name: int(round(live["average_field_lengths"][name] * old_count)) for name in SPARSE_FIELD_NAMES}
    include_response_text = validated_settings["include_response_text"]
    for statement_id in changed_statement_ids:
        previous = documents.get(statement_id)
        replacement_artifact = artifacts.get(statement_id)
        replacement = (
            _sparse_document_from_validated_artifact(replacement_artifact, include_response_text)
            if replacement_artifact is not None
            else None
        )
        previous_terms = _document_term_frequencies(previous) if previous is not None else {}
        replacement_terms = _document_term_frequencies(replacement) if replacement is not None else {}
        if previous is not None:
            for name in SPARSE_FIELD_NAMES:
                field_totals[name] -= len(previous["tokens"][name])
            documents.pop(statement_id, None)
        if replacement is not None:
            documents[statement_id] = replacement
            for name in SPARSE_FIELD_NAMES:
                field_totals[name] += len(replacement["tokens"][name])
        changed_terms = {term for field_values in (*previous_terms.values(), *replacement_terms.values()) for term in field_values}
        for term in changed_terms:
            field_frequencies = {
                name: replacement_terms[name][term] for name in replacement_terms if replacement_terms[name].get(term, 0)
            }
            replacement_posting: SparsePosting = (
                (statement_id, tuple(sorted(field_frequencies.items()))) if field_frequencies else EMPTY_SPARSE_POSTING
            )
            term_postings = _posting_with_replacement(
                postings.get(term, ()),
                statement_id,
                replacement_posting,
            )
            if term_postings:
                postings[term] = term_postings
                document_frequencies[term] = len(term_postings)
            else:
                postings.pop(term, None)
                document_frequencies.pop(term, None)
    count = len(documents)
    averages = MappingProxyType({name: (field_totals[name] / count if count else 0.0) for name in SPARSE_FIELD_NAMES})
    frozen_documents = MappingProxyType(documents)
    frozen_postings = MappingProxyType(postings)
    config_fingerprint = _config_fingerprint(validated_settings)
    fingerprint_value = int(live["fingerprint"], 16)
    for statement_id in changed_statement_ids:
        previous = live["documents"].get(statement_id)
        replacement = frozen_documents.get(statement_id)
        if previous is not None:
            fingerprint_value ^= _document_digest(previous)
        if replacement is not None:
            fingerprint_value ^= _document_digest(replacement)
    fingerprint = f"{fingerprint_value:064x}"
    result = MappingProxyType(
        SparseIndexState(
            schema_version=SPARSE_INDEX_SCHEMA_VERSION,
            index_version=SPARSE_INDEX_VERSION,
            tokenizer_version=SPARSE_TOKENIZER_VERSION,
            state_generation=live["state_generation"] + 1,
            repository_state_generation=repository_state_generation,
            config_fingerprint=config_fingerprint,
            documents=frozen_documents,
            postings=frozen_postings,
            document_frequencies=MappingProxyType(document_frequencies),
            average_field_lengths=averages,
            fingerprint=fingerprint,
        )
    )
    return result


def _phrase_present(tokens: tuple[str, ...], query: tuple[str, ...]) -> bool:
    if not query or len(query) > len(tokens):
        return False
    result = any(tokens[index : index + len(query)] == query for index in range(len(tokens) - len(query) + 1))
    return result


def _minimum_proximity(tokens: tuple[str, ...], query: tuple[str, ...]) -> int:
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


def _query_descriptors(
    text: str,
    max_prefix_expansions: int,
) -> tuple[tuple[str, ...], tuple[str, ...], Mapping[str, float]]:
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
    return tokens, identifiers, MappingProxyType(descriptors)


def _posting_for(postings: tuple[SparsePosting, ...], statement_id: str) -> SparsePosting:
    position = bisect_left(postings, statement_id, key=lambda posting: posting[0])
    result = postings[position] if position < len(postings) and postings[position][0] == statement_id else EMPTY_SPARSE_POSTING
    return result


def _posting_with_replacement(
    postings: tuple[SparsePosting, ...],
    statement_id: str,
    replacement: SparsePosting,
) -> tuple[SparsePosting, ...]:
    position = bisect_left(postings, statement_id, key=lambda posting: posting[0])
    present = position < len(postings) and postings[position][0] == statement_id
    before = postings[:position]
    after = postings[position + 1 :] if present else postings[position:]
    result = (*before, replacement, *after) if replacement[0] else (*before, *after)
    return result


def search_sparse_index(
    state: SparseIndexState,
    text: str,
    scope: ScopeKey,
    *,
    limit: int,
    max_query_terms: int,
    max_posting_visits: int,
    max_prefix_expansions: int,
    max_working_memory_bytes: int,
) -> SparseSearchResult:
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
    query_tokens, query_identifiers, base_descriptors = _query_descriptors(text, max_prefix_expansions)
    base_term_count = len({*(f"t:{token}" for token in query_tokens), *(f"x:{value}" for value in query_identifiers)})
    if base_term_count > max_query_terms:
        return SparseSearchResult(
            matches=(),
            complete=False,
            reason="query_term_budget",
            query_term_count=base_term_count,
            posting_visits=0,
            working_memory_bytes=0,
        )
    descriptors = dict(base_descriptors)
    posting_visits = 0
    exact_identifier_sets = []
    exact_identifier_item_count = 0
    for identifier in query_identifiers:
        statement_ids = set()
        for posting in state["postings"].get(f"x:{identifier}", ()):
            posting_visits += 1
            if posting_visits > max_posting_visits:
                return SparseSearchResult(
                    matches=(),
                    complete=False,
                    reason="posting_visit_budget",
                    query_term_count=len(descriptors),
                    posting_visits=posting_visits,
                    working_memory_bytes=256 + len(descriptors) * SPARSE_DESCRIPTOR_WORKING_BYTES,
                )
            if state["documents"][posting[0]]["scope"] != validated_scope:
                continue
            projected_items = exact_identifier_item_count + 1
            projected_memory = (
                256 + len(descriptors) * SPARSE_DESCRIPTOR_WORKING_BYTES + projected_items * SPARSE_IDENTIFIER_WORKING_BYTES
            )
            if projected_memory > max_working_memory_bytes:
                return SparseSearchResult(
                    matches=(),
                    complete=False,
                    reason="working_memory_budget",
                    query_term_count=len(descriptors),
                    posting_visits=posting_visits,
                    working_memory_bytes=256
                    + len(descriptors) * SPARSE_DESCRIPTOR_WORKING_BYTES
                    + exact_identifier_item_count * SPARSE_IDENTIFIER_WORKING_BYTES,
                )
            statement_ids.add(posting[0])
            exact_identifier_item_count = projected_items
        exact_identifier_sets.append(statement_ids)
        if statement_ids:
            continue
        ngrams = _character_ngrams(identifier)
        multiplier = 0.15 / max(1, len(ngrams))
        for ngram in ngrams:
            descriptors[f"g:{ngram}"] = multiplier
    if len(descriptors) > max_query_terms:
        return SparseSearchResult(
            matches=(),
            complete=False,
            reason="query_term_budget",
            query_term_count=len(descriptors),
            posting_visits=posting_visits,
            working_memory_bytes=0,
        )
    if not descriptors:
        return SparseSearchResult(
            matches=(),
            complete=True,
            reason="no_sparse_terms",
            query_term_count=0,
            posting_visits=posting_visits,
            working_memory_bytes=0,
        )

    scores: dict[str, dict[str, float]] = {}
    matched_keys: dict[str, set[str]] = {}
    document_count = max(1, len(state["documents"]))
    candidate_pool: set[str] = set()
    populated_identifier_sets = [value for value in exact_identifier_sets if value]
    if populated_identifier_sets:
        projected_memory = (
            256
            + len(descriptors) * SPARSE_DESCRIPTOR_WORKING_BYTES
            + (exact_identifier_item_count + len(populated_identifier_sets[0])) * SPARSE_IDENTIFIER_WORKING_BYTES
        )
        if projected_memory > max_working_memory_bytes:
            return SparseSearchResult(
                matches=(),
                complete=False,
                reason="working_memory_budget",
                query_term_count=len(descriptors),
                posting_visits=posting_visits,
                working_memory_bytes=256
                + len(descriptors) * SPARSE_DESCRIPTOR_WORKING_BYTES
                + exact_identifier_item_count * SPARSE_IDENTIFIER_WORKING_BYTES,
            )
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
                        return SparseSearchResult(
                            matches=(),
                            complete=False,
                            reason="working_memory_budget",
                            query_term_count=len(descriptors),
                            posting_visits=posting_visits,
                            working_memory_bytes=256
                            + len(descriptors) * SPARSE_DESCRIPTOR_WORKING_BYTES
                            + (exact_identifier_item_count + len(candidate_pool)) * SPARSE_IDENTIFIER_WORKING_BYTES,
                        )
                    candidate_pool.add(statement_id)
    else:
        rare_limit = max(32, min(1_000, document_count // 10))
        anchor_terms = sorted(
            (state["document_frequencies"].get(term, 0), term)
            for term in descriptors
            if 0 < state["document_frequencies"].get(term, 0) <= rare_limit
        )[:4]
        for _frequency, term in anchor_terms:
            for posting in state["postings"].get(term, ()):
                posting_visits += 1
                if posting_visits > max_posting_visits:
                    return SparseSearchResult(
                        matches=(),
                        complete=False,
                        reason="posting_visit_budget",
                        query_term_count=len(descriptors),
                        posting_visits=posting_visits,
                        working_memory_bytes=256
                        + len(descriptors) * SPARSE_DESCRIPTOR_WORKING_BYTES
                        + len(candidate_pool) * SPARSE_IDENTIFIER_WORKING_BYTES,
                    )
                statement_id = posting[0]
                if state["documents"][statement_id]["scope"] != validated_scope or statement_id in candidate_pool:
                    continue
                projected_memory = (
                    256
                    + len(descriptors) * SPARSE_DESCRIPTOR_WORKING_BYTES
                    + (len(candidate_pool) + 1) * SPARSE_IDENTIFIER_WORKING_BYTES
                )
                if projected_memory > max_working_memory_bytes:
                    return SparseSearchResult(
                        matches=(),
                        complete=False,
                        reason="working_memory_budget",
                        query_term_count=len(descriptors),
                        posting_visits=posting_visits,
                        working_memory_bytes=256
                        + len(descriptors) * SPARSE_DESCRIPTOR_WORKING_BYTES
                        + len(candidate_pool) * SPARSE_IDENTIFIER_WORKING_BYTES,
                    )
                candidate_pool.add(statement_id)
    exact_identifier_sets.clear()
    working_memory = (
        256 + len(descriptors) * SPARSE_DESCRIPTOR_WORKING_BYTES + len(candidate_pool) * SPARSE_IDENTIFIER_WORKING_BYTES
    )
    if working_memory > max_working_memory_bytes:
        return SparseSearchResult(
            matches=(),
            complete=False,
            reason="working_memory_budget",
            query_term_count=len(descriptors),
            posting_visits=posting_visits,
            working_memory_bytes=0,
        )
    for term, multiplier in descriptors.items():
        postings = state["postings"].get(term, ())
        if candidate_pool:
            selected_postings = (posting for statement_id in candidate_pool if (posting := _posting_for(postings, statement_id))[0])
        else:
            selected_postings = postings
        document_frequency = state["document_frequencies"].get(term, 0)
        inverse_frequency = math.log(1.0 + (document_count - document_frequency + 0.5) / (document_frequency + 0.5))
        for posting in selected_postings:
            posting_visits += 1
            if posting_visits > max_posting_visits:
                return SparseSearchResult(
                    matches=(),
                    complete=False,
                    reason="posting_visit_budget",
                    query_term_count=len(descriptors),
                    posting_visits=posting_visits,
                    working_memory_bytes=working_memory,
                )
            statement_id = posting[0]
            document = state["documents"][statement_id]
            if document["scope"] != validated_scope:
                continue
            if statement_id not in scores:
                projected_memory = working_memory + SPARSE_SCORE_WORKING_BYTES
                if projected_memory > max_working_memory_bytes:
                    return SparseSearchResult(
                        matches=(),
                        complete=False,
                        reason="working_memory_budget",
                        query_term_count=len(descriptors),
                        posting_visits=posting_visits,
                        working_memory_bytes=working_memory,
                    )
                working_memory = projected_memory
            contributions = scores.setdefault(statement_id, dict.fromkeys(SPARSE_FIELD_NAMES, 0.0))
            matched_keys.setdefault(statement_id, set()).add(term)
            for field_name, frequency in posting[1]:
                field_length = len(document["tokens"][field_name])
                average_length = state["average_field_lengths"][field_name] or 1.0
                denominator = frequency + 1.2 * (1.0 - 0.75 + 0.75 * field_length / average_length)
                bm25 = inverse_frequency * (frequency * 2.2) / denominator
                contributions[field_name] += bm25 * SPARSE_FIELD_WEIGHTS[field_name] * multiplier
    query_ngrams = {f"g:{ngram}" for identifier in query_identifiers for ngram in _character_ngrams(identifier)}
    retained = []
    for statement_id, raw_fields in scores.items():
        document = state["documents"][statement_id]
        raw_total = sum(raw_fields.values())
        normalization = 2.0 * max(1, len(query_tokens))
        base_score = raw_total / (raw_total + normalization) if raw_total else 0.0
        phrase_fields = tuple(name for name in SPARSE_FIELD_NAMES if _phrase_present(document["tokens"][name], query_tokens))
        proximity = {name: _minimum_proximity(document["tokens"][name], query_tokens) for name in SPARSE_FIELD_NAMES}
        proximity_fields = tuple(name for name in SPARSE_FIELD_NAMES if proximity[name] and proximity[name] <= 8)
        minimum_proximity = min((value for value in proximity.values() if value), default=0)
        keys = matched_keys[statement_id]
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
                return SparseSearchResult(
                    matches=(),
                    complete=False,
                    reason="working_memory_budget",
                    query_term_count=len(descriptors),
                    posting_visits=posting_visits,
                    working_memory_bytes=working_memory,
                )
            working_memory = projected_memory
        field_contributions = MappingProxyType(
            {
                name: raw_fields[name] / (raw_fields[name] + normalization) if raw_fields[name] else 0.0
                for name in SPARSE_FIELD_NAMES
            }
        )
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
    result = SparseSearchResult(
        matches=retained_values,
        complete=True,
        reason="sparse_candidates" if retained_values else "sparse_miss",
        query_term_count=len(descriptors),
        posting_visits=posting_visits,
        working_memory_bytes=working_memory,
    )
    return result


class SparseIndexOwner:
    """Atomic owner for one immutable, repository-derived sparse index."""

    def __init__(self, settings: Mapping[str, object], repository_state_generation: int = 1) -> None:
        self._settings = _validated_settings(settings)
        self._lock = threading.RLock()
        self._healthy = True
        self._last_error = ""
        self._state = build_sparse_index_state(
            (),
            repository_state_generation=repository_state_generation,
            settings=self._settings,
        )

    @property
    def enabled(self) -> bool:
        return self._settings["enabled"]

    @property
    def available(self) -> bool:
        with self._lock:
            result = bool(self.enabled and self._healthy)
            return result

    @property
    def last_error(self) -> str:
        with self._lock:
            result = self._last_error
            return result

    def snapshot(self) -> SparseIndexState:
        with self._lock:
            result = self._state
            return result

    def rebuild(
        self,
        artifacts: Iterable[CachedResponseArtifact],
        repository_state_generation: int,
    ) -> SparseIndexState:
        if not self.enabled:
            with self._lock:
                candidate = build_sparse_index_state(
                    (),
                    repository_state_generation=repository_state_generation,
                    settings=self._settings,
                    state_generation=self._state["state_generation"] + 1,
                    trusted_artifacts=True,
                )
                self._state = candidate
                self._healthy = True
                self._last_error = ""
                return self._state
        artifact_values = tuple(artifacts)
        with self._lock:
            next_generation = self._state["state_generation"] + 1
        candidate = build_sparse_index_state(
            artifact_values,
            repository_state_generation=repository_state_generation,
            settings=self._settings,
            state_generation=next_generation,
            trusted_artifacts=True,
        )
        with self._lock:
            if repository_state_generation < self._state["repository_state_generation"]:
                return self._state
            if candidate["state_generation"] <= self._state["state_generation"]:
                candidate = MappingProxyType(
                    {
                        **candidate,
                        "state_generation": self._state["state_generation"] + 1,
                    }
                )
            self._state = candidate
            self._healthy = True
            self._last_error = ""
            result = self._state
            return result

    def synchronize(
        self,
        artifacts: Mapping[str, CachedResponseArtifact],
        repository_state_generation: int,
        changed_statement_ids: tuple[str, ...],
    ) -> SparseIndexState:
        """Avoid posting rebuilds when a mutation changes statistics only."""
        if not isinstance(changed_statement_ids, tuple) or not all(
            isinstance(statement_id, str) and statement_id for statement_id in changed_statement_ids
        ):
            raise InvalidRequestError("changed sparse statement IDs must be a tuple of non-empty strings")
        normalized_ids = tuple(sorted(set(changed_statement_ids)))
        if normalized_ids != changed_statement_ids:
            raise InvalidRequestError("changed sparse statement IDs must be sorted and unique")
        with self._lock:
            live = self._state
            healthy = self._healthy
        if not self.enabled:
            return self.rebuild((), repository_state_generation)
        if not healthy:
            return self.rebuild(artifacts.values(), repository_state_generation)
        content_changed = False
        include_response_text = self._settings["include_response_text"]
        for statement_id in normalized_ids:
            current_document = live["documents"].get(statement_id)
            artifact = artifacts.get(statement_id)
            if artifact is None:
                if current_document is not None:
                    content_changed = True
                continue
            projected = _sparse_document_from_validated_artifact(artifact, include_response_text)
            if current_document != projected:
                content_changed = True
        if content_changed:
            candidate = update_sparse_index_state(
                live,
                artifacts,
                normalized_ids,
                repository_state_generation=repository_state_generation,
                settings=self._settings,
            )
            with self._lock:
                state_unchanged = self._state["state_generation"] == live["state_generation"]
                if state_unchanged:
                    self._state = candidate
                    self._healthy = True
                    self._last_error = ""
                    return self._state
            return self.rebuild(artifacts.values(), repository_state_generation)
        with self._lock:
            if repository_state_generation != self._state["repository_state_generation"]:
                self._state = MappingProxyType(
                    SparseIndexState(
                        schema_version=self._state["schema_version"],
                        index_version=self._state["index_version"],
                        tokenizer_version=self._state["tokenizer_version"],
                        state_generation=self._state["state_generation"] + 1,
                        repository_state_generation=repository_state_generation,
                        config_fingerprint=self._state["config_fingerprint"],
                        documents=self._state["documents"],
                        postings=self._state["postings"],
                        document_frequencies=self._state["document_frequencies"],
                        average_field_lengths=self._state["average_field_lengths"],
                        fingerprint=self._state["fingerprint"],
                    )
                )
            self._healthy = True
            self._last_error = ""
            result = self._state
            return result

    def mark_unavailable(self, error: object) -> None:
        with self._lock:
            self._healthy = False
            self._last_error = type(error).__name__

    def check_against(
        self,
        artifacts: Iterable[CachedResponseArtifact],
        repository_state_generation: int,
    ) -> SparseIndexCheckReport:
        with self._lock:
            live = self._state
            healthy = self._healthy
        expected = build_sparse_index_state(
            artifacts if self.enabled else (),
            repository_state_generation=repository_state_generation,
            settings=self._settings,
            state_generation=live["state_generation"],
            trusted_artifacts=True,
        )
        issues = []
        actual_fingerprint = _documents_fingerprint(live["documents"], live["config_fingerprint"])
        actual_structure = _index_structure_fingerprint(live["postings"], live["average_field_lengths"], live["config_fingerprint"])
        expected_structure = _index_structure_fingerprint(
            expected["postings"], expected["average_field_lengths"], expected["config_fingerprint"]
        )
        if not healthy:
            issues.append("index_unavailable")
        if live["config_fingerprint"] != expected["config_fingerprint"]:
            issues.append("configuration_mismatch")
        if live["fingerprint"] != actual_fingerprint:
            issues.append("self_fingerprint_mismatch")
        if actual_fingerprint != expected["fingerprint"]:
            issues.append("authoritative_content_mismatch")
        if actual_structure != expected_structure:
            issues.append("posting_content_mismatch")
        normalized = tuple(sorted(set(issues)))
        result = SparseIndexCheckReport(
            consistent=not normalized,
            repository_state_generation=repository_state_generation,
            document_count=len(live["documents"]),
            issues=normalized,
        )
        return result

    def search(
        self,
        text: str,
        scope: ScopeKey,
        *,
        limit: int,
        max_working_memory_bytes: int,
    ) -> SparseSearchResult:
        with self._lock:
            state = self._state
            available = self.available
        if not available:
            return SparseSearchResult(
                matches=(),
                complete=False,
                reason="sparse_unavailable",
                query_term_count=0,
                posting_visits=0,
                working_memory_bytes=0,
            )
        result = search_sparse_index(
            state,
            text,
            scope,
            limit=limit,
            max_query_terms=self._settings["max_query_terms"],
            max_posting_visits=self._settings["max_posting_visits"],
            max_prefix_expansions=self._settings["max_prefix_expansions"],
            max_working_memory_bytes=max_working_memory_bytes,
        )
        return result
