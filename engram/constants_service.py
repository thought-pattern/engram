"""Service request bounds, core state, gRPC regulator outcome names and health page figures."""

from enum import StrEnum

from engram.constants_identity import MAX_CANONICAL_FORM_BYTES, MAX_RETRIEVAL_REPRESENTATION_BYTES

MAX_REQUEST_BYTES = 16_384
# A regulated-cache request (resolve, propose, learn) becomes an exact-match
# lookup key, so the request and its normalized form must both fit the key's
# limits. Chat builds no key and accepts MAX_REQUEST_BYTES.
MAX_CACHE_REQUEST_BYTES = min(MAX_CANONICAL_FORM_BYTES, MAX_RETRIEVAL_REPRESENTATION_BYTES)


class CoreState(StrEnum):
    """Lifecycle state of the single owned application core."""

    RUNNING = "running"
    CLOSING = "closing"
    CLOSED = "closed"


GRPC_REGULATOR_OUTCOME_NAMES = {
    "REGULATOR_OUTCOME_ACCEPTED": "accepted",
    "REGULATOR_OUTCOME_REJECTED_QUALITY": "rejected_quality",
    "REGULATOR_OUTCOME_REJECTED_CONTEXT": "rejected_context",
    "REGULATOR_OUTCOME_REJECTED_STALE": "rejected_stale",
    "REGULATOR_OUTCOME_REJECTED_POLICY": "rejected_policy",
}
# The health page keeps the latest HEALTH_RECENT_OUTCOMES finished calls and one recent-activity sample every
# HEALTH_SAMPLE_SECONDS; HEALTH_HISTORY_SAMPLES five-second samples keep ten minutes of recent activity.
HEALTH_RECENT_OUTCOMES = 32
HEALTH_SAMPLE_SECONDS = 5.0
HEALTH_HISTORY_SAMPLES = 120
