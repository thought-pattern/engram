"""Section 4 registry planning, bounded executor, lease codecs, and exactly-once accounting conformance."""

from pytest import raises as pytest_raises

from engram.artifacts import validate_cached_response_artifact
from engram.constants_graph import PropositionProjectionQuery
from engram.constants_identity import EMPTY_SCOPE_KEY
from engram.constants_resolution import (
    BUDGET_CONSUMPTION_FIELDS,
    RESOLVER_BUDGET_FIELDS,
    RESOLVER_RESULT_FIELDS,
    CandidateSource,
    CostClass,
    EvidenceKind,
    ResolverState,
)
from engram.coordination import AtomicMutationCoordinator
from engram.core import Engram
from engram.errors import InvalidRequestError, ResolutionCancelledError
from engram.graph import (
    proposition_projection,
    proposition_projection_from_graph_row,
)
from engram.graph_resolvers import StructuredGraphResolver, SupportSemanticResolver
from engram.repository import ArtifactRepository, tier_admission_policy
from engram.resolution import (
    accounting_observation,
    evidence_reference,
    resolver_result,
    validate_candidate,
    validate_resolver_result,
)
from engram.resolution_accounting import ResolutionAccountingFinalizer
from engram.resolution_budget import budget_consumption, capture_resolution_budget, validate_resolution_budget
from engram.resolution_frame import QueryFrameBuilder, query_frame_with_changes
from engram.resolution_sizing import json_array_bytes, json_size
from engram.resolver_execution import ResolverExecutor, bound_validated_resolver_result
from engram.resolver_plan import (
    ResolverRegistry,
    resolver_budget_from_dict,
    resolver_budget_to_dict,
    resolver_budget_with_changes,
    resolver_contract,
    resolver_reservation,
    resolver_reservation_from_dict,
    resolver_reservation_to_dict,
    validate_resolver_budget,
)
from engram.responses import AcceptedResponseService
from tests.resolver_fixtures import (
    ACCEPTED_ARTIFACT,
    BY_ID_PROJECTION_CHANGES,
    NOW,
    SEMANTIC_PROJECTION_ROW,
    SPARSE_CANDIDATE,
    START_NS,
    STRUCTURED_PROJECTION_ROW,
    TENANT_A_SCOPE,
    FakeResolver,
    enable_graph_resolvers,
)


def test_executor_defensively_bounds_full_proposition_evidence_in_current_schema(monkeypatch) -> None:
    engine = Engram()
    discovered = proposition_projection_from_graph_row(STRUCTURED_PROJECTION_ROW, PropositionProjectionQuery.STRUCTURED_ENTITY)
    current = proposition_projection(**{**discovered, **BY_ID_PROJECTION_CHANGES})
    monkeypatch.setattr(
        engine,
        "structured_proposition_projections",
        lambda internal_text, row_limit, cooperative_check=(), max_working_memory_bytes=0, *, basis_window: [discovered][
            :row_limit
        ],
    )
    monkeypatch.setattr(engine, "current_proposition_projection", lambda internal_proposition_id, internal_basis_window: (current,))
    query_frame = QueryFrameBuilder(engine, lambda: START_NS, lambda: NOW).build(
        "Ada", EMPTY_SCOPE_KEY, diagnostic_seed="test:Ada:"
    )
    lease = validate_resolver_budget({name: query_frame.get("budget", {}).get(name, 0) for name in RESOLVER_BUDGET_FIELDS})
    raw = StructuredGraphResolver(engine, lambda: START_NS).resolve(query_frame, lease)

    bounded = bound_validated_resolver_result(
        validate_resolver_result(raw), validate_resolver_budget(resolver_budget_with_changes(lease, {"max_evidence": 0}))
    )
    bounded_consumption = bounded.get("consumption", {})

    assert bounded.keys() >= {"proposition_evidence", "evidence"}
    assert bounded.get("proposition_evidence", ()) == ()
    assert bounded.get("evidence", ()) == ()
    assert "evidence" in bounded_consumption
    assert bounded_consumption.get("evidence", 0) == 0
    assert "evidence" in bounded_consumption.get("exhausted_dimensions", ())


def test_executor_isolates_a_malformed_resolver_result() -> None:
    engine = Engram()
    query_frame = QueryFrameBuilder(engine, lambda: START_NS, lambda: NOW).build(
        "malformed resolver", TENANT_A_SCOPE, diagnostic_seed="test:malformed resolver:tenant-a"
    )
    malformed = FakeResolver("malformed", {})

    result = (
        ResolverExecutor(lambda: START_NS)
        .execute(query_frame, ResolverRegistry((malformed,)).plan(query_frame))
        .get("results", ())[0]
    )

    assert "state" in result
    assert result.get("state", ResolverState.FAILED) == ResolverState.FAILED
    assert result.get("reason_code", "") == "invalid_resolver_result"
    assert result.get("diagnostics", {}) == {"exception_type": "InvalidRequestError"}


def test_executor_runs_semantic_proposition_evidence_after_candidate_capacity_is_consumed(monkeypatch) -> None:
    engine = Engram()
    enable_graph_resolvers(engine, vector=True)
    engine.config.get("graph", {}).update({"enabled": True, "vector_enabled": True, "vector_weight": 1.0})
    discovered = proposition_projection_from_graph_row(
        {**SEMANTIC_PROJECTION_ROW, "proposition_id": "proposition-after-candidate", "semantic_similarity": 0.8},
        PropositionProjectionQuery.VECTOR,
        "proposition_premise_embeddings",
    )
    current = proposition_projection(**{**discovered, **BY_ID_PROJECTION_CHANGES})
    monkeypatch.setattr(
        engine,
        "graph_vector_proposition_projections",
        lambda internal_text, *, limit=0, cooperative_check=(), max_working_memory_bytes=0, basis_window: [discovered][:limit],
    )
    monkeypatch.setattr(engine, "current_proposition_projection", lambda internal_proposition_id, internal_basis_window: (current,))
    query_frame = QueryFrameBuilder(engine, lambda: START_NS, lambda: NOW).build(
        "Ada",
        EMPTY_SCOPE_KEY,
        diagnostic_seed="test:Ada:",
        budget=capture_resolution_budget(
            lambda: START_NS,
            max_candidates=1,
        ),
    )
    sparse = FakeResolver(
        "sparse",
        resolver_result(
            "sparse",
            ResolverState.COMPLETED,
            candidates=(SPARSE_CANDIDATE,),
            accounting=(accounting_observation("stmt-candidate"),),
        ),
    )
    semantic = SupportSemanticResolver(engine, lambda: START_NS)

    report = ResolverExecutor(lambda: START_NS).execute(
        query_frame,
        ResolverRegistry((sparse, semantic)).plan(query_frame),
    )
    results = report.get("results", ())
    semantic_lease = report.get("reservations", ())[1].get("lease", {})

    assert len(results[0].get("candidates", ())) == 1
    assert "max_candidates" in semantic_lease
    assert semantic_lease.get("max_candidates", 0) == 0
    assert tuple(record.get("proposition_id", "") for record in results[1].get("proposition_evidence", ())) == (
        "proposition-after-candidate",
    )
    assert "accounting" in results[1]
    assert results[1].get("accounting", ()) == ()


def test_registry_plan_is_deterministic_and_records_all_decisions() -> None:
    engine = Engram()
    query_frame = QueryFrameBuilder(engine, lambda: START_NS, lambda: NOW).build(
        "What is Engram?",
        EMPTY_SCOPE_KEY,
        diagnostic_seed="test:What is Engram?:",
        budget=capture_resolution_budget(
            lambda: START_NS,
            allowed_cost_classes=(CostClass.EXACT, CostClass.CHEAP),
        ),
    )
    exact = FakeResolver("exact", resolver_result("exact", ResolverState.COMPLETED), available=True, cost_class=CostClass.EXACT)
    unavailable = FakeResolver("sparse", resolver_result("sparse", ResolverState.COMPLETED), available=False)
    expensive = FakeResolver(
        "support_semantic",
        resolver_result("support_semantic", ResolverState.COMPLETED),
        cost_class=CostClass.EXPENSIVE,
    )
    registry = ResolverRegistry((exact, unavailable, expensive))

    plan = registry.plan(query_frame, ("exact", "support_semantic"))
    entries = plan.get("entries", ())

    assert [resolver_contract(entry.get("resolver", ()))[0] for entry in entries] == ["exact", "sparse", "support_semantic"]
    assert all("reason_code" in entry for entry in entries)
    assert [entry.get("reason_code", "") for entry in entries] == ["", "not_configured", "cost_class_disabled"]
    assert plan == registry.plan(query_frame, ("exact", "support_semantic"))
    with pytest_raises(InvalidRequestError, match="duplicates"):
        registry.plan(query_frame, ("exact", "exact"))


def test_registry_translates_availability_failures_without_aborting_plan() -> None:
    engine = Engram()
    query_frame = QueryFrameBuilder(engine, lambda: START_NS, lambda: NOW).build(
        "What is Engram?", EMPTY_SCOPE_KEY, diagnostic_seed="test:What is Engram?:"
    )
    broken = FakeResolver(
        "broken",
        resolver_result("broken", ResolverState.COMPLETED),
        availability_error=True,
    )
    healthy = FakeResolver("healthy", resolver_result("healthy", ResolverState.COMPLETED))

    plan = ResolverRegistry((broken, healthy)).plan(query_frame)
    execution = ResolverExecutor(lambda: START_NS).execute(query_frame, plan)

    assert plan.get("entries", ())[0].get("reason_code", "") == "availability_check_failed"
    assert [result.get("state", ResolverState.FAILED) for result in execution.get("results", ())] == [
        ResolverState.UNAVAILABLE,
        ResolverState.COMPLETED,
    ]


def test_executor_propagates_cancellation_without_publishing_partial_results() -> None:
    engine = Engram()
    query_frame = QueryFrameBuilder(engine, lambda: START_NS, lambda: NOW).build(
        "What is Engram?", EMPTY_SCOPE_KEY, diagnostic_seed="test:What is Engram?:"
    )

    class CancellableResolver(FakeResolver):
        def resolve(self, frame, budget, cooperative_check=()) -> dict:
            del frame, budget
            cooperative_check()
            self.calls += 1
            result = self.internal_result
            return result

    resolver = CancellableResolver("structured_graph", resolver_result("structured_graph", ResolverState.COMPLETED))

    def cancel() -> None:
        raise ResolutionCancelledError("transport cancelled")

    with pytest_raises(ResolutionCancelledError, match="transport cancelled"):
        ResolverExecutor(lambda: START_NS).execute(
            query_frame,
            ResolverRegistry((resolver,)).plan(query_frame),
            cancel,
        )

    assert resolver.calls == 0


def test_resolver_lease_and_reservation_codecs_round_trip() -> None:
    engine = Engram()
    query_frame = QueryFrameBuilder(engine, lambda: START_NS, lambda: NOW).build(
        "What is Engram?", EMPTY_SCOPE_KEY, diagnostic_seed="test:What is Engram?:"
    )
    lease = validate_resolver_budget({name: query_frame.get("budget", {}).get(name, 0) for name in RESOLVER_BUDGET_FIELDS})
    reservation = resolver_reservation("sparse", 2, lease, budget_consumption(resolvers=1, candidates=1))

    assert resolver_budget_from_dict(resolver_budget_to_dict(lease)) == lease
    assert resolver_reservation_from_dict(resolver_reservation_to_dict(reservation)) == reservation


def test_executor_isolates_failures_and_preserves_later_success() -> None:
    engine = Engram()
    query_frame = QueryFrameBuilder(engine, lambda: START_NS, lambda: NOW).build(
        "What is Engram?", EMPTY_SCOPE_KEY, diagnostic_seed="test:What is Engram?:"
    )
    failed = FakeResolver("first", resolver_result("first", ResolverState.COMPLETED), error=True)
    completed = FakeResolver(
        "second",
        resolver_result(
            "second",
            ResolverState.COMPLETED,
            candidates=(SPARSE_CANDIDATE,),
            accounting=(accounting_observation("stmt-candidate"),),
        ),
    )
    registry = ResolverRegistry((failed, completed))

    execution = ResolverExecutor(lambda: START_NS).execute(query_frame, registry.plan(query_frame))
    results = execution.get("results", ())

    assert all(set(result) == RESOLVER_RESULT_FIELDS for result in results)
    assert [result.get("state", ResolverState.FAILED) for result in results] == [ResolverState.FAILED, ResolverState.COMPLETED]
    assert results[0].get("diagnostics", {}).get("exception_type", "") == "RuntimeError"
    assert results[1].get("candidates", ()) == (SPARSE_CANDIDATE,)


def test_executor_short_circuits_only_on_one_exact_candidate() -> None:
    engine = Engram()
    query_frame = QueryFrameBuilder(engine, lambda: START_NS, lambda: NOW).build(
        "What is Engram?", EMPTY_SCOPE_KEY, diagnostic_seed="test:What is Engram?:"
    )
    exact_candidate = validate_candidate(
        {**SPARSE_CANDIDATE, "candidate_id": "candidate:exact:stmt-candidate", "source": CandidateSource.EXACT}
    )
    exact = FakeResolver(
        "exact",
        resolver_result(
            "exact",
            ResolverState.COMPLETED,
            candidates=(exact_candidate,),
            accounting=(accounting_observation("stmt-candidate"),),
        ),
        cost_class=CostClass.EXACT,
    )
    later = FakeResolver("later", resolver_result("later", ResolverState.COMPLETED))

    execution = ResolverExecutor(lambda: START_NS).execute(query_frame, ResolverRegistry((exact, later)).plan(query_frame))

    assert execution.get("exact_short_circuited", False) is True
    assert later.calls == 0
    assert len(execution.get("results", ())) == 1


def test_executor_enforces_nested_evidence_output_diagnostics_and_resource_bounds() -> None:
    engine = Engram()
    reference = evidence_reference("proposition-1", "oversized", EvidenceKind.SUPPORT, EMPTY_SCOPE_KEY)
    oversized_candidate = validate_candidate({**SPARSE_CANDIDATE, "response": "x" * 10_000, "evidence": (reference, reference)})
    raw = resolver_result(
        "oversized",
        ResolverState.COMPLETED,
        candidates=(oversized_candidate,),
        evidence=(reference,),
        accounting=(accounting_observation("stmt-candidate"),),
        diagnostics={"detail": "x" * 500},
        consumption=budget_consumption(graph_rows=50, vector_results=50, working_memory_bytes=10_000),
    )
    resolver = FakeResolver("oversized", raw)
    selected_budget = capture_resolution_budget(
        lambda: START_NS,
        max_graph_rows=1,
        max_vector_results=1,
        max_evidence=1,
        max_evidence_bytes=1,
        max_output_bytes=4_096,
        max_diagnostic_bytes=0,
        max_working_memory_bytes=1,
    )
    query_frame = QueryFrameBuilder(engine, lambda: START_NS, lambda: NOW).build(
        "What is Engram?", EMPTY_SCOPE_KEY, diagnostic_seed="test:What is Engram?:", budget=selected_budget
    )

    execution = ResolverExecutor(lambda: START_NS).execute(query_frame, ResolverRegistry((resolver,)).plan(query_frame))
    result = execution.get("results", ())[0]
    consumption = result.get("consumption", {})

    assert set(result) == RESOLVER_RESULT_FIELDS
    assert set(consumption) == BUDGET_CONSUMPTION_FIELDS
    assert result.get("candidates", ()) == ()
    assert result.get("evidence", ()) == ()
    assert result.get("diagnostics", {}) == {}
    assert consumption.get("graph_rows", 0) == 1
    assert consumption.get("vector_results", 0) == 1
    assert consumption.get("evidence", 0) <= 1
    assert consumption.get("evidence_bytes", 0) <= 1
    assert consumption.get("output_bytes", 0) <= 4_096
    assert consumption.get("diagnostic_bytes", 0) == 0
    assert consumption.get("working_memory_bytes", 0) <= 1
    assert {
        "diagnostic_bytes",
        "evidence_bytes",
        "graph_rows",
        "output_bytes",
        "vector_results",
        "working_memory_bytes",
    }.issubset(consumption.get("exhausted_dimensions", ()))


def test_executor_preserves_unavailable_consumption_measurements() -> None:
    engine = Engram()
    query_frame = QueryFrameBuilder(engine, lambda: START_NS, lambda: NOW).build(
        "What is Engram?", EMPTY_SCOPE_KEY, diagnostic_seed="test:What is Engram?:"
    )
    raw = resolver_result(
        "unmeasured",
        ResolverState.COMPLETED,
        consumption=budget_consumption(resolvers=1, measurement_available=False),
    )
    registry = ResolverRegistry((FakeResolver("unmeasured", raw),))

    execution = ResolverExecutor(lambda: START_NS).execute(query_frame, registry.plan(query_frame))
    consumption = execution.get("results", ())[0].get("consumption", {})

    assert "measurement_available" in consumption
    assert consumption.get("measurement_available", False) is False


def test_executor_reports_resolver_count_exhaustion() -> None:
    engine = Engram()
    selected_frame = QueryFrameBuilder(engine, lambda: START_NS, lambda: NOW).build(
        "What is Engram?", EMPTY_SCOPE_KEY, diagnostic_seed="test:What is Engram?:"
    )
    first = FakeResolver("first", resolver_result("first", ResolverState.COMPLETED))
    second = FakeResolver("second", resolver_result("second", ResolverState.COMPLETED))
    count_budget = validate_resolution_budget({**selected_frame.get("budget", {}), "max_resolvers": 1})
    count_frame = query_frame_with_changes(selected_frame, {"budget": count_budget})
    count = ResolverExecutor(lambda: START_NS).execute(count_frame, ResolverRegistry((first, second)).plan(count_frame))
    last = count.get("results", ())[-1]

    assert last.get("state", ResolverState.FAILED) == ResolverState.EXHAUSTED
    assert last.get("reason_code", "") == "resolver_budget"


def test_executor_reports_elapsed_time_without_changing_a_completed_result() -> None:
    engine = Engram()
    query_frame = QueryFrameBuilder(engine, lambda: START_NS, lambda: NOW).build(
        "What is Engram?", EMPTY_SCOPE_KEY, diagnostic_seed="test:What is Engram?:"
    )
    raw = resolver_result(
        "elapsed",
        ResolverState.COMPLETED,
        consumption=budget_consumption(elapsed_ns=1, resolvers=1),
    )
    observed_elapsed_ns = 8_000_000_000
    moments = iter((START_NS, START_NS + observed_elapsed_ns))

    execution = ResolverExecutor(lambda: next(moments)).execute(
        query_frame,
        ResolverRegistry((FakeResolver("elapsed", raw),)).plan(query_frame),
    )
    result = execution.get("results", ())[0]
    consumption = result.get("consumption", {})

    assert set(consumption) == BUDGET_CONSUMPTION_FIELDS
    assert result.get("state", ResolverState.FAILED) == ResolverState.COMPLETED
    assert consumption.get("elapsed_ns", 0) == observed_elapsed_ns
    assert consumption.get("exhausted_dimensions", ()) == ()


def test_accounting_deduplicates_candidates_and_applies_success_once() -> None:
    engine = Engram()
    engine.response_repository = ArtifactRepository(
        (validate_cached_response_artifact({**ACCEPTED_ARTIFACT, "response": "Candidate response"}),)
    )
    statement_id = "stmt-accepted"
    observation = accounting_observation(statement_id)
    results = (
        resolver_result("exact", ResolverState.COMPLETED, accounting=(observation,)),
        resolver_result("sparse", ResolverState.COMPLETED, accounting=(observation,)),
    )
    finalizer = ResolutionAccountingFinalizer(
        engine,
        AcceptedResponseService(
            AtomicMutationCoordinator(engine.response_repository, engine.mutation_receipts),
            tier_admission_policy(engine.config.get("capacity", 1)),
        ),
    )

    first = finalizer.finalize("request-1", results, statement_id)
    replay = finalizer.finalize("request-1", results, statement_id)

    assert first.get("candidate_statement_ids", ()) == (statement_id,)
    assert first.get("success_applied", False) is True
    assert replay.get("idempotent", False) is True
    current = engine.response_repository.get_artifact(statement_id)
    assert current.get("statistics", {}).get("query_count", 0) == 1
    assert current.get("statistics", {}).get("hit_count", 0) == 1
    assert engine.query_count == 0
    assert engine.hit_count == 0
    assert engine.get_statement(statement_id) == {}


def test_accounting_retry_signature_ignores_elapsed_time_and_retention_is_bounded() -> None:
    engine = Engram()
    finalizer = ResolutionAccountingFinalizer(
        engine,
        AcceptedResponseService(
            AtomicMutationCoordinator(engine.response_repository, engine.mutation_receipts),
            tier_admission_policy(engine.config.get("capacity", 1)),
        ),
        max_requests=1,
    )
    first = (resolver_result("empty", ResolverState.COMPLETED, consumption=budget_consumption(elapsed_ns=1, resolvers=1)),)
    replay = (resolver_result("empty", ResolverState.COMPLETED, consumption=budget_consumption(elapsed_ns=2, resolvers=1)),)

    finalizer.finalize("request-one", first)
    repeated = finalizer.finalize("request-one", replay)
    finalizer.finalize("request-two", first)

    assert repeated.get("idempotent", False) is True
    assert tuple(finalizer.internal_requests) == ("request-two",)


def test_accounting_rejects_invalid_acceptance_before_any_mutation() -> None:
    engine = Engram()
    engine.response_repository = ArtifactRepository((ACCEPTED_ARTIFACT,))
    statement_id = "stmt-accepted"
    results = (
        resolver_result(
            "exact",
            ResolverState.COMPLETED,
            accounting=(accounting_observation(statement_id),),
        ),
    )
    finalizer = ResolutionAccountingFinalizer(
        engine,
        AcceptedResponseService(
            AtomicMutationCoordinator(engine.response_repository, engine.mutation_receipts),
            tier_admission_policy(engine.config.get("capacity", 1)),
        ),
    )

    with pytest_raises(InvalidRequestError, match="not an observed candidate"):
        finalizer.finalize("request-invalid", results, "not-observed")

    statistics = engine.response_repository.get_artifact(statement_id).get("statistics", {})
    assert engine.query_count == 0
    assert "query_count" in statistics
    assert statistics.get("query_count", 0) == 0


def test_json_array_bytes_matches_encoding_every_prefix() -> None:
    values = [{"proposition_id": f"p-{index}", "label": "Ü" * index, "scores": [index, 0.5], "flag": True} for index in range(12)]

    for count in range(len(values) + 1):
        prefix = values[:count]
        expected = json_size(prefix) if prefix else 0
        assert json_array_bytes(sum(json_size(value) for value in prefix), count) == expected
