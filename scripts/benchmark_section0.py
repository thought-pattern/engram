"""Reproducible offline performance baseline for tracking Section 0."""

import argparse
import gc
import json
import os
import platform
import subprocess
import sys
import tempfile
import time
import tracemalloc
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from importlib import metadata
from pathlib import Path
from typing import Any, cast

REPOSITORY = Path(__file__).resolve().parents[1]
if str(REPOSITORY) not in sys.path:
    sys.path.insert(0, str(REPOSITORY))

from engram import persistence
from engram.config import engram_config, graph_config
from engram.core import Engram
from engram.service import EngramCore
from scripts.benchmark_metadata import benchmark_source_state

DEFAULT_OUTPUT = Path("documentation/baseline/benchmark-2026-08-19.json")
PACKAGE_NAMES = (
    "grpcio",
    "grpcio-tools",
    "nltk",
    "protobuf",
    "pymgclient",
    "sentence-transformers",
    "spacy",
)


class SyntheticVectorGraph:
    """Offline fixed-result graph used to measure support intersection cost."""

    def __init__(self, claim_id: str) -> None:
        self.available = True
        self.rows = [
            {
                "claim_id": claim_id,
                "subject": "Synthetic subject",
                "predicate": "supports",
                "object": "Synthetic object",
                "similarity": 0.84,
            }
        ]

    def vector_search_claims(self, embedding: list[float], **kwargs) -> list[dict]:
        result = list(self.rows)
        return result

    def execute_read(self, query: str, parameters=()) -> list[dict]:
        result = []
        return result


def _benchmark_config(capacity: int) -> dict:
    result = engram_config(
        capacity=max(capacity + 10, 100),
        learn_user_facts=False,
        polish_responses=False,
        use_lemmatization=False,
        use_spell_correction=False,
        use_stemming=False,
        use_synonyms=False,
    )
    return result


def _percentile(samples: list[float], fraction: float) -> float:
    ordered = sorted(samples)
    index = min(len(ordered) - 1, max(0, int(round((len(ordered) - 1) * fraction))))
    result = ordered[index]
    return result


def _measure(operation: Callable[[], object], iterations: int) -> dict:
    samples = []
    for _ in range(iterations):
        started = time.perf_counter_ns()
        operation()
        samples.append((time.perf_counter_ns() - started) / 1_000_000)
    result = {
        "iterations": iterations,
        "minimum_ms": round(min(samples), 6),
        "p50_ms": round(_percentile(samples, 0.50), 6),
        "p95_ms": round(_percentile(samples, 0.95), 6),
        "p99_ms": round(_percentile(samples, 0.99), 6),
        "maximum_ms": round(max(samples), 6),
    }
    return result


def _memory_build[BuildValue](builder: Callable[[], BuildValue]) -> tuple[BuildValue, dict]:
    gc.collect()
    tracemalloc.start()
    value = builder()
    current, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    result = value, {"current_bytes": current, "peak_bytes": peak}
    return result


def _build_lexical_core(corpus_size: int) -> tuple[EngramCore, str, dict]:
    def build() -> tuple[EngramCore, str]:
        engram = Engram(config=_benchmark_config(corpus_size))
        for index in range(max(0, corpus_size - 1)):
            engram.store(
                f"Synthetic response {index}.",
                keyword_source=f"noise-token-{index} archive-entry-{index}",
            )
        target_id = engram.learn_from_response(
            "What are the baseline support hours?",
            "Baseline support is open from nine to five.",
            source_label="section0:benchmark",
        )
        result = EngramCore(engram, checkpoint_on_mutation=False), target_id
        return result

    built, memory = _memory_build(build)
    core, target_id = built
    result = core, target_id, memory
    return result


def _lexical_result(corpus_size: int, iterations: int) -> dict:
    core, target_id, memory = _build_lexical_core(corpus_size)
    request = "What are the baseline support hours?"
    sequence = [0]
    candidate_ids = []

    def unique_proposal() -> object:
        request_id = f"lexical-{corpus_size}-{sequence[0]}"
        sequence[0] += 1
        proposal = core.propose(request, request_id=request_id)
        candidate_ids.append(proposal["candidates"][0]["statement_id"] if proposal["candidates"] else "")
        return proposal

    lexical_latency = _measure(unique_proposal, iterations)
    core.propose(request, request_id=f"retry-{corpus_size}")
    retry_latency = _measure(lambda: core.propose(request, request_id=f"retry-{corpus_size}"), iterations)
    retry = core.propose(request, request_id=f"retry-{corpus_size}")

    serialized = persistence.save_json(core.engram)
    persistence_iterations = max(3, min(10, iterations // 3))
    serialize_latency = _measure(lambda: persistence.save_json(core.engram), persistence_iterations)
    load_latency = _measure(lambda: persistence.load_engram_json(serialized), persistence_iterations)
    with tempfile.TemporaryDirectory(prefix="engram-section0-") as directory:
        store_path = Path(directory) / "engram.json"
        save_latency = _measure(lambda: persistence.save(core.engram, store_path), persistence_iterations)

    result = {
        "corpus_size": corpus_size,
        "memory": memory,
        "unique_request_proposal": lexical_latency,
        "idempotent_retry": retry_latency,
        "repeated_request": {
            "expected_statement_id": target_id,
            "all_candidates_stable": bool(candidate_ids) and set(candidate_ids) == {target_id},
            "retry_marked_idempotent": retry["idempotent"],
        },
        "persistence": {
            "serialized_bytes": len(serialized.encode("utf-8")),
            "serialize": serialize_latency,
            "atomic_save": save_latency,
            "load": load_latency,
        },
    }
    return result


def _build_vector_core(corpus_size: int, support_fanout: int) -> tuple[EngramCore, dict]:
    claim_id = "claim-section0-synthetic"

    def build() -> EngramCore:
        config = _benchmark_config(corpus_size)
        config["graph"] = graph_config(enabled=False, vector_enabled=False)
        engram = Engram(config=config)
        noise_count = max(0, corpus_size - support_fanout)
        for index in range(noise_count):
            engram.store(
                f"Unrelated response {index}.",
                keyword_source=f"unrelated-token-{index}",
                template={
                    "tapestry": {
                        "namespace": "benchmark",
                        "context_fingerprint": "section0-v1",
                        "support": [{"claim_id": f"noise-claim-{index}"}],
                    }
                },
            )
        for index in range(support_fanout):
            engram.store(
                f"Supported response {index}.",
                keyword_source=f"supported-cache-token-{index}",
                source_label="section0:benchmark",
                template={
                    "tapestry": {
                        "namespace": "benchmark",
                        "context_fingerprint": "section0-v1",
                        "support": [{"claim_id": claim_id}],
                    }
                },
            )
        engram.config["graph"]["enabled"] = True
        engram.config["graph"]["vector_enabled"] = True
        benchmark_engram = cast(Any, engram)
        benchmark_engram._graph_client = SyntheticVectorGraph(claim_id)
        benchmark_engram._graph_embedding_model = object()
        benchmark_engram._encode_graph_query = lambda text: [0.0] * 384
        result = EngramCore(engram, checkpoint_on_mutation=False)
        return result

    core, memory = _memory_build(build)
    result = core, memory
    return result


def _vector_result(corpus_size: int, support_fanout: int, iterations: int) -> dict:
    core, memory = _build_vector_core(corpus_size, support_fanout)
    sequence = [0]
    candidate_counts = []

    def propose() -> object:
        request_id = f"vector-{corpus_size}-{support_fanout}-{sequence[0]}"
        sequence[0] += 1
        proposal = core.propose(
            "Semantic probe with orthogonal vocabulary",
            request_id=request_id,
            namespace="benchmark",
            context_fingerprint="section0-v1",
            limit=10,
        )
        candidate_counts.append(len(proposal["candidates"]))
        return proposal

    latency = _measure(propose, iterations)
    result = {
        "corpus_size": corpus_size,
        "support_fanout": support_fanout,
        "returned_candidate_limit": 10,
        "candidate_count_stable": len(set(candidate_counts)) == 1,
        "observed_candidate_count": candidate_counts[0] if candidate_counts else 0,
        "memory": memory,
        "proposal": latency,
    }
    return result


def _startup_result(iterations: int) -> dict:
    warm_iterations = max(5, iterations)
    cold_iterations = max(3, min(5, iterations // 5))
    warm = _measure(lambda: Engram(config=_benchmark_config(100)), warm_iterations)
    command = [sys.executable, "-c", "from engram.core import Engram; Engram()"]

    def cold_start() -> object:
        result = subprocess.run(command, cwd=REPOSITORY, check=True, capture_output=True, text=True)
        return result

    cold = _measure(cold_start, cold_iterations)
    result = {
        "warm_process_core_construction": warm,
        "cold_process_import_and_construction": cold,
        "mode": "offline graph-disabled startup",
    }
    return result


def _package_versions() -> dict[str, str]:
    versions = {}
    for package in PACKAGE_NAMES:
        try:
            versions[package] = metadata.version(package)
        except metadata.PackageNotFoundError:
            versions[package] = ""
    return versions


def _reported[Result](label: str, operation: Callable[[], Result]) -> Result:
    """Report long setup stages without including them in latency samples."""
    print(f"[section0] starting {label}", file=sys.stderr, flush=True)
    started = time.perf_counter()
    result = operation()
    elapsed = time.perf_counter() - started
    print(f"[section0] completed {label} in {elapsed:.3f}s", file=sys.stderr, flush=True)
    return result


def run_benchmark(sizes: list[int], fanouts: list[int], iterations: int) -> dict:
    lexical = [
        _reported(
            f"lexical corpus_size={size}",
            lambda size=size: _lexical_result(size, iterations),
        )
        for size in sizes
    ]
    vector = [
        _reported(
            f"support-vector corpus_size={size} fanout={fanout}",
            lambda size=size, fanout=fanout: _vector_result(size, fanout, iterations),
        )
        for size in sizes
        for fanout in fanouts
        if fanout <= size
    ]
    result = {
        "artifact_schema_version": 1,
        "captured_at": datetime.now(UTC).isoformat(),
        "source": benchmark_source_state(),
        "environment": {
            "python": platform.python_version(),
            "platform": platform.platform(),
            "processor": platform.processor(),
            "logical_cpu_count": os.cpu_count() or 0,
            "packages": _package_versions(),
        },
        "method": {
            "clock": "time.perf_counter_ns",
            "latency_unit": "milliseconds",
            "percentiles": "nearest observed rank over per-call samples",
            "iterations": iterations,
            "corpus_sizes": sizes,
            "support_fanouts": fanouts,
            "memory": "tracemalloc current and peak bytes during isolated corpus construction",
            "external_services": "none; vector results and embeddings are deterministic synthetic fixtures",
        },
        "startup": _startup_result(iterations),
        "lexical": lexical,
        "support_vector": vector,
    }
    return result


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--sizes", type=int, nargs="+", default=[100, 1000, 5000])
    parser.add_argument("--fanouts", type=int, nargs="+", default=[1, 10, 100])
    parser.add_argument("--iterations", type=int, default=30)
    return parser


def main(argv: Sequence[str] = ()) -> int:
    args = _parser().parse_args(argv)
    if args.iterations < 5:
        raise ValueError("iterations must be at least 5")
    if any(size < 1 for size in args.sizes):
        raise ValueError("corpus sizes must be positive")
    if any(fanout < 1 for fanout in args.fanouts):
        raise ValueError("support fanouts must be positive")
    result = run_benchmark(sorted(set(args.sizes)), sorted(set(args.fanouts)), args.iterations)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(args.output)
    result = 0
    return result


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
