"""Apply the ENGRAM knowledge graph schema to MemGraph.

Reads schema.cypher (the recall-relevant subset of the canonical graph) and
executes each statement against the configured MemGraph instance. Idempotent:
existing indexes are skipped without error.

Usage:
    python scripts/setup_schema.py [--host HOST] [--port PORT] [--check]

Options:
    --host    MemGraph host (default: from config.yml graph.host)
    --port    MemGraph port (default: from config.yml graph.port)
    --check   Print statements that would run without executing
"""

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from engram.config import load_config
from engram.graph import MemGraphConnection

SCHEMA_FILE = Path(__file__).resolve().parent.parent / "schema.cypher"


def parse_statements(text):
    """Yield Cypher statements from schema text.

    Strips // line comments and joins multi-line statements terminated by ';'.
    """
    cleaned = []
    for raw in text.splitlines():
        stripped = raw.strip()
        if not stripped or stripped.startswith("//"):
            continue
        if "//" in raw:
            raw = raw.split("//", 1)[0]
        cleaned.append(raw)

    for stmt in "\n".join(cleaned).split(";"):
        stmt = stmt.strip()
        if stmt:
            yield stmt


def label_for(stmt):
    """Compact one-line label for a multi-line statement."""
    first = stmt.split("\n", 1)[0].strip()
    return first if len(first) <= 80 else first[:77] + "..."


def apply_schema(conn=(), dry_run=False) -> bool:
    """Apply every statement in schema.cypher; return True when none failed."""
    text = SCHEMA_FILE.read_text()
    statements = list(parse_statements(text))
    print(f"\nParsed {len(statements)} statements from {SCHEMA_FILE.name}\n")

    succeeded = 0
    skipped = 0
    failed = 0

    if dry_run:
        for stmt in statements:
            print(f"  [dry-run] {label_for(stmt)}")
        print(f"\nDry-run only -- {len(statements)} statement(s) would be applied.\n")
        return True

    for stmt in statements:
        label = label_for(stmt)
        try:
            cur = conn.conn.cursor()
            cur.execute(stmt)
            try:
                rows = cur.fetchall()
            except Exception:
                rows = []

            print(f"  OK:   {label}")
            if rows:
                for row in rows:
                    print(f"        {row}")
            succeeded += 1
        except Exception as err:
            err_str = str(err).lower()
            if "already exists" in err_str or "already created" in err_str:
                print(f"  SKIP: {label} (already exists)")
                skipped += 1
            else:
                print(f"  ERR:  {label}")
                print(f"        {err}")
                failed += 1

    total = succeeded + skipped + failed
    print(f"\nApplied {succeeded} statement(s), skipped {skipped}, failed {failed} (of {total})\n")
    return failed == 0


def main():
    """Apply the schema using config.yml or CLI overrides for the connection."""
    parser = argparse.ArgumentParser(description="Apply the ENGRAM schema to MemGraph")
    parser.add_argument("--host", default="", help="MemGraph host (default: from config.yml)")
    parser.add_argument("--port", type=int, default=0, help="MemGraph port (default: from config.yml)")
    parser.add_argument("--config", "-c", default="config.yml", help="Path to config.yml (default: config.yml)")
    parser.add_argument("--check", action="store_true", help="Print statements without executing")
    args = parser.parse_args()

    if args.check:
        success = apply_schema(dry_run=True)
        sys.exit(0 if success else 1)

    graph_cfg = load_config(args.config).get("graph") or {}
    host = args.host or graph_cfg.get("host", "localhost")
    port = args.port or graph_cfg.get("port", 7687)
    username = graph_cfg.get("username", "")
    password = graph_cfg.get("password", "")

    print(f"Connecting to MemGraph at {host}:{port}...")
    conn = MemGraphConnection(host=host, port=port, username=username, password=password)
    if not conn.connect():
        print(f"ERROR: Failed to connect to MemGraph at {host}:{port}")
        sys.exit(1)

    success = apply_schema(conn, dry_run=False)
    conn.disconnect()
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
