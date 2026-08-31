"""Tests for configuration."""

from pytest import mark as pytest_mark, raises as pytest_raises

from engram.config import config_from_dict, engram_config, graph_config
from engram.constants import SessionOverflow

"""Tests for configuration."""


def test_engram_config_custom_values() -> None:
    config = engram_config(
        capacity=5000,
        max_sessions=100,
        session_ttl_seconds=3600.0,
        weight_base=0.4,
        weight_recency=0.4,
        weight_hit_rate=0.2,
        session_overflow=SessionOverflow.REJECT,
    )

    assert config["capacity"] == 5000
    assert config["max_sessions"] == 100
    assert config["session_overflow"] == SessionOverflow.REJECT


def test_engram_config_invalid_capacity() -> None:
    with pytest_raises(ValueError):
        engram_config(capacity=0)

    with pytest_raises(ValueError):
        engram_config(capacity=-1)


def test_engram_config_invalid_max_sessions() -> None:
    with pytest_raises(ValueError):
        engram_config(max_sessions=0)


def test_engram_config_invalid_session_ttl() -> None:
    with pytest_raises(ValueError):
        engram_config(session_ttl_seconds=0)

    with pytest_raises(ValueError):
        engram_config(session_ttl_seconds=-1)


def test_engram_config_invalid_weights() -> None:
    with pytest_raises(ValueError):
        engram_config(weight_base=-1, weight_recency=-1, weight_hit_rate=-1)
    with pytest_raises(ValueError):
        engram_config(weight_base=-0.1)
    with pytest_raises(ValueError):
        engram_config(weight_recency=float("nan"))
    with pytest_raises(ValueError):
        engram_config(weight_hit_rate=float("inf"))


def test_engram_config_invalid_matching_settings() -> None:
    with pytest_raises(ValueError):
        engram_config(srai_depth_limit=0)
    with pytest_raises(ValueError):
        engram_config(max_synonyms_per_word=-1)


def test_removed_eviction_policy_keys_are_not_accepted() -> None:
    with pytest_raises(TypeError):
        engram_config(eviction_policy="fifo")
    with pytest_raises(TypeError):
        engram_config(min_hit_rate=0.5)


def test_engram_config_graph_config_validation() -> None:
    with pytest_raises(ValueError):
        graph_config(host="")
    with pytest_raises(ValueError):
        graph_config(port=0)
    with pytest_raises(ValueError):
        graph_config(port=70000)
    with pytest_raises(ValueError):
        graph_config(vector_limit=0)
    with pytest_raises(ValueError):
        graph_config(vector_support_scan_limit=0)
    with pytest_raises(ValueError):
        graph_config(vector_support_scan_limit=1_000_001)
    with pytest_raises(ValueError):
        graph_config(vector_min_similarity=1.1)
    with pytest_raises(ValueError):
        graph_config(vector_weight=-0.1)
    with pytest_raises(ValueError, match="requires graph enabled"):
        graph_config(vector_enabled=True)
    with pytest_raises(ValueError, match="explicit deployment_mode"):
        graph_config(enabled=True)
    with pytest_raises(ValueError, match="proposition_embeddings"):
        graph_config(vector_index_name="other_embeddings")
    with pytest_raises(ValueError, match="invalid shape"):
        graph_config(
            visibility_scope={
                "kind": "global",
                "company_id": {},
                "customer_id": {},
                "engagement_id": {},
                "user_id": "user:elias",
            }
        )


@pytest_mark.parametrize("invalid", [[], (), "", 0, False])
def test_engram_config_engram_config_rejects_falsey_non_object_graph_config(invalid) -> None:
    with pytest_raises(ValueError, match="graph config must be an object"):
        engram_config(graph=invalid)

    with pytest_raises(ValueError, match="serialized graph config must be an object"):
        config_from_dict({"graph": invalid})


def test_engram_config_graph_vector_config() -> None:
    config = graph_config(
        enabled=True,
        deployment_mode="tapestry_managed",
        vector_enabled=True,
        vector_index_name="proposition_embeddings",
        vector_model_path="/models/minilm",
        vector_limit=75,
        vector_support_scan_limit=50000,
        vector_min_similarity=0.52,
        vector_weight=0.8,
    )

    assert config["vector_enabled"] is True
    assert config["deployment_mode"] == "tapestry_managed"
    assert config["visibility_scope"] == {
        "kind": "global",
        "company_id": {},
        "customer_id": {},
        "engagement_id": {},
    }
    assert config["vector_limit"] == 75
    assert config["vector_support_scan_limit"] == 50000
    assert config["vector_min_similarity"] == 0.52
    assert config["vector_weight"] == 0.8


def test_engram_config_custom_stopwords() -> None:
    custom = {"custom", "stop", "words"}
    config = engram_config(stopwords=custom)

    assert config["stopwords"] == custom
