"""Immutable exact-retrieval and Claim-support indexes.

The records in this module are disposable derived state.  Authoritative
response artifacts live outside the index and supply :class:`IndexProjection`
values.  Builders never infer an exact identity from statement text, patterns,
or lexical keywords.
"""

import json
import threading
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from enum import StrEnum
from types import MappingProxyType
from typing import TypeVar

from engram.errors import ConflictError, IdentityValidationError, InvalidRequestError, UnsupportedIdentityVersionError
from engram.identity import RETRIEVAL_NORMALIZATION_VERSION, RetrievalKeyBinding, RetrievalOrigin, ScopedRetrievalKey

INDEX_PROJECTION_SCHEMA_VERSION = 1
INDEX_STATE_SCHEMA_VERSION = 1
MAX_INDEX_STATEMENT_ID_BYTES = 256
MAX_SUPPORT_CLAIM_ID_BYTES = 256
MAX_INDEX_RETRIEVAL_KEYS = 33
MAX_INDEX_SUPPORT_IDS = 256
MAX_INDEX_EXCLUSION_REASON_BYTES = 128
MAX_INDEX_REPORT_ITEMS = 1_000
MAX_INDEX_REPORT_DETAIL_BYTES = 512
MAX_INDEX_LOOKUP_OWNERS = 1_000
MAX_INDEX_SUPPORT_SCAN_EDGES = 100_000
MapKey = TypeVar("MapKey")
MapValue = TypeVar("MapValue")


class ExactLookupOutcome(StrEnum):
    """The complete set of exact lookup outcomes."""

    FOUND = "FOUND"
    MISS = "MISS"
    COLLISION = "COLLISION"


class IndexIssueReason(StrEnum):
    """Stable build and validation classifications."""

    MISSING_IDENTITY = "missing_identity"
    UNSUPPORTED_SCHEMA_VERSION = "unsupported_schema_version"
    UNSUPPORTED_NORMALIZATION_VERSION = "unsupported_normalization_version"
    MALFORMED_PROJECTION = "malformed_projection"
    MALFORMED_SUPPORT = "malformed_support"
    INELIGIBLE = "ineligible"
    WITHIN_ARTIFACT_DUPLICATE = "within_artifact_duplicate"
    DUPLICATE_STATEMENT_ID = "duplicate_statement_id"
    CROSS_ARTIFACT_COLLISION = "cross_artifact_collision"


class IndexCheckCategory(StrEnum):
    """Categories emitted by the consistency checker."""

    MISSING = "missing"
    EXTRA = "extra"
    ASYMMETRIC = "asymmetric"
    INELIGIBLE = "ineligible"
    UNINDEXABLE = "unindexable"
    CONFLICTING = "conflicting"


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


def _exact_mapping(value: object, name: str, keys: frozenset[str]) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise InvalidRequestError(f"{name} must be an object")
    if frozenset(value) != keys:
        missing = sorted(keys - frozenset(value))
        extra = sorted(frozenset(value) - keys)
        raise InvalidRequestError(f"{name} has invalid fields: missing={missing}, extra={extra}")
    return value


def _json_text(value: Mapping[str, object]) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _key_text(key: ScopedRetrievalKey) -> str:
    return key.to_json()


@dataclass(frozen=True, slots=True)
class IndexProjection:
    """Bounded index-only view of one authoritative response artifact."""

    statement_id: str
    generation: int
    retrieval_keys: tuple[RetrievalKeyBinding, ...]
    support_claim_ids: tuple[str, ...]
    direct_answer_eligible: bool
    exclusion_reason: str
    normalization_version: int = RETRIEVAL_NORMALIZATION_VERSION
    schema_version: int = INDEX_PROJECTION_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != INDEX_PROJECTION_SCHEMA_VERSION:
            raise UnsupportedIdentityVersionError(f"unsupported index projection schema version: {self.schema_version}")
        if self.normalization_version != RETRIEVAL_NORMALIZATION_VERSION:
            raise UnsupportedIdentityVersionError(
                f"unsupported index projection normalization version: {self.normalization_version}"
            )
        _bounded_text(self.statement_id, "index projection statement_id", MAX_INDEX_STATEMENT_ID_BYTES, allow_empty=False)
        _positive_int(self.generation, "index projection generation")
        if not isinstance(self.retrieval_keys, tuple):
            raise InvalidRequestError("index projection retrieval_keys must be a tuple")
        if len(self.retrieval_keys) > MAX_INDEX_RETRIEVAL_KEYS:
            raise InvalidRequestError(f"index projection retrieval_keys exceed the limit of {MAX_INDEX_RETRIEVAL_KEYS}")
        for binding in self.retrieval_keys:
            if not isinstance(binding, RetrievalKeyBinding):
                raise InvalidRequestError("every index projection retrieval key must be a RetrievalKeyBinding")
            if binding.key.normalization_version != self.normalization_version:
                raise InvalidRequestError("retrieval key normalization version differs from its projection")
        if not isinstance(self.support_claim_ids, tuple):
            raise InvalidRequestError("index projection support_claim_ids must be a tuple")
        if len(self.support_claim_ids) > MAX_INDEX_SUPPORT_IDS:
            raise InvalidRequestError(f"index projection support_claim_ids exceed the limit of {MAX_INDEX_SUPPORT_IDS}")
        validated_support = tuple(
            sorted(
                {
                    _bounded_text(claim_id, "support Claim ID", MAX_SUPPORT_CLAIM_ID_BYTES, allow_empty=False)
                    for claim_id in self.support_claim_ids
                }
            )
        )
        object.__setattr__(self, "support_claim_ids", validated_support)
        if not isinstance(self.direct_answer_eligible, bool):
            raise InvalidRequestError("index projection direct_answer_eligible must be a boolean")
        _bounded_text(
            self.exclusion_reason,
            "index projection exclusion_reason",
            MAX_INDEX_EXCLUSION_REASON_BYTES,
            allow_empty=True,
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "statement_id": self.statement_id,
            "generation": self.generation,
            "retrieval_keys": [
                {
                    "key": binding.key.to_dict(),
                    "provenance": binding.origin.value,
                    "representation": binding.representation,
                }
                for binding in self.retrieval_keys
            ],
            "support_claim_ids": list(self.support_claim_ids),
            "direct_answer_eligible": self.direct_answer_eligible,
            "exclusion_reason": self.exclusion_reason,
            "normalization_version": self.normalization_version,
        }

    def to_json(self) -> str:
        return _json_text(self.to_dict())

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> "IndexProjection":
        keys = frozenset(
            {
                "schema_version",
                "statement_id",
                "generation",
                "retrieval_keys",
                "support_claim_ids",
                "direct_answer_eligible",
                "exclusion_reason",
                "normalization_version",
            }
        )
        data = _exact_mapping(value, "IndexProjection", keys)
        schema_version = _positive_int(data["schema_version"], "index projection schema_version")
        if schema_version != INDEX_PROJECTION_SCHEMA_VERSION:
            raise UnsupportedIdentityVersionError(f"unsupported index projection schema version: {schema_version}")
        normalization_version = _positive_int(data["normalization_version"], "index projection normalization_version")
        if normalization_version != RETRIEVAL_NORMALIZATION_VERSION:
            raise UnsupportedIdentityVersionError(f"unsupported index projection normalization version: {normalization_version}")
        raw_keys = data["retrieval_keys"]
        if not isinstance(raw_keys, list):
            raise InvalidRequestError("index projection retrieval_keys must be an array")
        bindings = []
        binding_keys = frozenset({"key", "provenance", "representation"})
        for position, raw_binding in enumerate(raw_keys):
            binding_data = _exact_mapping(raw_binding, f"retrieval key binding {position}", binding_keys)
            provenance = _bounded_text(binding_data["provenance"], "retrieval provenance", 32, allow_empty=False)
            try:
                origin = RetrievalOrigin(provenance)
            except ValueError as error:
                raise InvalidRequestError(f"unsupported retrieval provenance: {provenance}") from error
            key_data = binding_data["key"]
            if not isinstance(key_data, Mapping):
                raise InvalidRequestError(f"retrieval key binding {position} key must be an object")
            bindings.append(
                RetrievalKeyBinding(
                    key=ScopedRetrievalKey.from_dict(key_data),
                    origin=origin,
                    representation=_bounded_text(
                        binding_data["representation"],
                        "retrieval representation",
                        1_024,
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
        direct_answer_eligible = data["direct_answer_eligible"]
        if not isinstance(direct_answer_eligible, bool):
            raise InvalidRequestError("index projection direct_answer_eligible must be a boolean")
        return cls(
            schema_version=schema_version,
            statement_id=_bounded_text(
                data["statement_id"], "index projection statement_id", MAX_INDEX_STATEMENT_ID_BYTES, allow_empty=False
            ),
            generation=_positive_int(data["generation"], "index projection generation"),
            retrieval_keys=tuple(bindings),
            support_claim_ids=support,
            direct_answer_eligible=direct_answer_eligible,
            exclusion_reason=_bounded_text(
                data["exclusion_reason"],
                "index projection exclusion_reason",
                MAX_INDEX_EXCLUSION_REASON_BYTES,
                allow_empty=True,
            ),
            normalization_version=normalization_version,
        )

    @classmethod
    def from_json(cls, value: str) -> "IndexProjection":
        if not isinstance(value, str):
            raise InvalidRequestError("IndexProjection JSON must be a string")
        try:
            data = json.loads(value)
        except json.JSONDecodeError as error:
            raise InvalidRequestError("IndexProjection JSON is malformed") from error
        if not isinstance(data, Mapping):
            raise InvalidRequestError("IndexProjection JSON must contain an object")
        return cls.from_dict(data)


@dataclass(frozen=True, order=True, slots=True)
class RetrievalOwner:
    """One statement's ownership and provenance for a retrieval key."""

    statement_id: str
    generation: int
    provenance: RetrievalOrigin
    representation: str
    direct_answer_eligible: bool


@dataclass(frozen=True, slots=True)
class IndexCollisionReport:
    """One bounded group of eligible artifacts that own the same key."""

    key: ScopedRetrievalKey
    statement_ids: tuple[str, ...]
    truncated: bool

    def to_dict(self) -> dict[str, object]:
        return {"key": self.key.to_dict(), "statement_ids": list(self.statement_ids), "truncated": self.truncated}


@dataclass(frozen=True, order=True, slots=True)
class IndexBuildIssue:
    """One deterministic build classification."""

    reason: IndexIssueReason
    statement_id: str
    position: int
    detail: str
    input_only: bool = False

    def to_dict(self) -> dict[str, object]:
        return {
            "reason": self.reason.value,
            "statement_id": self.statement_id,
            "position": self.position,
            "detail": self.detail,
            "input_only": self.input_only,
        }


@dataclass(frozen=True, slots=True)
class IndexBuildReport:
    """Bounded summary of deterministic candidate construction."""

    input_count: int
    projection_count: int
    exact_key_count: int
    support_edge_count: int
    issues: tuple[IndexBuildIssue, ...]
    collisions: tuple[IndexCollisionReport, ...]
    omitted_issue_count: int
    omitted_collision_count: int

    def to_dict(self) -> dict[str, object]:
        return {
            "input_count": self.input_count,
            "projection_count": self.projection_count,
            "exact_key_count": self.exact_key_count,
            "support_edge_count": self.support_edge_count,
            "issues": [issue.to_dict() for issue in self.issues],
            "collisions": [collision.to_dict() for collision in self.collisions],
            "omitted_issue_count": self.omitted_issue_count,
            "omitted_collision_count": self.omitted_collision_count,
        }


@dataclass(frozen=True, slots=True)
class ExactLookupResult:
    """Typed exact lookup with no implicit collision winner."""

    outcome: ExactLookupOutcome
    key: ScopedRetrievalKey
    statement_id: str
    generation: int
    provenance: str
    representation: str
    owner_statement_ids: tuple[str, ...]
    truncated: bool

    def to_dict(self) -> dict[str, object]:
        return {
            "outcome": self.outcome.value,
            "key": self.key.to_dict(),
            "statement_id": self.statement_id,
            "generation": self.generation,
            "provenance": self.provenance,
            "representation": self.representation,
            "owner_statement_ids": list(self.owner_statement_ids),
            "truncated": self.truncated,
        }


@dataclass(frozen=True, order=True, slots=True)
class SupportMatch:
    """One statement plus the queried Claims that support it."""

    statement_id: str
    matched_claim_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class SupportLookupResult:
    """Bounded support lookup result."""

    queried_claim_ids: tuple[str, ...]
    matches: tuple[SupportMatch, ...]
    omitted_match_count: int
    omitted_edge_count: int
    scanned_edge_count: int
    complete: bool
    reason: str


@dataclass(frozen=True, slots=True)
class SupportScanPlan:
    """Validated support traversal bounds shared by lookup and ranking."""

    queried_claim_ids: tuple[str, ...]
    edge_count: int
    scan_limit: int
    complete: bool
    reason: str


@dataclass(frozen=True, order=True, slots=True)
class IndexCheckIssue:
    """One checker finding; notices classify expected exclusions safely."""

    category: IndexCheckCategory
    index_name: str
    key: str
    expected: tuple[str, ...]
    actual: tuple[str, ...]
    error: bool

    def to_dict(self) -> dict[str, object]:
        return {
            "category": self.category.value,
            "index_name": self.index_name,
            "key": self.key,
            "expected": list(self.expected),
            "actual": list(self.actual),
            "error": self.error,
        }


@dataclass(frozen=True, slots=True)
class IndexCheckReport:
    """Bounded, non-mutating consistency report."""

    consistent: bool
    checked_state_generation: int
    issues: tuple[IndexCheckIssue, ...]
    omitted_issue_count: int

    def to_dict(self) -> dict[str, object]:
        return {
            "consistent": self.consistent,
            "checked_state_generation": self.checked_state_generation,
            "issues": [issue.to_dict() for issue in self.issues],
            "omitted_issue_count": self.omitted_issue_count,
        }


@dataclass(frozen=True, slots=True)
class IndexState:
    """One immutable, complete snapshot of every Section 2 index."""

    state_generation: int
    retrieval_to_owners: Mapping[ScopedRetrievalKey, tuple[RetrievalOwner, ...]]
    statement_to_retrieval: Mapping[str, tuple[RetrievalKeyBinding, ...]]
    claim_to_statements: Mapping[str, tuple[str, ...]]
    statement_to_claims: Mapping[str, tuple[str, ...]]
    direct_retrieval: Mapping[ScopedRetrievalKey, RetrievalOwner]
    projections: Mapping[str, IndexProjection]
    build_report: IndexBuildReport
    normalization_version: int = RETRIEVAL_NORMALIZATION_VERSION
    schema_version: int = INDEX_STATE_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != INDEX_STATE_SCHEMA_VERSION:
            raise UnsupportedIdentityVersionError(f"unsupported index state schema version: {self.schema_version}")
        if self.normalization_version != RETRIEVAL_NORMALIZATION_VERSION:
            raise UnsupportedIdentityVersionError(f"unsupported index state normalization version: {self.normalization_version}")
        _positive_int(self.state_generation, "index state generation")
        object.__setattr__(self, "retrieval_to_owners", MappingProxyType(dict(self.retrieval_to_owners)))
        object.__setattr__(self, "statement_to_retrieval", MappingProxyType(dict(self.statement_to_retrieval)))
        object.__setattr__(self, "claim_to_statements", MappingProxyType(dict(self.claim_to_statements)))
        object.__setattr__(self, "statement_to_claims", MappingProxyType(dict(self.statement_to_claims)))
        object.__setattr__(self, "direct_retrieval", MappingProxyType(dict(self.direct_retrieval)))
        object.__setattr__(self, "projections", MappingProxyType(dict(self.projections)))

    def exact_lookup(self, key: ScopedRetrievalKey) -> ExactLookupResult:
        if not isinstance(key, ScopedRetrievalKey):
            raise InvalidRequestError("exact lookup key must be a ScopedRetrievalKey")
        owners = self.retrieval_to_owners.get(key, ())
        owner_ids = tuple(dict.fromkeys(owner.statement_id for owner in owners))
        bounded_ids = owner_ids[:MAX_INDEX_LOOKUP_OWNERS]
        truncated = len(owner_ids) > len(bounded_ids)
        selected = self.direct_retrieval.get(key)
        if selected:
            return ExactLookupResult(
                outcome=ExactLookupOutcome.FOUND,
                key=key,
                statement_id=selected.statement_id,
                generation=selected.generation,
                provenance=selected.provenance.value,
                representation=selected.representation,
                owner_statement_ids=bounded_ids,
                truncated=truncated,
            )
        eligible_ids = tuple(dict.fromkeys(owner.statement_id for owner in owners if owner.direct_answer_eligible))
        outcome = ExactLookupOutcome.COLLISION if len(eligible_ids) > 1 else ExactLookupOutcome.MISS
        return ExactLookupResult(
            outcome=outcome,
            key=key,
            statement_id="",
            generation=0,
            provenance="",
            representation="",
            owner_statement_ids=bounded_ids,
            truncated=truncated,
        )

    def support_scan_plan(
        self,
        claim_ids: tuple[str, ...],
        scan_limit: int = MAX_INDEX_SUPPORT_SCAN_EDGES,
    ) -> SupportScanPlan:
        """Validate one bounded traversal before any fan-out is materialized."""
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
        edge_count = sum(len(self.claim_to_statements.get(claim_id, ())) for claim_id in queried)
        return SupportScanPlan(
            queried_claim_ids=queried,
            edge_count=edge_count,
            scan_limit=scan_limit,
            complete=edge_count <= scan_limit,
            reason="" if edge_count <= scan_limit else "scan_limit_exceeded",
        )

    def support_lookup(
        self,
        claim_ids: tuple[str, ...],
        scan_limit: int = MAX_INDEX_SUPPORT_SCAN_EDGES,
    ) -> SupportLookupResult:
        plan = self.support_scan_plan(claim_ids, scan_limit)
        if not plan.complete:
            return SupportLookupResult(
                queried_claim_ids=plan.queried_claim_ids,
                matches=(),
                omitted_match_count=0,
                omitted_edge_count=plan.edge_count,
                scanned_edge_count=0,
                complete=False,
                reason=plan.reason,
            )
        matched_by_statement: dict[str, set[str]] = {}
        for claim_id in plan.queried_claim_ids:
            for statement_id in self.claim_to_statements.get(claim_id, ()):
                matched_by_statement.setdefault(statement_id, set()).add(claim_id)
        all_matches = tuple(
            SupportMatch(statement_id=statement_id, matched_claim_ids=tuple(sorted(matched_by_statement[statement_id])))
            for statement_id in sorted(matched_by_statement)
        )
        matches = all_matches[:MAX_INDEX_LOOKUP_OWNERS]
        return SupportLookupResult(
            queried_claim_ids=plan.queried_claim_ids,
            matches=matches,
            omitted_match_count=len(all_matches) - len(matches),
            omitted_edge_count=0,
            scanned_edge_count=plan.edge_count,
            complete=True,
            reason="",
        )


@dataclass(frozen=True, slots=True)
class IndexRepairResult:
    """Result of a transport-neutral dry run, rebuild, or repair."""

    applied: bool
    changed: bool
    before_generation: int
    after_generation: int
    candidate_report: IndexBuildReport
    live_check: IndexCheckReport

    def to_dict(self) -> dict[str, object]:
        return {
            "applied": self.applied,
            "changed": self.changed,
            "before_generation": self.before_generation,
            "after_generation": self.after_generation,
            "candidate_report": self.candidate_report.to_dict(),
            "live_check": self.live_check.to_dict(),
        }


def _bounded_issues(issues: list[IndexBuildIssue]) -> tuple[tuple[IndexBuildIssue, ...], int]:
    ordered = tuple(sorted(issues))
    bounded = ordered[:MAX_INDEX_REPORT_ITEMS]
    return bounded, len(ordered) - len(bounded)


def _raw_statement_id(value: object) -> str:
    if not isinstance(value, Mapping):
        return ""
    statement_id = value.get("statement_id", "")
    if not isinstance(statement_id, str):
        return ""
    return statement_id[:MAX_INDEX_STATEMENT_ID_BYTES]


def _projection_from_input(value: object) -> IndexProjection:
    if isinstance(value, IndexProjection):
        return value
    if isinstance(value, Mapping):
        return IndexProjection.from_dict(value)
    raise InvalidRequestError("index projection input must be an IndexProjection or object")


def _input_error_reason(value: object, error: Exception) -> IndexIssueReason:
    message = str(error).lower()
    if isinstance(error, UnsupportedIdentityVersionError):
        if "normalization" in message:
            return IndexIssueReason.UNSUPPORTED_NORMALIZATION_VERSION
        return IndexIssueReason.UNSUPPORTED_SCHEMA_VERSION
    if "support" in message:
        return IndexIssueReason.MALFORMED_SUPPORT
    return IndexIssueReason.MALFORMED_PROJECTION


def _deduplicate_bindings(
    projection: IndexProjection,
    position: int,
    issues: list[IndexBuildIssue],
) -> tuple[RetrievalKeyBinding, ...]:
    by_key: dict[ScopedRetrievalKey, RetrievalKeyBinding] = {}
    duplicates = set()
    for binding in projection.retrieval_keys:
        current = by_key.get(binding.key)
        if current:
            duplicates.add(binding.key)
            if current.origin == RetrievalOrigin.ALIAS and binding.origin == RetrievalOrigin.CANONICAL:
                by_key[binding.key] = binding
        else:
            by_key[binding.key] = binding
    for key in sorted(duplicates):
        issues.append(
            IndexBuildIssue(
                reason=IndexIssueReason.WITHIN_ARTIFACT_DUPLICATE,
                statement_id=projection.statement_id,
                position=position,
                detail=_key_text(key)[:MAX_INDEX_REPORT_DETAIL_BYTES],
            )
        )
    return tuple(by_key[key] for key in sorted(by_key))


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
                IndexBuildIssue(
                    reason=_input_error_reason(raw_projection, error),
                    statement_id=_raw_statement_id(raw_projection),
                    position=position,
                    detail=str(error)[:MAX_INDEX_REPORT_DETAIL_BYTES],
                    input_only=True,
                )
            )

    grouped: dict[str, list[tuple[int, IndexProjection]]] = {}
    for position, projection in valid_with_position:
        grouped.setdefault(projection.statement_id, []).append((position, projection))
    accepted: list[tuple[int, IndexProjection]] = []
    for statement_id in sorted(grouped):
        entries = grouped[statement_id]
        unique = {projection.to_json(): projection for _, projection in entries}
        if len(unique) > 1:
            for position, _ in entries:
                issues.append(
                    IndexBuildIssue(
                        reason=IndexIssueReason.DUPLICATE_STATEMENT_ID,
                        statement_id=statement_id,
                        position=position,
                        detail="conflicting projections share one statement ID",
                        input_only=True,
                    )
                )
            continue
        accepted.append(min(entries, key=lambda entry: entry[0]))

    retrieval_work: dict[ScopedRetrievalKey, list[RetrievalOwner]] = {}
    statement_to_retrieval: dict[str, tuple[RetrievalKeyBinding, ...]] = {}
    claim_work: dict[str, set[str]] = {}
    statement_to_claims: dict[str, tuple[str, ...]] = {}
    projection_map: dict[str, IndexProjection] = {}

    for position, projection in sorted(accepted, key=lambda entry: entry[1].statement_id):
        bindings = _deduplicate_bindings(projection, position, issues)
        projection_map[projection.statement_id] = projection
        statement_to_retrieval[projection.statement_id] = bindings
        statement_to_claims[projection.statement_id] = projection.support_claim_ids
        for claim_id in projection.support_claim_ids:
            claim_work.setdefault(claim_id, set()).add(projection.statement_id)
        if not bindings:
            issues.append(
                IndexBuildIssue(
                    reason=IndexIssueReason.MISSING_IDENTITY,
                    statement_id=projection.statement_id,
                    position=position,
                    detail="projection carries no exact retrieval key",
                )
            )
        eligible = projection.direct_answer_eligible and not projection.exclusion_reason and bool(bindings)
        if not eligible:
            reason = projection.exclusion_reason or IndexIssueReason.INELIGIBLE.value
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
                    IndexBuildIssue(
                        reason=classified_reason,
                        statement_id=projection.statement_id,
                        position=position,
                        detail=reason[:MAX_INDEX_REPORT_DETAIL_BYTES],
                    )
                )
        for binding in bindings:
            retrieval_work.setdefault(binding.key, []).append(
                RetrievalOwner(
                    statement_id=projection.statement_id,
                    generation=projection.generation,
                    provenance=binding.origin,
                    representation=binding.representation,
                    direct_answer_eligible=eligible,
                )
            )

    retrieval_to_owners: dict[ScopedRetrievalKey, tuple[RetrievalOwner, ...]] = {}
    direct_retrieval: dict[ScopedRetrievalKey, RetrievalOwner] = {}
    collisions = []
    for key in sorted(retrieval_work):
        owners = tuple(sorted(retrieval_work[key]))
        retrieval_to_owners[key] = owners
        eligible_owners = tuple(owner for owner in owners if owner.direct_answer_eligible)
        eligible_statement_ids = tuple(dict.fromkeys(owner.statement_id for owner in eligible_owners))
        if len(eligible_statement_ids) == 1:
            direct_retrieval[key] = eligible_owners[0]
        elif len(eligible_statement_ids) > 1:
            bounded_ids = eligible_statement_ids[:MAX_INDEX_LOOKUP_OWNERS]
            collision = IndexCollisionReport(
                key=key,
                statement_ids=bounded_ids,
                truncated=len(eligible_statement_ids) > len(bounded_ids),
            )
            collisions.append(collision)
            for statement_id in bounded_ids:
                issues.append(
                    IndexBuildIssue(
                        reason=IndexIssueReason.CROSS_ARTIFACT_COLLISION,
                        statement_id=statement_id,
                        position=0,
                        detail=_key_text(key)[:MAX_INDEX_REPORT_DETAIL_BYTES],
                    )
                )

    claim_to_statements = {claim_id: tuple(sorted(statement_ids)) for claim_id, statement_ids in sorted(claim_work.items())}
    bounded_issues, omitted_issues = _bounded_issues(issues)
    ordered_collisions = tuple(sorted(collisions, key=lambda collision: collision.key))
    bounded_collisions = ordered_collisions[:MAX_INDEX_REPORT_ITEMS]
    report = IndexBuildReport(
        input_count=len(inputs),
        projection_count=len(projection_map),
        exact_key_count=len(retrieval_to_owners),
        support_edge_count=sum(len(statement_ids) for statement_ids in claim_to_statements.values()),
        issues=bounded_issues,
        collisions=bounded_collisions,
        omitted_issue_count=omitted_issues,
        omitted_collision_count=len(ordered_collisions) - len(bounded_collisions),
    )
    return IndexState(
        state_generation=state_generation,
        retrieval_to_owners=retrieval_to_owners,
        statement_to_retrieval=statement_to_retrieval,
        claim_to_statements=claim_to_statements,
        statement_to_claims=statement_to_claims,
        direct_retrieval=direct_retrieval,
        projections=projection_map,
        build_report=report,
    )


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
    return IndexProjection(
        statement_id=statement_id,
        generation=1,
        retrieval_keys=(),
        support_claim_ids=support_ids,
        direct_answer_eligible=False,
        exclusion_reason=reason,
    )


def _value_tuple(value: object) -> tuple[str, ...]:
    if isinstance(value, tuple):
        return tuple(str(item) for item in value)
    if isinstance(value, RetrievalOwner):
        return (value.statement_id, value.provenance.value, str(value.generation))
    if isinstance(value, IndexProjection):
        return (value.to_json(),)
    return (str(value),)


def _compare_maps(
    index_name: str,
    expected: Mapping[MapKey, MapValue],
    actual: Mapping[MapKey, MapValue],
    issues: list[IndexCheckIssue],
) -> None:
    all_keys = sorted(set(expected) | set(actual), key=str)
    for key in all_keys:
        key_text = _key_text(key) if isinstance(key, ScopedRetrievalKey) else str(key)
        if key not in actual:
            issues.append(IndexCheckIssue(IndexCheckCategory.MISSING, index_name, key_text, _value_tuple(expected[key]), (), True))
        elif key not in expected:
            issues.append(IndexCheckIssue(IndexCheckCategory.EXTRA, index_name, key_text, (), _value_tuple(actual[key]), True))
        elif expected[key] != actual[key]:
            issues.append(
                IndexCheckIssue(
                    IndexCheckCategory.ASYMMETRIC,
                    index_name,
                    key_text,
                    _value_tuple(expected[key]),
                    _value_tuple(actual[key]),
                    True,
                )
            )


def _report_issue_signature(issue: IndexBuildIssue) -> str:
    return _json_text({"detail": issue.detail, "reason": issue.reason.value, "statement_id": issue.statement_id})


def _append_report_mismatch(
    issues: list[IndexCheckIssue],
    key: str,
    expected: tuple[str, ...],
    actual: tuple[str, ...],
) -> None:
    if expected != actual:
        issues.append(IndexCheckIssue(IndexCheckCategory.ASYMMETRIC, "build_report", key, expected, actual, True))


def _check_build_report(state: IndexState, expected: IndexState, issues: list[IndexCheckIssue]) -> None:
    """Validate every diagnostic that can be rebuilt from retained projections."""
    actual_report = state.build_report
    expected_report = expected.build_report
    for field_name in ("projection_count", "exact_key_count", "support_edge_count"):
        _append_report_mismatch(
            issues,
            field_name,
            (str(getattr(expected_report, field_name)),),
            (str(getattr(actual_report, field_name)),),
        )
    expected_collisions = tuple(_json_text(collision.to_dict()) for collision in expected_report.collisions)
    actual_collisions = tuple(_json_text(collision.to_dict()) for collision in actual_report.collisions)
    _append_report_mismatch(issues, "collisions", expected_collisions, actual_collisions)
    _append_report_mismatch(
        issues,
        "omitted_collision_count",
        (str(expected_report.omitted_collision_count),),
        (str(actual_report.omitted_collision_count),),
    )

    expected_issues = tuple(sorted(_report_issue_signature(issue) for issue in expected_report.issues if not issue.input_only))
    actual_issues = tuple(sorted(_report_issue_signature(issue) for issue in actual_report.issues if not issue.input_only))
    source_input_loss = actual_report.input_count != actual_report.projection_count
    if not source_input_loss or actual_report.omitted_issue_count == 0:
        _append_report_mismatch(issues, "derived_issues", expected_issues, actual_issues)
        if not source_input_loss:
            _append_report_mismatch(
                issues,
                "omitted_issue_count",
                (str(expected_report.omitted_issue_count),),
                (str(actual_report.omitted_issue_count),),
            )
    elif not set(actual_issues).issubset(set(expected_issues)):
        _append_report_mismatch(issues, "derived_issues", expected_issues, actual_issues)


def _check_index_state_against(state: IndexState, expected_inputs: tuple[object, ...]) -> IndexCheckReport:
    """Compare every map to one explicit clean rebuild without mutation."""
    if not isinstance(state, IndexState):
        raise InvalidRequestError("index checker state must be an IndexState")
    expected = build_index_state(expected_inputs, state.state_generation)
    issues: list[IndexCheckIssue] = []
    _compare_maps("retrieval_to_owners", expected.retrieval_to_owners, state.retrieval_to_owners, issues)
    _compare_maps("statement_to_retrieval", expected.statement_to_retrieval, state.statement_to_retrieval, issues)
    _compare_maps("claim_to_statements", expected.claim_to_statements, state.claim_to_statements, issues)
    _compare_maps("statement_to_claims", expected.statement_to_claims, state.statement_to_claims, issues)
    _compare_maps("direct_retrieval", expected.direct_retrieval, state.direct_retrieval, issues)
    _compare_maps("projections", expected.projections, state.projections, issues)
    retained_expected = build_index_state(tuple(state.projections.values()), state.state_generation)
    _check_build_report(state, retained_expected, issues)

    for statement_id, projection in sorted(expected.projections.items()):
        bindings = expected.statement_to_retrieval.get(statement_id, ())
        if not bindings:
            issues.append(
                IndexCheckIssue(
                    IndexCheckCategory.UNINDEXABLE,
                    "projection",
                    statement_id,
                    (IndexIssueReason.MISSING_IDENTITY.value,),
                    (),
                    False,
                )
            )
        if not projection.direct_answer_eligible or projection.exclusion_reason:
            issues.append(
                IndexCheckIssue(
                    IndexCheckCategory.INELIGIBLE,
                    "projection",
                    statement_id,
                    (projection.exclusion_reason or IndexIssueReason.INELIGIBLE.value,),
                    (),
                    False,
                )
            )
    for collision in expected.build_report.collisions:
        issues.append(
            IndexCheckIssue(
                IndexCheckCategory.CONFLICTING,
                "direct_retrieval",
                _key_text(collision.key),
                collision.statement_ids,
                (),
                False,
            )
        )

    ordered = tuple(sorted(issues))
    bounded = ordered[:MAX_INDEX_REPORT_ITEMS]
    return IndexCheckReport(
        consistent=not any(issue.error for issue in issues),
        checked_state_generation=state.state_generation,
        issues=bounded,
        omitted_issue_count=len(ordered) - len(bounded),
    )


def check_index_state(state: IndexState) -> IndexCheckReport:
    """Check one state against its own retained projections."""
    if not isinstance(state, IndexState):
        raise InvalidRequestError("index checker state must be an IndexState")
    return _check_index_state_against(state, tuple(state.projections.values()))


def check_index_state_against(state: IndexState, projections: Iterable[object]) -> IndexCheckReport:
    """Compare one state with an explicitly supplied authoritative set."""
    return _check_index_state_against(state, tuple(projections))


def _report_from_valid_state(
    projections: Mapping[str, IndexProjection],
    statement_to_retrieval: Mapping[str, tuple[RetrievalKeyBinding, ...]],
    statement_to_claims: Mapping[str, tuple[str, ...]],
    retrieval_to_owners: Mapping[ScopedRetrievalKey, tuple[RetrievalOwner, ...]],
) -> IndexBuildReport:
    """Refresh bounded diagnostics without reconstructing any index map."""
    issues = []
    for position, (statement_id, projection) in enumerate(sorted(projections.items())):
        bindings = statement_to_retrieval[statement_id]
        _deduplicate_bindings(projection, position, issues)
        if not bindings:
            issues.append(
                IndexBuildIssue(
                    reason=IndexIssueReason.MISSING_IDENTITY,
                    statement_id=statement_id,
                    position=position,
                    detail="projection carries no exact retrieval key",
                )
            )
        eligible = projection.direct_answer_eligible and not projection.exclusion_reason and bool(bindings)
        if not eligible:
            reason = projection.exclusion_reason or IndexIssueReason.INELIGIBLE.value
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
                    IndexBuildIssue(
                        reason=classified_reason,
                        statement_id=statement_id,
                        position=position,
                        detail=reason[:MAX_INDEX_REPORT_DETAIL_BYTES],
                    )
                )

    collisions = []
    for key in sorted(retrieval_to_owners):
        owners = retrieval_to_owners[key]
        eligible_ids = tuple(dict.fromkeys(owner.statement_id for owner in owners if owner.direct_answer_eligible))
        if len(eligible_ids) <= 1:
            continue
        bounded_ids = eligible_ids[:MAX_INDEX_LOOKUP_OWNERS]
        collisions.append(
            IndexCollisionReport(
                key=key,
                statement_ids=bounded_ids,
                truncated=len(eligible_ids) > len(bounded_ids),
            )
        )
        for statement_id in bounded_ids:
            issues.append(
                IndexBuildIssue(
                    reason=IndexIssueReason.CROSS_ARTIFACT_COLLISION,
                    statement_id=statement_id,
                    position=0,
                    detail=_key_text(key)[:MAX_INDEX_REPORT_DETAIL_BYTES],
                )
            )

    bounded_issues, omitted_issues = _bounded_issues(issues)
    ordered_collisions = tuple(collisions)
    bounded_collisions = ordered_collisions[:MAX_INDEX_REPORT_ITEMS]
    return IndexBuildReport(
        input_count=len(projections),
        projection_count=len(projections),
        exact_key_count=len(retrieval_to_owners),
        support_edge_count=sum(len(claim_ids) for claim_ids in statement_to_claims.values()),
        issues=bounded_issues,
        collisions=bounded_collisions,
        omitted_issue_count=omitted_issues,
        omitted_collision_count=len(ordered_collisions) - len(bounded_collisions),
    )


def _refresh_direct_key(
    key: ScopedRetrievalKey,
    owners: tuple[RetrievalOwner, ...],
    direct_retrieval: dict[ScopedRetrievalKey, RetrievalOwner],
) -> None:
    eligible_owners = tuple(owner for owner in owners if owner.direct_answer_eligible)
    eligible_ids = tuple(dict.fromkeys(owner.statement_id for owner in eligible_owners))
    if len(eligible_ids) == 1:
        direct_retrieval[key] = eligible_owners[0]
    else:
        direct_retrieval.pop(key, ())


def _mutated_state(
    state: IndexState,
    removed_statement_ids: tuple[str, ...],
    added_projections: tuple[IndexProjection, ...],
) -> IndexState:
    """Copy the immutable maps and alter only the named projection edges."""
    retrieval_to_owners = dict(state.retrieval_to_owners)
    statement_to_retrieval = dict(state.statement_to_retrieval)
    claim_to_statements = dict(state.claim_to_statements)
    statement_to_claims = dict(state.statement_to_claims)
    direct_retrieval = dict(state.direct_retrieval)
    projections = dict(state.projections)

    for statement_id in removed_statement_ids:
        for binding in statement_to_retrieval.pop(statement_id, ()):
            owners = tuple(owner for owner in retrieval_to_owners[binding.key] if owner.statement_id != statement_id)
            if owners:
                retrieval_to_owners[binding.key] = owners
                _refresh_direct_key(binding.key, owners, direct_retrieval)
            else:
                retrieval_to_owners.pop(binding.key, ())
                direct_retrieval.pop(binding.key, ())
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
        eligible = projection.direct_answer_eligible and not projection.exclusion_reason and bool(bindings)
        projections[projection.statement_id] = projection
        statement_to_retrieval[projection.statement_id] = bindings
        statement_to_claims[projection.statement_id] = projection.support_claim_ids
        for claim_id in projection.support_claim_ids:
            statement_ids = tuple(sorted((*claim_to_statements.get(claim_id, ()), projection.statement_id)))
            claim_to_statements[claim_id] = statement_ids
        for binding in bindings:
            owner = RetrievalOwner(
                statement_id=projection.statement_id,
                generation=projection.generation,
                provenance=binding.origin,
                representation=binding.representation,
                direct_answer_eligible=eligible,
            )
            owners = tuple(sorted((*retrieval_to_owners.get(binding.key, ()), owner)))
            retrieval_to_owners[binding.key] = owners
            _refresh_direct_key(binding.key, owners, direct_retrieval)

    report = _report_from_valid_state(projections, statement_to_retrieval, statement_to_claims, retrieval_to_owners)
    return IndexState(
        state_generation=state.state_generation + 1,
        retrieval_to_owners=retrieval_to_owners,
        statement_to_retrieval=statement_to_retrieval,
        claim_to_statements=claim_to_statements,
        statement_to_claims=statement_to_claims,
        direct_retrieval=direct_retrieval,
        projections=projections,
        build_report=report,
    )


def add_index_projection(state: IndexState, projection: IndexProjection) -> IndexState:
    if not isinstance(projection, IndexProjection):
        raise InvalidRequestError("projection must be an IndexProjection")
    if projection.statement_id in state.projections:
        raise ConflictError(f"index projection already exists: {projection.statement_id}")
    return _mutated_state(state, (), (projection,))


def replace_index_projection(state: IndexState, projection: IndexProjection) -> IndexState:
    if not isinstance(projection, IndexProjection):
        raise InvalidRequestError("projection must be an IndexProjection")
    if projection.statement_id not in state.projections:
        raise InvalidRequestError(f"index projection does not exist: {projection.statement_id}")
    return _mutated_state(state, (projection.statement_id,), (projection,))


def remove_index_projection(state: IndexState, statement_id: str) -> IndexState:
    validated_id = _bounded_text(statement_id, "index projection statement_id", MAX_INDEX_STATEMENT_ID_BYTES, allow_empty=False)
    if validated_id not in state.projections:
        raise InvalidRequestError(f"index projection does not exist: {validated_id}")
    return _mutated_state(state, (validated_id,), ())


def update_index_support(state: IndexState, statement_id: str, support_claim_ids: tuple[str, ...]) -> IndexState:
    validated_id = _bounded_text(statement_id, "index projection statement_id", MAX_INDEX_STATEMENT_ID_BYTES, allow_empty=False)
    projection = state.projections.get(validated_id)
    if not projection:
        raise InvalidRequestError(f"index projection does not exist: {validated_id}")
    updated = replace(projection, generation=projection.generation + 1, support_claim_ids=support_claim_ids)
    return replace_index_projection(state, updated)


def _states_equivalent(left: IndexState, right: IndexState) -> bool:
    return (
        left.retrieval_to_owners == right.retrieval_to_owners
        and left.statement_to_retrieval == right.statement_to_retrieval
        and left.claim_to_statements == right.claim_to_statements
        and left.statement_to_claims == right.statement_to_claims
        and left.direct_retrieval == right.direct_retrieval
        and left.projections == right.projections
    )


def _mutation_is_consistent(before: IndexState, candidate: IndexState, statement_id: str) -> bool:
    """Check every forward, inverse, and direct edge touched by one mutation."""
    touched_keys = {
        binding.key
        for binding in (
            *before.statement_to_retrieval.get(statement_id, ()),
            *candidate.statement_to_retrieval.get(statement_id, ()),
        )
    }
    touched_claims = {
        *before.statement_to_claims.get(statement_id, ()),
        *candidate.statement_to_claims.get(statement_id, ()),
    }
    for key in touched_keys:
        owners = candidate.retrieval_to_owners.get(key, ())
        projection = candidate.projections.get(statement_id)
        for binding in candidate.statement_to_retrieval.get(statement_id, ()):
            if binding.key != key or not projection:
                continue
            eligible = projection.direct_answer_eligible and not projection.exclusion_reason
            expected_owner = RetrievalOwner(
                statement_id=statement_id,
                generation=projection.generation,
                provenance=binding.origin,
                representation=binding.representation,
                direct_answer_eligible=eligible,
            )
            if expected_owner not in owners:
                return False
        for owner in owners:
            reverse_keys = {binding.key for binding in candidate.statement_to_retrieval.get(owner.statement_id, ())}
            if key not in reverse_keys:
                return False
        eligible_owners = tuple(owner for owner in owners if owner.direct_answer_eligible)
        eligible_ids = tuple(dict.fromkeys(owner.statement_id for owner in eligible_owners))
        selected = candidate.direct_retrieval.get(key)
        if len(eligible_ids) == 1:
            if not selected or selected.statement_id != eligible_ids[0]:
                return False
        elif selected:
            return False
    for claim_id in touched_claims:
        statement_ids = candidate.claim_to_statements.get(claim_id, ())
        for owner_id in statement_ids:
            if claim_id not in candidate.statement_to_claims.get(owner_id, ()):
                return False
        if claim_id in candidate.statement_to_claims.get(statement_id, ()) and statement_id not in statement_ids:
            return False
        if statement_id in statement_ids and claim_id not in candidate.statement_to_claims.get(statement_id, ()):
            return False
    projection_present = statement_id in candidate.projections
    return (
        projection_present == (statement_id in candidate.statement_to_retrieval) == (statement_id in candidate.statement_to_claims)
    )


class IndexOwner:
    """Atomic owner of the currently visible immutable index snapshot."""

    def __init__(self, projections: Iterable[object] = ()) -> None:
        self._lock = threading.RLock()
        candidate = build_index_state(projections)
        if not check_index_state(candidate).consistent:
            raise ConflictError("initial index state failed consistency checking")
        self._state = candidate

    def snapshot(self) -> IndexState:
        with self._lock:
            return self._state

    def check(self) -> IndexCheckReport:
        return check_index_state(self.snapshot())

    def check_against(self, projections: Iterable[object]) -> IndexCheckReport:
        return check_index_state_against(self.snapshot(), projections)

    def atomic_swap(self, candidate: IndexState, expected_state_generation: int) -> IndexState:
        report = check_index_state(candidate)
        if not report.consistent:
            raise ConflictError("candidate index state failed consistency checking")
        with self._lock:
            if self._state.state_generation != expected_state_generation:
                raise ConflictError(
                    f"stale index state generation: expected {expected_state_generation}, found {self._state.state_generation}"
                )
            self._state = candidate
            return self._state

    def add(self, projection: IndexProjection) -> IndexState:
        with self._lock:
            candidate = add_index_projection(self._state, projection)
            if not _mutation_is_consistent(self._state, candidate, projection.statement_id):
                raise ConflictError("candidate add state failed consistency checking")
            self._state = candidate
            return self._state

    def replace(self, projection: IndexProjection) -> IndexState:
        with self._lock:
            candidate = replace_index_projection(self._state, projection)
            if not _mutation_is_consistent(self._state, candidate, projection.statement_id):
                raise ConflictError("candidate replacement state failed consistency checking")
            self._state = candidate
            return self._state

    def remove(self, statement_id: str) -> IndexState:
        with self._lock:
            candidate = remove_index_projection(self._state, statement_id)
            if not _mutation_is_consistent(self._state, candidate, statement_id):
                raise ConflictError("candidate removal state failed consistency checking")
            self._state = candidate
            return self._state

    def update_support(self, statement_id: str, support_claim_ids: tuple[str, ...]) -> IndexState:
        with self._lock:
            candidate = update_index_support(self._state, statement_id, support_claim_ids)
            if not _mutation_is_consistent(self._state, candidate, statement_id):
                raise ConflictError("candidate support-update state failed consistency checking")
            self._state = candidate
            return self._state

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

        if not isinstance(key, ScopedRetrievalKey):
            raise InvalidRequestError("exact refresh key must be a ScopedRetrievalKey")
        if not isinstance(refreshed_projections, tuple) or not all(
            isinstance(projection, IndexProjection) for projection in refreshed_projections
        ):
            raise InvalidRequestError("refreshed_projections must be a tuple of IndexProjection values")
        if isinstance(expected_state_generation, bool) or not isinstance(expected_state_generation, int):
            raise InvalidRequestError("expected_state_generation must be an integer")
        with self._lock:
            if self._state.state_generation != expected_state_generation:
                raise ConflictError(
                    f"stale index state generation: expected {expected_state_generation}, found {self._state.state_generation}"
                )
            owner_ids = tuple(dict.fromkeys(owner.statement_id for owner in self._state.retrieval_to_owners.get(key, ())))
            refreshed_ids = tuple(dict.fromkeys(projection.statement_id for projection in refreshed_projections))
            if len(refreshed_ids) != len(refreshed_projections):
                raise InvalidRequestError("refreshed_projections must contain unique statement IDs")
            if set(refreshed_ids) != set(owner_ids):
                raise ConflictError("exact refresh must cover every current owner and no unrelated projection")
            replacements = {projection.statement_id: projection for projection in refreshed_projections}
            for statement_id in owner_ids:
                current = self._state.projections[statement_id]
                refreshed = replacements[statement_id]
                if (
                    current.statement_id != refreshed.statement_id
                    or current.generation != refreshed.generation
                    or current.retrieval_keys != refreshed.retrieval_keys
                    or current.support_claim_ids != refreshed.support_claim_ids
                    or current.normalization_version != refreshed.normalization_version
                    or current.schema_version != refreshed.schema_version
                ):
                    raise InvalidRequestError("exact refresh may change only direct_answer_eligible and exclusion_reason")
            changed = any(self._state.projections[statement_id] != replacements[statement_id] for statement_id in owner_ids)
            if changed:
                projections = dict(self._state.projections)
                projections.update(replacements)
                candidate = build_index_state(projections.values(), self._state.state_generation + 1)
                if not check_index_state(candidate).consistent:
                    raise ConflictError("candidate exact refresh state failed consistency checking")
                self._state = candidate
            return self._state.exact_lookup(key), changed, self._state.state_generation

    def repair(self, projections: Iterable[object], *, dry_run: bool = True) -> IndexRepairResult:
        if not isinstance(dry_run, bool):
            raise InvalidRequestError("index repair dry_run must be a boolean")
        before = self.snapshot()
        candidate = build_index_state(projections, before.state_generation + 1)
        candidate_check = check_index_state(candidate)
        if not candidate_check.consistent:
            raise ConflictError("candidate repair state failed consistency checking")
        live_check = check_index_state_against(before, projections)
        changed = not _states_equivalent(before, candidate)
        if not dry_run:
            self.atomic_swap(candidate, before.state_generation)
        return IndexRepairResult(
            applied=not dry_run,
            changed=changed,
            before_generation=before.state_generation,
            after_generation=candidate.state_generation if not dry_run else before.state_generation,
            candidate_report=candidate.build_report,
            live_check=live_check,
        )
