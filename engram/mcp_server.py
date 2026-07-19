"""FastMCP adapter for persistent Engram conversations.

The MCP host owns this process and keeps it alive across tool calls.  All
conversation behavior is delegated to the same programmatic API used by Python
callers and the human CLI.
"""

import json
import threading
import time
from collections import Counter
from copy import deepcopy
from pathlib import Path
from uuid import uuid4

from mcp.server.fastmcp import FastMCP

from engram import persistence, sessions
from engram.config import engram_config, load_config
from engram.constants import Tier
from engram.conversation import ConversationRuntime, statement_view
from engram.core import Engram
from engram.text import normalize

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


def _resolve_seed_path(seed_path: str) -> Path:
    """Resolve the bundled seed independently of the MCP host's working directory."""
    candidate = Path(seed_path)
    if seed_path == "data/seed.json" and not candidate.exists():
        candidate = DEFAULT_SEED_PATH
    return candidate.resolve()


class MCPConversationService:
    """Stateful implementation behind the FastMCP tool declarations."""

    def __init__(self) -> None:
        self.runtime: ConversationRuntime | None = None
        self.store_path: Path | None = None
        self.lock = threading.RLock()
        self._reset_regulated_state()

    def start(
        self,
        user_id: str = "0",
        initial_bot_text: str = "",
        seed_path: str = "data/seed.json",
        store_path: str = "",
        config_path: str = "",
        transcript_path: str = "",
        random_seed: int | None = None,
    ) -> dict:
        """Start one persistent conversation owned by this MCP process."""
        with self.lock:
            if self.runtime is not None:
                raise ValueError("a conversation is already active; call engram_stop before starting another")

            config = load_config(config_path) if config_path else engram_config()
            resolved_store = Path(store_path).resolve() if store_path else None
            if resolved_store and resolved_store.exists():
                engram = persistence.load_engram(resolved_store, config=config)
            else:
                engram = Engram(config=config)

            if seed_path:
                resolved_seed = _resolve_seed_path(seed_path)
                if not resolved_seed.exists():
                    raise ValueError(f"seed file not found: {resolved_seed}")
                seed_data = json.loads(resolved_seed.read_text(encoding="utf-8"))
                engram.sync_corpus(seed_data.get("pairs", []))

            self.store_path = resolved_store
            self.runtime = ConversationRuntime(
                engram,
                user_id=user_id,
                initial_bot_text=initial_bot_text,
                random_seed=random_seed,
                transcript_path=transcript_path or None,
            )
            self._reset_regulated_state()
            snapshot = self.runtime.inspect()
            return {
                "started": True,
                "user_id": snapshot["user_id"],
                "initial_bot_text": snapshot["initial_bot_text"],
                "turn_count": snapshot["turn_count"],
                "statement_count": snapshot["metrics"]["statement_count"],
                "store_path": str(resolved_store) if resolved_store else "",
            }

    def send(self, text: str) -> dict:
        """Submit exactly one conversational message."""
        with self.lock:
            return self._require_runtime().send(text)

    def inspect(self) -> dict:
        """Inspect user context, learned facts, and metrics."""
        with self.lock:
            self._cleanup_transient()
            snapshot = self._require_runtime().inspect()
            snapshot["regulated_cache"] = {
                **deepcopy(self.regulated_metrics),
                "pending_proposals": sum(1 for record in self.proposals.values() if record["resolution"] is None),
                "retained_proposals": len(self.proposals),
            }
            return snapshot

    def add_fact(self, text: str, source_label: str = "") -> dict:
        """Add one unattributed shared fact without changing user context."""
        with self.lock:
            runtime = self._require_runtime()
            statement_id = runtime.engram.add_fact(text, source_label=source_label)
            return statement_view(runtime.engram.get_statement(statement_id))

    def finish(self, output_prefix: str = "engram-mcp-transcript") -> dict:
        """Write JSON and Markdown reports without ending the conversation."""
        with self.lock:
            runtime = self._require_runtime()
            self._save_store(runtime)
            return runtime.write_report(output_prefix)

    def stop(self) -> dict:
        """Persist configured state and release the active conversation."""
        with self.lock:
            runtime = self._require_runtime()
            report = runtime.report()
            self._save_store(runtime)
            self.runtime = None
            self.store_path = None
            self._reset_regulated_state()
            return {
                "stopped": True,
                "user_id": report["user_id"],
                "summary": report["summary"],
            }

    def propose(
        self,
        request: str,
        request_id: str,
        user_id: str = "0",
        namespace: str = "",
        context_fingerprint: str = "",
        limit: int = 1,
        required_metadata: dict | None = None,
        required_source_label: str = "",
    ) -> dict:
        """Create a speculative, uncredited response-cache proposal."""
        with self.lock:
            runtime = self._require_runtime()
            self._cleanup_transient()
            self._require_text(request, "request")
            self._require_text(request_id, "request_id")
            self._require_string(namespace, "namespace")
            self._require_string(context_fingerprint, "context_fingerprint")
            self._require_string(required_source_label, "required_source_label")
            if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 10:
                raise ValueError("limit must be an integer from 1 through 10")
            if required_metadata is None:
                required_metadata = {}
            if not isinstance(required_metadata, dict):
                raise ValueError("required_metadata must be an object or None")
            normalized_user_id = sessions.normalize_user_id(user_id)
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
                    raise ValueError("request_id is already associated with a different proposal request")
                self.regulated_metrics["idempotent_retries"] += 1
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

            query_result = runtime.engram.query(
                request,
                user_id=normalized_user_id,
                limit=limit,
                statement_filter=statement_matches_scope,
            )
            candidates = [self._candidate_result(statement, score) for statement, score in query_result["matches"]]
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
                "resolution": None,
            }
            self.proposal_requests[request_id] = proposal_id
            self.regulated_metrics["proposals"] += 1
            if not candidates:
                self.regulated_metrics["misses"] += 1
            self._enforce_transient_bound()
            return self._proposal_result(self.proposals[proposal_id], idempotent=False)

    def resolve(self, proposal_id: str, outcome: str, statement_id: str = "", reason: str = "") -> dict:
        """Commit one Regulator verdict without double-crediting retries."""
        with self.lock:
            runtime = self._require_runtime()
            self._cleanup_transient()
            self._require_text(proposal_id, "proposal_id")
            self._require_string(outcome, "outcome")
            self._require_string(statement_id, "statement_id")
            self._require_string(reason, "reason")
            if outcome not in REGULATOR_OUTCOMES:
                supported = ", ".join(sorted(REGULATOR_OUTCOMES))
                raise ValueError(f"outcome must be one of: {supported}")

            record = self.proposals.get(proposal_id)
            if record is None:
                raise ValueError("unknown or expired proposal_id")
            resolution_signature = self._signature(outcome=outcome, statement_id=statement_id, reason=reason)
            if record["resolution"] is not None:
                if record["resolution_signature"] != resolution_signature:
                    raise ValueError("proposal has already been resolved with a different verdict")
                self.regulated_metrics["idempotent_retries"] += 1
                result = deepcopy(record["resolution"])
                result["idempotent"] = True
                return result

            candidate_responses = record["candidate_responses"]
            if statement_id and statement_id not in candidate_responses:
                raise ValueError("statement_id is not a candidate in this proposal")
            if outcome == "accepted":
                if not statement_id:
                    raise ValueError("accepted outcomes require statement_id")
                current = runtime.engram.get_statement(statement_id)
                if not current or current["text"] != candidate_responses[statement_id]:
                    raise ValueError("candidate is no longer current; resolve it as rejected_stale")
                proposal = record["proposal"]
                runtime.engram.record_hit(proposal["keywords"], statement_id=statement_id)
                sessions.get_session(runtime.engram, proposal["user_id"], create_if_missing=True)
                sessions.update_session_context(runtime.engram, proposal["user_id"], current["text"])
                self.regulated_metrics["accepted"] += 1
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
        metadata: dict | None = None,
    ) -> dict:
        """Cache an Actor response, replacing only within its exact scope."""
        with self.lock:
            runtime = self._require_runtime()
            self._cleanup_transient()
            self._require_text(request, "request")
            self._require_text(response, "response")
            self._require_text(request_id, "request_id")
            self._require_string(namespace, "namespace")
            self._require_string(context_fingerprint, "context_fingerprint")
            self._require_string(source_label, "source_label")
            if normalize(response) == "idk":
                raise ValueError("IDK is not a cacheable response")
            if metadata is None:
                metadata = {}
            if not isinstance(metadata, dict):
                raise ValueError("metadata must be an object or None")
            normalized_user_id = sessions.normalize_user_id(user_id)
            signature = self._signature(
                request=request,
                response=response,
                user_id=normalized_user_id,
                namespace=namespace,
                context_fingerprint=context_fingerprint,
                source_label=source_label,
                metadata=metadata,
            )
            previous = self.learn_requests.get(request_id)
            if previous is not None:
                if previous["signature"] != signature:
                    raise ValueError("request_id is already associated with a different learned response")
                self.regulated_metrics["idempotent_retries"] += 1
                result = deepcopy(previous["result"])
                result["idempotent"] = True
                return result

            tapestry_metadata = deepcopy(metadata)
            tapestry_metadata.update(
                {
                    "namespace": namespace,
                    "context_fingerprint": context_fingerprint,
                    "request_id": request_id,
                }
            )
            with runtime.engram.statement_lock:
                statement_ids_before = set(runtime.engram.statement_index)
            statement_id = runtime.engram.learn_from_response(
                request,
                response,
                template={"tapestry": tapestry_metadata},
                introduced_by_user_id=None,
                source_label=source_label,
            )
            action = "replaced" if statement_id in statement_ids_before else "created"
            sessions.get_session(runtime.engram, normalized_user_id, create_if_missing=True)
            sessions.update_session_context(runtime.engram, normalized_user_id, response)
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
            return deepcopy(result)

    def retire_response(self, statement_id: str, reason: str, request_id: str) -> dict:
        """Retire one dynamic, patternless response-cache entry."""
        with self.lock:
            runtime = self._require_runtime()
            self._cleanup_transient()
            self._require_text(statement_id, "statement_id")
            self._require_text(reason, "reason")
            self._require_text(request_id, "request_id")
            signature = self._signature(statement_id=statement_id, reason=reason)
            previous = self.retire_requests.get(request_id)
            if previous is not None:
                if previous["signature"] != signature:
                    raise ValueError("request_id is already associated with a different retirement")
                self.regulated_metrics["idempotent_retries"] += 1
                result = deepcopy(previous["result"])
                result["idempotent"] = True
                return result

            statement = runtime.engram.get_statement(statement_id)
            if not statement:
                raise ValueError("unknown statement_id")
            if statement["tier"] != Tier.DYNAMIC or statement["pattern"]:
                raise ValueError("only dynamic, patternless response-cache entries can be retired")
            if not runtime.engram.retire_statement(statement_id):
                raise ValueError("statement could not be retired")
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
            return deepcopy(result)

    def _require_runtime(self) -> ConversationRuntime:
        if self.runtime is None:
            raise ValueError("no active conversation; call engram_start first")
        return self.runtime

    def _save_store(self, runtime: ConversationRuntime) -> None:
        if self.store_path is not None:
            self.store_path.parent.mkdir(parents=True, exist_ok=True)
            persistence.save(runtime.engram, self.store_path)

    @staticmethod
    def _require_text(value: str, name: str) -> None:
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{name} must be a non-empty string")

    @staticmethod
    def _require_string(value: str, name: str) -> None:
        if not isinstance(value, str):
            raise ValueError(f"{name} must be a string")

    @staticmethod
    def _signature(**values) -> str:
        try:
            return json.dumps(values, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
        except (TypeError, ValueError) as error:
            raise ValueError("metadata and request values must be JSON-compatible") from error

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
            "introduced_by_user_id": statement.get("introduced_by_user_id"),
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

    def _cleanup_transient(self) -> None:
        cutoff = time.monotonic() - PROPOSAL_TTL_SECONDS
        expired_proposals = [proposal_id for proposal_id, record in self.proposals.items() if record["created_at"] < cutoff]
        for proposal_id in expired_proposals:
            self._remove_proposal(proposal_id)
        for records in (self.learn_requests, self.retire_requests):
            expired_request_ids = [request_id for request_id, record in records.items() if record["created_at"] < cutoff]
            for request_id in expired_request_ids:
                records.pop(request_id, None)

    def _enforce_transient_bound(self) -> None:
        while len(self.proposals) > MAX_TRANSIENT_RECORDS:
            self._remove_proposal(next(iter(self.proposals)))
        for records in (self.learn_requests, self.retire_requests):
            while len(records) > MAX_TRANSIENT_RECORDS:
                records.pop(next(iter(records)))

    def _remove_proposal(self, proposal_id: str) -> None:
        record = self.proposals.pop(proposal_id, None)
        if record is None:
            return
        request_id = record["proposal"]["request_id"]
        if self.proposal_requests.get(request_id) == proposal_id:
            self.proposal_requests.pop(request_id, None)


def create_mcp_server(service: MCPConversationService | None = None) -> FastMCP:
    """Create the repository-local FastMCP server."""
    conversation_service = service or MCPConversationService()
    server = FastMCP(
        "Engram",
        instructions=(
            "Use engram_start once, then call engram_send exactly once per observed conversational turn. "
            "Use engram_inspect for context and provenance, and engram_finish before engram_stop when a transcript is needed."
        ),
    )

    @server.tool()
    def engram_start(
        user_id: str = "0",
        initial_bot_text: str = "",
        seed_path: str = "data/seed.json",
        store_path: str = "",
        config_path: str = "",
        transcript_path: str = "",
        random_seed: int | None = None,
    ) -> dict:
        """Start one persistent Engram conversation for subsequent tool calls."""
        return conversation_service.start(
            user_id=user_id,
            initial_bot_text=initial_bot_text,
            seed_path=seed_path,
            store_path=store_path,
            config_path=config_path,
            transcript_path=transcript_path,
            random_seed=random_seed,
        )

    @server.tool()
    def engram_send(text: str) -> dict:
        """Send exactly one message after observing Engram's previous reply."""
        return conversation_service.send(text)

    @server.tool()
    def engram_inspect() -> dict:
        """Inspect the active user context, learned facts, and metrics."""
        return conversation_service.inspect()

    @server.tool()
    def engram_add_fact(text: str, source_label: str = "") -> dict:
        """Add one unattributed shared fact without changing conversation context."""
        return conversation_service.add_fact(text, source_label=source_label)

    @server.tool()
    def engram_finish(output_prefix: str = "engram-mcp-transcript") -> dict:
        """Write complete JSON and Markdown transcripts without stopping."""
        return conversation_service.finish(output_prefix)

    @server.tool()
    def engram_stop() -> dict:
        """Persist configured state and release the active conversation."""
        return conversation_service.stop()

    @server.tool()
    def engram_propose(
        request: str,
        request_id: str,
        user_id: str = "0",
        namespace: str = "",
        context_fingerprint: str = "",
        limit: int = 1,
        required_metadata: dict | None = None,
        required_source_label: str = "",
    ) -> dict:
        """Retrieve scoped candidates without recording a successful hit."""
        return conversation_service.propose(
            request=request,
            request_id=request_id,
            user_id=user_id,
            namespace=namespace,
            context_fingerprint=context_fingerprint,
            limit=limit,
            required_metadata=required_metadata,
            required_source_label=required_source_label,
        )

    @server.tool()
    def engram_resolve(proposal_id: str, outcome: str, statement_id: str = "", reason: str = "") -> dict:
        """Commit one accepted or rejected Regulator verdict."""
        return conversation_service.resolve(
            proposal_id=proposal_id,
            outcome=outcome,
            statement_id=statement_id,
            reason=reason,
        )

    @server.tool()
    def engram_learn_response(
        request: str,
        response: str,
        request_id: str,
        user_id: str = "0",
        namespace: str = "",
        context_fingerprint: str = "",
        source_label: str = "tapestry:actor",
        metadata: dict | None = None,
    ) -> dict:
        """Cache one non-IDK Actor response with scope and provenance."""
        return conversation_service.learn_response(
            request=request,
            response=response,
            request_id=request_id,
            user_id=user_id,
            namespace=namespace,
            context_fingerprint=context_fingerprint,
            source_label=source_label,
            metadata=metadata,
        )

    @server.tool()
    def engram_retire_response(statement_id: str, reason: str, request_id: str) -> dict:
        """Retire one globally stale dynamic response-cache entry."""
        return conversation_service.retire_response(
            statement_id=statement_id,
            reason=reason,
            request_id=request_id,
        )

    return server


def main() -> None:
    """Run the MCP adapter over the host-owned stdio transport."""
    create_mcp_server().run(transport="stdio")


if __name__ == "__main__":
    main()
