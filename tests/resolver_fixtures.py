"""Shared artifacts, graph projection rows, and resolver doubles for the resolution conformance tests."""

from datetime import UTC, datetime

from sentence_transformers import SentenceTransformer

from engram.artifacts import validate_cached_response_artifact
from engram.constants_artifacts import INITIAL_ARTIFACT_STATISTICS, LifecycleState, Tier
from engram.constants_graph import PropositionProjectionQuery
from engram.constants_identity import EMPTY_SCOPE_KEY
from engram.constants_resolution import CandidateSource, CostClass
from engram.core import Engram
from engram.identity import extract_standalone_identity, retrieval_representation, scope_key
from engram.resolution import (
    candidate as resolution_candidate,
)
from engram.resolution_evidence import (
    feature_set,
)

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


class ReadyGraphCapability:
    """A connected graph double that implements every read and finds nothing."""

    available = True

    def empty_rows(self, *internal_args, **internal_kwargs) -> list:
        result = []
        return result

    execute = empty_rows
    vector_search_propositions = empty_rows
    vector_search_proposition_projections = empty_rows
    structured_proposition_projections = empty_rows
    canonical_entity_matches = empty_rows
    canonical_predicate_matches = empty_rows
    relation_one_hop_proposition_projections = empty_rows
    proposition_projection_by_id = empty_rows

    def disconnect(self) -> None:
        self.available = False


def enable_graph_resolvers(engine: Engram, *, vector: bool = False) -> None:
    """Mark an injected graph capability ready for resolver-planning tests."""
    client = ReadyGraphCapability()
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
