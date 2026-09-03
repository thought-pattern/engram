"""Bounded Section 8 query-frame retention and follow-up enrichment."""

from math import isfinite as math_isfinite

from engram.constants import (
    COMPACT_QUERY_FRAME_FIELDS,
    COMPACT_QUERY_FRAME_SCHEMA_VERSION,
    MAX_CONTEXTUAL_SUBJECTS,
    MAX_CONTEXTUAL_TOPIC_BYTES,
    MAX_CONTEXTUAL_TURN_DISTANCE,
    MIN_CONTEXTUAL_INHERITANCE_CONFIDENCE,
    ExpectedObjectType,
    QualifierKind,
    QueryOperator,
    TemporalQueryOperator,
)
from engram.errors import IdentityValidationError, InvalidRequestError
from engram.identity import (
    entity_reference_from_dict,
    entity_reference_to_dict,
    extract_operator,
    identity_qualifier_from_dict,
    identity_qualifier_to_dict,
    normalize_retrieval_key,
    query_identity,
    relation_reference_from_dict,
    relation_reference_to_dict,
    validate_entity_reference,
    validate_identity_qualifier,
    validate_relation_reference,
)
from engram.resolution import inheritance_provenance, query_frame_with_changes, validate_query_frame
from engram.temporal import temporal_query, temporal_query_from_dict, temporal_query_to_dict, validate_temporal_query

FOLLOW_UP_LEADS = (
    "and ",
    "also ",
    "what about ",
    "how about ",
    "then ",
    "instead ",
)
FOLLOW_UP_REFERENTS = set({"it", "its", "that", "this", "they", "them", "their", "there", "he", "she"})


def internal_text(value: object, name: str, maximum_bytes: int, *, allow_empty: bool) -> str:
    if not isinstance(value, str):
        raise InvalidRequestError(f"{name} must be a string")
    if not allow_empty and not value:
        raise InvalidRequestError(f"{name} must not be empty")
    if len(value.encode("utf-8")) > maximum_bytes:
        raise InvalidRequestError(f"{name} exceeds the limit of {maximum_bytes} UTF-8 bytes")
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise InvalidRequestError(f"{name} contains a control character")
    return value


def internal_turn(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 1_000_000:
        raise InvalidRequestError(f"{name} must be an integer from 1 through 1000000")
    return value


def internal_confidence(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise InvalidRequestError(f"{name} must be numeric")
    result = float(value)
    if not math_isfinite(result) or not 0.0 <= result <= 1.0:
        raise InvalidRequestError(f"{name} must be finite and from 0 through 1")
    return result


def internal_mapping(value: object, name: str) -> dict:
    if not isinstance(value, dict):
        raise InvalidRequestError(f"{name} must be an object")
    return value


def compact_query_frame(
    *,
    operator: object,
    subjects: object,
    relation: object,
    expected_object_type: object,
    qualifiers: object,
    source_turn: object,
    confidence: object,
    temporal_query_value: object = (),
    topic: object = "",
    schema_version: object = COMPACT_QUERY_FRAME_SCHEMA_VERSION,
) -> dict:
    """Build the only query interpretation retained in user context."""
    if (
        isinstance(schema_version, bool)
        or not isinstance(schema_version, int)
        or schema_version != COMPACT_QUERY_FRAME_SCHEMA_VERSION
    ):
        raise InvalidRequestError(f"unsupported compact query frame schema_version: {schema_version}")
    if not isinstance(operator, QueryOperator):
        raise InvalidRequestError("compact query frame operator must be a QueryOperator")
    if not isinstance(expected_object_type, ExpectedObjectType):
        raise InvalidRequestError("compact query frame expected_object_type must be an ExpectedObjectType")
    try:
        validated_temporal = temporal_query() if temporal_query_value == () else validate_temporal_query(temporal_query_value)
    except InvalidRequestError as error:
        raise InvalidRequestError("compact query frame temporal_query must be a TemporalQuery") from error
    if not isinstance(subjects, tuple):
        raise InvalidRequestError("compact query frame subjects must be a tuple")
    if len(subjects) > MAX_CONTEXTUAL_SUBJECTS:
        raise InvalidRequestError(f"compact query frame subjects exceed the limit of {MAX_CONTEXTUAL_SUBJECTS}")
    try:
        validated_subjects = tuple(validate_entity_reference(value) for value in subjects)
        validated_relation = validate_relation_reference(relation)
    except IdentityValidationError as error:
        raise InvalidRequestError("compact query frame identity fields are malformed") from error
    subject_keys = tuple((value["surface"].casefold(), value["canonical_id"]) for value in validated_subjects)
    if len(subject_keys) != len(set(subject_keys)):
        raise InvalidRequestError("compact query frame subjects must be unique")
    if not isinstance(qualifiers, tuple):
        raise InvalidRequestError("compact query frame qualifiers must be a tuple")
    try:
        validated_qualifiers = tuple(validate_identity_qualifier(value) for value in qualifiers)
    except IdentityValidationError as error:
        raise InvalidRequestError("compact query frame qualifiers are malformed") from error
    qualifier_keys = tuple((value["kind"], value["value"]) for value in validated_qualifiers)
    if len(qualifier_keys) != len(set(qualifier_keys)):
        raise InvalidRequestError("compact query frame qualifiers must be unique")
    result: dict = {
        "schema_version": COMPACT_QUERY_FRAME_SCHEMA_VERSION,
        "operator": operator,
        "subjects": validated_subjects,
        "relation": validated_relation,
        "expected_object_type": expected_object_type,
        "temporal_query": validated_temporal,
        "qualifiers": validated_qualifiers,
        "source_turn": internal_turn(source_turn, "compact query frame source_turn"),
        "confidence": internal_confidence(confidence, "compact query frame confidence"),
        "topic": internal_text(topic, "compact query frame topic", MAX_CONTEXTUAL_TOPIC_BYTES, allow_empty=True),
    }
    return result


def validate_compact_query_frame(value: object) -> dict:
    data = internal_mapping(value, "CompactQueryFrame")
    observed = set(data)
    if observed != COMPACT_QUERY_FRAME_FIELDS:
        raise InvalidRequestError(
            "CompactQueryFrame has invalid fields: "
            f"missing={sorted(COMPACT_QUERY_FRAME_FIELDS - observed)}, "
            f"extra={sorted(observed - COMPACT_QUERY_FRAME_FIELDS)}"
        )
    result = compact_query_frame(
        operator=data["operator"],
        subjects=data["subjects"],
        relation=data["relation"],
        expected_object_type=data["expected_object_type"],
        temporal_query_value=data["temporal_query"],
        qualifiers=data["qualifiers"],
        source_turn=data["source_turn"],
        confidence=data["confidence"],
        topic=data["topic"],
        schema_version=data["schema_version"],
    )
    return result


def compact_query_frame_to_dict(value: object) -> dict:
    frame = validate_compact_query_frame(value)
    result = {
        "schema_version": frame["schema_version"],
        "operator": frame["operator"].value,
        "subjects": [entity_reference_to_dict(subject) for subject in frame["subjects"]],
        "relation": relation_reference_to_dict(frame["relation"]),
        "expected_object_type": frame["expected_object_type"].value,
        "temporal_query": temporal_query_to_dict(frame["temporal_query"]),
        "qualifiers": [identity_qualifier_to_dict(qualifier) for qualifier in frame["qualifiers"]],
        "source_turn": frame["source_turn"],
        "confidence": frame["confidence"],
        "topic": frame["topic"],
    }
    return result


def compact_query_frame_from_dict(value: object) -> dict:
    data = internal_mapping(value, "CompactQueryFrame")
    observed = set(data)
    if observed != COMPACT_QUERY_FRAME_FIELDS:
        raise InvalidRequestError(
            "CompactQueryFrame has invalid fields: "
            f"missing={sorted(COMPACT_QUERY_FRAME_FIELDS - observed)}, "
            f"extra={sorted(observed - COMPACT_QUERY_FRAME_FIELDS)}"
        )
    raw_subjects = data["subjects"]
    raw_qualifiers = data["qualifiers"]
    if not isinstance(raw_subjects, list) or not isinstance(raw_qualifiers, list):
        raise InvalidRequestError("serialized compact query frame collections must be lists")
    try:
        operator = QueryOperator(internal_text(data["operator"], "compact operator", 32, allow_empty=False))
        expected = ExpectedObjectType(
            internal_text(data["expected_object_type"], "compact expected_object_type", 32, allow_empty=False)
        )
    except ValueError as error:
        raise InvalidRequestError("serialized compact query frame enum is unsupported") from error
    result = compact_query_frame(
        operator=operator,
        subjects=tuple(entity_reference_from_dict(internal_mapping(item, "compact subject")) for item in raw_subjects),
        relation=relation_reference_from_dict(internal_mapping(data["relation"], "compact relation")),
        expected_object_type=expected,
        temporal_query_value=temporal_query_from_dict(internal_mapping(data["temporal_query"], "compact temporal query")),
        qualifiers=tuple(identity_qualifier_from_dict(internal_mapping(item, "compact qualifier")) for item in raw_qualifiers),
        source_turn=data["source_turn"],
        confidence=data["confidence"],
        topic=data["topic"],
        schema_version=data["schema_version"],
    )
    return result


def infer_expected_object_type(operator: object) -> ExpectedObjectType:
    """Infer only the closed Section 4 object-type vocabulary."""
    if not isinstance(operator, QueryOperator):
        raise InvalidRequestError("expected object type inference requires a QueryOperator")
    values = {
        QueryOperator.WHO: ExpectedObjectType.PERSON,
        QueryOperator.WHERE: ExpectedObjectType.PLACE,
        QueryOperator.WHEN: ExpectedObjectType.DATE,
        QueryOperator.HOW_MANY: ExpectedObjectType.NUMBER,
        QueryOperator.COUNT: ExpectedObjectType.NUMBER,
        QueryOperator.EXISTS: ExpectedObjectType.BOOLEAN,
        QueryOperator.WHAT: ExpectedObjectType.ENTITY,
        QueryOperator.WHICH: ExpectedObjectType.ENTITY,
        QueryOperator.LOOKUP: ExpectedObjectType.ENTITY,
    }
    result = values.get(operator, ExpectedObjectType.UNKNOWN)
    return result


def is_elliptical_follow_up(request: object) -> bool:
    """Recognize bounded surface evidence for a context-dependent follow-up."""
    text = internal_text(request, "follow-up request", 4_096, allow_empty=False)
    normalized = normalize_retrieval_key(text)
    tokens = normalized.split()
    if not tokens:
        return False
    if normalized.startswith(FOLLOW_UP_LEADS):
        return True
    if any(token in FOLLOW_UP_REFERENTS for token in tokens):
        return True
    result = len(tokens) <= 4 and tokens[0] in {
        "who",
        "what",
        "where",
        "when",
        "which",
        "why",
        "how",
        "count",
    }
    return result


def classify_query_frame_operator(request: object, previous: object = {}) -> dict:
    """Classify or conservatively inherit the existing QueryOperator vocabulary."""
    text = internal_text(request, "operator request", 4_096, allow_empty=False)
    normalized = normalize_retrieval_key(text)
    contextual_text = normalized
    for lead in FOLLOW_UP_LEADS:
        if normalized.startswith(lead):
            contextual_text = normalized[len(lead) :]
            break
    current = extract_operator(contextual_text)
    if current != QueryOperator.UNKNOWN:
        result: dict = {
            "operator": current,
            "confidence": 0.98,
            "inherited": False,
            "source_turn": 0,
        }
        return result
    if previous and is_elliptical_follow_up(text):
        prior = validate_compact_query_frame(previous)
        if prior["confidence"] >= MIN_CONTEXTUAL_INHERITANCE_CONFIDENCE:
            result = {
                "operator": prior["operator"],
                "confidence": max(0.0, prior["confidence"] - 0.1),
                "inherited": True,
                "source_turn": prior["source_turn"],
            }
            return result
    result = {
        "operator": QueryOperator.UNKNOWN,
        "confidence": 0.0,
        "inherited": False,
        "source_turn": 0,
    }
    return result


def topic_continues(previous: dict, topic: str, follow_up: bool) -> bool:
    prior_topic = normalize_retrieval_key(previous.get("topic", "")) if previous.get("topic", "") else ""
    current_topic = normalize_retrieval_key(topic) if topic else ""
    if prior_topic and current_topic:
        result = prior_topic == current_topic
        return result
    if current_topic and not prior_topic:
        return False
    return follow_up


def frame_confidence(frame: dict, inherited_confidence: float = 0.0) -> float:
    identity = frame.get("identity", {})
    observations = []
    if identity["operator"] != QueryOperator.UNKNOWN:
        observations.append(0.98)
    if identity["entities"]:
        observations.append(0.95 if all(value["canonical_id"] for value in identity["entities"]) else 0.75)
    if identity["relation"]["surface"]:
        observations.append(0.95 if identity["relation"]["canonical_id"] else 0.7)
    if frame.get("expected_object_type", ExpectedObjectType.UNKNOWN) != ExpectedObjectType.UNKNOWN:
        observations.append(0.9)
    if inherited_confidence:
        observations.append(inherited_confidence)
    result = min(observations) if observations else 0.0
    return result


def enrich_query_frame(
    value: object,
    *,
    previous: object = {},
    current_turn: object,
    topic: object = "",
) -> dict:
    """Populate expected type and inherit only missing fields from nearby context."""
    frame = validate_query_frame(value)
    turn = internal_turn(current_turn, "current query frame turn")
    current_topic = internal_text(topic, "current query frame topic", MAX_CONTEXTUAL_TOPIC_BYTES, allow_empty=True)
    identity = frame["identity"]
    operator = identity["operator"]
    subjects = identity["entities"][:MAX_CONTEXTUAL_SUBJECTS]
    relation = identity["relation"]
    qualifiers = identity["qualifiers"]
    expected = frame["expected_object_type"]
    temporal = frame["temporal_query"]
    if expected == ExpectedObjectType.UNKNOWN:
        expected = infer_expected_object_type(operator)
    provenance = list(frame["inheritance"])
    normalized_request = normalize_retrieval_key(frame["original_text"])
    explicit_follow_up = normalized_request.startswith(FOLLOW_UP_LEADS) or any(
        token in FOLLOW_UP_REFERENTS for token in normalized_request.split()
    )
    temporal_follow_up = not subjects and temporal["operator"] != TemporalQueryOperator.UNSPECIFIED
    follow_up = explicit_follow_up or temporal_follow_up or (not subjects and is_elliptical_follow_up(frame["original_text"]))
    self_contained = bool(subjects) and not follow_up
    eligible_previous: object = {}
    if previous:
        prior = validate_compact_query_frame(previous)
        distance = turn - prior["source_turn"]
        if (
            not self_contained
            and 1 <= distance <= MAX_CONTEXTUAL_TURN_DISTANCE
            and prior["confidence"] >= MIN_CONTEXTUAL_INHERITANCE_CONFIDENCE
            and topic_continues(prior, current_topic, follow_up)
        ):
            eligible_previous = prior
    classification = classify_query_frame_operator(frame["original_text"], eligible_previous)
    if operator == QueryOperator.UNKNOWN and classification["operator"] != QueryOperator.UNKNOWN:
        operator = classification["operator"]
        if classification["inherited"]:
            provenance.append(inheritance_provenance("operator", classification["source_turn"]))
        expected = infer_expected_object_type(operator)

    if eligible_previous:
        prior = validate_compact_query_frame(eligible_previous)
        if not self_contained:
            if operator == QueryOperator.UNKNOWN and prior["operator"] != QueryOperator.UNKNOWN:
                operator = prior["operator"]
                provenance.append(inheritance_provenance("operator", prior["source_turn"]))
            if not subjects and prior["subjects"]:
                subjects = prior["subjects"]
                provenance.append(inheritance_provenance("subjects", prior["source_turn"]))
            if not relation["surface"] and prior["relation"]["surface"]:
                relation = prior["relation"]
                provenance.append(inheritance_provenance("relation", prior["source_turn"]))
            if expected == ExpectedObjectType.UNKNOWN and prior["expected_object_type"] != ExpectedObjectType.UNKNOWN:
                expected = prior["expected_object_type"]
                provenance.append(inheritance_provenance("expected_object_type", prior["source_turn"]))
            present_kinds = {qualifier["kind"] for qualifier in qualifiers}
            if temporal["operator"] != TemporalQueryOperator.UNSPECIFIED:
                present_kinds.update({QualifierKind.CURRENT, QualifierKind.HISTORICAL, QualifierKind.TEMPORAL})
            inherited_qualifiers = tuple(qualifier for qualifier in prior["qualifiers"] if qualifier["kind"] not in present_kinds)
            if inherited_qualifiers:
                qualifiers = (*qualifiers, *inherited_qualifiers)
                provenance.append(inheritance_provenance("qualifiers", prior["source_turn"]))
            if temporal["operator"] == TemporalQueryOperator.UNSPECIFIED:
                temporal = prior["temporal_query"]
                if temporal["operator"] != TemporalQueryOperator.UNSPECIFIED:
                    provenance.append(inheritance_provenance("temporal_query", prior["source_turn"]))

    if expected == ExpectedObjectType.UNKNOWN:
        expected = infer_expected_object_type(operator)
    updated_identity: dict = query_identity(
        canonical_form=identity["canonical_form"],
        operator=operator,
        entities=subjects,
        relation=relation,
        qualifiers=qualifiers,
        lexical_terms=identity["lexical_terms"],
        scope=identity["scope"],
        normalization_version=identity["normalization_version"],
        schema_version=identity["schema_version"],
    )
    result = query_frame_with_changes(
        frame,
        {
            "identity": updated_identity,
            "expected_object_type": expected,
            "temporal_query": temporal,
            "inheritance": tuple(provenance),
        },
    )
    return result


def compact_query_frame_from_frame(value: object, *, source_turn: object, topic: object = "") -> dict:
    """Project one enriched runtime frame into bounded user-owned context."""
    frame = validate_query_frame(value)
    result = compact_query_frame(
        operator=frame["identity"]["operator"],
        subjects=frame["identity"]["entities"][:MAX_CONTEXTUAL_SUBJECTS],
        relation=frame["identity"]["relation"],
        expected_object_type=frame["expected_object_type"],
        temporal_query_value=frame["temporal_query"],
        qualifiers=frame["identity"]["qualifiers"],
        source_turn=source_turn,
        confidence=frame_confidence(frame),
        topic=topic,
    )
    return result
