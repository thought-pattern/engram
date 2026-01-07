"""Tests for core ENGRAM implementation."""

import json
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from engram import Engram, EngramConfig, SessionOverflow, Tier
from engram.core import SessionLimitExceeded, SessionNotFound


class TestEngramStore:
    """Tests for statement storage."""

    def test_store_basic(self) -> None:
        engram = Engram()
        stmt_id = engram.store("Hello world")

        assert stmt_id.startswith("stmt_")
        assert engram.statement_count == 1

    def test_store_static(self) -> None:
        engram = Engram()
        engram.store("Static statement", tier=Tier.STATIC)

        assert engram.static_count == 1
        assert engram.dynamic_count == 0

    def test_store_dynamic(self) -> None:
        engram = Engram()
        engram.store("Dynamic statement", tier=Tier.DYNAMIC)

        assert engram.static_count == 0
        assert engram.dynamic_count == 1

    def test_store_keywords_indexed(self) -> None:
        engram = Engram()
        engram.store("Paris is the capital of France")

        assert engram.keyword_count > 0

    def test_store_capacity_eviction(self) -> None:
        config = EngramConfig(capacity=3)
        engram = Engram(config=config)

        engram.store("First", tier=Tier.DYNAMIC)
        engram.store("Second", tier=Tier.DYNAMIC)
        engram.store("Third", tier=Tier.DYNAMIC)
        engram.store("Fourth", tier=Tier.DYNAMIC)

        assert engram.dynamic_count == 3
        assert engram.total_evictions == 1

    def test_store_static_not_evicted(self) -> None:
        config = EngramConfig(capacity=2)
        engram = Engram(config=config)

        engram.store("Static", tier=Tier.STATIC)
        engram.store("Dynamic 1", tier=Tier.DYNAMIC)
        engram.store("Dynamic 2", tier=Tier.DYNAMIC)
        engram.store("Dynamic 3", tier=Tier.DYNAMIC)

        assert engram.static_count == 1
        assert engram.dynamic_count == 2


class TestEngramQuery:
    """Tests for query operations."""

    def test_query_basic(self) -> None:
        engram = Engram()
        engram.store("Paris is the capital of France")

        result = engram.query("What is the capital of France?")

        assert len(result.matches) == 1
        assert "Paris" in result.top_match.text

    def test_query_multiple_matches(self) -> None:
        engram = Engram()
        engram.store("Paris is the capital of France")
        engram.store("France has a population of 67 million")
        engram.store("The Eiffel Tower is in Paris")

        result = engram.query("France Paris")

        assert len(result.matches) >= 2

    def test_query_limit(self) -> None:
        engram = Engram()
        for i in range(10):
            engram.store(f"France statement number {i}")

        result = engram.query("France", limit=3)

        assert len(result.matches) == 3

    def test_query_no_matches(self) -> None:
        engram = Engram()
        engram.store("Hello world")

        result = engram.query("Goodbye universe")

        assert len(result.matches) == 0

    def test_query_stopwords_only(self) -> None:
        engram = Engram()
        engram.store("Hello world")

        result = engram.query("the is are")

        assert len(result.keywords) == 0
        assert len(result.matches) == 0

    def test_query_increments_query_count(self) -> None:
        engram = Engram()
        engram.store("Paris France")

        engram.query("Paris")
        engram.query("Paris")
        engram.query("Paris")

        assert engram.total_queries == 3

    def test_query_with_session_context(self) -> None:
        engram = Engram()
        engram.store("Paris is the capital of France")
        engram.store("France has a population of 67 million")

        session_id = engram.create_session()
        engram.update_session_context(session_id, "Paris is the capital of France")

        # "population" alone might not match, but with context it should
        result = engram.query("What is its population?", session_id=session_id)

        # Context expansion adds "Paris" and "France" keywords
        assert len(result.keywords) > 1


class TestEngramRecordHit:
    """Tests for hit recording."""

    def test_record_hit(self) -> None:
        engram = Engram()
        engram.store("Paris France")

        engram.query("Paris")
        engram.record_hit(["paris"])

        assert engram.total_hits == 1

    def test_record_hit_multiple_keywords(self) -> None:
        engram = Engram()
        engram.store("Paris is the capital of France")

        engram.query("Paris France")
        engram.record_hit(["paris", "france"])

        assert engram.total_hits == 1


class TestEngramEviction:
    """Tests for eviction."""

    def test_evict_manual(self) -> None:
        engram = Engram()
        engram.store("First", tier=Tier.DYNAMIC)
        engram.store("Second", tier=Tier.DYNAMIC)

        result = engram.evict()

        assert result is True
        assert engram.dynamic_count == 1

    def test_evict_empty(self) -> None:
        engram = Engram()
        result = engram.evict()
        assert result is False

    def test_evict_only_static(self) -> None:
        engram = Engram()
        engram.store("Static", tier=Tier.STATIC)

        result = engram.evict()

        assert result is False
        assert engram.static_count == 1

    def test_clear_dynamic(self) -> None:
        engram = Engram()
        engram.store("Static", tier=Tier.STATIC)
        engram.store("Dynamic 1", tier=Tier.DYNAMIC)
        engram.store("Dynamic 2", tier=Tier.DYNAMIC)

        count = engram.clear_dynamic()

        assert count == 2
        assert engram.static_count == 1
        assert engram.dynamic_count == 0


class TestEvictionPolicies:
    """Tests for different eviction policies."""

    def test_fifo_eviction(self) -> None:
        """FIFO evicts oldest (first stored) DYNAMIC statement."""
        from engram.config import EvictionPolicy

        config = EngramConfig(capacity=3, eviction_policy=EvictionPolicy.FIFO)
        engram = Engram(config=config)

        id1 = engram.store("First", tier=Tier.DYNAMIC)
        id2 = engram.store("Second", tier=Tier.DYNAMIC)
        id3 = engram.store("Third", tier=Tier.DYNAMIC)
        # Capacity reached, next store triggers eviction
        engram.store("Fourth", tier=Tier.DYNAMIC)

        # First should be evicted (FIFO)
        assert engram.get_statement(id1) is None
        assert engram.get_statement(id2) is not None
        assert engram.get_statement(id3) is not None

    def test_lru_eviction(self) -> None:
        """LRU evicts least recently used (oldest last_hit)."""
        from engram.config import EvictionPolicy
        from datetime import datetime, timezone, timedelta

        config = EngramConfig(capacity=3, eviction_policy=EvictionPolicy.LRU)
        engram = Engram(config=config)

        id1 = engram.store("First", tier=Tier.DYNAMIC)
        id2 = engram.store("Second", tier=Tier.DYNAMIC)
        id3 = engram.store("Third", tier=Tier.DYNAMIC)

        # Simulate hits on second and third, but not first
        stmt2 = engram.get_statement(id2)
        stmt3 = engram.get_statement(id3)
        if stmt2:
            stmt2.record_hit()
        if stmt3:
            stmt3.record_hit()

        # Store fourth, should evict first (never hit, so oldest)
        engram.store("Fourth", tier=Tier.DYNAMIC)

        assert engram.get_statement(id1) is None
        assert engram.get_statement(id2) is not None
        assert engram.get_statement(id3) is not None

    def test_lfu_eviction(self) -> None:
        """LFU evicts least frequently used (lowest hit_count)."""
        from engram.config import EvictionPolicy

        config = EngramConfig(capacity=3, eviction_policy=EvictionPolicy.LFU)
        engram = Engram(config=config)

        id1 = engram.store("First", tier=Tier.DYNAMIC)
        id2 = engram.store("Second", tier=Tier.DYNAMIC)
        id3 = engram.store("Third", tier=Tier.DYNAMIC)

        # Give second and third some hits
        stmt2 = engram.get_statement(id2)
        stmt3 = engram.get_statement(id3)
        if stmt2:
            stmt2.record_hit()
            stmt2.record_hit()
        if stmt3:
            stmt3.record_hit()

        # First has 0 hits, should be evicted
        engram.store("Fourth", tier=Tier.DYNAMIC)

        assert engram.get_statement(id1) is None
        assert engram.get_statement(id2) is not None
        assert engram.get_statement(id3) is not None

    def test_hit_rate_eviction(self) -> None:
        """HIT_RATE evicts statement with lowest hit rate."""
        from engram.config import EvictionPolicy

        config = EngramConfig(capacity=3, eviction_policy=EvictionPolicy.HIT_RATE)
        engram = Engram(config=config)

        id1 = engram.store("First", tier=Tier.DYNAMIC)
        id2 = engram.store("Second", tier=Tier.DYNAMIC)
        id3 = engram.store("Third", tier=Tier.DYNAMIC)

        # Simulate query/hit patterns
        stmt1 = engram.get_statement(id1)
        stmt2 = engram.get_statement(id2)
        stmt3 = engram.get_statement(id3)

        if stmt1:
            # 10 queries, 1 hit = 0.1 hit rate
            for _ in range(10):
                stmt1.record_query()
            stmt1.record_hit()
        if stmt2:
            # 10 queries, 5 hits = 0.5 hit rate
            for _ in range(10):
                stmt2.record_query()
            for _ in range(5):
                stmt2.record_hit()
        if stmt3:
            # 10 queries, 8 hits = 0.8 hit rate
            for _ in range(10):
                stmt3.record_query()
            for _ in range(8):
                stmt3.record_hit()

        # First has lowest hit rate, should be evicted
        engram.store("Fourth", tier=Tier.DYNAMIC)

        assert engram.get_statement(id1) is None
        assert engram.get_statement(id2) is not None
        assert engram.get_statement(id3) is not None

    def test_min_hit_rate_protection(self) -> None:
        """Statements above min_hit_rate are protected from eviction."""
        from engram.config import EvictionPolicy

        config = EngramConfig(
            capacity=3,
            eviction_policy=EvictionPolicy.HIT_RATE,
            min_hit_rate=0.3
        )
        engram = Engram(config=config)

        id1 = engram.store("First", tier=Tier.DYNAMIC)
        id2 = engram.store("Second", tier=Tier.DYNAMIC)
        id3 = engram.store("Third", tier=Tier.DYNAMIC)

        # Set hit rates: first=0.2 (below threshold), second=0.5, third=0.5
        stmt1 = engram.get_statement(id1)
        stmt2 = engram.get_statement(id2)
        stmt3 = engram.get_statement(id3)

        if stmt1:
            for _ in range(10):
                stmt1.record_query()
            for _ in range(2):
                stmt1.record_hit()  # 0.2 hit rate

        if stmt2:
            for _ in range(10):
                stmt2.record_query()
            for _ in range(5):
                stmt2.record_hit()  # 0.5 hit rate

        if stmt3:
            for _ in range(10):
                stmt3.record_query()
            for _ in range(5):
                stmt3.record_hit()  # 0.5 hit rate

        # Only first is below min_hit_rate, should be evicted
        engram.store("Fourth", tier=Tier.DYNAMIC)

        assert engram.get_statement(id1) is None
        assert engram.get_statement(id2) is not None
        assert engram.get_statement(id3) is not None


class TestEngramSessions:
    """Tests for session management."""

    def test_create_session(self) -> None:
        engram = Engram()
        session_id = engram.create_session()

        assert session_id.startswith("sess_")
        assert engram.session_count == 1

    def test_create_session_with_id(self) -> None:
        engram = Engram()
        session_id = engram.create_session(session_id="user_abc")

        assert session_id == "user_abc"

    def test_create_session_with_metadata(self) -> None:
        engram = Engram()
        engram.create_session(session_id="test", metadata={"user_id": "123"})

        session = engram.get_session("test")
        assert session.metadata["user_id"] == "123"

    def test_get_session(self) -> None:
        engram = Engram()
        engram.create_session(session_id="test")

        session = engram.get_session("test")

        assert session is not None
        assert session.session_id == "test"

    def test_get_session_create_if_missing(self) -> None:
        engram = Engram()

        session = engram.get_session("new_session", create_if_missing=True)

        assert session is not None
        assert session.session_id == "new_session"

    def test_get_session_not_found(self) -> None:
        engram = Engram()

        session = engram.get_session("nonexistent", create_if_missing=False)

        assert session is None

    def test_update_session_context(self) -> None:
        engram = Engram()
        engram.create_session(session_id="test")

        engram.update_session_context("test", "Previous response")

        session = engram.get_session("test")
        assert session.previous_response == "Previous response"

    def test_update_session_context_not_found(self) -> None:
        engram = Engram()

        with pytest.raises(SessionNotFound):
            engram.update_session_context("nonexistent", "Response")

    def test_delete_session(self) -> None:
        engram = Engram()
        engram.create_session(session_id="test")

        result = engram.delete_session("test")

        assert result is True
        assert engram.session_count == 0

    def test_delete_session_not_found(self) -> None:
        engram = Engram()

        result = engram.delete_session("nonexistent")

        assert result is False

    def test_expire_sessions(self) -> None:
        engram = Engram()
        engram.create_session(session_id="old")
        engram.create_session(session_id="new")

        # Manually set old session's last_active to past
        old_session = engram.get_session("old")
        old_session.last_active = datetime.now(timezone.utc) - timedelta(hours=2)

        count = engram.expire_sessions(inactive_threshold=timedelta(hours=1))

        assert count == 1
        assert engram.session_count == 1

    def test_list_sessions(self) -> None:
        engram = Engram()
        engram.create_session(session_id="a")
        engram.create_session(session_id="b")
        engram.create_session(session_id="c")

        sessions = engram.list_sessions()

        assert len(sessions) == 3

    def test_list_sessions_filtered(self) -> None:
        engram = Engram()
        engram.create_session(session_id="old")
        engram.create_session(session_id="new")

        # Set old session to past
        old_session = engram.get_session("old")
        old_session.last_active = datetime.now(timezone.utc) - timedelta(hours=2)

        sessions = engram.list_sessions(
            active_since=datetime.now(timezone.utc) - timedelta(hours=1)
        )

        assert len(sessions) == 1

    def test_session_limit_lru(self) -> None:
        config = EngramConfig(max_sessions=2, session_overflow=SessionOverflow.LRU)
        engram = Engram(config=config)

        engram.create_session(session_id="first")
        engram.create_session(session_id="second")
        engram.create_session(session_id="third")

        assert engram.session_count == 2
        assert engram.get_session("first", create_if_missing=False) is None

    def test_session_limit_reject(self) -> None:
        config = EngramConfig(max_sessions=2, session_overflow=SessionOverflow.REJECT)
        engram = Engram(config=config)

        engram.create_session(session_id="first")
        engram.create_session(session_id="second")

        with pytest.raises(SessionLimitExceeded):
            engram.create_session(session_id="third")


class TestEngramPersistence:
    """Tests for persistence."""

    def test_save_load_file(self) -> None:
        engram = Engram()
        engram.store("Paris is the capital of France", tier=Tier.STATIC)
        engram.store("Dynamic statement", tier=Tier.DYNAMIC)
        engram.create_session(session_id="test")

        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            path = f.name

        try:
            engram.save(path)
            loaded = Engram.load(path)

            assert loaded.statement_count == 2
            assert loaded.static_count == 1
            assert loaded.dynamic_count == 1
            assert loaded.session_count == 1
        finally:
            Path(path).unlink()

    def test_save_load_json_string(self) -> None:
        engram = Engram()
        engram.store("Test statement")

        json_str = engram.save_json()
        loaded = Engram.load_json(json_str)

        assert loaded.statement_count == 1

    def test_to_dict_from_dict(self) -> None:
        engram = Engram()
        engram.store("Statement 1")
        engram.store("Statement 2")

        data = engram.to_dict()
        loaded = Engram.from_dict(data)

        assert loaded.statement_count == 2

    def test_persistence_preserves_statistics(self) -> None:
        engram = Engram()
        engram.store("Paris France")

        # Generate some statistics
        engram.query("Paris")
        engram.query("Paris")
        engram.record_hit(["paris"])

        json_str = engram.save_json()
        loaded = Engram.load_json(json_str)

        # Keywords should have statistics preserved
        assert loaded.keyword_count > 0

    def test_save_load_sessions_only(self) -> None:
        engram = Engram()
        engram.store("Statement")
        engram.create_session(session_id="test")
        engram.update_session_context("test", "Previous response")

        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            path = f.name

        try:
            engram.save_sessions(path)

            # Create new engram and load sessions
            new_engram = Engram()
            count = new_engram.load_sessions(path)

            assert count == 1
            session = new_engram.get_session("test", create_if_missing=False)
            assert session is not None
            assert session.previous_response == "Previous response"
        finally:
            Path(path).unlink()

    def test_rebuild_index(self) -> None:
        engram = Engram()
        engram.store("Paris France")
        engram.store("London England")

        # Clear keywords manually
        engram._keywords.clear()
        assert engram.keyword_count == 0

        # Rebuild
        engram.rebuild_index()

        assert engram.keyword_count > 0

    def test_persistence_bot_properties(self) -> None:
        """Bot properties should persist."""
        engram = Engram()
        engram.set_bot_property("master", "Alice")
        engram.set_bot_property("custom", "value")

        data = engram.to_dict()
        loaded = Engram.from_dict(data)

        assert loaded.get_bot_property("name") == "ENGRAM"  # Default preserved
        assert loaded.get_bot_property("master") == "Alice"
        assert loaded.get_bot_property("custom") == "value"

    def test_persistence_sets(self) -> None:
        """Word sets should persist."""
        engram = Engram()
        engram.add_set("colors", ["red", "blue", "green"])
        engram.add_set("sizes", ["big", "small"])

        data = engram.to_dict()
        loaded = Engram.from_dict(data)

        assert loaded.get_set("colors") == ["red", "blue", "green"]
        assert loaded.get_set("sizes") == ["big", "small"]

    def test_persistence_maps(self) -> None:
        """Maps should persist."""
        engram = Engram()
        engram._maps["capital"] = {"france": "paris", "germany": "berlin"}
        engram._maps["successor"] = {"1": "2", "2": "3"}

        data = engram.to_dict()
        loaded = Engram.from_dict(data)

        assert loaded._maps["capital"]["france"] == "paris"
        assert loaded._maps["successor"]["1"] == "2"

    def test_persistence_substitutions(self) -> None:
        """Custom substitutions should persist."""
        engram = Engram()
        engram._substitution_maps.custom["howdy"] = "hello"
        engram._substitution_maps.contractions["gimme"] = "give me"

        data = engram.to_dict()
        loaded = Engram.from_dict(data)

        assert loaded._substitution_maps.custom["howdy"] == "hello"
        assert loaded._substitution_maps.contractions["gimme"] == "give me"
        # Default contractions should also be present
        assert "don't" in loaded._substitution_maps.contractions

    def test_persistence_sets_work_with_patterns(self) -> None:
        """Loaded sets should work with pattern matching."""
        engram = Engram()
        engram.add_set("color", ["red", "blue"])
        engram.store("Nice color!", pattern="I LIKE {set:color}")

        # Save and load
        data = engram.to_dict()
        loaded = Engram.from_dict(data)

        # Pattern matching should work with loaded set
        result = loaded.pattern_query("i like blue")
        assert result is not None
        assert "Nice color" in result[2]


class TestEngramInitialization:
    """Tests for initialization patterns."""

    def test_load_corpus(self) -> None:
        engram = Engram()
        corpus = [
            "Paris is the capital of France",
            "London is the capital of England",
            "Berlin is the capital of Germany",
        ]

        count = engram.load_corpus(corpus, tier=Tier.STATIC)

        assert count == 3
        assert engram.static_count == 3

    def test_fork(self) -> None:
        parent = Engram()
        parent.store("Static from parent", tier=Tier.STATIC)
        parent.store("Dynamic from parent", tier=Tier.DYNAMIC)
        parent.create_session(session_id="parent_session")

        child = Engram.fork(
            parent,
            static_corpus=["New static"],
        )

        # Should have new static
        assert child.static_count == 1
        # Should have copied dynamic
        assert child.dynamic_count == 1
        # Should NOT have parent sessions
        assert child.session_count == 0


class TestEngramMetrics:
    """Tests for metrics."""

    def test_get_metrics(self) -> None:
        engram = Engram()
        engram.store("Static", tier=Tier.STATIC)
        engram.store("Dynamic", tier=Tier.DYNAMIC)
        engram.create_session()
        engram.query("test")

        metrics = engram.get_metrics()

        assert metrics["statement_count"] == 2
        assert metrics["static_count"] == 1
        assert metrics["dynamic_count"] == 1
        assert metrics["session_count"] == 1
        assert metrics["query_count"] == 1

    def test_overall_hit_rate(self) -> None:
        engram = Engram()
        engram.store("Paris France")

        engram.query("Paris")
        engram.query("Paris")
        engram.record_hit(["paris"])

        assert engram.overall_hit_rate == 0.5

    def test_get_low_hit_keywords(self) -> None:
        engram = Engram()
        engram.store("Paris France")

        # Simulate many queries with few hits
        for _ in range(20):
            engram.query("Paris")
        engram.record_hit(["paris"])

        results = engram.get_low_hit_keywords(min_queries=10, max_hit_rate=0.2)

        assert len(results) >= 1

    def test_get_zero_hit_keywords(self) -> None:
        engram = Engram()
        engram.store("Paris France")

        for _ in range(15):
            engram.query("Paris")
        # No hits recorded

        results = engram.get_zero_hit_keywords(min_queries=10)

        assert len(results) >= 1

    def test_get_coverage_gaps(self) -> None:
        engram = Engram()
        engram.store("Paris France")

        # Generate queries with some hits
        for _ in range(20):
            engram.query("Paris")
        # Record only 2 hits (10% hit rate)
        engram.record_hit(["paris"])
        engram.record_hit(["paris"])

        results = engram.get_coverage_gaps(min_queries=10, max_hit_rate=0.2)

        assert len(results) >= 1
        assert results[0]["keyword"] == "paris"
        assert results[0]["queries"] == 20
        assert results[0]["hits"] == 2
        assert results[0]["hit_rate"] == 0.1

    def test_get_coverage_gaps_returns_dicts(self) -> None:
        engram = Engram()
        engram.store("Test keyword")

        for _ in range(15):
            engram.query("keyword")
        engram.record_hit(["keyword"])

        results = engram.get_coverage_gaps(min_queries=10, max_hit_rate=0.2)

        assert len(results) >= 1
        gap = results[0]
        assert "keyword" in gap
        assert "queries" in gap
        assert "hits" in gap
        assert "hit_rate" in gap

    def test_get_coverage_report(self) -> None:
        engram = Engram()
        engram.store("Hello world")
        engram.store("Goodbye world")

        # Generate some query activity
        for _ in range(10):
            engram.query("hello")
            engram.record_hit(["hello"])
        for _ in range(10):
            engram.query("goodbye")
            # No hits for goodbye

        report = engram.get_coverage_report()

        assert "total_keywords" in report
        assert "keywords_with_hits" in report
        assert "keywords_zero_hits" in report
        assert "overall_hit_rate" in report
        assert "coverage_gaps" in report
        assert "top_performing" in report
        assert "recommendations" in report

    def test_get_coverage_report_recommendations(self) -> None:
        engram = Engram()
        # Store statement with keyword that will have queries but no hits
        engram.store("The missing link information")

        # Generate queries for the keyword (keyword must exist in stored content)
        for _ in range(15):
            engram.query("missing link")
        # No hits recorded - simulates gap in knowledge base

        report = engram.get_coverage_report()

        # Should recommend adding categories for zero-hit keywords
        assert len(report["recommendations"]) >= 1


class TestEngramSpecExamples:
    """Tests based on specification examples."""

    def test_appendix_a_example(self) -> None:
        """Test the example session from Appendix A."""
        engram = Engram(config=EngramConfig(capacity=1000))

        # Initialize
        engram.store("Hello", tier=Tier.STATIC)
        engram.store("Paris is the capital of France", tier=Tier.STATIC)
        engram.store("France has a population of 67 million", tier=Tier.STATIC)

        # Session 1
        session_a = engram.create_session(session_id="user_a")

        # First query
        result1 = engram.query("What is the capital of France?", session_id="user_a")
        assert len(result1.matches) >= 1
        assert "Paris" in result1.top_match.text
        engram.record_hit(result1.keywords)

        # Update context
        engram.update_session_context("user_a", "Paris is the capital of France")

        # Follow-up query with context
        result2 = engram.query("What is its population?", session_id="user_a")
        assert len(result2.matches) >= 1
        # Should find population statement due to context expansion

        # Session 2 (concurrent)
        engram.create_session(session_id="user_b")
        result3 = engram.query("Hello", session_id="user_b")
        assert len(result3.matches) >= 1
        assert "Hello" in result3.top_match.text


class TestEngramContextMatching:
    """Tests for context-aware pattern matching with that/topic."""

    def test_pattern_with_topic(self) -> None:
        """Pattern with topic should match when session topic matches."""
        engram = Engram()
        session_id = engram.create_session()

        # Add patterns
        engram.store("Weather info", pattern="WHAT IS IT", topic="WEATHER")
        engram.store("General info", pattern="WHAT IS IT")

        # Without topic, general pattern wins
        result = engram.pattern_query("what is it", session_id=session_id)
        assert result is not None
        assert result[2] == "General info"

        # Set topic in session
        session = engram.get_session(session_id)
        session.set_predicate("topic", "WEATHER")

        # Now weather-specific pattern wins
        result = engram.pattern_query("what is it", session_id=session_id)
        assert result is not None
        assert result[2] == "Weather info"

    def test_pattern_with_that(self) -> None:
        """Pattern with that should match based on bot's previous response."""
        engram = Engram()
        session_id = engram.create_session()

        # Add patterns
        engram.store("Pizza follow-up", pattern="YES", that="DO YOU LIKE PIZZA")
        engram.store("General yes", pattern="YES")

        # Without previous response, general pattern wins
        result = engram.pattern_query("yes", session_id=session_id)
        assert result is not None
        assert result[2] == "General yes"

        # Simulate previous response
        session = engram.get_session(session_id)
        session.update_context("Do you like pizza?")

        # Now that-specific pattern wins
        result = engram.pattern_query("yes", session_id=session_id)
        assert result is not None
        assert result[2] == "Pizza follow-up"

    def test_pattern_with_topic_and_that(self) -> None:
        """Pattern with both topic and that should require both."""
        engram = Engram()
        session_id = engram.create_session()

        # Add patterns with increasing specificity
        engram.store(
            "Full context",
            pattern="HELLO",
            topic="GREETINGS",
            that="HI THERE"
        )
        engram.store("Topic only", pattern="HELLO", topic="GREETINGS")
        engram.store("General", pattern="HELLO")

        # Test with full context
        session = engram.get_session(session_id)
        session.set_predicate("topic", "GREETINGS")
        session.update_context("Hi there!")

        result = engram.pattern_query("hello", session_id=session_id)
        assert result is not None
        assert result[2] == "Full context"

    def test_thatstar_in_template(self) -> None:
        """Thatstar captures should be available in templates."""
        engram = Engram()
        session_id = engram.create_session()

        # Add pattern with that wildcard and template using thatstar
        engram.store(
            "You mentioned {thatstar1}",
            pattern="YES",
            that="DO YOU LIKE *",
            template={"text": "You mentioned {thatstar1}"}
        )

        # Set up that context
        session = engram.get_session(session_id)
        session.update_context("Do you like pizza?")

        result = engram.pattern_query("yes", session_id=session_id)
        assert result is not None
        assert "pizza" in result[2]

    def test_context_persisted(self) -> None:
        """Context patterns should persist with statements."""
        engram = Engram()
        engram.store("Weather", pattern="INFO", topic="WEATHER", that="ASK ME")

        # Save and reload
        state = engram.to_dict()
        engram2 = Engram.from_dict(state)

        # Verify context is preserved
        patterns = engram2._pattern_matcher.get_patterns_with_context()
        found = False
        for pattern, response, that, topic in patterns:
            if pattern == "INFO":
                assert that == "ASK ME"
                assert topic == "WEATHER"
                found = True
        assert found


class TestEngramSetsAndBotProperties:
    """Tests for word sets and bot properties."""

    def test_add_and_get_set(self) -> None:
        """Test adding and retrieving word sets."""
        engram = Engram()
        engram.add_set("colors", ["red", "blue", "green"])

        assert engram.get_set("colors") == ["red", "blue", "green"]
        assert engram.get_set("unknown") is None

    def test_remove_set(self) -> None:
        """Test removing word sets."""
        engram = Engram()
        engram.add_set("colors", ["red", "blue"])

        assert engram.remove_set("colors") is True
        assert engram.get_set("colors") is None
        assert engram.remove_set("colors") is False

    def test_list_sets(self) -> None:
        """Test listing all set names."""
        engram = Engram()
        engram.add_set("colors", ["red"])
        engram.add_set("sizes", ["big"])

        sets = engram.list_sets()
        assert "colors" in sets
        assert "sizes" in sets

    def test_set_pattern_matching(self) -> None:
        """Test {set:name} pattern matching through Engram."""
        engram = Engram()
        engram.add_set("color", ["red", "blue", "green"])
        engram.store("{star1} is a nice color!", pattern="I LIKE {set:color}")

        result = engram.pattern_query("i like blue")
        assert result is not None
        assert "blue" in result[1]
        assert "nice color" in result[2]

        # Non-set word should not match
        result = engram.pattern_query("i like purple")
        assert result is None

    def test_bot_property_get_set(self) -> None:
        """Test getting and setting bot properties."""
        engram = Engram()

        # Default properties
        assert engram.get_bot_property("name") == "ENGRAM"
        assert engram.get_bot_property("version") == "0.1.6"

        # Custom property
        engram.set_bot_property("master", "Alice")
        assert engram.get_bot_property("master") == "Alice"
        assert engram.get_bot_property("unknown") is None

    def test_get_bot_properties(self) -> None:
        """Test getting all bot properties."""
        engram = Engram()
        engram.set_bot_property("custom", "value")

        props = engram.get_bot_properties()
        assert "name" in props
        assert "version" in props
        assert "custom" in props
        assert props["custom"] == "value"

    def test_bot_pattern_matching(self) -> None:
        """Test {bot:name} pattern matching through Engram."""
        engram = Engram()
        engram.store("Yes, that's my name!", pattern="YOUR NAME IS {bot:name}")

        result = engram.pattern_query("your name is engram")
        assert result is not None
        assert "my name" in result[2]

        # Wrong name should not match
        result = engram.pattern_query("your name is alice")
        assert result is None

    def test_sets_pattern_added_after_set(self) -> None:
        """Test that patterns can use sets added before pattern."""
        engram = Engram()

        # Add set first
        engram.add_set("greeting", ["hello", "hi", "hey"])

        # Then add pattern using set
        engram.store("Greeting received!", pattern="{set:greeting} THERE")

        result = engram.pattern_query("hello there")
        assert result is not None
        assert "Greeting" in result[2]

        result = engram.pattern_query("hey there")
        assert result is not None


class TestMultiSentenceInput:
    """Tests for multi-sentence input processing."""

    def test_single_sentence_no_punctuation(self) -> None:
        """Single sentence without punctuation should work normally."""
        engram = Engram()
        engram.store("Hello to you!", pattern="HELLO")

        result = engram.pattern_query("hello")
        assert result is not None
        assert result[2] == "Hello to you!"

    def test_single_sentence_with_punctuation(self) -> None:
        """Single sentence with punctuation should work normally."""
        engram = Engram()
        engram.store("Hello to you!", pattern="HELLO")

        result = engram.pattern_query("hello!")
        assert result is not None
        assert result[2] == "Hello to you!"

    def test_two_sentences(self) -> None:
        """Two sentences should get two responses combined."""
        engram = Engram()
        engram.store("Hello to you!", pattern="HELLO")
        engram.store("Goodbye to you!", pattern="GOODBYE")

        result = engram.pattern_query("Hello. Goodbye.")
        assert result is not None
        assert "Hello to you!" in result[2]
        assert "Goodbye to you!" in result[2]

    def test_three_sentences(self) -> None:
        """Three sentences should get three responses combined."""
        engram = Engram()
        engram.store("Response A", pattern="A")
        engram.store("Response B", pattern="B")
        engram.store("Response C", pattern="C")

        result = engram.pattern_query("A! B? C.")
        assert result is not None
        assert "Response A" in result[2]
        assert "Response B" in result[2]
        assert "Response C" in result[2]

    def test_partial_match_in_multi_sentence(self) -> None:
        """Should get responses only for matched sentences."""
        engram = Engram()
        engram.store("Hello response", pattern="HELLO")
        # No pattern for "unknown"

        result = engram.pattern_query("Hello. Unknown.")
        assert result is not None
        assert "Hello response" in result[2]
        # "Unknown" doesn't match, so only one response

    def test_returns_first_statement(self) -> None:
        """Should return the first matched statement info."""
        engram = Engram()
        stmt1_id = engram.store("First response", pattern="FIRST")
        engram.store("Second response", pattern="SECOND")

        result = engram.pattern_query("First. Second.")
        assert result is not None
        assert result[0].id == stmt1_id

    def test_multi_sentence_with_session(self) -> None:
        """Multi-sentence with session should update context."""
        engram = Engram()
        session_id = engram.create_session()
        engram.store("Hello!", pattern="HELLO")
        engram.store("Goodbye!", pattern="GOODBYE")

        result = engram.pattern_query("Hello. Goodbye.", session_id=session_id)
        assert result is not None

        # Session context should be updated with combined response
        session = engram.get_session(session_id)
        # The 'that' should be the combined response (normalized)
        assert session.that is not None

    def test_that_context_flows_between_sentences(self) -> None:
        """That context should flow between sentences in same input."""
        engram = Engram()
        session_id = engram.create_session()

        # First pattern sets up a question
        engram.store("Do you like pizza?", pattern="HELLO")

        # Second pattern only matches if 'that' is the pizza question
        engram.store("I like pizza too!", pattern="YES", that="DO YOU LIKE PIZZA")

        # Both in same input - the "YES" should match because
        # the first response becomes the 'that' for the second sentence
        result = engram.pattern_query("Hello. Yes.", session_id=session_id)
        assert result is not None
        assert "pizza" in result[2].lower()

    def test_no_match_returns_none(self) -> None:
        """If no sentences match, should return None."""
        engram = Engram()
        engram.store("Hello!", pattern="HELLO")

        result = engram.pattern_query("Unknown. Also unknown.")
        assert result is None

    def test_empty_input(self) -> None:
        """Empty input should return None."""
        engram = Engram()
        engram.store("Hello!", pattern="HELLO")

        result = engram.pattern_query("")
        assert result is None

        result = engram.pattern_query("   ")
        assert result is None
