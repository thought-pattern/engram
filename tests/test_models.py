"""Tests for data models."""

from datetime import datetime

from engram.constants import Tier
from engram.models import (
    keyword_entry,
    keyword_entry_from_dict,
    keyword_entry_hit_rate,
    keyword_entry_to_dict,
    query_result,
    session,
    session_from_dict,
    session_to_dict,
    session_touch,
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


def test_statement_create_static() -> None:
    stmt = statement("Static statement", tier=Tier.STATIC)
    assert stmt["tier"] == Tier.STATIC


def test_statement_create_with_id() -> None:
    stmt = statement("Test", statement_id="custom_id")
    assert stmt["id"] == "custom_id"


def test_statement_create_with_keywords() -> None:
    stmt = statement("Test", keywords=["hello", "world"])
    assert stmt["keywords"] == ["hello", "world"]


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


def test_keyword_entry_hit_rate_default() -> None:
    entry = keyword_entry(keyword="test")
    assert keyword_entry_hit_rate(entry) == 0.5  # Default when query_count = 0


def test_keyword_entry_hit_rate_calculation() -> None:
    entry = keyword_entry(keyword="test", query_count=100, hit_count=80)
    assert keyword_entry_hit_rate(entry) == 0.8


def test_keyword_entry_hit_rate_zero_hits() -> None:
    entry = keyword_entry(keyword="test", query_count=50, hit_count=0)
    assert keyword_entry_hit_rate(entry) == 0.0


def test_keyword_entry_add_statement() -> None:
    entry = keyword_entry(keyword="test")
    entry["statement_ids"].add("stmt_1")
    entry["statement_ids"].add("stmt_2")
    entry["statement_ids"].add("stmt_1")  # Duplicate ignored by set

    assert entry["statement_ids"] == {"stmt_1", "stmt_2"}


def test_keyword_entry_remove_statement() -> None:
    entry = keyword_entry(keyword="test", statement_ids={"stmt_1", "stmt_2"})
    entry["statement_ids"].discard("stmt_1")
    assert entry["statement_ids"] == {"stmt_2"}


def test_keyword_entry_remove_nonexistent() -> None:
    entry = keyword_entry(keyword="test", statement_ids={"stmt_1"})
    entry["statement_ids"].discard("stmt_999")  # discard doesn't raise
    assert entry["statement_ids"] == {"stmt_1"}


def test_keyword_entry_increment_query() -> None:
    entry = keyword_entry(keyword="test")
    entry["query_count"] += 1
    entry["query_count"] += 1
    assert entry["query_count"] == 2


def test_keyword_entry_increment_hit() -> None:
    entry = keyword_entry(keyword="test")
    entry["hit_count"] += 1
    assert entry["hit_count"] == 1


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


def test_session_create_with_id() -> None:
    sess = session(session_id="user_abc")
    assert sess["session_id"] == "user_abc"


def test_session_create_with_metadata() -> None:
    sess = session(metadata={"user_id": "123"})
    assert sess["metadata"]["user_id"] == "123"


def test_session_update_context() -> None:
    sess = session()
    original_active = sess["last_active"]

    # Small delay to ensure timestamp changes
    import time

    time.sleep(0.01)

    session_update_context(sess, "Paris is the capital of France")
    assert sess["previous_response"] == "Paris is the capital of France"
    assert sess["last_active"] > original_active


def test_session_touch() -> None:
    sess = session()
    original_active = sess["last_active"]

    import time

    time.sleep(0.01)

    session_touch(sess)
    assert sess["last_active"] > original_active


def test_session_serialization() -> None:
    sess = session(session_id="test", metadata={"key": "value"})
    session_update_context(sess, "Previous response")

    data = session_to_dict(sess)
    restored = session_from_dict(data)

    assert restored["session_id"] == "test"
    assert restored["previous_response"] == "Previous response"
    assert restored["metadata"]["key"] == "value"


"""Tests for QueryResult model."""


def test_query_result_matches_extraction() -> None:
    stmt1 = statement("First")
    stmt2 = statement("Second")

    result = query_result(matches=[(stmt1, 1.5), (stmt2, 1.0)], keywords=["test"])
    # Extract statements from matches
    statements = [stmt for stmt, _ in result["matches"]]
    assert statements == [stmt1, stmt2]


def test_query_result_top_match_from_matches() -> None:
    stmt1 = statement("First")
    stmt2 = statement("Second")

    result = query_result(matches=[(stmt1, 1.5), (stmt2, 1.0)], keywords=["test"])
    # Get top match directly from matches
    top = result["matches"][0][0] if result["matches"] else None
    assert top == stmt1


def test_query_result_empty_matches() -> None:
    result = query_result(matches=[], keywords=["test"])
    top = result["matches"][0][0] if result["matches"] else {}
    assert top == {}
