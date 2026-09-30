"""Tests for data models."""

from datetime import datetime

from engram.constants import Tier
from engram.models import (
    keyword_entry,
    keyword_entry_hit_rate,
    session,
    session_update_context,
    statement,
)

"""Tests for Statement model."""


def test_statement_create_dynamic() -> None:
    stmt = statement("Hello world")
    assert stmt["text"] == "Hello world"
    assert stmt["tier"] == Tier.DYNAMIC
    assert stmt["id"].startswith("stmt_")
    assert isinstance(stmt["created_at"], datetime)


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
