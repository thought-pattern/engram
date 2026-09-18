"""Exact corrected support-reference fixtures for Engram tests."""

GLOBAL_VISIBILITY = {
    "kind": "global",
    "company_id": {},
    "customer_id": {},
    "engagement_id": {},
}

ASSERTION_REFERENCE_A = {
    "schema_version": "tapestry-engram-support",
    "record_kind": "assertion",
    "id": "ast_" + "a" * 64,
    "state_revision": 0,
    "support_revision": {},
    "representation_contract": "tapestry-ke-representation",
    "visibility_scope": GLOBAL_VISIBILITY,
    "dependency_state_digest": "dep_" + "a" * 64,
}
