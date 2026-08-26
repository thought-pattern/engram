"""MCPServer adapter for the transport-neutral Engram core."""

import threading

from mcp.server.mcpserver import MCPServer

from engram.config import load_config
from engram.constants import EMPTY_METADATA, VERSION
from engram.errors import ConflictError, LifecycleError
from engram.service import EngramCore, open_engram_core


class MCPConversationService:
    """MCP lifecycle state delegating all Engram behavior to ``EngramCore``."""

    def __init__(self) -> None:
        self.core = ()
        self.active_user_id = ""
        self.lock = threading.RLock()

    @property
    def runtime(self):
        """Expose the active runtime for compatibility with existing callers."""
        if not self.core or not self.active_user_id:
            result = {}
            return result
        result = self.core.get_conversation(self.active_user_id)
        return result

    @property
    def store_path(self):
        """Expose the configured store path for compatibility."""
        result = self.core.store_path if self.core else ""
        return result

    @property
    def proposals(self) -> dict:
        result = self.core.proposals if self.core else {}
        return result

    @property
    def proposal_requests(self) -> dict:
        result = self.core.proposal_requests if self.core else {}
        return result

    def start(
        self,
        user_id: str = "0",
        initial_bot_text: str = "",
        seed_path: str = "data/seed.json",
        store_path: str = "",
        config_path: str = "",
        transcript_path: str = "",
        random_seed: int = 0,
        random_seed_present: bool = False,
    ) -> dict:
        """Start one MCP-owned conversation over a new shared core."""
        with self.lock:
            if self.core:
                raise ConflictError("a conversation is already active; call engram_stop before starting another")

            config = load_config(config_path) if config_path else {}
            core = open_engram_core(config=config, store_path=store_path, seed_path=seed_path)
            try:
                started = core.start_conversation(
                    user_id=user_id,
                    initial_bot_text=initial_bot_text,
                    transcript_path=transcript_path,
                    random_seed=random_seed,
                    random_seed_present=random_seed_present,
                )
            except Exception:
                core.close(flush=False)
                raise
            self.core = core
            self.active_user_id = started["user_id"]
            return started

    def send(self, text: str) -> dict:
        """Submit exactly one conversational message."""
        with self.lock:
            core, user_id = self._require_active()
            result = core.chat(user_id, text)
            return result

    def inspect(self) -> dict:
        """Inspect user context, learned facts, and metrics."""
        with self.lock:
            core, user_id = self._require_active()
            result = core.inspect_conversation(user_id)
            return result

    def add_fact(self, text: str, source_label: str = "") -> dict:
        """Add one unattributed shared fact without changing user context."""
        with self.lock:
            core, _ = self._require_active()
            result = core.add_fact(text, source_label=source_label)
            return result

    def finish(self, output_prefix: str = "engram-mcp-transcript") -> dict:
        """Write JSON and Markdown reports without ending the conversation."""
        with self.lock:
            core, user_id = self._require_active()
            result = core.finish_conversation(user_id, output_prefix)
            return result

    def stop(self) -> dict:
        """Persist configured state and release the MCP-owned core."""
        with self.lock:
            core, user_id = self._require_active()
            result = core.stop_conversation(user_id)
            core.close(flush=False)
            self.core = ()
            self.active_user_id = ""
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
            core, _ = self._require_active()
            result = core.propose(
                request=request,
                request_id=request_id,
                user_id=user_id,
                namespace=namespace,
                context_fingerprint=context_fingerprint,
                limit=limit,
                required_metadata=required_metadata,
                required_source_label=required_source_label,
            )
            return result

    def resolve(self, proposal_id: str, outcome: str, statement_id: str = "", reason: str = "") -> dict:
        """Commit one Regulator verdict without double-crediting retries."""
        with self.lock:
            core, _ = self._require_active()
            result = core.resolve(proposal_id, outcome, statement_id=statement_id, reason=reason)
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
        """Cache an Actor response without implicitly replacing existing knowledge."""
        with self.lock:
            core, _ = self._require_active()
            result = core.learn_response(
                request=request,
                response=response,
                request_id=request_id,
                user_id=user_id,
                namespace=namespace,
                context_fingerprint=context_fingerprint,
                source_label=source_label,
                metadata=metadata,
            )
            return result

    def retire_response(self, statement_id: str, reason: str, request_id: str) -> dict:
        """Retire one dynamic, patternless response-cache entry."""
        with self.lock:
            core, _ = self._require_active()
            result = core.retire_response(statement_id, reason, request_id)
            return result

    def _require_active(self) -> tuple[EngramCore, str]:
        if not self.core or not self.active_user_id:
            raise LifecycleError("no active conversation; call engram_start first")
        result = self.core, self.active_user_id
        return result


def create_mcp_server(service=()) -> MCPServer:
    """Create the repository-local MCP server."""
    conversation_service = service or MCPConversationService()
    server = MCPServer(
        "Engram",
        version=VERSION,
        instructions=(
            "Use engram_start once, then use engram_send for chatbot turns or the propose/resolve tools for regulated cache work. "
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
        random_seed: int = 0,
        random_seed_present: bool = False,
    ) -> dict:
        """Start one persistent Engram conversation for subsequent tool calls."""
        result = conversation_service.start(
            user_id=user_id,
            initial_bot_text=initial_bot_text,
            seed_path=seed_path,
            store_path=store_path,
            config_path=config_path,
            transcript_path=transcript_path,
            random_seed=random_seed,
            random_seed_present=random_seed_present,
        )
        return result

    @server.tool()
    def engram_send(text: str) -> dict:
        """Send exactly one message after observing Engram's previous reply."""
        result = conversation_service.send(text)
        return result

    @server.tool()
    def engram_inspect() -> dict:
        """Inspect the active user context, learned facts, and metrics."""
        result = conversation_service.inspect()
        return result

    @server.tool()
    def engram_add_fact(text: str, source_label: str = "") -> dict:
        """Add one unattributed shared fact without changing conversation context."""
        result = conversation_service.add_fact(text, source_label=source_label)
        return result

    @server.tool()
    def engram_finish(output_prefix: str = "engram-mcp-transcript") -> dict:
        """Write complete JSON and Markdown transcripts without stopping."""
        result = conversation_service.finish(output_prefix)
        return result

    @server.tool()
    def engram_stop() -> dict:
        """Persist configured state and release the active conversation."""
        result = conversation_service.stop()
        return result

    @server.tool()
    def engram_propose(
        request: str,
        request_id: str,
        user_id: str = "0",
        namespace: str = "",
        context_fingerprint: str = "",
        limit: int = 1,
        required_metadata: dict = EMPTY_METADATA,
        required_source_label: str = "",
    ) -> dict:
        """Retrieve scoped candidates without recording a successful hit."""
        result = conversation_service.propose(
            request=request,
            request_id=request_id,
            user_id=user_id,
            namespace=namespace,
            context_fingerprint=context_fingerprint,
            limit=limit,
            required_metadata=required_metadata,
            required_source_label=required_source_label,
        )
        return result

    @server.tool()
    def engram_resolve(proposal_id: str, outcome: str, statement_id: str = "", reason: str = "") -> dict:
        """Commit one accepted or rejected Regulator verdict."""
        result = conversation_service.resolve(
            proposal_id=proposal_id,
            outcome=outcome,
            statement_id=statement_id,
            reason=reason,
        )
        return result

    @server.tool()
    def engram_learn_response(
        request: str,
        response: str,
        request_id: str,
        user_id: str = "0",
        namespace: str = "",
        context_fingerprint: str = "",
        source_label: str = "tapestry:actor",
        metadata: dict = EMPTY_METADATA,
    ) -> dict:
        """Cache one non-IDK Actor response with scope and provenance."""
        result = conversation_service.learn_response(
            request=request,
            response=response,
            request_id=request_id,
            user_id=user_id,
            namespace=namespace,
            context_fingerprint=context_fingerprint,
            source_label=source_label,
            metadata=metadata,
        )
        return result

    @server.tool()
    def engram_retire_response(statement_id: str, reason: str, request_id: str) -> dict:
        """Retire one globally stale dynamic response-cache entry."""
        result = conversation_service.retire_response(
            statement_id=statement_id,
            reason=reason,
            request_id=request_id,
        )
        return result

    return server


def main() -> None:
    """Run the MCP adapter over the host-owned stdio transport."""
    create_mcp_server().run(transport="stdio")


if __name__ == "__main__":
    main()
