"""Physical removal through Engram's authoritative response and feedback owners."""

from copy import deepcopy

from engram.coordination import MutationCoordinationError
from engram.errors import InvalidRequestError
from engram.support import validate_support_visibility

COMMAND_FIELDS = {"action", "operation_id", "visibility_scope", "dependency_ids"}


def validate_removal_command(value):
    """Validate a closed administrative envelope without interpreting support."""
    if not isinstance(value, dict) or set(value) != COMMAND_FIELDS:
        raise InvalidRequestError("engagement maintenance command fields are malformed")
    if not isinstance(value.get("action"), str) or value.get("action") not in {"plan", "prepare", "purge", "resume"}:
        raise InvalidRequestError("engagement maintenance action is not registered")
    operation = value.get("operation_id")
    if not isinstance(operation, str) or not operation.strip() or len(operation.encode("utf-8")) > 256:
        raise InvalidRequestError("engagement maintenance requires a bounded operation ID")
    try:
        scope = validate_support_visibility(value.get("visibility_scope"))
    except ValueError as error:
        raise InvalidRequestError(str(error)) from error
    if scope.get("kind") != "engagement":
        raise InvalidRequestError("removal requires an exact engagement scope")
    identifiers = value.get("dependency_ids")
    if not isinstance(identifiers, list) or any(not isinstance(item, str) or not item.strip() for item in identifiers):
        raise InvalidRequestError("removal dependency IDs must be nonempty strings in an array")
    return {
        "action": value.get("action"),
        "operation_id": operation,
        "visibility_scope": scope,
        "dependency_ids": sorted(set(identifiers)),
    }


class EngagementResponseRemoval:
    """One native removal transaction; caller owns the drained runtime barrier."""

    def __init__(self, coordinator, feedback):
        self.coordinator = coordinator
        self.feedback = feedback

    def selected_ids(self, artifacts, scope, dependencies, known_ids=()):
        selected = set(known_ids)
        for identifier, artifact in artifacts.items():
            if artifact.get("metadata", {}).get("visibility_scope") == scope or any(
                reference.get("visibility_scope") == scope or reference.get("id") in dependencies
                for reference in artifact.get("support_references", ())
            ):
                selected.add(identifier)
        while True:
            predecessors = {identifier for identifier, artifact in artifacts.items() if artifact.get("superseded_by") in selected}
            if predecessors.issubset(selected):
                return selected
            selected.update(predecessors)

    def receipt_statement_ids(self, receipt):
        """Read the declared artifact references in mutation receipt contracts."""
        result = receipt.get("result", {})
        references = {item.get("statement_id") for item in receipt.get("affected_generations", [])}
        references.update(result.get("statement_ids", []))
        references.update(result.get("evicted_statement_ids", []))
        references.update({result.get("statement_id"), result.get("replacement_statement_id")})
        references.update(
            binding.get("statement_id") for binding in receipt.get("scope_bindings", result.get("scope_bindings", []))
        )
        return references - {None, ""}

    def candidate(self, before, selected):
        after = deepcopy(before)
        repository = after.get("response", {}).get("repository", {})
        artifacts = repository.get("artifacts", {})
        for identifier in selected:
            artifacts.pop(identifier, {})
        if artifacts != before.get("response", {}).get("repository", {}).get("artifacts", {}):
            repository["state_generation"] = repository.get("state_generation", 0) + 1
        ledger = after.get("response", {}).get("mutation_receipts", {})
        ledger["receipts"] = [
            receipt for receipt in ledger.get("receipts", []) if not self.receipt_statement_ids(receipt).intersection(selected)
        ]
        ledger["tombstones"] = [
            receipt for receipt in ledger.get("tombstones", []) if not self.receipt_statement_ids(receipt).intersection(selected)
        ]
        feedback = after.get("feedback", {})
        feedback["statement_records"] = tuple(
            record for record in feedback.get("statement_records", ()) if record.get("key", {}).get("statement_id") not in selected
        )
        feedback["relationship_records"] = tuple(
            record
            for record in feedback.get("relationship_records", ())
            if record.get("key", {}).get("statement", {}).get("statement_id") not in selected
        )
        for field in ("policy_suppressions", "stale_exclusions"):
            feedback[field] = tuple(record for record in feedback.get(field, ()) if record.get("statement_id") not in selected)
        return after

    def execute(self, scope, dependency_ids, *, dry_run=True):
        """Select completely, then atomically delete artifacts and owned state."""
        command = validate_removal_command(
            {"action": "plan", "operation_id": "owner-validation", "visibility_scope": scope, "dependency_ids": dependency_ids}
        )
        if not isinstance(dry_run, bool):
            raise InvalidRequestError("removal dry_run must be a boolean")
        with self.coordinator.mutation(), self.feedback.internal_lock:
            before = {"response": self.coordinator.snapshot(), "feedback": self.feedback.snapshot()}
            repository = before.get("response", {}).get("repository", {})
            selected = self.selected_ids(
                repository.get("artifacts", {}), command.get("visibility_scope"), set(command.get("dependency_ids", []))
            )
            ledger = before.get("response", {}).get("mutation_receipts", {})
            for receipt in (*ledger.get("receipts", []), *ledger.get("tombstones", [])):
                for binding in receipt.get("scope_bindings", receipt.get("result", {}).get("scope_bindings", [])):
                    if binding.get("visibility_scope") == command.get("visibility_scope"):
                        selected.add(binding.get("statement_id"))
            selected = self.selected_ids(
                repository.get("artifacts", {}), command.get("visibility_scope"), set(command.get("dependency_ids", [])), selected
            )
            removed_artifacts = len(selected.intersection(repository.get("artifacts", {})))
            after = self.candidate(before, selected)
            report = {
                "dry_run": dry_run,
                "selected_statement_ids": sorted(selected),
                "removed_count": 0,
                "retained_count": len(repository.get("artifacts", {})) - removed_artifacts,
            }
            if dry_run or not selected:
                return report
            before_response, after_response = before.get("response", {}), after.get("response", {})
            try:
                if removed_artifacts:
                    self.coordinator.repository.atomic_replace(
                        after_response.get("repository", {}), repository.get("state_generation", 0)
                    )
                self.coordinator.mutation_receipts.replace_from_snapshot(after_response.get("mutation_receipts", {}))
                self.feedback.replace_from_snapshot(after.get("feedback", {}))
                self.coordinator.publication_hook(after_response)
            except Exception as error:
                try:
                    self.coordinator.repository.restore_state(repository)
                    self.coordinator.mutation_receipts.replace_from_snapshot(before_response.get("mutation_receipts", {}))
                    self.feedback.replace_from_snapshot(before.get("feedback", {}))
                    self.coordinator.publication_hook(before_response)
                except Exception as rollback_error:
                    raise MutationCoordinationError(
                        "engagement removal and rollback failed", live_state_changed=True
                    ) from rollback_error
                raise MutationCoordinationError(
                    "engagement removal failed and was rolled back", live_state_changed=False
                ) from error
            return {**report, "removed_count": removed_artifacts}
