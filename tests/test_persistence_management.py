"""Section 15 cross-feature persistence management tests."""

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


def version_one_state(engram: Engram) -> dict:
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


def test_current_persistence_without_manifest_is_rejected() -> None:
    state = persistence.to_dict(Engram())
    state.pop("manifest")

    with pytest.raises(InvalidRequestError, match="requires manifest"):
        persistence.load_engram_from_dict(state)


def test_runtime_override_is_reported_without_blocking_rebuildable_model_change() -> None:
    state = persistence.to_dict(Engram(config=managed_config("semantic-r1")))
    restored = persistence.load_engram_from_dict(state, config=managed_config("semantic-r2"))
    status = EngramCore(restored).status()["persistence"]

    assert status["ready"] is True
    assert status["manifest_present"] is True
    assert status["runtime_manifest_matches_source"] is False
    assert status["manifest"]["semantic_model_version"] == "semantic-r2"


def test_version_one_persistence_is_rejected_without_migration() -> None:
    engram = Engram()
    engram.store("Legacy response without identity", keyword_source="legacy response")
    source = version_one_state(engram)

    with pytest.raises(ValueError, match="Unsupported persistence version: 1"):
        persistence.load_engram_from_dict(source)
