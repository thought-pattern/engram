"""Tests for YAML configuration loading.

These exercise the loader's behavior (fallback, override, enum mapping,
unknown-key handling, validation) using arbitrary inputs. They deliberately do
not assert that any particular shipped configuration holds specific values:
configuration is the user's to tune, so the source of truth for defaults is
``engram_config()`` itself, not a hardcoded literal or the example template.
"""

import os

import pytest

from engram.config import engram_config, load_config
from engram.constants import EvictionPolicy, SessionOverflow

SCRATCH = os.environ.get("CLAUDE_SCRATCH", os.path.dirname(__file__))


def _write(tmp_path, text: str) -> str:
    path = os.path.join(str(tmp_path), "config.yml")
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    return path


class TestLoadConfigDefaults:
    """A missing or empty file falls back to the built-in EngramConfig defaults."""

    def test_missing_file_returns_defaults(self):
        cfg = load_config(os.path.join(str(SCRATCH), "does_not_exist_xyz.yml"))
        assert cfg == engram_config()

    def test_empty_file_returns_defaults(self, tmp_path):
        cfg = load_config(_write(tmp_path, ""))
        assert cfg == engram_config()


class TestLoadConfigValues:
    """Values in the file override the corresponding defaults; others are untouched."""

    def test_scalar_overrides(self, tmp_path):
        cfg = load_config(_write(tmp_path, "capacity: 500\nweight_base: 0.9\nuse_spacy_facts: true\n"))
        assert cfg["capacity"] == 500
        assert cfg["weight_base"] == 0.9
        assert cfg["use_spacy_facts"] is True

    def test_omitted_keys_keep_defaults(self, tmp_path):
        defaults = engram_config()
        cfg = load_config(_write(tmp_path, "capacity: 42\n"))
        assert cfg["capacity"] == 42
        assert cfg["max_sessions"] == defaults["max_sessions"]  # omitted -> default

    def test_enum_fields_by_name(self, tmp_path):
        cfg = load_config(_write(tmp_path, "eviction_policy: lru\nsession_overflow: reject\n"))
        assert cfg["eviction_policy"] == EvictionPolicy.LRU
        assert cfg["session_overflow"] == SessionOverflow.REJECT

    def test_graph_mapping(self, tmp_path):
        cfg = load_config(_write(tmp_path, "graph:\n  uri: bolt://db:7687\n  enabled: true\n"))
        assert cfg["graph"]["uri"] == "bolt://db:7687"
        assert cfg["graph"]["enabled"] is True

    def test_unknown_key_raises(self, tmp_path):
        # A config typo should fail loudly, not be silently dropped.
        with pytest.raises(TypeError):
            load_config(_write(tmp_path, "capcity: 500\n"))

    def test_validation_applies(self, tmp_path):
        with pytest.raises(ValueError):
            load_config(_write(tmp_path, "capacity: 0\n"))


class TestExampleTemplate:
    """The shipped template is loadable.

    This is a smoke test on the loader, not an assertion about the template's
    chosen values: it only confirms the tracked example parses into a config
    without raising, so a syntactically broken template cannot ship.
    """

    def test_example_parses(self):
        repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        example = os.path.join(repo_root, "config.example.yml")
        cfg = load_config(example)
        assert isinstance(cfg, dict)
