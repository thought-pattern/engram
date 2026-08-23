"""Run the Section 3 long-conversation gate through the MCP protocol."""

import argparse
import asyncio
import hashlib
import json
import math
import platform
import sys
import tempfile
import time
from collections import Counter
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

import yaml

REPOSITORY = Path(__file__).resolve().parents[1]
if str(REPOSITORY) not in sys.path:
    sys.path.insert(0, str(REPOSITORY))

from mcp.client import Client

from engram.constants import (
    MCP_CONFORMANCE_MESSAGES,
    MCP_CONFORMANCE_MINIMUM_TURNS,
    MCP_TURN_EVALUATION_CHECKS,
    MCP_TURN_EVENT_FIELDS,
    VERSION,
)
from engram.mcp_server import create_mcp_server
from scripts.benchmark_metadata import benchmark_source_state

DEFAULT_OUTPUT = REPOSITORY / "documentation" / "artifacts" / "mcp-conversation-1000-turns-2026-08-12.json"
SARAH_SUSHI_MESSAGES = (
    "Sushi is good.",
    "What's good?",
    "Tell me more.",
    "Why?",
    "What's good?",
    "Where are we?",
    "Continue.",
    "What did I say?",
    "What's good?",
    "How are you?",
)
SARAH_PREFERENCE_MESSAGES = (
    "I like sushi.",
    "I like cats.",
    "I dislike dogs.",
    "What food do I like?",
    "What animals do I like?",
    "What animals do I dislike?",
    "Please remind me what food I like.",
    "Tell me which animals I like.",
    "Tell me which animals I dislike.",
    "What preferences did I share?",
)
SARAH_PREFERENCE_EXPECTATIONS = {
    "I like sushi.": (),
    "I like cats.": (),
    "I dislike dogs.": (),
    "What food do I like?": ("sushi",),
    "What animals do I like?": ("cat",),
    "What animals do I dislike?": ("dog", "dislike"),
    "Please remind me what food I like.": ("sushi",),
    "Tell me which animals I like.": ("cat",),
    "Tell me which animals I dislike.": ("dog", "dislike"),
    "What preferences did I share?": ("sushi", "cat", "dog"),
}


def _tool_json(result) -> dict:
    if result.is_error:
        raise RuntimeError(f"MCP tool returned an error: {result.content}")
    if len(result.content) != 1 or result.content[0].type != "text":
        raise RuntimeError("MCP tool returned an unexpected content shape")
    value = json.loads(result.content[0].text)
    if not isinstance(value, dict):
        raise RuntimeError("MCP tool result must be a JSON object")
    tool_value = value
    return tool_value


def _percentile(samples: list[float], fraction: float) -> float:
    ordered = sorted(samples)
    index = min(len(ordered) - 1, max(0, int(round((len(ordered) - 1) * fraction))))
    percentile = ordered[index]
    return percentile


def _evaluate_turn(result: dict, expected_turn: int, expected_input: str, expected_user_id: str, latency_ms: float) -> dict:
    """Evaluate one complete observed MCP turn without retaining conversation text."""
    score = result.get("score")
    elapsed_seconds = result.get("elapsed_seconds")
    response = result.get("response")
    source = result.get("source")
    checks = {
        "exact_fields": set(result) == MCP_TURN_EVENT_FIELDS,
        "turn_sequence": result.get("turn") == expected_turn,
        "input_continuity": result.get("input") == expected_input,
        "user_continuity": result.get("user_id") == expected_user_id,
        "response_nonempty": isinstance(response, str) and bool(response),
        "source_nonempty": isinstance(source, str) and bool(source),
        "score_finite": (not isinstance(score, bool) and isinstance(score, (int, float)) and math.isfinite(float(score))),
        "pattern_string": isinstance(result.get("pattern"), str),
        "captured_list": isinstance(result.get("captured"), list),
        "dialogue_act_string": isinstance(result.get("dialogue_act"), str),
        "active_topic_string": isinstance(result.get("active_topic"), str),
        "entities_list": isinstance(result.get("entities"), list),
        "fact_admissions_list": isinstance(result.get("fact_admissions"), list),
        "elapsed_nonnegative": (
            not isinstance(elapsed_seconds, bool)
            and isinstance(elapsed_seconds, (int, float))
            and math.isfinite(float(elapsed_seconds))
            and float(elapsed_seconds) >= 0.0
        ),
        "context_changes_object": isinstance(result.get("context_changes"), dict),
        "learned_statements_list": isinstance(result.get("learned_statements"), list),
    }
    if set(checks) != MCP_TURN_EVALUATION_CHECKS:
        raise RuntimeError("MCP turn evaluation checks do not match the declared contract")
    failed_checks = [name for name, passed in checks.items() if not passed]
    response_text = response if isinstance(response, str) else ""
    source_text = source if isinstance(source, str) else ""
    evaluation = {
        "turn": expected_turn,
        "passed": not failed_checks,
        "failed_checks": failed_checks,
        "source": source_text,
        "response_bytes": len(response_text.encode("utf-8")),
        "response_sha256": hashlib.sha256(response_text.encode("utf-8")).hexdigest(),
        "latency_ms": round(latency_ms, 6),
    }
    return evaluation


def _run_length_encode_passes(evaluations: list[dict]) -> list[dict]:
    """Encode ordered per-turn pass/fail state without a transcription-prone bitmap."""
    runs = []
    for evaluation in evaluations:
        bit = "1" if evaluation["passed"] else "0"
        if runs and runs[-1]["bit"] == bit:
            runs[-1]["turns"] += 1
        else:
            runs.append({"bit": bit, "turns": 1})
    encoded_runs = runs
    return encoded_runs


async def _run(
    turns: int,
    gate: str = "EGR-315 MCP long-conversation conformance",
    user_id: str = "Section 3 MCP Conformance",
    profile: str = "standard",
    config_path: str = "",
    seed_path: str = "",
    memgraph_probe_every: int = 0,
    retrieval_rewrites_enabled: bool = False,
    sparse_enabled: bool = False,
    semantic_enabled: bool = False,
    reranker_enabled: bool = False,
    utility_enabled: bool = False,
) -> dict:
    server = create_mcp_server()
    latencies_ms = []
    sources: Counter[str] = Counter()
    response_count = 0
    evaluations = []
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
                    "user_id": user_id,
                    "initial_bot_text": ".",
                    "seed_path": seed_path or str(REPOSITORY / "data" / "seed.json"),
                    "config_path": config_path,
                    "random_seed": 315,
                    "random_seed_present": True,
                },
            )
        )
        if started["turn_count"] != 0:
            raise RuntimeError("new MCP conversation did not start at turn zero")

        first_turn = {}
        last_turn = {}
        messages = (
            SARAH_SUSHI_MESSAGES
            if profile == "sarah-sushi"
            else SARAH_PREFERENCE_MESSAGES
            if profile == "sarah-preferences"
            else MCP_CONFORMANCE_MESSAGES
        )
        for index in range(turns):
            call_started = time.perf_counter_ns()
            expected_turn = index + 1
            graph_probe = bool(memgraph_probe_every and expected_turn % memgraph_probe_every == 0)
            message = "Who is Sarah married to?" if graph_probe else messages[index % len(messages)]
            result = _tool_json(await client.call_tool("engram_send", {"text": message}))
            latency_ms = (time.perf_counter_ns() - call_started) / 1_000_000
            latencies_ms.append(latency_ms)
            evaluation = _evaluate_turn(result, expected_turn, message, user_id, latency_ms)
            if profile == "sarah-sushi":
                response = result.get("response", "")
                if graph_probe:
                    profile_check = "memgraph_source_when_asked"
                    profile_passed = result.get("source") == "graph"
                else:
                    profile_check = "sushi_recall_when_asked"
                    profile_passed = (
                        isinstance(response, str) and "sushi" in response.casefold() if message == "What's good?" else True
                    )
                evaluation["profile_check"] = profile_check
                evaluation["profile_passed"] = profile_passed
                if not profile_passed:
                    evaluation["passed"] = False
                    evaluation["failed_checks"].append(profile_check)
            elif profile == "sarah-preferences":
                response = result.get("response", "")
                if graph_probe:
                    profile_check = "memgraph_source_when_asked"
                    profile_passed = result.get("source") == "graph"
                else:
                    expected_terms = SARAH_PREFERENCE_EXPECTATIONS[message]
                    response_text = response.casefold() if isinstance(response, str) else ""
                    profile_check = "sarah_preference_continuity"
                    profile_passed = result.get("user_id") == "Sarah" and all(term in response_text for term in expected_terms)
                evaluation["profile_check"] = profile_check
                evaluation["profile_passed"] = profile_passed
                if not profile_passed:
                    evaluation["passed"] = False
                    evaluation["failed_checks"].append(profile_check)
            evaluations.append(evaluation)
            if evaluation["passed"]:
                response_count += 1
            sources[str(result.get("source", ""))] += 1
            bounded_turn = {
                "turn": result.get("turn", 0),
                "source": result.get("source", ""),
                "response_present": bool(result.get("response", "")),
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
    failure_counts: Counter[str] = Counter(failure for evaluation in evaluations for failure in evaluation["failed_checks"])
    failed_turns = [evaluation["turn"] for evaluation in evaluations if not evaluation["passed"]]
    evaluation_bytes = json.dumps(evaluations, sort_keys=True, separators=(",", ":")).encode("utf-8")
    response_sequence = [evaluation["response_sha256"] for evaluation in evaluations]
    observation_sequence = [
        {
            "turn": evaluation["turn"],
            "source": evaluation["source"],
            "response_bytes": evaluation["response_bytes"],
            "response_sha256": evaluation["response_sha256"],
            "profile_passed": evaluation.get("profile_passed", True),
        }
        for evaluation in evaluations
    ]
    response_sequence_bytes = json.dumps(response_sequence, separators=(",", ":")).encode("utf-8")
    observation_sequence_bytes = json.dumps(observation_sequence, sort_keys=True, separators=(",", ":")).encode("utf-8")
    components = inspected.get("core_status", {}).get("components", {})
    graph_status = components.get("graph", {}) if isinstance(components, dict) else {}
    semantic_status = components.get("semantic", {}) if isinstance(components, dict) else {}
    reranker_status = components.get("reranker", {}) if isinstance(components, dict) else {}
    utility_status = components.get("utility", {}) if isinstance(components, dict) else {}
    optional_components_ready = (
        (not semantic_enabled or semantic_status.get("ready") is True)
        and (not reranker_enabled or reranker_status.get("ready") is True)
        and (not utility_enabled or utility_status.get("ready") is True)
    )
    run_result = {
        "gate": gate,
        "passed": response_count == turns and not failed_turns and optional_components_ready,
        "minimum_required_turns": MCP_CONFORMANCE_MINIMUM_TURNS,
        "requested_turns": turns,
        "completed_turns": len(evaluations),
        "evaluated_turns": len(evaluations),
        "mcp_tool_calls": turns + 3,
        "transport": "official MCP Client against repository MCPServer",
        "conversation_profile": profile,
        "configuration": {
            "config_path_supplied": bool(config_path),
            "seed_path": seed_path or str(REPOSITORY / "data" / "seed.json"),
            "graph_enabled": graph_status.get("enabled", False),
            "graph_ready": graph_status.get("ready", False),
            "memgraph_probe_every": memgraph_probe_every,
            "retrieval_rewrites_enabled": retrieval_rewrites_enabled,
            "sparse_enabled": sparse_enabled,
            "semantic_enabled": semantic_enabled,
            "semantic_ready": semantic_status.get("ready", False),
            "semantic_model_version": semantic_status.get("artifact_identity", {}).get("model_version", ""),
            "semantic_record_count": semantic_status.get("record_count", 0),
            "reranker_enabled": reranker_enabled,
            "reranker_ready": reranker_status.get("ready", False),
            "reranker_model_version": reranker_status.get("model_version", ""),
            "utility_enabled": utility_enabled,
            "utility_ready": utility_status.get("ready", False),
            "utility_contract_version": utility_status.get("contract_version", ""),
            "utility_plugins": utility_status.get("plugins", {}),
        },
        "server": {"name": server_name, "version": server_version},
        "engram_version": VERSION,
        "python": platform.python_version(),
        "source_state": benchmark_source_state(),
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
        "turn_evaluation": {
            "checks_per_turn": len(MCP_TURN_EVALUATION_CHECKS) + int(profile in {"sarah-sushi", "sarah-preferences"}),
            "check_names": sorted(
                {
                    *MCP_TURN_EVALUATION_CHECKS,
                    *({"sushi_recall_when_asked"} if profile == "sarah-sushi" else set()),
                    *({"sarah_preference_continuity"} if profile == "sarah-preferences" else set()),
                    *({"memgraph_source_when_asked"} if memgraph_probe_every else set()),
                }
            ),
            "pass_runs_encoding": "ordered runs; bit 1 means every check passed and bit 0 means at least one failed",
            "pass_runs": _run_length_encode_passes(evaluations),
            "passed_turns": response_count,
            "failed_turns": failed_turns,
            "failure_counts": dict(sorted(failure_counts.items())),
            "detailed_evaluation_sha256": hashlib.sha256(evaluation_bytes).hexdigest(),
            "response_sequence_sha256": hashlib.sha256(response_sequence_bytes).hexdigest(),
            "observation_sequence_sha256": hashlib.sha256(observation_sequence_bytes).hexdigest(),
            "first_evaluation": evaluations[0] if evaluations else {},
            "last_evaluation": evaluations[-1] if evaluations else {},
        },
        "inspect": {
            "user_id": inspected.get("user_id"),
            "turn_count": inspected.get("turn_count"),
            "history_size": inspected.get("session", {}).get("history_size"),
        },
        "profile_definition": (
            {"user_id": "Sarah", "likes": ["sushi", "cats"], "dislikes": ["dogs"]} if profile == "sarah-preferences" else {}
        ),
        "stop_summary": stopped.get("summary", {}),
    }
    return run_result


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--turns", type=int, default=MCP_CONFORMANCE_MINIMUM_TURNS)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--gate", default="EGR-315 MCP long-conversation conformance")
    parser.add_argument("--user-id", default="Section 3 MCP Conformance")
    parser.add_argument("--profile", choices=("standard", "sarah-sushi", "sarah-preferences"), default="standard")
    parser.add_argument("--config", default="", help="Configuration path passed to the MCP engram_start tool")
    parser.add_argument("--seed", default="", help="Seed path passed to the MCP engram_start tool")
    parser.add_argument(
        "--enable-rewrites",
        action="store_true",
        help="Create an ephemeral runtime config with retrieval_rewrites_enabled=true",
    )
    parser.add_argument(
        "--enable-sparse",
        action="store_true",
        help="Create an ephemeral runtime config with sparse.enabled=true",
    )
    parser.add_argument(
        "--enable-semantic",
        action="store_true",
        help="Create an ephemeral runtime config with a checksum-gated standalone semantic model",
    )
    parser.add_argument(
        "--semantic-manifest",
        default="data/artifacts/models/all-MiniLM-L6-v2-826711e5.engram-model.json",
        help="Local provisioning manifest used with --enable-semantic",
    )
    parser.add_argument(
        "--enable-reranker",
        action="store_true",
        help="Create an ephemeral runtime config with the transparent reranker enabled",
    )
    parser.add_argument(
        "--enable-utility",
        action="store_true",
        help="Create an ephemeral runtime config with all fixed utility plugins enabled",
    )
    parser.add_argument(
        "--memgraph-probe-every",
        type=int,
        default=0,
        help="Replace every Nth profile message with a graph query and require a graph-sourced response",
    )
    parser.add_argument("--stdout", action="store_true")
    parser_value = parser
    return parser_value


def main(argv: Sequence[str] = ()) -> int:
    args = _parser().parse_args(argv)
    if args.turns < MCP_CONFORMANCE_MINIMUM_TURNS:
        raise ValueError(f"MCP conformance requires at least {MCP_CONFORMANCE_MINIMUM_TURNS} turns")
    if args.memgraph_probe_every < 0:
        raise ValueError("--memgraph-probe-every must be nonnegative")
    selected_config = args.config
    with tempfile.TemporaryDirectory(prefix="engram-mcp-conformance-") as temporary_directory:
        if args.enable_rewrites or args.enable_sparse or args.enable_semantic or args.enable_reranker or args.enable_utility:
            raw_config = {}
            if args.config:
                loaded = yaml.safe_load(Path(args.config).read_text(encoding="utf-8"))
                if loaded:
                    if not isinstance(loaded, dict):
                        raise ValueError("--config must contain a YAML object")
                    raw_config = loaded
            if args.enable_rewrites:
                raw_config["retrieval_rewrites_enabled"] = True
            if args.enable_sparse:
                raw_config["sparse"] = {"enabled": True}
            if args.enable_semantic:
                manifest = json.loads(Path(args.semantic_manifest).read_text(encoding="utf-8"))
                required_manifest_fields = {
                    "model_path",
                    "model_id",
                    "model_version",
                    "license_id",
                    "artifact_sha256",
                    "dimension",
                    "backend",
                }
                if not isinstance(manifest, dict) or not required_manifest_fields.issubset(manifest):
                    raise ValueError("--semantic-manifest is malformed")
                raw_config["semantic"] = {
                    "enabled": True,
                    **{name: manifest[name] for name in sorted(required_manifest_fields)},
                }
            if args.enable_reranker:
                raw_config["reranker"] = {"enabled": True}
            if args.enable_utility:
                raw_config["utility"] = {"enabled": True}
            selected_path = Path(temporary_directory) / "config.yml"
            selected_path.write_text(yaml.safe_dump(raw_config, sort_keys=True), encoding="utf-8")
            selected_config = str(selected_path)
        result = asyncio.run(
            _run(
                args.turns,
                args.gate,
                args.user_id,
                args.profile,
                selected_config,
                args.seed,
                args.memgraph_probe_every,
                args.enable_rewrites,
                args.enable_sparse,
                args.enable_semantic,
                args.enable_reranker,
                args.enable_utility,
            )
        )
    result_text = json.dumps(result, indent=2, sort_keys=True) + "\n"
    if args.stdout:
        print(result_text, end="")
    else:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(result_text, encoding="utf-8")
        print(args.output)
    exit_code = 0 if result["passed"] else 1
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
