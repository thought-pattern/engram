"""Exact corrected support-reference and accepted-artifact fixtures for Engram tests."""

from engram.constants import INITIAL_ARTIFACT_STATISTICS, LifecycleState, Tier
from engram.identity import extract_standalone_identity, retrieval_representation, scope_key

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
    "representation_contract": "representation-v1",
    "visibility_scope": GLOBAL_VISIBILITY,
    "dependency_state_digest": "dep_" + "a" * 64,
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

TENANT_SCOPE = scope_key(namespace="tenant-a", context_fingerprint="account:pro")

# Fields of the shared current accepted-response artifact for "Who acquired GitHub?" in TENANT_SCOPE. Tests pass a
# copy with their changed fields to validate_cached_response_artifact, recomputing query_identity and retrieval when
# they change the request or scope.
ACCEPTED_ARTIFACT_FIELDS = {
    "statement_id": "stmt-response-1",
    "generation": 1,
    "response": "Microsoft acquired GitHub in 2018.",
    "query_identity": extract_standalone_identity("Who acquired GitHub?", TENANT_SCOPE),
    "retrieval": retrieval_representation("Who acquired GitHub?", ("GitHub acquirer",)),
    "tier": Tier.STATIC,
    "lifecycle": LifecycleState.ACTIVE,
    "scope": TENANT_SCOPE,
    "support_references": (ASSERTION_REFERENCE_A,),
    "valid_from": "",
    "valid_from_available": False,
    "valid_until": "",
    "valid_until_available": False,
    "superseded_by": "",
    "provenance": {"source_label": "released", "caller_id": "regulator-a", "accepted_at": "2026-08-12T16:00:00Z"},
    "statistics": INITIAL_ARTIFACT_STATISTICS,
    "metadata": {},
}
