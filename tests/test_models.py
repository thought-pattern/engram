"""Tests for data models."""

from datetime import datetime

from engram.constants import Tier
from engram.models import (
    keyword_entry,
    keyword_entry_from_dict,
    keyword_entry_hit_rate,
    keyword_entry_to_dict,
    session,
    session_from_dict,
    session_to_dict,
    session_update_context,
    statement,
    statement_from_dict,
    statement_to_dict,
)

"""Tests for Statement model."""


def test_statement_create_dynamic() -> None:
    stmt = statement("Hello world")
    assert stmt["text"] == "Hello world"
    assert stmt["tier"] == Tier.DYNAMIC
    assert stmt["id"].startswith("stmt_")
    assert isinstance(stmt["created_at"], datetime)


def test_statement_serialization() -> None:
    stmt = statement(
        "Test statement",
        keywords=["test"],
        pattern="TEST",
        pattern_aliases=["WHAT IS TEST"],
        introduced_by_user_id="alice",
        source_label="conversation",
    )
    data = statement_to_dict(stmt)

    assert data["text"] == "Test statement"
    assert data["tier"] == "DYNAMIC"
    assert "created_at" in data

    restored = statement_from_dict(data)
    assert restored["id"] == stmt["id"]
    assert restored["text"] == stmt["text"]
    assert restored["tier"] == stmt["tier"]
    assert restored["pattern_aliases"] == ["WHAT IS TEST"]
    assert restored["introduced_by_user_id"] == "alice"
    assert restored["source_label"] == "conversation"


def test_statement_unattributed_statement_defaults() -> None:
    stmt = statement("Shared fact")

    assert stmt["introduced_by_user_id"] == ""
    assert stmt["source_label"] == ""
    assert stmt["pattern_aliases"] == []


"""Tests for KeywordEntry model."""


def test_keyword_entry_hit_rate_calculation() -> None:
    entry = keyword_entry(keyword="test", query_count=100, hit_count=80)
    assert keyword_entry_hit_rate(entry) == 0.8


def test_keyword_entry_hit_rate_zero_hits() -> None:
    entry = keyword_entry(keyword="test", query_count=50, hit_count=0)
    assert keyword_entry_hit_rate(entry) == 0.0


def test_keyword_entry_serialization() -> None:
    entry = keyword_entry(
        keyword="paris",
        statement_ids={"stmt_1"},
        query_count=150,
        hit_count=142,
    )
    data = keyword_entry_to_dict(entry)

    restored = keyword_entry_from_dict("paris", data)
    assert restored["keyword"] == "paris"
    assert restored["statement_ids"] == {"stmt_1"}
    assert restored["query_count"] == 150
    assert restored["hit_count"] == 142


"""Tests for Session model."""


def test_session_create() -> None:
    sess = session()
    assert sess["session_id"].startswith("sess_")
    assert sess["previous_response"] == ""
    assert isinstance(sess["created_at"], datetime)
    assert sess["last_active"] == sess["created_at"]


def test_session_update_context() -> None:
    sess = session()

    session_update_context(sess, "Paris is the capital of France")
    assert sess["previous_response"] == "Paris is the capital of France"
    assert sess["response_history"] == ["Paris is the capital of France"]


def test_session_serialization() -> None:
    sess = session(session_id="test", metadata={"key": "value"})
    session_update_context(sess, "Previous response")

    data = session_to_dict(sess)
    restored = session_from_dict(data)

    assert restored["session_id"] == "test"
    assert restored["previous_response"] == "Previous response"
    assert restored["metadata"]["key"] == "value"
