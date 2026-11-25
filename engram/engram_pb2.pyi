"""Static interface for the generated current Engram protobuf messages."""

from google.protobuf.descriptor import EnumDescriptor, FileDescriptor
from google.protobuf.internal.containers import RepeatedCompositeFieldContainer, RepeatedScalarFieldContainer
from google.protobuf.message import Message
from google.protobuf.struct_pb2 import Struct

DESCRIPTOR: FileDescriptor

class RegulatorOutcomeWrapper:
    DESCRIPTOR: EnumDescriptor
    ValueType: type[int]
    REGULATOR_OUTCOME_UNSPECIFIED: int
    REGULATOR_OUTCOME_ACCEPTED: int
    REGULATOR_OUTCOME_REJECTED_QUALITY: int
    REGULATOR_OUTCOME_REJECTED_CONTEXT: int
    REGULATOR_OUTCOME_REJECTED_STALE: int
    REGULATOR_OUTCOME_REJECTED_POLICY: int
    def Name(self, number: int) -> str: ...
    def Value(self, name: str) -> int: ...
    def keys(self) -> list[str]: ...
    def values(self) -> list[int]: ...
    def items(self) -> list[tuple[str, int]]: ...

RegulatorOutcome: RegulatorOutcomeWrapper
REGULATOR_OUTCOME_UNSPECIFIED: int
REGULATOR_OUTCOME_ACCEPTED: int
REGULATOR_OUTCOME_REJECTED_QUALITY: int
REGULATOR_OUTCOME_REJECTED_CONTEXT: int
REGULATOR_OUTCOME_REJECTED_STALE: int
REGULATOR_OUTCOME_REJECTED_POLICY: int

class StartConversationRequest(Message):
    human_id: str
    initial_bot_text: str
    random_seed: int

    def __init__(
        self,
        *,
        human_id: str = ...,
        initial_bot_text: str = ...,
        random_seed: int = ...,
    ) -> None: ...

class ChatRequest(Message):
    human_id: str
    text: str
    conversation_token: str

    def __init__(
        self,
        *,
        human_id: str = ...,
        text: str = ...,
        conversation_token: str = ...,
    ) -> None: ...

class HumanRequest(Message):
    human_id: str
    conversation_token: str

    def __init__(
        self,
        *,
        human_id: str = ...,
        conversation_token: str = ...,
    ) -> None: ...

class ConversationReport(Message):
    started_at: str
    finished_at: str
    human_id: str
    initial_bot_text: str
    random_seed: int
    random_seed_present: bool
    summary: Struct
    metrics_baseline: Struct
    metrics_final: Struct
    session: Struct
    learned_dynamic: RepeatedCompositeFieldContainer[Struct]

    def __init__(
        self,
        *,
        started_at: str = ...,
        finished_at: str = ...,
        human_id: str = ...,
        initial_bot_text: str = ...,
        random_seed: int = ...,
        random_seed_present: bool = ...,
        summary: object = ...,
        metrics_baseline: object = ...,
        metrics_final: object = ...,
        session: object = ...,
        learned_dynamic: object = ...,
    ) -> None: ...

class MetadataEntry(Message):
    key: str
    value: MetadataValue

    def __init__(
        self,
        *,
        key: str = ...,
        value: object = ...,
    ) -> None: ...

class MetadataList(Message):
    values: RepeatedCompositeFieldContainer[MetadataValue]

    def __init__(
        self,
        *,
        values: object = ...,
    ) -> None: ...

class MetadataMap(Message):
    entries: RepeatedCompositeFieldContainer[MetadataEntry]

    def __init__(
        self,
        *,
        entries: object = ...,
    ) -> None: ...

class MetadataValue(Message):
    bool_value: bool
    int_value: int
    float_value: float
    string_value: str
    list_value: MetadataList
    map_value: MetadataMap

    def __init__(
        self,
        *,
        bool_value: bool = ...,
        int_value: int = ...,
        float_value: float = ...,
        string_value: str = ...,
        list_value: object = ...,
        map_value: object = ...,
    ) -> None: ...

class AddFactRequest(Message):
    text: str
    source_label: str

    def __init__(
        self,
        *,
        text: str = ...,
        source_label: str = ...,
    ) -> None: ...

class SetPredicateRequest(Message):
    human_id: str
    name: str
    value: str

    def __init__(
        self,
        *,
        human_id: str = ...,
        name: str = ...,
        value: str = ...,
    ) -> None: ...

class GetPredicateRequest(Message):
    human_id: str
    name: str
    default_value: str

    def __init__(
        self,
        *,
        human_id: str = ...,
        name: str = ...,
        default_value: str = ...,
    ) -> None: ...

class PredicateResponse(Message):
    value: str

    def __init__(
        self,
        *,
        value: str = ...,
    ) -> None: ...

class ProposeRequest(Message):
    request: str
    request_id: str
    human_id: str
    namespace: str
    context_fingerprint: str
    limit: int
    required_metadata: MetadataMap
    required_source_label: str

    def __init__(
        self,
        *,
        request: str = ...,
        request_id: str = ...,
        human_id: str = ...,
        namespace: str = ...,
        context_fingerprint: str = ...,
        limit: int = ...,
        required_metadata: object = ...,
        required_source_label: str = ...,
    ) -> None: ...

class ResolveRequest(Message):
    proposal_id: str
    outcome: int
    statement_id: str
    reason: str

    def __init__(
        self,
        *,
        proposal_id: str = ...,
        outcome: int = ...,
        statement_id: str = ...,
        reason: str = ...,
    ) -> None: ...

class LearnResponseRequest(Message):
    request: str
    response: str
    request_id: str
    human_id: str
    namespace: str
    context_fingerprint: str
    source_label: str
    metadata: MetadataMap

    def __init__(
        self,
        *,
        request: str = ...,
        response: str = ...,
        request_id: str = ...,
        human_id: str = ...,
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
        *,
        statement_id: str = ...,
        reason: str = ...,
        request_id: str = ...,
    ) -> None: ...

class ResponsesBySupportRequest(Message):
    record_ids: RepeatedScalarFieldContainer[str]

    def __init__(
        self,
        *,
        record_ids: object = ...,
    ) -> None: ...

class ResolveEvidenceRequest(Message):
    request: str
    request_id: str
    human_id: str
    namespace: str
    context_fingerprint: str
    identity: Struct
    required_metadata: MetadataMap
    required_source_label: str
    budget: Struct
    configured_resolvers: RepeatedScalarFieldContainer[str]
    accept_exact: bool

    def __init__(
        self,
        *,
        request: str = ...,
        request_id: str = ...,
        human_id: str = ...,
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
    records: RepeatedCompositeFieldContainer[Struct]
    retained_count: int
    omitted_count: int
    truncated: bool
    truncation_reasons: RepeatedScalarFieldContainer[str]

    def __init__(
        self,
        *,
        records: object = ...,
        retained_count: int = ...,
        omitted_count: int = ...,
        truncated: bool = ...,
        truncation_reasons: object = ...,
    ) -> None: ...

class ResolutionResult(Message):
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
        *,
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
