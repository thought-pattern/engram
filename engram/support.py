"""Opaque support references.

Engram validates structure for safe storage and transport. It does not
interpret epistemic state; the producer performs current-state validation.
"""

from engram.constants_artifacts import MAX_METADATA_BYTES, MAX_SUPPORT_REVISION
from engram.validation import require_any_text

SUPPORT_REFERENCE_FIELDS = {
    "record_kind",
    "id",
    "state_revision",
    "support_revision",
    "dependency_state_digest",
}


def support_revision(value, name: str) -> int:
    """Validate a non-negative support revision the Struct transport carries exactly.

    A larger value would round in transit, so it is refused before storage rather
    than returned to its producer as a different revision.
    """
    if not isinstance(value, int) or isinstance(value, bool) or not 0 <= value <= MAX_SUPPORT_REVISION:
        raise ValueError(f"{name} must be an integer from 0 through {MAX_SUPPORT_REVISION}")
    return value


def validate_support_reference(value) -> dict:
    """Validate and defensively copy one opaque support reference."""
    if not isinstance(value, dict) or set(value) != SUPPORT_REFERENCE_FIELDS:
        raise ValueError("support reference has an invalid shape")
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
        "record_kind": kind,
        "id": identifier,
        "state_revision": state_revision,
        "support_revision": proposition_revision,
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
