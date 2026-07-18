"""Protocol and lifecycle tests for the FastMCP adapter."""

import asyncio
import json

import pytest
from mcp.shared.memory import create_connected_server_and_client_session

from engram.mcp_server import MCPConversationService, create_mcp_server


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
    assert result.isError is False
    assert len(result.content) == 1
    return json.loads(result.content[0].text)


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


def test_service_adds_unattributed_shared_fact_without_context_change(tmp_path) -> None:
    service = MCPConversationService()
    service.start(user_id="Carol", seed_path=str(_seed_file(tmp_path)))
    session_before = service.inspect()["session"]

    fact = service.add_fact("Tokyo is the capital of Japan.", source_label="research-tool")

    assert fact["introduced_by_user_id"] is None
    assert fact["source_label"] == "research-tool"
    assert service.inspect()["session"] == session_before
    assert service.send("What is Tokyo?")["response"] == "Tokyo is the capital of Japan."


def test_service_requires_an_explicit_lifecycle(tmp_path) -> None:
    service = MCPConversationService()
    with pytest.raises(ValueError, match="engram_start"):
        service.send("hello")

    service.start(seed_path=str(_seed_file(tmp_path)))
    with pytest.raises(ValueError, match="already active"):
        service.start(seed_path=str(_seed_file(tmp_path)))


def test_fastmcp_tools_work_through_the_mcp_protocol(tmp_path) -> None:
    async def exercise_protocol() -> None:
        server = create_mcp_server()
        async with create_connected_server_and_client_session(server) as client:
            listed = await client.list_tools()
            assert [tool.name for tool in listed.tools] == [
                "engram_start",
                "engram_send",
                "engram_inspect",
                "engram_add_fact",
                "engram_finish",
                "engram_stop",
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
            inspected = _tool_json(await client.call_tool("engram_inspect", {}))
            stopped = _tool_json(await client.call_tool("engram_stop", {}))

            assert started["turn_count"] == 0
            assert sent["response"] == "Hello!"
            assert inspected["turn_count"] == 1
            assert stopped["summary"]["exchanges"] == 1

    asyncio.run(exercise_protocol())
