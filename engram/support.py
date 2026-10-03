"""Opaque support references.

Engram validates structure for safe storage and transport. It does not
interpret epistemic state; the producer performs current-state validation.
A reference is accepted on its shape, not on the contract versions it names.
"""

from engram.constants import MAX_METADATA_BYTES, MAX_SUPPORT_REVISION
from engram.validation import require_any_text

MAX_CONTRACT_NAME_BYTES = 128
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


def support_revision(value, name: str) -> int:
    """Validate a non-negative support revision the Struct transport carries exactly.

    A larger value would round in transit, so it is refused before storage rather
    than returned to its producer as a different revision.
    """
    if not isinstance(value, int) or isinstance(value, bool) or not 0 <= value <= MAX_SUPPORT_REVISION:
        raise ValueError(f"{name} must be an integer from 0 through {MAX_SUPPORT_REVISION}")
    return value


def validate_support_visibility(value) -> dict:
    """Validate opaque visibility without widening its exact identifiers."""
    if not isinstance(value, dict) or set(value) != VISIBILITY_FIELDS:
        raise ValueError("support visibility_scope has an invalid shape")
    kind = require_any_text(value.get("kind", ""), "support visibility kind", MAX_METADATA_BYTES)
    company_id = value.get("company_id", {})
    customer_id = value.get("customer_id", {})
    engagement_id = value.get("engagement_id", {})
    # Absent identifiers are rebuilt as owned empty mappings so the validated
    # copy never aliases a caller's mutable dictionary.
    if kind == "global":
        if any(item != {} for item in (company_id, customer_id, engagement_id)):
            raise ValueError("global support visibility requires empty identifiers")
        company_id, customer_id, engagement_id = {}, {}, {}
    elif kind == "company":
        company_id = require_any_text(company_id, "support company_id", MAX_METADATA_BYTES)
        if customer_id != {} or engagement_id != {}:
            raise ValueError("company support visibility has customer identifiers")
        customer_id, engagement_id = {}, {}
    elif kind == "engagement":
        company_id = require_any_text(company_id, "support company_id", MAX_METADATA_BYTES)
        customer_id = require_any_text(customer_id, "support customer_id", MAX_METADATA_BYTES)
        engagement_id = require_any_text(engagement_id, "support engagement_id", MAX_METADATA_BYTES)
    else:
        raise ValueError("support visibility kind is not registered")
    return {
        "kind": kind,
        "company_id": company_id,
        "customer_id": customer_id,
        "engagement_id": engagement_id,
    }


def validate_support_reference(value) -> dict:
    """Validate and defensively copy one opaque support reference."""
    if not isinstance(value, dict) or set(value) != SUPPORT_REFERENCE_FIELDS:
        raise ValueError("support reference has an invalid shape")
    schema_version = require_any_text(value.get("schema_version", ""), "support schema_version", MAX_CONTRACT_NAME_BYTES)
    representation_contract = require_any_text(
        value.get("representation_contract", ""),
        "support representation_contract",
        MAX_CONTRACT_NAME_BYTES,
    )
    kind = require_any_text(value.get("record_kind", ""), "support record_kind", MAX_METADATA_BYTES)
    if kind not in {"assertion", "proposition"}:
        raise ValueError("support record_kind is not registered")
    identifier = require_any_text(value.get("id", ""), "support identifier", MAX_METADATA_BYTES)
    state_revision = support_revision(value.get("state_revision", {}), "support state_revision")
    proposition_revision = value.get("support_revision", {})
    if kind == "proposition":
        proposition_revision = support_revision(proposition_revision, "support support_revision")
    elif proposition_revision != {}:
        raise ValueError("Assertion support_revision must be {}")
    else:
        proposition_revision = {}
    digest = require_any_text(value.get("dependency_state_digest", ""), "support dependency_state_digest", MAX_METADATA_BYTES)
    if not digest.startswith("dep_") or len(digest) != 68:
        raise ValueError("support dependency_state_digest is malformed")
    result = {
        "schema_version": schema_version,
        "record_kind": kind,
        "id": identifier,
        "state_revision": state_revision,
        "support_revision": proposition_revision,
        "representation_contract": representation_contract,
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


def validate_statement_scope_bindings(value) -> tuple:
    """Keep removal ownership after accepted artifacts or full receipts expire."""
    if not isinstance(value, tuple):
        raise ValueError("statement scope bindings must be a tuple")
    bindings = []
    seen = set()
    for item in value:
        if not isinstance(item, dict) or set(item) != {"statement_id", "visibility_scope"}:
            raise ValueError("statement scope binding fields are malformed")
        identifier = require_any_text(item.get("statement_id"), "scope binding statement_id", MAX_METADATA_BYTES)
        scope = validate_support_visibility(item.get("visibility_scope"))
        key = (identifier, scope.get("kind"), *(scope.get(field) or "" for field in ("company_id", "customer_id", "engagement_id")))
        if key in seen:
            raise ValueError("statement scope bindings contain a duplicate")
        seen.add(key)
        bindings.append({"statement_id": identifier, "visibility_scope": scope})
    return tuple(bindings)
