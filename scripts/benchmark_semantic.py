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

from engram.artifacts import validate_cached_response_artifact
from engram.config import reranker_config, semantic_config, sparse_config
from engram.constants_artifacts import INITIAL_ARTIFACT_STATISTICS, LifecycleState, Tier
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
    "model_revision": "",
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


def load_corpus(corpus_path: Path) -> dict:
    """Read the corpus; refuse one lacking a field, a query field, the held-out partition or a gate threshold."""
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
    return corpus


def load_manifest(manifest_path: Path) -> dict:
    """Read the model manifest; refuse one lacking a field the benchmark reads."""
    manifest = json_loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(manifest, dict) or not MODEL_MANIFEST_FIELD_DEFAULTS.keys() <= manifest.keys():
        raise SystemExit(f"model manifest must be an object with {', '.join(sorted(MODEL_MANIFEST_FIELD_DEFAULTS))}")
    return manifest


def verified_artifact_checksum(model_path: Path, expected_checksum: str) -> str:
    """Hash the model artifact and refuse one that does not match its manifest."""
    actual_checksum = model_artifact_sha256(model_path)
    if actual_checksum != expected_checksum:
        raise SystemExit("model artifact checksum does not match manifest")
    return actual_checksum


def manifest_semantic_settings(manifest: dict, model_path: Path):
    """Semantic retrieval settings for the manifest's model."""
    settings = semantic_config(
        enabled=True,
        model_path=model_path.as_posix(),
        model_id=manifest.get("model_id", ""),
        model_revision=manifest.get("model_revision", ""),
        license_id=manifest.get("license_id", ""),
        artifact_sha256=manifest.get("artifact_sha256", ""),
        dimension=manifest.get("dimension", 0),
        backend=manifest.get("backend", ""),
        min_similarity=0.45,
    )
    return settings


def benchmark_artifacts(documents: list, selected_scope) -> tuple:
    """Turn each corpus document into one accepted response in the benchmark scope."""
    accepted = []
    for document in documents:
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
    result = tuple(accepted)
    return result


def semantic_search(semantic, text: str, selected_scope, artifacts: tuple) -> dict:
    """One bounded semantic search over the benchmark artifacts."""
    result = semantic.search(
        text,
        selected_scope,
        artifacts,
        limit=8,
        max_vector_results=8,
        max_working_memory_bytes=16_777_216,
    )
    return result


def load_semantic_model(semantic_settings, process) -> tuple:
    """Load the native model, timing its cold start and reading resident memory around it."""
    rss_before = process.memory_info().rss
    cold_started = time_perf_counter_ns()
    semantic = StandaloneSemanticRetriever(semantic_settings)
    cold_start_ms = (time_perf_counter_ns() - cold_started) / 1_000_000
    if not semantic.available:
        raise SystemExit(f"native semantic model unavailable: {semantic.last_error}")
    measurements = {"rss_before": rss_before, "cold_start_ms": cold_start_ms, "rss_after_model": process.memory_info().rss}
    return semantic, measurements


def warm_semantic_model(semantic, selected_scope, artifacts: tuple, process) -> dict:
    """Time one request-local embedding and read resident memory after it."""
    request_local_started = time_perf_counter_ns()
    semantic_search(semantic, "semantic benchmark warmup", selected_scope, artifacts)
    request_local_embedding_ms = (time_perf_counter_ns() - request_local_started) / 1_000_000
    measurements = {"request_local_embedding_ms": request_local_embedding_ms, "rss_after_request": process.memory_info().rss}
    return measurements


def run_semantic_phase(semantic, queries: list, selected_scope, artifacts: tuple) -> dict:
    """Search every query twice, timing the first search and recording the largest score drift between the two."""
    rows = []
    latencies = []
    results = []
    drift_max = 0.0
    for query in queries:
        started = time_perf_counter_ns()
        semantic_result = semantic_search(semantic, query.get("text", ""), selected_scope, artifacts)
        semantic_ms = (time_perf_counter_ns() - started) / 1_000_000
        latencies.append(semantic_ms)
        repeated = semantic_search(semantic, query.get("text", ""), selected_scope, artifacts)
        first_scores = {value.get("statement_id", ""): value.get("similarity", 0.0) for value in semantic_result.get("matches", ())}
        second_scores = {value.get("statement_id", ""): value.get("similarity", 0.0) for value in repeated.get("matches", ())}
        drift_max = max(
            drift_max,
            max((abs(score - second_scores.get(key, 0.0)) for key, score in first_scores.items()), default=0.0),
        )
        semantic_ids = [value.get("statement_id", "") for value in semantic_result.get("matches", ())]
        results.append(semantic_result)
        rows.append(
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
    phase = {"rows": rows, "latencies": latencies, "results": results, "drift_max": drift_max}
    return phase


def reranker_shortlist(semantic_result: dict, sparse_scores: dict) -> tuple:
    """The semantic candidate list with each candidate's lexical score attached."""
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
    result = tuple(candidates)
    return result


def run_sparse_phase(queries: list, semantic_results: list, selected_scope, artifacts: tuple, sparse_settings) -> dict:
    """Search every query sparsely and build each query's reranker shortlist."""
    rows = []
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
        rows.append(
            {
                "query_id": query.get("query_id", ""),
                "partition": query.get("partition", ""),
                "expected_statement_id": query.get("expected_statement_id", ""),
                "top_statement_id": sparse_ids[0] if sparse_ids else "",
                "rank": rank(sparse_ids, query.get("expected_statement_id", "")),
                "candidate_ids": sparse_ids,
            }
        )
        shortlists.append(reranker_shortlist(semantic_result, sparse_scores))
    phase = {"rows": rows, "shortlists": shortlists}
    return phase


def run_reranker_phase(reranker, queries: list, semantic_results: list, shortlists: list) -> dict:
    """Rerank every shortlist, timing each; an unapplied rerank keeps the semantic order."""
    rows = []
    latencies = []
    for query, semantic_result, shortlist in zip(queries, semantic_results, shortlists, strict=True):
        semantic_ids = [value.get("statement_id", "") for value in semantic_result.get("matches", ())]
        rerank_started = time_perf_counter_ns()
        reranked = reranker.rerank(shortlist)
        rerank_ms = (time_perf_counter_ns() - rerank_started) / 1_000_000
        latencies.append(rerank_ms)
        if reranked.get("applied", False):
            reranked_ids = [value.get("statement_id", "") for value in reranked.get("scores", ())]
        else:
            reranked_ids = semantic_ids
        rows.append(
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
    phase = {"rows": rows, "latencies": latencies}
    return phase


def reranker_peak_traced_bytes(reranker, shortlists: list) -> int:
    """The reranker's traced Python allocation high-water mark over every shortlist.

    The reranker is pure Python, so traced allocations give its own high-water mark. Tracing
    runs as a separate pass so it does not distort the timed phase, and each result is
    discarded as the service would.
    """
    tracemalloc_start()
    for shortlist in shortlists:
        reranker.rerank(shortlist)
    _, peak_bytes = tracemalloc_get_traced_memory()
    tracemalloc_stop()
    return peak_bytes


def semantic_gate_report(holdout_semantic: dict, holdout_sparse: dict, semantic_peak_rss: int, thresholds: dict) -> dict:
    """Judge the semantic held-out metrics and peak resident memory against their thresholds."""
    semantic_recall = holdout_semantic.get("recall_at_1", 0.0)
    values = {
        "engineering_holdout_recall_at_1": semantic_recall,
        "engineering_holdout_recall_delta_vs_sparse": semantic_recall - holdout_sparse.get("recall_at_1", 0.0),
        "engineering_holdout_false_answer_rate": holdout_semantic.get("false_answer_rate", 0.0),
        "peak_memory_mib": semantic_peak_rss / 1_048_576,
    }
    checks = {
        "engineering_holdout_recall_at_1": values.get("engineering_holdout_recall_at_1", 0.0)
        >= thresholds.get("engineering_holdout_recall_at_1_min", 0.0),
        "engineering_holdout_recall_delta_vs_sparse": values.get("engineering_holdout_recall_delta_vs_sparse", 0.0)
        >= thresholds.get("engineering_holdout_recall_delta_vs_sparse_min", 0.0),
        "engineering_holdout_false_answer_rate": values.get("engineering_holdout_false_answer_rate", 0.0)
        <= thresholds.get("engineering_holdout_false_answer_rate_max", 0.0),
        "peak_memory_mib": values.get("peak_memory_mib", 0.0) <= thresholds.get("peak_memory_mib_max", 0.0),
    }
    passed = all(checks.values())
    report = {
        "thresholds": thresholds,
        "values": values,
        "checks": checks,
        "passed": passed,
        "promoted_for_opt_in_component_use": passed,
    }
    return report


def reranker_gate_report(holdout_reranker: dict, holdout_semantic: dict, reranker_peak_bytes: int, thresholds: dict) -> dict:
    """Judge the reranker's held-out change over semantic retrieval and its traced memory against their thresholds."""
    values = {
        "engineering_holdout_recall_delta": holdout_reranker.get("recall_at_1", 0.0) - holdout_semantic.get("recall_at_1", 0.0),
        "engineering_holdout_false_answer_delta": holdout_reranker.get("false_answer_rate", 0.0)
        - holdout_semantic.get("false_answer_rate", 0.0),
        "peak_memory_mib": reranker_peak_bytes / 1_048_576,
    }
    checks = {
        "engineering_holdout_recall_delta": values.get("engineering_holdout_recall_delta", 0.0)
        >= thresholds.get("engineering_holdout_recall_delta_min", 0.0),
        "engineering_holdout_false_answer_delta": values.get("engineering_holdout_false_answer_delta", 0.0)
        <= thresholds.get("engineering_holdout_false_answer_delta_max", 0.0),
        "peak_memory_mib": values.get("peak_memory_mib", 0.0) <= thresholds.get("peak_memory_mib_max", 0.0),
    }
    passed = all(checks.values())
    positive = values.get("engineering_holdout_recall_delta", 0.0) > 0.0
    report = {
        "thresholds": thresholds,
        "values": values,
        "checks": checks,
        "passed": passed,
        "positive_value_observed": positive,
        "promoted_for_opt_in_component_use": passed and positive,
    }
    return report


def artifact_report(manifest: dict, actual_checksum: str, model_path: Path) -> dict:
    """The manifest's identity fields with the measured checksum and on-disk size; the model path stays out."""
    artifact_bytes = sum(
        value.stat().st_size
        for value in model_path.rglob("*")
        if value.is_file() and ".cache" not in value.relative_to(model_path).parts
    )
    report = {
        **{name: manifest.get(name, default) for name, default in MODEL_MANIFEST_FIELD_DEFAULTS.items() if name != "model_path"},
        "actual_sha256": actual_checksum,
        "size_bytes": artifact_bytes,
        "runtime_downloads_allowed": False,
    }
    return report


def backends_report(native: dict, semantic_metrics: dict) -> dict:
    """The native backend's timing, memory and retrieval measurements beside the unprovisioned backends."""
    latencies = native.get("latencies", [])
    elapsed_query_seconds = sum(latencies) / 1000.0
    report = {
        "native": {
            "available": True,
            "cold_start_ms": native.get("cold_start_ms", 0.0),
            "request_local_embedding_ms": native.get("request_local_embedding_ms", 0.0),
            "query_p50_ms": percentile(latencies, 0.50),
            "query_p95_ms": percentile(latencies, 0.95),
            "query_p99_ms": percentile(latencies, 0.99),
            "query_maximum_ms": max(latencies, default=0.0),
            "timing_gate_applied": False,
            "throughput_queries_per_second": len(latencies) / elapsed_query_seconds if elapsed_query_seconds else 0.0,
            "rss_before_mib": native.get("rss_before", 0) / 1_048_576,
            "rss_after_model_mib": native.get("rss_after_model", 0) / 1_048_576,
            "rss_after_request_mib": native.get("rss_after_request", 0) / 1_048_576,
            "rss_after_queries_mib": native.get("rss_after_queries", 0) / 1_048_576,
            "peak_rss_mib": native.get("peak_rss", 0) / 1_048_576,
            "peak_memory_measurement": "process resident-set high-water mark after model load, warmup and all queries",
            "artifact_count": native.get("artifact_count", 0),
            "maximum_repeated_score_drift": native.get("drift_max", 0.0),
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
    }
    return report


def reranker_report(reranker, reranker_metrics: dict, latencies: list[float], peak_traced_bytes: int) -> dict:
    """The transparent reranker's retrieval metrics, overhead and traced memory."""
    report = {
        "implementation": "transparent_logistic",
        "metrics": reranker_metrics,
        "p50_overhead_ms": percentile(latencies, 0.50),
        "p95_overhead_ms": percentile(latencies, 0.95),
        "p99_overhead_ms": percentile(latencies, 0.99),
        "maximum_overhead_ms": max(latencies, default=0.0),
        "peak_traced_bytes": peak_traced_bytes,
        "peak_memory_measurement": "traced Python allocation high-water mark of reranking every shortlist",
        "timing_gate_applied": False,
        "health": reranker.health(),
        "pairwise_encoder": {"available": False, "reason": "no approved local pairwise model artifact"},
    }
    return report


def main() -> int:
    args = parse_args()
    corpus_path = Path(args.corpus)
    output_path = Path(args.output)
    corpus = load_corpus(corpus_path)
    queries = corpus.get("queries", [])
    gates = corpus.get("gates", {})
    manifest = load_manifest(Path(args.manifest))
    model_path = Path(manifest.get("model_path", ""))
    actual_checksum = verified_artifact_checksum(model_path, manifest.get("artifact_sha256", ""))
    semantic_settings = manifest_semantic_settings(manifest, model_path)
    selected_scope = scope_key(namespace="semantic-benchmark")
    artifacts = benchmark_artifacts(corpus.get("documents", []), selected_scope)
    process = psutil_Process()
    semantic, model_measurements = load_semantic_model(semantic_settings, process)
    sparse_settings = sparse_config(enabled=True)
    reranker = TransparentLogisticReranker(reranker_config(enabled=True, shortlist_size=8))
    warmup_measurements = warm_semantic_model(semantic, selected_scope, artifacts, process)
    # Each component runs as its own phase so its memory can be attributed.
    # Every semantic search runs first; the process high-water mark read after
    # that phase bounds the model load, warmup and query work.
    semantic_phase = run_semantic_phase(semantic, queries, selected_scope, artifacts)
    semantic_peak_rss = peak_rss_bytes()
    rss_after_queries = process.memory_info().rss
    semantic_results = semantic_phase.get("results", [])
    sparse_phase = run_sparse_phase(queries, semantic_results, selected_scope, artifacts, sparse_settings)
    shortlists = sparse_phase.get("shortlists", [])
    reranker_phase = run_reranker_phase(reranker, queries, semantic_results, shortlists)
    reranker_peak_bytes = reranker_peak_traced_bytes(reranker, shortlists)
    semantic_metrics = evaluate_rows(semantic_phase.get("rows", []))
    sparse_metrics = evaluate_rows(sparse_phase.get("rows", []))
    reranker_metrics = evaluate_rows(reranker_phase.get("rows", []))
    # Every metric set holds the held-out partition, which the corpus check requires.
    holdout_semantic = semantic_metrics.get(HOLDOUT_PARTITION, {})
    semantic_gate = semantic_gate_report(
        holdout_semantic, sparse_metrics.get(HOLDOUT_PARTITION, {}), semantic_peak_rss, gates.get("semantic", {})
    )
    reranker_gate = reranker_gate_report(
        reranker_metrics.get(HOLDOUT_PARTITION, {}), holdout_semantic, reranker_peak_bytes, gates.get("reranker", {})
    )
    native = {
        **model_measurements,
        **warmup_measurements,
        "rss_after_queries": rss_after_queries,
        "peak_rss": semantic_peak_rss,
        "latencies": semantic_phase.get("latencies", []),
        "drift_max": semantic_phase.get("drift_max", 0.0),
        "artifact_count": len(artifacts),
    }
    artifact = artifact_report(manifest, actual_checksum, model_path)
    result = {
        "generated_at": datetime.now(UTC).isoformat(),
        "source_state": benchmark_source_state(),
        "corpus": {
            "path": corpus_path.as_posix(),
            "query_count": len(queries),
            "evaluation_role": corpus.get("evaluation_role", ""),
        },
        "artifact": artifact,
        "backends": backends_report(native, semantic_metrics),
        "sparse_baseline": {"metrics": sparse_metrics},
        "reranker": reranker_report(reranker, reranker_metrics, reranker_phase.get("latencies", []), reranker_peak_bytes),
        "gates": {"semantic": semantic_gate, "reranker": reranker_gate},
        "queries": {
            "sparse": sparse_phase.get("rows", []),
            "semantic": semantic_phase.get("rows", []),
            "reranker": reranker_phase.get("rows", []),
        },
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json_dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json_dumps({"output": output_path.as_posix(), "gates": result.get("gates", {})}, sort_keys=True))
    exit_code = 0 if semantic_gate.get("passed", False) and reranker_gate.get("passed", False) else 1
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
