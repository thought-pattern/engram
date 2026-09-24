"""Single-instance gRPC adapter for :class:`engram.service.EngramCore`."""

from argparse import ArgumentParser as argparse_ArgumentParser
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
from logging import (
    INFO,
    basicConfig,
    getLevelNamesMapping,
    getLogger,
)
from pathlib import Path
from signal import SIGINT, SIGTERM, signal
from sys import argv as sys_argv
from threading import Event as threading_Event, Lock as threading_Lock

from google.protobuf import empty_pb2, json_format, struct_pb2
from google.protobuf.message import Message as protobuf_Message
from grpc import (
    ServicerContext as grpc_ServicerContext,
    StatusCode as grpc_StatusCode,
    server as grpc_server,
    ssl_server_credentials as grpc_ssl_server_credentials,
)
from grpc_health.v1 import health, health_pb2, health_pb2_grpc

from engram import engram_pb2, engram_pb2_grpc
from engram.config import load_config
from engram.constants import DEFAULT_BIND_ADDRESS, DEFAULT_GRACE_SECONDS, DEFAULT_MAX_WORKERS, GRPC_REGULATOR_OUTCOME_NAMES
from engram.errors import (
    ConflictError,
    EngramCoreError,
    InvalidRequestError,
    LifecycleError,
    ResolutionCancelledError,
    ResourceNotFoundError,
)
from engram.identity import query_identity_from_dict
from engram.resolution import resolution_budget_from_dict, resolution_result_to_dict
from engram.service import EngramCore

LOGGER = getLogger(__name__)
SERVICE_NAME = engram_pb2.DESCRIPTOR.services_by_name["EngramService"].full_name
EVIDENCE_SERVICE_NAME = engram_pb2.DESCRIPTOR.services_by_name["EngramEvidenceService"].full_name

outcome_names = {getattr(engram_pb2, name): outcome for name, outcome in GRPC_REGULATOR_OUTCOME_NAMES.items()}


def to_struct(value: dict) -> struct_pb2.Struct:
    """Convert a JSON-ready core result to its protobuf representation."""
    result = struct_pb2.Struct()
    json_format.ParseDict(value, result)
    return result


def from_struct(value: struct_pb2.Struct) -> dict:
    """Convert caller metadata without inventing a transport-specific schema."""
    result = json_format.MessageToDict(value, preserving_proto_field_name=True)
    return result


def restore_contract_integers(value: object) -> object:
    """Restore integer JSON fields erased by protobuf Struct's number type."""
    if type(value) is dict:
        result: object = {key: restore_contract_integers(item) for key, item in value.items()}
    elif type(value) is list:
        result = [restore_contract_integers(item) for item in value]
    elif type(value) is float and value.is_integer():
        result = int(value)
    else:
        result = value
    return result


def contract_from_struct(value: struct_pb2.Struct) -> dict:
    """Convert a Struct into JSON-facing contract primitives."""
    restored = restore_contract_integers(from_struct(value))
    if not isinstance(restored, dict):
        raise InvalidRequestError("contract payload must be an object")
    return restored


def identity_from_struct(value: struct_pb2.Struct) -> dict:
    """Decode a JSON-facing identity payload into its runtime contract."""
    data = contract_from_struct(value)
    result = query_identity_from_dict(data) if data else {}
    return result


def budget_from_struct(value: struct_pb2.Struct) -> dict:
    """Decode a JSON-facing budget payload into its runtime contract."""
    data = contract_from_struct(value)
    result = resolution_budget_from_dict(data) if data else {}
    return result


def to_evidence_resolution(value: dict) -> engram_pb2.ResolutionResult:
    """Translate one validated core result to the explicit wire contract."""
    current = resolution_result_to_dict(value)
    package = current.get("evidence_package", {})
    if not isinstance(package, dict):
        raise InvalidRequestError("resolution evidence package must be an object")
    result = engram_pb2.ResolutionResult(
        schema_version=current.get("schema_version", 0),
        outcome=current.get("outcome", ""),
        selected_candidate=current.get("selected_candidate", {}),
        selected_candidate_available=current.get("selected_candidate_available", False),
        response_candidates=current.get("response_candidates", []),
        evidence=current.get("evidence", []),
        confidence=current.get("confidence", 0.0),
        confidence_available=current.get("confidence_available", False),
        reason_codes=current.get("reason_codes", []),
        frame_diagnostics=current.get("frame_diagnostics", {}),
        resolver_results=current.get("resolver_results", []),
        budget=current.get("budget", {}),
        evidence_package_available=current.get("evidence_package_available", False),
        evidence_package={
            "wire_version": package.get("wire_version", 0),
            "records": package.get("records", []),
            "retained_count": package.get("retained_count", 0),
            "omitted_count": package.get("omitted_count", 0),
            "truncated": package.get("truncated", False),
            "truncation_reasons": package.get("truncation_reasons", []),
        },
    )
    return result


def status_code(error: EngramCoreError, context: grpc_ServicerContext) -> grpc_StatusCode:
    if isinstance(error, ResolutionCancelledError):
        remaining = context.time_remaining()
        result = (
            grpc_StatusCode.DEADLINE_EXCEEDED
            if isinstance(remaining, (int, float)) and not isinstance(remaining, bool) and remaining <= 0
            else grpc_StatusCode.CANCELLED
        )
        return result
    if isinstance(error, InvalidRequestError):
        result = grpc_StatusCode.INVALID_ARGUMENT
        return result
    if isinstance(error, ResourceNotFoundError):
        result = grpc_StatusCode.NOT_FOUND
        return result
    if isinstance(error, ConflictError):
        result = grpc_StatusCode.ABORTED
        return result
    if isinstance(error, LifecycleError):
        result = grpc_StatusCode.FAILED_PRECONDITION
        return result
    result = grpc_StatusCode.INTERNAL
    return result


def grpc_cancellation_check(context: grpc_ServicerContext, cancelled: threading_Event) -> None:
    """Translate the gRPC call lifecycle into the core cooperative boundary."""
    if cancelled.is_set() or not context.is_active():
        raise ResolutionCancelledError("gRPC resolution request is no longer active")


def require_struct_message(value: protobuf_Message, operation: str) -> struct_pb2.Struct:
    """Require the concrete response type promised by a Struct RPC."""
    if not isinstance(value, struct_pb2.Struct):
        raise InvalidRequestError(f"{operation} returned an invalid protobuf message")
    return value


def require_predicate_message(value: protobuf_Message, operation: str) -> engram_pb2.PredicateResponse:
    """Require the concrete response type promised by a predicate RPC."""
    if not isinstance(value, engram_pb2.PredicateResponse):
        raise InvalidRequestError(f"{operation} returned an invalid protobuf message")
    return value


def require_resolution_message(value: protobuf_Message) -> engram_pb2.ResolutionResult:
    """Require the concrete response type promised by unified resolution."""
    if not isinstance(value, engram_pb2.ResolutionResult):
        raise InvalidRequestError("ResolveEvidence returned an invalid protobuf message")
    return value


class EngramGrpcService(engram_pb2_grpc.EngramServiceServicer):
    """Translate protobuf requests into calls on one shared core."""

    def __init__(
        self,
        core: EngramCore,
        health_servicer: health.HealthServicer,
    ) -> None:
        self.core = core
        self.health_servicer = health_servicer
        self.sync_health()

    def sync_health(self) -> None:
        """Publish core health for both the aggregate and named services."""
        status = self.core.status()
        serving_status = (
            health_pb2.HealthCheckResponse.SERVING
            if status["ready"] and status["healthy"]
            else health_pb2.HealthCheckResponse.NOT_SERVING
        )
        self.health_servicer.set("", serving_status)
        self.health_servicer.set(SERVICE_NAME, serving_status)
        self.health_servicer.set(EVIDENCE_SERVICE_NAME, serving_status)

    def invoke(self, context: grpc_ServicerContext, operation: object) -> protobuf_Message:
        if not callable(operation):
            raise InvalidRequestError("gRPC operation must be callable")
        try:
            result = operation()
            if not isinstance(result, protobuf_Message):
                raise InvalidRequestError("gRPC operation returned a non-message result")
            return result
        except EngramCoreError as error:
            metadata: list[tuple[str, str]] = [("engram-error-type", type(error).__name__)]
            context.set_trailing_metadata(tuple(metadata))
            context.abort(status_code(error, context), str(error))
        except Exception as error:
            LOGGER.error("Unhandled Engram gRPC operation failure (%s)", type(error).__name__)
            context.abort(grpc_StatusCode.INTERNAL, "internal Engram failure")
        finally:
            self.sync_health()

    def StartConversation(self, request: engram_pb2.StartConversationRequest, context: grpc_ServicerContext) -> struct_pb2.Struct:
        random_seed_present = request.HasField("random_seed")
        random_seed = request.random_seed if random_seed_present else 0
        message = self.invoke(
            context,
            lambda: to_struct(
                self.core.start_conversation(
                    user_id=request.user_id,
                    initial_bot_text=request.initial_bot_text,
                    random_seed=random_seed,
                    random_seed_present=random_seed_present,
                )
            ),
        )
        result = require_struct_message(message, "StartConversation")
        return result

    def Chat(self, request: engram_pb2.ChatRequest, context: grpc_ServicerContext) -> struct_pb2.Struct:
        message = self.invoke(context, lambda: to_struct(self.core.chat(request.user_id, request.text)))
        result = require_struct_message(message, "Chat")
        return result

    def InspectConversation(self, request: engram_pb2.UserRequest, context: grpc_ServicerContext) -> struct_pb2.Struct:
        message = self.invoke(context, lambda: to_struct(self.core.inspect_conversation(request.user_id)))
        result = require_struct_message(message, "InspectConversation")
        return result

    def FinishConversation(self, request: engram_pb2.UserRequest, context: grpc_ServicerContext) -> struct_pb2.Struct:
        message = self.invoke(context, lambda: to_struct(self.core.finish_conversation(request.user_id)))
        result = require_struct_message(message, "FinishConversation")
        return result

    def StopConversation(self, request: engram_pb2.UserRequest, context: grpc_ServicerContext) -> struct_pb2.Struct:
        message = self.invoke(context, lambda: to_struct(self.core.stop_conversation(request.user_id)))
        result = require_struct_message(message, "StopConversation")
        return result

    def AddFact(self, request: engram_pb2.AddFactRequest, context: grpc_ServicerContext) -> struct_pb2.Struct:
        message = self.invoke(context, lambda: to_struct(self.core.add_fact(request.text, source_label=request.source_label)))
        result = require_struct_message(message, "AddFact")
        return result

    def SetPredicate(self, request: engram_pb2.SetPredicateRequest, context: grpc_ServicerContext) -> engram_pb2.PredicateResponse:
        def set_predicate() -> engram_pb2.PredicateResponse:
            self.core.set_predicate(request.user_id, request.name, request.value)
            result = engram_pb2.PredicateResponse(value=request.value)
            return result

        message = self.invoke(context, set_predicate)
        result = require_predicate_message(message, "SetPredicate")
        return result

    def GetPredicate(self, request: engram_pb2.GetPredicateRequest, context: grpc_ServicerContext) -> engram_pb2.PredicateResponse:
        message = self.invoke(
            context,
            lambda: engram_pb2.PredicateResponse(
                value=self.core.get_predicate(request.user_id, request.name, request.default_value),
            ),
        )
        result = require_predicate_message(message, "GetPredicate")
        return result

    def Propose(self, request: engram_pb2.ProposeRequest, context: grpc_ServicerContext) -> struct_pb2.Struct:
        limit = request.limit if request.HasField("limit") else 1
        message = self.invoke(
            context,
            lambda: to_struct(
                self.core.propose(
                    request=request.request,
                    request_id=request.request_id,
                    user_id=request.user_id,
                    namespace=request.namespace,
                    context_fingerprint=request.context_fingerprint,
                    limit=limit,
                    required_metadata=from_struct(request.required_metadata),
                    required_source_label=request.required_source_label,
                )
            ),
        )
        result = require_struct_message(message, "Propose")
        return result

    def Resolve(self, request: engram_pb2.ResolveRequest, context: grpc_ServicerContext) -> struct_pb2.Struct:
        outcome = outcome_names.get(request.outcome, "")
        message = self.invoke(
            context,
            lambda: to_struct(
                self.core.resolve(
                    request.proposal_id,
                    outcome,
                    statement_id=request.statement_id,
                    reason=request.reason,
                )
            ),
        )
        result = require_struct_message(message, "Resolve")
        return result

    def LearnResponse(self, request: engram_pb2.LearnResponseRequest, context: grpc_ServicerContext) -> struct_pb2.Struct:
        source_label = request.source_label or "tapestry:actor"
        message = self.invoke(
            context,
            lambda: to_struct(
                self.core.learn_response(
                    request=request.request,
                    response=request.response,
                    request_id=request.request_id,
                    user_id=request.user_id,
                    namespace=request.namespace,
                    context_fingerprint=request.context_fingerprint,
                    source_label=source_label,
                    metadata=contract_from_struct(request.metadata),
                )
            ),
        )
        result = require_struct_message(message, "LearnResponse")
        return result

    def RetireResponse(self, request: engram_pb2.RetireResponseRequest, context: grpc_ServicerContext) -> struct_pb2.Struct:
        message = self.invoke(
            context,
            lambda: to_struct(
                self.core.retire_response(
                    request.statement_id,
                    request.reason,
                    request.request_id,
                )
            ),
        )
        result = require_struct_message(message, "RetireResponse")
        return result

    def GetStatus(self, request: empty_pb2.Empty, context: grpc_ServicerContext) -> struct_pb2.Struct:
        message = self.invoke(context, lambda: to_struct(self.core.status()))
        result = require_struct_message(message, "GetStatus")
        return result


class EngramEvidenceGrpcService(engram_pb2_grpc.EngramEvidenceServiceServicer):
    """Expose unified resolution as an additional Engram service."""

    def __init__(self, adapter: EngramGrpcService) -> None:
        self.adapter = adapter

    def ResolveEvidence(
        self,
        request: engram_pb2.ResolveEvidenceRequest,
        context: grpc_ServicerContext,
    ) -> engram_pb2.ResolutionResult:
        cancelled = threading_Event()
        if not context.add_callback(cancelled.set):
            cancelled.set()
        message = self.adapter.invoke(
            context,
            lambda: to_evidence_resolution(
                self.adapter.core.resolve_request(
                    request=request.request,
                    request_id=request.request_id,
                    user_id=request.user_id,
                    namespace=request.namespace,
                    context_fingerprint=request.context_fingerprint,
                    identity=identity_from_struct(request.identity),
                    required_metadata=from_struct(request.required_metadata),
                    required_source_label=request.required_source_label,
                    budget=budget_from_struct(request.budget),
                    configured_resolvers=tuple(request.configured_resolvers),
                    accept_exact=request.accept_exact,
                    cancellation_check=lambda: grpc_cancellation_check(context, cancelled),
                )
            ),
        )
        result = require_resolution_message(message)
        return result


class EngramGrpcServer:
    """Own one gRPC server, one health service, and exactly one Engram core."""

    def __init__(
        self,
        core: EngramCore,
        *,
        bind_address: str = DEFAULT_BIND_ADDRESS,
        max_workers: int = DEFAULT_MAX_WORKERS,
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
        self.internal_server = grpc_server(ThreadPoolExecutor(max_workers=max_workers))
        self.health_servicer = health.HealthServicer()
        self.service = EngramGrpcService(
            core,
            self.health_servicer,
        )
        self.evidence_service = EngramEvidenceGrpcService(self.service)
        engram_pb2_grpc.add_EngramServiceServicer_to_server(self.service, self.internal_server)
        engram_pb2_grpc.add_EngramEvidenceServiceServicer_to_server(self.evidence_service, self.internal_server)
        health_pb2_grpc.add_HealthServicer_to_server(self.health_servicer, self.internal_server)

        if tls_certificate and tls_private_key:
            credentials = grpc_ssl_server_credentials([(tls_private_key, tls_certificate)])
            self.bound_port = self.internal_server.add_secure_port(bind_address, credentials)
        else:
            self.bound_port = self.internal_server.add_insecure_port(bind_address)
        if self.bound_port == 0:
            raise InvalidRequestError(f"gRPC server could not bind to {bind_address}")

        self.started = False
        self.shutdown_started = False
        self.internal_closed = False
        self.lifecycle_lock = threading_Lock()

    @property
    def target(self) -> str:
        """Return the dial target, including an allocated ephemeral port."""
        if self.bind_address.endswith(":0"):
            result = f"{self.bind_address[:-1]}{self.bound_port}"
            return result
        result = self.bind_address
        return result

    def start(self) -> str:
        """Start accepting RPCs and return the client dial target."""
        with self.lifecycle_lock:
            if self.shutdown_started:
                raise LifecycleError("gRPC server has already stopped")
            if not self.started:
                self.service.sync_health()
                self.internal_server.start()
                self.started = True
            result = self.target
            return result

    def wait_for_termination(self, timeout: float = 0.0, timeout_present: bool = False) -> bool:
        """Wait for server termination, returning gRPC's timeout indicator."""
        if timeout_present or timeout:
            result = self.internal_server.wait_for_termination(timeout=timeout)
            return result
        result = self.internal_server.wait_for_termination()
        return result

    def stop(self, grace: float = DEFAULT_GRACE_SECONDS) -> bool:
        """Stop admission, drain calls, then close the single core instance."""
        if not isinstance(grace, (int, float)) or isinstance(grace, bool) or grace < 0:
            raise InvalidRequestError("grace must be a non-negative number")
        with self.lifecycle_lock:
            if self.internal_closed:
                result = False
                return result
            if not self.shutdown_started:
                self.shutdown_started = True
                self.health_servicer.enter_graceful_shutdown()
                stop_event = self.internal_server.stop(grace if self.started else 0)
                stop_event.wait(timeout=float(grace) + 5.0)
            closed = self.core.close()
            self.internal_closed = True
            return closed


def argument_parser() -> argparse_ArgumentParser:
    parser = argparse_ArgumentParser(description="Run the single-instance Engram gRPC server")
    parser.add_argument("--bind", default=DEFAULT_BIND_ADDRESS, help="listen address, default: %(default)s")
    parser.add_argument("--config-path", default="", help="Engram YAML configuration")
    parser.add_argument("--max-workers", type=int, default=DEFAULT_MAX_WORKERS, help="maximum concurrent RPC handlers")
    parser.add_argument("--grace-period", type=float, default=DEFAULT_GRACE_SECONDS, help="shutdown grace period in seconds")
    parser.add_argument("--tls-cert", default="", help="PEM server certificate")
    parser.add_argument("--tls-key", default="", help="PEM server private key")
    parser.add_argument("--log-level", default="INFO", choices=("DEBUG", "INFO", "WARNING", "ERROR"))
    return parser


def read_tls_files(parser: argparse_ArgumentParser, certificate_path: str, private_key_path: str) -> tuple[bytes, bytes]:
    if bool(certificate_path) != bool(private_key_path):
        parser.error("--tls-cert and --tls-key must be provided together")
    if not certificate_path:
        result = b"", b""
        return result
    try:
        result = Path(certificate_path).read_bytes(), Path(private_key_path).read_bytes()
        return result
    except OSError as error:
        parser.error(f"unable to read TLS files: {error}")


def main(argv: tuple[str, ...] = ()) -> int:
    """Run Engram until SIGINT or SIGTERM requests graceful shutdown."""
    parser = argument_parser()
    args = parser.parse_args(argv)
    log_level = getLevelNamesMapping().get(args.log_level, INFO)
    basicConfig(level=log_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    certificate, private_key = read_tls_files(parser, args.tls_cert, args.tls_key)

    config = load_config(args.config_path) if args.config_path else {}
    try:
        core = EngramCore(config=config)
        server = EngramGrpcServer(
            core,
            bind_address=args.bind,
            max_workers=args.max_workers,
            tls_certificate=certificate,
            tls_private_key=private_key,
        )
    except EngramCoreError as error:
        LOGGER.error("Unable to initialize Engram gRPC server (%s)", type(error).__name__)
        result = 1
        return result

    shutdown_requested = threading_Event()

    def request_shutdown(signum, frame) -> None:
        LOGGER.info("Received signal %s; beginning graceful shutdown", signum)
        shutdown_requested.set()

    signal(SIGINT, request_shutdown)
    signal(SIGTERM, request_shutdown)

    server.start()
    LOGGER.info("Engram gRPC server listening on %s", server.target)
    with suppress(KeyboardInterrupt):
        shutdown_requested.wait()

    try:
        server.stop(args.grace_period)
    except EngramCoreError as error:
        LOGGER.error("Engram gRPC shutdown failed (%s)", type(error).__name__)
        result = 1
        return result
    LOGGER.info("Engram gRPC server stopped")
    result = 0
    return result


def console_main() -> int:
    """Run the packaged console entry point with the process arguments."""
    result = main(tuple(sys_argv[1:]))
    return result


if __name__ == "__main__":
    raise SystemExit(main(tuple(sys_argv[1:])))
