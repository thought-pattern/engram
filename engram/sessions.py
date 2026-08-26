"""Session management for ENGRAM.

This module provides session management functionality including creation,
retrieval, updates, expiration, and cleanup of user sessions.
"""

from datetime import UTC, datetime, timedelta

from engram.constants import DEFAULT_USER_ID, SessionOverflow
from engram.models import session, session_update_context

MIN_SESSION_TIME = datetime.min.replace(tzinfo=UTC)


class SessionLimitExceededError(Exception):
    """Raised when session limit is reached and overflow is REJECT."""


class SessionNotFoundError(Exception):
    """Raised when a session is not found."""


def normalize_user_id(user_id: str = "") -> str:
    """Return the caller-owned user label, defaulting missing labels to "0"."""
    if user_id is None:
        user_id = ""
    if user_id == "" or user_id == "":
        return DEFAULT_USER_ID
    if not isinstance(user_id, str):
        raise ValueError("user_id must be a string")
    return user_id


def create_session(engram, session_id=False, metadata=False) -> str:
    """Create a new session.

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
            if engram.config.get("session_overflow", False) == SessionOverflow.REJECT:
                raise SessionLimitExceededError("Maximum sessions reached")
            elif engram.config.get("session_overflow", False) == SessionOverflow.EXPIRE_OLDEST:
                _expire_oldest_session(engram)
            else:  # LRU
                _expire_lru_session(engram)

        sess = session(session_id=session_id, metadata=metadata)
        engram.sessions[sess.get("session_id", "")] = sess
        _return_value = sess.get("session_id", "")
        return _return_value
    return ""


def get_session(engram, session_id: str, create_if_missing: bool = True):
    """Retrieve an existing session.

    Args:
        engram: Engram instance.
        session_id: Session ID to retrieve.
        create_if_missing: If True, create session if not found (default: True).

    Returns:
        Session object or `False` if not found and create_if_missing is False.
    """
    with engram.session_lock:
        session = engram.sessions.get(session_id, False)
        if session is False and create_if_missing:
            create_session(engram, session_id=session_id)
            session = engram.sessions.get(session_id, False)
        return session


def update_session_context(engram, session_id: str, previous_response: str) -> bool:
    """Update session's previous response context.

    Args:
        engram: Engram instance.
        session_id: Session ID to update.
        previous_response: Response text to add to context.

    Raises:
        SessionNotFoundError: If session does not exist.
    """

    with engram.session_lock:
        session = engram.sessions.get(session_id, False)
        if session is False:
            raise SessionNotFoundError(f"Session not found: {session_id}")
        session_update_context(session, previous_response)
    return False


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
            return True
        return False


def expire_sessions(engram, inactive_threshold=False) -> int:
    """Remove inactive sessions based on threshold.

    Args:
        engram: Engram instance.
        inactive_threshold: Inactivity cutoff. Defaults to session_ttl from config.

    Returns:
        Number of sessions removed.
    """
    if inactive_threshold is None:
        inactive_threshold = False
    if inactive_threshold is False:
        inactive_threshold = timedelta(seconds=engram.config.get("session_ttl_seconds", 0.0))

    cutoff = datetime.now(UTC) - inactive_threshold
    expired_ids: list[str] = []

    with engram.session_lock:
        for sid, session in engram.sessions.items():
            if session.get("last_active", MIN_SESSION_TIME) < cutoff:
                expired_ids.append(sid)
        for sid in expired_ids:
            del engram.sessions[sid]

    removed = len(expired_ids)
    return removed


def list_sessions(engram, active_since=MIN_SESSION_TIME) -> list:
    """List all sessions, optionally filtered by activity.

    Args:
        engram: Engram instance.
        active_since: If provided, only return sessions active since this time.

    Returns:
        List of matching sessions.
    """
    if active_since is None:
        active_since = MIN_SESSION_TIME
    with engram.session_lock:
        if active_since == MIN_SESSION_TIME:
            all_sessions = list(engram.sessions.values())
            return all_sessions
        filtered = [s for s in engram.sessions.values() if s.get("last_active", MIN_SESSION_TIME) >= active_since]
        return filtered
    return []


def _expire_oldest_session(engram) -> bool:
    """Remove the oldest session by creation time.

    Used when session limit is reached and overflow policy is EXPIRE_OLDEST.
    """
    if not engram.sessions:
        return False
    oldest = min(engram.sessions.values(), key=lambda s: s.get("created_at", False))
    del engram.sessions[oldest.get("session_id", "")]
    return False


def _expire_lru_session(engram) -> bool:
    """Remove the least recently used session.

    Used when session limit is reached and overflow policy is LRU.
    """
    if not engram.sessions:
        return False
    lru = min(engram.sessions.values(), key=lambda s: s.get("last_active", False))
    del engram.sessions[lru.get("session_id", "")]
    return False
