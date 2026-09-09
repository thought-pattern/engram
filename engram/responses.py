"""Transport-neutral accepted-response mutation operations."""

from datetime import UTC, datetime
from uuid import NAMESPACE_URL, uuid5

from engram.artifacts import (
    LifecycleOperation,
    LifecycleState,
    artifact_provenance,
    artifact_statistics,
    cached_response_artifact,
    cached_response_artifact_from_dict,
    cached_response_artifact_to_dict,
    require_lifecycle_transition,
    validate_cached_response_artifact,
)
from engram.constants import LifecycleMutationReason, Tier
from engram.coordination import AtomicMutationCoordinator, validate_mutation_execution_result
from engram.errors import ConflictError, InvalidRequestError
from engram.identity import (
    build_retrieval_representation,
    build_standalone_identity,
    normalize_retrieval_key,
    retrieval_representation_bindings,
    scope_key,
)
from engram.mutations import (
    MutationOperation,
    MutationResultCode,
    ReceiptCompletionState,
    ReceiptLookupOutcome,
    artifact_generation_change,
    canonical_payload_signature,
    mutation_receipt,
    mutation_receipt_to_dict,
    normalize_artifact_generation_changes,
    receipt_lookup_receipt,
    validate_mutation_receipt,
)
from engram.repository import AdmissionOutcome, repository_state_with_artifact_updates, validate_tier_admission_policy
from engram.support import validate_support_reference


def utc_receipt_clock() -> str:
    """Return one canonical UTC receipt timestamp."""

    timestamp = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    return timestamp


def mapping_copy(value: object, name: str) -> dict:
    if not isinstance(value, dict):
        raise ConflictError(f"authoritative artifact {name} must be an object")
    copied = dict(value)
    return copied


def response_mutation_result(
    receipt: dict,
    replayed: bool,
) -> dict:
    """Build one validated mutation-result dictionary."""
    validated_receipt = validate_mutation_receipt(receipt)
    if not isinstance(replayed, bool):
        raise InvalidRequestError("response mutation replayed must be a boolean")
    result: dict = {
        "receipt": validated_receipt,
        "replayed": replayed,
    }
    return result


def response_mutation_result_from_execution(execution: dict) -> dict:
    """Build a mutation-result dictionary from one coordinated execution."""
    validated = validate_mutation_execution_result(execution)
    result = response_mutation_result(
        receipt=validated["receipt"],
        replayed=False,
    )
    return result


def response_mutation_result_to_dict(value: dict) -> dict:
    """Return the stable external dictionary for one validated mutation result."""
    validated = response_mutation_result(**value)
    receipt = validated["receipt"]
    receipt_value = mutation_receipt_to_dict(receipt)
    result = {
        "request_id": receipt["request_id"],
        "operation": receipt["operation"].value,
        "result_code": receipt["result_code"].value,
        "result": receipt_value["result"],
        "receipt_sequence": receipt["sequence"],
        "replayed": validated["replayed"],
    }
    return result


def validate_base_response_artifact(artifact: dict) -> dict:
    """Validate the stricter invariants for a new accepted response."""
    try:
        validated_artifact = validate_cached_response_artifact(artifact)
    except InvalidRequestError as error:
        raise InvalidRequestError("commit artifact must be a CachedResponseArtifact") from error
    if validated_artifact["lifecycle"] != LifecycleState.ACTIVE:
        raise InvalidRequestError("base commit requires an ACTIVE artifact")
    if validated_artifact["generation"] != 1:
        raise InvalidRequestError("base commit requires artifact generation 1")
    if normalize_retrieval_key(validated_artifact["response"]) == "idk":
        raise InvalidRequestError("IDK is not a cacheable response")
    if "lifecycle_audit" in validated_artifact["metadata"]:
        raise InvalidRequestError("base commit metadata must not use the reserved lifecycle_audit field")
    return validated_artifact


def response_audit_text(value: object, name: str, maximum_bytes: int, *, allow_empty: bool) -> str:
    """Validate one bounded response-mutation audit string."""
    if not isinstance(value, str):
        raise InvalidRequestError(f"{name} must be a string")
    if not allow_empty and not value:
        raise InvalidRequestError(f"{name} must not be empty")
    if len(value.encode("utf-8")) > maximum_bytes:
        raise InvalidRequestError(f"{name} exceeds the UTF-8 limit of {maximum_bytes} bytes")
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise InvalidRequestError(f"{name} contains a control character")
    result = value
    return result


def response_collision_owner_ids(
    artifact: dict,
    coordinator: AtomicMutationCoordinator,
) -> tuple[str, ...]:
    """Return current owners that collide with an artifact's retrieval keys."""
    artifacts = coordinator.snapshot()["repository"]["artifacts"]
    target_keys = tuple(
        binding.get("key")
        for binding in retrieval_representation_bindings(artifact.get("retrieval", {}), artifact.get("scope", {}))
    )
    owners = {
        statement_id
        for statement_id, current in artifacts.items()
        if any(
            any(binding.get("key") == target_key for target_key in target_keys)
            for binding in retrieval_representation_bindings(current["retrieval"], current["scope"])
        )
    }
    result = tuple(sorted(owners))
    return result


class AcceptedResponseService:
    """Apply accepted-response commands through one atomic coordinator."""

    def __init__(
        self,
        coordinator: AtomicMutationCoordinator,
        admission_policy: dict,
        *,
        clock: object = utc_receipt_clock,
    ) -> None:
        if not isinstance(coordinator, AtomicMutationCoordinator):
            raise InvalidRequestError("response coordinator must be an AtomicMutationCoordinator")
        validated_policy = validate_tier_admission_policy(admission_policy)
        if not callable(clock):
            raise InvalidRequestError("response receipt clock must be callable")
        self.internal_coordinator = coordinator
        self.internal_admission_policy = validated_policy
        self.internal_clock = clock

    def receipt_time(self) -> str:
        """Read and validate the configured mutation clock boundary."""
        value = self.internal_clock()
        if not isinstance(value, str):
            raise InvalidRequestError("response receipt clock must return a timestamp string")
        return value

    @property
    def coordinator(self) -> AtomicMutationCoordinator:
        coordinator = self.internal_coordinator
        return coordinator

    def internal_lookup(
        self,
        request_id: str,
        operation: MutationOperation,
        payload_signature: str,
    ) -> dict:
        lookup = self.internal_coordinator.receipt_lookup(request_id, operation, payload_signature)
        return lookup

    def commit_response(self, artifact: dict, request_id: str) -> dict:
        """Create one accepted-response artifact without implicit replacement."""

        artifact = validate_base_response_artifact(artifact)
        payload_signature = canonical_payload_signature({"artifact": cached_response_artifact_to_dict(artifact)})
        with self.internal_coordinator.mutation():
            lookup = self.internal_lookup(request_id, MutationOperation.COMMIT_RESPONSE, payload_signature)
            if lookup["outcome"] == ReceiptLookupOutcome.REPLAY:
                result = response_mutation_result(
                    receipt=receipt_lookup_receipt(lookup),
                    replayed=True,
                )
                return result
            if lookup["outcome"] == ReceiptLookupOutcome.IN_PROGRESS:
                raise ConflictError(f"mutation request is already in progress: {request_id}")
            if lookup["outcome"] == ReceiptLookupOutcome.EXPIRED:
                raise ConflictError(f"mutation request result expired and cannot be reapplied safely: {request_id}")
            if lookup["outcome"] == ReceiptLookupOutcome.CONFLICT:
                raise ConflictError(f"request_id is already associated with a different mutation: {request_id}")
            created_at = self.receipt_time()
            result = self.commit_new_locked(artifact, request_id, payload_signature, created_at)
            return result

    def commit_new_locked(
        self,
        artifact: dict,
        request_id: str,
        payload_signature: str,
        created_at: str,
    ) -> dict:
        current = self.internal_coordinator.snapshot()["repository"]
        current_artifacts = current["artifacts"]
        if artifact.get("statement_id", "") in current_artifacts:
            raise ConflictError(f"artifact statement_id already exists: {artifact.get('statement_id', '')}")
        collision_owner_ids = response_collision_owner_ids(artifact, self.internal_coordinator)
        if collision_owner_ids:
            named = ", ".join(collision_owner_ids)
            raise ConflictError(f"retrieval key collision with existing statement IDs: {named}")

        plan = self.internal_coordinator.repository.plan_admission(artifact, self.internal_admission_policy)
        if plan["outcome"] == AdmissionOutcome.REJECTED_CAPACITY:
            result_code = MutationResultCode.REJECTED_CAPACITY
            affected_generations = ()
        else:
            result_code = (
                MutationResultCode.CREATED_WITH_EVICTION
                if plan["outcome"] == AdmissionOutcome.ADMITTED_WITH_EVICTION
                else MutationResultCode.CREATED
            )
            removed = tuple(
                artifact_generation_change(
                    statement_id,
                    current_artifacts[statement_id]["generation"],
                    0,
                )
                for statement_id in plan["evicted_statement_ids"]
            )
            affected_generations = normalize_artifact_generation_changes(
                (*removed, artifact_generation_change(artifact.get("statement_id", ""), 0, artifact.get("generation", 0)))
            )
        result = {
            "statement_id": artifact.get("statement_id", ""),
            "generation": artifact.get("generation", 0),
            "admission_outcome": plan["outcome"].value,
            "evicted_statement_ids": list(plan["evicted_statement_ids"]),
        }
        mutation_receipt_value = mutation_receipt(
            sequence=self.internal_coordinator.next_receipt_sequence,
            request_id=request_id,
            operation=MutationOperation.COMMIT_RESPONSE,
            payload_signature=payload_signature,
            result_code=result_code,
            affected_generations=affected_generations,
            result=result,
            completion_state=ReceiptCompletionState.COMPLETED,
            created_at=created_at,
        )
        candidate = self.internal_coordinator.build_candidate(
            plan["candidate"],
            mutation_receipt_value,
        )
        execution = self.internal_coordinator.execute(candidate)
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
    ) -> dict:
        """Construct one DYNAMIC ACTIVE response artifact and commit it."""

        if not isinstance(metadata, dict):
            raise InvalidRequestError("learn response metadata must be an object")
        payload_signature = canonical_payload_signature(
            {
                "operation": MutationOperation.COMMIT_RESPONSE.value,
                "request": request,
                "response": response,
                "caller_id": caller_id,
                "namespace": namespace,
                "context_fingerprint": context_fingerprint,
                "source_label": source_label,
                "metadata": metadata,
            }
        )
        with self.internal_coordinator.mutation():
            lookup = self.internal_lookup(request_id, MutationOperation.COMMIT_RESPONSE, payload_signature)
            if lookup["outcome"] == ReceiptLookupOutcome.REPLAY:
                result = response_mutation_result(
                    receipt=receipt_lookup_receipt(lookup),
                    replayed=True,
                )
                return result
            if lookup["outcome"] == ReceiptLookupOutcome.IN_PROGRESS:
                raise ConflictError(f"mutation request is already in progress: {request_id}")
            if lookup["outcome"] == ReceiptLookupOutcome.EXPIRED:
                raise ConflictError(f"mutation request result expired and cannot be reapplied safely: {request_id}")
            if lookup["outcome"] == ReceiptLookupOutcome.CONFLICT:
                raise ConflictError(f"request_id is already associated with a different learned response: {request_id}")
            accepted_at = self.receipt_time()
            scope = scope_key(namespace=namespace, context_fingerprint=context_fingerprint)
            support_records = metadata.get("support", [])
            if not isinstance(support_records, list) or not all(isinstance(record, dict) for record in support_records):
                raise InvalidRequestError("learn response metadata support must be an array of objects")
            try:
                support_references = tuple(validate_support_reference(record) for record in support_records)
            except ValueError as error:
                raise InvalidRequestError(str(error)) from error
            artifact = cached_response_artifact(
                statement_id=f"response_{uuid5(NAMESPACE_URL, f'engram:LearnResponse:{request_id}').hex}",
                generation=1,
                response=response,
                query_identity=build_standalone_identity(request, scope),
                retrieval=build_retrieval_representation(request),
                tier=Tier.DYNAMIC,
                lifecycle=LifecycleState.ACTIVE,
                scope=scope,
                support_references=support_references,
                valid_from="",
                valid_from_available=False,
                valid_until="",
                valid_until_available=False,
                superseded_by="",
                provenance=artifact_provenance(source_label, caller_id, accepted_at),
                statistics=artifact_statistics(),
                metadata=dict(metadata),
            )
            artifact = validate_base_response_artifact(artifact)
            result = self.commit_new_locked(artifact, request_id, payload_signature, accepted_at)
            return result

    def record_response_queries(
        self,
        statement_ids: tuple[str, ...],
        request_id: str,
    ) -> dict:
        """Increment authoritative candidate-query statistics once per artifact."""

        if not isinstance(statement_ids, tuple) or not statement_ids:
            raise InvalidRequestError("response query statement_ids must be a non-empty tuple")
        if not all(isinstance(statement_id, str) and statement_id for statement_id in statement_ids):
            raise InvalidRequestError("response query statement_ids must contain non-empty strings")
        normalized_ids = tuple(sorted(set(statement_ids)))
        if normalized_ids != statement_ids:
            raise InvalidRequestError("response query statement_ids must be sorted and unique")
        payload_signature = canonical_payload_signature({"statement_ids": list(normalized_ids)})
        with self.internal_coordinator.mutation():
            lookup = self.internal_lookup(request_id, MutationOperation.RECORD_RESPONSE_QUERY, payload_signature)
            if lookup["outcome"] == ReceiptLookupOutcome.REPLAY:
                result = response_mutation_result(
                    receipt=receipt_lookup_receipt(lookup),
                    replayed=True,
                )
                return result
            if lookup["outcome"] != ReceiptLookupOutcome.NEW:
                raise ConflictError(f"response query accounting request cannot be applied: {request_id}")
            updated_artifacts = []
            effects = []
            for statement_id in normalized_ids:
                current = self.internal_coordinator.repository.get_artifact(statement_id)
                updated = cached_response_artifact_to_dict(current)
                updated["generation"] = current["generation"] + 1
                statistics = mapping_copy(updated["statistics"], "statistics")
                statistics["query_count"] = current["statistics"]["query_count"] + 1
                updated["statistics"] = statistics
                artifact = cached_response_artifact_from_dict(updated)
                updated_artifacts.append(artifact)
                effects.append(artifact_generation_change(statement_id, current["generation"], artifact["generation"]))
            repository_candidate = self.internal_coordinator.repository.candidate_with_artifacts(tuple(updated_artifacts))
            recorded_at = self.receipt_time()
            receipt = mutation_receipt(
                sequence=self.internal_coordinator.next_receipt_sequence,
                request_id=request_id,
                operation=MutationOperation.RECORD_RESPONSE_QUERY,
                payload_signature=payload_signature,
                result_code=MutationResultCode.QUERY_RECORDED,
                affected_generations=tuple(effects),
                result={"statement_ids": list(normalized_ids), "recorded_at": recorded_at},
                completion_state=ReceiptCompletionState.COMPLETED,
                created_at=recorded_at,
            )
            candidate = self.internal_coordinator.build_candidate(repository_candidate, receipt)
            execution = self.internal_coordinator.execute(candidate)
            result = response_mutation_result_from_execution(execution)
            return result

    def record_response_hit(self, statement_id: str, request_id: str) -> dict:
        """Increment one authoritative accepted-hit statistic and last-hit time."""

        normalized_id = response_audit_text(statement_id, "response hit statement_id", 256, allow_empty=False)
        payload_signature = canonical_payload_signature({"statement_id": normalized_id})
        with self.internal_coordinator.mutation():
            lookup = self.internal_lookup(request_id, MutationOperation.RECORD_RESPONSE_HIT, payload_signature)
            if lookup["outcome"] == ReceiptLookupOutcome.REPLAY:
                result = response_mutation_result(
                    receipt=receipt_lookup_receipt(lookup),
                    replayed=True,
                )
                return result
            if lookup["outcome"] != ReceiptLookupOutcome.NEW:
                raise ConflictError(f"response hit accounting request cannot be applied: {request_id}")
            current = self.internal_coordinator.repository.get_artifact(normalized_id)
            recorded_at = self.receipt_time()
            updated = cached_response_artifact_to_dict(current)
            updated["generation"] = current["generation"] + 1
            statistics = mapping_copy(updated["statistics"], "statistics")
            statistics["hit_count"] = current["statistics"]["hit_count"] + 1
            statistics["last_hit"] = recorded_at
            statistics["last_hit_available"] = True
            updated["statistics"] = statistics
            artifact = cached_response_artifact_from_dict(updated)
            repository_candidate = self.internal_coordinator.repository.candidate_with_artifacts((artifact,))
            receipt = mutation_receipt(
                sequence=self.internal_coordinator.next_receipt_sequence,
                request_id=request_id,
                operation=MutationOperation.RECORD_RESPONSE_HIT,
                payload_signature=payload_signature,
                result_code=MutationResultCode.HIT_RECORDED,
                affected_generations=(artifact_generation_change(normalized_id, current["generation"], artifact["generation"]),),
                result={"statement_id": normalized_id, "recorded_at": recorded_at},
                completion_state=ReceiptCompletionState.COMPLETED,
                created_at=recorded_at,
            )
            candidate = self.internal_coordinator.build_candidate(repository_candidate, receipt)
            execution = self.internal_coordinator.execute(candidate)
            result = response_mutation_result_from_execution(execution)
            return result

    def finalize_resolution_accounting(
        self,
        statement_ids: tuple[str, ...],
        accepted_statement_id: str,
        request_id: str,
    ) -> dict:
        """Atomically record unique candidacy and an optional accepted hit."""
        if not isinstance(statement_ids, tuple) or not statement_ids:
            raise InvalidRequestError("resolution statement_ids must be a non-empty tuple")
        if not all(isinstance(statement_id, str) and statement_id for statement_id in statement_ids):
            raise InvalidRequestError("resolution statement_ids must contain non-empty strings")
        normalized_ids = tuple(sorted(set(statement_ids)))
        if normalized_ids != statement_ids:
            raise InvalidRequestError("resolution statement_ids must be sorted and unique")
        normalized_accepted = response_audit_text(
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
        with self.internal_coordinator.mutation():
            lookup = self.internal_lookup(request_id, MutationOperation.FINALIZE_RESOLUTION_ACCOUNTING, payload_signature)
            if lookup["outcome"] == ReceiptLookupOutcome.REPLAY:
                result = response_mutation_result(
                    receipt=receipt_lookup_receipt(lookup),
                    replayed=True,
                )
                return result
            if lookup["outcome"] != ReceiptLookupOutcome.NEW:
                raise ConflictError(f"resolution accounting request cannot be applied: {request_id}")
            recorded_at = self.receipt_time()
            updated_artifacts = []
            effects = []
            for statement_id in normalized_ids:
                current = self.internal_coordinator.repository.get_artifact(statement_id)
                updated = cached_response_artifact_to_dict(current)
                updated["generation"] = current["generation"] + 1
                statistics = mapping_copy(updated["statistics"], "statistics")
                statistics["query_count"] = current["statistics"]["query_count"] + 1
                if statement_id == normalized_accepted:
                    statistics["hit_count"] = current["statistics"]["hit_count"] + 1
                    statistics["last_hit"] = recorded_at
                    statistics["last_hit_available"] = True
                updated["statistics"] = statistics
                artifact = cached_response_artifact_from_dict(updated)
                updated_artifacts.append(artifact)
                effects.append(artifact_generation_change(statement_id, current["generation"], artifact["generation"]))
            repository_candidate = self.internal_coordinator.repository.candidate_with_artifacts(tuple(updated_artifacts))
            receipt = mutation_receipt(
                sequence=self.internal_coordinator.next_receipt_sequence,
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
            candidate = self.internal_coordinator.build_candidate(repository_candidate, receipt)
            execution = self.internal_coordinator.execute(candidate)
            result = response_mutation_result_from_execution(execution)
            return result

    def transition_response(
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
    ) -> dict:
        normalized_statement_id = response_audit_text(statement_id, "lifecycle statement_id", 256, allow_empty=False)
        if isinstance(expected_generation, bool) or not isinstance(expected_generation, int) or expected_generation < 1:
            raise InvalidRequestError("expected_generation must be a positive integer")
        if not isinstance(reason, LifecycleMutationReason):
            raise InvalidRequestError("lifecycle reason must be a LifecycleMutationReason")
        normalized_caller_id = response_audit_text(caller_id, "lifecycle caller_id", 256, allow_empty=False)
        normalized_detail = response_audit_text(audit_detail, "lifecycle audit_detail", 1_024, allow_empty=True)
        payload_signature = canonical_payload_signature(
            {
                "statement_id": normalized_statement_id,
                "expected_generation": expected_generation,
                "reason": reason.value,
                "caller_id": normalized_caller_id,
                "audit_detail": normalized_detail,
            }
        )
        with self.internal_coordinator.mutation():
            lookup = self.internal_lookup(request_id, mutation_operation, payload_signature)
            if lookup["outcome"] == ReceiptLookupOutcome.REPLAY:
                result = response_mutation_result(
                    receipt=receipt_lookup_receipt(lookup),
                    replayed=True,
                )
                return result
            if lookup["outcome"] == ReceiptLookupOutcome.IN_PROGRESS:
                raise ConflictError(f"mutation request is already in progress: {request_id}")
            if lookup["outcome"] == ReceiptLookupOutcome.EXPIRED:
                raise ConflictError(f"mutation request result expired and cannot be reapplied safely: {request_id}")
            if lookup["outcome"] == ReceiptLookupOutcome.CONFLICT:
                raise ConflictError(f"request_id is already associated with a different mutation: {request_id}")

            current = self.internal_coordinator.repository.get_artifact(normalized_statement_id)
            if current["generation"] != expected_generation:
                raise ConflictError(
                    f"artifact generation conflict for {normalized_statement_id}: "
                    f"expected {expected_generation}, current {current['generation']}"
                )
            require_lifecycle_transition(current["lifecycle"], target, lifecycle_operation)
            occurred_at = self.receipt_time()
            updated = cached_response_artifact_to_dict(current)
            updated["generation"] = current["generation"] + 1
            updated["lifecycle"] = target.value
            updated_metadata = mapping_copy(updated["metadata"], "metadata")
            updated_metadata["lifecycle_audit"] = {
                "operation": mutation_operation.value,
                "reason": reason.value,
                "caller_id": normalized_caller_id,
                "request_id": request_id,
                "occurred_at": occurred_at,
                "detail": normalized_detail,
            }
            updated["metadata"] = updated_metadata
            transitioned = cached_response_artifact_from_dict(updated)
            repository_candidate = self.internal_coordinator.repository.candidate_with_artifact(transitioned)
            result = {
                "statement_id": transitioned["statement_id"],
                "before_generation": current["generation"],
                "generation": transitioned["generation"],
                "lifecycle": transitioned["lifecycle"].value,
                "reason": reason.value,
                "caller_id": normalized_caller_id,
                "audit_detail": normalized_detail,
                "occurred_at": occurred_at,
            }
            mutation_receipt_value = mutation_receipt(
                sequence=self.internal_coordinator.next_receipt_sequence,
                request_id=request_id,
                operation=mutation_operation,
                payload_signature=payload_signature,
                result_code=result_code,
                affected_generations=(
                    artifact_generation_change(
                        transitioned["statement_id"],
                        current["generation"],
                        transitioned["generation"],
                    ),
                ),
                result=result,
                completion_state=ReceiptCompletionState.COMPLETED,
                created_at=occurred_at,
            )
            candidate = self.internal_coordinator.build_candidate(
                repository_candidate,
                mutation_receipt_value,
            )
            execution = self.internal_coordinator.execute(candidate)
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
    ) -> dict:
        """Invalidate one ACTIVE in-memory artifact with an audit record."""

        result = self.transition_response(
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
    ) -> dict:
        """Retire one ACTIVE in-memory artifact with an audit record."""

        result = self.transition_response(
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
        replacement: dict,
        reason: LifecycleMutationReason,
        caller_id: str,
        request_id: str,
        audit_detail: str = "",
    ) -> dict:
        """Atomically supersede one expected ACTIVE artifact with one new artifact."""

        normalized_statement_id = response_audit_text(
            expected_statement_id,
            "supersession expected_statement_id",
            256,
            allow_empty=False,
        )
        if isinstance(expected_generation, bool) or not isinstance(expected_generation, int) or expected_generation < 1:
            raise InvalidRequestError("expected_generation must be a positive integer")
        replacement = validate_base_response_artifact(replacement)
        if replacement.get("statement_id", "") == normalized_statement_id:
            raise InvalidRequestError("supersession replacement must have a new statement_id")
        if not isinstance(reason, LifecycleMutationReason):
            raise InvalidRequestError("supersession reason must be a LifecycleMutationReason")
        normalized_caller_id = response_audit_text(caller_id, "supersession caller_id", 256, allow_empty=False)
        normalized_detail = response_audit_text(audit_detail, "supersession audit_detail", 1_024, allow_empty=True)
        payload_signature = canonical_payload_signature(
            {
                "expected_statement_id": normalized_statement_id,
                "expected_generation": expected_generation,
                "replacement": cached_response_artifact_to_dict(replacement),
                "reason": reason.value,
                "caller_id": normalized_caller_id,
                "audit_detail": normalized_detail,
            }
        )
        with self.internal_coordinator.mutation():
            lookup = self.internal_lookup(request_id, MutationOperation.SUPERSEDE_RESPONSE, payload_signature)
            if lookup["outcome"] == ReceiptLookupOutcome.REPLAY:
                result = response_mutation_result(
                    receipt=receipt_lookup_receipt(lookup),
                    replayed=True,
                )
                return result
            if lookup["outcome"] == ReceiptLookupOutcome.IN_PROGRESS:
                raise ConflictError(f"mutation request is already in progress: {request_id}")
            if lookup["outcome"] == ReceiptLookupOutcome.EXPIRED:
                raise ConflictError(f"mutation request result expired and cannot be reapplied safely: {request_id}")
            if lookup["outcome"] == ReceiptLookupOutcome.CONFLICT:
                raise ConflictError(f"request_id is already associated with a different mutation: {request_id}")

            before = self.internal_coordinator.snapshot()["repository"]
            current = self.internal_coordinator.repository.get_artifact(normalized_statement_id)
            if current["generation"] != expected_generation:
                raise ConflictError(
                    f"artifact generation conflict for {normalized_statement_id}: "
                    f"expected {expected_generation}, current {current['generation']}"
                )
            require_lifecycle_transition(current["lifecycle"], LifecycleState.SUPERSEDED, LifecycleOperation.SUPERSEDE)
            before_artifacts = before["artifacts"]
            if replacement.get("statement_id", "") in before_artifacts:
                raise ConflictError(f"artifact statement_id already exists: {replacement.get('statement_id', '')}")
            conflicting_owners = set(response_collision_owner_ids(replacement, self.internal_coordinator))
            conflicting_owners.discard(normalized_statement_id)
            if conflicting_owners:
                named = ", ".join(sorted(conflicting_owners))
                raise ConflictError(f"replacement retrieval key collision with existing statement IDs: {named}")

            plan = self.internal_coordinator.repository.plan_admission(replacement, self.internal_admission_policy)
            lineage_evicted = normalized_statement_id in plan["evicted_statement_ids"]
            if plan["outcome"] == AdmissionOutcome.REJECTED_CAPACITY or lineage_evicted:
                result = {
                    "expected_statement_id": normalized_statement_id,
                    "expected_generation": expected_generation,
                    "replacement_statement_id": replacement.get("statement_id", ""),
                    "admission_outcome": AdmissionOutcome.REJECTED_CAPACITY.value,
                    "evicted_statement_ids": [],
                }
                rejected_receipt = mutation_receipt(
                    sequence=self.internal_coordinator.next_receipt_sequence,
                    request_id=request_id,
                    operation=MutationOperation.SUPERSEDE_RESPONSE,
                    payload_signature=payload_signature,
                    result_code=MutationResultCode.REJECTED_CAPACITY,
                    affected_generations=(),
                    result=result,
                    completion_state=ReceiptCompletionState.COMPLETED,
                    created_at=self.receipt_time(),
                )
                candidate = self.internal_coordinator.build_candidate(before, rejected_receipt)
                execution = self.internal_coordinator.execute(candidate)
                result = response_mutation_result_from_execution(execution)
                return result

            occurred_at = self.receipt_time()
            updated_current = cached_response_artifact_to_dict(current)
            updated_current["generation"] = current["generation"] + 1
            updated_current["lifecycle"] = LifecycleState.SUPERSEDED.value
            updated_current["superseded_by"] = replacement.get("statement_id", "")
            updated_metadata = mapping_copy(updated_current["metadata"], "metadata")
            updated_metadata["lifecycle_audit"] = {
                "operation": MutationOperation.SUPERSEDE_RESPONSE.value,
                "reason": reason.value,
                "caller_id": normalized_caller_id,
                "request_id": request_id,
                "occurred_at": occurred_at,
                "detail": normalized_detail,
                "replacement_statement_id": replacement.get("statement_id", ""),
            }
            updated_current["metadata"] = updated_metadata
            superseded = cached_response_artifact_from_dict(updated_current)
            repository_candidate = repository_state_with_artifact_updates(plan["candidate"], (superseded,))
            removed = tuple(
                artifact_generation_change(statement_id, before_artifacts[statement_id]["generation"], 0)
                for statement_id in plan["evicted_statement_ids"]
            )
            affected_generations = normalize_artifact_generation_changes(
                (
                    *removed,
                    artifact_generation_change(
                        superseded["statement_id"],
                        current["generation"],
                        superseded["generation"],
                    ),
                    artifact_generation_change(replacement.get("statement_id", ""), 0, replacement.get("generation", 0)),
                )
            )
            result = {
                "superseded_statement_id": superseded["statement_id"],
                "superseded_generation": superseded["generation"],
                "replacement_statement_id": replacement.get("statement_id", ""),
                "replacement_generation": replacement.get("generation", 0),
                "admission_outcome": plan["outcome"].value,
                "evicted_statement_ids": list(plan["evicted_statement_ids"]),
                "reason": reason.value,
                "caller_id": normalized_caller_id,
                "audit_detail": normalized_detail,
                "occurred_at": occurred_at,
            }
            mutation_receipt_value = mutation_receipt(
                sequence=self.internal_coordinator.next_receipt_sequence,
                request_id=request_id,
                operation=MutationOperation.SUPERSEDE_RESPONSE,
                payload_signature=payload_signature,
                result_code=MutationResultCode.SUPERSEDED,
                affected_generations=affected_generations,
                result=result,
                completion_state=ReceiptCompletionState.COMPLETED,
                created_at=occurred_at,
            )
            candidate = self.internal_coordinator.build_candidate(
                repository_candidate,
                mutation_receipt_value,
            )
            execution = self.internal_coordinator.execute(candidate)
            result = response_mutation_result_from_execution(execution)
            return result
