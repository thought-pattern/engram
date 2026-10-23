"""Candidate fusion policy, features, reranker model, and fusion decision reasons."""

from enum import StrEnum

from engram.constants_resolution import CandidateSource

FUSION_POLICY_FIELDS = set(
    {
        "weights",
        "answer_threshold",
        "evidence_threshold",
        "ambiguity_margin",
        "minimum_independent_sources",
        "require_support_for_non_exact",
        "max_report_candidates",
    }
)
CANDIDATE_ELIGIBILITY_FIELDS = set(
    {"score_eligible", "evidence_eligible", "answer_eligible", "reason_codes", "feature_values", "feature_available"}
)
FUSION_ANSWER_THRESHOLD = 0.78
FUSION_EVIDENCE_THRESHOLD = 0.35
FUSION_AMBIGUITY_MARGIN = 0.12
FUSION_MINIMUM_INDEPENDENT_SOURCES = 2
MIN_FUSION_INDEPENDENT_SOURCES = 2
MAX_FUSION_INDEPENDENT_SOURCES = 6
FUSION_REQUIRE_SUPPORT_FOR_NON_EXACT = True
MAX_FUSION_CONTRIBUTIONS = 1_000
MAX_FUSION_REPORT_CANDIDATES = 8
MAX_FUSION_REPORT_CONTRIBUTIONS = 8
MAX_FUSION_REPORT_BYTES = 16_384


class FusionFeature(StrEnum):
    """Closed, comparable feature vocabulary for the fusion policy."""

    EXACT = "exact"
    LEXICAL = "lexical"
    SEMANTIC = "semantic"
    ENTITY = "entity"
    RELATION = "relation"
    OBJECT_TYPE = "object_type"
    SUPPORT = "support"
    HISTORY = "history"
    FRESHNESS = "freshness"
    AUTHORITY = "authority"
    AGREEMENT = "agreement"
    MARGIN = "margin"


# Transparent logistic reranker model: the fused base score plus the fusion features it
# weighs, in scoring order, with one fixed coefficient each and the intercept.
RERANKER_BASE_SCORE_FEATURE = "base_score"
RERANKER_FEATURES = (
    RERANKER_BASE_SCORE_FEATURE,
    FusionFeature.EXACT,
    FusionFeature.LEXICAL,
    FusionFeature.SEMANTIC,
    FusionFeature.ENTITY,
    FusionFeature.RELATION,
    FusionFeature.OBJECT_TYPE,
    FusionFeature.SUPPORT,
    FusionFeature.FRESHNESS,
    FusionFeature.AUTHORITY,
    FusionFeature.AGREEMENT,
)
RERANKER_COEFFICIENTS: dict[str, float] = {
    RERANKER_BASE_SCORE_FEATURE: 2.40,
    FusionFeature.EXACT: 4.00,
    FusionFeature.LEXICAL: 0.65,
    FusionFeature.SEMANTIC: 0.85,
    FusionFeature.ENTITY: 0.55,
    FusionFeature.RELATION: 0.55,
    FusionFeature.OBJECT_TYPE: 0.40,
    FusionFeature.SUPPORT: 0.80,
    FusionFeature.FRESHNESS: 0.20,
    FusionFeature.AUTHORITY: 0.50,
    FusionFeature.AGREEMENT: 0.65,
}
RERANKER_INTERCEPT = -1.20


class FusionPolicyReason(StrEnum):
    """Stable non-content-bearing fusion outcome and eligibility reasons."""

    ANSWER_EXACT_ELIGIBLE = "answer_exact_eligible"
    ANSWER_FUSION_THRESHOLD = "answer_fusion_threshold"
    ANSWER_MARGIN_CLEAR = "answer_margin_clear"
    ANSWER_NO_RUNNER_UP = "answer_no_runner_up"
    AMBIGUOUS_TOP_CANDIDATES = "ambiguous_top_candidates"
    ANSWER_THRESHOLD_NOT_MET = "answer_threshold_not_met"
    ANSWER_ELIGIBILITY_PREVENTED = "answer_eligibility_prevented"
    EVIDENCE_THRESHOLD_MET = "evidence_threshold_met"
    GRAPH_EVIDENCE_AVAILABLE = "graph_evidence_available"
    EVIDENCE_THRESHOLD_NOT_MET = "evidence_threshold_not_met"
    NO_ELIGIBLE_CANDIDATES = "no_eligible_candidates"
    CANDIDATE_SCOPE_MISMATCH = "candidate_scope_mismatch"
    CANDIDATE_LIFECYCLE_INELIGIBLE = "candidate_lifecycle_ineligible"
    CANDIDATE_STATEMENT_CONFLICT = "candidate_statement_conflict"
    CANDIDATE_ID_CONFLICT = "candidate_id_conflict"
    CANDIDATE_EVIDENCE_SCOPE_MISMATCH = "candidate_evidence_scope_mismatch"
    EVIDENCE_REFERENCE_CONFLICT = "evidence_reference_conflict"
    AUTHORITATIVE_STATEMENT_MISSING = "authoritative_statement_missing"
    AUTHORITATIVE_RESPONSE_MISMATCH = "authoritative_response_mismatch"
    AUTHORITATIVE_GENERATION_MISMATCH = "authoritative_generation_mismatch"
    ARTIFACT_INELIGIBLE = "artifact_ineligible"
    REQUIRED_METADATA_MISMATCH = "required_metadata_mismatch"
    REQUIRED_SOURCE_MISMATCH = "required_source_mismatch"
    OWNERSHIP_VISIBILITY_MISMATCH = "ownership_visibility_mismatch"
    SUPPORT_INCOMPLETE = "support_incomplete"
    SUPPORT_REFERENCE_STALE = "support_reference_stale"
    INDEPENDENT_SOURCES_MISSING = "independent_sources_missing"
    IDENTITY_FEATURE_MISMATCH = "identity_feature_mismatch"
    OBJECT_TYPE_FEATURE_MISMATCH = "object_type_feature_mismatch"
    EXPLICIT_CONFLICT = "explicit_conflict"
    FUSION_MEMORY_EXHAUSTED = "fusion_memory_exhausted"
    FEEDBACK_STALE_EXCLUDED = "feedback_stale_excluded"
    FEEDBACK_POLICY_SUPPRESSED = "feedback_policy_suppressed"


DEFAULT_FUSION_WEIGHTS = {
    FusionFeature.EXACT: 1.0,
    FusionFeature.LEXICAL: 1.0,
    FusionFeature.SEMANTIC: 1.0,
    FusionFeature.ENTITY: 0.4,
    FusionFeature.RELATION: 0.5,
    FusionFeature.OBJECT_TYPE: 0.3,
    FusionFeature.SUPPORT: 0.8,
    FusionFeature.HISTORY: 0.2,
    FusionFeature.FRESHNESS: 0.2,
    FusionFeature.AUTHORITY: 0.5,
    FusionFeature.AGREEMENT: 0.9,
    FusionFeature.MARGIN: 0.0,
}
FUSION_SOURCE_ORDER = {
    CandidateSource.EXACT: 0,
    CandidateSource.SUPPORT_SEMANTIC: 1,
    CandidateSource.SPARSE: 2,
    CandidateSource.STANDALONE_SEMANTIC: 3,
    CandidateSource.UTILITY: 4,
}
FUSION_SOURCE_FAMILY = {
    CandidateSource.EXACT: "exact",
    CandidateSource.SUPPORT_SEMANTIC: "support_semantic",
    CandidateSource.SPARSE: "lexical",
    CandidateSource.STANDALONE_SEMANTIC: "standalone_semantic",
    CandidateSource.UTILITY: "utility",
}
FUSION_CONSERVATIVE_MINIMUM = set(
    {
        FusionFeature.ENTITY,
        FusionFeature.RELATION,
        FusionFeature.OBJECT_TYPE,
        FusionFeature.SUPPORT,
        FusionFeature.FRESHNESS,
        FusionFeature.AUTHORITY,
    }
)
