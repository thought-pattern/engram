"""Tests for YAML configuration loading.

These exercise the loader's behavior (fallback, override, enum mapping,
unknown-key handling, validation) using arbitrary inputs. They deliberately do
not assert that any particular shipped configuration holds specific values:
configuration is the user's to tune, so the source of truth for defaults is
``engram_config()`` itself, not a hardcoded literal or the example template.
"""

from pathlib import Path

import pytest

from engram.config import engram_config, load_config
from engram.constants import EvictionPolicy, SessionOverflow

SCRATCH = Path(__file__).resolve().parent


def _write(tmp_path, text: str) -> str:
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
    cfg = load_config(_write(tmp_path, ""))
    assert cfg == engram_config()


"""Values in the file override the corresponding defaults; others are untouched."""


def test_load_config_values_scalar_overrides(tmp_path):
    cfg = load_config(_write(tmp_path, "capacity: 500\nweight_base: 0.9\nuse_spacy_facts: true\n"))
    assert cfg["capacity"] == 500
    assert cfg["weight_base"] == 0.9
    assert cfg["use_spacy_facts"] is True


def test_load_config_values_omitted_keys_keep_defaults(tmp_path):
    defaults = engram_config()
    cfg = load_config(_write(tmp_path, "capacity: 42\n"))
    assert cfg["capacity"] == 42
    assert cfg["max_sessions"] == defaults["max_sessions"]  # omitted -> default


def test_load_config_values_enum_fields_by_name(tmp_path):
    cfg = load_config(_write(tmp_path, "eviction_policy: lru\nsession_overflow: reject\n"))
    assert cfg["eviction_policy"] == EvictionPolicy.LRU
    assert cfg["session_overflow"] == SessionOverflow.REJECT


def test_load_config_values_graph_mapping(tmp_path):
    cfg = load_config(
        _write(
            tmp_path,
            "graph:\n  host: db\n  port: 7777\n  enabled: true\n"
            "  deployment_mode: tapestry_managed\n",
        )
    )
    assert cfg["graph"]["host"] == "db"
    assert cfg["graph"]["port"] == 7777
    assert cfg["graph"]["enabled"] is True
    assert cfg["graph"]["deployment_mode"] == "tapestry_managed"


def test_load_config_values_unknown_key_raises(tmp_path):
    # A config typo should fail loudly, with the key and file named.
    with pytest.raises(ValueError, match="capcity"):
        load_config(_write(tmp_path, "capcity: 500\n"))


def test_load_config_values_unknown_graph_key_raises(tmp_path):
    # A stale graph section (e.g. the pre-pymgclient uri/database form)
    # should name the offending keys, even when the graph is disabled.
    with pytest.raises(ValueError, match="uri"):
        load_config(_write(tmp_path, "graph:\n  uri: bolt://localhost:7687\n  enabled: false\n"))


def test_load_config_values_validation_applies(tmp_path):
    with pytest.raises(ValueError):
        load_config(_write(tmp_path, "capacity: 0\n"))


"""The shipped template is loadable.

This is a smoke test on the loader, not an assertion about the template's
chosen values: it only confirms the tracked example parses into a config
without raising, so a syntactically broken template cannot ship.
"""


def test_example_template_example_parses():
    repo_root = Path(__file__).resolve().parents[1]
    cfg = load_config(str(repo_root / "config.example.yml"))
    assert isinstance(cfg, dict)
