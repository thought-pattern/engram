"""Section 15 security and privacy boundary tests."""

import logging

import pytest

from engram.constants import (
    MAX_ARTIFACT_ID_BYTES,
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
)
from engram.core import Engram
from engram.errors import InvalidRequestError
from engram.graph import MemGraphConnection
from engram.mcp_server import MCPConversationService
from engram.service import EngramCore, service_request_signature


def test_shared_service_rejects_oversized_request_and_identity_fields_before_state_change() -> None:
    core = EngramCore()

    with pytest.raises(InvalidRequestError, match="request exceeds"):
        core.resolve_request("x" * (MAX_REQUEST_BYTES + 1), "bounded-request")
    with pytest.raises(InvalidRequestError, match="request_id exceeds"):
        core.resolve_request("bounded request", "r" * (MAX_REQUEST_ID_BYTES + 1))
    with pytest.raises(InvalidRequestError, match="namespace exceeds"):
        core.propose("bounded request", "bounded-proposal", namespace="n" * (MAX_NAMESPACE_BYTES + 1))
    with pytest.raises(InvalidRequestError, match="context_fingerprint exceeds"):
        core.propose(
            "bounded request",
            "bounded-context",
            context_fingerprint="c" * (MAX_CONTEXT_FINGERPRINT_BYTES + 1),
        )
    with pytest.raises(InvalidRequestError, match="required_source_label exceeds"):
        core.propose(
            "bounded request",
            "bounded-source",
            required_source_label="s" * (MAX_SOURCE_LABEL_BYTES + 1),
        )

    assert core._resolution_requests == {}
    assert core.proposals == {}
    core.close(flush=False)


def test_shared_service_rejects_oversized_mutation_and_predicate_fields() -> None:
    core = EngramCore()

    with pytest.raises(InvalidRequestError, match="response exceeds"):
        core.learn_response("request", "x" * (MAX_RESPONSE_BYTES + 1), "bounded-response")
    with pytest.raises(InvalidRequestError, match="source_label exceeds"):
        core.add_fact("bounded fact", source_label="s" * (MAX_SOURCE_LABEL_BYTES + 1))
    with pytest.raises(InvalidRequestError, match="statement_id exceeds"):
        core.retire_response(
            "s" * (MAX_ARTIFACT_ID_BYTES + 1),
            "administrative",
            "bounded-retirement",
        )
    with pytest.raises(InvalidRequestError, match="reason exceeds"):
        core.retire_response("statement", "r" * (MAX_FEEDBACK_REASON_BYTES + 1), "bounded-reason")
    with pytest.raises(InvalidRequestError, match="name exceeds"):
        core.set_predicate("Sarah", "n" * (MAX_METADATA_KEY_BYTES + 1), "value")
    with pytest.raises(InvalidRequestError, match="value exceeds"):
        core.set_predicate("Sarah", "preference", "v" * (MAX_METADATA_STRING_BYTES + 1))

    assert core.engram.statements == []
    assert core.engram.sessions == {}
    core.close(flush=False)


def test_legacy_conversation_and_mcp_share_the_request_bound() -> None:
    service = MCPConversationService()
    service.start(user_id="Sarah", seed_path="")

    with pytest.raises(InvalidRequestError, match="text exceeds"):
        service.send("x" * (MAX_REQUEST_BYTES + 1))

    assert service.inspect()["turn_count"] == 0
    service.stop()


def test_initial_context_and_service_signatures_are_bounded() -> None:
    core = EngramCore()

    with pytest.raises(InvalidRequestError, match="initial_bot_text exceeds"):
        core.start_conversation(initial_bot_text="x" * (MAX_RESPONSE_BYTES + 1))
    with pytest.raises(InvalidRequestError, match="service request exceeds"):
        service_request_signature(value="x" * MAX_SIGNATURE_INPUT_BYTES)
    with pytest.raises(InvalidRequestError, match="must contain valid Unicode"):
        service_request_signature(metadata={"nested": "\ud800"})

    assert core.conversations == {}
    core.close(flush=False)


def test_core_graph_failure_log_omits_exception_content(caplog) -> None:
    secret = "private-request-and-proposition-content"

    class FailingGraph:
        available = True

        def execute_read(self, _query, _parameters=()):
            raise RuntimeError(secret)

    engine = Engram()
    engine._graph_client = FailingGraph()

    with caplog.at_level(logging.DEBUG, logger="engram.core"):
        assert engine.graph_query("RETURN 1") == []

    assert "RuntimeError" in caplog.text
    assert secret not in caplog.text


def test_memgraph_query_failure_log_and_wrapper_omit_exception_content(caplog) -> None:
    secret = "private-graph-driver-content"

    class FailingCursor:
        description = ()

        def execute(self, _query, _parameters):
            raise RuntimeError(secret)

    class FailingConnection:
        def cursor(self):
            return FailingCursor()

    client = MemGraphConnection()
    client.conn = FailingConnection()
    client.available = True

    with (
        caplog.at_level(logging.ERROR, logger="engram.graph"),
        pytest.raises(RuntimeError, match=r"Query failed \(RuntimeError\)") as failure,
    ):
        client.execute("RETURN 1")

    assert secret not in str(failure.value)
    assert secret not in caplog.text
    assert "RuntimeError" in caplog.text
