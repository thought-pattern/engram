"""MCPServer adapter for the transport-neutral Engram core."""

from threading import RLock as threading_RLock

from mcp.server.mcpserver import MCPServer

from engram.config import load_config
from engram.constants import VERSION
from engram.errors import ConflictError, LifecycleError
from engram.service import EngramCore

_DEFAULT_ARGUMENT_DICT = {}


class MCPConversationService:
    """MCP lifecycle state delegating all Engram behavior to ``EngramCore``."""

    def __init__(self) -> None:
        self.core: EngramCore = False
        self.active_user_id: str = ""
        self.lock = threading_RLock()

    @property
    def runtime(self):
        """Expose the active runtime for compatibility with existing callers."""
        if self.core is False or self.active_user_id == "":
            return False
        _return_value = self.core.get_conversation(self.active_user_id)
        return _return_value

    @property
    def store_path(self):
        """Expose the configured store path for compatibility."""
        _return_value = self.core.store_path if self.core is not False else False
        return _return_value

    @property
    def proposals(self) -> dict:
        _return_value = self.core.proposals if self.core is not False else {}
        return _return_value

    @property
    def proposal_requests(self) -> dict:
        _return_value = self.core.proposal_requests if self.core is not False else {}
        return _return_value

    def start(
        self,
        user_id: str = "0",
        initial_bot_text: str = "",
        seed_path: str = "data/seed.json",
        store_path: str = "",
        config_path: str = "",
        transcript_path: str = "",
        random_seed: int = 0,
    ) -> dict:
        """Start one MCP-owned conversation over a new shared core."""
        with self.lock:
            if self.core is not False:
                raise ConflictError("a conversation is already active; call engram_stop before starting another")

            config = load_config(config_path) if config_path else False
            core = EngramCore.open(config=config, store_path=store_path, seed_path=seed_path)
            try:
                started = core.start_conversation(
                    user_id=user_id,
                    initial_bot_text=initial_bot_text,
                    transcript_path=transcript_path,
                    random_seed=random_seed,
                )
            except Exception:
                core.close(flush=False)
                raise
            self.core = core
            self.active_user_id = started.get("user_id", "")
            return started
        return {}

    def send(self, text: str) -> dict:
        """Submit exactly one conversational message."""
        with self.lock:
            core, user_id = self._require_active()
            _return_value = core.chat(user_id, text)
            return _return_value
        return {}

    def inspect(self) -> dict:
        """Inspect user context, learned facts, and metrics."""
        with self.lock:
            core, user_id = self._require_active()
            _return_value = core.inspect_conversation(user_id)
            return _return_value
        return {}

    def add_fact(self, text: str, source_label: str = "") -> dict:
        """Add one unattributed shared fact without changing user context."""
        with self.lock:
            core, _ = self._require_active()
            _return_value = core.add_fact(text, source_label=source_label)
            return _return_value
        return {}

    def finish(self, output_prefix: str = "engram-mcp-transcript") -> dict:
        """Write JSON and Markdown reports without ending the conversation."""
        with self.lock:
            core, user_id = self._require_active()
            _return_value = core.finish_conversation(user_id, output_prefix)
            return _return_value
        return {}

    def stop(self) -> dict:
        """Persist configured state and release the MCP-owned core."""
        with self.lock:
            core, user_id = self._require_active()
            result = core.stop_conversation(user_id)
            core.close(flush=False)
            self.core = False
            self.active_user_id = ""
            return result
        return {}

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
        if required_metadata is _DEFAULT_ARGUMENT_DICT:
            required_metadata = _DEFAULT_ARGUMENT_DICT.copy()
        with self.lock:
            core, _ = self._require_active()
            _return_value = core.propose(
                request=request,
                request_id=request_id,
                user_id=user_id,
                namespace=namespace,
                context_fingerprint=context_fingerprint,
                limit=limit,
                required_metadata=required_metadata,
                required_source_label=required_source_label,
            )
            return _return_value
        return {}

    def resolve(self, proposal_id: str, outcome: str, statement_id: str = "", reason: str = "") -> dict:
        """Commit one Regulator verdict without double-crediting retries."""
        with self.lock:
            core, _ = self._require_active()
            _return_value = core.resolve(proposal_id, outcome, statement_id=statement_id, reason=reason)
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
        if metadata is _DEFAULT_ARGUMENT_DICT:
            metadata = _DEFAULT_ARGUMENT_DICT.copy()
        with self.lock:
            core, _ = self._require_active()
            _return_value = core.learn_response(
                request=request,
                response=response,
                request_id=request_id,
                user_id=user_id,
                namespace=namespace,
                context_fingerprint=context_fingerprint,
                source_label=source_label,
                metadata=metadata,
            )
            return _return_value
        return {}

    def retire_response(self, statement_id: str, reason: str, request_id: str) -> dict:
        """Retire one dynamic, patternless response-cache entry."""
        with self.lock:
            core, _ = self._require_active()
            _return_value = core.retire_response(statement_id, reason, request_id)
            return _return_value
        return {}

    def _require_active(self) -> tuple[EngramCore, str]:
        if self.core is False or self.active_user_id == "":
            raise LifecycleError("no active conversation; call engram_start first")
        return self.core, self.active_user_id


def create_mcp_server(service: MCPConversationService = False) -> MCPServer:
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
    ) -> dict:
        """Start one persistent Engram conversation for subsequent tool calls."""
        _return_value = conversation_service.start(
            user_id=user_id,
            initial_bot_text=initial_bot_text,
            seed_path=seed_path,
            store_path=store_path,
            config_path=config_path,
            transcript_path=transcript_path,
            random_seed=random_seed,
        )
        return _return_value

    @server.tool()
    def engram_send(text: str) -> dict:
        """Send exactly one message after observing Engram's previous reply."""
        _return_value = conversation_service.send(text)
        return _return_value

    @server.tool()
    def engram_inspect() -> dict:
        """Inspect the active user context, learned facts, and metrics."""
        _return_value = conversation_service.inspect()
        return _return_value

    @server.tool()
    def engram_add_fact(text: str, source_label: str = "") -> dict:
        """Add one unattributed shared fact without changing conversation context."""
        _return_value = conversation_service.add_fact(text, source_label=source_label)
        return _return_value

    @server.tool()
    def engram_finish(output_prefix: str = "engram-mcp-transcript") -> dict:
        """Write complete JSON and Markdown transcripts without stopping."""
        _return_value = conversation_service.finish(output_prefix)
        return _return_value

    @server.tool()
    def engram_stop() -> dict:
        """Persist configured state and release the active conversation."""
        _return_value = conversation_service.stop()
        return _return_value

    @server.tool()
    def engram_propose(
        request: str,
        request_id: str,
        user_id: str = "0",
        namespace: str = "",
        context_fingerprint: str = "",
        limit: int = 1,
        required_metadata: dict = _DEFAULT_ARGUMENT_DICT,
        required_source_label: str = "",
    ) -> dict:
        """Retrieve scoped candidates without recording a successful hit."""
        if required_metadata is _DEFAULT_ARGUMENT_DICT:
            required_metadata = _DEFAULT_ARGUMENT_DICT.copy()
        _return_value = conversation_service.propose(
            request=request,
            request_id=request_id,
            user_id=user_id,
            namespace=namespace,
            context_fingerprint=context_fingerprint,
            limit=limit,
            required_metadata=required_metadata,
            required_source_label=required_source_label,
        )
        return _return_value

    @server.tool()
    def engram_resolve(proposal_id: str, outcome: str, statement_id: str = "", reason: str = "") -> dict:
        """Commit one accepted or rejected Regulator verdict."""
        _return_value = conversation_service.resolve(
            proposal_id=proposal_id,
            outcome=outcome,
            statement_id=statement_id,
            reason=reason,
        )
        return _return_value

    @server.tool()
    def engram_learn_response(
        request: str,
        response: str,
        request_id: str,
        user_id: str = "0",
        namespace: str = "",
        context_fingerprint: str = "",
        source_label: str = "tapestry:actor",
        metadata: dict = _DEFAULT_ARGUMENT_DICT,
    ) -> dict:
        """Cache one non-IDK Actor response with scope and provenance."""
        if metadata is _DEFAULT_ARGUMENT_DICT:
            metadata = _DEFAULT_ARGUMENT_DICT.copy()
        _return_value = conversation_service.learn_response(
            request=request,
            response=response,
            request_id=request_id,
            user_id=user_id,
            namespace=namespace,
            context_fingerprint=context_fingerprint,
            source_label=source_label,
            metadata=metadata,
        )
        return _return_value

    @server.tool()
    def engram_retire_response(statement_id: str, reason: str, request_id: str) -> dict:
        """Retire one globally stale dynamic response-cache entry."""
        _return_value = conversation_service.retire_response(
            statement_id=statement_id,
            reason=reason,
            request_id=request_id,
        )
        return _return_value

    return server


def main() -> bool:
    """Run the MCP adapter over the host-owned stdio transport."""
    create_mcp_server().run(transport="stdio")
    return False


if __name__ == "__main__":
    main()
