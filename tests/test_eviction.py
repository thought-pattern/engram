"""Integration tests for eviction driven through the public engine API.

The unit tests in test_core.py set statement statistics by hand to exercise the
policy selectors in isolation. These tests drive the same behavior end to end --
store, query, record_hit, pattern_query -- so a policy that only works when
statistics are injected manually cannot pass. This is the seam where the
statement-stat wiring, min_hit_rate protection, and matcher cleanup meet.
"""

from engram.config import engram_config
from engram.constants import EvictionPolicy, Tier
from engram.core import Engram
from engram.metrics import get_dynamic_count


class TestCapacityEnforcement:
    """Capacity holds under engine-only usage."""

    def test_capacity_respected_without_manual_stats(self) -> None:
        config = engram_config(capacity=2)
        engram = Engram(config=config)

        for i in range(5):
            engram.store(f"statement number {i}")

        assert get_dynamic_count(engram) == 2
        assert engram.eviction_count == 3

    def test_min_hit_rate_does_not_protect_unqueried(self) -> None:
        """A statement with no query history has no evidence and stays evictable.

        The default hit rate for an unqueried statement is 0.5; if that were
        compared against the threshold, any min_hit_rate below 0.5 would
        protect every untouched statement and disable eviction entirely.
        """
        config = engram_config(capacity=2, min_hit_rate=0.3)
        engram = Engram(config=config)

        for i in range(5):
            engram.store(f"statement number {i}")

        assert get_dynamic_count(engram) == 2
        assert engram.eviction_count == 3

    def test_all_protected_admits_over_capacity(self) -> None:
        """When every DYNAMIC statement is protected, the new statement is admitted.

        Protection wins over capacity: the store must not drop the incoming
        statement silently, and must not loop forever trying to evict.
        """
        config = engram_config(capacity=1, min_hit_rate=0.3)
        engram = Engram(config=config)

        id1 = engram.store("alpha statement")
        result = engram.query("alpha")
        engram.record_hit(result["keywords"], statement_id=id1)  # rate 1.0 -> protected

        id2 = engram.store("beta statement")

        assert engram.get_statement(id1)
        assert engram.get_statement(id2)
        assert get_dynamic_count(engram) == 2


class TestPolicyDifferentiation:
    """Policies must diverge from FIFO when usage differs, via public calls only."""

    def test_lru_prefers_recently_hit(self) -> None:
        config = engram_config(capacity=2, eviction_policy=EvictionPolicy.LRU)
        engram = Engram(config=config)

        id1 = engram.store("alpha statement one")
        id2 = engram.store("beta statement two")

        # Use the first statement through the public flow
        result = engram.query("alpha")
        engram.record_hit(result["keywords"], statement_id=id1)

        # LRU evicts the never-hit second statement, even though it is newer;
        # FIFO would have evicted the first.
        engram.store("gamma statement three")

        assert engram.get_statement(id1)
        assert not engram.get_statement(id2)

    def test_lfu_prefers_frequently_hit(self) -> None:
        config = engram_config(capacity=2, eviction_policy=EvictionPolicy.LFU)
        engram = Engram(config=config)

        id1 = engram.store("alpha statement one")
        id2 = engram.store("beta statement two")

        result = engram.query("alpha")
        engram.record_hit(result["keywords"], statement_id=id1)
        result = engram.query("alpha")
        engram.record_hit(result["keywords"], statement_id=id1)

        engram.store("gamma statement three")

        assert engram.get_statement(id1)
        assert not engram.get_statement(id2)

    def test_hit_rate_evicts_low_performer(self) -> None:
        config = engram_config(capacity=2, eviction_policy=EvictionPolicy.HIT_RATE)
        engram = Engram(config=config)

        id1 = engram.store("alpha statement one")
        id2 = engram.store("beta statement two")

        # First statement: queried and confirmed (rate 1.0)
        result = engram.query("alpha")
        engram.record_hit(result["keywords"], statement_id=id1)
        # Second statement: queried but never confirmed (rate 0.0)
        engram.query("beta")

        # HIT_RATE evicts the low performer; FIFO would have evicted the first.
        engram.store("gamma statement three")

        assert engram.get_statement(id1)
        assert not engram.get_statement(id2)


class TestPatternCleanup:
    """Evicting or retiring a statement must not leave its pattern matching."""

    def test_eviction_removes_pattern_from_matcher(self) -> None:
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

    def test_shared_pattern_survives_partial_eviction(self) -> None:
        """A pattern shared by a surviving statement stays in the matcher."""
        config = engram_config(capacity=1)
        engram = Engram(config=config)

        engram.store("Shared response", pattern="GREETING", tier=Tier.STATIC)
        engram.store("Dynamic duplicate", pattern="GREETING")

        # Filler evicts the dynamic duplicate; the static twin keeps the pattern
        engram.store("Filler statement")

        assert engram.pattern_query("greeting")[2] == "Shared response"

    def test_retire_statement_removes_pattern(self) -> None:
        engram = Engram()
        stmt_id = engram.store("Retired response", pattern="RETIRE ME")
        assert engram.pattern_query("retire me")[2] == "Retired response"

        assert engram.retire_statement(stmt_id)

        assert engram.pattern_query("retire me") == ()
        assert "RETIRE ME" not in engram.pattern_to_statement
