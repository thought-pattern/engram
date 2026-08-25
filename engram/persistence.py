"""Persistence functionality for ENGRAM.

This module provides save/load functionality for serializing and
deserializing ENGRAM state to/from JSON files and strings.
"""

import contextlib
import copy
import json
import os
from collections import Counter
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path

from engram.artifacts import (
    CachedResponseArtifact,
    LifecycleState,
    artifact_provenance,
    artifact_statistics,
    cached_response_artifact,
    cached_response_artifact_from_dict,
    cached_response_artifact_to_dict,
)
from engram.config import config_from_dict, config_to_dict, engram_config
from engram.constants import (
    EMPTY_CONFIG,
    FEEDBACK_POLICY_SCHEMA_VERSION,
    FEEDBACK_POLICY_VERSION,
    FEEDBACK_STATE_SCHEMA_VERSION,
    FUSION_POLICY_SCHEMA_VERSION,
    FUSION_POLICY_VERSION,
    IDENTITY_SCHEMA_VERSION,
    INDEX_STATE_SCHEMA_VERSION,
    LEGACY_PERSISTENCE_VERSION,
    MAX_QUARANTINE_DETAIL_BYTES,
    MAX_QUARANTINE_RECORDS,
    PERSISTENCE_MANIFEST_FIELDS,
    PERSISTENCE_MANIFEST_SCHEMA_VERSION,
    PERSISTENCE_STATUS_SCHEMA_VERSION,
    PERSISTENCE_VERSION,
    RESPONSE_QUARANTINE_RECORD_FIELDS,
    RESPONSE_STATE_SCHEMA_VERSION,
    RETRIEVAL_NORMALIZATION_VERSION,
    SEMANTIC_INDEX_SCHEMA_VERSION,
    SEMANTIC_INDEX_VERSION,
    SPARSE_INDEX_SCHEMA_VERSION,
    SPARSE_INDEX_VERSION,
    ResponseQuarantineReason,
)
from engram.coordination import (
    CoordinatedResponseState,
    coordinated_response_state as build_coordinated_response_state,
    coordinated_response_state_to_dict,
    validate_coordinated_response_state,
)
from engram.core import Engram
from engram.eligibility import NamespaceEpochState, namespace_epoch_state_from_snapshot
from engram.errors import InvalidRequestError
from engram.feedback import FeedbackState, FeedbackStore, feedback_state_from_dict, feedback_state_to_dict, validate_feedback_state
from engram.identity import (
    build_retrieval_representation,
    build_standalone_identity,
    retrieval_representation_bindings,
    scope_key,
    scoped_retrieval_key_to_json,
)
from engram.models import (
    keyword_entry,
    keyword_entry_from_dict,
    keyword_entry_to_dict,
    session_from_dict,
    session_to_dict,
    statement_from_dict,
    statement_to_dict,
)
from engram.mutations import MutationReceiptLedger, mutation_receipt_ledger_from_snapshot
from engram.repository import ArtifactRepository


def _bounded_quarantine_text(value: object, name: str, allow_empty: bool) -> str:
    if not isinstance(value, str):
        raise InvalidRequestError(f"{name} must be a string")
    if not allow_empty and not value:
        raise InvalidRequestError(f"{name} must not be empty")
    if len(value.encode("utf-8")) > MAX_QUARANTINE_DETAIL_BYTES:
        raise InvalidRequestError(f"{name} exceeds the UTF-8 limit of {MAX_QUARANTINE_DETAIL_BYTES} bytes")
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise InvalidRequestError(f"{name} contains a control character")
    return value


ResponseQuarantineRecord = dict
PersistenceManifest = dict
PersistenceStatus = dict


def response_quarantine_record(
    statement_id: str,
    reason: ResponseQuarantineReason,
    detail: str,
) -> ResponseQuarantineRecord:
    """Build one validated legacy-response quarantine dictionary."""
    normalized_statement_id = _bounded_quarantine_text(statement_id, "quarantine statement_id", False)
    if not isinstance(reason, ResponseQuarantineReason):
        raise InvalidRequestError("quarantine reason must be a ResponseQuarantineReason")
    normalized_detail = _bounded_quarantine_text(detail, "quarantine detail", True)
    result: ResponseQuarantineRecord = {
        "statement_id": normalized_statement_id,
        "reason": reason,
        "detail": normalized_detail,
    }
    return result


def validate_response_quarantine_record(value: object) -> ResponseQuarantineRecord:
    """Validate and copy one quarantine dictionary."""
    if not isinstance(value, Mapping) or set(value) != RESPONSE_QUARANTINE_RECORD_FIELDS:
        raise InvalidRequestError("ResponseQuarantineRecord must contain exactly statement_id, reason, and detail")
    statement_id = value.get("statement_id", ())
    reason = value.get("reason", ())
    detail = value.get("detail", ())
    if not isinstance(statement_id, str) or not isinstance(reason, ResponseQuarantineReason) or not isinstance(detail, str):
        raise InvalidRequestError("quarantine record fields are malformed")
    result = response_quarantine_record(statement_id, reason, detail)
    return result


def response_quarantine_record_to_dict(value: object) -> dict[str, object]:
    """Return the exact persistent dictionary for one quarantine record."""
    record = validate_response_quarantine_record(value)
    result: dict[str, object] = {
        "statement_id": record["statement_id"],
        "reason": record["reason"].value,
        "detail": record["detail"],
    }
    return result


def response_quarantine_record_from_dict(value: object) -> ResponseQuarantineRecord:
    """Decode one quarantine record from its exact persistent dictionary."""
    if not isinstance(value, Mapping) or set(value) != RESPONSE_QUARANTINE_RECORD_FIELDS:
        raise InvalidRequestError("ResponseQuarantineRecord must contain exactly statement_id, reason, and detail")
    try:
        reason = ResponseQuarantineReason(value["reason"])
    except (TypeError, ValueError) as error:
        raise InvalidRequestError("quarantine record contains an unsupported reason") from error
    record = response_quarantine_record(
        statement_id=_bounded_quarantine_text(value["statement_id"], "quarantine statement_id", False),
        reason=reason,
        detail=_bounded_quarantine_text(value["detail"], "quarantine detail", True),
    )
    return record


def response_quarantine_record_key(value: ResponseQuarantineRecord) -> tuple[str, str, str]:
    """Return the deterministic order key for one validated quarantine record."""
    record = validate_response_quarantine_record(value)
    key = (record["statement_id"], record["reason"].value, record["detail"])
    return key


def validate_response_quarantine_records(value: object) -> tuple[ResponseQuarantineRecord, ...]:
    """Validate, bound, and deterministically order quarantine records."""
    if not isinstance(value, tuple):
        raise InvalidRequestError("response_quarantine must be a tuple of ResponseQuarantineRecord values")
    if len(value) > MAX_QUARANTINE_RECORDS:
        raise InvalidRequestError(f"response quarantine exceeds the limit of {MAX_QUARANTINE_RECORDS}")
    records = tuple(validate_response_quarantine_record(record) for record in value)
    ordered = tuple(sorted(records, key=response_quarantine_record_key))
    return ordered


def persistence_manifest(config: dict) -> PersistenceManifest:
    """Return the bounded cross-feature version manifest for one runtime config."""
    semantic = config.get("semantic") or {}
    reranker = config.get("reranker") or {}
    graph = config.get("graph") or {}
    result: PersistenceManifest = {
        "schema_version": PERSISTENCE_MANIFEST_SCHEMA_VERSION,
        "persistence_version": PERSISTENCE_VERSION,
        "response_state_schema_version": RESPONSE_STATE_SCHEMA_VERSION,
        "feedback_state_schema_version": FEEDBACK_STATE_SCHEMA_VERSION,
        "identity_schema_version": IDENTITY_SCHEMA_VERSION,
        "retrieval_normalization_version": RETRIEVAL_NORMALIZATION_VERSION,
        "index_state_schema_version": INDEX_STATE_SCHEMA_VERSION,
        "sparse_index_schema_version": SPARSE_INDEX_SCHEMA_VERSION,
        "sparse_index_version": SPARSE_INDEX_VERSION,
        "semantic_index_schema_version": SEMANTIC_INDEX_SCHEMA_VERSION,
        "semantic_index_version": SEMANTIC_INDEX_VERSION,
        "fusion_policy_schema_version": FUSION_POLICY_SCHEMA_VERSION,
        "fusion_policy_version": FUSION_POLICY_VERSION,
        "feedback_policy_schema_version": FEEDBACK_POLICY_SCHEMA_VERSION,
        "feedback_policy_version": FEEDBACK_POLICY_VERSION,
        "semantic_model_id": semantic.get("model_id", ""),
        "semantic_model_version": semantic.get("model_version", ""),
        "semantic_normalization_version": semantic.get("normalization_version", 0),
        "reranker_model_version": reranker.get("model_version", ""),
        "graph_vector_model_id": graph.get("vector_model", ""),
    }
    return result


def _validated_persistence_manifest(value: object, config: dict) -> PersistenceManifest:
    if not isinstance(value, Mapping) or set(value) != PERSISTENCE_MANIFEST_FIELDS:
        raise InvalidRequestError("persistence manifest fields are malformed")
    expected = persistence_manifest(config)
    mismatched = [
        name
        for name in sorted(PERSISTENCE_MANIFEST_FIELDS)
        if type(value[name]) is not type(expected[name]) or value[name] != expected[name]
    ]
    if mismatched:
        raise InvalidRequestError(f"persistence manifest is incompatible: {', '.join(mismatched)}")
    result = dict(value)
    return result


def _persistence_status(
    instance,
    source_version: int,
    source_manifest: PersistenceManifest,
    manifest_present: bool,
) -> PersistenceStatus:
    quarantine = validate_response_quarantine_records(instance.response_quarantine)
    reason_counts = Counter(record["reason"].value for record in quarantine)
    active_manifest = persistence_manifest(instance.config)
    result: PersistenceStatus = {
        "schema_version": PERSISTENCE_STATUS_SCHEMA_VERSION,
        "ready": True,
        "source_available": True,
        "source_version": source_version,
        "current_version": PERSISTENCE_VERSION,
        "migration_required": source_version != PERSISTENCE_VERSION or not manifest_present,
        "manifest_present": manifest_present,
        "runtime_manifest_matches_source": bool(source_manifest) and source_manifest == active_manifest,
        "quarantine_count": len(quarantine),
        "quarantine_reasons": dict(sorted(reason_counts.items())),
        "derived_state_rebuilt": True,
        "manifest": active_manifest,
    }
    return result


def _write_json_atomic(path, state: dict) -> None:
    """Write JSON to path atomically via a temporary file and rename."""
    tmp_path = f"{path}.tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)
    with contextlib.suppress(OSError):
        os.chmod(tmp_path, 0o600)
    os.replace(tmp_path, path)
    with contextlib.suppress(OSError):
        os.chmod(path, 0o600)


def save(engram, path) -> None:
    """Save complete state to JSON file (atomically).

    Args:
        engram: Engram instance.
        path: File path to write.
    """
    state = to_dict(engram)
    _write_json_atomic(path, state)


def _thaw_response_value(value: object) -> object:
    if isinstance(value, Mapping):
        result = {key: _thaw_response_value(item) for key, item in value.items()}
        return result
    if isinstance(value, tuple):
        result = [_thaw_response_value(item) for item in value]
        return result
    return value


def _thaw_response_mapping(value: object) -> dict[str, object]:
    thawed = _thaw_response_value(value)
    if not isinstance(thawed, dict):
        raise InvalidRequestError("response compatibility statement must be an object")
    return thawed


def to_dict_with_response_state(engram, response_state: CoordinatedResponseState) -> dict:
    """Serialize a complete off-live coordinated response candidate."""

    validated_response_state = validate_coordinated_response_state(response_state)
    repository = validated_response_state["repository"]
    state = to_dict(engram)
    previous_ids = set(engram.response_repository.snapshot()["artifacts"])
    retained_statements = [statement for statement in state["statements"] if statement["id"] not in previous_ids]
    candidate_statements = [
        statement_to_dict(_thaw_response_mapping(repository["statements"][statement_id]))
        for statement_id in sorted(repository["statements"])
    ]
    state["statements"] = [*retained_statements, *candidate_statements]

    keywords = copy.deepcopy(state["keywords"])
    for keyword in tuple(keywords):
        retained_ids = sorted(set(keywords[keyword]["statement_ids"]) - previous_ids)
        if retained_ids:
            keywords[keyword]["statement_ids"] = retained_ids
        else:
            del keywords[keyword]
    for statement in candidate_statements:
        for keyword in statement["keywords"]:
            entry = keywords.setdefault(keyword, {"statement_ids": [], "query_count": 0, "hit_count": 0})
            entry["statement_ids"] = sorted({*entry["statement_ids"], statement["id"]})
    state["keywords"] = keywords

    quarantine = validate_response_quarantine_records(engram.response_quarantine)
    quarantine_values = tuple(response_quarantine_record_to_dict(record) for record in quarantine)
    state["response_state"] = coordinated_response_state_to_dict(validated_response_state, quarantine_values)
    return state


def save_response_state(engram, response_state: CoordinatedResponseState, path) -> None:
    """Atomically checkpoint one complete coordinated response candidate."""

    state = to_dict_with_response_state(engram, response_state)
    _write_json_atomic(path, state)


def to_dict_with_feedback_state(engram, feedback_state: FeedbackState) -> dict:
    """Serialize a complete off-live feedback candidate with current Engram state."""

    validated_feedback_state = validate_feedback_state(feedback_state)
    state = to_dict(engram)
    state["feedback_state"] = feedback_state_to_dict(validated_feedback_state)
    return state


def save_feedback_state(engram, feedback_state: FeedbackState, path) -> None:
    """Atomically checkpoint one complete feedback candidate."""

    _write_json_atomic(path, to_dict_with_feedback_state(engram, feedback_state))


def load_feedback_state(path, config: dict = EMPTY_CONFIG) -> FeedbackState:
    """Load durable feature-owned feedback state."""

    engram = load_engram(path, config=config)
    state = engram.feedback_store.snapshot()
    return state


def coordinated_response_state(engram) -> CoordinatedResponseState:
    """Capture the complete authoritative response state from one loaded Engram."""

    state = build_coordinated_response_state(
        repository=engram.response_repository.snapshot(),
        namespace_epochs=engram.namespace_epochs.snapshot(),
        mutation_receipts=engram.mutation_receipts.snapshot(),
    )
    return state


def load_coordinated_response_state(path, config: dict = EMPTY_CONFIG) -> CoordinatedResponseState:
    """Load durable response authority and rebuild its derived repository state."""

    engram = load_engram(path, config=config)
    state = coordinated_response_state(engram)
    return state


def save_json(engram) -> str:
    """Serialize complete state to JSON string.

    Args:
        engram: Engram instance.

    Returns:
        JSON string representation of the complete state.
    """
    json_str = json.dumps(to_dict(engram), indent=2)
    return json_str


def _response_state_to_dict(engram) -> dict[str, object]:
    repository = engram.response_repository.snapshot()
    quarantine = validate_response_quarantine_records(engram.response_quarantine)
    result = {
        "schema_version": RESPONSE_STATE_SCHEMA_VERSION,
        "artifacts": [
            cached_response_artifact_to_dict(repository["artifacts"][statement_id])
            for statement_id in sorted(repository["artifacts"])
        ],
        "namespace_epochs": engram.namespace_epochs.snapshot(),
        "mutation_receipts": engram.mutation_receipts.snapshot(),
        "quarantine": [response_quarantine_record_to_dict(record) for record in quarantine],
    }
    return result


def to_dict(engram) -> dict:
    """Serialize complete state to dictionary.

    Args:
        engram: Engram instance.

    Returns:
        Dictionary containing all persistent state.
    """

    with engram.statement_lock, engram.keyword_lock, engram.session_lock:
        state = {
            "version": PERSISTENCE_VERSION,
            "manifest": persistence_manifest(engram.config),
            # Full configuration, so weights, eviction policy, and feature
            # flags survive a save/load cycle. The top-level "capacity" key is
            # kept alongside for files read by older loaders.
            "config": config_to_dict(engram.config),
            "capacity": engram.config["capacity"],
            "query_count": engram.query_count,
            "hit_count": engram.hit_count,
            "eviction_count": engram.eviction_count,
            "bot": engram.bot_properties.copy(),
            "sets": {k: list(v) for k, v in engram.sets.items()},
            "maps": {k: dict(v) for k, v in engram.maps.items()},
            "substitutions": {
                "contractions": dict(engram.substitution_maps["contractions"]),
                "person": dict(engram.substitution_maps["person"]),
                "person2": dict(engram.substitution_maps["person2"]),
                "gender": dict(engram.substitution_maps["gender"]),
                "custom": dict(engram.substitution_maps["custom"]),
            },
            "statements": [statement_to_dict(s) for s in engram.statements],
            "keywords": {kw: keyword_entry_to_dict(entry) for kw, entry in engram.keywords.items()},
            "sessions": [session_to_dict(s) for s in engram.sessions.values()],
            "response_state": _response_state_to_dict(engram),
            "feedback_state": feedback_state_to_dict(engram.feedback_store.snapshot()),
        }
        return state


def save_sessions(engram, path) -> None:
    """Save sessions only to JSON file.

    Useful for persisting session state separately from the knowledge base.

    Args:
        engram: Engram instance.
        path: File path to write.
    """

    with engram.session_lock:
        data = {
            "version": PERSISTENCE_VERSION,
            "sessions": [session_to_dict(s) for s in engram.sessions.values()],
        }
    _write_json_atomic(path, data)


def load_sessions(engram, path) -> int:
    """Load sessions from JSON file.

    Adds sessions to the current instance without clearing existing sessions.

    Args:
        engram: Engram instance.
        path: File path to read.

    Returns:
        Number of sessions loaded.
    """

    with open(path, encoding="utf-8") as f:
        data = json.load(f)

    with engram.session_lock:
        for sess_data in data.get("sessions", []):
            sess = session_from_dict(sess_data)
            engram.sessions[sess["session_id"]] = sess
        loaded_count = len(data.get("sessions", []))
        return loaded_count


def rebuild_index(engram) -> None:
    """Rebuild keyword index from statements.

    Warning: This loses keyword statistics. Use for recovery only.

    Args:
        engram: Engram instance.
    """

    with engram.statement_lock, engram.keyword_lock:
        engram.keywords.clear()
        for stmt in engram.statements:
            for kw in stmt["keywords"]:
                if kw not in engram.keywords:
                    engram.keywords[kw] = keyword_entry(keyword=kw)
                engram.keywords[kw]["statement_ids"].add(stmt["id"])


def _migration_detail(value: object) -> str:
    text = str(value).replace("\r", " ").replace("\n", " ").replace("\t", " ")
    while len(text.encode("utf-8")) > MAX_QUARANTINE_DETAIL_BYTES:
        text = text[:-1]
    return text


def _canonical_utc(value: object, name: str) -> str:
    if not isinstance(value, datetime) or not value.tzinfo:
        raise InvalidRequestError(f"{name} must be a timezone-aware datetime")
    text = value.astimezone(UTC).isoformat().replace("+00:00", "Z")
    return text


def _legacy_response_artifact(statement: dict) -> CachedResponseArtifact:
    template = statement.get("template", {})
    if not isinstance(template, dict):
        raise InvalidRequestError("legacy template must be an object")
    tapestry = template.get("tapestry", {})
    if not isinstance(tapestry, dict):
        raise InvalidRequestError("legacy tapestry metadata must be an object")
    request = tapestry.get("request", "")
    if not isinstance(request, str) or not request.strip():
        raise InvalidRequestError("legacy response has no recoverable request identity")
    namespace = tapestry.get("namespace", "")
    context_fingerprint = tapestry.get("context_fingerprint", "")
    if not isinstance(namespace, str) or not isinstance(context_fingerprint, str):
        raise InvalidRequestError("legacy response scope must use concrete strings")
    scope = scope_key(namespace=namespace, context_fingerprint=context_fingerprint)
    aliases = tapestry.get("retrieval_aliases", [])
    if not isinstance(aliases, list) or not all(isinstance(alias, str) for alias in aliases):
        raise InvalidRequestError("legacy retrieval_aliases must be an array of strings")
    retrieval = build_retrieval_representation(request, tuple(aliases))
    identity = build_standalone_identity(request, scope)
    raw_support = tapestry.get("support", [])
    if not isinstance(raw_support, list):
        raise InvalidRequestError("legacy support must be an array")
    support = []
    for reference in raw_support:
        if (
            not isinstance(reference, dict)
            or not isinstance(reference.get("claim_id", ""), str)
            or not reference.get("claim_id", "")
        ):
            raise InvalidRequestError("legacy support entries must contain a non-empty string claim_id")
        support.append(reference["claim_id"])
    reserved = {"request", "retrieval_aliases", "namespace", "context_fingerprint", "support"}
    metadata = {key: value for key, value in tapestry.items() if key not in reserved}
    last_hit = _canonical_utc(statement["last_hit"], "legacy last_hit") if statement["last_hit"] else ""
    artifact = cached_response_artifact(
        statement_id=statement["id"],
        generation=1,
        response=statement["text"],
        query_identity=identity,
        retrieval=retrieval,
        tier=statement["tier"],
        lifecycle=LifecycleState.ACTIVE,
        scope=scope,
        support_claim_ids=tuple(support),
        valid_from="",
        valid_from_available=False,
        valid_until="",
        valid_until_available=False,
        knowledge_epoch=0,
        knowledge_epoch_available=False,
        superseded_by="",
        provenance=artifact_provenance(
            source_label=statement["source_label"],
            caller_id=statement["introduced_by_user_id"],
            accepted_at=_canonical_utc(statement["created_at"], "legacy created_at"),
        ),
        statistics=artifact_statistics(
            hit_count=statement["hit_count"],
            query_count=statement["query_count"],
            last_hit=last_hit,
            last_hit_available=bool(last_hit),
        ),
        metadata=metadata,
    )
    return artifact


def _migrate_legacy_response_state(instance) -> None:
    artifacts = []
    quarantine = []
    for statement in instance.statements:
        template = statement.get("template", {})
        tapestry = template.get("tapestry", {}) if isinstance(template, dict) else {}
        request = tapestry.get("request", "") if isinstance(tapestry, dict) else ""
        if not isinstance(request, str) or not request.strip():
            quarantine.append(
                response_quarantine_record(
                    statement["id"],
                    ResponseQuarantineReason.MISSING_IDENTITY,
                    "legacy response has no recoverable request identity",
                )
            )
            continue
        try:
            artifacts.append(_legacy_response_artifact(statement))
        except (InvalidRequestError, ValueError, TypeError) as error:
            quarantine.append(
                response_quarantine_record(
                    statement["id"],
                    ResponseQuarantineReason.MALFORMED_IDENTITY,
                    _migration_detail(error),
                )
            )

    key_owners: dict[str, set[str]] = {}
    for artifact in artifacts:
        for binding in retrieval_representation_bindings(artifact["retrieval"], artifact["scope"]):
            key_text = scoped_retrieval_key_to_json(binding["key"])
            key_owners.setdefault(key_text, set()).add(artifact["statement_id"])
    ambiguous_ids = set()
    for owners in key_owners.values():
        if len(owners) > 1:
            ambiguous_ids.update(owners)
    for statement_id in sorted(ambiguous_ids):
        quarantine.append(
            response_quarantine_record(
                statement_id,
                ResponseQuarantineReason.AMBIGUOUS_IDENTITY,
                "recoverable scoped retrieval key has multiple legacy owners",
            )
        )

    instance.response_repository = ArtifactRepository(artifacts)
    instance.namespace_epochs = NamespaceEpochState()
    for namespace in sorted({artifact["scope"]["namespace"] for artifact in artifacts}):
        instance.namespace_epochs.initialize(namespace, 0)
    instance.mutation_receipts = MutationReceiptLedger()
    instance.response_quarantine = validate_response_quarantine_records(tuple(quarantine))


def _load_response_state(instance, value: object) -> None:
    if not isinstance(value, Mapping):
        raise InvalidRequestError("response_state must be an object")
    keys = set({"schema_version", "artifacts", "namespace_epochs", "mutation_receipts", "quarantine"})
    if set(value) != keys:
        raise InvalidRequestError(
            f"response_state has invalid fields: missing={sorted(keys - set(value))}, extra={sorted(set(value) - keys)}"
        )
    if value["schema_version"] != RESPONSE_STATE_SCHEMA_VERSION:
        raise InvalidRequestError(f"unsupported response_state schema_version: {value['schema_version']}")
    artifacts = value["artifacts"]
    quarantine = value["quarantine"]
    if not isinstance(artifacts, list) or not isinstance(quarantine, list):
        raise InvalidRequestError("response_state artifacts and quarantine must be arrays")
    if len(quarantine) > MAX_QUARANTINE_RECORDS:
        raise InvalidRequestError(f"response quarantine exceeds the limit of {MAX_QUARANTINE_RECORDS}")
    epochs = value["namespace_epochs"]
    receipts = value["mutation_receipts"]
    if not isinstance(epochs, Mapping) or not isinstance(receipts, Mapping):
        raise InvalidRequestError("response_state epoch and receipt state must be objects")
    instance.response_repository = ArtifactRepository(tuple(cached_response_artifact_from_dict(item) for item in artifacts))
    instance.namespace_epochs = namespace_epoch_state_from_snapshot(epochs)
    instance.mutation_receipts = mutation_receipt_ledger_from_snapshot(receipts)
    decoded_quarantine = tuple(response_quarantine_record_from_dict(item) for item in quarantine)
    instance.response_quarantine = validate_response_quarantine_records(decoded_quarantine)


def _install_response_compatibility_views(instance, previous_response_ids: tuple[str, ...] = ()) -> None:
    artifact_ids = set(instance.response_repository.snapshot()["artifacts"])
    response_ids = artifact_ids | set(previous_response_ids)
    retained = []
    for statement in instance.statements:
        if statement["id"] not in response_ids:
            retained.append(statement)
            continue
        if statement["pattern"]:
            raise InvalidRequestError(f"response artifact statement_id collides with a pattern statement: {statement['id']}")
        for keyword in statement["keywords"]:
            if keyword in instance.keywords:
                instance.keywords[keyword]["statement_ids"].discard(statement["id"])
                if not instance.keywords[keyword]["statement_ids"]:
                    del instance.keywords[keyword]
    for statement in instance.response_repository.statement_views():
        retained.append(statement)
        for keyword in statement["keywords"]:
            if keyword not in instance.keywords:
                instance.keywords[keyword] = keyword_entry(keyword=keyword)
            instance.keywords[keyword]["statement_ids"].add(statement["id"])
    instance.statements = retained
    instance.statement_index = {statement["id"]: position for position, statement in enumerate(instance.statements)}


def synchronize_response_compatibility_views(instance, previous_response_ids: tuple[str, ...]) -> None:
    """Replace legacy response mirrors from the authoritative live repository."""

    if not isinstance(previous_response_ids, tuple) or not all(
        isinstance(statement_id, str) and statement_id for statement_id in previous_response_ids
    ):
        raise InvalidRequestError("previous_response_ids must be a tuple of non-empty strings")
    with instance.statement_lock, instance.keyword_lock:
        _install_response_compatibility_views(instance, previous_response_ids)
        instance.rebuild_indexes(apply=True)


def migrate_persistence_state(data: dict) -> dict:
    """Return deterministic persistence v2 without mutating caller input."""

    if not isinstance(data, dict):
        raise InvalidRequestError("persisted state must be an object")
    version = data.get("version", LEGACY_PERSISTENCE_VERSION)
    if version == PERSISTENCE_VERSION:
        instance = load_engram_from_dict(copy.deepcopy(data))
        migrated = copy.deepcopy(data) if "manifest" in data else to_dict(instance)
        return migrated
    if version != LEGACY_PERSISTENCE_VERSION:
        raise InvalidRequestError(f"Unsupported persistence version: {version}")
    instance = load_engram_from_dict(copy.deepcopy(data))
    migrated = to_dict(instance)
    return migrated


def migrate_persistence_file(source_path, output_path) -> PersistenceStatus:
    """Migrate one source file to a distinct new output and return a bounded report."""
    source = Path(source_path)
    output = Path(output_path)
    if source == output:
        raise InvalidRequestError("migration output must differ from the source path")
    if output.exists():
        raise InvalidRequestError("migration output already exists")
    with source.open(encoding="utf-8") as stream:
        data = json.load(stream)
    migrated = migrate_persistence_state(data)
    if migrate_persistence_state(migrated) != migrated:
        raise InvalidRequestError("persistence migration is not idempotent")
    instance = load_engram_from_dict(migrated)
    _write_json_atomic(output, migrated)
    status = dict(instance.persistence_status)
    report: PersistenceStatus = {
        "schema_version": PERSISTENCE_STATUS_SCHEMA_VERSION,
        "source_path": source.as_posix(),
        "output_path": output.as_posix(),
        "source_version": data.get("version", LEGACY_PERSISTENCE_VERSION),
        "output_version": migrated["version"],
        "artifact_count": len(instance.response_repository.snapshot()["artifacts"]),
        "quarantine_count": status["quarantine_count"],
        "quarantine_reasons": status["quarantine_reasons"],
        "manifest": migrated["manifest"],
        "idempotent": True,
    }
    return report


def load_engram(path, config: dict = EMPTY_CONFIG, engram_class=()):
    """Load ENGRAM state from JSON file.

    Args:
        path: File path to read.
        config: Optional configuration override.
        engram_class: The Engram class to instantiate (default: imported from core).

    Returns:
        Engram instance with restored state.
    """
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    instance = load_engram_from_dict(data, config, engram_class)
    return instance


def load_engram_json(json_str: str, config: dict = EMPTY_CONFIG, engram_class=()):
    """Load ENGRAM state from JSON string.

    Args:
        json_str: JSON string.
        config: Optional configuration override.
        engram_class: The Engram class to instantiate (default: imported from core).

    Returns:
        Engram instance with restored state.
    """
    data = json.loads(json_str)
    instance = load_engram_from_dict(data, config, engram_class)
    return instance


def load_engram_from_dict(data: dict, config: dict = EMPTY_CONFIG, engram_class=()):
    """Deserialize ENGRAM state from dictionary.

    Args:
        data: Dictionary containing serialized state.
        config: Optional configuration override.
        engram_class: The Engram class to instantiate (default: imported from core).

    Returns:
        Engram instance with restored state.

    Raises:
        ValueError: If persistence version is unsupported.
    """

    if not isinstance(data, dict):
        raise ValueError("persisted state must be an object")
    if not isinstance(config, dict):
        raise ValueError("config override must be an object")
    if not engram_class:
        engram_class = Engram

    version = data.get("version", LEGACY_PERSISTENCE_VERSION)
    if isinstance(version, bool) or not isinstance(version, int):
        raise ValueError("persistence version must be an integer")
    if version not in {LEGACY_PERSISTENCE_VERSION, PERSISTENCE_VERSION}:
        raise ValueError(f"Unsupported persistence version: {version}")

    # Create instance with config: an explicit override wins, then the config
    # stored with the state, then defaults (older files carried only capacity).
    manifest_present = version == PERSISTENCE_VERSION and "manifest" in data
    stored_config: dict = {}
    if not config or manifest_present:
        stored_config = (
            config_from_dict(data["config"]) if "config" in data else engram_config(capacity=data.get("capacity", 10000))
        )
    source_manifest: PersistenceManifest = {}
    if manifest_present:
        source_manifest = _validated_persistence_manifest(data["manifest"], stored_config)
    if not config:
        config = stored_config
    instance = engram_class(config=config)

    instance.query_count = data.get("query_count", 0)
    instance.hit_count = data.get("hit_count", 0)
    instance.eviction_count = data.get("eviction_count", 0)

    if "bot" in data:
        instance.bot_properties.update(data["bot"])

    if "sets" in data:
        for name, words in data["sets"].items():
            instance.sets[name] = list(words)

    if "maps" in data:
        for name, mapping in data["maps"].items():
            instance.maps[name] = dict(mapping)

    if "substitutions" in data:
        subs = data["substitutions"]
        if "contractions" in subs:
            instance.substitution_maps["contractions"].update(subs["contractions"])
        if "person" in subs:
            instance.substitution_maps["person"].update(subs["person"])
        if "person2" in subs:
            instance.substitution_maps["person2"].update(subs["person2"])
        if "gender" in subs:
            instance.substitution_maps["gender"].update(subs["gender"])
        if "custom" in subs:
            instance.substitution_maps["custom"].update(subs["custom"])

    for stmt_data in data.get("statements", []):
        stmt = statement_from_dict(stmt_data)
        if stmt["id"] in instance.statement_index:
            raise ValueError(f"duplicate statement id in persisted data: {stmt['id']}")
        instance.statements.append(stmt)
        instance.statement_index[stmt["id"]] = len(instance.statements) - 1
        if stmt["pattern"]:
            for registered_pattern in [stmt["pattern"], *stmt["pattern_aliases"]]:
                instance.pattern_matcher.add_pattern(
                    registered_pattern,
                    stmt["text"],
                    that=stmt["that"],
                    topic=stmt["topic"],
                )
                instance.pattern_to_statement[registered_pattern] = stmt["id"]

    for kw, entry_data in data.get("keywords", {}).items():
        instance.keywords[kw] = keyword_entry_from_dict(kw, entry_data)

    for sess_data in data.get("sessions", []):
        sess = session_from_dict(sess_data)
        instance.sessions[sess["session_id"]] = sess

    if version == LEGACY_PERSISTENCE_VERSION:
        _migrate_legacy_response_state(instance)
    else:
        if "response_state" not in data:
            raise InvalidRequestError("persistence v2 requires response_state")
        _load_response_state(instance, data["response_state"])
    if "feedback_state" in data:
        feedback_state = feedback_state_from_dict(data["feedback_state"])
        instance.feedback_store = FeedbackStore(feedback_state)
    else:
        # Existing files deliberately leave typed Regulator feedback unavailable;
        # legacy query/hit statistics are not reinterpreted as external labels.
        instance.feedback_store = FeedbackStore()
    _install_response_compatibility_views(instance)

    # The legacy Engram index remains rebuildable compatibility state. The
    # response repository independently rebuilds exact/alias/support indexes
    # from authoritative artifacts and never loads a persisted index snapshot.
    instance.rebuild_indexes(apply=True)
    instance.synchronize_sparse_index(instance.response_repository.snapshot())
    instance.synchronize_semantic_index(instance.response_repository.snapshot())
    instance.persistence_status = _persistence_status(instance, version, source_manifest, manifest_present)
    return instance
