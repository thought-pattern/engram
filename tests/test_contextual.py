"""Section 8 compact query-frame and bounded follow-up tests."""

from datetime import timedelta

import pytest

from engram import persistence, sessions
from engram.constants import ExpectedObjectType, QualifierKind, QueryOperator
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


def compact_frame(**changes):
    values = {
        "operator": QueryOperator.WHEN,
        "subjects": (entity_reference("Ada Lovelace", "entity:ada-lovelace"),),
        "relation": relation_reference("born", "predicate:date-of-birth"),
        "expected_object_type": ExpectedObjectType.DATE,
        "qualifiers": (),
        "source_turn": 3,
        "confidence": 0.9,
        "topic": "Ada Lovelace",
    }
    values.update(changes)
    return compact_query_frame(**values)


def test_compact_query_frame_codec_is_exact_bounded_and_deterministic() -> None:
    value = compact_frame()
    encoded = compact_query_frame_to_dict(value)

    assert compact_query_frame_from_dict(encoded) == value
    assert encoded["operator"] == "when"
    assert encoded["expected_object_type"] == "DATE"
    subjects = encoded["subjects"]
    assert subjects[0]["canonical_id"] == "entity:ada-lovelace"

    malformed = dict(encoded)
    malformed["extra"] = True
    with pytest.raises(InvalidRequestError, match="invalid fields"):
        compact_query_frame_from_dict(malformed)
    with pytest.raises(InvalidRequestError, match="subjects exceed"):
        compact_frame(subjects=tuple(entity_reference(f"Entity {index}") for index in range(5)))


def test_session_persistence_preserves_compact_frame_and_legacy_absence() -> None:
    engine = Engram()
    session_id = sessions.create_session(engine, session_id="Sarah")
    engine.sessions[session_id]["previous_query_frame"] = compact_frame()
    engine.sessions[session_id]["query_frame_turn"] = 3

    restored = persistence.load_engram_from_dict(persistence.to_dict(engine))
    restored_session = sessions.get_session(restored, session_id, create_if_missing=False)

    assert restored_session["previous_query_frame"] == compact_frame()
    assert restored_session["query_frame_turn"] == 3

    serialized = persistence.to_dict(engine)
    del serialized["sessions"][0]["previous_query_frame"]
    del serialized["sessions"][0]["query_frame_turn"]
    legacy = persistence.load_engram_from_dict(serialized)
    assert legacy.sessions[session_id]["previous_query_frame"] == {}
    assert legacy.sessions[session_id]["query_frame_turn"] == 0

    invalid_frame = persistence.to_dict(engine)
    invalid_frame["sessions"][0]["previous_query_frame"] = False
    with pytest.raises(ValueError, match="previous_query_frame must be an object"):
        persistence.load_engram_from_dict(invalid_frame)

    invalid_turn = persistence.to_dict(engine)
    invalid_turn["sessions"][0]["query_frame_turn"] = False
    with pytest.raises(ValueError, match="query_frame_turn"):
        persistence.load_engram_from_dict(invalid_turn)

    engine.sessions[session_id]["query_frame_turn"] = 2
    with pytest.raises(ValueError, match="source_turn must match"):
        persistence.to_dict(engine)


def test_resolve_request_checkpoints_contextual_frame_when_configured(tmp_path) -> None:
    store_path = tmp_path / "contextual-frame.json"
    core = EngramCore(Engram(), store_path=str(store_path))

    core.resolve_request(
        "When was Ada Lovelace born?",
        "persisted-context-1",
        user_id="Sarah",
        configured_resolvers=("exact",),
    )

    restored = persistence.load_engram(store_path)
    session = restored.sessions["Sarah"]
    assert core.status()["dirty"] is False
    assert session["query_frame_turn"] == 1
    assert session["previous_query_frame"]["operator"] == QueryOperator.WHEN
    assert session["previous_query_frame"]["subjects"][0]["surface"] == "Ada Lovelace"


@pytest.mark.parametrize(
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
    assert classification["operator"] == expected
    assert classification["inherited"] is False


def test_contextual_operator_classification_handles_lead_and_inheritance() -> None:
    prior = compact_frame(operator=QueryOperator.WHERE, source_turn=7)

    explicit = classify_query_frame_operator("And when?", prior)
    inherited = classify_query_frame_operator("And then?", prior)

    assert explicit == {"operator": QueryOperator.WHEN, "confidence": 0.98, "inherited": False, "source_turn": 0}
    assert inherited["operator"] == QueryOperator.WHERE
    assert inherited["inherited"] is True
    assert inherited["source_turn"] == 7


@pytest.mark.parametrize(
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
    core = EngramCore(checkpoint_on_mutation=False)
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

    current = core.engram.sessions["Sarah"]["previous_query_frame"]
    assert current["source_turn"] == 2
    assert current["operator"] == QueryOperator.WHERE
    assert current["subjects"][0]["surface"] == "Ada Lovelace"
    assert current["relation"]["surface"] == "born"
    assert current["expected_object_type"] == ExpectedObjectType.PLACE
    cached_frame = core._resolution_requests["sarah-2"]["frame"]
    assert {(item["field_name"], item["source_turn"]) for item in cached_frame["inheritance"]} == {
        ("subjects", 1),
        ("relation", 1),
    }


def test_self_contained_request_and_other_user_do_not_receive_prior_context() -> None:
    core = EngramCore(checkpoint_on_mutation=False)
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

    sarah = core.engram.sessions["Sarah"]["previous_query_frame"]
    robin = core.engram.sessions["Robin"]["previous_query_frame"]
    assert [subject["surface"] for subject in sarah["subjects"]] == ["London"]
    assert sarah["relation"]["surface"] == ""
    assert robin["subjects"] == ()
    sarah_reset = core._resolution_requests["sarah-reset"]["frame"]
    robin_first = core._resolution_requests["robin-first"]["frame"]
    assert sarah_reset["inheritance"] == ()
    assert robin_first["inheritance"] == ()


def test_topic_change_and_session_expiration_remove_follow_up_context() -> None:
    core = EngramCore(checkpoint_on_mutation=False)
    sessions.create_session(core.engram, session_id="Sarah")
    core.engram.sessions["Sarah"]["active_topic"] = "Ada Lovelace"
    core.resolve_request(
        "When was Ada Lovelace born?",
        "topic-1",
        user_id="Sarah",
        configured_resolvers=("exact",),
    )
    core.engram.sessions["Sarah"]["active_topic"] = "Grace Hopper"
    core.resolve_request("And then?", "topic-2", user_id="Sarah", configured_resolvers=("exact",))

    topic_frame = core._resolution_requests["topic-2"]["frame"]
    assert topic_frame["inheritance"] == ()
    assert topic_frame["identity"]["operator"] == QueryOperator.UNKNOWN
    core.engram.sessions["Sarah"]["last_active"] -= timedelta(days=1)
    assert sessions.expire_sessions(core.engram, timedelta(hours=1)) == 1
    assert "Sarah" not in core.engram.sessions


def test_operator_inheritance_obeys_the_same_turn_distance_as_other_fields() -> None:
    core = EngramCore(checkpoint_on_mutation=False)
    core.resolve_request(
        "Where was Ada Lovelace born?",
        "distance-1",
        user_id="Sarah",
        configured_resolvers=("exact",),
    )
    core.engram.sessions["Sarah"]["query_frame_turn"] = 3

    core.resolve_request("And then?", "distance-4", user_id="Sarah", configured_resolvers=("exact",))

    distant_frame = core._resolution_requests["distance-4"]["frame"]
    assert distant_frame["inheritance"] == ()
    assert distant_frame["identity"]["operator"] == QueryOperator.UNKNOWN


def test_compact_frame_rejects_duplicate_or_malformed_identity_values() -> None:
    duplicate = entity_reference("Ada Lovelace")
    duplicate_qualifier = identity_qualifier(QualifierKind.CURRENT, "current")
    with pytest.raises(InvalidRequestError):
        compact_frame(subjects=(duplicate,) * 2)
    with pytest.raises(InvalidRequestError):
        compact_frame(confidence=float("nan"))
    with pytest.raises(InvalidRequestError, match="qualifiers must be unique"):
        compact_frame(qualifiers=(duplicate_qualifier,) * 2)
    with pytest.raises(InvalidRequestError, match="schema_version"):
        compact_frame(schema_version=True)
