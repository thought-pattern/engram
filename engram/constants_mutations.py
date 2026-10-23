"""Repository state, coordinated mutation, receipt and tombstone contracts and retention bounds."""

from enum import StrEnum

COORDINATED_RESPONSE_STATE_FIELDS = set({"repository", "mutation_receipts"})
COORDINATED_MUTATION_CANDIDATE_FIELDS = set({"before", "after", "receipt"})
TIER_ADMISSION_POLICY_FIELDS = set({"dynamic_capacity"})
REPOSITORY_STATE_FIELDS = set({"state_generation", "artifacts"})
MAX_REPOSITORY_ARTIFACTS = 100_000
# Candidates the repository and the mutation coordinator remember between planning and
# publication; each mutation plans and publishes one, so this only bounds abandoned plans.
MAX_PLANNED_CANDIDATES = 16
MAX_REQUEST_ID_BYTES = 256
MAX_RECEIPT_STATEMENT_ID_BYTES = 256
MAX_RESULT_BYTES = 65_536
MAX_SIGNATURE_INPUT_BYTES = 1_048_576
MAX_RESULT_DEPTH = 8
MAX_RESULT_ITEMS = 1_024
MAX_RESULT_KEY_BYTES = 256
MAX_RESULT_STRING_BYTES = 16_384
MAX_AFFECTED_GENERATIONS = 1_024
MAX_RECEIPTS = 100_000
MAX_TOMBSTONES = 100_000
# Retention of a ledger built without explicit bounds; each stays within its MAX_ ceiling.
DEFAULT_RECEIPT_RETENTION = 10_000
DEFAULT_TOMBSTONE_RETENTION = 10_000
ARTIFACT_GENERATION_CHANGE_FIELDS = set({"statement_id", "before_generation", "after_generation"})
RECEIPT_TOMBSTONE_FIELDS = set({"sequence", "request_id", "operation", "payload_signature", "scope_bindings"})
MUTATION_RECEIPT_FIELDS = set(
    {
        "sequence",
        "request_id",
        "operation",
        "payload_signature",
        "result_code",
        "affected_generations",
        "result",
        "completion_state",
        "created_at",
    }
)
RECEIPT_LOOKUP_FIELDS = set({"outcome", "request_id", "receipt", "receipt_available"})


class MutationOperation(StrEnum):
    """Transport-neutral accepted-response mutations."""

    COMMIT_RESPONSE = "COMMIT_RESPONSE"
    INVALIDATE_RESPONSE = "INVALIDATE_RESPONSE"
    RETIRE_RESPONSE = "RETIRE_RESPONSE"
    SUPERSEDE_RESPONSE = "SUPERSEDE_RESPONSE"
    RECORD_RESPONSE_QUERY = "RECORD_RESPONSE_QUERY"
    RECORD_RESPONSE_HIT = "RECORD_RESPONSE_HIT"
    FINALIZE_RESOLUTION_ACCOUNTING = "FINALIZE_RESOLUTION_ACCOUNTING"
    RECORD_FEEDBACK = "RECORD_FEEDBACK"


class MutationResultCode(StrEnum):
    """Stable results that can be replayed from a receipt."""

    CREATED = "CREATED"
    CREATED_WITH_EVICTION = "CREATED_WITH_EVICTION"
    INVALIDATED = "INVALIDATED"
    RETIRED = "RETIRED"
    SUPERSEDED = "SUPERSEDED"
    REJECTED_CAPACITY = "REJECTED_CAPACITY"
    QUERY_RECORDED = "QUERY_RECORDED"
    HIT_RECORDED = "HIT_RECORDED"
    RESOLUTION_ACCOUNTING_RECORDED = "RESOLUTION_ACCOUNTING_RECORDED"
    FEEDBACK_RECORDED = "FEEDBACK_RECORDED"


class ReceiptCompletionState(StrEnum):
    """Durable progress state of one mutation identity."""

    PREPARED = "PREPARED"
    COMPLETED = "COMPLETED"


class ReceiptLookupOutcome(StrEnum):
    """Complete outcomes before applying a mutation request."""

    NEW = "NEW"
    REPLAY = "REPLAY"
    IN_PROGRESS = "IN_PROGRESS"
    EXPIRED = "EXPIRED"
    CONFLICT = "CONFLICT"
