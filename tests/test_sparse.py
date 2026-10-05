"""Request-local sparse retrieval tests."""

from random import Random

from engram import sparse as sparse_module
from engram.artifacts import validate_cached_response_artifact
from engram.config import engram_config, sparse_config
from engram.constants import INITIAL_ARTIFACT_STATISTICS, LifecycleState, Tier
from engram.core import Engram
from engram.identity import extract_standalone_identity, retrieval_representation, scope_key
from engram.repository import ArtifactRepository
from engram.sparse import sparse_document_from_artifact, sparse_tokens, technical_identifiers

TENANT_A_SCOPE = scope_key(namespace="tenant-a")
TENANT_B_SCOPE = scope_key(namespace="tenant-b")
# Accepted static tenant-a artifact fields; each test adds the statement id, response, identity and retrieval
# representation. validate_cached_response_artifact copies its input, so this constant stays read-only.
SPARSE_ARTIFACT_FIELDS = {
    "generation": 1,
    "tier": Tier.STATIC,
    "lifecycle": LifecycleState.ACTIVE,
    "scope": TENANT_A_SCOPE,
    "support_references": (),
    "valid_from": "",
    "valid_from_available": False,
    "valid_until": "",
    "valid_until_available": False,
    "superseded_by": "",
    "provenance": {"source_label": "sparse-test", "caller_id": "regulator", "accepted_at": "2026-08-21T12:00:00Z"},
    "statistics": INITIAL_ARTIFACT_STATISTICS,
    "metadata": {},
}
SEARCH_RESULT_FIELDS = {"matches", "complete", "reason"}


def search(engine: Engram, text: str, namespace: str = "tenant-a", **changes) -> dict:
    options = {"limit": 5, "max_working_memory_bytes": 1_000_000}
    options.update(changes)
    result = engine.sparse_candidates(text, scope_key(namespace=namespace), **options)
    return result


def test_sparse_document_uses_request_fields_without_response_text_by_default() -> None:
    request = "How do I fix ERR_CONN_RESET in libfoo v2.4.1 at api/client.py?"
    accepted = validate_cached_response_artifact(
        {
            **SPARSE_ARTIFACT_FIELDS,
            "statement_id": "technical",
            "response": "Keep this answer prose out of retrieval.",
            "query_identity": extract_standalone_identity(request, TENANT_A_SCOPE),
            "retrieval": retrieval_representation(request, ("libfoo connection reset",)),
        }
    )

    document = sparse_document_from_artifact(accepted)
    fields = document.get("fields", {})

    assert "response_text" in fields
    assert fields.get("response_text", ()) == ()
    assert fields.get("aliases", ()) == ("libfoo connection reset",)
    assert {"err_conn_reset", "v2.4.1", "api/client.py"}.issubset(document.get("technical_identifiers", ()))


def test_technical_tokenization_preserves_identifiers_and_language_components() -> None:
    text = "ERR_CONN_RESET libfoo v2.4.1 api/client.py std::vector C++ RFC-9110"

    identifiers = technical_identifiers(text)
    tokens = sparse_tokens(text)

    assert identifiers == ("err_conn_reset", "v2.4.1", "api/client.py", "std::vector", "c++", "rfc-9110")
    assert {"err", "conn", "reset", "libfoo", "api", "client"}.issubset(tokens)


def test_sparse_search_ranks_phrase_and_technical_matches_deterministically() -> None:
    phrase_request = "Configure OAuth token refresh for the API client"
    scattered_request = "OAuth setup with client token rotation and later refresh"
    error_request = "Resolve ERR_CONN_RESET in libfoo v2.4.1"
    phrase = validate_cached_response_artifact(
        {
            **SPARSE_ARTIFACT_FIELDS,
            "statement_id": "phrase",
            "response": "phrase answer",
            "query_identity": extract_standalone_identity(phrase_request, TENANT_A_SCOPE),
            "retrieval": retrieval_representation(phrase_request),
        }
    )
    scattered = validate_cached_response_artifact(
        {
            **SPARSE_ARTIFACT_FIELDS,
            "statement_id": "scattered",
            "response": "scattered answer",
            "query_identity": extract_standalone_identity(scattered_request, TENANT_A_SCOPE),
            "retrieval": retrieval_representation(scattered_request),
        }
    )
    error = validate_cached_response_artifact(
        {
            **SPARSE_ARTIFACT_FIELDS,
            "statement_id": "error",
            "response": "error answer",
            "query_identity": extract_standalone_identity(error_request, TENANT_A_SCOPE),
            "retrieval": retrieval_representation(error_request),
        }
    )
    engine = Engram(config=engram_config(sparse=sparse_config(enabled=True)))
    engine.response_repository = ArtifactRepository((phrase, scattered, error))

    phrase_matches = search(engine, "oauth token refresh").get("matches", [])
    technical_match = search(engine, "ERR_CONN_RESE libfoo v2.4").get("matches", [])[0]

    assert [match.get("statement_id", "") for match in phrase_matches][:2] == ["phrase", "scattered"]
    assert all("score" in match for match in phrase_matches[:2])
    assert phrase_matches[0].get("score", 0.0) > phrase_matches[1].get("score", 0.0)
    assert technical_match.get("statement_id", "") == "error"
    assert technical_match.get("character_ngram_similarity", 0.0) > 0.0


def test_sparse_search_is_scope_and_lifecycle_isolated() -> None:
    first = validate_cached_response_artifact(
        {
            **SPARSE_ARTIFACT_FIELDS,
            "statement_id": "tenant-a",
            "response": "A",
            "query_identity": extract_standalone_identity("shared request", TENANT_A_SCOPE),
            "retrieval": retrieval_representation("shared request"),
        }
    )
    second = validate_cached_response_artifact(
        {
            **SPARSE_ARTIFACT_FIELDS,
            "statement_id": "tenant-b",
            "response": "B",
            "query_identity": extract_standalone_identity("shared request", TENANT_B_SCOPE),
            "retrieval": retrieval_representation("shared request"),
            "scope": TENANT_B_SCOPE,
        }
    )
    retired = validate_cached_response_artifact(
        {
            **SPARSE_ARTIFACT_FIELDS,
            "statement_id": "retired",
            "response": "C",
            "query_identity": extract_standalone_identity("shared request", TENANT_A_SCOPE),
            "retrieval": retrieval_representation("shared request"),
            "lifecycle": LifecycleState.RETIRED,
        }
    )
    engine = Engram(config=engram_config(sparse=sparse_config(enabled=True)))
    engine.response_repository = ArtifactRepository((first, second, retired))

    result = search(engine, "shared request", namespace="tenant-a")

    assert [match.get("statement_id", "") for match in result.get("matches", [])] == ["tenant-a"]


def test_sparse_search_abstains_when_request_budget_is_exhausted() -> None:
    first = validate_cached_response_artifact(
        {
            **SPARSE_ARTIFACT_FIELDS,
            "statement_id": "first",
            "response": "A",
            "query_identity": extract_standalone_identity("shared technical request alpha", TENANT_A_SCOPE),
            "retrieval": retrieval_representation("shared technical request alpha"),
        }
    )
    second = validate_cached_response_artifact(
        {
            **SPARSE_ARTIFACT_FIELDS,
            "statement_id": "second",
            "response": "B",
            "query_identity": extract_standalone_identity("shared technical request beta", TENANT_A_SCOPE),
            "retrieval": retrieval_representation("shared technical request beta"),
        }
    )
    engine = Engram(config=engram_config(sparse=sparse_config(enabled=True, max_posting_visits=1)))
    engine.response_repository = ArtifactRepository((first, second))

    result = search(engine, "shared technical request")

    assert "complete" in result
    assert result.get("complete", False) is False
    assert result.get("reason", "") == "posting_visit_budget"


def test_sparse_search_abstains_when_its_query_state_exceeds_the_budget() -> None:
    accepted = tuple(
        validate_cached_response_artifact(
            {
                **SPARSE_ARTIFACT_FIELDS,
                "statement_id": f"artifact-{index}",
                "response": "A",
                "query_identity": extract_standalone_identity(
                    f"technical request {index} with repeated searchable terms", TENANT_A_SCOPE
                ),
                "retrieval": retrieval_representation(f"technical request {index} with repeated searchable terms"),
            }
        )
        for index in range(20)
    )
    engine = Engram(config=engram_config(sparse=sparse_config(enabled=True)))
    engine.response_repository = ArtifactRepository(accepted)

    result = search(engine, "technical request", max_working_memory_bytes=1_000)

    assert "working_memory_bytes" in result
    assert result.get("complete", True) is False
    assert result.get("reason", "") == "working_memory_budget"
    assert result.get("working_memory_bytes", 0) <= 1_000


def test_sparse_documents_are_derived_once_and_statistics_changes_do_not_rederive(monkeypatch) -> None:
    accepted = tuple(
        validate_cached_response_artifact(
            {
                **SPARSE_ARTIFACT_FIELDS,
                "statement_id": f"artifact-{index}",
                "response": "A",
                "query_identity": extract_standalone_identity(f"technical request {index}", TENANT_A_SCOPE),
                "retrieval": retrieval_representation(f"technical request {index}"),
            }
        )
        for index in range(5)
    )
    engine = Engram(config=engram_config(sparse=sparse_config(enabled=True)))
    engine.response_repository = ArtifactRepository(accepted)
    derived = []
    original = sparse_module.sparse_document_from_validated_artifact

    def observe(artifact_value: dict, include_response_text: bool) -> dict:
        derived.append(artifact_value.get("statement_id", ""))
        result = original(artifact_value, include_response_text)
        return result

    monkeypatch.setattr(sparse_module, "sparse_document_from_validated_artifact", observe)
    search(engine, "technical request")
    search(engine, "technical request 3")
    assert sorted(derived) == sorted(value.get("statement_id", "") for value in accepted)

    # A resolve replaces an artifact to update its statistics; its document is unchanged.
    counted_statistics = {"hit_count": 1, "query_count": 1, "last_hit": "2026-08-22T12:00:00Z", "last_hit_available": True}
    counted = {**accepted[0], "generation": 2, "statistics": counted_statistics}
    engine.response_repository = ArtifactRepository((counted, *accepted[1:]))
    search(engine, "technical request")
    assert len(derived) == len(accepted)


def test_sparse_construction_does_not_charge_other_scopes() -> None:
    unrelated = tuple(
        validate_cached_response_artifact(
            {
                **SPARSE_ARTIFACT_FIELDS,
                "statement_id": f"other-{index}",
                "response": "B",
                "query_identity": extract_standalone_identity(
                    f"unrelated technical material {index} " + "noise " * 400, TENANT_B_SCOPE
                ),
                "retrieval": retrieval_representation(f"unrelated technical material {index} " + "noise " * 400),
                "scope": TENANT_B_SCOPE,
            }
        )
        for index in range(20)
    )
    target = validate_cached_response_artifact(
        {
            **SPARSE_ARTIFACT_FIELDS,
            "statement_id": "target",
            "response": "A",
            "query_identity": extract_standalone_identity("target technical request", TENANT_A_SCOPE),
            "retrieval": retrieval_representation("target technical request"),
        }
    )
    engine = Engram(config=engram_config(sparse=sparse_config(enabled=True)))
    engine.response_repository = ArtifactRepository((*unrelated, target))

    result = search(engine, "target technical request", namespace="tenant-a", max_working_memory_bytes=100_000)

    matches = result.get("matches", ())
    assert "working_memory_bytes" in result
    assert result.get("complete", False) is True
    assert [match.get("statement_id", "") for match in matches] == ["target"]
    assert result.get("working_memory_bytes", 0) <= 100_000


def test_sparse_search_uses_each_current_artifact_snapshot() -> None:
    first = validate_cached_response_artifact(
        {
            **SPARSE_ARTIFACT_FIELDS,
            "statement_id": "first",
            "response": "A",
            "query_identity": extract_standalone_identity("first unique request", TENANT_A_SCOPE),
            "retrieval": retrieval_representation("first unique request"),
        }
    )
    second = validate_cached_response_artifact(
        {
            **SPARSE_ARTIFACT_FIELDS,
            "statement_id": "second",
            "response": "B",
            "query_identity": extract_standalone_identity("second unique request", TENANT_A_SCOPE),
            "retrieval": retrieval_representation("second unique request"),
        }
    )
    engine = Engram(config=engram_config(sparse=sparse_config(enabled=True)))
    engine.response_repository = ArtifactRepository((first,))
    before = search(engine, "first unique request")
    engine.response_repository = ArtifactRepository((second,))
    after = search(engine, "second unique request")

    assert [match.get("statement_id", "") for match in before.get("matches", [])] == ["first"]
    assert [match.get("statement_id", "") for match in after.get("matches", [])] == ["second"]
    # The index is synced to the snapshot being searched, so nothing is left from the first.
    assert set(engine.sparse_index.internal_entries) == {"second"}


def test_sparse_index_matches_a_full_rebuild_through_random_changes() -> None:
    random = Random(20260928)
    words = ["alpha", "beta", "gamma", "ERR_CONN_RESET", "v2.4.1", "api/client.py", "libfoo", "reset", "timeout", "k8s"]
    queries = ("connection reset", "ERR_CONN_RESET libfoo", "api/client.py v2.4.1", "gamma timeout", "alpha")
    lifecycles = (LifecycleState.ACTIVE, LifecycleState.ACTIVE, LifecycleState.ACTIVE, LifecycleState.RETIRED)
    namespaces = ("tenant-a", "tenant-a", "tenant-b")

    for include_response_text in (False, True):
        settings = sparse_config(enabled=True, include_response_text=include_response_text)
        index = sparse_module.SparseIndex()
        current: dict[str, dict] = {}
        for step in range(60):
            action = random.random()
            # Inserts and replacements draw a fresh random artifact for statement_id; "" leaves the store as changed.
            statement_id = ""
            if action < 0.45 or not current:
                statement_id = f"s-{step}"
            elif action < 0.6:
                del current[random.choice(sorted(current))]
            elif action < 0.8:
                counted_id = random.choice(sorted(current))
                statistics = {"hit_count": step, "query_count": step, "last_hit": "", "last_hit_available": False}
                current[counted_id] = {**current.get(counted_id, {}), "statistics": statistics}
            else:
                statement_id = random.choice(sorted(current))
            if statement_id:
                request = " ".join(random.choice(words) for _ in range(random.randint(1, 5)))
                response = "Response " + random.choice(words)
                aliases = tuple(random.choice(words) for _ in range(random.randint(0, 2)))
                scope = scope_key(namespace=random.choice(namespaces))
                lifecycle = random.choice(lifecycles)
                current[statement_id] = validate_cached_response_artifact(
                    {
                        **SPARSE_ARTIFACT_FIELDS,
                        "statement_id": statement_id,
                        "response": response,
                        "query_identity": extract_standalone_identity(request, scope),
                        "retrieval": retrieval_representation(request, aliases),
                        "scope": scope,
                        "lifecycle": lifecycle,
                    }
                )
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
                    assert indexed.keys() >= SEARCH_RESULT_FIELDS
                    assert rebuilt.keys() >= SEARCH_RESULT_FIELDS
                    indexed_state = (indexed.get("complete", False), indexed.get("reason", ""))
                    rebuilt_state = (rebuilt.get("complete", False), rebuilt.get("reason", ""))
                    assert indexed.get("matches", []) == rebuilt.get("matches", []), (step, query, namespace)
                    assert indexed_state == rebuilt_state


def test_sparse_budget_charges_request_structures_so_large_stores_still_match() -> None:
    noise = tuple(
        validate_cached_response_artifact(
            {
                **SPARSE_ARTIFACT_FIELDS,
                "statement_id": f"noise-{index}",
                "response": "N",
                "query_identity": extract_standalone_identity(
                    f"archive entry {index} synthetic noise token {index}", TENANT_A_SCOPE
                ),
                "retrieval": retrieval_representation(f"archive entry {index} synthetic noise token {index}"),
            }
        )
        for index in range(200)
    )
    target = validate_cached_response_artifact(
        {
            **SPARSE_ARTIFACT_FIELDS,
            "statement_id": "target",
            "response": "Nine to five.",
            "query_identity": extract_standalone_identity("What are the baseline support hours?", TENANT_A_SCOPE),
            "retrieval": retrieval_representation("What are the baseline support hours?"),
        }
    )
    engine = Engram(config=engram_config(sparse=sparse_config(enabled=True)))
    engine.response_repository = ArtifactRepository((*noise, target))

    # Charging every document's full size needs about 12 KB per artifact,
    # which is over this budget; the request's own structures fit well within it.
    result = search(engine, "baseline support hours schedule", max_working_memory_bytes=1_000_000)

    assert "working_memory_bytes" in result
    assert result.get("complete", False) is True
    assert [match.get("statement_id", "") for match in result.get("matches", [])][:1] == ["target"]
    assert result.get("working_memory_bytes", 0) <= 1_000_000


def test_disabled_sparse_retrieval_derives_no_artifact_state() -> None:
    first = validate_cached_response_artifact(
        {
            **SPARSE_ARTIFACT_FIELDS,
            "statement_id": "first",
            "response": "A",
            "query_identity": extract_standalone_identity("first request", TENANT_A_SCOPE),
            "retrieval": retrieval_representation("first request"),
        }
    )
    engine = Engram(config=engram_config(sparse=sparse_config(enabled=False)))
    engine.response_repository = ArtifactRepository((first,))

    result = search(engine, "first request")

    assert "complete" in result
    assert result.get("complete", False) is False
    assert result.get("reason", "") == "sparse_unavailable"
