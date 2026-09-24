"""Standalone and Tapestry-managed Engram schema contract tests."""

from pathlib import Path

from pytest import raises

from engram.schema_admin import install_standalone_schema, verify_schema
from engram.schema_catalog import (
    schema_catalog,
    schema_ddl_digest,
    validate_schema_contract,
)

ENGRAM_ROOT = Path(__file__).resolve().parents[1]
SCHEMA_FILE = ENGRAM_ROOT / "schema.cypher"
CATALOG = schema_catalog(SCHEMA_FILE.read_text(encoding="utf-8"))
DEFAULT_PARAMETERS = {}
STANDALONE_METADATA = {
    "component": "engram",
    "deployment_owner": "engram",
    "representation_contract": "tapestry-ke-representation-v1",
    "proof_scratch_contract": {},
    "engram_support_contract": "tapestry-engram-support-v1",
    "schema_digest": schema_ddl_digest(SCHEMA_FILE),
    "graph_state_name": "engram_graph",
    "graph_deployment_owner": "engram",
    "installation_state": "schema_installed",
    "graph_revision": 0,
}
TAPESTRY_METADATA = {
    "component": "tapestry",
    "deployment_owner": "tapestry",
    "representation_contract": "tapestry-ke-representation-v1",
    "proof_scratch_contract": "tapestry-proof-scratch-v1",
    "engram_support_contract": "tapestry-engram-support-v1",
    "schema_digest": "root-schema-digest",
    "graph_state_name": "tapestry_knowledge_graph",
    "graph_deployment_owner": "tapestry",
    "installation_state": "accepted",
    "graph_revision": 42,
}


class RecordingConnection:
    """Serve one fixed catalog and record all graph operations."""

    def __init__(self, metadata: dict):
        self.metadata = dict(metadata)
        self.reads = []
        self.writes = []
        self.conn = {}

    def execute(self, query: str, parameters: dict = DEFAULT_PARAMETERS) -> list:
        self.reads.append({"query": query, "parameters": dict(parameters)})
        if query == "RETURN 1 AS ready":
            return [{"ready": 1}]
        if query == "SHOW INDEX INFO":
            rows = [
                {
                    "index type": "label+property",
                    "label": value.get("label", ""),
                    "property": [value.get("property", "")],
                }
                for value in CATALOG.get("ordinary_indexes", [])
            ]
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
        if "application_nodes" in query:
            return [{"application_nodes": 0}]
        if "metadata_nodes" in query:
            return [{"metadata_nodes": 2}]
        if "count(r) AS relationships" in query:
            return [{"relationships": 0}]
        if query.startswith("MATCH (s:SchemaRevision)"):
            result = [dict(self.metadata)]
            return result
        if query.startswith("MATCH (g:GraphState)"):
            result = [dict(self.metadata)]
            return result
        raise AssertionError(f"unexpected read: {query}")


class FailingAdministrativeCursor:
    """Raise on the third standalone DDL statement."""

    description = []

    def __init__(self, writes: list):
        self.writes = writes

    def cursor(self):
        return self

    def execute(self, query: str, parameters: dict = DEFAULT_PARAMETERS) -> None:
        self.writes.append({"query": query, "parameters": dict(parameters)})
        if len(self.writes) == 3:
            raise RuntimeError("injected standalone DDL failure")

    def fetchall(self) -> list:
        return []


class FailingAdministrativeConnection(RecordingConnection):
    """Expose raw cursor failure on the third standalone DDL statement."""

    def __init__(self):
        super().__init__({})
        self.conn = FailingAdministrativeCursor(self.writes)

    def execute(self, query: str, parameters: dict = DEFAULT_PARAMETERS) -> list:
        if "application_nodes" in query:
            return [{"application_nodes": 0}]
        if "metadata_nodes" in query:
            return [{"metadata_nodes": 0}]
        if "count(r) AS relationships" in query:
            return [{"relationships": 0}]
        if query == "RETURN 1 AS ready":
            return [{"ready": 1}]
        if query in {"SHOW INDEX INFO", "SHOW CONSTRAINT INFO", "SHOW VECTOR INDEX INFO"}:
            return []
        raise AssertionError(f"unexpected read: {query}")


class MixedMetadataConnection(RecordingConnection):
    """Expose two schema owners while keeping the catalog otherwise valid."""

    def execute(self, query: str, parameters: dict = DEFAULT_PARAMETERS) -> list:
        if query.startswith("MATCH (s:SchemaRevision)"):
            engram = dict(STANDALONE_METADATA)
            tapestry = dict(TAPESTRY_METADATA)
            return [engram, tapestry]
        result = super().execute(query, parameters)
        return result


class InvalidVectorConnection(RecordingConnection):
    """Return the right vector name with a contract-breaking dimension."""

    def execute(self, query: str, parameters: dict = DEFAULT_PARAMETERS) -> list:
        rows = super().execute(query, parameters)
        if query == "SHOW VECTOR INDEX INFO":
            rows[0].update({"dimension": 768})
        return rows


def test_standalone_install_is_exact_idempotent_and_read_only() -> None:
    connection = RecordingConnection(STANDALONE_METADATA)
    report = install_standalone_schema(connection, SCHEMA_FILE)
    assert report.get("valid", False) is True
    assert report.get("idempotent", False) is True
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


def test_tapestry_managed_verification_requires_accepted_tapestry_owner() -> None:
    connection = RecordingConnection(TAPESTRY_METADATA)
    report = verify_schema(connection, SCHEMA_FILE, "tapestry_managed")
    assert report.get("valid", False) is True
    assert connection.writes == []
    assert all(
        not record.get("query", "").lstrip().upper().startswith(("CREATE ", "DROP ", "DELETE ", "SET ", "MERGE "))
        for record in connection.reads
    )


def test_mode_owner_mismatch_fails_closed() -> None:
    connection = RecordingConnection(STANDALONE_METADATA)
    report = verify_schema(connection, SCHEMA_FILE, "tapestry_managed")
    assert report.get("valid", True) is False
    assert "deployment_owner" in report.get("metadata", {}).get("mismatched", {})
    assert connection.writes == []


def test_standalone_installer_rejects_tapestry_owner() -> None:
    connection = RecordingConnection(TAPESTRY_METADATA)
    try:
        install_standalone_schema(connection, SCHEMA_FILE)
    except RuntimeError as err:
        assert "partial, foreign, or invalid for this release" in str(err)
    else:
        raise AssertionError("standalone installer accepted Tapestry ownership")
    assert connection.writes == []


def test_standalone_installer_stops_on_first_ddl_failure() -> None:
    connection = FailingAdministrativeConnection()
    try:
        install_standalone_schema(connection, SCHEMA_FILE)
    except RuntimeError as err:
        assert "injected standalone DDL failure" in str(err)
    else:
        raise AssertionError("standalone installer continued after DDL failure")
    assert len(connection.writes) == 3


def test_mixed_schema_owners_fail_closed_without_writes() -> None:
    connection = MixedMetadataConnection(STANDALONE_METADATA)
    report = verify_schema(connection, SCHEMA_FILE, "standalone")
    assert report.get("valid", True) is False
    assert "schema_revision_count" in report.get("metadata", {}).get("mismatched", {})
    assert connection.writes == []


def test_invalid_vector_shape_fails_closed_without_writes() -> None:
    connection = InvalidVectorConnection(STANDALONE_METADATA)
    report = verify_schema(connection, SCHEMA_FILE, "standalone")
    assert report.get("valid", True) is False
    assert "vector_indexes" in report.get("catalog", {}).get("missing", {})
    assert connection.writes == []
