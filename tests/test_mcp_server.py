"""Protocol and lifecycle tests for the FastMCP adapter."""

import asyncio
import json
from concurrent.futures import ThreadPoolExecutor

import pytest
from mcp.shared.memory import create_connected_server_and_client_session

import engram.mcp_server as mcp_server
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
    with pytest.raises(ValueError, match="engram_start"):
        service.propose("hello", "proposal-before-start")

    service.start(seed_path=str(_seed_file(tmp_path)))
    with pytest.raises(ValueError, match="already active"):
        service.start(seed_path=str(_seed_file(tmp_path)))


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
    assert service.runtime.engram.get_statement(candidate["statement_id"])["hit_count"] == 0
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
    assert service.runtime.engram.get_statement(candidate["statement_id"])["hit_count"] == 1
    snapshot = service.inspect()
    assert snapshot["session"]["previous_response"] == "Support is open from nine to five."
    assert snapshot["regulated_cache"]["accepted"] == 1
    assert snapshot["regulated_cache"]["rejections"]["rejected_quality"] == 1


def test_regulated_learning_is_scoped_replaceable_and_idempotent(tmp_path) -> None:
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

    replacement = service.learn_response(
        **shared,
        response="Updated support hours.",
        request_id="learn-support-replacement",
        namespace="support",
    )
    assert replacement["action"] == "replaced"
    assert replacement["statement_id"] == support["statement_id"]
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

    assert support_proposal["candidates"][0]["response"] == "Updated support hours."
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
        return service.resolve(proposal["proposal_id"], "accepted", statement_id=learned["statement_id"])

    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(executor.map(lambda _: accept(), range(8)))

    assert sum(result["idempotent"] is False for result in results) == 1
    assert service.runtime.engram.get_statement(learned["statement_id"])["hit_count"] == 1


def test_regulated_retirement_is_limited_and_idempotent(tmp_path) -> None:
    service = MCPConversationService()
    service.start(seed_path=str(_seed_file(tmp_path)))
    learned = service.learn_response("What is stale?", "An old answer.", "learn-stale")

    retired = service.retire_response(learned["statement_id"], "superseded_source_data", "retire-1")
    retry = service.retire_response(learned["statement_id"], "superseded_source_data", "retire-1")

    assert retired["retired"] is True
    assert retry["idempotent"] is True
    assert service.runtime.engram.get_statement(learned["statement_id"]) == {}
    static_pattern_id = next(statement["id"] for statement in service.runtime.engram.statements if statement["pattern"] == "HELLO")
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
    service.proposals[proposal["proposal_id"]]["created_at"] = 0
    with pytest.raises(ValueError, match="expired"):
        service.resolve(proposal["proposal_id"], "accepted", statement_id=learned["statement_id"])

    service.stop()
    service.start(seed_path="", store_path=str(store))
    persisted = service.propose("What persists?", "proposal-after-restart")
    assert persisted["candidates"][0]["response"] == "The learned response."
    with pytest.raises(ValueError, match="expired"):
        service.resolve(proposal["proposal_id"], "accepted", statement_id=learned["statement_id"])


def test_regulated_proposal_storage_is_bounded(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(mcp_server, "MAX_TRANSIENT_RECORDS", 2)
    service = MCPConversationService()
    service.start(seed_path=str(_seed_file(tmp_path)))

    oldest = service.propose("first uncached question", "bounded-1")
    service.propose("second uncached question", "bounded-2")
    service.propose("third uncached question", "bounded-3")

    assert len(service.proposals) == 2
    assert "bounded-1" not in service.proposal_requests
    with pytest.raises(ValueError, match="expired"):
        service.resolve(oldest["proposal_id"], "rejected_quality")


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
