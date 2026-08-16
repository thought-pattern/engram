"""Current-time disclosure eligibility for response-less Claim evidence."""

import json
import math
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import datetime
from enum import StrEnum
from typing import Protocol

from engram.constants import CLAIM_EVIDENCE_PRODUCERS
from engram.errors import InvalidRequestError
from engram.graph import ClaimProjection, ClaimProjectionQuery
from engram.identity import ScopeKey
from engram.resolution import (
    MAX_CLAIM_SELECTION_REASONS,
    MAX_CLAIM_SOURCE_CONTRIBUTIONS,
    MAX_RESOLUTION_VALUES,
    CanonicalClaimReferences,
    ClaimEvidenceRecord,
    ClaimOwnership,
    ClaimTrustInputs,
    ClaimValidityInputs,
    DisclosureBasis,
    DisclosureDecision,
    FeatureSet,
    QueryFrame,
)

CLAIM_DISCLOSURE_POLICY_VERSION = "claim-disclosure-v1"
CLAIM_EVIDENCE_USEFULNESS_POLICY_VERSION = "claim-evidence-usefulness-v1"
CANONICAL_COMPLETENESS_FLOOR_V1 = 1.0
STRUCTURED_MATCH_FLOOR_V1 = 1.0
SEMANTIC_SIMILARITY_FLOOR_V1 = 0.60
SOURCE_AGREEMENT_FLOOR_V1 = 1.0
MAX_VISIBILITY_GRANTS = 4_096
EMPTY_DISCLOSURE_DECISION = DisclosureDecision(
    ownership=ClaimOwnership.PUBLIC,
    basis=DisclosureBasis.PUBLIC_RULE,
    scope=ScopeKey(),
    policy_version="unavailable",
)


def _no_cooperative_check() -> None:
    return


class ClaimEligibilityReason(StrEnum):
    """Closed current-time Claim evidence eligibility outcomes."""

    ELIGIBLE_PUBLIC = "eligible_public"
    ELIGIBLE_TRUSTED_SCOPE = "eligible_trusted_scope"
    EVALUATION_TIME_UNAVAILABLE = "evaluation_time_unavailable"
    CLAIM_INACTIVE = "claim_inactive"
    SYSTEM_TIME_UNAVAILABLE = "system_time_unavailable"
    SYSTEM_NOT_YET_CURRENT = "system_not_yet_current"
    SYSTEM_NO_LONGER_CURRENT = "system_no_longer_current"
    VALID_TIME_NOT_YET_CURRENT = "valid_time_not_yet_current"
    VALID_TIME_NO_LONGER_CURRENT = "valid_time_no_longer_current"
    RETRIEVAL_ONLY = "retrieval_only"
    VISIBILITY_AUTHORITY_UNAVAILABLE = "visibility_authority_unavailable"
    VISIBILITY_AUTHORITY_FAILED = "visibility_authority_failed"
    VISIBILITY_SCOPE_MISMATCH = "visibility_scope_mismatch"
    VISIBILITY_OWNERSHIP_MISMATCH = "visibility_ownership_mismatch"
    VISIBILITY_DENIED = "visibility_denied"
    REVALIDATION_UNAVAILABLE = "revalidation_unavailable"
    REVALIDATION_MISSING = "revalidation_missing"
    REVALIDATION_IDENTITY_CONFLICT = "revalidation_identity_conflict"


class EvidenceUsefulnessReason(StrEnum):
    """Stable inspectable reasons from the unfitted version-1 inclusion policy."""

    CANONICAL_COMPLETENESS_UNAVAILABLE = "canonical_completeness_unavailable"
    CANONICAL_COMPLETENESS_BELOW_FLOOR = "canonical_completeness_below_floor"
    STRUCTURED_MATCH_QUALIFIED = "structured_match_qualified"
    SEMANTIC_SIMILARITY_QUALIFIED = "semantic_similarity_qualified"
    RETRIEVAL_SIGNAL_UNAVAILABLE = "retrieval_signal_unavailable"
    RETRIEVAL_SIGNAL_BELOW_FLOOR = "retrieval_signal_below_floor"
    SOURCE_AGREEMENT_QUALIFIED = "source_agreement_qualified"
    SUPPLIED_TRUST_AVAILABLE = "supplied_trust_available"
    SUPPLIED_TRUST_UNAVAILABLE = "supplied_trust_unavailable"
    SUPPLIED_TRUST_FLOOR_SATISFIED = "supplied_trust_floor_satisfied"
    SUPPLIED_TRUST_REQUIRED_UNAVAILABLE = "supplied_trust_required_unavailable"
    SUPPLIED_TRUST_BELOW_FLOOR = "supplied_trust_below_floor"


@dataclass(frozen=True, slots=True)
class EvidenceUsefulnessDecision:
    """One content-free evidence inclusion decision."""

    claim_id: str
    included: bool
    reasons: tuple[EvidenceUsefulnessReason, ...]
    policy_version: str = CLAIM_EVIDENCE_USEFULNESS_POLICY_VERSION

    def __post_init__(self) -> None:
        _token(self.claim_id, "evidence usefulness claim_id")
        if not isinstance(self.included, bool):
            raise InvalidRequestError("evidence usefulness included must be a boolean")
        if self.policy_version != CLAIM_EVIDENCE_USEFULNESS_POLICY_VERSION:
            raise InvalidRequestError(f"unsupported evidence usefulness policy_version: {self.policy_version}")
        if not isinstance(self.reasons, tuple) or not self.reasons:
            raise InvalidRequestError("evidence usefulness reasons must be a non-empty tuple")
        if not all(isinstance(reason, EvidenceUsefulnessReason) for reason in self.reasons):
            raise InvalidRequestError("evidence usefulness reasons are invalid")
        if self.reasons != tuple(sorted(set(self.reasons), key=lambda reason: reason.value)):
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
        has_exclusion = bool(exclusion_reasons.intersection(self.reasons))
        has_qualifying_signal = bool(qualifying_reasons.intersection(self.reasons))
        if self.included and (has_exclusion or not has_qualifying_signal):
            raise InvalidRequestError("included evidence usefulness decision has inconsistent reasons")
        if not self.included and not has_exclusion:
            raise InvalidRequestError("excluded evidence usefulness decision requires an exclusion reason")

    def to_dict(self) -> dict[str, object]:
        return {
            "policy_version": self.policy_version,
            "claim_id": self.claim_id,
            "included": self.included,
            "reasons": [reason.value for reason in self.reasons],
        }


@dataclass(frozen=True, slots=True)
class EvidenceUsefulnessPolicy:
    """Frozen hand-authored v1 policy; Section 16 owns empirical calibration."""

    policy_version: str = CLAIM_EVIDENCE_USEFULNESS_POLICY_VERSION
    canonical_completeness_floor: float = CANONICAL_COMPLETENESS_FLOOR_V1
    structured_match_floor: float = STRUCTURED_MATCH_FLOOR_V1
    semantic_similarity_floor: float = SEMANTIC_SIMILARITY_FLOOR_V1
    source_agreement_floor: float = SOURCE_AGREEMENT_FLOOR_V1
    supplied_trust_floor: float = 0.0
    supplied_trust_floor_available: bool = False

    def __post_init__(self) -> None:
        if self.policy_version != CLAIM_EVIDENCE_USEFULNESS_POLICY_VERSION:
            raise InvalidRequestError(f"unsupported evidence usefulness policy_version: {self.policy_version}")
        frozen = {
            "canonical_completeness_floor": CANONICAL_COMPLETENESS_FLOOR_V1,
            "structured_match_floor": STRUCTURED_MATCH_FLOOR_V1,
            "semantic_similarity_floor": SEMANTIC_SIMILARITY_FLOOR_V1,
            "source_agreement_floor": SOURCE_AGREEMENT_FLOOR_V1,
        }
        for name, expected in frozen.items():
            value = getattr(self, name)
            if not isinstance(value, float) or not math.isfinite(value) or value != expected:
                raise InvalidRequestError(f"evidence usefulness {name} is frozen at {expected}")
        if not isinstance(self.supplied_trust_floor_available, bool):
            raise InvalidRequestError("evidence usefulness supplied_trust_floor_available must be a boolean")
        if (
            not isinstance(self.supplied_trust_floor, float)
            or not math.isfinite(self.supplied_trust_floor)
            or not 0.0 <= self.supplied_trust_floor <= 1.0
        ):
            raise InvalidRequestError("evidence usefulness supplied_trust_floor must be a finite float in [0, 1]")
        if not self.supplied_trust_floor_available and self.supplied_trust_floor != 0.0:
            raise InvalidRequestError("unavailable evidence usefulness supplied_trust_floor must be zero")

    def to_dict(self) -> dict[str, object]:
        return {
            "policy_version": self.policy_version,
            "canonical_completeness_floor": self.canonical_completeness_floor,
            "structured_match_floor": self.structured_match_floor,
            "semantic_similarity_floor": self.semantic_similarity_floor,
            "source_agreement_floor": self.source_agreement_floor,
            "supplied_trust_floor": self.supplied_trust_floor,
            "supplied_trust_floor_available": self.supplied_trust_floor_available,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"), allow_nan=False)

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> "EvidenceUsefulnessPolicy":
        expected = frozenset(cls().to_dict())
        if not isinstance(value, Mapping) or frozenset(value) != expected:
            raise InvalidRequestError("EvidenceUsefulnessPolicy has invalid fields")
        policy_version = value["policy_version"]
        supplied_trust_floor_available = value["supplied_trust_floor_available"]
        if not isinstance(policy_version, str):
            raise InvalidRequestError("evidence usefulness policy_version must be a string")
        if not isinstance(supplied_trust_floor_available, bool):
            raise InvalidRequestError("evidence usefulness supplied_trust_floor_available must be a boolean")

        def required_float(name: str) -> float:
            raw_value = value[name]
            if not isinstance(raw_value, float):
                raise InvalidRequestError(f"evidence usefulness {name} must be a float")
            return raw_value

        return cls(
            policy_version=policy_version,
            canonical_completeness_floor=required_float("canonical_completeness_floor"),
            structured_match_floor=required_float("structured_match_floor"),
            semantic_similarity_floor=required_float("semantic_similarity_floor"),
            source_agreement_floor=required_float("source_agreement_floor"),
            supplied_trust_floor=required_float("supplied_trust_floor"),
            supplied_trust_floor_available=supplied_trust_floor_available,
        )

    @classmethod
    def from_json(cls, value: str) -> "EvidenceUsefulnessPolicy":
        if not isinstance(value, str):
            raise InvalidRequestError("EvidenceUsefulnessPolicy JSON must be a string")
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError as error:
            raise InvalidRequestError("EvidenceUsefulnessPolicy JSON must be valid JSON") from error
        if not isinstance(decoded, Mapping):
            raise InvalidRequestError("EvidenceUsefulnessPolicy JSON must decode to an object")
        return cls.from_dict(decoded)

    def evaluate(self, record: ClaimEvidenceRecord) -> EvidenceUsefulnessDecision:
        if not isinstance(record, ClaimEvidenceRecord):
            raise InvalidRequestError("evidence usefulness requires a ClaimEvidenceRecord")
        values = record.features.values
        reasons: set[EvidenceUsefulnessReason] = set()
        exclusions: set[EvidenceUsefulnessReason] = set()

        if "canonical_completeness" not in values:
            exclusions.add(EvidenceUsefulnessReason.CANONICAL_COMPLETENESS_UNAVAILABLE)
        elif values["canonical_completeness"] < self.canonical_completeness_floor:
            exclusions.add(EvidenceUsefulnessReason.CANONICAL_COMPLETENESS_BELOW_FLOOR)

        qualifying_signal = False
        signal_available = False
        if "structured_match" in values:
            signal_available = True
            if values["structured_match"] >= self.structured_match_floor:
                qualifying_signal = True
                reasons.add(EvidenceUsefulnessReason.STRUCTURED_MATCH_QUALIFIED)
        if "semantic_similarity" in values:
            signal_available = True
            if values["semantic_similarity"] >= self.semantic_similarity_floor:
                qualifying_signal = True
                reasons.add(EvidenceUsefulnessReason.SEMANTIC_SIMILARITY_QUALIFIED)
        if not signal_available:
            exclusions.add(EvidenceUsefulnessReason.RETRIEVAL_SIGNAL_UNAVAILABLE)
        elif not qualifying_signal:
            exclusions.add(EvidenceUsefulnessReason.RETRIEVAL_SIGNAL_BELOW_FLOOR)

        if "source_agreement" in values and values["source_agreement"] >= self.source_agreement_floor:
            reasons.add(EvidenceUsefulnessReason.SOURCE_AGREEMENT_QUALIFIED)

        if not record.trust.supplied_trust_available:
            reasons.add(EvidenceUsefulnessReason.SUPPLIED_TRUST_UNAVAILABLE)
            if self.supplied_trust_floor_available:
                exclusions.add(EvidenceUsefulnessReason.SUPPLIED_TRUST_REQUIRED_UNAVAILABLE)
        else:
            reasons.add(EvidenceUsefulnessReason.SUPPLIED_TRUST_AVAILABLE)
            if self.supplied_trust_floor_available:
                if record.trust.supplied_trust < self.supplied_trust_floor:
                    exclusions.add(EvidenceUsefulnessReason.SUPPLIED_TRUST_BELOW_FLOOR)
                else:
                    reasons.add(EvidenceUsefulnessReason.SUPPLIED_TRUST_FLOOR_SATISFIED)

        reasons.update(exclusions)
        return EvidenceUsefulnessDecision(
            claim_id=record.claim_id,
            included=not exclusions,
            reasons=tuple(sorted(reasons, key=lambda reason: reason.value)),
            policy_version=self.policy_version,
        )


def _token(value: object, name: str, maximum_bytes: int = 256) -> str:
    if not isinstance(value, str) or not value:
        raise InvalidRequestError(f"{name} must be a non-empty string")
    if len(value.encode("utf-8")) > maximum_bytes:
        raise InvalidRequestError(f"{name} exceeds the limit of {maximum_bytes} UTF-8 bytes")
    if any(character.isspace() or ord(character) < 32 or ord(character) == 127 for character in value):
        raise InvalidRequestError(f"{name} must not contain whitespace or control characters")
    return value


def _timestamp(value: str) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise InvalidRequestError("Claim eligibility time must be canonical RFC 3339 UTC")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as error:
        raise InvalidRequestError("Claim eligibility time must be canonical RFC 3339 UTC") from error
    if parsed.isoformat().replace("+00:00", "Z") != value:
        raise InvalidRequestError("Claim eligibility time must use the canonical UTC representation")
    return parsed


@dataclass(frozen=True, slots=True)
class VisibilityAuthorization:
    """One trusted exact-scope decision for a non-public ownership category."""

    allowed: bool
    scope: ScopeKey
    ownership: ClaimOwnership
    authority_id: str
    policy_version: str
    reason_code: str

    def __post_init__(self) -> None:
        if not isinstance(self.allowed, bool):
            raise InvalidRequestError("visibility authorization allowed must be a boolean")
        if not isinstance(self.scope, ScopeKey):
            raise InvalidRequestError("visibility authorization scope must be a ScopeKey")
        if self.ownership not in {ClaimOwnership.COMPANY, ClaimOwnership.CUSTOMER}:
            raise InvalidRequestError("visibility authorization ownership must be COMPANY or CUSTOMER")
        _token(self.authority_id, "visibility authorization authority_id")
        _token(self.policy_version, "visibility authorization policy_version")
        _token(self.reason_code, "visibility authorization reason_code", 96)


class VisibilityAuthority(Protocol):
    """Trusted capability that decides exact ScopeKey visibility."""

    def evaluate(self, scope: ScopeKey, ownership: ClaimOwnership) -> VisibilityAuthorization: ...


class ProjectionRevalidator(Protocol):
    """Fixed by-ID read capability used immediately before publication."""

    def current_claim_projection(self, claim_id: str) -> tuple[ClaimProjection, ...]: ...


@dataclass(frozen=True, slots=True)
class VisibilityGrant:
    """One exact typed scope/ownership authorization."""

    scope: ScopeKey
    ownership: ClaimOwnership

    def __post_init__(self) -> None:
        if not isinstance(self.scope, ScopeKey):
            raise InvalidRequestError("visibility grant scope must be a ScopeKey")
        if self.ownership not in {ClaimOwnership.COMPANY, ClaimOwnership.CUSTOMER}:
            raise InvalidRequestError("visibility grant ownership must be COMPANY or CUSTOMER")


class ExactScopeVisibilityAuthority:
    """Configured allow-list that compares complete ScopeKey values by equality."""

    def __init__(self, authority_id: str, policy_version: str, grants: tuple[VisibilityGrant, ...]) -> None:
        self.authority_id = _token(authority_id, "visibility authority_id")
        self.policy_version = _token(policy_version, "visibility policy_version")
        if not isinstance(grants, tuple) or not all(isinstance(grant, VisibilityGrant) for grant in grants):
            raise InvalidRequestError("visibility grants must be a tuple of VisibilityGrant values")
        if len(grants) > MAX_VISIBILITY_GRANTS:
            raise InvalidRequestError(f"visibility grants exceeds the limit of {MAX_VISIBILITY_GRANTS}")
        keys = tuple((grant.scope, grant.ownership) for grant in grants)
        if len(keys) != len(set(keys)):
            raise InvalidRequestError("visibility grants must be unique")
        self._grants = frozenset(keys)

    def evaluate(self, scope: ScopeKey, ownership: ClaimOwnership) -> VisibilityAuthorization:
        if not isinstance(scope, ScopeKey):
            raise InvalidRequestError("visibility evaluation scope must be a ScopeKey")
        if ownership not in {ClaimOwnership.COMPANY, ClaimOwnership.CUSTOMER}:
            raise InvalidRequestError("visibility evaluation ownership must be COMPANY or CUSTOMER")
        allowed = (scope, ownership) in self._grants
        return VisibilityAuthorization(
            allowed=allowed,
            scope=scope,
            ownership=ownership,
            authority_id=self.authority_id,
            policy_version=self.policy_version,
            reason_code="exact_scope_granted" if allowed else "exact_scope_denied",
        )


@dataclass(frozen=True, slots=True)
class ClaimEligibilityDecision:
    """One fail-closed Claim projection eligibility decision."""

    projection: ClaimProjection
    eligible: bool
    reason: ClaimEligibilityReason
    disclosure: DisclosureDecision = EMPTY_DISCLOSURE_DECISION
    disclosure_available: bool = False
    revalidated: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.projection, ClaimProjection):
            raise InvalidRequestError("Claim eligibility projection must be a ClaimProjection")
        if not isinstance(self.eligible, bool):
            raise InvalidRequestError("Claim eligibility eligible must be a boolean")
        if not isinstance(self.reason, ClaimEligibilityReason):
            raise InvalidRequestError("Claim eligibility reason must be a ClaimEligibilityReason")
        if not isinstance(self.disclosure, DisclosureDecision):
            raise InvalidRequestError("Claim eligibility disclosure must be a DisclosureDecision")
        if not isinstance(self.disclosure_available, bool):
            raise InvalidRequestError("Claim eligibility disclosure_available must be a boolean")
        if not isinstance(self.revalidated, bool):
            raise InvalidRequestError("Claim eligibility revalidated must be a boolean")
        if self.eligible != self.disclosure_available:
            raise InvalidRequestError("eligible Claim decisions require an available disclosure decision")
        if not self.disclosure_available and self.disclosure != EMPTY_DISCLOSURE_DECISION:
            raise InvalidRequestError("unavailable Claim disclosure must use the concrete empty decision")
        eligible_reasons = {
            ClaimEligibilityReason.ELIGIBLE_PUBLIC,
            ClaimEligibilityReason.ELIGIBLE_TRUSTED_SCOPE,
        }
        if self.eligible != (self.reason in eligible_reasons):
            raise InvalidRequestError("Claim eligibility reason conflicts with eligible state")


class ClaimEligibilityEvaluator:
    """Current-only disclosure policy over strict Claim projections."""

    def __init__(self, visibility_authority: object = ()) -> None:
        if type(visibility_authority) is tuple and not visibility_authority:
            selected_authority = ()
        elif callable(getattr(visibility_authority, "evaluate", ())):
            selected_authority = visibility_authority
        else:
            raise InvalidRequestError("visibility authority must implement evaluate")
        self._visibility_authority = selected_authority

    @staticmethod
    def _decision(
        projection: ClaimProjection,
        reason: ClaimEligibilityReason,
        *,
        revalidated: bool = False,
    ) -> ClaimEligibilityDecision:
        return ClaimEligibilityDecision(projection, False, reason, revalidated=revalidated)

    def evaluate(self, projection: ClaimProjection, frame: QueryFrame) -> ClaimEligibilityDecision:
        if not isinstance(projection, ClaimProjection) or not isinstance(frame, QueryFrame):
            raise InvalidRequestError("Claim eligibility requires a ClaimProjection and QueryFrame")
        context = frame.eligibility_context
        if not context.evaluation_time_available:
            return self._decision(projection, ClaimEligibilityReason.EVALUATION_TIME_UNAVAILABLE)
        evaluation_time = _timestamp(context.evaluation_time)
        if projection.invalidated_at_available:
            return self._decision(projection, ClaimEligibilityReason.CLAIM_INACTIVE)
        if not projection.system_from_available:
            return self._decision(projection, ClaimEligibilityReason.SYSTEM_TIME_UNAVAILABLE)
        if evaluation_time < _timestamp(projection.system_from):
            return self._decision(projection, ClaimEligibilityReason.SYSTEM_NOT_YET_CURRENT)
        if projection.system_to_available and evaluation_time >= _timestamp(projection.system_to):
            return self._decision(projection, ClaimEligibilityReason.SYSTEM_NO_LONGER_CURRENT)
        if projection.valid_from_available and evaluation_time < _timestamp(projection.valid_from):
            return self._decision(projection, ClaimEligibilityReason.VALID_TIME_NOT_YET_CURRENT)
        if projection.valid_to_available and evaluation_time >= _timestamp(projection.valid_to):
            return self._decision(projection, ClaimEligibilityReason.VALID_TIME_NO_LONGER_CURRENT)
        if not projection.predicate_canonical or projection.predicate_id == "generic_relation":
            return self._decision(projection, ClaimEligibilityReason.RETRIEVAL_ONLY)

        ownership = ClaimOwnership(projection.ownership_category)
        if ownership == ClaimOwnership.PUBLIC:
            disclosure = DisclosureDecision(
                ownership=ownership,
                basis=DisclosureBasis.PUBLIC_RULE,
                scope=frame.scope,
                policy_version=CLAIM_DISCLOSURE_POLICY_VERSION,
            )
            return ClaimEligibilityDecision(
                projection,
                True,
                ClaimEligibilityReason.ELIGIBLE_PUBLIC,
                disclosure,
                True,
            )

        authority_method = getattr(self._visibility_authority, "evaluate", ())
        if not callable(authority_method):
            return self._decision(projection, ClaimEligibilityReason.VISIBILITY_AUTHORITY_UNAVAILABLE)
        try:
            authorization = authority_method(frame.scope, ownership)
        except Exception:
            return self._decision(projection, ClaimEligibilityReason.VISIBILITY_AUTHORITY_FAILED)
        if not isinstance(authorization, VisibilityAuthorization):
            return self._decision(projection, ClaimEligibilityReason.VISIBILITY_AUTHORITY_FAILED)
        if authorization.scope != frame.scope:
            return self._decision(projection, ClaimEligibilityReason.VISIBILITY_SCOPE_MISMATCH)
        if authorization.ownership != ownership:
            return self._decision(projection, ClaimEligibilityReason.VISIBILITY_OWNERSHIP_MISMATCH)
        if not authorization.allowed:
            return self._decision(projection, ClaimEligibilityReason.VISIBILITY_DENIED)
        disclosure = DisclosureDecision(
            ownership=ownership,
            basis=DisclosureBasis.TRUSTED_SCOPE_AUTHORITY,
            scope=frame.scope,
            policy_version=authorization.policy_version,
            authority=authorization.authority_id,
            authority_available=True,
        )
        return ClaimEligibilityDecision(
            projection,
            True,
            ClaimEligibilityReason.ELIGIBLE_TRUSTED_SCOPE,
            disclosure,
            True,
        )

    def revalidate(
        self,
        discovered: ClaimProjection,
        frame: QueryFrame,
        reader: ProjectionRevalidator,
    ) -> ClaimEligibilityDecision:
        if not callable(getattr(reader, "current_claim_projection", ())):
            return self._decision(discovered, ClaimEligibilityReason.REVALIDATION_UNAVAILABLE)
        try:
            current = reader.current_claim_projection(discovered.claim_id)
        except Exception:
            return self._decision(discovered, ClaimEligibilityReason.REVALIDATION_UNAVAILABLE)
        if not isinstance(current, tuple) or len(current) != 1 or not isinstance(current[0], ClaimProjection):
            return self._decision(discovered, ClaimEligibilityReason.REVALIDATION_MISSING)
        projection = current[0]
        if projection.projection_id != ClaimProjectionQuery.BY_ID_V1:
            return self._decision(projection, ClaimEligibilityReason.REVALIDATION_IDENTITY_CONFLICT, revalidated=True)
        discovered_identity = (
            discovered.claim_id,
            discovered.subject_entity_id,
            discovered.predicate_id,
            discovered.object_entity_id,
        )
        current_identity = (
            projection.claim_id,
            projection.subject_entity_id,
            projection.predicate_id,
            projection.object_entity_id,
        )
        if discovered_identity != current_identity:
            return self._decision(projection, ClaimEligibilityReason.REVALIDATION_IDENTITY_CONFLICT, revalidated=True)
        return replace(self.evaluate(projection, frame), revalidated=True)

    @staticmethod
    def validity_inputs(decision: ClaimEligibilityDecision, frame: QueryFrame) -> ClaimValidityInputs:
        if not decision.eligible or not decision.revalidated or not decision.disclosure_available:
            raise InvalidRequestError("Claim validity inputs require an eligible revalidated decision")
        projection = decision.projection
        return ClaimValidityInputs(
            evaluation_time=frame.eligibility_context.evaluation_time,
            active=True,
            system_current=True,
            valid_time_current=True,
            valid_from=projection.valid_from,
            valid_from_available=projection.valid_from_available,
            valid_to=projection.valid_to,
            valid_to_available=projection.valid_to_available,
        )


def revalidate_claims(
    projections: tuple[ClaimProjection, ...],
    frame: QueryFrame,
    evaluator: ClaimEligibilityEvaluator,
    reader: ProjectionRevalidator,
    cooperative_check: Callable[[], object] = _no_cooperative_check,
) -> tuple[ClaimEligibilityDecision, ...]:
    """Revalidate a bounded projection batch immediately before package construction."""
    if not isinstance(projections, tuple) or len(projections) > 1_000:
        raise InvalidRequestError("Claim revalidation projections must be a tuple of at most 1000 values")
    if not all(isinstance(projection, ClaimProjection) for projection in projections):
        raise InvalidRequestError("Claim revalidation projections must contain ClaimProjection values")
    if not isinstance(evaluator, ClaimEligibilityEvaluator):
        raise InvalidRequestError("Claim revalidation evaluator must be ClaimEligibilityEvaluator")
    if not callable(cooperative_check):
        raise InvalidRequestError("Claim revalidation cooperative_check must be callable")
    decisions = []
    for projection in projections:
        cooperative_check()
        decisions.append(evaluator.revalidate(projection, frame, reader))
    cooperative_check()
    return tuple(decisions)


def claim_evidence_record(
    discovered: ClaimProjection,
    decision: ClaimEligibilityDecision,
    frame: QueryFrame,
    source_resolver: str,
) -> ClaimEvidenceRecord:
    """Construct one strict full record only from eligible revalidated state."""
    source = _token(source_resolver, "Claim evidence source_resolver", 96)
    if source not in CLAIM_EVIDENCE_PRODUCERS:
        raise InvalidRequestError("Claim evidence source_resolver is not an allowed producer")
    if source == "structured_graph" and discovered.projection_id not in {
        ClaimProjectionQuery.STRUCTURED_ENTITY_V1,
        ClaimProjectionQuery.STRUCTURED_KEYWORD_V1,
    }:
        raise InvalidRequestError("structured Claim evidence requires a structured discovery projection")
    if source == "support_semantic" and discovered.projection_id != ClaimProjectionQuery.VECTOR_V1:
        raise InvalidRequestError("semantic Claim evidence requires a vector discovery projection")
    if not decision.eligible or not decision.revalidated or not decision.disclosure_available:
        raise InvalidRequestError("Claim evidence construction requires an eligible revalidated decision")
    current = decision.projection
    discovered_identity = (
        discovered.claim_id,
        discovered.subject_entity_id,
        discovered.predicate_id,
        discovered.object_entity_id,
    )
    current_identity = (
        current.claim_id,
        current.subject_entity_id,
        current.predicate_id,
        current.object_entity_id,
    )
    if discovered_identity != current_identity:
        raise InvalidRequestError("Claim evidence discovery and current canonical identity conflict")
    values = {"canonical_completeness": 1.0}
    unavailable = ["source_agreement"]
    reasons = [decision.reason.value, "canonical_complete"]
    if discovered.structured_match_available:
        values["structured_match"] = discovered.structured_match
        reasons.append("structured_match")
    else:
        unavailable.append("structured_match")
    if discovered.semantic_similarity_available:
        values["semantic_similarity"] = discovered.semantic_similarity
        reasons.append("semantic_similarity")
    else:
        unavailable.append("semantic_similarity")
    if current.supplied_trust_available:
        values["supplied_trust"] = current.supplied_trust
        reasons.append("supplied_trust_available")
    else:
        unavailable.append("supplied_trust")
        reasons.append("supplied_trust_unavailable")
    return ClaimEvidenceRecord(
        claim_id=current.claim_id,
        source_resolver=source,
        source_contributions=(source,),
        features=FeatureSet(values=values, unavailable=tuple(sorted(unavailable))),
        canonical_references=CanonicalClaimReferences(
            current.subject_entity_id,
            current.predicate_id,
            current.object_entity_id,
        ),
        validity=ClaimEligibilityEvaluator.validity_inputs(decision, frame),
        trust=ClaimTrustInputs(
            trust_category=current.trust_category,
            trust_category_available=current.trust_category_available,
            supplied_trust=current.supplied_trust,
            supplied_trust_available=current.supplied_trust_available,
            supplied_trust_version=current.supplied_trust_version,
            supplied_trust_version_available=current.supplied_trust_version_available,
        ),
        disclosure=decision.disclosure,
        path=(current.claim_id,),
        selection_reasons=tuple(sorted(reasons)),
    )


def _merge_claim_evidence_group(records: tuple[ClaimEvidenceRecord, ...]) -> ClaimEvidenceRecord:
    if not records:
        raise InvalidRequestError("cannot merge an empty Claim evidence group")
    ordered = tuple(sorted(records, key=lambda record: (record.source_resolver, record.to_json())))
    base = ordered[0]
    for record in ordered[1:]:
        if record.canonical_references != base.canonical_references:
            raise InvalidRequestError(f"conflicting canonical references for Claim evidence ID: {base.claim_id}")
        current_state = (record.validity, record.trust, record.disclosure, record.path)
        base_state = (base.validity, base.trust, base.disclosure, base.path)
        if current_state != base_state:
            raise InvalidRequestError(f"conflicting current evidence state for Claim evidence ID: {base.claim_id}")

    sources = tuple(sorted({source for record in ordered for source in record.source_contributions}))
    if not sources:
        raise InvalidRequestError("merged Claim evidence sources must not be empty")
    if len(sources) > MAX_CLAIM_SOURCE_CONTRIBUTIONS:
        raise InvalidRequestError(f"merged Claim evidence sources exceed the limit of {MAX_CLAIM_SOURCE_CONTRIBUTIONS}")
    primary_source = next(iter(sources))
    values: dict[str, float] = {}
    unavailable = set()
    reasons = set()
    for record in ordered:
        reasons.update(record.selection_reasons)
        unavailable.update(record.features.unavailable)
        for name, value in record.features.values.items():
            if name in values and values[name] != value:
                raise InvalidRequestError(f"conflicting measured feature {name} for Claim evidence ID: {base.claim_id}")
            values[name] = value
    if len(sources) > 1:
        if "source_agreement" in values and values["source_agreement"] != 1.0:
            raise InvalidRequestError(f"conflicting measured feature source_agreement for Claim evidence ID: {base.claim_id}")
        values["source_agreement"] = 1.0
        reasons.add("source_agreement")
    elif "source_agreement" in values:
        raise InvalidRequestError(f"source_agreement requires multiple sources for Claim evidence ID: {base.claim_id}")
    unavailable.difference_update(values)
    if len(reasons) > MAX_CLAIM_SELECTION_REASONS:
        raise InvalidRequestError(f"merged Claim evidence reasons exceed the limit of {MAX_CLAIM_SELECTION_REASONS}")
    return replace(
        base,
        source_resolver=primary_source,
        source_contributions=sources,
        features=FeatureSet(values=values, unavailable=tuple(sorted(unavailable))),
        selection_reasons=tuple(sorted(reasons)),
    )


def canonicalize_claim_evidence(
    records: tuple[ClaimEvidenceRecord, ...],
    cooperative_check: Callable[[], object] = _no_cooperative_check,
) -> tuple[ClaimEvidenceRecord, ...]:
    """Deterministically deduplicate and merge strict records by stable Claim ID."""
    if not isinstance(records, tuple) or len(records) > MAX_RESOLUTION_VALUES:
        raise InvalidRequestError(f"Claim evidence normalization requires a tuple of at most {MAX_RESOLUTION_VALUES} records")
    if not all(isinstance(record, ClaimEvidenceRecord) for record in records):
        raise InvalidRequestError("Claim evidence normalization requires ClaimEvidenceRecord values")
    if not callable(cooperative_check):
        raise InvalidRequestError("Claim evidence normalization cooperative_check must be callable")
    grouped: dict[str, list[ClaimEvidenceRecord]] = {}
    for record in records:
        cooperative_check()
        grouped.setdefault(record.claim_id, []).append(record)
    merged = []
    for claim_id in sorted(grouped):
        cooperative_check()
        merged.append(_merge_claim_evidence_group(tuple(grouped[claim_id])))
    cooperative_check()
    return tuple(merged)
