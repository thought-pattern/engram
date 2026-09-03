"""Process-memory command-line interface for Engram."""

from argparse import ArgumentParser as argparse_ArgumentParser
from json import dumps as json_dumps
from pathlib import Path
from sys import argv as sys_argv, path as sys_path, stderr as sys_stderr

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys_path:
    sys_path.insert(0, str(REPOSITORY_ROOT))

from engram.config import load_config
from engram.errors import EngramCoreError
from engram.service import EngramCore


class InteractiveChat:
    """One process-local interactive conversation."""

    def __init__(
        self,
        core: EngramCore,
        session_id: str = "",
        initial_bot_text: str = "",
    ) -> None:
        if not isinstance(core, EngramCore):
            raise TypeError("core must be an EngramCore")
        self.core = core
        self.session_id = session_id or "cli"
        self.debug_mode = False
        self.core.start_conversation(
            user_id=self.session_id,
            initial_bot_text=initial_bot_text,
        )

    def process_input(self, user_input: str) -> str:
        """Process one user turn and return Engram's response."""
        result = self.core.chat(self.session_id, user_input)
        if self.debug_mode:
            detail = f"Source: {result.get('source', '')} | Score: {result.get('score', 0.0):.2f}"
            if result.get("pattern", ""):
                detail += f" | Pattern: '{result.get('pattern', '')}' | Captured: {result.get('captured', {})}"
            print(f"     [{detail}]")
        result = result.get("response", "") or "Tell me more about that."
        return result

    def run(self) -> None:
        """Run until the caller exits the process-local conversation."""
        print("ENGRAM Chat (process memory; restart clears the cache)")
        print("Commands: /debug, /inspect, /metrics, /finish, /topic <name>, /set <name> <value>, /get <name>, /quit")
        print()
        while True:
            try:
                line = input("You: ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                break
            if not line:
                continue
            if line.startswith("/"):
                if self.handle_command(line):
                    break
                continue
            print(f"Bot: {self.process_input(line)}")

    def handle_command(self, line: str) -> bool:
        """Handle one slash command; return whether the loop should stop."""
        parts = line[1:].split(maxsplit=2)
        command = parts[0].lower()
        if command in ("quit", "exit", "q"):
            return True
        if command == "debug":
            self.debug_mode = not self.debug_mode
            print(f"Debug mode: {'on' if self.debug_mode else 'off'}")
        elif command == "metrics":
            metrics = self.core.inspect_conversation(self.session_id).get("metrics", {})
            print(json_dumps(metrics, indent=2))
        elif command == "inspect":
            print(json_dumps(self.core.inspect_conversation(self.session_id), indent=2))
        elif command == "finish":
            print(json_dumps(self.core.finish_conversation(self.session_id), indent=2))
        elif command == "topic" and len(parts) >= 2:
            self.core.set_predicate(self.session_id, "topic", parts[1])
            print(f"Topic set to: {parts[1]}")
        elif command == "set" and len(parts) >= 3:
            self.core.set_predicate(self.session_id, parts[1], parts[2])
            print(f"Set {parts[1]} = {parts[2]}")
        elif command == "get" and len(parts) >= 2:
            print(f"{parts[1]} = {self.core.get_predicate(self.session_id, parts[1], '(not set)')}")
        elif command == "help":
            print("Commands: /debug, /inspect, /metrics, /finish, /topic <name>, /set <name> <value>, /get <name>, /quit")
        else:
            print(f"Unknown command: {command} (try /help)")
        return False


def main(argv=()) -> int:
    """Run one query or one interactive process-memory session."""
    parser = argparse_ArgumentParser(
        prog="engram",
        description="Process-memory fast-recall cache with read-only graph retrieval",
    )
    parser.add_argument("--config", "-c", default="config.yml", help="YAML configuration path")
    parser.add_argument("--capacity", type=int, default=0, help="override maximum dynamic entries")
    subparsers = parser.add_subparsers(dest="command")
    query_parser = subparsers.add_parser("query", help="run unified process-local and graph resolution once")
    query_parser.add_argument("text")
    query_parser.add_argument("--request-id", default="cli-query")
    query_parser.add_argument("--user-id", default="0")
    query_parser.add_argument("--namespace", default="")
    query_parser.add_argument("--context-fingerprint", default="")
    query_parser.add_argument("--accept-exact", action="store_true")
    interactive_parser = subparsers.add_parser("interactive", help="start an interactive process-local cache")
    interactive_parser.add_argument("--session", default="")
    interactive_parser.add_argument("--initial-bot-text", default="")
    args = parser.parse_args(argv)

    config = load_config(args.config)
    if args.capacity:
        config["capacity"] = args.capacity
    command = args.command or "interactive"
    try:
        core = EngramCore(config=config)
        if command == "query":
            result = core.resolve_request(
                args.text,
                args.request_id,
                user_id=args.user_id,
                namespace=args.namespace,
                context_fingerprint=args.context_fingerprint,
                accept_exact=args.accept_exact,
            )
            print(json_dumps(result, indent=2, default=str))
        else:
            chat = InteractiveChat(
                core,
                session_id=getattr(args, "session", ""),
                initial_bot_text=getattr(args, "initial_bot_text", ""),
            )
            chat.run()
            core.stop_conversation(chat.session_id)
        core.close()
    except EngramCoreError as error:
        print(f"Error: {error}", file=sys_stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(tuple(sys_argv[1:])))
