"""Read-only check that a graph has the definitions Engram needs."""

from engram.schema_catalog import compare_catalogs, read_live_catalog, validate_schema_contract


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
