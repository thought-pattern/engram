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
from logging import getLogger as logging_getLogger
from threading import Condition as threading_Condition, RLock as threading_RLock
from time import monotonic_ns as time_monotonic_ns

from engram.constants import (
    EMPTY_CONFIG,
    MAX_TRANSIENT_RECORDS,
    REGULATOR_OUTCOMES,
    RESOLUTION_SLOT_CANCELLATION_POLL_SECONDS,
)
from engram.constants_service import CoreState
from engram.conversation import ConversationRuntime
from engram.coordination import AtomicMutationCoordinator
from engram.core import Engram
from engram.errors import InvalidRequestError, LifecycleError
from engram.fusion import CandidateFusionEngine, EngramCandidateAuthority, fusion_policy, policy_fingerprint
from engram.graph_resolvers import StructuredGraphResolver, SupportSemanticResolver
from engram.negative_resolution import NegativeResolutionStore
from engram.repository import tier_admission_policy
from engram.resolution import validate_resolution_result
from engram.resolution_accounting import ResolutionAccountingFinalizer
from engram.resolution_frame import QueryFrameBuilder
from engram.resolution_orchestrator import ResolutionOrchestrator
from engram.resolver_execution import ResolverExecutor
from engram.resolver_plan import ResolverRegistry
from engram.resolvers import ExactResolver, SparseResolver, StandaloneSemanticResolver, UtilityResolver
from engram.responses import AcceptedResponseService
from engram.rewrite import RewriteEngine, load_default_rewrite_corpus
from engram.rollout import rollout_status
from engram.scope_removal import EngagementResponseRemoval, validate_removal_command
from engram.service_conversations import (
    add_fact,
    chat,
    finish_conversation,
    get_conversation,
    get_predicate,
    inspect_conversation,
    make_conversation_room,
    owned_conversation,
    release_conversation,
    set_predicate,
    start_conversation,
    stop_conversation,
)
from engram.service_feedback import (
    apply_feedback,
    candidate_generation,
    ensure_proposal_candidacy,
    ensure_resolution_candidacy,
    feedback_observations,
    inspect_feedback_learning,
    proposal_feedback_observations,
    record_resolution_feedback,
)
from engram.service_proposals import complete_proposal_resolution, current_proposal_candidate, propose, resolve
from engram.service_requests import no_cancellation_check
from engram.service_resolution import internal_negative_key, resolve_request
from engram.service_responses import learn_response, responses_by_support, retire_response, supersede_response
from engram.telemetry import record_graph_recall, record_regulator_outcome, record_resolution

LOGGER = logging_getLogger("engram.service")


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

    def __init__(self, client, operation_context, telemetry_operation) -> None:
        if not callable(operation_context):
            raise InvalidRequestError("graph operation context must be callable")
        if not callable(telemetry_operation):
            raise InvalidRequestError("graph telemetry operation must be callable")
        object.__setattr__(self, "client", client)
        object.__setattr__(self, "operation_context", operation_context)
        object.__setattr__(self, "telemetry_operation", telemetry_operation)

    def __bool__(self) -> bool:
        result = bool(object.__getattribute__(self, "client"))
        return result

    def __getattr__(self, name: str):
        client = object.__getattribute__(self, "client")
        value = getattr(client, name)
        # Lifecycle calls run outside a turn and do no graph I/O on the caller's thread.
        if not callable(value) or name in {"disconnect", "reconnect_after_turn"}:
            return value
        operation_context = object.__getattribute__(self, "operation_context")
        telemetry_operation = object.__getattribute__(self, "telemetry_operation")

        def isolated(*args, **kwargs):
            started_ns = time_monotonic_ns()
            with operation_context():
                try:
                    result = value(*args, **kwargs)
                except Exception:
                    if name not in {"execute", "vector_search_propositions"}:
                        telemetry_operation("failure", time_monotonic_ns() - started_ns)
                    raise
                if name not in {"execute", "vector_search_propositions"}:
                    outcome = "hit" if result else "miss"
                    telemetry_operation(outcome, time_monotonic_ns() - started_ns)
                return result

        return isolated

    def __setattr__(self, name: str, value: object) -> None:
        if name in {"client", "operation_context", "telemetry_operation"}:
            object.__setattr__(self, name, value)
        else:
            setattr(object.__getattribute__(self, "client"), name, value)


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
            except ValueError as err:
                LOGGER.error("Engram could not start with the supplied configuration", exc_info=err)
                raise InvalidRequestError("Engram configuration is invalid; the server log has the details") from err
        elif isinstance(engram, Engram):
            if config:
                raise InvalidRequestError("config cannot be supplied with an existing Engram")
            selected_engram = engram
        else:
            raise InvalidRequestError("engram must be an Engram")
        self.engram = selected_engram
        # A core that built its Engram owns its session memory and releases it on close;
        # a supplied Engram's named sessions remain its caller's.
        self.internal_owns_engram = engram == ()
        self.conversations: dict[str, ConversationRuntime] = {}
        # Anonymous ("0") conversations are exclusive: each is owned by the token its start returned.
        self.conversation_tokens: dict[str, str] = {}
        self.conversation_activity: dict[str, float] = {}
        self.resolution_requests: dict[str, dict] = {}
        self.internal_clock = ServiceClock(clock)
        self.lock = threading_RLock()
        self.resolution_condition = threading_Condition(self.lock)
        self.active_resolution_request_ids: set[str] = set()
        self.active_resolution_user_ids: set[str] = set()
        self.active_graph_operations = 0
        self.internal_state = CoreState.RUNNING
        self.engagement_maintenance = {}
        self.engagement_purged = False
        graph_client = self.engram.graph_client
        if graph_client:
            self.engram.internal_graph_client = IsolatedGraphClient(
                graph_client,
                self.graph_operation,
                self.record_graph_operation,
            )
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
        self.response_removal = EngagementResponseRemoval(self.response_coordinator, self.engram.feedback_store)
        self.query_frame_builder = QueryFrameBuilder(self.engram, time_monotonic_ns, self.internal_clock)
        self.rewrite_engine: object = (
            RewriteEngine(load_default_rewrite_corpus()) if self.engram.config.get("retrieval_rewrites_enabled", False) else ()
        )
        graph_resolver = StructuredGraphResolver(self.engram, time_monotonic_ns)
        local_resolvers = (
            ExactResolver(self.engram, time_monotonic_ns),
            UtilityResolver(self.engram.utility_registry, time_monotonic_ns),
            SparseResolver(self.engram, time_monotonic_ns),
            StandaloneSemanticResolver(self.engram, time_monotonic_ns),
            SupportSemanticResolver(self.engram, time_monotonic_ns),
        )
        configured_graph = bool(self.engram.config.get("graph", {}).get("enabled", False))
        registered_resolvers = (graph_resolver, *local_resolvers) if configured_graph else (*local_resolvers, graph_resolver)
        self.resolver_registry = ResolverRegistry(registered_resolvers)
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
    def resolution_slot(self, request_id: str, user_id: str, cancellation_check: object = no_cancellation_check):
        """Serialize retry identity and per-user context while permitting unrelated work.

        A slot spans one turn: a chat exchange or a resolution request. When
        it ends, a graph connection lost during the turn starts reconnecting.
        A request queued behind another holder of its request ID or user checks
        its caller's cancellation between bounded waits, so a cancelled caller
        leaves the queue without waiting for that holder to finish.
        """
        if not callable(cancellation_check):
            raise InvalidRequestError("cancellation_check must be callable")
        with self.resolution_condition:
            while request_id in self.active_resolution_request_ids or user_id in self.active_resolution_user_ids:
                self.require_running()
                cancellation_check()
                self.resolution_condition.wait(RESOLUTION_SLOT_CANCELLATION_POLL_SECONDS)
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
            self.reconnect_graph_after_turn()

    def reconnect_graph_after_turn(self) -> None:
        """Let a graph client that lost its connection reconnect now that the turn is over.

        Called when a chat or resolution turn ends, and by the gRPC service
        after every call. The client reconnects in the background, so the
        finished turn is not delayed, and its reads fail immediately until the
        reconnect succeeds. The core lock is not taken, so a response is never
        held behind other requests.
        """
        if self.internal_state == CoreState.RUNNING:
            reconnect = getattr(self.engram.graph_client, "reconnect_after_turn", ())
            if callable(reconnect):
                try:
                    reconnect()
                except Exception as err:
                    LOGGER.warning("graph reconnect could not start", exc_info=err)

    @contextlib_contextmanager
    def graph_operation(self):
        """Track optional graph I/O and release an owned core-wide lock.

        The core lock stays held when this thread is inside an Engram state
        lock, such as a template graph query rendered under statement_lock.
        Releasing it there would let another request take the core lock and
        then wait on that state lock while this thread waits on the core lock.
        """
        with self.resolution_condition:
            self.require_running()
            self.active_graph_operations += 1
        released = False
        try:
            if self.engram.holds_state_lock():
                LOGGER.debug("graph operation keeps the core lock inside an Engram state lock")
            else:
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

    def record_graph_operation(self, outcome: str, elapsed_ns: int) -> None:
        """Record one fixed-capability graph call without retaining request data."""
        with self.engram.count_lock:
            record_graph_recall(self.engram.operational_metrics, outcome, elapsed_ns)

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
                "proposals": self.regulated_metrics.get("proposals", 0),
                "misses": self.regulated_metrics.get("misses", 0),
                "accepted": self.regulated_metrics.get("accepted", 0),
                "learned_created": self.regulated_metrics.get("learned_created", 0),
                "retired": self.regulated_metrics.get("retired", 0),
                "idempotent_retries": self.regulated_metrics.get("idempotent_retries", 0),
                "pending_proposals": sum(1 for record in self.proposals.values() if not record.get("resolution", {})),
                "retained_proposals": len(self.proposals),
            }
            return result

    def record_resolution_telemetry(self, result: dict, *, replayed: bool) -> None:
        with self.engram.count_lock:
            record_resolution(self.engram.operational_metrics, result, replayed=replayed)

    def record_regulator_telemetry(self, outcome: str) -> None:
        with self.engram.count_lock:
            record_regulator_outcome(self.engram.operational_metrics, outcome)

    def warm_vector_recall(self) -> bool:
        """Re-run component preflight and report whether vector recall is ready."""
        with self.lock:
            self.require_running()
            try:
                self.internal_component_status = self.engram.preflight_components()
                self.engram.component_status = deepcopy(self.internal_component_status)
                vector_status = self.internal_component_status.get("vector", {})
                result = vector_status.get("ready", False) and vector_status.get("enabled", False)
                return result
            except Exception as err:
                LOGGER.error("Vector recall could not be initialized", exc_info=err)
                raise InvalidRequestError("unable to initialize vector recall; the server log has the details") from err

    def maintain_engagement(self, value: dict) -> dict:
        """Trusted administrative pause, physical purge and explicit resumption."""
        command = validate_removal_command(value)
        action = command.get("action", "")
        operation_id = command.get("operation_id", "")
        visibility_scope = command.get("visibility_scope", {})
        dependency_ids = command.get("dependency_ids", [])
        binding = {key: item for key, item in command.items() if key != "action"}
        with self.resolution_condition:
            if action == "resume" and self.internal_state == CoreState.RUNNING:
                return {"state": "running", "already_running": True}
            if action == "plan":
                retained = {}
                if self.internal_state != CoreState.RUNNING:
                    retained = self.engagement_maintenance
                    if (
                        self.internal_state not in {CoreState.QUIESCING, CoreState.MAINTENANCE}
                        or retained.get("operation_id", "") != operation_id
                        or retained.get("visibility_scope", {}) != visibility_scope
                        or (dependency_ids and retained.get("dependency_ids", []) != dependency_ids)
                    ):
                        raise LifecycleError("removal plan conflicts with the current maintenance owner")
                report = self.response_removal.execute(visibility_scope, retained.get("dependency_ids", dependency_ids))
                result = {**report, "maintenance_binding": deepcopy(retained), "purged": self.engagement_purged}
                return result
            if action == "prepare":
                if self.internal_state == CoreState.RUNNING:
                    self.engagement_maintenance = deepcopy(binding)
                    self.engagement_purged = False
                    self.internal_state = CoreState.QUIESCING
                    self.resolution_condition.notify_all()
                elif (
                    self.internal_state not in {CoreState.QUIESCING, CoreState.MAINTENANCE}
                    or self.engagement_maintenance != binding
                ):
                    raise LifecycleError("engagement maintenance belongs to another operation or lifecycle")
                while self.active_resolution_request_ids or self.active_graph_operations:
                    self.resolution_condition.wait()
                self.internal_state = CoreState.MAINTENANCE
                return {"state": "maintenance", "drained": True, "purged": self.engagement_purged}
            if self.internal_state != CoreState.MAINTENANCE or self.engagement_maintenance != binding:
                raise LifecycleError("engagement removal requires its exact drained maintenance owner")
            if action == "purge":
                report = self.response_removal.execute(visibility_scope, dependency_ids, dry_run=False)
                # Context is intentionally unscoped transport state, not an
                # accepted-response owner. Drop it after the drained purge.
                for records in (
                    self.conversations,
                    self.conversation_tokens,
                    self.conversation_activity,
                    self.resolution_requests,
                    self.proposals,
                    self.proposal_requests,
                    self.learn_requests,
                    self.retire_requests,
                ):
                    records.clear()
                self.resolution_accounting.clear()
                self.negative_resolutions.clear()
                with self.engram.session_lock:
                    self.engram.sessions.clear()
                self.engagement_purged = True
                result = {**report, "state": "maintenance", "purged": True}
                return result
            if not self.engagement_purged:
                raise LifecycleError("engagement maintenance cannot resume before a successful purge")
            self.engagement_maintenance = {}
            self.engagement_purged = False
            self.internal_state = CoreState.RUNNING
            self.resolution_condition.notify_all()
            return {"state": "running", "already_running": False}

    def close(self) -> bool:
        """Release all transport-independent runtime state."""
        with self.resolution_condition:
            if self.internal_state == CoreState.CLOSED:
                result = False
                return result
            if self.internal_state not in {CoreState.RUNNING, CoreState.MAINTENANCE}:
                raise LifecycleError(f"core cannot close while {self.internal_state.value}")
            self.internal_state = CoreState.CLOSING
            self.resolution_condition.notify_all()
            while self.active_resolution_request_ids or self.active_graph_operations:
                self.resolution_condition.wait()
            # Release Core-owned request, token, activity, and negative state at the drained
            # boundary. Each runtime is released as stop would, so an anonymous session goes too.
            for runtime in tuple(self.conversations.values()):
                self.release_conversation(runtime)
            for records in (self.conversations, self.conversation_tokens, self.conversation_activity, self.resolution_requests):
                records.clear()
            self.resolution_accounting.clear()
            self.negative_resolutions.clear()
            self.reset_regulated_state()
            if self.internal_owns_engram:
                with self.engram.session_lock:
                    self.engram.sessions.clear()
            disconnect = getattr(self.engram.graph_client, "disconnect", ())
            if callable(disconnect):
                try:
                    disconnect()
                except Exception as err:
                    LOGGER.warning("graph client disconnect failed while closing Engram", exc_info=err)
            self.internal_state = CoreState.CLOSED
            self.resolution_condition.notify_all()
            result = True
            return result

    def regulated_cache_metrics(self) -> dict:
        """Return a JSON-ready snapshot of regulated-cache activity."""
        with self.lock:
            self.require_running()
            result = {
                **deepcopy(self.regulated_metrics),
                "pending_proposals": sum(1 for record in self.proposals.values() if not record.get("resolution", {})),
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
        request_id = record.get("proposal", {}).get("request_id", "")
        if self.proposal_requests.get(request_id, "") == proposal_id:
            self.proposal_requests.pop(request_id, "")
        return True

    # Feedback learning, in engram.service_feedback.
    candidate_generation = candidate_generation
    feedback_observations = feedback_observations
    apply_feedback = apply_feedback
    ensure_resolution_candidacy = ensure_resolution_candidacy
    ensure_proposal_candidacy = ensure_proposal_candidacy
    proposal_feedback_observations = proposal_feedback_observations
    record_resolution_feedback = record_resolution_feedback
    inspect_feedback_learning = inspect_feedback_learning

    # Cache resolution, in engram.service_resolution.
    internal_negative_key = internal_negative_key
    resolve_request = resolve_request

    # Response proposals, in engram.service_proposals.
    propose = propose
    resolve = resolve
    current_proposal_candidate = current_proposal_candidate
    complete_proposal_resolution = complete_proposal_resolution

    # Conversations, in engram.service_conversations.
    start_conversation = start_conversation
    owned_conversation = owned_conversation
    release_conversation = release_conversation
    make_conversation_room = make_conversation_room
    get_conversation = get_conversation
    chat = chat
    inspect_conversation = inspect_conversation
    add_fact = add_fact
    finish_conversation = finish_conversation
    stop_conversation = stop_conversation
    set_predicate = set_predicate
    get_predicate = get_predicate

    # Accepted-response maintenance, in engram.service_responses.
    learn_response = learn_response
    supersede_response = supersede_response
    retire_response = retire_response
    responses_by_support = responses_by_support
