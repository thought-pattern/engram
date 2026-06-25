"""Tests for data models."""

from datetime import datetime

import pytest

from engram.models import (
    KeywordEntry,
    QueryResult,
    Session,
    Statement,
    Tier,
    keyword_entry_from_dict,
    keyword_entry_hit_rate,
    keyword_entry_to_dict,
    session_from_dict,
    session_to_dict,
    session_touch,
    session_update_context,
    statement_from_dict,
    statement_to_dict,
)


class TestStatement:
    """Tests for Statement model."""

    def test_create_dynamic(self) -> None:
        stmt = Statement("Hello world")
        assert stmt["text"] == "Hello world"
        assert stmt["tier"] == Tier.DYNAMIC
        assert stmt["id"].startswith("stmt_")
        assert isinstance(stmt["created_at"], datetime)

    def test_create_static(self) -> None:
        stmt = Statement("Static statement", tier=Tier.STATIC)
        assert stmt["tier"] == Tier.STATIC

    def test_create_with_id(self) -> None:
        stmt = Statement("Test", statement_id="custom_id")
        assert stmt["id"] == "custom_id"

    def test_create_with_keywords(self) -> None:
        stmt = Statement("Test", keywords=["hello", "world"])
        assert stmt["keywords"] == ["hello", "world"]

    def test_serialization(self) -> None:
        stmt = Statement("Test statement", keywords=["test"])
        data = statement_to_dict(stmt)

        assert data["text"] == "Test statement"
        assert data["tier"] == "DYNAMIC"
        assert "created_at" in data

        restored = statement_from_dict(data)
        assert restored["id"] == stmt["id"]
        assert restored["text"] == stmt["text"]
        assert restored["tier"] == stmt["tier"]


class TestKeywordEntry:
    """Tests for KeywordEntry model."""

    def test_hit_rate_default(self) -> None:
        entry = KeywordEntry(keyword="test")
        assert keyword_entry_hit_rate(entry) == 0.5  # Default when query_count = 0

    def test_hit_rate_calculation(self) -> None:
        entry = KeywordEntry(keyword="test", query_count=100, hit_count=80)
        assert keyword_entry_hit_rate(entry) == 0.8

    def test_hit_rate_zero_hits(self) -> None:
        entry = KeywordEntry(keyword="test", query_count=50, hit_count=0)
        assert keyword_entry_hit_rate(entry) == 0.0

    def test_add_statement(self) -> None:
        entry = KeywordEntry(keyword="test")
        entry["statement_ids"].add("stmt_1")
        entry["statement_ids"].add("stmt_2")
        entry["statement_ids"].add("stmt_1")  # Duplicate ignored by set

        assert entry["statement_ids"] == {"stmt_1", "stmt_2"}

    def test_remove_statement(self) -> None:
        entry = KeywordEntry(keyword="test", statement_ids={"stmt_1", "stmt_2"})
        entry["statement_ids"].discard("stmt_1")
        assert entry["statement_ids"] == {"stmt_2"}

    def test_remove_nonexistent(self) -> None:
        entry = KeywordEntry(keyword="test", statement_ids={"stmt_1"})
        entry["statement_ids"].discard("stmt_999")  # discard doesn't raise
        assert entry["statement_ids"] == {"stmt_1"}

    def test_increment_query(self) -> None:
        entry = KeywordEntry(keyword="test")
        entry["query_count"] += 1
        entry["query_count"] += 1
        assert entry["query_count"] == 2

    def test_increment_hit(self) -> None:
        entry = KeywordEntry(keyword="test")
        entry["hit_count"] += 1
        assert entry["hit_count"] == 1

    def test_serialization(self) -> None:
        entry = KeywordEntry(
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


class TestSession:
    """Tests for Session model."""

    def test_create(self) -> None:
        session = Session()
        assert session["session_id"].startswith("sess_")
        assert session["previous_response"] == ""
        assert isinstance(session["created_at"], datetime)
        assert session["last_active"] == session["created_at"]

    def test_create_with_id(self) -> None:
        session = Session(session_id="user_abc")
        assert session["session_id"] == "user_abc"

    def test_create_with_metadata(self) -> None:
        session = Session(metadata={"user_id": "123"})
        assert session["metadata"]["user_id"] == "123"

    def test_update_context(self) -> None:
        session = Session()
        original_active = session["last_active"]

        # Small delay to ensure timestamp changes
        import time

        time.sleep(0.01)

        session_update_context(session, "Paris is the capital of France")
        assert session["previous_response"] == "Paris is the capital of France"
        assert session["last_active"] > original_active

    def test_touch(self) -> None:
        session = Session()
        original_active = session["last_active"]

        import time

        time.sleep(0.01)

        session_touch(session)
        assert session["last_active"] > original_active

    def test_serialization(self) -> None:
        session = Session(session_id="test", metadata={"key": "value"})
        session_update_context(session, "Previous response")

        data = session_to_dict(session)
        restored = session_from_dict(data)

        assert restored["session_id"] == "test"
        assert restored["previous_response"] == "Previous response"
        assert restored["metadata"]["key"] == "value"


class TestQueryResult:
    """Tests for QueryResult model."""

    def test_matches_extraction(self) -> None:
        stmt1 = Statement("First")
        stmt2 = Statement("Second")

        result = QueryResult(matches=[(stmt1, 1.5), (stmt2, 1.0)], keywords=["test"])
        # Extract statements from matches
        statements = [stmt for stmt, _ in result["matches"]]
        assert statements == [stmt1, stmt2]

    def test_top_match_from_matches(self) -> None:
        stmt1 = Statement("First")
        stmt2 = Statement("Second")

        result = QueryResult(matches=[(stmt1, 1.5), (stmt2, 1.0)], keywords=["test"])
        # Get top match directly from matches
        top = result["matches"][0][0] if result["matches"] else None
        assert top == stmt1

    def test_empty_matches(self) -> None:
        result = QueryResult(matches=[], keywords=["test"])
        top = result["matches"][0][0] if result["matches"] else None
        assert top is None
