"""Run the frozen Section 9 temporal/conflict corpus and report case durations."""

import argparse
import json
import statistics
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

REPOSITORY = Path(__file__).resolve().parents[1]
if str(REPOSITORY) not in sys.path:
    sys.path.insert(0, str(REPOSITORY))

from engram.graph import RelationClaimProjection, relation_claim_projection_from_graph_row
from engram.relation import select_relation_claims
from engram.temporal import parse_temporal_query
from scripts.benchmark_metadata import benchmark_source_state


def _score(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be numeric")
    result = float(value)
    return result


def _positive_int(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{name} must be a positive integer")
    return value


def _item(value: dict[str, object], cardinality: str) -> RelationClaimProjection:
    trust_available = bool(value.get("trust_available", True))
    valid_from = str(value.get("valid_from", "2020-01-01T00:00:00Z"))
    valid_to = str(value.get("valid_to", "2030-01-01T00:00:00Z"))
    system_from = str(value.get("system_from", "2020-01-01T00:00:00Z"))
    system_to = str(value.get("system_to", ""))
    row = {
        "claim_id": value["claim_id"],
        "subject_entity_id": "entity:subject",
        "predicate_id": "predicate:relation",
        "object_entity_id": value["object_id"],
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
        "supplied_trust": _score(value.get("trust", 0.8), "trust") if trust_available else 0.0,
        "supplied_trust_available": trust_available,
        "supplied_trust_version": _positive_int(value.get("trust_version", 1), "trust_version") if trust_available else 0,
        "supplied_trust_version_available": trust_available,
        "structured_match": 1.0,
        "structured_match_available": True,
        "semantic_similarity": 0.0,
        "semantic_similarity_available": False,
        "object_label": str(value["object_id"]),
        "object_type": "ENTITY",
        "predicate_cardinality": cardinality,
    }
    result = relation_claim_projection_from_graph_row(row)
    return result


def _percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int((len(ordered) - 1) * fraction)))
    result = ordered[index]
    return result


def run(corpus_path: Path) -> dict[str, object]:
    corpus = json.loads(corpus_path.read_text(encoding="utf-8"))
    results = []
    durations = []
    for split in ("development", "held_out"):
        for case in corpus[split]:
            started = time.perf_counter_ns()
            items = tuple(_item(value, case["cardinality"]) for value in case["claims"])
            selection = select_relation_claims(items, parse_temporal_query(case["query"]))
            elapsed_ms = (time.perf_counter_ns() - started) / 1_000_000
            durations.append(elapsed_ms)
            expected = case["expected"]
            observed = {
                "direct_answer": selection["direct_answer"],
                "reason": selection["reason"].value,
                "selected_claim_id": selection["selected_claim_id"],
                "conflict_claim_ids": list(selection["conflict_claim_ids"]),
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
            "median": statistics.median(durations),
            "p95": _percentile(durations, 0.95),
            "maximum": max(durations),
        },
        "cases": results,
    }
    return report


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--corpus", type=Path, default=REPOSITORY / "eval" / "section9-temporal-conflict-v1.json")
    parser.add_argument(
        "--output",
        type=Path,
        default=REPOSITORY / "documentation" / "temporal" / "benchmark-2026-08-20.json",
    )
    arguments = parser.parse_args()
    report = run(arguments.corpus)
    arguments.output.parent.mkdir(parents=True, exist_ok=True)
    arguments.output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({"passed": report["passed"], "case_count": report["case_count"], "duration_ms": report["duration_ms"]}))
    if not report["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
