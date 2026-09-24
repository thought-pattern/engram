#!/usr/bin/env python3
"""Read-only schema and ownership verification for either Engram deployment."""

from argparse import ArgumentParser as argparse_ArgumentParser
from pathlib import Path
from sys import exit as sys_exit, path as sys_path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys_path:
    sys_path.insert(0, str(REPO_ROOT))

SCHEMA_FILE = REPO_ROOT / "schema.cypher"


from engram.config import load_config
from engram.graph import MemGraphConnection
from engram.schema_admin import verify_schema


def connection_settings(config_path: str, host: str, port: int) -> dict:
    """Resolve the verification connection without importing another script."""

    configured = load_config(config_path).get("graph", {}) or {}
    result = {
        "host": host or configured.get("host", "localhost"),
        "port": port or configured.get("port", 7687),
        "username": configured.get("username", ""),
        "password": configured.get("password", ""),
    }
    return result


def main() -> None:
    """Verify the explicitly selected deployment without issuing writes."""
    parser = argparse_ArgumentParser(description="Verify Engram's Memgraph contract")
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

    settings = connection_settings(arguments.config, arguments.host, arguments.port)
    connection = MemGraphConnection(**settings)
    try:
        if not connection.connect():
            raise RuntimeError(f"Memgraph is unavailable at {settings.get('host', '')}:{settings.get('port', 0)}")
        report = verify_schema(connection, SCHEMA_FILE, arguments.deployment)
        print(report)
        sys_exit(0 if report.get("valid", False) else 1)
    except Exception as error:
        print({"valid": False, "error": str(error)})
        sys_exit(1)
    finally:
        connection.disconnect()


if __name__ == "__main__":
    main()
