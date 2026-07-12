"""Command line interface for ENGRAM."""

import argparse
import json
import os
import sys
from pathlib import Path

# Allow running as `python scripts/cli.py` without installing the package: put
# the repo root (this file's parent's parent) on the import path.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from engram import metrics, persistence, pipeline, sessions
from engram.config import load_config
from engram.constants import EvictionPolicy, Tier
from engram.core import Engram
from engram.sessions import SessionLimitExceededError, SessionNotFoundError


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
        "--config",
        "-c",
        type=str,
        default="config.yml",
        help="Path to YAML config file (default: config.yml; defaults used if absent)",
    )
    parser.add_argument(
        "--capacity",
        type=int,
        default=None,
        help="Override maximum DYNAMIC statements (default: from config)",
    )
    parser.add_argument(
        "--eviction",
        type=str,
        choices=["fifo", "lru", "lfu", "hit_rate"],
        default=None,
        help="Override eviction policy (default: from config)",
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

    # sync-seed command
    sync_parser = subparsers.add_parser(
        "sync-seed",
        help="Upsert the bundled seed corpus into the store (refresh stale templates)",
    )
    sync_parser.add_argument(
        "--file",
        type=str,
        default="",
        help="Seed file to sync from (default: data/seed.json)",
    )

    # metrics command
    subparsers.add_parser("metrics", help="Show store metrics")

    # decay command
    decay_parser = subparsers.add_parser("decay", help="Age hit statistics (run periodically)")
    decay_parser.add_argument(
        "--factor",
        type=float,
        default=0.5,
        help="Multiplier applied to every hit/query count (default: 0.5)",
    )

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


def resolve_config(args: argparse.Namespace) -> dict:
    """Build the EngramConfig from the config file, applying CLI flag overrides."""
    config = load_config(args.config)
    if args.capacity is not None:
        config["capacity"] = args.capacity
    if args.eviction is not None:
        config["eviction_policy"] = get_eviction_policy(args.eviction)
    return config


def load_engram_instance(args: argparse.Namespace) -> Engram:
    """Load engram from file (applying the resolved config) or create a new one."""
    path = Path(args.store)
    if path.exists():
        return persistence.load_engram(path, config=args.engram_config)
    return Engram(config=args.engram_config)


def save_engram(engram: Engram, store_path: str) -> None:
    """Save engram to file."""
    persistence.save(engram, store_path)


def load_seed_pairs(path: str = "") -> list:
    """Read seed pairs from a file (default: the bundled data/seed.json).

    Returns [] when the file does not exist.
    """
    seed_file = Path(path) if path else Path(_REPO_ROOT) / "data" / "seed.json"
    if not seed_file.exists():
        return []
    with open(seed_file, encoding="utf-8") as f:
        seed_data = json.load(f)
    pairs = seed_data.get("pairs", [])
    return pairs


def cmd_init(args: argparse.Namespace) -> int:
    """Initialize a new engram store, seeded from the bundled corpus."""
    path = Path(args.store)
    if path.exists() and not args.force:
        print(f"Store already exists: {args.store}", file=sys.stderr)
        print("Use --force to overwrite", file=sys.stderr)
        return 1

    engram = Engram(config=args.engram_config)
    pairs = load_seed_pairs()
    counts = engram.sync_corpus(pairs)
    save_engram(engram, args.store)
    print(f"Initialized engram store: {args.store} ({counts['added']} seed statements)")
    return 0


def cmd_sync_seed(args: argparse.Namespace) -> int:
    """Upsert the seed corpus into an existing store.

    Refreshes stale STATIC templates in place (preserving ids and hit
    statistics) and adds new seed entries; DYNAMIC learned content is never
    touched. Run after updating data/seed.json so existing stores pick up the
    changes.
    """
    engram = load_engram_instance(args)

    pairs = load_seed_pairs(args.file)
    if not pairs:
        source = args.file or "data/seed.json"
        print(f"No seed pairs found: {source}", file=sys.stderr)
        return 1

    counts = engram.sync_corpus(pairs)
    save_engram(engram, args.store)
    print(f"Seed sync: {counts['added']} added, {counts['updated']} updated, {counts['unchanged']} unchanged")
    return 0


def cmd_store(args: argparse.Namespace) -> int:
    """Store a statement."""
    engram = load_engram_instance(args)
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
    engram = load_engram_instance(args)
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
            response = item.get("response") or item.get("text") or ""
            template = item.get("template", {})
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
    engram = load_engram_instance(args)

    result = engram.query(args.text, session_id=args.session, limit=args.limit)

    # A query is a stat-generating event: query() bumped the global and
    # per-keyword query counters, so persist them. --hit additionally records
    # the hit (numerator) before saving.
    if args.hit and result["matches"]:
        engram.record_hit(result["keywords"])
    save_engram(engram, args.store)

    print(f"Keywords: {', '.join(result['keywords'])}")
    print(f"Matches: {len(result['matches'])}")
    print()

    for i, (stmt, score) in enumerate(result["matches"], 1):
        print(f"{i}. [{score:.3f}] ({stmt['tier'].value}) {stmt['text']}")

    if not result["matches"]:
        print("No matches found.")

    return 0


def cmd_session(args: argparse.Namespace) -> int:
    """Session management commands."""
    engram = load_engram_instance(args)
    modified = False

    if args.session_command == "create":
        try:
            session_id = sessions.create_session(engram, session_id=args.id)
            print(f"Created session: {session_id}")
            modified = True
        except SessionLimitExceededError as e:
            print(f"Error: {e}", file=sys.stderr)
            return 1

    elif args.session_command == "list":
        session_list = sessions.list_sessions(engram)
        if session_list:
            print(f"Sessions ({len(session_list)}):")
            for s in session_list:
                topic = s["predicates"].get("topic", "") or "(none)"
                print(f"  {s['session_id']}: topic={topic}, last={s['last_active'].isoformat()}")
        else:
            print("No sessions.")

    elif args.session_command == "get":
        session = sessions.get_session(engram, args.id, create_if_missing=False)
        if session:
            print(f"Session: {session['session_id']}")
            print(f"Created: {session['created_at'].isoformat()}")
            print(f"Last active: {session['last_active'].isoformat()}")
            print(f"Topic: {session.get('predicates', {}).get('topic', '') or '(none)'}")
            print(f"That: {session['previous_response'] or '(empty)'}")
            if session["predicates"]:
                print(f"Predicates: {json.dumps(session['predicates'])}")
            if session["input_history"]:
                print(f"Input history: {session.get('input_history', [])[:5]}")
        else:
            print(f"Session not found: {args.id}", file=sys.stderr)
            return 1

    elif args.session_command == "update":
        try:
            sessions.update_session_context(engram, args.id, args.response)
            print(f"Updated session: {args.id}")
            modified = True
        except SessionNotFoundError:
            print(f"Session not found: {args.id}", file=sys.stderr)
            return 1

    elif args.session_command == "delete":
        if sessions.delete_session(engram, args.id):
            print(f"Deleted session: {args.id}")
            modified = True
        else:
            print(f"Session not found: {args.id}", file=sys.stderr)
            return 1

    elif args.session_command == "expire":
        from datetime import timedelta

        threshold = timedelta(hours=args.hours)
        count = sessions.expire_sessions(engram, inactive_threshold=threshold)
        print(f"Expired {count} sessions")
        modified = count > 0

    elif args.session_command == "set":
        session = sessions.get_session(engram, args.id, create_if_missing=False)
        if session:
            session["predicates"][args.name] = args.value
            print(f"Set {args.name}={args.value} for session {args.id}")
            modified = True
        else:
            print(f"Session not found: {args.id}", file=sys.stderr)
            return 1

    elif args.session_command == "topic":
        session = sessions.get_session(engram, args.id, create_if_missing=False)
        if session:
            session["predicates"]["topic"] = args.topic
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
    engram = load_engram_instance(args)
    metric_data = metrics.get_metrics(engram)

    print("ENGRAM Metrics")
    print("-" * 40)
    print(f"Statements:     {metric_data['statement_count']:,}")
    print(f"  STATIC:       {metric_data['static_count']:,}")
    print(f"  DYNAMIC:      {metric_data['dynamic_count']:,}")
    print(f"Keywords:       {metric_data['keyword_count']:,}")
    print(f"Sessions:       {metric_data['session_count']:,}")
    print(f"Total queries:  {metric_data['query_count']:,}")
    print(f"Total hits:     {metric_data['hit_count']:,}")
    print(f"Hit rate:       {metric_data['hit_rate']:.1%}")
    print(f"Evictions:      {metric_data['eviction_count']:,}")
    print(f"Eviction policy: {engram.config.get('eviction_policy', EvictionPolicy.FIFO).value}")

    return 0


def cmd_decay(args: argparse.Namespace) -> int:
    """Age hit statistics so old evidence loses standing over time."""
    engram = load_engram_instance(args)

    try:
        changed = metrics.decay_statistics(engram, factor=args.factor)
    except ValueError as err:
        print(f"Error: {err}", file=sys.stderr)
        return 1

    save_engram(engram, args.store)
    print(f"Decayed statistics on {changed} records (factor {args.factor})")
    return 0


def cmd_keywords(args: argparse.Namespace) -> int:
    """Keyword analysis."""
    engram = load_engram_instance(args)

    if args.zero_hit:
        results = metrics.get_zero_hit_keywords(engram, min_queries=args.min_queries)
        if results:
            print(f"Zero-hit keywords (min queries: {args.min_queries}):")
            for kw, query_count in results[:20]:
                print(f"  {kw}: {query_count} queries, 0 hits")
        else:
            print("No zero-hit keywords found.")

    elif args.low_hit:
        low = metrics.get_low_hit_keywords(engram, min_queries=args.min_queries, max_hit_rate=0.2)
        if low:
            print(f"Low hit-rate keywords (min queries: {args.min_queries}, max rate: 20%):")
            for kw, query_count, hit_rate in low[:20]:
                print(f"  {kw}: {query_count} queries, {hit_rate:.1%} hit rate")
        else:
            print("No low hit-rate keywords found.")

    else:
        print("Usage: engram keywords {--low-hit|--zero-hit}")
        return 1

    return 0


def cmd_coverage(args: argparse.Namespace) -> int:
    """Coverage analysis."""
    engram = load_engram_instance(args)

    if args.report:
        report = metrics.get_coverage_report(engram)
        print("Coverage Report")
        print("-" * 40)
        print(f"Total keywords:      {report['total_keywords']:,}")
        print(f"Keywords with hits:  {report['keywords_with_hits']:,}")
        print(f"Keywords zero hits:  {report['keywords_zero_hits']:,}")
        print(f"Overall hit rate:    {report['overall_hit_rate']:.1%}")
        print()

        if report["coverage_gaps"]:
            print("Coverage Gaps (high queries, low hits):")
            for gap in report.get("coverage_gaps", [])[:10]:
                print(f"  {gap['keyword']}: {gap['queries']} queries, {gap['hit_rate']:.1%} hit rate")
            print()

        if report["top_performing"]:
            print("Top Performing Keywords:")
            for kw in report.get("top_performing", [])[:5]:
                print(f"  {kw['keyword']}: {kw['queries']} queries, {kw['hit_rate']:.1%} hit rate")
            print()

        if report["recommendations"]:
            print("Recommendations:")
            for rec in report.get("recommendations", []):
                print(f"  - {rec}")

    elif args.gaps:
        gaps = metrics.get_coverage_gaps(engram, min_queries=args.min_queries, max_hit_rate=0.2)
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
    engram = load_engram_instance(args)

    statements = []
    for stmt in engram.statements:
        if args.static_only and stmt["tier"] != Tier.STATIC:
            continue
        if args.dynamic_only and stmt["tier"] != Tier.DYNAMIC:
            continue
        statements.append(stmt)

    if args.json:
        pairs = []
        for stmt in statements:
            item = {"pattern": stmt["pattern"], "response": stmt["text"]}
            if stmt["template"]:
                item["template"] = stmt["template"]
            if stmt["that"]:
                item["that"] = stmt["that"]
            if stmt["topic"]:
                item["topic"] = stmt["topic"]
            pairs.append(item)
        output = json.dumps({"pairs": pairs}, indent=2)
    else:
        output = "\n".join(stmt["text"] for stmt in statements)

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
        session_id=None,
        enable_graph: bool = False,
        store_path: str = "",
    ):
        self.engram = engram
        self.store_path = store_path
        self.debug_mode = False
        self.session_id = session_id or sessions.create_session(
            engram,
        )

        # Ensure session exists
        self.session = sessions.get_session(engram, self.session_id, create_if_missing=True)

        # Note: Graph support would require extending core.py to accept graph callbacks
        if enable_graph:
            print("Note: Graph client support is a future feature")

    def process_input(self, user_input: str) -> str:
        """Process user input through the tiered pipeline and return a response.

        Pattern match first; a question that only hits the catch-all consults
        keyword retrieval before settling for the deflection.
        """
        result = pipeline.respond(self.engram, user_input, session_id=self.session_id)

        if self.debug_mode:
            print(f"     [Source: {result['source']} | Score: {result['score']:.2f}]")

        return result["response"] or "Tell me more about that."

    def run(self) -> None:
        """Run the interactive chat loop."""
        print("ENGRAM Chat")
        print("Commands: /debug, /metrics, /topic <name>, /set <name> <value>, /get <name>, /save, /quit")
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
            metric_data = metrics.get_metrics(self.engram)
            print(f"Patterns: {len(self.engram.pattern_matcher)}")
            print(f"Statements: {metric_data['statement_count']}")
            print(f"Sessions: {metric_data['session_count']}")
            print(f"Hit rate: {metric_data['hit_rate']:.1%}")

        elif cmd == "topic" and len(parts) >= 2:
            session = sessions.get_session(self.engram, self.session_id)
            if session:
                session["predicates"]["topic"] = parts[1]
                print(f"Topic set to: {parts[1]}")

        elif cmd == "set" and len(parts) >= 3:
            session = sessions.get_session(self.engram, self.session_id)
            if session:
                session["predicates"][parts[1]] = parts[2]
                print(f"Set {parts[1]} = {parts[2]}")

        elif cmd == "get" and len(parts) >= 2:
            session = sessions.get_session(self.engram, self.session_id)
            if session:
                value = session.get("predicates", {}).get(parts[1], "(not set)")
                print(f"{parts[1]} = {value}")

        elif cmd == "save":
            if self.store_path:
                save_engram(self.engram, self.store_path)
                print(f"Saved: {self.store_path}")
            else:
                print("No store path configured; state will be saved on exit.")

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
    engram = load_engram_instance(args)

    chat = InteractiveChat(
        engram,
        session_id=args.session,
        enable_graph=args.graph,
        store_path=args.store,
    )

    chat.run()

    save_engram(engram, args.store)
    print("Saved.")
    return 0


def main(argv=None) -> int:
    """Main entry point.

    Args:
        argv: Optional argument list (defaults to sys.argv), so tests can
            drive the CLI in-process.
    """
    parser = create_parser()
    args = parser.parse_args(argv)

    if args.command is None:
        args.command = "interactive"
        # Set defaults for interactive mode arguments when no subcommand was used
        args.session = None
        args.graph = False

    # Resolve configuration once (config.yml, with CLI flag overrides applied).
    args.engram_config = resolve_config(args)

    # Auto-init if store doesn't exist
    if args.command != "init" and not Path(args.store).exists():
        engram = Engram(config=args.engram_config)
        engram.sync_corpus(load_seed_pairs())
        save_engram(engram, args.store)
        print(f"Initialized engram store: {args.store}")

    commands = {
        "init": cmd_init,
        "store": cmd_store,
        "load": cmd_load,
        "query": cmd_query,
        "session": cmd_session,
        "metrics": cmd_metrics,
        "sync-seed": cmd_sync_seed,
        "decay": cmd_decay,
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
