from google.protobuf import struct_pb2 as _struct_pb2
from google.protobuf.internal import containers as _containers
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from collections.abc import Iterable as _Iterable, Mapping as _Mapping
from typing import ClassVar as _ClassVar, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class ResolveEvidenceRequest(_message.Message):
    __slots__ = ("request", "request_id", "user_id", "namespace", "context_fingerprint", "identity", "required_metadata", "required_source_label", "budget", "configured_resolvers", "accept_exact")
    REQUEST_FIELD_NUMBER: _ClassVar[int]
    REQUEST_ID_FIELD_NUMBER: _ClassVar[int]
    USER_ID_FIELD_NUMBER: _ClassVar[int]
    NAMESPACE_FIELD_NUMBER: _ClassVar[int]
    CONTEXT_FINGERPRINT_FIELD_NUMBER: _ClassVar[int]
    IDENTITY_FIELD_NUMBER: _ClassVar[int]
    REQUIRED_METADATA_FIELD_NUMBER: _ClassVar[int]
    REQUIRED_SOURCE_LABEL_FIELD_NUMBER: _ClassVar[int]
    BUDGET_FIELD_NUMBER: _ClassVar[int]
    CONFIGURED_RESOLVERS_FIELD_NUMBER: _ClassVar[int]
    ACCEPT_EXACT_FIELD_NUMBER: _ClassVar[int]
    request: str
    request_id: str
    user_id: str
    namespace: str
    context_fingerprint: str
    identity: _struct_pb2.Struct
    required_metadata: _struct_pb2.Struct
    required_source_label: str
    budget: _struct_pb2.Struct
    configured_resolvers: _containers.RepeatedScalarFieldContainer[str]
    accept_exact: bool
    def __init__(self, request: _Optional[str] = ..., request_id: _Optional[str] = ..., user_id: _Optional[str] = ..., namespace: _Optional[str] = ..., context_fingerprint: _Optional[str] = ..., identity: _Optional[_Union[_struct_pb2.Struct, _Mapping]] = ..., required_metadata: _Optional[_Union[_struct_pb2.Struct, _Mapping]] = ..., required_source_label: _Optional[str] = ..., budget: _Optional[_Union[_struct_pb2.Struct, _Mapping]] = ..., configured_resolvers: _Optional[_Iterable[str]] = ..., accept_exact: _Optional[bool] = ...) -> None: ...

class EvidencePackage(_message.Message):
    __slots__ = ("wire_version", "records", "retained_count", "omitted_count", "truncated", "truncation_reasons")
    WIRE_VERSION_FIELD_NUMBER: _ClassVar[int]
    RECORDS_FIELD_NUMBER: _ClassVar[int]
    RETAINED_COUNT_FIELD_NUMBER: _ClassVar[int]
    OMITTED_COUNT_FIELD_NUMBER: _ClassVar[int]
    TRUNCATED_FIELD_NUMBER: _ClassVar[int]
    TRUNCATION_REASONS_FIELD_NUMBER: _ClassVar[int]
    wire_version: int
    records: _containers.RepeatedCompositeFieldContainer[_struct_pb2.Struct]
    retained_count: int
    omitted_count: int
    truncated: bool
    truncation_reasons: _containers.RepeatedScalarFieldContainer[str]
    def __init__(self, wire_version: _Optional[int] = ..., records: _Optional[_Iterable[_Union[_struct_pb2.Struct, _Mapping]]] = ..., retained_count: _Optional[int] = ..., omitted_count: _Optional[int] = ..., truncated: _Optional[bool] = ..., truncation_reasons: _Optional[_Iterable[str]] = ...) -> None: ...

class ResolutionResult(_message.Message):
    __slots__ = ("schema_version", "outcome", "selected_candidate", "selected_candidate_available", "response_candidates", "evidence", "confidence", "confidence_available", "reason_codes", "frame_diagnostics", "resolver_results", "budget", "evidence_package_available", "evidence_package")
    SCHEMA_VERSION_FIELD_NUMBER: _ClassVar[int]
    OUTCOME_FIELD_NUMBER: _ClassVar[int]
    SELECTED_CANDIDATE_FIELD_NUMBER: _ClassVar[int]
    SELECTED_CANDIDATE_AVAILABLE_FIELD_NUMBER: _ClassVar[int]
    RESPONSE_CANDIDATES_FIELD_NUMBER: _ClassVar[int]
    EVIDENCE_FIELD_NUMBER: _ClassVar[int]
    CONFIDENCE_FIELD_NUMBER: _ClassVar[int]
    CONFIDENCE_AVAILABLE_FIELD_NUMBER: _ClassVar[int]
    REASON_CODES_FIELD_NUMBER: _ClassVar[int]
    FRAME_DIAGNOSTICS_FIELD_NUMBER: _ClassVar[int]
    RESOLVER_RESULTS_FIELD_NUMBER: _ClassVar[int]
    BUDGET_FIELD_NUMBER: _ClassVar[int]
    EVIDENCE_PACKAGE_AVAILABLE_FIELD_NUMBER: _ClassVar[int]
    EVIDENCE_PACKAGE_FIELD_NUMBER: _ClassVar[int]
    schema_version: int
    outcome: str
    selected_candidate: _struct_pb2.Struct
    selected_candidate_available: bool
    response_candidates: _containers.RepeatedCompositeFieldContainer[_struct_pb2.Struct]
    evidence: _containers.RepeatedCompositeFieldContainer[_struct_pb2.Struct]
    confidence: float
    confidence_available: bool
    reason_codes: _containers.RepeatedScalarFieldContainer[str]
    frame_diagnostics: _struct_pb2.Struct
    resolver_results: _containers.RepeatedCompositeFieldContainer[_struct_pb2.Struct]
    budget: _struct_pb2.Struct
    evidence_package_available: bool
    evidence_package: EvidencePackage
    def __init__(self, schema_version: _Optional[int] = ..., outcome: _Optional[str] = ..., selected_candidate: _Optional[_Union[_struct_pb2.Struct, _Mapping]] = ..., selected_candidate_available: _Optional[bool] = ..., response_candidates: _Optional[_Iterable[_Union[_struct_pb2.Struct, _Mapping]]] = ..., evidence: _Optional[_Iterable[_Union[_struct_pb2.Struct, _Mapping]]] = ..., confidence: _Optional[float] = ..., confidence_available: _Optional[bool] = ..., reason_codes: _Optional[_Iterable[str]] = ..., frame_diagnostics: _Optional[_Union[_struct_pb2.Struct, _Mapping]] = ..., resolver_results: _Optional[_Iterable[_Union[_struct_pb2.Struct, _Mapping]]] = ..., budget: _Optional[_Union[_struct_pb2.Struct, _Mapping]] = ..., evidence_package_available: _Optional[bool] = ..., evidence_package: _Optional[_Union[EvidencePackage, _Mapping]] = ...) -> None: ...
