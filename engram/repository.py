"""Authoritative live accepted-response artifact repository."""

import threading
from collections.abc import Iterable, Mapping
from contextlib import contextmanager
from datetime import datetime
from types import MappingProxyType

from engram.artifacts import (
    CachedResponseArtifact,
    LifecycleState,
    lifecycle_after_capacity_eviction,
    validate_cached_response_artifact,
)
from engram.constants import (
    ADMISSION_PLAN_FIELDS,
    MAX_REPOSITORY_ARTIFACTS,
    MAX_REPOSITORY_CHECK_ISSUES,
    REPOSITORY_CHECK_REPORT_FIELDS,
    REPOSITORY_STATE_FIELDS,
    TIER_ADMISSION_POLICY_FIELDS,
    AdmissionOutcome,
    EvictionPolicy,
    RepositoryRemovalReason,
    Tier,
)
from engram.eligibility import ContextualExactLookup, ContextualExactLookupResult, EligibilityContext, EpochEligibilityPolicy
from engram.errors import ConflictError, InvalidRequestError, ResourceNotFoundError
from engram.identity import ScopedRetrievalKey, retrieval_representation_bindings
from engram.indexes import (
    IndexOwner,
    IndexProjection,
    IndexState,
    build_index_state,
    check_index_state,
    index_projection,
    validate_index_state,
)

TierAdmissionPolicy = dict


def tier_admission_policy(
    dynamic_capacity: int,
    eviction_policy: EvictionPolicy,
    minimum_protected_hit_rate: object = 0.0,
) -> TierAdmissionPolicy:
    """Build one validated tier-admission policy dictionary."""
    capacity = _positive_int(dynamic_capacity, "dynamic capacity")
    if not isinstance(eviction_policy, EvictionPolicy):
        raise InvalidRequestError("eviction_policy must be an EvictionPolicy")
    if isinstance(minimum_protected_hit_rate, bool) or not isinstance(minimum_protected_hit_rate, (float, int)):
        raise InvalidRequestError("minimum_protected_hit_rate must be a number")
    protected_rate = float(minimum_protected_hit_rate)
    if not 0.0 <= protected_rate <= 1.0:
        raise InvalidRequestError("minimum_protected_hit_rate must be between 0 and 1")
    result: TierAdmissionPolicy = {
        "dynamic_capacity": capacity,
        "eviction_policy": eviction_policy,
        "minimum_protected_hit_rate": protected_rate,
    }
    return result


def validate_tier_admission_policy(value: object) -> TierAdmissionPolicy:
    """Validate and copy one tier-admission policy dictionary."""
    if not isinstance(value, Mapping) or set(value) != TIER_ADMISSION_POLICY_FIELDS:
        raise InvalidRequestError("admission policy must be a TierAdmissionPolicy")
    dynamic_capacity = value.get("dynamic_capacity", ())
    eviction_policy = value.get("eviction_policy", ())
    minimum_protected_hit_rate = value.get("minimum_protected_hit_rate", ())
    if not isinstance(dynamic_capacity, int) or not isinstance(eviction_policy, EvictionPolicy):
        raise InvalidRequestError("admission policy must be a TierAdmissionPolicy")
    result = tier_admission_policy(dynamic_capacity, eviction_policy, minimum_protected_hit_rate)
    return result


RepositoryCheckReport = dict


def repository_check_report(
    consistent: bool,
    state_generation: int,
    artifact_count: int,
    issues: tuple[str, ...],
    omitted_issue_count: int,
) -> RepositoryCheckReport:
    """Build one validated bounded repository-equivalence report."""

    if not isinstance(consistent, bool):
        raise InvalidRequestError("repository check consistent must be a boolean")
    generation = _positive_int(state_generation, "repository check state_generation")
    count = _nonnegative_int(artifact_count, "repository check artifact_count")
    if not isinstance(issues, tuple) or not all(isinstance(issue, str) and issue for issue in issues):
        raise InvalidRequestError("repository check issues must be a tuple of non-empty strings")
    omitted = _nonnegative_int(omitted_issue_count, "repository check omitted_issue_count")
    if consistent != (not issues and omitted == 0):
        raise InvalidRequestError("repository check consistency must agree with issue accounting")
    result: RepositoryCheckReport = {
        "consistent": consistent,
        "state_generation": generation,
        "artifact_count": count,
        "issues": issues,
        "omitted_issue_count": omitted,
    }
    return result


def validate_repository_check_report(value: object) -> RepositoryCheckReport:
    """Validate and copy one repository-equivalence report dictionary."""

    if not isinstance(value, Mapping) or set(value) != REPOSITORY_CHECK_REPORT_FIELDS:
        raise InvalidRequestError("repository check report must be a RepositoryCheckReport")
    consistent = value.get("consistent", ())
    state_generation = value.get("state_generation", ())
    artifact_count = value.get("artifact_count", ())
    issues = value.get("issues", ())
    omitted_issue_count = value.get("omitted_issue_count", ())
    valid_types = (
        isinstance(consistent, bool)
        and isinstance(state_generation, int)
        and isinstance(artifact_count, int)
        and isinstance(issues, tuple)
        and isinstance(omitted_issue_count, int)
    )
    if not valid_types:
        raise InvalidRequestError("repository check report must be a RepositoryCheckReport")
    result = repository_check_report(
        consistent,
        state_generation,
        artifact_count,
        issues,
        omitted_issue_count,
    )
    return result


def repository_check_report_to_dict(value: object) -> dict[str, object]:
    """Serialize one validated repository-equivalence report."""

    validated = validate_repository_check_report(value)
    result = {
        "consistent": validated["consistent"],
        "state_generation": validated["state_generation"],
        "artifact_count": validated["artifact_count"],
        "issues": list(validated["issues"]),
        "omitted_issue_count": validated["omitted_issue_count"],
    }
    return result


def _positive_int(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise InvalidRequestError(f"{name} must be a positive integer")
    return value


def _nonnegative_int(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise InvalidRequestError(f"{name} must be a non-negative integer")
    return value


def _freeze_value(value: object) -> object:
    if isinstance(value, Mapping):
        result = MappingProxyType({key: _freeze_value(item) for key, item in value.items()})
    elif isinstance(value, (list, tuple)):
        result = tuple(_freeze_value(item) for item in value)
    else:
        result = value
    return result


def _thaw_value(value: object) -> object:
    if isinstance(value, Mapping):
        result = {key: _thaw_value(item) for key, item in value.items()}
    elif isinstance(value, tuple):
        result = [_thaw_value(item) for item in value]
    else:
        result = value
    return result


def _thaw_mapping(value: object) -> dict[str, object]:
    thawed = _thaw_value(value)
    if not isinstance(thawed, dict):
        raise ConflictError("repository statement view must be an object")
    return thawed


def compatibility_statement_from_artifact(artifact: CachedResponseArtifact) -> Mapping[str, object]:
    """Derive the complete legacy statement shape without sharing authority."""

    try:
        artifact = validate_cached_response_artifact(artifact)
    except InvalidRequestError as error:
        raise InvalidRequestError("compatibility source must be a CachedResponseArtifact") from error
    support = [{"claim_id": claim_id} for claim_id in artifact["support_claim_ids"]]
    last_hit = (
        datetime.fromisoformat(artifact["statistics"]["last_hit"][:-1] + "+00:00")
        if artifact["statistics"]["last_hit_available"]
        else ""
    )
    statement = {
        "id": artifact["statement_id"],
        "text": artifact["response"],
        "tier": artifact["tier"],
        "created_at": datetime.fromisoformat(artifact["provenance"]["accepted_at"][:-1] + "+00:00"),
        "keywords": list(artifact["query_identity"]["lexical_terms"]),
        "pattern": "",
        "pattern_aliases": [],
        "that": "",
        "topic": "",
        "template": {
            "response_artifact": {
                "schema_version": artifact["schema_version"],
                "statement_id": artifact["statement_id"],
                "generation": artifact["generation"],
                "lifecycle": artifact["lifecycle"].value,
                "valid_from": artifact["valid_from"],
                "valid_from_available": artifact["valid_from_available"],
                "valid_until": artifact["valid_until"],
                "valid_until_available": artifact["valid_until_available"],
                "knowledge_epoch": artifact["knowledge_epoch"],
                "knowledge_epoch_available": artifact["knowledge_epoch_available"],
                "superseded_by": artifact["superseded_by"],
            },
            "tapestry": {
                "request": artifact["retrieval"]["canonical"],
                "retrieval_aliases": list(artifact["retrieval"]["aliases"]),
                "namespace": artifact["scope"]["namespace"],
                "context_fingerprint": artifact["scope"]["context_fingerprint"],
                "support": support,
                "metadata": _thaw_value(artifact["metadata"]),
            },
        },
        "priority": 0,
        "introduced_by_user_id": artifact["provenance"]["caller_id"],
        "source_label": artifact["provenance"]["source_label"],
        "hit_count": artifact["statistics"]["hit_count"],
        "query_count": artifact["statistics"]["query_count"],
        "last_hit": last_hit,
    }
    frozen = _freeze_value(statement)
    if not isinstance(frozen, Mapping):
        raise ConflictError("derived compatibility statement must be an object")
    return frozen


def _context_required_projection(artifact: CachedResponseArtifact) -> IndexProjection:
    exclusion_reason = (
        "eligibility_context_required"
        if artifact["lifecycle"] == LifecycleState.ACTIVE
        else f"lifecycle_{artifact['lifecycle'].value.lower()}"
    )
    result = index_projection(
        artifact["statement_id"],
        artifact["generation"],
        retrieval_representation_bindings(artifact["retrieval"], artifact["scope"]),
        artifact["support_claim_ids"],
        False,
        exclusion_reason,
    )
    return result


def _artifact_hit_rate(artifact: CachedResponseArtifact) -> float:
    statistics = artifact["statistics"]
    result = 0.5 if statistics["query_count"] == 0 else statistics["hit_count"] / statistics["query_count"]
    return result


def _artifact_time(value: str) -> datetime:
    result = datetime.fromisoformat(value[:-1] + "+00:00")
    return result


def _eviction_order_key(artifact: CachedResponseArtifact, policy: EvictionPolicy) -> tuple[object, ...]:
    statistics = artifact["statistics"]
    accepted_at = _artifact_time(artifact["provenance"]["accepted_at"])
    last_used = _artifact_time(statistics["last_hit"]) if statistics["last_hit_available"] else accepted_at
    if policy == EvictionPolicy.FIFO:
        result = (accepted_at, artifact["statement_id"])
    elif policy == EvictionPolicy.LRU:
        result = (last_used, 1 if statistics["last_hit_available"] else 0, artifact["statement_id"])
    elif policy == EvictionPolicy.LFU:
        result = (statistics["hit_count"], accepted_at, artifact["statement_id"])
    elif policy == EvictionPolicy.HIT_RATE:
        result = (_artifact_hit_rate(artifact), accepted_at, artifact["statement_id"])
    else:
        raise InvalidRequestError(f"unsupported eviction policy: {policy}")
    return result


def _is_protected_dynamic(artifact: CachedResponseArtifact, minimum_hit_rate: float) -> bool:
    result = minimum_hit_rate > 0 and artifact["statistics"]["query_count"] > 0 and _artifact_hit_rate(artifact) >= minimum_hit_rate
    return result


RepositoryState = dict


def repository_state(
    state_generation: int,
    artifacts: object,
    statements: object,
    index_state: IndexState,
) -> RepositoryState:
    """Build one validated repository snapshot dictionary."""

    generation = _positive_int(state_generation, "repository state_generation")
    if not isinstance(artifacts, Mapping):
        raise InvalidRequestError("repository artifacts must be an object")
    validated_artifacts = {}
    for statement_id, artifact in artifacts.items():
        if not isinstance(statement_id, str) or not statement_id:
            raise InvalidRequestError("repository artifacts must map statement IDs to CachedResponseArtifact values")
        try:
            validated_artifact = validate_cached_response_artifact(artifact)
        except InvalidRequestError as error:
            raise InvalidRequestError("repository artifacts must map statement IDs to CachedResponseArtifact values") from error
        if validated_artifact["statement_id"] != statement_id:
            raise InvalidRequestError("repository artifacts must map statement IDs to CachedResponseArtifact values")
        validated_artifacts[statement_id] = validated_artifact
    if not isinstance(statements, Mapping):
        raise InvalidRequestError("repository statements must be an object")
    valid_statements = all(
        isinstance(statement_id, str) and statement_id and isinstance(statement, Mapping)
        for statement_id, statement in statements.items()
    )
    if not valid_statements:
        raise InvalidRequestError("repository statements must map statement IDs to objects")
    try:
        validated_index_state = validate_index_state(index_state)
    except InvalidRequestError as error:
        raise InvalidRequestError("repository index_state must be an IndexState") from error
    frozen_artifacts = MappingProxyType(validated_artifacts)
    frozen_statements = MappingProxyType(dict(statements))
    result: RepositoryState = {
        "state_generation": generation,
        "artifacts": frozen_artifacts,
        "statements": frozen_statements,
        "index_state": validated_index_state,
    }
    return result


def validate_repository_state(value: object) -> RepositoryState:
    """Validate and copy one repository snapshot dictionary."""

    if not isinstance(value, Mapping) or set(value) != REPOSITORY_STATE_FIELDS:
        raise InvalidRequestError("repository state must be a RepositoryState")
    state_generation = value.get("state_generation", ())
    artifacts = value.get("artifacts", ())
    statements = value.get("statements", ())
    index_state = value.get("index_state", ())
    if not isinstance(state_generation, int):
        raise InvalidRequestError("repository state must be a RepositoryState")
    result = repository_state(state_generation, artifacts, statements, index_state)
    return result


AdmissionPlan = dict


def admission_plan(
    outcome: AdmissionOutcome,
    candidate: RepositoryState,
    admitted_statement_id: str,
    evicted_statement_ids: tuple[str, ...],
    affected_epoch_namespaces: tuple[str, ...],
    residency_changed: bool,
    lifecycle_changed: bool,
) -> AdmissionPlan:
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
    valid_namespaces = isinstance(affected_epoch_namespaces, tuple) and all(
        isinstance(namespace, str) for namespace in affected_epoch_namespaces
    )
    if not valid_namespaces:
        raise InvalidRequestError("affected_epoch_namespaces must be a tuple of strings")
    if not isinstance(residency_changed, bool) or not isinstance(lifecycle_changed, bool):
        raise InvalidRequestError("admission change flags must be booleans")
    if lifecycle_changed:
        raise InvalidRequestError("tier admission must not change lifecycle")
    rejected = outcome == AdmissionOutcome.REJECTED_CAPACITY
    if rejected == residency_changed:
        raise InvalidRequestError("admission outcome must agree with residency_changed")
    state_changes_reported = admitted_statement_id or evicted_statement_ids or affected_epoch_namespaces
    if rejected and state_changes_reported:
        raise InvalidRequestError("rejected admission must not report state changes")
    result: AdmissionPlan = {
        "outcome": outcome,
        "candidate": validated_candidate,
        "admitted_statement_id": admitted_statement_id,
        "evicted_statement_ids": evicted_statement_ids,
        "affected_epoch_namespaces": affected_epoch_namespaces,
        "residency_changed": residency_changed,
        "lifecycle_changed": lifecycle_changed,
    }
    return result


def validate_admission_plan(value: object) -> AdmissionPlan:
    """Validate and copy one tier-admission result dictionary."""

    if not isinstance(value, Mapping) or set(value) != ADMISSION_PLAN_FIELDS:
        raise InvalidRequestError("admission plan must be an AdmissionPlan")
    outcome = value.get("outcome", ())
    candidate = value.get("candidate", ())
    admitted_statement_id = value.get("admitted_statement_id", ())
    evicted_statement_ids = value.get("evicted_statement_ids", ())
    affected_epoch_namespaces = value.get("affected_epoch_namespaces", ())
    residency_changed = value.get("residency_changed", ())
    lifecycle_changed = value.get("lifecycle_changed", ())
    valid_types = (
        isinstance(outcome, AdmissionOutcome)
        and isinstance(admitted_statement_id, str)
        and isinstance(evicted_statement_ids, tuple)
        and isinstance(affected_epoch_namespaces, tuple)
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
        affected_epoch_namespaces,
        residency_changed,
        lifecycle_changed,
    )
    return result


def admission_plan_to_dict(value: object) -> dict[str, object]:
    """Serialize one validated admission plan without its candidate snapshot."""

    validated = validate_admission_plan(value)
    candidate = validated["candidate"]
    result = {
        "outcome": validated["outcome"].value,
        "candidate_state_generation": candidate["state_generation"],
        "admitted_statement_id": validated["admitted_statement_id"],
        "evicted_statement_ids": list(validated["evicted_statement_ids"]),
        "affected_epoch_namespaces": list(validated["affected_epoch_namespaces"]),
        "residency_changed": validated["residency_changed"],
        "lifecycle_changed": validated["lifecycle_changed"],
    }
    return result


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
        try:
            validated_artifact = validate_cached_response_artifact(artifact)
        except InvalidRequestError as error:
            raise InvalidRequestError("repository artifact input must contain CachedResponseArtifact values") from error
        statement_id = validated_artifact["statement_id"]
        if statement_id in artifact_map:
            raise ConflictError(f"duplicate artifact statement_id: {statement_id}")
        artifact_map[statement_id] = validated_artifact
        if len(artifact_map) > MAX_REPOSITORY_ARTIFACTS:
            raise InvalidRequestError(f"repository artifacts exceed the limit of {MAX_REPOSITORY_ARTIFACTS}")
    statements = {
        statement_id: compatibility_statement_from_artifact(artifact) for statement_id, artifact in sorted(artifact_map.items())
    }
    projections = tuple(_context_required_projection(artifact) for artifact in artifact_map.values())
    state = repository_state(
        state_generation=generation,
        artifacts=artifact_map,
        statements=statements,
        index_state=build_index_state(projections, index_generation),
    )
    report = check_repository_state(state)
    if not report["consistent"]:
        raise ConflictError(f"candidate repository state failed equivalence checking: {report['issues']}")
    return state


def repository_state_with_artifact_updates(
    state: RepositoryState,
    artifacts: tuple[CachedResponseArtifact, ...],
) -> RepositoryState:
    """Rebuild one off-live repository state after same-transaction updates."""

    validated_state = validate_repository_state(state)
    if not isinstance(artifacts, tuple):
        raise InvalidRequestError("repository updates must be a tuple of CachedResponseArtifact values")
    try:
        validated_artifacts = tuple(validate_cached_response_artifact(artifact) for artifact in artifacts)
    except InvalidRequestError as error:
        raise InvalidRequestError("repository updates must be a tuple of CachedResponseArtifact values") from error
    updated = dict(validated_state["artifacts"])
    for artifact in validated_artifacts:
        if artifact["statement_id"] not in updated:
            raise ConflictError(f"repository update artifact does not exist in candidate: {artifact['statement_id']}")
        updated[artifact["statement_id"]] = artifact
    result = build_repository_state(
        updated.values(),
        validated_state["state_generation"],
        validated_state["index_state"]["state_generation"],
    )
    return result


def check_repository_state(state: RepositoryState) -> RepositoryCheckReport:
    """Verify complete repository/view/index equivalence without mutation."""

    validated_state = validate_repository_state(state)
    issues = []
    artifacts = validated_state["artifacts"]
    statements = validated_state["statements"]
    index_state = validated_state["index_state"]
    artifact_ids = set(artifacts)
    if artifact_ids != set(statements):
        issues.append("artifact_statement_id_set_mismatch")
    if artifact_ids != set(index_state["projections"]):
        issues.append("artifact_projection_id_set_mismatch")
    for statement_id, artifact in artifacts.items():
        if statement_id not in statements or statement_id not in index_state["projections"]:
            continue
        expected_statement = compatibility_statement_from_artifact(artifact)
        if _thaw_value(statements[statement_id]) != _thaw_value(expected_statement):
            issues.append(f"compatibility_statement_mismatch:{statement_id}")
        projection = index_state["projections"][statement_id]
        if projection["statement_id"] != artifact["statement_id"] or projection["generation"] != artifact["generation"]:
            issues.append(f"projection_identity_mismatch:{statement_id}")
        if projection["retrieval_keys"] != retrieval_representation_bindings(artifact["retrieval"], artifact["scope"]):
            issues.append(f"projection_retrieval_mismatch:{statement_id}")
        if projection["support_claim_ids"] != artifact["support_claim_ids"]:
            issues.append(f"projection_support_mismatch:{statement_id}")
    index_report = check_index_state(index_state)
    if not index_report["consistent"]:
        issues.append("index_state_inconsistent")
    bounded = tuple(issues[:MAX_REPOSITORY_CHECK_ISSUES])
    report = repository_check_report(
        consistent=not issues,
        state_generation=validated_state["state_generation"],
        artifact_count=len(artifacts),
        issues=bounded,
        omitted_issue_count=len(issues) - len(bounded),
    )
    return report


class ArtifactRepository:
    """Atomic owner of authoritative artifacts and every derived live view.

    Lock order is repository lock, then the private Section 2 index-owner lock.
    The index owner is never exposed, so callers cannot invert that order.
    """

    def __init__(self, artifacts: Iterable[CachedResponseArtifact] = ()) -> None:
        self._lock = threading.RLock()
        initial = build_repository_state(artifacts)
        self._indexes = IndexOwner(initial["index_state"]["projections"].values())
        self._state = repository_state(
            state_generation=initial["state_generation"],
            artifacts=initial["artifacts"],
            statements=initial["statements"],
            index_state=self._indexes.snapshot(),
        )

    def snapshot(self) -> RepositoryState:
        with self._lock:
            snapshot = validate_repository_state(self._state)
            return snapshot

    @contextmanager
    def coordinated_mutation(self):
        """Hold the repository boundary across candidate checkpoint/publication."""

        with self._lock:
            yield

    def get_artifact(self, statement_id: str) -> CachedResponseArtifact:
        if not isinstance(statement_id, str) or not statement_id:
            raise InvalidRequestError("repository statement_id must be a non-empty string")
        with self._lock:
            if statement_id not in self._state["artifacts"]:
                raise ResourceNotFoundError(f"accepted response artifact not found: {statement_id}")
            artifact = validate_cached_response_artifact(self._state["artifacts"][statement_id])
            return artifact

    def _trusted_get_artifact(self, statement_id: str) -> CachedResponseArtifact:
        """Return an immutable artifact already validated at repository publication."""
        with self._lock:
            if statement_id not in self._state["artifacts"]:
                raise ResourceNotFoundError(f"accepted response artifact not found: {statement_id}")
            artifact = self._state["artifacts"][statement_id]
            return artifact

    def get_statement(self, statement_id: str) -> dict[str, object]:
        self.get_artifact(statement_id)
        with self._lock:
            statement = _thaw_mapping(self._state["statements"][statement_id])
            return statement

    def statement_views(self) -> list[dict[str, object]]:
        with self._lock:
            statements = self._state["statements"]
            views = [_thaw_mapping(statements[statement_id]) for statement_id in sorted(statements)]
            return views

    def check(self) -> RepositoryCheckReport:
        snapshot = self.snapshot()
        report = check_repository_state(snapshot)
        return report

    def candidate_with_artifact(self, artifact: CachedResponseArtifact) -> RepositoryState:
        try:
            artifact = validate_cached_response_artifact(artifact)
        except InvalidRequestError as error:
            raise InvalidRequestError("repository artifact must be a CachedResponseArtifact") from error
        with self._lock:
            artifacts = dict(self._state["artifacts"])
            artifacts[artifact["statement_id"]] = artifact
            candidate = build_repository_state(
                artifacts.values(),
                self._state["state_generation"] + 1,
                self._state["index_state"]["state_generation"] + 1,
            )
            return candidate

    def candidate_with_artifacts(self, artifacts: tuple[CachedResponseArtifact, ...]) -> RepositoryState:
        """Build one next-generation candidate containing multiple artifact updates."""

        if not isinstance(artifacts, tuple):
            raise InvalidRequestError("repository artifacts must be a tuple of CachedResponseArtifact values")
        try:
            validated_artifacts = tuple(validate_cached_response_artifact(artifact) for artifact in artifacts)
        except InvalidRequestError as error:
            raise InvalidRequestError("repository artifacts must be a tuple of CachedResponseArtifact values") from error
        if len({artifact["statement_id"] for artifact in validated_artifacts}) != len(validated_artifacts):
            raise ConflictError("repository artifact updates must have unique statement IDs")
        with self._lock:
            updated = dict(self._state["artifacts"])
            for artifact in validated_artifacts:
                if artifact["statement_id"] not in updated:
                    raise ResourceNotFoundError(f"accepted response artifact not found: {artifact['statement_id']}")
                updated[artifact["statement_id"]] = artifact
            candidate = build_repository_state(
                updated.values(),
                self._state["state_generation"] + 1,
                self._state["index_state"]["state_generation"] + 1,
            )
            return candidate

    def plan_admission(self, artifact: CachedResponseArtifact, policy: TierAdmissionPolicy) -> AdmissionPlan:
        """Plan bounded tier admission without publishing any live mutation."""

        try:
            artifact = validate_cached_response_artifact(artifact)
        except InvalidRequestError as error:
            raise InvalidRequestError("admission artifact must be a CachedResponseArtifact") from error
        validated_policy = validate_tier_admission_policy(policy)
        with self._lock:
            if artifact["statement_id"] in self._state["artifacts"]:
                raise ConflictError(f"artifact statement_id already exists: {artifact['statement_id']}")
            artifacts = dict(self._state["artifacts"])
            if artifact["tier"] == Tier.STATIC:
                artifacts[artifact["statement_id"]] = artifact
                candidate = build_repository_state(
                    artifacts.values(),
                    self._state["state_generation"] + 1,
                    self._state["index_state"]["state_generation"] + 1,
                )
                plan = admission_plan(
                    outcome=AdmissionOutcome.ADMITTED,
                    candidate=candidate,
                    admitted_statement_id=artifact["statement_id"],
                    evicted_statement_ids=(),
                    affected_epoch_namespaces=(artifact["scope"]["namespace"],),
                    residency_changed=True,
                    lifecycle_changed=False,
                )
                return plan

            dynamics = tuple(existing for existing in artifacts.values() if existing["tier"] == Tier.DYNAMIC)
            required_evictions = max(0, len(dynamics) - validated_policy["dynamic_capacity"] + 1)
            victims = tuple(
                sorted(
                    (
                        existing
                        for existing in dynamics
                        if not _is_protected_dynamic(existing, validated_policy["minimum_protected_hit_rate"])
                    ),
                    key=lambda existing: _eviction_order_key(existing, validated_policy["eviction_policy"]),
                )[:required_evictions]
            )
            if len(victims) != required_evictions:
                plan = admission_plan(
                    outcome=AdmissionOutcome.REJECTED_CAPACITY,
                    candidate=self._state,
                    admitted_statement_id="",
                    evicted_statement_ids=(),
                    affected_epoch_namespaces=(),
                    residency_changed=False,
                    lifecycle_changed=False,
                )
                return plan
            for victim in victims:
                lifecycle_after_capacity_eviction(victim["lifecycle"])
                del artifacts[victim["statement_id"]]
            artifacts[artifact["statement_id"]] = artifact
            candidate = build_repository_state(
                artifacts.values(),
                self._state["state_generation"] + 1,
                self._state["index_state"]["state_generation"] + 1,
            )
            outcome = AdmissionOutcome.ADMITTED_WITH_EVICTION if victims else AdmissionOutcome.ADMITTED
            affected_namespaces = tuple(
                sorted({artifact["scope"]["namespace"], *(victim["scope"]["namespace"] for victim in victims)})
            )
            plan = admission_plan(
                outcome=outcome,
                candidate=candidate,
                admitted_statement_id=artifact["statement_id"],
                evicted_statement_ids=tuple(victim["statement_id"] for victim in victims),
                affected_epoch_namespaces=affected_namespaces,
                residency_changed=True,
                lifecycle_changed=False,
            )
            return plan

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
            if statement_id not in self._state["artifacts"]:
                raise ResourceNotFoundError(f"accepted response artifact not found: {statement_id}")
            artifact = self._state["artifacts"][statement_id]
            if artifact["generation"] != expected:
                raise ConflictError(
                    f"artifact generation conflict for {statement_id}: expected {expected}, current {artifact['generation']}"
                )
            if reason == RepositoryRemovalReason.CAPACITY_EVICTION:
                if artifact["tier"] != Tier.DYNAMIC:
                    raise ConflictError(f"capacity eviction cannot remove STATIC artifact: {statement_id}")
                lifecycle_after_capacity_eviction(artifact["lifecycle"])
            artifacts = dict(self._state["artifacts"])
            del artifacts[statement_id]
            candidate = build_repository_state(
                artifacts.values(),
                self._state["state_generation"] + 1,
                self._state["index_state"]["state_generation"] + 1,
            )
            return candidate

    def atomic_replace(self, candidate: RepositoryState, expected_state_generation: int) -> RepositoryState:
        validated_candidate = validate_repository_state(candidate)
        expected = _positive_int(expected_state_generation, "expected repository state_generation")
        report = check_repository_state(validated_candidate)
        if not report["consistent"]:
            raise ConflictError("candidate repository state failed equivalence checking")
        with self._lock:
            current_generation = self._state["state_generation"]
            if current_generation != expected:
                raise ConflictError(f"repository state generation conflict: expected {expected}, current {current_generation}")
            if validated_candidate["state_generation"] != expected + 1:
                raise ConflictError("candidate repository state_generation must advance by exactly one")
            current_index_generation = self._state["index_state"]["state_generation"]
            self._indexes.atomic_swap(validated_candidate["index_state"], current_index_generation)
            self._state = repository_state(
                state_generation=validated_candidate["state_generation"],
                artifacts=validated_candidate["artifacts"],
                statements=validated_candidate["statements"],
                index_state=self._indexes.snapshot(),
            )
            published = validate_repository_state(self._state)
            return published

    def recover_from_durable_state(self, durable: RepositoryState) -> RepositoryState:
        """Converge live derived state from an already-durable authority."""

        validated_durable = validate_repository_state(durable)
        report = check_repository_state(validated_durable)
        if not report["consistent"]:
            raise ConflictError("durable repository state failed equivalence checking")
        with self._lock:
            self._indexes = IndexOwner(validated_durable["index_state"]["projections"].values())
            self._state = repository_state(
                state_generation=validated_durable["state_generation"],
                artifacts=validated_durable["artifacts"],
                statements=validated_durable["statements"],
                index_state=self._indexes.snapshot(),
            )
            recovered = validate_repository_state(self._state)
            return recovered

    def exact_lookup(
        self,
        key: ScopedRetrievalKey,
        context: EligibilityContext,
        epoch_policy: EpochEligibilityPolicy,
    ) -> ContextualExactLookupResult:
        with self._lock:
            lookup = ContextualExactLookup(
                self._state["artifacts"],
                self._indexes,
                trusted_artifacts=True,
            ).exact_lookup(key, context, epoch_policy)
            if lookup["index_refreshed"]:
                self._state = repository_state(
                    state_generation=self._state["state_generation"] + 1,
                    artifacts=self._state["artifacts"],
                    statements=self._state["statements"],
                    index_state=self._indexes.snapshot(),
                )
            result = lookup
            return result
