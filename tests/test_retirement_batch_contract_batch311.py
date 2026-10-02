# Copyright 2025-2026 Jason E. Robinson.
# SPDX-License-Identifier: Apache-2.0

"""Native, MCP, and service requirements for ordered response retirement."""

from google.protobuf import json_format
from grpc import channel_ready_future, insecure_channel

from engram import engram_pb2, engram_pb2_grpc
from engram.core import Engram
from engram.grpc_server import EngramGrpcServer
from engram.mcp_server import MCPConversationService
from engram.service import EngramCore


def test_mcp_batch_uses_the_same_core_operation() -> None:
    service = MCPConversationService(static_pairs=[], require_catch_all=False)
    service.start(user_id="batch311")
    learned = service.learn_response("request", "answer", "batch311:mcp")
    statement_id = learned.get("statement_id", "")

    result = service.retire_responses(
        [{"statement_id": statement_id, "reason": "invalid_or_missing_support", "request_id": "batch311:mcp-retire"}]
    )

    assert result.get("results", [])[0].get("response", {}).get("retired", False) is True
    service.stop()


def test_grpc_batch_exposes_the_core_envelope_and_scalar_receipt() -> None:
    core = EngramCore(Engram())
    learned = core.learn_response("request", "answer", "batch311:grpc")
    statement_id = learned.get("statement_id", "")
    server = EngramGrpcServer(core, bind_address="127.0.0.1:0")
    channel = insecure_channel(server.start())
    channel_ready_future(channel).result(timeout=5)
    try:
        stub = engram_pb2_grpc.EngramServiceStub(channel)
        response = stub.RetireResponses(
            engram_pb2.RetireResponsesRequest(
                entries=[
                    engram_pb2.RetireResponseRequest(
                        statement_id=statement_id,
                        reason="invalid_or_missing_support",
                        request_id="batch311:grpc-retire",
                    )
                ]
            ),
            timeout=5,
        )
        result = json_format.MessageToDict(response, preserving_proto_field_name=True)
        assert result.get("results", [])[0].get("response", {}).get("retired", False) is True
        assert result.get("results", [])[0].get("statement_id", "") == statement_id
    finally:
        channel.close()
        server.stop(0)
