"""Tests for configuration."""

from pathlib import Path

from pytest import mark as pytest_mark, raises as pytest_raises
from yaml import safe_dump as yaml_safe_dump

from engram import config as engram_config_module
from engram.config import DEFAULT_CONFIG_PATH, config_from_dict, engram_config, graph_config, load_config
from engram.constants import SessionOverflow


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

    assert config.get("capacity", 0) == 5000
    assert config.get("max_sessions", 0) == 100
    assert config.get("session_overflow", SessionOverflow.LRU) == SessionOverflow.REJECT


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

    assert config.get("vector_enabled", False) is True
    assert config.get("vector_limit", 0) == 75
    assert config.get("vector_support_scan_limit", 0) == 50000
    assert config.get("vector_min_similarity", 0.0) == 0.52
    assert config.get("vector_weight", 0.0) == 0.8


def test_engram_config_custom_stopwords() -> None:
    custom = {"custom", "stop", "words"}
    config = engram_config(stopwords=custom)

    assert config.get("stopwords", set()) == custom


def test_config_from_dict_builds_a_host_mapping() -> None:
    config = config_from_dict(
        {"session_overflow": "reject", "graph": {"host": "graph.internal", "port": 7687, "enabled": True, "vector_enabled": False}}
    )

    graph = config.get("graph", {})
    assert config.get("session_overflow", SessionOverflow.LRU) == SessionOverflow.REJECT
    assert graph.get("host", "") == "graph.internal"
    assert graph.get("enabled", False) is True


def test_config_from_dict_rejects_an_unknown_graph_key() -> None:
    with pytest_raises(ValueError, match="unknown graph config key"):
        config_from_dict({"graph": {"host": "graph.internal", "deployment_mode": "tapestry_managed"}})


@pytest_mark.parametrize("invalid", [[], (), "", 0, False])
def test_config_from_dict_rejects_non_object_config_and_graph(invalid) -> None:
    with pytest_raises(ValueError, match="config must be an object"):
        config_from_dict(invalid)
    with pytest_raises(ValueError, match="graph config"):
        config_from_dict({"graph": invalid})


def write_seed(directory: Path) -> Path:
    seed = directory / "data" / "seed.json"
    seed.parent.mkdir(parents=True)
    seed.write_text('{"pairs": [{"pattern": "*", "response": "Ready."}]}', encoding="utf-8")
    return seed


def refuse_configuration_file_reads(monkeypatch) -> bool:
    """Make any read of a YAML configuration file through engram.config fail the test."""

    def refuse(*arguments, **keywords):
        raise AssertionError("a configuration file was read")

    monkeypatch.setattr(engram_config_module, "read_config_mapping", refuse)
    monkeypatch.setattr(engram_config_module, "yaml_safe_load", refuse)
    return True


def test_config_from_dict_is_the_whole_configuration_and_reads_no_engram_file(tmp_path, monkeypatch) -> None:
    # Engram's own file sits in the working directory with other values; a host mapping never reads it.
    (tmp_path / "config.yml").write_text("capacity: 42\nconversation:\n  bot_name: From File\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    refuse_configuration_file_reads(monkeypatch)
    host_root = tmp_path / "host"
    seed = write_seed(host_root)

    config = config_from_dict(
        {"max_sessions": 7, "conversation": {"bot_name": "Host Persona", "seed_files": ["data/seed.json"]}},
        base_directory=str(host_root),
    )

    conversation = config.get("conversation", {})
    assert config.get("capacity", 0) == engram_config().get("capacity", 0)
    assert config.get("max_sessions", 0) == 7
    assert conversation.get("bot_name", "") == "Host Persona"
    assert conversation.get("seed_files", []) == [str(seed)]
    assert config_from_dict({}) == engram_config()


def test_config_from_dict_names_a_missing_conversation_file(tmp_path) -> None:
    with pytest_raises(ValueError, match="listed in config mapping is missing or is not a file"):
        config_from_dict({"conversation": {"seed_files": ["data/missing.json"]}}, base_directory=str(tmp_path))


def test_load_config_reads_engrams_own_file_for_a_standalone_process(tmp_path) -> None:
    seed = write_seed(tmp_path)
    own_file = tmp_path / "config.yml"
    own_file.write_text(
        "capacity: 42\nconversation:\n  bot_name: Standalone\n  seed_files:\n    - data/seed.json\n", encoding="utf-8"
    )

    config = load_config(str(own_file))

    conversation = config.get("conversation", {})
    assert config.get("capacity", 0) == 42
    assert conversation.get("bot_name", "") == "Standalone"
    assert conversation.get("seed_files", []) == [str(seed)]
    assert load_config.__defaults__ == (DEFAULT_CONFIG_PATH,)
    assert Path(DEFAULT_CONFIG_PATH) == Path(engram_config_module.__file__).resolve().parents[1] / "config.yml"


GRAPH_ON = {
    "host": "graph.internal",
    "port": 7687,
    "enabled": True,
    "vector_enabled": True,
    "vector_model_path": "/models/minilm",
}


@pytest_mark.parametrize("graph", [{}, {"enabled": False}, GRAPH_ON], ids=["graph-default", "graph-off", "graph-on"])
def test_both_sources_load_with_the_graph_off_and_on(tmp_path, graph) -> None:
    own_file = tmp_path / "config.yml"
    own_file.write_text(yaml_safe_dump({"graph": graph} if graph else {"capacity": 9}), encoding="utf-8")

    from_file = load_config(str(own_file))
    from_host = config_from_dict({"graph": graph})

    expected_enabled = graph.get("enabled", graph_config().get("enabled", True))
    assert from_file.get("graph", {}) == from_host.get("graph", {})
    assert from_host.get("graph", {}).get("enabled", not expected_enabled) is expected_enabled
    assert from_host.get("graph", {}).get("vector_enabled", False) is graph.get("vector_enabled", False)


def test_example_config_leaves_the_graph_at_its_code_default() -> None:
    example = Path(engram_config_module.__file__).resolve().parents[1] / "config.example.yml"

    config = load_config(str(example))

    assert config.get("graph", {}).get("enabled", True) is graph_config().get("enabled", True)
    assert config.get("graph", {}).get("enabled", True) is False
