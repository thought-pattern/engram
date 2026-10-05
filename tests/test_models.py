"""Tests for data models."""

from datetime import datetime

from engram.constants import EARLIEST_UTC, Tier
from engram.models import (
    keyword_entry_hit_rate,
    session,
    session_update_context,
    statement,
)

"""Tests for Statement model."""


def test_statement_create_dynamic() -> None:
    stmt = statement("Hello world")
    assert stmt.get("text", "") == "Hello world"
    assert stmt.get("tier", Tier.STATIC) == Tier.DYNAMIC
    assert stmt.get("id", "").startswith("stmt_")
    assert "created_at" in stmt
    assert isinstance(stmt.get("created_at", EARLIEST_UTC), datetime)


def test_statement_unattributed_statement_defaults() -> None:
    stmt = statement("Shared fact")

    assert {"introduced_by_user_id", "source_label", "pattern_aliases"} <= stmt.keys()
    assert stmt.get("introduced_by_user_id", "") == ""
    assert stmt.get("source_label", "") == ""
    assert stmt.get("pattern_aliases", []) == []


"""Tests for KeywordEntry model."""


def test_keyword_entry_hit_rate_calculation() -> None:
    entry = {"keyword": "test", "statement_ids": set(), "query_count": 100, "hit_count": 80}
    assert keyword_entry_hit_rate(entry) == 0.8


def test_keyword_entry_hit_rate_zero_hits() -> None:
    entry = {"keyword": "test", "statement_ids": set(), "query_count": 50, "hit_count": 0}
    assert keyword_entry_hit_rate(entry) == 0.0


"""Tests for Session model."""


def test_session_create() -> None:
    sess = session()
    assert sess.get("session_id", "").startswith("sess_")
    assert "previous_response" in sess
    assert sess.get("previous_response", "") == ""
    assert {"created_at", "last_active"} <= sess.keys()
    created_at = sess.get("created_at", EARLIEST_UTC)
    assert isinstance(created_at, datetime)
    assert sess.get("last_active", EARLIEST_UTC) == created_at


def test_session_update_context() -> None:
    sess = session()

    session_update_context(sess, "Paris is the capital of France")
    assert sess.get("previous_response", "") == "Paris is the capital of France"
    assert sess.get("response_history", []) == ["Paris is the capital of France"]
