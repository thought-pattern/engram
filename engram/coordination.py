"""Checkpoint-before-publication coordinator for Section 3 mutations."""

import json
import threading
from collections.abc import Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Protocol

from engram.eligibility import EpochChangeReason, NamespaceEpochState
from engram.errors import ConflictError, EngramCoreError, InvalidRequestError
from engram.mutations import (
    ArtifactGenerationChange,
    MutationOperation,
    MutationReceipt,
    MutationReceiptLedger,
    ReceiptLookup,
)
from engram.repository import ArtifactRepository, RepositoryState, check_repository_state


class CheckpointFailureKind(StrEnum):
    """Whether a failed checkpoint is known not to have committed."""

    DEFINITE = "DEFINITE"
    INDETERMINATE = "INDETERMINATE"


class CheckpointFailureError(Exception):
    """Storage callback failure with an explicit durable-outcome classification."""

    def __init__(self, kind: CheckpointFailureKind, detail: str) -> None:
        if not isinstance(kind, CheckpointFailureKind):
            raise InvalidRequestError("checkpoint failure kind must be a CheckpointFailureKind")
        if not isinstance(detail, str) or not detail:
            raise InvalidRequestError("checkpoint failure detail must be a non-empty string")
        self.kind = kind
        self.detail = detail
        super().__init__(detail)


class MutationCoordinationError(EngramCoreError):
    """Mutation failed before or after its single checkpoint attempt."""

    def __init__(
        self,
        detail: str,
        *,
        checkpoint_count: int,
        durable_candidate: bool,
        live_state_changed: bool,
        recovery_required: bool,
    ) -> None:
        self.detail = detail
        self.checkpoint_count = checkpoint_count
        self.durable_candidate = durable_candidate
        self.live_state_changed = live_state_changed
        self.recovery_required = recovery_required
        super().__init__(detail)


def _freeze(value: object) -> object:
    if isinstance(value, Mapping):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, list | tuple):
        return tuple(_freeze(item) for item in value)
    return value


def _thaw(value: object) -> object:
    if isinstance(value, Mapping):
        return {key: _thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw(item) for item in value]
    return value


def _canonical_mapping(value: object, name: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise InvalidRequestError(f"{name} must be an object")
    try:
        encoded = json.dumps(_thaw(value), ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        decoded = json.loads(encoded)
    except (TypeError, ValueError) as error:
        raise InvalidRequestError(f"{name} must contain concrete JSON values") from error
    if not isinstance(decoded, Mapping):
        raise InvalidRequestError(f"{name} must be an object")
    frozen = _freeze(decoded)
    if not isinstance(frozen, Mapping):
        raise InvalidRequestError(f"{name} must be an object")
    return frozen


def _thaw_mapping(value: Mapping[str, object]) -> Mapping[str, object]:
    thawed = _thaw(value)
    if not isinstance(thawed, Mapping):
        raise InvalidRequestError("coordinated state mapping could not be thawed")
    return thawed


def _state_signature(
    repository: RepositoryState,
    namespace_epochs: Mapping[str, object],
    mutation_receipts: Mapping[str, object],
) -> str:
    return json.dumps(
        {
            "repository_state_generation": repository.state_generation,
            "artifacts": [repository.artifacts[statement_id].to_dict() for statement_id in sorted(repository.artifacts)],
            "namespace_epochs": _thaw(namespace_epochs),
            "mutation_receipts": _thaw(mutation_receipts),
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _durable_state_signature(
    repository: RepositoryState,
    namespace_epochs: Mapping[str, object],
    mutation_receipts: Mapping[str, object],
) -> str:
    return json.dumps(
        {
            "artifacts": [repository.artifacts[statement_id].to_dict() for statement_id in sorted(repository.artifacts)],
            "namespace_epochs": _thaw(namespace_epochs),
            "mutation_receipts": _thaw(mutation_receipts),
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


@dataclass(frozen=True, slots=True)
class CoordinatedResponseState:
    """Complete authoritative response state passed to checkpoint/recovery."""

    repository: RepositoryState
    namespace_epochs: Mapping[str, object]
    mutation_receipts: Mapping[str, object]

    def __post_init__(self) -> None:
        if not isinstance(self.repository, RepositoryState):
            raise InvalidRequestError("coordinated repository must be a RepositoryState")
        if not check_repository_state(self.repository).consistent:
            raise ConflictError("coordinated repository state is inconsistent")
        epochs = _canonical_mapping(self.namespace_epochs, "coordinated namespace_epochs")
        receipts = _canonical_mapping(self.mutation_receipts, "coordinated mutation_receipts")
        NamespaceEpochState.from_snapshot(epochs)
        MutationReceiptLedger.from_snapshot(_thaw_mapping(receipts))
        object.__setattr__(self, "namespace_epochs", epochs)
        object.__setattr__(self, "mutation_receipts", receipts)

    def signature(self) -> str:
        return _state_signature(self.repository, self.namespace_epochs, self.mutation_receipts)

    def durable_signature(self) -> str:
        """Identify persisted authority while excluding transient owner generations."""

        return _durable_state_signature(self.repository, self.namespace_epochs, self.mutation_receipts)

    def response_state_dict(self, quarantine: tuple[Mapping[str, object], ...] = ()) -> dict[str, object]:
        if not isinstance(quarantine, tuple) or not all(isinstance(record, Mapping) for record in quarantine):
            raise InvalidRequestError("coordinated quarantine must be a tuple of objects")
        return {
            "schema_version": 1,
            "artifacts": [self.repository.artifacts[statement_id].to_dict() for statement_id in sorted(self.repository.artifacts)],
            "namespace_epochs": _thaw(self.namespace_epochs),
            "mutation_receipts": _thaw(self.mutation_receipts),
            "quarantine": [_thaw(record) for record in quarantine],
        }


@dataclass(frozen=True, slots=True)
class CoordinatedMutationCandidate:
    """Validated before/after state and the receipt committed with it."""

    before: CoordinatedResponseState
    after: CoordinatedResponseState
    receipt: MutationReceipt
    affected_epoch_namespaces: tuple[str, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.before, CoordinatedResponseState) or not isinstance(self.after, CoordinatedResponseState):
            raise InvalidRequestError("coordinated candidate states must be CoordinatedResponseState values")
        if not isinstance(self.receipt, MutationReceipt):
            raise InvalidRequestError("coordinated candidate receipt must be a MutationReceipt")
        if not isinstance(self.affected_epoch_namespaces, tuple) or not all(
            isinstance(namespace, str) for namespace in self.affected_epoch_namespaces
        ):
            raise InvalidRequestError("affected_epoch_namespaces must be a tuple of strings")
        if tuple(sorted(set(self.affected_epoch_namespaces))) != self.affected_epoch_namespaces:
            raise InvalidRequestError("affected_epoch_namespaces must be sorted and unique")


@dataclass(frozen=True, slots=True)
class MutationExecutionResult:
    """Successful live publication result."""

    receipt: MutationReceipt
    checkpoint_count: int
    durable: bool
    published: bool
    recovered: bool

    def to_dict(self) -> dict[str, object]:
        return {
            "receipt": self.receipt.to_dict(),
            "checkpoint_count": self.checkpoint_count,
            "durable": self.durable,
            "published": self.published,
            "recovered": self.recovered,
        }


class CheckpointCallback(Protocol):
    def __call__(self, state: CoordinatedResponseState, /) -> None: ...


class RecoveryLoader(Protocol):
    def __call__(self) -> CoordinatedResponseState: ...


class PublicationHook(Protocol):
    def __call__(self, state: CoordinatedResponseState, /) -> None: ...


def _no_checkpoint(_state: CoordinatedResponseState) -> None:
    return


def _no_recovery() -> CoordinatedResponseState:
    raise MutationCoordinationError(
        "no durable recovery loader is configured",
        checkpoint_count=1,
        durable_candidate=False,
        live_state_changed=False,
        recovery_required=True,
    )


def _no_publication_hook(_state: CoordinatedResponseState) -> None:
    return


def _artifact_changes(before: RepositoryState, after: RepositoryState) -> tuple[ArtifactGenerationChange, ...]:
    changes = []
    for statement_id in sorted(set(before.artifacts) | set(after.artifacts)):
        before_generation = before.artifacts[statement_id].generation if statement_id in before.artifacts else 0
        after_generation = after.artifacts[statement_id].generation if statement_id in after.artifacts else 0
        if (
            statement_id not in before.artifacts
            or statement_id not in after.artifacts
            or before.artifacts[statement_id] != after.artifacts[statement_id]
        ):
            changes.append(ArtifactGenerationChange(statement_id, before_generation, after_generation))
    return tuple(changes)


def _changed_namespaces(before: RepositoryState, after: RepositoryState) -> tuple[str, ...]:
    namespaces = set()
    for change in _artifact_changes(before, after):
        before_artifact = before.artifacts.get(change.statement_id, ())
        after_artifact = after.artifacts.get(change.statement_id, ())
        affects_epoch = not before_artifact or not after_artifact
        if before_artifact and after_artifact:
            before_authority = before_artifact.to_dict()
            after_authority = after_artifact.to_dict()
            for transient_field in ("generation", "statistics"):
                del before_authority[transient_field]
                del after_authority[transient_field]
            affects_epoch = before_authority != after_authority
        if affects_epoch and before_artifact:
            namespaces.add(before_artifact.scope.namespace)
        if affects_epoch and after_artifact:
            namespaces.add(after_artifact.scope.namespace)
    return tuple(sorted(namespaces))


class AtomicMutationCoordinator:
    """Own response repository, epochs, receipts, checkpoint, and publication."""

    def __init__(
        self,
        repository: ArtifactRepository,
        namespace_epochs: NamespaceEpochState,
        mutation_receipts: MutationReceiptLedger,
        *,
        checkpoint_configured: bool = False,
        checkpoint: CheckpointCallback = _no_checkpoint,
        recovery_loader: RecoveryLoader = _no_recovery,
        publication_hook: PublicationHook = _no_publication_hook,
    ) -> None:
        if not isinstance(repository, ArtifactRepository):
            raise InvalidRequestError("coordinator repository must be an ArtifactRepository")
        if not isinstance(namespace_epochs, NamespaceEpochState):
            raise InvalidRequestError("coordinator namespace_epochs must be a NamespaceEpochState")
        if not isinstance(mutation_receipts, MutationReceiptLedger):
            raise InvalidRequestError("coordinator mutation_receipts must be a MutationReceiptLedger")
        if not isinstance(checkpoint_configured, bool):
            raise InvalidRequestError("checkpoint_configured must be a boolean")
        if not callable(checkpoint) or not callable(recovery_loader) or not callable(publication_hook):
            raise InvalidRequestError("coordinator callbacks must be callable")
        self._lock = threading.RLock()
        self._repository = repository
        self._namespace_epochs = namespace_epochs
        self._mutation_receipts = mutation_receipts
        self._checkpoint_configured = checkpoint_configured
        self._checkpoint = checkpoint
        self._recovery_loader = recovery_loader
        self._publication_hook = publication_hook

    @property
    def repository(self) -> ArtifactRepository:
        return self._repository

    @property
    def checkpoint_configured(self) -> bool:
        return self._checkpoint_configured

    @contextmanager
    def mutation(self):
        """Serialize receipt lookup, candidate planning, and execution."""

        with self._lock, self._repository.coordinated_mutation():
            yield

    @property
    def next_receipt_sequence(self) -> int:
        with self._lock:
            return self._mutation_receipts.next_sequence

    def receipt_lookup(
        self,
        request_id: str,
        operation: MutationOperation,
        payload_signature: str,
    ) -> ReceiptLookup:
        with self._lock:
            return self._mutation_receipts.lookup(request_id, operation, payload_signature)

    def snapshot(self) -> CoordinatedResponseState:
        with self._lock:
            return CoordinatedResponseState(
                repository=self._repository.snapshot(),
                namespace_epochs=self._namespace_epochs.snapshot(),
                mutation_receipts=self._mutation_receipts.snapshot(),
            )

    def build_candidate(
        self,
        repository_candidate: RepositoryState,
        affected_epoch_namespaces: tuple[str, ...],
        receipt: MutationReceipt,
    ) -> CoordinatedMutationCandidate:
        if not isinstance(repository_candidate, RepositoryState):
            raise InvalidRequestError("repository_candidate must be a RepositoryState")
        if not isinstance(receipt, MutationReceipt):
            raise InvalidRequestError("receipt must be a MutationReceipt")
        if not isinstance(affected_epoch_namespaces, tuple) or not all(
            isinstance(namespace, str) for namespace in affected_epoch_namespaces
        ):
            raise InvalidRequestError("affected_epoch_namespaces must be a tuple of strings")
        normalized_namespaces = tuple(sorted(set(affected_epoch_namespaces)))
        if normalized_namespaces != affected_epoch_namespaces:
            raise InvalidRequestError("affected_epoch_namespaces must be sorted and unique")
        with self._lock, self._repository.coordinated_mutation():
            before = self.snapshot()
            repository_changed = before.repository.artifacts != repository_candidate.artifacts
            if repository_changed:
                if repository_candidate.state_generation != before.repository.state_generation + 1:
                    raise ConflictError("changed repository candidate must advance state_generation by one")
            elif repository_candidate.state_generation != before.repository.state_generation:
                raise ConflictError("unchanged repository candidate must retain state_generation")
            changes = _artifact_changes(before.repository, repository_candidate)
            if changes != receipt.affected_generations:
                raise ConflictError("mutation receipt affected generations do not match repository changes")
            if _changed_namespaces(before.repository, repository_candidate) != normalized_namespaces:
                raise ConflictError("affected epoch namespaces do not match repository changes")

            epochs = NamespaceEpochState.from_snapshot(before.namespace_epochs)
            for namespace in normalized_namespaces:
                current = epochs.get(namespace)
                if not current.knowledge_epoch_available:
                    current = epochs.initialize(namespace, 0)
                epochs.increment(namespace, current.knowledge_epoch, EpochChangeReason.ACCEPTED_ARTIFACT_ELIGIBILITY)
            receipts = MutationReceiptLedger.from_snapshot(_thaw_mapping(before.mutation_receipts))
            receipts.record(receipt)
            after = CoordinatedResponseState(
                repository=repository_candidate,
                namespace_epochs=epochs.snapshot(),
                mutation_receipts=receipts.snapshot(),
            )
            return CoordinatedMutationCandidate(before, after, receipt, normalized_namespaces)

    def _publish(self, candidate: CoordinatedMutationCandidate) -> bool:
        current = self._repository.snapshot()
        repository_changed = current.artifacts != candidate.after.repository.artifacts
        try:
            if repository_changed:
                self._repository.atomic_replace(candidate.after.repository, current.state_generation)
            self._publication_hook(candidate.after)
            self._namespace_epochs.replace_from_snapshot(candidate.after.namespace_epochs)
            self._mutation_receipts.replace_from_snapshot(_thaw_mapping(candidate.after.mutation_receipts))
            return False
        except Exception as publication_error:
            try:
                self._repository.recover_from_durable_state(candidate.after.repository)
                self._namespace_epochs.replace_from_snapshot(candidate.after.namespace_epochs)
                self._mutation_receipts.replace_from_snapshot(_thaw_mapping(candidate.after.mutation_receipts))
                return True
            except Exception as recovery_error:
                live = self._repository.snapshot()
                live_state_changed = live.state_generation != current.state_generation or live.artifacts != current.artifacts
                raise MutationCoordinationError(
                    f"durable mutation publication failed and live recovery failed: {publication_error}; {recovery_error}",
                    checkpoint_count=1 if self._checkpoint_configured else 0,
                    durable_candidate=self._checkpoint_configured,
                    live_state_changed=live_state_changed,
                    recovery_required=True,
                ) from recovery_error

    def execute(self, candidate: CoordinatedMutationCandidate) -> MutationExecutionResult:
        if not isinstance(candidate, CoordinatedMutationCandidate):
            raise InvalidRequestError("candidate must be a CoordinatedMutationCandidate")
        with self._lock, self._repository.coordinated_mutation():
            current = self.snapshot()
            if current.signature() != candidate.before.signature():
                raise ConflictError("coordinated mutation candidate is stale")
            checkpoint_count = 0
            recovered_checkpoint = False
            if self._checkpoint_configured:
                checkpoint_count = 1
                try:
                    self._checkpoint(candidate.after)
                except CheckpointFailureError as error:
                    if error.kind == CheckpointFailureKind.DEFINITE:
                        raise MutationCoordinationError(
                            f"checkpoint definitely failed before publication: {error.detail}",
                            checkpoint_count=1,
                            durable_candidate=False,
                            live_state_changed=False,
                            recovery_required=False,
                        ) from error
                    try:
                        recovered = self._recovery_loader()
                    except Exception as recovery_error:
                        raise MutationCoordinationError(
                            f"indeterminate checkpoint could not be recovered: {error.detail}; {recovery_error}",
                            checkpoint_count=1,
                            durable_candidate=False,
                            live_state_changed=False,
                            recovery_required=True,
                        ) from recovery_error
                    if not isinstance(recovered, CoordinatedResponseState):
                        raise MutationCoordinationError(
                            "indeterminate checkpoint recovery returned an invalid state",
                            checkpoint_count=1,
                            durable_candidate=False,
                            live_state_changed=False,
                            recovery_required=True,
                        ) from error
                    if recovered.durable_signature() == candidate.before.durable_signature():
                        raise MutationCoordinationError(
                            "indeterminate checkpoint recovered the unchanged pre-mutation state",
                            checkpoint_count=1,
                            durable_candidate=False,
                            live_state_changed=False,
                            recovery_required=False,
                        ) from error
                    if recovered.durable_signature() != candidate.after.durable_signature():
                        raise MutationCoordinationError(
                            "indeterminate checkpoint recovered divergent durable state",
                            checkpoint_count=1,
                            durable_candidate=False,
                            live_state_changed=False,
                            recovery_required=True,
                        ) from error
                    recovered_checkpoint = True
                except Exception as error:
                    raise MutationCoordinationError(
                        f"checkpoint definitely failed before publication: {error}",
                        checkpoint_count=1,
                        durable_candidate=False,
                        live_state_changed=False,
                        recovery_required=False,
                    ) from error
            recovered_publication = self._publish(candidate)
            return MutationExecutionResult(
                receipt=candidate.receipt,
                checkpoint_count=checkpoint_count,
                durable=self._checkpoint_configured,
                published=True,
                recovered=recovered_checkpoint or recovered_publication,
            )
