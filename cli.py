"""Command line interface for ENGRAM."""

import argparse
import json
import sys
from pathlib import Path

from engram.config import EngramConfig, EvictionPolicy
from engram.core import Engram, SessionLimitExceeded, SessionNotFound
from engram.models import Tier


def create_parser() -> argparse.ArgumentParser:
    """Create the argument parser."""
    parser = argparse.ArgumentParser(
        prog="engram",
        description="Keyword-indexed statement store with hit-rate tracking",
    )
    parser.add_argument(
        "--store",
        "-s",
        type=str,
        default="engram.json",
        help="Path to engram store file (default: engram.json)",
    )
    parser.add_argument(
        "--capacity",
        type=int,
        default=10000,
        help="Maximum DYNAMIC statements (default: 10000)",
    )
    parser.add_argument(
        "--eviction",
        type=str,
        choices=["fifo", "lru", "lfu", "hit_rate"],
        default="fifo",
        help="Eviction policy (default: fifo)",
    )

    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    # init command
    init_parser = subparsers.add_parser("init", help="Initialize a new engram store")
    init_parser.add_argument(
        "--force",
        "-f",
        action="store_true",
        help="Overwrite existing store",
    )

    # store command
    store_parser = subparsers.add_parser("store", help="Store a statement")
    store_parser.add_argument("text", help="Statement text or template (JSON)")
    store_parser.add_argument(
        "--pattern",
        "-p",
        type=str,
        help="Pattern to match (default: extracted from text)",
    )
    store_parser.add_argument(
        "--static",
        action="store_true",
        help="Store as STATIC (protected from eviction)",
    )

    # load command
    load_parser = subparsers.add_parser("load", help="Load statements from a file")
    load_parser.add_argument("file", help="JSON file with patterns/templates")
    load_parser.add_argument(
        "--static",
        action="store_true",
        help="Load as STATIC statements",
    )

    # query command
    query_parser = subparsers.add_parser("query", help="Query for matching statements")
    query_parser.add_argument("text", help="Query text")
    query_parser.add_argument(
        "--limit",
        "-n",
        type=int,
        default=5,
        help="Maximum results (default: 5)",
    )
    query_parser.add_argument(
        "--session",
        type=str,
        help="Session ID for context expansion",
    )
    query_parser.add_argument(
        "--hit",
        action="store_true",
        help="Record as a hit (successful retrieval)",
    )

    # session commands
    session_parser = subparsers.add_parser("session", help="Session management")
    session_sub = session_parser.add_subparsers(dest="session_command")

    session_create = session_sub.add_parser("create", help="Create a new session")
    session_create.add_argument("--id", type=str, help="Session ID (generated if omitted)")

    session_sub.add_parser("list", help="List sessions")

    session_get = session_sub.add_parser("get", help="Get session details")
    session_get.add_argument("id", help="Session ID")

    session_update = session_sub.add_parser("update", help="Update session context")
    session_update.add_argument("id", help="Session ID")
    session_update.add_argument("response", help="Previous response text")

    session_delete = session_sub.add_parser("delete", help="Delete a session")
    session_delete.add_argument("id", help="Session ID")

    session_expire = session_sub.add_parser("expire", help="Expire inactive sessions")
    session_expire.add_argument(
        "--hours",
        type=float,
        default=24,
        help="Inactivity threshold in hours (default: 24)",
    )

    session_set = session_sub.add_parser("set", help="Set session predicate")
    session_set.add_argument("id", help="Session ID")
    session_set.add_argument("name", help="Predicate name")
    session_set.add_argument("value", help="Predicate value")

    session_topic = session_sub.add_parser("topic", help="Set session topic")
    session_topic.add_argument("id", help="Session ID")
    session_topic.add_argument("topic", help="Topic name")

    # metrics command
    subparsers.add_parser("metrics", help="Show store metrics")

    # keywords command
    keywords_parser = subparsers.add_parser("keywords", help="Keyword analysis")
    keywords_parser.add_argument(
        "--low-hit",
        action="store_true",
        help="Show low hit-rate keywords",
    )
    keywords_parser.add_argument(
        "--zero-hit",
        action="store_true",
        help="Show zero-hit keywords",
    )
    keywords_parser.add_argument(
        "--min-queries",
        type=int,
        default=10,
        help="Minimum query count threshold (default: 10)",
    )

    # coverage command (new)
    coverage_parser = subparsers.add_parser("coverage", help="Coverage analysis")
    coverage_parser.add_argument(
        "--gaps",
        action="store_true",
        help="Show coverage gaps (high queries, low hits)",
    )
    coverage_parser.add_argument(
        "--report",
        action="store_true",
        help="Show full coverage report with recommendations",
    )
    coverage_parser.add_argument(
        "--min-queries",
        type=int,
        default=10,
        help="Minimum query count threshold (default: 10)",
    )

    # export command
    export_parser = subparsers.add_parser("export", help="Export statements")
    export_parser.add_argument(
        "--output",
        "-o",
        type=str,
        help="Output file (default: stdout)",
    )
    export_parser.add_argument(
        "--static-only",
        action="store_true",
        help="Export only STATIC statements",
    )
    export_parser.add_argument(
        "--dynamic-only",
        action="store_true",
        help="Export only DYNAMIC statements",
    )
    export_parser.add_argument(
        "--json",
        action="store_true",
        help="Export as JSON with patterns/templates",
    )

    # interactive command
    interactive_parser = subparsers.add_parser("interactive", help="Start interactive chat")
    interactive_parser.add_argument(
        "--session",
        type=str,
        help="Session ID to use (created if not exists)",
    )
    interactive_parser.add_argument(
        "--graph",
        action="store_true",
        help="Enable mock graph client for testing",
    )

    return parser


def get_eviction_policy(name: str) -> EvictionPolicy:
    """Convert string to EvictionPolicy enum."""
    return {
        "fifo": EvictionPolicy.FIFO,
        "lru": EvictionPolicy.LRU,
        "lfu": EvictionPolicy.LFU,
        "hit_rate": EvictionPolicy.HIT_RATE,
    }[name]


def load_engram(store_path: str, capacity: int, eviction: str = "fifo") -> Engram:
    """Load engram from file or create new."""
    path = Path(store_path)
    if path.exists():
        return Engram.load(path)
    return Engram(config=EngramConfig(
        capacity=capacity,
        eviction_policy=get_eviction_policy(eviction),
    ))


def save_engram(engram: Engram, store_path: str) -> None:
    """Save engram to file."""
    engram.save(store_path)


def cmd_init(args: argparse.Namespace) -> int:
    """Initialize a new engram store."""
    path = Path(args.store)
    if path.exists() and not args.force:
        print(f"Store already exists: {args.store}", file=sys.stderr)
        print("Use --force to overwrite", file=sys.stderr)
        return 1

    engram = Engram(config=EngramConfig(
        capacity=args.capacity,
        eviction_policy=get_eviction_policy(args.eviction),
    ))
    save_engram(engram, args.store)
    print(f"Initialized engram store: {args.store}")
    return 0


def cmd_store(args: argparse.Namespace) -> int:
    """Store a statement."""
    engram = load_engram(args.store, args.capacity, args.eviction)
    tier = Tier.STATIC if args.static else Tier.DYNAMIC

    # Check if text is JSON template
    template = None
    text = args.text
    if args.text.startswith("{"):
        try:
            template = json.loads(args.text)
            text = template.get("text", args.text)
        except json.JSONDecodeError:
            pass

    stmt_id = engram.store(text, tier=tier, pattern=args.pattern, template=template)
    save_engram(engram, args.store)
    print(f"Stored: {stmt_id} ({tier.value})")
    return 0


def cmd_load(args: argparse.Namespace) -> int:
    """Load statements from JSON file."""
    engram = load_engram(args.store, args.capacity, args.eviction)
    tier = Tier.STATIC if args.static else Tier.DYNAMIC

    path = Path(args.file)
    if not path.exists():
        print(f"File not found: {args.file}", file=sys.stderr)
        return 1

    with open(path, encoding="utf-8") as f:
        data = json.load(f)

    count = 0
    pairs = data.get("pairs", data.get("categories", []))
    for item in pairs:
        if isinstance(item, dict):
            pattern = item.get("pattern", "")
            response = item.get("response", item.get("text", ""))
            template = item.get("template")
            that = item.get("that", "")
            topic = item.get("topic", "")

            engram.store(
                response,
                tier=tier,
                pattern=pattern,
                template=template,
                that=that,
                topic=topic,
            )
            count += 1
        elif isinstance(item, str):
            engram.store(item, tier=tier)
            count += 1

    save_engram(engram, args.store)
    print(f"Loaded {count} statements ({tier.value})")
    return 0


def cmd_query(args: argparse.Namespace) -> int:
    """Query for statements."""
    engram = load_engram(args.store, args.capacity, args.eviction)

    result = engram.query(args.text, session_id=args.session, limit=args.limit)

    if args.hit and result.matches:
        engram.record_hit(result.keywords)
        save_engram(engram, args.store)

    print(f"Keywords: {', '.join(result.keywords)}")
    print(f"Matches: {len(result.matches)}")
    print()

    for i, (stmt, score) in enumerate(result.matches, 1):
        print(f"{i}. [{score:.3f}] ({stmt.tier.value}) {stmt.text}")

    if not result.matches:
        print("No matches found.")

    return 0


def cmd_session(args: argparse.Namespace) -> int:
    """Session management commands."""
    engram = load_engram(args.store, args.capacity, args.eviction)
    modified = False

    if args.session_command == "create":
        try:
            session_id = engram.create_session(session_id=args.id)
            print(f"Created session: {session_id}")
            modified = True
        except SessionLimitExceeded as e:
            print(f"Error: {e}", file=sys.stderr)
            return 1

    elif args.session_command == "list":
        sessions = engram.list_sessions()
        if sessions:
            print(f"Sessions ({len(sessions)}):")
            for s in sessions:
                topic = s.topic or "(none)"
                print(f"  {s.session_id}: topic={topic}, last={s.last_active.isoformat()}")
        else:
            print("No sessions.")

    elif args.session_command == "get":
        session = engram.get_session(args.id, create_if_missing=False)
        if session:
            print(f"Session: {session.session_id}")
            print(f"Created: {session.created_at.isoformat()}")
            print(f"Last active: {session.last_active.isoformat()}")
            print(f"Topic: {session.topic or '(none)'}")
            print(f"That: {session.previous_response or '(empty)'}")
            if session.predicates:
                print(f"Predicates: {json.dumps(session.predicates)}")
            if session.input_history:
                print(f"Input history: {session.input_history[:5]}")
        else:
            print(f"Session not found: {args.id}", file=sys.stderr)
            return 1

    elif args.session_command == "update":
        try:
            engram.update_session_context(args.id, args.response)
            print(f"Updated session: {args.id}")
            modified = True
        except SessionNotFound:
            print(f"Session not found: {args.id}", file=sys.stderr)
            return 1

    elif args.session_command == "delete":
        if engram.delete_session(args.id):
            print(f"Deleted session: {args.id}")
            modified = True
        else:
            print(f"Session not found: {args.id}", file=sys.stderr)
            return 1

    elif args.session_command == "expire":
        from datetime import timedelta
        threshold = timedelta(hours=args.hours)
        count = engram.expire_sessions(inactive_threshold=threshold)
        print(f"Expired {count} sessions")
        modified = count > 0

    elif args.session_command == "set":
        session = engram.get_session(args.id, create_if_missing=False)
        if session:
            session.set_predicate(args.name, args.value)
            print(f"Set {args.name}={args.value} for session {args.id}")
            modified = True
        else:
            print(f"Session not found: {args.id}", file=sys.stderr)
            return 1

    elif args.session_command == "topic":
        session = engram.get_session(args.id, create_if_missing=False)
        if session:
            session.set_predicate("topic", args.topic)
            print(f"Set topic={args.topic} for session {args.id}")
            modified = True
        else:
            print(f"Session not found: {args.id}", file=sys.stderr)
            return 1

    else:
        print("Usage: engram session {create|list|get|update|delete|expire|set|topic}")
        return 1

    if modified:
        save_engram(engram, args.store)

    return 0


def cmd_metrics(args: argparse.Namespace) -> int:
    """Show store metrics."""
    engram = load_engram(args.store, args.capacity, args.eviction)
    metrics = engram.get_metrics()

    print("ENGRAM Metrics")
    print("-" * 40)
    print(f"Statements:     {metrics['statement_count']:,}")
    print(f"  STATIC:       {metrics['static_count']:,}")
    print(f"  DYNAMIC:      {metrics['dynamic_count']:,}")
    print(f"Keywords:       {metrics['keyword_count']:,}")
    print(f"Sessions:       {metrics['session_count']:,}")
    print(f"Total queries:  {metrics['query_count']:,}")
    print(f"Total hits:     {metrics['hit_count']:,}")
    print(f"Hit rate:       {metrics['hit_rate']:.1%}")
    print(f"Evictions:      {metrics['eviction_count']:,}")
    print(f"Eviction policy: {engram.config.eviction_policy.value}")

    return 0


def cmd_keywords(args: argparse.Namespace) -> int:
    """Keyword analysis."""
    engram = load_engram(args.store, args.capacity, args.eviction)

    if args.zero_hit:
        results = engram.get_zero_hit_keywords(min_queries=args.min_queries)
        if results:
            print(f"Zero-hit keywords (min queries: {args.min_queries}):")
            for kw, query_count in results[:20]:
                print(f"  {kw}: {query_count} queries, 0 hits")
        else:
            print("No zero-hit keywords found.")

    elif args.low_hit:
        results = engram.get_low_hit_keywords(min_queries=args.min_queries, max_hit_rate=0.2)
        if results:
            print(f"Low hit-rate keywords (min queries: {args.min_queries}, max rate: 20%):")
            for kw, query_count, hit_rate in results[:20]:
                print(f"  {kw}: {query_count} queries, {hit_rate:.1%} hit rate")
        else:
            print("No low hit-rate keywords found.")

    else:
        print("Usage: engram keywords {--low-hit|--zero-hit}")
        return 1

    return 0


def cmd_coverage(args: argparse.Namespace) -> int:
    """Coverage analysis."""
    engram = load_engram(args.store, args.capacity, args.eviction)

    if args.report:
        report = engram.get_coverage_report()
        print("Coverage Report")
        print("-" * 40)
        print(f"Total keywords:      {report['total_keywords']:,}")
        print(f"Keywords with hits:  {report['keywords_with_hits']:,}")
        print(f"Keywords zero hits:  {report['keywords_zero_hits']:,}")
        print(f"Overall hit rate:    {report['overall_hit_rate']:.1%}")
        print()

        if report['coverage_gaps']:
            print("Coverage Gaps (high queries, low hits):")
            for gap in report['coverage_gaps'][:10]:
                print(f"  {gap['keyword']}: {gap['queries']} queries, {gap['hit_rate']:.1%} hit rate")
            print()

        if report['top_performing']:
            print("Top Performing Keywords:")
            for kw in report['top_performing'][:5]:
                print(f"  {kw['keyword']}: {kw['queries']} queries, {kw['hit_rate']:.1%} hit rate")
            print()

        if report['recommendations']:
            print("Recommendations:")
            for rec in report['recommendations']:
                print(f"  - {rec}")

    elif args.gaps:
        gaps = engram.get_coverage_gaps(min_queries=args.min_queries, max_hit_rate=0.2)
        if gaps:
            print(f"Coverage gaps (min queries: {args.min_queries}, max hit rate: 20%):")
            for gap in gaps[:20]:
                print(f"  {gap['keyword']}: {gap['queries']} queries, {gap['hits']} hits, {gap['hit_rate']:.1%}")
        else:
            print("No coverage gaps found.")

    else:
        print("Usage: engram coverage {--gaps|--report}")
        return 1

    return 0


def cmd_export(args: argparse.Namespace) -> int:
    """Export statements."""
    engram = load_engram(args.store, args.capacity, args.eviction)

    statements = []
    for stmt in engram._statements:
        if args.static_only and stmt.tier != Tier.STATIC:
            continue
        if args.dynamic_only and stmt.tier != Tier.DYNAMIC:
            continue
        statements.append(stmt)

    if args.json:
        pairs = []
        for stmt in statements:
            item = {"pattern": stmt.pattern, "response": stmt.text}
            if stmt.template:
                item["template"] = stmt.template
            if stmt.that:
                item["that"] = stmt.that
            if stmt.topic:
                item["topic"] = stmt.topic
            pairs.append(item)
        output = json.dumps({"pairs": pairs}, indent=2)
    else:
        output = "\n".join(stmt.text for stmt in statements)

    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(output)
        print(f"Exported {len(statements)} statements to {args.output}")
    else:
        print(output)

    return 0


class InteractiveChat:
    """Interactive chat session."""

    def __init__(
        self,
        engram: Engram,
        session_id: str | None = None,
        enable_graph: bool = False,
    ):
        self.engram = engram
        self.debug_mode = False
        self.session_id = session_id or engram.create_session()

        # Ensure session exists
        self.session = engram.get_session(self.session_id, create_if_missing=True)

        # Note: Graph support would require extending core.py to accept graph callbacks
        if enable_graph:
            print("Note: Graph client support is a future feature")

    def process_input(self, user_input: str) -> str:
        """Process user input and return response."""
        result = self.engram.pattern_query(user_input, session_id=self.session_id)

        if not result:
            return "Tell me more about that."

        stmt, captured, response = result

        if self.debug_mode:
            print(f"     [Pattern: '{stmt.pattern}' | Captured: {captured}]")

        # pattern_query already processes templates and updates session context
        return response or "..."

    def run(self) -> None:
        """Run the interactive chat loop."""
        print("ENGRAM Chat")
        print("Commands: /debug, /metrics, /topic <name>, /set <name> <value>, /save, /quit")
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
                if self._handle_command(line):
                    break
            else:
                response = self.process_input(line)
                print(f"Bot: {response}")

    def _handle_command(self, line: str) -> bool:
        """Handle slash commands. Returns True if should quit."""
        parts = line[1:].split(maxsplit=2)
        cmd = parts[0].lower()

        if cmd in ("quit", "exit", "q"):
            return True

        elif cmd == "debug":
            self.debug_mode = not self.debug_mode
            print(f"Debug mode: {'on' if self.debug_mode else 'off'}")

        elif cmd == "metrics":
            metrics = self.engram.get_metrics()
            print(f"Patterns: {len(self.engram._pattern_matcher)}")
            print(f"Statements: {metrics['statement_count']}")
            print(f"Sessions: {metrics['session_count']}")
            print(f"Hit rate: {metrics['hit_rate']:.1%}")

        elif cmd == "topic" and len(parts) >= 2:
            session = self.engram.get_session(self.session_id)
            if session:
                session.set_predicate("topic", parts[1])
                print(f"Topic set to: {parts[1]}")

        elif cmd == "set" and len(parts) >= 3:
            session = self.engram.get_session(self.session_id)
            if session:
                session.set_predicate(parts[1], parts[2])
                print(f"Set {parts[1]} = {parts[2]}")

        elif cmd == "get" and len(parts) >= 2:
            session = self.engram.get_session(self.session_id)
            if session:
                value = session.get_predicate(parts[1], "(not set)")
                print(f"{parts[1]} = {value}")

        elif cmd == "save":
            print("Saved.")

        elif cmd == "help":
            print("Commands:")
            print("  /debug         - Toggle debug mode")
            print("  /metrics       - Show metrics")
            print("  /topic <name>  - Set conversation topic")
            print("  /set <n> <v>   - Set predicate")
            print("  /get <name>    - Get predicate value")
            print("  /save          - Save to disk")
            print("  /quit          - Exit")

        else:
            print(f"Unknown command: {cmd} (try /help)")

        return False


def cmd_interactive(args: argparse.Namespace) -> int:
    """Interactive AIML-style chat."""
    engram = load_engram(args.store, args.capacity, args.eviction)

    chat = InteractiveChat(
        engram,
        session_id=args.session,
        enable_graph=args.graph,
    )

    chat.run()

    save_engram(engram, args.store)
    print("Saved.")
    return 0


def main() -> int:
    """Main entry point."""
    parser = create_parser()
    args = parser.parse_args()

    if args.command is None:
        args.command = "interactive"
        # Set defaults for interactive mode arguments when no subcommand was used
        args.session = None
        args.graph = False

    # Auto-init if store doesn't exist
    if args.command != "init" and not Path(args.store).exists():
        engram = Engram(config=EngramConfig(
            capacity=args.capacity,
            eviction_policy=get_eviction_policy(args.eviction),
        ))
        # Load seed responses from seed.json if available
        seed_file = Path(__file__).parent / "engram" / "seed.json"
        if seed_file.exists():
            with open(seed_file, encoding="utf-8") as f:
                seed_data = json.load(f)
            for pair in seed_data.get("pairs", []):
                pattern = pair.get("pattern", "")
                response = pair.get("response", "")
                template = pair.get("template")
                engram.store(response, tier=Tier.STATIC, pattern=pattern, template=template)
        save_engram(engram, args.store)
        print(f"Initialized engram store: {args.store}")

    commands = {
        "init": cmd_init,
        "store": cmd_store,
        "load": cmd_load,
        "query": cmd_query,
        "session": cmd_session,
        "metrics": cmd_metrics,
        "keywords": cmd_keywords,
        "coverage": cmd_coverage,
        "export": cmd_export,
        "interactive": cmd_interactive,
    }

    handler = commands.get(args.command)
    if handler:
        return handler(args)

    parser.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
