"""Request-local sparse retrieval tests."""

from engram import sparse as sparse_module
from engram.artifacts import LifecycleState, artifact_provenance, artifact_statistics, cached_response_artifact
from engram.config import engram_config, sparse_config
from engram.constants import Tier
from engram.core import Engram
from engram.identity import build_retrieval_representation, build_standalone_identity, scope_key
from engram.repository import ArtifactRepository
from engram.sparse import sparse_document_from_artifact, sparse_tokens, technical_identifiers


def artifact(
    statement_id: str,
    request: str,
    response: str,
    *,
    aliases: tuple[str, ...] = (),
    namespace: str = "tenant-a",
    lifecycle: LifecycleState = LifecycleState.ACTIVE,
) -> dict:
    scope = scope_key(namespace=namespace)
    result = cached_response_artifact(
        statement_id=statement_id,
        generation=1,
        response=response,
        query_identity=build_standalone_identity(request, scope),
        retrieval=build_retrieval_representation(request, aliases),
        tier=Tier.STATIC,
        lifecycle=lifecycle,
        scope=scope,
        support_references=(),
        valid_from="",
        valid_from_available=False,
        valid_until="",
        valid_until_available=False,
        superseded_by="replacement" if lifecycle == LifecycleState.SUPERSEDED else "",
        provenance=artifact_provenance("sparse-test", "regulator", "2026-08-21T12:00:00Z"),
        statistics=artifact_statistics(),
        metadata={},
    )
    return result


def sparse_engine(*artifacts: dict, **changes) -> Engram:
    configuration = sparse_config(enabled=True, **changes)
    engine = Engram(config=engram_config(sparse=configuration))
    engine.response_repository = ArtifactRepository(artifacts)
    return engine


def search(engine: Engram, text: str, namespace: str = "tenant-a", **changes) -> dict:
    options = {"limit": 5, "max_working_memory_bytes": 1_000_000}
    options.update(changes)
    result = engine.sparse_candidates(text, scope_key(namespace=namespace), **options)
    return result


def test_sparse_document_uses_request_fields_without_response_text_by_default() -> None:
    accepted = artifact(
        "technical",
        "How do I fix ERR_CONN_RESET in libfoo v2.4.1 at api/client.py?",
        "Keep this answer prose out of retrieval.",
        aliases=("libfoo connection reset",),
    )

    document = sparse_document_from_artifact(accepted)

    assert document["fields"]["response_text"] == ()
    assert document["fields"]["aliases"] == ("libfoo connection reset",)
    assert {"err_conn_reset", "v2.4.1", "api/client.py"}.issubset(document["technical_identifiers"])


def test_technical_tokenization_preserves_identifiers_and_language_components() -> None:
    text = "ERR_CONN_RESET libfoo v2.4.1 api/client.py std::vector C++ RFC-9110"

    identifiers = technical_identifiers(text)
    tokens = sparse_tokens(text)

    assert identifiers == ("err_conn_reset", "v2.4.1", "api/client.py", "std::vector", "c++", "rfc-9110")
    assert {"err", "conn", "reset", "libfoo", "api", "client"}.issubset(tokens)


def test_sparse_search_ranks_phrase_and_technical_matches_deterministically() -> None:
    phrase = artifact("phrase", "Configure OAuth token refresh for the API client", "phrase answer")
    scattered = artifact("scattered", "OAuth setup with client token rotation and later refresh", "scattered answer")
    error = artifact("error", "Resolve ERR_CONN_RESET in libfoo v2.4.1", "error answer")
    engine = sparse_engine(phrase, scattered, error)

    phrase_result = search(engine, "oauth token refresh")
    technical_result = search(engine, "ERR_CONN_RESE libfoo v2.4")

    assert [match["statement_id"] for match in phrase_result["matches"]][:2] == ["phrase", "scattered"]
    assert phrase_result["matches"][0]["score"] > phrase_result["matches"][1]["score"]
    assert technical_result["matches"][0]["statement_id"] == "error"
    assert technical_result["matches"][0]["character_ngram_similarity"] > 0.0


def test_sparse_search_is_scope_and_lifecycle_isolated() -> None:
    first = artifact("tenant-a", "shared request", "A", namespace="tenant-a")
    second = artifact("tenant-b", "shared request", "B", namespace="tenant-b")
    retired = artifact("retired", "shared request", "C", lifecycle=LifecycleState.RETIRED)
    engine = sparse_engine(first, second, retired)

    result = search(engine, "shared request", namespace="tenant-a")

    assert [match["statement_id"] for match in result["matches"]] == ["tenant-a"]


def test_sparse_search_abstains_when_request_budget_is_exhausted() -> None:
    engine = sparse_engine(
        artifact("first", "shared technical request alpha", "A"),
        artifact("second", "shared technical request beta", "B"),
        max_posting_visits=1,
    )

    result = search(engine, "shared technical request")

    assert result["complete"] is False
    assert result["reason"] == "posting_visit_budget"


def test_sparse_search_abstains_when_its_query_state_exceeds_the_budget() -> None:
    accepted = tuple(
        artifact(f"artifact-{index}", f"technical request {index} with repeated searchable terms", "A") for index in range(20)
    )
    engine = sparse_engine(*accepted)

    result = search(engine, "technical request", max_working_memory_bytes=1_000)

    assert result.get("complete", True) is False
    assert result.get("reason", "") == "working_memory_budget"
    assert result.get("working_memory_bytes", 0) <= 1_000


def test_sparse_documents_are_derived_once_and_statistics_changes_do_not_rederive(monkeypatch) -> None:
    accepted = tuple(artifact(f"artifact-{index}", f"technical request {index}", "A") for index in range(5))
    engine = sparse_engine(*accepted)
    derived = []
    original = sparse_module.sparse_document_from_validated_artifact

    def observe(artifact_value: dict, include_response_text: bool) -> dict:
        derived.append(artifact_value.get("statement_id", ""))
        result = original(artifact_value, include_response_text)
        return result

    monkeypatch.setattr(sparse_module, "sparse_document_from_validated_artifact", observe)
    search(engine, "technical request")
    search(engine, "technical request 3")
    assert sorted(derived) == sorted(value["statement_id"] for value in accepted)

    # A resolve replaces an artifact to update its statistics; its document is unchanged.
    counted = {**accepted[0], "generation": 2, "statistics": artifact_statistics(1, 1, "2026-08-22T12:00:00Z", True)}
    engine.response_repository = ArtifactRepository((counted, *accepted[1:]))
    search(engine, "technical request")
    assert len(derived) == len(accepted)


def test_sparse_construction_does_not_charge_other_scopes() -> None:
    unrelated = tuple(
        artifact(f"other-{index}", f"unrelated technical material {index} " + "noise " * 400, "B", namespace="tenant-b")
        for index in range(20)
    )
    target = artifact("target", "target technical request", "A", namespace="tenant-a")
    engine = sparse_engine(*unrelated, target)

    result = search(engine, "target technical request", namespace="tenant-a", max_working_memory_bytes=100_000)

    matches = result.get("matches", ())
    assert result.get("complete", False) is True
    assert [match.get("statement_id", "") for match in matches] == ["target"]
    assert result.get("working_memory_bytes", 100_001) <= 100_000


def test_sparse_search_uses_each_current_artifact_snapshot() -> None:
    engine = sparse_engine(artifact("first", "first unique request", "A"))
    before = search(engine, "first unique request")
    engine.response_repository = ArtifactRepository((artifact("second", "second unique request", "B"),))
    after = search(engine, "second unique request")

    assert [match["statement_id"] for match in before["matches"]] == ["first"]
    assert [match["statement_id"] for match in after["matches"]] == ["second"]
    # The index is synced to the snapshot being searched, so nothing is left from the first.
    assert set(engine.sparse_index.internal_entries) == {"second"}


def test_sparse_index_matches_a_full_rebuild_through_random_changes() -> None:
    from random import Random

    random = Random(20260928)
    words = ["alpha", "beta", "gamma", "ERR_CONN_RESET", "v2.4.1", "api/client.py", "libfoo", "reset", "timeout", "k8s"]
    queries = ("connection reset", "ERR_CONN_RESET libfoo", "api/client.py v2.4.1", "gamma timeout", "alpha")
    lifecycles = (LifecycleState.ACTIVE, LifecycleState.ACTIVE, LifecycleState.ACTIVE, LifecycleState.RETIRED)
    namespaces = ("tenant-a", "tenant-a", "tenant-b")

    def random_artifact(statement_id: str) -> dict:
        result = artifact(
            statement_id,
            " ".join(random.choice(words) for _ in range(random.randint(1, 5))),
            "Response " + random.choice(words),
            aliases=tuple(random.choice(words) for _ in range(random.randint(0, 2))),
            namespace=random.choice(namespaces),
            lifecycle=random.choice(lifecycles),
        )
        return result

    for include_response_text in (False, True):
        settings = sparse_config(enabled=True, include_response_text=include_response_text)
        index = sparse_module.SparseIndex()
        current: dict[str, dict] = {}
        for step in range(60):
            action = random.random()
            if action < 0.45 or not current:
                statement_id = f"s-{step}"
                current[statement_id] = random_artifact(statement_id)
            elif action < 0.6:
                del current[random.choice(sorted(current))]
            elif action < 0.8:
                statement_id = random.choice(sorted(current))
                current[statement_id] = {**current[statement_id], "statistics": artifact_statistics(step, step)}
            else:
                statement_id = random.choice(sorted(current))
                current[statement_id] = random_artifact(statement_id)
            snapshot = tuple(current.values())
            for query in queries:
                for namespace in ("tenant-a", "tenant-b"):
                    options = {"limit": 10, "max_working_memory_bytes": 50_000_000, "trusted_artifacts": True}
                    indexed = sparse_module.search_sparse_artifacts(
                        snapshot, query, scope_key(namespace=namespace), settings, index=index, **options
                    )
                    rebuilt = sparse_module.search_sparse_artifacts(
                        snapshot, query, scope_key(namespace=namespace), settings, **options
                    )
                    assert indexed["matches"] == rebuilt["matches"], (step, query, namespace)
                    assert (indexed["complete"], indexed["reason"]) == (rebuilt["complete"], rebuilt["reason"])


def test_sparse_budget_charges_request_structures_so_large_stores_still_match() -> None:
    noise = tuple(artifact(f"noise-{index}", f"archive entry {index} synthetic noise token {index}", "N") for index in range(200))
    target = artifact("target", "What are the baseline support hours?", "Nine to five.")
    engine = sparse_engine(*noise, target)

    # Charging every document's full size needs about 12 KB per artifact,
    # which is over this budget; the request's own structures fit well within it.
    result = search(engine, "baseline support hours schedule", max_working_memory_bytes=1_000_000)

    assert result["complete"] is True
    assert [match["statement_id"] for match in result["matches"]][:1] == ["target"]
    assert result["working_memory_bytes"] <= 1_000_000


def test_disabled_sparse_retrieval_derives_no_artifact_state() -> None:
    engine = Engram(config=engram_config(sparse=sparse_config(enabled=False)))
    engine.response_repository = ArtifactRepository((artifact("first", "first request", "A"),))

    result = search(engine, "first request")

    assert result["complete"] is False
    assert result["reason"] == "sparse_unavailable"
