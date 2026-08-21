"""Compare bounded no-seed MCP conversations with MemGraph disabled and enabled."""

import argparse
import asyncio
import hashlib
import json
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

REPOSITORY = Path(__file__).resolve().parents[1]
if str(REPOSITORY) not in sys.path:
    sys.path.insert(0, str(REPOSITORY))

from mcp.client import Client

from engram.config import load_config
from engram.constants import VERSION
from engram.core import Engram
from engram.mcp_server import create_mcp_server
from engram.service import EngramCore

DEFAULT_OUTPUT = REPOSITORY / "documentation" / "contextual" / "mcp-memgraph-comparison-2026-08-20.json"
PROMPTS = (
    "Tell me about Sarah.",
    "Who is Sarah married to?",
    "What work is Sarah present in?",
    "Tell me about Abraham.",
    "Who is Abraham married to?",
)


def _tool_json(result) -> dict:
    if result.is_error or len(result.content) != 1 or result.content[0].type != "text":
        raise RuntimeError("MCP tool returned an error or unexpected content shape")
    value = json.loads(result.content[0].text)
    if not isinstance(value, dict):
        raise RuntimeError("MCP tool result must be a JSON object")
    return value


def _turn_summary(turn: dict, latency_ms: float) -> dict:
    response = turn.get("response", "")
    if not isinstance(response, str):
        raise RuntimeError("MCP turn response must be a string")
    return {
        "turn": turn.get("turn", 0),
        "source": turn.get("source", ""),
        "response": response,
        "response_bytes": len(response.encode("utf-8")),
        "response_sha256": hashlib.sha256(response.encode("utf-8")).hexdigest(),
        "latency_ms": round(latency_ms, 4),
    }


def _timed(callable_value):
    started = time.perf_counter_ns()
    value = callable_value()
    return value, round((time.perf_counter_ns() - started) / 1_000_000, 4)


def _relation_probe(config_path: str) -> dict:
    """Exercise the Section 8 fixed capabilities and report live turn lengths."""
    engine = Engram(load_config(config_path))
    try:
        entities, entity_latency = _timed(lambda: engine.canonical_entity_matches("Sarah", limit=8))
        predicates, predicate_latency = _timed(lambda: engine.canonical_predicate_matches("married", limit=8))
        relation_rows = []
        relation_latency = 0.0
        if len(entities) == 1 and len(predicates) == 1:
            relation_rows, relation_latency = _timed(
                lambda: engine.relation_one_hop_claim_projections(
                    entities[0]["canonical_id"], predicates[0]["canonical_id"], row_limit=10
                )
            )
        core = EngramCore(engine, checkpoint_on_mutation=False)
        resolution, resolution_latency = _timed(
            lambda: core.resolve_request(
                "Who is Sarah married to?",
                "live-memgraph-relation-probe",
                user_id="Sarah",
                configured_resolvers=("structured_graph",),
            )
        )
        structured = next(item for item in resolution["resolver_results"] if item["resolver"] == "structured_graph")
        return {
            "canonical_entity": {
                "latency_ms": entity_latency,
                "matches": [
                    {
                        "canonical_id": item["canonical_id"],
                        "primary_label": item["primary_label"],
                        "entity_type": item["entity_type"].value,
                    }
                    for item in entities
                ],
            },
            "canonical_predicate": {
                "latency_ms": predicate_latency,
                "matches": [
                    {
                        "canonical_id": item["canonical_id"],
                        "primary_label": item["primary_label"],
                        "object_type": item["object_type"].value,
                    }
                    for item in predicates
                ],
            },
            "one_hop": {
                "latency_ms": relation_latency,
                "matches": [
                    {
                        "claim_id": item["projection"]["claim_id"],
                        "object_label": item["object_label"],
                        "object_type": item["object_type"].value,
                    }
                    for item in relation_rows
                ],
            },
            "default_core_resolution": {
                "latency_ms": resolution_latency,
                "outcome": resolution["outcome"].value,
                "reason_codes": list(resolution["reason_codes"]),
                "response_candidates": [item["response"] for item in resolution["response_candidates"]],
                "claim_evidence": resolution["evidence_package"]["retained_count"],
                "structured_graph": {
                    "state": structured["state"].value,
                    "reason_code": structured["reason_code"],
                    "elapsed_ns": structured["consumption"]["elapsed_ns"],
                    "exhausted_dimensions": list(structured["consumption"]["exhausted_dimensions"]),
                },
            },
        }
    finally:
        client = engine.graph_client
        disconnect = getattr(client, "disconnect", None)
        if callable(disconnect):
            disconnect()


async def _conversation(config_path: str) -> dict:
    server = create_mcp_server()
    turns = []
    started_clock = time.perf_counter()
    async with Client(server) as client:
        started = _tool_json(
            await client.call_tool(
                "engram_start",
                {
                    "user_id": "Sarah",
                    "initial_bot_text": ".",
                    "seed_path": "",
                    "config_path": config_path,
                    "random_seed": 808,
                    "random_seed_present": True,
                },
            )
        )
        if started.get("turn_count") != 0:
            raise RuntimeError("MCP conversation did not start at turn zero")
        for prompt in PROMPTS:
            turn_started = time.perf_counter_ns()
            turn = _tool_json(await client.call_tool("engram_send", {"text": prompt}))
            latency_ms = (time.perf_counter_ns() - turn_started) / 1_000_000
            turns.append(_turn_summary(turn, latency_ms))
        inspected = _tool_json(await client.call_tool("engram_inspect", {}))
        stopped = _tool_json(await client.call_tool("engram_stop", {}))
    components = inspected.get("core_status", {}).get("components", {})
    graph = components.get("graph", {}) if isinstance(components, dict) else {}
    return {
        "config_path_supplied": bool(config_path),
        "graph_enabled": graph.get("enabled", False),
        "graph_ready": graph.get("ready", False),
        "duration_seconds": round(time.perf_counter() - started_clock, 6),
        "turns": turns,
        "stop_summary": stopped.get("summary", {}),
    }


async def compare(config_path: str) -> dict:
    disabled = await _conversation("")
    enabled = await _conversation(config_path)
    differences = []
    for index, (disabled_turn, enabled_turn) in enumerate(zip(disabled["turns"], enabled["turns"], strict=True)):
        differences.append(
            {
                "turn": index + 1,
                "prompt": PROMPTS[index],
                "response_changed": disabled_turn["response_sha256"] != enabled_turn["response_sha256"],
                "source_changed": disabled_turn["source"] != enabled_turn["source"],
                "disabled": disabled_turn,
                "enabled": enabled_turn,
            }
        )
    return {
        "schema_version": 1,
        "created_at": datetime.now(UTC).isoformat(),
        "engram_version": VERSION,
        "transport": "official MCP Client against repository MCPServer",
        "seed_enabled": False,
        "user_id": "Sarah",
        "disabled": disabled,
        "enabled": enabled,
        "section8_relation_probe": _relation_probe(config_path),
        "comparison": {
            "turns": len(differences),
            "response_changed_turns": sum(item["response_changed"] for item in differences),
            "source_changed_turns": sum(item["source_changed"] for item in differences),
            "differences": differences,
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="config.yml")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    report = asyncio.run(compare(args.config))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
