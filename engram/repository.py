"""Authoritative live accepted-response artifact repository."""

import threading
from collections.abc import Iterable, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from types import MappingProxyType

from engram.artifacts import CachedResponseArtifact, LifecycleState, lifecycle_after_capacity_eviction
from engram.constants import EvictionPolicy, Tier
from engram.eligibility import (
    ContextualExactLookup,
    ContextualExactLookupResult,
    EligibilityContext,
    EpochEligibilityPolicy,
)
from engram.errors import ConflictError, InvalidRequestError, ResourceNotFoundError
from engram.identity import ScopedRetrievalKey
from engram.indexes import IndexOwner, IndexProjection, IndexState, build_index_state, check_index_state

MAX_REPOSITORY_ARTIFACTS = 100_000
MAX_REPOSITORY_CHECK_ISSUES = 1_000


class RepositoryRemovalReason(StrEnum):
    """Physical removal reasons, deliberately separate from lifecycle."""

    EXPLICIT_DELETE = "explicit_delete"
    CAPACITY_EVICTION = "capacity_eviction"


class AdmissionOutcome(StrEnum):
    """Complete bounded accepted-response admission outcomes."""

    ADMITTED = "ADMITTED"
    ADMITTED_WITH_EVICTION = "ADMITTED_WITH_EVICTION"
    REJECTED_CAPACITY = "REJECTED_CAPACITY"


@dataclass(frozen=True, slots=True)
class TierAdmissionPolicy:
    """Bounded DYNAMIC admission and deterministic victim policy."""

    dynamic_capacity: int
    eviction_policy: EvictionPolicy
    minimum_protected_hit_rate: float = 0.0

    def __post_init__(self) -> None:
        _positive_int(self.dynamic_capacity, "dynamic capacity")
        if not isinstance(self.eviction_policy, EvictionPolicy):
            raise InvalidRequestError("eviction_policy must be an EvictionPolicy")
        if isinstance(self.minimum_protected_hit_rate, bool) or not isinstance(self.minimum_protected_hit_rate, float | int):
            raise InvalidRequestError("minimum_protected_hit_rate must be a number")
        if not 0.0 <= float(self.minimum_protected_hit_rate) <= 1.0:
            raise InvalidRequestError("minimum_protected_hit_rate must be between 0 and 1")


@dataclass(frozen=True, slots=True)
class AdmissionPlan:
    """Off-live tier admission result for later atomic coordination."""

    outcome: AdmissionOutcome
    candidate: "RepositoryState"
    admitted_statement_id: str
    evicted_statement_ids: tuple[str, ...]
    affected_epoch_namespaces: tuple[str, ...]
    residency_changed: bool
    lifecycle_changed: bool

    def __post_init__(self) -> None:
        if not isinstance(self.outcome, AdmissionOutcome):
            raise InvalidRequestError("admission outcome must be an AdmissionOutcome")
        if not isinstance(self.candidate, RepositoryState):
            raise InvalidRequestError("admission candidate must be a RepositoryState")
        if not isinstance(self.admitted_statement_id, str):
            raise InvalidRequestError("admitted_statement_id must be a string")
        if not isinstance(self.evicted_statement_ids, tuple) or not all(
            isinstance(statement_id, str) and statement_id for statement_id in self.evicted_statement_ids
        ):
            raise InvalidRequestError("evicted_statement_ids must be a tuple of non-empty strings")
        if not isinstance(self.affected_epoch_namespaces, tuple) or not all(
            isinstance(namespace, str) for namespace in self.affected_epoch_namespaces
        ):
            raise InvalidRequestError("affected_epoch_namespaces must be a tuple of strings")
        if not isinstance(self.residency_changed, bool) or not isinstance(self.lifecycle_changed, bool):
            raise InvalidRequestError("admission change flags must be booleans")
        if self.lifecycle_changed:
            raise InvalidRequestError("tier admission must not change lifecycle")
        rejected = self.outcome == AdmissionOutcome.REJECTED_CAPACITY
        if rejected == self.residency_changed:
            raise InvalidRequestError("admission outcome must agree with residency_changed")
        if rejected and (self.admitted_statement_id or self.evicted_statement_ids or self.affected_epoch_namespaces):
            raise InvalidRequestError("rejected admission must not report state changes")

    def to_dict(self) -> dict[str, object]:
        return {
            "outcome": self.outcome.value,
            "candidate_state_generation": self.candidate.state_generation,
            "admitted_statement_id": self.admitted_statement_id,
            "evicted_statement_ids": list(self.evicted_statement_ids),
            "affected_epoch_namespaces": list(self.affected_epoch_namespaces),
            "residency_changed": self.residency_changed,
            "lifecycle_changed": self.lifecycle_changed,
        }


@dataclass(frozen=True, slots=True)
class RepositoryCheckReport:
    """Bounded equivalence report for artifacts, views, and indexes."""

    consistent: bool
    state_generation: int
    artifact_count: int
    issues: tuple[str, ...]
    omitted_issue_count: int

    def to_dict(self) -> dict[str, object]:
        return {
            "consistent": self.consistent,
            "state_generation": self.state_generation,
            "artifact_count": self.artifact_count,
            "issues": list(self.issues),
            "omitted_issue_count": self.omitted_issue_count,
        }


def _positive_int(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise InvalidRequestError(f"{name} must be a positive integer")
    return value


def _freeze_value(value: object) -> object:
    if isinstance(value, Mapping):
        return MappingProxyType({key: _freeze_value(item) for key, item in value.items()})
    if isinstance(value, list | tuple):
        return tuple(_freeze_value(item) for item in value)
    return value


def _thaw_value(value: object) -> object:
    if isinstance(value, Mapping):
        return {key: _thaw_value(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw_value(item) for item in value]
    return value


def _thaw_mapping(value: object) -> dict[str, object]:
    thawed = _thaw_value(value)
    if not isinstance(thawed, dict):
        raise ConflictError("repository statement view must be an object")
    return thawed


def compatibility_statement_from_artifact(artifact: CachedResponseArtifact) -> Mapping[str, object]:
    """Derive the complete legacy statement shape without sharing authority."""

    if not isinstance(artifact, CachedResponseArtifact):
        raise InvalidRequestError("compatibility source must be a CachedResponseArtifact")
    support = [{"claim_id": claim_id} for claim_id in artifact.support_claim_ids]
    last_hit = (
        datetime.fromisoformat(artifact.statistics.last_hit[:-1] + "+00:00") if artifact.statistics.last_hit_available else ""
    )
    statement = {
        "id": artifact.statement_id,
        "text": artifact.response,
        "tier": artifact.tier,
        "created_at": datetime.fromisoformat(artifact.provenance.accepted_at[:-1] + "+00:00"),
        "keywords": list(artifact.query_identity.lexical_terms),
        "pattern": "",
        "pattern_aliases": [],
        "that": "",
        "topic": "",
        "template": {
            "response_artifact": {
                "schema_version": artifact.schema_version,
                "statement_id": artifact.statement_id,
                "generation": artifact.generation,
                "lifecycle": artifact.lifecycle.value,
                "valid_from": artifact.valid_from,
                "valid_from_available": artifact.valid_from_available,
                "valid_until": artifact.valid_until,
                "valid_until_available": artifact.valid_until_available,
                "knowledge_epoch": artifact.knowledge_epoch,
                "knowledge_epoch_available": artifact.knowledge_epoch_available,
                "superseded_by": artifact.superseded_by,
            },
            "tapestry": {
                "request": artifact.retrieval.canonical,
                "retrieval_aliases": list(artifact.retrieval.aliases),
                "namespace": artifact.scope.namespace,
                "context_fingerprint": artifact.scope.context_fingerprint,
                "support": support,
                "metadata": _thaw_value(artifact.metadata),
            },
        },
        "priority": 0,
        "introduced_by_user_id": artifact.provenance.caller_id,
        "source_label": artifact.provenance.source_label,
        "hit_count": artifact.statistics.hit_count,
        "query_count": artifact.statistics.query_count,
        "last_hit": last_hit,
    }
    frozen = _freeze_value(statement)
    if not isinstance(frozen, Mapping):
        raise ConflictError("derived compatibility statement must be an object")
    return frozen


def _context_required_projection(artifact: CachedResponseArtifact) -> IndexProjection:
    return IndexProjection(
        statement_id=artifact.statement_id,
        generation=artifact.generation,
        retrieval_keys=artifact.retrieval.bindings(artifact.scope),
        support_claim_ids=artifact.support_claim_ids,
        direct_answer_eligible=False,
        exclusion_reason=(
            "eligibility_context_required"
            if artifact.lifecycle == LifecycleState.ACTIVE
            else f"lifecycle_{artifact.lifecycle.value.lower()}"
        ),
    )


def _artifact_hit_rate(artifact: CachedResponseArtifact) -> float:
    if artifact.statistics.query_count == 0:
        return 0.5
    return artifact.statistics.hit_count / artifact.statistics.query_count


def _artifact_time(value: str) -> datetime:
    return datetime.fromisoformat(value[:-1] + "+00:00")


def _eviction_order_key(artifact: CachedResponseArtifact, policy: EvictionPolicy) -> tuple[object, ...]:
    accepted_at = _artifact_time(artifact.provenance.accepted_at)
    last_used = _artifact_time(artifact.statistics.last_hit) if artifact.statistics.last_hit_available else accepted_at
    if policy == EvictionPolicy.FIFO:
        return (accepted_at, artifact.statement_id)
    if policy == EvictionPolicy.LRU:
        return (last_used, 1 if artifact.statistics.last_hit_available else 0, artifact.statement_id)
    if policy == EvictionPolicy.LFU:
        return (artifact.statistics.hit_count, accepted_at, artifact.statement_id)
    if policy == EvictionPolicy.HIT_RATE:
        return (_artifact_hit_rate(artifact), accepted_at, artifact.statement_id)
    raise InvalidRequestError(f"unsupported eviction policy: {policy}")


def _is_protected_dynamic(artifact: CachedResponseArtifact, minimum_hit_rate: float) -> bool:
    return minimum_hit_rate > 0 and artifact.statistics.query_count > 0 and _artifact_hit_rate(artifact) >= minimum_hit_rate


@dataclass(frozen=True, slots=True)
class RepositoryState:
    """One immutable completed artifact, view, and index snapshot."""

    state_generation: int
    artifacts: Mapping[str, CachedResponseArtifact]
    statements: Mapping[str, Mapping[str, object]]
    index_state: IndexState

    def __post_init__(self) -> None:
        _positive_int(self.state_generation, "repository state_generation")
        if not isinstance(self.artifacts, Mapping):
            raise InvalidRequestError("repository artifacts must be an object")
        if not isinstance(self.statements, Mapping):
            raise InvalidRequestError("repository statements must be an object")
        if not isinstance(self.index_state, IndexState):
            raise InvalidRequestError("repository index_state must be an IndexState")
        object.__setattr__(self, "artifacts", MappingProxyType(dict(self.artifacts)))
        object.__setattr__(self, "statements", MappingProxyType(dict(self.statements)))


def build_repository_state(
    artifacts: Iterable[CachedResponseArtifact],
    state_generation: int = 1,
    index_state_generation: int = 1,
) -> RepositoryState:
    """Build a complete conservative candidate without changing live state."""

    generation = _positive_int(state_generation, "repository state_generation")
    index_generation = _positive_int(index_state_generation, "repository index state_generation")
    if isinstance(artifacts, (str, bytes, Mapping)):
        raise InvalidRequestError("repository artifact input must be an iterable of CachedResponseArtifact values")
    artifact_map = {}
    for artifact in artifacts:
        if not isinstance(artifact, CachedResponseArtifact):
            raise InvalidRequestError("repository artifact input must contain CachedResponseArtifact values")
        if artifact.statement_id in artifact_map:
            raise ConflictError(f"duplicate artifact statement_id: {artifact.statement_id}")
        artifact_map[artifact.statement_id] = artifact
        if len(artifact_map) > MAX_REPOSITORY_ARTIFACTS:
            raise InvalidRequestError(f"repository artifacts exceed the limit of {MAX_REPOSITORY_ARTIFACTS}")
    statements = {
        statement_id: compatibility_statement_from_artifact(artifact) for statement_id, artifact in sorted(artifact_map.items())
    }
    projections = tuple(_context_required_projection(artifact) for artifact in artifact_map.values())
    state = RepositoryState(
        state_generation=generation,
        artifacts=artifact_map,
        statements=statements,
        index_state=build_index_state(projections, index_generation),
    )
    report = check_repository_state(state)
    if not report.consistent:
        raise ConflictError(f"candidate repository state failed equivalence checking: {report.issues}")
    return state


def repository_state_with_artifact_updates(
    state: RepositoryState,
    artifacts: tuple[CachedResponseArtifact, ...],
) -> RepositoryState:
    """Rebuild one off-live repository state after same-transaction updates."""

    if not isinstance(state, RepositoryState):
        raise InvalidRequestError("repository update base must be a RepositoryState")
    if not isinstance(artifacts, tuple) or not all(isinstance(artifact, CachedResponseArtifact) for artifact in artifacts):
        raise InvalidRequestError("repository updates must be a tuple of CachedResponseArtifact values")
    updated = dict(state.artifacts)
    for artifact in artifacts:
        if artifact.statement_id not in updated:
            raise ConflictError(f"repository update artifact does not exist in candidate: {artifact.statement_id}")
        updated[artifact.statement_id] = artifact
    return build_repository_state(updated.values(), state.state_generation, state.index_state.state_generation)


def check_repository_state(state: RepositoryState) -> RepositoryCheckReport:
    """Verify complete repository/view/index equivalence without mutation."""

    if not isinstance(state, RepositoryState):
        raise InvalidRequestError("repository check state must be a RepositoryState")
    issues = []
    artifact_ids = set(state.artifacts)
    if artifact_ids != set(state.statements):
        issues.append("artifact_statement_id_set_mismatch")
    if artifact_ids != set(state.index_state.projections):
        issues.append("artifact_projection_id_set_mismatch")
    for statement_id, artifact in state.artifacts.items():
        if statement_id not in state.statements or statement_id not in state.index_state.projections:
            continue
        expected_statement = compatibility_statement_from_artifact(artifact)
        if _thaw_value(state.statements[statement_id]) != _thaw_value(expected_statement):
            issues.append(f"compatibility_statement_mismatch:{statement_id}")
        projection = state.index_state.projections[statement_id]
        if projection.statement_id != artifact.statement_id or projection.generation != artifact.generation:
            issues.append(f"projection_identity_mismatch:{statement_id}")
        if projection.retrieval_keys != artifact.retrieval.bindings(artifact.scope):
            issues.append(f"projection_retrieval_mismatch:{statement_id}")
        if projection.support_claim_ids != artifact.support_claim_ids:
            issues.append(f"projection_support_mismatch:{statement_id}")
    index_report = check_index_state(state.index_state)
    if not index_report.consistent:
        issues.append("index_state_inconsistent")
    bounded = tuple(issues[:MAX_REPOSITORY_CHECK_ISSUES])
    return RepositoryCheckReport(
        consistent=not issues,
        state_generation=state.state_generation,
        artifact_count=len(state.artifacts),
        issues=bounded,
        omitted_issue_count=len(issues) - len(bounded),
    )


class ArtifactRepository:
    """Atomic owner of authoritative artifacts and every derived live view.

    Lock order is repository lock, then the private Section 2 index-owner lock.
    The index owner is never exposed, so callers cannot invert that order.
    """

    def __init__(self, artifacts: Iterable[CachedResponseArtifact] = ()) -> None:
        self._lock = threading.RLock()
        initial = build_repository_state(artifacts)
        self._indexes = IndexOwner(initial.index_state.projections.values())
        self._state = RepositoryState(
            state_generation=initial.state_generation,
            artifacts=initial.artifacts,
            statements=initial.statements,
            index_state=self._indexes.snapshot(),
        )

    def snapshot(self) -> RepositoryState:
        with self._lock:
            return self._state

    @contextmanager
    def coordinated_mutation(self):
        """Hold the repository boundary across candidate checkpoint/publication."""

        with self._lock:
            yield

    def get_artifact(self, statement_id: str) -> CachedResponseArtifact:
        if not isinstance(statement_id, str) or not statement_id:
            raise InvalidRequestError("repository statement_id must be a non-empty string")
        with self._lock:
            if statement_id not in self._state.artifacts:
                raise ResourceNotFoundError(f"accepted response artifact not found: {statement_id}")
            return self._state.artifacts[statement_id]

    def get_statement(self, statement_id: str) -> dict[str, object]:
        self.get_artifact(statement_id)
        with self._lock:
            return _thaw_mapping(self._state.statements[statement_id])

    def statement_views(self) -> list[dict[str, object]]:
        with self._lock:
            return [_thaw_mapping(self._state.statements[statement_id]) for statement_id in sorted(self._state.statements)]

    def check(self) -> RepositoryCheckReport:
        return check_repository_state(self.snapshot())

    def candidate_with_artifact(self, artifact: CachedResponseArtifact) -> RepositoryState:
        if not isinstance(artifact, CachedResponseArtifact):
            raise InvalidRequestError("repository artifact must be a CachedResponseArtifact")
        with self._lock:
            artifacts = dict(self._state.artifacts)
            artifacts[artifact.statement_id] = artifact
            return build_repository_state(
                artifacts.values(),
                self._state.state_generation + 1,
                self._state.index_state.state_generation + 1,
            )

    def candidate_with_artifacts(self, artifacts: tuple[CachedResponseArtifact, ...]) -> RepositoryState:
        """Build one next-generation candidate containing multiple artifact updates."""

        if not isinstance(artifacts, tuple) or not all(isinstance(artifact, CachedResponseArtifact) for artifact in artifacts):
            raise InvalidRequestError("repository artifacts must be a tuple of CachedResponseArtifact values")
        if len({artifact.statement_id for artifact in artifacts}) != len(artifacts):
            raise ConflictError("repository artifact updates must have unique statement IDs")
        with self._lock:
            updated = dict(self._state.artifacts)
            for artifact in artifacts:
                if artifact.statement_id not in updated:
                    raise ResourceNotFoundError(f"accepted response artifact not found: {artifact.statement_id}")
                updated[artifact.statement_id] = artifact
            return build_repository_state(
                updated.values(),
                self._state.state_generation + 1,
                self._state.index_state.state_generation + 1,
            )

    def plan_admission(self, artifact: CachedResponseArtifact, policy: TierAdmissionPolicy) -> AdmissionPlan:
        """Plan bounded tier admission without publishing any live mutation."""

        if not isinstance(artifact, CachedResponseArtifact):
            raise InvalidRequestError("admission artifact must be a CachedResponseArtifact")
        if not isinstance(policy, TierAdmissionPolicy):
            raise InvalidRequestError("admission policy must be a TierAdmissionPolicy")
        with self._lock:
            if artifact.statement_id in self._state.artifacts:
                raise ConflictError(f"artifact statement_id already exists: {artifact.statement_id}")
            artifacts = dict(self._state.artifacts)
            if artifact.tier == Tier.STATIC:
                artifacts[artifact.statement_id] = artifact
                candidate = build_repository_state(
                    artifacts.values(),
                    self._state.state_generation + 1,
                    self._state.index_state.state_generation + 1,
                )
                return AdmissionPlan(
                    outcome=AdmissionOutcome.ADMITTED,
                    candidate=candidate,
                    admitted_statement_id=artifact.statement_id,
                    evicted_statement_ids=(),
                    affected_epoch_namespaces=(artifact.scope.namespace,),
                    residency_changed=True,
                    lifecycle_changed=False,
                )

            dynamics = tuple(existing for existing in artifacts.values() if existing.tier == Tier.DYNAMIC)
            required_evictions = max(0, len(dynamics) - policy.dynamic_capacity + 1)
            victims = tuple(
                sorted(
                    (
                        existing
                        for existing in dynamics
                        if not _is_protected_dynamic(existing, float(policy.minimum_protected_hit_rate))
                    ),
                    key=lambda existing: _eviction_order_key(existing, policy.eviction_policy),
                )[:required_evictions]
            )
            if len(victims) != required_evictions:
                return AdmissionPlan(
                    outcome=AdmissionOutcome.REJECTED_CAPACITY,
                    candidate=self._state,
                    admitted_statement_id="",
                    evicted_statement_ids=(),
                    affected_epoch_namespaces=(),
                    residency_changed=False,
                    lifecycle_changed=False,
                )
            for victim in victims:
                lifecycle_after_capacity_eviction(victim.lifecycle)
                del artifacts[victim.statement_id]
            artifacts[artifact.statement_id] = artifact
            candidate = build_repository_state(
                artifacts.values(),
                self._state.state_generation + 1,
                self._state.index_state.state_generation + 1,
            )
            outcome = AdmissionOutcome.ADMITTED_WITH_EVICTION if victims else AdmissionOutcome.ADMITTED
            affected_namespaces = tuple(sorted({artifact.scope.namespace, *(victim.scope.namespace for victim in victims)}))
            return AdmissionPlan(
                outcome=outcome,
                candidate=candidate,
                admitted_statement_id=artifact.statement_id,
                evicted_statement_ids=tuple(victim.statement_id for victim in victims),
                affected_epoch_namespaces=affected_namespaces,
                residency_changed=True,
                lifecycle_changed=False,
            )

    def candidate_without_artifact(
        self,
        statement_id: str,
        expected_generation: int,
        reason: RepositoryRemovalReason,
    ) -> RepositoryState:
        if not isinstance(statement_id, str) or not statement_id:
            raise InvalidRequestError("repository statement_id must be a non-empty string")
        expected = _positive_int(expected_generation, "expected artifact generation")
        if not isinstance(reason, RepositoryRemovalReason):
            raise InvalidRequestError("repository removal reason must be a RepositoryRemovalReason")
        with self._lock:
            if statement_id not in self._state.artifacts:
                raise ResourceNotFoundError(f"accepted response artifact not found: {statement_id}")
            artifact = self._state.artifacts[statement_id]
            if artifact.generation != expected:
                raise ConflictError(
                    f"artifact generation conflict for {statement_id}: expected {expected}, current {artifact.generation}"
                )
            if reason == RepositoryRemovalReason.CAPACITY_EVICTION:
                if artifact.tier != Tier.DYNAMIC:
                    raise ConflictError(f"capacity eviction cannot remove STATIC artifact: {statement_id}")
                lifecycle_after_capacity_eviction(artifact.lifecycle)
            artifacts = dict(self._state.artifacts)
            del artifacts[statement_id]
            return build_repository_state(
                artifacts.values(),
                self._state.state_generation + 1,
                self._state.index_state.state_generation + 1,
            )

    def atomic_replace(self, candidate: RepositoryState, expected_state_generation: int) -> RepositoryState:
        if not isinstance(candidate, RepositoryState):
            raise InvalidRequestError("repository candidate must be a RepositoryState")
        expected = _positive_int(expected_state_generation, "expected repository state_generation")
        report = check_repository_state(candidate)
        if not report.consistent:
            raise ConflictError("candidate repository state failed equivalence checking")
        with self._lock:
            if self._state.state_generation != expected:
                raise ConflictError(
                    f"repository state generation conflict: expected {expected}, current {self._state.state_generation}"
                )
            if candidate.state_generation != expected + 1:
                raise ConflictError("candidate repository state_generation must advance by exactly one")
            self._indexes.atomic_swap(candidate.index_state, self._state.index_state.state_generation)
            self._state = RepositoryState(
                state_generation=candidate.state_generation,
                artifacts=candidate.artifacts,
                statements=candidate.statements,
                index_state=self._indexes.snapshot(),
            )
            return self._state

    def recover_from_durable_state(self, durable: RepositoryState) -> RepositoryState:
        """Converge live derived state from an already-durable authority."""

        if not isinstance(durable, RepositoryState):
            raise InvalidRequestError("durable repository state must be a RepositoryState")
        report = check_repository_state(durable)
        if not report.consistent:
            raise ConflictError("durable repository state failed equivalence checking")
        with self._lock:
            self._indexes = IndexOwner(durable.index_state.projections.values())
            self._state = RepositoryState(
                state_generation=durable.state_generation,
                artifacts=durable.artifacts,
                statements=durable.statements,
                index_state=self._indexes.snapshot(),
            )
            return self._state

    def exact_lookup(
        self,
        key: ScopedRetrievalKey,
        context: EligibilityContext,
        epoch_policy: EpochEligibilityPolicy,
    ) -> ContextualExactLookupResult:
        with self._lock:
            lookup = ContextualExactLookup(self._state.artifacts, self._indexes).exact_lookup(key, context, epoch_policy)
            if lookup.index_refreshed:
                self._state = RepositoryState(
                    state_generation=self._state.state_generation + 1,
                    artifacts=self._state.artifacts,
                    statements=self._state.statements,
                    index_state=self._indexes.snapshot(),
                )
            return lookup
