"""Versioned, transport-neutral query identity and retrieval contracts.

This module deliberately depends only on the standard library plus Engram's
literal constants and error types. Identity construction therefore performs no
graph access, model loading, resource download, or transport work.
"""

from json import JSONDecodeError as json_JSONDecodeError, dumps as json_dumps, loads as json_loads
from re import Match as re_Match, fullmatch as re_fullmatch, search as re_search
from unicodedata import category as unicodedata_category, normalize as unicodedata_normalize

from engram.constants import (
    DEFAULT_CONTRACTIONS,
    DEFAULT_STOPWORDS,
    EMPTY_RELATION_REFERENCE,
    EMPTY_SCOPE_KEY,
    ENTITY_REFERENCE_FIELDS,
    IDENTITY_ALLOWED_RAW_WHITESPACE,
    IDENTITY_AUXILIARIES,
    IDENTITY_CANONICAL_ID_RE,
    IDENTITY_COMPARISON_OPERATOR_RE,
    IDENTITY_COMPARISON_PHRASES,
    IDENTITY_CONTRACTION_RE,
    IDENTITY_CURRENT_TERMS,
    IDENTITY_DIRECT_LOOKUP_LEADS,
    IDENTITY_ENTITY_EXCLUDED_WORDS,
    IDENTITY_HISTORICAL_TERMS,
    IDENTITY_LOCATION_TERMS,
    IDENTITY_NEGATION_TERMS,
    IDENTITY_OPERATOR_TOKENS,
    IDENTITY_PUNCTUATION_TRANSLATION,
    IDENTITY_QUALIFIER_FIELDS,
    IDENTITY_QUOTED_SPAN_RE,
    IDENTITY_RELATION_HINTS,
    IDENTITY_SCHEMA_VERSION,
    IDENTITY_TECHNICAL_PATTERNS,
    IDENTITY_TECHNICAL_PUNCTUATION,
    IDENTITY_TITLE_SEQUENCE_RE,
    MAX_CANONICAL_FORM_BYTES,
    MAX_CANONICAL_ID_BYTES,
    MAX_CONTEXT_FINGERPRINT_BYTES,
    MAX_ENTITIES,
    MAX_IDENTITY_JSON_BYTES,
    MAX_IDENTITY_SURFACE_BYTES,
    MAX_LEXICAL_TERM_BYTES,
    MAX_LEXICAL_TERMS,
    MAX_NAMESPACE_BYTES,
    MAX_QUALIFIER_VALUE_BYTES,
    MAX_QUALIFIERS,
    MAX_RETRIEVAL_ALIASES,
    MAX_RETRIEVAL_REPRESENTATION_BYTES,
    QUERY_IDENTITY_FIELDS,
    RELATION_REFERENCE_FIELDS,
    RETRIEVAL_KEY_BINDING_FIELDS,
    RETRIEVAL_NORMALIZATION_VERSION,
    RETRIEVAL_REPRESENTATION_FIELDS,
    RETRIEVAL_REPRESENTATION_SCHEMA_VERSION,
    SCOPE_KEY_FIELDS,
    SCOPE_SCHEMA_VERSION,
    SCOPED_RETRIEVAL_KEY_FIELDS,
    SCOPED_RETRIEVAL_KEY_SCHEMA_VERSION,
    QualifierKind,
    QueryOperator,
    RetrievalOrigin,
)
from engram.errors import IdentityValidationError, UnsupportedIdentityVersionError
from engram.lexical import select_lexical_terms
from engram.temporal import parse_temporal_query


def byte_length(value: str) -> int:
    result = len(value) if value.isascii() else len(value.encode("utf-8"))
    return result


def require_text(value: object, name: str, maximum_bytes: int, *, allow_empty: bool) -> str:
    if not isinstance(value, str):
        raise IdentityValidationError(f"{name} must be a string")
    if not allow_empty and not value:
        raise IdentityValidationError(f"{name} must be a non-empty string")
    if byte_length(value) > maximum_bytes:
        raise IdentityValidationError(f"{name} exceeds {maximum_bytes} UTF-8 bytes")
    if value.isascii():
        if value and not value.isprintable():
            raise IdentityValidationError(f"{name} contains a control or surrogate character")
    elif any(unicodedata_category(character) in {"Cc", "Cs"} for character in value):
        raise IdentityValidationError(f"{name} contains a control or surrogate character")
    return value


def require_raw_text(value: object, name: str, maximum_bytes: int, *, allow_empty: bool) -> str:
    if not isinstance(value, str):
        raise IdentityValidationError(f"{name} must be a string")
    if not allow_empty and not value.strip():
        raise IdentityValidationError(f"{name} must contain non-whitespace text")
    if byte_length(value) > maximum_bytes:
        raise IdentityValidationError(f"{name} exceeds {maximum_bytes} UTF-8 bytes")
    if value.isascii() and (not value or value.isprintable()):
        return value
    for character in value:
        category = unicodedata_category(character)
        if category in {"Cc", "Cs"} and character not in IDENTITY_ALLOWED_RAW_WHITESPACE:
            raise IdentityValidationError(f"{name} contains a control or surrogate character")
    return value


def require_version(value: object, expected: int, name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise IdentityValidationError(f"{name} must be an integer")
    if value != expected:
        raise UnsupportedIdentityVersionError(f"unsupported {name}: {value}; expected {expected}")
    return value


def require_mapping(value: object, name: str) -> dict:
    if not isinstance(value, dict):
        raise IdentityValidationError(f"{name} must be an object")
    return value


def require_list(value: object, name: str) -> list:
    if not isinstance(value, list):
        raise IdentityValidationError(f"{name} must be an array")
    result = list(value)
    return result


def require_exact_keys(data: dict, expected: set[str], name: str) -> None:
    actual = set(data)
    missing = expected - actual
    extra = actual - expected
    if missing:
        raise IdentityValidationError(f"{name} is missing fields: {', '.join(sorted(missing))}")
    if extra:
        raise IdentityValidationError(f"{name} has unsupported fields: {', '.join(sorted(str(key) for key in extra))}")


def load_json_object(value: str, name: str) -> dict:
    require_raw_text(value, name, MAX_IDENTITY_JSON_BYTES, allow_empty=False)
    try:
        decoded = json_loads(value)
    except json_JSONDecodeError as error:
        raise IdentityValidationError(f"{name} is not valid JSON: {error.msg}") from error
    result = require_mapping(decoded, name)
    return result


def dump_json(data: dict) -> str:
    result = json_dumps(data, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":"))
    return result


def expand_contraction(match: re_Match) -> str:
    result = DEFAULT_CONTRACTIONS[match.group(0)]
    return result


def clean_normalized_token(token: str) -> str:
    cleaned = token.strip("'")
    cleaned = cleaned.rstrip(".")
    if cleaned.endswith(":") and not re_fullmatch(r"[a-z]:", cleaned):
        cleaned = cleaned.rstrip(":")
    return cleaned


def normalize_retrieval_key(text: str, normalization_version: int = RETRIEVAL_NORMALIZATION_VERSION) -> str:
    """Normalize one identity or retrieval representation with version 1 rules."""
    require_version(normalization_version, RETRIEVAL_NORMALIZATION_VERSION, "normalization_version")
    raw = require_raw_text(text, "retrieval text", MAX_RETRIEVAL_REPRESENTATION_BYTES, allow_empty=True)
    normalized = unicodedata_normalize("NFKC", raw).translate(IDENTITY_PUNCTUATION_TRANSLATION).casefold()
    normalized = IDENTITY_CONTRACTION_RE.sub(expand_contraction, normalized)
    characters = []
    for index, character in enumerate(normalized):
        is_not_equal = character == "!" and index + 1 < len(normalized) and normalized[index + 1] == "="
        if character.isalnum() or character.isspace() or character in IDENTITY_TECHNICAL_PUNCTUATION or is_not_equal:
            characters.append(character)
        else:
            characters.append(" ")
    collapsed = " ".join("".join(characters).split())
    tokens = [clean_normalized_token(token) for token in collapsed.split()]
    result = " ".join(token for token in tokens if token)
    return result


def validate_canonical_id(value: object, name: str) -> str:
    canonical_id = require_text(value, name, MAX_CANONICAL_ID_BYTES, allow_empty=True)
    if canonical_id and not IDENTITY_CANONICAL_ID_RE.fullmatch(canonical_id):
        raise IdentityValidationError(f"{name} must use a URI-like scheme and contain no whitespace")
    return canonical_id


def scope_key(
    namespace: object = "",
    context_fingerprint: object = "",
    schema_version: object = SCOPE_SCHEMA_VERSION,
) -> dict:
    """Build one versioned exact eligibility boundary."""

    result: dict = {
        "schema_version": require_version(schema_version, SCOPE_SCHEMA_VERSION, "scope schema_version"),
        "namespace": require_text(namespace, "scope namespace", MAX_NAMESPACE_BYTES, allow_empty=True),
        "context_fingerprint": require_text(
            context_fingerprint,
            "scope context_fingerprint",
            MAX_CONTEXT_FINGERPRINT_BYTES,
            allow_empty=True,
        ),
    }
    return result


def validate_scope_key(value: object) -> dict:
    """Validate and copy one scope-key dictionary."""

    data = require_mapping(value, "ScopeKey")
    require_exact_keys(data, SCOPE_KEY_FIELDS, "ScopeKey")
    result = scope_key(
        namespace=data.get("namespace", ()),
        context_fingerprint=data.get("context_fingerprint", ()),
        schema_version=data.get("schema_version", ()),
    )
    return result


def scope_key_signature(value: object) -> tuple:
    """Return the hashable exact signature for one validated scope."""

    scope = validate_scope_key(value)
    result = (scope["schema_version"], scope["namespace"], scope["context_fingerprint"])
    return result


def scope_key_to_dict(value: object) -> dict:
    """Serialize one scope key."""

    scope = validate_scope_key(value)
    result = dict(scope)
    return result


def scope_key_to_json(value: object) -> str:
    """Serialize one scope key to canonical JSON."""

    serialized = scope_key_to_dict(value)
    result = dump_json(serialized)
    return result


def scope_key_from_dict(value: object) -> dict:
    """Decode one scope key from its exact dictionary form."""

    result = validate_scope_key(value)
    return result


def scope_key_from_json(value: str) -> dict:
    """Decode one scope key from canonical JSON."""

    decoded = load_json_object(value, "ScopeKey JSON")
    result = scope_key_from_dict(decoded)
    return result


def entity_reference(surface: object, canonical_id: object = "") -> dict:
    """Build one validated entity-reference dictionary."""

    validated_surface = require_text(surface, "entity surface", MAX_IDENTITY_SURFACE_BYTES, allow_empty=False)
    validated_canonical_id = validate_canonical_id(canonical_id, "entity canonical_id")
    result: dict = {
        "surface": validated_surface,
        "canonical_id": validated_canonical_id,
    }
    return result


def validate_entity_reference(value: object) -> dict:
    """Validate and copy one entity-reference dictionary."""

    data = require_mapping(value, "EntityReference")
    require_exact_keys(data, ENTITY_REFERENCE_FIELDS, "EntityReference")
    result = entity_reference(data.get("surface", ()), data.get("canonical_id", ()))
    return result


def entity_reference_to_dict(value: object) -> dict:
    """Serialize one validated entity reference."""

    validated = validate_entity_reference(value)
    result: dict = {
        "surface": validated["surface"],
        "canonical_id": validated["canonical_id"],
    }
    return result


def entity_reference_from_dict(value: object) -> dict:
    """Decode one entity reference from its exact serialized form."""

    result = validate_entity_reference(value)
    return result


def entity_reference_key(value: object) -> tuple[str, str]:
    validated = validate_entity_reference(value)
    result = (validated["surface"], validated["canonical_id"])
    return result


def relation_reference(surface: object = "", canonical_id: object = "") -> dict:
    """Build one validated relation-reference dictionary."""

    validated_surface = require_text(surface, "relation surface", MAX_IDENTITY_SURFACE_BYTES, allow_empty=True)
    validated_canonical_id = validate_canonical_id(canonical_id, "relation canonical_id")
    if validated_canonical_id and not validated_surface:
        raise IdentityValidationError("relation surface is required when relation canonical_id is present")
    result: dict = {
        "surface": validated_surface,
        "canonical_id": validated_canonical_id,
    }
    return result


def validate_relation_reference(value: object) -> dict:
    """Validate and copy one relation-reference dictionary."""

    data = require_mapping(value, "RelationReference")
    require_exact_keys(data, RELATION_REFERENCE_FIELDS, "RelationReference")
    result = relation_reference(data.get("surface", ()), data.get("canonical_id", ()))
    return result


def relation_reference_to_dict(value: object) -> dict:
    """Serialize one validated relation reference."""

    validated = validate_relation_reference(value)
    result: dict = {
        "surface": validated["surface"],
        "canonical_id": validated["canonical_id"],
    }
    return result


def relation_reference_from_dict(value: object) -> dict:
    """Decode one relation reference from its exact serialized form."""

    result = validate_relation_reference(value)
    return result


def identity_qualifier(kind: QualifierKind, value: object) -> dict:
    """Build one validated identity-qualifier dictionary."""

    if not isinstance(kind, QualifierKind):
        raise IdentityValidationError("qualifier kind must be a QualifierKind")
    normalized = require_text(value, "qualifier value", MAX_QUALIFIER_VALUE_BYTES, allow_empty=False)
    if normalize_retrieval_key(normalized) != normalized:
        raise IdentityValidationError("qualifier value must already use retrieval normalization version 1")
    result: dict = {
        "kind": kind,
        "value": normalized,
    }
    return result


def validate_identity_qualifier(value: object) -> dict:
    """Validate and copy one identity-qualifier dictionary."""

    data = require_mapping(value, "IdentityQualifier")
    require_exact_keys(data, IDENTITY_QUALIFIER_FIELDS, "IdentityQualifier")
    kind = data.get("kind", ())
    if not isinstance(kind, QualifierKind):
        raise IdentityValidationError("qualifier kind must be a QualifierKind")
    result = identity_qualifier(kind, data.get("value", ()))
    return result


def identity_qualifier_to_dict(value: object) -> dict:
    """Serialize one validated identity qualifier."""

    validated = validate_identity_qualifier(value)
    result: dict = {
        "kind": validated["kind"].value,
        "value": validated["value"],
    }
    return result


def identity_qualifier_from_dict(value: object) -> dict:
    """Decode one identity qualifier from its exact serialized form."""

    data = require_mapping(value, "IdentityQualifier")
    require_exact_keys(data, IDENTITY_QUALIFIER_FIELDS, "IdentityQualifier")
    kind_value = require_text(data.get("kind", ()), "qualifier kind", 32, allow_empty=False)
    try:
        kind = QualifierKind(kind_value)
    except ValueError as error:
        raise IdentityValidationError(f"unsupported qualifier kind: {kind_value}") from error
    result = identity_qualifier(kind, data.get("value", ()))
    return result


def identity_qualifier_key(value: object) -> tuple[QualifierKind, str]:
    validated = validate_identity_qualifier(value)
    result = (validated["kind"], validated["value"])
    return result


def scoped_retrieval_key(
    scope: object,
    normalized_key: object,
    normalization_version: object = RETRIEVAL_NORMALIZATION_VERSION,
    schema_version: object = SCOPED_RETRIEVAL_KEY_SCHEMA_VERSION,
) -> dict:
    """Build one validated exact-index retrieval key."""

    try:
        validated_scope = validate_scope_key(scope)
    except IdentityValidationError as error:
        raise IdentityValidationError("scoped retrieval key scope must be a ScopeKey") from error
    validated_schema_version = require_version(
        schema_version,
        SCOPED_RETRIEVAL_KEY_SCHEMA_VERSION,
        "scoped retrieval key schema_version",
    )
    validated_normalization_version = require_version(
        normalization_version,
        RETRIEVAL_NORMALIZATION_VERSION,
        "normalization_version",
    )
    validated_normalized_key = require_text(
        normalized_key,
        "normalized retrieval key",
        MAX_RETRIEVAL_REPRESENTATION_BYTES,
        allow_empty=False,
    )
    if normalize_retrieval_key(validated_normalized_key, validated_normalization_version) != validated_normalized_key:
        raise IdentityValidationError("normalized retrieval key does not match its normalization version")
    result: dict = {
        "schema_version": validated_schema_version,
        "normalization_version": validated_normalization_version,
        "scope": validated_scope,
        "normalized_key": validated_normalized_key,
    }
    return result


def build_scoped_retrieval_key(
    scope: object,
    representation: str,
    normalization_version: int = RETRIEVAL_NORMALIZATION_VERSION,
) -> dict:
    """Normalize one representation and build its exact-index key."""

    normalized = normalize_retrieval_key(representation, normalization_version)
    if not normalized:
        raise IdentityValidationError("retrieval representation normalizes to an empty key")
    result = scoped_retrieval_key(
        scope=scope,
        normalized_key=normalized,
        normalization_version=normalization_version,
    )
    return result


def validate_scoped_retrieval_key(value: object) -> dict:
    """Validate and copy one exact-index retrieval key."""

    data = require_mapping(value, "ScopedRetrievalKey")
    require_exact_keys(data, SCOPED_RETRIEVAL_KEY_FIELDS, "ScopedRetrievalKey")
    result = scoped_retrieval_key(
        schema_version=data.get("schema_version", ()),
        normalization_version=data.get("normalization_version", ()),
        scope=data.get("scope", ()),
        normalized_key=data.get("normalized_key", ()),
    )
    return result


def scoped_retrieval_key_signature(value: object) -> tuple:
    """Return the hashable exact signature for one validated retrieval key."""

    key = validate_scoped_retrieval_key(value)
    result = trusted_scoped_retrieval_key_signature(key)
    return result


def trusted_scoped_retrieval_key_signature(value: dict) -> tuple:
    """Return a signature for a key already validated inside a locked boundary."""

    result = (
        value.get("schema_version", 0),
        value.get("normalization_version", 0),
        (
            value.get("scope", {})["schema_version"],
            value.get("scope", {})["namespace"],
            value.get("scope", {})["context_fingerprint"],
        ),
        value.get("normalized_key", ""),
    )
    return result


def scoped_retrieval_key_to_dict(value: object) -> dict:
    """Serialize one exact-index retrieval key."""

    key = validate_scoped_retrieval_key(value)
    result = {
        "schema_version": key["schema_version"],
        "normalization_version": key["normalization_version"],
        "scope": scope_key_to_dict(key["scope"]),
        "normalized_key": key["normalized_key"],
    }
    return result


def scoped_retrieval_key_to_json(value: object) -> str:
    """Serialize one exact-index retrieval key to canonical JSON."""

    serialized = scoped_retrieval_key_to_dict(value)
    result = dump_json(serialized)
    return result


def scoped_retrieval_key_from_dict(value: object) -> dict:
    """Decode one exact-index retrieval key from its dictionary form."""

    data = require_mapping(value, "ScopedRetrievalKey")
    require_exact_keys(data, SCOPED_RETRIEVAL_KEY_FIELDS, "ScopedRetrievalKey")
    result = scoped_retrieval_key(
        schema_version=data.get("schema_version", ()),
        normalization_version=data.get("normalization_version", ()),
        scope=scope_key_from_dict(require_mapping(data.get("scope", ()), "scoped retrieval key scope")),
        normalized_key=data.get("normalized_key", ()),
    )
    return result


def scoped_retrieval_key_from_json(value: str) -> dict:
    """Decode one exact-index retrieval key from canonical JSON."""

    decoded = load_json_object(value, "ScopedRetrievalKey JSON")
    result = scoped_retrieval_key_from_dict(decoded)
    return result


def retrieval_key_binding(
    key: object,
    origin: object,
    representation: object,
) -> dict:
    """Build one validated scoped retrieval-key binding dictionary."""

    try:
        validated_key = validate_scoped_retrieval_key(key)
    except IdentityValidationError as error:
        raise IdentityValidationError("retrieval binding key must be a ScopedRetrievalKey") from error
    if not isinstance(origin, RetrievalOrigin):
        raise IdentityValidationError("retrieval binding origin must be a RetrievalOrigin")
    validated_representation = require_raw_text(
        representation,
        "retrieval binding representation",
        MAX_RETRIEVAL_REPRESENTATION_BYTES,
        allow_empty=False,
    )
    if normalize_retrieval_key(validated_representation, validated_key["normalization_version"]) != validated_key["normalized_key"]:
        raise IdentityValidationError("retrieval binding representation does not produce its scoped key")
    result: dict = {
        "key": validated_key,
        "origin": origin,
        "representation": validated_representation,
    }
    return result


def validate_retrieval_key_binding(value: object) -> dict:
    """Validate and copy one scoped retrieval-key binding dictionary."""

    data = require_mapping(value, "RetrievalKeyBinding")
    require_exact_keys(data, RETRIEVAL_KEY_BINDING_FIELDS, "RetrievalKeyBinding")
    try:
        result = retrieval_key_binding(data.get("key", ()), data.get("origin", ()), data.get("representation", ()))
    except IdentityValidationError as error:
        raise IdentityValidationError("retrieval binding fields are malformed") from error
    return result


def retrieval_representation(
    canonical: object,
    aliases: object = (),
    normalization_version: object = RETRIEVAL_NORMALIZATION_VERSION,
    schema_version: object = RETRIEVAL_REPRESENTATION_SCHEMA_VERSION,
) -> dict:
    """Build one validated canonical retrieval representation dictionary."""

    validated_schema_version = require_version(
        schema_version,
        RETRIEVAL_REPRESENTATION_SCHEMA_VERSION,
        "retrieval representation schema_version",
    )
    validated_normalization_version = require_version(
        normalization_version,
        RETRIEVAL_NORMALIZATION_VERSION,
        "normalization_version",
    )
    validated_canonical = require_raw_text(
        canonical,
        "retrieval canonical representation",
        MAX_RETRIEVAL_REPRESENTATION_BYTES,
        allow_empty=False,
    )
    if not isinstance(aliases, tuple):
        raise IdentityValidationError("retrieval aliases must be a tuple")
    if len(aliases) > MAX_RETRIEVAL_ALIASES:
        raise IdentityValidationError(f"retrieval aliases exceed the limit of {MAX_RETRIEVAL_ALIASES}")

    canonical_key = normalize_retrieval_key(validated_canonical, validated_normalization_version)
    if not canonical_key:
        raise IdentityValidationError("retrieval canonical representation normalizes to an empty key")
    seen = {canonical_key}
    validated_aliases = []
    for index, alias in enumerate(aliases):
        validated_alias = require_raw_text(
            alias,
            f"retrieval alias {index}",
            MAX_RETRIEVAL_REPRESENTATION_BYTES,
            allow_empty=False,
        )
        normalized = normalize_retrieval_key(validated_alias, validated_normalization_version)
        if not normalized:
            raise IdentityValidationError(f"retrieval alias {index} normalizes to an empty key")
        if normalized in seen:
            continue
        seen.add(normalized)
        validated_aliases.append(validated_alias)
    result: dict = {
        "canonical": validated_canonical,
        "aliases": tuple(validated_aliases),
        "normalization_version": validated_normalization_version,
        "schema_version": validated_schema_version,
    }
    return result


def validate_retrieval_representation(value: object) -> dict:
    """Validate and copy one retrieval-representation dictionary."""

    data = require_mapping(value, "RetrievalRepresentation")
    require_exact_keys(data, RETRIEVAL_REPRESENTATION_FIELDS, "RetrievalRepresentation")
    result = retrieval_representation(
        canonical=data.get("canonical", ()),
        aliases=data.get("aliases", ()),
        normalization_version=data.get("normalization_version", ()),
        schema_version=data.get("schema_version", ()),
    )
    return result


def retrieval_representation_to_dict(value: object) -> dict:
    """Serialize one validated retrieval representation."""

    validated = validate_retrieval_representation(value)
    result = {
        "schema_version": validated["schema_version"],
        "normalization_version": validated["normalization_version"],
        "canonical": validated["canonical"],
        "aliases": list(validated["aliases"]),
    }
    return result


def retrieval_representation_to_json(value: object) -> str:
    """Serialize one validated retrieval representation to canonical JSON."""

    serialized = retrieval_representation_to_dict(value)
    result = dump_json(serialized)
    return result


def retrieval_representation_bindings(
    value: object,
    scope: dict,
) -> tuple[dict, ...]:
    """Build scoped canonical and alias bindings for one representation."""

    validated_scope = validate_scope_key(scope)
    validated = validate_retrieval_representation(value)
    canonical = validated["canonical"]
    normalization_version = validated["normalization_version"]
    canonical_binding = retrieval_key_binding(
        key=build_scoped_retrieval_key(validated_scope, canonical, normalization_version),
        origin=RetrievalOrigin.CANONICAL,
        representation=canonical,
    )
    alias_bindings = tuple(
        retrieval_key_binding(
            key=build_scoped_retrieval_key(validated_scope, alias, normalization_version),
            origin=RetrievalOrigin.ALIAS,
            representation=alias,
        )
        for alias in validated["aliases"]
    )
    result = (canonical_binding, *alias_bindings)
    return result


def retrieval_representation_from_dict(value: object) -> dict:
    """Decode one retrieval representation from its exact serialized form."""

    data = require_mapping(value, "RetrievalRepresentation")
    require_exact_keys(data, RETRIEVAL_REPRESENTATION_FIELDS, "RetrievalRepresentation")
    aliases = require_list(data.get("aliases", ()), "retrieval aliases")
    validated_aliases = []
    for alias in aliases:
        if not isinstance(alias, str):
            raise IdentityValidationError("every retrieval alias must be a string")
        validated_aliases.append(alias)
    result = retrieval_representation(
        schema_version=data.get("schema_version", ()),
        normalization_version=data.get("normalization_version", ()),
        canonical=data.get("canonical", ()),
        aliases=tuple(validated_aliases),
    )
    return result


def retrieval_representation_from_json(value: str) -> dict:
    """Decode one retrieval representation from canonical JSON."""

    decoded = load_json_object(value, "RetrievalRepresentation JSON")
    result = retrieval_representation_from_dict(decoded)
    return result


def query_identity(
    canonical_form: object,
    operator: QueryOperator = QueryOperator.UNKNOWN,
    entities: object = (),
    relation: object = EMPTY_RELATION_REFERENCE,
    qualifiers: object = (),
    lexical_terms: object = (),
    scope: object = EMPTY_SCOPE_KEY,
    normalization_version: object = RETRIEVAL_NORMALIZATION_VERSION,
    schema_version: object = IDENTITY_SCHEMA_VERSION,
) -> dict:
    """Build one validated semantic query-identity dictionary."""

    validated_schema_version = require_version(schema_version, IDENTITY_SCHEMA_VERSION, "identity schema_version")
    validated_normalization_version = require_version(
        normalization_version,
        RETRIEVAL_NORMALIZATION_VERSION,
        "normalization_version",
    )
    validated_canonical_form = require_text(
        canonical_form,
        "identity canonical_form",
        MAX_CANONICAL_FORM_BYTES,
        allow_empty=False,
    )
    if normalize_retrieval_key(validated_canonical_form, validated_normalization_version) != validated_canonical_form:
        raise IdentityValidationError("identity canonical_form must already match its normalization version")
    if not isinstance(operator, QueryOperator):
        raise IdentityValidationError("identity operator must be a QueryOperator")
    if not isinstance(entities, tuple):
        raise IdentityValidationError("identity entities must be a tuple of EntityReference values")
    if len(entities) > MAX_ENTITIES:
        raise IdentityValidationError(f"identity entities exceed the limit of {MAX_ENTITIES}")
    try:
        validated_entities = tuple(validate_entity_reference(entity) for entity in entities)
    except IdentityValidationError as error:
        raise IdentityValidationError("identity entities must be a tuple of EntityReference values") from error
    entity_keys = tuple(entity_reference_key(entity) for entity in validated_entities)
    if len(set(entity_keys)) != len(entity_keys):
        raise IdentityValidationError("identity entities must be unique")
    try:
        validated_relation = validate_relation_reference(relation)
    except IdentityValidationError as error:
        raise IdentityValidationError("identity relation must be a RelationReference") from error
    if not isinstance(qualifiers, tuple):
        raise IdentityValidationError("identity qualifiers must be a tuple of IdentityQualifier values")
    if len(qualifiers) > MAX_QUALIFIERS:
        raise IdentityValidationError(f"identity qualifiers exceed the limit of {MAX_QUALIFIERS}")
    try:
        validated_qualifiers = tuple(validate_identity_qualifier(qualifier) for qualifier in qualifiers)
    except IdentityValidationError as error:
        raise IdentityValidationError("identity qualifiers must be a tuple of IdentityQualifier values") from error
    qualifier_keys = tuple(identity_qualifier_key(qualifier) for qualifier in validated_qualifiers)
    if len(set(qualifier_keys)) != len(qualifier_keys):
        raise IdentityValidationError("identity qualifiers must be unique")
    if not isinstance(lexical_terms, tuple):
        raise IdentityValidationError("identity lexical_terms must be a tuple")
    if len(lexical_terms) > MAX_LEXICAL_TERMS:
        raise IdentityValidationError(f"identity lexical_terms exceed the limit of {MAX_LEXICAL_TERMS}")
    validated_lexical_terms = []
    for index, term in enumerate(lexical_terms):
        validated_term = require_text(
            term,
            f"identity lexical term {index}",
            MAX_LEXICAL_TERM_BYTES,
            allow_empty=False,
        )
        if normalize_retrieval_key(validated_term, validated_normalization_version) != validated_term:
            raise IdentityValidationError(f"identity lexical term {index} must already be normalized")
        if any(character.isspace() for character in validated_term):
            raise IdentityValidationError(f"identity lexical term {index} must be one token")
        validated_lexical_terms.append(validated_term)
    if len(set(validated_lexical_terms)) != len(validated_lexical_terms):
        raise IdentityValidationError("identity lexical_terms must be unique")
    try:
        validated_scope = validate_scope_key(scope)
    except IdentityValidationError as error:
        raise IdentityValidationError("identity scope must be a ScopeKey") from error
    result: dict = {
        "canonical_form": validated_canonical_form,
        "operator": operator,
        "entities": validated_entities,
        "relation": validated_relation,
        "qualifiers": validated_qualifiers,
        "lexical_terms": tuple(validated_lexical_terms),
        "scope": validated_scope,
        "normalization_version": validated_normalization_version,
        "schema_version": validated_schema_version,
    }
    return result


def validate_query_identity(value: object) -> dict:
    """Validate and copy one semantic query-identity dictionary."""

    data = require_mapping(value, "QueryIdentity")
    require_exact_keys(data, QUERY_IDENTITY_FIELDS, "QueryIdentity")
    operator = data.get("operator", ())
    scope = data.get("scope", ())
    if not isinstance(operator, QueryOperator):
        raise IdentityValidationError("QueryIdentity fields are malformed")
    result = query_identity(
        canonical_form=data.get("canonical_form", ()),
        operator=operator,
        entities=data.get("entities", ()),
        relation=data.get("relation", ()),
        qualifiers=data.get("qualifiers", ()),
        lexical_terms=data.get("lexical_terms", ()),
        scope=scope,
        normalization_version=data.get("normalization_version", ()),
        schema_version=data.get("schema_version", ()),
    )
    return result


def query_identity_to_dict(value: object) -> dict:
    """Serialize one validated query identity."""

    validated = validate_query_identity(value)
    result = {
        "schema_version": validated["schema_version"],
        "normalization_version": validated["normalization_version"],
        "canonical_form": validated["canonical_form"],
        "operator": validated["operator"].value,
        "entities": [entity_reference_to_dict(entity) for entity in validated["entities"]],
        "relation": relation_reference_to_dict(validated["relation"]),
        "qualifiers": [identity_qualifier_to_dict(qualifier) for qualifier in validated["qualifiers"]],
        "lexical_terms": list(validated["lexical_terms"]),
        "scope": scope_key_to_dict(validated["scope"]),
    }
    return result


def query_identity_to_json(value: object) -> str:
    """Serialize one validated query identity to canonical JSON."""

    serialized = query_identity_to_dict(value)
    result = dump_json(serialized)
    return result


def query_identity_from_dict(value: object) -> dict:
    """Decode one query identity from its exact serialized form."""

    data = require_mapping(value, "QueryIdentity")
    require_exact_keys(data, QUERY_IDENTITY_FIELDS, "QueryIdentity")
    operator_value = require_text(data.get("operator", ()), "identity operator", 32, allow_empty=False)
    try:
        operator = QueryOperator(operator_value)
    except ValueError as error:
        raise IdentityValidationError(f"unsupported identity operator: {operator_value}") from error
    entities = require_list(data.get("entities", ()), "identity entities")
    qualifiers = require_list(data.get("qualifiers", ()), "identity qualifiers")
    lexical_terms = require_list(data.get("lexical_terms", ()), "identity lexical_terms")
    validated_lexical_terms = []
    for term in lexical_terms:
        if not isinstance(term, str):
            raise IdentityValidationError("every identity lexical term must be a string")
        validated_lexical_terms.append(term)
    result = query_identity(
        schema_version=data.get("schema_version", ()),
        normalization_version=data.get("normalization_version", ()),
        canonical_form=data.get("canonical_form", ()),
        operator=operator,
        entities=tuple(entity_reference_from_dict(require_mapping(entity, "identity entity")) for entity in entities),
        relation=relation_reference_from_dict(require_mapping(data.get("relation", ()), "identity relation")),
        qualifiers=tuple(
            identity_qualifier_from_dict(require_mapping(qualifier, "identity qualifier")) for qualifier in qualifiers
        ),
        lexical_terms=tuple(validated_lexical_terms),
        scope=scope_key_from_dict(require_mapping(data.get("scope", ()), "identity scope")),
    )
    return result


def query_identity_from_json(value: str) -> dict:
    """Decode one query identity from canonical JSON."""

    decoded = load_json_object(value, "QueryIdentity JSON")
    result = query_identity_from_dict(decoded)
    return result


def extract_operator(request: str) -> QueryOperator:
    """Classify a request through a conservative, closed operator vocabulary."""
    normalized = normalize_retrieval_key(request)
    if not normalized:
        result = QueryOperator.UNKNOWN
        return result
    if normalized.startswith("how many ") or normalized == "how many":
        result = QueryOperator.HOW_MANY
        return result
    first = normalized.split()[0]
    direct = {
        "who": QueryOperator.WHO,
        "what": QueryOperator.WHAT,
        "where": QueryOperator.WHERE,
        "when": QueryOperator.WHEN,
        "which": QueryOperator.WHICH,
        "why": QueryOperator.WHY,
        "how": QueryOperator.HOW,
    }
    if first in direct:
        result = direct.get(first, QueryOperator.UNKNOWN)
        return result
    if first == "count" or normalized.startswith("number of "):
        result = QueryOperator.COUNT
        return result
    if first == "compare" or " versus " in f" {normalized} " or " vs " in f" {normalized} ":
        result = QueryOperator.COMPARE
        return result
    if normalized.startswith(("is there ", "are there ")) or normalized.endswith(" exist"):
        result = QueryOperator.EXISTS
        return result
    if normalized.startswith(IDENTITY_DIRECT_LOOKUP_LEADS):
        result = QueryOperator.LOOKUP
        return result
    result = QueryOperator.UNKNOWN
    return result


def extract_qualifiers(request: str, operator: QueryOperator) -> tuple[dict, ...]:
    """Extract explicit identity-bearing qualifiers without lexical filtering."""
    if not isinstance(operator, QueryOperator):
        raise IdentityValidationError("qualifier extraction operator must be a QueryOperator")
    normalized = normalize_retrieval_key(request)
    tokens = normalized.split()
    qualifiers = []

    for token in tokens:
        if token in IDENTITY_NEGATION_TERMS:
            qualifiers.append(identity_qualifier(QualifierKind.NEGATION, token))
            break
    for token in tokens:
        if token in IDENTITY_CURRENT_TERMS:
            qualifiers.append(identity_qualifier(QualifierKind.CURRENT, token))
            break
    for token in tokens:
        if token in IDENTITY_HISTORICAL_TERMS:
            qualifiers.append(identity_qualifier(QualifierKind.HISTORICAL, token))
            break

    temporal_match = re_search(r"\b(?:as of|before|after|since|until|during|in)\s+(?:the\s+)?(?:year\s+)?\d{4}\b", normalized)
    if temporal_match:
        qualifiers.append(identity_qualifier(QualifierKind.TEMPORAL, temporal_match.group(0)))

    comparison_match = IDENTITY_COMPARISON_OPERATOR_RE.search(normalized)
    comparison = comparison_match.group(0) if comparison_match else ""
    if not comparison:
        comparison = next((phrase.strip() for phrase in IDENTITY_COMPARISON_PHRASES if phrase in f" {normalized} "), "")
    if comparison or operator == QueryOperator.COMPARE:
        qualifiers.append(identity_qualifier(QualifierKind.COMPARISON, comparison or operator.value))

    if operator in {QueryOperator.HOW_MANY, QueryOperator.COUNT} or "number of" in normalized or "amount of" in normalized:
        qualifiers.append(identity_qualifier(QualifierKind.QUANTITY, operator.value))

    location = next((token for token in tokens if token in IDENTITY_LOCATION_TERMS), "")
    if operator == QueryOperator.WHERE or location:
        qualifiers.append(identity_qualifier(QualifierKind.LOCATION, location or operator.value))

    unique_qualifiers = []
    seen_qualifiers = set()
    for qualifier in qualifiers:
        qualifier_key = identity_qualifier_key(qualifier)
        if qualifier_key not in seen_qualifiers:
            seen_qualifiers.add(qualifier_key)
            unique_qualifiers.append(qualifier)
    result = tuple(unique_qualifiers)
    return result


def extract_entities_and_identifiers(request: str) -> tuple[dict, ...]:
    """Extract explicit surfaces and technical identifiers without graph access."""
    raw = require_raw_text(request, "identity request", MAX_CANONICAL_FORM_BYTES, allow_empty=False)
    normalized_unicode = unicodedata_normalize("NFKC", raw).translate(IDENTITY_PUNCTUATION_TRANSLATION)
    candidates: list[tuple[int, int, str]] = []
    for pattern in IDENTITY_TECHNICAL_PATTERNS:
        candidates.extend((match.start(), match.end(), match.group(0)) for match in pattern.finditer(normalized_unicode))
    candidates.extend(
        (match.start(1), match.end(1), match.group(1)) for match in IDENTITY_QUOTED_SPAN_RE.finditer(normalized_unicode)
    )
    for match in IDENTITY_TITLE_SEQUENCE_RE.finditer(normalized_unicode):
        surface = match.group(0).strip()
        surface_parts = surface.split()
        while surface_parts and normalize_retrieval_key(surface_parts[0]).rstrip(".") in IDENTITY_ENTITY_EXCLUDED_WORDS:
            surface_parts = surface_parts[1:]
        surface = " ".join(surface_parts)
        words = normalize_retrieval_key(surface).split()
        if words and not all(word in IDENTITY_ENTITY_EXCLUDED_WORDS for word in words):
            surface_start = normalized_unicode.find(surface, match.start(), match.end())
            candidates.append((surface_start, surface_start + len(surface), surface))

    entities = []
    seen = set()
    occupied: list[tuple[int, int]] = []
    for start, end, surface in sorted(candidates, key=lambda item: (item[0], -(item[1] - item[0]), item[2])):
        cleaned = surface.strip(' \t\r\n,;!?()[]{}"“”').rstrip(".")
        if not cleaned:
            continue
        key = normalize_retrieval_key(cleaned)
        if not key or key in seen:
            continue
        if any(start < occupied_end and end > occupied_start for occupied_start, occupied_end in occupied):
            continue
        seen.add(key)
        occupied.append((start, end))
        entities.append(entity_reference(surface=cleaned))
        if len(entities) == MAX_ENTITIES:
            break
    result = tuple(entities)
    return result


def extract_lexical_terms(request: str) -> tuple[str, ...]:
    """Derive bounded retrieval terms separately from semantic identity fields."""
    normalized = normalize_retrieval_key(request)
    stopwords = DEFAULT_STOPWORDS | IDENTITY_OPERATOR_TOKENS | IDENTITY_AUXILIARIES
    terms = select_lexical_terms(
        [token.strip("'") for token in normalized.split()],
        set(stopwords),
        allow_technical=True,
        max_terms=MAX_LEXICAL_TERMS,
    )
    result = tuple(terms)
    return result


def extract_relation_surface(
    request: str,
    operator: QueryOperator,
    entities: tuple[dict, ...],
) -> dict:
    """Extract a main relation only when simple surface evidence supports one."""
    if not isinstance(operator, QueryOperator):
        raise IdentityValidationError("relation extraction operator must be a QueryOperator")
    if not isinstance(entities, tuple):
        raise IdentityValidationError("relation extraction entities must be EntityReference values")
    try:
        for entity in entities:
            validate_entity_reference(entity)
    except IdentityValidationError as error:
        raise IdentityValidationError("relation extraction entities must be EntityReference values") from error
    if operator in {QueryOperator.LOOKUP, QueryOperator.HOW_MANY, QueryOperator.COUNT, QueryOperator.COMPARE, QueryOperator.EXISTS}:
        result = relation_reference()
        return result

    normalized = normalize_retrieval_key(request)
    tokens = normalized.split()
    if tokens and tokens[0] in IDENTITY_OPERATOR_TOKENS:
        tokens = tokens[1:]
    if not tokens:
        result = relation_reference()
        return result

    auxiliary_index = next((index for index, token in enumerate(tokens) if token in IDENTITY_AUXILIARIES), -1)
    if auxiliary_index >= 0:
        tail = [token for token in tokens[auxiliary_index + 1 :] if token not in DEFAULT_STOPWORDS]
        if not tail:
            result = relation_reference()
            return result
        candidate = next(
            (token for token in tail if token in IDENTITY_RELATION_HINTS),
            "",
        )
        if candidate:
            result = relation_reference(surface=candidate)
            return result
        result = relation_reference()
        return result

    content = [token for token in tokens if token not in DEFAULT_STOPWORDS and token not in IDENTITY_OPERATOR_TOKENS]
    candidate = next((token for token in content if token in IDENTITY_RELATION_HINTS), "")
    result = relation_reference(surface=candidate)
    return result


def build_standalone_identity(request: str, scope: object = EMPTY_SCOPE_KEY) -> dict:
    """Build a conservative identity from request surfaces with no external I/O."""
    try:
        validated_scope = validate_scope_key(scope)
    except IdentityValidationError as error:
        raise IdentityValidationError("standalone identity scope must be a ScopeKey") from error
    raw = require_raw_text(request, "identity request", MAX_CANONICAL_FORM_BYTES, allow_empty=False)
    canonical_form = normalize_retrieval_key(raw)
    if not canonical_form:
        raise IdentityValidationError("identity request normalizes to an empty canonical form")
    operator = extract_operator(raw)
    entities = extract_entities_and_identifiers(raw)
    relation = extract_relation_surface(raw, operator, entities)
    qualifiers = extract_qualifiers(raw, operator)
    lexical_terms = extract_lexical_terms(raw)
    temporal = parse_temporal_query(raw)
    if temporal["source_text"]:
        temporal_terms = set(normalize_retrieval_key(temporal["source_text"]).split())
        lexical_terms = tuple(term for term in lexical_terms if term not in temporal_terms)
    result = query_identity(
        canonical_form=canonical_form,
        operator=operator,
        entities=entities,
        relation=relation,
        qualifiers=qualifiers,
        lexical_terms=lexical_terms,
        scope=validated_scope,
    )
    return result


def build_retrieval_representation(request: str, aliases: tuple[str, ...] = ()) -> dict:
    """Build the non-executable retrieval representations for one response."""
    result = retrieval_representation(canonical=request, aliases=aliases)
    return result


def validate_authoritative_identity(
    identity: dict,
    retrieval: dict,
) -> dict:
    """Validate cross-contract consistency without rewriting authoritative data."""
    try:
        validated_identity = validate_query_identity(identity)
    except IdentityValidationError as error:
        raise IdentityValidationError("authoritative identity must be a QueryIdentity") from error
    try:
        validated_retrieval = validate_retrieval_representation(retrieval)
    except IdentityValidationError as error:
        raise IdentityValidationError("authoritative retrieval must be a RetrievalRepresentation") from error
    if validated_identity["normalization_version"] != validated_retrieval["normalization_version"]:
        raise IdentityValidationError("identity and retrieval normalization versions must match")
    build_scoped_retrieval_key(
        validated_identity["scope"],
        validated_retrieval["canonical"],
        validated_retrieval["normalization_version"],
    )
    return identity


def load_authoritative_identity(
    identity: dict,
    retrieval: dict,
) -> tuple[dict, dict]:
    """Decode and validate authoritative JSON-compatible identity contracts."""
    decoded_identity = query_identity_from_dict(identity)
    decoded_retrieval = retrieval_representation_from_dict(retrieval)
    validate_authoritative_identity(decoded_identity, decoded_retrieval)
    result = (decoded_identity, decoded_retrieval)
    return result
