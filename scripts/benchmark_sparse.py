"""Compare Section 12 sparse engines and measure artifact-local retrieval."""

from argparse import ArgumentParser as argparse_ArgumentParser
from collections import Counter, defaultdict
from collections.abc import Mapping
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
from engram.sparse import search_sparse_artifacts, sparse_document_from_artifact, sparse_tokens
from scripts.benchmark_metadata import benchmark_source_state, recorded_at

DEFAULT_CORPUS = Path("eval/section12-sparse-v1.json")
DEFAULT_OUTPUT = Path("eval/results/sparse/benchmark-2026-08-21.json")
SCALE_DOCUMENTS = 10_000
SCALE_QUERY_SAMPLES = 200


def internal_percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(round((len(ordered) - 1) * fraction))))
    result = ordered[index]
    return result


def internal_measure(operation: object, samples: int) -> dict[str, object]:
    durations = []
    for _ in range(samples):
        started = time_perf_counter_ns()
        operation()
        durations.append((time_perf_counter_ns() - started) / 1_000_000)
    result = {
        "samples": samples,
        "minimum_ms": min(durations),
        "p50_ms": statistics_median(durations),
        "p95_ms": internal_percentile(durations, 0.95),
        "p99_ms": internal_percentile(durations, 0.99),
        "maximum_ms": max(durations),
    }
    return result


def internal_load(path: Path) -> dict[str, object]:
    decoded = json_loads(path.read_text(encoding="utf-8"))
    if not isinstance(decoded, dict) or decoded.get("schema_version") != 1:
        raise ValueError("sparse benchmark corpus must be a schema-1 object")
    if not isinstance(decoded.get("documents"), list) or not isinstance(decoded.get("queries"), list):
        raise ValueError("sparse benchmark corpus must contain document and query arrays")
    if not isinstance(decoded.get("gates"), dict):
        raise ValueError("sparse benchmark corpus must contain gates")
    return decoded


def internal_artifact(raw: dict[str, object], namespace: str) -> dict:
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

    def search(text: str, limit: int) -> list[tuple[str, float]]:
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
        result = scored[:limit]
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

    def search(text: str, limit: int) -> list[tuple[str, float]]:
        tokens = tuple(dict.fromkeys(sparse_tokens(text)))
        if not tokens:
            return []
        expression = " OR ".join(f'"{token.replace(chr(34), chr(34) * 2)}"' for token in tokens)
        rows = connection.execute(
            "SELECT statement_id, bm25(sparse_documents) FROM sparse_documents "
            "WHERE sparse_documents MATCH ? ORDER BY bm25(sparse_documents), statement_id LIMIT ?",
            (expression, limit),
        ).fetchall()
        result = [(str(statement_id), -float(score)) for statement_id, score in rows]
        return result

    result = (search, page_size * page_count)
    return result


def selected_sparse(
    artifacts: tuple[dict, ...],
    namespace: str,
) -> object:
    settings = sparse_config(enabled=True)
    selected_scope = scope_key(namespace=namespace)

    def search(text: str, limit: int) -> list[tuple[str, float]]:
        result = search_sparse_artifacts(
            artifacts,
            text,
            selected_scope,
            settings,
            limit=limit,
            max_working_memory_bytes=64 * 1024 * 1024,
        )
        if not result["complete"]:
            return []
        result = [(match["statement_id"], match["score"]) for match in result["matches"]]
        return result

    return search


def internal_relevance(
    name: str,
    search: object,
    queries: list[object],
) -> dict[str, object]:
    positive = 0
    top_one = 0
    top_five = 0
    reciprocal_rank = 0.0
    negatives = 0
    negative_candidates = 0
    family: dict[str, Counter[str]] = defaultdict(Counter)
    cases = []
    for raw in queries:
        if not isinstance(raw, Mapping):
            raise ValueError("sparse query cases must be objects")
        expected = str(raw["expected_statement_id"])
        ranking = search(str(raw["query"]), 5)
        ids = [statement_id for statement_id, internal_score in ranking]
        rank = ids.index(expected) + 1 if expected in ids else 0
        group = str(raw["family"])
        if expected:
            positive += 1
            top_one += rank == 1
            top_five += rank > 0
            reciprocal_rank += 1.0 / rank if rank else 0.0
            family.get(group, {})["positive"] += 1
            family.get(group, {})["top_one"] += rank == 1
            family.get(group, {})["top_five"] += rank > 0
        else:
            negatives += 1
            negative_candidates += bool(ids)
            family.get(group, {})["negative"] += 1
            family.get(group, {})["negative_candidate"] += bool(ids)
        cases.append(
            {
                "case_id": raw["case_id"],
                "family": group,
                "expected_statement_id": expected,
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
        "negative_candidate_rate": negative_candidates / negatives,
        "families": {key: dict(value) for key, value in sorted(family.items())},
        "cases": cases,
    }
    return result


def internal_latency(
    search: object,
    queries: list[object],
    repeats: int,
) -> dict[str, object]:
    samples = []
    for _ in range(repeats):
        for raw in queries:
            if not isinstance(raw, Mapping):
                continue
            started = time_perf_counter_ns()
            search(str(raw["query"]), 5)
            samples.append((time_perf_counter_ns() - started) / 1_000_000)
    result = {
        "samples": len(samples),
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


def scale_profile() -> dict[str, object]:
    artifacts = scale_artifacts(SCALE_DOCUMENTS)
    settings = sparse_config(enabled=True)
    selected_scope = scope_key(namespace="section12-scale")
    sequence = [0]

    def query() -> object:
        index = sequence[0] % SCALE_DOCUMENTS
        sequence[0] += 1
        result = search_sparse_artifacts(
            artifacts,
            f"service_{index:05d} ERR_SCALE_{index:05d}",
            selected_scope,
            settings,
            limit=10,
            max_working_memory_bytes=64 * 1024 * 1024,
        )
        return result

    query_latency = internal_measure(query, SCALE_QUERY_SAMPLES)
    tracemalloc_start()
    query()
    current_bytes, peak_bytes = tracemalloc_get_traced_memory()
    tracemalloc_stop()
    return {
        "documents": SCALE_DOCUMENTS,
        "request_local_query": query_latency,
        "memory": {"current_bytes": current_bytes, "peak_bytes": peak_bytes},
        "repository_storage_bytes": 0,
    }


def benchmark(corpus_path: Path = DEFAULT_CORPUS, repeats: int = 50, include_scale: bool = True) -> dict[str, object]:
    if repeats < 20:
        raise ValueError("sparse benchmark requires at least 20 relevance repeats")
    corpus = internal_load(corpus_path)
    documents = corpus["documents"]
    queries = corpus["queries"]
    gates = corpus["gates"]
    if not isinstance(documents, list) or not isinstance(queries, list) or not isinstance(gates, dict):
        raise ValueError("sparse benchmark corpus fields are malformed")
    namespace = str(corpus["namespace"])
    artifacts = tuple(internal_artifact(raw, namespace) for raw in documents if isinstance(raw, Mapping))

    build_latencies = {}
    started = time_perf_counter_ns()
    bm25 = internal_unfielded_bm25(artifacts)
    build_latencies["unfielded_bm25"] = (time_perf_counter_ns() - started) / 1_000_000
    started = time_perf_counter_ns()
    fts5, fts5_disk_bytes = sqlite_fts(artifacts)
    build_latencies["sqlite_fts5"] = (time_perf_counter_ns() - started) / 1_000_000
    selected = selected_sparse(artifacts, namespace)
    build_latencies["fielded_bm25_v1"] = 0.0

    searches = {
        "unfielded_bm25": bm25,
        "sqlite_fts5": fts5,
        "fielded_bm25_v1": selected,
    }
    relevance = {name: internal_relevance(name, search, queries) for name, search in searches.items()}
    latency = {name: internal_latency(search, queries, repeats) for name, search in searches.items()}
    scale = scale_profile() if include_scale else {}
    selected_relevance = relevance["fielded_bm25_v1"]
    verdicts = {
        "positive_top1_recall": selected_relevance["top1_recall"] >= float(gates["minimum_positive_top1_recall"]),
        "positive_recall_at_5": selected_relevance["recall_at_5"] >= float(gates["minimum_positive_recall_at_5"]),
        "negative_candidate_rate": selected_relevance["negative_candidate_rate"] <= float(gates["maximum_negative_candidate_rate"]),
    }
    if include_scale:
        verdicts.update(
            {
                "scale_request_local_query_p95": scale["request_local_query"]["p95_ms"]
                <= float(gates["maximum_scale_request_local_query_p95_ms"]),
                "scale_peak_memory": scale["memory"]["peak_bytes"] <= int(gates["maximum_scale_peak_memory_bytes"]),
            }
        )
    result = {
        "schema_version": 1,
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
    parser.add_argument("--corpus", type=Path, default=DEFAULT_CORPUS)
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
