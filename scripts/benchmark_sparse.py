"""Compare Section 12 sparse engines and measure artifact-local retrieval."""

from argparse import ArgumentParser as argparse_ArgumentParser
from collections import Counter, defaultdict
from json import dumps as json_dumps, loads as json_loads
from math import log as math_log
from pathlib import Path
from sqlite3 import connect as sqlite3_connect
from statistics import median as statistics_median
from sys import path as sys_path
from time import perf_counter_ns as time_perf_counter_ns
from tracemalloc import get_traced_memory as tracemalloc_get_traced_memory, start as tracemalloc_start, stop as tracemalloc_stop

REPOSITORY = Path(__file__).resolve().parents[1]
if str(REPOSITORY) not in sys_path:
    sys_path.insert(0, str(REPOSITORY))

from engram.artifacts import LifecycleState, artifact_provenance, artifact_statistics, cached_response_artifact
from engram.config import sparse_config
from engram.constants import Tier
from engram.identity import build_retrieval_representation, build_standalone_identity, scope_key
from engram.sparse import SparseIndex, search_sparse_artifacts, sparse_document_from_artifact, sparse_tokens
from scripts.benchmark_metadata import benchmark_source_state, recorded_at

DEFAULT_OUTPUT = Path("eval/results/sparse/benchmark.json")
SCALE_DOCUMENTS = 10_000
SCALE_QUERY_SAMPLES = 200


def internal_percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(round((len(ordered) - 1) * fraction))))
    result = ordered[index]
    return result


def internal_measure(operation: object, samples: int) -> dict:
    """Time ``operation``, a native sparse search, counting refused searches instead of timing them."""
    if not callable(operation):
        raise ValueError("benchmark operation must be callable")
    durations = []
    incomplete: Counter[str] = Counter()
    for _ in range(samples):
        started = time_perf_counter_ns()
        outcome = operation()
        elapsed = (time_perf_counter_ns() - started) / 1_000_000
        if outcome.get("complete", False):
            durations.append(elapsed)
        else:
            incomplete[outcome.get("reason", "")] += 1
    if not durations:
        raise ValueError(f"no sparse benchmark sample completed: {dict(incomplete)}")
    result = {
        "samples": len(durations),
        "incomplete_samples": sum(incomplete.values()),
        "incomplete_reasons": dict(sorted(incomplete.items())),
        "minimum_ms": min(durations),
        "p50_ms": statistics_median(durations),
        "p95_ms": internal_percentile(durations, 0.95),
        "p99_ms": internal_percentile(durations, 0.99),
        "maximum_ms": max(durations),
    }
    return result


def internal_load(path: Path) -> dict:
    decoded = json_loads(path.read_text(encoding="utf-8"))
    if not isinstance(decoded, dict):
        raise ValueError("sparse benchmark corpus must be an object")
    if not isinstance(decoded.get("documents"), list) or not isinstance(decoded.get("queries"), list):
        raise ValueError("sparse benchmark corpus must contain document and query arrays")
    if not isinstance(decoded.get("gates"), dict):
        raise ValueError("sparse benchmark corpus must contain gates")
    return decoded


def internal_artifact(raw: dict, namespace: str) -> dict:
    request = str(raw.get("request", ""))
    aliases_value = raw.get("aliases", [])
    if not isinstance(aliases_value, list):
        raise ValueError("sparse document aliases must be an array")
    selected_scope = scope_key(namespace=namespace)
    result = cached_response_artifact(
        statement_id=str(raw.get("statement_id", "")),
        generation=1,
        response=str(raw.get("response", "")),
        query_identity=build_standalone_identity(request, selected_scope),
        retrieval=build_retrieval_representation(request, tuple(str(value) for value in aliases_value)),
        tier=Tier.STATIC,
        lifecycle=LifecycleState.ACTIVE,
        scope=selected_scope,
        support_references=(),
        valid_from="",
        valid_from_available=False,
        valid_until="",
        valid_until_available=False,
        superseded_by="",
        provenance=artifact_provenance("section12:benchmark", "engineering", "2026-08-21T00:00:00Z"),
        statistics=artifact_statistics(),
        metadata={},
    )
    return result


def internal_unfielded_bm25(artifacts: tuple[dict, ...]) -> object:
    documents = {
        artifact["statement_id"]: tuple(
            token
            for text in (artifact["retrieval"]["canonical"], *artifact["retrieval"]["aliases"])
            for token in sparse_tokens(text)
        )
        for artifact in artifacts
    }
    frequencies = {statement_id: Counter(tokens) for statement_id, tokens in documents.items()}
    document_frequencies: Counter[str] = Counter()
    for tokens in documents.values():
        document_frequencies.update(set(tokens))
    average_length = sum(len(tokens) for tokens in documents.values()) / max(1, len(documents))

    def search(text: str, limit: int) -> dict:
        query = tuple(dict.fromkeys(sparse_tokens(text)))
        scored = []
        for statement_id, term_frequencies in frequencies.items():
            score = 0.0
            for token in query:
                frequency = term_frequencies[token]
                if not frequency:
                    continue
                df = document_frequencies[token]
                inverse = math_log(1.0 + (len(documents) - df + 0.5) / (df + 0.5))
                denominator = frequency + 1.2 * (0.25 + 0.75 * len(documents[statement_id]) / average_length)
                score += inverse * frequency * 2.2 / denominator
            if score:
                scored.append((statement_id, score))
        scored.sort(key=lambda value: (-value[1], value[0]))
        result = {"complete": True, "reason": "", "ranking": scored[:limit]}
        return result

    return search


def sqlite_fts(artifacts: tuple[dict, ...]) -> tuple[object, int]:
    connection = sqlite3_connect(":memory:")
    connection.execute("CREATE VIRTUAL TABLE sparse_documents USING fts5(statement_id UNINDEXED, content)")
    for artifact in artifacts:
        document = sparse_document_from_artifact(artifact)
        content = " ".join(text for values in document["fields"].values() for text in values)
        connection.execute("INSERT INTO sparse_documents(statement_id, content) VALUES (?, ?)", (artifact["statement_id"], content))
    connection.commit()
    page_size = int(connection.execute("PRAGMA page_size").fetchone()[0])
    page_count = int(connection.execute("PRAGMA page_count").fetchone()[0])

    def search(text: str, limit: int) -> dict:
        tokens = tuple(dict.fromkeys(sparse_tokens(text)))
        if not tokens:
            result = {"complete": True, "reason": "", "ranking": []}
            return result
        expression = " OR ".join(f'"{token.replace(chr(34), chr(34) * 2)}"' for token in tokens)
        rows = connection.execute(
            "SELECT statement_id, bm25(sparse_documents) FROM sparse_documents "
            "WHERE sparse_documents MATCH ? ORDER BY bm25(sparse_documents), statement_id LIMIT ?",
            (expression, limit),
        ).fetchall()
        result = {"complete": True, "reason": "", "ranking": [(str(statement_id), -float(score)) for statement_id, score in rows]}
        return result

    result = (search, page_size * page_count)
    return result


def selected_sparse(
    artifacts: tuple[dict, ...],
    namespace: str,
    index: object,
) -> object:
    """Search through the selected sparse owner.

    A ``SparseIndex`` measures the retained index that production requests use;
    an empty ``index`` selects the full per-request rebuild, the independent
    reference the index must agree with. The native completion state and
    refusal reason are kept, so a refused search is never scored as a miss.
    Artifacts were validated when constructed, as the repository validates
    them before production searches, so both routes search them as trusted.
    """
    settings = sparse_config(enabled=True)
    selected_scope = scope_key(namespace=namespace)

    def search(text: str, limit: int) -> dict:
        native = search_sparse_artifacts(
            artifacts,
            text,
            selected_scope,
            settings,
            limit=limit,
            max_working_memory_bytes=64 * 1024 * 1024,
            trusted_artifacts=True,
            index=index,
        )
        result = {
            "complete": native.get("complete", False),
            "reason": native.get("reason", ""),
            "ranking": [(match.get("statement_id", ""), match.get("score", 0.0)) for match in native.get("matches", ())],
        }
        return result

    return search


def internal_relevance(
    name: str,
    search: object,
    queries: list[object],
) -> dict:
    if not callable(search):
        raise ValueError("sparse benchmark search must be callable")
    positive = 0
    top_one = 0
    top_five = 0
    reciprocal_rank = 0.0
    negatives = 0
    completed_negatives = 0
    negative_candidates = 0
    incomplete: Counter[str] = Counter()
    family: dict[str, Counter[str]] = defaultdict(Counter)
    cases = []
    for raw in queries:
        if not isinstance(raw, dict):
            raise ValueError("sparse query cases must be objects")
        expected = str(raw["expected_statement_id"])
        outcome = search(str(raw["query"]), 5)
        if not isinstance(outcome, dict) or not isinstance(outcome.get("ranking", ()), list):
            raise ValueError("sparse benchmark search must return a result object with a ranking list")
        complete = outcome.get("complete", False)
        reason = outcome.get("reason", "")
        ids = [statement_id for statement_id, internal_score in outcome.get("ranking", [])]
        rank = ids.index(expected) + 1 if expected in ids else 0
        group = str(raw["family"])
        if not complete:
            incomplete[reason] += 1
            family[group]["incomplete"] += 1
        if expected:
            # A refused positive has no ranking and counts as a miss.
            positive += 1
            top_one += rank == 1
            top_five += rank > 0
            reciprocal_rank += 1.0 / rank if rank else 0.0
            family[group]["positive"] += 1
            family[group]["top_one"] += rank == 1
            family[group]["top_five"] += rank > 0
        else:
            negatives += 1
            family[group]["negative"] += 1
            # Only a completed search can show a clean negative; a refusal is
            # counted as incomplete rather than as a case without candidates.
            if complete:
                completed_negatives += 1
                negative_candidates += bool(ids)
                family[group]["negative_candidate"] += bool(ids)
        cases.append(
            {
                "case_id": raw["case_id"],
                "family": group,
                "expected_statement_id": expected,
                "complete": complete,
                "reason": reason,
                "rank": rank,
                "top_statement_ids": ids,
            }
        )
    result = {
        "engine": name,
        "positive_cases": positive,
        "top1_recall": top_one / positive,
        "recall_at_5": top_five / positive,
        "mean_reciprocal_rank": reciprocal_rank / positive,
        "negative_cases": negatives,
        "completed_negative_cases": completed_negatives,
        # With no completed negative case the rate takes its failing maximum.
        "negative_candidate_rate": negative_candidates / completed_negatives if completed_negatives else 1.0,
        "incomplete_cases": sum(incomplete.values()),
        "incomplete_reasons": dict(sorted(incomplete.items())),
        "families": {key: dict(value) for key, value in sorted(family.items())},
        "cases": cases,
    }
    return result


def internal_latency(
    search: object,
    queries: list[object],
    repeats: int,
) -> dict:
    if not callable(search):
        raise ValueError("sparse benchmark search must be callable")
    samples = []
    incomplete: Counter[str] = Counter()
    for _ in range(repeats):
        for raw in queries:
            if not isinstance(raw, dict):
                continue
            started = time_perf_counter_ns()
            outcome = search(str(raw["query"]), 5)
            elapsed = (time_perf_counter_ns() - started) / 1_000_000
            if outcome.get("complete", False):
                samples.append(elapsed)
            else:
                incomplete[outcome.get("reason", "")] += 1
    if not samples:
        raise ValueError(f"no sparse benchmark sample completed: {dict(incomplete)}")
    result = {
        "samples": len(samples),
        "incomplete_samples": sum(incomplete.values()),
        "incomplete_reasons": dict(sorted(incomplete.items())),
        "p50_ms": statistics_median(samples),
        "p95_ms": internal_percentile(samples, 0.95),
        "p99_ms": internal_percentile(samples, 0.99),
        "maximum_ms": max(samples),
    }
    return result


def scale_artifacts(count: int) -> tuple[dict, ...]:
    values = []
    for index in range(count):
        values.append(
            internal_artifact(
                {
                    "statement_id": f"scale-{index:05d}",
                    "request": f"Troubleshoot service_{index:05d} ERR_SCALE_{index:05d} version v{index % 20}.4.1",
                    "aliases": [f"service_{index:05d} scale failure"],
                    "response": f"Scale response {index}",
                },
                "section12-scale",
            )
        )
    result = tuple(values)
    return result


def scale_profile() -> dict:
    """Measure the retained sparse index at scale in two phases.

    The cold build is the first search on an empty index, which indexes every
    artifact; steady queries then read only their own postings. Timing and
    traced memory use separate indexes so tracing does not distort latency.
    """
    artifacts = scale_artifacts(SCALE_DOCUMENTS)
    settings = sparse_config(enabled=True)
    selected_scope = scope_key(namespace="section12-scale")
    sequence = [0]

    def query(index: SparseIndex) -> dict:
        position = sequence[0] % SCALE_DOCUMENTS
        sequence[0] += 1
        result = search_sparse_artifacts(
            artifacts,
            f"service_{position:05d} ERR_SCALE_{position:05d}",
            selected_scope,
            settings,
            limit=10,
            max_working_memory_bytes=64 * 1024 * 1024,
            trusted_artifacts=True,
            index=index,
        )
        return result

    timed_index = SparseIndex()
    cold_build = internal_measure(lambda: query(timed_index), 1)
    steady_query = internal_measure(lambda: query(timed_index), SCALE_QUERY_SAMPLES)
    traced_index = SparseIndex()
    tracemalloc_start()
    cold_outcome = query(traced_index)
    cold_current_bytes, cold_peak_bytes = tracemalloc_get_traced_memory()
    tracemalloc_stop()
    tracemalloc_start()
    steady_outcome = query(traced_index)
    query_current_bytes, query_peak_bytes = tracemalloc_get_traced_memory()
    tracemalloc_stop()
    memory_complete = cold_outcome.get("complete", False) and steady_outcome.get("complete", False)
    result = {
        "documents": SCALE_DOCUMENTS,
        "indexed_cold_build": cold_build,
        "indexed_query": steady_query,
        "memory": {
            "complete": memory_complete,
            "cold_build": {"current_bytes": cold_current_bytes, "peak_bytes": cold_peak_bytes},
            "query": {"current_bytes": query_current_bytes, "peak_bytes": query_peak_bytes},
            "peak_bytes": max(cold_peak_bytes, query_peak_bytes),
        },
        "repository_storage_bytes": 0,
    }
    return result


def benchmark(corpus_path: Path, repeats: int = 50, include_scale: bool = True) -> dict:
    if repeats < 20:
        raise ValueError("sparse benchmark requires at least 20 relevance repeats")
    corpus = internal_load(corpus_path)
    documents = corpus["documents"]
    queries = corpus["queries"]
    gates = corpus["gates"]
    if not isinstance(documents, list) or not isinstance(queries, list) or not isinstance(gates, dict):
        raise ValueError("sparse benchmark corpus fields are malformed")
    namespace = str(corpus["namespace"])
    artifacts = tuple(internal_artifact(raw, namespace) for raw in documents if isinstance(raw, dict))

    build_latencies = {}
    started = time_perf_counter_ns()
    bm25 = internal_unfielded_bm25(artifacts)
    build_latencies["unfielded_bm25"] = (time_perf_counter_ns() - started) / 1_000_000
    started = time_perf_counter_ns()
    fts5, fts5_disk_bytes = sqlite_fts(artifacts)
    build_latencies["sqlite_fts5"] = (time_perf_counter_ns() - started) / 1_000_000
    selected = selected_sparse(artifacts, namespace, SparseIndex())
    # The selected engine's build is the cold first search on its empty index;
    # the rebuild reference builds every structure inside each search.
    cold_case = queries[0] if queries and isinstance(queries[0], dict) else {}
    cold_text = str(cold_case.get("query", ""))
    build_latencies["fielded_bm25_v1"] = internal_measure(lambda: selected(cold_text, 5), 1)["p50_ms"]
    build_latencies["fielded_bm25_v1_rebuild"] = 0.0

    searches = {
        "unfielded_bm25": bm25,
        "sqlite_fts5": fts5,
        "fielded_bm25_v1": selected,
        "fielded_bm25_v1_rebuild": selected_sparse(artifacts, namespace, ()),
    }
    relevance = {name: internal_relevance(name, search, queries) for name, search in searches.items()}
    latency = {name: internal_latency(search, queries, repeats) for name, search in searches.items()}
    scale = scale_profile() if include_scale else {}
    selected_relevance = relevance["fielded_bm25_v1"]
    reference_cases = relevance["fielded_bm25_v1_rebuild"]["cases"]
    verdicts = {
        "selected_cases_complete": selected_relevance["incomplete_cases"] == 0,
        "indexed_matches_rebuild": selected_relevance["cases"] == reference_cases,
        "positive_top1_recall": selected_relevance["top1_recall"] >= float(gates["minimum_positive_top1_recall"]),
        "positive_recall_at_5": selected_relevance["recall_at_5"] >= float(gates["minimum_positive_recall_at_5"]),
        "negative_candidate_rate": selected_relevance["negative_candidate_rate"] <= float(gates["maximum_negative_candidate_rate"]),
    }
    if include_scale:
        verdicts.update(
            {
                "scale_queries_complete": not scale["indexed_cold_build"]["incomplete_samples"]
                and not scale["indexed_query"]["incomplete_samples"]
                and scale["memory"]["complete"],
                "scale_request_local_query_p95": scale["indexed_query"]["p95_ms"]
                <= float(gates["maximum_scale_request_local_query_p95_ms"]),
                "scale_peak_memory": scale["memory"]["peak_bytes"] <= int(gates["maximum_scale_peak_memory_bytes"]),
            }
        )
    result = {
        "created_at": recorded_at(),
        "source_state": benchmark_source_state(),
        "corpus": {
            "path": corpus_path.as_posix(),
            "evaluation_role": corpus["evaluation_role"],
            "provenance": corpus["provenance"],
            "documents": len(artifacts),
            "queries": len(queries),
        },
        "decision": {
            "selected": "fielded_bm25_v1",
            "rationale": "deterministic request-local CPU operation with explicit field and technical signals",
        },
        "engines": {
            "unfielded_bm25": {"license": "benchmark implementation in Engram project", "portability": "Python runtime"},
            "sqlite_fts5": {
                "license": "SQLite public domain; Python sqlite3 standard library binding",
                "portability": "requires a Python SQLite build with FTS5 enabled",
                "in_memory_page_bytes": fts5_disk_bytes,
            },
            "fielded_bm25_v1": {
                "license": "Engram project code",
                "portability": "Python runtime; no service or extension dependency",
                "measured_owner": "retained SparseIndex",
            },
            "fielded_bm25_v1_rebuild": {
                "license": "Engram project code",
                "portability": "Python runtime; no service or extension dependency",
                "measured_owner": "full per-request rebuild; independent reference for the retained index",
            },
        },
        "build_latency_ms": build_latencies,
        "relevance": relevance,
        "latency": latency,
        "scale": scale,
        "gates": gates,
        "verdicts": verdicts,
        "passed": all(verdicts.values()),
        "release_authority": "sparse capability evidence only; release qualification remains separate",
    }
    return result


def main() -> int:
    parser = argparse_ArgumentParser(description=__doc__)
    # The tree carries no Section 12 corpus, so the input is always named explicitly.
    parser.add_argument("--corpus", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--repeats", type=int, default=50)
    parser.add_argument("--skip-scale", action="store_true")
    args = parser.parse_args()
    report = benchmark(args.corpus, args.repeats, include_scale=not args.skip_scale)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json_dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(
        json_dumps(
            {
                "passed": report["passed"],
                "verdicts": report["verdicts"],
                "selected_relevance": report["relevance"]["fielded_bm25_v1"],
            },
            default=str,
        )
    )
    result = 0 if report["passed"] else 1
    return result


if __name__ == "__main__":
    raise SystemExit(main())
