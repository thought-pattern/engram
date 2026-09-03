"""Evaluate the frozen Section 10 algebra corpus and report observed durations."""

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

from engram.composition import composition_plan, composition_step, execute_composition_plan
from engram.constants import ExpectedObjectType, GraphCompositionOperator
from engram.core import Engram
from engram.evidence import PropositionEligibilityEvaluator
from engram.graph import PropositionProjectionQuery, proposition_projection, relation_proposition_projection_from_graph_row
from engram.identity import scope_key
from engram.resolution import QueryFrameBuilder
from scripts.benchmark_metadata import benchmark_source_state

NOW = datetime(2026, 8, 20, 16, 0, tzinfo=UTC)
AGGREGATES = {
    GraphCompositionOperator.COUNT,
    GraphCompositionOperator.MIN,
    GraphCompositionOperator.MAX,
    GraphCompositionOperator.ORDER,
}


def positive_integer(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{name} must be a positive integer")
    return value


def internal_percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    result = ordered[min(len(ordered) - 1, max(0, int((len(ordered) - 1) * fraction)))]
    return result


def internal_relation(raw: list[object]):
    if len(raw) != 7 or not all(isinstance(value, str) for value in raw):
        raise ValueError("composition corpus Propositions must be seven-string lists")
    proposition_id, subject_id, predicate_id, object_id, label, object_type, cardinality = raw
    result = relation_proposition_projection_from_graph_row(
        {
            "proposition_id": proposition_id,
            "subject_entity_id": subject_id,
            "predicate_id": predicate_id,
            "object_entity_id": object_id,
            "invalidated_at": "",
            "invalidated_at_available": False,
            "system_from": "2020-01-01T00:00:00Z",
            "system_from_available": True,
            "system_to": "",
            "system_to_available": False,
            "valid_from": "",
            "valid_from_available": False,
            "valid_to": "",
            "valid_to_available": False,
            "predicate_canonical": True,
            "ownership_category": "PUBLIC",
            "trust_category": "source_supplied",
            "trust_category_available": True,
            "supplied_trust": 0.8,
            "supplied_trust_available": True,
            "supplied_trust_version": 1,
            "supplied_trust_version_available": True,
            "structured_match": 1.0,
            "structured_match_available": True,
            "semantic_similarity": 0.0,
            "semantic_similarity_available": False,
            "object_label": label,
            "object_type": object_type,
            "predicate_cardinality": cardinality,
        }
    )
    return result


def internal_current(item):
    values = dict(item["projection"])
    values.update(
        {
            "projection_id": PropositionProjectionQuery.BY_ID_V1,
            "structured_match": 0.0,
            "structured_match_available": False,
            "semantic_similarity": 0.0,
            "semantic_similarity_available": False,
            "vector_index_id": "",
            "vector_index_id_available": False,
        }
    )
    result = proposition_projection(**values)
    return result


def internal_branches(case: dict[str, object]) -> list[list[list[str]]]:
    raw = case.get("branches", [])
    if raw is None:
        raw = [case.get("predicates", {})]
    if not isinstance(raw, list) or not raw:
        raise ValueError("composition corpus case must declare branches or predicates")
    result = []
    for branch in raw:
        if not isinstance(branch, list) or not branch:
            raise ValueError("composition corpus branches must be non-empty lists")
        predicates = []
        for predicate in branch:
            if not isinstance(predicate, list) or len(predicate) != 3 or not all(isinstance(value, str) for value in predicate):
                raise ValueError("composition corpus predicates must be three-string lists")
            predicates.append(predicate)
        result.append(predicates)
    return result


def internal_plan(case: dict[str, object]):
    operator = GraphCompositionOperator(str(case.get("operator", "")))
    branches = internal_branches(case)
    candidate_limit = positive_integer(case.get("max_candidates", 4), "max_candidates")
    steps = []
    for branch_index, predicates in enumerate(branches):
        prior_binding = "$root"
        for hop, (predicate_id, label, object_type) in enumerate(predicates):
            output_binding = "$result" if hop == len(predicates) - 1 else f"$b{branch_index}h{hop + 1}"
            steps.append(
                composition_step(
                    branch_index,
                    hop,
                    prior_binding,
                    "entity:root" if hop == 0 else "",
                    predicate_id,
                    label,
                    output_binding,
                    ExpectedObjectType(object_type),
                    candidate_limit,
                )
            )
            prior_binding = output_binding
    result = composition_plan(
        operator,
        "entity:root",
        "Root",
        tuple(steps),
        "$result",
        aggregation_inputs=("$result",) if operator in AGGREGATES else (),
        max_rows=positive_integer(case.get("max_rows", 64), "max_rows"),
        max_branches=len(branches),
        max_candidates_per_step=candidate_limit,
    )
    return result


def run_case(case: dict[str, object], frame, evaluator: PropositionEligibilityEvaluator) -> dict[str, object]:
    started = time_perf_counter_ns()
    plan = internal_plan(case)
    raw_propositions = case.get("propositions", [])
    if not isinstance(raw_propositions, list):
        raise ValueError("composition corpus propositions must be a list")
    propositions = tuple(internal_relation(value) for value in raw_propositions if isinstance(value, list))
    if len(propositions) != len(raw_propositions):
        raise ValueError("composition corpus Proposition entries must be lists")
    rows = {}
    current = {}
    for item in propositions:
        projection = item["projection"]
        rows.setdefault((projection["subject_entity_id"], projection["predicate_id"]), []).append(item)
        current[projection["proposition_id"]] = internal_current(item)
    raw_failures = case.get("fail_queries", [])
    if not isinstance(raw_failures, list) or not all(isinstance(value, str) for value in raw_failures):
        raise ValueError("composition corpus fail_queries must be a string list")
    failures = set(raw_failures)

    def query(subject_id: str, predicate_id: str, limit: int):
        if f"{subject_id}|{predicate_id}" in failures:
            raise RuntimeError("injected dependency failure")
        result = rows.get((subject_id, predicate_id), [])[:limit]
        return result

    execution = execute_composition_plan(
        plan,
        query,
        lambda projection: evaluator.evaluate(projection, frame),
        lambda projection: evaluator.revalidate(
            projection,
            frame,
            lambda proposition_id: (current.get(proposition_id, {}),),
        ),
    )
    elapsed_ms = (time_perf_counter_ns() - started) / 1_000_000
    observed = {
        "direct": execution["direct_result"],
        "truth_available": execution["truth_available"],
        "truth": execution["truth_value"],
        "aggregate": execution["aggregate_value"],
        "terminal_ids": list(execution["terminal_entity_ids"]),
        "complete_paths": len(execution["complete_paths"]),
        "partial_paths": len(execution["partial_paths"]),
        "truncated": execution["truncated"],
        "reasons": [reason.value for reason in execution["reasons"]],
        "graph_rows": execution["graph_rows"],
        "complete_proposition_paths": [
            [entry["proposition"]["projection"]["proposition_id"] for entry in path] for path in execution["complete_paths"]
        ],
        "partial_proposition_paths": [
            [entry["proposition"]["projection"]["proposition_id"] for entry in path] for path in execution["partial_paths"]
        ],
    }
    raw_expected = case.get("expected", {})
    if not isinstance(raw_expected, dict) or not all(isinstance(name, str) for name in raw_expected):
        raise ValueError("composition corpus expected value must be a string-keyed object")
    expected = raw_expected
    expected_reasons = expected.get("reasons")
    if not isinstance(expected_reasons, list) or not all(isinstance(reason, str) for reason in expected_reasons):
        raise ValueError("composition corpus expected reasons must be a string list")
    comparable = {name: observed.get(name, False) for name in expected if name != "reasons"}
    reasons_match = set(expected_reasons).issubset(observed.get("reasons", []))
    passed = comparable == {name: value for name, value in expected.items() if name != "reasons"} and reasons_match
    bounded = (
        1 <= plan["max_hops"] <= 2
        and 1 <= plan["max_rows"] <= 64
        and 1 <= plan["max_branches"] <= 4
        and 1 <= plan["max_candidates_per_step"] <= 8
        and 1 <= plan["max_path_propositions"] <= 2
        and execution["graph_rows"] <= plan["max_rows"]
    )
    useful_evidence = execution["direct_result"] or bool(execution["complete_paths"] or execution["partial_paths"])
    result = {
        "id": case.get("id", ""),
        "passed": passed,
        "bounded": bounded,
        "useful_evidence": useful_evidence,
        "elapsed_ms": elapsed_ms,
        "expected": expected,
        "observed": observed,
    }
    return result


def run(corpus_path: Path) -> dict[str, object]:
    corpus = json_loads(corpus_path.read_text(encoding="utf-8"))
    frame = QueryFrameBuilder(Engram(), lambda: 1, lambda: NOW).build(
        "What is connected to the root?",
        scope_key(namespace="public"),
    )
    evaluator = PropositionEligibilityEvaluator()
    results = []
    for split in ("development", "held_out"):
        raw_cases = corpus[split]
        if not isinstance(raw_cases, list):
            raise ValueError("composition corpus splits must be lists")
        for raw_case in raw_cases:
            if not isinstance(raw_case, dict):
                raise ValueError("composition corpus cases must be objects")
            case = run_case(raw_case, frame, evaluator)
            case["split"] = split
            results.append(case)
    durations = [float(case["elapsed_ms"]) for case in results]
    held_out = [case for case in results if case["split"] == "held_out"]
    abstentions = [case for case in results if not case["observed"]["direct"]]
    result = {
        "schema_version": 1,
        "corpus": corpus["name"],
        "generated_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "source": benchmark_source_state(),
        "passed": all(case["passed"] and case["bounded"] for case in results),
        "case_count": len(results),
        "passed_count": sum(case["passed"] for case in results),
        "held_out_accuracy": sum(case["passed"] for case in held_out) / len(held_out),
        "zero_unbounded_execution": all(case["bounded"] for case in results),
        "useful_evidence_on_abstention": all(case["useful_evidence"] for case in abstentions),
        "duration_ms": {
            "median": statistics_median(durations),
            "p95": internal_percentile(durations, 0.95),
            "maximum": max(durations),
            "timing_gate": False,
        },
        "cases": results,
    }
    return result


def main() -> None:
    parser = argparse_ArgumentParser()
    parser.add_argument("--corpus", type=Path, default=Path("eval/section10-composition-v1.json"))
    parser.add_argument("--output", type=Path)
    arguments = parser.parse_args()
    report = run(arguments.corpus)
    if arguments.output:
        arguments.output.parent.mkdir(parents=True, exist_ok=True)
        arguments.output.write_text(json_dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json_dumps(report, sort_keys=True))
    if not report["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
