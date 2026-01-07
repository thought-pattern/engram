"""Session management for ENGRAM.

This module provides session management functionality including creation,
retrieval, updates, expiration, and cleanup of user sessions.
"""

from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from engram.config import EngramConfig, SessionOverflow
    from engram.models import Session


class SessionLimitExceeded(Exception):
    """Raised when session limit is reached and overflow is REJECT."""

    pass


class SessionNotFound(Exception):
    """Raised when a session is not found."""

    pass


class SessionMixin:
    """Mixin class providing session management methods.

    This mixin is designed to be used with the Engram class and expects
    the following attributes to be present:
    - _sessions: dict[str, Session]
    - _session_lock: threading.RLock
    - config: EngramConfig
    """

    # Type hints for expected attributes (defined in Engram)
    _sessions: dict[str, "Session"]
    _session_lock: threading.RLock
    config: "EngramConfig"

    def create_session(
        self,
        session_id=None,
        metadata=None,
    ) -> str:
        """Create a new session.

        Args:
            session_id: Optional specific ID. If not provided, one will be generated.
            metadata: Optional key-value pairs to store with the session.

        Returns:
            Session ID of the created session.

        Raises:
            SessionLimitExceeded: If at maximum sessions and overflow policy is REJECT.
        """
        from engram.config import SessionOverflow
        from engram.models import Session

        with self._session_lock:
            # Check session limit
            if len(self._sessions) >= self.config.max_sessions:
                if self.config.session_overflow == SessionOverflow.REJECT:
                    raise SessionLimitExceeded("Maximum sessions reached")
                elif self.config.session_overflow == SessionOverflow.EXPIRE_OLDEST:
                    self._expire_oldest_session()
                else:  # LRU
                    self._expire_lru_session()

            session = Session.create(session_id=session_id, metadata=metadata)
            self._sessions[session.session_id] = session
            return session.session_id

    def get_session(
        self,
        session_id: str,
        create_if_missing: bool = True,
    ) -> "Session":
        """Retrieve an existing session.

        Args:
            session_id: Session ID to retrieve.
            create_if_missing: If True, create session if not found (default: True).

        Returns:
            Session object or None if not found and create_if_missing is False.
        """
        with self._session_lock:
            session = self._sessions.get(session_id)
            if session is None and create_if_missing:
                self.create_session(session_id=session_id)
                session = self._sessions.get(session_id)
            return session

    def update_session_context(self, session_id: str, previous_response: str) -> None:
        """Update session's previous response context.

        Args:
            session_id: Session ID to update.
            previous_response: Response text to add to context.

        Raises:
            SessionNotFound: If session does not exist.
        """
        with self._session_lock:
            session = self._sessions.get(session_id)
            if session is None:
                raise SessionNotFound(f"Session not found: {session_id}")
            session.update_context(previous_response)

    def delete_session(self, session_id: str) -> bool:
        """Remove a session.

        Args:
            session_id: Session ID to delete.

        Returns:
            True if session was deleted, False if not found.
        """
        with self._session_lock:
            if session_id in self._sessions:
                del self._sessions[session_id]
                return True
            return False

    def expire_sessions(self, inactive_threshold=None) -> int:
        """Remove inactive sessions based on threshold.

        Args:
            inactive_threshold: Inactivity cutoff. Defaults to session_ttl from config.

        Returns:
            Number of sessions removed.
        """
        if inactive_threshold is None:
            inactive_threshold = timedelta(seconds=self.config.session_ttl_seconds)

        cutoff = datetime.now(timezone.utc) - inactive_threshold
        expired_ids: list[str] = []

        with self._session_lock:
            for sid, session in self._sessions.items():
                if session.last_active < cutoff:
                    expired_ids.append(sid)
            for sid in expired_ids:
                del self._sessions[sid]

        return len(expired_ids)

    def list_sessions(self, active_since=None) -> list["Session"]:
        """List all sessions, optionally filtered by activity.

        Args:
            active_since: If provided, only return sessions active since this time.

        Returns:
            List of matching sessions.
        """
        with self._session_lock:
            if active_since is None:
                return list(self._sessions.values())
            return [s for s in self._sessions.values() if s.last_active >= active_since]

    def _expire_oldest_session(self) -> None:
        """Remove the oldest session by creation time.

        Used when session limit is reached and overflow policy is EXPIRE_OLDEST.
        """
        if not self._sessions:
            return
        oldest = min(self._sessions.values(), key=lambda s: s.created_at)
        del self._sessions[oldest.session_id]

    def _expire_lru_session(self) -> None:
        """Remove the least recently used session.

        Used when session limit is reached and overflow policy is LRU.
        """
        if not self._sessions:
            return
        lru = min(self._sessions.values(), key=lambda s: s.last_active)
        del self._sessions[lru.session_id]
