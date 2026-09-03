"""In-process coordination for accepted-response cache mutations."""

from contextlib import contextmanager
from threading import RLock as threading_RLock

from engram.constants import COORDINATED_MUTATION_CANDIDATE_FIELDS, COORDINATED_RESPONSE_STATE_FIELDS
from engram.errors import ConflictError, EngramCoreError, InvalidRequestError
from engram.mutations import (
    MutationOperation,
    MutationReceiptLedger,
    validate_mutation_receipt,
)
from engram.repository import ArtifactRepository, validate_repository_state


class MutationCoordinationError(EngramCoreError):
    """An in-process cache mutation could not be published consistently."""

    def __init__(self, detail: str, *, live_state_changed: bool) -> None:
        self.detail = detail
        self.live_state_changed = live_state_changed
        super().__init__(detail)


def coordinated_response_state(repository: dict, mutation_receipts: object) -> dict:
    """Validate one complete in-process accepted-response state."""
    try:
        validated_repository = validate_repository_state(repository)
    except InvalidRequestError as error:
        raise InvalidRequestError("coordinated repository must be a RepositoryState") from error
    if not isinstance(mutation_receipts, dict):
        raise InvalidRequestError("coordinated mutation receipts must be an object")
    ledger = MutationReceiptLedger(state=mutation_receipts)
    result: dict = {
        "repository": validated_repository,
        "mutation_receipts": ledger.snapshot(),
    }
    return result


def validate_coordinated_response_state(value: object) -> dict:
    """Validate and copy one coordinated response state."""
    if not isinstance(value, dict):
        raise InvalidRequestError("coordinated response state must be an object")
    if set(value) != COORDINATED_RESPONSE_STATE_FIELDS:
        raise InvalidRequestError("coordinated response state fields are malformed")
    result = coordinated_response_state(value.get("repository", ()), value.get("mutation_receipts", ()))
    return result


def coordinated_mutation_candidate(
    before: object,
    after: object,
    receipt: dict,
) -> dict:
    """Validate one planned in-process mutation."""
    result: dict = {
        "before": validate_coordinated_response_state(before),
        "after": validate_coordinated_response_state(after),
        "receipt": validate_mutation_receipt(receipt),
    }
    return result


def validate_coordinated_mutation_candidate(value: object) -> dict:
    """Validate and copy one coordinated mutation candidate."""
    if not isinstance(value, dict):
        raise InvalidRequestError("coordinated mutation candidate must be an object")
    if set(value) != COORDINATED_MUTATION_CANDIDATE_FIELDS:
        raise InvalidRequestError("coordinated mutation candidate fields are malformed")
    result = coordinated_mutation_candidate(value.get("before", ()), value.get("after", ()), value.get("receipt", ()))
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
    """Accept publication when no derived in-process projection is configured."""
    validate_coordinated_response_state(state)


def artifact_generation_changes(before: dict, after: dict) -> tuple[tuple[str, int, int], ...]:
    """Return statement and generation changes in stable order."""
    changes = []
    before_artifacts = before.get("artifacts", ())
    after_artifacts = after.get("artifacts", ())
    for statement_id in sorted(set(before_artifacts) | set(after_artifacts)):
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
    """Atomically publish repository and receipt changes inside one process."""

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
        with self.lock:
            result = coordinated_response_state(self.repository.snapshot(), self.mutation_receipts.snapshot())
            return result

    def build_candidate(
        self,
        repository_candidate: dict,
        receipt: dict,
    ) -> dict:
        try:
            validated_repository_candidate = validate_repository_state(repository_candidate)
        except InvalidRequestError as error:
            raise InvalidRequestError("repository_candidate must be a RepositoryState") from error
        validated_receipt = validate_mutation_receipt(receipt)
        with self.lock, self.repository.coordinated_mutation():
            before = self.snapshot()
            before_repository = before["repository"]
            repository_changed = before_repository["artifacts"] != validated_repository_candidate["artifacts"]
            if repository_changed:
                if validated_repository_candidate["state_generation"] != before_repository["state_generation"] + 1:
                    raise ConflictError("changed repository candidate must advance state_generation by one")
            elif validated_repository_candidate["state_generation"] != before_repository["state_generation"]:
                raise ConflictError("unchanged repository candidate must retain state_generation")
            expected_changes = tuple(
                (change["statement_id"], change["before_generation"], change["after_generation"])
                for change in validated_receipt["affected_generations"]
            )
            if artifact_generation_changes(before_repository, validated_repository_candidate) != expected_changes:
                raise ConflictError("mutation receipt affected generations do not match repository changes")
            receipts = MutationReceiptLedger(state=before["mutation_receipts"])
            receipts.record(validated_receipt)
            after = coordinated_response_state(validated_repository_candidate, receipts.snapshot())
            result = coordinated_mutation_candidate(before, after, validated_receipt)
            return result

    def execute(self, candidate: dict) -> dict:
        validated_candidate = validate_coordinated_mutation_candidate(candidate)
        before = validated_candidate["before"]
        after = validated_candidate["after"]
        with self.lock, self.repository.coordinated_mutation():
            if self.snapshot() != before:
                raise ConflictError("coordinated mutation candidate is stale")
            before_repository = before["repository"]
            after_repository = after["repository"]
            repository_changed = before_repository["artifacts"] != after_repository["artifacts"]
            try:
                if repository_changed:
                    self.repository.atomic_replace(after_repository, before_repository["state_generation"])
                self.publication_hook(after)
                self.mutation_receipts.replace_from_snapshot(after["mutation_receipts"])
            except Exception as error:
                try:
                    self.repository.restore_state(before_repository)
                    self.publication_hook(before)
                    self.mutation_receipts.replace_from_snapshot(before["mutation_receipts"])
                except Exception as rollback_error:
                    raise MutationCoordinationError(
                        f"cache mutation publication failed and rollback failed: {error}; {rollback_error}",
                        live_state_changed=True,
                    ) from rollback_error
                raise MutationCoordinationError(
                    f"cache mutation publication failed and was rolled back: {error}",
                    live_state_changed=False,
                ) from error
            result = mutation_execution_result(validated_candidate["receipt"], True)
            return result
