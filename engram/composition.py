"""Closed contracts and bounded execution for one- and two-hop graph composition."""

import json
import math
import re
from collections.abc import Callable, Mapping
from datetime import datetime

from engram.constants import (
    COMPOSITION_CONTRACT_SCHEMA_VERSION,
    COMPOSITION_PLAN_FIELDS,
    COMPOSITION_STEP_FIELDS,
    MAX_COMPOSITION_BINDING_BYTES,
    MAX_COMPOSITION_BRANCHES,
    MAX_COMPOSITION_CANDIDATES_PER_STEP,
    MAX_COMPOSITION_HOPS,
    MAX_COMPOSITION_PATH_PROPOSITIONS,
    MAX_COMPOSITION_ROWS,
    CanonicalResolutionStatus,
    CompositionReason,
    ExpectedObjectType,
    GraphCompositionOperator,
    PredicateCardinality,
    QueryOperator,
)
from engram.errors import InvalidRequestError
from engram.evidence import PropositionEligibilityDecision, validate_proposition_eligibility_decision
from engram.graph import PropositionProjection, RelationPropositionProjection, validate_relation_proposition_projection
from engram.relation import CanonicalResolution, canonical_resolution, validate_canonical_resolution
from engram.resolution import validate_query_frame

CompositionStep = dict


CompositionPlan = dict


def _text(value: object, name: str, maximum: int = 256, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str) or (not allow_empty and not value):
        raise InvalidRequestError(f"{name} must be a {'possibly empty ' if allow_empty else 'non-empty '}string")
    if len(value.encode("utf-8")) > maximum:
        raise InvalidRequestError(f"{name} exceeds {maximum} UTF-8 bytes")
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise InvalidRequestError(f"{name} contains a control character")
    return value


def _identifier(value: object, name: str, *, allow_empty: bool = False) -> str:
    result = _text(value, name, allow_empty=allow_empty)
    if result and any(character.isspace() for character in result):
        raise InvalidRequestError(f"{name} must not contain whitespace")
    return result


def _binding(value: object, name: str) -> str:
    result = _identifier(value, name)
    if len(result.encode("utf-8")) > MAX_COMPOSITION_BINDING_BYTES or not result.startswith("$") or len(result) == 1:
        raise InvalidRequestError(f"{name} must be a bounded $ binding")
    return result


def _integer(value: object, name: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise InvalidRequestError(f"{name} must be an integer from {minimum} through {maximum}")
    return value


def composition_step(
    branch: object,
    hop: object,
    subject_binding: object,
    subject_entity_id: object,
    predicate_id: object,
    predicate_label: object,
    object_binding: object,
    expected_object_type: object,
    max_candidates: object = MAX_COMPOSITION_CANDIDATES_PER_STEP,
    schema_version: object = COMPOSITION_CONTRACT_SCHEMA_VERSION,
) -> CompositionStep:
    """Build one fixed-predicate linear binding step."""
    version = _integer(schema_version, "composition step schema_version", 1, COMPOSITION_CONTRACT_SCHEMA_VERSION)
    if not isinstance(expected_object_type, ExpectedObjectType):
        raise InvalidRequestError("composition step expected_object_type is unsupported")
    result: CompositionStep = {
        "schema_version": version,
        "branch": _integer(branch, "composition step branch", 0, MAX_COMPOSITION_BRANCHES - 1),
        "hop": _integer(hop, "composition step hop", 0, MAX_COMPOSITION_HOPS - 1),
        "subject_binding": _binding(subject_binding, "composition step subject_binding"),
        "subject_entity_id": _identifier(
            subject_entity_id,
            "composition step subject_entity_id",
            allow_empty=True,
        ),
        "predicate_id": _identifier(predicate_id, "composition step predicate_id"),
        "predicate_label": _text(predicate_label, "composition step predicate_label"),
        "object_binding": _binding(object_binding, "composition step object_binding"),
        "expected_object_type": expected_object_type,
        "max_candidates": _integer(
            max_candidates,
            "composition step max_candidates",
            1,
            MAX_COMPOSITION_CANDIDATES_PER_STEP,
        ),
    }
    if result["subject_binding"] == result["object_binding"]:
        raise InvalidRequestError("composition step input and output bindings must differ")
    return result


def validate_composition_step(value: object) -> CompositionStep:
    if not isinstance(value, Mapping) or set(value) != COMPOSITION_STEP_FIELDS:
        raise InvalidRequestError("CompositionStep has invalid fields")
    return composition_step(
        value["branch"],
        value["hop"],
        value["subject_binding"],
        value["subject_entity_id"],
        value["predicate_id"],
        value["predicate_label"],
        value["object_binding"],
        value["expected_object_type"],
        value["max_candidates"],
        value["schema_version"],
    )


def composition_step_to_dict(value: object) -> dict[str, object]:
    step = validate_composition_step(value)
    result: dict[str, object] = dict(step)
    result["expected_object_type"] = step["expected_object_type"].value
    return result


def composition_step_from_dict(value: object) -> CompositionStep:
    if not isinstance(value, Mapping) or set(value) != COMPOSITION_STEP_FIELDS:
        raise InvalidRequestError("CompositionStep has invalid fields")
    try:
        expected = ExpectedObjectType(_text(value["expected_object_type"], "composition step expected_object_type", 16))
    except ValueError as error:
        raise InvalidRequestError("composition step expected_object_type is unsupported") from error
    return composition_step(
        value["branch"],
        value["hop"],
        value["subject_binding"],
        value["subject_entity_id"],
        value["predicate_id"],
        value["predicate_label"],
        value["object_binding"],
        expected,
        value["max_candidates"],
        value["schema_version"],
    )


def composition_plan(
    operator: object,
    root_entity_id: object,
    root_label: object,
    steps: object,
    terminal_binding: object,
    *,
    aggregation_inputs: object = (),
    descending: object = False,
    max_hops: object = MAX_COMPOSITION_HOPS,
    max_rows: object = MAX_COMPOSITION_ROWS,
    max_branches: object = MAX_COMPOSITION_BRANCHES,
    max_candidates_per_step: object = MAX_COMPOSITION_CANDIDATES_PER_STEP,
    max_path_propositions: object = MAX_COMPOSITION_PATH_PROPOSITIONS,
    schema_version: object = COMPOSITION_CONTRACT_SCHEMA_VERSION,
) -> CompositionPlan:
    """Build one closed graph plan and reject underconstrained or cyclic bindings."""
    version = _integer(schema_version, "composition plan schema_version", 1, COMPOSITION_CONTRACT_SCHEMA_VERSION)
    if not isinstance(operator, GraphCompositionOperator):
        raise InvalidRequestError("composition plan operator is unsupported")
    if not isinstance(steps, tuple) or not steps:
        raise InvalidRequestError("composition plan steps must be a non-empty tuple")
    normalized_steps = tuple(validate_composition_step(value) for value in steps)
    if normalized_steps != tuple(sorted(normalized_steps, key=lambda value: (value["branch"], value["hop"]))):
        raise InvalidRequestError("composition plan steps must be ordered by branch and hop")
    hop_limit = _integer(max_hops, "composition plan max_hops", 1, MAX_COMPOSITION_HOPS)
    row_limit = _integer(max_rows, "composition plan max_rows", 1, MAX_COMPOSITION_ROWS)
    branch_limit = _integer(max_branches, "composition plan max_branches", 1, MAX_COMPOSITION_BRANCHES)
    candidate_limit = _integer(
        max_candidates_per_step,
        "composition plan max_candidates_per_step",
        1,
        MAX_COMPOSITION_CANDIDATES_PER_STEP,
    )
    path_limit = _integer(max_path_propositions, "composition plan max_path_propositions", 1, MAX_COMPOSITION_PATH_PROPOSITIONS)
    branches = tuple(sorted({step["branch"] for step in normalized_steps}))
    if branches != tuple(range(len(branches))) or len(branches) > branch_limit:
        raise InvalidRequestError("composition plan branches must be contiguous and bounded")
    if (
        operator
        in {
            GraphCompositionOperator.LOOKUP,
            GraphCompositionOperator.EXISTS,
            GraphCompositionOperator.COUNT,
            GraphCompositionOperator.NOT,
            GraphCompositionOperator.MIN,
            GraphCompositionOperator.MAX,
            GraphCompositionOperator.ORDER,
        }
        and len(branches) != 1
    ):
        raise InvalidRequestError("composition plan operator requires exactly one branch")
    if operator in {GraphCompositionOperator.AND, GraphCompositionOperator.OR} and len(branches) < 2:
        raise InvalidRequestError("composition Boolean operator requires at least two branches")

    terminal = _binding(terminal_binding, "composition plan terminal_binding")
    root_id = _identifier(root_entity_id, "composition plan root_entity_id")
    for branch in branches:
        branch_steps = tuple(step for step in normalized_steps if step["branch"] == branch)
        if len(branch_steps) > hop_limit or len(branch_steps) > path_limit:
            raise InvalidRequestError("composition plan branch exceeds its hop or path limit")
        if tuple(step["hop"] for step in branch_steps) != tuple(range(len(branch_steps))):
            raise InvalidRequestError("composition plan branch hops must be contiguous")
        seen_bindings = {"$root"}
        previous_binding = "$root"
        for index, step in enumerate(branch_steps):
            if step["subject_binding"] != previous_binding:
                raise InvalidRequestError("composition plan contains an unconstrained or cartesian binding")
            if index == 0 and step["subject_entity_id"] != root_id:
                raise InvalidRequestError("composition plan root step must bind the selected root entity")
            if index and step["subject_entity_id"]:
                raise InvalidRequestError("composition plan intermediate subject must come only from its prior binding")
            if step["object_binding"] in seen_bindings:
                raise InvalidRequestError("composition plan contains a binding cycle")
            if step["max_candidates"] > candidate_limit:
                raise InvalidRequestError("composition step exceeds the plan candidate limit")
            seen_bindings.add(step["object_binding"])
            previous_binding = step["object_binding"]
        if previous_binding != terminal:
            raise InvalidRequestError("composition plan terminal_binding must be produced by every branch")

    if not isinstance(aggregation_inputs, tuple):
        raise InvalidRequestError("composition plan aggregation_inputs must be a tuple")
    normalized_aggregation = tuple(_binding(value, "composition plan aggregation input") for value in aggregation_inputs)
    if normalized_aggregation != tuple(sorted(set(normalized_aggregation))):
        raise InvalidRequestError("composition plan aggregation_inputs must be unique and sorted")
    aggregate_operators = {
        GraphCompositionOperator.COUNT,
        GraphCompositionOperator.MIN,
        GraphCompositionOperator.MAX,
        GraphCompositionOperator.ORDER,
    }
    if operator in aggregate_operators:
        if normalized_aggregation != (terminal,):
            raise InvalidRequestError("composition aggregate must consume exactly the terminal binding")
    elif normalized_aggregation:
        raise InvalidRequestError("composition non-aggregate operator cannot carry aggregation inputs")
    if not isinstance(descending, bool):
        raise InvalidRequestError("composition plan descending must be a boolean")
    if operator != GraphCompositionOperator.ORDER and descending:
        raise InvalidRequestError("composition descending is supported only for ORDER")
    final_types = {step["expected_object_type"] for step in normalized_steps if step["object_binding"] == terminal}
    if operator in {
        GraphCompositionOperator.MIN,
        GraphCompositionOperator.MAX,
        GraphCompositionOperator.ORDER,
    } and not final_types.issubset({ExpectedObjectType.NUMBER, ExpectedObjectType.DATE}):
        raise InvalidRequestError("composition ordered aggregates require NUMBER or DATE terminal values")
    result: CompositionPlan = {
        "schema_version": version,
        "operator": operator,
        "root_entity_id": root_id,
        "root_label": _text(root_label, "composition plan root_label"),
        "steps": normalized_steps,
        "terminal_binding": terminal,
        "aggregation_inputs": normalized_aggregation,
        "descending": descending,
        "max_hops": hop_limit,
        "max_rows": row_limit,
        "max_branches": branch_limit,
        "max_candidates_per_step": candidate_limit,
        "max_path_propositions": path_limit,
    }
    return result


def validate_composition_plan(value: object) -> CompositionPlan:
    if not isinstance(value, Mapping) or set(value) != COMPOSITION_PLAN_FIELDS:
        raise InvalidRequestError("CompositionPlan has invalid fields")
    return composition_plan(
        value["operator"],
        value["root_entity_id"],
        value["root_label"],
        value["steps"],
        value["terminal_binding"],
        aggregation_inputs=value["aggregation_inputs"],
        descending=value["descending"],
        max_hops=value["max_hops"],
        max_rows=value["max_rows"],
        max_branches=value["max_branches"],
        max_candidates_per_step=value["max_candidates_per_step"],
        max_path_propositions=value["max_path_propositions"],
        schema_version=value["schema_version"],
    )


def composition_plan_to_dict(value: object) -> dict[str, object]:
    plan = validate_composition_plan(value)
    return {
        "schema_version": plan["schema_version"],
        "operator": plan["operator"].value,
        "root_entity_id": plan["root_entity_id"],
        "root_label": plan["root_label"],
        "steps": [composition_step_to_dict(step) for step in plan["steps"]],
        "terminal_binding": plan["terminal_binding"],
        "aggregation_inputs": list(plan["aggregation_inputs"]),
        "descending": plan["descending"],
        "max_hops": plan["max_hops"],
        "max_rows": plan["max_rows"],
        "max_branches": plan["max_branches"],
        "max_candidates_per_step": plan["max_candidates_per_step"],
        "max_path_propositions": plan["max_path_propositions"],
    }


def composition_plan_from_dict(value: object) -> CompositionPlan:
    if not isinstance(value, Mapping) or set(value) != COMPOSITION_PLAN_FIELDS:
        raise InvalidRequestError("CompositionPlan has invalid fields")
    try:
        operator = GraphCompositionOperator(_text(value["operator"], "composition plan operator", 16))
    except ValueError as error:
        raise InvalidRequestError("composition plan operator is unsupported") from error
    raw_steps = value["steps"]
    raw_aggregation = value["aggregation_inputs"]
    if not isinstance(raw_steps, list) or not isinstance(raw_aggregation, list):
        raise InvalidRequestError("serialized composition plan collections must be lists")
    return composition_plan(
        operator,
        value["root_entity_id"],
        value["root_label"],
        tuple(composition_step_from_dict(step) for step in raw_steps),
        value["terminal_binding"],
        aggregation_inputs=tuple(raw_aggregation),
        descending=value["descending"],
        max_hops=value["max_hops"],
        max_rows=value["max_rows"],
        max_branches=value["max_branches"],
        max_candidates_per_step=value["max_candidates_per_step"],
        max_path_propositions=value["max_path_propositions"],
        schema_version=value["schema_version"],
    )


def composition_plan_to_json(value: object) -> str:
    return json.dumps(composition_plan_to_dict(value), sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def composition_plan_from_json(value: object) -> CompositionPlan:
    if not isinstance(value, str):
        raise InvalidRequestError("CompositionPlan JSON must be a string")
    try:
        data = json.loads(value)
    except json.JSONDecodeError as error:
        raise InvalidRequestError("CompositionPlan JSON is invalid") from error
    return composition_plan_from_dict(data)


def graph_composition_operator(frame: object) -> GraphCompositionOperator:
    """Map one query frame to the closed graph algebra without guessing unsupported intent."""
    current = validate_query_frame(frame)
    operator = current["identity"]["operator"]
    normalized = current["resolved_text"].casefold()
    if operator in {QueryOperator.HOW_MANY, QueryOperator.COUNT}:
        return GraphCompositionOperator.COUNT
    if operator == QueryOperator.EXISTS:
        return GraphCompositionOperator.EXISTS
    if operator == QueryOperator.COMPARE:
        if any(value in normalized for value in ("minimum", "earliest", "smallest", "lowest")):
            return GraphCompositionOperator.MIN
        if any(value in normalized for value in ("maximum", "latest", "largest", "highest")):
            return GraphCompositionOperator.MAX
        if any(value in normalized for value in ("order", "ordered", "sort", "sorted")):
            return GraphCompositionOperator.ORDER
        raise InvalidRequestError(CompositionReason.UNSUPPORTED_QUERY.value)
    if operator in {
        QueryOperator.WHO,
        QueryOperator.WHAT,
        QueryOperator.WHERE,
        QueryOperator.WHEN,
        QueryOperator.WHICH,
        QueryOperator.LOOKUP,
    }:
        return GraphCompositionOperator.LOOKUP
    raise InvalidRequestError(CompositionReason.UNSUPPORTED_QUERY.value)


def linear_composition_plan(
    subject: object,
    predicates: object,
    operator: object,
    expected_object_type: object,
    *,
    max_rows: int = MAX_COMPOSITION_ROWS,
    max_candidates_per_step: int = MAX_COMPOSITION_CANDIDATES_PER_STEP,
) -> CompositionPlan:
    """Compile selected canonical identities into one bounded linear plan."""
    root = validate_canonical_resolution(subject)
    if not isinstance(predicates, tuple) or not 1 <= len(predicates) <= MAX_COMPOSITION_HOPS:
        raise InvalidRequestError("linear composition requires one or two selected Predicates")
    selected = tuple(validate_canonical_resolution(value) for value in predicates)
    if not all(value["canonical_id"] and value["primary_label"] for value in (root, *selected)):
        raise InvalidRequestError(CompositionReason.IDENTITY_MISS.value)
    if not isinstance(operator, GraphCompositionOperator) or not isinstance(expected_object_type, ExpectedObjectType):
        raise InvalidRequestError("linear composition operator or expected type is unsupported")
    steps = []
    prior_binding = "$root"
    for hop, predicate in enumerate(selected):
        output_binding = "$result" if hop == len(selected) - 1 else f"$hop{hop + 1}"
        step_type = expected_object_type if hop == len(selected) - 1 else predicate["object_type"]
        steps.append(
            composition_step(
                0,
                hop,
                prior_binding,
                root["canonical_id"] if hop == 0 else "",
                predicate["canonical_id"],
                predicate["primary_label"],
                output_binding,
                step_type,
                max_candidates_per_step,
            )
        )
        prior_binding = output_binding
    aggregation = (
        ("$result",)
        if operator
        in {
            GraphCompositionOperator.COUNT,
            GraphCompositionOperator.MIN,
            GraphCompositionOperator.MAX,
            GraphCompositionOperator.ORDER,
        }
        else ()
    )
    return composition_plan(
        operator,
        root["canonical_id"],
        root["primary_label"],
        tuple(steps),
        "$result",
        aggregation_inputs=aggregation,
        max_rows=max_rows,
        max_candidates_per_step=max_candidates_per_step,
    )


_TOKEN = re.compile(r"[A-Za-z0-9]+(?:[-_][A-Za-z0-9]+)*")
_PREDICATE_STOP_WORDS = {
    "a",
    "an",
    "and",
    "are",
    "as",
    "at",
    "be",
    "did",
    "do",
    "does",
    "for",
    "from",
    "how",
    "in",
    "is",
    "of",
    "or",
    "the",
    "to",
    "was",
    "were",
    "what",
    "when",
    "where",
    "which",
    "who",
    "whose",
}


def bounded_predicate_surfaces(text: object, excluded_words: object = ()) -> tuple[tuple[int, str], ...]:
    """Return deterministic one- to three-token Predicate lookup surfaces."""
    request = _text(text, "composition request", 16_384)
    if not isinstance(excluded_words, tuple):
        raise InvalidRequestError("composition excluded_words must be a tuple")
    excluded = {str(value).casefold() for value in excluded_words}
    tokens = [(match.start(), match.group(0).casefold()) for match in _TOKEN.finditer(request)]
    values = []
    for index, (position, token) in enumerate(tokens):
        if token in _PREDICATE_STOP_WORDS or token in excluded:
            continue
        for width in (3, 2, 1):
            phrase_tokens = tokens[index : index + width]
            if len(phrase_tokens) != width:
                continue
            words = tuple(value for _, value in phrase_tokens)
            if any(value in excluded for value in words) or all(value in _PREDICATE_STOP_WORDS for value in words):
                continue
            values.append((position, " ".join(words)))
    unique = []
    seen: set[tuple[int, str]] = set()
    for value in values:
        if value not in seen:
            seen.add(value)
            unique.append(value)
    return tuple(unique[:12])


def resolve_composition_predicates(
    frame: object,
    root: object,
    lookup: Callable[..., list],
    cooperative_check: Callable[[], object] = lambda: None,
) -> tuple[CanonicalResolution, ...]:
    """Resolve exactly two ordered Predicate identities for a supported composed frame."""
    current = validate_query_frame(frame)
    subject = validate_canonical_resolution(root)
    if not callable(lookup) or not callable(cooperative_check):
        raise InvalidRequestError("composition Predicate resolver dependencies must be callable")
    excluded = tuple(value.casefold() for value in _TOKEN.findall(subject["primary_label"]))
    matches: dict[tuple[str, int], tuple[str, ExpectedObjectType, str]] = {}
    for position, surface in bounded_predicate_surfaces(current["resolved_text"], excluded):
        cooperative_check()
        rows = lookup(surface, limit=2, cooperative_check=cooperative_check)
        if not isinstance(rows, list) or len(rows) > 2:
            raise InvalidRequestError("composition Predicate lookup returned an invalid collection")
        if len({row["canonical_id"] for row in rows}) > 1:
            raise InvalidRequestError(CompositionReason.IDENTITY_AMBIGUOUS.value)
        for row in rows:
            canonical_id = _identifier(row["canonical_id"], "composition Predicate canonical_id")
            label = _text(row["primary_label"], "composition Predicate primary_label")
            object_type = row["object_type"]
            if not isinstance(object_type, ExpectedObjectType):
                raise InvalidRequestError("composition Predicate object_type is unsupported")
            match_key = (canonical_id, position)
            prior = matches.get(match_key)
            candidate = (label, object_type, surface)
            if prior is not None and prior[:2] != candidate[:2]:
                raise InvalidRequestError(CompositionReason.IDENTITY_AMBIGUOUS.value)
            if prior is None or surface < prior[2]:
                matches[match_key] = candidate
        if len(matches) > MAX_COMPOSITION_HOPS:
            raise InvalidRequestError(CompositionReason.IDENTITY_AMBIGUOUS.value)
    cooperative_check()
    if len(matches) != MAX_COMPOSITION_HOPS:
        raise InvalidRequestError(CompositionReason.IDENTITY_MISS.value)
    ordered = sorted(matches.items(), key=lambda value: (value[0][1], value[0][0]))
    result = tuple(
        canonical_resolution(
            status=CanonicalResolutionStatus.SELECTED,
            canonical_id=match_key[0],
            primary_label=value[0],
            object_type=value[1],
            score=1.0,
            candidate_ids=(match_key[0],),
            evidence=("composition_surface_match",),
        )
        for match_key, value in ordered
    )
    return result


def _typed_order_value(label: str, object_type: ExpectedObjectType) -> float:
    if object_type == ExpectedObjectType.NUMBER:
        try:
            value = float(label.replace(",", ""))
        except ValueError as error:
            raise InvalidRequestError(CompositionReason.TYPE_MISMATCH.value) from error
        if not math.isfinite(value):
            raise InvalidRequestError(CompositionReason.TYPE_MISMATCH.value)
        return value
    if object_type == ExpectedObjectType.DATE:
        try:
            return datetime.fromisoformat(label.replace("Z", "+00:00")).timestamp()
        except ValueError as error:
            raise InvalidRequestError(CompositionReason.TYPE_MISMATCH.value) from error
    raise InvalidRequestError(CompositionReason.TYPE_UNAVAILABLE.value)


CompositionPathEntry = dict


CompositionPath = tuple[CompositionPathEntry, ...]


CompositionExecution = dict


_TraversalState = dict


def _path_key(path: CompositionPath) -> tuple[str, ...]:
    return tuple(entry["proposition"]["projection"]["proposition_id"] for entry in path)


def _ordered_paths(paths: list[CompositionPath]) -> tuple[CompositionPath, ...]:
    by_key = {_path_key(path): path for path in paths}
    return tuple(by_key[key] for key in sorted(by_key))


def execute_composition_plan(
    plan: object,
    query: Callable[[str, str, int], list[RelationPropositionProjection]],
    evaluate: Callable[[PropositionProjection], PropositionEligibilityDecision],
    revalidate: Callable[[PropositionProjection], PropositionEligibilityDecision],
    cooperative_check: Callable[[], object] = lambda: None,
) -> CompositionExecution:
    """Execute fixed one-hop capabilities sequentially under declared non-time bounds."""
    current = validate_composition_plan(plan)
    if not all(callable(value) for value in (query, evaluate, revalidate, cooperative_check)):
        raise InvalidRequestError("composition execution dependencies must be callable")
    graph_rows = 0
    truncated = False
    reasons: set[CompositionReason] = set()
    complete_paths: list[CompositionPath] = []
    partial_paths: list[CompositionPath] = []
    branches = tuple(sorted({step["branch"] for step in current["steps"]}))
    branch_complete: list[bool] = []
    branch_known: list[bool] = []

    for branch in branches:
        cooperative_check()
        states: list[_TraversalState] = [
            {
                "entity_id": current["root_entity_id"],
                "entity_label": current["root_label"],
                "entity_type": ExpectedObjectType.ENTITY,
                "entity_history": (current["root_entity_id"],),
                "path": (),
            }
        ]
        branch_steps = tuple(step for step in current["steps"] if step["branch"] == branch)
        branch_truncated = False
        branch_failed = False
        for step in branch_steps:
            next_states: list[_TraversalState] = []
            for state in states:
                cooperative_check()
                state_advanced = False
                remaining = current["max_rows"] - graph_rows
                if remaining <= 0:
                    truncated = True
                    branch_truncated = True
                    reasons.add(CompositionReason.ROW_LIMIT)
                    if state["path"]:
                        partial_paths.append(state["path"])
                    continue
                sentinel_limit = step["max_candidates"] + 1
                query_limit = min(sentinel_limit, remaining)
                try:
                    raw_rows = query(state["entity_id"], step["predicate_id"], query_limit)
                except Exception:
                    branch_failed = True
                    reasons.add(CompositionReason.DEPENDENCY_FAILED)
                    if state["path"]:
                        partial_paths.append(state["path"])
                    continue
                if not isinstance(raw_rows, list) or len(raw_rows) > query_limit:
                    raise InvalidRequestError("composition query returned an invalid collection")
                graph_rows += len(raw_rows)
                rows = [validate_relation_proposition_projection(value) for value in raw_rows]
                rows.sort(key=lambda value: value["projection"]["proposition_id"])
                if len(rows) == sentinel_limit:
                    truncated = True
                    branch_truncated = True
                    reasons.add(CompositionReason.CANDIDATE_LIMIT)
                    rows = rows[: step["max_candidates"]]
                elif query_limit < sentinel_limit and len(rows) == query_limit:
                    truncated = True
                    branch_truncated = True
                    reasons.add(CompositionReason.ROW_LIMIT)
                for item in rows:
                    cooperative_check()
                    projection = item["projection"]
                    if projection["subject_entity_id"] != state["entity_id"] or projection["predicate_id"] != step["predicate_id"]:
                        raise InvalidRequestError("composition query returned a Proposition outside the requested binding")
                    initial = validate_proposition_eligibility_decision(evaluate(projection))
                    if not initial["eligible"]:
                        continue
                    if graph_rows >= current["max_rows"]:
                        truncated = True
                        branch_truncated = True
                        reasons.add(CompositionReason.ROW_LIMIT)
                        if state["path"]:
                            partial_paths.append(state["path"])
                        break
                    decision = validate_proposition_eligibility_decision(revalidate(projection))
                    graph_rows += 1
                    if not decision["eligible"] or not decision["revalidated"]:
                        continue
                    expected = step["expected_object_type"]
                    if expected != ExpectedObjectType.UNKNOWN and item["object_type"] != expected:
                        reasons.add(
                            CompositionReason.TYPE_UNAVAILABLE
                            if item["object_type"] == ExpectedObjectType.UNKNOWN
                            else CompositionReason.TYPE_MISMATCH
                        )
                        continue
                    object_id = projection["object_entity_id"]
                    if object_id in state["entity_history"]:
                        reasons.add(CompositionReason.CYCLE)
                        if state["path"]:
                            partial_paths.append(state["path"])
                        continue
                    entry: CompositionPathEntry = {
                        "step": step,
                        "proposition": item,
                        "decision": decision,
                    }
                    path = (*state["path"], entry)
                    if len(path) > current["max_path_propositions"]:
                        truncated = True
                        branch_truncated = True
                        reasons.add(CompositionReason.PATH_LIMIT)
                        partial_paths.append(state["path"])
                        continue
                    next_states.append(
                        {
                            "entity_id": object_id,
                            "entity_label": item["object_label"],
                            "entity_type": item["object_type"],
                            "entity_history": (*state["entity_history"], object_id),
                            "path": path,
                        }
                    )
                    state_advanced = True
                if not state_advanced and state["path"]:
                    partial_paths.append(state["path"])
            states = next_states
            if not states:
                break
            if len(states) > current["max_branches"] * current["max_candidates_per_step"]:
                truncated = True
                branch_truncated = True
                reasons.add(CompositionReason.BRANCH_LIMIT)
                states = sorted(states, key=lambda value: _path_key(value["path"]))[
                    : current["max_branches"] * current["max_candidates_per_step"]
                ]
        completed = [state["path"] for state in states if len(state["path"]) == len(branch_steps)]
        complete_paths.extend(completed)
        branch_complete.append(bool(completed))
        branch_known.append(not branch_truncated and not branch_failed)

    complete = _ordered_paths(complete_paths)
    partial = _ordered_paths(partial_paths)
    if complete:
        reasons.add(CompositionReason.COMPLETE_UNIQUE if len(complete) == 1 else CompositionReason.COMPLETE_MULTIPLE)
    elif partial:
        reasons.add(CompositionReason.PARTIAL_PATH)
    else:
        reasons.add(CompositionReason.NO_PATH)

    raw_terminal_ids = tuple(path[-1]["proposition"]["projection"]["object_entity_id"] for path in complete)
    raw_terminal_labels = tuple(path[-1]["proposition"]["object_label"] for path in complete)
    raw_terminal_types = tuple(path[-1]["proposition"]["object_type"] for path in complete)
    terminal_values: dict[str, tuple[str, ExpectedObjectType]] = {}
    terminal_consistent = True
    for entity_id, label, object_type in zip(
        raw_terminal_ids,
        raw_terminal_labels,
        raw_terminal_types,
        strict=True,
    ):
        prior = terminal_values.get(entity_id)
        value = (label, object_type)
        if prior is not None and prior != value:
            terminal_consistent = False
            reasons.add(CompositionReason.CARDINALITY_CONFLICT)
        else:
            terminal_values[entity_id] = value
    terminal_ids = tuple(sorted(terminal_values))
    terminal_labels = tuple(terminal_values[entity_id][0] for entity_id in terminal_ids)
    terminal_types = tuple(terminal_values[entity_id][1] for entity_id in terminal_ids)
    truth_value = False
    truth_available = False
    aggregate_value = ""
    aggregate_available = False
    direct = False
    operator = current["operator"]
    known_complete = all(branch_known)

    if operator == GraphCompositionOperator.LOOKUP:
        direct = known_complete and terminal_consistent and len(terminal_ids) == 1
    elif operator == GraphCompositionOperator.EXISTS:
        if complete:
            truth_value = True
            truth_available = True
        elif known_complete:
            truth_value = False
            truth_available = True
        direct = truth_available
    elif operator == GraphCompositionOperator.NOT:
        if complete:
            truth_value = False
            truth_available = True
        elif known_complete:
            truth_value = True
            truth_available = True
        direct = truth_available
    elif operator == GraphCompositionOperator.AND:
        if all(branch_complete):
            truth_value = True
            truth_available = True
        elif known_complete:
            truth_value = False
            truth_available = True
        direct = truth_available
    elif operator == GraphCompositionOperator.OR:
        if any(branch_complete):
            truth_value = True
            truth_available = True
        elif known_complete:
            truth_value = False
            truth_available = True
        direct = truth_available
    elif operator == GraphCompositionOperator.COUNT:
        terminal_cardinalities = tuple(path[-1]["proposition"]["predicate_cardinality"] for path in complete)
        if any(value == PredicateCardinality.UNKNOWN for value in terminal_cardinalities):
            reasons.add(CompositionReason.CARDINALITY_UNKNOWN)
        elif len(terminal_ids) != len(raw_terminal_ids):
            reasons.add(CompositionReason.CARDINALITY_CONFLICT)
        elif known_complete:
            aggregate_value = str(len(terminal_ids))
            aggregate_available = True
            direct = True
    elif operator in {GraphCompositionOperator.MIN, GraphCompositionOperator.MAX, GraphCompositionOperator.ORDER}:
        if known_complete and terminal_consistent and terminal_labels:
            try:
                ordered = sorted(
                    zip(terminal_labels, terminal_types, strict=True),
                    key=lambda value: _typed_order_value(value[0], value[1]),
                    reverse=(operator == GraphCompositionOperator.MAX or current["descending"]),
                )
            except InvalidRequestError as error:
                reasons.add(
                    CompositionReason.TYPE_UNAVAILABLE
                    if str(error) == CompositionReason.TYPE_UNAVAILABLE.value
                    else CompositionReason.TYPE_MISMATCH
                )
            else:
                aggregate_value = (
                    ordered[0][0]
                    if operator in {GraphCompositionOperator.MIN, GraphCompositionOperator.MAX}
                    else " | ".join(value[0] for value in ordered)
                )
                aggregate_available = True
                direct = True
        elif known_complete:
            reasons.add(CompositionReason.AGGREGATE_UNSAFE)

    if truncated and not (operator in {GraphCompositionOperator.EXISTS, GraphCompositionOperator.OR} and truth_value):
        direct = False
        if not known_complete:
            reasons.add(CompositionReason.COMPLETENESS_UNKNOWN)
    result: CompositionExecution = {
        "complete_paths": complete,
        "partial_paths": partial,
        "terminal_entity_ids": terminal_ids,
        "terminal_labels": terminal_labels,
        "terminal_types": terminal_types,
        "truth_value": truth_value,
        "truth_available": truth_available,
        "aggregate_value": aggregate_value,
        "aggregate_value_available": aggregate_available,
        "direct_result": direct,
        "truncated": truncated,
        "reasons": tuple(sorted(reasons, key=lambda value: value.value)),
        "graph_rows": graph_rows,
    }
    return result


def phrase_composition_result(plan: object, execution: object) -> str:
    """Phrase only a direct-safe composition result from closed plan fields."""
    current = validate_composition_plan(plan)
    if not isinstance(execution, Mapping) or not execution.get("direct_result"):
        raise InvalidRequestError("composition result is not direct-result eligible")
    operator = current["operator"]
    chain = " → ".join(step["predicate_label"] for step in current["steps"] if step["branch"] == 0)
    if operator == GraphCompositionOperator.LOOKUP:
        labels = execution.get("terminal_labels")
        if not isinstance(labels, tuple) or len(labels) != 1 or not isinstance(labels[0], str):
            raise InvalidRequestError("composition lookup result is malformed")
        return f"{current['root_label']} — {chain}: {labels[0]}."
    if operator in {
        GraphCompositionOperator.EXISTS,
        GraphCompositionOperator.AND,
        GraphCompositionOperator.OR,
        GraphCompositionOperator.NOT,
    }:
        truth = execution.get("truth_value")
        if not isinstance(truth, bool) or not execution.get("truth_available"):
            raise InvalidRequestError("composition Boolean result is malformed")
        return f"{current['root_label']} — {operator.value.lower()} {chain}: {'true' if truth else 'false'}."
    aggregate = execution.get("aggregate_value")
    if not isinstance(aggregate, str) or not aggregate or not execution.get("aggregate_value_available"):
        raise InvalidRequestError("composition aggregate result is malformed")
    return f"{current['root_label']} — {operator.value.lower()} {chain}: {aggregate}."
