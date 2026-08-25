"""Offline standalone semantic retrieval over authoritative request representations.

Accepted-response artifacts remain authoritative.  Embeddings are disposable,
in-memory projections of canonical requests and aliases; response prose is never
an embedding input.
"""

import hashlib
import json
import math
import threading
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from types import MappingProxyType

from sentence_transformers import SentenceTransformer

from engram.artifacts import CachedResponseArtifact, LifecycleState, validate_cached_response_artifact
from engram.config import SemanticConfig, semantic_config
from engram.constants import (
    APPROVED_SEMANTIC_ARTIFACT_SHA256,
    APPROVED_SEMANTIC_BACKEND,
    APPROVED_SEMANTIC_DIMENSION,
    APPROVED_SEMANTIC_LICENSE_ID,
    APPROVED_SEMANTIC_MODEL_ID,
    APPROVED_SEMANTIC_MODEL_VERSION,
    SEMANTIC_ARTIFACT_HASH_VERSION,
    SEMANTIC_INDEX_SCHEMA_VERSION,
    SEMANTIC_INDEX_VERSION,
    SEMANTIC_RECORD_SCHEMA_VERSION,
)
from engram.errors import InvalidRequestError
from engram.identity import ScopeKey, validate_scope_key

SEMANTIC_QUERY_WORKING_BYTES_PER_DIMENSION = 32
SEMANTIC_MATCH_WORKING_BYTES = 640

EmbeddingRecord = dict
SemanticIndexState = dict
SemanticMatch = dict
SemanticSearchResult = dict
SemanticIndexCheckReport = dict


def model_artifact_sha256(path: Path) -> str:
    """Hash one local model artifact without following symbolic links."""
    root = path
    if not root.exists():
        raise InvalidRequestError("semantic model artifact does not exist")
    if root.is_symlink():
        raise InvalidRequestError("semantic model artifact must not be a symbolic link")
    entries = () if root.is_file() else tuple(root.rglob("*"))
    if any(value.is_symlink() for value in entries):
        raise InvalidRequestError("semantic model artifact must not contain symbolic links")
    files = (
        (root,)
        if root.is_file()
        else tuple(sorted(value for value in entries if value.is_file() and ".cache" not in value.relative_to(root).parts))
    )
    if not files:
        raise InvalidRequestError("semantic model artifact contains no files")
    digest = hashlib.sha256()
    digest.update(f"engram-model-artifact-v{SEMANTIC_ARTIFACT_HASH_VERSION}\0".encode())
    for file_path in files:
        if file_path.is_symlink():
            raise InvalidRequestError("semantic model artifact must not contain symbolic links")
        relative = file_path.name if root.is_file() else file_path.relative_to(root).as_posix()
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(str(file_path.stat().st_size).encode())
        digest.update(b"\0")
        with file_path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
    return digest.hexdigest()


def _artifact_identity(settings: SemanticConfig, actual_sha256: str) -> Mapping[str, object]:
    result = MappingProxyType(
        {
            "model_id": settings["model_id"],
            "model_version": settings["model_version"],
            "license_id": settings["license_id"],
            "artifact_sha256": actual_sha256,
            "backend": settings["backend"],
            "dimension": settings["dimension"],
            "normalization_version": settings["normalization_version"],
        }
    )
    return result


def _config_fingerprint(settings: SemanticConfig) -> str:
    encoded = json.dumps(dict(settings), sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _load_native_model(settings: SemanticConfig) -> object:
    if settings["backend"] != "native":
        raise RuntimeError(f"semantic backend is unavailable: {settings['backend']}")
    model = SentenceTransformer(
        settings["model_path"],
        device="cpu",
        local_files_only=True,
        trust_remote_code=False,
    )
    return model


def _model_dimension(model: object) -> int:
    operation = getattr(model, "get_embedding_dimension", ())
    if not callable(operation):
        operation = getattr(model, "get_sentence_embedding_dimension", ())
    if not callable(operation):
        raise InvalidRequestError("semantic model does not report its embedding dimension")
    dimension = operation()
    if not isinstance(dimension, int) or isinstance(dimension, bool) or dimension < 1:
        raise InvalidRequestError("semantic model reported an invalid embedding dimension")
    return dimension


def _normalize_embedding(value: object, dimension: int) -> tuple[float, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        try:
            values = tuple(value)  # type: ignore[arg-type]
        except TypeError as error:
            raise InvalidRequestError("semantic model returned a non-vector value") from error
    else:
        values = tuple(value)
    if len(values) != dimension:
        raise InvalidRequestError("semantic model output dimension does not match configured dimension")
    numbers = []
    for item in values:
        if isinstance(item, bool):
            raise InvalidRequestError("semantic model returned a non-finite embedding")
        try:
            number = float(item)
        except (TypeError, ValueError) as error:
            raise InvalidRequestError("semantic model returned a non-finite embedding") from error
        if not math.isfinite(number):
            raise InvalidRequestError("semantic model returned a non-finite embedding")
        numbers.append(number)
    norm = math.sqrt(sum(item * item for item in numbers))
    if not math.isfinite(norm) or norm <= 0.0:
        raise InvalidRequestError("semantic model returned a zero embedding")
    result = tuple(item / norm for item in numbers)
    return result


def _encode(model: object, texts: tuple[str, ...], settings: SemanticConfig) -> tuple[tuple[float, ...], ...]:
    if not texts:
        return ()
    operation = getattr(model, "encode", ())
    if not callable(operation):
        raise InvalidRequestError("semantic model does not implement encode")
    raw = operation(
        list(texts),
        batch_size=settings["batch_size"],
        convert_to_numpy=False,
        normalize_embeddings=True,
        show_progress_bar=False,
    )
    if isinstance(raw, (str, bytes)) or not isinstance(raw, Iterable):
        raise InvalidRequestError("semantic model returned a non-batch value")
    values = tuple(raw)
    if len(values) != len(texts):
        raise InvalidRequestError("semantic model returned the wrong batch size")
    result = tuple(_normalize_embedding(value, settings["dimension"]) for value in values)
    return result


def _representation_specs(artifact: CachedResponseArtifact, settings: SemanticConfig) -> tuple[dict[str, object], ...]:
    if artifact["lifecycle"] != LifecycleState.ACTIVE:
        return ()
    texts = (("canonical", 0, artifact["retrieval"]["canonical"]),) + tuple(
        ("alias", index, value) for index, value in enumerate(artifact["retrieval"]["aliases"])
    )
    seen = set()
    values = []
    for origin, ordinal, text in texts:
        normalized = text.strip()
        key = normalized.casefold()
        if not normalized or key in seen:
            continue
        seen.add(key)
        if len(normalized.encode("utf-8")) > settings["max_input_bytes"]:
            raise InvalidRequestError("semantic request representation exceeds max_input_bytes")
        values.append(
            {
                "statement_id": artifact["statement_id"],
                "generation": artifact["generation"],
                "scope": artifact["scope"],
                "lifecycle": artifact["lifecycle"],
                "origin": origin,
                "ordinal": ordinal,
                "text": normalized,
            }
        )
    return tuple(values)


def _embedding_record(spec: Mapping[str, object], embedding: tuple[float, ...], identity: Mapping[str, object]) -> EmbeddingRecord:
    statement_id = spec["statement_id"]
    generation = spec["generation"]
    origin = spec["origin"]
    ordinal = spec["ordinal"]
    text = spec["text"]
    digest = hashlib.sha256(f"{statement_id}\0{generation}\0{origin}\0{ordinal}\0{text}".encode()).hexdigest()
    result = MappingProxyType(
        {
            "schema_version": SEMANTIC_RECORD_SCHEMA_VERSION,
            "representation_id": f"semantic:sha256:{digest}",
            "statement_id": statement_id,
            "generation": generation,
            "scope": MappingProxyType(validate_scope_key(spec["scope"])),
            "lifecycle": spec["lifecycle"],
            "origin": origin,
            "ordinal": ordinal,
            "text": text,
            "model_id": identity["model_id"],
            "model_version": identity["model_version"],
            "artifact_sha256": identity["artifact_sha256"],
            "backend": identity["backend"],
            "dimension": identity["dimension"],
            "normalization_version": identity["normalization_version"],
            "embedding": embedding,
        }
    )
    return result


def _record_projection(record: EmbeddingRecord) -> tuple[object, ...]:
    return (
        record["statement_id"],
        record["generation"],
        record["scope"],
        record["lifecycle"],
        record["origin"],
        record["ordinal"],
        record["text"],
    )


def _spec_projection(spec: Mapping[str, object]) -> tuple[object, ...]:
    return tuple(spec[name] for name in ("statement_id", "generation", "scope", "lifecycle", "origin", "ordinal", "text"))


def _state(
    by_statement: Mapping[str, tuple[EmbeddingRecord, ...]],
    *,
    repository_state_generation: int,
    state_generation: int,
    settings: SemanticConfig,
    identity: Mapping[str, object],
) -> SemanticIndexState:
    ordered = {key: by_statement[key] for key in sorted(by_statement)}
    records = tuple(record for values in ordered.values() for record in values)
    fingerprint_values = [
        {
            "id": record["representation_id"],
            "lifecycle": record["lifecycle"].value,
            "embedding": record["embedding"],
        }
        for record in records
    ]
    fingerprint = hashlib.sha256(json.dumps(fingerprint_values, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    result = MappingProxyType(
        {
            "schema_version": SEMANTIC_INDEX_SCHEMA_VERSION,
            "index_version": SEMANTIC_INDEX_VERSION,
            "state_generation": state_generation,
            "repository_state_generation": repository_state_generation,
            "config_fingerprint": _config_fingerprint(settings),
            "artifact_identity": identity,
            "by_statement": MappingProxyType(ordered),
            "records": records,
            "record_count": len(records),
            "fingerprint": fingerprint,
        }
    )
    return result


class StandaloneSemanticIndexOwner:
    """Atomic owner for one immutable, local-model semantic index."""

    def __init__(
        self,
        settings: Mapping[str, object],
        repository_state_generation: int = 1,
        model_loader: object = (),
    ) -> None:
        try:
            self._settings = semantic_config(**dict(settings))
        except (TypeError, ValueError) as error:
            raise InvalidRequestError(str(error)) from error
        if model_loader != () and not callable(model_loader):
            raise InvalidRequestError("semantic model_loader must be callable")
        injected_loader = callable(model_loader)
        self._model_loader = model_loader if callable(model_loader) else _load_native_model
        self._lock = threading.RLock()
        self._model: object = ()
        self._healthy = not self._settings["enabled"]
        self._last_error = ""
        self._identity: Mapping[str, object] = MappingProxyType({})
        self._state = _state(
            {},
            repository_state_generation=repository_state_generation,
            state_generation=1,
            settings=self._settings,
            identity=self._identity,
        )
        if self._settings["enabled"]:
            try:
                if not injected_loader:
                    approved_identity = {
                        "model_id": APPROVED_SEMANTIC_MODEL_ID,
                        "model_version": APPROVED_SEMANTIC_MODEL_VERSION,
                        "license_id": APPROVED_SEMANTIC_LICENSE_ID,
                        "artifact_sha256": APPROVED_SEMANTIC_ARTIFACT_SHA256,
                        "backend": APPROVED_SEMANTIC_BACKEND,
                        "dimension": APPROVED_SEMANTIC_DIMENSION,
                    }
                    if any(self._settings[name] != expected for name, expected in approved_identity.items()):
                        raise InvalidRequestError("semantic model identity is not approved")
                model_path = Path(self._settings["model_path"])
                if not model_path.is_dir():
                    raise InvalidRequestError("semantic model artifact must be a directory")
                if not (model_path / "LICENSE").is_file():
                    raise InvalidRequestError("semantic model artifact is missing its license file")
                actual_sha256 = model_artifact_sha256(model_path)
                if actual_sha256 != self._settings["artifact_sha256"]:
                    raise InvalidRequestError("semantic model artifact checksum mismatch")
                model = self._model_loader(self._settings)
                if _model_dimension(model) != self._settings["dimension"]:
                    raise InvalidRequestError("semantic model dimension mismatch")
                self._model = model
                self._identity = _artifact_identity(self._settings, actual_sha256)
                self._state = _state(
                    {},
                    repository_state_generation=repository_state_generation,
                    state_generation=2,
                    settings=self._settings,
                    identity=self._identity,
                )
                self._healthy = True
            except Exception as error:
                self._healthy = False
                self._last_error = type(error).__name__

    @property
    def enabled(self) -> bool:
        return self._settings["enabled"]

    @property
    def available(self) -> bool:
        with self._lock:
            return bool(self.enabled and self._healthy and self._model)

    @property
    def last_error(self) -> str:
        with self._lock:
            return self._last_error

    def health(self) -> dict[str, object]:
        with self._lock:
            result = {
                "enabled": self.enabled,
                "ready": bool(self.enabled and self._healthy and self._model),
                "error": self._last_error,
                "artifact_identity": dict(self._identity),
                "state_generation": self._state["state_generation"],
                "repository_state_generation": self._state["repository_state_generation"],
                "record_count": self._state["record_count"],
            }
            return result

    def snapshot(self) -> SemanticIndexState:
        with self._lock:
            return self._state

    def _records(self, specs: tuple[Mapping[str, object], ...]) -> tuple[EmbeddingRecord, ...]:
        model = self._model
        if not model:
            raise InvalidRequestError("semantic model is unavailable")
        embeddings = _encode(model, tuple(spec["text"] for spec in specs), self._settings)
        return tuple(_embedding_record(spec, embedding, self._identity) for spec, embedding in zip(specs, embeddings, strict=True))

    def rebuild(
        self,
        artifacts: Iterable[CachedResponseArtifact],
        repository_state_generation: int,
    ) -> SemanticIndexState:
        if not self.enabled or not self._model:
            raise InvalidRequestError("semantic index is unavailable")
        validated = tuple(validate_cached_response_artifact(value) for value in artifacts)
        specs_by_statement = {
            artifact["statement_id"]: specs for artifact in validated if (specs := _representation_specs(artifact, self._settings))
        }
        count = sum(len(values) for values in specs_by_statement.values())
        if count > self._settings["max_records"]:
            raise InvalidRequestError("semantic index exceeds max_records")
        by_statement = {statement_id: self._records(specs) for statement_id, specs in specs_by_statement.items()}
        with self._lock:
            candidate = _state(
                by_statement,
                repository_state_generation=repository_state_generation,
                state_generation=self._state["state_generation"] + 1,
                settings=self._settings,
                identity=self._identity,
            )
            if repository_state_generation < self._state["repository_state_generation"]:
                return self._state
            self._state = candidate
            self._healthy = True
            self._last_error = ""
            return self._state

    def synchronize(
        self,
        artifacts: Mapping[str, CachedResponseArtifact],
        repository_state_generation: int,
        changed_statement_ids: tuple[str, ...],
    ) -> SemanticIndexState:
        if not isinstance(changed_statement_ids, tuple):
            raise InvalidRequestError("changed semantic statement IDs must be a tuple")
        normalized_ids = tuple(sorted(set(changed_statement_ids)))
        if normalized_ids != changed_statement_ids or not all(isinstance(value, str) and value for value in normalized_ids):
            raise InvalidRequestError("changed semantic statement IDs must be sorted unique non-empty strings")
        if not self.enabled or not self._model:
            raise InvalidRequestError("semantic index is unavailable")
        if not normalized_ids:
            return self.rebuild(artifacts.values(), repository_state_generation)
        with self._lock:
            live = self._state
            if repository_state_generation < live["repository_state_generation"]:
                return live
            healthy = self._healthy
            updated = dict(live["by_statement"])
        if not healthy:
            return self.rebuild(artifacts.values(), repository_state_generation)
        for statement_id in normalized_ids:
            artifact = artifacts.get(statement_id)
            if artifact is None:
                updated.pop(statement_id, None)
                continue
            validated = validate_cached_response_artifact(artifact)
            specs = _representation_specs(validated, self._settings)
            if not specs:
                updated.pop(statement_id, None)
                continue
            existing = updated.get(statement_id, ())
            if tuple(_record_projection(record) for record in existing) == tuple(_spec_projection(spec) for spec in specs):
                continue
            updated[statement_id] = self._records(specs)
        if sum(len(values) for values in updated.values()) > self._settings["max_records"]:
            raise InvalidRequestError("semantic index exceeds max_records")
        with self._lock:
            if repository_state_generation < self._state["repository_state_generation"]:
                return self._state
            if self._state["state_generation"] == live["state_generation"]:
                self._state = _state(
                    updated,
                    repository_state_generation=repository_state_generation,
                    state_generation=live["state_generation"] + 1,
                    settings=self._settings,
                    identity=self._identity,
                )
                self._healthy = True
                self._last_error = ""
                return self._state
        return self.rebuild(artifacts.values(), repository_state_generation)

    def mark_unavailable(self, error: object) -> None:
        with self._lock:
            self._healthy = False
            self._last_error = type(error).__name__

    def check_against(
        self,
        artifacts: Iterable[CachedResponseArtifact],
        repository_state_generation: int,
    ) -> SemanticIndexCheckReport:
        expected = {}
        if self.enabled:
            validated = tuple(validate_cached_response_artifact(value) for value in artifacts)
            expected = {
                artifact["statement_id"]: tuple(_spec_projection(spec) for spec in specs)
                for artifact in validated
                if (specs := _representation_specs(artifact, self._settings))
            }
        with self._lock:
            live = self._state
            healthy = self._healthy
            actual = {
                statement_id: tuple(_record_projection(record) for record in records)
                for statement_id, records in live["by_statement"].items()
            }
        issues = []
        if self.enabled and not healthy:
            issues.append("semantic_unavailable")
        if self.enabled and live["repository_state_generation"] != repository_state_generation:
            issues.append("repository_state_generation")
        if expected != actual:
            issues.append("request_representations")
        if live["artifact_identity"] != self._identity:
            issues.append("artifact_identity")
        return {
            "consistent": not issues,
            "issues": tuple(issues),
            "checked_state_generation": live["state_generation"],
            "record_count": live["record_count"],
        }

    def search(
        self,
        text: str,
        scope: ScopeKey,
        *,
        limit: int,
        max_vector_results: int,
        max_working_memory_bytes: int,
        cooperative_check: object = (),
    ) -> SemanticSearchResult:
        if not isinstance(text, str):
            raise InvalidRequestError("semantic query must be a string")
        if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
            raise InvalidRequestError("semantic result limit must be a positive integer")
        if not isinstance(max_vector_results, int) or isinstance(max_vector_results, bool) or max_vector_results < 0:
            raise InvalidRequestError("semantic vector-result budget must be nonnegative")
        if (
            not isinstance(max_working_memory_bytes, int)
            or isinstance(max_working_memory_bytes, bool)
            or max_working_memory_bytes < 0
        ):
            raise InvalidRequestError("semantic working-memory budget must be nonnegative")
        if cooperative_check != () and not callable(cooperative_check):
            raise InvalidRequestError("semantic cooperative_check must be callable")
        if not self.available:
            return {
                "complete": False,
                "reason": "semantic_unavailable",
                "matches": (),
                "scanned_records": 0,
                "working_memory_bytes": 0,
            }
        normalized_scope = validate_scope_key(scope)
        encoded_size = len(text.encode("utf-8"))
        if not text.strip():
            return {"complete": True, "reason": "semantic_miss", "matches": (), "scanned_records": 0, "working_memory_bytes": 0}
        if encoded_size > self._settings["max_input_bytes"]:
            return {
                "complete": False,
                "reason": "semantic_input_too_large",
                "matches": (),
                "scanned_records": 0,
                "working_memory_bytes": 0,
            }
        if not max_vector_results:
            return {
                "complete": False,
                "reason": "vector_result_budget",
                "matches": (),
                "scanned_records": 0,
                "working_memory_bytes": 0,
            }
        query_memory = 64 + self._settings["dimension"] * SEMANTIC_QUERY_WORKING_BYTES_PER_DIMENSION
        if query_memory > max_working_memory_bytes:
            return {
                "complete": False,
                "reason": "working_memory_budget",
                "matches": (),
                "scanned_records": 0,
                "working_memory_bytes": max_working_memory_bytes,
            }
        if callable(cooperative_check):
            cooperative_check()
        query = _encode(self._model, (text.strip(),), self._settings)[0]
        if callable(cooperative_check):
            cooperative_check()
        state = self.snapshot()
        retained_limit = min(limit, max_vector_results)
        retained_by_statement: dict[str, SemanticMatch] = {}
        scanned_records = 0
        for record in state["records"]:
            if record["scope"] != normalized_scope or record["lifecycle"] != LifecycleState.ACTIVE:
                continue
            scanned_records += 1
            if scanned_records > self._settings["max_scan_records"]:
                return {
                    "complete": False,
                    "reason": "semantic_scan_budget",
                    "matches": (),
                    "scanned_records": scanned_records,
                    "working_memory_bytes": query_memory + len(retained_by_statement) * SEMANTIC_MATCH_WORKING_BYTES,
                }
            if callable(cooperative_check) and scanned_records % 64 == 1:
                cooperative_check()
            similarity = sum(first * second for first, second in zip(query, record["embedding"], strict=True))
            if similarity <= 0.0 or similarity < self._settings["min_similarity"]:
                continue
            match = {
                "statement_id": record["statement_id"],
                "similarity": min(1.0, similarity),
                "origin": record["origin"],
                "ordinal": record["ordinal"],
                "representation_id": record["representation_id"],
                "generation": record["generation"],
            }
            statement_id = match["statement_id"]
            current = retained_by_statement.get(statement_id)
            if current is not None:
                if (-match["similarity"], match["representation_id"]) < (
                    -current["similarity"],
                    current["representation_id"],
                ):
                    retained_by_statement[statement_id] = match
                continue
            global_key = (-match["similarity"], match["statement_id"])
            if len(retained_by_statement) >= retained_limit:
                worst = max(retained_by_statement.values(), key=lambda value: (-value["similarity"], value["statement_id"]))
                if global_key >= (-worst["similarity"], worst["statement_id"]):
                    continue
                retained_by_statement.pop(worst["statement_id"])
            projected_memory = query_memory + (len(retained_by_statement) + 1) * SEMANTIC_MATCH_WORKING_BYTES
            if projected_memory > max_working_memory_bytes:
                return {
                    "complete": False,
                    "reason": "working_memory_budget",
                    "matches": (),
                    "scanned_records": scanned_records,
                    "working_memory_bytes": query_memory + len(retained_by_statement) * SEMANTIC_MATCH_WORKING_BYTES,
                }
            retained_by_statement[statement_id] = match
        if callable(cooperative_check):
            cooperative_check()
        retained = tuple(sorted(retained_by_statement.values(), key=lambda value: (-value["similarity"], value["statement_id"])))
        working_memory = query_memory + len(retained) * SEMANTIC_MATCH_WORKING_BYTES
        return {
            "complete": True,
            "reason": "semantic_candidates" if retained else "semantic_miss",
            "matches": retained,
            "scanned_records": scanned_records,
            "working_memory_bytes": working_memory,
        }
