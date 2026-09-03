"""Static interface for the generated current Engram protobuf messages."""

from google.protobuf.descriptor import FileDescriptor
from google.protobuf.internal.containers import RepeatedCompositeFieldContainer, RepeatedScalarFieldContainer
from google.protobuf.message import Message
from google.protobuf.struct_pb2 import Struct

DESCRIPTOR: FileDescriptor

class RegulatorOutcome(int):
    REGULATOR_OUTCOME_UNSPECIFIED: RegulatorOutcome
    REGULATOR_OUTCOME_ACCEPTED: RegulatorOutcome
    REGULATOR_OUTCOME_REJECTED_QUALITY: RegulatorOutcome
    REGULATOR_OUTCOME_REJECTED_CONTEXT: RegulatorOutcome
    REGULATOR_OUTCOME_REJECTED_STALE: RegulatorOutcome
    REGULATOR_OUTCOME_REJECTED_POLICY: RegulatorOutcome

REGULATOR_OUTCOME_UNSPECIFIED: RegulatorOutcome
REGULATOR_OUTCOME_ACCEPTED: RegulatorOutcome
REGULATOR_OUTCOME_REJECTED_QUALITY: RegulatorOutcome
REGULATOR_OUTCOME_REJECTED_CONTEXT: RegulatorOutcome
REGULATOR_OUTCOME_REJECTED_STALE: RegulatorOutcome
REGULATOR_OUTCOME_REJECTED_POLICY: RegulatorOutcome

class StartConversationRequest(Message):
    user_id: str
    initial_bot_text: str
    random_seed: int

    def __init__(
        self,
        user_id: str = ...,
        initial_bot_text: str = ...,
        random_seed: int = ...,
    ) -> None: ...

class ChatRequest(Message):
    user_id: str
    text: str

    def __init__(
        self,
        user_id: str = ...,
        text: str = ...,
    ) -> None: ...

class UserRequest(Message):
    user_id: str

    def __init__(
        self,
        user_id: str = ...,
    ) -> None: ...

class AddFactRequest(Message):
    text: str
    source_label: str

    def __init__(
        self,
        text: str = ...,
        source_label: str = ...,
    ) -> None: ...

class SetPredicateRequest(Message):
    user_id: str
    name: str
    value: str

    def __init__(
        self,
        user_id: str = ...,
        name: str = ...,
        value: str = ...,
    ) -> None: ...

class GetPredicateRequest(Message):
    user_id: str
    name: str
    default_value: str

    def __init__(
        self,
        user_id: str = ...,
        name: str = ...,
        default_value: str = ...,
    ) -> None: ...

class PredicateResponse(Message):
    value: str

    def __init__(
        self,
        value: str = ...,
    ) -> None: ...

class ProposeRequest(Message):
    request: str
    request_id: str
    user_id: str
    namespace: str
    context_fingerprint: str
    limit: int
    required_metadata: Struct
    required_source_label: str

    def __init__(
        self,
        request: str = ...,
        request_id: str = ...,
        user_id: str = ...,
        namespace: str = ...,
        context_fingerprint: str = ...,
        limit: int = ...,
        required_metadata: object = ...,
        required_source_label: str = ...,
    ) -> None: ...

class ResolveRequest(Message):
    proposal_id: str
    outcome: RegulatorOutcome
    statement_id: str
    reason: str

    def __init__(
        self,
        proposal_id: str = ...,
        outcome: RegulatorOutcome = ...,
        statement_id: str = ...,
        reason: str = ...,
    ) -> None: ...

class LearnResponseRequest(Message):
    request: str
    response: str
    request_id: str
    user_id: str
    namespace: str
    context_fingerprint: str
    source_label: str
    metadata: Struct

    def __init__(
        self,
        request: str = ...,
        response: str = ...,
        request_id: str = ...,
        user_id: str = ...,
        namespace: str = ...,
        context_fingerprint: str = ...,
        source_label: str = ...,
        metadata: object = ...,
    ) -> None: ...

class RetireResponseRequest(Message):
    statement_id: str
    reason: str
    request_id: str

    def __init__(
        self,
        statement_id: str = ...,
        reason: str = ...,
        request_id: str = ...,
    ) -> None: ...

class ResolveEvidenceRequest(Message):
    request: str
    request_id: str
    user_id: str
    namespace: str
    context_fingerprint: str
    identity: Struct
    required_metadata: Struct
    required_source_label: str
    budget: Struct
    configured_resolvers: RepeatedScalarFieldContainer[str]
    accept_exact: bool

    def __init__(
        self,
        request: str = ...,
        request_id: str = ...,
        user_id: str = ...,
        namespace: str = ...,
        context_fingerprint: str = ...,
        identity: object = ...,
        required_metadata: object = ...,
        required_source_label: str = ...,
        budget: object = ...,
        configured_resolvers: object = ...,
        accept_exact: bool = ...,
    ) -> None: ...

class EvidencePackage(Message):
    wire_version: int
    records: RepeatedCompositeFieldContainer[Struct]
    retained_count: int
    omitted_count: int
    truncated: bool
    truncation_reasons: RepeatedScalarFieldContainer[str]

    def __init__(
        self,
        wire_version: int = ...,
        records: object = ...,
        retained_count: int = ...,
        omitted_count: int = ...,
        truncated: bool = ...,
        truncation_reasons: object = ...,
    ) -> None: ...

class ResolutionResult(Message):
    schema_version: int
    outcome: str
    selected_candidate: Struct
    selected_candidate_available: bool
    response_candidates: RepeatedCompositeFieldContainer[Struct]
    evidence: RepeatedCompositeFieldContainer[Struct]
    confidence: float
    confidence_available: bool
    reason_codes: RepeatedScalarFieldContainer[str]
    frame_diagnostics: Struct
    resolver_results: RepeatedCompositeFieldContainer[Struct]
    budget: Struct
    evidence_package_available: bool
    evidence_package: EvidencePackage

    def __init__(
        self,
        schema_version: int = ...,
        outcome: str = ...,
        selected_candidate: object = ...,
        selected_candidate_available: bool = ...,
        response_candidates: object = ...,
        evidence: object = ...,
        confidence: float = ...,
        confidence_available: bool = ...,
        reason_codes: object = ...,
        frame_diagnostics: object = ...,
        resolver_results: object = ...,
        budget: object = ...,
        evidence_package_available: bool = ...,
        evidence_package: object = ...,
    ) -> None: ...
