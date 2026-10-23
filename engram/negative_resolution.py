"""Memory-only bounded negative-resolution contracts and expiry ownership."""

from datetime import datetime, timedelta
from heapq import heapify as heapq_heapify, heappop as heapq_heappop, heappush as heapq_heappush
from threading import RLock as threading_RLock

from engram.constants_feedback import (
    DEFAULT_NEGATIVE_MAX_RECORDS,
    DEFAULT_NEGATIVE_TTL_SECONDS,
    EMPTY_FINGERPRINT,
    EMPTY_NEGATIVE_CREATED_AT,
    EMPTY_NEGATIVE_EXPIRES_AT,
    MAX_INSPECTION_RECORDS,
    MAX_NEGATIVE_RECORDS,
    MAX_NEGATIVE_TTL_SECONDS,
    NEGATIVE_RESOLUTION_FIELDS,
    NEGATIVE_RESOLUTION_KEY_FIELDS,
    NegativeResolutionReason,
)
from engram.constants_identity import EMPTY_SCOPE_KEY
from engram.errors import IdentityValidationError, InvalidRequestError
from engram.feedback_values import (
    canonical_fingerprint,
    canonical_utc,
    exact_mapping,
    internal_diagnostic_id,
    internal_fingerprint,
    internal_integer,
)
from engram.identity import query_identity, query_identity_to_dict, scope_key_to_dict, validate_query_identity, validate_scope_key
from engram.validation import require_bool, require_utc_datetime, utc_datetime


def negative_resolution_key(
    query_identity: object,
    scope: object,
    constraint_fingerprint: object,
    resolver_plan_fingerprint: object,
    capability_readiness_fingerprint: object,
    policy_fingerprint: object,
) -> dict:
    """Build the exact knowledge and policy state for one observed miss."""
    try:
        validated_query_identity = validate_query_identity(query_identity)
    except IdentityValidationError as err:
        raise InvalidRequestError("negative query_identity must be QueryIdentity") from err
    try:
        validated_scope = validate_scope_key(scope)
    except IdentityValidationError as err:
        raise InvalidRequestError("negative scope must match query identity scope") from err
    if validated_query_identity.get("scope", {}) != validated_scope:
        raise InvalidRequestError("negative scope must match query identity scope")
    validated_constraint = internal_fingerprint(constraint_fingerprint, "negative constraint_fingerprint")
    validated_plan = internal_fingerprint(resolver_plan_fingerprint, "negative resolver_plan_fingerprint")
    validated_readiness = internal_fingerprint(capability_readiness_fingerprint, "negative capability_readiness_fingerprint")
    validated_policy = internal_fingerprint(policy_fingerprint, "negative policy_fingerprint")
    result: dict = {
        "query_identity": validated_query_identity,
        "scope": validated_scope,
        "constraint_fingerprint": validated_constraint,
        "resolver_plan_fingerprint": validated_plan,
        "capability_readiness_fingerprint": validated_readiness,
        "policy_fingerprint": validated_policy,
    }
    return result


def validate_negative_resolution_key(value: object) -> dict:
    data = exact_mapping(value, "NegativeResolutionKey", NEGATIVE_RESOLUTION_KEY_FIELDS)
    result = negative_resolution_key(
        data.get("query_identity", {}),
        data.get("scope", {}),
        data.get("constraint_fingerprint", ""),
        data.get("resolver_plan_fingerprint", ""),
        data.get("capability_readiness_fingerprint", ""),
        data.get("policy_fingerprint", ""),
    )
    return result


def negative_resolution_key_to_dict(value: object) -> dict:
    current = validate_negative_resolution_key(value)
    result = {
        "query_identity": query_identity_to_dict(current.get("query_identity", {})),
        "scope": scope_key_to_dict(current.get("scope", {})),
        "constraint_fingerprint": current.get("constraint_fingerprint", ""),
        "resolver_plan_fingerprint": current.get("resolver_plan_fingerprint", ""),
        "capability_readiness_fingerprint": current.get("capability_readiness_fingerprint", ""),
        "policy_fingerprint": current.get("policy_fingerprint", ""),
    }
    return result


def negative_resolution_key_fingerprint(value: object) -> str:
    data = negative_resolution_key_to_dict(value)
    result = canonical_fingerprint(data)
    return result


def negative_resolution_key_relationship_fingerprint(value: object) -> str:
    current = validate_negative_resolution_key(value)
    data = {
        "query_identity": query_identity_to_dict(current.get("query_identity", {})),
        "scope": scope_key_to_dict(current.get("scope", {})),
        "constraint_fingerprint": current.get("constraint_fingerprint", ""),
    }
    result = canonical_fingerprint(data)
    return result


def negative_resolution(
    key: object,
    reason: object,
    created_at: object,
    expires_at: object,
    hit_count: object = 0,
) -> dict:
    try:
        validated_key = validate_negative_resolution_key(key)
    except InvalidRequestError as err:
        raise InvalidRequestError("negative resolution key has an invalid type") from err
    if not isinstance(reason, NegativeResolutionReason):
        raise InvalidRequestError("negative resolution reason has an invalid type")
    created = require_utc_datetime(created_at, "negative created_at")
    expires = require_utc_datetime(expires_at, "negative expires_at")
    if expires <= created:
        raise InvalidRequestError("negative expires_at must be after created_at")
    validated_created_at = canonical_utc(created)
    validated_expires_at = canonical_utc(expires)
    validated_hit_count = internal_integer(hit_count, "negative hit_count", 0)
    result: dict = {
        "key": validated_key,
        "reason": reason,
        "created_at": validated_created_at,
        "expires_at": validated_expires_at,
        "hit_count": validated_hit_count,
    }
    return result


def empty_negative_resolution() -> dict:
    """Return a fresh, validated copy of the documented null NegativeResolution.

    Lookup misses and unavailable negative keys use this concrete null object
    instead of an absent value; each call returns an isolated record.
    """
    key = negative_resolution_key(
        query_identity=query_identity("unavailable", scope=EMPTY_SCOPE_KEY),
        scope=EMPTY_SCOPE_KEY,
        constraint_fingerprint=EMPTY_FINGERPRINT,
        resolver_plan_fingerprint=EMPTY_FINGERPRINT,
        capability_readiness_fingerprint=EMPTY_FINGERPRINT,
        policy_fingerprint=EMPTY_FINGERPRINT,
    )
    result = negative_resolution(
        key,
        NegativeResolutionReason.INSUFFICIENT_KNOWLEDGE,
        EMPTY_NEGATIVE_CREATED_AT,
        EMPTY_NEGATIVE_EXPIRES_AT,
    )
    return result


def validate_negative_resolution(value: object) -> dict:
    data = exact_mapping(value, "NegativeResolution", NEGATIVE_RESOLUTION_FIELDS)
    result = negative_resolution(
        data.get("key", {}),
        data.get("reason", NegativeResolutionReason.INSUFFICIENT_KNOWLEDGE),
        data.get("created_at", ""),
        data.get("expires_at", ""),
        data.get("hit_count", 0),
    )
    return result


def negative_resolution_with_changes(value: object, changes: object) -> dict:
    current = validate_negative_resolution(value)
    if not isinstance(changes, dict) or not set(changes).issubset(NEGATIVE_RESOLUTION_FIELDS):
        raise InvalidRequestError("negative resolution changes contain invalid fields")
    updated: dict = dict(current)
    updated.update(changes)
    result = validate_negative_resolution(updated)
    return result


def negative_lookup(hit: object, record: object = {}) -> dict:
    validated_hit = require_bool(hit, "negative lookup hit")
    if not isinstance(record, dict):
        raise InvalidRequestError("negative lookup record must be NegativeResolution")
    try:
        validated_record = empty_negative_resolution() if not record else validate_negative_resolution(record)
    except InvalidRequestError as err:
        raise InvalidRequestError("negative lookup record must be NegativeResolution") from err
    if not validated_hit and validated_record != empty_negative_resolution():
        raise InvalidRequestError("negative lookup miss must use the concrete empty record")
    result: dict = {"hit": validated_hit, "record": validated_record}
    return result


class NegativeResolutionStore:
    """Memory-only bounded, non-sliding negative-resolution owner.

    Every admitted record keeps its relationship fingerprint and expiry instant
    in owning indexes, so a lookup touches only due expiries and its own
    relationship instead of reparsing and fingerprinting every retained entry.
    The TTL is fixed per store, so expiry order is also creation order, and the
    one expiry heap also yields the oldest record for capacity eviction.
    """

    def __init__(
        self,
        max_records: int = DEFAULT_NEGATIVE_MAX_RECORDS,
        ttl_seconds: int = DEFAULT_NEGATIVE_TTL_SECONDS,
    ) -> None:
        self.max_records = internal_integer(max_records, "negative max_records", 1, MAX_NEGATIVE_RECORDS)
        self.ttl_seconds = internal_integer(ttl_seconds, "negative ttl_seconds", 1, MAX_NEGATIVE_TTL_SECONDS)
        self.internal_lock = threading_RLock()
        self.internal_records: dict[str, dict] = {}
        self.internal_record_relationships: dict[str, str] = {}
        self.internal_relationships: dict[str, dict[str, bool]] = {}
        # (expiry instant, record fingerprint). Entries of removed records stay
        # until popped or compacted; a popped entry acts only while its record
        # still carries that expiry.
        self.internal_expiry_heap: list[tuple[datetime, str]] = []
        self.internal_metrics = {
            "lookups": 0,
            "hits": 0,
            "misses": 0,
            "admissions": 0,
            "expiries": 0,
            "evictions": 0,
            "invalidations": 0,
        }

    def remove_record(self, fingerprint: str) -> None:
        """Remove one record and its relationship index entry."""
        self.internal_records.pop(fingerprint, {})
        relationship = self.internal_record_relationships.pop(fingerprint, "")
        members = self.internal_relationships.get(relationship, {})
        members.pop(fingerprint, False)
        if not members:
            self.internal_relationships.pop(relationship, {})

    def live_heap_entry(self, entry: tuple) -> bool:
        record = self.internal_records.get(entry[1], {})
        result = bool(record) and utc_datetime(record.get("expires_at", "")) == entry[0]
        return result

    def expire(self, at: datetime) -> None:
        heap = self.internal_expiry_heap
        while heap and heap[0][0] <= at:
            entry = heapq_heappop(heap)
            if self.live_heap_entry(entry):
                self.remove_record(entry[1])
                self.internal_metrics["expiries"] = self.internal_metrics.get("expiries", 0) + 1

    def invalidate_related(self, fingerprint: str, relationship: str) -> None:
        """Remove every other record of this relationship (changed plan or policy state)."""
        stale = [member for member in self.internal_relationships.get(relationship, {}) if member != fingerprint]
        for member in stale:
            self.remove_record(member)
            self.internal_metrics["invalidations"] = self.internal_metrics.get("invalidations", 0) + 1
        # Removed records leave heap entries behind; rebuild once they outnumber
        # the live records, so the heap stays proportional to the store.
        if len(self.internal_expiry_heap) > 2 * len(self.internal_records):
            self.internal_expiry_heap = [entry for entry in self.internal_expiry_heap if self.live_heap_entry(entry)]
            heapq_heapify(self.internal_expiry_heap)

    def lookup(self, key: dict, evaluation_time: str) -> dict:
        """Look up a negative resolution for ``key``; the read also maintains the store.

        Records expired at ``evaluation_time`` are removed first, then every other record
        for the same relationship is invalidated. A remaining exact record counts a hit,
        increments its ``hit_count`` and is returned; otherwise the lookup is a miss.
        """
        try:
            validated_key = validate_negative_resolution_key(key)
        except InvalidRequestError as err:
            raise InvalidRequestError("negative lookup key must be NegativeResolutionKey") from err
        at = require_utc_datetime(evaluation_time, "negative lookup evaluation_time")
        fingerprint = negative_resolution_key_fingerprint(validated_key)
        relationship = negative_resolution_key_relationship_fingerprint(validated_key)
        with self.internal_lock:
            self.internal_metrics["lookups"] = self.internal_metrics.get("lookups", 0) + 1
            self.expire(at)
            self.invalidate_related(fingerprint, relationship)
            if fingerprint not in self.internal_records:
                self.internal_metrics["misses"] = self.internal_metrics.get("misses", 0) + 1
                result = negative_lookup(False)
                return result
            record = self.internal_records.get(fingerprint, {})
            updated = negative_resolution_with_changes(record, {"hit_count": record.get("hit_count", 0) + 1})
            self.internal_records[fingerprint] = updated
            self.internal_metrics["hits"] = self.internal_metrics.get("hits", 0) + 1
            result = negative_lookup(True, updated)
            return result

    def admit(self, key: dict, evaluation_time: str) -> dict:
        try:
            validated_key = validate_negative_resolution_key(key)
        except InvalidRequestError as err:
            raise InvalidRequestError("negative admission key must be NegativeResolutionKey") from err
        at = require_utc_datetime(evaluation_time, "negative admission evaluation_time")
        fingerprint = negative_resolution_key_fingerprint(validated_key)
        relationship = negative_resolution_key_relationship_fingerprint(validated_key)
        with self.internal_lock:
            self.expire(at)
            self.invalidate_related(fingerprint, relationship)
            if fingerprint in self.internal_records:
                result = validate_negative_resolution(self.internal_records.get(fingerprint, {}))
                return result
            created_at = canonical_utc(at)
            record = negative_resolution(
                validated_key,
                NegativeResolutionReason.INSUFFICIENT_KNOWLEDGE,
                created_at,
                canonical_utc(at + timedelta(seconds=self.ttl_seconds)),
            )
            self.internal_records[fingerprint] = record
            self.internal_record_relationships[fingerprint] = relationship
            self.internal_relationships.setdefault(relationship, {})[fingerprint] = True
            heapq_heappush(self.internal_expiry_heap, (utc_datetime(record.get("expires_at", "")), fingerprint))
            self.internal_metrics["admissions"] = self.internal_metrics.get("admissions", 0) + 1
            while len(self.internal_records) > self.max_records:
                entry = heapq_heappop(self.internal_expiry_heap)
                if self.live_heap_entry(entry):
                    self.remove_record(entry[1])
                    self.internal_metrics["evictions"] = self.internal_metrics.get("evictions", 0) + 1
            result = validate_negative_resolution(record)
            return result

    def invalidate_for_key(self, key: dict) -> int:
        """Invalidate prior state for one relationship under changed policy state."""

        try:
            validated_key = validate_negative_resolution_key(key)
        except InvalidRequestError as err:
            raise InvalidRequestError("negative invalidation key must be NegativeResolutionKey") from err
        fingerprint = negative_resolution_key_fingerprint(validated_key)
        relationship = negative_resolution_key_relationship_fingerprint(validated_key)
        with self.internal_lock:
            before = len(self.internal_records)
            self.invalidate_related(fingerprint, relationship)
            removed = before - len(self.internal_records)
            return removed

    def clear(self) -> None:
        with self.internal_lock:
            removed = len(self.internal_records)
            self.internal_records.clear()
            self.internal_record_relationships.clear()
            self.internal_relationships.clear()
            self.internal_expiry_heap.clear()
            self.internal_metrics["invalidations"] = self.internal_metrics.get("invalidations", 0) + removed

    def inspect(self, limit: int = MAX_INSPECTION_RECORDS) -> dict:
        internal_integer(limit, "negative inspection limit", 1, MAX_INSPECTION_RECORDS)
        with self.internal_lock:
            records = sorted(self.internal_records.items())
            result = {
                "memory_only": True,
                "ttl_seconds": self.ttl_seconds,
                "max_records": self.max_records,
                "record_count": len(records),
                **self.internal_metrics,
                "records": [
                    {
                        "diagnostic_id": internal_diagnostic_id(fingerprint),
                        "reason": record.get("reason", NegativeResolutionReason.INSUFFICIENT_KNOWLEDGE).value,
                        "created_at": record.get("created_at", ""),
                        "expires_at": record.get("expires_at", ""),
                        "hit_count": record.get("hit_count", 0),
                    }
                    for fingerprint, record in records[:limit]
                ],
                "omitted_record_count": max(0, len(records) - limit),
            }
            return result
