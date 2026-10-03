"""Versioned candidate normalization, fusion, eligibility, and ambiguity policy."""

from hashlib import sha256 as hashlib_sha256
from json import dumps as json_dumps
from logging import getLogger as logging_getLogger
from math import isfinite as math_isfinite

from engram.artifacts import LifecycleState
from engram.constants import (
    CANDIDATE_ELIGIBILITY_FIELDS,
    DEFAULT_FUSION_WEIGHTS,
    FUSION_AMBIGUITY_MARGIN,
    FUSION_ANSWER_THRESHOLD,
    FUSION_CONSERVATIVE_MINIMUM,
    FUSION_EVIDENCE_THRESHOLD,
    FUSION_MINIMUM_INDEPENDENT_SOURCES,
    FUSION_POLICY_FIELDS,
    FUSION_REQUIRE_SUPPORT_FOR_NON_EXACT,
    FUSION_SOURCE_FAMILY,
    FUSION_SOURCE_ORDER,
    MAX_FUSION_CONTRIBUTIONS,
    MAX_FUSION_INDEPENDENT_SOURCES,
    MAX_FUSION_REPORT_BYTES,
    MAX_FUSION_REPORT_CANDIDATES,
    MAX_FUSION_REPORT_CONTRIBUTIONS,
    MIN_FUSION_INDEPENDENT_SOURCES,
    UTILITY_RESOLVER_PRODUCER,
    FusionFeature,
    FusionPolicyReason,
    GraphCompositionOperator,
    PredicateCardinality,
    RelationSelectionReason,
)
from engram.eligibility import evaluate_artifact_eligibility
from engram.errors import InvalidRequestError, ResolutionCancelledError
from engram.evidence import PropositionEligibilityEvaluator, assertion_basis_window
from engram.feedback import FeedbackStore, canonical_fingerprint, constraint_fingerprint
from engram.graph import PropositionProjectionQuery, validate_proposition_projection
from engram.relation import phrase_relation_result
from engram.reranking import RERANKER_FEATURES, TransparentLogisticReranker
from engram.resolution import (
    CandidateSource,
    EvidenceKind,
    ExpectedObjectType,
    ResolutionOutcome,
    empty_candidate,
    evidence_reference_to_json,
    feature_set,
    trusted_candidate,
    trusted_candidate_to_dict,
    trusted_candidate_to_json,
    trusted_candidate_with_changes,
    trusted_feature_set,
    validate_candidate,
    validate_evidence_reference,
    validate_query_frame,
)
from engram.utilities import evaluate_named_utility
from engram.validation import require_bool

logger = logging_getLogger(__name__)

EMPTY_FEATURE_VALUES = {}


def bounded_unit(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math_isfinite(float(value)):
        raise InvalidRequestError("fusion feature values must be finite numbers")
    result = min(1.0, max(0.0, float(value)))
    return result


def policy_unit(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math_isfinite(float(value)):
        raise InvalidRequestError(f"{name} must be a finite number")
    selected = float(value)
    if not 0.0 <= selected <= 1.0:
        raise InvalidRequestError(f"{name} must be between 0 and 1")
    return selected


def json_text(value: dict) -> str:
    result = json_dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    return result


def exact_mapping(value: object, name: str, fields: set[str]) -> dict:
    if not isinstance(value, dict) or set(value) != fields or not all(isinstance(key, str) for key in value):
        raise InvalidRequestError(f"{name} has invalid fields")
    return value


def contains_none(value: object) -> bool:
    if value is None:
        result = True
        return result
    if isinstance(value, dict):
        result = any(contains_none(key) or contains_none(item) for key, item in value.items())
        return result
    if isinstance(value, (list, tuple)):
        result = any(contains_none(item) for item in value)
        return result
    result = False
    return result


def internal_integer(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise InvalidRequestError(f"{name} must be an integer")
    return value


def internal_number(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math_isfinite(float(value)):
        raise InvalidRequestError(f"{name} must be a finite number")
    result = float(value)
    return result


def trusted_normalized_feature_set_to_dict(current: dict) -> dict:
    """Serialize an engine-owned normalized feature set."""
    values = current.get("values", {})
    result = {
        "values": {feature.value: values.get(feature, 0.0) for feature in FusionFeature},
        "available": [feature.value for feature in current.get("available", ())],
    }
    return result


def fusion_policy(
    weights: object = DEFAULT_FUSION_WEIGHTS,
    answer_threshold: object = FUSION_ANSWER_THRESHOLD,
    evidence_threshold: object = FUSION_EVIDENCE_THRESHOLD,
    ambiguity_margin: object = FUSION_AMBIGUITY_MARGIN,
    minimum_independent_sources: object = FUSION_MINIMUM_INDEPENDENT_SOURCES,
    require_support_for_non_exact: object = FUSION_REQUIRE_SUPPORT_FOR_NON_EXACT,
    max_report_candidates: object = MAX_FUSION_REPORT_CANDIDATES,
) -> dict:
    """Validate and normalize the configurable first-generation linear fusion policy."""
    if not isinstance(weights, dict) or set(weights) != set(FusionFeature):
        raise InvalidRequestError("fusion weights must contain every canonical feature")
    normalized_weights = {}
    for feature in FusionFeature:
        value = weights.get(feature, 0.0)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math_isfinite(float(value)) or value < 0:
            raise InvalidRequestError("fusion weights must be finite nonnegative numbers")
        normalized_weights[feature] = float(value)
    if normalized_weights.get(FusionFeature.MARGIN, 0.0) != 0.0:
        raise InvalidRequestError("margin is a decision feature and must have zero scoring weight")
    if not any(weight for feature, weight in normalized_weights.items() if feature != FusionFeature.MARGIN):
        raise InvalidRequestError("fusion policy must have a positive scoring weight")
    answer = policy_unit(answer_threshold, "fusion answer_threshold")
    evidence = policy_unit(evidence_threshold, "fusion evidence_threshold")
    margin = policy_unit(ambiguity_margin, "fusion ambiguity_margin")
    if evidence > answer:
        raise InvalidRequestError("fusion evidence_threshold cannot exceed answer_threshold")
    independent_sources = internal_integer(minimum_independent_sources, "minimum_independent_sources")
    if not MIN_FUSION_INDEPENDENT_SOURCES <= independent_sources <= MAX_FUSION_INDEPENDENT_SOURCES:
        raise InvalidRequestError(
            f"minimum_independent_sources must be from {MIN_FUSION_INDEPENDENT_SOURCES} through {MAX_FUSION_INDEPENDENT_SOURCES}"
        )
    support_required = require_bool(require_support_for_non_exact, "require_support_for_non_exact")
    report_candidates = internal_integer(max_report_candidates, "max_report_candidates")
    if not 1 <= report_candidates <= MAX_FUSION_REPORT_CANDIDATES:
        raise InvalidRequestError(f"max_report_candidates must be from 1 through {MAX_FUSION_REPORT_CANDIDATES}")
    result: dict = {
        "weights": dict(normalized_weights),
        "answer_threshold": answer,
        "evidence_threshold": evidence,
        "ambiguity_margin": margin,
        "minimum_independent_sources": independent_sources,
        "require_support_for_non_exact": support_required,
        "max_report_candidates": report_candidates,
    }
    return result


def validate_fusion_policy(value: object) -> dict:
    data = exact_mapping(value, "FusionPolicy", FUSION_POLICY_FIELDS)
    result = fusion_policy(
        data.get("weights", {}),
        data.get("answer_threshold", 0.0),
        data.get("evidence_threshold", 0.0),
        data.get("ambiguity_margin", 0.0),
        data.get("minimum_independent_sources", 0),
        data.get("require_support_for_non_exact", False),
        data.get("max_report_candidates", 0),
    )
    return result


def fusion_policy_to_dict(value: object) -> dict:
    current = validate_fusion_policy(value)
    weights = current.get("weights", {})
    result = {
        "weights": {feature.value: weights.get(feature, 0.0) for feature in FusionFeature},
        "answer_threshold": current.get("answer_threshold", 0.0),
        "evidence_threshold": current.get("evidence_threshold", 0.0),
        "ambiguity_margin": current.get("ambiguity_margin", 0.0),
        "minimum_independent_sources": current.get("minimum_independent_sources", 0),
        "require_support_for_non_exact": current.get("require_support_for_non_exact", False),
        "max_report_candidates": current.get("max_report_candidates", 0),
    }
    return result


def fusion_policy_from_dict(value: object) -> dict:
    data = exact_mapping(value, "FusionPolicy", FUSION_POLICY_FIELDS)
    raw_weights = data.get("weights", {})
    if not isinstance(raw_weights, dict) or set(raw_weights) != {feature.value for feature in FusionFeature}:
        raise InvalidRequestError("FusionPolicy weights have invalid fields")
    weights = {
        feature: internal_number(raw_weights.get(feature.value, 0.0), f"FusionPolicy {feature.value} weight")
        for feature in FusionFeature
    }
    result = fusion_policy(
        weights=weights,
        answer_threshold=internal_number(data.get("answer_threshold", 0.0), "FusionPolicy answer_threshold"),
        evidence_threshold=internal_number(data.get("evidence_threshold", 0.0), "FusionPolicy evidence_threshold"),
        ambiguity_margin=internal_number(data.get("ambiguity_margin", 0.0), "FusionPolicy ambiguity_margin"),
        minimum_independent_sources=internal_integer(
            data.get("minimum_independent_sources", 0), "FusionPolicy minimum_independent_sources"
        ),
        require_support_for_non_exact=require_bool(
            data.get("require_support_for_non_exact", False), "FusionPolicy require_support_for_non_exact"
        ),
        max_report_candidates=internal_integer(data.get("max_report_candidates", 0), "FusionPolicy max_report_candidates"),
    )
    return result


def candidate_eligibility(
    score_eligible: object = True,
    evidence_eligible: object = True,
    answer_eligible: object = True,
    reason_codes: object = (),
    feature_values: object = {},
    feature_available: object = (),
) -> dict:
    """Validate separate score, evidence, and direct-answer eligibility."""
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
    if not isinstance(feature_values, dict):
        raise InvalidRequestError("eligibility feature values must be an object")
    values = {}
    for feature, value in feature_values.items():
        if not isinstance(feature, FusionFeature):
            raise InvalidRequestError("eligibility feature keys must be FusionFeature values")
        values[feature] = bounded_unit(value)
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
    result: dict = {
        "score_eligible": score_eligible,
        "evidence_eligible": evidence_eligible,
        "answer_eligible": answer_eligible,
        "reason_codes": normalized_reasons,
        "feature_values": dict(values),
        "feature_available": ordered,
    }
    return result


def validate_candidate_eligibility(value: object) -> dict:
    data = exact_mapping(value, "CandidateEligibility", CANDIDATE_ELIGIBILITY_FIELDS)
    result = candidate_eligibility(
        data.get("score_eligible", False),
        data.get("evidence_eligible", False),
        data.get("answer_eligible", False),
        data.get("reason_codes", ()),
        data.get("feature_values", {}),
        data.get("feature_available", ()),
    )
    return result


def trusted_candidate_eligibility_to_dict(current: dict) -> dict:
    """Serialize engine-owned candidate eligibility."""
    result = {
        "score_eligible": current.get("score_eligible", False),
        "evidence_eligible": current.get("evidence_eligible", False),
        "answer_eligible": current.get("answer_eligible", False),
        "reason_codes": [reason.value for reason in current.get("reason_codes", ())],
        "feature_values": {feature.value: value for feature, value in current.get("feature_values", {}).items()},
        "feature_available": [feature.value for feature in current.get("feature_available", ())],
    }
    return result


def permissive_candidate_authority(candidate: dict, frame: dict) -> dict:
    """Return explicit conformance-only eligibility for isolated fixtures."""
    del candidate, frame
    result = candidate_eligibility()
    return result


def abstaining_candidate_authority(candidate: dict, frame: dict) -> dict:
    """Return the safe default when no current-state authority was supplied."""
    del candidate, frame
    result = candidate_eligibility(
        False,
        False,
        False,
        (FusionPolicyReason.AUTHORITATIVE_STATEMENT_MISSING,),
    )
    return result


def candidate_visibility_allowed(metadata: dict, scope: dict) -> bool:
    """Return whether candidate metadata permits disclosure in the exact scope."""
    visibility = metadata.get("visibility", "")
    if visibility in ("", "public", "scope"):
        result = True
        return result
    if visibility == "context":
        owner = metadata.get("owner_context_fingerprint", "")
        result = isinstance(owner, str) and bool(owner) and owner == scope.get("context_fingerprint", "")
        return result
    result = False
    return result


class EngramCandidateAuthority:
    """Revalidate response artifacts and deterministic candidates against their owners."""

    def __init__(self, engram, feedback_store: object = (), policy_fingerprint_value: str = "") -> None:
        self.internal_engram = engram
        if feedback_store != () and not isinstance(feedback_store, FeedbackStore):
            raise InvalidRequestError("candidate feedback_store must be FeedbackStore")
        if feedback_store != () and (
            len(policy_fingerprint_value) != 64 or any(c not in "0123456789abcdef" for c in policy_fingerprint_value)
        ):
            raise InvalidRequestError("candidate feedback policy fingerprint must be lowercase SHA-256")
        self.internal_feedback_store = feedback_store
        self.internal_policy_fingerprint = policy_fingerprint_value

    def __call__(self, candidate: dict, frame: dict) -> dict:
        result = self.evaluate(candidate, frame)
        return result

    def relation_candidate(self, candidate: dict, frame: dict) -> dict:
        """Revalidate a deterministic one-hop phrase against current Proposition state."""
        provenance = candidate.get("provenance", {})
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
        }
        if set(provenance) != required or provenance.get("producer", "") != "relation_one_hop":
            result = candidate_eligibility(
                False,
                False,
                False,
                (FusionPolicyReason.AUTHORITATIVE_STATEMENT_MISSING,),
            )
            return result
        if candidate.get("scope", {}) != frame.get("scope", {}):
            result = candidate_eligibility(False, False, False, (FusionPolicyReason.CANDIDATE_SCOPE_MISMATCH,))
            return result
        candidate_evidence = candidate.get("evidence", ())
        only_reference = candidate_evidence[0] if len(candidate_evidence) == 1 else {}
        if (
            len(candidate_evidence) != 1
            or only_reference.get("kind", "") != EvidenceKind.PROPOSITION
            or only_reference.get("evidence_id", "") != candidate.get("statement_id", "")
        ):
            result = candidate_eligibility(
                False,
                False,
                False,
                (FusionPolicyReason.SUPPORT_REFERENCE_STALE,),
            )
            return result
        try:
            current_values = self.internal_engram.current_proposition_projection(
                candidate.get("statement_id", ""), assertion_basis_window(frame)
            )
            if not isinstance(current_values, tuple) or len(current_values) != 1:
                raise InvalidRequestError("current relation Proposition is unavailable")
            current = validate_proposition_projection(current_values[0])
            if current.get("projection_id", "") != PropositionProjectionQuery.BY_ID:
                raise InvalidRequestError("current relation Proposition was not read by ID")
            identity = (
                current.get("subject_entity_id", ""),
                current.get("predicate_id", ""),
                current.get("object_entity_id", ""),
            )
            expected_identity = (
                provenance.get("subject_entity_id", ""),
                provenance.get("predicate_id", ""),
                provenance.get("object_entity_id", ""),
            )
            if identity != expected_identity:
                raise InvalidRequestError("current relation Proposition identity changed")
            if not current.get("supplied_trust_available", False) or current.get("supplied_trust", 0.0) != provenance.get(
                "supplied_trust", 0.0
            ):
                raise InvalidRequestError("current relation Proposition trust changed")
            response = phrase_relation_result(
                provenance.get("subject_label", ""),
                provenance.get("predicate_label", ""),
                provenance.get("object_label", ""),
            )
            object_type = ExpectedObjectType(str(provenance.get("object_type", "")))
            PredicateCardinality(str(provenance.get("predicate_cardinality", "")))
            selection_reason = RelationSelectionReason(str(provenance.get("selection_reason", "")))
            if selection_reason not in {
                RelationSelectionReason.SELECTED_UNIQUE,
                RelationSelectionReason.SELECTED_LATEST,
                RelationSelectionReason.SELECTED_TRUST_RANKED,
            }:
                raise InvalidRequestError("relation candidate selection reason is not direct-answer eligible")
        except (InvalidRequestError, TypeError, ValueError):
            result = candidate_eligibility(
                False,
                False,
                False,
                (FusionPolicyReason.AUTHORITATIVE_STATEMENT_MISSING,),
            )
            return result
        if response != candidate.get("response", ""):
            result = candidate_eligibility(
                False,
                False,
                False,
                (FusionPolicyReason.AUTHORITATIVE_RESPONSE_MISMATCH,),
            )
            return result
        if frame.get("expected_object_type", ExpectedObjectType.UNKNOWN) != ExpectedObjectType.UNKNOWN and (
            object_type == ExpectedObjectType.UNKNOWN
            or object_type != frame.get("expected_object_type", ExpectedObjectType.UNKNOWN)
        ):
            result = candidate_eligibility(
                True,
                True,
                False,
                (FusionPolicyReason.OBJECT_TYPE_FEATURE_MISMATCH,),
            )
            return result
        decision = PropositionEligibilityEvaluator(getattr(self.internal_engram, "proposition_visibility_authority", ())).evaluate(
            current, frame
        )
        if not decision.get("eligible", False):
            result = candidate_eligibility(False, False, False, (FusionPolicyReason.ARTIFACT_INELIGIBLE,))
            return result
        result = candidate_eligibility(True, True, True)
        return result

    def composition_candidate(self, candidate: dict, frame: dict) -> dict:
        """Revalidate every Proposition and reconstruct one bounded composition phrase."""
        provenance = candidate.get("provenance", {})
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
        if set(provenance) != required or provenance.get("producer") != "graph_composition":
            result = candidate_eligibility(False, False, False, (FusionPolicyReason.AUTHORITATIVE_STATEMENT_MISSING,))
            return result
        if candidate.get("scope", {}) != frame.get("scope", {}):
            result = candidate_eligibility(False, False, False, (FusionPolicyReason.CANDIDATE_SCOPE_MISMATCH,))
            return result
        tuple_values = (
            provenance["proposition_ids"],
            provenance["identity_chain"],
            provenance["trust_chain"],
            provenance["predicate_labels"],
            provenance["terminal_labels"],
            provenance["terminal_types"],
        )
        if not all(isinstance(value, tuple) for value in tuple_values):
            result = candidate_eligibility(False, False, False, (FusionPolicyReason.AUTHORITATIVE_STATEMENT_MISSING,))
            return result
        proposition_ids = tuple_values[0]
        identity_chain = tuple_values[1]
        trust_chain = tuple_values[2]
        predicate_labels = tuple_values[3]
        terminal_labels = tuple_values[4]
        terminal_types = tuple_values[5]
        if not 1 <= len(proposition_ids) <= 2 or not (
            len(identity_chain) == len(trust_chain) == len(predicate_labels) == len(proposition_ids)
        ):
            result = candidate_eligibility(False, False, False, (FusionPolicyReason.AUTHORITATIVE_STATEMENT_MISSING,))
            return result
        if (
            len(candidate.get("evidence", ())) != len(proposition_ids)
            or tuple(reference["evidence_id"] for reference in candidate.get("evidence", ())) != proposition_ids
        ):
            result = candidate_eligibility(False, False, False, (FusionPolicyReason.SUPPORT_REFERENCE_STALE,))
            return result
        try:
            operator = GraphCompositionOperator(str(provenance["operator"]))
            evaluator = PropositionEligibilityEvaluator(getattr(self.internal_engram, "proposition_visibility_authority", ()))
            for index, proposition_id in enumerate(proposition_ids):
                if not isinstance(proposition_id, str) or not proposition_id:
                    raise InvalidRequestError("composition Proposition ID is malformed")
                current_values = self.internal_engram.current_proposition_projection(proposition_id, assertion_basis_window(frame))
                if not isinstance(current_values, tuple) or len(current_values) != 1:
                    raise InvalidRequestError("composition Proposition is unavailable")
                current = validate_proposition_projection(current_values[0])
                if current.get("projection_id") != PropositionProjectionQuery.BY_ID:
                    raise InvalidRequestError("composition Proposition was not read by ID")
                identity_value = identity_chain[index]
                trust = trust_chain[index]
                identity = identity_value if isinstance(identity_value, tuple) else ()
                if len(identity) != 3 or isinstance(trust, bool) or not isinstance(trust, (int, float)):
                    raise InvalidRequestError("composition provenance chain is malformed")
                if (
                    current["subject_entity_id"],
                    current["predicate_id"],
                    current["object_entity_id"],
                ) != identity:
                    raise InvalidRequestError("composition Proposition identity changed")
                if not current["supplied_trust_available"] or current["supplied_trust"] != trust:
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
            result = candidate_eligibility(False, False, False, (FusionPolicyReason.AUTHORITATIVE_STATEMENT_MISSING,))
            return result
        if response != candidate.get("response", ""):
            result = candidate_eligibility(False, False, False, (FusionPolicyReason.AUTHORITATIVE_RESPONSE_MISMATCH,))
            return result
        if frame.get("expected_object_type", ExpectedObjectType.UNKNOWN) != ExpectedObjectType.UNKNOWN and (
            object_type == ExpectedObjectType.UNKNOWN
            or object_type != frame.get("expected_object_type", ExpectedObjectType.UNKNOWN)
        ):
            result = candidate_eligibility(True, True, False, (FusionPolicyReason.OBJECT_TYPE_FEATURE_MISMATCH,))
            return result
        result = candidate_eligibility(True, True, True)
        return result

    def evaluate(self, candidate: dict, frame: dict) -> dict:
        if (
            candidate.get("source", CandidateSource.EXACT) == CandidateSource.UTILITY
            and candidate.get("provenance", {}).get("producer") == "relation_one_hop"
        ):
            result = self.relation_candidate(candidate, frame)
            return result
        if (
            candidate.get("source", CandidateSource.EXACT) == CandidateSource.UTILITY
            and candidate.get("provenance", {}).get("producer") == "graph_composition"
        ):
            result = self.composition_candidate(candidate, frame)
            return result
        if (
            candidate.get("source", CandidateSource.EXACT) == CandidateSource.UTILITY
            and candidate.get("provenance", {}).get("producer") == UTILITY_RESOLVER_PRODUCER
        ):
            if frame.get("required_source_label", ""):
                result = candidate_eligibility(False, False, False, (FusionPolicyReason.REQUIRED_SOURCE_MISMATCH,))
                return result
            if frame.get("required_metadata", {}):
                result = candidate_eligibility(False, False, False, (FusionPolicyReason.REQUIRED_METADATA_MISMATCH,))
                return result
            plugin_name = candidate.get("provenance", {}).get("plugin_name", "")
            evaluated = evaluate_named_utility(frame.get("original_text", ""), plugin_name)
            if (
                evaluated["status"] != "resolved"
                or candidate.get("provenance", {}).get("canonical_input") != evaluated["canonical_input"]
            ):
                result = candidate_eligibility(False, False, False, (FusionPolicyReason.AUTHORITATIVE_STATEMENT_MISSING,))
                return result
            digest = hashlib_sha256(f"{plugin_name}:{evaluated['canonical_input']}".encode()).hexdigest()
            if candidate.get("statement_id", "") != f"utility:{plugin_name}:sha256:{digest}":
                result = candidate_eligibility(False, False, False, (FusionPolicyReason.AUTHORITATIVE_STATEMENT_MISSING,))
                return result
            if candidate.get("response", "") != evaluated["response"]:
                result = candidate_eligibility(False, False, False, (FusionPolicyReason.AUTHORITATIVE_RESPONSE_MISMATCH,))
                return result
            result = candidate_eligibility(True, True, True)
            return result
        artifact = self.internal_engram.response_repository.find_artifact(candidate.get("statement_id", ""))
        if artifact:
            if isinstance(self.internal_feedback_store, FeedbackStore):
                if self.internal_feedback_store.stale_excluded(artifact["statement_id"], artifact["generation"]):
                    result = candidate_eligibility(False, False, False, (FusionPolicyReason.FEEDBACK_STALE_EXCLUDED,))
                    return result
                if self.internal_feedback_store.policy_suppressed(
                    artifact["statement_id"],
                    frame.get("scope", {})["namespace"],
                    self.internal_policy_fingerprint,
                ):
                    result = candidate_eligibility(False, False, False, (FusionPolicyReason.FEEDBACK_POLICY_SUPPRESSED,))
                    return result
            decision = evaluate_artifact_eligibility(
                artifact,
                frame.get("eligibility_context", {}),
            )
            if not decision["direct_answer_eligible"]:
                result = candidate_eligibility(
                    False,
                    False,
                    False,
                    (FusionPolicyReason.ARTIFACT_INELIGIBLE,),
                )
                return result
            if artifact["scope"] != frame.get("scope", {}):
                result = candidate_eligibility(False, False, False, (FusionPolicyReason.CANDIDATE_SCOPE_MISMATCH,))
                return result
            if artifact["response"] != candidate.get("response", ""):
                result = candidate_eligibility(False, False, False, (FusionPolicyReason.AUTHORITATIVE_RESPONSE_MISMATCH,))
                return result
            if "generation" in candidate.get("provenance", {}):
                candidate_generation = candidate.get("provenance", {})["generation"]
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
            if frame.get("required_source_label", "") and artifact["provenance"]["source_label"] != frame.get(
                "required_source_label", ""
            ):
                result = candidate_eligibility(False, False, False, (FusionPolicyReason.REQUIRED_SOURCE_MISMATCH,))
                return result
            if any(
                key not in artifact["metadata"] or artifact["metadata"][key] != value
                for key, value in frame.get("required_metadata", {}).items()
            ):
                result = candidate_eligibility(False, False, False, (FusionPolicyReason.REQUIRED_METADATA_MISMATCH,))
                return result
            if not candidate_visibility_allowed(artifact["metadata"], frame.get("scope", {})):
                result = candidate_eligibility(False, False, False, (FusionPolicyReason.OWNERSHIP_VISIBILITY_MISMATCH,))
                return result
            feature_values: dict[FusionFeature, float] = {
                FusionFeature.SUPPORT: float(bool(artifact["support_references"])),
            }
            feature_available = [FusionFeature.SUPPORT]
            if artifact["statistics"]["query_count"]:
                feature_values[FusionFeature.HISTORY] = artifact["statistics"]["hit_count"] / artifact["statistics"]["query_count"]
                feature_available.append(FusionFeature.HISTORY)
            if isinstance(self.internal_feedback_store, FeedbackStore):
                feedback_history = self.internal_feedback_store.history(
                    frame.get("identity", {}),
                    constraint_fingerprint(
                        frame.get("expected_object_type", ExpectedObjectType.UNKNOWN).value,
                        frame.get("required_metadata", {}),
                        frame.get("required_source_label", ""),
                    ),
                    artifact["statement_id"],
                    artifact["generation"],
                    self.internal_policy_fingerprint,
                    frame.get("eligibility_context", {})["evaluation_time"],
                )
                if feedback_history["available"]:
                    if FusionFeature.HISTORY in feature_available:
                        feature_values[FusionFeature.HISTORY] = (
                            feature_values.get(FusionFeature.HISTORY, 0.0) + feedback_history["value"]
                        ) / 2.0
                    else:
                        feature_values[FusionFeature.HISTORY] = feedback_history["value"]
                        feature_available.append(FusionFeature.HISTORY)
            authority = artifact["metadata"].get("authority")
            if (
                isinstance(authority, (int, float))
                and not isinstance(authority, bool)
                and math_isfinite(float(authority))
                and 0.0 <= float(authority) <= 1.0
            ):
                feature_values[FusionFeature.AUTHORITY] = float(authority)
                feature_available.append(FusionFeature.AUTHORITY)
            support_complete = artifact["metadata"].get("support_complete", True)
            answer_eligible = isinstance(support_complete, bool) and support_complete
            reasons: tuple[FusionPolicyReason, ...] = () if answer_eligible else (FusionPolicyReason.SUPPORT_INCOMPLETE,)
            retained_support = tuple(
                reference["evidence_id"] for reference in candidate.get("evidence", ()) if reference["kind"] == EvidenceKind.SUPPORT
            )
            support_ids = {reference.get("id", "") for reference in artifact["support_references"]}
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
        result = candidate_eligibility(False, False, False, (FusionPolicyReason.AUTHORITATIVE_STATEMENT_MISSING,))
        return result


def trusted_fusion_contribution(
    candidate: dict,
    normalized: dict,
    eligibility: dict,
) -> dict:
    """Build a contribution from engine-owned validated components."""
    result: dict = {
        "candidate": candidate,
        "normalized": normalized,
        "eligibility": eligibility,
    }
    return result


def trusted_fusion_contribution_to_dict(current: dict) -> dict:
    """Serialize one engine-owned contribution."""
    result = {
        "candidate": trusted_candidate_to_dict(current.get("candidate", {})),
        "normalized": trusted_normalized_feature_set_to_dict(current.get("normalized", {})),
        "eligibility": trusted_candidate_eligibility_to_dict(current.get("eligibility", {})),
    }
    return result


def trusted_fusion_contribution_to_report_dict(current: dict) -> dict:
    """Render an engine-owned contribution for bounded diagnostics."""
    result = {
        "candidate_id": current.get("candidate", {})["candidate_id"],
        "source": current.get("candidate", {})["source"].value,
        "raw_features": {
            "values": dict(current.get("candidate", {})["features"]["values"]),
            "unavailable": list(current.get("candidate", {})["features"]["unavailable"]),
        },
        "normalized_features": trusted_normalized_feature_set_to_dict(current.get("normalized", {})),
        "eligibility": trusted_candidate_eligibility_to_dict(current.get("eligibility", {})),
        "diagnostic_fields": sorted(current.get("candidate", {})["diagnostics"]),
        "evidence_ids": [reference["evidence_id"] for reference in current.get("candidate", {})["evidence"]],
    }
    return result


def trusted_fusion_contribution_to_json(value: dict) -> str:
    """Serialize one engine-owned contribution without redundant revalidation."""
    data = trusted_fusion_contribution_to_dict(value)
    result = json_text(data)
    return result


def trusted_fused_candidate(
    candidate: dict,
    contributions: tuple[dict, ...],
    normalized: dict,
    score: float,
    score_contributions: dict[FusionFeature, float],
    eligibility: dict,
) -> dict:
    """Build a fused candidate from engine-owned validated components."""
    result: dict = {
        "candidate": candidate,
        "contributions": contributions,
        "normalized": normalized,
        "score": score,
        "score_contributions": dict(dict(score_contributions)),
        "eligibility": eligibility,
    }
    return result


def trusted_fused_candidate_sources(current: dict) -> tuple[CandidateSource, ...]:
    """Collect active sources from an engine-owned fused candidate."""
    sources = tuple(
        dict.fromkeys(
            contribution["candidate"]["source"]
            for contribution in current.get("contributions", ())
            if contribution["eligibility"]["score_eligible"]
        )
    )
    return sources


def trusted_fused_candidate_to_dict(current: dict) -> dict:
    """Serialize one engine-owned fused candidate."""
    result = {
        "candidate": trusted_candidate_to_dict(current.get("candidate", {})),
        "contributions": [trusted_fusion_contribution_to_dict(contribution) for contribution in current.get("contributions", ())],
        "normalized": trusted_normalized_feature_set_to_dict(current.get("normalized", {})),
        "score": current.get("score", 0.0),
        "score_contributions": {feature.value: current.get("score_contributions", {})[feature] for feature in FusionFeature},
        "eligibility": trusted_candidate_eligibility_to_dict(current.get("eligibility", {})),
    }
    return result


def trusted_fused_candidate_to_report_dict(current: dict) -> dict:
    """Render an engine-owned fused candidate for bounded diagnostics."""
    visible = current.get("contributions", ())[:MAX_FUSION_REPORT_CONTRIBUTIONS]
    sources = trusted_fused_candidate_sources(current)
    result = {
        "statement_id": current.get("candidate", {})["statement_id"],
        "candidate_id": current.get("candidate", {})["candidate_id"],
        "sources": [source.value for source in sources],
        "score": current.get("score", 0.0),
        "normalized_features": trusted_normalized_feature_set_to_dict(current.get("normalized", {})),
        "score_contributions": {feature.value: current.get("score_contributions", {})[feature] for feature in FusionFeature},
        "eligibility": {
            "score": current.get("eligibility", {})["score_eligible"],
            "evidence": current.get("eligibility", {})["evidence_eligible"],
            "answer": current.get("eligibility", {})["answer_eligible"],
            "reason_codes": [reason.value for reason in current.get("eligibility", {})["reason_codes"]],
        },
        "contributions": [trusted_fusion_contribution_to_report_dict(contribution) for contribution in visible],
        "omitted_contribution_count": len(current.get("contributions", ())) - len(visible),
    }
    return result


def trusted_fused_candidate_to_json(value: dict) -> str:
    """Serialize one engine-owned fused candidate without redundant revalidation."""
    data = trusted_fused_candidate_to_dict(value)
    result = json_text(data)
    return result


def trusted_fusion_decision(
    outcome: ResolutionOutcome,
    selected_candidate: dict,
    response_candidates: tuple[dict, ...],
    evidence: tuple[dict, ...],
    confidence: float,
    confidence_available: bool,
    reason_codes: tuple[str, ...],
    report: dict,
    working_memory_bytes: int,
) -> dict:
    """Build a decision from values whose invariants the engine has established."""
    selected_available = outcome == ResolutionOutcome.ANSWER
    selected = trusted_candidate_with_changes(selected_candidate, {})
    result: dict = {
        "outcome": outcome,
        "selected_candidate": selected,
        "selected_candidate_available": selected_available,
        "response_candidates": tuple(trusted_candidate_with_changes(candidate, {}) for candidate in response_candidates),
        "evidence": evidence,
        "confidence": confidence,
        "confidence_available": confidence_available,
        "reason_codes": reason_codes,
        "report": dict(dict(report)),
        "working_memory_bytes": working_memory_bytes,
    }
    return result


def normalize_validated_candidate_features(validated_candidate: dict) -> dict:
    """Normalize a candidate already copied and validated at the engine boundary."""
    raw = validated_candidate.get("features", {})["values"]
    values = dict.fromkeys(FusionFeature, 0.0)
    available: set[FusionFeature] = set()

    def assign(feature: FusionFeature, value: float) -> None:
        values[feature] = bounded_unit(value)
        available.add(feature)

    if validated_candidate.get("source", CandidateSource.EXACT) == CandidateSource.EXACT and "exact_match" in raw:
        assign(FusionFeature.EXACT, raw["exact_match"])
    if (
        validated_candidate.get("source", CandidateSource.EXACT) == CandidateSource.UTILITY
        and validated_candidate.get("provenance", {}).get("producer") == UTILITY_RESOLVER_PRODUCER
        and "utility_match" in raw
    ):
        assign(FusionFeature.EXACT, raw["utility_match"])
    if validated_candidate.get("source", CandidateSource.EXACT) == CandidateSource.SPARSE and "sparse_score" in raw:
        assign(FusionFeature.LEXICAL, raw["sparse_score"])
    if validated_candidate.get("source", CandidateSource.EXACT) in (
        CandidateSource.SUPPORT_SEMANTIC,
        CandidateSource.STANDALONE_SEMANTIC,
    ) and ("semantic_score" in raw):
        assign(FusionFeature.SEMANTIC, raw["semantic_score"])
    for raw_name, feature in (
        ("entity_match", FusionFeature.ENTITY),
        ("relation_match", FusionFeature.RELATION),
        ("object_type_match", FusionFeature.OBJECT_TYPE),
    ):
        if raw_name in raw:
            assign(feature, raw[raw_name])
    if validated_candidate.get("source", CandidateSource.EXACT) == CandidateSource.SUPPORT_SEMANTIC and "support_coverage" in raw:
        assign(FusionFeature.SUPPORT, raw["support_coverage"])
    elif validated_candidate.get("evidence", ()):
        support_available = any(item["kind"] == EvidenceKind.SUPPORT for item in validated_candidate.get("evidence", ()))
        assign(FusionFeature.SUPPORT, float(support_available))
    if validated_candidate.get("source", CandidateSource.EXACT) in (
        CandidateSource.SUPPORT_SEMANTIC,
        CandidateSource.STANDALONE_SEMANTIC,
    ) and ("authority_score" in raw):
        assign(FusionFeature.AUTHORITY, raw["authority_score"])
    ordered_available = tuple(feature for feature in FusionFeature if feature in available)
    result = trusted_normalized_feature_set(values, ordered_available)
    return result


def merge_validated_candidate_eligibility(
    validated_first: dict,
    validated_second: dict,
) -> dict:
    """Merge eligibility values already owned by the fusion engine."""
    reasons = tuple(dict.fromkeys((*validated_first.get("reason_codes", ()), *validated_second.get("reason_codes", ()))))
    values = dict(validated_first.get("feature_values", {}))
    values.update(validated_second.get("feature_values", {}))
    available = tuple(
        feature
        for feature in FusionFeature
        if feature in set(validated_first.get("feature_available", ())).union(validated_second.get("feature_available", ()))
    )
    result = trusted_candidate_eligibility(
        validated_first.get("score_eligible", False) and validated_second.get("score_eligible", False),
        validated_first.get("evidence_eligible", False) and validated_second.get("evidence_eligible", False),
        validated_first.get("answer_eligible", False) and validated_second.get("answer_eligible", False),
        reasons,
        values,
        available,
    )
    return result


def trusted_canonical_candidate_key(candidate: dict) -> tuple[int, str, str]:
    """Order a candidate already validated at the fusion boundary."""
    result = (
        FUSION_SOURCE_ORDER[candidate.get("source", CandidateSource.EXACT)],
        candidate.get("candidate_id", ""),
        trusted_candidate_to_json(candidate),
    )
    return result


def dedupe_evidence(
    evidence: tuple[dict, ...],
    scope: dict,
) -> tuple[tuple[dict, ...], int]:
    """Retain unique in-scope evidence and count conflicting identifiers."""
    groups: dict[str, dict[str, dict]] = {}
    for reference in evidence:
        if reference["scope"] != scope:
            continue
        groups.setdefault(reference["evidence_id"], {})[evidence_reference_to_json(reference)] = reference
    retained = []
    conflict_count = 0
    for evidence_id in sorted(groups):
        variants = groups.get(evidence_id, {})
        if len(variants) != 1:
            conflict_count += 1
            continue
        retained.append(variants[sorted(variants)[0]])
    result = tuple(retained), conflict_count
    return result


def apply_validated_authoritative_features(
    normalized: dict,
    eligibility: dict,
) -> dict:
    """Overlay authority values already validated at the fusion boundary."""
    values = dict(normalized.get("values", {}))
    available = set(normalized.get("available", ()))
    for feature in eligibility.get("feature_available", ()):
        values[feature] = eligibility.get("feature_values", {})[feature]
        available.add(feature)
    ordered_available = tuple(feature for feature in FusionFeature if feature in available)
    result = trusted_normalized_feature_set(values, ordered_available)
    return result


class CandidateFusionEngine:
    """Deterministic, bounded, non-generative first-generation fusion engine."""

    def __init__(
        self,
        policy: object = {},
        authority: object = abstaining_candidate_authority,
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
        self.policy_fingerprint = policy_fingerprint(validated_policy)
        self.internal_authority = authority
        self.internal_reranker = reranker

    def individual_eligibility(
        self,
        candidate: dict,
        frame: dict,
        normalized: dict,
        candidate_id_conflict: bool,
    ) -> dict:
        if candidate_id_conflict:
            result = candidate_eligibility(False, False, False, (FusionPolicyReason.CANDIDATE_ID_CONFLICT,))
            return result
        if candidate.get("scope", {}) != frame.get("scope", {}):
            result = candidate_eligibility(False, False, False, (FusionPolicyReason.CANDIDATE_SCOPE_MISMATCH,))
            return result
        if candidate.get("lifecycle", LifecycleState.RETIRED) != LifecycleState.ACTIVE:
            result = candidate_eligibility(False, False, False, (FusionPolicyReason.CANDIDATE_LIFECYCLE_INELIGIBLE,))
            return result
        if any(reference["scope"] != frame.get("scope", {}) for reference in candidate.get("evidence", ())):
            result = candidate_eligibility(
                False,
                False,
                False,
                (FusionPolicyReason.CANDIDATE_EVIDENCE_SCOPE_MISMATCH,),
            )
            return result
        reasons = []
        answer_eligible = True
        if candidate.get("features", {})["values"].get("explicit_conflict", 0.0) > 0.0:
            answer_eligible = False
            reasons.append(FusionPolicyReason.EXPLICIT_CONFLICT)
        if FusionFeature.SUPPORT in normalized.get("available", ()) and normalized.get("values", {})[FusionFeature.SUPPORT] == 0.0:
            answer_eligible = False
            reasons.append(FusionPolicyReason.SUPPORT_INCOMPLETE)
        structural = candidate_eligibility(True, True, answer_eligible, tuple(reasons))
        authority = validate_candidate_eligibility(self.internal_authority(candidate, frame))
        result = merge_validated_candidate_eligibility(structural, authority)
        return result

    def internal_score(self, normalized: dict) -> tuple[float, dict[FusionFeature, float]]:
        weighted = dict.fromkeys(FusionFeature, 0.0)
        numerator = 0.0
        denominator = 0.0
        for feature in normalized.get("available", ()):
            if feature in (FusionFeature.EXACT, FusionFeature.MARGIN):
                continue
            contribution = self.policy["weights"][feature] * normalized.get("values", {})[feature]
            weighted[feature] = contribution
            numerator += contribution
            denominator += self.policy["weights"][feature]
        base = numerator / denominator if denominator else 0.0
        exact = normalized.get("values", {})[FusionFeature.EXACT] if FusionFeature.EXACT in normalized.get("available", ()) else 0.0
        weighted[FusionFeature.EXACT] = exact
        score = exact + (1.0 - exact) * base
        bounded_score = bounded_unit(score)
        result = (bounded_score, dict(weighted))
        return result

    def fuse_group(
        self,
        statement_id: str,
        contributions: tuple[dict, ...],
        frame: dict,
    ) -> dict:
        active = tuple(contribution for contribution in contributions if contribution["eligibility"]["score_eligible"])
        representative = (active or contributions)[0]["candidate"]
        if active:
            eligibility = active[0]["eligibility"]
            for contribution in active[1:]:
                eligibility = merge_validated_candidate_eligibility(eligibility, contribution["eligibility"])
        else:
            eligibility = contributions[0]["eligibility"]
            for contribution in contributions[1:]:
                eligibility = merge_validated_candidate_eligibility(eligibility, contribution["eligibility"])
        excluded_reasons = tuple(
            reason
            for contribution in contributions
            if not contribution["eligibility"]["score_eligible"]
            for reason in contribution["eligibility"]["reason_codes"]
        )
        if active and excluded_reasons:
            eligibility = trusted_candidate_eligibility(
                eligibility["score_eligible"],
                eligibility["evidence_eligible"],
                eligibility["answer_eligible"],
                tuple(dict.fromkeys((*eligibility["reason_codes"], *excluded_reasons))),
                eligibility["feature_values"],
                eligibility["feature_available"],
            )
        if active and len({contribution["candidate"]["response"] for contribution in active}) != 1:
            eligibility = trusted_candidate_eligibility(
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
        retained_evidence, evidence_conflicts = dedupe_evidence(active_evidence, frame.get("scope", {}))
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
                and contribution["candidate"]["provenance"].get("producer") == UTILITY_RESOLVER_PRODUCER
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
            and frame.get("expected_object_type", ExpectedObjectType.UNKNOWN) != ExpectedObjectType.UNKNOWN
            and (FusionFeature.OBJECT_TYPE not in available or values[FusionFeature.OBJECT_TYPE] == 0.0)
        ):
            answer_eligible = False
            reasons.append(FusionPolicyReason.OBJECT_TYPE_FEATURE_MISMATCH)
        eligibility = trusted_candidate_eligibility(
            eligibility["score_eligible"],
            eligibility["evidence_eligible"],
            answer_eligible,
            tuple(dict.fromkeys(reasons)),
            eligibility["feature_values"],
            eligibility["feature_available"],
        )
        normalized = trusted_normalized_feature_set(values, tuple(feature for feature in FusionFeature if feature in available))
        score, score_contributions = self.internal_score(normalized)
        digest = hashlib_sha256(f"{self.policy_fingerprint}:{frame.get('diagnostic_id', '')}:{statement_id}".encode()).hexdigest()
        fused = trusted_candidate(
            candidate_id=f"fused:sha256:{digest}",
            statement_id=statement_id,
            response=representative["response"],
            source=representative["source"],
            features=trusted_feature_set(
                values={feature.value: values[feature] for feature in normalized["available"]},
                unavailable=tuple(sorted(feature.value for feature in FusionFeature if feature not in normalized["available"])),
            ),
            evidence=retained_evidence,
            scope=representative["scope"],
            lifecycle=representative["lifecycle"],
            provenance={
                **dict(representative["provenance"]),
                "resolver_sources": [source.value for source in sources],
                "resolver_families": list(families),
            },
            diagnostics={"fusion_contribution_count": len(contributions)},
        )
        result = trusted_fused_candidate(fused, contributions, normalized, score, score_contributions, eligibility)
        return result

    def exhausted_decision(
        self,
        frame: dict,
        candidates: tuple[dict, ...],
        evidence: tuple[dict, ...],
        reason: FusionPolicyReason,
        working_memory_bytes: int = 0,
    ) -> dict:
        retained_evidence, evidence_conflicts = dedupe_evidence(evidence, frame.get("scope", {}))
        outcome = ResolutionOutcome.EVIDENCE if retained_evidence else ResolutionOutcome.MISS
        report = {
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
        result = trusted_fusion_decision(
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
        frame: dict,
        candidates: tuple[dict, ...],
        evidence: tuple[dict, ...] = (),
        *,
        working_memory_limit: int = 0,
        working_memory_limit_available: bool = False,
        cooperative_check: object = (),
    ) -> dict:
        frame = validate_query_frame(frame)
        # Counts, scalar limits and the control callback are checked before any
        # per-item validation, copy or serialization work begins.
        if not isinstance(candidates, tuple):
            raise InvalidRequestError("fusion candidates must be a tuple of Candidate values")
        if len(candidates) > MAX_FUSION_CONTRIBUTIONS:
            raise InvalidRequestError(f"fusion candidates exceed the limit of {MAX_FUSION_CONTRIBUTIONS}")
        if not isinstance(evidence, tuple):
            raise InvalidRequestError("fusion evidence must be a tuple of EvidenceReference values")
        if len(evidence) > MAX_FUSION_CONTRIBUTIONS:
            raise InvalidRequestError(f"fusion evidence exceeds the limit of {MAX_FUSION_CONTRIBUTIONS}")
        if not isinstance(working_memory_limit_available, bool):
            raise InvalidRequestError("fusion working_memory_limit_available must be a boolean")
        if isinstance(working_memory_limit, bool) or not isinstance(working_memory_limit, int) or working_memory_limit < 0:
            raise InvalidRequestError("fusion working_memory_limit must be a nonnegative integer")
        if not working_memory_limit_available and working_memory_limit != 0:
            raise InvalidRequestError("fusion working_memory_limit requires availability")
        if working_memory_limit_available and working_memory_limit > frame.get("budget", {})["max_working_memory_bytes"]:
            raise InvalidRequestError("fusion working_memory_limit exceeds the frame budget")
        if cooperative_check != () and not callable(cooperative_check):
            raise InvalidRequestError("fusion cooperative_check must be callable")
        control_check = cooperative_check if cooperative_check != () else (lambda: False)
        memory_limit = (
            working_memory_limit if working_memory_limit_available else frame.get("budget", {})["max_working_memory_bytes"]
        )
        control_check()
        # Evidence is retained even by an exhausted decision, so all of it is
        # validated (the count is already bounded); candidates are copied and
        # serialized one at a time against the working-memory allowance.
        validated_evidence = []
        working_bytes = 0
        for value in evidence:
            control_check()
            reference = validate_evidence_reference(value)
            validated_evidence.append(reference)
            working_bytes += len(evidence_reference_to_json(reference).encode("utf-8"))
        evidence = tuple(validated_evidence)
        serialized_values = []
        for value in candidates:
            control_check()
            if working_bytes > memory_limit:
                break
            candidate = validate_candidate(value)
            serialized = trusted_candidate_to_json(candidate)
            serialized_values.append((candidate, serialized))
            working_bytes += len(serialized.encode("utf-8"))
        if working_bytes > memory_limit:
            result = self.exhausted_decision(
                frame,
                candidates,
                evidence,
                FusionPolicyReason.FUSION_MEMORY_EXHAUSTED,
                memory_limit,
            )
            return result
        serialized_candidates = tuple(serialized_values)
        candidates = tuple(candidate for candidate, _ in serialized_candidates)
        candidate_variants: dict[str, set[str]] = {}
        for candidate_value, serialized in serialized_candidates:
            candidate_variants.setdefault(candidate_value["candidate_id"], set()).add(serialized)
        conflicting_candidate_ids = {candidate_id for candidate_id, variants in candidate_variants.items() if len(variants) > 1}
        ordered = tuple(sorted(candidates, key=trusted_canonical_candidate_key))
        contribution_groups: dict[str, list[dict]] = {}
        for candidate in ordered:
            control_check()
            normalized = normalize_validated_candidate_features(candidate)
            eligibility = self.individual_eligibility(
                candidate,
                frame,
                normalized,
                candidate["candidate_id"] in conflicting_candidate_ids,
            )
            normalized = apply_validated_authoritative_features(normalized, eligibility)
            contribution = trusted_fusion_contribution(candidate, normalized, eligibility)
            contribution_groups.setdefault(candidate["statement_id"], []).append(contribution)
            contribution_json = trusted_fusion_contribution_to_json(contribution)
            working_bytes += len(contribution_json.encode("utf-8"))
            if working_bytes > memory_limit:
                result = self.exhausted_decision(
                    frame,
                    candidates,
                    evidence,
                    FusionPolicyReason.FUSION_MEMORY_EXHAUSTED,
                    memory_limit,
                )
                return result
        fused_values = []
        for statement_id in sorted(contribution_groups):
            control_check()
            fused_value = self.fuse_group(statement_id, tuple(contribution_groups.get(statement_id, [])), frame)
            fused_values.append(fused_value)
            fused_json = trusted_fused_candidate_to_json(fused_value)
            working_bytes += len(fused_json.encode("utf-8"))
            if working_bytes > memory_limit:
                result = self.exhausted_decision(
                    frame,
                    candidates,
                    evidence,
                    FusionPolicyReason.FUSION_MEMORY_EXHAUSTED,
                    memory_limit,
                )
                return result
        fused = tuple(fused_values)
        # The reranker only reorders its shortlist. Thresholds, the ambiguity
        # margin, and confidence stay on the calibrated fused score, so the
        # reranker cannot turn EVIDENCE into ANSWER by rescaling scores.
        reranked_scores: dict[str, float] = {}
        reranker_report: dict = {
            "applied": False,
            "reason": "not_configured",
            "model_version": "",
            "elapsed_ns": 0,
            "model_time_target_exceeded": False,
            "input_bytes": 0,
            "scores": [],
        }
        if isinstance(self.internal_reranker, TransparentLogisticReranker) and self.internal_reranker.enabled:
            eligible_shortlist = sorted(
                (value for value in fused if value["eligibility"]["score_eligible"]),
                key=lambda value: (-value["score"], value["candidate"]["statement_id"]),
            )[: self.internal_reranker.settings["shortlist_size"]]
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
                reranker_report = self.internal_reranker.rerank(shortlist, cooperative_check)
            except ResolutionCancelledError:
                raise
            except Exception as error:
                logger.warning("Reranker failed; using the baseline order", exc_info=error)
                self.internal_reranker.record_fallback("reranker_exception")
                reranker_report = {
                    "applied": False,
                    "reason": "reranker_exception",
                    "exception_type": type(error).__name__,
                    "model_version": self.internal_reranker.settings["model_version"],
                    "elapsed_ns": 0,
                    "model_time_target_exceeded": False,
                    "input_bytes": 0,
                    "scores": [],
                }
            if reranker_report.get("applied", False):
                score_values = reranker_report.get("scores", [])
                if not isinstance(score_values, list):
                    raise InvalidRequestError("reranker scores must be a list")
                for value in score_values:
                    if not isinstance(value, dict):
                        raise InvalidRequestError("reranker scores must contain mappings")
                    statement_id = value.get("statement_id", "")
                    if not isinstance(statement_id, str) or not statement_id:
                        raise InvalidRequestError("reranker score statement_id must be a non-empty string")
                    reranked_scores[statement_id] = bounded_unit(value.get("score", 0.0))
                updated_fused = []
                for item in fused:
                    statement_id = item["candidate"]["statement_id"]
                    if statement_id not in reranked_scores:
                        updated_fused.append(item)
                        continue
                    rerank_score = reranked_scores[statement_id]
                    updated_candidate = trusted_candidate_with_changes(
                        item["candidate"],
                        {
                            "provenance": {
                                **dict(item["candidate"]["provenance"]),
                                "reranker_implementation": self.internal_reranker.settings["implementation"],
                                "reranker_model_version": self.internal_reranker.settings["model_version"],
                            },
                            "diagnostics": {
                                **dict(item["candidate"]["diagnostics"]),
                                "fusion_base_score": item["score"],
                                "reranker_score": rerank_score,
                            },
                        },
                    )
                    updated_fused.append(
                        trusted_fused_candidate(
                            updated_candidate,
                            item["contributions"],
                            item["normalized"],
                            item["score"],
                            item["score_contributions"],
                            item["eligibility"],
                        )
                    )
                fused = tuple(updated_fused)
        ranked = tuple(
            sorted(
                (candidate for candidate in fused if candidate["eligibility"]["score_eligible"]),
                key=lambda item: (
                    item["candidate"]["statement_id"] not in reranked_scores,
                    -reranked_scores.get(item["candidate"]["statement_id"], 0.0),
                    -item["score"],
                    item["candidate"]["statement_id"],
                ),
            )
        )
        if ranked:
            margin_available = len(ranked) > 1
            # A reranked leader with a lower fused score than the runner-up
            # has no margin, so the disagreement resolves as ambiguous.
            margin = max(0.0, ranked[0]["score"] - ranked[1]["score"]) if margin_available else 1.0
            top = ranked[0]
            values = dict(top["normalized"]["values"])
            values[FusionFeature.MARGIN] = margin if margin_available else 0.0
            available = set(top["normalized"]["available"])
            if margin_available:
                available.add(FusionFeature.MARGIN)
            top_normalized = trusted_normalized_feature_set(
                values, tuple(feature for feature in FusionFeature if feature in available)
            )
            top_candidate = trusted_candidate_with_changes(
                top["candidate"],
                {
                    "features": feature_set(
                        values={feature.value: values.get(feature, 0.0) for feature in available},
                        unavailable=tuple(sorted(feature.value for feature in FusionFeature if feature not in available)),
                    )
                },
            )
            top = trusted_fused_candidate(
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
        response_candidates: tuple[dict, ...] = ()
        retained_evidence, top_level_evidence_conflicts = dedupe_evidence(evidence, frame.get("scope", {}))
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
        control_check()
        ranked_ids = {candidate["candidate"]["statement_id"] for candidate in ranked}
        report_values = (
            *ranked,
            *(candidate for candidate in fused if candidate["candidate"]["statement_id"] not in ranked_ids),
        )
        visible_report_values = report_values[: self.policy["max_report_candidates"]]
        candidate_reports = [trusted_fused_candidate_to_report_dict(candidate) for candidate in visible_report_values]

        report = {
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
        report_limit = min(MAX_FUSION_REPORT_BYTES, max(512, frame.get("budget", {}).get("max_diagnostic_bytes", 0)))
        while candidate_reports and len(json_text(report).encode("utf-8")) > report_limit:
            candidate_reports.pop()
            report["omitted_candidate_count"] = len(report_values) - len(candidate_reports)
        if len(json_text(report).encode("utf-8")) > report_limit:
            report = {
                "policy_fingerprint": policy_fingerprint(self.policy),
                "input_candidate_count": len(candidates),
                "candidate_count": len(fused),
                "omitted_candidate_count": len(report_values),
                "top_two_margin": margin,
                "top_two_margin_available": margin_available,
                "evidence_conflict_count": top_level_evidence_conflicts,
                "budget_exhausted": "",
                "reranker": {
                    "applied": reranker_report.get("applied", False),
                    "reason": reranker_report.get("reason", ""),
                    "model_version": reranker_report.get("model_version", ""),
                },
                "candidates": [],
            }
        working_bytes += len(json_text(report).encode("utf-8"))
        if working_bytes > memory_limit:
            result = self.exhausted_decision(
                frame,
                candidates,
                evidence,
                FusionPolicyReason.FUSION_MEMORY_EXHAUSTED,
                memory_limit,
            )
            return result
        result = trusted_fusion_decision(
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


def policy_fingerprint(policy: dict) -> str:
    """Return the current policy identity for reports and feedback suppression."""
    data = fusion_policy_to_dict(policy)
    result = canonical_fingerprint(data)
    return result
