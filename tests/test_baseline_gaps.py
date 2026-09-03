"""Behavioral regressions for distinct accepted-response identities."""

from engram.config import engram_config
from engram.core import Engram
from engram.service import EngramCore


def test_when_and_where_requests_keep_distinct_cached_responses() -> None:
    engram = Engram(
        config=engram_config(
            learn_user_facts=False,
            use_lemmatization=False,
            use_spell_correction=False,
            use_stemming=False,
            use_synonyms=False,
        )
    )
    core = EngramCore(engram)

    when = core.learn_response("When was Ada Lovelace born?", "Ada Lovelace was born in 1815.", "learn-when")
    where = core.learn_response("Where was Ada Lovelace born?", "Ada Lovelace was born in London.", "learn-where")
    when_proposal = core.propose("When was Ada Lovelace born?", "propose-when")
    where_proposal = core.propose("Where was Ada Lovelace born?", "propose-where")

    assert when["statement_id"] != where["statement_id"]
    assert when_proposal["candidates"][0]["response"] == "Ada Lovelace was born in 1815."
    assert where_proposal["candidates"][0]["response"] == "Ada Lovelace was born in London."
