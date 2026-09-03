"""Tests for graph-probe configuration adaptation."""

from pathlib import Path

from yaml import safe_load as yaml_safe_load

from scripts.graph_probe_config import materialize_engram_graph_config


def test_graph_probe_config_preserves_an_engram_config_path(tmp_path: Path) -> None:
    source = tmp_path / "engram.yml"
    source.write_text("graph:\n  enabled: false\n", encoding="utf-8")

    result = materialize_engram_graph_config(str(source), tmp_path / "unused.yml")

    assert result == str(source)


def test_graph_probe_config_adapts_tapestry_memgraph_without_unrelated_values(tmp_path: Path) -> None:
    source = tmp_path / "tapestry.yml"
    source.write_text(
        "memgraph:\n"
        "  host: graph.internal\n"
        "  port: 7687\n"
        "  username: reader\n"
        "  password: credential\n"
        "model:\n"
        "  actor: unrelated\n",
        encoding="utf-8",
    )
    destination = tmp_path / "derived.yml"

    result = materialize_engram_graph_config(str(source), destination)

    assert result == str(destination)
    assert yaml_safe_load(destination.read_text(encoding="utf-8")) == {
        "graph": {
            "deployment_mode": "tapestry_managed",
            "enabled": True,
            "host": "graph.internal",
            "password": "credential",
            "port": 7687,
            "username": "reader",
            "vector_enabled": False,
        }
    }
