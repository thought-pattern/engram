"""Integration tests for process-memory LRU eviction."""

from random import Random

from engram import eviction
from engram.config import engram_config
from engram.constants import EARLIEST_UTC, Tier
from engram.core import Engram
from engram.metrics import get_dynamic_count
from engram.models import record_statement_hit

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
    assert "keywords" in result
    engram.record_hit(result.get("keywords", []), statement_id=id1)

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


def test_evicting_every_carrier_of_a_shared_pattern_leaves_no_dead_entry() -> None:
    engram = Engram()
    engram.store("Fallback.", pattern="*", tier=Tier.STATIC)
    first = engram.store("First.", pattern="SHARED PATTERN", tier=Tier.DYNAMIC)
    engram.store("Second.", pattern="SHARED PATTERN", tier=Tier.DYNAMIC)

    assert engram.retire_statement(first)
    assert engram.pattern_query("shared pattern")[2] == "Second."
    assert eviction.clear_dynamic(engram) == 1

    # A leftover entry would still win the walk and answer with nothing.
    assert len(engram.pattern_matcher) == 1
    assert engram.pattern_query("shared pattern")[2] == "Fallback."


def test_lru_heap_evicts_what_a_full_scan_would() -> None:
    def scan_choice(engram: Engram) -> str:
        def key(item: tuple[int, dict]) -> tuple:
            idx, statement = item
            last_hit = statement.get("last_hit", "")
            result = (last_hit or statement.get("created_at", EARLIEST_UTC), 1 if last_hit else 0, idx)
            return result

        result = min(eviction.get_eviction_candidates(engram), key=key)[1].get("id", "")
        return result

    random = Random(20260928)
    engram = Engram(config=engram_config(capacity=1_000_000))
    engram.store("Static.", pattern="STATIC *", tier=Tier.STATIC)
    for index in range(40):
        engram.store(f"Fact {index}.", pattern=f"FACT {index}", tier=Tier.DYNAMIC)
    for step in range(200):
        dynamic = sorted(engram.dynamic_statement_ids)
        action = random.random()
        if action < 0.35 and dynamic:
            hit_statement = engram.statement_by_id.get(random.choice(dynamic), {})
            assert hit_statement
            record_statement_hit(hit_statement)
        elif action < 0.55:
            engram.store(f"Later fact {step}.", pattern=f"LATER {step}", tier=Tier.DYNAMIC)
        elif action < 0.65 and dynamic:
            assert engram.retire_statement(random.choice(dynamic))
        elif dynamic:
            expected = scan_choice(engram)
            assert eviction.evict_dynamic(engram)
            assert expected not in engram.statement_by_id
            assert set(engram.dynamic_statement_ids) == set(dynamic) - {expected}
        positions = engram.statement_index
        assert all(engram.statements[position].get("id", "") == statement_id for statement_id, position in positions.items())
        assert all(engram.statement_position(statement_id) == position for statement_id, position in positions.items())
