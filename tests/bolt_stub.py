"""In-process Bolt 5.2 server for graph connection tests.

It speaks enough of the protocol for the neo4j driver: the handshake,
HELLO/LOGON, auto-commit RUN + PULL, RESET, and GOODBYE. ``mode`` decides how
the next handshake or query is answered:

- ``answer``: reply with ``rows`` (dicts sharing one ordered set of keys)
- ``hang``: never reply to the query
- ``fail``: reply with a FAILURE carrying ``failure_message``
- ``silent``: accept connections but never answer the handshake

Received queries and their parameters are kept in ``queries``.
"""

from contextlib import suppress as contextlib_suppress
from socket import (
    SHUT_RDWR,
    create_connection as socket_create_connection,
    create_server as socket_create_server,
    socket as socket_socket,
)
from struct import calcsize as struct_calcsize, pack as struct_pack, unpack as struct_unpack
from threading import Lock as threading_Lock, Thread as threading_Thread

# How long close waits for a worker after shutting down the socket it blocks on.
# Shutdown wakes the blocking call at once, so reaching this bound means a worker is stranded.
WORKER_STOP_SECONDS = 5.0

BOLT_5_2 = b"\x00\x00\x02\x05"
HELLO = 0x01
GOODBYE = 0x02
RESET = 0x0F
RUN = 0x10
DISCARD = 0x2F
PULL = 0x3F
SUCCESS = 0x70
RECORD = 0x71
IGNORED = 0x7E
FAILURE = 0x7F

# PackStream markers with a fixed-width struct payload.
PACKSTREAM_FIXED_FORMS = {0xC8: ">b", 0xC9: ">h", 0xCA: ">i", 0xCB: ">q", 0xC1: ">d"}
# PackStream markers whose size follows in a struct field: marker -> (tiny-marker kind, size form).
PACKSTREAM_SIZED_FORMS = {
    0xD0: (0x80, ">B"),
    0xD1: (0x80, ">H"),
    0xD2: (0x80, ">I"),
    0xD4: (0x90, ">B"),
    0xD5: (0x90, ">H"),
    0xD6: (0x90, ">I"),
    0xD8: (0xA0, ">B"),
    0xD9: (0xA0, ">H"),
    0xDA: (0xA0, ">I"),
}


class Structure:
    """A PackStream structure, such as a Bolt message or a temporal value.

    PackStream gives structures their own wire kind, distinct from lists and maps, so the codec needs a distinct
    Python type to tell a structure apart from a dictionary map when it encodes a value.
    """

    def __init__(self, tag: int, *fields) -> None:
        self.tag = tag
        self.fields = fields


def pack(value) -> bytes:
    """Encode one value as PackStream; Python None is the PackStream null the Bolt peer sends and expects."""
    if value is None:
        encoded = b"\xc0"
    elif value is True:
        encoded = b"\xc3"
    elif value is False:
        encoded = b"\xc2"
    elif isinstance(value, int):
        encoded = struct_pack(">b", value) if -16 <= value < 128 else b"\xcb" + struct_pack(">q", value)
    elif isinstance(value, float):
        encoded = b"\xc1" + struct_pack(">d", value)
    elif isinstance(value, str):
        data = value.encode("utf-8")
        header = bytes([0x80 + len(data)]) if len(data) < 16 else b"\xd1" + struct_pack(">H", len(data))
        encoded = header + data
    elif isinstance(value, (list, tuple)):
        header = bytes([0x90 + len(value)]) if len(value) < 16 else b"\xd5" + struct_pack(">H", len(value))
        encoded = header + b"".join(pack(item) for item in value)
    elif isinstance(value, dict):
        header = bytes([0xA0 + len(value)]) if len(value) < 16 else b"\xd9" + struct_pack(">H", len(value))
        encoded = header + b"".join(pack(key) + pack(item) for key, item in value.items())
    elif isinstance(value, Structure):
        encoded = bytes([0xB0 + len(value.fields), value.tag]) + b"".join(pack(item) for item in value.fields)
    else:
        raise TypeError(f"cannot pack {type(value).__name__}")
    return encoded


def unpack(data: bytes, offset: int = 0) -> tuple:
    """Decode one PackStream value; return it and the next offset.

    The PackStream null decodes to Python None: this stub stands in for the external Bolt peer, so it records the
    driver's parameters exactly as they arrive on the wire.
    """
    marker = data[offset]
    offset += 1
    high = marker & 0xF0
    fixed_form = PACKSTREAM_FIXED_FORMS.get(marker, "")
    sized_kind, sized_form = PACKSTREAM_SIZED_FORMS.get(marker, (0, ""))
    if marker < 0x80:
        decoded = (marker, offset)
    elif marker >= 0xF0:
        decoded = (marker - 0x100, offset)
    elif high in (0x80, 0x90, 0xA0, 0xB0):
        decoded = unpack_sized(high, marker & 0x0F, data, offset)
    elif fixed_form:
        width = struct_calcsize(fixed_form)
        decoded = (struct_unpack(fixed_form, data[offset : offset + width])[0], offset + width)
    elif marker == 0xC0:
        decoded = (None, offset)
    elif marker in (0xC2, 0xC3):
        decoded = (marker == 0xC3, offset)
    elif sized_form:
        width = struct_calcsize(sized_form)
        size = struct_unpack(sized_form, data[offset : offset + width])[0]
        decoded = unpack_sized(sized_kind, size, data, offset + width)
    else:
        raise ValueError(f"unsupported PackStream marker {marker:#x}")
    return decoded


def unpack_sized(kind: int, size: int, data: bytes, offset: int) -> tuple:
    """Decode a string, list, map or structure body of ``size`` entries; return it and the next offset."""
    if kind == 0x80:
        value = data[offset : offset + size].decode("utf-8")
        offset += size
    elif kind == 0x90:
        value = []
        for _ in range(size):
            item, offset = unpack(data, offset)
            value.append(item)
    elif kind == 0xA0:
        value = {}
        for _ in range(size):
            key, offset = unpack(data, offset)
            value[key], offset = unpack(data, offset)
    else:
        tag = data[offset]
        offset += 1
        fields = []
        for _ in range(size):
            item, offset = unpack(data, offset)
            fields.append(item)
        value = Structure(tag, *fields)
    decoded = (value, offset)
    return decoded


def read_exact(connection: socket_socket, size: int) -> bytes:
    data = b""
    while len(data) < size:
        chunk = connection.recv(size - len(data))
        if not chunk:
            raise EOFError
        data += chunk
    return data


def read_message(connection: socket_socket) -> Structure:
    body = b""
    while True:
        size = int.from_bytes(read_exact(connection, 2), "big")
        if size == 0:
            if body:
                message, _ = unpack(body)
                if not isinstance(message, Structure):
                    raise ValueError("a Bolt message must be a structure")
                return message
            continue
        body += read_exact(connection, size)


def send(connection: socket_socket, tag: int, *fields) -> None:
    body = pack(Structure(tag, *fields))
    connection.sendall(struct_pack(">H", len(body)) + body + b"\x00\x00")


def drain(connection: socket_socket) -> None:
    """Read until the client closes the connection."""
    while connection.recv(4096):
        pass


class BoltStub:
    """A Bolt server on 127.0.0.1 whose answers the test controls."""

    def __init__(self) -> None:
        self.mode = "answer"
        self.rows: list[dict] = [{"ready": 1}]
        self.failure_message = "stub failure"
        self.queries: list[tuple[str, dict]] = []
        self.connections: list[socket_socket] = []
        self.workers: list[threading_Thread] = []
        self.stopping = False
        self.lock = threading_Lock()
        self.listener = socket_create_server(("127.0.0.1", 0))
        self.port = self.listener.getsockname()[1]
        self.acceptor = threading_Thread(target=self.accept, daemon=True)
        self.acceptor.start()

    def accept(self) -> None:
        while True:
            try:
                connection, _ = self.listener.accept()
            except OSError:
                # close() closed the listener.
                break
            with self.lock:
                if self.stopping:
                    connection.close()
                    break
                worker = threading_Thread(target=self.serve, args=(connection,), daemon=True)
                self.connections.append(connection)
                self.workers.append(worker)
                worker.start()

    def serve(self, connection: socket_socket) -> None:
        # A client disconnect, or close() shutting the socket down under a blocked read, is this worker's normal end.
        with connection, contextlib_suppress(OSError, EOFError):
            read_exact(connection, 20)
            if self.mode == "silent":
                drain(connection)
            else:
                connection.sendall(BOLT_5_2)
                self.answer(connection)

    def answer(self, connection: socket_socket) -> None:
        """Answer Bolt messages on one handshaken connection until GOODBYE or a hung query."""
        failed = False
        pending: list[list] = []
        while True:
            message = read_message(connection)
            if message.tag == GOODBYE:
                break
            if message.tag == HELLO:
                send(connection, SUCCESS, {"server": "Neo4j/5.2.0", "connection_id": "bolt-stub"})
            elif message.tag == RUN:
                self.queries.append((message.fields[0], message.fields[1]))
                if self.mode == "hang":
                    drain(connection)
                    break
                if self.mode == "fail":
                    failed = True
                    send(connection, FAILURE, {"code": "Neo.DatabaseError.General.UnknownError", "message": self.failure_message})
                    continue
                keys = list(self.rows[0]) if self.rows else []
                if any(list(row) != keys for row in self.rows):
                    raise ValueError("Bolt stub rows must share one ordered set of keys")
                pending = [list(row.values()) for row in self.rows]
                send(connection, SUCCESS, {"fields": keys, "t_first": 0})
            elif message.tag in (PULL, DISCARD):
                if failed:
                    send(connection, IGNORED)
                    continue
                for values in pending:
                    send(connection, RECORD, values)
                pending = []
                send(connection, SUCCESS, {"has_more": False, "t_last": 0, "type": "r"})
            elif message.tag == RESET:
                failed = False
                send(connection, SUCCESS, {})
            else:
                send(connection, SUCCESS, {})

    def close(self) -> None:
        """Stop accepting, wake every blocked worker and confirm that all of them finished."""
        with self.lock:
            self.stopping = True
        # Closing a listener does not wake a blocked accept on every platform. A loopback
        # connection does, and the acceptor then sees the stop flag and exits.
        socket_create_connection(("127.0.0.1", self.port)).close()
        self.acceptor.join(WORKER_STOP_SECONDS)
        self.listener.close()
        with self.lock:
            connections = list(self.connections)
            workers = list(self.workers)
        for connection in connections:
            try:
                connection.shutdown(SHUT_RDWR)
            except OSError:
                # The worker already finished and closed this connection.
                continue
        for worker in workers:
            worker.join(WORKER_STOP_SECONDS)
        stranded = [thread.name for thread in (self.acceptor, *workers) if thread.is_alive()]
        if stranded:
            raise RuntimeError(f"Bolt stub workers did not stop: {', '.join(stranded)}")
