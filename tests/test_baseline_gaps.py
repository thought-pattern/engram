"""Section 0 characterizations retained across enhancement increments."""

import json
from pathlib import Path

import pytest

from engram import persistence
from engram.config import engram_config
from engram.constants import Tier
from engram.core import Engram
from engram.errors import InvalidRequestError
from engram.identity import ScopedRetrievalKey, ScopeKey
from engram.indexes import ExactLookupOutcome, IndexIssueReason
from engram.service import EngramCore

REPOSITORY = Path(__file__).resolve().parent.parent
REGULATED_FIXTURE = REPOSITORY / "documentation" / "baseline" / "fixtures" / "regulated-response-v1.json"


def _baseline_engram() -> Engram:
    return Engram(
        config=engram_config(
            learn_user_facts=False,
            use_lemmatization=False,
            use_spell_correction=False,
            use_stemming=False,
            use_synonyms=False,
        )
    )


@pytest.mark.xfail(
    strict=True,
    reason="EGR-308: legacy LearnResponse still replaces equal keyword sets before Section 3 artifact commit",
)
def test_when_and_where_requests_keep_distinct_cached_responses() -> None:
    engram = _baseline_engram()

    when_id = engram.learn_from_response("When was Ada Lovelace born?", "Ada Lovelace was born in 1815.")
    where_id = engram.learn_from_response("Where was Ada Lovelace born?", "Ada Lovelace was born in London.")

    assert when_id != where_id
    assert engram.query("When was Ada Lovelace born?")["matches"][0][0]["text"] == "Ada Lovelace was born in 1815."
    assert engram.query("Where was Ada Lovelace born?")["matches"][0][0]["text"] == "Ada Lovelace was born in London."


def test_baseline_has_no_non_executable_retrieval_alias_storage() -> None:
    engram = _baseline_engram()
    statement_id = engram.learn_from_response("What are the support hours?", "Support is open from nine to five.")
    stored = engram.get_statement(statement_id)

    assert "retrieval_aliases" not in stored
    assert "normalization_version" not in stored
    assert stored["pattern_aliases"] == []


def test_section2_exact_lookup_does_not_invent_a_legacy_key() -> None:
    engram = _baseline_engram()
    statement_id = engram.learn_from_response("What are the support hours?", "Support is open from nine to five.")
    guessed_key = ScopedRetrievalKey.build(ScopeKey(), "What are the support hours?")

    assert not hasattr(engram, "exact_retrieval_index")
    assert engram.exact_lookup(guessed_key).outcome == ExactLookupOutcome.MISS
    assert engram.index_snapshot().statement_to_retrieval[statement_id] == ()
    assert IndexIssueReason.MISSING_IDENTITY in {issue.reason for issue in engram.index_snapshot().build_report.issues}


def test_baseline_couples_eviction_to_tier_without_lifecycle_state() -> None:
    engram = _baseline_engram()
    statement_id = engram.learn_from_response("What are the support hours?", "Support is open from nine to five.")
    stored = engram.get_statement(statement_id)

    assert stored["tier"] == Tier.DYNAMIC
    assert "lifecycle" not in stored
    assert "valid_from" not in stored
    assert "valid_to" not in stored
    assert "superseded_by" not in stored

    static_id = engram.store("Static response.", keyword_source="static response", tier=Tier.STATIC)
    core = EngramCore(engram, checkpoint_on_mutation=False)
    with pytest.raises(InvalidRequestError, match="only dynamic"):
        core.retire_response(static_id, "baseline characterization", "retire-static")


def test_section2_claim_support_reverse_index_preserves_current_metadata() -> None:
    engram = _baseline_engram()
    statement_id = engram.store(
        "A response supported by a synthetic Claim.",
        keyword_source="synthetic support",
        template={"tapestry": {"support": [{"claim_id": "claim-synthetic"}]}},
    )

    state = engram.index_snapshot()
    assert state.claim_to_statements == {"claim-synthetic": (statement_id,)}
    assert state.statement_to_claims == {statement_id: ("claim-synthetic",)}


def test_sanitized_regulated_response_fixture_matches_persistence_v1() -> None:
    fixture = json.loads(REGULATED_FIXTURE.read_text(encoding="utf-8"))
    restored = persistence.load_engram_from_dict(fixture["persisted_store_excerpt"])
    stored = restored.get_statement("stmt_section0fixture")

    assert fixture["sanitization"]["contains_customer_content"] is False
    assert stored["text"] == "The synthetic service is available from nine to five."
    assert stored["template"]["tapestry"]["support"] == [
        {"claim_id": "claim-section0-001"},
        {"claim_id": "claim-section0-002"},
    ]
    assert fixture["runtime_only_regulated_state"]["persisted"] is False
    assert fixture["confirmed_absences"]["persisted_idempotency_records"] is False
