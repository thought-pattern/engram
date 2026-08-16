"""Versioned candidate normalization, fusion, eligibility, and ambiguity policy."""

from __future__ import annotations

import hashlib
import json
import math
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from enum import StrEnum
from types import MappingProxyType, NoneType
from typing import Protocol

from engram.artifacts import LifecycleState
from engram.eligibility import EpochEligibilityPolicy, evaluate_artifact_eligibility
from engram.errors import InvalidRequestError
from engram.feedback import FeedbackStore, constraint_fingerprint
from engram.identity import ScopeKey
from engram.resolution import (
    EMPTY_CANDIDATE,
    Candidate,
    CandidateSource,
    EvidenceKind,
    EvidenceReference,
    ExpectedObjectType,
    FeatureSet,
    QueryFrame,
    ResolutionOutcome,
)

FUSION_POLICY_SCHEMA_VERSION = 1
NORMALIZED_FEATURE_SCHEMA_VERSION = 1
CANDIDATE_ELIGIBILITY_SCHEMA_VERSION = 1
FUSION_CONTRIBUTION_SCHEMA_VERSION = 1
FUSED_CANDIDATE_SCHEMA_VERSION = 1
FUSION_DECISION_SCHEMA_VERSION = 1
MAX_FUSION_CONTRIBUTIONS = 1_000
MAX_FUSION_REPORT_CANDIDATES = 8
MAX_FUSION_REPORT_CONTRIBUTIONS = 8
MAX_FUSION_REPORT_BYTES = 16_384
MAX_FUSION_REASON_CODES = 64


class FusionFeature(StrEnum):
    """Closed, comparable feature vocabulary for policy version 1."""

    EXACT = "exact"
    PATTERN = "pattern"
    LEXICAL = "lexical"
    SEMANTIC = "semantic"
    ENTITY = "entity"
    RELATION = "relation"
    OBJECT_TYPE = "object_type"
    SUPPORT = "support"
    HISTORY = "history"
    FRESHNESS = "freshness"
    AUTHORITY = "authority"
    AGREEMENT = "agreement"
    MARGIN = "margin"


class FusionFeatureRole(StrEnum):
    """Closed policy-stage role for one canonical feature."""

    SCORING = "scoring"
    SCORING_AND_GATE = "scoring_and_gate"
    DERIVED_SCORING = "derived_scoring"
    DECISION_ONLY = "decision_only"


class FusionPolicyReason(StrEnum):
    """Stable non-content-bearing outcome and eligibility reasons."""

    ANSWER_EXACT_ELIGIBLE = "answer_exact_eligible"
    ANSWER_FUSION_THRESHOLD = "answer_fusion_threshold"
    ANSWER_MARGIN_CLEAR = "answer_margin_clear"
    ANSWER_NO_RUNNER_UP = "answer_no_runner_up"
    AMBIGUOUS_TOP_CANDIDATES = "ambiguous_top_candidates"
    ANSWER_THRESHOLD_NOT_MET = "answer_threshold_not_met"
    ANSWER_ELIGIBILITY_PREVENTED = "answer_eligibility_prevented"
    EVIDENCE_THRESHOLD_MET = "evidence_threshold_met"
    GRAPH_EVIDENCE_AVAILABLE = "graph_evidence_available"
    EVIDENCE_THRESHOLD_NOT_MET = "evidence_threshold_not_met"
    NO_ELIGIBLE_CANDIDATES = "no_eligible_candidates"
    CANDIDATE_SCOPE_MISMATCH = "candidate_scope_mismatch"
    CANDIDATE_LIFECYCLE_INELIGIBLE = "candidate_lifecycle_ineligible"
    CANDIDATE_STATEMENT_CONFLICT = "candidate_statement_conflict"
    CANDIDATE_ID_CONFLICT = "candidate_id_conflict"
    CANDIDATE_EVIDENCE_SCOPE_MISMATCH = "candidate_evidence_scope_mismatch"
    EVIDENCE_REFERENCE_CONFLICT = "evidence_reference_conflict"
    AUTHORITATIVE_STATEMENT_MISSING = "authoritative_statement_missing"
    AUTHORITATIVE_RESPONSE_MISMATCH = "authoritative_response_mismatch"
    AUTHORITATIVE_GENERATION_MISMATCH = "authoritative_generation_mismatch"
    ARTIFACT_INELIGIBLE = "artifact_ineligible"
    REQUIRED_METADATA_MISMATCH = "required_metadata_mismatch"
    REQUIRED_SOURCE_MISMATCH = "required_source_mismatch"
    OWNERSHIP_VISIBILITY_MISMATCH = "ownership_visibility_mismatch"
    SUPPORT_INCOMPLETE = "support_incomplete"
    SUPPORT_REFERENCE_STALE = "support_reference_stale"
    INDEPENDENT_SOURCES_MISSING = "independent_sources_missing"
    IDENTITY_FEATURE_MISMATCH = "identity_feature_mismatch"
    OBJECT_TYPE_FEATURE_MISMATCH = "object_type_feature_mismatch"
    EXPLICIT_CONFLICT = "explicit_conflict"
    FUSION_DEADLINE_EXHAUSTED = "fusion_deadline_exhausted"
    FUSION_MEMORY_EXHAUSTED = "fusion_memory_exhausted"
    FEEDBACK_STALE_EXCLUDED = "feedback_stale_excluded"
    FEEDBACK_POLICY_SUPPRESSED = "feedback_policy_suppressed"


@dataclass(frozen=True, slots=True)
class FeatureDefinition:
    """Documentation-bearing definition for one normalized policy input."""

    feature: FusionFeature
    minimum: float
    maximum: float
    higher_is_better: bool
    meaning: str
    unavailable_meaning: str
    producer: str
    owner_section: str
    trust_boundary: str
    raw_range: str
    combination_rule: str
    role: FusionFeatureRole


FEATURE_DEFINITIONS: Mapping[FusionFeature, FeatureDefinition] = MappingProxyType(
    {
        FusionFeature.EXACT: FeatureDefinition(
            FusionFeature.EXACT,
            0.0,
            1.0,
            True,
            "scoped normalized request equality",
            "exact resolver did not measure equality",
            "ExactResolver.exact_match",
            "§§3-5",
            "exact source plus current authoritative artifact revalidation",
            "exact_match in [0, 1] from the exact source only",
            "maximum compatible exact observation",
            FusionFeatureRole.SCORING_AND_GATE,
        ),
        FusionFeature.PATTERN: FeatureDefinition(
            FusionFeature.PATTERN,
            0.0,
            1.0,
            True,
            "bounded pattern specificity",
            "no pattern contribution",
            "PatternResolver.pattern_specificity",
            "§§4-5",
            "pattern source only",
            "finite nonnegative pattern word count",
            "maximum compatible pattern observation",
            FusionFeatureRole.SCORING,
        ),
        FusionFeature.LEXICAL: FeatureDefinition(
            FusionFeature.LEXICAL,
            0.0,
            1.0,
            True,
            "calibrated lexical relevance",
            "no lexical contribution",
            "LexicalResolver.lexical_score or lexical_overlap",
            "§§4-5",
            "lexical source only",
            "finite lexical score in the resolver's documented scale",
            "maximum compatible lexical observation",
            FusionFeatureRole.SCORING,
        ),
        FusionFeature.SEMANTIC: FeatureDefinition(
            FusionFeature.SEMANTIC,
            0.0,
            1.0,
            True,
            "bounded semantic similarity",
            "no semantic model observation",
            "support or standalone semantic resolver semantic_score",
            "§§4-5, 13",
            "semantic candidate sources only",
            "finite similarity in [0, 1]",
            "maximum compatible semantic observation",
            FusionFeatureRole.SCORING,
        ),
        FusionFeature.ENTITY: FeatureDefinition(
            FusionFeature.ENTITY,
            0.0,
            1.0,
            True,
            "query/candidate entity identity agreement",
            "entity identity was not measured",
            "contextual resolver entity_match",
            "§8",
            "trusted contextual identity producer",
            "finite agreement in [0, 1]",
            "minimum available agreement so a mismatch cannot be hidden",
            FusionFeatureRole.SCORING_AND_GATE,
        ),
        FusionFeature.RELATION: FeatureDefinition(
            FusionFeature.RELATION,
            0.0,
            1.0,
            True,
            "query/candidate relation agreement",
            "relation identity was not measured",
            "contextual resolver relation_match",
            "§8",
            "trusted contextual identity producer",
            "finite agreement in [0, 1]",
            "minimum available agreement so a mismatch cannot be hidden",
            FusionFeatureRole.SCORING_AND_GATE,
        ),
        FusionFeature.OBJECT_TYPE: FeatureDefinition(
            FusionFeature.OBJECT_TYPE,
            0.0,
            1.0,
            True,
            "expected/candidate object-type agreement",
            "object type was not measured",
            "contextual resolver object_type_match",
            "§8",
            "trusted contextual type producer",
            "finite agreement in [0, 1]",
            "minimum available agreement so a mismatch cannot be hidden",
            FusionFeatureRole.SCORING_AND_GATE,
        ),
        FusionFeature.SUPPORT: FeatureDefinition(
            FusionFeature.SUPPORT,
            0.0,
            1.0,
            True,
            "presence and completeness of linked response support",
            "support was not inspected",
            "retained SUPPORT references plus current artifact support state",
            "§§3-5",
            "current authoritative artifact and scope-consistent evidence only",
            "binary presence or finite coverage in [0, 1]",
            "authoritative current state with minimum explicit coverage",
            FusionFeatureRole.SCORING_AND_GATE,
        ),
        FusionFeature.HISTORY: FeatureDefinition(
            FusionFeature.HISTORY,
            0.0,
            1.0,
            True,
            "bounded accepted-use history",
            "history was not observed",
            "current artifact statistics and later §6 feedback features",
            "§§3, 6",
            "authoritative statistics or versioned feedback producer; never statement priority",
            "finite rate in [0, 1]",
            "authoritative current observation",
            FusionFeatureRole.SCORING,
        ),
        FusionFeature.FRESHNESS: FeatureDefinition(
            FusionFeature.FRESHNESS,
            0.0,
            1.0,
            True,
            "bounded time-recency signal",
            "freshness was not observed",
            "current resolver recency",
            "§§4-5",
            "resolver observation after Section 3 validity remains a hard gate",
            "finite recency in [0, 1]",
            "minimum available freshness",
            FusionFeatureRole.SCORING,
        ),
        FusionFeature.AUTHORITY: FeatureDefinition(
            FusionFeature.AUTHORITY,
            0.0,
            1.0,
            True,
            "explicit source trust or authority input",
            "authority was not supplied",
            "explicit artifact metadata and later graph trust producer",
            "§§5, 9",
            "explicit finite trusted input only; source labels do not imply rank",
            "finite value in [0, 1]",
            "minimum available authority",
            FusionFeatureRole.SCORING,
        ),
        FusionFeature.AGREEMENT: FeatureDefinition(
            FusionFeature.AGREEMENT,
            0.0,
            1.0,
            True,
            "independent resolver-family agreement for one statement",
            "deduplication was not run",
            "Section 5 candidate grouping",
            "§5",
            "derived only from eligible configured resolver families",
            "distinct family count",
            "derived once after deduplication",
            FusionFeatureRole.DERIVED_SCORING,
        ),
        FusionFeature.MARGIN: FeatureDefinition(
            FusionFeature.MARGIN,
            0.0,
            1.0,
            True,
            "leading score minus runner-up score",
            "fewer than two score-eligible candidates",
            "Section 5 ranked distinct statements",
            "§5",
            "derived from the final deterministic ranking only",
            "difference between two bounded scores",
            "decision-only; never enters the score",
            FusionFeatureRole.DECISION_ONLY,
        ),
    }
)


def _bounded_unit(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
        raise InvalidRequestError("fusion feature values must be finite numbers")
    return min(1.0, max(0.0, float(value)))


def _policy_unit(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
        raise InvalidRequestError(f"{name} must be a finite number")
    selected = float(value)
    if not 0.0 <= selected <= 1.0:
        raise InvalidRequestError(f"{name} must be between 0 and 1")
    return selected


def _saturating(value: object, scale: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
        raise InvalidRequestError("resolver feature values must be finite numbers")
    bounded = max(0.0, float(value))
    return bounded / (bounded + scale) if bounded else 0.0


def _json_text(value: Mapping[str, object]) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def _load_mapping(value: str, name: str) -> Mapping[str, object]:
    if not isinstance(value, str):
        raise InvalidRequestError(f"{name} must be a string")
    try:
        decoded = json.loads(value)
    except json.JSONDecodeError as error:
        raise InvalidRequestError(f"{name} must be valid JSON") from error
    if not isinstance(decoded, Mapping):
        raise InvalidRequestError(f"{name} must contain an object")
    return decoded


def _exact_mapping(value: object, name: str, fields: frozenset[str]) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or frozenset(value) != fields or not all(isinstance(key, str) for key in value):
        raise InvalidRequestError(f"{name} has invalid fields")
    return value


def _enum_feature_mapping(value: object, name: str, *, complete: bool) -> dict[FusionFeature, float]:
    if not isinstance(value, Mapping) or not all(isinstance(key, str) for key in value):
        raise InvalidRequestError(f"{name} must contain an object")
    expected = frozenset(feature.value for feature in FusionFeature)
    if complete and frozenset(value) != expected:
        raise InvalidRequestError(f"{name} must contain every canonical feature")
    if not complete and not frozenset(value).issubset(expected):
        raise InvalidRequestError(f"{name} contains an unsupported feature")
    try:
        return {FusionFeature(key): _bounded_unit(item) for key, item in value.items()}
    except ValueError as error:
        raise InvalidRequestError(f"{name} contains an unsupported feature") from error


def _feature_tuple(value: object, name: str) -> tuple[FusionFeature, ...]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise InvalidRequestError(f"{name} must contain a list of feature names")
    try:
        return tuple(FusionFeature(item) for item in value)
    except ValueError as error:
        raise InvalidRequestError(f"{name} contains an unsupported feature") from error


def _weighted_feature_mapping(value: object, name: str) -> dict[FusionFeature, float]:
    if not isinstance(value, Mapping) or frozenset(value) != frozenset(feature.value for feature in FusionFeature):
        raise InvalidRequestError(f"{name} must contain every canonical feature")
    weighted: dict[FusionFeature, float] = {}
    for feature in FusionFeature:
        item = value[feature.value]
        if isinstance(item, bool) or not isinstance(item, (int, float)) or not math.isfinite(float(item)) or item < 0:
            raise InvalidRequestError(f"{name} values must be finite nonnegative numbers")
        weighted[feature] = float(item)
    return weighted


def _contains_none(value: object) -> bool:
    if isinstance(value, NoneType):
        return True
    if isinstance(value, Mapping):
        return any(_contains_none(key) or _contains_none(item) for key, item in value.items())
    if isinstance(value, (list, tuple)):
        return any(_contains_none(item) for item in value)
    return False


def _integer(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise InvalidRequestError(f"{name} must be an integer")
    return value


def _text(value: object, name: str) -> str:
    if not isinstance(value, str):
        raise InvalidRequestError(f"{name} must be a string")
    return value


def _boolean(value: object, name: str) -> bool:
    if not isinstance(value, bool):
        raise InvalidRequestError(f"{name} must be a boolean")
    return value


def _number(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
        raise InvalidRequestError(f"{name} must be a finite number")
    return float(value)


@dataclass(frozen=True, slots=True)
class NormalizedFeatureSet:
    """Concrete-zero feature vector with an explicit availability set."""

    values: Mapping[FusionFeature, float]
    available: tuple[FusionFeature, ...]
    schema_version: int = NORMALIZED_FEATURE_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != NORMALIZED_FEATURE_SCHEMA_VERSION:
            raise InvalidRequestError(f"unsupported normalized feature schema_version: {self.schema_version}")
        if not isinstance(self.values, Mapping) or set(self.values) != set(FusionFeature):
            raise InvalidRequestError("normalized features must contain every canonical feature")
        normalized = {feature: _bounded_unit(self.values[feature]) for feature in FusionFeature}
        if not isinstance(self.available, tuple) or not all(isinstance(value, FusionFeature) for value in self.available):
            raise InvalidRequestError("normalized feature availability must be a tuple of FusionFeature values")
        ordered = tuple(feature for feature in FusionFeature if feature in set(self.available))
        if ordered != self.available:
            raise InvalidRequestError("normalized feature availability must be unique and canonical-order sorted")
        object.__setattr__(self, "values", MappingProxyType(normalized))

    @classmethod
    def empty(cls) -> NormalizedFeatureSet:
        return cls(dict.fromkeys(FusionFeature, 0.0), ())

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "values": {feature.value: self.values[feature] for feature in FusionFeature},
            "available": [feature.value for feature in self.available],
        }

    def to_json(self) -> str:
        return _json_text(self.to_dict())

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> NormalizedFeatureSet:
        data = _exact_mapping(value, "NormalizedFeatureSet", frozenset({"schema_version", "values", "available"}))
        schema_version = data["schema_version"]
        if isinstance(schema_version, bool) or not isinstance(schema_version, int):
            raise InvalidRequestError("NormalizedFeatureSet schema_version must be an integer")
        return cls(
            values=_enum_feature_mapping(data["values"], "NormalizedFeatureSet values", complete=True),
            available=_feature_tuple(data["available"], "NormalizedFeatureSet available"),
            schema_version=schema_version,
        )

    @classmethod
    def from_json(cls, value: str) -> NormalizedFeatureSet:
        return cls.from_dict(_load_mapping(value, "NormalizedFeatureSet JSON"))


DEFAULT_FUSION_WEIGHTS: Mapping[FusionFeature, float] = MappingProxyType(
    {
        FusionFeature.EXACT: 1.0,
        FusionFeature.PATTERN: 0.75,
        FusionFeature.LEXICAL: 1.0,
        FusionFeature.SEMANTIC: 1.0,
        FusionFeature.ENTITY: 0.4,
        FusionFeature.RELATION: 0.5,
        FusionFeature.OBJECT_TYPE: 0.3,
        FusionFeature.SUPPORT: 0.8,
        FusionFeature.HISTORY: 0.2,
        FusionFeature.FRESHNESS: 0.2,
        FusionFeature.AUTHORITY: 0.5,
        FusionFeature.AGREEMENT: 0.9,
        FusionFeature.MARGIN: 0.0,
    }
)


@dataclass(frozen=True, slots=True)
class FusionPolicy:
    """Frozen, configurable first-generation linear fusion policy."""

    policy_version: str = "fusion-v1.0.0"
    formula_version: int = 1
    weights: Mapping[FusionFeature, float] = field(default_factory=lambda: DEFAULT_FUSION_WEIGHTS)
    answer_threshold: float = 0.78
    evidence_threshold: float = 0.35
    ambiguity_margin: float = 0.12
    minimum_independent_sources: int = 2
    require_support_for_non_exact: bool = True
    max_report_candidates: int = MAX_FUSION_REPORT_CANDIDATES
    schema_version: int = FUSION_POLICY_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != FUSION_POLICY_SCHEMA_VERSION:
            raise InvalidRequestError(f"unsupported fusion policy schema_version: {self.schema_version}")
        if not isinstance(self.policy_version, str) or not self.policy_version or len(self.policy_version) > 96:
            raise InvalidRequestError("fusion policy_version must be a bounded non-empty string")
        if self.formula_version != 1:
            raise InvalidRequestError("unsupported fusion formula_version")
        if not isinstance(self.weights, Mapping) or set(self.weights) != set(FusionFeature):
            raise InvalidRequestError("fusion weights must contain every canonical feature")
        weights = {}
        for feature in FusionFeature:
            value = self.weights[feature]
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)) or value < 0:
                raise InvalidRequestError("fusion weights must be finite nonnegative numbers")
            weights[feature] = float(value)
        if weights[FusionFeature.MARGIN] != 0.0:
            raise InvalidRequestError("margin is a decision feature and must have zero scoring weight")
        if not any(weight for feature, weight in weights.items() if feature != FusionFeature.MARGIN):
            raise InvalidRequestError("fusion policy must have a positive scoring weight")
        answer = _policy_unit(self.answer_threshold, "fusion answer_threshold")
        evidence = _policy_unit(self.evidence_threshold, "fusion evidence_threshold")
        margin = _policy_unit(self.ambiguity_margin, "fusion ambiguity_margin")
        if evidence > answer:
            raise InvalidRequestError("fusion evidence_threshold cannot exceed answer_threshold")
        if (
            isinstance(self.minimum_independent_sources, bool)
            or not isinstance(self.minimum_independent_sources, int)
            or not 2 <= self.minimum_independent_sources <= 6
        ):
            raise InvalidRequestError("minimum_independent_sources must be from 2 through 6")
        if not isinstance(self.require_support_for_non_exact, bool):
            raise InvalidRequestError("require_support_for_non_exact must be a boolean")
        if (
            isinstance(self.max_report_candidates, bool)
            or not isinstance(self.max_report_candidates, int)
            or not 1 <= self.max_report_candidates <= MAX_FUSION_REPORT_CANDIDATES
        ):
            raise InvalidRequestError(f"max_report_candidates must be from 1 through {MAX_FUSION_REPORT_CANDIDATES}")
        object.__setattr__(self, "weights", MappingProxyType(weights))
        object.__setattr__(self, "answer_threshold", answer)
        object.__setattr__(self, "evidence_threshold", evidence)
        object.__setattr__(self, "ambiguity_margin", margin)

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "policy_version": self.policy_version,
            "formula_version": self.formula_version,
            "weights": {feature.value: self.weights[feature] for feature in FusionFeature},
            "answer_threshold": self.answer_threshold,
            "evidence_threshold": self.evidence_threshold,
            "ambiguity_margin": self.ambiguity_margin,
            "minimum_independent_sources": self.minimum_independent_sources,
            "require_support_for_non_exact": self.require_support_for_non_exact,
            "max_report_candidates": self.max_report_candidates,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"), allow_nan=False)

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> FusionPolicy:
        expected = frozenset(
            {
                "schema_version",
                "policy_version",
                "formula_version",
                "weights",
                "answer_threshold",
                "evidence_threshold",
                "ambiguity_margin",
                "minimum_independent_sources",
                "require_support_for_non_exact",
                "max_report_candidates",
            }
        )
        if not isinstance(value, Mapping) or frozenset(value) != expected:
            raise InvalidRequestError("FusionPolicy has invalid fields")
        raw_weights = value["weights"]
        if not isinstance(raw_weights, Mapping) or frozenset(raw_weights) != frozenset(feature.value for feature in FusionFeature):
            raise InvalidRequestError("FusionPolicy weights have invalid fields")
        weights = {
            feature: _number(raw_weights[feature.value], f"FusionPolicy {feature.value} weight") for feature in FusionFeature
        }
        return cls(
            schema_version=_integer(value["schema_version"], "FusionPolicy schema_version"),
            policy_version=_text(value["policy_version"], "FusionPolicy policy_version"),
            formula_version=_integer(value["formula_version"], "FusionPolicy formula_version"),
            weights=weights,
            answer_threshold=_number(value["answer_threshold"], "FusionPolicy answer_threshold"),
            evidence_threshold=_number(value["evidence_threshold"], "FusionPolicy evidence_threshold"),
            ambiguity_margin=_number(value["ambiguity_margin"], "FusionPolicy ambiguity_margin"),
            minimum_independent_sources=_integer(value["minimum_independent_sources"], "FusionPolicy minimum_independent_sources"),
            require_support_for_non_exact=_boolean(
                value["require_support_for_non_exact"], "FusionPolicy require_support_for_non_exact"
            ),
            max_report_candidates=_integer(value["max_report_candidates"], "FusionPolicy max_report_candidates"),
        )

    @classmethod
    def from_json(cls, value: str) -> FusionPolicy:
        try:
            decoded = json.loads(value)
        except (TypeError, json.JSONDecodeError) as error:
            raise InvalidRequestError("FusionPolicy JSON must be valid JSON") from error
        if not isinstance(decoded, Mapping):
            raise InvalidRequestError("FusionPolicy JSON must contain an object")
        return cls.from_dict(decoded)


DEFAULT_FUSION_POLICY = FusionPolicy()


@dataclass(frozen=True, slots=True)
class CandidateEligibility:
    """Separate score/evidence/direct-answer eligibility for one statement."""

    score_eligible: bool = True
    evidence_eligible: bool = True
    answer_eligible: bool = True
    reason_codes: tuple[FusionPolicyReason, ...] = ()
    feature_values: Mapping[FusionFeature, float] = field(default_factory=lambda: MappingProxyType({}))
    feature_available: tuple[FusionFeature, ...] = ()
    schema_version: int = CANDIDATE_ELIGIBILITY_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != CANDIDATE_ELIGIBILITY_SCHEMA_VERSION:
            raise InvalidRequestError(f"unsupported candidate eligibility schema_version: {self.schema_version}")
        if not all(isinstance(value, bool) for value in (self.score_eligible, self.evidence_eligible, self.answer_eligible)):
            raise InvalidRequestError("candidate eligibility flags must be booleans")
        if self.answer_eligible and not self.score_eligible:
            raise InvalidRequestError("an answer-eligible candidate must be score eligible")
        if not isinstance(self.reason_codes, tuple) or not all(
            isinstance(value, FusionPolicyReason) for value in self.reason_codes
        ):
            raise InvalidRequestError("candidate eligibility reasons must use FusionPolicyReason")
        if tuple(dict.fromkeys(self.reason_codes)) != self.reason_codes:
            raise InvalidRequestError("candidate eligibility reasons must be unique and ordered")
        if not isinstance(self.feature_values, Mapping):
            raise InvalidRequestError("eligibility feature values must be an object")
        values = {}
        for feature, value in self.feature_values.items():
            if not isinstance(feature, FusionFeature):
                raise InvalidRequestError("eligibility feature keys must be FusionFeature values")
            values[feature] = _bounded_unit(value)
        if not isinstance(self.feature_available, tuple) or not all(
            isinstance(value, FusionFeature) for value in self.feature_available
        ):
            raise InvalidRequestError("eligibility feature availability must use FusionFeature")
        if any(feature not in values for feature in self.feature_available):
            raise InvalidRequestError("available eligibility features must have values")
        if tuple(feature for feature in FusionFeature if feature in set(self.feature_available)) != self.feature_available:
            raise InvalidRequestError("eligibility feature availability must be unique and canonical-order sorted")
        object.__setattr__(self, "feature_values", MappingProxyType(values))

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "score_eligible": self.score_eligible,
            "evidence_eligible": self.evidence_eligible,
            "answer_eligible": self.answer_eligible,
            "reason_codes": [reason.value for reason in self.reason_codes],
            "feature_values": {feature.value: self.feature_values[feature] for feature in self.feature_values},
            "feature_available": [feature.value for feature in self.feature_available],
        }

    def to_json(self) -> str:
        return _json_text(self.to_dict())

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> CandidateEligibility:
        fields = frozenset(
            {
                "schema_version",
                "score_eligible",
                "evidence_eligible",
                "answer_eligible",
                "reason_codes",
                "feature_values",
                "feature_available",
            }
        )
        data = _exact_mapping(value, "CandidateEligibility", fields)
        schema_version = data["schema_version"]
        score_eligible = data["score_eligible"]
        evidence_eligible = data["evidence_eligible"]
        answer_eligible = data["answer_eligible"]
        if isinstance(schema_version, bool) or not isinstance(schema_version, int):
            raise InvalidRequestError("CandidateEligibility schema_version must be an integer")
        if not isinstance(score_eligible, bool) or not isinstance(evidence_eligible, bool) or not isinstance(answer_eligible, bool):
            raise InvalidRequestError("CandidateEligibility flags must be booleans")
        raw_reasons = data["reason_codes"]
        if not isinstance(raw_reasons, list) or not all(isinstance(item, str) for item in raw_reasons):
            raise InvalidRequestError("CandidateEligibility reason_codes must contain a list of strings")
        try:
            reasons = tuple(FusionPolicyReason(item) for item in raw_reasons)
        except ValueError as error:
            raise InvalidRequestError("CandidateEligibility contains an unsupported reason code") from error
        return cls(
            score_eligible=score_eligible,
            evidence_eligible=evidence_eligible,
            answer_eligible=answer_eligible,
            reason_codes=reasons,
            feature_values=_enum_feature_mapping(data["feature_values"], "CandidateEligibility feature_values", complete=False),
            feature_available=_feature_tuple(data["feature_available"], "CandidateEligibility feature_available"),
            schema_version=schema_version,
        )

    @classmethod
    def from_json(cls, value: str) -> CandidateEligibility:
        return cls.from_dict(_load_mapping(value, "CandidateEligibility JSON"))


class CandidateAuthority(Protocol):
    """Trusted current-state revalidation boundary used before answer scoring."""

    def evaluate(self, candidate: Candidate, frame: QueryFrame) -> CandidateEligibility: ...


class PermissiveCandidateAuthority:
    """Explicit conformance-only authority for isolated deterministic fixtures."""

    def evaluate(self, candidate: Candidate, frame: QueryFrame) -> CandidateEligibility:
        del candidate, frame
        return CandidateEligibility()


PERMISSIVE_CANDIDATE_AUTHORITY = PermissiveCandidateAuthority()


class AbstainingCandidateAuthority:
    """Safe default when no authoritative current-state boundary was supplied."""

    def evaluate(self, candidate: Candidate, frame: QueryFrame) -> CandidateEligibility:
        del candidate, frame
        return CandidateEligibility(
            False,
            False,
            False,
            (FusionPolicyReason.AUTHORITATIVE_STATEMENT_MISSING,),
        )


ABSTAINING_CANDIDATE_AUTHORITY = AbstainingCandidateAuthority()


class EngramCandidateAuthority:
    """Revalidate artifact and legacy candidates against current authoritative state."""

    def __init__(self, engram, feedback_store: object = (), policy_fingerprint_value: str = "") -> None:
        self._engram = engram
        if feedback_store != () and not isinstance(feedback_store, FeedbackStore):
            raise InvalidRequestError("candidate feedback_store must be FeedbackStore")
        if feedback_store != () and (
            len(policy_fingerprint_value) != 64 or any(c not in "0123456789abcdef" for c in policy_fingerprint_value)
        ):
            raise InvalidRequestError("candidate feedback policy fingerprint must be lowercase SHA-256")
        self._feedback_store = feedback_store
        self._policy_fingerprint = policy_fingerprint_value

    @staticmethod
    def _visibility_allowed(metadata: Mapping[str, object], scope: ScopeKey) -> bool:
        visibility = metadata.get("visibility", "")
        if visibility in ("", "public", "scope"):
            return True
        if visibility == "context":
            owner = metadata.get("owner_context_fingerprint", "")
            return isinstance(owner, str) and bool(owner) and owner == scope.context_fingerprint
        return False

    def evaluate(self, candidate: Candidate, frame: QueryFrame) -> CandidateEligibility:
        snapshot = self._engram.response_repository.snapshot()
        if candidate.statement_id in snapshot.artifacts:
            artifact = snapshot.artifacts[candidate.statement_id]
            if isinstance(self._feedback_store, FeedbackStore):
                if self._feedback_store.stale_excluded(artifact.statement_id, artifact.generation):
                    return CandidateEligibility(False, False, False, (FusionPolicyReason.FEEDBACK_STALE_EXCLUDED,))
                if self._feedback_store.policy_suppressed(
                    artifact.statement_id,
                    frame.scope.namespace,
                    self._policy_fingerprint,
                ):
                    return CandidateEligibility(False, False, False, (FusionPolicyReason.FEEDBACK_POLICY_SUPPRESSED,))
            decision = evaluate_artifact_eligibility(
                artifact,
                frame.eligibility_context,
                EpochEligibilityPolicy.MATCH_WHEN_ARTIFACT_AVAILABLE,
            )
            if not decision.direct_answer_eligible:
                return CandidateEligibility(
                    False,
                    False,
                    False,
                    (FusionPolicyReason.ARTIFACT_INELIGIBLE,),
                )
            if artifact.scope != frame.scope:
                return CandidateEligibility(False, False, False, (FusionPolicyReason.CANDIDATE_SCOPE_MISMATCH,))
            if artifact.response != candidate.response:
                return CandidateEligibility(False, False, False, (FusionPolicyReason.AUTHORITATIVE_RESPONSE_MISMATCH,))
            if "generation" in candidate.provenance:
                candidate_generation = candidate.provenance["generation"]
                if (
                    isinstance(candidate_generation, bool)
                    or not isinstance(candidate_generation, int)
                    or candidate_generation != artifact.generation
                ):
                    return CandidateEligibility(
                        False,
                        False,
                        False,
                        (FusionPolicyReason.AUTHORITATIVE_GENERATION_MISMATCH,),
                    )
            if frame.required_source_label and artifact.provenance.source_label != frame.required_source_label:
                return CandidateEligibility(False, False, False, (FusionPolicyReason.REQUIRED_SOURCE_MISMATCH,))
            if any(
                key not in artifact.metadata or artifact.metadata[key] != value for key, value in frame.required_metadata.items()
            ):
                return CandidateEligibility(False, False, False, (FusionPolicyReason.REQUIRED_METADATA_MISMATCH,))
            if not self._visibility_allowed(artifact.metadata, frame.scope):
                return CandidateEligibility(False, False, False, (FusionPolicyReason.OWNERSHIP_VISIBILITY_MISMATCH,))
            feature_values: dict[FusionFeature, float] = {
                FusionFeature.SUPPORT: float(bool(artifact.support_claim_ids)),
            }
            feature_available = [FusionFeature.SUPPORT]
            if artifact.statistics.query_count:
                feature_values[FusionFeature.HISTORY] = artifact.statistics.hit_count / artifact.statistics.query_count
                feature_available.append(FusionFeature.HISTORY)
            if isinstance(self._feedback_store, FeedbackStore):
                feedback_history = self._feedback_store.history(
                    frame.identity,
                    constraint_fingerprint(
                        frame.expected_object_type.value,
                        frame.required_metadata,
                        frame.required_source_label,
                    ),
                    artifact.statement_id,
                    artifact.generation,
                    self._policy_fingerprint,
                    frame.eligibility_context.evaluation_time,
                )
                if feedback_history.available:
                    if FusionFeature.HISTORY in feature_available:
                        feature_values[FusionFeature.HISTORY] = (
                            feature_values[FusionFeature.HISTORY] + feedback_history.value
                        ) / 2.0
                    else:
                        feature_values[FusionFeature.HISTORY] = feedback_history.value
                        feature_available.append(FusionFeature.HISTORY)
            authority = artifact.metadata.get("authority")
            if (
                isinstance(authority, (int, float))
                and not isinstance(authority, bool)
                and math.isfinite(float(authority))
                and 0.0 <= float(authority) <= 1.0
            ):
                feature_values[FusionFeature.AUTHORITY] = float(authority)
                feature_available.append(FusionFeature.AUTHORITY)
            support_complete = artifact.metadata.get("support_complete", True)
            answer_eligible = isinstance(support_complete, bool) and support_complete
            reasons: tuple[FusionPolicyReason, ...] = () if answer_eligible else (FusionPolicyReason.SUPPORT_INCOMPLETE,)
            retained_support = tuple(
                reference.evidence_id for reference in candidate.evidence if reference.kind == EvidenceKind.SUPPORT
            )
            if any(reference_id not in artifact.support_claim_ids for reference_id in retained_support):
                answer_eligible = False
                reasons = tuple(dict.fromkeys((*reasons, FusionPolicyReason.SUPPORT_REFERENCE_STALE)))
            return CandidateEligibility(
                True,
                True,
                answer_eligible,
                reasons,
                feature_values,
                tuple(feature for feature in FusionFeature if feature in feature_available),
            )
        if isinstance(self._feedback_store, FeedbackStore):
            if self._feedback_store.stale_excluded(candidate.statement_id, 0, False):
                return CandidateEligibility(False, False, False, (FusionPolicyReason.FEEDBACK_STALE_EXCLUDED,))
            if self._feedback_store.policy_suppressed(
                candidate.statement_id,
                frame.scope.namespace,
                self._policy_fingerprint,
            ):
                return CandidateEligibility(False, False, False, (FusionPolicyReason.FEEDBACK_POLICY_SUPPRESSED,))
        statement = self._engram.get_statement(candidate.statement_id)
        if not statement:
            return CandidateEligibility(False, False, False, (FusionPolicyReason.AUTHORITATIVE_STATEMENT_MISSING,))
        if statement.get("text", "") != candidate.response:
            return CandidateEligibility(False, False, False, (FusionPolicyReason.AUTHORITATIVE_RESPONSE_MISMATCH,))
        template = statement.get("template", {})
        template = template if isinstance(template, Mapping) else MappingProxyType({})
        tapestry = template.get("tapestry", {})
        tapestry = tapestry if isinstance(tapestry, Mapping) else MappingProxyType({})
        metadata = tapestry.get("metadata", {})
        metadata = metadata if isinstance(metadata, Mapping) else MappingProxyType({})
        namespace = tapestry.get("namespace", "")
        context_fingerprint = tapestry.get("context_fingerprint", "")
        if (namespace or context_fingerprint) and (namespace, context_fingerprint) != (
            frame.scope.namespace,
            frame.scope.context_fingerprint,
        ):
            return CandidateEligibility(False, False, False, (FusionPolicyReason.CANDIDATE_SCOPE_MISMATCH,))
        response_artifact = template.get("response_artifact", {})
        if isinstance(response_artifact, Mapping) and response_artifact.get("lifecycle", "ACTIVE") != "ACTIVE":
            return CandidateEligibility(False, False, False, (FusionPolicyReason.CANDIDATE_LIFECYCLE_INELIGIBLE,))
        source_label = statement.get("source_label", "")
        if frame.required_source_label and source_label != frame.required_source_label:
            return CandidateEligibility(False, False, False, (FusionPolicyReason.REQUIRED_SOURCE_MISMATCH,))
        if any(key not in metadata or metadata[key] != value for key, value in frame.required_metadata.items()):
            return CandidateEligibility(False, False, False, (FusionPolicyReason.REQUIRED_METADATA_MISMATCH,))
        if not self._visibility_allowed(metadata, frame.scope):
            return CandidateEligibility(False, False, False, (FusionPolicyReason.OWNERSHIP_VISIBILITY_MISMATCH,))
        return CandidateEligibility()


@dataclass(frozen=True, slots=True)
class FusionContribution:
    """One retained raw resolver proposal plus its normalized projection."""

    candidate: Candidate
    normalized: NormalizedFeatureSet
    eligibility: CandidateEligibility = field(default_factory=CandidateEligibility)
    schema_version: int = FUSION_CONTRIBUTION_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != FUSION_CONTRIBUTION_SCHEMA_VERSION:
            raise InvalidRequestError(f"unsupported fusion contribution schema_version: {self.schema_version}")
        if not isinstance(self.candidate, Candidate):
            raise InvalidRequestError("fusion contribution candidate must be a Candidate")
        if not isinstance(self.normalized, NormalizedFeatureSet):
            raise InvalidRequestError("fusion contribution normalized value must be a NormalizedFeatureSet")
        if not isinstance(self.eligibility, CandidateEligibility):
            raise InvalidRequestError("fusion contribution eligibility must be a CandidateEligibility")

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "candidate": self.candidate.to_dict(),
            "normalized": self.normalized.to_dict(),
            "eligibility": self.eligibility.to_dict(),
        }

    def to_report_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "candidate_id": self.candidate.candidate_id,
            "source": self.candidate.source.value,
            "raw_features": self.candidate.features.to_dict(),
            "normalized_features": self.normalized.to_dict(),
            "eligibility": self.eligibility.to_dict(),
            "diagnostic_fields": sorted(self.candidate.diagnostics),
            "evidence_ids": [reference.evidence_id for reference in self.candidate.evidence],
        }

    def to_json(self) -> str:
        return _json_text(self.to_dict())

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> FusionContribution:
        fields = frozenset({"schema_version", "candidate", "normalized", "eligibility"})
        data = _exact_mapping(value, "FusionContribution", fields)
        schema_version = data["schema_version"]
        if isinstance(schema_version, bool) or not isinstance(schema_version, int):
            raise InvalidRequestError("FusionContribution schema_version must be an integer")
        candidate_value = data["candidate"]
        normalized_value = data["normalized"]
        eligibility_value = data["eligibility"]
        if not isinstance(candidate_value, Mapping):
            raise InvalidRequestError("FusionContribution candidate must contain an object")
        if not isinstance(normalized_value, Mapping) or not isinstance(eligibility_value, Mapping):
            raise InvalidRequestError("FusionContribution nested contracts must contain objects")
        return cls(
            candidate=Candidate.from_dict(candidate_value),
            normalized=NormalizedFeatureSet.from_dict(normalized_value),
            eligibility=CandidateEligibility.from_dict(eligibility_value),
            schema_version=schema_version,
        )

    @classmethod
    def from_json(cls, value: str) -> FusionContribution:
        return cls.from_dict(_load_mapping(value, "FusionContribution JSON"))


@dataclass(frozen=True, slots=True)
class FusedCandidate:
    """Deduplicated statement with every contribution and inspectable score."""

    candidate: Candidate
    contributions: tuple[FusionContribution, ...]
    normalized: NormalizedFeatureSet
    score: float
    score_contributions: Mapping[FusionFeature, float]
    eligibility: CandidateEligibility
    schema_version: int = FUSED_CANDIDATE_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != FUSED_CANDIDATE_SCHEMA_VERSION:
            raise InvalidRequestError(f"unsupported fused candidate schema_version: {self.schema_version}")
        _bounded_unit(self.score)
        if not self.contributions or len(self.contributions) > MAX_FUSION_CONTRIBUTIONS:
            raise InvalidRequestError("fused candidate contribution count is out of bounds")
        if not all(isinstance(value, FusionContribution) for value in self.contributions):
            raise InvalidRequestError("fused candidate contributions must use FusionContribution")
        if any(value.candidate.statement_id != self.candidate.statement_id for value in self.contributions):
            raise InvalidRequestError("fused candidate contributions must share one statement_id")
        if not isinstance(self.normalized, NormalizedFeatureSet):
            raise InvalidRequestError("fused candidate normalized value must be a NormalizedFeatureSet")
        if not isinstance(self.eligibility, CandidateEligibility):
            raise InvalidRequestError("fused candidate eligibility must be a CandidateEligibility")
        if not isinstance(self.score_contributions, Mapping) or set(self.score_contributions) != set(FusionFeature):
            raise InvalidRequestError("score contributions must contain every canonical feature")
        if any(
            isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)) or value < 0
            for value in self.score_contributions.values()
        ):
            raise InvalidRequestError("score contributions must be finite nonnegative numbers")
        object.__setattr__(
            self,
            "score_contributions",
            MappingProxyType({feature: float(self.score_contributions[feature]) for feature in FusionFeature}),
        )

    @property
    def sources(self) -> tuple[CandidateSource, ...]:
        return tuple(
            dict.fromkeys(
                contribution.candidate.source for contribution in self.contributions if contribution.eligibility.score_eligible
            )
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "candidate": self.candidate.to_dict(),
            "contributions": [contribution.to_dict() for contribution in self.contributions],
            "normalized": self.normalized.to_dict(),
            "score": self.score,
            "score_contributions": {feature.value: self.score_contributions[feature] for feature in FusionFeature},
            "eligibility": self.eligibility.to_dict(),
        }

    def to_report_dict(self) -> dict[str, object]:
        visible = self.contributions[:MAX_FUSION_REPORT_CONTRIBUTIONS]
        return {
            "statement_id": self.candidate.statement_id,
            "candidate_id": self.candidate.candidate_id,
            "sources": [source.value for source in self.sources],
            "score": self.score,
            "normalized_features": self.normalized.to_dict(),
            "score_contributions": {feature.value: self.score_contributions[feature] for feature in FusionFeature},
            "eligibility": {
                "score": self.eligibility.score_eligible,
                "evidence": self.eligibility.evidence_eligible,
                "answer": self.eligibility.answer_eligible,
                "reason_codes": [reason.value for reason in self.eligibility.reason_codes],
            },
            "contributions": [contribution.to_report_dict() for contribution in visible],
            "omitted_contribution_count": len(self.contributions) - len(visible),
        }

    def to_json(self) -> str:
        return _json_text(self.to_dict())

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> FusedCandidate:
        fields = frozenset(
            {
                "schema_version",
                "candidate",
                "contributions",
                "normalized",
                "score",
                "score_contributions",
                "eligibility",
            }
        )
        data = _exact_mapping(value, "FusedCandidate", fields)
        schema_version = data["schema_version"]
        if isinstance(schema_version, bool) or not isinstance(schema_version, int):
            raise InvalidRequestError("FusedCandidate schema_version must be an integer")
        candidate_value = data["candidate"]
        normalized_value = data["normalized"]
        eligibility_value = data["eligibility"]
        contribution_values = data["contributions"]
        if not isinstance(candidate_value, Mapping):
            raise InvalidRequestError("FusedCandidate candidate must contain an object")
        if not isinstance(normalized_value, Mapping) or not isinstance(eligibility_value, Mapping):
            raise InvalidRequestError("FusedCandidate nested contracts must contain objects")
        if not isinstance(contribution_values, list) or not all(isinstance(item, Mapping) for item in contribution_values):
            raise InvalidRequestError("FusedCandidate contributions must contain a list of objects")
        score = data["score"]
        if isinstance(score, bool) or not isinstance(score, (int, float)):
            raise InvalidRequestError("FusedCandidate score must be a number")
        return cls(
            candidate=Candidate.from_dict(candidate_value),
            contributions=tuple(FusionContribution.from_dict(item) for item in contribution_values),
            normalized=NormalizedFeatureSet.from_dict(normalized_value),
            score=float(score),
            score_contributions=_weighted_feature_mapping(data["score_contributions"], "FusedCandidate score_contributions"),
            eligibility=CandidateEligibility.from_dict(eligibility_value),
            schema_version=schema_version,
        )

    @classmethod
    def from_json(cls, value: str) -> FusedCandidate:
        return cls.from_dict(_load_mapping(value, "FusedCandidate JSON"))


@dataclass(frozen=True, slots=True)
class FusionDecision:
    """Policy result consumed by the transport-neutral resolution envelope."""

    outcome: ResolutionOutcome
    selected_candidate: Candidate = EMPTY_CANDIDATE
    selected_candidate_available: bool = False
    response_candidates: tuple[Candidate, ...] = ()
    evidence: tuple[EvidenceReference, ...] = ()
    confidence: float = 0.0
    confidence_available: bool = False
    reason_codes: tuple[str, ...] = ()
    report: Mapping[str, object] = field(default_factory=lambda: MappingProxyType({}))
    working_memory_bytes: int = 0
    schema_version: int = FUSION_DECISION_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != FUSION_DECISION_SCHEMA_VERSION:
            raise InvalidRequestError(f"unsupported fusion decision schema_version: {self.schema_version}")
        if not isinstance(self.outcome, ResolutionOutcome):
            raise InvalidRequestError("fusion decision outcome must be a ResolutionOutcome")
        if not isinstance(self.selected_candidate, Candidate):
            raise InvalidRequestError("fusion selected_candidate must be a Candidate")
        if not isinstance(self.selected_candidate_available, bool):
            raise InvalidRequestError("fusion selected_candidate_available must be a boolean")
        if not isinstance(self.response_candidates, tuple) or not all(
            isinstance(value, Candidate) for value in self.response_candidates
        ):
            raise InvalidRequestError("fusion response_candidates must be a tuple of Candidate values")
        if len(self.response_candidates) > MAX_FUSION_CONTRIBUTIONS:
            raise InvalidRequestError("fusion response_candidates exceed the candidate limit")
        if not isinstance(self.evidence, tuple) or not all(isinstance(value, EvidenceReference) for value in self.evidence):
            raise InvalidRequestError("fusion evidence must be a tuple of EvidenceReference values")
        if len(self.evidence) > MAX_FUSION_CONTRIBUTIONS:
            raise InvalidRequestError("fusion evidence exceeds the evidence limit")
        _bounded_unit(self.confidence)
        if not isinstance(self.confidence_available, bool):
            raise InvalidRequestError("fusion confidence_available must be a boolean")
        if not isinstance(self.reason_codes, tuple) or not 1 <= len(self.reason_codes) <= MAX_FUSION_REASON_CODES:
            raise InvalidRequestError("fusion reason_codes must be a bounded non-empty tuple")
        if tuple(dict.fromkeys(self.reason_codes)) != self.reason_codes:
            raise InvalidRequestError("fusion reason_codes must be unique and ordered")
        try:
            tuple(FusionPolicyReason(value) for value in self.reason_codes)
        except (TypeError, ValueError) as error:
            raise InvalidRequestError("fusion decision contains an unsupported reason code") from error
        if not isinstance(self.report, Mapping) or _contains_none(self.report):
            raise InvalidRequestError("fusion report must be a concrete no-null object")
        try:
            report_size = len(_json_text(dict(self.report)).encode("utf-8"))
        except (TypeError, ValueError) as error:
            raise InvalidRequestError("fusion report must contain deterministic JSON values") from error
        if report_size > MAX_FUSION_REPORT_BYTES:
            raise InvalidRequestError("fusion report exceeds its byte limit")
        if (
            isinstance(self.working_memory_bytes, bool)
            or not isinstance(self.working_memory_bytes, int)
            or self.working_memory_bytes < 0
        ):
            raise InvalidRequestError("fusion working_memory_bytes must be a nonnegative integer")
        object.__setattr__(self, "report", MappingProxyType(dict(self.report)))
        if self.outcome == ResolutionOutcome.ANSWER:
            if not self.selected_candidate_available or self.response_candidates != (self.selected_candidate,):
                raise InvalidRequestError("fusion ANSWER requires exactly one selected response candidate")
            if self.evidence:
                raise InvalidRequestError("fusion ANSWER cannot retain top-level evidence")
            if not self.confidence_available or self.confidence <= 0.0:
                raise InvalidRequestError("fusion ANSWER requires positive available confidence")
        else:
            if self.selected_candidate_available or self.selected_candidate != EMPTY_CANDIDATE:
                raise InvalidRequestError("non-ANSWER fusion decisions cannot select a candidate")
            if self.confidence_available or self.confidence != 0.0:
                raise InvalidRequestError("non-ANSWER fusion confidence must be unavailable and zero")
        if self.outcome == ResolutionOutcome.EVIDENCE and not (self.response_candidates or self.evidence):
            raise InvalidRequestError("fusion EVIDENCE requires response candidates or evidence references")
        if self.outcome == ResolutionOutcome.MISS and (self.response_candidates or self.evidence):
            raise InvalidRequestError("fusion MISS cannot retain candidates or evidence")

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "outcome": self.outcome.value,
            "selected_candidate": self.selected_candidate.to_dict() if self.selected_candidate_available else {},
            "selected_candidate_available": self.selected_candidate_available,
            "response_candidates": [value.to_dict() for value in self.response_candidates],
            "evidence": [value.to_dict() for value in self.evidence],
            "confidence": self.confidence,
            "confidence_available": self.confidence_available,
            "reason_codes": list(self.reason_codes),
            "report": dict(self.report),
            "working_memory_bytes": self.working_memory_bytes,
        }

    def to_json(self) -> str:
        return _json_text(self.to_dict())

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> FusionDecision:
        fields = frozenset(
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
                "report",
                "working_memory_bytes",
            }
        )
        data = _exact_mapping(value, "FusionDecision", fields)
        schema_version = data["schema_version"]
        if isinstance(schema_version, bool) or not isinstance(schema_version, int):
            raise InvalidRequestError("FusionDecision schema_version must be an integer")
        outcome_value = data["outcome"]
        if not isinstance(outcome_value, str):
            raise InvalidRequestError("FusionDecision outcome must be a string")
        try:
            outcome = ResolutionOutcome(outcome_value)
        except ValueError as error:
            raise InvalidRequestError("FusionDecision outcome is unsupported") from error
        selected_available = data["selected_candidate_available"]
        confidence_available = data["confidence_available"]
        if not isinstance(selected_available, bool) or not isinstance(confidence_available, bool):
            raise InvalidRequestError("FusionDecision availability fields must be booleans")
        selected_value = data["selected_candidate"]
        response_values = data["response_candidates"]
        evidence_values = data["evidence"]
        reason_values = data["reason_codes"]
        report_value = data["report"]
        if not isinstance(selected_value, Mapping) or not isinstance(report_value, Mapping):
            raise InvalidRequestError("FusionDecision selected_candidate and report must contain objects")
        if not isinstance(response_values, list) or not all(isinstance(item, Mapping) for item in response_values):
            raise InvalidRequestError("FusionDecision response_candidates must contain objects")
        if not isinstance(evidence_values, list) or not all(isinstance(item, Mapping) for item in evidence_values):
            raise InvalidRequestError("FusionDecision evidence must contain objects")
        if not isinstance(reason_values, list) or not all(isinstance(item, str) for item in reason_values):
            raise InvalidRequestError("FusionDecision reason_codes must contain strings")
        confidence = data["confidence"]
        if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
            raise InvalidRequestError("FusionDecision confidence must be a number")
        working_memory_bytes = data["working_memory_bytes"]
        if isinstance(working_memory_bytes, bool) or not isinstance(working_memory_bytes, int):
            raise InvalidRequestError("FusionDecision working_memory_bytes must be an integer")
        selected = Candidate.from_dict(selected_value) if selected_available else EMPTY_CANDIDATE
        return cls(
            outcome=outcome,
            selected_candidate=selected,
            selected_candidate_available=selected_available,
            response_candidates=tuple(Candidate.from_dict(item) for item in response_values),
            evidence=tuple(EvidenceReference.from_dict(item) for item in evidence_values),
            confidence=float(confidence),
            confidence_available=confidence_available,
            reason_codes=tuple(reason_values),
            report=report_value,
            working_memory_bytes=working_memory_bytes,
            schema_version=schema_version,
        )

    @classmethod
    def from_json(cls, value: str) -> FusionDecision:
        return cls.from_dict(_load_mapping(value, "FusionDecision JSON"))


class FeatureNormalizer:
    """Resolver-specific transformations into documented comparable inputs."""

    @staticmethod
    def normalize(candidate: Candidate) -> NormalizedFeatureSet:
        raw = candidate.features.values
        values = dict.fromkeys(FusionFeature, 0.0)
        available: set[FusionFeature] = set()

        def assign(feature: FusionFeature, value: float) -> None:
            values[feature] = _bounded_unit(value)
            available.add(feature)

        if candidate.source == CandidateSource.EXACT and "exact_match" in raw:
            assign(FusionFeature.EXACT, raw["exact_match"])
        if candidate.source == CandidateSource.PATTERN and "pattern_specificity" in raw:
            assign(FusionFeature.PATTERN, _saturating(raw["pattern_specificity"], 4.0))
        if candidate.source == CandidateSource.LEXICAL:
            if "lexical_score" in raw:
                assign(FusionFeature.LEXICAL, raw["lexical_score"])
            elif "lexical_overlap" in raw:
                assign(FusionFeature.LEXICAL, raw["lexical_overlap"])
            elif "score" in raw:
                assign(FusionFeature.LEXICAL, raw["score"])
        if candidate.source in (CandidateSource.SUPPORT_SEMANTIC, CandidateSource.STANDALONE_SEMANTIC) and (
            "semantic_score" in raw
        ):
            assign(FusionFeature.SEMANTIC, raw["semantic_score"])
        for raw_name, feature in (
            ("entity_match", FusionFeature.ENTITY),
            ("relation_match", FusionFeature.RELATION),
            ("object_type_match", FusionFeature.OBJECT_TYPE),
        ):
            if raw_name in raw:
                assign(feature, raw[raw_name])
        if candidate.source == CandidateSource.SUPPORT_SEMANTIC and "support_coverage" in raw:
            assign(FusionFeature.SUPPORT, raw["support_coverage"])
        elif candidate.evidence:
            assign(FusionFeature.SUPPORT, float(any(item.kind == EvidenceKind.SUPPORT for item in candidate.evidence)))
        if candidate.source == CandidateSource.LEXICAL and "recency" in raw:
            assign(FusionFeature.FRESHNESS, raw["recency"])
        if candidate.source in (CandidateSource.SUPPORT_SEMANTIC, CandidateSource.STANDALONE_SEMANTIC) and (
            "authority_score" in raw
        ):
            assign(FusionFeature.AUTHORITY, raw["authority_score"])
        return NormalizedFeatureSet(values, tuple(feature for feature in FusionFeature if feature in available))


class CandidateFusionEngine:
    """Deterministic, bounded, non-generative first-generation fusion engine."""

    _SOURCE_ORDER = {
        CandidateSource.EXACT: 0,
        CandidateSource.SUPPORT_SEMANTIC: 1,
        CandidateSource.PATTERN: 2,
        CandidateSource.LEXICAL: 3,
        CandidateSource.STANDALONE_SEMANTIC: 4,
        CandidateSource.UTILITY: 5,
    }
    _SOURCE_FAMILY = {
        CandidateSource.EXACT: "exact",
        CandidateSource.SUPPORT_SEMANTIC: "support_semantic",
        CandidateSource.PATTERN: "pattern",
        CandidateSource.LEXICAL: "lexical",
        CandidateSource.STANDALONE_SEMANTIC: "standalone_semantic",
        CandidateSource.UTILITY: "utility",
    }
    _CONSERVATIVE_MINIMUM = frozenset(
        {
            FusionFeature.ENTITY,
            FusionFeature.RELATION,
            FusionFeature.OBJECT_TYPE,
            FusionFeature.SUPPORT,
            FusionFeature.FRESHNESS,
            FusionFeature.AUTHORITY,
        }
    )

    def __init__(
        self,
        policy: FusionPolicy = DEFAULT_FUSION_POLICY,
        authority: CandidateAuthority = ABSTAINING_CANDIDATE_AUTHORITY,
        clock_ns: Callable[[], int] = time.monotonic_ns,
    ) -> None:
        if not isinstance(policy, FusionPolicy):
            raise InvalidRequestError("fusion policy must be a FusionPolicy")
        if not callable(getattr(authority, "evaluate", ())):
            raise InvalidRequestError("fusion authority must implement candidate evaluation")
        if not callable(clock_ns):
            raise InvalidRequestError("fusion clock_ns must be callable")
        self.policy = policy
        self._authority = authority
        self._clock_ns = clock_ns

    @staticmethod
    def _merge_eligibility(first: CandidateEligibility, second: CandidateEligibility) -> CandidateEligibility:
        reasons = tuple(dict.fromkeys((*first.reason_codes, *second.reason_codes)))
        values = dict(first.feature_values)
        values.update(second.feature_values)
        available = tuple(
            feature for feature in FusionFeature if feature in set(first.feature_available).union(second.feature_available)
        )
        return CandidateEligibility(
            first.score_eligible and second.score_eligible,
            first.evidence_eligible and second.evidence_eligible,
            first.answer_eligible and second.answer_eligible,
            reasons,
            values,
            available,
        )

    def _clock(self) -> int:
        value = self._clock_ns()
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise InvalidRequestError("fusion clock_ns must return a nonnegative integer")
        return value

    def _deadline_exhausted(self, frame: QueryFrame) -> bool:
        return bool(frame.budget.deadline_ns and self._clock() >= frame.budget.deadline_ns)

    @staticmethod
    def _canonical_candidate_key(candidate: Candidate) -> tuple[int, str, str]:
        return (
            CandidateFusionEngine._SOURCE_ORDER[candidate.source],
            candidate.candidate_id,
            candidate.to_json(),
        )

    @staticmethod
    def _dedupe_evidence(evidence: tuple[EvidenceReference, ...], scope: ScopeKey) -> tuple[tuple[EvidenceReference, ...], int]:
        groups: dict[str, dict[str, EvidenceReference]] = {}
        for reference in evidence:
            if reference.scope != scope:
                continue
            groups.setdefault(reference.evidence_id, {})[reference.to_json()] = reference
        retained = []
        conflict_count = 0
        for evidence_id in sorted(groups):
            variants = groups[evidence_id]
            if len(variants) != 1:
                conflict_count += 1
                continue
            retained.append(variants[sorted(variants)[0]])
        return tuple(retained), conflict_count

    def _individual_eligibility(
        self,
        candidate: Candidate,
        frame: QueryFrame,
        normalized: NormalizedFeatureSet,
        candidate_id_conflict: bool,
    ) -> CandidateEligibility:
        if candidate_id_conflict:
            return CandidateEligibility(False, False, False, (FusionPolicyReason.CANDIDATE_ID_CONFLICT,))
        if candidate.scope != frame.scope:
            return CandidateEligibility(False, False, False, (FusionPolicyReason.CANDIDATE_SCOPE_MISMATCH,))
        if candidate.lifecycle != LifecycleState.ACTIVE:
            return CandidateEligibility(False, False, False, (FusionPolicyReason.CANDIDATE_LIFECYCLE_INELIGIBLE,))
        if any(reference.scope != frame.scope for reference in candidate.evidence):
            return CandidateEligibility(
                False,
                False,
                False,
                (FusionPolicyReason.CANDIDATE_EVIDENCE_SCOPE_MISMATCH,),
            )
        reasons = []
        answer_eligible = True
        if candidate.features.values.get("explicit_conflict", 0.0) > 0.0:
            answer_eligible = False
            reasons.append(FusionPolicyReason.EXPLICIT_CONFLICT)
        if FusionFeature.SUPPORT in normalized.available and normalized.values[FusionFeature.SUPPORT] == 0.0:
            answer_eligible = False
            reasons.append(FusionPolicyReason.SUPPORT_INCOMPLETE)
        structural = CandidateEligibility(True, True, answer_eligible, tuple(reasons))
        authority = self._authority.evaluate(candidate, frame)
        return self._merge_eligibility(structural, authority)

    @staticmethod
    def _apply_authoritative_features(normalized: NormalizedFeatureSet, eligibility: CandidateEligibility) -> NormalizedFeatureSet:
        values = dict(normalized.values)
        available = set(normalized.available)
        for feature in eligibility.feature_available:
            values[feature] = eligibility.feature_values[feature]
            available.add(feature)
        return NormalizedFeatureSet(values, tuple(feature for feature in FusionFeature if feature in available))

    def _score(self, normalized: NormalizedFeatureSet) -> tuple[float, Mapping[FusionFeature, float]]:
        weighted = dict.fromkeys(FusionFeature, 0.0)
        numerator = 0.0
        denominator = 0.0
        for feature in normalized.available:
            if feature in (FusionFeature.EXACT, FusionFeature.MARGIN):
                continue
            contribution = self.policy.weights[feature] * normalized.values[feature]
            weighted[feature] = contribution
            numerator += contribution
            denominator += self.policy.weights[feature]
        base = numerator / denominator if denominator else 0.0
        exact = normalized.values[FusionFeature.EXACT] if FusionFeature.EXACT in normalized.available else 0.0
        weighted[FusionFeature.EXACT] = exact
        score = exact + (1.0 - exact) * base
        return _bounded_unit(score), MappingProxyType(weighted)

    def _fuse_group(
        self,
        statement_id: str,
        contributions: tuple[FusionContribution, ...],
        frame: QueryFrame,
    ) -> FusedCandidate:
        active = tuple(contribution for contribution in contributions if contribution.eligibility.score_eligible)
        representative = (active or contributions)[0].candidate
        if active:
            eligibility = active[0].eligibility
            for contribution in active[1:]:
                eligibility = self._merge_eligibility(eligibility, contribution.eligibility)
        else:
            eligibility = contributions[0].eligibility
            for contribution in contributions[1:]:
                eligibility = self._merge_eligibility(eligibility, contribution.eligibility)
        excluded_reasons = tuple(
            reason
            for contribution in contributions
            if not contribution.eligibility.score_eligible
            for reason in contribution.eligibility.reason_codes
        )
        if active and excluded_reasons:
            eligibility = CandidateEligibility(
                eligibility.score_eligible,
                eligibility.evidence_eligible,
                eligibility.answer_eligible,
                tuple(dict.fromkeys((*eligibility.reason_codes, *excluded_reasons))),
                eligibility.feature_values,
                eligibility.feature_available,
            )
        if active and len({contribution.candidate.response for contribution in active}) != 1:
            eligibility = CandidateEligibility(
                False,
                False,
                False,
                tuple(dict.fromkeys((*eligibility.reason_codes, FusionPolicyReason.CANDIDATE_STATEMENT_CONFLICT))),
                eligibility.feature_values,
                eligibility.feature_available,
            )
            active = ()
        values = dict.fromkeys(FusionFeature, 0.0)
        available: set[FusionFeature] = set()
        for feature in FusionFeature:
            observed = [
                contribution.normalized.values[feature] for contribution in active if feature in contribution.normalized.available
            ]
            if observed:
                values[feature] = min(observed) if feature in self._CONSERVATIVE_MINIMUM else max(observed)
                available.add(feature)
        sources = tuple(dict.fromkeys(contribution.candidate.source for contribution in active))
        families = tuple(dict.fromkeys(self._SOURCE_FAMILY[source] for source in sources))
        if active:
            values[FusionFeature.AGREEMENT] = min(1.0, max(0.0, (len(families) - 1) / 2.0))
            available.add(FusionFeature.AGREEMENT)
        active_evidence = tuple(
            reference
            for contribution in active
            if FusionPolicyReason.SUPPORT_REFERENCE_STALE not in contribution.eligibility.reason_codes
            for reference in contribution.candidate.evidence
        )
        retained_evidence, evidence_conflicts = self._dedupe_evidence(active_evidence, frame.scope)
        support_present = any(reference.kind == EvidenceKind.SUPPORT for reference in retained_evidence)
        if active:
            values[FusionFeature.SUPPORT] = max(values[FusionFeature.SUPPORT], float(support_present))
            available.add(FusionFeature.SUPPORT)
        reasons = list(eligibility.reason_codes)
        answer_eligible = eligibility.answer_eligible
        exact_present = CandidateSource.EXACT in sources and values[FusionFeature.EXACT] == 1.0
        if evidence_conflicts:
            answer_eligible = False
            reasons.append(FusionPolicyReason.EVIDENCE_REFERENCE_CONFLICT)
        if active and not exact_present and len(families) < self.policy.minimum_independent_sources:
            answer_eligible = False
            reasons.append(FusionPolicyReason.INDEPENDENT_SOURCES_MISSING)
        if active and not exact_present and self.policy.require_support_for_non_exact and not support_present:
            answer_eligible = False
            reasons.append(FusionPolicyReason.SUPPORT_INCOMPLETE)
        identity_features = (FusionFeature.ENTITY, FusionFeature.RELATION)
        if any(feature in available and values[feature] == 0.0 for feature in identity_features):
            answer_eligible = False
            reasons.append(FusionPolicyReason.IDENTITY_FEATURE_MISMATCH)
        if frame.expected_object_type != ExpectedObjectType.UNKNOWN and (
            FusionFeature.OBJECT_TYPE not in available or values[FusionFeature.OBJECT_TYPE] == 0.0
        ):
            answer_eligible = False
            reasons.append(FusionPolicyReason.OBJECT_TYPE_FEATURE_MISMATCH)
        eligibility = CandidateEligibility(
            eligibility.score_eligible,
            eligibility.evidence_eligible,
            answer_eligible,
            tuple(dict.fromkeys(reasons)),
            eligibility.feature_values,
            eligibility.feature_available,
        )
        normalized = NormalizedFeatureSet(values, tuple(feature for feature in FusionFeature if feature in available))
        score, score_contributions = self._score(normalized)
        digest = hashlib.sha256(f"{self.policy.policy_version}:{frame.diagnostic_id}:{statement_id}".encode()).hexdigest()
        fused = Candidate(
            candidate_id=f"fused:sha256:{digest}",
            statement_id=statement_id,
            response=representative.response,
            source=representative.source,
            features=FeatureSet(
                values={feature.value: values[feature] for feature in normalized.available},
                unavailable=tuple(sorted(feature.value for feature in FusionFeature if feature not in normalized.available)),
            ),
            evidence=retained_evidence,
            scope=representative.scope,
            lifecycle=representative.lifecycle,
            provenance={
                **dict(representative.provenance),
                "fusion_policy_version": self.policy.policy_version,
                "resolver_sources": [source.value for source in sources],
                "resolver_families": list(families),
            },
            diagnostics={"fusion_contribution_count": len(contributions)},
        )
        return FusedCandidate(fused, contributions, normalized, score, score_contributions, eligibility)

    def _exhausted_decision(
        self,
        frame: QueryFrame,
        candidates: tuple[Candidate, ...],
        evidence: tuple[EvidenceReference, ...],
        reason: FusionPolicyReason,
        working_memory_bytes: int = 0,
    ) -> FusionDecision:
        retained_evidence, evidence_conflicts = self._dedupe_evidence(evidence, frame.scope)
        outcome = ResolutionOutcome.EVIDENCE if retained_evidence else ResolutionOutcome.MISS
        report = {
            "policy_version": self.policy.policy_version,
            "policy_fingerprint": policy_fingerprint(self.policy),
            "input_candidate_count": len(candidates),
            "candidate_count": 0,
            "omitted_candidate_count": 0,
            "top_two_margin": 0.0,
            "top_two_margin_available": False,
            "evidence_conflict_count": evidence_conflicts,
            "budget_exhausted": reason.value,
            "working_memory_bytes": working_memory_bytes,
            "candidates": [],
        }
        reasons = [reason.value]
        if retained_evidence:
            reasons.append(FusionPolicyReason.GRAPH_EVIDENCE_AVAILABLE.value)
        return FusionDecision(
            outcome=outcome,
            evidence=retained_evidence,
            reason_codes=tuple(reasons),
            report=MappingProxyType(report),
            working_memory_bytes=working_memory_bytes,
        )

    def decide(
        self,
        frame: QueryFrame,
        candidates: tuple[Candidate, ...],
        evidence: tuple[EvidenceReference, ...] = (),
        *,
        working_memory_limit: int = 0,
        working_memory_limit_available: bool = False,
    ) -> FusionDecision:
        if not isinstance(frame, QueryFrame):
            raise InvalidRequestError("fusion requires a QueryFrame")
        if not isinstance(candidates, tuple) or not all(isinstance(value, Candidate) for value in candidates):
            raise InvalidRequestError("fusion candidates must be a tuple of Candidate values")
        if len(candidates) > MAX_FUSION_CONTRIBUTIONS:
            raise InvalidRequestError(f"fusion candidates exceed the limit of {MAX_FUSION_CONTRIBUTIONS}")
        if not isinstance(evidence, tuple) or not all(isinstance(value, EvidenceReference) for value in evidence):
            raise InvalidRequestError("fusion evidence must be a tuple of EvidenceReference values")
        if len(evidence) > MAX_FUSION_CONTRIBUTIONS:
            raise InvalidRequestError(f"fusion evidence exceeds the limit of {MAX_FUSION_CONTRIBUTIONS}")
        if not isinstance(working_memory_limit_available, bool):
            raise InvalidRequestError("fusion working_memory_limit_available must be a boolean")
        if isinstance(working_memory_limit, bool) or not isinstance(working_memory_limit, int) or working_memory_limit < 0:
            raise InvalidRequestError("fusion working_memory_limit must be a nonnegative integer")
        if not working_memory_limit_available and working_memory_limit != 0:
            raise InvalidRequestError("fusion working_memory_limit requires availability")
        if working_memory_limit_available and working_memory_limit > frame.budget.max_working_memory_bytes:
            raise InvalidRequestError("fusion working_memory_limit exceeds the frame budget")
        if self._deadline_exhausted(frame):
            return self._exhausted_decision(frame, candidates, evidence, FusionPolicyReason.FUSION_DEADLINE_EXHAUSTED)
        memory_limit = working_memory_limit if working_memory_limit_available else frame.budget.max_working_memory_bytes
        serialized_candidates = tuple((candidate, candidate.to_json()) for candidate in candidates)
        working_bytes = sum(len(value.encode("utf-8")) for _, value in serialized_candidates) + sum(
            len(reference.to_json().encode("utf-8")) for reference in evidence
        )
        if working_bytes > memory_limit:
            return self._exhausted_decision(
                frame,
                candidates,
                evidence,
                FusionPolicyReason.FUSION_MEMORY_EXHAUSTED,
                memory_limit,
            )
        candidate_variants: dict[str, set[str]] = {}
        for candidate, serialized in serialized_candidates:
            candidate_variants.setdefault(candidate.candidate_id, set()).add(serialized)
        conflicting_candidate_ids = {candidate_id for candidate_id, variants in candidate_variants.items() if len(variants) > 1}
        ordered = tuple(sorted(candidates, key=self._canonical_candidate_key))
        contribution_groups: dict[str, list[FusionContribution]] = {}
        for index, candidate in enumerate(ordered):
            if index % 16 == 0 and self._deadline_exhausted(frame):
                return self._exhausted_decision(
                    frame,
                    candidates,
                    evidence,
                    FusionPolicyReason.FUSION_DEADLINE_EXHAUSTED,
                    working_bytes,
                )
            normalized = FeatureNormalizer.normalize(candidate)
            eligibility = self._individual_eligibility(
                candidate,
                frame,
                normalized,
                candidate.candidate_id in conflicting_candidate_ids,
            )
            normalized = self._apply_authoritative_features(normalized, eligibility)
            contribution = FusionContribution(candidate, normalized, eligibility)
            contribution_groups.setdefault(candidate.statement_id, []).append(contribution)
            working_bytes += len(contribution.to_json().encode("utf-8"))
            if working_bytes > memory_limit:
                return self._exhausted_decision(
                    frame,
                    candidates,
                    evidence,
                    FusionPolicyReason.FUSION_MEMORY_EXHAUSTED,
                    memory_limit,
                )
        fused_values = []
        for statement_id in sorted(contribution_groups):
            if self._deadline_exhausted(frame):
                return self._exhausted_decision(
                    frame,
                    candidates,
                    evidence,
                    FusionPolicyReason.FUSION_DEADLINE_EXHAUSTED,
                    working_bytes,
                )
            fused_value = self._fuse_group(statement_id, tuple(contribution_groups[statement_id]), frame)
            fused_values.append(fused_value)
            working_bytes += len(fused_value.to_json().encode("utf-8"))
            if working_bytes > memory_limit:
                return self._exhausted_decision(
                    frame,
                    candidates,
                    evidence,
                    FusionPolicyReason.FUSION_MEMORY_EXHAUSTED,
                    memory_limit,
                )
        fused = tuple(fused_values)
        ranked = tuple(
            sorted(
                (candidate for candidate in fused if candidate.eligibility.score_eligible),
                key=lambda item: (-item.score, item.candidate.statement_id),
            )
        )
        if ranked:
            margin_available = len(ranked) > 1
            margin = ranked[0].score - ranked[1].score if margin_available else 1.0
            top = ranked[0]
            values = dict(top.normalized.values)
            values[FusionFeature.MARGIN] = margin if margin_available else 0.0
            available = set(top.normalized.available)
            if margin_available:
                available.add(FusionFeature.MARGIN)
            top = replace(
                top,
                normalized=NormalizedFeatureSet(values, tuple(feature for feature in FusionFeature if feature in available)),
                candidate=replace(
                    top.candidate,
                    features=FeatureSet(
                        values={feature.value: values[feature] for feature in available},
                        unavailable=tuple(sorted(feature.value for feature in FusionFeature if feature not in available)),
                    ),
                ),
            )
            ranked = (top, *ranked[1:])
        else:
            margin_available = False
            margin = 0.0
        evidence_candidates = tuple(
            item for item in ranked if item.eligibility.evidence_eligible and item.score >= self.policy.evidence_threshold
        )
        reasons: list[str] = []
        selected = EMPTY_CANDIDATE
        selected_available = False
        confidence = 0.0
        confidence_available = False
        response_candidates: tuple[Candidate, ...] = ()
        retained_evidence, top_level_evidence_conflicts = self._dedupe_evidence(evidence, frame.scope)
        if ranked and ranked[0].eligibility.answer_eligible and ranked[0].score >= self.policy.answer_threshold:
            top = ranked[0]
            if margin_available and margin < self.policy.ambiguity_margin:
                outcome = ResolutionOutcome.EVIDENCE
                response_candidates = tuple(item.candidate for item in evidence_candidates)
                reasons.extend(
                    (
                        FusionPolicyReason.AMBIGUOUS_TOP_CANDIDATES.value,
                        FusionPolicyReason.EVIDENCE_THRESHOLD_MET.value,
                    )
                )
            else:
                outcome = ResolutionOutcome.ANSWER
                selected = top.candidate
                selected_available = True
                response_candidates = (selected,)
                retained_evidence = ()
                confidence = top.score
                confidence_available = True
                reasons.append(
                    FusionPolicyReason.ANSWER_EXACT_ELIGIBLE.value
                    if selected.source == CandidateSource.EXACT
                    else FusionPolicyReason.ANSWER_FUSION_THRESHOLD.value
                )
                reasons.append(
                    FusionPolicyReason.ANSWER_MARGIN_CLEAR.value
                    if margin_available
                    else FusionPolicyReason.ANSWER_NO_RUNNER_UP.value
                )
        elif evidence_candidates or retained_evidence:
            outcome = ResolutionOutcome.EVIDENCE
            response_candidates = tuple(item.candidate for item in evidence_candidates)
            if ranked:
                top = ranked[0]
                reasons.append(
                    FusionPolicyReason.ANSWER_ELIGIBILITY_PREVENTED.value
                    if not top.eligibility.answer_eligible
                    else FusionPolicyReason.ANSWER_THRESHOLD_NOT_MET.value
                )
            if evidence_candidates:
                reasons.append(FusionPolicyReason.EVIDENCE_THRESHOLD_MET.value)
            if retained_evidence:
                reasons.append(FusionPolicyReason.GRAPH_EVIDENCE_AVAILABLE.value)
        else:
            outcome = ResolutionOutcome.MISS
            reasons.append(
                FusionPolicyReason.NO_ELIGIBLE_CANDIDATES.value
                if not ranked
                else FusionPolicyReason.EVIDENCE_THRESHOLD_NOT_MET.value
            )
        ranked_ids = {candidate.candidate.statement_id for candidate in ranked}
        report_values = (*ranked, *(candidate for candidate in fused if candidate.candidate.statement_id not in ranked_ids))
        candidate_reports = [candidate.to_report_dict() for candidate in report_values[: self.policy.max_report_candidates]]

        def make_report() -> dict[str, object]:
            return {
                "policy": self.policy.to_dict(),
                "policy_fingerprint": policy_fingerprint(self.policy),
                "input_candidate_count": len(candidates),
                "candidate_count": len(fused),
                "omitted_candidate_count": len(report_values) - len(candidate_reports),
                "top_two_margin": margin,
                "top_two_margin_available": margin_available,
                "evidence_conflict_count": top_level_evidence_conflicts,
                "budget_exhausted": "",
                "candidates": candidate_reports,
            }

        report = make_report()
        report_limit = min(MAX_FUSION_REPORT_BYTES, max(512, frame.budget.max_diagnostic_bytes))
        while candidate_reports and len(_json_text(report).encode("utf-8")) > report_limit:
            candidate_reports.pop()
            report = make_report()
        if len(_json_text(report).encode("utf-8")) > report_limit:
            report = {
                "policy_version": self.policy.policy_version,
                "policy_fingerprint": policy_fingerprint(self.policy),
                "input_candidate_count": len(candidates),
                "candidate_count": len(fused),
                "omitted_candidate_count": len(report_values),
                "top_two_margin": margin,
                "top_two_margin_available": margin_available,
                "evidence_conflict_count": top_level_evidence_conflicts,
                "budget_exhausted": "",
                "candidates": [],
            }
        working_bytes += len(_json_text(report).encode("utf-8"))
        if working_bytes > memory_limit:
            return self._exhausted_decision(
                frame,
                candidates,
                evidence,
                FusionPolicyReason.FUSION_MEMORY_EXHAUSTED,
                memory_limit,
            )
        if self._deadline_exhausted(frame):
            return self._exhausted_decision(
                frame,
                candidates,
                evidence,
                FusionPolicyReason.FUSION_DEADLINE_EXHAUSTED,
                working_bytes,
            )
        return FusionDecision(
            outcome,
            selected,
            selected_available,
            response_candidates,
            retained_evidence,
            confidence,
            confidence_available,
            tuple(dict.fromkeys(reasons)),
            MappingProxyType(report),
            working_memory_bytes=working_bytes,
        )


def policy_fingerprint(policy: FusionPolicy) -> str:
    """Return a stable release/evidence fingerprint without hidden state."""

    encoded = json.dumps(policy.to_dict(), sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
