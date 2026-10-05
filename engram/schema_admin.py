"""Engram schema installation and graph compatibility checks."""

from engram.schema_catalog import (
    compare_catalogs,
    cypher_statements,
    read_live_catalog,
    validate_schema_contract,
)

DEFAULT_PARAMETERS = {}


def catalog_has_definitions(catalog: dict) -> bool:
    """Return whether a live catalog contains any schema definition."""
    result = any(bool(catalog.get(group, [])) for group in ("ordinary_indexes", "text_indexes", "vector_indexes", "constraints"))
    return result


def execute_admin_statement(connection, statement: str, parameters: dict = DEFAULT_PARAMETERS) -> list:
    """Execute one explicit administrative statement."""
    try:
        execute_admin = connection.execute_admin
    except AttributeError as error:
        raise RuntimeError("graph database administrative connection is unavailable") from error
    result = execute_admin(statement, dict(parameters))
    return result


def read_graph_shape(connection) -> dict:
    """Count the graph's nodes and relationships."""
    node_rows = connection.execute("MATCH (n) RETURN count(n) AS nodes")
    relationship_rows = connection.execute("MATCH ()-[r]->() RETURN count(r) AS relationships")
    if not node_rows or not relationship_rows:
        raise RuntimeError("graph database shape inspection returned no receipt")
    result = {
        "nodes": int(node_rows[0].get("nodes", -1)),
        "relationships": int(relationship_rows[0].get("relationships", -1)),
    }
    return result


def verify_schema(connection, schema_path) -> dict:
    """Check that the graph has every definition Engram's schema needs.

    Extra definitions are fine: the graph may serve a larger schema.
    """
    expected_catalog = validate_schema_contract(schema_path.read_text(encoding="utf-8"))
    catalog_report = compare_catalogs(expected_catalog, read_live_catalog(connection))
    result = {
        "valid": bool(catalog_report.get("valid", False)),
        "catalog": catalog_report,
    }
    return result


def install_schema(connection, schema_path) -> dict:
    """Install Engram's schema on an empty graph, or confirm a compatible one."""
    text = schema_path.read_text(encoding="utf-8")
    validate_schema_contract(text)
    shape = read_graph_shape(connection)
    if shape.get("nodes", -1) != 0 or shape.get("relationships", -1) != 0:
        raise RuntimeError("schema installation requires an empty graph")
    if catalog_has_definitions(read_live_catalog(connection)):
        report = verify_schema(connection, schema_path)
        if not report.get("valid", False):
            raise RuntimeError(f"live catalog lacks definitions Engram needs: {report.get('catalog', {}).get('missing', {})}")
        report["idempotent"] = True
        report["statements_applied"] = 0
        return report
    statements = cypher_statements(text)
    for statement in statements:
        execute_admin_statement(connection, statement)
    report = verify_schema(connection, schema_path)
    if not report.get("valid", False):
        raise RuntimeError(f"installed catalog verification failed: {report}")
    report["idempotent"] = False
    report["statements_applied"] = len(statements)
    return report
