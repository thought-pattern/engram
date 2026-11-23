"""Feedback learning policy, observation and state contracts, and negative-resolution cache bounds."""

from enum import StrEnum

FEEDBACK_POLICY_FIELDS = set(
    {
        "minimum_verdict_samples",
        "prior_accept",
        "prior_reject",
        "half_life_seconds",
        "bucket_seconds",
        "max_buckets_per_record",
        "max_statement_records",
        "max_relationship_records",
    }
)
FEEDBACK_MINIMUM_VERDICT_SAMPLES = 5
FEEDBACK_PRIOR_ACCEPT = 1.0
FEEDBACK_PRIOR_REJECT = 3.0
FEEDBACK_HALF_LIFE_SECONDS = 2_592_000
FEEDBACK_BUCKET_SECONDS = 86_400
FEEDBACK_MAX_BUCKETS_PER_RECORD = 32
FEEDBACK_MAX_STATEMENT_RECORDS = 10_000
FEEDBACK_MAX_RELATIONSHIP_RECORDS = 50_000
MAX_FEEDBACK_POLICY_SAMPLES = 1_000_000
MAX_FEEDBACK_PRIOR_MASS = 1_000_000.0
MAX_FEEDBACK_HALF_LIFE_SECONDS = 315_576_000
MAX_FEEDBACK_BUCKET_SECONDS = 31_557_600
MAX_FEEDBACK_POLICY_RECORDS = 1_000_000
STATEMENT_FEEDBACK_KEY_FIELDS = set({"statement_id", "generation", "generation_available", "policy_fingerprint"})
RELATIONSHIP_FEEDBACK_KEY_FIELDS = set({"query_identity", "scope", "constraint_fingerprint", "statement"})
FEEDBACK_OBSERVATION_FIELDS = set(
    {
        "reference_kind",
        "reference_id",
        "kind",
        "outcome",
        "query_identity",
        "scope",
        "constraint_fingerprint",
        "statement_id",
        "generation",
        "generation_available",
        "policy_fingerprint",
        "observed_at",
        "reason",
    }
)
FEEDBACK_STATISTICS_FIELDS = set(
    {"candidate_count", "accept_count", "rejected_quality", "rejected_context", "rejected_stale", "rejected_policy"}
)
FEEDBACK_BUCKET_FIELDS = set({"start_at", "statistics"})
FEEDBACK_RECORD_FIELDS = set({"key", "raw", "buckets", "last_outcome", "last_observed_at"})
POLICY_SUPPRESSION_FIELDS = set({"statement_id", "namespace", "policy_fingerprint", "observed_at"})
STALE_EXCLUSION_FIELDS = set({"statement_id", "generation", "generation_available", "observed_at"})
FEEDBACK_STATE_FIELDS = set(
    {
        "policy",
        "statement_records",
        "relationship_records",
        "policy_suppressions",
        "stale_exclusions",
        "receipts",
        "statement_evictions",
        "relationship_evictions",
        "policy_suppression_evictions",
        "stale_exclusion_evictions",
    }
)
NEGATIVE_RESOLUTION_KEY_FIELDS = set(
    {
        "query_identity",
        "scope",
        "constraint_fingerprint",
        "resolver_plan_fingerprint",
        "capability_readiness_fingerprint",
        "policy_fingerprint",
    }
)
NEGATIVE_RESOLUTION_FIELDS = set({"key", "reason", "created_at", "expires_at", "hit_count"})
EMPTY_FINGERPRINT = "0" * 64
EMPTY_NEGATIVE_CREATED_AT = "1970-01-01T00:00:00Z"
EMPTY_NEGATIVE_EXPIRES_AT = "1970-01-01T00:00:01Z"
DEFAULT_NEGATIVE_MAX_RECORDS = 1_000
DEFAULT_NEGATIVE_TTL_SECONDS = 300
MAX_REFERENCE_ID_BYTES = 256
MAX_FEEDBACK_REASON_BYTES = 512
MAX_STATEMENT_ID_BYTES = 256
MAX_FINGERPRINT_BYTES = 64
MAX_FEEDBACK_OBSERVATIONS = 1_000
MAX_FEEDBACK_SIGNATURE_BYTES = 67_108_864
MAX_FEEDBACK_BUCKETS = 64
MAX_INSPECTION_RECORDS = 64
MAX_NEGATIVE_RECORDS = 10_000
MAX_NEGATIVE_TTL_SECONDS = 86_400
MAX_CONSTRAINT_ENCODED_BYTES = 65_536


class NegativeResolutionReason(StrEnum):
    """Only reusable first-generation negative-resolution reason."""

    INSUFFICIENT_KNOWLEDGE = "insufficient_knowledge"


class FeedbackReferenceKind(StrEnum):
    """Stable owner of the request reference carried by feedback."""

    RESOLUTION_REQUEST = "resolution_request"
    REGULATED_PROPOSAL = "regulated_proposal"


class FeedbackObservationKind(StrEnum):
    """Whether an observation records candidacy or an external verdict."""

    CANDIDACY = "candidacy"
    VERDICT = "verdict"


class FeedbackOutcome(StrEnum):
    """Closed first-generation feedback outcome vocabulary."""

    CANDIDATE = "candidate"
    ACCEPTED = "accepted"
    REJECTED_QUALITY = "rejected_quality"
    REJECTED_CONTEXT = "rejected_context"
    REJECTED_STALE = "rejected_stale"
    REJECTED_POLICY = "rejected_policy"


FEEDBACK_OUTCOME_COUNTER_FIELDS = {
    FeedbackOutcome.CANDIDATE: "candidate_count",
    FeedbackOutcome.ACCEPTED: "accept_count",
    FeedbackOutcome.REJECTED_QUALITY: "rejected_quality",
    FeedbackOutcome.REJECTED_CONTEXT: "rejected_context",
    FeedbackOutcome.REJECTED_STALE: "rejected_stale",
    FeedbackOutcome.REJECTED_POLICY: "rejected_policy",
}


class LifecycleHandoffStatus(StrEnum):
    """Inspectable result of a stale-feedback lifecycle handoff."""

    NOT_APPLICABLE = "not_applicable"
    PENDING = "pending"
    COMPLETED = "completed"
    REPLAYED = "replayed"
    CONFLICTED = "conflicted"
    FAILED = "failed"
