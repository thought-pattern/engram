"""Section 4 resolver adapter conformance: exact, structured graph, and support-linked semantic resolvers."""

from pytest import approx as pytest_approx, raises as pytest_raises

from engram.artifacts import validate_cached_response_artifact
from engram.constants_artifacts import LifecycleState
from engram.constants_graph import PROPOSITION_PROJECTION_FIELDS, PropositionProjectionQuery
from engram.constants_identity import EMPTY_SCOPE_KEY
from engram.constants_resolution import RESOLVER_BUDGET_FIELDS, CandidateSource, ResolverState
from engram.core import Engram
from engram.errors import InvalidRequestError
from engram.graph import (
    MemGraphConnection,
    proposition_projection,
    proposition_projection_from_graph_row,
    proposition_projection_to_dict,
)
from engram.graph_resolvers import StructuredGraphResolver, SupportSemanticResolver
from engram.identity import extract_standalone_identity
from engram.repository import ArtifactRepository
from engram.resolution import (
    accounting_observation,
    resolver_result_from_dict,
    resolver_result_to_dict,
)
from engram.resolution_evidence import (
    proposition_evidence_record_to_dict,
)
from engram.resolution_frame import QueryFrameBuilder, query_frame_with_changes
from engram.resolver_execution import ResolverExecutor
from engram.resolver_plan import (
    ResolverRegistry,
    resolver_budget_with_changes,
    validate_resolver_budget,
)
from engram.resolvers import ExactResolver
from tests.resolver_fixtures import (
    ACCEPTED_ARTIFACT,
    BY_ID_PROJECTION_CHANGES,
    NOW,
    SEMANTIC_PROJECTION_ROW,
    START_NS,
    STRUCTURED_PROJECTION_ROW,
    TENANT_A_SCOPE,
    TENANT_B_SCOPE,
    enable_graph_resolvers,
)
from tests.support_fixtures import PROPOSITION_REFERENCE_A, PROPOSITION_REFERENCE_B, REFERENCE_IDS


def test_exact_adapter_preserves_alias_origin_text_and_hard_filters() -> None:
    engine = Engram()
    engine.response_repository = ArtifactRepository((ACCEPTED_ARTIFACT,))
    query_frame = QueryFrameBuilder(engine, lambda: START_NS, lambda: NOW).build(
        "Explain Engram",
        TENANT_A_SCOPE,
        required_metadata={"approved": True},
        required_source_label="released",
        diagnostic_seed="test:Explain Engram:tenant-a",
    )
    lease = validate_resolver_budget({name: query_frame.get("budget", {}).get(name, 0) for name in RESOLVER_BUDGET_FIELDS})

    result = ExactResolver(engine, lambda: START_NS).resolve(query_frame, lease)
    found = result.get("candidates", ())[0]

    assert result.get("state", ResolverState.FAILED) == ResolverState.COMPLETED
    assert result.get("reason_code", "") == "exact_found"
    assert found.get("response", "") == "Engram preserves exact text: café ☕."
    assert found.get("provenance", {}).get("retrieval_origin", "") == "alias"
    assert result.get("accounting", ()) == (accounting_observation("stmt-accepted"),)

    excluded = ExactResolver(engine, lambda: START_NS).resolve(
        query_frame_with_changes(query_frame, {"required_metadata": {"approved": False}}),
        lease,
    )
    assert "candidates" in excluded
    assert excluded.get("candidates", ()) == ()
    assert excluded.get("reason_code", "") == "exact_required_filter_excluded"


def test_adapter_candidate_ids_are_stable_within_and_distinct_across_requests() -> None:
    engine = Engram()
    engine.response_repository = ArtifactRepository((ACCEPTED_ARTIFACT,))
    first_frame = QueryFrameBuilder(engine, lambda: START_NS, lambda: NOW).build(
        "Explain Engram", TENANT_A_SCOPE, diagnostic_seed="test:Explain Engram:tenant-a"
    )
    second_frame = query_frame_with_changes(first_frame, {"diagnostic_id": "resolution:sha256:" + "a" * 64})
    first_lease = validate_resolver_budget({name: first_frame.get("budget", {}).get(name, 0) for name in RESOLVER_BUDGET_FIELDS})
    second_lease = validate_resolver_budget({name: second_frame.get("budget", {}).get(name, 0) for name in RESOLVER_BUDGET_FIELDS})
    resolver = ExactResolver(engine, lambda: START_NS)

    first = resolver.resolve(first_frame, first_lease).get("candidates", ())[0]
    replay = resolver.resolve(first_frame, first_lease).get("candidates", ())[0]
    second = resolver.resolve(second_frame, second_lease).get("candidates", ())[0]
    first_id = first.get("candidate_id", "")

    assert first_id
    assert first_id == replay.get("candidate_id", "")
    assert first_id != second.get("candidate_id", "")


def test_exact_adapter_abstains_for_wrong_scope_and_ineligible_lifecycle() -> None:
    engine = Engram()
    engine.response_repository = ArtifactRepository(
        (validate_cached_response_artifact({**ACCEPTED_ARTIFACT, "lifecycle": LifecycleState.RETIRED}),)
    )
    retired_frame = QueryFrameBuilder(engine, lambda: START_NS, lambda: NOW).build(
        "What is Engram?", TENANT_A_SCOPE, diagnostic_seed="test:What is Engram?:tenant-a"
    )
    retired_lease = validate_resolver_budget(
        {name: retired_frame.get("budget", {}).get(name, 0) for name in RESOLVER_BUDGET_FIELDS}
    )

    retired_result = ExactResolver(engine, lambda: START_NS).resolve(retired_frame, retired_lease)
    wrong_scope_frame = QueryFrameBuilder(engine, lambda: START_NS, lambda: NOW).build(
        "What is Engram?", TENANT_B_SCOPE, diagnostic_seed="test:What is Engram?:tenant-b"
    )
    wrong_scope_lease = validate_resolver_budget(
        {name: wrong_scope_frame.get("budget", {}).get(name, 0) for name in RESOLVER_BUDGET_FIELDS}
    )
    wrong_scope = ExactResolver(engine, lambda: START_NS).resolve(wrong_scope_frame, wrong_scope_lease)

    assert "candidates" in retired_result
    assert retired_result.get("candidates", ()) == ()
    assert "candidates" in wrong_scope
    assert wrong_scope.get("candidates", ()) == ()


def proposition_projection_graph_row(projection: dict) -> dict[str, object]:
    encoded = proposition_projection_to_dict(projection)
    result = {field: value for field, value in encoded.items() if field in PROPOSITION_PROJECTION_FIELDS}
    return result


def test_proposition_resolvers_reject_falsey_invalid_eligibility_evaluator() -> None:
    engine = Engram()

    with pytest_raises(InvalidRequestError, match="structured graph eligibility_evaluator"):
        StructuredGraphResolver(engine, lambda: START_NS, False)
    with pytest_raises(InvalidRequestError, match="support semantic eligibility_evaluator"):
        SupportSemanticResolver(engine, lambda: START_NS, False)


def test_structured_graph_adapter_emits_full_proposition_in_current_core_result(monkeypatch) -> None:
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

    result = StructuredGraphResolver(engine, lambda: START_NS).resolve(query_frame, lease)
    evidence = result.get("proposition_evidence", ())

    assert result.keys() >= {"candidates", "evidence"}
    assert result.get("candidates", ()) == ()
    assert result.get("evidence", ()) == ()
    assert len(evidence) == 1
    record = evidence[0]
    record_features = record.get("features", {})
    assert record.get("proposition_id", "") == "proposition-1"
    assert record.get("canonical_references", {}).get("subject_entity_id", "") == "entity:ada"
    assert record_features.get("values", {}).get("structured_match", 0.0) == 1.0
    assert record_features.get("unavailable", ()) == ("semantic_similarity", "source_agreement", "supplied_trust")
    assert "scope" in record.get("disclosure", {})
    assert record.get("disclosure", {}).get("scope", {}) == query_frame.get("scope", {})
    serialized = proposition_evidence_record_to_dict(record)
    assert "response" not in serialized
    assert not {"subject", "predicate", "object", "proof", "cypher", "embedding"}.intersection(serialized)
    assert resolver_result_from_dict(resolver_result_to_dict(result)) == result


def test_structured_graph_adapter_excludes_ineligible_and_changed_propositions(monkeypatch) -> None:
    engine = Engram()
    eligible = proposition_projection_from_graph_row(
        {**STRUCTURED_PROJECTION_ROW, "proposition_id": "proposition-eligible"}, PropositionProjectionQuery.STRUCTURED_ENTITY
    )
    inactive = proposition_projection_from_graph_row(
        {
            **STRUCTURED_PROJECTION_ROW,
            "proposition_id": "proposition-inactive",
            "invalidated_at": "2026-08-01T00:00:00Z",
            "invalidated_at_available": True,
        },
        PropositionProjectionQuery.STRUCTURED_ENTITY,
    )
    changed = proposition_projection_from_graph_row(
        {**STRUCTURED_PROJECTION_ROW, "proposition_id": "proposition-changed"}, PropositionProjectionQuery.STRUCTURED_ENTITY
    )
    monkeypatch.setattr(
        engine,
        "structured_proposition_projections",
        lambda internal_text, row_limit, cooperative_check=(), max_working_memory_bytes=0, *, basis_window: [
            eligible,
            inactive,
            changed,
        ][:row_limit],
    )

    def current(proposition_id, internal_basis_window):
        if proposition_id == "proposition-eligible":
            result = (proposition_projection(**{**eligible, **BY_ID_PROJECTION_CHANGES}),)
            return result
        if proposition_id == "proposition-changed":
            result = (proposition_projection(**{**changed, **BY_ID_PROJECTION_CHANGES, "object_entity_id": "entity:changed"}),)
            return result
        raise AssertionError("initially ineligible Proposition must not be revalidated")

    monkeypatch.setattr(engine, "current_proposition_projection", current)
    query_frame = QueryFrameBuilder(engine, lambda: START_NS, lambda: NOW).build(
        "Ada", EMPTY_SCOPE_KEY, diagnostic_seed="test:Ada:"
    )
    lease = validate_resolver_budget({name: query_frame.get("budget", {}).get(name, 0) for name in RESOLVER_BUDGET_FIELDS})

    result = StructuredGraphResolver(engine, lambda: START_NS).resolve(query_frame, lease)
    diagnostics = result.get("diagnostics", {})
    consumption = result.get("consumption", {})

    assert tuple(record.get("proposition_id", "") for record in result.get("proposition_evidence", ())) == ("proposition-eligible",)
    assert diagnostics.get("discovery_rows", 0) == 3
    assert diagnostics.get("revalidation_rows", 0) == 2
    assert diagnostics.get("exclusion_counts", {}) == {
        "proposition_inactive": 1,
        "revalidation_identity_conflict": 1,
    }
    assert consumption.get("graph_rows", 0) == 5
    assert consumption.get("evidence", 0) == 1


def test_structured_graph_adapter_honors_evidence_bytes_and_never_mutates(monkeypatch) -> None:
    engine = Engram()
    discovered = proposition_projection_from_graph_row(STRUCTURED_PROJECTION_ROW, PropositionProjectionQuery.STRUCTURED_ENTITY)
    current = proposition_projection(**{**discovered, **BY_ID_PROJECTION_CHANGES})
    before = (engine.query_count, engine.hit_count, tuple(engine.statements))
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
    lease = resolver_budget_with_changes(
        validate_resolver_budget({name: query_frame.get("budget", {}).get(name, 0) for name in RESOLVER_BUDGET_FIELDS}),
        {"max_evidence_bytes": 256, "max_output_bytes": 256},
    )

    result = StructuredGraphResolver(engine, lambda: START_NS).resolve(query_frame, lease)

    assert "proposition_evidence" in result
    assert result.get("proposition_evidence", ()) == ()
    assert result.get("reason_code", "") == "structured_graph_miss"
    assert result.get("consumption", {}).get("exhausted_dimensions", ()) == ("evidence_bytes",)
    assert before == (engine.query_count, engine.hit_count, tuple(engine.statements))


def test_support_semantic_adapter_only_returns_support_linked_artifacts(monkeypatch) -> None:
    engine = Engram()
    engine.response_repository = ArtifactRepository(
        (validate_cached_response_artifact({**ACCEPTED_ARTIFACT, "support_references": (PROPOSITION_REFERENCE_A,)}),)
    )
    assert "graph" in engine.config
    graph_config = engine.config.get("graph", {})
    graph_config["enabled"] = True
    graph_config["vector_enabled"] = True
    graph_config["vector_weight"] = 1.0
    projections = [
        proposition_projection_from_graph_row(
            {**SEMANTIC_PROJECTION_ROW, "proposition_id": "unlinked", "semantic_similarity": 1.0},
            PropositionProjectionQuery.VECTOR,
            "proposition_premise_embeddings",
        )
    ]
    monkeypatch.setattr(
        engine,
        "graph_vector_propositions",
        lambda internal_text, *, limit=0, evaluation_time="": [
            {"proposition_id": REFERENCE_IDS.get("proposition_a", ""), "similarity": 0.9},
            {"proposition_id": "unlinked", "similarity": 1.0},
        ][:limit],
    )
    monkeypatch.setattr(
        engine,
        "graph_vector_proposition_projections",
        lambda internal_text, *, limit=0, cooperative_check=(), max_working_memory_bytes=0, basis_window: projections[:limit],
    )
    by_id = {
        projection.get("proposition_id", ""): proposition_projection(**{**projection, **BY_ID_PROJECTION_CHANGES})
        for projection in projections
    }
    monkeypatch.setattr(
        engine, "current_proposition_projection", lambda proposition_id, internal_basis_window: (by_id.get(proposition_id, {}),)
    )
    query_frame = QueryFrameBuilder(engine, lambda: START_NS, lambda: NOW).build(
        "What is Engram?", TENANT_A_SCOPE, diagnostic_seed="test:What is Engram?:tenant-a"
    )
    lease = validate_resolver_budget({name: query_frame.get("budget", {}).get(name, 0) for name in RESOLVER_BUDGET_FIELDS})

    result = SupportSemanticResolver(engine, lambda: START_NS).resolve(query_frame, lease)
    candidates = result.get("candidates", ())
    found_values = candidates[0].get("features", {}).get("values", {})

    assert len(candidates) == 1
    assert candidates[0].get("statement_id", "") == "stmt-accepted"
    assert candidates[0].get("source", CandidateSource.EXACT) == CandidateSource.SUPPORT_SEMANTIC
    assert tuple(reference.get("evidence_id", "") for reference in candidates[0].get("evidence", ())) == (
        REFERENCE_IDS.get("proposition_a", ""),
    )
    assert found_values.get("semantic_score", 0.0) == pytest_approx(0.9)
    assert "priority" in found_values
    assert found_values.get("priority", 0.0) == pytest_approx(0.0)
    assert found_values.get("retrieval_score", 0.0) == pytest_approx(0.9)
    assert tuple(record.get("proposition_id", "") for record in result.get("proposition_evidence", ())) == ("unlinked",)
    assert result.get("consumption", {}).get("vector_results", 0) == 3


def test_support_semantic_scan_limit_counts_only_relevant_edges() -> None:
    unrelated = validate_cached_response_artifact(
        {
            **ACCEPTED_ARTIFACT,
            "statement_id": "unrelated",
            "query_identity": extract_standalone_identity("What is Engram?", TENANT_B_SCOPE),
            "scope": TENANT_B_SCOPE,
            "support_references": (PROPOSITION_REFERENCE_A,),
        }
    )
    target = validate_cached_response_artifact(
        {
            **ACCEPTED_ARTIFACT,
            "statement_id": "target",
            "support_references": (PROPOSITION_REFERENCE_B, PROPOSITION_REFERENCE_A),
        }
    )
    engine = Engram()
    engine.response_repository = ArtifactRepository((unrelated, target))
    assert "graph" in engine.config
    graph_config = engine.config.get("graph", {})
    graph_config["vector_support_scan_limit"] = 1
    graph_config["vector_weight"] = 1.0

    matches = engine.vector_supported_match_components_from_scores(
        {PROPOSITION_REFERENCE_A.get("id", ""): 0.9},
        source_working_bytes=0,
        limit=1,
        artifact_filter=lambda value: value.get("scope", {}) == TENANT_A_SCOPE,
        max_working_memory_bytes=1_000_000,
    )

    assert len(matches) == 1
    assert matches[0].get("artifact", {}).get("statement_id", "") == "target"


def test_support_semantic_emits_unlinked_full_proposition_without_response_candidate(monkeypatch) -> None:
    engine = Engram()
    assert "graph" in engine.config
    graph_config = engine.config.get("graph", {})
    graph_config["enabled"] = True
    graph_config["vector_enabled"] = True
    graph_config["vector_weight"] = 1.0
    discovered = proposition_projection_from_graph_row(
        {**SEMANTIC_PROJECTION_ROW, "proposition_id": "proposition-unlinked", "semantic_similarity": 0.73},
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
        "Ada", EMPTY_SCOPE_KEY, diagnostic_seed="test:Ada:"
    )
    lease = validate_resolver_budget({name: query_frame.get("budget", {}).get(name, 0) for name in RESOLVER_BUDGET_FIELDS})

    result = SupportSemanticResolver(engine, lambda: START_NS).resolve(query_frame, lease)
    evidence = result.get("proposition_evidence", ())
    consumption = result.get("consumption", {})

    assert result.keys() >= {"candidates", "accounting"}
    assert result.get("candidates", ()) == ()
    assert result.get("accounting", ()) == ()
    assert tuple(record.get("proposition_id", "") for record in evidence) == ("proposition-unlinked",)
    assert evidence[0].get("features", {}).get("values", {}).get("semantic_similarity", 0.0) == pytest_approx(0.73)
    assert evidence[0].get("features", {}).get("unavailable", ()) == (
        "source_agreement",
        "structured_match",
        "supplied_trust",
    )
    assert consumption.get("vector_results", 0) == 1
    assert consumption.get("graph_rows", 0) == 1
    assert consumption.get("evidence", 0) == 1


def test_support_semantic_vertical_fixed_query_to_full_record(monkeypatch) -> None:
    discovered = proposition_projection_from_graph_row(
        {**SEMANTIC_PROJECTION_ROW, "proposition_id": "proposition-vertical", "semantic_similarity": 0.67},
        PropositionProjectionQuery.VECTOR,
        "proposition_premise_embeddings",
    )
    current = proposition_projection(**{**discovered, **BY_ID_PROJECTION_CHANGES})
    client = MemGraphConnection()
    calls = []

    def execute(query: str, parameters=()):
        calls.append((query, parameters))
        if "proposition.subject AS subject" in query:
            result = [{"proposition_id": "proposition-vertical", "similarity": 0.67}]
            return result
        if "query_embedding" in parameters:
            result = [proposition_projection_graph_row(discovered)]
            return result
        result = [proposition_projection_graph_row(current)]
        return result

    client.execute = execute
    engine = Engram()
    engine.internal_graph_client = client
    assert "graph" in engine.config
    engine.config.get("graph", {}).update(
        {
            "enabled": True,
            "vector_enabled": True,
            "vector_index_name": "proposition_premise_embeddings",
            "vector_limit": 10,
            "vector_min_similarity": 0.45,
            "vector_weight": 1.0,
        }
    )
    monkeypatch.setattr(engine, "encode_graph_query", lambda internal_text: [0.0, 1.0])
    query_frame = QueryFrameBuilder(engine, lambda: START_NS, lambda: NOW).build(
        "Ada", EMPTY_SCOPE_KEY, diagnostic_seed="test:Ada:"
    )
    lease = validate_resolver_budget({name: query_frame.get("budget", {}).get(name, 0) for name in RESOLVER_BUDGET_FIELDS})
    frame_budget = query_frame.get("budget", {})
    evaluation_time = query_frame.get("eligibility_context", {}).get("evaluation_time", "")
    assert evaluation_time

    result = SupportSemanticResolver(engine, lambda: START_NS).resolve(query_frame, lease)
    evidence = result.get("proposition_evidence", ())

    assert tuple(record.get("proposition_id", "") for record in evidence) == ("proposition-vertical",)
    assert evidence[0].get("features", {}).get("values", {}).get("semantic_similarity", 0.0) == pytest_approx(0.67)
    assert len(calls) == 3
    assert calls[0][1] == {
        "index_name": "proposition_premise_embeddings",
        "limit": frame_budget.get("max_candidates", 0),
        "query_embedding": [0.0, 1.0],
        "min_similarity": 0.45,
        "evaluation_time": evaluation_time,
    }
    current_basis = {
        "basis_start": evaluation_time,
        "basis_start_available": True,
        "basis_end": evaluation_time,
        "basis_end_available": True,
        "basis_end_inclusive": True,
    }
    assert calls[1][1] == {
        "index_name": "proposition_premise_embeddings",
        "limit": frame_budget.get("max_vector_results", 0) - 1,
        "query_embedding": [0.0, 1.0],
        "min_similarity": 0.45,
        **current_basis,
    }
    assert calls[2][1] == {"proposition_id": "proposition-vertical", **current_basis}
    assert "Ada" not in calls[0][0]
    assert "Ada" not in calls[1][0]
    assert "Ada" not in calls[2][0]


def test_support_semantic_proposition_discovery_fails_soft_and_cooperates_with_limits(monkeypatch) -> None:
    engine = Engram()
    enable_graph_resolvers(engine, vector=True)
    query_frame = QueryFrameBuilder(engine, lambda: START_NS, lambda: NOW).build(
        "Ada", EMPTY_SCOPE_KEY, diagnostic_seed="test:Ada:"
    )
    lease = validate_resolver_budget({name: query_frame.get("budget", {}).get(name, 0) for name in RESOLVER_BUDGET_FIELDS})
    monkeypatch.setattr(engine, "encode_graph_query", lambda internal_text: [0.0] * 384)
    monkeypatch.setattr(
        engine,
        "graph_vector_proposition_projections",
        lambda *internal_args, **internal_kwargs: [],
    )

    unavailable = SupportSemanticResolver(engine, lambda: START_NS).resolve(query_frame, lease)
    unavailable_consumption = unavailable.get("consumption", {})

    assert unavailable.get("state", ResolverState.FAILED) == ResolverState.COMPLETED
    assert unavailable.get("reason_code", "") == "support_semantic_miss"
    assert "proposition_evidence" in unavailable
    assert unavailable.get("proposition_evidence", ()) == ()
    assert "vector_results" in unavailable_consumption
    assert unavailable_consumption.get("vector_results", 0) == 0

    monkeypatch.setattr(
        engine,
        "graph_vector_proposition_projections",
        lambda *internal_args, **internal_kwargs: (_ for _ in ()).throw(TimeoutError("deadline")),
    )
    failed = (
        ResolverExecutor(lambda: START_NS)
        .execute(query_frame, ResolverRegistry((SupportSemanticResolver(engine, lambda: START_NS),)).plan(query_frame))
        .get("results", ())[0]
    )
    failed_consumption = failed.get("consumption", {})

    assert "state" in failed
    assert failed.get("state", ResolverState.FAILED) == ResolverState.FAILED
    assert failed.get("reason_code", "") == "resolver_exception"
    assert failed.get("diagnostics", {}).get("exception_type", "") == "TimeoutError"
    assert "exhausted_dimensions" in failed_consumption
    assert failed_consumption.get("exhausted_dimensions", ()) == ()


def test_support_semantic_proposition_evidence_honors_graph_byte_and_memory_bounds(monkeypatch) -> None:
    engine = Engram()
    assert "graph" in engine.config
    engine.config.get("graph", {}).update({"enabled": True, "vector_enabled": True, "vector_weight": 1.0})
    discovered = proposition_projection_from_graph_row(
        {**SEMANTIC_PROJECTION_ROW, "proposition_id": "proposition-bounded", "semantic_similarity": 0.8},
        PropositionProjectionQuery.VECTOR,
        "proposition_premise_embeddings",
    )
    current = proposition_projection(**{**discovered, **BY_ID_PROJECTION_CHANGES})
    current_calls = 0
    monkeypatch.setattr(
        engine,
        "graph_vector_proposition_projections",
        lambda internal_text, *, limit=0, cooperative_check=(), max_working_memory_bytes=0, basis_window: [discovered][:limit],
    )

    def current_projection(internal_proposition_id, internal_basis_window):
        nonlocal current_calls
        current_calls += 1
        result = (current,)
        return result

    monkeypatch.setattr(engine, "current_proposition_projection", current_projection)
    query_frame = QueryFrameBuilder(engine, lambda: START_NS, lambda: NOW).build(
        "Ada", EMPTY_SCOPE_KEY, diagnostic_seed="test:Ada:"
    )
    lease = validate_resolver_budget({name: query_frame.get("budget", {}).get(name, 0) for name in RESOLVER_BUDGET_FIELDS})

    no_graph_rows = SupportSemanticResolver(engine, lambda: START_NS).resolve(
        query_frame,
        resolver_budget_with_changes(lease, {"max_graph_rows": 0}),
    )
    assert "proposition_evidence" in no_graph_rows
    assert no_graph_rows.get("proposition_evidence", ()) == ()
    assert current_calls == 0
    byte_limited = SupportSemanticResolver(engine, lambda: START_NS).resolve(
        query_frame,
        resolver_budget_with_changes(lease, {"max_evidence_bytes": 256}),
    )
    assert current_calls == 1
    memory_limited = SupportSemanticResolver(engine, lambda: START_NS).resolve(
        query_frame,
        resolver_budget_with_changes(lease, {"max_working_memory_bytes": 256}),
    )

    assert current_calls == 2
    assert "proposition_evidence" in byte_limited
    assert byte_limited.get("proposition_evidence", ()) == ()
    assert "evidence_bytes" in byte_limited.get("consumption", {}).get("exhausted_dimensions", ())
    assert memory_limited.get("state", ResolverState.FAILED) == ResolverState.EXHAUSTED
    assert memory_limited.get("reason_code", "") == "working_memory_bytes_budget"
