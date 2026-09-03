"""Opaque Tapestry support-reference contract implemented by Engram.

Engram validates structure for safe storage and transport. It does not
interpret epistemic state; Tapestry performs current-state validation.
"""

SUPPORT_CONTRACT = "tapestry-engram-support-v1"
REPRESENTATION_CONTRACT = "tapestry-ke-representation-v1"
SUPPORT_REFERENCE_FIELDS = {
    "schema_version",
    "record_kind",
    "id",
    "state_revision",
    "support_revision",
    "representation_contract",
    "visibility_scope",
    "dependency_state_digest",
}
VISIBILITY_FIELDS = {"kind", "company_id", "customer_id", "engagement_id"}


def support_text(value, name: str, *, allow_empty: bool = False) -> str:
    """Validate exact support text."""
    if not isinstance(value, str) or (not allow_empty and not value):
        raise ValueError(f"{name} must be a non-empty string")
    return value


def support_revision(value, name: str) -> int:
    """Validate a non-negative support revision."""
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError(f"{name} must be a non-negative integer")
    return value


def validate_support_visibility(value) -> dict:
    """Validate opaque visibility without widening its exact identifiers."""
    if not isinstance(value, dict) or set(value) != VISIBILITY_FIELDS:
        raise ValueError("support visibility_scope has an invalid shape")
    kind = support_text(value.get("kind", ""), "support visibility kind")
    company_id = value.get("company_id", {})
    customer_id = value.get("customer_id", {})
    engagement_id = value.get("engagement_id", {})
    if kind == "global":
        if any(item != {} for item in (company_id, customer_id, engagement_id)):
            raise ValueError("global support visibility requires empty identifiers")
    elif kind == "company":
        company_id = support_text(company_id, "support company_id")
        if customer_id != {} or engagement_id != {}:
            raise ValueError("company support visibility has customer identifiers")
    elif kind == "engagement":
        company_id = support_text(company_id, "support company_id")
        customer_id = support_text(customer_id, "support customer_id")
        engagement_id = support_text(engagement_id, "support engagement_id")
    else:
        raise ValueError("support visibility kind is not registered")
    return {
        "kind": kind,
        "company_id": company_id,
        "customer_id": customer_id,
        "engagement_id": engagement_id,
    }


def validate_support_reference(value) -> dict:
    """Validate and defensively copy one opaque Tapestry support reference."""
    if not isinstance(value, dict) or set(value) != SUPPORT_REFERENCE_FIELDS:
        raise ValueError("support reference has an invalid shape")
    if value.get("schema_version", "") != SUPPORT_CONTRACT:
        raise ValueError("support reference schema_version is unsupported")
    if value.get("representation_contract", "") != REPRESENTATION_CONTRACT:
        raise ValueError("support reference representation contract is unsupported")
    kind = support_text(value.get("record_kind", ""), "support record_kind")
    if kind not in {"assertion", "proposition"}:
        raise ValueError("support record_kind is not registered")
    identifier = support_text(value.get("id", ""), "support identifier")
    expected_prefix = "ast_" if kind == "assertion" else "prp_"
    if not identifier.startswith(expected_prefix) or len(identifier) != 68:
        raise ValueError("support identifier does not match record_kind")
    state_revision = support_revision(value.get("state_revision", {}), "support state_revision")
    proposition_revision = value.get("support_revision", {})
    if kind == "proposition":
        proposition_revision = support_revision(proposition_revision, "support support_revision")
    elif proposition_revision != {}:
        raise ValueError("Assertion support_revision must be {}")
    digest = support_text(value.get("dependency_state_digest", ""), "support dependency_state_digest")
    if not digest.startswith("dep_") or len(digest) != 68:
        raise ValueError("support dependency_state_digest is malformed")
    result = {
        "schema_version": SUPPORT_CONTRACT,
        "record_kind": kind,
        "id": identifier,
        "state_revision": state_revision,
        "support_revision": proposition_revision,
        "representation_contract": REPRESENTATION_CONTRACT,
        "visibility_scope": validate_support_visibility(value.get("visibility_scope", {})),
        "dependency_state_digest": digest,
    }
    return result


def validate_support_references(value) -> tuple:
    """Validate an ordered duplicate-free tuple of opaque references."""
    if not isinstance(value, tuple):
        raise ValueError("support_references must be a tuple")
    references = tuple(validate_support_reference(item) for item in value)
    keys = tuple((item.get("record_kind", ""), item.get("id", "")) for item in references)
    if len(keys) != len(set(keys)):
        raise ValueError("support_references contain duplicate durable records")
    return references
