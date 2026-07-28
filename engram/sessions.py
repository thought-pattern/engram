"""Session management for ENGRAM.

This module provides session management functionality including creation,
retrieval, updates, expiration, and cleanup of user sessions.
"""

from datetime import UTC, datetime, timedelta

from engram.constants import DEFAULT_USER_ID, SessionOverflow
from engram.models import session, session_update_context


class SessionLimitExceededError(Exception):
    """Raised when session limit is reached and overflow is REJECT."""

    pass


class SessionNotFoundError(Exception):
    """Raised when a session is not found."""

    pass


def normalize_user_id(user_id: str | None = None) -> str:
    """Return the caller-owned user label, defaulting missing labels to "0"."""
    if user_id is None or user_id == "":
        return DEFAULT_USER_ID
    if not isinstance(user_id, str):
        raise ValueError("user_id must be a string")
    return user_id


def create_session(engram, session_id=None, metadata=None) -> str:
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
        if len(engram.sessions) >= engram.config["max_sessions"]:
            if engram.config["session_overflow"] == SessionOverflow.REJECT:
                raise SessionLimitExceededError("Maximum sessions reached")
            elif engram.config["session_overflow"] == SessionOverflow.EXPIRE_OLDEST:
                _expire_oldest_session(engram)
            else:  # LRU
                _expire_lru_session(engram)

        sess = session(session_id=session_id, metadata=metadata)
        engram.sessions[sess["session_id"]] = sess
        return sess["session_id"]


def get_session(engram, session_id: str, create_if_missing: bool = True):
    """Retrieve an existing session.

    Args:
        engram: Engram instance.
        session_id: Session ID to retrieve.
        create_if_missing: If True, create session if not found (default: True).

    Returns:
        Session object or None if not found and create_if_missing is False.
    """
    with engram.session_lock:
        session = engram.sessions.get(session_id)
        if session is None and create_if_missing:
            create_session(engram, session_id=session_id)
            session = engram.sessions.get(session_id)
        return session


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
        session = engram.sessions.get(session_id)
        if session is None:
            raise SessionNotFoundError(f"Session not found: {session_id}")
        session_update_context(session, previous_response)


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


def expire_sessions(engram, inactive_threshold=None) -> int:
    """Remove inactive sessions based on threshold.

    Args:
        engram: Engram instance.
        inactive_threshold: Inactivity cutoff. Defaults to session_ttl from config.

    Returns:
        Number of sessions removed.
    """
    if inactive_threshold is None:
        inactive_threshold = timedelta(seconds=engram.config["session_ttl_seconds"])

    cutoff = datetime.now(UTC) - inactive_threshold
    expired_ids: list[str] = []

    with engram.session_lock:
        for sid, session in engram.sessions.items():
            if session["last_active"] < cutoff:
                expired_ids.append(sid)
        for sid in expired_ids:
            del engram.sessions[sid]

    removed = len(expired_ids)
    return removed


def list_sessions(engram, active_since=None) -> list:
    """List all sessions, optionally filtered by activity.

    Args:
        engram: Engram instance.
        active_since: If provided, only return sessions active since this time.

    Returns:
        List of matching sessions.
    """
    with engram.session_lock:
        if active_since is None:
            all_sessions = list(engram.sessions.values())
            return all_sessions
        filtered = [s for s in engram.sessions.values() if s["last_active"] >= active_since]
        return filtered


def _expire_oldest_session(engram) -> None:
    """Remove the oldest session by creation time.

    Used when session limit is reached and overflow policy is EXPIRE_OLDEST.
    """
    if not engram.sessions:
        return
    oldest = min(engram.sessions.values(), key=lambda s: s["created_at"])
    del engram.sessions[oldest["session_id"]]


def _expire_lru_session(engram) -> None:
    """Remove the least recently used session.

    Used when session limit is reached and overflow policy is LRU.
    """
    if not engram.sessions:
        return
    lru = min(engram.sessions.values(), key=lambda s: s["last_active"])
    del engram.sessions[lru["session_id"]]
