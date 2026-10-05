"""Measure how request and mutation latency grow with the size of the store.

Each scenario runs at several store sizes so growth is visible, and the largest
size is compared with a latency budget. ``--check`` exits non-zero when a
budget is exceeded. See R1 in REMEDIATION.md.
"""

from argparse import ArgumentParser as argparse_ArgumentParser
from datetime import UTC, datetime
from gc import collect as gc_collect, freeze as gc_freeze, unfreeze as gc_unfreeze
from itertools import count as itertools_count
from json import dumps as json_dumps
from pathlib import Path
from sys import argv as sys_argv, path as sys_path, stderr as sys_stderr
from time import perf_counter_ns as time_perf_counter_ns

REPOSITORY = Path(__file__).resolve().parents[1]
if str(REPOSITORY) not in sys_path:
    sys_path.insert(0, str(REPOSITORY))

from engram.artifacts import LifecycleState, validate_cached_response_artifact
from engram.config import engram_config, sparse_config
from engram.constants import INITIAL_ARTIFACT_STATISTICS, UNCONSTRAINED_ASSERTION_BASIS, PropositionProjectionQuery, Tier
from engram.core import Engram
from engram.graph import proposition_projection, proposition_projection_from_graph_row
from engram.identity import extract_standalone_identity, retrieval_representation, scope_key
from engram.mutations import (
    MutationOperation,
    MutationResultCode,
    ReceiptCompletionState,
    canonical_payload_signature,
    mutation_receipt,
)
from engram.repository import ArtifactRepository
from engram.resolution import QueryFrameBuilder, capture_resolution_budget
from engram.resolvers import StructuredGraphResolver, resolver_budget
from engram.service import EngramCore
from scripts.benchmark_metadata import benchmark_source_state
from scripts.benchmark_runtime import internal_measure, package_versions, reported

DEFAULT_OUTPUT = Path("eval/results/scale/benchmark-scale.json")
TARGET_REQUEST = "What are the baseline support hours?"
SPARSE_REQUEST = "baseline support hours schedule"
START_NS = 1_000_000

# Target p95 latency in milliseconds at the largest measured size.
BUDGETS_MS = {
    "resolve_exact": 50.0,
    "resolve_sparse": 100.0,
    "learn_response": 50.0,
    "propose": 50.0,
    "accept": 50.0,
    "retire_response": 50.0,
    "pattern_retire": 20.0,
    "chat_learn_at_capacity": 100.0,
    "evidence_trim": 100.0,
}
# Every scale engine disables polishing and lexical expansion so that only the measured
# store work is timed; capacity, sparse retrieval and fact learning are set per engine.
SCALE_CONFIG_OPTIONS = {
    "polish_responses": False,
    "use_lemmatization": False,
    "use_spell_correction": False,
    "use_stemming": False,
    "use_synonyms": False,
}
# Read-only provenance shared by every synthetic accepted response; artifact validation copies it.
SCALE_ARTIFACT_PROVENANCE = {"source_label": "scale:benchmark", "caller_id": "engineering", "accepted_at": "2026-09-27T00:00:00Z"}


RECEIPTS = [10_000]


def seed_receipts(core: EngramCore, count: int) -> None:
    """Fill the mutation ledger with prior receipts, as a long-running process would have."""
    ledger = core.engram.mutation_receipts
    for index in range(count):
        ledger.record(
            mutation_receipt(
                sequence=ledger.next_sequence,
                request_id=f"seeded-{index}",
                operation=MutationOperation.RECORD_RESPONSE_QUERY,
                payload_signature=canonical_payload_signature({"seeded": index}),
                result_code=MutationResultCode.QUERY_RECORDED,
                affected_generations=(),
                result={"statement_ids": [], "recorded_at": "2026-09-27T00:00:00Z"},
                completion_state=ReceiptCompletionState.COMPLETED,
                created_at="2026-09-27T00:00:00Z",
            )
        )


FREEZE_GC = [True]


def warmed_measure(operation: object, iterations: int) -> dict:
    """Run ``operation`` once untimed, so first-use loading is not counted, then time it.

    Like the gRPC server after startup, long-lived setup objects are frozen out
    of garbage collection for the timed iterations unless ``--no-gc-freeze`` is
    given. The benchmark owns the process's collector state, so the freeze is
    released after timing: a scenario's setup stays collectable once a later
    scenario replaces it, and every measurement starts from the same state.
    """
    if not callable(operation):
        raise ValueError("benchmark operation must be callable")
    operation()
    if not FREEZE_GC[0]:
        result = internal_measure(operation, iterations)
        return result
    gc_collect()
    gc_freeze()
    try:
        result = internal_measure(operation, iterations)
    finally:
        gc_unfreeze()
    return result


def measure_prepared(prepare: object, operation: object, iterations: int) -> dict:
    """Time ``operation(prepared)`` for values ``prepare`` builds outside the timing."""
    if not callable(prepare) or not callable(operation):
        raise ValueError("benchmark prepare and operation must be callable")
    prepared = [prepare() for _ in range(iterations + 1)]
    values = iter(prepared)
    result = warmed_measure(lambda: operation(next(values)), iterations)
    return result


def sparse_outcome(core: EngramCore) -> dict:
    """Check that sparse retrieval still finds the target, so a fast failure cannot pass as fast."""
    discovery = core.engram.sparse_candidates(SPARSE_REQUEST, scope_key(), limit=5, max_working_memory_bytes=16 * 1024 * 1024)
    matches = [match.get("statement_id", "") for match in discovery.get("matches", ())]
    result = {
        "complete": discovery.get("complete", False),
        "reason": discovery.get("reason", ""),
        "found_target": "scale-target" in matches,
    }
    return result


def artifact_scenarios(size: int, iterations: int) -> dict:
    # Two cores hold the same size accepted responses (size - 1 unrelated plus the target),
    # one with sparse retrieval enabled; each ledger is seeded like a long-running process's.
    cores = []
    for sparse_enabled in (False, True):
        engram = Engram(
            config=engram_config(
                capacity=max(size * 2, 100),
                learn_user_facts=False,
                sparse=sparse_config(enabled=sparse_enabled),
                **SCALE_CONFIG_OPTIONS,
            )
        )
        selected_scope = scope_key()
        artifacts = []
        entries = [
            (f"scale-noise-{index}", f"archive entry {index} noise token {index}", f"Synthetic response {index}.")
            for index in range(max(0, size - 1))
        ]
        entries.append(("scale-target", TARGET_REQUEST, "Baseline support is open from nine to five."))
        for statement_id, request, response in entries:
            artifacts.append(
                validate_cached_response_artifact(
                    {
                        "statement_id": statement_id,
                        "generation": 1,
                        "response": response,
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
                        "provenance": SCALE_ARTIFACT_PROVENANCE,
                        "statistics": INITIAL_ARTIFACT_STATISTICS,
                        "metadata": {},
                    }
                )
            )
        engram.response_repository = ArtifactRepository(tuple(artifacts))
        seeded_core = EngramCore(engram)
        seed_receipts(seeded_core, RECEIPTS[0])
        cores.append(seeded_core)
    core, sparse_core = cores
    sequence = itertools_count(1)

    def learn() -> dict:
        number = next(sequence)
        result = core.learn_response(f"scale learned question {number}", f"Scale learned answer {number}.", f"learn-{number}")
        return result

    def accept(proposal: dict) -> dict:
        candidates = proposal.get("candidates", [])
        result = core.resolve(proposal.get("proposal_id", ""), "accepted", candidates[0].get("statement_id", ""), "benchmark")
        return result

    result = {
        "resolve_exact": warmed_measure(
            lambda: core.resolve_request(TARGET_REQUEST, f"exact-{next(sequence)}", configured_resolvers=("exact",)),
            iterations,
        ),
        "resolve_sparse": warmed_measure(
            lambda: sparse_core.resolve_request(SPARSE_REQUEST, f"sparse-{next(sequence)}", configured_resolvers=("sparse",)),
            iterations,
        ),
        "learn_response": warmed_measure(learn, iterations),
        "propose": warmed_measure(lambda: core.propose(TARGET_REQUEST, f"propose-{next(sequence)}"), iterations),
        "accept": measure_prepared(
            lambda: core.propose(TARGET_REQUEST, f"accept-proposal-{next(sequence)}"),
            accept,
            iterations,
        ),
        "sparse_outcome": sparse_outcome(sparse_core),
        "retire_response": measure_prepared(
            lambda: next(sequence),
            lambda number: core.retire_response(f"scale-noise-{number % max(1, size - 1)}", "benchmark", f"retire-{number}"),
            iterations,
        ),
    }
    return result


def pattern_scenarios(size: int, iterations: int) -> dict:
    # Two engines at capacity, each a catch-all plus size pattern statements: STATIC statements
    # to retire, and DYNAMIC ones with fact learning so a learning chat turn must evict.
    engines = []
    for tier, learn_user_facts in ((Tier.STATIC, False), (Tier.DYNAMIC, True)):
        engine = Engram(
            config=engram_config(
                capacity=size + 1,
                learn_user_facts=learn_user_facts,
                sparse=sparse_config(enabled=False),
                **SCALE_CONFIG_OPTIONS,
            )
        )
        engine.store("Go on.", pattern="*", tier=Tier.STATIC)
        stored_ids = [engine.store(f"Scale reply {index}.", pattern=f"SCALE PATTERN {index} *", tier=tier) for index in range(size)]
        engines.append((engine, stored_ids))
    (engram, statement_ids), (full, _) = engines
    victims = iter(statement_ids)
    sequence = itertools_count(1)
    result = {
        "pattern_retire": warmed_measure(lambda: engram.retire_statement(next(victims)), iterations),
        "chat_learn_at_capacity": warmed_measure(
            lambda: full.pattern_query(f"Zorp{next(sequence)} is blue.", context_id="scale-user"),
            iterations,
        ),
    }
    return result


class TrimEngram(Engram):
    """Engram whose structured graph reads return fixed synthetic projections."""

    def __init__(self, projections: list[dict]) -> None:
        super().__init__(
            config=engram_config(capacity=100, learn_user_facts=False, sparse=sparse_config(enabled=False), **SCALE_CONFIG_OPTIONS)
        )
        self.scale_projections = projections
        # Revalidation reads each Proposition back as its current by-id projection.
        self.scale_current = {}
        for projection in projections:
            current_values = dict(projection)
            current_values.update(
                projection_id=PropositionProjectionQuery.BY_ID,
                structured_match=0.0,
                structured_match_available=False,
                semantic_similarity=0.0,
                semantic_similarity_available=False,
                vector_index_id="",
                vector_index_id_available=False,
            )
            self.scale_current[projection.get("proposition_id", "")] = proposition_projection(**current_values)

    def structured_proposition_projections(
        self,
        text: str,
        *,
        row_limit: int = 10,
        cooperative_check=(),
        max_working_memory_bytes: int = 0,
        basis_window: dict = UNCONSTRAINED_ASSERTION_BASIS,
    ) -> list[dict]:
        del text, cooperative_check, max_working_memory_bytes, basis_window
        result = self.scale_projections[:row_limit]
        return result

    def current_proposition_projection(
        self, proposition_id: str, basis_window: dict = UNCONSTRAINED_ASSERTION_BASIS
    ) -> tuple[dict, ...]:
        del basis_window
        # Like the graph boundary, an unknown Proposition has no current projection.
        current = self.scale_current.get(proposition_id, {})
        result = (current,) if current else ()
        return result


def trim_scenario(records: int, iterations: int) -> dict:
    """Structured fallback with more eligible records than the evidence byte budget holds."""
    # Eligible, content-neutral structured-entity projections about one subject.
    projections = [
        proposition_projection_from_graph_row(
            {
                "proposition_id": f"scale-proposition-{index:06d}",
                "subject_entity_id": "entity:ada",
                "predicate_id": "predicate:built",
                "object_entity_id": f"entity:object-{index}",
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
                "trust_category": "",
                "trust_category_available": False,
                "supplied_trust": 0.0,
                "supplied_trust_available": False,
                "structured_match": 1.0,
                "structured_match_available": True,
                "semantic_similarity": 0.0,
                "semantic_similarity_available": False,
            },
            PropositionProjectionQuery.STRUCTURED_ENTITY,
        )
        for index in range(records)
    ]
    engine = TrimEngram(projections)
    frame = QueryFrameBuilder(engine, lambda: START_NS, lambda: datetime(2026, 9, 27, tzinfo=UTC)).build(
        "Ada",
        scope_key(),
        budget=capture_resolution_budget(lambda: START_NS),
    )
    budget = frame.get("budget", {})
    lease = resolver_budget(
        max_candidates=budget.get("max_candidates", 0),
        max_graph_rows=records * 2,
        max_vector_results=budget.get("max_vector_results", 0),
        max_evidence=records,
        max_evidence_bytes=2_500,
        max_output_bytes=budget.get("max_output_bytes", 0),
        max_diagnostic_bytes=budget.get("max_diagnostic_bytes", 0),
        max_working_memory_bytes=max(budget.get("max_working_memory_bytes", 0), records * 20_000),
    )
    resolver = StructuredGraphResolver(engine, lambda: START_NS)
    result = {"evidence_trim": warmed_measure(lambda: resolver.resolve(frame, lease), iterations)}
    return result


def budget_report(results: list[dict]) -> dict:
    """Compare the p95 at the largest size of each scenario with its budget."""
    report = {}
    for entry in results:
        scenarios = entry.get("scenarios", {})
        if "sparse_outcome" in scenarios:
            outcome = scenarios.get("sparse_outcome", {})
            report[f"sparse_found_target@{entry.get('size', 0)}"] = {
                "size": entry.get("size", 0),
                "p95_ms": 0.0,
                "budget_ms": 0.0,
                "within_budget": bool(outcome.get("complete", False) and outcome.get("found_target", False)),
            }
    for scenario, budget in BUDGETS_MS.items():
        measured = [
            (entry.get("size", 0), entry.get("scenarios", {}).get(scenario, {}))
            for entry in results
            if scenario in entry.get("scenarios", {})
        ]
        if not measured:
            continue
        size, latency = max(measured, key=lambda item: item[0])
        p95_ms = latency.get("p95_ms", 0.0)
        report[scenario] = {
            "size": size,
            "p95_ms": p95_ms,
            "budget_ms": budget,
            "within_budget": p95_ms <= budget,
        }
    return report


def run_benchmark(sizes: list[int], pattern_sizes: list[int], trim_sizes: list[int], iterations: int) -> dict:
    results = []
    for size in sizes:
        scenarios = reported(f"artifacts size={size}", lambda size=size: artifact_scenarios(size, iterations))
        results.append({"kind": "artifacts", "size": size, "scenarios": scenarios})
    for size in pattern_sizes:
        scenarios = reported(f"patterns size={size}", lambda size=size: pattern_scenarios(size, iterations))
        results.append({"kind": "patterns", "size": size, "scenarios": scenarios})
    for size in trim_sizes:
        scenarios = reported(f"evidence records={size}", lambda size=size: trim_scenario(size, iterations))
        results.append({"kind": "evidence", "size": size, "scenarios": scenarios})
    result = {
        "captured_at": datetime.now(UTC).isoformat(),
        "source": benchmark_source_state(),
        "environment": {"packages": package_versions()},
        "method": {
            "clock": "time.perf_counter_ns",
            "latency_unit": "milliseconds",
            "iterations": iterations,
            "artifact_sizes": sizes,
            "pattern_sizes": pattern_sizes,
            "evidence_record_sizes": trim_sizes,
            "external_services": "none; graph rows are synthetic in-process projections",
            "gc_freeze_after_setup": FREEZE_GC[0],
            "seeded_mutation_receipts": RECEIPTS[0],
        },
        "results": results,
        "budgets": budget_report(results),
    }
    return result


def main(argv: tuple[str, ...] = ()) -> int:
    parser = argparse_ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--sizes", type=int, nargs="+", default=[100, 1000], help="accepted-response store sizes")
    parser.add_argument("--pattern-sizes", type=int, nargs="+", default=[1000, 10000], help="pattern statement counts")
    parser.add_argument("--trim-sizes", type=int, nargs="+", default=[50, 200], help="structured evidence record counts")
    parser.add_argument("--iterations", type=int, default=10)
    parser.add_argument("--check", action="store_true", help="exit 1 when a scenario exceeds its budget")
    parser.add_argument("--no-gc-freeze", action="store_true", help="measure without freezing setup objects out of GC")
    parser.add_argument("--receipts", type=int, default=10_000, help="prior mutation receipts to seed in each core")
    args = parser.parse_args(argv)
    if args.iterations < 3:
        parser.error("iterations must be at least 3")
    if any(size < 2 for size in (*args.sizes, *args.pattern_sizes, *args.trim_sizes)):
        parser.error("sizes must be at least 2")
    if any(size <= args.iterations for size in (*args.sizes, *args.pattern_sizes)):
        parser.error("sizes must exceed the iteration count (each scenario also runs one warm-up)")
    FREEZE_GC[0] = not args.no_gc_freeze
    if args.receipts < 0:
        parser.error("receipts must not be negative")
    RECEIPTS[0] = args.receipts
    started = time_perf_counter_ns()
    result = run_benchmark(sorted(set(args.sizes)), sorted(set(args.pattern_sizes)), sorted(set(args.trim_sizes)), args.iterations)
    try:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json_dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    except OSError as err:
        parser.error(f"cannot write benchmark output: {err}")
    budgets = result.get("budgets", {})
    over_budget = [name for name, entry in budgets.items() if not entry.get("within_budget", False)]
    for name, entry in sorted(budgets.items()):
        within_budget = entry.get("within_budget", False)
        if name.startswith("sparse_found_target"):
            status = "ok" if within_budget else "FAIL"
            print(f"{status:4} {name:24} sparse retrieval returned the target", file=sys_stderr)
            continue
        status = "ok" if within_budget else "OVER"
        size, p95_ms, budget_ms = entry.get("size", 0), entry.get("p95_ms", 0.0), entry.get("budget_ms", 0.0)
        print(f"{status:4} {name:24} size={size:<7} p95={p95_ms:>10.2f} ms  budget={budget_ms:.0f} ms", file=sys_stderr)
    print(f"[scale] total {(time_perf_counter_ns() - started) / 1e9:.1f}s", file=sys_stderr)
    print(args.output)
    result_code = 1 if args.check and over_budget else 0
    return result_code


if __name__ == "__main__":
    raise SystemExit(main(tuple(sys_argv[1:])))
