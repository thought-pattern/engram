"""Transport-neutral application facade for Engram interfaces.

``EngramCore`` owns the shared engine, user-bound conversation runtimes,
persistence lifecycle, and regulated response-cache transactions. Interfaces
such as MCP and the CLI translate their inputs and outputs at the boundary;
this module contains no transport-specific types or behavior.
"""

from collections import Counter
from contextlib import suppress as contextlib_suppress
from copy import deepcopy
from datetime import UTC, datetime
from enum import StrEnum
from json import JSONDecodeError as json_JSONDecodeError, dumps as json_dumps, loads as json_loads
from pathlib import Path
from threading import RLock as threading_RLock
from time import monotonic as time_monotonic
from uuid import uuid4

from engram import persistence, sessions
from engram.config import engram_config
from engram.constants import NULL_DATETIME, Tier
from engram.conversation import ConversationRuntime, statement_view
from engram.core import Engram
from engram.errors import (
    ConflictError,
    InvalidRequestError,
    LifecycleError,
    PersistenceError,
    ResourceNotFoundError,
)
from engram.models import record_statement_query
from engram.text import normalize

_DEFAULT_ARGUMENT_DICT = {}

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SEED_PATH = REPO_ROOT / "data" / "seed.json"
PROPOSAL_TTL_SECONDS = 300
MAX_TRANSIENT_RECORDS = 1000
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
    _return_value = candidate.resolve()
    return _return_value


class EngramCore:
    """Shared application runtime used by every Engram interface."""

    def __init__(
        self,
        engram: Engram = None,
        store_path: object = "",
        *,
        checkpoint_on_mutation: bool = True,
    ) -> None:
        if not isinstance(checkpoint_on_mutation, bool):
            raise InvalidRequestError("checkpoint_on_mutation must be a boolean")
        self.engram = engram or Engram()
        self.store_path = Path(store_path).resolve() if store_path else None
        self.checkpoint_on_mutation = checkpoint_on_mutation
        self.conversations: dict[str, ConversationRuntime] = {}
        self.lock = threading_RLock()
        self._state = CoreState.RUNNING
        self._durability = DurabilityState.HEALTHY if self.store_path else DurabilityState.DISABLED
        self._dirty = False
        self._last_checkpoint_at = ""
        self._last_persistence_error = ""
        self._reset_regulated_state()

    @classmethod
    def open(
        cls,
        *,
        config: dict = _DEFAULT_ARGUMENT_DICT,
        store_path: object = "",
        seed_path: object = "",
        checkpoint_on_mutation: bool = True,
    ) -> "EngramCore":
        """Load or create a core, optionally synchronizing a seed corpus."""
        if config is None:
            config = _DEFAULT_ARGUMENT_DICT
        resolved_store = Path(store_path).resolve() if store_path else None
        if resolved_store and resolved_store.exists():
            try:
                engram = persistence.load_engram(resolved_store, config=config)
            except Exception as error:
                raise PersistenceError("store load", error, state_changed=False) from error
        else:
            try:
                core_config = config if config is not _DEFAULT_ARGUMENT_DICT else engram_config()
                engram = Engram(config=core_config)
            except ValueError as error:
                raise InvalidRequestError(str(error)) from error

        if seed_path:
            resolved_seed = resolve_seed_path(str(seed_path))
            if not resolved_seed.exists():
                raise ResourceNotFoundError(f"seed file not found: {resolved_seed}")
            try:
                seed_data = json_loads(resolved_seed.read_text(encoding="utf-8"))
                engram.sync_corpus(seed_data.get("pairs", []))
            except (OSError, json_JSONDecodeError, ValueError) as error:
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

    def __exit__(self, exc_type, exc_value, traceback) -> bool:
        """Flush configured state and release runtime resources."""
        self.close()
        return False

    def status(self) -> dict:
        """Return transport-neutral lifecycle and durability readiness."""
        with self.lock:
            accepting_requests = self._state == CoreState.RUNNING
            durability_healthy = self._durability != DurabilityState.DEGRADED
            _return_value = {
                "state": self._state.value,
                "ready": accepting_requests,
                "healthy": accepting_requests and durability_healthy,
                "durability": self._durability.value,
                "dirty": self._dirty,
                "last_checkpoint_at": self._last_checkpoint_at,
                "last_persistence_error": self._last_persistence_error,
                "active_conversations": len(self.conversations),
                "store_path": str(self.store_path) if self.store_path else "",
            }
            return _return_value
        return {}

    def start_conversation(
        self,
        user_id: str = "0",
        initial_bot_text: str = "",
        transcript_path: object = "",
        random_seed: int = None,
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
                    transcript_path=transcript_path or None,
                )
            except ValueError as error:
                raise InvalidRequestError(str(error)) from error
            self.conversations[normalized_user_id] = runtime
            snapshot = runtime.inspect()
            self._dirty = True
            self._checkpoint()
            _return_value = {
                "started": True,
                "user_id": snapshot.get("user_id", ""),
                "initial_bot_text": snapshot.get("initial_bot_text", ""),
                "turn_count": snapshot.get("turn_count", 0),
                "statement_count": snapshot.get("metrics", {}).get("statement_count", 0),
                "store_path": str(self.store_path) if self.store_path else "",
            }
            return _return_value
        return {}

    def get_conversation(self, user_id: str) -> ConversationRuntime:
        """Return an active user runtime or raise a lifecycle error."""
        with self.lock:
            self._require_running()
            normalized_user_id = self._normalize_user_id(user_id)
            runtime = self.conversations.get(normalized_user_id, False)
            if runtime is False:
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
        return {}

    def inspect_conversation(self, user_id: str) -> dict:
        """Inspect one conversation and shared regulated-cache metrics."""
        with self.lock:
            self._require_running()
            self._cleanup_transient()
            snapshot = self.get_conversation(user_id).inspect()
            snapshot["regulated_cache"] = self.regulated_cache_metrics()
            snapshot["core_status"] = self.status()
            return snapshot
        return {}

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
        return {}

    def finish_conversation(self, user_id: str, output_prefix: object = "engram-transcript") -> dict:
        """Persist the store and write reports without ending a conversation."""
        with self.lock:
            self._require_running()
            runtime = self.get_conversation(user_id)
            self.flush()
            _return_value = runtime.write_report(output_prefix)
            return _return_value
        return {}

    def stop_conversation(self, user_id: str, *, flush: bool = True) -> dict:
        """End one conversation while leaving the shared core available."""
        with self.lock:
            self._require_running()
            runtime = self.get_conversation(user_id)
            report = runtime.report()
            if flush:
                self.flush()
            self.conversations.pop(runtime.user_id, False)
            _return_value = {
                "stopped": True,
                "user_id": report.get("user_id", ""),
                "summary": report.get("summary", False),
            }
            return _return_value
        return {}

    def set_predicate(self, user_id: str, name: str, value: str) -> bool:
        """Set one caller-owned predicate on a user context."""
        with self.lock:
            self._require_running()
            self._require_text(name, "name")
            self._require_string(value, "value")
            normalized_user_id = self._normalize_user_id(user_id)
            session = sessions.get_session(self.engram, normalized_user_id, create_if_missing=True)
            with self.engram.session_lock:
                session.get("predicates", {})[name] = value
            self._dirty = True
            self._checkpoint()
        return True

    def get_predicate(self, user_id: str, name: str, default=""):
        """Read one caller-owned predicate from a user context."""
        with self.lock:
            self._require_running()
            normalized_user_id = self._normalize_user_id(user_id)
            session = sessions.get_session(self.engram, normalized_user_id, create_if_missing=False)
            if session is False:
                return default
            with self.engram.session_lock:
                _return_value = session.get("predicates", {}).get(name, default)
                return _return_value

    def flush(self) -> bool:
        """Atomically save configured state; return False when no store is configured."""
        with self.lock:
            self._require_running()
            _return_value = self._flush_store()
            return _return_value

    def warm_vector_recall(self) -> bool:
        """Warm and verify optional graph-vector retrieval before serving."""
        with self.lock:
            self._require_running()
            try:
                _return_value = self.engram.warm_vector_recall()
                return _return_value
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
        required_metadata: dict = _DEFAULT_ARGUMENT_DICT,
        required_source_label: str = "",
    ) -> dict:
        """Create a speculative, uncredited response-cache proposal."""
        if required_metadata is None:
            required_metadata = _DEFAULT_ARGUMENT_DICT
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
            if required_metadata is _DEFAULT_ARGUMENT_DICT:
                required_metadata = {}
            if not isinstance(required_metadata, dict):
                raise InvalidRequestError("required_metadata must be an object or None")
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

            existing_proposal_id = self.proposal_requests.get(request_id, False)
            if existing_proposal_id:
                existing = self.proposals.get(existing_proposal_id, False)
                if existing.get("signature", False) != signature:
                    raise ConflictError("request_id is already associated with a different proposal request")
                self.regulated_metrics["idempotent_retries"] += 1
                self._checkpoint()
                _return_value = self._proposal_result(existing, idempotent=True)
                return _return_value

            def statement_matches_scope(statement: dict) -> bool:
                if statement.get("pattern", ""):
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
                _return_value = all(tapestry_metadata.get(key, "") == value for key, value in required_metadata.items())
                return _return_value

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
                ("keyword", query_result.get("matches", [])),
                ("vector", vector_matches),
            ):
                for statement, score in matches:
                    entry = merged.setdefault(
                        statement.get("id", ""),
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
                    max(entry.get("keyword_score", 0.0), entry.get("vector_score", 0.0)),
                    entry.get("statement", {}).get("created_at", NULL_DATETIME),
                    entry.get("statement", {}).get("id", ""),
                ),
                reverse=True,
            )[:limit]
            candidates = []
            with self.engram.statement_lock:
                for entry in ranked:
                    statement = entry.get("statement", {})
                    selected_score = max(entry.get("keyword_score", 0.0), entry.get("vector_score", 0.0))
                    record_statement_query(statement)
                    candidate = self._candidate_result(statement, selected_score)
                    candidate["retrieval"] = {
                        "keyword_score": entry.get("keyword_score", 0.0),
                        "vector_score": entry.get("vector_score", 0.0),
                        "selected": ("vector" if entry.get("vector_score", 0.0) > entry.get("keyword_score", 0.0) else "keyword"),
                    }
                    candidates.append(candidate)
            proposal_id = f"proposal_{uuid4().hex[:16]}"
            proposal = {
                "proposal_id": proposal_id,
                "request_id": request_id,
                "user_id": normalized_user_id,
                "namespace": namespace,
                "context_fingerprint": context_fingerprint,
                "resolved_request": query_result.get("resolved_query", ""),
                "keywords": list(query_result.get("keywords", [])),
                "candidates": candidates,
            }
            self.proposals[proposal_id] = {
                "created_at": time_monotonic(),
                "signature": signature,
                "proposal": proposal,
                "candidate_responses": {
                    candidate.get("statement_id", ""): candidate.get("response", "") for candidate in candidates
                },
                "resolution": False,
            }
            self.proposal_requests[request_id] = proposal_id
            self.regulated_metrics["proposals"] += 1
            if not candidates:
                self.regulated_metrics["misses"] += 1
            self._enforce_transient_bound()
            self._dirty = True
            self._checkpoint()
            _return_value = self._proposal_result(self.proposals.get(proposal_id, False), idempotent=False)
            return _return_value
        return {}

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

            record = self.proposals.get(proposal_id, False)
            if record is False:
                raise ResourceNotFoundError("unknown or expired proposal_id")
            resolution_signature = self._signature(outcome=outcome, statement_id=statement_id, reason=reason)
            if record.get("resolution", False) is not False:
                if record.get("resolution_signature", False) != resolution_signature:
                    raise ConflictError("proposal has already been resolved with a different verdict")
                self.regulated_metrics["idempotent_retries"] += 1
                result = deepcopy(record.get("resolution", False))
                result["idempotent"] = True
                if outcome == "accepted":
                    self._checkpoint()
                return result

            candidate_responses = record.get("candidate_responses", [])
            if statement_id and statement_id not in candidate_responses:
                raise InvalidRequestError("statement_id is not a candidate in this proposal")
            if outcome == "accepted":
                if not statement_id:
                    raise InvalidRequestError("accepted outcomes require statement_id")
                current = self.engram.get_statement(statement_id)
                if not current or current.get("text", "") != candidate_responses.get(statement_id, False):
                    raise ConflictError("candidate is no longer current; resolve it as rejected_stale")
                proposal = record.get("proposal", False)
                self.engram.record_hit(proposal.get("keywords", []), statement_id=statement_id)
                sessions.get_session(self.engram, proposal.get("user_id", ""), create_if_missing=True)
                sessions.update_session_context(self.engram, proposal.get("user_id", ""), current.get("text", ""))
                self.regulated_metrics["accepted"] += 1
                self._dirty = True
            else:
                self.regulated_metrics.get("rejections", {})[outcome] += 1

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
            _return_value = deepcopy(resolution)
            return _return_value
        return {}

    def learn_response(
        self,
        request: str,
        response: str,
        request_id: str,
        user_id: str = "0",
        namespace: str = "",
        context_fingerprint: str = "",
        source_label: str = "tapestry:actor",
        metadata: dict = _DEFAULT_ARGUMENT_DICT,
    ) -> dict:
        """Cache an Actor response, replacing only within its exact scope."""
        if metadata is None:
            metadata = _DEFAULT_ARGUMENT_DICT
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
            if metadata is _DEFAULT_ARGUMENT_DICT:
                metadata = {}
            if not isinstance(metadata, dict):
                raise InvalidRequestError("metadata must be an object or None")
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
            previous = self.learn_requests.get(request_id, False)
            if previous is not False:
                if previous.get("signature", False) != signature:
                    raise ConflictError("request_id is already associated with a different learned response")
                self.regulated_metrics["idempotent_retries"] += 1
                result = deepcopy(previous.get("result", {}))
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
                "created_at": time_monotonic(),
                "signature": signature,
                "result": result,
            }
            self.regulated_metrics[f"learned_{action}"] += 1
            self._enforce_transient_bound()
            self._dirty = True
            self._checkpoint()
            _return_value = deepcopy(result)
            return _return_value
        return {}

    def retire_response(self, statement_id: str, reason: str, request_id: str) -> dict:
        """Retire one dynamic, patternless response-cache entry."""
        with self.lock:
            self._require_running()
            self._cleanup_transient()
            self._require_text(statement_id, "statement_id")
            self._require_text(reason, "reason")
            self._require_text(request_id, "request_id")
            signature = self._signature(statement_id=statement_id, reason=reason)
            previous = self.retire_requests.get(request_id, False)
            if previous is not False:
                if previous.get("signature", False) != signature:
                    raise ConflictError("request_id is already associated with a different retirement")
                self.regulated_metrics["idempotent_retries"] += 1
                result = deepcopy(previous.get("result", {}))
                result["idempotent"] = True
                self._checkpoint()
                return result

            statement = self.engram.get_statement(statement_id)
            if not statement:
                raise ResourceNotFoundError("unknown statement_id")
            if statement.get("tier", "") != Tier.DYNAMIC or statement.get("pattern", ""):
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
                "created_at": time_monotonic(),
                "signature": signature,
                "result": result,
            }
            self.regulated_metrics["retired"] += 1
            self._enforce_transient_bound()
            self._dirty = True
            self._checkpoint()
            _return_value = deepcopy(result)
            return _return_value
        return {}

    def regulated_cache_metrics(self) -> dict:
        """Return a JSON-ready snapshot of regulated-cache activity."""
        with self.lock:
            self._require_running()
            self._cleanup_transient()
            _return_value = {
                **deepcopy(self.regulated_metrics),
                "pending_proposals": sum(1 for record in self.proposals.values() if record.get("resolution", False) is False),
                "retained_proposals": len(self.proposals),
            }
            return _return_value
        return {}

    @staticmethod
    def _require_text(value: str, name: str) -> bool:
        if not isinstance(value, str) or not value.strip():
            raise InvalidRequestError(f"{name} must be a non-empty string")
        return False

    @staticmethod
    def _require_string(value: str, name: str) -> bool:
        if not isinstance(value, str):
            raise InvalidRequestError(f"{name} must be a string")
        return False

    @staticmethod
    def _signature(**values) -> str:
        try:
            _return_value = json_dumps(values, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
            return _return_value
        except (TypeError, ValueError) as error:
            raise InvalidRequestError("metadata and request values must be JSON-compatible") from error
        return ""

    @staticmethod
    def _candidate_result(statement: dict, score: float) -> dict:
        _return_value = {
            "statement_id": statement.get("id", ""),
            "response": statement.get("text", ""),
            "score": score,
            "tier": statement.get("tier", Tier.DYNAMIC).value,
            "created_at": statement.get("created_at", NULL_DATETIME).isoformat(),
            "hit_count": statement.get("hit_count", 0),
            "query_count": statement.get("query_count", 0),
            "source_label": statement.get("source_label", ""),
            "introduced_by_user_id": statement.get("introduced_by_user_id", ""),
            "metadata": deepcopy(statement.get("template", {})),
        }
        return _return_value

    @staticmethod
    def _proposal_result(record: dict, *, idempotent: bool) -> dict:
        result = deepcopy(record.get("proposal", False))
        result["idempotent"] = idempotent
        return result

    def _reset_regulated_state(self) -> bool:
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
        return False

    def _checkpoint(self) -> bool:
        if self.checkpoint_on_mutation:
            self._flush_store()
        return False

    def _flush_store(self) -> bool:
        if self.store_path is None:
            self._durability = DurabilityState.DISABLED
            self._dirty = False
            return False
        try:
            self.store_path.parent.mkdir(parents=True, exist_ok=True)
            with contextlib_suppress(OSError):
                self.store_path.parent.chmod(0o700)
            persistence.save(self.engram, self.store_path)
        except Exception as error:
            self._durability = DurabilityState.DEGRADED
            self._last_persistence_error = str(error)
            raise PersistenceError("store checkpoint", error, state_changed=self._dirty) from error
        self._durability = DurabilityState.HEALTHY
        self._dirty = False
        self._last_checkpoint_at = datetime.now(UTC).isoformat()
        self._last_persistence_error = ""
        return True

    def _require_running(self) -> bool:
        if self._state != CoreState.RUNNING:
            raise LifecycleError(f"core is {self._state.value}; operation requires running state")
        return False

    @staticmethod
    def _normalize_user_id(user_id: str) -> str:
        try:
            _return_value = sessions.normalize_user_id(user_id)
            return _return_value
        except ValueError as error:
            raise InvalidRequestError(str(error)) from error
        return ""

    def _cleanup_transient(self) -> bool:
        cutoff = time_monotonic() - PROPOSAL_TTL_SECONDS
        expired_proposals = [
            proposal_id for proposal_id, record in self.proposals.items() if record.get("created_at", 0.0) < cutoff
        ]
        for proposal_id in expired_proposals:
            self._remove_proposal(proposal_id)
        for records in (self.learn_requests, self.retire_requests):
            expired_request_ids = [request_id for request_id, record in records.items() if record.get("created_at", 0.0) < cutoff]
            for request_id in expired_request_ids:
                records.pop(request_id, False)
        return False

    def _enforce_transient_bound(self) -> bool:
        while len(self.proposals) > MAX_TRANSIENT_RECORDS:
            self._remove_proposal(next(iter(self.proposals)))
        for records in (self.learn_requests, self.retire_requests):
            while len(records) > MAX_TRANSIENT_RECORDS:
                records.pop(next(iter(records)))
        return False

    def _remove_proposal(self, proposal_id: str) -> bool:
        record = self.proposals.pop(proposal_id, False)
        if record is False:
            return False
        request_id = record.get("proposal", {}).get("request_id", "")
        if self.proposal_requests.get(request_id, "") == proposal_id:
            self.proposal_requests.pop(request_id, False)
        return False
