"""Measure the frozen Section 11 engineering promotion corpus."""

import argparse
import json
import statistics
import sys
import time
from collections import Counter
from pathlib import Path

REPOSITORY = Path(__file__).resolve().parents[1]
if str(REPOSITORY) not in sys.path:
    sys.path.insert(0, str(REPOSITORY))

from engram.constants import QueryOperator
from engram.identity import build_scoped_retrieval_key, scope_key, scoped_retrieval_key_to_json
from engram.rewrite import RewriteEngine, load_default_rewrite_corpus
from scripts.benchmark_metadata import benchmark_source_state, recorded_at

DEFAULT_CORPUS = Path("eval/section11-rewrite-v1.json")
DEFAULT_OUTPUT = Path("documentation/rewrite/benchmark-2026-08-20.json")


def _percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, max(0, int((len(ordered) - 1) * fraction)))]


def _load(path: Path) -> dict[str, object]:
    decoded = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(decoded, dict) or decoded.get("schema_version") != 1:
        raise ValueError("rewrite benchmark corpus must be a schema-1 object")
    if not isinstance(decoded.get("cases"), list) or not isinstance(decoded.get("gates"), dict):
        raise ValueError("rewrite benchmark corpus must contain cases and gates")
    return decoded


def benchmark(corpus_path: Path = DEFAULT_CORPUS, repeats: int = 100) -> dict[str, object]:
    if repeats < 30:
        raise ValueError("rewrite benchmark requires at least 30 repeats")
    corpus = _load(corpus_path)
    cases = corpus["cases"]
    gates = corpus["gates"]
    if not isinstance(cases, list) or not isinstance(gates, dict):
        raise ValueError("rewrite benchmark corpus fields have invalid types")
    engine = RewriteEngine(load_default_rewrite_corpus())
    selected_scope = scope_key()
    results = []
    latencies_ms = []
    positive = 0
    baseline_hits = 0
    rewritten_hits = 0
    false_direct_answers = 0
    final_owners: dict[str, str] = {}
    semantic_collisions = 0
    category_counts: Counter[str] = Counter()
    for raw_case in cases:
        if not isinstance(raw_case, dict):
            raise ValueError("rewrite benchmark cases must be objects")
        case = raw_case
        case_id = str(case["case_id"])
        operator = QueryOperator(str(case["operator"]))
        execution = engine.rewrite(
            str(case["input"]),
            operator=operator,
            subject=str(case["subject"]),
            inherited_subject=case["inherited_subject"] is True,
        )
        expected = str(case["expected_final"])
        expected_key = build_scoped_retrieval_key(selected_scope, expected)
        baseline_key = build_scoped_retrieval_key(selected_scope, str(case["input"]))
        rewritten_key = build_scoped_retrieval_key(selected_scope, execution["final_text"])
        should_rewrite = case["should_rewrite"] is True
        if should_rewrite:
            positive += 1
            category_counts[str(case["category"])] += 1
            baseline_hits += baseline_key == expected_key
            rewritten_hits += rewritten_key == expected_key
            signature = scoped_retrieval_key_to_json(expected_key)
            prior = final_owners.get(signature)
            if prior and prior != case_id:
                semantic_collisions += 1
            final_owners[signature] = case_id
        elif rewritten_key != baseline_key:
            false_direct_answers += 1
        results.append(
            {
                "case_id": case_id,
                "category": case["category"],
                "passed": execution["final_text"] == expected and bool(execution["chain"]) == should_rewrite,
                "baseline_exact_hit": baseline_key == expected_key if should_rewrite else False,
                "rewritten_exact_hit": rewritten_key == expected_key if should_rewrite else False,
                "rules": [step[0] for step in execution["chain"]],
                "stop_reason": execution["stop_reason"].value,
            }
        )
    for _ in range(repeats):
        for raw_case in cases:
            if not isinstance(raw_case, dict):
                continue
            started = time.perf_counter_ns()
            engine.rewrite(
                str(raw_case["input"]),
                operator=QueryOperator(str(raw_case["operator"])),
                subject=str(raw_case["subject"]),
                inherited_subject=raw_case["inherited_subject"] is True,
            )
            latencies_ms.append((time.perf_counter_ns() - started) / 1_000_000)
    negatives = len(cases) - positive
    baseline_recall = baseline_hits / positive
    rewritten_recall = rewritten_hits / positive
    recall_gain = rewritten_recall - baseline_recall
    collision_rate = semantic_collisions / positive
    false_rate = false_direct_answers / negatives
    verdicts = {
        "all_cases_pass": all(bool(result["passed"]) for result in results),
        "recall_gain": recall_gain >= float(gates["minimum_recall_gain"]),
        "semantic_collision_rate": collision_rate <= float(gates["maximum_semantic_collision_rate"]),
        "false_direct_answer_rate": false_rate <= float(gates["maximum_false_direct_answer_rate"]),
    }
    return {
        "schema_version": 1,
        "created_at": recorded_at(),
        "source_state": benchmark_source_state(),
        "corpus": {
            "path": corpus_path.as_posix(),
            "corpus_id": corpus["corpus_id"],
            "frozen_at": corpus["frozen_at"],
            "partition": corpus["provenance"],
            "positive_cases": positive,
            "negative_controls": negatives,
            "category_counts": dict(sorted(category_counts.items())),
        },
        "accuracy": {
            "baseline_exact_hits": baseline_hits,
            "rewritten_exact_hits": rewritten_hits,
            "baseline_exact_recall": baseline_recall,
            "rewritten_exact_recall": rewritten_recall,
            "absolute_recall_gain": recall_gain,
            "semantic_collisions": semantic_collisions,
            "semantic_collision_rate": collision_rate,
            "false_direct_answers": false_direct_answers,
            "false_direct_answer_rate": false_rate,
        },
        "latency_observation": {
            "samples": len(latencies_ms),
            "repeats": repeats,
            "p50_ms": statistics.median(latencies_ms),
            "p95_ms": _percentile(latencies_ms, 0.95),
            "p99_ms": _percentile(latencies_ms, 0.99),
            "maximum_ms": max(latencies_ms),
            "timing_gate_applied": False,
        },
        "gates": gates,
        "verdicts": verdicts,
        "passed": all(verdicts.values()),
        "cases": results,
        "release_authority": "component engineering promotion only; Section 16 project qualification owns release authority",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus", type=Path, default=DEFAULT_CORPUS)
    parser.add_argument("--repeats", type=int, default=100)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    report = benchmark(args.corpus, args.repeats)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({"passed": report["passed"], "accuracy": report["accuracy"], "latency": report["latency_observation"]}))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
