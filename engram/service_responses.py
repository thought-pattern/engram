"""Accepted-response maintenance for the Engram service.

Learning, superseding and retiring accepted responses, and finding the responses a set
of graph records supports. Each function that takes ``core`` is bound on
``engram.service.EngramCore`` as the method of the same name.
"""

from copy import deepcopy

from engram import sessions
from engram.constants import DEFAULT_LEARNED_SOURCE_LABEL, DEFAULT_USER_ID, EMPTY_METADATA
from engram.constants_artifacts import (
    MAX_ARTIFACT_ID_BYTES,
    MAX_RESPONSE_BYTES,
    MAX_SOURCE_LABEL_BYTES,
    LifecycleMutationReason,
    LifecycleState,
)
from engram.constants_feedback import MAX_FEEDBACK_REASON_BYTES
from engram.constants_identity import MAX_CONTEXT_FINGERPRINT_BYTES, MAX_NAMESPACE_BYTES
from engram.constants_mutations import MAX_REQUEST_ID_BYTES
from engram.errors import ConflictError, InvalidRequestError, LifecycleError, ResourceNotFoundError
from engram.mutations import mutation_receipt_to_dict
from engram.responses import response_mutation_result_to_dict
from engram.service_requests import normalize_service_user_id, require_cache_request
from engram.text import normalize
from engram.validation import require_any_text


def learn_response(
    core,
    request: str,
    response: str,
    request_id: str,
    user_id: str = DEFAULT_USER_ID,
    namespace: str = "",
    context_fingerprint: str = "",
    source_label: str = DEFAULT_LEARNED_SOURCE_LABEL,
    metadata: dict = EMPTY_METADATA,
) -> dict:
    """Create one DYNAMIC ACTIVE response artifact through base commit."""
    with core.lock:
        core.require_running()
        require_cache_request(request)
        require_any_text(response, "response", MAX_RESPONSE_BYTES, blank_is_empty=True)
        require_any_text(request_id, "request_id", MAX_REQUEST_ID_BYTES, blank_is_empty=True)
        require_any_text(namespace, "namespace", MAX_NAMESPACE_BYTES, allow_empty=True)
        require_any_text(context_fingerprint, "context_fingerprint", MAX_CONTEXT_FINGERPRINT_BYTES, allow_empty=True)
        require_any_text(source_label, "source_label", MAX_SOURCE_LABEL_BYTES, allow_empty=True)
        if normalize(response) == "idk":
            raise InvalidRequestError("IDK is not a cacheable response")
        if not isinstance(metadata, dict):
            raise InvalidRequestError("metadata must be an object")
        metadata = dict(metadata)
        normalized_user_id = normalize_service_user_id(user_id)
        mutation = core.response_mutations.learn_response(
            request,
            response,
            request_id,
            normalized_user_id,
            namespace,
            context_fingerprint,
            source_label,
            metadata,
        )
        replayed = mutation.get("replayed", False)
        receipt_value = mutation_receipt_to_dict(mutation.get("receipt", {}))
        receipt_result = receipt_value.get("result", False)
        if not isinstance(receipt_result, dict):
            raise LifecycleError("response mutation receipt result is not an object")
        receipt_statement_id = receipt_result.get("statement_id", False)
        evicted_statement_ids = receipt_result.get("evicted_statement_ids", False)
        if not isinstance(receipt_statement_id, str) or not isinstance(evicted_statement_ids, list):
            raise LifecycleError("response mutation receipt result is malformed")
        result_code = receipt_value.get("result_code", "")
        learned = result_code != "REJECTED_CAPACITY"
        if learned and not replayed:
            core.engram.eviction_count += len(evicted_statement_ids)
        if learned and normalized_user_id != DEFAULT_USER_ID:
            with core.engram.session_lock:
                sessions.get_session(core.engram, normalized_user_id, create_if_missing=True)
                sessions.update_session_context(core.engram, normalized_user_id, response)
        if replayed:
            core.regulated_metrics["idempotent_retries"] += 1
        elif learned:
            core.regulated_metrics["learned_created"] += 1
        result = {
            "learned": learned,
            "statement_id": receipt_statement_id,
            "action": "created" if learned else "rejected_capacity",
            "request_id": request_id,
            "user_id": normalized_user_id,
            "namespace": namespace,
            "context_fingerprint": context_fingerprint,
            "source_label": source_label,
            "idempotent": replayed,
            "result_code": result_code,
            "evicted_statement_ids": evicted_statement_ids,
        }
        result = deepcopy(result)
        return result


def supersede_response(
    core,
    statement_id: str,
    replacement: dict,
    reason: LifecycleMutationReason,
    request_id: str,
    audit_detail: str = "",
) -> dict:
    """Atomically replace one accepted-response artifact with its correction."""

    with core.lock:
        core.require_running()
        require_any_text(statement_id, "statement_id", MAX_ARTIFACT_ID_BYTES, blank_is_empty=True)
        require_any_text(request_id, "request_id", MAX_REQUEST_ID_BYTES, blank_is_empty=True)
        require_any_text(audit_detail, "audit_detail", MAX_FEEDBACK_REASON_BYTES, allow_empty=True)
        current = core.engram.response_repository.get_artifact(statement_id)
        mutation = core.response_mutations.supersede_response(
            statement_id,
            current.get("generation", 0),
            replacement,
            reason,
            "SupersedeResponse",
            request_id,
            audit_detail,
        )
        result = response_mutation_result_to_dict(mutation)
        return result


def retire_response(core, statement_id: str, reason: str, request_id: str) -> dict:
    """Retire one accepted-response artifact."""
    with core.lock:
        core.require_running()
        require_any_text(statement_id, "statement_id", MAX_ARTIFACT_ID_BYTES, blank_is_empty=True)
        require_any_text(reason, "reason", MAX_FEEDBACK_REASON_BYTES, blank_is_empty=True)
        require_any_text(request_id, "request_id", MAX_REQUEST_ID_BYTES, blank_is_empty=True)
        try:
            current = core.engram.response_repository.get_artifact(statement_id)
        except ResourceNotFoundError as err:
            raise ResourceNotFoundError("accepted response artifact not found") from err
        current_generation = current.get("generation", False)
        if isinstance(current_generation, bool) or not isinstance(current_generation, int):
            raise LifecycleError("accepted-response artifact generation is malformed")
        previous = core.retire_requests.get(request_id, {})
        if previous:
            if not isinstance(previous, dict):
                raise LifecycleError("retirement replay record is malformed")
            previous_result = previous.get("result", False)
            if not isinstance(previous_result, dict):
                raise LifecycleError("retirement replay result is malformed")
            if previous_result.get("statement_id", "") != statement_id or previous_result.get("reason", "") != reason:
                raise ConflictError("request_id is already associated with a different retirement")
            expected_generation = previous.get("expected_generation", False)
            if isinstance(expected_generation, bool) or not isinstance(expected_generation, int):
                raise LifecycleError("retirement replay generation is malformed")
        else:
            expected_generation = current_generation
        mutation = core.response_mutations.retire_response(
            statement_id,
            expected_generation,
            LifecycleMutationReason.ADMINISTRATIVE,
            "RetireResponse",
            request_id,
            reason,
        )
        replayed = mutation.get("replayed", False)
        if type(replayed) is not bool:
            raise LifecycleError("response mutation replay marker is malformed")
        metric_key = "idempotent_retries" if replayed else "retired"
        metric_value = core.regulated_metrics.get(metric_key, False)
        if isinstance(metric_value, bool) or not isinstance(metric_value, int):
            raise LifecycleError("response mutation metric is malformed")
        core.regulated_metrics[metric_key] = metric_value + 1
        receipt = mutation.get("receipt", False)
        if not isinstance(receipt, dict):
            raise LifecycleError("response mutation receipt is not an object")
        receipt_value = mutation_receipt_to_dict(receipt)
        receipt_result = receipt_value.get("result", False)
        if not isinstance(receipt_result, dict):
            raise LifecycleError("response mutation receipt result is not an object")
        generation = receipt_result.get("generation", False)
        if isinstance(generation, bool) or not isinstance(generation, int):
            raise LifecycleError("response mutation receipt generation is malformed")
        result = {
            "retired": True,
            "statement_id": statement_id,
            "reason": reason,
            "request_id": request_id,
            "idempotent": replayed,
            "generation": generation,
        }
        core.retire_requests[request_id] = {
            "expected_generation": expected_generation,
            "result": result,
        }
        core.enforce_transient_bound()
        result = deepcopy(result)
        return result


def responses_by_support(core, record_ids: list[str]) -> dict:
    """List ACTIVE accepted responses whose support names any given durable record."""
    with core.lock:
        core.require_running()
        if not isinstance(record_ids, list) or not record_ids:
            raise InvalidRequestError("record_ids must be a non-empty list")
        for identifier in record_ids:
            require_any_text(identifier, "record_id", MAX_ARTIFACT_ID_BYTES, blank_is_empty=True)
        wanted = set(record_ids)
        artifacts = core.engram.response_repository.trusted_artifacts()
        statement_ids = sorted(
            identifier
            for identifier, artifact in artifacts.items()
            if artifact.get("lifecycle", LifecycleState.RETIRED) == LifecycleState.ACTIVE
            and any(reference.get("id", "") in wanted for reference in artifact.get("support_references", ()))
        )
        result = {"statement_ids": statement_ids}
        return result
