"""Service request bounds, core state, gRPC regulator outcome names, MCP conformance vocabulary and health page figures."""

from enum import StrEnum

from engram.constants_identity import MAX_CANONICAL_FORM_BYTES, MAX_RETRIEVAL_REPRESENTATION_BYTES

MAX_REQUEST_BYTES = 16_384
# A regulated-cache request (resolve, propose, learn) becomes an exact-match
# lookup key, so the request and its normalized form must both fit the key's
# limits. Chat builds no key and accepts MAX_REQUEST_BYTES.
MAX_CACHE_REQUEST_BYTES = min(MAX_CANONICAL_FORM_BYTES, MAX_RETRIEVAL_REPRESENTATION_BYTES)
MCP_CONFORMANCE_MINIMUM_TURNS = 1_000
MCP_CONFORMANCE_MESSAGES = (
    "hello",
    "How are you?",
    "What can you remember?",
    "Tell me more.",
    "Why?",
    "Where are we?",
    "When is now?",
    "Who are you?",
    "What did I say?",
    "Continue.",
)
MCP_TURN_EVENT_FIELDS = set(
    {
        "turn",
        "input",
        "response",
        "user_id",
        "source",
        "score",
        "pattern",
        "captured",
        "dialogue_act",
        "active_topic",
        "entities",
        "fact_admissions",
        "elapsed_seconds",
        "context_changes",
        "learned_statements",
    }
)
MCP_TURN_EVALUATION_CHECKS = set(
    {
        "exact_fields",
        "turn_sequence",
        "input_continuity",
        "user_continuity",
        "response_nonempty",
        "source_nonempty",
        "score_finite",
        "pattern_string",
        "captured_list",
        "dialogue_act_string",
        "active_topic_string",
        "entities_list",
        "fact_admissions_list",
        "elapsed_nonnegative",
        "context_changes_object",
        "learned_statements_list",
    }
)


class CoreState(StrEnum):
    """Lifecycle state of the single owned application core."""

    RUNNING = "running"
    QUIESCING = "quiescing"
    MAINTENANCE = "maintenance"
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
