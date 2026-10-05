"""Request-local semantic retrieval tests."""

from datetime import UTC, datetime
from pathlib import Path

from pytest import raises as pytest_raises

from engram.artifacts import validate_cached_response_artifact
from engram.config import engram_config, semantic_config
from engram.constants import INITIAL_ARTIFACT_STATISTICS, CandidateSource, LifecycleState, Tier
from engram.core import Engram
from engram.errors import ResolutionCancelledError
from engram.identity import extract_standalone_identity, retrieval_representation, scope_key
from engram.repository import ArtifactRepository
from engram.resolution import QueryFrameBuilder
from engram.resolvers import StandaloneSemanticResolver, resolver_budget
from engram.semantic import StandaloneSemanticRetriever, model_artifact_sha256

NOW = datetime(2026, 8, 22, 12, 0, tzinfo=UTC)
TENANT_A_SCOPE = scope_key(namespace="tenant-a")
# Accepted static tenant-a artifact fields; each test adds the statement id, response, identity and retrieval
# representation. validate_cached_response_artifact copies its input, so this constant stays read-only.
SEMANTIC_ARTIFACT_FIELDS = {
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
    "provenance": {"source_label": "semantic-test", "caller_id": "regulator", "accepted_at": "2026-08-22T12:00:00Z"},
    "statistics": INITIAL_ARTIFACT_STATISTICS,
    "metadata": {},
}


class FakeSemanticModel:
    def __init__(self, dimension: int = 4) -> None:
        self.dimension = dimension
        self.encoded_texts: list[str] = []

    def get_sentence_embedding_dimension(self) -> int:
        return self.dimension

    def encode(self, texts, **kwargs):
        del kwargs
        self.encoded_texts.extend(texts)
        values = []
        for text in texts:
            normalized = text.casefold()
            values.append(
                [
                    float(any(token in normalized for token in ("sushi", "japanese", "roll"))),
                    float(any(token in normalized for token in ("cat", "feline", "kitten"))),
                    float(any(token in normalized for token in ("dog", "canine", "puppy"))),
                    0.25,
                ][: self.dimension]
            )
        return values


def settings(tmp_path: Path, **changes) -> dict:
    model_path = tmp_path / "model"
    model_path.mkdir(parents=True, exist_ok=True)
    (model_path / "weights.bin").write_bytes(b"local-test-model")
    (model_path / "LICENSE").write_text("Apache License 2.0", encoding="utf-8")
    values = {
        "enabled": True,
        "model_path": str(model_path),
        "model_version": "test-v1",
        "artifact_sha256": model_artifact_sha256(model_path),
        "dimension": 4,
        "min_similarity": 0.45,
    }
    values.update(changes)
    result = semantic_config(**values)
    return result


def search(value, artifacts, text="japanese rolls", namespace="tenant-a", **changes):
    options = {
        "limit": 5,
        "max_vector_results": 5,
        "max_working_memory_bytes": 1_000_000,
    }
    options.update(changes)
    result = value.search(text, scope_key(namespace=namespace), artifacts, **options)
    return result


def test_semantic_model_is_checksum_license_and_dimension_gated(tmp_path: Path) -> None:
    valid = settings(tmp_path)
    corrupt = dict(valid)
    corrupt["artifact_sha256"] = "0" * 64

    checksum_failure = StandaloneSemanticRetriever(corrupt, model=FakeSemanticModel())
    dimension_failure = StandaloneSemanticRetriever(valid, model=FakeSemanticModel(dimension=3))

    assert checksum_failure.available is False
    assert dimension_failure.available is False


def test_semantic_search_derives_embeddings_from_current_artifacts_only(tmp_path: Path) -> None:
    model = FakeSemanticModel()
    value = StandaloneSemanticRetriever(settings(tmp_path), model=model)
    sushi = validate_cached_response_artifact(
        {
            **SEMANTIC_ARTIFACT_FIELDS,
            "statement_id": "sushi",
            "response": "response prose must not be embedded",
            "query_identity": extract_standalone_identity("best sushi", TENANT_A_SCOPE),
            "retrieval": retrieval_representation("best sushi", ("japanese rolls",)),
        }
    )

    result = search(value, (sushi,))
    first_match = result.get("matches", [])[0]

    assert result.get("complete", False) is True
    assert first_match.get("statement_id", "") == "sushi"
    assert first_match.get("origin", "") in {"canonical", "alias"}
    assert "response prose must not be embedded" not in model.encoded_texts
    assert not hasattr(value, "snapshot")
    assert not hasattr(value, "rebuild")


def test_semantic_search_observes_replacement_snapshot_without_synchronization(tmp_path: Path) -> None:
    value = StandaloneSemanticRetriever(settings(tmp_path), model=FakeSemanticModel())
    first = validate_cached_response_artifact(
        {
            **SEMANTIC_ARTIFACT_FIELDS,
            "statement_id": "sushi",
            "response": "Sushi",
            "query_identity": extract_standalone_identity("best sushi", TENANT_A_SCOPE),
            "retrieval": retrieval_representation("best sushi", ("japanese rolls",)),
        }
    )
    second = validate_cached_response_artifact(
        {
            **SEMANTIC_ARTIFACT_FIELDS,
            "statement_id": "cats",
            "response": "Cats",
            "query_identity": extract_standalone_identity("best cats", TENANT_A_SCOPE),
            "retrieval": retrieval_representation("best cats", ("feline guide",)),
        }
    )

    before = search(value, (first,), text="japanese rolls")
    after = search(value, (second,), text="feline guide")

    assert [match.get("statement_id", "") for match in before.get("matches", [])] == ["sushi"]
    assert [match.get("statement_id", "") for match in after.get("matches", [])] == ["cats"]


def test_semantic_search_is_scope_and_lifecycle_isolated(tmp_path: Path) -> None:
    value = StandaloneSemanticRetriever(settings(tmp_path), model=FakeSemanticModel())
    tenant_b_scope = scope_key(namespace="tenant-b")
    active = validate_cached_response_artifact(
        {
            **SEMANTIC_ARTIFACT_FIELDS,
            "statement_id": "active",
            "response": "A",
            "query_identity": extract_standalone_identity("best sushi", TENANT_A_SCOPE),
            "retrieval": retrieval_representation("best sushi"),
        }
    )
    other = validate_cached_response_artifact(
        {
            **SEMANTIC_ARTIFACT_FIELDS,
            "statement_id": "other",
            "response": "B",
            "query_identity": extract_standalone_identity("best sushi", tenant_b_scope),
            "retrieval": retrieval_representation("best sushi"),
            "scope": tenant_b_scope,
        }
    )
    retired = validate_cached_response_artifact(
        {
            **SEMANTIC_ARTIFACT_FIELDS,
            "statement_id": "retired",
            "response": "C",
            "query_identity": extract_standalone_identity("best sushi", TENANT_A_SCOPE),
            "retrieval": retrieval_representation("best sushi"),
            "lifecycle": LifecycleState.RETIRED,
        }
    )

    result = search(value, (active, other, retired), text="sushi", namespace="tenant-a")

    assert [match.get("statement_id", "") for match in result.get("matches", [])] == ["active"]


def test_semantic_search_honors_budgets_and_cancellation(tmp_path: Path) -> None:
    value = StandaloneSemanticRetriever(settings(tmp_path), model=FakeSemanticModel())
    accepted = validate_cached_response_artifact(
        {
            **SEMANTIC_ARTIFACT_FIELDS,
            "statement_id": "sushi",
            "response": "Sushi",
            "query_identity": extract_standalone_identity("best sushi", TENANT_A_SCOPE),
            "retrieval": retrieval_representation("best sushi"),
        }
    )

    exhausted = search(value, (accepted,), max_vector_results=0)
    assert "complete" in exhausted
    assert exhausted.get("complete", False) is False
    assert exhausted.get("reason", "") == "vector_result_budget"

    def cancelled() -> None:
        raise ResolutionCancelledError("cancelled")

    with pytest_raises(ResolutionCancelledError):
        search(value, (accepted,), cooperative_check=cancelled)


def test_semantic_search_checks_scan_budget_before_encoding_corpus(tmp_path: Path) -> None:
    model = FakeSemanticModel()
    value = StandaloneSemanticRetriever(settings(tmp_path, max_scan_records=1), model=model)
    first = validate_cached_response_artifact(
        {
            **SEMANTIC_ARTIFACT_FIELDS,
            "statement_id": "first",
            "response": "A",
            "query_identity": extract_standalone_identity("first sushi request", TENANT_A_SCOPE),
            "retrieval": retrieval_representation("first sushi request"),
        }
    )
    second = validate_cached_response_artifact(
        {
            **SEMANTIC_ARTIFACT_FIELDS,
            "statement_id": "second",
            "response": "B",
            "query_identity": extract_standalone_identity("second sushi request", TENANT_A_SCOPE),
            "retrieval": retrieval_representation("second sushi request"),
        }
    )

    result = search(value, (first, second), text="sushi")

    assert result.get("complete", True) is False
    assert result.get("reason", "") == "semantic_scan_budget"
    assert result.get("scanned_records", 0) == 2
    assert model.encoded_texts == []


def test_semantic_search_checks_memory_budget_before_encoding_corpus(tmp_path: Path) -> None:
    model = FakeSemanticModel()
    value = StandaloneSemanticRetriever(settings(tmp_path), model=model)
    accepted = validate_cached_response_artifact(
        {
            **SEMANTIC_ARTIFACT_FIELDS,
            "statement_id": "sushi",
            "response": "Sushi",
            "query_identity": extract_standalone_identity("best sushi", TENANT_A_SCOPE),
            "retrieval": retrieval_representation("best sushi"),
        }
    )

    result = search(value, (accepted,), max_working_memory_bytes=1_000)

    assert "working_memory_bytes" in result
    assert result.get("complete", True) is False
    assert result.get("reason", "") == "working_memory_budget"
    assert result.get("working_memory_bytes", 0) <= 1_000
    assert model.encoded_texts == []


def test_semantic_resolver_reads_artifacts_without_a_live_index(tmp_path: Path) -> None:
    accepted = validate_cached_response_artifact(
        {
            **SEMANTIC_ARTIFACT_FIELDS,
            "statement_id": "sushi",
            "response": "Sushi",
            "query_identity": extract_standalone_identity("best sushi", TENANT_A_SCOPE),
            "retrieval": retrieval_representation("best sushi", ("japanese rolls",)),
        }
    )
    configuration = engram_config(semantic=settings(tmp_path))
    engine = Engram(config=configuration)
    engine.semantic_retriever = StandaloneSemanticRetriever(
        configuration.get("semantic", {}),
        model=FakeSemanticModel(),
    )
    engine.response_repository = ArtifactRepository((accepted,))
    frame = QueryFrameBuilder(engine, lambda: 1_000_000_000, lambda: NOW).build(
        "japanese rolls",
        scope_key(namespace="tenant-a"),
    )

    budget = frame.get("budget", {})
    lease = resolver_budget(
        budget.get("max_candidates", 0),
        budget.get("max_graph_rows", 0),
        budget.get("max_vector_results", 0),
        budget.get("max_evidence", 0),
        budget.get("max_evidence_bytes", 0),
        budget.get("max_output_bytes", 0),
        budget.get("max_diagnostic_bytes", 0),
        budget.get("max_working_memory_bytes", 0),
    )
    result = StandaloneSemanticResolver(engine, lambda: 1_000_000_100).resolve(frame, lease)
    first_candidate = result.get("candidates", ())[0]

    assert first_candidate.get("source", CandidateSource.EXACT) == CandidateSource.STANDALONE_SEMANTIC
    assert first_candidate.get("statement_id", "") == "sushi"
    assert engine.get_statement("sushi") == {}


def test_semantic_records_are_reused_only_while_an_artifact_is_unchanged(tmp_path: Path) -> None:
    model = FakeSemanticModel()
    configuration = engram_config(semantic=settings(tmp_path))
    engine = Engram(config=configuration)
    engine.semantic_retriever = StandaloneSemanticRetriever(configuration.get("semantic", {}), model=model)
    sushi = validate_cached_response_artifact(
        {
            **SEMANTIC_ARTIFACT_FIELDS,
            "statement_id": "sushi",
            "response": "Sushi",
            "query_identity": extract_standalone_identity("best sushi", TENANT_A_SCOPE),
            "retrieval": retrieval_representation("best sushi", ("japanese rolls",)),
        }
    )
    cats = validate_cached_response_artifact(
        {
            **SEMANTIC_ARTIFACT_FIELDS,
            "statement_id": "cats",
            "response": "Cats",
            "query_identity": extract_standalone_identity("best cats", TENANT_A_SCOPE),
            "retrieval": retrieval_representation("best cats", ("feline guide",)),
        }
    )
    engine.response_repository = ArtifactRepository((sushi,))
    options = {"limit": 5, "max_vector_results": 10, "max_working_memory_bytes": 10_000_000}

    first = engine.semantic_candidates("japanese rolls", scope_key(namespace="tenant-a"), **options)
    encoded = len(model.encoded_texts)
    again = engine.semantic_candidates("japanese rolls", scope_key(namespace="tenant-a"), **options)

    assert again == first
    # Only the query is encoded again; the artifact's records are reused.
    assert model.encoded_texts[encoded:] == ["japanese rolls"]
    engine.response_repository = ArtifactRepository((cats,))
    replaced = engine.semantic_candidates("feline guide", scope_key(namespace="tenant-a"), **options)
    assert [match.get("statement_id", "") for match in replaced.get("matches", [])] == ["cats"]
    assert set(engine.semantic_retriever.internal_records) == {"cats"}
