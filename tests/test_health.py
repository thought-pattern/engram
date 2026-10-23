"""Engram's HTTP health page and API: the routes, GetStatus carrying the same snapshot, figures that move with real
calls, no conversation content, and a port in use reported while Engram keeps serving."""

from contextlib import contextmanager
from errno import EADDRINUSE, errorcode
from http.client import HTTPConnection
from json import loads
from socket import socket

from google.protobuf import empty_pb2, json_format
from grpc import RpcError as grpc_RpcError, StatusCode as grpc_StatusCode, insecure_channel as grpc_insecure_channel
from pytest import raises as pytest_raises

from engram import engram_pb2
from engram.core import Engram
from engram.engram_pb2_grpc import EngramServiceStub
from engram.grpc_server import EngramGrpcServer
from engram.health import HealthPageServer
from engram.service import EngramCore

# Fields that change between two reads taken moments apart: the read's own time and the server's uptime, the call in
# progress (GetStatus itself) and the counts that include it.
MOVING_FIELDS = ("observed_at", "uptime_seconds", "requests", "timing", "active_work", "recent_outcomes", "history", "errors")


def http_settings(port: int) -> dict:
    settings = {"enabled": True, "host": "127.0.0.1", "port": port, "refresh_seconds": 5.0}
    return settings


def static_core() -> EngramCore:
    engram = Engram()
    engram.load_static_data([{"pattern": "HELLO", "response": "Hello!"}, {"pattern": "*", "response": "Go on."}])
    core = EngramCore(engram)
    return core


@contextmanager
def running(core: EngramCore, port: int):
    server = EngramGrpcServer(core, bind_address="127.0.0.1:0", http=http_settings(port))
    try:
        channel = grpc_insecure_channel(server.start())
        try:
            yield server, EngramServiceStub(channel)
        finally:
            channel.close()
    finally:
        server.stop(0)


def fetch(port: int, path: str) -> tuple:
    connection = HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        connection.request("GET", path)
        response = connection.getresponse()
        reply = (response.status, dict(response.getheaders()), response.read())
    finally:
        connection.close()
    return reply


def steady(snapshot: dict) -> dict:
    kept = {name: value for name, value in snapshot.items() if name not in MOVING_FIELDS}
    return kept


def test_page_serves_the_status_snapshot_and_counts_calls_without_conversation_content():
    with running(static_core(), 0) as (server, stub):
        listener = server.health_page.listener
        assert isinstance(listener, HealthPageServer)
        port = listener.server_port
        for path, marker in (("/", b"Engram health"), ("/health.js", b"Engram makes no model calls"), ("/health.css", b":root")):
            status, headers, body = fetch(port, path)
            assert status == 200 and marker in body
            assert headers.get("Cache-Control", "") == "no-store" and headers.get("X-Content-Type-Options", "") == "nosniff"
            assert "frame-ancestors 'none'" in headers.get("Content-Security-Policy", "")
        assert b"export function start" in fetch(port, "/health-common.js")[2]
        assert fetch(port, "/health/live")[0] == 200 and fetch(port, "/health/ready")[0] == 200
        assert fetch(port, "/missing")[0] == 404
        before = loads(fetch(port, "/api/health")[2])
        stub.StartConversation(engram_pb2.StartConversationRequest(user_id="Dorothea"))
        stub.Chat(engram_pb2.ChatRequest(user_id="Dorothea", text="my secret word is marmalade"))
        with pytest_raises(grpc_RpcError) as missing:
            stub.Chat(engram_pb2.ChatRequest(user_id="Bartholomew", text="hello"))
        assert missing.value.code() == grpc_StatusCode.NOT_FOUND
        status = json_format.MessageToDict(stub.GetStatus(empty_pb2.Empty()), preserving_proto_field_name=True)
        status_code, _, raw = fetch(port, "/api/health")
    after = loads(raw)
    assert status_code == 200 and after.get("ready", False) is True and after.get("component", "") == "Engram"
    described = status.get("health", {})
    assert steady(described) == steady(after)
    assert described.get("incarnation", "") == before.get("incarnation", "-") and after.get("active_conversations", 0) == 1
    by_call = after.get("requests", {}).get("by_operation", {})
    assert by_call.get("StartConversation", {}).get("outcomes", {}) == {"OK": 1}
    assert by_call.get("Chat", {}).get("outcomes", {}) == {"OK": 1, "NOT_FOUND": 1}
    assert by_call.get("GetStatus", {}).get("outcomes", {}) == {"OK": 1}
    assert after.get("requests", {}).get("received", 0) == before.get("requests", {}).get("received", 0) + 4
    assert after.get("errors", {}).get("by_kind", {}) == {"NOT_FOUND": 1}
    assert after.get("errors", {}).get("last", {}).get("operation", "") == "Chat"
    assert [item.get("operation", "") for item in after.get("recent_outcomes", [])][:2] == ["GetStatus", "Chat"]
    assert after.get("usage", {"calls": 1}) == {} and after.get("http", {}).get("available", False) is True
    for content in (b"Dorothea", b"Bartholomew", b"marmalade", b"Go on."):
        assert content not in raw


def test_a_port_in_use_is_reported_while_engram_keeps_serving():
    with socket() as occupant:
        occupant.bind(("127.0.0.1", 0))
        occupant.listen()
        port = occupant.getsockname()[1]
        with running(static_core(), port) as (server, stub):
            stub.StartConversation(engram_pb2.StartConversationRequest(user_id="Dorothea"))
            status = json_format.MessageToDict(stub.GetStatus(empty_pb2.Empty()), preserving_proto_field_name=True)
            assert server.health_page.listener is False
    http = status.get("health", {}).get("http", {})
    assert status.get("ready", False) is True and status.get("active_conversations", 0) == 1
    assert http.get("enabled", False) is True and http.get("available", True) is False
    assert http.get("error_type", "") == "OSError" and http.get("error_code", "") == errorcode.get(EADDRINUSE, "-")


def test_a_server_without_http_settings_serves_no_page_and_still_reports_its_figures():
    server = EngramGrpcServer(static_core(), bind_address="127.0.0.1:0")
    try:
        server.start()
        snapshot = server.health_page.snapshot()
    finally:
        server.stop(0)
    assert server.health_page.listener is False
    assert snapshot.get("http", {}).get("enabled", True) is False and snapshot.get("ready", False) is True
