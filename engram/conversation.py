"""Persistent conversation orchestration shared by human and agent adapters.

The existing :class:`engram.core.Engram` and :func:`engram.pipeline.chat`
interfaces remain the canonical programmatic API.  ``ConversationRuntime`` is
an additive orchestration layer for clients that also need a transcript,
turn-level diagnostics, inspection, and report generation.
"""

import contextlib
import json
import os
import random
import threading
import time
from collections import Counter, deque
from datetime import UTC, datetime
from pathlib import Path

from engram import metrics, pipeline, sessions
from engram.constants import Tier
from engram.text import normalize

CONVERSATION_REPORT_VERSION = 1


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _atomic_write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with contextlib.suppress(OSError):
        path.parent.chmod(0o700)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    with contextlib.suppress(OSError):
        temporary.chmod(0o600)
    os.replace(temporary, path)
    with contextlib.suppress(OSError):
        path.chmod(0o600)


def statement_view(statement: dict) -> dict:
    """Return the statement fields useful to conversation adapters."""
    return {
        "id": statement["id"],
        "text": statement["text"],
        "pattern": statement["pattern"],
        "pattern_aliases": list(statement.get("pattern_aliases", [])),
        "introduced_by_user_id": statement.get("introduced_by_user_id") or "",
        "source_label": statement.get("source_label", ""),
    }


def session_view(session: dict) -> dict:
    """Return a JSON-ready snapshot of one user conversation context."""
    return {
        "session_id": session["session_id"],
        "previous_response": session["previous_response"],
        "predicates": dict(session["predicates"]),
        "active_topic": session.get("active_topic", ""),
        "entities": list(session.get("entities", [])),
        "dialogue_act_history": list(session.get("dialogue_act_history", [])),
        "last_fact_admissions": list(session.get("last_fact_admissions", [])),
        "input_history": list(session["input_history"]),
        "response_history": list(session["response_history"]),
        "history_size": session["history_size"],
    }


def _predicate_changes(before: dict, after: dict) -> dict:
    changes = {}
    for name in sorted(set(before) | set(after)):
        old_value = before.get(name, "")
        new_value = after.get(name, "")
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
        allowed_repeats=(),
    ) -> None:
        if not isinstance(total_turns, int) or total_turns < 1:
            raise ValueError("total_turns must be a positive integer")
        if len(planned_messages) > total_turns - 1:
            raise ValueError("planned messages must leave the final turn for the farewell")

        self.total_turns = total_turns
        self._planned = deque(planned_messages)
        self._farewell = farewell
        self._sent_keys: set[str] = set()
        self._sent_messages: list[str] = []
        self._allowed_repeat_keys = {self._message_key(message) for message in allowed_repeats or ()}

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
        return len(self._sent_messages)

    @property
    def remaining_turns(self) -> int:
        """Return the unissued portion of the fixed turn budget."""
        return self.total_turns - self.turn_count

    def next_message(self, adaptive_message: str = "") -> str:
        """Issue the next unique message while preserving plan and farewell."""
        remaining = self.remaining_turns
        if remaining <= 0:
            raise StopIteration

        if remaining == 1:
            if self._planned:
                raise RuntimeError("planned messages remain at the reserved farewell turn")
            candidate = self._farewell
        elif adaptive_message and remaining > len(self._planned) + 1:
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
        random_seed: int = 0,
        random_seed_present: bool = False,
        transcript_path: str = "",
    ) -> None:
        if not isinstance(initial_bot_text, str):
            raise ValueError("initial_bot_text must be a string")
        if not isinstance(random_seed, int) or isinstance(random_seed, bool):
            raise ValueError("random_seed must be an integer")
        if not isinstance(random_seed_present, bool):
            raise ValueError("random_seed_present must be a boolean")

        self.engram = engram
        self.user_id = sessions.normalize_user_id(user_id)
        self.initial_bot_text = initial_bot_text
        self.random_seed = random_seed
        self.random_seed_present = random_seed_present or bool(random_seed)
        self.transcript_path = str(Path(transcript_path)) if transcript_path else ""
        self.started_at = _utc_now()
        self.turns: list[dict] = []
        self.lock = threading.RLock()

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
            session = self.engram.sessions[self.user_id]
            predicates_before = dict(session["predicates"])
            previous_response_before = session["previous_response"]
            dynamic_ids_before = {statement["id"] for statement in self.engram.statements if statement["tier"] == Tier.DYNAMIC}

            turn_number = len(self.turns) + 1
            random_state = ()
            if self.random_seed_present:
                random_state = random.getstate()
                random.seed(self.random_seed + turn_number)
            started = time.perf_counter()
            try:
                result = pipeline.chat(self.engram, text, user_id=self.user_id)
            finally:
                if random_state:
                    random.setstate(random_state)
            elapsed = time.perf_counter() - started

            session = self.engram.sessions[self.user_id]
            learned = [
                statement_view(statement)
                for statement in self.engram.statements
                if statement["tier"] == Tier.DYNAMIC and statement["id"] not in dynamic_ids_before
            ]
            event = {
                "turn": turn_number,
                "input": text,
                "response": result["response"],
                "user_id": result["user_id"],
                "source": result["source"],
                "score": round(result["score"], 3),
                "pattern": result["pattern"],
                "captured": result["captured"],
                "dialogue_act": result.get("dialogue_act", ""),
                "active_topic": result.get("active_topic", ""),
                "entities": result.get("entities", []),
                "fact_admissions": result.get("fact_admissions", []),
                "elapsed_seconds": round(elapsed, 3),
                "context_changes": {
                    "previous_response": {
                        "before": previous_response_before,
                        "after": session["previous_response"],
                    },
                    "predicates": _predicate_changes(predicates_before, session["predicates"]),
                },
                "learned_statements": learned,
            }
            self.turns.append(event)
            self._persist()
            return event

    def inspect(self) -> dict:
        """Return conversation context, learned knowledge, and current metrics."""
        with self.lock:
            learned = [statement_view(statement) for statement in self.engram.statements if statement["tier"] == Tier.DYNAMIC]
            return {
                "user_id": self.user_id,
                "turn_count": len(self.turns),
                "initial_bot_text": self.initial_bot_text,
                "session": session_view(self.engram.sessions[self.user_id]),
                "metrics": metrics.get_metrics(self.engram),
                "learned_dynamic": learned,
                "learned_unique_texts": sorted({statement["text"] for statement in learned}),
                "latest_turn": self.turns[-1] if self.turns else {},
            }

    def report(self) -> dict:
        """Build the complete machine-readable conversation report."""
        with self.lock:
            snapshot = self.inspect()
            sources = Counter(turn["source"] for turn in self.turns)
            return {
                "report_version": CONVERSATION_REPORT_VERSION,
                "started_at": self.started_at,
                "finished_at": _utc_now(),
                "user_id": self.user_id,
                "initial_bot_text": self.initial_bot_text,
                "random_seed": self.random_seed,
                "random_seed_present": self.random_seed_present,
                "summary": {
                    "exchanges": len(self.turns),
                    "sources": dict(sources),
                    "catch_all_turns": sum(turn["pattern"] == "*" for turn in self.turns),
                    "learned_statements": len(snapshot["learned_dynamic"]),
                    "learned_unique_texts": len(snapshot["learned_unique_texts"]),
                },
                "metrics_baseline": self.metrics_baseline,
                "metrics_final": snapshot["metrics"],
                "session": snapshot["session"],
                "learned_dynamic": snapshot["learned_dynamic"],
                "turns": list(self.turns),
            }

    def write_report(self, output_prefix: str) -> dict:
        """Write JSON and Markdown reports and return their paths and summary."""
        prefix = Path(output_prefix).resolve()
        json_path = prefix.with_suffix(".json")
        markdown_path = prefix.with_suffix(".md")
        report = self.report()
        _atomic_write_json(json_path, report)
        markdown_path.parent.mkdir(parents=True, exist_ok=True)
        with contextlib.suppress(OSError):
            markdown_path.parent.chmod(0o700)
        markdown_path.write_text(render_markdown(report), encoding="utf-8")
        with contextlib.suppress(OSError):
            markdown_path.chmod(0o600)
        return {
            "summary": report["summary"],
            "json": str(json_path),
            "markdown": str(markdown_path),
        }

    def _persist(self) -> None:
        if not self.transcript_path:
            return
        _atomic_write_json(
            Path(self.transcript_path),
            {
                "report_version": CONVERSATION_REPORT_VERSION,
                "started_at": self.started_at,
                "user_id": self.user_id,
                "initial_bot_text": self.initial_bot_text,
                "random_seed": self.random_seed,
                "random_seed_present": self.random_seed_present,
                "metrics_baseline": self.metrics_baseline,
                "turns": self.turns,
            },
        )


def render_markdown(report: dict) -> str:
    """Render a conversation report as readable Markdown."""
    lines = [
        "# Engram Conversation",
        "",
        f"User context: `{report['user_id']}`  ",
        f"Initial Engram utterance: `{report['initial_bot_text']}`  ",
        f"Exchanges: `{report['summary']['exchanges']}`",
        "",
    ]
    for turn in report["turns"]:
        lines.extend(
            [
                f"## Exchange {turn['turn']}",
                "",
                f"**Interlocutor:** {turn['input']}",
                "",
                f"**Engram:** {turn['response'] or '[no response]'}",
                "",
                (
                    f"_source={turn['source']}; pattern={turn['pattern'] or '[none]'}; "
                    f"score={turn['score']:.3f}; seconds={turn['elapsed_seconds']:.3f}_"
                ),
                "",
            ]
        )
    return "\n".join(lines)
