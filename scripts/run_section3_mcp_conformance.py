"""Run the Section 3 long-conversation gate through the MCP protocol."""

import argparse
import asyncio
import json
import platform
import sys
import time
from collections import Counter
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

REPOSITORY = Path(__file__).resolve().parents[1]
if str(REPOSITORY) not in sys.path:
    sys.path.insert(0, str(REPOSITORY))

from mcp.client import Client

from engram.constants import VERSION
from engram.mcp_server import create_mcp_server

DEFAULT_OUTPUT = REPOSITORY / "documentation" / "artifacts" / "mcp-conversation-1000-turns-2026-08-12.json"
MINIMUM_CONFORMANCE_TURNS = 1_000
MESSAGES = (
    "hello",
    "How are you?",
    "What can you remember?",
    "Tell me more.",
    "Why?",
    "Where are we?",
    "When is now?",
    "Who are you?",
    "What did I say?",
    "Continue.",
)


def _tool_json(result) -> dict:
    if result.is_error:
        raise RuntimeError(f"MCP tool returned an error: {result.content}")
    if len(result.content) != 1 or result.content[0].type != "text":
        raise RuntimeError("MCP tool returned an unexpected content shape")
    value = json.loads(result.content[0].text)
    if not isinstance(value, dict):
        raise RuntimeError("MCP tool result must be a JSON object")
    return value


def _percentile(samples: list[float], fraction: float) -> float:
    ordered = sorted(samples)
    index = min(len(ordered) - 1, max(0, int(round((len(ordered) - 1) * fraction))))
    return ordered[index]


async def _run(turns: int) -> dict:
    server = create_mcp_server()
    latencies_ms = []
    sources: Counter[str] = Counter()
    response_count = 0
    started_at = datetime.now(UTC)
    started_clock = time.perf_counter()

    async with Client(server) as client:
        if not client.server_info:
            raise RuntimeError("MCP initialization did not return server information")
        server_name = client.server_info.name
        server_version = client.server_info.version
        started = _tool_json(
            await client.call_tool(
                "engram_start",
                {
                    "user_id": "Section 3 MCP Conformance",
                    "initial_bot_text": ".",
                    "seed_path": str(REPOSITORY / "data" / "seed.json"),
                    "random_seed": 315,
                    "random_seed_present": True,
                },
            )
        )
        if started["turn_count"] != 0:
            raise RuntimeError("new MCP conversation did not start at turn zero")

        first_turn = {}
        last_turn = {}
        for index in range(turns):
            call_started = time.perf_counter_ns()
            result = _tool_json(await client.call_tool("engram_send", {"text": MESSAGES[index % len(MESSAGES)]}))
            latencies_ms.append((time.perf_counter_ns() - call_started) / 1_000_000)
            expected_turn = index + 1
            if result.get("turn") != expected_turn:
                raise RuntimeError(f"MCP turn sequence mismatch: expected {expected_turn}, got {result.get('turn')}")
            if not isinstance(result.get("response"), str) or not result["response"]:
                raise RuntimeError(f"MCP turn {expected_turn} returned no response")
            response_count += 1
            sources[str(result.get("source", ""))] += 1
            bounded_turn = {
                "turn": result["turn"],
                "source": result.get("source", ""),
                "response_present": bool(result["response"]),
            }
            if index == 0:
                first_turn = bounded_turn
            last_turn = bounded_turn
            if expected_turn % 100 == 0:
                print(f"completed {expected_turn}/{turns} MCP conversation turns", flush=True)

        inspected = _tool_json(await client.call_tool("engram_inspect", {}))
        if inspected.get("turn_count") != turns:
            raise RuntimeError(f"MCP inspection reported {inspected.get('turn_count')} turns instead of {turns}")
        stopped = _tool_json(await client.call_tool("engram_stop", {}))
        if stopped.get("summary", {}).get("exchanges") != turns:
            raise RuntimeError("MCP stop report did not preserve the complete exchange count")

    finished_at = datetime.now(UTC)
    duration_seconds = time.perf_counter() - started_clock
    return {
        "gate": "EGR-315 MCP long-conversation conformance",
        "passed": response_count == turns,
        "minimum_required_turns": MINIMUM_CONFORMANCE_TURNS,
        "requested_turns": turns,
        "completed_turns": response_count,
        "mcp_tool_calls": turns + 3,
        "transport": "official MCP Client against repository MCPServer",
        "server": {"name": server_name, "version": server_version},
        "engram_version": VERSION,
        "python": platform.python_version(),
        "started_at": started_at.isoformat(),
        "finished_at": finished_at.isoformat(),
        "duration_seconds": round(duration_seconds, 6),
        "turn_latency_ms": {
            "minimum": round(min(latencies_ms), 6),
            "p50": round(_percentile(latencies_ms, 0.50), 6),
            "p95": round(_percentile(latencies_ms, 0.95), 6),
            "maximum": round(max(latencies_ms), 6),
        },
        "sources": dict(sorted(sources.items())),
        "first_turn": first_turn,
        "last_turn": last_turn,
        "inspect": {
            "user_id": inspected.get("user_id"),
            "turn_count": inspected.get("turn_count"),
            "history_size": inspected.get("session", {}).get("history_size"),
        },
        "stop_summary": stopped.get("summary", {}),
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--turns", type=int, default=MINIMUM_CONFORMANCE_TURNS)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    return parser


def main(argv: Sequence[str] = ()) -> int:
    args = _parser().parse_args(argv)
    if args.turns < MINIMUM_CONFORMANCE_TURNS:
        raise ValueError(f"Section 3 conformance requires at least {MINIMUM_CONFORMANCE_TURNS} turns")
    result = asyncio.run(_run(args.turns))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
