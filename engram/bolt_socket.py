"""TCP configuration for pymgclient's Bolt sockets.

pymgclient never sets TCP_NODELAY, so each query's small Bolt writes wait on the
server's delayed acknowledgement: about 83 ms per round trip against a 0.5 ms
network, and 0.8 ms with the option set. pymgclient exposes no socket option, so
the connected socket is found by its peer address. The Tapestry graph adapters
carry the same helper (tapestry/knowledge_graph/bolt_socket.py).
"""

from os import fstat, listdir
from socket import IPPROTO_TCP, SOCK_STREAM, TCP_NODELAY, getaddrinfo, socket
from stat import S_ISSOCK


def disable_nagle(host: str, port: int) -> int:
    """Set TCP_NODELAY on this process's sockets connected to one Memgraph endpoint.

    Every such socket belongs to a Memgraph connection, so each one is configured,
    including one another thread has just opened. Returns the number configured.

    Descriptors are only inspected and configured in place: a socket object wraps
    the existing descriptor and is detached, never closed, and no descriptor is
    duplicated. The operating system guards some descriptors it owns (macOS
    EXC_GUARD), and duplicating or closing one terminates the process.
    """
    addresses = {info[4][0] for info in getaddrinfo(host, port, type=SOCK_STREAM)}
    configured = 0
    for name in listdir("/dev/fd"):
        try:
            descriptor = int(name)
            if not S_ISSOCK(fstat(descriptor).st_mode):
                continue
            candidate = socket(fileno=descriptor)
        except (OSError, ValueError):
            continue
        try:
            peer = candidate.getpeername() if candidate.type == SOCK_STREAM else ()
            if isinstance(peer, tuple) and len(peer) >= 2 and peer[0] in addresses and peer[1] == port:
                candidate.setsockopt(IPPROTO_TCP, TCP_NODELAY, 1)
                configured += 1
        except OSError:
            continue
        finally:
            candidate.detach()
    return configured
