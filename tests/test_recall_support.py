"""Tests for learn_from_response caching semantics and retire_statement removal."""

from engram.core import Engram


def test_learn_from_response_carries_template():
    engram = Engram()
    support = {"support": [{"claim_id": "c1", "trust": 1.0}]}
    stmt_id = engram.learn_from_response("who acquired github", "Microsoft acquired GitHub.", template=support)
    stmt = engram.get_statement(stmt_id)
    assert stmt["template"] == support


def test_learn_from_response_default_template_empty():
    engram = Engram()
    stmt_id = engram.learn_from_response("q", "r")
    stmt = engram.get_statement(stmt_id)
    assert stmt["template"] == {}


def test_learn_from_response_indexed_under_query_keywords():
    # The response shares no keywords with the query; retrieval must go
    # through the query's terms, which the entry is indexed under.
    engram = Engram()
    engram.learn_from_response("what is the boiling point of water", "100 degrees Celsius at sea level.")

    result = engram.query("boiling point of water")

    assert result["matches"]
    assert result["matches"][0][0]["text"] == "100 degrees Celsius at sea level."


def test_learn_from_response_generates_no_pattern():
    engram = Engram()
    stmt_id = engram.learn_from_response("who acquired github", "Microsoft acquired GitHub.")

    stmt = engram.get_statement(stmt_id)
    assert stmt["pattern"] == ""
    assert len(engram.pattern_matcher) == 0


def test_learn_from_response_dedup_replaces_in_place():
    # Re-learning the same question (same keyword set) updates the cached
    # entry instead of accumulating duplicates, and resets its statistics
    # because the new content is unproven.
    engram = Engram()
    first_id = engram.learn_from_response("who acquired github", "First conclusion.")

    result = engram.query("who acquired github")
    engram.record_hit(result["keywords"], statement_id=first_id)
    assert engram.get_statement(first_id)["hit_count"] == 1

    second_id = engram.learn_from_response("who acquired github", "Second conclusion.")

    assert second_id == first_id
    stmt = engram.get_statement(first_id)
    assert stmt["text"] == "Second conclusion."
    assert stmt["hit_count"] == 0
    assert stmt["query_count"] == 0
    texts = [match[0]["text"] for match in engram.query("who acquired github")["matches"]]
    assert texts.count("Second conclusion.") == 1
    assert "First conclusion." not in texts


def test_retire_statement_removes_entry():
    engram = Engram()
    stmt_id = engram.learn_from_response("who acquired github", "Microsoft acquired GitHub.")
    assert engram.get_statement(stmt_id)
    assert engram.query("who acquired github")["matches"]

    removed = engram.retire_statement(stmt_id)
    assert removed is True
    assert engram.get_statement(stmt_id) == {}
    # The retired entry is no longer recalled.
    assert engram.query("who acquired github")["matches"] == []


def test_retire_statement_unknown_id():
    engram = Engram()
    assert engram.retire_statement("does-not-exist") is False


def test_retire_statement_leaves_other_entries():
    engram = Engram()
    a_id = engram.learn_from_response("microsoft acquired github", "First conclusion.")
    engram.learn_from_response("microsoft acquired github repository hosting", "Second conclusion.")

    engram.retire_statement(a_id)

    texts = [match[0]["text"] for match in engram.query("microsoft acquired github")["matches"]]
    assert "Second conclusion." in texts
    assert "First conclusion." not in texts


def test_retire_statement_clears_pattern_map():
    engram = Engram()
    stmt_id = engram.store("Microsoft acquired GitHub.", pattern="WHO ACQUIRED GITHUB")
    assert engram.pattern_to_statement.get("WHO ACQUIRED GITHUB") == stmt_id

    engram.retire_statement(stmt_id)
    assert "WHO ACQUIRED GITHUB" not in engram.pattern_to_statement
