"""Tests for data models."""

from datetime import datetime
from time import sleep as time_sleep

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
from engram.sessions import MIN_SESSION_TIME


class TestStatement:
    """Tests for Statement model."""

    def test_create_dynamic(self) -> bool:
        stmt = statement("Hello world")
        assert stmt.get("text", "") == "Hello world"
        assert stmt.get("tier", "") == Tier.DYNAMIC
        assert stmt.get("id", "").startswith("stmt_")
        assert isinstance(stmt.get("created_at", False), datetime)
        return False

    def test_create_static(self) -> bool:
        stmt = statement("Static statement", tier=Tier.STATIC)
        assert stmt.get("tier", "") == Tier.STATIC
        return False

    def test_create_with_id(self) -> bool:
        stmt = statement("Test", statement_id="custom_id")
        assert stmt.get("id", "") == "custom_id"
        return False

    def test_create_with_keywords(self) -> bool:
        stmt = statement("Test", keywords=["hello", "world"])
        assert stmt.get("keywords", []) == ["hello", "world"]
        return False

    def test_serialization(self) -> bool:
        stmt = statement(
            "Test statement",
            keywords=["test"],
            pattern="TEST",
            pattern_aliases=["WHAT IS TEST"],
            introduced_by_user_id="alice",
            source_label="conversation",
        )
        data = statement_to_dict(stmt)

        assert data.get("text", "") == "Test statement"
        assert data.get("tier", "") == "DYNAMIC"
        assert "created_at" in data

        restored = statement_from_dict(data)
        assert restored.get("id", "") == stmt.get("id", "")
        assert restored.get("text", "") == stmt.get("text", "")
        assert restored.get("tier", "") == stmt.get("tier", "")
        assert restored.get("pattern_aliases", []) == ["WHAT IS TEST"]
        assert restored.get("introduced_by_user_id", "") == "alice"
        assert restored.get("source_label", "") == "conversation"
        return False

    def test_unattributed_statement_defaults(self) -> bool:
        stmt = statement("Shared fact")

        assert stmt.get("introduced_by_user_id", "") == ""
        assert stmt.get("source_label", "") == ""
        assert stmt.get("pattern_aliases", []) == []
        return False


class TestKeywordEntry:
    """Tests for KeywordEntry model."""

    def test_hit_rate_default(self) -> bool:
        entry = keyword_entry(keyword="test")
        assert keyword_entry_hit_rate(entry) == 0.5  # Default when query_count = 0
        return False

    def test_hit_rate_calculation(self) -> bool:
        entry = keyword_entry(keyword="test", query_count=100, hit_count=80)
        assert keyword_entry_hit_rate(entry) == 0.8
        return False

    def test_hit_rate_zero_hits(self) -> bool:
        entry = keyword_entry(keyword="test", query_count=50, hit_count=0)
        assert keyword_entry_hit_rate(entry) == 0.0
        return False

    def test_add_statement(self) -> bool:
        entry = keyword_entry(keyword="test")
        entry.get("statement_ids", set()).add("stmt_1")
        entry.get("statement_ids", set()).add("stmt_2")
        entry.get("statement_ids", set()).add("stmt_1")  # Duplicate ignored by set

        assert entry.get("statement_ids", set()) == {"stmt_1", "stmt_2"}
        return False

    def test_remove_statement(self) -> bool:
        entry = keyword_entry(keyword="test", statement_ids={"stmt_1", "stmt_2"})
        entry.get("statement_ids", set()).discard("stmt_1")
        assert entry.get("statement_ids", set()) == {"stmt_2"}
        return False

    def test_remove_nonexistent(self) -> bool:
        entry = keyword_entry(keyword="test", statement_ids={"stmt_1"})
        entry.get("statement_ids", set()).discard("stmt_999")  # discard doesn't raise
        assert entry.get("statement_ids", set()) == {"stmt_1"}
        return False

    def test_increment_query(self) -> bool:
        entry = keyword_entry(keyword="test")
        entry["query_count"] += 1
        entry["query_count"] += 1
        assert entry.get("query_count", 0) == 2
        return False

    def test_increment_hit(self) -> bool:
        entry = keyword_entry(keyword="test")
        entry["hit_count"] += 1
        assert entry.get("hit_count", 0) == 1
        return False

    def test_serialization(self) -> bool:
        entry = keyword_entry(
            keyword="paris",
            statement_ids={"stmt_1"},
            query_count=150,
            hit_count=142,
        )
        data = keyword_entry_to_dict(entry)

        restored = keyword_entry_from_dict("paris", data)
        assert restored.get("keyword", "") == "paris"
        assert restored.get("statement_ids", set()) == {"stmt_1"}
        assert restored.get("query_count", 0) == 150
        assert restored.get("hit_count", 0) == 142
        return False


class TestSession:
    """Tests for Session model."""

    def test_create(self) -> bool:
        sess = session()
        assert sess.get("session_id", "").startswith("sess_")
        assert sess.get("previous_response", "") == ""
        assert isinstance(sess.get("created_at", False), datetime)
        assert sess.get("last_active", False) == sess.get("created_at", False)
        return False

    def test_create_with_id(self) -> bool:
        sess = session(session_id="user_abc")
        assert sess.get("session_id", "") == "user_abc"
        return False

    def test_create_with_metadata(self) -> bool:
        sess = session(metadata={"user_id": "123"})
        assert sess.get("metadata", {}).get("user_id", "") == "123"
        return False

    def test_update_context(self) -> bool:
        sess = session()
        original_active = sess.get("last_active", MIN_SESSION_TIME)

        time_sleep(0.01)

        session_update_context(sess, "Paris is the capital of France")
        assert sess.get("previous_response", "") == "Paris is the capital of France"
        assert sess.get("last_active", MIN_SESSION_TIME) > original_active
        return False

    def test_touch(self) -> bool:
        sess = session()
        original_active = sess.get("last_active", MIN_SESSION_TIME)

        time_sleep(0.01)

        session_touch(sess)
        assert sess.get("last_active", MIN_SESSION_TIME) > original_active
        return False

    def test_serialization(self) -> bool:
        sess = session(session_id="test", metadata={"key": "value"})
        session_update_context(sess, "Previous response")

        data = session_to_dict(sess)
        restored = session_from_dict(data)

        assert restored.get("session_id", "") == "test"
        assert restored.get("previous_response", "") == "Previous response"
        assert restored.get("metadata", {}).get("key", "") == "value"
        return False


class TestQueryResult:
    """Tests for QueryResult model."""

    def test_matches_extraction(self) -> bool:
        stmt1 = statement("First")
        stmt2 = statement("Second")

        result = query_result(matches=[(stmt1, 1.5), (stmt2, 1.0)], keywords=["test"])
        # Extract statements from matches
        statements = [stmt for stmt, _ in result.get("matches", [])]
        assert statements == [stmt1, stmt2]
        return False

    def test_top_match_from_matches(self) -> bool:
        stmt1 = statement("First")
        stmt2 = statement("Second")

        result = query_result(matches=[(stmt1, 1.5), (stmt2, 1.0)], keywords=["test"])
        # Get top match directly from matches
        top = result.get("matches", [])[0][0] if result.get("matches", []) else False
        assert top == stmt1
        return False

    def test_empty_matches(self) -> bool:
        result = query_result(matches=[], keywords=["test"])
        top = result.get("matches", [])[0][0] if result.get("matches", []) else False
        assert top is None
        return False
