"""Process-local conversation orchestration shared by human and agent adapters.

``ConversationRuntime`` owns turn-level diagnostics, inspection, and report
generation without writing conversation state to disk.
"""

from bisect import bisect_left
from collections import Counter
from copy import deepcopy
from datetime import UTC, datetime
from random import Random
from threading import RLock as threading_RLock
from time import perf_counter as time_perf_counter

from engram import metrics, pipeline, sessions
from engram.constants import CONVERSATION_REPORT_VERSION, DEFAULT_USER_ID, MAX_REQUEST_BYTES, MAX_RESPONSE_BYTES, Tier
from engram.errors import InvalidRequestError
from engram.feedback import canonical_utc
from engram.template import TEMPLATE_RANDOM


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


class ConversationRuntime:
    """One process-local Engram conversation with observable turn diagnostics.

    Constructing a runtime for ``DEFAULT_USER_ID`` deletes that user's existing
    session so the anonymous conversation starts clean; if construction fails,
    the prior session is restored.
    """

    def __init__(
        self,
        engram,
        user_id: str = DEFAULT_USER_ID,
        initial_bot_text: str = "",
        random_seed: int = 0,
        random_seed_present: bool = False,
        clock=(),
    ) -> None:
        normalized_user_id = sessions.normalize_user_id(user_id)
        if not isinstance(initial_bot_text, str):
            raise InvalidRequestError("initial_bot_text must be a string")
        try:
            initial_bot_text_bytes = len(initial_bot_text.encode("utf-8"))
        except UnicodeEncodeError as err:
            raise InvalidRequestError("initial_bot_text must contain valid Unicode") from err
        if initial_bot_text_bytes > MAX_RESPONSE_BYTES:
            raise InvalidRequestError(f"initial_bot_text exceeds the UTF-8 limit of {MAX_RESPONSE_BYTES} bytes")
        if not isinstance(random_seed, int) or isinstance(random_seed, bool):
            raise InvalidRequestError("random_seed must be an integer")
        if not isinstance(random_seed_present, bool):
            raise InvalidRequestError("random_seed_present must be a boolean")
        if clock != () and not callable(clock):
            raise InvalidRequestError("clock must be callable")

        self.engram = engram
        self.user_id = normalized_user_id
        self.session_id = normalized_user_id
        self.initial_bot_text = initial_bot_text
        self.random_seed = random_seed
        self.random_seed_present = random_seed_present or bool(random_seed)
        self.clock = clock
        self.started_at = utc_now()
        # Past turns are not kept: responses read the session's bounded
        # history, not a transcript. The report summary uses running counts,
        # and inspection shows only the latest turn.
        self.turn_count = 0
        self.latest_turn: dict = {}
        self.source_counts: Counter[str] = Counter()
        self.catch_all_turns = 0
        self.lock = threading_RLock()

        with engram.session_lock:
            prior_session = sessions.get_session(engram, self.session_id, create_if_missing=False)
            if self.user_id == DEFAULT_USER_ID:
                sessions.delete_session(engram, self.session_id)
            try:
                sessions.get_session(engram, self.session_id, create_if_missing=True)
                if initial_bot_text:
                    sessions.update_session_context(engram, self.session_id, initial_bot_text)
                self.metrics_baseline = metrics.get_metrics(engram)
            except BaseException:
                if self.user_id == DEFAULT_USER_ID:
                    sessions.delete_session(engram, self.session_id)
                    if prior_session:
                        sessions.restore_session(engram, prior_session)
                raise

    def send(self, text: object) -> dict:
        """Submit exactly one message and return the complete observable turn."""
        if not isinstance(text, str) or not text.strip():
            raise InvalidRequestError("text must be one non-empty string")
        try:
            text_bytes = len(text.encode("utf-8"))
        except UnicodeEncodeError as err:
            raise InvalidRequestError("text must contain valid Unicode") from err
        if text_bytes > MAX_REQUEST_BYTES:
            raise InvalidRequestError(f"text exceeds the UTF-8 limit of {MAX_REQUEST_BYTES} bytes")

        with self.lock:
            with self.engram.session_lock:
                session = sessions.get_session(self.engram, self.session_id, create_if_missing=False)
                predicates_before = dict(session.get("predicates", {}))
                previous_response_before = session.get("previous_response", "")
            # Statements stored during this turn have a sequence at or after this one.
            first_new_sequence = self.engram.next_statement_sequence

            turn_number = self.turn_count + 1
            # A seeded turn draws from its own generator, so other threads keep
            # the shared one. Seeding by turn keeps a replay deterministic.
            random_token = ()
            if self.random_seed_present:
                random_token = TEMPLATE_RANDOM.set(Random(self.random_seed + turn_number))
            started = time_perf_counter()
            try:
                evaluation_clock = self.clock() if callable(self.clock) else datetime.now(UTC)
                if not isinstance(evaluation_clock, datetime):
                    raise ValueError("clock must return a datetime")
                result = pipeline.respond(
                    self.engram,
                    text,
                    context_id=self.session_id,
                    user_id=self.user_id,
                    evaluation_time=canonical_utc(evaluation_clock),
                )
            finally:
                if random_token:
                    TEMPLATE_RANDOM.reset(random_token)
            elapsed = time_perf_counter() - started

            with self.engram.session_lock:
                session = sessions.get_session(self.engram, self.session_id, create_if_missing=False)
                previous_response_after = session.get("previous_response", "")
                predicates_after = dict(session.get("predicates", {}))
            with self.engram.statement_lock:
                first_new = bisect_left(self.engram.statement_sequences, first_new_sequence)
                learned = [
                    statement_view(statement)
                    for statement in self.engram.statements[first_new:]
                    if statement.get("tier", Tier.STATIC) == Tier.DYNAMIC
                ]
            source = result.get("source", "")
            pattern = result.get("pattern", "")
            event = {
                "turn": turn_number,
                "input": text,
                "response": result.get("response", ""),
                "user_id": self.user_id,
                "source": source,
                "score": round(result.get("score", 0.0), 3),
                "pattern": pattern,
                "captured": result.get("captured", []),
                "dialogue_act": result.get("dialogue_act", ""),
                "active_topic": result.get("active_topic", ""),
                "entities": result.get("entities", []),
                "fact_admissions": result.get("fact_admissions", []),
                "elapsed_seconds": round(elapsed, 3),
                "context_changes": {
                    "previous_response": {
                        "before": previous_response_before,
                        "after": previous_response_after,
                    },
                    "predicates": predicate_changes(predicates_before, predicates_after),
                },
                "learned_statements": learned,
            }
            self.turn_count = turn_number
            # The caller owns the returned event; diagnostics retain their own copy.
            self.latest_turn = deepcopy(event)
            self.source_counts[source] = self.source_counts.get(source, 0) + 1
            if pattern == "*":
                self.catch_all_turns += 1
            return event

    def inspect(self) -> dict:
        """Return conversation context, learned knowledge, and current metrics."""
        with self.lock, self.engram.statement_lock:
            learned = [
                statement_view(statement)
                for statement in self.engram.statements
                if statement.get("tier", Tier.STATIC) == Tier.DYNAMIC
            ]
            with self.engram.session_lock:
                current_session = session_view(sessions.get_session(self.engram, self.session_id, create_if_missing=False))
            result = {
                "user_id": self.user_id,
                "turn_count": self.turn_count,
                "initial_bot_text": self.initial_bot_text,
                "session": current_session,
                "metrics": metrics.get_metrics(self.engram),
                "learned_dynamic": learned,
                "learned_unique_texts": sorted({statement.get("text", "") for statement in learned}),
                "latest_turn": deepcopy(self.latest_turn),
            }
            return result

    def report(self) -> dict:
        """Build the complete machine-readable conversation report."""
        with self.lock:
            snapshot = self.inspect()
            result = {
                "report_version": CONVERSATION_REPORT_VERSION,
                "started_at": self.started_at,
                "finished_at": utc_now(),
                "user_id": self.user_id,
                "initial_bot_text": self.initial_bot_text,
                "random_seed": self.random_seed,
                "random_seed_present": self.random_seed_present,
                "summary": {
                    "exchanges": self.turn_count,
                    "sources": dict(self.source_counts),
                    "catch_all_turns": self.catch_all_turns,
                    "learned_statements": len(snapshot.get("learned_dynamic", [])),
                    "learned_unique_texts": len(snapshot.get("learned_unique_texts", [])),
                },
                "metrics_baseline": self.metrics_baseline,
                "metrics_final": snapshot.get("metrics", {}),
                "session": snapshot.get("session", {}),
                "learned_dynamic": snapshot.get("learned_dynamic", []),
            }
            return result
