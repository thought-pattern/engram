"""Persistent conversation orchestration shared by human and agent adapters.

The existing :class:`engram.core.Engram` and :func:`engram.pipeline.chat`
interfaces remain the canonical programmatic API.  ``ConversationRuntime`` is
an additive orchestration layer for clients that also need a transcript,
turn-level diagnostics, inspection, and report generation.
"""

from collections import Counter, deque
from contextlib import suppress as contextlib_suppress
from datetime import UTC, datetime
from json import dumps as json_dumps
from os import replace as os_replace
from pathlib import Path
from random import getstate as random_getstate, seed as random_seed_2, setstate as random_setstate
from threading import RLock as threading_RLock
from time import perf_counter as time_perf_counter

from engram import metrics, pipeline, sessions
from engram.constants import Tier
from engram.text import normalize

_DEFAULT_ARGUMENT_LIST = []

CONVERSATION_REPORT_VERSION = 1


def _utc_now() -> str:
    _return_value = datetime.now(UTC).isoformat()
    return _return_value


def _atomic_write_json(path: Path, data: dict) -> bool:
    path.parent.mkdir(parents=True, exist_ok=True)
    with contextlib_suppress(OSError):
        path.parent.chmod(0o700)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json_dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    with contextlib_suppress(OSError):
        temporary.chmod(0o600)
    os_replace(temporary, path)
    with contextlib_suppress(OSError):
        path.chmod(0o600)
    return False


def statement_view(statement: dict) -> dict:
    """Return the statement fields useful to conversation adapters."""
    _return_value = {
        "id": statement.get("id", ""),
        "text": statement.get("text", ""),
        "pattern": statement.get("pattern", ""),
        "pattern_aliases": list(statement.get("pattern_aliases", [])),
        "introduced_by_user_id": statement.get("introduced_by_user_id", ""),
        "source_label": statement.get("source_label", ""),
    }
    return _return_value


def session_view(session: dict) -> dict:
    """Return a JSON-ready snapshot of one user conversation context."""
    _return_value = {
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
    return _return_value


def _predicate_changes(before: dict, after: dict) -> dict:
    changes = {}
    for name in sorted(set(before) | set(after)):
        old_value = before.get(name, False)
        new_value = after.get(name, False)
        if old_value != new_value:
            changes[name] = {"before": old_value, "after": new_value}
    return changes


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
        allowed_repeats: list[str] = _DEFAULT_ARGUMENT_LIST,
    ) -> None:
        if allowed_repeats is _DEFAULT_ARGUMENT_LIST:
            allowed_repeats = _DEFAULT_ARGUMENT_LIST.copy()
        if not isinstance(total_turns, int) or total_turns < 1:
            raise ValueError("total_turns must be a positive integer")
        if len(planned_messages) > total_turns - 1:
            raise ValueError("planned messages must leave the final turn for the farewell")

        self.total_turns = total_turns
        self._planned = deque(planned_messages)
        self._farewell = farewell
        self._sent_keys: set[str] = set()
        self._sent_messages: list[str] = []
        self._allowed_repeat_keys = {self._message_key(message) for message in allowed_repeats or []}

        planned_keys = [self._message_key(message) for message in planned_messages]
        farewell_key = self._message_key(farewell)
        seen: set[str] = set()
        for key in [*planned_keys, farewell_key]:
            if key in seen and key not in self._allowed_repeat_keys:
                raise ValueError("conversation plan contains an unapproved repeated input")
            seen.add(key)

    @staticmethod
    def _message_key(message: str) -> str:
        if not isinstance(message, str) or not message.strip():
            raise ValueError("conversation messages must be non-empty strings")
        key = normalize(message)
        if not key:
            raise ValueError("conversation messages must contain meaningful text")
        return key

    @property
    def turn_count(self) -> int:
        """Return how many messages the planner has issued."""
        _return_value = len(self._sent_messages)
        return _return_value

    @property
    def remaining_turns(self) -> int:
        """Return the unissued portion of the fixed turn budget."""
        _return_value = self.total_turns - self.turn_count
        return _return_value

    def next_message(self, adaptive_message: str = "") -> str:
        """Issue the next unique message while preserving plan and farewell."""
        if adaptive_message is None:
            adaptive_message = ""
        remaining = self.remaining_turns
        if remaining <= 0:
            raise StopIteration

        if remaining == 1:
            if self._planned:
                raise RuntimeError("planned messages remain at the reserved farewell turn")
            candidate = self._farewell
        elif adaptive_message != "" and remaining > len(self._planned) + 1:
            candidate = adaptive_message
        elif self._planned:
            candidate = self._planned.popleft()
        else:
            raise RuntimeError("conversation plan exhausted before the reserved farewell")

        key = self._message_key(candidate)
        if key in self._sent_keys and key not in self._allowed_repeat_keys:
            raise ValueError("conversation driver attempted an unapproved repeated input")
        self._sent_keys.add(key)
        self._sent_messages.append(candidate)
        return candidate


class ConversationRuntime:
    """One persistent Engram conversation with observable turn diagnostics."""

    def __init__(
        self,
        engram,
        user_id: str = "0",
        initial_bot_text: str = "",
        random_seed: int = None,
        transcript_path: object = None,
    ) -> None:
        if not isinstance(initial_bot_text, str):
            raise ValueError("initial_bot_text must be a string")
        if random_seed is not None and not isinstance(random_seed, int):
            raise ValueError("random_seed must be an integer or None")

        self.engram = engram
        self.user_id = sessions.normalize_user_id(user_id)
        self.initial_bot_text = initial_bot_text
        self.random_seed = random_seed
        self.transcript_path = Path(transcript_path) if transcript_path else None
        self.started_at = _utc_now()
        self.turns: list[dict] = []
        self.lock = threading_RLock()

        sessions.get_session(engram, self.user_id, create_if_missing=True)
        if initial_bot_text:
            sessions.update_session_context(engram, self.user_id, initial_bot_text)
        self.metrics_baseline = metrics.get_metrics(engram)
        self._persist()

    def send(self, text: str) -> dict:
        """Submit exactly one message and return the complete observable turn."""
        if not isinstance(text, str) or not text.strip():
            raise ValueError("text must be one non-empty string")

        with self.lock:
            session = self.engram.sessions.get(self.user_id, False)
            predicates_before = dict(session.get("predicates", {}))
            previous_response_before = session.get("previous_response", "")
            dynamic_ids_before = {
                statement.get("id", "") for statement in self.engram.statements if statement.get("tier", "") == Tier.DYNAMIC
            }

            turn_number = len(self.turns) + 1
            random_state = None
            if self.random_seed is not None:
                random_state = random_getstate()
                random_seed_2(self.random_seed + turn_number)
            started = time_perf_counter()
            try:
                result = pipeline.chat(self.engram, text, user_id=self.user_id)
            finally:
                if random_state is not None:
                    random_setstate(random_state)
            elapsed = time_perf_counter() - started

            session = self.engram.sessions.get(self.user_id, False)
            learned = [
                statement_view(statement)
                for statement in self.engram.statements
                if statement.get("tier", "") == Tier.DYNAMIC and statement.get("id", "") not in dynamic_ids_before
            ]
            event = {
                "turn": turn_number,
                "input": text,
                "response": result.get("response", ""),
                "user_id": result.get("user_id", ""),
                "source": result.get("source", ""),
                "score": round(result.get("score", 0.0), 3),
                "pattern": result.get("pattern", ""),
                "captured": result.get("captured", False),
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
                    "predicates": _predicate_changes(predicates_before, session.get("predicates", [])),
                },
                "learned_statements": learned,
            }
            self.turns.append(event)
            self._persist()
            return event
        return {}

    def inspect(self) -> dict:
        """Return conversation context, learned knowledge, and current metrics."""
        with self.lock:
            learned = [
                statement_view(statement) for statement in self.engram.statements if statement.get("tier", "") == Tier.DYNAMIC
            ]
            _return_value = {
                "user_id": self.user_id,
                "turn_count": len(self.turns),
                "initial_bot_text": self.initial_bot_text,
                "session": session_view(self.engram.sessions.get(self.user_id, False)),
                "metrics": metrics.get_metrics(self.engram),
                "learned_dynamic": learned,
                "learned_unique_texts": sorted({statement.get("text", "") for statement in learned}),
                "latest_turn": self.turns[-1] if self.turns else False,
            }
            return _return_value
        return {}

    def report(self) -> dict:
        """Build the complete machine-readable conversation report."""
        with self.lock:
            snapshot = self.inspect()
            sources = Counter(turn.get("source", "") for turn in self.turns)
            _return_value = {
                "report_version": CONVERSATION_REPORT_VERSION,
                "started_at": self.started_at,
                "finished_at": _utc_now(),
                "user_id": self.user_id,
                "initial_bot_text": self.initial_bot_text,
                "random_seed": self.random_seed,
                "summary": {
                    "exchanges": len(self.turns),
                    "sources": dict(sources),
                    "catch_all_turns": sum(turn.get("pattern", "") == "*" for turn in self.turns),
                    "learned_statements": len(snapshot.get("learned_dynamic", [])),
                    "learned_unique_texts": len(snapshot.get("learned_unique_texts", [])),
                },
                "metrics_baseline": self.metrics_baseline,
                "metrics_final": snapshot.get("metrics", []),
                "session": snapshot.get("session", False),
                "learned_dynamic": snapshot.get("learned_dynamic", False),
                "turns": list(self.turns),
            }
            return _return_value
        return {}

    def write_report(self, output_prefix: object) -> dict:
        """Write JSON and Markdown reports and return their paths and summary."""
        prefix = Path(output_prefix).resolve()
        json_path = prefix.with_suffix(".json")
        markdown_path = prefix.with_suffix(".md")
        report = self.report()
        _atomic_write_json(json_path, report)
        markdown_path.parent.mkdir(parents=True, exist_ok=True)
        with contextlib_suppress(OSError):
            markdown_path.parent.chmod(0o700)
        markdown_path.write_text(render_markdown(report), encoding="utf-8")
        with contextlib_suppress(OSError):
            markdown_path.chmod(0o600)
        _return_value = {
            "summary": report.get("summary", False),
            "json": str(json_path),
            "markdown": str(markdown_path),
        }
        return _return_value

    def _persist(self) -> bool:
        if self.transcript_path == "":
            return False
        _atomic_write_json(
            self.transcript_path,
            {
                "report_version": CONVERSATION_REPORT_VERSION,
                "started_at": self.started_at,
                "user_id": self.user_id,
                "initial_bot_text": self.initial_bot_text,
                "random_seed": self.random_seed,
                "metrics_baseline": self.metrics_baseline,
                "turns": self.turns,
            },
        )
        return False


def render_markdown(report: dict) -> str:
    """Render a conversation report as readable Markdown."""
    lines = [
        "# Engram Conversation",
        "",
        f"User context: `{report.get('user_id', '')}`  ",
        f"Initial Engram utterance: `{report.get('initial_bot_text', '')}`  ",
        f"Exchanges: `{report.get('summary', {}).get('exchanges', [])}`",
        "",
    ]
    for turn in report.get("turns", []):
        lines.extend(
            [
                f"## Exchange {turn.get('turn', False)}",
                "",
                f"**Interlocutor:** {turn.get('input', '')}",
                "",
                f"**Engram:** {turn.get('response', '') or '[no response]'}",
                "",
                (
                    f"_source={turn.get('source', '')}; pattern={turn.get('pattern', '') or '[none]'}; "
                    f"score={turn.get('score', 0.0):.3f}; seconds={turn.get('elapsed_seconds', 0.0):.3f}_"
                ),
                "",
            ]
        )
    _return_value = "\n".join(lines)
    return _return_value
