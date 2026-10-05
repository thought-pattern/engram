"""Measure current offline runtime latency, scaling, and memory use."""

from argparse import ArgumentParser as argparse_ArgumentParser
from datetime import UTC, datetime
from gc import collect as gc_collect
from importlib import metadata
from json import dumps as json_dumps
from os import cpu_count as os_cpu_count
from pathlib import Path
from platform import platform as platform_platform, processor as platform_processor, python_version as platform_python_version
from subprocess import run as subprocess_run
from sys import argv as sys_argv, executable as sys_executable, path as sys_path, stderr as sys_stderr
from time import perf_counter as time_perf_counter, perf_counter_ns as time_perf_counter_ns
from tracemalloc import get_traced_memory as tracemalloc_get_traced_memory, start as tracemalloc_start, stop as tracemalloc_stop

from sentence_transformers import SentenceTransformer

REPOSITORY = Path(__file__).resolve().parents[1]
if str(REPOSITORY) not in sys_path:
    sys_path.insert(0, str(REPOSITORY))

from engram.artifacts import LifecycleState, validate_cached_response_artifact
from engram.config import engram_config, graph_config
from engram.constants import INITIAL_ARTIFACT_STATISTICS, Tier
from engram.core import Engram
from engram.identity import extract_standalone_identity, retrieval_representation, scope_key
from engram.repository import ArtifactRepository
from engram.service import EngramCore
from scripts.benchmark_metadata import benchmark_source_state

DEFAULT_OUTPUT = Path("eval/results/runtime/benchmark.json")
PACKAGE_NAMES = (
    "grpcio",
    "grpcio-tools",
    "neo4j",
    "nltk",
    "protobuf",
    "sentence-transformers",
    "spacy",
)
# Every benchmark engine disables learning, polishing and lexical expansion so that only
# the measured lookup work is timed; capacity is sized per corpus at each construction.
BENCHMARK_CONFIG_OPTIONS = {
    "learn_user_facts": False,
    "polish_responses": False,
    "use_lemmatization": False,
    "use_spell_correction": False,
    "use_stemming": False,
    "use_synonyms": False,
}
# Read-only provenance shared by every synthetic artifact; artifact validation copies it.
RUNTIME_BENCHMARK_PROVENANCE = {
    "source_label": "runtime:benchmark",
    "caller_id": "engineering",
    "accepted_at": "2026-08-19T00:00:00Z",
}


class SyntheticVectorGraph:
    """Offline fixed-result graph used to measure support intersection cost."""

    def __init__(self, proposition_id: str) -> None:
        self.available = True
        self.rows = [
            {
                "proposition_id": proposition_id,
                "subject": "Synthetic subject",
                "predicate": "supports",
                "object": "Synthetic object",
                "similarity": 0.84,
            }
        ]

    def vector_search_propositions(self, embedding: list[float], **kwargs) -> list[dict]:
        result = list(self.rows)
        return result

    def execute(self, query: str, parameters=()) -> list[dict]:
        result = []
        return result


class SyntheticEmbeddingModel(SentenceTransformer):
    """Truthful benchmark marker for a locally replaced encoder operation."""

    def __init__(self) -> None:
        pass


def internal_percentile(samples: list[float], fraction: float) -> float:
    ordered = sorted(samples)
    index = min(len(ordered) - 1, max(0, int(round((len(ordered) - 1) * fraction))))
    result = ordered[index]
    return result


def internal_measure(operation: object, iterations: int) -> dict:
    if not callable(operation):
        raise ValueError("benchmark operation must be callable")
    samples = []
    for _ in range(iterations):
        started = time_perf_counter_ns()
        operation()
        samples.append((time_perf_counter_ns() - started) / 1_000_000)
    result = {
        "iterations": iterations,
        "minimum_ms": round(min(samples), 6),
        "p50_ms": round(internal_percentile(samples, 0.50), 6),
        "p95_ms": round(internal_percentile(samples, 0.95), 6),
        "p99_ms": round(internal_percentile(samples, 0.99), 6),
        "maximum_ms": round(max(samples), 6),
    }
    return result


def artifact_result(corpus_size: int, iterations: int) -> dict:
    # Corpus construction is traced in isolation: corpus_size - 1 unrelated artifacts and one target.
    gc_collect()
    tracemalloc_start()
    engram = Engram(config=engram_config(capacity=max(corpus_size + 10, 100), **BENCHMARK_CONFIG_OPTIONS))
    selected_scope = scope_key()
    artifacts = []
    for index in range(max(0, corpus_size - 1)):
        noise_request = f"noise-token-{index} archive-entry-{index}"
        artifacts.append(
            validate_cached_response_artifact(
                {
                    "statement_id": f"runtime-noise-{index}",
                    "generation": 1,
                    "response": f"Synthetic response {index}.",
                    "query_identity": extract_standalone_identity(noise_request, selected_scope),
                    "retrieval": retrieval_representation(noise_request),
                    "tier": Tier.STATIC,
                    "lifecycle": LifecycleState.ACTIVE,
                    "scope": selected_scope,
                    "support_references": (),
                    "valid_from": "",
                    "valid_from_available": False,
                    "valid_until": "",
                    "valid_until_available": False,
                    "superseded_by": "",
                    "provenance": RUNTIME_BENCHMARK_PROVENANCE,
                    "statistics": INITIAL_ARTIFACT_STATISTICS,
                    "metadata": {},
                }
            )
        )
    target_id = "runtime-target"
    request = "What are the baseline support hours?"
    artifacts.append(
        validate_cached_response_artifact(
            {
                "statement_id": target_id,
                "generation": 1,
                "response": "Baseline support is open from nine to five.",
                "query_identity": extract_standalone_identity(request, selected_scope),
                "retrieval": retrieval_representation(request),
                "tier": Tier.STATIC,
                "lifecycle": LifecycleState.ACTIVE,
                "scope": selected_scope,
                "support_references": (),
                "valid_from": "",
                "valid_from_available": False,
                "valid_until": "",
                "valid_until_available": False,
                "superseded_by": "",
                "provenance": RUNTIME_BENCHMARK_PROVENANCE,
                "statistics": INITIAL_ARTIFACT_STATISTICS,
                "metadata": {},
            }
        )
    )
    engram.response_repository = ArtifactRepository(tuple(artifacts))
    core = EngramCore(engram)
    # The working list is not retained by the core; release it before reading current bytes.
    del artifacts
    current_bytes, peak_bytes = tracemalloc_get_traced_memory()
    tracemalloc_stop()
    memory = {"current_bytes": current_bytes, "peak_bytes": peak_bytes}
    sequence = [0]
    candidate_ids = []

    def unique_proposal() -> dict:
        request_id = f"artifact-{corpus_size}-{sequence[0]}"
        sequence[0] += 1
        proposal = core.propose(request, request_id=request_id)
        candidates = proposal.get("candidates", [])
        candidate_ids.append(candidates[0].get("statement_id", "") if candidates else "")
        return proposal

    artifact_latency = internal_measure(unique_proposal, iterations)
    core.propose(request, request_id=f"retry-{corpus_size}")
    retry_latency = internal_measure(lambda: core.propose(request, request_id=f"retry-{corpus_size}"), iterations)
    retry = core.propose(request, request_id=f"retry-{corpus_size}")

    result = {
        "corpus_size": corpus_size,
        "memory": memory,
        "unique_request_proposal": artifact_latency,
        "idempotent_retry": retry_latency,
        "repeated_request": {
            "expected_statement_id": target_id,
            "all_candidates_stable": bool(candidate_ids) and set(candidate_ids) == {target_id},
            "retry_marked_idempotent": retry.get("idempotent", False),
        },
    }
    return result


def vector_result(corpus_size: int, support_fanout: int, iterations: int) -> dict:
    proposition_id = "prp_" + "a" * 64
    # Corpus construction is traced in isolation: unrelated artifacts plus support_fanout artifacts
    # citing the one Proposition the synthetic vector graph returns.
    gc_collect()
    tracemalloc_start()
    config = engram_config(capacity=max(corpus_size + 10, 100), **BENCHMARK_CONFIG_OPTIONS)
    config["graph"] = graph_config(enabled=False, vector_enabled=False)
    engram = Engram(config=config)
    selected_scope = scope_key(namespace="benchmark", context_fingerprint="runtime")
    # The current eight-field opaque support contract, as the Tapestry producer emits it.
    support_reference = {
        "schema_version": "tapestry-engram-support",
        "record_kind": "proposition",
        "id": proposition_id,
        "state_revision": 0,
        "support_revision": 0,
        "representation_contract": "representation-v1",
        "visibility_scope": {"kind": "global", "company_id": {}, "customer_id": {}, "engagement_id": {}},
        "dependency_state_digest": "dep_" + "a" * 64,
    }
    artifacts = []
    noise_count = max(0, corpus_size - support_fanout)
    for index in range(noise_count):
        request = f"unrelated-token-{index}"
        artifacts.append(
            validate_cached_response_artifact(
                {
                    "statement_id": f"runtime-vector-noise-{index}",
                    "generation": 1,
                    "response": f"Unrelated response {index}.",
                    "query_identity": extract_standalone_identity(request, selected_scope),
                    "retrieval": retrieval_representation(request),
                    "tier": Tier.STATIC,
                    "lifecycle": LifecycleState.ACTIVE,
                    "scope": selected_scope,
                    "support_references": (),
                    "valid_from": "",
                    "valid_from_available": False,
                    "valid_until": "",
                    "valid_until_available": False,
                    "superseded_by": "",
                    "provenance": RUNTIME_BENCHMARK_PROVENANCE,
                    "statistics": INITIAL_ARTIFACT_STATISTICS,
                    "metadata": {},
                }
            )
        )
    for index in range(support_fanout):
        request = f"supported-artifact-token-{index}"
        artifacts.append(
            validate_cached_response_artifact(
                {
                    "statement_id": f"runtime-vector-supported-{index}",
                    "generation": 1,
                    "response": f"Supported response {index}.",
                    "query_identity": extract_standalone_identity(request, selected_scope),
                    "retrieval": retrieval_representation(request),
                    "tier": Tier.STATIC,
                    "lifecycle": LifecycleState.ACTIVE,
                    "scope": selected_scope,
                    "support_references": (support_reference,),
                    "valid_from": "",
                    "valid_from_available": False,
                    "valid_until": "",
                    "valid_until_available": False,
                    "superseded_by": "",
                    "provenance": RUNTIME_BENCHMARK_PROVENANCE,
                    "statistics": INITIAL_ARTIFACT_STATISTICS,
                    "metadata": {},
                }
            )
        )
    engram.response_repository = ArtifactRepository(tuple(artifacts))
    graph_settings = engram.config.get("graph", {})
    graph_settings["enabled"] = True
    graph_settings["vector_enabled"] = True
    engram.internal_graph_client = SyntheticVectorGraph(proposition_id)
    engram.graph_embedding_model = SyntheticEmbeddingModel()
    engram.encode_graph_query = lambda text: [0.0] * 384
    core = EngramCore(engram)
    # The working list is not retained by the core; release it before reading current bytes.
    del artifacts
    current_bytes, peak_bytes = tracemalloc_get_traced_memory()
    tracemalloc_stop()
    memory = {"current_bytes": current_bytes, "peak_bytes": peak_bytes}
    sequence = [0]
    candidate_counts = []

    def propose() -> dict:
        request_id = f"vector-{corpus_size}-{support_fanout}-{sequence[0]}"
        sequence[0] += 1
        proposal = core.propose(
            "Semantic probe with orthogonal vocabulary",
            request_id=request_id,
            namespace="benchmark",
            context_fingerprint="runtime",
            limit=10,
        )
        candidate_counts.append(len(proposal.get("candidates", [])))
        return proposal

    latency = internal_measure(propose, iterations)
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


def startup_result(iterations: int) -> dict:
    warm_iterations = max(5, iterations)
    cold_iterations = max(3, min(5, iterations // 5))
    # Warm construction uses the 100-artifact benchmark profile (capacity 110).
    warm = internal_measure(lambda: Engram(config=engram_config(capacity=110, **BENCHMARK_CONFIG_OPTIONS)), warm_iterations)
    command = [sys_executable, "-c", "from engram.core import Engram; Engram()"]

    def cold_start():
        result = subprocess_run(command, cwd=REPOSITORY, check=True, capture_output=True, text=True)
        return result

    cold = internal_measure(cold_start, cold_iterations)
    result = {
        "warm_process_core_construction": warm,
        "cold_process_import_and_construction": cold,
        "mode": "offline graph-disabled startup",
    }
    return result


def package_versions() -> dict[str, str]:
    versions = {}
    for package in PACKAGE_NAMES:
        try:
            versions[package] = metadata.version(package)
        except metadata.PackageNotFoundError:
            versions[package] = ""
    return versions


def reported(label: str, operation: object):
    """Report long setup stages without including them in latency samples."""
    if not callable(operation):
        raise ValueError("reported benchmark operation must be callable")
    print(f"[runtime] starting {label}", file=sys_stderr, flush=True)
    started = time_perf_counter()
    result = operation()
    elapsed = time_perf_counter() - started
    print(f"[runtime] completed {label} in {elapsed:.3f}s", file=sys_stderr, flush=True)
    return result


def run_benchmark(sizes: list[int], fanouts: list[int], iterations: int) -> dict:
    artifact_lookup = [
        reported(
            f"artifact lookup corpus_size={size}",
            lambda size=size: artifact_result(size, iterations),
        )
        for size in sizes
    ]
    vector = [
        reported(
            f"support-vector corpus_size={size} fanout={fanout}",
            lambda size=size, fanout=fanout: vector_result(size, fanout, iterations),
        )
        for size in sizes
        for fanout in fanouts
        if fanout <= size
    ]
    result = {
        "captured_at": datetime.now(UTC).isoformat(),
        "source": benchmark_source_state(),
        "environment": {
            "python": platform_python_version(),
            "platform": platform_platform(),
            "processor": platform_processor(),
            "logical_cpu_count": os_cpu_count() or 0,
            "packages": package_versions(),
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
        "startup": startup_result(iterations),
        "artifact_lookup": artifact_lookup,
        "support_vector": vector,
    }
    return result


def main(argv: tuple[str, ...] = ()) -> int:
    parser = argparse_ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--sizes", type=int, nargs="+", default=[100, 1000, 5000])
    parser.add_argument("--fanouts", type=int, nargs="+", default=[1, 10, 100])
    parser.add_argument("--iterations", type=int, default=30)
    args = parser.parse_args(argv)
    if args.iterations < 5:
        parser.error("iterations must be at least 5")
    if any(size < 1 for size in args.sizes):
        parser.error("corpus sizes must be positive")
    if any(fanout < 1 for fanout in args.fanouts):
        parser.error("support fanouts must be positive")
    result = run_benchmark(sorted(set(args.sizes)), sorted(set(args.fanouts)), args.iterations)
    try:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json_dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    except OSError as err:
        parser.error(f"cannot write benchmark output: {err}")
    print(args.output)
    result = 0
    return result


if __name__ == "__main__":
    raise SystemExit(main(tuple(sys_argv[1:])))
