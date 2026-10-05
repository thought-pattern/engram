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

from engram.constants import UNCONSTRAINED_ASSERTION_BASIS, VERSION, ExpectedObjectType, ResolutionOutcome
from engram.core import Engram
from engram.graph import PropositionProjectionQuery, proposition_projection, relation_proposition_projection_from_graph_row
from engram.identity import normalize_retrieval_key
from engram.service import EngramCore
from engram.spacy_setup import SPACY_PIPELINES
from scripts.benchmark_metadata import benchmark_source_state

DEFAULT_OUTPUT = Path("eval/results/contextual/benchmark.json")


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
                    "proposition_id": proposition_id,
                    "subject_entity_id": subject_id,
                    "predicate_id": predicate_id,
                    "object_entity_id": object_id,
                    "polarity": "positive",
                    "modality_family": "none",
                    "modality_operator": "none",
                    "argument_count": 2,
                    "qualification_count": 0,
                    "context_count": 0,
                    "applicability_count": 0,
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
                    "structured_match": 1.0,
                    "structured_match_available": True,
                    "semantic_similarity": 0.0,
                    "semantic_similarity_available": False,
                    "predicate_cardinality": "SINGLE",
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
        basis_window: dict = UNCONSTRAINED_ASSERTION_BASIS,
    ):
        del include_historical, basis_window
        self.one_hop_calls.append((subject_entity_id, predicate_id, limit))
        result = [
            result
            for result in self.results.values()
            if result.get("projection", {}).get("subject_entity_id", "") == subject_entity_id
            and result.get("projection", {}).get("predicate_id", "") == predicate_id
        ][:limit]
        return result

    def proposition_projection_by_id(self, proposition_id: str, basis_window: dict = UNCONSTRAINED_ASSERTION_BASIS):
        del basis_window
        stored = self.results.get(proposition_id, {})
        if not stored:
            return []
        values = dict(stored.get("projection", {}))
        values.update(
            {
                "projection_id": PropositionProjectionQuery.BY_ID,
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
    if not isinstance(value, dict) or set(value) != {"benchmark_id", "authored_at", "partitions"}:
        raise ValueError("benchmark manifest has invalid top-level fields")
    if value.get("benchmark_id", "") != "section8-relation-followup":
        raise ValueError("benchmark manifest identity is unsupported")
    partitions = value.get("partitions", {})
    if not isinstance(partitions, dict) or set(partitions) != {"development", "held_out"}:
        raise ValueError("benchmark manifest partitions are invalid")
    for cases in partitions.values():
        if not isinstance(cases, list) or not cases:
            raise ValueError("each benchmark partition must contain cases")
        for case in cases:
            if not isinstance(case, dict) or set(case) != {"id", "category", "turns"} or not case.get("turns", []):
                raise ValueError("benchmark case is malformed")
            for turn in case.get("turns", []):
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
    if not SPACY_PIPELINES.pipeline():
        raise RuntimeError("Section 8 benchmark requires the pre-provisioned spaCy model")
    partition_reports = {}
    all_latencies = []
    all_failures = []
    for partition_name, cases in manifest.get("partitions", {}).items():
        case_reports = []
        for case in cases:
            case_id = case.get("id", "")
            engine = Engram()
            graph = BenchmarkGraph()
            engine.internal_graph_client = graph
            core = EngramCore(engine)
            turn_reports = []
            for turn_index, turn in enumerate(case.get("turns", []), 1):
                request_id = f"benchmark:{partition_name}:{case_id}:{turn_index}"
                started = time_perf_counter_ns()
                result = core.resolve_request(
                    turn.get("text", ""),
                    request_id,
                    user_id=f"benchmark:{case_id}",
                    namespace="benchmark",
                    configured_resolvers=("structured_graph",),
                )
                latency_ms = (time_perf_counter_ns() - started) / 1_000_000
                all_latencies.append(latency_ms)
                frame = core.resolution_requests.get(request_id, {}).get("frame", {})
                if not isinstance(frame, dict) or "inheritance" not in frame:
                    raise RuntimeError("benchmark resolution frame is malformed")
                observed_inheritance = sorted(item.get("field_name", "") for item in frame.get("inheritance", []))
                responses = [candidate.get("response", "") for candidate in result.get("response_candidates", [])]
                if result.get("selected_candidate_available", False):
                    responses.append(result.get("selected_candidate", {}).get("response", ""))
                response_contains = turn.get("response_contains", "")
                checks = {
                    "outcome": result.get("outcome", ResolutionOutcome.MISS).value == turn.get("expected_outcome", ""),
                    "response": not response_contains or any(response_contains in response for response in responses),
                    "inheritance": observed_inheritance == sorted(turn.get("inheritance_fields", [])),
                    "bounded_plan": all(1 <= call[2] <= 10 for call in graph.one_hop_calls),
                }
                failures = sorted(name for name, passed in checks.items() if not passed)
                all_failures.extend(f"{partition_name}:{case_id}:{turn_index}:{name}" for name in failures)
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
                    "id": case_id,
                    "category": case.get("category", ""),
                    "passed": all(turn_report.get("passed", False) for turn_report in turn_reports),
                    "turns": turn_reports,
                }
            )
        partition_reports[partition_name] = {
            "cases": len(case_reports),
            "passed": sum(case_report.get("passed", False) for case_report in case_reports),
            "results": case_reports,
        }
    result = {
        "benchmark_id": manifest.get("benchmark_id", ""),
        "manifest_authored_at": manifest.get("authored_at", ""),
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
    # The tree carries no Section 8 relation follow-up manifest, so the input is always named explicitly.
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    report = run(args.manifest)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json_dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    passed = report.get("passed", False)
    print(json_dumps({"passed": passed, "failures": report.get("failures", []), "latency_ms": report.get("latency_ms", {})}))
    result = 0 if passed else 1
    return result


if __name__ == "__main__":
    raise SystemExit(main())
