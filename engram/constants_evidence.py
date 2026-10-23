"""Proposition evidence records, disclosure and eligibility, usefulness policy, and evidence package contracts."""

from enum import StrEnum

from engram.constants_artifacts import LifecycleDecisionReason

CANONICAL_PROPOSITION_REFERENCES_FIELDS = set({"subject_entity_id", "predicate_id", "object_entity_id"})
PROPOSITION_VALIDITY_INPUTS_FIELDS = set(
    {
        "evaluation_time",
        "active",
        "system_current",
        "valid_time_current",
        "eligible_for_request",
        "system_time_match",
        "valid_time_match",
        "valid_time_match_available",
        "temporal_operator",
        "temporal_axis",
        "requested_start",
        "requested_start_available",
        "requested_end",
        "requested_end_available",
        "system_from",
        "system_from_available",
        "system_to",
        "system_to_available",
        "invalidated_at",
        "invalidated_at_available",
        "valid_from",
        "valid_from_available",
        "valid_to",
        "valid_to_available",
    }
)
PROPOSITION_TRUST_INPUTS_FIELDS = set({"trust_category", "trust_category_available", "supplied_trust", "supplied_trust_available"})
DISCLOSURE_DECISION_FIELDS = set({"ownership", "basis", "scope", "authority", "authority_available"})
PROPOSITION_EVIDENCE_PATH_STEP_FIELDS = set(
    {
        "position",
        "proposition_id",
        "subject_entity_id",
        "predicate_id",
        "object_entity_id",
        "operator",
        "input_binding",
        "output_binding",
        "filters",
        "aggregation_inputs",
    }
)
# Closed filter vocabulary of a Proposition evidence path step, in the sorted order a step carries it.
PROPOSITION_PATH_FILTERS = ("canonical_identity", "object_type", "publication_revalidation", "temporal_eligibility", "visibility")
PROPOSITION_EVIDENCE_RECORD_FIELDS = set(
    {
        "proposition_id",
        "source_resolver",
        "source_contributions",
        "features",
        "canonical_references",
        "validity",
        "trust",
        "disclosure",
        "path",
        "selection_reasons",
    }
)
EVIDENCE_PACKAGE_FIELDS = set({"records", "retained_count", "omitted_count", "truncated", "truncation_reasons"})
EVIDENCE_USEFULNESS_POLICY_FIELDS = set(
    {
        "canonical_completeness_floor",
        "structured_match_floor",
        "semantic_similarity_floor",
        "source_agreement_floor",
        "supplied_trust_floor",
        "supplied_trust_floor_available",
    }
)
VISIBILITY_AUTHORIZATION_FIELDS = set({"allowed", "scope", "ownership", "authority_id", "reason_code"})
VISIBILITY_GRANT_FIELDS = set({"scope", "ownership"})
PROPOSITION_ELIGIBILITY_DECISION_FIELDS = set(
    {"projection", "eligible", "reason", "disclosure", "disclosure_available", "revalidated"}
)
MAX_PROPOSITION_IDENTIFIER_BYTES = 256
MAX_PROPOSITION_SOURCE_CONTRIBUTIONS = 8
MAX_PROPOSITION_SELECTION_REASONS = 16
MAX_PROPOSITION_TRUST_CATEGORY_BYTES = 96
MAX_DISCLOSURE_AUTHORITY_BYTES = 256
MAX_DISCLOSURE_ENUM_BYTES = 32
MAX_EVIDENCE_PACKAGE_RECORDS = 10
MAX_EVIDENCE_PACKAGE_INPUT_RECORDS = 1_000
MAX_EVIDENCE_PACKAGE_BYTES = 65_536
MAX_EVIDENCE_PACKAGE_TRUNCATION_REASONS = 8
MIN_EVIDENCE_PACKAGE_BYTES = 256
# Retained and omitted record counts stay within the signed 32-bit range.
MAX_EVIDENCE_PACKAGE_RECORD_COUNT = 2_147_483_647
CANONICAL_COMPLETENESS_FLOOR = 1.0
STRUCTURED_MATCH_FLOOR = 1.0
SEMANTIC_SIMILARITY_FLOOR = 0.60
SOURCE_AGREEMENT_FLOOR = 1.0
MAX_VISIBILITY_GRANTS = 4_096


class PropositionEligibilityReason(StrEnum):
    """Closed temporal Proposition evidence eligibility outcomes."""

    ELIGIBLE_PUBLIC = "eligible_public"
    ELIGIBLE_TRUSTED_SCOPE = "eligible_trusted_scope"
    EVALUATION_TIME_UNAVAILABLE = "evaluation_time_unavailable"
    TEMPORAL_QUERY_UNRESOLVED = "temporal_query_unresolved"
    PROPOSITION_INACTIVE = "proposition_inactive"
    SYSTEM_TIME_UNAVAILABLE = "system_time_unavailable"
    SYSTEM_NOT_YET_CURRENT = "system_not_yet_current"
    SYSTEM_NO_LONGER_CURRENT = "system_no_longer_current"
    VALID_TIME_NOT_YET_CURRENT = "valid_time_not_yet_current"
    VALID_TIME_NO_LONGER_CURRENT = "valid_time_no_longer_current"
    RETRIEVAL_ONLY = "retrieval_only"
    SEMANTIC_MEANING_UNREPRESENTED = "semantic_meaning_unrepresented"
    VISIBILITY_AUTHORITY_UNAVAILABLE = "visibility_authority_unavailable"
    VISIBILITY_AUTHORITY_FAILED = "visibility_authority_failed"
    VISIBILITY_SCOPE_MISMATCH = "visibility_scope_mismatch"
    VISIBILITY_OWNERSHIP_MISMATCH = "visibility_ownership_mismatch"
    VISIBILITY_DENIED = "visibility_denied"
    REVALIDATION_UNAVAILABLE = "revalidation_unavailable"
    REVALIDATION_MISSING = "revalidation_missing"
    REVALIDATION_IDENTITY_CONFLICT = "revalidation_identity_conflict"


class EvidenceUsefulnessReason(StrEnum):
    """Stable inspectable reasons from the unfitted inclusion policy."""

    CANONICAL_COMPLETENESS_UNAVAILABLE = "canonical_completeness_unavailable"
    CANONICAL_COMPLETENESS_BELOW_FLOOR = "canonical_completeness_below_floor"
    STRUCTURED_MATCH_QUALIFIED = "structured_match_qualified"
    SEMANTIC_SIMILARITY_QUALIFIED = "semantic_similarity_qualified"
    RETRIEVAL_SIGNAL_UNAVAILABLE = "retrieval_signal_unavailable"
    RETRIEVAL_SIGNAL_BELOW_FLOOR = "retrieval_signal_below_floor"
    SOURCE_AGREEMENT_QUALIFIED = "source_agreement_qualified"
    SUPPLIED_TRUST_AVAILABLE = "supplied_trust_available"
    SUPPLIED_TRUST_UNAVAILABLE = "supplied_trust_unavailable"
    SUPPLIED_TRUST_FLOOR_SATISFIED = "supplied_trust_floor_satisfied"
    SUPPLIED_TRUST_REQUIRED_UNAVAILABLE = "supplied_trust_required_unavailable"
    SUPPLIED_TRUST_BELOW_FLOOR = "supplied_trust_below_floor"


class PropositionOwnership(StrEnum):
    """Allow-listed canonical Proposition ownership categories."""

    PUBLIC = "PUBLIC"
    COMPANY = "COMPANY"
    CUSTOMER = "CUSTOMER"


class DisclosureBasis(StrEnum):
    """Stable provenance for a successful Proposition visibility decision."""

    PUBLIC_RULE = "public_rule"
    TRUSTED_SCOPE_AUTHORITY = "trusted_scope_authority"


class EvidencePackageTruncationReason(StrEnum):
    """Stable reasons that a package retained fewer records than supplied."""

    DUPLICATE_PROPOSITION_ID = "duplicate_proposition_id"
    RECORD_LIMIT = "record_limit"
    SERIALIZED_SIZE_LIMIT = "serialized_size_limit"


class EligibilityExclusionReason(StrEnum):
    """Stable complete outcomes for direct accepted-response eligibility."""

    ELIGIBLE = "eligible"
    ARTIFACT_REPOSITORY_UNAVAILABLE = "artifact_repository_unavailable"
    EVALUATION_TIME_UNAVAILABLE = "evaluation_time_unavailable"
    SCOPE_NAMESPACE_MISMATCH = "scope_namespace_mismatch"
    LIFECYCLE_SUPERSEDED = "lifecycle_superseded"
    LIFECYCLE_INVALIDATED = "lifecycle_invalidated"
    LIFECYCLE_RETIRED = "lifecycle_retired"
    VALIDITY_INTERVAL_INVALID = "validity_interval_invalid"
    NOT_YET_VALID = "not_yet_valid"
    EXPIRED = "expired"


LIFECYCLE_EXCLUSION_REASONS = {
    LifecycleDecisionReason.SUPERSEDED: EligibilityExclusionReason.LIFECYCLE_SUPERSEDED,
    LifecycleDecisionReason.INVALIDATED: EligibilityExclusionReason.LIFECYCLE_INVALIDATED,
    LifecycleDecisionReason.RETIRED: EligibilityExclusionReason.LIFECYCLE_RETIRED,
}
