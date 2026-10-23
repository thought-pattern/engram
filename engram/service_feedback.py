"""Feedback learning for the Engram service.

Turns resolution and proposal outcomes into utility observations, applies them once per
request through the mutation ledger, and keeps cached responses and proposals candidates
for that learning. Each function that takes ``core`` is bound on
``engram.service.EngramCore`` as the method of the same name.
"""

from copy import deepcopy
from hashlib import sha256 as hashlib_sha256
from logging import getLogger as logging_getLogger

from engram.constants_artifacts import MAX_ARTIFACT_ID_BYTES, LifecycleMutationReason
from engram.constants_feedback import (
    MAX_FEEDBACK_REASON_BYTES,
    FeedbackObservationKind,
    FeedbackOutcome,
    FeedbackReferenceKind,
    LifecycleHandoffStatus,
)
from engram.constants_mutations import MAX_REQUEST_ID_BYTES
from engram.constants_relation import ExpectedObjectType
from engram.coordination import MutationCoordinationError
from engram.errors import ConflictError, InvalidRequestError, LifecycleError, ResourceNotFoundError
from engram.feedback import validate_feedback_observation
from engram.feedback_values import canonical_utc, constraint_fingerprint
from engram.fusion import policy_fingerprint
from engram.mutations import mutation_receipt_to_dict
from engram.resolution import validate_resolution_result
from engram.validation import require_any_text

LOGGER = logging_getLogger("engram.service")


def feedback_target(observations: tuple[object, ...], statement_id: str) -> dict:
    target = {}
    for value in observations:
        try:
            observation = validate_feedback_observation(value)
        except InvalidRequestError:
            continue
        if observation.get("statement_id", "") == statement_id:
            target = observation
            break
    if target == {}:
        raise LifecycleError("candidate feedback target is unavailable")
    result = validate_feedback_observation(target)
    return result


def feedback_mutation_request_id(kind: str, request_id: str) -> str:
    """Return a bounded non-disclosing feedback mutation identity."""
    digest = hashlib_sha256(request_id.encode("utf-8")).hexdigest()
    result = f"feedback:{kind}:sha256:{digest}"
    return result


def candidate_generation(core, statement_id: str, resolution: dict) -> tuple[int, bool]:
    artifact = core.engram.response_repository.trusted_artifacts().get(statement_id, {})
    if artifact:
        result = artifact.get("generation", 0), True
        return result
    current = validate_resolution_result(resolution)
    for resolver_result in current.get("resolver_results", ()):
        for candidate in resolver_result.get("candidates", ()):
            if candidate.get("statement_id", "") != statement_id:
                continue
            generation = candidate.get("provenance", {}).get("generation", 0)
            if isinstance(generation, int) and not isinstance(generation, bool) and generation > 0:
                result = generation, True
                return result
    result = 0, False
    return result


def feedback_observations(
    core,
    frame: dict,
    resolution: dict,
    statement_ids: tuple[str, ...],
    *,
    reference_kind: FeedbackReferenceKind,
    reference_id: str,
    kind: FeedbackObservationKind,
    outcome: FeedbackOutcome,
    observed_at: str,
    reason: str = "",
) -> tuple[dict, ...]:
    policy_value = policy_fingerprint(core.resolution_orchestrator.internal_fusion.policy)
    constraint = constraint_fingerprint(
        frame.get("expected_object_type", ExpectedObjectType.UNKNOWN).value,
        frame.get("required_metadata", {}),
        frame.get("required_source_label", ""),
    )
    observations = []
    for statement_id in statement_ids:
        generation, generation_available = core.candidate_generation(statement_id, resolution)
        observation = {
            "reference_kind": reference_kind,
            "reference_id": reference_id,
            "kind": kind,
            "outcome": outcome,
            "query_identity": frame.get("identity", {}),
            "scope": frame.get("scope", {}),
            "constraint_fingerprint": constraint,
            "statement_id": statement_id,
            "generation": generation,
            "generation_available": generation_available,
            "policy_fingerprint": policy_value,
            "observed_at": observed_at,
            "reason": reason,
        }
        observations.append(observation)
    result = tuple(observations)
    return result


def apply_feedback(
    core,
    request_id: str,
    observations: tuple[dict, ...],
    lifecycle_status: LifecycleHandoffStatus = LifecycleHandoffStatus.NOT_APPLICABLE,
) -> dict[str, object]:
    candidate = core.engram.feedback_store.apply_validated(request_id, observations, lifecycle_status)
    receipt_value = mutation_receipt_to_dict(candidate.get("receipt", {}))
    result = receipt_value.get("result", False)
    if not isinstance(result, dict):
        raise LifecycleError("feedback mutation receipt result is malformed")
    value = deepcopy(result)
    value.update(
        {
            "request_id": request_id,
            "result_code": receipt_value.get("result_code", ""),
            "idempotent": candidate.get("replayed", False),
            "memory_only": True,
        }
    )
    return value


def ensure_resolution_candidacy(core, request_id: str, record: dict[str, object]) -> bool:
    if record.get("candidacy_applied", False):
        return False
    observations = record.get("candidacy_observations", [])
    if not isinstance(observations, tuple):
        raise LifecycleError("resolution candidacy observations are malformed")
    core.apply_feedback(feedback_mutation_request_id("candidacy", request_id), observations)
    record["candidacy_applied"] = True
    return True


def ensure_proposal_candidacy(core, proposal_id: str, record: dict[str, object]) -> bool:
    if record.get("candidacy_applied", False):
        return False
    observations = record.get("candidacy_observations", [])
    if not isinstance(observations, tuple):
        raise LifecycleError("proposal candidacy observations are malformed")
    core.apply_feedback(feedback_mutation_request_id("proposal-candidacy", proposal_id), observations)
    record["candidacy_applied"] = True
    return True


def proposal_feedback_observations(
    core,
    frame: dict,
    proposal_id: str,
    statement_ids: tuple[str, ...],
) -> tuple[dict, ...]:
    constraint = constraint_fingerprint(
        frame.get("expected_object_type", ExpectedObjectType.UNKNOWN).value,
        frame.get("required_metadata", {}),
        frame.get("required_source_label", ""),
    )
    policy_value = policy_fingerprint(core.resolution_orchestrator.internal_fusion.policy)
    repository = core.engram.response_repository.trusted_artifacts()
    observed_at = frame.get("eligibility_context", {}).get("evaluation_time", "")
    values = []
    for statement_id in statement_ids:
        artifact = repository.get(statement_id, {})
        observation = {
            "reference_kind": FeedbackReferenceKind.REGULATED_PROPOSAL,
            "reference_id": proposal_id,
            "kind": FeedbackObservationKind.CANDIDACY,
            "outcome": FeedbackOutcome.CANDIDATE,
            "query_identity": frame.get("identity", {}),
            "scope": frame.get("scope", {}),
            "constraint_fingerprint": constraint,
            "statement_id": statement_id,
            "generation": artifact.get("generation", 0),
            "generation_available": bool(artifact),
            "policy_fingerprint": policy_value,
            "observed_at": observed_at,
            "reason": "",
        }
        values.append(observation)
    result = tuple(values)
    return result


def record_resolution_feedback(
    core,
    resolution_request_id: str,
    feedback_request_id: str,
    outcome: str,
    statement_id: str,
    reason: str = "",
) -> dict[str, object]:
    """Apply one explicit external verdict to an observed resolution candidate."""

    with core.lock:
        core.require_running()
        require_any_text(resolution_request_id, "resolution_request_id", MAX_REQUEST_ID_BYTES, blank_is_empty=True)
        require_any_text(feedback_request_id, "feedback_request_id", MAX_REQUEST_ID_BYTES, blank_is_empty=True)
        require_any_text(statement_id, "statement_id", MAX_ARTIFACT_ID_BYTES, blank_is_empty=True)
        require_any_text(reason, "reason", MAX_FEEDBACK_REASON_BYTES, allow_empty=True)
        try:
            verdict = FeedbackOutcome(outcome)
        except (TypeError, ValueError) as err:
            supported = ", ".join(value.value for value in FeedbackOutcome if value != FeedbackOutcome.CANDIDATE)
            raise InvalidRequestError(f"feedback outcome must be one of: {supported}") from err
        if verdict == FeedbackOutcome.CANDIDATE:
            raise InvalidRequestError("candidate is an internal observation, not an external feedback outcome")
        record = core.resolution_requests.get(resolution_request_id, {})
        if not record:
            raise ResourceNotFoundError("unknown or expired resolution_request_id")
        core.ensure_resolution_candidacy(resolution_request_id, record)
        candidate_ids = record.get("candidate_statement_ids", ())
        if not isinstance(candidate_ids, tuple) or statement_id not in candidate_ids:
            raise InvalidRequestError("statement_id is not a candidate in this resolution request")
        candidacy = record.get("candidacy_observations", ())
        if not isinstance(candidacy, tuple):
            raise LifecycleError("resolution candidacy observations are malformed")
        target = feedback_target(candidacy, statement_id)
        observed_at = canonical_utc(core.internal_clock())
        observation = validate_feedback_observation(
            {
                **target,
                "kind": FeedbackObservationKind.VERDICT,
                "outcome": verdict,
                "observed_at": observed_at,
                "reason": reason,
            }
        )
        lifecycle_status = LifecycleHandoffStatus.NOT_APPLICABLE
        if verdict == FeedbackOutcome.REJECTED_STALE:
            probe = core.engram.feedback_store.prepare(feedback_request_id, (observation,))
            if probe.get("replayed", False):
                probe_receipt = mutation_receipt_to_dict(probe.get("receipt", {}))
                probe_result = probe_receipt.get("result", False)
                if not isinstance(probe_result, dict):
                    raise LifecycleError("feedback replay lifecycle result is malformed")
                lifecycle_status = LifecycleHandoffStatus(probe_result.get("lifecycle_status", ""))
            elif target.get("generation_available", False):
                lifecycle_status = LifecycleHandoffStatus.PENDING
                try:
                    core.response_mutations.invalidate_response(
                        statement_id,
                        target.get("generation", 0),
                        LifecycleMutationReason.STALE,
                        "regulator-feedback",
                        feedback_mutation_request_id("stale-lifecycle", feedback_request_id),
                        "external regulator marked the observed candidate generation stale",
                    )
                    lifecycle_status = LifecycleHandoffStatus.COMPLETED
                except (ConflictError, ResourceNotFoundError, InvalidRequestError) as err:
                    LOGGER.info("stale-lifecycle handoff conflicted", exc_info=err)
                    lifecycle_status = LifecycleHandoffStatus.CONFLICTED
                except MutationCoordinationError as err:
                    LOGGER.error("stale-lifecycle handoff failed", exc_info=err)
                    lifecycle_status = LifecycleHandoffStatus.FAILED
            else:
                lifecycle_status = LifecycleHandoffStatus.PENDING
        result = core.apply_feedback(feedback_request_id, (observation,), lifecycle_status)
        result.update(
            {
                "resolution_request_id": resolution_request_id,
                "outcome": verdict.value,
                "statement_id": statement_id,
                "reason": reason,
            }
        )
        if not result.get("idempotent", False):
            core.record_regulator_telemetry(verdict.value)
        return result


def inspect_feedback_learning(core, limit: int = 64) -> dict:
    """Return bounded transport-neutral Section 6 state and counters."""

    with core.lock:
        core.require_running()
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 64:
            raise InvalidRequestError("feedback inspection limit must be an integer from 1 through 64")
        result = {
            "feedback": core.engram.feedback_store.inspect(limit),
            "negative_resolution": core.negative_resolutions.inspect(limit),
        }
        return result
