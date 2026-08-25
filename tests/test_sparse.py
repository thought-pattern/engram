"""Section 12 fielded sparse retrieval contracts and integration."""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from types import MappingProxyType

import pytest

from engram.artifacts import (
    CachedResponseArtifact,
    LifecycleState,
    artifact_provenance,
    artifact_statistics,
    cached_response_artifact,
)
from engram.config import engram_config, sparse_config
from engram.constants import FUSION_SOURCE_FAMILY, CandidateSource, Tier
from engram.core import Engram
from engram.identity import build_retrieval_representation, build_standalone_identity, scope_key
from engram.repository import ArtifactRepository
from engram.responses import LifecycleMutationReason
from engram.service import EngramCore, open_engram_core
from engram.sparse import (
    SPARSE_FIELD_WEIGHTS,
    SPARSE_INDEX_VERSION,
    SparseIndexOwner,
    build_sparse_index_state,
    sparse_document_from_artifact,
    sparse_tokens,
    technical_identifiers,
)


def artifact(
    statement_id: str,
    request: str,
    response: str,
    *,
    aliases: tuple[str, ...] = (),
    namespace: str = "tenant-a",
    lifecycle: LifecycleState = LifecycleState.ACTIVE,
    generation: int = 1,
    tier: Tier = Tier.STATIC,
) -> CachedResponseArtifact:
    scope = scope_key(namespace=namespace)
    return cached_response_artifact(
        statement_id=statement_id,
        generation=generation,
        response=response,
        query_identity=build_standalone_identity(request, scope),
        retrieval=build_retrieval_representation(request, aliases),
        tier=tier,
        lifecycle=lifecycle,
        scope=scope,
        support_claim_ids=(),
        valid_from="",
        valid_from_available=False,
        valid_until="",
        valid_until_available=False,
        knowledge_epoch=0,
        knowledge_epoch_available=False,
        superseded_by="replacement" if lifecycle == LifecycleState.SUPERSEDED else "",
        provenance=artifact_provenance("sparse-test", "regulator", "2026-08-21T12:00:00Z"),
        statistics=artifact_statistics(),
        metadata={},
    )


def sparse_engine(*artifacts: CachedResponseArtifact) -> Engram:
    engine = Engram(config=engram_config(sparse=sparse_config(enabled=True)))
    engine.response_repository = ArtifactRepository(artifacts)
    assert engine.synchronize_sparse_index(engine.response_repository.snapshot()) is True
    return engine


def test_sparse_document_has_versioned_weighted_fields_without_response_text_by_default() -> None:
    accepted = artifact(
        "technical",
        "How do I fix ERR_CONN_RESET in libfoo v2.4.1 at api/client.py?",
        "Keep this accepted answer out of the intent index.",
        aliases=("libfoo connection reset",),
    )

    document = sparse_document_from_artifact(accepted)

    assert document["schema_version"] == 1
    assert tuple(document["fields"]) == tuple(SPARSE_FIELD_WEIGHTS)
    assert document["fields"]["canonical"] == ("how do i fix err_conn_reset in libfoo v2.4.1 at api/client.py?",)
    assert document["fields"]["aliases"] == ("libfoo connection reset",)
    assert document["fields"]["response_text"] == ()
    assert "err_conn_reset" in document["technical_identifiers"]
    assert "v2.4.1" in document["technical_identifiers"]
    assert "api/client.py" in document["technical_identifiers"]
    assert SPARSE_FIELD_WEIGHTS == {
        "canonical": 3.0,
        "aliases": 2.5,
        "entities": 2.25,
        "relation": 2.0,
        "keywords": 1.5,
        "technical_identifiers": 3.5,
        "response_text": 0.25,
    }


def test_sparse_response_text_is_an_explicit_low_weight_opt_in() -> None:
    accepted = artifact("response-opt-in", "Where is the API guide?", "Internal phrase only in accepted prose")

    default = sparse_document_from_artifact(accepted)
    enabled = sparse_document_from_artifact(accepted, include_response_text=True)

    assert default["fields"]["response_text"] == ()
    assert enabled["fields"]["response_text"] == ("internal phrase only in accepted prose",)
    assert SPARSE_FIELD_WEIGHTS["response_text"] < min(
        weight for name, weight in SPARSE_FIELD_WEIGHTS.items() if name != "response_text"
    )


def test_technical_tokenization_preserves_identifiers_and_exposes_language_components() -> None:
    text = "ERR_CONN_RESET libfoo v2.4.1 api/client.py std::vector C++ RFC-9110"

    identifiers = technical_identifiers(text)
    tokens = sparse_tokens(text)

    assert identifiers == (
        "err_conn_reset",
        "v2.4.1",
        "api/client.py",
        "std::vector",
        "c++",
        "rfc-9110",
    )
    assert {"err", "conn", "reset", "libfoo", "v2", "4", "1", "api", "client", "py"}.issubset(tokens)


def test_sparse_search_ranks_phrase_proximity_and_technical_matches_deterministically() -> None:
    phrase = artifact("phrase", "Configure OAuth token refresh for the API client", "phrase answer")
    scattered = artifact("scattered", "OAuth setup with client token rotation and later refresh", "scattered answer")
    error = artifact("error", "Resolve ERR_CONN_RESET in libfoo v2.4.1", "error answer")
    engine = sparse_engine(phrase, scattered, error)
    scope = scope_key(namespace="tenant-a")

    phrase_result = engine.sparse_candidates(
        "oauth token refresh",
        scope,
        limit=3,
        max_working_memory_bytes=1_000_000,
    )
    technical_result = engine.sparse_candidates(
        "ERR_CONN_RESE libfoo v2.4",
        scope,
        limit=3,
        max_working_memory_bytes=1_000_000,
    )

    assert phrase_result["complete"] is True
    assert [match["statement_id"] for match in phrase_result["matches"]][:2] == ["phrase", "scattered"]
    assert phrase_result["matches"][0]["phrase_fields"]
    assert phrase_result["matches"][0]["score"] > phrase_result["matches"][1]["score"]
    assert technical_result["matches"][0]["statement_id"] == "error"
    assert technical_result["matches"][0]["character_ngram_similarity"] > 0.0
    assert 0.0 <= technical_result["matches"][0]["score"] <= 1.0


def test_sparse_search_is_scope_isolated_and_abstains_on_resource_exhaustion() -> None:
    first = artifact("tenant-a", "shared technical request alpha", "A", namespace="tenant-a")
    second = artifact("tenant-b", "shared technical request beta", "B", namespace="tenant-b")
    state = build_sparse_index_state(
        (first, second),
        repository_state_generation=1,
        settings=sparse_config(enabled=True),
    )
    owner = SparseIndexOwner(sparse_config(enabled=True, max_posting_visits=1))
    owner.rebuild((first, second), 1)

    scoped = sparse_engine(first, second).sparse_candidates(
        "shared technical request",
        scope_key(namespace="tenant-a"),
        limit=10,
        max_working_memory_bytes=1_000_000,
    )
    exhausted = owner.search(
        "shared technical request",
        scope_key(namespace="tenant-a"),
        limit=10,
        max_working_memory_bytes=1_000_000,
    )

    assert state["index_version"] == SPARSE_INDEX_VERSION
    assert [match["statement_id"] for match in scoped["matches"]] == ["tenant-a"]
    assert exhausted["complete"] is False
    assert exhausted["matches"] == ()
    assert exhausted["reason"] == "posting_visit_budget"


def test_sparse_scope_isolation_also_applies_to_technical_fallback_selection() -> None:
    target = artifact("target", "Resolve ERR_CONN_RESET in libfoo", "target", namespace="tenant-a")
    other_scope = artifact("other", "Resolve ERR_CONN_RESE in libfoo", "other", namespace="tenant-b")
    engine = sparse_engine(target, other_scope)

    result = engine.sparse_candidates(
        "ERR_CONN_RESE libfoo",
        scope_key(namespace="tenant-a"),
        limit=5,
        max_working_memory_bytes=1_000_000,
    )

    assert [match["statement_id"] for match in result["matches"]] == ["target"]
    assert result["matches"][0]["character_ngram_similarity"] > 0.0


def test_sparse_abstains_instead_of_truncating_an_overlong_query() -> None:
    accepted = artifact("bounded", "alpha beta gamma delta", "bounded")
    owner = SparseIndexOwner(sparse_config(enabled=True, max_query_terms=2))
    owner.rebuild((accepted,), 1)

    result = owner.search(
        "alpha beta gamma",
        scope_key(namespace="tenant-a"),
        limit=5,
        max_working_memory_bytes=1_000_000,
    )

    assert result["complete"] is False
    assert result["matches"] == ()
    assert result["reason"] == "query_term_budget"
    assert result["query_term_count"] == 3


def test_sparse_index_check_detects_corruption_and_atomic_rebuild_repairs_it() -> None:
    accepted = artifact("repair", "repair the sparse index", "repaired")
    owner = SparseIndexOwner(sparse_config(enabled=True))
    owner.rebuild((accepted,), 7)
    live = owner.snapshot()
    corrupt = dict(live)
    corrupt["postings"] = MappingProxyType({})
    owner._state = corrupt  # type: ignore[assignment]

    broken = owner.check_against((accepted,), 7)
    repaired = owner.rebuild((accepted,), 7)
    checked = owner.check_against((accepted,), 7)

    assert broken["consistent"] is False
    assert "posting_content_mismatch" in broken["issues"]
    assert repaired["state_generation"] > live["state_generation"]
    assert checked == {
        "consistent": True,
        "repository_state_generation": 7,
        "document_count": 1,
        "issues": (),
    }


def test_sparse_generations_are_deeply_read_only_and_unhealthy_state_rebuilds() -> None:
    accepted = artifact("immutable", "immutable sparse generation", "immutable")
    owner = SparseIndexOwner(sparse_config(enabled=True))
    state = owner.rebuild((accepted,), 1)

    with pytest.raises(TypeError):
        state["documents"] = MappingProxyType({})  # type: ignore[typeddict-item]
    with pytest.raises(TypeError):
        state["documents"]["immutable"]["lifecycle"] = LifecycleState.INVALIDATED

    owner.mark_unavailable(RuntimeError("simulated derived-state failure"))
    recovered = owner.synchronize({"immutable": accepted}, 2, ())

    assert recovered["repository_state_generation"] == 2
    assert owner.available is True
    assert owner.check_against((accepted,), 2)["consistent"] is True


def test_disabled_sparse_index_does_not_project_authoritative_content() -> None:
    accepted = artifact("disabled", "disabled sparse content", "disabled")
    engine = Engram(config=engram_config())
    engine.response_repository = ArtifactRepository((accepted,))

    assert engine.synchronize_sparse_index(engine.response_repository.snapshot()) is True
    assert engine.sparse_index_snapshot()["documents"] == {}
    assert engine.check_sparse_index()["consistent"] is True


def test_sparse_component_status_tracks_fail_soft_unavailability_and_recovery() -> None:
    accepted = artifact("status", "sparse status request", "sparse status response")
    engine = sparse_engine(accepted)
    core = EngramCore(engine)

    assert core.status()["components"]["sparse"] == {"enabled": True, "ready": True}

    engine._sparse_index_owner.mark_unavailable(RuntimeError("simulated sparse failure"))
    degraded = core.status()

    assert degraded["ready"] is True
    assert degraded["healthy"] is True
    assert degraded["components"]["sparse"] == {"enabled": True, "ready": False}

    engine.rebuild_sparse_index()
    assert core.status()["components"]["sparse"] == {"enabled": True, "ready": True}


def test_sparse_and_legacy_lexical_scores_are_one_correlated_fusion_family() -> None:
    assert FUSION_SOURCE_FAMILY[CandidateSource.LEXICAL] == "lexical"
    assert FUSION_SOURCE_FAMILY[CandidateSource.SPARSE] == "lexical"


def test_sparse_resolver_emits_common_candidates_features_diagnostics_and_budget() -> None:
    accepted = artifact(
        "resolver",
        "Troubleshoot libfoo ERR_CONN_RESET",
        "Restart the libfoo transport.",
    )
    engine = sparse_engine(accepted)
    core = EngramCore(engine, clock=lambda: datetime(2026, 8, 21, 12, 30, tzinfo=UTC))
    postings_before = engine.sparse_index_snapshot()["postings"]

    result = core.resolve_request(
        "libfoo ERR_CONN_RESE troubleshooting",
        "sparse-resolution",
        namespace="tenant-a",
        configured_resolvers=("sparse",),
    )

    sparse_result = next(item for item in result["resolver_results"] if item["resolver"] == "sparse")
    candidate = sparse_result["candidates"][0]
    assert sparse_result["reason_code"] == "sparse_candidates"
    assert sparse_result["consumption"]["resolvers"] == 1
    assert sparse_result["consumption"]["working_memory_bytes"] > 0
    assert candidate["source"] == CandidateSource.SPARSE
    assert 0.0 < candidate["features"]["values"]["sparse_score"] <= 1.0
    assert "field_contributions" in candidate["diagnostics"]
    posting_visits = sparse_result["diagnostics"]["posting_visits"]
    assert isinstance(posting_visits, int) and posting_visits > 0
    assert engine.response_repository.get_artifact("resolver")["statistics"]["query_count"] == 1
    assert engine.sparse_index_snapshot()["postings"] is postings_before
    assert engine.check_sparse_index()["consistent"] is True
    core.close(flush=False)


def test_sparse_index_tracks_commit_supersession_lifecycle_and_capacity_eviction() -> None:
    engine = Engram(config=engram_config(capacity=1, sparse=sparse_config(enabled=True)))
    core = EngramCore(engine, clock=lambda: datetime(2026, 8, 21, 13, 0, tzinfo=UTC))
    mutations = core._response_mutations
    old = artifact("old", "Old sparse request", "old", tier=Tier.STATIC)
    replacement = artifact("replacement", "Old sparse request", "replacement", tier=Tier.STATIC)

    mutations.commit_response(old, "commit-old")
    assert set(engine.sparse_index_snapshot()["documents"]) == {"old"}
    mutations.supersede_response(
        "old",
        1,
        replacement,
        LifecycleMutationReason.STALE,
        "operator",
        "supersede-old",
    )

    assert set(engine.sparse_index_snapshot()["documents"]) == {"old", "replacement"}
    assert engine.sparse_index_snapshot()["documents"]["old"]["lifecycle"] == LifecycleState.SUPERSEDED
    resolved = core.resolve_request(
        "Old sparse request variation",
        "after-supersession",
        namespace="tenant-a",
        configured_resolvers=("sparse",),
    )
    sparse_result = next(item for item in resolved["resolver_results"] if item["resolver"] == "sparse")
    assert [candidate["statement_id"] for candidate in sparse_result["candidates"]] == ["replacement"]

    current_generation = engine.response_repository.get_artifact("replacement")["generation"]
    mutations.invalidate_response(
        "replacement",
        current_generation,
        LifecycleMutationReason.SOURCE_RETRACTED,
        "operator",
        "invalidate-replacement",
    )
    assert engine.sparse_index_snapshot()["documents"]["replacement"]["lifecycle"] == LifecycleState.INVALIDATED
    invalidated = core.resolve_request(
        "Old sparse request variation",
        "after-invalidation",
        namespace="tenant-a",
        configured_resolvers=("sparse",),
    )
    invalidated_sparse = next(item for item in invalidated["resolver_results"] if item["resolver"] == "sparse")
    assert invalidated_sparse["candidates"] == ()

    dynamic_one = artifact("dynamic-one", "first dynamic sparse item", "one", tier=Tier.DYNAMIC)
    dynamic_two = artifact("dynamic-two", "second dynamic sparse item", "two", tier=Tier.DYNAMIC)
    mutations.commit_response(dynamic_one, "commit-dynamic-one")
    mutations.commit_response(dynamic_two, "commit-dynamic-two")
    assert "dynamic-one" not in engine.sparse_index_snapshot()["documents"]
    assert "dynamic-two" in engine.sparse_index_snapshot()["documents"]
    assert engine.check_sparse_index()["consistent"] is True
    core.close(flush=False)


def test_sparse_index_rebuilds_from_persistence_and_never_requires_persisted_postings(tmp_path) -> None:
    store = tmp_path / "sparse-state.json"
    config = engram_config(sparse=sparse_config(enabled=True))
    core = open_engram_core(config=config, store_path=str(store))
    core._response_mutations.commit_response(
        artifact("persisted", "persistent sparse technical key v9.4", "persisted"),
        "commit-persisted",
    )
    fingerprint = core.engram.sparse_index_snapshot()["fingerprint"]
    core.close()

    raw = store.read_text(encoding="utf-8")
    restored = open_engram_core(config=config, store_path=str(store))

    assert '"postings"' not in raw
    assert restored.engram.sparse_index_snapshot()["fingerprint"] == fingerprint
    assert restored.engram.check_sparse_index()["consistent"] is True
    restored.close(flush=False)


def test_sparse_readers_observe_only_complete_atomic_generations() -> None:
    first = artifact("first", "alpha technical identifier v1.0", "first")
    second = artifact("second", "beta technical identifier v2.0", "second")
    owner = SparseIndexOwner(sparse_config(enabled=True))
    owner.rebuild((first,), 1)
    observed: list[tuple[str, ...]] = []

    def read() -> None:
        for _ in range(100):
            result = owner.search(
                "technical identifier",
                scope_key(namespace="tenant-a"),
                limit=10,
                max_working_memory_bytes=1_000_000,
            )
            observed.append(tuple(match["statement_id"] for match in result["matches"]))

    with ThreadPoolExecutor(max_workers=5) as executor:
        readers = [executor.submit(read) for _ in range(4)]
        owner.rebuild((second,), 2)
        for reader in readers:
            reader.result()

    assert observed
    assert set(observed).issubset({("first",), ("second",)})
    assert owner.check_against((second,), 2)["consistent"] is True
