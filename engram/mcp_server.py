"""MCPServer adapter for the transport-neutral Engram core."""

from threading import RLock as threading_RLock

from mcp.server.mcpserver import MCPServer

from engram.config import load_config
from engram.constants import EMPTY_METADATA, VERSION
from engram.errors import ConflictError, LifecycleError
from engram.service import EngramCore, normalize_service_user_id


class MCPConversationService:
    """MCP lifecycle state delegating all Engram behavior to ``EngramCore``."""

    def __init__(self) -> None:
        self.core = ()
        self.active_user_id = ""
        self.lock = threading_RLock()

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
        config_path: str = "",
        random_seed: int = 0,
        random_seed_present: bool = False,
    ) -> dict:
        """Start one MCP-owned conversation over a new shared core."""
        with self.lock:
            if isinstance(self.core, EngramCore):
                raise ConflictError("a conversation is already active; call engram_stop before starting another")

            config = load_config(config_path) if config_path else {}
            conversation_user_id = normalize_service_user_id(user_id)
            core = EngramCore(config=config)
            try:
                started = core.start_conversation(
                    user_id=conversation_user_id,
                    initial_bot_text=initial_bot_text,
                    random_seed=random_seed,
                    random_seed_present=random_seed_present,
                )
            except Exception:
                core.close()
                raise
            self.core = core
            self.active_user_id = conversation_user_id
            return started

    def send(self, text: str) -> dict:
        """Submit exactly one conversational message."""
        with self.lock:
            core, user_id = self.require_active()
            result = core.chat(user_id, text)
            return result

    def inspect(self) -> dict:
        """Inspect user context, learned facts, and metrics."""
        with self.lock:
            core, user_id = self.require_active()
            result = core.inspect_conversation(user_id)
            return result

    def add_fact(self, text: str, source_label: str = "") -> dict:
        """Add one unattributed shared fact without changing user context."""
        with self.lock:
            core, _ = self.require_active()
            result = core.add_fact(text, source_label=source_label)
            return result

    def finish(self) -> dict:
        """Return the current report without ending the conversation."""
        with self.lock:
            core, user_id = self.require_active()
            result = core.finish_conversation(user_id)
            return result

    def stop(self) -> dict:
        """Discard process memory and release the MCP-owned core."""
        with self.lock:
            core, user_id = self.require_active()
            result = core.stop_conversation(user_id)
            core.close()
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
            core, _ = self.require_active()
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
            core, _ = self.require_active()
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
            core, _ = self.require_active()
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
            core, _ = self.require_active()
            result = core.retire_response(statement_id, reason, request_id)
            return result

    def require_active(self) -> tuple[EngramCore, str]:
        if not isinstance(self.core, EngramCore) or self.active_user_id == "":
            raise LifecycleError("no active conversation; call engram_start first")
        result = self.core, self.active_user_id
        return result


class EngramMCPServer(MCPServer):
    """Repository-local MCP server with one injected conversation service."""

    def __init__(self, service=()):
        super().__init__(
            "Engram",
            version=VERSION,
            instructions=(
                "Use engram_start once, then use engram_send for chatbot turns or the propose/resolve tools "
                "for regulated cache work. Use engram_inspect for context and provenance, and engram_finish "
                "for an in-memory report."
            ),
        )
        self.conversation_service = service or MCPConversationService()
        self.tool()(self.engram_start)
        self.tool()(self.engram_send)
        self.tool()(self.engram_inspect)
        self.tool()(self.engram_add_fact)
        self.tool()(self.engram_finish)
        self.tool()(self.engram_stop)
        self.tool()(self.engram_propose)
        self.tool()(self.engram_resolve)
        self.tool()(self.engram_learn_response)
        self.tool()(self.engram_retire_response)

    def engram_start(
        self,
        user_id: str = "0",
        initial_bot_text: str = "",
        config_path: str = "",
        random_seed: int = 0,
        random_seed_present: bool = False,
    ) -> dict:
        """Start one in-memory Engram conversation for subsequent tool calls."""
        result = self.conversation_service.start(
            user_id=user_id,
            initial_bot_text=initial_bot_text,
            config_path=config_path,
            random_seed=random_seed,
            random_seed_present=random_seed_present,
        )
        return result

    def engram_send(self, text: str) -> dict:
        """Send exactly one message after observing Engram's previous reply."""
        result = self.conversation_service.send(text)
        return result

    def engram_inspect(self) -> dict:
        """Inspect the active user context, learned facts, and metrics."""
        result = self.conversation_service.inspect()
        return result

    def engram_add_fact(self, text: str, source_label: str = "") -> dict:
        """Add one unattributed shared fact without changing conversation context."""
        result = self.conversation_service.add_fact(text, source_label=source_label)
        return result

    def engram_finish(self) -> dict:
        """Return the complete process-local report without stopping."""
        result = self.conversation_service.finish()
        return result

    def engram_stop(self) -> dict:
        """Discard process memory and release the active conversation."""
        result = self.conversation_service.stop()
        return result

    def engram_propose(
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
        """Retrieve scoped candidates without recording a successful hit."""
        result = self.conversation_service.propose(
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

    def engram_resolve(self, proposal_id: str, outcome: str, statement_id: str = "", reason: str = "") -> dict:
        """Commit one accepted or rejected Regulator verdict."""
        result = self.conversation_service.resolve(
            proposal_id=proposal_id,
            outcome=outcome,
            statement_id=statement_id,
            reason=reason,
        )
        return result

    def engram_learn_response(
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
        """Cache one non-IDK Actor response with scope and provenance."""
        result = self.conversation_service.learn_response(
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

    def engram_retire_response(self, statement_id: str, reason: str, request_id: str) -> dict:
        """Retire one globally stale dynamic response-cache entry."""
        result = self.conversation_service.retire_response(
            statement_id=statement_id,
            reason=reason,
            request_id=request_id,
        )
        return result


def main() -> None:
    """Run the MCP adapter over the host-owned stdio transport."""
    EngramMCPServer().run(transport="stdio")


if __name__ == "__main__":
    main()
