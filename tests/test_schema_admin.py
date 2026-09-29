"""Engram schema installation and graph compatibility tests."""

from pytest import raises

from engram.schema_admin import install_schema, verify_schema
from engram.schema_catalog import packaged_schema, schema_catalog, validate_schema_contract

SCHEMA_FILE = packaged_schema()
CATALOG = schema_catalog(SCHEMA_FILE.read_text(encoding="utf-8"))
DEFAULT_PARAMETERS = {}
WRITE_PREFIXES = ("CREATE ", "DROP ", "DELETE ", "SET ", "MERGE ")


class RecordingConnection:
    """Serve Engram's catalog, optionally altered, and record all graph operations."""

    def __init__(self, nodes: int = 0, extra_index: bool = False, missing_index: bool = False):
        self.nodes = nodes
        self.extra_index = extra_index
        self.missing_index = missing_index
        self.reads = []
        self.writes = []

    def execute_admin(self, statement: str, parameters: dict = DEFAULT_PARAMETERS) -> list:
        self.writes.append({"query": statement, "parameters": dict(parameters)})
        return []

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


class FailingAdministrativeConnection(RecordingConnection):
    """Expose the driver's own failure on the third DDL statement of a fresh install."""

    def execute_admin(self, statement: str, parameters: dict = DEFAULT_PARAMETERS) -> list:
        result = super().execute_admin(statement, parameters)
        if len(self.writes) == 3:
            raise RuntimeError("injected DDL failure")
        return result

    def execute(self, query: str, parameters: dict = DEFAULT_PARAMETERS) -> list:
        if query in {"SHOW INDEX INFO", "SHOW CONSTRAINT INFO", "SHOW VECTOR INDEX INFO"}:
            return []
        result = super().execute(query, parameters)
        return result


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


def test_install_on_a_compatible_graph_is_idempotent_and_read_only() -> None:
    connection = RecordingConnection()
    report = install_schema(connection, SCHEMA_FILE)
    assert report.get("valid", False) is True
    assert report.get("idempotent", False) is True
    assert connection.writes == []


def test_a_graph_with_a_larger_schema_is_compatible() -> None:
    connection = RecordingConnection(nodes=12, extra_index=True)
    report = verify_schema(connection, SCHEMA_FILE)
    assert report.get("valid", False) is True
    assert connection.writes == []
    assert all(not record.get("query", "").lstrip().upper().startswith(WRITE_PREFIXES) for record in connection.reads)


def test_a_graph_missing_a_needed_definition_is_incompatible() -> None:
    connection = RecordingConnection(missing_index=True)
    report = verify_schema(connection, SCHEMA_FILE)
    assert report.get("valid", True) is False
    assert "ordinary_indexes" in report.get("catalog", {}).get("missing", {})
    assert connection.writes == []


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


def test_installer_requires_an_empty_graph() -> None:
    connection = RecordingConnection(nodes=1)
    with raises(RuntimeError, match="requires an empty graph"):
        install_schema(connection, SCHEMA_FILE)
    assert connection.writes == []


def test_installer_refuses_a_partial_catalog_without_writes() -> None:
    connection = RecordingConnection(missing_index=True)
    with raises(RuntimeError, match="lacks definitions Engram needs"):
        install_schema(connection, SCHEMA_FILE)
    assert connection.writes == []


def test_installer_stops_on_first_ddl_failure() -> None:
    connection = FailingAdministrativeConnection()
    with raises(RuntimeError, match="injected DDL failure"):
        install_schema(connection, SCHEMA_FILE)
    assert len(connection.writes) == 3


def test_vector_index_storage_sizing_does_not_affect_compatibility() -> None:
    connection = ResizedVectorConnection()
    report = verify_schema(connection, SCHEMA_FILE)
    assert report.get("valid", False) is True
    assert connection.writes == []


def test_invalid_vector_shape_fails_closed_without_writes() -> None:
    connection = InvalidVectorConnection()
    report = verify_schema(connection, SCHEMA_FILE)
    assert report.get("valid", True) is False
    assert "vector_indexes" in report.get("catalog", {}).get("missing", {})
    assert connection.writes == []
