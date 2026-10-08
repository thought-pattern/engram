"""Session lifecycle operations over an Engram instance's session map.

The ``engram.sessions`` map and its ``engram.session_lock`` belong to the Engram
instance; this module is the only code that adds, replaces or removes a session.
A session record is live shared state, so every read or change of one happens
while the caller holds ``session_lock``. The module sits above ``engram.models``,
which defines the record shape, and below ``core``, ``conversation``, ``pipeline``
and ``service``.
"""

from datetime import datetime, timedelta

from engram.constants import DEFAULT_USER_ID, EARLIEST_UTC, SessionOverflow
from engram.errors import InvalidRequestError, ResourceExhaustedError, ResourceNotFoundError
from engram.models import session, session_update_context


class SessionLimitExceededError(ResourceExhaustedError):
    """Raised when session limit is reached and overflow is REJECT."""


class SessionNotFoundError(ResourceNotFoundError):
    """Raised when a session is not found."""


def require_session_lock(engram) -> None:
    """Refuse a live session access made without holding ``engram.session_lock``."""
    if not engram.session_lock.held_by_current_thread():
        raise RuntimeError("live session access requires holding session_lock")


def normalize_user_id(user_id: str = "") -> str:
    """Return the caller-owned user label, defaulting missing labels to "0"."""
    if user_id == "":
        return DEFAULT_USER_ID
    if not isinstance(user_id, str):
        raise InvalidRequestError("user_id must be a string")
    return user_id


def start_session(engram, session_id: str = "", metadata=()) -> str:
    """Start a new session and return its identifier.

    Args:
        engram: Engram instance.
        session_id: Optional specific ID. If not provided, one will be generated.
        metadata: Optional key-value pairs to store with the session.

    Returns:
        Session ID of the created session.

    Raises:
        SessionLimitExceededError: If at maximum sessions and overflow policy is REJECT.
    """

    with engram.session_lock:
        # Check session limit
        if len(engram.sessions) >= engram.config.get("max_sessions", 0):
            if engram.config.get("session_overflow", SessionOverflow.REJECT) == SessionOverflow.REJECT:
                raise SessionLimitExceededError("Maximum sessions reached")
            elif engram.config.get("session_overflow", SessionOverflow.REJECT) == SessionOverflow.EXPIRE_OLDEST:
                expire_oldest_session(engram)
            else:  # LRU
                expire_lru_session(engram)

        sess = session(
            session_id=session_id or f"sess_{engram.identifier_source()}",
            created_at=engram.clock(),
            metadata=metadata,
        )
        sess["predicates"] = dict(engram.default_predicates)
        identifier = sess.get("session_id", "")
        engram.sessions[identifier] = sess
        result = identifier
        return result


def get_session(engram, session_id: str, create_if_missing: bool = True) -> dict:
    """Return the live session record, creating it when asked.

    The caller must hold ``engram.session_lock`` for as long as it reads or changes
    the returned record; a call without the lock raises ``RuntimeError``.

    Args:
        engram: Engram instance.
        session_id: Session ID to retrieve.
        create_if_missing: If True, create session if not found (default: True).

    Returns:
        Session object or an empty dict if not found and create_if_missing is False.
    """
    require_session_lock(engram)
    session = engram.sessions.get(session_id, {})
    if not session and create_if_missing:
        start_session(engram, session_id=session_id)
        session = engram.sessions.get(session_id, {})
    return session


def restore_session(engram, record: dict) -> None:
    """Put back a session record the caller took out of the map earlier in the same locked operation."""
    require_session_lock(engram)
    identifier = record.get("session_id", "")
    if not identifier:
        raise InvalidRequestError("a restored session must carry its session_id")
    engram.sessions[identifier] = record


def update_session_context(engram, session_id: str, previous_response: str) -> None:
    """Update session's previous response context.

    Args:
        engram: Engram instance.
        session_id: Session ID to update.
        previous_response: Response text to add to context.

    Raises:
        SessionNotFoundError: If session does not exist.
    """

    with engram.session_lock:
        session = engram.sessions.get(session_id, {})
        if not session:
            raise SessionNotFoundError(f"Session not found: {session_id}")
        session_update_context(session, previous_response, moment=engram.clock())


def delete_session(engram, session_id: str) -> bool:
    """Remove a session.

    Args:
        engram: Engram instance.
        session_id: Session ID to delete.

    Returns:
        True if session was deleted, False if not found.
    """
    with engram.session_lock:
        if session_id in engram.sessions:
            del engram.sessions[session_id]
            result = True
            return result
        result = False
        return result


def expire_sessions(engram, now: datetime, inactive_threshold: timedelta = timedelta()) -> int:
    """Remove inactive sessions based on threshold.

    Args:
        engram: Engram instance.
        now: The caller's aware UTC time snapshot for this sweep.
        inactive_threshold: Inactivity cutoff. Defaults to session_ttl from config.

    Returns:
        Number of sessions removed.
    """
    if not inactive_threshold:
        inactive_threshold = timedelta(seconds=engram.config.get("session_ttl_seconds", 0))

    # Comparing elapsed inactivity, rather than subtracting the threshold from
    # now, keeps every representable threshold free of datetime underflow.
    expired_ids: list[str] = []

    with engram.session_lock:
        for sid, session in engram.sessions.items():
            if now - session.get("last_active", EARLIEST_UTC) > inactive_threshold:
                expired_ids.append(sid)
        for sid in expired_ids:
            del engram.sessions[sid]

    removed = len(expired_ids)
    return removed


def list_sessions(engram, active_since: datetime = EARLIEST_UTC) -> list:
    """List all sessions, optionally filtered by activity.

    Args:
        engram: Engram instance.
        active_since: If provided, only return sessions active since this time.

    Returns:
        List of matching sessions.
    """
    with engram.session_lock:
        filtered = [s for s in engram.sessions.values() if s.get("last_active", EARLIEST_UTC) >= active_since]
        return filtered


def expire_oldest_session(engram) -> bool:
    """Remove the oldest session by creation time.

    Used when session limit is reached and overflow policy is EXPIRE_OLDEST.
    """
    if not engram.sessions:
        return False
    oldest = min(engram.sessions.values(), key=lambda s: s.get("created_at", EARLIEST_UTC))
    identifier = oldest.get("session_id", "")
    del engram.sessions[identifier]
    return True


def expire_lru_session(engram) -> bool:
    """Remove the least recently used session.

    Used when session limit is reached and overflow policy is LRU.
    """
    if not engram.sessions:
        return False
    lru = min(engram.sessions.values(), key=lambda s: s.get("last_active", EARLIEST_UTC))
    identifier = lru.get("session_id", "")
    del engram.sessions[identifier]
    return True
