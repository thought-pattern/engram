"""Command line interface for ENGRAM."""

from argparse import ArgumentParser as argparse_ArgumentParser, Namespace as argparse_Namespace
from datetime import timedelta
from json import JSONDecodeError as json_JSONDecodeError, dumps as json_dumps, load as json_load, loads as json_loads
from pathlib import Path
from sys import exit as sys_exit, path as sys_path, stderr as sys_stderr

from engram import metrics, sessions
from engram.config import load_config
from engram.constants import NULL_DATETIME, EvictionPolicy, Tier
from engram.core import Engram
from engram.errors import EngramCoreError
from engram.service import EngramCore
from engram.sessions import SessionLimitExceededError, SessionNotFoundError

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys_path:
    sys_path.insert(0, str(_REPO_ROOT))


def create_parser() -> argparse_ArgumentParser:
    """Create the argument parser."""
    parser = argparse_ArgumentParser(
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
        default=False,
        help="Override maximum DYNAMIC statements (default: from config)",
    )
    parser.add_argument(
        "--eviction",
        type=str,
        choices=["fifo", "lru", "lfu", "hit_rate"],
        default=False,
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
    sync_parser.add_argument(
        "--prune",
        action="store_true",
        help="Retire STATIC statements absent from the seed (mirror, not just upsert)",
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
        "--user-id",
        dest="session",
        type=str,
        help="Caller-owned user/session label (created if absent)",
    )
    interactive_parser.add_argument(
        "--initial-bot-text",
        type=str,
        default="",
        help="Bot utterance immediately preceding the first interactive turn",
    )
    interactive_parser.add_argument(
        "--transcript",
        type=str,
        default="",
        help="Optional JSON recovery transcript updated after each turn",
    )
    interactive_parser.add_argument(
        "--graph",
        action="store_true",
        help="Enable mock graph client for testing",
    )

    return parser


def get_eviction_policy(name: str) -> EvictionPolicy:
    """Convert string to EvictionPolicy enum."""
    _return_value = {
        "fifo": EvictionPolicy.FIFO,
        "lru": EvictionPolicy.LRU,
        "lfu": EvictionPolicy.LFU,
        "hit_rate": EvictionPolicy.HIT_RATE,
    }.get(name, "")
    return _return_value


def resolve_config(args: argparse_Namespace) -> dict:
    """Build the EngramConfig from the config file, applying CLI flag overrides."""
    config = load_config(args.config)
    if args.capacity is not None:
        config["capacity"] = args.capacity
    if args.eviction is not None:
        config["eviction_policy"] = get_eviction_policy(args.eviction)
    return config


def load_core_instance(args: argparse_Namespace) -> EngramCore:
    """Load the shared core using the CLI's resolved configuration."""
    _return_value = EngramCore.open(config=args.engram_config, store_path=args.store)
    return _return_value


def load_seed_pairs(path: str = "") -> list:
    """Read seed pairs from a file (default: the bundled data/seed.json).

    Returns [] when the file does not exist.
    """
    seed_file = Path(path) if path else Path(_REPO_ROOT) / "data" / "seed.json"
    if not seed_file.exists():
        return []
    with open(seed_file, encoding="utf-8") as f:
        seed_data = json_load(f)
    pairs = seed_data.get("pairs", [])
    return pairs


def cmd_init(args: argparse_Namespace) -> int:
    """Initialize a new engram store, seeded from the bundled corpus."""
    path = Path(args.store)
    if path.exists() and not args.force:
        print(f"Store already exists: {args.store}", file=sys_stderr)
        print("Use --force to overwrite", file=sys_stderr)
        return 1

    core = EngramCore(Engram(config=args.engram_config), store_path=args.store)
    engram = core.engram
    pairs = load_seed_pairs()
    counts = engram.sync_corpus(pairs)
    core.flush()
    print(f"Initialized engram store: {args.store} ({counts.get('added', False)} seed statements)")
    return 0


def cmd_sync_seed(args: argparse_Namespace) -> int:
    """Upsert the seed corpus into an existing store.

    Refreshes stale STATIC templates in place (preserving ids and hit
    statistics) and adds new seed entries; DYNAMIC learned content is never
    touched. Run after updating data/seed.json so existing stores pick up the
    changes.
    """
    core = load_core_instance(args)
    engram = core.engram

    pairs = load_seed_pairs(args.file)
    if not pairs:
        source = args.file or "data/seed.json"
        print(f"No seed pairs found: {source}", file=sys_stderr)
        return 1

    counts = engram.sync_corpus(pairs, prune=args.prune)
    core.flush()
    summary = (
        f"Seed sync: {counts.get('added', False)} added, {counts.get('updated', False)} updated, "
        f"{counts.get('unchanged', False)} unchanged"
    )
    if args.prune:
        summary += f", {counts.get('pruned', False)} pruned"
    print(summary)
    return 0


def cmd_store(args: argparse_Namespace) -> int:
    """Store a statement."""
    core = load_core_instance(args)
    engram = core.engram
    tier = Tier.STATIC if args.static else Tier.DYNAMIC

    # Check if text is JSON template
    template = False
    text = args.text
    if args.text.startswith("{"):
        try:
            template = json_loads(args.text)
            text = template.get("text", args.text)
        except json_JSONDecodeError:
            pass

    stmt_id = engram.store(text, tier=tier, pattern=args.pattern, template=template)
    core.flush()
    print(f"Stored: {stmt_id} ({tier.value})")
    return 0


def cmd_load(args: argparse_Namespace) -> int:
    """Load statements from JSON file."""
    core = load_core_instance(args)
    engram = core.engram
    tier = Tier.STATIC if args.static else Tier.DYNAMIC

    path = Path(args.file)
    if not path.exists():
        print(f"File not found: {args.file}", file=sys_stderr)
        return 1

    with open(path, encoding="utf-8") as f:
        data = json_load(f)

    count = 0
    pairs = data.get("pairs", data.get("categories", []))
    for item in pairs:
        if isinstance(item, dict):
            pattern = item.get("pattern", "")
            response = item.get("response", "") or item.get("text", "") or ""
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

    core.flush()
    print(f"Loaded {count} statements ({tier.value})")
    return 0


def cmd_query(args: argparse_Namespace) -> int:
    """Query for statements."""
    core = load_core_instance(args)
    engram = core.engram

    result = engram.query(args.text, session_id=args.session, limit=args.limit)

    # A query is a stat-generating event: query() bumped the global and
    # per-keyword query counters, so persist them. --hit additionally records
    # the hit (numerator) before saving.
    if args.hit and result.get("matches", False):
        top_statement = result.get("matches", [])[0][0]
        engram.record_hit(result.get("keywords", []), statement_id=top_statement.get("id", ""))
    core.flush()

    print(f"Keywords: {', '.join(result.get('keywords', []))}")
    print(f"Matches: {len(result.get('matches', []))}")
    print()

    for i, (stmt, score) in enumerate(result.get("matches", []), 1):
        print(f"{i}. [{score:.3f}] ({stmt.get('tier', Tier.DYNAMIC).value}) {stmt.get('text', '')}")

    if not result.get("matches", []):
        print("No matches found.")

    return 0


def cmd_session(args: argparse_Namespace) -> int:
    """Session management commands."""
    core = load_core_instance(args)
    engram = core.engram
    modified = False

    if args.session_command == "create":
        try:
            session_id = sessions.create_session(engram, session_id=args.id)
            print(f"Created session: {session_id}")
            modified = True
        except SessionLimitExceededError as e:
            print(f"Error: {e}", file=sys_stderr)
            return 1

    elif args.session_command == "list":
        session_list = sessions.list_sessions(engram)
        if session_list:
            print(f"Sessions ({len(session_list)}):")
            for s in session_list:
                topic = s.get("predicates", {}).get("topic", "") or "(none)"
                print(f"  {s.get('session_id', '')}: topic={topic}, last={s.get('last_active', NULL_DATETIME).isoformat()}")
        else:
            print("No sessions.")

    elif args.session_command == "get":
        session = sessions.get_session(engram, args.id, create_if_missing=False)
        if session:
            print(f"Session: {session.get('session_id', '')}")
            print(f"Created: {session.get('created_at', NULL_DATETIME).isoformat()}")
            print(f"Last active: {session.get('last_active', NULL_DATETIME).isoformat()}")
            print(f"Topic: {session.get('predicates', {}).get('topic', '') or '(none)'}")
            print(f"That: {session.get('previous_response', '') or '(empty)'}")
            if session.get("predicates", []):
                print(f"Predicates: {json_dumps(session.get('predicates', []))}")
            if session.get("input_history", []):
                print(f"Input history: {session.get('input_history', [])[:5]}")
        else:
            print(f"Session not found: {args.id}", file=sys_stderr)
            return 1

    elif args.session_command == "update":
        try:
            sessions.update_session_context(engram, args.id, args.response)
            print(f"Updated session: {args.id}")
            modified = True
        except SessionNotFoundError:
            print(f"Session not found: {args.id}", file=sys_stderr)
            return 1

    elif args.session_command == "delete":
        if sessions.delete_session(engram, args.id):
            print(f"Deleted session: {args.id}")
            modified = True
        else:
            print(f"Session not found: {args.id}", file=sys_stderr)
            return 1

    elif args.session_command == "expire":
        threshold = timedelta(hours=args.hours)
        count = sessions.expire_sessions(engram, inactive_threshold=threshold)
        print(f"Expired {count} sessions")
        modified = count > 0

    elif args.session_command == "set":
        session = sessions.get_session(engram, args.id, create_if_missing=False)
        if session:
            session.get("predicates", {})[args.name] = args.value
            print(f"Set {args.name}={args.value} for session {args.id}")
            modified = True
        else:
            print(f"Session not found: {args.id}", file=sys_stderr)
            return 1

    elif args.session_command == "topic":
        session = sessions.get_session(engram, args.id, create_if_missing=False)
        if session:
            session.get("predicates", {})["topic"] = args.topic
            print(f"Set topic={args.topic} for session {args.id}")
            modified = True
        else:
            print(f"Session not found: {args.id}", file=sys_stderr)
            return 1

    else:
        print("Usage: engram session {create|list|get|update|delete|expire|set|topic}")
        return 1

    if modified:
        core.flush()

    return 0


def cmd_metrics(args: argparse_Namespace) -> int:
    """Show store metrics."""
    core = load_core_instance(args)
    engram = core.engram
    metric_data = metrics.get_metrics(engram)

    print("ENGRAM Metrics")
    print("-" * 40)
    print(f"Statements:     {metric_data.get('statement_count', 0):,}")
    print(f"  STATIC:       {metric_data.get('static_count', 0):,}")
    print(f"  DYNAMIC:      {metric_data.get('dynamic_count', 0):,}")
    print(f"Keywords:       {metric_data.get('keyword_count', 0):,}")
    print(f"Sessions:       {metric_data.get('session_count', 0):,}")
    print(f"Total queries:  {metric_data.get('query_count', 0):,}")
    print(f"Total hits:     {metric_data.get('hit_count', 0):,}")
    print(f"Hit rate:       {metric_data.get('hit_rate', 0.0):.1%}")
    print(f"Evictions:      {metric_data.get('eviction_count', 0):,}")
    print(f"Eviction policy: {engram.config.get('eviction_policy', EvictionPolicy.FIFO).value}")

    return 0


def cmd_decay(args: argparse_Namespace) -> int:
    """Age hit statistics so old evidence loses standing over time."""
    core = load_core_instance(args)
    engram = core.engram

    try:
        changed = metrics.decay_statistics(engram, factor=args.factor)
    except ValueError as err:
        print(f"Error: {err}", file=sys_stderr)
        return 1

    core.flush()
    print(f"Decayed statistics on {changed} records (factor {args.factor})")
    return 0


def cmd_keywords(args: argparse_Namespace) -> int:
    """Keyword analysis."""
    core = load_core_instance(args)
    engram = core.engram

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


def cmd_coverage(args: argparse_Namespace) -> int:
    """Coverage analysis."""
    core = load_core_instance(args)
    engram = core.engram

    if args.report:
        report = metrics.get_coverage_report(engram)
        print("Coverage Report")
        print("-" * 40)
        print(f"Total keywords:      {report.get('total_keywords', 0):,}")
        print(f"Keywords with hits:  {report.get('keywords_with_hits', []):,}")
        print(f"Keywords zero hits:  {report.get('keywords_zero_hits', []):,}")
        print(f"Overall hit rate:    {report.get('overall_hit_rate', 0.0):.1%}")
        print()

        if report.get("coverage_gaps", []):
            print("Coverage Gaps (high queries, low hits):")
            for gap in report.get("coverage_gaps", [])[:10]:
                print(f"  {gap.get('keyword', False)}: {gap.get('queries', [])} queries, {gap.get('hit_rate', 0.0):.1%} hit rate")
            print()

        if report.get("top_performing", False):
            print("Top Performing Keywords:")
            for kw in report.get("top_performing", [])[:5]:
                print(f"  {kw.get('keyword', False)}: {kw.get('queries', [])} queries, {kw.get('hit_rate', 0.0):.1%} hit rate")
            print()

        if report.get("recommendations", []):
            print("Recommendations:")
            for rec in report.get("recommendations", []):
                print(f"  - {rec}")

    elif args.gaps:
        gaps = metrics.get_coverage_gaps(engram, min_queries=args.min_queries, max_hit_rate=0.2)
        if gaps:
            print(f"Coverage gaps (min queries: {args.min_queries}, max hit rate: 20%):")
            for gap in gaps[:20]:
                print(
                    f"  {gap.get('keyword', False)}: {gap.get('queries', [])} queries, {gap.get('hits', [])}"
                    f" hits, {gap.get('hit_rate', 0.0):.1%}"
                )
        else:
            print("No coverage gaps found.")

    else:
        print("Usage: engram coverage {--gaps|--report}")
        return 1

    return 0


def cmd_export(args: argparse_Namespace) -> int:
    """Export statements."""
    core = load_core_instance(args)
    engram = core.engram

    statements = []
    for stmt in engram.statements:
        if args.static_only and stmt.get("tier", "") != Tier.STATIC:
            continue
        if args.dynamic_only and stmt.get("tier", "") != Tier.DYNAMIC:
            continue
        statements.append(stmt)

    if args.json:
        pairs = []
        for stmt in statements:
            item = {"pattern": stmt.get("pattern", ""), "response": stmt.get("text", "")}
            if stmt.get("template", ""):
                item["template"] = stmt.get("template", "")
            if stmt.get("that", False):
                item["that"] = stmt.get("that", False)
            if stmt.get("topic", ""):
                item["topic"] = stmt.get("topic", "")
            pairs.append(item)
        output = json_dumps({"pairs": pairs}, indent=2)
    else:
        output = "\n".join(stmt.get("text", "") for stmt in statements)

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
        core_or_engram: object,
        session_id=False,
        enable_graph: bool = False,
        store_path: str = "",
        initial_bot_text: str = "",
        transcript_path: str = "",
    ):
        if isinstance(core_or_engram, EngramCore):
            self.core = core_or_engram
        else:
            self.core = EngramCore(core_or_engram, store_path=store_path)
        self.engram = self.core.engram
        self.store_path = str(self.core.store_path) if self.core.store_path else store_path
        self.debug_mode = False
        self.session_id = session_id or sessions.create_session(
            self.engram,
        )

        self.core.start_conversation(
            user_id=self.session_id,
            initial_bot_text=initial_bot_text,
            transcript_path=transcript_path or False,
        )
        self.runtime = self.core.get_conversation(self.session_id)
        self.session = sessions.get_session(self.engram, self.session_id, create_if_missing=True)

        # Note: Graph support would require extending core.py to accept graph callbacks
        if enable_graph:
            print("Note: Graph client support is a future feature")

    def process_input(self, user_input: str) -> str:
        """Process user input through the tiered pipeline and return a response.

        Pattern match first; a question that only hits the catch-all consults
        keyword retrieval before settling for the deflection.
        """
        result = self.core.chat(self.session_id, user_input)

        if self.debug_mode:
            detail = f"Source: {result.get('source', '')} | Score: {result.get('score', 0.0):.2f}"
            if result.get("pattern", ""):
                detail += f" | Pattern: '{result.get('pattern', '')}' | Captured: {result.get('captured', False)}"
            print(f"     [{detail}]")

        _return_value = result.get("response", "") or "Tell me more about that."
        return _return_value

    def run(self) -> bool:
        """Run the interactive chat loop."""
        print("ENGRAM Chat")
        print(
            "Commands: /debug, /inspect, /metrics, /finish [prefix], /topic <name>, /set <name> <value>, /get <name>, /save, /quit"
        )
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
            metric_data = self.core.inspect_conversation(self.session_id).get("metrics", {})
            print(f"Patterns: {len(self.engram.pattern_matcher)}")
            print(f"Statements: {metric_data.get('statement_count', 0)}")
            print(f"Sessions: {metric_data.get('session_count', 0)}")
            print(f"Hit rate: {metric_data.get('hit_rate', 0.0):.1%}")

        elif cmd == "inspect":
            print(json_dumps(self.core.inspect_conversation(self.session_id), indent=2))

        elif cmd == "finish":
            output_prefix = line.split(maxsplit=1)[1] if len(line.split(maxsplit=1)) == 2 else "engram-chat-transcript"
            print(json_dumps(self.core.finish_conversation(self.session_id, output_prefix), indent=2))

        elif cmd == "topic" and len(parts) >= 2:
            self.core.set_predicate(self.session_id, "topic", parts[1])
            print(f"Topic set to: {parts[1]}")

        elif cmd == "set" and len(parts) >= 3:
            self.core.set_predicate(self.session_id, parts[1], parts[2])
            print(f"Set {parts[1]} = {parts[2]}")

        elif cmd == "get" and len(parts) >= 2:
            value = self.core.get_predicate(self.session_id, parts[1], "(not set)")
            print(f"{parts[1]} = {value}")

        elif cmd == "save":
            if self.core.flush():
                print(f"Saved: {self.store_path}")
            else:
                print("No store path configured; state will be saved on exit.")

        elif cmd == "help":
            print("Commands:")
            print("  /debug         - Toggle debug mode")
            print("  /inspect       - Show context, learned facts, and metrics")
            print("  /metrics       - Show metrics")
            print("  /finish [path] - Write JSON and Markdown conversation reports")
            print("  /topic <name>  - Set conversation topic")
            print("  /set <n> <v>   - Set predicate")
            print("  /get <name>    - Get predicate value")
            print("  /save          - Save to disk")
            print("  /quit          - Exit")

        else:
            print(f"Unknown command: {cmd} (try /help)")

        return False


def cmd_interactive(args: argparse_Namespace) -> int:
    """Interactive AIML-style chat."""
    core = load_core_instance(args)

    chat = InteractiveChat(
        core,
        session_id=args.session,
        enable_graph=args.graph,
        store_path=args.store,
        initial_bot_text=args.initial_bot_text,
        transcript_path=args.transcript,
    )

    chat.run()

    core.stop_conversation(chat.session_id)
    print("Saved.")
    return 0


def main(argv=False) -> int:
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
        args.session = False
        args.graph = False
        args.initial_bot_text = ""
        args.transcript = ""

    # Resolve configuration once (config.yml, with CLI flag overrides applied).
    args.engram_config = resolve_config(args)

    # Auto-init if store doesn't exist
    if args.command != "init" and not Path(args.store).exists():
        core = EngramCore(Engram(config=args.engram_config), store_path=args.store)
        engram = core.engram
        engram.sync_corpus(load_seed_pairs())
        core.flush()
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

    handler = commands.get(args.command, False)
    if handler:
        try:
            _return_value = handler(args)
            return _return_value
        except EngramCoreError as error:
            print(f"Error: {error}", file=sys_stderr)
            return 1

    parser.print_help()
    return 1


if __name__ == "__main__":
    sys_exit(main())
