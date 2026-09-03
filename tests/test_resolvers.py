"""Section 4 resolver, executor, accounting, and orchestration conformance."""

from collections.abc import Mapping
from datetime import UTC, datetime
from json import dumps as json_dumps

from pytest import approx as pytest_approx, mark as pytest_mark, raises as pytest_raises
from sentence_transformers import SentenceTransformer

from engram import service as service_module
from engram.artifacts import LifecycleState, artifact_provenance, artifact_statistics, cached_response_artifact
from engram.constants import EMPTY_MAPPING, Tier
from engram.coordination import AtomicMutationCoordinator
from engram.core import Engram
from engram.errors import ConflictError, InvalidRequestError, ResolutionCancelledError
from engram.fusion import CandidateFusionEngine, FusionPolicyReason, permissive_candidate_authority
from engram.graph import (
    PROPOSITION_PROJECTION_FIELDS,
    MemGraphConnection,
    PropositionProjectionQuery,
    proposition_projection,
    proposition_projection_from_graph_row,
    proposition_projection_to_dict,
)
from engram.identity import build_retrieval_representation, build_standalone_identity, scope_key
from engram.repository import ArtifactRepository, tier_admission_policy
from engram.resolution import (
    CandidateSource,
    CostClass,
    EvidenceKind,
    EvidencePackageTruncationReason,
    QueryFrameBuilder,
    ResolutionOutcome,
    ResolverState,
    accounting_observation,
    budget_consumption,
    build_evidence_package,
    candidate as resolution_candidate,
    canonical_proposition_references_with_changes,
    capture_resolution_budget,
    disclosure_decision_with_changes,
    empty_evidence_package,
    evidence_package_to_json,
    evidence_reference,
    feature_set,
    proposition_evidence_record_to_dict,
    proposition_evidence_record_with_changes,
    proposition_validity_inputs_with_changes,
    query_frame_with_changes,
    resolution_budget,
    resolution_budget_with_changes,
    resolution_result_to_dict,
    resolution_result_to_json,
    resolver_result,
    resolver_result_from_json,
    resolver_result_to_json,
)
from engram.resolvers import (
    ExactResolver,
    ResolutionAccountingFinalizer,
    ResolutionOrchestrator,
    ResolverExecutor,
    ResolverRegistry,
    StructuredGraphResolver,
    SupportSemanticResolver,
    bound_resolver_result,
    execution_report_canonical_proposition_evidence,
    execution_report_with_changes,
    resolution_plan_to_dict,
    resolver_budget as build_resolver_budget,
    resolver_budget_from_json,
    resolver_budget_to_json,
    resolver_budget_with_changes,
    resolver_contract,
    resolver_reservation,
    resolver_reservation_from_json,
    resolver_reservation_to_json,
    resolver_reservation_with_changes,
)
from engram.responses import AcceptedResponseService
from engram.service import EngramCore

from .support_fixtures import PROPOSITION_REFERENCE_A, REFERENCE_IDS

DEFAULT_CANDIDATE_FEATURES = {"sparse_score": 1.0}

NOW = datetime(2026, 8, 12, 18, 0, tzinfo=UTC)
START_NS = 1_000_000_000


class ReadyEmbeddingModel(SentenceTransformer):
    def __init__(self) -> None:
        pass

    def __bool__(self) -> bool:
        return True


def artifact(
    statement_id: str = "stmt-accepted",
    *,
    request: str = "What is Engram?",
    response: str = "Engram preserves exact text: café ☕.",
    aliases: tuple[str, ...] = ("Explain Engram",),
    namespace: str = "tenant-a",
    lifecycle: LifecycleState = LifecycleState.ACTIVE,
    source_label: str = "tapestry:released",
    support_references: tuple[dict, ...] = (),
    metadata=(),
) -> dict:
    scope = scope_key(namespace=namespace)
    selected_metadata = metadata if isinstance(metadata, dict) else {"approved": True}
    result = cached_response_artifact(
        statement_id=statement_id,
        generation=1,
        response=response,
        query_identity=build_standalone_identity(request, scope),
        retrieval=build_retrieval_representation(request, aliases),
        tier=Tier.STATIC,
        lifecycle=lifecycle,
        scope=scope,
        support_references=support_references,
        valid_from="",
        valid_from_available=False,
        valid_until="",
        valid_until_available=False,
        superseded_by="",
        provenance=artifact_provenance(source_label, "regulator-a", "2026-08-12T16:00:00Z"),
        statistics=artifact_statistics(),
        metadata=selected_metadata,
    )
    return result


def engine_with_artifacts(*artifacts: dict) -> Engram:
    engine = Engram()
    engine.response_repository = ArtifactRepository(artifacts)
    return engine


def accounting_finalizer(engine: Engram, max_requests: int = 1_000) -> ResolutionAccountingFinalizer:
    coordinator = AtomicMutationCoordinator(engine.response_repository, engine.mutation_receipts)
    response_service = AcceptedResponseService(coordinator, tier_admission_policy(engine.config.get("capacity", 1)))
    result = ResolutionAccountingFinalizer(engine, response_service, max_requests=max_requests)
    return result


def enable_graph_resolvers(engine: Engram, *, vector: bool = False) -> None:
    """Mark an injected graph capability ready for resolver-planning tests."""
    client = type("ReadyGraphCapability", (), {"available": True})()
    engine.internal_graph_client = client
    engine.config["graph"]["enabled"] = True
    if vector:
        engine.config["graph"]["vector_enabled"] = True
        engine.graph_embedding_model = ReadyEmbeddingModel()


def frame(
    engine: Engram,
    request: str = "What is Engram?",
    *,
    namespace: str = "tenant-a",
    required_metadata=(),
    required_source_label: str = "",
    budget: object = EMPTY_MAPPING,
):
    if budget is EMPTY_MAPPING:
        selected_budget = capture_resolution_budget(lambda: START_NS)
    elif isinstance(budget, dict):
        selected_budget = budget
    else:
        raise ValueError("test budget must be a dictionary")
    result = QueryFrameBuilder(engine, lambda: START_NS, lambda: NOW).build(
        request,
        scope_key(namespace=namespace),
        required_metadata=required_metadata if isinstance(required_metadata, dict) else {},
        required_source_label=required_source_label,
        diagnostic_seed=f"test:{request}:{namespace}",
        budget=selected_budget,
    )
    return result


def resolver_budget(query_frame) -> dict:
    budget = query_frame["budget"]
    result = build_resolver_budget(
        max_candidates=budget["max_candidates"],
        max_graph_rows=budget["max_graph_rows"],
        max_vector_results=budget["max_vector_results"],
        max_evidence=budget["max_evidence"],
        max_evidence_bytes=budget["max_evidence_bytes"],
        max_output_bytes=budget["max_output_bytes"],
        max_diagnostic_bytes=budget["max_diagnostic_bytes"],
        max_working_memory_bytes=budget["max_working_memory_bytes"],
    )
    return result


def candidate(
    statement_id: str = "stmt-candidate",
    *,
    source: CandidateSource = CandidateSource.SPARSE,
    response: str = "Candidate response",
    evidence: tuple[dict, ...] = (),
    features: dict[str, float] = DEFAULT_CANDIDATE_FEATURES,
) -> dict:
    result = resolution_candidate(
        candidate_id=f"candidate:{source.value}:{statement_id}",
        statement_id=statement_id,
        response=response,
        source=source,
        features=feature_set(values=features),
        evidence=evidence,
        scope=scope_key(),
        lifecycle=LifecycleState.ACTIVE,
    )
    return result


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
    accepted = artifact()
    engine = engine_with_artifacts(accepted)
    query_frame = frame(
        engine,
        "Explain Engram",
        required_metadata={"approved": True},
        required_source_label="tapestry:released",
    )

    result = ExactResolver(engine, lambda: START_NS).resolve(query_frame, resolver_budget(query_frame))

    assert result["state"] == ResolverState.COMPLETED
    assert result["reason_code"] == "exact_found"
    assert result["candidates"][0]["response"] == accepted["response"]
    assert result["candidates"][0]["provenance"]["retrieval_origin"] == "alias"
    assert result["accounting"] == (accounting_observation(accepted["statement_id"]),)

    excluded = ExactResolver(engine, lambda: START_NS).resolve(
        query_frame_with_changes(query_frame, {"required_metadata": {"approved": False}}),
        resolver_budget(query_frame),
    )
    assert excluded["candidates"] == ()
    assert excluded["reason_code"] == "exact_required_filter_excluded"


def test_adapter_candidate_ids_are_stable_within_and_distinct_across_requests() -> None:
    accepted = artifact()
    engine = engine_with_artifacts(accepted)
    first_frame = frame(engine, "Explain Engram")
    second_frame = query_frame_with_changes(first_frame, {"diagnostic_id": "resolution:sha256:" + "a" * 64})
    resolver = ExactResolver(engine, lambda: START_NS)

    first = resolver.resolve(first_frame, resolver_budget(first_frame))["candidates"][0]
    replay = resolver.resolve(first_frame, resolver_budget(first_frame))["candidates"][0]
    second = resolver.resolve(second_frame, resolver_budget(second_frame))["candidates"][0]

    assert first["candidate_id"] == replay["candidate_id"]
    assert first["candidate_id"] != second["candidate_id"]


def test_exact_adapter_abstains_for_wrong_scope_and_ineligible_lifecycle() -> None:
    retired = artifact(lifecycle=LifecycleState.RETIRED)
    engine = engine_with_artifacts(retired)

    retired_result = ExactResolver(engine, lambda: START_NS).resolve(frame(engine), resolver_budget(frame(engine)))
    wrong_scope_frame = frame(engine, namespace="tenant-b")
    wrong_scope = ExactResolver(engine, lambda: START_NS).resolve(wrong_scope_frame, resolver_budget(wrong_scope_frame))

    assert retired_result["candidates"] == ()
    assert wrong_scope["candidates"] == ()


def structured_proposition_projection(proposition_id: str = "proposition-1") -> dict:
    row = {
        "proposition_id": proposition_id,
        "subject_entity_id": "entity:ada",
        "predicate_id": "predicate:built",
        "object_entity_id": "entity:engine",
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
        "supplied_trust_version": 0,
        "supplied_trust_version_available": False,
        "structured_match": 1.0,
        "structured_match_available": True,
        "semantic_similarity": 0.0,
        "semantic_similarity_available": False,
    }
    result = proposition_projection_from_graph_row(row, PropositionProjectionQuery.STRUCTURED_ENTITY_V1)
    return result


def changed_proposition_projection(projection: dict, **changes) -> dict:
    values = dict(projection)
    values.update(changes)
    result = proposition_projection(**values)
    return result


def internal_current_proposition_projection(discovered: dict, **changes) -> dict:
    result = changed_proposition_projection(
        discovered,
        projection_id=PropositionProjectionQuery.BY_ID_V1,
        structured_match=0.0,
        structured_match_available=False,
        semantic_similarity=0.0,
        semantic_similarity_available=False,
        vector_index_id="",
        vector_index_id_available=False,
        **changes,
    )
    return result


def semantic_proposition_projection(proposition_id: str = "proposition-1", similarity: float = 0.9) -> dict:
    result = changed_proposition_projection(
        structured_proposition_projection(proposition_id),
        projection_id=PropositionProjectionQuery.VECTOR_V1,
        structured_match=0.0,
        structured_match_available=False,
        semantic_similarity=similarity,
        semantic_similarity_available=True,
        vector_index_id="proposition_premise_embeddings",
        vector_index_id_available=True,
    )
    return result


def proposition_projection_graph_row(projection: dict) -> dict[str, object]:
    encoded = proposition_projection_to_dict(projection)
    result = {field: encoded[field] for field in PROPOSITION_PROJECTION_FIELDS}
    return result


def test_proposition_resolvers_reject_falsey_invalid_eligibility_evaluator() -> None:
    engine = Engram()

    with pytest_raises(InvalidRequestError, match="structured graph eligibility_evaluator"):
        StructuredGraphResolver(engine, lambda: START_NS, False)
    with pytest_raises(InvalidRequestError, match="support semantic eligibility_evaluator"):
        SupportSemanticResolver(engine, lambda: START_NS, False)


def test_structured_graph_adapter_emits_full_proposition_in_current_core_result(monkeypatch) -> None:
    engine = Engram()
    discovered = structured_proposition_projection()
    current = internal_current_proposition_projection(discovered)
    monkeypatch.setattr(
        engine,
        "structured_proposition_projections",
        lambda internal_text, row_limit, cooperative_check=(), max_working_memory_bytes=0: [discovered][:row_limit],
    )
    monkeypatch.setattr(engine, "current_proposition_projection", lambda internal_proposition_id: (current,))
    query_frame = frame(engine, "Ada", namespace="")

    result = StructuredGraphResolver(engine, lambda: START_NS).resolve(query_frame, resolver_budget(query_frame))

    assert result["candidates"] == ()
    assert result["schema_version"] == 1
    assert result["evidence"] == ()
    assert len(result["proposition_evidence"]) == 1
    record = result["proposition_evidence"][0]
    assert record["proposition_id"] == "proposition-1"
    assert record["canonical_references"]["subject_entity_id"] == "entity:ada"
    assert record["features"]["values"]["structured_match"] == 1.0
    assert record["features"]["unavailable"] == ("semantic_similarity", "source_agreement", "supplied_trust")
    assert record["disclosure"]["scope"] == query_frame["scope"]
    serialized = proposition_evidence_record_to_dict(record)
    assert "response" not in serialized
    assert not {"subject", "predicate", "object", "proof", "cypher", "embedding"}.intersection(serialized)
    assert resolver_result_from_json(resolver_result_to_json(result)) == result


def test_structured_graph_adapter_excludes_ineligible_and_changed_propositions(monkeypatch) -> None:
    engine = Engram()
    eligible = structured_proposition_projection("proposition-eligible")
    inactive = changed_proposition_projection(
        structured_proposition_projection("proposition-inactive"),
        invalidated_at="2026-08-01T00:00:00Z",
        invalidated_at_available=True,
    )
    changed = structured_proposition_projection("proposition-changed")
    monkeypatch.setattr(
        engine,
        "structured_proposition_projections",
        lambda internal_text, row_limit, cooperative_check=(), max_working_memory_bytes=0: [eligible, inactive, changed][
            :row_limit
        ],
    )

    def current(proposition_id):
        if proposition_id == eligible["proposition_id"]:
            result = (internal_current_proposition_projection(eligible),)
            return result
        if proposition_id == changed["proposition_id"]:
            result = (internal_current_proposition_projection(changed, object_entity_id="entity:changed"),)
            return result
        raise AssertionError("initially ineligible Proposition must not be revalidated")

    monkeypatch.setattr(engine, "current_proposition_projection", current)
    query_frame = frame(engine, "Ada", namespace="")

    result = StructuredGraphResolver(engine, lambda: START_NS).resolve(query_frame, resolver_budget(query_frame))

    assert tuple(record["proposition_id"] for record in result["proposition_evidence"]) == ("proposition-eligible",)
    assert result["diagnostics"]["discovery_rows"] == 3
    assert result["diagnostics"]["revalidation_rows"] == 2
    assert result["diagnostics"]["exclusion_counts"] == {
        "proposition_inactive": 1,
        "revalidation_identity_conflict": 1,
    }
    assert result["consumption"]["graph_rows"] == 5
    assert result["consumption"]["evidence"] == 1


def test_structured_graph_adapter_honors_evidence_bytes_and_never_mutates(monkeypatch) -> None:
    engine = Engram()
    discovered = structured_proposition_projection()
    current = internal_current_proposition_projection(discovered)
    before = (engine.query_count, engine.hit_count, tuple(engine.statements))
    monkeypatch.setattr(
        engine,
        "structured_proposition_projections",
        lambda internal_text, row_limit, cooperative_check=(), max_working_memory_bytes=0: [discovered][:row_limit],
    )
    monkeypatch.setattr(engine, "current_proposition_projection", lambda internal_proposition_id: (current,))
    query_frame = frame(engine, "Ada", namespace="")
    lease = resolver_budget_with_changes(
        resolver_budget(query_frame),
        {"max_evidence_bytes": 256, "max_output_bytes": 256},
    )

    result = StructuredGraphResolver(engine, lambda: START_NS).resolve(query_frame, lease)

    assert result["proposition_evidence"] == ()
    assert result["reason_code"] == "structured_graph_miss"
    assert result["consumption"]["exhausted_dimensions"] == ("evidence_bytes",)
    assert before == (engine.query_count, engine.hit_count, tuple(engine.statements))


def test_executor_defensively_bounds_full_proposition_evidence_in_current_schema(monkeypatch) -> None:
    engine = Engram()
    discovered = structured_proposition_projection()
    current = internal_current_proposition_projection(discovered)
    monkeypatch.setattr(
        engine,
        "structured_proposition_projections",
        lambda internal_text, row_limit, cooperative_check=(), max_working_memory_bytes=0: [discovered][:row_limit],
    )
    monkeypatch.setattr(engine, "current_proposition_projection", lambda internal_proposition_id: (current,))
    query_frame = frame(engine, "Ada", namespace="")
    lease = resolver_budget(query_frame)
    raw = StructuredGraphResolver(engine, lambda: START_NS).resolve(query_frame, lease)

    bounded = bound_resolver_result(raw, resolver_budget_with_changes(lease, {"max_evidence": 0}))

    assert bounded["schema_version"] == 1
    assert bounded["proposition_evidence"] == ()
    assert bounded["evidence"] == ()
    assert bounded["consumption"]["evidence"] == 0
    assert "evidence" in bounded["consumption"]["exhausted_dimensions"]


def test_support_semantic_adapter_only_returns_support_linked_artifacts(monkeypatch) -> None:
    accepted = artifact(support_references=(PROPOSITION_REFERENCE_A,))
    engine = engine_with_artifacts(accepted)
    engine.config["graph"]["enabled"] = True
    engine.config["graph"]["vector_enabled"] = True
    engine.config["graph"]["vector_weight"] = 1.0
    projections = [semantic_proposition_projection("unlinked", 1.0)]
    monkeypatch.setattr(
        engine,
        "graph_vector_propositions",
        lambda internal_text, *, limit=0: [
            {"proposition_id": REFERENCE_IDS.get("proposition_a", ""), "similarity": 0.9},
            {"proposition_id": "unlinked", "similarity": 1.0},
        ][:limit],
    )
    monkeypatch.setattr(
        engine,
        "graph_vector_proposition_projections",
        lambda internal_text, *, limit=0, cooperative_check=(), max_working_memory_bytes=0: projections[:limit],
    )
    by_id = {projection["proposition_id"]: internal_current_proposition_projection(projection) for projection in projections}
    monkeypatch.setattr(engine, "current_proposition_projection", lambda proposition_id: (by_id[proposition_id],))
    query_frame = frame(engine)

    result = SupportSemanticResolver(engine, lambda: START_NS).resolve(query_frame, resolver_budget(query_frame))

    assert len(result["candidates"]) == 1
    assert result["candidates"][0]["statement_id"] == accepted["statement_id"]
    assert result["candidates"][0]["source"] == CandidateSource.SUPPORT_SEMANTIC
    assert tuple(reference["evidence_id"] for reference in result["candidates"][0]["evidence"]) == (
        REFERENCE_IDS.get("proposition_a", ""),
    )
    assert result["candidates"][0]["features"]["values"]["semantic_score"] == pytest_approx(0.9)
    assert result["candidates"][0]["features"]["values"]["priority"] == pytest_approx(0.0)
    assert result["candidates"][0]["features"]["values"]["retrieval_score"] == pytest_approx(0.9)
    assert tuple(record["proposition_id"] for record in result["proposition_evidence"]) == ("unlinked",)
    assert result["consumption"]["vector_results"] == 3


def test_support_semantic_emits_unlinked_full_proposition_without_response_candidate(monkeypatch) -> None:
    engine = Engram()
    engine.config["graph"]["enabled"] = True
    engine.config["graph"]["vector_enabled"] = True
    engine.config["graph"]["vector_weight"] = 1.0
    discovered = semantic_proposition_projection("proposition-unlinked", 0.73)
    current = internal_current_proposition_projection(discovered)
    monkeypatch.setattr(
        engine,
        "graph_vector_proposition_projections",
        lambda internal_text, *, limit=0, cooperative_check=(), max_working_memory_bytes=0: [discovered][:limit],
    )
    monkeypatch.setattr(engine, "current_proposition_projection", lambda internal_proposition_id: (current,))
    query_frame = frame(engine, "Ada", namespace="")

    result = SupportSemanticResolver(engine, lambda: START_NS).resolve(query_frame, resolver_budget(query_frame))

    assert result["schema_version"] == 1
    assert result["candidates"] == ()
    assert result["accounting"] == ()
    assert tuple(record["proposition_id"] for record in result["proposition_evidence"]) == ("proposition-unlinked",)
    assert result["proposition_evidence"][0]["features"]["values"]["semantic_similarity"] == pytest_approx(0.73)
    assert result["proposition_evidence"][0]["features"]["unavailable"] == (
        "source_agreement",
        "structured_match",
        "supplied_trust",
    )
    assert result["consumption"]["vector_results"] == 1
    assert result["consumption"]["graph_rows"] == 1
    assert result["consumption"]["evidence"] == 1


def test_support_semantic_vertical_fixed_query_to_full_record(monkeypatch) -> None:
    discovered = semantic_proposition_projection("proposition-vertical", 0.67)
    current = internal_current_proposition_projection(discovered)
    client = MemGraphConnection()
    calls = []

    def execute(query: str, parameters=()):
        calls.append((query, parameters))
        if "proposition.subject AS subject" in query:
            result = [{"proposition_id": discovered["proposition_id"], "similarity": discovered["semantic_similarity"]}]
            return result
        if "query_embedding" in parameters:
            result = [proposition_projection_graph_row(discovered)]
            return result
        result = [proposition_projection_graph_row(current)]
        return result

    client.execute = execute
    engine = Engram()
    engine.internal_graph_client = client
    engine.config["graph"].update(
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
    query_frame = frame(engine, "Ada", namespace="")

    result = SupportSemanticResolver(engine, lambda: START_NS).resolve(query_frame, resolver_budget(query_frame))

    assert tuple(record["proposition_id"] for record in result["proposition_evidence"]) == ("proposition-vertical",)
    assert result["proposition_evidence"][0]["features"]["values"]["semantic_similarity"] == pytest_approx(0.67)
    assert len(calls) == 3
    assert calls[0][1] == {
        "index_name": "proposition_premise_embeddings",
        "limit": query_frame["budget"]["max_candidates"],
        "query_embedding": [0.0, 1.0],
        "min_similarity": 0.45,
    }
    assert calls[1][1] == {
        "index_name": "proposition_premise_embeddings",
        "limit": query_frame["budget"]["max_vector_results"] - 1,
        "query_embedding": [0.0, 1.0],
        "min_similarity": 0.45,
    }
    assert calls[2][1] == {"proposition_id": "proposition-vertical"}
    assert "Ada" not in calls[0][0]
    assert "Ada" not in calls[1][0]
    assert "Ada" not in calls[2][0]


def test_support_semantic_proposition_discovery_fails_soft_and_cooperates_with_limits(monkeypatch) -> None:
    engine = Engram()
    enable_graph_resolvers(engine, vector=True)
    query_frame = frame(engine, "Ada", namespace="")
    lease = resolver_budget(query_frame)
    monkeypatch.setattr(
        engine,
        "graph_vector_proposition_projections",
        lambda *internal_args, **internal_kwargs: [],
    )

    unavailable = SupportSemanticResolver(engine, lambda: START_NS).resolve(query_frame, lease)

    assert unavailable["state"] == ResolverState.COMPLETED
    assert unavailable["reason_code"] == "support_semantic_miss"
    assert unavailable["proposition_evidence"] == ()
    assert unavailable["consumption"]["vector_results"] == 0

    monkeypatch.setattr(
        engine,
        "graph_vector_proposition_projections",
        lambda *internal_args, **internal_kwargs: (_ for _ in ()).throw(TimeoutError("deadline")),
    )
    failed = ResolverExecutor(lambda: START_NS).execute(
        query_frame,
        ResolverRegistry((SupportSemanticResolver(engine, lambda: START_NS),)).plan(query_frame),
    )["results"][0]

    assert failed["state"] == ResolverState.FAILED
    assert failed["reason_code"] == "resolver_exception"
    assert failed["diagnostics"]["exception_type"] == "TimeoutError"
    assert failed["consumption"]["exhausted_dimensions"] == ()


def test_executor_isolates_a_malformed_resolver_result() -> None:
    engine = Engram()
    query_frame = frame(engine, "malformed resolver")
    malformed = FakeResolver("malformed", {})

    result = ResolverExecutor(lambda: START_NS).execute(
        query_frame,
        ResolverRegistry((malformed,)).plan(query_frame),
    )[
        "results"
    ][0]

    assert result["state"] == ResolverState.FAILED
    assert result["reason_code"] == "invalid_resolver_result"
    assert result["diagnostics"] == {"exception_type": "InvalidRequestError"}


def test_support_semantic_proposition_evidence_honors_graph_byte_and_memory_bounds(monkeypatch) -> None:
    engine = Engram()
    engine.config["graph"].update({"enabled": True, "vector_enabled": True, "vector_weight": 1.0})
    discovered = semantic_proposition_projection("proposition-bounded", 0.8)
    current = internal_current_proposition_projection(discovered)
    current_calls = 0
    monkeypatch.setattr(
        engine,
        "graph_vector_proposition_projections",
        lambda internal_text, *, limit=0, cooperative_check=(), max_working_memory_bytes=0: [discovered][:limit],
    )

    def current_projection(internal_proposition_id):
        nonlocal current_calls
        current_calls += 1
        result = (current,)
        return result

    monkeypatch.setattr(engine, "current_proposition_projection", current_projection)
    query_frame = frame(engine, "Ada", namespace="")
    lease = resolver_budget(query_frame)

    no_graph_rows = SupportSemanticResolver(engine, lambda: START_NS).resolve(
        query_frame,
        resolver_budget_with_changes(lease, {"max_graph_rows": 0}),
    )
    assert no_graph_rows["proposition_evidence"] == ()
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
    assert byte_limited["proposition_evidence"] == ()
    assert "evidence_bytes" in byte_limited["consumption"]["exhausted_dimensions"]
    assert memory_limited["state"] == ResolverState.EXHAUSTED
    assert memory_limited["reason_code"] == "working_memory_bytes_budget"


def test_executor_runs_semantic_proposition_evidence_after_candidate_capacity_is_consumed(monkeypatch) -> None:
    engine = Engram()
    enable_graph_resolvers(engine, vector=True)
    engine.config["graph"].update({"enabled": True, "vector_enabled": True, "vector_weight": 1.0})
    discovered = semantic_proposition_projection("proposition-after-candidate", 0.8)
    current = internal_current_proposition_projection(discovered)
    monkeypatch.setattr(
        engine,
        "graph_vector_proposition_projections",
        lambda internal_text, *, limit=0, cooperative_check=(), max_working_memory_bytes=0: [discovered][:limit],
    )
    monkeypatch.setattr(engine, "current_proposition_projection", lambda internal_proposition_id: (current,))
    query_frame = frame(
        engine,
        "Ada",
        namespace="",
        budget=capture_resolution_budget(
            lambda: START_NS,
            max_candidates=1,
        ),
    )
    first_candidate = candidate()
    sparse = FakeResolver(
        "sparse",
        resolver_result(
            "sparse",
            ResolverState.COMPLETED,
            candidates=(first_candidate,),
            accounting=(accounting_observation(first_candidate["statement_id"]),),
        ),
    )
    semantic = SupportSemanticResolver(engine, lambda: START_NS)

    report = ResolverExecutor(lambda: START_NS).execute(
        query_frame,
        ResolverRegistry((sparse, semantic)).plan(query_frame),
    )

    assert len(report["results"][0]["candidates"]) == 1
    assert report["reservations"][1]["lease"]["max_candidates"] == 0
    assert tuple(record["proposition_id"] for record in report["results"][1]["proposition_evidence"]) == (
        "proposition-after-candidate",
    )
    assert report["results"][1]["accounting"] == ()


def test_execution_report_canonicalizes_cross_producer_proposition_without_candidacy_or_accounting(monkeypatch) -> None:
    engine = Engram()
    enable_graph_resolvers(engine, vector=True)
    engine.config["graph"].update({"enabled": True, "vector_enabled": True, "vector_weight": 1.0})
    structured = structured_proposition_projection("proposition-shared")
    semantic = semantic_proposition_projection("proposition-shared", 0.76)
    current = internal_current_proposition_projection(structured)
    monkeypatch.setattr(
        engine,
        "structured_proposition_projections",
        lambda internal_text, row_limit, cooperative_check=(), max_working_memory_bytes=0: [structured][:row_limit],
    )
    monkeypatch.setattr(engine, "graph_vector_propositions", lambda internal_text, *, limit=0: [])
    monkeypatch.setattr(
        engine,
        "graph_vector_proposition_projections",
        lambda internal_text, *, limit=0, cooperative_check=(), max_working_memory_bytes=0: [semantic][:limit],
    )
    monkeypatch.setattr(engine, "current_proposition_projection", lambda internal_proposition_id: (current,))
    query_frame = frame(engine, "Ada", namespace="")
    registry = ResolverRegistry(
        (
            StructuredGraphResolver(engine, lambda: START_NS),
            SupportSemanticResolver(engine, lambda: START_NS),
        )
    )

    report = ResolverExecutor(lambda: START_NS).execute(query_frame, registry.plan(query_frame))
    records = execution_report_canonical_proposition_evidence(report)

    assert len(records) == 1
    assert records[0]["proposition_id"] == "proposition-shared"
    assert records[0]["source_contributions"] == ("structured_graph", "support_semantic")
    assert records[0]["features"]["values"]["structured_match"] == 1.0
    assert records[0]["features"]["values"]["semantic_similarity"] == pytest_approx(0.76)
    assert records[0]["features"]["values"]["source_agreement"] == 1.0
    assert all(result["candidates"] == () for result in report["results"])
    assert all(result["accounting"] == () for result in report["results"])

    raw_record = report["results"][0]["proposition_evidence"][0]
    untrusted_record = proposition_evidence_record_with_changes(
        raw_record,
        {"source_resolver": "untrusted", "source_contributions": ("untrusted",)},
    )
    untrusted_report = execution_report_with_changes(
        report,
        {
            "results": (
                resolver_result(
                    "untrusted",
                    ResolverState.COMPLETED,
                    proposition_evidence=(untrusted_record,),
                ),
            ),
        },
    )
    assert execution_report_canonical_proposition_evidence(untrusted_report) == ()

    orchestrated, finalization = ResolutionOrchestrator(
        registry,
        ResolverExecutor(lambda: START_NS),
        accounting_finalizer(engine),
    ).resolve(query_frame, "request-cross-producer-package")

    assert orchestrated["outcome"] == ResolutionOutcome.EVIDENCE
    assert len(orchestrated["evidence_package"]["records"]) == 1
    assert orchestrated["evidence_package"]["records"][0]["source_contributions"] == (
        "structured_graph",
        "support_semantic",
    )
    assert orchestrated["evidence_package"]["records"][0]["features"]["values"]["source_agreement"] == 1.0
    assert all(not result["proposition_evidence"] for result in orchestrated["resolver_results"])
    assert finalization["candidate_statement_ids"] == ()


def test_orchestrator_emits_only_bounded_package_for_proposition_only_evidence(monkeypatch) -> None:
    engine = Engram()
    enable_graph_resolvers(engine)
    discovered = structured_proposition_projection("proposition-orchestrated")
    current = internal_current_proposition_projection(discovered)
    monkeypatch.setattr(
        engine,
        "structured_proposition_projections",
        lambda internal_text, row_limit, cooperative_check=(), max_working_memory_bytes=0: [discovered][:row_limit],
    )
    monkeypatch.setattr(engine, "current_proposition_projection", lambda internal_proposition_id: (current,))
    query_frame = frame(engine, "Ada", namespace="")
    orchestrator = ResolutionOrchestrator(
        ResolverRegistry((StructuredGraphResolver(engine, lambda: START_NS),)),
        ResolverExecutor(lambda: START_NS),
        accounting_finalizer(engine),
    )

    result, finalization = orchestrator.resolve(query_frame, "request-proposition-only")

    assert result["schema_version"] == 1
    assert result["outcome"] == ResolutionOutcome.EVIDENCE
    assert result["selected_candidate_available"] is False
    assert result["response_candidates"] == ()
    assert result["evidence"] == ()
    assert result["evidence_package_available"] is True
    assert tuple(record["proposition_id"] for record in result["evidence_package"]["records"]) == ("proposition-orchestrated",)
    assert all(not resolver_result["proposition_evidence"] for resolver_result in result["resolver_results"])
    assert result["budget"]["evidence"] == 1
    assert result["budget"]["evidence_bytes"] == len(evidence_package_to_json(result["evidence_package"]).encode("utf-8"))
    assert result["budget"]["evidence_bytes"] <= query_frame["budget"]["max_evidence_bytes"]
    assert result["budget"]["graph_rows"] == 2
    assert result["budget"]["vector_results"] == 0
    assert result["budget"]["output_bytes"] == len(resolution_result_to_json(result).encode("utf-8"))
    assert result["budget"]["diagnostic_bytes"] <= query_frame["budget"]["max_diagnostic_bytes"]
    assert result["budget"]["working_memory_bytes"] <= query_frame["budget"]["max_working_memory_bytes"]
    proposition_diagnostics = result["frame_diagnostics"]["proposition_evidence"]
    assert isinstance(proposition_diagnostics, Mapping)
    assert set(proposition_diagnostics) == set(
        {
            "policy_version",
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
    diagnostic_payload = json_dumps(resolution_result_to_dict(result)["frame_diagnostics"], sort_keys=True)
    assert discovered["proposition_id"] not in diagnostic_payload
    assert discovered["subject_entity_id"] not in diagnostic_payload
    assert discovered["object_entity_id"] not in diagnostic_payload
    assert finalization["candidate_statement_ids"] == ()
    assert finalization["accepted_statement_id"] == ""
    assert finalization["success_applied"] is False


def test_orchestrator_keeps_miss_when_proposition_fails_usefulness_policy(monkeypatch) -> None:
    engine = Engram()
    enable_graph_resolvers(engine, vector=True)
    engine.config["graph"].update({"enabled": True, "vector_enabled": True, "vector_weight": 1.0})
    discovered = semantic_proposition_projection("proposition-below-floor", 0.59)
    current = internal_current_proposition_projection(discovered)
    monkeypatch.setattr(engine, "graph_vector_propositions", lambda internal_text, *, limit=0: [])
    monkeypatch.setattr(
        engine,
        "graph_vector_proposition_projections",
        lambda internal_text, *, limit=0, cooperative_check=(), max_working_memory_bytes=0: [discovered][:limit],
    )
    monkeypatch.setattr(engine, "current_proposition_projection", lambda internal_proposition_id: (current,))
    query_frame = frame(engine, "Ada", namespace="")
    orchestrator = ResolutionOrchestrator(
        ResolverRegistry((SupportSemanticResolver(engine, lambda: START_NS),)),
        ResolverExecutor(lambda: START_NS),
        accounting_finalizer(engine),
    )

    result, finalization = orchestrator.resolve(query_frame, "request-proposition-excluded")

    assert result["outcome"] == ResolutionOutcome.MISS
    assert result["evidence_package_available"] is True
    assert result["evidence_package"]["records"] == ()
    assert "proposition_evidence_excluded" in result["reason_codes"]
    diagnostics = result["frame_diagnostics"]["proposition_evidence"]
    assert isinstance(diagnostics, Mapping)
    assert diagnostics["input_count"] == 1
    assert diagnostics["normalized_count"] == 1
    assert diagnostics["included_count"] == 0
    assert diagnostics["excluded_count"] == 1
    assert diagnostics["reason_counts"] == {
        "retrieval_signal_below_floor": 1,
        "supplied_trust_unavailable": 1,
    }
    assert all(not resolver_result["proposition_evidence"] for resolver_result in result["resolver_results"])
    assert result["budget"]["evidence"] == 0
    assert result["budget"]["evidence_bytes"] == len(evidence_package_to_json(result["evidence_package"]).encode("utf-8"))
    assert result["budget"]["graph_rows"] == 1
    assert result["budget"]["vector_results"] == 1
    assert finalization["candidate_statement_ids"] == ()
    assert finalization["success_applied"] is False


def test_orchestrator_retains_response_candidate_evidence_when_proposition_is_excluded(monkeypatch) -> None:
    engine = Engram()
    enable_graph_resolvers(engine, vector=True)
    statement_id = engine.store("Candidate response")
    engine.config["graph"].update({"enabled": True, "vector_enabled": True, "vector_weight": 1.0})
    discovered = semantic_proposition_projection("proposition-below-floor-with-candidate", 0.59)
    current = internal_current_proposition_projection(discovered)
    monkeypatch.setattr(engine, "graph_vector_propositions", lambda internal_text, *, limit=0: [])
    monkeypatch.setattr(
        engine,
        "graph_vector_proposition_projections",
        lambda internal_text, *, limit=0, cooperative_check=(), max_working_memory_bytes=0: [discovered][:limit],
    )
    monkeypatch.setattr(engine, "current_proposition_projection", lambda internal_proposition_id: (current,))
    sparse_candidate = candidate(statement_id)
    sparse = FakeResolver(
        "sparse",
        resolver_result(
            "sparse",
            ResolverState.COMPLETED,
            candidates=(sparse_candidate,),
        ),
    )
    query_frame = frame(engine, "Ada", namespace="")
    orchestrator = ResolutionOrchestrator(
        ResolverRegistry((sparse, SupportSemanticResolver(engine, lambda: START_NS))),
        ResolverExecutor(lambda: START_NS),
        accounting_finalizer(engine),
        CandidateFusionEngine(authority=permissive_candidate_authority),
    )

    result, finalization = orchestrator.resolve(query_frame, "request-candidate-plus-excluded-proposition")

    assert result["outcome"] == ResolutionOutcome.EVIDENCE
    assert tuple(value["statement_id"] for value in result["response_candidates"]) == (statement_id,)
    assert result["evidence_package_available"] is True
    assert result["evidence_package"]["records"] == ()
    assert "proposition_evidence_excluded" in result["reason_codes"]
    assert finalization["candidate_statement_ids"] == ()
    assert finalization["success_applied"] is False


def test_orchestrator_canonically_truncates_proposition_package_to_ten_records(monkeypatch) -> None:
    engine = Engram()
    enable_graph_resolvers(engine)
    discovered = tuple(structured_proposition_projection(f"proposition-{index:02d}") for index in range(12))
    current = {projection["proposition_id"]: internal_current_proposition_projection(projection) for projection in discovered}
    monkeypatch.setattr(
        engine,
        "structured_proposition_projections",
        lambda internal_text, row_limit, cooperative_check=(), max_working_memory_bytes=0: list(discovered[:row_limit]),
    )
    monkeypatch.setattr(engine, "current_proposition_projection", lambda proposition_id: (current[proposition_id],))
    query_frame = frame(engine, "Ada", namespace="")
    orchestrator = ResolutionOrchestrator(
        ResolverRegistry((StructuredGraphResolver(engine, lambda: START_NS),)),
        ResolverExecutor(lambda: START_NS),
        accounting_finalizer(engine),
    )

    result, finalization = orchestrator.resolve(query_frame, "request-proposition-count-limit")

    assert result["outcome"] == ResolutionOutcome.EVIDENCE
    assert tuple(record["proposition_id"] for record in result["evidence_package"]["records"]) == tuple(
        f"proposition-{index:02d}" for index in range(10)
    )
    assert result["evidence_package"]["retained_count"] == 10
    assert result["evidence_package"]["omitted_count"] == 2
    assert result["evidence_package"]["truncated"] is True
    assert result["evidence_package"]["truncation_reasons"] == (EvidencePackageTruncationReason.RECORD_LIMIT,)
    assert result["budget"]["evidence"] == 10
    assert result["budget"]["evidence_bytes"] == len(evidence_package_to_json(result["evidence_package"]).encode("utf-8"))
    assert result["budget"]["output_bytes"] == len(resolution_result_to_json(result).encode("utf-8"))
    assert finalization["candidate_statement_ids"] == ()


def test_orchestrator_trims_proposition_package_to_complete_output_budget(monkeypatch) -> None:
    engine = Engram()
    enable_graph_resolvers(engine)
    discovered = tuple(structured_proposition_projection(f"proposition-output-{index:02d}") for index in range(4))
    current = {projection["proposition_id"]: internal_current_proposition_projection(projection) for projection in discovered}
    monkeypatch.setattr(
        engine,
        "structured_proposition_projections",
        lambda internal_text, row_limit, cooperative_check=(), max_working_memory_bytes=0: list(discovered[:row_limit]),
    )
    monkeypatch.setattr(engine, "current_proposition_projection", lambda proposition_id: (current[proposition_id],))
    selected_budget = capture_resolution_budget(
        lambda: START_NS,
        max_output_bytes=4_096,
    )
    query_frame = frame(engine, "Ada", namespace="", budget=selected_budget)
    orchestrator = ResolutionOrchestrator(
        ResolverRegistry((StructuredGraphResolver(engine, lambda: START_NS),)),
        ResolverExecutor(lambda: START_NS),
        accounting_finalizer(engine),
    )

    result, finalization = orchestrator.resolve(query_frame, "request-proposition-output-limit")

    assert result["outcome"] == ResolutionOutcome.EVIDENCE
    assert 0 < result["evidence_package"]["retained_count"] < len(discovered)
    assert result["evidence_package"]["truncated"] is True
    assert "output_truncated" in result["reason_codes"]
    assert "output_bytes" in result["budget"]["exhausted_dimensions"]
    assert result["budget"]["output_bytes"] == len(resolution_result_to_json(result).encode("utf-8"))
    assert result["budget"]["output_bytes"] <= selected_budget["max_output_bytes"]
    assert all(not resolver_result["proposition_evidence"] for resolver_result in result["resolver_results"])
    assert finalization["candidate_statement_ids"] == ()


def test_orchestrator_fits_package_to_aggregate_evidence_byte_budget(monkeypatch) -> None:
    engine = Engram()
    enable_graph_resolvers(engine)
    probe_projection = structured_proposition_projection("proposition-byte-00")
    monkeypatch.setattr(
        engine,
        "structured_proposition_projections",
        lambda internal_text, row_limit, cooperative_check=(), max_working_memory_bytes=0: [probe_projection][:row_limit],
    )
    monkeypatch.setattr(
        engine,
        "current_proposition_projection",
        lambda internal_proposition_id: (internal_current_proposition_projection(probe_projection),),
    )
    probe_frame = frame(engine, "Ada", namespace="")
    probe_result = StructuredGraphResolver(engine, lambda: START_NS).resolve(
        probe_frame,
        resolver_budget(probe_frame),
    )
    probe_record = probe_result["proposition_evidence"][0]
    single_package_bytes = len(evidence_package_to_json(build_evidence_package((probe_record,))).encode("utf-8"))

    discovered = tuple(structured_proposition_projection(f"proposition-byte-{index:02d}") for index in range(2))
    current = {projection["proposition_id"]: internal_current_proposition_projection(projection) for projection in discovered}
    monkeypatch.setattr(
        engine,
        "structured_proposition_projections",
        lambda internal_text, row_limit, cooperative_check=(), max_working_memory_bytes=0: list(discovered[:row_limit]),
    )
    monkeypatch.setattr(engine, "current_proposition_projection", lambda proposition_id: (current[proposition_id],))
    selected_budget = capture_resolution_budget(
        lambda: START_NS,
        max_evidence_bytes=single_package_bytes,
    )
    query_frame = frame(engine, "Ada", namespace="", budget=selected_budget)
    orchestrator = ResolutionOrchestrator(
        ResolverRegistry((StructuredGraphResolver(engine, lambda: START_NS),)),
        ResolverExecutor(lambda: START_NS),
        accounting_finalizer(engine),
    )

    result, finalization = orchestrator.resolve(query_frame, "request-proposition-evidence-byte-limit")

    assert result["outcome"] == ResolutionOutcome.EVIDENCE
    assert tuple(record["proposition_id"] for record in result["evidence_package"]["records"]) == ("proposition-byte-00",)
    assert result["budget"]["evidence"] == 1
    assert result["budget"]["evidence_bytes"] == len(evidence_package_to_json(result["evidence_package"]).encode("utf-8"))
    assert result["budget"]["evidence_bytes"] <= single_package_bytes
    assert "evidence_bytes" in result["budget"]["exhausted_dimensions"]
    assert finalization["candidate_statement_ids"] == ()


def test_orchestrator_omits_diagnostics_without_losing_proposition_package(monkeypatch) -> None:
    engine = Engram()
    enable_graph_resolvers(engine)
    discovered = structured_proposition_projection("proposition-no-diagnostics")
    current = internal_current_proposition_projection(discovered)
    monkeypatch.setattr(
        engine,
        "structured_proposition_projections",
        lambda internal_text, row_limit, cooperative_check=(), max_working_memory_bytes=0: [discovered][:row_limit],
    )
    monkeypatch.setattr(engine, "current_proposition_projection", lambda internal_proposition_id: (current,))
    selected_budget = capture_resolution_budget(
        lambda: START_NS,
        max_diagnostic_bytes=0,
    )
    query_frame = frame(engine, "Ada", namespace="", budget=selected_budget)
    orchestrator = ResolutionOrchestrator(
        ResolverRegistry((StructuredGraphResolver(engine, lambda: START_NS),)),
        ResolverExecutor(lambda: START_NS),
        accounting_finalizer(engine),
    )

    result, finalization = orchestrator.resolve(query_frame, "request-proposition-diagnostic-limit")

    assert result["outcome"] == ResolutionOutcome.EVIDENCE
    assert tuple(record["proposition_id"] for record in result["evidence_package"]["records"]) == ("proposition-no-diagnostics",)
    assert result["frame_diagnostics"] == {}
    assert result["budget"]["diagnostic_bytes"] == 0
    assert "diagnostic_bytes" in result["budget"]["exhausted_dimensions"]
    assert "diagnostics_truncated" in result["reason_codes"]
    assert finalization["candidate_statement_ids"] == ()


def test_orchestrator_refuses_proposition_package_when_post_fusion_memory_is_exhausted(monkeypatch) -> None:
    engine = Engram()
    enable_graph_resolvers(engine)
    discovered = structured_proposition_projection("proposition-memory-bound")
    current = internal_current_proposition_projection(discovered)
    monkeypatch.setattr(
        engine,
        "structured_proposition_projections",
        lambda internal_text, row_limit, cooperative_check=(), max_working_memory_bytes=0: [discovered][:row_limit],
    )
    monkeypatch.setattr(engine, "current_proposition_projection", lambda internal_proposition_id: (current,))
    base_frame = frame(engine, "Ada", namespace="")
    registry = ResolverRegistry((StructuredGraphResolver(engine, lambda: START_NS),))
    executor = ResolverExecutor(lambda: START_NS)
    probe_execution = executor.execute(base_frame, registry.plan(base_frame))
    fusion = CandidateFusionEngine(authority=permissive_candidate_authority)
    fusion_required = fusion.decide(
        base_frame,
        (),
        (),
        working_memory_limit=(
            base_frame["budget"]["max_working_memory_bytes"] - probe_execution["consumption"]["working_memory_bytes"]
        ),
        working_memory_limit_available=True,
    )["working_memory_bytes"]
    memory_limit = probe_execution["consumption"]["working_memory_bytes"] + fusion_required
    constrained_frame = query_frame_with_changes(
        base_frame,
        {"budget": resolution_budget_with_changes(base_frame["budget"], {"max_working_memory_bytes": memory_limit})},
    )
    orchestrator = ResolutionOrchestrator(
        registry,
        executor,
        accounting_finalizer(engine),
        fusion,
    )

    result, finalization = orchestrator.resolve(constrained_frame, "request-proposition-memory-limit")

    assert result["outcome"] == ResolutionOutcome.MISS
    assert result["evidence_package_available"] is False
    assert result["evidence_package"]["records"] == ()
    assert "proposition_evidence_memory_exhausted" in result["reason_codes"]
    assert "working_memory_bytes" in result["budget"]["exhausted_dimensions"]
    assert result["budget"]["working_memory_bytes"] == memory_limit
    assert all(not resolver_result["proposition_evidence"] for resolver_result in result["resolver_results"])
    assert finalization["candidate_statement_ids"] == ()


def test_orchestrator_rejects_cross_producer_proposition_conflict_without_leaking_records(monkeypatch) -> None:
    engine = Engram()
    discovered = structured_proposition_projection("proposition-conflict")
    current = internal_current_proposition_projection(discovered)
    monkeypatch.setattr(
        engine,
        "structured_proposition_projections",
        lambda internal_text, row_limit, cooperative_check=(), max_working_memory_bytes=0: [discovered][:row_limit],
    )
    monkeypatch.setattr(engine, "current_proposition_projection", lambda internal_proposition_id: (current,))
    query_frame = frame(engine, "Ada", namespace="")
    resolver_output = StructuredGraphResolver(engine, lambda: START_NS).resolve(
        query_frame,
        resolver_budget(query_frame),
    )
    record = resolver_output["proposition_evidence"][0]
    conflicting = proposition_evidence_record_with_changes(
        record,
        {
            "source_resolver": "support_semantic",
            "source_contributions": ("support_semantic",),
            "canonical_references": canonical_proposition_references_with_changes(
                record["canonical_references"],
                {"object_entity_id": "entity:conflict"},
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
        accounting_finalizer(engine),
    )

    result, finalization = orchestrator.resolve(query_frame, "request-proposition-conflict")

    assert result["outcome"] == ResolutionOutcome.MISS
    assert result["evidence_package_available"] is False
    assert result["evidence_package"]["records"] == ()
    assert "proposition_evidence_conflict" in result["reason_codes"]
    assert all(not resolver_result["proposition_evidence"] for resolver_result in result["resolver_results"])
    assert finalization["candidate_statement_ids"] == ()


@pytest_mark.parametrize("mismatch", ("scope", "evaluation_time"))
def test_orchestrator_rejects_proposition_not_bound_to_current_frame(monkeypatch, mismatch: str) -> None:
    engine = Engram()
    discovered = structured_proposition_projection(f"proposition-{mismatch}-mismatch")
    current = internal_current_proposition_projection(discovered)
    monkeypatch.setattr(
        engine,
        "structured_proposition_projections",
        lambda internal_text, row_limit, cooperative_check=(), max_working_memory_bytes=0: [discovered][:row_limit],
    )
    monkeypatch.setattr(engine, "current_proposition_projection", lambda internal_proposition_id: (current,))
    query_frame = frame(engine, "Ada", namespace="")
    resolver_output = StructuredGraphResolver(engine, lambda: START_NS).resolve(
        query_frame,
        resolver_budget(query_frame),
    )
    record = resolver_output["proposition_evidence"][0]
    if mismatch == "scope":
        record = proposition_evidence_record_with_changes(
            record,
            {
                "disclosure": disclosure_decision_with_changes(
                    record["disclosure"],
                    {"scope": scope_key(namespace="other")},
                )
            },
        )
    else:
        record = proposition_evidence_record_with_changes(
            record,
            {
                "validity": proposition_validity_inputs_with_changes(
                    record["validity"],
                    {"evaluation_time": "2026-08-17T12:00:00Z"},
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
        accounting_finalizer(engine),
    )

    result, finalization = orchestrator.resolve(query_frame, f"request-{mismatch}-mismatch")

    assert result["outcome"] == ResolutionOutcome.MISS
    assert result["evidence_package_available"] is False
    assert result["evidence_package"]["records"] == ()
    assert "proposition_evidence_conflict" in result["reason_codes"]
    assert all(not resolver_result["proposition_evidence"] for resolver_result in result["resolver_results"])
    assert finalization["candidate_statement_ids"] == ()


def test_orchestrator_ignores_proposition_from_untrusted_producer(monkeypatch) -> None:
    engine = Engram()
    discovered = structured_proposition_projection("proposition-untrusted-producer")
    current = internal_current_proposition_projection(discovered)
    monkeypatch.setattr(
        engine,
        "structured_proposition_projections",
        lambda internal_text, row_limit, cooperative_check=(), max_working_memory_bytes=0: [discovered][:row_limit],
    )
    monkeypatch.setattr(engine, "current_proposition_projection", lambda internal_proposition_id: (current,))
    query_frame = frame(engine, "Ada", namespace="")
    resolver_output = StructuredGraphResolver(engine, lambda: START_NS).resolve(
        query_frame,
        resolver_budget(query_frame),
    )
    trusted_record = resolver_output["proposition_evidence"][0]
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
        accounting_finalizer(engine),
    )

    result, finalization = orchestrator.resolve(query_frame, "request-untrusted-proposition-producer")

    assert result["outcome"] == ResolutionOutcome.MISS
    assert result["evidence_package_available"] is False
    assert result["evidence_package"]["records"] == ()
    assert "proposition_evidence_untrusted_producer" in result["reason_codes"]
    assert all(not resolver_result["proposition_evidence"] for resolver_result in result["resolver_results"])
    assert finalization["candidate_statement_ids"] == ()


def test_orchestrator_fails_soft_when_proposition_producer_dependency_fails() -> None:
    engine = Engram()
    query_frame = frame(engine, "Ada", namespace="")
    failed = FakeResolver(
        "structured_graph",
        resolver_result("structured_graph", ResolverState.COMPLETED),
        error=True,
    )
    orchestrator = ResolutionOrchestrator(
        ResolverRegistry((failed,)),
        ResolverExecutor(lambda: START_NS),
        accounting_finalizer(engine),
    )

    result, finalization = orchestrator.resolve(query_frame, "request-proposition-dependency-failure")

    assert result["outcome"] == ResolutionOutcome.MISS
    assert result["evidence_package_available"] is False
    assert result["evidence_package"]["records"] == ()
    assert tuple(value["state"] for value in result["resolver_results"]) == (ResolverState.FAILED,)
    assert finalization["candidate_statement_ids"] == ()
    assert finalization["success_applied"] is False


def test_orchestrator_keeps_package_unavailable_when_producer_has_no_strict_records() -> None:
    engine = Engram()
    query_frame = frame(engine, "Ada", namespace="")
    empty = FakeResolver(
        "structured_graph",
        resolver_result("structured_graph", ResolverState.COMPLETED, reason_code="structured_graph_miss"),
    )
    orchestrator = ResolutionOrchestrator(
        ResolverRegistry((empty,)),
        ResolverExecutor(lambda: START_NS),
        accounting_finalizer(engine),
    )

    result, finalization = orchestrator.resolve(query_frame, "request-empty-proposition-producer")

    assert result["outcome"] == ResolutionOutcome.MISS
    assert result["evidence_package_available"] is False
    assert result["evidence_package"] == empty_evidence_package()
    assert finalization["candidate_statement_ids"] == ()


def test_registry_plan_is_deterministic_and_records_all_decisions() -> None:
    engine = Engram()
    query_frame = frame(
        engine,
        namespace="",
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

    assert [resolver_contract(entry["resolver"])[0] for entry in plan["entries"]] == ["exact", "sparse", "support_semantic"]
    assert [entry["reason_code"] for entry in plan["entries"]] == ["", "not_configured", "cost_class_disabled"]
    assert resolution_plan_to_dict(plan) == resolution_plan_to_dict(registry.plan(query_frame, ("exact", "support_semantic")))
    with pytest_raises(InvalidRequestError, match="duplicates"):
        registry.plan(query_frame, ("exact", "exact"))


def test_registry_translates_availability_failures_without_aborting_plan() -> None:
    engine = Engram()
    query_frame = frame(engine, namespace="")
    broken = FakeResolver(
        "broken",
        resolver_result("broken", ResolverState.COMPLETED),
        availability_error=True,
    )
    healthy = FakeResolver("healthy", resolver_result("healthy", ResolverState.COMPLETED))

    plan = ResolverRegistry((broken, healthy)).plan(query_frame)
    execution = ResolverExecutor(lambda: START_NS).execute(query_frame, plan)

    assert plan["entries"][0]["reason_code"] == "availability_check_failed"
    assert [result["state"] for result in execution["results"]] == [ResolverState.UNAVAILABLE, ResolverState.COMPLETED]


def test_executor_propagates_cancellation_without_publishing_partial_results() -> None:
    engine = Engram()
    query_frame = frame(engine, namespace="")

    class CancellableResolver(FakeResolver):
        def resolve(self, current_frame, current_budget, cooperative_check=()) -> dict:
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
    query_frame = frame(engine, namespace="")
    resolver = FakeResolver("structured_graph", resolver_result("structured_graph", ResolverState.COMPLETED))
    finalizer = accounting_finalizer(engine)
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


def test_resolver_lease_and_reservation_codecs_are_deterministic() -> None:
    engine = Engram()
    query_frame = frame(engine, namespace="")
    lease = resolver_budget(query_frame)
    reservation = resolver_reservation("sparse", 2, lease, budget_consumption(resolvers=1, candidates=1))

    assert resolver_budget_from_json(resolver_budget_to_json(lease)) == lease
    assert resolver_reservation_from_json(resolver_reservation_to_json(reservation)) == reservation
    with pytest_raises(InvalidRequestError, match="unsupported resolver budget"):
        resolver_budget_with_changes(lease, {"schema_version": 3})
    with pytest_raises(InvalidRequestError, match="unsupported resolver reservation"):
        resolver_reservation_with_changes(reservation, {"schema_version": 2})


def test_executor_isolates_failures_and_preserves_later_success() -> None:
    engine = Engram()
    query_frame = frame(engine, namespace="")
    failed = FakeResolver("first", resolver_result("first", ResolverState.COMPLETED), error=True)
    later_candidate = candidate()
    completed = FakeResolver(
        "second",
        resolver_result(
            "second",
            ResolverState.COMPLETED,
            candidates=(later_candidate,),
            accounting=(accounting_observation(later_candidate["statement_id"]),),
        ),
    )
    registry = ResolverRegistry((failed, completed))

    execution = ResolverExecutor(lambda: START_NS).execute(query_frame, registry.plan(query_frame))

    assert [result["state"] for result in execution["results"]] == [ResolverState.FAILED, ResolverState.COMPLETED]
    assert execution["results"][0]["diagnostics"]["exception_type"] == "RuntimeError"
    assert execution["results"][1]["candidates"] == (later_candidate,)


def test_executor_short_circuits_only_on_one_exact_candidate() -> None:
    engine = Engram()
    query_frame = frame(engine, namespace="")
    exact_candidate = candidate(source=CandidateSource.EXACT)
    exact = FakeResolver(
        "exact",
        resolver_result(
            "exact",
            ResolverState.COMPLETED,
            candidates=(exact_candidate,),
            accounting=(accounting_observation(exact_candidate["statement_id"]),),
        ),
        cost_class=CostClass.EXACT,
    )
    later = FakeResolver("later", resolver_result("later", ResolverState.COMPLETED))

    execution = ResolverExecutor(lambda: START_NS).execute(query_frame, ResolverRegistry((exact, later)).plan(query_frame))

    assert execution["exact_short_circuited"] is True
    assert later.calls == 0
    assert len(execution["results"]) == 1


def test_executor_enforces_nested_evidence_output_diagnostics_and_resource_bounds() -> None:
    engine = Engram()
    reference = evidence_reference("proposition-1", "oversized", EvidenceKind.SUPPORT, scope_key())
    oversized_candidate = candidate(response="x" * 10_000, evidence=(reference, reference))
    raw = resolver_result(
        "oversized",
        ResolverState.COMPLETED,
        candidates=(oversized_candidate,),
        evidence=(reference,),
        accounting=(accounting_observation(oversized_candidate["statement_id"]),),
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
    query_frame = frame(engine, namespace="", budget=selected_budget)

    execution = ResolverExecutor(lambda: START_NS).execute(query_frame, ResolverRegistry((resolver,)).plan(query_frame))
    result = execution["results"][0]

    assert result["candidates"] == ()
    assert result["evidence"] == ()
    assert result["diagnostics"] == {}
    assert result["consumption"]["graph_rows"] == 1
    assert result["consumption"]["vector_results"] == 1
    assert result["consumption"]["evidence"] <= 1
    assert result["consumption"]["evidence_bytes"] <= 1
    assert result["consumption"]["output_bytes"] <= 4_096
    assert result["consumption"]["diagnostic_bytes"] == 0
    assert result["consumption"]["working_memory_bytes"] <= 1
    assert {
        "diagnostic_bytes",
        "evidence_bytes",
        "graph_rows",
        "output_bytes",
        "vector_results",
        "working_memory_bytes",
    }.issubset(result["consumption"]["exhausted_dimensions"])


def test_executor_preserves_unavailable_consumption_measurements() -> None:
    engine = Engram()
    query_frame = frame(engine, namespace="")
    raw = resolver_result(
        "unmeasured",
        ResolverState.COMPLETED,
        consumption=budget_consumption(resolvers=1, measurement_available=False),
    )
    registry = ResolverRegistry((FakeResolver("unmeasured", raw),))

    execution = ResolverExecutor(lambda: START_NS).execute(query_frame, registry.plan(query_frame))
    result = execution["results"][0]

    assert result["consumption"]["measurement_available"] is False


def test_executor_reports_resolver_count_exhaustion() -> None:
    engine = Engram()
    selected_frame = frame(engine, namespace="")
    first = FakeResolver("first", resolver_result("first", ResolverState.COMPLETED))
    second = FakeResolver("second", resolver_result("second", ResolverState.COMPLETED))
    count_budget = resolution_budget_with_changes(selected_frame["budget"], {"max_resolvers": 1})
    count_frame = query_frame_with_changes(selected_frame, {"budget": count_budget})
    count = ResolverExecutor(lambda: START_NS).execute(count_frame, ResolverRegistry((first, second)).plan(count_frame))

    assert count["results"][-1]["state"] == ResolverState.EXHAUSTED
    assert count["results"][-1]["reason_code"] == "resolver_budget"


def test_executor_reports_elapsed_time_without_changing_a_completed_result() -> None:
    engine = Engram()
    query_frame = frame(engine, namespace="")
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
    result = execution["results"][0]

    assert result["state"] == ResolverState.COMPLETED
    assert result["consumption"]["elapsed_ns"] == observed_elapsed_ns
    assert result["consumption"]["exhausted_dimensions"] == ()


def test_accounting_deduplicates_candidates_and_applies_success_once() -> None:
    accepted = artifact(response="Candidate response")
    engine = engine_with_artifacts(accepted)
    statement_id = accepted.get("statement_id", "")
    observation = accounting_observation(statement_id)
    results = (
        resolver_result("exact", ResolverState.COMPLETED, accounting=(observation,)),
        resolver_result("sparse", ResolverState.COMPLETED, accounting=(observation,)),
    )
    finalizer = accounting_finalizer(engine)

    first = finalizer.finalize("request-1", results, statement_id)
    replay = finalizer.finalize("request-1", results, statement_id)

    assert first["candidate_statement_ids"] == (statement_id,)
    assert first["success_applied"] is True
    assert replay["idempotent"] is True
    current = engine.response_repository.get_artifact(statement_id)
    assert current.get("statistics", {}).get("query_count") == 1
    assert current.get("statistics", {}).get("hit_count") == 1
    assert engine.query_count == 0
    assert engine.hit_count == 0
    assert engine.get_statement(statement_id) == {}


def test_accounting_retry_signature_ignores_elapsed_time_and_retention_is_bounded() -> None:
    engine = Engram()
    finalizer = accounting_finalizer(engine, max_requests=1)
    first = (resolver_result("empty", ResolverState.COMPLETED, consumption=budget_consumption(elapsed_ns=1, resolvers=1)),)
    replay = (resolver_result("empty", ResolverState.COMPLETED, consumption=budget_consumption(elapsed_ns=2, resolvers=1)),)

    finalizer.finalize("request-one", first)
    repeated = finalizer.finalize("request-one", replay)
    finalizer.finalize("request-two", first)

    assert repeated["idempotent"] is True
    assert tuple(finalizer.internal_requests) == ("request-two",)


def test_accounting_rejects_invalid_acceptance_before_any_mutation() -> None:
    accepted = artifact()
    engine = engine_with_artifacts(accepted)
    statement_id = accepted.get("statement_id", "")
    results = (
        resolver_result(
            "exact",
            ResolverState.COMPLETED,
            accounting=(accounting_observation(statement_id),),
        ),
    )

    with pytest_raises(InvalidRequestError, match="not an observed candidate"):
        accounting_finalizer(engine).finalize("request-invalid", results, "not-observed")

    assert engine.query_count == 0
    assert engine.response_repository.get_artifact(statement_id).get("statistics", {}).get("query_count") == 0


def test_core_orchestration_exact_answer_is_deterministic_and_retry_safe() -> None:
    accepted = artifact()
    core = EngramCore(engine_with_artifacts(accepted))

    first = core.resolve_request("Explain Engram", "request-exact", namespace="tenant-a", accept_exact=True)
    replay = core.resolve_request("Explain Engram", "request-exact", namespace="tenant-a", accept_exact=True)

    assert first == replay
    assert first is not replay
    assert first["outcome"] == ResolutionOutcome.ANSWER
    assert first["selected_candidate"]["response"] == accepted["response"]
    assert first["evidence_package_available"] is False
    assert first["evidence_package"]["records"] == ()
    assert [result["resolver"] for result in first["resolver_results"]] == ["exact"]
    updated = core.engram.response_repository.get_artifact(accepted["statement_id"])
    assert updated["statistics"]["query_count"] == 1
    assert updated["statistics"]["hit_count"] == 1
    assert core.engram.get_statement(accepted["statement_id"]) == {}
    first["reason_codes"] = ("caller_mutation",)
    first["budget"]["candidates"] = 999
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

    assert replay["outcome"] == ResolutionOutcome.MISS
    assert tuple(core.resolution_requests) == ("request-first",)
    assert tuple(core.resolution_accounting.internal_requests) == ()


def test_output_budget_downgrade_does_not_record_accepted_success() -> None:
    accepted = artifact(response="x" * 1_000)
    core = EngramCore(engine_with_artifacts(accepted))

    result = core.resolve_request(
        "What is Engram?",
        "request-output-downgrade",
        namespace="tenant-a",
        accept_exact=True,
        budget=resolution_budget(max_output_bytes=4_096),
    )

    updated = core.engram.response_repository.get_artifact(accepted["statement_id"])
    accounting = result["frame_diagnostics"]["accounting"]
    assert isinstance(accounting, Mapping)
    assert result["outcome"] == ResolutionOutcome.MISS
    assert "answer_exceeds_output_budget" in result["reason_codes"]
    assert accounting["success_applied"] is False
    assert updated["statistics"]["query_count"] == 1
    assert updated["statistics"]["hit_count"] == 0


def test_exact_accounting_receipt_is_scoped_to_one_process() -> None:
    accepted = artifact()
    first_core = EngramCore(engine_with_artifacts(accepted))
    first_core.resolve_request("Explain Engram", "request-restart", namespace="tenant-a", accept_exact=True)
    restarted_core = EngramCore(engine_with_artifacts(accepted))

    replay = restarted_core.resolve_request(
        "Explain Engram",
        "request-restart",
        namespace="tenant-a",
        accept_exact=True,
    )

    updated = restarted_core.engram.response_repository.get_artifact(accepted["statement_id"])
    assert replay["outcome"] == ResolutionOutcome.ANSWER
    assert updated["statistics"]["query_count"] == 1
    assert updated["statistics"]["hit_count"] == 1
    assert restarted_core.engram.query_count == 0
    assert restarted_core.engram.hit_count == 0


def test_orchestration_returns_evidence_for_non_exact_and_miss_for_no_output() -> None:
    engine = Engram()
    query_frame = frame(engine, namespace="")
    sparse_candidate = candidate()
    sparse = FakeResolver(
        "sparse",
        resolver_result(
            "sparse",
            ResolverState.COMPLETED,
            candidates=(sparse_candidate,),
        ),
    )
    evidence_accounting = accounting_finalizer(engine)
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
        accounting_finalizer(engine),
    )
    miss_result, _ = miss_orchestrator.resolve(query_frame, "request-miss")

    assert evidence_result["outcome"] == ResolutionOutcome.EVIDENCE
    assert evidence_result["selected_candidate_available"] is False
    assert tuple(candidate["statement_id"] for candidate in evidence_result["response_candidates"]) == (
        sparse_candidate["statement_id"],
    )
    assert evidence_result["response_candidates"][0]["features"]["values"]["lexical"] == 1.0
    assert miss_result["outcome"] == ResolutionOutcome.MISS
    assert miss_result["response_candidates"] == ()
    assert miss_result["evidence"] == ()


def test_fused_non_exact_answer_is_fail_soft_and_accounted_once_without_implicit_acceptance() -> None:
    accepted = artifact(
        response="Candidate response",
        namespace="",
        support_references=(PROPOSITION_REFERENCE_A,),
    )
    engine = engine_with_artifacts(accepted)
    statement_id = accepted.get("statement_id", "")
    reference = evidence_reference(PROPOSITION_REFERENCE_A.get("id", ""), "semantic", EvidenceKind.SUPPORT, scope_key())
    sparse_candidate = candidate(
        statement_id,
        source=CandidateSource.SPARSE,
        features={"sparse_score": 0.95},
    )
    semantic_candidate = candidate(
        statement_id,
        source=CandidateSource.SUPPORT_SEMANTIC,
        evidence=(reference,),
        features={"semantic_score": 0.92, "support_coverage": 1.0},
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
        accounting_finalizer(engine),
    )
    query_frame = frame(engine, namespace="")

    result, first = orchestrator.resolve(query_frame, "request-fused", accept_exact=True)
    replay_result, replay = orchestrator.resolve(query_frame, "request-fused", accept_exact=True)

    assert result["outcome"] == ResolutionOutcome.ANSWER
    assert replay_result["outcome"] == ResolutionOutcome.ANSWER
    assert replay_result["selected_candidate"].get("statement_id", "") == statement_id
    assert [value["state"] for value in result["resolver_results"]] == [
        ResolverState.FAILED,
        ResolverState.COMPLETED,
        ResolverState.COMPLETED,
    ]
    assert first["candidate_statement_ids"] == (statement_id,)
    assert first["accepted_statement_id"] == ""
    assert first["success_applied"] is False
    assert replay["idempotent"] is True
    current = engine.response_repository.get_artifact(statement_id)
    assert current.get("statistics", {}).get("query_count") == 1
    assert current.get("statistics", {}).get("hit_count") == 0
    assert engine.get_statement(statement_id) == {}


def test_orchestrator_reserves_remaining_memory_and_reports_fusion_consumption() -> None:
    engine = Engram()
    query_frame = frame(engine, namespace="")
    value = candidate(features={"sparse_score": 0.9})
    raw = resolver_result(
        "sparse",
        ResolverState.COMPLETED,
        candidates=(value,),
    )
    executor = ResolverExecutor(lambda: START_NS)
    probe_resolver = FakeResolver("sparse", raw)
    probe_execution = executor.execute(query_frame, ResolverRegistry((probe_resolver,)).plan(query_frame))
    fusion = CandidateFusionEngine(authority=permissive_candidate_authority)
    fusion_required = fusion.decide(query_frame, (value,))["working_memory_bytes"]
    total_limit = probe_execution["consumption"]["working_memory_bytes"] + fusion_required - 1
    constrained_frame = query_frame_with_changes(
        query_frame,
        {"budget": resolution_budget_with_changes(query_frame["budget"], {"max_working_memory_bytes": total_limit})},
    )
    orchestrator = ResolutionOrchestrator(
        ResolverRegistry((FakeResolver("sparse", raw),)),
        executor,
        accounting_finalizer(engine),
        fusion,
    )

    result, finalization = orchestrator.resolve(constrained_frame, "request-fusion-memory")

    assert result["outcome"] == ResolutionOutcome.MISS
    assert FusionPolicyReason.FUSION_MEMORY_EXHAUSTED.value in result["reason_codes"]
    assert "working_memory_bytes" in result["budget"]["exhausted_dimensions"]
    assert result["budget"]["working_memory_bytes"] == total_limit
    assert finalization["success_applied"] is False


def test_complete_result_serialization_obeys_and_reports_output_budget() -> None:
    engine = Engram()
    selected_budget = capture_resolution_budget(
        lambda: START_NS,
        max_output_bytes=4_096,
    )
    query_frame = frame(engine, namespace="", budget=selected_budget)
    empty = FakeResolver("empty", resolver_result("empty", ResolverState.COMPLETED))
    orchestrator = ResolutionOrchestrator(
        ResolverRegistry((empty,)),
        ResolverExecutor(lambda: START_NS),
        accounting_finalizer(engine),
    )

    result, _ = orchestrator.resolve(query_frame, "request-output-envelope")
    encoded_size = len(resolution_result_to_json(result).encode("utf-8"))

    assert encoded_size <= query_frame["budget"]["max_output_bytes"]
    assert result["budget"]["output_bytes"] == encoded_size


def test_complete_result_truncates_variable_payload_to_output_budget() -> None:
    engine = Engram()
    selected_budget = capture_resolution_budget(
        lambda: START_NS,
        max_output_bytes=4_096,
    )
    query_frame = frame(engine, namespace="", budget=selected_budget)
    large_candidate = candidate(response="x" * 2_500)
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
        accounting_finalizer(engine),
        CandidateFusionEngine(authority=permissive_candidate_authority),
    )

    result, _ = orchestrator.resolve(query_frame, "request-output-payload")
    encoded_size = len(resolution_result_to_json(result).encode("utf-8"))

    assert encoded_size <= query_frame["budget"]["max_output_bytes"]
    assert result["budget"]["output_bytes"] == encoded_size
    assert "output_truncated" in result["reason_codes"]
    assert "output_bytes" in result["budget"]["exhausted_dimensions"]
