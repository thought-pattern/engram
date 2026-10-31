"""TCP keepalive on every graph socket, the watchdog for a database that stops answering.

A graph statement has no deadline: a query runs as long as it needs. A database that stops answering is detected by
the kernel instead. After ``GRAPH_KEEPALIVE_IDLE_SECONDS`` without traffic it probes the peer every
``GRAPH_KEEPALIVE_INTERVAL_SECONDS``, and after ``GRAPH_KEEPALIVE_PROBES`` unanswered probes it fails the socket, which
the driver reports as a lost connection. A database that is alive answers the probes from its kernel however long its
query runs, so only a dead host or a broken network path ends a wait. The system defaults (two hours of idle time on
macOS) are too slow for a watchdog, so each value is set explicitly.

The driver's descriptors are configured in place: a socket object wraps the existing descriptor and is detached, never
closed, and no descriptor is duplicated. The operating system guards some descriptors it owns (macOS EXC_GUARD), and
duplicating or closing one terminates the process.
"""

from os import fstat as os_fstat, listdir as os_listdir
from socket import (
    IPPROTO_TCP,
    SO_KEEPALIVE,
    SOCK_STREAM,
    SOL_SOCKET,
    TCP_KEEPCNT,
    TCP_KEEPINTVL,
    getaddrinfo as socket_getaddrinfo,
    socket as socket_socket,
)
from stat import S_ISSOCK
import socket as socket_options

from engram.constants_graph import GRAPH_KEEPALIVE_IDLE_SECONDS, GRAPH_KEEPALIVE_INTERVAL_SECONDS, GRAPH_KEEPALIVE_PROBES

# The idle-time option is TCP_KEEPIDLE on Linux and TCP_KEEPALIVE on macOS.
KEEPALIVE_IDLE_OPTION = getattr(socket_options, "TCP_KEEPIDLE", getattr(socket_options, "TCP_KEEPALIVE", 0))


def keep_graph_sockets_alive(host: str, port: int) -> int:
    """Set the keepalive watchdog on this process's sockets connected to one graph endpoint.

    Every such socket belongs to a graph connection, so each one is configured, including one another thread has just
    opened. Descriptors are wrapped and detached, never duplicated or closed (the module docstring has why). Returns
    the number configured.
    """
    addresses = {info[4][0] for info in socket_getaddrinfo(host, port, type=SOCK_STREAM)}
    configured = 0
    for name in os_listdir("/dev/fd"):
        try:
            descriptor = int(name)
            if not S_ISSOCK(os_fstat(descriptor).st_mode):
                continue
            candidate = socket_socket(fileno=descriptor)
        except (OSError, ValueError):
            continue
        try:
            peer = candidate.getpeername() if candidate.type == SOCK_STREAM else ()
            if isinstance(peer, tuple) and len(peer) >= 2 and peer[0] in addresses and peer[1] == port:
                candidate.setsockopt(SOL_SOCKET, SO_KEEPALIVE, 1)
                candidate.setsockopt(IPPROTO_TCP, KEEPALIVE_IDLE_OPTION, GRAPH_KEEPALIVE_IDLE_SECONDS)
                candidate.setsockopt(IPPROTO_TCP, TCP_KEEPINTVL, GRAPH_KEEPALIVE_INTERVAL_SECONDS)
                candidate.setsockopt(IPPROTO_TCP, TCP_KEEPCNT, GRAPH_KEEPALIVE_PROBES)
                configured += 1
        except OSError:
            continue
        finally:
            candidate.detach()
    return configured
