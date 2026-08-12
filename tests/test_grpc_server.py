"""Network-level contract tests for the single-instance gRPC adapter."""

import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path

import grpc
import pytest
from google.protobuf import empty_pb2, json_format, struct_pb2
from grpc_health.v1 import health_pb2, health_pb2_grpc

from engram import grpc_server as grpc_server_module, service as service_module
from engram.constants import Tier
from engram.core import Engram
from engram.errors import InvalidRequestError, PersistenceError
from engram.grpc_server import SERVICE_NAME, create_grpc_server
from engram.service import EngramCore
from engram.v1 import engram_pb2, engram_pb2_grpc


def _core(store_path="") -> EngramCore:
    engram = Engram()
    engram.store("Hello!", pattern="HELLO", tier=Tier.STATIC)
    engram.store("Go on.", pattern="*", tier=Tier.STATIC)
    return EngramCore(engram, store_path=store_path)


def _as_dict(message: struct_pb2.Struct) -> dict:
    return json_format.MessageToDict(message, preserving_proto_field_name=True)


@contextmanager
def _running_server(core: EngramCore, **kwargs):
    server = create_grpc_server(core, bind_address="127.0.0.1:0", **kwargs)
    channel = grpc.insecure_channel(server.start())
    grpc.channel_ready_future(channel).result(timeout=5)
    try:
        yield server, channel, engram_pb2_grpc.EngramServiceStub(channel)
    finally:
        channel.close()
        server.stop(0)


def _health_status(channel: grpc.Channel) -> int:
    health_stub = health_pb2_grpc.HealthStub(channel)
    return health_stub.Check(health_pb2.HealthCheckRequest(service=SERVICE_NAME), timeout=5).status


def test_conversation_fact_predicate_report_and_health_protocol(tmp_path) -> None:
    store = tmp_path / "state" / "engram.json"
    transcript_directory = tmp_path / "transcripts"
    report_directory = tmp_path / "reports"

    with _running_server(
        _core(store),
        transcript_directory=transcript_directory,
        report_directory=report_directory,
    ) as (_, channel, stub):
        alice = _as_dict(stub.StartConversation(engram_pb2.StartConversationRequest(user_id="Alice", random_seed=7)))
        carol = _as_dict(stub.StartConversation(engram_pb2.StartConversationRequest(user_id="Carol")))
        first = _as_dict(stub.Chat(engram_pb2.ChatRequest(user_id="Alice", text="Sushi is good.")))
        recalled = _as_dict(stub.Chat(engram_pb2.ChatRequest(user_id="Carol", text="What's good?")))

        predicate = stub.SetPredicate(engram_pb2.SetPredicateRequest(user_id="Alice", name="mood", value="curious"))
        read_predicate = stub.GetPredicate(engram_pb2.GetPredicateRequest(user_id="Alice", name="mood"))
        fact = _as_dict(stub.AddFact(engram_pb2.AddFactRequest(text="Tokyo is the capital of Japan.", source_label="research")))
        inspected = _as_dict(stub.InspectConversation(engram_pb2.UserRequest(user_id="Carol")))
        report = _as_dict(stub.FinishConversation(engram_pb2.UserRequest(user_id="Alice")))
        status = _as_dict(stub.GetStatus(empty_pb2.Empty()))

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
        assert Path(report["json"]).is_file()
        assert Path(report["markdown"]).is_file()
        assert len(list(transcript_directory.glob("*.json"))) == 2
        assert status["healthy"] is True
        assert _health_status(channel) == health_pb2.HealthCheckResponse.SERVING
        assert stub.Flush(empty_pb2.Empty()).persisted is True

        stopped = _as_dict(stub.StopConversation(engram_pb2.UserRequest(user_id="Alice")))
        assert stopped["stopped"] is True
        assert _as_dict(stub.GetStatus(empty_pb2.Empty()))["active_conversations"] == 1

    assert store.is_file()


def test_regulated_cache_protocol_and_error_mapping() -> None:
    core = _core()
    with _running_server(core) as (_, channel, stub):
        learned = _as_dict(
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
        proposal = _as_dict(
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
        resolved = _as_dict(
            stub.Resolve(
                engram_pb2.ResolveRequest(
                    proposal_id=proposal["proposal_id"],
                    outcome=engram_pb2.REGULATOR_OUTCOME_ACCEPTED,
                    statement_id=learned["statement_id"],
                )
            )
        )
        retry = _as_dict(
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
        assert core.engram.get_statement(learned["statement_id"])["hit_count"] == 1

        with pytest.raises(grpc.RpcError) as invalid:
            stub.Resolve(engram_pb2.ResolveRequest(proposal_id=proposal["proposal_id"]))
        assert invalid.value.code() == grpc.StatusCode.INVALID_ARGUMENT
        assert dict(invalid.value.trailing_metadata())["engram-error-type"] == "InvalidRequestError"

        with pytest.raises(grpc.RpcError) as missing:
            stub.Chat(engram_pb2.ChatRequest(user_id="Missing", text="hello"))
        assert missing.value.code() == grpc.StatusCode.NOT_FOUND

        stub.StartConversation(engram_pb2.StartConversationRequest(user_id="Alice"))
        with pytest.raises(grpc.RpcError) as conflict:
            stub.StartConversation(engram_pb2.StartConversationRequest(user_id="Alice"))
        assert conflict.value.code() == grpc.StatusCode.ABORTED

        core.close(flush=False)
        with pytest.raises(grpc.RpcError) as closed:
            stub.AddFact(engram_pb2.AddFactRequest(text="Too late."))
        assert closed.value.code() == grpc.StatusCode.FAILED_PRECONDITION
        assert _as_dict(stub.GetStatus(empty_pb2.Empty()))["state"] == "closed"
        assert _health_status(channel) == health_pb2.HealthCheckResponse.NOT_SERVING


def test_checkpoint_failure_exposes_metadata_and_health_recovers(tmp_path, monkeypatch) -> None:
    store = tmp_path / "engram.json"
    core = _core(store)
    real_save = service_module.persistence.save

    with _running_server(core) as (_, channel, stub):

        def fail_save(engram, path) -> None:
            raise OSError("disk unavailable")

        monkeypatch.setattr(service_module.persistence, "save", fail_save)
        request = engram_pb2.LearnResponseRequest(
            request="What is durable?",
            response="This answer should persist.",
            request_id="learn-degraded",
        )
        with pytest.raises(grpc.RpcError) as failed:
            stub.LearnResponse(request)

        metadata = dict(failed.value.trailing_metadata())
        assert failed.value.code() == grpc.StatusCode.UNAVAILABLE
        assert metadata["engram-error-type"] == "PersistenceError"
        assert metadata["engram-operation"] == "store checkpoint"
        assert metadata["engram-state-changed"] == "true"
        assert _as_dict(stub.GetStatus(empty_pb2.Empty()))["durability"] == "degraded"
        assert _health_status(channel) == health_pb2.HealthCheckResponse.NOT_SERVING

        monkeypatch.setattr(service_module.persistence, "save", real_save)
        recovered = _as_dict(stub.LearnResponse(request))

        assert recovered["idempotent"] is True
        assert _as_dict(stub.GetStatus(empty_pb2.Empty()))["durability"] == "healthy"
        assert _health_status(channel) == health_pb2.HealthCheckResponse.SERVING

    restored = EngramCore.open(store_path=store)
    assert restored.engram.get_statement(recovered["statement_id"])["text"] == "This answer should persist."
    restored.close()


def test_durable_state_survives_restart_but_proposals_do_not(tmp_path) -> None:
    store = tmp_path / "engram.json"
    first_core = _core(store)
    first_server = create_grpc_server(first_core, bind_address="127.0.0.1:0")
    first_channel = grpc.insecure_channel(first_server.start())
    first_stub = engram_pb2_grpc.EngramServiceStub(first_channel)
    learned = _as_dict(
        first_stub.LearnResponse(
            engram_pb2.LearnResponseRequest(
                request="What survives?",
                response="Durable knowledge survives.",
                request_id="learn-before-restart",
            )
        )
    )
    old_proposal = _as_dict(
        first_stub.Propose(engram_pb2.ProposeRequest(request="What survives?", request_id="proposal-before-restart"))
    )
    first_channel.close()
    first_server.stop(0)

    restored_core = EngramCore.open(store_path=store)
    with _running_server(restored_core) as (_, _, restored_stub):
        proposal = _as_dict(
            restored_stub.Propose(engram_pb2.ProposeRequest(request="What survives?", request_id="proposal-after-restart"))
        )
        assert proposal["candidates"][0]["statement_id"] == learned["statement_id"]

        with pytest.raises(grpc.RpcError) as expired:
            restored_stub.Resolve(
                engram_pb2.ResolveRequest(
                    proposal_id=old_proposal["proposal_id"],
                    outcome=engram_pb2.REGULATOR_OUTCOME_ACCEPTED,
                    statement_id=learned["statement_id"],
                )
            )
        assert expired.value.code() == grpc.StatusCode.NOT_FOUND


def test_deadline_does_not_claim_to_roll_back_started_core_work() -> None:
    core = _core()
    entered = threading.Event()
    release = threading.Event()
    original_chat = core.chat

    def delayed_chat(user_id: str, text: str) -> dict:
        entered.set()
        assert release.wait(timeout=5)
        return original_chat(user_id, text)

    core.chat = delayed_chat
    with _running_server(core) as (_, _, stub):
        stub.StartConversation(engram_pb2.StartConversationRequest(user_id="Alice"))
        with pytest.raises(grpc.RpcError) as deadline:
            stub.Chat(engram_pb2.ChatRequest(user_id="Alice", text="hello"), timeout=0.05)
        assert deadline.value.code() == grpc.StatusCode.DEADLINE_EXCEEDED
        assert entered.is_set()

        release.set()
        inspected = _as_dict(stub.InspectConversation(engram_pb2.UserRequest(user_id="Alice"), timeout=5))
        assert inspected["turn_count"] == 1
        assert inspected["latest_turn"]["response"] == "Hello!"


def test_graceful_shutdown_drains_an_in_flight_rpc() -> None:
    core = _core()
    entered = threading.Event()
    release = threading.Event()
    original_chat = core.chat

    def delayed_chat(user_id: str, text: str) -> dict:
        entered.set()
        assert release.wait(timeout=5)
        return original_chat(user_id, text)

    core.chat = delayed_chat
    server = create_grpc_server(core, bind_address="127.0.0.1:0")
    channel = grpc.insecure_channel(server.start())
    stub = engram_pb2_grpc.EngramServiceStub(channel)
    stub.StartConversation(engram_pb2.StartConversationRequest(user_id="Alice"))
    chat_future = stub.Chat.future(engram_pb2.ChatRequest(user_id="Alice", text="hello"), timeout=5)
    assert entered.wait(timeout=5)

    with ThreadPoolExecutor(max_workers=1) as executor:
        stop_future = executor.submit(server.stop, 2)
        assert stop_future.done() is False
        release.set()
        assert _as_dict(chat_future.result(timeout=5))["response"] == "Hello!"
        assert stop_future.result(timeout=5) is True

    channel.close()
    assert core.status()["state"] == "closed"
    assert server.stop(0) is False


def test_shutdown_can_retry_a_failed_final_checkpoint(tmp_path, monkeypatch) -> None:
    store = tmp_path / "engram.json"
    core = EngramCore(Engram(), store_path=store, checkpoint_on_mutation=False)
    core.add_fact("Pending state.")
    real_save = service_module.persistence.save
    server = create_grpc_server(core, bind_address="127.0.0.1:0")
    channel = grpc.insecure_channel(server.start())
    grpc.channel_ready_future(channel).result(timeout=5)

    def fail_save(engram, path) -> None:
        raise OSError("read only")

    monkeypatch.setattr(service_module.persistence, "save", fail_save)
    with pytest.raises(PersistenceError):
        server.stop(0)

    assert core.status()["state"] == "running"
    assert core.status()["durability"] == "degraded"

    monkeypatch.setattr(service_module.persistence, "save", real_save)
    assert server.stop(0) is True
    assert server.stop(0) is False
    restored = EngramCore.open(store_path=store)
    assert restored.engram.statements[0]["text"] == "Pending state."
    restored.close()
    channel.close()


def test_tls_requires_a_certificate_and_key_pair() -> None:
    core = _core()
    with pytest.raises(InvalidRequestError, match="together"):
        create_grpc_server(core, bind_address="127.0.0.1:0", tls_certificate=b"certificate")
    core.close()


def test_grpc_main_refuses_to_serve_after_component_preflight_failure(monkeypatch) -> None:
    def fail_open(cls, **kwargs):
        raise InvalidRequestError("component preflight failed: graph unavailable")

    monkeypatch.setattr(EngramCore, "open", classmethod(fail_open))

    assert grpc_server_module.main(["--log-level", "ERROR"]) == 1


def test_committed_generated_stubs_match_the_proto(tmp_path) -> None:
    repository = Path(__file__).resolve().parent.parent
    command = [
        sys.executable,
        "-m",
        "grpc_tools.protoc",
        f"-I{repository}",
        f"--python_out={tmp_path}",
        f"--pyi_out={tmp_path}",
        f"--grpc_python_out={tmp_path}",
        str(repository / "engram" / "v1" / "engram.proto"),
    ]
    subprocess.run(command, check=True, cwd=repository, capture_output=True, text=True)

    for filename in ("engram_pb2.py", "engram_pb2.pyi", "engram_pb2_grpc.py"):
        committed = repository / "engram" / "v1" / filename
        regenerated = tmp_path / "engram" / "v1" / filename
        assert regenerated.read_bytes() == committed.read_bytes(), f"regenerate {filename} from engram.proto"
