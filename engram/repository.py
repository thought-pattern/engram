"""Authoritative live accepted-response artifact repository."""

from contextlib import contextmanager
from datetime import datetime
from threading import RLock as threading_RLock

from engram.artifacts import lifecycle_after_capacity_eviction, validate_cached_response_artifact
from engram.constants import (
    ADMISSION_PLAN_FIELDS,
    MAX_REPOSITORY_ARTIFACTS,
    REPOSITORY_STATE_FIELDS,
    TIER_ADMISSION_POLICY_FIELDS,
    AdmissionOutcome,
    LifecycleState,
    RepositoryRemovalReason,
    Tier,
)
from engram.eligibility import ContextualExactLookup
from engram.errors import ConflictError, InvalidRequestError, ResourceNotFoundError


def tier_admission_policy(dynamic_capacity: int) -> dict:
    """Build one validated tier-admission policy dictionary."""
    capacity = positive_int(dynamic_capacity, "dynamic capacity")
    result: dict = {
        "dynamic_capacity": capacity,
    }
    return result


def validate_tier_admission_policy(value: object) -> dict:
    """Validate and copy one tier-admission policy dictionary."""
    if not isinstance(value, dict) or set(value) != TIER_ADMISSION_POLICY_FIELDS:
        raise InvalidRequestError("admission policy must be a TierAdmissionPolicy")
    dynamic_capacity = value.get("dynamic_capacity", ())
    if not isinstance(dynamic_capacity, int):
        raise InvalidRequestError("admission policy must be a TierAdmissionPolicy")
    result = tier_admission_policy(dynamic_capacity)
    return result


def positive_int(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise InvalidRequestError(f"{name} must be a positive integer")
    return value


def artifact_time(value: str) -> datetime:
    result = datetime.fromisoformat(value[:-1] + "+00:00")
    return result


def eviction_order_key(artifact: dict) -> tuple[datetime, int, str]:
    statistics = artifact.get("statistics", {})
    provenance = artifact.get("provenance", {})
    accepted_at = artifact_time(provenance.get("accepted_at", ""))
    last_hit_available = bool(statistics.get("last_hit_available", False))
    last_used = artifact_time(statistics.get("last_hit", "")) if last_hit_available else accepted_at
    result = (last_used, 1 if last_hit_available else 0, artifact.get("statement_id", ""))
    return result


def repository_state(
    state_generation: int,
    artifacts: object,
) -> dict:
    """Build one validated repository snapshot dictionary."""

    generation = positive_int(state_generation, "repository state_generation")
    if not isinstance(artifacts, dict):
        raise InvalidRequestError("repository artifacts must be an object")
    validated_artifacts = {}
    for statement_id, artifact in artifacts.items():
        if not isinstance(statement_id, str) or not statement_id:
            raise InvalidRequestError("repository artifacts must map statement IDs to CachedResponseArtifact values")
        try:
            validated_artifact = validate_cached_response_artifact(artifact)
        except InvalidRequestError as error:
            raise InvalidRequestError("repository artifacts must map statement IDs to CachedResponseArtifact values") from error
        if validated_artifact.get("statement_id", "") != statement_id:
            raise InvalidRequestError("repository artifacts must map statement IDs to CachedResponseArtifact values")
        validated_artifacts[statement_id] = validated_artifact
    frozen_artifacts = dict(validated_artifacts)
    result: dict = {
        "state_generation": generation,
        "artifacts": frozen_artifacts,
    }
    return result


def validate_repository_state(value: object) -> dict:
    """Validate and copy one repository snapshot dictionary."""

    if not isinstance(value, dict) or set(value) != REPOSITORY_STATE_FIELDS:
        raise InvalidRequestError("repository state must be a RepositoryState")
    state_generation = value.get("state_generation", ())
    artifacts = value.get("artifacts", ())
    if not isinstance(state_generation, int):
        raise InvalidRequestError("repository state must be a RepositoryState")
    result = repository_state(state_generation, artifacts)
    return result


def admission_plan(
    outcome: AdmissionOutcome,
    candidate: dict,
    admitted_statement_id: str,
    evicted_statement_ids: tuple[str, ...],
    residency_changed: bool,
    lifecycle_changed: bool,
) -> dict:
    """Build one validated off-live tier-admission result dictionary."""

    if not isinstance(outcome, AdmissionOutcome):
        raise InvalidRequestError("admission outcome must be an AdmissionOutcome")
    try:
        validated_candidate = validate_repository_state(candidate)
    except InvalidRequestError as error:
        raise InvalidRequestError("admission candidate must be a RepositoryState") from error
    if not isinstance(admitted_statement_id, str):
        raise InvalidRequestError("admitted_statement_id must be a string")
    valid_evicted_ids = isinstance(evicted_statement_ids, tuple) and all(
        isinstance(statement_id, str) and statement_id for statement_id in evicted_statement_ids
    )
    if not valid_evicted_ids:
        raise InvalidRequestError("evicted_statement_ids must be a tuple of non-empty strings")
    if not isinstance(residency_changed, bool) or not isinstance(lifecycle_changed, bool):
        raise InvalidRequestError("admission change flags must be booleans")
    if lifecycle_changed:
        raise InvalidRequestError("tier admission must not change lifecycle")
    rejected = outcome == AdmissionOutcome.REJECTED_CAPACITY
    if rejected == residency_changed:
        raise InvalidRequestError("admission outcome must agree with residency_changed")
    state_changes_reported = admitted_statement_id or evicted_statement_ids
    if rejected and state_changes_reported:
        raise InvalidRequestError("rejected admission must not report state changes")
    result: dict = {
        "outcome": outcome,
        "candidate": validated_candidate,
        "admitted_statement_id": admitted_statement_id,
        "evicted_statement_ids": evicted_statement_ids,
        "residency_changed": residency_changed,
        "lifecycle_changed": lifecycle_changed,
    }
    return result


def validate_admission_plan(value: object) -> dict:
    """Validate and copy one tier-admission result dictionary."""

    if not isinstance(value, dict) or set(value) != ADMISSION_PLAN_FIELDS:
        raise InvalidRequestError("admission plan must be an AdmissionPlan")
    outcome = value.get("outcome", ())
    candidate = value.get("candidate", ())
    admitted_statement_id = value.get("admitted_statement_id", ())
    evicted_statement_ids = value.get("evicted_statement_ids", ())
    residency_changed = value.get("residency_changed", ())
    lifecycle_changed = value.get("lifecycle_changed", ())
    valid_types = (
        isinstance(outcome, AdmissionOutcome)
        and isinstance(admitted_statement_id, str)
        and isinstance(evicted_statement_ids, tuple)
        and isinstance(residency_changed, bool)
        and isinstance(lifecycle_changed, bool)
    )
    if not valid_types:
        raise InvalidRequestError("admission plan must be an AdmissionPlan")
    result = admission_plan(
        outcome,
        candidate,
        admitted_statement_id,
        evicted_statement_ids,
        residency_changed,
        lifecycle_changed,
    )
    return result


def admission_plan_to_dict(value: object) -> dict:
    """Serialize one validated admission plan without its candidate snapshot."""

    validated = validate_admission_plan(value)
    candidate = validated.get("candidate", {})
    outcome = validated.get("outcome", AdmissionOutcome.REJECTED_CAPACITY)
    result = {
        "outcome": outcome.value,
        "candidate_state_generation": candidate.get("state_generation", 0),
        "admitted_statement_id": validated.get("admitted_statement_id", ""),
        "evicted_statement_ids": list(validated.get("evicted_statement_ids", ())),
        "residency_changed": validated.get("residency_changed", False),
        "lifecycle_changed": validated.get("lifecycle_changed", False),
    }
    return result


def normalize_repository_state(
    artifacts: object,
    state_generation: int = 1,
) -> dict:
    """Validate and normalize a complete candidate without changing live state."""

    generation = positive_int(state_generation, "repository state_generation")
    if not isinstance(artifacts, tuple):
        raise InvalidRequestError("repository artifact input must be a tuple of CachedResponseArtifact values")
    artifact_map = {}
    for artifact in artifacts:
        try:
            validated_artifact = validate_cached_response_artifact(artifact)
        except InvalidRequestError as error:
            raise InvalidRequestError("repository artifact input must contain CachedResponseArtifact values") from error
        statement_id = validated_artifact.get("statement_id", "")
        if statement_id in artifact_map:
            raise ConflictError(f"duplicate artifact statement_id: {statement_id}")
        artifact_map[statement_id] = validated_artifact
        if len(artifact_map) > MAX_REPOSITORY_ARTIFACTS:
            raise InvalidRequestError(f"repository artifacts exceed the limit of {MAX_REPOSITORY_ARTIFACTS}")
    state = repository_state(
        state_generation=generation,
        artifacts=artifact_map,
    )
    return state


def repository_state_with_artifact_updates(
    state: dict,
    artifacts: tuple[dict, ...],
) -> dict:
    """Rebuild one off-live repository state after same-transaction updates."""

    validated_state = validate_repository_state(state)
    if not isinstance(artifacts, tuple):
        raise InvalidRequestError("repository updates must be a tuple of CachedResponseArtifact values")
    try:
        validated_artifacts = tuple(validate_cached_response_artifact(artifact) for artifact in artifacts)
    except InvalidRequestError as error:
        raise InvalidRequestError("repository updates must be a tuple of CachedResponseArtifact values") from error
    updated = dict(validated_state.get("artifacts", {}))
    for artifact in validated_artifacts:
        statement_id = artifact.get("statement_id", "")
        if statement_id not in updated:
            raise ConflictError(f"repository update artifact does not exist in candidate: {statement_id}")
        updated[statement_id] = artifact
    result = normalize_repository_state(
        tuple(updated.values()),
        validated_state.get("state_generation", 0),
    )
    return result


class ArtifactRepository:
    """Atomic owner of authoritative accepted-response artifacts."""

    def __init__(self, artifacts: tuple[dict, ...] = ()) -> None:
        self.internal_lock = threading_RLock()
        self.internal_state = normalize_repository_state(artifacts)

    def snapshot(self) -> dict:
        with self.internal_lock:
            snapshot = validate_repository_state(self.internal_state)
            return snapshot

    @contextmanager
    def coordinated_mutation(self):
        """Hold the repository boundary across candidate publication."""

        with self.internal_lock:
            yield

    def get_artifact(self, statement_id: str) -> dict:
        if not isinstance(statement_id, str) or not statement_id:
            raise InvalidRequestError("repository statement_id must be a non-empty string")
        with self.internal_lock:
            artifacts = self.internal_state.get("artifacts", {})
            if statement_id not in artifacts:
                raise ResourceNotFoundError(f"accepted response artifact not found: {statement_id}")
            artifact = validate_cached_response_artifact(artifacts.get(statement_id, {}))
            return artifact

    def candidate_with_artifact(self, artifact: dict) -> dict:
        try:
            artifact = validate_cached_response_artifact(artifact)
        except InvalidRequestError as error:
            raise InvalidRequestError("repository artifact must be a CachedResponseArtifact") from error
        with self.internal_lock:
            artifacts = dict(self.internal_state.get("artifacts", {}))
            artifacts[artifact.get("statement_id", "")] = artifact
            candidate = normalize_repository_state(
                tuple(artifacts.values()),
                self.internal_state.get("state_generation", 0) + 1,
            )
            return candidate

    def candidate_with_artifacts(self, artifacts: tuple[dict, ...]) -> dict:
        """Build one next-generation candidate containing multiple artifact updates."""

        if not isinstance(artifacts, tuple):
            raise InvalidRequestError("repository artifacts must be a tuple of CachedResponseArtifact values")
        try:
            validated_artifacts = tuple(validate_cached_response_artifact(artifact) for artifact in artifacts)
        except InvalidRequestError as error:
            raise InvalidRequestError("repository artifacts must be a tuple of CachedResponseArtifact values") from error
        if len({artifact.get("statement_id", "") for artifact in validated_artifacts}) != len(validated_artifacts):
            raise ConflictError("repository artifact updates must have unique statement IDs")
        with self.internal_lock:
            updated = dict(self.internal_state.get("artifacts", {}))
            for artifact in validated_artifacts:
                statement_id = artifact.get("statement_id", "")
                if statement_id not in updated:
                    raise ResourceNotFoundError(f"accepted response artifact not found: {statement_id}")
                updated[statement_id] = artifact
            candidate = normalize_repository_state(
                tuple(updated.values()),
                self.internal_state.get("state_generation", 0) + 1,
            )
            return candidate

    def plan_admission(self, artifact: dict, policy: dict) -> dict:
        """Plan bounded tier admission without publishing any live mutation."""

        try:
            artifact = validate_cached_response_artifact(artifact)
        except InvalidRequestError as error:
            raise InvalidRequestError("admission artifact must be a CachedResponseArtifact") from error
        validated_policy = validate_tier_admission_policy(policy)
        with self.internal_lock:
            if artifact.get("statement_id", "") in self.internal_state.get("artifacts", {}):
                raise ConflictError(f"artifact statement_id already exists: {artifact.get('statement_id', "")}")
            artifacts = dict(self.internal_state.get("artifacts", {}))
            if artifact.get("tier", Tier.STATIC) == Tier.STATIC:
                artifacts[artifact.get("statement_id", "")] = artifact
                candidate = normalize_repository_state(
                    tuple(artifacts.values()),
                    self.internal_state.get("state_generation", 0) + 1,
                )
                plan = admission_plan(
                    outcome=AdmissionOutcome.ADMITTED,
                    candidate=candidate,
                    admitted_statement_id=artifact.get("statement_id", ""),
                    evicted_statement_ids=(),
                    residency_changed=True,
                    lifecycle_changed=False,
                )
                return plan

            dynamics = tuple(existing for existing in artifacts.values() if existing.get("tier", Tier.STATIC) == Tier.DYNAMIC)
            required_evictions = max(0, len(dynamics) - validated_policy.get("dynamic_capacity", 0) + 1)
            victims = tuple(
                sorted(
                    dynamics,
                    key=eviction_order_key,
                )[:required_evictions]
            )
            for victim in victims:
                lifecycle_after_capacity_eviction(victim.get("lifecycle", LifecycleState.ACTIVE))
                victim_id = victim.get("statement_id", "")
                del artifacts[victim_id]
            artifacts[artifact.get("statement_id", "")] = artifact
            candidate = normalize_repository_state(
                tuple(artifacts.values()),
                self.internal_state.get("state_generation", 0) + 1,
            )
            outcome = AdmissionOutcome.ADMITTED_WITH_EVICTION if victims else AdmissionOutcome.ADMITTED
            plan = admission_plan(
                outcome=outcome,
                candidate=candidate,
                admitted_statement_id=artifact.get("statement_id", ""),
                evicted_statement_ids=tuple(victim.get("statement_id", "") for victim in victims),
                residency_changed=True,
                lifecycle_changed=False,
            )
            return plan

    def candidate_without_artifact(
        self,
        statement_id: str,
        expected_generation: int,
        reason: object,
    ) -> dict:
        if not isinstance(statement_id, str) or not statement_id:
            raise InvalidRequestError("repository statement_id must be a non-empty string")
        expected = positive_int(expected_generation, "expected artifact generation")
        if not isinstance(reason, RepositoryRemovalReason):
            raise InvalidRequestError("repository removal reason must be a RepositoryRemovalReason")
        with self.internal_lock:
            current_artifacts = self.internal_state.get("artifacts", {})
            if statement_id not in current_artifacts:
                raise ResourceNotFoundError(f"accepted response artifact not found: {statement_id}")
            artifact = current_artifacts.get(statement_id, {})
            if artifact.get("generation", 0) != expected:
                raise ConflictError(
                    f"artifact generation conflict for {statement_id}: expected {expected}, current {artifact.get('generation', 0)}"
                )
            if reason == RepositoryRemovalReason.CAPACITY_EVICTION:
                if artifact.get("tier", Tier.STATIC) != Tier.DYNAMIC:
                    raise ConflictError(f"capacity eviction cannot remove STATIC artifact: {statement_id}")
                lifecycle_after_capacity_eviction(artifact.get("lifecycle", LifecycleState.ACTIVE))
            artifacts = dict(current_artifacts)
            del artifacts[statement_id]
            candidate = normalize_repository_state(
                tuple(artifacts.values()),
                self.internal_state.get("state_generation", 0) + 1,
            )
            return candidate

    def atomic_replace(self, candidate: dict, expected_state_generation: int) -> dict:
        validated_candidate = validate_repository_state(candidate)
        expected = positive_int(expected_state_generation, "expected repository state_generation")
        with self.internal_lock:
            current_generation = self.internal_state.get("state_generation", 0)
            if current_generation != expected:
                raise ConflictError(f"repository state generation conflict: expected {expected}, current {current_generation}")
            if validated_candidate.get("state_generation", 0) != expected + 1:
                raise ConflictError("candidate repository state_generation must advance by exactly one")
            self.internal_state = repository_state(
                state_generation=validated_candidate.get("state_generation", 0),
                artifacts=validated_candidate.get("artifacts", {}),
            )
            published = validate_repository_state(self.internal_state)
            return published

    def restore_state(self, state: dict) -> dict:
        """Restore one previously validated in-process repository state."""

        validated_state = validate_repository_state(state)
        with self.internal_lock:
            self.internal_state = repository_state(
                state_generation=validated_state.get("state_generation", 0),
                artifacts=validated_state.get("artifacts", {}),
            )
            recovered = validate_repository_state(self.internal_state)
            return recovered

    def exact_lookup(
        self,
        key: dict,
        context: dict,
    ) -> dict:
        with self.internal_lock:
            lookup = ContextualExactLookup(
                self.internal_state.get("artifacts", {}),
                trusted_artifacts=True,
            ).exact_lookup(key, context)
            result = lookup
            return result
