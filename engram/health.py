"""Engram's read-only health figures, page and API, served by a standard-library threaded HTTP server.

`EngramHealth` counts every Engram RPC by call and outcome as `HealthInterceptor` sees it arrive and answer, keeps the
calls in progress, the latest finished calls, the errors by kind and a sample of recent activity every
HEALTH_SAMPLE_SECONDS. Only call names, outcomes, times and counts are kept, never request or conversation content,
user names or IDs; the standard gRPC health checks are not counted. Every figure covers the running process only.

`HealthPage` serves the page (`/`, `/health.js`, `/health-common.js`, `/health.css`), the snapshot (`/api/health`),
liveness (`/health/live`) and readiness (`/health/ready`) on its own listener. The assets in `health_assets/` are copies
of Tapestry's shared health page assets with Engram's own page and renderer, because Engram runs without the tapestry
package; the snapshot has the same shape as every Tapestry component's, so the shared renderer draws it unchanged. A
listener that cannot bind is logged and reported in the snapshot's `http` state while Engram keeps serving.
"""

from collections import deque
from datetime import UTC, datetime
from errno import errorcode
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from json import dumps as json_dumps
from logging import getLogger
from os import getpid
from pathlib import Path
from threading import Lock as threading_Lock, Thread as threading_Thread
from time import monotonic

from grpc import ServerInterceptor, StatusCode as grpc_StatusCode, unary_unary_rpc_method_handler

from engram.constants import DEFAULT_HTTP_REFRESH_SECONDS
from engram.constants_service import HEALTH_HISTORY_SAMPLES, HEALTH_RECENT_OUTCOMES, HEALTH_SAMPLE_SECONDS

LOGGER = getLogger(__name__)
ASSETS = Path(__file__).with_name("health_assets")
# The page's routes, each answered from one file in ASSETS with its media type.
PAGE_FILES = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/health.js": ("health.js", "text/javascript; charset=utf-8"),
    "/health-common.js": ("health-common.js", "text/javascript; charset=utf-8"),
    "/health.css": ("health.css", "text/css; charset=utf-8"),
}
PAGE_HEADERS = {
    "Cache-Control": "no-store",
    "X-Content-Type-Options": "nosniff",
    "Content-Security-Policy": (
        "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; frame-ancestors 'none'"
    ),
}
# The fields of GetStatus the page shows beside the standard figures.
STATUS_FIELDS = ("state", "healthy", "memory_only", "active_conversations", "components", "rollout", "telemetry")


def operation_counts() -> dict:
    counts = {"received": 0, "responses": 0, "outcomes": {}}
    return counts


class EngramHealth:
    """Engram's figures since its server started; every method takes the lock only briefly, from any thread."""

    def __init__(self, incarnation: str):
        self.incarnation = incarnation
        self.lock = threading_Lock()
        self.began = monotonic()
        self.started_at = datetime.now(UTC).isoformat()
        self.items = {}
        self.next_key = 0
        self.requests = {"received": 0, "responses": 0, "last_received_at": "", "last_response_at": "", "by_operation": {}}
        self.timing = {"total_seconds": 0.0, "terminal_count": 0, "execution_seconds": 0.0, "execution_count": 0}
        self.timing.update(queue_only_seconds=0.0, queue_only_count=0)
        self.finished_count = 0
        self.errors = {"by_kind": {}, "last": {}}
        self.recent = deque(maxlen=HEALTH_RECENT_OUTCOMES)
        self.history = deque(maxlen=HEALTH_HISTORY_SAMPLES)
        self.last_sample = self.began - HEALTH_SAMPLE_SECONDS

    def received(self, operation: str) -> int:
        """Count one call arriving and hold it as running until `finished`; return its key."""
        observed_at = datetime.now(UTC).isoformat()
        with self.lock:
            self.next_key += 1
            key = self.next_key
            self.items[key] = {"operation": operation, "started": monotonic()}
            self.requests["received"] = self.requests.get("received", 0) + 1
            self.requests["last_received_at"] = observed_at
            row = self.requests.get("by_operation", {}).setdefault(operation, operation_counts())
            row["received"] = row.get("received", 0) + 1
        return key

    def finished(self, key: int, outcome: str, error_kind: str = "") -> bool:
        """Record a running call's answer by outcome; return whether the key was held, so a call finishes once."""
        now = monotonic()
        observed_at = datetime.now(UTC).isoformat()
        with self.lock:
            item = self.items.pop(key, {})
            if not item:
                return False
            operation = item.get("operation", "")
            elapsed = max(0.0, now - item.get("started", now))
            self.requests["responses"] = self.requests.get("responses", 0) + 1
            self.requests["last_response_at"] = observed_at
            row = self.requests.get("by_operation", {}).setdefault(operation, operation_counts())
            row["responses"] = row.get("responses", 0) + 1
            outcomes = row.get("outcomes", {})
            outcomes[outcome] = outcomes.get(outcome, 0) + 1
            if error_kind:
                kinds = self.errors.get("by_kind", {})
                kinds[error_kind] = kinds.get(error_kind, 0) + 1
                self.errors["last"] = {"kind": error_kind, "operation": operation, "observed_at": observed_at}
            for name, value in (("total_seconds", elapsed), ("execution_seconds", elapsed)):
                self.timing[name] = self.timing.get(name, 0.0) + value
            for name in ("terminal_count", "execution_count"):
                self.timing[name] = self.timing.get(name, 0) + 1
            self.finished_count += 1
            self.recent.append(
                {
                    "operation": operation,
                    "kind": "request",
                    "outcome": outcome,
                    "error_kind": error_kind,
                    "finished_at": observed_at,
                    "queue_seconds": 0.0,
                    "executed": True,
                    "execution_seconds": elapsed,
                    "total_seconds": elapsed,
                    "ids": {},
                }
            )
        return True

    def sample(self) -> bool:
        """Keep one recent-activity sample, at most one every HEALTH_SAMPLE_SECONDS, and return whether it was kept."""
        now = monotonic()
        with self.lock:
            if now - self.last_sample < HEALTH_SAMPLE_SECONDS:
                return False
            self.last_sample = now
            self.history.append(
                {
                    "observed_at": datetime.now(UTC).isoformat(),
                    "elapsed_seconds": now - self.began,
                    "queue_depth": 0,
                    "finished": self.finished_count,
                    "responses": self.requests.get("responses", 0),
                    "provider_calls": 0,
                    "input_tokens": 0,
                    "output_tokens": 0,
                }
            )
        return True

    def snapshot(self, status: dict, *, stopping: bool, http: dict, refresh_seconds: float) -> dict:
        """The standard snapshot every Tapestry page shows, then the GetStatus fields in STATUS_FIELDS.

        Calls wait for a gRPC worker inside the gRPC library, where they cannot be seen, so the queue is always empty.
        """
        now = monotonic()
        with self.lock:
            active = [
                {
                    "operation": item.get("operation", ""),
                    "kind": "request",
                    "stage": "",
                    "elapsed_seconds": max(0.0, now - item.get("started", now)),
                    "stage_seconds": max(0.0, now - item.get("started", now)),
                    "ids": {},
                }
                for item in self.items.values()
            ]
            by_operation = {
                operation: {**row, "outcomes": dict(row.get("outcomes", {}))}
                for operation, row in self.requests.get("by_operation", {}).items()
            }
            requests = {**self.requests, "by_operation": by_operation}
            errors = {"by_kind": dict(self.errors.get("by_kind", {})), "last": dict(self.errors.get("last", {}))}
            timing = dict(self.timing)
            recent = list(reversed(self.recent))
            history = list(self.history)
        for average, total, samples in (
            ("average_total_seconds", "total_seconds", "terminal_count"),
            ("average_execution_seconds", "execution_seconds", "execution_count"),
        ):
            timing[average] = timing.get(total, 0.0) / timing.get(samples, 0) if timing.get(samples, 0) else 0.0
        snapshot = {
            "component": "Engram",
            "observed_at": datetime.now(UTC).isoformat(),
            "incarnation": self.incarnation,
            "process_id": getpid(),
            "started_at": self.started_at,
            "uptime_seconds": max(0.0, now - self.began),
            "ready": bool(status.get("ready", False) and status.get("healthy", False)) and not stopping,
            "stopping": stopping,
            "fault_type": "",
            "http": dict(http),
            "refresh_seconds": refresh_seconds,
            "requests": requests,
            "timing": timing,
            "queue_depth": 0,
            "oldest_wait_seconds": 0.0,
            "queue": [],
            "queue_display_clipped": False,
            "active_work": active,
            "work": {"by_operation": {}},
            "errors": errors,
            "recent_outcomes": recent,
            "history": history,
            "usage": {},
            **{name: status.get(name, False) for name in STATUS_FIELDS},
        }
        return snapshot


def call_outcome(context, err: Exception) -> str:
    """Name how a failed call ended: its gRPC status, or the exception class when no status was set."""
    code = context.code()
    name = code.name if isinstance(code, grpc_StatusCode) else ""
    outcome = name if name and name != "OK" else type(err).__name__
    return outcome


class HealthInterceptor(ServerInterceptor):
    """Count every Engram call from arrival to answer by its method name and outcome; gRPC health checks are not counted."""

    def __init__(self, metrics: EngramHealth):
        self.metrics = metrics

    def intercept_service(self, continuation, handler_call_details):
        handler = continuation(handler_call_details)
        method = handler_call_details.method
        behavior = getattr(handler, "unary_unary", False)
        if handler is None or not callable(behavior) or method.startswith("/grpc.health."):
            return handler
        operation = method.rpartition("/")[2]
        metrics = self.metrics

        def observe(request, context):
            key = metrics.received(operation)
            try:
                response = behavior(request, context)
            except Exception as err:
                outcome = call_outcome(context, err)
                metrics.finished(key, outcome, outcome)
                raise
            metrics.finished(key, "OK")
            return response

        wrapped = unary_unary_rpc_method_handler(
            observe,
            request_deserializer=handler.request_deserializer,
            response_serializer=handler.response_serializer,
        )
        return wrapped


class HealthPageServer(ThreadingHTTPServer):
    """The page's threaded listener; it samples recent activity between requests."""

    daemon_threads = True

    def __init__(self, address: tuple, owner):
        self.owner = owner
        super().__init__(address, HealthPageHandler)

    def service_actions(self):
        self.owner.metrics.sample()
        return False


class HealthPageHandler(BaseHTTPRequestHandler):
    """Answer the page, its assets and the health routes; every other path is not found."""

    server: HealthPageServer
    server_version = "EngramHealth"

    def send_body(self, status: int, body: bytes, media_type: str) -> bool:
        self.send_response(status)
        self.send_header("Content-Type", media_type)
        self.send_header("Content-Length", str(len(body)))
        for name, value in PAGE_HEADERS.items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)
        return True

    def do_GET(self):
        name, media_type = PAGE_FILES.get(self.path, ("", ""))
        if name:
            self.send_body(200, (ASSETS / name).read_bytes(), media_type)
        elif self.path == "/api/health":
            body = json_dumps(self.server.owner.snapshot(), separators=(",", ":")).encode("utf-8")
            self.send_body(200, body, "application/json")
        elif self.path == "/health/live":
            self.send_body(200, b"Running", "text/plain; charset=utf-8")
        elif self.path == "/health/ready":
            ready = self.server.owner.snapshot().get("ready", False) is True
            self.send_body(200 if ready else 503, b"Ready" if ready else b"Unavailable", "text/plain; charset=utf-8")
        else:
            self.send_body(404, b"Not found", "text/plain; charset=utf-8")
        return False

    # BaseHTTPRequestHandler.log_message names its first parameter format; the override keeps the standard-library signature.
    def log_message(self, format, *args):  # pylint: disable=redefined-builtin
        """Page polling is not an audit event; the access log stays silent."""
        return False


class HealthPage:
    """Engram's health listener and the snapshot it serves; `status` returns GetStatus's fields and `stopping` whether
    the server is shutting down."""

    def __init__(self, settings: dict, metrics: EngramHealth, status, stopping):
        self.settings = dict(settings)
        self.metrics = metrics
        self.status = status
        self.stopping = stopping
        self.listener = False
        self.thread = False
        self.state = {"enabled": self.settings.get("enabled", False), "available": False, "error_type": "", "error_code": ""}

    def snapshot(self) -> dict:
        """The snapshot `/api/health` serves, read with the current GetStatus fields."""
        snapshot = self.snapshot_from(self.status())
        return snapshot

    def snapshot_from(self, status: dict) -> dict:
        """The snapshot for GetStatus fields already read, so GetStatus carries the page's snapshot from one read."""
        snapshot = self.metrics.snapshot(
            status,
            stopping=self.stopping(),
            http=self.state,
            refresh_seconds=self.settings.get("refresh_seconds", DEFAULT_HTTP_REFRESH_SECONDS),
        )
        return snapshot

    def start(self) -> bool:
        """Bind and serve when enabled; a bind failure is logged and kept in `state`, and Engram keeps serving."""
        if not self.settings.get("enabled", False) or self.listener:
            return False
        host = self.settings.get("host", "")
        port = self.settings.get("port", 0)
        try:
            self.listener = HealthPageServer((host, port), self)
        except OSError as err:
            self.state.update(error_type=type(err).__name__, error_code=errorcode.get(err.errno or 0, ""))
            LOGGER.error("Engram HTTP health page unavailable (%s)", type(err).__name__)
            return False
        self.thread = threading_Thread(
            target=self.listener.serve_forever, kwargs={"poll_interval": 0.5}, name="engram-health-http", daemon=True
        )
        self.thread.start()
        listen_host = "[" + host + "]" if ":" in host else host
        self.state.update(available=True, url="http://" + listen_host + ":" + str(self.listener.server_port))
        return True

    def stop(self) -> bool:
        self.state["available"] = False
        if not isinstance(self.listener, HealthPageServer):
            return False
        self.listener.shutdown()
        self.listener.server_close()
        if isinstance(self.thread, threading_Thread):
            self.thread.join(timeout=5)
        self.listener = False
        return True
