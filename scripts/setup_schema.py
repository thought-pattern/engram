#!/usr/bin/env python3
"""Check, install, or verify the standalone Engram Memgraph schema."""

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

SCHEMA_FILE = REPO_ROOT / "schema.cypher"


def static_check() -> dict:
    """Validate Engram DDL without configuration or network access."""
    from engram.schema_catalog import cypher_statements, validate_schema_contract

    text = SCHEMA_FILE.read_text(encoding="utf-8")
    catalog = validate_schema_contract(text)
    return {
        "compatible": True,
        "statements": len(cypher_statements(text)),
        "ordinary_indexes": len(catalog.get("ordinary_indexes", [])),
        "text_indexes": len(catalog.get("text_indexes", [])),
        "vector_indexes": len(catalog.get("vector_indexes", [])),
        "constraints": len(catalog.get("constraints", [])),
        "relationships": len(catalog.get("relationships", [])),
    }


def connection_settings(config_path: str, host: str, port: int) -> dict:
    """Resolve standalone administrative settings after mode selection."""
    from engram.config import load_config

    configured = load_config(config_path).get("graph", {}) or {}
    return {
        "host": host or configured.get("host", "localhost"),
        "port": port or configured.get("port", 7687),
        "username": configured.get("username", ""),
        "password": configured.get("password", ""),
    }


def connected_operation(mode: str, settings: dict) -> dict:
    """Run one standalone operation and always disconnect."""
    from engram.graph import MemGraphConnection
    from engram.schema_admin import install_standalone_schema, verify_schema

    connection = MemGraphConnection(**settings)
    if not connection.connect():
        raise RuntimeError(f"Memgraph is unavailable at {settings.get('host', '')}:{settings.get('port', 0)}")
    try:
        if mode == "apply":
            return install_standalone_schema(connection, SCHEMA_FILE)
        return verify_schema(connection, SCHEMA_FILE, "standalone")
    finally:
        connection.disconnect()


def main() -> None:
    """Select exactly one explicit standalone schema administration mode."""
    parser = argparse.ArgumentParser(description="Administer the standalone Engram schema")
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument("--check", action="store_true", help="validate files only")
    modes.add_argument("--verify", action="store_true", help="verify standalone schema")
    modes.add_argument("--apply", action="store_true", help="install on an empty graph")
    parser.add_argument("--host", default="", help="override Memgraph host")
    parser.add_argument("--port", type=int, default=0, help="override Memgraph port")
    parser.add_argument(
        "--config",
        "-c",
        default=str(REPO_ROOT / "config.example.yml"),
        help="Engram config path",
    )
    arguments = parser.parse_args()
    try:
        if arguments.check:
            report = static_check()
        else:
            mode = "apply" if arguments.apply else "verify"
            report = connected_operation(
                mode,
                connection_settings(arguments.config, arguments.host, arguments.port),
            )
        print(report)
        sys.exit(0 if report.get("compatible", False) else 1)
    except Exception as error:
        print({"compatible": False, "error": str(error)})
        sys.exit(1)


if __name__ == "__main__":
    main()
