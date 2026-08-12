"""Versioned, transport-neutral query identity and retrieval contracts.

This module deliberately depends only on the standard library plus Engram's
literal constants and error types. Identity construction therefore performs no
graph access, model loading, resource download, or transport work.
"""

import json
import re
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum

from engram.constants import DEFAULT_CONTRACTIONS, DEFAULT_STOPWORDS
from engram.errors import IdentityValidationError, UnsupportedIdentityVersionError
from engram.lexical import select_lexical_terms

SCOPE_SCHEMA_VERSION = 1
IDENTITY_SCHEMA_VERSION = 1
RETRIEVAL_NORMALIZATION_VERSION = 1
SCOPED_RETRIEVAL_KEY_SCHEMA_VERSION = 1
RETRIEVAL_REPRESENTATION_SCHEMA_VERSION = 1

MAX_NAMESPACE_BYTES = 128
MAX_CONTEXT_FINGERPRINT_BYTES = 512
MAX_CANONICAL_FORM_BYTES = 4096
MAX_IDENTITY_SURFACE_BYTES = 512
MAX_CANONICAL_ID_BYTES = 256
MAX_QUALIFIER_VALUE_BYTES = 512
MAX_LEXICAL_TERM_BYTES = 256
MAX_ENTITIES = 32
MAX_QUALIFIERS = 32
MAX_LEXICAL_TERMS = 128
MAX_RETRIEVAL_REPRESENTATION_BYTES = 4096
MAX_RETRIEVAL_ALIASES = 32
MAX_IDENTITY_JSON_BYTES = 262144


class QueryOperator(StrEnum):
    """Closed vocabulary for the operation requested by one query."""

    WHO = "who"
    WHAT = "what"
    WHERE = "where"
    WHEN = "when"
    WHICH = "which"
    WHY = "why"
    HOW = "how"
    HOW_MANY = "how_many"
    LOOKUP = "lookup"
    EXISTS = "exists"
    COUNT = "count"
    COMPARE = "compare"
    UNKNOWN = "unknown"


class QualifierKind(StrEnum):
    """Identity-bearing qualifier categories recognized by the first builder."""

    NEGATION = "negation"
    QUANTITY = "quantity"
    COMPARISON = "comparison"
    TEMPORAL = "temporal"
    LOCATION = "location"
    CURRENT = "current"
    HISTORICAL = "historical"


class RetrievalOrigin(StrEnum):
    """The representation that produced one scoped retrieval key."""

    CANONICAL = "canonical"
    ALIAS = "alias"


_CANONICAL_ID_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:[^\s\x00-\x1f\x7f]+$")
_PUNCTUATION_TRANSLATION = str.maketrans(
    {
        "\u2018": "'",
        "\u2019": "'",
        "\u201b": "'",
        "\u02bc": "'",
        "\uff07": "'",
        "\u2010": " ",
        "\u2011": " ",
        "\u2012": " ",
        "\u2013": " ",
        "\u2014": " ",
        "\u2212": "-",
    }
)
_CONTRACTION_RE = re.compile(
    r"(?<!\w)(?:" + "|".join(re.escape(key) for key in sorted(DEFAULT_CONTRACTIONS, key=len, reverse=True)) + r")(?!\w)"
)
_TECHNICAL_PUNCTUATION = frozenset("._:/\\-+#@'<>=%|&*$")
_ALLOWED_RAW_WHITESPACE = frozenset("\t\n\r")
_OPERATOR_TOKENS = frozenset({"who", "what", "where", "when", "which", "why", "how", "many"})
_AUXILIARIES = frozenset(
    {
        "am",
        "is",
        "are",
        "was",
        "were",
        "be",
        "been",
        "being",
        "do",
        "does",
        "did",
        "has",
        "have",
        "had",
        "can",
        "could",
        "will",
        "would",
        "should",
        "may",
        "might",
        "must",
    }
)
_DIRECT_LOOKUP_LEADS = ("find ", "lookup ", "look up ", "tell me about ", "show me ")
_NEGATION_TERMS = frozenset({"not", "no", "never", "without", "neither", "nor"})
_CURRENT_TERMS = frozenset({"current", "currently", "latest", "now", "today", "present"})
_HISTORICAL_TERMS = frozenset({"historical", "historically", "previous", "previously", "former", "formerly", "past"})
_COMPARISON_PHRASES = (
    "compare",
    "compared with",
    "compared to",
    "versus",
    " vs ",
    "more than",
    "less than",
    "greater than",
    "fewer than",
    "oldest",
    "newest",
)
_COMPARISON_OPERATOR_RE = re.compile(r"(?<![<>=!])(?:<=|>=|==|!=|<|>)(?![<>=])")
_LOCATION_TERMS = frozenset({"near", "nearby", "within", "inside", "outside"})
_RELATION_HINTS = frozenset(
    {
        "acquire",
        "acquired",
        "born",
        "created",
        "developed",
        "founded",
        "invented",
        "located",
        "made",
        "maintains",
        "owns",
        "released",
        "support",
        "supports",
        "use",
        "uses",
        "wrote",
    }
)
_ENTITY_EXCLUDED_WORDS = frozenset(
    {
        *_OPERATOR_TOKENS,
        *_AUXILIARIES,
        "a",
        "an",
        "the",
        "compare",
        "count",
        "find",
        "lookup",
        "show",
        "tell",
        "current",
        "latest",
        "historical",
    }
)
_TITLE_SEQUENCE_RE = re.compile(r"(?<![\w])(?:[A-Z][\w'’+#.-]*)(?:\s+(?:[A-Z][\w'’+#.-]*)){0,4}")
_QUOTED_SPAN_RE = re.compile(r"[\"“]([^\"”]{1,512})[\"”]")
_TECHNICAL_PATTERNS = (
    re.compile(r"(?<!\w)[A-Za-z]:\\[^\s?*\"<>|]+"),
    re.compile(r"(?<!\w)/(?:[A-Za-z0-9._~!$&'()*+,;=:@%+-]+/)*[A-Za-z0-9._~!$&'()*+,;=:@%+-]+"),
    re.compile(r"(?<!\w)v?\d+(?:\.\d+){1,}(?!\w)", re.IGNORECASE),
    re.compile(r"(?<!\w)[A-Z][A-Z0-9]+(?:[-_][A-Z0-9]+)+(?!\w)"),
    re.compile(r"(?<!\w)[A-Za-z][A-Za-z0-9]*(?:\+\+|#)(?!\w)"),
    re.compile(r"(?<!\w)@[A-Za-z0-9_.-]+"),
    re.compile(r"(?<!\w)[A-Za-z][A-Za-z0-9_-]*(?:[.:/][A-Za-z0-9][A-Za-z0-9_-]*)+(?!\w)"),
)


def _byte_length(value: str) -> int:
    return len(value.encode("utf-8"))


def _require_text(value: object, name: str, maximum_bytes: int, *, allow_empty: bool) -> str:
    if not isinstance(value, str):
        raise IdentityValidationError(f"{name} must be a string")
    if not allow_empty and not value:
        raise IdentityValidationError(f"{name} must be a non-empty string")
    if _byte_length(value) > maximum_bytes:
        raise IdentityValidationError(f"{name} exceeds {maximum_bytes} UTF-8 bytes")
    if any(unicodedata.category(character) in {"Cc", "Cs"} for character in value):
        raise IdentityValidationError(f"{name} contains a control or surrogate character")
    return value


def _require_raw_text(value: object, name: str, maximum_bytes: int, *, allow_empty: bool) -> str:
    if not isinstance(value, str):
        raise IdentityValidationError(f"{name} must be a string")
    if not allow_empty and not value.strip():
        raise IdentityValidationError(f"{name} must contain non-whitespace text")
    if _byte_length(value) > maximum_bytes:
        raise IdentityValidationError(f"{name} exceeds {maximum_bytes} UTF-8 bytes")
    for character in value:
        category = unicodedata.category(character)
        if category in {"Cc", "Cs"} and character not in _ALLOWED_RAW_WHITESPACE:
            raise IdentityValidationError(f"{name} contains a control or surrogate character")
    return value


def _require_version(value: object, expected: int, name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise IdentityValidationError(f"{name} must be an integer")
    if value != expected:
        raise UnsupportedIdentityVersionError(f"unsupported {name}: {value}; expected {expected}")
    return value


def _require_mapping(value: object, name: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise IdentityValidationError(f"{name} must be an object")
    return value


def _require_list(value: object, name: str) -> list[object]:
    if not isinstance(value, list):
        raise IdentityValidationError(f"{name} must be an array")
    return list(value)


def _require_exact_keys(data: Mapping[str, object], expected: frozenset[str], name: str) -> None:
    actual = set(data)
    missing = expected - actual
    extra = actual - expected
    if missing:
        raise IdentityValidationError(f"{name} is missing fields: {', '.join(sorted(missing))}")
    if extra:
        raise IdentityValidationError(f"{name} has unsupported fields: {', '.join(sorted(str(key) for key in extra))}")


def _load_json_object(value: str, name: str) -> Mapping[str, object]:
    _require_raw_text(value, name, MAX_IDENTITY_JSON_BYTES, allow_empty=False)
    try:
        decoded = json.loads(value)
    except json.JSONDecodeError as error:
        raise IdentityValidationError(f"{name} is not valid JSON: {error.msg}") from error
    return _require_mapping(decoded, name)


def _dump_json(data: dict[str, object]) -> str:
    return json.dumps(data, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":"))


def _expand_contraction(match: re.Match) -> str:
    return DEFAULT_CONTRACTIONS[match.group(0)]


def _clean_normalized_token(token: str) -> str:
    cleaned = token.strip("'")
    cleaned = cleaned.rstrip(".")
    if cleaned.endswith(":") and not re.fullmatch(r"[a-z]:", cleaned):
        cleaned = cleaned.rstrip(":")
    return cleaned


def normalize_retrieval_key(text: str, normalization_version: int = RETRIEVAL_NORMALIZATION_VERSION) -> str:
    """Normalize one identity or retrieval representation with version 1 rules."""
    _require_version(normalization_version, RETRIEVAL_NORMALIZATION_VERSION, "normalization_version")
    raw = _require_raw_text(text, "retrieval text", MAX_RETRIEVAL_REPRESENTATION_BYTES, allow_empty=True)
    normalized = unicodedata.normalize("NFKC", raw).translate(_PUNCTUATION_TRANSLATION).casefold()
    normalized = _CONTRACTION_RE.sub(_expand_contraction, normalized)
    characters = []
    for index, character in enumerate(normalized):
        is_not_equal = character == "!" and index + 1 < len(normalized) and normalized[index + 1] == "="
        if character.isalnum() or character.isspace() or character in _TECHNICAL_PUNCTUATION or is_not_equal:
            characters.append(character)
        else:
            characters.append(" ")
    collapsed = " ".join("".join(characters).split())
    tokens = [_clean_normalized_token(token) for token in collapsed.split()]
    return " ".join(token for token in tokens if token)


def _validate_canonical_id(value: object, name: str) -> str:
    canonical_id = _require_text(value, name, MAX_CANONICAL_ID_BYTES, allow_empty=True)
    if canonical_id and not _CANONICAL_ID_RE.fullmatch(canonical_id):
        raise IdentityValidationError(f"{name} must use a URI-like scheme and contain no whitespace")
    return canonical_id


@dataclass(frozen=True, order=True, slots=True)
class ScopeKey:
    """Versioned exact eligibility boundary for identity and retrieval."""

    namespace: str = ""
    context_fingerprint: str = ""
    schema_version: int = SCOPE_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _require_version(self.schema_version, SCOPE_SCHEMA_VERSION, "scope schema_version")
        _require_text(self.namespace, "scope namespace", MAX_NAMESPACE_BYTES, allow_empty=True)
        _require_text(
            self.context_fingerprint,
            "scope context_fingerprint",
            MAX_CONTEXT_FINGERPRINT_BYTES,
            allow_empty=True,
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "namespace": self.namespace,
            "context_fingerprint": self.context_fingerprint,
        }

    def to_json(self) -> str:
        return _dump_json(self.to_dict())

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> "ScopeKey":
        data = _require_mapping(value, "ScopeKey")
        _require_exact_keys(data, frozenset({"schema_version", "namespace", "context_fingerprint"}), "ScopeKey")
        return cls(
            schema_version=_require_version(data["schema_version"], SCOPE_SCHEMA_VERSION, "scope schema_version"),
            namespace=_require_text(data["namespace"], "scope namespace", MAX_NAMESPACE_BYTES, allow_empty=True),
            context_fingerprint=_require_text(
                data["context_fingerprint"],
                "scope context_fingerprint",
                MAX_CONTEXT_FINGERPRINT_BYTES,
                allow_empty=True,
            ),
        )

    @classmethod
    def from_json(cls, value: str) -> "ScopeKey":
        return cls.from_dict(_load_json_object(value, "ScopeKey JSON"))


DEFAULT_SCOPE_KEY = ScopeKey()


@dataclass(frozen=True, order=True, slots=True)
class EntityReference:
    """One explicit entity surface and an optional authoritative canonical ID."""

    surface: str
    canonical_id: str = ""

    def __post_init__(self) -> None:
        _require_text(self.surface, "entity surface", MAX_IDENTITY_SURFACE_BYTES, allow_empty=False)
        _validate_canonical_id(self.canonical_id, "entity canonical_id")

    def to_dict(self) -> dict[str, object]:
        return {"surface": self.surface, "canonical_id": self.canonical_id}

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> "EntityReference":
        data = _require_mapping(value, "EntityReference")
        _require_exact_keys(data, frozenset({"surface", "canonical_id"}), "EntityReference")
        return cls(
            surface=_require_text(data["surface"], "entity surface", MAX_IDENTITY_SURFACE_BYTES, allow_empty=False),
            canonical_id=_validate_canonical_id(data["canonical_id"], "entity canonical_id"),
        )


@dataclass(frozen=True, order=True, slots=True)
class RelationReference:
    """A conservative relation surface and an optional authoritative canonical ID."""

    surface: str = ""
    canonical_id: str = ""

    def __post_init__(self) -> None:
        _require_text(self.surface, "relation surface", MAX_IDENTITY_SURFACE_BYTES, allow_empty=True)
        _validate_canonical_id(self.canonical_id, "relation canonical_id")
        if self.canonical_id and not self.surface:
            raise IdentityValidationError("relation surface is required when relation canonical_id is present")

    def to_dict(self) -> dict[str, object]:
        return {"surface": self.surface, "canonical_id": self.canonical_id}

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> "RelationReference":
        data = _require_mapping(value, "RelationReference")
        _require_exact_keys(data, frozenset({"surface", "canonical_id"}), "RelationReference")
        return cls(
            surface=_require_text(data["surface"], "relation surface", MAX_IDENTITY_SURFACE_BYTES, allow_empty=True),
            canonical_id=_validate_canonical_id(data["canonical_id"], "relation canonical_id"),
        )


@dataclass(frozen=True, order=True, slots=True)
class IdentityQualifier:
    """One identity-bearing qualifier with a stable category and normalized value."""

    kind: QualifierKind
    value: str

    def __post_init__(self) -> None:
        if not isinstance(self.kind, QualifierKind):
            raise IdentityValidationError("qualifier kind must be a QualifierKind")
        normalized = _require_text(self.value, "qualifier value", MAX_QUALIFIER_VALUE_BYTES, allow_empty=False)
        if normalize_retrieval_key(normalized) != normalized:
            raise IdentityValidationError("qualifier value must already use retrieval normalization version 1")

    def to_dict(self) -> dict[str, object]:
        return {"kind": self.kind.value, "value": self.value}

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> "IdentityQualifier":
        data = _require_mapping(value, "IdentityQualifier")
        _require_exact_keys(data, frozenset({"kind", "value"}), "IdentityQualifier")
        kind_value = _require_text(data["kind"], "qualifier kind", 32, allow_empty=False)
        try:
            kind = QualifierKind(kind_value)
        except ValueError as error:
            raise IdentityValidationError(f"unsupported qualifier kind: {kind_value}") from error
        return cls(
            kind=kind,
            value=_require_text(data["value"], "qualifier value", MAX_QUALIFIER_VALUE_BYTES, allow_empty=False),
        )


@dataclass(frozen=True, order=True, slots=True)
class ScopedRetrievalKey:
    """Immutable logical key consumed by exact indexes in Section 2."""

    scope: ScopeKey
    normalized_key: str
    normalization_version: int = RETRIEVAL_NORMALIZATION_VERSION
    schema_version: int = SCOPED_RETRIEVAL_KEY_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if not isinstance(self.scope, ScopeKey):
            raise IdentityValidationError("scoped retrieval key scope must be a ScopeKey")
        _require_version(
            self.schema_version,
            SCOPED_RETRIEVAL_KEY_SCHEMA_VERSION,
            "scoped retrieval key schema_version",
        )
        _require_version(self.normalization_version, RETRIEVAL_NORMALIZATION_VERSION, "normalization_version")
        normalized = _require_text(
            self.normalized_key,
            "normalized retrieval key",
            MAX_RETRIEVAL_REPRESENTATION_BYTES,
            allow_empty=False,
        )
        if normalize_retrieval_key(normalized, self.normalization_version) != normalized:
            raise IdentityValidationError("normalized retrieval key does not match its normalization version")

    @classmethod
    def build(
        cls,
        scope: ScopeKey,
        representation: str,
        normalization_version: int = RETRIEVAL_NORMALIZATION_VERSION,
    ) -> "ScopedRetrievalKey":
        normalized = normalize_retrieval_key(representation, normalization_version)
        if not normalized:
            raise IdentityValidationError("retrieval representation normalizes to an empty key")
        return cls(scope=scope, normalized_key=normalized, normalization_version=normalization_version)

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "normalization_version": self.normalization_version,
            "scope": self.scope.to_dict(),
            "normalized_key": self.normalized_key,
        }

    def to_json(self) -> str:
        return _dump_json(self.to_dict())

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> "ScopedRetrievalKey":
        data = _require_mapping(value, "ScopedRetrievalKey")
        _require_exact_keys(
            data,
            frozenset({"schema_version", "normalization_version", "scope", "normalized_key"}),
            "ScopedRetrievalKey",
        )
        return cls(
            schema_version=_require_version(
                data["schema_version"],
                SCOPED_RETRIEVAL_KEY_SCHEMA_VERSION,
                "scoped retrieval key schema_version",
            ),
            normalization_version=_require_version(
                data["normalization_version"],
                RETRIEVAL_NORMALIZATION_VERSION,
                "normalization_version",
            ),
            scope=ScopeKey.from_dict(_require_mapping(data["scope"], "scoped retrieval key scope")),
            normalized_key=_require_text(
                data["normalized_key"],
                "normalized retrieval key",
                MAX_RETRIEVAL_REPRESENTATION_BYTES,
                allow_empty=False,
            ),
        )

    @classmethod
    def from_json(cls, value: str) -> "ScopedRetrievalKey":
        return cls.from_dict(_load_json_object(value, "ScopedRetrievalKey JSON"))


@dataclass(frozen=True, order=True, slots=True)
class RetrievalKeyBinding:
    """A scoped key plus its original representation and provenance."""

    key: ScopedRetrievalKey
    origin: RetrievalOrigin
    representation: str

    def __post_init__(self) -> None:
        if not isinstance(self.key, ScopedRetrievalKey):
            raise IdentityValidationError("retrieval binding key must be a ScopedRetrievalKey")
        if not isinstance(self.origin, RetrievalOrigin):
            raise IdentityValidationError("retrieval binding origin must be a RetrievalOrigin")
        _require_raw_text(
            self.representation,
            "retrieval binding representation",
            MAX_RETRIEVAL_REPRESENTATION_BYTES,
            allow_empty=False,
        )
        if normalize_retrieval_key(self.representation, self.key.normalization_version) != self.key.normalized_key:
            raise IdentityValidationError("retrieval binding representation does not produce its scoped key")


@dataclass(frozen=True, order=True, slots=True)
class RetrievalRepresentation:
    """One canonical request plus bounded non-executable aliases."""

    canonical: str
    aliases: tuple[str, ...] = ()
    normalization_version: int = RETRIEVAL_NORMALIZATION_VERSION
    schema_version: int = RETRIEVAL_REPRESENTATION_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _require_version(
            self.schema_version,
            RETRIEVAL_REPRESENTATION_SCHEMA_VERSION,
            "retrieval representation schema_version",
        )
        _require_version(self.normalization_version, RETRIEVAL_NORMALIZATION_VERSION, "normalization_version")
        _require_raw_text(
            self.canonical,
            "retrieval canonical representation",
            MAX_RETRIEVAL_REPRESENTATION_BYTES,
            allow_empty=False,
        )
        if not isinstance(self.aliases, tuple):
            raise IdentityValidationError("retrieval aliases must be a tuple")
        if len(self.aliases) > MAX_RETRIEVAL_ALIASES:
            raise IdentityValidationError(f"retrieval aliases exceed the limit of {MAX_RETRIEVAL_ALIASES}")

        canonical_key = normalize_retrieval_key(self.canonical, self.normalization_version)
        if not canonical_key:
            raise IdentityValidationError("retrieval canonical representation normalizes to an empty key")
        seen = {canonical_key}
        aliases = []
        for index, alias in enumerate(self.aliases):
            validated = _require_raw_text(
                alias,
                f"retrieval alias {index}",
                MAX_RETRIEVAL_REPRESENTATION_BYTES,
                allow_empty=False,
            )
            normalized = normalize_retrieval_key(validated, self.normalization_version)
            if not normalized:
                raise IdentityValidationError(f"retrieval alias {index} normalizes to an empty key")
            if normalized in seen:
                continue
            seen.add(normalized)
            aliases.append(validated)
        object.__setattr__(self, "aliases", tuple(aliases))

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "normalization_version": self.normalization_version,
            "canonical": self.canonical,
            "aliases": list(self.aliases),
        }

    def to_json(self) -> str:
        return _dump_json(self.to_dict())

    def bindings(self, scope: ScopeKey) -> tuple[RetrievalKeyBinding, ...]:
        canonical_binding = RetrievalKeyBinding(
            key=ScopedRetrievalKey.build(scope, self.canonical, self.normalization_version),
            origin=RetrievalOrigin.CANONICAL,
            representation=self.canonical,
        )
        alias_bindings = tuple(
            RetrievalKeyBinding(
                key=ScopedRetrievalKey.build(scope, alias, self.normalization_version),
                origin=RetrievalOrigin.ALIAS,
                representation=alias,
            )
            for alias in self.aliases
        )
        return (canonical_binding, *alias_bindings)

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> "RetrievalRepresentation":
        data = _require_mapping(value, "RetrievalRepresentation")
        _require_exact_keys(
            data,
            frozenset({"schema_version", "normalization_version", "canonical", "aliases"}),
            "RetrievalRepresentation",
        )
        aliases = _require_list(data["aliases"], "retrieval aliases")
        validated_aliases = []
        for alias in aliases:
            if not isinstance(alias, str):
                raise IdentityValidationError("every retrieval alias must be a string")
            validated_aliases.append(alias)
        return cls(
            schema_version=_require_version(
                data["schema_version"],
                RETRIEVAL_REPRESENTATION_SCHEMA_VERSION,
                "retrieval representation schema_version",
            ),
            normalization_version=_require_version(
                data["normalization_version"],
                RETRIEVAL_NORMALIZATION_VERSION,
                "normalization_version",
            ),
            canonical=_require_raw_text(
                data["canonical"],
                "retrieval canonical representation",
                MAX_RETRIEVAL_REPRESENTATION_BYTES,
                allow_empty=False,
            ),
            aliases=tuple(validated_aliases),
        )

    @classmethod
    def from_json(cls, value: str) -> "RetrievalRepresentation":
        return cls.from_dict(_load_json_object(value, "RetrievalRepresentation JSON"))


@dataclass(frozen=True, order=True, slots=True)
class QueryIdentity:
    """Versioned semantic identity kept separate from retrieval terms."""

    canonical_form: str
    operator: QueryOperator = QueryOperator.UNKNOWN
    entities: tuple[EntityReference, ...] = ()
    relation: RelationReference = field(default_factory=RelationReference)
    qualifiers: tuple[IdentityQualifier, ...] = ()
    lexical_terms: tuple[str, ...] = ()
    scope: ScopeKey = field(default_factory=ScopeKey)
    normalization_version: int = RETRIEVAL_NORMALIZATION_VERSION
    schema_version: int = IDENTITY_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _require_version(self.schema_version, IDENTITY_SCHEMA_VERSION, "identity schema_version")
        _require_version(self.normalization_version, RETRIEVAL_NORMALIZATION_VERSION, "normalization_version")
        canonical = _require_text(
            self.canonical_form,
            "identity canonical_form",
            MAX_CANONICAL_FORM_BYTES,
            allow_empty=False,
        )
        if normalize_retrieval_key(canonical, self.normalization_version) != canonical:
            raise IdentityValidationError("identity canonical_form must already match its normalization version")
        if not isinstance(self.operator, QueryOperator):
            raise IdentityValidationError("identity operator must be a QueryOperator")
        if not isinstance(self.entities, tuple) or not all(isinstance(entity, EntityReference) for entity in self.entities):
            raise IdentityValidationError("identity entities must be a tuple of EntityReference values")
        if len(self.entities) > MAX_ENTITIES:
            raise IdentityValidationError(f"identity entities exceed the limit of {MAX_ENTITIES}")
        if len(set(self.entities)) != len(self.entities):
            raise IdentityValidationError("identity entities must be unique")
        if not isinstance(self.relation, RelationReference):
            raise IdentityValidationError("identity relation must be a RelationReference")
        if not isinstance(self.qualifiers, tuple) or not all(
            isinstance(qualifier, IdentityQualifier) for qualifier in self.qualifiers
        ):
            raise IdentityValidationError("identity qualifiers must be a tuple of IdentityQualifier values")
        if len(self.qualifiers) > MAX_QUALIFIERS:
            raise IdentityValidationError(f"identity qualifiers exceed the limit of {MAX_QUALIFIERS}")
        if len(set(self.qualifiers)) != len(self.qualifiers):
            raise IdentityValidationError("identity qualifiers must be unique")
        if not isinstance(self.lexical_terms, tuple):
            raise IdentityValidationError("identity lexical_terms must be a tuple")
        if len(self.lexical_terms) > MAX_LEXICAL_TERMS:
            raise IdentityValidationError(f"identity lexical_terms exceed the limit of {MAX_LEXICAL_TERMS}")
        lexical_terms = []
        for index, term in enumerate(self.lexical_terms):
            validated = _require_text(term, f"identity lexical term {index}", MAX_LEXICAL_TERM_BYTES, allow_empty=False)
            if normalize_retrieval_key(validated, self.normalization_version) != validated:
                raise IdentityValidationError(f"identity lexical term {index} must already be normalized")
            if any(character.isspace() for character in validated):
                raise IdentityValidationError(f"identity lexical term {index} must be one token")
            lexical_terms.append(validated)
        if len(set(lexical_terms)) != len(lexical_terms):
            raise IdentityValidationError("identity lexical_terms must be unique")
        if not isinstance(self.scope, ScopeKey):
            raise IdentityValidationError("identity scope must be a ScopeKey")

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "normalization_version": self.normalization_version,
            "canonical_form": self.canonical_form,
            "operator": self.operator.value,
            "entities": [entity.to_dict() for entity in self.entities],
            "relation": self.relation.to_dict(),
            "qualifiers": [qualifier.to_dict() for qualifier in self.qualifiers],
            "lexical_terms": list(self.lexical_terms),
            "scope": self.scope.to_dict(),
        }

    def to_json(self) -> str:
        return _dump_json(self.to_dict())

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> "QueryIdentity":
        data = _require_mapping(value, "QueryIdentity")
        _require_exact_keys(
            data,
            frozenset(
                {
                    "schema_version",
                    "normalization_version",
                    "canonical_form",
                    "operator",
                    "entities",
                    "relation",
                    "qualifiers",
                    "lexical_terms",
                    "scope",
                }
            ),
            "QueryIdentity",
        )
        operator_value = _require_text(data["operator"], "identity operator", 32, allow_empty=False)
        try:
            operator = QueryOperator(operator_value)
        except ValueError as error:
            raise IdentityValidationError(f"unsupported identity operator: {operator_value}") from error
        entities = _require_list(data["entities"], "identity entities")
        qualifiers = _require_list(data["qualifiers"], "identity qualifiers")
        lexical_terms = _require_list(data["lexical_terms"], "identity lexical_terms")
        validated_lexical_terms = []
        for term in lexical_terms:
            if not isinstance(term, str):
                raise IdentityValidationError("every identity lexical term must be a string")
            validated_lexical_terms.append(term)
        return cls(
            schema_version=_require_version(data["schema_version"], IDENTITY_SCHEMA_VERSION, "identity schema_version"),
            normalization_version=_require_version(
                data["normalization_version"],
                RETRIEVAL_NORMALIZATION_VERSION,
                "normalization_version",
            ),
            canonical_form=_require_text(
                data["canonical_form"],
                "identity canonical_form",
                MAX_CANONICAL_FORM_BYTES,
                allow_empty=False,
            ),
            operator=operator,
            entities=tuple(EntityReference.from_dict(_require_mapping(entity, "identity entity")) for entity in entities),
            relation=RelationReference.from_dict(_require_mapping(data["relation"], "identity relation")),
            qualifiers=tuple(
                IdentityQualifier.from_dict(_require_mapping(qualifier, "identity qualifier")) for qualifier in qualifiers
            ),
            lexical_terms=tuple(validated_lexical_terms),
            scope=ScopeKey.from_dict(_require_mapping(data["scope"], "identity scope")),
        )

    @classmethod
    def from_json(cls, value: str) -> "QueryIdentity":
        return cls.from_dict(_load_json_object(value, "QueryIdentity JSON"))


def extract_operator(request: str) -> QueryOperator:
    """Classify a request through a conservative, closed operator vocabulary."""
    normalized = normalize_retrieval_key(request)
    if not normalized:
        return QueryOperator.UNKNOWN
    if normalized.startswith("how many ") or normalized == "how many":
        return QueryOperator.HOW_MANY
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
        return direct[first]
    if first == "count" or normalized.startswith("number of "):
        return QueryOperator.COUNT
    if first == "compare" or " versus " in f" {normalized} " or " vs " in f" {normalized} ":
        return QueryOperator.COMPARE
    if normalized.startswith(("is there ", "are there ")) or normalized.endswith(" exist"):
        return QueryOperator.EXISTS
    if normalized.startswith(_DIRECT_LOOKUP_LEADS):
        return QueryOperator.LOOKUP
    return QueryOperator.UNKNOWN


def extract_qualifiers(request: str, operator: QueryOperator) -> tuple[IdentityQualifier, ...]:
    """Extract explicit identity-bearing qualifiers without lexical filtering."""
    if not isinstance(operator, QueryOperator):
        raise IdentityValidationError("qualifier extraction operator must be a QueryOperator")
    normalized = normalize_retrieval_key(request)
    tokens = normalized.split()
    qualifiers = []

    for token in tokens:
        if token in _NEGATION_TERMS:
            qualifiers.append(IdentityQualifier(QualifierKind.NEGATION, token))
            break
    for token in tokens:
        if token in _CURRENT_TERMS:
            qualifiers.append(IdentityQualifier(QualifierKind.CURRENT, token))
            break
    for token in tokens:
        if token in _HISTORICAL_TERMS:
            qualifiers.append(IdentityQualifier(QualifierKind.HISTORICAL, token))
            break

    temporal_match = re.search(r"\b(?:as of|before|after|since|until|during|in)\s+(?:the\s+)?(?:year\s+)?\d{4}\b", normalized)
    if temporal_match:
        qualifiers.append(IdentityQualifier(QualifierKind.TEMPORAL, temporal_match.group(0)))

    comparison_match = _COMPARISON_OPERATOR_RE.search(normalized)
    comparison = comparison_match.group(0) if comparison_match else ""
    if not comparison:
        comparison = next((phrase.strip() for phrase in _COMPARISON_PHRASES if phrase in f" {normalized} "), "")
    if comparison or operator == QueryOperator.COMPARE:
        qualifiers.append(IdentityQualifier(QualifierKind.COMPARISON, comparison or operator.value))

    if operator in {QueryOperator.HOW_MANY, QueryOperator.COUNT} or "number of" in normalized or "amount of" in normalized:
        qualifiers.append(IdentityQualifier(QualifierKind.QUANTITY, operator.value))

    location = next((token for token in tokens if token in _LOCATION_TERMS), "")
    if operator == QueryOperator.WHERE or location:
        qualifiers.append(IdentityQualifier(QualifierKind.LOCATION, location or operator.value))

    return tuple(dict.fromkeys(qualifiers))


def extract_entities_and_identifiers(request: str) -> tuple[EntityReference, ...]:
    """Extract explicit surfaces and technical identifiers without graph access."""
    raw = _require_raw_text(request, "identity request", MAX_CANONICAL_FORM_BYTES, allow_empty=False)
    normalized_unicode = unicodedata.normalize("NFKC", raw).translate(_PUNCTUATION_TRANSLATION)
    candidates: list[tuple[int, int, str]] = []
    for pattern in _TECHNICAL_PATTERNS:
        candidates.extend((match.start(), match.end(), match.group(0)) for match in pattern.finditer(normalized_unicode))
    candidates.extend((match.start(1), match.end(1), match.group(1)) for match in _QUOTED_SPAN_RE.finditer(normalized_unicode))
    for match in _TITLE_SEQUENCE_RE.finditer(normalized_unicode):
        surface = match.group(0).strip()
        surface_parts = surface.split()
        while surface_parts and normalize_retrieval_key(surface_parts[0]).rstrip(".") in _ENTITY_EXCLUDED_WORDS:
            surface_parts = surface_parts[1:]
        surface = " ".join(surface_parts)
        words = normalize_retrieval_key(surface).split()
        if words and not all(word in _ENTITY_EXCLUDED_WORDS for word in words):
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
        entities.append(EntityReference(surface=cleaned))
        if len(entities) == MAX_ENTITIES:
            break
    return tuple(entities)


def extract_lexical_terms(request: str) -> tuple[str, ...]:
    """Derive bounded retrieval terms separately from semantic identity fields."""
    normalized = normalize_retrieval_key(request)
    stopwords = DEFAULT_STOPWORDS | _OPERATOR_TOKENS | _AUXILIARIES
    terms = select_lexical_terms(
        [token.strip("'") for token in normalized.split()],
        set(stopwords),
        allow_technical=True,
        max_terms=MAX_LEXICAL_TERMS,
    )
    return tuple(terms)


def extract_relation_surface(
    request: str,
    operator: QueryOperator,
    entities: tuple[EntityReference, ...],
) -> RelationReference:
    """Extract a main relation only when simple surface evidence supports one."""
    if not isinstance(operator, QueryOperator):
        raise IdentityValidationError("relation extraction operator must be a QueryOperator")
    if not isinstance(entities, tuple) or not all(isinstance(entity, EntityReference) for entity in entities):
        raise IdentityValidationError("relation extraction entities must be EntityReference values")
    if operator in {QueryOperator.LOOKUP, QueryOperator.HOW_MANY, QueryOperator.COUNT, QueryOperator.COMPARE, QueryOperator.EXISTS}:
        return RelationReference()

    normalized = normalize_retrieval_key(request)
    tokens = normalized.split()
    if tokens and tokens[0] in _OPERATOR_TOKENS:
        tokens = tokens[1:]
    if not tokens:
        return RelationReference()

    auxiliary_index = next((index for index, token in enumerate(tokens) if token in _AUXILIARIES), -1)
    if auxiliary_index >= 0:
        tail = [token for token in tokens[auxiliary_index + 1 :] if token not in DEFAULT_STOPWORDS]
        if not tail:
            return RelationReference()
        candidate = next(
            (token for token in tail if token in _RELATION_HINTS),
            "",
        )
        if candidate:
            return RelationReference(surface=candidate)
        return RelationReference()

    content = [token for token in tokens if token not in DEFAULT_STOPWORDS and token not in _OPERATOR_TOKENS]
    candidate = next((token for token in content if token in _RELATION_HINTS), "")
    return RelationReference(surface=candidate)


def build_standalone_identity(request: str, scope: ScopeKey = DEFAULT_SCOPE_KEY) -> QueryIdentity:
    """Build a conservative identity from request surfaces with no external I/O."""
    if not isinstance(scope, ScopeKey):
        raise IdentityValidationError("standalone identity scope must be a ScopeKey")
    raw = _require_raw_text(request, "identity request", MAX_CANONICAL_FORM_BYTES, allow_empty=False)
    canonical_form = normalize_retrieval_key(raw)
    if not canonical_form:
        raise IdentityValidationError("identity request normalizes to an empty canonical form")
    operator = extract_operator(raw)
    entities = extract_entities_and_identifiers(raw)
    relation = extract_relation_surface(raw, operator, entities)
    qualifiers = extract_qualifiers(raw, operator)
    lexical_terms = extract_lexical_terms(raw)
    return QueryIdentity(
        canonical_form=canonical_form,
        operator=operator,
        entities=entities,
        relation=relation,
        qualifiers=qualifiers,
        lexical_terms=lexical_terms,
        scope=scope,
    )


def build_retrieval_representation(request: str, aliases: tuple[str, ...] = ()) -> RetrievalRepresentation:
    """Build the non-executable retrieval representations for one response."""
    return RetrievalRepresentation(canonical=request, aliases=aliases)


def validate_authoritative_identity(
    identity: QueryIdentity,
    retrieval: RetrievalRepresentation,
) -> QueryIdentity:
    """Validate cross-contract consistency without rewriting authoritative data."""
    if not isinstance(identity, QueryIdentity):
        raise IdentityValidationError("authoritative identity must be a QueryIdentity")
    if not isinstance(retrieval, RetrievalRepresentation):
        raise IdentityValidationError("authoritative retrieval must be a RetrievalRepresentation")
    if identity.normalization_version != retrieval.normalization_version:
        raise IdentityValidationError("identity and retrieval normalization versions must match")
    ScopedRetrievalKey.build(identity.scope, retrieval.canonical, retrieval.normalization_version)
    return identity


def load_authoritative_identity(
    identity: Mapping[str, object],
    retrieval: Mapping[str, object],
) -> tuple[QueryIdentity, RetrievalRepresentation]:
    """Decode and validate authoritative JSON-compatible identity contracts."""
    decoded_identity = QueryIdentity.from_dict(identity)
    decoded_retrieval = RetrievalRepresentation.from_dict(retrieval)
    validate_authoritative_identity(decoded_identity, decoded_retrieval)
    return decoded_identity, decoded_retrieval
