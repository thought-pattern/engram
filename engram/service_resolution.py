"""Cache resolution for the Engram service.

Resolves one cache request against the accepted-response artifacts, including the
bounded negative cache for misses. Each function that takes ``core`` is bound on
``engram.service.EngramCore`` as the method of the same name.
"""

from json import dumps as json_dumps
from logging import getLogger as logging_getLogger
from time import monotonic_ns as time_monotonic_ns

from engram import sessions
from engram.constants import DEFAULT_USER_ID, EMPTY_MAPPING, EMPTY_METADATA, RolloutMode
from engram.constants_artifacts import MAX_SOURCE_LABEL_BYTES
from engram.constants_feedback import FeedbackObservationKind, FeedbackOutcome, FeedbackReferenceKind
from engram.constants_identity import MAX_CONTEXT_FINGERPRINT_BYTES, MAX_NAMESPACE_BYTES
from engram.constants_mutations import MAX_REQUEST_ID_BYTES
from engram.constants_relation import MAX_CONTEXTUAL_TURN, ExpectedObjectType
from engram.constants_resolution import ResolutionOutcome, ResolverState
from engram.contextual import compact_query_frame_from_frame, enrich_query_frame
from engram.errors import (
    ConflictError,
    IdentityValidationError,
    InvalidRequestError,
    LifecycleError,
    ResolutionCancelledError,
    WorkingMemoryExceededError,
)
from engram.feedback_values import canonical_fingerprint, constraint_fingerprint
from engram.fusion import policy_fingerprint
from engram.identity import query_identity_to_dict, scope_key, validate_query_identity
from engram.negative_resolution import empty_negative_resolution, negative_resolution_key
from engram.resolution import empty_candidate, resolution_result, resolution_result_to_json, validate_resolution_result
from engram.resolution_budget import (
    budget_consumption,
    budget_consumption_with_changes,
    resolution_budget,
    resolution_budget_to_dict,
    validate_resolution_budget,
)
from engram.resolver_plan import resolver_contract, trusted_resolution_plan_to_dict
from engram.rewrite import RewriteEngine, apply_rewrites_to_frame
from engram.rollout import apply_rollout, select_rollout
from engram.service_requests import (
    no_cancellation_check,
    normalize_service_user_id,
    require_cache_request,
    service_request_signature,
)
from engram.validation import require_any_text

LOGGER = logging_getLogger("engram.service")


def negative_resolution_admissible(value: dict, plan) -> bool:
    """Return whether a complete knowledge miss is safe to cache negatively."""
    current = value
    outcome = current.get("outcome", ResolutionOutcome.ANSWER)
    resolver_results = current.get("resolver_results", ())
    if outcome != ResolutionOutcome.MISS or current.get("budget", {}).get("exhausted_dimensions", ()):
        result = False
        return result
    if "output_truncated" in current.get("reason_codes", ()):
        result = False
        return result
    if any(value.get("candidates", ()) or value.get("evidence", ()) or value.get("accounting", {}) for value in resolver_results):
        result = False
        return result
    results = {item.get("resolver", ""): item for item in resolver_results}
    configured = [entry for entry in plan.get("entries", ()) if entry.get("configured", False)]
    if not configured or any(not entry.get("available", False) for entry in configured):
        result = False
        return result
    knowledge_miss_reasons = {
        "exact": "exact_MISS",
        "sparse": "sparse_miss",
        "standalone_semantic": "semantic_miss",
        "utility": "utility_miss",
        "structured_graph": "structured_graph_miss",
        "support_semantic": "support_semantic_miss",
    }
    for entry in configured:
        resolver_name, _, _, _ = resolver_contract(entry.get("resolver", {}))
        resolver_result = results.get(resolver_name, {})
        if not resolver_result:
            result = False
            return result
        expected_reason = knowledge_miss_reasons.get(resolver_name, "")
        state = resolver_result.get("state", ResolverState.FAILED)
        if state != ResolverState.COMPLETED or resolver_result.get("reason_code", "") != expected_reason:
            result = False
            return result
        diagnostics = resolver_result.get("diagnostics", {})
        if resolver_name == "exact" and (diagnostics.get("owner_count", 0) != 0 or diagnostics.get("truncated", False)):
            result = False
            return result
    result = True
    return result


def bounded_miss_result(
    frame: dict,
    started_ns: int,
    reason_codes: tuple[str, ...],
    diagnostics: dict[str, object],
) -> dict:
    """Build an exactly accounted resolver-free MISS result."""
    current_ns = time_monotonic_ns()
    elapsed_ns = max(0, current_ns - started_ns)
    budget = frame.get("budget", {})
    exhausted = set()
    selected_diagnostics = {
        "diagnostic_id": frame.get("diagnostic_id", ""),
        **diagnostics,
        "accounting": {"candidate_count": 0, "accepted_present": False, "success_applied": False},
    }
    diagnostic_bytes = len(json_dumps(selected_diagnostics, sort_keys=True, separators=(",", ":")).encode("utf-8"))
    if diagnostic_bytes > budget.get("max_diagnostic_bytes", 0):
        selected_diagnostics = {}
        diagnostic_bytes = 0
        exhausted.add("diagnostic_bytes")
    consumption = budget_consumption(
        elapsed_ns=elapsed_ns,
        diagnostic_bytes=diagnostic_bytes,
        exhausted_dimensions=tuple(sorted(exhausted)),
    )
    for _ in range(8):
        result = resolution_result(
            outcome=ResolutionOutcome.MISS,
            selected_candidate=empty_candidate(),
            selected_candidate_available=False,
            response_candidates=(),
            evidence=(),
            confidence=0.0,
            confidence_available=False,
            reason_codes=reason_codes,
            frame_diagnostics=selected_diagnostics,
            resolver_results=(),
            budget=consumption,
        )
        size = len(resolution_result_to_json(result).encode("utf-8"))
        if size > budget.get("max_output_bytes", 0):
            raise InvalidRequestError("negative resolution result exceeds max_output_bytes")
        if size > budget.get("max_working_memory_bytes", 0):
            raise WorkingMemoryExceededError("negative resolution result exceeds max_working_memory_bytes")
        updated = budget_consumption_with_changes(consumption, {"output_bytes": size, "working_memory_bytes": size})
        if updated == consumption:
            return result
        consumption = updated
    raise InvalidRequestError("negative resolution budget accounting did not converge")


def negative_hit_result(frame: dict, started_ns: int) -> dict:
    """Build an exactly accounted MISS result for a negative-cache hit."""
    result = bounded_miss_result(
        frame,
        started_ns,
        ("negative_resolution_hit", "insufficient_knowledge"),
        {"negative_resolution": {"hit": True, "reason": "insufficient_knowledge", "memory_only": True}},
    )
    return result


def internal_negative_key(core, frame: dict, plan) -> tuple[dict, bool]:
    context = frame.get("eligibility_context", {})
    if not (context.get("evaluation_time_available", False) and context.get("artifact_repository_available", False)):
        result = empty_negative_resolution().get("key", {}), False
        return result
    configured = tuple(
        resolver_contract(entry.get("resolver", {}))[0] for entry in plan.get("entries", ()) if entry.get("configured", False)
    )
    plan_entries = trusted_resolution_plan_to_dict(plan).get("entries", False)
    if not isinstance(plan_entries, list):
        raise LifecycleError("resolution plan entries are malformed")
    resolver_plan = [
        {
            "resolver": value.get("resolver", ""),
            "cost_class": value.get("cost_class", ""),
            "order": value.get("order", 0),
            "configured": value.get("configured", False),
        }
        for value in plan_entries
    ]
    readiness = [
        {
            "resolver": value.get("resolver", ""),
            "configured": value.get("configured", False),
            "available": value.get("available", False),
            "reason_code": value.get("reason_code", ""),
        }
        for value in plan_entries
    ]
    key = negative_resolution_key(
        query_identity=frame.get("identity", {}),
        scope=frame.get("scope", {}),
        constraint_fingerprint=constraint_fingerprint(
            frame.get("expected_object_type", ExpectedObjectType.UNKNOWN).value,
            frame.get("required_metadata", {}),
            frame.get("required_source_label", ""),
        ),
        resolver_plan_fingerprint=canonical_fingerprint(resolver_plan),
        capability_readiness_fingerprint=canonical_fingerprint(readiness),
        policy_fingerprint=policy_fingerprint(core.resolution_orchestrator.internal_fusion.policy),
    )
    # Negative entries apply to exact-only plans; broader resolver plans
    # invalidate an exact-only miss for the same relationship.
    if configured != ("exact",):
        core.negative_resolutions.invalidate_for_key(key)
        result = key, False
        return result
    result = key, True
    return result


def resolve_request(
    core,
    request: str,
    request_id: str,
    *,
    user_id: str = DEFAULT_USER_ID,
    namespace: str = "",
    context_fingerprint: str = "",
    identity: dict[str, object] = EMPTY_MAPPING,
    required_metadata: dict = EMPTY_METADATA,
    required_source_label: str = "",
    budget: dict[str, object] = EMPTY_MAPPING,
    configured_resolvers: tuple[str, ...] = (),
    accept_exact: bool = False,
    cancellation_check: object = (),
) -> dict:
    """Run one transport-neutral bounded resolution pipeline."""
    require_cache_request(request)
    require_any_text(request_id, "request_id", MAX_REQUEST_ID_BYTES, blank_is_empty=True)
    normalized_user_id = normalize_service_user_id(user_id)
    if cancellation_check != () and not callable(cancellation_check):
        raise InvalidRequestError("cancellation_check must be callable")
    selected_cancellation_check = cancellation_check if callable(cancellation_check) else no_cancellation_check

    def check_cancellation() -> None:
        selected_cancellation_check()

    with core.resolution_slot(request_id, normalized_user_id, check_cancellation), core.lock:
        core.require_running()
        require_any_text(namespace, "namespace", MAX_NAMESPACE_BYTES, allow_empty=True)
        require_any_text(context_fingerprint, "context_fingerprint", MAX_CONTEXT_FINGERPRINT_BYTES, allow_empty=True)
        require_any_text(required_source_label, "required_source_label", MAX_SOURCE_LABEL_BYTES, allow_empty=True)
        if not isinstance(identity, dict):
            raise InvalidRequestError("identity must be an object")
        if not isinstance(budget, dict):
            raise InvalidRequestError("budget must be an object")
        selected_identity = {}
        if identity:
            try:
                selected_identity = validate_query_identity(identity)
            except IdentityValidationError as err:
                raise InvalidRequestError("identity must be a QueryIdentity") from err
        selected_budget: object = EMPTY_MAPPING
        if budget:
            try:
                selected_budget = validate_resolution_budget(budget)
            except InvalidRequestError as err:
                raise InvalidRequestError("budget must be a ResolutionBudget") from err
        if not isinstance(required_metadata, dict):
            raise InvalidRequestError("required_metadata must be an object")
        if not isinstance(configured_resolvers, tuple) or not all(isinstance(name, str) and name for name in configured_resolvers):
            raise InvalidRequestError("configured_resolvers must be a tuple of non-empty strings")
        if not isinstance(accept_exact, bool):
            raise InvalidRequestError("accept_exact must be a boolean")
        check_cancellation()
        rollout = select_rollout(core.engram.config, namespace)
        rollout_mode = rollout.get("mode", RolloutMode.DISABLED)
        selected_resolvers = ("exact",) if rollout_mode == RolloutMode.ROLLBACK else configured_resolvers
        graph_enabled = bool(core.engram.config.get("graph", {}).get("enabled", False))
        if graph_enabled and selected_resolvers and "structured_graph" not in selected_resolvers:
            selected_resolvers = (*selected_resolvers, "structured_graph")
        signature_budget = resolution_budget_to_dict(selected_budget if budget else resolution_budget())
        signature_budget.pop("started_ns", 0)
        signature = service_request_signature(
            request=request,
            user_id=normalized_user_id,
            namespace=namespace,
            context_fingerprint=context_fingerprint,
            identity=query_identity_to_dict(selected_identity) if selected_identity else {},
            required_metadata=required_metadata,
            required_source_label=required_source_label,
            budget=signature_budget,
            configured_resolvers=list(selected_resolvers),
            accept_exact=accept_exact,
            rollout_mode=rollout_mode.value,
        )
        if request_id in core.resolution_requests:
            check_cancellation()
            prior = core.resolution_requests.get(request_id, {})
            prior_signature = prior.get("signature", "")
            if prior_signature != signature:
                raise ConflictError("resolution request_id is associated with different input")
            core.ensure_resolution_candidacy(request_id, prior)
            prior_result = prior.get("result", {})
            try:
                prior_result = validate_resolution_result(prior_result)
            except InvalidRequestError as err:
                raise LifecycleError("resolution request cache is malformed") from err
            core.record_resolution_telemetry(prior_result, replayed=True)
            return prior_result
        scope = scope_key(namespace=namespace, context_fingerprint=context_fingerprint)
        frame = core.query_frame_builder.build(
            request,
            scope,
            identity=selected_identity,
            required_metadata=required_metadata,
            required_source_label=required_source_label,
            diagnostic_seed=request_id,
            budget=selected_budget,
        )
        with core.engram.session_lock:
            session = sessions.get_session(core.engram, normalized_user_id, create_if_missing=True)
            prior_turn = session.get("query_frame_turn", 0)
            if isinstance(prior_turn, bool) or not isinstance(prior_turn, int) or not 0 <= prior_turn <= MAX_CONTEXTUAL_TURN:
                raise LifecycleError("user contextual query-frame turn is malformed")
            if prior_turn == MAX_CONTEXTUAL_TURN:
                prior_turn = 0
                session["previous_query_frame"] = {}
            current_turn = prior_turn + 1
            previous_query_frame = session.get("previous_query_frame", {})
            if not isinstance(previous_query_frame, dict):
                raise LifecycleError("user previous query frame is malformed")
            active_topic = session.get("active_topic", "")
            if not isinstance(active_topic, str):
                raise LifecycleError("user contextual topic is malformed")
            predicates = session.get("predicates", {})
            if not isinstance(predicates, dict):
                raise LifecycleError("user predicates are malformed")
            predicate_topic = predicates.get("topic", "")
            if not isinstance(predicate_topic, str):
                raise LifecycleError("user topic predicate is malformed")
            topic = active_topic or predicate_topic
        frame = enrich_query_frame(
            frame,
            previous=previous_query_frame,
            current_turn=current_turn,
            topic=topic,
        )
        if isinstance(core.rewrite_engine, RewriteEngine):
            frame = apply_rewrites_to_frame(frame, core.rewrite_engine, check_cancellation)

        def remember_contextual_frame() -> None:
            compact = compact_query_frame_from_frame(frame, source_turn=current_turn, topic=topic)
            with core.engram.session_lock:
                session["previous_query_frame"] = compact
                session["query_frame_turn"] = current_turn
                session["last_active"] = core.internal_clock()

        evaluation_time = frame.get("eligibility_context", {}).get("evaluation_time", "")
        negative_started_ns = time_monotonic_ns()
        plan = core.resolver_registry.plan(frame, selected_resolvers)
        negative_key = empty_negative_resolution().get("key", {})
        negative_key_available = False
        negative_hit = False
        negative_result: object = {}
        try:
            check_cancellation()
            negative_key, negative_key_available = core.internal_negative_key(frame, plan)
            if rollout_mode == RolloutMode.SHADOW:
                negative_key_available = False
            if negative_key_available:
                negative_lookup = core.negative_resolutions.lookup(negative_key, evaluation_time)
                if negative_lookup.get("hit", False):
                    negative_result = negative_hit_result(frame, negative_started_ns)
                    negative_hit = True
        except ResolutionCancelledError:
            raise
        except Exception as err:
            # Negative-cache ownership and lookup are optional optimizations.
            LOGGER.warning("negative-resolution lookup failed closed", exc_info=err)
            negative_key_available = False
            negative_hit = False
        if negative_hit:
            public_result = apply_rollout(validate_resolution_result(negative_result), rollout)
            public_result = core.cache_resolution(request_id, signature, public_result, frame, (), ())
            remember_contextual_frame()
            core.record_resolution_telemetry(public_result, replayed=False)
            return public_result
        raw_result, finalization = core.resolution_orchestrator.resolve_with_plan(
            frame,
            request_id,
            plan,
            accept_exact=accept_exact,
            cooperative_check=selected_cancellation_check,
            # Only a released direct answer earns success credit; other modes observe it.
            credit_success=rollout_mode == RolloutMode.REGULATED_DIRECT_ANSWER,
        )
        if negative_key_available and negative_resolution_admissible(raw_result, plan):
            try:
                core.negative_resolutions.admit(negative_key, evaluation_time)
            except Exception as err:
                LOGGER.warning("negative-resolution admission failed", exc_info=err)
        result = apply_rollout(raw_result, rollout)
        candidate_statement_ids = finalization.get("candidate_statement_ids", ()) if rollout_mode != RolloutMode.SHADOW else ()
        candidacy_observations = core.feedback_observations(
            frame,
            result,
            candidate_statement_ids,
            reference_kind=FeedbackReferenceKind.RESOLUTION_REQUEST,
            reference_id=request_id,
            kind=FeedbackObservationKind.CANDIDACY,
            outcome=FeedbackOutcome.CANDIDATE,
            observed_at=evaluation_time,
        )
        public_result = core.cache_resolution(
            request_id,
            signature,
            result,
            frame,
            candidate_statement_ids,
            candidacy_observations,
        )
        remember_contextual_frame()
        core.record_resolution_telemetry(public_result, replayed=False)
        return public_result
