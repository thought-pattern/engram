"""Benchmark Section 13 semantic retrieval and transparent reranking."""

from argparse import ArgumentParser as argparse_ArgumentParser
from datetime import UTC, datetime
from importlib import util as importlib_util
from json import dumps as json_dumps, loads as json_loads
from math import ceil as math_ceil
from pathlib import Path
from resource import RUSAGE_SELF, getrusage as resource_getrusage
from statistics import fmean as statistics_fmean
from sys import path as sys_path, platform as sys_platform
from time import perf_counter_ns as time_perf_counter_ns
from tracemalloc import get_traced_memory as tracemalloc_get_traced_memory, start as tracemalloc_start, stop as tracemalloc_stop

from psutil import Process as psutil_Process

sys_path.insert(0, str(Path(__file__).resolve().parent.parent))

from engram.artifacts import LifecycleState, validate_cached_response_artifact
from engram.config import reranker_config, semantic_config, sparse_config
from engram.constants import INITIAL_ARTIFACT_STATISTICS, Tier
from engram.identity import extract_standalone_identity, retrieval_representation, scope_key
from engram.reranking import TransparentLogisticReranker
from engram.semantic import StandaloneSemanticRetriever, model_artifact_sha256
from engram.sparse import search_sparse_artifacts
from scripts.benchmark_metadata import benchmark_source_state

# Model manifest fields the benchmark reads, with the type each carries; a manifest lacking one is refused.
MODEL_MANIFEST_FIELD_DEFAULTS = {
    "artifact_sha256": "",
    "backend": "",
    "dimension": 0,
    "license_id": "",
    "model_id": "",
    "model_path": "",
    "model_version": "",
}
# Every corpus, query and gate field the benchmark reads; a corpus lacking one is refused.
CORPUS_FIELDS = {"documents", "queries", "gates", "evaluation_role"}
QUERY_FIELDS = {"query_id", "partition", "text", "expected_statement_id"}
SEMANTIC_GATE_FIELDS = {
    "engineering_holdout_recall_at_1_min",
    "engineering_holdout_recall_delta_vs_sparse_min",
    "engineering_holdout_false_answer_rate_max",
    "peak_memory_mib_max",
}
RERANKER_GATE_FIELDS = {"engineering_holdout_recall_delta_min", "engineering_holdout_false_answer_delta_max", "peak_memory_mib_max"}
# Promotion is judged on the held-out partition only; a corpus without it is refused.
HOLDOUT_PARTITION = "engineering_holdout"


def parse_args():
    parser = argparse_ArgumentParser(description=__doc__)
    # The tree carries no Section 13 corpus, so the input is always named explicitly.
    parser.add_argument("--corpus", required=True)
    parser.add_argument(
        "--manifest",
        default="data/artifacts/models/all-MiniLM-L6-v2-826711e5.engram-model.json",
    )
    parser.add_argument("--output", default="eval/results/semantic/benchmark.json")
    result = parser.parse_args()
    return result


def peak_rss_bytes() -> int:
    """Return this process's resident-set high-water mark in bytes."""
    peak = resource_getrusage(RUSAGE_SELF).ru_maxrss
    # getrusage reports kilobytes on Linux and bytes on macOS.
    result = peak if sys_platform == "darwin" else peak * 1024
    return result


def percentile(values: list[float], quantile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, math_ceil(quantile * len(ordered)) - 1))
    result = ordered[index]
    return result


def rank(statement_ids: list[str], expected: str) -> int:
    result = statement_ids.index(expected) + 1 if expected in statement_ids else 0
    return result


def partition_metrics(rows: list[dict]) -> dict:
    positives = [row for row in rows if row.get("expected_statement_id", "")]
    reciprocal = [1.0 / row.get("rank", 0) if row.get("rank", 0) else 0.0 for row in positives]
    false_answers = [
        row
        for row in rows
        if row.get("top_statement_id", "") and row.get("top_statement_id", "") != row.get("expected_statement_id", "")
    ]
    result = {
        "query_count": len(rows),
        "positive_count": len(positives),
        "recall_at_1": sum(row.get("rank", 0) == 1 for row in positives) / len(positives) if positives else 0.0,
        "recall_at_3": sum(0 < row.get("rank", 0) <= 3 for row in positives) / len(positives) if positives else 0.0,
        "mrr": statistics_fmean(reciprocal) if reciprocal else 0.0,
        "false_answer_rate": len(false_answers) / len(rows) if rows else 0.0,
    }
    return result


def evaluate_rows(rows: list[dict]) -> dict:
    partitions = sorted({str(row.get("partition", "")) for row in rows})
    result = {
        partition: partition_metrics([row for row in rows if row.get("partition", "") == partition]) for partition in partitions
    }
    return result


def main() -> int:
    args = parse_args()
    corpus_path = Path(args.corpus)
    manifest_path = Path(args.manifest)
    output_path = Path(args.output)
    corpus = json_loads(corpus_path.read_text(encoding="utf-8"))
    if not isinstance(corpus, dict) or not CORPUS_FIELDS.issubset(corpus):
        raise SystemExit(f"semantic benchmark corpus must be an object with {', '.join(sorted(CORPUS_FIELDS))}")
    queries = corpus.get("queries", [])
    gates = corpus.get("gates", {})
    if any(not isinstance(query, dict) or not QUERY_FIELDS.issubset(query) for query in queries):
        raise SystemExit(f"semantic benchmark queries must be objects with {', '.join(sorted(QUERY_FIELDS))}")
    if HOLDOUT_PARTITION not in {str(query.get("partition", "")) for query in queries}:
        raise SystemExit(f"semantic benchmark corpus has no {HOLDOUT_PARTITION} queries")
    if not isinstance(gates, dict) or any(not isinstance(gates.get(name, {}), dict) for name in ("semantic", "reranker")):
        raise SystemExit("semantic benchmark gates must hold semantic and reranker threshold objects")
    semantic_gates = gates.get("semantic", {})
    reranker_gates = gates.get("reranker", {})
    if not SEMANTIC_GATE_FIELDS.issubset(semantic_gates) or not RERANKER_GATE_FIELDS.issubset(reranker_gates):
        raise SystemExit("semantic benchmark corpus is missing semantic or reranker gate thresholds")
    manifest = json_loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(manifest, dict) or not MODEL_MANIFEST_FIELD_DEFAULTS.keys() <= manifest.keys():
        raise SystemExit(f"model manifest must be an object with {', '.join(sorted(MODEL_MANIFEST_FIELD_DEFAULTS))}")
    model_path = Path(manifest.get("model_path", ""))
    expected_checksum = manifest.get("artifact_sha256", "")
    actual_checksum = model_artifact_sha256(model_path)
    if actual_checksum != expected_checksum:
        raise SystemExit("model artifact checksum does not match manifest")
    semantic_settings = semantic_config(
        enabled=True,
        model_path=model_path.as_posix(),
        model_id=manifest.get("model_id", ""),
        model_version=manifest.get("model_version", ""),
        license_id=manifest.get("license_id", ""),
        artifact_sha256=expected_checksum,
        dimension=manifest.get("dimension", 0),
        backend=manifest.get("backend", ""),
        min_similarity=0.45,
    )
    selected_scope = scope_key(namespace="semantic-benchmark")
    # Each corpus document becomes one accepted response in the benchmark scope.
    accepted = []
    for document in corpus.get("documents", []):
        request = str(document.get("canonical", ""))
        accepted.append(
            validate_cached_response_artifact(
                {
                    "statement_id": str(document.get("statement_id", "")),
                    "generation": 1,
                    "response": str(document.get("response", "")),
                    "query_identity": extract_standalone_identity(request, selected_scope),
                    "retrieval": retrieval_representation(request, tuple(document.get("aliases", []))),
                    "tier": Tier.STATIC,
                    "lifecycle": LifecycleState.ACTIVE,
                    "scope": selected_scope,
                    "support_references": (),
                    "valid_from": "",
                    "valid_from_available": False,
                    "valid_until": "",
                    "valid_until_available": False,
                    "superseded_by": "",
                    "provenance": {
                        "source_label": "section13-benchmark",
                        "caller_id": "engineering",
                        "accepted_at": "2026-08-22T00:00:00Z",
                    },
                    "statistics": INITIAL_ARTIFACT_STATISTICS,
                    "metadata": {},
                }
            )
        )
    artifacts = tuple(accepted)
    process = psutil_Process()
    rss_before = process.memory_info().rss
    cold_started = time_perf_counter_ns()
    semantic = StandaloneSemanticRetriever(semantic_settings)
    cold_start_ms = (time_perf_counter_ns() - cold_started) / 1_000_000
    if not semantic.available:
        raise SystemExit(f"native semantic model unavailable: {semantic.last_error}")
    rss_after_model = process.memory_info().rss
    sparse_settings = sparse_config(enabled=True)
    reranker = TransparentLogisticReranker(reranker_config(enabled=True, shortlist_size=8))
    request_local_started = time_perf_counter_ns()
    semantic.search(
        "semantic benchmark warmup",
        selected_scope,
        artifacts,
        limit=8,
        max_vector_results=8,
        max_working_memory_bytes=16_777_216,
    )
    request_local_embedding_ms = (time_perf_counter_ns() - request_local_started) / 1_000_000
    rss_after_request = process.memory_info().rss
    semantic_rows = []
    sparse_rows = []
    reranker_rows = []
    semantic_latencies = []
    reranker_latencies = []
    drift_max = 0.0
    # Each component runs as its own phase so its memory can be attributed.
    # Every semantic search runs first; the process high-water mark read after
    # that phase bounds the model load, warmup and query work.
    semantic_results = []
    for query in queries:
        started = time_perf_counter_ns()
        semantic_result = semantic.search(
            query.get("text", ""),
            selected_scope,
            artifacts,
            limit=8,
            max_vector_results=8,
            max_working_memory_bytes=16_777_216,
        )
        semantic_ms = (time_perf_counter_ns() - started) / 1_000_000
        semantic_latencies.append(semantic_ms)
        repeated = semantic.search(
            query.get("text", ""),
            selected_scope,
            artifacts,
            limit=8,
            max_vector_results=8,
            max_working_memory_bytes=16_777_216,
        )
        first_scores = {value.get("statement_id", ""): value.get("similarity", 0.0) for value in semantic_result.get("matches", ())}
        second_scores = {value.get("statement_id", ""): value.get("similarity", 0.0) for value in repeated.get("matches", ())}
        drift_max = max(
            drift_max,
            max((abs(score - second_scores.get(key, 0.0)) for key, score in first_scores.items()), default=0.0),
        )
        semantic_ids = [value.get("statement_id", "") for value in semantic_result.get("matches", ())]
        semantic_results.append(semantic_result)
        semantic_rows.append(
            {
                "query_id": query.get("query_id", ""),
                "partition": query.get("partition", ""),
                "expected_statement_id": query.get("expected_statement_id", ""),
                "top_statement_id": semantic_ids[0] if semantic_ids else "",
                "rank": rank(semantic_ids, query.get("expected_statement_id", "")),
                "latency_ms": semantic_ms,
                "candidate_ids": semantic_ids,
            }
        )
    semantic_peak_rss = peak_rss_bytes()
    rss_after_queries = process.memory_info().rss
    shortlists = []
    for query, semantic_result in zip(queries, semantic_results, strict=True):
        sparse_result = search_sparse_artifacts(
            artifacts,
            query.get("text", ""),
            selected_scope,
            sparse_settings,
            limit=8,
            max_working_memory_bytes=16_777_216,
        )
        sparse_scores = {value.get("statement_id", ""): value.get("score", 0.0) for value in sparse_result.get("matches", ())}
        sparse_ids = [value.get("statement_id", "") for value in sparse_result.get("matches", ())]
        sparse_rows.append(
            {
                "query_id": query.get("query_id", ""),
                "partition": query.get("partition", ""),
                "expected_statement_id": query.get("expected_statement_id", ""),
                "top_statement_id": sparse_ids[0] if sparse_ids else "",
                "rank": rank(sparse_ids, query.get("expected_statement_id", "")),
                "candidate_ids": sparse_ids,
            }
        )
        # The reranker shortlist is the semantic candidate list with each candidate's lexical score attached.
        candidates = []
        for value in semantic_result.get("matches", ()):
            similarity = value.get("similarity", 0.0)
            statement_id = value.get("statement_id", "")
            candidates.append(
                {
                    "statement_id": statement_id,
                    "base_score": similarity,
                    "features": {"base_score": similarity, "semantic": similarity, "lexical": sparse_scores.get(statement_id, 0.0)},
                }
            )
        shortlists.append(tuple(candidates))
    for query, semantic_result, shortlist in zip(queries, semantic_results, shortlists, strict=True):
        semantic_ids = [value.get("statement_id", "") for value in semantic_result.get("matches", ())]
        rerank_started = time_perf_counter_ns()
        reranked = reranker.rerank(shortlist)
        rerank_ms = (time_perf_counter_ns() - rerank_started) / 1_000_000
        reranker_latencies.append(rerank_ms)
        if reranked.get("applied", False):
            reranked_ids = [value.get("statement_id", "") for value in reranked.get("scores", ())]
        else:
            reranked_ids = semantic_ids
        reranker_rows.append(
            {
                "query_id": query.get("query_id", ""),
                "partition": query.get("partition", ""),
                "expected_statement_id": query.get("expected_statement_id", ""),
                "top_statement_id": reranked_ids[0] if reranked_ids else "",
                "rank": rank(reranked_ids, query.get("expected_statement_id", "")),
                "latency_ms": rerank_ms,
                "candidate_ids": reranked_ids,
            }
        )
    # The reranker is pure Python, so traced allocations give its own
    # high-water mark. Tracing runs as a separate pass so it does not distort
    # the timings above, and each result is discarded as the service would.
    tracemalloc_start()
    for shortlist in shortlists:
        reranker.rerank(shortlist)
    _, reranker_peak_bytes = tracemalloc_get_traced_memory()
    tracemalloc_stop()
    elapsed_query_seconds = sum(semantic_latencies) / 1000.0
    semantic_metrics = evaluate_rows(semantic_rows)
    sparse_metrics = evaluate_rows(sparse_rows)
    reranker_metrics = evaluate_rows(reranker_rows)
    # Every metric set holds the held-out partition, which the corpus check above requires.
    holdout_semantic = semantic_metrics.get(HOLDOUT_PARTITION, {})
    holdout_sparse = sparse_metrics.get(HOLDOUT_PARTITION, {})
    holdout_reranker = reranker_metrics.get(HOLDOUT_PARTITION, {})
    semantic_recall = holdout_semantic.get("recall_at_1", 0.0)
    semantic_false_answer_rate = holdout_semantic.get("false_answer_rate", 0.0)
    semantic_gate_values = {
        "engineering_holdout_recall_at_1": semantic_recall,
        "engineering_holdout_recall_delta_vs_sparse": semantic_recall - holdout_sparse.get("recall_at_1", 0.0),
        "engineering_holdout_false_answer_rate": semantic_false_answer_rate,
        "peak_memory_mib": semantic_peak_rss / 1_048_576,
    }
    semantic_checks = {
        "engineering_holdout_recall_at_1": semantic_gate_values.get("engineering_holdout_recall_at_1", 0.0)
        >= semantic_gates.get("engineering_holdout_recall_at_1_min", 0.0),
        "engineering_holdout_recall_delta_vs_sparse": semantic_gate_values.get("engineering_holdout_recall_delta_vs_sparse", 0.0)
        >= semantic_gates.get("engineering_holdout_recall_delta_vs_sparse_min", 0.0),
        "engineering_holdout_false_answer_rate": semantic_gate_values.get("engineering_holdout_false_answer_rate", 0.0)
        <= semantic_gates.get("engineering_holdout_false_answer_rate_max", 0.0),
        "peak_memory_mib": semantic_gate_values.get("peak_memory_mib", 0.0) <= semantic_gates.get("peak_memory_mib_max", 0.0),
    }
    reranker_gate_values = {
        "engineering_holdout_recall_delta": holdout_reranker.get("recall_at_1", 0.0) - semantic_recall,
        "engineering_holdout_false_answer_delta": holdout_reranker.get("false_answer_rate", 0.0) - semantic_false_answer_rate,
        "peak_memory_mib": reranker_peak_bytes / 1_048_576,
    }
    reranker_checks = {
        "engineering_holdout_recall_delta": reranker_gate_values.get("engineering_holdout_recall_delta", 0.0)
        >= reranker_gates.get("engineering_holdout_recall_delta_min", 0.0),
        "engineering_holdout_false_answer_delta": reranker_gate_values.get("engineering_holdout_false_answer_delta", 0.0)
        <= reranker_gates.get("engineering_holdout_false_answer_delta_max", 0.0),
        "peak_memory_mib": reranker_gate_values.get("peak_memory_mib", 0.0) <= reranker_gates.get("peak_memory_mib_max", 0.0),
    }
    semantic_passed = all(semantic_checks.values())
    reranker_passed = all(reranker_checks.values())
    reranker_positive = reranker_gate_values.get("engineering_holdout_recall_delta", 0.0) > 0.0
    artifact_bytes = sum(
        value.stat().st_size
        for value in model_path.rglob("*")
        if value.is_file() and ".cache" not in value.relative_to(model_path).parts
    )
    result = {
        "generated_at": datetime.now(UTC).isoformat(),
        "source_state": benchmark_source_state(),
        "corpus": {
            "path": corpus_path.as_posix(),
            "query_count": len(queries),
            "evaluation_role": corpus.get("evaluation_role", ""),
        },
        "artifact": {
            # The manifest's identity fields; the model path stays out of the published report.
            **{
                name: manifest.get(name, default) for name, default in MODEL_MANIFEST_FIELD_DEFAULTS.items() if name != "model_path"
            },
            "actual_sha256": actual_checksum,
            "size_bytes": artifact_bytes,
            "runtime_downloads_allowed": False,
        },
        "backends": {
            "native": {
                "available": True,
                "cold_start_ms": cold_start_ms,
                "request_local_embedding_ms": request_local_embedding_ms,
                "query_p50_ms": percentile(semantic_latencies, 0.50),
                "query_p95_ms": percentile(semantic_latencies, 0.95),
                "query_p99_ms": percentile(semantic_latencies, 0.99),
                "query_maximum_ms": max(semantic_latencies, default=0.0),
                "timing_gate_applied": False,
                "throughput_queries_per_second": len(semantic_latencies) / elapsed_query_seconds if elapsed_query_seconds else 0.0,
                "rss_before_mib": rss_before / 1_048_576,
                "rss_after_model_mib": rss_after_model / 1_048_576,
                "rss_after_request_mib": rss_after_request / 1_048_576,
                "rss_after_queries_mib": rss_after_queries / 1_048_576,
                "peak_rss_mib": semantic_peak_rss / 1_048_576,
                "peak_memory_measurement": "process resident-set high-water mark after model load, warmup and all queries",
                "artifact_count": len(artifacts),
                "maximum_repeated_score_drift": drift_max,
                "metrics": semantic_metrics,
            },
            "onnx": {
                "available": False,
                "runtime_available": bool(importlib_util.find_spec("onnxruntime") and importlib_util.find_spec("optimum")),
                "reason": "no approved pre-provisioned ONNX artifact",
            },
            "quantized": {
                "available": False,
                "reason": "no approved pre-provisioned quantized artifact",
            },
        },
        "sparse_baseline": {"metrics": sparse_metrics},
        "reranker": {
            "implementation": "transparent_logistic",
            "model_version": "transparent-logistic",
            "metrics": reranker_metrics,
            "p50_overhead_ms": percentile(reranker_latencies, 0.50),
            "p95_overhead_ms": percentile(reranker_latencies, 0.95),
            "p99_overhead_ms": percentile(reranker_latencies, 0.99),
            "maximum_overhead_ms": max(reranker_latencies, default=0.0),
            "peak_traced_bytes": reranker_peak_bytes,
            "peak_memory_measurement": "traced Python allocation high-water mark of reranking every shortlist",
            "timing_gate_applied": False,
            "health": reranker.health(),
            "pairwise_encoder": {"available": False, "reason": "no approved local pairwise model artifact"},
        },
        "gates": {
            "semantic": {
                "thresholds": semantic_gates,
                "values": semantic_gate_values,
                "checks": semantic_checks,
                "passed": semantic_passed,
                "promoted_for_opt_in_component_use": semantic_passed,
            },
            "reranker": {
                "thresholds": reranker_gates,
                "values": reranker_gate_values,
                "checks": reranker_checks,
                "passed": reranker_passed,
                "positive_value_observed": reranker_positive,
                "promoted_for_opt_in_component_use": reranker_passed and reranker_positive,
            },
        },
        "queries": {"sparse": sparse_rows, "semantic": semantic_rows, "reranker": reranker_rows},
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json_dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json_dumps({"output": output_path.as_posix(), "gates": result.get("gates", {})}, sort_keys=True))
    exit_code = 0 if semantic_passed and reranker_passed else 1
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
