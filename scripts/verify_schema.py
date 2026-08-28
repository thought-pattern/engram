#!/usr/bin/env python3
"""Read-only schema and ownership verification for either Engram deployment."""

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

SCHEMA_FILE = REPO_ROOT / "schema.cypher"


def connection_settings(config_path: str, host: str, port: int) -> dict:
    """Resolve the verification connection without importing another script."""
    from engram.config import load_config

    configured = load_config(config_path).get("graph", {}) or {}
    return {
        "host": host or configured.get("host", "localhost"),
        "port": port or configured.get("port", 7687),
        "username": configured.get("username", ""),
        "password": configured.get("password", ""),
    }


def main() -> None:
    """Verify the explicitly selected deployment without issuing writes."""
    parser = argparse.ArgumentParser(description="Verify Engram's Memgraph contract")
    parser.add_argument(
        "--deployment",
        required=True,
        choices=("standalone", "tapestry_managed"),
        help="select the expected graph owner and acceptance state",
    )
    parser.add_argument("--host", default="", help="override Memgraph host")
    parser.add_argument("--port", type=int, default=0, help="override Memgraph port")
    parser.add_argument(
        "--config",
        "-c",
        default=str(REPO_ROOT / "config.example.yml"),
        help="Engram config path",
    )
    arguments = parser.parse_args()

    from engram.graph import MemGraphConnection
    from engram.schema_admin import verify_schema

    settings = connection_settings(arguments.config, arguments.host, arguments.port)
    connection = MemGraphConnection(**settings)
    try:
        if not connection.connect():
            raise RuntimeError(f"Memgraph is unavailable at {settings.get('host', '')}:{settings.get('port', 0)}")
        report = verify_schema(connection, SCHEMA_FILE, arguments.deployment)
        print(report)
        sys.exit(0 if report.get("compatible", False) else 1)
    except Exception as error:
        print({"compatible": False, "error": str(error)})
        sys.exit(1)
    finally:
        connection.disconnect()


if __name__ == "__main__":
    main()
