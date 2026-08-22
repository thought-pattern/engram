"""Transport-neutral application facade for Engram interfaces.

``EngramCore`` owns the shared engine, user-bound conversation runtimes,
persistence lifecycle, and regulated response-cache transactions. Interfaces
such as MCP and the CLI translate their inputs and outputs at the boundary;
this module contains no transport-specific types or behavior.
"""

import contextlib
import hashlib
import json
import threading
import time
from collections import Counter
from collections.abc import Callable, Iterator, Mapping
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from typing import cast
from uuid import uuid4

from engram import persistence, sessions
from engram.config import engram_config
from engram.constants import (
    DEFAULT_SEED_PATH,
    EMPTY_CONFIG,
    EMPTY_MAPPING,
    EMPTY_METADATA,
    MAX_TRANSIENT_RECORDS,
    PROPOSAL_TTL_SECONDS,
    REGULATOR_OUTCOMES,
    CoreState,
    DurabilityState,
    Tier,
)
from engram.contextual import compact_query_frame_from_frame, enrich_query_frame
from engram.conversation import ConversationRuntime, statement_view
from engram.coordination import (
    AtomicMutationCoordinator,
    CheckpointFailureError,
    CheckpointFailureKind,
    CoordinatedResponseState,
    MutationCoordinationError,
)
from engram.core import Engram
from engram.eligibility import EligibilityContextFactory, EpochEligibilityPolicy
from engram.errors import (
    ConflictError,
    IdentityValidationError,
    InvalidRequestError,
    LifecycleError,
    PersistenceError,
    ResolutionCancelledError,
    ResourceNotFoundError,
)
from engram.feedback import (
    FeedbackObservation,
    FeedbackObservationKind,
    FeedbackOutcome,
    FeedbackReferenceKind,
    LifecycleHandoffStatus,
    NegativeResolutionKey,
    NegativeResolutionStore,
    _trusted_feedback_observation,
    canonical_fingerprint,
    canonical_utc,
    constraint_fingerprint,
    empty_negative_resolution,
    feedback_observation,
    feedback_state_signature,
    negative_resolution_key,
    validate_feedback_observation,
)
from engram.fusion import CandidateFusionEngine, EngramCandidateAuthority, fusion_policy, policy_fingerprint
from engram.identity import build_scoped_retrieval_key, query_identity_to_dict, scope_key, validate_query_identity
from engram.indexes import ExactLookupOutcome
from engram.models import record_statement_query
from engram.mutations import mutation_receipt_to_dict
from engram.repository import tier_admission_policy
from engram.resolution import (
    QueryFrame,
    QueryFrameBuilder,
    ResolutionOutcome,
    ResolutionResult,
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
    validate_resolver_result,
)
from engram.resolvers import (
    ExactResolver,
    LexicalResolver,
    PatternResolver,
    ResolutionAccountingFinalizer,
    ResolutionOrchestrator,
    ResolverExecutor,
    ResolverRegistry,
    SparseResolver,
    StructuredGraphResolver,
    SupportSemanticResolver,
    resolution_plan_to_dict,
    resolver_contract,
)
from engram.responses import AcceptedResponseService, LifecycleMutationReason
from engram.rewrite import RewriteEngine, apply_rewrites_to_frame, load_default_rewrite_corpus
from engram.text import normalize


def resolve_seed_path(seed_path: str) -> Path:
    """Resolve the bundled seed independently of an interface's working directory."""
    candidate = Path(seed_path)
    if seed_path == "data/seed.json" and not candidate.exists():
        candidate = DEFAULT_SEED_PATH
    result = candidate.resolve()
    return result


def _feedback_target(observations: tuple[object, ...], statement_id: str) -> FeedbackObservation:
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
    digest = hashlib.sha256(request_id.encode("utf-8")).hexdigest()
    result = f"feedback:{kind}:sha256:{digest}"
    return result


def require_service_text(value: str, name: str) -> None:
    """Require a nonempty service-boundary string."""
    if not isinstance(value, str) or not value.strip():
        raise InvalidRequestError(f"{name} must be a non-empty string")


def require_service_string(value: str, name: str) -> None:
    """Require a concrete service-boundary string, including an empty string."""
    if not isinstance(value, str):
        raise InvalidRequestError(f"{name} must be a string")


def service_request_signature(**values) -> str:
    """Return deterministic JSON for an idempotent service request."""
    try:
        result = json.dumps(values, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
        return result
    except (TypeError, ValueError) as error:
        raise InvalidRequestError("metadata and request values must be JSON-compatible") from error


def accounting_request_id(kind: str, external_id: str) -> str:
    """Derive a bounded, non-disclosing identity for internal accounting."""
    digest = hashlib.sha256(external_id.encode("utf-8")).hexdigest()
    result = f"internal:{kind}:sha256:{digest}"
    return result


class _IsolatedGraphClient:
    """Run optional graph calls through a core-owned isolation context."""

    def __init__(self, client: object, operation_context: Callable[[], contextlib.AbstractContextManager[None]]) -> None:
        object.__setattr__(self, "_client", client)
        object.__setattr__(self, "_operation_context", operation_context)

    def __bool__(self) -> bool:
        return bool(object.__getattribute__(self, "_client"))

    def __getattr__(self, name: str):
        client = object.__getattribute__(self, "_client")
        value = getattr(client, name)
        if not callable(value) or name == "disconnect":
            return value
        operation_context = object.__getattribute__(self, "_operation_context")

        def isolated(*args, **kwargs):
            with operation_context():
                return value(*args, **kwargs)

        return isolated

    def __setattr__(self, name: str, value: object) -> None:
        if name in {"_client", "_operation_context"}:
            object.__setattr__(self, name, value)
            return
        setattr(object.__getattribute__(self, "_client"), name, value)


def service_candidate_result(statement: dict, score: float) -> dict:
    """Build the transport-neutral proposal view of one response candidate."""
    result = {
        "statement_id": statement["id"],
        "response": statement["text"],
        "score": score,
        "tier": statement["tier"].value,
        "created_at": statement["created_at"].isoformat(),
        "hit_count": statement["hit_count"],
        "query_count": statement["query_count"],
        "source_label": statement.get("source_label", ""),
        "introduced_by_user_id": statement.get("introduced_by_user_id") or "",
        "metadata": deepcopy(statement.get("template", {})),
    }
    return result


def service_proposal_result(record: dict, *, idempotent: bool) -> dict:
    """Return an isolated proposal view with retry status."""
    result = deepcopy(record["proposal"])
    result["idempotent"] = idempotent
    return result


def normalize_service_user_id(user_id: str) -> str:
    """Normalize a service user identity and translate boundary errors."""
    try:
        result = sessions.normalize_user_id(user_id)
        return result
    except ValueError as error:
        raise InvalidRequestError(str(error)) from error


def _no_cancellation_check() -> None:
    """Provide the concrete no-op cancellation operation."""
    return


def negative_resolution_admissible(value: ResolutionResult, plan) -> bool:
    """Return whether a complete knowledge miss is safe to cache negatively."""
    try:
        current = validate_resolution_result(value)
    except InvalidRequestError:
        result = False
        return result
    if current["outcome"] != ResolutionOutcome.MISS or current["budget"]["exhausted_dimensions"]:
        result = False
        return result
    if "output_truncated" in current["reason_codes"]:
        result = False
        return result
    if any(value["candidates"] or value["evidence"] or value["accounting"] for value in current["resolver_results"]):
        result = False
        return result
    results = {item["resolver"]: item for item in current["resolver_results"]}
    configured = [entry for entry in plan["entries"] if entry["configured"]]
    if not configured or any(not entry["available"] for entry in configured):
        result = False
        return result
    knowledge_miss_reasons = {
        "exact": "exact_MISS",
        "pattern": "pattern_miss",
        "lexical": "lexical_miss",
        "structured_graph": "structured_graph_miss",
        "support_semantic": "support_semantic_miss",
    }
    for entry in configured:
        resolver_name, _, _, _ = resolver_contract(entry["resolver"])
        resolver_result = results.get(resolver_name, ())
        try:
            resolver_result = validate_resolver_result(resolver_result)
        except InvalidRequestError:
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


def negative_hit_result(frame: QueryFrame, started_ns: int) -> ResolutionResult:
    """Build an exactly accounted MISS result for a negative-cache hit."""
    current_ns = time.monotonic_ns()
    elapsed_ns = max(0, current_ns - started_ns)
    exhausted = set()
    diagnostics = {
        "diagnostic_id": frame["diagnostic_id"],
        "negative_resolution": {
            "hit": True,
            "reason": "insufficient_knowledge",
            "memory_only": True,
        },
        "accounting": {
            "candidate_count": 0,
            "accepted_present": False,
            "candidacy_applied": False,
            "success_applied": False,
            "idempotent": False,
        },
    }
    diagnostic_bytes = len(json.dumps(diagnostics, sort_keys=True, separators=(",", ":")).encode("utf-8"))
    if diagnostic_bytes > frame["budget"]["max_diagnostic_bytes"]:
        diagnostics = {}
        diagnostic_bytes = 0
        exhausted.add("diagnostic_bytes")
    consumption = budget_consumption(
        elapsed_ns=elapsed_ns,
        diagnostic_bytes=diagnostic_bytes,
        exhausted_dimensions=tuple(sorted(exhausted)),
    )

    def build() -> ResolutionResult:
        value = resolution_result(
            outcome=ResolutionOutcome.MISS,
            selected_candidate=empty_candidate(),
            selected_candidate_available=False,
            response_candidates=(),
            evidence=(),
            confidence=0.0,
            confidence_available=False,
            reason_codes=("negative_resolution_hit", "insufficient_knowledge"),
            frame_diagnostics=diagnostics,
            resolver_results=(),
            budget=consumption,
        )
        return value

    for _ in range(8):
        result = build()
        size = len(resolution_result_to_json(result).encode("utf-8"))
        if size > frame["budget"]["max_output_bytes"]:
            raise InvalidRequestError("negative resolution result exceeds max_output_bytes")
        if size > frame["budget"]["max_working_memory_bytes"]:
            raise MemoryError("negative resolution result exceeds max_working_memory_bytes")
        updated = budget_consumption_with_changes(consumption, {"output_bytes": size, "working_memory_bytes": size})
        if updated == consumption:
            return result
        consumption = updated
    raise InvalidRequestError("negative resolution budget accounting did not converge")


class EngramCore:
    """Shared application runtime used by every Engram interface."""

    def __init__(
        self,
        engram=(),
        store_path: str = "",
        *,
        checkpoint_on_mutation: bool = True,
        clock: object = (),
    ) -> None:
        if not isinstance(checkpoint_on_mutation, bool):
            raise InvalidRequestError("checkpoint_on_mutation must be a boolean")
        if clock != () and not callable(clock):
            raise InvalidRequestError("clock must be callable")
        self.engram = engram or Engram()
        self.store_path = str(Path(store_path).resolve()) if store_path else ""
        self.checkpoint_on_mutation = checkpoint_on_mutation
        self.conversations: dict[str, ConversationRuntime] = {}
        self._resolution_requests: dict[str, dict[str, object]] = {}
        self._clock: Callable[[], datetime] = cast(Callable[[], datetime], clock) if callable(clock) else lambda: datetime.now(UTC)
        self.lock = threading.RLock()
        self._resolution_condition = threading.Condition(self.lock)
        self._active_resolution_request_ids: set[str] = set()
        self._active_resolution_user_ids: set[str] = set()
        self._active_graph_operations = 0
        self._state = CoreState.RUNNING
        graph_client = self.engram.graph_client
        if graph_client:
            self.engram._graph_client = _IsolatedGraphClient(graph_client, self._graph_operation)
        self._durability = DurabilityState.HEALTHY if self.store_path else DurabilityState.DISABLED
        self._dirty = False
        self._last_checkpoint_at = ""
        self._last_persistence_error = ""
        self._component_status = deepcopy(self.engram.component_status)
        self._negative_resolutions = NegativeResolutionStore()
        self._reset_regulated_state()
        self._response_coordinator = AtomicMutationCoordinator(
            self.engram.response_repository,
            self.engram.namespace_epochs,
            self.engram.mutation_receipts,
            checkpoint_configured=bool(self.store_path and self.checkpoint_on_mutation),
            checkpoint=self._checkpoint_response_state,
            recovery_loader=self._recover_response_state,
            publication_hook=self._publish_response_state,
        )
        self._response_mutations = AcceptedResponseService(
            self._response_coordinator,
            tier_admission_policy(
                self.engram.config["capacity"],
                self.engram.config["eviction_policy"],
                self.engram.config["min_hit_rate"],
            ),
        )
        self._query_frame_builder = QueryFrameBuilder(self.engram, time.monotonic_ns, self._clock)
        self._rewrite_engine: object = (
            RewriteEngine(load_default_rewrite_corpus()) if self.engram.config["retrieval_rewrites_enabled"] else ()
        )
        self._resolver_registry = ResolverRegistry(
            (
                ExactResolver(self.engram, time.monotonic_ns),
                PatternResolver(self.engram, time.monotonic_ns),
                LexicalResolver(self.engram, time.monotonic_ns),
                SparseResolver(self.engram, time.monotonic_ns),
                StructuredGraphResolver(self.engram, time.monotonic_ns),
                SupportSemanticResolver(self.engram, time.monotonic_ns),
            )
        )
        self._resolution_accounting = ResolutionAccountingFinalizer(
            self.engram,
            self._response_mutations,
            lambda previous_ids: persistence.synchronize_response_compatibility_views(self.engram, previous_ids),
        )
        default_fusion_policy = fusion_policy()
        fusion_policy_value = policy_fingerprint(default_fusion_policy)
        self._resolution_orchestrator = ResolutionOrchestrator(
            self._resolver_registry,
            ResolverExecutor(time.monotonic_ns),
            self._resolution_accounting,
            CandidateFusionEngine(
                policy=default_fusion_policy,
                authority=EngramCandidateAuthority(
                    self.engram,
                    self.engram.feedback_store,
                    fusion_policy_value,
                ),
            ),
        )

    @contextlib.contextmanager
    def _resolution_slot(self, request_id: str, user_id: str) -> Iterator[None]:
        """Serialize retry identity and per-user context while permitting unrelated work."""
        with self._resolution_condition:
            while request_id in self._active_resolution_request_ids or user_id in self._active_resolution_user_ids:
                self._require_running()
                self._resolution_condition.wait()
            self._require_running()
            self._active_resolution_request_ids.add(request_id)
            self._active_resolution_user_ids.add(user_id)
        try:
            yield
        finally:
            with self._resolution_condition:
                self._active_resolution_request_ids.discard(request_id)
                self._active_resolution_user_ids.discard(user_id)
                self._resolution_condition.notify_all()

    @contextlib.contextmanager
    def _graph_operation(self) -> Iterator[None]:
        """Track optional graph I/O and release an owned core-wide lock."""
        with self._resolution_condition:
            self._require_running()
            self._active_graph_operations += 1
        released = False
        try:
            try:
                self.lock.release()
                released = True
            except RuntimeError:
                pass
            yield
        finally:
            if released:
                self.lock.acquire()
            with self._resolution_condition:
                self._active_graph_operations -= 1
                self._resolution_condition.notify_all()

    def _invalidate_negative_for_response_state(self, state: CoordinatedResponseState) -> None:
        self._negative_resolutions.invalidate_epoch_snapshot(state["namespace_epochs"])

    def _publish_response_state(self, state: CoordinatedResponseState) -> None:
        """Refresh fail-soft derived state after authoritative publication."""
        receipt_values = state["mutation_receipts"]["receipts"]
        changed_statement_ids: tuple[str, ...] = ()
        if isinstance(receipt_values, tuple) and receipt_values:
            latest = receipt_values[-1]
            if isinstance(latest, Mapping):
                changes = latest.get("affected_generations", ())
                if isinstance(changes, tuple):
                    changed_statement_ids = tuple(
                        sorted(
                            {
                                str(change["statement_id"])
                                for change in changes
                                if isinstance(change, Mapping) and isinstance(change.get("statement_id"), str)
                            }
                        )
                    )
        self.engram.synchronize_sparse_index(state["repository"], changed_statement_ids)
        self._invalidate_negative_for_response_state(state)

    def __enter__(self) -> "EngramCore":
        """Return this core as a single owned application runtime."""
        with self.lock:
            self._require_running()
            return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        """Flush configured state and release runtime resources."""
        self.close()

    def status(self) -> dict:
        """Return transport-neutral lifecycle and durability readiness."""
        with self.lock:
            accepting_requests = self._state == CoreState.RUNNING
            durability_healthy = self._durability != DurabilityState.DEGRADED
            self._component_status = self.engram.component_status_snapshot()
            result = {
                "state": self._state.value,
                "ready": accepting_requests,
                "healthy": accepting_requests and durability_healthy,
                "durability": self._durability.value,
                "dirty": self._dirty,
                "last_checkpoint_at": self._last_checkpoint_at,
                "last_persistence_error": self._last_persistence_error,
                "active_conversations": len(self.conversations),
                "store_path": str(self.store_path) if self.store_path else "",
                "components": deepcopy(self._component_status),
            }
            return result

    def _candidate_generation(self, statement_id: str, resolution: ResolutionResult) -> tuple[int, bool]:
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

    def _feedback_observations(
        self,
        frame: QueryFrame,
        resolution: ResolutionResult,
        statement_ids: tuple[str, ...],
        *,
        reference_kind: FeedbackReferenceKind,
        reference_id: str,
        kind: FeedbackObservationKind,
        outcome: FeedbackOutcome,
        observed_at: str,
        reason: str = "",
    ) -> tuple[FeedbackObservation, ...]:
        policy_value = policy_fingerprint(self._resolution_orchestrator._fusion.policy)
        constraint = constraint_fingerprint(
            frame["expected_object_type"].value,
            frame["required_metadata"],
            frame["required_source_label"],
        )
        observations = []
        for statement_id in statement_ids:
            generation, generation_available = self._candidate_generation(statement_id, resolution)
            observations.append(
                _trusted_feedback_observation(
                    reference_kind=reference_kind,
                    reference_id=reference_id,
                    kind=kind,
                    outcome=outcome,
                    query_identity=frame["identity"],
                    scope=frame["scope"],
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

    def _apply_feedback(
        self,
        request_id: str,
        observations: tuple[FeedbackObservation, ...],
        lifecycle_status: LifecycleHandoffStatus = LifecycleHandoffStatus.NOT_APPLICABLE,
    ) -> dict[str, object]:
        candidate = self.engram.feedback_store._prepare_validated(request_id, observations, lifecycle_status)
        durable = False
        if not candidate["replayed"]:
            if self.store_path and self.checkpoint_on_mutation:
                try:
                    persistence.save_feedback_state(self.engram, candidate["after"], self.store_path)
                except Exception as error:
                    recovered: object = ()
                    with contextlib.suppress(Exception):
                        recovered = persistence.load_feedback_state(self.store_path, config=self.engram.config)
                    recovered_signature = ""
                    if isinstance(recovered, dict):
                        with contextlib.suppress(InvalidRequestError):
                            recovered_signature = feedback_state_signature(recovered)
                    if recovered_signature == feedback_state_signature(candidate["after"]):
                        durable = True
                    elif recovered_signature == feedback_state_signature(candidate["before"]):
                        # The durable file is conclusively still the validated
                        # before-state.  Live state was not published, so this
                        # is retryable without declaring divergent durability.
                        self._durability = DurabilityState.HEALTHY
                        self._last_persistence_error = str(error)
                        raise PersistenceError("feedback checkpoint", error, state_changed=False) from error
                    else:
                        self._durability = DurabilityState.DEGRADED
                        self._last_persistence_error = str(error)
                        raise PersistenceError("feedback checkpoint", error, state_changed=False) from error
                durable = True
                self._durability = DurabilityState.HEALTHY
                self._last_checkpoint_at = canonical_utc(self._clock())
                self._last_persistence_error = ""
            self.engram.feedback_store.replace_from_snapshot(candidate["after"])
            self._dirty = bool(self.store_path and not durable)
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
                "durable": durable or bool(candidate["replayed"] and self.store_path and not self._dirty),
            }
        )
        return value

    def _ensure_resolution_candidacy(self, request_id: str, record: dict[str, object]) -> None:
        if record["candidacy_applied"]:
            return
        observations = record["candidacy_observations"]
        if not isinstance(observations, tuple):
            raise LifecycleError("resolution candidacy observations are malformed")
        self._apply_feedback(feedback_mutation_request_id("candidacy", request_id), observations)
        record["candidacy_applied"] = True

    def _ensure_proposal_candidacy(self, proposal_id: str, record: dict[str, object]) -> None:
        if record["candidacy_applied"]:
            return
        observations = record["candidacy_observations"]
        if not isinstance(observations, tuple):
            raise LifecycleError("proposal candidacy observations are malformed")
        self._apply_feedback(feedback_mutation_request_id("proposal-candidacy", proposal_id), observations)
        record["candidacy_applied"] = True

    def _proposal_feedback_observations(
        self,
        frame: QueryFrame,
        proposal_id: str,
        statement_ids: tuple[str, ...],
    ) -> tuple[FeedbackObservation, ...]:
        constraint = constraint_fingerprint(
            frame["expected_object_type"].value,
            frame["required_metadata"],
            frame["required_source_label"],
        )
        policy_value = policy_fingerprint(self._resolution_orchestrator._fusion.policy)
        repository = self.engram.response_repository.snapshot()["artifacts"]
        values = []
        for statement_id in statement_ids:
            artifact = repository.get(statement_id)
            values.append(
                _trusted_feedback_observation(
                    reference_kind=FeedbackReferenceKind.REGULATED_PROPOSAL,
                    reference_id=proposal_id,
                    kind=FeedbackObservationKind.CANDIDACY,
                    outcome=FeedbackOutcome.CANDIDATE,
                    query_identity=frame["identity"],
                    scope=frame["scope"],
                    constraint_fingerprint=constraint,
                    statement_id=statement_id,
                    generation=artifact["generation"] if artifact else 0,
                    generation_available=bool(artifact),
                    policy_fingerprint=policy_value,
                    observed_at=frame["eligibility_context"]["evaluation_time"],
                )
            )
        result = tuple(values)
        return result

    def _negative_key(self, frame: QueryFrame, plan) -> tuple[NegativeResolutionKey, bool]:
        context = frame["eligibility_context"]
        if not (
            context["evaluation_time_available"]
            and context["knowledge_epoch_available"]
            and context["artifact_repository_available"]
        ):
            result = empty_negative_resolution()["key"], False
            return result
        configured = tuple(resolver_contract(entry["resolver"])[0] for entry in plan["entries"] if entry["configured"])
        serialized_entries = resolution_plan_to_dict(plan)["entries"]
        if not isinstance(serialized_entries, list):
            raise LifecycleError("resolution plan entries are malformed")
        plan_entries = cast(list[dict[str, object]], serialized_entries)
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
            query_identity=frame["identity"],
            scope=frame["scope"],
            constraint_fingerprint=constraint_fingerprint(
                frame["expected_object_type"].value,
                frame["required_metadata"],
                frame["required_source_label"],
            ),
            knowledge_epoch=context["knowledge_epoch"],
            knowledge_epoch_available=True,
            normalization_version=frame["identity"]["normalization_version"],
            resolver_plan_fingerprint=canonical_fingerprint(resolver_plan),
            capability_readiness_fingerprint=canonical_fingerprint(readiness),
            policy_fingerprint=policy_fingerprint(self._resolution_orchestrator._fusion.policy),
        )
        # Namespace epochs currently version only the authoritative accepted-
        # response repository.  Lexical, pattern, graph, and vector knowledge
        # have independent mutation authorities, so caching their misses could
        # hide newly available evidence.  V1 is therefore exact-only until a
        # shared knowledge-version contract is available.  Consulting another
        # plan also invalidates an older exact-only record for this relationship.
        if configured != ("exact",):
            self._negative_resolutions.invalidate_for_key(key)
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
        identity: Mapping[str, object] = EMPTY_MAPPING,
        required_metadata: dict = EMPTY_METADATA,
        required_source_label: str = "",
        budget: Mapping[str, object] = EMPTY_MAPPING,
        configured_resolvers: tuple[str, ...] = (),
        accept_exact: bool = False,
        cancellation_check: object = (),
    ) -> ResolutionResult:
        """Run one transport-neutral bounded resolution pipeline."""
        require_service_text(request, "request")
        require_service_text(request_id, "request_id")
        normalized_user_id = normalize_service_user_id(user_id)
        with self._resolution_slot(request_id, normalized_user_id), self.lock:
            self._require_running()
            require_service_string(namespace, "namespace")
            require_service_string(context_fingerprint, "context_fingerprint")
            require_service_string(required_source_label, "required_source_label")
            if not isinstance(identity, Mapping):
                raise InvalidRequestError("identity must be an object")
            if not isinstance(budget, Mapping):
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
            selected_cancellation_check = (
                cast(Callable[[], object], cancellation_check) if callable(cancellation_check) else _no_cancellation_check
            )

            def check_cancellation() -> None:
                selected_cancellation_check()

            check_cancellation()
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
            )
            if request_id in self._resolution_requests:
                check_cancellation()
                prior = self._resolution_requests[request_id]
                prior_signature = prior["signature"]
                if prior_signature != signature:
                    raise ConflictError("resolution request_id is associated with different input")
                self._ensure_resolution_candidacy(request_id, prior)
                prior_result = prior["result"]
                try:
                    prior_result = validate_resolution_result(prior_result)
                except InvalidRequestError as error:
                    raise LifecycleError("resolution request cache is malformed") from error
                return prior_result
            scope = scope_key(namespace=namespace, context_fingerprint=context_fingerprint)
            frame = self._query_frame_builder.build(
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
                if not isinstance(previous_query_frame, Mapping):
                    raise LifecycleError("user previous query frame is malformed")
                topic = session.get("active_topic", "") or session.get("predicates", {}).get("topic", "")
                if not isinstance(topic, str):
                    raise LifecycleError("user contextual topic is malformed")
            frame = enrich_query_frame(
                frame,
                previous=previous_query_frame,
                current_turn=current_turn,
                topic=topic,
            )
            if isinstance(self._rewrite_engine, RewriteEngine):
                frame = apply_rewrites_to_frame(frame, self._rewrite_engine, check_cancellation)

            def remember_contextual_frame() -> None:
                compact = compact_query_frame_from_frame(frame, source_turn=current_turn, topic=topic)
                with self.engram.session_lock:
                    session["previous_query_frame"] = compact
                    session["query_frame_turn"] = current_turn
                    session["last_active"] = self._clock()
                self._dirty = True
                self._checkpoint()

            negative_started_ns = time.monotonic_ns()
            plan = self._resolver_registry.plan(frame, configured_resolvers)
            negative_key = empty_negative_resolution()["key"]
            negative_key_available = False
            try:
                check_cancellation()
                negative_key, negative_key_available = self._negative_key(frame, plan)
                if negative_key_available:
                    negative_lookup = self._negative_resolutions.lookup(
                        negative_key,
                        frame["eligibility_context"]["evaluation_time"],
                    )
                    if negative_lookup["hit"]:
                        result = negative_hit_result(frame, negative_started_ns)
                        cached_result = validate_resolution_result(result)
                        self._resolution_requests[request_id] = {
                            "signature": signature,
                            "result": cached_result,
                            "frame": frame,
                            "candidate_statement_ids": (),
                            "candidacy_observations": (),
                            "candidacy_applied": True,
                        }
                        while len(self._resolution_requests) > MAX_TRANSIENT_RECORDS:
                            evicted_request_id = next(iter(self._resolution_requests))
                            self._resolution_requests.pop(evicted_request_id)
                            self._resolution_accounting.discard(evicted_request_id)
                        remember_contextual_frame()
                        public_result = validate_resolution_result(cached_result)
                        return public_result
            except ResolutionCancelledError:
                raise
            except Exception:
                # Negative resolution is an optimization and always fails open.
                negative_key_available = False
            result, finalization = self._resolution_orchestrator.resolve(
                frame,
                request_id,
                configured_names=configured_resolvers,
                accept_exact=accept_exact,
                cooperative_check=selected_cancellation_check,
            )
            if negative_key_available and negative_resolution_admissible(result, plan):
                with contextlib.suppress(Exception):
                    self._negative_resolutions.admit(
                        negative_key,
                        frame["eligibility_context"]["evaluation_time"],
                    )
            candidacy_observations = self._feedback_observations(
                frame,
                result,
                finalization["candidate_statement_ids"],
                reference_kind=FeedbackReferenceKind.RESOLUTION_REQUEST,
                reference_id=request_id,
                kind=FeedbackObservationKind.CANDIDACY,
                outcome=FeedbackOutcome.CANDIDATE,
                observed_at=frame["eligibility_context"]["evaluation_time"],
            )
            cached_result = validate_resolution_result(result)
            record: dict[str, object] = {
                "signature": signature,
                "result": cached_result,
                "frame": frame,
                "candidate_statement_ids": finalization["candidate_statement_ids"],
                "candidacy_observations": candidacy_observations,
                "candidacy_applied": not candidacy_observations,
            }
            self._resolution_requests[request_id] = record
            if candidacy_observations:
                self._ensure_resolution_candidacy(request_id, record)
            while len(self._resolution_requests) > MAX_TRANSIENT_RECORDS:
                evicted_request_id = next(iter(self._resolution_requests))
                self._resolution_requests.pop(evicted_request_id)
                self._resolution_accounting.discard(evicted_request_id)
            remember_contextual_frame()
            public_result = validate_resolution_result(cached_result)
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
            self._require_running()
            require_service_text(resolution_request_id, "resolution_request_id")
            require_service_text(feedback_request_id, "feedback_request_id")
            require_service_text(statement_id, "statement_id")
            require_service_string(reason, "reason")
            try:
                verdict = FeedbackOutcome(outcome)
            except (TypeError, ValueError) as error:
                supported = ", ".join(value.value for value in FeedbackOutcome if value != FeedbackOutcome.CANDIDATE)
                raise InvalidRequestError(f"feedback outcome must be one of: {supported}") from error
            if verdict == FeedbackOutcome.CANDIDATE:
                raise InvalidRequestError("candidate is an internal observation, not an external feedback outcome")
            record = self._resolution_requests.get(resolution_request_id)
            if not record:
                raise ResourceNotFoundError("unknown or expired resolution_request_id")
            self._ensure_resolution_candidacy(resolution_request_id, record)
            candidate_ids = record["candidate_statement_ids"]
            if not isinstance(candidate_ids, tuple) or statement_id not in candidate_ids:
                raise InvalidRequestError("statement_id is not a candidate in this resolution request")
            candidacy = record["candidacy_observations"]
            if not isinstance(candidacy, tuple):
                raise LifecycleError("resolution candidacy observations are malformed")
            target = _feedback_target(candidacy, statement_id)
            observed_at = canonical_utc(self._clock())
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
                    lifecycle_status = LifecycleHandoffStatus(cast(str, probe_result["lifecycle_status"]))
                elif target["generation_available"]:
                    lifecycle_status = LifecycleHandoffStatus.PENDING
                    previous_response_ids = tuple(sorted(self.engram.response_repository.snapshot()["artifacts"]))
                    try:
                        lifecycle = self._response_mutations.invalidate_response(
                            statement_id,
                            target["generation"],
                            LifecycleMutationReason.STALE,
                            "regulator-feedback",
                            feedback_mutation_request_id("stale-lifecycle", feedback_request_id),
                            "external regulator marked the observed candidate generation stale",
                        )
                        if not lifecycle["replayed"]:
                            persistence.synchronize_response_compatibility_views(self.engram, previous_response_ids)
                        lifecycle_status = LifecycleHandoffStatus.COMPLETED
                    except (ConflictError, ResourceNotFoundError, InvalidRequestError):
                        lifecycle_status = LifecycleHandoffStatus.CONFLICTED
                    except MutationCoordinationError:
                        lifecycle_status = LifecycleHandoffStatus.FAILED
                else:
                    lifecycle_status = LifecycleHandoffStatus.PENDING
            result = self._apply_feedback(feedback_request_id, (observation,), lifecycle_status)
            result.update(
                {
                    "resolution_request_id": resolution_request_id,
                    "outcome": verdict.value,
                    "statement_id": statement_id,
                    "reason": reason,
                }
            )
            return result

    def inspect_feedback_learning(self, limit: int = 64) -> dict[str, object]:
        """Return bounded transport-neutral Section 6 state and counters."""

        with self.lock:
            self._require_running()
            if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 64:
                raise InvalidRequestError("feedback inspection limit must be an integer from 1 through 64")
            result = {
                "schema_version": 1,
                "feedback": self.engram.feedback_store.inspect(limit),
                "negative_resolution": self._negative_resolutions.inspect(limit),
            }
            return result

    def start_conversation(
        self,
        user_id: str = "0",
        initial_bot_text: str = "",
        transcript_path: str = "",
        random_seed: int = 0,
        random_seed_present: bool = False,
    ) -> dict:
        """Create one observable conversation for a user context."""
        with self.lock:
            self._require_running()
            normalized_user_id = normalize_service_user_id(user_id)
            if normalized_user_id in self.conversations:
                raise ConflictError(f"conversation already active for user_id: {normalized_user_id}")
            try:
                runtime = ConversationRuntime(
                    self.engram,
                    user_id=normalized_user_id,
                    initial_bot_text=initial_bot_text,
                    random_seed=random_seed,
                    random_seed_present=random_seed_present,
                    transcript_path=str(transcript_path) if transcript_path else "",
                )
            except ValueError as error:
                raise InvalidRequestError(str(error)) from error
            self.conversations[normalized_user_id] = runtime
            snapshot = runtime.inspect()
            self._dirty = True
            self._checkpoint()
            result = {
                "started": True,
                "user_id": snapshot["user_id"],
                "initial_bot_text": snapshot["initial_bot_text"],
                "turn_count": snapshot["turn_count"],
                "statement_count": snapshot["metrics"]["statement_count"],
                "store_path": str(self.store_path) if self.store_path else "",
            }
            return result

    def get_conversation(self, user_id: str) -> ConversationRuntime:
        """Return an active user runtime or raise a lifecycle error."""
        with self.lock:
            self._require_running()
            normalized_user_id = normalize_service_user_id(user_id)
            if normalized_user_id not in self.conversations:
                raise ResourceNotFoundError(f"no active conversation for user_id: {normalized_user_id}")
            result = self.conversations[normalized_user_id]
            return result

    def chat(self, user_id: str, text: str) -> dict:
        """Submit one chatbot turn to an active user conversation."""
        normalized_user_id = normalize_service_user_id(user_id)
        operation_id = f"chat:{uuid4().hex}"
        with self._resolution_slot(operation_id, normalized_user_id), self.lock:
            self._require_running()
            runtime = self.get_conversation(normalized_user_id)
            try:
                result = runtime.send(text)
            except ValueError as error:
                raise InvalidRequestError(str(error)) from error
            self._dirty = True
            self._checkpoint()
            return result

    def inspect_conversation(self, user_id: str) -> dict:
        """Inspect one conversation and shared regulated-cache metrics."""
        with self.lock:
            self._require_running()
            self._cleanup_transient()
            snapshot = self.get_conversation(user_id).inspect()
            snapshot["regulated_cache"] = self.regulated_cache_metrics()
            snapshot["core_status"] = self.status()
            return snapshot

    def add_fact(self, text: str, source_label: str = "") -> dict:
        """Add one unattributed shared fact without changing user context."""
        with self.lock:
            self._require_running()
            try:
                statement_id = self.engram.add_fact(text, source_label=source_label)
            except ValueError as error:
                raise InvalidRequestError(str(error)) from error
            result = statement_view(self.engram.get_statement(statement_id))
            self._dirty = True
            self._checkpoint()
            return result

    def finish_conversation(self, user_id: str, output_prefix: str = "engram-transcript") -> dict:
        """Persist the store and write reports without ending a conversation."""
        with self.lock:
            self._require_running()
            runtime = self.get_conversation(user_id)
            self.flush()
            result = runtime.write_report(output_prefix)
            return result

    def stop_conversation(self, user_id: str, *, flush: bool = True) -> dict:
        """End one conversation while leaving the shared core available."""
        with self.lock:
            self._require_running()
            runtime = self.get_conversation(user_id)
            report = runtime.report()
            if flush:
                self.flush()
            self.conversations.pop(runtime.user_id, {})
            result = {
                "stopped": True,
                "user_id": report["user_id"],
                "summary": report["summary"],
            }
            return result

    def set_predicate(self, user_id: str, name: str, value: str) -> None:
        """Set one caller-owned predicate on a user context."""
        with self.lock:
            self._require_running()
            require_service_text(name, "name")
            require_service_string(value, "value")
            normalized_user_id = normalize_service_user_id(user_id)
            session = sessions.get_session(self.engram, normalized_user_id, create_if_missing=True)
            with self.engram.session_lock:
                session["predicates"][name] = value
            self._dirty = True
            self._checkpoint()

    def get_predicate(self, user_id: str, name: str, default=""):
        """Read one caller-owned predicate from a user context."""
        with self.lock:
            self._require_running()
            normalized_user_id = normalize_service_user_id(user_id)
            session = sessions.get_session(self.engram, normalized_user_id, create_if_missing=False)
            if not session:
                return default
            with self.engram.session_lock:
                result = session["predicates"].get(name, default)
                return result

    def flush(self) -> bool:
        """Atomically save configured state; return False when no store is configured."""
        with self.lock:
            self._require_running()
            result = self._flush_store()
            return result

    def warm_vector_recall(self) -> bool:
        """Re-run component preflight and report whether vector recall is ready."""
        with self.lock:
            self._require_running()
            try:
                self._component_status = self.engram.preflight_components()
                self.engram.component_status = deepcopy(self._component_status)
                result = self._component_status["vector"]["ready"] and self._component_status["vector"]["enabled"]
                return result
            except Exception as error:
                raise InvalidRequestError(f"unable to initialize vector recall: {error}") from error

    def close(self, *, flush: bool = True) -> bool:
        """Flush and release all transport-independent runtime state."""
        with self._resolution_condition:
            if self._state == CoreState.CLOSED:
                result = False
                return result
            if self._state != CoreState.RUNNING:
                raise LifecycleError(f"core cannot close while {self._state.value}")
            self._state = CoreState.CLOSING
            self._resolution_condition.notify_all()
            while self._active_resolution_request_ids or self._active_graph_operations:
                self._resolution_condition.wait()
            try:
                if flush:
                    self._flush_store()
            except PersistenceError:
                self._state = CoreState.RUNNING
                self._resolution_condition.notify_all()
                raise
            self.conversations.clear()
            self._reset_regulated_state()
            disconnect = getattr(self.engram.graph_client, "disconnect", ())
            if callable(disconnect):
                with contextlib.suppress(Exception):
                    disconnect()
            self._state = CoreState.CLOSED
            self._resolution_condition.notify_all()
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
            self._require_running()
            self._cleanup_transient()
            require_service_text(request, "request")
            require_service_text(request_id, "request_id")
            require_service_string(namespace, "namespace")
            require_service_string(context_fingerprint, "context_fingerprint")
            require_service_string(required_source_label, "required_source_label")
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
                self._ensure_proposal_candidacy(existing_proposal_id, existing)
                self.regulated_metrics["idempotent_retries"] += 1
                result = service_proposal_result(existing, idempotent=True)
                return result

            scope = scope_key(namespace=namespace, context_fingerprint=context_fingerprint)
            feedback_frame = self._query_frame_builder.build(
                request,
                scope,
                required_metadata=required_metadata,
                required_source_label=required_source_label,
                diagnostic_seed=f"proposal:{request_id}",
            )
            feedback_policy_value = policy_fingerprint(self._resolution_orchestrator._fusion.policy)
            feedback_artifacts = self.engram.response_repository.snapshot()["artifacts"]

            def statement_matches_scope(statement: dict) -> bool:
                if statement["pattern"]:
                    result = False
                    return result
                artifact = feedback_artifacts.get(statement["id"])
                generation = artifact["generation"] if artifact else 0
                if self.engram.feedback_store._trusted_stale_excluded(statement["id"], generation, bool(artifact)):
                    result = False
                    return result
                if self.engram.feedback_store._trusted_policy_suppressed(statement["id"], namespace, feedback_policy_value):
                    result = False
                    return result
                template = statement.get("template", {})
                response_artifact = template.get("response_artifact", {}) if isinstance(template, dict) else {}
                if response_artifact and response_artifact.get("lifecycle", "") != "ACTIVE":
                    result = False
                    return result
                tapestry_metadata = template.get("tapestry", {}) if isinstance(template, dict) else {}
                if not isinstance(tapestry_metadata, dict):
                    result = False
                    return result
                if tapestry_metadata.get("namespace", "") != namespace:
                    result = False
                    return result
                if tapestry_metadata.get("context_fingerprint", "") != context_fingerprint:
                    result = False
                    return result
                if required_source_label and statement.get("source_label", "") != required_source_label:
                    result = False
                    return result
                artifact_metadata = tapestry_metadata.get("metadata", {})
                if not isinstance(artifact_metadata, dict):
                    result = False
                    return result
                result = all(artifact_metadata.get(key) == value for key, value in required_metadata.items())
                return result

            query_result = self.engram.query(
                request,
                user_id=normalized_user_id,
                limit=limit,
                statement_filter=statement_matches_scope,
                record_candidates=False,
            )
            exact_statement_id = ""
            try:
                eligibility_context = EligibilityContextFactory(
                    self._clock,
                    self.engram.namespace_epochs,
                ).capture_standalone(scope, True)
                exact = self.engram.response_repository.exact_lookup(
                    build_scoped_retrieval_key(scope, request),
                    eligibility_context,
                    EpochEligibilityPolicy.MATCH_WHEN_ARTIFACT_AVAILABLE,
                )
                if exact["lookup"]["outcome"] == ExactLookupOutcome.FOUND:
                    exact_statement_id = exact["lookup"]["statement_id"]
                    if not statement_matches_scope(self.engram.get_statement(exact_statement_id)):
                        exact_statement_id = ""
            except InvalidRequestError:
                exact_statement_id = ""
            vector_matches = (
                ()
                if exact_statement_id
                else self.engram.vector_supported_matches(
                    request,
                    limit=limit,
                    statement_filter=statement_matches_scope,
                )
            )
            merged = {}
            match_sources = (
                (("exact", ((self.engram.get_statement(exact_statement_id), 1.0),)),)
                if exact_statement_id
                else (("keyword", query_result["matches"]), ("vector", vector_matches))
            )
            for source, matches in match_sources:
                for statement, score in matches:
                    entry = merged.setdefault(
                        statement["id"],
                        {
                            "statement": statement,
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
                    entry["statement"]["created_at"],
                    entry["statement"]["id"],
                ),
                reverse=True,
            )[:limit]
            candidates = []
            response_artifact_ids = set(self.engram.response_repository.snapshot()["artifacts"])
            queried_response_ids = []
            with self.engram.statement_lock:
                for entry in ranked:
                    statement = entry["statement"]
                    selected_score = max(entry["keyword_score"], entry["vector_score"])
                    if statement["id"] in response_artifact_ids:
                        queried_response_ids.append(statement["id"])
                    else:
                        record_statement_query(statement)
                    candidate = service_candidate_result(statement, selected_score)
                    if statement["id"] in response_artifact_ids:
                        candidate["query_count"] += 1
                    candidate["retrieval"] = {
                        "keyword_score": entry["keyword_score"],
                        "vector_score": entry["vector_score"],
                        "selected": (
                            "exact"
                            if exact_statement_id
                            else "vector" if entry["vector_score"] > entry["keyword_score"] else "keyword"
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
                "resolved_request": query_result["resolved_query"],
                "keywords": list(query_result["keywords"]),
                "candidates": candidates,
            }
            self.proposals[proposal_id] = {
                "created_at": time.monotonic(),
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
            self._enforce_transient_bound()
            if queried_response_ids:
                previous_response_ids = tuple(sorted(response_artifact_ids))
                accounting = self._response_mutations.record_response_queries(
                    tuple(sorted(queried_response_ids)),
                    accounting_request_id("response-query", request_id),
                )
                if not accounting["replayed"]:
                    persistence.synchronize_response_compatibility_views(self.engram, previous_response_ids)
                self._dirty = bool(self.store_path and not accounting["durable"])
            else:
                self._dirty = True
                self._checkpoint()
            record = self.proposals[proposal_id]
            if candidates:
                record["candidacy_observations"] = self._proposal_feedback_observations(
                    feedback_frame,
                    proposal_id,
                    tuple(sorted(record["candidate_responses"])),
                )
                self._ensure_proposal_candidacy(proposal_id, record)
            result = service_proposal_result(self.proposals[proposal_id], idempotent=False)
            return result

    def resolve(self, proposal_id: str, outcome: str, statement_id: str = "", reason: str = "") -> dict:
        """Commit one Regulator verdict without double-crediting retries."""
        with self.lock:
            self._require_running()
            self._cleanup_transient()
            require_service_text(proposal_id, "proposal_id")
            require_service_string(outcome, "outcome")
            require_service_string(statement_id, "statement_id")
            require_service_string(reason, "reason")
            if outcome not in REGULATOR_OUTCOMES:
                supported = ", ".join(sorted(REGULATOR_OUTCOMES))
                raise InvalidRequestError(f"outcome must be one of: {supported}")

            record = self.proposals.get(proposal_id, {})
            if not record:
                raise ResourceNotFoundError("unknown or expired proposal_id")
            self._ensure_proposal_candidacy(proposal_id, record)
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
            if outcome == "accepted":
                current = self.engram.get_statement(statement_id)
                if not current or current["text"] != candidate_responses[statement_id]:
                    raise ConflictError("candidate is no longer current; resolve it as rejected_stale")
            observations = record["candidacy_observations"]
            if not isinstance(observations, tuple):
                raise LifecycleError("proposal candidacy observations are malformed")
            target = _feedback_target(observations, statement_id)
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
                observed_at=canonical_utc(self._clock()),
                contract_fingerprint=target["contract_fingerprint"],
                reason=reason,
            )
            verdict_feedback_request_id = feedback_mutation_request_id("proposal-verdict", proposal_id)
            lifecycle_status = LifecycleHandoffStatus.NOT_APPLICABLE
            if feedback_outcome == FeedbackOutcome.REJECTED_STALE:
                probe = self.engram.feedback_store.prepare(verdict_feedback_request_id, (verdict_observation,))
                if probe["replayed"]:
                    probe_receipt = mutation_receipt_to_dict(probe["receipt"])
                    probe_result = probe_receipt["result"]
                    if not isinstance(probe_result, dict):
                        raise LifecycleError("proposal feedback replay lifecycle result is malformed")
                    lifecycle_status = LifecycleHandoffStatus(cast(str, probe_result["lifecycle_status"]))
                elif target["generation_available"]:
                    lifecycle_status = LifecycleHandoffStatus.PENDING
                    previous_response_ids = tuple(sorted(self.engram.response_repository.snapshot()["artifacts"]))
                    try:
                        lifecycle = self._response_mutations.invalidate_response(
                            statement_id,
                            target["generation"],
                            LifecycleMutationReason.STALE,
                            "regulator-feedback",
                            feedback_mutation_request_id("proposal-stale-lifecycle", proposal_id),
                            "external regulator marked the observed proposal candidate generation stale",
                        )
                        if not lifecycle["replayed"]:
                            persistence.synchronize_response_compatibility_views(self.engram, previous_response_ids)
                        lifecycle_status = LifecycleHandoffStatus.COMPLETED
                    except (ConflictError, ResourceNotFoundError, InvalidRequestError):
                        lifecycle_status = LifecycleHandoffStatus.CONFLICTED
                    except MutationCoordinationError:
                        lifecycle_status = LifecycleHandoffStatus.FAILED
                else:
                    lifecycle_status = LifecycleHandoffStatus.PENDING
            self._apply_feedback(
                verdict_feedback_request_id,
                (verdict_observation,),
                lifecycle_status,
            )
            response_artifact_ids: set[str] = set()
            if outcome == "accepted":
                current = self.engram.get_statement(statement_id)
                proposal = record["proposal"]
                response_artifact_ids = set(self.engram.response_repository.snapshot()["artifacts"])
                self.engram.record_hit(proposal["keywords"], statement_id=statement_id)
                sessions.get_session(self.engram, proposal["user_id"], create_if_missing=True)
                sessions.update_session_context(self.engram, proposal["user_id"], current["text"])
                self.regulated_metrics["accepted"] += 1
                self._dirty = True
            else:
                self.regulated_metrics["rejections"][outcome] += 1

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
                if statement_id in response_artifact_ids:
                    previous_response_ids = tuple(sorted(response_artifact_ids))
                    accounting = self._response_mutations.record_response_hit(
                        statement_id,
                        accounting_request_id("response-hit", proposal_id),
                    )
                    if not accounting["replayed"]:
                        persistence.synchronize_response_compatibility_views(self.engram, previous_response_ids)
                    self._dirty = bool(self.store_path and not accounting["durable"])
                else:
                    self._checkpoint()
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
            self._require_running()
            self._cleanup_transient()
            require_service_text(request, "request")
            require_service_text(response, "response")
            require_service_text(request_id, "request_id")
            require_service_string(namespace, "namespace")
            require_service_string(context_fingerprint, "context_fingerprint")
            require_service_string(source_label, "source_label")
            if normalize(response) == "idk":
                raise InvalidRequestError("IDK is not a cacheable response")
            if not isinstance(metadata, dict):
                raise InvalidRequestError("metadata must be an object")
            metadata = dict(metadata)
            normalized_user_id = normalize_service_user_id(user_id)
            previous_response_ids = tuple(sorted(self.engram.response_repository.snapshot()["artifacts"]))
            try:
                mutation = self._response_mutations.learn_response(
                    request,
                    response,
                    request_id,
                    normalized_user_id,
                    namespace,
                    context_fingerprint,
                    source_label,
                    metadata,
                )
            except MutationCoordinationError as error:
                if error.checkpoint_count:
                    raise PersistenceError("store checkpoint", error, state_changed=error.live_state_changed) from error
                raise
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
                persistence.synchronize_response_compatibility_views(self.engram, previous_response_ids)
                self.engram.eviction_count += len(evicted_statement_ids)
            if learned:
                sessions.get_session(self.engram, normalized_user_id, create_if_missing=True)
                sessions.update_session_context(self.engram, normalized_user_id, response)
            if mutation["replayed"]:
                self.regulated_metrics["idempotent_retries"] += 1
            elif learned:
                self.regulated_metrics["learned_created"] += 1
            self._dirty = bool(self.store_path and not mutation["durable"])
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

    def retire_response(self, statement_id: str, reason: str, request_id: str) -> dict:
        """Retire one dynamic, patternless response-cache entry."""
        with self.lock:
            self._require_running()
            self._cleanup_transient()
            require_service_text(statement_id, "statement_id")
            require_service_text(reason, "reason")
            require_service_text(request_id, "request_id")
            response_artifacts = self.engram.response_repository.snapshot()["artifacts"]
            if statement_id in response_artifacts:
                previous = self.retire_requests.get(request_id, {})
                if previous and (previous["result"]["statement_id"] != statement_id or previous["result"]["reason"] != reason):
                    raise ConflictError("request_id is already associated with a different retirement")
                expected_generation = (
                    previous["expected_generation"] if previous else response_artifacts[statement_id]["generation"]
                )
                previous_response_ids = tuple(sorted(response_artifacts))
                mutation = self._response_mutations.retire_response(
                    statement_id,
                    expected_generation,
                    LifecycleMutationReason.ADMINISTRATIVE,
                    "legacy:RetireResponse",
                    request_id,
                    reason,
                )
                if not mutation["replayed"]:
                    persistence.synchronize_response_compatibility_views(self.engram, previous_response_ids)
                    self.regulated_metrics["retired"] += 1
                else:
                    self.regulated_metrics["idempotent_retries"] += 1
                self._dirty = bool(self.store_path and not mutation["durable"])
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
                    "created_at": time.monotonic(),
                    "expected_generation": expected_generation,
                    "result": result,
                }
                self._enforce_transient_bound()
                result = deepcopy(result)
                return result
            signature = service_request_signature(statement_id=statement_id, reason=reason)
            previous = self.retire_requests.get(request_id, {})
            if previous:
                if previous["signature"] != signature:
                    raise ConflictError("request_id is already associated with a different retirement")
                self.regulated_metrics["idempotent_retries"] += 1
                result = deepcopy(previous["result"])
                result["idempotent"] = True
                self._checkpoint()
                return result

            statement = self.engram.get_statement(statement_id)
            if not statement:
                raise ResourceNotFoundError("unknown statement_id")
            if statement["tier"] != Tier.DYNAMIC or statement["pattern"]:
                raise InvalidRequestError("only dynamic, patternless response-cache entries can be retired")
            if not self.engram.retire_statement(statement_id):
                raise ConflictError("statement could not be retired")
            result = {
                "retired": True,
                "statement_id": statement_id,
                "reason": reason,
                "request_id": request_id,
                "idempotent": False,
            }
            self.retire_requests[request_id] = {
                "created_at": time.monotonic(),
                "signature": signature,
                "result": result,
            }
            self.regulated_metrics["retired"] += 1
            self._enforce_transient_bound()
            self._dirty = True
            self._checkpoint()
            result = deepcopy(result)
            return result

    def regulated_cache_metrics(self) -> dict:
        """Return a JSON-ready snapshot of regulated-cache activity."""
        with self.lock:
            self._require_running()
            self._cleanup_transient()
            result = {
                **deepcopy(self.regulated_metrics),
                "pending_proposals": sum(1 for record in self.proposals.values() if not record["resolution"]),
                "retained_proposals": len(self.proposals),
            }
            return result

    def _reset_regulated_state(self) -> None:
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

    def _checkpoint(self) -> None:
        if self.checkpoint_on_mutation:
            self._flush_store()

    def _checkpoint_response_state(self, state: CoordinatedResponseState) -> None:
        try:
            store_path = Path(self.store_path)
            store_path.parent.mkdir(parents=True, exist_ok=True)
            with contextlib.suppress(OSError):
                store_path.parent.chmod(0o700)
            persistence.save_response_state(self.engram, state, store_path)
        except Exception as error:
            self._durability = DurabilityState.DEGRADED
            self._last_persistence_error = str(error)
            raise CheckpointFailureError(CheckpointFailureKind.INDETERMINATE, str(error)) from error
        self._durability = DurabilityState.HEALTHY
        self._dirty = False
        self._last_checkpoint_at = datetime.now(UTC).isoformat()
        self._last_persistence_error = ""

    def _recover_response_state(self) -> CoordinatedResponseState:
        result = persistence.load_coordinated_response_state(self.store_path, config=self.engram.config)
        return result

    def _flush_store(self) -> bool:
        if not self.store_path:
            self._durability = DurabilityState.DISABLED
            self._dirty = False
            result = False
            return result
        try:
            store_path = Path(self.store_path)
            store_path.parent.mkdir(parents=True, exist_ok=True)
            with contextlib.suppress(OSError):
                store_path.parent.chmod(0o700)
            persistence.save(self.engram, store_path)
        except Exception as error:
            self._durability = DurabilityState.DEGRADED
            self._last_persistence_error = str(error)
            raise PersistenceError("store checkpoint", error, state_changed=self._dirty) from error
        self._durability = DurabilityState.HEALTHY
        self._dirty = False
        self._last_checkpoint_at = datetime.now(UTC).isoformat()
        self._last_persistence_error = ""
        result = True
        return result

    def _require_running(self) -> None:
        if self._state != CoreState.RUNNING:
            raise LifecycleError(f"core is {self._state.value}; operation requires running state")

    def _cleanup_transient(self) -> None:
        cutoff = time.monotonic() - PROPOSAL_TTL_SECONDS
        expired_proposals = [proposal_id for proposal_id, record in self.proposals.items() if record["created_at"] < cutoff]
        for proposal_id in expired_proposals:
            self._remove_proposal(proposal_id)
        for records in (self.learn_requests, self.retire_requests):
            expired_request_ids = [request_id for request_id, record in records.items() if record["created_at"] < cutoff]
            for request_id in expired_request_ids:
                records.pop(request_id, {})

    def _enforce_transient_bound(self) -> None:
        while len(self.proposals) > MAX_TRANSIENT_RECORDS:
            self._remove_proposal(next(iter(self.proposals)))
        for records in (self.learn_requests, self.retire_requests):
            while len(records) > MAX_TRANSIENT_RECORDS:
                records.pop(next(iter(records)))

    def _remove_proposal(self, proposal_id: str) -> None:
        record = self.proposals.pop(proposal_id, {})
        if not record:
            return
        request_id = record["proposal"]["request_id"]
        if self.proposal_requests.get(request_id) == proposal_id:
            self.proposal_requests.pop(request_id, "")


def open_engram_core(
    *,
    config: dict = EMPTY_CONFIG,
    store_path: str = "",
    seed_path: str = "",
    checkpoint_on_mutation: bool = True,
) -> EngramCore:
    """Load or create a core, optionally synchronizing a seed corpus."""
    if not isinstance(config, dict):
        raise InvalidRequestError("config must be an object")
    resolved_store = str(Path(store_path).resolve()) if store_path else ""
    if resolved_store and Path(resolved_store).exists():
        try:
            engram = persistence.load_engram(resolved_store, config=config)
        except Exception as error:
            raise PersistenceError("store load", error, state_changed=False) from error
    else:
        try:
            core_config = config or engram_config()
            engram = Engram(config=core_config)
        except ValueError as error:
            raise InvalidRequestError(str(error)) from error

    if seed_path:
        resolved_seed = resolve_seed_path(str(seed_path))
        if not resolved_seed.exists():
            raise ResourceNotFoundError(f"seed file not found: {resolved_seed}")
        try:
            seed_data = json.loads(resolved_seed.read_text(encoding="utf-8"))
            engram.sync_corpus(seed_data.get("pairs", []))
        except (OSError, json.JSONDecodeError, ValueError) as error:
            raise InvalidRequestError(f"invalid seed file {resolved_seed}: {error}") from error

    core = EngramCore(
        engram,
        store_path=resolved_store,
        checkpoint_on_mutation=checkpoint_on_mutation,
    )
    if seed_path:
        core._dirty = True
        core._checkpoint()
    result = core
    return result
