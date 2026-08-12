"""Transport-neutral application facade for Engram interfaces.

``EngramCore`` owns the shared engine, user-bound conversation runtimes,
persistence lifecycle, and regulated response-cache transactions. Interfaces
such as MCP and the CLI translate their inputs and outputs at the boundary;
this module contains no transport-specific types or behavior.
"""

import contextlib
import json
import threading
import time
from collections import Counter
from copy import deepcopy
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from uuid import uuid4

from engram import persistence, sessions
from engram.config import engram_config
from engram.constants import Tier
from engram.conversation import ConversationRuntime, statement_view
from engram.core import Engram
from engram.errors import ConflictError, InvalidRequestError, LifecycleError, PersistenceError, ResourceNotFoundError
from engram.models import record_statement_query
from engram.text import normalize

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SEED_PATH = REPO_ROOT / "data" / "seed.json"
PROPOSAL_TTL_SECONDS = 300
MAX_TRANSIENT_RECORDS = 1000
EMPTY_CONFIG: dict = {}
EMPTY_METADATA: dict = {}
REGULATOR_OUTCOMES = frozenset(
    {
        "accepted",
        "rejected_quality",
        "rejected_context",
        "rejected_stale",
        "rejected_policy",
    }
)


class CoreState(StrEnum):
    """Lifecycle state of the single owned application core."""

    RUNNING = "running"
    CLOSING = "closing"
    CLOSED = "closed"


class DurabilityState(StrEnum):
    """Current relationship between live state and configured persistence."""

    DISABLED = "disabled"
    HEALTHY = "healthy"
    DEGRADED = "degraded"


def resolve_seed_path(seed_path: str) -> Path:
    """Resolve the bundled seed independently of an interface's working directory."""
    candidate = Path(seed_path)
    if seed_path == "data/seed.json" and not candidate.exists():
        candidate = DEFAULT_SEED_PATH
    return candidate.resolve()


class EngramCore:
    """Shared application runtime used by every Engram interface."""

    def __init__(
        self,
        engram=(),
        store_path: str = "",
        *,
        checkpoint_on_mutation: bool = True,
    ) -> None:
        if not isinstance(checkpoint_on_mutation, bool):
            raise InvalidRequestError("checkpoint_on_mutation must be a boolean")
        self.engram = engram or Engram()
        self.store_path = str(Path(store_path).resolve()) if store_path else ""
        self.checkpoint_on_mutation = checkpoint_on_mutation
        self.conversations: dict[str, ConversationRuntime] = {}
        self.lock = threading.RLock()
        self._state = CoreState.RUNNING
        self._durability = DurabilityState.HEALTHY if self.store_path else DurabilityState.DISABLED
        self._dirty = False
        self._last_checkpoint_at = ""
        self._last_persistence_error = ""
        self._component_status = deepcopy(self.engram.component_status)
        self._reset_regulated_state()

    @classmethod
    def open(
        cls,
        *,
        config: dict = EMPTY_CONFIG,
        store_path: str = "",
        seed_path: str = "",
        checkpoint_on_mutation: bool = True,
    ) -> "EngramCore":
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

        core = cls(
            engram,
            store_path=resolved_store or "",
            checkpoint_on_mutation=checkpoint_on_mutation,
        )
        if seed_path:
            core._dirty = True
            core._checkpoint()
        return core

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
            return {
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
            normalized_user_id = self._normalize_user_id(user_id)
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
            return {
                "started": True,
                "user_id": snapshot["user_id"],
                "initial_bot_text": snapshot["initial_bot_text"],
                "turn_count": snapshot["turn_count"],
                "statement_count": snapshot["metrics"]["statement_count"],
                "store_path": str(self.store_path) if self.store_path else "",
            }

    def get_conversation(self, user_id: str) -> ConversationRuntime:
        """Return an active user runtime or raise a lifecycle error."""
        with self.lock:
            self._require_running()
            normalized_user_id = self._normalize_user_id(user_id)
            runtime = self.conversations.get(normalized_user_id, {})
            if not runtime:
                raise ResourceNotFoundError(f"no active conversation for user_id: {normalized_user_id}")
            return runtime

    def chat(self, user_id: str, text: str) -> dict:
        """Submit one chatbot turn to an active user conversation."""
        with self.lock:
            self._require_running()
            runtime = self.get_conversation(user_id)
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
            return runtime.write_report(output_prefix)

    def stop_conversation(self, user_id: str, *, flush: bool = True) -> dict:
        """End one conversation while leaving the shared core available."""
        with self.lock:
            self._require_running()
            runtime = self.get_conversation(user_id)
            report = runtime.report()
            if flush:
                self.flush()
            self.conversations.pop(runtime.user_id, {})
            return {
                "stopped": True,
                "user_id": report["user_id"],
                "summary": report["summary"],
            }

    def set_predicate(self, user_id: str, name: str, value: str) -> None:
        """Set one caller-owned predicate on a user context."""
        with self.lock:
            self._require_running()
            self._require_text(name, "name")
            self._require_string(value, "value")
            normalized_user_id = self._normalize_user_id(user_id)
            session = sessions.get_session(self.engram, normalized_user_id, create_if_missing=True)
            with self.engram.session_lock:
                session["predicates"][name] = value
            self._dirty = True
            self._checkpoint()

    def get_predicate(self, user_id: str, name: str, default=""):
        """Read one caller-owned predicate from a user context."""
        with self.lock:
            self._require_running()
            normalized_user_id = self._normalize_user_id(user_id)
            session = sessions.get_session(self.engram, normalized_user_id, create_if_missing=False)
            if not session:
                return default
            with self.engram.session_lock:
                return session["predicates"].get(name, default)

    def flush(self) -> bool:
        """Atomically save configured state; return False when no store is configured."""
        with self.lock:
            self._require_running()
            return self._flush_store()

    def warm_vector_recall(self) -> bool:
        """Re-run component preflight and report whether vector recall is ready."""
        with self.lock:
            self._require_running()
            try:
                self._component_status = self.engram.preflight_components()
                self.engram.component_status = deepcopy(self._component_status)
                return self._component_status["vector"]["ready"] and self._component_status["vector"]["enabled"]
            except Exception as error:
                raise InvalidRequestError(f"unable to initialize vector recall: {error}") from error

    def close(self, *, flush: bool = True) -> bool:
        """Flush and release all transport-independent runtime state."""
        with self.lock:
            if self._state == CoreState.CLOSED:
                return False
            if self._state != CoreState.RUNNING:
                raise LifecycleError(f"core cannot close while {self._state.value}")
            self._state = CoreState.CLOSING
            try:
                if flush:
                    self._flush_store()
            except PersistenceError:
                self._state = CoreState.RUNNING
                raise
            self.conversations.clear()
            self._reset_regulated_state()
            self._state = CoreState.CLOSED
            return True

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
            self._require_text(request, "request")
            self._require_text(request_id, "request_id")
            self._require_string(namespace, "namespace")
            self._require_string(context_fingerprint, "context_fingerprint")
            self._require_string(required_source_label, "required_source_label")
            if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 10:
                raise InvalidRequestError("limit must be an integer from 1 through 10")
            if not isinstance(required_metadata, dict):
                raise InvalidRequestError("required_metadata must be an object")
            required_metadata = dict(required_metadata)
            normalized_user_id = self._normalize_user_id(user_id)
            signature = self._signature(
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
                self.regulated_metrics["idempotent_retries"] += 1
                self._checkpoint()
                return self._proposal_result(existing, idempotent=True)

            def statement_matches_scope(statement: dict) -> bool:
                if statement["pattern"]:
                    return False
                template = statement.get("template", {})
                tapestry_metadata = template.get("tapestry", {}) if isinstance(template, dict) else {}
                if not isinstance(tapestry_metadata, dict):
                    return False
                if tapestry_metadata.get("namespace", "") != namespace:
                    return False
                if tapestry_metadata.get("context_fingerprint", "") != context_fingerprint:
                    return False
                if required_source_label and statement.get("source_label", "") != required_source_label:
                    return False
                return all(tapestry_metadata.get(key) == value for key, value in required_metadata.items())

            query_result = self.engram.query(
                request,
                user_id=normalized_user_id,
                limit=limit,
                statement_filter=statement_matches_scope,
                record_candidates=False,
            )
            vector_matches = self.engram.vector_supported_matches(
                request,
                limit=limit,
                statement_filter=statement_matches_scope,
            )
            merged = {}
            for source, matches in (
                ("keyword", query_result["matches"]),
                ("vector", vector_matches),
            ):
                for statement, score in matches:
                    entry = merged.setdefault(
                        statement["id"],
                        {
                            "statement": statement,
                            "keyword_score": 0.0,
                            "vector_score": 0.0,
                        },
                    )
                    entry[f"{source}_score"] = max(float(entry[f"{source}_score"]), float(score))
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
            with self.engram.statement_lock:
                for entry in ranked:
                    statement = entry["statement"]
                    selected_score = max(entry["keyword_score"], entry["vector_score"])
                    record_statement_query(statement)
                    candidate = self._candidate_result(statement, selected_score)
                    candidate["retrieval"] = {
                        "keyword_score": entry["keyword_score"],
                        "vector_score": entry["vector_score"],
                        "selected": ("vector" if entry["vector_score"] > entry["keyword_score"] else "keyword"),
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
            }
            self.proposal_requests[request_id] = proposal_id
            self.regulated_metrics["proposals"] += 1
            if not candidates:
                self.regulated_metrics["misses"] += 1
            self._enforce_transient_bound()
            self._dirty = True
            self._checkpoint()
            return self._proposal_result(self.proposals[proposal_id], idempotent=False)

    def resolve(self, proposal_id: str, outcome: str, statement_id: str = "", reason: str = "") -> dict:
        """Commit one Regulator verdict without double-crediting retries."""
        with self.lock:
            self._require_running()
            self._cleanup_transient()
            self._require_text(proposal_id, "proposal_id")
            self._require_string(outcome, "outcome")
            self._require_string(statement_id, "statement_id")
            self._require_string(reason, "reason")
            if outcome not in REGULATOR_OUTCOMES:
                supported = ", ".join(sorted(REGULATOR_OUTCOMES))
                raise InvalidRequestError(f"outcome must be one of: {supported}")

            record = self.proposals.get(proposal_id, {})
            if not record:
                raise ResourceNotFoundError("unknown or expired proposal_id")
            resolution_signature = self._signature(outcome=outcome, statement_id=statement_id, reason=reason)
            if record["resolution"]:
                if record["resolution_signature"] != resolution_signature:
                    raise ConflictError("proposal has already been resolved with a different verdict")
                self.regulated_metrics["idempotent_retries"] += 1
                result = deepcopy(record["resolution"])
                result["idempotent"] = True
                if outcome == "accepted":
                    self._checkpoint()
                return result

            candidate_responses = record["candidate_responses"]
            if statement_id and statement_id not in candidate_responses:
                raise InvalidRequestError("statement_id is not a candidate in this proposal")
            if outcome == "accepted":
                if not statement_id:
                    raise InvalidRequestError("accepted outcomes require statement_id")
                current = self.engram.get_statement(statement_id)
                if not current or current["text"] != candidate_responses[statement_id]:
                    raise ConflictError("candidate is no longer current; resolve it as rejected_stale")
                proposal = record["proposal"]
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
                "resolved": True,
                "idempotent": False,
            }
            record["resolution_signature"] = resolution_signature
            record["resolution"] = resolution
            if outcome == "accepted":
                self._checkpoint()
            return deepcopy(resolution)

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
        """Cache an Actor response, replacing only within its exact scope."""
        with self.lock:
            self._require_running()
            self._cleanup_transient()
            self._require_text(request, "request")
            self._require_text(response, "response")
            self._require_text(request_id, "request_id")
            self._require_string(namespace, "namespace")
            self._require_string(context_fingerprint, "context_fingerprint")
            self._require_string(source_label, "source_label")
            if normalize(response) == "idk":
                raise InvalidRequestError("IDK is not a cacheable response")
            if not isinstance(metadata, dict):
                raise InvalidRequestError("metadata must be an object")
            metadata = dict(metadata)
            normalized_user_id = self._normalize_user_id(user_id)
            signature = self._signature(
                request=request,
                response=response,
                user_id=normalized_user_id,
                namespace=namespace,
                context_fingerprint=context_fingerprint,
                source_label=source_label,
                metadata=metadata,
            )
            previous = self.learn_requests.get(request_id, {})
            if previous:
                if previous["signature"] != signature:
                    raise ConflictError("request_id is already associated with a different learned response")
                self.regulated_metrics["idempotent_retries"] += 1
                result = deepcopy(previous["result"])
                result["idempotent"] = True
                self._checkpoint()
                return result

            tapestry_metadata = deepcopy(metadata)
            tapestry_metadata.update(
                {
                    "namespace": namespace,
                    "context_fingerprint": context_fingerprint,
                    "request_id": request_id,
                }
            )
            with self.engram.statement_lock:
                statement_ids_before = set(self.engram.statement_index)
            statement_id = self.engram.learn_from_response(
                request,
                response,
                template={"tapestry": tapestry_metadata},
                introduced_by_user_id="",
                source_label=source_label,
            )
            action = "replaced" if statement_id in statement_ids_before else "created"
            sessions.get_session(self.engram, normalized_user_id, create_if_missing=True)
            sessions.update_session_context(self.engram, normalized_user_id, response)
            result = {
                "learned": True,
                "statement_id": statement_id,
                "action": action,
                "request_id": request_id,
                "user_id": normalized_user_id,
                "namespace": namespace,
                "context_fingerprint": context_fingerprint,
                "source_label": source_label,
                "idempotent": False,
            }
            self.learn_requests[request_id] = {
                "created_at": time.monotonic(),
                "signature": signature,
                "result": result,
            }
            self.regulated_metrics[f"learned_{action}"] += 1
            self._enforce_transient_bound()
            self._dirty = True
            self._checkpoint()
            return deepcopy(result)

    def retire_response(self, statement_id: str, reason: str, request_id: str) -> dict:
        """Retire one dynamic, patternless response-cache entry."""
        with self.lock:
            self._require_running()
            self._cleanup_transient()
            self._require_text(statement_id, "statement_id")
            self._require_text(reason, "reason")
            self._require_text(request_id, "request_id")
            signature = self._signature(statement_id=statement_id, reason=reason)
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
            return deepcopy(result)

    def regulated_cache_metrics(self) -> dict:
        """Return a JSON-ready snapshot of regulated-cache activity."""
        with self.lock:
            self._require_running()
            self._cleanup_transient()
            return {
                **deepcopy(self.regulated_metrics),
                "pending_proposals": sum(1 for record in self.proposals.values() if not record["resolution"]),
                "retained_proposals": len(self.proposals),
            }

    @staticmethod
    def _require_text(value: str, name: str) -> None:
        if not isinstance(value, str) or not value.strip():
            raise InvalidRequestError(f"{name} must be a non-empty string")

    @staticmethod
    def _require_string(value: str, name: str) -> None:
        if not isinstance(value, str):
            raise InvalidRequestError(f"{name} must be a string")

    @staticmethod
    def _signature(**values) -> str:
        try:
            return json.dumps(values, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
        except (TypeError, ValueError) as error:
            raise InvalidRequestError("metadata and request values must be JSON-compatible") from error

    @staticmethod
    def _candidate_result(statement: dict, score: float) -> dict:
        return {
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

    @staticmethod
    def _proposal_result(record: dict, *, idempotent: bool) -> dict:
        result = deepcopy(record["proposal"])
        result["idempotent"] = idempotent
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

    def _flush_store(self) -> bool:
        if not self.store_path:
            self._durability = DurabilityState.DISABLED
            self._dirty = False
            return False
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
        return True

    def _require_running(self) -> None:
        if self._state != CoreState.RUNNING:
            raise LifecycleError(f"core is {self._state.value}; operation requires running state")

    @staticmethod
    def _normalize_user_id(user_id: str) -> str:
        try:
            return sessions.normalize_user_id(user_id)
        except ValueError as error:
            raise InvalidRequestError(str(error)) from error

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
