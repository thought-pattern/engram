"""Run the frozen Section 8 relation/follow-up development and held-out corpus."""

from argparse import ArgumentParser as argparse_ArgumentParser
from datetime import UTC, datetime
from json import dumps as json_dumps, loads as json_loads
from pathlib import Path
from platform import python_version as platform_python_version
from statistics import median as statistics_median
from sys import path as sys_path
from time import perf_counter_ns as time_perf_counter_ns

REPOSITORY = Path(__file__).resolve().parents[1]
if str(REPOSITORY) not in sys_path:
    sys_path.insert(0, str(REPOSITORY))

from engram.constants import VERSION, ExpectedObjectType, ResolutionOutcome
from engram.core import Engram
from engram.graph import PropositionProjectionQuery, proposition_projection, relation_proposition_projection_from_graph_row
from engram.identity import normalize_retrieval_key
from engram.service import EngramCore
from engram.spacy_setup import get_nlp
from scripts.benchmark_metadata import benchmark_source_state

DEFAULT_MANIFEST = Path("eval/section8-relation-followup-v1.json")
DEFAULT_OUTPUT = Path("eval/results/contextual/benchmark-2026-08-20.json")


def proposition_row(proposition_id: str, subject_id: str, predicate_id: str, object_id: str) -> dict[str, object]:
    return {
        "proposition_id": proposition_id,
        "subject_entity_id": subject_id,
        "predicate_id": predicate_id,
        "object_entity_id": object_id,
        "invalidated_at": "",
        "invalidated_at_available": False,
        "system_from": "2026-01-01T00:00:00Z",
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
        "predicate_cardinality": "SINGLE",
    }


class BenchmarkGraph:
    """Exact in-memory stand-in for the three fixed Section 8 graph capabilities."""

    available = True

    def __init__(self) -> None:
        self.one_hop_calls: list[tuple[str, str, int]] = []
        self.entities = (
            ("entity:ada-lovelace", "Ada Lovelace", ("Ada", "Augusta Ada King"), ("Lovelace",), "PERSON"),
            ("entity:grace-hopper", "Grace Hopper", ("Grace",), ("Hopper",), "PERSON"),
            ("entity:rfc-7231", "RFC 7231", ("RFC7231",), (), "ENTITY"),
            ("entity:london", "London", (), (), "PLACE"),
            ("entity:springfield-il", "Springfield", (), (), "PLACE"),
            ("entity:springfield-ma", "Springfield", (), (), "PLACE"),
        )
        self.predicates = (
            ("predicate:birth-place", "birth place", ("born", "bear", "come", "come into world"), "PLACE"),
            ("predicate:birth-date", "birth date", ("born", "bear", "come", "come into world"), "DATE"),
            ("predicate:employer", "employer", ("work", "work at", "serve", "serve at"), "PLACE"),
            ("predicate:designed", "designed", ("design",), "ENTITY"),
            ("predicate:authored", "authored", ("design",), "ENTITY"),
            ("predicate:supersedes", "supersedes", ("supersede",), "ENTITY"),
            ("predicate:uses", "uses", ("use",), "UNKNOWN"),
            ("predicate:located", "located", ("locate",), "PLACE"),
        )
        definitions = (
            ("proposition:ada-place", "entity:ada-lovelace", "predicate:birth-place", "entity:london", "London", "PLACE"),
            ("proposition:ada-date", "entity:ada-lovelace", "predicate:birth-date", "entity:1815", "1815", "DATE"),
            (
                "proposition:ada-employer",
                "entity:ada-lovelace",
                "predicate:employer",
                "entity:bletchley-park",
                "Bletchley Park",
                "PLACE",
            ),
            (
                "proposition:rfc-superseded",
                "entity:rfc-7231",
                "predicate:supersedes",
                "entity:rfc-9110",
                "RFC 9110",
                "ENTITY",
            ),
            (
                "proposition:ada-uses",
                "entity:ada-lovelace",
                "predicate:uses",
                "entity:analytical-engine",
                "Analytical Engine",
                "UNKNOWN",
            ),
        )
        self.results = {
            proposition_id: relation_proposition_projection_from_graph_row(
                {
                    **proposition_row(proposition_id, subject_id, predicate_id, object_id),
                    "object_label": object_label,
                    "object_type": object_type,
                }
            )
            for proposition_id, subject_id, predicate_id, object_id, object_label, object_type in definitions
        }

    def canonical_entity_matches(self, surface: str, *, limit: int):
        normalized = normalize_retrieval_key(surface)
        values = []
        for canonical_id, label, aliases, edges, entity_type in self.entities:
            known = {normalize_retrieval_key(value) for value in (label, *aliases, *edges)}
            if normalized in known:
                values.append(
                    {
                        "canonical_id": canonical_id,
                        "primary_label": label,
                        "aliases": aliases,
                        "edge_surfaces": edges,
                        "entity_type": ExpectedObjectType(entity_type),
                    }
                )
        result = values[:limit]
        return result

    def canonical_predicate_matches(self, surface: str, *, limit: int):
        normalized = normalize_retrieval_key(surface)
        values = []
        for canonical_id, label, synonyms, object_type in self.predicates:
            known = {normalize_retrieval_key(value) for value in (canonical_id, label, *synonyms)}
            if normalized in known:
                values.append(
                    {
                        "canonical_id": canonical_id,
                        "primary_label": label,
                        "synonyms": synonyms,
                        "object_type": ExpectedObjectType(object_type),
                    }
                )
        result = values[:limit]
        return result

    def relation_one_hop_proposition_projections(
        self,
        subject_entity_id: str,
        predicate_id: str,
        *,
        limit: int,
        include_historical: bool = False,
    ):
        del include_historical
        self.one_hop_calls.append((subject_entity_id, predicate_id, limit))
        result = [
            result
            for result in self.results.values()
            if result["projection"]["subject_entity_id"] == subject_entity_id
            and result["projection"]["predicate_id"] == predicate_id
        ][:limit]
        return result

    def proposition_projection_by_id(self, proposition_id: str):
        result = self.results.get(proposition_id)
        if not result:
            return []
        values = dict(result["projection"])
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
        result = [proposition_projection(**values)]
        return result


def internal_percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    index = min(len(ordered) - 1, round((len(ordered) - 1) * fraction))
    result = ordered[index]
    return result


def validate_manifest(value: object) -> dict:
    if not isinstance(value, dict) or set(value) != {"schema_version", "benchmark_id", "authored_at", "partitions"}:
        raise ValueError("benchmark manifest has invalid top-level fields")
    if value["schema_version"] != 1 or value["benchmark_id"] != "section8-relation-followup-v1":
        raise ValueError("benchmark manifest identity is unsupported")
    partitions = value["partitions"]
    if not isinstance(partitions, dict) or set(partitions) != {"development", "held_out"}:
        raise ValueError("benchmark manifest partitions are invalid")
    for cases in partitions.values():
        if not isinstance(cases, list) or not cases:
            raise ValueError("each benchmark partition must contain cases")
        for case in cases:
            if not isinstance(case, dict) or set(case) != {"id", "category", "turns"} or not case["turns"]:
                raise ValueError("benchmark case is malformed")
            for turn in case["turns"]:
                if not isinstance(turn, dict) or set(turn) != {
                    "text",
                    "expected_outcome",
                    "response_contains",
                    "inheritance_fields",
                }:
                    raise ValueError("benchmark turn is malformed")
    return value


def run(manifest_path: Path) -> dict:
    manifest = validate_manifest(json_loads(manifest_path.read_text(encoding="utf-8")))
    if not get_nlp():
        raise RuntimeError("Section 8 benchmark requires the pre-provisioned spaCy model")
    partition_reports = {}
    all_latencies = []
    all_failures = []
    for partition_name, cases in manifest["partitions"].items():
        case_reports = []
        for case in cases:
            engine = Engram()
            graph = BenchmarkGraph()
            engine.internal_graph_client = graph
            core = EngramCore(engine)
            turn_reports = []
            for turn_index, turn in enumerate(case["turns"], 1):
                request_id = f"benchmark:{partition_name}:{case['id']}:{turn_index}"
                started = time_perf_counter_ns()
                result = core.resolve_request(
                    turn["text"],
                    request_id,
                    user_id=f"benchmark:{case['id']}",
                    namespace="benchmark",
                    configured_resolvers=("structured_graph",),
                )
                latency_ms = (time_perf_counter_ns() - started) / 1_000_000
                all_latencies.append(latency_ms)
                frame = core.resolution_requests.get(request_id, {}).get("frame", {})
                if not isinstance(frame, dict):
                    raise RuntimeError("benchmark resolution frame is malformed")
                observed_inheritance = sorted(item["field_name"] for item in frame["inheritance"])
                responses = [candidate["response"] for candidate in result.get("response_candidates", [])]
                if result.get("selected_candidate_available", False):
                    responses.append(result.get("selected_candidate", {})["response"])
                checks = {
                    "outcome": result.get("outcome", ResolutionOutcome.MISS).value == turn["expected_outcome"],
                    "response": not turn["response_contains"]
                    or any(turn["response_contains"] in response for response in responses),
                    "inheritance": observed_inheritance == sorted(turn["inheritance_fields"]),
                    "bounded_plan": all(1 <= call[2] <= 10 for call in graph.one_hop_calls),
                }
                failures = sorted(name for name, passed in checks.items() if not passed)
                all_failures.extend(f"{partition_name}:{case['id']}:{turn_index}:{name}" for name in failures)
                turn_reports.append(
                    {
                        "turn": turn_index,
                        "passed": not failures,
                        "failed_checks": failures,
                        "outcome": result.get("outcome", ResolutionOutcome.MISS).value,
                        "inheritance_fields": observed_inheritance,
                        "one_hop_calls": len(graph.one_hop_calls),
                        "latency_ms": round(latency_ms, 6),
                    }
                )
            case_reports.append(
                {
                    "id": case["id"],
                    "category": case["category"],
                    "passed": all(turn["passed"] for turn in turn_reports),
                    "turns": turn_reports,
                }
            )
        partition_reports[partition_name] = {
            "cases": len(case_reports),
            "passed": sum(case["passed"] for case in case_reports),
            "results": case_reports,
        }
    result = {
        "schema_version": 1,
        "benchmark_id": manifest["benchmark_id"],
        "manifest_authored_at": manifest["authored_at"],
        "executed_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "engram_version": VERSION,
        "python_version": platform_python_version(),
        "source": benchmark_source_state(),
        "passed": not all_failures,
        "failures": all_failures,
        "partitions": partition_reports,
        "latency_ms": {
            "samples": len(all_latencies),
            "median": round(statistics_median(all_latencies), 6),
            "p95": round(internal_percentile(all_latencies, 0.95), 6),
            "maximum": round(max(all_latencies), 6),
        },
    }
    return result


def main() -> int:
    parser = argparse_ArgumentParser()
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    report = run(args.manifest)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json_dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json_dumps({"passed": report["passed"], "failures": report["failures"], "latency_ms": report["latency_ms"]}))
    result = 0 if report["passed"] else 1
    return result


if __name__ == "__main__":
    raise SystemExit(main())
