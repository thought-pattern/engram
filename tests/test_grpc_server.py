"""Network-level contract tests for the single-instance gRPC adapter."""

import logging
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path

import grpc
import pytest
from google.protobuf import empty_pb2, json_format, struct_pb2
from grpc_health.v1 import health_pb2, health_pb2_grpc

from engram import grpc_server as grpc_server_module, service as service_module
from engram.constants import MAX_REQUEST_BYTES, Tier
from engram.core import Engram
from engram.errors import InvalidRequestError, PersistenceError
from engram.grpc_server import SERVICE_NAME, create_grpc_server
from engram.identity import build_standalone_identity, query_identity_to_dict
from engram.mcp_server import MCPConversationService
from engram.resolution import resolution_budget, resolution_budget_to_dict
from engram.service import EngramCore, open_engram_core
from engram.v1 import engram_pb2, engram_pb2_grpc
from engram.v2 import engram_pb2 as evidence_pb2, engram_pb2_grpc as evidence_pb2_grpc


def _core(store_path="") -> EngramCore:
    engram = Engram()
    engram.store("Hello!", pattern="HELLO", tier=Tier.STATIC)
    engram.store("Go on.", pattern="*", tier=Tier.STATIC)
    result = EngramCore(engram, store_path=store_path)
    return result


def _as_dict(message: struct_pb2.Struct) -> dict:
    result = json_format.MessageToDict(message, preserving_proto_field_name=True)
    return result


def _as_struct(value: dict) -> struct_pb2.Struct:
    result = struct_pb2.Struct()
    json_format.ParseDict(value, result)
    return result


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
    result = health_stub.Check(health_pb2.HealthCheckRequest(service=SERVICE_NAME), timeout=5).status
    return result


def _trailing_metadata(error: grpc.RpcError) -> dict[str, str]:
    metadata = error.trailing_metadata()
    result = dict(metadata)
    return result


def test_section7_keeps_current_grpc_v1_as_proposal_resolution_only() -> None:
    service = engram_pb2.DESCRIPTOR.services_by_name["EngramService"]
    resolve = service.methods_by_name["Resolve"]

    assert tuple(method.name for method in service.methods) == (
        "StartConversation",
        "Chat",
        "InspectConversation",
        "FinishConversation",
        "StopConversation",
        "AddFact",
        "SetPredicate",
        "GetPredicate",
        "Propose",
        "Resolve",
        "LearnResponse",
        "RetireResponse",
        "GetStatus",
        "Flush",
    )
    assert resolve.input_type.full_name == "engram.v1.ResolveRequest"
    assert resolve.output_type.full_name == "google.protobuf.Struct"
    assert tuple((field.name, field.number) for field in resolve.input_type.fields) == (
        ("proposal_id", 1),
        ("outcome", 2),
        ("statement_id", 3),
        ("reason", 4),
    )
    assert not {
        "ClaimEvidenceRecord",
        "EvidencePackage",
        "ResolutionResult",
    }.intersection(engram_pb2.DESCRIPTOR.message_types_by_name)


def test_section15_exposes_unified_resolution_only_through_the_v2_evidence_service() -> None:
    service = evidence_pb2.DESCRIPTOR.services_by_name["EngramEvidenceService"]
    resolve = service.methods_by_name["ResolveEvidence"]

    assert tuple(method.name for method in service.methods) == ("ResolveEvidence",)
    assert resolve.input_type.full_name == "engram.v2.ResolveEvidenceRequest"
    assert resolve.output_type.full_name == "engram.v2.ResolutionResult"
    assert tuple((field.name, field.number) for field in resolve.input_type.fields) == (
        ("request", 1),
        ("request_id", 2),
        ("user_id", 3),
        ("namespace", 4),
        ("context_fingerprint", 5),
        ("identity", 6),
        ("required_metadata", 7),
        ("required_source_label", 8),
        ("budget", 9),
        ("configured_resolvers", 10),
        ("accept_exact", 11),
    )
    assert tuple((field.name, field.number) for field in resolve.output_type.fields) == (
        ("schema_version", 1),
        ("outcome", 2),
        ("selected_candidate", 3),
        ("selected_candidate_available", 4),
        ("response_candidates", 5),
        ("evidence", 6),
        ("confidence", 7),
        ("confidence_available", 8),
        ("reason_codes", 9),
        ("frame_diagnostics", 10),
        ("resolver_results", 11),
        ("budget", 12),
        ("evidence_package_available", 13),
        ("evidence_package", 14),
    )


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
        assert _trailing_metadata(invalid.value)["engram-error-type"] == "InvalidRequestError"

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


def test_v2_evidence_service_delegates_unified_resolution_to_the_shared_core() -> None:
    core = _core()
    with _running_server(core) as (_, channel, v1_stub):
        v1_stub.LearnResponse(
            engram_pb2.LearnResponseRequest(
                request="What is served through v2?",
                response="The transport-neutral result.",
                request_id="learn-v2-evidence",
            )
        )
        stub = evidence_pb2_grpc.EngramEvidenceServiceStub(channel)

        result = stub.ResolveEvidence(
            evidence_pb2.ResolveEvidenceRequest(
                request="What is served through v2?",
                request_id="resolve-v2-evidence",
                user_id="Alice",
                configured_resolvers=("exact",),
                accept_exact=True,
            )
        )

        assert result.schema_version == 1
        assert result.outcome == "ANSWER"
        assert result.selected_candidate_available is True
        assert _as_dict(result.selected_candidate)["response"] == "The transport-neutral result."
        assert len(result.response_candidates) == 1
        assert result.evidence_package_available is False
        assert result.evidence_package.wire_version == 2
        assert result.evidence_package.retained_count == 0
        assert result.evidence_package.records == []


def test_v2_evidence_service_decodes_json_facing_identity_and_budget_contracts() -> None:
    core = _core()
    identity = build_standalone_identity("Uncached v2 contract request")
    budget = resolution_budget()
    with _running_server(core) as (_, channel, _):
        stub = evidence_pb2_grpc.EngramEvidenceServiceStub(channel)

        result = stub.ResolveEvidence(
            evidence_pb2.ResolveEvidenceRequest(
                request="Uncached v2 contract request",
                request_id="resolve-v2-contracts",
                identity=_as_struct(query_identity_to_dict(identity)),
                budget=_as_struct(resolution_budget_to_dict(budget)),
                configured_resolvers=("exact",),
            )
        )

        assert result.outcome == "MISS"


def test_v2_evidence_service_enforces_the_shared_request_bound() -> None:
    core = _core()
    with _running_server(core) as (_, channel, _):
        stub = evidence_pb2_grpc.EngramEvidenceServiceStub(channel)

        with pytest.raises(grpc.RpcError) as failure:
            stub.ResolveEvidence(
                evidence_pb2.ResolveEvidenceRequest(
                    request="x" * (MAX_REQUEST_BYTES + 1),
                    request_id="oversized-v2-request",
                )
            )

        assert failure.value.code() == grpc.StatusCode.INVALID_ARGUMENT
        assert _trailing_metadata(failure.value)["engram-error-type"] == "InvalidRequestError"
        assert core._resolution_requests == {}


def test_unhandled_grpc_failure_redacts_exception_content(caplog, monkeypatch) -> None:
    secret = "private-request-and-credential-content"
    core = _core()

    def fail(_text, source_label=""):
        raise RuntimeError(secret)

    monkeypatch.setattr(core, "add_fact", fail)
    with (
        caplog.at_level(logging.ERROR, logger="engram.grpc_server"),
        _running_server(core) as (_, _, stub),
        pytest.raises(grpc.RpcError) as failure,
    ):
        stub.AddFact(engram_pb2.AddFactRequest(text="trigger failure"))

    assert failure.value.code() == grpc.StatusCode.INTERNAL
    assert failure.value.details() == "internal Engram failure"
    assert secret not in caplog.text
    assert "RuntimeError" in caplog.text


def test_exact_and_conflicting_mutation_retries_are_shared_across_python_mcp_and_grpc() -> None:
    mcp = MCPConversationService()
    mcp.start(seed_path="")
    core = mcp.core
    created = core.learn_response("What is shared?", "One shared result.", "cross-adapter-learn")
    mcp_replay = mcp.learn_response("What is shared?", "One shared result.", "cross-adapter-learn")

    with _running_server(core) as (_, _, stub):
        grpc_replay = _as_dict(
            stub.LearnResponse(
                engram_pb2.LearnResponseRequest(
                    request="What is shared?",
                    response="One shared result.",
                    request_id="cross-adapter-learn",
                )
            )
        )
        with pytest.raises(grpc.RpcError) as conflicting:
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
        assert conflicting.value.code() == grpc.StatusCode.ABORTED
        assert len(core.engram.response_repository.snapshot()["artifacts"]) == 1


def test_concurrent_proposal_resolution_has_one_result_and_consistent_cross_adapter_visibility() -> None:
    mcp = MCPConversationService()
    mcp.start(seed_path="")
    core = mcp.core
    learned = core.learn_response("What is concurrent?", "One accepted result.", "concurrent-learn")
    proposal = core.propose("What is concurrent?", "concurrent-proposal")

    with _running_server(core) as (_, _, stub):
        barrier = threading.Barrier(7)

        def python_resolve() -> dict:
            barrier.wait()
            return core.resolve(proposal["proposal_id"], "accepted", learned["statement_id"])

        def mcp_resolve() -> dict:
            barrier.wait()
            return mcp.resolve(proposal["proposal_id"], "accepted", learned["statement_id"])

        def grpc_resolve() -> dict:
            barrier.wait()
            result = stub.Resolve(
                engram_pb2.ResolveRequest(
                    proposal_id=proposal["proposal_id"],
                    outcome=engram_pb2.REGULATOR_OUTCOME_ACCEPTED,
                    statement_id=learned["statement_id"],
                )
            )
            return _as_dict(result)

        operations = (python_resolve, mcp_resolve, grpc_resolve, python_resolve, mcp_resolve, grpc_resolve)
        with ThreadPoolExecutor(max_workers=6) as executor:
            futures = [executor.submit(operation) for operation in operations]
            barrier.wait()
            results = [future.result(timeout=10) for future in futures]

        python_view = core.inspect_conversation("0")["session"]
        mcp_view = mcp.inspect()["session"]
        grpc_view = _as_dict(stub.InspectConversation(engram_pb2.UserRequest(user_id="0")))["session"]

        assert sum(result["idempotent"] is False for result in results) == 1
        assert sum(result["idempotent"] is True for result in results) == 5
        assert core.engram.get_statement(learned["statement_id"])["hit_count"] == 1
        assert python_view["previous_response"] == mcp_view["previous_response"] == grpc_view["previous_response"]
        assert core.engram.response_repository.check()["consistent"] is True


@pytest.mark.parametrize("mode", ["cancel", "deadline"])
def test_v2_resolution_propagates_cancellation_without_caching_partial_work(monkeypatch, mode) -> None:
    core = _core()
    entered = threading.Event()
    release = threading.Event()
    completed = threading.Event()
    original = core._resolution_orchestrator._resolve_with_plan

    def delayed(frame, request_id, plan, accept_exact=False, cooperative_check=()):
        del frame, request_id, plan, accept_exact
        entered.set()
        assert release.wait(timeout=5)
        try:
            for _ in range(500):
                cooperative_check()
                time.sleep(0.01)
            raise AssertionError("transport cancellation did not reach the cooperative check")
        finally:
            completed.set()

    monkeypatch.setattr(core._resolution_orchestrator, "_resolve_with_plan", delayed)
    request = evidence_pb2.ResolveEvidenceRequest(
        request="Uncached cancellation request",
        request_id=f"v2-{mode}",
        configured_resolvers=("exact",),
    )
    with _running_server(core) as (_, channel, _):
        stub = evidence_pb2_grpc.EngramEvidenceServiceStub(channel)
        call = stub.ResolveEvidence.future(request, timeout=5 if mode == "cancel" else 0.05)
        assert entered.wait(timeout=5)
        if mode == "cancel":
            assert call.cancel() is True
            with pytest.raises(grpc.FutureCancelledError):
                call.result(timeout=5)
            assert call.code() == grpc.StatusCode.CANCELLED
        else:
            with pytest.raises(grpc.RpcError) as stopped:
                call.result(timeout=5)
            assert stopped.value.code() == grpc.StatusCode.DEADLINE_EXCEEDED
        release.set()
        assert completed.wait(timeout=5)
        assert request.request_id not in core._resolution_requests

        monkeypatch.setattr(core._resolution_orchestrator, "_resolve_with_plan", original)
        retry = stub.ResolveEvidence(request, timeout=5)
        assert retry.outcome == "MISS"


def test_checkpoint_failure_exposes_metadata_and_health_recovers(tmp_path, monkeypatch) -> None:
    store = tmp_path / "engram.json"
    core = _core(store)
    real_save = service_module.persistence.save_response_state

    with _running_server(core) as (_, channel, stub):

        def fail_save(engram, state, path) -> None:
            raise OSError("disk unavailable")

        monkeypatch.setattr(service_module.persistence, "save_response_state", fail_save)
        request = engram_pb2.LearnResponseRequest(
            request="What is durable?",
            response="This answer should persist.",
            request_id="learn-degraded",
        )
        with pytest.raises(grpc.RpcError) as failed:
            stub.LearnResponse(request)

        metadata = _trailing_metadata(failed.value)
        details = failed.value.details()
        assert failed.value.code() == grpc.StatusCode.UNAVAILABLE
        assert details == "Engram persistence failure"
        assert "disk unavailable" not in details
        assert metadata["engram-error-type"] == "PersistenceError"
        assert metadata["engram-operation"] == "store checkpoint"
        assert metadata["engram-state-changed"] == "false"
        assert _as_dict(stub.GetStatus(empty_pb2.Empty()))["durability"] == "degraded"
        assert _health_status(channel) == health_pb2.HealthCheckResponse.NOT_SERVING

        monkeypatch.setattr(service_module.persistence, "save_response_state", real_save)
        recovered = _as_dict(stub.LearnResponse(request))

        assert recovered["idempotent"] is False
        assert _as_dict(stub.GetStatus(empty_pb2.Empty()))["durability"] == "healthy"
        assert _health_status(channel) == health_pb2.HealthCheckResponse.SERVING

    restored = open_engram_core(store_path=store)
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

    restored_core = open_engram_core(store_path=store)
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
        result = original_chat(user_id, text)
        return result

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
        result = original_chat(user_id, text)
        return result

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
    restored = open_engram_core(store_path=store)
    assert restored.engram.statements[0]["text"] == "Pending state."
    restored.close()
    channel.close()


def test_tls_requires_a_certificate_and_key_pair() -> None:
    core = _core()
    with pytest.raises(InvalidRequestError, match="together"):
        create_grpc_server(core, bind_address="127.0.0.1:0", tls_certificate=b"certificate")
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
    monkeypatch.setattr(sys, "argv", ["engram-grpc", "--store-path", "state.json", "--tls-cert", "server.pem"])

    assert grpc_server_module.console_main() == 7
    assert observed == ["--store-path", "state.json", "--tls-cert", "server.pem"]


def test_committed_generated_stubs_match_the_proto(tmp_path) -> None:
    repository = Path(__file__).resolve().parent.parent
    for version in ("v1", "v2"):
        command = [
            sys.executable,
            "-m",
            "grpc_tools.protoc",
            f"-I{repository}",
            f"--python_out={tmp_path}",
            f"--pyi_out={tmp_path}",
            f"--grpc_python_out={tmp_path}",
            str(repository / "engram" / version / "engram.proto"),
        ]
        subprocess.run(command, check=True, cwd=repository, capture_output=True, text=True)

        for filename in ("engram_pb2.py", "engram_pb2.pyi", "engram_pb2_grpc.py"):
            committed = repository / "engram" / version / filename
            regenerated = tmp_path / "engram" / version / filename
            assert regenerated.read_bytes() == committed.read_bytes(), f"regenerate {version}/{filename} from engram.proto"
