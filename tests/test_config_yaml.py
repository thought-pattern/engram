"""Tests for YAML configuration loading."""

import os

from engram.config import (
    EvictionPolicy,
    SessionOverflow,
    load_config,
)
from engram.nltk_data import DEFAULT_STOPWORDS

SCRATCH = os.environ.get("CLAUDE_SCRATCH", os.path.dirname(__file__))


def _write(tmp_path, text: str) -> str:
    path = os.path.join(str(tmp_path), "config.yml")
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    return path


class TestLoadConfigDefaults:
    """Missing or empty files fall back to defaults."""

    def test_missing_file_returns_defaults(self):
        cfg = load_config(os.path.join(str(SCRATCH), "does_not_exist_xyz.yml"))
        assert cfg["capacity"] == 10000
        assert cfg["eviction_policy"] == EvictionPolicy.FIFO
        assert cfg["stopwords"] == DEFAULT_STOPWORDS

    def test_empty_file_returns_defaults(self, tmp_path):
        cfg = load_config(_write(tmp_path, ""))
        assert cfg["capacity"] == 10000
        assert cfg["use_stemming"] is True


class TestLoadConfigValues:
    """Values in the file override defaults."""

    def test_scalar_overrides(self, tmp_path):
        cfg = load_config(_write(tmp_path, "capacity: 500\nweight_base: 0.9\nuse_spacy_facts: true\n"))
        assert cfg["capacity"] == 500
        assert cfg["weight_base"] == 0.9
        assert cfg["use_spacy_facts"] is True

    def test_omitted_keys_keep_defaults(self, tmp_path):
        cfg = load_config(_write(tmp_path, "capacity: 42\n"))
        assert cfg["capacity"] == 42
        assert cfg["max_sessions"] == 10000  # untouched default

    def test_enum_fields_by_name(self, tmp_path):
        cfg = load_config(_write(tmp_path, "eviction_policy: lru\nsession_overflow: reject\n"))
        assert cfg["eviction_policy"] == EvictionPolicy.LRU
        assert cfg["session_overflow"] == SessionOverflow.REJECT

    def test_graph_mapping(self, tmp_path):
        cfg = load_config(_write(tmp_path, "graph:\n  driver: neo4j\n  enabled: true\n"))
        assert cfg["graph"]["driver"] == "neo4j"
        assert cfg["graph"]["enabled"] is True

    def test_validation_applies(self, tmp_path):
        import pytest

        with pytest.raises(ValueError):
            load_config(_write(tmp_path, "capacity: 0\n"))


class TestExampleTemplate:
    """The tracked template parses and matches the built-in defaults."""

    def test_example_loads(self):
        repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        example = os.path.join(repo_root, "config.example.yml")
        cfg = load_config(example)
        assert cfg["capacity"] == 10000
        assert cfg["eviction_policy"] == EvictionPolicy.FIFO
        assert cfg["session_overflow"] == SessionOverflow.LRU
        assert cfg["use_lemmatization"] is True
