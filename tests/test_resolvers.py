"""Section 4 resolver, executor, accounting, and orchestration conformance."""

from collections.abc import Mapping
from dataclasses import replace
from datetime import UTC, datetime

import pytest

from engram import persistence, service as service_module
from engram.artifacts import ArtifactProvenance, ArtifactStatistics, CachedResponseArtifact, LifecycleState
from engram.constants import Tier
from engram.core import Engram
from engram.errors import ConflictError, InvalidRequestError
from engram.fusion import PERMISSIVE_CANDIDATE_AUTHORITY, CandidateFusionEngine, FusionPolicyReason
from engram.identity import ScopeKey, build_retrieval_representation, build_standalone_identity
from engram.repository import ArtifactRepository
from engram.resolution import (
    AccountingObservation,
    BudgetConsumption,
    Candidate,
    CandidateSource,
    CostClass,
    EvidenceKind,
    EvidenceReference,
    FeatureSet,
    QueryFrame,
    QueryFrameBuilder,
    ResolutionBudget,
    ResolutionOutcome,
    ResolverResult,
    ResolverState,
)
from engram.resolvers import (
    ExactResolver,
    LexicalResolver,
    PatternResolver,
    ResolutionAccountingFinalizer,
    ResolutionOrchestrator,
    ResolverBudget,
    ResolverExecutor,
    ResolverRegistry,
    ResolverReservation,
    StructuredGraphResolver,
    SupportSemanticResolver,
)
from engram.service import EngramCore

NOW = datetime(2026, 8, 12, 18, 0, tzinfo=UTC)
START_NS = 1_000_000_000


def artifact(
    statement_id: str = "stmt-accepted",
    *,
    request: str = "What is Engram?",
    response: str = "Engram preserves exact text: café ☕.",
    aliases: tuple[str, ...] = ("Explain Engram",),
    namespace: str = "tenant-a",
    lifecycle: LifecycleState = LifecycleState.ACTIVE,
    source_label: str = "tapestry:released",
    support_claim_ids: tuple[str, ...] = (),
    metadata=(),
) -> CachedResponseArtifact:
    scope = ScopeKey(namespace=namespace)
    selected_metadata = metadata if isinstance(metadata, dict) else {"approved": True}
    return CachedResponseArtifact(
        statement_id=statement_id,
        generation=1,
        response=response,
        query_identity=build_standalone_identity(request, scope),
        retrieval=build_retrieval_representation(request, aliases),
        tier=Tier.STATIC,
        lifecycle=lifecycle,
        scope=scope,
        support_claim_ids=support_claim_ids,
        valid_from="",
        valid_from_available=False,
        valid_until="",
        valid_until_available=False,
        knowledge_epoch=0,
        knowledge_epoch_available=False,
        superseded_by="",
        provenance=ArtifactProvenance(source_label, "regulator-a", "2026-08-12T16:00:00Z"),
        statistics=ArtifactStatistics(),
        metadata=selected_metadata,
    )


def engine_with_artifacts(*artifacts: CachedResponseArtifact) -> Engram:
    engine = Engram()
    engine.response_repository = ArtifactRepository(artifacts)
    persistence.synchronize_response_compatibility_views(engine, ())
    return engine


def frame(
    engine: Engram,
    request: str = "What is Engram?",
    *,
    namespace: str = "tenant-a",
    required_metadata=(),
    required_source_label: str = "",
    budget=(),
):
    selected_budget = budget or ResolutionBudget.capture(
        lambda: START_NS,
        total_time_ms=100,
        resolver_time_ms=25,
    )
    return QueryFrameBuilder(engine, lambda: START_NS, lambda: NOW).build(
        request,
        ScopeKey(namespace=namespace),
        required_metadata=required_metadata if isinstance(required_metadata, dict) else {},
        required_source_label=required_source_label,
        diagnostic_seed=f"test:{request}:{namespace}",
        budget=selected_budget,
    )


def resolver_budget(query_frame) -> ResolverBudget:
    budget = query_frame.budget
    return ResolverBudget(
        deadline_ns=budget.deadline_ns,
        max_candidates=budget.max_candidates,
        max_graph_rows=budget.max_graph_rows,
        max_vector_results=budget.max_vector_results,
        max_evidence=budget.max_evidence,
        max_evidence_bytes=budget.max_evidence_bytes,
        max_output_bytes=budget.max_output_bytes,
        max_diagnostic_bytes=budget.max_diagnostic_bytes,
        max_working_memory_bytes=budget.max_working_memory_bytes,
    )


def candidate(
    statement_id: str = "stmt-candidate",
    *,
    source: CandidateSource = CandidateSource.LEXICAL,
    response: str = "Candidate response",
    evidence: tuple[EvidenceReference, ...] = (),
    features: Mapping[str, float] = {"score": 1.0},
) -> Candidate:
    return Candidate(
        candidate_id=f"candidate:{source.value}:{statement_id}",
        statement_id=statement_id,
        response=response,
        source=source,
        features=FeatureSet(values=features),
        evidence=evidence,
        scope=ScopeKey(),
        lifecycle=LifecycleState.ACTIVE,
    )


class FakeResolver:
    def __init__(
        self,
        name: str,
        result: ResolverResult,
        *,
        available: bool = True,
        cost_class: CostClass = CostClass.CHEAP,
        error: bool = False,
        availability_error: bool = False,
    ) -> None:
        self.name = name
        self.cost_class = cost_class
        self._result = result
        self._available = available
        self._error = error
        self._availability_error = availability_error
        self.calls = 0

    def available(self, frame: QueryFrame) -> bool:
        if self._availability_error:
            raise RuntimeError("injected availability failure")
        return self._available

    def resolve(self, frame: QueryFrame, budget: ResolverBudget) -> ResolverResult:
        self.calls += 1
        if self._error:
            raise RuntimeError("injected resolver failure")
        return self._result


def test_exact_adapter_preserves_alias_origin_text_and_hard_filters() -> None:
    accepted = artifact()
    engine = engine_with_artifacts(accepted)
    query_frame = frame(
        engine,
        "Explain Engram",
        required_metadata={"approved": True},
        required_source_label="tapestry:released",
    )

    result = ExactResolver(engine, lambda: START_NS).resolve(query_frame, resolver_budget(query_frame))

    assert result.state == ResolverState.COMPLETED
    assert result.reason_code == "exact_found"
    assert result.candidates[0].response == accepted.response
    assert result.candidates[0].provenance["retrieval_origin"] == "alias"
    assert result.accounting == (AccountingObservation(accepted.statement_id),)

    excluded = ExactResolver(engine, lambda: START_NS).resolve(
        replace(query_frame, required_metadata={"approved": False}),
        resolver_budget(query_frame),
    )
    assert excluded.candidates == ()
    assert excluded.reason_code == "exact_required_filter_excluded"


def test_adapter_candidate_ids_are_stable_within_and_distinct_across_requests() -> None:
    accepted = artifact()
    engine = engine_with_artifacts(accepted)
    first_frame = frame(engine, "Explain Engram")
    second_frame = replace(first_frame, diagnostic_id="resolution:sha256:" + "a" * 64)
    resolver = ExactResolver(engine, lambda: START_NS)

    first = resolver.resolve(first_frame, resolver_budget(first_frame)).candidates[0]
    replay = resolver.resolve(first_frame, resolver_budget(first_frame)).candidates[0]
    second = resolver.resolve(second_frame, resolver_budget(second_frame)).candidates[0]

    assert first.candidate_id == replay.candidate_id
    assert first.candidate_id != second.candidate_id


def test_exact_adapter_abstains_for_wrong_scope_and_ineligible_lifecycle() -> None:
    retired = artifact(lifecycle=LifecycleState.RETIRED)
    engine = engine_with_artifacts(retired)

    retired_result = ExactResolver(engine, lambda: START_NS).resolve(frame(engine), resolver_budget(frame(engine)))
    wrong_scope_frame = frame(engine, namespace="tenant-b")
    wrong_scope = ExactResolver(engine, lambda: START_NS).resolve(wrong_scope_frame, resolver_budget(wrong_scope_frame))

    assert retired_result.candidates == ()
    assert wrong_scope.candidates == ()


def test_pattern_discovery_is_pure_and_never_uses_graph_fallback(monkeypatch) -> None:
    engine = Engram()
    statement_id = engine.store("Hello {star1}", pattern="HELLO *")
    before = (
        engine.query_count,
        engine.get_statement(statement_id)["query_count"],
        engine.get_statement(statement_id)["hit_count"],
    )
    monkeypatch.setattr(engine, "graph_lookup", lambda _text: (_ for _ in ()).throw(AssertionError("graph fallback")))
    query_frame = frame(engine, "Hello Ada", namespace="")

    result = PatternResolver(engine, lambda: START_NS).resolve(query_frame, resolver_budget(query_frame))
    miss = engine.pattern_candidates("No pattern can match this", limit=3)

    assert result.candidates[0].statement_id == statement_id
    assert result.candidates[0].diagnostics["captures"] == ("Ada",)
    assert result.candidates[0].features.values["pattern_specificity"] == 1.0
    assert miss == []
    assert before == (
        engine.query_count,
        engine.get_statement(statement_id)["query_count"],
        engine.get_statement(statement_id)["hit_count"],
    )


def test_lexical_discovery_exposes_existing_components_without_accounting() -> None:
    engine = Engram()
    statement_id = engine.store("The automobile is fast")
    before_statement = dict(engine.get_statement(statement_id))
    before_keywords = {name: (value["query_count"], value["hit_count"]) for name, value in engine.keywords.items()}
    query_frame = frame(engine, "car", namespace="")

    result = LexicalResolver(engine, lambda: START_NS).resolve(query_frame, resolver_budget(query_frame))

    features = result.candidates[0].features
    assert result.candidates[0].statement_id == statement_id
    assert features.values["lexical_score"] > 0.0
    assert features.values["synonym_match_ratio"] > 0.0
    assert {"lexical_overlap", "recency", "keyword_hit_rate", "priority"}.issubset(features.values)
    assert features.unavailable == ("lemma_match_ratio", "stem_match_ratio")
    assert engine.query_count == 0
    assert engine.get_statement(statement_id)["query_count"] == before_statement["query_count"]
    assert {name: (value["query_count"], value["hit_count"]) for name, value in engine.keywords.items()} == before_keywords


def test_legacy_query_wrappers_retain_accounting_behavior() -> None:
    engine = Engram()
    lexical_id = engine.store("alpha beta")
    pattern_id = engine.store("Pattern response", pattern="PING")

    assert engine.query_candidates("alpha", limit=1)["matches"]
    assert engine.get_statement(lexical_id)["query_count"] == 0
    legacy_query = engine.query("alpha", limit=1)
    assert legacy_query["matches"]
    assert set(legacy_query) == {"matches", "keywords", "resolved_query"}
    assert engine.get_statement(lexical_id)["query_count"] == 1
    assert engine.pattern_candidates("ping")
    assert engine.get_statement(pattern_id)["query_count"] == 0
    assert engine.pattern_query("ping")[2] == "Pattern response"
    assert engine.get_statement(pattern_id)["query_count"] == 1
    assert engine.get_statement(pattern_id)["hit_count"] == 1


def test_structured_graph_adapter_emits_minimal_stable_references(monkeypatch) -> None:
    engine = Engram()
    rows = [
        {"claim_id": "claim-1", "subject": "Ada", "predicate": "built", "object": "Engine"},
        {"subject": "Legacy", "predicate": "is", "object": "bounded"},
    ]
    monkeypatch.setattr(
        engine,
        "structured_graph_evidence",
        lambda _text, row_limit, cooperative_check=(), max_working_memory_bytes=0: rows[:row_limit],
    )
    query_frame = frame(engine, "Ada", namespace="")

    result = StructuredGraphResolver(engine, lambda: START_NS).resolve(query_frame, resolver_budget(query_frame))

    assert result.candidates == ()
    assert [value.kind for value in result.evidence] == [EvidenceKind.GRAPH_FACT, EvidenceKind.GRAPH_FACT]
    assert result.evidence[0].evidence_id == "claim-1"
    assert result.evidence[1].evidence_id.startswith("legacy-graph:sha256:")
    assert result.evidence[1].provenance["legacy_identifier_synthesized"] is True


def test_support_semantic_adapter_only_returns_support_linked_artifacts(monkeypatch) -> None:
    accepted = artifact(support_claim_ids=("claim-support",))
    engine = engine_with_artifacts(accepted)
    engine.config["graph"]["enabled"] = True
    engine.config["graph"]["vector_enabled"] = True
    engine.config["graph"]["vector_weight"] = 1.0
    engine.statements[engine.statement_index[accepted.statement_id]]["priority"] = 3
    monkeypatch.setattr(
        engine,
        "graph_vector_claims",
        lambda _text, *, limit=0: [
            {"claim_id": "claim-support", "similarity": 0.9},
            {"claim_id": "unlinked", "similarity": 1.0},
        ][:limit],
    )
    query_frame = frame(engine)

    result = SupportSemanticResolver(engine, lambda: START_NS).resolve(query_frame, resolver_budget(query_frame))

    assert len(result.candidates) == 1
    assert result.candidates[0].statement_id == accepted.statement_id
    assert result.candidates[0].source == CandidateSource.SUPPORT_SEMANTIC
    assert tuple(reference.evidence_id for reference in result.candidates[0].evidence) == ("claim-support",)
    assert result.candidates[0].features.values["semantic_score"] == pytest.approx(0.9)
    assert result.candidates[0].features.values["priority"] == pytest.approx(3.0)
    assert result.candidates[0].features.values["legacy_retrieval_score"] == pytest.approx(3.9)
    assert result.consumption.vector_results == 1


def test_registry_plan_is_deterministic_and_records_all_decisions() -> None:
    engine = Engram()
    query_frame = frame(
        engine,
        namespace="",
        budget=ResolutionBudget.capture(
            lambda: START_NS,
            total_time_ms=100,
            resolver_time_ms=25,
            allowed_cost_classes=(CostClass.EXACT, CostClass.CHEAP),
        ),
    )
    exact = FakeResolver("exact", ResolverResult("exact", ResolverState.COMPLETED), available=True, cost_class=CostClass.EXACT)
    unavailable = FakeResolver("pattern", ResolverResult("pattern", ResolverState.COMPLETED), available=False)
    expensive = FakeResolver(
        "support_semantic",
        ResolverResult("support_semantic", ResolverState.COMPLETED),
        cost_class=CostClass.EXPENSIVE,
    )
    registry = ResolverRegistry((exact, unavailable, expensive))

    plan = registry.plan(query_frame, ("exact", "support_semantic"))

    assert [entry.resolver.name for entry in plan.entries] == ["exact", "pattern", "support_semantic"]
    assert [entry.reason_code for entry in plan.entries] == ["", "not_configured", "cost_class_disabled"]
    assert plan.to_dict() == registry.plan(query_frame, ("exact", "support_semantic")).to_dict()
    with pytest.raises(InvalidRequestError, match="duplicates"):
        registry.plan(query_frame, ("exact", "exact"))


def test_registry_translates_availability_failures_without_aborting_plan() -> None:
    engine = Engram()
    query_frame = frame(engine, namespace="")
    broken = FakeResolver(
        "broken",
        ResolverResult("broken", ResolverState.COMPLETED),
        availability_error=True,
    )
    healthy = FakeResolver("healthy", ResolverResult("healthy", ResolverState.COMPLETED))

    plan = ResolverRegistry((broken, healthy)).plan(query_frame)
    execution = ResolverExecutor(lambda: START_NS).execute(query_frame, plan)

    assert plan.entries[0].reason_code == "availability_check_failed"
    assert [result.state for result in execution.results] == [ResolverState.UNAVAILABLE, ResolverState.COMPLETED]


def test_resolver_lease_and_reservation_codecs_are_deterministic() -> None:
    engine = Engram()
    query_frame = frame(engine, namespace="")
    lease = resolver_budget(query_frame)
    reservation = ResolverReservation("lexical", 2, lease, BudgetConsumption(resolvers=1, candidates=1))

    assert ResolverBudget.from_json(lease.to_json()) == lease
    assert ResolverReservation.from_json(reservation.to_json()) == reservation
    with pytest.raises(InvalidRequestError, match="unsupported resolver budget"):
        replace(lease, schema_version=2)
    with pytest.raises(InvalidRequestError, match="unsupported resolver reservation"):
        replace(reservation, schema_version=2)


def test_executor_isolates_failures_and_preserves_later_success() -> None:
    engine = Engram()
    query_frame = frame(engine, namespace="")
    failed = FakeResolver("first", ResolverResult("first", ResolverState.COMPLETED), error=True)
    later_candidate = candidate()
    completed = FakeResolver(
        "second",
        ResolverResult(
            "second",
            ResolverState.COMPLETED,
            candidates=(later_candidate,),
            accounting=(AccountingObservation(later_candidate.statement_id),),
        ),
    )
    registry = ResolverRegistry((failed, completed))

    execution = ResolverExecutor(lambda: START_NS).execute(query_frame, registry.plan(query_frame))

    assert [result.state for result in execution.results] == [ResolverState.FAILED, ResolverState.COMPLETED]
    assert execution.results[0].diagnostics["exception_type"] == "RuntimeError"
    assert execution.results[1].candidates == (later_candidate,)


def test_executor_short_circuits_only_on_one_exact_candidate() -> None:
    engine = Engram()
    query_frame = frame(engine, namespace="")
    exact_candidate = candidate(source=CandidateSource.EXACT)
    exact = FakeResolver(
        "exact",
        ResolverResult(
            "exact",
            ResolverState.COMPLETED,
            candidates=(exact_candidate,),
            accounting=(AccountingObservation(exact_candidate.statement_id),),
        ),
        cost_class=CostClass.EXACT,
    )
    later = FakeResolver("later", ResolverResult("later", ResolverState.COMPLETED))

    execution = ResolverExecutor(lambda: START_NS).execute(query_frame, ResolverRegistry((exact, later)).plan(query_frame))

    assert execution.exact_short_circuited is True
    assert later.calls == 0
    assert len(execution.results) == 1


def test_executor_enforces_nested_evidence_output_diagnostics_and_resource_bounds() -> None:
    engine = Engram()
    reference = EvidenceReference("claim-1", "oversized", EvidenceKind.SUPPORT, ScopeKey())
    oversized_candidate = candidate(response="x" * 10_000, evidence=(reference, reference))
    raw = ResolverResult(
        "oversized",
        ResolverState.COMPLETED,
        candidates=(oversized_candidate,),
        evidence=(reference,),
        accounting=(AccountingObservation(oversized_candidate.statement_id),),
        diagnostics={"detail": "x" * 500},
        consumption=BudgetConsumption(graph_rows=50, vector_results=50, working_memory_bytes=10_000),
    )
    resolver = FakeResolver("oversized", raw)
    selected_budget = ResolutionBudget.capture(
        lambda: START_NS,
        total_time_ms=100,
        resolver_time_ms=25,
        max_graph_rows=1,
        max_vector_results=1,
        max_evidence=1,
        max_evidence_bytes=1,
        max_output_bytes=4_096,
        max_diagnostic_bytes=0,
        max_working_memory_bytes=1,
    )
    query_frame = frame(engine, namespace="", budget=selected_budget)

    result = ResolverExecutor(lambda: START_NS).execute(query_frame, ResolverRegistry((resolver,)).plan(query_frame)).results[0]

    assert result.candidates == ()
    assert result.evidence == ()
    assert result.diagnostics == {}
    assert result.consumption.graph_rows == 1
    assert result.consumption.vector_results == 1
    assert result.consumption.evidence <= 1
    assert result.consumption.evidence_bytes <= 1
    assert result.consumption.output_bytes <= 4_096
    assert result.consumption.diagnostic_bytes == 0
    assert result.consumption.working_memory_bytes <= 1
    assert {
        "diagnostic_bytes",
        "evidence_bytes",
        "graph_rows",
        "output_bytes",
        "vector_results",
        "working_memory_bytes",
    }.issubset(result.consumption.exhausted_dimensions)


def test_executor_preserves_unavailable_consumption_measurements() -> None:
    engine = Engram()
    query_frame = frame(engine, namespace="")
    raw = ResolverResult(
        "unmeasured",
        ResolverState.COMPLETED,
        consumption=BudgetConsumption(resolvers=1, measurement_available=False),
    )
    registry = ResolverRegistry((FakeResolver("unmeasured", raw),))

    result = ResolverExecutor(lambda: START_NS).execute(query_frame, registry.plan(query_frame)).results[0]

    assert result.consumption.measurement_available is False


def test_concrete_lexical_resolver_cooperates_with_lease_deadline(monkeypatch) -> None:
    engine = Engram()
    calls = iter((START_NS, START_NS + 2))

    def discover(_text, **options):
        options["cooperative_check"]()
        raise AssertionError("deadline check must stop discovery")

    monkeypatch.setattr(engine, "query_candidates", discover)
    query_frame = frame(engine, namespace="")
    lease = replace(resolver_budget(query_frame), deadline_ns=START_NS + 1)

    result = LexicalResolver(engine, lambda: next(calls)).resolve(query_frame, lease)

    assert result.state == ResolverState.EXHAUSTED
    assert result.reason_code == "resolver_time_budget"
    assert result.consumption.exhausted_dimensions == ("resolver_time",)


def test_concrete_lexical_resolver_abstains_before_exceeding_memory_estimate() -> None:
    engine = Engram()
    engine.store("alpha beta gamma")
    query_frame = frame(engine, "alpha beta", namespace="")
    lease = replace(resolver_budget(query_frame), max_working_memory_bytes=100)

    result = LexicalResolver(engine, lambda: START_NS).resolve(query_frame, lease)

    assert result.state == ResolverState.EXHAUSTED
    assert result.reason_code == "working_memory_bytes_budget"
    assert result.candidates == ()
    assert result.consumption.exhausted_dimensions == ("working_memory_bytes",)


def test_executor_reports_total_deadline_and_resolver_count_exhaustion() -> None:
    engine = Engram()
    resolver = FakeResolver("one", ResolverResult("one", ResolverState.COMPLETED))
    deadline_frame = frame(engine, namespace="")
    deadline = ResolverExecutor(lambda: deadline_frame.budget.deadline_ns).execute(
        deadline_frame,
        ResolverRegistry((resolver,)).plan(deadline_frame),
    )
    first = FakeResolver("first", ResolverResult("first", ResolverState.COMPLETED))
    second = FakeResolver("second", ResolverResult("second", ResolverState.COMPLETED))
    count_budget = replace(deadline_frame.budget, max_resolvers=1)
    count_frame = replace(deadline_frame, budget=count_budget)
    count = ResolverExecutor(lambda: START_NS).execute(count_frame, ResolverRegistry((first, second)).plan(count_frame))

    assert deadline.results[0].state == ResolverState.EXHAUSTED
    assert deadline.results[0].reason_code == "total_deadline"
    assert count.results[-1].state == ResolverState.EXHAUSTED
    assert count.results[-1].reason_code == "resolver_budget"


def test_executor_rejects_result_returned_after_aggregate_deadline() -> None:
    engine = Engram()
    query_frame = frame(engine, namespace="")
    near_deadline = START_NS + 90_000_000
    at_deadline = query_frame.budget.deadline_ns
    moments = iter((near_deadline, near_deadline, near_deadline, at_deadline, at_deadline))
    resolver = FakeResolver("late", ResolverResult("late", ResolverState.COMPLETED))

    result = (
        ResolverExecutor(lambda: next(moments, at_deadline))
        .execute(
            query_frame,
            ResolverRegistry((resolver,)).plan(query_frame),
        )
        .results[0]
    )

    assert result.state == ResolverState.EXHAUSTED
    assert result.reason_code == "total_deadline"
    assert result.consumption.exhausted_dimensions == ("total_time",)


def test_executor_uses_its_clock_instead_of_untrusted_reported_elapsed_time() -> None:
    engine = Engram()
    query_frame = frame(engine, namespace="")
    raw = ResolverResult(
        "elapsed",
        ResolverState.COMPLETED,
        consumption=BudgetConsumption(elapsed_ns=query_frame.budget.resolver_time_ms * 2_000_000, resolvers=1),
    )

    result = (
        ResolverExecutor(lambda: START_NS)
        .execute(
            query_frame,
            ResolverRegistry((FakeResolver("elapsed", raw),)).plan(query_frame),
        )
        .results[0]
    )

    assert result.state == ResolverState.COMPLETED
    assert result.consumption.elapsed_ns == 0


def test_accounting_deduplicates_candidates_and_applies_success_once() -> None:
    engine = Engram()
    statement_id = engine.store("alpha beta")
    observation = AccountingObservation(statement_id, ("alpha",))
    results = (
        ResolverResult("lexical", ResolverState.COMPLETED, accounting=(observation,)),
        ResolverResult("pattern", ResolverState.COMPLETED, accounting=(observation,)),
    )
    finalizer = ResolutionAccountingFinalizer(engine)

    first = finalizer.finalize("request-1", results, statement_id)
    replay = finalizer.finalize("request-1", results, statement_id)

    assert first.candidate_statement_ids == (statement_id,)
    assert first.success_applied is True
    assert replay.idempotent is True
    assert engine.query_count == 1
    assert engine.hit_count == 1
    assert engine.get_statement(statement_id)["query_count"] == 1
    assert engine.get_statement(statement_id)["hit_count"] == 1
    assert engine.keywords["alpha"]["query_count"] == 1
    assert engine.keywords["alpha"]["hit_count"] == 1


def test_accounting_retry_signature_ignores_elapsed_time_and_retention_is_bounded() -> None:
    engine = Engram()
    finalizer = ResolutionAccountingFinalizer(engine, max_requests=1)
    first = (ResolverResult("empty", ResolverState.COMPLETED, consumption=BudgetConsumption(elapsed_ns=1, resolvers=1)),)
    replay = (ResolverResult("empty", ResolverState.COMPLETED, consumption=BudgetConsumption(elapsed_ns=2, resolvers=1)),)

    finalizer.finalize("request-one", first)
    repeated = finalizer.finalize("request-one", replay)
    finalizer.finalize("request-two", first)

    assert repeated.idempotent is True
    assert tuple(finalizer._requests) == ("request-two",)


def test_accounting_rejects_invalid_acceptance_before_any_mutation() -> None:
    engine = Engram()
    statement_id = engine.store("alpha beta")
    results = (
        ResolverResult(
            "lexical",
            ResolverState.COMPLETED,
            accounting=(AccountingObservation(statement_id, ("alpha",)),),
        ),
    )

    with pytest.raises(InvalidRequestError, match="not an observed candidate"):
        ResolutionAccountingFinalizer(engine).finalize("request-invalid", results, "not-observed")

    assert engine.query_count == 0
    assert engine.get_statement(statement_id)["query_count"] == 0


def test_core_orchestration_exact_answer_is_deterministic_and_retry_safe() -> None:
    accepted = artifact()
    core = EngramCore(engine_with_artifacts(accepted))

    first = core.resolve_request("Explain Engram", "request-exact", namespace="tenant-a", accept_exact=True)
    replay = core.resolve_request("Explain Engram", "request-exact", namespace="tenant-a", accept_exact=True)

    assert first is replay
    assert first.outcome == ResolutionOutcome.ANSWER
    assert first.selected_candidate.response == accepted.response
    assert [result.resolver for result in first.resolver_results] == ["exact"]
    updated = core.engram.response_repository.get_artifact(accepted.statement_id)
    assert updated.statistics.query_count == 1
    assert updated.statistics.hit_count == 1
    assert core.engram.get_statement(accepted.statement_id)["query_count"] == 1
    assert core.engram.get_statement(accepted.statement_id)["hit_count"] == 1
    with pytest.raises(ConflictError, match="different input"):
        core.resolve_request("Different request", "request-exact", namespace="tenant-a", accept_exact=True)


def test_core_result_cache_eviction_discards_matching_transient_accounting(monkeypatch) -> None:
    monkeypatch.setattr(service_module, "MAX_TRANSIENT_RECORDS", 1)
    core = EngramCore(Engram())

    core.resolve_request("first miss", "request-first", configured_resolvers=("exact",))
    core.resolve_request("second miss", "request-second", configured_resolvers=("exact",))
    replay = core.resolve_request("first miss", "request-first", configured_resolvers=("exact",))

    assert replay.outcome == ResolutionOutcome.MISS
    assert tuple(core._resolution_requests) == ("request-first",)
    assert tuple(core._resolution_accounting._requests) == ("request-first",)


def test_output_budget_downgrade_does_not_record_accepted_success() -> None:
    accepted = artifact(response="x" * 1_000)
    core = EngramCore(engine_with_artifacts(accepted))

    result = core.resolve_request(
        "What is Engram?",
        "request-output-downgrade",
        namespace="tenant-a",
        accept_exact=True,
        budget=ResolutionBudget(max_output_bytes=4_096),
    )

    updated = core.engram.response_repository.get_artifact(accepted.statement_id)
    accounting = result.frame_diagnostics["accounting"]
    assert isinstance(accounting, Mapping)
    assert result.outcome == ResolutionOutcome.MISS
    assert "answer_exceeds_output_budget" in result.reason_codes
    assert accounting["success_applied"] is False
    assert updated.statistics.query_count == 1
    assert updated.statistics.hit_count == 0


def test_exact_accounting_receipt_replays_after_restart_without_double_credit() -> None:
    accepted = artifact()
    first_core = EngramCore(engine_with_artifacts(accepted))
    first_core.resolve_request("Explain Engram", "request-restart", namespace="tenant-a", accept_exact=True)
    restored = persistence.load_engram_json(persistence.save_json(first_core.engram))
    restored_core = EngramCore(restored)

    replay = restored_core.resolve_request(
        "Explain Engram",
        "request-restart",
        namespace="tenant-a",
        accept_exact=True,
    )

    updated = restored_core.engram.response_repository.get_artifact(accepted.statement_id)
    assert replay.outcome == ResolutionOutcome.ANSWER
    assert updated.statistics.query_count == 1
    assert updated.statistics.hit_count == 1
    assert restored_core.engram.query_count == 1
    assert restored_core.engram.hit_count == 1


def test_orchestration_returns_evidence_for_non_exact_and_miss_for_no_output() -> None:
    engine = Engram()
    query_frame = frame(engine, namespace="")
    lexical_candidate = candidate()
    lexical = FakeResolver(
        "lexical",
        ResolverResult(
            "lexical",
            ResolverState.COMPLETED,
            candidates=(lexical_candidate,),
            accounting=(AccountingObservation(lexical_candidate.statement_id),),
        ),
    )
    evidence_accounting = ResolutionAccountingFinalizer(engine)
    evidence_orchestrator = ResolutionOrchestrator(
        ResolverRegistry((lexical,)),
        ResolverExecutor(lambda: START_NS),
        evidence_accounting,
        CandidateFusionEngine(authority=PERMISSIVE_CANDIDATE_AUTHORITY, clock_ns=lambda: START_NS),
    )
    evidence_result, _ = evidence_orchestrator.resolve(query_frame, "request-evidence")
    empty = FakeResolver("empty", ResolverResult("empty", ResolverState.COMPLETED))
    miss_orchestrator = ResolutionOrchestrator(
        ResolverRegistry((empty,)),
        ResolverExecutor(lambda: START_NS),
        ResolutionAccountingFinalizer(engine),
    )
    miss_result, _ = miss_orchestrator.resolve(query_frame, "request-miss")

    assert evidence_result.outcome == ResolutionOutcome.EVIDENCE
    assert evidence_result.selected_candidate_available is False
    assert tuple(candidate.statement_id for candidate in evidence_result.response_candidates) == (lexical_candidate.statement_id,)
    assert evidence_result.response_candidates[0].features.values["lexical"] == 1.0
    assert miss_result.outcome == ResolutionOutcome.MISS
    assert miss_result.response_candidates == ()
    assert miss_result.evidence == ()


def test_fused_non_exact_answer_is_fail_soft_and_accounted_once_without_implicit_acceptance() -> None:
    engine = Engram()
    statement_id = engine.store("Candidate response")
    reference = EvidenceReference("claim-fused", "semantic", EvidenceKind.SUPPORT, ScopeKey())
    lexical_candidate = candidate(
        statement_id,
        source=CandidateSource.LEXICAL,
        features={"lexical_score": 0.95},
    )
    semantic_candidate = candidate(
        statement_id,
        source=CandidateSource.SUPPORT_SEMANTIC,
        evidence=(reference,),
        features={"semantic_score": 0.92, "support_coverage": 1.0},
    )
    observation = AccountingObservation(statement_id, ("candidate",))
    failed = FakeResolver("failed", ResolverResult("failed", ResolverState.COMPLETED), error=True)
    lexical = FakeResolver(
        "lexical",
        ResolverResult(
            "lexical",
            ResolverState.COMPLETED,
            candidates=(lexical_candidate,),
            accounting=(observation,),
        ),
    )
    semantic = FakeResolver(
        "semantic",
        ResolverResult(
            "semantic",
            ResolverState.COMPLETED,
            candidates=(semantic_candidate,),
            accounting=(observation,),
        ),
    )
    orchestrator = ResolutionOrchestrator(
        ResolverRegistry((failed, lexical, semantic)),
        ResolverExecutor(lambda: START_NS),
        ResolutionAccountingFinalizer(engine),
    )
    query_frame = frame(engine, namespace="")

    result, first = orchestrator.resolve(query_frame, "request-fused", accept_exact=True)
    replay_result, replay = orchestrator.resolve(query_frame, "request-fused", accept_exact=True)

    assert result.outcome == ResolutionOutcome.ANSWER
    assert replay_result.selected_candidate == result.selected_candidate
    assert [value.state for value in result.resolver_results] == [
        ResolverState.FAILED,
        ResolverState.COMPLETED,
        ResolverState.COMPLETED,
    ]
    assert first.candidate_statement_ids == (statement_id,)
    assert first.accepted_statement_id == ""
    assert first.success_applied is False
    assert replay.idempotent is True
    assert engine.get_statement(statement_id)["query_count"] == 1
    assert engine.get_statement(statement_id)["hit_count"] == 0


def test_fusion_deadline_exhaustion_is_typed_in_complete_resolution_budget() -> None:
    engine = Engram()
    query_frame = frame(engine, namespace="")
    value = candidate(features={"lexical_score": 0.9})
    resolver = FakeResolver(
        "lexical",
        ResolverResult(
            "lexical",
            ResolverState.COMPLETED,
            candidates=(value,),
            accounting=(AccountingObservation(value.statement_id),),
        ),
    )
    orchestrator = ResolutionOrchestrator(
        ResolverRegistry((resolver,)),
        ResolverExecutor(lambda: START_NS),
        ResolutionAccountingFinalizer(engine),
        CandidateFusionEngine(
            authority=PERMISSIVE_CANDIDATE_AUTHORITY,
            clock_ns=lambda: query_frame.budget.deadline_ns,
        ),
    )

    result, finalization = orchestrator.resolve(query_frame, "request-fusion-deadline")

    assert result.outcome == ResolutionOutcome.MISS
    assert FusionPolicyReason.FUSION_DEADLINE_EXHAUSTED.value in result.reason_codes
    assert "fusion_deadline" in result.budget.exhausted_dimensions
    assert finalization.candidate_statement_ids == (value.statement_id,)
    assert finalization.success_applied is False


def test_orchestrator_reserves_remaining_memory_and_reports_fusion_consumption() -> None:
    engine = Engram()
    query_frame = frame(engine, namespace="")
    value = candidate(features={"lexical_score": 0.9})
    raw = ResolverResult(
        "lexical",
        ResolverState.COMPLETED,
        candidates=(value,),
        accounting=(AccountingObservation(value.statement_id),),
    )
    executor = ResolverExecutor(lambda: START_NS)
    probe_resolver = FakeResolver("lexical", raw)
    probe_execution = executor.execute(query_frame, ResolverRegistry((probe_resolver,)).plan(query_frame))
    fusion = CandidateFusionEngine(authority=PERMISSIVE_CANDIDATE_AUTHORITY, clock_ns=lambda: START_NS)
    fusion_required = fusion.decide(query_frame, (value,)).working_memory_bytes
    total_limit = probe_execution.consumption.working_memory_bytes + fusion_required - 1
    constrained_frame = replace(
        query_frame,
        budget=replace(query_frame.budget, max_working_memory_bytes=total_limit),
    )
    orchestrator = ResolutionOrchestrator(
        ResolverRegistry((FakeResolver("lexical", raw),)),
        executor,
        ResolutionAccountingFinalizer(engine),
        fusion,
    )

    result, finalization = orchestrator.resolve(constrained_frame, "request-fusion-memory")

    assert result.outcome == ResolutionOutcome.MISS
    assert FusionPolicyReason.FUSION_MEMORY_EXHAUSTED.value in result.reason_codes
    assert "working_memory_bytes" in result.budget.exhausted_dimensions
    assert result.budget.working_memory_bytes == total_limit
    assert finalization.success_applied is False


def test_complete_result_serialization_obeys_and_reports_output_budget() -> None:
    engine = Engram()
    selected_budget = ResolutionBudget.capture(
        lambda: START_NS,
        total_time_ms=100,
        resolver_time_ms=25,
        max_output_bytes=4_096,
    )
    query_frame = frame(engine, namespace="", budget=selected_budget)
    empty = FakeResolver("empty", ResolverResult("empty", ResolverState.COMPLETED))
    orchestrator = ResolutionOrchestrator(
        ResolverRegistry((empty,)),
        ResolverExecutor(lambda: START_NS),
        ResolutionAccountingFinalizer(engine),
    )

    result, _ = orchestrator.resolve(query_frame, "request-output-envelope")
    encoded_size = len(result.to_json().encode("utf-8"))

    assert encoded_size <= query_frame.budget.max_output_bytes
    assert result.budget.output_bytes == encoded_size


def test_complete_result_truncates_variable_payload_to_output_budget() -> None:
    engine = Engram()
    selected_budget = ResolutionBudget.capture(
        lambda: START_NS,
        total_time_ms=100,
        resolver_time_ms=25,
        max_output_bytes=4_096,
    )
    query_frame = frame(engine, namespace="", budget=selected_budget)
    large_candidate = candidate(response="x" * 2_500)
    resolver = FakeResolver(
        "large",
        ResolverResult(
            "large",
            ResolverState.COMPLETED,
            candidates=(large_candidate,),
            accounting=(AccountingObservation(large_candidate.statement_id),),
        ),
    )
    orchestrator = ResolutionOrchestrator(
        ResolverRegistry((resolver,)),
        ResolverExecutor(lambda: START_NS),
        ResolutionAccountingFinalizer(engine),
        CandidateFusionEngine(authority=PERMISSIVE_CANDIDATE_AUTHORITY, clock_ns=lambda: START_NS),
    )

    result, _ = orchestrator.resolve(query_frame, "request-output-payload")
    encoded_size = len(result.to_json().encode("utf-8"))

    assert encoded_size <= query_frame.budget.max_output_bytes
    assert result.budget.output_bytes == encoded_size
    assert "output_truncated" in result.reason_codes
    assert "output_bytes" in result.budget.exhausted_dimensions
