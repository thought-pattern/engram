"""Executable deployment and rollback runbook checks."""

from shutil import copy2

from engram.core import Engram
from engram.service import EngramCore, open_engram_core


def test_persistence_rollback_restores_consistent_authority_and_retry_identity(tmp_path) -> None:
    primary = tmp_path / "primary.json"
    rollback_copy = tmp_path / "rollback.json"
    core = EngramCore(Engram(), store_path=primary)
    created = core.learn_response(
        "What is the approved endpoint?",
        "Use the approved endpoint.",
        "rollback-baseline",
        namespace="operations",
    )
    assert core.status()["healthy"] is True
    copy2(primary, rollback_copy)

    later = core.learn_response(
        "What changed after the backup?",
        "This response must not survive rollback.",
        "rollback-later",
        namespace="operations",
    )
    assert later["statement_id"] != created["statement_id"]
    assert core.close() is True

    restored = open_engram_core(store_path=rollback_copy)
    replay = restored.learn_response(
        "What is the approved endpoint?",
        "Use the approved endpoint.",
        "rollback-baseline",
        namespace="operations",
    )
    proposal = restored.propose(
        "What is the approved endpoint?",
        "rollback-proposal",
        namespace="operations",
    )

    assert replay["idempotent"] is True
    assert replay["statement_id"] == created["statement_id"]
    assert proposal["candidates"][0]["statement_id"] == created["statement_id"]
    assert restored.engram.get_statement(later["statement_id"]) == {}
    assert restored.engram.response_repository.check()["consistent"] is True
    assert restored.engram.check_indexes()["consistent"] is True
    assert restored.status()["healthy"] is True
