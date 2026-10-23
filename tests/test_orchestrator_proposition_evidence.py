"""Section 5 orchestration of full Proposition evidence: canonicalization, usefulness, packaging, and bounds."""

from collections.abc import Mapping
from json import dumps as json_dumps

from pytest import approx as pytest_approx, mark as pytest_mark

from engram.constants_evidence import EVIDENCE_PACKAGE_FIELDS, EvidencePackageTruncationReason
from engram.constants_graph import PropositionProjectionQuery
from engram.constants_identity import EMPTY_SCOPE_KEY
from engram.constants_resolution import (
    ACCOUNTING_FINALIZATION_FIELDS,
    BUDGET_CONSUMPTION_FIELDS,
    RESOLUTION_RESULT_FIELDS,
    RESOLVER_BUDGET_FIELDS,
    RESOLVER_RESULT_FIELDS,
    ResolutionOutcome,
    ResolverState,
)
from engram.coordination import AtomicMutationCoordinator
from engram.core import Engram
from engram.fusion import CandidateFusionEngine, permissive_candidate_authority
from engram.graph import (
    proposition_projection,
    proposition_projection_from_graph_row,
)
from engram.graph_resolvers import StructuredGraphResolver, SupportSemanticResolver
from engram.identity import scope_key
from engram.repository import tier_admission_policy
from engram.resolution import (
    resolution_result_to_dict,
    resolution_result_to_json,
    resolver_result,
    validate_candidate,
)
from engram.resolution_accounting import ResolutionAccountingFinalizer
from engram.resolution_budget import capture_resolution_budget, validate_resolution_budget
from engram.resolution_evidence import (
    proposition_evidence_record_with_changes,
    validate_canonical_proposition_references,
    validate_disclosure_decision,
    validate_proposition_validity_inputs,
)
from engram.resolution_frame import QueryFrameBuilder, query_frame_with_changes
from engram.resolution_orchestrator import ResolutionOrchestrator
from engram.resolution_package import build_evidence_package, empty_evidence_package, evidence_package_to_json
from engram.resolver_execution import ResolverExecutor
from engram.resolver_plan import (
    ResolverRegistry,
    validate_resolver_budget,
)
from engram.responses import AcceptedResponseService
from tests.resolver_fixtures import (
    BY_ID_PROJECTION_CHANGES,
    NOW,
    SEMANTIC_PROJECTION_ROW,
    SPARSE_CANDIDATE,
    START_NS,
    STRUCTURED_PROJECTION_ROW,
    FakeResolver,
    enable_graph_resolvers,
)


def test_orchestrator_canonicalizes_cross_producer_proposition_without_candidacy_or_accounting(monkeypatch) -> None:
    engine = Engram()
    enable_graph_resolvers(engine, vector=True)
    engine.config.get("graph", {}).update({"enabled": True, "vector_enabled": True, "vector_weight": 1.0})
    structured = proposition_projection_from_graph_row(
        {**STRUCTURED_PROJECTION_ROW, "proposition_id": "proposition-shared"}, PropositionProjectionQuery.STRUCTURED_ENTITY
    )
    semantic = proposition_projection_from_graph_row(
        {**SEMANTIC_PROJECTION_ROW, "proposition_id": "proposition-shared", "semantic_similarity": 0.76},
        PropositionProjectionQuery.VECTOR,
        "proposition_premise_embeddings",
    )
    current = proposition_projection(**{**structured, **BY_ID_PROJECTION_CHANGES})
    monkeypatch.setattr(
        engine,
        "structured_proposition_projections",
        lambda internal_text, row_limit, cooperative_check=(), max_working_memory_bytes=0, *, basis_window: [structured][
            :row_limit
        ],
    )
    monkeypatch.setattr(engine, "graph_vector_propositions", lambda internal_text, *, limit=0, evaluation_time="": [])
    monkeypatch.setattr(
        engine,
        "graph_vector_proposition_projections",
        lambda internal_text, *, limit=0, cooperative_check=(), max_working_memory_bytes=0, basis_window: [semantic][:limit],
    )
    monkeypatch.setattr(engine, "current_proposition_projection", lambda internal_proposition_id, internal_basis_window: (current,))
    query_frame = QueryFrameBuilder(engine, lambda: START_NS, lambda: NOW).build(
        "Ada", EMPTY_SCOPE_KEY, diagnostic_seed="test:Ada:"
    )
    registry = ResolverRegistry(
        (
            StructuredGraphResolver(engine, lambda: START_NS),
            SupportSemanticResolver(engine, lambda: START_NS),
        )
    )

    report = ResolverExecutor(lambda: START_NS).execute(query_frame, registry.plan(query_frame))
    results = report.get("results", ())

    assert results
    assert all(set(result) == RESOLVER_RESULT_FIELDS for result in results)
    assert all(result.get("candidates", ()) == () for result in results)
    assert all(result.get("accounting", ()) == () for result in results)

    orchestrated, finalization = ResolutionOrchestrator(
        registry,
        ResolverExecutor(lambda: START_NS),
        ResolutionAccountingFinalizer(
            engine,
            AcceptedResponseService(
                AtomicMutationCoordinator(engine.response_repository, engine.mutation_receipts),
                tier_admission_policy(engine.config.get("capacity", 1)),
            ),
        ),
    ).resolve(query_frame, "request-cross-producer-package")
    records = orchestrated.get("evidence_package", {}).get("records", ())
    record_values = records[0].get("features", {}).get("values", {})

    assert set(orchestrated) == RESOLUTION_RESULT_FIELDS
    assert orchestrated.get("outcome", ResolutionOutcome.MISS) == ResolutionOutcome.EVIDENCE
    assert len(records) == 1
    assert records[0].get("source_contributions", ()) == (
        "structured_graph",
        "support_semantic",
    )
    assert record_values.get("structured_match", 0.0) == 1.0
    assert record_values.get("semantic_similarity", 0.0) == pytest_approx(0.76)
    assert record_values.get("source_agreement", 0.0) == 1.0
    assert all(not result.get("proposition_evidence", ()) for result in orchestrated.get("resolver_results", ()))
    assert set(finalization) == ACCOUNTING_FINALIZATION_FIELDS
    assert finalization.get("candidate_statement_ids", ()) == ()


def test_orchestrator_emits_only_bounded_package_for_proposition_only_evidence(monkeypatch) -> None:
    engine = Engram()
    enable_graph_resolvers(engine)
    discovered = proposition_projection_from_graph_row(
        {**STRUCTURED_PROJECTION_ROW, "proposition_id": "proposition-orchestrated"}, PropositionProjectionQuery.STRUCTURED_ENTITY
    )
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
    orchestrator = ResolutionOrchestrator(
        ResolverRegistry((StructuredGraphResolver(engine, lambda: START_NS),)),
        ResolverExecutor(lambda: START_NS),
        ResolutionAccountingFinalizer(
            engine,
            AcceptedResponseService(
                AtomicMutationCoordinator(engine.response_repository, engine.mutation_receipts),
                tier_admission_policy(engine.config.get("capacity", 1)),
            ),
        ),
    )

    result, finalization = orchestrator.resolve(query_frame, "request-proposition-only")
    package = result.get("evidence_package", {})
    budget = result.get("budget", {})
    frame_budget = query_frame.get("budget", {})

    assert set(result) == RESOLUTION_RESULT_FIELDS
    assert set(budget) == BUDGET_CONSUMPTION_FIELDS
    assert set(finalization) == ACCOUNTING_FINALIZATION_FIELDS
    assert result.get("outcome", ResolutionOutcome.MISS) == ResolutionOutcome.EVIDENCE
    assert result.get("selected_candidate_available", False) is False
    assert result.get("response_candidates", ()) == ()
    assert result.get("evidence", ()) == ()
    assert result.get("evidence_package_available", False) is True
    assert tuple(record.get("proposition_id", "") for record in package.get("records", ())) == ("proposition-orchestrated",)
    assert all(not value.get("proposition_evidence", ()) for value in result.get("resolver_results", ()))
    assert budget.get("evidence", 0) == 1
    assert budget.get("evidence_bytes", 0) == len(evidence_package_to_json(package).encode("utf-8"))
    assert budget.get("evidence_bytes", 0) <= frame_budget.get("max_evidence_bytes", 0)
    assert budget.get("graph_rows", 0) == 2
    assert budget.get("vector_results", 0) == 0
    assert budget.get("output_bytes", 0) == len(resolution_result_to_json(result).encode("utf-8"))
    assert budget.get("diagnostic_bytes", 0) <= frame_budget.get("max_diagnostic_bytes", 0)
    assert budget.get("working_memory_bytes", 0) <= frame_budget.get("max_working_memory_bytes", 0)
    proposition_diagnostics = result.get("frame_diagnostics", {}).get("proposition_evidence", {})
    assert isinstance(proposition_diagnostics, Mapping)
    assert set(proposition_diagnostics) == set(
        {
            "available",
            "input_count",
            "normalized_count",
            "included_count",
            "excluded_count",
            "reason_counts",
            "retained_count",
            "omitted_count",
            "truncated",
        }
    )
    diagnostic_payload = json_dumps(resolution_result_to_dict(result).get("frame_diagnostics", {}), sort_keys=True)
    assert "proposition-orchestrated" not in diagnostic_payload
    assert "entity:ada" not in diagnostic_payload
    assert "entity:engine" not in diagnostic_payload
    assert finalization.get("candidate_statement_ids", ()) == ()
    assert finalization.get("accepted_statement_id", "") == ""
    assert finalization.get("success_applied", False) is False


def test_orchestrator_keeps_miss_when_proposition_fails_usefulness_policy(monkeypatch) -> None:
    engine = Engram()
    enable_graph_resolvers(engine, vector=True)
    engine.config.get("graph", {}).update({"enabled": True, "vector_enabled": True, "vector_weight": 1.0})
    discovered = proposition_projection_from_graph_row(
        {**SEMANTIC_PROJECTION_ROW, "proposition_id": "proposition-below-floor", "semantic_similarity": 0.59},
        PropositionProjectionQuery.VECTOR,
        "proposition_premise_embeddings",
    )
    current = proposition_projection(**{**discovered, **BY_ID_PROJECTION_CHANGES})
    monkeypatch.setattr(engine, "graph_vector_propositions", lambda internal_text, *, limit=0, evaluation_time="": [])
    monkeypatch.setattr(
        engine,
        "graph_vector_proposition_projections",
        lambda internal_text, *, limit=0, cooperative_check=(), max_working_memory_bytes=0, basis_window: [discovered][:limit],
    )
    monkeypatch.setattr(engine, "current_proposition_projection", lambda internal_proposition_id, internal_basis_window: (current,))
    query_frame = QueryFrameBuilder(engine, lambda: START_NS, lambda: NOW).build(
        "Ada", EMPTY_SCOPE_KEY, diagnostic_seed="test:Ada:"
    )
    orchestrator = ResolutionOrchestrator(
        ResolverRegistry((SupportSemanticResolver(engine, lambda: START_NS),)),
        ResolverExecutor(lambda: START_NS),
        ResolutionAccountingFinalizer(
            engine,
            AcceptedResponseService(
                AtomicMutationCoordinator(engine.response_repository, engine.mutation_receipts),
                tier_admission_policy(engine.config.get("capacity", 1)),
            ),
        ),
    )

    result, finalization = orchestrator.resolve(query_frame, "request-proposition-excluded")
    package = result.get("evidence_package", {})
    budget = result.get("budget", {})

    assert set(result) == RESOLUTION_RESULT_FIELDS
    assert set(package) == EVIDENCE_PACKAGE_FIELDS
    assert set(budget) == BUDGET_CONSUMPTION_FIELDS
    assert set(finalization) == ACCOUNTING_FINALIZATION_FIELDS
    assert result.get("outcome", ResolutionOutcome.MISS) == ResolutionOutcome.MISS
    assert result.get("evidence_package_available", False) is True
    assert package.get("records", ()) == ()
    assert "proposition_evidence_excluded" in result.get("reason_codes", ())
    diagnostics = result.get("frame_diagnostics", {}).get("proposition_evidence", {})
    assert isinstance(diagnostics, Mapping)
    assert diagnostics.keys() >= {"input_count", "normalized_count", "included_count", "excluded_count"}
    assert diagnostics.get("input_count", 0) == 1
    assert diagnostics.get("normalized_count", 0) == 1
    assert diagnostics.get("included_count", 0) == 0
    assert diagnostics.get("excluded_count", 0) == 1
    assert diagnostics.get("reason_counts", {}) == {
        "retrieval_signal_below_floor": 1,
        "supplied_trust_unavailable": 1,
    }
    assert all(not value.get("proposition_evidence", ()) for value in result.get("resolver_results", ()))
    assert budget.get("evidence", 0) == 0
    assert budget.get("evidence_bytes", 0) == len(evidence_package_to_json(package).encode("utf-8"))
    assert budget.get("graph_rows", 0) == 1
    assert budget.get("vector_results", 0) == 1
    assert finalization.get("candidate_statement_ids", ()) == ()
    assert finalization.get("success_applied", False) is False


def test_orchestrator_retains_response_candidate_evidence_when_proposition_is_excluded(monkeypatch) -> None:
    engine = Engram()
    enable_graph_resolvers(engine, vector=True)
    statement_id = engine.store("Candidate response")
    engine.config.get("graph", {}).update({"enabled": True, "vector_enabled": True, "vector_weight": 1.0})
    discovered = proposition_projection_from_graph_row(
        {**SEMANTIC_PROJECTION_ROW, "proposition_id": "proposition-below-floor-with-candidate", "semantic_similarity": 0.59},
        PropositionProjectionQuery.VECTOR,
        "proposition_premise_embeddings",
    )
    current = proposition_projection(**{**discovered, **BY_ID_PROJECTION_CHANGES})
    monkeypatch.setattr(engine, "graph_vector_propositions", lambda internal_text, *, limit=0, evaluation_time="": [])
    monkeypatch.setattr(
        engine,
        "graph_vector_proposition_projections",
        lambda internal_text, *, limit=0, cooperative_check=(), max_working_memory_bytes=0, basis_window: [discovered][:limit],
    )
    monkeypatch.setattr(engine, "current_proposition_projection", lambda internal_proposition_id, internal_basis_window: (current,))
    sparse_candidate = validate_candidate(
        {**SPARSE_CANDIDATE, "candidate_id": f"candidate:sparse:{statement_id}", "statement_id": statement_id}
    )
    sparse = FakeResolver(
        "sparse",
        resolver_result(
            "sparse",
            ResolverState.COMPLETED,
            candidates=(sparse_candidate,),
        ),
    )
    query_frame = QueryFrameBuilder(engine, lambda: START_NS, lambda: NOW).build(
        "Ada", EMPTY_SCOPE_KEY, diagnostic_seed="test:Ada:"
    )
    orchestrator = ResolutionOrchestrator(
        ResolverRegistry((sparse, SupportSemanticResolver(engine, lambda: START_NS))),
        ResolverExecutor(lambda: START_NS),
        ResolutionAccountingFinalizer(
            engine,
            AcceptedResponseService(
                AtomicMutationCoordinator(engine.response_repository, engine.mutation_receipts),
                tier_admission_policy(engine.config.get("capacity", 1)),
            ),
        ),
        CandidateFusionEngine(authority=permissive_candidate_authority),
    )

    result, finalization = orchestrator.resolve(query_frame, "request-candidate-plus-excluded-proposition")
    package = result.get("evidence_package", {})

    assert set(result) == RESOLUTION_RESULT_FIELDS
    assert set(package) == EVIDENCE_PACKAGE_FIELDS
    assert set(finalization) == ACCOUNTING_FINALIZATION_FIELDS
    assert result.get("outcome", ResolutionOutcome.MISS) == ResolutionOutcome.EVIDENCE
    assert tuple(value.get("statement_id", "") for value in result.get("response_candidates", ())) == (statement_id,)
    assert result.get("evidence_package_available", False) is True
    assert package.get("records", ()) == ()
    assert "proposition_evidence_excluded" in result.get("reason_codes", ())
    assert finalization.get("candidate_statement_ids", ()) == ()
    assert finalization.get("success_applied", False) is False


def test_orchestrator_canonically_truncates_proposition_package_to_ten_records(monkeypatch) -> None:
    engine = Engram()
    enable_graph_resolvers(engine)
    discovered = tuple(
        proposition_projection_from_graph_row(
            {**STRUCTURED_PROJECTION_ROW, "proposition_id": f"proposition-{index:02d}"},
            PropositionProjectionQuery.STRUCTURED_ENTITY,
        )
        for index in range(12)
    )
    current = {
        projection.get("proposition_id", ""): proposition_projection(**{**projection, **BY_ID_PROJECTION_CHANGES})
        for projection in discovered
    }
    monkeypatch.setattr(
        engine,
        "structured_proposition_projections",
        lambda internal_text, row_limit, cooperative_check=(), max_working_memory_bytes=0, *, basis_window: list(
            discovered[:row_limit]
        ),
    )
    monkeypatch.setattr(
        engine, "current_proposition_projection", lambda proposition_id, internal_basis_window: (current.get(proposition_id, {}),)
    )
    query_frame = QueryFrameBuilder(engine, lambda: START_NS, lambda: NOW).build(
        "Ada", EMPTY_SCOPE_KEY, diagnostic_seed="test:Ada:"
    )
    orchestrator = ResolutionOrchestrator(
        ResolverRegistry((StructuredGraphResolver(engine, lambda: START_NS),)),
        ResolverExecutor(lambda: START_NS),
        ResolutionAccountingFinalizer(
            engine,
            AcceptedResponseService(
                AtomicMutationCoordinator(engine.response_repository, engine.mutation_receipts),
                tier_admission_policy(engine.config.get("capacity", 1)),
            ),
        ),
    )

    result, finalization = orchestrator.resolve(query_frame, "request-proposition-count-limit")
    package = result.get("evidence_package", {})
    budget = result.get("budget", {})

    assert set(finalization) == ACCOUNTING_FINALIZATION_FIELDS
    assert result.get("outcome", ResolutionOutcome.MISS) == ResolutionOutcome.EVIDENCE
    assert tuple(record.get("proposition_id", "") for record in package.get("records", ())) == tuple(
        f"proposition-{index:02d}" for index in range(10)
    )
    assert package.get("retained_count", 0) == 10
    assert package.get("omitted_count", 0) == 2
    assert package.get("truncated", False) is True
    assert package.get("truncation_reasons", ()) == (EvidencePackageTruncationReason.RECORD_LIMIT,)
    assert budget.get("evidence", 0) == 10
    assert budget.get("evidence_bytes", 0) == len(evidence_package_to_json(package).encode("utf-8"))
    assert budget.get("output_bytes", 0) == len(resolution_result_to_json(result).encode("utf-8"))
    assert finalization.get("candidate_statement_ids", ()) == ()


def test_orchestrator_trims_proposition_package_to_complete_output_budget(monkeypatch) -> None:
    engine = Engram()
    enable_graph_resolvers(engine)
    discovered = tuple(
        proposition_projection_from_graph_row(
            {**STRUCTURED_PROJECTION_ROW, "proposition_id": f"proposition-output-{index:02d}"},
            PropositionProjectionQuery.STRUCTURED_ENTITY,
        )
        for index in range(4)
    )
    current = {
        projection.get("proposition_id", ""): proposition_projection(**{**projection, **BY_ID_PROJECTION_CHANGES})
        for projection in discovered
    }
    monkeypatch.setattr(
        engine,
        "structured_proposition_projections",
        lambda internal_text, row_limit, cooperative_check=(), max_working_memory_bytes=0, *, basis_window: list(
            discovered[:row_limit]
        ),
    )
    monkeypatch.setattr(
        engine, "current_proposition_projection", lambda proposition_id, internal_basis_window: (current.get(proposition_id, {}),)
    )
    selected_budget = capture_resolution_budget(
        lambda: START_NS,
        max_output_bytes=4_096,
    )
    query_frame = QueryFrameBuilder(engine, lambda: START_NS, lambda: NOW).build(
        "Ada", EMPTY_SCOPE_KEY, diagnostic_seed="test:Ada:", budget=selected_budget
    )
    orchestrator = ResolutionOrchestrator(
        ResolverRegistry((StructuredGraphResolver(engine, lambda: START_NS),)),
        ResolverExecutor(lambda: START_NS),
        ResolutionAccountingFinalizer(
            engine,
            AcceptedResponseService(
                AtomicMutationCoordinator(engine.response_repository, engine.mutation_receipts),
                tier_admission_policy(engine.config.get("capacity", 1)),
            ),
        ),
    )

    result, finalization = orchestrator.resolve(query_frame, "request-proposition-output-limit")
    package = result.get("evidence_package", {})
    budget = result.get("budget", {})

    assert set(result) == RESOLUTION_RESULT_FIELDS
    assert set(finalization) == ACCOUNTING_FINALIZATION_FIELDS
    assert result.get("outcome", ResolutionOutcome.MISS) == ResolutionOutcome.EVIDENCE
    assert 0 < package.get("retained_count", 0) < len(discovered)
    assert package.get("truncated", False) is True
    assert "output_truncated" in result.get("reason_codes", ())
    assert "output_bytes" in budget.get("exhausted_dimensions", ())
    assert budget.get("output_bytes", 0) == len(resolution_result_to_json(result).encode("utf-8"))
    assert budget.get("output_bytes", 0) <= selected_budget.get("max_output_bytes", 0)
    assert all(not value.get("proposition_evidence", ()) for value in result.get("resolver_results", ()))
    assert finalization.get("candidate_statement_ids", ()) == ()


def test_orchestrator_fits_package_to_aggregate_evidence_byte_budget(monkeypatch) -> None:
    engine = Engram()
    enable_graph_resolvers(engine)
    probe_projection = proposition_projection_from_graph_row(
        {**STRUCTURED_PROJECTION_ROW, "proposition_id": "proposition-byte-00"}, PropositionProjectionQuery.STRUCTURED_ENTITY
    )
    probe_current = proposition_projection(**{**probe_projection, **BY_ID_PROJECTION_CHANGES})
    monkeypatch.setattr(
        engine,
        "structured_proposition_projections",
        lambda internal_text, row_limit, cooperative_check=(), max_working_memory_bytes=0, *, basis_window: [probe_projection][
            :row_limit
        ],
    )
    monkeypatch.setattr(
        engine,
        "current_proposition_projection",
        lambda internal_proposition_id, internal_basis_window: (probe_current,),
    )
    probe_frame = QueryFrameBuilder(engine, lambda: START_NS, lambda: NOW).build(
        "Ada", EMPTY_SCOPE_KEY, diagnostic_seed="test:Ada:"
    )
    probe_result = StructuredGraphResolver(engine, lambda: START_NS).resolve(
        probe_frame,
        validate_resolver_budget({name: probe_frame.get("budget", {}).get(name, 0) for name in RESOLVER_BUDGET_FIELDS}),
    )
    probe_record = probe_result.get("proposition_evidence", ())[0]
    single_package_bytes = len(evidence_package_to_json(build_evidence_package((probe_record,))).encode("utf-8"))

    discovered = tuple(
        proposition_projection_from_graph_row(
            {**STRUCTURED_PROJECTION_ROW, "proposition_id": f"proposition-byte-{index:02d}"},
            PropositionProjectionQuery.STRUCTURED_ENTITY,
        )
        for index in range(2)
    )
    current = {
        projection.get("proposition_id", ""): proposition_projection(**{**projection, **BY_ID_PROJECTION_CHANGES})
        for projection in discovered
    }
    monkeypatch.setattr(
        engine,
        "structured_proposition_projections",
        lambda internal_text, row_limit, cooperative_check=(), max_working_memory_bytes=0, *, basis_window: list(
            discovered[:row_limit]
        ),
    )
    monkeypatch.setattr(
        engine, "current_proposition_projection", lambda proposition_id, internal_basis_window: (current.get(proposition_id, {}),)
    )
    selected_budget = capture_resolution_budget(
        lambda: START_NS,
        max_evidence_bytes=single_package_bytes,
    )
    query_frame = QueryFrameBuilder(engine, lambda: START_NS, lambda: NOW).build(
        "Ada", EMPTY_SCOPE_KEY, diagnostic_seed="test:Ada:", budget=selected_budget
    )
    orchestrator = ResolutionOrchestrator(
        ResolverRegistry((StructuredGraphResolver(engine, lambda: START_NS),)),
        ResolverExecutor(lambda: START_NS),
        ResolutionAccountingFinalizer(
            engine,
            AcceptedResponseService(
                AtomicMutationCoordinator(engine.response_repository, engine.mutation_receipts),
                tier_admission_policy(engine.config.get("capacity", 1)),
            ),
        ),
    )

    result, finalization = orchestrator.resolve(query_frame, "request-proposition-evidence-byte-limit")
    package = result.get("evidence_package", {})
    budget = result.get("budget", {})

    assert set(finalization) == ACCOUNTING_FINALIZATION_FIELDS
    assert result.get("outcome", ResolutionOutcome.MISS) == ResolutionOutcome.EVIDENCE
    assert tuple(record.get("proposition_id", "") for record in package.get("records", ())) == ("proposition-byte-00",)
    assert budget.get("evidence", 0) == 1
    assert budget.get("evidence_bytes", 0) == len(evidence_package_to_json(package).encode("utf-8"))
    assert budget.get("evidence_bytes", 0) <= single_package_bytes
    assert "evidence_bytes" in budget.get("exhausted_dimensions", ())
    assert finalization.get("candidate_statement_ids", ()) == ()


def test_orchestrator_omits_diagnostics_without_losing_proposition_package(monkeypatch) -> None:
    engine = Engram()
    enable_graph_resolvers(engine)
    discovered = proposition_projection_from_graph_row(
        {**STRUCTURED_PROJECTION_ROW, "proposition_id": "proposition-no-diagnostics"}, PropositionProjectionQuery.STRUCTURED_ENTITY
    )
    current = proposition_projection(**{**discovered, **BY_ID_PROJECTION_CHANGES})
    monkeypatch.setattr(
        engine,
        "structured_proposition_projections",
        lambda internal_text, row_limit, cooperative_check=(), max_working_memory_bytes=0, *, basis_window: [discovered][
            :row_limit
        ],
    )
    monkeypatch.setattr(engine, "current_proposition_projection", lambda internal_proposition_id, internal_basis_window: (current,))
    selected_budget = capture_resolution_budget(
        lambda: START_NS,
        max_diagnostic_bytes=0,
    )
    query_frame = QueryFrameBuilder(engine, lambda: START_NS, lambda: NOW).build(
        "Ada", EMPTY_SCOPE_KEY, diagnostic_seed="test:Ada:", budget=selected_budget
    )
    orchestrator = ResolutionOrchestrator(
        ResolverRegistry((StructuredGraphResolver(engine, lambda: START_NS),)),
        ResolverExecutor(lambda: START_NS),
        ResolutionAccountingFinalizer(
            engine,
            AcceptedResponseService(
                AtomicMutationCoordinator(engine.response_repository, engine.mutation_receipts),
                tier_admission_policy(engine.config.get("capacity", 1)),
            ),
        ),
    )

    result, finalization = orchestrator.resolve(query_frame, "request-proposition-diagnostic-limit")
    budget = result.get("budget", {})

    assert set(result) == RESOLUTION_RESULT_FIELDS
    assert set(budget) == BUDGET_CONSUMPTION_FIELDS
    assert set(finalization) == ACCOUNTING_FINALIZATION_FIELDS
    assert result.get("outcome", ResolutionOutcome.MISS) == ResolutionOutcome.EVIDENCE
    assert tuple(record.get("proposition_id", "") for record in result.get("evidence_package", {}).get("records", ())) == (
        "proposition-no-diagnostics",
    )
    assert result.get("frame_diagnostics", {}) == {}
    assert budget.get("diagnostic_bytes", 0) == 0
    assert "diagnostic_bytes" in budget.get("exhausted_dimensions", ())
    assert "diagnostics_truncated" in result.get("reason_codes", ())
    assert finalization.get("candidate_statement_ids", ()) == ()


def test_orchestrator_refuses_proposition_package_when_post_fusion_memory_is_exhausted(monkeypatch) -> None:
    engine = Engram()
    enable_graph_resolvers(engine)
    discovered = proposition_projection_from_graph_row(
        {**STRUCTURED_PROJECTION_ROW, "proposition_id": "proposition-memory-bound"}, PropositionProjectionQuery.STRUCTURED_ENTITY
    )
    current = proposition_projection(**{**discovered, **BY_ID_PROJECTION_CHANGES})
    monkeypatch.setattr(
        engine,
        "structured_proposition_projections",
        lambda internal_text, row_limit, cooperative_check=(), max_working_memory_bytes=0, *, basis_window: [discovered][
            :row_limit
        ],
    )
    monkeypatch.setattr(engine, "current_proposition_projection", lambda internal_proposition_id, internal_basis_window: (current,))
    base_frame = QueryFrameBuilder(engine, lambda: START_NS, lambda: NOW).build("Ada", EMPTY_SCOPE_KEY, diagnostic_seed="test:Ada:")
    base_budget = base_frame.get("budget", {})
    registry = ResolverRegistry((StructuredGraphResolver(engine, lambda: START_NS),))
    executor = ResolverExecutor(lambda: START_NS)
    probe_execution = executor.execute(base_frame, registry.plan(base_frame))
    probe_memory_bytes = probe_execution.get("consumption", {}).get("working_memory_bytes", 0)
    assert probe_memory_bytes > 0
    fusion = CandidateFusionEngine(authority=permissive_candidate_authority)
    fusion_required = fusion.decide(
        base_frame,
        (),
        (),
        working_memory_limit=base_budget.get("max_working_memory_bytes", 0) - probe_memory_bytes,
        working_memory_limit_available=True,
    ).get("working_memory_bytes", 0)
    memory_limit = probe_memory_bytes + fusion_required
    constrained_frame = query_frame_with_changes(
        base_frame,
        {"budget": validate_resolution_budget({**base_budget, "max_working_memory_bytes": memory_limit})},
    )
    orchestrator = ResolutionOrchestrator(
        registry,
        executor,
        ResolutionAccountingFinalizer(
            engine,
            AcceptedResponseService(
                AtomicMutationCoordinator(engine.response_repository, engine.mutation_receipts),
                tier_admission_policy(engine.config.get("capacity", 1)),
            ),
        ),
        fusion,
    )

    result, finalization = orchestrator.resolve(constrained_frame, "request-proposition-memory-limit")
    package = result.get("evidence_package", {})
    budget = result.get("budget", {})

    assert set(result) == RESOLUTION_RESULT_FIELDS
    assert set(package) == EVIDENCE_PACKAGE_FIELDS
    assert set(finalization) == ACCOUNTING_FINALIZATION_FIELDS
    assert result.get("outcome", ResolutionOutcome.MISS) == ResolutionOutcome.MISS
    assert result.get("evidence_package_available", False) is False
    assert package.get("records", ()) == ()
    assert "proposition_evidence_memory_exhausted" in result.get("reason_codes", ())
    assert "working_memory_bytes" in budget.get("exhausted_dimensions", ())
    assert budget.get("working_memory_bytes", 0) == memory_limit
    assert all(not value.get("proposition_evidence", ()) for value in result.get("resolver_results", ()))
    assert finalization.get("candidate_statement_ids", ()) == ()


def test_orchestrator_rejects_cross_producer_proposition_conflict_without_leaking_records(monkeypatch) -> None:
    engine = Engram()
    discovered = proposition_projection_from_graph_row(
        {**STRUCTURED_PROJECTION_ROW, "proposition_id": "proposition-conflict"}, PropositionProjectionQuery.STRUCTURED_ENTITY
    )
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
    resolver_output = StructuredGraphResolver(engine, lambda: START_NS).resolve(
        query_frame,
        validate_resolver_budget({name: query_frame.get("budget", {}).get(name, 0) for name in RESOLVER_BUDGET_FIELDS}),
    )
    record = resolver_output.get("proposition_evidence", ())[0]
    conflicting = proposition_evidence_record_with_changes(
        record,
        {
            "source_resolver": "support_semantic",
            "source_contributions": ("support_semantic",),
            "canonical_references": validate_canonical_proposition_references(
                {**record.get("canonical_references", {}), "object_entity_id": "entity:conflict"}
            ),
        },
    )
    structured = FakeResolver(
        "structured_graph",
        resolver_result("structured_graph", ResolverState.COMPLETED, proposition_evidence=(record,)),
    )
    semantic = FakeResolver(
        "support_semantic",
        resolver_result("support_semantic", ResolverState.COMPLETED, proposition_evidence=(conflicting,)),
    )
    orchestrator = ResolutionOrchestrator(
        ResolverRegistry((structured, semantic)),
        ResolverExecutor(lambda: START_NS),
        ResolutionAccountingFinalizer(
            engine,
            AcceptedResponseService(
                AtomicMutationCoordinator(engine.response_repository, engine.mutation_receipts),
                tier_admission_policy(engine.config.get("capacity", 1)),
            ),
        ),
    )

    result, finalization = orchestrator.resolve(query_frame, "request-proposition-conflict")
    package = result.get("evidence_package", {})

    assert set(result) == RESOLUTION_RESULT_FIELDS
    assert set(package) == EVIDENCE_PACKAGE_FIELDS
    assert set(finalization) == ACCOUNTING_FINALIZATION_FIELDS
    assert result.get("outcome", ResolutionOutcome.MISS) == ResolutionOutcome.MISS
    assert result.get("evidence_package_available", False) is False
    assert package.get("records", ()) == ()
    assert "proposition_evidence_conflict" in result.get("reason_codes", ())
    assert all(not value.get("proposition_evidence", ()) for value in result.get("resolver_results", ()))
    assert finalization.get("candidate_statement_ids", ()) == ()


@pytest_mark.parametrize("mismatch", ("scope", "evaluation_time"))
def test_orchestrator_rejects_proposition_not_bound_to_current_frame(monkeypatch, mismatch: str) -> None:
    engine = Engram()
    discovered = proposition_projection_from_graph_row(
        {**STRUCTURED_PROJECTION_ROW, "proposition_id": f"proposition-{mismatch}-mismatch"},
        PropositionProjectionQuery.STRUCTURED_ENTITY,
    )
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
    resolver_output = StructuredGraphResolver(engine, lambda: START_NS).resolve(
        query_frame,
        validate_resolver_budget({name: query_frame.get("budget", {}).get(name, 0) for name in RESOLVER_BUDGET_FIELDS}),
    )
    record = resolver_output.get("proposition_evidence", ())[0]
    if mismatch == "scope":
        record = proposition_evidence_record_with_changes(
            record,
            {"disclosure": validate_disclosure_decision({**record.get("disclosure", {}), "scope": scope_key(namespace="other")})},
        )
    else:
        record = proposition_evidence_record_with_changes(
            record,
            {
                "validity": validate_proposition_validity_inputs(
                    {**record.get("validity", {}), "evaluation_time": "2026-08-17T12:00:00Z"}
                )
            },
        )
    resolver = FakeResolver(
        "structured_graph",
        resolver_result("structured_graph", ResolverState.COMPLETED, proposition_evidence=(record,)),
    )
    orchestrator = ResolutionOrchestrator(
        ResolverRegistry((resolver,)),
        ResolverExecutor(lambda: START_NS),
        ResolutionAccountingFinalizer(
            engine,
            AcceptedResponseService(
                AtomicMutationCoordinator(engine.response_repository, engine.mutation_receipts),
                tier_admission_policy(engine.config.get("capacity", 1)),
            ),
        ),
    )

    result, finalization = orchestrator.resolve(query_frame, f"request-{mismatch}-mismatch")
    package = result.get("evidence_package", {})

    assert set(result) == RESOLUTION_RESULT_FIELDS
    assert set(package) == EVIDENCE_PACKAGE_FIELDS
    assert set(finalization) == ACCOUNTING_FINALIZATION_FIELDS
    assert result.get("outcome", ResolutionOutcome.MISS) == ResolutionOutcome.MISS
    assert result.get("evidence_package_available", False) is False
    assert package.get("records", ()) == ()
    assert "proposition_evidence_conflict" in result.get("reason_codes", ())
    assert all(not value.get("proposition_evidence", ()) for value in result.get("resolver_results", ()))
    assert finalization.get("candidate_statement_ids", ()) == ()


def test_orchestrator_ignores_proposition_from_untrusted_producer(monkeypatch) -> None:
    engine = Engram()
    discovered = proposition_projection_from_graph_row(
        {**STRUCTURED_PROJECTION_ROW, "proposition_id": "proposition-untrusted-producer"},
        PropositionProjectionQuery.STRUCTURED_ENTITY,
    )
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
    resolver_output = StructuredGraphResolver(engine, lambda: START_NS).resolve(
        query_frame,
        validate_resolver_budget({name: query_frame.get("budget", {}).get(name, 0) for name in RESOLVER_BUDGET_FIELDS}),
    )
    trusted_record = resolver_output.get("proposition_evidence", ())[0]
    untrusted_record = proposition_evidence_record_with_changes(
        trusted_record,
        {"source_resolver": "untrusted", "source_contributions": ("untrusted",)},
    )
    resolver = FakeResolver(
        "untrusted",
        resolver_result("untrusted", ResolverState.COMPLETED, proposition_evidence=(untrusted_record,)),
    )
    orchestrator = ResolutionOrchestrator(
        ResolverRegistry((resolver,)),
        ResolverExecutor(lambda: START_NS),
        ResolutionAccountingFinalizer(
            engine,
            AcceptedResponseService(
                AtomicMutationCoordinator(engine.response_repository, engine.mutation_receipts),
                tier_admission_policy(engine.config.get("capacity", 1)),
            ),
        ),
    )

    result, finalization = orchestrator.resolve(query_frame, "request-untrusted-proposition-producer")
    package = result.get("evidence_package", {})

    assert set(result) == RESOLUTION_RESULT_FIELDS
    assert set(package) == EVIDENCE_PACKAGE_FIELDS
    assert set(finalization) == ACCOUNTING_FINALIZATION_FIELDS
    assert result.get("outcome", ResolutionOutcome.MISS) == ResolutionOutcome.MISS
    assert result.get("evidence_package_available", False) is False
    assert package.get("records", ()) == ()
    assert "proposition_evidence_untrusted_producer" in result.get("reason_codes", ())
    assert all(not value.get("proposition_evidence", ()) for value in result.get("resolver_results", ()))
    assert finalization.get("candidate_statement_ids", ()) == ()


def test_orchestrator_fails_soft_when_proposition_producer_dependency_fails() -> None:
    engine = Engram()
    query_frame = QueryFrameBuilder(engine, lambda: START_NS, lambda: NOW).build(
        "Ada", EMPTY_SCOPE_KEY, diagnostic_seed="test:Ada:"
    )
    failed = FakeResolver(
        "structured_graph",
        resolver_result("structured_graph", ResolverState.COMPLETED),
        error=True,
    )
    orchestrator = ResolutionOrchestrator(
        ResolverRegistry((failed,)),
        ResolverExecutor(lambda: START_NS),
        ResolutionAccountingFinalizer(
            engine,
            AcceptedResponseService(
                AtomicMutationCoordinator(engine.response_repository, engine.mutation_receipts),
                tier_admission_policy(engine.config.get("capacity", 1)),
            ),
        ),
    )

    result, finalization = orchestrator.resolve(query_frame, "request-proposition-dependency-failure")
    package = result.get("evidence_package", {})

    assert set(result) == RESOLUTION_RESULT_FIELDS
    assert set(package) == EVIDENCE_PACKAGE_FIELDS
    assert set(finalization) == ACCOUNTING_FINALIZATION_FIELDS
    assert result.get("outcome", ResolutionOutcome.MISS) == ResolutionOutcome.MISS
    assert result.get("evidence_package_available", False) is False
    assert package.get("records", ()) == ()
    assert all(set(value) == RESOLVER_RESULT_FIELDS for value in result.get("resolver_results", ()))
    assert tuple(value.get("state", ResolverState.FAILED) for value in result.get("resolver_results", ())) == (
        ResolverState.FAILED,
    )
    assert finalization.get("candidate_statement_ids", ()) == ()
    assert finalization.get("success_applied", False) is False


def test_orchestrator_keeps_package_unavailable_when_producer_has_no_strict_records() -> None:
    engine = Engram()
    query_frame = QueryFrameBuilder(engine, lambda: START_NS, lambda: NOW).build(
        "Ada", EMPTY_SCOPE_KEY, diagnostic_seed="test:Ada:"
    )
    empty = FakeResolver(
        "structured_graph",
        resolver_result("structured_graph", ResolverState.COMPLETED, reason_code="structured_graph_miss"),
    )
    orchestrator = ResolutionOrchestrator(
        ResolverRegistry((empty,)),
        ResolverExecutor(lambda: START_NS),
        ResolutionAccountingFinalizer(
            engine,
            AcceptedResponseService(
                AtomicMutationCoordinator(engine.response_repository, engine.mutation_receipts),
                tier_admission_policy(engine.config.get("capacity", 1)),
            ),
        ),
    )

    result, finalization = orchestrator.resolve(query_frame, "request-empty-proposition-producer")

    assert set(result) == RESOLUTION_RESULT_FIELDS
    assert set(finalization) == ACCOUNTING_FINALIZATION_FIELDS
    assert result.get("outcome", ResolutionOutcome.MISS) == ResolutionOutcome.MISS
    assert result.get("evidence_package_available", False) is False
    assert result.get("evidence_package", {}) == empty_evidence_package()
    assert finalization.get("candidate_statement_ids", ()) == ()
