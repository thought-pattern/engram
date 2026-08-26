"""Protocol and lifecycle tests for the MCPServer adapter."""

from asyncio import run as asyncio_run
from concurrent.futures import ThreadPoolExecutor
from json import dumps as json_dumps, loads as json_loads
from time import monotonic as time_monotonic

from mcp.client import Client
from pytest import raises as pytest_raises

from engram import service as engram_service
from engram.config import engram_config
from engram.constants import VERSION
from engram.core import Engram
from engram.errors import ConflictError, LifecycleError
from engram.mcp_server import MCPConversationService, create_mcp_server
from engram.service import EngramCore


def _seed_file(tmp_path):
    path = tmp_path / "seed.json"
    path.write_text(
        json_dumps(
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
    _return_value = json_loads(result.content[0].text)
    return _return_value


def test_service_persists_one_runtime_across_calls(tmp_path) -> bool:
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

    assert started.get("user_id", "") == "Agent"
    assert first.get("turn", 0) == 1
    assert second.get("turn", 0) == 2
    assert second.get("response", "") == "Sushi is good."
    assert snapshot.get("session", {}).get("previous_response", "") == "Sushi is good."
    assert snapshot.get("learned_dynamic", [])[0].get("introduced_by_user_id", "") == "Agent"
    assert transcript.exists()

    stopped = service.stop()
    assert stopped.get("summary", {}).get("exchanges", 0) == 2
    assert store.exists()
    with pytest_raises(ValueError, match="no active conversation"):
        service.inspect()

    restarted = MCPConversationService()
    restarted.start(user_id="Carol", seed_path="", store_path=str(store))
    assert restarted.send("What's good?").get("response", "") == "Sushi is good."
    return False


def test_service_restart_without_config_path_restores_stored_config(tmp_path) -> bool:
    store = tmp_path / "engram.json"
    stored = EngramCore(Engram(config=engram_config(capacity=37, use_synonyms=False)), store_path=store)
    assert stored.flush() is True

    service = MCPConversationService()
    service.start(seed_path="", store_path=str(store))

    assert service.core is not None
    assert service.core.engram.config.get("capacity", 0) == 37
    assert service.core.engram.config.get("use_synonyms", False) is False
    return False


def test_service_adds_unattributed_shared_fact_without_context_change(tmp_path) -> bool:
    service = MCPConversationService()
    service.start(user_id="Carol", seed_path=str(_seed_file(tmp_path)))
    session_before = service.inspect().get("session", False)

    fact = service.add_fact("Tokyo is the capital of Japan.", source_label="research-tool")

    assert fact.get("introduced_by_user_id", "") == ""
    assert fact.get("source_label", "") == "research-tool"
    assert service.inspect().get("session", False) == session_before
    assert service.send("What is Tokyo?").get("response", "") == "Tokyo is the capital of Japan."
    return False


def test_service_requires_an_explicit_lifecycle(tmp_path) -> bool:
    service = MCPConversationService()
    with pytest_raises(LifecycleError, match="engram_start"):
        service.send("hello")
    with pytest_raises(LifecycleError, match="engram_start"):
        service.propose("hello", "proposal-before-start")

    service.start(seed_path=str(_seed_file(tmp_path)))
    with pytest_raises(ConflictError, match="already active"):
        service.start(seed_path=str(_seed_file(tmp_path)))
    return False


def test_regulated_proposal_records_only_accepted_hits(tmp_path) -> bool:
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
    candidate = rejected_proposal.get("candidates", [])[0]
    assert candidate.get("statement_id", "") == learned.get("statement_id", "")
    assert candidate.get("query_count", 0) == 1
    assert candidate.get("hit_count", 0) == 0

    rejected = service.resolve(
        rejected_proposal.get("proposal_id", ""),
        "rejected_quality",
        statement_id=candidate.get("statement_id", ""),
        reason="unsupported",
    )
    retry = service.resolve(
        rejected_proposal.get("proposal_id", ""),
        "rejected_quality",
        statement_id=candidate.get("statement_id", ""),
        reason="unsupported",
    )
    assert rejected.get("idempotent", False) is False
    assert retry.get("idempotent", False) is True
    assert service.runtime.engram.get_statement(candidate.get("statement_id", "")).get("hit_count", 0) == 0
    with pytest_raises(ValueError, match="different verdict"):
        service.resolve(rejected_proposal.get("proposal_id", ""), "accepted", statement_id=candidate.get("statement_id", ""))

    accepted_proposal = service.propose(
        request="When are you open?",
        request_id="proposal-2",
        user_id="Robin",
        namespace="support",
        context_fingerprint="tier:pro",
    )
    accepted = service.resolve(
        accepted_proposal.get("proposal_id", ""),
        "accepted",
        statement_id=candidate.get("statement_id", ""),
        reason="applicable_and_supported",
    )

    assert accepted.get("resolved", False) is True
    assert service.runtime.engram.get_statement(candidate.get("statement_id", "")).get("hit_count", 0) == 1
    snapshot = service.inspect()
    assert snapshot.get("session", {}).get("previous_response", "") == "Support is open from nine to five."
    assert snapshot.get("regulated_cache", {}).get("accepted", 0) == 1
    assert snapshot.get("regulated_cache", {}).get("rejections", {}).get("rejected_quality", 0) == 1
    return False


def test_regulated_learning_is_scoped_replaceable_and_idempotent(tmp_path) -> bool:
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

    assert support.get("statement_id", "") != billing.get("statement_id", "")
    assert replay.get("statement_id", "") == support.get("statement_id", "")
    assert replay.get("idempotent", False) is True
    with pytest_raises(ValueError, match="different learned response"):
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
    assert replacement.get("action", "") == "replaced"
    assert replacement.get("statement_id", "") == support.get("statement_id", "")
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

    assert support_proposal.get("candidates", [])[0].get("response", "") == "Updated support hours."
    assert billing_proposal.get("candidates", [])[0].get("response", "") == "Billing hours."
    assert version_miss.get("candidates", []) == []
    with pytest_raises(ValueError, match="IDK"):
        service.learn_response("question", "  IDK  ", "learn-idk")
    return False


def test_regulated_resolution_is_concurrency_safe(tmp_path) -> bool:
    service = MCPConversationService()
    service.start(seed_path=str(_seed_file(tmp_path)))
    learned = service.learn_response("What is cached?", "This is cached.", "learn-concurrent")
    proposal = service.propose("What is cached?", "proposal-concurrent")

    def accept() -> dict:
        _return_value = service.resolve(proposal.get("proposal_id", ""), "accepted", statement_id=learned.get("statement_id", ""))
        return _return_value

    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(executor.map(lambda _: accept(), range(8)))

    assert sum(result.get("idempotent", False) is False for result in results) == 1
    assert service.runtime.engram.get_statement(learned.get("statement_id", "")).get("hit_count", 0) == 1
    return False


def test_regulated_retirement_is_limited_and_idempotent(tmp_path) -> bool:
    service = MCPConversationService()
    service.start(seed_path=str(_seed_file(tmp_path)))
    learned = service.learn_response("What is stale?", "An old answer.", "learn-stale")

    retired = service.retire_response(learned.get("statement_id", ""), "superseded_source_data", "retire-1")
    retry = service.retire_response(learned.get("statement_id", ""), "superseded_source_data", "retire-1")

    assert retired.get("retired", False) is True
    assert retry.get("idempotent", False) is True
    assert service.runtime.engram.get_statement(learned.get("statement_id", "")) == {}
    static_pattern_id = next(
        statement.get("id", "") for statement in service.runtime.engram.statements if statement.get("pattern", "") == "HELLO"
    )
    with pytest_raises(ValueError, match="dynamic, patternless"):
        service.retire_response(static_pattern_id, "not allowed", "retire-static")
    with pytest_raises(ValueError, match="different retirement"):
        service.retire_response(learned.get("statement_id", ""), "different reason", "retire-1")
    return False


def test_regulated_state_expires_and_is_not_persisted(tmp_path) -> bool:
    service = MCPConversationService()
    store = tmp_path / "engram.json"
    service.start(seed_path=str(_seed_file(tmp_path)), store_path=str(store))
    learned = service.learn_response("What persists?", "The learned response.", "learn-persist")
    proposal = service.propose("What persists?", "proposal-expiring")
    service.proposals[proposal.get("proposal_id", "")]["created_at"] = time_monotonic() - engram_service.PROPOSAL_TTL_SECONDS - 1
    with pytest_raises(ValueError, match="expired"):
        service.resolve(proposal.get("proposal_id", ""), "accepted", statement_id=learned.get("statement_id", ""))

    service.stop()
    service.start(seed_path="", store_path=str(store))
    persisted = service.propose("What persists?", "proposal-after-restart")
    assert persisted.get("candidates", [])[0].get("response", "") == "The learned response."
    with pytest_raises(ValueError, match="expired"):
        service.resolve(proposal.get("proposal_id", ""), "accepted", statement_id=learned.get("statement_id", ""))
    return False


def test_regulated_proposal_storage_is_bounded(tmp_path, monkeypatch) -> bool:
    monkeypatch.setattr(engram_service, "MAX_TRANSIENT_RECORDS", 2)
    service = MCPConversationService()
    service.start(seed_path=str(_seed_file(tmp_path)))

    oldest = service.propose("first uncached question", "bounded-1")
    service.propose("second uncached question", "bounded-2")
    service.propose("third uncached question", "bounded-3")

    assert len(service.proposals) == 2
    assert "bounded-1" not in service.proposal_requests
    with pytest_raises(ValueError, match="expired"):
        service.resolve(oldest.get("proposal_id", ""), "rejected_quality")
    return False


def test_mcpserver_tools_work_through_the_mcp_protocol(tmp_path) -> bool:
    async def exercise_protocol() -> bool:
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
                        "proposal_id": proposed.get("proposal_id", ""),
                        "outcome": "accepted",
                        "statement_id": learned.get("statement_id", ""),
                    },
                )
            )
            inspected = _tool_json(await client.call_tool("engram_inspect", {}))
            stopped = _tool_json(await client.call_tool("engram_stop", {}))

            assert started.get("turn_count", 0) == 0
            assert sent.get("response", "") == "Hello!"
            assert resolved.get("resolved", False) is True
            assert inspected.get("turn_count", 0) == 1
            assert inspected.get("regulated_cache", {}).get("accepted", 0) == 1
            assert stopped.get("summary", {}).get("exchanges", 0) == 1
        return False

    asyncio_run(exercise_protocol())
    return False
