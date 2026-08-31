"""Versioned candidate normalization, fusion, eligibility, and ambiguity policy."""

import hashlib
import json
import math
from collections.abc import Callable, Mapping
from types import MappingProxyType, NoneType

from engram.artifacts import LifecycleState
from engram.constants import (
    CANDIDATE_ELIGIBILITY_FIELDS,
    CANDIDATE_ELIGIBILITY_SCHEMA_VERSION,
    DEFAULT_FUSION_WEIGHTS,
    FEATURE_DEFINITION_FIELDS,
    FUSED_CANDIDATE_FIELDS,
    FUSED_CANDIDATE_SCHEMA_VERSION,
    FUSION_AMBIGUITY_MARGIN,
    FUSION_ANSWER_THRESHOLD,
    FUSION_CONSERVATIVE_MINIMUM,
    FUSION_CONTRIBUTION_FIELDS,
    FUSION_CONTRIBUTION_SCHEMA_VERSION,
    FUSION_DECISION_FIELDS,
    FUSION_DECISION_SCHEMA_VERSION,
    FUSION_EVIDENCE_THRESHOLD,
    FUSION_FEATURE_DEFINITION_SPECS,
    FUSION_FORMULA_VERSION,
    FUSION_MINIMUM_INDEPENDENT_SOURCES,
    FUSION_POLICY_FIELDS,
    FUSION_POLICY_SCHEMA_VERSION,
    FUSION_POLICY_VERSION,
    FUSION_REQUIRE_SUPPORT_FOR_NON_EXACT,
    FUSION_SOURCE_FAMILY,
    FUSION_SOURCE_ORDER,
    MAX_FUSION_CONTRIBUTIONS,
    MAX_FUSION_INDEPENDENT_SOURCES,
    MAX_FUSION_POLICY_VERSION_BYTES,
    MAX_FUSION_REASON_CODES,
    MAX_FUSION_REPORT_BYTES,
    MAX_FUSION_REPORT_CANDIDATES,
    MAX_FUSION_REPORT_CONTRIBUTIONS,
    MIN_FUSION_INDEPENDENT_SOURCES,
    NORMALIZED_FEATURE_SCHEMA_VERSION,
    NORMALIZED_FEATURE_SET_FIELDS,
    UTILITY_CONTRACT_VERSION,
    UTILITY_RESOLVER_VERSION,
    FusionFeature,
    FusionFeatureRole,
    FusionPolicyReason,
    GraphCompositionOperator,
    PredicateCardinality,
    RelationSelectionReason,
)
from engram.eligibility import EpochEligibilityPolicy, evaluate_artifact_eligibility
from engram.errors import InvalidRequestError, ResolutionCancelledError
from engram.evidence import PropositionEligibilityEvaluator
from engram.feedback import FeedbackStore, constraint_fingerprint
from engram.graph import PropositionProjectionQuery, validate_proposition_projection
from engram.identity import ScopeKey
from engram.relation import phrase_relation_result
from engram.reranking import RERANKER_FEATURES, TransparentLogisticReranker
from engram.resolution import (
    Candidate,
    CandidateSource,
    EvidenceKind,
    EvidenceReference,
    ExpectedObjectType,
    QueryFrame,
    ResolutionOutcome,
    _trusted_candidate,
    _trusted_candidate_to_dict,
    _trusted_candidate_to_json,
    _trusted_candidate_with_changes,
    _trusted_feature_set,
    candidate_from_dict,
    candidate_to_dict,
    empty_candidate,
    evidence_reference_from_dict,
    evidence_reference_to_dict,
    evidence_reference_to_json,
    feature_set,
    validate_candidate,
    validate_evidence_reference,
    validate_query_frame,
)
from engram.utilities import UTILITY_PLUGIN_VERSION, evaluate_named_utility

FeatureDefinition = dict


def feature_definition(
    feature: object,
    minimum: object,
    maximum: object,
    higher_is_better: object,
    meaning: object,
    unavailable_meaning: object,
    producer: object,
    owner_section: object,
    trust_boundary: object,
    raw_range: object,
    combination_rule: object,
    role: object,
) -> FeatureDefinition:
    """Build one documentation-bearing normalized policy input definition."""
    if not isinstance(feature, FusionFeature):
        raise InvalidRequestError("feature definition feature must be a FusionFeature")
    if not isinstance(role, FusionFeatureRole):
        raise InvalidRequestError("feature definition role must be a FusionFeatureRole")
    if isinstance(minimum, bool) or not isinstance(minimum, (int, float)) or not math.isfinite(float(minimum)):
        raise InvalidRequestError("feature definition minimum must be a finite number")
    if isinstance(maximum, bool) or not isinstance(maximum, (int, float)) or not math.isfinite(float(maximum)):
        raise InvalidRequestError("feature definition maximum must be a finite number")
    validated_minimum = float(minimum)
    validated_maximum = float(maximum)
    if not 0.0 <= validated_minimum <= 1.0 or not 0.0 <= validated_maximum <= 1.0:
        raise InvalidRequestError("feature definition range must be bounded from zero through one")
    if validated_minimum > validated_maximum:
        raise InvalidRequestError("feature definition range is invalid")
    if not isinstance(higher_is_better, bool):
        raise InvalidRequestError("feature definition direction must be a boolean")
    text_values = (meaning, unavailable_meaning, producer, owner_section, trust_boundary, raw_range, combination_rule)
    if not all(isinstance(value, str) and value for value in text_values):
        raise InvalidRequestError("feature definition text must be non-empty")
    normalized_meaning = str(meaning)
    normalized_unavailable_meaning = str(unavailable_meaning)
    normalized_producer = str(producer)
    normalized_owner_section = str(owner_section)
    normalized_trust_boundary = str(trust_boundary)
    normalized_raw_range = str(raw_range)
    normalized_combination_rule = str(combination_rule)
    result: FeatureDefinition = {
        "feature": feature,
        "minimum": validated_minimum,
        "maximum": validated_maximum,
        "higher_is_better": higher_is_better,
        "meaning": normalized_meaning,
        "unavailable_meaning": normalized_unavailable_meaning,
        "producer": normalized_producer,
        "owner_section": normalized_owner_section,
        "trust_boundary": normalized_trust_boundary,
        "raw_range": normalized_raw_range,
        "combination_rule": normalized_combination_rule,
        "role": role,
    }
    return result


def validate_feature_definition(value: object) -> FeatureDefinition:
    data = _exact_mapping(value, "FeatureDefinition", FEATURE_DEFINITION_FIELDS)
    result = feature_definition(
        data["feature"],
        data["minimum"],
        data["maximum"],
        data["higher_is_better"],
        data["meaning"],
        data["unavailable_meaning"],
        data["producer"],
        data["owner_section"],
        data["trust_boundary"],
        data["raw_range"],
        data["combination_rule"],
        data["role"],
    )
    return result


def feature_definitions() -> dict[FusionFeature, FeatureDefinition]:
    """Build an isolated validated view of the centralized feature vocabulary."""

    result = {}
    for feature, specification in FUSION_FEATURE_DEFINITION_SPECS.items():
        result[feature] = feature_definition(feature, *specification)
    return result


def _bounded_unit(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
        raise InvalidRequestError("fusion feature values must be finite numbers")
    result = min(1.0, max(0.0, float(value)))
    return result


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
    result = bounded / (bounded + scale) if bounded else 0.0
    return result


def _json_text(value: Mapping[str, object]) -> str:
    result = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    return result


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


def _exact_mapping(value: object, name: str, fields: set[str]) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or set(value) != fields or not all(isinstance(key, str) for key in value):
        raise InvalidRequestError(f"{name} has invalid fields")
    return value


def _enum_feature_mapping(value: object, name: str, *, complete: bool) -> dict[FusionFeature, float]:
    if not isinstance(value, Mapping) or not all(isinstance(key, str) for key in value):
        raise InvalidRequestError(f"{name} must contain an object")
    expected = {feature.value for feature in FusionFeature}
    if complete and set(value) != expected:
        raise InvalidRequestError(f"{name} must contain every canonical feature")
    if not complete and not set(value).issubset(expected):
        raise InvalidRequestError(f"{name} contains an unsupported feature")
    try:
        result = {FusionFeature(key): _bounded_unit(item) for key, item in value.items()}
        return result
    except ValueError as error:
        raise InvalidRequestError(f"{name} contains an unsupported feature") from error


def _feature_tuple(value: object, name: str) -> tuple[FusionFeature, ...]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise InvalidRequestError(f"{name} must contain a list of feature names")
    try:
        result = tuple(FusionFeature(item) for item in value)
        return result
    except ValueError as error:
        raise InvalidRequestError(f"{name} contains an unsupported feature") from error


def _weighted_feature_mapping(value: object, name: str) -> dict[FusionFeature, float]:
    if not isinstance(value, Mapping) or set(value) != {feature.value for feature in FusionFeature}:
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
        result = True
        return result
    if isinstance(value, Mapping):
        result = any(_contains_none(key) or _contains_none(item) for key, item in value.items())
        return result
    if isinstance(value, (list, tuple)):
        result = any(_contains_none(item) for item in value)
        return result
    result = False
    return result


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
    result = float(value)
    return result


NormalizedFeatureSet = dict


def normalized_feature_set(
    values: object,
    available: object,
    schema_version: object = NORMALIZED_FEATURE_SCHEMA_VERSION,
) -> NormalizedFeatureSet:
    """Build a concrete-zero feature vector with explicit availability."""
    version = _integer(schema_version, "NormalizedFeatureSet schema_version")
    if version != NORMALIZED_FEATURE_SCHEMA_VERSION:
        raise InvalidRequestError(f"unsupported normalized feature schema_version: {version}")
    if not isinstance(values, Mapping) or set(values) != set(FusionFeature):
        raise InvalidRequestError("normalized features must contain every canonical feature")
    normalized = {feature: _bounded_unit(values[feature]) for feature in FusionFeature}
    if not isinstance(available, tuple) or not all(isinstance(value, FusionFeature) for value in available):
        raise InvalidRequestError("normalized feature availability must be a tuple of FusionFeature values")
    ordered = tuple(feature for feature in FusionFeature if feature in set(available))
    if ordered != available:
        raise InvalidRequestError("normalized feature availability must be unique and canonical-order sorted")
    result: NormalizedFeatureSet = {
        "values": MappingProxyType(normalized),
        "available": ordered,
        "schema_version": version,
    }
    return result


def empty_normalized_feature_set() -> NormalizedFeatureSet:
    values = dict.fromkeys(FusionFeature, 0.0)
    result = normalized_feature_set(values, ())
    return result


def validate_normalized_feature_set(value: object) -> NormalizedFeatureSet:
    data = _exact_mapping(value, "NormalizedFeatureSet", NORMALIZED_FEATURE_SET_FIELDS)
    result = normalized_feature_set(data["values"], data["available"], data["schema_version"])
    return result


def _trusted_normalized_feature_set(
    values: Mapping[FusionFeature, float],
    available: tuple[FusionFeature, ...],
) -> NormalizedFeatureSet:
    """Build a normalized feature set from engine-owned, already bounded values."""
    result: NormalizedFeatureSet = {
        "values": MappingProxyType({feature: values[feature] for feature in FusionFeature}),
        "available": available,
        "schema_version": NORMALIZED_FEATURE_SCHEMA_VERSION,
    }
    return result


def normalized_feature_set_to_dict(value: object) -> dict[str, object]:
    current = validate_normalized_feature_set(value)
    result = _trusted_normalized_feature_set_to_dict(current)
    return result


def _trusted_normalized_feature_set_to_dict(current: NormalizedFeatureSet) -> dict[str, object]:
    """Serialize an engine-owned normalized feature set."""
    result = {
        "schema_version": current["schema_version"],
        "values": {feature.value: current["values"][feature] for feature in FusionFeature},
        "available": [feature.value for feature in current["available"]],
    }
    return result


def normalized_feature_set_to_json(value: object) -> str:
    data = normalized_feature_set_to_dict(value)
    result = _json_text(data)
    return result


def normalized_feature_set_from_dict(value: object) -> NormalizedFeatureSet:
    data = _exact_mapping(value, "NormalizedFeatureSet", NORMALIZED_FEATURE_SET_FIELDS)
    schema_version = _integer(data["schema_version"], "NormalizedFeatureSet schema_version")
    values = _enum_feature_mapping(data["values"], "NormalizedFeatureSet values", complete=True)
    available = _feature_tuple(data["available"], "NormalizedFeatureSet available")
    result = normalized_feature_set(values, available, schema_version)
    return result


def normalized_feature_set_from_json(value: str) -> NormalizedFeatureSet:
    data = _load_mapping(value, "NormalizedFeatureSet JSON")
    result = normalized_feature_set_from_dict(data)
    return result


FusionPolicy = dict


def fusion_policy(
    policy_version: object = FUSION_POLICY_VERSION,
    formula_version: object = FUSION_FORMULA_VERSION,
    weights: object = DEFAULT_FUSION_WEIGHTS,
    answer_threshold: object = FUSION_ANSWER_THRESHOLD,
    evidence_threshold: object = FUSION_EVIDENCE_THRESHOLD,
    ambiguity_margin: object = FUSION_AMBIGUITY_MARGIN,
    minimum_independent_sources: object = FUSION_MINIMUM_INDEPENDENT_SOURCES,
    require_support_for_non_exact: object = FUSION_REQUIRE_SUPPORT_FOR_NON_EXACT,
    max_report_candidates: object = MAX_FUSION_REPORT_CANDIDATES,
    schema_version: object = FUSION_POLICY_SCHEMA_VERSION,
) -> FusionPolicy:
    """Build the configurable first-generation linear fusion policy."""
    version = _integer(schema_version, "FusionPolicy schema_version")
    if version != FUSION_POLICY_SCHEMA_VERSION:
        raise InvalidRequestError(f"unsupported fusion policy schema_version: {version}")
    if not isinstance(policy_version, str) or not policy_version or len(policy_version) > MAX_FUSION_POLICY_VERSION_BYTES:
        raise InvalidRequestError("fusion policy_version must be a bounded non-empty string")
    formula = _integer(formula_version, "FusionPolicy formula_version")
    if formula != FUSION_FORMULA_VERSION:
        raise InvalidRequestError("unsupported fusion formula_version")
    if not isinstance(weights, Mapping) or set(weights) != set(FusionFeature):
        raise InvalidRequestError("fusion weights must contain every canonical feature")
    normalized_weights = {}
    for feature in FusionFeature:
        value = weights[feature]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)) or value < 0:
            raise InvalidRequestError("fusion weights must be finite nonnegative numbers")
        normalized_weights[feature] = float(value)
    if normalized_weights[FusionFeature.MARGIN] != 0.0:
        raise InvalidRequestError("margin is a decision feature and must have zero scoring weight")
    if not any(weight for feature, weight in normalized_weights.items() if feature != FusionFeature.MARGIN):
        raise InvalidRequestError("fusion policy must have a positive scoring weight")
    answer = _policy_unit(answer_threshold, "fusion answer_threshold")
    evidence = _policy_unit(evidence_threshold, "fusion evidence_threshold")
    margin = _policy_unit(ambiguity_margin, "fusion ambiguity_margin")
    if evidence > answer:
        raise InvalidRequestError("fusion evidence_threshold cannot exceed answer_threshold")
    independent_sources = _integer(minimum_independent_sources, "minimum_independent_sources")
    if not MIN_FUSION_INDEPENDENT_SOURCES <= independent_sources <= MAX_FUSION_INDEPENDENT_SOURCES:
        raise InvalidRequestError(
            f"minimum_independent_sources must be from {MIN_FUSION_INDEPENDENT_SOURCES} through {MAX_FUSION_INDEPENDENT_SOURCES}"
        )
    support_required = _boolean(require_support_for_non_exact, "require_support_for_non_exact")
    report_candidates = _integer(max_report_candidates, "max_report_candidates")
    if not 1 <= report_candidates <= MAX_FUSION_REPORT_CANDIDATES:
        raise InvalidRequestError(f"max_report_candidates must be from 1 through {MAX_FUSION_REPORT_CANDIDATES}")
    result: FusionPolicy = {
        "policy_version": policy_version,
        "formula_version": formula,
        "weights": MappingProxyType(normalized_weights),
        "answer_threshold": answer,
        "evidence_threshold": evidence,
        "ambiguity_margin": margin,
        "minimum_independent_sources": independent_sources,
        "require_support_for_non_exact": support_required,
        "max_report_candidates": report_candidates,
        "schema_version": version,
    }
    return result


def validate_fusion_policy(value: object) -> FusionPolicy:
    data = _exact_mapping(value, "FusionPolicy", FUSION_POLICY_FIELDS)
    result = fusion_policy(
        data["policy_version"],
        data["formula_version"],
        data["weights"],
        data["answer_threshold"],
        data["evidence_threshold"],
        data["ambiguity_margin"],
        data["minimum_independent_sources"],
        data["require_support_for_non_exact"],
        data["max_report_candidates"],
        data["schema_version"],
    )
    return result


def fusion_policy_with_changes(value: object, changes: object) -> FusionPolicy:
    current = validate_fusion_policy(value)
    if not isinstance(changes, Mapping) or not set(changes).issubset(FUSION_POLICY_FIELDS):
        raise InvalidRequestError("fusion policy changes contain invalid fields")
    updated: dict[str, object] = dict(current)
    updated.update(changes)
    result = validate_fusion_policy(updated)
    return result


def fusion_policy_to_dict(value: object) -> dict[str, object]:
    current = validate_fusion_policy(value)
    result = {
        "schema_version": current["schema_version"],
        "policy_version": current["policy_version"],
        "formula_version": current["formula_version"],
        "weights": {feature.value: current["weights"][feature] for feature in FusionFeature},
        "answer_threshold": current["answer_threshold"],
        "evidence_threshold": current["evidence_threshold"],
        "ambiguity_margin": current["ambiguity_margin"],
        "minimum_independent_sources": current["minimum_independent_sources"],
        "require_support_for_non_exact": current["require_support_for_non_exact"],
        "max_report_candidates": current["max_report_candidates"],
    }
    return result


def fusion_policy_to_json(value: object) -> str:
    data = fusion_policy_to_dict(value)
    result = _json_text(data)
    return result


def fusion_policy_from_dict(value: object) -> FusionPolicy:
    data = _exact_mapping(value, "FusionPolicy", FUSION_POLICY_FIELDS)
    raw_weights = data["weights"]
    if not isinstance(raw_weights, Mapping) or set(raw_weights) != {feature.value for feature in FusionFeature}:
        raise InvalidRequestError("FusionPolicy weights have invalid fields")
    weights = {feature: _number(raw_weights[feature.value], f"FusionPolicy {feature.value} weight") for feature in FusionFeature}
    result = fusion_policy(
        policy_version=_text(data["policy_version"], "FusionPolicy policy_version"),
        formula_version=_integer(data["formula_version"], "FusionPolicy formula_version"),
        weights=weights,
        answer_threshold=_number(data["answer_threshold"], "FusionPolicy answer_threshold"),
        evidence_threshold=_number(data["evidence_threshold"], "FusionPolicy evidence_threshold"),
        ambiguity_margin=_number(data["ambiguity_margin"], "FusionPolicy ambiguity_margin"),
        minimum_independent_sources=_integer(data["minimum_independent_sources"], "FusionPolicy minimum_independent_sources"),
        require_support_for_non_exact=_boolean(data["require_support_for_non_exact"], "FusionPolicy require_support_for_non_exact"),
        max_report_candidates=_integer(data["max_report_candidates"], "FusionPolicy max_report_candidates"),
        schema_version=_integer(data["schema_version"], "FusionPolicy schema_version"),
    )
    return result


def fusion_policy_from_json(value: str) -> FusionPolicy:
    data = _load_mapping(value, "FusionPolicy JSON")
    result = fusion_policy_from_dict(data)
    return result


CandidateEligibility = dict


def candidate_eligibility(
    score_eligible: object = True,
    evidence_eligible: object = True,
    answer_eligible: object = True,
    reason_codes: object = (),
    feature_values: object = MappingProxyType({}),
    feature_available: object = (),
    schema_version: object = CANDIDATE_ELIGIBILITY_SCHEMA_VERSION,
) -> CandidateEligibility:
    """Build separate score, evidence, and direct-answer eligibility."""
    version = _integer(schema_version, "CandidateEligibility schema_version")
    if version != CANDIDATE_ELIGIBILITY_SCHEMA_VERSION:
        raise InvalidRequestError(f"unsupported candidate eligibility schema_version: {version}")
    if not isinstance(score_eligible, bool) or not isinstance(evidence_eligible, bool) or not isinstance(answer_eligible, bool):
        raise InvalidRequestError("candidate eligibility flags must be booleans")
    if answer_eligible and not score_eligible:
        raise InvalidRequestError("an answer-eligible candidate must be score eligible")
    if not isinstance(reason_codes, tuple):
        raise InvalidRequestError("candidate eligibility reasons must use FusionPolicyReason")
    validated_reasons = []
    for reason in reason_codes:
        if not isinstance(reason, FusionPolicyReason):
            raise InvalidRequestError("candidate eligibility reasons must use FusionPolicyReason")
        validated_reasons.append(reason)
    normalized_reasons = tuple(validated_reasons)
    if tuple(dict.fromkeys(normalized_reasons)) != normalized_reasons:
        raise InvalidRequestError("candidate eligibility reasons must be unique and ordered")
    if not isinstance(feature_values, Mapping):
        raise InvalidRequestError("eligibility feature values must be an object")
    values = {}
    for feature, value in feature_values.items():
        if not isinstance(feature, FusionFeature):
            raise InvalidRequestError("eligibility feature keys must be FusionFeature values")
        values[feature] = _bounded_unit(value)
    if not isinstance(feature_available, tuple):
        raise InvalidRequestError("eligibility feature availability must use FusionFeature")
    validated_available = []
    for feature in feature_available:
        if not isinstance(feature, FusionFeature):
            raise InvalidRequestError("eligibility feature availability must use FusionFeature")
        validated_available.append(feature)
    normalized_available = tuple(validated_available)
    if any(feature not in values for feature in normalized_available):
        raise InvalidRequestError("available eligibility features must have values")
    ordered = tuple(feature for feature in FusionFeature if feature in set(normalized_available))
    if ordered != normalized_available:
        raise InvalidRequestError("eligibility feature availability must be unique and canonical-order sorted")
    result: CandidateEligibility = {
        "score_eligible": score_eligible,
        "evidence_eligible": evidence_eligible,
        "answer_eligible": answer_eligible,
        "reason_codes": normalized_reasons,
        "feature_values": MappingProxyType(values),
        "feature_available": ordered,
        "schema_version": version,
    }
    return result


def validate_candidate_eligibility(value: object) -> CandidateEligibility:
    data = _exact_mapping(value, "CandidateEligibility", CANDIDATE_ELIGIBILITY_FIELDS)
    result = candidate_eligibility(
        data["score_eligible"],
        data["evidence_eligible"],
        data["answer_eligible"],
        data["reason_codes"],
        data["feature_values"],
        data["feature_available"],
        data["schema_version"],
    )
    return result


def _trusted_candidate_eligibility(
    score_eligible: bool = True,
    evidence_eligible: bool = True,
    answer_eligible: bool = True,
    reason_codes: tuple[FusionPolicyReason, ...] = (),
    feature_values: Mapping[FusionFeature, float] = MappingProxyType({}),
    feature_available: tuple[FusionFeature, ...] = (),
) -> CandidateEligibility:
    """Build eligibility from values established by the fusion engine."""
    result: CandidateEligibility = {
        "score_eligible": score_eligible,
        "evidence_eligible": evidence_eligible,
        "answer_eligible": answer_eligible,
        "reason_codes": reason_codes,
        "feature_values": MappingProxyType(dict(feature_values)),
        "feature_available": feature_available,
        "schema_version": CANDIDATE_ELIGIBILITY_SCHEMA_VERSION,
    }
    return result


def candidate_eligibility_to_dict(value: object) -> dict[str, object]:
    current = validate_candidate_eligibility(value)
    result = _trusted_candidate_eligibility_to_dict(current)
    return result


def _trusted_candidate_eligibility_to_dict(current: CandidateEligibility) -> dict[str, object]:
    """Serialize engine-owned candidate eligibility."""
    result = {
        "schema_version": current["schema_version"],
        "score_eligible": current["score_eligible"],
        "evidence_eligible": current["evidence_eligible"],
        "answer_eligible": current["answer_eligible"],
        "reason_codes": [reason.value for reason in current["reason_codes"]],
        "feature_values": {feature.value: current["feature_values"][feature] for feature in current["feature_values"]},
        "feature_available": [feature.value for feature in current["feature_available"]],
    }
    return result


def candidate_eligibility_to_json(value: object) -> str:
    data = candidate_eligibility_to_dict(value)
    result = _json_text(data)
    return result


def candidate_eligibility_from_dict(value: object) -> CandidateEligibility:
    data = _exact_mapping(value, "CandidateEligibility", CANDIDATE_ELIGIBILITY_FIELDS)
    raw_reasons = data["reason_codes"]
    if not isinstance(raw_reasons, list) or not all(isinstance(item, str) for item in raw_reasons):
        raise InvalidRequestError("CandidateEligibility reason_codes must contain a list of strings")
    try:
        reasons = tuple(FusionPolicyReason(item) for item in raw_reasons)
    except ValueError as error:
        raise InvalidRequestError("CandidateEligibility contains an unsupported reason code") from error
    result = candidate_eligibility(
        score_eligible=_boolean(data["score_eligible"], "CandidateEligibility score_eligible"),
        evidence_eligible=_boolean(data["evidence_eligible"], "CandidateEligibility evidence_eligible"),
        answer_eligible=_boolean(data["answer_eligible"], "CandidateEligibility answer_eligible"),
        reason_codes=reasons,
        feature_values=_enum_feature_mapping(data["feature_values"], "CandidateEligibility feature_values", complete=False),
        feature_available=_feature_tuple(data["feature_available"], "CandidateEligibility feature_available"),
        schema_version=_integer(data["schema_version"], "CandidateEligibility schema_version"),
    )
    return result


def candidate_eligibility_from_json(value: str) -> CandidateEligibility:
    data = _load_mapping(value, "CandidateEligibility JSON")
    result = candidate_eligibility_from_dict(data)
    return result


CandidateAuthority = Callable[[Candidate, QueryFrame], CandidateEligibility]


def permissive_candidate_authority(candidate: Candidate, frame: QueryFrame) -> CandidateEligibility:
    """Return explicit conformance-only eligibility for isolated fixtures."""
    del candidate, frame
    result = candidate_eligibility()
    return result


def abstaining_candidate_authority(candidate: Candidate, frame: QueryFrame) -> CandidateEligibility:
    """Return the safe default when no current-state authority was supplied."""
    del candidate, frame
    result = candidate_eligibility(
        False,
        False,
        False,
        (FusionPolicyReason.AUTHORITATIVE_STATEMENT_MISSING,),
    )
    return result


def candidate_visibility_allowed(metadata: Mapping[str, object], scope: ScopeKey) -> bool:
    """Return whether candidate metadata permits disclosure in the exact scope."""
    visibility = metadata.get("visibility", "")
    if visibility in ("", "public", "scope"):
        result = True
        return result
    if visibility == "context":
        owner = metadata.get("owner_context_fingerprint", "")
        result = isinstance(owner, str) and bool(owner) and owner == scope["context_fingerprint"]
        return result
    result = False
    return result


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

    def __call__(self, candidate: Candidate, frame: QueryFrame) -> CandidateEligibility:
        result = self.evaluate(candidate, frame)
        return result

    def _relation_candidate(self, candidate: Candidate, frame: QueryFrame) -> CandidateEligibility:
        """Revalidate a deterministic one-hop phrase against current Proposition state."""
        provenance = candidate["provenance"]
        required = {
            "producer",
            "subject_entity_id",
            "subject_label",
            "predicate_id",
            "predicate_label",
            "object_entity_id",
            "object_label",
            "object_type",
            "predicate_cardinality",
            "selection_reason",
            "supplied_trust",
            "supplied_trust_version",
        }
        if set(provenance) != required or provenance.get("producer") != "relation_one_hop_v1":
            return candidate_eligibility(
                False,
                False,
                False,
                (FusionPolicyReason.AUTHORITATIVE_STATEMENT_MISSING,),
            )
        if candidate["scope"] != frame["scope"]:
            return candidate_eligibility(False, False, False, (FusionPolicyReason.CANDIDATE_SCOPE_MISMATCH,))
        if (
            len(candidate["evidence"]) != 1
            or candidate["evidence"][0]["kind"] != EvidenceKind.PROPOSITION
            or candidate["evidence"][0]["evidence_id"] != candidate["statement_id"]
        ):
            return candidate_eligibility(
                False,
                False,
                False,
                (FusionPolicyReason.SUPPORT_REFERENCE_STALE,),
            )
        try:
            current_values = self._engram.current_proposition_projection(candidate["statement_id"])
            if not isinstance(current_values, tuple) or len(current_values) != 1:
                raise InvalidRequestError("current relation Proposition is unavailable")
            current = validate_proposition_projection(current_values[0])
            if current["projection_id"] != PropositionProjectionQuery.BY_ID_V1:
                raise InvalidRequestError("current relation Proposition was not read by ID")
            identity = (
                current["subject_entity_id"],
                current["predicate_id"],
                current["object_entity_id"],
            )
            expected_identity = (
                provenance["subject_entity_id"],
                provenance["predicate_id"],
                provenance["object_entity_id"],
            )
            if identity != expected_identity:
                raise InvalidRequestError("current relation Proposition identity changed")
            if (
                not current["supplied_trust_available"]
                or current["supplied_trust"] != provenance["supplied_trust"]
                or current["supplied_trust_version"] != provenance["supplied_trust_version"]
            ):
                raise InvalidRequestError("current relation Proposition trust changed")
            response = phrase_relation_result(
                provenance["subject_label"],
                provenance["predicate_label"],
                provenance["object_label"],
            )
            object_type = ExpectedObjectType(str(provenance["object_type"]))
            PredicateCardinality(str(provenance["predicate_cardinality"]))
            selection_reason = RelationSelectionReason(str(provenance["selection_reason"]))
            if selection_reason not in {
                RelationSelectionReason.SELECTED_UNIQUE,
                RelationSelectionReason.SELECTED_LATEST,
                RelationSelectionReason.SELECTED_TRUST_RANKED,
            }:
                raise InvalidRequestError("relation candidate selection reason is not direct-answer eligible")
        except (InvalidRequestError, TypeError, ValueError):
            return candidate_eligibility(
                False,
                False,
                False,
                (FusionPolicyReason.AUTHORITATIVE_STATEMENT_MISSING,),
            )
        if response != candidate["response"]:
            return candidate_eligibility(
                False,
                False,
                False,
                (FusionPolicyReason.AUTHORITATIVE_RESPONSE_MISMATCH,),
            )
        if frame["expected_object_type"] != ExpectedObjectType.UNKNOWN and (
            object_type == ExpectedObjectType.UNKNOWN or object_type != frame["expected_object_type"]
        ):
            return candidate_eligibility(
                True,
                True,
                False,
                (FusionPolicyReason.OBJECT_TYPE_FEATURE_MISMATCH,),
            )
        decision = PropositionEligibilityEvaluator(getattr(self._engram, "proposition_visibility_authority", ())).evaluate(current, frame)
        if not decision["eligible"]:
            return candidate_eligibility(False, False, False, (FusionPolicyReason.ARTIFACT_INELIGIBLE,))
        return candidate_eligibility(True, True, True)

    def _composition_candidate(self, candidate: Candidate, frame: QueryFrame) -> CandidateEligibility:
        """Revalidate every Proposition and reconstruct one bounded composition phrase."""
        provenance = candidate["provenance"]
        required = {
            "producer",
            "operator",
            "root_entity_id",
            "root_label",
            "predicate_labels",
            "proposition_ids",
            "identity_chain",
            "trust_chain",
            "terminal_labels",
            "terminal_types",
            "truth_value",
            "truth_available",
            "aggregate_value",
            "aggregate_value_available",
        }
        if set(provenance) != required or provenance.get("producer") != "graph_composition_v1":
            return candidate_eligibility(False, False, False, (FusionPolicyReason.AUTHORITATIVE_STATEMENT_MISSING,))
        if candidate["scope"] != frame["scope"]:
            return candidate_eligibility(False, False, False, (FusionPolicyReason.CANDIDATE_SCOPE_MISMATCH,))
        tuple_values = (
            provenance["proposition_ids"],
            provenance["identity_chain"],
            provenance["trust_chain"],
            provenance["predicate_labels"],
            provenance["terminal_labels"],
            provenance["terminal_types"],
        )
        if not all(isinstance(value, tuple) for value in tuple_values):
            return candidate_eligibility(False, False, False, (FusionPolicyReason.AUTHORITATIVE_STATEMENT_MISSING,))
        proposition_ids = tuple_values[0]
        identity_chain = tuple_values[1]
        trust_chain = tuple_values[2]
        predicate_labels = tuple_values[3]
        terminal_labels = tuple_values[4]
        terminal_types = tuple_values[5]
        if not 1 <= len(proposition_ids) <= 2 or not (len(identity_chain) == len(trust_chain) == len(predicate_labels) == len(proposition_ids)):
            return candidate_eligibility(False, False, False, (FusionPolicyReason.AUTHORITATIVE_STATEMENT_MISSING,))
        if (
            len(candidate["evidence"]) != len(proposition_ids)
            or tuple(reference["evidence_id"] for reference in candidate["evidence"]) != proposition_ids
        ):
            return candidate_eligibility(False, False, False, (FusionPolicyReason.SUPPORT_REFERENCE_STALE,))
        try:
            operator = GraphCompositionOperator(str(provenance["operator"]))
            evaluator = PropositionEligibilityEvaluator(getattr(self._engram, "proposition_visibility_authority", ()))
            for index, proposition_id in enumerate(proposition_ids):
                if not isinstance(proposition_id, str) or not proposition_id:
                    raise InvalidRequestError("composition Proposition ID is malformed")
                current_values = self._engram.current_proposition_projection(proposition_id)
                if not isinstance(current_values, tuple) or len(current_values) != 1:
                    raise InvalidRequestError("composition Proposition is unavailable")
                current = validate_proposition_projection(current_values[0])
                if current["projection_id"] != PropositionProjectionQuery.BY_ID_V1:
                    raise InvalidRequestError("composition Proposition was not read by ID")
                identity_value = identity_chain[index]
                trust_value = trust_chain[index]
                identity = identity_value if isinstance(identity_value, tuple) else ()
                trust = trust_value if isinstance(trust_value, tuple) else ()
                if not isinstance(identity, tuple) or len(identity) != 3 or not isinstance(trust, tuple) or len(trust) != 2:
                    raise InvalidRequestError("composition provenance chain is malformed")
                if (
                    current["subject_entity_id"],
                    current["predicate_id"],
                    current["object_entity_id"],
                ) != identity:
                    raise InvalidRequestError("composition Proposition identity changed")
                if (
                    not current["supplied_trust_available"]
                    or not current["supplied_trust_version_available"]
                    or (current["supplied_trust"], current["supplied_trust_version"]) != trust
                ):
                    raise InvalidRequestError("composition Proposition trust changed")
                if not evaluator.evaluate(current, frame)["eligible"]:
                    raise InvalidRequestError("composition Proposition is no longer eligible")
            root_label = str(provenance["root_label"])
            chain = " → ".join(str(value) for value in predicate_labels)
            if operator == GraphCompositionOperator.LOOKUP:
                if len(terminal_labels) != 1:
                    raise InvalidRequestError("composition terminal is not unique")
                response = f"{root_label} — {chain}: {terminal_labels[0]}."
            elif operator in {
                GraphCompositionOperator.EXISTS,
                GraphCompositionOperator.AND,
                GraphCompositionOperator.OR,
                GraphCompositionOperator.NOT,
            }:
                if not provenance["truth_available"] or not isinstance(provenance["truth_value"], bool):
                    raise InvalidRequestError("composition truth is unavailable")
                response = f"{root_label} — {operator.value.lower()} {chain}: {'true' if provenance['truth_value'] else 'false'}."
            else:
                aggregate = provenance["aggregate_value"]
                if not provenance["aggregate_value_available"] or not isinstance(aggregate, str) or not aggregate:
                    raise InvalidRequestError("composition aggregate is unavailable")
                response = f"{root_label} — {operator.value.lower()} {chain}: {aggregate}."
            object_type = ExpectedObjectType(str(terminal_types[0])) if terminal_types else ExpectedObjectType.UNKNOWN
        except (InvalidRequestError, TypeError, ValueError):
            return candidate_eligibility(False, False, False, (FusionPolicyReason.AUTHORITATIVE_STATEMENT_MISSING,))
        if response != candidate["response"]:
            return candidate_eligibility(False, False, False, (FusionPolicyReason.AUTHORITATIVE_RESPONSE_MISMATCH,))
        if frame["expected_object_type"] != ExpectedObjectType.UNKNOWN and (
            object_type == ExpectedObjectType.UNKNOWN or object_type != frame["expected_object_type"]
        ):
            return candidate_eligibility(True, True, False, (FusionPolicyReason.OBJECT_TYPE_FEATURE_MISMATCH,))
        return candidate_eligibility(True, True, True)

    def evaluate(self, candidate: Candidate, frame: QueryFrame) -> CandidateEligibility:
        if candidate["source"] == CandidateSource.UTILITY and candidate["provenance"].get("producer") == "relation_one_hop_v1":
            return self._relation_candidate(candidate, frame)
        if candidate["source"] == CandidateSource.UTILITY and candidate["provenance"].get("producer") == "graph_composition_v1":
            return self._composition_candidate(candidate, frame)
        if candidate["source"] == CandidateSource.UTILITY and candidate["provenance"].get("producer") == UTILITY_RESOLVER_VERSION:
            if frame["required_source_label"]:
                return candidate_eligibility(False, False, False, (FusionPolicyReason.REQUIRED_SOURCE_MISMATCH,))
            if frame["required_metadata"]:
                return candidate_eligibility(False, False, False, (FusionPolicyReason.REQUIRED_METADATA_MISMATCH,))
            plugin_name = candidate["provenance"].get("plugin_name", "")
            evaluated = evaluate_named_utility(frame["original_text"], plugin_name)
            if (
                evaluated["status"] != "resolved"
                or evaluated["plugin_version"] != UTILITY_PLUGIN_VERSION
                or evaluated["contract_version"] != UTILITY_CONTRACT_VERSION
                or candidate["provenance"].get("plugin_version") != evaluated["plugin_version"]
                or candidate["provenance"].get("contract_version") != evaluated["contract_version"]
                or candidate["provenance"].get("canonical_input") != evaluated["canonical_input"]
            ):
                return candidate_eligibility(False, False, False, (FusionPolicyReason.AUTHORITATIVE_STATEMENT_MISSING,))
            digest = hashlib.sha256(
                f"{plugin_name}:{evaluated['plugin_version']}:{evaluated['canonical_input']}".encode()
            ).hexdigest()
            if candidate["statement_id"] != f"utility:{plugin_name}:sha256:{digest}":
                return candidate_eligibility(False, False, False, (FusionPolicyReason.AUTHORITATIVE_STATEMENT_MISSING,))
            if candidate["response"] != evaluated["response"]:
                return candidate_eligibility(False, False, False, (FusionPolicyReason.AUTHORITATIVE_RESPONSE_MISMATCH,))
            return candidate_eligibility(True, True, True)
        snapshot = self._engram.response_repository.snapshot()
        artifacts = snapshot["artifacts"]
        if candidate["statement_id"] in artifacts:
            artifact = artifacts[candidate["statement_id"]]
            if isinstance(self._feedback_store, FeedbackStore):
                if self._feedback_store.stale_excluded(artifact["statement_id"], artifact["generation"]):
                    result = candidate_eligibility(False, False, False, (FusionPolicyReason.FEEDBACK_STALE_EXCLUDED,))
                    return result
                if self._feedback_store.policy_suppressed(
                    artifact["statement_id"],
                    frame["scope"]["namespace"],
                    self._policy_fingerprint,
                ):
                    result = candidate_eligibility(False, False, False, (FusionPolicyReason.FEEDBACK_POLICY_SUPPRESSED,))
                    return result
            decision = evaluate_artifact_eligibility(
                artifact,
                frame["eligibility_context"],
                EpochEligibilityPolicy.MATCH_WHEN_ARTIFACT_AVAILABLE,
            )
            if not decision["direct_answer_eligible"]:
                result = candidate_eligibility(
                    False,
                    False,
                    False,
                    (FusionPolicyReason.ARTIFACT_INELIGIBLE,),
                )
                return result
            if artifact["scope"] != frame["scope"]:
                result = candidate_eligibility(False, False, False, (FusionPolicyReason.CANDIDATE_SCOPE_MISMATCH,))
                return result
            if artifact["response"] != candidate["response"]:
                result = candidate_eligibility(False, False, False, (FusionPolicyReason.AUTHORITATIVE_RESPONSE_MISMATCH,))
                return result
            if "generation" in candidate["provenance"]:
                candidate_generation = candidate["provenance"]["generation"]
                if (
                    isinstance(candidate_generation, bool)
                    or not isinstance(candidate_generation, int)
                    or candidate_generation != artifact["generation"]
                ):
                    result = candidate_eligibility(
                        False,
                        False,
                        False,
                        (FusionPolicyReason.AUTHORITATIVE_GENERATION_MISMATCH,),
                    )
                    return result
            if frame["required_source_label"] and artifact["provenance"]["source_label"] != frame["required_source_label"]:
                result = candidate_eligibility(False, False, False, (FusionPolicyReason.REQUIRED_SOURCE_MISMATCH,))
                return result
            if any(
                key not in artifact["metadata"] or artifact["metadata"][key] != value
                for key, value in frame["required_metadata"].items()
            ):
                result = candidate_eligibility(False, False, False, (FusionPolicyReason.REQUIRED_METADATA_MISMATCH,))
                return result
            if not candidate_visibility_allowed(artifact["metadata"], frame["scope"]):
                result = candidate_eligibility(False, False, False, (FusionPolicyReason.OWNERSHIP_VISIBILITY_MISMATCH,))
                return result
            feature_values: dict[FusionFeature, float] = {
                FusionFeature.SUPPORT: float(bool(artifact["support_references"])),
            }
            feature_available = [FusionFeature.SUPPORT]
            if artifact["statistics"]["query_count"]:
                feature_values[FusionFeature.HISTORY] = artifact["statistics"]["hit_count"] / artifact["statistics"]["query_count"]
                feature_available.append(FusionFeature.HISTORY)
            if isinstance(self._feedback_store, FeedbackStore):
                feedback_history = self._feedback_store.history(
                    frame["identity"],
                    constraint_fingerprint(
                        frame["expected_object_type"].value,
                        frame["required_metadata"],
                        frame["required_source_label"],
                    ),
                    artifact["statement_id"],
                    artifact["generation"],
                    self._policy_fingerprint,
                    frame["eligibility_context"]["evaluation_time"],
                )
                if feedback_history["available"]:
                    if FusionFeature.HISTORY in feature_available:
                        feature_values[FusionFeature.HISTORY] = (
                            feature_values[FusionFeature.HISTORY] + feedback_history["value"]
                        ) / 2.0
                    else:
                        feature_values[FusionFeature.HISTORY] = feedback_history["value"]
                        feature_available.append(FusionFeature.HISTORY)
            authority = artifact["metadata"].get("authority")
            if (
                isinstance(authority, (int, float))
                and not isinstance(authority, bool)
                and math.isfinite(float(authority))
                and 0.0 <= float(authority) <= 1.0
            ):
                feature_values[FusionFeature.AUTHORITY] = float(authority)
                feature_available.append(FusionFeature.AUTHORITY)
            support_complete = artifact["metadata"].get("support_complete", True)
            answer_eligible = isinstance(support_complete, bool) and support_complete
            reasons: tuple[FusionPolicyReason, ...] = () if answer_eligible else (FusionPolicyReason.SUPPORT_INCOMPLETE,)
            retained_support = tuple(
                reference["evidence_id"] for reference in candidate["evidence"] if reference["kind"] == EvidenceKind.SUPPORT
            )
            support_ids = {
                reference.get("id", "")
                for reference in artifact["support_references"]
            }
            if any(reference_id not in support_ids for reference_id in retained_support):
                answer_eligible = False
                reasons = tuple(dict.fromkeys((*reasons, FusionPolicyReason.SUPPORT_REFERENCE_STALE)))
            result = candidate_eligibility(
                True,
                True,
                answer_eligible,
                reasons,
                feature_values,
                tuple(feature for feature in FusionFeature if feature in feature_available),
            )
            return result
        if isinstance(self._feedback_store, FeedbackStore):
            if self._feedback_store.stale_excluded(candidate["statement_id"], 0, False):
                result = candidate_eligibility(False, False, False, (FusionPolicyReason.FEEDBACK_STALE_EXCLUDED,))
                return result
            if self._feedback_store.policy_suppressed(
                candidate["statement_id"],
                frame["scope"]["namespace"],
                self._policy_fingerprint,
            ):
                result = candidate_eligibility(False, False, False, (FusionPolicyReason.FEEDBACK_POLICY_SUPPRESSED,))
                return result
        statement = self._engram.get_statement(candidate["statement_id"])
        if not statement:
            result = candidate_eligibility(False, False, False, (FusionPolicyReason.AUTHORITATIVE_STATEMENT_MISSING,))
            return result
        if statement.get("text", "") != candidate["response"]:
            result = candidate_eligibility(False, False, False, (FusionPolicyReason.AUTHORITATIVE_RESPONSE_MISMATCH,))
            return result
        template = statement.get("template", {})
        template = template if isinstance(template, Mapping) else MappingProxyType({})
        tapestry = template.get("tapestry", {})
        tapestry = tapestry if isinstance(tapestry, Mapping) else MappingProxyType({})
        metadata = tapestry.get("metadata", {})
        metadata = metadata if isinstance(metadata, Mapping) else MappingProxyType({})
        namespace = tapestry.get("namespace", "")
        context_fingerprint = tapestry.get("context_fingerprint", "")
        if (namespace or context_fingerprint) and (namespace, context_fingerprint) != (
            frame["scope"]["namespace"],
            frame["scope"]["context_fingerprint"],
        ):
            result = candidate_eligibility(False, False, False, (FusionPolicyReason.CANDIDATE_SCOPE_MISMATCH,))
            return result
        response_artifact = template.get("response_artifact", {})
        if isinstance(response_artifact, Mapping) and response_artifact.get("lifecycle", "ACTIVE") != "ACTIVE":
            result = candidate_eligibility(False, False, False, (FusionPolicyReason.CANDIDATE_LIFECYCLE_INELIGIBLE,))
            return result
        source_label = statement.get("source_label", "")
        if frame["required_source_label"] and source_label != frame["required_source_label"]:
            result = candidate_eligibility(False, False, False, (FusionPolicyReason.REQUIRED_SOURCE_MISMATCH,))
            return result
        if any(key not in metadata or metadata[key] != value for key, value in frame["required_metadata"].items()):
            result = candidate_eligibility(False, False, False, (FusionPolicyReason.REQUIRED_METADATA_MISMATCH,))
            return result
        if not candidate_visibility_allowed(metadata, frame["scope"]):
            result = candidate_eligibility(False, False, False, (FusionPolicyReason.OWNERSHIP_VISIBILITY_MISMATCH,))
            return result
        result = candidate_eligibility()
        return result


FusionContribution = dict


def fusion_contribution(
    candidate: object,
    normalized: object,
    eligibility: object,
    schema_version: object = FUSION_CONTRIBUTION_SCHEMA_VERSION,
) -> FusionContribution:
    """Build one retained raw resolver proposal and normalized projection."""
    version = _integer(schema_version, "FusionContribution schema_version")
    if version != FUSION_CONTRIBUTION_SCHEMA_VERSION:
        raise InvalidRequestError(f"unsupported fusion contribution schema_version: {version}")
    try:
        validated_candidate = validate_candidate(candidate)
    except InvalidRequestError as error:
        raise InvalidRequestError("fusion contribution candidate must be a Candidate") from error
    try:
        validated_normalized = validate_normalized_feature_set(normalized)
    except InvalidRequestError as error:
        raise InvalidRequestError("fusion contribution normalized value must be a NormalizedFeatureSet") from error
    try:
        validated_eligibility = validate_candidate_eligibility(eligibility)
    except InvalidRequestError as error:
        raise InvalidRequestError("fusion contribution eligibility must be a CandidateEligibility") from error
    result: FusionContribution = {
        "candidate": validated_candidate,
        "normalized": validated_normalized,
        "eligibility": validated_eligibility,
        "schema_version": version,
    }
    return result


def validate_fusion_contribution(value: object) -> FusionContribution:
    data = _exact_mapping(value, "FusionContribution", FUSION_CONTRIBUTION_FIELDS)
    result = fusion_contribution(data["candidate"], data["normalized"], data["eligibility"], data["schema_version"])
    return result


def _trusted_fusion_contribution(
    candidate: Candidate,
    normalized: NormalizedFeatureSet,
    eligibility: CandidateEligibility,
) -> FusionContribution:
    """Build a contribution from engine-owned validated components."""
    result: FusionContribution = {
        "candidate": candidate,
        "normalized": normalized,
        "eligibility": eligibility,
        "schema_version": FUSION_CONTRIBUTION_SCHEMA_VERSION,
    }
    return result


def fusion_contribution_to_dict(value: object) -> dict[str, object]:
    current = validate_fusion_contribution(value)
    result = _trusted_fusion_contribution_to_dict(current)
    return result


def _trusted_fusion_contribution_to_dict(current: FusionContribution) -> dict[str, object]:
    """Serialize one engine-owned contribution."""
    result = {
        "schema_version": current["schema_version"],
        "candidate": _trusted_candidate_to_dict(current["candidate"]),
        "normalized": _trusted_normalized_feature_set_to_dict(current["normalized"]),
        "eligibility": _trusted_candidate_eligibility_to_dict(current["eligibility"]),
    }
    return result


def fusion_contribution_to_report_dict(value: object) -> dict[str, object]:
    current = validate_fusion_contribution(value)
    result = _trusted_fusion_contribution_to_report_dict(current)
    return result


def _trusted_fusion_contribution_to_report_dict(current: FusionContribution) -> dict[str, object]:
    """Render an engine-owned contribution for bounded diagnostics."""
    result = {
        "schema_version": current["schema_version"],
        "candidate_id": current["candidate"]["candidate_id"],
        "source": current["candidate"]["source"].value,
        "raw_features": {
            "schema_version": current["candidate"]["features"]["schema_version"],
            "values": dict(current["candidate"]["features"]["values"]),
            "unavailable": list(current["candidate"]["features"]["unavailable"]),
        },
        "normalized_features": _trusted_normalized_feature_set_to_dict(current["normalized"]),
        "eligibility": _trusted_candidate_eligibility_to_dict(current["eligibility"]),
        "diagnostic_fields": sorted(current["candidate"]["diagnostics"]),
        "evidence_ids": [reference["evidence_id"] for reference in current["candidate"]["evidence"]],
    }
    return result


def fusion_contribution_to_json(value: object) -> str:
    data = fusion_contribution_to_dict(value)
    result = _json_text(data)
    return result


def _trusted_fusion_contribution_to_json(value: FusionContribution) -> str:
    """Serialize one engine-owned contribution without redundant revalidation."""
    data = _trusted_fusion_contribution_to_dict(value)
    result = _json_text(data)
    return result


def fusion_contribution_from_dict(value: object) -> FusionContribution:
    data = _exact_mapping(value, "FusionContribution", FUSION_CONTRIBUTION_FIELDS)
    candidate_value = data["candidate"]
    normalized_value = data["normalized"]
    eligibility_value = data["eligibility"]
    if not isinstance(candidate_value, Mapping):
        raise InvalidRequestError("FusionContribution candidate must contain an object")
    if not isinstance(normalized_value, Mapping) or not isinstance(eligibility_value, Mapping):
        raise InvalidRequestError("FusionContribution nested contracts must contain objects")
    result = fusion_contribution(
        candidate_from_dict(candidate_value),
        normalized_feature_set_from_dict(normalized_value),
        candidate_eligibility_from_dict(eligibility_value),
        _integer(data["schema_version"], "FusionContribution schema_version"),
    )
    return result


def fusion_contribution_from_json(value: str) -> FusionContribution:
    data = _load_mapping(value, "FusionContribution JSON")
    result = fusion_contribution_from_dict(data)
    return result


FusedCandidate = dict


def fused_candidate(
    candidate: object,
    contributions: object,
    normalized: object,
    score: object,
    score_contributions: object,
    eligibility: object,
    schema_version: object = FUSED_CANDIDATE_SCHEMA_VERSION,
) -> FusedCandidate:
    """Build one deduplicated statement with its contributions and score."""
    version = _integer(schema_version, "FusedCandidate schema_version")
    if version != FUSED_CANDIDATE_SCHEMA_VERSION:
        raise InvalidRequestError(f"unsupported fused candidate schema_version: {version}")
    validated_score = _bounded_unit(score)
    try:
        validated_candidate = validate_candidate(candidate)
    except InvalidRequestError as error:
        raise InvalidRequestError("fused candidate candidate must be a Candidate") from error
    if not isinstance(contributions, tuple) or not contributions or len(contributions) > MAX_FUSION_CONTRIBUTIONS:
        raise InvalidRequestError("fused candidate contribution count is out of bounds")
    try:
        validated_contributions = tuple(validate_fusion_contribution(value) for value in contributions)
    except InvalidRequestError as error:
        raise InvalidRequestError("fused candidate contributions must use FusionContribution") from error
    if any(value["candidate"]["statement_id"] != validated_candidate["statement_id"] for value in validated_contributions):
        raise InvalidRequestError("fused candidate contributions must share one statement_id")
    try:
        validated_normalized = validate_normalized_feature_set(normalized)
    except InvalidRequestError as error:
        raise InvalidRequestError("fused candidate normalized value must be a NormalizedFeatureSet") from error
    try:
        validated_eligibility = validate_candidate_eligibility(eligibility)
    except InvalidRequestError as error:
        raise InvalidRequestError("fused candidate eligibility must be a CandidateEligibility") from error
    if not isinstance(score_contributions, Mapping) or set(score_contributions) != set(FusionFeature):
        raise InvalidRequestError("score contributions must contain every canonical feature")
    if any(
        isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)) or value < 0
        for value in score_contributions.values()
    ):
        raise InvalidRequestError("score contributions must be finite nonnegative numbers")
    validated_score_contributions = MappingProxyType({feature: float(score_contributions[feature]) for feature in FusionFeature})
    result: FusedCandidate = {
        "candidate": validated_candidate,
        "contributions": validated_contributions,
        "normalized": validated_normalized,
        "score": validated_score,
        "score_contributions": validated_score_contributions,
        "eligibility": validated_eligibility,
        "schema_version": version,
    }
    return result


def validate_fused_candidate(value: object) -> FusedCandidate:
    data = _exact_mapping(value, "FusedCandidate", FUSED_CANDIDATE_FIELDS)
    result = fused_candidate(
        data["candidate"],
        data["contributions"],
        data["normalized"],
        data["score"],
        data["score_contributions"],
        data["eligibility"],
        data["schema_version"],
    )
    return result


def _trusted_fused_candidate(
    candidate: Candidate,
    contributions: tuple[FusionContribution, ...],
    normalized: NormalizedFeatureSet,
    score: float,
    score_contributions: Mapping[FusionFeature, float],
    eligibility: CandidateEligibility,
) -> FusedCandidate:
    """Build a fused candidate from engine-owned validated components."""
    result: FusedCandidate = {
        "candidate": candidate,
        "contributions": contributions,
        "normalized": normalized,
        "score": score,
        "score_contributions": MappingProxyType(dict(score_contributions)),
        "eligibility": eligibility,
        "schema_version": FUSED_CANDIDATE_SCHEMA_VERSION,
    }
    return result


def fused_candidate_with_changes(value: object, changes: object) -> FusedCandidate:
    current = validate_fused_candidate(value)
    if not isinstance(changes, Mapping) or not set(changes).issubset(FUSED_CANDIDATE_FIELDS):
        raise InvalidRequestError("fused candidate changes contain invalid fields")
    updated: dict[str, object] = dict(current)
    updated.update(changes)
    result = validate_fused_candidate(updated)
    return result


def fused_candidate_sources(value: object) -> tuple[CandidateSource, ...]:
    current = validate_fused_candidate(value)
    result = _trusted_fused_candidate_sources(current)
    return result


def _trusted_fused_candidate_sources(current: FusedCandidate) -> tuple[CandidateSource, ...]:
    """Collect active sources from an engine-owned fused candidate."""
    sources = tuple(
        dict.fromkeys(
            contribution["candidate"]["source"]
            for contribution in current["contributions"]
            if contribution["eligibility"]["score_eligible"]
        )
    )
    return sources


def fused_candidate_to_dict(value: object) -> dict[str, object]:
    current = validate_fused_candidate(value)
    result = _trusted_fused_candidate_to_dict(current)
    return result


def _trusted_fused_candidate_to_dict(current: FusedCandidate) -> dict[str, object]:
    """Serialize one engine-owned fused candidate."""
    result = {
        "schema_version": current["schema_version"],
        "candidate": _trusted_candidate_to_dict(current["candidate"]),
        "contributions": [_trusted_fusion_contribution_to_dict(contribution) for contribution in current["contributions"]],
        "normalized": _trusted_normalized_feature_set_to_dict(current["normalized"]),
        "score": current["score"],
        "score_contributions": {feature.value: current["score_contributions"][feature] for feature in FusionFeature},
        "eligibility": _trusted_candidate_eligibility_to_dict(current["eligibility"]),
    }
    return result


def fused_candidate_to_report_dict(value: object) -> dict[str, object]:
    current = validate_fused_candidate(value)
    result = _trusted_fused_candidate_to_report_dict(current)
    return result


def _trusted_fused_candidate_to_report_dict(current: FusedCandidate) -> dict[str, object]:
    """Render an engine-owned fused candidate for bounded diagnostics."""
    visible = current["contributions"][:MAX_FUSION_REPORT_CONTRIBUTIONS]
    sources = _trusted_fused_candidate_sources(current)
    result = {
        "statement_id": current["candidate"]["statement_id"],
        "candidate_id": current["candidate"]["candidate_id"],
        "sources": [source.value for source in sources],
        "score": current["score"],
        "normalized_features": _trusted_normalized_feature_set_to_dict(current["normalized"]),
        "score_contributions": {feature.value: current["score_contributions"][feature] for feature in FusionFeature},
        "eligibility": {
            "score": current["eligibility"]["score_eligible"],
            "evidence": current["eligibility"]["evidence_eligible"],
            "answer": current["eligibility"]["answer_eligible"],
            "reason_codes": [reason.value for reason in current["eligibility"]["reason_codes"]],
        },
        "contributions": [_trusted_fusion_contribution_to_report_dict(contribution) for contribution in visible],
        "omitted_contribution_count": len(current["contributions"]) - len(visible),
    }
    return result


def fused_candidate_to_json(value: object) -> str:
    data = fused_candidate_to_dict(value)
    result = _json_text(data)
    return result


def _trusted_fused_candidate_to_json(value: FusedCandidate) -> str:
    """Serialize one engine-owned fused candidate without redundant revalidation."""
    data = _trusted_fused_candidate_to_dict(value)
    result = _json_text(data)
    return result


def fused_candidate_from_dict(value: object) -> FusedCandidate:
    data = _exact_mapping(value, "FusedCandidate", FUSED_CANDIDATE_FIELDS)
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
    result = fused_candidate(
        candidate=candidate_from_dict(candidate_value),
        contributions=tuple(fusion_contribution_from_dict(item) for item in contribution_values),
        normalized=normalized_feature_set_from_dict(normalized_value),
        score=_number(data["score"], "FusedCandidate score"),
        score_contributions=_weighted_feature_mapping(data["score_contributions"], "FusedCandidate score_contributions"),
        eligibility=candidate_eligibility_from_dict(eligibility_value),
        schema_version=_integer(data["schema_version"], "FusedCandidate schema_version"),
    )
    return result


def fused_candidate_from_json(value: str) -> FusedCandidate:
    data = _load_mapping(value, "FusedCandidate JSON")
    result = fused_candidate_from_dict(data)
    return result


FusionDecision = dict


def fusion_decision(
    outcome: object,
    selected_candidate: object = MappingProxyType({}),
    selected_candidate_available: object = False,
    response_candidates: object = (),
    evidence: object = (),
    confidence: object = 0.0,
    confidence_available: object = False,
    reason_codes: object = (),
    report: object = MappingProxyType({}),
    working_memory_bytes: object = 0,
    schema_version: object = FUSION_DECISION_SCHEMA_VERSION,
) -> FusionDecision:
    """Build the fusion policy result consumed by resolution orchestration."""
    version = _integer(schema_version, "FusionDecision schema_version")
    if version != FUSION_DECISION_SCHEMA_VERSION:
        raise InvalidRequestError(f"unsupported fusion decision schema_version: {version}")
    if not isinstance(outcome, ResolutionOutcome):
        raise InvalidRequestError("fusion decision outcome must be a ResolutionOutcome")
    if not isinstance(selected_candidate, Mapping):
        raise InvalidRequestError("fusion selected_candidate must be a Candidate")
    try:
        validated_selected_candidate = empty_candidate() if not selected_candidate else validate_candidate(selected_candidate)
    except InvalidRequestError as error:
        raise InvalidRequestError("fusion selected_candidate must be a Candidate") from error
    selected_available = _boolean(selected_candidate_available, "fusion selected_candidate_available")
    if not isinstance(response_candidates, tuple):
        raise InvalidRequestError("fusion response_candidates must be a tuple of Candidate values")
    try:
        validated_response_candidates = tuple(validate_candidate(value) for value in response_candidates)
    except InvalidRequestError as error:
        raise InvalidRequestError("fusion response_candidates must be a tuple of Candidate values") from error
    if len(validated_response_candidates) > MAX_FUSION_CONTRIBUTIONS:
        raise InvalidRequestError("fusion response_candidates exceed the candidate limit")
    if not isinstance(evidence, tuple):
        raise InvalidRequestError("fusion evidence must be a tuple of EvidenceReference values")
    try:
        validated_evidence = tuple(validate_evidence_reference(value) for value in evidence)
    except InvalidRequestError as error:
        raise InvalidRequestError("fusion evidence must be a tuple of EvidenceReference values") from error
    if len(validated_evidence) > MAX_FUSION_CONTRIBUTIONS:
        raise InvalidRequestError("fusion evidence exceeds the evidence limit")
    validated_confidence = _bounded_unit(confidence)
    confidence_is_available = _boolean(confidence_available, "fusion confidence_available")
    if not isinstance(reason_codes, tuple) or not 1 <= len(reason_codes) <= MAX_FUSION_REASON_CODES:
        raise InvalidRequestError("fusion reason_codes must be a bounded non-empty tuple")
    if tuple(dict.fromkeys(reason_codes)) != reason_codes:
        raise InvalidRequestError("fusion reason_codes must be unique and ordered")
    try:
        tuple(FusionPolicyReason(value) for value in reason_codes)
    except (TypeError, ValueError) as error:
        raise InvalidRequestError("fusion decision contains an unsupported reason code") from error
    if not isinstance(report, Mapping) or _contains_none(report):
        raise InvalidRequestError("fusion report must be a concrete no-null object")
    try:
        encoded_report = _json_text(dict(report))
        copied_report = json.loads(encoded_report)
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        raise InvalidRequestError("fusion report must contain deterministic JSON values") from error
    report_size = len(encoded_report.encode("utf-8"))
    if report_size > MAX_FUSION_REPORT_BYTES:
        raise InvalidRequestError("fusion report exceeds its byte limit")
    if not isinstance(copied_report, dict):
        raise InvalidRequestError("fusion report must contain an object")
    working_bytes = _integer(working_memory_bytes, "fusion working_memory_bytes")
    if working_bytes < 0:
        raise InvalidRequestError("fusion working_memory_bytes must be a nonnegative integer")
    if outcome == ResolutionOutcome.ANSWER:
        if not selected_available or validated_response_candidates != (validated_selected_candidate,):
            raise InvalidRequestError("fusion ANSWER requires exactly one selected response candidate")
        if validated_evidence:
            raise InvalidRequestError("fusion ANSWER cannot retain top-level evidence")
        if not confidence_is_available or validated_confidence <= 0.0:
            raise InvalidRequestError("fusion ANSWER requires positive available confidence")
    else:
        if selected_available or validated_selected_candidate != empty_candidate():
            raise InvalidRequestError("non-ANSWER fusion decisions cannot select a candidate")
        if confidence_is_available or validated_confidence != 0.0:
            raise InvalidRequestError("non-ANSWER fusion confidence must be unavailable and zero")
    if outcome == ResolutionOutcome.EVIDENCE and not (validated_response_candidates or validated_evidence):
        raise InvalidRequestError("fusion EVIDENCE requires response candidates or evidence references")
    if outcome == ResolutionOutcome.MISS and (validated_response_candidates or validated_evidence):
        raise InvalidRequestError("fusion MISS cannot retain candidates or evidence")
    result: FusionDecision = {
        "outcome": outcome,
        "selected_candidate": validated_selected_candidate,
        "selected_candidate_available": selected_available,
        "response_candidates": validated_response_candidates,
        "evidence": validated_evidence,
        "confidence": validated_confidence,
        "confidence_available": confidence_is_available,
        "reason_codes": reason_codes,
        "report": MappingProxyType(copied_report),
        "working_memory_bytes": working_bytes,
        "schema_version": version,
    }
    return result


def _trusted_fusion_decision(
    outcome: ResolutionOutcome,
    selected_candidate: Candidate,
    response_candidates: tuple[Candidate, ...],
    evidence: tuple[EvidenceReference, ...],
    confidence: float,
    confidence_available: bool,
    reason_codes: tuple[str, ...],
    report: Mapping[str, object],
    working_memory_bytes: int,
) -> FusionDecision:
    """Build a decision from values whose invariants the engine has established."""
    selected_available = outcome == ResolutionOutcome.ANSWER
    selected = _trusted_candidate_with_changes(selected_candidate, {})
    result: FusionDecision = {
        "outcome": outcome,
        "selected_candidate": selected,
        "selected_candidate_available": selected_available,
        "response_candidates": tuple(_trusted_candidate_with_changes(candidate, {}) for candidate in response_candidates),
        "evidence": evidence,
        "confidence": confidence,
        "confidence_available": confidence_available,
        "reason_codes": reason_codes,
        "report": MappingProxyType(dict(report)),
        "working_memory_bytes": working_memory_bytes,
        "schema_version": FUSION_DECISION_SCHEMA_VERSION,
    }
    return result


def validate_fusion_decision(value: object) -> FusionDecision:
    data = _exact_mapping(value, "FusionDecision", FUSION_DECISION_FIELDS)
    result = fusion_decision(
        data["outcome"],
        data["selected_candidate"],
        data["selected_candidate_available"],
        data["response_candidates"],
        data["evidence"],
        data["confidence"],
        data["confidence_available"],
        data["reason_codes"],
        data["report"],
        data["working_memory_bytes"],
        data["schema_version"],
    )
    return result


def fusion_decision_to_dict(value: object) -> dict[str, object]:
    current = validate_fusion_decision(value)
    selected = candidate_to_dict(current["selected_candidate"]) if current["selected_candidate_available"] else {}
    report = json.loads(_json_text(dict(current["report"])))
    result = {
        "schema_version": current["schema_version"],
        "outcome": current["outcome"].value,
        "selected_candidate": selected,
        "selected_candidate_available": current["selected_candidate_available"],
        "response_candidates": [candidate_to_dict(candidate_value) for candidate_value in current["response_candidates"]],
        "evidence": [evidence_reference_to_dict(reference) for reference in current["evidence"]],
        "confidence": current["confidence"],
        "confidence_available": current["confidence_available"],
        "reason_codes": list(current["reason_codes"]),
        "report": report,
        "working_memory_bytes": current["working_memory_bytes"],
    }
    return result


def fusion_decision_to_json(value: object) -> str:
    data = fusion_decision_to_dict(value)
    result = _json_text(data)
    return result


def fusion_decision_from_dict(value: object) -> FusionDecision:
    data = _exact_mapping(value, "FusionDecision", FUSION_DECISION_FIELDS)
    outcome_value = data["outcome"]
    if not isinstance(outcome_value, str):
        raise InvalidRequestError("FusionDecision outcome must be a string")
    try:
        outcome = ResolutionOutcome(outcome_value)
    except ValueError as error:
        raise InvalidRequestError("FusionDecision outcome is unsupported") from error
    selected_available = _boolean(data["selected_candidate_available"], "FusionDecision selected_candidate_available")
    confidence_available = _boolean(data["confidence_available"], "FusionDecision confidence_available")
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
    selected = candidate_from_dict(selected_value) if selected_available else empty_candidate()
    result = fusion_decision(
        outcome=outcome,
        selected_candidate=selected,
        selected_candidate_available=selected_available,
        response_candidates=tuple(candidate_from_dict(item) for item in response_values),
        evidence=tuple(evidence_reference_from_dict(item) for item in evidence_values),
        confidence=_number(data["confidence"], "FusionDecision confidence"),
        confidence_available=confidence_available,
        reason_codes=tuple(reason_values),
        report=report_value,
        working_memory_bytes=_integer(data["working_memory_bytes"], "FusionDecision working_memory_bytes"),
        schema_version=_integer(data["schema_version"], "FusionDecision schema_version"),
    )
    return result


def fusion_decision_from_json(value: str) -> FusionDecision:
    data = _load_mapping(value, "FusionDecision JSON")
    result = fusion_decision_from_dict(data)
    return result


def normalize_candidate_features(candidate: Candidate) -> NormalizedFeatureSet:
    """Transform resolver-specific observations into comparable policy inputs."""
    validated_candidate = validate_candidate(candidate)
    result = _normalize_validated_candidate_features(validated_candidate)
    return result


def _normalize_validated_candidate_features(validated_candidate: Candidate) -> NormalizedFeatureSet:
    """Normalize a candidate already copied and validated at the engine boundary."""
    raw = validated_candidate["features"]["values"]
    values = dict.fromkeys(FusionFeature, 0.0)
    available: set[FusionFeature] = set()

    def assign(feature: FusionFeature, value: float) -> None:
        values[feature] = _bounded_unit(value)
        available.add(feature)

    if validated_candidate["source"] == CandidateSource.EXACT and "exact_match" in raw:
        assign(FusionFeature.EXACT, raw["exact_match"])
    if (
        validated_candidate["source"] == CandidateSource.UTILITY
        and validated_candidate["provenance"].get("producer") == UTILITY_RESOLVER_VERSION
        and "utility_match" in raw
    ):
        assign(FusionFeature.EXACT, raw["utility_match"])
    if validated_candidate["source"] == CandidateSource.PATTERN and "pattern_specificity" in raw:
        assign(FusionFeature.PATTERN, _saturating(raw["pattern_specificity"], 4.0))
    if validated_candidate["source"] in (CandidateSource.LEXICAL, CandidateSource.SPARSE):
        if "sparse_score" in raw:
            assign(FusionFeature.LEXICAL, raw["sparse_score"])
        elif "lexical_score" in raw:
            assign(FusionFeature.LEXICAL, raw["lexical_score"])
        elif "lexical_overlap" in raw:
            assign(FusionFeature.LEXICAL, raw["lexical_overlap"])
        elif "score" in raw:
            assign(FusionFeature.LEXICAL, raw["score"])
    if validated_candidate["source"] in (CandidateSource.SUPPORT_SEMANTIC, CandidateSource.STANDALONE_SEMANTIC) and (
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
    if validated_candidate["source"] == CandidateSource.SUPPORT_SEMANTIC and "support_coverage" in raw:
        assign(FusionFeature.SUPPORT, raw["support_coverage"])
    elif validated_candidate["evidence"]:
        support_available = any(item["kind"] == EvidenceKind.SUPPORT for item in validated_candidate["evidence"])
        assign(FusionFeature.SUPPORT, float(support_available))
    if validated_candidate["source"] == CandidateSource.LEXICAL and "recency" in raw:
        assign(FusionFeature.FRESHNESS, raw["recency"])
    if validated_candidate["source"] in (CandidateSource.SUPPORT_SEMANTIC, CandidateSource.STANDALONE_SEMANTIC) and (
        "authority_score" in raw
    ):
        assign(FusionFeature.AUTHORITY, raw["authority_score"])
    ordered_available = tuple(feature for feature in FusionFeature if feature in available)
    result = _trusted_normalized_feature_set(values, ordered_available)
    return result


def merge_candidate_eligibility(first: CandidateEligibility, second: CandidateEligibility) -> CandidateEligibility:
    """Merge structural and authoritative candidate eligibility."""
    validated_first = validate_candidate_eligibility(first)
    validated_second = validate_candidate_eligibility(second)
    result = _merge_validated_candidate_eligibility(validated_first, validated_second)
    return result


def _merge_validated_candidate_eligibility(
    validated_first: CandidateEligibility,
    validated_second: CandidateEligibility,
) -> CandidateEligibility:
    """Merge eligibility values already owned by the fusion engine."""
    reasons = tuple(dict.fromkeys((*validated_first["reason_codes"], *validated_second["reason_codes"])))
    values = dict(validated_first["feature_values"])
    values.update(validated_second["feature_values"])
    available = tuple(
        feature
        for feature in FusionFeature
        if feature in set(validated_first["feature_available"]).union(validated_second["feature_available"])
    )
    result = _trusted_candidate_eligibility(
        validated_first["score_eligible"] and validated_second["score_eligible"],
        validated_first["evidence_eligible"] and validated_second["evidence_eligible"],
        validated_first["answer_eligible"] and validated_second["answer_eligible"],
        reasons,
        values,
        available,
    )
    return result


def canonical_candidate_key(candidate: Candidate) -> tuple[int, str, str]:
    """Return the deterministic ordering key for one candidate."""
    candidate = validate_candidate(candidate)
    result = _trusted_canonical_candidate_key(candidate)
    return result


def _trusted_canonical_candidate_key(candidate: Candidate) -> tuple[int, str, str]:
    """Order a candidate already validated at the fusion boundary."""
    result = (
        FUSION_SOURCE_ORDER[candidate["source"]],
        candidate["candidate_id"],
        _trusted_candidate_to_json(candidate),
    )
    return result


def dedupe_evidence(
    evidence: tuple[EvidenceReference, ...],
    scope: ScopeKey,
) -> tuple[tuple[EvidenceReference, ...], int]:
    """Retain unique in-scope evidence and count conflicting identifiers."""
    groups: dict[str, dict[str, EvidenceReference]] = {}
    for reference in evidence:
        if reference["scope"] != scope:
            continue
        groups.setdefault(reference["evidence_id"], {})[evidence_reference_to_json(reference)] = reference
    retained = []
    conflict_count = 0
    for evidence_id in sorted(groups):
        variants = groups[evidence_id]
        if len(variants) != 1:
            conflict_count += 1
            continue
        retained.append(variants[sorted(variants)[0]])
    result = tuple(retained), conflict_count
    return result


def apply_authoritative_features(
    normalized: NormalizedFeatureSet,
    eligibility: CandidateEligibility,
) -> NormalizedFeatureSet:
    """Overlay authoritative feature values on normalized producer features."""
    normalized = validate_normalized_feature_set(normalized)
    eligibility = validate_candidate_eligibility(eligibility)
    result = _apply_validated_authoritative_features(normalized, eligibility)
    return result


def _apply_validated_authoritative_features(
    normalized: NormalizedFeatureSet,
    eligibility: CandidateEligibility,
) -> NormalizedFeatureSet:
    """Overlay authority values already validated at the fusion boundary."""
    values = dict(normalized["values"])
    available = set(normalized["available"])
    for feature in eligibility["feature_available"]:
        values[feature] = eligibility["feature_values"][feature]
        available.add(feature)
    ordered_available = tuple(feature for feature in FusionFeature if feature in available)
    result = _trusted_normalized_feature_set(values, ordered_available)
    return result


class CandidateFusionEngine:
    """Deterministic, bounded, non-generative first-generation fusion engine."""

    def __init__(
        self,
        policy: object = {},
        authority: CandidateAuthority = abstaining_candidate_authority,
        reranker: object = (),
    ) -> None:
        policy_value = fusion_policy() if policy == {} else policy
        try:
            validated_policy = validate_fusion_policy(policy_value)
        except InvalidRequestError as error:
            raise InvalidRequestError("fusion policy must be a FusionPolicy") from error
        if not callable(authority):
            raise InvalidRequestError("fusion authority must be callable")
        if reranker != () and not isinstance(reranker, TransparentLogisticReranker):
            raise InvalidRequestError("fusion reranker must be a TransparentLogisticReranker")
        self.policy = validated_policy
        self._authority = authority
        self._reranker = reranker

    def _individual_eligibility(
        self,
        candidate: Candidate,
        frame: QueryFrame,
        normalized: NormalizedFeatureSet,
        candidate_id_conflict: bool,
    ) -> CandidateEligibility:
        if candidate_id_conflict:
            result = candidate_eligibility(False, False, False, (FusionPolicyReason.CANDIDATE_ID_CONFLICT,))
            return result
        if candidate["scope"] != frame["scope"]:
            result = candidate_eligibility(False, False, False, (FusionPolicyReason.CANDIDATE_SCOPE_MISMATCH,))
            return result
        if candidate["lifecycle"] != LifecycleState.ACTIVE:
            result = candidate_eligibility(False, False, False, (FusionPolicyReason.CANDIDATE_LIFECYCLE_INELIGIBLE,))
            return result
        if any(reference["scope"] != frame["scope"] for reference in candidate["evidence"]):
            result = candidate_eligibility(
                False,
                False,
                False,
                (FusionPolicyReason.CANDIDATE_EVIDENCE_SCOPE_MISMATCH,),
            )
            return result
        reasons = []
        answer_eligible = True
        if candidate["features"]["values"].get("explicit_conflict", 0.0) > 0.0:
            answer_eligible = False
            reasons.append(FusionPolicyReason.EXPLICIT_CONFLICT)
        if FusionFeature.SUPPORT in normalized["available"] and normalized["values"][FusionFeature.SUPPORT] == 0.0:
            answer_eligible = False
            reasons.append(FusionPolicyReason.SUPPORT_INCOMPLETE)
        structural = candidate_eligibility(True, True, answer_eligible, tuple(reasons))
        authority = self._authority(candidate, frame)
        result = _merge_validated_candidate_eligibility(structural, authority)
        return result

    def _score(self, normalized: NormalizedFeatureSet) -> tuple[float, Mapping[FusionFeature, float]]:
        weighted = dict.fromkeys(FusionFeature, 0.0)
        numerator = 0.0
        denominator = 0.0
        for feature in normalized["available"]:
            if feature in (FusionFeature.EXACT, FusionFeature.MARGIN):
                continue
            contribution = self.policy["weights"][feature] * normalized["values"][feature]
            weighted[feature] = contribution
            numerator += contribution
            denominator += self.policy["weights"][feature]
        base = numerator / denominator if denominator else 0.0
        exact = normalized["values"][FusionFeature.EXACT] if FusionFeature.EXACT in normalized["available"] else 0.0
        weighted[FusionFeature.EXACT] = exact
        score = exact + (1.0 - exact) * base
        bounded_score = _bounded_unit(score)
        result = (bounded_score, MappingProxyType(weighted))
        return result

    def _fuse_group(
        self,
        statement_id: str,
        contributions: tuple[FusionContribution, ...],
        frame: QueryFrame,
    ) -> FusedCandidate:
        active = tuple(contribution for contribution in contributions if contribution["eligibility"]["score_eligible"])
        representative = (active or contributions)[0]["candidate"]
        if active:
            eligibility = active[0]["eligibility"]
            for contribution in active[1:]:
                eligibility = _merge_validated_candidate_eligibility(eligibility, contribution["eligibility"])
        else:
            eligibility = contributions[0]["eligibility"]
            for contribution in contributions[1:]:
                eligibility = _merge_validated_candidate_eligibility(eligibility, contribution["eligibility"])
        excluded_reasons = tuple(
            reason
            for contribution in contributions
            if not contribution["eligibility"]["score_eligible"]
            for reason in contribution["eligibility"]["reason_codes"]
        )
        if active and excluded_reasons:
            eligibility = _trusted_candidate_eligibility(
                eligibility["score_eligible"],
                eligibility["evidence_eligible"],
                eligibility["answer_eligible"],
                tuple(dict.fromkeys((*eligibility["reason_codes"], *excluded_reasons))),
                eligibility["feature_values"],
                eligibility["feature_available"],
            )
        if active and len({contribution["candidate"]["response"] for contribution in active}) != 1:
            eligibility = _trusted_candidate_eligibility(
                False,
                False,
                False,
                tuple(dict.fromkeys((*eligibility["reason_codes"], FusionPolicyReason.CANDIDATE_STATEMENT_CONFLICT))),
                eligibility["feature_values"],
                eligibility["feature_available"],
            )
            active = ()
        values = dict.fromkeys(FusionFeature, 0.0)
        available: set[FusionFeature] = set()
        for feature in FusionFeature:
            observed = [
                contribution["normalized"]["values"][feature]
                for contribution in active
                if feature in contribution["normalized"]["available"]
            ]
            if observed:
                values[feature] = min(observed) if feature in FUSION_CONSERVATIVE_MINIMUM else max(observed)
                available.add(feature)
        sources = tuple(dict.fromkeys(contribution["candidate"]["source"] for contribution in active))
        families = tuple(dict.fromkeys(FUSION_SOURCE_FAMILY[source] for source in sources))
        if active:
            values[FusionFeature.AGREEMENT] = min(1.0, max(0.0, (len(families) - 1) / 2.0))
            available.add(FusionFeature.AGREEMENT)
        active_evidence = tuple(
            reference
            for contribution in active
            if FusionPolicyReason.SUPPORT_REFERENCE_STALE not in contribution["eligibility"]["reason_codes"]
            for reference in contribution["candidate"]["evidence"]
        )
        retained_evidence, evidence_conflicts = dedupe_evidence(active_evidence, frame["scope"])
        support_present = any(reference["kind"] == EvidenceKind.SUPPORT for reference in retained_evidence)
        if active:
            values[FusionFeature.SUPPORT] = max(values[FusionFeature.SUPPORT], float(support_present))
            available.add(FusionFeature.SUPPORT)
        reasons = list(eligibility["reason_codes"])
        answer_eligible = eligibility["answer_eligible"]
        exact_present = CandidateSource.EXACT in sources and values[FusionFeature.EXACT] == 1.0
        utility_present = (
            any(
                contribution["candidate"]["source"] == CandidateSource.UTILITY
                and contribution["candidate"]["provenance"].get("producer") == UTILITY_RESOLVER_VERSION
                for contribution in active
            )
            and values[FusionFeature.EXACT] == 1.0
        )
        deterministic_present = exact_present or utility_present
        if evidence_conflicts:
            answer_eligible = False
            reasons.append(FusionPolicyReason.EVIDENCE_REFERENCE_CONFLICT)
        if active and not deterministic_present and len(families) < self.policy["minimum_independent_sources"]:
            answer_eligible = False
            reasons.append(FusionPolicyReason.INDEPENDENT_SOURCES_MISSING)
        if active and not deterministic_present and self.policy["require_support_for_non_exact"] and not support_present:
            answer_eligible = False
            reasons.append(FusionPolicyReason.SUPPORT_INCOMPLETE)
        identity_features = (FusionFeature.ENTITY, FusionFeature.RELATION)
        if any(feature in available and values[feature] == 0.0 for feature in identity_features):
            answer_eligible = False
            reasons.append(FusionPolicyReason.IDENTITY_FEATURE_MISMATCH)
        if (
            active
            and not exact_present
            and frame["expected_object_type"] != ExpectedObjectType.UNKNOWN
            and (FusionFeature.OBJECT_TYPE not in available or values[FusionFeature.OBJECT_TYPE] == 0.0)
        ):
            answer_eligible = False
            reasons.append(FusionPolicyReason.OBJECT_TYPE_FEATURE_MISMATCH)
        eligibility = _trusted_candidate_eligibility(
            eligibility["score_eligible"],
            eligibility["evidence_eligible"],
            answer_eligible,
            tuple(dict.fromkeys(reasons)),
            eligibility["feature_values"],
            eligibility["feature_available"],
        )
        normalized = _trusted_normalized_feature_set(values, tuple(feature for feature in FusionFeature if feature in available))
        score, score_contributions = self._score(normalized)
        digest = hashlib.sha256(f"{self.policy['policy_version']}:{frame['diagnostic_id']}:{statement_id}".encode()).hexdigest()
        fused = _trusted_candidate(
            candidate_id=f"fused:sha256:{digest}",
            statement_id=statement_id,
            response=representative["response"],
            source=representative["source"],
            features=_trusted_feature_set(
                values={feature.value: values[feature] for feature in normalized["available"]},
                unavailable=tuple(sorted(feature.value for feature in FusionFeature if feature not in normalized["available"])),
            ),
            evidence=retained_evidence,
            scope=representative["scope"],
            lifecycle=representative["lifecycle"],
            provenance={
                **dict(representative["provenance"]),
                "fusion_policy_version": self.policy["policy_version"],
                "resolver_sources": [source.value for source in sources],
                "resolver_families": list(families),
            },
            diagnostics={"fusion_contribution_count": len(contributions)},
        )
        result = _trusted_fused_candidate(fused, contributions, normalized, score, score_contributions, eligibility)
        return result

    def _exhausted_decision(
        self,
        frame: QueryFrame,
        candidates: tuple[Candidate, ...],
        evidence: tuple[EvidenceReference, ...],
        reason: FusionPolicyReason,
        working_memory_bytes: int = 0,
    ) -> FusionDecision:
        retained_evidence, evidence_conflicts = dedupe_evidence(evidence, frame["scope"])
        outcome = ResolutionOutcome.EVIDENCE if retained_evidence else ResolutionOutcome.MISS
        report = {
            "policy_version": self.policy["policy_version"],
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
        result = _trusted_fusion_decision(
            outcome,
            empty_candidate(),
            (),
            retained_evidence,
            0.0,
            False,
            tuple(reasons),
            report,
            working_memory_bytes,
        )
        return result

    def decide(
        self,
        frame: QueryFrame,
        candidates: tuple[Candidate, ...],
        evidence: tuple[EvidenceReference, ...] = (),
        *,
        working_memory_limit: int = 0,
        working_memory_limit_available: bool = False,
        cooperative_check: object = (),
    ) -> FusionDecision:
        frame = validate_query_frame(frame)
        if not isinstance(candidates, tuple):
            raise InvalidRequestError("fusion candidates must be a tuple of Candidate values")
        candidates = tuple(validate_candidate(value) for value in candidates)
        if len(candidates) > MAX_FUSION_CONTRIBUTIONS:
            raise InvalidRequestError(f"fusion candidates exceed the limit of {MAX_FUSION_CONTRIBUTIONS}")
        if not isinstance(evidence, tuple):
            raise InvalidRequestError("fusion evidence must be a tuple of EvidenceReference values")
        evidence = tuple(validate_evidence_reference(value) for value in evidence)
        if len(evidence) > MAX_FUSION_CONTRIBUTIONS:
            raise InvalidRequestError(f"fusion evidence exceeds the limit of {MAX_FUSION_CONTRIBUTIONS}")
        if not isinstance(working_memory_limit_available, bool):
            raise InvalidRequestError("fusion working_memory_limit_available must be a boolean")
        if isinstance(working_memory_limit, bool) or not isinstance(working_memory_limit, int) or working_memory_limit < 0:
            raise InvalidRequestError("fusion working_memory_limit must be a nonnegative integer")
        if not working_memory_limit_available and working_memory_limit != 0:
            raise InvalidRequestError("fusion working_memory_limit requires availability")
        if working_memory_limit_available and working_memory_limit > frame["budget"]["max_working_memory_bytes"]:
            raise InvalidRequestError("fusion working_memory_limit exceeds the frame budget")
        if cooperative_check != () and not callable(cooperative_check):
            raise InvalidRequestError("fusion cooperative_check must be callable")
        memory_limit = working_memory_limit if working_memory_limit_available else frame["budget"]["max_working_memory_bytes"]
        serialized_candidates = tuple((candidate, _trusted_candidate_to_json(candidate)) for candidate in candidates)
        working_bytes = sum(len(value.encode("utf-8")) for _, value in serialized_candidates) + sum(
            len(evidence_reference_to_json(reference).encode("utf-8")) for reference in evidence
        )
        if working_bytes > memory_limit:
            result = self._exhausted_decision(
                frame,
                candidates,
                evidence,
                FusionPolicyReason.FUSION_MEMORY_EXHAUSTED,
                memory_limit,
            )
            return result
        candidate_variants: dict[str, set[str]] = {}
        for candidate_value, serialized in serialized_candidates:
            candidate_variants.setdefault(candidate_value["candidate_id"], set()).add(serialized)
        conflicting_candidate_ids = {candidate_id for candidate_id, variants in candidate_variants.items() if len(variants) > 1}
        ordered = tuple(sorted(candidates, key=_trusted_canonical_candidate_key))
        contribution_groups: dict[str, list[FusionContribution]] = {}
        for candidate in ordered:
            normalized = _normalize_validated_candidate_features(candidate)
            eligibility = self._individual_eligibility(
                candidate,
                frame,
                normalized,
                candidate["candidate_id"] in conflicting_candidate_ids,
            )
            normalized = _apply_validated_authoritative_features(normalized, eligibility)
            contribution = _trusted_fusion_contribution(candidate, normalized, eligibility)
            contribution_groups.setdefault(candidate["statement_id"], []).append(contribution)
            contribution_json = _trusted_fusion_contribution_to_json(contribution)
            working_bytes += len(contribution_json.encode("utf-8"))
            if working_bytes > memory_limit:
                result = self._exhausted_decision(
                    frame,
                    candidates,
                    evidence,
                    FusionPolicyReason.FUSION_MEMORY_EXHAUSTED,
                    memory_limit,
                )
                return result
        fused_values = []
        for statement_id in sorted(contribution_groups):
            fused_value = self._fuse_group(statement_id, tuple(contribution_groups[statement_id]), frame)
            fused_values.append(fused_value)
            fused_json = _trusted_fused_candidate_to_json(fused_value)
            working_bytes += len(fused_json.encode("utf-8"))
            if working_bytes > memory_limit:
                result = self._exhausted_decision(
                    frame,
                    candidates,
                    evidence,
                    FusionPolicyReason.FUSION_MEMORY_EXHAUSTED,
                    memory_limit,
                )
                return result
        fused = tuple(fused_values)
        reranker_report: dict[str, object] = {
            "applied": False,
            "reason": "not_configured",
            "model_version": "",
            "elapsed_ns": 0,
            "model_time_target_exceeded": False,
            "input_bytes": 0,
            "scores": [],
        }
        if isinstance(self._reranker, TransparentLogisticReranker) and self._reranker.enabled:
            eligible_shortlist = sorted(
                (value for value in fused if value["eligibility"]["score_eligible"]),
                key=lambda value: (-value["score"], value["candidate"]["statement_id"]),
            )[: self._reranker.settings["shortlist_size"]]
            shortlist = tuple(
                {
                    "statement_id": item["candidate"]["statement_id"],
                    "base_score": item["score"],
                    "features": {
                        name: (
                            item["score"] if name == "base_score" else item["normalized"]["values"].get(FusionFeature(name), 0.0)
                        )
                        for name in RERANKER_FEATURES
                    },
                }
                for item in eligible_shortlist
            )
            try:
                reranker_report = self._reranker.rerank(shortlist, cooperative_check)
            except ResolutionCancelledError:
                raise
            except Exception as error:
                self._reranker.record_fallback("reranker_exception")
                reranker_report = {
                    "applied": False,
                    "reason": "reranker_exception",
                    "exception_type": type(error).__name__,
                    "model_version": self._reranker.settings["model_version"],
                    "elapsed_ns": 0,
                    "model_time_target_exceeded": False,
                    "input_bytes": 0,
                    "scores": [],
                }
            if reranker_report["applied"]:
                reranked_scores = {value["statement_id"]: value["score"] for value in reranker_report["scores"]}
                updated_fused = []
                for item in fused:
                    statement_id = item["candidate"]["statement_id"]
                    if statement_id not in reranked_scores:
                        updated_fused.append(item)
                        continue
                    rerank_score = reranked_scores[statement_id]
                    updated_candidate = _trusted_candidate_with_changes(
                        item["candidate"],
                        {
                            "provenance": {
                                **dict(item["candidate"]["provenance"]),
                                "reranker_implementation": self._reranker.settings["implementation"],
                                "reranker_model_version": self._reranker.settings["model_version"],
                            },
                            "diagnostics": {
                                **dict(item["candidate"]["diagnostics"]),
                                "fusion_base_score": item["score"],
                                "reranker_score": rerank_score,
                            },
                        },
                    )
                    updated_fused.append(
                        _trusted_fused_candidate(
                            updated_candidate,
                            item["contributions"],
                            item["normalized"],
                            rerank_score,
                            item["score_contributions"],
                            item["eligibility"],
                        )
                    )
                fused = tuple(updated_fused)
        ranked = tuple(
            sorted(
                (candidate for candidate in fused if candidate["eligibility"]["score_eligible"]),
                key=lambda item: (-item["score"], item["candidate"]["statement_id"]),
            )
        )
        if ranked:
            margin_available = len(ranked) > 1
            margin = ranked[0]["score"] - ranked[1]["score"] if margin_available else 1.0
            top = ranked[0]
            values = dict(top["normalized"]["values"])
            values[FusionFeature.MARGIN] = margin if margin_available else 0.0
            available = set(top["normalized"]["available"])
            if margin_available:
                available.add(FusionFeature.MARGIN)
            top_normalized = _trusted_normalized_feature_set(
                values, tuple(feature for feature in FusionFeature if feature in available)
            )
            top_candidate = _trusted_candidate_with_changes(
                top["candidate"],
                {
                    "features": feature_set(
                        values={feature.value: values[feature] for feature in available},
                        unavailable=tuple(sorted(feature.value for feature in FusionFeature if feature not in available)),
                    )
                },
            )
            top = _trusted_fused_candidate(
                top_candidate,
                top["contributions"],
                top_normalized,
                top["score"],
                top["score_contributions"],
                top["eligibility"],
            )
            ranked = (top, *ranked[1:])
        else:
            margin_available = False
            margin = 0.0
        evidence_candidates = tuple(
            item
            for item in ranked
            if item["eligibility"]["evidence_eligible"] and item["score"] >= self.policy["evidence_threshold"]
        )
        reasons: list[str] = []
        selected = empty_candidate()
        confidence = 0.0
        confidence_available = False
        response_candidates: tuple[Candidate, ...] = ()
        retained_evidence, top_level_evidence_conflicts = dedupe_evidence(evidence, frame["scope"])
        if ranked and ranked[0]["eligibility"]["answer_eligible"] and ranked[0]["score"] >= self.policy["answer_threshold"]:
            top = ranked[0]
            if margin_available and margin < self.policy["ambiguity_margin"]:
                outcome = ResolutionOutcome.EVIDENCE
                response_candidates = tuple(item["candidate"] for item in evidence_candidates)
                reasons.extend(
                    (
                        FusionPolicyReason.AMBIGUOUS_TOP_CANDIDATES.value,
                        FusionPolicyReason.EVIDENCE_THRESHOLD_MET.value,
                    )
                )
            else:
                outcome = ResolutionOutcome.ANSWER
                selected = top["candidate"]
                response_candidates = (selected,)
                retained_evidence = ()
                confidence = top["score"]
                confidence_available = True
                reasons.append(
                    FusionPolicyReason.ANSWER_EXACT_ELIGIBLE.value
                    if selected["source"] == CandidateSource.EXACT
                    else FusionPolicyReason.ANSWER_FUSION_THRESHOLD.value
                )
                reasons.append(
                    FusionPolicyReason.ANSWER_MARGIN_CLEAR.value
                    if margin_available
                    else FusionPolicyReason.ANSWER_NO_RUNNER_UP.value
                )
        elif evidence_candidates or retained_evidence:
            outcome = ResolutionOutcome.EVIDENCE
            response_candidates = tuple(item["candidate"] for item in evidence_candidates)
            if ranked:
                top = ranked[0]
                reasons.append(
                    FusionPolicyReason.ANSWER_ELIGIBILITY_PREVENTED.value
                    if not top["eligibility"]["answer_eligible"]
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
        ranked_ids = {candidate["candidate"]["statement_id"] for candidate in ranked}
        report_values = (
            *ranked,
            *(candidate for candidate in fused if candidate["candidate"]["statement_id"] not in ranked_ids),
        )
        visible_report_values = report_values[: self.policy["max_report_candidates"]]
        candidate_reports = [_trusted_fused_candidate_to_report_dict(candidate) for candidate in visible_report_values]

        def make_report() -> dict[str, object]:
            result = {
                "policy": fusion_policy_to_dict(self.policy),
                "policy_fingerprint": policy_fingerprint(self.policy),
                "input_candidate_count": len(candidates),
                "candidate_count": len(fused),
                "omitted_candidate_count": len(report_values) - len(candidate_reports),
                "top_two_margin": margin,
                "top_two_margin_available": margin_available,
                "evidence_conflict_count": top_level_evidence_conflicts,
                "budget_exhausted": "",
                "reranker": reranker_report,
                "candidates": candidate_reports,
            }
            return result

        report = make_report()
        report_limit = min(MAX_FUSION_REPORT_BYTES, max(512, frame["budget"]["max_diagnostic_bytes"]))
        while candidate_reports and len(_json_text(report).encode("utf-8")) > report_limit:
            candidate_reports.pop()
            report = make_report()
        if len(_json_text(report).encode("utf-8")) > report_limit:
            report = {
                "policy_version": self.policy["policy_version"],
                "policy_fingerprint": policy_fingerprint(self.policy),
                "input_candidate_count": len(candidates),
                "candidate_count": len(fused),
                "omitted_candidate_count": len(report_values),
                "top_two_margin": margin,
                "top_two_margin_available": margin_available,
                "evidence_conflict_count": top_level_evidence_conflicts,
                "budget_exhausted": "",
                "reranker": {
                    "applied": reranker_report["applied"],
                    "reason": reranker_report["reason"],
                    "model_version": reranker_report["model_version"],
                },
                "candidates": [],
            }
        working_bytes += len(_json_text(report).encode("utf-8"))
        if working_bytes > memory_limit:
            result = self._exhausted_decision(
                frame,
                candidates,
                evidence,
                FusionPolicyReason.FUSION_MEMORY_EXHAUSTED,
                memory_limit,
            )
            return result
        result = _trusted_fusion_decision(
            outcome,
            selected,
            response_candidates,
            retained_evidence,
            confidence,
            confidence_available,
            tuple(dict.fromkeys(reasons)),
            report,
            working_bytes,
        )
        return result


def policy_fingerprint(policy: FusionPolicy) -> str:
    """Return a stable release/evidence fingerprint without hidden state."""
    data = fusion_policy_to_dict(policy)
    encoded = json.dumps(data, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    result = hashlib.sha256(encoded).hexdigest()
    return result
