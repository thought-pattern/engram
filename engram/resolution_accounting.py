"""Exactly-once accounting finalization of the response artifacts one resolution observed and accepted."""

from hashlib import sha256 as hashlib_sha256
from threading import RLock as threading_RLock

from engram.constants_feedback import MAX_STATEMENT_ID_BYTES
from engram.constants_resolution import ACCOUNTING_FINALIZATION_FIELDS, MAX_ACCOUNTING_VISIBLE_STATEMENT_IDS, MAX_RESOURCE_COUNTER
from engram.errors import (
    ConflictError,
    InvalidRequestError,
)
from engram.feedback_values import (
    canonical_fingerprint,
)
from engram.fusion import EngramCandidateAuthority
from engram.resolution import (
    resolver_result_to_dict,
    validate_resolver_result,
)


def accounting_finalization(
    candidate_statement_ids: object,
    accepted_statement_id: object,
    candidacy_applied: object,
    success_applied: object,
    idempotent: object,
) -> dict:
    """Build an inspectable exactly-once accounting result."""
    if not isinstance(candidate_statement_ids, tuple) or not all(
        isinstance(value, str) and value and len(value.encode("utf-8")) <= MAX_STATEMENT_ID_BYTES
        for value in candidate_statement_ids
    ):
        raise InvalidRequestError("candidate_statement_ids must be a tuple of bounded non-empty strings")
    if len(candidate_statement_ids) > MAX_RESOURCE_COUNTER:
        raise InvalidRequestError("candidate_statement_ids exceed the resource limit")
    if tuple(sorted(set(candidate_statement_ids))) != candidate_statement_ids:
        raise InvalidRequestError("candidate_statement_ids must be sorted and unique")
    if not isinstance(accepted_statement_id, str) or len(accepted_statement_id.encode("utf-8")) > MAX_STATEMENT_ID_BYTES:
        raise InvalidRequestError("accepted_statement_id must be a bounded string")
    if accepted_statement_id and accepted_statement_id not in candidate_statement_ids:
        raise InvalidRequestError("accepted_statement_id must identify an observed candidate")
    if not isinstance(candidacy_applied, bool) or not isinstance(success_applied, bool) or not isinstance(idempotent, bool):
        raise InvalidRequestError("accounting finalization flags must be booleans")
    if success_applied and not accepted_statement_id:
        raise InvalidRequestError("success accounting requires an accepted statement")
    result: dict = {
        "candidate_statement_ids": candidate_statement_ids,
        "accepted_statement_id": accepted_statement_id,
        "candidacy_applied": candidacy_applied,
        "success_applied": success_applied,
        "idempotent": idempotent,
    }
    return result


def validate_accounting_finalization(value: object) -> dict:
    if not isinstance(value, dict) or set(value) != ACCOUNTING_FINALIZATION_FIELDS:
        raise InvalidRequestError("AccountingFinalization has invalid fields")
    result = accounting_finalization(
        value.get("candidate_statement_ids", ()),
        value.get("accepted_statement_id", ""),
        value.get("candidacy_applied", False),
        value.get("success_applied", False),
        value.get("idempotent", False),
    )
    return result


def accounting_finalization_to_dict(value: object) -> dict[str, object]:
    current = validate_accounting_finalization(value)
    candidate_statement_ids = current.get("candidate_statement_ids", ())
    visible_ids = candidate_statement_ids[:MAX_ACCOUNTING_VISIBLE_STATEMENT_IDS]
    result = {
        "candidate_statement_ids": list(visible_ids),
        "omitted_candidate_statement_id_count": len(candidate_statement_ids) - len(visible_ids),
        "accepted_statement_id": current.get("accepted_statement_id", ""),
        "candidacy_applied": current.get("candidacy_applied", False),
        "success_applied": current.get("success_applied", False),
        "idempotent": current.get("idempotent", False),
    }
    return result


def accounting_diagnostics(finalization: dict, compact: bool) -> dict[str, object]:
    """Report accounting in resolution diagnostics; the compact form omits statement ids."""
    if compact:
        result = {
            "candidate_count": len(finalization.get("candidate_statement_ids", ())),
            "accepted_present": bool(finalization.get("accepted_statement_id", "")),
            "candidacy_applied": finalization.get("candidacy_applied", False),
            "success_applied": finalization.get("success_applied", False),
            "idempotent": finalization.get("idempotent", False),
        }
    else:
        result = accounting_finalization_to_dict(finalization)
    return result


def accounting_signature(results: tuple[dict, ...], accepted_statement_id: str) -> str:
    """Return the retry identity for one stable accounting observation set."""
    stable_results = []
    for result in results:
        value = resolver_result_to_dict(result)
        raw_consumption = value.get("consumption", {})
        if not isinstance(raw_consumption, dict):
            raise InvalidRequestError("resolver result consumption must be an object")
        consumption = dict(raw_consumption)
        consumption["elapsed_ns"] = 0
        value["consumption"] = consumption
        stable_results.append(value)
    payload = {
        "accepted_statement_id": accepted_statement_id,
        "results": stable_results,
    }
    result = canonical_fingerprint(payload)
    return result


def accounting_receipt_id(kind: str, request_id: str) -> str:
    """Return a bounded non-disclosing receipt identity."""
    digest = hashlib_sha256(request_id.encode("utf-8")).hexdigest()
    result = f"resolution:{kind}:sha256:{digest}"
    return result


class ResolutionAccountingFinalizer:
    """Exactly-once boundary for authoritative response-artifact accounting."""

    def __init__(self, engram, accepted_response_service, max_requests: int = 1_000) -> None:
        self.internal_engram = engram
        if not callable(getattr(accepted_response_service, "finalize_resolution_accounting", ())):
            raise InvalidRequestError("accepted-response accounting service is required")
        self.internal_accepted_response_service = accepted_response_service
        if isinstance(max_requests, bool) or not isinstance(max_requests, int) or max_requests < 1:
            raise InvalidRequestError("accounting max_requests must be a positive integer")
        self.internal_max_requests = max_requests
        self.internal_lock = threading_RLock()
        self.internal_requests: dict[str, tuple[str, dict]] = {}

    @property
    def candidate_authority(self) -> EngramCandidateAuthority:
        """Return current-state authority bound to the same Engram as accounting."""
        result = EngramCandidateAuthority(self.internal_engram)
        return result

    def clear(self) -> None:
        """Forget every transient retry record when the owning result cache is cleared."""
        with self.internal_lock:
            self.internal_requests.clear()

    def discard(self, request_id: str) -> None:
        """Forget one transient retry record after the owning result cache evicts it."""
        if not isinstance(request_id, str) or not request_id:
            raise InvalidRequestError("accounting request_id must be a non-empty string")
        with self.internal_lock:
            if request_id in self.internal_requests:
                del self.internal_requests[request_id]

    def finalize(
        self,
        request_id: str,
        results: tuple[dict, ...],
        accepted_statement_id: str = "",
    ) -> dict:
        if not isinstance(request_id, str) or not request_id:
            raise InvalidRequestError("accounting request_id must be a non-empty string")
        if not isinstance(results, tuple):
            raise InvalidRequestError("accounting results must be a tuple of ResolverResult values")
        try:
            results = tuple(validate_resolver_result(value) for value in results)
        except InvalidRequestError as err:
            raise InvalidRequestError("accounting results must be a tuple of ResolverResult values") from err
        if not isinstance(accepted_statement_id, str):
            raise InvalidRequestError("accepted_statement_id must be a string")
        signature = accounting_signature(results, accepted_statement_id)
        with self.internal_lock:
            if request_id in self.internal_requests:
                previous_signature, previous = self.internal_requests.get(request_id, ("", {}))
                if previous_signature != signature:
                    raise ConflictError("accounting request_id is associated with different observations")
                result = accounting_finalization(
                    previous.get("candidate_statement_ids", ()),
                    previous.get("accepted_statement_id", ""),
                    previous.get("candidacy_applied", False),
                    previous.get("success_applied", False),
                    True,
                )
                return result
            observations = {
                observation.get("statement_id", ""): observation
                for resolver_value in results
                for observation in resolver_value.get("accounting", ())
            }
            candidate_ids = tuple(sorted(observations))
            repository = self.internal_engram.response_repository
            missing_ids = tuple(statement_id for statement_id in candidate_ids if not repository.has_artifact(statement_id))
            if missing_ids:
                raise InvalidRequestError("observed accepted response no longer exists")
            if accepted_statement_id and accepted_statement_id not in observations:
                raise InvalidRequestError("accepted_statement_id was not an observed candidate")
            accounting_replayed = False
            if candidate_ids:
                accounting_mutation = self.internal_accepted_response_service.finalize_resolution_accounting(
                    candidate_ids,
                    accepted_statement_id,
                    accounting_receipt_id("finalize", request_id),
                )
                accounting_replayed = bool(accounting_mutation.get("replayed", False))
            finalization = accounting_finalization(
                candidate_ids,
                accepted_statement_id,
                True,
                bool(accepted_statement_id),
                accounting_replayed,
            )
            self.internal_requests[request_id] = (signature, finalization)
            while len(self.internal_requests) > self.internal_max_requests:
                self.internal_requests.pop(next(iter(self.internal_requests)))
            return finalization
