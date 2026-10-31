"""Response proposals for the Engram service.

Proposing a candidate response, resolving a proposal to accepted or rejected, and
completing the resolution once its candidate is settled. Each function that takes
``core`` is bound on ``engram.service.EngramCore`` as the method of the same name.
"""

from collections import Counter
from copy import deepcopy
from uuid import uuid4

from engram import sessions
from engram.artifacts import cached_response_artifact_to_dict
from engram.constants import DEFAULT_PROPOSE_LIMIT, DEFAULT_USER_ID, EMPTY_METADATA, REGULATOR_OUTCOMES
from engram.constants_artifacts import MAX_ARTIFACT_ID_BYTES, MAX_SOURCE_LABEL_BYTES, LifecycleState, Tier
from engram.constants_feedback import MAX_FEEDBACK_REASON_BYTES, FeedbackObservationKind, FeedbackOutcome, LifecycleHandoffStatus
from engram.constants_identity import MAX_CONTEXT_FINGERPRINT_BYTES, MAX_NAMESPACE_BYTES, ExactLookupOutcome
from engram.constants_mutations import MAX_REQUEST_ID_BYTES
from engram.constants_resolution import MAX_REASON_CODE_BYTES
from engram.eligibility import capture_standalone_eligibility_context
from engram.errors import ConflictError, InvalidRequestError, LifecycleError, ResourceNotFoundError
from engram.feedback import validate_feedback_observation
from engram.feedback_values import canonical_utc
from engram.fusion import policy_fingerprint
from engram.identity import scope_key, scoped_retrieval_key_from_text
from engram.service_feedback import feedback_mutation_request_id, feedback_target
from engram.service_requests import (
    accounting_request_id,
    normalize_service_user_id,
    require_cache_request,
    service_request_signature,
)
from engram.validation import require_any_text


def artifact_candidate_result(artifact: dict, score: float) -> dict:
    """Build the proposal view directly from one accepted-response artifact."""
    statistics = artifact.get("statistics", {})
    provenance = artifact.get("provenance", {})
    tier = artifact.get("tier", Tier.DYNAMIC)
    if not isinstance(statistics, dict) or not isinstance(provenance, dict) or not isinstance(tier, Tier):
        raise LifecycleError("accepted-response artifact contains malformed candidate fields")
    result = {
        "statement_id": artifact.get("statement_id", ""),
        "response": artifact.get("response", ""),
        "score": score,
        "tier": tier.value,
        "created_at": provenance.get("accepted_at", ""),
        "hit_count": statistics.get("hit_count", 0),
        "query_count": statistics.get("query_count", 0),
        "source_label": provenance.get("source_label", ""),
        "introduced_by_user_id": provenance.get("caller_id", ""),
        "metadata": cached_response_artifact_to_dict(artifact).get("metadata", {}),
    }
    return result


def service_proposal_result(record: dict, *, idempotent: bool) -> dict:
    """Return an isolated proposal view with retry status."""
    result = deepcopy(record.get("proposal", {}))
    result["idempotent"] = idempotent
    return result


def propose(
    core,
    request: str,
    request_id: str,
    user_id: str = DEFAULT_USER_ID,
    namespace: str = "",
    context_fingerprint: str = "",
    limit: int = DEFAULT_PROPOSE_LIMIT,
    required_metadata: dict = EMPTY_METADATA,
    required_source_label: str = "",
) -> dict:
    """Create a speculative, uncredited response-cache proposal."""
    with core.lock:
        core.require_running()
        require_cache_request(request)
        require_any_text(request_id, "request_id", MAX_REQUEST_ID_BYTES, blank_is_empty=True)
        require_any_text(namespace, "namespace", MAX_NAMESPACE_BYTES, allow_empty=True)
        require_any_text(context_fingerprint, "context_fingerprint", MAX_CONTEXT_FINGERPRINT_BYTES, allow_empty=True)
        require_any_text(required_source_label, "required_source_label", MAX_SOURCE_LABEL_BYTES, allow_empty=True)
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 10:
            raise InvalidRequestError("limit must be an integer from 1 through 10")
        if not isinstance(required_metadata, dict):
            raise InvalidRequestError("required_metadata must be an object")
        required_metadata = dict(required_metadata)
        normalized_user_id = normalize_service_user_id(user_id)
        signature = service_request_signature(
            request=request,
            user_id=normalized_user_id,
            namespace=namespace,
            context_fingerprint=context_fingerprint,
            limit=limit,
            required_metadata=required_metadata,
            required_source_label=required_source_label,
        )

        existing_proposal_id = core.proposal_requests.get(request_id, "")
        if existing_proposal_id:
            existing = core.proposals.get(existing_proposal_id, {})
            if existing.get("signature", "") != signature:
                raise ConflictError("request_id is already associated with a different proposal request")
            core.ensure_proposal_candidacy(existing_proposal_id, existing)
            core.regulated_metrics["idempotent_retries"] += 1
            result = service_proposal_result(existing, idempotent=True)
            return result

        scope = scope_key(namespace=namespace, context_fingerprint=context_fingerprint)
        feedback_frame = core.query_frame_builder.build(
            request,
            scope,
            required_metadata=required_metadata,
            required_source_label=required_source_label,
            diagnostic_seed=f"proposal:{request_id}",
        )
        feedback_policy_value = policy_fingerprint(core.resolution_orchestrator.internal_fusion.policy)
        response_artifacts = core.engram.response_repository.trusted_artifacts()

        def artifact_matches_scope(artifact: dict) -> bool:
            statement_id = str(artifact.get("statement_id", ""))
            generation = artifact.get("generation", 0)
            if isinstance(generation, bool) or not isinstance(generation, int):
                raise LifecycleError("accepted-response artifact generation is malformed")
            if core.engram.feedback_store.trusted_stale_excluded(statement_id, generation, True):
                return False
            if core.engram.feedback_store.trusted_policy_suppressed(statement_id, namespace, feedback_policy_value):
                return False
            if (
                artifact.get("scope", {}) != scope
                or getattr(artifact.get("lifecycle", LifecycleState.RETIRED), "value", "") != "ACTIVE"
            ):
                return False
            provenance = artifact.get("provenance", {})
            if required_source_label and provenance.get("source_label", "") != required_source_label:
                return False
            artifact_metadata = artifact.get("metadata", {})
            if not isinstance(provenance, dict) or not isinstance(artifact_metadata, dict):
                raise LifecycleError("accepted-response artifact metadata is malformed")
            result = all(
                key in artifact_metadata and artifact_metadata.get(key, {}) == value for key, value in required_metadata.items()
            )
            return result

        exact_artifact: dict[str, object] = {}
        try:
            eligibility_context = capture_standalone_eligibility_context(core.internal_clock, scope, True)
            exact = core.engram.response_repository.exact_lookup(
                scoped_retrieval_key_from_text(scope, request),
                eligibility_context,
            )
            lookup = exact.get("lookup", {})
            if lookup.get("outcome", ExactLookupOutcome.MISS) == ExactLookupOutcome.FOUND:
                found = response_artifacts.get(lookup.get("statement_id", ""), {})
                if found and artifact_matches_scope(found):
                    exact_artifact = found
        except InvalidRequestError:
            exact_artifact = {}
        sparse_matches = ()
        if not exact_artifact:
            sparse_discovery = core.engram.sparse_candidates(
                request,
                scope,
                limit=limit,
                max_working_memory_bytes=64 * 1024 * 1024,
            )
            if sparse_discovery.get("complete", False):
                sparse_matches = tuple(
                    (response_artifacts.get(match.get("statement_id", ""), {}), float(match.get("score", 0.0)))
                    for match in sparse_discovery.get("matches", ())
                )
        vector_matches = (
            ()
            if exact_artifact
            else core.engram.vector_supported_matches(
                request,
                limit=limit,
                artifact_filter=artifact_matches_scope,
            )
        )
        merged = {}
        match_sources = (
            (("exact", ((exact_artifact, 1.0),)),) if exact_artifact else (("keyword", sparse_matches), ("vector", vector_matches))
        )
        for source, matches in match_sources:
            for artifact, score in matches:
                if not artifact or not artifact_matches_scope(artifact):
                    continue
                statement_id = artifact.get("statement_id", "")
                entry = merged.setdefault(
                    statement_id,
                    {
                        "artifact": artifact,
                        "keyword_score": 0.0,
                        "vector_score": 0.0,
                    },
                )
                score_field = "keyword_score" if source == "exact" else f"{source}_score"
                entry[score_field] = max(float(entry.get(score_field, 0.0)), float(score))
        ranked = sorted(
            merged.values(),
            key=lambda entry: (
                max(entry.get("keyword_score", 0.0), entry.get("vector_score", 0.0)),
                entry.get("artifact", {}).get("provenance", {}).get("accepted_at", ""),
                entry.get("artifact", {}).get("statement_id", ""),
            ),
            reverse=True,
        )[:limit]
        candidates = []
        queried_response_ids = []
        for entry in ranked:
            artifact = entry.get("artifact", {})
            keyword_score = entry.get("keyword_score", 0.0)
            vector_score = entry.get("vector_score", 0.0)
            statement_id = artifact.get("statement_id", "")
            selected_score = max(keyword_score, vector_score)
            queried_response_ids.append(statement_id)
            candidate = artifact_candidate_result(artifact, selected_score)
            candidate["query_count"] = candidate.get("query_count", 0) + 1
            candidate["retrieval"] = {
                "keyword_score": keyword_score,
                "vector_score": vector_score,
                "selected": "exact" if exact_artifact else "vector" if vector_score > keyword_score else "keyword",
            }
            candidates.append(candidate)
        proposal_id = f"proposal_{uuid4().hex[:16]}"
        proposal = {
            "proposal_id": proposal_id,
            "request_id": request_id,
            "user_id": normalized_user_id,
            "namespace": namespace,
            "context_fingerprint": context_fingerprint,
            "resolved_request": feedback_frame.get("resolved_text", ""),
            "keywords": list(feedback_frame.get("identity", {}).get("lexical_terms", ())),
            "candidates": candidates,
        }
        candidate_responses = {candidate.get("statement_id", ""): candidate.get("response", "") for candidate in candidates}
        record = {
            "signature": signature,
            "proposal": proposal,
            "candidate_responses": candidate_responses,
            "resolution": {},
            "resolution_signature": "",
            "pending_resolution": {},
            "feedback_frame": feedback_frame,
            "candidacy_observations": (),
            "candidacy_applied": not candidates,
        }
        core.proposals[proposal_id] = record
        core.proposal_requests[request_id] = proposal_id
        core.regulated_metrics["proposals"] += 1
        if not candidates:
            core.regulated_metrics["misses"] += 1
        core.enforce_transient_bound()
        if queried_response_ids:
            core.response_mutations.record_response_queries(
                tuple(sorted(queried_response_ids)),
                accounting_request_id("response-query", request_id),
            )
        if candidates:
            record["candidacy_observations"] = core.proposal_feedback_observations(
                feedback_frame,
                proposal_id,
                tuple(sorted(candidate_responses)),
            )
            core.ensure_proposal_candidacy(proposal_id, record)
        result = service_proposal_result(record, idempotent=False)
        return result


def resolve(core, proposal_id: str, outcome: str, statement_id: str = "", reason: str = "") -> dict:
    """Commit one Regulator verdict without double-crediting retries."""
    with core.lock:
        core.require_running()
        require_any_text(proposal_id, "proposal_id", MAX_REQUEST_ID_BYTES, blank_is_empty=True)
        require_any_text(outcome, "outcome", MAX_REASON_CODE_BYTES, allow_empty=True)
        require_any_text(statement_id, "statement_id", MAX_ARTIFACT_ID_BYTES, allow_empty=True)
        require_any_text(reason, "reason", MAX_FEEDBACK_REASON_BYTES, allow_empty=True)
        if outcome not in REGULATOR_OUTCOMES:
            supported = ", ".join(sorted(REGULATOR_OUTCOMES))
            raise InvalidRequestError(f"outcome must be one of: {supported}")

        record = core.proposals.get(proposal_id, {})
        if not record:
            raise ResourceNotFoundError("unknown or expired proposal_id")
        core.ensure_proposal_candidacy(proposal_id, record)
        resolution_signature = service_request_signature(outcome=outcome, statement_id=statement_id, reason=reason)
        prior_signature = record.get("resolution_signature", "")
        if prior_signature:
            if prior_signature != resolution_signature:
                raise ConflictError("proposal has already been resolved with a different verdict")
            if not record.get("resolution", {}):
                # The verdict was applied but its completion failed; finish it through the
                # same idempotent hit request before reporting it resolved.
                result = core.complete_proposal_resolution(proposal_id, record)
                return result
            core.regulated_metrics["idempotent_retries"] += 1
            result = deepcopy(record.get("resolution", {}))
            result["idempotent"] = True
            return result

        candidate_responses = record.get("candidate_responses", {})
        if not statement_id:
            raise InvalidRequestError("Regulator outcomes require statement_id")
        if statement_id not in candidate_responses:
            raise InvalidRequestError("statement_id is not a candidate in this proposal")
        if outcome == "accepted":
            core.current_proposal_candidate(record, statement_id)
        observations = record.get("candidacy_observations", ())
        if not isinstance(observations, tuple):
            raise LifecycleError("proposal candidacy observations are malformed")
        target = feedback_target(observations, statement_id)
        feedback_outcome = FeedbackOutcome(outcome)
        observed_at = canonical_utc(core.internal_clock())
        verdict_observation = validate_feedback_observation(
            {
                **target,
                "kind": FeedbackObservationKind.VERDICT,
                "outcome": feedback_outcome,
                "observed_at": observed_at,
                "reason": reason,
            }
        )
        verdict_feedback_request_id = feedback_mutation_request_id("proposal-verdict", proposal_id)
        lifecycle_status = LifecycleHandoffStatus.NOT_APPLICABLE
        core.apply_feedback(
            verdict_feedback_request_id,
            (verdict_observation,),
            lifecycle_status,
        )
        # The verdict is applied but stays pending until its accepted hit commits. A retry
        # with this verdict finishes it; a different verdict still conflicts.
        record["resolution_signature"] = resolution_signature
        record["pending_resolution"] = {
            "proposal_id": proposal_id,
            "outcome": outcome,
            "statement_id": statement_id,
            "reason": reason,
            "lifecycle_status": lifecycle_status.value,
            "resolved": True,
            "idempotent": False,
        }
        result = core.complete_proposal_resolution(proposal_id, record)
        return result


def current_proposal_candidate(core, record: dict, statement_id: str) -> dict:
    """Return the proposed artifact while it is still the accepted candidate, or raise a conflict.

    The same text survives a retire, supersede, or invalidation, so an accepted verdict
    also requires the artifact to be ACTIVE. Generation is not compared: proposal
    accounting advances it.
    """
    current_artifact = core.engram.response_repository.get_artifact(statement_id)
    if (
        current_artifact.get("response", "") != record.get("candidate_responses", {}).get(statement_id, "")
        or getattr(current_artifact.get("lifecycle", LifecycleState.RETIRED), "value", "") != "ACTIVE"
    ):
        raise ConflictError("candidate is no longer current; resolve it as rejected_stale")
    return current_artifact


def complete_proposal_resolution(core, proposal_id: str, record: dict) -> dict:
    """Commit a pending verdict's accepted hit, then publish and count the verdict once.

    The hit uses one idempotent accounting request, so a retry after a failure here
    replays a hit that did commit instead of crediting it again. The session context,
    metrics, and published resolution follow the hit, so they occur once.
    """
    resolution = record.get("pending_resolution", {})
    outcome = resolution.get("outcome", "")
    if outcome == "accepted":
        statement_id = resolution.get("statement_id", "")
        current_artifact = core.current_proposal_candidate(record, statement_id)
        core.response_mutations.record_response_hit(statement_id, accounting_request_id("response-hit", proposal_id))
        proposal = record.get("proposal", {})
        if proposal.get("user_id", "") != DEFAULT_USER_ID:
            with core.engram.session_lock:
                sessions.get_session(core.engram, proposal.get("user_id", ""), create_if_missing=True)
                sessions.update_session_context(core.engram, proposal.get("user_id", ""), current_artifact.get("response", ""))
        core.regulated_metrics["accepted"] += 1
    else:
        rejections = core.regulated_metrics.get("rejections", Counter())
        rejections[outcome] += 1
    core.record_regulator_telemetry(outcome)
    record["resolution"] = resolution
    record["pending_resolution"] = {}
    result = deepcopy(resolution)
    return result
