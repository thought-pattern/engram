"""Data models for ENGRAM."""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from uuid import uuid4

from engram.substitutions import split_sentences


class Tier(Enum):
    """Statement tier classification."""

    STATIC = "STATIC"
    DYNAMIC = "DYNAMIC"


@dataclass
class Statement:
    """Atomic unit of storage in ENGRAM (Category).

    Represents a pattern-template pair with optional context constraints.
    """

    id: str
    text: str  # Response text or template
    tier: Tier
    created_at: datetime
    keywords: list[str] = field(default_factory=list)
    pattern: str = ""  # AIML-style pattern for matching
    that: str = ""  # Pattern to match bot's previous response
    topic: str = ""  # Topic scope constraint
    template: object = None  # Structured template (JSON)
    priority: int = 0  # Override default priority (higher = preferred)
    # Eviction tracking
    hit_count: int = 0  # Number of times this statement was selected
    query_count: int = 0  # Number of times this statement was a candidate
    last_hit: object = None  # Timestamp of most recent hit

    @property
    def hit_rate(self) -> float:
        """Calculate hit rate (hits/queries), default 0.5 when undefined."""
        if self.query_count == 0:
            return 0.5
        return self.hit_count / self.query_count

    def record_hit(self) -> None:
        """Record a hit on this statement."""
        self.hit_count += 1
        self.last_hit = datetime.now(timezone.utc)

    def record_query(self) -> None:
        """Record that this statement was a candidate in a query."""
        self.query_count += 1

    @classmethod
    def create(
        cls,
        text: str,
        tier: Tier = Tier.DYNAMIC,
        keywords=None,
        statement_id=None,
        pattern: str = "",
        that: str = "",
        topic: str = "",
        template=None,
        priority: int = 0,
    ) -> "Statement":
        """Create a new statement with generated ID and timestamp."""
        return cls(
            id=statement_id or f"stmt_{uuid4().hex[:12]}",
            text=text,
            tier=tier,
            created_at=datetime.now(timezone.utc),
            keywords=keywords or [],
            pattern=pattern,
            that=that,
            topic=topic,
            template=template,
            priority=priority,
        )

    def to_dict(self) -> dict:
        """Serialize to dictionary."""
        data = {
            "id": self.id,
            "text": self.text,
            "tier": self.tier.value,
            "created_at": self.created_at.isoformat(),
            "keywords": self.keywords,
            "pattern": self.pattern,
        }
        # Only include optional fields if set
        if self.that:
            data["that"] = self.that
        if self.topic:
            data["topic"] = self.topic
        if self.template is not None:
            data["template"] = self.template
        if self.priority != 0:
            data["priority"] = self.priority
        # Eviction tracking (always include for consistency)
        data["hit_count"] = self.hit_count
        data["query_count"] = self.query_count
        if self.last_hit is not None:
            data["last_hit"] = self.last_hit.isoformat()
        return data

    @classmethod
    def from_dict(cls, data: dict) -> "Statement":
        """Deserialize from dictionary."""
        last_hit = None
        if data.get("last_hit"):
            last_hit = datetime.fromisoformat(data["last_hit"])
        return cls(
            id=data["id"],
            text=data["text"],
            tier=Tier(data["tier"]),
            created_at=datetime.fromisoformat(data["created_at"]),
            keywords=data.get("keywords", []),
            pattern=data.get("pattern", ""),
            that=data.get("that", ""),
            topic=data.get("topic", ""),
            template=data.get("template"),
            priority=data.get("priority", 0),
            hit_count=data.get("hit_count", 0),
            query_count=data.get("query_count", 0),
            last_hit=last_hit,
        )


@dataclass
class KeywordEntry:
    """Keyword index entry with retrieval statistics."""

    keyword: str
    statement_ids: set[str] = field(default_factory=set)
    query_count: int = 0
    hit_count: int = 0

    @property
    def hit_rate(self) -> float:
        """Calculate hit rate, defaulting to 0.5 when undefined."""
        if self.query_count == 0:
            return 0.5
        return self.hit_count / self.query_count

    def to_dict(self) -> dict:
        """Serialize to dictionary."""
        return {
            "statement_ids": list(self.statement_ids),
            "query_count": self.query_count,
            "hit_count": self.hit_count,
        }

    @classmethod
    def from_dict(cls, keyword: str, data: dict) -> "KeywordEntry":
        """Deserialize from dictionary."""
        return cls(
            keyword=keyword,
            statement_ids=set(data.get("statement_ids", [])),
            query_count=data.get("query_count", 0),
            hit_count=data.get("hit_count", 0),
        )


@dataclass
class Session:
    """Independent conversation context.

    Stores per-user state including predicates (variables), topic,
    and conversation history.
    """

    session_id: str
    previous_response: str  # Kept for backward compatibility (alias for that)
    created_at: datetime
    last_active: datetime
    metadata: dict = field(default_factory=dict)
    predicates: dict[str, str] = field(default_factory=dict)
    input_history: list[str] = field(default_factory=list)
    response_history: list[str] = field(default_factory=list)
    that_history: list[list[str]] = field(default_factory=list)
    history_size: int = 10  # Maximum history entries

    @classmethod
    def create(
        cls,
        session_id=None,
        metadata=None,
        history_size: int = 10,
    ) -> "Session":
        """Create a new session with generated ID and timestamps."""
        now = datetime.now(timezone.utc)
        return cls(
            session_id=session_id or f"sess_{uuid4().hex[:12]}",
            previous_response="",
            created_at=now,
            last_active=now,
            metadata=metadata or {},
            predicates={},
            input_history=[],
            response_history=[],
            that_history=[],
            history_size=history_size,
        )

    def update_context(
        self,
        previous_response: str,
        user_input=None,
    ) -> None:
        """Update the session's context after a turn.

        Args:
            previous_response: Bot's response text.
            user_input: User's input text (optional).
        """
        self.previous_response = previous_response
        self.last_active = datetime.now(timezone.utc)

        # Update response history
        if previous_response:
            self.response_history.insert(0, previous_response)
            if len(self.response_history) > self.history_size:
                self.response_history.pop()

            # Update that_history (split into sentences)
            sentences = split_sentences(previous_response.upper())
            self.that_history.insert(0, sentences)
            if len(self.that_history) > self.history_size:
                self.that_history.pop()

        # Update input history
        if user_input:
            self.input_history.insert(0, user_input)
            if len(self.input_history) > self.history_size:
                self.input_history.pop()

    def touch(self) -> None:
        """Update last_active timestamp."""
        self.last_active = datetime.now(timezone.utc)

    def clear_predicates(self) -> None:
        """Clear all predicates except topic."""
        topic = self.predicates.get("topic", "")
        self.predicates.clear()
        if topic:
            self.predicates["topic"] = topic

    def to_dict(self) -> dict:
        """Serialize to dictionary."""
        return {
            "session_id": self.session_id,
            "previous_response": self.previous_response,
            "created_at": self.created_at.isoformat(),
            "last_active": self.last_active.isoformat(),
            "metadata": self.metadata,
            "predicates": self.predicates,
            "input_history": self.input_history,
            "response_history": self.response_history,
            "that_history": self.that_history,
            "history_size": self.history_size,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Session":
        """Deserialize from dictionary."""
        return cls(
            session_id=data["session_id"],
            previous_response=data.get("previous_response", ""),
            created_at=datetime.fromisoformat(data["created_at"]),
            last_active=datetime.fromisoformat(data["last_active"]),
            metadata=data.get("metadata", {}),
            predicates=data.get("predicates", {}),
            input_history=data.get("input_history", []),
            response_history=data.get("response_history", []),
            that_history=data.get("that_history", []),
            history_size=data.get("history_size", 10),
        )


@dataclass
class QueryResult:
    """Result from a query operation."""

    matches: list[tuple[Statement, float]]  # (statement, score) pairs
    keywords: list[str]  # Extracted query keywords
