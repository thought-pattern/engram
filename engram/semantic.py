"""Request-local semantic retrieval over accepted-response artifacts."""

from collections.abc import Iterable
from hashlib import sha256 as hashlib_sha256
from itertools import batched as itertools_batched
from math import isfinite as math_isfinite, sqrt as math_sqrt
from pathlib import Path

from sentence_transformers import SentenceTransformer

from engram.artifacts import LifecycleState, validate_cached_response_artifact
from engram.config import semantic_config
from engram.constants import (
    APPROVED_SEMANTIC_ARTIFACT_SHA256,
    APPROVED_SEMANTIC_BACKEND,
    APPROVED_SEMANTIC_DIMENSION,
    APPROVED_SEMANTIC_LICENSE_ID,
    APPROVED_SEMANTIC_MODEL_ID,
    APPROVED_SEMANTIC_MODEL_VERSION,
    SEMANTIC_ARTIFACT_HASH_VERSION,
    SEMANTIC_RECORD_SCHEMA_VERSION,
)
from engram.errors import InvalidRequestError
from engram.identity import validate_scope_key
from engram.resources import estimate_working_bytes

SEMANTIC_QUERY_WORKING_BYTES_PER_DIMENSION = 32
SEMANTIC_RECORD_WORKING_BYTES_PER_DIMENSION = 64
SEMANTIC_MATCH_WORKING_BYTES = 640


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
    digest = hashlib_sha256()
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
    result = digest.hexdigest()
    return result


def internal_artifact_identity(settings: dict, actual_sha256: str) -> dict:
    result = {
        "model_id": settings.get("model_id", ""),
        "model_version": settings.get("model_version", ""),
        "license_id": settings.get("license_id", ""),
        "artifact_sha256": actual_sha256,
        "backend": settings.get("backend", ""),
        "dimension": settings.get("dimension", 0),
        "normalization_version": settings.get("normalization_version", 0),
    }
    return result


def model_dimension(model: object) -> int:
    operation = getattr(model, "get_embedding_dimension", ())
    if not callable(operation):
        operation = getattr(model, "get_sentence_embedding_dimension", ())
    if not callable(operation):
        raise InvalidRequestError("semantic model does not report its embedding dimension")
    dimension = operation()
    if not isinstance(dimension, int) or isinstance(dimension, bool) or dimension < 1:
        raise InvalidRequestError("semantic model reported an invalid embedding dimension")
    return dimension


def normalize_embedding(value: object, dimension: int) -> tuple[float, ...]:
    if isinstance(value, (str, bytes, dict)) or not isinstance(value, Iterable):
        raise InvalidRequestError("semantic model returned a non-vector value")
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
        if not math_isfinite(number):
            raise InvalidRequestError("semantic model returned a non-finite embedding")
        numbers.append(number)
    norm = math_sqrt(sum(item * item for item in numbers))
    if not math_isfinite(norm) or norm <= 0.0:
        raise InvalidRequestError("semantic model returned a zero embedding")
    result = tuple(item / norm for item in numbers)
    return result


def internal_encode(model: object, texts: tuple[str, ...], settings: dict) -> tuple[tuple[float, ...], ...]:
    if not texts:
        return ()
    operation = getattr(model, "encode", ())
    if not callable(operation):
        raise InvalidRequestError("semantic model does not implement encode")
    raw = operation(
        list(texts),
        batch_size=settings.get("batch_size", 0),
        convert_to_numpy=False,
        normalize_embeddings=True,
        show_progress_bar=False,
    )
    if isinstance(raw, (str, bytes)) or not isinstance(raw, Iterable):
        raise InvalidRequestError("semantic model returned a non-batch value")
    values = tuple(raw)
    if len(values) != len(texts):
        raise InvalidRequestError("semantic model returned the wrong batch size")
    result = tuple(normalize_embedding(value, settings.get("dimension", 0)) for value in values)
    return result


def representation_specs(artifact: dict, settings: dict) -> tuple[dict, ...]:
    if artifact.get("lifecycle", LifecycleState.RETIRED) != LifecycleState.ACTIVE:
        return ()
    retrieval = artifact.get("retrieval", {})
    texts = (("canonical", 0, retrieval.get("canonical", "")),) + tuple(
        ("alias", index, value) for index, value in enumerate(retrieval.get("aliases", ()))
    )
    seen = set()
    values = []
    for origin, ordinal, text in texts:
        normalized = text.strip()
        key = normalized.casefold()
        if not normalized or key in seen:
            continue
        seen.add(key)
        if len(normalized.encode("utf-8")) > settings.get("max_input_bytes", 0):
            raise InvalidRequestError("semantic request representation exceeds max_input_bytes")
        values.append(
            {
                "statement_id": artifact.get("statement_id", ""),
                "generation": artifact.get("generation", 0),
                "scope": artifact.get("scope", {}),
                "lifecycle": artifact.get("lifecycle", LifecycleState.RETIRED),
                "origin": origin,
                "ordinal": ordinal,
                "text": normalized,
            }
        )
    result = tuple(values)
    return result


def scoped_representation_specs(artifacts: tuple[dict, ...], scope: dict, settings: dict):
    """Yield validated active representation specifications for one request scope."""
    for artifact_value in artifacts:
        artifact = validate_cached_response_artifact(artifact_value)
        if artifact.get("scope", {}) != scope:
            continue
        yield from representation_specs(artifact, settings)


def semantic_record_working_bytes(spec: dict, dimension: int) -> int:
    """Estimate peak specification, model-output, and normalized-record memory."""
    embedding_bytes = 256 + dimension * SEMANTIC_RECORD_WORKING_BYTES_PER_DIMENSION
    result = estimate_working_bytes(spec) + embedding_bytes
    return result


def embedding_record(spec: dict, embedding: tuple[float, ...], identity: dict) -> dict:
    statement_id = spec.get("statement_id", "")
    generation = spec.get("generation", 0)
    origin = spec.get("origin", "")
    ordinal = spec.get("ordinal", 0)
    text = spec.get("text", "")
    digest = hashlib_sha256(f"{statement_id}\0{generation}\0{origin}\0{ordinal}\0{text}".encode()).hexdigest()
    result = {
        "schema_version": SEMANTIC_RECORD_SCHEMA_VERSION,
        "representation_id": f"semantic:sha256:{digest}",
        "statement_id": statement_id,
        "generation": generation,
        "scope": dict(validate_scope_key(spec.get("scope", {}))),
        "lifecycle": spec.get("lifecycle", LifecycleState.RETIRED),
        "origin": origin,
        "ordinal": ordinal,
        "text": text,
        "model_id": identity.get("model_id", ""),
        "model_version": identity.get("model_version", ""),
        "artifact_sha256": identity.get("artifact_sha256", ""),
        "backend": identity.get("backend", ""),
        "dimension": identity.get("dimension", 0),
        "normalization_version": identity.get("normalization_version", 0),
        "embedding": embedding,
    }
    return result


class StandaloneSemanticRetriever:
    """Hold the local model while deriving all response embeddings per request."""

    def __init__(
        self,
        settings: dict,
        model: object = (),
    ) -> None:
        try:
            self.internal_settings = semantic_config(**dict(settings))
        except (TypeError, ValueError) as error:
            raise InvalidRequestError(str(error)) from error
        injected_model = model != ()
        self.internal_model: object = ()
        self.internal_healthy = not self.internal_settings["enabled"]
        self.internal_last_error = ""
        self.internal_identity: dict = {}
        if self.internal_settings["enabled"]:
            try:
                if not injected_model:
                    approved_identity = {
                        "model_id": APPROVED_SEMANTIC_MODEL_ID,
                        "model_version": APPROVED_SEMANTIC_MODEL_VERSION,
                        "license_id": APPROVED_SEMANTIC_LICENSE_ID,
                        "artifact_sha256": APPROVED_SEMANTIC_ARTIFACT_SHA256,
                        "backend": APPROVED_SEMANTIC_BACKEND,
                        "dimension": APPROVED_SEMANTIC_DIMENSION,
                    }
                    if any(self.internal_settings[name] != expected for name, expected in approved_identity.items()):
                        raise InvalidRequestError("semantic model identity is not approved")
                model_path = Path(self.internal_settings["model_path"])
                if not model_path.is_dir():
                    raise InvalidRequestError("semantic model artifact must be a directory")
                if not (model_path / "LICENSE").is_file():
                    raise InvalidRequestError("semantic model artifact is missing its license file")
                actual_sha256 = model_artifact_sha256(model_path)
                if actual_sha256 != self.internal_settings["artifact_sha256"]:
                    raise InvalidRequestError("semantic model artifact checksum mismatch")
                selected_model = model
                if not injected_model:
                    if self.internal_settings.get("backend", "") != "native":
                        raise RuntimeError(f"semantic backend is unavailable: {self.internal_settings.get('backend', "")}")
                    selected_model = SentenceTransformer(
                        self.internal_settings.get("model_path", ""),
                        device="cpu",
                        local_files_only=True,
                        trust_remote_code=False,
                    )
                if model_dimension(selected_model) != self.internal_settings["dimension"]:
                    raise InvalidRequestError("semantic model dimension mismatch")
                self.internal_model = selected_model
                self.internal_identity = internal_artifact_identity(self.internal_settings, actual_sha256)
                self.internal_healthy = True
            except Exception as error:
                self.internal_healthy = False
                self.internal_last_error = type(error).__name__

    @property
    def enabled(self) -> bool:
        result = self.internal_settings["enabled"]
        return result

    @property
    def available(self) -> bool:
        result = bool(self.enabled and self.internal_healthy and self.internal_model)
        return result

    @property
    def last_error(self) -> str:
        return self.internal_last_error

    def health(self) -> dict:
        result = {
            "enabled": self.enabled,
            "ready": bool(self.enabled and self.internal_healthy and self.internal_model),
            "error": self.internal_last_error,
            "artifact_identity": dict(self.internal_identity),
        }
        return result

    def records(self, specs: tuple[dict, ...]) -> tuple[dict, ...]:
        model = self.internal_model
        if not model:
            raise InvalidRequestError("semantic model is unavailable")
        embeddings = internal_encode(model, tuple(spec.get("text", "") for spec in specs), self.internal_settings)
        result = tuple(
            embedding_record(spec, embedding, self.internal_identity) for spec, embedding in zip(specs, embeddings, strict=True)
        )
        return result

    def search(
        self,
        text: str,
        scope: dict,
        artifacts: tuple[dict, ...],
        *,
        limit: int,
        max_vector_results: int,
        max_working_memory_bytes: int,
        cooperative_check: object = (),
    ) -> dict:
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
        settings = self.internal_settings
        normalized_scope = validate_scope_key(scope)
        normalized_text = text.strip()
        encoded_size = len(text.encode("utf-8"))
        if not normalized_text:
            result = {
                "complete": True,
                "reason": "semantic_miss",
                "matches": (),
                "scanned_records": 0,
                "working_memory_bytes": 0,
            }
            return result
        if encoded_size > settings.get("max_input_bytes", 0):
            result = {
                "complete": False,
                "reason": "semantic_input_too_large",
                "matches": (),
                "scanned_records": 0,
                "working_memory_bytes": 0,
            }
            return result
        if not max_vector_results:
            result = {
                "complete": False,
                "reason": "vector_result_budget",
                "matches": (),
                "scanned_records": 0,
                "working_memory_bytes": 0,
            }
            return result
        dimension = settings.get("dimension", 0)
        query_memory = 64 + dimension * SEMANTIC_QUERY_WORKING_BYTES_PER_DIMENSION
        if query_memory > max_working_memory_bytes:
            result = {
                "complete": False,
                "reason": "working_memory_budget",
                "matches": (),
                "scanned_records": 0,
                "working_memory_bytes": max_working_memory_bytes,
            }
            return result

        corpus_record_count = 0
        largest_record_working_bytes = 0
        for spec in scoped_representation_specs(artifacts, normalized_scope, settings):
            corpus_record_count += 1
            if corpus_record_count > settings.get("max_records", 0):
                result = {
                    "complete": False,
                    "reason": "semantic_record_budget",
                    "matches": (),
                    "scanned_records": 0,
                    "working_memory_bytes": query_memory,
                }
                return result
            if corpus_record_count > settings.get("max_scan_records", 0):
                result = {
                    "complete": False,
                    "reason": "semantic_scan_budget",
                    "matches": (),
                    "scanned_records": corpus_record_count,
                    "working_memory_bytes": query_memory,
                }
                return result
            record_working_bytes = semantic_record_working_bytes(spec, dimension)
            largest_record_working_bytes = max(largest_record_working_bytes, record_working_bytes)
            if callable(cooperative_check) and corpus_record_count % 64 == 1:
                cooperative_check()
        if not corpus_record_count:
            result = {
                "complete": True,
                "reason": "semantic_miss",
                "matches": (),
                "scanned_records": 0,
                "working_memory_bytes": query_memory,
            }
            return result

        retained_limit = min(limit, max_vector_results, corpus_record_count)
        retained_reservation = retained_limit * SEMANTIC_MATCH_WORKING_BYTES
        available_batch_memory = max_working_memory_bytes - query_memory - retained_reservation
        if largest_record_working_bytes > available_batch_memory:
            result = {
                "complete": False,
                "reason": "working_memory_budget",
                "matches": (),
                "scanned_records": 0,
                "working_memory_bytes": query_memory,
            }
            return result
        batch_size = settings.get("batch_size", 1)
        batch_capacity = max(1, min(batch_size, available_batch_memory // largest_record_working_bytes))
        if callable(cooperative_check):
            cooperative_check()
        query = internal_encode(self.internal_model, (normalized_text,), settings)[0]
        if callable(cooperative_check):
            cooperative_check()
        retained_by_statement: dict[str, dict] = {}
        scanned_records = 0
        peak_working_memory = query_memory
        specs = scoped_representation_specs(artifacts, normalized_scope, settings)
        for spec_batch in itertools_batched(specs, batch_capacity):
            batch_working_bytes = sum(semantic_record_working_bytes(spec, dimension) for spec in spec_batch)
            records = self.records(spec_batch)
            peak_working_memory = max(
                peak_working_memory,
                query_memory + batch_working_bytes + len(retained_by_statement) * SEMANTIC_MATCH_WORKING_BYTES,
            )
            for record in records:
                scanned_records += 1
                if callable(cooperative_check) and scanned_records % 64 == 1:
                    cooperative_check()
                embedding = record.get("embedding", ())
                similarity = sum(first * second for first, second in zip(query, embedding, strict=True))
                if similarity <= 0.0 or similarity < settings.get("min_similarity", 0.0):
                    continue
                match = {
                    "statement_id": record.get("statement_id", ""),
                    "similarity": min(1.0, similarity),
                    "origin": record.get("origin", ""),
                    "ordinal": record.get("ordinal", 0),
                    "representation_id": record.get("representation_id", ""),
                    "generation": record.get("generation", 0),
                }
                statement_id = match.get("statement_id", "")
                current = retained_by_statement.get(statement_id, {})
                if current:
                    if (-match.get("similarity", 0.0), match.get("representation_id", "")) < (
                        -current.get("similarity", 0.0),
                        current.get("representation_id", ""),
                    ):
                        retained_by_statement[statement_id] = match
                    continue
                global_key = (-match.get("similarity", 0.0), match.get("statement_id", ""))
                if len(retained_by_statement) >= retained_limit:
                    worst = max(
                        retained_by_statement.values(),
                        key=lambda value: (-value.get("similarity", 0.0), value.get("statement_id", "")),
                    )
                    if global_key >= (-worst.get("similarity", 0.0), worst.get("statement_id", "")):
                        continue
                    retained_by_statement.pop(worst.get("statement_id", ""))
                projected_memory = (
                    query_memory + batch_working_bytes + (len(retained_by_statement) + 1) * SEMANTIC_MATCH_WORKING_BYTES
                )
                if projected_memory > max_working_memory_bytes:
                    result = {
                        "complete": False,
                        "reason": "working_memory_budget",
                        "matches": (),
                        "scanned_records": scanned_records,
                        "working_memory_bytes": peak_working_memory,
                    }
                    return result
                retained_by_statement[statement_id] = match
                peak_working_memory = max(peak_working_memory, projected_memory)
        if callable(cooperative_check):
            cooperative_check()
        retained = tuple(
            sorted(
                retained_by_statement.values(),
                key=lambda value: (-value.get("similarity", 0.0), value.get("statement_id", "")),
            )
        )
        working_memory = max(peak_working_memory, query_memory + len(retained) * SEMANTIC_MATCH_WORKING_BYTES)
        result = {
            "complete": True,
            "reason": "semantic_candidates" if retained else "semantic_miss",
            "matches": retained,
            "scanned_records": scanned_records,
            "working_memory_bytes": working_memory,
        }
        return result
