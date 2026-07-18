"""FastMCP adapter for persistent Engram conversations.

The MCP host owns this process and keeps it alive across tool calls.  All
conversation behavior is delegated to the same programmatic API used by Python
callers and the human CLI.
"""

import json
import threading
from pathlib import Path

from mcp.server.fastmcp import FastMCP

from engram import persistence
from engram.config import engram_config, load_config
from engram.conversation import ConversationRuntime, statement_view
from engram.core import Engram

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SEED_PATH = REPO_ROOT / "data" / "seed.json"


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
            return self._require_runtime().inspect()

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
            return {
                "stopped": True,
                "user_id": report["user_id"],
                "summary": report["summary"],
            }

    def _require_runtime(self) -> ConversationRuntime:
        if self.runtime is None:
            raise ValueError("no active conversation; call engram_start first")
        return self.runtime

    def _save_store(self, runtime: ConversationRuntime) -> None:
        if self.store_path is not None:
            self.store_path.parent.mkdir(parents=True, exist_ok=True)
            persistence.save(runtime.engram, self.store_path)


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

    return server


def main() -> None:
    """Run the MCP adapter over the host-owned stdio transport."""
    create_mcp_server().run(transport="stdio")


if __name__ == "__main__":
    main()
