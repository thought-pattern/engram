"""Conversations for the Engram service.

Starting, owning, releasing and finishing per-user conversation runtimes, chat and
inspection turns, shared fact intake, and per-user predicates. Each function that takes
``core`` is bound on ``engram.service.EngramCore`` as the method of the same name.
"""

from time import monotonic as time_monotonic
from uuid import uuid4

from engram import sessions
from engram.constants import ANONYMOUS_CONVERSATION_LEASE_SECONDS, DEFAULT_USER_ID, SessionOverflow
from engram.constants_artifacts import MAX_METADATA_KEY_BYTES, MAX_METADATA_STRING_BYTES, MAX_RESPONSE_BYTES, MAX_SOURCE_LABEL_BYTES
from engram.conversation import ConversationRuntime, statement_view
from engram.errors import ConflictError, ConversationOwnershipError, LifecycleError, ResourceNotFoundError
from engram.service_requests import normalize_service_user_id
from engram.validation import require_any_text


def start_conversation(
    core,
    user_id: str = DEFAULT_USER_ID,
    initial_bot_text: str = "",
    random_seed: int = 0,
    random_seed_present: bool = False,
) -> dict:
    """Create one observable conversation for a user context."""
    with core.lock:
        core.require_running()
        conversation_id = normalize_service_user_id(user_id)
        if conversation_id == DEFAULT_USER_ID and conversation_id in core.conversations:
            idle = time_monotonic() - core.conversation_activity.get(conversation_id, time_monotonic())
            if idle >= ANONYMOUS_CONVERSATION_LEASE_SECONDS:
                core.release_conversation(core.conversations.get(conversation_id, ()))
        if conversation_id in core.conversations:
            raise ConflictError(f"conversation already active for user_id: {conversation_id}")
        core.make_conversation_room()
        # Request validation raises InvalidRequestError with its own message;
        # anything else is an internal failure, logged at the transport.
        runtime = ConversationRuntime(
            core.engram,
            user_id=conversation_id,
            initial_bot_text=initial_bot_text,
            random_seed=random_seed,
            random_seed_present=random_seed_present,
            clock=core.internal_clock,
        )
        core.conversations[conversation_id] = runtime
        core.conversation_activity[conversation_id] = time_monotonic()
        snapshot = runtime.inspect()
        result = {
            "started": True,
            "user_id": snapshot.get("user_id", ""),
            "initial_bot_text": snapshot.get("initial_bot_text", ""),
            "turn_count": snapshot.get("turn_count", 0),
            "statement_count": snapshot.get("metrics", {}).get("statement_count", 0),
            "memory_only": True,
        }
        if conversation_id == DEFAULT_USER_ID:
            token = uuid4().hex
            core.conversation_tokens[conversation_id] = token
            result["conversation_token"] = token
        return result


def owned_conversation(core, user_id: str, conversation_token: str) -> ConversationRuntime:
    """Return an active runtime, requiring the anonymous owner's token and renewing its lease."""
    runtime = core.get_conversation(user_id)
    if runtime.user_id == DEFAULT_USER_ID:
        expected = core.conversation_tokens.get(runtime.user_id, "")
        if not expected or not isinstance(conversation_token, str) or conversation_token != expected:
            raise ConversationOwnershipError("anonymous conversation requires the token issued when it started")
    core.conversation_activity[runtime.user_id] = time_monotonic()
    return runtime


def release_conversation(core, runtime: object) -> None:
    """Remove one runtime and its anonymous session, taught facts, ownership token, and lease."""
    if not isinstance(runtime, ConversationRuntime):
        raise LifecycleError("active conversation runtime is malformed")
    core.conversations.pop(runtime.user_id, {})
    core.conversation_tokens.pop(runtime.user_id, "")
    core.conversation_activity.pop(runtime.user_id, 0.0)
    if runtime.user_id == DEFAULT_USER_ID:
        sessions.delete_session(core.engram, runtime.session_id)
        core.engram.forget_person_facts(DEFAULT_USER_ID)


def make_conversation_room(core) -> None:
    """Bound the runtime registry by the session capacity before one more runtime starts.

    Each runtime owns one session, so a runtime whose session the session policy has
    already evicted or expired is released first. At capacity the configured overflow
    policy applies to runtimes as it does to sessions: reject refuses the start,
    expire-oldest releases the earliest started runtime, and LRU the least recently
    active one. A runtime with a turn in flight is never released, and explicit stop is
    unchanged. Call with the core lock held.
    """
    with core.engram.session_lock:
        stale = [
            runtime
            for user_id, runtime in core.conversations.items()
            if runtime.session_id not in core.engram.sessions and user_id not in core.active_resolution_user_ids
        ]
    for runtime in stale:
        core.release_conversation(runtime)
    if len(core.conversations) >= core.engram.config.get("max_sessions", 1):
        overflow = core.engram.config.get("session_overflow", SessionOverflow.REJECT)
        idle = [runtime for user_id, runtime in core.conversations.items() if user_id not in core.active_resolution_user_ids]
        if overflow == SessionOverflow.REJECT or not idle:
            raise sessions.SessionLimitExceededError("maximum active conversations reached")
        if overflow == SessionOverflow.EXPIRE_OLDEST:
            selected = min(idle, key=lambda runtime: runtime.started_at)
        else:
            selected = min(idle, key=lambda runtime: core.conversation_activity.get(runtime.user_id, 0.0))
        core.release_conversation(selected)


def get_conversation(core, user_id: str) -> ConversationRuntime:
    """Return an active user runtime or raise a lifecycle error."""
    with core.lock:
        core.require_running()
        conversation_id = normalize_service_user_id(user_id)
        if conversation_id not in core.conversations:
            raise ResourceNotFoundError(f"no active conversation for user_id: {conversation_id}")
        result = core.conversations.get(conversation_id, ())
        if not isinstance(result, ConversationRuntime):
            raise LifecycleError("active conversation runtime is malformed")
        return result


def chat(core, user_id: str, text: str, conversation_token: str = "") -> dict:
    """Submit one chatbot turn to an active user conversation."""
    conversation_id = normalize_service_user_id(user_id)
    operation_id = f"chat:{uuid4().hex}"
    with core.resolution_slot(operation_id, conversation_id), core.lock:
        core.require_running()
        runtime = core.owned_conversation(conversation_id, conversation_token)
        result = runtime.send(text)
        return result


def inspect_conversation(core, user_id: str, conversation_token: str = "") -> dict:
    """Inspect one conversation and shared regulated-cache metrics."""
    with core.lock:
        core.require_running()
        core.cleanup_transient()
        snapshot = core.owned_conversation(user_id, conversation_token).inspect()
        snapshot["regulated_cache"] = core.regulated_cache_metrics()
        snapshot["core_status"] = core.status()
        return snapshot


def add_fact(core, text: str, source_label: str = "") -> dict:
    """Add one unattributed shared fact without changing user context."""
    with core.lock:
        core.require_running()
        require_any_text(text, "text", MAX_RESPONSE_BYTES, blank_is_empty=True)
        require_any_text(source_label, "source_label", MAX_SOURCE_LABEL_BYTES, allow_empty=True)
        statement_id = core.engram.add_fact(text, source_label=source_label)
        result = statement_view(core.engram.get_statement(statement_id))
        return result


def finish_conversation(core, user_id: str, conversation_token: str = "") -> dict:
    """Return a report without ending or persisting the conversation."""
    with core.lock:
        core.require_running()
        runtime = core.owned_conversation(user_id, conversation_token)
        result = runtime.report()
        return result


def stop_conversation(core, user_id: str, conversation_token: str = "") -> dict:
    """End one conversation while leaving the shared core available."""
    with core.lock:
        core.require_running()
        runtime = core.owned_conversation(user_id, conversation_token)
        report = runtime.report()
        core.release_conversation(runtime)
        result = {
            "stopped": True,
            "user_id": report.get("user_id", ""),
            "summary": report.get("summary", {}),
        }
        return result


def set_predicate(core, user_id: str, name: str, value: str) -> None:
    """Set one caller-owned predicate on a user context."""
    with core.lock:
        core.require_running()
        require_any_text(name, "name", MAX_METADATA_KEY_BYTES, blank_is_empty=True)
        require_any_text(value, "value", MAX_METADATA_STRING_BYTES, allow_empty=True)
        normalized_user_id = normalize_service_user_id(user_id)
        with core.engram.session_lock:
            session = sessions.get_session(core.engram, normalized_user_id, create_if_missing=True)
            predicates = session.get("predicates", {})
            predicates[name] = value
            session["predicates"] = predicates


def get_predicate(core, user_id: str, name: str, default=""):
    """Read one caller-owned predicate from a user context."""
    with core.lock:
        core.require_running()
        require_any_text(name, "name", MAX_METADATA_KEY_BYTES, blank_is_empty=True)
        require_any_text(default, "default", MAX_METADATA_STRING_BYTES, allow_empty=True)
        normalized_user_id = normalize_service_user_id(user_id)
        with core.engram.session_lock:
            session = sessions.get_session(core.engram, normalized_user_id, create_if_missing=False)
            result = session.get("predicates", {}).get(name, default) if session else default
            return result
