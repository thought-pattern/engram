"""Request-local semantic retrieval tests."""

from datetime import UTC, datetime
from pathlib import Path

from pytest import raises as pytest_raises

from engram.artifacts import LifecycleState, artifact_provenance, artifact_statistics, cached_response_artifact
from engram.config import engram_config, semantic_config
from engram.constants import CandidateSource, Tier
from engram.core import Engram
from engram.errors import ResolutionCancelledError
from engram.identity import build_retrieval_representation, build_standalone_identity, scope_key
from engram.repository import ArtifactRepository
from engram.resolution import QueryFrameBuilder
from engram.resolvers import StandaloneSemanticResolver, resolver_budget
from engram.semantic import StandaloneSemanticRetriever, model_artifact_sha256

NOW = datetime(2026, 8, 22, 12, 0, tzinfo=UTC)


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


def artifact(
    statement_id: str,
    request: str,
    response: str,
    *,
    aliases: tuple[str, ...] = (),
    lifecycle: LifecycleState = LifecycleState.ACTIVE,
    namespace: str = "tenant-a",
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
        provenance=artifact_provenance("semantic-test", "regulator", "2026-08-22T12:00:00Z"),
        statistics=artifact_statistics(),
        metadata={},
    )
    return result


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


def retriever(tmp_path: Path, model: object = False, **changes) -> StandaloneSemanticRetriever:
    selected = model or FakeSemanticModel()
    result = StandaloneSemanticRetriever(settings(tmp_path, **changes), model=selected)
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
    value = retriever(tmp_path, model)
    sushi = artifact("sushi", "best sushi", "response prose must not be embedded", aliases=("japanese rolls",))

    result = search(value, (sushi,))

    assert result["complete"] is True
    assert result["matches"][0]["statement_id"] == "sushi"
    assert result["matches"][0]["origin"] in {"canonical", "alias"}
    assert "response prose must not be embedded" not in model.encoded_texts
    assert not hasattr(value, "snapshot")
    assert not hasattr(value, "rebuild")


def test_semantic_search_observes_replacement_snapshot_without_synchronization(tmp_path: Path) -> None:
    value = retriever(tmp_path)
    first = artifact("sushi", "best sushi", "Sushi", aliases=("japanese rolls",))
    second = artifact("cats", "best cats", "Cats", aliases=("feline guide",))

    before = search(value, (first,), text="japanese rolls")
    after = search(value, (second,), text="feline guide")

    assert [match["statement_id"] for match in before["matches"]] == ["sushi"]
    assert [match["statement_id"] for match in after["matches"]] == ["cats"]


def test_semantic_search_is_scope_and_lifecycle_isolated(tmp_path: Path) -> None:
    value = retriever(tmp_path)
    active = artifact("active", "best sushi", "A", namespace="tenant-a")
    other = artifact("other", "best sushi", "B", namespace="tenant-b")
    retired = artifact("retired", "best sushi", "C", lifecycle=LifecycleState.RETIRED)

    result = search(value, (active, other, retired), text="sushi", namespace="tenant-a")

    assert [match["statement_id"] for match in result["matches"]] == ["active"]


def test_semantic_search_honors_budgets_and_cancellation(tmp_path: Path) -> None:
    value = retriever(tmp_path)
    accepted = artifact("sushi", "best sushi", "Sushi")

    exhausted = search(value, (accepted,), max_vector_results=0)
    assert exhausted["complete"] is False
    assert exhausted["reason"] == "vector_result_budget"

    def cancelled() -> None:
        raise ResolutionCancelledError("cancelled")

    with pytest_raises(ResolutionCancelledError):
        search(value, (accepted,), cooperative_check=cancelled)


def test_semantic_search_checks_scan_budget_before_encoding_corpus(tmp_path: Path) -> None:
    model = FakeSemanticModel()
    value = retriever(tmp_path, model, max_scan_records=1)
    first = artifact("first", "first sushi request", "A")
    second = artifact("second", "second sushi request", "B")

    result = search(value, (first, second), text="sushi")

    assert result.get("complete", True) is False
    assert result.get("reason", "") == "semantic_scan_budget"
    assert result.get("scanned_records", 0) == 2
    assert model.encoded_texts == []


def test_semantic_search_checks_memory_budget_before_encoding_corpus(tmp_path: Path) -> None:
    model = FakeSemanticModel()
    value = retriever(tmp_path, model)
    accepted = artifact("sushi", "best sushi", "Sushi")

    result = search(value, (accepted,), max_working_memory_bytes=1_000)

    assert result.get("complete", True) is False
    assert result.get("reason", "") == "working_memory_budget"
    assert result.get("working_memory_bytes", 0) <= 1_000
    assert model.encoded_texts == []


def test_semantic_resolver_reads_artifacts_without_a_live_index(tmp_path: Path) -> None:
    accepted = artifact("sushi", "best sushi", "Sushi", aliases=("japanese rolls",))
    configuration = engram_config(semantic=settings(tmp_path))
    engine = Engram(config=configuration)
    engine.semantic_retriever = StandaloneSemanticRetriever(
        configuration["semantic"],
        model=FakeSemanticModel(),
    )
    engine.response_repository = ArtifactRepository((accepted,))
    frame = QueryFrameBuilder(engine, lambda: 1_000_000_000, lambda: NOW).build(
        "japanese rolls",
        scope_key(namespace="tenant-a"),
    )

    budget = frame["budget"]
    lease = resolver_budget(
        budget["max_candidates"],
        budget["max_graph_rows"],
        budget["max_vector_results"],
        budget["max_evidence"],
        budget["max_evidence_bytes"],
        budget["max_output_bytes"],
        budget["max_diagnostic_bytes"],
        budget["max_working_memory_bytes"],
    )
    result = StandaloneSemanticResolver(engine, lambda: 1_000_000_100).resolve(frame, lease)

    assert result["candidates"][0]["source"] == CandidateSource.STANDALONE_SEMANTIC
    assert result["candidates"][0]["statement_id"] == "sushi"
    assert engine.get_statement("sushi") == {}
