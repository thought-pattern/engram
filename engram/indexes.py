"""Immutable exact-retrieval and Claim-support indexes.

The records in this module are disposable derived state.  Authoritative
response artifacts live outside the index and supply :class:`IndexProjection`
values.  Builders never infer an exact identity from statement text, patterns,
or lexical keywords.
"""

import json
import threading
from collections.abc import Iterable, Mapping
from types import MappingProxyType

from engram.constants import (
    EXACT_LOOKUP_RESULT_FIELDS,
    INDEX_BUILD_ISSUE_FIELDS,
    INDEX_BUILD_REPORT_FIELDS,
    INDEX_CHECK_ISSUE_FIELDS,
    INDEX_CHECK_REPORT_FIELDS,
    INDEX_COLLISION_REPORT_FIELDS,
    INDEX_PROJECTION_BINDING_FIELDS,
    INDEX_PROJECTION_FIELDS,
    INDEX_PROJECTION_SCHEMA_VERSION,
    INDEX_REPAIR_RESULT_FIELDS,
    INDEX_STATE_FIELDS,
    INDEX_STATE_SCHEMA_VERSION,
    INDEX_SUPPORT_SCAN_LIMIT_REASON,
    MAX_INDEX_CHECK_KEY_BYTES,
    MAX_INDEX_CHECK_NAME_BYTES,
    MAX_INDEX_CHECK_VALUE_BYTES,
    MAX_INDEX_EXCLUSION_REASON_BYTES,
    MAX_INDEX_LOOKUP_OWNERS,
    MAX_INDEX_PROVENANCE_BYTES,
    MAX_INDEX_REPORT_DETAIL_BYTES,
    MAX_INDEX_REPORT_ITEMS,
    MAX_INDEX_REPRESENTATION_BYTES,
    MAX_INDEX_RETRIEVAL_KEYS,
    MAX_INDEX_STATEMENT_ID_BYTES,
    MAX_INDEX_SUPPORT_IDS,
    MAX_INDEX_SUPPORT_REASON_BYTES,
    MAX_INDEX_SUPPORT_SCAN_EDGES,
    MAX_SUPPORT_CLAIM_ID_BYTES,
    RETRIEVAL_OWNER_FIELDS,
    SUPPORT_LOOKUP_RESULT_FIELDS,
    SUPPORT_MATCH_FIELDS,
    SUPPORT_SCAN_PLAN_FIELDS,
    ExactLookupOutcome,
    IndexCheckCategory,
    IndexIssueReason,
)
from engram.errors import ConflictError, IdentityValidationError, InvalidRequestError, UnsupportedIdentityVersionError
from engram.identity import (
    RETRIEVAL_NORMALIZATION_VERSION,
    RetrievalKeyBinding,
    RetrievalOrigin,
    ScopedRetrievalKey,
    ScopedRetrievalKeySignature,
    retrieval_key_binding,
    scoped_retrieval_key_from_dict,
    scoped_retrieval_key_signature,
    scoped_retrieval_key_to_dict,
    scoped_retrieval_key_to_json,
    trusted_scoped_retrieval_key_signature,
    validate_retrieval_key_binding,
    validate_scoped_retrieval_key,
)


def _bounded_text(value: object, name: str, maximum_bytes: int, *, allow_empty: bool) -> str:
    if not isinstance(value, str):
        raise InvalidRequestError(f"{name} must be a string")
    if not allow_empty and not value:
        raise InvalidRequestError(f"{name} must not be empty")
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise InvalidRequestError(f"{name} must not contain control characters")
    if len(value.encode("utf-8")) > maximum_bytes:
        raise InvalidRequestError(f"{name} exceeds the UTF-8 limit of {maximum_bytes} bytes")
    return value


def _positive_int(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise InvalidRequestError(f"{name} must be a positive integer")
    return value


def _exact_mapping(value: object, name: str, keys: set[str]) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise InvalidRequestError(f"{name} must be an object")
    if set(value) != keys:
        missing = sorted(keys - set(value))
        extra = sorted(set(value) - keys)
        raise InvalidRequestError(f"{name} has invalid fields: missing={missing}, extra={extra}")
    return value


def _json_text(value: Mapping[str, object]) -> str:
    result = json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return result


def _key_text(key: ScopedRetrievalKey) -> str:
    result = scoped_retrieval_key_to_json(key)
    return result


IndexProjection = dict


def index_projection(
    statement_id: object,
    generation: object,
    retrieval_keys: object,
    support_claim_ids: object,
    direct_answer_eligible: object,
    exclusion_reason: object,
    normalization_version: object = RETRIEVAL_NORMALIZATION_VERSION,
    schema_version: object = INDEX_PROJECTION_SCHEMA_VERSION,
) -> IndexProjection:
    """Build one bounded index-only artifact projection."""
    version = _positive_int(schema_version, "index projection schema_version")
    if version != INDEX_PROJECTION_SCHEMA_VERSION:
        raise UnsupportedIdentityVersionError(f"unsupported index projection schema version: {version}")
    normalization = _positive_int(normalization_version, "index projection normalization_version")
    if normalization != RETRIEVAL_NORMALIZATION_VERSION:
        raise UnsupportedIdentityVersionError(f"unsupported index projection normalization version: {normalization}")
    normalized_statement_id = _bounded_text(
        statement_id,
        "index projection statement_id",
        MAX_INDEX_STATEMENT_ID_BYTES,
        allow_empty=False,
    )
    normalized_generation = _positive_int(generation, "index projection generation")
    if not isinstance(retrieval_keys, tuple):
        raise InvalidRequestError("index projection retrieval_keys must be a tuple")
    if len(retrieval_keys) > MAX_INDEX_RETRIEVAL_KEYS:
        raise InvalidRequestError(f"index projection retrieval_keys exceed the limit of {MAX_INDEX_RETRIEVAL_KEYS}")
    validated_bindings = []
    for binding in retrieval_keys:
        try:
            validated_binding = validate_retrieval_key_binding(binding)
        except IdentityValidationError as error:
            raise InvalidRequestError("every index projection retrieval key must be a RetrievalKeyBinding") from error
        if validated_binding["key"]["normalization_version"] != normalization:
            raise InvalidRequestError("retrieval key normalization version differs from its projection")
        validated_bindings.append(validated_binding)
    if not isinstance(support_claim_ids, tuple):
        raise InvalidRequestError("index projection support_claim_ids must be a tuple")
    if len(support_claim_ids) > MAX_INDEX_SUPPORT_IDS:
        raise InvalidRequestError(f"index projection support_claim_ids exceed the limit of {MAX_INDEX_SUPPORT_IDS}")
    validated_support = tuple(
        sorted(
            {
                _bounded_text(claim_id, "support Claim ID", MAX_SUPPORT_CLAIM_ID_BYTES, allow_empty=False)
                for claim_id in support_claim_ids
            }
        )
    )
    if not isinstance(direct_answer_eligible, bool):
        raise InvalidRequestError("index projection direct_answer_eligible must be a boolean")
    normalized_reason = _bounded_text(
        exclusion_reason,
        "index projection exclusion_reason",
        MAX_INDEX_EXCLUSION_REASON_BYTES,
        allow_empty=True,
    )
    result: IndexProjection = {
        "statement_id": normalized_statement_id,
        "generation": normalized_generation,
        "retrieval_keys": tuple(validated_bindings),
        "support_claim_ids": validated_support,
        "direct_answer_eligible": direct_answer_eligible,
        "exclusion_reason": normalized_reason,
        "normalization_version": normalization,
        "schema_version": version,
    }
    return result


def validate_index_projection(value: object) -> IndexProjection:
    """Revalidate and copy one in-memory index projection."""
    data = _exact_mapping(value, "IndexProjection", INDEX_PROJECTION_FIELDS)
    result = index_projection(
        data["statement_id"],
        data["generation"],
        data["retrieval_keys"],
        data["support_claim_ids"],
        data["direct_answer_eligible"],
        data["exclusion_reason"],
        data["normalization_version"],
        data["schema_version"],
    )
    return result


def index_projection_with_changes(value: object, changes: object) -> IndexProjection:
    """Apply named fields and revalidate the complete projection."""
    projection = validate_index_projection(value)
    if not isinstance(changes, Mapping):
        raise InvalidRequestError("index projection changes must be an object")
    if not set(changes).issubset(INDEX_PROJECTION_FIELDS):
        raise InvalidRequestError("index projection changes contain an unknown field")
    updated: dict[str, object] = dict(projection)
    updated.update(changes)
    result = validate_index_projection(updated)
    return result


def index_projection_to_dict(value: object) -> dict[str, object]:
    """Serialize one index projection."""
    projection = validate_index_projection(value)
    result = _trusted_index_projection_to_dict(projection)
    return result


def _trusted_index_projection_to_dict(projection: IndexProjection) -> dict[str, object]:
    """Serialize a projection already validated at the index-build boundary."""
    result = {
        "schema_version": projection["schema_version"],
        "statement_id": projection["statement_id"],
        "generation": projection["generation"],
        "retrieval_keys": [
            {
                "key": {
                    "schema_version": binding["key"]["schema_version"],
                    "normalization_version": binding["key"]["normalization_version"],
                    "scope": dict(binding["key"]["scope"]),
                    "normalized_key": binding["key"]["normalized_key"],
                },
                "provenance": binding["origin"].value,
                "representation": binding["representation"],
            }
            for binding in projection["retrieval_keys"]
        ],
        "support_claim_ids": list(projection["support_claim_ids"]),
        "direct_answer_eligible": projection["direct_answer_eligible"],
        "exclusion_reason": projection["exclusion_reason"],
        "normalization_version": projection["normalization_version"],
    }
    return result


def index_projection_to_json(value: object) -> str:
    """Encode one index projection as canonical JSON."""
    data = index_projection_to_dict(value)
    result = _json_text(data)
    return result


def _trusted_index_projection_to_json(value: IndexProjection) -> str:
    """Encode a projection already validated at the index-build boundary."""
    data = _trusted_index_projection_to_dict(value)
    result = _json_text(data)
    return result


def index_projection_from_dict(value: object) -> IndexProjection:
    """Decode one serialized index projection."""
    data = _exact_mapping(value, "IndexProjection", INDEX_PROJECTION_FIELDS)
    schema_version = _positive_int(data["schema_version"], "index projection schema_version")
    normalization_version = _positive_int(data["normalization_version"], "index projection normalization_version")
    raw_keys = data["retrieval_keys"]
    if not isinstance(raw_keys, list):
        raise InvalidRequestError("index projection retrieval_keys must be an array")
    bindings = []
    for position, raw_binding in enumerate(raw_keys):
        binding_data = _exact_mapping(raw_binding, f"retrieval key binding {position}", INDEX_PROJECTION_BINDING_FIELDS)
        provenance = _bounded_text(
            binding_data["provenance"],
            "retrieval provenance",
            MAX_INDEX_PROVENANCE_BYTES,
            allow_empty=False,
        )
        try:
            origin = RetrievalOrigin(provenance)
        except ValueError as error:
            raise InvalidRequestError(f"unsupported retrieval provenance: {provenance}") from error
        key_data = binding_data["key"]
        if not isinstance(key_data, Mapping):
            raise InvalidRequestError(f"retrieval key binding {position} key must be an object")
        bindings.append(
            retrieval_key_binding(
                key=scoped_retrieval_key_from_dict(key_data),
                origin=origin,
                representation=_bounded_text(
                    binding_data["representation"],
                    "retrieval representation",
                    MAX_INDEX_REPRESENTATION_BYTES,
                    allow_empty=False,
                ),
            )
        )
    raw_support = data["support_claim_ids"]
    if not isinstance(raw_support, list):
        raise InvalidRequestError("index projection support_claim_ids must be an array")
    support = tuple(
        _bounded_text(claim_id, "support Claim ID", MAX_SUPPORT_CLAIM_ID_BYTES, allow_empty=False) for claim_id in raw_support
    )
    result = index_projection(
        data["statement_id"],
        data["generation"],
        tuple(bindings),
        support,
        data["direct_answer_eligible"],
        data["exclusion_reason"],
        normalization_version,
        schema_version,
    )
    return result


def index_projection_from_json(value: object) -> IndexProjection:
    """Decode one index projection from canonical JSON."""
    if not isinstance(value, str):
        raise InvalidRequestError("IndexProjection JSON must be a string")
    try:
        data = json.loads(value)
    except json.JSONDecodeError as error:
        raise InvalidRequestError("IndexProjection JSON is malformed") from error
    if not isinstance(data, Mapping):
        raise InvalidRequestError("IndexProjection JSON must contain an object")
    result = index_projection_from_dict(data)
    return result


RetrievalOwner = dict

RetrievalOwnerSignature = tuple[str, int, str, str, bool]


def retrieval_owner(
    statement_id: object,
    generation: object,
    provenance: object,
    representation: object,
    direct_answer_eligible: object,
) -> RetrievalOwner:
    """Build one statement's ownership and provenance for a retrieval key."""
    normalized_statement_id = _bounded_text(
        statement_id,
        "retrieval owner statement_id",
        MAX_INDEX_STATEMENT_ID_BYTES,
        allow_empty=False,
    )
    normalized_generation = _positive_int(generation, "retrieval owner generation")
    if not isinstance(provenance, RetrievalOrigin):
        raise InvalidRequestError("retrieval owner provenance must be a RetrievalOrigin")
    normalized_representation = _bounded_text(
        representation,
        "retrieval owner representation",
        MAX_INDEX_REPRESENTATION_BYTES,
        allow_empty=False,
    )
    if not isinstance(direct_answer_eligible, bool):
        raise InvalidRequestError("retrieval owner direct_answer_eligible must be a boolean")
    result: RetrievalOwner = {
        "statement_id": normalized_statement_id,
        "generation": normalized_generation,
        "provenance": provenance,
        "representation": normalized_representation,
        "direct_answer_eligible": direct_answer_eligible,
    }
    return result


def validate_retrieval_owner(value: object) -> RetrievalOwner:
    """Revalidate and copy one retrieval owner."""
    data = _exact_mapping(value, "RetrievalOwner", RETRIEVAL_OWNER_FIELDS)
    result = retrieval_owner(
        data["statement_id"],
        data["generation"],
        data["provenance"],
        data["representation"],
        data["direct_answer_eligible"],
    )
    return result


def retrieval_owner_signature(value: object) -> RetrievalOwnerSignature:
    """Return the explicit immutable ordering signature for one owner."""
    owner = validate_retrieval_owner(value)
    result = _trusted_retrieval_owner_signature(owner)
    return result


def _trusted_retrieval_owner_signature(owner: RetrievalOwner) -> RetrievalOwnerSignature:
    """Return an ordering signature for an index-builder-owned retrieval owner."""
    result = (
        owner["statement_id"],
        owner["generation"],
        owner["provenance"].value,
        owner["representation"],
        owner["direct_answer_eligible"],
    )
    return result


def retrieval_owner_to_dict(value: object) -> dict[str, object]:
    """Serialize one retrieval owner."""
    owner = validate_retrieval_owner(value)
    result: dict[str, object] = dict(owner)
    result["provenance"] = owner["provenance"].value
    return result


IndexCollisionReport = dict


def index_collision_report(key: object, statement_ids: object, truncated: object) -> IndexCollisionReport:
    """Build one bounded group of eligible artifacts that own the same key."""
    try:
        normalized_key = validate_scoped_retrieval_key(key)
    except IdentityValidationError as error:
        raise InvalidRequestError("index collision key must be a ScopedRetrievalKey") from error
    if not isinstance(statement_ids, tuple):
        raise InvalidRequestError("index collision statement_ids must be a tuple")
    if len(statement_ids) < 2:
        raise InvalidRequestError("index collision must identify at least two statements")
    if len(statement_ids) > MAX_INDEX_LOOKUP_OWNERS:
        raise InvalidRequestError(f"index collision statements exceed the limit of {MAX_INDEX_LOOKUP_OWNERS}")
    normalized_ids = tuple(
        sorted(
            {
                _bounded_text(
                    statement_id,
                    "index collision statement_id",
                    MAX_INDEX_STATEMENT_ID_BYTES,
                    allow_empty=False,
                )
                for statement_id in statement_ids
            }
        )
    )
    if len(normalized_ids) != len(statement_ids):
        raise InvalidRequestError("index collision statement_ids must be unique")
    if not isinstance(truncated, bool):
        raise InvalidRequestError("index collision truncated must be a boolean")
    result: IndexCollisionReport = {"key": normalized_key, "statement_ids": normalized_ids, "truncated": truncated}
    return result


def validate_index_collision_report(value: object) -> IndexCollisionReport:
    """Revalidate and copy one index collision report."""
    data = _exact_mapping(value, "IndexCollisionReport", INDEX_COLLISION_REPORT_FIELDS)
    result = index_collision_report(data["key"], data["statement_ids"], data["truncated"])
    return result


def index_collision_report_to_dict(value: object) -> dict[str, object]:
    """Serialize one index collision report."""
    collision = validate_index_collision_report(value)
    result = {
        "key": scoped_retrieval_key_to_dict(collision["key"]),
        "statement_ids": list(collision["statement_ids"]),
        "truncated": collision["truncated"],
    }
    return result


IndexBuildIssue = dict

IndexBuildIssueSignature = tuple[str, str, int, str, bool]


def _index_build_issue_from_validated(
    reason: IndexIssueReason,
    statement_id: str,
    position: int,
    detail: str,
    input_only: bool = False,
) -> IndexBuildIssue:
    """Assemble one issue whose values were validated by the index builder."""
    result: IndexBuildIssue = {
        "reason": reason,
        "statement_id": statement_id,
        "position": position,
        "detail": detail,
        "input_only": input_only,
    }
    return result


def _index_build_issue_signature_from_validated(issue: IndexBuildIssue) -> IndexBuildIssueSignature:
    result = (issue["reason"].value, issue["statement_id"], issue["position"], issue["detail"], issue["input_only"])
    return result


def index_build_issue(
    reason: object,
    statement_id: object,
    position: object,
    detail: object,
    input_only: object = False,
) -> IndexBuildIssue:
    """Build one deterministic index-build classification."""
    if not isinstance(reason, IndexIssueReason):
        raise InvalidRequestError("index build issue reason must be an IndexIssueReason")
    normalized_statement_id = _bounded_text(
        statement_id,
        "index build issue statement_id",
        MAX_INDEX_STATEMENT_ID_BYTES,
        allow_empty=True,
    )
    normalized_position = _nonnegative_integer(position, "index build issue position")
    normalized_detail = _bounded_text(
        detail,
        "index build issue detail",
        MAX_INDEX_REPORT_DETAIL_BYTES,
        allow_empty=True,
    )
    if not isinstance(input_only, bool):
        raise InvalidRequestError("index build issue input_only must be a boolean")
    result: IndexBuildIssue = {
        "reason": reason,
        "statement_id": normalized_statement_id,
        "position": normalized_position,
        "detail": normalized_detail,
        "input_only": input_only,
    }
    return result


def validate_index_build_issue(value: object) -> IndexBuildIssue:
    """Revalidate and copy one index build issue."""
    data = _exact_mapping(value, "IndexBuildIssue", INDEX_BUILD_ISSUE_FIELDS)
    result = index_build_issue(data["reason"], data["statement_id"], data["position"], data["detail"], data["input_only"])
    return result


def index_build_issue_signature(value: object) -> IndexBuildIssueSignature:
    """Return the former record-order signature explicitly."""
    issue = validate_index_build_issue(value)
    result = (issue["reason"].value, issue["statement_id"], issue["position"], issue["detail"], issue["input_only"])
    return result


def index_build_issue_to_dict(value: object) -> dict[str, object]:
    """Serialize one index build issue."""
    issue = validate_index_build_issue(value)
    result: dict[str, object] = dict(issue)
    result["reason"] = issue["reason"].value
    return result


IndexBuildReport = dict


def _index_build_report_from_validated(
    input_count: int,
    projection_count: int,
    exact_key_count: int,
    support_edge_count: int,
    issues: tuple[IndexBuildIssue, ...],
    collisions: tuple[IndexCollisionReport, ...],
    omitted_issue_count: int,
    omitted_collision_count: int,
) -> IndexBuildReport:
    """Assemble one report from values derived by a complete index operation."""
    result: IndexBuildReport = {
        "input_count": input_count,
        "projection_count": projection_count,
        "exact_key_count": exact_key_count,
        "support_edge_count": support_edge_count,
        "issues": issues,
        "collisions": collisions,
        "omitted_issue_count": omitted_issue_count,
        "omitted_collision_count": omitted_collision_count,
    }
    return result


def index_build_report(
    input_count: object,
    projection_count: object,
    exact_key_count: object,
    support_edge_count: object,
    issues: object,
    collisions: object,
    omitted_issue_count: object,
    omitted_collision_count: object,
) -> IndexBuildReport:
    """Build one bounded summary of deterministic candidate construction."""
    normalized_input_count = _nonnegative_integer(input_count, "index build input_count")
    normalized_projection_count = _nonnegative_integer(projection_count, "index build projection_count")
    if normalized_projection_count > normalized_input_count:
        raise InvalidRequestError("index build projection_count cannot exceed input_count")
    normalized_exact_count = _nonnegative_integer(exact_key_count, "index build exact_key_count")
    normalized_support_count = _nonnegative_integer(support_edge_count, "index build support_edge_count")
    if not isinstance(issues, tuple):
        raise InvalidRequestError("index build issues must be a tuple")
    if len(issues) > MAX_INDEX_REPORT_ITEMS:
        raise InvalidRequestError(f"index build issues exceed the limit of {MAX_INDEX_REPORT_ITEMS}")
    normalized_issues = tuple(sorted((validate_index_build_issue(issue) for issue in issues), key=index_build_issue_signature))
    if not isinstance(collisions, tuple):
        raise InvalidRequestError("index build collisions must be a tuple")
    if len(collisions) > MAX_INDEX_REPORT_ITEMS:
        raise InvalidRequestError(f"index build collisions exceed the limit of {MAX_INDEX_REPORT_ITEMS}")
    normalized_collisions = tuple(
        sorted(
            (validate_index_collision_report(collision) for collision in collisions),
            key=lambda collision: scoped_retrieval_key_signature(collision["key"]),
        )
    )
    omitted_issues = _nonnegative_integer(omitted_issue_count, "index build omitted_issue_count")
    omitted_collisions = _nonnegative_integer(omitted_collision_count, "index build omitted_collision_count")
    result = _index_build_report_from_validated(
        normalized_input_count,
        normalized_projection_count,
        normalized_exact_count,
        normalized_support_count,
        normalized_issues,
        normalized_collisions,
        omitted_issues,
        omitted_collisions,
    )
    return result


def validate_index_build_report(value: object) -> IndexBuildReport:
    """Revalidate and copy one index build report."""
    data = _exact_mapping(value, "IndexBuildReport", INDEX_BUILD_REPORT_FIELDS)
    result = index_build_report(
        data["input_count"],
        data["projection_count"],
        data["exact_key_count"],
        data["support_edge_count"],
        data["issues"],
        data["collisions"],
        data["omitted_issue_count"],
        data["omitted_collision_count"],
    )
    return result


def index_build_report_to_dict(value: object) -> dict[str, object]:
    """Serialize one index build report."""
    report = validate_index_build_report(value)
    result = {
        "input_count": report["input_count"],
        "projection_count": report["projection_count"],
        "exact_key_count": report["exact_key_count"],
        "support_edge_count": report["support_edge_count"],
        "issues": [index_build_issue_to_dict(issue) for issue in report["issues"]],
        "collisions": [index_collision_report_to_dict(collision) for collision in report["collisions"]],
        "omitted_issue_count": report["omitted_issue_count"],
        "omitted_collision_count": report["omitted_collision_count"],
    }
    return result


ExactLookupResult = dict


def exact_lookup_result(
    outcome: object,
    key: object,
    statement_id: object,
    generation: object,
    provenance: object,
    representation: object,
    owner_statement_ids: object,
    truncated: object,
) -> ExactLookupResult:
    """Build one exact lookup result with no implicit collision winner."""
    if not isinstance(outcome, ExactLookupOutcome):
        raise InvalidRequestError("exact lookup outcome must be an ExactLookupOutcome")
    try:
        normalized_key = validate_scoped_retrieval_key(key)
    except IdentityValidationError as error:
        raise InvalidRequestError("exact lookup key must be a ScopedRetrievalKey") from error
    selected = outcome == ExactLookupOutcome.FOUND
    normalized_statement_id = _bounded_text(
        statement_id,
        "exact lookup statement_id",
        MAX_INDEX_STATEMENT_ID_BYTES,
        allow_empty=not selected,
    )
    if selected:
        normalized_generation = _positive_int(generation, "exact lookup generation")
    else:
        normalized_generation = _nonnegative_integer(generation, "exact lookup generation")
        if normalized_generation:
            raise InvalidRequestError("non-found exact lookup generation must be 0")
        if normalized_statement_id:
            raise InvalidRequestError("non-found exact lookup statement_id must be empty")
    normalized_provenance = _bounded_text(
        provenance,
        "exact lookup provenance",
        MAX_INDEX_PROVENANCE_BYTES,
        allow_empty=not selected,
    )
    normalized_representation = _bounded_text(
        representation,
        "exact lookup representation",
        MAX_INDEX_REPRESENTATION_BYTES,
        allow_empty=not selected,
    )
    if selected:
        try:
            RetrievalOrigin(normalized_provenance)
        except ValueError as error:
            raise InvalidRequestError("exact lookup provenance must be a RetrievalOrigin value") from error
    elif normalized_provenance or normalized_representation:
        raise InvalidRequestError("non-found exact lookup selected fields must be empty")
    if not isinstance(owner_statement_ids, tuple):
        raise InvalidRequestError("exact lookup owner_statement_ids must be a tuple")
    if len(owner_statement_ids) > MAX_INDEX_LOOKUP_OWNERS:
        raise InvalidRequestError(f"exact lookup owners exceed the limit of {MAX_INDEX_LOOKUP_OWNERS}")
    normalized_owners = tuple(
        _bounded_text(owner, "exact lookup owner statement_id", MAX_INDEX_STATEMENT_ID_BYTES, allow_empty=False)
        for owner in owner_statement_ids
    )
    if len(set(normalized_owners)) != len(normalized_owners):
        raise InvalidRequestError("exact lookup owner_statement_ids must be unique")
    if not isinstance(truncated, bool):
        raise InvalidRequestError("exact lookup truncated must be a boolean")
    if selected and not truncated and normalized_statement_id not in normalized_owners:
        raise InvalidRequestError("found exact lookup owner_statement_ids must include the selected statement")
    result: ExactLookupResult = {
        "outcome": outcome,
        "key": normalized_key,
        "statement_id": normalized_statement_id,
        "generation": normalized_generation,
        "provenance": normalized_provenance,
        "representation": normalized_representation,
        "owner_statement_ids": normalized_owners,
        "truncated": truncated,
    }
    return result


def validate_exact_lookup_result(value: object) -> ExactLookupResult:
    """Revalidate and copy one exact lookup result."""
    data = _exact_mapping(value, "ExactLookupResult", EXACT_LOOKUP_RESULT_FIELDS)
    result = exact_lookup_result(
        data["outcome"],
        data["key"],
        data["statement_id"],
        data["generation"],
        data["provenance"],
        data["representation"],
        data["owner_statement_ids"],
        data["truncated"],
    )
    return result


def exact_lookup_result_to_dict(value: object) -> dict[str, object]:
    """Serialize one exact lookup result."""
    lookup = validate_exact_lookup_result(value)
    result = {
        "outcome": lookup["outcome"].value,
        "key": scoped_retrieval_key_to_dict(lookup["key"]),
        "statement_id": lookup["statement_id"],
        "generation": lookup["generation"],
        "provenance": lookup["provenance"],
        "representation": lookup["representation"],
        "owner_statement_ids": list(lookup["owner_statement_ids"]),
        "truncated": lookup["truncated"],
    }
    return result


SupportMatch = dict


def _support_claim_ids(value: object, name: str, *, allow_empty: bool) -> tuple[str, ...]:
    if not isinstance(value, tuple):
        raise InvalidRequestError(f"{name} must be a tuple")
    if len(value) > MAX_INDEX_SUPPORT_IDS:
        raise InvalidRequestError(f"{name} exceed the limit of {MAX_INDEX_SUPPORT_IDS}")
    result = tuple(sorted({_bounded_text(claim_id, name, MAX_SUPPORT_CLAIM_ID_BYTES, allow_empty=False) for claim_id in value}))
    if not allow_empty and not result:
        raise InvalidRequestError(f"{name} must not be empty")
    return result


def support_match(statement_id: object, matched_claim_ids: object) -> SupportMatch:
    """Build one statement plus the queried Claims that support it."""
    normalized_statement_id = _bounded_text(
        statement_id,
        "support match statement_id",
        MAX_INDEX_STATEMENT_ID_BYTES,
        allow_empty=False,
    )
    normalized_claim_ids = _support_claim_ids(matched_claim_ids, "support match Claim IDs", allow_empty=False)
    result: SupportMatch = {
        "statement_id": normalized_statement_id,
        "matched_claim_ids": normalized_claim_ids,
    }
    return result


def validate_support_match(value: object) -> SupportMatch:
    """Revalidate and copy one support match."""
    data = _exact_mapping(value, "SupportMatch", SUPPORT_MATCH_FIELDS)
    result = support_match(data["statement_id"], data["matched_claim_ids"])
    return result


def support_match_to_dict(value: object) -> dict[str, object]:
    """Serialize one support match."""
    match = validate_support_match(value)
    result = {
        "statement_id": match["statement_id"],
        "matched_claim_ids": list(match["matched_claim_ids"]),
    }
    return result


SupportLookupResult = dict


def _nonnegative_integer(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise InvalidRequestError(f"{name} must be a nonnegative integer")
    return value


def support_lookup_result(
    queried_claim_ids: object,
    matches: object,
    omitted_match_count: object,
    omitted_edge_count: object,
    scanned_edge_count: object,
    complete: object,
    reason: object,
) -> SupportLookupResult:
    """Build one bounded support lookup result."""
    normalized_queried = _support_claim_ids(queried_claim_ids, "support lookup queried Claim IDs", allow_empty=True)
    if not isinstance(matches, tuple):
        raise InvalidRequestError("support lookup matches must be a tuple")
    if len(matches) > MAX_INDEX_LOOKUP_OWNERS:
        raise InvalidRequestError(f"support lookup matches exceed the limit of {MAX_INDEX_LOOKUP_OWNERS}")
    normalized_matches = tuple(validate_support_match(match) for match in matches)
    if tuple(match["statement_id"] for match in normalized_matches) != tuple(
        sorted(match["statement_id"] for match in normalized_matches)
    ):
        raise InvalidRequestError("support lookup matches must use deterministic statement order")
    if len({match["statement_id"] for match in normalized_matches}) != len(normalized_matches):
        raise InvalidRequestError("support lookup matches must have unique statement IDs")
    queried_set = set(normalized_queried)
    if any(not set(match["matched_claim_ids"]).issubset(queried_set) for match in normalized_matches):
        raise InvalidRequestError("support lookup matched Claim IDs must be queried")
    omitted_matches = _nonnegative_integer(omitted_match_count, "support lookup omitted_match_count")
    omitted_edges = _nonnegative_integer(omitted_edge_count, "support lookup omitted_edge_count")
    scanned_edges = _nonnegative_integer(scanned_edge_count, "support lookup scanned_edge_count")
    if not isinstance(complete, bool):
        raise InvalidRequestError("support lookup complete must be a boolean")
    normalized_reason = _bounded_text(
        reason,
        "support lookup reason",
        MAX_INDEX_SUPPORT_REASON_BYTES,
        allow_empty=complete,
    )
    if complete:
        if omitted_edges or normalized_reason:
            raise InvalidRequestError("complete support lookup cannot omit edges or carry a reason")
    elif normalized_matches or omitted_matches or scanned_edges or normalized_reason != INDEX_SUPPORT_SCAN_LIMIT_REASON:
        raise InvalidRequestError("incomplete support lookup must abstain with the scan-limit reason")
    result: SupportLookupResult = {
        "queried_claim_ids": normalized_queried,
        "matches": normalized_matches,
        "omitted_match_count": omitted_matches,
        "omitted_edge_count": omitted_edges,
        "scanned_edge_count": scanned_edges,
        "complete": complete,
        "reason": normalized_reason,
    }
    return result


def validate_support_lookup_result(value: object) -> SupportLookupResult:
    """Revalidate and copy one support lookup result."""
    data = _exact_mapping(value, "SupportLookupResult", SUPPORT_LOOKUP_RESULT_FIELDS)
    result = support_lookup_result(
        data["queried_claim_ids"],
        data["matches"],
        data["omitted_match_count"],
        data["omitted_edge_count"],
        data["scanned_edge_count"],
        data["complete"],
        data["reason"],
    )
    return result


def support_lookup_result_to_dict(value: object) -> dict[str, object]:
    """Serialize one support lookup result."""
    lookup = validate_support_lookup_result(value)
    result = {
        "queried_claim_ids": list(lookup["queried_claim_ids"]),
        "matches": [support_match_to_dict(match) for match in lookup["matches"]],
        "omitted_match_count": lookup["omitted_match_count"],
        "omitted_edge_count": lookup["omitted_edge_count"],
        "scanned_edge_count": lookup["scanned_edge_count"],
        "complete": lookup["complete"],
        "reason": lookup["reason"],
    }
    return result


SupportScanPlan = dict


def support_scan_plan(
    queried_claim_ids: object,
    edge_count: object,
    scan_limit: object,
    complete: object,
    reason: object,
) -> SupportScanPlan:
    """Build one validated support traversal plan."""
    normalized_queried = _support_claim_ids(queried_claim_ids, "support scan queried Claim IDs", allow_empty=True)
    normalized_edges = _nonnegative_integer(edge_count, "support scan edge_count")
    normalized_limit = _positive_int(scan_limit, "support scan limit")
    if not isinstance(complete, bool):
        raise InvalidRequestError("support scan complete must be a boolean")
    expected_complete = normalized_edges <= normalized_limit
    if complete != expected_complete:
        raise InvalidRequestError("support scan complete must agree with edge_count and scan_limit")
    normalized_reason = _bounded_text(
        reason,
        "support scan reason",
        MAX_INDEX_SUPPORT_REASON_BYTES,
        allow_empty=complete,
    )
    expected_reason = "" if complete else INDEX_SUPPORT_SCAN_LIMIT_REASON
    if normalized_reason != expected_reason:
        raise InvalidRequestError("support scan reason must agree with completeness")
    result: SupportScanPlan = {
        "queried_claim_ids": normalized_queried,
        "edge_count": normalized_edges,
        "scan_limit": normalized_limit,
        "complete": complete,
        "reason": normalized_reason,
    }
    return result


def validate_support_scan_plan(value: object) -> SupportScanPlan:
    """Revalidate and copy one support scan plan."""
    data = _exact_mapping(value, "SupportScanPlan", SUPPORT_SCAN_PLAN_FIELDS)
    result = support_scan_plan(
        data["queried_claim_ids"],
        data["edge_count"],
        data["scan_limit"],
        data["complete"],
        data["reason"],
    )
    return result


def support_scan_plan_to_dict(value: object) -> dict[str, object]:
    """Serialize one support scan plan."""
    plan = validate_support_scan_plan(value)
    result = {
        "queried_claim_ids": list(plan["queried_claim_ids"]),
        "edge_count": plan["edge_count"],
        "scan_limit": plan["scan_limit"],
        "complete": plan["complete"],
        "reason": plan["reason"],
    }
    return result


IndexCheckIssue = dict

IndexCheckIssueSignature = tuple[str, str, str, tuple[str, ...], tuple[str, ...], bool]


def _check_values(value: object, name: str) -> tuple[str, ...]:
    if not isinstance(value, tuple):
        raise InvalidRequestError(f"{name} must be a tuple")
    if len(value) > MAX_INDEX_REPORT_ITEMS:
        raise InvalidRequestError(f"{name} exceed the limit of {MAX_INDEX_REPORT_ITEMS}")
    result = tuple(_bounded_text(item, name, MAX_INDEX_CHECK_VALUE_BYTES, allow_empty=True) for item in value)
    return result


def index_check_issue(
    category: object,
    index_name: object,
    key: object,
    expected: object,
    actual: object,
    error: object,
) -> IndexCheckIssue:
    """Build one bounded index checker finding."""
    if not isinstance(category, IndexCheckCategory):
        raise InvalidRequestError("index check category must be an IndexCheckCategory")
    normalized_name = _bounded_text(index_name, "index check index_name", MAX_INDEX_CHECK_NAME_BYTES, allow_empty=False)
    normalized_key = _bounded_text(key, "index check key", MAX_INDEX_CHECK_KEY_BYTES, allow_empty=True)
    normalized_expected = _check_values(expected, "index check expected values")
    normalized_actual = _check_values(actual, "index check actual values")
    if not isinstance(error, bool):
        raise InvalidRequestError("index check error must be a boolean")
    result: IndexCheckIssue = {
        "category": category,
        "index_name": normalized_name,
        "key": normalized_key,
        "expected": normalized_expected,
        "actual": normalized_actual,
        "error": error,
    }
    return result


def validate_index_check_issue(value: object) -> IndexCheckIssue:
    """Revalidate and copy one index checker finding."""
    data = _exact_mapping(value, "IndexCheckIssue", INDEX_CHECK_ISSUE_FIELDS)
    result = index_check_issue(
        data["category"],
        data["index_name"],
        data["key"],
        data["expected"],
        data["actual"],
        data["error"],
    )
    return result


def index_check_issue_signature(value: object) -> IndexCheckIssueSignature:
    """Return the former record-order signature explicitly."""
    issue = validate_index_check_issue(value)
    result = (
        issue["category"].value,
        issue["index_name"],
        issue["key"],
        issue["expected"],
        issue["actual"],
        issue["error"],
    )
    return result


def index_check_issue_to_dict(value: object) -> dict[str, object]:
    """Serialize one index checker finding."""
    issue = validate_index_check_issue(value)
    result = {
        "category": issue["category"].value,
        "index_name": issue["index_name"],
        "key": issue["key"],
        "expected": list(issue["expected"]),
        "actual": list(issue["actual"]),
        "error": issue["error"],
    }
    return result


IndexCheckReport = dict


def index_check_report(
    consistent: object,
    checked_state_generation: object,
    issues: object,
    omitted_issue_count: object,
) -> IndexCheckReport:
    """Build one bounded, non-mutating consistency report."""
    if not isinstance(consistent, bool):
        raise InvalidRequestError("index check consistent must be a boolean")
    generation = _positive_int(checked_state_generation, "index check state generation")
    if not isinstance(issues, tuple):
        raise InvalidRequestError("index check issues must be a tuple")
    if len(issues) > MAX_INDEX_REPORT_ITEMS:
        raise InvalidRequestError(f"index check issues exceed the limit of {MAX_INDEX_REPORT_ITEMS}")
    normalized_issues = tuple(sorted((validate_index_check_issue(issue) for issue in issues), key=index_check_issue_signature))
    omitted = _nonnegative_integer(omitted_issue_count, "index check omitted_issue_count")
    retained_error = any(issue["error"] for issue in normalized_issues)
    if consistent and retained_error:
        raise InvalidRequestError("consistent index check cannot retain an error issue")
    if not consistent and not retained_error and not omitted:
        raise InvalidRequestError("inconsistent index check must retain or omit an error issue")
    result: IndexCheckReport = {
        "consistent": consistent,
        "checked_state_generation": generation,
        "issues": normalized_issues,
        "omitted_issue_count": omitted,
    }
    return result


def validate_index_check_report(value: object) -> IndexCheckReport:
    """Revalidate and copy one index check report."""
    data = _exact_mapping(value, "IndexCheckReport", INDEX_CHECK_REPORT_FIELDS)
    result = index_check_report(
        data["consistent"],
        data["checked_state_generation"],
        data["issues"],
        data["omitted_issue_count"],
    )
    return result


def index_check_report_to_dict(value: object) -> dict[str, object]:
    """Serialize one index check report."""
    report = validate_index_check_report(value)
    result = {
        "consistent": report["consistent"],
        "checked_state_generation": report["checked_state_generation"],
        "issues": [index_check_issue_to_dict(issue) for issue in report["issues"]],
        "omitted_issue_count": report["omitted_issue_count"],
    }
    return result


IndexState = dict


def _retrieval_key_signature(value: object) -> ScopedRetrievalKeySignature:
    if not isinstance(value, tuple) or len(value) != 4:
        raise InvalidRequestError("index retrieval key signature must be a four-item tuple")
    scope_signature = value[2]
    if not isinstance(scope_signature, tuple) or len(scope_signature) != 3:
        raise InvalidRequestError("index retrieval key scope signature must be a three-item tuple")
    candidate = {
        "schema_version": value[0],
        "normalization_version": value[1],
        "scope": {
            "schema_version": scope_signature[0],
            "namespace": scope_signature[1],
            "context_fingerprint": scope_signature[2],
        },
        "normalized_key": value[3],
    }
    try:
        result = scoped_retrieval_key_signature(candidate)
    except IdentityValidationError as error:
        raise InvalidRequestError("index retrieval key signature is invalid") from error
    return result


def _index_mapping(value: object, name: str) -> Mapping[object, object]:
    if not isinstance(value, Mapping):
        raise InvalidRequestError(f"{name} must be an object")
    return value


def _frozen_index_state(
    state_generation: int,
    retrieval_to_owners: dict[ScopedRetrievalKeySignature, tuple[RetrievalOwner, ...]],
    statement_to_retrieval: dict[str, tuple[RetrievalKeyBinding, ...]],
    claim_to_statements: dict[str, tuple[str, ...]],
    statement_to_claims: dict[str, tuple[str, ...]],
    direct_retrieval: dict[ScopedRetrievalKeySignature, RetrievalOwner],
    projections: dict[str, IndexProjection],
    build_report: IndexBuildReport,
    normalization_version: int,
    schema_version: int,
) -> IndexState:
    """Freeze already validated values into one exact index-state dictionary."""
    result: IndexState = {
        "state_generation": state_generation,
        "retrieval_to_owners": MappingProxyType(retrieval_to_owners),
        "statement_to_retrieval": MappingProxyType(statement_to_retrieval),
        "claim_to_statements": MappingProxyType(claim_to_statements),
        "statement_to_claims": MappingProxyType(statement_to_claims),
        "direct_retrieval": MappingProxyType(direct_retrieval),
        "projections": MappingProxyType(projections),
        "build_report": build_report,
        "normalization_version": normalization_version,
        "schema_version": schema_version,
    }
    return result


def index_state(
    state_generation: object,
    retrieval_to_owners: object,
    statement_to_retrieval: object,
    claim_to_statements: object,
    statement_to_claims: object,
    direct_retrieval: object,
    projections: object,
    build_report: object,
    normalization_version: object = RETRIEVAL_NORMALIZATION_VERSION,
    schema_version: object = INDEX_STATE_SCHEMA_VERSION,
) -> IndexState:
    """Build one structurally validated immutable index-state dictionary."""
    generation = _positive_int(state_generation, "index state generation")
    version = _positive_int(schema_version, "index state schema_version")
    if version != INDEX_STATE_SCHEMA_VERSION:
        raise UnsupportedIdentityVersionError(f"unsupported index state schema version: {version}")
    normalization = _positive_int(normalization_version, "index state normalization_version")
    if normalization != RETRIEVAL_NORMALIZATION_VERSION:
        raise UnsupportedIdentityVersionError(f"unsupported index state normalization version: {normalization}")

    validated_owner_map = {}
    for raw_signature, owners in _index_mapping(retrieval_to_owners, "index retrieval_to_owners").items():
        key_signature = _retrieval_key_signature(raw_signature)
        if not isinstance(owners, tuple):
            raise InvalidRequestError("index retrieval owners must be tuples")
        validated_owner_map[key_signature] = tuple(validate_retrieval_owner(owner) for owner in owners)

    validated_statement_to_retrieval = {}
    for raw_statement_id, bindings in _index_mapping(
        statement_to_retrieval,
        "index statement_to_retrieval",
    ).items():
        statement_id = _bounded_text(
            raw_statement_id,
            "index statement_to_retrieval statement ID",
            MAX_INDEX_STATEMENT_ID_BYTES,
            allow_empty=False,
        )
        if not isinstance(bindings, tuple):
            raise InvalidRequestError("index statement retrieval bindings must be tuples")
        try:
            validated_bindings = tuple(validate_retrieval_key_binding(binding) for binding in bindings)
        except IdentityValidationError as error:
            raise InvalidRequestError("index statement retrieval bindings are invalid") from error
        validated_statement_to_retrieval[statement_id] = validated_bindings

    validated_claim_to_statements = {}
    for raw_claim_id, statement_ids in _index_mapping(claim_to_statements, "index claim_to_statements").items():
        claim_id = _bounded_text(raw_claim_id, "index support Claim ID", MAX_SUPPORT_CLAIM_ID_BYTES, allow_empty=False)
        if not isinstance(statement_ids, tuple):
            raise InvalidRequestError("index Claim statement IDs must be tuples")
        validated_statement_ids = tuple(
            _bounded_text(
                statement_id,
                "index Claim statement ID",
                MAX_INDEX_STATEMENT_ID_BYTES,
                allow_empty=False,
            )
            for statement_id in statement_ids
        )
        validated_claim_to_statements[claim_id] = validated_statement_ids

    validated_statement_to_claims = {}
    for raw_statement_id, claim_ids in _index_mapping(statement_to_claims, "index statement_to_claims").items():
        statement_id = _bounded_text(
            raw_statement_id,
            "index statement_to_claims statement ID",
            MAX_INDEX_STATEMENT_ID_BYTES,
            allow_empty=False,
        )
        validated_statement_to_claims[statement_id] = _support_claim_ids(
            claim_ids,
            "index statement support Claim IDs",
            allow_empty=True,
        )

    validated_direct = {}
    for raw_signature, owner in _index_mapping(direct_retrieval, "index direct_retrieval").items():
        key_signature = _retrieval_key_signature(raw_signature)
        validated_direct[key_signature] = validate_retrieval_owner(owner)

    validated_projections = {}
    for raw_statement_id, projection in _index_mapping(projections, "index projections").items():
        statement_id = _bounded_text(
            raw_statement_id,
            "index projection map statement ID",
            MAX_INDEX_STATEMENT_ID_BYTES,
            allow_empty=False,
        )
        normalized_projection = validate_index_projection(projection)
        if statement_id != normalized_projection["statement_id"]:
            raise InvalidRequestError("index projection key must match statement_id")
        validated_projections[statement_id] = normalized_projection

    validated_report = validate_index_build_report(build_report)
    result = _frozen_index_state(
        generation,
        validated_owner_map,
        validated_statement_to_retrieval,
        validated_claim_to_statements,
        validated_statement_to_claims,
        validated_direct,
        validated_projections,
        validated_report,
        normalization,
        version,
    )
    return result


def validate_index_state(value: object) -> IndexState:
    """Revalidate and deeply copy one index-state dictionary."""
    data = _exact_mapping(value, "IndexState", INDEX_STATE_FIELDS)
    result = index_state(
        data["state_generation"],
        data["retrieval_to_owners"],
        data["statement_to_retrieval"],
        data["claim_to_statements"],
        data["statement_to_claims"],
        data["direct_retrieval"],
        data["projections"],
        data["build_report"],
        data["normalization_version"],
        data["schema_version"],
    )
    return result


def index_state_exact_lookup(state: IndexState, key: ScopedRetrievalKey) -> ExactLookupResult:
    """Perform one exact lookup against a structurally valid index state."""
    _exact_mapping(state, "IndexState", INDEX_STATE_FIELDS)
    try:
        validated_key = validate_scoped_retrieval_key(key)
    except IdentityValidationError as error:
        raise InvalidRequestError("exact lookup key must be a ScopedRetrievalKey") from error
    result = trusted_index_state_exact_lookup(state, validated_key)
    return result


def trusted_index_state_exact_lookup(state: IndexState, key: ScopedRetrievalKey) -> ExactLookupResult:
    """Look up a key already validated inside an IndexOwner lock."""
    validated_key = key
    key_signature = trusted_scoped_retrieval_key_signature(validated_key)
    owners = state["retrieval_to_owners"].get(key_signature, ())
    owner_ids = tuple(dict.fromkeys(owner["statement_id"] for owner in owners))
    bounded_ids = owner_ids[:MAX_INDEX_LOOKUP_OWNERS]
    truncated = len(owner_ids) > len(bounded_ids)
    selected = state["direct_retrieval"].get(key_signature)
    if selected:
        result = exact_lookup_result(
            ExactLookupOutcome.FOUND,
            validated_key,
            selected["statement_id"],
            selected["generation"],
            selected["provenance"].value,
            selected["representation"],
            bounded_ids,
            truncated,
        )
        return result
    eligible_ids = tuple(dict.fromkeys(owner["statement_id"] for owner in owners if owner["direct_answer_eligible"]))
    outcome = ExactLookupOutcome.COLLISION if len(eligible_ids) > 1 else ExactLookupOutcome.MISS
    result = exact_lookup_result(
        outcome,
        validated_key,
        "",
        0,
        "",
        "",
        bounded_ids,
        truncated,
    )
    return result


def index_state_support_scan_plan(
    state: IndexState,
    claim_ids: tuple[str, ...],
    scan_limit: int = MAX_INDEX_SUPPORT_SCAN_EDGES,
) -> SupportScanPlan:
    """Validate one bounded support traversal before materializing fan-out."""
    _exact_mapping(state, "IndexState", INDEX_STATE_FIELDS)
    if not isinstance(claim_ids, tuple):
        raise InvalidRequestError("support lookup Claim IDs must be a tuple")
    if isinstance(scan_limit, bool) or not isinstance(scan_limit, int) or scan_limit < 1:
        raise InvalidRequestError("support scan limit must be a positive integer")
    queried = tuple(
        sorted(
            {
                _bounded_text(claim_id, "support lookup Claim ID", MAX_SUPPORT_CLAIM_ID_BYTES, allow_empty=False)
                for claim_id in claim_ids
            }
        )
    )
    if len(queried) > MAX_INDEX_SUPPORT_IDS:
        raise InvalidRequestError(f"support lookup Claim IDs exceed the limit of {MAX_INDEX_SUPPORT_IDS}")
    edge_count = sum(len(state["claim_to_statements"].get(claim_id, ())) for claim_id in queried)
    complete = edge_count <= scan_limit
    reason = "" if complete else INDEX_SUPPORT_SCAN_LIMIT_REASON
    result = support_scan_plan(
        queried,
        edge_count,
        scan_limit,
        complete,
        reason,
    )
    return result


def index_state_support_lookup(
    state: IndexState,
    claim_ids: tuple[str, ...],
    scan_limit: int = MAX_INDEX_SUPPORT_SCAN_EDGES,
) -> SupportLookupResult:
    """Return bounded statement matches for one support-Claim set."""
    plan = index_state_support_scan_plan(state, claim_ids, scan_limit)
    if not plan["complete"]:
        result = support_lookup_result(
            plan["queried_claim_ids"],
            (),
            0,
            plan["edge_count"],
            0,
            False,
            plan["reason"],
        )
        return result
    matched_by_statement: dict[str, set[str]] = {}
    for claim_id in plan["queried_claim_ids"]:
        for statement_id in state["claim_to_statements"].get(claim_id, ()):
            matched_by_statement.setdefault(statement_id, set()).add(claim_id)
    all_matches = tuple(
        support_match(statement_id, tuple(sorted(matched_by_statement[statement_id])))
        for statement_id in sorted(matched_by_statement)
    )
    matches = all_matches[:MAX_INDEX_LOOKUP_OWNERS]
    result = support_lookup_result(
        plan["queried_claim_ids"],
        matches,
        len(all_matches) - len(matches),
        0,
        plan["edge_count"],
        True,
        "",
    )
    return result


IndexRepairResult = dict


def index_repair_result(
    applied: object,
    changed: object,
    before_generation: object,
    after_generation: object,
    candidate_report: object,
    live_check: object,
) -> IndexRepairResult:
    """Build one transport-neutral index repair result."""
    if not isinstance(applied, bool):
        raise InvalidRequestError("index repair applied must be a boolean")
    if not isinstance(changed, bool):
        raise InvalidRequestError("index repair changed must be a boolean")
    before = _positive_int(before_generation, "index repair before_generation")
    after = _positive_int(after_generation, "index repair after_generation")
    expected_after = before + 1 if applied else before
    if after != expected_after:
        raise InvalidRequestError("index repair after_generation must agree with applied")
    normalized_candidate = validate_index_build_report(candidate_report)
    normalized_check = validate_index_check_report(live_check)
    result: IndexRepairResult = {
        "applied": applied,
        "changed": changed,
        "before_generation": before,
        "after_generation": after,
        "candidate_report": normalized_candidate,
        "live_check": normalized_check,
    }
    return result


def validate_index_repair_result(value: object) -> IndexRepairResult:
    """Revalidate and copy one index repair result."""
    data = _exact_mapping(value, "IndexRepairResult", INDEX_REPAIR_RESULT_FIELDS)
    result = index_repair_result(
        data["applied"],
        data["changed"],
        data["before_generation"],
        data["after_generation"],
        data["candidate_report"],
        data["live_check"],
    )
    return result


def index_repair_result_to_dict(value: object) -> dict[str, object]:
    """Serialize one index repair result."""
    repair = validate_index_repair_result(value)
    result = {
        "applied": repair["applied"],
        "changed": repair["changed"],
        "before_generation": repair["before_generation"],
        "after_generation": repair["after_generation"],
        "candidate_report": index_build_report_to_dict(repair["candidate_report"]),
        "live_check": index_check_report_to_dict(repair["live_check"]),
    }
    return result


def _bounded_issues(issues: list[IndexBuildIssue]) -> tuple[tuple[IndexBuildIssue, ...], int]:
    ordered = tuple(sorted(issues, key=index_build_issue_signature))
    bounded = ordered[:MAX_INDEX_REPORT_ITEMS]
    result = (bounded, len(ordered) - len(bounded))
    return result


def _bounded_valid_issues(issues: list[IndexBuildIssue]) -> tuple[tuple[IndexBuildIssue, ...], int]:
    ordered = tuple(sorted(issues, key=_index_build_issue_signature_from_validated))
    bounded = ordered[:MAX_INDEX_REPORT_ITEMS]
    result = (bounded, len(ordered) - len(bounded))
    return result


def _raw_statement_id(value: object) -> str:
    if not isinstance(value, Mapping):
        result = ""
        return result
    statement_id = value.get("statement_id", "")
    if not isinstance(statement_id, str):
        result = ""
        return result
    result = statement_id[:MAX_INDEX_STATEMENT_ID_BYTES]
    return result


def _projection_from_input(value: object) -> IndexProjection:
    if isinstance(value, Mapping):
        try:
            result = validate_index_projection(value)
        except (InvalidRequestError, UnsupportedIdentityVersionError):
            result = index_projection_from_dict(value)
        return result
    raise InvalidRequestError("index projection input must be an IndexProjection or object")


def _input_error_reason(value: object, error: Exception) -> IndexIssueReason:
    message = str(error).lower()
    if isinstance(error, UnsupportedIdentityVersionError):
        if "normalization" in message:
            result = IndexIssueReason.UNSUPPORTED_NORMALIZATION_VERSION
            return result
        result = IndexIssueReason.UNSUPPORTED_SCHEMA_VERSION
        return result
    if "support" in message:
        result = IndexIssueReason.MALFORMED_SUPPORT
        return result
    result = IndexIssueReason.MALFORMED_PROJECTION
    return result


def _deduplicate_bindings(
    projection: IndexProjection,
    position: int,
    issues: list[IndexBuildIssue],
) -> tuple[RetrievalKeyBinding, ...]:
    by_key: dict[ScopedRetrievalKeySignature, RetrievalKeyBinding] = {}
    duplicates: set[ScopedRetrievalKeySignature] = set()
    for binding in projection["retrieval_keys"]:
        key_signature = trusted_scoped_retrieval_key_signature(binding["key"])
        current = by_key.get(key_signature)
        if current:
            duplicates.add(key_signature)
            if current["origin"] == RetrievalOrigin.ALIAS and binding["origin"] == RetrievalOrigin.CANONICAL:
                by_key[key_signature] = binding
        else:
            by_key[key_signature] = binding
    for key_signature in sorted(duplicates):
        issues.append(
            _index_build_issue_from_validated(
                IndexIssueReason.WITHIN_ARTIFACT_DUPLICATE,
                projection["statement_id"],
                position,
                _key_text(by_key[key_signature]["key"])[:MAX_INDEX_REPORT_DETAIL_BYTES],
            )
        )
    result = tuple(by_key[key_signature] for key_signature in sorted(by_key))
    return result


def build_index_state(projections: Iterable[object], state_generation: int = 1) -> IndexState:
    """Build one deterministic candidate state without changing live state."""
    _positive_int(state_generation, "index state generation")
    inputs = tuple(projections)
    valid_with_position: list[tuple[int, IndexProjection]] = []
    issues: list[IndexBuildIssue] = []
    for position, raw_projection in enumerate(inputs):
        try:
            valid_with_position.append((position, _projection_from_input(raw_projection)))
        except (IdentityValidationError, InvalidRequestError) as error:
            issues.append(
                index_build_issue(
                    reason=_input_error_reason(raw_projection, error),
                    statement_id=_raw_statement_id(raw_projection),
                    position=position,
                    detail=str(error)[:MAX_INDEX_REPORT_DETAIL_BYTES],
                    input_only=True,
                )
            )

    grouped: dict[str, list[tuple[int, IndexProjection]]] = {}
    for position, projection in valid_with_position:
        grouped.setdefault(projection["statement_id"], []).append((position, projection))
    accepted: list[tuple[int, IndexProjection]] = []
    for statement_id in sorted(grouped):
        entries = grouped[statement_id]
        unique = {_trusted_index_projection_to_json(projection): projection for _, projection in entries}
        if len(unique) > 1:
            for position, _ in entries:
                issues.append(
                    index_build_issue(
                        reason=IndexIssueReason.DUPLICATE_STATEMENT_ID,
                        statement_id=statement_id,
                        position=position,
                        detail="conflicting projections share one statement ID",
                        input_only=True,
                    )
                )
            continue
        accepted.append(min(entries, key=lambda entry: entry[0]))

    retrieval_work: dict[ScopedRetrievalKeySignature, list[RetrievalOwner]] = {}
    retrieval_keys: dict[ScopedRetrievalKeySignature, ScopedRetrievalKey] = {}
    statement_to_retrieval: dict[str, tuple[RetrievalKeyBinding, ...]] = {}
    claim_work: dict[str, set[str]] = {}
    statement_to_claims: dict[str, tuple[str, ...]] = {}
    projection_map: dict[str, IndexProjection] = {}

    for position, projection in sorted(accepted, key=lambda entry: entry[1]["statement_id"]):
        bindings = _deduplicate_bindings(projection, position, issues)
        projection_map[projection["statement_id"]] = projection
        statement_to_retrieval[projection["statement_id"]] = bindings
        statement_to_claims[projection["statement_id"]] = projection["support_claim_ids"]
        for claim_id in projection["support_claim_ids"]:
            claim_work.setdefault(claim_id, set()).add(projection["statement_id"])
        if not bindings:
            issues.append(
                index_build_issue(
                    reason=IndexIssueReason.MISSING_IDENTITY,
                    statement_id=projection["statement_id"],
                    position=position,
                    detail="projection carries no exact retrieval key",
                )
            )
        eligible = projection["direct_answer_eligible"] and not projection["exclusion_reason"] and bool(bindings)
        if not eligible:
            reason = projection["exclusion_reason"] or IndexIssueReason.INELIGIBLE.value
            if reason != IndexIssueReason.MISSING_IDENTITY.value or bindings:
                classified_reason = IndexIssueReason.INELIGIBLE
                try:
                    requested_reason = IndexIssueReason(reason)
                    if requested_reason in {
                        IndexIssueReason.MALFORMED_SUPPORT,
                        IndexIssueReason.MISSING_IDENTITY,
                    }:
                        classified_reason = requested_reason
                except ValueError:
                    pass
                issues.append(
                    index_build_issue(
                        reason=classified_reason,
                        statement_id=projection["statement_id"],
                        position=position,
                        detail=reason[:MAX_INDEX_REPORT_DETAIL_BYTES],
                    )
                )
        for binding in bindings:
            key_signature = trusted_scoped_retrieval_key_signature(binding["key"])
            retrieval_keys[key_signature] = binding["key"]
            retrieval_work.setdefault(key_signature, []).append(
                retrieval_owner(
                    projection["statement_id"],
                    projection["generation"],
                    binding["origin"],
                    binding["representation"],
                    eligible,
                )
            )

    retrieval_to_owners: dict[ScopedRetrievalKeySignature, tuple[RetrievalOwner, ...]] = {}
    direct_retrieval: dict[ScopedRetrievalKeySignature, RetrievalOwner] = {}
    collisions = []
    for key_signature in sorted(retrieval_work):
        key = retrieval_keys[key_signature]
        owners = tuple(sorted(retrieval_work[key_signature], key=_trusted_retrieval_owner_signature))
        retrieval_to_owners[key_signature] = owners
        eligible_owners = tuple(owner for owner in owners if owner["direct_answer_eligible"])
        eligible_statement_ids = tuple(dict.fromkeys(owner["statement_id"] for owner in eligible_owners))
        if len(eligible_statement_ids) == 1:
            direct_retrieval[key_signature] = eligible_owners[0]
        elif len(eligible_statement_ids) > 1:
            bounded_ids = eligible_statement_ids[:MAX_INDEX_LOOKUP_OWNERS]
            collision = index_collision_report(
                key=key,
                statement_ids=bounded_ids,
                truncated=len(eligible_statement_ids) > len(bounded_ids),
            )
            collisions.append(collision)
            for statement_id in bounded_ids:
                issues.append(
                    index_build_issue(
                        reason=IndexIssueReason.CROSS_ARTIFACT_COLLISION,
                        statement_id=statement_id,
                        position=0,
                        detail=_key_text(key)[:MAX_INDEX_REPORT_DETAIL_BYTES],
                    )
                )

    claim_to_statements = {claim_id: tuple(sorted(statement_ids)) for claim_id, statement_ids in sorted(claim_work.items())}
    bounded_issues, omitted_issues = _bounded_issues(issues)
    ordered_collisions = tuple(sorted(collisions, key=lambda collision: scoped_retrieval_key_signature(collision["key"])))
    bounded_collisions = ordered_collisions[:MAX_INDEX_REPORT_ITEMS]
    report = index_build_report(
        input_count=len(inputs),
        projection_count=len(projection_map),
        exact_key_count=len(retrieval_to_owners),
        support_edge_count=sum(len(statement_ids) for statement_ids in claim_to_statements.values()),
        issues=bounded_issues,
        collisions=bounded_collisions,
        omitted_issue_count=omitted_issues,
        omitted_collision_count=len(ordered_collisions) - len(bounded_collisions),
    )
    result = _frozen_index_state(
        state_generation,
        retrieval_to_owners,
        statement_to_retrieval,
        claim_to_statements,
        statement_to_claims,
        direct_retrieval,
        projection_map,
        report,
        RETRIEVAL_NORMALIZATION_VERSION,
        INDEX_STATE_SCHEMA_VERSION,
    )
    return result


def projection_from_statement(statement: dict) -> IndexProjection:
    """Project current statement support without inventing exact identity."""
    if not isinstance(statement, dict):
        raise InvalidRequestError("statement projection source must be an object")
    statement_id = _bounded_text(statement.get("id", ""), "statement id", MAX_INDEX_STATEMENT_ID_BYTES, allow_empty=False)
    malformed_support = False
    support_ids: tuple[str, ...] = ()
    template = statement.get("template", {})
    if not isinstance(template, dict):
        malformed_support = True
    else:
        metadata = template.get("tapestry", {})
        if not isinstance(metadata, dict):
            malformed_support = True
        else:
            raw_support = metadata.get("support", [])
            if not isinstance(raw_support, list):
                malformed_support = True
            else:
                support = []
                for reference in raw_support:
                    if not isinstance(reference, dict):
                        malformed_support = True
                        break
                    claim_id = reference.get("claim_id", "")
                    try:
                        support.append(_bounded_text(claim_id, "support Claim ID", MAX_SUPPORT_CLAIM_ID_BYTES, allow_empty=False))
                    except InvalidRequestError:
                        malformed_support = True
                        break
                if not malformed_support:
                    support_ids = tuple(support)
    reason = IndexIssueReason.MALFORMED_SUPPORT.value if malformed_support else IndexIssueReason.MISSING_IDENTITY.value
    result = index_projection(
        statement_id,
        1,
        (),
        support_ids,
        False,
        reason,
    )
    return result


def _value_tuple(value: object) -> tuple[str, ...]:
    if isinstance(value, tuple):
        result = tuple(str(item) for item in value)
        return result
    if isinstance(value, Mapping) and set(value) == RETRIEVAL_OWNER_FIELDS:
        try:
            owner = validate_retrieval_owner(value)
        except InvalidRequestError:
            result = (str(value),)
            return result
        result = (owner["statement_id"], owner["provenance"].value, str(owner["generation"]))
        return result
    if isinstance(value, Mapping) and set(value) == INDEX_PROJECTION_FIELDS:
        try:
            projection = validate_index_projection(value)
        except (InvalidRequestError, UnsupportedIdentityVersionError):
            result = (str(value),)
            return result
        result = (index_projection_to_json(projection),)
        return result
    result = (str(value),)
    return result


def _compare_maps[MapKey, MapValue](
    index_name: str,
    expected: Mapping[MapKey, MapValue],
    actual: Mapping[MapKey, MapValue],
    issues: list[IndexCheckIssue],
) -> None:
    all_keys = sorted(set(expected) | set(actual), key=str)
    for key in all_keys:
        key_text = str(key)
        if key not in actual:
            issues.append(
                index_check_issue(IndexCheckCategory.MISSING, index_name, key_text, _value_tuple(expected[key]), (), True)
            )
        elif key not in expected:
            issues.append(index_check_issue(IndexCheckCategory.EXTRA, index_name, key_text, (), _value_tuple(actual[key]), True))
        elif expected[key] != actual[key]:
            issues.append(
                index_check_issue(
                    IndexCheckCategory.ASYMMETRIC,
                    index_name,
                    key_text,
                    _value_tuple(expected[key]),
                    _value_tuple(actual[key]),
                    True,
                )
            )


def _report_issue_signature(issue: IndexBuildIssue) -> str:
    data = {"detail": issue["detail"], "reason": issue["reason"].value, "statement_id": issue["statement_id"]}
    result = _json_text(data)
    return result


def _append_report_mismatch(
    issues: list[IndexCheckIssue],
    key: str,
    expected: tuple[str, ...],
    actual: tuple[str, ...],
) -> None:
    if expected != actual:
        issues.append(index_check_issue(IndexCheckCategory.ASYMMETRIC, "build_report", key, expected, actual, True))


def _check_build_report(state: IndexState, expected: IndexState, issues: list[IndexCheckIssue]) -> None:
    """Validate every diagnostic that can be rebuilt from retained projections."""
    actual_report = state["build_report"]
    expected_report = expected["build_report"]
    for field_name in ("projection_count", "exact_key_count", "support_edge_count"):
        _append_report_mismatch(
            issues,
            field_name,
            (str(expected_report[field_name]),),
            (str(actual_report[field_name]),),
        )
    expected_collisions = tuple(
        _json_text(index_collision_report_to_dict(collision)) for collision in expected_report["collisions"]
    )
    actual_collisions = tuple(_json_text(index_collision_report_to_dict(collision)) for collision in actual_report["collisions"])
    _append_report_mismatch(issues, "collisions", expected_collisions, actual_collisions)
    _append_report_mismatch(
        issues,
        "omitted_collision_count",
        (str(expected_report["omitted_collision_count"]),),
        (str(actual_report["omitted_collision_count"]),),
    )

    expected_issues = tuple(
        sorted(_report_issue_signature(issue) for issue in expected_report["issues"] if not issue["input_only"])
    )
    actual_issues = tuple(sorted(_report_issue_signature(issue) for issue in actual_report["issues"] if not issue["input_only"]))
    source_input_loss = actual_report["input_count"] != actual_report["projection_count"]
    if not source_input_loss or actual_report["omitted_issue_count"] == 0:
        _append_report_mismatch(issues, "derived_issues", expected_issues, actual_issues)
        if not source_input_loss:
            _append_report_mismatch(
                issues,
                "omitted_issue_count",
                (str(expected_report["omitted_issue_count"]),),
                (str(actual_report["omitted_issue_count"]),),
            )
    elif not set(actual_issues).issubset(set(expected_issues)):
        _append_report_mismatch(issues, "derived_issues", expected_issues, actual_issues)


def _check_index_state_against(state: IndexState, expected_inputs: tuple[object, ...]) -> IndexCheckReport:
    """Compare every map to one explicit clean rebuild without mutation."""
    _exact_mapping(state, "IndexState", INDEX_STATE_FIELDS)
    expected = build_index_state(expected_inputs, state["state_generation"])
    issues: list[IndexCheckIssue] = []
    _compare_maps("retrieval_to_owners", expected["retrieval_to_owners"], state["retrieval_to_owners"], issues)
    _compare_maps("statement_to_retrieval", expected["statement_to_retrieval"], state["statement_to_retrieval"], issues)
    _compare_maps("claim_to_statements", expected["claim_to_statements"], state["claim_to_statements"], issues)
    _compare_maps("statement_to_claims", expected["statement_to_claims"], state["statement_to_claims"], issues)
    _compare_maps("direct_retrieval", expected["direct_retrieval"], state["direct_retrieval"], issues)
    _compare_maps("projections", expected["projections"], state["projections"], issues)
    retained_expected = build_index_state(tuple(state["projections"].values()), state["state_generation"])
    _check_build_report(state, retained_expected, issues)

    for statement_id, projection in sorted(expected["projections"].items()):
        bindings = expected["statement_to_retrieval"].get(statement_id, ())
        if not bindings:
            issues.append(
                index_check_issue(
                    IndexCheckCategory.UNINDEXABLE,
                    "projection",
                    statement_id,
                    (IndexIssueReason.MISSING_IDENTITY.value,),
                    (),
                    False,
                )
            )
        if not projection["direct_answer_eligible"] or projection["exclusion_reason"]:
            issues.append(
                index_check_issue(
                    IndexCheckCategory.INELIGIBLE,
                    "projection",
                    statement_id,
                    (projection["exclusion_reason"] or IndexIssueReason.INELIGIBLE.value,),
                    (),
                    False,
                )
            )
    for collision in expected["build_report"]["collisions"]:
        issues.append(
            index_check_issue(
                IndexCheckCategory.CONFLICTING,
                "direct_retrieval",
                _key_text(collision["key"]),
                collision["statement_ids"],
                (),
                False,
            )
        )

    ordered = tuple(sorted(issues, key=index_check_issue_signature))
    bounded = ordered[:MAX_INDEX_REPORT_ITEMS]
    result = index_check_report(
        consistent=not any(issue["error"] for issue in issues),
        checked_state_generation=state["state_generation"],
        issues=bounded,
        omitted_issue_count=len(ordered) - len(bounded),
    )
    return result


def check_index_state(state: IndexState) -> IndexCheckReport:
    """Check one state against its own retained projections."""
    _exact_mapping(state, "IndexState", INDEX_STATE_FIELDS)
    result = _check_index_state_against(state, tuple(state["projections"].values()))
    return result


def check_index_state_against(state: IndexState, projections: Iterable[object]) -> IndexCheckReport:
    """Compare one state with an explicitly supplied authoritative set."""
    result = _check_index_state_against(state, tuple(projections))
    return result


def _report_from_valid_state(
    projections: Mapping[str, IndexProjection],
    statement_to_retrieval: Mapping[str, tuple[RetrievalKeyBinding, ...]],
    statement_to_claims: Mapping[str, tuple[str, ...]],
    retrieval_to_owners: Mapping[ScopedRetrievalKeySignature, tuple[RetrievalOwner, ...]],
) -> IndexBuildReport:
    """Refresh bounded diagnostics without reconstructing any index map."""
    issues = []
    for position, (statement_id, projection) in enumerate(sorted(projections.items())):
        bindings = statement_to_retrieval[statement_id]
        _deduplicate_bindings(projection, position, issues)
        if not bindings:
            issues.append(
                _index_build_issue_from_validated(
                    IndexIssueReason.MISSING_IDENTITY,
                    statement_id,
                    position,
                    "projection carries no exact retrieval key",
                )
            )
        eligible = projection["direct_answer_eligible"] and not projection["exclusion_reason"] and bool(bindings)
        if not eligible:
            reason = projection["exclusion_reason"] or IndexIssueReason.INELIGIBLE.value
            if reason != IndexIssueReason.MISSING_IDENTITY.value or bindings:
                classified_reason = IndexIssueReason.INELIGIBLE
                try:
                    requested_reason = IndexIssueReason(reason)
                    if requested_reason in {
                        IndexIssueReason.MALFORMED_SUPPORT,
                        IndexIssueReason.MISSING_IDENTITY,
                    }:
                        classified_reason = requested_reason
                except ValueError:
                    pass
                issues.append(
                    _index_build_issue_from_validated(
                        classified_reason,
                        statement_id,
                        position,
                        reason[:MAX_INDEX_REPORT_DETAIL_BYTES],
                    )
                )

    retrieval_keys = {
        scoped_retrieval_key_signature(binding["key"]): binding["key"]
        for bindings in statement_to_retrieval.values()
        for binding in bindings
    }
    collisions = []
    for key_signature in sorted(retrieval_to_owners):
        key = retrieval_keys[key_signature]
        owners = retrieval_to_owners[key_signature]
        eligible_ids = tuple(dict.fromkeys(owner["statement_id"] for owner in owners if owner["direct_answer_eligible"]))
        if len(eligible_ids) <= 1:
            continue
        bounded_ids = eligible_ids[:MAX_INDEX_LOOKUP_OWNERS]
        collisions.append(
            index_collision_report(
                key=key,
                statement_ids=bounded_ids,
                truncated=len(eligible_ids) > len(bounded_ids),
            )
        )
        for statement_id in bounded_ids:
            issues.append(
                _index_build_issue_from_validated(
                    IndexIssueReason.CROSS_ARTIFACT_COLLISION,
                    statement_id,
                    0,
                    _key_text(key)[:MAX_INDEX_REPORT_DETAIL_BYTES],
                )
            )

    bounded_issues, omitted_issues = _bounded_valid_issues(issues)
    ordered_collisions = tuple(collisions)
    bounded_collisions = ordered_collisions[:MAX_INDEX_REPORT_ITEMS]
    result = _index_build_report_from_validated(
        len(projections),
        len(projections),
        len(retrieval_to_owners),
        sum(len(claim_ids) for claim_ids in statement_to_claims.values()),
        bounded_issues,
        bounded_collisions,
        omitted_issues,
        len(ordered_collisions) - len(bounded_collisions),
    )
    return result


def _refresh_direct_key(
    key_signature: ScopedRetrievalKeySignature,
    owners: tuple[RetrievalOwner, ...],
    direct_retrieval: dict[ScopedRetrievalKeySignature, RetrievalOwner],
) -> None:
    eligible_owners = tuple(owner for owner in owners if owner["direct_answer_eligible"])
    eligible_ids = tuple(dict.fromkeys(owner["statement_id"] for owner in eligible_owners))
    if len(eligible_ids) == 1:
        direct_retrieval[key_signature] = eligible_owners[0]
    else:
        direct_retrieval.pop(key_signature, ())


def _mutated_state(
    state: IndexState,
    removed_statement_ids: tuple[str, ...],
    added_projections: tuple[IndexProjection, ...],
) -> IndexState:
    """Copy the immutable maps and alter only the named projection edges."""
    retrieval_to_owners = dict(state["retrieval_to_owners"])
    statement_to_retrieval = dict(state["statement_to_retrieval"])
    claim_to_statements = dict(state["claim_to_statements"])
    statement_to_claims = dict(state["statement_to_claims"])
    direct_retrieval = dict(state["direct_retrieval"])
    projections = dict(state["projections"])

    for statement_id in removed_statement_ids:
        for binding in statement_to_retrieval.pop(statement_id, ()):
            key_signature = scoped_retrieval_key_signature(binding["key"])
            owners = tuple(owner for owner in retrieval_to_owners[key_signature] if owner["statement_id"] != statement_id)
            if owners:
                retrieval_to_owners[key_signature] = owners
                _refresh_direct_key(key_signature, owners, direct_retrieval)
            else:
                retrieval_to_owners.pop(key_signature, ())
                direct_retrieval.pop(key_signature, ())
        for claim_id in statement_to_claims.pop(statement_id, ()):
            statement_ids = tuple(owner_id for owner_id in claim_to_statements[claim_id] if owner_id != statement_id)
            if statement_ids:
                claim_to_statements[claim_id] = statement_ids
            else:
                claim_to_statements.pop(claim_id, ())
        projections.pop(statement_id, ())

    for projection in added_projections:
        duplicate_issues: list[IndexBuildIssue] = []
        bindings = _deduplicate_bindings(projection, 0, duplicate_issues)
        eligible = projection["direct_answer_eligible"] and not projection["exclusion_reason"] and bool(bindings)
        projections[projection["statement_id"]] = projection
        statement_to_retrieval[projection["statement_id"]] = bindings
        statement_to_claims[projection["statement_id"]] = projection["support_claim_ids"]
        for claim_id in projection["support_claim_ids"]:
            statement_ids = tuple(sorted((*claim_to_statements.get(claim_id, ()), projection["statement_id"])))
            claim_to_statements[claim_id] = statement_ids
        for binding in bindings:
            key_signature = scoped_retrieval_key_signature(binding["key"])
            owner = retrieval_owner(
                projection["statement_id"],
                projection["generation"],
                binding["origin"],
                binding["representation"],
                eligible,
            )
            owners = tuple(
                sorted(
                    (*retrieval_to_owners.get(key_signature, ()), owner),
                    key=retrieval_owner_signature,
                )
            )
            retrieval_to_owners[key_signature] = owners
            _refresh_direct_key(key_signature, owners, direct_retrieval)

    report = _report_from_valid_state(projections, statement_to_retrieval, statement_to_claims, retrieval_to_owners)
    result = _frozen_index_state(
        state["state_generation"] + 1,
        retrieval_to_owners,
        statement_to_retrieval,
        claim_to_statements,
        statement_to_claims,
        direct_retrieval,
        projections,
        report,
        RETRIEVAL_NORMALIZATION_VERSION,
        INDEX_STATE_SCHEMA_VERSION,
    )
    return result


def _add_index_projection_to_valid_state(state: IndexState, projection: IndexProjection) -> IndexState:
    try:
        projection = validate_index_projection(projection)
    except (InvalidRequestError, UnsupportedIdentityVersionError) as error:
        raise InvalidRequestError("projection must be an IndexProjection") from error
    if projection["statement_id"] in state["projections"]:
        raise ConflictError(f"index projection already exists: {projection['statement_id']}")
    result = _mutated_state(state, (), (projection,))
    return result


def add_index_projection(state: IndexState, projection: IndexProjection) -> IndexState:
    """Return an isolated state containing one newly validated projection."""
    validated_state = validate_index_state(state)
    result = _add_index_projection_to_valid_state(validated_state, projection)
    return result


def _replace_index_projection_in_valid_state(state: IndexState, projection: IndexProjection) -> IndexState:
    try:
        projection = validate_index_projection(projection)
    except (InvalidRequestError, UnsupportedIdentityVersionError) as error:
        raise InvalidRequestError("projection must be an IndexProjection") from error
    if projection["statement_id"] not in state["projections"]:
        raise InvalidRequestError(f"index projection does not exist: {projection['statement_id']}")
    result = _mutated_state(state, (projection["statement_id"],), (projection,))
    return result


def replace_index_projection(state: IndexState, projection: IndexProjection) -> IndexState:
    """Return an isolated state containing one validated replacement."""
    validated_state = validate_index_state(state)
    result = _replace_index_projection_in_valid_state(validated_state, projection)
    return result


def _remove_index_projection_from_valid_state(state: IndexState, statement_id: str) -> IndexState:
    validated_id = _bounded_text(statement_id, "index projection statement_id", MAX_INDEX_STATEMENT_ID_BYTES, allow_empty=False)
    if validated_id not in state["projections"]:
        raise InvalidRequestError(f"index projection does not exist: {validated_id}")
    result = _mutated_state(state, (validated_id,), ())
    return result


def remove_index_projection(state: IndexState, statement_id: str) -> IndexState:
    """Return an isolated state without one validated projection ID."""
    validated_state = validate_index_state(state)
    result = _remove_index_projection_from_valid_state(validated_state, statement_id)
    return result


def _update_index_support_in_valid_state(
    state: IndexState,
    statement_id: str,
    support_claim_ids: tuple[str, ...],
) -> IndexState:
    validated_id = _bounded_text(statement_id, "index projection statement_id", MAX_INDEX_STATEMENT_ID_BYTES, allow_empty=False)
    projection = state["projections"].get(validated_id)
    if not projection:
        raise InvalidRequestError(f"index projection does not exist: {validated_id}")
    updated = index_projection_with_changes(
        projection,
        {"generation": projection["generation"] + 1, "support_claim_ids": support_claim_ids},
    )
    result = _replace_index_projection_in_valid_state(state, updated)
    return result


def update_index_support(state: IndexState, statement_id: str, support_claim_ids: tuple[str, ...]) -> IndexState:
    """Return an isolated state with one projection's support set replaced."""
    validated_state = validate_index_state(state)
    result = _update_index_support_in_valid_state(validated_state, statement_id, support_claim_ids)
    return result


def _states_equivalent(left: IndexState, right: IndexState) -> bool:
    result = (
        left["retrieval_to_owners"] == right["retrieval_to_owners"]
        and left["statement_to_retrieval"] == right["statement_to_retrieval"]
        and left["claim_to_statements"] == right["claim_to_statements"]
        and left["statement_to_claims"] == right["statement_to_claims"]
        and left["direct_retrieval"] == right["direct_retrieval"]
        and left["projections"] == right["projections"]
    )
    return result


def _mutation_is_consistent(before: IndexState, candidate: IndexState, statement_id: str) -> bool:
    """Check every forward, inverse, and direct edge touched by one mutation."""
    touched_keys = {
        scoped_retrieval_key_signature(binding["key"])
        for binding in (
            *before["statement_to_retrieval"].get(statement_id, ()),
            *candidate["statement_to_retrieval"].get(statement_id, ()),
        )
    }
    touched_claims = {
        *before["statement_to_claims"].get(statement_id, ()),
        *candidate["statement_to_claims"].get(statement_id, ()),
    }
    for key_signature in touched_keys:
        owners = candidate["retrieval_to_owners"].get(key_signature, ())
        projection = candidate["projections"].get(statement_id)
        for binding in candidate["statement_to_retrieval"].get(statement_id, ()):
            if scoped_retrieval_key_signature(binding["key"]) != key_signature or not projection:
                continue
            eligible = projection["direct_answer_eligible"] and not projection["exclusion_reason"]
            expected_owner = retrieval_owner(
                statement_id,
                projection["generation"],
                binding["origin"],
                binding["representation"],
                eligible,
            )
            if expected_owner not in owners:
                result = False
                return result
        for owner in owners:
            reverse_keys = {
                scoped_retrieval_key_signature(binding["key"])
                for binding in candidate["statement_to_retrieval"].get(owner["statement_id"], ())
            }
            if key_signature not in reverse_keys:
                result = False
                return result
        eligible_owners = tuple(owner for owner in owners if owner["direct_answer_eligible"])
        eligible_ids = tuple(dict.fromkeys(owner["statement_id"] for owner in eligible_owners))
        selected = candidate["direct_retrieval"].get(key_signature)
        if len(eligible_ids) == 1:
            if not selected or selected["statement_id"] != eligible_ids[0]:
                result = False
                return result
        elif selected:
            result = False
            return result
    for claim_id in touched_claims:
        statement_ids = candidate["claim_to_statements"].get(claim_id, ())
        for owner_id in statement_ids:
            if claim_id not in candidate["statement_to_claims"].get(owner_id, ()):
                result = False
                return result
        if claim_id in candidate["statement_to_claims"].get(statement_id, ()) and statement_id not in statement_ids:
            result = False
            return result
        if statement_id in statement_ids and claim_id not in candidate["statement_to_claims"].get(statement_id, ()):
            result = False
            return result
    projection_present = statement_id in candidate["projections"]
    result = (
        projection_present
        == (statement_id in candidate["statement_to_retrieval"])
        == (statement_id in candidate["statement_to_claims"])
    )
    return result


def _copy_index_state(state: IndexState) -> IndexState:
    result = validate_index_state(state)
    return result


class IndexOwner:
    """Atomic owner of the currently visible immutable index snapshot."""

    def __init__(self, projections: Iterable[object] = ()) -> None:
        self._lock = threading.RLock()
        candidate = build_index_state(projections)
        if not check_index_state(candidate)["consistent"]:
            raise ConflictError("initial index state failed consistency checking")
        self._state = candidate

    def snapshot(self) -> IndexState:
        with self._lock:
            result = _copy_index_state(self._state)
            return result

    def _trusted_snapshot(self) -> IndexState:
        """Return the owner-held state for a bounded internal read-only operation."""
        with self._lock:
            result = self._state
            return result

    def check(self) -> IndexCheckReport:
        snapshot = self.snapshot()
        result = check_index_state(snapshot)
        return result

    def check_against(self, projections: Iterable[object]) -> IndexCheckReport:
        snapshot = self.snapshot()
        result = check_index_state_against(snapshot, projections)
        return result

    def has_projection(self, statement_id: str) -> bool:
        """Report whether the live state contains one bounded statement ID."""
        validated_id = _bounded_text(
            statement_id,
            "index projection statement_id",
            MAX_INDEX_STATEMENT_ID_BYTES,
            allow_empty=False,
        )
        with self._lock:
            result = validated_id in self._state["projections"]
            return result

    def exact_owner_snapshot(self, key: ScopedRetrievalKey) -> tuple[int, tuple[str, ...]]:
        """Return one exact key's generation and owners without copying the full index."""
        try:
            validated_key = validate_scoped_retrieval_key(key)
        except IdentityValidationError as error:
            raise InvalidRequestError("exact owner key must be a ScopedRetrievalKey") from error
        result = self.trusted_exact_owner_snapshot(validated_key)
        return result

    def trusted_exact_owner_snapshot(self, key: ScopedRetrievalKey) -> tuple[int, tuple[str, ...]]:
        """Return owners for a key already validated by ContextualExactLookup."""
        key_signature = trusted_scoped_retrieval_key_signature(key)
        with self._lock:
            owner_ids = tuple(
                dict.fromkeys(owner["statement_id"] for owner in self._state["retrieval_to_owners"].get(key_signature, ()))
            )
            result = self._state["state_generation"], owner_ids
            return result

    def atomic_swap(self, candidate: IndexState, expected_state_generation: int) -> IndexState:
        report = check_index_state(candidate)
        if not report["consistent"]:
            raise ConflictError("candidate index state failed consistency checking")
        validated_candidate = validate_index_state(candidate)
        with self._lock:
            if self._state["state_generation"] != expected_state_generation:
                raise ConflictError(
                    f"stale index state generation: expected {expected_state_generation}, found {self._state['state_generation']}"
                )
            self._state = validated_candidate
            result = _copy_index_state(self._state)
            return result

    def add(self, projection: IndexProjection) -> IndexState:
        with self._lock:
            candidate = _add_index_projection_to_valid_state(self._state, projection)
            if not _mutation_is_consistent(self._state, candidate, projection["statement_id"]):
                raise ConflictError("candidate add state failed consistency checking")
            self._state = candidate
            result = _copy_index_state(self._state)
            return result

    def add_in_place(self, projection: IndexProjection) -> None:
        """Publish one validated addition without constructing a caller snapshot."""
        with self._lock:
            candidate = _add_index_projection_to_valid_state(self._state, projection)
            if not _mutation_is_consistent(self._state, candidate, projection["statement_id"]):
                raise ConflictError("candidate add state failed consistency checking")
            self._state = candidate
            return

    def replace(self, projection: IndexProjection) -> IndexState:
        with self._lock:
            candidate = _replace_index_projection_in_valid_state(self._state, projection)
            if not _mutation_is_consistent(self._state, candidate, projection["statement_id"]):
                raise ConflictError("candidate replacement state failed consistency checking")
            self._state = candidate
            result = _copy_index_state(self._state)
            return result

    def replace_in_place(self, projection: IndexProjection) -> None:
        """Publish one validated replacement without constructing a caller snapshot."""
        with self._lock:
            candidate = _replace_index_projection_in_valid_state(self._state, projection)
            if not _mutation_is_consistent(self._state, candidate, projection["statement_id"]):
                raise ConflictError("candidate replacement state failed consistency checking")
            self._state = candidate
            return

    def remove(self, statement_id: str) -> IndexState:
        with self._lock:
            candidate = _remove_index_projection_from_valid_state(self._state, statement_id)
            if not _mutation_is_consistent(self._state, candidate, statement_id):
                raise ConflictError("candidate removal state failed consistency checking")
            self._state = candidate
            result = _copy_index_state(self._state)
            return result

    def remove_in_place(self, statement_id: str) -> None:
        """Publish one validated removal without constructing a caller snapshot."""
        with self._lock:
            candidate = _remove_index_projection_from_valid_state(self._state, statement_id)
            if not _mutation_is_consistent(self._state, candidate, statement_id):
                raise ConflictError("candidate removal state failed consistency checking")
            self._state = candidate
            return

    def update_support(self, statement_id: str, support_claim_ids: tuple[str, ...]) -> IndexState:
        with self._lock:
            candidate = _update_index_support_in_valid_state(self._state, statement_id, support_claim_ids)
            if not _mutation_is_consistent(self._state, candidate, statement_id):
                raise ConflictError("candidate support-update state failed consistency checking")
            self._state = candidate
            result = _copy_index_state(self._state)
            return result

    def atomic_refresh_exact_lookup(
        self,
        key: ScopedRetrievalKey,
        refreshed_projections: tuple[IndexProjection, ...],
        expected_state_generation: int,
    ) -> tuple[ExactLookupResult, bool, int]:
        """Atomically refresh every current owner of one exact key, then look up.

        Section 3 calculates the projections from authoritative artifacts and
        a captured eligibility context.  Section 2 only verifies complete owner
        coverage and publishes a checked generic index state.
        """

        try:
            key = validate_scoped_retrieval_key(key)
        except IdentityValidationError as error:
            raise InvalidRequestError("exact refresh key must be a ScopedRetrievalKey") from error
        if not isinstance(refreshed_projections, tuple):
            raise InvalidRequestError("refreshed_projections must be a tuple of IndexProjection values")
        try:
            refreshed_projections = tuple(validate_index_projection(projection) for projection in refreshed_projections)
        except (InvalidRequestError, UnsupportedIdentityVersionError) as error:
            raise InvalidRequestError("refreshed_projections must be a tuple of IndexProjection values") from error
        if isinstance(expected_state_generation, bool) or not isinstance(expected_state_generation, int):
            raise InvalidRequestError("expected_state_generation must be an integer")
        result = self.trusted_atomic_refresh_exact_lookup(key, refreshed_projections, expected_state_generation)
        return result

    def trusted_atomic_refresh_exact_lookup(
        self,
        key: ScopedRetrievalKey,
        refreshed_projections: tuple[IndexProjection, ...],
        expected_state_generation: int,
    ) -> tuple[ExactLookupResult, bool, int]:
        """Refresh values produced by ContextualExactLookup after one validation pass."""
        with self._lock:
            if self._state["state_generation"] != expected_state_generation:
                raise ConflictError(
                    f"stale index state generation: expected {expected_state_generation}, found {self._state['state_generation']}"
                )
            key_signature = trusted_scoped_retrieval_key_signature(key)
            owner_ids = tuple(
                dict.fromkeys(owner["statement_id"] for owner in self._state["retrieval_to_owners"].get(key_signature, ()))
            )
            refreshed_ids = tuple(dict.fromkeys(projection["statement_id"] for projection in refreshed_projections))
            if len(refreshed_ids) != len(refreshed_projections):
                raise InvalidRequestError("refreshed_projections must contain unique statement IDs")
            if set(refreshed_ids) != set(owner_ids):
                raise ConflictError("exact refresh must cover every current owner and no unrelated projection")
            replacements = {projection["statement_id"]: projection for projection in refreshed_projections}
            for statement_id in owner_ids:
                current = self._state["projections"][statement_id]
                refreshed = replacements[statement_id]
                if (
                    current["statement_id"] != refreshed["statement_id"]
                    or current["generation"] != refreshed["generation"]
                    or current["retrieval_keys"] != refreshed["retrieval_keys"]
                    or current["support_claim_ids"] != refreshed["support_claim_ids"]
                    or current["normalization_version"] != refreshed["normalization_version"]
                    or current["schema_version"] != refreshed["schema_version"]
                ):
                    raise InvalidRequestError("exact refresh may change only direct_answer_eligible and exclusion_reason")
            changed = any(self._state["projections"][statement_id] != replacements[statement_id] for statement_id in owner_ids)
            if changed:
                projections = dict(self._state["projections"])
                projections.update(replacements)
                candidate = build_index_state(projections.values(), self._state["state_generation"] + 1)
                if not check_index_state(candidate)["consistent"]:
                    raise ConflictError("candidate exact refresh state failed consistency checking")
                self._state = candidate
            lookup = trusted_index_state_exact_lookup(self._state, key)
            result = (lookup, changed, self._state["state_generation"])
            return result

    def repair(self, projections: Iterable[object], *, dry_run: bool = True) -> IndexRepairResult:
        if not isinstance(dry_run, bool):
            raise InvalidRequestError("index repair dry_run must be a boolean")
        before = self.snapshot()
        candidate = build_index_state(projections, before["state_generation"] + 1)
        candidate_check = check_index_state(candidate)
        if not candidate_check["consistent"]:
            raise ConflictError("candidate repair state failed consistency checking")
        live_check = check_index_state_against(before, projections)
        changed = not _states_equivalent(before, candidate)
        if not dry_run:
            self.atomic_swap(candidate, before["state_generation"])
        result = index_repair_result(
            not dry_run,
            changed,
            before["state_generation"],
            candidate["state_generation"] if not dry_run else before["state_generation"],
            candidate["build_report"],
            live_check,
        )
        return result
