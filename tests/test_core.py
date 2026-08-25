"""Tests for core ENGRAM implementation."""

import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

import pytest

from engram import eviction, metrics, persistence, pipeline, sessions
from engram.config import engram_config, graph_config
from engram.constants import SessionOverflow, Tier
from engram.core import Engram, fork_engram
from engram.models import record_statement_hit, record_statement_query, session_update_context
from engram.sessions import SessionLimitExceededError, SessionNotFoundError


def test_component_preflight_requires_offline_nltk_resources(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("engram.core.ensure_nltk_data", lambda download=False: [("corpora/missing", "missing")])

    with pytest.raises(ValueError, match="required NLTK resources are unavailable: missing"):
        Engram()


def test_component_preflight_reports_nltk_readiness() -> None:
    assert Engram().component_status["nltk"] == {"enabled": True, "ready": True}


def test_component_preflight_reports_disabled_sparse_readiness_independently() -> None:
    engine = Engram()

    assert engine.component_status["sparse"] == {"enabled": False, "ready": False}


"""Tests for statement storage."""


def test_engram_store_store_basic() -> None:
    engram = Engram()
    stmt_id = engram.store("Hello world")

    assert stmt_id.startswith("stmt_")
    assert metrics.get_statement_count(engram) == 1


def test_engram_store_store_static() -> None:
    engram = Engram()
    engram.store("Static statement", tier=Tier.STATIC)

    assert metrics.get_static_count(engram) == 1
    assert metrics.get_dynamic_count(engram) == 0


def test_engram_store_store_keywords_indexed() -> None:
    engram = Engram()
    engram.store("Paris is the capital of France")

    assert metrics.get_keyword_count(engram) > 0


def test_engram_store_store_capacity_eviction() -> None:
    config = engram_config(capacity=3)
    engram = Engram(config=config)

    engram.store("First", tier=Tier.DYNAMIC)
    engram.store("Second", tier=Tier.DYNAMIC)
    engram.store("Third", tier=Tier.DYNAMIC)
    engram.store("Fourth", tier=Tier.DYNAMIC)

    assert metrics.get_dynamic_count(engram) == 3
    assert engram.eviction_count == 1


def test_engram_store_store_static_not_evicted() -> None:
    config = engram_config(capacity=2)
    engram = Engram(config=config)

    engram.store("Static", tier=Tier.STATIC)
    engram.store("Dynamic 1", tier=Tier.DYNAMIC)
    engram.store("Dynamic 2", tier=Tier.DYNAMIC)
    engram.store("Dynamic 3", tier=Tier.DYNAMIC)

    assert metrics.get_static_count(engram) == 1
    assert metrics.get_dynamic_count(engram) == 2


def test_engram_store_duplicate_statement_id_is_rejected_without_mutation() -> None:
    engram = Engram()
    engram.store("First", statement_id="fixed", pattern="FIRST")

    with pytest.raises(ValueError, match="duplicate statement id"):
        engram.store("Second", statement_id="fixed", pattern="SECOND")

    assert len(engram.statements) == 1
    assert engram.pattern_query("first")[2] == "First"
    assert not engram.pattern_query("second")


"""Tests for query operations."""


def test_engram_query_query_basic() -> None:
    engram = Engram()
    engram.store("Paris is the capital of France")

    result = engram.query("What is the capital of France?")

    assert len(result["matches"]) == 1
    assert "Paris" in result["matches"][0][0]["text"]


def test_engram_query_query_multiple_matches() -> None:
    engram = Engram()
    engram.store("Paris is the capital of France")
    engram.store("France has a population of 67 million")
    engram.store("The Eiffel Tower is in Paris")

    result = engram.query("France Paris")

    assert len(result["matches"]) >= 2


def test_engram_query_query_limit() -> None:
    engram = Engram()
    for i in range(10):
        engram.store(f"France statement number {i}")

    result = engram.query("France", limit=3)

    assert len(result["matches"]) == 3


def test_engram_query_query_no_matches() -> None:
    engram = Engram()
    engram.store("Hello world")

    result = engram.query("quantum tensor")

    assert len(result["matches"]) == 0


def test_engram_query_query_synonym_match_scores_discounted() -> None:
    """A synonym-only match surfaces with less than exact-match credit."""
    engram = Engram()
    engram.store("The automobile is fast")

    with_synonym = engram.query("car")

    assert len(with_synonym["matches"]) == 1
    _, score = with_synonym["matches"][0]
    assert 0.0 < score < 0.5  # discounted below a same-shape exact match


def test_engram_query_query_stopwords_only() -> None:
    engram = Engram()
    engram.store("Hello world")

    result = engram.query("the is are")

    assert len(result["keywords"]) == 0
    assert len(result["matches"]) == 0


def test_engram_query_query_increments_query_count() -> None:
    engram = Engram()
    engram.store("Paris France")

    engram.query("Paris")
    engram.query("Paris")
    engram.query("Paris")

    assert engram.query_count == 3


def test_engram_query_query_with_session_context() -> None:
    engram = Engram()
    engram.store("Paris is the capital of France")
    engram.store("France has a population of 67 million")

    session_id = sessions.create_session(engram)
    sessions.update_session_context(engram, session_id, "Paris is the capital of France")

    # "population" alone might not match, but with context it should
    result = engram.query("What is its population?", session_id=session_id)

    # Context expansion adds "Paris" and "France" keywords
    assert len(result["keywords"]) > 1


"""Tests for hit recording."""


def test_engram_record_hit_record_hit() -> None:
    engram = Engram()
    engram.store("Paris France")

    engram.query("Paris")
    engram.record_hit(["paris"])

    assert engram.hit_count == 1


def test_engram_record_hit_record_hit_multiple_keywords() -> None:
    engram = Engram()
    engram.store("Paris is the capital of France")

    engram.query("Paris France")
    engram.record_hit(["paris", "france"])

    assert engram.hit_count == 1


def test_engram_record_hit_query_records_statement_candidacy() -> None:
    """Each returned match counts as a query against that statement."""
    engram = Engram()
    stmt_id = engram.store("Paris is the capital of France")

    engram.query("capital of France")

    stmt = engram.get_statement(stmt_id)
    assert stmt["query_count"] == 1
    assert stmt["hit_count"] == 0


def test_engram_record_hit_record_hit_with_statement_id() -> None:
    """record_hit credits the answering statement when its id is given."""
    engram = Engram()
    stmt_id = engram.store("Paris is the capital of France")

    result = engram.query("capital of France")
    engram.record_hit(result["keywords"], statement_id=stmt_id)

    stmt = engram.get_statement(stmt_id)
    assert stmt["hit_count"] == 1
    assert stmt["last_hit"]


def test_engram_record_hit_record_hit_unknown_statement_id() -> None:
    """An unknown statement id updates keyword stats and nothing else."""
    engram = Engram()
    engram.store("Paris is the capital of France")

    engram.query("capital of France")
    engram.record_hit(["capital"], statement_id="stmt_missing")

    assert engram.hit_count == 1


def test_engram_record_hit_pattern_query_records_statement_usage() -> None:
    """Pattern selection records both a query and a hit on the statement."""
    engram = Engram()
    stmt_id = engram.store("Hello there", pattern="HELLO")

    engram.pattern_query("hello")

    stmt = engram.get_statement(stmt_id)
    assert stmt["query_count"] == 1
    assert stmt["hit_count"] == 1
    assert stmt["last_hit"]


"""Tests for eviction."""


def test_engram_eviction_evict_manual() -> None:
    engram = Engram()
    engram.store("First", tier=Tier.DYNAMIC)
    engram.store("Second", tier=Tier.DYNAMIC)

    result = eviction.evict(engram)

    assert result is True
    assert metrics.get_dynamic_count(engram) == 1


def test_engram_eviction_evict_empty() -> None:
    engram = Engram()
    result = eviction.evict(engram)
    assert result is False


def test_engram_eviction_evict_only_static() -> None:
    engram = Engram()
    engram.store("Static", tier=Tier.STATIC)

    result = eviction.evict(engram)

    assert result is False
    assert metrics.get_static_count(engram) == 1


def test_engram_eviction_clear_dynamic() -> None:
    engram = Engram()
    engram.store("Static", tier=Tier.STATIC)
    engram.store("Dynamic 1", tier=Tier.DYNAMIC)
    engram.store("Dynamic 2", tier=Tier.DYNAMIC)

    count = eviction.clear_dynamic(engram)

    assert count == 2
    assert metrics.get_static_count(engram) == 1
    assert metrics.get_dynamic_count(engram) == 0


"""Tests for different eviction policies."""


def test_eviction_policies_fifo_eviction() -> None:
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


def test_eviction_policies_lru_eviction() -> None:
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


def test_eviction_policies_lfu_eviction() -> None:
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


def test_eviction_policies_hit_rate_eviction() -> None:
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


def test_eviction_policies_min_hit_rate_protection() -> None:
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


"""Tests for session management."""


def test_engram_sessions_create_session() -> None:
    engram = Engram()
    session_id = sessions.create_session(engram)

    assert session_id.startswith("sess_")
    assert metrics.get_session_count(engram) == 1


def test_engram_sessions_create_session_with_id() -> None:
    engram = Engram()
    session_id = sessions.create_session(engram, session_id="user_abc")

    assert session_id == "user_abc"


def test_engram_sessions_create_session_with_metadata() -> None:
    engram = Engram()
    sessions.create_session(engram, session_id="test", metadata={"user_id": "123"})

    session = sessions.get_session(engram, "test")
    assert session["metadata"]["user_id"] == "123"


def test_engram_sessions_get_session() -> None:
    engram = Engram()
    sessions.create_session(engram, session_id="test")

    session = sessions.get_session(engram, "test")

    assert session
    assert session["session_id"] == "test"


def test_engram_sessions_get_session_create_if_missing() -> None:
    engram = Engram()

    session = sessions.get_session(engram, "new_session", create_if_missing=True)

    assert session
    assert session["session_id"] == "new_session"


def test_engram_sessions_get_session_not_found() -> None:
    engram = Engram()

    session = sessions.get_session(engram, "nonexistent", create_if_missing=False)

    assert not session


def test_engram_sessions_update_session_context() -> None:
    engram = Engram()
    sessions.create_session(engram, session_id="test")

    sessions.update_session_context(engram, "test", "Previous response")

    session = sessions.get_session(engram, "test")
    assert session["previous_response"] == "Previous response"


def test_engram_sessions_update_session_context_not_found() -> None:
    engram = Engram()

    with pytest.raises(SessionNotFoundError):
        sessions.update_session_context(engram, "nonexistent", "Response")


def test_engram_sessions_delete_session() -> None:
    engram = Engram()
    sessions.create_session(engram, session_id="test")

    result = sessions.delete_session(engram, "test")

    assert result is True
    assert metrics.get_session_count(engram) == 0


def test_engram_sessions_delete_session_not_found() -> None:
    engram = Engram()

    result = sessions.delete_session(engram, "nonexistent")

    assert result is False


def test_engram_sessions_expire_sessions() -> None:
    engram = Engram()
    sessions.create_session(engram, session_id="old")
    sessions.create_session(engram, session_id="new")

    # Manually set old session's last_active to past
    old_session = sessions.get_session(engram, "old")
    old_session["last_active"] = datetime.now(UTC) - timedelta(hours=2)

    count = sessions.expire_sessions(engram, inactive_threshold=timedelta(hours=1))

    assert count == 1
    assert metrics.get_session_count(engram) == 1


def test_engram_sessions_list_sessions() -> None:
    engram = Engram()
    sessions.create_session(engram, session_id="a")
    sessions.create_session(engram, session_id="b")
    sessions.create_session(engram, session_id="c")

    session_list = sessions.list_sessions(engram)

    assert len(session_list) == 3


def test_engram_sessions_list_sessions_filtered() -> None:
    engram = Engram()
    sessions.create_session(engram, session_id="old")
    sessions.create_session(engram, session_id="new")

    # Set old session to past
    old_session = sessions.get_session(engram, "old")
    old_session["last_active"] = datetime.now(UTC) - timedelta(hours=2)

    session_list = sessions.list_sessions(engram, active_since=datetime.now(UTC) - timedelta(hours=1))

    assert len(session_list) == 1


def test_engram_sessions_session_limit_lru() -> None:
    config = engram_config(max_sessions=2, session_overflow=SessionOverflow.LRU)
    engram = Engram(config=config)

    sessions.create_session(engram, session_id="first")
    sessions.create_session(engram, session_id="second")
    sessions.create_session(engram, session_id="third")

    assert metrics.get_session_count(engram) == 2
    assert not sessions.get_session(engram, "first", create_if_missing=False)


def test_engram_sessions_session_limit_reject() -> None:
    config = engram_config(max_sessions=2, session_overflow=SessionOverflow.REJECT)
    engram = Engram(config=config)

    sessions.create_session(engram, session_id="first")
    sessions.create_session(engram, session_id="second")

    with pytest.raises(SessionLimitExceededError):
        sessions.create_session(engram, session_id="third")


"""Tests for persistence."""


def test_engram_persistence_save_load_file() -> None:
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


def test_engram_persistence_save_load_json_string() -> None:
    engram = Engram()
    engram.store("Test statement")

    json_str = persistence.save_json(engram)
    loaded = persistence.load_engram_json(json_str)

    assert metrics.get_statement_count(loaded) == 1


def test_engram_persistence_to_dict_from_dict() -> None:
    engram = Engram()
    engram.store("Statement 1")
    engram.store("Statement 2")

    data = persistence.to_dict(engram)
    loaded = persistence.load_engram_from_dict(data)

    assert metrics.get_statement_count(loaded) == 2


def test_engram_persistence_persistence_preserves_user_context_and_fact_provenance() -> None:
    engram = Engram()
    engram.store("Go on.", pattern="*", tier=Tier.STATIC)

    pipeline.chat(engram, "Sushi is good.", user_id="Alice")
    research_id = engram.add_fact(
        "Tokyo is the capital of Japan.",
        source_label="research-tool",
    )

    loaded = persistence.load_engram_json(persistence.save_json(engram))

    sushi_id = loaded.pattern_to_statement["SUSHI"]
    sushi = loaded.get_statement(sushi_id)
    assert sushi["introduced_by_user_id"] == "Alice"
    assert sushi["source_label"] == ""

    research = loaded.get_statement(research_id)
    assert research["introduced_by_user_id"] == ""
    assert research["source_label"] == "research-tool"
    assert "Alice" in loaded.sessions


def test_engram_persistence_legacy_statement_without_provenance_still_loads() -> None:
    engram = Engram()
    engram.store(
        "Legacy statement",
        introduced_by_user_id="old-user",
        source_label="old-source",
    )
    data = persistence.to_dict(engram)
    data["statements"][0].pop("introduced_by_user_id")
    data["statements"][0].pop("source_label")

    loaded = persistence.load_engram_from_dict(data)
    statement = loaded.statements[0]

    assert statement["introduced_by_user_id"] == ""
    assert statement["source_label"] == ""


def test_engram_persistence_persistence_preserves_statistics() -> None:
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


def test_engram_persistence_persistence_preserves_config() -> None:
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


def test_engram_persistence_persistence_config_override_wins() -> None:
    """An explicit config passed to the loader beats the stored one."""
    engram = Engram(config=engram_config(capacity=123))
    engram.store("Paris France")

    json_str = persistence.save_json(engram)
    override = engram_config(capacity=456)
    loaded = persistence.load_engram_json(json_str, config=override)

    assert loaded.config["capacity"] == 456


def test_engram_persistence_persistence_omits_graph_password() -> None:
    config = engram_config(graph=graph_config(username="reader", password="secret"))
    engram = Engram(config=config)

    data = persistence.to_dict(engram)

    assert "password" not in data["config"]["graph"]
    loaded = persistence.load_engram_from_dict(data)
    assert loaded.config["graph"]["password"] == ""


def test_engram_persistence_duplicate_statement_id_in_persistence_is_rejected() -> None:
    engram = Engram()
    engram.store("First", statement_id="fixed")
    data = persistence.to_dict(engram)
    data["statements"].append(dict(data["statements"][0]))

    with pytest.raises(ValueError, match="duplicate statement id"):
        persistence.load_engram_from_dict(data)


def test_engram_persistence_load_legacy_state_without_config() -> None:
    """Files written before the config block still load, keeping capacity."""
    engram = Engram(config=engram_config(capacity=123))
    engram.store("Paris France")

    data = persistence.to_dict(engram)
    del data["config"]
    loaded = persistence.load_engram_from_dict(data)

    assert loaded.config["capacity"] == 123
    assert metrics.get_statement_count(loaded) == 1


def test_engram_persistence_save_load_sessions_only() -> None:
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


def test_engram_persistence_rebuild_index() -> None:
    engram = Engram()
    engram.store("Paris France")
    engram.store("London England")

    # Clear keywords manually
    engram.keywords.clear()
    assert metrics.get_keyword_count(engram) == 0

    # Rebuild
    persistence.rebuild_index(engram)

    assert metrics.get_keyword_count(engram) > 0


def test_engram_persistence_persistence_bot_properties() -> None:
    """Bot properties should persist."""
    engram = Engram()
    engram.bot_properties["master"] = "Alice"
    engram.bot_properties["custom"] = "value"

    data = persistence.to_dict(engram)
    loaded = persistence.load_engram_from_dict(data)

    assert loaded.bot_properties.get("name") == "ENGRAM"  # Default preserved
    assert loaded.bot_properties.get("master") == "Alice"
    assert loaded.bot_properties.get("custom") == "value"


def test_engram_persistence_persistence_sets() -> None:
    """Word sets should persist."""
    engram = Engram()
    engram.sets["colors"] = ["red", "blue", "green"]
    engram.sets["sizes"] = ["big", "small"]

    data = persistence.to_dict(engram)
    loaded = persistence.load_engram_from_dict(data)

    assert loaded.sets.get("colors") == ["red", "blue", "green"]
    assert loaded.sets.get("sizes") == ["big", "small"]


def test_engram_persistence_persistence_maps() -> None:
    """Maps should persist."""
    engram = Engram()
    engram.maps["capital"] = {"france": "paris", "germany": "berlin"}
    engram.maps["successor"] = {"1": "2", "2": "3"}

    data = persistence.to_dict(engram)
    loaded = persistence.load_engram_from_dict(data)

    assert loaded.maps["capital"]["france"] == "paris"
    assert loaded.maps["successor"]["1"] == "2"


def test_engram_persistence_persistence_substitutions() -> None:
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


def test_engram_persistence_persistence_sets_work_with_patterns() -> None:
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


"""Tests for initialization patterns."""


def test_engram_initialization_load_corpus() -> None:
    engram = Engram()
    corpus = [
        "Paris is the capital of France",
        "London is the capital of England",
        "Berlin is the capital of Germany",
    ]

    count = engram.load_corpus(corpus, tier=Tier.STATIC)

    assert count == 3
    assert metrics.get_static_count(engram) == 3


def test_engram_initialization_fork() -> None:
    parent = Engram()
    parent.store("Static from parent", tier=Tier.STATIC)
    parent.store("Dynamic from parent", tier=Tier.DYNAMIC)
    sessions.create_session(parent, session_id="parent_session")

    child = fork_engram(
        parent,
        static_corpus=["New static"],
    )

    # Should have new static
    assert metrics.get_static_count(child) == 1
    # Should have copied dynamic
    assert metrics.get_dynamic_count(child) == 1
    # Should NOT have parent sessions
    assert metrics.get_session_count(child) == 0


def test_engram_initialization_fork_copies_dynamic_patterns() -> None:
    """Scripted DYNAMIC statements survive the fork with their patterns."""
    parent = Engram()
    parent.store("Dynamic greeting", pattern="HI THERE", tier=Tier.DYNAMIC)

    child = fork_engram(parent)

    result = child.pattern_query("hi there")
    assert result[2] == "Dynamic greeting"


"""Tests for metrics."""


def test_engram_metrics_get_metrics() -> None:
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


def test_engram_metrics_overall_hit_rate() -> None:
    engram = Engram()
    engram.store("Paris France")

    engram.query("Paris")
    engram.query("Paris")
    engram.record_hit(["paris"])

    assert metrics.get_overall_hit_rate(engram) == 0.5


def test_engram_metrics_get_low_hit_keywords() -> None:
    engram = Engram()
    engram.store("Paris France")

    # Simulate many queries with few hits
    for _ in range(20):
        engram.query("Paris")
    engram.record_hit(["paris"])

    results = metrics.get_low_hit_keywords(engram, min_queries=10, max_hit_rate=0.2)

    assert len(results) >= 1


def test_engram_metrics_get_zero_hit_keywords() -> None:
    engram = Engram()
    engram.store("Paris France")

    for _ in range(15):
        engram.query("Paris")
    # No hits recorded

    results = metrics.get_zero_hit_keywords(engram, min_queries=10)

    assert len(results) >= 1


def test_engram_metrics_get_coverage_gaps() -> None:
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


def test_engram_metrics_get_coverage_report() -> None:
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


def test_engram_metrics_get_coverage_report_recommendations() -> None:
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


"""Tests for context-aware pattern matching with that/topic."""


def test_engram_context_matching_pattern_with_topic() -> None:
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


def test_engram_context_matching_pattern_with_that() -> None:
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


def test_engram_context_matching_pattern_with_topic_and_that() -> None:
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


def test_engram_context_matching_thatstar_in_template() -> None:
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


def test_engram_context_matching_context_persisted() -> None:
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


"""Tests for word sets and bot properties."""


def test_engram_sets_and_bot_properties_add_and_get_set() -> None:
    """Test adding and retrieving word sets."""
    engram = Engram()
    engram.sets["colors"] = ["red", "blue", "green"]

    assert engram.sets.get("colors") == ["red", "blue", "green"]
    assert engram.sets.get("unknown", []) == []


def test_engram_sets_and_bot_properties_remove_set() -> None:
    """Test removing word sets."""
    engram = Engram()
    engram.sets["colors"] = ["red", "blue"]

    del engram.sets["colors"]
    assert engram.sets.get("colors", []) == []


def test_engram_sets_and_bot_properties_list_sets() -> None:
    """Test listing all set names."""
    engram = Engram()
    engram.sets["colors"] = ["red"]
    engram.sets["sizes"] = ["big"]

    sets = list(engram.sets.keys())
    assert "colors" in sets
    assert "sizes" in sets


def test_engram_sets_and_bot_properties_set_pattern_matching() -> None:
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


def test_engram_sets_and_bot_properties_bot_property_get_set() -> None:
    """Test getting and setting bot properties."""
    engram = Engram()

    # Default properties
    assert engram.bot_properties.get("name") == "ENGRAM"

    # Custom property
    engram.bot_properties["master"] = "Alice"
    assert engram.bot_properties.get("master") == "Alice"
    assert engram.bot_properties.get("unknown", "") == ""


def test_engram_sets_and_bot_properties_get_bot_properties() -> None:
    """Test getting all bot properties."""
    engram = Engram()
    engram.bot_properties["custom"] = "value"

    props = engram.bot_properties.copy()
    assert "name" in props
    assert "version" in props
    assert "custom" in props
    assert props["custom"] == "value"


def test_engram_sets_and_bot_properties_bot_pattern_matching() -> None:
    """Test {bot:name} pattern matching through Engram."""
    engram = Engram()
    engram.store("Yes, that's my name!", pattern="YOUR NAME IS {bot:name}")

    result = engram.pattern_query("your name is engram")
    assert result
    assert "my name" in result[2]

    # Wrong name should not match
    result = engram.pattern_query("your name is alice")
    assert not result


def test_engram_sets_and_bot_properties_sets_pattern_added_after_set() -> None:
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


"""Tests for multi-sentence input processing."""


def test_multi_sentence_input_two_sentences() -> None:
    """Two sentences should get two responses combined."""
    engram = Engram()
    engram.store("Hello to you!", pattern="HELLO")
    engram.store("Goodbye to you!", pattern="GOODBYE")

    result = engram.pattern_query("Hello. Goodbye.")
    assert result
    assert "Hello to you!" in result[2]
    assert "Goodbye to you!" in result[2]


def test_multi_sentence_input_returns_first_statement() -> None:
    """Should return the first matched statement info."""
    engram = Engram()
    stmt1_id = engram.store("First response", pattern="FIRST")
    engram.store("Second response", pattern="SECOND")

    result = engram.pattern_query("First. Second.")
    assert result
    assert result[0]["id"] == stmt1_id


def test_multi_sentence_input_that_context_flows_between_sentences() -> None:
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


def test_multi_sentence_input_no_match_returns_none() -> None:
    """If no sentences match, should return None."""
    engram = Engram()
    engram.store("Hello!", pattern="HELLO")

    result = engram.pattern_query("Unknown. Also unknown.")
    assert not result


def test_multi_sentence_input_empty_input() -> None:
    """Empty input should return None."""
    engram = Engram()
    engram.store("Hello!", pattern="HELLO")

    result = engram.pattern_query("")
    assert not result

    result = engram.pattern_query("   ")
    assert not result


"""Priority prefers a statement among equals (keyword and pattern paths)."""


def test_statement_priority_pattern_query_prefers_priority() -> None:
    engram = Engram()
    engram.store("Standard answer", pattern="PRICING")
    engram.store("Priority answer", pattern="PRICING", priority=5)

    result = engram.pattern_query("pricing")

    assert result[2] == "Priority answer"


def test_statement_priority_query_priority_outranks_recency() -> None:
    engram = Engram()
    engram.store("shared topic boosted", priority=1)
    engram.store("shared topic plain")

    result = engram.query("shared topic")

    assert result["matches"][0][0]["text"] == "shared topic boosted"
    assert result["matches"][0][1] > 1.0  # priority added on top of the calibrated score


def test_statement_priority_priority_requires_a_match() -> None:
    engram = Engram()
    engram.store("boosted but unrelated", priority=5)

    result = engram.query("quantum tensor")

    assert result["matches"] == []


"""Tests for aging hit statistics."""


def _decay_statistics_engram_with_stats() -> tuple:
    engram = Engram()
    stmt_id = engram.store("Paris is the capital of France")
    result = {}
    for _ in range(4):
        result = engram.query("capital of France")
    engram.record_hit(result["keywords"], statement_id=stmt_id)
    engram.record_hit(result["keywords"], statement_id=stmt_id)
    result = engram, stmt_id
    return result


def test_decay_statistics_decay_halves_counts() -> None:
    engram, stmt_id = _decay_statistics_engram_with_stats()

    changed = metrics.decay_statistics(engram, factor=0.5)

    assert changed > 0
    stmt = engram.get_statement(stmt_id)
    assert stmt["query_count"] == 2
    assert stmt["hit_count"] == 1
    assert engram.keywords["capital"]["query_count"] == 2
    assert engram.keywords["capital"]["hit_count"] == 1


def test_decay_statistics_repeated_decay_returns_entry_to_no_history() -> None:
    """An entry that stops re-earning statistics eventually loses protection."""
    engram, stmt_id = _decay_statistics_engram_with_stats()

    for _ in range(4):
        metrics.decay_statistics(engram, factor=0.5)

    stmt = engram.get_statement(stmt_id)
    assert stmt["query_count"] == 0
    assert stmt["hit_count"] == 0
    # With no query history it is evictable again despite min_hit_rate.
    engram.config["min_hit_rate"] = 0.3
    candidates = eviction.get_eviction_candidates(engram)
    assert any(s["id"] == stmt_id for _, s in candidates)


def test_decay_statistics_decay_zero_resets_everything() -> None:
    engram, stmt_id = _decay_statistics_engram_with_stats()

    metrics.decay_statistics(engram, factor=0.0)

    stmt = engram.get_statement(stmt_id)
    assert stmt["query_count"] == 0
    assert all(e["query_count"] == 0 and e["hit_count"] == 0 for e in engram.keywords.values())


def test_decay_statistics_decay_validates_factor() -> None:
    engram = Engram()
    with pytest.raises(ValueError):
        metrics.decay_statistics(engram, factor=1.0)
    with pytest.raises(ValueError):
        metrics.decay_statistics(engram, factor=-0.1)


"""Tests for seed refresh (upserting pairs into an existing store)."""


def test_sync_corpus_updates_stale_template_in_place() -> None:
    engram = Engram()
    stmt_id = engram.store("Nice to know.", pattern="I AM *", template={"text": "old {star1}!"}, tier=Tier.STATIC)
    stmt = engram.get_statement(stmt_id)
    record_statement_hit(stmt)

    counts = engram.sync_corpus([{"pattern": "I AM *", "response": "Nice to know.", "template": {"text": "new {star1}."}}])

    assert counts == {"added": 0, "updated": 1, "unchanged": 0, "pruned": 0}
    refreshed = engram.get_statement(stmt_id)
    assert refreshed["template"] == {"text": "new {star1}."}
    assert refreshed["hit_count"] == 1  # statistics preserved


def test_sync_corpus_adds_missing_pairs() -> None:
    engram = Engram()

    counts = engram.sync_corpus([{"pattern": "HELLO", "response": "Hi there!"}])

    assert counts == {"added": 1, "updated": 0, "unchanged": 0, "pruned": 0}
    assert engram.pattern_query("hello")[2] == "Hi there!"


def test_sync_corpus_unchanged_pairs_counted() -> None:
    engram = Engram()
    pairs = [{"pattern": "HELLO", "response": "Hi there!"}]
    engram.sync_corpus(pairs)

    counts = engram.sync_corpus(pairs)

    assert counts == {"added": 0, "updated": 0, "unchanged": 1, "pruned": 0}
    assert metrics.get_statement_count(engram) == 1


def test_sync_corpus_dynamic_content_untouched() -> None:
    engram = Engram()
    learned_id = engram.learn_from_response("who acquired github", "Microsoft acquired GitHub.")
    # A DYNAMIC statement sharing a seed pattern is not the seed's entry
    dynamic_id = engram.store("Dynamic hello", pattern="HELLO", tier=Tier.DYNAMIC)

    counts = engram.sync_corpus([{"pattern": "HELLO", "response": "Hi there!"}])

    assert counts["added"] == 1  # synced as a separate STATIC statement
    assert engram.get_statement(learned_id)["text"] == "Microsoft acquired GitHub."
    assert engram.get_statement(dynamic_id)["text"] == "Dynamic hello"


def test_sync_corpus_plain_text_pairs_dedup_by_text() -> None:
    engram = Engram()
    engram.store("Plain fact statement", tier=Tier.STATIC)

    counts = engram.sync_corpus([{"response": "Plain fact statement"}])

    assert counts == {"added": 0, "updated": 0, "unchanged": 1, "pruned": 0}


"""Integration tests for spelling correction and response polish."""


def test_input_output_cleanup_pattern_query_corrects_typo() -> None:
    engram = Engram()
    engram.store("Cats are small felines.", pattern="TELL ME ABOUT CATS")

    result = engram.pattern_query("tell me abotu cats")

    assert result[2] == "Cats are small felines."


def test_input_output_cleanup_query_corrects_typo() -> None:
    engram = Engram()
    engram.store("The capital of France is Paris.")

    result = engram.query("capitla of france")

    assert result["matches"]
    assert "capital" in result["keywords"]


def test_input_output_cleanup_spell_correction_disabled() -> None:
    config = engram_config(use_spell_correction=False)
    engram = Engram(config=config)
    engram.store("Cats are small felines.", pattern="TELL ME ABOUT CATS")

    assert engram.pattern_query("tell me abotu cats") == ()


def test_input_output_cleanup_response_polish_repairs_casing() -> None:
    engram = Engram()
    engram.store("you said {star1}. i heard you.", pattern="ECHO *")

    result = engram.pattern_query("echo something loud")

    assert result[2] == "You said something loud. I heard you."


def test_input_output_cleanup_wildcard_capture_preserves_caller_name_casing() -> None:
    engram = Engram()
    engram.store("Nice to meet you, {star1}!", pattern="MY NAME IS *")

    result = engram.pattern_query("My name is Robin.")

    assert result[1] == ["Robin"]
    assert result[2] == "Nice to meet you, Robin!"


def test_input_output_cleanup_response_polish_disabled() -> None:
    config = engram_config(polish_responses=False)
    engram = Engram(config=config)
    engram.store("you said {star1}.", pattern="ECHO *")

    result = engram.pattern_query("echo something")

    assert result[2] == "you said something."


def test_input_output_cleanup_sentiment_clause_flow_end_to_end() -> None:
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


"""The catch-all template can branch on input intent via {qtype:...}."""


def _question_aware_catchall_engram_with_intent_catchall() -> Engram:
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


def test_question_aware_catchall_question_gets_question_fallback() -> None:
    engram = _question_aware_catchall_engram_with_intent_catchall()
    result = engram.pattern_query("Where is the nearest coffee shop?")
    assert result[2] == "I don't know that one yet."


def test_question_aware_catchall_statement_gets_statement_fallback() -> None:
    engram = _question_aware_catchall_engram_with_intent_catchall()
    result = engram.pattern_query("The coffee here tastes burnt")
    assert result[2] == "Tell me more about that."


"""Underscore-prefixed predicates are template-local, never session state."""


def test_scratch_predicates_scratch_predicate_does_not_persist() -> None:
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


def test_scratch_predicates_leaked_scratch_purged_from_existing_sessions() -> None:
    engram = Engram()
    engram.store("Okay.", pattern="HELLO")
    session_id = sessions.create_session(engram, session_id="legacy")
    engram.sessions["legacy"]["predicates"]["_mood"] = "stale"

    engram.pattern_query("hello", session_id=session_id)

    assert "_mood" not in engram.sessions["legacy"]["predicates"]


def test_query_session_symmetry_query_creates_missing_session() -> None:
    """query() creates the session like pattern_query does."""
    engram = Engram()
    engram.store("Paris is the capital of France")

    engram.query("capital of France", session_id="fresh_session")

    assert "fresh_session" in engram.sessions


def test_learn_spell_correction_typo_learn_and_clean_query_share_an_entry() -> None:
    """learn_from_response normalizes spelling like query() does."""
    engram = Engram()
    # Seed the vocabulary so the typo has something to correct toward
    engram.store("The boiling point of water is 100 Celsius.")

    learned_id = engram.learn_from_response("boilng point of water", "It boils at 100 C.")

    stmt = engram.get_statement(learned_id)
    assert "boiling" in stmt["keywords"]
    texts = [m[0]["text"] for m in engram.query("boiling point of water")["matches"]]
    assert "It boils at 100 C." in texts


def test_sync_corpus_prune_prune_retires_entries_absent_from_corpus() -> None:
    engram = Engram()
    engram.sync_corpus([{"pattern": "KEEP ME", "response": "Kept."}, {"pattern": "DROP ME", "response": "Dropped."}])
    learned_id = engram.learn_from_response("some cached question", "Cached answer.")

    counts = engram.sync_corpus([{"pattern": "KEEP ME", "response": "Kept."}], prune=True)

    assert counts["pruned"] == 1
    assert engram.pattern_query("keep me")[2] == "Kept."
    assert engram.pattern_query("drop me") == ()
    # Corpus pruning leaves DYNAMIC learned content unchanged.
    assert engram.get_statement(learned_id)


def test_sync_corpus_prune_prune_off_by_default() -> None:
    engram = Engram()
    engram.sync_corpus([{"pattern": "OLD ENTRY", "response": "Still here."}])

    counts = engram.sync_corpus([{"pattern": "NEW ENTRY", "response": "Added."}])

    assert counts["pruned"] == 0
    assert engram.pattern_query("old entry")[2] == "Still here."


"""Engine-level regressions from the 100-turn conversation soak."""


def test_soak_regressions_typo_question_not_learned_as_fact() -> None:
    """Spelling correction reveals a typo'd question before fact learning."""
    engram = Engram(config=engram_config(learn_user_facts=True))
    engram.store("Fallback.", pattern="*", tier=Tier.STATIC)
    engram.store("It is time.", pattern="WHAT TIME IS IT", tier=Tier.STATIC)

    engram.pattern_query("waht is rust")

    patterns = [s["pattern"] for s in engram.statements]
    assert "WAHT" not in patterns


def test_soak_regressions_possessive_sentence_not_greeted() -> None:
    """'His name is Rex.' must not stem-match a greeting pattern."""
    engram = Engram(config=engram_config(learn_user_facts=True))
    engram.store("Hi there!", pattern="HI *", tier=Tier.STATIC)
    engram.store("Go on.", pattern="*", tier=Tier.STATIC)

    result = engram.pattern_query("His name is Rex.")

    assert result[2] != "Hi there!"


def test_soak_regressions_learn_acknowledgment_rotates() -> None:
    from engram.constants import LEARNED_ACKNOWLEDGMENTS

    engram = Engram(config=engram_config(learn_user_facts=True))
    engram.store("Go on.", pattern="*", tier=Tier.STATIC)

    responses = set()
    for i in range(12):
        result = engram.pattern_query(f"Gadget{i} is a useful tool")
        responses.add(result[2])

    assert responses <= set(LEARNED_ACKNOWLEDGMENTS)
    assert len(responses) >= 2


def test_soak_regressions_user_assertions_enter_shared_knowledge() -> None:
    engram = Engram()
    engram.store("Go on.", pattern="*", tier=Tier.STATIC)

    engram.pattern_query(
        "The support code is 9999.",
        user_id="user-a",
    )
    result = engram.pattern_query(
        "What is the support code?",
        user_id="user-b",
    )

    assert result[2] == "The support code is 9999."
    learned_id = engram.pattern_to_statement["SUPPORT CODE"]
    assert engram.get_statement(learned_id)["introduced_by_user_id"] == "user-a"


"""Research facts enter shared knowledge without entering a user context."""


def test_external_fact_ingestion_add_fact_is_unattributed_and_globally_retrievable() -> None:
    engram = Engram()

    stmt_id = engram.add_fact(
        "Tokyo is the capital of Japan",
        source_label="research-tool",
    )

    stmt = engram.get_statement(stmt_id)
    assert stmt["introduced_by_user_id"] == ""
    assert stmt["source_label"] == "research-tool"
    assert engram.sessions == {}
    assert engram.pattern_query("What is Tokyo?", user_id="carol")[2] == ("Tokyo is the capital of Japan.")
    assert metrics.get_dynamic_count(engram) == 1
    assert "WHAT IS TOKYO" in stmt["pattern_aliases"]


def test_external_fact_ingestion_fact_aliases_survive_persistence_and_retirement() -> None:
    engram = Engram()
    stmt_id = engram.add_fact("Tokyo is the capital of Japan")
    restored = persistence.load_engram_json(persistence.save_json(engram))

    assert restored.pattern_query("What is Tokyo?")[2] == "Tokyo is the capital of Japan."
    assert restored.pattern_to_statement["WHAT IS TOKYO"] == stmt_id

    restored.retire_statement(stmt_id)
    assert restored.pattern_query("What is Tokyo?") == ()
    assert "WHAT IS TOKYO" not in restored.pattern_to_statement


def test_external_fact_ingestion_add_fact_retains_unstructured_text() -> None:
    engram = Engram()

    stmt_id = engram.add_fact("Sushi: good, portable, and widely available.")

    assert engram.get_statement(stmt_id)["text"] == ("Sushi: good, portable, and widely available.")
    assert engram.get_statement(stmt_id)["pattern"] == ""


def test_external_fact_ingestion_add_fact_is_idempotent_for_existing_fact() -> None:
    engram = Engram()

    first = engram.add_fact("Tokyo is the capital of Japan")
    second = engram.add_fact("Tokyo is the capital of Japan")

    assert second == first
    assert sum(stmt["pattern"] == "TOKYO" for stmt in engram.statements) == 1


def test_external_fact_ingestion_add_fact_validates_input() -> None:
    engram = Engram()

    with pytest.raises(ValueError):
        engram.add_fact("")
    with pytest.raises(ValueError):
        engram.add_fact("A fact", source_label=cast(str, ()))


"""Restating or contradicting a known fact surfaces the stored belief."""


def _known_fact_responses_taught_engram() -> Engram:
    engram = Engram(config=engram_config(learn_user_facts=True))
    engram.store("Go on.", pattern="*", tier=Tier.STATIC)
    engram.pattern_query("The sky is blue.")
    return engram


def test_known_fact_responses_contradiction_surfaces_stored_fact() -> None:
    from engram.constants import CONFLICTING_FACT_RESPONSES

    engram = _known_fact_responses_taught_engram()
    result = engram.pattern_query("The sky is green.")

    expected = {r.replace("{existing}", "The sky is blue.") for r in CONFLICTING_FACT_RESPONSES}
    assert result[2] in expected
    # The stored fact is untouched
    assert engram.pattern_query("What is the sky?")[2] == "The sky is blue."


def test_known_fact_responses_restatement_confirms_stored_fact() -> None:
    from engram.constants import KNOWN_FACT_RESPONSES

    engram = _known_fact_responses_taught_engram()
    result = engram.pattern_query("The sky is blue.")

    expected = {r.replace("{existing}", "The sky is blue.") for r in KNOWN_FACT_RESPONSES}
    assert result[2] in expected


"""Learned facts are keyword-indexed under their full content."""


def test_fact_content_retrieval_yes_no_question_reaches_cache() -> None:
    from engram import pipeline

    engram = Engram(config=engram_config(learn_user_facts=True))
    engram.store("Go on.", pattern="*", tier=Tier.STATIC)
    engram.pattern_query("The sky is blue.")

    result = pipeline.respond(engram, "Is the sky blue?")

    assert result["source"] == "cache"
    assert result["response"] == "The sky is blue."


def test_fact_content_retrieval_who_is_pattern_generated() -> None:
    engram = Engram(config=engram_config(learn_user_facts=True))
    engram.store("Go on.", pattern="*", tier=Tier.STATIC)
    engram.pattern_query("Rex is a golden retriever.")

    assert engram.pattern_query("Who is rex?")[2] == "Rex is a golden retriever."
