"""Single-instance gRPC adapter for :class:`engram.service.EngramCore`."""

from argparse import ArgumentParser as argparse_ArgumentParser
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
from hashlib import sha256 as hashlib_sha256
from importlib import import_module as _import_module
from pathlib import Path
from threading import Event as threading_Event, Lock as threading_Lock
from typing import Any

from google.protobuf import empty_pb2, json_format, struct_pb2
from grpc import (
    ServicerContext as grpc_ServicerContext,
    StatusCode as grpc_StatusCode,
    server as grpc_server,
    ssl_server_credentials as grpc_ssl_server_credentials,
)
from grpc_health.v1 import health, health_pb2, health_pb2_grpc

from engram.config import load_config
from engram.errors import (
    ConflictError,
    EngramCoreError,
    InvalidRequestError,
    LifecycleError,
    PersistenceError,
    ResourceNotFoundError,
)
from engram.service import EngramCore
from engram.v1 import engram_pb2, engram_pb2_grpc

logging = _import_module("logging")
signal = _import_module("signal")

LOGGER = logging.getLogger(__name__)


class _MissingServiceDescriptor:
    """Stable null descriptor for an unavailable generated service."""

    full_name = ""


SERVICE_NAME = engram_pb2.DESCRIPTOR.services_by_name.get("EngramService", _MissingServiceDescriptor()).full_name
DEFAULT_BIND_ADDRESS = "127.0.0.1:50051"
DEFAULT_GRACE_SECONDS = 10.0
DEFAULT_MAX_WORKERS = 10

_OUTCOME_NAMES = {
    engram_pb2.REGULATOR_OUTCOME_ACCEPTED: "accepted",
    engram_pb2.REGULATOR_OUTCOME_REJECTED_QUALITY: "rejected_quality",
    engram_pb2.REGULATOR_OUTCOME_REJECTED_CONTEXT: "rejected_context",
    engram_pb2.REGULATOR_OUTCOME_REJECTED_STALE: "rejected_stale",
    engram_pb2.REGULATOR_OUTCOME_REJECTED_POLICY: "rejected_policy",
}


def _to_struct(value: dict) -> struct_pb2.Struct:
    """Convert a JSON-ready core result to its protobuf representation."""
    result = struct_pb2.Struct()
    json_format.ParseDict(value, result)
    return result


def _from_struct(value: struct_pb2.Struct) -> dict:
    """Convert caller metadata without inventing a transport-specific schema."""
    _return_value = json_format.MessageToDict(value, preserving_proto_field_name=True)
    return _return_value


def _artifact_path(directory: Path, user_id: str, suffix: str = ""):
    """Derive a traversal-safe, stable artifact path from an arbitrary user label."""
    if directory is None:
        return None
    normalized_user_id = user_id or "0"
    digest = hashlib_sha256(normalized_user_id.encode("utf-8")).hexdigest()[:16]
    _return_value = directory / f"conversation-{digest}{suffix}"
    return _return_value


def _status_code(error: EngramCoreError) -> grpc_StatusCode:
    if isinstance(error, InvalidRequestError):
        return grpc_StatusCode.INVALID_ARGUMENT
    if isinstance(error, ResourceNotFoundError):
        return grpc_StatusCode.NOT_FOUND
    if isinstance(error, ConflictError):
        return grpc_StatusCode.ABORTED
    if isinstance(error, LifecycleError):
        return grpc_StatusCode.FAILED_PRECONDITION
    if isinstance(error, PersistenceError):
        return grpc_StatusCode.UNAVAILABLE
    return grpc_StatusCode.INTERNAL


class EngramGrpcService(engram_pb2_grpc.EngramServiceServicer):
    """Translate protobuf requests into calls on one shared core."""

    def __init__(
        self,
        core: EngramCore,
        health_servicer: health.HealthServicer,
        *,
        transcript_directory: object = "",
        report_directory: object = "",
    ) -> None:
        self.core = core
        self.health_servicer = health_servicer
        self.transcript_directory = Path(transcript_directory).resolve() if transcript_directory else None
        self.report_directory = Path(report_directory).resolve() if report_directory else None
        for directory in (self.transcript_directory, self.report_directory):
            if directory is not None:
                directory.mkdir(parents=True, exist_ok=True)
                with suppress(OSError):
                    directory.chmod(0o700)
        self.sync_health()

    def sync_health(self) -> bool:
        """Publish core health for both the aggregate and named services."""
        status = self.core.status()
        serving_status = (
            health_pb2.HealthCheckResponse.SERVING
            if status.get("ready", False) and status.get("healthy", False)
            else health_pb2.HealthCheckResponse.NOT_SERVING
        )
        self.health_servicer.set("", serving_status)
        self.health_servicer.set(SERVICE_NAME, serving_status)
        return False

    def _invoke(self, context: grpc_ServicerContext, operation: Callable[[], Any]) -> Any:
        try:
            _return_value = operation()
            return _return_value
        except EngramCoreError as error:
            metadata = [("engram-error-type", type(error).__name__)]
            if isinstance(error, PersistenceError):
                metadata.extend(
                    [
                        ("engram-operation", error.operation),
                        ("engram-state-changed", str(error.state_changed).lower()),
                    ]
                )
            context.set_trailing_metadata(metadata)
            context.abort(_status_code(error), str(error))
        except Exception:
            LOGGER.exception("Unhandled Engram gRPC operation failure")
            context.abort(grpc_StatusCode.INTERNAL, "internal Engram failure")
        finally:
            self.sync_health()
        return False

    def StartConversation(self, request: engram_pb2.StartConversationRequest, context: grpc_ServicerContext) -> struct_pb2.Struct:
        random_seed = request.random_seed if request.HasField("random_seed") else None
        transcript_path = _artifact_path(self.transcript_directory, request.user_id, ".json")
        _return_value = self._invoke(
            context,
            lambda: _to_struct(
                self.core.start_conversation(
                    user_id=request.user_id,
                    initial_bot_text=request.initial_bot_text,
                    transcript_path=transcript_path or "",
                    random_seed=random_seed,
                )
            ),
        )
        return _return_value

    def Chat(self, request: engram_pb2.ChatRequest, context: grpc_ServicerContext) -> struct_pb2.Struct:
        _return_value = self._invoke(context, lambda: _to_struct(self.core.chat(request.user_id, request.text)))
        return _return_value

    def InspectConversation(self, request: engram_pb2.UserRequest, context: grpc_ServicerContext) -> struct_pb2.Struct:
        _return_value = self._invoke(context, lambda: _to_struct(self.core.inspect_conversation(request.user_id)))
        return _return_value

    def FinishConversation(self, request: engram_pb2.UserRequest, context: grpc_ServicerContext) -> struct_pb2.Struct:
        def finish() -> struct_pb2.Struct:
            output_prefix = _artifact_path(self.report_directory, request.user_id)
            if output_prefix is None:
                raise LifecycleError("report directory is not configured for this server")
            _return_value = _to_struct(self.core.finish_conversation(request.user_id, output_prefix))
            return _return_value

        _return_value = self._invoke(context, finish)
        return _return_value

    def StopConversation(self, request: engram_pb2.UserRequest, context: grpc_ServicerContext) -> struct_pb2.Struct:
        _return_value = self._invoke(context, lambda: _to_struct(self.core.stop_conversation(request.user_id)))
        return _return_value

    def AddFact(self, request: engram_pb2.AddFactRequest, context: grpc_ServicerContext) -> struct_pb2.Struct:
        _return_value = self._invoke(
            context, lambda: _to_struct(self.core.add_fact(request.text, source_label=request.source_label))
        )
        return _return_value

    def SetPredicate(self, request: engram_pb2.SetPredicateRequest, context: grpc_ServicerContext) -> engram_pb2.PredicateResponse:
        def set_predicate() -> engram_pb2.PredicateResponse:
            self.core.set_predicate(request.user_id, request.name, request.value)
            _return_value = engram_pb2.PredicateResponse(value=request.value)
            return _return_value

        _return_value = self._invoke(context, set_predicate)
        return _return_value

    def GetPredicate(self, request: engram_pb2.GetPredicateRequest, context: grpc_ServicerContext) -> engram_pb2.PredicateResponse:
        _return_value = self._invoke(
            context,
            lambda: engram_pb2.PredicateResponse(
                value=self.core.get_predicate(request.user_id, request.name, request.default_value),
            ),
        )
        return _return_value

    def Propose(self, request: engram_pb2.ProposeRequest, context: grpc_ServicerContext) -> struct_pb2.Struct:
        limit = request.limit if request.HasField("limit") else 1
        _return_value = self._invoke(
            context,
            lambda: _to_struct(
                self.core.propose(
                    request=request.request,
                    request_id=request.request_id,
                    user_id=request.user_id,
                    namespace=request.namespace,
                    context_fingerprint=request.context_fingerprint,
                    limit=limit,
                    required_metadata=_from_struct(request.required_metadata),
                    required_source_label=request.required_source_label,
                )
            ),
        )
        return _return_value

    def Resolve(self, request: engram_pb2.ResolveRequest, context: grpc_ServicerContext) -> struct_pb2.Struct:
        outcome = _OUTCOME_NAMES.get(request.outcome, "")
        _return_value = self._invoke(
            context,
            lambda: _to_struct(
                self.core.resolve(
                    request.proposal_id,
                    outcome,
                    statement_id=request.statement_id,
                    reason=request.reason,
                )
            ),
        )
        return _return_value

    def LearnResponse(self, request: engram_pb2.LearnResponseRequest, context: grpc_ServicerContext) -> struct_pb2.Struct:
        source_label = request.source_label or "tapestry:actor"
        _return_value = self._invoke(
            context,
            lambda: _to_struct(
                self.core.learn_response(
                    request=request.request,
                    response=request.response,
                    request_id=request.request_id,
                    user_id=request.user_id,
                    namespace=request.namespace,
                    context_fingerprint=request.context_fingerprint,
                    source_label=source_label,
                    metadata=_from_struct(request.metadata),
                )
            ),
        )
        return _return_value

    def RetireResponse(self, request: engram_pb2.RetireResponseRequest, context: grpc_ServicerContext) -> struct_pb2.Struct:
        _return_value = self._invoke(
            context,
            lambda: _to_struct(
                self.core.retire_response(
                    request.statement_id,
                    request.reason,
                    request.request_id,
                )
            ),
        )
        return _return_value

    def GetStatus(self, request: empty_pb2.Empty, context: grpc_ServicerContext) -> struct_pb2.Struct:
        _return_value = self._invoke(context, lambda: _to_struct(self.core.status()))
        return _return_value

    def Flush(self, request: empty_pb2.Empty, context: grpc_ServicerContext) -> engram_pb2.FlushResponse:
        _return_value = self._invoke(context, lambda: engram_pb2.FlushResponse(persisted=self.core.flush()))
        return _return_value


class EngramGrpcServer:
    """Own one gRPC server, one health service, and exactly one Engram core."""

    def __init__(
        self,
        core: EngramCore,
        *,
        bind_address: str = DEFAULT_BIND_ADDRESS,
        max_workers: int = DEFAULT_MAX_WORKERS,
        transcript_directory: object = "",
        report_directory: object = "",
        tls_certificate: bytes = b"",
        tls_private_key: bytes = b"",
    ) -> None:
        if not isinstance(bind_address, str) or not bind_address.strip():
            raise InvalidRequestError("bind_address must be a non-empty string")
        if not isinstance(max_workers, int) or isinstance(max_workers, bool) or max_workers < 1:
            raise InvalidRequestError("max_workers must be a positive integer")
        if bool(tls_certificate) != bool(tls_private_key):
            raise InvalidRequestError("TLS certificate and private key must be configured together")

        self.core = core
        self.bind_address = bind_address
        self._server = grpc_server(ThreadPoolExecutor(max_workers=max_workers))
        self.health_servicer = health.HealthServicer()
        self.service = EngramGrpcService(
            core,
            self.health_servicer,
            transcript_directory=transcript_directory,
            report_directory=report_directory,
        )
        engram_pb2_grpc.add_EngramServiceServicer_to_server(self.service, self._server)
        health_pb2_grpc.add_HealthServicer_to_server(self.health_servicer, self._server)

        if tls_certificate and tls_private_key:
            credentials = grpc_ssl_server_credentials(((tls_private_key, tls_certificate),))
            self.bound_port = self._server.add_secure_port(bind_address, credentials)
        else:
            self.bound_port = self._server.add_insecure_port(bind_address)
        if self.bound_port == 0:
            raise InvalidRequestError(f"gRPC server could not bind to {bind_address}")

        self._started = False
        self._shutdown_started = False
        self._closed = False
        self._lifecycle_lock = threading_Lock()

    @property
    def target(self) -> str:
        """Return the dial target, including an allocated ephemeral port."""
        if self.bind_address.endswith(":0"):
            _return_value = f"{self.bind_address[:-1]}{self.bound_port}"
            return _return_value
        return self.bind_address

    def start(self) -> str:
        """Start accepting RPCs and return the client dial target."""
        with self._lifecycle_lock:
            if self._shutdown_started:
                raise LifecycleError("gRPC server has already stopped")
            if not self._started:
                self.service.sync_health()
                self._server.start()
                self._started = True
            return self.target
        return ""

    def wait_for_termination(self, timeout=None) -> bool:
        """Wait for server termination, returning gRPC's timeout indicator."""
        _return_value = self._server.wait_for_termination(timeout=timeout)
        return _return_value

    def stop(self, grace: float = DEFAULT_GRACE_SECONDS) -> bool:
        """Stop admission, drain calls, then close the single core instance."""
        if not isinstance(grace, (int, float)) or isinstance(grace, bool) or grace < 0:
            raise InvalidRequestError("grace must be a non-negative number")
        with self._lifecycle_lock:
            if self._closed:
                return False
            if not self._shutdown_started:
                self._shutdown_started = True
                self.health_servicer.enter_graceful_shutdown()
                stop_event = self._server.stop(grace if self._started else 0)
                stop_event.wait(timeout=float(grace) + 5.0)
            closed = self.core.close()
            self._closed = True
            return closed


def create_grpc_server(core: EngramCore, **kwargs) -> EngramGrpcServer:
    """Create, but do not start, a single-instance Engram gRPC server."""
    _return_value = EngramGrpcServer(core, **kwargs)
    return _return_value


def _argument_parser() -> argparse_ArgumentParser:
    parser = argparse_ArgumentParser(description="Run the single-instance Engram gRPC server")
    parser.add_argument("--bind", default=DEFAULT_BIND_ADDRESS, help="listen address, default: %(default)s")
    parser.add_argument("--store-path", default="", help="persistent Engram JSON store")
    parser.add_argument(
        "--seed-path", default="data/seed.json", help="seed corpus synchronized at startup; use an empty value to disable"
    )
    parser.add_argument("--config-path", default="", help="Engram YAML configuration")
    parser.add_argument("--transcript-directory", default="", help="server-owned directory for per-turn recovery transcripts")
    parser.add_argument("--report-directory", default="", help="server-owned directory used by FinishConversation")
    parser.add_argument("--max-workers", type=int, default=DEFAULT_MAX_WORKERS, help="maximum concurrent RPC handlers")
    parser.add_argument("--grace-period", type=float, default=DEFAULT_GRACE_SECONDS, help="shutdown grace period in seconds")
    parser.add_argument("--tls-cert", default="", help="PEM server certificate")
    parser.add_argument("--tls-key", default="", help="PEM server private key")
    parser.add_argument("--log-level", default="INFO", choices=("DEBUG", "INFO", "WARNING", "ERROR"))
    return parser


def _read_tls_files(parser: argparse_ArgumentParser, certificate_path: str, private_key_path: str) -> tuple[bytes, bytes]:
    if bool(certificate_path) != bool(private_key_path):
        parser.error("--tls-cert and --tls-key must be provided together")
    if not certificate_path:
        return None, None
    try:
        _return_value = Path(certificate_path).read_bytes(), Path(private_key_path).read_bytes()
        return _return_value
    except OSError as error:
        parser.error(f"unable to read TLS files: {error}")
    return ()


def main(argv: Sequence[str] = None) -> int:
    """Run Engram until SIGINT or SIGTERM requests graceful shutdown."""
    parser = _argument_parser()
    args = parser.parse_args(argv)
    logging.basicConfig(level=getattr(logging, args.log_level), format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    certificate, private_key = _read_tls_files(parser, args.tls_cert, args.tls_key)

    config = load_config(args.config_path) if args.config_path else None
    try:
        core = EngramCore.open(config=config, store_path=args.store_path, seed_path=args.seed_path)
        core.warm_vector_recall()
        server = create_grpc_server(
            core,
            bind_address=args.bind,
            max_workers=args.max_workers,
            transcript_directory=args.transcript_directory,
            report_directory=args.report_directory,
            tls_certificate=certificate,
            tls_private_key=private_key,
        )
    except EngramCoreError as error:
        LOGGER.error("Unable to initialize Engram gRPC server: %s", error)
        return 1

    shutdown_requested = threading_Event()

    def request_shutdown(signum, frame) -> bool:
        LOGGER.info("Received signal %s; beginning graceful shutdown", signum)
        shutdown_requested.set()
        return False

    signal.signal(signal.SIGINT, request_shutdown)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, request_shutdown)

    server.start()
    LOGGER.info("Engram gRPC server listening on %s", server.target)
    with suppress(KeyboardInterrupt):
        shutdown_requested.wait()

    try:
        server.stop(args.grace_period)
    except EngramCoreError as error:
        LOGGER.error("Engram gRPC shutdown failed: %s", error)
        return 1
    LOGGER.info("Engram gRPC server stopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
