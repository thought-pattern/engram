"""Persistent conversation orchestration shared by human and agent adapters.

The existing :class:`engram.core.Engram` and :func:`engram.pipeline.chat`
interfaces remain the canonical programmatic API.  ``ConversationRuntime`` is
an additive orchestration layer for clients that also need a transcript,
turn-level diagnostics, inspection, and report generation.
"""

import json
import os
import random
import threading
import time
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

from engram import metrics, pipeline, sessions
from engram.constants import Tier

CONVERSATION_REPORT_VERSION = 1


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _atomic_write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def statement_view(statement: dict) -> dict:
    """Return the statement fields useful to conversation adapters."""
    return {
        "id": statement["id"],
        "text": statement["text"],
        "pattern": statement["pattern"],
        "pattern_aliases": list(statement.get("pattern_aliases", [])),
        "introduced_by_user_id": statement.get("introduced_by_user_id"),
        "source_label": statement.get("source_label", ""),
    }


def session_view(session: dict) -> dict:
    """Return a JSON-ready snapshot of one user conversation context."""
    return {
        "session_id": session["session_id"],
        "previous_response": session["previous_response"],
        "predicates": dict(session["predicates"]),
        "input_history": list(session["input_history"]),
        "response_history": list(session["response_history"]),
        "history_size": session["history_size"],
    }


def _predicate_changes(before: dict, after: dict) -> dict:
    changes = {}
    for name in sorted(set(before) | set(after)):
        old_value = before.get(name)
        new_value = after.get(name)
        if old_value != new_value:
            changes[name] = {"before": old_value, "after": new_value}
    return changes


class ConversationRuntime:
    """One persistent Engram conversation with observable turn diagnostics."""

    def __init__(
        self,
        engram,
        user_id: str = "0",
        initial_bot_text: str = "",
        random_seed: int | None = None,
        transcript_path: str | Path | None = None,
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
            random_state = None
            if self.random_seed is not None:
                random_state = random.getstate()
                random.seed(self.random_seed + turn_number)
            started = time.perf_counter()
            try:
                result = pipeline.chat(self.engram, text, user_id=self.user_id)
            finally:
                if random_state is not None:
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
                "latest_turn": self.turns[-1] if self.turns else None,
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

    def write_report(self, output_prefix: str | Path) -> dict:
        """Write JSON and Markdown reports and return their paths and summary."""
        prefix = Path(output_prefix).resolve()
        json_path = prefix.with_suffix(".json")
        markdown_path = prefix.with_suffix(".md")
        report = self.report()
        _atomic_write_json(json_path, report)
        markdown_path.parent.mkdir(parents=True, exist_ok=True)
        markdown_path.write_text(render_markdown(report), encoding="utf-8")
        return {
            "summary": report["summary"],
            "json": str(json_path),
            "markdown": str(markdown_path),
        }

    def _persist(self) -> None:
        if self.transcript_path is None:
            return
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
