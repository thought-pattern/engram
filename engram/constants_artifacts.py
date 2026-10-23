"""Response artifact contract and metadata bounds, statement tiers, and artifact lifecycle vocabulary."""

from enum import Enum, StrEnum

from engram.constants import EMPTY_MAPPING

MAX_ARTIFACT_ID_BYTES = 256
MAX_RESPONSE_BYTES = 1_048_576
MAX_SOURCE_LABEL_BYTES = 256
MAX_CALLER_ID_BYTES = 256
MAX_SUPPORT_REFERENCES = 256
# Support revisions cross gRPC as protobuf Struct numbers (IEEE-754 doubles),
# which carry every integer exactly only through 2**53 - 1.
MAX_SUPPORT_REVISION = 2**53 - 1
MAX_TIMESTAMP_BYTES = 40
MAX_METADATA_BYTES = 65_536
MAX_METADATA_DEPTH = 8
MAX_METADATA_ITEMS = 1_024
MAX_METADATA_KEY_BYTES = 256
MAX_METADATA_STRING_BYTES = 16_384
# Terminal transitions write this reserved metadata entry themselves. It has its own
# field bounds, outside the caller metadata limits, so an admitted artifact always has
# room for the audit its retirement, invalidation, or supersession must record.
LIFECYCLE_AUDIT_KEY = "lifecycle_audit"
LIFECYCLE_AUDIT_FIELDS = set({"operation", "reason", "caller_id", "request_id", "occurred_at", "detail"})
MAX_LIFECYCLE_AUDIT_DETAIL_BYTES = 1_024
MAX_ARTIFACT_ENUM_BYTES = 16
ARTIFACT_PROVENANCE_FIELDS = set({"source_label", "caller_id", "accepted_at"})
ARTIFACT_STATISTICS_FIELDS = set({"hit_count", "query_count", "last_hit", "last_hit_available"})
# Statistics of an artifact that has not been served. Read-only: artifact
# validation copies statistics, so no artifact aliases this mapping.
INITIAL_ARTIFACT_STATISTICS = {"hit_count": 0, "query_count": 0, "last_hit": "", "last_hit_available": False}
CACHED_RESPONSE_ARTIFACT_FIELDS = set(
    {
        "statement_id",
        "generation",
        "response",
        "query_identity",
        "retrieval",
        "tier",
        "lifecycle",
        "scope",
        "support_references",
        "valid_from",
        "valid_from_available",
        "valid_until",
        "valid_until_available",
        "superseded_by",
        "provenance",
        "statistics",
        "metadata",
    }
)
LIFECYCLE_BASE_DECISION_FIELDS = set({"lifecycle", "direct_answer_eligible", "reason"})
LIFECYCLE_TRANSITION_DECISION_FIELDS = set({"current", "target", "operation", "allowed", "reason"})


class LifecycleMutationReason(StrEnum):
    """Closed caller-supplied reasons for terminal lifecycle mutations."""

    SOURCE_RETRACTED = "SOURCE_RETRACTED"
    POLICY = "POLICY"
    STALE = "STALE"
    USER_REQUEST = "USER_REQUEST"
    ADMINISTRATIVE = "ADMINISTRATIVE"


class Tier(Enum):
    """Statement tier classification."""

    STATIC = "STATIC"
    DYNAMIC = "DYNAMIC"


class LifecycleState(StrEnum):
    """Persisted lifecycle states for accepted-response artifacts."""

    ACTIVE = "ACTIVE"
    SUPERSEDED = "SUPERSEDED"
    INVALIDATED = "INVALIDATED"
    RETIRED = "RETIRED"


class LifecycleOperation(StrEnum):
    """Operations that may request a lifecycle transition."""

    SUPERSEDE = "SUPERSEDE"
    INVALIDATE = "INVALIDATE"
    RETIRE = "RETIRE"


class LifecycleDecisionReason(StrEnum):
    """Stable policy reasons for lifecycle eligibility and transitions."""

    ELIGIBLE = "eligible"
    SUPERSEDED = "lifecycle_superseded"
    INVALIDATED = "lifecycle_invalidated"
    RETIRED = "lifecycle_retired"
    LEGAL_TRANSITION = "legal_transition"
    SAME_STATE_NOT_A_TRANSITION = "same_state_not_a_transition"
    TERMINAL_STATE = "terminal_state"
    OPERATION_TARGET_MISMATCH = "operation_target_mismatch"


LEGAL_LIFECYCLE_TRANSITIONS = {
    LifecycleState.ACTIVE: {
        LifecycleOperation.SUPERSEDE: LifecycleState.SUPERSEDED,
        LifecycleOperation.INVALIDATE: LifecycleState.INVALIDATED,
        LifecycleOperation.RETIRE: LifecycleState.RETIRED,
    },
    LifecycleState.SUPERSEDED: EMPTY_MAPPING,
    LifecycleState.INVALIDATED: EMPTY_MAPPING,
    LifecycleState.RETIRED: EMPTY_MAPPING,
}
TERMINAL_LIFECYCLE_STATES = set({LifecycleState.SUPERSEDED, LifecycleState.INVALIDATED, LifecycleState.RETIRED})
LIFECYCLE_INELIGIBLE_REASONS = {
    LifecycleState.SUPERSEDED: LifecycleDecisionReason.SUPERSEDED,
    LifecycleState.INVALIDATED: LifecycleDecisionReason.INVALIDATED,
    LifecycleState.RETIRED: LifecycleDecisionReason.RETIRED,
}


class RepositoryRemovalReason(StrEnum):
    """Physical repository removal reasons, separate from lifecycle."""

    EXPLICIT_DELETE = "explicit_delete"
    CAPACITY_EVICTION = "capacity_eviction"


class AdmissionOutcome(StrEnum):
    """Complete bounded accepted-response admission outcomes."""

    ADMITTED = "ADMITTED"
    ADMITTED_WITH_EVICTION = "ADMITTED_WITH_EVICTION"
    REJECTED_CAPACITY = "REJECTED_CAPACITY"
