"""Tests for learn_from_response metadata pass-through and retire_statement removal."""

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
    stmt_id = engram.learn_from_response("who acquired github", "Microsoft acquired GitHub.")
    stmt = engram.get_statement(stmt_id)
    pattern = stmt["pattern"]
    assert engram.pattern_to_statement.get(pattern) == stmt_id

    engram.retire_statement(stmt_id)
    assert pattern not in engram.pattern_to_statement
