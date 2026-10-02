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


def test_engram_config_graph_vector_config() -> None:
    config = graph_config(
        enabled=True,
        vector_enabled=True,
        vector_index_name="proposition_embeddings",
        vector_model_path="/models/minilm",
        vector_limit=75,
        vector_support_scan_limit=50000,
        vector_min_similarity=0.52,
        vector_weight=0.8,
    )

    assert config["vector_enabled"] is True
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


def test_config_from_dict_builds_a_host_mapping_and_drops_retired_deployment_mode(caplog) -> None:
    config = config_from_dict(
        {
            "session_overflow": "reject",
            "graph": {
                "host": "graph.internal",
                "port": 7687,
                "enabled": True,
                "deployment_mode": "tapestry_managed",
                "vector_enabled": False,
            },
        },
        base_path="",
    )

    assert config["session_overflow"] == SessionOverflow.REJECT
    assert config["graph"]["host"] == "graph.internal"
    assert config["graph"]["enabled"] is True
    assert "deployment_mode" not in config["graph"]
    assert "deployment_mode" not in caplog.text


@pytest_mark.parametrize("invalid", [[], (), "", 0, False])
def test_config_from_dict_rejects_non_object_config_and_graph(invalid) -> None:
    with pytest_raises(ValueError, match="config must be an object"):
        config_from_dict(invalid, base_path="")
    with pytest_raises(ValueError, match="graph config"):
        config_from_dict({"graph": invalid}, base_path="")


def test_config_from_dict_starts_from_the_engram_file_and_applies_host_overrides(tmp_path) -> None:
    seed = tmp_path / "data" / "seed.json"
    seed.parent.mkdir()
    seed.write_text('{"pairs": [{"pattern": "*", "response": "Ready."}]}', encoding="utf-8")
    base = tmp_path / "config.yml"
    base.write_text(
        "capacity: 42\n"
        "conversation:\n"
        "  bot_name: Elias Thorne\n"
        "  seed_files:\n"
        "    - data/seed.json\n"
        "graph:\n"
        "  host: file-host\n"
        "  vector_limit: 125\n",
        encoding="utf-8",
    )

    config = config_from_dict(
        {"graph": {"host": "host-override", "enabled": True, "deployment_mode": "tapestry_managed"}},
        base_path=str(base),
    )

    assert config["capacity"] == 42
    assert config["conversation"]["bot_name"] == "Elias Thorne"
    assert config["conversation"]["seed_files"] == [str(seed)]
    assert config["graph"]["host"] == "host-override"
    assert config["graph"]["enabled"] is True
    assert config["graph"]["vector_limit"] == 125
    assert config_from_dict({}, base_path=str(tmp_path / "missing.yml")) == engram_config()
