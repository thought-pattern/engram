"""Scoped hard deletion, owner rollback and a real drained Engram barrier."""

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from threading import Event

from pytest import mark, raises

from engram.constants import CoreState
from engram.coordination import MutationCoordinationError
from engram.core import Engram
from engram.errors import InvalidRequestError, LifecycleError
from engram.mutations import MutationReceiptLedger
from engram.service import EngramCore
from tests.support_fixtures import ASSERTION_REFERENCE_A
from tests.test_support_scope_metadata import SCOPES


class RemovalExample:
    def __init__(self):
        self.core = EngramCore()
        self.ids = {}
        self.scope = deepcopy(SCOPES.get("engagement_a", {}))
        self.dependency = {**deepcopy(ASSERTION_REFERENCE_A), "visibility_scope": self.scope}
        for name, scope in SCOPES.items():
            self.learn(name, scope, [] if name != "engagement_a" else [self.dependency])
        self.learn("private-without-support", self.scope, [])
        # An opaque support ID still selects a response whose own stored scope
        # disagrees. Engram does not reinterpret Tapestry's epistemic identity.
        self.learn("dependent", SCOPES.get("public", {}), [{**self.dependency, "visibility_scope": SCOPES.get("public", {})}])

    def learn(self, name, scope, support):
        question = "What is " + name + " marker?"
        learned = self.core.learn_response(
            question,
            name + " is the marker.",
            "learn-" + name,
            user_id="0",
            metadata={"visibility_scope": scope, "support": support, "support_complete": bool(support)},
        )
        self.ids[name] = learned.get("statement_id")
        result = self.core.resolve_request(question, "resolve-" + name, user_id="0", configured_resolvers=("exact",))
        assert result.get("response_candidates")
        self.core.record_resolution_feedback("resolve-" + name, "feedback-" + name, "accepted", learned.get("statement_id", ""))

    def command(self, action):
        return {
            "action": action,
            "operation_id": "removal-operation",
            "visibility_scope": deepcopy(self.scope),
            "dependency_ids": [self.dependency.get("id")],
        }

    def snapshot(self):
        return {"response": self.core.response_coordinator.snapshot(), "feedback": self.core.engram.feedback_store.snapshot()}


def test_exact_scope_purge_preserves_peer_artifacts_statistics_feedback_and_replay():
    example = RemovalExample()
    core = example.core
    try:
        before = example.snapshot()
        removed = {example.ids.get(name) for name in ("engagement_a", "private-without-support", "dependent")}
        plan = core.maintain_engagement(example.command("plan"))
        assert plan.get("dry_run") is True
        assert set(plan.get("selected_statement_ids", [])) == removed
        assert example.snapshot() == before
        assert core.maintain_engagement(example.command("prepare")).get("drained") is True
        assert core.status().get("ready") is False
        with raises(LifecycleError, match="before a successful purge"):
            core.maintain_engagement(example.command("resume"))
        result = core.maintain_engagement(example.command("purge"))
        assert result.get("removed_count") == 3
        after = example.snapshot()
        before_response, after_response = before.get("response", {}), after.get("response", {})
        assert after_response.get("repository", {}).get("artifacts") == {
            identifier: record
            for identifier, record in before_response.get("repository", {}).get("artifacts", {}).items()
            if identifier not in removed
        }
        kept_receipts = [
            receipt
            for receipt in before_response.get("mutation_receipts", {}).get("receipts", [])
            if not core.response_removal.receipt_statement_ids(receipt).intersection(removed)
        ]
        assert after_response.get("mutation_receipts", {}).get("receipts") == kept_receipts
        assert kept_receipts
        for field in ("statement_records", "relationship_records"):
            rows = after.get("feedback", {}).get(field, ())
            assert rows
            for record in rows:
                key = record.get("key", {})
                assert key.get("statement_id", key.get("statement", {}).get("statement_id")) not in removed
            assert all(record in before.get("feedback", {}).get(field, ()) for record in rows)
        assert core.conversations == core.proposals == core.resolution_requests == core.engram.sessions == {}
        assert core.maintain_engagement(example.command("purge")).get("removed_count") == 0
        assert example.snapshot() == after
        assert core.maintain_engagement(example.command("resume")).get("state") == "running"
        assert core.maintain_engagement(example.command("resume")).get("already_running") is True
        public = core.learn_response(
            "What is public marker?",
            "public is the marker.",
            "learn-public",
            user_id="0",
            metadata={"visibility_scope": SCOPES.get("public"), "support": [], "support_complete": False},
        )
        assert public.get("idempotent") is True
        assert public.get("statement_id") == example.ids.get("public")
    finally:
        core.close()


def test_failed_publication_restores_every_owner_and_keeps_maintenance_for_retry(monkeypatch):
    example = RemovalExample()
    core = example.core
    try:
        before = example.snapshot()
        core.maintain_engagement(example.command("prepare"))
        original = core.response_coordinator.publication_hook

        def reject_removed(state):
            if example.ids.get("engagement_a") not in state.get("repository", {}).get("artifacts", {}):
                raise RuntimeError("injected removal publication failure")
            original(state)

        monkeypatch.setattr(core.response_coordinator, "publication_hook", reject_removed)
        with raises(MutationCoordinationError, match="was rolled back") as failure:
            core.maintain_engagement(example.command("purge"))
        assert failure.value.live_state_changed is False
        assert example.snapshot() == before
        assert core.status().get("state") == "maintenance"
        with raises(LifecycleError):
            core.learn_response("Late", "Late private result", "late")
        monkeypatch.setattr(core.response_coordinator, "publication_hook", original)
        assert core.maintain_engagement(example.command("purge")).get("removed_count") == 3
        core.maintain_engagement(example.command("resume"))
    finally:
        core.close()


def test_paused_owner_can_recover_original_dependency_binding_after_local_purge():
    example = RemovalExample()
    with example.core as core:
        core.maintain_engagement(example.command("prepare"))
        core.maintain_engagement(example.command("purge"))
        before = example.snapshot()
        inspection = core.maintain_engagement({**example.command("plan"), "dependency_ids": []})
        expected = {key: value for key, value in example.command("purge").items() if key != "action"}
        assert inspection.get("maintenance_binding") == expected
        assert inspection.get("purged") is True
        assert example.snapshot() == before
        for fields in (
            {"operation_id": "foreign"},
            {"visibility_scope": SCOPES.get("engagement_b")},
            {"dependency_ids": ["foreign"]},
        ):
            with raises(LifecycleError, match="conflicts"):
                core.maintain_engagement({**example.command("plan"), **fields})
        with raises(LifecycleError):
            core.maintain_engagement({**example.command("resume"), "dependency_ids": []})
        core.maintain_engagement({**inspection.get("maintenance_binding", {}), "action": "resume"})


def test_scope_removal_finds_evicted_artifacts_after_their_receipts_are_pruned():
    engine = Engram()
    engine.config["capacity"] = 1
    engine.mutation_receipts = MutationReceiptLedger(max_receipts=1, max_tombstones=10)
    with EngramCore(engine) as core:
        scope = SCOPES.get("engagement_a", {})
        private = core.learn_response("Private marker?", "Private marker.", "private-evicted", metadata={"visibility_scope": scope})
        public = core.learn_response(
            "Public marker?", "Public marker.", "public-retained", metadata={"visibility_scope": SCOPES.get("public")}
        )
        private_id, public_id = private.get("statement_id"), public.get("statement_id")
        assert set(core.engram.response_repository.snapshot().get("artifacts", {})) == {public_id}
        core.propose("Public marker?", "unrelated-public-query", user_id="0")
        before = core.response_coordinator.snapshot()
        ledger = before.get("mutation_receipts", {})
        assert any(
            private_id in core.response_removal.receipt_statement_ids(tombstone) for tombstone in ledger.get("tombstones", [])
        )
        command = {"operation_id": "evicted-scope-removal", "visibility_scope": scope, "dependency_ids": []}
        plan = core.maintain_engagement({**command, "action": "plan"})
        assert plan.get("selected_statement_ids") == [private_id]
        core.maintain_engagement({**command, "action": "prepare"})
        result = core.maintain_engagement({**command, "action": "purge"})
        assert result.get("removed_count") == 0
        after = core.response_coordinator.snapshot()
        assert after.get("repository") == before.get("repository")
        retained_ledger = after.get("mutation_receipts", {})
        assert retained_ledger.get("receipts") == ledger.get("receipts")
        assert len(retained_ledger.get("tombstones", [])) < len(ledger.get("tombstones", []))
        assert all(
            private_id not in core.response_removal.receipt_statement_ids(receipt)
            for receipt in (*retained_ledger.get("receipts", []), *retained_ledger.get("tombstones", []))
        )
        core.maintain_engagement({**command, "action": "resume"})


def test_purge_removes_retired_response_body_and_its_private_lifecycle_audit():
    example = RemovalExample()
    core = example.core
    try:
        private_id = example.ids.get("engagement_a", "")
        core.retire_response(private_id, "private lifecycle detail", "retired-private-audit")
        assert core.engram.response_repository.get_artifact(private_id).get("metadata", {}).get("lifecycle_audit")
        core.maintain_engagement(example.command("prepare"))
        assert core.maintain_engagement(example.command("purge")).get("removed_count") == 3
        response = core.response_coordinator.snapshot()
        assert private_id not in response.get("repository", {}).get("artifacts", {})
        assert all(
            receipt.get("request_id") != "retired-private-audit"
            for receipt in response.get("mutation_receipts", {}).get("receipts", [])
        )
        core.maintain_engagement(example.command("resume"))
    finally:
        core.close()


@mark.parametrize("kind", ["resolution", "graph"])
def test_prepare_drains_active_work_and_rejects_late_learning_and_other_owners(kind):
    example = RemovalExample()
    core = example.core
    entered, release = Event(), Event()

    def active_work():
        slot = core.resolution_slot("held-request", "0") if kind == "resolution" else core.graph_operation()
        with slot:
            entered.set()
            assert release.wait(5)

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            active = executor.submit(active_work)
            assert entered.wait(5)
            prepare = executor.submit(core.maintain_engagement, example.command("prepare"))
            try:
                with core.resolution_condition:
                    assert core.resolution_condition.wait_for(lambda: core.internal_state == CoreState.QUIESCING, timeout=5)
                assert not prepare.done()
                with raises(LifecycleError):
                    core.learn_response("late", "private late result", "late-request")
                with raises(LifecycleError):
                    core.maintain_engagement({**example.command("prepare"), "operation_id": "different-owner"})
            finally:
                release.set()
            active.result(timeout=5)
            assert prepare.result(timeout=5).get("drained") is True
        with raises(LifecycleError):
            core.maintain_engagement({**example.command("purge"), "dependency_ids": ["substituted-id"]})
        core.maintain_engagement(example.command("purge"))
        core.maintain_engagement(example.command("resume"))
    finally:
        release.set()
        core.close()


@mark.parametrize(
    "damage",
    [
        {"action": "wipe"},
        {"action": []},
        {"operation_id": ""},
        {"visibility_scope": SCOPES.get("public")},
        {"visibility_scope": {}},
        {"dependency_ids": "all"},
        {"dependency_ids": [None]},
        {"extra": True},
    ],
)
def test_invalid_command_never_changes_core_or_owned_state(damage):
    example = RemovalExample()
    try:
        before = example.snapshot()
        with raises(InvalidRequestError):
            example.core.maintain_engagement({**example.command("purge"), **damage})
        assert example.core.status().get("state") == "running"
        assert example.snapshot() == before
    finally:
        example.core.close()
