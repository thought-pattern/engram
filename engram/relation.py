"""Canonical entity, Predicate, and bounded one-hop relation interpretation."""

import math
from collections.abc import Callable, Mapping
from datetime import datetime

from engram.constants import (
    CANONICAL_RESOLUTION_FIELDS,
    MAX_RELATION_CANDIDATES,
    MAX_RELATION_LABEL_BYTES,
    MAX_RELATION_PLAN_ROWS,
    MAX_RELATION_SURFACES,
    ONE_HOP_QUERY_PLAN_FIELDS,
    RELATION_CONTRACT_SCHEMA_VERSION,
    CanonicalResolutionStatus,
    ExpectedObjectType,
    PredicateCardinality,
    RelationPlanTemplate,
    RelationSelectionReason,
    TemporalAxis,
    TemporalQueryOperator,
)
from engram.errors import InvalidRequestError
from engram.graph import (
    CanonicalEntityMatch,
    CanonicalPredicateMatch,
    RelationPropositionProjection,
    validate_relation_proposition_projection,
)
from engram.identity import normalize_retrieval_key
from engram.resolution import validate_query_frame
from engram.spacy_setup import get_nlp
from engram.temporal import TemporalQuery, validate_temporal_query

CanonicalResolution = dict


OneHopQueryPlan = dict


RelationPropositionSelection = dict


def _text(value: object, name: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str) or (not allow_empty and not value):
        raise InvalidRequestError(f"{name} must be a {'possibly empty ' if allow_empty else 'non-empty '}string")
    if len(value.encode("utf-8")) > MAX_RELATION_LABEL_BYTES:
        raise InvalidRequestError(f"{name} exceeds {MAX_RELATION_LABEL_BYTES} UTF-8 bytes")
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise InvalidRequestError(f"{name} contains a control character")
    return value


def _identifier(value: object, name: str, *, allow_empty: bool = False) -> str:
    result = _text(value, name, allow_empty=allow_empty)
    if result and any(character.isspace() for character in result):
        raise InvalidRequestError(f"{name} must not contain whitespace")
    return result


def _score(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise InvalidRequestError(f"{name} must be numeric")
    result = float(value)
    if not math.isfinite(result) or not 0.0 <= result <= 1.0:
        raise InvalidRequestError(f"{name} must be finite and from 0 through 1")
    return result


def canonical_resolution(
    status: object,
    *,
    canonical_id: object = "",
    primary_label: object = "",
    object_type: object = ExpectedObjectType.UNKNOWN,
    score: object = 0.0,
    candidate_ids: object = (),
    evidence: object = (),
    schema_version: object = RELATION_CONTRACT_SCHEMA_VERSION,
) -> CanonicalResolution:
    """Build one explicit selected, ambiguous, or miss resolution."""
    if (
        isinstance(schema_version, bool)
        or not isinstance(schema_version, int)
        or schema_version != RELATION_CONTRACT_SCHEMA_VERSION
    ):
        raise InvalidRequestError("unsupported canonical resolution schema_version")
    if not isinstance(status, CanonicalResolutionStatus):
        raise InvalidRequestError("canonical resolution status is unsupported")
    if not isinstance(object_type, ExpectedObjectType):
        raise InvalidRequestError("canonical resolution object_type is unsupported")
    if not isinstance(candidate_ids, tuple) or not isinstance(evidence, tuple):
        raise InvalidRequestError("canonical resolution collections must be tuples")
    normalized_ids = tuple(_identifier(value, "canonical resolution candidate ID") for value in candidate_ids)
    normalized_evidence = tuple(_identifier(value, "canonical resolution evidence") for value in evidence)
    if len(normalized_ids) > MAX_RELATION_CANDIDATES or normalized_ids != tuple(sorted(set(normalized_ids))):
        raise InvalidRequestError("canonical resolution candidate IDs must be bounded, unique, and sorted")
    if len(normalized_evidence) > MAX_RELATION_SURFACES or normalized_evidence != tuple(sorted(set(normalized_evidence))):
        raise InvalidRequestError("canonical resolution evidence must be bounded, unique, and sorted")
    normalized_id = _identifier(canonical_id, "canonical resolution ID", allow_empty=True)
    normalized_label = _text(primary_label, "canonical resolution label", allow_empty=True)
    normalized_score = _score(score, "canonical resolution score")
    if status == CanonicalResolutionStatus.SELECTED:
        if not normalized_id or not normalized_label or normalized_id not in normalized_ids or not normalized_evidence:
            raise InvalidRequestError("selected canonical resolution is incomplete")
    elif normalized_id or normalized_label or normalized_score or object_type != ExpectedObjectType.UNKNOWN:
        raise InvalidRequestError("non-selected canonical resolution must not carry a selected value")
    if status == CanonicalResolutionStatus.AMBIGUOUS and len(normalized_ids) < 2:
        raise InvalidRequestError("ambiguous canonical resolution requires at least two candidates")
    if status == CanonicalResolutionStatus.MISS and normalized_ids:
        raise InvalidRequestError("miss canonical resolution must not carry candidates")
    result: CanonicalResolution = {
        "schema_version": RELATION_CONTRACT_SCHEMA_VERSION,
        "status": status,
        "canonical_id": normalized_id,
        "primary_label": normalized_label,
        "object_type": object_type,
        "score": normalized_score,
        "candidate_ids": normalized_ids,
        "evidence": normalized_evidence,
    }
    return result


def validate_canonical_resolution(value: object) -> CanonicalResolution:
    if not isinstance(value, Mapping) or set(value) != CANONICAL_RESOLUTION_FIELDS:
        raise InvalidRequestError("CanonicalResolution has invalid fields")
    return canonical_resolution(
        value["status"],
        canonical_id=value["canonical_id"],
        primary_label=value["primary_label"],
        object_type=value["object_type"],
        score=value["score"],
        candidate_ids=value["candidate_ids"],
        evidence=value["evidence"],
        schema_version=value["schema_version"],
    )


def _miss() -> CanonicalResolution:
    return canonical_resolution(CanonicalResolutionStatus.MISS)


def _explicit_resolution(values: tuple[tuple[str, str, ExpectedObjectType], ...], evidence: str) -> CanonicalResolution:
    by_id = {canonical_id: (label, object_type) for canonical_id, label, object_type in values}
    candidate_ids = tuple(sorted(by_id))
    if not candidate_ids:
        return _miss()
    if len(candidate_ids) > 1:
        return canonical_resolution(
            CanonicalResolutionStatus.AMBIGUOUS,
            candidate_ids=candidate_ids,
            evidence=(evidence,),
        )
    canonical_id = next(iter(candidate_ids))
    label, object_type = by_id[canonical_id]
    return canonical_resolution(
        CanonicalResolutionStatus.SELECTED,
        canonical_id=canonical_id,
        primary_label=label,
        object_type=object_type,
        score=1.0,
        candidate_ids=candidate_ids,
        evidence=(evidence,),
    )


def _named_entity_surfaces(text: str) -> tuple[str, ...]:
    nlp = get_nlp()
    if not nlp:
        return ()
    document = nlp(text)
    result = tuple(dict.fromkeys(entity.text.strip() for entity in document.ents if entity.text.strip()))
    return result[:MAX_RELATION_SURFACES]


def _select_scored(
    values: dict[str, tuple[float, str, ExpectedObjectType, set[str]]],
) -> CanonicalResolution:
    if not values:
        return _miss()
    ordered = sorted(values.items(), key=lambda item: (-item[1][0], item[0]))[:MAX_RELATION_CANDIDATES]
    candidate_ids = tuple(sorted(item[0] for item in ordered))
    best_id, (best_score, best_label, best_type, best_evidence) = ordered[0]
    if len(ordered) > 1 and best_score - ordered[1][1][0] <= 0.05:
        return canonical_resolution(
            CanonicalResolutionStatus.AMBIGUOUS,
            candidate_ids=candidate_ids,
            evidence=tuple(sorted({evidence for item in ordered for evidence in item[1][3]}))[:MAX_RELATION_SURFACES],
        )
    return canonical_resolution(
        CanonicalResolutionStatus.SELECTED,
        canonical_id=best_id,
        primary_label=best_label,
        object_type=best_type,
        score=best_score,
        candidate_ids=candidate_ids,
        evidence=tuple(sorted(best_evidence)),
    )


def resolve_canonical_subject(
    frame: object,
    lookup: Callable[..., list[CanonicalEntityMatch]],
    *,
    cooperative_check: Callable[[], object] = lambda: None,
) -> CanonicalResolution:
    """Resolve subjects from caller IDs, labels, aliases, edge surfaces, and NER."""
    current = validate_query_frame(frame)
    if not callable(lookup) or not callable(cooperative_check):
        raise InvalidRequestError("canonical subject resolver dependencies must be callable")
    explicit = tuple(
        (entity["canonical_id"], entity["surface"], ExpectedObjectType.UNKNOWN)
        for entity in current["identity"]["entities"]
        if entity["canonical_id"]
    )
    if explicit:
        return _explicit_resolution(explicit, "caller_entity_identity")
    surfaces: list[tuple[str, str, float]] = [
        (entity["surface"], "identity_surface", 0.0) for entity in current["identity"]["entities"]
    ]
    known = {normalize_retrieval_key(surface) for surface, _, _ in surfaces}
    for surface in _named_entity_surfaces(current["resolved_text"]):
        normalized = normalize_retrieval_key(surface)
        if normalized not in known:
            known.add(normalized)
            surfaces.append((surface, "named_entity", 0.03))
    scored: dict[str, tuple[float, str, ExpectedObjectType, set[str]]] = {}
    for surface, evidence, penalty in surfaces[:MAX_RELATION_SURFACES]:
        cooperative_check()
        normalized = normalize_retrieval_key(surface)
        rows = lookup(surface, limit=MAX_RELATION_CANDIDATES, cooperative_check=cooperative_check)
        if not isinstance(rows, list) or len(rows) > MAX_RELATION_CANDIDATES:
            raise InvalidRequestError("canonical entity lookup returned an invalid collection")
        for row in rows:
            label = normalize_retrieval_key(row["primary_label"])
            aliases = {normalize_retrieval_key(value) for value in row["aliases"]}
            edges = {normalize_retrieval_key(value) for value in row["edge_surfaces"]}
            base = 1.0 if normalized == label else 0.92 if normalized in aliases else 0.82 if normalized in edges else 0.0
            if not base:
                continue
            score = max(0.0, base - penalty)
            prior = scored.get(row["canonical_id"])
            evidence_values = {evidence, "primary_label" if base == 1.0 else "alias" if base == 0.92 else "edge_surface"}
            if prior and prior[0] >= score:
                prior[3].update(evidence_values)
            else:
                scored[row["canonical_id"]] = (score, row["primary_label"], row["entity_type"], evidence_values)
    cooperative_check()
    return _select_scored(scored)


def dependency_predicate_surfaces(text: object) -> tuple[tuple[str, str, float], ...]:
    """Extract bounded verb-lemma and preposition candidates from the loaded parser."""
    request = _text(text, "predicate request")
    nlp = get_nlp()
    if not nlp:
        return ()
    document = nlp(request)
    values: list[tuple[str, str, float]] = []
    auxiliaries = {"be", "do", "have", "can", "could", "will", "would", "should", "may", "might", "must"}
    for token in document:
        lemma = normalize_retrieval_key(token.lemma_ or token.text)
        predicate_root = (
            token.dep_ == "ROOT"
            and token.pos_ in {"NOUN", "ADJ"}
            and any(child.dep_ in {"aux", "auxpass"} for child in token.children)
        )
        if (token.pos_ == "VERB" or predicate_root) and lemma and lemma not in auxiliaries:
            values.append((lemma, "verb_lemma", 0.12))
            prepositions = [child for child in token.children if child.dep_ == "prep" and child.text]
            values.extend(
                (f"{lemma} {normalize_retrieval_key(preposition.text)}", "dependency_preposition", 0.08)
                for preposition in prepositions
            )
        elif token.pos_ == "ADP" and (
            token.head.pos_ == "VERB"
            or (
                token.head.dep_ == "ROOT"
                and token.head.pos_ in {"NOUN", "ADJ"}
                and any(child.dep_ in {"aux", "auxpass"} for child in token.head.children)
            )
        ):
            head = normalize_retrieval_key(token.head.lemma_ or token.head.text)
            if head and head not in auxiliaries:
                values.append((f"{head} {normalize_retrieval_key(token.text)}", "dependency_preposition", 0.08))
    unique: dict[str, tuple[str, str, float]] = {}
    for surface, evidence, penalty in values:
        if surface and (surface not in unique or penalty < unique[surface][2]):
            unique[surface] = (surface, evidence, penalty)
    return tuple(unique.values())[:MAX_RELATION_SURFACES]


def resolve_canonical_predicate(
    frame: object,
    lookup: Callable[..., list[CanonicalPredicateMatch]],
    *,
    cooperative_check: Callable[[], object] = lambda: None,
) -> CanonicalResolution:
    """Resolve Predicate identity from explicit IDs, syntax, labels, and synonyms."""
    current = validate_query_frame(frame)
    if not callable(lookup) or not callable(cooperative_check):
        raise InvalidRequestError("canonical Predicate resolver dependencies must be callable")
    relation = current["identity"]["relation"]
    if relation["canonical_id"]:
        return _explicit_resolution(
            ((relation["canonical_id"], relation["surface"], current["expected_object_type"]),),
            "caller_predicate_identity",
        )
    surfaces: list[tuple[str, str, float]] = []
    if relation["surface"]:
        surfaces.append((relation["surface"], "relation_surface", 0.0))
    surfaces.extend(dependency_predicate_surfaces(current["resolved_text"]))
    known = {normalize_retrieval_key(surface) for surface, _, _ in surfaces}
    for term in current["identity"]["lexical_terms"]:
        if term not in known:
            known.add(term)
            surfaces.append((term, "lexical_relation", 0.35))
    expected = current["expected_object_type"]
    scored: dict[str, tuple[float, str, ExpectedObjectType, set[str]]] = {}
    for surface, evidence, penalty in surfaces[:MAX_RELATION_SURFACES]:
        cooperative_check()
        normalized = normalize_retrieval_key(surface)
        rows = lookup(surface, limit=MAX_RELATION_CANDIDATES, cooperative_check=cooperative_check)
        if not isinstance(rows, list) or len(rows) > MAX_RELATION_CANDIDATES:
            raise InvalidRequestError("canonical Predicate lookup returned an invalid collection")
        for row in rows:
            label = normalize_retrieval_key(row["primary_label"])
            synonyms = {normalize_retrieval_key(value) for value in row["synonyms"]}
            canonical_id = normalize_retrieval_key(row["canonical_id"])
            base = 1.0 if normalized in {label, canonical_id} else 0.92 if normalized in synonyms else 0.0
            if not base:
                continue
            type_penalty = (
                0.12
                if expected != ExpectedObjectType.UNKNOWN
                and row["object_type"] != ExpectedObjectType.UNKNOWN
                and expected != row["object_type"]
                else 0.0
            )
            score = max(0.0, base - penalty - type_penalty)
            prior = scored.get(row["canonical_id"])
            evidence_values = {evidence, "predicate_label" if base == 1.0 else "predicate_synonym"}
            if prior and prior[0] >= score:
                prior[3].update(evidence_values)
            else:
                scored[row["canonical_id"]] = (score, row["primary_label"], row["object_type"], evidence_values)
    cooperative_check()
    return _select_scored(scored)


def one_hop_query_plan(
    subject: object,
    predicate: object,
    expected_object_type: object,
    *,
    max_rows: object = MAX_RELATION_PLAN_ROWS,
    template_id: object = RelationPlanTemplate.ONE_HOP_PROPOSITION_V1,
    schema_version: object = RELATION_CONTRACT_SCHEMA_VERSION,
) -> OneHopQueryPlan:
    """Compile only the fixed one-hop template; Cypher and procedures are not inputs."""
    entity = validate_canonical_resolution(subject)
    relation = validate_canonical_resolution(predicate)
    if entity["status"] != CanonicalResolutionStatus.SELECTED or relation["status"] != CanonicalResolutionStatus.SELECTED:
        raise InvalidRequestError("one-hop query plan requires selected entity and Predicate identities")
    if not isinstance(template_id, RelationPlanTemplate) or template_id != RelationPlanTemplate.ONE_HOP_PROPOSITION_V1:
        raise InvalidRequestError("one-hop query plan template is unsupported")
    if not isinstance(expected_object_type, ExpectedObjectType):
        raise InvalidRequestError("one-hop query plan expected object type is unsupported")
    if (
        isinstance(schema_version, bool)
        or not isinstance(schema_version, int)
        or schema_version != RELATION_CONTRACT_SCHEMA_VERSION
    ):
        raise InvalidRequestError("unsupported one-hop query plan schema_version")
    if isinstance(max_rows, bool) or not isinstance(max_rows, int) or not 1 <= max_rows <= MAX_RELATION_PLAN_ROWS:
        raise InvalidRequestError(f"one-hop query plan max_rows must be from 1 through {MAX_RELATION_PLAN_ROWS}")
    result: OneHopQueryPlan = {
        "schema_version": RELATION_CONTRACT_SCHEMA_VERSION,
        "template_id": RelationPlanTemplate.ONE_HOP_PROPOSITION_V1,
        "subject_entity_id": entity["canonical_id"],
        "predicate_id": relation["canonical_id"],
        "expected_object_type": expected_object_type,
        "max_rows": max_rows,
    }
    return result


def validate_one_hop_query_plan(value: object) -> OneHopQueryPlan:
    if not isinstance(value, Mapping) or set(value) != ONE_HOP_QUERY_PLAN_FIELDS:
        raise InvalidRequestError("OneHopQueryPlan has invalid fields")
    subject = canonical_resolution(
        CanonicalResolutionStatus.SELECTED,
        canonical_id=value["subject_entity_id"],
        primary_label=value["subject_entity_id"],
        candidate_ids=(value["subject_entity_id"],),
        evidence=("validated_plan",),
        score=1.0,
    )
    predicate = canonical_resolution(
        CanonicalResolutionStatus.SELECTED,
        canonical_id=value["predicate_id"],
        primary_label=value["predicate_id"],
        candidate_ids=(value["predicate_id"],),
        evidence=("validated_plan",),
        score=1.0,
    )
    return one_hop_query_plan(
        subject,
        predicate,
        value["expected_object_type"],
        max_rows=value["max_rows"],
        template_id=value["template_id"],
        schema_version=value["schema_version"],
    )


def object_type_match(expected: object, actual: object) -> tuple[float, bool]:
    """Return a concrete match value and an explicit availability flag."""
    if not isinstance(expected, ExpectedObjectType) or not isinstance(actual, ExpectedObjectType):
        raise InvalidRequestError("object type comparison requires ExpectedObjectType values")
    if ExpectedObjectType.UNKNOWN in {expected, actual}:
        result = (0.0, False)
        return result
    result = (float(expected == actual), True)
    return result


def _projection_interval(
    item: RelationPropositionProjection,
    axis: TemporalAxis,
) -> tuple[str, bool, str, bool]:
    projection = item["projection"]
    if axis == TemporalAxis.VALID_TIME:
        result = (
            projection["valid_from"],
            projection["valid_from_available"],
            projection["valid_to"],
            projection["valid_to_available"],
        )
        return result
    upper = projection["system_to"]
    upper_available = projection["system_to_available"]
    if projection["invalidated_at_available"] and (
        not upper_available
        or datetime.fromisoformat(projection["invalidated_at"][:-1] + "+00:00") < datetime.fromisoformat(upper[:-1] + "+00:00")
    ):
        upper = projection["invalidated_at"]
        upper_available = True
    result = (
        projection["system_from"],
        projection["system_from_available"],
        upper,
        upper_available,
    )
    return result


def _intervals_overlap(first: RelationPropositionProjection, second: RelationPropositionProjection, axis: TemporalAxis) -> bool:
    first_lower, first_lower_available, first_upper, first_upper_available = _projection_interval(first, axis)
    second_lower, second_lower_available, second_upper, second_upper_available = _projection_interval(second, axis)
    first_starts_before_second_ends = (
        not second_upper_available
        or not first_lower_available
        or datetime.fromisoformat(first_lower[:-1] + "+00:00") < datetime.fromisoformat(second_upper[:-1] + "+00:00")
    )
    second_starts_before_first_ends = (
        not first_upper_available
        or not second_lower_available
        or datetime.fromisoformat(second_lower[:-1] + "+00:00") < datetime.fromisoformat(first_upper[:-1] + "+00:00")
    )
    result = first_starts_before_second_ends and second_starts_before_first_ends
    return result


def _relation_selection(
    *,
    direct_answer: bool,
    selected_proposition_id: str,
    evidence_proposition_ids: tuple[str, ...],
    conflict_proposition_ids: tuple[str, ...],
    ranking_proposition_ids: tuple[str, ...],
    reason: RelationSelectionReason,
    cardinality: PredicateCardinality,
    trust_version: int = 0,
    trust_version_available: bool = False,
) -> RelationPropositionSelection:
    result: RelationPropositionSelection = {
        "direct_answer": direct_answer,
        "selected_proposition_id": selected_proposition_id,
        "selected_proposition_id_available": bool(selected_proposition_id),
        "evidence_proposition_ids": evidence_proposition_ids,
        "conflict_proposition_ids": conflict_proposition_ids,
        "ranking_proposition_ids": ranking_proposition_ids,
        "reason": reason,
        "cardinality": cardinality,
        "trust_version": trust_version,
        "trust_version_available": trust_version_available,
    }
    return result


def select_relation_propositions(items: object, temporal_query: object) -> RelationPropositionSelection:
    """Select one direct one-hop Proposition or preserve bounded evidence conservatively."""
    if not isinstance(items, tuple) or len(items) > MAX_RELATION_PLAN_ROWS:
        raise InvalidRequestError(f"relation selection items must be a tuple of at most {MAX_RELATION_PLAN_ROWS} values")
    validated = tuple(validate_relation_proposition_projection(item) for item in items)
    temporal: TemporalQuery = validate_temporal_query(temporal_query)
    evidence_proposition_ids = tuple(sorted(item["projection"]["proposition_id"] for item in validated))
    if not validated:
        result = _relation_selection(
            direct_answer=False,
            selected_proposition_id="",
            evidence_proposition_ids=(),
            conflict_proposition_ids=(),
            ranking_proposition_ids=(),
            reason=RelationSelectionReason.NO_ELIGIBLE_PROPOSITION,
            cardinality=PredicateCardinality.UNKNOWN,
        )
        return result

    cardinalities = {item["predicate_cardinality"] for item in validated}
    cardinality = next(iter(cardinalities)) if len(cardinalities) == 1 else PredicateCardinality.UNKNOWN
    considered = validated
    if temporal["operator"] == TemporalQueryOperator.LATEST:
        lower_values = []
        for item in validated:
            lower, lower_available, _upper, _upper_available = _projection_interval(item, temporal["axis"])
            if not lower_available:
                ranking_proposition_ids = tuple(sorted(evidence_proposition_ids))
                result = _relation_selection(
                    direct_answer=False,
                    selected_proposition_id="",
                    evidence_proposition_ids=evidence_proposition_ids,
                    conflict_proposition_ids=(),
                    ranking_proposition_ids=ranking_proposition_ids,
                    reason=RelationSelectionReason.LATEST_BOUND_UNAVAILABLE,
                    cardinality=cardinality,
                )
                return result
            lower_values.append((datetime.fromisoformat(lower[:-1] + "+00:00"), item))
        latest = max(value for value, _item in lower_values)
        considered = tuple(item for value, item in lower_values if value == latest)

    ranking_proposition_ids = tuple(
        item["projection"]["proposition_id"]
        for item in sorted(
            validated,
            key=lambda item: (
                not item["projection"]["supplied_trust_available"],
                -item["projection"]["supplied_trust"],
                item["projection"]["proposition_id"],
            ),
        )
    )
    objects = {item["projection"]["object_entity_id"] for item in considered}
    conflict_ids = set()
    for index, first in enumerate(considered):
        for second in considered[index + 1 :]:
            if first["projection"]["object_entity_id"] != second["projection"]["object_entity_id"] and _intervals_overlap(
                first, second, temporal["axis"]
            ):
                conflict_ids.add(first["projection"]["proposition_id"])
                conflict_ids.add(second["projection"]["proposition_id"])
    normalized_conflict_ids = tuple(sorted(conflict_ids))
    if len(objects) > 1:
        if cardinality == PredicateCardinality.MULTI:
            reason = RelationSelectionReason.VALID_MULTI_VALUE
            normalized_conflict_ids = ()
        elif temporal["operator"] == TemporalQueryOperator.LATEST and len(considered) > 1:
            reason = RelationSelectionReason.LATEST_TIE
            if cardinality == PredicateCardinality.UNKNOWN:
                normalized_conflict_ids = ()
        elif cardinality == PredicateCardinality.SINGLE and normalized_conflict_ids:
            reason = RelationSelectionReason.CONFLICT_SINGLE_VALUE
        elif normalized_conflict_ids:
            reason = RelationSelectionReason.CARDINALITY_UNKNOWN
            normalized_conflict_ids = ()
        else:
            reason = RelationSelectionReason.BOUNDED_MULTIPLE_PERIODS
        result = _relation_selection(
            direct_answer=False,
            selected_proposition_id="",
            evidence_proposition_ids=evidence_proposition_ids,
            conflict_proposition_ids=normalized_conflict_ids,
            ranking_proposition_ids=ranking_proposition_ids,
            reason=reason,
            cardinality=cardinality,
        )
        return result

    explicit_historical = temporal["operator"] not in {
        TemporalQueryOperator.UNSPECIFIED,
        TemporalQueryOperator.CURRENT,
        TemporalQueryOperator.NOW,
        TemporalQueryOperator.LATEST,
    }
    if explicit_historical:
        has_open_bounds = any(
            not lower_available or not upper_available
            for lower, lower_available, upper, upper_available in (
                _projection_interval(item, temporal["axis"]) for item in considered
            )
        )
        if has_open_bounds:
            result = _relation_selection(
                direct_answer=False,
                selected_proposition_id="",
                evidence_proposition_ids=evidence_proposition_ids,
                conflict_proposition_ids=(),
                ranking_proposition_ids=ranking_proposition_ids,
                reason=RelationSelectionReason.TEMPORAL_BOUNDS_OPEN,
                cardinality=cardinality,
            )
            return result

    if any(not item["projection"]["supplied_trust_available"] for item in considered):
        result = _relation_selection(
            direct_answer=False,
            selected_proposition_id="",
            evidence_proposition_ids=evidence_proposition_ids,
            conflict_proposition_ids=(),
            ranking_proposition_ids=ranking_proposition_ids,
            reason=RelationSelectionReason.TRUST_UNAVAILABLE,
            cardinality=cardinality,
        )
        return result
    trust_versions = {item["projection"]["supplied_trust_version"] for item in considered}
    if len(trust_versions) != 1:
        result = _relation_selection(
            direct_answer=False,
            selected_proposition_id="",
            evidence_proposition_ids=evidence_proposition_ids,
            conflict_proposition_ids=(),
            ranking_proposition_ids=ranking_proposition_ids,
            reason=RelationSelectionReason.TRUST_VERSION_INCOMPARABLE,
            cardinality=cardinality,
        )
        return result
    trust_version = next(iter(trust_versions))
    ranked_considered = sorted(
        considered,
        key=lambda item: (-item["projection"]["supplied_trust"], item["projection"]["proposition_id"]),
    )
    selected_proposition_id = ranked_considered[0]["projection"]["proposition_id"]
    unique_trust_leader = len(ranked_considered) > 1 and (
        ranked_considered[0]["projection"]["supplied_trust"] > ranked_considered[1]["projection"]["supplied_trust"]
    )
    if temporal["operator"] == TemporalQueryOperator.LATEST:
        reason = RelationSelectionReason.SELECTED_LATEST
    elif unique_trust_leader:
        reason = RelationSelectionReason.SELECTED_TRUST_RANKED
    else:
        reason = RelationSelectionReason.SELECTED_UNIQUE
    result = _relation_selection(
        direct_answer=True,
        selected_proposition_id=selected_proposition_id,
        evidence_proposition_ids=evidence_proposition_ids,
        conflict_proposition_ids=(),
        ranking_proposition_ids=ranking_proposition_ids,
        reason=reason,
        cardinality=cardinality,
        trust_version=trust_version,
        trust_version_available=True,
    )
    return result


def phrase_relation_result(subject_label: object, predicate_label: object, object_label: object) -> str:
    """Produce the sole bounded one-hop phrasing form from validated labels."""
    subject = _text(subject_label, "relation subject label")
    predicate = _text(predicate_label, "relation Predicate label")
    value = _text(object_label, "relation object label")
    return f"{subject} — {predicate}: {value}."
