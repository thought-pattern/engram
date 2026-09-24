"""Process-local conversation orchestration shared by human and agent adapters.

``ConversationRuntime`` owns turn-level diagnostics, inspection, and report
generation without writing conversation state to disk.
"""

from collections import Counter, deque
from datetime import UTC, datetime
from random import getstate as random_getstate, seed as random_seed, setstate as random_setstate
from threading import RLock as threading_RLock
from time import perf_counter as time_perf_counter

from engram import metrics, pipeline, sessions
from engram.constants import CONVERSATION_REPORT_VERSION, MAX_REQUEST_BYTES, MAX_RESPONSE_BYTES, Tier
from engram.text import normalize


def utc_now() -> str:
    result = datetime.now(UTC).isoformat()
    return result


def statement_view(statement: dict) -> dict:
    """Return the statement fields useful to conversation adapters."""
    result = {
        "id": statement.get("id", ""),
        "text": statement.get("text", ""),
        "pattern": statement.get("pattern", ""),
        "pattern_aliases": list(statement.get("pattern_aliases", [])),
        "introduced_by_user_id": statement.get("introduced_by_user_id", "") or "",
        "source_label": statement.get("source_label", ""),
    }
    return result


def session_view(session: dict) -> dict:
    """Return a JSON-ready snapshot of one user conversation context."""
    result = {
        "session_id": session.get("session_id", ""),
        "previous_response": session.get("previous_response", ""),
        "predicates": dict(session.get("predicates", {})),
        "active_topic": session.get("active_topic", ""),
        "entities": list(session.get("entities", [])),
        "dialogue_act_history": list(session.get("dialogue_act_history", [])),
        "last_fact_admissions": list(session.get("last_fact_admissions", [])),
        "input_history": list(session.get("input_history", [])),
        "response_history": list(session.get("response_history", [])),
        "history_size": session.get("history_size", 0),
    }
    return result


def predicate_changes(before: dict, after: dict) -> dict:
    changes = {}
    for name in sorted(set(before) | set(after)):
        old_value = before.get(name, "")
        new_value = after.get(name, "")
        if old_value != new_value:
            changes[name] = {"before": old_value, "after": new_value}
    return changes


def conversation_message_key(message: str) -> str:
    """Return the meaningful normalized identity of one planned message."""
    if not isinstance(message, str) or not message.strip():
        raise ValueError("conversation messages must be non-empty strings")
    key = normalize(message)
    if not key:
        raise ValueError("conversation messages must contain meaningful text")
    return key


class ConversationTurnPlanner:
    """Budget a scripted/adaptive conversation without accidental repeats.

    Planned messages are always preserved, the final turn is reserved for the
    farewell, and adaptive messages are accepted only while spare turn budget
    remains. Exhaustion and unapproved normalized duplicates fail explicitly
    instead of generating repeated filler.
    """

    def __init__(
        self,
        planned_messages: list[str],
        total_turns: int,
        farewell: str,
        allowed_repeats=(),
    ) -> None:
        if not isinstance(total_turns, int) or total_turns < 1:
            raise ValueError("total_turns must be a positive integer")
        if len(planned_messages) > total_turns - 1:
            raise ValueError("planned messages must leave the final turn for the farewell")

        self.total_turns = total_turns
        self.internal_planned = deque(planned_messages)
        self.internal_farewell = farewell
        self.sent_keys: set[str] = set()
        self.sent_messages: list[str] = []
        self.allowed_repeat_keys = {conversation_message_key(message) for message in allowed_repeats or ()}

        planned_keys = [conversation_message_key(message) for message in planned_messages]
        farewell_key = conversation_message_key(farewell)
        seen: set[str] = set()
        for key in [*planned_keys, farewell_key]:
            if key in seen and key not in self.allowed_repeat_keys:
                raise ValueError("conversation plan contains an unapproved repeated input")
            seen.add(key)

    @property
    def turn_count(self) -> int:
        """Return how many messages the planner has issued."""
        result = len(self.sent_messages)
        return result

    @property
    def remaining_turns(self) -> int:
        """Return the unissued portion of the fixed turn budget."""
        result = self.total_turns - self.turn_count
        return result

    def next_message(self, adaptive_message: str = "") -> str:
        """Issue the next unique message while preserving plan and farewell."""
        remaining = self.remaining_turns
        if remaining <= 0:
            raise StopIteration

        if remaining == 1:
            if self.internal_planned:
                raise RuntimeError("planned messages remain at the reserved farewell turn")
            candidate = self.internal_farewell
        elif adaptive_message and remaining > len(self.internal_planned) + 1:
            candidate = adaptive_message
        elif self.internal_planned:
            candidate = self.internal_planned.popleft()
        else:
            raise RuntimeError("conversation plan exhausted before the reserved farewell")

        key = conversation_message_key(candidate)
        if key in self.sent_keys and key not in self.allowed_repeat_keys:
            raise ValueError("conversation driver attempted an unapproved repeated input")
        self.sent_keys.add(key)
        self.sent_messages.append(candidate)
        return candidate


class ConversationRuntime:
    """One process-local Engram conversation with observable turn diagnostics."""

    def __init__(
        self,
        engram,
        user_id: str = "0",
        anonymous_session_id: str = "",
        initial_bot_text: str = "",
        random_seed: int = 0,
        random_seed_present: bool = False,
    ) -> None:
        normalized_user_id = sessions.normalize_user_id(user_id)
        if not isinstance(anonymous_session_id, str):
            raise ValueError("anonymous_session_id must be a string")
        if anonymous_session_id and user_id != "":
            raise ValueError("anonymous_session_id requires an empty user_id")
        if not isinstance(initial_bot_text, str):
            raise ValueError("initial_bot_text must be a string")
        try:
            initial_bot_text_bytes = len(initial_bot_text.encode("utf-8"))
        except UnicodeEncodeError as error:
            raise ValueError("initial_bot_text must contain valid Unicode") from error
        if initial_bot_text_bytes > MAX_RESPONSE_BYTES:
            raise ValueError(f"initial_bot_text exceeds the UTF-8 limit of {MAX_RESPONSE_BYTES} bytes")
        if not isinstance(random_seed, int) or isinstance(random_seed, bool):
            raise ValueError("random_seed must be an integer")
        if not isinstance(random_seed_present, bool):
            raise ValueError("random_seed_present must be a boolean")

        self.engram = engram
        self.user_id = user_id if user_id == "" else normalized_user_id
        self.session_id = anonymous_session_id or normalized_user_id
        self.initial_bot_text = initial_bot_text
        self.random_seed = random_seed
        self.random_seed_present = random_seed_present or bool(random_seed)
        self.started_at = utc_now()
        self.turns: list[dict] = []
        self.lock = threading_RLock()

        sessions.get_session(engram, self.session_id, create_if_missing=True)
        if initial_bot_text:
            sessions.update_session_context(engram, self.session_id, initial_bot_text)
        self.metrics_baseline = metrics.get_metrics(engram)

    def send(self, text: object) -> dict:
        """Submit exactly one message and return the complete observable turn."""
        if not isinstance(text, str) or not text.strip():
            raise ValueError("text must be one non-empty string")
        try:
            text_bytes = len(text.encode("utf-8"))
        except UnicodeEncodeError as err:
            raise ValueError("text must contain valid Unicode") from err
        if text_bytes > MAX_REQUEST_BYTES:
            raise ValueError(f"text exceeds the UTF-8 limit of {MAX_REQUEST_BYTES} bytes")

        with self.lock:
            session = self.engram.sessions.get(self.session_id, {})
            predicates_before = dict(session.get("predicates", {}))
            previous_response_before = session.get("previous_response", "")
            dynamic_ids_before = {
                statement.get("id", "")
                for statement in self.engram.statements
                if statement.get("tier", Tier.STATIC) == Tier.DYNAMIC
            }

            turn_number = len(self.turns) + 1
            random_state = ()
            if self.random_seed_present:
                random_state = random_getstate()
                random_seed(self.random_seed + turn_number)
            started = time_perf_counter()
            try:
                result = pipeline.respond(
                    self.engram,
                    text,
                    context_id=self.session_id,
                    user_id=self.user_id,
                )
            finally:
                if random_state:
                    random_setstate(random_state)
            elapsed = time_perf_counter() - started

            session = self.engram.sessions.get(self.session_id, {})
            learned = [
                statement_view(statement)
                for statement in self.engram.statements
                if statement.get("tier", Tier.STATIC) == Tier.DYNAMIC and statement.get("id", "") not in dynamic_ids_before
            ]
            event = {
                "turn": turn_number,
                "input": text,
                "response": result.get("response", ""),
                "user_id": self.user_id,
                "source": result.get("source", ""),
                "score": round(result.get("score", 0.0), 3),
                "pattern": result.get("pattern", ""),
                "captured": result.get("captured", []),
                "dialogue_act": result.get("dialogue_act", ""),
                "active_topic": result.get("active_topic", ""),
                "entities": result.get("entities", []),
                "fact_admissions": result.get("fact_admissions", []),
                "elapsed_seconds": round(elapsed, 3),
                "context_changes": {
                    "previous_response": {
                        "before": previous_response_before,
                        "after": session.get("previous_response", ""),
                    },
                    "predicates": predicate_changes(predicates_before, session.get("predicates", {})),
                },
                "learned_statements": learned,
            }
            self.turns.append(event)
            return event

    def inspect(self) -> dict:
        """Return conversation context, learned knowledge, and current metrics."""
        with self.lock:
            learned = [
                statement_view(statement)
                for statement in self.engram.statements
                if statement.get("tier", Tier.STATIC) == Tier.DYNAMIC
            ]
            result = {
                "user_id": self.user_id,
                "turn_count": len(self.turns),
                "initial_bot_text": self.initial_bot_text,
                "session": session_view(self.engram.sessions.get(self.session_id, {})),
                "metrics": metrics.get_metrics(self.engram),
                "learned_dynamic": learned,
                "learned_unique_texts": sorted({statement.get("text", "") for statement in learned}),
                "latest_turn": self.turns[-1] if self.turns else {},
            }
            return result

    def report(self) -> dict:
        """Build the complete machine-readable conversation report."""
        with self.lock:
            snapshot = self.inspect()
            sources = Counter(turn.get("source", "") for turn in self.turns)
            result = {
                "report_version": CONVERSATION_REPORT_VERSION,
                "started_at": self.started_at,
                "finished_at": utc_now(),
                "user_id": self.user_id,
                "initial_bot_text": self.initial_bot_text,
                "random_seed": self.random_seed,
                "random_seed_present": self.random_seed_present,
                "summary": {
                    "exchanges": len(self.turns),
                    "sources": dict(sources),
                    "catch_all_turns": sum(turn.get("pattern", "") == "*" for turn in self.turns),
                    "learned_statements": len(snapshot.get("learned_dynamic", [])),
                    "learned_unique_texts": len(snapshot.get("learned_unique_texts", [])),
                },
                "metrics_baseline": self.metrics_baseline,
                "metrics_final": snapshot.get("metrics", {}),
                "session": snapshot.get("session", {}),
                "learned_dynamic": snapshot.get("learned_dynamic", []),
                "turns": list(self.turns),
            }
            return result
