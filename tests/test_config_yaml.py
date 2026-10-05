"""Tests for YAML configuration loading.

These exercise the loader's behavior (fallback, override, enum mapping,
unknown-key handling, validation) using arbitrary inputs. They deliberately do
not assert that any particular shipped configuration holds specific values:
configuration is the user's to tune, so the source of truth for defaults is
``engram_config()`` itself, not a hardcoded literal or the example template.
"""

from os import path as os_path
from pathlib import Path

from pytest import raises as pytest_raises

from engram.config import engram_config, load_config
from engram.constants import SessionOverflow

SCRATCH = Path(__file__).resolve().parent


def internal_write(tmp_path, text: str) -> str:
    path = Path(tmp_path) / "config.yml"
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    result = str(path)
    return result


"""A missing or empty file falls back to the built-in EngramConfig defaults."""


def test_load_config_defaults_missing_file_returns_defaults():
    cfg = load_config(str(SCRATCH / "does_not_exist_xyz.yml"))
    assert cfg == engram_config()


def test_load_config_defaults_empty_file_returns_defaults(tmp_path):
    cfg = load_config(internal_write(tmp_path, ""))
    assert cfg == engram_config()


"""Values in the file override the corresponding defaults; others are untouched."""


def test_load_config_values_scalar_overrides(tmp_path):
    cfg = load_config(internal_write(tmp_path, "capacity: 500\nweight_base: 0.9\nuse_spacy_facts: true\n"))
    assert cfg.get("capacity", 0) == 500
    assert cfg.get("weight_base", 0.0) == 0.9
    assert cfg.get("use_spacy_facts", False) is True


def test_load_config_values_omitted_keys_keep_defaults(tmp_path):
    defaults = engram_config()
    cfg = load_config(internal_write(tmp_path, "capacity: 42\n"))
    assert cfg.get("capacity", 0) == 42
    assert "max_sessions" in defaults
    assert "max_sessions" in cfg
    assert cfg.get("max_sessions", 0) == defaults.get("max_sessions", 0)  # omitted -> default


def test_load_config_values_enum_fields_by_name(tmp_path):
    cfg = load_config(internal_write(tmp_path, "session_overflow: reject\n"))
    assert cfg.get("session_overflow", SessionOverflow.LRU) == SessionOverflow.REJECT


def test_load_config_values_graph_mapping(tmp_path):
    cfg = load_config(
        internal_write(
            tmp_path,
            "graph:\n  host: db\n  port: 7777\n  enabled: true\n",
        )
    )
    graph = cfg.get("graph", {})
    assert graph.get("host", "") == "db"
    assert graph.get("port", 0) == 7777
    assert graph.get("enabled", False) is True


def test_load_config_ignores_and_logs_an_unknown_key(tmp_path, caplog):
    # An extra or stale key must not stop startup; the log still names it.
    cfg = load_config(internal_write(tmp_path, "capcity: 500\ncapacity: 7\n"))
    assert cfg == engram_config(capacity=7)
    assert "capcity" in caplog.text


def test_load_config_ignores_an_unknown_graph_key(tmp_path, caplog):
    # A stale graph section (e.g. the pre-pymgclient uri/database form).
    cfg = load_config(internal_write(tmp_path, "graph:\n  uri: bolt://localhost:7687\n  port: 7777\n"))
    graph = cfg.get("graph", {})
    assert graph.get("port", 0) == 7777
    assert "uri" not in graph
    assert "uri" in caplog.text


def test_load_config_values_validation_applies(tmp_path):
    with pytest_raises(ValueError):
        load_config(internal_write(tmp_path, "capacity: 0\n"))


def test_load_config_ignores_an_unknown_conversation_key(tmp_path):
    cfg = load_config(internal_write(tmp_path, "conversation:\n  persona: Mara\n  bot_name: Mara\n"))
    conversation = cfg.get("conversation", {})
    assert conversation.get("bot_name", "") == "Mara"
    assert "persona" not in conversation


def test_load_config_conversation_blank_bot_name_raises(tmp_path):
    with pytest_raises(ValueError, match="bot_name"):
        load_config(internal_write(tmp_path, 'conversation:\n  bot_name: "   "\n'))


def test_load_config_conversation_missing_seed_names_the_config_file(tmp_path):
    config_path = internal_write(tmp_path, "conversation:\n  seed_files:\n    - data/missing.json\n")
    with pytest_raises(ValueError, match="missing.json") as caught:
        load_config(config_path)
    assert config_path in str(caught.value)


def test_load_config_conversation_directory_is_not_a_seed_file(tmp_path):
    seed_dir = Path(tmp_path) / "seeds"
    seed_dir.mkdir()
    with pytest_raises(ValueError, match="missing or is not a file"):
        load_config(internal_write(tmp_path, "conversation:\n  seed_files:\n    - seeds\n"))


def test_load_config_absolute_seed_path_stays_absolute(tmp_path):
    seed = Path(tmp_path) / "abs.json"
    seed.write_text('{"pairs": []}', encoding="utf-8")
    quoted = seed.resolve().as_posix()
    cfg = load_config(internal_write(tmp_path, f'conversation:\n  seed_files:\n    - "{quoted}"\n'))
    seed_files = cfg.get("conversation", {}).get("seed_files", [])
    assert os_path.isabs(seed_files[0])
    assert os_path.normcase(seed_files[0]) == os_path.normcase(os_path.abspath(quoted))


"""The shipped template is loadable.

This is a smoke test on the loader, not an assertion about the template's
chosen values: it only confirms the tracked example parses into a config
without raising, so a syntactically broken template cannot ship.
"""


def test_example_template_example_parses():
    repo_root = Path(__file__).resolve().parents[1]
    cfg = load_config(str(repo_root / "config.example.yml"))
    assert isinstance(cfg, dict)
    assert "conversation" in cfg
    assert cfg.get("conversation", {}) == engram_config().get("conversation", {})
