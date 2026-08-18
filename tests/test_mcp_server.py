"""Protocol and lifecycle tests for the MCPServer adapter."""

import asyncio
import json
import time
from concurrent.futures import ThreadPoolExecutor
from typing import cast

import pytest
from mcp.client import Client

from engram import service as engram_service
from engram.config import engram_config
from engram.constants import VERSION
from engram.conversation import ConversationRuntime
from engram.core import Engram
from engram.errors import ConflictError, InvalidRequestError, LifecycleError
from engram.mcp_server import MCPConversationService, create_mcp_server
from engram.service import EngramCore
from scripts.run_section3_mcp_conformance import _evaluate_turn, _run_length_encode_passes


def _runtime(service: MCPConversationService) -> ConversationRuntime:
    result = cast(ConversationRuntime, service.runtime)
    return result


def _complete_turn_event() -> dict:
    result = {
        "turn": 1,
        "input": "hello",
        "response": "Hello!",
        "user_id": "Protocol Agent",
        "source": "pattern",
        "score": 1.0,
        "pattern": "HELLO",
        "captured": [],
        "dialogue_act": "greeting",
        "active_topic": "",
        "entities": [],
        "fact_admissions": [],
        "elapsed_seconds": 0.001,
        "context_changes": {},
        "learned_statements": [],
    }
    return result


def test_mcp_long_conversation_evaluator_checks_each_complete_turn_without_retaining_text() -> None:
    evaluation = _evaluate_turn(_complete_turn_event(), 1, "hello", "Protocol Agent", 2.5)

    assert evaluation["passed"] is True
    assert evaluation["failed_checks"] == []
    assert evaluation["response_bytes"] == 6
    assert "Hello!" not in str(evaluation)


def test_mcp_long_conversation_evaluator_reports_all_failed_contract_checks() -> None:
    malformed = {**_complete_turn_event(), "turn": 2, "source": "", "unexpected": True}

    evaluation = _evaluate_turn(malformed, 1, "hello", "Protocol Agent", 2.5)

    assert evaluation["passed"] is False
    assert evaluation["failed_checks"] == ["exact_fields", "turn_sequence", "source_nonempty"]


def test_mcp_turn_evaluation_pass_runs_preserve_every_ordered_turn() -> None:
    evaluations = [{"passed": True}, {"passed": True}, {"passed": False}, {"passed": True}]

    encoded = _run_length_encode_passes(evaluations)

    assert encoded == [
        {"bit": "1", "turns": 2},
        {"bit": "0", "turns": 1},
        {"bit": "1", "turns": 1},
    ]


def _seed_file(tmp_path):
    path = tmp_path / "seed.json"
    path.write_text(
        json.dumps(
            {
                "pairs": [
                    {"pattern": "HELLO", "response": "Hello!"},
                    {"pattern": "*", "response": "Go on."},
                ]
            }
        ),
        encoding="utf-8",
    )
    return path


def _tool_json(result) -> dict:
    assert result.is_error is False
    assert len(result.content) == 1
    result = json.loads(result.content[0].text)
    return result


def test_service_persists_one_runtime_across_calls(tmp_path) -> None:
    service = MCPConversationService()
    seed = _seed_file(tmp_path)
    store = tmp_path / "state" / "engram.json"
    transcript = tmp_path / "transcript.json"

    started = service.start(
        user_id="Agent",
        initial_bot_text=".",
        seed_path=str(seed),
        store_path=str(store),
        transcript_path=str(transcript),
    )
    first = service.send("Sushi is good.")
    second = service.send("What's good?")
    snapshot = service.inspect()

    assert started["user_id"] == "Agent"
    assert first["turn"] == 1
    assert second["turn"] == 2
    assert second["response"] == "Sushi is good."
    assert snapshot["session"]["previous_response"] == "Sushi is good."
    assert snapshot["learned_dynamic"][0]["introduced_by_user_id"] == "Agent"
    assert transcript.exists()

    stopped = service.stop()
    assert stopped["summary"]["exchanges"] == 2
    assert store.exists()
    with pytest.raises(ValueError, match="no active conversation"):
        service.inspect()

    restarted = MCPConversationService()
    restarted.start(user_id="Carol", seed_path="", store_path=str(store))
    assert restarted.send("What's good?")["response"] == "Sushi is good."


def test_service_restart_without_config_path_restores_stored_config(tmp_path) -> None:
    store = tmp_path / "engram.json"
    stored = EngramCore(Engram(config=engram_config(capacity=37, use_synonyms=False)), store_path=store)
    assert stored.flush() is True

    service = MCPConversationService()
    service.start(seed_path="", store_path=str(store))

    assert service.core
    assert service.core.engram.config["capacity"] == 37
    assert service.core.engram.config["use_synonyms"] is False


def test_service_adds_unattributed_shared_fact_without_context_change(tmp_path) -> None:
    service = MCPConversationService()
    service.start(user_id="Carol", seed_path=str(_seed_file(tmp_path)))
    session_before = service.inspect()["session"]

    fact = service.add_fact("Tokyo is the capital of Japan.", source_label="research-tool")

    assert fact["introduced_by_user_id"] == ""
    assert fact["source_label"] == "research-tool"
    assert service.inspect()["session"] == session_before
    assert service.send("What is Tokyo?")["response"] == "Tokyo is the capital of Japan."


def test_service_requires_an_explicit_lifecycle(tmp_path) -> None:
    service = MCPConversationService()
    with pytest.raises(LifecycleError, match="engram_start"):
        service.send("hello")
    with pytest.raises(LifecycleError, match="engram_start"):
        service.propose("hello", "proposal-before-start")

    service.start(seed_path=str(_seed_file(tmp_path)))
    with pytest.raises(ConflictError, match="already active"):
        service.start(seed_path=str(_seed_file(tmp_path)))


def test_mcp_start_propagates_transport_neutral_component_preflight(tmp_path, monkeypatch) -> None:
    config = tmp_path / "graph.yml"
    config.write_text("graph:\n  enabled: true\n", encoding="utf-8")
    unavailable_client = type("UnavailableGraph", (), {"available": False})()
    monkeypatch.setattr("engram.core.create_graph_client", lambda **kwargs: unavailable_client)

    with pytest.raises(InvalidRequestError, match="MemGraph service is unavailable"):
        MCPConversationService().start(seed_path="", config_path=str(config))


def test_regulated_proposal_records_only_accepted_hits(tmp_path) -> None:
    service = MCPConversationService()
    service.start(user_id="Robin", seed_path=str(_seed_file(tmp_path)))
    learned = service.learn_response(
        request="When are you open?",
        response="Support is open from nine to five.",
        request_id="learn-1",
        user_id="Robin",
        namespace="support",
        context_fingerprint="tier:pro",
        metadata={"actor_version": "actor-7"},
    )

    rejected_proposal = service.propose(
        request="When are you open?",
        request_id="proposal-1",
        user_id="Robin",
        namespace="support",
        context_fingerprint="tier:pro",
        required_metadata={"actor_version": "actor-7"},
    )
    candidate = rejected_proposal["candidates"][0]
    assert candidate["statement_id"] == learned["statement_id"]
    assert candidate["query_count"] == 1
    assert candidate["hit_count"] == 0

    rejected = service.resolve(
        rejected_proposal["proposal_id"],
        "rejected_quality",
        statement_id=candidate["statement_id"],
        reason="unsupported",
    )
    retry = service.resolve(
        rejected_proposal["proposal_id"],
        "rejected_quality",
        statement_id=candidate["statement_id"],
        reason="unsupported",
    )
    assert rejected["idempotent"] is False
    assert retry["idempotent"] is True
    assert _runtime(service).engram.get_statement(candidate["statement_id"])["hit_count"] == 0
    with pytest.raises(ValueError, match="different verdict"):
        service.resolve(rejected_proposal["proposal_id"], "accepted", statement_id=candidate["statement_id"])

    accepted_proposal = service.propose(
        request="When are you open?",
        request_id="proposal-2",
        user_id="Robin",
        namespace="support",
        context_fingerprint="tier:pro",
    )
    accepted = service.resolve(
        accepted_proposal["proposal_id"],
        "accepted",
        statement_id=candidate["statement_id"],
        reason="applicable_and_supported",
    )

    assert accepted["resolved"] is True
    assert _runtime(service).engram.get_statement(candidate["statement_id"])["hit_count"] == 1
    snapshot = service.inspect()
    assert snapshot["session"]["previous_response"] == "Support is open from nine to five."
    assert snapshot["regulated_cache"]["accepted"] == 1
    assert snapshot["regulated_cache"]["rejections"]["rejected_quality"] == 1


def test_regulated_learning_is_scoped_nonreplacing_and_idempotent(tmp_path) -> None:
    service = MCPConversationService()
    service.start(seed_path=str(_seed_file(tmp_path)))
    shared = {
        "request": "When are you open?",
        "user_id": "0",
        "context_fingerprint": "tier:pro",
        "metadata": {"actor_version": "actor-7"},
    }

    support = service.learn_response(
        **shared,
        response="Support hours.",
        request_id="learn-support",
        namespace="support",
    )
    billing = service.learn_response(
        **shared,
        response="Billing hours.",
        request_id="learn-billing",
        namespace="billing",
    )
    replay = service.learn_response(
        **shared,
        response="Support hours.",
        request_id="learn-support",
        namespace="support",
    )

    assert support["statement_id"] != billing["statement_id"]
    assert replay["statement_id"] == support["statement_id"]
    assert replay["idempotent"] is True
    with pytest.raises(ValueError, match="different learned response"):
        service.learn_response(
            **shared,
            response="Conflicting retry.",
            request_id="learn-support",
            namespace="support",
        )

    with pytest.raises(ValueError, match=support["statement_id"]):
        service.learn_response(
            **shared,
            response="Updated support hours.",
            request_id="learn-support-replacement",
            namespace="support",
        )
    support_proposal = service.propose(
        "When are you open?",
        "proposal-support",
        namespace="support",
        context_fingerprint="tier:pro",
    )
    billing_proposal = service.propose(
        "When are you open?",
        "proposal-billing",
        namespace="billing",
        context_fingerprint="tier:pro",
    )
    version_miss = service.propose(
        "When are you open?",
        "proposal-version-miss",
        namespace="support",
        context_fingerprint="tier:pro",
        required_metadata={"actor_version": "actor-8"},
    )

    assert support_proposal["candidates"][0]["response"] == "Support hours."
    assert billing_proposal["candidates"][0]["response"] == "Billing hours."
    assert version_miss["candidates"] == []
    with pytest.raises(ValueError, match="IDK"):
        service.learn_response("question", "  IDK  ", "learn-idk")


def test_regulated_resolution_is_concurrency_safe(tmp_path) -> None:
    service = MCPConversationService()
    service.start(seed_path=str(_seed_file(tmp_path)))
    learned = service.learn_response("What is cached?", "This is cached.", "learn-concurrent")
    proposal = service.propose("What is cached?", "proposal-concurrent")

    def accept() -> dict:
        result = service.resolve(proposal["proposal_id"], "accepted", statement_id=learned["statement_id"])
        return result

    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(executor.map(lambda _: accept(), range(8)))

    assert sum(result["idempotent"] is False for result in results) == 1
    assert _runtime(service).engram.get_statement(learned["statement_id"])["hit_count"] == 1


def test_regulated_retirement_is_limited_and_idempotent(tmp_path) -> None:
    service = MCPConversationService()
    service.start(seed_path=str(_seed_file(tmp_path)))
    learned = service.learn_response("What is stale?", "An old answer.", "learn-stale")

    retired = service.retire_response(learned["statement_id"], "superseded_source_data", "retire-1")
    retry = service.retire_response(learned["statement_id"], "superseded_source_data", "retire-1")

    assert retired["retired"] is True
    assert retry["idempotent"] is True
    retired_artifact = _runtime(service).engram.response_repository.get_artifact(learned["statement_id"])
    assert retired_artifact["lifecycle"].value == "RETIRED"
    assert service.propose("What is stale?", "proposal-retired")["candidates"] == []
    static_pattern_id = next(
        statement["id"] for statement in _runtime(service).engram.statements if statement["pattern"] == "HELLO"
    )
    with pytest.raises(ValueError, match="dynamic, patternless"):
        service.retire_response(static_pattern_id, "not allowed", "retire-static")
    with pytest.raises(ValueError, match="different retirement"):
        service.retire_response(learned["statement_id"], "different reason", "retire-1")


def test_regulated_state_expires_and_is_not_persisted(tmp_path) -> None:
    service = MCPConversationService()
    store = tmp_path / "engram.json"
    service.start(seed_path=str(_seed_file(tmp_path)), store_path=str(store))
    learned = service.learn_response("What persists?", "The learned response.", "learn-persist")
    proposal = service.propose("What persists?", "proposal-expiring")
    service.proposals[proposal["proposal_id"]]["created_at"] = time.monotonic() - engram_service.PROPOSAL_TTL_SECONDS - 1
    with pytest.raises(ValueError, match="expired"):
        service.resolve(proposal["proposal_id"], "accepted", statement_id=learned["statement_id"])

    service.stop()
    service.start(seed_path="", store_path=str(store))
    persisted = service.propose("What persists?", "proposal-after-restart")
    assert persisted["candidates"][0]["response"] == "The learned response."
    with pytest.raises(ValueError, match="expired"):
        service.resolve(proposal["proposal_id"], "accepted", statement_id=learned["statement_id"])


def test_regulated_proposal_storage_is_bounded(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(engram_service, "MAX_TRANSIENT_RECORDS", 2)
    service = MCPConversationService()
    service.start(seed_path=str(_seed_file(tmp_path)))

    oldest = service.propose("first uncached question", "bounded-1")
    service.propose("second uncached question", "bounded-2")
    service.propose("third uncached question", "bounded-3")

    assert len(service.proposals) == 2
    assert "bounded-1" not in service.proposal_requests
    with pytest.raises(ValueError, match="expired"):
        service.resolve(oldest["proposal_id"], "rejected_quality")


def test_mcpserver_tools_work_through_the_mcp_protocol(tmp_path) -> None:
    async def exercise_protocol() -> None:
        server = create_mcp_server()
        async with Client(server) as client:
            assert client.server_info is not None
            assert client.server_info.name == "Engram"
            assert client.server_info.version == VERSION
            assert client.instructions

            listed = await client.list_tools()
            assert [tool.name for tool in listed.tools] == [
                "engram_start",
                "engram_send",
                "engram_inspect",
                "engram_add_fact",
                "engram_finish",
                "engram_stop",
                "engram_propose",
                "engram_resolve",
                "engram_learn_response",
                "engram_retire_response",
            ]

            started = _tool_json(
                await client.call_tool(
                    "engram_start",
                    {
                        "user_id": "Protocol Agent",
                        "initial_bot_text": ".",
                        "seed_path": str(_seed_file(tmp_path)),
                    },
                )
            )
            sent = _tool_json(await client.call_tool("engram_send", {"text": "hello"}))
            learned = _tool_json(
                await client.call_tool(
                    "engram_learn_response",
                    {
                        "request": "When are you open?",
                        "response": "Nine to five.",
                        "request_id": "protocol-learn",
                        "namespace": "support",
                    },
                )
            )
            proposed = _tool_json(
                await client.call_tool(
                    "engram_propose",
                    {
                        "request": "When are you open?",
                        "request_id": "protocol-proposal",
                        "namespace": "support",
                    },
                )
            )
            resolved = _tool_json(
                await client.call_tool(
                    "engram_resolve",
                    {
                        "proposal_id": proposed["proposal_id"],
                        "outcome": "accepted",
                        "statement_id": learned["statement_id"],
                    },
                )
            )
            inspected = _tool_json(await client.call_tool("engram_inspect", {}))
            stopped = _tool_json(await client.call_tool("engram_stop", {}))

            assert started["turn_count"] == 0
            assert sent["response"] == "Hello!"
            assert resolved["resolved"] is True
            assert inspected["turn_count"] == 1
            assert inspected["regulated_cache"]["accepted"] == 1
            assert stopped["summary"]["exchanges"] == 1

    asyncio.run(exercise_protocol())
