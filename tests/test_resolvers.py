"""Section 4 resolver, executor, accounting, and orchestration conformance."""

from collections.abc import Mapping
from datetime import UTC, datetime
from json import dumps as json_dumps

from pytest import approx as pytest_approx, mark as pytest_mark, raises as pytest_raises
from sentence_transformers import SentenceTransformer

from engram import service as service_module
from engram.artifacts import validate_cached_response_artifact
from engram.constants import (
    ACCOUNTING_FINALIZATION_FIELDS,
    BUDGET_CONSUMPTION_FIELDS,
    EMPTY_SCOPE_KEY,
    EVIDENCE_PACKAGE_FIELDS,
    INITIAL_ARTIFACT_STATISTICS,
    PROPOSITION_PROJECTION_FIELDS,
    RESOLUTION_RESULT_FIELDS,
    RESOLVER_BUDGET_FIELDS,
    RESOLVER_RESULT_FIELDS,
    CandidateSource,
    CostClass,
    EvidenceKind,
    EvidencePackageTruncationReason,
    FusionPolicyReason,
    LifecycleState,
    PropositionProjectionQuery,
    ResolutionOutcome,
    ResolverState,
    Tier,
)
from engram.coordination import AtomicMutationCoordinator
from engram.core import Engram
from engram.errors import ConflictError, InvalidRequestError, ResolutionCancelledError
from engram.fusion import CandidateFusionEngine, permissive_candidate_authority
from engram.graph import (
    MemGraphConnection,
    proposition_projection,
    proposition_projection_from_graph_row,
    proposition_projection_to_dict,
)
from engram.identity import extract_standalone_identity, retrieval_representation, scope_key
from engram.repository import ArtifactRepository, tier_admission_policy
from engram.resolution import (
    QueryFrameBuilder,
    accounting_observation,
    budget_consumption,
    build_evidence_package,
    candidate as resolution_candidate,
    capture_resolution_budget,
    empty_evidence_package,
    evidence_package_to_json,
    evidence_reference,
    feature_set,
    proposition_evidence_record_to_dict,
    proposition_evidence_record_with_changes,
    query_frame_with_changes,
    resolution_budget,
    resolution_result_to_dict,
    resolution_result_to_json,
    resolver_result,
    resolver_result_from_dict,
    resolver_result_to_dict,
    validate_candidate,
    validate_canonical_proposition_references,
    validate_disclosure_decision,
    validate_proposition_validity_inputs,
    validate_resolution_budget,
    validate_resolver_result,
)
from engram.resolvers import (
    ExactResolver,
    ResolutionAccountingFinalizer,
    ResolutionOrchestrator,
    ResolverExecutor,
    ResolverRegistry,
    StructuredGraphResolver,
    SupportSemanticResolver,
    bound_validated_resolver_result,
    json_array_bytes,
    json_size,
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
from engram.service import EngramCore
from tests.support_fixtures import PROPOSITION_REFERENCE_A, PROPOSITION_REFERENCE_B, REFERENCE_IDS

NOW = datetime(2026, 8, 12, 18, 0, tzinfo=UTC)
START_NS = 1_000_000_000
TENANT_A_SCOPE = scope_key(namespace="tenant-a")
TENANT_B_SCOPE = scope_key(namespace="tenant-b")
# The accepted STATIC tenant-a artifact for "What is Engram?" (alias "Explain Engram"). Tests derive variants with
# validate_cached_response_artifact({**ACCEPTED_ARTIFACT, ...}); the validator and ArtifactRepository copy their
# input, so this constant stays read-only.
ACCEPTED_ARTIFACT = validate_cached_response_artifact(
    {
        "statement_id": "stmt-accepted",
        "generation": 1,
        "response": "Engram preserves exact text: café ☕.",
        "query_identity": extract_standalone_identity("What is Engram?", TENANT_A_SCOPE),
        "retrieval": retrieval_representation("What is Engram?", ("Explain Engram",)),
        "tier": Tier.STATIC,
        "lifecycle": LifecycleState.ACTIVE,
        "scope": TENANT_A_SCOPE,
        "support_references": (),
        "valid_from": "",
        "valid_from_available": False,
        "valid_until": "",
        "valid_until_available": False,
        "superseded_by": "",
        "provenance": {"source_label": "released", "caller_id": "regulator-a", "accepted_at": "2026-08-12T16:00:00Z"},
        "statistics": INITIAL_ARTIFACT_STATISTICS,
        "metadata": {"approved": True},
    }
)
# The global-scope sparse "stmt-candidate" response candidate; tests derive variants with
# validate_candidate({**SPARSE_CANDIDATE, ...}), which copies its input.
SPARSE_CANDIDATE = resolution_candidate(
    candidate_id="candidate:sparse:stmt-candidate",
    statement_id="stmt-candidate",
    response="Candidate response",
    source=CandidateSource.SPARSE,
    features=feature_set(values={"sparse_score": 1.0}),
    evidence=(),
    scope=EMPTY_SCOPE_KEY,
    lifecycle=LifecycleState.ACTIVE,
)
# A current public graph row for "proposition-1" (Ada built the engine) with a structured match; tests decode it with
# proposition_projection_from_graph_row({**STRUCTURED_PROJECTION_ROW, "proposition_id": ...}, STRUCTURED_ENTITY).
STRUCTURED_PROJECTION_ROW = {
    "proposition_id": "proposition-1",
    "subject_entity_id": "entity:ada",
    "predicate_id": "predicate:built",
    "object_entity_id": "entity:engine",
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
}
# The same row discovered by vector search: tests add the id and similarity and decode it as a VECTOR projection
# from the "proposition_premise_embeddings" index.
SEMANTIC_PROJECTION_ROW = {
    **STRUCTURED_PROJECTION_ROW,
    "structured_match": 0.0,
    "structured_match_available": False,
    "semantic_similarity_available": True,
}
# A by-id current read carries the BY_ID query and no discovery match scores or vector index.
BY_ID_PROJECTION_CHANGES = {
    "projection_id": PropositionProjectionQuery.BY_ID,
    "structured_match": 0.0,
    "structured_match_available": False,
    "semantic_similarity": 0.0,
    "semantic_similarity_available": False,
    "vector_index_id": "",
    "vector_index_id_available": False,
}


class ReadyEmbeddingModel(SentenceTransformer):
    def __init__(self) -> None:
        pass

    def __bool__(self) -> bool:
        return True


def enable_graph_resolvers(engine: Engram, *, vector: bool = False) -> None:
    """Mark an injected graph capability ready for resolver-planning tests."""
    client = type("ReadyGraphCapability", (), {"available": True})()
    engine.internal_graph_client = client
    assert "graph" in engine.config
    graph_config = engine.config.get("graph", {})
    graph_config["enabled"] = True
    if vector:
        graph_config["vector_enabled"] = True
        engine.graph_embedding_model = ReadyEmbeddingModel()


class FakeResolver:
    def __init__(
        self,
        name: str,
        result: dict,
        *,
        available: bool = True,
        cost_class: CostClass = CostClass.CHEAP,
        error: bool = False,
        availability_error: bool = False,
    ) -> None:
        self.name = name
        self.cost_class = cost_class
        self.internal_result = result
        self.internal_available = available
        self.internal_error = error
        self.internal_availability_error = availability_error
        self.calls = 0

    def available(self, frame: dict) -> bool:
        if self.internal_availability_error:
            raise RuntimeError("injected availability failure")
        result = self.internal_available
        return result

    def resolve(
        self,
        frame: dict,
        budget: dict,
        cooperative_check=(),
    ) -> dict:
        if cooperative_check:
            cooperative_check()
        self.calls += 1
        if self.internal_error:
            raise RuntimeError("injected resolver failure")
        result = self.internal_result
        return result


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
            {
                "disclosure": validate_disclosure_decision(
                    {**record.get("disclosure", {}), "scope": scope_key(namespace="other")}
                )
            },
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


def test_orchestrator_cancellation_after_execution_prevents_accounting_publication() -> None:
    engine = Engram()
    query_frame = QueryFrameBuilder(engine, lambda: START_NS, lambda: NOW).build(
        "What is Engram?", EMPTY_SCOPE_KEY, diagnostic_seed="test:What is Engram?:"
    )
    resolver = FakeResolver("structured_graph", resolver_result("structured_graph", ResolverState.COMPLETED))
    finalizer = ResolutionAccountingFinalizer(
        engine,
        AcceptedResponseService(
            AtomicMutationCoordinator(engine.response_repository, engine.mutation_receipts),
            tier_admission_policy(engine.config.get("capacity", 1)),
        ),
    )
    orchestrator = ResolutionOrchestrator(
        ResolverRegistry((resolver,)),
        ResolverExecutor(lambda: START_NS),
        finalizer,
    )
    checks = 0

    def cancel_after_execution() -> None:
        nonlocal checks
        checks += 1
        if checks == 4:
            raise ResolutionCancelledError("cancel after resolver execution")

    with pytest_raises(ResolutionCancelledError, match="after resolver execution"):
        orchestrator.resolve(
            query_frame,
            "cancel-after-execution",
            cooperative_check=cancel_after_execution,
        )

    assert resolver.calls == 1
    assert finalizer.internal_requests == {}
    assert engine.query_count == 0


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
    oversized_candidate = validate_candidate(
        {**SPARSE_CANDIDATE, "response": "x" * 10_000, "evidence": (reference, reference)}
    )
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


def test_core_orchestration_exact_answer_is_deterministic_and_retry_safe() -> None:
    engine = Engram()
    engine.response_repository = ArtifactRepository((ACCEPTED_ARTIFACT,))
    core = EngramCore(engine)

    first = core.resolve_request("Explain Engram", "request-exact", namespace="tenant-a", accept_exact=True)
    replay = core.resolve_request("Explain Engram", "request-exact", namespace="tenant-a", accept_exact=True)

    assert first == replay
    assert first is not replay
    assert set(first) == RESOLUTION_RESULT_FIELDS
    assert first.get("outcome", ResolutionOutcome.MISS) == ResolutionOutcome.ANSWER
    assert first.get("selected_candidate", {}).get("response", "") == "Engram preserves exact text: café ☕."
    assert first.get("evidence_package_available", False) is False
    assert first.get("evidence_package", {}).get("records", ()) == ()
    assert [result.get("resolver", "") for result in first.get("resolver_results", ())] == ["exact"]
    updated_statistics = core.engram.response_repository.get_artifact("stmt-accepted").get("statistics", {})
    assert updated_statistics.get("query_count", 0) == 1
    assert updated_statistics.get("hit_count", 0) == 1
    assert core.engram.get_statement("stmt-accepted") == {}
    first["reason_codes"] = ("caller_mutation",)
    first_budget = first.get("budget", {})
    first_budget["candidates"] = 999
    isolated_replay = core.resolve_request("Explain Engram", "request-exact", namespace="tenant-a", accept_exact=True)
    assert isolated_replay == replay
    with pytest_raises(ConflictError, match="different input"):
        core.resolve_request("Different request", "request-exact", namespace="tenant-a", accept_exact=True)


def test_core_result_cache_eviction_discards_matching_transient_accounting(monkeypatch) -> None:
    monkeypatch.setattr(service_module, "MAX_TRANSIENT_RECORDS", 1)
    core = EngramCore(Engram())

    core.resolve_request("first miss", "request-first", configured_resolvers=("exact",))
    core.resolve_request("second miss", "request-second", configured_resolvers=("exact",))
    replay = core.resolve_request("first miss", "request-first", configured_resolvers=("exact",))

    assert "outcome" in replay
    assert replay.get("outcome", ResolutionOutcome.MISS) == ResolutionOutcome.MISS
    assert tuple(core.resolution_requests) == ("request-first",)
    assert tuple(core.resolution_accounting.internal_requests) == ()


def test_output_budget_downgrade_does_not_record_accepted_success() -> None:
    engine = Engram()
    engine.response_repository = ArtifactRepository(
        (validate_cached_response_artifact({**ACCEPTED_ARTIFACT, "response": "x" * 1_000}),)
    )
    core = EngramCore(engine)

    result = core.resolve_request(
        "What is Engram?",
        "request-output-downgrade",
        namespace="tenant-a",
        accept_exact=True,
        budget=resolution_budget(max_output_bytes=4_096),
    )

    updated_statistics = core.engram.response_repository.get_artifact("stmt-accepted").get("statistics", {})
    accounting = result.get("frame_diagnostics", {}).get("accounting", {})
    assert isinstance(accounting, Mapping)
    assert set(result) == RESOLUTION_RESULT_FIELDS
    assert "success_applied" in accounting
    assert "hit_count" in updated_statistics
    assert result.get("outcome", ResolutionOutcome.MISS) == ResolutionOutcome.MISS
    assert "answer_exceeds_output_budget" in result.get("reason_codes", ())
    assert accounting.get("success_applied", False) is False
    assert updated_statistics.get("query_count", 0) == 1
    assert updated_statistics.get("hit_count", 0) == 0


def test_exact_accounting_receipt_is_scoped_to_one_process() -> None:
    first_engine = Engram()
    first_engine.response_repository = ArtifactRepository((ACCEPTED_ARTIFACT,))
    first_core = EngramCore(first_engine)
    first_core.resolve_request("Explain Engram", "request-restart", namespace="tenant-a", accept_exact=True)
    restarted_engine = Engram()
    restarted_engine.response_repository = ArtifactRepository((ACCEPTED_ARTIFACT,))
    restarted_core = EngramCore(restarted_engine)

    replay = restarted_core.resolve_request(
        "Explain Engram",
        "request-restart",
        namespace="tenant-a",
        accept_exact=True,
    )

    updated_statistics = restarted_core.engram.response_repository.get_artifact("stmt-accepted").get("statistics", {})
    assert replay.get("outcome", ResolutionOutcome.MISS) == ResolutionOutcome.ANSWER
    assert updated_statistics.get("query_count", 0) == 1
    assert updated_statistics.get("hit_count", 0) == 1
    assert restarted_core.engram.query_count == 0
    assert restarted_core.engram.hit_count == 0


def test_orchestration_returns_evidence_for_non_exact_and_miss_for_no_output() -> None:
    engine = Engram()
    query_frame = QueryFrameBuilder(engine, lambda: START_NS, lambda: NOW).build(
        "What is Engram?", EMPTY_SCOPE_KEY, diagnostic_seed="test:What is Engram?:"
    )
    sparse = FakeResolver(
        "sparse",
        resolver_result(
            "sparse",
            ResolverState.COMPLETED,
            candidates=(SPARSE_CANDIDATE,),
        ),
    )
    evidence_accounting = ResolutionAccountingFinalizer(
        engine,
        AcceptedResponseService(
            AtomicMutationCoordinator(engine.response_repository, engine.mutation_receipts),
            tier_admission_policy(engine.config.get("capacity", 1)),
        ),
    )
    evidence_orchestrator = ResolutionOrchestrator(
        ResolverRegistry((sparse,)),
        ResolverExecutor(lambda: START_NS),
        evidence_accounting,
        CandidateFusionEngine(authority=permissive_candidate_authority),
    )
    evidence_result, _ = evidence_orchestrator.resolve(query_frame, "request-evidence")
    empty = FakeResolver("empty", resolver_result("empty", ResolverState.COMPLETED))
    miss_orchestrator = ResolutionOrchestrator(
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
    miss_result, _ = miss_orchestrator.resolve(query_frame, "request-miss")
    response_candidates = evidence_result.get("response_candidates", ())

    assert set(evidence_result) == RESOLUTION_RESULT_FIELDS
    assert set(miss_result) == RESOLUTION_RESULT_FIELDS
    assert evidence_result.get("outcome", ResolutionOutcome.MISS) == ResolutionOutcome.EVIDENCE
    assert evidence_result.get("selected_candidate_available", False) is False
    assert tuple(value.get("statement_id", "") for value in response_candidates) == ("stmt-candidate",)
    assert response_candidates[0].get("features", {}).get("values", {}).get("lexical", 0.0) == 1.0
    assert miss_result.get("outcome", ResolutionOutcome.MISS) == ResolutionOutcome.MISS
    assert miss_result.get("response_candidates", ()) == ()
    assert miss_result.get("evidence", ()) == ()


def test_fused_non_exact_answer_is_fail_soft_and_accounted_once_without_implicit_acceptance() -> None:
    statement_id = "stmt-accepted"
    engine = Engram()
    engine.response_repository = ArtifactRepository(
        (
            validate_cached_response_artifact(
                {
                    **ACCEPTED_ARTIFACT,
                    "response": "Candidate response",
                    "query_identity": extract_standalone_identity("What is Engram?", EMPTY_SCOPE_KEY),
                    "scope": EMPTY_SCOPE_KEY,
                    "support_references": (PROPOSITION_REFERENCE_A,),
                }
            ),
        )
    )
    reference = evidence_reference(PROPOSITION_REFERENCE_A.get("id", ""), "semantic", EvidenceKind.SUPPORT, EMPTY_SCOPE_KEY)
    sparse_candidate = validate_candidate(
        {
            **SPARSE_CANDIDATE,
            "candidate_id": f"candidate:sparse:{statement_id}",
            "statement_id": statement_id,
            "features": feature_set(values={"sparse_score": 0.95}),
        }
    )
    semantic_candidate = validate_candidate(
        {
            **SPARSE_CANDIDATE,
            "candidate_id": f"candidate:support_semantic:{statement_id}",
            "statement_id": statement_id,
            "source": CandidateSource.SUPPORT_SEMANTIC,
            "evidence": (reference,),
            "features": feature_set(values={"semantic_score": 0.92, "support_coverage": 1.0}),
        }
    )
    observation = accounting_observation(statement_id, ("candidate",))
    failed = FakeResolver("failed", resolver_result("failed", ResolverState.COMPLETED), error=True)
    sparse = FakeResolver(
        "sparse",
        resolver_result(
            "sparse",
            ResolverState.COMPLETED,
            candidates=(sparse_candidate,),
            accounting=(observation,),
        ),
    )
    semantic = FakeResolver(
        "semantic",
        resolver_result(
            "semantic",
            ResolverState.COMPLETED,
            candidates=(semantic_candidate,),
            accounting=(observation,),
        ),
    )
    orchestrator = ResolutionOrchestrator(
        ResolverRegistry((failed, sparse, semantic)),
        ResolverExecutor(lambda: START_NS),
        ResolutionAccountingFinalizer(
            engine,
            AcceptedResponseService(
                AtomicMutationCoordinator(engine.response_repository, engine.mutation_receipts),
                tier_admission_policy(engine.config.get("capacity", 1)),
            ),
        ),
    )
    query_frame = QueryFrameBuilder(engine, lambda: START_NS, lambda: NOW).build(
        "What is Engram?", EMPTY_SCOPE_KEY, diagnostic_seed="test:What is Engram?:"
    )

    result, first = orchestrator.resolve(query_frame, "request-fused", accept_exact=True)
    replay_result, replay = orchestrator.resolve(query_frame, "request-fused", accept_exact=True)

    assert set(first) == ACCOUNTING_FINALIZATION_FIELDS
    assert result.get("outcome", ResolutionOutcome.MISS) == ResolutionOutcome.ANSWER
    assert replay_result.get("outcome", ResolutionOutcome.MISS) == ResolutionOutcome.ANSWER
    assert replay_result.get("selected_candidate", {}).get("statement_id", "") == statement_id
    assert [value.get("state", ResolverState.FAILED) for value in result.get("resolver_results", ())] == [
        ResolverState.FAILED,
        ResolverState.COMPLETED,
        ResolverState.COMPLETED,
    ]
    assert first.get("candidate_statement_ids", ()) == (statement_id,)
    assert first.get("accepted_statement_id", "") == ""
    assert first.get("success_applied", False) is False
    assert replay.get("idempotent", False) is True
    statistics = engine.response_repository.get_artifact(statement_id).get("statistics", {})
    assert "hit_count" in statistics
    assert statistics.get("query_count", 0) == 1
    assert statistics.get("hit_count", 0) == 0
    assert engine.get_statement(statement_id) == {}


def test_orchestrator_reserves_remaining_memory_and_reports_fusion_consumption() -> None:
    engine = Engram()
    query_frame = QueryFrameBuilder(engine, lambda: START_NS, lambda: NOW).build(
        "What is Engram?", EMPTY_SCOPE_KEY, diagnostic_seed="test:What is Engram?:"
    )
    value = validate_candidate({**SPARSE_CANDIDATE, "features": feature_set(values={"sparse_score": 0.9})})
    raw = resolver_result(
        "sparse",
        ResolverState.COMPLETED,
        candidates=(value,),
    )
    executor = ResolverExecutor(lambda: START_NS)
    probe_resolver = FakeResolver("sparse", raw)
    probe_execution = executor.execute(query_frame, ResolverRegistry((probe_resolver,)).plan(query_frame))
    fusion = CandidateFusionEngine(authority=permissive_candidate_authority)
    fusion_required = fusion.decide(query_frame, (value,)).get("working_memory_bytes", 0)
    probe_memory_bytes = probe_execution.get("consumption", {}).get("working_memory_bytes", 0)
    total_limit = probe_memory_bytes + fusion_required - 1
    constrained_frame = query_frame_with_changes(
        query_frame,
        {"budget": validate_resolution_budget({**query_frame.get("budget", {}), "max_working_memory_bytes": total_limit})},
    )
    orchestrator = ResolutionOrchestrator(
        ResolverRegistry((FakeResolver("sparse", raw),)),
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

    result, finalization = orchestrator.resolve(constrained_frame, "request-fusion-memory")
    budget = result.get("budget", {})

    assert set(result) == RESOLUTION_RESULT_FIELDS
    assert set(finalization) == ACCOUNTING_FINALIZATION_FIELDS
    assert result.get("outcome", ResolutionOutcome.MISS) == ResolutionOutcome.MISS
    assert FusionPolicyReason.FUSION_MEMORY_EXHAUSTED.value in result.get("reason_codes", ())
    assert "working_memory_bytes" in budget.get("exhausted_dimensions", ())
    assert budget.get("working_memory_bytes", 0) == total_limit
    assert finalization.get("success_applied", False) is False


def test_complete_result_serialization_obeys_and_reports_output_budget() -> None:
    engine = Engram()
    selected_budget = capture_resolution_budget(
        lambda: START_NS,
        max_output_bytes=4_096,
    )
    query_frame = QueryFrameBuilder(engine, lambda: START_NS, lambda: NOW).build(
        "What is Engram?", EMPTY_SCOPE_KEY, diagnostic_seed="test:What is Engram?:", budget=selected_budget
    )
    empty = FakeResolver("empty", resolver_result("empty", ResolverState.COMPLETED))
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

    result, _ = orchestrator.resolve(query_frame, "request-output-envelope")
    encoded_size = len(resolution_result_to_json(result).encode("utf-8"))

    assert encoded_size <= query_frame.get("budget", {}).get("max_output_bytes", 0)
    assert result.get("budget", {}).get("output_bytes", 0) == encoded_size


def test_complete_result_truncates_variable_payload_to_output_budget() -> None:
    engine = Engram()
    selected_budget = capture_resolution_budget(
        lambda: START_NS,
        max_output_bytes=4_096,
    )
    query_frame = QueryFrameBuilder(engine, lambda: START_NS, lambda: NOW).build(
        "What is Engram?", EMPTY_SCOPE_KEY, diagnostic_seed="test:What is Engram?:", budget=selected_budget
    )
    large_candidate = validate_candidate({**SPARSE_CANDIDATE, "response": "x" * 2_500})
    resolver = FakeResolver(
        "large",
        resolver_result(
            "large",
            ResolverState.COMPLETED,
            candidates=(large_candidate,),
        ),
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
        CandidateFusionEngine(authority=permissive_candidate_authority),
    )

    result, _ = orchestrator.resolve(query_frame, "request-output-payload")
    encoded_size = len(resolution_result_to_json(result).encode("utf-8"))
    budget = result.get("budget", {})

    assert encoded_size <= query_frame.get("budget", {}).get("max_output_bytes", 0)
    assert budget.get("output_bytes", 0) == encoded_size
    assert "output_truncated" in result.get("reason_codes", ())
    assert "output_bytes" in budget.get("exhausted_dimensions", ())


@pytest_mark.parametrize("max_output_bytes", (4_096, 8_192))
def test_resolution_never_fails_after_accounting_at_the_output_boundary(max_output_bytes: int) -> None:
    statement_id = "stmt-candidate"
    engine = Engram()
    engine.response_repository = ArtifactRepository(
        (
            validate_cached_response_artifact(
                {
                    **ACCEPTED_ARTIFACT,
                    "statement_id": statement_id,
                    "query_identity": extract_standalone_identity("What is Engram?", EMPTY_SCOPE_KEY),
                    "scope": EMPTY_SCOPE_KEY,
                }
            ),
        )
    )
    finalizer = ResolutionAccountingFinalizer(
        engine,
        AcceptedResponseService(
            AtomicMutationCoordinator(engine.response_repository, engine.mutation_receipts),
            tier_admission_policy(engine.config.get("capacity", 1)),
        ),
    )
    selected_budget = capture_resolution_budget(lambda: START_NS, max_output_bytes=max_output_bytes)
    query_frame = QueryFrameBuilder(engine, lambda: START_NS, lambda: NOW).build(
        "What is Engram?", EMPTY_SCOPE_KEY, diagnostic_seed="test:What is Engram?:", budget=selected_budget
    )
    request_ids = iter(range(1_000_000))

    def resolve(response_bytes: int) -> dict:
        found = validate_candidate({**SPARSE_CANDIDATE, "response": "x" * response_bytes})
        resolver = FakeResolver(
            "large",
            resolver_result(
                "large",
                ResolverState.COMPLETED,
                candidates=(found,),
                accounting=(accounting_observation(statement_id),),
            ),
        )
        orchestrator = ResolutionOrchestrator(
            ResolverRegistry((resolver,)),
            ResolverExecutor(lambda: START_NS),
            finalizer,
            CandidateFusionEngine(authority=permissive_candidate_authority),
        )
        result, _ = orchestrator.resolve(query_frame, f"request-boundary-{next(request_ids)}")
        return result

    def check(response_bytes: int) -> tuple:
        """Assert R5 for one size and return the result's shape."""
        before = engine.response_repository.get_artifact(statement_id)
        try:
            result = resolve(response_bytes)
        except InvalidRequestError:
            assert engine.response_repository.get_artifact(statement_id) == before
            return ("failed",)
        encoded_size = len(resolution_result_to_json(result).encode("utf-8"))
        assert encoded_size <= max_output_bytes
        assert set(result) == RESOLUTION_RESULT_FIELDS
        assert result.get("budget", {}).get("output_bytes", 0) == encoded_size
        shape = (
            result.get("reason_codes", ()),
            len(result.get("resolver_results", ())),
            len(result.get("response_candidates", ())),
        )
        return shape

    # Growth after the write shows up where one trimming step stops being enough, so
    # scan coarsely and check every size between two scanned sizes whose shapes differ.
    step = 16
    shapes = {response_bytes: check(response_bytes) for response_bytes in range(1, max_output_bytes + 1, step)}
    scanned = sorted(shapes)
    for previous, current in zip(scanned, scanned[1:], strict=False):
        if shapes.get(previous, ()) != shapes.get(current, ()):
            for response_bytes in range(previous + 1, current):
                check(response_bytes)


def test_json_array_bytes_matches_encoding_every_prefix() -> None:
    values = [{"proposition_id": f"p-{index}", "label": "Ü" * index, "scores": [index, 0.5], "flag": True} for index in range(12)]

    for count in range(len(values) + 1):
        prefix = values[:count]
        expected = json_size(prefix) if prefix else 0
        assert json_array_bytes(sum(json_size(value) for value in prefix), count) == expected
