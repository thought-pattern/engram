# Copyright 2025-2026 Jason E. Robinson.
# SPDX-License-Identifier: Apache-2.0

"""Support lookup that lets Tapestry retire responses whose graph support changed."""

from google.protobuf import json_format
from grpc import channel_ready_future, insecure_channel

from engram import engram_pb2, engram_pb2_grpc
from engram.core import Engram
from engram.grpc_server import EngramGrpcServer
from engram.service import EngramCore

PROPOSITION_ID = "prp_" + "a" * 64
OTHER_PROPOSITION_ID = "prp_" + "b" * 64


def support_reference(identifier: str) -> dict:
    return {
        "schema_version": "tapestry-engram-support",
        "record_kind": "proposition",
        "id": identifier,
        "state_revision": 1,
        "support_revision": 1,
        "representation_contract": "tapestry-ke-representation",
        "visibility_scope": {"kind": "global", "company_id": {}, "customer_id": {}, "engagement_id": {}},
        "dependency_state_digest": "dep_" + "c" * 64,
    }


def learned(core: EngramCore, request_id: str, identifier: str) -> str:
    result = core.learn_response(
        f"request {request_id}", "answer", request_id, metadata={"support": [support_reference(identifier)]}
    )
    return result.get("statement_id", "")


def test_lookup_returns_only_active_responses_naming_the_changed_record() -> None:
    core = EngramCore(Engram())
    named = learned(core, "support:named", PROPOSITION_ID)
    learned(core, "support:other", OTHER_PROPOSITION_ID)

    assert core.responses_by_support([PROPOSITION_ID]) == {"statement_ids": [named]}

    core.retire_response(named, "graph support changed", "support:retire")
    assert core.responses_by_support([PROPOSITION_ID]) == {"statement_ids": []}


def test_grpc_lookup_exposes_the_core_result() -> None:
    core = EngramCore(Engram())
    named = learned(core, "support:grpc", PROPOSITION_ID)
    server = EngramGrpcServer(core, bind_address="127.0.0.1:0")
    channel = insecure_channel(server.start())
    channel_ready_future(channel).result(timeout=5)
    try:
        stub = engram_pb2_grpc.EngramServiceStub(channel)
        response = stub.ResponsesBySupport(engram_pb2.ResponsesBySupportRequest(record_ids=[PROPOSITION_ID]), timeout=5)
        result = json_format.MessageToDict(response, preserving_proto_field_name=True)
        assert result.get("statement_ids", []) == [named]
    finally:
        channel.close()
        server.stop(0)
