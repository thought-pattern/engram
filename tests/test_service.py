"""Transport-neutral facade tests shared by CLI, MCP, and future adapters."""

import threading
from concurrent.futures import ThreadPoolExecutor
from typing import cast

import pytest

from engram import service as service_module
from engram.config import engram_config
from engram.constants import RESOLUTION_RESULT_FIELDS, Tier
from engram.core import Engram
from engram.errors import (
    ConflictError,
    InvalidRequestError,
    LifecycleError,
    PersistenceError,
    ResolutionCancelledError,
    ResourceNotFoundError,
)
from engram.identity import build_standalone_identity, scope_key
from engram.service import EngramCore, open_engram_core


def test_resolution_reuses_the_plan_built_for_negative_lookup(monkeypatch) -> None:
    core = EngramCore()
    original = core._resolver_registry.plan
    calls = 0

    def plan(frame, configured_names=()):
        nonlocal calls
        calls += 1
        return original(frame, configured_names)

    monkeypatch.setattr(core._resolver_registry, "plan", plan)

    result = core.resolve_request("unmatched request", "single-plan", configured_resolvers=("exact",))

    assert result["outcome"].value == "MISS"
    assert calls == 1
    core.close(flush=False)


def test_optional_graph_execution_does_not_hold_the_core_lock() -> None:
    engine = Engram()
    entered = threading.Event()
    release = threading.Event()

    class BlockingGraph:
        available = True

        def structured_claim_projections(self, _value, *, projection_id, limit):
            entered.set()
            assert release.wait(timeout=5)
            return []

    engine._graph_client = BlockingGraph()
    core = EngramCore(engine)

    with ThreadPoolExecutor(max_workers=5) as executor:
        graph_future = executor.submit(
            core.resolve_request,
            "Ada",
            "blocking-graph",
            user_id="graph-user",
            configured_resolvers=("structured_graph",),
        )
        assert entered.wait(timeout=5)

        status_future = executor.submit(core.status)
        local_future = executor.submit(
            core.resolve_request,
            "ordinary local request",
            "local-during-graph",
            user_id="local-user",
            configured_resolvers=("exact",),
        )
        same_user_future = executor.submit(
            core.resolve_request,
            "same user local request",
            "same-user-during-graph",
            user_id="graph-user",
            configured_resolvers=("exact",),
        )
        conflicting_retry_future = executor.submit(
            core.resolve_request,
            "different input for the same request ID",
            "blocking-graph",
            user_id="other-user",
            configured_resolvers=("exact",),
        )
        try:
            status = status_future.result(timeout=1)
            local = local_future.result(timeout=1)
            assert same_user_future.done() is False
            assert conflicting_retry_future.done() is False
        finally:
            release.set()

        graph = graph_future.result(timeout=5)
        same_user = same_user_future.result(timeout=5)
        with pytest.raises(ConflictError, match="different input"):
            conflicting_retry_future.result(timeout=5)

    assert status["ready"] is True
    assert local["outcome"].value == "MISS"
    assert graph["outcome"].value == "MISS"
    assert same_user["outcome"].value == "MISS"
    assert graph_future.done() is True


def test_legacy_chat_graph_execution_does_not_hold_the_core_lock() -> None:
    engine = Engram()
    entered = threading.Event()
    release = threading.Event()

    class BlockingGraph:
        available = True

        def execute_read(self, _query, _parameters=()):
            entered.set()
            assert release.wait(timeout=5)
            return []

    engine._graph_client = BlockingGraph()
    engine.pattern_matcher.clear()
    core = EngramCore(engine)
    core.start_conversation(user_id="graph-user")

    with ThreadPoolExecutor(max_workers=4) as executor:
        graph_future = executor.submit(core.chat, "graph-user", "What do you know about Ada Lovelace?")
        assert entered.wait(timeout=20)

        status_future = executor.submit(core.status)
        local_future = executor.submit(
            core.resolve_request,
            "ordinary local request",
            "local-during-legacy-graph",
            user_id="local-user",
            configured_resolvers=("exact",),
        )
        same_user_future = executor.submit(
            core.resolve_request,
            "same user local request",
            "same-user-during-legacy-graph",
            user_id="graph-user",
            configured_resolvers=("exact",),
        )
        try:
            status = status_future.result(timeout=1)
            local = local_future.result(timeout=1)
            assert same_user_future.done() is False
        finally:
            release.set()

        graph = graph_future.result(timeout=5)
        same_user = same_user_future.result(timeout=5)

    assert status["ready"] is True
    assert local["outcome"].value == "MISS"
    assert graph["input"] == "What do you know about Ada Lovelace?"
    assert same_user["outcome"].value == "MISS"


def test_unified_resolution_cancellation_is_transient_and_not_cached() -> None:
    core = EngramCore()

    def cancel() -> None:
        raise ResolutionCancelledError("caller cancelled resolution")

    with pytest.raises(ResolutionCancelledError, match="caller cancelled"):
        core.resolve_request("What is Engram?", "resolution-cancelled", cancellation_check=cancel)

    assert "resolution-cancelled" not in core._resolution_requests
    retry = core.resolve_request("What is Engram?", "resolution-cancelled", configured_resolvers=("exact",))
    assert retry["outcome"].value == "MISS"


def test_core_shares_knowledge_while_isolating_user_context() -> None:
    engram = Engram()
    engram.store("Go on.", pattern="*", tier=Tier.STATIC)
    core = EngramCore(engram)
    core.start_conversation("Alice")
    core.start_conversation("Carol")

    core.chat("Alice", "Sushi is good.")
    alice_context = core.inspect_conversation("Alice")["session"]
    carol_turn = core.chat("Carol", "What's good?")

    assert carol_turn["response"] == "Sushi is good."
    assert core.inspect_conversation("Alice")["session"] == alice_context
    assert core.inspect_conversation("Carol")["session"]["previous_response"] == "Sushi is good."
    assert core.inspect_conversation("Carol")["core_status"]["state"] == "running"


def test_stopping_one_conversation_leaves_other_users_active() -> None:
    core = EngramCore()
    core.start_conversation("Alice")
    core.start_conversation("Carol")

    stopped = core.stop_conversation("Alice")

    assert stopped["user_id"] == "Alice"
    with pytest.raises(ValueError, match="Alice"):
        core.get_conversation("Alice")
    assert core.chat("Carol", "Hello")["user_id"] == "Carol"


def test_regulated_cache_does_not_require_a_chat_conversation() -> None:
    core = EngramCore()
    learned = core.learn_response(
        "When are you open?",
        "Support is open from nine to five.",
        "learn-1",
        user_id="Alice",
        namespace="support",
    )
    proposal = core.propose(
        "When are you open?",
        "proposal-1",
        user_id="Carol",
        namespace="support",
    )

    resolved = core.resolve(
        proposal["proposal_id"],
        "accepted",
        statement_id=learned["statement_id"],
    )

    assert resolved["resolved"] is True
    assert core.engram.sessions["Carol"]["previous_response"] == "Support is open from nine to five."


@pytest.mark.parametrize("invalid", [[], (), "", 0, False])
def test_regulated_mapping_arguments_reject_falsey_non_objects(invalid) -> None:
    core = EngramCore()

    with pytest.raises(InvalidRequestError, match="required_metadata must be an object"):
        core.propose("What is cached?", "proposal-invalid-metadata", required_metadata=invalid)
    with pytest.raises(InvalidRequestError, match="metadata must be an object"):
        core.learn_response("What is cached?", "A cached answer.", "learn-invalid-metadata", metadata=invalid)


def test_regulated_mapping_arguments_copy_concrete_empty_objects() -> None:
    core = EngramCore()

    learned = core.learn_response("What is cached?", "A cached answer.", "learn-empty-metadata", metadata={})
    proposal = core.propose("What is cached?", "proposal-empty-metadata", required_metadata={})

    assert learned["action"] == "created"
    assert proposal["candidates"][0]["statement_id"] == learned["statement_id"]


@pytest.mark.parametrize("field", ["identity", "budget"])
@pytest.mark.parametrize("invalid", [[], (), "", 0, False])
def test_unified_python_api_rejects_falsey_non_mapping_absence(field, invalid) -> None:
    core = EngramCore()
    arguments = {field: invalid}

    with pytest.raises(InvalidRequestError, match=f"{field} must be an object"):
        core.resolve_request("What is Engram?", f"invalid-{field}-{type(invalid).__name__}", **arguments)


def test_unified_python_api_accepts_mapping_absence_and_authoritative_identity() -> None:
    core = EngramCore()
    scope = scope_key("support", "python-api-v1")
    identity = build_standalone_identity("When did Engram launch?", scope)

    result = core.resolve_request(
        "When did Engram launch?",
        "python-api-authoritative",
        namespace="support",
        context_fingerprint="python-api-v1",
        identity=identity,
        budget={},
        configured_resolvers=("exact",),
    )

    assert type(result) is dict
    assert set(result) == set(RESOLUTION_RESULT_FIELDS)
    assert result["schema_version"] == 1
    assert result["selected_candidate_available"] is False
    assert result["evidence_package_available"] is False
    assert result["evidence_package"]["records"] == ()


def test_unified_python_api_candidate_feedback_uses_keyed_records() -> None:
    core = EngramCore(checkpoint_on_mutation=False)
    learned = core.learn_response(
        "What is Engram?",
        "Engram is a bounded retrieval system.",
        "python-api-learn",
        namespace="support",
    )
    result = core.resolve_request(
        "What is Engram?",
        "python-api-resolve",
        namespace="support",
        configured_resolvers=("exact",),
    )

    candidate = result["response_candidates"][0]
    feedback = core.record_resolution_feedback(
        "python-api-resolve",
        "python-api-feedback",
        "accepted",
        candidate["statement_id"],
    )

    assert candidate["statement_id"] == learned["statement_id"]
    assert feedback["outcome"] == "accepted"
    assert feedback["statement_id"] == learned["statement_id"]


def test_learn_response_is_dynamic_active_artifact_wrapper_with_user_context() -> None:
    core = EngramCore(checkpoint_on_mutation=False)

    learned = core.learn_response(
        "What is cached?",
        "Exact café response ☕.",
        "learn-artifact",
        user_id="Alice",
        namespace="support",
        context_fingerprint="tier:pro",
        source_label="actor:test",
        metadata={"actor_version": "actor-7"},
    )

    artifact = core.engram.response_repository.get_artifact(learned["statement_id"])
    assert artifact["response"] == "Exact café response ☕."
    assert artifact["tier"] == Tier.DYNAMIC
    assert artifact["lifecycle"].value == "ACTIVE"
    assert artifact["generation"] == 1
    assert artifact["scope"]["namespace"] == "support"
    assert artifact["scope"]["context_fingerprint"] == "tier:pro"
    assert artifact["provenance"]["caller_id"] == "Alice"
    assert artifact["provenance"]["source_label"] == "actor:test"
    assert artifact["metadata"] == {"actor_version": "actor-7"}
    assert core.engram.sessions["Alice"]["previous_response"] == artifact["response"]
    assert core.engram.response_repository.check()["consistent"] is True


def test_learn_response_exact_retry_survives_restart_without_second_checkpoint(tmp_path) -> None:
    store = tmp_path / "engram.json"
    first = EngramCore(Engram(), store_path=store)
    created = first.learn_response("What persists?", "Persistent exact response.", "learn-restart", user_id="Alice")
    durable_before = store.read_bytes()

    restored = open_engram_core(store_path=store)
    replay = restored.learn_response("What persists?", "Persistent exact response.", "learn-restart", user_id="Alice")

    assert replay["statement_id"] == created["statement_id"]
    assert replay["idempotent"] is True
    assert store.read_bytes() == durable_before
    assert restored.status()["last_checkpoint_at"] == ""
    assert restored.engram.sessions["Alice"]["previous_response"] == "Persistent exact response."


def test_learn_response_new_request_cannot_implicitly_replace_owned_identity() -> None:
    core = EngramCore(checkpoint_on_mutation=False)
    original = core.learn_response("What is current?", "Original exact response.", "learn-original")

    with pytest.raises(ConflictError, match=original["statement_id"]):
        core.learn_response("What is current?", "Implicit replacement.", "learn-replacement")

    artifact = core.engram.response_repository.get_artifact(original["statement_id"])
    assert artifact["response"] == "Original exact response."
    assert artifact["lifecycle"].value == "ACTIVE"
    assert artifact["generation"] == 1


def test_proposal_and_resolution_accounting_remain_artifact_view_equivalent() -> None:
    core = EngramCore(checkpoint_on_mutation=False)
    learned = core.learn_response("What is counted?", "Counted response.", "learn-counted")
    epoch_after_commit = core.engram.namespace_epochs.get("")["knowledge_epoch"]

    proposal = core.propose("What is counted?", "proposal-counted")
    core.resolve(proposal["proposal_id"], "accepted", learned["statement_id"])

    artifact = core.engram.response_repository.get_artifact(learned["statement_id"])
    compatibility = core.engram.get_statement(learned["statement_id"])
    assert artifact["generation"] == 3
    assert artifact["statistics"]["query_count"] == compatibility["query_count"] == 1
    assert artifact["statistics"]["hit_count"] == compatibility["hit_count"] == 1
    assert artifact["statistics"]["last_hit_available"] is True
    assert core.engram.namespace_epochs.get("")["knowledge_epoch"] == epoch_after_commit
    assert core.engram.response_repository.check()["consistent"] is True
    assert core.engram.mutation_receipts.next_sequence == 4


def test_proposal_accounting_derives_bounded_internal_receipt_identity() -> None:
    core = EngramCore(checkpoint_on_mutation=False)
    learned = core.learn_response("What has a bounded receipt?", "A bounded receipt.", "learn-bounded-receipt")
    external_request_id = "r" * 256

    proposal = core.propose("What has a bounded receipt?", external_request_id)
    core.resolve(proposal["proposal_id"], "accepted", learned["statement_id"])

    receipt_state = core.engram.mutation_receipts.snapshot()["receipts"]
    receipt_ids = [receipt["request_id"] for receipt in cast(list[dict[str, str]], receipt_state)]
    accounting_ids = [request_id for request_id in receipt_ids if request_id.startswith("internal:")]
    assert len(accounting_ids) == 2
    assert all(len(request_id.encode("utf-8")) <= 256 for request_id in accounting_ids)
    assert all(external_request_id not in request_id for request_id in accounting_ids)
    assert core.engram.response_repository.check()["consistent"] is True


def test_core_flush_restores_shared_state_but_not_transient_proposals(tmp_path) -> None:
    store = tmp_path / "engram.json"
    core = EngramCore(Engram(), store_path=store)
    learned = core.learn_response("What persists?", "The answer persists.", "learn-persist")
    proposal = core.propose("What persists?", "proposal-before-restart")
    assert store.exists()

    restored = open_engram_core(store_path=store)
    recalled = restored.propose("What persists?", "proposal-after-restart")

    assert recalled["candidates"][0]["statement_id"] == learned["statement_id"]
    with pytest.raises(ValueError, match="expired"):
        restored.resolve(proposal["proposal_id"], "accepted", statement_id=learned["statement_id"])


def test_core_open_restores_stored_config_unless_explicitly_overridden(tmp_path) -> None:
    store = tmp_path / "engram.json"
    stored_config = engram_config(capacity=37, use_synonyms=False)
    core = EngramCore(Engram(config=stored_config), store_path=store)
    assert core.flush() is True

    restored = open_engram_core(store_path=store)

    assert restored.engram.config["capacity"] == 37
    assert restored.engram.config["use_synonyms"] is False

    override = engram_config(capacity=41, use_synonyms=True)
    overridden = open_engram_core(config=override, store_path=store)

    assert overridden.engram.config["capacity"] == 41
    assert overridden.engram.config["use_synonyms"] is True


def test_core_without_store_reports_that_flush_was_skipped() -> None:
    assert EngramCore().flush() is False


@pytest.mark.parametrize("invalid", [[], (), "", 0, False])
def test_core_open_rejects_falsey_non_object_config(invalid) -> None:
    with pytest.raises(InvalidRequestError, match="config must be an object"):
        open_engram_core(config=invalid)


def test_core_checkpoints_each_durable_mutation(tmp_path) -> None:
    store = tmp_path / "engram.json"
    engram = Engram()
    engram.store("Hello!", pattern="HELLO", tier=Tier.STATIC)
    core = EngramCore(engram, store_path=store)

    core.start_conversation("Alice")
    assert "Alice" in open_engram_core(store_path=store).engram.sessions

    core.chat("Alice", "hello")
    assert open_engram_core(store_path=store).engram.sessions["Alice"]["previous_response"] == "Hello!"

    core.set_predicate("Alice", "mood", "curious")
    assert open_engram_core(store_path=store).engram.sessions["Alice"]["predicates"]["mood"] == "curious"

    fact = core.add_fact("Tokyo is the capital of Japan.", source_label="research")
    assert open_engram_core(store_path=store).engram.get_statement(fact["id"])["source_label"] == "research"

    learned = core.learn_response("What is cached?", "A cached answer.", "learn-checkpoint")
    proposal = core.propose("What is cached?", "proposal-checkpoint")
    proposed_state = open_engram_core(store_path=store).engram.get_statement(learned["statement_id"])
    assert proposed_state["query_count"] == 1

    core.resolve(proposal["proposal_id"], "accepted", statement_id=learned["statement_id"])
    accepted_state = open_engram_core(store_path=store).engram.get_statement(learned["statement_id"])
    assert accepted_state["hit_count"] == 1

    core.retire_response(learned["statement_id"], "superseded", "retire-checkpoint")
    restored_retired = open_engram_core(store_path=store).engram
    assert restored_retired.response_repository.get_artifact(learned["statement_id"])["lifecycle"].value == "RETIRED"


def test_context_manager_flushes_when_mutation_checkpointing_is_disabled(tmp_path) -> None:
    store = tmp_path / "engram.json"

    with EngramCore(Engram(), store_path=store, checkpoint_on_mutation=False) as core:
        fact = core.add_fact("A deferred fact.")
        assert not store.exists()

    assert open_engram_core(store_path=store).engram.get_statement(fact["id"])["text"] == "A deferred fact."


def test_core_exposes_stable_request_and_resource_errors() -> None:
    core = EngramCore()

    with pytest.raises(ResourceNotFoundError, match="Alice"):
        core.chat("Alice", "hello")
    with pytest.raises(InvalidRequestError, match="limit"):
        core.propose("question", "bad-limit", limit=0)
    with pytest.raises(ResourceNotFoundError, match="proposal"):
        core.resolve("missing", "accepted", statement_id="missing")

    core.start_conversation("Alice")
    with pytest.raises(ConflictError, match="already active"):
        core.start_conversation("Alice")


def test_close_is_idempotent_and_blocks_subsequent_operations() -> None:
    core = EngramCore()
    core.start_conversation("Alice")

    assert core.status()["state"] == "running"
    assert core.status()["ready"] is True
    assert core.close() is True
    assert core.close() is False

    status = core.status()
    assert status["state"] == "closed"
    assert status["ready"] is False
    assert status["healthy"] is False
    with pytest.raises(LifecycleError, match="closed"):
        core.start_conversation("Carol")
    with pytest.raises(LifecycleError, match="closed"):
        core.add_fact("A fact after closure.")
    with pytest.raises(LifecycleError, match="closed"):
        core.flush()


def test_close_waits_for_an_active_core_operation() -> None:
    engram = Engram()
    engram.store("Hello!", pattern="HELLO", tier=Tier.STATIC)
    core = EngramCore(engram)
    core.start_conversation("Alice")
    runtime = core.get_conversation("Alice")
    original_send = runtime.send
    entered = threading.Event()
    release = threading.Event()

    def delayed_send(text: str) -> dict:
        entered.set()
        assert release.wait(timeout=5)
        result = original_send(text)
        return result

    runtime.send = delayed_send
    with ThreadPoolExecutor(max_workers=2) as executor:
        chat_future = executor.submit(core.chat, "Alice", "hello")
        assert entered.wait(timeout=5)
        close_future = executor.submit(core.close, flush=False)
        assert close_future.done() is False
        release.set()
        assert chat_future.result(timeout=5)["response"] == "Hello!"
        assert close_future.result(timeout=5) is True

    assert core.status()["state"] == "closed"


def test_checkpoint_failure_reports_degraded_state_and_recovers(tmp_path, monkeypatch) -> None:
    store = tmp_path / "engram.json"
    core = EngramCore(Engram(), store_path=store)
    real_save = service_module.persistence.save_response_state

    def fail_save(engram, state, path) -> None:
        raise OSError("disk unavailable")

    monkeypatch.setattr(service_module.persistence, "save_response_state", fail_save)
    with pytest.raises(PersistenceError) as failure:
        core.learn_response("What is cached?", "A durable answer.", "learn-degraded")

    assert failure.value.state_changed is False
    assert failure.value.operation == "store checkpoint"
    degraded = core.status()
    assert degraded["state"] == "running"
    assert degraded["ready"] is True
    assert degraded["healthy"] is False
    assert degraded["durability"] == "degraded"
    assert degraded["dirty"] is False
    assert degraded["last_persistence_error"] == "OSError"

    monkeypatch.setattr(service_module.persistence, "save_response_state", real_save)
    retry = core.learn_response("What is cached?", "A durable answer.", "learn-degraded")

    assert retry["idempotent"] is False
    recovered = core.status()
    assert recovered["healthy"] is True
    assert recovered["durability"] == "healthy"
    assert recovered["dirty"] is False
    assert recovered["last_checkpoint_at"]
    assert recovered["last_persistence_error"] == ""
    assert open_engram_core(store_path=store).engram.get_statement(retry["statement_id"])["text"] == "A durable answer."


def test_failed_close_returns_core_to_running_for_flush_recovery(tmp_path, monkeypatch) -> None:
    store = tmp_path / "engram.json"
    core = EngramCore(Engram(), store_path=store, checkpoint_on_mutation=False)
    core.add_fact("Pending state.")
    real_save = service_module.persistence.save

    def fail_save(engram, path) -> None:
        raise OSError("read only")

    monkeypatch.setattr(service_module.persistence, "save", fail_save)
    with pytest.raises(PersistenceError) as failure:
        core.close()

    assert failure.value.state_changed is True
    assert core.status()["state"] == "running"
    assert core.status()["durability"] == "degraded"

    monkeypatch.setattr(service_module.persistence, "save", real_save)
    assert core.close() is True
    assert open_engram_core(store_path=store).engram.statements[0]["text"] == "Pending state."
