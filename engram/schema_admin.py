"""Standalone installation and dual-mode read-only schema verification."""

from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from engram.schema_catalog import (
    compare_catalogs,
    cypher_statements,
    read_live_catalog,
    schema_ddl_digest,
    validate_schema_contract,
)

REPRESENTATION_CONTRACT = "tapestry-ke-representation-v1"
PROOF_SCRATCH_CONTRACT = "tapestry-proof-scratch-v1"
ENGRAM_SUPPORT_CONTRACT = "tapestry-engram-support-v1"
STANDALONE_COMPONENT = "engram"
STANDALONE_GRAPH_STATE = "engram_graph"
TAPESTRY_COMPONENT = "tapestry"
TAPESTRY_GRAPH_STATE = "tapestry_knowledge_graph"
DEFAULT_PARAMETERS = {}


def catalog_has_definitions(catalog: dict) -> bool:
    """Return whether a live catalog contains any schema definition."""
    return any(bool(catalog.get(group, [])) for group in ("ordinary_indexes", "text_indexes", "vector_indexes", "constraints"))


def execute_admin_statement(connection, statement: str, parameters: dict = DEFAULT_PARAMETERS) -> list:
    """Execute one explicit standalone administrative statement."""
    raw_connection = getattr(connection, "conn", {})
    if not raw_connection:
        raise RuntimeError("Memgraph administrative connection is unavailable")
    cursor = raw_connection.cursor()
    cursor.execute(statement, dict(parameters))
    columns = [description.name for description in cursor.description] if cursor.description else []
    rows = cursor.fetchall() if cursor.description else []
    return [dict(zip(columns, row, strict=False)) for row in rows]


def read_graph_shape(connection) -> dict:
    """Read standalone apply preconditions without mistaking metadata for data."""
    node_rows = connection.execute_read(
        "MATCH (n) WHERE NOT n:SchemaRevision AND NOT n:GraphState RETURN count(n) AS application_nodes"
    )
    relationship_rows = connection.execute_read("MATCH ()-[r]->() RETURN count(r) AS relationships")
    metadata_rows = connection.execute_read("MATCH (n) WHERE n:SchemaRevision OR n:GraphState RETURN count(n) AS metadata_nodes")
    if not node_rows or not relationship_rows or not metadata_rows:
        raise RuntimeError("Memgraph graph-shape inspection returned no receipt")
    return {
        "application_nodes": int(node_rows[0].get("application_nodes", -1)),
        "relationships": int(relationship_rows[0].get("relationships", -1)),
        "metadata_nodes": int(metadata_rows[0].get("metadata_nodes", -1)),
    }


def read_metadata(connection) -> dict:
    """Read all deployment markers so mixed ownership cannot be hidden."""
    schema_rows = connection.execute_read(
        "MATCH (s:SchemaRevision) "
        "RETURN s.component AS component, s.deployment_owner AS deployment_owner, "
        "s.store_epoch AS schema_store_epoch, "
        "s.representation_contract AS representation_contract, "
        "s.proof_scratch_contract AS proof_scratch_contract, "
        "s.engram_support_contract AS engram_support_contract, "
        "s.schema_digest AS schema_digest"
    )
    graph_rows = connection.execute_read(
        "MATCH (g:GraphState) RETURN g.name AS graph_state_name, "
        "g.deployment_owner AS graph_deployment_owner, "
        "g.store_epoch AS graph_store_epoch, "
        "g.installation_state AS installation_state, "
        "g.graph_revision AS graph_revision"
    )
    if len(schema_rows) != 1 or len(graph_rows) != 1:
        return {"schema_revision_count": len(schema_rows), "graph_state_count": len(graph_rows)}
    result = dict(schema_rows[0])
    result.update(graph_rows[0])
    result["schema_revision_count"] = 1
    result["graph_state_count"] = 1
    return result


def expected_standalone_metadata(schema_path: Path) -> dict:
    """Return immutable standalone Engram metadata expectations."""
    return {
        "component": STANDALONE_COMPONENT,
        "deployment_owner": STANDALONE_COMPONENT,
        "graph_state_name": STANDALONE_GRAPH_STATE,
        "graph_deployment_owner": STANDALONE_COMPONENT,
        "representation_contract": REPRESENTATION_CONTRACT,
        "engram_support_contract": ENGRAM_SUPPORT_CONTRACT,
        "schema_digest": schema_ddl_digest(schema_path),
        "installation_state": "schema_installed",
    }


def expected_tapestry_metadata() -> dict:
    """Return the Tapestry-owned contract required by Engram recall."""
    return {
        "component": TAPESTRY_COMPONENT,
        "deployment_owner": TAPESTRY_COMPONENT,
        "graph_state_name": TAPESTRY_GRAPH_STATE,
        "graph_deployment_owner": TAPESTRY_COMPONENT,
        "representation_contract": REPRESENTATION_CONTRACT,
        "proof_scratch_contract": PROOF_SCRATCH_CONTRACT,
        "engram_support_contract": ENGRAM_SUPPORT_CONTRACT,
        "installation_state": "accepted",
    }


def compare_metadata(expected: dict, actual: dict) -> dict:
    """Compare ownership, versions, state, epoch, and singleton cardinality."""
    mismatched = {}
    for name, expected_value in expected.items():
        if actual.get(name, {}) != expected_value:
            mismatched[name] = {"expected": expected_value, "actual": actual.get(name, {})}
    if actual.get("schema_revision_count", 0) != 1:
        mismatched["schema_revision_count"] = {"expected": 1, "actual": actual.get("schema_revision_count", 0)}
    if actual.get("graph_state_count", 0) != 1:
        mismatched["graph_state_count"] = {"expected": 1, "actual": actual.get("graph_state_count", 0)}
    schema_epoch = actual.get("schema_store_epoch", "")
    graph_epoch = actual.get("graph_store_epoch", "")
    if not schema_epoch or schema_epoch != graph_epoch:
        mismatched["store_epoch"] = {
            "expected": "one non-empty matching epoch",
            "actual": {"schema": schema_epoch, "graph": graph_epoch},
        }
    revision = actual.get("graph_revision", {})
    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 0:
        mismatched["graph_revision"] = {"expected": "non-negative integer", "actual": revision}
    return {"compatible": not mismatched, "mismatched": mismatched}


def verify_schema(connection, schema_path: Path, deployment_mode: str) -> dict:
    """Verify standalone exactness or Tapestry-managed compatible superset."""
    if deployment_mode not in {"standalone", "tapestry_managed"}:
        raise ValueError("deployment mode must be standalone or tapestry_managed")
    expected_catalog = validate_schema_contract(schema_path.read_text(encoding="utf-8"))
    catalog_report = compare_catalogs(
        expected_catalog,
        read_live_catalog(connection),
        allow_superset=deployment_mode == "tapestry_managed",
    )
    metadata = read_metadata(connection)
    expected = expected_standalone_metadata(schema_path) if deployment_mode == "standalone" else expected_tapestry_metadata()
    metadata_report = compare_metadata(expected, metadata)
    return {
        "compatible": bool(catalog_report.get("compatible", False) and metadata_report.get("compatible", False)),
        "deployment_mode": deployment_mode,
        "catalog": catalog_report,
        "metadata": metadata_report,
        "store_epoch": metadata.get("schema_store_epoch", ""),
        "installation_state": metadata.get("installation_state", ""),
    }


def write_standalone_metadata(connection, schema_path: Path) -> dict:
    """Create standalone ownership metadata after exact catalog validation."""
    values = expected_standalone_metadata(schema_path)
    values["store_epoch"] = str(uuid4())
    values["installed_at"] = datetime.now(UTC).isoformat()
    execute_admin_statement(
        connection,
        "CREATE (s:SchemaRevision {component: $component, "
        "deployment_owner: $deployment_owner, store_epoch: $store_epoch, "
        "representation_contract: $representation_contract, "
        "engram_support_contract: $engram_support_contract, "
        "schema_digest: $schema_digest, installed_at: $installed_at}) "
        "CREATE (g:GraphState {name: $graph_state_name, "
        "deployment_owner: $graph_deployment_owner, store_epoch: $store_epoch, "
        "graph_revision: 0, installation_state: 'schema_installed'}) "
        "RETURN s.component AS component, g.name AS graph_state_name",
        values,
    )
    return values


def install_standalone_schema(connection, schema_path: Path) -> dict:
    """Install the Engram catalog from empty or verify an exact prior install."""
    text = schema_path.read_text(encoding="utf-8")
    expected_catalog = validate_schema_contract(text)
    shape = read_graph_shape(connection)
    if shape.get("application_nodes", -1) != 0 or shape.get("relationships", -1) != 0:
        raise RuntimeError("standalone schema installation requires an empty application graph")
    live = read_live_catalog(connection)
    if catalog_has_definitions(live):
        report = verify_schema(connection, schema_path, "standalone")
        if not report.get("compatible", False):
            raise RuntimeError("live catalog is partial, foreign, or release-incompatible")
        report["idempotent"] = True
        report["statements_applied"] = 0
        return report
    if shape.get("metadata_nodes", -1) != 0:
        raise RuntimeError("metadata exists without its exact standalone catalog")
    for statement in cypher_statements(text):
        execute_admin_statement(connection, statement)
    catalog_report = compare_catalogs(expected_catalog, read_live_catalog(connection))
    if not catalog_report.get("compatible", False):
        raise RuntimeError(f"installed catalog verification failed: {catalog_report}")
    write_standalone_metadata(connection, schema_path)
    report = verify_schema(connection, schema_path, "standalone")
    if not report.get("compatible", False):
        raise RuntimeError(f"standalone metadata verification failed: {report}")
    report["idempotent"] = False
    report["statements_applied"] = len(cypher_statements(text))
    return report
