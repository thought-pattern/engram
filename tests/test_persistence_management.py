"""Section 15 cross-feature persistence management tests."""

import copy
import json
from pathlib import Path

import pytest

from engram import persistence
from engram.config import engram_config, graph_config, reranker_config, semantic_config
from engram.constants import (
    FEEDBACK_POLICY_VERSION,
    FUSION_POLICY_VERSION,
    INDEX_STATE_SCHEMA_VERSION,
    PERSISTENCE_MANIFEST_FIELDS,
    PERSISTENCE_VERSION,
    RETRIEVAL_NORMALIZATION_VERSION,
    SEMANTIC_INDEX_VERSION,
    SPARSE_INDEX_VERSION,
)
from engram.core import Engram
from engram.errors import InvalidRequestError
from engram.service import EngramCore


def managed_config(semantic_version: str = "semantic-r1") -> dict:
    result = engram_config(
        graph=graph_config(vector_model="graph-embedding-r1"),
        semantic=semantic_config(model_version=semantic_version),
        reranker=reranker_config(model_version="reranker-r1"),
    )
    return result


def legacy_state(engram: Engram) -> dict:
    state = persistence.to_dict(engram)
    state["version"] = 1
    state.pop("manifest")
    state.pop("response_state")
    return state


def test_persistence_manifest_records_cross_feature_contract_and_model_versions() -> None:
    state = persistence.to_dict(Engram(config=managed_config()))
    manifest = state["manifest"]

    assert state["version"] == PERSISTENCE_VERSION
    assert set(manifest) == PERSISTENCE_MANIFEST_FIELDS
    assert manifest["persistence_version"] == PERSISTENCE_VERSION
    assert manifest["retrieval_normalization_version"] == RETRIEVAL_NORMALIZATION_VERSION
    assert manifest["index_state_schema_version"] == INDEX_STATE_SCHEMA_VERSION
    assert manifest["sparse_index_version"] == SPARSE_INDEX_VERSION
    assert manifest["semantic_index_version"] == SEMANTIC_INDEX_VERSION
    assert manifest["fusion_policy_version"] == FUSION_POLICY_VERSION
    assert manifest["feedback_policy_version"] == FEEDBACK_POLICY_VERSION
    assert manifest["semantic_model_version"] == "semantic-r1"
    assert manifest["reranker_model_version"] == "reranker-r1"
    assert manifest["graph_vector_model_id"] == "graph-embedding-r1"


def test_manifest_mismatch_blocks_startup_before_derived_state_is_served() -> None:
    state = persistence.to_dict(Engram(config=managed_config()))
    state["manifest"]["index_state_schema_version"] = INDEX_STATE_SCHEMA_VERSION + 1

    with pytest.raises(InvalidRequestError, match="index_state_schema_version"):
        persistence.load_engram_from_dict(state)


def test_existing_v2_without_manifest_loads_and_explicit_migration_adds_it() -> None:
    state = persistence.to_dict(Engram())
    state.pop("manifest")

    restored = persistence.load_engram_from_dict(state)
    status = EngramCore(restored).status()["persistence"]
    migrated = persistence.migrate_persistence_state(state)

    assert status["ready"] is True
    assert status["manifest_present"] is False
    assert status["migration_required"] is True
    assert status["derived_state_rebuilt"] is True
    assert set(migrated["manifest"]) == PERSISTENCE_MANIFEST_FIELDS
    assert persistence.migrate_persistence_state(migrated) == migrated


def test_runtime_override_is_reported_without_blocking_rebuildable_model_change() -> None:
    state = persistence.to_dict(Engram(config=managed_config("semantic-r1")))
    restored = persistence.load_engram_from_dict(state, config=managed_config("semantic-r2"))
    status = EngramCore(restored).status()["persistence"]

    assert status["ready"] is True
    assert status["manifest_present"] is True
    assert status["runtime_manifest_matches_source"] is False
    assert status["manifest"]["semantic_model_version"] == "semantic-r2"


def test_explicit_file_migration_preserves_source_refuses_overwrite_and_reports_quarantine(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    engram = Engram()
    engram.store("Legacy response without identity", keyword_source="legacy response")
    source_state = legacy_state(engram)
    source = Path("engram-v1.json")
    output = Path("engram-v2.json")
    source.write_text(json.dumps(source_state, indent=2), encoding="utf-8")
    source_before = source.read_bytes()

    report = persistence.migrate_persistence_file(source, output)
    migrated = json.loads(output.read_text(encoding="utf-8"))

    assert source.read_bytes() == source_before
    assert report["source_version"] == 1
    assert report["output_version"] == PERSISTENCE_VERSION
    assert report["source_path"] == "engram-v1.json"
    assert report["output_path"] == "engram-v2.json"
    assert report["artifact_count"] == 0
    assert report["quarantine_count"] == 1
    assert report["quarantine_reasons"] == {"missing_identity": 1}
    assert report["idempotent"] is True
    assert persistence.migrate_persistence_state(migrated) == migrated

    with pytest.raises(InvalidRequestError, match="already exists"):
        persistence.migrate_persistence_file(source, output)
    with pytest.raises(InvalidRequestError, match="must differ"):
        persistence.migrate_persistence_file(source, source)


def test_migration_functions_do_not_mutate_caller_input() -> None:
    source = legacy_state(Engram())
    original = copy.deepcopy(source)

    persistence.migrate_persistence_state(source)

    assert source == original
