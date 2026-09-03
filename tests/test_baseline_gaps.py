"""Section 0 characterizations retained across enhancement increments."""

from engram.config import engram_config
from engram.core import Engram
from engram.identity import build_scoped_retrieval_key, scope_key
from engram.indexes import ExactLookupOutcome, IndexIssueReason
from engram.service import EngramCore

from .support_fixtures import PROPOSITION_REFERENCE_A, REFERENCE_IDS


def _baseline_engram() -> Engram:
    result = Engram(
        config=engram_config(
            learn_user_facts=False,
            use_lemmatization=False,
            use_spell_correction=False,
            use_stemming=False,
            use_synonyms=False,
        )
    )
    return result


def test_when_and_where_requests_keep_distinct_cached_responses() -> None:
    engram = _baseline_engram()
    core = EngramCore(engram, checkpoint_on_mutation=False)

    when = core.learn_response("When was Ada Lovelace born?", "Ada Lovelace was born in 1815.", "learn-when")
    where = core.learn_response("Where was Ada Lovelace born?", "Ada Lovelace was born in London.", "learn-where")
    when_proposal = core.propose("When was Ada Lovelace born?", "propose-when")
    where_proposal = core.propose("Where was Ada Lovelace born?", "propose-where")

    assert when["statement_id"] != where["statement_id"]
    assert when_proposal["candidates"][0]["response"] == "Ada Lovelace was born in 1815."
    assert where_proposal["candidates"][0]["response"] == "Ada Lovelace was born in London."


def test_section2_exact_lookup_does_not_invent_a_legacy_key() -> None:
    engram = _baseline_engram()
    statement_id = engram.learn_from_response("What are the support hours?", "Support is open from nine to five.")
    guessed_key = build_scoped_retrieval_key(scope_key(), "What are the support hours?")

    assert engram.exact_lookup(guessed_key)["outcome"] == ExactLookupOutcome.MISS
    assert engram.index_snapshot()["statement_to_retrieval"][statement_id] == ()
    assert IndexIssueReason.MISSING_IDENTITY in {issue["reason"] for issue in engram.index_snapshot()["build_report"]["issues"]}


def test_section2_proposition_support_reverse_index_preserves_current_metadata() -> None:
    engram = _baseline_engram()
    statement_id = engram.store(
        "A response supported by a synthetic Proposition.",
        keyword_source="synthetic support",
        template={"tapestry": {"support": [PROPOSITION_REFERENCE_A]}},
    )

    state = engram.index_snapshot()
    assert state["record_to_statements"] == {REFERENCE_IDS.get("proposition_a", ""): (statement_id,)}
    assert state["statement_to_references"] == {statement_id: (PROPOSITION_REFERENCE_A,)}
