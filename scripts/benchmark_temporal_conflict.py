"""Run the frozen Section 9 temporal/conflict corpus and report case durations."""

from argparse import ArgumentParser as argparse_ArgumentParser
from datetime import UTC, datetime
from json import dumps as json_dumps, loads as json_loads
from pathlib import Path
from statistics import median as statistics_median
from sys import path as sys_path
from time import perf_counter_ns as time_perf_counter_ns

REPOSITORY = Path(__file__).resolve().parents[1]
if str(REPOSITORY) not in sys_path:
    sys_path.insert(0, str(REPOSITORY))

from engram.graph import relation_proposition_projection_from_graph_row
from engram.relation import select_relation_propositions
from engram.temporal import parse_temporal_query
from scripts.benchmark_metadata import benchmark_source_state


def internal_score(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be numeric")
    result = float(value)
    return result


def positive_int(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{name} must be a positive integer")
    return value


def internal_item(value: dict[str, object], cardinality: str) -> dict:
    trust_available = bool(value.get("trust_available", True))
    valid_from = str(value.get("valid_from", "2020-01-01T00:00:00Z"))
    valid_to = str(value.get("valid_to", "2030-01-01T00:00:00Z"))
    system_from = str(value.get("system_from", "2020-01-01T00:00:00Z"))
    system_to = str(value.get("system_to", ""))
    row = {
        "proposition_id": value.get("proposition_id", ""),
        "subject_entity_id": "entity:subject",
        "predicate_id": "predicate:relation",
        "object_entity_id": value.get("object_id", ""),
        "invalidated_at": "",
        "invalidated_at_available": False,
        "system_from": system_from,
        "system_from_available": bool(system_from),
        "system_to": system_to,
        "system_to_available": bool(system_to),
        "valid_from": valid_from,
        "valid_from_available": bool(valid_from),
        "valid_to": valid_to,
        "valid_to_available": bool(valid_to),
        "predicate_canonical": True,
        "ownership_category": "PUBLIC",
        "trust_category": "source_supplied" if trust_available else "",
        "trust_category_available": trust_available,
        "supplied_trust": internal_score(value.get("trust", 0.8), "trust") if trust_available else 0.0,
        "supplied_trust_available": trust_available,
        "supplied_trust_version": positive_int(value.get("trust_version", 1), "trust_version") if trust_available else 0,
        "supplied_trust_version_available": trust_available,
        "structured_match": 1.0,
        "structured_match_available": True,
        "semantic_similarity": 0.0,
        "semantic_similarity_available": False,
        "object_label": str(value.get("object_id", "")),
        "object_type": "ENTITY",
        "predicate_cardinality": cardinality,
    }
    result = relation_proposition_projection_from_graph_row(row)
    return result


def internal_percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int((len(ordered) - 1) * fraction)))
    result = ordered[index]
    return result


def run(corpus_path: Path) -> dict[str, object]:
    corpus = json_loads(corpus_path.read_text(encoding="utf-8"))
    results = []
    durations = []
    for split in ("development", "held_out"):
        for case in corpus[split]:
            started = time_perf_counter_ns()
            items = tuple(internal_item(value, case["cardinality"]) for value in case["propositions"])
            selection = select_relation_propositions(items, parse_temporal_query(case["query"]))
            elapsed_ms = (time_perf_counter_ns() - started) / 1_000_000
            durations.append(elapsed_ms)
            expected = case["expected"]
            observed = {
                "direct_answer": selection["direct_answer"],
                "reason": selection["reason"].value,
                "selected_proposition_id": selection["selected_proposition_id"],
                "conflict_proposition_ids": list(selection["conflict_proposition_ids"]),
            }
            results.append(
                {
                    "id": case["id"],
                    "split": split,
                    "passed": observed == expected,
                    "elapsed_ms": elapsed_ms,
                    "expected": expected,
                    "observed": observed,
                }
            )
    report = {
        "schema_version": 1,
        "corpus": corpus["name"],
        "generated_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "source": benchmark_source_state(),
        "passed": all(value["passed"] for value in results),
        "case_count": len(results),
        "passed_count": sum(value["passed"] for value in results),
        "duration_ms": {
            "median": statistics_median(durations),
            "p95": internal_percentile(durations, 0.95),
            "maximum": max(durations),
        },
        "cases": results,
    }
    return report


def main() -> None:
    parser = argparse_ArgumentParser()
    parser.add_argument("--corpus", type=Path, default=Path("eval/section9-temporal-conflict-v1.json"))
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("eval/results/temporal/benchmark-2026-08-20.json"),
    )
    arguments = parser.parse_args()
    report = run(arguments.corpus)
    arguments.output.parent.mkdir(parents=True, exist_ok=True)
    arguments.output.write_text(json_dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json_dumps({"passed": report["passed"], "case_count": report["case_count"], "duration_ms": report["duration_ms"]}))
    if not report["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
