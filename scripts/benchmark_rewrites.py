"""Measure the frozen Section 11 engineering promotion corpus."""

from argparse import ArgumentParser as argparse_ArgumentParser
from collections import Counter
from json import dumps as json_dumps, loads as json_loads
from pathlib import Path
from statistics import median as statistics_median
from sys import path as sys_path
from time import perf_counter_ns as time_perf_counter_ns

REPOSITORY = Path(__file__).resolve().parents[1]
if str(REPOSITORY) not in sys_path:
    sys_path.insert(0, str(REPOSITORY))

from engram.constants import QueryOperator
from engram.identity import scope_key, scoped_retrieval_key_from_text, trusted_scoped_retrieval_key_signature
from engram.rewrite import RewriteEngine, load_default_rewrite_corpus
from scripts.benchmark_metadata import benchmark_source_state, recorded_at

DEFAULT_OUTPUT = Path("eval/results/rewrite/benchmark.json")
# Every case, gate and corpus field the report reads; a corpus lacking one is refused.
CASE_FIELDS = {"case_id", "category", "input", "expected_final", "operator", "subject", "inherited_subject", "should_rewrite"}
GATE_FIELDS = {"minimum_recall_gain", "maximum_semantic_collision_rate", "maximum_false_direct_answer_rate"}
CORPUS_METADATA_FIELDS = {"corpus_id", "frozen_at", "provenance"}


def internal_percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    result = ordered[min(len(ordered) - 1, max(0, int((len(ordered) - 1) * fraction)))]
    return result


def internal_load(path: Path) -> dict:
    decoded = json_loads(path.read_text(encoding="utf-8"))
    if not isinstance(decoded, dict):
        raise ValueError("rewrite benchmark corpus must be an object")
    if (
        "cases" not in decoded
        or "gates" not in decoded
        or not isinstance(decoded.get("cases", []), list)
        or not isinstance(decoded.get("gates", {}), dict)
    ):
        raise ValueError("rewrite benchmark corpus must contain cases and gates")
    return decoded


def benchmark(corpus_path: Path, repeats: int = 100) -> dict:
    if repeats < 30:
        raise ValueError("rewrite benchmark requires at least 30 repeats")
    corpus = internal_load(corpus_path)
    cases = corpus.get("cases", [])
    gates = corpus.get("gates", {})
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
    category_counts = Counter()
    for raw_case in cases:
        if not isinstance(raw_case, dict):
            raise ValueError("rewrite benchmark cases must be objects")
        case = raw_case
        missing_fields = sorted(CASE_FIELDS - set(case))
        if missing_fields:
            raise ValueError(f"rewrite benchmark case is missing fields: {', '.join(missing_fields)}")
        case_id = str(case.get("case_id", ""))
        operator = QueryOperator(str(case.get("operator", "")))
        execution = engine.rewrite(
            str(case.get("input", "")),
            operator=operator,
            subject=str(case.get("subject", "")),
            inherited_subject=case.get("inherited_subject", False) is True,
        )
        final_text = execution.get("final_text", "")
        chain = execution.get("chain", ())
        expected = str(case.get("expected_final", ""))
        expected_key = scoped_retrieval_key_from_text(selected_scope, expected)
        baseline_key = scoped_retrieval_key_from_text(selected_scope, str(case.get("input", "")))
        rewritten_key = scoped_retrieval_key_from_text(selected_scope, final_text)
        should_rewrite = case.get("should_rewrite", False) is True
        if should_rewrite:
            positive += 1
            category_counts[str(case.get("category", ""))] += 1
            baseline_hits += baseline_key == expected_key
            rewritten_hits += rewritten_key == expected_key
            signature = trusted_scoped_retrieval_key_signature(expected_key)
            prior = final_owners.get(signature, "")
            if prior and prior != case_id:
                semantic_collisions += 1
            final_owners[signature] = case_id
        elif rewritten_key != baseline_key:
            false_direct_answers += 1
        results.append(
            {
                "case_id": case_id,
                "category": case.get("category", ""),
                "passed": final_text == expected and bool(chain) == should_rewrite,
                "baseline_exact_hit": baseline_key == expected_key if should_rewrite else False,
                "rewritten_exact_hit": rewritten_key == expected_key if should_rewrite else False,
                "rules": [step[0] for step in chain],
                # The stop reason is a StrEnum, so its string form is its value.
                "stop_reason": str(execution.get("stop_reason", "")),
            }
        )
    for _ in range(repeats):
        for raw_case in cases:
            if not isinstance(raw_case, dict):
                continue
            started = time_perf_counter_ns()
            engine.rewrite(
                str(raw_case.get("input", "")),
                operator=QueryOperator(str(raw_case.get("operator", ""))),
                subject=str(raw_case.get("subject", "")),
                inherited_subject=raw_case.get("inherited_subject", False) is True,
            )
            latencies_ms.append((time_perf_counter_ns() - started) / 1_000_000)
    negatives = len(cases) - positive
    baseline_recall = baseline_hits / positive
    rewritten_recall = rewritten_hits / positive
    recall_gain = rewritten_recall - baseline_recall
    collision_rate = semantic_collisions / positive
    false_rate = false_direct_answers / negatives
    missing_gates = sorted(GATE_FIELDS - set(gates))
    missing_metadata = sorted(CORPUS_METADATA_FIELDS - set(corpus))
    if missing_gates or missing_metadata:
        raise ValueError(f"rewrite benchmark corpus is missing fields: {', '.join(missing_metadata + missing_gates)}")
    verdicts = {
        "all_cases_pass": all(bool(result.get("passed", False)) for result in results),
        "recall_gain": recall_gain >= float(gates.get("minimum_recall_gain", 0.0)),
        "semantic_collision_rate": collision_rate <= float(gates.get("maximum_semantic_collision_rate", 0.0)),
        "false_direct_answer_rate": false_rate <= float(gates.get("maximum_false_direct_answer_rate", 0.0)),
    }
    result = {
        "created_at": recorded_at(),
        "source_state": benchmark_source_state(),
        "corpus": {
            "path": corpus_path.as_posix(),
            "corpus_id": corpus.get("corpus_id", ""),
            "frozen_at": corpus.get("frozen_at", ""),
            "partition": corpus.get("provenance", {}),
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
            "p50_ms": statistics_median(latencies_ms),
            "p95_ms": internal_percentile(latencies_ms, 0.95),
            "p99_ms": internal_percentile(latencies_ms, 0.99),
            "maximum_ms": max(latencies_ms),
            "timing_gate_applied": False,
        },
        "gates": gates,
        "verdicts": verdicts,
        "passed": all(verdicts.values()),
        "cases": results,
        "release_authority": "component capability evidence only; release qualification owns the release decision",
    }
    return result


def main() -> int:
    parser = argparse_ArgumentParser(description=__doc__)
    # The tree carries no Section 11 corpus, so the input is always named explicitly.
    parser.add_argument("--corpus", type=Path, required=True)
    parser.add_argument("--repeats", type=int, default=100)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    report = benchmark(args.corpus, args.repeats)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json_dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    passed = report.get("passed", False)
    accuracy = report.get("accuracy", {})
    print(json_dumps({"passed": passed, "accuracy": accuracy, "latency": report.get("latency_observation", {})}))
    result = 0 if passed else 1
    return result


if __name__ == "__main__":
    raise SystemExit(main())
