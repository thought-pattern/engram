"""Materialize a short-lived Engram graph config for read-only probes."""

from pathlib import Path

from yaml import safe_dump as yaml_safe_dump, safe_load as yaml_safe_load

# The Tapestry memgraph connection fields an Engram graph config may carry, with their
# concrete types; only fields present in the owner config are copied.
CONNECTION_FIELD_DEFAULTS = {
    "host": "",
    "password": "",
    "port": 0,
    "username": "",
    "visibility_scope": {},
}


def materialize_engram_graph_config(source: str, destination: Path) -> str:
    """Return an Engram config path, adapting a Tapestry owner config when needed."""
    source_path = Path(source)
    loaded = yaml_safe_load(source_path.read_text(encoding="utf-8"))
    if not isinstance(loaded, dict):
        raise ValueError("graph probe config must contain a YAML object")
    if "graph" in loaded:
        selected_path = str(source_path)
        return selected_path

    memgraph = loaded.get("memgraph", {})
    if not isinstance(memgraph, dict) or not memgraph:
        raise ValueError("graph probe config must contain graph or memgraph settings")
    connection = {
        name: memgraph.get(name, default) for name, default in sorted(CONNECTION_FIELD_DEFAULTS.items()) if name in memgraph
    }
    graph = {
        **connection,
        "enabled": True,
        "deployment_mode": "tapestry_managed",
        "vector_enabled": False,
    }
    destination.write_text(yaml_safe_dump({"graph": graph}, sort_keys=True), encoding="utf-8")
    selected_path = str(destination)
    return selected_path
