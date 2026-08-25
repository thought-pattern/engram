"""Benchmark Section 13 semantic retrieval and transparent reranking."""

import argparse
import importlib.util
import json
import math
import statistics
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import psutil

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from engram.artifacts import LifecycleState, artifact_provenance, artifact_statistics, cached_response_artifact
from engram.config import reranker_config, semantic_config, sparse_config
from engram.constants import Tier
from engram.identity import build_retrieval_representation, build_standalone_identity, scope_key
from engram.reranking import TransparentLogisticReranker
from engram.semantic import StandaloneSemanticIndexOwner, model_artifact_sha256
from engram.sparse import SparseIndexOwner
from scripts.benchmark_metadata import benchmark_source_state


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus", default="eval/section13-semantic-v1.json")
    parser.add_argument(
        "--manifest",
        default="data/artifacts/models/all-MiniLM-L6-v2-826711e5.engram-model.json",
    )
    parser.add_argument("--output", default="documentation/semantic/benchmark-2026-08-22.json")
    return parser.parse_args()


def percentile(values: list[float], quantile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, math.ceil(quantile * len(ordered)) - 1))
    return ordered[index]


def artifact(value: dict) -> dict:
    request = str(value["canonical"])
    selected_scope = scope_key(namespace="semantic-benchmark")
    return cached_response_artifact(
        statement_id=str(value["statement_id"]),
        generation=1,
        response=str(value["response"]),
        query_identity=build_standalone_identity(request, selected_scope),
        retrieval=build_retrieval_representation(request, tuple(value["aliases"])),
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
        provenance=artifact_provenance("section13-benchmark", "engineering", "2026-08-22T00:00:00Z"),
        statistics=artifact_statistics(),
        metadata={},
    )


def rank(statement_ids: list[str], expected: str) -> int:
    return statement_ids.index(expected) + 1 if expected in statement_ids else 0


def partition_metrics(rows: list[dict]) -> dict:
    positives = [row for row in rows if row["expected_statement_id"]]
    reciprocal = [1.0 / row["rank"] if row["rank"] else 0.0 for row in positives]
    false_answers = [row for row in rows if row["top_statement_id"] and row["top_statement_id"] != row["expected_statement_id"]]
    return {
        "query_count": len(rows),
        "positive_count": len(positives),
        "recall_at_1": sum(row["rank"] == 1 for row in positives) / len(positives) if positives else 0.0,
        "recall_at_3": sum(0 < row["rank"] <= 3 for row in positives) / len(positives) if positives else 0.0,
        "mrr": statistics.fmean(reciprocal) if reciprocal else 0.0,
        "false_answer_rate": len(false_answers) / len(rows) if rows else 0.0,
    }


def evaluate_rows(rows: list[dict]) -> dict:
    partitions = sorted({str(row["partition"]) for row in rows})
    return {partition: partition_metrics([row for row in rows if row["partition"] == partition]) for partition in partitions}


def main() -> int:
    args = parse_args()
    corpus_path = Path(args.corpus)
    manifest_path = Path(args.manifest)
    output_path = Path(args.output)
    corpus = json.loads(corpus_path.read_text(encoding="utf-8"))
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    model_path = Path(manifest["model_path"])
    actual_checksum = model_artifact_sha256(model_path)
    if actual_checksum != manifest["artifact_sha256"]:
        raise SystemExit("model artifact checksum does not match manifest")
    semantic_settings = semantic_config(
        enabled=True,
        model_path=model_path.as_posix(),
        model_id=manifest["model_id"],
        model_version=manifest["model_version"],
        license_id=manifest["license_id"],
        artifact_sha256=manifest["artifact_sha256"],
        dimension=manifest["dimension"],
        backend=manifest["backend"],
        min_similarity=0.45,
    )
    artifacts = tuple(artifact(value) for value in corpus["documents"])
    process = psutil.Process()
    rss_before = process.memory_info().rss
    cold_started = time.perf_counter_ns()
    semantic = StandaloneSemanticIndexOwner(semantic_settings)
    cold_start_ms = (time.perf_counter_ns() - cold_started) / 1_000_000
    if not semantic.available:
        raise SystemExit(f"native semantic model unavailable: {semantic.last_error}")
    rss_after_model = process.memory_info().rss
    build_started = time.perf_counter_ns()
    semantic_state = semantic.rebuild(artifacts, 2)
    build_ms = (time.perf_counter_ns() - build_started) / 1_000_000
    rss_after_index = process.memory_info().rss
    sparse = SparseIndexOwner(sparse_config(enabled=True))
    sparse.rebuild(artifacts, 2)
    reranker = TransparentLogisticReranker(reranker_config(enabled=True, shortlist_size=8))
    selected_scope = scope_key(namespace="semantic-benchmark")
    semantic.search(
        "semantic benchmark warmup",
        selected_scope,
        limit=8,
        max_vector_results=8,
        max_working_memory_bytes=16_777_216,
    )
    semantic_rows = []
    sparse_rows = []
    reranker_rows = []
    semantic_latencies = []
    reranker_latencies = []
    drift_max = 0.0
    for query in corpus["queries"]:
        started = time.perf_counter_ns()
        semantic_result = semantic.search(
            query["text"],
            selected_scope,
            limit=8,
            max_vector_results=8,
            max_working_memory_bytes=16_777_216,
        )
        semantic_ms = (time.perf_counter_ns() - started) / 1_000_000
        semantic_latencies.append(semantic_ms)
        repeated = semantic.search(
            query["text"],
            selected_scope,
            limit=8,
            max_vector_results=8,
            max_working_memory_bytes=16_777_216,
        )
        first_scores = {value["statement_id"]: value["similarity"] for value in semantic_result["matches"]}
        second_scores = {value["statement_id"]: value["similarity"] for value in repeated["matches"]}
        drift_max = max(
            drift_max,
            max((abs(first_scores[key] - second_scores.get(key, 0.0)) for key in first_scores), default=0.0),
        )
        semantic_ids = [value["statement_id"] for value in semantic_result["matches"]]
        expected = query["expected_statement_id"]
        semantic_rows.append(
            {
                "query_id": query["query_id"],
                "partition": query["partition"],
                "expected_statement_id": expected,
                "top_statement_id": semantic_ids[0] if semantic_ids else "",
                "rank": rank(semantic_ids, expected),
                "latency_ms": semantic_ms,
                "candidate_ids": semantic_ids,
            }
        )
        sparse_result = sparse.search(
            query["text"],
            selected_scope,
            limit=8,
            max_working_memory_bytes=16_777_216,
        )
        sparse_scores = {value["statement_id"]: value["score"] for value in sparse_result["matches"]}
        sparse_ids = [value["statement_id"] for value in sparse_result["matches"]]
        sparse_rows.append(
            {
                "query_id": query["query_id"],
                "partition": query["partition"],
                "expected_statement_id": expected,
                "top_statement_id": sparse_ids[0] if sparse_ids else "",
                "rank": rank(sparse_ids, expected),
                "candidate_ids": sparse_ids,
            }
        )
        shortlist = tuple(
            {
                "statement_id": value["statement_id"],
                "base_score": value["similarity"],
                "features": {
                    "base_score": value["similarity"],
                    "semantic": value["similarity"],
                    "lexical": sparse_scores.get(value["statement_id"], 0.0),
                },
            }
            for value in semantic_result["matches"]
        )
        rerank_started = time.perf_counter_ns()
        reranked = reranker.rerank(shortlist)
        rerank_ms = (time.perf_counter_ns() - rerank_started) / 1_000_000
        reranker_latencies.append(rerank_ms)
        reranked_ids = [value["statement_id"] for value in reranked["scores"]] if reranked["applied"] else semantic_ids
        reranker_rows.append(
            {
                "query_id": query["query_id"],
                "partition": query["partition"],
                "expected_statement_id": expected,
                "top_statement_id": reranked_ids[0] if reranked_ids else "",
                "rank": rank(reranked_ids, expected),
                "latency_ms": rerank_ms,
                "candidate_ids": reranked_ids,
            }
        )
    elapsed_query_seconds = sum(semantic_latencies) / 1000.0
    semantic_metrics = evaluate_rows(semantic_rows)
    sparse_metrics = evaluate_rows(sparse_rows)
    reranker_metrics = evaluate_rows(reranker_rows)
    holdout_semantic = semantic_metrics["engineering_holdout"]
    holdout_sparse = sparse_metrics["engineering_holdout"]
    holdout_reranker = reranker_metrics["engineering_holdout"]
    gates = corpus["gates"]
    semantic_gate_values = {
        "engineering_holdout_recall_at_1": holdout_semantic["recall_at_1"],
        "engineering_holdout_recall_delta_vs_sparse": holdout_semantic["recall_at_1"] - holdout_sparse["recall_at_1"],
        "engineering_holdout_false_answer_rate": holdout_semantic["false_answer_rate"],
        "peak_memory_mib": max(rss_after_model, rss_after_index) / 1_048_576,
    }
    semantic_checks = {
        "engineering_holdout_recall_at_1": semantic_gate_values["engineering_holdout_recall_at_1"]
        >= gates["semantic"]["engineering_holdout_recall_at_1_min"],
        "engineering_holdout_recall_delta_vs_sparse": semantic_gate_values["engineering_holdout_recall_delta_vs_sparse"]
        >= gates["semantic"]["engineering_holdout_recall_delta_vs_sparse_min"],
        "engineering_holdout_false_answer_rate": semantic_gate_values["engineering_holdout_false_answer_rate"]
        <= gates["semantic"]["engineering_holdout_false_answer_rate_max"],
        "peak_memory_mib": semantic_gate_values["peak_memory_mib"] <= gates["semantic"]["peak_memory_mib_max"],
    }
    reranker_gate_values = {
        "engineering_holdout_recall_delta": holdout_reranker["recall_at_1"] - holdout_semantic["recall_at_1"],
        "engineering_holdout_false_answer_delta": holdout_reranker["false_answer_rate"] - holdout_semantic["false_answer_rate"],
        "peak_memory_mib": max(0, process.memory_info().rss - rss_after_index) / 1_048_576,
    }
    reranker_checks = {
        "engineering_holdout_recall_delta": reranker_gate_values["engineering_holdout_recall_delta"]
        >= gates["reranker"]["engineering_holdout_recall_delta_min"],
        "engineering_holdout_false_answer_delta": reranker_gate_values["engineering_holdout_false_answer_delta"]
        <= gates["reranker"]["engineering_holdout_false_answer_delta_max"],
        "peak_memory_mib": reranker_gate_values["peak_memory_mib"] <= gates["reranker"]["peak_memory_mib_max"],
    }
    artifact_bytes = sum(
        value.stat().st_size
        for value in model_path.rglob("*")
        if value.is_file() and ".cache" not in value.relative_to(model_path).parts
    )
    result = {
        "schema_version": 2,
        "generated_at": datetime.now(UTC).isoformat(),
        "source_state": benchmark_source_state(),
        "corpus": {
            "path": Path(args.corpus).as_posix(),
            "version": corpus["corpus_version"],
            "query_count": len(corpus["queries"]),
            "evaluation_role": corpus["evaluation_role"],
            "section16_release_eligible": corpus["section16_release_eligible"],
        },
        "artifact": {
            **{
                key: manifest[key] for key in ("model_id", "model_version", "license_id", "artifact_sha256", "dimension", "backend")
            },
            "actual_sha256": actual_checksum,
            "size_bytes": artifact_bytes,
            "runtime_downloads_allowed": False,
        },
        "backends": {
            "native": {
                "available": True,
                "cold_start_ms": cold_start_ms,
                "index_build_ms": build_ms,
                "query_p50_ms": percentile(semantic_latencies, 0.50),
                "query_p95_ms": percentile(semantic_latencies, 0.95),
                "query_p99_ms": percentile(semantic_latencies, 0.99),
                "query_maximum_ms": max(semantic_latencies, default=0.0),
                "timing_gate_applied": False,
                "throughput_queries_per_second": len(semantic_latencies) / elapsed_query_seconds if elapsed_query_seconds else 0.0,
                "rss_before_mib": rss_before / 1_048_576,
                "rss_after_model_mib": rss_after_model / 1_048_576,
                "rss_after_index_mib": rss_after_index / 1_048_576,
                "record_count": semantic_state["record_count"],
                "maximum_repeated_score_drift": drift_max,
                "metrics": semantic_metrics,
            },
            "onnx": {
                "available": False,
                "runtime_available": bool(importlib.util.find_spec("onnxruntime") and importlib.util.find_spec("optimum")),
                "reason": "no approved pre-provisioned ONNX artifact",
            },
            "quantized": {
                "available": False,
                "reason": "no approved pre-provisioned quantized artifact",
            },
        },
        "sparse_baseline": {"metrics": sparse_metrics},
        "reranker": {
            "implementation": "transparent_logistic_v1",
            "model_version": "transparent-logistic-v1",
            "metrics": reranker_metrics,
            "p50_overhead_ms": percentile(reranker_latencies, 0.50),
            "p95_overhead_ms": percentile(reranker_latencies, 0.95),
            "p99_overhead_ms": percentile(reranker_latencies, 0.99),
            "maximum_overhead_ms": max(reranker_latencies, default=0.0),
            "timing_gate_applied": False,
            "health": reranker.health(),
            "pairwise_encoder": {"available": False, "reason": "no approved local pairwise model artifact"},
        },
        "gates": {
            "semantic": {
                "thresholds": gates["semantic"],
                "values": semantic_gate_values,
                "checks": semantic_checks,
                "passed": all(semantic_checks.values()),
                "promoted_for_opt_in_component_use": all(semantic_checks.values()),
            },
            "reranker": {
                "thresholds": gates["reranker"],
                "values": reranker_gate_values,
                "checks": reranker_checks,
                "passed": all(reranker_checks.values()),
                "positive_value_observed": reranker_gate_values["engineering_holdout_recall_delta"] > 0.0,
                "promoted_for_opt_in_component_use": all(reranker_checks.values())
                and reranker_gate_values["engineering_holdout_recall_delta"] > 0.0,
            },
        },
        "queries": {"sparse": sparse_rows, "semantic": semantic_rows, "reranker": reranker_rows},
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({"output": output_path.as_posix(), "gates": result["gates"]}, sort_keys=True))
    return 0 if result["gates"]["semantic"]["passed"] and result["gates"]["reranker"]["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
