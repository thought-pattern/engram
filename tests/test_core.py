"""Tests for core ENGRAM implementation."""

import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from engram import eviction, metrics, persistence, sessions
from engram.config import engram_config, graph_config
from engram.constants import SessionOverflow, Tier
from engram.core import Engram
from engram.models import record_statement_hit, record_statement_query, session_update_context
from engram.sessions import SessionLimitExceededError, SessionNotFoundError


class TestEngramStore:
    """Tests for statement storage."""

    def test_store_basic(self) -> None:
        engram = Engram()
        stmt_id = engram.store("Hello world")

        assert stmt_id.startswith("stmt_")
        assert metrics.get_statement_count(engram) == 1

    def test_store_static(self) -> None:
        engram = Engram()
        engram.store("Static statement", tier=Tier.STATIC)

        assert metrics.get_static_count(engram) == 1
        assert metrics.get_dynamic_count(engram) == 0

    def test_store_dynamic(self) -> None:
        engram = Engram()
        engram.store("Dynamic statement", tier=Tier.DYNAMIC)

        assert metrics.get_static_count(engram) == 0
        assert metrics.get_dynamic_count(engram) == 1

    def test_store_keywords_indexed(self) -> None:
        engram = Engram()
        engram.store("Paris is the capital of France")

        assert metrics.get_keyword_count(engram) > 0

    def test_store_capacity_eviction(self) -> None:
        config = engram_config(capacity=3)
        engram = Engram(config=config)

        engram.store("First", tier=Tier.DYNAMIC)
        engram.store("Second", tier=Tier.DYNAMIC)
        engram.store("Third", tier=Tier.DYNAMIC)
        engram.store("Fourth", tier=Tier.DYNAMIC)

        assert metrics.get_dynamic_count(engram) == 3
        assert engram.eviction_count == 1

    def test_store_static_not_evicted(self) -> None:
        config = engram_config(capacity=2)
        engram = Engram(config=config)

        engram.store("Static", tier=Tier.STATIC)
        engram.store("Dynamic 1", tier=Tier.DYNAMIC)
        engram.store("Dynamic 2", tier=Tier.DYNAMIC)
        engram.store("Dynamic 3", tier=Tier.DYNAMIC)

        assert metrics.get_static_count(engram) == 1
        assert metrics.get_dynamic_count(engram) == 2

    def test_duplicate_statement_id_is_rejected_without_mutation(self) -> None:
        engram = Engram()
        engram.store("First", statement_id="fixed", pattern="FIRST")

        with pytest.raises(ValueError, match="duplicate statement id"):
            engram.store("Second", statement_id="fixed", pattern="SECOND")

        assert len(engram.statements) == 1
        assert engram.pattern_query("first")[2] == "First"
        assert not engram.pattern_query("second")


class TestEngramQuery:
    """Tests for query operations."""

    def test_query_basic(self) -> None:
        engram = Engram()
        engram.store("Paris is the capital of France")

        result = engram.query("What is the capital of France?")

        assert len(result["matches"]) == 1
        assert "Paris" in result["matches"][0][0]["text"]

    def test_query_multiple_matches(self) -> None:
        engram = Engram()
        engram.store("Paris is the capital of France")
        engram.store("France has a population of 67 million")
        engram.store("The Eiffel Tower is in Paris")

        result = engram.query("France Paris")

        assert len(result["matches"]) >= 2

    def test_query_limit(self) -> None:
        engram = Engram()
        for i in range(10):
            engram.store(f"France statement number {i}")

        result = engram.query("France", limit=3)

        assert len(result["matches"]) == 3

    def test_query_no_matches(self) -> None:
        engram = Engram()
        engram.store("Hello world")

        result = engram.query("quantum tensor")

        assert len(result["matches"]) == 0

    def test_query_synonym_match_scores_discounted(self) -> None:
        """A synonym-only match surfaces with less than exact-match credit."""
        engram = Engram()
        engram.store("The automobile is fast")

        with_synonym = engram.query("car")

        assert len(with_synonym["matches"]) == 1
        _, score = with_synonym["matches"][0]
        assert 0.0 < score < 0.5  # discounted below a same-shape exact match

    def test_query_stopwords_only(self) -> None:
        engram = Engram()
        engram.store("Hello world")

        result = engram.query("the is are")

        assert len(result["keywords"]) == 0
        assert len(result["matches"]) == 0

    def test_query_increments_query_count(self) -> None:
        engram = Engram()
        engram.store("Paris France")

        engram.query("Paris")
        engram.query("Paris")
        engram.query("Paris")

        assert engram.query_count == 3

    def test_query_with_session_context(self) -> None:
        engram = Engram()
        engram.store("Paris is the capital of France")
        engram.store("France has a population of 67 million")

        session_id = sessions.create_session(engram)
        sessions.update_session_context(engram, session_id, "Paris is the capital of France")

        # "population" alone might not match, but with context it should
        result = engram.query("What is its population?", session_id=session_id)

        # Context expansion adds "Paris" and "France" keywords
        assert len(result["keywords"]) > 1


class TestEngramRecordHit:
    """Tests for hit recording."""

    def test_record_hit(self) -> None:
        engram = Engram()
        engram.store("Paris France")

        engram.query("Paris")
        engram.record_hit(["paris"])

        assert engram.hit_count == 1

    def test_record_hit_multiple_keywords(self) -> None:
        engram = Engram()
        engram.store("Paris is the capital of France")

        engram.query("Paris France")
        engram.record_hit(["paris", "france"])

        assert engram.hit_count == 1

    def test_query_records_statement_candidacy(self) -> None:
        """Each returned match counts as a query against that statement."""
        engram = Engram()
        stmt_id = engram.store("Paris is the capital of France")

        engram.query("capital of France")

        stmt = engram.get_statement(stmt_id)
        assert stmt["query_count"] == 1
        assert stmt["hit_count"] == 0

    def test_record_hit_with_statement_id(self) -> None:
        """record_hit credits the answering statement when its id is given."""
        engram = Engram()
        stmt_id = engram.store("Paris is the capital of France")

        result = engram.query("capital of France")
        engram.record_hit(result["keywords"], statement_id=stmt_id)

        stmt = engram.get_statement(stmt_id)
        assert stmt["hit_count"] == 1
        assert stmt["last_hit"]

    def test_record_hit_unknown_statement_id(self) -> None:
        """An unknown statement id updates keyword stats and nothing else."""
        engram = Engram()
        engram.store("Paris is the capital of France")

        engram.query("capital of France")
        engram.record_hit(["capital"], statement_id="stmt_missing")

        assert engram.hit_count == 1

    def test_pattern_query_records_statement_usage(self) -> None:
        """Pattern selection records both a query and a hit on the statement."""
        engram = Engram()
        stmt_id = engram.store("Hello there", pattern="HELLO")

        engram.pattern_query("hello")

        stmt = engram.get_statement(stmt_id)
        assert stmt["query_count"] == 1
        assert stmt["hit_count"] == 1
        assert stmt["last_hit"]


class TestEngramEviction:
    """Tests for eviction."""

    def test_evict_manual(self) -> None:
        engram = Engram()
        engram.store("First", tier=Tier.DYNAMIC)
        engram.store("Second", tier=Tier.DYNAMIC)

        result = eviction.evict(engram)

        assert result is True
        assert metrics.get_dynamic_count(engram) == 1

    def test_evict_empty(self) -> None:
        engram = Engram()
        result = eviction.evict(engram)
        assert result is False

    def test_evict_only_static(self) -> None:
        engram = Engram()
        engram.store("Static", tier=Tier.STATIC)

        result = eviction.evict(engram)

        assert result is False
        assert metrics.get_static_count(engram) == 1

    def test_clear_dynamic(self) -> None:
        engram = Engram()
        engram.store("Static", tier=Tier.STATIC)
        engram.store("Dynamic 1", tier=Tier.DYNAMIC)
        engram.store("Dynamic 2", tier=Tier.DYNAMIC)

        count = eviction.clear_dynamic(engram)

        assert count == 2
        assert metrics.get_static_count(engram) == 1
        assert metrics.get_dynamic_count(engram) == 0


class TestEvictionPolicies:
    """Tests for different eviction policies."""

    def test_fifo_eviction(self) -> None:
        """FIFO evicts oldest (first stored) DYNAMIC statement."""
        from engram.config import EvictionPolicy

        config = engram_config(capacity=3, eviction_policy=EvictionPolicy.FIFO)
        engram = Engram(config=config)

        id1 = engram.store("First", tier=Tier.DYNAMIC)
        id2 = engram.store("Second", tier=Tier.DYNAMIC)
        id3 = engram.store("Third", tier=Tier.DYNAMIC)
        # Capacity reached, next store triggers eviction
        engram.store("Fourth", tier=Tier.DYNAMIC)

        # First should be evicted (FIFO)
        assert not engram.get_statement(id1)
        assert engram.get_statement(id2)
        assert engram.get_statement(id3)

    def test_lru_eviction(self) -> None:
        """LRU evicts least recently used (oldest last_hit)."""

        from engram.config import EvictionPolicy

        config = engram_config(capacity=3, eviction_policy=EvictionPolicy.LRU)
        engram = Engram(config=config)

        id1 = engram.store("First", tier=Tier.DYNAMIC)
        id2 = engram.store("Second", tier=Tier.DYNAMIC)
        id3 = engram.store("Third", tier=Tier.DYNAMIC)

        # Simulate hits on second and third, but not first
        stmt2 = engram.get_statement(id2)
        stmt3 = engram.get_statement(id3)
        if stmt2:
            record_statement_hit(stmt2)
        if stmt3:
            record_statement_hit(stmt3)

        # Store fourth, should evict first (never hit, so oldest)
        engram.store("Fourth", tier=Tier.DYNAMIC)

        assert not engram.get_statement(id1)
        assert engram.get_statement(id2)
        assert engram.get_statement(id3)

    def test_lfu_eviction(self) -> None:
        """LFU evicts least frequently used (lowest hit_count)."""
        from engram.config import EvictionPolicy

        config = engram_config(capacity=3, eviction_policy=EvictionPolicy.LFU)
        engram = Engram(config=config)

        id1 = engram.store("First", tier=Tier.DYNAMIC)
        id2 = engram.store("Second", tier=Tier.DYNAMIC)
        id3 = engram.store("Third", tier=Tier.DYNAMIC)

        # Give second and third some hits
        stmt2 = engram.get_statement(id2)
        stmt3 = engram.get_statement(id3)
        if stmt2:
            record_statement_hit(stmt2)
            record_statement_hit(stmt2)
        if stmt3:
            record_statement_hit(stmt3)

        # First has 0 hits, should be evicted
        engram.store("Fourth", tier=Tier.DYNAMIC)

        assert not engram.get_statement(id1)
        assert engram.get_statement(id2)
        assert engram.get_statement(id3)

    def test_hit_rate_eviction(self) -> None:
        """HIT_RATE evicts statement with lowest hit rate."""
        from engram.config import EvictionPolicy

        config = engram_config(capacity=3, eviction_policy=EvictionPolicy.HIT_RATE)
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
                record_statement_query(stmt1)
            record_statement_hit(stmt1)
        if stmt2:
            # 10 queries, 5 hits = 0.5 hit rate
            for _ in range(10):
                record_statement_query(stmt2)
            for _ in range(5):
                record_statement_hit(stmt2)
        if stmt3:
            # 10 queries, 8 hits = 0.8 hit rate
            for _ in range(10):
                record_statement_query(stmt3)
            for _ in range(8):
                record_statement_hit(stmt3)

        # First has lowest hit rate, should be evicted
        engram.store("Fourth", tier=Tier.DYNAMIC)

        assert not engram.get_statement(id1)
        assert engram.get_statement(id2)
        assert engram.get_statement(id3)

    def test_min_hit_rate_protection(self) -> None:
        """Statements above min_hit_rate are protected from eviction."""
        from engram.config import EvictionPolicy

        config = engram_config(capacity=3, eviction_policy=EvictionPolicy.HIT_RATE, min_hit_rate=0.3)
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
                record_statement_query(stmt1)
            for _ in range(2):
                record_statement_hit(stmt1)  # 0.2 hit rate

        if stmt2:
            for _ in range(10):
                record_statement_query(stmt2)
            for _ in range(5):
                record_statement_hit(stmt2)  # 0.5 hit rate

        if stmt3:
            for _ in range(10):
                record_statement_query(stmt3)
            for _ in range(5):
                record_statement_hit(stmt3)  # 0.5 hit rate

        # Only first is below min_hit_rate, should be evicted
        engram.store("Fourth", tier=Tier.DYNAMIC)

        assert not engram.get_statement(id1)
        assert engram.get_statement(id2)
        assert engram.get_statement(id3)


class TestEngramSessions:
    """Tests for session management."""

    def test_create_session(self) -> None:
        engram = Engram()
        session_id = sessions.create_session(engram)

        assert session_id.startswith("sess_")
        assert metrics.get_session_count(engram) == 1

    def test_create_session_with_id(self) -> None:
        engram = Engram()
        session_id = sessions.create_session(engram, session_id="user_abc")

        assert session_id == "user_abc"

    def test_create_session_with_metadata(self) -> None:
        engram = Engram()
        sessions.create_session(engram, session_id="test", metadata={"user_id": "123"})

        session = sessions.get_session(engram, "test")
        assert session["metadata"]["user_id"] == "123"

    def test_get_session(self) -> None:
        engram = Engram()
        sessions.create_session(engram, session_id="test")

        session = sessions.get_session(engram, "test")

        assert session
        assert session["session_id"] == "test"

    def test_get_session_create_if_missing(self) -> None:
        engram = Engram()

        session = sessions.get_session(engram, "new_session", create_if_missing=True)

        assert session
        assert session["session_id"] == "new_session"

    def test_get_session_not_found(self) -> None:
        engram = Engram()

        session = sessions.get_session(engram, "nonexistent", create_if_missing=False)

        assert not session

    def test_update_session_context(self) -> None:
        engram = Engram()
        sessions.create_session(engram, session_id="test")

        sessions.update_session_context(engram, "test", "Previous response")

        session = sessions.get_session(engram, "test")
        assert session["previous_response"] == "Previous response"

    def test_update_session_context_not_found(self) -> None:
        engram = Engram()

        with pytest.raises(SessionNotFoundError):
            sessions.update_session_context(engram, "nonexistent", "Response")

    def test_delete_session(self) -> None:
        engram = Engram()
        sessions.create_session(engram, session_id="test")

        result = sessions.delete_session(engram, "test")

        assert result is True
        assert metrics.get_session_count(engram) == 0

    def test_delete_session_not_found(self) -> None:
        engram = Engram()

        result = sessions.delete_session(engram, "nonexistent")

        assert result is False

    def test_expire_sessions(self) -> None:
        engram = Engram()
        sessions.create_session(engram, session_id="old")
        sessions.create_session(engram, session_id="new")

        # Manually set old session's last_active to past
        old_session = sessions.get_session(engram, "old")
        old_session["last_active"] = datetime.now(UTC) - timedelta(hours=2)

        count = sessions.expire_sessions(engram, inactive_threshold=timedelta(hours=1))

        assert count == 1
        assert metrics.get_session_count(engram) == 1

    def test_list_sessions(self) -> None:
        engram = Engram()
        sessions.create_session(engram, session_id="a")
        sessions.create_session(engram, session_id="b")
        sessions.create_session(engram, session_id="c")

        session_list = sessions.list_sessions(engram)

        assert len(session_list) == 3

    def test_list_sessions_filtered(self) -> None:
        engram = Engram()
        sessions.create_session(engram, session_id="old")
        sessions.create_session(engram, session_id="new")

        # Set old session to past
        old_session = sessions.get_session(engram, "old")
        old_session["last_active"] = datetime.now(UTC) - timedelta(hours=2)

        session_list = sessions.list_sessions(engram, active_since=datetime.now(UTC) - timedelta(hours=1))

        assert len(session_list) == 1

    def test_session_limit_lru(self) -> None:
        config = engram_config(max_sessions=2, session_overflow=SessionOverflow.LRU)
        engram = Engram(config=config)

        sessions.create_session(engram, session_id="first")
        sessions.create_session(engram, session_id="second")
        sessions.create_session(engram, session_id="third")

        assert metrics.get_session_count(engram) == 2
        assert not sessions.get_session(engram, "first", create_if_missing=False)

    def test_session_limit_reject(self) -> None:
        config = engram_config(max_sessions=2, session_overflow=SessionOverflow.REJECT)
        engram = Engram(config=config)

        sessions.create_session(engram, session_id="first")
        sessions.create_session(engram, session_id="second")

        with pytest.raises(SessionLimitExceededError):
            sessions.create_session(engram, session_id="third")


class TestEngramPersistence:
    """Tests for persistence."""

    def test_save_load_file(self) -> None:
        engram = Engram()
        engram.store("Paris is the capital of France", tier=Tier.STATIC)
        engram.store("Dynamic statement", tier=Tier.DYNAMIC)
        sessions.create_session(engram, session_id="test")

        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            path = f.name

        try:
            persistence.save(engram, path)
            loaded = persistence.load_engram(path)

            assert metrics.get_statement_count(loaded) == 2
            assert metrics.get_static_count(loaded) == 1
            assert metrics.get_dynamic_count(loaded) == 1
            assert metrics.get_session_count(loaded) == 1
        finally:
            Path(path).unlink()

    def test_save_load_json_string(self) -> None:
        engram = Engram()
        engram.store("Test statement")

        json_str = persistence.save_json(engram)
        loaded = persistence.load_engram_json(json_str)

        assert metrics.get_statement_count(loaded) == 1

    def test_to_dict_from_dict(self) -> None:
        engram = Engram()
        engram.store("Statement 1")
        engram.store("Statement 2")

        data = persistence.to_dict(engram)
        loaded = persistence.load_engram_from_dict(data)

        assert metrics.get_statement_count(loaded) == 2

    def test_persistence_preserves_statistics(self) -> None:
        engram = Engram()
        engram.store("Paris France")

        # Generate some statistics
        engram.query("Paris")
        engram.query("Paris")
        engram.record_hit(["paris"])

        json_str = persistence.save_json(engram)
        loaded = persistence.load_engram_json(json_str)

        # Keywords should have statistics preserved
        assert metrics.get_keyword_count(loaded) > 0

    def test_persistence_preserves_config(self) -> None:
        """The full config survives a save/load cycle, not just capacity."""
        from engram.constants import EvictionPolicy

        config = engram_config(
            capacity=123,
            weight_base=0.7,
            weight_recency=0.2,
            weight_hit_rate=0.1,
            eviction_policy=EvictionPolicy.HIT_RATE,
            session_overflow=SessionOverflow.REJECT,
            use_synonyms=False,
            fallback_response="Tell me more.",
        )
        engram = Engram(config=config)
        engram.store("Paris France")

        json_str = persistence.save_json(engram)
        loaded = persistence.load_engram_json(json_str)

        assert loaded.config == config

    def test_persistence_config_override_wins(self) -> None:
        """An explicit config passed to the loader beats the stored one."""
        engram = Engram(config=engram_config(capacity=123))
        engram.store("Paris France")

        json_str = persistence.save_json(engram)
        override = engram_config(capacity=456)
        loaded = persistence.load_engram_json(json_str, config=override)

        assert loaded.config["capacity"] == 456

    def test_persistence_omits_graph_password(self) -> None:
        config = engram_config(
            graph=graph_config(enabled=True, username="reader", password="secret")
        )
        engram = Engram(config=config)

        data = persistence.to_dict(engram)

        assert "password" not in data["config"]["graph"]
        loaded = persistence.load_engram_from_dict(data)
        assert loaded.config["graph"]["password"] == ""

    def test_duplicate_statement_id_in_persistence_is_rejected(self) -> None:
        engram = Engram()
        engram.store("First", statement_id="fixed")
        data = persistence.to_dict(engram)
        data["statements"].append(dict(data["statements"][0]))

        with pytest.raises(ValueError, match="duplicate statement id"):
            persistence.load_engram_from_dict(data)

    def test_load_legacy_state_without_config(self) -> None:
        """Files written before the config block still load, keeping capacity."""
        engram = Engram(config=engram_config(capacity=123))
        engram.store("Paris France")

        data = persistence.to_dict(engram)
        del data["config"]
        loaded = persistence.load_engram_from_dict(data)

        assert loaded.config["capacity"] == 123
        assert metrics.get_statement_count(loaded) == 1

    def test_save_load_sessions_only(self) -> None:
        engram = Engram()
        engram.store("Statement")
        sessions.create_session(engram, session_id="test")
        sessions.update_session_context(engram, "test", "Previous response")

        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            path = f.name

        try:
            persistence.save_sessions(engram, path)

            # Create new engram and load sessions
            new_engram = Engram()
            count = persistence.load_sessions(new_engram, path)

            assert count == 1
            session = sessions.get_session(new_engram, "test", create_if_missing=False)
            assert session
            assert session["previous_response"] == "Previous response"
        finally:
            Path(path).unlink()

    def test_rebuild_index(self) -> None:
        engram = Engram()
        engram.store("Paris France")
        engram.store("London England")

        # Clear keywords manually
        engram.keywords.clear()
        assert metrics.get_keyword_count(engram) == 0

        # Rebuild
        persistence.rebuild_index(engram)

        assert metrics.get_keyword_count(engram) > 0

    def test_persistence_bot_properties(self) -> None:
        """Bot properties should persist."""
        engram = Engram()
        engram.bot_properties["master"] = "Alice"
        engram.bot_properties["custom"] = "value"

        data = persistence.to_dict(engram)
        loaded = persistence.load_engram_from_dict(data)

        assert loaded.bot_properties.get("name") == "ENGRAM"  # Default preserved
        assert loaded.bot_properties.get("master") == "Alice"
        assert loaded.bot_properties.get("custom") == "value"

    def test_persistence_sets(self) -> None:
        """Word sets should persist."""
        engram = Engram()
        engram.sets["colors"] = ["red", "blue", "green"]
        engram.sets["sizes"] = ["big", "small"]

        data = persistence.to_dict(engram)
        loaded = persistence.load_engram_from_dict(data)

        assert loaded.sets.get("colors") == ["red", "blue", "green"]
        assert loaded.sets.get("sizes") == ["big", "small"]

    def test_persistence_maps(self) -> None:
        """Maps should persist."""
        engram = Engram()
        engram.maps["capital"] = {"france": "paris", "germany": "berlin"}
        engram.maps["successor"] = {"1": "2", "2": "3"}

        data = persistence.to_dict(engram)
        loaded = persistence.load_engram_from_dict(data)

        assert loaded.maps["capital"]["france"] == "paris"
        assert loaded.maps["successor"]["1"] == "2"

    def test_persistence_substitutions(self) -> None:
        """Custom substitutions should persist."""
        engram = Engram()
        engram.substitution_maps["custom"]["howdy"] = "hello"
        engram.substitution_maps["contractions"]["gimme"] = "give me"

        data = persistence.to_dict(engram)
        loaded = persistence.load_engram_from_dict(data)

        assert loaded.substitution_maps["custom"]["howdy"] == "hello"
        assert loaded.substitution_maps["contractions"]["gimme"] == "give me"
        # Default contractions should also be present
        assert "don't" in loaded.substitution_maps["contractions"]

    def test_persistence_sets_work_with_patterns(self) -> None:
        """Loaded sets should work with pattern matching."""
        engram = Engram()
        engram.sets["color"] = ["red", "blue"]
        engram.store("Nice color!", pattern="I LIKE {set:color}")

        # Save and load
        data = persistence.to_dict(engram)
        loaded = persistence.load_engram_from_dict(data)

        # Pattern matching should work with loaded set
        result = loaded.pattern_query("i like blue")
        assert result
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
        assert metrics.get_static_count(engram) == 3

    def test_fork(self) -> None:
        parent = Engram()
        parent.store("Static from parent", tier=Tier.STATIC)
        parent.store("Dynamic from parent", tier=Tier.DYNAMIC)
        sessions.create_session(parent, session_id="parent_session")

        child = Engram.fork(
            parent,
            static_corpus=["New static"],
        )

        # Should have new static
        assert metrics.get_static_count(child) == 1
        # Should have copied dynamic
        assert metrics.get_dynamic_count(child) == 1
        # Should NOT have parent sessions
        assert metrics.get_session_count(child) == 0

    def test_fork_copies_dynamic_patterns(self) -> None:
        """Scripted DYNAMIC statements survive the fork with their patterns."""
        parent = Engram()
        parent.store("Dynamic greeting", pattern="HI THERE", tier=Tier.DYNAMIC)

        child = Engram.fork(parent)

        result = child.pattern_query("hi there")
        assert result[2] == "Dynamic greeting"


class TestEngramMetrics:
    """Tests for metrics."""

    def test_get_metrics(self) -> None:
        engram = Engram()
        engram.store("Static", tier=Tier.STATIC)
        engram.store("Dynamic", tier=Tier.DYNAMIC)
        sessions.create_session(engram)
        engram.query("test")

        metric_data = metrics.get_metrics(engram)

        assert metric_data["statement_count"] == 2
        assert metric_data["static_count"] == 1
        assert metric_data["dynamic_count"] == 1
        assert metric_data["session_count"] == 1
        assert metric_data["query_count"] == 1

    def test_overall_hit_rate(self) -> None:
        engram = Engram()
        engram.store("Paris France")

        engram.query("Paris")
        engram.query("Paris")
        engram.record_hit(["paris"])

        assert metrics.get_overall_hit_rate(engram) == 0.5

    def test_get_low_hit_keywords(self) -> None:
        engram = Engram()
        engram.store("Paris France")

        # Simulate many queries with few hits
        for _ in range(20):
            engram.query("Paris")
        engram.record_hit(["paris"])

        results = metrics.get_low_hit_keywords(engram, min_queries=10, max_hit_rate=0.2)

        assert len(results) >= 1

    def test_get_zero_hit_keywords(self) -> None:
        engram = Engram()
        engram.store("Paris France")

        for _ in range(15):
            engram.query("Paris")
        # No hits recorded

        results = metrics.get_zero_hit_keywords(engram, min_queries=10)

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

        results = metrics.get_coverage_gaps(engram, min_queries=10, max_hit_rate=0.2)

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

        results = metrics.get_coverage_gaps(engram, min_queries=10, max_hit_rate=0.2)

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

        report = metrics.get_coverage_report(engram)

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

        report = metrics.get_coverage_report(engram)

        # Should recommend adding categories for zero-hit keywords
        assert len(report["recommendations"]) >= 1


class TestEngramSpecExamples:
    """Tests based on specification examples."""

    def test_appendix_a_example(self) -> None:
        """Test the example session from Appendix A."""
        engram = Engram(config=engram_config(capacity=1000))

        # Initialize
        engram.store("Hello", tier=Tier.STATIC)
        engram.store("Paris is the capital of France", tier=Tier.STATIC)
        engram.store("France has a population of 67 million", tier=Tier.STATIC)

        # Session 1
        sessions.create_session(engram, session_id="user_a")

        # First query
        result1 = engram.query("What is the capital of France?", session_id="user_a")
        assert len(result1["matches"]) >= 1
        assert "Paris" in result1["matches"][0][0]["text"]
        engram.record_hit(result1["keywords"])

        # Update context
        sessions.update_session_context(engram, "user_a", "Paris is the capital of France")

        # Follow-up query with context
        result2 = engram.query("What is its population?", session_id="user_a")
        assert len(result2["matches"]) >= 1
        # Should find population statement due to context expansion

        # Session 2 (concurrent)
        sessions.create_session(engram, session_id="user_b")
        result3 = engram.query("Hello", session_id="user_b")
        assert len(result3["matches"]) >= 1
        assert "Hello" in result3["matches"][0][0]["text"]


class TestEngramContextMatching:
    """Tests for context-aware pattern matching with that/topic."""

    def test_pattern_with_topic(self) -> None:
        """Pattern with topic should match when session topic matches."""
        engram = Engram()
        session_id = sessions.create_session(engram)

        # Add patterns
        engram.store("Weather info", pattern="WHAT IS IT", topic="WEATHER")
        engram.store("General info", pattern="WHAT IS IT")

        # Without topic, general pattern wins
        result = engram.pattern_query("what is it", session_id=session_id)
        assert result
        assert result[2] == "General info"

        # Set topic in session
        session = sessions.get_session(engram, session_id)
        session["predicates"]["topic"] = "WEATHER"

        # Now weather-specific pattern wins
        result = engram.pattern_query("what is it", session_id=session_id)
        assert result
        assert result[2] == "Weather info"

    def test_pattern_with_that(self) -> None:
        """Pattern with that should match based on bot's previous response."""
        engram = Engram()
        session_id = sessions.create_session(engram)

        # Add patterns
        engram.store("Pizza follow-up", pattern="YES", that="DO YOU LIKE PIZZA")
        engram.store("General yes", pattern="YES")

        # Without previous response, general pattern wins
        result = engram.pattern_query("yes", session_id=session_id)
        assert result
        assert result[2] == "General yes"

        # Simulate previous response
        session = sessions.get_session(engram, session_id)
        session_update_context(session, "Do you like pizza?")

        # Now that-specific pattern wins
        result = engram.pattern_query("yes", session_id=session_id)
        assert result
        assert result[2] == "Pizza follow-up"

    def test_pattern_with_topic_and_that(self) -> None:
        """Pattern with both topic and that should require both."""
        engram = Engram()
        session_id = sessions.create_session(engram)

        # Add patterns with increasing specificity
        engram.store("Full context", pattern="HELLO", topic="GREETINGS", that="HI THERE")
        engram.store("Topic only", pattern="HELLO", topic="GREETINGS")
        engram.store("General", pattern="HELLO")

        # Test with full context
        session = sessions.get_session(engram, session_id)
        session["predicates"]["topic"] = "GREETINGS"
        session_update_context(session, "Hi there!")

        result = engram.pattern_query("hello", session_id=session_id)
        assert result
        assert result[2] == "Full context"

    def test_thatstar_in_template(self) -> None:
        """Thatstar captures should be available in templates."""
        engram = Engram()
        session_id = sessions.create_session(engram)

        # Add pattern with that wildcard and template using thatstar
        engram.store(
            "You mentioned {thatstar1}",
            pattern="YES",
            that="DO YOU LIKE *",
            template={"text": "You mentioned {thatstar1}"},
        )

        # Set up that context
        session = sessions.get_session(engram, session_id)
        session_update_context(session, "Do you like pizza?")

        result = engram.pattern_query("yes", session_id=session_id)
        assert result
        assert "pizza" in result[2]

    def test_context_persisted(self) -> None:
        """Context patterns should persist with statements."""
        engram = Engram()
        engram.store("Weather", pattern="INFO", topic="WEATHER", that="ASK ME")

        # Save and reload
        state = persistence.to_dict(engram)
        engram2 = persistence.load_engram_from_dict(state)

        # Verify context is preserved
        patterns = engram2.pattern_matcher.get_patterns_with_context()
        found = False
        for pattern, _response, that, topic in patterns:
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
        engram.sets["colors"] = ["red", "blue", "green"]

        assert engram.sets.get("colors") == ["red", "blue", "green"]
        assert engram.sets.get("unknown") is None

    def test_remove_set(self) -> None:
        """Test removing word sets."""
        engram = Engram()
        engram.sets["colors"] = ["red", "blue"]

        del engram.sets["colors"]
        assert engram.sets.get("colors") is None

    def test_list_sets(self) -> None:
        """Test listing all set names."""
        engram = Engram()
        engram.sets["colors"] = ["red"]
        engram.sets["sizes"] = ["big"]

        sets = list(engram.sets.keys())
        assert "colors" in sets
        assert "sizes" in sets

    def test_set_pattern_matching(self) -> None:
        """Test {set:name} pattern matching through Engram."""
        engram = Engram()
        engram.sets["color"] = ["red", "blue", "green"]
        engram.store("{star1} is a nice color!", pattern="I LIKE {set:color}")

        result = engram.pattern_query("i like blue")
        assert result
        assert "blue" in result[1]
        assert "nice color" in result[2]

        # Non-set word should not match
        result = engram.pattern_query("i like purple")
        assert not result

    def test_bot_property_get_set(self) -> None:
        """Test getting and setting bot properties."""
        engram = Engram()

        # Default properties
        assert engram.bot_properties.get("name") == "ENGRAM"

        # Custom property
        engram.bot_properties["master"] = "Alice"
        assert engram.bot_properties.get("master") == "Alice"
        assert engram.bot_properties.get("unknown") is None

    def test_get_bot_properties(self) -> None:
        """Test getting all bot properties."""
        engram = Engram()
        engram.bot_properties["custom"] = "value"

        props = engram.bot_properties.copy()
        assert "name" in props
        assert "version" in props
        assert "custom" in props
        assert props["custom"] == "value"

    def test_bot_pattern_matching(self) -> None:
        """Test {bot:name} pattern matching through Engram."""
        engram = Engram()
        engram.store("Yes, that's my name!", pattern="YOUR NAME IS {bot:name}")

        result = engram.pattern_query("your name is engram")
        assert result
        assert "my name" in result[2]

        # Wrong name should not match
        result = engram.pattern_query("your name is alice")
        assert not result

    def test_sets_pattern_added_after_set(self) -> None:
        """Test that patterns can use sets added before pattern."""
        engram = Engram()

        # Add set first
        engram.sets["greeting"] = ["hello", "hi", "hey"]

        # Then add pattern using set
        engram.store("Greeting received!", pattern="{set:greeting} THERE")

        result = engram.pattern_query("hello there")
        assert result
        assert "Greeting" in result[2]

        result = engram.pattern_query("hey there")
        assert result


class TestMultiSentenceInput:
    """Tests for multi-sentence input processing."""

    def test_single_sentence_no_punctuation(self) -> None:
        """Single sentence without punctuation should work normally."""
        engram = Engram()
        engram.store("Hello to you!", pattern="HELLO")

        result = engram.pattern_query("hello")
        assert result
        assert result[2] == "Hello to you!"

    def test_single_sentence_with_punctuation(self) -> None:
        """Single sentence with punctuation should work normally."""
        engram = Engram()
        engram.store("Hello to you!", pattern="HELLO")

        result = engram.pattern_query("hello!")
        assert result
        assert result[2] == "Hello to you!"

    def test_two_sentences(self) -> None:
        """Two sentences should get two responses combined."""
        engram = Engram()
        engram.store("Hello to you!", pattern="HELLO")
        engram.store("Goodbye to you!", pattern="GOODBYE")

        result = engram.pattern_query("Hello. Goodbye.")
        assert result
        assert "Hello to you!" in result[2]
        assert "Goodbye to you!" in result[2]

    def test_three_sentences(self) -> None:
        """Three sentences should get three responses combined."""
        engram = Engram()
        engram.store("Response A", pattern="A")
        engram.store("Response B", pattern="B")
        engram.store("Response C", pattern="C")

        result = engram.pattern_query("A! B? C.")
        assert result
        assert "Response A" in result[2]
        assert "Response B" in result[2]
        assert "Response C" in result[2]

    def test_partial_match_in_multi_sentence(self) -> None:
        """Should get responses only for matched sentences."""
        engram = Engram()
        engram.store("Hello response", pattern="HELLO")
        # No pattern for "unknown"

        result = engram.pattern_query("Hello. Unknown.")
        assert result
        assert "Hello response" in result[2]
        # "Unknown" doesn't match, so only one response

    def test_returns_first_statement(self) -> None:
        """Should return the first matched statement info."""
        engram = Engram()
        stmt1_id = engram.store("First response", pattern="FIRST")
        engram.store("Second response", pattern="SECOND")

        result = engram.pattern_query("First. Second.")
        assert result
        assert result[0]["id"] == stmt1_id

    def test_multi_sentence_with_session(self) -> None:
        """Multi-sentence with session should update context."""
        engram = Engram()
        session_id = sessions.create_session(engram)
        engram.store("Hello!", pattern="HELLO")
        engram.store("Goodbye!", pattern="GOODBYE")

        result = engram.pattern_query("Hello. Goodbye.", session_id=session_id)
        assert result
        # Session context should be updated with combined response
        session = sessions.get_session(engram, session_id)
        # The 'previous_response' should be the combined response (normalized)
        assert session["previous_response"]

    def test_that_context_flows_between_sentences(self) -> None:
        """That context should flow between sentences in same input."""
        engram = Engram()
        session_id = sessions.create_session(engram)

        # First pattern sets up a question
        engram.store("Do you like pizza?", pattern="HELLO")

        # Second pattern only matches if 'that' is the pizza question
        engram.store("I like pizza too!", pattern="YES", that="DO YOU LIKE PIZZA")

        # Both in same input - the "YES" should match because
        # the first response becomes the 'that' for the second sentence
        result = engram.pattern_query("Hello. Yes.", session_id=session_id)
        assert result
        assert "pizza" in result[2].lower()

    def test_no_match_returns_none(self) -> None:
        """If no sentences match, should return None."""
        engram = Engram()
        engram.store("Hello!", pattern="HELLO")

        result = engram.pattern_query("Unknown. Also unknown.")
        assert not result

    def test_empty_input(self) -> None:
        """Empty input should return None."""
        engram = Engram()
        engram.store("Hello!", pattern="HELLO")

        result = engram.pattern_query("")
        assert not result

        result = engram.pattern_query("   ")
        assert not result


class TestStatementPriority:
    """Priority prefers a statement among equals (keyword and pattern paths)."""

    def test_pattern_query_prefers_priority(self) -> None:
        engram = Engram()
        engram.store("Standard answer", pattern="PRICING")
        engram.store("Priority answer", pattern="PRICING", priority=5)

        result = engram.pattern_query("pricing")

        assert result[2] == "Priority answer"

    def test_query_priority_outranks_recency(self) -> None:
        engram = Engram()
        engram.store("shared topic boosted", priority=1)
        engram.store("shared topic plain")

        result = engram.query("shared topic")

        assert result["matches"][0][0]["text"] == "shared topic boosted"
        assert result["matches"][0][1] > 1.0  # priority added on top of the calibrated score

    def test_priority_requires_a_match(self) -> None:
        engram = Engram()
        engram.store("boosted but unrelated", priority=5)

        result = engram.query("quantum tensor")

        assert result["matches"] == []


class TestDecayStatistics:
    """Tests for aging hit statistics."""

    def _engram_with_stats(self) -> tuple:
        engram = Engram()
        stmt_id = engram.store("Paris is the capital of France")
        result = {}
        for _ in range(4):
            result = engram.query("capital of France")
        engram.record_hit(result["keywords"], statement_id=stmt_id)
        engram.record_hit(result["keywords"], statement_id=stmt_id)
        return engram, stmt_id

    def test_decay_halves_counts(self) -> None:
        engram, stmt_id = self._engram_with_stats()

        changed = metrics.decay_statistics(engram, factor=0.5)

        assert changed > 0
        stmt = engram.get_statement(stmt_id)
        assert stmt["query_count"] == 2
        assert stmt["hit_count"] == 1
        assert engram.keywords["capital"]["query_count"] == 2
        assert engram.keywords["capital"]["hit_count"] == 1

    def test_repeated_decay_returns_entry_to_no_history(self) -> None:
        """An entry that stops re-earning statistics eventually loses protection."""
        engram, stmt_id = self._engram_with_stats()

        for _ in range(4):
            metrics.decay_statistics(engram, factor=0.5)

        stmt = engram.get_statement(stmt_id)
        assert stmt["query_count"] == 0
        assert stmt["hit_count"] == 0
        # With no query history it is evictable again despite min_hit_rate.
        engram.config["min_hit_rate"] = 0.3
        candidates = eviction.get_eviction_candidates(engram)
        assert any(s["id"] == stmt_id for _, s in candidates)

    def test_decay_zero_resets_everything(self) -> None:
        engram, stmt_id = self._engram_with_stats()

        metrics.decay_statistics(engram, factor=0.0)

        stmt = engram.get_statement(stmt_id)
        assert stmt["query_count"] == 0
        assert all(e["query_count"] == 0 and e["hit_count"] == 0 for e in engram.keywords.values())

    def test_decay_validates_factor(self) -> None:
        engram = Engram()
        with pytest.raises(ValueError):
            metrics.decay_statistics(engram, factor=1.0)
        with pytest.raises(ValueError):
            metrics.decay_statistics(engram, factor=-0.1)


class TestSyncCorpus:
    """Tests for seed refresh (upserting pairs into an existing store)."""

    def test_updates_stale_template_in_place(self) -> None:
        engram = Engram()
        stmt_id = engram.store("Nice to know.", pattern="I AM *", template={"text": "old {star1}!"}, tier=Tier.STATIC)
        stmt = engram.get_statement(stmt_id)
        record_statement_hit(stmt)

        counts = engram.sync_corpus([{"pattern": "I AM *", "response": "Nice to know.", "template": {"text": "new {star1}."}}])

        assert counts == {"added": 0, "updated": 1, "unchanged": 0, "pruned": 0}
        refreshed = engram.get_statement(stmt_id)
        assert refreshed["template"] == {"text": "new {star1}."}
        assert refreshed["hit_count"] == 1  # statistics preserved

    def test_adds_missing_pairs(self) -> None:
        engram = Engram()

        counts = engram.sync_corpus([{"pattern": "HELLO", "response": "Hi there!"}])

        assert counts == {"added": 1, "updated": 0, "unchanged": 0, "pruned": 0}
        assert engram.pattern_query("hello")[2] == "Hi there!"

    def test_unchanged_pairs_counted(self) -> None:
        engram = Engram()
        pairs = [{"pattern": "HELLO", "response": "Hi there!"}]
        engram.sync_corpus(pairs)

        counts = engram.sync_corpus(pairs)

        assert counts == {"added": 0, "updated": 0, "unchanged": 1, "pruned": 0}
        assert metrics.get_statement_count(engram) == 1

    def test_dynamic_content_untouched(self) -> None:
        engram = Engram()
        learned_id = engram.learn_from_response("who acquired github", "Microsoft acquired GitHub.")
        # A DYNAMIC statement sharing a seed pattern is not the seed's entry
        dynamic_id = engram.store("Dynamic hello", pattern="HELLO", tier=Tier.DYNAMIC)

        counts = engram.sync_corpus([{"pattern": "HELLO", "response": "Hi there!"}])

        assert counts["added"] == 1  # synced as a separate STATIC statement
        assert engram.get_statement(learned_id)["text"] == "Microsoft acquired GitHub."
        assert engram.get_statement(dynamic_id)["text"] == "Dynamic hello"

    def test_plain_text_pairs_dedup_by_text(self) -> None:
        engram = Engram()
        engram.store("Plain fact statement", tier=Tier.STATIC)

        counts = engram.sync_corpus([{"response": "Plain fact statement"}])

        assert counts == {"added": 0, "updated": 0, "unchanged": 1, "pruned": 0}


class TestInputOutputCleanup:
    """Integration tests for spelling correction and response polish."""

    def test_pattern_query_corrects_typo(self) -> None:
        engram = Engram()
        engram.store("Cats are small felines.", pattern="TELL ME ABOUT CATS")

        result = engram.pattern_query("tell me abotu cats")

        assert result[2] == "Cats are small felines."

    def test_query_corrects_typo(self) -> None:
        engram = Engram()
        engram.store("The capital of France is Paris.")

        result = engram.query("capitla of france")

        assert result["matches"]
        assert "capital" in result["keywords"]

    def test_spell_correction_disabled(self) -> None:
        config = engram_config(use_spell_correction=False)
        engram = Engram(config=config)
        engram.store("Cats are small felines.", pattern="TELL ME ABOUT CATS")

        assert engram.pattern_query("tell me abotu cats") == ()

    def test_response_polish_repairs_casing(self) -> None:
        engram = Engram()
        engram.store("you said {star1}. i heard you.", pattern="ECHO *")

        result = engram.pattern_query("echo something loud")

        assert result[2] == "You said something loud. I heard you."

    def test_response_polish_disabled(self) -> None:
        config = engram_config(polish_responses=False)
        engram = Engram(config=config)
        engram.store("you said {star1}.", pattern="ECHO *")

        result = engram.pattern_query("echo something")

        assert result[2] == "you said something."

    def test_sentiment_clause_flow_end_to_end(self) -> None:
        """The transcript scenario: compound 'I am' input gets a clean, trimmed reply."""
        engram = Engram()
        engram.store(
            "Nice to know.",
            pattern="I AM *",
            template={
                "sequence": [
                    {"set": {"name": "_mood", "value": "{sentiment:{star1}}"}},
                    {
                        "condition": {
                            "name": "_mood",
                            "branches": [
                                {
                                    "value": "negative",
                                    "then": {"text": "i'm sorry to hear you're {clause:{star1}}. Want to talk about it?"},
                                },
                                {"then": {"text": "Nice to know you're {clause:{star1}}."}},
                            ],
                        }
                    },
                ]
            },
        )

        result = engram.pattern_query("I'm tired, I have been working really hard.")

        assert result[2] == "I'm sorry to hear you're tired. Want to talk about it?"


class TestQuestionAwareCatchall:
    """The catch-all template can branch on input intent via {qtype:...}."""

    def _engram_with_intent_catchall(self) -> Engram:
        engram = Engram()
        engram.store(
            "Go on.",
            pattern="*",
            tier=Tier.STATIC,
            template={
                "sequence": [
                    {"set": {"name": "_kind", "value": "{qtype:{request}}"}},
                    {
                        "condition": {
                            "name": "_kind",
                            "branches": [
                                {"value": "question", "then": {"text": "I don't know that one yet."}},
                                {"then": {"text": "Tell me more about that."}},
                            ],
                        }
                    },
                ]
            },
        )
        return engram

    def test_question_gets_question_fallback(self) -> None:
        engram = self._engram_with_intent_catchall()
        result = engram.pattern_query("Where is the nearest coffee shop?")
        assert result[2] == "I don't know that one yet."

    def test_statement_gets_statement_fallback(self) -> None:
        engram = self._engram_with_intent_catchall()
        result = engram.pattern_query("The coffee here tastes burnt")
        assert result[2] == "Tell me more about that."


class TestScratchPredicates:
    """Underscore-prefixed predicates are template-local, never session state."""

    def test_scratch_predicate_does_not_persist(self) -> None:
        engram = Engram()
        engram.store(
            "Noted.",
            pattern="I AM *",
            template={
                "sequence": [
                    {"set": {"name": "_mood", "value": "{sentiment:{star1}}"}},
                    {"set": {"name": "last_feeling", "value": "{clause:{star1}}"}},
                    {"text": "Noted."},
                ]
            },
        )
        session_id = sessions.create_session(engram, session_id="scratch")

        engram.pattern_query("I am delighted", session_id=session_id)

        predicates = engram.sessions["scratch"]["predicates"]
        assert "_mood" not in predicates
        assert predicates["last_feeling"] == "delighted"

    def test_leaked_scratch_purged_from_existing_sessions(self) -> None:
        engram = Engram()
        engram.store("Okay.", pattern="HELLO")
        session_id = sessions.create_session(engram, session_id="legacy")
        engram.sessions["legacy"]["predicates"]["_mood"] = "stale"

        engram.pattern_query("hello", session_id=session_id)

        assert "_mood" not in engram.sessions["legacy"]["predicates"]


class TestQuerySessionSymmetry:
    def test_query_creates_missing_session(self) -> None:
        """query() creates the session like pattern_query does."""
        engram = Engram()
        engram.store("Paris is the capital of France")

        engram.query("capital of France", session_id="fresh_session")

        assert "fresh_session" in engram.sessions


class TestLearnSpellCorrection:
    def test_typo_learn_and_clean_query_share_an_entry(self) -> None:
        """learn_from_response normalizes spelling like query() does."""
        engram = Engram()
        # Seed the vocabulary so the typo has something to correct toward
        engram.store("The boiling point of water is 100 Celsius.")

        learned_id = engram.learn_from_response("boilng point of water", "It boils at 100 C.")

        stmt = engram.get_statement(learned_id)
        assert "boiling" in stmt["keywords"]
        texts = [m[0]["text"] for m in engram.query("boiling point of water")["matches"]]
        assert "It boils at 100 C." in texts


class TestSyncCorpusPrune:
    def test_prune_retires_entries_absent_from_corpus(self) -> None:
        engram = Engram()
        engram.sync_corpus([{"pattern": "KEEP ME", "response": "Kept."}, {"pattern": "DROP ME", "response": "Dropped."}])
        learned_id = engram.learn_from_response("some cached question", "Cached answer.")

        counts = engram.sync_corpus([{"pattern": "KEEP ME", "response": "Kept."}], prune=True)

        assert counts["pruned"] == 1
        assert engram.pattern_query("keep me")[2] == "Kept."
        assert engram.pattern_query("drop me") == ()
        # DYNAMIC learned content is never pruned
        assert engram.get_statement(learned_id)

    def test_prune_off_by_default(self) -> None:
        engram = Engram()
        engram.sync_corpus([{"pattern": "OLD ENTRY", "response": "Still here."}])

        counts = engram.sync_corpus([{"pattern": "NEW ENTRY", "response": "Added."}])

        assert counts["pruned"] == 0
        assert engram.pattern_query("old entry")[2] == "Still here."


class TestSoakRegressions:
    """Engine-level regressions from the 100-turn conversation soak."""

    def test_typo_question_not_learned_as_fact(self) -> None:
        """Spelling correction reveals a typo'd question before fact learning."""
        engram = Engram(config=engram_config(learn_user_facts=True))
        engram.store("Fallback.", pattern="*", tier=Tier.STATIC)
        engram.store("It is time.", pattern="WHAT TIME IS IT", tier=Tier.STATIC)

        engram.pattern_query("waht is rust")

        patterns = [s["pattern"] for s in engram.statements]
        assert "WAHT" not in patterns

    def test_possessive_sentence_not_greeted(self) -> None:
        """'His name is Rex.' must not stem-match a greeting pattern."""
        engram = Engram(config=engram_config(learn_user_facts=True))
        engram.store("Hi there!", pattern="HI *", tier=Tier.STATIC)
        engram.store("Go on.", pattern="*", tier=Tier.STATIC)

        result = engram.pattern_query("His name is Rex.")

        assert result[2] != "Hi there!"

    def test_learn_acknowledgment_rotates(self) -> None:
        from engram.constants import LEARNED_ACKNOWLEDGMENTS

        engram = Engram(config=engram_config(learn_user_facts=True))
        engram.store("Go on.", pattern="*", tier=Tier.STATIC)

        responses = set()
        for i in range(12):
            result = engram.pattern_query(f"Gadget{i} is a useful tool")
            responses.add(result[2])

        assert responses <= set(LEARNED_ACKNOWLEDGMENTS)
        assert len(responses) >= 2

    def test_default_does_not_share_user_assertions_across_sessions(self) -> None:
        engram = Engram()
        engram.store("Go on.", pattern="*", tier=Tier.STATIC)

        engram.pattern_query("The support code is 9999.", session_id="user-a")
        result = engram.pattern_query("What is the support code?", session_id="user-b")

        assert result[2] == "Go on."
        assert all(stmt["text"] != "The support code is 9999." for stmt in engram.statements)


class TestKnownFactResponses:
    """Restating or contradicting a known fact surfaces the stored belief."""

    def _taught_engram(self) -> Engram:
        engram = Engram(config=engram_config(learn_user_facts=True))
        engram.store("Go on.", pattern="*", tier=Tier.STATIC)
        engram.pattern_query("The sky is blue.")
        return engram

    def test_contradiction_surfaces_stored_fact(self) -> None:
        from engram.constants import CONFLICTING_FACT_RESPONSES

        engram = self._taught_engram()
        result = engram.pattern_query("The sky is green.")

        expected = {r.replace("{existing}", "The sky is blue.") for r in CONFLICTING_FACT_RESPONSES}
        assert result[2] in expected
        # The stored fact is untouched
        assert engram.pattern_query("What is the sky?")[2] == "The sky is blue."

    def test_restatement_confirms_stored_fact(self) -> None:
        from engram.constants import KNOWN_FACT_RESPONSES

        engram = self._taught_engram()
        result = engram.pattern_query("The sky is blue.")

        expected = {r.replace("{existing}", "The sky is blue.") for r in KNOWN_FACT_RESPONSES}
        assert result[2] in expected


class TestFactContentRetrieval:
    """Learned facts are keyword-indexed under their full content."""

    def test_yes_no_question_reaches_cache(self) -> None:
        from engram import pipeline

        engram = Engram(config=engram_config(learn_user_facts=True))
        engram.store("Go on.", pattern="*", tier=Tier.STATIC)
        engram.pattern_query("The sky is blue.")

        result = pipeline.respond(engram, "Is the sky blue?")

        assert result["source"] == "cache"
        assert result["response"] == "The sky is blue."

    def test_who_is_pattern_generated(self) -> None:
        engram = Engram(config=engram_config(learn_user_facts=True))
        engram.store("Go on.", pattern="*", tier=Tier.STATIC)
        engram.pattern_query("Rex is a golden retriever.")

        assert engram.pattern_query("Who is rex?")[2] == "Rex is a golden retriever."
