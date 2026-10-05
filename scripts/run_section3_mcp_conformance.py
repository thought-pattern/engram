"""Run the Section 3 long-conversation gate through the MCP protocol."""

from argparse import ArgumentParser as argparse_ArgumentParser
from asyncio import run as asyncio_run
from collections import Counter
from datetime import UTC, datetime
from hashlib import sha256 as hashlib_sha256
from json import dumps as json_dumps, loads as json_loads
from math import isfinite as math_isfinite
from pathlib import Path
from platform import python_version as platform_python_version
from sys import argv as sys_argv, path as sys_path
from tempfile import TemporaryDirectory as tempfile_TemporaryDirectory
from time import perf_counter as time_perf_counter, perf_counter_ns as time_perf_counter_ns

from yaml import safe_dump as yaml_safe_dump, safe_load as yaml_safe_load

REPOSITORY = Path(__file__).resolve().parents[1]
if str(REPOSITORY) not in sys_path:
    sys_path.insert(0, str(REPOSITORY))

from mcp.client import Client

from engram.constants import (
    MCP_CONFORMANCE_MESSAGES,
    MCP_CONFORMANCE_MINIMUM_TURNS,
    MCP_TURN_EVALUATION_CHECKS,
    MCP_TURN_EVENT_FIELDS,
    VERSION,
)
from engram.conversation_seed import load_conversation_pairs
from engram.mcp_server import EngramMCPServer, MCPConversationService
from scripts.benchmark_metadata import benchmark_source_state

DEFAULT_OUTPUT = Path("eval/results/artifacts/mcp-conversation-1000-turns.json")
PREFERENCE_CONTINUITY_SEED = REPOSITORY / "eval" / "fixtures" / "preference-continuity-mcp-seed.json"
CONTEXT_RECALL_MESSAGES = (
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
PREFERENCE_CONTINUITY_MESSAGES = (
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
PREFERENCE_CONTINUITY_EXPECTATIONS = {
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
# Provisioning-manifest fields copied into the ephemeral semantic config, with their
# concrete types; every one is required before the copy.
SEMANTIC_MANIFEST_FIELD_DEFAULTS = {
    "artifact_sha256": "",
    "backend": "",
    "dimension": 0,
    "license_id": "",
    "model_id": "",
    "model_path": "",
    "model_version": "",
}


def tool_json(result) -> dict:
    if result.is_error:
        raise RuntimeError(f"MCP tool returned an error: {result.content}")
    if len(result.content) != 1 or result.content[0].type != "text":
        raise RuntimeError("MCP tool returned an unexpected content shape")
    value = json_loads(result.content[0].text)
    if not isinstance(value, dict):
        raise RuntimeError("MCP tool result must be a JSON object")
    tool_value = value
    return tool_value


def internal_percentile(samples: list[float], fraction: float) -> float:
    ordered = sorted(samples)
    index = min(len(ordered) - 1, max(0, int(round((len(ordered) - 1) * fraction))))
    percentile = ordered[index]
    return percentile


def evaluate_turn(result: dict, expected_turn: int, expected_input: str, expected_user_id: str, latency_ms: float) -> dict:
    """Evaluate one complete observed MCP turn without retaining conversation text."""
    score = result.get("score", 0.0)
    elapsed_seconds = result.get("elapsed_seconds", 0.0)
    response = result.get("response", "")
    source = result.get("source", "")
    checks = {
        "exact_fields": set(result) == MCP_TURN_EVENT_FIELDS,
        "turn_sequence": result.get("turn", 0) == expected_turn,
        "input_continuity": result.get("input", "") == expected_input,
        "user_continuity": result.get("user_id", "") == expected_user_id,
        "response_nonempty": isinstance(response, str) and bool(response),
        "source_nonempty": isinstance(source, str) and bool(source),
        "score_finite": (not isinstance(score, bool) and isinstance(score, (int, float)) and math_isfinite(float(score))),
        "pattern_string": isinstance(result.get("pattern", ""), str),
        "captured_list": isinstance(result.get("captured", ""), list),
        "dialogue_act_string": isinstance(result.get("dialogue_act", ""), str),
        "active_topic_string": isinstance(result.get("active_topic", ""), str),
        "entities_list": isinstance(result.get("entities", []), list),
        "fact_admissions_list": isinstance(result.get("fact_admissions", []), list),
        "elapsed_nonnegative": (
            not isinstance(elapsed_seconds, bool)
            and isinstance(elapsed_seconds, (int, float))
            and math_isfinite(float(elapsed_seconds))
            and float(elapsed_seconds) >= 0.0
        ),
        "context_changes_object": isinstance(result.get("context_changes", {}), dict),
        "learned_statements_list": isinstance(result.get("learned_statements", []), list),
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
        "response_sha256": hashlib_sha256(response_text.encode("utf-8")).hexdigest(),
        "latency_ms": round(latency_ms, 6),
    }
    return evaluation


def run_length_encode_passes(evaluations: list[dict]) -> list[dict]:
    """Encode ordered per-turn pass/fail state without a transcription-prone bitmap."""
    runs = []
    for evaluation in evaluations:
        bit = "1" if evaluation.get("passed", False) else "0"
        if runs and runs[-1].get("bit", "") == bit:
            runs[-1]["turns"] = runs[-1].get("turns", 0) + 1
        else:
            runs.append({"bit": bit, "turns": 1})
    encoded_runs = runs
    return encoded_runs


async def internal_run(
    turns: int,
    gate: str = "EGR-315 MCP long-conversation conformance",
    user_id: str = "Section 3 MCP Conformance",
    profile: str = "standard",
    config_path: str = "",
    memgraph_probe_every: int = 0,
) -> dict:
    service = ()
    if profile == "preference-continuity":
        service = MCPConversationService(static_pairs=load_conversation_pairs(str(PREFERENCE_CONTINUITY_SEED)))
    server = EngramMCPServer(service=service)
    latencies_ms = []
    sources = Counter()
    response_count = 0
    evaluations = []
    started_at = datetime.now(UTC)
    started_clock = time_perf_counter()

    async with Client(server) as client:
        if not client.server_info:
            raise RuntimeError("MCP initialization did not return server information")
        server_name = client.server_info.name
        server_version = client.server_info.version
        started = tool_json(
            await client.call_tool(
                "engram_start",
                {
                    "user_id": user_id,
                    "initial_bot_text": ".",
                    "config_path": config_path,
                    "random_seed": 315,
                    "random_seed_present": True,
                },
            )
        )
        if "turn_count" not in started or started.get("turn_count", 0) != 0:
            raise RuntimeError("new MCP conversation did not start at turn zero")

        first_turn = {}
        last_turn = {}
        messages = (
            CONTEXT_RECALL_MESSAGES
            if profile == "context-recall"
            else PREFERENCE_CONTINUITY_MESSAGES if profile == "preference-continuity" else MCP_CONFORMANCE_MESSAGES
        )
        for index in range(turns):
            call_started = time_perf_counter_ns()
            expected_turn = index + 1
            graph_probe = bool(memgraph_probe_every and expected_turn % memgraph_probe_every == 0)
            message = "What is Elias Thorne?" if graph_probe else messages[index % len(messages)]
            result = tool_json(await client.call_tool("engram_send", {"text": message}))
            latency_ms = (time_perf_counter_ns() - call_started) / 1_000_000
            latencies_ms.append(latency_ms)
            evaluation = evaluate_turn(result, expected_turn, message, user_id, latency_ms)
            response = result.get("response", "")
            # A requested graph probe is enforced here for every profile; a
            # profile's own check applies to its other turns.
            profile_check = ""
            profile_passed = True
            if graph_probe:
                profile_check = "memgraph_source_when_asked"
                profile_passed = result.get("source", "") == "graph"
            elif profile == "context-recall":
                profile_check = "sushi_recall_when_asked"
                profile_passed = isinstance(response, str) and "sushi" in response.casefold() if message == "What's good?" else True
            elif profile == "preference-continuity":
                expected_terms = PREFERENCE_CONTINUITY_EXPECTATIONS.get(message, ())
                response_text = response.casefold() if isinstance(response, str) else ""
                profile_check = "preference_continuity"
                # An absent user_id never matches, even for an empty requested user.
                user_matches = "user_id" in result and result.get("user_id", "") == user_id
                profile_passed = user_matches and all(term in response_text for term in expected_terms)
            if profile_check:
                evaluation["profile_check"] = profile_check
                evaluation["profile_passed"] = profile_passed
            if not profile_passed:
                evaluation["passed"] = False
                evaluation["failed_checks"] = [*evaluation.get("failed_checks", []), profile_check]
            evaluations.append(evaluation)
            if evaluation.get("passed", False):
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

        inspected = tool_json(await client.call_tool("engram_inspect", {}))
        # Absent counts fail these checks exactly as mismatched counts do.
        if "turn_count" not in inspected or inspected.get("turn_count", 0) != turns:
            raise RuntimeError(f"MCP inspection reported {inspected.get('turn_count', 0)} turns instead of {turns}")
        stopped = tool_json(await client.call_tool("engram_stop", {}))
        stop_summary = stopped.get("summary", {})
        if "exchanges" not in stop_summary or stop_summary.get("exchanges", 0) != turns:
            raise RuntimeError("MCP stop report did not preserve the complete exchange count")

    finished_at = datetime.now(UTC)
    duration_seconds = time_perf_counter() - started_clock
    failure_counts = Counter(failure for evaluation in evaluations for failure in evaluation.get("failed_checks", []))
    # Reported checks are derived from the checks each turn actually applied.
    additional_checks = Counter(
        evaluation.get("profile_check", "") for evaluation in evaluations if evaluation.get("profile_check", "")
    )
    failed_turns = [evaluation.get("turn", 0) for evaluation in evaluations if not evaluation.get("passed", False)]
    evaluation_bytes = json_dumps(evaluations, sort_keys=True, separators=(",", ":")).encode("utf-8")
    response_sequence = [evaluation.get("response_sha256", "") for evaluation in evaluations]
    observation_sequence = [
        {
            "turn": evaluation.get("turn", 0),
            "source": evaluation.get("source", ""),
            "response_bytes": evaluation.get("response_bytes", 0),
            "response_sha256": evaluation.get("response_sha256", ""),
            "profile_passed": evaluation.get("profile_passed", True),
        }
        for evaluation in evaluations
    ]
    response_sequence_bytes = json_dumps(response_sequence, separators=(",", ":")).encode("utf-8")
    observation_sequence_bytes = json_dumps(observation_sequence, sort_keys=True, separators=(",", ":")).encode("utf-8")
    components = inspected.get("core_status", {}).get("components", {})
    graph_status = components.get("graph", {}) if isinstance(components, dict) else {}
    sparse_status = components.get("sparse", {}) if isinstance(components, dict) else {}
    semantic_status = components.get("semantic", {}) if isinstance(components, dict) else {}
    reranker_status = components.get("reranker", {}) if isinstance(components, dict) else {}
    utility_status = components.get("utility", {}) if isinstance(components, dict) else {}
    sparse_active = sparse_status.get("enabled", False) is True
    semantic_active = semantic_status.get("enabled", False) is True
    reranker_active = reranker_status.get("enabled", False) is True
    utility_active = utility_status.get("enabled", False) is True
    configured_values = yaml_safe_load(Path(config_path).read_text(encoding="utf-8")) if config_path else {}
    retrieval_rewrites_active = configured_values.get("retrieval_rewrites_enabled", False) is True
    optional_components_ready = (
        (not sparse_active or sparse_status.get("ready", False) is True)
        and (not semantic_active or semantic_status.get("ready", False) is True)
        and (not reranker_active or reranker_status.get("ready", False) is True)
        and (not utility_active or utility_status.get("ready", False) is True)
        # Requested graph probes need a ready graph, whatever the profile.
        and (not memgraph_probe_every or graph_status.get("ready", False) is True)
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
            "graph_enabled": graph_status.get("enabled", False),
            "graph_ready": graph_status.get("ready", False),
            "memgraph_probe_every": memgraph_probe_every,
            "retrieval_rewrites_enabled": retrieval_rewrites_active,
            "sparse_enabled": sparse_active,
            "sparse_ready": sparse_status.get("ready", False),
            "semantic_enabled": semantic_active,
            "semantic_ready": semantic_status.get("ready", False),
            "semantic_model_version": semantic_status.get("artifact_identity", {}).get("model_version", ""),
            "semantic_record_count": semantic_status.get("record_count", 0),
            "reranker_enabled": reranker_active,
            "reranker_ready": reranker_status.get("ready", False),
            "reranker_model_version": reranker_status.get("model_version", ""),
            "utility_enabled": utility_active,
            "utility_ready": utility_status.get("ready", False),
            "utility_plugins": utility_status.get("plugins", {}),
        },
        "server": {"name": server_name, "version": server_version},
        "engram_version": VERSION,
        "python": platform_python_version(),
        "source_state": benchmark_source_state(),
        "started_at": started_at.isoformat(),
        "finished_at": finished_at.isoformat(),
        "duration_seconds": round(duration_seconds, 6),
        "turn_latency_ms": {
            "minimum": round(min(latencies_ms), 6),
            "p50": round(internal_percentile(latencies_ms, 0.50), 6),
            "p95": round(internal_percentile(latencies_ms, 0.95), 6),
            "maximum": round(max(latencies_ms), 6),
        },
        "sources": dict(sorted(sources.items())),
        "first_turn": first_turn,
        "last_turn": last_turn,
        "turn_evaluation": {
            "checks_per_turn": len(MCP_TURN_EVALUATION_CHECKS),
            "additional_check_turns": dict(sorted(additional_checks.items())),
            "check_names": sorted({*MCP_TURN_EVALUATION_CHECKS, *additional_checks}),
            "pass_runs_encoding": "ordered runs; bit 1 means every check passed and bit 0 means at least one failed",
            "pass_runs": run_length_encode_passes(evaluations),
            "passed_turns": response_count,
            "failed_turns": failed_turns,
            "failure_counts": dict(sorted(failure_counts.items())),
            "detailed_evaluation_sha256": hashlib_sha256(evaluation_bytes).hexdigest(),
            "response_sequence_sha256": hashlib_sha256(response_sequence_bytes).hexdigest(),
            "observation_sequence_sha256": hashlib_sha256(observation_sequence_bytes).hexdigest(),
            "first_evaluation": evaluations[0] if evaluations else {},
            "last_evaluation": evaluations[-1] if evaluations else {},
        },
        "inspect": {
            "user_id": inspected.get("user_id", ""),
            "turn_count": inspected.get("turn_count", 0),
            "history_size": inspected.get("session", {}).get("history_size", 0),
            "telemetry": inspected.get("core_status", {}).get("telemetry", {}),
        },
        "profile_definition": ({"likes": ["sushi", "cats"], "dislikes": ["dogs"]} if profile == "preference-continuity" else {}),
        "stop_summary": stopped.get("summary", {}),
    }
    return run_result


def main(argv: tuple[str, ...] = ()) -> int:
    parser = argparse_ArgumentParser(description=__doc__)
    parser.add_argument("--turns", type=int, default=MCP_CONFORMANCE_MINIMUM_TURNS)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--gate", default="EGR-315 MCP long-conversation conformance")
    parser.add_argument("--user-id", default="Section 3 MCP Conformance")
    parser.add_argument(
        "--profile",
        choices=("standard", "context-recall", "preference-continuity"),
        default="standard",
    )
    parser.add_argument("--config", default="", help="Configuration path passed to the MCP engram_start tool")
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
    args = parser.parse_args(argv)
    if args.turns < MCP_CONFORMANCE_MINIMUM_TURNS:
        raise ValueError(f"MCP conformance requires at least {MCP_CONFORMANCE_MINIMUM_TURNS} turns")
    if args.memgraph_probe_every < 0:
        raise ValueError("--memgraph-probe-every must be nonnegative")
    if args.memgraph_probe_every > args.turns:
        raise ValueError("--memgraph-probe-every must not exceed --turns, or no requested probe would run")
    selected_config = args.config
    with tempfile_TemporaryDirectory(prefix="engram-mcp-conformance-") as temporary_directory:
        if args.enable_rewrites or args.enable_sparse or args.enable_semantic or args.enable_reranker or args.enable_utility:
            raw_config = {}
            if args.config:
                loaded = yaml_safe_load(Path(args.config).read_text(encoding="utf-8"))
                if loaded:
                    if not isinstance(loaded, dict):
                        raise ValueError("--config must contain a YAML object")
                    raw_config = loaded
            if args.enable_rewrites:
                raw_config["retrieval_rewrites_enabled"] = True
            if args.enable_sparse:
                raw_config["sparse"] = {"enabled": True}
            if args.enable_semantic:
                manifest = json_loads(Path(args.semantic_manifest).read_text(encoding="utf-8"))
                if not isinstance(manifest, dict) or not SEMANTIC_MANIFEST_FIELD_DEFAULTS.keys() <= manifest.keys():
                    raise ValueError("--semantic-manifest is malformed")
                raw_config["semantic"] = {
                    "enabled": True,
                    **{name: manifest.get(name, default) for name, default in sorted(SEMANTIC_MANIFEST_FIELD_DEFAULTS.items())},
                }
            if args.enable_reranker:
                raw_config["reranker"] = {"enabled": True}
            if args.enable_utility:
                raw_config["utility"] = {"enabled": True}
            selected_path = Path(temporary_directory) / "config.yml"
            selected_path.write_text(yaml_safe_dump(raw_config, sort_keys=True), encoding="utf-8")
            selected_config = str(selected_path)
        result = asyncio_run(
            internal_run(
                args.turns,
                args.gate,
                args.user_id,
                args.profile,
                selected_config,
                args.memgraph_probe_every,
            )
        )
    result_text = json_dumps(result, indent=2, sort_keys=True) + "\n"
    if args.stdout:
        print(result_text, end="")
    else:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(result_text, encoding="utf-8")
        print(args.output)
    exit_code = 0 if result.get("passed", False) else 1
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main(tuple(sys_argv[1:])))
