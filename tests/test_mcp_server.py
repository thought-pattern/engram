"""Protocol and process-lifecycle tests for the MCP adapter."""

from asyncio import run as asyncio_run
from json import loads as json_loads
from pathlib import Path
from sys import executable as sys_executable

from mcp.client import Client
from mcp.client.stdio import StdioServerParameters, stdio_client
from pytest import raises as pytest_raises

from engram.errors import ConflictError, LifecycleError
from engram.mcp_server import EngramMCPServer, MCPConversationService

from .test_relation import RelationGraph

REPOSITORY = Path(__file__).resolve().parent.parent


def tool_json(result) -> dict:
    assert result.is_error is False
    assert len(result.content) == 1
    assert result.content[0].type == "text"
    value = json_loads(result.content[0].text)
    assert isinstance(value, dict)
    return value


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


def test_default_service_loads_a_conversational_corpus() -> None:
    service = MCPConversationService()

    started = service.start(user_id="Mira", random_seed=17)
    turn = service.send("Hello")
    stopped = service.stop()

    assert started.get("statement_count", 0) > 0
    assert turn.get("response")
    assert turn.get("source") == "pattern"
    assert stopped.get("summary", {}).get("exchanges") == 1


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

    core, _ = service.require_active()
    assert proposal.get("candidates") == []
    assert core.engram.mutation_receipts.next_sequence == 1


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
                "engram_query",
                "engram_propose",
                "engram_resolve",
                "engram_learn_response",
                "engram_retire_response",
                "engram_retire_responses",
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
            queried = tool_json(
                await client.call_tool(
                    "engram_query",
                    {
                        "request": "What evidence is available?",
                        "request_id": "protocol-query",
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
            assert queried.get("outcome") in {"ANSWER", "EVIDENCE", "MISS"}
            assert resolved.get("resolved") is True
            assert status.get("core_status", {}).get("memory_only") is True
            assert stopped.get("stopped") is True

    asyncio_run(exercise())


def test_stdio_mcp_conversation_learns_recalls_finishes_and_stops() -> None:
    async def exercise() -> None:
        parameters = StdioServerParameters(
            command=sys_executable,
            args=["-m", "engram.mcp_server"],
            cwd=REPOSITORY,
        )
        async with Client(stdio_client(parameters), mode="legacy", read_timeout_seconds=30) as client:
            started = tool_json(
                await client.call_tool(
                    "engram_start",
                    {
                        "user_id": "Mira",
                        "initial_bot_text": ".",
                        "random_seed": 17,
                        "random_seed_present": True,
                    },
                )
            )
            introduced = tool_json(await client.call_tool("engram_send", {"text": "Cobalt Harbor is a floating library."}))
            recalled = tool_json(await client.call_tool("engram_send", {"text": "What do you remember about Cobalt Harbor?"}))
            report = tool_json(await client.call_tool("engram_finish", {}))
            continued = tool_json(await client.call_tool("engram_send", {"text": "Thank you."}))
            inspected = tool_json(await client.call_tool("engram_inspect", {}))
            stopped = tool_json(await client.call_tool("engram_stop", {}))
            after_stop = await client.call_tool("engram_inspect", {})

            assert started.get("statement_count", 0) > 0
            assert introduced.get("response")
            assert [item.get("text") for item in introduced.get("learned_statements", [])] == [
                "Cobalt Harbor is a floating library."
            ]
            assert recalled.get("response") == "Cobalt Harbor is a floating library."
            assert report.get("summary", {}).get("exchanges") == 2
            assert continued.get("turn") == 3
            assert continued.get("response")
            assert inspected.get("turn_count") == 3
            assert stopped.get("summary", {}).get("exchanges") == 3
            assert after_stop.is_error is True

    asyncio_run(exercise())


def test_mcp_query_uses_shared_graph_resolution(tmp_path, monkeypatch) -> None:
    async def exercise() -> None:
        graph = RelationGraph()
        monkeypatch.setattr("engram.core.connect_graph", lambda **internal_kwargs: graph)
        config = tmp_path / "graph.yml"
        config.write_text(
            "graph:\n  enabled: true\n  deployment_mode: tapestry_managed\n",
            encoding="utf-8",
        )
        async with Client(EngramMCPServer()) as client:
            tool_json(
                await client.call_tool(
                    "engram_start",
                    {"user_id": "MCP Graph", "config_path": str(config)},
                )
            )
            result = tool_json(
                await client.call_tool(
                    "engram_query",
                    {
                        "request": "Where was Ada Lovelace born?",
                        "request_id": "mcp-graph-query",
                        "configured_resolvers": ["exact"],
                    },
                )
            )
            await client.call_tool("engram_stop", {})

        assert result["outcome"] == "EVIDENCE"
        assert result["response_candidates"][0]["response"] == "Ada Lovelace — birth place: London."
        assert graph.one_hop_calls == [("entity:ada-lovelace", "predicate:birth-place", 10, False)]

    asyncio_run(exercise())
