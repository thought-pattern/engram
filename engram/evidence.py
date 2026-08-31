"""Current-time disclosure eligibility for response-less Proposition evidence."""

from collections.abc import Mapping
from datetime import datetime
from json import JSONDecodeError as json_JSONDecodeError, dumps as json_dumps, loads as json_loads
from math import isfinite as math_isfinite

from engram.constants import (
    CANONICAL_COMPLETENESS_FLOOR_V1,
    EMPTY_SCOPE_KEY,
    EVIDENCE_USEFULNESS_DECISION_FIELDS,
    EVIDENCE_USEFULNESS_POLICY_FIELDS,
    MAX_VISIBILITY_GRANTS,
    PROPOSITION_DISCLOSURE_POLICY_VERSION,
    PROPOSITION_ELIGIBILITY_DECISION_FIELDS,
    PROPOSITION_EVIDENCE_PRODUCERS,
    PROPOSITION_EVIDENCE_USEFULNESS_POLICY_VERSION,
    SEMANTIC_SIMILARITY_FLOOR_V1,
    SOURCE_AGREEMENT_FLOOR_V1,
    STRUCTURED_MATCH_FLOOR_V1,
    VISIBILITY_AUTHORIZATION_FIELDS,
    VISIBILITY_GRANT_FIELDS,
    EvidenceUsefulnessReason,
    PropositionEligibilityReason,
    TemporalAxis,
    TemporalQueryOperator,
)
from engram.errors import IdentityValidationError, InvalidRequestError
from engram.graph import PropositionProjectionQuery, validate_proposition_projection
from engram.identity import scope_key_signature, validate_scope_key
from engram.resolution import (
    MAX_PROPOSITION_SELECTION_REASONS,
    MAX_PROPOSITION_SOURCE_CONTRIBUTIONS,
    MAX_RESOLUTION_VALUES,
    DisclosureBasis,
    PropositionOwnership,
    canonical_proposition_references,
    disclosure_decision,
    feature_set,
    proposition_evidence_record as build_proposition_evidence_record,
    proposition_evidence_record_to_json,
    proposition_evidence_record_with_changes,
    proposition_trust_inputs,
    proposition_validity_inputs,
    validate_disclosure_decision,
    validate_proposition_evidence_record,
    validate_query_frame,
)


def empty_disclosure_decision() -> dict:
    """Return the concrete unavailable disclosure-decision value."""
    result = disclosure_decision(
        PropositionOwnership.PUBLIC,
        DisclosureBasis.PUBLIC_RULE,
        EMPTY_SCOPE_KEY,
        "unavailable",
    )
    return result


def no_cooperative_check() -> bool:
    return False


def exact_mapping(value: object, name: str, fields: set[str]) -> dict[str, object]:
    if not isinstance(value, Mapping) or set(value) != fields:
        raise InvalidRequestError(f"{name} has invalid fields")
    result = value
    return result


def evidence_usefulness_decision(
    proposition_id: object,
    included: object,
    reasons: object,
    policy_version: object = PROPOSITION_EVIDENCE_USEFULNESS_POLICY_VERSION,
) -> dict:
    """Build one content-free evidence inclusion decision."""
    normalized_proposition_id = token(proposition_id, "evidence usefulness proposition_id")
    if not isinstance(included, bool):
        raise InvalidRequestError("evidence usefulness included must be a boolean")
    if policy_version != PROPOSITION_EVIDENCE_USEFULNESS_POLICY_VERSION:
        raise InvalidRequestError(f"unsupported evidence usefulness policy_version: {policy_version}")
    if not isinstance(reasons, tuple) or not reasons:
        raise InvalidRequestError("evidence usefulness reasons must be a non-empty tuple")
    if not all(isinstance(reason, EvidenceUsefulnessReason) for reason in reasons):
        raise InvalidRequestError("evidence usefulness reasons are invalid")
    normalized_reasons = tuple(reasons)
    if normalized_reasons != tuple(sorted(set(normalized_reasons), key=lambda reason: reason.value)):
        raise InvalidRequestError("evidence usefulness reasons must be unique and sorted")
    exclusion_reasons = {
        EvidenceUsefulnessReason.CANONICAL_COMPLETENESS_UNAVAILABLE,
        EvidenceUsefulnessReason.CANONICAL_COMPLETENESS_BELOW_FLOOR,
        EvidenceUsefulnessReason.RETRIEVAL_SIGNAL_UNAVAILABLE,
        EvidenceUsefulnessReason.RETRIEVAL_SIGNAL_BELOW_FLOOR,
        EvidenceUsefulnessReason.SUPPLIED_TRUST_REQUIRED_UNAVAILABLE,
        EvidenceUsefulnessReason.SUPPLIED_TRUST_BELOW_FLOOR,
    }
    qualifying_reasons = {
        EvidenceUsefulnessReason.STRUCTURED_MATCH_QUALIFIED,
        EvidenceUsefulnessReason.SEMANTIC_SIMILARITY_QUALIFIED,
    }
    has_exclusion = bool(exclusion_reasons.intersection(normalized_reasons))
    has_qualifying_signal = bool(qualifying_reasons.intersection(normalized_reasons))
    if included and (has_exclusion or not has_qualifying_signal):
        raise InvalidRequestError("included evidence usefulness decision has inconsistent reasons")
    if not included and not has_exclusion:
        raise InvalidRequestError("excluded evidence usefulness decision requires an exclusion reason")
    result: dict = {
        "policy_version": PROPOSITION_EVIDENCE_USEFULNESS_POLICY_VERSION,
        "proposition_id": normalized_proposition_id,
        "included": included,
        "reasons": normalized_reasons,
    }
    return result


def validate_evidence_usefulness_decision(value: object) -> dict:
    data = exact_mapping(value, "EvidenceUsefulnessDecision", EVIDENCE_USEFULNESS_DECISION_FIELDS)
    result = evidence_usefulness_decision(data["proposition_id"], data["included"], data["reasons"], data["policy_version"])
    return result


def evidence_usefulness_decision_to_dict(value: object) -> dict[str, object]:
    decision = validate_evidence_usefulness_decision(value)
    result = {
        "policy_version": decision["policy_version"],
        "proposition_id": decision["proposition_id"],
        "included": decision["included"],
        "reasons": [reason.value for reason in decision["reasons"]],
    }
    return result


def evidence_usefulness_policy(
    policy_version: object = PROPOSITION_EVIDENCE_USEFULNESS_POLICY_VERSION,
    canonical_completeness_floor: object = CANONICAL_COMPLETENESS_FLOOR_V1,
    structured_match_floor: object = STRUCTURED_MATCH_FLOOR_V1,
    semantic_similarity_floor: object = SEMANTIC_SIMILARITY_FLOOR_V1,
    source_agreement_floor: object = SOURCE_AGREEMENT_FLOOR_V1,
    supplied_trust_floor: object = 0.0,
    supplied_trust_floor_available: object = False,
) -> dict:
    """Build the frozen hand-authored evidence policy used by release qualification."""
    if not isinstance(policy_version, str):
        raise InvalidRequestError("evidence usefulness policy_version must be a string")
    if policy_version != PROPOSITION_EVIDENCE_USEFULNESS_POLICY_VERSION:
        raise InvalidRequestError(f"unsupported evidence usefulness policy_version: {policy_version}")
    frozen = {
        "canonical_completeness_floor": (canonical_completeness_floor, CANONICAL_COMPLETENESS_FLOOR_V1),
        "structured_match_floor": (structured_match_floor, STRUCTURED_MATCH_FLOOR_V1),
        "semantic_similarity_floor": (semantic_similarity_floor, SEMANTIC_SIMILARITY_FLOOR_V1),
        "source_agreement_floor": (source_agreement_floor, SOURCE_AGREEMENT_FLOOR_V1),
    }
    normalized_floors: dict[str, float] = {}
    for name, pair in frozen.items():
        value, expected = pair
        if not isinstance(value, float):
            raise InvalidRequestError(f"evidence usefulness {name} must be a float")
        if not math_isfinite(value) or value != expected:
            raise InvalidRequestError(f"evidence usefulness {name} is frozen at {expected}")
        normalized_floors[name] = value
    if not isinstance(supplied_trust_floor_available, bool):
        raise InvalidRequestError("evidence usefulness supplied_trust_floor_available must be a boolean")
    if (
        not isinstance(supplied_trust_floor, float)
        or not math_isfinite(supplied_trust_floor)
        or not 0.0 <= supplied_trust_floor <= 1.0
    ):
        raise InvalidRequestError("evidence usefulness supplied_trust_floor must be a finite float in [0, 1]")
    if not supplied_trust_floor_available and supplied_trust_floor != 0.0:
        raise InvalidRequestError("unavailable evidence usefulness supplied_trust_floor must be zero")
    result: dict = {
        "policy_version": PROPOSITION_EVIDENCE_USEFULNESS_POLICY_VERSION,
        "canonical_completeness_floor": normalized_floors.get("canonical_completeness_floor", 0.0),
        "structured_match_floor": normalized_floors.get("structured_match_floor", 0.0),
        "semantic_similarity_floor": normalized_floors.get("semantic_similarity_floor", 0.0),
        "source_agreement_floor": normalized_floors.get("source_agreement_floor", 0.0),
        "supplied_trust_floor": supplied_trust_floor,
        "supplied_trust_floor_available": supplied_trust_floor_available,
    }
    return result


def validate_evidence_usefulness_policy(value: object) -> dict:
    data = exact_mapping(value, "EvidenceUsefulnessPolicy", EVIDENCE_USEFULNESS_POLICY_FIELDS)
    result = evidence_usefulness_policy(
        data["policy_version"],
        data["canonical_completeness_floor"],
        data["structured_match_floor"],
        data["semantic_similarity_floor"],
        data["source_agreement_floor"],
        data["supplied_trust_floor"],
        data["supplied_trust_floor_available"],
    )
    return result


def evidence_usefulness_policy_with_changes(value: object, changes: object) -> dict:
    policy = validate_evidence_usefulness_policy(value)
    if not isinstance(changes, Mapping):
        raise InvalidRequestError("evidence usefulness policy changes must be an object")
    if not set(changes).issubset(EVIDENCE_USEFULNESS_POLICY_FIELDS):
        raise InvalidRequestError("evidence usefulness policy changes contain an unknown field")
    updated: dict[str, object] = dict(policy)
    updated.update(changes)
    result = validate_evidence_usefulness_policy(updated)
    return result


def evidence_usefulness_policy_to_dict(value: object) -> dict[str, object]:
    policy = validate_evidence_usefulness_policy(value)
    result: dict[str, object] = dict(policy)
    return result


def evidence_usefulness_policy_from_dict(value: object) -> dict:
    result = validate_evidence_usefulness_policy(value)
    return result


def evidence_usefulness_policy_to_json(value: object) -> str:
    payload = evidence_usefulness_policy_to_dict(value)
    result = json_dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return result


def evidence_usefulness_policy_from_json(value: str) -> dict:
    if not isinstance(value, str):
        raise InvalidRequestError("EvidenceUsefulnessPolicy JSON must be a string")
    try:
        decoded = json_loads(value)
    except json_JSONDecodeError as error:
        raise InvalidRequestError("EvidenceUsefulnessPolicy JSON must be valid JSON") from error
    if not isinstance(decoded, Mapping):
        raise InvalidRequestError("EvidenceUsefulnessPolicy JSON must decode to an object")
    result = evidence_usefulness_policy_from_dict(decoded)
    return result


def evaluate_evidence_usefulness(policy: object, record: object) -> dict:
    """Apply one validated content-free evidence policy to a full Proposition record."""
    validated_policy = validate_evidence_usefulness_policy(policy)
    try:
        validated_record = validate_proposition_evidence_record(record)
    except InvalidRequestError as error:
        raise InvalidRequestError("evidence usefulness requires a PropositionEvidenceRecord") from error
    values = validated_record["features"]["values"]
    reasons: set[EvidenceUsefulnessReason] = set()
    exclusions: set[EvidenceUsefulnessReason] = set()

    if "canonical_completeness" not in values:
        exclusions.add(EvidenceUsefulnessReason.CANONICAL_COMPLETENESS_UNAVAILABLE)
    elif values["canonical_completeness"] < validated_policy["canonical_completeness_floor"]:
        exclusions.add(EvidenceUsefulnessReason.CANONICAL_COMPLETENESS_BELOW_FLOOR)

    qualifying_signal = False
    signal_available = False
    if "structured_match" in values:
        signal_available = True
        if values["structured_match"] >= validated_policy["structured_match_floor"]:
            qualifying_signal = True
            reasons.add(EvidenceUsefulnessReason.STRUCTURED_MATCH_QUALIFIED)
    if "semantic_similarity" in values:
        signal_available = True
        if values["semantic_similarity"] >= validated_policy["semantic_similarity_floor"]:
            qualifying_signal = True
            reasons.add(EvidenceUsefulnessReason.SEMANTIC_SIMILARITY_QUALIFIED)
    if not signal_available:
        exclusions.add(EvidenceUsefulnessReason.RETRIEVAL_SIGNAL_UNAVAILABLE)
    elif not qualifying_signal:
        exclusions.add(EvidenceUsefulnessReason.RETRIEVAL_SIGNAL_BELOW_FLOOR)

    if "source_agreement" in values and values["source_agreement"] >= validated_policy["source_agreement_floor"]:
        reasons.add(EvidenceUsefulnessReason.SOURCE_AGREEMENT_QUALIFIED)

    if not validated_record["trust"]["supplied_trust_available"]:
        reasons.add(EvidenceUsefulnessReason.SUPPLIED_TRUST_UNAVAILABLE)
        if validated_policy["supplied_trust_floor_available"]:
            exclusions.add(EvidenceUsefulnessReason.SUPPLIED_TRUST_REQUIRED_UNAVAILABLE)
    else:
        reasons.add(EvidenceUsefulnessReason.SUPPLIED_TRUST_AVAILABLE)
        if validated_policy["supplied_trust_floor_available"]:
            if validated_record["trust"]["supplied_trust"] < validated_policy["supplied_trust_floor"]:
                exclusions.add(EvidenceUsefulnessReason.SUPPLIED_TRUST_BELOW_FLOOR)
            else:
                reasons.add(EvidenceUsefulnessReason.SUPPLIED_TRUST_FLOOR_SATISFIED)

    reasons.update(exclusions)
    result = evidence_usefulness_decision(
        validated_record["proposition_id"],
        not exclusions,
        tuple(sorted(reasons, key=lambda reason: reason.value)),
        validated_policy["policy_version"],
    )
    return result


def token(value: object, name: str, maximum_bytes: int = 256) -> str:
    if not isinstance(value, str) or not value:
        raise InvalidRequestError(f"{name} must be a non-empty string")
    if len(value.encode("utf-8")) > maximum_bytes:
        raise InvalidRequestError(f"{name} exceeds the limit of {maximum_bytes} UTF-8 bytes")
    if any(character.isspace() or ord(character) < 32 or ord(character) == 127 for character in value):
        raise InvalidRequestError(f"{name} must not contain whitespace or control characters")
    return value


def internal_timestamp(value: str) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise InvalidRequestError("Proposition eligibility time must be canonical RFC 3339 UTC")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as error:
        raise InvalidRequestError("Proposition eligibility time must be canonical RFC 3339 UTC") from error
    if parsed.isoformat().replace("+00:00", "Z") != value:
        raise InvalidRequestError("Proposition eligibility time must use the canonical UTC representation")
    return parsed


def visibility_authorization(
    allowed: object,
    scope: object,
    ownership: object,
    authority_id: object,
    policy_version: object,
    reason_code: object,
) -> dict:
    """Build one trusted exact-scope decision for a non-public ownership category."""
    if not isinstance(allowed, bool):
        raise InvalidRequestError("visibility authorization allowed must be a boolean")
    try:
        validated_scope = validate_scope_key(scope)
    except IdentityValidationError as error:
        raise InvalidRequestError("visibility authorization scope must be a ScopeKey") from error
    if not isinstance(ownership, PropositionOwnership) or ownership not in {
        PropositionOwnership.COMPANY,
        PropositionOwnership.CUSTOMER,
    }:
        raise InvalidRequestError("visibility authorization ownership must be COMPANY or CUSTOMER")
    normalized_authority = token(authority_id, "visibility authorization authority_id")
    normalized_policy = token(policy_version, "visibility authorization policy_version")
    normalized_reason = token(reason_code, "visibility authorization reason_code", 96)
    result: dict = {
        "allowed": allowed,
        "scope": validated_scope,
        "ownership": ownership,
        "authority_id": normalized_authority,
        "policy_version": normalized_policy,
        "reason_code": normalized_reason,
    }
    return result


def validate_visibility_authorization(value: object) -> dict:
    data = exact_mapping(value, "VisibilityAuthorization", VISIBILITY_AUTHORIZATION_FIELDS)
    result = visibility_authorization(
        data["allowed"],
        data["scope"],
        data["ownership"],
        data["authority_id"],
        data["policy_version"],
        data["reason_code"],
    )
    return result


def visibility_grant(scope: object, ownership: object) -> dict:
    """Build one exact typed scope/ownership authorization."""
    try:
        validated_scope = validate_scope_key(scope)
    except IdentityValidationError as error:
        raise InvalidRequestError("visibility grant scope must be a ScopeKey") from error
    if not isinstance(ownership, PropositionOwnership) or ownership not in {
        PropositionOwnership.COMPANY,
        PropositionOwnership.CUSTOMER,
    }:
        raise InvalidRequestError("visibility grant ownership must be COMPANY or CUSTOMER")
    result: dict = {"scope": validated_scope, "ownership": ownership}
    return result


def validate_visibility_grant(value: object) -> dict:
    data = exact_mapping(value, "VisibilityGrant", VISIBILITY_GRANT_FIELDS)
    result = visibility_grant(data["scope"], data["ownership"])
    return result


class ExactScopeVisibilityAuthority:
    """Configured allow-list that compares complete ScopeKey values by equality."""

    def __init__(self, authority_id: str, policy_version: str, grants: tuple[dict, ...]) -> None:
        self.authority_id = token(authority_id, "visibility authority_id")
        self.policy_version = token(policy_version, "visibility policy_version")
        if not isinstance(grants, tuple):
            raise InvalidRequestError("visibility grants must be a tuple of VisibilityGrant values")
        if len(grants) > MAX_VISIBILITY_GRANTS:
            raise InvalidRequestError(f"visibility grants exceeds the limit of {MAX_VISIBILITY_GRANTS}")
        try:
            validated_grants = tuple(validate_visibility_grant(grant) for grant in grants)
        except InvalidRequestError as error:
            raise InvalidRequestError("visibility grants must be a tuple of VisibilityGrant values") from error
        keys = tuple((scope_key_signature(grant["scope"]), grant["ownership"]) for grant in validated_grants)
        if len(keys) != len(set(keys)):
            raise InvalidRequestError("visibility grants must be unique")
        self.internal_grants = set(keys)

    def evaluate(self, scope: dict, ownership: PropositionOwnership) -> dict:
        try:
            scope = validate_scope_key(scope)
        except IdentityValidationError as error:
            raise InvalidRequestError("visibility evaluation scope must be a ScopeKey") from error
        if ownership not in {PropositionOwnership.COMPANY, PropositionOwnership.CUSTOMER}:
            raise InvalidRequestError("visibility evaluation ownership must be COMPANY or CUSTOMER")
        allowed = (scope_key_signature(scope), ownership) in self.internal_grants
        reason_code = "exact_scope_granted" if allowed else "exact_scope_denied"
        result = visibility_authorization(
            allowed,
            scope,
            ownership,
            self.authority_id,
            self.policy_version,
            reason_code,
        )
        return result


def proposition_eligibility_decision(
    projection: object,
    eligible: object,
    reason: object,
    disclosure: object = (),
    disclosure_available: object = False,
    revalidated: object = False,
) -> dict:
    """Build one fail-closed Proposition projection eligibility decision."""
    validated_projection = validate_proposition_projection(projection)
    if not isinstance(eligible, bool):
        raise InvalidRequestError("Proposition eligibility eligible must be a boolean")
    if not isinstance(reason, PropositionEligibilityReason):
        raise InvalidRequestError("Proposition eligibility reason must be a PropositionEligibilityReason")
    selected_disclosure = empty_disclosure_decision() if type(disclosure) is tuple and not disclosure else disclosure
    try:
        validated_disclosure = validate_disclosure_decision(selected_disclosure)
    except InvalidRequestError as error:
        raise InvalidRequestError("Proposition eligibility disclosure must be a DisclosureDecision") from error
    if not isinstance(disclosure_available, bool):
        raise InvalidRequestError("Proposition eligibility disclosure_available must be a boolean")
    if not isinstance(revalidated, bool):
        raise InvalidRequestError("Proposition eligibility revalidated must be a boolean")
    if eligible != disclosure_available:
        raise InvalidRequestError("eligible Proposition decisions require an available disclosure decision")
    if not disclosure_available and validated_disclosure != empty_disclosure_decision():
        raise InvalidRequestError("unavailable Proposition disclosure must use the concrete empty decision")
    eligible_reasons = {
        PropositionEligibilityReason.ELIGIBLE_PUBLIC,
        PropositionEligibilityReason.ELIGIBLE_TRUSTED_SCOPE,
    }
    if eligible != (reason in eligible_reasons):
        raise InvalidRequestError("Proposition eligibility reason conflicts with eligible state")
    result: dict = {
        "projection": validated_projection,
        "eligible": eligible,
        "reason": reason,
        "disclosure": validated_disclosure,
        "disclosure_available": disclosure_available,
        "revalidated": revalidated,
    }
    return result


def validate_proposition_eligibility_decision(value: object) -> dict:
    data = exact_mapping(value, "PropositionEligibilityDecision", PROPOSITION_ELIGIBILITY_DECISION_FIELDS)
    result = proposition_eligibility_decision(
        data["projection"],
        data["eligible"],
        data["reason"],
        data["disclosure"],
        data["disclosure_available"],
        data["revalidated"],
    )
    return result


def proposition_eligibility_decision_with_changes(value: object, changes: object) -> dict:
    decision = validate_proposition_eligibility_decision(value)
    if not isinstance(changes, Mapping):
        raise InvalidRequestError("Proposition eligibility decision changes must be an object")
    if not set(changes).issubset(PROPOSITION_ELIGIBILITY_DECISION_FIELDS):
        raise InvalidRequestError("Proposition eligibility decision changes contain an unknown field")
    updated: dict[str, object] = dict(decision)
    updated.update(changes)
    result = validate_proposition_eligibility_decision(updated)
    return result


def proposition_exclusion_decision(
    projection: dict,
    reason: PropositionEligibilityReason,
    *,
    revalidated: bool = False,
) -> dict:
    """Construct one ineligible Proposition decision."""
    result = proposition_eligibility_decision(projection, False, reason, revalidated=revalidated)
    return result


def proposition_validity_inputs_from_eligibility(
    decision: dict,
    frame: dict,
) -> dict:
    """Derive validity inputs from an eligible publication-time decision."""
    decision = validate_proposition_eligibility_decision(decision)
    if (
        not decision.get("eligible", False)
        or not decision.get("revalidated", False)
        or not decision.get("disclosure_available", False)
    ):
        raise InvalidRequestError("Proposition validity inputs require an eligible revalidated decision")
    projection = decision.get("projection", {})
    temporal = frame.get("temporal_query", {})
    evaluation_time = internal_timestamp(frame.get("eligibility_context", {})["evaluation_time"])
    effective_system_to = projection["system_to"]
    effective_system_to_available = projection["system_to_available"]
    if projection["invalidated_at_available"] and (
        not effective_system_to_available
        or internal_timestamp(projection["invalidated_at"]) < internal_timestamp(effective_system_to)
    ):
        effective_system_to = projection["invalidated_at"]
        effective_system_to_available = True
    result = proposition_validity_inputs(
        frame.get("eligibility_context", {})["evaluation_time"],
        not projection["invalidated_at_available"],
        interval_contains(
            evaluation_time,
            projection["system_from"],
            projection["system_from_available"],
            effective_system_to,
            effective_system_to_available,
        ),
        interval_contains(
            evaluation_time,
            projection["valid_from"],
            projection["valid_from_available"],
            projection["valid_to"],
            projection["valid_to_available"],
        ),
        valid_from=projection["valid_from"],
        valid_from_available=projection["valid_from_available"],
        valid_to=projection["valid_to"],
        valid_to_available=projection["valid_to_available"],
        temporal_operator=temporal["operator"],
        temporal_axis=temporal["axis"],
        requested_start=temporal["start"],
        requested_start_available=temporal["start_available"],
        requested_end=temporal["end"],
        requested_end_available=temporal["end_available"],
        system_from=projection["system_from"],
        system_from_available=projection["system_from_available"],
        system_to=projection["system_to"],
        system_to_available=projection["system_to_available"],
        invalidated_at=projection["invalidated_at"],
        invalidated_at_available=projection["invalidated_at_available"],
        eligible_for_request=True,
        system_time_match=True,
        valid_time_match=temporal["axis"] == TemporalAxis.VALID_TIME,
        valid_time_match_available=temporal["axis"] == TemporalAxis.VALID_TIME,
    )
    return result


def interval_contains(
    point: datetime,
    lower: str,
    lower_available: bool,
    upper: str,
    upper_available: bool,
) -> bool:
    """Return half-open point containment for an interval with concrete open bounds."""
    after_lower = not lower_available or point >= internal_timestamp(lower)
    before_upper = not upper_available or point < internal_timestamp(upper)
    result = after_lower and before_upper
    return result


def interval_overlaps(
    requested_start: str,
    requested_start_available: bool,
    requested_end: str,
    requested_end_available: bool,
    lower: str,
    lower_available: bool,
    upper: str,
    upper_available: bool,
) -> bool:
    """Return half-open overlap without inventing values for open bounds."""
    starts_before_request_end = (
        not requested_end_available or not lower_available or internal_timestamp(lower) < internal_timestamp(requested_end)
    )
    ends_after_request_start = (
        not requested_start_available or not upper_available or internal_timestamp(upper) > internal_timestamp(requested_start)
    )
    result = starts_before_request_end and ends_after_request_start
    return result


def requested_interval_match(
    operator: TemporalQueryOperator,
    requested_start: str,
    requested_start_available: bool,
    requested_end: str,
    requested_end_available: bool,
    lower: str,
    lower_available: bool,
    upper: str,
    upper_available: bool,
) -> bool:
    if operator == TemporalQueryOperator.AS_OF:
        result = interval_contains(internal_timestamp(requested_start), lower, lower_available, upper, upper_available)
        return result
    result = interval_overlaps(
        requested_start,
        requested_start_available,
        requested_end,
        requested_end_available,
        lower,
        lower_available,
        upper,
        upper_available,
    )
    return result


def outside_interval_reason(
    requested_start: str,
    requested_start_available: bool,
    requested_end: str,
    requested_end_available: bool,
    lower: str,
    lower_available: bool,
    not_yet_reason: PropositionEligibilityReason,
    no_longer_reason: PropositionEligibilityReason,
) -> PropositionEligibilityReason:
    if requested_end_available and lower_available and internal_timestamp(lower) >= internal_timestamp(requested_end):
        result = not_yet_reason
        return result
    if (
        requested_start_available
        and not requested_end_available
        and lower_available
        and internal_timestamp(lower) > internal_timestamp(requested_start)
    ):
        result = not_yet_reason
        return result
    result = no_longer_reason
    return result


class PropositionEligibilityEvaluator:
    """Shared temporal and disclosure policy over strict Proposition projections."""

    def __init__(self, visibility_authority: object = ()) -> None:
        if type(visibility_authority) is tuple and not visibility_authority:
            selected_authority = ()
        elif callable(getattr(visibility_authority, "evaluate", ())):
            selected_authority = visibility_authority
        else:
            raise InvalidRequestError("visibility authority must implement evaluate")
        self.internal_visibility_authority = selected_authority

    def evaluate(self, projection: dict, frame: dict) -> dict:
        projection = validate_proposition_projection(projection)
        frame = validate_query_frame(frame)
        context = frame.get("eligibility_context", {})
        if not context["evaluation_time_available"]:
            result = proposition_exclusion_decision(projection, PropositionEligibilityReason.EVALUATION_TIME_UNAVAILABLE)
            return result
        evaluation_time = internal_timestamp(context["evaluation_time"])
        temporal = frame.get("temporal_query", {})
        if not temporal["resolved"]:
            result = proposition_exclusion_decision(projection, PropositionEligibilityReason.TEMPORAL_QUERY_UNRESOLVED)
            return result
        if not projection.get("system_from_available", False):
            result = proposition_exclusion_decision(projection, PropositionEligibilityReason.SYSTEM_TIME_UNAVAILABLE)
            return result
        current_operator = temporal["operator"] in {
            TemporalQueryOperator.UNSPECIFIED,
            TemporalQueryOperator.CURRENT,
            TemporalQueryOperator.NOW,
        }
        if current_operator:
            if projection.get("invalidated_at_available", False):
                result = proposition_exclusion_decision(projection, PropositionEligibilityReason.PROPOSITION_INACTIVE)
                return result
            if evaluation_time < internal_timestamp(projection.get("system_from", "")):
                result = proposition_exclusion_decision(projection, PropositionEligibilityReason.SYSTEM_NOT_YET_CURRENT)
                return result
            if projection.get("system_to_available", False) and evaluation_time >= internal_timestamp(
                projection.get("system_to", "")
            ):
                result = proposition_exclusion_decision(projection, PropositionEligibilityReason.SYSTEM_NO_LONGER_CURRENT)
                return result
            if projection.get("valid_from_available", False) and evaluation_time < internal_timestamp(
                projection.get("valid_from", "")
            ):
                result = proposition_exclusion_decision(projection, PropositionEligibilityReason.VALID_TIME_NOT_YET_CURRENT)
                return result
            if projection.get("valid_to_available", False) and evaluation_time >= internal_timestamp(
                projection.get("valid_to", "")
            ):
                result = proposition_exclusion_decision(projection, PropositionEligibilityReason.VALID_TIME_NO_LONGER_CURRENT)
                return result
        elif temporal["axis"] == TemporalAxis.VALID_TIME:
            if projection.get("invalidated_at_available", False):
                result = proposition_exclusion_decision(projection, PropositionEligibilityReason.PROPOSITION_INACTIVE)
                return result
            if evaluation_time < internal_timestamp(projection.get("system_from", "")):
                result = proposition_exclusion_decision(projection, PropositionEligibilityReason.SYSTEM_NOT_YET_CURRENT)
                return result
            if projection.get("system_to_available", False) and evaluation_time >= internal_timestamp(
                projection.get("system_to", "")
            ):
                result = proposition_exclusion_decision(projection, PropositionEligibilityReason.SYSTEM_NO_LONGER_CURRENT)
                return result
            if (
                temporal["operator"] == TemporalQueryOperator.LATEST
                and projection.get("valid_from_available", False)
                and evaluation_time < internal_timestamp(projection.get("valid_from", ""))
            ):
                result = proposition_exclusion_decision(projection, PropositionEligibilityReason.VALID_TIME_NOT_YET_CURRENT)
                return result
            if temporal["operator"] != TemporalQueryOperator.LATEST and not requested_interval_match(
                temporal["operator"],
                temporal["start"],
                temporal["start_available"],
                temporal["end"],
                temporal["end_available"],
                projection.get("valid_from", ""),
                projection.get("valid_from_available", False),
                projection.get("valid_to", ""),
                projection.get("valid_to_available", False),
            ):
                reason = outside_interval_reason(
                    temporal["start"],
                    temporal["start_available"],
                    temporal["end"],
                    temporal["end_available"],
                    projection.get("valid_from", ""),
                    projection.get("valid_from_available", False),
                    PropositionEligibilityReason.VALID_TIME_NOT_YET_CURRENT,
                    PropositionEligibilityReason.VALID_TIME_NO_LONGER_CURRENT,
                )
                result = proposition_exclusion_decision(projection, reason)
                return result
        else:
            effective_system_to = projection.get("system_to", "")
            effective_system_to_available = projection.get("system_to_available", False)
            if projection.get("invalidated_at_available", False) and (
                not effective_system_to_available
                or internal_timestamp(projection.get("invalidated_at", "")) < internal_timestamp(effective_system_to)
            ):
                effective_system_to = projection.get("invalidated_at", "")
                effective_system_to_available = True
            if temporal["operator"] == TemporalQueryOperator.LATEST:
                if internal_timestamp(projection.get("system_from", "")) > evaluation_time:
                    result = proposition_exclusion_decision(projection, PropositionEligibilityReason.SYSTEM_NOT_YET_CURRENT)
                    return result
            elif not requested_interval_match(
                temporal["operator"],
                temporal["start"],
                temporal["start_available"],
                temporal["end"],
                temporal["end_available"],
                projection.get("system_from", ""),
                projection.get("system_from_available", False),
                effective_system_to,
                effective_system_to_available,
            ):
                reason = outside_interval_reason(
                    temporal["start"],
                    temporal["start_available"],
                    temporal["end"],
                    temporal["end_available"],
                    projection.get("system_from", ""),
                    projection.get("system_from_available", False),
                    PropositionEligibilityReason.SYSTEM_NOT_YET_CURRENT,
                    PropositionEligibilityReason.SYSTEM_NO_LONGER_CURRENT,
                )
                result = proposition_exclusion_decision(projection, reason)
                return result
        if not projection.get("predicate_canonical", False) or projection.get("predicate_id", "") == "generic_relation":
            result = proposition_exclusion_decision(projection, PropositionEligibilityReason.RETRIEVAL_ONLY)
            return result

        ownership = PropositionOwnership(projection.get("ownership_category", PropositionOwnership.PUBLIC))
        if ownership == PropositionOwnership.PUBLIC:
            disclosure = disclosure_decision(
                ownership,
                DisclosureBasis.PUBLIC_RULE,
                frame.get("scope", {}),
                PROPOSITION_DISCLOSURE_POLICY_VERSION,
            )
            result = proposition_eligibility_decision(
                projection,
                True,
                PropositionEligibilityReason.ELIGIBLE_PUBLIC,
                disclosure,
                True,
            )
            return result

        authority_method = getattr(self.internal_visibility_authority, "evaluate", ())
        if not callable(authority_method):
            result = proposition_exclusion_decision(projection, PropositionEligibilityReason.VISIBILITY_AUTHORITY_UNAVAILABLE)
            return result
        try:
            authorization = authority_method(frame.get("scope", {}), ownership)
        except Exception:
            result = proposition_exclusion_decision(projection, PropositionEligibilityReason.VISIBILITY_AUTHORITY_FAILED)
            return result
        try:
            authorization = validate_visibility_authorization(authorization)
        except InvalidRequestError:
            result = proposition_exclusion_decision(projection, PropositionEligibilityReason.VISIBILITY_AUTHORITY_FAILED)
            return result
        if authorization["scope"] != frame.get("scope", {}):
            result = proposition_exclusion_decision(projection, PropositionEligibilityReason.VISIBILITY_SCOPE_MISMATCH)
            return result
        if authorization["ownership"] != ownership:
            result = proposition_exclusion_decision(projection, PropositionEligibilityReason.VISIBILITY_OWNERSHIP_MISMATCH)
            return result
        if not authorization["allowed"]:
            result = proposition_exclusion_decision(projection, PropositionEligibilityReason.VISIBILITY_DENIED)
            return result
        disclosure = disclosure_decision(
            ownership,
            DisclosureBasis.TRUSTED_SCOPE_AUTHORITY,
            frame.get("scope", {}),
            authorization["policy_version"],
            authorization["authority_id"],
            True,
        )
        result = proposition_eligibility_decision(
            projection,
            True,
            PropositionEligibilityReason.ELIGIBLE_TRUSTED_SCOPE,
            disclosure,
            True,
        )
        return result

    def revalidate(
        self,
        discovered: dict,
        frame: dict,
        current_proposition_projection: object,
    ) -> dict:
        if not callable(current_proposition_projection):
            result = proposition_exclusion_decision(discovered, PropositionEligibilityReason.REVALIDATION_UNAVAILABLE)
            return result
        discovered = validate_proposition_projection(discovered)
        try:
            current = current_proposition_projection(discovered.get("proposition_id", ""))
        except Exception:
            result = proposition_exclusion_decision(discovered, PropositionEligibilityReason.REVALIDATION_UNAVAILABLE)
            return result
        if not isinstance(current, tuple) or len(current) != 1:
            result = proposition_exclusion_decision(discovered, PropositionEligibilityReason.REVALIDATION_MISSING)
            return result
        try:
            projection = validate_proposition_projection(current[0])
        except InvalidRequestError:
            result = proposition_exclusion_decision(discovered, PropositionEligibilityReason.REVALIDATION_MISSING)
            return result
        if projection["projection_id"] != PropositionProjectionQuery.BY_ID_V1:
            result = proposition_exclusion_decision(
                projection, PropositionEligibilityReason.REVALIDATION_IDENTITY_CONFLICT, revalidated=True
            )
            return result
        discovered_identity = (
            discovered.get("proposition_id", ""),
            discovered.get("subject_entity_id", ""),
            discovered.get("predicate_id", ""),
            discovered.get("object_entity_id", ""),
        )
        current_identity = (
            projection["proposition_id"],
            projection["subject_entity_id"],
            projection["predicate_id"],
            projection["object_entity_id"],
        )
        if discovered_identity != current_identity:
            result = proposition_exclusion_decision(
                projection, PropositionEligibilityReason.REVALIDATION_IDENTITY_CONFLICT, revalidated=True
            )
            return result
        decision = self.evaluate(projection, frame)
        result = proposition_eligibility_decision_with_changes(decision, {"revalidated": True})
        return result


def revalidate_propositions(
    projections: tuple[dict, ...],
    frame: dict,
    evaluator: PropositionEligibilityEvaluator,
    current_proposition_projection: object,
    cooperative_check: object = no_cooperative_check,
) -> tuple[dict, ...]:
    """Revalidate a bounded projection batch immediately before package construction."""
    if not isinstance(projections, tuple) or len(projections) > 1_000:
        raise InvalidRequestError("Proposition revalidation projections must be a tuple of at most 1000 values")
    validated_projections = tuple(validate_proposition_projection(projection) for projection in projections)
    if not isinstance(evaluator, PropositionEligibilityEvaluator):
        raise InvalidRequestError("Proposition revalidation evaluator must be PropositionEligibilityEvaluator")
    if not callable(cooperative_check):
        raise InvalidRequestError("Proposition revalidation cooperative_check must be callable")
    decisions = []
    for projection in validated_projections:
        cooperative_check()
        decisions.append(evaluator.revalidate(projection, frame, current_proposition_projection))
    cooperative_check()
    result = tuple(decisions)
    return result


def proposition_evidence_record(
    discovered: dict,
    decision: dict,
    frame: dict,
    source_resolver: str,
) -> dict:
    """Construct one strict full record only from eligible revalidated state."""
    discovered = validate_proposition_projection(discovered)
    decision = validate_proposition_eligibility_decision(decision)
    source = token(source_resolver, "Proposition evidence source_resolver", 96)
    if source not in PROPOSITION_EVIDENCE_PRODUCERS:
        raise InvalidRequestError("Proposition evidence source_resolver is not an allowed producer")
    if source == "structured_graph" and discovered.get("projection_id", PropositionProjectionQuery.BY_ID_V1) not in {
        PropositionProjectionQuery.STRUCTURED_ENTITY_V1,
        PropositionProjectionQuery.STRUCTURED_KEYWORD_V1,
        PropositionProjectionQuery.RELATION_ONE_HOP_V1,
    }:
        raise InvalidRequestError("structured Proposition evidence requires a structured discovery projection")
    if (
        source == "support_semantic"
        and discovered.get("projection_id", PropositionProjectionQuery.BY_ID_V1) != PropositionProjectionQuery.VECTOR_V1
    ):
        raise InvalidRequestError("semantic Proposition evidence requires a vector discovery projection")
    if (
        not decision.get("eligible", False)
        or not decision.get("revalidated", False)
        or not decision.get("disclosure_available", False)
    ):
        raise InvalidRequestError("Proposition evidence construction requires an eligible revalidated decision")
    current = decision.get("projection", {})
    discovered_identity = (
        discovered.get("proposition_id", ""),
        discovered.get("subject_entity_id", ""),
        discovered.get("predicate_id", ""),
        discovered.get("object_entity_id", ""),
    )
    current_identity = (
        current["proposition_id"],
        current["subject_entity_id"],
        current["predicate_id"],
        current["object_entity_id"],
    )
    if discovered_identity != current_identity:
        raise InvalidRequestError("Proposition evidence discovery and current canonical identity conflict")
    values = {"canonical_completeness": 1.0}
    unavailable = ["source_agreement"]
    reasons = [
        decision.get(
            "reason",
            PropositionEligibilityReason.REVALIDATION_UNAVAILABLE,
        ).value,
        "canonical_complete",
    ]
    if discovered.get("structured_match_available", False):
        values["structured_match"] = discovered.get("structured_match", 0.0)
        reasons.append("structured_match")
    else:
        unavailable.append("structured_match")
    if discovered.get("semantic_similarity_available", False):
        values["semantic_similarity"] = discovered.get("semantic_similarity", 0.0)
        reasons.append("semantic_similarity")
    else:
        unavailable.append("semantic_similarity")
    if current["supplied_trust_available"]:
        values["supplied_trust"] = current["supplied_trust"]
        reasons.append("supplied_trust_available")
    else:
        unavailable.append("supplied_trust")
        reasons.append("supplied_trust_unavailable")
    result = build_proposition_evidence_record(
        proposition_id=current["proposition_id"],
        source_resolver=source,
        source_contributions=(source,),
        features=feature_set(values=values, unavailable=tuple(sorted(unavailable))),
        canonical_references=canonical_proposition_references(
            current["subject_entity_id"],
            current["predicate_id"],
            current["object_entity_id"],
        ),
        validity=proposition_validity_inputs_from_eligibility(decision, frame),
        trust=proposition_trust_inputs(
            current["trust_category"],
            current["trust_category_available"],
            current["supplied_trust"],
            current["supplied_trust_available"],
            current["supplied_trust_version"],
            current["supplied_trust_version_available"],
        ),
        disclosure=decision.get("disclosure", {}),
        path=(current["proposition_id"],),
        selection_reasons=tuple(sorted(reasons)),
    )
    return result


def merge_proposition_evidence_group(records: tuple[dict, ...]) -> dict:
    if not records:
        raise InvalidRequestError("cannot merge an empty Proposition evidence group")
    ordered = tuple(sorted(records, key=lambda record: (record["source_resolver"], proposition_evidence_record_to_json(record))))
    base = ordered[0]
    for record in ordered[1:]:
        if record["canonical_references"] != base["canonical_references"]:
            raise InvalidRequestError(f"conflicting canonical references for Proposition evidence ID: {base['proposition_id']}")
        current_state = (record["validity"], record["trust"], record["disclosure"], record["path"])
        base_state = (base["validity"], base["trust"], base["disclosure"], base["path"])
        if current_state != base_state:
            raise InvalidRequestError(f"conflicting current evidence state for Proposition evidence ID: {base['proposition_id']}")

    sources = tuple(sorted({source for record in ordered for source in record["source_contributions"]}))
    if not sources:
        raise InvalidRequestError("merged Proposition evidence sources must not be empty")
    if len(sources) > MAX_PROPOSITION_SOURCE_CONTRIBUTIONS:
        raise InvalidRequestError(f"merged Proposition evidence sources exceed the limit of {MAX_PROPOSITION_SOURCE_CONTRIBUTIONS}")
    primary_source = next(iter(sources))
    values: dict[str, float] = {}
    unavailable = set()
    reasons = set()
    for record in ordered:
        reasons.update(record["selection_reasons"])
        unavailable.update(record["features"]["unavailable"])
        for name, value in record["features"]["values"].items():
            if name in values and values.get(name, 0.0) != value:
                raise InvalidRequestError(
                    f"conflicting measured feature {name} for Proposition evidence ID: {base['proposition_id']}"
                )
            values[name] = value
    if len(sources) > 1:
        if "source_agreement" in values and values.get("source_agreement", 0.0) != 1.0:
            raise InvalidRequestError(
                f"conflicting measured feature source_agreement for Proposition evidence ID: {base['proposition_id']}"
            )
        values["source_agreement"] = 1.0
        reasons.add("source_agreement")
    elif "source_agreement" in values:
        raise InvalidRequestError(
            f"source_agreement requires multiple sources for Proposition evidence ID: {base['proposition_id']}"
        )
    unavailable.difference_update(values)
    if len(reasons) > MAX_PROPOSITION_SELECTION_REASONS:
        raise InvalidRequestError(f"merged Proposition evidence reasons exceed the limit of {MAX_PROPOSITION_SELECTION_REASONS}")
    result = proposition_evidence_record_with_changes(
        base,
        {
            "source_resolver": primary_source,
            "source_contributions": sources,
            "features": feature_set(values=values, unavailable=tuple(sorted(unavailable))),
            "selection_reasons": tuple(sorted(reasons)),
        },
    )
    return result


def canonicalize_proposition_evidence(
    records: tuple[dict, ...],
    cooperative_check: object = no_cooperative_check,
) -> tuple[dict, ...]:
    """Deterministically deduplicate and merge strict records by stable Proposition ID."""
    if not isinstance(records, tuple) or len(records) > MAX_RESOLUTION_VALUES:
        raise InvalidRequestError(f"Proposition evidence normalization requires a tuple of at most {MAX_RESOLUTION_VALUES} records")
    try:
        validated_records = tuple(validate_proposition_evidence_record(record) for record in records)
    except InvalidRequestError as error:
        raise InvalidRequestError("Proposition evidence normalization requires PropositionEvidenceRecord values") from error
    if not callable(cooperative_check):
        raise InvalidRequestError("Proposition evidence normalization cooperative_check must be callable")
    grouped: dict[str, list[dict]] = {}
    for record in validated_records:
        cooperative_check()
        grouped.setdefault(record["proposition_id"], []).append(record)
    merged = []
    for proposition_id in sorted(grouped):
        cooperative_check()
        merged.append(merge_proposition_evidence_group(tuple(grouped.get(proposition_id, []))))
    cooperative_check()
    result = tuple(merged)
    return result
