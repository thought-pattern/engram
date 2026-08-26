"""Tests for YAML configuration loading.

These exercise the loader's behavior (fallback, override, enum mapping,
unknown-key handling, validation) using arbitrary inputs. They deliberately do
not assert that any particular shipped configuration holds specific values:
configuration is the user's to tune, so the source of truth for defaults is
``engram_config()`` itself, not a hardcoded literal or the example template.
"""

from pathlib import Path

from pytest import raises as pytest_raises

from engram.config import engram_config, load_config
from engram.constants import EvictionPolicy, SessionOverflow

SCRATCH = Path(__file__).resolve().parent


def _write(tmp_path, text: str) -> str:
    path = Path(tmp_path) / "config.yml"
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    _return_value = str(path)
    return _return_value


class TestLoadConfigDefaults:
    """A missing or empty file falls back to the built-in EngramConfig defaults."""

    def test_missing_file_returns_defaults(self):
        cfg = load_config(str(SCRATCH / "does_not_exist_xyz.yml"))
        assert cfg == engram_config()
        return False

    def test_empty_file_returns_defaults(self, tmp_path):
        cfg = load_config(_write(tmp_path, ""))
        assert cfg == engram_config()
        return False


class TestLoadConfigValues:
    """Values in the file override the corresponding defaults; others are untouched."""

    def test_scalar_overrides(self, tmp_path):
        cfg = load_config(_write(tmp_path, "capacity: 500\nweight_base: 0.9\nuse_spacy_facts: true\n"))
        assert cfg.get("capacity", 0) == 500
        assert cfg.get("weight_base", 0.0) == 0.9
        assert cfg.get("use_spacy_facts", False) is True
        return False

    def test_omitted_keys_keep_defaults(self, tmp_path):
        defaults = engram_config()
        cfg = load_config(_write(tmp_path, "capacity: 42\n"))
        assert cfg.get("capacity", 0) == 42
        assert cfg.get("max_sessions", 0) == defaults.get("max_sessions", 0)  # omitted -> default
        return False

    def test_enum_fields_by_name(self, tmp_path):
        cfg = load_config(_write(tmp_path, "eviction_policy: lru\nsession_overflow: reject\n"))
        assert cfg.get("eviction_policy", False) == EvictionPolicy.LRU
        assert cfg.get("session_overflow", False) == SessionOverflow.REJECT
        return False

    def test_graph_mapping(self, tmp_path):
        cfg = load_config(_write(tmp_path, "graph:\n  host: db\n  port: 7777\n  enabled: true\n"))
        assert cfg.get("graph", {}).get("host", "") == "db"
        assert cfg.get("graph", {}).get("port", 0) == 7777
        assert cfg.get("graph", {}).get("enabled", False) is True
        return False

    def test_unknown_key_raises(self, tmp_path):
        # A config typo should fail loudly, with the key and file named.
        with pytest_raises(ValueError, match="capcity"):
            load_config(_write(tmp_path, "capcity: 500\n"))
        return False

    def test_unknown_graph_key_raises(self, tmp_path):
        # A stale graph section (e.g. the pre-pymgclient uri/database form)
        # should name the offending keys, even when the graph is disabled.
        with pytest_raises(ValueError, match="uri"):
            load_config(_write(tmp_path, "graph:\n  uri: bolt://localhost:7687\n  enabled: false\n"))
        return False

    def test_validation_applies(self, tmp_path):
        with pytest_raises(ValueError):
            load_config(_write(tmp_path, "capacity: 0\n"))
        return False


class TestExampleTemplate:
    """The shipped template is loadable.

    This is a smoke test on the loader, not an assertion about the template's
    chosen values: it only confirms the tracked example parses into a config
    without raising, so a syntactically broken template cannot ship.
    """

    def test_example_parses(self):
        repo_root = Path(__file__).resolve().parents[1]
        cfg = load_config(str(repo_root / "config.example.yml"))
        assert isinstance(cfg, dict)
        return False
