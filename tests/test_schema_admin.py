"""Engram graph compatibility tests; every check only reads the graph."""

from pytest import raises

from engram.schema_admin import verify_schema
from engram.schema_catalog import packaged_schema, schema_catalog, validate_schema_contract

SCHEMA_FILE = packaged_schema()
CATALOG = schema_catalog(SCHEMA_FILE.read_text(encoding="utf-8"))
DEFAULT_PARAMETERS = {}
WRITE_PREFIXES = ("CREATE ", "DROP ", "DELETE ", "SET ", "MERGE ")


class RecordingConnection:
    """Serve Engram's catalog, optionally altered, and record every graph read."""

    def __init__(self, nodes: int = 0, extra_index: bool = False, missing_index: bool = False):
        self.nodes = nodes
        self.extra_index = extra_index
        self.missing_index = missing_index
        self.reads = []

    def execute(self, query: str, parameters: dict = DEFAULT_PARAMETERS) -> list:
        self.reads.append({"query": query, "parameters": dict(parameters)})
        if query == "RETURN 1 AS ready":
            return [{"ready": 1}]
        if query == "SHOW INDEX INFO":
            ordinary = CATALOG.get("ordinary_indexes", [])[1:] if self.missing_index else CATALOG.get("ordinary_indexes", [])
            rows = [
                {
                    "index type": "label+property",
                    "label": value.get("label", ""),
                    "property": [value.get("property", "")],
                }
                for value in ordinary
            ]
            if self.extra_index:
                rows.append({"index type": "label+property", "label": "Unrelated", "property": ["name"]})
            rows.extend(
                {
                    "index type": f"label_text (name: {value.get('name', '')})",
                    "label": value.get("label", ""),
                    "property": list(value.get("properties", ())),
                }
                for value in CATALOG.get("text_indexes", [])
            )
            rows.extend(
                {
                    "index type": "label+property_vector",
                    "label": value.get("label", ""),
                    "property": value.get("property", ""),
                }
                for value in CATALOG.get("vector_indexes", [])
            )
            return rows
        if query == "SHOW CONSTRAINT INFO":
            result = [
                {
                    "constraint type": value.get("constraint_type", ""),
                    "label": value.get("label", ""),
                    "properties": [value.get("property", "")],
                }
                for value in CATALOG.get("constraints", [])
            ]
            return result
        if query == "SHOW VECTOR INDEX INFO":
            result = [
                {
                    "index_name": value.get("name", ""),
                    "label": value.get("label", ""),
                    "property": value.get("property", ""),
                    "capacity": value.get("effective_capacity", 0),
                    "dimension": value.get("dimension", 0),
                    "metric": value.get("metric", ""),
                    "size": 0,
                    "scalar_kind": value.get("scalar_kind", ""),
                    "index_type": "label+property_vector",
                }
                for value in CATALOG.get("vector_indexes", [])
            ]
            return result
        if query == "MATCH (n) RETURN count(n) AS nodes":
            return [{"nodes": self.nodes}]
        if query == "MATCH ()-[r]->() RETURN count(r) AS relationships":
            return [{"relationships": 0}]
        raise AssertionError(f"unexpected read: {query}")


class InvalidVectorConnection(RecordingConnection):
    """Return the right vector name with a contract-breaking dimension."""

    def execute(self, query: str, parameters: dict = DEFAULT_PARAMETERS) -> list:
        rows = super().execute(query, parameters)
        if query == "SHOW VECTOR INDEX INFO":
            rows[0].update({"dimension": 768})
        return rows


class ResizedVectorConnection(RecordingConnection):
    """Return the vector index with different storage sizing."""

    def execute(self, query: str, parameters: dict = DEFAULT_PARAMETERS) -> list:
        rows = super().execute(query, parameters)
        if query == "SHOW VECTOR INDEX INFO":
            rows[0].update({"capacity": 16_777_216, "scalar_kind": "f16"})
        return rows


def test_a_graph_with_a_larger_schema_is_compatible() -> None:
    connection = RecordingConnection(nodes=12, extra_index=True)
    report = verify_schema(connection, SCHEMA_FILE)
    assert report.get("valid", False) is True
    assert all(not record.get("query", "").lstrip().upper().startswith(WRITE_PREFIXES) for record in connection.reads)


def test_a_graph_missing_a_needed_definition_is_incompatible() -> None:
    connection = RecordingConnection(missing_index=True)
    report = verify_schema(connection, SCHEMA_FILE)
    assert report.get("valid", True) is False
    assert "ordinary_indexes" in report.get("catalog", {}).get("missing", {})
    assert all(not record.get("query", "").lstrip().upper().startswith(WRITE_PREFIXES) for record in connection.reads)


def test_static_contract_rejects_required_relationship_and_vector_drift() -> None:
    text = SCHEMA_FILE.read_text(encoding="utf-8")
    missing_relationship = text.replace(
        "// Assertion -[:ASSERTS]-> Proposition",
        "// Assertion ASSERTS relationship removed",
        1,
    )
    with raises(ValueError, match="required corrected relationship"):
        validate_schema_contract(missing_relationship)

    wrong_vector_shape = text.replace('"dimension": 384', '"dimension": 385', 1)
    with raises(ValueError, match="vector catalog"):
        validate_schema_contract(wrong_vector_shape)


def test_vector_index_storage_sizing_does_not_affect_compatibility() -> None:
    connection = ResizedVectorConnection()
    report = verify_schema(connection, SCHEMA_FILE)
    assert report.get("valid", False) is True
    assert all(not record.get("query", "").lstrip().upper().startswith(WRITE_PREFIXES) for record in connection.reads)


def test_invalid_vector_shape_fails_closed_without_writes() -> None:
    connection = InvalidVectorConnection()
    report = verify_schema(connection, SCHEMA_FILE)
    assert report.get("valid", True) is False
    assert "vector_indexes" in report.get("catalog", {}).get("missing", {})
    assert all(not record.get("query", "").lstrip().upper().startswith(WRITE_PREFIXES) for record in connection.reads)
