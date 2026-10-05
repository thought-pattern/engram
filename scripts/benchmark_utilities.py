"""Evaluate and independently gate the Section 14 utility plugins."""

from argparse import ArgumentParser as argparse_ArgumentParser
from ast import Call as ast_Call, Name as ast_Name, parse as ast_parse, walk as ast_walk
from datetime import UTC, datetime
from decimal import Decimal
from hashlib import sha256 as hashlib_sha256
from json import dumps as json_dumps, loads as json_loads
from math import ceil as math_ceil
from pathlib import Path
from random import Random as random_Random
from string import ascii_letters as string_ascii_letters, digits as string_digits, punctuation as string_punctuation
from sys import path as sys_path
from time import perf_counter_ns as time_perf_counter_ns

REPOSITORY = Path(__file__).resolve().parents[1]
if str(REPOSITORY) not in sys_path:
    sys_path.insert(0, str(REPOSITORY))

from engram.constants import UTILITY_MAX_OUTPUT_BYTES, UTILITY_PLUGIN_NAMES
from engram.utilities import UtilityRegistry, evaluate_named_utility, utility_config, utility_plugin_contracts
from scripts.benchmark_metadata import benchmark_source_state

DEFAULT_CORPUS = Path("eval/section14-utilities-v1.json")
DEFAULT_OUTPUT_DIRECTORY = Path("eval/results/utilities")
THREAT_INPUTS = {
    "arithmetic": "calculate __import__('os').system('echo bad')",
    "boolean": "boolean __import__",
    "set": "set union {safe} and {bad item}",
    "date_time": "date __import__('os')",
    "unit_conversion": "convert __import__ to m",
    "version": "compare version __import__('os') and 1.0.0",
    "identifier": "validate slug __import__('os')",
}
THREAT_EXPECTATIONS = {
    "arithmetic": {"status": "rejected", "error_code": "arithmetic_syntax", "response": ""},
    "boolean": {"status": "rejected", "error_code": "boolean_syntax", "response": ""},
    "set": {"status": "rejected", "error_code": "collection_item_invalid", "response": ""},
    "date_time": {"status": "rejected", "error_code": "date_time_syntax", "response": ""},
    "unit_conversion": {"status": "rejected", "error_code": "unit_syntax", "response": ""},
    "version": {"status": "rejected", "error_code": "version_syntax", "response": ""},
    "identifier": {"status": "resolved", "error_code": "", "response": "invalid slug"},
}
FUZZ_PREFIXES = {
    "arithmetic": "calculate ",
    "boolean": "boolean ",
    "set": "set ",
    "date_time": "date ",
    "unit_conversion": "convert ",
    "version": "compare version ",
    "identifier": "validate slug ",
}


def parse_args():
    parser = argparse_ArgumentParser(description=__doc__)
    parser.add_argument("--corpus", type=Path, default=DEFAULT_CORPUS)
    parser.add_argument("--output-directory", type=Path, default=DEFAULT_OUTPUT_DIRECTORY)
    parser.add_argument("--fuzz-cases", type=int, default=500)
    result = parser.parse_args()
    return result


def percentile(values: list[float], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, math_ceil(fraction * len(ordered)) - 1))
    result = ordered[index]
    return result


def check_case(plugin_name: str, case: dict) -> tuple[bool, dict]:
    started = time_perf_counter_ns()
    outcome = evaluate_named_utility(case.get("input", ""), plugin_name)
    elapsed_ms = (time_perf_counter_ns() - started) / 1_000_000
    passed = outcome.get("status", "") == case.get("status", "")
    if "response" in case:
        passed = passed and outcome.get("response", "") == case.get("response", "")
    if "error_code" in case:
        passed = passed and outcome.get("error_code", "") == case.get("error_code", "")
    repeated = evaluate_named_utility(case.get("input", ""), plugin_name)
    record = {
        "input_sha256": hashlib_sha256(case.get("input", "").encode("utf-8")).hexdigest(),
        "expected_status": case.get("status", ""),
        "observed_status": outcome.get("status", ""),
        "response": outcome.get("response", ""),
        "error_code": outcome.get("error_code", ""),
        "latency_ms": elapsed_ms,
        "deterministic": repeated == outcome,
        "passed": passed and repeated == outcome,
    }
    result = (record.get("passed", False), record)
    return result


def property_checks(plugin_name: str) -> dict:
    if plugin_name == "arithmetic":
        first = evaluate_named_utility("calculate 319 + 71", plugin_name).get("response", "")
        second = evaluate_named_utility("calculate 71 + 319", plugin_name).get("response", "")
        result = {"commutative_addition": first == second == "390"}
        return result
    if plugin_name == "boolean":
        value = evaluate_named_utility("boolean not not true", plugin_name).get("response", "")
        result = {"double_negation": value == "true"}
        return result
    if plugin_name == "set":
        first = evaluate_named_utility("set union {cat,sushi} and {dog,cat}", plugin_name).get("response", "")
        second = evaluate_named_utility("set union {dog,cat} and {cat,sushi}", plugin_name).get("response", "")
        result = {"commutative_union": first == second == "{cat, dog, sushi}"}
        return result
    if plugin_name == "date_time":
        forward = evaluate_named_utility("date 2026-08-22 plus 10 days", plugin_name).get("response", "")
        backward = evaluate_named_utility(f"date {forward} minus 10 days", plugin_name).get("response", "")
        result = {"date_addition_round_trip": backward == "2026-08-22"}
        return result
    if plugin_name == "unit_conversion":
        forward = evaluate_named_utility("convert 123.5 km to mi", plugin_name).get("response", "").split()[0]
        backward = evaluate_named_utility(f"convert {forward} mi to km", plugin_name).get("response", "").split()[0]
        result = {"unit_round_trip": abs(Decimal(backward) - Decimal("123.5")) < Decimal("0.000000000001")}
        return result
    if plugin_name == "version":
        first = evaluate_named_utility("compare version 1.2.3 and 2.0.0", plugin_name).get("response", "")
        second = evaluate_named_utility("compare version 2.0.0 and 1.2.3", plugin_name).get("response", "")
        result = {"comparison_antisymmetry": first.endswith("< 2.0.0") and second.endswith("> 1.2.3")}
        return result
    first = evaluate_named_utility("validate uuid 550e8400-e29b-41d4-a716-446655440000", plugin_name)
    second = evaluate_named_utility("validate uuid 550e8400-e29b-41d4-a716-446655440000", plugin_name)
    result = {"canonical_identifier_replay": first == second and first.get("response", "").startswith("valid uuid:")}
    return result


def fuzz_plugin(plugin_name: str, count: int) -> dict:
    randomizer = random_Random(f"section14:{plugin_name}:v1")
    alphabet = string_ascii_letters + string_digits + string_punctuation + " \t"
    statuses = {}
    failed = 0
    oversized_output = 0
    contract_violations = 0
    canonical_replay_failures = 0
    latencies = []
    for _ in range(count):
        suffix = "".join(randomizer.choice(alphabet) for _ in range(randomizer.randint(0, 256)))
        started = time_perf_counter_ns()
        result = evaluate_named_utility(FUZZ_PREFIXES.get(plugin_name, "") + suffix, plugin_name)
        latencies.append((time_perf_counter_ns() - started) / 1_000_000)
        status = result.get("status", "")
        statuses[status] = statuses.get(status, 0) + 1
        failed += int(status == "failed")
        oversized_output += int(len(result.get("response", "").encode("utf-8")) > UTILITY_MAX_OUTPUT_BYTES)
        repeated = evaluate_named_utility(FUZZ_PREFIXES.get(plugin_name, "") + suffix, plugin_name)
        if repeated != result or status not in {"resolved", "rejected"}:
            contract_violations += 1
        elif status == "resolved":
            canonical = evaluate_named_utility(result.get("canonical_input", ""), plugin_name)
            if canonical.get("status", "") != "resolved" or canonical.get("response", "") != result.get("response", ""):
                canonical_replay_failures += 1
        elif result.get("response", "") or result.get("canonical_input", "") or not result.get("error_code", ""):
            contract_violations += 1
    result = {
        "case_count": count,
        "statuses": statuses,
        "unexpected_failure_count": failed,
        "oversized_output_count": oversized_output,
        "contract_violation_count": contract_violations,
        "canonical_replay_failure_count": canonical_replay_failures,
        "p95_latency_ms": percentile(latencies, 0.95),
        "passed": failed == 0 and oversized_output == 0 and contract_violations == 0 and canonical_replay_failures == 0,
    }
    return result


def forbidden_execution_calls() -> list[dict]:
    source_path = Path("engram/utilities.py")
    tree = ast_parse(source_path.read_text(encoding="utf-8"), filename=str(source_path))
    forbidden = []
    for node in ast_walk(tree):
        if (
            isinstance(node, ast_Call)
            and isinstance(node.func, ast_Name)
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
        for case in specification.get(partition, []):
            _, record = check_case(plugin_name, case)
            records.append(record)
            all_latencies.append(record.get("latency_ms", 0.0))
            all_deterministic.append(record.get("deterministic", False))
        passed_count = sum(record.get("passed", False) for record in records)
        partitions[partition] = {
            "case_count": len(records),
            "passed_count": passed_count,
            "accuracy": passed_count / len(records),
            "records": records,
        }
    resource = evaluate_named_utility(specification.get("resource_input", ""), plugin_name)
    resource_error = specification.get("resource_error", "")
    resource_passed = resource.get("status", "") == "rejected" and resource.get("error_code", "") == resource_error
    threat = evaluate_named_utility(THREAT_INPUTS.get(plugin_name, ""), plugin_name)
    threat_expected = THREAT_EXPECTATIONS.get(plugin_name, {})
    # Every expected threat field is a string; an absent observed field never matches a declared value.
    threat_passed = all(name in threat and threat.get(name, "") == value for name, value in threat_expected.items())
    fuzz = fuzz_plugin(plugin_name, fuzz_cases)
    properties = property_checks(plugin_name)
    fuzz_failure_fields = ("unexpected_failure_count", "contract_violation_count", "canonical_replay_failure_count")
    fuzz_failures = sum(fuzz.get(name, 0) for name in fuzz_failure_fields)
    fuzz_passed = fuzz.get("passed", False)
    values = {
        "conformance_accuracy": partitions.get("conformance", {}).get("accuracy", 0.0),
        "held_out_accuracy": partitions.get("held_out", {}).get("accuracy", 0.0),
        "determinism_rate": sum(all_deterministic) / len(all_deterministic),
        "resource_rejection_rate": float(resource_passed),
        "fuzz_failure_rate": fuzz_failures / fuzz.get("case_count", 0),
        "p95_latency_ms": max(percentile(all_latencies, 0.95), fuzz.get("p95_latency_ms", 0.0)),
    }
    checks = {
        "conformance_accuracy": values.get("conformance_accuracy", 0.0) >= gates.get("accuracy_min", 0.0),
        "held_out_accuracy": values.get("held_out_accuracy", 0.0) >= gates.get("accuracy_min", 0.0),
        "determinism": values.get("determinism_rate", 0.0) >= gates.get("determinism_min", 0.0),
        "resource_rejection": values.get("resource_rejection_rate", 0.0) >= gates.get("resource_rejection_rate_min", 0.0),
        "fuzz_safety": values.get("fuzz_failure_rate", 0.0) <= gates.get("fuzz_failure_rate_max", 0.0) and fuzz_passed,
        "latency": values.get("p95_latency_ms", 0.0) <= gates.get("p95_latency_ms_max", 0.0),
        "property_checks": all(properties.values()),
        "threat_payload_safe_disposition": threat_passed,
    }
    passed = all(checks.values())
    result = {
        "generated_at": datetime.now(UTC).isoformat(),
        "plugin_name": plugin_name,
        "contract": next(contract for contract in utility_plugin_contracts() if contract.get("name", "") == plugin_name),
        "source_state": source_state,
        "partitions": partitions,
        "properties": properties,
        "resource_limit": {
            "expected_error": resource_error,
            "observed_status": resource.get("status", ""),
            "observed_error": resource.get("error_code", ""),
            "passed": resource_passed,
        },
        "threat_probe": {
            "expected": threat_expected,
            "status": threat.get("status", ""),
            "error_code": threat.get("error_code", ""),
            "response_sha256": hashlib_sha256(threat.get("response", "").encode("utf-8")).hexdigest(),
            "passed": threat_passed,
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
    return result


def main() -> int:
    args = parse_args()
    if args.fuzz_cases < 1:
        raise ValueError("--fuzz-cases must be positive")
    corpus = json_loads(args.corpus.read_text(encoding="utf-8"))
    if not isinstance(corpus, dict) or not isinstance(corpus.get("plugins", {}), dict):
        raise ValueError("utility corpus must be an object whose plugins are keyed by name")
    plugins = corpus.get("plugins", {})
    if tuple(plugins) != UTILITY_PLUGIN_NAMES:
        raise ValueError("utility corpus plugin order does not match the executable allowlist")
    gates = corpus.get("gates", {})
    if "gates" not in corpus or not isinstance(gates, dict):
        raise ValueError("utility corpus must declare its gate thresholds")
    source_state = benchmark_source_state()
    results = {
        name: evaluate_plugin(name, plugins.get(name, {}), gates, args.fuzz_cases, source_state) for name in UTILITY_PLUGIN_NAMES
    }
    forbidden = forbidden_execution_calls()
    registry = UtilityRegistry(utility_config(enabled=True))
    # Only an explicit disabled default counts as default-off.
    default_off = utility_config().get("enabled", True) is False
    gate_results = {name: result.get("gate", {}) for name, result in results.items()}
    all_plugins_passed = all(gate.get("passed", False) for gate in gate_results.values())
    overall_passed = all_plugins_passed and not forbidden and default_off
    args.output_directory.mkdir(parents=True, exist_ok=True)
    for name, result in results.items():
        output = args.output_directory / f"{name.replace('_', '-')}-conformance.json"
        output.write_text(json_dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    summary = {
        "generated_at": datetime.now(UTC).isoformat(),
        "corpus": {"path": args.corpus.as_posix()},
        "source_state": source_state,
        "fixed_registry": {
            "plugin_names": list(UTILITY_PLUGIN_NAMES),
            "dynamic_execution_calls": forbidden,
            "health": registry.health(),
        },
        "fuzz_cases_per_plugin": args.fuzz_cases,
        "plugins": {
            name: {
                "passed": gate.get("passed", False),
                "values": gate.get("values", {}),
                "promoted_for_opt_in_component_use": gate.get("promoted_for_opt_in_component_use", False),
                "default_enabled": gate.get("default_enabled", False),
            }
            for name, gate in gate_results.items()
        },
        "enablement_decision": {
            "all_plugins_independently_passed": all_plugins_passed,
            "default_enabled": not default_off,
            "decision": "promoted_for_opt_in_component_use_default_off" if overall_passed else "withheld",
            "reason": "Utility capability evidence does not change the protected default rollout.",
        },
        "passed": overall_passed,
    }
    summary_path = args.output_directory / "benchmark.json"
    summary_path.write_text(json_dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json_dumps({"output": summary_path.as_posix(), "passed": overall_passed}, sort_keys=True))
    result = 0 if overall_passed else 1
    return result


if __name__ == "__main__":
    raise SystemExit(main())
