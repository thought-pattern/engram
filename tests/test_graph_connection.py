"""Graph connection loss and the after-turn reconnect, over a stub Bolt server."""

from datetime import UTC, datetime, timedelta
from threading import Thread
from time import monotonic as time_monotonic
from unittest.mock import patch

from pytest import fixture, raises as pytest_raises

from engram.config import engram_config, graph_config
from engram.constants import EARLIEST_UTC, EMPTY_CONFIG
from engram.core import Engram
from engram.graph import MemGraphConnection
from engram.service import EngramCore
from tests.bolt_stub import BoltStub, Structure

FAIL_FAST_SECONDS = 0.05


@fixture(name="stub")
def stub_fixture():
    server = BoltStub()
    yield server
    server.close()


@fixture(name="client")
def client_fixture(stub):
    connection = MemGraphConnection(host="127.0.0.1", port=stub.port)
    assert connection.connect()
    yield connection
    connection.disconnect()


def lose_one_query(stub: BoltStub, client: MemGraphConnection) -> None:
    stub.mode = "drop"
    with pytest_raises(RuntimeError, match=r"Query failed \((ServiceUnavailable|SessionExpired)\)"):
        client.execute("RETURN 1 AS ready")


def test_rows_are_dicts_and_carry_only_caller_parameters(stub, client) -> None:
    stub.rows = [{"subject": "Athens", "count": 2, "score": 0.5, "labels": ["City"]}]

    rows = client.execute("MATCH (n) RETURN n.subject AS subject", {"limit": 3})

    assert rows == [{"subject": "Athens", "count": 2, "score": 0.5, "labels": ["City"]}]
    query, params = stub.queries[-1]
    assert query == "MATCH (n) RETURN n.subject AS subject"
    assert params == {"limit": 3}


def test_driver_datetimes_come_back_as_python_datetimes(stub, client) -> None:
    expected = datetime(2026, 8, 6, 12, 30, tzinfo=UTC)
    # Bolt 5 DateTime: UTC epoch seconds, nanoseconds, offset seconds.
    stub.rows = [{"recorded_at": Structure(0x49, int(expected.timestamp()), 0, 0)}]

    value = client.execute("RETURN 1 AS recorded_at")[0].get("recorded_at", EARLIEST_UTC)

    assert isinstance(value, datetime)
    assert value == expected
    assert value.utcoffset() == timedelta(0)


def test_a_lost_connection_makes_later_reads_fail_fast_until_the_after_turn_reconnect(stub, client) -> None:
    lose_one_query(stub, client)
    assert client.available is False

    # The rest of the turn does not touch the database.
    queries_sent = len(stub.queries)
    started = time_monotonic()
    with pytest_raises(RuntimeError, match="unavailable"):
        client.execute("RETURN 1 AS ready")
    assert time_monotonic() - started < FAIL_FAST_SECONDS
    assert len(stub.queries) == queries_sent

    stub.mode = "answer"
    assert client.reconnect_after_turn() is True
    assert client.reconnect_future.result(timeout=5) is True
    assert client.available is True
    assert client.execute("RETURN 1 AS ready") == [{"ready": 1}]
    assert client.reconnect_after_turn() is False


def test_a_failed_reconnect_is_retried_after_the_next_turn(stub, client) -> None:
    lose_one_query(stub, client)

    assert client.reconnect_after_turn() is True
    assert client.reconnect_future.result(timeout=5) is False
    assert client.available is False
    assert client.reconnect_needed is True

    stub.mode = "answer"
    assert client.reconnect_after_turn() is True
    assert client.reconnect_future.result(timeout=5) is True
    assert client.execute("RETURN 1 AS ready") == [{"ready": 1}]


def test_disconnect_stops_the_driver_loop(stub) -> None:
    connection = MemGraphConnection(host="127.0.0.1", port=stub.port)
    assert connection.connect()
    thread = connection.loop_thread
    assert isinstance(thread, Thread)

    connection.disconnect()

    assert not thread.is_alive()
    assert connection.driver == ()
    assert connection.reconnect_after_turn() is False


class ReconnectCountingGraph:
    """Graph stand-in that counts reconnect requests."""

    def __init__(self) -> None:
        self.available = True
        self.reconnect_requests = 0

    def execute(self, query: str, params=()) -> list:
        del query, params
        return []

    def reconnect_after_turn(self) -> bool:
        self.reconnect_requests += 1
        return False


class TurnRecordingCore(EngramCore):
    """Core that records how many turns were still active when it asked for a reconnect."""

    def __init__(self, engram=(), *, config: dict = EMPTY_CONFIG, clock: object = ()) -> None:
        self.active_turns_at_reconnect: list[int] = []
        super().__init__(engram, config=config, clock=clock)

    def reconnect_graph_after_turn(self) -> None:
        self.active_turns_at_reconnect.append(len(self.active_resolution_request_ids))
        super().reconnect_graph_after_turn()


def test_the_core_asks_the_graph_to_reconnect_once_a_turn_is_over() -> None:
    graph = ReconnectCountingGraph()
    with patch("engram.core.connect_graph", return_value=graph):
        engram = Engram(config=engram_config(graph=graph_config(enabled=True)))
    core = TurnRecordingCore(engram)
    try:
        core.start_conversation(user_id="user-1")
        assert graph.reconnect_requests == 0

        core.chat("user-1", "Hello there")

        assert core.active_turns_at_reconnect == [0]
        assert graph.reconnect_requests == 1
    finally:
        core.close()
