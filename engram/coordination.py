"""In-process coordination for accepted-response cache mutations."""

from collections.abc import Iterable
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

logger = logging_getLogger(__name__)


class MutationCoordinationError(EngramCoreError):
    """An in-process cache mutation could not be published consistently."""

    def __init__(self, detail: str, *, live_state_changed: bool) -> None:
        self.detail = detail
        self.live_state_changed = live_state_changed
        super().__init__(detail)


def coordinated_response_state(repository: dict, mutation_receipts: object, trusted_artifacts: object = ()) -> dict:
    """Validate one complete in-process accepted-response state.

    ``mutation_receipts`` is a ledger snapshot dictionary, which is validated,
    or an in-process ``MutationReceiptLedger``, which already is. Repository
    artifacts equal to their entry in ``trusted_artifacts`` are not validated
    again.
    """
    try:
        validated_repository = validate_repository_state(repository, trusted_artifacts)
    except InvalidRequestError as error:
        raise InvalidRequestError("coordinated repository must be a RepositoryState") from error
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


def validate_coordinated_response_state(value: object, trusted_artifacts: object = ()) -> dict:
    """Validate and copy one coordinated response state."""
    if not isinstance(value, dict):
        raise InvalidRequestError("coordinated response state must be an object")
    if set(value) != COORDINATED_RESPONSE_STATE_FIELDS:
        raise InvalidRequestError("coordinated response state fields are malformed")
    result = coordinated_response_state(value.get("repository", ()), value.get("mutation_receipts", ()), trusted_artifacts)
    return result


def coordinated_mutation_candidate(
    before: object,
    after: object,
    receipt: dict,
    trusted_artifacts: object = (),
) -> dict:
    """Validate one planned in-process mutation."""
    result: dict = {
        "before": validate_coordinated_response_state(before, trusted_artifacts),
        "after": validate_coordinated_response_state(after, trusted_artifacts),
        "receipt": validate_mutation_receipt(receipt),
    }
    return result


def validate_coordinated_mutation_candidate(value: object, trusted_artifacts: object = ()) -> dict:
    """Validate and copy one coordinated mutation candidate."""
    if not isinstance(value, dict):
        raise InvalidRequestError("coordinated mutation candidate must be an object")
    if set(value) != COORDINATED_MUTATION_CANDIDATE_FIELDS:
        raise InvalidRequestError("coordinated mutation candidate fields are malformed")
    result = coordinated_mutation_candidate(
        value.get("before", ()),
        value.get("after", ()),
        value.get("receipt", ()),
        trusted_artifacts,
    )
    return result


def mutation_execution_result(receipt: dict, published: bool) -> dict:
    """Validate one successful in-process mutation result."""
    if not isinstance(published, bool) or not published:
        raise InvalidRequestError("a successful mutation execution must be published")
    result = {"receipt": validate_mutation_receipt(receipt), "published": published}
    return result


def validate_mutation_execution_result(value: object) -> dict:
    """Validate and copy one mutation execution result."""
    if not isinstance(value, dict) or set(value) != {"receipt", "published"}:
        raise InvalidRequestError("mutation execution result fields are malformed")
    result = mutation_execution_result(value.get("receipt", ()), value.get("published", False))
    return result


def no_publication_hook(state: dict) -> None:
    """Accept publication when no derived in-process projection is configured.

    The coordinator validated the state before calling a hook, so this only
    checks its shape.
    """
    if not isinstance(state, dict) or set(state) != COORDINATED_RESPONSE_STATE_FIELDS:
        raise InvalidRequestError("coordinated response state fields are malformed")


def artifact_generation_changes(
    before: dict, after: dict, statement_ids: Iterable[str] | None = None
) -> tuple[tuple[str, int, int], ...]:
    """Return statement and generation changes in stable order.

    ``statement_ids`` limits the comparison to IDs known to be the only ones
    that can differ.
    """
    changes = []
    before_artifacts = before.get("artifacts", ())
    after_artifacts = after.get("artifacts", ())
    compared = set(before_artifacts) | set(after_artifacts) if statement_ids is None else set(statement_ids)
    for statement_id in sorted(compared):
        before_generation = before_artifacts[statement_id]["generation"] if statement_id in before_artifacts else 0
        after_generation = after_artifacts[statement_id]["generation"] if statement_id in after_artifacts else 0
        if (
            statement_id not in before_artifacts
            or statement_id not in after_artifacts
            or before_artifacts[statement_id] != after_artifacts[statement_id]
        ):
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
        self.internal_planned: dict[int, tuple[dict, frozenset[str]]] = {}

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

    def trusted_state(self) -> dict:
        """Return the live repository state and a ledger clone for package-internal planning."""
        with self.lock:
            result = {
                "repository": self.repository.trusted_state(),
                "mutation_receipts": self.mutation_receipts.clone(),
            }
            return result

    def is_current(self, state: dict) -> bool:
        """Return whether a validated coordinated state still matches the live state."""
        live_repository = self.repository.trusted_state()
        repository = state["repository"]
        repository_current = repository is live_repository or (
            repository["state_generation"] == live_repository["state_generation"]
            and repository["artifacts"] == live_repository["artifacts"]
        )
        receipts = state["mutation_receipts"]
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
            before = self.trusted_state()
            before_repository = before["repository"]
            planned = self.repository.planned_changes(repository_candidate, before_repository)
            if planned is None:
                try:
                    validated_repository_candidate = validate_repository_state(repository_candidate, before_repository["artifacts"])
                except InvalidRequestError as error:
                    raise InvalidRequestError("repository_candidate must be a RepositoryState") from error
            else:
                # Built by the repository from this live state; only ``planned`` can differ.
                validated_repository_candidate = repository_candidate
            changes = artifact_generation_changes(before_repository, validated_repository_candidate, planned)
            repository_changed = bool(changes)
            if repository_changed:
                if validated_repository_candidate["state_generation"] != before_repository["state_generation"] + 1:
                    raise ConflictError("changed repository candidate must advance state_generation by one")
            elif validated_repository_candidate["state_generation"] != before_repository["state_generation"]:
                raise ConflictError("unchanged repository candidate must retain state_generation")
            expected_changes = tuple(
                (change["statement_id"], change["before_generation"], change["after_generation"])
                for change in validated_receipt["affected_generations"]
            )
            if changes != expected_changes:
                raise ConflictError("mutation receipt affected generations do not match repository changes")
            receipts = before["mutation_receipts"].clone()
            receipts.record(validated_receipt)
            after = {"repository": validated_repository_candidate, "mutation_receipts": receipts}
            result = {"before": before, "after": after, "receipt": validated_receipt}
            while len(self.internal_planned) >= MAX_PLANNED_CANDIDATES:
                del self.internal_planned[next(iter(self.internal_planned))]
            self.internal_planned[id(result)] = (result, frozenset(change[0] for change in changes))
            return result

    def execute(self, candidate: dict) -> dict:
        with self.lock, self.repository.coordinated_mutation():
            live_repository = self.repository.trusted_state()
            planned = self.internal_planned.pop(id(candidate), None)
            changed: frozenset[str] | None = None
            if planned is not None and planned[0] is candidate:
                validated_candidate = candidate
                changed = planned[1]
                after = candidate["after"]
                # Publish a fresh top-level map, so the caller's candidate never aliases live state.
                after_repository = {
                    "state_generation": after["repository"]["state_generation"],
                    "artifacts": dict(after["repository"]["artifacts"]),
                }
                after = {"repository": after_repository, "mutation_receipts": after["mutation_receipts"]}
            else:
                validated_candidate = validate_coordinated_mutation_candidate(candidate, live_repository["artifacts"])
                after = validated_candidate["after"]
                after_repository = after["repository"]
            if not self.is_current(validated_candidate["before"]):
                raise ConflictError("coordinated mutation candidate is stale")
            # Roll back to the live objects, not to their validated copies.
            before = {"repository": live_repository, "mutation_receipts": self.mutation_receipts.clone()}
            repository_changed = live_repository["artifacts"] != after_repository["artifacts"] if changed is None else bool(changed)
            try:
                if repository_changed:
                    self.repository.replace_trusted(after_repository, live_repository["state_generation"], changed)
                self.publication_hook(after)
                self.mutation_receipts.replace_from_snapshot(after["mutation_receipts"])
            except Exception as error:
                try:
                    self.repository.restore_trusted(live_repository, changed)
                    self.publication_hook(before)
                    self.mutation_receipts.replace_from_snapshot(before["mutation_receipts"])
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
            result = mutation_execution_result(validated_candidate["receipt"], True)
            return result
