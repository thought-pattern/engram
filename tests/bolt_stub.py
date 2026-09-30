"""In-process Bolt 5.2 server for graph connection tests.

It speaks enough of the protocol for the neo4j driver: the handshake,
HELLO/LOGON, auto-commit RUN + PULL, RESET, and GOODBYE. ``mode`` decides how
the next handshake or query is answered:

- ``answer``: reply with ``rows`` (dicts sharing one set of keys)
- ``hang``: never reply to the query
- ``fail``: reply with a FAILURE carrying ``failure_message``
- ``silent``: accept connections but never answer the handshake

Received queries and their parameters are kept in ``queries``.
"""

from socket import create_server as socket_create_server, socket as socket_socket
from struct import calcsize as struct_calcsize, pack as struct_pack, unpack as struct_unpack
from threading import Lock as threading_Lock, Thread as threading_Thread

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


class Structure:
    """A PackStream structure, such as a temporal value."""

    def __init__(self, tag: int, *fields) -> None:
        self.tag = tag
        self.fields = fields


def pack(value) -> bytes:
    """Encode one value as PackStream."""
    if value is None:
        return b"\xc0"
    if value is True:
        return b"\xc3"
    if value is False:
        return b"\xc2"
    if isinstance(value, int):
        if -16 <= value < 128:
            return struct_pack(">b", value)
        return b"\xcb" + struct_pack(">q", value)
    if isinstance(value, float):
        return b"\xc1" + struct_pack(">d", value)
    if isinstance(value, str):
        data = value.encode("utf-8")
        header = bytes([0x80 + len(data)]) if len(data) < 16 else b"\xd1" + struct_pack(">H", len(data))
        return header + data
    if isinstance(value, (list, tuple)):
        header = bytes([0x90 + len(value)]) if len(value) < 16 else b"\xd5" + struct_pack(">H", len(value))
        return header + b"".join(pack(item) for item in value)
    if isinstance(value, dict):
        header = bytes([0xA0 + len(value)]) if len(value) < 16 else b"\xd9" + struct_pack(">H", len(value))
        return header + b"".join(pack(key) + pack(item) for key, item in value.items())
    if isinstance(value, Structure):
        return bytes([0xB0 + len(value.fields), value.tag]) + b"".join(pack(item) for item in value.fields)
    raise TypeError(f"cannot pack {type(value).__name__}")


def unpack(data: bytes, offset: int = 0):
    """Decode one PackStream value; return it and the next offset."""
    marker = data[offset]
    offset += 1
    if marker < 0x80:
        return marker, offset
    if marker >= 0xF0:
        return marker - 0x100, offset
    high = marker & 0xF0
    if high in (0x80, 0x90, 0xA0, 0xB0):
        size = marker & 0x0F
        return unpack_sized(high, size, data, offset)
    fixed = {0xC8: ">b", 0xC9: ">h", 0xCA: ">i", 0xCB: ">q", 0xC1: ">d"}
    if marker in fixed:
        form = fixed[marker]
        width = struct_calcsize(form)
        return struct_unpack(form, data[offset : offset + width])[0], offset + width
    if marker == 0xC0:
        return None, offset
    if marker in (0xC2, 0xC3):
        return marker == 0xC3, offset
    sized = {0xD0: (0x80, ">B"), 0xD1: (0x80, ">H"), 0xD2: (0x80, ">I")}
    sized.update({0xD4: (0x90, ">B"), 0xD5: (0x90, ">H"), 0xD6: (0x90, ">I")})
    sized.update({0xD8: (0xA0, ">B"), 0xD9: (0xA0, ">H"), 0xDA: (0xA0, ">I")})
    if marker in sized:
        kind, form = sized[marker]
        width = struct_calcsize(form)
        size = struct_unpack(form, data[offset : offset + width])[0]
        return unpack_sized(kind, size, data, offset + width)
    raise ValueError(f"unsupported PackStream marker {marker:#x}")


def unpack_sized(kind: int, size: int, data: bytes, offset: int):
    if kind == 0x80:
        return data[offset : offset + size].decode("utf-8"), offset + size
    if kind == 0x90:
        items = []
        for _ in range(size):
            item, offset = unpack(data, offset)
            items.append(item)
        return items, offset
    if kind == 0xA0:
        mapping = {}
        for _ in range(size):
            key, offset = unpack(data, offset)
            mapping[key], offset = unpack(data, offset)
        return mapping, offset
    tag = data[offset]
    offset += 1
    fields = []
    for _ in range(size):
        item, offset = unpack(data, offset)
        fields.append(item)
    return Structure(tag, *fields), offset


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
        self.lock = threading_Lock()
        self.listener = socket_create_server(("127.0.0.1", 0))
        self.port = self.listener.getsockname()[1]
        threading_Thread(target=self.accept, daemon=True).start()

    def accept(self) -> None:
        while True:
            try:
                connection, _ = self.listener.accept()
            except OSError:
                return
            with self.lock:
                self.connections.append(connection)
            threading_Thread(target=self.serve, args=(connection,), daemon=True).start()

    def serve(self, connection: socket_socket) -> None:
        try:
            read_exact(connection, 20)
            if self.mode == "silent":
                drain(connection)
                return
            connection.sendall(BOLT_5_2)
            failed = False
            pending: list[list] = []
            while True:
                message = read_message(connection)
                if message.tag == GOODBYE:
                    return
                if message.tag == HELLO:
                    send(connection, SUCCESS, {"server": "Neo4j/5.2.0", "connection_id": "bolt-stub"})
                elif message.tag == RUN:
                    self.queries.append((message.fields[0], message.fields[1]))
                    if self.mode == "hang":
                        drain(connection)
                        return
                    if self.mode == "fail":
                        failed = True
                        send(
                            connection, FAILURE, {"code": "Neo.DatabaseError.General.UnknownError", "message": self.failure_message}
                        )
                        continue
                    keys = list(self.rows[0]) if self.rows else []
                    pending = [[row[key] for key in keys] for row in self.rows]
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
        except (OSError, EOFError):
            return
        finally:
            connection.close()

    def close(self) -> None:
        self.listener.close()
        with self.lock:
            for connection in self.connections:
                connection.close()
