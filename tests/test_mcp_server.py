"""Protocol and process-lifecycle tests for the MCP adapter."""

from asyncio import run as asyncio_run
from json import loads as json_loads

from mcp.client import Client
from pytest import raises as pytest_raises

from engram.errors import ConflictError, LifecycleError
from engram.mcp_server import EngramMCPServer, MCPConversationService
from scripts.run_section3_mcp_conformance import evaluate_turn, run_length_encode_passes


def complete_turn_event() -> dict:
    return {
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


def tool_json(result) -> dict:
    assert result.is_error is False
    assert len(result.content) == 1
    assert result.content[0].type == "text"
    value = json_loads(result.content[0].text)
    assert isinstance(value, dict)
    return value


def test_long_conversation_evaluator_checks_complete_turn_without_retaining_text() -> None:
    evaluation = evaluate_turn(complete_turn_event(), 1, "hello", "Protocol Agent", 2.5)

    assert evaluation.get("passed") is True
    assert evaluation.get("failed_checks") == []
    assert evaluation.get("response_bytes") == 6
    assert "Hello!" not in str(evaluation)


def test_turn_evaluation_run_length_encoding_preserves_order() -> None:
    encoded = run_length_encode_passes([{"passed": True}, {"passed": True}, {"passed": False}, {"passed": True}])

    assert encoded == [
        {"bit": "1", "turns": 2},
        {"bit": "0", "turns": 1},
        {"bit": "1", "turns": 1},
    ]


def test_service_requires_an_explicit_lifecycle() -> None:
    service = MCPConversationService()

    with pytest_raises(LifecycleError, match="engram_start"):
        service.inspect()

    service.start(user_id="Alice")
    with pytest_raises(ConflictError, match="already active"):
        service.start(user_id="Carol")

    service.stop()
    with pytest_raises(LifecycleError, match="engram_start"):
        service.inspect()


def test_empty_mcp_user_uses_unknown_user_zero_for_complete_lifecycle() -> None:
    service = MCPConversationService()

    started = service.start(user_id="")
    turn = service.send("Hello")
    inspected = service.inspect()
    fact = service.add_fact("Tokyo is the capital of Japan.", source_label="research")
    finished = service.finish()
    stopped = service.stop()

    assert started.get("user_id") == "0"
    assert turn.get("user_id") == "0"
    assert inspected.get("user_id") == "0"
    assert fact.get("source_label") == "research"
    assert finished.get("user_id") == "0"
    assert stopped.get("user_id") == "0"
    assert service.active_user_id == ""
    assert service.core == ()


def test_stop_discards_responses_conversations_and_receipts() -> None:
    service = MCPConversationService()
    service.start(user_id="Alice")
    learned = service.learn_response(
        "When is support open?",
        "Nine to five.",
        "learn-1",
        user_id="Alice",
        namespace="support",
    )
    assert learned.get("idempotent") is False

    service.stop()
    service.start(user_id="Alice")
    proposal = service.propose(
        "When is support open?",
        "proposal-after-restart",
        user_id="Alice",
        namespace="support",
    )

    assert proposal.get("candidates") == []
    assert service.core.engram.mutation_receipts.next_sequence == 1


def test_finish_returns_an_in_memory_report_without_writing_files(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    service = MCPConversationService()

    service.start(user_id="Alice")
    service.send("Hello")
    report = service.finish()
    service.stop()

    assert report.get("summary", {}).get("exchanges") == 1
    assert list(tmp_path.iterdir()) == []


def test_regulator_learn_propose_resolve_and_retire_share_one_process_core() -> None:
    service = MCPConversationService()
    service.start(user_id="Regulator")
    learned = service.learn_response(
        "When is support open?",
        "Nine to five.",
        "learn-1",
        namespace="support",
    )
    replay = service.learn_response(
        "When is support open?",
        "Nine to five.",
        "learn-1",
        namespace="support",
    )
    proposal = service.propose(
        "When is support open?",
        "proposal-1",
        namespace="support",
    )
    resolved = service.resolve(
        proposal.get("proposal_id", ""),
        "accepted",
        learned.get("statement_id", ""),
    )
    retired = service.retire_response(
        learned.get("statement_id", ""),
        "support changed",
        "retire-1",
    )

    assert replay.get("idempotent") is True
    assert resolved.get("resolved") is True
    assert retired.get("retired") is True


def test_add_fact_is_shared_but_does_not_change_user_context() -> None:
    service = MCPConversationService()
    service.start(user_id="Alice", initial_bot_text="Initial context.")
    before = service.inspect().get("session", {})

    fact = service.add_fact("Tokyo is the capital of Japan.", source_label="research")
    after = service.inspect().get("session", {})

    assert fact.get("source_label") == "research"
    assert after == before


def test_mcp_protocol_exposes_no_disk_memory_parameters() -> None:
    async def exercise() -> None:
        server = EngramMCPServer()
        async with Client(server) as client:
            assert client.server_info is not None
            assert client.server_info.name == "Engram"

            listed = await client.list_tools()
            names = [tool.name for tool in listed.tools]
            assert names == [
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
            contracts = {tool.name: tool.model_dump() for tool in listed.tools}
            start_schema = contracts.get("engram_start", {}).get("input_schema", {})
            finish_schema = contracts.get("engram_finish", {}).get("input_schema", {})
            assert sorted(start_schema.get("properties", {})) == [
                "config_path",
                "initial_bot_text",
                "random_seed",
                "random_seed_present",
                "user_id",
            ]
            assert finish_schema.get("properties", {}) == {}
            started = tool_json(
                await client.call_tool(
                    "engram_start",
                    {"user_id": "Protocol Agent", "initial_bot_text": "."},
                )
            )
            learned = tool_json(
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
            proposed = tool_json(
                await client.call_tool(
                    "engram_propose",
                    {
                        "request": "When are you open?",
                        "request_id": "protocol-proposal",
                        "namespace": "support",
                    },
                )
            )
            resolved = tool_json(
                await client.call_tool(
                    "engram_resolve",
                    {
                        "proposal_id": proposed.get("proposal_id", ""),
                        "outcome": "accepted",
                        "statement_id": learned.get("statement_id", ""),
                    },
                )
            )
            status = tool_json(await client.call_tool("engram_inspect", {}))
            stopped = tool_json(await client.call_tool("engram_stop", {}))

            assert started.get("turn_count") == 0
            assert resolved.get("resolved") is True
            assert status.get("core_status", {}).get("memory_only") is True
            assert stopped.get("stopped") is True

    asyncio_run(exercise())
