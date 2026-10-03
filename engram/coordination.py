"""In-process coordination for accepted-response cache mutations."""

from contextlib import contextmanager
from logging import getLogger as logging_getLogger
from threading import RLock as threading_RLock

from engram.constants import COORDINATED_MUTATION_CANDIDATE_FIELDS, COORDINATED_RESPONSE_STATE_FIELDS
from engram.errors import ConflictError, EngramCoreError, InvalidRequestError
from engram.mutations import (
    MutationOperation,
    MutationReceiptLedger,
    validate_mutation_receipt,
)
from engram.repository import MAX_PLANNED_CANDIDATES, ArtifactRepository, validate_repository_state
from engram.support import validate_statement_scope_bindings, validate_support_visibility

logger = logging_getLogger(__name__)


class MutationCoordinationError(EngramCoreError):
    """An in-process cache mutation could not be published consistently."""

    def __init__(self, detail: str, *, live_state_changed: bool) -> None:
        self.detail = detail
        self.live_state_changed = live_state_changed
        super().__init__(detail)


def validate_coordinated_response_state(value: object, trusted_artifacts: object = ()) -> dict:
    """Validate and copy one complete in-process accepted-response state.

    ``mutation_receipts`` is a ledger snapshot dictionary, which is validated,
    or an in-process ``MutationReceiptLedger``, which already is. Repository
    artifacts equal to their entry in ``trusted_artifacts`` are not validated
    again.
    """
    if not isinstance(value, dict):
        raise InvalidRequestError("coordinated response state must be an object")
    if set(value) != COORDINATED_RESPONSE_STATE_FIELDS:
        raise InvalidRequestError("coordinated response state fields are malformed")
    try:
        validated_repository = validate_repository_state(value.get("repository", {}), trusted_artifacts)
    except InvalidRequestError as error:
        raise InvalidRequestError("coordinated repository must be a RepositoryState") from error
    mutation_receipts = value.get("mutation_receipts", {})
    if isinstance(mutation_receipts, MutationReceiptLedger):
        receipts: object = mutation_receipts
    elif isinstance(mutation_receipts, dict):
        receipts = MutationReceiptLedger(state=mutation_receipts).snapshot()
    else:
        raise InvalidRequestError("coordinated mutation receipts must be an object")
    result: dict = {
        "repository": validated_repository,
        "mutation_receipts": receipts,
    }
    return result


def validate_coordinated_mutation_candidate(value: object, trusted_artifacts: object = ()) -> dict:
    """Validate and copy one planned in-process mutation."""
    if not isinstance(value, dict):
        raise InvalidRequestError("coordinated mutation candidate must be an object")
    if set(value) != COORDINATED_MUTATION_CANDIDATE_FIELDS:
        raise InvalidRequestError("coordinated mutation candidate fields are malformed")
    result: dict = {
        "before": validate_coordinated_response_state(value.get("before", {}), trusted_artifacts),
        "after": validate_coordinated_response_state(value.get("after", {}), trusted_artifacts),
        "receipt": validate_mutation_receipt(value.get("receipt", {})),
    }
    return result


def validate_mutation_execution_result(value: object) -> dict:
    """Validate and copy one successful in-process mutation result."""
    if not isinstance(value, dict) or set(value) != {"receipt", "published"}:
        raise InvalidRequestError("mutation execution result fields are malformed")
    published = value.get("published", False)
    if not isinstance(published, bool) or not published:
        raise InvalidRequestError("a successful mutation execution must be published")
    result = {"receipt": validate_mutation_receipt(value.get("receipt", {})), "published": published}
    return result


def no_publication_hook(state: dict) -> None:
    """Accept publication when no derived in-process projection is configured.

    The coordinator validated the state before calling a hook, so this only
    checks its shape.
    """
    if not isinstance(state, dict) or set(state) != COORDINATED_RESPONSE_STATE_FIELDS:
        raise InvalidRequestError("coordinated response state fields are malformed")


def artifact_generation_changes(before: dict, after: dict, statement_ids: set) -> tuple[tuple[str, int, int], ...]:
    """Return statement and generation changes among ``statement_ids`` in stable order.

    Callers pass every statement ID of both states, or the IDs known to be the
    only ones that can differ.
    """
    changes = []
    before_artifacts = before.get("artifacts", {})
    after_artifacts = after.get("artifacts", {})
    for statement_id in sorted(statement_ids):
        before_artifact = before_artifacts.get(statement_id, {})
        after_artifact = after_artifacts.get(statement_id, {})
        before_generation = before_artifact.get("generation", 0)
        after_generation = after_artifact.get("generation", 0)
        if statement_id not in before_artifacts or statement_id not in after_artifacts or before_artifact != after_artifact:
            changes.append((statement_id, before_generation, after_generation))
    result = tuple(changes)
    return result


class AtomicMutationCoordinator:
    """Atomically publish repository and receipt changes inside one process.

    Candidates start from the live repository state and a clone of the live
    receipt ledger, so planning and publication validate only what changed.
    Staleness is detected from the repository state generation and the
    ledger revision.
    """

    def __init__(
        self,
        repository: ArtifactRepository,
        mutation_receipts: MutationReceiptLedger,
        *,
        publication_hook: object = no_publication_hook,
    ) -> None:
        if not isinstance(repository, ArtifactRepository):
            raise InvalidRequestError("coordinator repository must be an ArtifactRepository")
        if not isinstance(mutation_receipts, MutationReceiptLedger):
            raise InvalidRequestError("coordinator mutation_receipts must be a MutationReceiptLedger")
        if not callable(publication_hook):
            raise InvalidRequestError("coordinator publication_hook must be callable")
        self.lock = threading_RLock()
        self.repository = repository
        self.mutation_receipts = mutation_receipts
        self.publication_hook = publication_hook
        # Recent candidates this coordinator built: id -> (candidate, changed
        # statement IDs). Executing one of them unchanged skips revalidation.
        # A candidate must not be modified between build_candidate and execute.
        self.internal_planned: dict[int, tuple[dict, set]] = {}

    @contextmanager
    def mutation(self):
        """Serialize receipt lookup, candidate planning, and publication."""
        with self.lock, self.repository.coordinated_mutation():
            yield

    @property
    def next_receipt_sequence(self) -> int:
        with self.lock:
            return self.mutation_receipts.next_sequence

    def receipt_lookup(
        self,
        request_id: str,
        operation: MutationOperation,
        payload_signature: str,
    ) -> dict:
        with self.lock:
            result = self.mutation_receipts.lookup(request_id, operation, payload_signature)
            return result

    def snapshot(self) -> dict:
        """Return a copy of the live state that the caller may change."""
        with self.lock:
            result = {
                "repository": self.repository.snapshot(),
                "mutation_receipts": self.mutation_receipts.snapshot(),
            }
            return result

    def is_current(self, state: dict) -> bool:
        """Return whether a validated coordinated state still matches the live state."""
        live_repository = self.repository.trusted_state()
        repository = state.get("repository", {})
        repository_current = repository is live_repository or (
            repository.get("state_generation", 0) == live_repository.get("state_generation", 0)
            and repository.get("artifacts", {}) == live_repository.get("artifacts", {})
        )
        receipts = state.get("mutation_receipts", {})
        if isinstance(receipts, MutationReceiptLedger):
            receipts_current = (
                receipts.revision == self.mutation_receipts.revision
                and receipts.next_sequence == self.mutation_receipts.next_sequence
            )
        else:
            receipts_current = receipts == self.mutation_receipts.snapshot()
        result = repository_current and receipts_current
        return result

    def build_candidate(
        self,
        repository_candidate: dict,
        receipt: dict,
    ) -> dict:
        validated_receipt = validate_mutation_receipt(receipt)
        with self.lock, self.repository.coordinated_mutation():
            before_repository = self.repository.trusted_state()
            before_receipts = self.mutation_receipts.clone()
            before = {"repository": before_repository, "mutation_receipts": before_receipts}
            before_artifacts = before_repository.get("artifacts", {})
            planned = self.repository.planned_changes(repository_candidate, before_repository)
            if planned is None:
                try:
                    validated_repository_candidate = validate_repository_state(repository_candidate, before_artifacts)
                except InvalidRequestError as error:
                    raise InvalidRequestError("repository_candidate must be a RepositoryState") from error
                compared = set(before_artifacts) | set(validated_repository_candidate.get("artifacts", {}))
            else:
                # Built by the repository from this live state; only ``planned`` can differ.
                validated_repository_candidate = repository_candidate
                compared = set(planned)
            changes = artifact_generation_changes(before_repository, validated_repository_candidate, compared)
            repository_changed = bool(changes)
            before_generation = before_repository.get("state_generation", 0)
            candidate_generation = validated_repository_candidate.get("state_generation", 0)
            if repository_changed:
                if candidate_generation != before_generation + 1:
                    raise ConflictError("changed repository candidate must advance state_generation by one")
            elif candidate_generation != before_generation:
                raise ConflictError("unchanged repository candidate must retain state_generation")
            expected_changes = tuple(
                (change.get("statement_id", ""), change.get("before_generation", 0), change.get("after_generation", 0))
                for change in validated_receipt.get("affected_generations", ())
            )
            if changes != expected_changes:
                raise ConflictError("mutation receipt affected generations do not match repository changes")
            bindings = {}
            for identifier, _, _ in expected_changes:
                for state in (before_repository, validated_repository_candidate):
                    artifact = state.get("artifacts", {}).get(identifier, {})
                    scopes = [reference.get("visibility_scope", {}) for reference in artifact.get("support_references", ())]
                    if "visibility_scope" in artifact.get("metadata", {}):
                        scopes.append(artifact.get("metadata", {}).get("visibility_scope", {}))
                    for value in scopes:
                        scope = validate_support_visibility(value)
                        key = (
                            identifier,
                            scope.get("kind", ""),
                            *(scope.get(field, "") or "" for field in ("company_id", "customer_id", "engagement_id")),
                        )
                        bindings[key] = {"statement_id": identifier, "visibility_scope": scope}
            validated_receipt.get("result", {}).pop("scope_bindings", {})
            if bindings:
                validated_receipt.get("result", {})["scope_bindings"] = validate_statement_scope_bindings(
                    tuple(bindings.get(key, {}) for key in sorted(bindings))
                )
                validated_receipt = validate_mutation_receipt(validated_receipt)
            receipts = before_receipts.clone()
            receipts.record(validated_receipt)
            after = {"repository": validated_repository_candidate, "mutation_receipts": receipts}
            result = {"before": before, "after": after, "receipt": validated_receipt}
            while len(self.internal_planned) >= MAX_PLANNED_CANDIDATES:
                del self.internal_planned[next(iter(self.internal_planned))]
            self.internal_planned[id(result)] = (result, {change[0] for change in changes})
            return result

    def execute(self, candidate: dict) -> dict:
        with self.lock, self.repository.coordinated_mutation():
            live_repository = self.repository.trusted_state()
            live_artifacts = live_repository.get("artifacts", {})
            live_generation = live_repository.get("state_generation", 0)
            planned = self.internal_planned.pop(id(candidate), ())
            # Only a candidate this coordinator built knows exactly which
            # statements changed; any other candidate is compared in full.
            changed_known = bool(planned) and planned[0] is candidate
            changed: set = planned[1] if changed_known else set()
            if changed_known:
                validated_candidate = candidate
                planned_after = candidate.get("after", {})
                planned_repository = planned_after.get("repository", {})
                # Publish a fresh top-level map, so the caller's candidate never aliases live state.
                after_repository = {
                    "state_generation": planned_repository.get("state_generation", 0),
                    "artifacts": dict(planned_repository.get("artifacts", {})),
                }
                after_receipts = planned_after.get("mutation_receipts", {})
                after = {"repository": after_repository, "mutation_receipts": after_receipts}
            else:
                validated_candidate = validate_coordinated_mutation_candidate(candidate, live_artifacts)
                after = validated_candidate.get("after", {})
                after_repository = after.get("repository", {})
                after_receipts = after.get("mutation_receipts", {})
            if not self.is_current(validated_candidate.get("before", {})):
                raise ConflictError("coordinated mutation candidate is stale")
            # Roll back to the live objects, not to their validated copies.
            before_receipts = self.mutation_receipts.clone()
            before = {"repository": live_repository, "mutation_receipts": before_receipts}
            repository_changed = bool(changed) if changed_known else live_artifacts != after_repository.get("artifacts", {})
            try:
                if repository_changed and changed_known:
                    self.repository.replace_trusted(after_repository, live_generation, changed)
                elif repository_changed:
                    self.repository.replace_trusted(after_repository, live_generation)
                self.publication_hook(after)
                self.mutation_receipts.replace_from_snapshot(after_receipts)
            except Exception as error:
                try:
                    if changed_known:
                        self.repository.restore_trusted(live_repository, changed)
                    else:
                        self.repository.restore_trusted(live_repository)
                    self.publication_hook(before)
                    self.mutation_receipts.replace_from_snapshot(before_receipts)
                except Exception as rollback_error:
                    logger.error("Cache mutation publication failed", exc_info=error)
                    logger.error("Cache mutation rollback failed", exc_info=rollback_error)
                    raise MutationCoordinationError(
                        "cache mutation publication failed and rollback failed",
                        live_state_changed=True,
                    ) from rollback_error
                logger.error("Cache mutation publication failed and was rolled back", exc_info=error)
                raise MutationCoordinationError(
                    "cache mutation publication failed and was rolled back",
                    live_state_changed=False,
                ) from error
            execution = {"receipt": validated_candidate.get("receipt", {}), "published": True}
            result = validate_mutation_execution_result(execution)
            return result
