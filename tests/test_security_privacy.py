"""Section 15 security and privacy boundary tests."""

from logging import DEBUG, ERROR, WARNING

from pytest import raises as pytest_raises

from engram.constants import (
    MAX_ARTIFACT_ID_BYTES,
    MAX_CACHE_REQUEST_BYTES,
    MAX_CONTEXT_FINGERPRINT_BYTES,
    MAX_FEEDBACK_REASON_BYTES,
    MAX_METADATA_KEY_BYTES,
    MAX_METADATA_STRING_BYTES,
    MAX_NAMESPACE_BYTES,
    MAX_REQUEST_BYTES,
    MAX_REQUEST_ID_BYTES,
    MAX_RESPONSE_BYTES,
    MAX_SIGNATURE_INPUT_BYTES,
    MAX_SOURCE_LABEL_BYTES,
    ResolutionOutcome,
    ResolverState,
)
from engram.core import Engram
from engram.errors import InvalidRequestError
from engram.graph import MemGraphConnection
from engram.mcp_server import MCPConversationService
from engram.service import EngramCore, service_request_signature

from .bolt_stub import BoltStub


def test_shared_service_rejects_oversized_request_and_identity_fields_before_state_change() -> None:
    core = EngramCore()

    with pytest_raises(InvalidRequestError, match="request exceeds"):
        core.resolve_request("x" * (MAX_CACHE_REQUEST_BYTES + 1), "bounded-request")
    with pytest_raises(InvalidRequestError, match="request_id exceeds"):
        core.resolve_request("bounded request", "r" * (MAX_REQUEST_ID_BYTES + 1))
    with pytest_raises(InvalidRequestError, match="namespace exceeds"):
        core.propose("bounded request", "bounded-proposal", namespace="n" * (MAX_NAMESPACE_BYTES + 1))
    with pytest_raises(InvalidRequestError, match="context_fingerprint exceeds"):
        core.propose(
            "bounded request",
            "bounded-context",
            context_fingerprint="c" * (MAX_CONTEXT_FINGERPRINT_BYTES + 1),
        )
    with pytest_raises(InvalidRequestError, match="required_source_label exceeds"):
        core.propose(
            "bounded request",
            "bounded-source",
            required_source_label="s" * (MAX_SOURCE_LABEL_BYTES + 1),
        )

    assert core.resolution_requests == {}
    assert core.proposals == {}
    core.close()


def test_shared_service_rejects_oversized_mutation_and_predicate_fields() -> None:
    core = EngramCore()

    with pytest_raises(InvalidRequestError, match="response exceeds"):
        core.learn_response("request", "x" * (MAX_RESPONSE_BYTES + 1), "bounded-response")
    with pytest_raises(InvalidRequestError, match="source_label exceeds"):
        core.add_fact("bounded fact", source_label="s" * (MAX_SOURCE_LABEL_BYTES + 1))
    with pytest_raises(InvalidRequestError, match="statement_id exceeds"):
        core.retire_response(
            "s" * (MAX_ARTIFACT_ID_BYTES + 1),
            "administrative",
            "bounded-retirement",
        )
    with pytest_raises(InvalidRequestError, match="reason exceeds"):
        core.retire_response("statement", "r" * (MAX_FEEDBACK_REASON_BYTES + 1), "bounded-reason")
    with pytest_raises(InvalidRequestError, match="name exceeds"):
        core.set_predicate("Sarah", "n" * (MAX_METADATA_KEY_BYTES + 1), "value")
    with pytest_raises(InvalidRequestError, match="value exceeds"):
        core.set_predicate("Sarah", "preference", "v" * (MAX_METADATA_STRING_BYTES + 1))

    assert core.engram.statements == []
    assert core.engram.sessions == {}
    core.close()


def test_conversation_and_mcp_share_the_request_bound() -> None:
    service = MCPConversationService()
    service.start(user_id="Sarah")

    with pytest_raises(InvalidRequestError, match="text exceeds"):
        service.send("x" * (MAX_REQUEST_BYTES + 1))

    assert service.inspect()["turn_count"] == 0
    service.stop()


def test_initial_context_and_service_signatures_are_bounded() -> None:
    core = EngramCore()

    with pytest_raises(InvalidRequestError, match="initial_bot_text exceeds"):
        core.start_conversation(initial_bot_text="x" * (MAX_RESPONSE_BYTES + 1))
    with pytest_raises(InvalidRequestError, match="service request exceeds"):
        service_request_signature(value="x" * MAX_SIGNATURE_INPUT_BYTES)
    with pytest_raises(InvalidRequestError, match="must contain valid Unicode"):
        service_request_signature(metadata={"nested": "\ud800"})

    assert core.conversations == {}
    core.close()


def test_core_graph_failure_is_logged_in_full_and_returns_no_rows(caplog) -> None:
    secret = "private-request-and-proposition-content"

    class FailingGraph:
        available = True

        def execute(self, internal_query, internal_parameters=()):
            del internal_query, internal_parameters
            raise RuntimeError(secret)

    engine = Engram()
    engine.internal_graph_client = FailingGraph()

    with caplog.at_level(DEBUG, logger="engram.core"):
        assert engine.graph_query("RETURN 1") == []

    assert "RuntimeError" in caplog.text
    assert secret in caplog.text


def test_graph_query_failure_is_logged_in_full_but_the_wrapper_carries_only_its_type(caplog) -> None:
    secret = "private-graph-driver-content"
    stub = BoltStub()
    stub.mode = "fail"
    stub.failure_message = secret
    client = MemGraphConnection(host="127.0.0.1", port=stub.port)

    try:
        assert client.connect()
        with (
            caplog.at_level(ERROR, logger="engram.graph"),
            pytest_raises(RuntimeError, match=r"Query failed \(DatabaseError\)") as failure,
        ):
            client.execute("RETURN 1")
        # A failed query arrives over a healthy connection, so reads stay available.
        assert client.available is True
        assert client.reconnect_needed is False
    finally:
        client.disconnect()
        stub.close()

    assert secret in str(failure.value.__cause__)
    assert secret not in str(failure.value)
    assert secret in caplog.text
    assert "DatabaseError" in caplog.text


def test_database_failures_stay_out_of_user_results_and_are_logged(caplog) -> None:
    secret = "private-database-outage-detail"

    class FailingGraph:
        available = True

        def execute(self, internal_query, internal_parameters=()):
            del internal_query, internal_parameters
            raise RuntimeError(secret)

        def structured_proposition_projections(self, *internal_args, **internal_kwargs):
            del internal_args, internal_kwargs
            raise RuntimeError(secret)

        def canonical_entity_matches(self, *internal_args, **internal_kwargs):
            del internal_args, internal_kwargs
            raise RuntimeError(secret)

        def canonical_predicate_matches(self, *internal_args, **internal_kwargs):
            del internal_args, internal_kwargs
            raise RuntimeError(secret)

        def relation_one_hop_proposition_projections(self, *internal_args, **internal_kwargs):
            del internal_args, internal_kwargs
            raise RuntimeError(secret)

        def proposition_projection_by_id(self, *internal_args, **internal_kwargs):
            del internal_args, internal_kwargs
            raise RuntimeError(secret)

    engine = Engram()
    engine.internal_graph_client = FailingGraph()
    engine.config["graph"]["enabled"] = True
    core = EngramCore(engine)
    engine = core.engram

    with caplog.at_level(WARNING, logger="engram.core"):
        assert engine.canonical_entity_matches("France") == []
        assert engine.canonical_predicate_matches("capital") == []
        assert engine.relation_one_hop_proposition_projections("entity:france", "predicate:capital") == []
        assert engine.current_proposition_projection("proposition:paris") == ()
        assert engine.structured_proposition_projections("France") == []
        resolved = core.resolve_request("What is the capital of France?", "database-failure")
        started = core.start_conversation(user_id="alice")
        turn = core.chat(started["user_id"], "What is the capital of France?")

    visible = f"{resolved}{turn}"
    assert secret not in visible
    assert secret in caplog.text
    assert "Graph read failed" in caplog.text
    assert "RuntimeError" in caplog.text
    assert resolved["outcome"] == ResolutionOutcome.MISS
    assert all(item["state"] != ResolverState.FAILED for item in resolved["resolver_results"])
    assert turn["response"] == ""
    core.close()
