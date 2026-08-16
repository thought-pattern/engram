"""Transport-neutral accepted-response mutation operations."""

from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from enum import StrEnum
from typing import TypedDict
from uuid import NAMESPACE_URL, uuid5

from engram.artifacts import (
    ArtifactProvenance,
    ArtifactStatistics,
    CachedResponseArtifact,
    LifecycleOperation,
    LifecycleState,
    require_lifecycle_transition,
)
from engram.constants import Tier
from engram.coordination import AtomicMutationCoordinator, MutationExecutionResult, validate_mutation_execution_result
from engram.errors import ConflictError, InvalidRequestError
from engram.identity import ScopeKey, build_retrieval_representation, build_standalone_identity, normalize_retrieval_key
from engram.mutations import (
    ArtifactGenerationChange,
    MutationOperation,
    MutationReceipt,
    MutationResultCode,
    ReceiptCompletionState,
    ReceiptLookup,
    ReceiptLookupOutcome,
    canonical_payload_signature,
)
from engram.repository import AdmissionOutcome, TierAdmissionPolicy, repository_state_with_artifact_updates


def utc_receipt_clock() -> str:
    """Return one canonical UTC receipt timestamp."""

    timestamp = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    return timestamp


def _mapping_copy(value: object, name: str) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise ConflictError(f"authoritative artifact {name} must be an object")
    copied = dict(value)
    return copied


class LifecycleMutationReason(StrEnum):
    """Closed caller-supplied reasons for terminal lifecycle mutations."""

    SOURCE_RETRACTED = "SOURCE_RETRACTED"
    POLICY = "POLICY"
    STALE = "STALE"
    USER_REQUEST = "USER_REQUEST"
    ADMINISTRATIVE = "ADMINISTRATIVE"


ResponseMutationResult = TypedDict(
    "ResponseMutationResult",
    {
        "receipt": MutationReceipt,
        "replayed": bool,
        "checkpoint_count": int,
        "durable": bool,
        "recovered": bool,
    },
)


def response_mutation_result(
    receipt: MutationReceipt,
    replayed: bool,
    checkpoint_count: int,
    durable: bool,
    recovered: bool,
) -> ResponseMutationResult:
    """Build one validated mutation-result dictionary."""
    if not isinstance(receipt, MutationReceipt):
        raise InvalidRequestError("response mutation receipt must be a MutationReceipt")
    if not all(isinstance(value, bool) for value in (replayed, durable, recovered)):
        raise InvalidRequestError("response mutation disposition flags must be booleans")
    if isinstance(checkpoint_count, bool) or not isinstance(checkpoint_count, int):
        raise InvalidRequestError("response mutation checkpoint_count must be an integer")
    if checkpoint_count < 0 or checkpoint_count > 1:
        raise InvalidRequestError("response mutation checkpoint_count must be zero or one")
    if replayed and checkpoint_count:
        raise InvalidRequestError("a replayed response mutation cannot perform a checkpoint")
    result: ResponseMutationResult = {
        "receipt": receipt,
        "replayed": replayed,
        "checkpoint_count": checkpoint_count,
        "durable": durable,
        "recovered": recovered,
    }
    return result


def response_mutation_result_from_execution(execution: MutationExecutionResult) -> ResponseMutationResult:
    """Build a mutation-result dictionary from one coordinated execution."""
    validated = validate_mutation_execution_result(execution)
    result = response_mutation_result(
        receipt=validated["receipt"],
        replayed=False,
        checkpoint_count=validated["checkpoint_count"],
        durable=validated["durable"],
        recovered=validated["recovered"],
    )
    return result


def response_mutation_result_to_dict(value: ResponseMutationResult) -> dict[str, object]:
    """Return the stable external dictionary for one validated mutation result."""
    validated = response_mutation_result(**value)
    receipt = validated["receipt"]
    result = {
        "request_id": receipt.request_id,
        "operation": receipt.operation.value,
        "result_code": receipt.result_code.value,
        "result": receipt.to_dict()["result"],
        "receipt_sequence": receipt.sequence,
        "replayed": validated["replayed"],
        "checkpoint_count": validated["checkpoint_count"],
        "durable": validated["durable"],
        "recovered": validated["recovered"],
    }
    return result


class AcceptedResponseService:
    """Apply accepted-response commands through one atomic coordinator."""

    def __init__(
        self,
        coordinator: AtomicMutationCoordinator,
        admission_policy: TierAdmissionPolicy,
        *,
        clock: Callable[[], str] = utc_receipt_clock,
    ) -> None:
        if not isinstance(coordinator, AtomicMutationCoordinator):
            raise InvalidRequestError("response coordinator must be an AtomicMutationCoordinator")
        if not isinstance(admission_policy, TierAdmissionPolicy):
            raise InvalidRequestError("response admission_policy must be a TierAdmissionPolicy")
        if not callable(clock):
            raise InvalidRequestError("response receipt clock must be callable")
        self._coordinator = coordinator
        self._admission_policy = admission_policy
        self._clock = clock

    @property
    def coordinator(self) -> AtomicMutationCoordinator:
        coordinator = self._coordinator
        return coordinator

    def _lookup(
        self,
        request_id: str,
        operation: MutationOperation,
        payload_signature: str,
    ) -> ReceiptLookup:
        lookup = self._coordinator.receipt_lookup(request_id, operation, payload_signature)
        return lookup

    @staticmethod
    def _validate_base_artifact(artifact: CachedResponseArtifact) -> None:
        if not isinstance(artifact, CachedResponseArtifact):
            raise InvalidRequestError("commit artifact must be a CachedResponseArtifact")
        if artifact.lifecycle != LifecycleState.ACTIVE:
            raise InvalidRequestError("base commit requires an ACTIVE artifact")
        if artifact.generation != 1:
            raise InvalidRequestError("base commit requires artifact generation 1")
        if normalize_retrieval_key(artifact.response) == "idk":
            raise InvalidRequestError("IDK is not a cacheable response")
        if "lifecycle_audit" in artifact.metadata:
            raise InvalidRequestError("base commit metadata must not use the reserved lifecycle_audit field")

    @staticmethod
    def _audit_text(value: object, name: str, maximum_bytes: int, *, allow_empty: bool) -> str:
        if not isinstance(value, str):
            raise InvalidRequestError(f"{name} must be a string")
        if not allow_empty and not value:
            raise InvalidRequestError(f"{name} must not be empty")
        if len(value.encode("utf-8")) > maximum_bytes:
            raise InvalidRequestError(f"{name} exceeds the UTF-8 limit of {maximum_bytes} bytes")
        if any(ord(character) < 32 or ord(character) == 127 for character in value):
            raise InvalidRequestError(f"{name} contains a control character")
        return value

    @staticmethod
    def _collision_owner_ids(artifact: CachedResponseArtifact, coordinator: AtomicMutationCoordinator) -> tuple[str, ...]:
        state = coordinator.snapshot().repository.index_state
        owners = set()
        for binding in artifact.retrieval.bindings(artifact.scope):
            owners.update(owner.statement_id for owner in state.retrieval_to_owners.get(binding.key, ()))
        owner_ids = tuple(sorted(owners))
        return owner_ids

    def commit_response(self, artifact: CachedResponseArtifact, request_id: str) -> ResponseMutationResult:
        """Create one accepted-response artifact without implicit replacement."""

        self._validate_base_artifact(artifact)
        payload_signature = canonical_payload_signature({"artifact": artifact.to_dict()})
        with self._coordinator.mutation():
            lookup = self._lookup(request_id, MutationOperation.COMMIT_RESPONSE, payload_signature)
            if lookup.outcome == ReceiptLookupOutcome.REPLAY:
                result = response_mutation_result(
                    receipt=lookup.receipt(),
                    replayed=True,
                    checkpoint_count=0,
                    durable=self._coordinator.checkpoint_configured,
                    recovered=False,
                )
                return result
            if lookup.outcome == ReceiptLookupOutcome.IN_PROGRESS:
                raise ConflictError(f"mutation request is in progress and requires recovery: {request_id}")
            if lookup.outcome == ReceiptLookupOutcome.EXPIRED:
                raise ConflictError(f"mutation request result expired and cannot be reapplied safely: {request_id}")
            if lookup.outcome == ReceiptLookupOutcome.CONFLICT:
                raise ConflictError(f"request_id is already associated with a different mutation: {request_id}")
            created_at = self._clock()
            result = self._commit_new_locked(artifact, request_id, payload_signature, created_at)
            return result

    def _commit_new_locked(
        self,
        artifact: CachedResponseArtifact,
        request_id: str,
        payload_signature: str,
        created_at: str,
    ) -> ResponseMutationResult:
        current = self._coordinator.snapshot().repository
        if artifact.statement_id in current.artifacts:
            raise ConflictError(f"artifact statement_id already exists: {artifact.statement_id}")
        collision_owner_ids = self._collision_owner_ids(artifact, self._coordinator)
        if collision_owner_ids:
            named = ", ".join(collision_owner_ids)
            raise ConflictError(f"retrieval key collision with existing statement IDs: {named}")

        plan = self._coordinator.repository.plan_admission(artifact, self._admission_policy)
        if plan.outcome == AdmissionOutcome.REJECTED_CAPACITY:
            result_code = MutationResultCode.REJECTED_CAPACITY
            affected_generations = ()
        else:
            result_code = (
                MutationResultCode.CREATED_WITH_EVICTION
                if plan.outcome == AdmissionOutcome.ADMITTED_WITH_EVICTION
                else MutationResultCode.CREATED
            )
            removed = tuple(
                ArtifactGenerationChange(
                    statement_id,
                    current.artifacts[statement_id].generation,
                    0,
                )
                for statement_id in plan.evicted_statement_ids
            )
            affected_generations = tuple(
                sorted((*removed, ArtifactGenerationChange(artifact.statement_id, 0, artifact.generation)))
            )
        result = {
            "statement_id": artifact.statement_id,
            "generation": artifact.generation,
            "admission_outcome": plan.outcome.value,
            "evicted_statement_ids": list(plan.evicted_statement_ids),
        }
        mutation_receipt = MutationReceipt(
            sequence=self._coordinator.next_receipt_sequence,
            request_id=request_id,
            operation=MutationOperation.COMMIT_RESPONSE,
            payload_signature=payload_signature,
            result_code=result_code,
            affected_generations=affected_generations,
            result=result,
            completion_state=ReceiptCompletionState.COMPLETED,
            created_at=created_at,
        )
        candidate = self._coordinator.build_candidate(
            plan.candidate,
            plan.affected_epoch_namespaces,
            mutation_receipt,
        )
        execution = self._coordinator.execute(candidate)
        result = response_mutation_result_from_execution(execution)
        return result

    def learn_response(
        self,
        request: str,
        response: str,
        request_id: str,
        caller_id: str,
        namespace: str,
        context_fingerprint: str,
        source_label: str,
        metadata: dict,
    ) -> ResponseMutationResult:
        """Construct the DYNAMIC ACTIVE compatibility artifact and base-commit it."""

        if not isinstance(metadata, dict):
            raise InvalidRequestError("learn response metadata must be an object")
        payload_signature = canonical_payload_signature(
            {
                "compatibility_operation": "LearnResponse",
                "request": request,
                "response": response,
                "caller_id": caller_id,
                "namespace": namespace,
                "context_fingerprint": context_fingerprint,
                "source_label": source_label,
                "metadata": metadata,
            }
        )
        with self._coordinator.mutation():
            lookup = self._lookup(request_id, MutationOperation.COMMIT_RESPONSE, payload_signature)
            if lookup.outcome == ReceiptLookupOutcome.REPLAY:
                result = response_mutation_result(
                    receipt=lookup.receipt(),
                    replayed=True,
                    checkpoint_count=0,
                    durable=self._coordinator.checkpoint_configured,
                    recovered=False,
                )
                return result
            if lookup.outcome == ReceiptLookupOutcome.IN_PROGRESS:
                raise ConflictError(f"mutation request is in progress and requires recovery: {request_id}")
            if lookup.outcome == ReceiptLookupOutcome.EXPIRED:
                raise ConflictError(f"mutation request result expired and cannot be reapplied safely: {request_id}")
            if lookup.outcome == ReceiptLookupOutcome.CONFLICT:
                raise ConflictError(f"request_id is already associated with a different learned response: {request_id}")
            accepted_at = self._clock()
            scope = ScopeKey(namespace=namespace, context_fingerprint=context_fingerprint)
            support_records = metadata.get("support", [])
            if not isinstance(support_records, list) or not all(isinstance(record, dict) for record in support_records):
                raise InvalidRequestError("learn response metadata support must be an array of objects")
            support_claim_ids = tuple(
                sorted(
                    {
                        self._audit_text(record.get("claim_id", ""), "learn response support claim_id", 256, allow_empty=False)
                        for record in support_records
                    }
                )
            )
            artifact = CachedResponseArtifact(
                statement_id=f"response_{uuid5(NAMESPACE_URL, f'engram:LearnResponse:{request_id}').hex}",
                generation=1,
                response=response,
                query_identity=build_standalone_identity(request, scope),
                retrieval=build_retrieval_representation(request),
                tier=Tier.DYNAMIC,
                lifecycle=LifecycleState.ACTIVE,
                scope=scope,
                support_claim_ids=support_claim_ids,
                valid_from="",
                valid_from_available=False,
                valid_until="",
                valid_until_available=False,
                knowledge_epoch=0,
                knowledge_epoch_available=False,
                superseded_by="",
                provenance=ArtifactProvenance(source_label, caller_id, accepted_at),
                statistics=ArtifactStatistics(),
                metadata=dict(metadata),
            )
            self._validate_base_artifact(artifact)
            result = self._commit_new_locked(artifact, request_id, payload_signature, accepted_at)
            return result

    def record_response_queries(
        self,
        statement_ids: tuple[str, ...],
        request_id: str,
    ) -> ResponseMutationResult:
        """Increment authoritative candidate-query statistics once per artifact."""

        if not isinstance(statement_ids, tuple) or not statement_ids:
            raise InvalidRequestError("response query statement_ids must be a non-empty tuple")
        if not all(isinstance(statement_id, str) and statement_id for statement_id in statement_ids):
            raise InvalidRequestError("response query statement_ids must contain non-empty strings")
        normalized_ids = tuple(sorted(set(statement_ids)))
        if normalized_ids != statement_ids:
            raise InvalidRequestError("response query statement_ids must be sorted and unique")
        payload_signature = canonical_payload_signature({"statement_ids": list(normalized_ids)})
        with self._coordinator.mutation():
            lookup = self._lookup(request_id, MutationOperation.RECORD_RESPONSE_QUERY, payload_signature)
            if lookup.outcome == ReceiptLookupOutcome.REPLAY:
                result = response_mutation_result(
                    receipt=lookup.receipt(),
                    replayed=True,
                    checkpoint_count=0,
                    durable=self._coordinator.checkpoint_configured,
                    recovered=False,
                )
                return result
            if lookup.outcome != ReceiptLookupOutcome.NEW:
                raise ConflictError(f"response query accounting request conflicts or requires recovery: {request_id}")
            updated_artifacts = []
            effects = []
            for statement_id in normalized_ids:
                current = self._coordinator.repository.get_artifact(statement_id)
                updated = current.to_dict()
                updated["generation"] = current.generation + 1
                statistics = _mapping_copy(updated["statistics"], "statistics")
                statistics["query_count"] = current.statistics.query_count + 1
                updated["statistics"] = statistics
                artifact = CachedResponseArtifact.from_dict(updated)
                updated_artifacts.append(artifact)
                effects.append(ArtifactGenerationChange(statement_id, current.generation, artifact.generation))
            repository_candidate = self._coordinator.repository.candidate_with_artifacts(tuple(updated_artifacts))
            recorded_at = self._clock()
            receipt = MutationReceipt(
                sequence=self._coordinator.next_receipt_sequence,
                request_id=request_id,
                operation=MutationOperation.RECORD_RESPONSE_QUERY,
                payload_signature=payload_signature,
                result_code=MutationResultCode.QUERY_RECORDED,
                affected_generations=tuple(effects),
                result={"statement_ids": list(normalized_ids), "recorded_at": recorded_at},
                completion_state=ReceiptCompletionState.COMPLETED,
                created_at=recorded_at,
            )
            candidate = self._coordinator.build_candidate(repository_candidate, (), receipt)
            execution = self._coordinator.execute(candidate)
            result = response_mutation_result_from_execution(execution)
            return result

    def record_response_hit(self, statement_id: str, request_id: str) -> ResponseMutationResult:
        """Increment one authoritative accepted-hit statistic and last-hit time."""

        normalized_id = self._audit_text(statement_id, "response hit statement_id", 256, allow_empty=False)
        payload_signature = canonical_payload_signature({"statement_id": normalized_id})
        with self._coordinator.mutation():
            lookup = self._lookup(request_id, MutationOperation.RECORD_RESPONSE_HIT, payload_signature)
            if lookup.outcome == ReceiptLookupOutcome.REPLAY:
                result = response_mutation_result(
                    receipt=lookup.receipt(),
                    replayed=True,
                    checkpoint_count=0,
                    durable=self._coordinator.checkpoint_configured,
                    recovered=False,
                )
                return result
            if lookup.outcome != ReceiptLookupOutcome.NEW:
                raise ConflictError(f"response hit accounting request conflicts or requires recovery: {request_id}")
            current = self._coordinator.repository.get_artifact(normalized_id)
            recorded_at = self._clock()
            updated = current.to_dict()
            updated["generation"] = current.generation + 1
            statistics = _mapping_copy(updated["statistics"], "statistics")
            statistics["hit_count"] = current.statistics.hit_count + 1
            statistics["last_hit"] = recorded_at
            statistics["last_hit_available"] = True
            updated["statistics"] = statistics
            artifact = CachedResponseArtifact.from_dict(updated)
            repository_candidate = self._coordinator.repository.candidate_with_artifacts((artifact,))
            receipt = MutationReceipt(
                sequence=self._coordinator.next_receipt_sequence,
                request_id=request_id,
                operation=MutationOperation.RECORD_RESPONSE_HIT,
                payload_signature=payload_signature,
                result_code=MutationResultCode.HIT_RECORDED,
                affected_generations=(ArtifactGenerationChange(normalized_id, current.generation, artifact.generation),),
                result={"statement_id": normalized_id, "recorded_at": recorded_at},
                completion_state=ReceiptCompletionState.COMPLETED,
                created_at=recorded_at,
            )
            candidate = self._coordinator.build_candidate(repository_candidate, (), receipt)
            execution = self._coordinator.execute(candidate)
            result = response_mutation_result_from_execution(execution)
            return result

    def finalize_resolution_accounting(
        self,
        statement_ids: tuple[str, ...],
        accepted_statement_id: str,
        request_id: str,
    ) -> ResponseMutationResult:
        """Atomically record unique candidacy and an optional accepted hit."""
        if not isinstance(statement_ids, tuple) or not statement_ids:
            raise InvalidRequestError("resolution statement_ids must be a non-empty tuple")
        if not all(isinstance(statement_id, str) and statement_id for statement_id in statement_ids):
            raise InvalidRequestError("resolution statement_ids must contain non-empty strings")
        normalized_ids = tuple(sorted(set(statement_ids)))
        if normalized_ids != statement_ids:
            raise InvalidRequestError("resolution statement_ids must be sorted and unique")
        normalized_accepted = self._audit_text(
            accepted_statement_id,
            "resolution accepted_statement_id",
            256,
            allow_empty=True,
        )
        if normalized_accepted and normalized_accepted not in normalized_ids:
            raise InvalidRequestError("resolution accepted_statement_id must be a candidate")
        payload_signature = canonical_payload_signature(
            {"statement_ids": list(normalized_ids), "accepted_statement_id": normalized_accepted}
        )
        with self._coordinator.mutation():
            lookup = self._lookup(request_id, MutationOperation.FINALIZE_RESOLUTION_ACCOUNTING, payload_signature)
            if lookup.outcome == ReceiptLookupOutcome.REPLAY:
                result = response_mutation_result(
                    receipt=lookup.receipt(),
                    replayed=True,
                    checkpoint_count=0,
                    durable=self._coordinator.checkpoint_configured,
                    recovered=False,
                )
                return result
            if lookup.outcome != ReceiptLookupOutcome.NEW:
                raise ConflictError(f"resolution accounting request conflicts or requires recovery: {request_id}")
            recorded_at = self._clock()
            updated_artifacts = []
            effects = []
            for statement_id in normalized_ids:
                current = self._coordinator.repository.get_artifact(statement_id)
                updated = current.to_dict()
                updated["generation"] = current.generation + 1
                statistics = _mapping_copy(updated["statistics"], "statistics")
                statistics["query_count"] = current.statistics.query_count + 1
                if statement_id == normalized_accepted:
                    statistics["hit_count"] = current.statistics.hit_count + 1
                    statistics["last_hit"] = recorded_at
                    statistics["last_hit_available"] = True
                updated["statistics"] = statistics
                artifact = CachedResponseArtifact.from_dict(updated)
                updated_artifacts.append(artifact)
                effects.append(ArtifactGenerationChange(statement_id, current.generation, artifact.generation))
            repository_candidate = self._coordinator.repository.candidate_with_artifacts(tuple(updated_artifacts))
            receipt = MutationReceipt(
                sequence=self._coordinator.next_receipt_sequence,
                request_id=request_id,
                operation=MutationOperation.FINALIZE_RESOLUTION_ACCOUNTING,
                payload_signature=payload_signature,
                result_code=MutationResultCode.RESOLUTION_ACCOUNTING_RECORDED,
                affected_generations=tuple(effects),
                result={
                    "statement_ids": list(normalized_ids),
                    "accepted_statement_id": normalized_accepted,
                    "recorded_at": recorded_at,
                },
                completion_state=ReceiptCompletionState.COMPLETED,
                created_at=recorded_at,
            )
            candidate = self._coordinator.build_candidate(repository_candidate, (), receipt)
            execution = self._coordinator.execute(candidate)
            result = response_mutation_result_from_execution(execution)
            return result

    def _transition_response(
        self,
        statement_id: str,
        expected_generation: int,
        reason: LifecycleMutationReason,
        caller_id: str,
        request_id: str,
        audit_detail: str,
        *,
        lifecycle_operation: LifecycleOperation,
        mutation_operation: MutationOperation,
        target: LifecycleState,
        result_code: MutationResultCode,
    ) -> ResponseMutationResult:
        normalized_statement_id = self._audit_text(statement_id, "lifecycle statement_id", 256, allow_empty=False)
        if isinstance(expected_generation, bool) or not isinstance(expected_generation, int) or expected_generation < 1:
            raise InvalidRequestError("expected_generation must be a positive integer")
        if not isinstance(reason, LifecycleMutationReason):
            raise InvalidRequestError("lifecycle reason must be a LifecycleMutationReason")
        normalized_caller_id = self._audit_text(caller_id, "lifecycle caller_id", 256, allow_empty=False)
        normalized_detail = self._audit_text(audit_detail, "lifecycle audit_detail", 1_024, allow_empty=True)
        payload_signature = canonical_payload_signature(
            {
                "statement_id": normalized_statement_id,
                "expected_generation": expected_generation,
                "reason": reason.value,
                "caller_id": normalized_caller_id,
                "audit_detail": normalized_detail,
            }
        )
        with self._coordinator.mutation():
            lookup = self._lookup(request_id, mutation_operation, payload_signature)
            if lookup.outcome == ReceiptLookupOutcome.REPLAY:
                result = response_mutation_result(
                    receipt=lookup.receipt(),
                    replayed=True,
                    checkpoint_count=0,
                    durable=self._coordinator.checkpoint_configured,
                    recovered=False,
                )
                return result
            if lookup.outcome == ReceiptLookupOutcome.IN_PROGRESS:
                raise ConflictError(f"mutation request is in progress and requires recovery: {request_id}")
            if lookup.outcome == ReceiptLookupOutcome.EXPIRED:
                raise ConflictError(f"mutation request result expired and cannot be reapplied safely: {request_id}")
            if lookup.outcome == ReceiptLookupOutcome.CONFLICT:
                raise ConflictError(f"request_id is already associated with a different mutation: {request_id}")

            current = self._coordinator.repository.get_artifact(normalized_statement_id)
            if current.generation != expected_generation:
                raise ConflictError(
                    f"artifact generation conflict for {normalized_statement_id}: "
                    f"expected {expected_generation}, current {current.generation}"
                )
            require_lifecycle_transition(current.lifecycle, target, lifecycle_operation)
            occurred_at = self._clock()
            updated = current.to_dict()
            updated["generation"] = current.generation + 1
            updated["lifecycle"] = target.value
            updated_metadata = _mapping_copy(updated["metadata"], "metadata")
            updated_metadata["lifecycle_audit"] = {
                "operation": mutation_operation.value,
                "reason": reason.value,
                "caller_id": normalized_caller_id,
                "request_id": request_id,
                "occurred_at": occurred_at,
                "detail": normalized_detail,
            }
            updated["metadata"] = updated_metadata
            transitioned = CachedResponseArtifact.from_dict(updated)
            repository_candidate = self._coordinator.repository.candidate_with_artifact(transitioned)
            result = {
                "statement_id": transitioned.statement_id,
                "before_generation": current.generation,
                "generation": transitioned.generation,
                "lifecycle": transitioned.lifecycle.value,
                "reason": reason.value,
                "caller_id": normalized_caller_id,
                "audit_detail": normalized_detail,
                "occurred_at": occurred_at,
            }
            mutation_receipt = MutationReceipt(
                sequence=self._coordinator.next_receipt_sequence,
                request_id=request_id,
                operation=mutation_operation,
                payload_signature=payload_signature,
                result_code=result_code,
                affected_generations=(
                    ArtifactGenerationChange(
                        transitioned.statement_id,
                        current.generation,
                        transitioned.generation,
                    ),
                ),
                result=result,
                completion_state=ReceiptCompletionState.COMPLETED,
                created_at=occurred_at,
            )
            candidate = self._coordinator.build_candidate(
                repository_candidate,
                (transitioned.scope.namespace,),
                mutation_receipt,
            )
            execution = self._coordinator.execute(candidate)
            result = response_mutation_result_from_execution(execution)
            return result

    def invalidate_response(
        self,
        statement_id: str,
        expected_generation: int,
        reason: LifecycleMutationReason,
        caller_id: str,
        request_id: str,
        audit_detail: str = "",
    ) -> ResponseMutationResult:
        """Invalidate one ACTIVE artifact with a durable audit record."""

        result = self._transition_response(
            statement_id,
            expected_generation,
            reason,
            caller_id,
            request_id,
            audit_detail,
            lifecycle_operation=LifecycleOperation.INVALIDATE,
            mutation_operation=MutationOperation.INVALIDATE_RESPONSE,
            target=LifecycleState.INVALIDATED,
            result_code=MutationResultCode.INVALIDATED,
        )
        return result

    def retire_response(
        self,
        statement_id: str,
        expected_generation: int,
        reason: LifecycleMutationReason,
        caller_id: str,
        request_id: str,
        audit_detail: str = "",
    ) -> ResponseMutationResult:
        """Retire one ACTIVE artifact with a durable audit record."""

        result = self._transition_response(
            statement_id,
            expected_generation,
            reason,
            caller_id,
            request_id,
            audit_detail,
            lifecycle_operation=LifecycleOperation.RETIRE,
            mutation_operation=MutationOperation.RETIRE_RESPONSE,
            target=LifecycleState.RETIRED,
            result_code=MutationResultCode.RETIRED,
        )
        return result

    def supersede_response(
        self,
        expected_statement_id: str,
        expected_generation: int,
        replacement: CachedResponseArtifact,
        reason: LifecycleMutationReason,
        caller_id: str,
        request_id: str,
        audit_detail: str = "",
    ) -> ResponseMutationResult:
        """Atomically supersede one expected ACTIVE artifact with one new artifact."""

        normalized_statement_id = self._audit_text(
            expected_statement_id,
            "supersession expected_statement_id",
            256,
            allow_empty=False,
        )
        if isinstance(expected_generation, bool) or not isinstance(expected_generation, int) or expected_generation < 1:
            raise InvalidRequestError("expected_generation must be a positive integer")
        self._validate_base_artifact(replacement)
        if replacement.statement_id == normalized_statement_id:
            raise InvalidRequestError("supersession replacement must have a new statement_id")
        if not isinstance(reason, LifecycleMutationReason):
            raise InvalidRequestError("supersession reason must be a LifecycleMutationReason")
        normalized_caller_id = self._audit_text(caller_id, "supersession caller_id", 256, allow_empty=False)
        normalized_detail = self._audit_text(audit_detail, "supersession audit_detail", 1_024, allow_empty=True)
        payload_signature = canonical_payload_signature(
            {
                "expected_statement_id": normalized_statement_id,
                "expected_generation": expected_generation,
                "replacement": replacement.to_dict(),
                "reason": reason.value,
                "caller_id": normalized_caller_id,
                "audit_detail": normalized_detail,
            }
        )
        with self._coordinator.mutation():
            lookup = self._lookup(request_id, MutationOperation.SUPERSEDE_RESPONSE, payload_signature)
            if lookup.outcome == ReceiptLookupOutcome.REPLAY:
                result = response_mutation_result(
                    receipt=lookup.receipt(),
                    replayed=True,
                    checkpoint_count=0,
                    durable=self._coordinator.checkpoint_configured,
                    recovered=False,
                )
                return result
            if lookup.outcome == ReceiptLookupOutcome.IN_PROGRESS:
                raise ConflictError(f"mutation request is in progress and requires recovery: {request_id}")
            if lookup.outcome == ReceiptLookupOutcome.EXPIRED:
                raise ConflictError(f"mutation request result expired and cannot be reapplied safely: {request_id}")
            if lookup.outcome == ReceiptLookupOutcome.CONFLICT:
                raise ConflictError(f"request_id is already associated with a different mutation: {request_id}")

            before = self._coordinator.snapshot().repository
            current = self._coordinator.repository.get_artifact(normalized_statement_id)
            if current.generation != expected_generation:
                raise ConflictError(
                    f"artifact generation conflict for {normalized_statement_id}: "
                    f"expected {expected_generation}, current {current.generation}"
                )
            require_lifecycle_transition(current.lifecycle, LifecycleState.SUPERSEDED, LifecycleOperation.SUPERSEDE)
            if replacement.statement_id in before.artifacts:
                raise ConflictError(f"artifact statement_id already exists: {replacement.statement_id}")
            conflicting_owners = set()
            for binding in replacement.retrieval.bindings(replacement.scope):
                for owner in before.index_state.retrieval_to_owners.get(binding.key, ()):
                    if owner.statement_id != normalized_statement_id:
                        conflicting_owners.add(owner.statement_id)
            if conflicting_owners:
                named = ", ".join(sorted(conflicting_owners))
                raise ConflictError(f"replacement retrieval key collision with existing statement IDs: {named}")

            plan = self._coordinator.repository.plan_admission(replacement, self._admission_policy)
            lineage_evicted = normalized_statement_id in plan.evicted_statement_ids
            if plan.outcome == AdmissionOutcome.REJECTED_CAPACITY or lineage_evicted:
                result = {
                    "expected_statement_id": normalized_statement_id,
                    "expected_generation": expected_generation,
                    "replacement_statement_id": replacement.statement_id,
                    "admission_outcome": AdmissionOutcome.REJECTED_CAPACITY.value,
                    "evicted_statement_ids": [],
                }
                rejected_receipt = MutationReceipt(
                    sequence=self._coordinator.next_receipt_sequence,
                    request_id=request_id,
                    operation=MutationOperation.SUPERSEDE_RESPONSE,
                    payload_signature=payload_signature,
                    result_code=MutationResultCode.REJECTED_CAPACITY,
                    affected_generations=(),
                    result=result,
                    completion_state=ReceiptCompletionState.COMPLETED,
                    created_at=self._clock(),
                )
                candidate = self._coordinator.build_candidate(before, (), rejected_receipt)
                execution = self._coordinator.execute(candidate)
                result = response_mutation_result_from_execution(execution)
                return result

            occurred_at = self._clock()
            updated_current = current.to_dict()
            updated_current["generation"] = current.generation + 1
            updated_current["lifecycle"] = LifecycleState.SUPERSEDED.value
            updated_current["superseded_by"] = replacement.statement_id
            updated_metadata = _mapping_copy(updated_current["metadata"], "metadata")
            updated_metadata["lifecycle_audit"] = {
                "operation": MutationOperation.SUPERSEDE_RESPONSE.value,
                "reason": reason.value,
                "caller_id": normalized_caller_id,
                "request_id": request_id,
                "occurred_at": occurred_at,
                "detail": normalized_detail,
                "replacement_statement_id": replacement.statement_id,
            }
            updated_current["metadata"] = updated_metadata
            superseded = CachedResponseArtifact.from_dict(updated_current)
            repository_candidate = repository_state_with_artifact_updates(plan.candidate, (superseded,))
            removed = tuple(
                ArtifactGenerationChange(statement_id, before.artifacts[statement_id].generation, 0)
                for statement_id in plan.evicted_statement_ids
            )
            affected_generations = tuple(
                sorted(
                    (
                        *removed,
                        ArtifactGenerationChange(superseded.statement_id, current.generation, superseded.generation),
                        ArtifactGenerationChange(replacement.statement_id, 0, replacement.generation),
                    )
                )
            )
            affected_namespaces = tuple(sorted({*plan.affected_epoch_namespaces, superseded.scope.namespace}))
            result = {
                "superseded_statement_id": superseded.statement_id,
                "superseded_generation": superseded.generation,
                "replacement_statement_id": replacement.statement_id,
                "replacement_generation": replacement.generation,
                "admission_outcome": plan.outcome.value,
                "evicted_statement_ids": list(plan.evicted_statement_ids),
                "reason": reason.value,
                "caller_id": normalized_caller_id,
                "audit_detail": normalized_detail,
                "occurred_at": occurred_at,
            }
            mutation_receipt = MutationReceipt(
                sequence=self._coordinator.next_receipt_sequence,
                request_id=request_id,
                operation=MutationOperation.SUPERSEDE_RESPONSE,
                payload_signature=payload_signature,
                result_code=MutationResultCode.SUPERSEDED,
                affected_generations=affected_generations,
                result=result,
                completion_state=ReceiptCompletionState.COMPLETED,
                created_at=occurred_at,
            )
            candidate = self._coordinator.build_candidate(
                repository_candidate,
                affected_namespaces,
                mutation_receipt,
            )
            execution = self._coordinator.execute(candidate)
            result = response_mutation_result_from_execution(execution)
            return result
