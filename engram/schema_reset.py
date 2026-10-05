"""Exact, data-preserving reset of Engram's schema catalog."""

from engram.schema_admin import execute_admin_statement, read_graph_shape
from engram.schema_catalog import (
    catalog_keys,
    read_live_catalog,
    validate_schema_contract,
)


def prove_empty_graph(connection) -> dict:
    """Require zero nodes and relationships before catalog removal."""
    shape = read_graph_shape(connection)
    if shape.get("nodes", -1) != 0 or shape.get("relationships", -1) != 0:
        raise RuntimeError(f"schema reset requires an empty graph: {shape}")
    return shape


def allowed_catalog_keys(schema_path) -> dict:
    """Return the exact definitions in Engram's schema."""
    corrected = validate_schema_contract(schema_path.read_text(encoding="utf-8"))
    result = {
        "ordinary_indexes": catalog_keys(corrected, "ordinary_indexes"),
        "text_indexes": catalog_keys(corrected, "text_indexes"),
        "vector_indexes": catalog_keys(corrected, "vector_indexes"),
        "constraints": catalog_keys(corrected, "constraints"),
    }
    return result


def catalog_drop_statements(catalog: dict) -> list[str]:
    """Render exact drops from the validated live catalog."""
    statements = []
    for value in sorted(catalog.get("vector_indexes", []), key=lambda item: item.get("name", "")):
        statements.append(f"DROP VECTOR INDEX {value.get('name', '')}")
    for value in sorted(catalog.get("text_indexes", []), key=lambda item: item.get("name", "")):
        statements.append(f"DROP TEXT INDEX {value.get('name', '')}")
    for value in sorted(
        catalog.get("ordinary_indexes", []),
        key=lambda item: (item.get("label", ""), item.get("property", "")),
    ):
        statements.append(f"DROP INDEX ON :{value.get('label', '')}({value.get('property', '')})")
    for value in sorted(
        catalog.get("constraints", []),
        key=lambda item: (
            item.get("label", ""),
            item.get("property", ""),
            item.get("constraint_type", ""),
        ),
    ):
        label = value.get("label", "")
        property_name = value.get("property", "")
        if value.get("constraint_type", "") == "exists":
            statements.append(f"DROP CONSTRAINT ON (n:{label}) ASSERT EXISTS (n.{property_name})")
        else:
            statements.append(f"DROP CONSTRAINT ON (n:{label}) ASSERT n.{property_name} IS UNIQUE")
    return statements


def resolve_schema_reset(connection, schema_path) -> dict:
    """Resolve reset targets only when every live definition is recognized."""
    shape = prove_empty_graph(connection)
    catalog = read_live_catalog(connection)
    allowed = allowed_catalog_keys(schema_path)
    unrecognized = {}
    for group in ("ordinary_indexes", "text_indexes", "vector_indexes", "constraints"):
        values = catalog_keys(catalog, group) - allowed.get(group, set())
        if values:
            unrecognized[group] = sorted(values)
    if unrecognized:
        raise RuntimeError(f"schema reset found unrecognized definitions: {unrecognized}")
    result = {"valid": True, "graph": shape, "statements": catalog_drop_statements(catalog)}
    return result


def apply_schema_reset(connection, schema_path) -> dict:
    """Apply exact drops and prove the complete catalog is empty."""
    report = resolve_schema_reset(connection, schema_path)
    for statement in report.get("statements", []):
        execute_admin_statement(connection, statement)
    remaining = read_live_catalog(connection)
    residual = {
        group: sorted(catalog_keys(remaining, group))
        for group in ("ordinary_indexes", "text_indexes", "vector_indexes", "constraints")
        if catalog_keys(remaining, group)
    }
    if residual:
        raise RuntimeError(f"schema reset left residual definitions: {residual}")
    report["applied"] = True
    return report
