"""Persistence functionality for ENGRAM.

This module provides save/load functionality for serializing and
deserializing ENGRAM state to/from JSON files and strings.
"""

import contextlib
import copy
import json
import os
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum

from engram.artifacts import ArtifactProvenance, ArtifactStatistics, CachedResponseArtifact, LifecycleState
from engram.config import config_from_dict, config_to_dict, engram_config
from engram.constants import PERSISTENCE_VERSION
from engram.coordination import CoordinatedResponseState
from engram.core import Engram
from engram.eligibility import NamespaceEpochState
from engram.errors import InvalidRequestError
from engram.feedback import FeedbackState, FeedbackStore
from engram.identity import ScopeKey, build_retrieval_representation, build_standalone_identity
from engram.models import (
    keyword_entry,
    keyword_entry_from_dict,
    keyword_entry_to_dict,
    session_from_dict,
    session_to_dict,
    statement_from_dict,
    statement_to_dict,
)
from engram.mutations import MutationReceiptLedger
from engram.repository import ArtifactRepository

EMPTY_CONFIG: dict = {}
LEGACY_PERSISTENCE_VERSION = 1
RESPONSE_STATE_SCHEMA_VERSION = 1
MAX_QUARANTINE_DETAIL_BYTES = 512
MAX_QUARANTINE_RECORDS = 100_000


class ResponseQuarantineReason(StrEnum):
    """Stable v1 response migration exclusion reasons."""

    MISSING_IDENTITY = "missing_identity"
    MALFORMED_IDENTITY = "malformed_identity"
    AMBIGUOUS_IDENTITY = "ambiguous_identity"


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


@dataclass(frozen=True, order=True, slots=True)
class ResponseQuarantineRecord:
    """One retained legacy response that could not safely enter exact lookup."""

    statement_id: str
    reason: ResponseQuarantineReason
    detail: str

    def __post_init__(self) -> None:
        _bounded_quarantine_text(self.statement_id, "quarantine statement_id", False)
        if not isinstance(self.reason, ResponseQuarantineReason):
            raise InvalidRequestError("quarantine reason must be a ResponseQuarantineReason")
        _bounded_quarantine_text(self.detail, "quarantine detail", True)

    def to_dict(self) -> dict[str, object]:
        return {"statement_id": self.statement_id, "reason": self.reason.value, "detail": self.detail}

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> "ResponseQuarantineRecord":
        if not isinstance(value, Mapping) or frozenset(value) != frozenset({"statement_id", "reason", "detail"}):
            raise InvalidRequestError("ResponseQuarantineRecord must contain exactly statement_id, reason, and detail")
        try:
            reason = ResponseQuarantineReason(value["reason"])
        except (TypeError, ValueError) as error:
            raise InvalidRequestError("quarantine record contains an unsupported reason") from error
        return cls(
            statement_id=_bounded_quarantine_text(value["statement_id"], "quarantine statement_id", False),
            reason=reason,
            detail=_bounded_quarantine_text(value["detail"], "quarantine detail", True),
        )


def _write_json_atomic(path, state: dict) -> None:
    """Write JSON to path atomically via a temp file and rename.

    A crash mid-write leaves the previous file intact instead of a truncated
    store; os.replace is atomic on POSIX and Windows.
    """
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
        return {key: _thaw_response_value(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw_response_value(item) for item in value]
    return value


def _thaw_response_mapping(value: object) -> dict[str, object]:
    thawed = _thaw_response_value(value)
    if not isinstance(thawed, dict):
        raise InvalidRequestError("response compatibility statement must be an object")
    return thawed


def to_dict_with_response_state(engram, response_state: CoordinatedResponseState) -> dict:
    """Serialize a complete off-live coordinated response candidate."""

    if not isinstance(response_state, CoordinatedResponseState):
        raise InvalidRequestError("response_state candidate must be a CoordinatedResponseState")
    state = to_dict(engram)
    previous_ids = set(engram.response_repository.snapshot().artifacts)
    retained_statements = [statement for statement in state["statements"] if statement["id"] not in previous_ids]
    candidate_statements = [
        statement_to_dict(_thaw_response_mapping(response_state.repository.statements[statement_id]))
        for statement_id in sorted(response_state.repository.statements)
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

    quarantine = engram.response_quarantine
    if not isinstance(quarantine, tuple) or not all(isinstance(record, ResponseQuarantineRecord) for record in quarantine):
        raise InvalidRequestError("response_quarantine must be a tuple of ResponseQuarantineRecord values")
    state["response_state"] = response_state.response_state_dict(tuple(record.to_dict() for record in quarantine))
    return state


def save_response_state(engram, response_state: CoordinatedResponseState, path) -> None:
    """Atomically checkpoint one complete coordinated response candidate."""

    _write_json_atomic(path, to_dict_with_response_state(engram, response_state))


def to_dict_with_feedback_state(engram, feedback_state: FeedbackState) -> dict:
    """Serialize a complete off-live feedback candidate with current Engram state."""

    if not isinstance(feedback_state, FeedbackState):
        raise InvalidRequestError("feedback_state candidate must be a FeedbackState")
    state = to_dict(engram)
    state["feedback_state"] = feedback_state.to_dict()
    return state


def save_feedback_state(engram, feedback_state: FeedbackState, path) -> None:
    """Atomically checkpoint one complete feedback candidate."""

    _write_json_atomic(path, to_dict_with_feedback_state(engram, feedback_state))


def load_feedback_state(path, config: dict = EMPTY_CONFIG) -> FeedbackState:
    """Load durable feature-owned feedback state."""

    return load_engram(path, config=config).feedback_store.snapshot()


def coordinated_response_state(engram) -> CoordinatedResponseState:
    """Capture the complete authoritative response state from one loaded Engram."""

    return CoordinatedResponseState(
        repository=engram.response_repository.snapshot(),
        namespace_epochs=engram.namespace_epochs.snapshot(),
        mutation_receipts=engram.mutation_receipts.snapshot(),
    )


def load_coordinated_response_state(path, config: dict = EMPTY_CONFIG) -> CoordinatedResponseState:
    """Load durable response authority and rebuild its derived repository state."""

    return coordinated_response_state(load_engram(path, config=config))


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
    quarantine = engram.response_quarantine
    if not isinstance(quarantine, tuple) or not all(isinstance(record, ResponseQuarantineRecord) for record in quarantine):
        raise InvalidRequestError("response_quarantine must be a tuple of ResponseQuarantineRecord values")
    if len(quarantine) > MAX_QUARANTINE_RECORDS:
        raise InvalidRequestError(f"response quarantine exceeds the limit of {MAX_QUARANTINE_RECORDS}")
    return {
        "schema_version": RESPONSE_STATE_SCHEMA_VERSION,
        "artifacts": [repository.artifacts[statement_id].to_dict() for statement_id in sorted(repository.artifacts)],
        "namespace_epochs": engram.namespace_epochs.snapshot(),
        "mutation_receipts": engram.mutation_receipts.snapshot(),
        "quarantine": [record.to_dict() for record in sorted(quarantine)],
    }


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
            "feedback_state": engram.feedback_store.snapshot().to_dict(),
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
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


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
    scope = ScopeKey(namespace=namespace, context_fingerprint=context_fingerprint)
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
    return CachedResponseArtifact(
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
        provenance=ArtifactProvenance(
            source_label=statement["source_label"],
            caller_id=statement["introduced_by_user_id"],
            accepted_at=_canonical_utc(statement["created_at"], "legacy created_at"),
        ),
        statistics=ArtifactStatistics(
            hit_count=statement["hit_count"],
            query_count=statement["query_count"],
            last_hit=last_hit,
            last_hit_available=bool(last_hit),
        ),
        metadata=metadata,
    )


def _migrate_legacy_response_state(instance) -> None:
    artifacts = []
    quarantine = []
    for statement in instance.statements:
        template = statement.get("template", {})
        tapestry = template.get("tapestry", {}) if isinstance(template, dict) else {}
        request = tapestry.get("request", "") if isinstance(tapestry, dict) else ""
        if not isinstance(request, str) or not request.strip():
            quarantine.append(
                ResponseQuarantineRecord(
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
                ResponseQuarantineRecord(
                    statement["id"],
                    ResponseQuarantineReason.MALFORMED_IDENTITY,
                    _migration_detail(error),
                )
            )

    key_owners: dict[str, set[str]] = {}
    for artifact in artifacts:
        for binding in artifact.retrieval.bindings(artifact.scope):
            key_owners.setdefault(binding.key.to_json(), set()).add(artifact.statement_id)
    ambiguous_ids = set()
    for owners in key_owners.values():
        if len(owners) > 1:
            ambiguous_ids.update(owners)
    for statement_id in sorted(ambiguous_ids):
        quarantine.append(
            ResponseQuarantineRecord(
                statement_id,
                ResponseQuarantineReason.AMBIGUOUS_IDENTITY,
                "recoverable scoped retrieval key has multiple legacy owners",
            )
        )

    if len(quarantine) > MAX_QUARANTINE_RECORDS:
        raise InvalidRequestError(f"response quarantine exceeds the limit of {MAX_QUARANTINE_RECORDS}")

    instance.response_repository = ArtifactRepository(artifacts)
    instance.namespace_epochs = NamespaceEpochState()
    for namespace in sorted({artifact.scope.namespace for artifact in artifacts}):
        instance.namespace_epochs.initialize(namespace, 0)
    instance.mutation_receipts = MutationReceiptLedger()
    instance.response_quarantine = tuple(sorted(quarantine))


def _load_response_state(instance, value: object) -> None:
    if not isinstance(value, Mapping):
        raise InvalidRequestError("response_state must be an object")
    keys = frozenset({"schema_version", "artifacts", "namespace_epochs", "mutation_receipts", "quarantine"})
    if frozenset(value) != keys:
        raise InvalidRequestError(
            f"response_state has invalid fields: missing={sorted(keys - frozenset(value))}, extra={sorted(frozenset(value) - keys)}"
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
    instance.response_repository = ArtifactRepository(tuple(CachedResponseArtifact.from_dict(item) for item in artifacts))
    instance.namespace_epochs = NamespaceEpochState.from_snapshot(epochs)
    instance.mutation_receipts = MutationReceiptLedger.from_snapshot(receipts)
    instance.response_quarantine = tuple(sorted(ResponseQuarantineRecord.from_dict(item) for item in quarantine))


def _install_response_compatibility_views(instance, previous_response_ids: tuple[str, ...] = ()) -> None:
    artifact_ids = set(instance.response_repository.snapshot().artifacts)
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
        load_engram_from_dict(copy.deepcopy(data))
        return copy.deepcopy(data)
    if version != LEGACY_PERSISTENCE_VERSION:
        raise InvalidRequestError(f"Unsupported persistence version: {version}")
    instance = load_engram_from_dict(copy.deepcopy(data))
    return to_dict(instance)


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
    if not config and "config" in data:
        config = config_from_dict(data["config"])
    if not config:
        config = engram_config(capacity=data.get("capacity", 10000))
    instance = engram_class(config=config)

    # Restore global counters
    instance.query_count = data.get("query_count", 0)
    instance.hit_count = data.get("hit_count", 0)
    instance.eviction_count = data.get("eviction_count", 0)

    # Restore bot properties
    if "bot" in data:
        instance.bot_properties.update(data["bot"])

    # Restore sets
    if "sets" in data:
        for name, words in data["sets"].items():
            instance.sets[name] = list(words)

    # Restore maps
    if "maps" in data:
        for name, mapping in data["maps"].items():
            instance.maps[name] = dict(mapping)

    # Restore substitutions
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

    # Restore statements
    for stmt_data in data.get("statements", []):
        stmt = statement_from_dict(stmt_data)
        if stmt["id"] in instance.statement_index:
            raise ValueError(f"duplicate statement id in persisted data: {stmt['id']}")
        instance.statements.append(stmt)
        instance.statement_index[stmt["id"]] = len(instance.statements) - 1
        # Rebuild pattern matcher with context
        if stmt["pattern"]:
            for registered_pattern in [stmt["pattern"], *stmt["pattern_aliases"]]:
                instance.pattern_matcher.add_pattern(
                    registered_pattern,
                    stmt["text"],
                    that=stmt["that"],
                    topic=stmt["topic"],
                )
                instance.pattern_to_statement[registered_pattern] = stmt["id"]

    # Restore keyword index
    for kw, entry_data in data.get("keywords", {}).items():
        instance.keywords[kw] = keyword_entry_from_dict(kw, entry_data)

    # Restore sessions
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
        instance.feedback_store = FeedbackStore(FeedbackState.from_dict(data["feedback_state"]))
    else:
        # Existing files deliberately leave typed Regulator feedback unavailable;
        # legacy query/hit statistics are not reinterpreted as external labels.
        instance.feedback_store = FeedbackStore()
    _install_response_compatibility_views(instance)

    # The legacy Engram index remains rebuildable compatibility state. The
    # response repository independently rebuilds exact/alias/support indexes
    # from authoritative artifacts and never loads a persisted index snapshot.
    instance.rebuild_indexes(apply=True)
    return instance
