"""Compare bounded no-seed MCP conversations with MemGraph disabled and enabled."""

from argparse import ArgumentParser as argparse_ArgumentParser
from asyncio import run as asyncio_run
from datetime import UTC, datetime
from hashlib import sha256 as hashlib_sha256
from json import dumps as json_dumps, loads as json_loads
from pathlib import Path
from sys import path as sys_path
from tempfile import TemporaryDirectory as tempfile_TemporaryDirectory
from time import perf_counter as time_perf_counter, perf_counter_ns as time_perf_counter_ns

REPOSITORY = Path(__file__).resolve().parents[1]
if str(REPOSITORY) not in sys_path:
    sys_path.insert(0, str(REPOSITORY))

from mcp.client import Client

from engram.config import load_config
from engram.constants import VERSION
from engram.core import Engram
from engram.mcp_server import EngramMCPServer, MCPConversationService
from engram.service import EngramCore
from scripts.graph_probe_config import materialize_engram_graph_config

DEFAULT_OUTPUT = Path("eval/results/contextual/mcp-memgraph-comparison-2026-08-20.json")
PROMPTS = (
    "What is Elias Throrne?",
    "What was Elias Throrne classified as?",
    "What does Elias Throrne do?",
    "Tell me about Elias Throrne.",
    "What does evaluating mapping[key] for an absent key result in?",
)


def tool_json(result) -> dict:
    if result.is_error or len(result.content) != 1 or result.content[0].type != "text":
        raise RuntimeError("MCP tool returned an error or unexpected content shape")
    value = json_loads(result.content[0].text)
    if not isinstance(value, dict):
        raise RuntimeError("MCP tool result must be a JSON object")
    return value


def turn_summary(turn: dict, latency_ms: float) -> dict:
    response = turn.get("response", "")
    if not isinstance(response, str):
        raise RuntimeError("MCP turn response must be a string")
    result = {
        "turn": turn.get("turn", 0),
        "source": turn.get("source", ""),
        "response": response,
        "response_bytes": len(response.encode("utf-8")),
        "response_sha256": hashlib_sha256(response.encode("utf-8")).hexdigest(),
        "latency_ms": round(latency_ms, 4),
    }
    return result


def timed(callable_value):
    started = time_perf_counter_ns()
    value = callable_value()
    result = (value, round((time_perf_counter_ns() - started) / 1_000_000, 4))
    return result


def relation_probe(config_path: str) -> dict:
    """Exercise the Section 8 fixed capabilities and report live turn lengths."""
    engine = Engram(load_config(config_path))
    try:
        entities, entity_latency = timed(lambda: engine.canonical_entity_matches("Elias Throrne", limit=8))
        predicates, predicate_latency = timed(lambda: engine.canonical_predicate_matches("is a", limit=8))
        relation_rows = []
        relation_latency = 0.0
        if len(entities) == 1 and len(predicates) == 1:
            relation_rows, relation_latency = timed(
                lambda: engine.relation_one_hop_proposition_projections(
                    entities[0]["canonical_id"], predicates[0]["canonical_id"], row_limit=10
                )
            )
        core = EngramCore(engine)
        resolution, resolution_latency = timed(
            lambda: core.resolve_request(
                "What was Elias Throrne classified as?",
                "live-memgraph-relation-probe",
                user_id="Graph Comparison",
                configured_resolvers=("structured_graph",),
            )
        )
        structured = next(item for item in resolution["resolver_results"] if item["resolver"] == "structured_graph")
        result = {
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
                        "proposition_id": item["projection"]["proposition_id"],
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
                "proposition_evidence": resolution["evidence_package"]["retained_count"],
                "structured_graph": {
                    "state": structured["state"].value,
                    "reason_code": structured["reason_code"],
                    "elapsed_ns": structured["consumption"]["elapsed_ns"],
                    "exhausted_dimensions": list(structured["consumption"]["exhausted_dimensions"]),
                },
            },
        }
        return result
    finally:
        client = engine.graph_client
        disconnect = getattr(client, "disconnect", False)
        if callable(disconnect):
            disconnect()


async def internal_conversation(config_path: str) -> dict:
    server = EngramMCPServer(service=MCPConversationService(static_pairs=[], require_catch_all=False))
    turns = []
    started_clock = time_perf_counter()
    async with Client(server) as client:
        started = tool_json(
            await client.call_tool(
                "engram_start",
                {
                    "user_id": "Graph Comparison",
                    "initial_bot_text": ".",
                    "config_path": config_path,
                    "random_seed": 808,
                    "random_seed_present": True,
                },
            )
        )
        if started.get("turn_count") != 0:
            raise RuntimeError("MCP conversation did not start at turn zero")
        for prompt in PROMPTS:
            turn_started = time_perf_counter_ns()
            turn = tool_json(await client.call_tool("engram_send", {"text": prompt}))
            latency_ms = (time_perf_counter_ns() - turn_started) / 1_000_000
            turns.append(turn_summary(turn, latency_ms))
        inspected = tool_json(await client.call_tool("engram_inspect", {}))
        stopped = tool_json(await client.call_tool("engram_stop", {}))
    components = inspected.get("core_status", {}).get("components", {})
    graph = components.get("graph", {}) if isinstance(components, dict) else {}
    result = {
        "config_path_supplied": bool(config_path),
        "graph_enabled": graph.get("enabled", False),
        "graph_ready": graph.get("ready", False),
        "duration_seconds": round(time_perf_counter() - started_clock, 6),
        "turns": turns,
        "stop_summary": stopped.get("summary", {}),
    }
    return result


async def compare(config_path: str) -> dict:
    disabled = await internal_conversation("")
    enabled = await internal_conversation(config_path)
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
    result = {
        "schema_version": 1,
        "created_at": datetime.now(UTC).isoformat(),
        "engram_version": VERSION,
        "transport": "official MCP Client against repository MCPServer",
        "seed_enabled": False,
        "user_id": "Graph Comparison",
        "disabled": disabled,
        "enabled": enabled,
        "section8_relation_probe": relation_probe(config_path),
        "comparison": {
            "turns": len(differences),
            "response_changed_turns": sum(item["response_changed"] for item in differences),
            "source_changed_turns": sum(item["source_changed"] for item in differences),
            "differences": differences,
        },
    }
    return result


def main() -> int:
    parser = argparse_ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="config.yml")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    with tempfile_TemporaryDirectory(prefix="engram-graph-probe-") as temporary_directory:
        selected_config = materialize_engram_graph_config(
            args.config,
            Path(temporary_directory) / "engram-graph.yml",
        )
        report = asyncio_run(compare(selected_config))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json_dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
