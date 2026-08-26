"""Section 13 standalone semantic retrieval contracts and integration."""

import json
import math
from argparse import Namespace
from datetime import UTC, datetime
from pathlib import Path
from threading import Event, Thread

import pytest

from engram import semantic as semantic_module
from engram.artifacts import (
    CachedResponseArtifact,
    LifecycleState,
    artifact_provenance,
    artifact_statistics,
    cached_response_artifact,
)
from engram.config import engram_config, semantic_config
from engram.constants import CandidateSource, Tier
from engram.core import Engram
from engram.errors import ResolutionCancelledError
from engram.identity import build_retrieval_representation, build_standalone_identity, scope_key
from engram.repository import ArtifactRepository
from engram.resolution import QueryFrameBuilder, ResolverState, capture_resolution_budget
from engram.resolvers import StandaloneSemanticResolver, resolver_budget
from engram.semantic import StandaloneSemanticIndexOwner, model_artifact_sha256
from scripts import provision_semantic_model

NOW = datetime(2026, 8, 22, 12, 0, tzinfo=UTC)
START_NS = 1_000_000_000


class FakeSemanticModel:
    """Small deterministic encoder used only to test index behavior."""

    def __init__(self, dimension: int = 4) -> None:
        self.dimension = dimension
        self.encoded_texts: list[str] = []

    def get_sentence_embedding_dimension(self) -> int:
        return self.dimension

    def encode(self, texts, **kwargs):
        self.encoded_texts.extend(texts)
        result = []
        for text in texts:
            normalized = text.casefold()
            values = [
                float(any(token in normalized for token in ("sushi", "japanese", "roll"))),
                float(any(token in normalized for token in ("cat", "feline", "kitten"))),
                float(any(token in normalized for token in ("dog", "canine", "puppy"))),
                0.25,
            ][: self.dimension]
            if not any(values):
                values[-1] = 1.0
            result.append(values)
        return result


def artifact(
    statement_id: str,
    request: str,
    response: str,
    *,
    aliases: tuple[str, ...] = (),
    lifecycle: LifecycleState = LifecycleState.ACTIVE,
    generation: int = 1,
    namespace: str = "tenant-a",
) -> CachedResponseArtifact:
    scope = scope_key(namespace=namespace)
    return cached_response_artifact(
        statement_id=statement_id,
        generation=generation,
        response=response,
        query_identity=build_standalone_identity(request, scope),
        retrieval=build_retrieval_representation(request, aliases),
        tier=Tier.STATIC,
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
        provenance=artifact_provenance("semantic-test", "regulator", "2026-08-22T12:00:00Z"),
        statistics=artifact_statistics(),
        metadata={},
    )


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
    return semantic_config(**values)


def test_semantic_similarity_threshold_uses_the_fusion_unit_interval() -> None:
    with pytest.raises(ValueError, match="between 0 and 1"):
        semantic_config(min_similarity=-0.01)


def owner(tmp_path: Path, model: FakeSemanticModel | None = None, **changes) -> StandaloneSemanticIndexOwner:
    selected_model = model or FakeSemanticModel()
    return StandaloneSemanticIndexOwner(settings(tmp_path, **changes), model_loader=lambda _: selected_model)


def test_provisioner_reuses_verified_artifact_without_downloading(tmp_path: Path, monkeypatch, capsys) -> None:
    destination = tmp_path / "approved-model"
    destination.mkdir()
    (destination / "weights.bin").write_bytes(b"provisioned-once")
    (destination / "LICENSE").write_text("Apache License 2.0", encoding="utf-8")
    checksum = model_artifact_sha256(destination)
    monkeypatch.setattr(provision_semantic_model, "APPROVED_SEMANTIC_ARTIFACT_SHA256", checksum)
    manifest_path = destination.parent / f"{destination.name}.engram-model.json"
    manifest_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "model_id": provision_semantic_model.DEFAULT_MODEL_ID,
                "model_version": provision_semantic_model.DEFAULT_REVISION,
                "license_id": provision_semantic_model.DEFAULT_LICENSE,
                "dimension": provision_semantic_model.DEFAULT_DIMENSION,
                "backend": "native",
                "runtime_downloads_allowed": False,
                "artifact_sha256": checksum,
                "model_path": str(destination),
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        provision_semantic_model,
        "parse_args",
        lambda: Namespace(destination=str(destination), revision=provision_semantic_model.DEFAULT_REVISION),
    )

    def unexpected_download(**_kwargs) -> None:
        raise AssertionError("verified artifacts must not be downloaded again")

    monkeypatch.setattr(provision_semantic_model, "snapshot_download", unexpected_download)

    assert provision_semantic_model.main() == 0
    assert json.loads(capsys.readouterr().out)["reused"] is True


def test_provisioner_rejects_an_unapproved_revision(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        provision_semantic_model,
        "parse_args",
        lambda: Namespace(destination=str(tmp_path / "model"), revision="main"),
    )

    with pytest.raises(SystemExit, match="revision is not approved"):
        provision_semantic_model.main()


def test_provisioner_cleans_partial_download_without_publishing_destination(tmp_path: Path, monkeypatch) -> None:
    destination = tmp_path / "model"
    monkeypatch.setattr(
        provision_semantic_model,
        "parse_args",
        lambda: Namespace(destination=str(destination), revision=provision_semantic_model.DEFAULT_REVISION),
    )

    def partial_download(**kwargs) -> None:
        local_dir = Path(kwargs["local_dir"])
        local_dir.mkdir(parents=True)
        (local_dir / "partial.bin").write_bytes(b"partial")
        raise RuntimeError("download interrupted")

    monkeypatch.setattr(provision_semantic_model, "snapshot_download", partial_download)

    with pytest.raises(RuntimeError, match="download interrupted"):
        provision_semantic_model.main()

    assert not destination.exists()
    assert not (tmp_path / "model.engram-model.json").exists()


def frame(engine: Engram, request: str, namespace: str = "tenant-a") -> dict:
    return QueryFrameBuilder(engine, lambda: START_NS, lambda: NOW).build(
        request,
        scope_key(namespace=namespace),
        diagnostic_seed=f"semantic:{request}:{namespace}",
        budget=capture_resolution_budget(lambda: START_NS),
    )


def lease(query_frame: dict) -> dict:
    budget = query_frame["budget"]
    return resolver_budget(
        max_candidates=budget["max_candidates"],
        max_graph_rows=budget["max_graph_rows"],
        max_vector_results=budget["max_vector_results"],
        max_evidence=budget["max_evidence"],
        max_evidence_bytes=budget["max_evidence_bytes"],
        max_output_bytes=budget["max_output_bytes"],
        max_diagnostic_bytes=budget["max_diagnostic_bytes"],
        max_working_memory_bytes=budget["max_working_memory_bytes"],
    )


def test_embedding_records_use_only_canonical_requests_and_aliases(tmp_path: Path) -> None:
    model = FakeSemanticModel()
    index = owner(tmp_path, model)
    accepted = artifact(
        "sushi",
        "Where is the best sushi?",
        "SECRET RESPONSE PROSE MUST NOT BE EMBEDDED",
        aliases=("recommend japanese rolls",),
    )

    state = index.rebuild((accepted,), 2)

    assert [record["origin"] for record in state["records"]] == ["canonical", "alias"]
    assert [record["text"] for record in state["records"]] == [
        "Where is the best sushi?",
        "recommend japanese rolls",
    ]
    assert "SECRET RESPONSE PROSE MUST NOT BE EMBEDDED" not in model.encoded_texts
    assert all(record["model_version"] == "test-v1" for record in state["records"])
    assert all(record["normalization_version"] == 1 for record in state["records"])
    assert all(math.isclose(sum(value * value for value in record["embedding"]), 1.0) for record in state["records"])


def test_local_artifact_policy_is_offline_checksum_and_dimension_gated(tmp_path: Path) -> None:
    valid = settings(tmp_path)
    corrupt = dict(valid)
    corrupt["artifact_sha256"] = "0" * 64

    checksum_failure = StandaloneSemanticIndexOwner(corrupt, model_loader=lambda _: FakeSemanticModel())
    dimension_failure = StandaloneSemanticIndexOwner(valid, model_loader=lambda _: FakeSemanticModel(dimension=3))

    assert checksum_failure.available is False
    assert checksum_failure.last_error == "InvalidRequestError"
    assert dimension_failure.available is False
    assert dimension_failure.last_error == "InvalidRequestError"


def test_local_artifact_policy_requires_a_directory_and_license(tmp_path: Path) -> None:
    model_file = tmp_path / "model.bin"
    model_file.write_bytes(b"not-a-model-directory")
    file_settings = semantic_config(
        enabled=True,
        model_path=str(model_file),
        model_version="test-v1",
        artifact_sha256=model_artifact_sha256(model_file),
        dimension=4,
    )
    missing_license_path = tmp_path / "missing-license"
    missing_license_path.mkdir()
    (missing_license_path / "weights.bin").write_bytes(b"local-test-model")
    missing_license_settings = semantic_config(
        enabled=True,
        model_path=str(missing_license_path),
        model_version="test-v1",
        artifact_sha256=model_artifact_sha256(missing_license_path),
        dimension=4,
    )

    file_owner = StandaloneSemanticIndexOwner(file_settings, model_loader=lambda _: FakeSemanticModel())
    missing_license_owner = StandaloneSemanticIndexOwner(
        missing_license_settings,
        model_loader=lambda _: FakeSemanticModel(),
    )

    assert file_owner.available is False
    assert file_owner.last_error == "InvalidRequestError"
    assert missing_license_owner.available is False
    assert missing_license_owner.last_error == "InvalidRequestError"


def test_native_loader_forces_cpu_offline_mode_and_disables_remote_code(tmp_path: Path, monkeypatch) -> None:
    captured = {}

    def load(path, **kwargs):
        captured.update({"path": path, **kwargs})
        return FakeSemanticModel()

    monkeypatch.setattr(semantic_module, "SentenceTransformer", load)
    semantic_settings = settings(tmp_path)

    model = semantic_module._load_native_model(semantic_settings)

    assert isinstance(model, FakeSemanticModel)
    assert captured == {
        "path": semantic_settings["model_path"],
        "device": "cpu",
        "local_files_only": True,
        "trust_remote_code": False,
    }


def test_enabled_missing_model_fails_soft_without_blocking_engram_startup(tmp_path: Path) -> None:
    missing = semantic_config(
        enabled=True,
        model_path=str(tmp_path / "missing"),
        model_version="missing-v1",
        artifact_sha256="0" * 64,
        dimension=4,
    )

    engine = Engram(config=engram_config(semantic=missing))

    status = engine.component_status_snapshot()["semantic"]
    assert status["enabled"] is True
    assert status["ready"] is False
    assert status["error"] == "InvalidRequestError"


def test_semantic_search_is_scope_isolated_and_uses_alias_provenance(tmp_path: Path) -> None:
    index = owner(tmp_path)
    first = artifact("sushi", "restaurant recommendation", "Sushi answer", aliases=("japanese rolls",))
    second = artifact("other-tenant", "japanese rolls", "Other answer", namespace="tenant-b")
    index.rebuild((first, second), 2)

    result = index.search(
        "where can I get sushi rolls",
        scope_key(namespace="tenant-a"),
        limit=10,
        max_vector_results=10,
        max_working_memory_bytes=1_000_000,
    )

    assert result["complete"] is True
    assert [match["statement_id"] for match in result["matches"]] == ["sushi"]
    assert result["matches"][0]["origin"] == "alias"
    assert result["matches"][0]["ordinal"] == 0
    assert "representation" not in result["matches"][0]


def test_incremental_update_reuses_unchanged_embeddings_and_tracks_retirement(tmp_path: Path) -> None:
    model = FakeSemanticModel()
    index = owner(tmp_path, model)
    sushi = artifact("sushi", "best sushi", "Sushi")
    cats = artifact("cats", "why cats purr", "Cats")
    index.rebuild((sushi, cats), 2)
    original_sushi = index.snapshot()["by_statement"]["sushi"]
    encoded_before = len(model.encoded_texts)
    retired_cats = artifact("cats", "why cats purr", "Cats", lifecycle=LifecycleState.RETIRED, generation=2)

    state = index.synchronize({"sushi": sushi, "cats": retired_cats}, 3, ("cats",))
    assert len(model.encoded_texts) == encoded_before
    result = index.search(
        "feline kitten",
        scope_key(namespace="tenant-a"),
        limit=10,
        max_vector_results=10,
        max_working_memory_bytes=1_000_000,
    )

    assert state["by_statement"]["sushi"] is original_sushi
    assert "cats" not in state["by_statement"]
    assert result["matches"] == ()
    assert index.check_against((sushi, retired_cats), 3)["consistent"] is True


def test_incremental_conflict_rebuild_releases_the_owner_lock(tmp_path: Path, monkeypatch) -> None:
    index = owner(tmp_path)
    sushi = artifact("sushi", "best sushi", "Sushi")
    changed = artifact("sushi", "updated sushi", "Sushi", generation=2)
    index.rebuild((sushi,), 2)
    original_records = index._records
    original_rebuild = index.rebuild

    def conflicting_records(specs):
        records = original_records(specs)
        with index._lock:
            live = index._state
            index._state = semantic_module._state(
                live["by_statement"],
                repository_state_generation=live["repository_state_generation"],
                state_generation=live["state_generation"] + 1,
                settings=index._settings,
                identity=index._identity,
            )
        return records

    def observed_rebuild(artifacts, repository_state_generation):
        completed = Event()

        def read_snapshot() -> None:
            index.snapshot()
            completed.set()

        reader = Thread(target=read_snapshot, daemon=True)
        reader.start()
        assert completed.wait(1.0), "semantic rebuild started while the owner lock was held"
        return original_rebuild(artifacts, repository_state_generation)

    monkeypatch.setattr(index, "_records", conflicting_records)
    monkeypatch.setattr(index, "rebuild", observed_rebuild)

    state = index.synchronize({"sushi": changed}, 3, ("sushi",))

    assert state["repository_state_generation"] == 3
    assert state["by_statement"]["sushi"][0]["generation"] == 2


def test_normal_runtime_rejects_a_self_attested_unapproved_model_identity(tmp_path: Path) -> None:
    index = StandaloneSemanticIndexOwner(settings(tmp_path))

    assert index.available is False
    assert index.last_error == "InvalidRequestError"


def test_rebuild_recovers_after_incremental_projection_drift(tmp_path: Path) -> None:
    index = owner(tmp_path)
    sushi = artifact("sushi", "best sushi", "Sushi")
    index.rebuild((sushi,), 2)

    assert index.check_against((), 3)["consistent"] is False
    repaired = index.rebuild((), 3)

    assert repaired["record_count"] == 0
    assert index.check_against((), 3)["consistent"] is True


def test_incremental_sync_recovers_health_and_ignores_stale_publications(tmp_path: Path) -> None:
    index = owner(tmp_path)
    sushi = artifact("sushi", "best sushi", "Sushi")
    index.rebuild((sushi,), 4)
    current = index.snapshot()

    stale = index.synchronize({}, 3, ("sushi",))
    assert stale is current
    assert stale["repository_state_generation"] == 4

    index.mark_unavailable(RuntimeError("transient projection failure"))
    assert index.available is False
    recovered = index.synchronize({"sushi": sushi}, 5, ("sushi",))

    assert index.available is True
    assert recovered["repository_state_generation"] == 5
    assert recovered["record_count"] == 1


def test_incremental_sync_removes_deleted_projection(tmp_path: Path) -> None:
    index = owner(tmp_path)
    sushi = artifact("sushi", "best sushi", "Sushi")
    index.rebuild((sushi,), 2)

    state = index.synchronize({}, 3, ("sushi",))

    assert state["record_count"] == 0
    assert "sushi" not in state["by_statement"]


@pytest.mark.parametrize(
    "lifecycle",
    (LifecycleState.SUPERSEDED, LifecycleState.INVALIDATED, LifecycleState.RETIRED),
)
def test_terminal_lifecycle_records_are_not_searchable(tmp_path: Path, lifecycle: LifecycleState) -> None:
    index = owner(tmp_path)
    terminal = artifact("sushi", "best sushi", "Sushi", lifecycle=lifecycle)
    index.rebuild((terminal,), 2)

    result = index.search(
        "sushi restaurant",
        scope_key(namespace="tenant-a"),
        limit=10,
        max_vector_results=10,
        max_working_memory_bytes=1_000_000,
    )

    assert result["matches"] == ()


def test_semantic_search_abstains_on_scan_memory_and_vector_budgets(tmp_path: Path) -> None:
    accepted = artifact("sushi", "best sushi", "Sushi")
    scan_limited = owner(tmp_path, max_scan_records=1)
    scan_limited.rebuild((artifact("one", "sushi one", "1", aliases=("japanese one",)),), 2)
    ordinary = owner(tmp_path / "ordinary")
    ordinary.rebuild((accepted,), 2)
    arguments = {
        "text": "sushi",
        "scope": scope_key(namespace="tenant-a"),
        "limit": 10,
        "max_vector_results": 10,
        "max_working_memory_bytes": 1_000_000,
    }

    scan = scan_limited.search(**arguments)
    memory = ordinary.search(**{**arguments, "max_working_memory_bytes": 1})
    retained_memory = ordinary.search(**{**arguments, "max_working_memory_bytes": 100})
    vectors = ordinary.search(**{**arguments, "max_vector_results": 0})

    assert scan["reason"] == "semantic_scan_budget"
    assert memory["reason"] == "working_memory_budget"
    assert retained_memory["reason"] == "working_memory_budget"
    assert retained_memory["matches"] == ()
    assert vectors["reason"] == "vector_result_budget"


def test_semantic_search_propagates_cooperative_cancellation(tmp_path: Path) -> None:
    index = owner(tmp_path)
    index.rebuild((artifact("sushi", "best sushi", "Sushi"),), 2)

    def cancel() -> None:
        raise ResolutionCancelledError("cancelled")

    with pytest.raises(ResolutionCancelledError):
        index.search(
            "sushi",
            scope_key(namespace="tenant-a"),
            limit=10,
            max_vector_results=10,
            max_working_memory_bytes=1_000_000,
            cooperative_check=cancel,
        )


def test_resolver_emits_standalone_source_score_and_model_provenance(tmp_path: Path) -> None:
    semantic_settings = settings(tmp_path)
    engine = Engram(config=engram_config())
    engine.config["semantic"] = semantic_settings
    engine._semantic_index_owner = StandaloneSemanticIndexOwner(
        semantic_settings,
        model_loader=lambda _: FakeSemanticModel(),
    )
    accepted = artifact("sushi", "restaurant recommendation", "Sushi answer", aliases=("japanese rolls",))
    engine.response_repository = ArtifactRepository((accepted,))
    assert engine.synchronize_semantic_index(engine.response_repository.snapshot()) is True
    query_frame = frame(engine, "suggest sushi rolls")

    result = StandaloneSemanticResolver(engine, lambda: START_NS).resolve(query_frame, lease(query_frame))

    assert result["state"] == ResolverState.COMPLETED
    assert result["consumption"]["vector_results"] == 1
    candidate = result["candidates"][0]
    assert candidate["source"] == CandidateSource.STANDALONE_SEMANTIC
    assert candidate["features"]["values"]["semantic_score"] > 0.9
    assert candidate["provenance"]["matched_representation_origin"] == "alias"
    assert candidate["provenance"]["semantic_model_version"] == "test-v1"
    assert "matched_representation" not in candidate["diagnostics"]


def test_resolver_is_planned_after_cheaper_retrieval(tmp_path: Path) -> None:
    semantic_settings = settings(tmp_path)
    engine = Engram(config=engram_config())
    engine.config["semantic"] = semantic_settings
    engine._semantic_index_owner = StandaloneSemanticIndexOwner(
        semantic_settings,
        model_loader=lambda _: FakeSemanticModel(),
    )
    from engram.service import EngramCore

    core = EngramCore(engine)
    plan = core._resolver_registry.plan(frame(engine, "sushi"))
    names = [entry["resolver"].name for entry in plan["entries"]]

    assert names.index("standalone_semantic") > names.index("sparse")
    assert names[:4] == ["exact", "utility", "pattern", "lexical"]
