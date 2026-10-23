"""Request validation and identity for the Engram service.

Cache-request admission, request signatures, accounting request ids, service user id
normalization and the default cancellation check shared by the service and its
transports.
"""

from hashlib import sha256 as hashlib_sha256
from json import dumps as json_dumps

from engram import sessions
from engram.constants_artifacts import MAX_CALLER_ID_BYTES
from engram.constants_mutations import MAX_SIGNATURE_INPUT_BYTES
from engram.constants_service import MAX_CACHE_REQUEST_BYTES
from engram.errors import InvalidRequestError
from engram.identity import normalize_retrieval_key
from engram.validation import require_any_text


def require_cache_request(request: str) -> None:
    """Reject a regulated-cache request that cannot become a lookup key, before any other work.

    The request is normalized into an exact-match key. Normalization expands
    contractions and some characters, so the normalized form is checked too.
    """
    require_any_text(request, "request", MAX_CACHE_REQUEST_BYTES, blank_is_empty=True)
    normalized = normalize_retrieval_key(request)
    if not normalized:
        raise InvalidRequestError("request must contain searchable text")
    if len(normalized.encode("utf-8")) > MAX_CACHE_REQUEST_BYTES:
        raise InvalidRequestError(
            f"request exceeds the UTF-8 limit of {MAX_CACHE_REQUEST_BYTES} bytes once normalized; "
            "normalization expands contractions and some characters"
        )


def service_request_signature(**values) -> str:
    """Return the canonical retry identity of one idempotent service request.

    The text is compared for equality with the identity retained for the same
    request ID and is never parsed. Request values are JSON-shaped, so canonical
    JSON gives the exact identity the request contract defines: lists and tuples
    match, while Booleans, integers and floats stay distinct, which native
    equality would merge. Its UTF-8 length bounds the retained identity.
    """
    try:
        result = json_dumps(values, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError) as err:
        raise InvalidRequestError("metadata and request values must be JSON-compatible") from err
    try:
        encoded = result.encode("utf-8")
    except UnicodeEncodeError as err:
        raise InvalidRequestError("metadata and request values must contain valid Unicode") from err
    if len(encoded) > MAX_SIGNATURE_INPUT_BYTES:
        raise InvalidRequestError(f"service request exceeds the UTF-8 limit of {MAX_SIGNATURE_INPUT_BYTES} bytes")
    return result


def accounting_request_id(kind: str, external_id: str) -> str:
    """Derive a bounded, non-disclosing identity for internal accounting."""
    digest = hashlib_sha256(external_id.encode("utf-8")).hexdigest()
    result = f"internal:{kind}:sha256:{digest}"
    return result


def normalize_service_user_id(user_id: str) -> str:
    """Normalize a service user identity and translate boundary errors."""
    try:
        result = sessions.normalize_user_id(user_id)
        require_any_text(result, "user_id", MAX_CALLER_ID_BYTES, blank_is_empty=True)
        return result
    except ValueError as err:
        raise InvalidRequestError(str(err)) from err


def no_cancellation_check() -> bool:
    """Provide the concrete no-op cancellation operation."""
    return False
