"""Graph connection timeouts and the after-turn reconnect, over a stub Bolt server."""

from datetime import UTC, datetime, timedelta
from threading import Thread
from time import monotonic as time_monotonic
from unittest.mock import patch

from pytest import fixture, raises as pytest_raises

from engram.config import engram_config, graph_config
from engram.constants import GRAPH_TIMEOUT_SECONDS
from engram.core import Engram
from engram.graph import MemGraphConnection
from engram.service import EngramCore

from .bolt_stub import BoltStub, Structure

# Room for thread wake-ups; Windows timers tick about every 16 ms.
SLACK_SECONDS = 0.25
FAIL_FAST_SECONDS = 0.05


@fixture
def stub():
    server = BoltStub()
    yield server
    server.close()


@fixture
def client(stub):
    connection = MemGraphConnection(host="127.0.0.1", port=stub.port)
    assert connection.connect()
    yield connection
    connection.disconnect()


def time_out_one_query(stub: BoltStub, client: MemGraphConnection) -> None:
    stub.mode = "hang"
    with pytest_raises(RuntimeError, match=r"Query failed \(TimeoutError\)"):
        client.execute("RETURN 1 AS ready")


def test_rows_are_dicts_and_carry_visibility_parameters(stub, client) -> None:
    stub.rows = [{"subject": "Athens", "count": 2, "score": 0.5, "labels": ["City"]}]

    rows = client.execute("MATCH (n) RETURN n.subject AS subject", {"limit": 3})

    assert rows == [{"subject": "Athens", "count": 2, "score": 0.5, "labels": ["City"]}]
    query, params = stub.queries[-1]
    assert query == "MATCH (n) RETURN n.subject AS subject"
    assert params["limit"] == 3
    assert params["visibility_kind"] == "global"


def test_driver_datetimes_come_back_as_python_datetimes(stub, client) -> None:
    expected = datetime(2026, 8, 6, 12, 30, tzinfo=UTC)
    # Bolt 5 DateTime: UTC epoch seconds, nanoseconds, offset seconds.
    stub.rows = [{"recorded_at": Structure(0x49, int(expected.timestamp()), 0, 0)}]

    value = client.execute("RETURN 1 AS recorded_at")[0]["recorded_at"]

    assert isinstance(value, datetime)
    assert value == expected
    assert value.utcoffset() == timedelta(0)


def test_query_times_out_and_later_reads_fail_fast_until_the_after_turn_reconnect(stub, client) -> None:
    started = time_monotonic()
    time_out_one_query(stub, client)
    elapsed = time_monotonic() - started
    assert GRAPH_TIMEOUT_SECONDS - 0.02 <= elapsed < GRAPH_TIMEOUT_SECONDS + SLACK_SECONDS
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
    time_out_one_query(stub, client)

    stub.mode = "silent"
    assert client.reconnect_after_turn() is True
    assert client.reconnect_future.result(timeout=5) is False
    assert client.available is False
    assert client.reconnect_needed is True

    stub.mode = "answer"
    assert client.reconnect_after_turn() is True
    assert client.reconnect_future.result(timeout=5) is True
    assert client.execute("RETURN 1 AS ready") == [{"ready": 1}]


def test_connect_gives_up_at_the_timeout_on_an_unresponsive_server(stub) -> None:
    stub.mode = "silent"
    connection = MemGraphConnection(host="127.0.0.1", port=stub.port)
    try:
        started = time_monotonic()
        assert connection.connect() == ()
        assert time_monotonic() - started < GRAPH_TIMEOUT_SECONDS + SLACK_SECONDS
        assert connection.available is False
        with pytest_raises(RuntimeError, match="unavailable"):
            connection.execute("RETURN 1")
    finally:
        connection.disconnect()


def test_disconnect_stops_the_driver_loop(stub) -> None:
    connection = MemGraphConnection(host="127.0.0.1", port=stub.port)
    assert connection.connect()
    thread = connection.loop_thread
    assert isinstance(thread, Thread)

    connection.disconnect()

    assert not thread.is_alive()
    assert connection.driver == ()
    assert connection.reconnect_after_turn() is False


def test_admin_statements_may_write_and_raise_the_driver_error(stub, client) -> None:
    stub.rows = []
    assert client.execute_admin("CREATE INDEX ON :Entity(id)") == []
    assert stub.queries[-1][0] == "CREATE INDEX ON :Entity(id)"

    stub.mode = "fail"
    stub.failure_message = "index already exists"
    with pytest_raises(Exception, match="index already exists"):
        client.execute_admin("CREATE INDEX ON :Entity(id)")


def test_timeout_must_be_positive() -> None:
    with pytest_raises(ValueError, match="positive"):
        MemGraphConnection(timeout_seconds=0)


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

    def __init__(self, engram: Engram) -> None:
        self.active_turns_at_reconnect: list[int] = []
        super().__init__(engram)

    def reconnect_graph_after_turn(self) -> None:
        self.active_turns_at_reconnect.append(len(self.active_resolution_request_ids))
        super().reconnect_graph_after_turn()


def test_the_core_asks_the_graph_to_reconnect_once_a_turn_is_over() -> None:
    graph = ReconnectCountingGraph()
    with patch("engram.core.connect_graph", return_value=graph):
        engram = Engram(config=engram_config(graph=graph_config(enabled=True, deployment_mode="tapestry_managed")))
    core = TurnRecordingCore(engram)
    try:
        core.start_conversation(user_id="user-1")
        assert graph.reconnect_requests == 0

        core.chat("user-1", "Hello there")

        assert core.active_turns_at_reconnect == [0]
        assert graph.reconnect_requests == 1
    finally:
        core.close()
