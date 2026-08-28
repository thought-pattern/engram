"""Independent static and live catalog contracts for Engram Memgraph DDL."""

import re
from hashlib import sha256
from pathlib import Path

ORDINARY_INDEX_PATTERN = re.compile(
    r"CREATE\s+INDEX\s+ON\s+:(?P<label>[A-Za-z][A-Za-z0-9_]*)" r"\((?P<property>[A-Za-z][A-Za-z0-9_]*)\)",
    re.IGNORECASE,
)
TEXT_INDEX_PATTERN = re.compile(
    r"CREATE\s+TEXT\s+INDEX\s+(?P<name>[A-Za-z][A-Za-z0-9_]*)\s+ON\s+:"
    r"(?P<label>[A-Za-z][A-Za-z0-9_]*)\((?P<properties>[^)]+)\)",
    re.IGNORECASE,
)
VECTOR_INDEX_PATTERN = re.compile(
    r"CREATE\s+VECTOR\s+(?P<edge>EDGE\s+)?INDEX\s+"
    r"(?P<name>[A-Za-z][A-Za-z0-9_]*)\s+ON\s+:"
    r"(?P<label>[A-Za-z][A-Za-z0-9_]*)"
    r"\((?P<property>[A-Za-z][A-Za-z0-9_]*)\)\s+"
    r"WITH\s+CONFIG\s*\{(?P<config>.*)\}",
    re.IGNORECASE,
)
UNIQUE_CONSTRAINT_PATTERN = re.compile(
    r"CREATE\s+CONSTRAINT\s+ON\s+\((?P<variable>[A-Za-z][A-Za-z0-9_]*):"
    r"(?P<label>[A-Za-z][A-Za-z0-9_]*)\)\s+ASSERT\s+"
    r"(?P=variable)\.(?P<property>[A-Za-z][A-Za-z0-9_]*)\s+IS\s+UNIQUE",
    re.IGNORECASE,
)
EXISTS_CONSTRAINT_PATTERN = re.compile(
    r"CREATE\s+CONSTRAINT\s+ON\s+\((?P<variable>[A-Za-z][A-Za-z0-9_]*):"
    r"(?P<label>[A-Za-z][A-Za-z0-9_]*)\)\s+ASSERT\s+EXISTS\s*\("
    r"(?P=variable)\.(?P<property>[A-Za-z][A-Za-z0-9_]*)\)",
    re.IGNORECASE,
)
RELATIONSHIP_PATTERN = re.compile(
    r"^//\s+(?P<origin>[A-Za-z][A-Za-z0-9_]*)\s+-\[:" r"(?P<relationship>[A-Z][A-Z0-9_]*)\]->\s+(?P<target>.+?)\s*$",
    re.MULTILINE,
)
CONFIG_VALUE_PATTERN = re.compile(
    r'"(?P<name>dimension|capacity|metric|scalar_kind|resize_coefficient)"' r"\s*:\s*(?P<value>\"[^\"]*\"|[0-9]+)"
)
TEXT_INDEX_NAME_PATTERN = re.compile(r"name:\s*([A-Za-z][A-Za-z0-9_]*)", re.IGNORECASE)
REQUIRED_VECTOR_CONFIG_FIELDS = {"dimension", "capacity", "metric", "scalar_kind", "resize_coefficient"}

REQUIRED_IDENTITY_PROPERTIES = {
    ("SchemaRevision", "component"),
    ("GraphState", "name"),
    ("Source", "id"),
    ("SourceArtifact", "id"),
    ("Observation", "id"),
    ("Passage", "id"),
    ("ExtractionReceipt", "id"),
    ("AcquisitionReceipt", "id"),
    ("Proposition", "id"),
    ("Assertion", "id"),
    ("Entity", "canonical_id"),
    ("Predicate", "canonical_id"),
    ("SemanticDefinition", "id"),
    ("SemanticBinding", "id"),
}
REQUIRED_SOURCE_BASIS_INDEXES = {
    ("Source", "identity_namespace"),
    ("SourceArtifact", "artifact_locator_profile"),
    ("Observation", "event_key"),
    ("Passage", "passage_digest"),
}
REQUIRED_RELATIONSHIPS = {
    ("SourceArtifact", "FROM_SOURCE", "Source"),
    ("Observation", "FROM_SOURCE", "Source"),
    ("Passage", "FROM_BASIS", "SourceArtifact|Observation"),
    ("ExtractionReceipt", "SELECTS", "Passage"),
    ("ExtractionReceipt", "SELECTS_FROM", "Observation"),
    ("Assertion", "FROM_SOURCE", "Source"),
    ("Assertion", "FROM_BASIS", "SourceArtifact|Observation"),
    ("Assertion", "EXTRACTED_BY", "ExtractionReceipt"),
    ("Assertion", "ASSERTS", "Proposition"),
    ("Proposition", "USES_PREDICATE", "Predicate"),
    ("Proposition", "HAS_ARGUMENT", "SemanticBinding"),
    ("Proposition", "HAS_QUALIFICATION", "SemanticBinding"),
    ("Proposition", "HAS_CONTEXT", "SemanticBinding"),
    ("Proposition", "HAS_APPLICABILITY_SCOPE", "SemanticBinding"),
    ("Assertion", "HAS_QUALIFICATION", "SemanticBinding"),
    ("Assertion", "HAS_CONTEXT", "SemanticBinding"),
    ("SemanticBinding", "BINDS_ENTITY", "Entity"),
    ("SemanticBinding", "BINDS_PROPOSITION", "Proposition"),
    ("Proposition", "SUPPORTED_BY", "Assertion"),
    ("Proposition", "OPPOSED_BY", "Assertion"),
    ("Proposition", "CONTRADICTS", "Proposition"),
    ("SourceArtifact", "REVISES", "SourceArtifact"),
    ("Assertion", "SUPERSEDES", "Assertion"),
}
REQUIRED_TEXT_INDEXES = {("proposition_rendering", "Proposition", ("rendering",))}
REQUIRED_VECTOR_INDEXES = {
    (
        "proposition_embeddings",
        "Proposition",
        "embedding",
        "node",
        384,
        5000000,
        "cos",
        "f32",
        2,
    )
}


def schema_file_digest(path: Path) -> str:
    """Return the full SHA-256 digest of one schema file."""
    return sha256(path.read_bytes()).hexdigest()


def schema_ddl_digest(path: Path) -> str:
    """Digest only normalized executable DDL, excluding comments and layout."""
    statements = cypher_statements(path.read_text(encoding="utf-8"))
    normalized = [" ".join(statement.split()) for statement in statements]
    return sha256((";\n".join(normalized) + ";").encode("utf-8")).hexdigest()


def cypher_statements(text: str) -> list[str]:
    """Parse complete, unique, semicolon-terminated Cypher statements."""
    lines = []
    for line in text.splitlines():
        content = line.split("//", 1)[0].rstrip()
        if content.strip():
            lines.append(content)
    cleaned = "\n".join(lines).strip()
    if not cleaned or not cleaned.endswith(";"):
        raise ValueError("schema is empty or contains an unterminated statement")
    statements = [statement.strip() for statement in cleaned.split(";") if statement.strip()]
    normalized = [" ".join(statement.split()) for statement in statements]
    if len(normalized) != len(set(normalized)):
        raise ValueError("schema contains a duplicated statement")
    return statements


def vector_config(config_text: str) -> dict:
    """Parse one closed vector-index configuration mapping."""
    values = {}
    for match in CONFIG_VALUE_PATTERN.finditer(config_text):
        name = match.group("name")
        if name in values:
            raise ValueError(f"vector configuration duplicates {name}")
        value = match.group("value")
        values[name] = value[1:-1] if value.startswith('"') else int(value)
    if set(values) != REQUIRED_VECTOR_CONFIG_FIELDS:
        raise ValueError("vector configuration fields do not match the contract")
    return values


def normalized_vector_capacity(requested_capacity: int) -> int:
    """Return Memgraph's effective power-of-two vector capacity."""
    if isinstance(requested_capacity, bool) or not isinstance(requested_capacity, int):
        raise ValueError("vector capacity must be an integer")
    if requested_capacity <= 0:
        raise ValueError("vector capacity must be positive")
    return 1 << (requested_capacity - 1).bit_length()


def schema_catalog(text: str) -> dict:
    """Parse one schema into a deterministic native catalog mapping."""
    catalog = {
        "ordinary_indexes": [],
        "text_indexes": [],
        "vector_indexes": [],
        "constraints": [],
        "relationships": [],
    }
    for statement in cypher_statements(text):
        normalized = " ".join(statement.split())
        match = ORDINARY_INDEX_PATTERN.fullmatch(normalized)
        if match:
            catalog.get("ordinary_indexes", []).append({"label": match.group("label"), "property": match.group("property")})
            continue
        match = TEXT_INDEX_PATTERN.fullmatch(normalized)
        if match:
            catalog.get("text_indexes", []).append(
                {
                    "name": match.group("name"),
                    "label": match.group("label"),
                    "properties": tuple(value.strip() for value in match.group("properties").split(",")),
                }
            )
            continue
        match = VECTOR_INDEX_PATTERN.fullmatch(normalized)
        if match:
            config = vector_config(match.group("config"))
            catalog.get("vector_indexes", []).append(
                {
                    "name": match.group("name"),
                    "label": match.group("label"),
                    "property": match.group("property"),
                    "index_type": "edge" if match.group("edge") else "node",
                    "dimension": config.get("dimension", 0),
                    "requested_capacity": config.get("capacity", 0),
                    "effective_capacity": normalized_vector_capacity(config.get("capacity", 0)),
                    "metric": config.get("metric", ""),
                    "scalar_kind": config.get("scalar_kind", ""),
                    "resize_coefficient": config.get("resize_coefficient", 0),
                }
            )
            continue
        match = UNIQUE_CONSTRAINT_PATTERN.fullmatch(normalized)
        if match:
            catalog.get("constraints", []).append(
                {"constraint_type": "unique", "label": match.group("label"), "property": match.group("property")}
            )
            continue
        match = EXISTS_CONSTRAINT_PATTERN.fullmatch(normalized)
        if match:
            catalog.get("constraints", []).append(
                {"constraint_type": "exists", "label": match.group("label"), "property": match.group("property")}
            )
            continue
        raise ValueError(f"schema contains an unrecognized statement: {normalized[:120]}")
    catalog["relationships"] = [
        {"origin": match.group("origin"), "relationship": match.group("relationship"), "target": match.group("target")}
        for match in RELATIONSHIP_PATTERN.finditer(text)
    ]
    validate_catalog_uniqueness(catalog)
    return catalog


def validate_catalog_uniqueness(catalog: dict) -> None:
    """Reject duplicate physical definitions inside one parsed catalog."""
    for group in ("ordinary_indexes", "text_indexes", "vector_indexes", "constraints"):
        keys = catalog_keys(catalog, group)
        if len(keys) != len(catalog.get(group, [])):
            raise ValueError(f"schema catalog contains duplicate {group}")


def validate_schema_contract(text: str) -> dict:
    """Validate the corrected standalone Engram schema."""
    catalog = schema_catalog(text)
    executable = "\n".join(cypher_statements(text))
    forbidden = (":" + "Claim", "Candidate" + "Claim", "claim" + "_premise")
    if any(token in executable for token in forbidden):
        raise ValueError("schema contains a superseded semantic-record declaration")
    indexed = catalog_keys(catalog, "ordinary_indexes")
    constraints = catalog_keys(catalog, "constraints")
    unique_properties = {
        (label, property_name) for constraint_type, label, property_name in constraints if constraint_type == "unique"
    }
    existence_properties = {
        (label, property_name) for constraint_type, label, property_name in constraints if constraint_type == "exists"
    }
    if unique_properties - existence_properties or unique_properties - indexed:
        raise ValueError("every unique identity requires an existence constraint and lookup index")
    if not REQUIRED_IDENTITY_PROPERTIES.issubset(unique_properties):
        raise ValueError("Engram schema is missing a required corrected identity")
    if not REQUIRED_SOURCE_BASIS_INDEXES.issubset(indexed):
        raise ValueError("Engram source-basis indexes do not match the v1 contract")

    relationships = {
        (value.get("origin", ""), value.get("relationship", ""), value.get("target", ""))
        for value in catalog.get("relationships", [])
    }
    if not REQUIRED_RELATIONSHIPS.issubset(relationships):
        raise ValueError("Engram schema is missing a required corrected relationship")
    if catalog_keys(catalog, "text_indexes") != REQUIRED_TEXT_INDEXES:
        raise ValueError("Engram text index does not match its recall contract")

    vector_indexes = {
        (
            value.get("name", ""),
            value.get("label", ""),
            value.get("property", ""),
            value.get("index_type", ""),
            value.get("dimension", 0),
            value.get("requested_capacity", 0),
            value.get("metric", ""),
            value.get("scalar_kind", ""),
            value.get("resize_coefficient", 0),
        )
        for value in catalog.get("vector_indexes", [])
    }
    if vector_indexes != REQUIRED_VECTOR_INDEXES:
        raise ValueError("Engram vector catalog does not match its recall contract")
    return catalog


def row_properties(row: dict, singular: str, plural: str) -> tuple[str, ...]:
    """Normalize one Memgraph property or property-list catalog field."""
    value = row.get(plural, row.get(singular, []))
    if isinstance(value, str):
        return (value,)
    if isinstance(value, (list, tuple)):
        return tuple(str(item) for item in value)
    return ()


def live_catalog(index_rows: list[dict], constraint_rows: list[dict], vector_rows: list[dict]) -> dict:
    """Normalize Memgraph 3.9 SHOW results into the static catalog shape."""
    catalog = {"ordinary_indexes": [], "text_indexes": [], "vector_indexes": [], "constraints": [], "relationships": []}
    for row in index_rows:
        index_type = str(row.get("index type", row.get("index_type", "")) or "")
        if "vector" in index_type.casefold():
            continue
        properties = row_properties(row, "property", "properties")
        if "text" in index_type.casefold():
            name = str(row.get("name", "") or "")
            if not name:
                match = TEXT_INDEX_NAME_PATTERN.search(index_type)
                name = match.group(1) if match else ""
            catalog.get("text_indexes", []).append(
                {"name": name, "label": str(row.get("label", "") or ""), "properties": properties}
            )
        else:
            for property_name in properties:
                catalog.get("ordinary_indexes", []).append({"label": str(row.get("label", "") or ""), "property": property_name})
    for row in constraint_rows:
        constraint_type = str(row.get("constraint type", row.get("constraint_type", "")) or "").casefold()
        for property_name in row_properties(row, "property", "properties"):
            catalog.get("constraints", []).append(
                {"constraint_type": constraint_type, "label": str(row.get("label", "") or ""), "property": property_name}
            )
    for row in vector_rows:
        index_type = str(row.get("index_type", row.get("index type", "")) or "")
        catalog.get("vector_indexes", []).append(
            {
                "name": str(row.get("index_name", row.get("name", "")) or ""),
                "label": str(row.get("label", "") or ""),
                "property": str(row.get("property", "") or ""),
                "index_type": "edge" if "edge" in index_type.casefold() else "node",
                "dimension": int(row.get("dimension", 0) or 0),
                "effective_capacity": int(row.get("capacity", 0) or 0),
                "metric": str(row.get("metric", "") or ""),
                "scalar_kind": str(row.get("scalar_kind", "") or ""),
            }
        )
    return catalog


def catalog_keys(catalog: dict, group: str) -> set[tuple]:
    """Return comparable immutable keys for one catalog group."""
    values = catalog.get(group, [])
    if group == "ordinary_indexes":
        return {(value.get("label", ""), value.get("property", "")) for value in values}
    if group == "text_indexes":
        return {(value.get("name", ""), value.get("label", ""), tuple(value.get("properties", ()))) for value in values}
    if group == "constraints":
        return {(value.get("constraint_type", ""), value.get("label", ""), value.get("property", "")) for value in values}
    if group == "vector_indexes":
        return {
            (
                value.get("name", ""),
                value.get("label", ""),
                value.get("property", ""),
                value.get("index_type", ""),
                value.get("dimension", 0),
                value.get("effective_capacity", 0),
                value.get("metric", ""),
                value.get("scalar_kind", ""),
            )
            for value in values
        }
    raise ValueError(f"catalog group is unsupported: {group}")


def compare_catalogs(expected: dict, actual: dict, allow_superset: bool = False) -> dict:
    """Compare every live-exposed catalog field with one static schema."""
    missing = {}
    unexpected = {}
    for group in ("ordinary_indexes", "text_indexes", "constraints", "vector_indexes"):
        expected_keys = catalog_keys(expected, group)
        actual_keys = catalog_keys(actual, group)
        if expected_keys - actual_keys:
            missing[group] = sorted(expected_keys - actual_keys)
        if actual_keys - expected_keys and not allow_superset:
            unexpected[group] = sorted(actual_keys - expected_keys)
    return {
        "compatible": not missing and not unexpected,
        "missing": missing,
        "unexpected": unexpected,
        "static_only_vector_fields": ("requested_capacity", "resize_coefficient"),
    }


def read_live_catalog(connection) -> dict:
    """Read all live catalogs through read-only Engram connection methods."""
    ready = connection.execute_read("RETURN 1 AS ready")
    if not ready or ready[0].get("ready", 0) != 1:
        raise RuntimeError("Memgraph catalog connection is unavailable")
    return live_catalog(
        connection.execute_read("SHOW INDEX INFO"),
        connection.execute_read("SHOW CONSTRAINT INFO"),
        connection.execute_read("SHOW VECTOR INDEX INFO"),
    )
