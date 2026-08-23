"""Evaluate and independently gate the Section 14 utility plugins."""

import argparse
import ast
import hashlib
import json
import math
import random
import string
import sys
import time
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

REPOSITORY = Path(__file__).resolve().parents[1]
if str(REPOSITORY) not in sys.path:
    sys.path.insert(0, str(REPOSITORY))

from engram.constants import UTILITY_MAX_OUTPUT_BYTES, UTILITY_PLUGIN_NAMES
from engram.utilities import UtilityRegistry, evaluate_named_utility, utility_config, utility_plugin_contracts
from scripts.benchmark_metadata import benchmark_source_state

DEFAULT_CORPUS = REPOSITORY / "eval" / "section14-utilities-v1.json"
DEFAULT_OUTPUT_DIRECTORY = REPOSITORY / "documentation" / "utilities"
THREAT_INPUTS = {
    "arithmetic_v1": "calculate __import__('os').system('echo bad')",
    "boolean_v1": "boolean __import__",
    "set_v1": "set union {safe} and {bad item}",
    "date_time_v1": "date __import__('os')",
    "unit_conversion_v1": "convert __import__ to m",
    "version_v1": "compare version __import__('os') and 1.0.0",
    "identifier_v1": "validate slug __import__('os')",
}
FUZZ_PREFIXES = {
    "arithmetic_v1": "calculate ",
    "boolean_v1": "boolean ",
    "set_v1": "set ",
    "date_time_v1": "date ",
    "unit_conversion_v1": "convert ",
    "version_v1": "compare version ",
    "identifier_v1": "validate slug ",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus", type=Path, default=DEFAULT_CORPUS)
    parser.add_argument("--output-directory", type=Path, default=DEFAULT_OUTPUT_DIRECTORY)
    parser.add_argument("--fuzz-cases", type=int, default=500)
    return parser.parse_args()


def percentile(values: list[float], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, math.ceil(fraction * len(ordered)) - 1))
    return ordered[index]


def check_case(plugin_name: str, case: dict) -> tuple[bool, dict]:
    started = time.perf_counter_ns()
    result = evaluate_named_utility(case["input"], plugin_name)
    elapsed_ms = (time.perf_counter_ns() - started) / 1_000_000
    passed = result["status"] == case["status"]
    if "response" in case:
        passed = passed and result["response"] == case["response"]
    if "error_code" in case:
        passed = passed and result["error_code"] == case["error_code"]
    repeated = evaluate_named_utility(case["input"], plugin_name)
    record = {
        "input_sha256": hashlib.sha256(case["input"].encode("utf-8")).hexdigest(),
        "expected_status": case["status"],
        "observed_status": result["status"],
        "response": result["response"],
        "error_code": result["error_code"],
        "latency_ms": elapsed_ms,
        "deterministic": repeated == result,
        "passed": passed and repeated == result,
    }
    return record["passed"], record


def property_checks(plugin_name: str) -> dict:
    if plugin_name == "arithmetic_v1":
        first = evaluate_named_utility("calculate 319 + 71", plugin_name)["response"]
        second = evaluate_named_utility("calculate 71 + 319", plugin_name)["response"]
        return {"commutative_addition": first == second == "390"}
    if plugin_name == "boolean_v1":
        value = evaluate_named_utility("boolean not not true", plugin_name)["response"]
        return {"double_negation": value == "true"}
    if plugin_name == "set_v1":
        first = evaluate_named_utility("set union {cat,sushi} and {dog,cat}", plugin_name)["response"]
        second = evaluate_named_utility("set union {dog,cat} and {cat,sushi}", plugin_name)["response"]
        return {"commutative_union": first == second == "{cat, dog, sushi}"}
    if plugin_name == "date_time_v1":
        forward = evaluate_named_utility("date 2026-08-22 plus 10 days", plugin_name)["response"]
        backward = evaluate_named_utility(f"date {forward} minus 10 days", plugin_name)["response"]
        return {"date_addition_round_trip": backward == "2026-08-22"}
    if plugin_name == "unit_conversion_v1":
        forward = evaluate_named_utility("convert 123.5 km to mi", plugin_name)["response"].split()[0]
        backward = evaluate_named_utility(f"convert {forward} mi to km", plugin_name)["response"].split()[0]
        return {"unit_round_trip": abs(Decimal(backward) - Decimal("123.5")) < Decimal("0.000000000001")}
    if plugin_name == "version_v1":
        first = evaluate_named_utility("compare version 1.2.3 and 2.0.0", plugin_name)["response"]
        second = evaluate_named_utility("compare version 2.0.0 and 1.2.3", plugin_name)["response"]
        return {"comparison_antisymmetry": first.endswith("< 2.0.0") and second.endswith("> 1.2.3")}
    first = evaluate_named_utility("validate uuid 550e8400-e29b-41d4-a716-446655440000", plugin_name)
    second = evaluate_named_utility("validate uuid 550e8400-e29b-41d4-a716-446655440000", plugin_name)
    return {"canonical_identifier_replay": first == second and first["response"].startswith("valid uuid:")}


def fuzz_plugin(plugin_name: str, count: int) -> dict:
    randomizer = random.Random(f"section14:{plugin_name}:v1")
    alphabet = string.ascii_letters + string.digits + string.punctuation + " \t"
    statuses = {}
    failed = 0
    oversized_output = 0
    latencies = []
    for _ in range(count):
        suffix = "".join(randomizer.choice(alphabet) for _ in range(randomizer.randint(0, 256)))
        started = time.perf_counter_ns()
        result = evaluate_named_utility(FUZZ_PREFIXES[plugin_name] + suffix, plugin_name)
        latencies.append((time.perf_counter_ns() - started) / 1_000_000)
        status = result["status"]
        statuses[status] = statuses.get(status, 0) + 1
        failed += int(status == "failed")
        oversized_output += int(len(result["response"].encode("utf-8")) > UTILITY_MAX_OUTPUT_BYTES)
    return {
        "case_count": count,
        "statuses": statuses,
        "unexpected_failure_count": failed,
        "oversized_output_count": oversized_output,
        "p95_latency_ms": percentile(latencies, 0.95),
        "passed": failed == 0 and oversized_output == 0,
    }


def forbidden_execution_calls() -> list[dict]:
    source_path = REPOSITORY / "engram" / "utilities.py"
    tree = ast.parse(source_path.read_text(encoding="utf-8"), filename=str(source_path))
    forbidden = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id
            in {
                "eval",
                "exec",
                "compile",
                "__import__",
            }
        ):
            forbidden.append({"name": node.func.id, "line": node.lineno})
    return forbidden


def evaluate_plugin(plugin_name: str, specification: dict, gates: dict, fuzz_cases: int, source_state: dict) -> dict:
    partitions = {}
    all_latencies = []
    all_deterministic = []
    for partition in ("conformance", "held_out"):
        records = []
        for case in specification[partition]:
            _, record = check_case(plugin_name, case)
            records.append(record)
            all_latencies.append(record["latency_ms"])
            all_deterministic.append(record["deterministic"])
        partitions[partition] = {
            "case_count": len(records),
            "passed_count": sum(record["passed"] for record in records),
            "accuracy": sum(record["passed"] for record in records) / len(records),
            "records": records,
        }
    resource = evaluate_named_utility(specification["resource_input"], plugin_name)
    resource_passed = resource["status"] == "rejected" and resource["error_code"] == specification["resource_error"]
    threat = evaluate_named_utility(THREAT_INPUTS[plugin_name], plugin_name)
    fuzz = fuzz_plugin(plugin_name, fuzz_cases)
    properties = property_checks(plugin_name)
    values = {
        "conformance_accuracy": partitions["conformance"]["accuracy"],
        "held_out_accuracy": partitions["held_out"]["accuracy"],
        "determinism_rate": sum(all_deterministic) / len(all_deterministic),
        "resource_rejection_rate": float(resource_passed),
        "fuzz_failure_rate": fuzz["unexpected_failure_count"] / fuzz["case_count"],
        "p95_latency_ms": max(percentile(all_latencies, 0.95), fuzz["p95_latency_ms"]),
    }
    checks = {
        "conformance_accuracy": values["conformance_accuracy"] >= gates["accuracy_min"],
        "held_out_accuracy": values["held_out_accuracy"] >= gates["accuracy_min"],
        "determinism": values["determinism_rate"] >= gates["determinism_min"],
        "resource_rejection": values["resource_rejection_rate"] >= gates["resource_rejection_rate_min"],
        "fuzz_safety": values["fuzz_failure_rate"] <= gates["fuzz_failure_rate_max"] and fuzz["passed"],
        "latency": values["p95_latency_ms"] <= gates["p95_latency_ms_max"],
        "property_checks": all(properties.values()),
        "threat_payload_not_executed": threat["status"] != "failed",
    }
    passed = all(checks.values())
    return {
        "schema_version": 1,
        "generated_at": datetime.now(UTC).isoformat(),
        "plugin_name": plugin_name,
        "contract": next(contract for contract in utility_plugin_contracts() if contract["name"] == plugin_name),
        "source_state": source_state,
        "partitions": partitions,
        "properties": properties,
        "resource_limit": {
            "expected_error": specification["resource_error"],
            "observed_status": resource["status"],
            "observed_error": resource["error_code"],
            "passed": resource_passed,
        },
        "threat_probe": {
            "status": threat["status"],
            "error_code": threat["error_code"],
            "response_sha256": hashlib.sha256(threat["response"].encode("utf-8")).hexdigest(),
            "passed": threat["status"] != "failed",
        },
        "fuzz": fuzz,
        "gate": {
            "thresholds": gates,
            "values": values,
            "checks": checks,
            "passed": passed,
            "promoted_for_opt_in_component_use": passed,
            "default_enabled": False,
        },
    }


def main() -> int:
    args = parse_args()
    if args.fuzz_cases < 1:
        raise ValueError("--fuzz-cases must be positive")
    corpus = json.loads(args.corpus.read_text(encoding="utf-8"))
    if tuple(corpus["plugins"]) != UTILITY_PLUGIN_NAMES:
        raise ValueError("utility corpus plugin order does not match the executable allowlist")
    source_state = benchmark_source_state()
    results = {
        name: evaluate_plugin(name, corpus["plugins"][name], corpus["gates"], args.fuzz_cases, source_state)
        for name in UTILITY_PLUGIN_NAMES
    }
    forbidden = forbidden_execution_calls()
    registry = UtilityRegistry(utility_config(enabled=True))
    default_off = utility_config()["enabled"] is False
    overall_passed = all(result["gate"]["passed"] for result in results.values()) and not forbidden and default_off
    args.output_directory.mkdir(parents=True, exist_ok=True)
    for name, result in results.items():
        output = args.output_directory / f"{name.replace('_', '-')}-conformance-2026-08-22.json"
        output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    summary = {
        "schema_version": 1,
        "generated_at": datetime.now(UTC).isoformat(),
        "corpus": {"path": str(args.corpus.relative_to(REPOSITORY)), "version": corpus["corpus_version"]},
        "source_state": source_state,
        "fixed_registry": {
            "plugin_names": list(UTILITY_PLUGIN_NAMES),
            "dynamic_execution_calls": forbidden,
            "health": registry.health(),
        },
        "fuzz_cases_per_plugin": args.fuzz_cases,
        "plugins": {
            name: {
                "passed": result["gate"]["passed"],
                "values": result["gate"]["values"],
                "promoted_for_opt_in_component_use": result["gate"]["promoted_for_opt_in_component_use"],
                "default_enabled": result["gate"]["default_enabled"],
            }
            for name, result in results.items()
        },
        "enablement_decision": {
            "all_plugins_independently_passed": all(result["gate"]["passed"] for result in results.values()),
            "default_enabled": not default_off,
            "decision": "promoted_for_opt_in_component_use_default_off" if overall_passed else "withheld",
            "reason": "Section 14 supplies component evidence; protected default rollout remains owned by Section 16.",
        },
        "passed": overall_passed,
    }
    summary_path = args.output_directory / "benchmark-2026-08-22.json"
    summary_path.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({"output": str(summary_path), "passed": overall_passed}, sort_keys=True))
    return 0 if overall_passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
