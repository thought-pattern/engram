"""Authoritative accepted-response artifact domain contracts.

Section 3 owns these transport-neutral types.  This first slice establishes
the lifecycle vocabulary and policies without depending on storage tier,
residency, persistence, indexes, or adapters.
"""

import json
import math
import unicodedata
from collections.abc import Mapping
from datetime import datetime
from types import MappingProxyType
from typing import TypedDict

from engram.constants import (
    ARTIFACT_PROVENANCE_FIELDS,
    ARTIFACT_PROVENANCE_SCHEMA_VERSION,
    ARTIFACT_SCHEMA_VERSION,
    ARTIFACT_STATISTICS_FIELDS,
    ARTIFACT_STATISTICS_SCHEMA_VERSION,
    CACHED_RESPONSE_ARTIFACT_FIELDS,
    EMPTY_MAPPING,
    HISTORICAL_KEY_REUSE_DECISION_FIELDS,
    LEGAL_LIFECYCLE_TRANSITIONS,
    LIFECYCLE_BASE_DECISION_FIELDS,
    LIFECYCLE_INELIGIBLE_REASONS,
    LIFECYCLE_TRANSITION_DECISION_FIELDS,
    MAX_ARTIFACT_ENUM_BYTES,
    MAX_ARTIFACT_ID_BYTES,
    MAX_CALLER_ID_BYTES,
    MAX_METADATA_BYTES,
    MAX_METADATA_DEPTH,
    MAX_METADATA_ITEMS,
    MAX_METADATA_KEY_BYTES,
    MAX_METADATA_STRING_BYTES,
    MAX_RESPONSE_BYTES,
    MAX_SOURCE_LABEL_BYTES,
    MAX_SUPPORT_CLAIM_ID_BYTES,
    MAX_SUPPORT_CLAIM_IDS,
    MAX_TIMESTAMP_BYTES,
    TERMINAL_LIFECYCLE_STATES as TERMINAL_LIFECYCLE_STATES,
    HistoricalKeyReuseReason,
    LifecycleDecisionReason,
    LifecycleOperation,
    LifecycleState,
    Tier,
)
from engram.errors import IdentityValidationError, InvalidRequestError, LifecycleError
from engram.identity import (
    QueryIdentity,
    RetrievalRepresentation,
    ScopeKey,
    query_identity_from_dict,
    query_identity_to_dict,
    retrieval_representation_from_dict,
    retrieval_representation_to_dict,
    scope_key_from_dict,
    scope_key_to_dict,
    validate_authoritative_identity,
    validate_query_identity,
    validate_retrieval_representation,
    validate_scope_key,
)


def _require_exact_mapping(value: object, name: str, keys: frozenset[str]) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise InvalidRequestError(f"{name} must be an object")
    actual = frozenset(value)
    if actual != keys:
        missing = sorted(keys - actual)
        extra = sorted(actual - keys)
        raise InvalidRequestError(f"{name} has invalid fields: missing={missing}, extra={extra}")
    return value


def _require_mapping(value: object, name: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise InvalidRequestError(f"{name} must be an object")
    return value


def _require_text(value: object, name: str, maximum_bytes: int, *, allow_empty: bool) -> str:
    if not isinstance(value, str):
        raise InvalidRequestError(f"{name} must be a string")
    if not allow_empty and not value:
        raise InvalidRequestError(f"{name} must not be empty")
    if len(value.encode("utf-8")) > maximum_bytes:
        raise InvalidRequestError(f"{name} exceeds the UTF-8 limit of {maximum_bytes} bytes")
    if any(unicodedata.category(character) in {"Cc", "Cs"} for character in value):
        raise InvalidRequestError(f"{name} contains a control or surrogate character")
    return value


def _require_response(value: object) -> str:
    if not isinstance(value, str):
        raise InvalidRequestError("artifact response must be a string")
    if not value:
        raise InvalidRequestError("artifact response must not be empty")
    if len(value.encode("utf-8")) > MAX_RESPONSE_BYTES:
        raise InvalidRequestError(f"artifact response exceeds the UTF-8 limit of {MAX_RESPONSE_BYTES} bytes")
    for character in value:
        if unicodedata.category(character) == "Cs":
            raise InvalidRequestError("artifact response contains a surrogate character")
        if unicodedata.category(character) == "Cc" and character not in "\t\n\r":
            raise InvalidRequestError("artifact response contains an unsupported control character")
    return value


def _require_bool(value: object, name: str) -> bool:
    if not isinstance(value, bool):
        raise InvalidRequestError(f"{name} must be a boolean")
    return value


def _require_positive_int(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise InvalidRequestError(f"{name} must be a positive integer")
    return value


def _require_nonnegative_int(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise InvalidRequestError(f"{name} must be a nonnegative integer")
    return value


def _require_schema_version(value: object, expected: int, name: str) -> int:
    version = _require_positive_int(value, name)
    if version != expected:
        raise InvalidRequestError(f"unsupported {name}: {version}; expected {expected}")
    return version


def _require_canonical_utc_timestamp(value: object, name: str, *, allow_empty: bool) -> str:
    text = _require_text(value, name, MAX_TIMESTAMP_BYTES, allow_empty=allow_empty)
    if not text:
        return text
    if not text.endswith("Z"):
        raise InvalidRequestError(f"{name} must be a canonical RFC 3339 UTC timestamp ending in Z")
    try:
        parsed = datetime.fromisoformat(text[:-1] + "+00:00")
    except ValueError as error:
        raise InvalidRequestError(f"{name} must be a canonical RFC 3339 UTC timestamp") from error
    canonical = parsed.isoformat().replace("+00:00", "Z")
    if canonical != text:
        raise InvalidRequestError(f"{name} must use the canonical RFC 3339 UTC representation")
    return text


def _require_present_timestamp(value: object, available: object, name: str) -> tuple[str, bool]:
    presence = _require_bool(available, f"{name}_available")
    text = _require_canonical_utc_timestamp(value, name, allow_empty=not presence)
    if presence and not text:
        raise InvalidRequestError(f"{name} must not be empty when {name}_available is true")
    if not presence and text:
        raise InvalidRequestError(f"{name} must be empty when {name}_available is false")
    result = (text, presence)
    return result


def _freeze_json_value(value: object, name: str, depth: int, item_count: list[int]) -> object:
    if depth > MAX_METADATA_DEPTH:
        raise InvalidRequestError(f"{name} exceeds the metadata depth limit of {MAX_METADATA_DEPTH}")
    item_count[0] += 1
    if item_count[0] > MAX_METADATA_ITEMS:
        raise InvalidRequestError(f"{name} exceeds the metadata item limit of {MAX_METADATA_ITEMS}")
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        result = _require_text(value, name, MAX_METADATA_STRING_BYTES, allow_empty=True)
        return result
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise InvalidRequestError(f"{name} must not contain a non-finite number")
        return value
    if isinstance(value, Mapping):
        validated_items = []
        for key, item in value.items():
            validated_key = _require_text(key, f"{name} key", MAX_METADATA_KEY_BYTES, allow_empty=False)
            validated_items.append((validated_key, item))
        frozen = {}
        for validated_key, item in sorted(validated_items, key=lambda pair: pair[0]):
            frozen[validated_key] = _freeze_json_value(item, f"{name}.{validated_key}", depth + 1, item_count)
        result = MappingProxyType(frozen)
        return result
    if isinstance(value, (list, tuple)):
        result = tuple(
            _freeze_json_value(item, f"{name}[{position}]", depth + 1, item_count) for position, item in enumerate(value)
        )
        return result
    raise InvalidRequestError(f"{name} contains an unsupported JSON value")


def _freeze_metadata(value: object) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise InvalidRequestError("artifact metadata must be an object")
    frozen = _freeze_json_value(value, "artifact metadata", 0, [0])
    if not isinstance(frozen, Mapping):
        raise InvalidRequestError("artifact metadata must be an object")
    encoded = json.dumps(_thaw_json_value(frozen), ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    if len(encoded.encode("utf-8")) > MAX_METADATA_BYTES:
        raise InvalidRequestError(f"artifact metadata exceeds the UTF-8 limit of {MAX_METADATA_BYTES} bytes")
    return frozen


def _thaw_json_value(value: object) -> object:
    if isinstance(value, Mapping):
        result = {key: _thaw_json_value(item) for key, item in value.items()}
        return result
    if isinstance(value, tuple):
        result = [_thaw_json_value(item) for item in value]
        return result
    return value


def _json_text(value: Mapping[str, object]) -> str:
    result = json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return result


ArtifactProvenance = TypedDict(
    "ArtifactProvenance",
    {
        "schema_version": int,
        "source_label": str,
        "caller_id": str,
        "accepted_at": str,
    },
)
ArtifactStatistics = TypedDict(
    "ArtifactStatistics",
    {
        "schema_version": int,
        "hit_count": int,
        "query_count": int,
        "last_hit": str,
        "last_hit_available": bool,
    },
)
CachedResponseArtifact = TypedDict(
    "CachedResponseArtifact",
    {
        "schema_version": int,
        "statement_id": str,
        "generation": int,
        "response": str,
        "query_identity": QueryIdentity,
        "retrieval": RetrievalRepresentation,
        "tier": Tier,
        "lifecycle": LifecycleState,
        "scope": ScopeKey,
        "support_claim_ids": tuple[str, ...],
        "valid_from": str,
        "valid_from_available": bool,
        "valid_until": str,
        "valid_until_available": bool,
        "knowledge_epoch": int,
        "knowledge_epoch_available": bool,
        "superseded_by": str,
        "provenance": ArtifactProvenance,
        "statistics": ArtifactStatistics,
        "metadata": Mapping[str, object],
    },
)


def validate_artifact_provenance(value: object) -> ArtifactProvenance:
    """Validate and copy bounded accepted-response provenance."""

    data = _require_exact_mapping(value, "ArtifactProvenance", ARTIFACT_PROVENANCE_FIELDS)
    result: ArtifactProvenance = {
        "schema_version": _require_schema_version(
            data["schema_version"],
            ARTIFACT_PROVENANCE_SCHEMA_VERSION,
            "artifact provenance schema_version",
        ),
        "source_label": _require_text(
            data["source_label"],
            "artifact provenance source_label",
            MAX_SOURCE_LABEL_BYTES,
            allow_empty=True,
        ),
        "caller_id": _require_text(
            data["caller_id"],
            "artifact provenance caller_id",
            MAX_CALLER_ID_BYTES,
            allow_empty=True,
        ),
        "accepted_at": _require_canonical_utc_timestamp(
            data["accepted_at"],
            "artifact provenance accepted_at",
            allow_empty=False,
        ),
    }
    return result


def artifact_provenance(
    source_label: str,
    caller_id: str,
    accepted_at: str,
    schema_version: int = ARTIFACT_PROVENANCE_SCHEMA_VERSION,
) -> ArtifactProvenance:
    """Construct bounded accepted-response provenance."""

    raw_provenance = {
        "schema_version": schema_version,
        "source_label": source_label,
        "caller_id": caller_id,
        "accepted_at": accepted_at,
    }
    result = validate_artifact_provenance(raw_provenance)
    return result


def artifact_provenance_to_dict(value: object) -> dict[str, object]:
    """Serialize bounded accepted-response provenance."""

    provenance = validate_artifact_provenance(value)
    result = dict(provenance)
    return result


def artifact_provenance_from_dict(value: object) -> ArtifactProvenance:
    """Decode bounded accepted-response provenance."""

    result = validate_artifact_provenance(value)
    return result


def validate_artifact_statistics(value: object) -> ArtifactStatistics:
    """Validate and copy authoritative response statistics."""

    data = _require_exact_mapping(value, "ArtifactStatistics", ARTIFACT_STATISTICS_FIELDS)
    last_hit, last_hit_available = _require_present_timestamp(
        data["last_hit"],
        data["last_hit_available"],
        "artifact statistics last_hit",
    )
    result: ArtifactStatistics = {
        "schema_version": _require_schema_version(
            data["schema_version"],
            ARTIFACT_STATISTICS_SCHEMA_VERSION,
            "artifact statistics schema_version",
        ),
        "hit_count": _require_nonnegative_int(data["hit_count"], "artifact statistics hit_count"),
        "query_count": _require_nonnegative_int(data["query_count"], "artifact statistics query_count"),
        "last_hit": last_hit,
        "last_hit_available": last_hit_available,
    }
    return result


def artifact_statistics(
    hit_count: int = 0,
    query_count: int = 0,
    last_hit: str = "",
    last_hit_available: bool = False,
    schema_version: int = ARTIFACT_STATISTICS_SCHEMA_VERSION,
) -> ArtifactStatistics:
    """Construct authoritative response statistics."""

    raw_statistics = {
        "schema_version": schema_version,
        "hit_count": hit_count,
        "query_count": query_count,
        "last_hit": last_hit,
        "last_hit_available": last_hit_available,
    }
    result = validate_artifact_statistics(raw_statistics)
    return result


def artifact_statistics_to_dict(value: object) -> dict[str, object]:
    """Serialize authoritative response statistics."""

    statistics = validate_artifact_statistics(value)
    result = dict(statistics)
    return result


def artifact_statistics_from_dict(value: object) -> ArtifactStatistics:
    """Decode authoritative response statistics."""

    result = validate_artifact_statistics(value)
    return result


def validate_cached_response_artifact(value: object) -> CachedResponseArtifact:
    """Validate and defensively copy one accepted-response artifact."""

    data = _require_exact_mapping(value, "CachedResponseArtifact", CACHED_RESPONSE_ARTIFACT_FIELDS)
    schema_version = _require_schema_version(data["schema_version"], ARTIFACT_SCHEMA_VERSION, "artifact schema_version")
    statement_id = _require_text(data["statement_id"], "artifact statement_id", MAX_ARTIFACT_ID_BYTES, allow_empty=False)
    generation = _require_positive_int(data["generation"], "artifact generation")
    response = _require_response(data["response"])
    try:
        query_identity = validate_query_identity(data["query_identity"])
    except IdentityValidationError as error:
        raise InvalidRequestError("artifact query_identity must be a QueryIdentity") from error
    try:
        retrieval = validate_retrieval_representation(data["retrieval"])
    except IdentityValidationError as error:
        raise InvalidRequestError("artifact retrieval must be a RetrievalRepresentation") from error
    validate_authoritative_identity(query_identity, retrieval)
    tier = data["tier"]
    if not isinstance(tier, Tier):
        raise InvalidRequestError("artifact tier must be a Tier")
    lifecycle = _require_lifecycle(data["lifecycle"], "artifact lifecycle")
    try:
        scope = validate_scope_key(data["scope"])
    except IdentityValidationError as error:
        raise InvalidRequestError("artifact scope must be a ScopeKey") from error
    if scope != query_identity["scope"]:
        raise InvalidRequestError("artifact scope must match query_identity scope")
    raw_support = data["support_claim_ids"]
    if not isinstance(raw_support, tuple):
        raise InvalidRequestError("artifact support_claim_ids must be a tuple")
    if len(raw_support) > MAX_SUPPORT_CLAIM_IDS:
        raise InvalidRequestError(f"artifact support_claim_ids exceed the limit of {MAX_SUPPORT_CLAIM_IDS}")
    support_claim_ids = tuple(
        sorted(
            {
                _require_text(
                    claim_id,
                    "artifact support Claim ID",
                    MAX_SUPPORT_CLAIM_ID_BYTES,
                    allow_empty=False,
                )
                for claim_id in raw_support
            }
        )
    )
    valid_from, valid_from_available = _require_present_timestamp(
        data["valid_from"],
        data["valid_from_available"],
        "artifact valid_from",
    )
    valid_until, valid_until_available = _require_present_timestamp(
        data["valid_until"],
        data["valid_until_available"],
        "artifact valid_until",
    )
    knowledge_epoch = _require_nonnegative_int(data["knowledge_epoch"], "artifact knowledge_epoch")
    knowledge_epoch_available = _require_bool(data["knowledge_epoch_available"], "artifact knowledge_epoch_available")
    if not knowledge_epoch_available and knowledge_epoch != 0:
        raise InvalidRequestError("artifact knowledge_epoch must be 0 when unavailable")
    superseded_by = _require_text(data["superseded_by"], "artifact superseded_by", MAX_ARTIFACT_ID_BYTES, allow_empty=True)
    if lifecycle == LifecycleState.ACTIVE and superseded_by:
        raise InvalidRequestError("an ACTIVE artifact must not name superseded_by")
    if lifecycle == LifecycleState.SUPERSEDED and not superseded_by:
        raise InvalidRequestError("a SUPERSEDED artifact must name superseded_by")
    if superseded_by == statement_id:
        raise InvalidRequestError("artifact superseded_by must not reference itself")
    provenance = validate_artifact_provenance(data["provenance"])
    statistics = validate_artifact_statistics(data["statistics"])
    metadata = _freeze_metadata(data["metadata"])
    result: CachedResponseArtifact = {
        "schema_version": schema_version,
        "statement_id": statement_id,
        "generation": generation,
        "response": response,
        "query_identity": query_identity,
        "retrieval": retrieval,
        "tier": tier,
        "lifecycle": lifecycle,
        "scope": scope,
        "support_claim_ids": support_claim_ids,
        "valid_from": valid_from,
        "valid_from_available": valid_from_available,
        "valid_until": valid_until,
        "valid_until_available": valid_until_available,
        "knowledge_epoch": knowledge_epoch,
        "knowledge_epoch_available": knowledge_epoch_available,
        "superseded_by": superseded_by,
        "provenance": provenance,
        "statistics": statistics,
        "metadata": metadata,
    }
    return result


def cached_response_artifact(
    statement_id: str,
    generation: int,
    response: str,
    query_identity: QueryIdentity,
    retrieval: RetrievalRepresentation,
    tier: Tier,
    lifecycle: LifecycleState,
    scope: ScopeKey,
    support_claim_ids: tuple[str, ...],
    valid_from: str,
    valid_from_available: bool,
    valid_until: str,
    valid_until_available: bool,
    knowledge_epoch: int,
    knowledge_epoch_available: bool,
    superseded_by: str,
    provenance: ArtifactProvenance,
    statistics: Mapping[str, object] = EMPTY_MAPPING,
    metadata: Mapping[str, object] = EMPTY_MAPPING,
    schema_version: int = ARTIFACT_SCHEMA_VERSION,
) -> CachedResponseArtifact:
    """Construct one authoritative accepted-response artifact."""

    if not isinstance(statistics, Mapping):
        raise InvalidRequestError("artifact statistics must be ArtifactStatistics")
    selected_statistics = validate_artifact_statistics(statistics) if statistics else artifact_statistics()
    raw_artifact = {
        "schema_version": schema_version,
        "statement_id": statement_id,
        "generation": generation,
        "response": response,
        "query_identity": query_identity,
        "retrieval": retrieval,
        "tier": tier,
        "lifecycle": lifecycle,
        "scope": scope,
        "support_claim_ids": support_claim_ids,
        "valid_from": valid_from,
        "valid_from_available": valid_from_available,
        "valid_until": valid_until,
        "valid_until_available": valid_until_available,
        "knowledge_epoch": knowledge_epoch,
        "knowledge_epoch_available": knowledge_epoch_available,
        "superseded_by": superseded_by,
        "provenance": provenance,
        "statistics": selected_statistics,
        "metadata": metadata,
    }
    result = validate_cached_response_artifact(raw_artifact)
    return result


def cached_response_artifact_to_dict(value: object) -> dict[str, object]:
    """Serialize one authoritative accepted-response artifact."""

    artifact = validate_cached_response_artifact(value)
    result: dict[str, object] = {
        "schema_version": artifact["schema_version"],
        "statement_id": artifact["statement_id"],
        "generation": artifact["generation"],
        "response": artifact["response"],
        "query_identity": query_identity_to_dict(artifact["query_identity"]),
        "retrieval": retrieval_representation_to_dict(artifact["retrieval"]),
        "tier": artifact["tier"].value,
        "lifecycle": artifact["lifecycle"].value,
        "scope": scope_key_to_dict(artifact["scope"]),
        "support_claim_ids": list(artifact["support_claim_ids"]),
        "valid_from": artifact["valid_from"],
        "valid_from_available": artifact["valid_from_available"],
        "valid_until": artifact["valid_until"],
        "valid_until_available": artifact["valid_until_available"],
        "knowledge_epoch": artifact["knowledge_epoch"],
        "knowledge_epoch_available": artifact["knowledge_epoch_available"],
        "superseded_by": artifact["superseded_by"],
        "provenance": artifact_provenance_to_dict(artifact["provenance"]),
        "statistics": artifact_statistics_to_dict(artifact["statistics"]),
        "metadata": _thaw_json_value(artifact["metadata"]),
    }
    return result


def cached_response_artifact_to_json(value: object) -> str:
    """Serialize one accepted-response artifact to canonical JSON."""

    data = cached_response_artifact_to_dict(value)
    result = _json_text(data)
    return result


def cached_response_artifact_from_dict(value: object) -> CachedResponseArtifact:
    """Decode one accepted-response artifact from its wire dictionary."""

    data = _require_exact_mapping(value, "CachedResponseArtifact", CACHED_RESPONSE_ARTIFACT_FIELDS)
    tier_value = _require_text(data["tier"], "artifact tier", MAX_ARTIFACT_ENUM_BYTES, allow_empty=False)
    lifecycle_value = _require_text(data["lifecycle"], "artifact lifecycle", MAX_ARTIFACT_ENUM_BYTES, allow_empty=False)
    try:
        tier = Tier(tier_value)
    except ValueError as error:
        raise InvalidRequestError(f"unsupported artifact tier: {tier_value}") from error
    try:
        lifecycle = LifecycleState(lifecycle_value)
    except ValueError as error:
        raise InvalidRequestError(f"unsupported artifact lifecycle: {lifecycle_value}") from error
    raw_support = data["support_claim_ids"]
    if not isinstance(raw_support, list):
        raise InvalidRequestError("artifact support_claim_ids must be an array")
    metadata = _require_mapping(data["metadata"], "artifact metadata")
    query_identity = _require_mapping(data["query_identity"], "artifact query_identity")
    retrieval = _require_mapping(data["retrieval"], "artifact retrieval")
    scope = _require_mapping(data["scope"], "artifact scope")
    provenance = _require_mapping(data["provenance"], "artifact provenance")
    statistics = _require_mapping(data["statistics"], "artifact statistics")
    result = cached_response_artifact(
        schema_version=_require_schema_version(data["schema_version"], ARTIFACT_SCHEMA_VERSION, "artifact schema_version"),
        statement_id=_require_text(data["statement_id"], "artifact statement_id", MAX_ARTIFACT_ID_BYTES, allow_empty=False),
        generation=_require_positive_int(data["generation"], "artifact generation"),
        response=_require_response(data["response"]),
        query_identity=query_identity_from_dict(query_identity),
        retrieval=retrieval_representation_from_dict(retrieval),
        tier=tier,
        lifecycle=lifecycle,
        scope=scope_key_from_dict(scope),
        support_claim_ids=tuple(raw_support),
        valid_from=_require_canonical_utc_timestamp(data["valid_from"], "artifact valid_from", allow_empty=True),
        valid_from_available=_require_bool(data["valid_from_available"], "artifact valid_from_available"),
        valid_until=_require_canonical_utc_timestamp(data["valid_until"], "artifact valid_until", allow_empty=True),
        valid_until_available=_require_bool(data["valid_until_available"], "artifact valid_until_available"),
        knowledge_epoch=_require_nonnegative_int(data["knowledge_epoch"], "artifact knowledge_epoch"),
        knowledge_epoch_available=_require_bool(data["knowledge_epoch_available"], "artifact knowledge_epoch_available"),
        superseded_by=_require_text(data["superseded_by"], "artifact superseded_by", MAX_ARTIFACT_ID_BYTES, allow_empty=True),
        provenance=artifact_provenance_from_dict(provenance),
        statistics=artifact_statistics_from_dict(statistics),
        metadata=metadata,
    )
    return result


def cached_response_artifact_from_json(value: str) -> CachedResponseArtifact:
    """Decode one accepted-response artifact from canonical JSON."""

    if not isinstance(value, str):
        raise InvalidRequestError("CachedResponseArtifact JSON must be a string")
    try:
        data = json.loads(value)
    except json.JSONDecodeError as error:
        raise InvalidRequestError("CachedResponseArtifact JSON is malformed") from error
    if not isinstance(data, Mapping):
        raise InvalidRequestError("CachedResponseArtifact JSON must contain an object")
    result = cached_response_artifact_from_dict(data)
    return result


LifecycleBaseDecision = TypedDict(
    "LifecycleBaseDecision",
    {
        "lifecycle": LifecycleState,
        "direct_answer_eligible": bool,
        "reason": LifecycleDecisionReason,
    },
)
LifecycleTransitionDecision = TypedDict(
    "LifecycleTransitionDecision",
    {
        "current": LifecycleState,
        "target": LifecycleState,
        "operation": LifecycleOperation,
        "allowed": bool,
        "reason": LifecycleDecisionReason,
    },
)
HistoricalKeyReuseDecision = TypedDict(
    "HistoricalKeyReuseDecision",
    {
        "allowed": bool,
        "reason": HistoricalKeyReuseReason,
    },
)


def _require_lifecycle(value: object, name: str) -> LifecycleState:
    if not isinstance(value, LifecycleState):
        raise InvalidRequestError(f"{name} must be a LifecycleState")
    return value


def _require_operation(value: object) -> LifecycleOperation:
    if not isinstance(value, LifecycleOperation):
        raise InvalidRequestError("lifecycle operation must be a LifecycleOperation")
    return value


def _require_lifecycle_decision_reason(value: object, name: str) -> LifecycleDecisionReason:
    if not isinstance(value, LifecycleDecisionReason):
        raise InvalidRequestError(f"{name} must be a LifecycleDecisionReason")
    return value


def _require_historical_key_reuse_reason(value: object) -> HistoricalKeyReuseReason:
    if not isinstance(value, HistoricalKeyReuseReason):
        raise InvalidRequestError("historical key reuse reason must be a HistoricalKeyReuseReason")
    return value


def validate_lifecycle_base_decision(value: object) -> LifecycleBaseDecision:
    """Validate and copy one lifecycle-only eligibility decision."""

    data = _require_exact_mapping(value, "LifecycleBaseDecision", LIFECYCLE_BASE_DECISION_FIELDS)
    lifecycle = _require_lifecycle(data["lifecycle"], "lifecycle decision lifecycle")
    direct_answer_eligible = _require_bool(data["direct_answer_eligible"], "lifecycle decision direct_answer_eligible")
    reason = _require_lifecycle_decision_reason(data["reason"], "lifecycle decision reason")
    expected_eligible = lifecycle == LifecycleState.ACTIVE
    expected_reason = LifecycleDecisionReason.ELIGIBLE if expected_eligible else LIFECYCLE_INELIGIBLE_REASONS[lifecycle]
    if direct_answer_eligible != expected_eligible or reason != expected_reason:
        raise InvalidRequestError("LifecycleBaseDecision fields do not match lifecycle policy")
    result: LifecycleBaseDecision = {
        "lifecycle": lifecycle,
        "direct_answer_eligible": direct_answer_eligible,
        "reason": reason,
    }
    return result


def lifecycle_base_decision_to_dict(value: object) -> dict[str, object]:
    """Serialize one validated lifecycle-only eligibility decision."""

    decision = validate_lifecycle_base_decision(value)
    result: dict[str, object] = {
        "lifecycle": decision["lifecycle"].value,
        "direct_answer_eligible": decision["direct_answer_eligible"],
        "reason": decision["reason"].value,
    }
    return result


def _lifecycle_transition_outcome(
    current: LifecycleState,
    target: LifecycleState,
    operation: LifecycleOperation,
) -> tuple[bool, LifecycleDecisionReason]:
    if current == target:
        result = (False, LifecycleDecisionReason.SAME_STATE_NOT_A_TRANSITION)
    else:
        transitions = LEGAL_LIFECYCLE_TRANSITIONS[current]
        if operation not in transitions:
            result = (False, LifecycleDecisionReason.TERMINAL_STATE)
        elif transitions[operation] != target:
            result = (False, LifecycleDecisionReason.OPERATION_TARGET_MISMATCH)
        else:
            result = (True, LifecycleDecisionReason.LEGAL_TRANSITION)
    return result


def validate_lifecycle_transition_decision(value: object) -> LifecycleTransitionDecision:
    """Validate and copy one lifecycle transition decision."""

    data = _require_exact_mapping(value, "LifecycleTransitionDecision", LIFECYCLE_TRANSITION_DECISION_FIELDS)
    current = _require_lifecycle(data["current"], "current lifecycle")
    target = _require_lifecycle(data["target"], "target lifecycle")
    operation = _require_operation(data["operation"])
    allowed = _require_bool(data["allowed"], "lifecycle transition allowed")
    reason = _require_lifecycle_decision_reason(data["reason"], "lifecycle transition reason")
    expected_allowed, expected_reason = _lifecycle_transition_outcome(current, target, operation)
    if allowed != expected_allowed or reason != expected_reason:
        raise InvalidRequestError("LifecycleTransitionDecision fields do not match transition policy")
    result: LifecycleTransitionDecision = {
        "current": current,
        "target": target,
        "operation": operation,
        "allowed": allowed,
        "reason": reason,
    }
    return result


def lifecycle_transition_decision_to_dict(value: object) -> dict[str, object]:
    """Serialize one validated lifecycle transition decision."""

    decision = validate_lifecycle_transition_decision(value)
    result = {
        "current": decision["current"].value,
        "target": decision["target"].value,
        "operation": decision["operation"].value,
        "allowed": decision["allowed"],
        "reason": decision["reason"].value,
    }
    return result


def validate_historical_key_reuse_decision(value: object) -> HistoricalKeyReuseDecision:
    """Validate and copy one historical retrieval-key reuse decision."""

    data = _require_exact_mapping(value, "HistoricalKeyReuseDecision", HISTORICAL_KEY_REUSE_DECISION_FIELDS)
    allowed = _require_bool(data["allowed"], "historical key reuse allowed")
    reason = _require_historical_key_reuse_reason(data["reason"])
    expected_allowed = reason == HistoricalKeyReuseReason.ALLOWED_EXPLICIT_REPLACEMENT
    if allowed != expected_allowed:
        raise InvalidRequestError("HistoricalKeyReuseDecision allowed does not match reason")
    result: HistoricalKeyReuseDecision = {"allowed": allowed, "reason": reason}
    return result


def historical_key_reuse_decision_to_dict(value: object) -> dict[str, object]:
    """Serialize one validated historical retrieval-key reuse decision."""

    decision = validate_historical_key_reuse_decision(value)
    result = {"allowed": decision["allowed"], "reason": decision["reason"].value}
    return result


def lifecycle_base_eligibility(lifecycle: LifecycleState) -> LifecycleBaseDecision:
    """Evaluate lifecycle alone; temporal and epoch checks are later stages."""

    state = _require_lifecycle(lifecycle, "lifecycle")
    if state == LifecycleState.ACTIVE:
        raw_decision = {
            "lifecycle": state,
            "direct_answer_eligible": True,
            "reason": LifecycleDecisionReason.ELIGIBLE,
        }
    else:
        raw_decision = {
            "lifecycle": state,
            "direct_answer_eligible": False,
            "reason": LIFECYCLE_INELIGIBLE_REASONS[state],
        }
    decision = validate_lifecycle_base_decision(raw_decision)
    return decision


def lifecycle_transition_decision(
    current: LifecycleState,
    target: LifecycleState,
    operation: LifecycleOperation,
) -> LifecycleTransitionDecision:
    """Return the version 1 legal-transition decision without mutating state."""

    current_state = _require_lifecycle(current, "current lifecycle")
    target_state = _require_lifecycle(target, "target lifecycle")
    named_operation = _require_operation(operation)
    allowed, reason = _lifecycle_transition_outcome(current_state, target_state, named_operation)
    raw_decision = {
        "current": current_state,
        "target": target_state,
        "operation": named_operation,
        "allowed": allowed,
        "reason": reason,
    }
    decision = validate_lifecycle_transition_decision(raw_decision)
    return decision


def require_lifecycle_transition(
    current: LifecycleState,
    target: LifecycleState,
    operation: LifecycleOperation,
) -> LifecycleTransitionDecision:
    """Return a legal decision or raise a stable lifecycle error."""

    decision = lifecycle_transition_decision(current, target, operation)
    if not decision["allowed"]:
        raise LifecycleError(
            f"illegal lifecycle transition: {decision['current'].value} -> {decision['target'].value} "
            f"via {decision['operation'].value} ({decision['reason'].value})"
        )
    return decision


def historical_key_reuse_decision(
    *,
    explicit_replacement: bool,
    expected_statement_id: str,
    expected_generation: int,
) -> HistoricalKeyReuseDecision:
    """Apply the v1 policy for a retrieval key already present in history.

    Repository collision checks remain responsible for verifying that the
    expected owner is the applicable current or historical owner.  This policy
    merely prevents blind base commit from reusing an owned key.
    """

    if not isinstance(explicit_replacement, bool):
        raise InvalidRequestError("explicit_replacement must be a boolean")
    if not isinstance(expected_statement_id, str):
        raise InvalidRequestError("expected_statement_id must be a string")
    if isinstance(expected_generation, bool) or not isinstance(expected_generation, int):
        raise InvalidRequestError("expected_generation must be an integer")
    if not explicit_replacement:
        allowed = False
        reason = HistoricalKeyReuseReason.BASE_COMMIT_FORBIDDEN
    elif not expected_statement_id:
        allowed = False
        reason = HistoricalKeyReuseReason.EXPECTED_STATEMENT_ID_REQUIRED
    elif expected_generation < 1:
        allowed = False
        reason = HistoricalKeyReuseReason.EXPECTED_GENERATION_REQUIRED
    else:
        allowed = True
        reason = HistoricalKeyReuseReason.ALLOWED_EXPLICIT_REPLACEMENT
    raw_decision = {"allowed": allowed, "reason": reason}
    decision = validate_historical_key_reuse_decision(raw_decision)
    return decision


def lifecycle_after_capacity_eviction(lifecycle: LifecycleState) -> LifecycleState:
    """Confirm that capacity eviction cannot perform a lifecycle transition."""

    state = _require_lifecycle(lifecycle, "lifecycle")
    return state
