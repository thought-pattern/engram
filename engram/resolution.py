"""Transport-neutral contracts for the bounded unified resolution pipeline."""

import hashlib
import json
import math
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from types import MappingProxyType
from typing import cast

from engram.artifacts import LifecycleState
from engram.eligibility import EligibilityContext, EligibilityContextFactory
from engram.errors import InvalidRequestError
from engram.identity import (
    QueryIdentity,
    ScopeKey,
    build_retrieval_representation,
    build_standalone_identity,
    validate_authoritative_identity,
)
from engram.substitutions import expand_contractions

RESOLUTION_BUDGET_SCHEMA_VERSION = 1
BUDGET_CONSUMPTION_SCHEMA_VERSION = 1
QUERY_FRAME_SCHEMA_VERSION = 1
FEATURE_SET_SCHEMA_VERSION = 1
CANONICAL_CLAIM_REFERENCES_SCHEMA_VERSION = 1
CLAIM_VALIDITY_INPUTS_SCHEMA_VERSION = 1
CLAIM_TRUST_INPUTS_SCHEMA_VERSION = 1
DISCLOSURE_DECISION_SCHEMA_VERSION = 1
CLAIM_EVIDENCE_RECORD_SCHEMA_VERSION = 1
EVIDENCE_REFERENCE_SCHEMA_VERSION = 1
CANDIDATE_SCHEMA_VERSION = 1
ACCOUNTING_OBSERVATION_SCHEMA_VERSION = 1
RESOLVER_RESULT_SCHEMA_VERSION = 1
RESOLUTION_RESULT_SCHEMA_VERSION = 1

MAX_REQUEST_BYTES = 16_384
MAX_DIAGNOSTIC_ID_BYTES = 256
MAX_RESOLVER_NAME_BYTES = 96
MAX_REASON_CODE_BYTES = 96
MAX_FEATURES = 64
MAX_TRACE_STEPS = 32
MAX_JSON_DEPTH = 8
MAX_JSON_ITEMS = 4_096
MAX_JSON_STRING_BYTES = 16_384
MAX_JSON_BYTES = 65_536
MAX_RESOLUTION_VALUES = 1_000
MAX_CLAIM_IDENTIFIER_BYTES = 256
MAX_CLAIM_SOURCE_CONTRIBUTIONS = 8
MAX_CLAIM_SELECTION_REASONS = 16
MAX_CLAIM_TIMESTAMP_BYTES = 40
MAX_EVIDENCE_PACKAGE_RECORDS = 10
MAX_EVIDENCE_PACKAGE_INPUT_RECORDS = 1_000
MAX_EVIDENCE_PACKAGE_BYTES = 65_536
MAX_EVIDENCE_PACKAGE_TRUNCATION_REASONS = 8


class CostClass(StrEnum):
    """Closed cost classes used by deterministic resolver plans."""

    EXACT = "exact"
    CHEAP = "cheap"
    STANDARD = "standard"
    EXPENSIVE = "expensive"


class ExpectedObjectType(StrEnum):
    """Base object-type vocabulary populated further by Section 8."""

    UNKNOWN = "UNKNOWN"
    PERSON = "PERSON"
    PLACE = "PLACE"
    DATE = "DATE"
    NUMBER = "NUMBER"
    BOOLEAN = "BOOLEAN"
    ENTITY = "ENTITY"


class EvidenceKind(StrEnum):
    """Kinds safe for the minimal Section 4 evidence-reference contract."""

    CLAIM = "claim"
    GRAPH_FACT = "graph_fact"
    SUPPORT = "support"


class ClaimOwnership(StrEnum):
    """Allow-listed canonical Claim ownership categories."""

    PUBLIC = "PUBLIC"
    COMPANY = "COMPANY"
    CUSTOMER = "CUSTOMER"


class DisclosureBasis(StrEnum):
    """Stable provenance for a successful Claim visibility decision."""

    PUBLIC_RULE = "public_rule"
    TRUSTED_SCOPE_AUTHORITY = "trusted_scope_authority"


class EvidencePackageTruncationReason(StrEnum):
    """Stable reasons that a package retained fewer records than supplied."""

    DUPLICATE_CLAIM_ID = "duplicate_claim_id"
    RECORD_LIMIT = "record_limit"
    SERIALIZED_SIZE_LIMIT = "serialized_size_limit"


class CandidateSource(StrEnum):
    """Initial and future candidate-producing resolver sources."""

    EXACT = "exact"
    PATTERN = "pattern"
    LEXICAL = "lexical"
    SUPPORT_SEMANTIC = "support_semantic"
    STANDALONE_SEMANTIC = "standalone_semantic"
    UTILITY = "utility"


class ResolverState(StrEnum):
    """Stable resolver completion vocabulary."""

    COMPLETED = "completed"
    UNAVAILABLE = "unavailable"
    SKIPPED = "skipped"
    EXHAUSTED = "exhausted"
    FAILED = "failed"


class ResolutionOutcome(StrEnum):
    """Closed unified core outcomes."""

    ANSWER = "ANSWER"
    EVIDENCE = "EVIDENCE"
    MISS = "MISS"


def _require_text(value: object, name: str, maximum_bytes: int, *, allow_empty: bool) -> str:
    if not isinstance(value, str):
        raise InvalidRequestError(f"{name} must be a string")
    if not allow_empty and not value:
        raise InvalidRequestError(f"{name} must not be empty")
    if len(value.encode("utf-8")) > maximum_bytes:
        raise InvalidRequestError(f"{name} exceeds the limit of {maximum_bytes} UTF-8 bytes")
    return value


def _require_int(value: object, name: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise InvalidRequestError(f"{name} must be an integer")
    if not minimum <= value <= maximum:
        raise InvalidRequestError(f"{name} must be from {minimum} through {maximum}")
    return value


def _require_float(value: object, name: str, minimum: float, maximum: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise InvalidRequestError(f"{name} must be numeric")
    result = float(value)
    if not math.isfinite(result) or not minimum <= result <= maximum:
        raise InvalidRequestError(f"{name} must be finite and from {minimum} through {maximum}")
    return result


def _require_bool(value: object, name: str) -> bool:
    if not isinstance(value, bool):
        raise InvalidRequestError(f"{name} must be a boolean")
    return value


def _require_identifier(value: object, name: str, maximum_bytes: int = MAX_CLAIM_IDENTIFIER_BYTES) -> str:
    identifier = _require_text(value, name, maximum_bytes, allow_empty=False)
    if any(character.isspace() or ord(character) < 32 or ord(character) == 127 for character in identifier):
        raise InvalidRequestError(f"{name} must not contain whitespace or control characters")
    return identifier


def _require_claim_timestamp(value: object, available: object, name: str) -> tuple[str, bool]:
    presence = _require_bool(available, f"{name}_available")
    text = _require_text(value, name, MAX_CLAIM_TIMESTAMP_BYTES, allow_empty=not presence)
    if not presence:
        if text:
            raise InvalidRequestError(f"{name} must be empty when unavailable")
        return text, presence
    if not text or not text.endswith("Z"):
        raise InvalidRequestError(f"{name} must be a canonical RFC 3339 UTC timestamp ending in Z")
    try:
        parsed = datetime.fromisoformat(text[:-1] + "+00:00")
    except ValueError as error:
        raise InvalidRequestError(f"{name} must be a canonical RFC 3339 UTC timestamp") from error
    if parsed.isoformat().replace("+00:00", "Z") != text:
        raise InvalidRequestError(f"{name} must use the canonical RFC 3339 UTC representation")
    return text, presence


def _exact_mapping(value: object, name: str, keys: frozenset[str]) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise InvalidRequestError(f"{name} must be an object")
    observed = frozenset(value)
    if observed != keys:
        raise InvalidRequestError(f"{name} has invalid fields: missing={sorted(keys - observed)}, extra={sorted(observed - keys)}")
    return value


def _require_list(value: object, name: str) -> list[object]:
    if not isinstance(value, (list, tuple)):
        raise InvalidRequestError(f"{name} must be an array")
    return list(value)


def _freeze_json(value: object, name: str, depth: int = 0, count=()) -> object:
    if not count:
        count = [0]
    if depth > MAX_JSON_DEPTH:
        raise InvalidRequestError(f"{name} exceeds the maximum nesting depth of {MAX_JSON_DEPTH}")
    count[0] += 1
    if count[0] > MAX_JSON_ITEMS:
        raise InvalidRequestError(f"{name} exceeds the limit of {MAX_JSON_ITEMS} values")
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return _require_text(value, name, MAX_JSON_STRING_BYTES, allow_empty=True)
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise InvalidRequestError(f"{name} numbers must be finite")
        return value
    if isinstance(value, Mapping):
        frozen = {}
        for key in sorted(value):
            if not isinstance(key, str):
                raise InvalidRequestError(f"{name} keys must be strings")
            _require_text(key, f"{name} key", 128, allow_empty=False)
            frozen[key] = _freeze_json(value[key], name, depth + 1, count)
        return MappingProxyType(frozen)
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_json(item, name, depth + 1, count) for item in value)
    raise InvalidRequestError(f"{name} values must be concrete JSON values")


def _freeze_mapping(value: object, name: str) -> Mapping[str, object]:
    frozen = _freeze_json(value, name)
    if not isinstance(frozen, Mapping):
        raise InvalidRequestError(f"{name} must be an object")
    encoded = json.dumps(_thaw_json(frozen), sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    if len(encoded.encode("utf-8")) > MAX_JSON_BYTES:
        raise InvalidRequestError(f"{name} exceeds the limit of {MAX_JSON_BYTES} UTF-8 bytes")
    return frozen


def _thaw_json(value: object) -> object:
    if isinstance(value, Mapping):
        return {key: _thaw_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw_json(item) for item in value]
    return value


def _json_text(value: Mapping[str, object]) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def _load_json_mapping(value: str, name: str) -> Mapping[str, object]:
    if not isinstance(value, str):
        raise InvalidRequestError(f"{name} must be a string")
    try:
        decoded = json.loads(value)
    except json.JSONDecodeError as error:
        raise InvalidRequestError(f"{name} must be valid JSON") from error
    if not isinstance(decoded, Mapping):
        raise InvalidRequestError(f"{name} must contain an object")
    return decoded


def _enum_tuple(values: object, enum_type, name: str, maximum: int) -> tuple:
    if not isinstance(values, tuple):
        raise InvalidRequestError(f"{name} must be a tuple")
    if len(values) > maximum:
        raise InvalidRequestError(f"{name} exceeds the limit of {maximum}")
    if not all(isinstance(value, enum_type) for value in values):
        raise InvalidRequestError(f"{name} must contain only {enum_type.__name__} values")
    if len(set(values)) != len(values):
        raise InvalidRequestError(f"{name} must not contain duplicates")
    return values


@dataclass(frozen=True, slots=True)
class ResolutionBudget:
    """Immutable limits and captured monotonic deadline for one resolution."""

    total_time_ms: int = 2_000
    resolver_time_ms: int = 500
    max_resolvers: int = 8
    max_candidates: int = 10
    max_graph_rows: int = 100
    max_vector_results: int = 100
    max_evidence: int = 20
    max_evidence_bytes: int = 32_768
    max_output_bytes: int = 65_536
    max_diagnostic_bytes: int = 16_384
    max_working_memory_bytes: int = 16_777_216
    allowed_cost_classes: tuple[CostClass, ...] = tuple(CostClass)
    started_ns: int = 0
    deadline_ns: int = 0
    schema_version: int = RESOLUTION_BUDGET_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != RESOLUTION_BUDGET_SCHEMA_VERSION:
            raise InvalidRequestError(f"unsupported resolution budget schema_version: {self.schema_version}")
        _require_int(self.total_time_ms, "total_time_ms", 1, 60_000)
        _require_int(self.resolver_time_ms, "resolver_time_ms", 1, self.total_time_ms)
        _require_int(self.max_resolvers, "max_resolvers", 1, 64)
        _require_int(self.max_candidates, "max_candidates", 1, 1_000)
        _require_int(self.max_graph_rows, "max_graph_rows", 0, 100_000)
        _require_int(self.max_vector_results, "max_vector_results", 0, 100_000)
        _require_int(self.max_evidence, "max_evidence", 0, 1_000)
        _require_int(self.max_evidence_bytes, "max_evidence_bytes", 0, 1_048_576)
        _require_int(self.max_output_bytes, "max_output_bytes", 4_096, 2_097_152)
        _require_int(self.max_diagnostic_bytes, "max_diagnostic_bytes", 0, 1_048_576)
        _require_int(self.max_working_memory_bytes, "max_working_memory_bytes", 1, 1_073_741_824)
        _enum_tuple(self.allowed_cost_classes, CostClass, "allowed_cost_classes", len(CostClass))
        if not self.allowed_cost_classes:
            raise InvalidRequestError("allowed_cost_classes must not be empty")
        _require_int(self.started_ns, "started_ns", 0, 9_223_372_036_854_775_807)
        _require_int(self.deadline_ns, "deadline_ns", 0, 9_223_372_036_854_775_807)
        if (self.started_ns == 0) != (self.deadline_ns == 0):
            raise InvalidRequestError("started_ns and deadline_ns must be present or absent together")
        if self.started_ns and self.deadline_ns <= self.started_ns:
            raise InvalidRequestError("deadline_ns must be after started_ns")

    @classmethod
    def capture(cls, clock_ns: Callable[[], int], **limits) -> "ResolutionBudget":
        if not callable(clock_ns):
            raise InvalidRequestError("budget clock_ns must be callable")
        started = clock_ns()
        if isinstance(started, bool) or not isinstance(started, int) or started < 1:
            raise InvalidRequestError("budget clock_ns must return a positive integer")
        total_time_ms = limits.get("total_time_ms", 2_000)
        _require_int(total_time_ms, "total_time_ms", 1, 60_000)
        values = dict(limits)
        values["total_time_ms"] = total_time_ms
        values["started_ns"] = started
        values["deadline_ns"] = started + total_time_ms * 1_000_000
        return cls(**values)

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "total_time_ms": self.total_time_ms,
            "resolver_time_ms": self.resolver_time_ms,
            "max_resolvers": self.max_resolvers,
            "max_candidates": self.max_candidates,
            "max_graph_rows": self.max_graph_rows,
            "max_vector_results": self.max_vector_results,
            "max_evidence": self.max_evidence,
            "max_evidence_bytes": self.max_evidence_bytes,
            "max_output_bytes": self.max_output_bytes,
            "max_diagnostic_bytes": self.max_diagnostic_bytes,
            "max_working_memory_bytes": self.max_working_memory_bytes,
            "allowed_cost_classes": [value.value for value in self.allowed_cost_classes],
            "started_ns": self.started_ns,
            "deadline_ns": self.deadline_ns,
        }

    def recapture(self, clock_ns: Callable[[], int]) -> "ResolutionBudget":
        """Capture a fresh deadline from these immutable configured limits."""
        return ResolutionBudget.capture(
            clock_ns,
            total_time_ms=self.total_time_ms,
            resolver_time_ms=self.resolver_time_ms,
            max_resolvers=self.max_resolvers,
            max_candidates=self.max_candidates,
            max_graph_rows=self.max_graph_rows,
            max_vector_results=self.max_vector_results,
            max_evidence=self.max_evidence,
            max_evidence_bytes=self.max_evidence_bytes,
            max_output_bytes=self.max_output_bytes,
            max_diagnostic_bytes=self.max_diagnostic_bytes,
            max_working_memory_bytes=self.max_working_memory_bytes,
            allowed_cost_classes=self.allowed_cost_classes,
        )

    def to_json(self) -> str:
        return _json_text(self.to_dict())

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> "ResolutionBudget":
        keys = frozenset(
            {
                "schema_version",
                "total_time_ms",
                "resolver_time_ms",
                "max_resolvers",
                "max_candidates",
                "max_graph_rows",
                "max_vector_results",
                "max_evidence",
                "max_evidence_bytes",
                "max_output_bytes",
                "max_diagnostic_bytes",
                "max_working_memory_bytes",
                "allowed_cost_classes",
                "started_ns",
                "deadline_ns",
            }
        )
        data = _exact_mapping(value, "ResolutionBudget", keys)
        raw_costs = _require_list(data["allowed_cost_classes"], "allowed_cost_classes")
        try:
            costs = tuple(CostClass(_require_text(item, "cost class", 32, allow_empty=False)) for item in raw_costs)
        except ValueError as error:
            raise InvalidRequestError("allowed_cost_classes contains an unsupported value") from error
        return cls(
            schema_version=_require_int(data["schema_version"], "schema_version", 1, 1),
            total_time_ms=_require_int(data["total_time_ms"], "total_time_ms", 1, 60_000),
            resolver_time_ms=_require_int(data["resolver_time_ms"], "resolver_time_ms", 1, 60_000),
            max_resolvers=_require_int(data["max_resolvers"], "max_resolvers", 1, 64),
            max_candidates=_require_int(data["max_candidates"], "max_candidates", 1, 1_000),
            max_graph_rows=_require_int(data["max_graph_rows"], "max_graph_rows", 0, 100_000),
            max_vector_results=_require_int(data["max_vector_results"], "max_vector_results", 0, 100_000),
            max_evidence=_require_int(data["max_evidence"], "max_evidence", 0, 1_000),
            max_evidence_bytes=_require_int(data["max_evidence_bytes"], "max_evidence_bytes", 0, 1_048_576),
            max_output_bytes=_require_int(data["max_output_bytes"], "max_output_bytes", 4_096, 2_097_152),
            max_diagnostic_bytes=_require_int(data["max_diagnostic_bytes"], "max_diagnostic_bytes", 0, 1_048_576),
            max_working_memory_bytes=_require_int(data["max_working_memory_bytes"], "max_working_memory_bytes", 1, 1_073_741_824),
            allowed_cost_classes=costs,
            started_ns=_require_int(data["started_ns"], "started_ns", 0, 9_223_372_036_854_775_807),
            deadline_ns=_require_int(data["deadline_ns"], "deadline_ns", 0, 9_223_372_036_854_775_807),
        )

    @classmethod
    def from_json(cls, value: str) -> "ResolutionBudget":
        try:
            decoded = json.loads(value)
        except (TypeError, json.JSONDecodeError) as error:
            raise InvalidRequestError("ResolutionBudget JSON must be valid JSON") from error
        if not isinstance(decoded, Mapping):
            raise InvalidRequestError("ResolutionBudget JSON must contain an object")
        return cls.from_dict(decoded)


@dataclass(frozen=True, slots=True)
class BudgetConsumption:
    """Concrete resource use for a resolver or complete resolution."""

    elapsed_ns: int = 0
    resolvers: int = 0
    candidates: int = 0
    graph_rows: int = 0
    vector_results: int = 0
    evidence: int = 0
    evidence_bytes: int = 0
    output_bytes: int = 0
    diagnostic_bytes: int = 0
    working_memory_bytes: int = 0
    exhausted_dimensions: tuple[str, ...] = ()
    measurement_available: bool = True
    schema_version: int = BUDGET_CONSUMPTION_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != BUDGET_CONSUMPTION_SCHEMA_VERSION:
            raise InvalidRequestError(f"unsupported budget consumption schema_version: {self.schema_version}")
        for name in (
            "elapsed_ns",
            "resolvers",
            "candidates",
            "graph_rows",
            "vector_results",
            "evidence",
            "evidence_bytes",
            "output_bytes",
            "diagnostic_bytes",
            "working_memory_bytes",
        ):
            _require_int(getattr(self, name), name, 0, 9_223_372_036_854_775_807)
        if not isinstance(self.exhausted_dimensions, tuple):
            raise InvalidRequestError("exhausted_dimensions must be a tuple")
        if len(self.exhausted_dimensions) > 64:
            raise InvalidRequestError("exhausted_dimensions exceeds the limit of 64")
        normalized = tuple(
            _require_text(value, "exhausted dimension", 64, allow_empty=False) for value in self.exhausted_dimensions
        )
        if normalized != tuple(sorted(set(normalized))):
            raise InvalidRequestError("exhausted_dimensions must be unique and sorted")
        if not isinstance(self.measurement_available, bool):
            raise InvalidRequestError("measurement_available must be a boolean")

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "elapsed_ns": self.elapsed_ns,
            "resolvers": self.resolvers,
            "candidates": self.candidates,
            "graph_rows": self.graph_rows,
            "vector_results": self.vector_results,
            "evidence": self.evidence,
            "evidence_bytes": self.evidence_bytes,
            "output_bytes": self.output_bytes,
            "diagnostic_bytes": self.diagnostic_bytes,
            "working_memory_bytes": self.working_memory_bytes,
            "exhausted_dimensions": list(self.exhausted_dimensions),
            "measurement_available": self.measurement_available,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> "BudgetConsumption":
        keys = frozenset(cls().to_dict())
        data = _exact_mapping(value, "BudgetConsumption", keys)
        exhausted = _require_list(data["exhausted_dimensions"], "exhausted_dimensions")
        if not isinstance(data["measurement_available"], bool):
            raise InvalidRequestError("measurement_available must be a boolean")
        kwargs = {
            name: _require_int(data[name], name, 0, 9_223_372_036_854_775_807)
            for name in keys
            if name not in {"schema_version", "exhausted_dimensions", "measurement_available"}
        }
        return cls(
            schema_version=_require_int(data["schema_version"], "schema_version", 1, 1),
            exhausted_dimensions=tuple(_require_text(item, "exhausted dimension", 64, allow_empty=False) for item in exhausted),
            measurement_available=data["measurement_available"],
            **kwargs,
        )

    def to_json(self) -> str:
        return _json_text(self.to_dict())

    @classmethod
    def from_json(cls, value: str) -> "BudgetConsumption":
        return cls.from_dict(_load_json_mapping(value, "BudgetConsumption JSON"))


@dataclass(frozen=True, order=True, slots=True)
class InheritanceProvenance:
    """Concrete empty-capable contextual field provenance owned by Section 8."""

    field_name: str
    source_turn: int

    def __post_init__(self) -> None:
        _require_text(self.field_name, "inheritance field_name", 64, allow_empty=False)
        _require_int(self.source_turn, "inheritance source_turn", 1, 1_000_000)

    def to_dict(self) -> dict[str, object]:
        return {"field_name": self.field_name, "source_turn": self.source_turn}

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> "InheritanceProvenance":
        data = _exact_mapping(value, "InheritanceProvenance", frozenset({"field_name", "source_turn"}))
        return cls(
            field_name=_require_text(data["field_name"], "inheritance field_name", 64, allow_empty=False),
            source_turn=_require_int(data["source_turn"], "inheritance source_turn", 1, 1_000_000),
        )


@dataclass(frozen=True, order=True, slots=True)
class RewriteTraceStep:
    """Empty-capable rewrite trace container whose rule semantics belong to Section 11."""

    rule_id: str
    input_text: str
    output_text: str

    def __post_init__(self) -> None:
        _require_text(self.rule_id, "rewrite rule_id", 128, allow_empty=False)
        _require_text(self.input_text, "rewrite input_text", MAX_REQUEST_BYTES, allow_empty=False)
        _require_text(self.output_text, "rewrite output_text", MAX_REQUEST_BYTES, allow_empty=False)

    def to_dict(self) -> dict[str, object]:
        return {"rule_id": self.rule_id, "input_text": self.input_text, "output_text": self.output_text}

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> "RewriteTraceStep":
        data = _exact_mapping(value, "RewriteTraceStep", frozenset({"rule_id", "input_text", "output_text"}))
        return cls(
            rule_id=_require_text(data["rule_id"], "rewrite rule_id", 128, allow_empty=False),
            input_text=_require_text(data["input_text"], "rewrite input_text", MAX_REQUEST_BYTES, allow_empty=False),
            output_text=_require_text(data["output_text"], "rewrite output_text", MAX_REQUEST_BYTES, allow_empty=False),
        )


@dataclass(frozen=True, slots=True)
class QueryFrame:
    """Immutable base interpretation passed to every resolver."""

    original_text: str
    resolved_text: str
    identity: QueryIdentity
    expected_object_type: ExpectedObjectType
    inheritance: tuple[InheritanceProvenance, ...]
    rewrite_chain: tuple[RewriteTraceStep, ...]
    scope: ScopeKey
    required_metadata: Mapping[str, object]
    required_source_label: str
    budget: ResolutionBudget
    eligibility_context: EligibilityContext
    diagnostic_id: str
    schema_version: int = QUERY_FRAME_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != QUERY_FRAME_SCHEMA_VERSION:
            raise InvalidRequestError(f"unsupported query frame schema_version: {self.schema_version}")
        _require_text(self.original_text, "frame original_text", MAX_REQUEST_BYTES, allow_empty=False)
        _require_text(self.resolved_text, "frame resolved_text", MAX_REQUEST_BYTES, allow_empty=False)
        if not isinstance(self.identity, QueryIdentity):
            raise InvalidRequestError("frame identity must be a QueryIdentity")
        if not isinstance(self.expected_object_type, ExpectedObjectType):
            raise InvalidRequestError("frame expected_object_type must be an ExpectedObjectType")
        if not isinstance(self.inheritance, tuple) or not all(
            isinstance(value, InheritanceProvenance) for value in self.inheritance
        ):
            raise InvalidRequestError("frame inheritance must be a tuple of InheritanceProvenance values")
        if len(self.inheritance) > MAX_TRACE_STEPS or len(set(self.inheritance)) != len(self.inheritance):
            raise InvalidRequestError("frame inheritance must be bounded and unique")
        if not isinstance(self.rewrite_chain, tuple) or not all(
            isinstance(value, RewriteTraceStep) for value in self.rewrite_chain
        ):
            raise InvalidRequestError("frame rewrite_chain must be a tuple of RewriteTraceStep values")
        if len(self.rewrite_chain) > MAX_TRACE_STEPS:
            raise InvalidRequestError(f"frame rewrite_chain exceeds the limit of {MAX_TRACE_STEPS}")
        if not isinstance(self.scope, ScopeKey) or self.identity.scope != self.scope:
            raise InvalidRequestError("frame scope must match identity scope")
        object.__setattr__(self, "required_metadata", _freeze_mapping(self.required_metadata, "frame required_metadata"))
        _require_text(self.required_source_label, "frame required_source_label", 256, allow_empty=True)
        if not isinstance(self.budget, ResolutionBudget):
            raise InvalidRequestError("frame budget must be a ResolutionBudget")
        if not isinstance(self.eligibility_context, EligibilityContext):
            raise InvalidRequestError("frame eligibility_context must be an EligibilityContext")
        if self.eligibility_context.namespace != self.scope.namespace:
            raise InvalidRequestError("frame eligibility context namespace must match scope")
        _require_text(self.diagnostic_id, "frame diagnostic_id", MAX_DIAGNOSTIC_ID_BYTES, allow_empty=False)

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "original_text": self.original_text,
            "resolved_text": self.resolved_text,
            "identity": self.identity.to_dict(),
            "expected_object_type": self.expected_object_type.value,
            "inheritance": [value.to_dict() for value in self.inheritance],
            "rewrite_chain": [value.to_dict() for value in self.rewrite_chain],
            "scope": self.scope.to_dict(),
            "required_metadata": _thaw_json(self.required_metadata),
            "required_source_label": self.required_source_label,
            "budget": self.budget.to_dict(),
            "eligibility_context": self.eligibility_context.to_dict(),
            "diagnostic_id": self.diagnostic_id,
        }

    def to_json(self) -> str:
        return _json_text(self.to_dict())

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> "QueryFrame":
        keys = frozenset(
            {
                "schema_version",
                "original_text",
                "resolved_text",
                "identity",
                "expected_object_type",
                "inheritance",
                "rewrite_chain",
                "scope",
                "required_metadata",
                "required_source_label",
                "budget",
                "eligibility_context",
                "diagnostic_id",
            }
        )
        data = _exact_mapping(value, "QueryFrame", keys)
        inheritance = _require_list(data["inheritance"], "frame inheritance")
        rewrites = _require_list(data["rewrite_chain"], "frame rewrite_chain")
        try:
            expected_type = ExpectedObjectType(
                _require_text(data["expected_object_type"], "expected_object_type", 32, allow_empty=False)
            )
        except ValueError as error:
            raise InvalidRequestError("unsupported expected_object_type") from error
        return cls(
            schema_version=_require_int(data["schema_version"], "schema_version", 1, 1),
            original_text=_require_text(data["original_text"], "frame original_text", MAX_REQUEST_BYTES, allow_empty=False),
            resolved_text=_require_text(data["resolved_text"], "frame resolved_text", MAX_REQUEST_BYTES, allow_empty=False),
            identity=QueryIdentity.from_dict(
                cast(Mapping[str, object], _thaw_json(_freeze_mapping(data["identity"], "frame identity")))
            ),
            expected_object_type=expected_type,
            inheritance=tuple(InheritanceProvenance.from_dict(_freeze_mapping(item, "inheritance item")) for item in inheritance),
            rewrite_chain=tuple(RewriteTraceStep.from_dict(_freeze_mapping(item, "rewrite item")) for item in rewrites),
            scope=ScopeKey.from_dict(_freeze_mapping(data["scope"], "frame scope")),
            required_metadata=_freeze_mapping(data["required_metadata"], "frame required_metadata"),
            required_source_label=_require_text(
                data["required_source_label"], "frame required_source_label", 256, allow_empty=True
            ),
            budget=ResolutionBudget.from_dict(_freeze_mapping(data["budget"], "frame budget")),
            eligibility_context=EligibilityContext.from_dict(
                _freeze_mapping(data["eligibility_context"], "frame eligibility_context")
            ),
            diagnostic_id=_require_text(data["diagnostic_id"], "frame diagnostic_id", MAX_DIAGNOSTIC_ID_BYTES, allow_empty=False),
        )

    @classmethod
    def from_json(cls, value: str) -> "QueryFrame":
        try:
            decoded = json.loads(value)
        except (TypeError, json.JSONDecodeError) as error:
            raise InvalidRequestError("QueryFrame JSON must be valid JSON") from error
        if not isinstance(decoded, Mapping):
            raise InvalidRequestError("QueryFrame JSON must contain an object")
        return cls.from_dict(decoded)


@dataclass(frozen=True, slots=True)
class FeatureSet:
    """Generic feature value/availability container; Section 5 owns semantics."""

    values: Mapping[str, float] = field(default_factory=lambda: MappingProxyType({}))
    unavailable: tuple[str, ...] = ()
    schema_version: int = FEATURE_SET_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != FEATURE_SET_SCHEMA_VERSION:
            raise InvalidRequestError(f"unsupported feature set schema_version: {self.schema_version}")
        if not isinstance(self.values, Mapping):
            raise InvalidRequestError("feature values must be an object")
        validated = {}
        for name in sorted(self.values):
            key = _require_text(name, "feature name", 96, allow_empty=False)
            validated[key] = _require_float(self.values[name], f"feature {key}", -1_000_000.0, 1_000_000.0)
        if len(validated) > MAX_FEATURES:
            raise InvalidRequestError(f"feature values exceed the limit of {MAX_FEATURES}")
        if not isinstance(self.unavailable, tuple):
            raise InvalidRequestError("unavailable features must be a tuple")
        unavailable = tuple(_require_text(name, "unavailable feature", 96, allow_empty=False) for name in self.unavailable)
        if unavailable != tuple(sorted(set(unavailable))):
            raise InvalidRequestError("unavailable features must be unique and sorted")
        if set(validated).intersection(unavailable):
            raise InvalidRequestError("a feature cannot be both available and unavailable")
        if len(validated) + len(unavailable) > MAX_FEATURES:
            raise InvalidRequestError(f"features exceed the limit of {MAX_FEATURES}")
        object.__setattr__(self, "values", MappingProxyType(validated))

    def to_dict(self) -> dict[str, object]:
        return {"schema_version": self.schema_version, "values": dict(self.values), "unavailable": list(self.unavailable)}

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> "FeatureSet":
        data = _exact_mapping(value, "FeatureSet", frozenset({"schema_version", "values", "unavailable"}))
        unavailable = _require_list(data["unavailable"], "unavailable features")
        raw_values = _freeze_mapping(data["values"], "feature values")
        values = {name: _require_float(item, f"feature {name}", -1_000_000.0, 1_000_000.0) for name, item in raw_values.items()}
        return cls(
            schema_version=_require_int(data["schema_version"], "schema_version", 1, 1),
            values=values,
            unavailable=tuple(_require_text(item, "unavailable feature", 96, allow_empty=False) for item in unavailable),
        )

    def to_json(self) -> str:
        return _json_text(self.to_dict())

    @classmethod
    def from_json(cls, value: str) -> "FeatureSet":
        return cls.from_dict(_load_json_mapping(value, "FeatureSet JSON"))


@dataclass(frozen=True, slots=True)
class CanonicalClaimReferences:
    """Canonical graph identifiers permitted in a full Claim evidence record."""

    subject_entity_id: str
    predicate_id: str
    object_entity_id: str
    schema_version: int = CANONICAL_CLAIM_REFERENCES_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != CANONICAL_CLAIM_REFERENCES_SCHEMA_VERSION:
            raise InvalidRequestError(f"unsupported canonical Claim references schema_version: {self.schema_version}")
        _require_identifier(self.subject_entity_id, "Claim subject_entity_id")
        _require_identifier(self.predicate_id, "Claim predicate_id")
        _require_identifier(self.object_entity_id, "Claim object_entity_id")

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "subject_entity_id": self.subject_entity_id,
            "predicate_id": self.predicate_id,
            "object_entity_id": self.object_entity_id,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> "CanonicalClaimReferences":
        data = _exact_mapping(
            value,
            "CanonicalClaimReferences",
            frozenset({"schema_version", "subject_entity_id", "predicate_id", "object_entity_id"}),
        )
        return cls(
            schema_version=_require_int(data["schema_version"], "schema_version", 1, 1),
            subject_entity_id=_require_identifier(data["subject_entity_id"], "Claim subject_entity_id"),
            predicate_id=_require_identifier(data["predicate_id"], "Claim predicate_id"),
            object_entity_id=_require_identifier(data["object_entity_id"], "Claim object_entity_id"),
        )


@dataclass(frozen=True, slots=True)
class ClaimValidityInputs:
    """Current-time Claim validity decision and the disclosed world-validity bounds."""

    evaluation_time: str
    active: bool
    system_current: bool
    valid_time_current: bool
    valid_from: str = ""
    valid_from_available: bool = False
    valid_to: str = ""
    valid_to_available: bool = False
    schema_version: int = CLAIM_VALIDITY_INPUTS_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != CLAIM_VALIDITY_INPUTS_SCHEMA_VERSION:
            raise InvalidRequestError(f"unsupported Claim validity inputs schema_version: {self.schema_version}")
        evaluation, _ = _require_claim_timestamp(self.evaluation_time, True, "Claim evaluation_time")
        _require_bool(self.active, "Claim active")
        _require_bool(self.system_current, "Claim system_current")
        _require_bool(self.valid_time_current, "Claim valid_time_current")
        lower, lower_available = _require_claim_timestamp(self.valid_from, self.valid_from_available, "Claim valid_from")
        upper, upper_available = _require_claim_timestamp(self.valid_to, self.valid_to_available, "Claim valid_to")
        if lower_available and upper_available:
            lower_time = datetime.fromisoformat(lower[:-1] + "+00:00")
            upper_time = datetime.fromisoformat(upper[:-1] + "+00:00")
            if lower_time >= upper_time:
                raise InvalidRequestError("Claim valid_from must be earlier than valid_to")
        evaluation_time = datetime.fromisoformat(evaluation[:-1] + "+00:00")
        observed_current = (not lower_available or evaluation_time >= datetime.fromisoformat(lower[:-1] + "+00:00")) and (
            not upper_available or evaluation_time < datetime.fromisoformat(upper[:-1] + "+00:00")
        )
        if self.valid_time_current != observed_current:
            raise InvalidRequestError("Claim valid_time_current conflicts with the disclosed validity bounds")
        if not (self.active and self.system_current and self.valid_time_current):
            raise InvalidRequestError("Claim evidence validity inputs must describe a currently eligible Claim")

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "evaluation_time": self.evaluation_time,
            "active": self.active,
            "system_current": self.system_current,
            "valid_time_current": self.valid_time_current,
            "valid_from": self.valid_from,
            "valid_from_available": self.valid_from_available,
            "valid_to": self.valid_to,
            "valid_to_available": self.valid_to_available,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> "ClaimValidityInputs":
        data = _exact_mapping(
            value,
            "ClaimValidityInputs",
            frozenset(
                {
                    "schema_version",
                    "evaluation_time",
                    "active",
                    "system_current",
                    "valid_time_current",
                    "valid_from",
                    "valid_from_available",
                    "valid_to",
                    "valid_to_available",
                }
            ),
        )
        return cls(
            schema_version=_require_int(data["schema_version"], "schema_version", 1, 1),
            evaluation_time=_require_text(
                data["evaluation_time"], "Claim evaluation_time", MAX_CLAIM_TIMESTAMP_BYTES, allow_empty=False
            ),
            active=_require_bool(data["active"], "Claim active"),
            system_current=_require_bool(data["system_current"], "Claim system_current"),
            valid_time_current=_require_bool(data["valid_time_current"], "Claim valid_time_current"),
            valid_from=_require_text(data["valid_from"], "Claim valid_from", MAX_CLAIM_TIMESTAMP_BYTES, allow_empty=True),
            valid_from_available=_require_bool(data["valid_from_available"], "Claim valid_from_available"),
            valid_to=_require_text(data["valid_to"], "Claim valid_to", MAX_CLAIM_TIMESTAMP_BYTES, allow_empty=True),
            valid_to_available=_require_bool(data["valid_to_available"], "Claim valid_to_available"),
        )


@dataclass(frozen=True, slots=True)
class ClaimTrustInputs:
    """Supplied Claim trust values with concrete availability semantics."""

    trust_category: str = ""
    trust_category_available: bool = False
    supplied_trust: float = 0.0
    supplied_trust_available: bool = False
    supplied_trust_version: int = 0
    supplied_trust_version_available: bool = False
    schema_version: int = CLAIM_TRUST_INPUTS_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != CLAIM_TRUST_INPUTS_SCHEMA_VERSION:
            raise InvalidRequestError(f"unsupported Claim trust inputs schema_version: {self.schema_version}")
        category_available = _require_bool(self.trust_category_available, "Claim trust_category_available")
        category = _require_text(self.trust_category, "Claim trust_category", 96, allow_empty=not category_available)
        if not category_available and category:
            raise InvalidRequestError("Claim trust_category must be empty when unavailable")
        supplied_available = _require_bool(self.supplied_trust_available, "Claim supplied_trust_available")
        supplied = _require_float(self.supplied_trust, "Claim supplied_trust", 0.0, 1.0)
        if not supplied_available and supplied != 0.0:
            raise InvalidRequestError("Claim supplied_trust must be zero when unavailable")
        version_available = _require_bool(self.supplied_trust_version_available, "Claim supplied_trust_version_available")
        version = _require_int(self.supplied_trust_version, "Claim supplied_trust_version", 0, 2_147_483_647)
        if not version_available and version != 0:
            raise InvalidRequestError("Claim supplied_trust_version must be zero when unavailable")
        if supplied_available != version_available:
            raise InvalidRequestError("Claim supplied trust value and version availability must match")
        if version_available and version == 0:
            raise InvalidRequestError("Claim supplied_trust_version must be positive when available")

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "trust_category": self.trust_category,
            "trust_category_available": self.trust_category_available,
            "supplied_trust": self.supplied_trust,
            "supplied_trust_available": self.supplied_trust_available,
            "supplied_trust_version": self.supplied_trust_version,
            "supplied_trust_version_available": self.supplied_trust_version_available,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> "ClaimTrustInputs":
        data = _exact_mapping(
            value,
            "ClaimTrustInputs",
            frozenset(
                {
                    "schema_version",
                    "trust_category",
                    "trust_category_available",
                    "supplied_trust",
                    "supplied_trust_available",
                    "supplied_trust_version",
                    "supplied_trust_version_available",
                }
            ),
        )
        return cls(
            schema_version=_require_int(data["schema_version"], "schema_version", 1, 1),
            trust_category=_require_text(data["trust_category"], "Claim trust_category", 96, allow_empty=True),
            trust_category_available=_require_bool(data["trust_category_available"], "Claim trust_category_available"),
            supplied_trust=_require_float(data["supplied_trust"], "Claim supplied_trust", 0.0, 1.0),
            supplied_trust_available=_require_bool(data["supplied_trust_available"], "Claim supplied_trust_available"),
            supplied_trust_version=_require_int(data["supplied_trust_version"], "Claim supplied_trust_version", 0, 2_147_483_647),
            supplied_trust_version_available=_require_bool(
                data["supplied_trust_version_available"], "Claim supplied_trust_version_available"
            ),
        )


@dataclass(frozen=True, slots=True)
class DisclosureDecision:
    """Exact caller scope and allow-listed provenance for Claim disclosure."""

    ownership: ClaimOwnership
    basis: DisclosureBasis
    scope: ScopeKey
    policy_version: str
    authority: str = ""
    authority_available: bool = False
    schema_version: int = DISCLOSURE_DECISION_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != DISCLOSURE_DECISION_SCHEMA_VERSION:
            raise InvalidRequestError(f"unsupported disclosure decision schema_version: {self.schema_version}")
        if not isinstance(self.ownership, ClaimOwnership):
            raise InvalidRequestError("disclosure ownership must be a ClaimOwnership")
        if not isinstance(self.basis, DisclosureBasis):
            raise InvalidRequestError("disclosure basis must be a DisclosureBasis")
        if not isinstance(self.scope, ScopeKey):
            raise InvalidRequestError("disclosure scope must be a ScopeKey")
        _require_identifier(self.policy_version, "disclosure policy_version")
        authority_available = _require_bool(self.authority_available, "disclosure authority_available")
        authority = self.authority
        if authority_available:
            _require_identifier(authority, "disclosure authority")
        else:
            _require_text(authority, "disclosure authority", 256, allow_empty=True)
        if not authority_available and authority:
            raise InvalidRequestError("disclosure authority must be empty when unavailable")
        if self.ownership == ClaimOwnership.PUBLIC:
            if self.basis != DisclosureBasis.PUBLIC_RULE or authority_available:
                raise InvalidRequestError("PUBLIC Claim disclosure requires the public rule without an authority")
        elif self.basis != DisclosureBasis.TRUSTED_SCOPE_AUTHORITY or not authority_available:
            raise InvalidRequestError("non-PUBLIC Claim disclosure requires an available trusted scope authority")

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "ownership": self.ownership.value,
            "basis": self.basis.value,
            "scope": self.scope.to_dict(),
            "policy_version": self.policy_version,
            "authority": self.authority,
            "authority_available": self.authority_available,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> "DisclosureDecision":
        data = _exact_mapping(
            value,
            "DisclosureDecision",
            frozenset(
                {
                    "schema_version",
                    "ownership",
                    "basis",
                    "scope",
                    "policy_version",
                    "authority",
                    "authority_available",
                }
            ),
        )
        try:
            ownership = ClaimOwnership(_require_text(data["ownership"], "disclosure ownership", 32, allow_empty=False))
            basis = DisclosureBasis(_require_text(data["basis"], "disclosure basis", 32, allow_empty=False))
        except ValueError as error:
            raise InvalidRequestError("unsupported disclosure ownership or basis") from error
        return cls(
            schema_version=_require_int(data["schema_version"], "schema_version", 1, 1),
            ownership=ownership,
            basis=basis,
            scope=ScopeKey.from_dict(_freeze_mapping(data["scope"], "disclosure scope")),
            policy_version=_require_identifier(data["policy_version"], "disclosure policy_version"),
            authority=_require_text(data["authority"], "disclosure authority", 256, allow_empty=True),
            authority_available=_require_bool(data["authority_available"], "disclosure authority_available"),
        )


@dataclass(frozen=True, slots=True)
class ClaimEvidenceRecord:
    """Strict wire-safe full-Claim evidence without unrestricted graph content."""

    claim_id: str
    source_resolver: str
    source_contributions: tuple[str, ...]
    features: FeatureSet
    canonical_references: CanonicalClaimReferences
    validity: ClaimValidityInputs
    trust: ClaimTrustInputs
    disclosure: DisclosureDecision
    path: tuple[str, ...]
    selection_reasons: tuple[str, ...]
    schema_version: int = CLAIM_EVIDENCE_RECORD_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != CLAIM_EVIDENCE_RECORD_SCHEMA_VERSION:
            raise InvalidRequestError(f"unsupported Claim evidence record schema_version: {self.schema_version}")
        claim_id = _require_identifier(self.claim_id, "Claim evidence claim_id")
        source = _require_identifier(self.source_resolver, "Claim evidence source_resolver", MAX_RESOLVER_NAME_BYTES)
        if not isinstance(self.source_contributions, tuple):
            raise InvalidRequestError("Claim evidence source_contributions must be a tuple")
        contributions = tuple(
            _require_identifier(value, "Claim evidence source contribution", MAX_RESOLVER_NAME_BYTES)
            for value in self.source_contributions
        )
        if not contributions or len(contributions) > MAX_CLAIM_SOURCE_CONTRIBUTIONS:
            raise InvalidRequestError(
                f"Claim evidence source_contributions must contain 1 through {MAX_CLAIM_SOURCE_CONTRIBUTIONS} values"
            )
        if contributions != tuple(sorted(set(contributions))):
            raise InvalidRequestError("Claim evidence source_contributions must be unique and sorted")
        if source not in contributions:
            raise InvalidRequestError("Claim evidence source_resolver must be present in source_contributions")
        if not isinstance(self.features, FeatureSet):
            raise InvalidRequestError("Claim evidence features must be a FeatureSet")
        if not isinstance(self.canonical_references, CanonicalClaimReferences):
            raise InvalidRequestError("Claim evidence canonical_references must be CanonicalClaimReferences")
        if not isinstance(self.validity, ClaimValidityInputs):
            raise InvalidRequestError("Claim evidence validity must be ClaimValidityInputs")
        if not isinstance(self.trust, ClaimTrustInputs):
            raise InvalidRequestError("Claim evidence trust must be ClaimTrustInputs")
        if not isinstance(self.disclosure, DisclosureDecision):
            raise InvalidRequestError("Claim evidence disclosure must be DisclosureDecision")
        if not isinstance(self.path, tuple):
            raise InvalidRequestError("Claim evidence path must be a tuple")
        path = tuple(_require_identifier(value, "Claim evidence path identifier") for value in self.path)
        if path != (claim_id,):
            raise InvalidRequestError("Section 7 Claim evidence path must be the singleton claim_id")
        if not isinstance(self.selection_reasons, tuple):
            raise InvalidRequestError("Claim evidence selection_reasons must be a tuple")
        reasons = tuple(
            _require_identifier(value, "Claim evidence selection reason", MAX_REASON_CODE_BYTES) for value in self.selection_reasons
        )
        if not reasons or len(reasons) > MAX_CLAIM_SELECTION_REASONS:
            raise InvalidRequestError(
                "Claim evidence selection_reasons must contain " f"1 through {MAX_CLAIM_SELECTION_REASONS} values"
            )
        if reasons != tuple(sorted(set(reasons))):
            raise InvalidRequestError("Claim evidence selection_reasons must be unique and sorted")

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "claim_id": self.claim_id,
            "source_resolver": self.source_resolver,
            "source_contributions": list(self.source_contributions),
            "features": self.features.to_dict(),
            "canonical_references": self.canonical_references.to_dict(),
            "validity": self.validity.to_dict(),
            "trust": self.trust.to_dict(),
            "disclosure": self.disclosure.to_dict(),
            "path": list(self.path),
            "selection_reasons": list(self.selection_reasons),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> "ClaimEvidenceRecord":
        data = _exact_mapping(
            value,
            "ClaimEvidenceRecord",
            frozenset(
                {
                    "schema_version",
                    "claim_id",
                    "source_resolver",
                    "source_contributions",
                    "features",
                    "canonical_references",
                    "validity",
                    "trust",
                    "disclosure",
                    "path",
                    "selection_reasons",
                }
            ),
        )
        contributions = _require_list(data["source_contributions"], "Claim evidence source_contributions")
        path = _require_list(data["path"], "Claim evidence path")
        reasons = _require_list(data["selection_reasons"], "Claim evidence selection_reasons")
        return cls(
            schema_version=_require_int(data["schema_version"], "schema_version", 1, 1),
            claim_id=_require_identifier(data["claim_id"], "Claim evidence claim_id"),
            source_resolver=_require_identifier(
                data["source_resolver"],
                "Claim evidence source_resolver",
                MAX_RESOLVER_NAME_BYTES,
            ),
            source_contributions=tuple(
                _require_identifier(value, "Claim evidence source contribution", MAX_RESOLVER_NAME_BYTES) for value in contributions
            ),
            features=FeatureSet.from_dict(_freeze_mapping(data["features"], "Claim evidence features")),
            canonical_references=CanonicalClaimReferences.from_dict(
                _freeze_mapping(data["canonical_references"], "Claim evidence canonical_references")
            ),
            validity=ClaimValidityInputs.from_dict(_freeze_mapping(data["validity"], "Claim evidence validity")),
            trust=ClaimTrustInputs.from_dict(_freeze_mapping(data["trust"], "Claim evidence trust")),
            disclosure=DisclosureDecision.from_dict(_freeze_mapping(data["disclosure"], "Claim evidence disclosure")),
            path=tuple(_require_identifier(value, "Claim evidence path identifier") for value in path),
            selection_reasons=tuple(
                _require_identifier(value, "Claim evidence selection reason", MAX_REASON_CODE_BYTES) for value in reasons
            ),
        )

    def to_json(self) -> str:
        return _json_text(self.to_dict())

    @classmethod
    def from_json(cls, value: str) -> "ClaimEvidenceRecord":
        return cls.from_dict(_load_json_mapping(value, "ClaimEvidenceRecord JSON"))


@dataclass(frozen=True, slots=True)
class EvidencePackage:
    """Canonical, deduplicated, count- and byte-bounded full-Claim package."""

    records: tuple[ClaimEvidenceRecord, ...]
    retained_count: int
    omitted_count: int
    truncated: bool
    truncation_reasons: tuple[EvidencePackageTruncationReason, ...]
    wire_version: int = 1

    def __post_init__(self) -> None:
        if self.wire_version != 1:
            raise InvalidRequestError(f"unsupported evidence package wire_version: {self.wire_version}")
        if not isinstance(self.records, tuple) or not all(isinstance(record, ClaimEvidenceRecord) for record in self.records):
            raise InvalidRequestError("evidence package records must be a tuple of ClaimEvidenceRecord values")
        if len(self.records) > MAX_EVIDENCE_PACKAGE_RECORDS:
            raise InvalidRequestError(f"evidence package records exceeds the limit of {MAX_EVIDENCE_PACKAGE_RECORDS}")
        identifiers = tuple(record.claim_id for record in self.records)
        if identifiers != tuple(sorted(identifiers)):
            raise InvalidRequestError("evidence package records must use canonical Claim-ID order")
        if len(set(identifiers)) != len(identifiers):
            raise InvalidRequestError("evidence package records must have unique Claim IDs")
        retained = _require_int(self.retained_count, "evidence package retained_count", 0, 2_147_483_647)
        omitted = _require_int(self.omitted_count, "evidence package omitted_count", 0, 2_147_483_647)
        if retained != len(self.records):
            raise InvalidRequestError("evidence package retained_count must equal the number of records")
        _require_bool(self.truncated, "evidence package truncated")
        if not isinstance(self.truncation_reasons, tuple):
            raise InvalidRequestError("evidence package truncation_reasons must be a tuple")
        if len(self.truncation_reasons) > MAX_EVIDENCE_PACKAGE_TRUNCATION_REASONS:
            raise InvalidRequestError(
                "evidence package truncation_reasons exceeds the limit of " f"{MAX_EVIDENCE_PACKAGE_TRUNCATION_REASONS}"
            )
        if not all(isinstance(reason, EvidencePackageTruncationReason) for reason in self.truncation_reasons):
            raise InvalidRequestError("evidence package truncation_reasons must contain EvidencePackageTruncationReason values")
        reason_values = tuple(reason.value for reason in self.truncation_reasons)
        if reason_values != tuple(sorted(set(reason_values))):
            raise InvalidRequestError("evidence package truncation_reasons must be unique and sorted")
        if self.truncated != (omitted > 0):
            raise InvalidRequestError("evidence package truncated must equal whether omitted_count is positive")
        if self.truncated != bool(self.truncation_reasons):
            raise InvalidRequestError("evidence package truncation_reasons must be present exactly when truncated")
        encoded = _json_text(self.to_dict()).encode("utf-8")
        if len(encoded) > MAX_EVIDENCE_PACKAGE_BYTES:
            raise InvalidRequestError(f"evidence package exceeds the limit of {MAX_EVIDENCE_PACKAGE_BYTES} UTF-8 bytes")

    @staticmethod
    def _canonical_records(
        records: tuple[ClaimEvidenceRecord, ...],
    ) -> tuple[tuple[ClaimEvidenceRecord, ...], int]:
        if not isinstance(records, tuple) or not all(isinstance(record, ClaimEvidenceRecord) for record in records):
            raise InvalidRequestError("evidence package input must be a tuple of ClaimEvidenceRecord values")
        if len(records) > MAX_EVIDENCE_PACKAGE_INPUT_RECORDS:
            raise InvalidRequestError(f"evidence package input exceeds the limit of {MAX_EVIDENCE_PACKAGE_INPUT_RECORDS}")
        by_claim_id: dict[str, ClaimEvidenceRecord] = {}
        duplicate_count = 0
        for record in records:
            if record.claim_id in by_claim_id:
                previous = by_claim_id[record.claim_id]
                if previous != record:
                    raise InvalidRequestError(f"conflicting Claim evidence projections for Claim ID: {record.claim_id}")
                duplicate_count += 1
                continue
            by_claim_id[record.claim_id] = record
        return tuple(by_claim_id[claim_id] for claim_id in sorted(by_claim_id)), duplicate_count

    @classmethod
    def build(
        cls,
        records: tuple[ClaimEvidenceRecord, ...],
        *,
        max_records: int = MAX_EVIDENCE_PACKAGE_RECORDS,
        max_bytes: int = MAX_EVIDENCE_PACKAGE_BYTES,
    ) -> "EvidencePackage":
        retained_limit = _require_int(max_records, "evidence package max_records", 0, MAX_EVIDENCE_PACKAGE_RECORDS)
        byte_limit = _require_int(max_bytes, "evidence package max_bytes", 256, MAX_EVIDENCE_PACKAGE_BYTES)
        canonical, duplicate_count = cls._canonical_records(records)
        reasons: set[EvidencePackageTruncationReason] = set()
        omitted = duplicate_count
        if duplicate_count:
            reasons.add(EvidencePackageTruncationReason.DUPLICATE_CLAIM_ID)
        retained = canonical[:retained_limit]
        if len(canonical) > retained_limit:
            omitted += len(canonical) - retained_limit
            reasons.add(EvidencePackageTruncationReason.RECORD_LIMIT)

        while True:
            ordered_reasons = tuple(sorted(reasons, key=lambda reason: reason.value))
            payload = {
                "wire_version": 1,
                "records": [record.to_dict() for record in retained],
                "retained_count": len(retained),
                "omitted_count": omitted,
                "truncated": omitted > 0,
                "truncation_reasons": [reason.value for reason in ordered_reasons],
            }
            if len(_json_text(payload).encode("utf-8")) <= byte_limit:
                return cls(
                    records=retained,
                    retained_count=len(retained),
                    omitted_count=omitted,
                    truncated=omitted > 0,
                    truncation_reasons=ordered_reasons,
                )
            if not retained:
                raise InvalidRequestError("evidence package max_bytes cannot contain the empty package envelope")
            retained = retained[:-1]
            omitted += 1
            reasons.add(EvidencePackageTruncationReason.SERIALIZED_SIZE_LIMIT)

    def to_dict(self) -> dict[str, object]:
        return {
            "wire_version": self.wire_version,
            "records": [record.to_dict() for record in self.records],
            "retained_count": self.retained_count,
            "omitted_count": self.omitted_count,
            "truncated": self.truncated,
            "truncation_reasons": [reason.value for reason in self.truncation_reasons],
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> "EvidencePackage":
        data = _exact_mapping(
            value,
            "EvidencePackage",
            frozenset(
                {
                    "wire_version",
                    "records",
                    "retained_count",
                    "omitted_count",
                    "truncated",
                    "truncation_reasons",
                }
            ),
        )
        records = _require_list(data["records"], "evidence package records")
        if len(records) > MAX_EVIDENCE_PACKAGE_RECORDS:
            raise InvalidRequestError(f"evidence package records exceeds the limit of {MAX_EVIDENCE_PACKAGE_RECORDS}")
        raw_reasons = _require_list(data["truncation_reasons"], "evidence package truncation_reasons")
        reasons = []
        for reason_value in raw_reasons:
            try:
                reasons.append(
                    EvidencePackageTruncationReason(
                        _require_text(
                            reason_value,
                            "evidence package truncation reason",
                            MAX_REASON_CODE_BYTES,
                            allow_empty=False,
                        )
                    )
                )
            except ValueError as error:
                raise InvalidRequestError("unsupported evidence package truncation reason") from error
        return cls(
            wire_version=_require_int(data["wire_version"], "wire_version", 1, 1),
            records=tuple(ClaimEvidenceRecord.from_dict(_freeze_mapping(record, "evidence package record")) for record in records),
            retained_count=_require_int(data["retained_count"], "evidence package retained_count", 0, 2_147_483_647),
            omitted_count=_require_int(data["omitted_count"], "evidence package omitted_count", 0, 2_147_483_647),
            truncated=_require_bool(data["truncated"], "evidence package truncated"),
            truncation_reasons=tuple(reasons),
        )

    def to_json(self) -> str:
        return _json_text(self.to_dict())

    @classmethod
    def from_json(cls, value: str) -> "EvidencePackage":
        if not isinstance(value, str):
            raise InvalidRequestError("EvidencePackage JSON must be a string")
        if len(value.encode("utf-8")) > MAX_EVIDENCE_PACKAGE_BYTES:
            raise InvalidRequestError(f"evidence package exceeds the limit of {MAX_EVIDENCE_PACKAGE_BYTES} UTF-8 bytes")
        return cls.from_dict(_load_json_mapping(value, "EvidencePackage JSON"))


EMPTY_EVIDENCE_PACKAGE = EvidencePackage((), 0, 0, False, ())


@dataclass(frozen=True, slots=True)
class EvidenceReference:
    """Minimal stable evidence reference safe for Section 4 results."""

    evidence_id: str
    resolver: str
    kind: EvidenceKind
    scope: ScopeKey
    provenance: Mapping[str, object] = field(default_factory=lambda: MappingProxyType({}))
    diagnostics: Mapping[str, object] = field(default_factory=lambda: MappingProxyType({}))
    schema_version: int = EVIDENCE_REFERENCE_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != EVIDENCE_REFERENCE_SCHEMA_VERSION:
            raise InvalidRequestError(f"unsupported evidence reference schema_version: {self.schema_version}")
        _require_text(self.evidence_id, "evidence_id", 256, allow_empty=False)
        _require_text(self.resolver, "evidence resolver", MAX_RESOLVER_NAME_BYTES, allow_empty=False)
        if not isinstance(self.kind, EvidenceKind):
            raise InvalidRequestError("evidence kind must be an EvidenceKind")
        if not isinstance(self.scope, ScopeKey):
            raise InvalidRequestError("evidence scope must be a ScopeKey")
        object.__setattr__(self, "provenance", _freeze_mapping(self.provenance, "evidence provenance"))
        object.__setattr__(self, "diagnostics", _freeze_mapping(self.diagnostics, "evidence diagnostics"))

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "evidence_id": self.evidence_id,
            "resolver": self.resolver,
            "kind": self.kind.value,
            "scope": self.scope.to_dict(),
            "provenance": _thaw_json(self.provenance),
            "diagnostics": _thaw_json(self.diagnostics),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> "EvidenceReference":
        keys = frozenset({"schema_version", "evidence_id", "resolver", "kind", "scope", "provenance", "diagnostics"})
        data = _exact_mapping(value, "EvidenceReference", keys)
        try:
            kind = EvidenceKind(_require_text(data["kind"], "evidence kind", 32, allow_empty=False))
        except ValueError as error:
            raise InvalidRequestError("unsupported evidence kind") from error
        return cls(
            schema_version=_require_int(data["schema_version"], "schema_version", 1, 1),
            evidence_id=_require_text(data["evidence_id"], "evidence_id", 256, allow_empty=False),
            resolver=_require_text(data["resolver"], "evidence resolver", MAX_RESOLVER_NAME_BYTES, allow_empty=False),
            kind=kind,
            scope=ScopeKey.from_dict(_freeze_mapping(data["scope"], "evidence scope")),
            provenance=_freeze_mapping(data["provenance"], "evidence provenance"),
            diagnostics=_freeze_mapping(data["diagnostics"], "evidence diagnostics"),
        )

    def to_json(self) -> str:
        return _json_text(self.to_dict())

    @classmethod
    def from_json(cls, value: str) -> "EvidenceReference":
        return cls.from_dict(_load_json_mapping(value, "EvidenceReference JSON"))


@dataclass(frozen=True, slots=True)
class Candidate:
    """One response candidate emitted by a resolver without selecting it."""

    candidate_id: str
    statement_id: str
    response: str
    source: CandidateSource
    features: FeatureSet
    evidence: tuple[EvidenceReference, ...]
    scope: ScopeKey
    lifecycle: LifecycleState
    provenance: Mapping[str, object] = field(default_factory=lambda: MappingProxyType({}))
    diagnostics: Mapping[str, object] = field(default_factory=lambda: MappingProxyType({}))
    schema_version: int = CANDIDATE_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != CANDIDATE_SCHEMA_VERSION:
            raise InvalidRequestError(f"unsupported candidate schema_version: {self.schema_version}")
        _require_text(self.candidate_id, "candidate_id", 256, allow_empty=False)
        _require_text(self.statement_id, "candidate statement_id", 256, allow_empty=False)
        _require_text(self.response, "candidate response", 1_048_576, allow_empty=False)
        if not isinstance(self.source, CandidateSource):
            raise InvalidRequestError("candidate source must be a CandidateSource")
        if not isinstance(self.features, FeatureSet):
            raise InvalidRequestError("candidate features must be a FeatureSet")
        if not isinstance(self.evidence, tuple) or not all(isinstance(item, EvidenceReference) for item in self.evidence):
            raise InvalidRequestError("candidate evidence must be a tuple of EvidenceReference values")
        if len(self.evidence) > MAX_RESOLUTION_VALUES:
            raise InvalidRequestError(f"candidate evidence exceeds the limit of {MAX_RESOLUTION_VALUES}")
        if not isinstance(self.scope, ScopeKey):
            raise InvalidRequestError("candidate scope must be a ScopeKey")
        if not isinstance(self.lifecycle, LifecycleState):
            raise InvalidRequestError("candidate lifecycle must be a LifecycleState")
        object.__setattr__(self, "provenance", _freeze_mapping(self.provenance, "candidate provenance"))
        object.__setattr__(self, "diagnostics", _freeze_mapping(self.diagnostics, "candidate diagnostics"))

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "candidate_id": self.candidate_id,
            "statement_id": self.statement_id,
            "response": self.response,
            "source": self.source.value,
            "features": self.features.to_dict(),
            "evidence": [item.to_dict() for item in self.evidence],
            "scope": self.scope.to_dict(),
            "lifecycle": self.lifecycle.value,
            "provenance": _thaw_json(self.provenance),
            "diagnostics": _thaw_json(self.diagnostics),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> "Candidate":
        keys = frozenset(
            {
                "schema_version",
                "candidate_id",
                "statement_id",
                "response",
                "source",
                "features",
                "evidence",
                "scope",
                "lifecycle",
                "provenance",
                "diagnostics",
            }
        )
        data = _exact_mapping(value, "Candidate", keys)
        try:
            source = CandidateSource(_require_text(data["source"], "candidate source", 32, allow_empty=False))
            lifecycle = LifecycleState(_require_text(data["lifecycle"], "candidate lifecycle", 32, allow_empty=False))
        except ValueError as error:
            raise InvalidRequestError("candidate source or lifecycle is unsupported") from error
        evidence = _require_list(data["evidence"], "candidate evidence")
        return cls(
            schema_version=_require_int(data["schema_version"], "schema_version", 1, 1),
            candidate_id=_require_text(data["candidate_id"], "candidate_id", 256, allow_empty=False),
            statement_id=_require_text(data["statement_id"], "candidate statement_id", 256, allow_empty=False),
            response=_require_text(data["response"], "candidate response", 1_048_576, allow_empty=False),
            source=source,
            features=FeatureSet.from_dict(_freeze_mapping(data["features"], "candidate features")),
            evidence=tuple(EvidenceReference.from_dict(_freeze_mapping(item, "candidate evidence item")) for item in evidence),
            scope=ScopeKey.from_dict(_freeze_mapping(data["scope"], "candidate scope")),
            lifecycle=lifecycle,
            provenance=_freeze_mapping(data["provenance"], "candidate provenance"),
            diagnostics=_freeze_mapping(data["diagnostics"], "candidate diagnostics"),
        )

    def to_json(self) -> str:
        return _json_text(self.to_dict())

    @classmethod
    def from_json(cls, value: str) -> "Candidate":
        return cls.from_dict(_load_json_mapping(value, "Candidate JSON"))


@dataclass(frozen=True, slots=True)
class AccountingObservation:
    """Pure resolver observation applied only by the Section 4 finalizer."""

    statement_id: str
    keywords: tuple[str, ...] = ()
    schema_version: int = ACCOUNTING_OBSERVATION_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != ACCOUNTING_OBSERVATION_SCHEMA_VERSION:
            raise InvalidRequestError(f"unsupported accounting observation schema_version: {self.schema_version}")
        _require_text(self.statement_id, "accounting statement_id", 256, allow_empty=False)
        if not isinstance(self.keywords, tuple):
            raise InvalidRequestError("accounting keywords must be a tuple")
        if len(self.keywords) > 256:
            raise InvalidRequestError("accounting keywords exceeds the limit of 256")
        for keyword in self.keywords:
            _require_text(keyword, "accounting keyword", 256, allow_empty=False)
        if len(set(self.keywords)) != len(self.keywords):
            raise InvalidRequestError("accounting keywords must be unique")

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "statement_id": self.statement_id,
            "keywords": list(self.keywords),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> "AccountingObservation":
        data = _exact_mapping(
            value,
            "AccountingObservation",
            frozenset({"schema_version", "statement_id", "keywords"}),
        )
        keywords = _require_list(data["keywords"], "accounting keywords")
        return cls(
            schema_version=_require_int(data["schema_version"], "schema_version", 1, 1),
            statement_id=_require_text(data["statement_id"], "accounting statement_id", 256, allow_empty=False),
            keywords=tuple(_require_text(item, "accounting keyword", 256, allow_empty=False) for item in keywords),
        )


@dataclass(frozen=True, slots=True)
class ResolverResult:
    """Bounded typed output from one side-effect-free resolver invocation."""

    resolver: str
    state: ResolverState
    reason_code: str = ""
    candidates: tuple[Candidate, ...] = ()
    evidence: tuple[EvidenceReference, ...] = ()
    claim_evidence: tuple[ClaimEvidenceRecord, ...] = ()
    accounting: tuple[AccountingObservation, ...] = ()
    diagnostics: Mapping[str, object] = field(default_factory=lambda: MappingProxyType({}))
    consumption: BudgetConsumption = field(default_factory=BudgetConsumption)
    schema_version: int = RESOLVER_RESULT_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != RESOLVER_RESULT_SCHEMA_VERSION:
            raise InvalidRequestError(f"unsupported resolver result schema_version: {self.schema_version}")
        _require_text(self.resolver, "resolver result resolver", MAX_RESOLVER_NAME_BYTES, allow_empty=False)
        if not isinstance(self.state, ResolverState):
            raise InvalidRequestError("resolver result state must be a ResolverState")
        _require_text(self.reason_code, "resolver result reason_code", MAX_REASON_CODE_BYTES, allow_empty=True)
        if self.state != ResolverState.COMPLETED and (self.candidates or self.evidence or self.claim_evidence or self.accounting):
            raise InvalidRequestError("non-completed resolver results cannot contain output or accounting")
        if not isinstance(self.candidates, tuple) or not all(isinstance(value, Candidate) for value in self.candidates):
            raise InvalidRequestError("resolver candidates must be a tuple of Candidate values")
        if not isinstance(self.evidence, tuple) or not all(isinstance(value, EvidenceReference) for value in self.evidence):
            raise InvalidRequestError("resolver evidence must be a tuple of EvidenceReference values")
        if not isinstance(self.claim_evidence, tuple) or not all(
            isinstance(value, ClaimEvidenceRecord) for value in self.claim_evidence
        ):
            raise InvalidRequestError("resolver claim_evidence must be a tuple of ClaimEvidenceRecord values")
        if any(
            value.source_resolver != self.resolver or value.source_contributions != (self.resolver,)
            for value in self.claim_evidence
        ):
            raise InvalidRequestError("resolver claim_evidence source must match its producing resolver")
        if not isinstance(self.accounting, tuple) or not all(isinstance(value, AccountingObservation) for value in self.accounting):
            raise InvalidRequestError("resolver accounting must be a tuple of AccountingObservation values")
        if max(len(self.candidates), len(self.evidence), len(self.claim_evidence), len(self.accounting)) > MAX_RESOLUTION_VALUES:
            raise InvalidRequestError(f"resolver output exceeds the item limit of {MAX_RESOLUTION_VALUES}")
        object.__setattr__(self, "diagnostics", _freeze_mapping(self.diagnostics, "resolver diagnostics"))
        if not isinstance(self.consumption, BudgetConsumption):
            raise InvalidRequestError("resolver consumption must be a BudgetConsumption")

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "resolver": self.resolver,
            "state": self.state.value,
            "reason_code": self.reason_code,
            "candidates": [value.to_dict() for value in self.candidates],
            "evidence": [value.to_dict() for value in self.evidence],
            "claim_evidence": [value.to_dict() for value in self.claim_evidence],
            "accounting": [value.to_dict() for value in self.accounting],
            "diagnostics": _thaw_json(self.diagnostics),
            "consumption": self.consumption.to_dict(),
        }

    def to_json(self) -> str:
        return _json_text(self.to_dict())

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> "ResolverResult":
        data = _exact_mapping(
            value,
            "ResolverResult",
            frozenset(
                {
                    "schema_version",
                    "resolver",
                    "state",
                    "reason_code",
                    "candidates",
                    "evidence",
                    "claim_evidence",
                    "accounting",
                    "diagnostics",
                    "consumption",
                }
            ),
        )
        try:
            state = ResolverState(_require_text(data["state"], "resolver state", 32, allow_empty=False))
        except ValueError as error:
            raise InvalidRequestError("unsupported resolver state") from error
        candidates = _require_list(data["candidates"], "resolver candidates")
        evidence = _require_list(data["evidence"], "resolver evidence")
        claim_evidence = _require_list(data["claim_evidence"], "resolver Claim evidence")
        accounting = _require_list(data["accounting"], "resolver accounting")
        return cls(
            schema_version=_require_int(data["schema_version"], "schema_version", 1, 1),
            resolver=_require_text(data["resolver"], "resolver result resolver", MAX_RESOLVER_NAME_BYTES, allow_empty=False),
            state=state,
            reason_code=_require_text(data["reason_code"], "resolver result reason_code", MAX_REASON_CODE_BYTES, allow_empty=True),
            candidates=tuple(Candidate.from_dict(_freeze_mapping(item, "resolver candidate")) for item in candidates),
            evidence=tuple(EvidenceReference.from_dict(_freeze_mapping(item, "resolver evidence item")) for item in evidence),
            claim_evidence=tuple(
                ClaimEvidenceRecord.from_dict(_freeze_mapping(item, "resolver Claim evidence item")) for item in claim_evidence
            ),
            accounting=tuple(
                AccountingObservation.from_dict(_freeze_mapping(item, "resolver accounting item")) for item in accounting
            ),
            diagnostics=_freeze_mapping(data["diagnostics"], "resolver diagnostics"),
            consumption=BudgetConsumption.from_dict(_freeze_mapping(data["consumption"], "resolver consumption")),
        )

    @classmethod
    def from_json(cls, value: str) -> "ResolverResult":
        return cls.from_dict(_load_json_mapping(value, "ResolverResult JSON"))


@dataclass(frozen=True, slots=True)
class ResolutionResult:
    """Strict unified result with concrete ANSWER/EVIDENCE/MISS invariants."""

    outcome: ResolutionOutcome
    selected_candidate: Candidate
    selected_candidate_available: bool
    response_candidates: tuple[Candidate, ...]
    evidence: tuple[EvidenceReference, ...]
    confidence: float
    confidence_available: bool
    reason_codes: tuple[str, ...]
    frame_diagnostics: Mapping[str, object]
    resolver_results: tuple[ResolverResult, ...]
    budget: BudgetConsumption
    evidence_package_available: bool = False
    evidence_package: EvidencePackage = EMPTY_EVIDENCE_PACKAGE
    schema_version: int = RESOLUTION_RESULT_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != RESOLUTION_RESULT_SCHEMA_VERSION:
            raise InvalidRequestError(f"unsupported resolution result schema_version: {self.schema_version}")
        if not isinstance(self.outcome, ResolutionOutcome):
            raise InvalidRequestError("resolution outcome must be a ResolutionOutcome")
        if not isinstance(self.selected_candidate, Candidate):
            raise InvalidRequestError("selected_candidate must be a Candidate")
        if not isinstance(self.selected_candidate_available, bool):
            raise InvalidRequestError("selected_candidate_available must be a boolean")
        if not isinstance(self.response_candidates, tuple) or not all(
            isinstance(value, Candidate) for value in self.response_candidates
        ):
            raise InvalidRequestError("response_candidates must be a tuple of Candidate values")
        if not isinstance(self.evidence, tuple) or not all(isinstance(value, EvidenceReference) for value in self.evidence):
            raise InvalidRequestError("resolution evidence must be a tuple of EvidenceReference values")
        _require_float(self.confidence, "resolution confidence", 0.0, 1.0)
        if not isinstance(self.confidence_available, bool):
            raise InvalidRequestError("confidence_available must be a boolean")
        if not isinstance(self.reason_codes, tuple):
            raise InvalidRequestError("reason_codes must be a tuple")
        if len(self.reason_codes) > 64:
            raise InvalidRequestError("reason_codes exceeds the limit of 64")
        reason_codes = tuple(
            _require_text(value, "reason code", MAX_REASON_CODE_BYTES, allow_empty=False) for value in self.reason_codes
        )
        if reason_codes != tuple(dict.fromkeys(reason_codes)):
            raise InvalidRequestError("reason_codes must be unique and ordered")
        object.__setattr__(self, "frame_diagnostics", _freeze_mapping(self.frame_diagnostics, "frame diagnostics"))
        if not isinstance(self.resolver_results, tuple) or not all(
            isinstance(value, ResolverResult) for value in self.resolver_results
        ):
            raise InvalidRequestError("resolver_results must be a tuple of ResolverResult values")
        if len(self.response_candidates) > MAX_RESOLUTION_VALUES or len(self.evidence) > MAX_RESOLUTION_VALUES:
            raise InvalidRequestError(f"resolution output exceeds the item limit of {MAX_RESOLUTION_VALUES}")
        if len(self.resolver_results) > 64:
            raise InvalidRequestError("resolver_results exceeds the limit of 64")
        if any(result.claim_evidence for result in self.resolver_results):
            raise InvalidRequestError("resolution resolver_results cannot expose unpackaged Claim evidence")
        if not isinstance(self.budget, BudgetConsumption):
            raise InvalidRequestError("resolution budget must be a BudgetConsumption")
        if not isinstance(self.evidence_package_available, bool):
            raise InvalidRequestError("evidence_package_available must be a boolean")
        if not isinstance(self.evidence_package, EvidencePackage):
            raise InvalidRequestError("evidence_package must be an EvidencePackage")
        if not self.evidence_package_available and self.evidence_package != EMPTY_EVIDENCE_PACKAGE:
            raise InvalidRequestError("unavailable evidence_package must use the concrete empty package")
        if self.outcome == ResolutionOutcome.ANSWER:
            if not self.selected_candidate_available:
                raise InvalidRequestError("ANSWER requires one selected candidate")
            if self.response_candidates != (self.selected_candidate,):
                raise InvalidRequestError("ANSWER response_candidates must contain only the selected candidate")
            if self.evidence:
                raise InvalidRequestError("ANSWER cannot contain top-level evidence")
            if not self.confidence_available or self.confidence <= 0.0:
                raise InvalidRequestError("ANSWER requires available positive confidence")
            if self.evidence_package_available or self.evidence_package != EMPTY_EVIDENCE_PACKAGE:
                raise InvalidRequestError("ANSWER cannot contain a response-less evidence package")
        else:
            if self.selected_candidate_available:
                raise InvalidRequestError("only ANSWER can make selected_candidate available")
            if self.selected_candidate != EMPTY_CANDIDATE:
                raise InvalidRequestError("an unavailable selected_candidate must be EMPTY_CANDIDATE")
            if self.confidence_available or self.confidence != 0.0:
                raise InvalidRequestError("non-ANSWER confidence must be unavailable and zero")
        if self.outcome == ResolutionOutcome.EVIDENCE and not (
            self.response_candidates or self.evidence or self.evidence_package.records
        ):
            raise InvalidRequestError("EVIDENCE requires response candidates, evidence references, or package records")
        if self.outcome == ResolutionOutcome.MISS and (self.response_candidates or self.evidence or self.evidence_package.records):
            raise InvalidRequestError("MISS cannot contain response candidates or retained evidence")

    def to_dict(self) -> dict[str, object]:
        selected = self.selected_candidate.to_dict() if self.selected_candidate_available else {}
        return {
            "schema_version": self.schema_version,
            "outcome": self.outcome.value,
            "selected_candidate": selected,
            "selected_candidate_available": self.selected_candidate_available,
            "response_candidates": [value.to_dict() for value in self.response_candidates],
            "evidence": [value.to_dict() for value in self.evidence],
            "confidence": self.confidence,
            "confidence_available": self.confidence_available,
            "reason_codes": list(self.reason_codes),
            "frame_diagnostics": _thaw_json(self.frame_diagnostics),
            "resolver_results": [value.to_dict() for value in self.resolver_results],
            "budget": self.budget.to_dict(),
            "evidence_package_available": self.evidence_package_available,
            "evidence_package": self.evidence_package.to_dict(),
        }

    def to_json(self) -> str:
        return _json_text(self.to_dict())

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> "ResolutionResult":
        data = _exact_mapping(
            value,
            "ResolutionResult",
            frozenset(
                {
                    "schema_version",
                    "outcome",
                    "selected_candidate",
                    "selected_candidate_available",
                    "response_candidates",
                    "evidence",
                    "confidence",
                    "confidence_available",
                    "reason_codes",
                    "frame_diagnostics",
                    "resolver_results",
                    "budget",
                    "evidence_package_available",
                    "evidence_package",
                }
            ),
        )
        try:
            outcome = ResolutionOutcome(_require_text(data["outcome"], "resolution outcome", 32, allow_empty=False))
        except ValueError as error:
            raise InvalidRequestError("unsupported resolution outcome") from error
        if not isinstance(data["selected_candidate_available"], bool):
            raise InvalidRequestError("selected_candidate_available must be a boolean")
        selected_available = data["selected_candidate_available"]
        selected_mapping = _freeze_mapping(data["selected_candidate"], "selected_candidate")
        selected = Candidate.from_dict(selected_mapping) if selected_available else EMPTY_CANDIDATE
        if not selected_available and selected_mapping:
            raise InvalidRequestError("unavailable selected_candidate must be an empty object")
        if not isinstance(data["confidence_available"], bool):
            raise InvalidRequestError("confidence_available must be a boolean")
        response_candidates = _require_list(data["response_candidates"], "response_candidates")
        evidence = _require_list(data["evidence"], "resolution evidence")
        reasons = _require_list(data["reason_codes"], "reason_codes")
        resolver_results = _require_list(data["resolver_results"], "resolver_results")
        if not isinstance(data["evidence_package_available"], bool):
            raise InvalidRequestError("evidence_package_available must be a boolean")
        return cls(
            schema_version=_require_int(data["schema_version"], "schema_version", 1, 1),
            outcome=outcome,
            selected_candidate=selected,
            selected_candidate_available=selected_available,
            response_candidates=tuple(
                Candidate.from_dict(_freeze_mapping(item, "response candidate")) for item in response_candidates
            ),
            evidence=tuple(EvidenceReference.from_dict(_freeze_mapping(item, "resolution evidence item")) for item in evidence),
            confidence=_require_float(data["confidence"], "resolution confidence", 0.0, 1.0),
            confidence_available=data["confidence_available"],
            reason_codes=tuple(_require_text(item, "reason code", MAX_REASON_CODE_BYTES, allow_empty=False) for item in reasons),
            frame_diagnostics=_freeze_mapping(data["frame_diagnostics"], "frame diagnostics"),
            resolver_results=tuple(ResolverResult.from_dict(_freeze_mapping(item, "resolver result")) for item in resolver_results),
            budget=BudgetConsumption.from_dict(_freeze_mapping(data["budget"], "resolution budget")),
            evidence_package_available=data["evidence_package_available"],
            evidence_package=EvidencePackage.from_dict(_freeze_mapping(data["evidence_package"], "evidence package")),
        )

    @classmethod
    def from_json(cls, value: str) -> "ResolutionResult":
        return cls.from_dict(_load_json_mapping(value, "ResolutionResult JSON"))


EMPTY_SCOPE = ScopeKey()
EMPTY_CANDIDATE = Candidate(
    candidate_id="empty",
    statement_id="empty",
    response="empty",
    source=CandidateSource.EXACT,
    features=FeatureSet(),
    evidence=(),
    scope=EMPTY_SCOPE,
    lifecycle=LifecycleState.ACTIVE,
)


class QueryFrameBuilder:
    """Trusted transport-neutral base-frame construction boundary."""

    def __init__(self, engram, monotonic_clock_ns: Callable[[], int], utc_clock: Callable[[], datetime]) -> None:
        if not callable(monotonic_clock_ns) or not callable(utc_clock):
            raise InvalidRequestError("frame builder clocks must be callable")
        self._engram = engram
        self._monotonic_clock_ns = monotonic_clock_ns
        self._utc_clock = utc_clock

    def build(
        self,
        request: str,
        scope: ScopeKey = EMPTY_SCOPE,
        identity=(),
        required_metadata: Mapping[str, object] = MappingProxyType({}),
        required_source_label: str = "",
        diagnostic_seed: str = "",
        budget=(),
    ) -> QueryFrame:
        original = _require_text(request, "request", MAX_REQUEST_BYTES, allow_empty=False)
        if not isinstance(scope, ScopeKey):
            raise InvalidRequestError("scope must be a ScopeKey")
        if identity:
            if not isinstance(identity, QueryIdentity):
                raise InvalidRequestError("identity must be a QueryIdentity")
            selected_identity = identity
            validate_authoritative_identity(selected_identity, build_retrieval_representation(original))
        else:
            selected_identity = build_standalone_identity(original, scope)
        if selected_identity.scope != scope:
            raise InvalidRequestError("identity scope must match frame scope")
        if budget:
            if not isinstance(budget, ResolutionBudget):
                raise InvalidRequestError("budget must be a ResolutionBudget")
            selected_budget = budget.recapture(self._monotonic_clock_ns)
        else:
            selected_budget = ResolutionBudget.capture(self._monotonic_clock_ns)
        resolved = original
        if self._engram.config["expand_contractions"]:
            resolved = expand_contractions(original, self._engram.substitution_maps["contractions"])
        eligibility = EligibilityContextFactory(self._utc_clock, self._engram.namespace_epochs).capture_standalone(scope, True)
        seed = diagnostic_seed or f"{selected_identity.to_json()}:{resolved}"
        _require_text(seed, "diagnostic_seed", MAX_REQUEST_BYTES * 4, allow_empty=False)
        diagnostic_id = f"resolution:sha256:{hashlib.sha256(seed.encode('utf-8')).hexdigest()}"
        return QueryFrame(
            original_text=original,
            resolved_text=resolved,
            identity=selected_identity,
            expected_object_type=ExpectedObjectType.UNKNOWN,
            inheritance=(),
            rewrite_chain=(),
            scope=scope,
            required_metadata=required_metadata,
            required_source_label=required_source_label,
            budget=selected_budget,
            eligibility_context=eligibility,
            diagnostic_id=diagnostic_id,
        )


class BudgetLedger:
    """Thread-safe deterministic aggregate consumption and allowance checker."""

    def __init__(self, budget: ResolutionBudget, clock_ns: Callable[[], int]) -> None:
        if not isinstance(budget, ResolutionBudget):
            raise InvalidRequestError("ledger budget must be a ResolutionBudget")
        if not callable(clock_ns):
            raise InvalidRequestError("ledger clock_ns must be callable")
        self._budget = budget
        self._clock_ns = clock_ns
        self._lock = threading.RLock()
        self._totals = BudgetConsumption()

    @property
    def budget(self) -> ResolutionBudget:
        return self._budget

    def deadline_exhausted(self) -> bool:
        return bool(self._budget.deadline_ns and self._clock_ns() >= self._budget.deadline_ns)

    def remaining_candidates(self) -> int:
        with self._lock:
            return max(0, self._budget.max_candidates - self._totals.candidates)

    def remaining_evidence(self) -> int:
        with self._lock:
            return max(0, self._budget.max_evidence - self._totals.evidence)

    def add(self, consumption: BudgetConsumption) -> BudgetConsumption:
        if not isinstance(consumption, BudgetConsumption):
            raise InvalidRequestError("ledger consumption must be a BudgetConsumption")
        with self._lock:
            values = {}
            for name in (
                "elapsed_ns",
                "resolvers",
                "candidates",
                "graph_rows",
                "vector_results",
                "evidence",
                "evidence_bytes",
                "output_bytes",
                "diagnostic_bytes",
                "working_memory_bytes",
            ):
                values[name] = getattr(self._totals, name) + getattr(consumption, name)
            exhausted = set(self._totals.exhausted_dimensions).union(consumption.exhausted_dimensions)
            limits = {
                "resolvers": self._budget.max_resolvers,
                "candidates": self._budget.max_candidates,
                "graph_rows": self._budget.max_graph_rows,
                "vector_results": self._budget.max_vector_results,
                "evidence": self._budget.max_evidence,
                "evidence_bytes": self._budget.max_evidence_bytes,
                "output_bytes": self._budget.max_output_bytes,
                "diagnostic_bytes": self._budget.max_diagnostic_bytes,
                "working_memory_bytes": self._budget.max_working_memory_bytes,
            }
            for name, limit in limits.items():
                if values[name] > limit:
                    exhausted.add(name)
            if self.deadline_exhausted():
                exhausted.add("total_time")
            values["exhausted_dimensions"] = tuple(sorted(exhausted))
            values["measurement_available"] = self._totals.measurement_available and consumption.measurement_available
            self._totals = BudgetConsumption(**values)
            return self._totals

    def snapshot(self) -> BudgetConsumption:
        with self._lock:
            return self._totals
