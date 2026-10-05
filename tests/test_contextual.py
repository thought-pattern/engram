"""Section 8 compact query-frame and bounded follow-up tests."""

from datetime import timedelta

from pytest import mark as pytest_mark, raises as pytest_raises

from engram import sessions
from engram.constants import EARLIEST_UTC, ExpectedObjectType, QualifierKind, QueryOperator
from engram.contextual import (
    classify_query_frame_operator,
    compact_query_frame,
    compact_query_frame_from_dict,
    compact_query_frame_to_dict,
    infer_expected_object_type,
)
from engram.core import Engram
from engram.errors import InvalidRequestError
from engram.identity import entity_reference, identity_qualifier, relation_reference
from engram.service import EngramCore

COMPACT_FRAME_VALUES = {
    "operator": QueryOperator.WHEN,
    "subjects": (entity_reference("Ada Lovelace", "entity:ada-lovelace"),),
    "relation": relation_reference("born", "predicate:date-of-birth"),
    "expected_object_type": ExpectedObjectType.DATE,
    "qualifiers": (),
    "source_turn": 3,
    "confidence": 0.9,
    "topic": "Ada Lovelace",
}


def test_compact_query_frame_codec_is_exact_bounded_and_deterministic() -> None:
    value = compact_query_frame(**COMPACT_FRAME_VALUES)
    encoded = compact_query_frame_to_dict(value)

    assert compact_query_frame_from_dict(encoded) == value
    assert encoded.get("operator", "") == "when"
    assert encoded.get("expected_object_type", "") == "DATE"
    subjects = encoded.get("subjects", [])
    assert subjects[0].get("canonical_id", "") == "entity:ada-lovelace"

    malformed = dict(encoded)
    malformed["extra"] = True
    with pytest_raises(InvalidRequestError, match="invalid fields"):
        compact_query_frame_from_dict(malformed)
    with pytest_raises(InvalidRequestError, match="subjects exceed"):
        compact_query_frame(
            **{**COMPACT_FRAME_VALUES, "subjects": tuple(entity_reference(f"Entity {index}") for index in range(5))}
        )


def test_resolve_request_keeps_contextual_frame_in_process_memory() -> None:
    core = EngramCore(Engram())

    core.resolve_request(
        "When was Ada Lovelace born?",
        "persisted-context-1",
        user_id="Sarah",
        configured_resolvers=("exact",),
    )

    session = core.engram.sessions.get("Sarah", {})
    previous_frame = session.get("previous_query_frame", {})
    assert session.get("query_frame_turn", 0) == 1
    assert previous_frame.get("operator", QueryOperator.UNKNOWN) == QueryOperator.WHEN
    assert previous_frame.get("subjects", ())[0].get("surface", "") == "Ada Lovelace"


@pytest_mark.parametrize(
    ("query_text", "expected"),
    [
        ("Who wrote Hamlet?", QueryOperator.WHO),
        ("What is PostgreSQL?", QueryOperator.WHAT),
        ("Where was Ada born?", QueryOperator.WHERE),
        ("When was Ada born?", QueryOperator.WHEN),
        ("Which version is current?", QueryOperator.WHICH),
        ("How many releases exist?", QueryOperator.HOW_MANY),
        ("Find PostgreSQL", QueryOperator.LOOKUP),
        ("Is there a current release?", QueryOperator.EXISTS),
        ("Count the releases", QueryOperator.COUNT),
        ("Compare PostgreSQL vs SQLite", QueryOperator.COMPARE),
        ("Explain PostgreSQL", QueryOperator.UNKNOWN),
    ],
)
def test_query_frame_operator_classification_reuses_closed_vocabulary(
    query_text: str,
    expected: QueryOperator,
) -> None:
    classification = classify_query_frame_operator(query_text)
    assert "operator" in classification
    assert classification.get("operator", QueryOperator.UNKNOWN) == expected
    assert "inherited" in classification
    assert classification.get("inherited", False) is False


def test_contextual_operator_classification_handles_lead_and_inheritance() -> None:
    prior = compact_query_frame(**{**COMPACT_FRAME_VALUES, "operator": QueryOperator.WHERE, "source_turn": 7})

    explicit = classify_query_frame_operator("And when?", prior)
    inherited = classify_query_frame_operator("And then?", prior)

    assert explicit == {"operator": QueryOperator.WHEN, "confidence": 0.98, "inherited": False, "source_turn": 0}
    assert inherited.get("operator", QueryOperator.UNKNOWN) == QueryOperator.WHERE
    assert inherited.get("inherited", False) is True
    assert inherited.get("source_turn", 0) == 7


@pytest_mark.parametrize(
    ("operator", "expected"),
    [
        (QueryOperator.WHO, ExpectedObjectType.PERSON),
        (QueryOperator.WHERE, ExpectedObjectType.PLACE),
        (QueryOperator.WHEN, ExpectedObjectType.DATE),
        (QueryOperator.HOW_MANY, ExpectedObjectType.NUMBER),
        (QueryOperator.COUNT, ExpectedObjectType.NUMBER),
        (QueryOperator.EXISTS, ExpectedObjectType.BOOLEAN),
        (QueryOperator.WHAT, ExpectedObjectType.ENTITY),
        (QueryOperator.WHICH, ExpectedObjectType.ENTITY),
        (QueryOperator.LOOKUP, ExpectedObjectType.ENTITY),
        (QueryOperator.COMPARE, ExpectedObjectType.UNKNOWN),
        (QueryOperator.UNKNOWN, ExpectedObjectType.UNKNOWN),
    ],
)
def test_expected_object_type_inference_is_closed_and_unknown_tolerant(
    operator: QueryOperator,
    expected: ExpectedObjectType,
) -> None:
    assert infer_expected_object_type(operator) == expected


def test_follow_up_inherits_only_missing_fields_and_records_source_turn() -> None:
    core = EngramCore()
    core.resolve_request(
        "When was Ada Lovelace born?",
        "sarah-1",
        user_id="Sarah",
        configured_resolvers=("exact",),
    )
    core.resolve_request(
        "And where?",
        "sarah-2",
        user_id="Sarah",
        configured_resolvers=("exact",),
    )

    current = core.engram.sessions.get("Sarah", {}).get("previous_query_frame", {})
    assert current.get("source_turn", 0) == 2
    assert current.get("operator", QueryOperator.UNKNOWN) == QueryOperator.WHERE
    assert current.get("subjects", ())[0].get("surface", "") == "Ada Lovelace"
    assert current.get("relation", {}).get("surface", "") == "born"
    assert current.get("expected_object_type", ExpectedObjectType.UNKNOWN) == ExpectedObjectType.PLACE
    cached_frame = core.resolution_requests.get("sarah-2", {}).get("frame", {})
    inherited_fields = {(item.get("field_name", ""), item.get("source_turn", 0)) for item in cached_frame.get("inheritance", ())}
    assert inherited_fields == {
        ("subjects", 1),
        ("relation", 1),
    }


def test_live_request_keeps_every_entity_while_the_carried_frame_is_bounded() -> None:
    core = EngramCore()
    request = "Compare Ada Lovelace, Charles Babbage, Alan Turing, Grace Hopper, Eve Adams, and the Paris Summit."

    core.resolve_request(request, "many-entities", user_id="Sarah", configured_resolvers=("exact",))

    live = core.resolution_requests.get("many-entities", {}).get("frame", {}).get("identity", {}).get("entities", ())
    carried = core.engram.sessions.get("Sarah", {}).get("previous_query_frame", {}).get("subjects", ())
    assert [entity.get("surface", "") for entity in live][-2:] == ["Eve Adams", "Paris Summit"]
    assert len(live) == 6
    assert len(carried) == 4


def test_self_contained_request_and_other_user_do_not_receive_prior_context() -> None:
    core = EngramCore()
    core.resolve_request(
        "When was Ada Lovelace born?",
        "sarah-context",
        user_id="Sarah",
        configured_resolvers=("exact",),
    )
    core.resolve_request(
        "Where is London?",
        "sarah-reset",
        user_id="Sarah",
        configured_resolvers=("exact",),
    )
    core.resolve_request("And when?", "robin-first", user_id="Robin", configured_resolvers=("exact",))

    sarah = core.engram.sessions.get("Sarah", {}).get("previous_query_frame", {})
    robin = core.engram.sessions.get("Robin", {}).get("previous_query_frame", {})
    sarah_relation = sarah.get("relation", {})
    assert [subject.get("surface", "") for subject in sarah.get("subjects", ())] == ["London"]
    assert "surface" in sarah_relation
    assert sarah_relation.get("surface", "") == ""
    assert "subjects" in robin
    assert robin.get("subjects", ()) == ()
    sarah_reset = core.resolution_requests.get("sarah-reset", {}).get("frame", {})
    robin_first = core.resolution_requests.get("robin-first", {}).get("frame", {})
    assert "inheritance" in sarah_reset
    assert sarah_reset.get("inheritance", ()) == ()
    assert "inheritance" in robin_first
    assert robin_first.get("inheritance", ()) == ()


def test_topic_change_and_session_expiration_remove_follow_up_context() -> None:
    core = EngramCore()
    sessions.start_session(core.engram, session_id="Sarah")
    started_session = core.engram.sessions.get("Sarah", {})
    assert started_session
    started_session["active_topic"] = "Ada Lovelace"
    core.resolve_request(
        "When was Ada Lovelace born?",
        "topic-1",
        user_id="Sarah",
        configured_resolvers=("exact",),
    )
    first_turn_session = core.engram.sessions.get("Sarah", {})
    assert first_turn_session
    first_turn_session["active_topic"] = "Grace Hopper"
    core.resolve_request("And then?", "topic-2", user_id="Sarah", configured_resolvers=("exact",))

    topic_frame = core.resolution_requests.get("topic-2", {}).get("frame", {})
    topic_identity = topic_frame.get("identity", {})
    assert "inheritance" in topic_frame
    assert topic_frame.get("inheritance", ()) == ()
    assert "operator" in topic_identity
    assert topic_identity.get("operator", QueryOperator.UNKNOWN) == QueryOperator.UNKNOWN
    expiring_session = core.engram.sessions.get("Sarah", {})
    assert "last_active" in expiring_session
    expiring_session["last_active"] = expiring_session.get("last_active", EARLIEST_UTC) - timedelta(days=1)
    assert sessions.expire_sessions(core.engram, timedelta(hours=1)) == 1
    assert "Sarah" not in core.engram.sessions


def test_caller_topic_predicate_is_retained_in_contextual_frames() -> None:
    core = EngramCore()
    core.set_predicate("Sarah", "topic", "Kyoto")

    core.resolve_request(
        "When is the spring festival?",
        "predicate-topic",
        user_id="Sarah",
        configured_resolvers=("exact",),
    )

    session = core.engram.sessions.get("Sarah", {})
    previous_frame = session.get("previous_query_frame", {})
    assert session.get("active_topic", "") == ""
    assert previous_frame.get("topic", "") == "Kyoto"


def test_operator_inheritance_obeys_the_same_turn_distance_as_other_fields() -> None:
    core = EngramCore()
    core.resolve_request(
        "Where was Ada Lovelace born?",
        "distance-1",
        user_id="Sarah",
        configured_resolvers=("exact",),
    )
    distance_session = core.engram.sessions.get("Sarah", {})
    assert distance_session
    distance_session["query_frame_turn"] = 3

    core.resolve_request("And then?", "distance-4", user_id="Sarah", configured_resolvers=("exact",))

    distant_frame = core.resolution_requests.get("distance-4", {}).get("frame", {})
    distant_identity = distant_frame.get("identity", {})
    assert "inheritance" in distant_frame
    assert distant_frame.get("inheritance", ()) == ()
    assert "operator" in distant_identity
    assert distant_identity.get("operator", QueryOperator.UNKNOWN) == QueryOperator.UNKNOWN


def test_compact_frame_rejects_duplicate_or_malformed_identity_values() -> None:
    duplicate = entity_reference("Ada Lovelace")
    duplicate_qualifier = identity_qualifier(QualifierKind.CURRENT, "current")
    with pytest_raises(InvalidRequestError):
        compact_query_frame(**{**COMPACT_FRAME_VALUES, "subjects": (duplicate,) * 2})
    with pytest_raises(InvalidRequestError):
        compact_query_frame(**{**COMPACT_FRAME_VALUES, "confidence": float("nan")})
    with pytest_raises(InvalidRequestError, match="qualifiers must be unique"):
        compact_query_frame(**{**COMPACT_FRAME_VALUES, "qualifiers": (duplicate_qualifier,) * 2})
