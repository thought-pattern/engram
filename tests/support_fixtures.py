"""Exact corrected support-reference fixtures for Engram tests."""

GLOBAL_VISIBILITY = {
    "kind": "global",
    "company_id": {},
    "customer_id": {},
    "engagement_id": {},
}

ASSERTION_REFERENCE_A = {
    "schema_version": "tapestry-engram-support-v1",
    "record_kind": "assertion",
    "id": "ast_" + "a" * 64,
    "state_revision": 0,
    "support_revision": {},
    "representation_contract": "tapestry-ke-representation-v1",
    "visibility_scope": GLOBAL_VISIBILITY,
    "dependency_state_digest": "dep_" + "a" * 64,
    "store_epoch": "test-store-epoch",
}

ASSERTION_REFERENCE_B = {
    **ASSERTION_REFERENCE_A,
    "id": "ast_" + "b" * 64,
    "dependency_state_digest": "dep_" + "b" * 64,
}

ASSERTION_REFERENCE_C = {
    **ASSERTION_REFERENCE_A,
    "id": "ast_" + "c" * 64,
    "dependency_state_digest": "dep_" + "c" * 64,
}

ASSERTION_REFERENCE_D = {
    **ASSERTION_REFERENCE_A,
    "id": "ast_" + "d" * 64,
    "dependency_state_digest": "dep_" + "d" * 64,
}

PROPOSITION_REFERENCE_A = {
    **ASSERTION_REFERENCE_A,
    "record_kind": "proposition",
    "id": "prp_" + "a" * 64,
    "support_revision": 0,
}

PROPOSITION_REFERENCE_B = {
    **PROPOSITION_REFERENCE_A,
    "id": "prp_" + "b" * 64,
    "dependency_state_digest": "dep_" + "b" * 64,
}

PROPOSITION_REFERENCE_C = {
    **PROPOSITION_REFERENCE_A,
    "id": "prp_" + "c" * 64,
    "dependency_state_digest": "dep_" + "c" * 64,
}

REFERENCE_IDS = {
    "a": ASSERTION_REFERENCE_A.get("id", ""),
    "b": ASSERTION_REFERENCE_B.get("id", ""),
    "c": ASSERTION_REFERENCE_C.get("id", ""),
    "d": ASSERTION_REFERENCE_D.get("id", ""),
    "proposition_a": PROPOSITION_REFERENCE_A.get("id", ""),
    "proposition_b": PROPOSITION_REFERENCE_B.get("id", ""),
    "proposition_c": PROPOSITION_REFERENCE_C.get("id", ""),
}
