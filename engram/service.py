"""Transport-neutral application facade for Engram interfaces.

``EngramCore`` owns the shared engine, user-bound conversation runtimes,
and regulated in-memory response-cache transactions. Interfaces
such as MCP and the CLI translate their inputs and outputs at the boundary;
this module contains no transport-specific types or behavior.
"""

from collections import Counter
from contextlib import contextmanager as contextlib_contextmanager
from copy import deepcopy
from datetime import UTC, datetime
from hashlib import sha256 as hashlib_sha256
from json import dumps as json_dumps
from logging import getLogger as logging_getLogger
from threading import Condition as threading_Condition, RLock as threading_RLock
from time import monotonic as time_monotonic, monotonic_ns as time_monotonic_ns
from uuid import uuid4

from engram import sessions
from engram.artifacts import cached_response_artifact_to_dict
from engram.constants import (
    EARLIEST_UTC,
    EMPTY_CONFIG,
    EMPTY_MAPPING,
    EMPTY_METADATA,
    MAX_ARTIFACT_ID_BYTES,
    MAX_CALLER_ID_BYTES,
    MAX_CONTEXT_FINGERPRINT_BYTES,
    MAX_FEEDBACK_REASON_BYTES,
    MAX_METADATA_KEY_BYTES,
    MAX_METADATA_STRING_BYTES,
    MAX_NAMESPACE_BYTES,
    MAX_REASON_CODE_BYTES,
    MAX_REQUEST_BYTES,
    MAX_REQUEST_ID_BYTES,
    MAX_RESPONSE_BYTES,
    MAX_SIGNATURE_INPUT_BYTES,
    MAX_SOURCE_LABEL_BYTES,
    MAX_TRANSIENT_RECORDS,
    PROPOSAL_TTL_SECONDS,
    REGULATOR_OUTCOMES,
    CoreState,
    ExactLookupOutcome,
    ExpectedObjectType,
    LifecycleState,
    RolloutMode,
    Tier,
)
from engram.contextual import compact_query_frame_from_frame, enrich_query_frame
from engram.conversation import ConversationRuntime, statement_view
from engram.coordination import AtomicMutationCoordinator, MutationCoordinationError
from engram.core import Engram
from engram.eligibility import EligibilityContextCapture
from engram.errors import (
    ConflictError,
    IdentityValidationError,
    InvalidRequestError,
    LifecycleError,
    ResolutionCancelledError,
    ResourceNotFoundError,
)
from engram.feedback import (
    FeedbackObservationKind,
    FeedbackOutcome,
    FeedbackReferenceKind,
    LifecycleHandoffStatus,
    NegativeResolutionStore,
    canonical_fingerprint,
    canonical_utc,
    constraint_fingerprint,
    empty_negative_resolution,
    feedback_observation,
    negative_resolution_key,
    trusted_feedback_observation,
    validate_feedback_observation,
)
from engram.fusion import CandidateFusionEngine, EngramCandidateAuthority, fusion_policy, policy_fingerprint
from engram.identity import build_scoped_retrieval_key, query_identity_to_dict, scope_key, validate_query_identity
from engram.mutations import mutation_receipt_to_dict
from engram.repository import tier_admission_policy
from engram.resolution import (
    QueryFrameBuilder,
    ResolutionOutcome,
    ResolverState,
    budget_consumption,
    budget_consumption_with_changes,
    empty_candidate,
    resolution_budget,
    resolution_budget_to_dict,
    resolution_result,
    resolution_result_to_json,
    validate_resolution_budget,
    validate_resolution_result,
)
from engram.resolvers import (
    ExactResolver,
    ResolutionAccountingFinalizer,
    ResolutionOrchestrator,
    ResolverExecutor,
    ResolverRegistry,
    SparseResolver,
    StandaloneSemanticResolver,
    StructuredGraphResolver,
    SupportSemanticResolver,
    UtilityResolver,
    resolver_contract,
    trusted_resolution_plan_to_dict,
)
from engram.responses import AcceptedResponseService, LifecycleMutationReason, response_mutation_result_to_dict
from engram.rewrite import RewriteEngine, apply_rewrites_to_frame, load_default_rewrite_corpus
from engram.rollout import apply_rollout, rollout_status, select_rollout
from engram.telemetry import record_regulator_outcome, record_resolution
from engram.text import normalize

LOGGER = logging_getLogger("engram.service")


def feedback_target(observations: tuple[object, ...], statement_id: str) -> dict:
    target: object = {}
    for value in observations:
        try:
            observation = validate_feedback_observation(value)
        except InvalidRequestError:
            continue
        if observation["statement_id"] == statement_id:
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


def require_service_text(value: str, name: str, maximum_bytes: int) -> None:
    """Require a nonempty service-boundary string."""
    if not isinstance(value, str) or not value.strip():
        raise InvalidRequestError(f"{name} must be a non-empty string")
    try:
        size = len(value.encode("utf-8"))
    except UnicodeEncodeError as error:
        raise InvalidRequestError(f"{name} must contain valid Unicode") from error
    if size > maximum_bytes:
        raise InvalidRequestError(f"{name} exceeds the UTF-8 limit of {maximum_bytes} bytes")


def require_service_string(value: str, name: str, maximum_bytes: int) -> None:
    """Require a concrete service-boundary string, including an empty string."""
    if not isinstance(value, str):
        raise InvalidRequestError(f"{name} must be a string")
    try:
        size = len(value.encode("utf-8"))
    except UnicodeEncodeError as error:
        raise InvalidRequestError(f"{name} must contain valid Unicode") from error
    if size > maximum_bytes:
        raise InvalidRequestError(f"{name} exceeds the UTF-8 limit of {maximum_bytes} bytes")


def service_request_signature(**values) -> str:
    """Return deterministic JSON for an idempotent service request."""
    try:
        result = json_dumps(values, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError) as error:
        raise InvalidRequestError("metadata and request values must be JSON-compatible") from error
    try:
        encoded = result.encode("utf-8")
    except UnicodeEncodeError as error:
        raise InvalidRequestError("metadata and request values must contain valid Unicode") from error
    if len(encoded) > MAX_SIGNATURE_INPUT_BYTES:
        raise InvalidRequestError(f"service request exceeds the UTF-8 limit of {MAX_SIGNATURE_INPUT_BYTES} bytes")
    return result


def accounting_request_id(kind: str, external_id: str) -> str:
    """Derive a bounded, non-disclosing identity for internal accounting."""
    digest = hashlib_sha256(external_id.encode("utf-8")).hexdigest()
    result = f"internal:{kind}:sha256:{digest}"
    return result


class ServiceClock:
    """Validate one injectable wall clock at every read."""

    def __init__(self, operation: object = ()) -> None:
        if operation != () and not callable(operation):
            raise InvalidRequestError("clock must be callable")
        self.operation = operation

    def __call__(self) -> datetime:
        value = datetime.now(UTC) if self.operation == () else self.read_injected()
        if not isinstance(value, datetime):
            raise InvalidRequestError("clock must return a datetime")
        return value

    def read_injected(self) -> object:
        if not callable(self.operation):
            raise InvalidRequestError("clock must be callable")
        result = self.operation()
        return result


class IsolatedGraphClient:
    """Run optional graph calls through a core-owned isolation context."""

    def __init__(self, client, operation_context) -> None:
        if not callable(operation_context):
            raise InvalidRequestError("graph operation context must be callable")
        object.__setattr__(self, "client", client)
        object.__setattr__(self, "operation_context", operation_context)

    def __bool__(self) -> bool:
        result = bool(object.__getattribute__(self, "client"))
        return result

    def __getattr__(self, name: str):
        client = object.__getattribute__(self, "client")
        value = getattr(client, name)
        if not callable(value) or name == "disconnect":
            return value
        operation_context = object.__getattribute__(self, "operation_context")

        def isolated(*args, **kwargs):
            with operation_context():
                result = value(*args, **kwargs)
                return result

        return isolated

    def __setattr__(self, name: str, value: object) -> None:
        if name in {"client", "operation_context"}:
            object.__setattr__(self, name, value)
        else:
            setattr(object.__getattribute__(self, "client"), name, value)


def service_candidate_result(statement: dict, score: float) -> dict:
    """Build the transport-neutral proposal view of one response candidate."""
    result = {
        "statement_id": statement.get("id", ""),
        "response": statement.get("text", ""),
        "score": score,
        "tier": statement.get("tier", Tier.DYNAMIC).value,
        "created_at": statement.get("created_at", EARLIEST_UTC).isoformat(),
        "hit_count": statement.get("hit_count", 0),
        "query_count": statement.get("query_count", 0),
        "source_label": statement.get("source_label", ""),
        "introduced_by_user_id": statement.get("introduced_by_user_id", "") or "",
        "metadata": deepcopy(statement.get("template", {})),
    }
    return result


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


def normalize_service_user_id(user_id: str) -> str:
    """Normalize a service user identity and translate boundary errors."""
    try:
        result = sessions.normalize_user_id(user_id)
        require_service_text(result, "user_id", MAX_CALLER_ID_BYTES)
        return result
    except ValueError as error:
        raise InvalidRequestError(str(error)) from error


def conversation_user_id(user_id: str) -> str:
    """Validate a conversation identity while preserving anonymous emptiness."""
    require_service_string(user_id, "user_id", MAX_CALLER_ID_BYTES)
    result = user_id
    return result


def no_cancellation_check() -> bool:
    """Provide the concrete no-op cancellation operation."""
    return False


def negative_resolution_admissible(value: dict, plan) -> bool:
    """Return whether a complete knowledge miss is safe to cache negatively."""
    current = value
    if current["outcome"] != ResolutionOutcome.MISS or current["budget"]["exhausted_dimensions"]:
        result = False
        return result
    if "output_truncated" in current["reason_codes"]:
        result = False
        return result
    if any(
        value.get("candidates", ()) or value.get("evidence", ()) or value.get("accounting", {})
        for value in current["resolver_results"]
    ):
        result = False
        return result
    results = {item["resolver"]: item for item in current["resolver_results"]}
    configured = [entry for entry in plan["entries"] if entry["configured"]]
    if not configured or any(not entry["available"] for entry in configured):
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
        resolver_name, _, _, _ = resolver_contract(entry["resolver"])
        resolver_result = results.get(resolver_name)
        if not resolver_result:
            result = False
            return result
        expected_reason = knowledge_miss_reasons.get(resolver_name, "")
        if resolver_result["state"] != ResolverState.COMPLETED or resolver_result["reason_code"] != expected_reason:
            result = False
            return result
        if resolver_name == "exact" and (
            resolver_result["diagnostics"].get("owner_count", 0) != 0 or resolver_result["diagnostics"].get("truncated", False)
        ):
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
    exhausted = set()
    selected_diagnostics = {
        "diagnostic_id": frame.get("diagnostic_id", ""),
        **diagnostics,
        "accounting": {"candidate_count": 0, "accepted_present": False, "success_applied": False},
    }
    diagnostic_bytes = len(json_dumps(selected_diagnostics, sort_keys=True, separators=(",", ":")).encode("utf-8"))
    if diagnostic_bytes > frame.get("budget", {})["max_diagnostic_bytes"]:
        selected_diagnostics = {}
        diagnostic_bytes = 0
        exhausted.add("diagnostic_bytes")
    consumption = budget_consumption(
        elapsed_ns=elapsed_ns,
        diagnostic_bytes=diagnostic_bytes,
        exhausted_dimensions=tuple(sorted(exhausted)),
    )

    def build() -> dict:
        value = resolution_result(
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
        return value

    for _ in range(8):
        result = build()
        size = len(resolution_result_to_json(result).encode("utf-8"))
        if size > frame.get("budget", {})["max_output_bytes"]:
            raise InvalidRequestError("negative resolution result exceeds max_output_bytes")
        if size > frame.get("budget", {})["max_working_memory_bytes"]:
            raise MemoryError("negative resolution result exceeds max_working_memory_bytes")
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


class EngramCore:
    """Shared application runtime used by every Engram interface."""

    def __init__(
        self,
        engram=(),
        *,
        config: dict = EMPTY_CONFIG,
        clock: object = (),
    ) -> None:
        if not isinstance(config, dict):
            raise InvalidRequestError("config must be an object")
        if engram == ():
            try:
                selected_engram = Engram(config=config)
            except ValueError as error:
                raise InvalidRequestError(str(error)) from error
        elif isinstance(engram, Engram):
            if config:
                raise InvalidRequestError("config cannot be supplied with an existing Engram")
            selected_engram = engram
        else:
            raise InvalidRequestError("engram must be an Engram")
        self.engram = selected_engram
        self.conversations: dict[str, ConversationRuntime] = {}
        self.resolution_requests: dict[str, dict] = {}
        self.internal_clock = ServiceClock(clock)
        self.lock = threading_RLock()
        self.resolution_condition = threading_Condition(self.lock)
        self.active_resolution_request_ids: set[str] = set()
        self.active_resolution_user_ids: set[str] = set()
        self.active_graph_operations = 0
        self.internal_state = CoreState.RUNNING
        graph_client = self.engram.graph_client
        if graph_client:
            self.engram.internal_graph_client = IsolatedGraphClient(graph_client, self.graph_operation)
        self.internal_component_status = deepcopy(self.engram.component_status)
        self.negative_resolutions = NegativeResolutionStore()
        self.reset_regulated_state()
        self.response_coordinator = AtomicMutationCoordinator(
            self.engram.response_repository,
            self.engram.mutation_receipts,
            publication_hook=self.publish_response_state,
        )
        self.response_mutations = AcceptedResponseService(
            self.response_coordinator,
            tier_admission_policy(self.engram.config.get("capacity", 1)),
        )
        self.query_frame_builder = QueryFrameBuilder(self.engram, time_monotonic_ns, self.internal_clock)
        self.rewrite_engine: object = (
            RewriteEngine(load_default_rewrite_corpus()) if self.engram.config["retrieval_rewrites_enabled"] else ()
        )
        self.resolver_registry = ResolverRegistry(
            (
                ExactResolver(self.engram, time_monotonic_ns),
                UtilityResolver(self.engram.utility_registry, time_monotonic_ns),
                SparseResolver(self.engram, time_monotonic_ns),
                StandaloneSemanticResolver(self.engram, time_monotonic_ns),
                StructuredGraphResolver(self.engram, time_monotonic_ns),
                SupportSemanticResolver(self.engram, time_monotonic_ns),
            )
        )
        self.resolution_accounting = ResolutionAccountingFinalizer(
            self.engram,
            self.response_mutations,
        )
        default_fusion_policy = fusion_policy()
        fusion_policy_value = policy_fingerprint(default_fusion_policy)
        self.resolution_orchestrator = ResolutionOrchestrator(
            self.resolver_registry,
            ResolverExecutor(time_monotonic_ns),
            self.resolution_accounting,
            CandidateFusionEngine(
                policy=default_fusion_policy,
                authority=EngramCandidateAuthority(
                    self.engram,
                    self.engram.feedback_store,
                    fusion_policy_value,
                ),
                reranker=self.engram.reranker,
            ),
        )

    @contextlib_contextmanager
    def resolution_slot(self, request_id: str, user_id: str):
        """Serialize retry identity and per-user context while permitting unrelated work."""
        with self.resolution_condition:
            while request_id in self.active_resolution_request_ids or user_id in self.active_resolution_user_ids:
                self.require_running()
                self.resolution_condition.wait()
            self.require_running()
            self.active_resolution_request_ids.add(request_id)
            self.active_resolution_user_ids.add(user_id)
        try:
            yield
        finally:
            with self.resolution_condition:
                self.active_resolution_request_ids.discard(request_id)
                self.active_resolution_user_ids.discard(user_id)
                self.resolution_condition.notify_all()

    @contextlib_contextmanager
    def graph_operation(self):
        """Track optional graph I/O and release an owned core-wide lock."""
        with self.resolution_condition:
            self.require_running()
            self.active_graph_operations += 1
        released = False
        try:
            try:
                self.lock.release()
                released = True
            except RuntimeError:
                LOGGER.debug("graph operation began without an owned core lock")
            yield
        finally:
            if released:
                self.lock.acquire()
            with self.resolution_condition:
                self.active_graph_operations -= 1
                self.resolution_condition.notify_all()

    def publish_response_state(self, state: dict) -> None:
        """Invalidate request-result misses after authoritative publication."""
        del state
        self.negative_resolutions.clear()

    def __enter__(self) -> "EngramCore":
        """Return this core as a single owned application runtime."""
        with self.lock:
            self.require_running()
            return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        """Release runtime resources."""
        self.close()

    def status(self) -> dict:
        """Return transport-neutral process lifecycle readiness."""
        with self.lock:
            accepting_requests = self.internal_state == CoreState.RUNNING
            self.internal_component_status = self.engram.component_status_snapshot()
            result = {
                "state": self.internal_state.value,
                "ready": accepting_requests,
                "healthy": accepting_requests,
                "memory_only": True,
                "active_conversations": len(self.conversations),
                "components": deepcopy(self.internal_component_status),
                "rollout": rollout_status(self.engram.config),
                "telemetry": self.operational_telemetry(),
            }
            return result

    def cache_resolution(
        self,
        request_id: str,
        signature: str,
        result: dict,
        frame: dict,
        candidate_statement_ids: tuple[str, ...],
        candidacy_observations: tuple[dict, ...],
    ) -> dict:
        """Retain one public resolution result and its feedback references."""
        public_result = validate_resolution_result(result)
        record: dict[str, object] = {
            "signature": signature,
            "result": public_result,
            "frame": frame,
            "candidate_statement_ids": candidate_statement_ids,
            "candidacy_observations": candidacy_observations,
            "candidacy_applied": not candidacy_observations,
        }
        self.resolution_requests[request_id] = record
        if candidacy_observations:
            self.ensure_resolution_candidacy(request_id, record)
        while len(self.resolution_requests) > MAX_TRANSIENT_RECORDS:
            evicted_request_id = next(iter(self.resolution_requests))
            self.resolution_requests.pop(evicted_request_id)
            self.resolution_accounting.discard(evicted_request_id)
        result_copy = validate_resolution_result(public_result)
        return result_copy

    def operational_telemetry(self) -> dict:
        """Return fixed-cardinality process metrics without request-scoped values."""
        with self.lock:
            result = self.engram.operational_telemetry_snapshot()
            result["matcher"] = {
                "queries": self.engram.query_count,
                "hits": self.engram.hit_count,
                "evictions": self.engram.eviction_count,
            }
            result["regulated_cache"] = {
                "proposals": self.regulated_metrics["proposals"],
                "misses": self.regulated_metrics["misses"],
                "accepted": self.regulated_metrics["accepted"],
                "learned_created": self.regulated_metrics["learned_created"],
                "retired": self.regulated_metrics["retired"],
                "idempotent_retries": self.regulated_metrics["idempotent_retries"],
                "pending_proposals": sum(1 for record in self.proposals.values() if not record["resolution"]),
                "retained_proposals": len(self.proposals),
            }
            return result

    def record_resolution_telemetry(self, result: dict, *, replayed: bool) -> None:
        with self.engram.count_lock:
            record_resolution(self.engram.operational_metrics, result, replayed=replayed)

    def record_regulator_telemetry(self, outcome: str) -> None:
        with self.engram.count_lock:
            record_regulator_outcome(self.engram.operational_metrics, outcome)

    def candidate_generation(self, statement_id: str, resolution: dict) -> tuple[int, bool]:
        artifact = self.engram.response_repository.snapshot()["artifacts"].get(statement_id)
        if artifact:
            result = artifact["generation"], True
            return result
        current = validate_resolution_result(resolution)
        for resolver_result in current["resolver_results"]:
            for candidate in resolver_result["candidates"]:
                if candidate["statement_id"] != statement_id:
                    continue
                generation = candidate["provenance"].get("generation", 0)
                if isinstance(generation, int) and not isinstance(generation, bool) and generation > 0:
                    result = generation, True
                    return result
        result = 0, False
        return result

    def feedback_observations(
        self,
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
        policy_value = policy_fingerprint(self.resolution_orchestrator.internal_fusion.policy)
        constraint = constraint_fingerprint(
            frame.get("expected_object_type", ExpectedObjectType.UNKNOWN).value,
            frame.get("required_metadata", {}),
            frame.get("required_source_label", ""),
        )
        observations = []
        for statement_id in statement_ids:
            generation, generation_available = self.candidate_generation(statement_id, resolution)
            observations.append(
                trusted_feedback_observation(
                    reference_kind=reference_kind,
                    reference_id=reference_id,
                    kind=kind,
                    outcome=outcome,
                    query_identity=frame.get("identity", {}),
                    scope=frame.get("scope", {}),
                    constraint_fingerprint=constraint,
                    statement_id=statement_id,
                    generation=generation,
                    generation_available=generation_available,
                    policy_fingerprint=policy_value,
                    observed_at=observed_at,
                    reason=reason,
                )
            )
        result = tuple(observations)
        return result

    def apply_feedback(
        self,
        request_id: str,
        observations: tuple[dict, ...],
        lifecycle_status: LifecycleHandoffStatus = LifecycleHandoffStatus.NOT_APPLICABLE,
    ) -> dict[str, object]:
        candidate = self.engram.feedback_store.prepare_validated(request_id, observations, lifecycle_status)
        if not candidate["replayed"]:
            self.engram.feedback_store.replace_from_snapshot(candidate["after"])
        receipt_value = mutation_receipt_to_dict(candidate["receipt"])
        result = receipt_value["result"]
        if not isinstance(result, dict):
            raise LifecycleError("feedback mutation receipt result is malformed")
        value = deepcopy(result)
        value.update(
            {
                "request_id": request_id,
                "result_code": candidate["receipt"]["result_code"].value,
                "idempotent": candidate["replayed"],
                "memory_only": True,
            }
        )
        return value

    def ensure_resolution_candidacy(self, request_id: str, record: dict[str, object]) -> bool:
        if record.get("candidacy_applied", False):
            return False
        observations = record.get("candidacy_observations", [])
        if not isinstance(observations, tuple):
            raise LifecycleError("resolution candidacy observations are malformed")
        self.apply_feedback(feedback_mutation_request_id("candidacy", request_id), observations)
        record["candidacy_applied"] = True
        return True

    def ensure_proposal_candidacy(self, proposal_id: str, record: dict[str, object]) -> bool:
        if record.get("candidacy_applied", False):
            return False
        observations = record.get("candidacy_observations", [])
        if not isinstance(observations, tuple):
            raise LifecycleError("proposal candidacy observations are malformed")
        self.apply_feedback(feedback_mutation_request_id("proposal-candidacy", proposal_id), observations)
        record["candidacy_applied"] = True
        return True

    def proposal_feedback_observations(
        self,
        frame: dict,
        proposal_id: str,
        statement_ids: tuple[str, ...],
    ) -> tuple[dict, ...]:
        constraint = constraint_fingerprint(
            frame.get("expected_object_type", ExpectedObjectType.UNKNOWN).value,
            frame.get("required_metadata", {}),
            frame.get("required_source_label", ""),
        )
        policy_value = policy_fingerprint(self.resolution_orchestrator.internal_fusion.policy)
        repository = self.engram.response_repository.snapshot()["artifacts"]
        values = []
        for statement_id in statement_ids:
            artifact = repository.get(statement_id)
            values.append(
                trusted_feedback_observation(
                    reference_kind=FeedbackReferenceKind.REGULATED_PROPOSAL,
                    reference_id=proposal_id,
                    kind=FeedbackObservationKind.CANDIDACY,
                    outcome=FeedbackOutcome.CANDIDATE,
                    query_identity=frame.get("identity", {}),
                    scope=frame.get("scope", {}),
                    constraint_fingerprint=constraint,
                    statement_id=statement_id,
                    generation=artifact["generation"] if artifact else 0,
                    generation_available=bool(artifact),
                    policy_fingerprint=policy_value,
                    observed_at=frame.get("eligibility_context", {})["evaluation_time"],
                )
            )
        result = tuple(values)
        return result

    def internal_negative_key(self, frame: dict, plan) -> tuple[dict, bool]:
        context = frame.get("eligibility_context", {})
        if not (context["evaluation_time_available"] and context["artifact_repository_available"]):
            result = empty_negative_resolution()["key"], False
            return result
        configured = tuple(resolver_contract(entry["resolver"])[0] for entry in plan["entries"] if entry["configured"])
        serialized_entries = trusted_resolution_plan_to_dict(plan)["entries"]
        if not isinstance(serialized_entries, list):
            raise LifecycleError("resolution plan entries are malformed")
        plan_entries = serialized_entries
        resolver_plan = [
            {
                "resolver": value["resolver"],
                "cost_class": value["cost_class"],
                "order": value["order"],
                "configured": value["configured"],
            }
            for value in plan_entries
        ]
        readiness = [
            {
                "resolver": value["resolver"],
                "configured": value["configured"],
                "available": value["available"],
                "reason_code": value["reason_code"],
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
            normalization_version=frame.get("identity", {})["normalization_version"],
            resolver_plan_fingerprint=canonical_fingerprint(resolver_plan),
            capability_readiness_fingerprint=canonical_fingerprint(readiness),
            policy_fingerprint=policy_fingerprint(self.resolution_orchestrator.internal_fusion.policy),
        )
        # Negative entries apply to exact-only plans; broader resolver plans
        # invalidate an exact-only miss for the same relationship.
        if configured != ("exact",):
            self.negative_resolutions.invalidate_for_key(key)
            result = key, False
            return result
        result = key, True
        return result

    def resolve_request(
        self,
        request: str,
        request_id: str,
        *,
        user_id: str = "0",
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
        require_service_text(request, "request", MAX_REQUEST_BYTES)
        require_service_text(request_id, "request_id", MAX_REQUEST_ID_BYTES)
        normalized_user_id = normalize_service_user_id(user_id)
        with self.resolution_slot(request_id, normalized_user_id), self.lock:
            self.require_running()
            require_service_string(namespace, "namespace", MAX_NAMESPACE_BYTES)
            require_service_string(context_fingerprint, "context_fingerprint", MAX_CONTEXT_FINGERPRINT_BYTES)
            require_service_string(required_source_label, "required_source_label", MAX_SOURCE_LABEL_BYTES)
            if not isinstance(identity, dict):
                raise InvalidRequestError("identity must be an object")
            if not isinstance(budget, dict):
                raise InvalidRequestError("budget must be an object")
            selected_identity = {}
            if identity:
                try:
                    selected_identity = validate_query_identity(identity)
                except IdentityValidationError as error:
                    raise InvalidRequestError("identity must be a QueryIdentity") from error
            selected_budget: object = EMPTY_MAPPING
            if budget:
                try:
                    selected_budget = validate_resolution_budget(budget)
                except InvalidRequestError as error:
                    raise InvalidRequestError("budget must be a ResolutionBudget") from error
            if not isinstance(required_metadata, dict):
                raise InvalidRequestError("required_metadata must be an object")
            if not isinstance(configured_resolvers, tuple) or not all(
                isinstance(name, str) and name for name in configured_resolvers
            ):
                raise InvalidRequestError("configured_resolvers must be a tuple of non-empty strings")
            if not isinstance(accept_exact, bool):
                raise InvalidRequestError("accept_exact must be a boolean")
            if cancellation_check != () and not callable(cancellation_check):
                raise InvalidRequestError("cancellation_check must be callable")
            selected_cancellation_check = cancellation_check if callable(cancellation_check) else no_cancellation_check

            def check_cancellation() -> None:
                selected_cancellation_check()

            check_cancellation()
            rollout = select_rollout(self.engram.config, namespace)
            rollout_mode = rollout["mode"]
            signature_budget = resolution_budget_to_dict(selected_budget if budget else resolution_budget())
            signature_budget.pop("started_ns")
            signature = service_request_signature(
                request=request,
                user_id=normalized_user_id,
                namespace=namespace,
                context_fingerprint=context_fingerprint,
                identity=query_identity_to_dict(selected_identity) if selected_identity else {},
                required_metadata=required_metadata,
                required_source_label=required_source_label,
                budget=signature_budget,
                configured_resolvers=list(configured_resolvers),
                accept_exact=accept_exact,
                rollout_policy_version=rollout["policy_version"],
                rollout_mode=rollout_mode.value,
            )
            if request_id in self.resolution_requests:
                check_cancellation()
                prior = self.resolution_requests[request_id]
                prior_signature = prior["signature"]
                if prior_signature != signature:
                    raise ConflictError("resolution request_id is associated with different input")
                self.ensure_resolution_candidacy(request_id, prior)
                prior_result = prior["result"]
                try:
                    prior_result = validate_resolution_result(prior_result)
                except InvalidRequestError as error:
                    raise LifecycleError("resolution request cache is malformed") from error
                self.record_resolution_telemetry(prior_result, replayed=True)
                return prior_result
            scope = scope_key(namespace=namespace, context_fingerprint=context_fingerprint)
            frame = self.query_frame_builder.build(
                request,
                scope,
                identity=selected_identity,
                required_metadata=required_metadata,
                required_source_label=required_source_label,
                diagnostic_seed=request_id,
                budget=selected_budget,
            )
            session = sessions.get_session(self.engram, normalized_user_id, create_if_missing=True)
            with self.engram.session_lock:
                prior_turn = session.get("query_frame_turn", 0)
                if isinstance(prior_turn, bool) or not isinstance(prior_turn, int) or not 0 <= prior_turn <= 1_000_000:
                    raise LifecycleError("user contextual query-frame turn is malformed")
                if prior_turn == 1_000_000:
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
            if isinstance(self.rewrite_engine, RewriteEngine):
                frame = apply_rewrites_to_frame(frame, self.rewrite_engine, check_cancellation)

            def remember_contextual_frame() -> None:
                compact = compact_query_frame_from_frame(frame, source_turn=current_turn, topic=topic)
                with self.engram.session_lock:
                    session["previous_query_frame"] = compact
                    session["query_frame_turn"] = current_turn
                    session["last_active"] = self.internal_clock()

            negative_started_ns = time_monotonic_ns()
            if rollout_mode == RolloutMode.DISABLED:
                disabled_result = bounded_miss_result(
                    frame,
                    negative_started_ns,
                    ("rollout_disabled",),
                    {
                        "rollout": {
                            "policy_version": rollout["policy_version"],
                            "mode": rollout_mode.value,
                            "namespace_override": rollout["namespace_override"],
                        }
                    },
                )
                public_result = self.cache_resolution(request_id, signature, disabled_result, frame, (), ())
                remember_contextual_frame()
                self.record_resolution_telemetry(public_result, replayed=False)
                return public_result

            selected_resolvers = ("exact",) if rollout_mode == RolloutMode.ROLLBACK else configured_resolvers
            plan = self.resolver_registry.plan(frame, selected_resolvers)
            negative_key = empty_negative_resolution()["key"]
            negative_key_available = False
            negative_hit = False
            negative_result: object = {}
            try:
                check_cancellation()
                negative_key, negative_key_available = self.internal_negative_key(frame, plan)
                if rollout_mode == RolloutMode.SHADOW:
                    negative_key_available = False
                if negative_key_available:
                    negative_lookup = self.negative_resolutions.lookup(
                        negative_key,
                        frame["eligibility_context"]["evaluation_time"],
                    )
                    if negative_lookup["hit"]:
                        negative_result = negative_hit_result(frame, negative_started_ns)
                        negative_hit = True
            except ResolutionCancelledError:
                raise
            except Exception as error:
                # Negative-cache ownership and lookup are optional optimizations.
                LOGGER.warning("negative-resolution lookup failed closed: %s", error)
                negative_key_available = False
                negative_hit = False
            if negative_hit:
                public_result = apply_rollout(validate_resolution_result(negative_result), rollout)
                public_result = self.cache_resolution(request_id, signature, public_result, frame, (), ())
                remember_contextual_frame()
                self.record_resolution_telemetry(public_result, replayed=False)
                return public_result
            raw_result, finalization = self.resolution_orchestrator.resolve_with_plan(
                frame,
                request_id,
                plan,
                accept_exact=accept_exact and rollout_mode == RolloutMode.REGULATED_DIRECT_ANSWER,
                cooperative_check=selected_cancellation_check,
            )
            if negative_key_available and negative_resolution_admissible(raw_result, plan):
                try:
                    self.negative_resolutions.admit(
                        negative_key,
                        frame["eligibility_context"]["evaluation_time"],
                    )
                except Exception as error:
                    LOGGER.warning("negative-resolution admission failed: %s", error)
            result = apply_rollout(raw_result, rollout)
            candidate_statement_ids = finalization["candidate_statement_ids"] if rollout_mode != RolloutMode.SHADOW else ()
            candidacy_observations = self.feedback_observations(
                frame,
                result,
                candidate_statement_ids,
                reference_kind=FeedbackReferenceKind.RESOLUTION_REQUEST,
                reference_id=request_id,
                kind=FeedbackObservationKind.CANDIDACY,
                outcome=FeedbackOutcome.CANDIDATE,
                observed_at=frame["eligibility_context"]["evaluation_time"],
            )
            public_result = self.cache_resolution(
                request_id,
                signature,
                result,
                frame,
                candidate_statement_ids,
                candidacy_observations,
            )
            remember_contextual_frame()
            self.record_resolution_telemetry(public_result, replayed=False)
            return public_result

    def record_resolution_feedback(
        self,
        resolution_request_id: str,
        feedback_request_id: str,
        outcome: str,
        statement_id: str,
        reason: str = "",
    ) -> dict[str, object]:
        """Apply one explicit external verdict to an observed resolution candidate."""

        with self.lock:
            self.require_running()
            require_service_text(resolution_request_id, "resolution_request_id", MAX_REQUEST_ID_BYTES)
            require_service_text(feedback_request_id, "feedback_request_id", MAX_REQUEST_ID_BYTES)
            require_service_text(statement_id, "statement_id", MAX_ARTIFACT_ID_BYTES)
            require_service_string(reason, "reason", MAX_FEEDBACK_REASON_BYTES)
            try:
                verdict = FeedbackOutcome(outcome)
            except (TypeError, ValueError) as error:
                supported = ", ".join(value.value for value in FeedbackOutcome if value != FeedbackOutcome.CANDIDATE)
                raise InvalidRequestError(f"feedback outcome must be one of: {supported}") from error
            if verdict == FeedbackOutcome.CANDIDATE:
                raise InvalidRequestError("candidate is an internal observation, not an external feedback outcome")
            record = self.resolution_requests.get(resolution_request_id)
            if not record:
                raise ResourceNotFoundError("unknown or expired resolution_request_id")
            self.ensure_resolution_candidacy(resolution_request_id, record)
            candidate_ids = record["candidate_statement_ids"]
            if not isinstance(candidate_ids, tuple) or statement_id not in candidate_ids:
                raise InvalidRequestError("statement_id is not a candidate in this resolution request")
            candidacy = record["candidacy_observations"]
            if not isinstance(candidacy, tuple):
                raise LifecycleError("resolution candidacy observations are malformed")
            target = feedback_target(candidacy, statement_id)
            observed_at = canonical_utc(self.internal_clock())
            observation = feedback_observation(
                reference_kind=target["reference_kind"],
                reference_id=target["reference_id"],
                kind=FeedbackObservationKind.VERDICT,
                outcome=verdict,
                query_identity=target["query_identity"],
                scope=target["scope"],
                constraint_fingerprint=target["constraint_fingerprint"],
                statement_id=target["statement_id"],
                generation=target["generation"],
                generation_available=target["generation_available"],
                policy_fingerprint=target["policy_fingerprint"],
                observed_at=observed_at,
                contract_fingerprint=target["contract_fingerprint"],
                reason=reason,
            )
            lifecycle_status = LifecycleHandoffStatus.NOT_APPLICABLE
            if verdict == FeedbackOutcome.REJECTED_STALE:
                probe = self.engram.feedback_store.prepare(feedback_request_id, (observation,))
                if probe["replayed"]:
                    probe_receipt = mutation_receipt_to_dict(probe["receipt"])
                    probe_result = probe_receipt["result"]
                    if not isinstance(probe_result, dict):
                        raise LifecycleError("feedback replay lifecycle result is malformed")
                    lifecycle_status = LifecycleHandoffStatus(probe_result["lifecycle_status"])
                elif target["generation_available"]:
                    lifecycle_status = LifecycleHandoffStatus.PENDING
                    try:
                        self.response_mutations.invalidate_response(
                            statement_id,
                            target["generation"],
                            LifecycleMutationReason.STALE,
                            "regulator-feedback",
                            feedback_mutation_request_id("stale-lifecycle", feedback_request_id),
                            "external regulator marked the observed candidate generation stale",
                        )
                        lifecycle_status = LifecycleHandoffStatus.COMPLETED
                    except (ConflictError, ResourceNotFoundError, InvalidRequestError):
                        lifecycle_status = LifecycleHandoffStatus.CONFLICTED
                    except MutationCoordinationError:
                        lifecycle_status = LifecycleHandoffStatus.FAILED
                else:
                    lifecycle_status = LifecycleHandoffStatus.PENDING
            result = self.apply_feedback(feedback_request_id, (observation,), lifecycle_status)
            result.update(
                {
                    "resolution_request_id": resolution_request_id,
                    "outcome": verdict.value,
                    "statement_id": statement_id,
                    "reason": reason,
                }
            )
            if not result["idempotent"]:
                self.record_regulator_telemetry(verdict.value)
            return result

    def inspect_feedback_learning(self, limit: int = 64) -> dict:
        """Return bounded transport-neutral Section 6 state and counters."""

        with self.lock:
            self.require_running()
            if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 64:
                raise InvalidRequestError("feedback inspection limit must be an integer from 1 through 64")
            result = {
                "schema_version": 1,
                "feedback": self.engram.feedback_store.inspect(limit),
                "negative_resolution": self.negative_resolutions.inspect(limit),
            }
            return result

    def start_conversation(
        self,
        user_id: str = "0",
        initial_bot_text: str = "",
        random_seed: int = 0,
        random_seed_present: bool = False,
    ) -> dict:
        """Create one observable conversation for a user context."""
        with self.lock:
            self.require_running()
            conversation_id = conversation_user_id(user_id)
            if conversation_id in self.conversations:
                raise ConflictError(f"conversation already active for user_id: {conversation_id}")
            anonymous_session_id = f"anonymous_{uuid4().hex}" if conversation_id == "" else ""
            try:
                runtime = ConversationRuntime(
                    self.engram,
                    user_id=conversation_id,
                    anonymous_session_id=anonymous_session_id,
                    initial_bot_text=initial_bot_text,
                    random_seed=random_seed,
                    random_seed_present=random_seed_present,
                )
            except ValueError as error:
                raise InvalidRequestError(str(error)) from error
            self.conversations[conversation_id] = runtime
            snapshot = runtime.inspect()
            result = {
                "started": True,
                "user_id": snapshot["user_id"],
                "initial_bot_text": snapshot["initial_bot_text"],
                "turn_count": snapshot["turn_count"],
                "statement_count": snapshot["metrics"]["statement_count"],
                "memory_only": True,
            }
            return result

    def get_conversation(self, user_id: str) -> ConversationRuntime:
        """Return an active user runtime or raise a lifecycle error."""
        with self.lock:
            self.require_running()
            conversation_id = conversation_user_id(user_id)
            if conversation_id not in self.conversations:
                raise ResourceNotFoundError(f"no active conversation for user_id: {conversation_id}")
            result = self.conversations.get(conversation_id, ())
            if not isinstance(result, ConversationRuntime):
                raise LifecycleError("active conversation runtime is malformed")
            return result

    def chat(self, user_id: str, text: str) -> dict:
        """Submit one chatbot turn to an active user conversation."""
        conversation_id = conversation_user_id(user_id)
        operation_id = f"chat:{uuid4().hex}"
        with self.resolution_slot(operation_id, conversation_id), self.lock:
            self.require_running()
            runtime = self.get_conversation(conversation_id)
            try:
                result = runtime.send(text)
            except ValueError as error:
                raise InvalidRequestError(str(error)) from error
            return result

    def inspect_conversation(self, user_id: str) -> dict:
        """Inspect one conversation and shared regulated-cache metrics."""
        with self.lock:
            self.require_running()
            self.cleanup_transient()
            snapshot = self.get_conversation(user_id).inspect()
            snapshot["regulated_cache"] = self.regulated_cache_metrics()
            snapshot["core_status"] = self.status()
            return snapshot

    def add_fact(self, text: str, source_label: str = "") -> dict:
        """Add one unattributed shared fact without changing user context."""
        with self.lock:
            self.require_running()
            require_service_text(text, "text", MAX_RESPONSE_BYTES)
            require_service_string(source_label, "source_label", MAX_SOURCE_LABEL_BYTES)
            try:
                statement_id = self.engram.add_fact(text, source_label=source_label)
            except ValueError as error:
                raise InvalidRequestError(str(error)) from error
            result = statement_view(self.engram.get_statement(statement_id))
            return result

    def finish_conversation(self, user_id: str) -> dict:
        """Return a report without ending or persisting the conversation."""
        with self.lock:
            self.require_running()
            runtime = self.get_conversation(user_id)
            result = runtime.report()
            return result

    def stop_conversation(self, user_id: str) -> dict:
        """End one conversation while leaving the shared core available."""
        with self.lock:
            self.require_running()
            runtime = self.get_conversation(user_id)
            report = runtime.report()
            self.conversations.pop(runtime.user_id, {})
            if runtime.user_id == "":
                sessions.delete_session(self.engram, runtime.session_id)
            result = {
                "stopped": True,
                "user_id": report["user_id"],
                "summary": report["summary"],
            }
            return result

    def set_predicate(self, user_id: str, name: str, value: str) -> None:
        """Set one caller-owned predicate on a user context."""
        with self.lock:
            self.require_running()
            require_service_text(name, "name", MAX_METADATA_KEY_BYTES)
            require_service_string(value, "value", MAX_METADATA_STRING_BYTES)
            normalized_user_id = normalize_service_user_id(user_id)
            session = sessions.get_session(self.engram, normalized_user_id, create_if_missing=True)
            with self.engram.session_lock:
                session["predicates"][name] = value

    def get_predicate(self, user_id: str, name: str, default=""):
        """Read one caller-owned predicate from a user context."""
        with self.lock:
            self.require_running()
            require_service_text(name, "name", MAX_METADATA_KEY_BYTES)
            require_service_string(default, "default", MAX_METADATA_STRING_BYTES)
            normalized_user_id = normalize_service_user_id(user_id)
            session = sessions.get_session(self.engram, normalized_user_id, create_if_missing=False)
            if not session:
                return default
            with self.engram.session_lock:
                result = session["predicates"].get(name, default)
                return result

    def warm_vector_recall(self) -> bool:
        """Re-run component preflight and report whether vector recall is ready."""
        with self.lock:
            self.require_running()
            try:
                self.internal_component_status = self.engram.preflight_components()
                self.engram.component_status = deepcopy(self.internal_component_status)
                result = self.internal_component_status["vector"]["ready"] and self.internal_component_status["vector"]["enabled"]
                return result
            except Exception as error:
                raise InvalidRequestError(f"unable to initialize vector recall: {error}") from error

    def close(self) -> bool:
        """Release all transport-independent runtime state."""
        with self.resolution_condition:
            if self.internal_state == CoreState.CLOSED:
                result = False
                return result
            if self.internal_state != CoreState.RUNNING:
                raise LifecycleError(f"core cannot close while {self.internal_state.value}")
            self.internal_state = CoreState.CLOSING
            self.resolution_condition.notify_all()
            while self.active_resolution_request_ids or self.active_graph_operations:
                self.resolution_condition.wait()
            self.conversations.clear()
            self.reset_regulated_state()
            disconnect = getattr(self.engram.graph_client, "disconnect", ())
            if callable(disconnect):
                try:
                    disconnect()
                except Exception as error:
                    LOGGER.warning("graph client disconnect failed while closing Engram: %s", error)
            self.internal_state = CoreState.CLOSED
            self.resolution_condition.notify_all()
            result = True
            return result

    def propose(
        self,
        request: str,
        request_id: str,
        user_id: str = "0",
        namespace: str = "",
        context_fingerprint: str = "",
        limit: int = 1,
        required_metadata: dict = EMPTY_METADATA,
        required_source_label: str = "",
    ) -> dict:
        """Create a speculative, uncredited response-cache proposal."""
        with self.lock:
            self.require_running()
            self.cleanup_transient()
            require_service_text(request, "request", MAX_REQUEST_BYTES)
            require_service_text(request_id, "request_id", MAX_REQUEST_ID_BYTES)
            require_service_string(namespace, "namespace", MAX_NAMESPACE_BYTES)
            require_service_string(context_fingerprint, "context_fingerprint", MAX_CONTEXT_FINGERPRINT_BYTES)
            require_service_string(required_source_label, "required_source_label", MAX_SOURCE_LABEL_BYTES)
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

            existing_proposal_id = self.proposal_requests.get(request_id)
            if existing_proposal_id:
                existing = self.proposals[existing_proposal_id]
                if existing["signature"] != signature:
                    raise ConflictError("request_id is already associated with a different proposal request")
                self.ensure_proposal_candidacy(existing_proposal_id, existing)
                self.regulated_metrics["idempotent_retries"] += 1
                result = service_proposal_result(existing, idempotent=True)
                return result

            scope = scope_key(namespace=namespace, context_fingerprint=context_fingerprint)
            feedback_frame = self.query_frame_builder.build(
                request,
                scope,
                required_metadata=required_metadata,
                required_source_label=required_source_label,
                diagnostic_seed=f"proposal:{request_id}",
            )
            feedback_policy_value = policy_fingerprint(self.resolution_orchestrator.internal_fusion.policy)
            response_artifacts = self.engram.response_repository.snapshot()["artifacts"]

            def artifact_matches_scope(artifact: dict) -> bool:
                statement_id = str(artifact.get("statement_id", ""))
                generation = artifact.get("generation", 0)
                if isinstance(generation, bool) or not isinstance(generation, int):
                    raise LifecycleError("accepted-response artifact generation is malformed")
                if self.engram.feedback_store.trusted_stale_excluded(statement_id, generation, True):
                    return False
                if self.engram.feedback_store.trusted_policy_suppressed(statement_id, namespace, feedback_policy_value):
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
                eligibility_context = EligibilityContextCapture(self.internal_clock).capture_standalone(scope, True)
                exact = self.engram.response_repository.exact_lookup(
                    build_scoped_retrieval_key(scope, request),
                    eligibility_context,
                )
                if exact["lookup"]["outcome"] == ExactLookupOutcome.FOUND:
                    found = response_artifacts.get(exact["lookup"]["statement_id"], {})
                    if found and artifact_matches_scope(found):
                        exact_artifact = found
            except InvalidRequestError:
                exact_artifact = {}
            sparse_matches = ()
            if not exact_artifact:
                sparse_discovery = self.engram.sparse_candidates(
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
                else self.engram.vector_supported_matches(
                    request,
                    limit=limit,
                    artifact_filter=artifact_matches_scope,
                )
            )
            merged = {}
            match_sources = (
                (("exact", ((exact_artifact, 1.0),)),)
                if exact_artifact
                else (("keyword", sparse_matches), ("vector", vector_matches))
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
                    entry[score_field] = max(float(entry[score_field]), float(score))
            ranked = sorted(
                merged.values(),
                key=lambda entry: (
                    max(entry["keyword_score"], entry["vector_score"]),
                    entry["artifact"].get("provenance", {}).get("accepted_at", ""),
                    entry["artifact"].get("statement_id", ""),
                ),
                reverse=True,
            )[:limit]
            candidates = []
            queried_response_ids = []
            for entry in ranked:
                artifact = entry["artifact"]
                statement_id = artifact.get("statement_id", "")
                selected_score = max(entry["keyword_score"], entry["vector_score"])
                queried_response_ids.append(statement_id)
                candidate = artifact_candidate_result(artifact, selected_score)
                candidate["query_count"] += 1
                candidate["retrieval"] = {
                    "keyword_score": entry["keyword_score"],
                    "vector_score": entry["vector_score"],
                    "selected": (
                        "exact" if exact_artifact else "vector" if entry["vector_score"] > entry["keyword_score"] else "keyword"
                    ),
                }
                candidates.append(candidate)
            proposal_id = f"proposal_{uuid4().hex[:16]}"
            proposal = {
                "proposal_id": proposal_id,
                "request_id": request_id,
                "user_id": normalized_user_id,
                "namespace": namespace,
                "context_fingerprint": context_fingerprint,
                "resolved_request": feedback_frame["resolved_text"],
                "keywords": list(feedback_frame["identity"]["lexical_terms"]),
                "candidates": candidates,
            }
            self.proposals[proposal_id] = {
                "created_at": time_monotonic(),
                "signature": signature,
                "proposal": proposal,
                "candidate_responses": {candidate["statement_id"]: candidate["response"] for candidate in candidates},
                "resolution": {},
                "feedback_frame": feedback_frame,
                "candidacy_observations": (),
                "candidacy_applied": not candidates,
            }
            self.proposal_requests[request_id] = proposal_id
            self.regulated_metrics["proposals"] += 1
            if not candidates:
                self.regulated_metrics["misses"] += 1
            self.enforce_transient_bound()
            if queried_response_ids:
                self.response_mutations.record_response_queries(
                    tuple(sorted(queried_response_ids)),
                    accounting_request_id("response-query", request_id),
                )
            record = self.proposals[proposal_id]
            if candidates:
                record["candidacy_observations"] = self.proposal_feedback_observations(
                    feedback_frame,
                    proposal_id,
                    tuple(sorted(record["candidate_responses"])),
                )
                self.ensure_proposal_candidacy(proposal_id, record)
            result = service_proposal_result(self.proposals[proposal_id], idempotent=False)
            return result

    def resolve(self, proposal_id: str, outcome: str, statement_id: str = "", reason: str = "") -> dict:
        """Commit one Regulator verdict without double-crediting retries."""
        with self.lock:
            self.require_running()
            self.cleanup_transient()
            require_service_text(proposal_id, "proposal_id", MAX_REQUEST_ID_BYTES)
            require_service_string(outcome, "outcome", MAX_REASON_CODE_BYTES)
            require_service_string(statement_id, "statement_id", MAX_ARTIFACT_ID_BYTES)
            require_service_string(reason, "reason", MAX_FEEDBACK_REASON_BYTES)
            if outcome not in REGULATOR_OUTCOMES:
                supported = ", ".join(sorted(REGULATOR_OUTCOMES))
                raise InvalidRequestError(f"outcome must be one of: {supported}")

            record = self.proposals.get(proposal_id, {})
            if not record:
                raise ResourceNotFoundError("unknown or expired proposal_id")
            self.ensure_proposal_candidacy(proposal_id, record)
            resolution_signature = service_request_signature(outcome=outcome, statement_id=statement_id, reason=reason)
            if record["resolution"]:
                if record["resolution_signature"] != resolution_signature:
                    raise ConflictError("proposal has already been resolved with a different verdict")
                self.regulated_metrics["idempotent_retries"] += 1
                result = deepcopy(record["resolution"])
                result["idempotent"] = True
                return result

            candidate_responses = record["candidate_responses"]
            if not statement_id:
                raise InvalidRequestError("Regulator outcomes require statement_id")
            if statement_id not in candidate_responses:
                raise InvalidRequestError("statement_id is not a candidate in this proposal")
            current_artifact: dict = {}
            if outcome == "accepted":
                current_artifact = self.engram.response_repository.get_artifact(statement_id)
                if current_artifact.get("response", "") != candidate_responses.get(statement_id, ""):
                    raise ConflictError("candidate is no longer current; resolve it as rejected_stale")
            observations = record["candidacy_observations"]
            if not isinstance(observations, tuple):
                raise LifecycleError("proposal candidacy observations are malformed")
            target = feedback_target(observations, statement_id)
            feedback_outcome = FeedbackOutcome(outcome)
            verdict_observation = feedback_observation(
                reference_kind=target["reference_kind"],
                reference_id=target["reference_id"],
                kind=FeedbackObservationKind.VERDICT,
                outcome=feedback_outcome,
                query_identity=target["query_identity"],
                scope=target["scope"],
                constraint_fingerprint=target["constraint_fingerprint"],
                statement_id=target["statement_id"],
                generation=target["generation"],
                generation_available=target["generation_available"],
                policy_fingerprint=target["policy_fingerprint"],
                observed_at=canonical_utc(self.internal_clock()),
                contract_fingerprint=target["contract_fingerprint"],
                reason=reason,
            )
            verdict_feedback_request_id = feedback_mutation_request_id("proposal-verdict", proposal_id)
            lifecycle_status = LifecycleHandoffStatus.NOT_APPLICABLE
            self.apply_feedback(
                verdict_feedback_request_id,
                (verdict_observation,),
                lifecycle_status,
            )
            if outcome == "accepted":
                proposal = record["proposal"]
                sessions.get_session(self.engram, proposal["user_id"], create_if_missing=True)
                sessions.update_session_context(
                    self.engram,
                    proposal["user_id"],
                    current_artifact.get("response", ""),
                )
                self.regulated_metrics["accepted"] += 1
            else:
                self.regulated_metrics["rejections"][outcome] += 1
            self.record_regulator_telemetry(outcome)

            resolution = {
                "proposal_id": proposal_id,
                "outcome": outcome,
                "statement_id": statement_id,
                "reason": reason,
                "lifecycle_status": lifecycle_status.value,
                "resolved": True,
                "idempotent": False,
            }
            record["resolution_signature"] = resolution_signature
            record["resolution"] = resolution
            if outcome == "accepted":
                self.response_mutations.record_response_hit(
                    statement_id,
                    accounting_request_id("response-hit", proposal_id),
                )
            result = deepcopy(resolution)
            return result

    def learn_response(
        self,
        request: str,
        response: str,
        request_id: str,
        user_id: str = "0",
        namespace: str = "",
        context_fingerprint: str = "",
        source_label: str = "tapestry:actor",
        metadata: dict = EMPTY_METADATA,
    ) -> dict:
        """Create one DYNAMIC ACTIVE response artifact through base commit."""
        with self.lock:
            self.require_running()
            self.cleanup_transient()
            require_service_text(request, "request", MAX_REQUEST_BYTES)
            require_service_text(response, "response", MAX_RESPONSE_BYTES)
            require_service_text(request_id, "request_id", MAX_REQUEST_ID_BYTES)
            require_service_string(namespace, "namespace", MAX_NAMESPACE_BYTES)
            require_service_string(context_fingerprint, "context_fingerprint", MAX_CONTEXT_FINGERPRINT_BYTES)
            require_service_string(source_label, "source_label", MAX_SOURCE_LABEL_BYTES)
            if normalize(response) == "idk":
                raise InvalidRequestError("IDK is not a cacheable response")
            if not isinstance(metadata, dict):
                raise InvalidRequestError("metadata must be an object")
            metadata = dict(metadata)
            normalized_user_id = normalize_service_user_id(user_id)
            mutation = self.response_mutations.learn_response(
                request,
                response,
                request_id,
                normalized_user_id,
                namespace,
                context_fingerprint,
                source_label,
                metadata,
            )
            receipt = mutation["receipt"]
            receipt_value = mutation_receipt_to_dict(receipt)
            receipt_result = receipt_value["result"]
            if not isinstance(receipt_result, dict):
                raise LifecycleError("response mutation receipt result is not an object")
            receipt_statement_id = receipt_result.get("statement_id")
            evicted_statement_ids = receipt_result.get("evicted_statement_ids")
            if not isinstance(receipt_statement_id, str) or not isinstance(evicted_statement_ids, list):
                raise LifecycleError("response mutation receipt result is malformed")
            learned = receipt["result_code"].value != "REJECTED_CAPACITY"
            if learned and not mutation["replayed"]:
                self.engram.eviction_count += len(evicted_statement_ids)
            if learned:
                sessions.get_session(self.engram, normalized_user_id, create_if_missing=True)
                sessions.update_session_context(self.engram, normalized_user_id, response)
            if mutation["replayed"]:
                self.regulated_metrics["idempotent_retries"] += 1
            elif learned:
                self.regulated_metrics["learned_created"] += 1
            result = {
                "learned": learned,
                "statement_id": receipt_statement_id,
                "action": "created" if learned else "rejected_capacity",
                "request_id": request_id,
                "user_id": normalized_user_id,
                "namespace": namespace,
                "context_fingerprint": context_fingerprint,
                "source_label": source_label,
                "idempotent": mutation["replayed"],
                "result_code": receipt["result_code"].value,
                "evicted_statement_ids": evicted_statement_ids,
            }
            result = deepcopy(result)
            return result

    def supersede_response(
        self,
        statement_id: str,
        replacement: dict,
        reason: LifecycleMutationReason,
        request_id: str,
        audit_detail: str = "",
    ) -> dict:
        """Atomically replace one accepted-response artifact with its correction."""

        with self.lock:
            self.require_running()
            self.cleanup_transient()
            require_service_text(statement_id, "statement_id", MAX_ARTIFACT_ID_BYTES)
            require_service_text(request_id, "request_id", MAX_REQUEST_ID_BYTES)
            require_service_string(audit_detail, "audit_detail", MAX_FEEDBACK_REASON_BYTES)
            current = self.engram.response_repository.get_artifact(statement_id)
            mutation = self.response_mutations.supersede_response(
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

    def retire_response(self, statement_id: str, reason: str, request_id: str) -> dict:
        """Retire one accepted-response artifact."""
        with self.lock:
            self.require_running()
            self.cleanup_transient()
            require_service_text(statement_id, "statement_id", MAX_ARTIFACT_ID_BYTES)
            require_service_text(reason, "reason", MAX_FEEDBACK_REASON_BYTES)
            require_service_text(request_id, "request_id", MAX_REQUEST_ID_BYTES)
            response_artifacts = self.engram.response_repository.snapshot()["artifacts"]
            if statement_id in response_artifacts:
                previous = self.retire_requests.get(request_id, {})
                if previous and (previous["result"]["statement_id"] != statement_id or previous["result"]["reason"] != reason):
                    raise ConflictError("request_id is already associated with a different retirement")
                expected_generation = (
                    previous["expected_generation"] if previous else response_artifacts[statement_id]["generation"]
                )
                mutation = self.response_mutations.retire_response(
                    statement_id,
                    expected_generation,
                    LifecycleMutationReason.ADMINISTRATIVE,
                    "RetireResponse",
                    request_id,
                    reason,
                )
                if not mutation["replayed"]:
                    self.regulated_metrics["retired"] += 1
                else:
                    self.regulated_metrics["idempotent_retries"] += 1
                receipt = mutation["receipt"]
                receipt_value = mutation_receipt_to_dict(receipt)
                receipt_result = receipt_value["result"]
                if not isinstance(receipt_result, dict):
                    raise LifecycleError("response mutation receipt result is not an object")
                generation = receipt_result.get("generation")
                if isinstance(generation, bool) or not isinstance(generation, int):
                    raise LifecycleError("response mutation receipt generation is malformed")
                result = {
                    "retired": True,
                    "statement_id": statement_id,
                    "reason": reason,
                    "request_id": request_id,
                    "idempotent": mutation["replayed"],
                    "generation": generation,
                }
                self.retire_requests[request_id] = {
                    "created_at": time_monotonic(),
                    "expected_generation": expected_generation,
                    "result": result,
                }
                self.enforce_transient_bound()
                result = deepcopy(result)
                return result
            raise ResourceNotFoundError("accepted response artifact not found")

    def regulated_cache_metrics(self) -> dict:
        """Return a JSON-ready snapshot of regulated-cache activity."""
        with self.lock:
            self.require_running()
            self.cleanup_transient()
            result = {
                **deepcopy(self.regulated_metrics),
                "pending_proposals": sum(1 for record in self.proposals.values() if not record["resolution"]),
                "retained_proposals": len(self.proposals),
            }
            return result

    def reset_regulated_state(self) -> None:
        self.proposals: dict[str, dict] = {}
        self.proposal_requests: dict[str, str] = {}
        self.learn_requests: dict[str, dict] = {}
        self.retire_requests: dict[str, dict] = {}
        self.regulated_metrics = {
            "proposals": 0,
            "misses": 0,
            "accepted": 0,
            "rejections": Counter({outcome: 0 for outcome in REGULATOR_OUTCOMES if outcome != "accepted"}),
            "learned_created": 0,
            "learned_replaced": 0,
            "retired": 0,
            "idempotent_retries": 0,
        }

    def require_running(self) -> None:
        if self.internal_state != CoreState.RUNNING:
            raise LifecycleError(f"core is {self.internal_state.value}; operation requires running state")

    def cleanup_transient(self) -> None:
        cutoff = time_monotonic() - PROPOSAL_TTL_SECONDS
        expired_proposals = [proposal_id for proposal_id, record in self.proposals.items() if record["created_at"] < cutoff]
        for proposal_id in expired_proposals:
            self.remove_proposal(proposal_id)
        for records in (self.learn_requests, self.retire_requests):
            expired_request_ids = [request_id for request_id, record in records.items() if record["created_at"] < cutoff]
            for request_id in expired_request_ids:
                records.pop(request_id, {})

    def enforce_transient_bound(self) -> None:
        while len(self.proposals) > MAX_TRANSIENT_RECORDS:
            self.remove_proposal(next(iter(self.proposals)))
        for records in (self.learn_requests, self.retire_requests):
            while len(records) > MAX_TRANSIENT_RECORDS:
                records.pop(next(iter(records)))

    def remove_proposal(self, proposal_id: str) -> bool:
        record = self.proposals.pop(proposal_id, {})
        if not record:
            return False
        request_id = record["proposal"]["request_id"]
        if self.proposal_requests.get(request_id) == proposal_id:
            self.proposal_requests.pop(request_id, "")
        return True
