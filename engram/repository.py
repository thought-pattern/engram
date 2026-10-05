"""Authoritative live accepted-response artifact repository."""

from contextlib import contextmanager
from datetime import datetime
from heapq import nsmallest as heapq_nsmallest
from threading import RLock as threading_RLock

from engram.artifacts import lifecycle_after_capacity_eviction, validate_cached_response_artifact
from engram.constants import (
    MAX_REPOSITORY_ARTIFACTS,
    REPOSITORY_STATE_FIELDS,
    TIER_ADMISSION_POLICY_FIELDS,
    AdmissionOutcome,
    LifecycleState,
    RepositoryRemovalReason,
    Tier,
)
from engram.copies import structural_copy
from engram.eligibility import ContextualExactLookup
from engram.errors import ConflictError, IdentityValidationError, InvalidRequestError, ResourceNotFoundError
from engram.identity import (
    retrieval_representation_bindings,
    trusted_scoped_retrieval_key_signature,
    validate_scoped_retrieval_key,
)
from engram.validation import utc_datetime

MAX_PLANNED_CANDIDATES = 16


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


def eviction_order_key(artifact: dict) -> tuple[datetime, int, str]:
    statistics = artifact.get("statistics", {})
    provenance = artifact.get("provenance", {})
    accepted_at = utc_datetime(provenance.get("accepted_at", ""))
    last_hit_available = bool(statistics.get("last_hit_available", False))
    last_used = utc_datetime(statistics.get("last_hit", "")) if last_hit_available else accepted_at
    result = (last_used, 1 if last_hit_available else 0, artifact.get("statement_id", ""))
    return result


def trusted_or_validated_artifact(artifact: object, trusted: dict) -> dict:
    """Return the live artifact when ``artifact`` is it or equals it, else validate.

    Live artifacts were validated when they entered the repository and are
    never changed in place, so an equal value needs no second validation.
    Returning the live object keeps the stored form canonical. ``trusted`` is
    {} when no live artifact exists; a validated artifact is never empty.
    """
    if trusted and (artifact is trusted or artifact == trusted):
        result = trusted
        return result
    result = validate_cached_response_artifact(artifact)
    return result


def repository_state(
    state_generation: int,
    artifacts: object,
    trusted_artifacts: object = (),
) -> dict:
    """Build one validated repository snapshot dictionary.

    ``trusted_artifacts`` maps statement IDs to live, already validated
    artifacts. An entry equal to its live artifact is not validated again.
    """

    generation = positive_int(state_generation, "repository state_generation")
    if not isinstance(artifacts, dict):
        raise InvalidRequestError("repository artifacts must be an object")
    # Every validated state, including a public replacement or restore, holds the
    # process ceiling, refused before any artifact is validated or copied.
    if len(artifacts) > MAX_REPOSITORY_ARTIFACTS:
        raise InvalidRequestError(f"repository artifacts exceed the limit of {MAX_REPOSITORY_ARTIFACTS}")
    trusted = trusted_artifacts if isinstance(trusted_artifacts, dict) else {}
    validated_artifacts = {}
    for statement_id, artifact in artifacts.items():
        if not isinstance(statement_id, str) or not statement_id:
            raise InvalidRequestError("repository artifacts must map statement IDs to CachedResponseArtifact values")
        try:
            validated_artifact = trusted_or_validated_artifact(artifact, trusted.get(statement_id, {}))
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


def validate_repository_state(value: object, trusted_artifacts: object = ()) -> dict:
    """Validate and copy one repository snapshot dictionary.

    Artifacts equal to their entry in ``trusted_artifacts`` are reused.
    """

    if not isinstance(value, dict) or set(value) != REPOSITORY_STATE_FIELDS:
        raise InvalidRequestError("repository state must be a RepositoryState")
    state_generation = value.get("state_generation", ())
    artifacts = value.get("artifacts", ())
    if not isinstance(state_generation, int):
        raise InvalidRequestError("repository state must be a RepositoryState")
    result = repository_state(state_generation, artifacts, trusted_artifacts)
    return result


def trusted_repository_state(state_generation: int, artifacts: dict) -> dict:
    """Assemble a state from live and freshly validated artifacts without revalidating them."""
    if len(artifacts) > MAX_REPOSITORY_ARTIFACTS:
        raise InvalidRequestError(f"repository artifacts exceed the limit of {MAX_REPOSITORY_ARTIFACTS}")
    result: dict = {
        "state_generation": positive_int(state_generation, "repository state_generation"),
        "artifacts": artifacts,
    }
    return result


def admission_plan(
    outcome: AdmissionOutcome,
    candidate: dict,
    admitted_statement_id: str,
    evicted_statement_ids: tuple[str, ...],
    residency_changed: bool,
    lifecycle_changed: bool,
    trusted_artifacts: object = (),
    candidate_trusted: bool = False,
) -> dict:
    """Build one validated off-live tier-admission result dictionary.

    ``candidate_trusted`` skips revalidating a candidate the repository built
    from its live state and a freshly validated artifact.
    """

    if not isinstance(outcome, AdmissionOutcome):
        raise InvalidRequestError("admission outcome must be an AdmissionOutcome")
    if candidate_trusted:
        validated_candidate = candidate
    else:
        try:
            validated_candidate = validate_repository_state(candidate, trusted_artifacts)
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
    """Atomic owner of authoritative accepted-response artifacts.

    Artifacts are validated once, when they enter. The live state dictionary
    is replaced, never changed in place, so package code may read it through
    ``trusted_state`` without copying. Public reads (``snapshot``,
    ``get_artifact``, ``find_artifact``) return structural copies a caller may
    change freely. Candidate states share live artifacts for entries they do
    not change; they go back to ``atomic_replace`` or the coordinator and must
    not be changed in place.
    """

    def __init__(self, artifacts: tuple[dict, ...] = ()) -> None:
        self.internal_lock = threading_RLock()
        self.internal_state = normalize_repository_state(artifacts)
        # Exact-key index over the live state: statement_id -> (artifact, key
        # signatures), and key signature -> statement IDs carrying it.
        self.internal_indexed: dict[str, tuple[dict, tuple[tuple, ...]]] = {}
        self.internal_key_owners: dict[tuple, set[str]] = {}
        # DYNAMIC statement IDs, so admission need not scan STATIC artifacts.
        self.internal_dynamic_ids: set[str] = set()
        # Recent candidates this repository built: id -> (candidate, base state,
        # changed statement IDs). A candidate handed back unchanged needs only
        # its changed entries checked, not every stored artifact.
        self.internal_planned: dict[int, tuple[dict, dict, set[str]]] = {}
        self.index_state(self.internal_state)

    def plan(self, candidate: dict, changed: tuple) -> dict:
        """Remember a candidate built from the live state along with the IDs it changes."""
        with self.internal_lock:
            while len(self.internal_planned) >= MAX_PLANNED_CANDIDATES:
                del self.internal_planned[next(iter(self.internal_planned))]
            self.internal_planned[id(candidate)] = (candidate, self.internal_state, set(changed))
            return candidate

    def planned_changes(self, candidate: object, base: dict) -> dict:
        """Report whether this repository built ``candidate`` from ``base``, and the IDs it changes.

        ``planned`` is False for any other candidate; a planned candidate may
        change no statement, so an empty ``changed`` set does not mean unknown.
        The returned set is a copy of the recorded one.
        """
        with self.internal_lock:
            entry = self.internal_planned.get(id(candidate), ())
            if not entry or entry[0] is not candidate or entry[1] is not base:
                result = {"planned": False, "changed": set()}
                return result
            changed = set(entry[2])
            result = {"planned": True, "changed": changed}
            return result

    def index_state(self, state: dict, changed=(), changed_known: bool = False) -> None:
        """Bring the exact-key index in line with ``state``.

        Artifacts are compared by identity, so only added, replaced, and
        removed artifacts have their retrieval bindings computed. When
        ``changed_known`` is set, ``changed`` (a collection of statement IDs)
        limits the comparison to those IDs; otherwise every artifact is compared.
        """
        artifacts = state.get("artifacts", {})
        candidates = [value for value in changed if value in self.internal_indexed] if changed_known else self.internal_indexed
        stale = []
        for statement_id in candidates:
            # An empty default is never the indexed object, so a missing artifact is stale.
            indexed_artifact = self.internal_indexed.get(statement_id, ({}, ()))[0]
            if artifacts.get(statement_id, {}) is not indexed_artifact:
                stale.append(statement_id)
        for statement_id in stale:
            self.internal_dynamic_ids.discard(statement_id)
            _, signatures = self.internal_indexed.pop(statement_id)
            for signature in signatures:
                # Owner sets are removed once empty, so an absent set has nothing to discard.
                owners = self.internal_key_owners.get(signature, set())
                if owners:
                    owners.discard(statement_id)
                    if not owners:
                        del self.internal_key_owners[signature]
        additions = {value: artifacts.get(value, {}) for value in changed if value in artifacts} if changed_known else artifacts
        for statement_id, artifact in additions.items():
            if statement_id in self.internal_indexed:
                continue
            signatures = tuple(
                trusted_scoped_retrieval_key_signature(binding.get("key", {}))
                for binding in retrieval_representation_bindings(artifact.get("retrieval", {}), artifact.get("scope", {}))
            )
            self.internal_indexed[statement_id] = (artifact, signatures)
            if artifact.get("tier", Tier.STATIC) == Tier.DYNAMIC:
                self.internal_dynamic_ids.add(statement_id)
            for signature in signatures:
                self.internal_key_owners.setdefault(signature, set()).add(statement_id)

    def key_owner_ids(self, keys: tuple) -> tuple[str, ...]:
        """Return the statement IDs whose retrieval bindings include any of ``keys``."""
        with self.internal_lock:
            owners: set[str] = set()
            for key in keys:
                owners.update(self.internal_key_owners.get(trusted_scoped_retrieval_key_signature(key), ()))
            result = tuple(sorted(owners))
            return result

    def trusted_state(self) -> dict:
        """Return the live state for package-internal reads. Do not modify it."""
        with self.internal_lock:
            result = self.internal_state
            return result

    def trusted_artifacts(self) -> dict:
        """Return the live artifact map for package-internal reads. Do not modify it."""
        with self.internal_lock:
            result = self.internal_state.get("artifacts", {})
            return result

    def snapshot(self) -> dict:
        with self.internal_lock:
            state = self.internal_state
        snapshot = {
            "state_generation": state.get("state_generation", 0),
            "artifacts": {statement_id: structural_copy(artifact) for statement_id, artifact in state.get("artifacts", {}).items()},
        }
        return snapshot

    @contextmanager
    def coordinated_mutation(self):
        """Hold the repository boundary across candidate publication."""

        with self.internal_lock:
            yield

    def has_artifact(self, statement_id: str) -> bool:
        """Return whether an artifact exists."""
        with self.internal_lock:
            result = statement_id in self.internal_state.get("artifacts", {})
            return result

    def find_artifact(self, statement_id: str) -> dict:
        """Return a copy of one artifact, or {} when it does not exist."""
        with self.internal_lock:
            artifact = self.internal_state.get("artifacts", {}).get(statement_id, {})
        # Stored artifacts are validated and never empty, so {} means absent.
        if not artifact:
            result: dict = {}
            return result
        copied = structural_copy(artifact)
        if not isinstance(copied, dict):
            raise InvalidRequestError("repository artifact did not remain an object")
        return copied

    def get_artifact(self, statement_id: str) -> dict:
        if not isinstance(statement_id, str) or not statement_id:
            raise InvalidRequestError("repository statement_id must be a non-empty string")
        with self.internal_lock:
            artifacts = self.internal_state.get("artifacts", {})
            if statement_id not in artifacts:
                raise ResourceNotFoundError(f"accepted response artifact not found: {statement_id}")
            artifact = artifacts.get(statement_id, {})
        copied = structural_copy(artifact)
        if not isinstance(copied, dict):
            raise InvalidRequestError("repository artifact did not remain an object")
        return copied

    def candidate_with_artifact(self, artifact: dict) -> dict:
        try:
            artifact = validate_cached_response_artifact(artifact)
        except InvalidRequestError as error:
            raise InvalidRequestError("repository artifact must be a CachedResponseArtifact") from error
        with self.internal_lock:
            artifacts = dict(self.internal_state.get("artifacts", {}))
            artifacts[artifact.get("statement_id", "")] = artifact
            candidate = trusted_repository_state(self.internal_state.get("state_generation", 0) + 1, artifacts)
            result = self.plan(candidate, (artifact.get("statement_id", ""),))
            return result

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
            candidate = trusted_repository_state(self.internal_state.get("state_generation", 0) + 1, updated)
            result = self.plan(candidate, tuple(artifact.get("statement_id", "") for artifact in validated_artifacts))
            return result

    def plan_admission(self, artifact: dict, policy: dict) -> dict:
        """Plan bounded tier admission without publishing any live mutation."""

        try:
            artifact = validate_cached_response_artifact(artifact)
        except InvalidRequestError as error:
            raise InvalidRequestError("admission artifact must be a CachedResponseArtifact") from error
        validated_policy = validate_tier_admission_policy(policy)
        statement_id = artifact.get("statement_id", "")
        with self.internal_lock:
            live_artifacts = self.internal_state.get("artifacts", {})
            if statement_id in live_artifacts:
                raise ConflictError(f"artifact statement_id already exists: {statement_id}")
            artifacts = dict(live_artifacts)
            if artifact.get("tier", Tier.STATIC) == Tier.STATIC:
                artifacts[statement_id] = artifact
                candidate = trusted_repository_state(self.internal_state.get("state_generation", 0) + 1, artifacts)
                plan = admission_plan(
                    outcome=AdmissionOutcome.ADMITTED,
                    candidate=candidate,
                    admitted_statement_id=statement_id,
                    evicted_statement_ids=(),
                    residency_changed=True,
                    lifecycle_changed=False,
                    candidate_trusted=True,
                )
                self.plan(plan.get("candidate", {}), (statement_id,))
                return plan

            dynamic_ids = self.internal_dynamic_ids
            required_evictions = max(0, len(dynamic_ids) - validated_policy.get("dynamic_capacity", 0) + 1)
            # Eviction keys end with the statement ID, so they are unique and
            # nsmallest picks the same victims as sorting every dynamic artifact.
            victims = (
                tuple(
                    heapq_nsmallest(
                        required_evictions,
                        (live_artifacts.get(dynamic_id, {}) for dynamic_id in dynamic_ids),
                        key=eviction_order_key,
                    )
                )
                if required_evictions
                else ()
            )
            for victim in victims:
                lifecycle_after_capacity_eviction(victim.get("lifecycle", LifecycleState.ACTIVE))
                victim_id = victim.get("statement_id", "")
                del artifacts[victim_id]
            artifacts[statement_id] = artifact
            candidate = trusted_repository_state(self.internal_state.get("state_generation", 0) + 1, artifacts)
            outcome = AdmissionOutcome.ADMITTED_WITH_EVICTION if victims else AdmissionOutcome.ADMITTED
            plan = admission_plan(
                outcome=outcome,
                candidate=candidate,
                admitted_statement_id=statement_id,
                evicted_statement_ids=tuple(victim.get("statement_id", "") for victim in victims),
                residency_changed=True,
                lifecycle_changed=False,
                candidate_trusted=True,
            )
            self.plan(plan.get("candidate", {}), (statement_id, *plan.get("evicted_statement_ids", ())))
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
            candidate = trusted_repository_state(self.internal_state.get("state_generation", 0) + 1, artifacts)
            result = self.plan(candidate, (statement_id,))
            return result

    def atomic_replace(self, candidate: dict, expected_state_generation: int) -> dict:
        expected = positive_int(expected_state_generation, "expected repository state_generation")
        with self.internal_lock:
            validated_candidate = validate_repository_state(candidate, self.internal_state.get("artifacts", {}))
            self.replace_trusted(validated_candidate, expected)
        published = self.snapshot()
        return published

    def replace_trusted(
        self,
        candidate: dict,
        expected_state_generation: int,
        changed=(),
        changed_known: bool = False,
    ) -> None:
        """Publish a candidate the caller validated against this repository's live state.

        With ``changed_known``, ``changed`` names every statement ID that differs
        from the live state; otherwise every artifact is compared.
        """
        with self.internal_lock:
            current_generation = self.internal_state.get("state_generation", 0)
            if current_generation != expected_state_generation:
                raise ConflictError(
                    f"repository state generation conflict: expected {expected_state_generation}, current {current_generation}"
                )
            if candidate.get("state_generation", 0) != expected_state_generation + 1:
                raise ConflictError("candidate repository state_generation must advance by exactly one")
            self.internal_state = candidate
            self.index_state(candidate, changed, changed_known)

    def restore_state(self, state: dict) -> dict:
        """Restore one previously validated in-process repository state."""

        with self.internal_lock:
            validated_state = validate_repository_state(state, self.internal_state.get("artifacts", {}))
            self.restore_trusted(validated_state)
        recovered = self.snapshot()
        return recovered

    def restore_trusted(self, state: dict, changed=(), changed_known: bool = False) -> None:
        """Roll back to a state that was live earlier in this process.

        ``changed`` and ``changed_known`` follow ``replace_trusted``.
        """
        with self.internal_lock:
            self.internal_state = state
            self.index_state(state, changed, changed_known)

    def exact_lookup(
        self,
        key: dict,
        context: dict,
    ) -> dict:
        """Look up one key among the artifacts the index says carry it.

        The lookup compares bindings exactly, and an artifact without the key
        never matches, so passing only the carriers gives the same result as
        scanning every artifact.
        """
        try:
            signature = trusted_scoped_retrieval_key_signature(validate_scoped_retrieval_key(key))
        except IdentityValidationError as error:
            raise InvalidRequestError("contextual exact key must be a ScopedRetrievalKey") from error
        with self.internal_lock:
            artifacts = self.internal_state.get("artifacts", {})
            # The key index tracks the live state, so every owner names a stored artifact.
            owners = self.internal_key_owners.get(signature, ())
            carriers = {statement_id: artifacts.get(statement_id, {}) for statement_id in owners}
            lookup = ContextualExactLookup(carriers, trusted_artifacts=True).exact_lookup(key, context)
            result = lookup
            return result
