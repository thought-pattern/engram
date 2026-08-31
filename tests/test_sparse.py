"""Request-local sparse retrieval tests."""

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

    assert document["schema_version"] == 1
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


def test_sparse_search_uses_each_current_artifact_snapshot_without_synchronization() -> None:
    engine = sparse_engine(artifact("first", "first unique request", "A"))
    before = search(engine, "first unique request")
    engine.response_repository = ArtifactRepository((artifact("second", "second unique request", "B"),))
    after = search(engine, "second unique request")

    assert [match["statement_id"] for match in before["matches"]] == ["first"]
    assert [match["statement_id"] for match in after["matches"]] == ["second"]
    assert not hasattr(engine, "sparse_index_snapshot")
    assert not hasattr(engine, "synchronize_sparse_index")


def test_disabled_sparse_retrieval_derives_no_artifact_state() -> None:
    engine = Engram(config=engram_config(sparse=sparse_config(enabled=False)))
    engine.response_repository = ArtifactRepository((artifact("first", "first request", "A"),))

    result = search(engine, "first request")

    assert result["complete"] is False
    assert result["reason"] == "sparse_unavailable"
