"""Process-memory CLI tests."""

from json import loads as json_loads

from engram.service import EngramCore
from scripts.cli import InteractiveChat, main

from .test_relation import RelationGraph


def test_query_runs_against_a_fresh_empty_process_cache(tmp_path, capsys) -> None:
    config = tmp_path / "config.yml"
    config.write_text("capacity: 7\n", encoding="utf-8")

    assert main(("--config", str(config), "query", "unknown question")) == 0

    payload = json_loads(capsys.readouterr().out)
    assert payload.get("outcome") == "MISS"
    assert payload.get("response_candidates") == []
    assert payload.get("evidence") == []


def test_cli_no_longer_accepts_disk_state_or_transcript_options() -> None:
    for arguments in (
        ("--store", "state.json", "query", "question"),
        ("--transcript", "conversation.json", "interactive"),
    ):
        try:
            main(arguments)
        except SystemExit as error:
            assert error.code == 2
        else:
            raise AssertionError(f"removed disk option was unexpectedly accepted: {arguments[0]}")


def test_cli_query_uses_shared_graph_resolution(tmp_path, capsys, monkeypatch) -> None:
    graph = RelationGraph()
    monkeypatch.setattr("engram.core.connect_graph", lambda **internal_kwargs: graph)
    config = tmp_path / "graph.yml"
    config.write_text(
        "graph:\n  enabled: true\n  deployment_mode: tapestry_managed\n",
        encoding="utf-8",
    )

    exit_code = main(
        (
            "--config",
            str(config),
            "query",
            "Where was Ada Lovelace born?",
            "--request-id",
            "cli-graph-query",
        )
    )

    payload = json_loads(capsys.readouterr().out)
    assert exit_code == 0
    assert payload["outcome"] == "EVIDENCE"
    assert payload["response_candidates"][0]["response"] == "Ada Lovelace — birth place: London."
    assert graph.one_hop_calls == [("entity:ada-lovelace", "predicate:birth-place", 10, False)]


def test_interactive_chat_uses_only_the_owned_core() -> None:
    core = EngramCore()
    learned = core.learn_response(
        "When is support open?",
        "Support is open from nine to five.",
        "learn-cli",
        user_id="regulator",
    )
    chat = InteractiveChat(core, session_id="cli-test")

    response = chat.process_input("When is support open?")

    proposal = core.propose("When is support open?", "cli-proposal")
    assert response != "Support is open from nine to five."
    assert proposal["candidates"][0]["statement_id"] == learned["statement_id"]
    assert chat.handle_command("/quit") is True


def test_new_cli_core_does_not_inherit_prior_process_memory() -> None:
    first = EngramCore()
    first.learn_response("Question?", "Answer.", "learn-1", user_id="regulator")
    restarted = EngramCore()

    assert first.engram.response_repository.snapshot()["artifacts"]
    assert first.engram.statements == []
    assert restarted.engram.statements == []
    assert restarted.engram.response_repository.snapshot()["artifacts"] == {}
