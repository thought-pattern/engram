#!/usr/bin/env python3
"""Resolve or apply an exact standalone Engram catalog reset."""

from argparse import ArgumentParser as argparse_ArgumentParser
from pathlib import Path
from sys import exit as sys_exit, path as sys_path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys_path:
    sys_path.insert(0, str(REPO_ROOT))

SCHEMA_FILE = REPO_ROOT / "schema.cypher"


from engram.config import load_config
from engram.graph import MemGraphConnection
from engram.schema_reset import apply_schema_reset, resolve_schema_reset


def connection_settings(config_path: str, host: str, port: int) -> dict:
    """Resolve standalone administrative settings."""

    configured = load_config(config_path).get("graph", {}) or {}
    result = {
        "host": host or configured.get("host", "localhost"),
        "port": port or configured.get("port", 7687),
        "username": configured.get("username", ""),
        "password": configured.get("password", ""),
    }
    return result


def main() -> None:
    """Dry-run by default; mutate only with explicit --apply."""
    parser = argparse_ArgumentParser(description="Reset standalone Engram schema")
    parser.add_argument("--apply", action="store_true", help="drop resolved definitions")
    parser.add_argument("--host", default="", help="override Memgraph host")
    parser.add_argument("--port", type=int, default=0, help="override Memgraph port")
    parser.add_argument(
        "--config",
        "-c",
        default=str(REPO_ROOT / "config.example.yml"),
        help="Engram config path",
    )
    arguments = parser.parse_args()

    settings = connection_settings(arguments.config, arguments.host, arguments.port)
    connection = MemGraphConnection(**settings)
    try:
        if not connection.connect():
            raise RuntimeError(f"Memgraph is unavailable at {settings.get('host', '')}:{settings.get('port', 0)}")
        if arguments.apply:
            report = apply_schema_reset(connection, SCHEMA_FILE)
        else:
            report = resolve_schema_reset(connection, SCHEMA_FILE)
            report["applied"] = False
        print(report)
        sys_exit(0)
    except Exception as error:
        print({"valid": False, "error": str(error)})
        sys_exit(1)
    finally:
        connection.disconnect()


if __name__ == "__main__":
    main()
