"""Integration tests for process-memory LRU eviction."""

from engram.config import engram_config
from engram.constants import Tier
from engram.core import Engram
from engram.metrics import get_dynamic_count

"""Capacity holds under engine-only usage."""


def test_capacity_enforcement_capacity_respected_without_manual_stats() -> None:
    config = engram_config(capacity=2)
    engram = Engram(config=config)

    for i in range(5):
        engram.store(f"statement number {i}")

    assert get_dynamic_count(engram) == 2
    assert engram.eviction_count == 3


def test_lru_prefers_recently_hit_statement() -> None:
    config = engram_config(capacity=2)
    engram = Engram(config=config)

    id1 = engram.store("alpha statement one")
    id2 = engram.store("beta statement two")

    # Use the first statement through the public flow
    result = engram.query("alpha")
    engram.record_hit(result["keywords"], statement_id=id1)

    # LRU evicts the never-hit second statement even though it is newer.
    engram.store("gamma statement three")

    assert engram.get_statement(id1)
    assert not engram.get_statement(id2)


"""Evicting or retiring a statement must not leave its pattern matching."""


def test_pattern_cleanup_eviction_removes_pattern_from_matcher() -> None:
    config = engram_config(capacity=1)
    engram = Engram(config=config)

    engram.store("First response", pattern="FIRST PATTERN")
    assert engram.pattern_query("first pattern")[2] == "First response"

    # Storing a second dynamic statement evicts the first
    engram.store("Second response", pattern="SECOND PATTERN")

    assert engram.pattern_query("first pattern") == ()
    assert engram.pattern_query("second pattern")[2] == "Second response"
    assert len(engram.pattern_matcher) == 1
    assert "FIRST PATTERN" not in engram.pattern_to_statement


def test_pattern_cleanup_shared_pattern_survives_partial_eviction() -> None:
    """A pattern shared by a surviving statement stays in the matcher."""
    config = engram_config(capacity=1)
    engram = Engram(config=config)

    engram.store("Shared response", pattern="GREETING", tier=Tier.STATIC)
    engram.store("Dynamic duplicate", pattern="GREETING")

    # Filler evicts the dynamic duplicate; the static twin keeps the pattern
    engram.store("Filler statement")

    assert engram.pattern_query("greeting")[2] == "Shared response"


def test_pattern_cleanup_retire_statement_removes_pattern() -> None:
    engram = Engram()
    stmt_id = engram.store("Retired response", pattern="RETIRE ME")
    assert engram.pattern_query("retire me")[2] == "Retired response"

    assert engram.retire_statement(stmt_id)

    assert engram.pattern_query("retire me") == ()
    assert "RETIRE ME" not in engram.pattern_to_statement
