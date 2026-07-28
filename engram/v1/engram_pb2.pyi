from google.protobuf import empty_pb2 as _empty_pb2
from google.protobuf import struct_pb2 as _struct_pb2
from google.protobuf.internal import enum_type_wrapper as _enum_type_wrapper
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from collections.abc import Mapping as _Mapping
from typing import ClassVar as _ClassVar, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class RegulatorOutcome(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    REGULATOR_OUTCOME_UNSPECIFIED: _ClassVar[RegulatorOutcome]
    REGULATOR_OUTCOME_ACCEPTED: _ClassVar[RegulatorOutcome]
    REGULATOR_OUTCOME_REJECTED_QUALITY: _ClassVar[RegulatorOutcome]
    REGULATOR_OUTCOME_REJECTED_CONTEXT: _ClassVar[RegulatorOutcome]
    REGULATOR_OUTCOME_REJECTED_STALE: _ClassVar[RegulatorOutcome]
    REGULATOR_OUTCOME_REJECTED_POLICY: _ClassVar[RegulatorOutcome]
REGULATOR_OUTCOME_UNSPECIFIED: RegulatorOutcome
REGULATOR_OUTCOME_ACCEPTED: RegulatorOutcome
REGULATOR_OUTCOME_REJECTED_QUALITY: RegulatorOutcome
REGULATOR_OUTCOME_REJECTED_CONTEXT: RegulatorOutcome
REGULATOR_OUTCOME_REJECTED_STALE: RegulatorOutcome
REGULATOR_OUTCOME_REJECTED_POLICY: RegulatorOutcome

class StartConversationRequest(_message.Message):
    __slots__ = ("user_id", "initial_bot_text", "random_seed")
    USER_ID_FIELD_NUMBER: _ClassVar[int]
    INITIAL_BOT_TEXT_FIELD_NUMBER: _ClassVar[int]
    RANDOM_SEED_FIELD_NUMBER: _ClassVar[int]
    user_id: str
    initial_bot_text: str
    random_seed: int
    def __init__(self, user_id: _Optional[str] = ..., initial_bot_text: _Optional[str] = ..., random_seed: _Optional[int] = ...) -> None: ...

class ChatRequest(_message.Message):
    __slots__ = ("user_id", "text")
    USER_ID_FIELD_NUMBER: _ClassVar[int]
    TEXT_FIELD_NUMBER: _ClassVar[int]
    user_id: str
    text: str
    def __init__(self, user_id: _Optional[str] = ..., text: _Optional[str] = ...) -> None: ...

class UserRequest(_message.Message):
    __slots__ = ("user_id",)
    USER_ID_FIELD_NUMBER: _ClassVar[int]
    user_id: str
    def __init__(self, user_id: _Optional[str] = ...) -> None: ...

class AddFactRequest(_message.Message):
    __slots__ = ("text", "source_label")
    TEXT_FIELD_NUMBER: _ClassVar[int]
    SOURCE_LABEL_FIELD_NUMBER: _ClassVar[int]
    text: str
    source_label: str
    def __init__(self, text: _Optional[str] = ..., source_label: _Optional[str] = ...) -> None: ...

class SetPredicateRequest(_message.Message):
    __slots__ = ("user_id", "name", "value")
    USER_ID_FIELD_NUMBER: _ClassVar[int]
    NAME_FIELD_NUMBER: _ClassVar[int]
    VALUE_FIELD_NUMBER: _ClassVar[int]
    user_id: str
    name: str
    value: str
    def __init__(self, user_id: _Optional[str] = ..., name: _Optional[str] = ..., value: _Optional[str] = ...) -> None: ...

class GetPredicateRequest(_message.Message):
    __slots__ = ("user_id", "name", "default_value")
    USER_ID_FIELD_NUMBER: _ClassVar[int]
    NAME_FIELD_NUMBER: _ClassVar[int]
    DEFAULT_VALUE_FIELD_NUMBER: _ClassVar[int]
    user_id: str
    name: str
    default_value: str
    def __init__(self, user_id: _Optional[str] = ..., name: _Optional[str] = ..., default_value: _Optional[str] = ...) -> None: ...

class PredicateResponse(_message.Message):
    __slots__ = ("value",)
    VALUE_FIELD_NUMBER: _ClassVar[int]
    value: str
    def __init__(self, value: _Optional[str] = ...) -> None: ...

class ProposeRequest(_message.Message):
    __slots__ = ("request", "request_id", "user_id", "namespace", "context_fingerprint", "limit", "required_metadata", "required_source_label")
    REQUEST_FIELD_NUMBER: _ClassVar[int]
    REQUEST_ID_FIELD_NUMBER: _ClassVar[int]
    USER_ID_FIELD_NUMBER: _ClassVar[int]
    NAMESPACE_FIELD_NUMBER: _ClassVar[int]
    CONTEXT_FINGERPRINT_FIELD_NUMBER: _ClassVar[int]
    LIMIT_FIELD_NUMBER: _ClassVar[int]
    REQUIRED_METADATA_FIELD_NUMBER: _ClassVar[int]
    REQUIRED_SOURCE_LABEL_FIELD_NUMBER: _ClassVar[int]
    request: str
    request_id: str
    user_id: str
    namespace: str
    context_fingerprint: str
    limit: int
    required_metadata: _struct_pb2.Struct
    required_source_label: str
    def __init__(self, request: _Optional[str] = ..., request_id: _Optional[str] = ..., user_id: _Optional[str] = ..., namespace: _Optional[str] = ..., context_fingerprint: _Optional[str] = ..., limit: _Optional[int] = ..., required_metadata: _Optional[_Union[_struct_pb2.Struct, _Mapping]] = ..., required_source_label: _Optional[str] = ...) -> None: ...

class ResolveRequest(_message.Message):
    __slots__ = ("proposal_id", "outcome", "statement_id", "reason")
    PROPOSAL_ID_FIELD_NUMBER: _ClassVar[int]
    OUTCOME_FIELD_NUMBER: _ClassVar[int]
    STATEMENT_ID_FIELD_NUMBER: _ClassVar[int]
    REASON_FIELD_NUMBER: _ClassVar[int]
    proposal_id: str
    outcome: RegulatorOutcome
    statement_id: str
    reason: str
    def __init__(self, proposal_id: _Optional[str] = ..., outcome: _Optional[_Union[RegulatorOutcome, str]] = ..., statement_id: _Optional[str] = ..., reason: _Optional[str] = ...) -> None: ...

class LearnResponseRequest(_message.Message):
    __slots__ = ("request", "response", "request_id", "user_id", "namespace", "context_fingerprint", "source_label", "metadata")
    REQUEST_FIELD_NUMBER: _ClassVar[int]
    RESPONSE_FIELD_NUMBER: _ClassVar[int]
    REQUEST_ID_FIELD_NUMBER: _ClassVar[int]
    USER_ID_FIELD_NUMBER: _ClassVar[int]
    NAMESPACE_FIELD_NUMBER: _ClassVar[int]
    CONTEXT_FINGERPRINT_FIELD_NUMBER: _ClassVar[int]
    SOURCE_LABEL_FIELD_NUMBER: _ClassVar[int]
    METADATA_FIELD_NUMBER: _ClassVar[int]
    request: str
    response: str
    request_id: str
    user_id: str
    namespace: str
    context_fingerprint: str
    source_label: str
    metadata: _struct_pb2.Struct
    def __init__(self, request: _Optional[str] = ..., response: _Optional[str] = ..., request_id: _Optional[str] = ..., user_id: _Optional[str] = ..., namespace: _Optional[str] = ..., context_fingerprint: _Optional[str] = ..., source_label: _Optional[str] = ..., metadata: _Optional[_Union[_struct_pb2.Struct, _Mapping]] = ...) -> None: ...

class RetireResponseRequest(_message.Message):
    __slots__ = ("statement_id", "reason", "request_id")
    STATEMENT_ID_FIELD_NUMBER: _ClassVar[int]
    REASON_FIELD_NUMBER: _ClassVar[int]
    REQUEST_ID_FIELD_NUMBER: _ClassVar[int]
    statement_id: str
    reason: str
    request_id: str
    def __init__(self, statement_id: _Optional[str] = ..., reason: _Optional[str] = ..., request_id: _Optional[str] = ...) -> None: ...

class FlushResponse(_message.Message):
    __slots__ = ("persisted",)
    PERSISTED_FIELD_NUMBER: _ClassVar[int]
    persisted: bool
    def __init__(self, persisted: _Optional[bool] = ...) -> None: ...
