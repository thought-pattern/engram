"""Network-level contract tests for the single-instance gRPC adapter."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from logging import ERROR
from threading import Barrier as threading_Barrier, Event as threading_Event

from google.protobuf import empty_pb2, json_format, struct_pb2
from grpc import (
    RpcError as grpc_RpcError,
    StatusCode as grpc_StatusCode,
    channel_ready_future as grpc_channel_ready_future,
    insecure_channel as grpc_insecure_channel,
)
from grpc_health.v1 import health_pb2, health_pb2_grpc
from pytest import raises as pytest_raises

from engram import engram_pb2, engram_pb2_grpc, grpc_server as grpc_server_module
from engram.config import engram_config, graph_config
from engram.constants import MAX_CACHE_REQUEST_BYTES, SessionOverflow
from engram.core import Engram
from engram.errors import InvalidRequestError
from engram.grpc_server import SERVICE_NAME, EngramGrpcServer
from engram.identity import extract_standalone_identity, query_identity_to_dict
from engram.mcp_server import MCPConversationService
from engram.resolution import resolution_budget, resolution_budget_to_dict
from engram.service import EngramCore

from .support_fixtures import ASSERTION_REFERENCE_A
from .test_relation import RelationGraph
from .test_scope_removal import RemovalExample


class GrpcCore(EngramCore):
    def __init__(self) -> None:
        engram = Engram()
        engram.load_static_data(
            [
                {"pattern": "HELLO", "response": "Hello!"},
                {"pattern": "*", "response": "Go on."},
            ]
        )
        super().__init__(engram)


def as_dict(message) -> dict:
    result = json_format.MessageToDict(message, preserving_proto_field_name=True)
    return result


def as_struct(value: dict):
    result = struct_pb2.Struct()
    json_format.ParseDict(value, result)
    return result


@contextmanager
def running_server(core: EngramCore, **kwargs):
    server = EngramGrpcServer(core, bind_address="127.0.0.1:0", **kwargs)
    try:
        channel = grpc_insecure_channel(server.start())
        try:
            grpc_channel_ready_future(channel).result(timeout=5)
            yield server, channel, engram_pb2_grpc.EngramServiceStub(channel)
        finally:
            channel.close()
    finally:
        server.stop(0)


def health_status(channel) -> int:
    health_stub = health_pb2_grpc.HealthStub(channel)
    check = getattr(health_stub, "Check", ())
    if not callable(check):
        raise RuntimeError("gRPC health stub has no Check operation")
    response = check(health_pb2.HealthCheckRequest(service=SERVICE_NAME), timeout=5)
    if not isinstance(response, health_pb2.HealthCheckResponse):
        raise RuntimeError("gRPC health stub returned a malformed response")
    result = response.status
    return result


def internal_trailing_metadata(error) -> dict[str, str]:
    metadata = error.trailing_metadata()
    result: dict[str, str] = {}
    for item in metadata:
        key = getattr(item, "key", ())
        value = getattr(item, "value", ())
        selected_key = key.decode("utf-8") if isinstance(key, bytes) else key
        selected_value = value.decode("utf-8") if isinstance(value, bytes) else value
        if not isinstance(selected_key, str) or not isinstance(selected_value, str):
            raise RuntimeError("gRPC trailing metadata is malformed")
        result[selected_key] = selected_value
    return result


def test_engagement_maintenance_protocol_deletes_one_scope_and_keeps_peer_responses():
    example = RemovalExample()
    with running_server(example.core) as (_, _, stub):
        for action in ("plan", "prepare", "purge", "purge", "resume"):
            request = struct_pb2.Struct()
            request.update(example.command(action))
            result = as_dict(stub.MaintainEngagement(request))
            if action == "plan":
                assert result.get("dry_run", False) is True
                assert len(result.get("selected_statement_ids", [])) == 3
            elif action == "prepare":
                assert result.get("drained", False) is True
                paused_status = as_dict(stub.GetStatus(empty_pb2.Empty()))
                assert "ready" in paused_status
                assert paused_status.get("ready", False) is False
                with pytest_raises(grpc_RpcError) as paused:
                    stub.LearnResponse(
                        engram_pb2.LearnResponseRequest(request="late", response="late private response", request_id="late-wire")
                    )
                assert paused.value.code() == grpc_StatusCode.FAILED_PRECONDITION
            elif action == "purge":
                assert result.get("purged", False) is True
        assert as_dict(stub.GetStatus(empty_pb2.Empty())).get("ready", False) is True
        artifacts = example.core.engram.response_repository.snapshot().get("artifacts", {})
        assert set(artifacts) == {example.ids.get(name, "") for name in ("public", "company", "engagement_b")}


def test_conversation_fact_predicate_report_and_health_protocol() -> None:
    with running_server(GrpcCore()) as (_, channel, stub):
        alice = as_dict(stub.StartConversation(engram_pb2.StartConversationRequest(user_id="Alice", random_seed=7)))
        carol = as_dict(stub.StartConversation(engram_pb2.StartConversationRequest(user_id="Carol")))
        first = as_dict(stub.Chat(engram_pb2.ChatRequest(user_id="Alice", text="Sushi is good.")))
        recalled = as_dict(stub.Chat(engram_pb2.ChatRequest(user_id="Carol", text="What's good?")))

        predicate = stub.SetPredicate(engram_pb2.SetPredicateRequest(user_id="Alice", name="mood", value="curious"))
        read_predicate = stub.GetPredicate(engram_pb2.GetPredicateRequest(user_id="Alice", name="mood"))
        fact = as_dict(stub.AddFact(engram_pb2.AddFactRequest(text="Tokyo is the capital of Japan.", source_label="research")))
        inspected = as_dict(stub.InspectConversation(engram_pb2.UserRequest(user_id="Carol")))
        report = as_dict(stub.FinishConversation(engram_pb2.UserRequest(user_id="Alice")))
        status = as_dict(stub.GetStatus(empty_pb2.Empty()))

        assert alice.get("user_id", "") == "Alice"
        assert carol.get("user_id", "") == "Carol"
        assert first.get("turn", 0) == 1
        assert recalled.get("response", "") == "Sushi is good."
        assert predicate.value == "curious"
        assert read_predicate.value == "curious"
        assert "introduced_by_user_id" in fact
        assert fact.get("introduced_by_user_id", "") == ""
        assert fact.get("source_label", "") == "research"
        assert inspected.get("session", {}).get("previous_response", "") == "Sushi is good."
        assert inspected.get("core_status", {}).get("active_conversations", 0) == 2
        assert report.get("summary", {}).get("exchanges", 0) == 1
        assert "turns" not in report
        assert status.get("healthy", False) is True
        assert health_status(channel) == health_pb2.HealthCheckResponse.SERVING

        stopped = as_dict(stub.StopConversation(engram_pb2.UserRequest(user_id="Alice")))
        assert stopped.get("stopped", False) is True
        assert as_dict(stub.GetStatus(empty_pb2.Empty())).get("active_conversations", 0) == 1


def test_every_call_ends_by_letting_a_lost_graph_connection_reconnect() -> None:
    calls = []

    class ReconnectRecordingCore(GrpcCore):
        def reconnect_graph_after_turn(self) -> None:
            calls.append(len(self.active_resolution_request_ids))
            super().reconnect_graph_after_turn()

    with running_server(ReconnectRecordingCore()) as (_, _, stub):
        stub.StartConversation(engram_pb2.StartConversationRequest(user_id="Alice"))
        stub.AddFact(engram_pb2.AddFactRequest(text="Tokyo is the capital of Japan."))
        # A chat turn ends its resolution slot, then its call.
        stub.Chat(engram_pb2.ChatRequest(user_id="Alice", text="Hello"))

    assert calls == [0, 0, 0, 0]


def test_unknown_user_wire_lifecycle_uses_explicit_zero() -> None:
    core = GrpcCore()
    with running_server(core) as (_, _, stub):
        started = as_dict(
            stub.StartConversation(engram_pb2.StartConversationRequest(initial_bot_text="Unknown context."), timeout=5)
        )
        token = started.get("conversation_token", "")
        turn = as_dict(stub.Chat(engram_pb2.ChatRequest(user_id="", text="Hello", conversation_token=token), timeout=5))
        stub.SetPredicate(engram_pb2.SetPredicateRequest(user_id="", name="mood", value="curious"), timeout=5)
        assert stub.GetPredicate(engram_pb2.GetPredicateRequest(user_id="0", name="mood"), timeout=5).value == "curious"
        inspected = as_dict(stub.InspectConversation(engram_pb2.UserRequest(user_id="0", conversation_token=token), timeout=5))
        report = as_dict(stub.FinishConversation(engram_pb2.UserRequest(user_id="", conversation_token=token), timeout=5))
        assert core.get_conversation("0").user_id == "0"
        with pytest_raises(grpc_RpcError) as unowned:
            stub.StopConversation(engram_pb2.UserRequest(user_id="0"), timeout=5)
        assert unowned.value.code() == grpc_StatusCode.PERMISSION_DENIED
        stopped = as_dict(stub.StopConversation(engram_pb2.UserRequest(user_id="0", conversation_token=token), timeout=5))

        assert started.get("user_id", "") == "0"
        assert turn.get("user_id", "") == "0"
        assert inspected.get("user_id", "") == "0"
        assert report.get("user_id", "") == "0"
        assert stopped.get("user_id", "") == "0"
        assert turn.get("context_changes", {}).get("previous_response", {}).get("before", "") == "Unknown context."
        assert inspected.get("session", {}).get("previous_response", "") == turn.get("response", "")
        assert "0" not in core.engram.sessions


def test_regulated_cache_protocol_and_error_mapping() -> None:
    core = GrpcCore()
    with running_server(core) as (_, channel, stub):
        learned = as_dict(
            stub.LearnResponse(
                engram_pb2.LearnResponseRequest(
                    request="When are you open?",
                    response="Nine to five.",
                    request_id="learn-1",
                    user_id="Alice",
                    namespace="support",
                    metadata=struct_pb2.Struct(fields={"actor_version": struct_pb2.Value(string_value="actor-7")}),
                )
            )
        )
        proposal = as_dict(
            stub.Propose(
                engram_pb2.ProposeRequest(
                    request="When are you open?",
                    request_id="proposal-1",
                    user_id="Carol",
                    namespace="support",
                    required_metadata=struct_pb2.Struct(fields={"actor_version": struct_pb2.Value(string_value="actor-7")}),
                )
            )
        )
        proposal_id = proposal.get("proposal_id", "")
        statement_id = learned.get("statement_id", "")
        assert proposal_id
        assert statement_id
        resolved = as_dict(
            stub.Resolve(
                engram_pb2.ResolveRequest(
                    proposal_id=proposal_id,
                    outcome=engram_pb2.REGULATOR_OUTCOME_ACCEPTED,
                    statement_id=statement_id,
                )
            )
        )
        retry = as_dict(
            stub.Resolve(
                engram_pb2.ResolveRequest(
                    proposal_id=proposal_id,
                    outcome=engram_pb2.REGULATOR_OUTCOME_ACCEPTED,
                    statement_id=statement_id,
                )
            )
        )

        assert proposal.get("candidates", [])[0].get("response", "") == "Nine to five."
        assert "idempotent" in resolved
        assert resolved.get("idempotent", False) is False
        assert retry.get("idempotent", False) is True
        artifact = core.engram.response_repository.get_artifact(statement_id)
        assert artifact.get("statistics", {}).get("hit_count", 0) == 1

        with pytest_raises(grpc_RpcError) as invalid:
            stub.Resolve(engram_pb2.ResolveRequest(proposal_id=proposal_id))
        assert invalid.value.code() == grpc_StatusCode.INVALID_ARGUMENT
        assert internal_trailing_metadata(invalid.value).get("engram-error-type", "") == "InvalidRequestError"

        with pytest_raises(grpc_RpcError) as missing:
            stub.Chat(engram_pb2.ChatRequest(user_id="Missing", text="hello"))
        assert missing.value.code() == grpc_StatusCode.NOT_FOUND

        stub.StartConversation(engram_pb2.StartConversationRequest(user_id="Alice"))
        with pytest_raises(grpc_RpcError) as conflict:
            stub.StartConversation(engram_pb2.StartConversationRequest(user_id="Alice"))
        assert conflict.value.code() == grpc_StatusCode.ABORTED

        core.close()
        with pytest_raises(grpc_RpcError) as closed:
            stub.AddFact(engram_pb2.AddFactRequest(text="Too late."))
        assert closed.value.code() == grpc_StatusCode.FAILED_PRECONDITION
        assert as_dict(stub.GetStatus(empty_pb2.Empty())).get("state", "") == "closed"
        assert health_status(channel) == health_pb2.HealthCheckResponse.NOT_SERVING


def test_learn_response_restores_integral_support_revisions_from_struct() -> None:
    core = GrpcCore()
    with running_server(core) as (_, internal_channel, stub):
        learned = as_dict(
            stub.LearnResponse(
                engram_pb2.LearnResponseRequest(
                    request="Which response has durable support?",
                    response="This response has durable support.",
                    request_id="learn-struct-support-integers",
                    user_id="",
                    metadata=as_struct({"support": [ASSERTION_REFERENCE_A]}),
                )
            )
        )

    assert learned.get("learned", False) is True
    assert learned.get("statement_id", "")


def test_evidence_service_delegates_unified_resolution_to_the_shared_core() -> None:
    core = GrpcCore()
    with running_server(core) as (_, channel, service_stub):
        service_stub.LearnResponse(
            engram_pb2.LearnResponseRequest(
                request="What is served through unified resolution?",
                response="The transport-neutral result.",
                request_id="learn-evidence",
            )
        )
        stub = engram_pb2_grpc.EngramEvidenceServiceStub(channel)

        result = stub.ResolveEvidence(
            engram_pb2.ResolveEvidenceRequest(
                request="What is served through unified resolution?",
                request_id="resolve-evidence",
                user_id="Alice",
                configured_resolvers=("exact",),
                accept_exact=True,
            )
        )

        assert result.outcome == "ANSWER"
        assert result.selected_candidate_available is True
        assert as_dict(result.selected_candidate).get("response", "") == "The transport-neutral result."
        assert len(result.response_candidates) == 1
        assert result.evidence_package_available is False
        assert result.evidence_package.retained_count == 0
        assert result.evidence_package.records == []


def test_grpc_evidence_service_uses_shared_graph_resolution(monkeypatch) -> None:
    graph = RelationGraph()
    monkeypatch.setattr("engram.core.connect_graph", lambda **internal_kwargs: graph)
    core = EngramCore(
        Engram(
            engram_config(
                graph=graph_config(enabled=True),
            )
        )
    )
    with running_server(core) as (_, channel, _):
        stub = engram_pb2_grpc.EngramEvidenceServiceStub(channel)

        result = stub.ResolveEvidence(
            engram_pb2.ResolveEvidenceRequest(
                request="Where was Ada Lovelace born?",
                request_id="grpc-graph-query",
                configured_resolvers=("exact",),
            )
        )

    assert result.outcome == "EVIDENCE"
    assert as_dict(result.response_candidates[0]).get("response", "") == "Ada Lovelace — birth place: London."
    assert graph.one_hop_calls == [("entity:ada-lovelace", "predicate:birth-place", 10, False)]


def test_evidence_service_decodes_json_facing_identity_and_budget_contracts() -> None:
    core = GrpcCore()
    identity = extract_standalone_identity("Uncached evidence contract request")
    budget = resolution_budget()
    with running_server(core) as (_, channel, _):
        stub = engram_pb2_grpc.EngramEvidenceServiceStub(channel)

        result = stub.ResolveEvidence(
            engram_pb2.ResolveEvidenceRequest(
                request="Uncached evidence contract request",
                request_id="resolve-evidence-contracts",
                identity=as_struct(query_identity_to_dict(identity)),
                budget=as_struct(resolution_budget_to_dict(budget)),
                configured_resolvers=("exact",),
            )
        )

        assert result.outcome == "MISS"


def test_evidence_service_enforces_the_shared_request_bound() -> None:
    core = GrpcCore()
    with running_server(core) as (_, channel, _):
        stub = engram_pb2_grpc.EngramEvidenceServiceStub(channel)

        with pytest_raises(grpc_RpcError) as failure:
            stub.ResolveEvidence(
                engram_pb2.ResolveEvidenceRequest(
                    request="x" * (MAX_CACHE_REQUEST_BYTES + 1),
                    request_id="oversized-evidence-request",
                )
            )

        assert failure.value.code() == grpc_StatusCode.INVALID_ARGUMENT
        assert failure.value.details() == f"request exceeds the limit of {MAX_CACHE_REQUEST_BYTES} UTF-8 bytes"
        assert internal_trailing_metadata(failure.value).get("engram-error-type", "") == "InvalidRequestError"
        assert core.resolution_requests == {}


def test_unhandled_grpc_failure_is_logged_in_full_and_redacted_for_the_client(caplog, monkeypatch) -> None:
    secret = "private-request-and-credential-content"
    core = GrpcCore()

    def fail(internal_text, source_label=""):
        raise RuntimeError(secret)

    monkeypatch.setattr(core, "add_fact", fail)
    with (
        caplog.at_level(ERROR, logger="engram.grpc_server"),
        running_server(core) as (_, _, stub),
        pytest_raises(grpc_RpcError) as failure,
    ):
        stub.AddFact(engram_pb2.AddFactRequest(text="trigger failure"))

    assert failure.value.code() == grpc_StatusCode.INTERNAL
    assert failure.value.details() == "internal Engram failure"
    assert secret in caplog.text
    assert "Traceback" in caplog.text


def test_session_limit_is_resource_exhausted() -> None:
    class LimitedCore(EngramCore):
        def __init__(self) -> None:
            super().__init__(Engram(config=engram_config(max_sessions=1, session_overflow=SessionOverflow.REJECT)))

    with running_server(LimitedCore()) as (_, _, stub):
        stub.StartConversation(engram_pb2.StartConversationRequest(user_id="Alice"))
        with pytest_raises(grpc_RpcError) as failure:
            stub.StartConversation(engram_pb2.StartConversationRequest(user_id="Bob"))

    # The core's conversation registry refuses the second start before any session is created.
    assert failure.value.code() == grpc_StatusCode.RESOURCE_EXHAUSTED
    assert failure.value.details() == "maximum active conversations reached"


def test_chat_validation_is_invalid_argument_but_an_internal_value_error_is_internal(caplog, monkeypatch) -> None:
    secret = "private-chat-pipeline-detail"

    def fail(*internal_args, **internal_kwargs):
        raise ValueError(secret)

    with caplog.at_level(ERROR, logger="engram.grpc_server"), running_server(GrpcCore()) as (_, _, stub):
        stub.StartConversation(engram_pb2.StartConversationRequest(user_id="Alice"))
        with pytest_raises(grpc_RpcError) as invalid:
            stub.Chat(engram_pb2.ChatRequest(user_id="Alice", text=" "))
        monkeypatch.setattr("engram.conversation.pipeline.respond", fail)
        with pytest_raises(grpc_RpcError) as internal:
            stub.Chat(engram_pb2.ChatRequest(user_id="Alice", text="Hello"))

    assert invalid.value.code() == grpc_StatusCode.INVALID_ARGUMENT
    assert invalid.value.details() == "text must be one non-empty string"
    assert internal.value.code() == grpc_StatusCode.INTERNAL
    assert internal.value.details() == "internal Engram failure"
    assert secret in caplog.text


def test_exact_and_conflicting_mutation_retries_are_shared_across_python_mcp_and_grpc() -> None:
    mcp = MCPConversationService()
    mcp.start()
    core, _ = mcp.require_active()
    created = core.learn_response("What is shared?", "One shared result.", "cross-adapter-learn")
    mcp_replay = mcp.learn_response("What is shared?", "One shared result.", "cross-adapter-learn")

    with running_server(core) as (_, _, stub):
        grpc_replay = as_dict(
            stub.LearnResponse(
                engram_pb2.LearnResponseRequest(
                    request="What is shared?",
                    response="One shared result.",
                    request_id="cross-adapter-learn",
                )
            )
        )
        with pytest_raises(grpc_RpcError) as conflicting:
            stub.LearnResponse(
                engram_pb2.LearnResponseRequest(
                    request="What is shared?",
                    response="A conflicting result.",
                    request_id="cross-adapter-learn",
                )
            )

        created_statement_id = created.get("statement_id", "")
        assert "idempotent" in created
        assert created.get("idempotent", False) is False
        assert mcp_replay.get("idempotent", False) is True
        assert grpc_replay.get("idempotent", False) is True
        assert created_statement_id
        assert grpc_replay.get("statement_id", "") == created_statement_id
        assert conflicting.value.code() == grpc_StatusCode.ABORTED
        assert len(core.engram.response_repository.snapshot().get("artifacts", {})) == 1


def test_concurrent_proposal_resolution_has_one_result_and_consistent_cross_adapter_visibility() -> None:
    mcp = MCPConversationService()
    mcp.start()
    core, _ = mcp.require_active()
    learned = core.learn_response("What is concurrent?", "One accepted result.", "concurrent-learn")
    proposal = core.propose("What is concurrent?", "concurrent-proposal")
    proposal_id = proposal.get("proposal_id", "")
    statement_id = learned.get("statement_id", "")
    assert proposal_id
    assert statement_id

    with running_server(core) as (_, _, stub):
        barrier = threading_Barrier(7)

        def python_resolve() -> dict:
            barrier.wait()
            result = core.resolve(proposal_id, "accepted", statement_id)
            return result

        def mcp_resolve() -> dict:
            barrier.wait()
            result = mcp.resolve(proposal_id, "accepted", statement_id)
            return result

        def grpc_resolve() -> dict:
            barrier.wait()
            result = stub.Resolve(
                engram_pb2.ResolveRequest(
                    proposal_id=proposal_id,
                    outcome=engram_pb2.REGULATOR_OUTCOME_ACCEPTED,
                    statement_id=statement_id,
                )
            )
            result = as_dict(result)
            return result

        operations = (python_resolve, mcp_resolve, grpc_resolve, python_resolve, mcp_resolve, grpc_resolve)
        with ThreadPoolExecutor(max_workers=6) as executor:
            futures = [executor.submit(operation) for operation in operations]
            barrier.wait()
            results = [future.result(timeout=10) for future in futures]

        python_view = core.inspect_conversation("0", conversation_token=mcp.active_conversation_token).get("session", {})
        mcp_view = mcp.inspect().get("session", {})
        grpc_view = as_dict(
            stub.InspectConversation(engram_pb2.UserRequest(user_id="0", conversation_token=mcp.active_conversation_token))
        ).get("session", {})

        assert all("idempotent" in result for result in results)
        assert sum(result.get("idempotent", False) is False for result in results) == 1
        assert sum(result.get("idempotent", False) is True for result in results) == 5
        artifact = core.engram.response_repository.get_artifact(statement_id)
        assert artifact.get("statistics", {}).get("hit_count", 0) == 1
        assert "previous_response" in python_view
        assert "previous_response" in mcp_view
        assert "previous_response" in grpc_view
        assert (
            python_view.get("previous_response", "")
            == mcp_view.get("previous_response", "")
            == grpc_view.get("previous_response", "")
        )


def test_grpc_restart_begins_with_empty_process_memory() -> None:
    first_core = GrpcCore()
    with running_server(first_core) as (_, _, first_stub):
        learned = as_dict(
            first_stub.LearnResponse(
                engram_pb2.LearnResponseRequest(
                    request="What is process-local?",
                    response="This answer belongs to one process.",
                    request_id="learn-before-restart",
                )
            )
        )
        old_proposal = as_dict(
            first_stub.Propose(
                engram_pb2.ProposeRequest(
                    request="What is process-local?",
                    request_id="proposal-before-restart",
                )
            )
        )
        assert old_proposal.get("candidates", [])[0].get("statement_id", "") == learned.get("statement_id", "")

    restarted_core = GrpcCore()
    with running_server(restarted_core) as (_, _, restarted_stub):
        started = as_dict(restarted_stub.StartConversation(engram_pb2.StartConversationRequest(user_id="static-after-restart")))
        static_turn = as_dict(restarted_stub.Chat(engram_pb2.ChatRequest(user_id="static-after-restart", text="hello")))
        assert started.get("statement_count", 0) == 2
        assert static_turn.get("response", "") == "Hello!"
        proposal = as_dict(
            restarted_stub.Propose(
                engram_pb2.ProposeRequest(
                    request="What is process-local?",
                    request_id="proposal-after-restart",
                )
            )
        )
        assert proposal.get("candidates", []) == []
        with pytest_raises(grpc_RpcError) as expired:
            restarted_stub.Resolve(
                engram_pb2.ResolveRequest(
                    proposal_id=old_proposal.get("proposal_id", ""),
                    outcome=engram_pb2.REGULATOR_OUTCOME_ACCEPTED,
                    statement_id=learned.get("statement_id", ""),
                )
            )
        assert expired.value.code() == grpc_StatusCode.NOT_FOUND


def test_deadline_does_not_proposition_to_roll_back_started_core_work() -> None:
    core = GrpcCore()
    entered = threading_Event()
    release = threading_Event()
    original_chat = core.chat

    def delayed_chat(user_id: str, text: str, conversation_token: str = "") -> dict:
        entered.set()
        assert release.wait(timeout=5)
        result = original_chat(user_id, text, conversation_token=conversation_token)
        return result

    core.chat = delayed_chat
    with running_server(core) as (_, _, stub):
        stub.StartConversation(engram_pb2.StartConversationRequest(user_id="Alice"))
        with pytest_raises(grpc_RpcError) as deadline:
            stub.Chat(engram_pb2.ChatRequest(user_id="Alice", text="hello"), timeout=0.05)
        assert deadline.value.code() == grpc_StatusCode.DEADLINE_EXCEEDED
        assert entered.is_set()

        release.set()
        inspected = as_dict(stub.InspectConversation(engram_pb2.UserRequest(user_id="Alice"), timeout=5))
        assert inspected.get("turn_count", 0) == 1
        assert inspected.get("latest_turn", {}).get("response", "") == "Hello!"


def test_graceful_shutdown_drains_an_in_flight_rpc() -> None:
    core = GrpcCore()
    entered = threading_Event()
    release = threading_Event()
    original_chat = core.chat

    def delayed_chat(user_id: str, text: str, conversation_token: str = "") -> dict:
        entered.set()
        assert release.wait(timeout=5)
        result = original_chat(user_id, text, conversation_token=conversation_token)
        return result

    core.chat = delayed_chat
    server = EngramGrpcServer(core, bind_address="127.0.0.1:0")
    channel = grpc_insecure_channel(server.start())
    stub = engram_pb2_grpc.EngramServiceStub(channel)
    stub.StartConversation(engram_pb2.StartConversationRequest(user_id="Alice"))
    chat_future = stub.Chat.future(engram_pb2.ChatRequest(user_id="Alice", text="hello"), timeout=5)
    assert entered.wait(timeout=5)

    with ThreadPoolExecutor(max_workers=1) as executor:
        stop_future = executor.submit(server.stop, 2)
        assert stop_future.done() is False
        release.set()
        assert as_dict(chat_future.result(timeout=5)).get("response", "") == "Hello!"
        assert stop_future.result(timeout=5) is True

    channel.close()
    assert core.status().get("state", "") == "closed"
    assert server.stop(0) is False


def test_tls_requires_a_certificate_and_key_pair() -> None:
    core = GrpcCore()
    with pytest_raises(InvalidRequestError, match="together"):
        EngramGrpcServer(core, bind_address="127.0.0.1:0", tls_certificate=b"certificate")
    core.close()


def test_grpc_main_refuses_to_serve_after_required_component_preflight_failure(monkeypatch) -> None:
    class FailingCore:
        def __init__(self, **kwargs):
            del kwargs
            raise InvalidRequestError("component preflight failed: required NLTK data unavailable")

    monkeypatch.setattr(grpc_server_module, "EngramCore", FailingCore)

    assert grpc_server_module.main(("--log-level", "ERROR")) == 1


def test_grpc_console_entry_point_forwards_process_arguments(monkeypatch) -> None:
    observed = []

    def run(argv=()):
        observed.extend(argv)
        return 7

    monkeypatch.setattr(grpc_server_module, "main", run)
    monkeypatch.setattr(
        grpc_server_module,
        "sys_argv",
        ["engram-grpc", "--bind", "127.0.0.1:9901", "--tls-cert", "server.pem"],
    )

    assert grpc_server_module.console_main() == 7
    assert observed == ["--bind", "127.0.0.1:9901", "--tls-cert", "server.pem"]
