"""Network-level contract tests for the single-instance gRPC adapter."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from logging import ERROR
from threading import Barrier as threading_Barrier, Event as threading_Event

from google.protobuf import empty_pb2, json_format, struct_pb2
from grpc import (
    Channel as grpc_Channel,
    RpcError as grpc_RpcError,
    StatusCode as grpc_StatusCode,
    channel_ready_future as grpc_channel_ready_future,
    insecure_channel as grpc_insecure_channel,
)
from grpc_health.v1 import health_pb2, health_pb2_grpc
from pytest import raises as pytest_raises

from engram import engram_pb2, engram_pb2_grpc, grpc_server as grpc_server_module
from engram.constants import MAX_REQUEST_BYTES
from engram.core import Engram
from engram.errors import InvalidRequestError
from engram.grpc_server import SERVICE_NAME, EngramGrpcServer
from engram.identity import build_standalone_identity, query_identity_to_dict
from engram.mcp_server import MCPConversationService
from engram.resolution import resolution_budget, resolution_budget_to_dict
from engram.service import EngramCore

from .support_fixtures import ASSERTION_REFERENCE_A


def internal_core() -> EngramCore:
    engram = Engram()
    engram.load_static_data(
        [
            {"pattern": "HELLO", "response": "Hello!"},
            {"pattern": "*", "response": "Go on."},
        ]
    )
    result = EngramCore(engram)
    return result


def as_dict(message: struct_pb2.Struct) -> dict:
    result = json_format.MessageToDict(message, preserving_proto_field_name=True)
    return result


def as_struct(value: dict) -> struct_pb2.Struct:
    result = struct_pb2.Struct()
    json_format.ParseDict(value, result)
    return result


@contextmanager
def running_server(core: EngramCore, **kwargs):
    server = EngramGrpcServer(core, bind_address="127.0.0.1:0", **kwargs)
    channel = grpc_insecure_channel(server.start())
    grpc_channel_ready_future(channel).result(timeout=5)
    try:
        yield server, channel, engram_pb2_grpc.EngramServiceStub(channel)
    finally:
        channel.close()
        server.stop(0)


def health_status(channel: grpc_Channel) -> int:
    health_stub = health_pb2_grpc.HealthStub(channel)
    result = health_stub.Check(health_pb2.HealthCheckRequest(service=SERVICE_NAME), timeout=5).status
    return result


def internal_trailing_metadata(error: grpc_RpcError) -> dict[str, str]:
    metadata = error.trailing_metadata()
    result = dict(metadata)
    return result


def test_conversation_fact_predicate_report_and_health_protocol() -> None:
    with running_server(internal_core()) as (_, channel, stub):
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

        assert alice["user_id"] == "Alice"
        assert carol["user_id"] == "Carol"
        assert first["turn"] == 1
        assert recalled["response"] == "Sushi is good."
        assert predicate.value == "curious"
        assert read_predicate.value == "curious"
        assert fact["introduced_by_user_id"] == ""
        assert fact["source_label"] == "research"
        assert inspected["session"]["previous_response"] == "Sushi is good."
        assert inspected["core_status"]["active_conversations"] == 2
        assert report["summary"]["exchanges"] == 1
        assert report["turns"][0]["input"] == "Sushi is good."
        assert status["healthy"] is True
        assert health_status(channel) == health_pb2.HealthCheckResponse.SERVING

        stopped = as_dict(stub.StopConversation(engram_pb2.UserRequest(user_id="Alice")))
        assert stopped["stopped"] is True
        assert as_dict(stub.GetStatus(empty_pb2.Empty()))["active_conversations"] == 1


def test_empty_wire_conversations_are_fresh_and_distinct_from_explicit_zero() -> None:
    core = internal_core()
    with running_server(core) as (_, _, stub):
        explicit = as_dict(
            stub.StartConversation(
                engram_pb2.StartConversationRequest(
                    user_id="0",
                    initial_bot_text="Explicit zero context.",
                )
            )
        )

        with pytest_raises(grpc_RpcError) as absent:
            stub.StopConversation(engram_pb2.UserRequest(user_id=""))
        assert absent.value.code() == grpc_StatusCode.NOT_FOUND
        assert explicit["user_id"] == "0"

        first_start = as_dict(stub.StartConversation(engram_pb2.StartConversationRequest(user_id="")))
        first_session_id = core.get_conversation("").session_id
        first_turn = as_dict(stub.Chat(engram_pb2.ChatRequest(user_id="", text="Hello")))
        first_stop = as_dict(stub.StopConversation(engram_pb2.UserRequest(user_id="")))

        second_start = as_dict(stub.StartConversation(engram_pb2.StartConversationRequest(user_id="")))
        second_session_id = core.get_conversation("").session_id
        second_turn = as_dict(stub.Chat(engram_pb2.ChatRequest(user_id="", text="Hello")))

        assert first_start["user_id"] == second_start["user_id"] == ""
        assert first_turn["user_id"] == second_turn["user_id"] == ""
        assert first_stop["user_id"] == ""
        assert first_session_id != second_session_id
        assert first_session_id not in core.engram.sessions
        assert first_turn["context_changes"]["previous_response"]["before"] == ""
        assert second_turn["context_changes"]["previous_response"]["before"] == ""
        assert (
            as_dict(stub.InspectConversation(engram_pb2.UserRequest(user_id="0")))["session"]["previous_response"]
            == "Explicit zero context."
        )


def test_regulated_cache_protocol_and_error_mapping() -> None:
    core = internal_core()
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
        resolved = as_dict(
            stub.Resolve(
                engram_pb2.ResolveRequest(
                    proposal_id=proposal["proposal_id"],
                    outcome=engram_pb2.REGULATOR_OUTCOME_ACCEPTED,
                    statement_id=learned["statement_id"],
                )
            )
        )
        retry = as_dict(
            stub.Resolve(
                engram_pb2.ResolveRequest(
                    proposal_id=proposal["proposal_id"],
                    outcome=engram_pb2.REGULATOR_OUTCOME_ACCEPTED,
                    statement_id=learned["statement_id"],
                )
            )
        )

        assert proposal["candidates"][0]["response"] == "Nine to five."
        assert resolved["idempotent"] is False
        assert retry["idempotent"] is True
        assert core.engram.response_repository.get_artifact(learned["statement_id"])["statistics"]["hit_count"] == 1

        with pytest_raises(grpc_RpcError) as invalid:
            stub.Resolve(engram_pb2.ResolveRequest(proposal_id=proposal["proposal_id"]))
        assert invalid.value.code() == grpc_StatusCode.INVALID_ARGUMENT
        assert internal_trailing_metadata(invalid.value)["engram-error-type"] == "InvalidRequestError"

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
        assert as_dict(stub.GetStatus(empty_pb2.Empty()))["state"] == "closed"
        assert health_status(channel) == health_pb2.HealthCheckResponse.NOT_SERVING


def test_learn_response_restores_integral_support_revisions_from_struct() -> None:
    core = internal_core()
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
    core = internal_core()
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

        assert result.schema_version == 1
        assert result.outcome == "ANSWER"
        assert result.selected_candidate_available is True
        assert as_dict(result.selected_candidate)["response"] == "The transport-neutral result."
        assert len(result.response_candidates) == 1
        assert result.evidence_package_available is False
        assert result.evidence_package.wire_version == 2
        assert result.evidence_package.retained_count == 0
        assert result.evidence_package.records == []


def test_evidence_service_decodes_json_facing_identity_and_budget_contracts() -> None:
    core = internal_core()
    identity = build_standalone_identity("Uncached evidence contract request")
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
    core = internal_core()
    with running_server(core) as (_, channel, _):
        stub = engram_pb2_grpc.EngramEvidenceServiceStub(channel)

        with pytest_raises(grpc_RpcError) as failure:
            stub.ResolveEvidence(
                engram_pb2.ResolveEvidenceRequest(
                    request="x" * (MAX_REQUEST_BYTES + 1),
                    request_id="oversized-evidence-request",
                )
            )

        assert failure.value.code() == grpc_StatusCode.INVALID_ARGUMENT
        assert internal_trailing_metadata(failure.value)["engram-error-type"] == "InvalidRequestError"
        assert core.resolution_requests == {}


def test_unhandled_grpc_failure_redacts_exception_content(caplog, monkeypatch) -> None:
    secret = "private-request-and-credential-content"
    core = internal_core()

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
    assert secret not in caplog.text
    assert "RuntimeError" in caplog.text


def test_exact_and_conflicting_mutation_retries_are_shared_across_python_mcp_and_grpc() -> None:
    mcp = MCPConversationService()
    mcp.start()
    core = mcp.core
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

        assert created["idempotent"] is False
        assert mcp_replay["idempotent"] is True
        assert grpc_replay["idempotent"] is True
        assert grpc_replay["statement_id"] == created["statement_id"]
        assert conflicting.value.code() == grpc_StatusCode.ABORTED
        assert len(core.engram.response_repository.snapshot()["artifacts"]) == 1


def test_concurrent_proposal_resolution_has_one_result_and_consistent_cross_adapter_visibility() -> None:
    mcp = MCPConversationService()
    mcp.start()
    core = mcp.core
    learned = core.learn_response("What is concurrent?", "One accepted result.", "concurrent-learn")
    proposal = core.propose("What is concurrent?", "concurrent-proposal")

    with running_server(core) as (_, _, stub):
        barrier = threading_Barrier(7)

        def python_resolve() -> dict:
            barrier.wait()
            result = core.resolve(proposal["proposal_id"], "accepted", learned["statement_id"])
            return result

        def mcp_resolve() -> dict:
            barrier.wait()
            result = mcp.resolve(proposal["proposal_id"], "accepted", learned["statement_id"])
            return result

        def grpc_resolve() -> dict:
            barrier.wait()
            result = stub.Resolve(
                engram_pb2.ResolveRequest(
                    proposal_id=proposal["proposal_id"],
                    outcome=engram_pb2.REGULATOR_OUTCOME_ACCEPTED,
                    statement_id=learned["statement_id"],
                )
            )
            result = as_dict(result)
            return result

        operations = (python_resolve, mcp_resolve, grpc_resolve, python_resolve, mcp_resolve, grpc_resolve)
        with ThreadPoolExecutor(max_workers=6) as executor:
            futures = [executor.submit(operation) for operation in operations]
            barrier.wait()
            results = [future.result(timeout=10) for future in futures]

        python_view = core.inspect_conversation("0")["session"]
        mcp_view = mcp.inspect()["session"]
        grpc_view = as_dict(stub.InspectConversation(engram_pb2.UserRequest(user_id="0")))["session"]

        assert sum(result["idempotent"] is False for result in results) == 1
        assert sum(result["idempotent"] is True for result in results) == 5
        assert core.engram.response_repository.get_artifact(learned["statement_id"])["statistics"]["hit_count"] == 1
        assert python_view["previous_response"] == mcp_view["previous_response"] == grpc_view["previous_response"]


def test_grpc_restart_begins_with_empty_process_memory() -> None:
    first_core = internal_core()
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

    restarted_core = internal_core()
    with running_server(restarted_core) as (_, _, restarted_stub):
        started = as_dict(restarted_stub.StartConversation(engram_pb2.StartConversationRequest(user_id="static-after-restart")))
        static_turn = as_dict(restarted_stub.Chat(engram_pb2.ChatRequest(user_id="static-after-restart", text="hello")))
        assert started.get("statement_count") == 2
        assert static_turn.get("response") == "Hello!"
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
    core = internal_core()
    entered = threading_Event()
    release = threading_Event()
    original_chat = core.chat

    def delayed_chat(user_id: str, text: str) -> dict:
        entered.set()
        assert release.wait(timeout=5)
        result = original_chat(user_id, text)
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
        assert inspected["turn_count"] == 1
        assert inspected["latest_turn"]["response"] == "Hello!"


def test_graceful_shutdown_drains_an_in_flight_rpc() -> None:
    core = internal_core()
    entered = threading_Event()
    release = threading_Event()
    original_chat = core.chat

    def delayed_chat(user_id: str, text: str) -> dict:
        entered.set()
        assert release.wait(timeout=5)
        result = original_chat(user_id, text)
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
        assert as_dict(chat_future.result(timeout=5))["response"] == "Hello!"
        assert stop_future.result(timeout=5) is True

    channel.close()
    assert core.status()["state"] == "closed"
    assert server.stop(0) is False


def test_tls_requires_a_certificate_and_key_pair() -> None:
    core = internal_core()
    with pytest_raises(InvalidRequestError, match="together"):
        EngramGrpcServer(core, bind_address="127.0.0.1:0", tls_certificate=b"certificate")
    core.close()


def test_grpc_main_refuses_to_serve_after_required_component_preflight_failure(monkeypatch) -> None:
    def fail_open(**kwargs):
        del kwargs
        raise InvalidRequestError("component preflight failed: required NLTK data unavailable")

    monkeypatch.setattr(grpc_server_module, "open_engram_core", fail_open)

    assert grpc_server_module.main(["--log-level", "ERROR"]) == 1


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
