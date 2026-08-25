"""Compare Section 12 sparse engines and measure the selected index."""

import argparse
import gc
import json
import math
import sqlite3
import statistics
import sys
import time
import tracemalloc
from collections import Counter, defaultdict
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, cast

REPOSITORY = Path(__file__).resolve().parents[1]
if str(REPOSITORY) not in sys.path:
    sys.path.insert(0, str(REPOSITORY))

from engram.artifacts import (
    CachedResponseArtifact,
    LifecycleState,
    artifact_provenance,
    artifact_statistics,
    cached_response_artifact,
    cached_response_artifact_from_dict,
    cached_response_artifact_to_dict,
)
from engram.config import engram_config, sparse_config
from engram.constants import Tier
from engram.core import Engram
from engram.identity import build_retrieval_representation, build_standalone_identity, scope_key
from engram.repository import ArtifactRepository
from engram.sparse import SparseIndexOwner, sparse_document_from_artifact, sparse_tokens
from scripts.benchmark_metadata import benchmark_source_state, recorded_at

DEFAULT_CORPUS = Path("eval/section12-sparse-v1.json")
DEFAULT_OUTPUT = Path("documentation/sparse/benchmark-2026-08-21.json")
SCALE_DOCUMENTS = 10_000
SCALE_QUERY_SAMPLES = 200
SCALE_BUILD_SAMPLES = 5


def _percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(round((len(ordered) - 1) * fraction))))
    return ordered[index]


def _measure(operation: Callable[[], object], samples: int) -> dict[str, Any]:
    durations = []
    for _ in range(samples):
        started = time.perf_counter_ns()
        operation()
        durations.append((time.perf_counter_ns() - started) / 1_000_000)
    return {
        "samples": samples,
        "minimum_ms": min(durations),
        "p50_ms": statistics.median(durations),
        "p95_ms": _percentile(durations, 0.95),
        "p99_ms": _percentile(durations, 0.99),
        "maximum_ms": max(durations),
    }


def _load(path: Path) -> dict[str, object]:
    decoded = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(decoded, dict) or decoded.get("schema_version") != 1:
        raise ValueError("sparse benchmark corpus must be a schema-1 object")
    if not isinstance(decoded.get("documents"), list) or not isinstance(decoded.get("queries"), list):
        raise ValueError("sparse benchmark corpus must contain document and query arrays")
    if not isinstance(decoded.get("gates"), dict):
        raise ValueError("sparse benchmark corpus must contain gates")
    return decoded


def _artifact(raw: Mapping[str, object], namespace: str) -> CachedResponseArtifact:
    request = str(raw["request"])
    aliases_value = raw.get("aliases", [])
    if not isinstance(aliases_value, list):
        raise ValueError("sparse document aliases must be an array")
    selected_scope = scope_key(namespace=namespace)
    return cached_response_artifact(
        statement_id=str(raw["statement_id"]),
        generation=1,
        response=str(raw["response"]),
        query_identity=build_standalone_identity(request, selected_scope),
        retrieval=build_retrieval_representation(request, tuple(str(value) for value in aliases_value)),
        tier=Tier.STATIC,
        lifecycle=LifecycleState.ACTIVE,
        scope=selected_scope,
        support_claim_ids=(),
        valid_from="",
        valid_from_available=False,
        valid_until="",
        valid_until_available=False,
        knowledge_epoch=0,
        knowledge_epoch_available=False,
        superseded_by="",
        provenance=artifact_provenance("section12:benchmark", "engineering", "2026-08-21T00:00:00Z"),
        statistics=artifact_statistics(),
        metadata={},
    )


def _unfielded_bm25(artifacts: tuple[CachedResponseArtifact, ...]) -> Callable[[str, int], list[tuple[str, float]]]:
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
                inverse = math.log(1.0 + (len(documents) - df + 0.5) / (df + 0.5))
                denominator = frequency + 1.2 * (0.25 + 0.75 * len(documents[statement_id]) / average_length)
                score += inverse * frequency * 2.2 / denominator
            if score:
                scored.append((statement_id, score))
        scored.sort(key=lambda value: (-value[1], value[0]))
        return scored[:limit]

    return search


def _existing_idf(artifacts: tuple[CachedResponseArtifact, ...]) -> Callable[[str, int], list[tuple[str, float]]]:
    engine = Engram(
        config=engram_config(
            use_synonyms=False,
            use_spell_correction=False,
            use_stemming=False,
            use_lemmatization=False,
        )
    )
    engine.response_repository = ArtifactRepository(artifacts)
    from engram import persistence

    persistence.synchronize_response_compatibility_views(engine, ())

    def search(text: str, limit: int) -> list[tuple[str, float]]:
        result = engine.query_candidates(text, limit=limit, max_working_memory_bytes=32 * 1024 * 1024)
        return [(str(statement["id"]), float(score)) for statement, score in result["matches"]]

    return search


def _sqlite_fts(artifacts: tuple[CachedResponseArtifact, ...]) -> tuple[Callable[[str, int], list[tuple[str, float]]], int]:
    connection = sqlite3.connect(":memory:")
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
        return [(str(statement_id), -float(score)) for statement_id, score in rows]

    return search, page_size * page_count


def _selected_sparse(
    artifacts: tuple[CachedResponseArtifact, ...],
    namespace: str,
) -> tuple[SparseIndexOwner, Callable[[str, int], list[tuple[str, float]]]]:
    owner = SparseIndexOwner(sparse_config(enabled=True))
    owner.rebuild(artifacts, 1)
    selected_scope = scope_key(namespace=namespace)

    def search(text: str, limit: int) -> list[tuple[str, float]]:
        result = owner.search(text, selected_scope, limit=limit, max_working_memory_bytes=64 * 1024 * 1024)
        if not result["complete"]:
            return []
        return [(match["statement_id"], match["score"]) for match in result["matches"]]

    return owner, search


def _relevance(
    name: str,
    search: Callable[[str, int], list[tuple[str, float]]],
    queries: list[object],
) -> dict[str, Any]:
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
        ids = [statement_id for statement_id, _score in ranking]
        rank = ids.index(expected) + 1 if expected in ids else 0
        group = str(raw["family"])
        if expected:
            positive += 1
            top_one += rank == 1
            top_five += rank > 0
            reciprocal_rank += 1.0 / rank if rank else 0.0
            family[group]["positive"] += 1
            family[group]["top_one"] += rank == 1
            family[group]["top_five"] += rank > 0
        else:
            negatives += 1
            negative_candidates += bool(ids)
            family[group]["negative"] += 1
            family[group]["negative_candidate"] += bool(ids)
        cases.append(
            {
                "case_id": raw["case_id"],
                "family": group,
                "expected_statement_id": expected,
                "rank": rank,
                "top_statement_ids": ids,
            }
        )
    return {
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


def _latency(
    search: Callable[[str, int], list[tuple[str, float]]],
    queries: list[object],
    repeats: int,
) -> dict[str, Any]:
    samples = []
    for _ in range(repeats):
        for raw in queries:
            if not isinstance(raw, Mapping):
                continue
            started = time.perf_counter_ns()
            search(str(raw["query"]), 5)
            samples.append((time.perf_counter_ns() - started) / 1_000_000)
    return {
        "samples": len(samples),
        "p50_ms": statistics.median(samples),
        "p95_ms": _percentile(samples, 0.95),
        "p99_ms": _percentile(samples, 0.99),
        "maximum_ms": max(samples),
    }


def _scale_artifacts(count: int) -> tuple[CachedResponseArtifact, ...]:
    values = []
    for index in range(count):
        values.append(
            _artifact(
                {
                    "statement_id": f"scale-{index:05d}",
                    "request": f"Troubleshoot service_{index:05d} ERR_SCALE_{index:05d} version v{index % 20}.4.1",
                    "aliases": [f"service_{index:05d} scale failure"],
                    "response": f"Scale response {index}",
                },
                "section12-scale",
            )
        )
    return tuple(values)


def _scale_profile() -> dict[str, Any]:
    artifacts = _scale_artifacts(SCALE_DOCUMENTS)
    settings = sparse_config(enabled=True)
    owner_holder: list[SparseIndexOwner] = []

    def build() -> object:
        owner = SparseIndexOwner(settings)
        state = owner.rebuild(artifacts, 1)
        owner_holder[:] = [owner]
        return state

    build_latency = _measure(build, SCALE_BUILD_SAMPLES)
    selected_scope = scope_key(namespace="section12-scale")
    sequence = [0]

    def query() -> object:
        index = sequence[0] % SCALE_DOCUMENTS
        sequence[0] += 1
        return owner_holder[0].search(
            f"service_{index:05d} ERR_SCALE_{index:05d}",
            selected_scope,
            limit=10,
            max_working_memory_bytes=64 * 1024 * 1024,
        )

    query_latency = _measure(query, SCALE_QUERY_SAMPLES)
    del query
    owner_holder.clear()
    gc.collect()
    tracemalloc.start()
    measured_owner = SparseIndexOwner(settings)
    measured_state = measured_owner.rebuild(artifacts, 1)
    current_bytes, peak_bytes = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    before = measured_owner.snapshot()
    before_postings = before["postings"]
    first = artifacts[0]
    updated = cached_response_artifact_to_dict(first)
    updated["generation"] = 2
    statistics_value = dict(cast(Mapping[str, object], updated["statistics"]))
    statistics_value["query_count"] = 1
    updated["statistics"] = statistics_value
    statistics_artifact = cached_response_artifact_from_dict(updated)
    artifact_map = {artifact["statement_id"]: artifact for artifact in artifacts}
    artifact_map[first["statement_id"]] = statistics_artifact
    statistics_latency = _measure(
        lambda: measured_owner.synchronize(artifact_map, 2, (first["statement_id"],)),
        30,
    )
    statistics_replacements = sum(
        measured_owner.snapshot()["postings"].get(term) is not posting for term, posting in before_postings.items()
    )

    added = _artifact(
        {
            "statement_id": "scale-added",
            "request": "Troubleshoot service_added ERR_SCALE_ADDED version v99.4.1",
            "aliases": ["service_added scale failure"],
            "response": "Added response",
        },
        "section12-scale",
    )
    before_incremental = measured_owner.snapshot()["postings"]
    artifact_map[added["statement_id"]] = added
    started = time.perf_counter_ns()
    measured_owner.synchronize(artifact_map, 3, (added["statement_id"],))
    incremental_ms = (time.perf_counter_ns() - started) / 1_000_000
    after_incremental = measured_owner.snapshot()["postings"]
    all_terms = set(before_incremental).union(after_incremental)
    replaced_terms = sum(before_incremental.get(term) is not after_incremental.get(term) for term in all_terms)
    replacement_rate = replaced_terms / max(1, len(all_terms))
    incremental_equivalent = measured_owner.check_against(artifact_map.values(), 3)["consistent"]
    return {
        "documents": SCALE_DOCUMENTS,
        "index_terms": len(measured_state["postings"]),
        "build": build_latency,
        "query": query_latency,
        "memory": {"current_bytes": current_bytes, "peak_bytes": peak_bytes},
        "disk_bytes_persisted": 0,
        "statistics_only_update": {
            "latency": statistics_latency,
            "posting_replacements": statistics_replacements,
        },
        "incremental_add": {
            "latency_ms": incremental_ms,
            "replaced_posting_terms": replaced_terms,
            "posting_replacement_rate": replacement_rate,
            "clean_rebuild_equivalent": incremental_equivalent,
        },
    }


def benchmark(corpus_path: Path = DEFAULT_CORPUS, repeats: int = 50, include_scale: bool = True) -> dict[str, Any]:
    if repeats < 20:
        raise ValueError("sparse benchmark requires at least 20 relevance repeats")
    corpus = _load(corpus_path)
    documents = corpus["documents"]
    queries = corpus["queries"]
    gates = corpus["gates"]
    if not isinstance(documents, list) or not isinstance(queries, list) or not isinstance(gates, dict):
        raise ValueError("sparse benchmark corpus fields are malformed")
    namespace = str(corpus["namespace"])
    artifacts = tuple(_artifact(raw, namespace) for raw in documents if isinstance(raw, Mapping))

    build_latencies = {}
    started = time.perf_counter_ns()
    idf = _existing_idf(artifacts)
    build_latencies["existing_idf_overlap"] = (time.perf_counter_ns() - started) / 1_000_000
    started = time.perf_counter_ns()
    bm25 = _unfielded_bm25(artifacts)
    build_latencies["unfielded_bm25"] = (time.perf_counter_ns() - started) / 1_000_000
    started = time.perf_counter_ns()
    fts5, fts5_disk_bytes = _sqlite_fts(artifacts)
    build_latencies["sqlite_fts5"] = (time.perf_counter_ns() - started) / 1_000_000
    started = time.perf_counter_ns()
    _owner, selected = _selected_sparse(artifacts, namespace)
    build_latencies["fielded_bm25_v1"] = (time.perf_counter_ns() - started) / 1_000_000

    searches = {
        "existing_idf_overlap": idf,
        "unfielded_bm25": bm25,
        "sqlite_fts5": fts5,
        "fielded_bm25_v1": selected,
    }
    relevance = {name: _relevance(name, search, queries) for name, search in searches.items()}
    latency = {name: _latency(search, queries, repeats) for name, search in searches.items()}
    scale = _scale_profile() if include_scale else {}
    selected_relevance = relevance["fielded_bm25_v1"]
    existing_relevance = relevance["existing_idf_overlap"]
    verdicts = {
        "positive_top1_recall": selected_relevance["top1_recall"] >= float(gates["minimum_positive_top1_recall"]),
        "positive_recall_at_5": selected_relevance["recall_at_5"] >= float(gates["minimum_positive_recall_at_5"]),
        "top1_gain_over_existing_idf": (
            selected_relevance["top1_recall"] - existing_relevance["top1_recall"]
            >= float(gates["minimum_top1_gain_over_existing_idf"])
        ),
        "negative_candidate_rate": selected_relevance["negative_candidate_rate"] <= float(gates["maximum_negative_candidate_rate"]),
    }
    if include_scale:
        verdicts.update(
            {
                "scale_query_p95": scale["query"]["p95_ms"] <= float(gates["maximum_scale_query_p95_ms"]),
                "scale_build": scale["build"]["p95_ms"] <= float(gates["maximum_scale_build_ms"]),
                "scale_peak_memory": scale["memory"]["peak_bytes"] <= int(gates["maximum_scale_peak_memory_bytes"]),
                "incremental_write_amplification": scale["incremental_add"]["posting_replacement_rate"]
                <= float(gates["maximum_incremental_posting_replacement_rate"]),
                "incremental_add_latency": scale["incremental_add"]["latency_ms"] <= float(gates["maximum_incremental_add_ms"]),
                "statistics_write_amplification": scale["statistics_only_update"]["posting_replacements"]
                <= int(gates["maximum_statistics_only_posting_replacements"]),
                "incremental_rebuild_equivalence": scale["incremental_add"]["clean_rebuild_equivalent"] is True,
            }
        )
    return {
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
            "rationale": "deterministic local CPU operation, explicit field/technical signals, immutable atomic state, and no persisted secondary authority",
        },
        "engines": {
            "existing_idf_overlap": {"license": "Engram project code", "portability": "Python runtime"},
            "unfielded_bm25": {"license": "benchmark implementation in Engram project", "portability": "Python runtime"},
            "sqlite_fts5": {
                "license": "SQLite public domain; Python sqlite3 standard library binding",
                "portability": "requires a Python SQLite build with FTS5 enabled",
                "in_memory_page_bytes": fts5_disk_bytes,
            },
            "fielded_bm25_v1": {
                "license": "Engram project code",
                "portability": "Python runtime; no service or extension dependency",
                "persisted_index_bytes": 0,
            },
        },
        "build_latency_ms": build_latencies,
        "relevance": relevance,
        "latency": latency,
        "scale": scale,
        "gates": gates,
        "verdicts": verdicts,
        "passed": all(verdicts.values()),
        "release_authority": "Section 12 engineering promotion only; Section 16 release authority remains separate",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus", type=Path, default=DEFAULT_CORPUS)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--repeats", type=int, default=50)
    parser.add_argument("--skip-scale", action="store_true")
    args = parser.parse_args()
    report = benchmark(args.corpus, args.repeats, include_scale=not args.skip_scale)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {
                "passed": report["passed"],
                "verdicts": report["verdicts"],
                "selected_relevance": report["relevance"]["fielded_bm25_v1"],
            },
            default=str,
        )
    )
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
