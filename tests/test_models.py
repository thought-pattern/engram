"""Tests for data models."""

from datetime import UTC, datetime, timedelta

from engram.constants import EARLIEST_UTC
from engram.constants_artifacts import Tier
from engram.models import (
    keyword_entry_hit_rate,
    record_statement_hit,
    session,
    session_update_context,
    statement,
)

NOW = datetime(2026, 8, 12, 16, 0, tzinfo=UTC)

"""Tests for Statement model."""


def test_statement_create_dynamic() -> None:
    stmt = statement("Hello world", statement_id="stmt_hello", created_at=NOW)
    assert stmt.get("text", "") == "Hello world"
    assert stmt.get("tier", Tier.STATIC) == Tier.DYNAMIC
    assert stmt.get("id", "") == "stmt_hello"
    assert stmt.get("created_at", EARLIEST_UTC) == NOW
    assert stmt.get("last_hit_available", True) is False


def test_statement_hit_records_the_callers_moment() -> None:
    stmt = statement("Hello world", statement_id="stmt_hello", created_at=NOW)
    hit_moment = NOW + timedelta(minutes=5)

    record_statement_hit(stmt, hit_moment)

    assert stmt.get("hit_count", 0) == 1
    assert stmt.get("last_hit", EARLIEST_UTC) == hit_moment
    assert stmt.get("last_hit_available", False) is True


def test_statement_unattributed_statement_defaults() -> None:
    stmt = statement("Shared fact", statement_id="stmt_shared", created_at=NOW)

    assert {"introduced_by_user_id", "source_label", "pattern_aliases"} <= stmt.keys()
    assert stmt.get("introduced_by_user_id", "") == ""
    assert stmt.get("source_label", "") == ""
    assert stmt.get("pattern_aliases", []) == []


# Tests for KeywordEntry model.


def test_keyword_entry_hit_rate_calculation() -> None:
    entry = {"keyword": "test", "statement_ids": set(), "query_count": 100, "hit_count": 80}
    assert keyword_entry_hit_rate(entry) == 0.8


def test_keyword_entry_hit_rate_zero_hits() -> None:
    entry = {"keyword": "test", "statement_ids": set(), "query_count": 50, "hit_count": 0}
    assert keyword_entry_hit_rate(entry) == 0.0


# Tests for Session model.


def test_session_create() -> None:
    sess = session("sess_create", NOW)
    assert sess.get("session_id", "") == "sess_create"
    assert "previous_response" in sess
    assert sess.get("previous_response", "") == ""
    assert sess.get("created_at", EARLIEST_UTC) == NOW
    assert sess.get("last_active", EARLIEST_UTC) == NOW


def test_session_update_context() -> None:
    sess = session("sess_update", NOW)
    turn_moment = NOW + timedelta(seconds=30)

    session_update_context(sess, "Paris is the capital of France", moment=turn_moment)
    assert sess.get("last_active", EARLIEST_UTC) == turn_moment
    assert sess.get("previous_response", "") == "Paris is the capital of France"
    assert sess.get("response_history", []) == ["Paris is the capital of France"]
