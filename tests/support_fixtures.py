"""Exact corrected support-reference fixtures for Engram tests."""

from engram.artifacts import (
    LifecycleState,
    artifact_provenance,
    artifact_statistics,
    cached_response_artifact,
)
from engram.constants import Tier
from engram.identity import build_retrieval_representation, build_standalone_identity, scope_key

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


def accepted_artifact(**overrides) -> dict:
    """Build one current accepted-response artifact for cross-module tests."""
    selected_scope = overrides.pop(
        "scope",
        scope_key(namespace="tenant-a", context_fingerprint="account:pro"),
    )
    request = overrides.pop("request", "Who acquired GitHub?")
    values = {
        "statement_id": "stmt-response-1",
        "generation": 1,
        "response": "Microsoft acquired GitHub in 2018.",
        "query_identity": build_standalone_identity(request, selected_scope),
        "retrieval": build_retrieval_representation(request, ("GitHub acquirer",)),
        "tier": Tier.STATIC,
        "lifecycle": LifecycleState.ACTIVE,
        "scope": selected_scope,
        "support_references": (ASSERTION_REFERENCE_A,),
        "valid_from": "",
        "valid_from_available": False,
        "valid_until": "",
        "valid_until_available": False,
        "superseded_by": "",
        "provenance": artifact_provenance(
            source_label="tapestry:released",
            caller_id="regulator-a",
            accepted_at="2026-08-12T16:00:00Z",
        ),
        "statistics": artifact_statistics(),
        "metadata": {},
    }
    values.update(overrides)
    result = cached_response_artifact(**values)
    return result
