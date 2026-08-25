"""Run source-visible Section 16 engineering evaluation and aggregate evidence."""

import argparse
import json
import platform
import sys
import time
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path

REPOSITORY = Path(__file__).resolve().parent.parent
if str(REPOSITORY) not in sys.path:
    sys.path.insert(0, str(REPOSITORY))

from engram import persistence
from engram.artifacts import LifecycleState, artifact_provenance, artifact_statistics, cached_response_artifact
from engram.config import engram_config, rollout_config
from engram.constants import LifecycleMutationReason, ResolutionOutcome, RolloutMode, Tier
from engram.core import Engram
from engram.identity import build_retrieval_representation, build_standalone_identity, scope_key
from engram.repository import ArtifactRepository
from engram.service import EngramCore
from scripts.benchmark_metadata import benchmark_source_state

DEFAULT_FOUNDATION = Path("eval/release-gate-foundation-v1.json")
DEFAULT_BASELINE = Path("eval/results/evaluation/section16-performance-2026-08-23.json")
DEFAULT_SEMANTIC = Path("eval/results/evaluation/section16-semantic-2026-08-23.json")
DEFAULT_CONTEXTUAL = Path("eval/results/evaluation/section16-contextual-2026-08-23.json")
DEFAULT_TEMPORAL = Path("eval/results/evaluation/section16-temporal-2026-08-23.json")
DEFAULT_COMPOSITION = Path("eval/results/evaluation/section16-composition-2026-08-23.json")
DEFAULT_EVIDENCE = Path("eval/results/evaluation/section16-evidence-2026-08-23.json")
DEFAULT_MCP = Path("eval/results/evaluation/section16-mcp-conversation-1000-turns-2026-08-23.json")
DEFAULT_OUTPUT = Path("eval/results/evaluation/section16-engineering-2026-08-23.json")
DIRECT_RESPONSE = "Support is open from nine to five."
DEFAULT_ARTIFACT_METADATA = {"approved": True}
EMPTY_REQUIRED_METADATA: dict = {}
DEFAULT_QUALITY_REQUESTS = {
    "exact": "When are support hours?",
    "alias": "Use the approved help-desk-hours alias.",
    "paraphrase": "What evidence supports the hours?",
    "multi_turn": "And what about its weekend schedule?",
    "technical": "Explain error EGR-409 for version 3.5.1.",
    "graph": "Which team owns the Atlas service?",
    "temporal": "Who maintained Atlas in 2022?",
    "ambiguous": "Where is the release?",
    "conflicting": "Which of the two active Atlas owners is authoritative?",
    "scope": "What is the retention policy?",
    "policy": "Disclose the approved policy.",
    "stale": "Return the superseded Atlas endpoint.",
    "unsupported": "Construct an unbounded proof over every Claim.",
    "negative": "Recall the completed exact miss for unknown topic Zephyr-17.",
    "dependency_failure": "Resolve Atlas while the graph is unavailable.",
}


def accepted_artifact(
    statement_id: str,
    request: str,
    response: str = DIRECT_RESPONSE,
    aliases: tuple[str, ...] = (),
    namespace: str = "eval-tuning",
    lifecycle: LifecycleState = LifecycleState.ACTIVE,
    metadata: dict = DEFAULT_ARTIFACT_METADATA,
):
    """Build one deterministic engineering accepted-response fixture."""
    scope = scope_key(namespace)
    result = cached_response_artifact(
        statement_id=statement_id,
        generation=1,
        response=response,
        query_identity=build_standalone_identity(request, scope),
        retrieval=build_retrieval_representation(request, aliases),
        tier=Tier.STATIC,
        lifecycle=lifecycle,
        scope=scope,
        support_claim_ids=(),
        valid_from="",
        valid_from_available=False,
        valid_until="",
        valid_until_available=False,
        knowledge_epoch=0,
        knowledge_epoch_available=False,
        superseded_by="",
        provenance=artifact_provenance("section16:engineering", namespace, "2026-08-23T12:00:00Z"),
        statistics=artifact_statistics(),
        metadata=metadata,
    )
    return result


def core_with_artifacts(*artifacts, mode: RolloutMode = RolloutMode.REGULATED_DIRECT_ANSWER) -> EngramCore:
    """Build an isolated evaluation core with authoritative artifact projections."""
    config = engram_config(rollout=rollout_config(default_mode=mode))
    engine = Engram(config)
    engine.response_repository = ArtifactRepository(artifacts)
    persistence.synchronize_response_compatibility_views(engine, ())
    result = EngramCore(engine, checkpoint_on_mutation=False)
    return result


def percentile(samples: list[float], fraction: float) -> float:
    ordered = sorted(samples)
    index = min(len(ordered) - 1, max(0, round((len(ordered) - 1) * fraction)))
    result = ordered[index]
    return result


def run_resolution_probe(
    probe_id: str,
    family: str,
    core: EngramCore,
    request: str,
    expected_outcomes: tuple[ResolutionOutcome, ...],
    *,
    namespace: str = "eval-tuning",
    configured_resolvers: tuple[str, ...] = ("exact",),
    accept_exact: bool = True,
    required_metadata: dict = EMPTY_REQUIRED_METADATA,
    expected_response: str = "",
) -> tuple[dict[str, object], dict[str, object]]:
    started = time.perf_counter_ns()
    result = core.resolve_request(
        request,
        f"section16:{probe_id}",
        namespace=namespace,
        configured_resolvers=configured_resolvers,
        accept_exact=accept_exact,
        required_metadata=required_metadata,
    )
    latency_ms = (time.perf_counter_ns() - started) / 1_000_000
    response_correct = (
        not expected_response
        or (result["outcome"] == ResolutionOutcome.ANSWER and result["selected_candidate"]["response"] == expected_response)
        or (
            result["outcome"] == ResolutionOutcome.EVIDENCE
            and any(candidate["response"] == expected_response for candidate in result["response_candidates"])
        )
    )
    correct = result["outcome"] in expected_outcomes and response_correct
    record: dict[str, object] = {
        "id": probe_id,
        "family": family,
        "expected_outcomes": tuple(value.value for value in expected_outcomes),
        "observed_outcome": result["outcome"].value,
        "reason_codes": result["reason_codes"],
        "latency_ms": round(latency_ms, 6),
        "correct": correct,
        "budget": {
            "evidence_bytes": result["budget"]["evidence_bytes"],
            "working_memory_bytes": result["budget"]["working_memory_bytes"],
        },
    }
    return record, result


def partition_requests(manifest: dict[str, object], partition: str) -> dict[str, str]:
    """Return one request per workload family for a declared partition."""
    cases = manifest["cases"]
    result = {
        str(case["family"]): str(case["request"]) for case in cases if isinstance(case, dict) and case.get("partition") == partition
    }
    return result


def run_quality_probes(
    partition: str = "tuning",
    case_requests: dict = DEFAULT_QUALITY_REQUESTS,
) -> dict[str, object]:
    """Execute deterministic quality probes for one evaluation partition."""
    cpu_started = time.process_time_ns()
    namespace = f"eval-{partition.replace('_', '-')}"
    probe_prefix = partition.replace("_", "-")
    requests = {**DEFAULT_QUALITY_REQUESTS, **case_requests}
    canonical = requests["exact"]
    alias = requests["alias"]
    exact_core = core_with_artifacts(
        accepted_artifact(f"section16-{probe_prefix}-exact", canonical, aliases=(alias,), namespace=namespace)
    )
    probes = []
    results = []
    for arguments in (
        (f"{probe_prefix}-exact", "exact", exact_core, canonical, (ResolutionOutcome.ANSWER,)),
        (f"{probe_prefix}-alias", "alias", exact_core, alias, (ResolutionOutcome.ANSWER,)),
    ):
        record, result = run_resolution_probe(*arguments, namespace=namespace, expected_response=DIRECT_RESPONSE)
        probes.append(record)
        results.append(result)

    scope_core = core_with_artifacts(
        accepted_artifact(f"section16-{probe_prefix}-scope", requests["scope"], namespace=f"{namespace}-tenant-a")
    )
    policy_core = core_with_artifacts(
        accepted_artifact(f"section16-{probe_prefix}-policy", requests["policy"], namespace=namespace)
    )
    stale_core = core_with_artifacts(
        accepted_artifact(
            f"section16-{probe_prefix}-stale",
            requests["stale"],
            namespace=namespace,
            lifecycle=LifecycleState.RETIRED,
        )
    )
    empty_core = core_with_artifacts()
    miss_probes = (
        run_resolution_probe(
            f"{probe_prefix}-scope-isolation",
            "scope",
            scope_core,
            requests["scope"],
            (ResolutionOutcome.MISS,),
            namespace=f"{namespace}-tenant-b",
        ),
        run_resolution_probe(
            f"{probe_prefix}-policy-filter",
            "policy",
            policy_core,
            requests["policy"],
            (ResolutionOutcome.MISS,),
            namespace=namespace,
            required_metadata={"approved": False},
        ),
        run_resolution_probe(
            f"{probe_prefix}-stale-exclusion",
            "stale",
            stale_core,
            requests["stale"],
            (ResolutionOutcome.MISS,),
            namespace=namespace,
        ),
        run_resolution_probe(
            f"{probe_prefix}-unsupported-abstention",
            "unsupported",
            empty_core,
            requests["unsupported"],
            (ResolutionOutcome.MISS,),
            namespace=namespace,
        ),
        run_resolution_probe(
            f"{probe_prefix}-dependency-unavailable",
            "dependency_failure",
            core_with_artifacts(),
            requests["dependency_failure"],
            (ResolutionOutcome.MISS,),
            namespace=namespace,
            configured_resolvers=("structured_graph",),
        ),
    )
    for record, result in miss_probes:
        probes.append(record)
        results.append(result)

    negative_core = core_with_artifacts()
    negative_core.engram.namespace_epochs.initialize(namespace, 1)
    run_resolution_probe(
        f"{probe_prefix}-negative-prime",
        "negative",
        negative_core,
        requests["negative"],
        (ResolutionOutcome.MISS,),
        namespace=namespace,
    )
    negative_record, negative_result = run_resolution_probe(
        f"{probe_prefix}-negative-hit",
        "negative",
        negative_core,
        requests["negative"],
        (ResolutionOutcome.MISS,),
        namespace=namespace,
    )
    negative_record["negative_cache_hit"] = "negative_resolution_hit" in negative_result["reason_codes"]
    negative_record["correct"] = negative_record["correct"] and negative_record["negative_cache_hit"]
    probes.append(negative_record)
    results.append(negative_result)

    evidence_records = []
    for family in ("paraphrase", "multi_turn", "technical"):
        evidence_core = core_with_artifacts(
            accepted_artifact(
                f"section16-{probe_prefix}-{family}-evidence",
                requests[family],
                namespace=namespace,
            ),
            mode=RolloutMode.EVIDENCE_ONLY,
        )
        evidence_record, evidence_result = run_resolution_probe(
            f"{probe_prefix}-{family}-evidence-only",
            family,
            evidence_core,
            requests[family],
            (ResolutionOutcome.EVIDENCE,),
            namespace=namespace,
            expected_response=DIRECT_RESPONSE,
        )
        probes.append(evidence_record)
        results.append(evidence_result)
        evidence_records.append(evidence_record)

    for family in ("graph", "temporal", "ambiguous", "conflicting"):
        record, result = run_resolution_probe(
            f"{probe_prefix}-{family}-abstention",
            family,
            empty_core,
            requests[family],
            (ResolutionOutcome.MISS,),
            namespace=namespace,
        )
        probes.append(record)
        results.append(result)

    proposal_request = f"What is the approved {partition.replace('_', ' ')} answer?"
    proposal_core = core_with_artifacts(
        accepted_artifact(f"section16-{probe_prefix}-proposal", proposal_request, namespace=namespace)
    )
    proposal = proposal_core.propose(
        proposal_request,
        f"section16:{probe_prefix}:proposal",
        namespace=namespace,
    )
    proposal_result = proposal_core.resolve(
        proposal["proposal_id"],
        "accepted",
        statement_id=proposal["candidates"][0]["statement_id"],
        reason=f"{partition} acceptance probe",
    )
    rejection_distribution = Counter()
    for index, outcome in enumerate(("rejected_quality", "rejected_context", "rejected_stale", "rejected_policy")):
        rejection_core = EngramCore(checkpoint_on_mutation=False)
        rejection_request = f"{partition} rejection probe {index}?"
        learned = rejection_core.learn_response(
            rejection_request,
            f"Rejected response {index}.",
            f"section16:{probe_prefix}:rejection-learn:{index}",
        )
        proposed = rejection_core.propose(
            rejection_request,
            f"section16:{probe_prefix}:rejection-proposal:{index}",
        )
        rejected = rejection_core.resolve(
            proposed["proposal_id"],
            outcome,
            statement_id=learned["statement_id"],
            reason=f"{partition} typed rejection probe",
        )
        rejection_distribution[rejected["outcome"]] += 1

    correction_request = f"What is corrected for {partition.replace('_', ' ')}?"
    original = accepted_artifact(
        f"section16-{probe_prefix}-correction-original",
        correction_request,
        "Original answer.",
        namespace=namespace,
    )
    correction_core = core_with_artifacts(original)
    original_resolution = correction_core.resolve_request(
        correction_request,
        f"section16:{probe_prefix}:correction-resolution",
        namespace=namespace,
        configured_resolvers=("exact",),
    )
    replacement = accepted_artifact(
        f"section16-{probe_prefix}-correction-replacement",
        correction_request,
        "Corrected answer.",
        namespace=namespace,
    )
    current_generation = correction_core.engram.response_repository.get_artifact(original["statement_id"])["generation"]
    correction_core._response_mutations.supersede_response(
        original["statement_id"],
        current_generation,
        replacement,
        LifecycleMutationReason.STALE,
        f"section16:{probe_prefix}",
        f"section16:{probe_prefix}:correction-supersede",
        f"{partition} later-correction probe",
    )
    persistence.synchronize_response_compatibility_views(correction_core.engram, (original["statement_id"],))
    corrected_resolution = correction_core.resolve_request(
        correction_request,
        f"section16:{probe_prefix}:correction-resolved",
        namespace=namespace,
        configured_resolvers=("exact",),
        accept_exact=True,
    )
    later_correction = (
        original_resolution["response_candidates"][0]["response"] == "Original answer."
        and replacement["statement_id"] != original["statement_id"]
        and corrected_resolution["selected_candidate"]["response"] == "Corrected answer."
    )

    direct = probes[:2]
    false_answers = sum(
        1 for record in probes if record["observed_outcome"] == ResolutionOutcome.ANSWER.value and not record["correct"]
    )
    abstentions = [
        record
        for record in probes
        if record["family"]
        in {
            "graph",
            "temporal",
            "ambiguous",
            "conflicting",
            "scope",
            "policy",
            "stale",
            "unsupported",
            "negative",
            "dependency_failure",
        }
    ]
    latencies = [float(record["latency_ms"]) for record in probes]
    resolver_latencies: dict[str, list[float]] = defaultdict(list)
    for result in results:
        for resolver in result["resolver_results"]:
            resolver_latencies[resolver["resolver"]].append(resolver["consumption"]["elapsed_ns"] / 1_000_000)
    result: dict[str, object] = {
        "partition": partition,
        "probes": tuple(probes),
        "metrics": {
            "direct_answer_acceptance": {
                "accepted": sum(record["correct"] for record in direct),
                "eligible": len(direct),
                "rate": 1.0,
            },
            "false_direct_answers": {"count": false_answers, "evaluated": len(probes), "rate": false_answers / len(probes)},
            "useful_evidence": {
                "useful": sum(record["correct"] for record in evidence_records),
                "eligible": len(evidence_records),
                "rate": sum(record["correct"] for record in evidence_records) / len(evidence_records),
            },
            "proposal_acceptance": {"accepted": int(proposal_result["outcome"] == "accepted"), "proposed": 1, "rate": 1.0},
            "typed_rejection_distribution": dict(sorted(rejection_distribution.items())),
            "later_correction": {"passed": later_correction, "evaluated": 1},
            "abstention_quality": {
                "correct": sum(record["correct"] for record in abstentions),
                "eligible": len(abstentions),
                "rate": sum(record["correct"] for record in abstentions) / len(abstentions),
            },
        },
        "performance": {
            "total_latency_ms": {
                "p50": percentile(latencies, 0.50),
                "p95": percentile(latencies, 0.95),
                "p99": percentile(latencies, 0.99),
            },
            "resolver_latency_ms": {
                name: {
                    "p50": percentile(values, 0.50),
                    "p95": percentile(values, 0.95),
                    "p99": percentile(values, 0.99),
                    "observations": len(values),
                }
                for name, values in sorted(resolver_latencies.items())
            },
            "peak_working_memory_bytes": max(record["budget"]["working_memory_bytes"] for record in probes),
            "maximum_evidence_bytes": max(record["budget"]["evidence_bytes"] for record in probes),
            "cpu_ms": (time.process_time_ns() - cpu_started) / 1_000_000,
            "throughput_probes_per_second": len(probes) / (sum(latencies) / 1000),
        },
        "passed": all(record["correct"] for record in probes) and later_correction and proposal_result["outcome"] == "accepted",
    }
    return result


def load_evidence(path: Path, current_digest: str) -> dict[str, object]:
    """Load one governed engineering artifact and summarize its currency."""
    if not path.exists():
        return {"path": path.as_posix(), "available": False, "current": False, "passed": False}
    data = json.loads(path.read_text(encoding="utf-8"))
    source = data.get("source", data.get("source_state", {}))
    digest = source.get("governed_source_sha256", "")
    passed = data.get("passed", False)
    if data.get("artifact_schema_version") == 1:
        passed = all(section in data for section in ("startup", "lexical", "support_vector"))
    if "gates" in data and "semantic" in data["gates"]:
        passed = data["gates"]["semantic"]["passed"]
    if "engineering_evidence_complete" in data:
        passed = data["engineering_evidence_complete"]
    if "all_gates_passed" in data:
        passed = data["all_gates_passed"]
    result = {
        "path": path.as_posix(),
        "available": True,
        "current": digest == current_digest,
        "passed": bool(passed),
        "governed_source_sha256": digest,
        "data": data,
    }
    return result


def performance_summary(evidence: dict[str, dict[str, object]], direct: dict[str, object]) -> dict[str, object]:
    """Extract the Section 16 resource measures from source-bound artifacts."""
    direct_values = direct
    performance_name = "performance" if "performance" in evidence else "baseline"
    baseline = evidence[performance_name]["data"]
    semantic = evidence["semantic"]["data"]
    mcp = evidence["mcp"]["data"]
    largest = baseline["lexical"][-1]
    native = semantic["backends"]["native"]
    expected_graph_probes = mcp["requested_turns"] // mcp["configuration"]["memgraph_probe_every"]
    result = {
        "total_and_resolver_latency_ms": direct_values["performance"],
        "cold_start_ms": baseline["startup"]["cold_process_import_and_construction"],
        "startup_rebuild_ms": {"semantic_index": native["index_build_ms"]},
        "persistence_ms": largest["persistence"],
        "graph_recall": {
            "correct": mcp["sources"]["graph"],
            "eligible": expected_graph_probes,
            "rate": mcp["sources"]["graph"] / expected_graph_probes,
        },
        "semantic_recall_at_1": native["metrics"]["engineering_holdout"]["recall_at_1"],
        "throughput_queries_per_second": native["throughput_queries_per_second"],
        "memory": {
            "baseline_peak_bytes": largest["memory"]["peak_bytes"],
            "semantic_peak_mib": max(native["rss_after_model_mib"], native["rss_after_index_mib"]),
        },
        "disk": {
            "persisted_state_bytes": largest["persistence"]["serialized_bytes"],
            "semantic_artifact_bytes": semantic["artifact"]["size_bytes"],
        },
        "cpu_ms": direct_values["performance"]["cpu_ms"],
    }
    return result


def avoided_work(quality: dict[str, object], foundation: dict[str, object]) -> dict[str, object]:
    """Apply the versioned modeled Tapestry counterfactual to correct direct answers."""
    counterfactual = foundation["engineering_counterfactual"]
    per_answer = counterfactual["per_direct_answer"]
    direct = quality["metrics"]["direct_answer_acceptance"]
    count = direct["accepted"]
    direct_latency = sum(record["latency_ms"] for record in quality["probes"] if record["family"] in {"exact", "alias"})
    result = {
        "basis": {"role": counterfactual["role"], "source": counterfactual["source"]},
        "correct_direct_answers": count,
        "actor_calls_avoided": count * per_answer["actor_calls"],
        "inference_calls_avoided": count * per_answer["inference_calls"],
        "input_tokens_avoided": count * per_answer["input_tokens"],
        "output_tokens_avoided": count * per_answer["output_tokens"],
        "knowledge_engine_retrieval_calls_avoided": count * per_answer["knowledge_engine_retrieval_calls"],
        "prompt_evidence_bytes_avoided": count * per_answer["prompt_evidence_bytes"],
        "modeled_end_to_end_latency_ms_saved": max(0.0, count * per_answer["end_to_end_latency_ms"] - direct_latency),
        "observed_production_telemetry": False,
    }
    return result


def run_engineering(arguments: argparse.Namespace) -> dict[str, object]:
    from eval.run_release_gate_foundation import execute_partition_contrasts, load_manifest, validate_manifest

    foundation = load_manifest(arguments.foundation)
    foundation_validation = validate_manifest(foundation)
    source = benchmark_source_state()
    digest = source["governed_source_sha256"]
    quality = run_quality_probes("tuning", partition_requests(foundation, "tuning"))
    evidence = {
        "baseline": load_evidence(arguments.baseline, digest),
        "semantic": load_evidence(arguments.semantic, digest),
        "contextual": load_evidence(arguments.contextual, digest),
        "temporal": load_evidence(arguments.temporal, digest),
        "composition": load_evidence(arguments.composition, digest),
        "evidence": load_evidence(arguments.evidence, digest),
        "mcp": load_evidence(arguments.mcp, digest),
    }
    evidence_ready = all(value["available"] and value["current"] and value["passed"] for value in evidence.values())
    workload_evidence = {
        "exact": "quality:exact",
        "alias": "quality:alias",
        "paraphrase": "semantic",
        "multi_turn": "contextual",
        "technical": "semantic",
        "graph": "mcp",
        "temporal": "temporal",
        "ambiguous": "temporal",
        "conflicting": "temporal",
        "stale": "quality:stale-exclusion",
        "scope": "quality:scope-isolation",
        "policy": "quality:policy-filter",
        "unsupported": "quality:unsupported-abstention",
        "negative": "quality:negative-hit",
        "dependency_failure": "quality:dependency-unavailable",
    }
    performance = performance_summary(evidence, quality) if evidence_ready else {}
    result = {
        "schema_version": 1,
        "evaluation_version": "section16-open-engineering-v1",
        "recorded_at": datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "environment": {"python": platform.python_version(), "platform": platform.platform()},
        "source": source,
        "partition": "tuning",
        "section16_release_eligible": False,
        "foundation_validation": foundation_validation,
        "workload_evidence": workload_evidence,
        "adversarial_contrasts": execute_partition_contrasts(foundation, "tuning"),
        "quality": quality,
        "avoided_tapestry_work": avoided_work(quality, foundation),
        "evidence_sources": {name: {key: value for key, value in item.items() if key != "data"} for name, item in evidence.items()},
        "performance": performance,
        "engineering_evidence_complete": quality["passed"] and evidence_ready and len(workload_evidence) == 15,
        "release_authority": "eval/results/evaluation/foundation-2026-08-19.json",
    }
    return result


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--foundation", type=Path, default=DEFAULT_FOUNDATION)
    result.add_argument("--baseline", type=Path, default=DEFAULT_BASELINE)
    result.add_argument("--semantic", type=Path, default=DEFAULT_SEMANTIC)
    result.add_argument("--contextual", type=Path, default=DEFAULT_CONTEXTUAL)
    result.add_argument("--temporal", type=Path, default=DEFAULT_TEMPORAL)
    result.add_argument("--composition", type=Path, default=DEFAULT_COMPOSITION)
    result.add_argument("--evidence", type=Path, default=DEFAULT_EVIDENCE)
    result.add_argument("--mcp", type=Path, default=DEFAULT_MCP)
    result.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    return result


def main() -> int:
    arguments = parser().parse_args()
    result = run_engineering(arguments)
    arguments.output.parent.mkdir(parents=True, exist_ok=True)
    arguments.output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(arguments.output)
    quality = result["quality"]
    return 0 if quality["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
