"""Closed contracts and bounded execution for one- and two-hop graph composition."""

from concurrent.futures import CancelledError
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from logging import getLogger as logging_getLogger
from re import compile as re_compile

from engram.constants import (
    COMPOSITION_DATE_SECONDS_FRACTION,
    COMPOSITION_PLAN_FIELDS,
    COMPOSITION_STEP_FIELDS,
    DATETIME_FRACTION_DIGITS,
    MAX_COMPOSITION_BINDING_BYTES,
    MAX_COMPOSITION_BRANCHES,
    MAX_COMPOSITION_CANDIDATES_PER_STEP,
    MAX_COMPOSITION_HOPS,
    MAX_COMPOSITION_PATH_PROPOSITIONS,
    MAX_COMPOSITION_ROWS,
    MAX_COMPOSITION_TEXT_BYTES,
    CanonicalResolutionStatus,
    CompositionReason,
    ExpectedObjectType,
    GraphCompositionOperator,
    PredicateCardinality,
    QueryOperator,
)
from engram.errors import InvalidRequestError, ResolutionCancelledError
from engram.evidence import validate_proposition_eligibility_decision
from engram.graph import validate_relation_proposition_projection
from engram.relation import canonical_resolution, validate_canonical_resolution
from engram.resolution import validate_query_frame
from engram.validation import require_identifier, require_text

logger = logging_getLogger(__name__)


def internal_binding(value: object, name: str) -> str:
    result = require_identifier(value, name, maximum_bytes=MAX_COMPOSITION_TEXT_BYTES)
    if len(result.encode("utf-8")) > MAX_COMPOSITION_BINDING_BYTES or not result.startswith("$") or len(result) == 1:
        raise InvalidRequestError(f"{name} must be a bounded $ binding")
    return result


def internal_integer(value: object, name: str, minimum: int, maximum: int) -> int:
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
) -> dict:
    """Build one fixed-predicate linear binding step."""
    if not isinstance(expected_object_type, ExpectedObjectType):
        raise InvalidRequestError("composition step expected_object_type is unsupported")
    result: dict = {
        "branch": internal_integer(branch, "composition step branch", 0, MAX_COMPOSITION_BRANCHES - 1),
        "hop": internal_integer(hop, "composition step hop", 0, MAX_COMPOSITION_HOPS - 1),
        "subject_binding": internal_binding(subject_binding, "composition step subject_binding"),
        "subject_entity_id": require_identifier(
            subject_entity_id, "composition step subject_entity_id", allow_empty=True, maximum_bytes=MAX_COMPOSITION_TEXT_BYTES
        ),
        "predicate_id": require_identifier(predicate_id, "composition step predicate_id", maximum_bytes=MAX_COMPOSITION_TEXT_BYTES),
        "predicate_label": require_text(
            predicate_label, "composition step predicate_label", maximum_bytes=MAX_COMPOSITION_TEXT_BYTES
        ),
        "object_binding": internal_binding(object_binding, "composition step object_binding"),
        "expected_object_type": expected_object_type,
        "max_candidates": internal_integer(
            max_candidates,
            "composition step max_candidates",
            1,
            MAX_COMPOSITION_CANDIDATES_PER_STEP,
        ),
    }
    if result.get("subject_binding", "") == result.get("object_binding", ""):
        raise InvalidRequestError("composition step input and output bindings must differ")
    return result


def validate_composition_step(value: object) -> dict:
    if not isinstance(value, dict) or set(value) != COMPOSITION_STEP_FIELDS:
        raise InvalidRequestError("CompositionStep has invalid fields")
    # The exact field set is checked above, so every typed default below is unreachable.
    result = composition_step(
        value.get("branch", 0),
        value.get("hop", 0),
        value.get("subject_binding", ""),
        value.get("subject_entity_id", ""),
        value.get("predicate_id", ""),
        value.get("predicate_label", ""),
        value.get("object_binding", ""),
        value.get("expected_object_type", ExpectedObjectType.UNKNOWN),
        value.get("max_candidates", 0),
    )
    return result


def composition_step_to_dict(value: object) -> dict:
    step = validate_composition_step(value)
    result: dict = dict(step)
    result["expected_object_type"] = step.get("expected_object_type", ExpectedObjectType.UNKNOWN).value
    return result


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
) -> dict:
    """Build one closed graph plan and reject underconstrained or cyclic bindings."""
    if not isinstance(operator, GraphCompositionOperator):
        raise InvalidRequestError("composition plan operator is unsupported")
    if not isinstance(steps, tuple) or not steps:
        raise InvalidRequestError("composition plan steps must be a non-empty tuple")
    normalized_steps = tuple(validate_composition_step(value) for value in steps)
    if normalized_steps != tuple(sorted(normalized_steps, key=lambda value: (value.get("branch", 0), value.get("hop", 0)))):
        raise InvalidRequestError("composition plan steps must be ordered by branch and hop")
    hop_limit = internal_integer(max_hops, "composition plan max_hops", 1, MAX_COMPOSITION_HOPS)
    row_limit = internal_integer(max_rows, "composition plan max_rows", 1, MAX_COMPOSITION_ROWS)
    branch_limit = internal_integer(max_branches, "composition plan max_branches", 1, MAX_COMPOSITION_BRANCHES)
    candidate_limit = internal_integer(
        max_candidates_per_step,
        "composition plan max_candidates_per_step",
        1,
        MAX_COMPOSITION_CANDIDATES_PER_STEP,
    )
    path_limit = internal_integer(
        max_path_propositions, "composition plan max_path_propositions", 1, MAX_COMPOSITION_PATH_PROPOSITIONS
    )
    branches = tuple(sorted({step.get("branch", 0) for step in normalized_steps}))
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

    terminal = internal_binding(terminal_binding, "composition plan terminal_binding")
    root_id = require_identifier(root_entity_id, "composition plan root_entity_id", maximum_bytes=MAX_COMPOSITION_TEXT_BYTES)
    for branch in branches:
        branch_steps = tuple(step for step in normalized_steps if step.get("branch", 0) == branch)
        if len(branch_steps) > hop_limit or len(branch_steps) > path_limit:
            raise InvalidRequestError("composition plan branch exceeds its hop or path limit")
        if tuple(step.get("hop", 0) for step in branch_steps) != tuple(range(len(branch_steps))):
            raise InvalidRequestError("composition plan branch hops must be contiguous")
        seen_bindings = {"$root"}
        previous_binding = "$root"
        for index, step in enumerate(branch_steps):
            subject_entity_id = step.get("subject_entity_id", "")
            object_binding = step.get("object_binding", "")
            if step.get("subject_binding", "") != previous_binding:
                raise InvalidRequestError("composition plan contains an unconstrained or cartesian binding")
            if index == 0 and subject_entity_id != root_id:
                raise InvalidRequestError("composition plan root step must bind the selected root entity")
            if index and subject_entity_id:
                raise InvalidRequestError("composition plan intermediate subject must come only from its prior binding")
            if object_binding in seen_bindings:
                raise InvalidRequestError("composition plan contains a binding cycle")
            if step.get("max_candidates", 0) > candidate_limit:
                raise InvalidRequestError("composition step exceeds the plan candidate limit")
            seen_bindings.add(object_binding)
            previous_binding = object_binding
        if previous_binding != terminal:
            raise InvalidRequestError("composition plan terminal_binding must be produced by every branch")

    if not isinstance(aggregation_inputs, tuple):
        raise InvalidRequestError("composition plan aggregation_inputs must be a tuple")
    normalized_aggregation = tuple(internal_binding(value, "composition plan aggregation input") for value in aggregation_inputs)
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
    final_types = {
        step.get("expected_object_type", ExpectedObjectType.UNKNOWN)
        for step in normalized_steps
        if step.get("object_binding", "") == terminal
    }
    if operator in {
        GraphCompositionOperator.MIN,
        GraphCompositionOperator.MAX,
        GraphCompositionOperator.ORDER,
    } and not final_types.issubset({ExpectedObjectType.NUMBER, ExpectedObjectType.DATE}):
        raise InvalidRequestError("composition ordered aggregates require NUMBER or DATE terminal values")
    result: dict = {
        "operator": operator,
        "root_entity_id": root_id,
        "root_label": require_text(root_label, "composition plan root_label", maximum_bytes=MAX_COMPOSITION_TEXT_BYTES),
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


def validate_composition_plan(value: object) -> dict:
    if not isinstance(value, dict) or set(value) != COMPOSITION_PLAN_FIELDS:
        raise InvalidRequestError("CompositionPlan has invalid fields")
    # The exact field set is checked above, so every typed default below is unreachable.
    result = composition_plan(
        value.get("operator", GraphCompositionOperator.LOOKUP),
        value.get("root_entity_id", ""),
        value.get("root_label", ""),
        value.get("steps", ()),
        value.get("terminal_binding", ""),
        aggregation_inputs=value.get("aggregation_inputs", ()),
        descending=value.get("descending", False),
        max_hops=value.get("max_hops", 0),
        max_rows=value.get("max_rows", 0),
        max_branches=value.get("max_branches", 0),
        max_candidates_per_step=value.get("max_candidates_per_step", 0),
        max_path_propositions=value.get("max_path_propositions", 0),
    )
    return result


def composition_plan_to_dict(value: object) -> dict:
    plan = validate_composition_plan(value)
    result = {
        "operator": plan.get("operator", GraphCompositionOperator.LOOKUP).value,
        "root_entity_id": plan.get("root_entity_id", ""),
        "root_label": plan.get("root_label", ""),
        "steps": [composition_step_to_dict(step) for step in plan.get("steps", ())],
        "terminal_binding": plan.get("terminal_binding", ""),
        "aggregation_inputs": list(plan.get("aggregation_inputs", ())),
        "descending": plan.get("descending", False),
        "max_hops": plan.get("max_hops", 0),
        "max_rows": plan.get("max_rows", 0),
        "max_branches": plan.get("max_branches", 0),
        "max_candidates_per_step": plan.get("max_candidates_per_step", 0),
        "max_path_propositions": plan.get("max_path_propositions", 0),
    }
    return result


def graph_composition_operator(frame: object) -> GraphCompositionOperator:
    """Map one query frame to the closed graph algebra without guessing unsupported intent."""
    current = validate_query_frame(frame)
    # validate_query_frame enforces the exact frame and identity field sets.
    operator = current.get("identity", {}).get("operator", QueryOperator.UNKNOWN)
    normalized = current.get("resolved_text", "").casefold()
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
) -> dict:
    """Compile selected canonical identities into one bounded linear plan."""
    root = validate_canonical_resolution(subject)
    if not isinstance(predicates, tuple) or not 1 <= len(predicates) <= MAX_COMPOSITION_HOPS:
        raise InvalidRequestError("linear composition requires one or two selected Predicates")
    selected = tuple(validate_canonical_resolution(value) for value in predicates)
    if not all(value.get("canonical_id", "") and value.get("primary_label", "") for value in (root, *selected)):
        raise InvalidRequestError(CompositionReason.IDENTITY_MISS.value)
    if not isinstance(operator, GraphCompositionOperator) or not isinstance(expected_object_type, ExpectedObjectType):
        raise InvalidRequestError("linear composition operator or expected type is unsupported")
    root_id = root.get("canonical_id", "")
    steps = []
    prior_binding = "$root"
    for hop, predicate in enumerate(selected):
        output_binding = "$result" if hop == len(selected) - 1 else f"$hop{hop + 1}"
        step_type = expected_object_type if hop == len(selected) - 1 else predicate.get("object_type", ExpectedObjectType.UNKNOWN)
        steps.append(
            composition_step(
                0,
                hop,
                prior_binding,
                root_id if hop == 0 else "",
                predicate.get("canonical_id", ""),
                predicate.get("primary_label", ""),
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
    result = composition_plan(
        operator,
        root_id,
        root.get("primary_label", ""),
        tuple(steps),
        "$result",
        aggregation_inputs=aggregation,
        max_rows=max_rows,
        max_candidates_per_step=max_candidates_per_step,
    )
    return result


TOKEN = re_compile(r"[A-Za-z0-9]+(?:[-_][A-Za-z0-9]+)*")
PREDICATE_STOP_WORDS = {
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
COMPOSITION_PREDICATE_ROW_FIELDS = set({"canonical_id", "primary_label", "object_type"})


def bounded_predicate_surfaces(text: object, excluded_words: object = ()) -> tuple[tuple[int, str], ...]:
    """Return deterministic one- to three-token Predicate lookup surfaces."""
    request = require_text(text, "composition request", 16_384)
    if not isinstance(excluded_words, tuple):
        raise InvalidRequestError("composition excluded_words must be a tuple")
    excluded = {str(value).casefold() for value in excluded_words}
    tokens = [(match.start(), match.group(0).casefold()) for match in TOKEN.finditer(request)]
    values = []
    for index, (position, token) in enumerate(tokens):
        if token in PREDICATE_STOP_WORDS or token in excluded:
            continue
        for width in (3, 2, 1):
            phrase_tokens = tokens[index : index + width]
            if len(phrase_tokens) != width:
                continue
            words = tuple(value for _, value in phrase_tokens)
            if any(value in excluded for value in words) or all(value in PREDICATE_STOP_WORDS for value in words):
                continue
            values.append((position, " ".join(words)))
    unique = []
    seen: set[tuple[int, str]] = set()
    for value in values:
        if value not in seen:
            seen.add(value)
            unique.append(value)
    result = tuple(unique[:12])
    return result


def resolve_composition_predicates(
    frame: object,
    root: object,
    lookup: object,
    cooperative_check: object = lambda: False,
) -> tuple[dict, ...]:
    """Resolve exactly two ordered Predicate identities for a supported composed frame."""
    current = validate_query_frame(frame)
    subject = validate_canonical_resolution(root)
    if not callable(lookup) or not callable(cooperative_check):
        raise InvalidRequestError("composition Predicate resolver dependencies must be callable")
    excluded = tuple(value.casefold() for value in TOKEN.findall(subject.get("primary_label", "")))
    matches: dict[tuple[str, int], tuple[str, ExpectedObjectType, str]] = {}
    for position, surface in bounded_predicate_surfaces(current.get("resolved_text", ""), excluded):
        cooperative_check()
        rows = lookup(surface, limit=2, cooperative_check=cooperative_check)
        # A lookup row lacking a required field is a malformed collection, refused before any field is read.
        if (
            not isinstance(rows, list)
            or len(rows) > 2
            or not all(isinstance(row, dict) and COMPOSITION_PREDICATE_ROW_FIELDS.issubset(row) for row in rows)
        ):
            raise InvalidRequestError("composition Predicate lookup returned an invalid collection")
        if len({row.get("canonical_id", "") for row in rows}) > 1:
            raise InvalidRequestError(CompositionReason.IDENTITY_AMBIGUOUS.value)
        for row in rows:
            canonical_id = require_identifier(
                row.get("canonical_id", ""), "composition Predicate canonical_id", maximum_bytes=MAX_COMPOSITION_TEXT_BYTES
            )
            label = require_text(
                row.get("primary_label", ""), "composition Predicate primary_label", maximum_bytes=MAX_COMPOSITION_TEXT_BYTES
            )
            object_type = row.get("object_type", ExpectedObjectType.UNKNOWN)
            if not isinstance(object_type, ExpectedObjectType):
                raise InvalidRequestError("composition Predicate object_type is unsupported")
            match_key = (canonical_id, position)
            prior = matches.get(match_key, ())
            candidate = (label, object_type, surface)
            if prior and prior[:2] != candidate[:2]:
                raise InvalidRequestError(CompositionReason.IDENTITY_AMBIGUOUS.value)
            if not prior or surface < prior[2]:
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


def typed_order_value(label: str, object_type: ExpectedObjectType) -> tuple:
    """Return an exact order key for one NUMBER or DATE terminal label.

    NUMBER labels compare as exact decimals. DATE labels compare as aware
    instants followed by any fractional-second digits beyond datetime's
    resolution, so distinct labels never share a rounded key.
    """
    if object_type == ExpectedObjectType.NUMBER:
        try:
            value = Decimal(label.replace(",", ""))
        except InvalidOperation as err:
            raise InvalidRequestError(CompositionReason.TYPE_MISMATCH.value) from err
        if not value.is_finite():
            raise InvalidRequestError(CompositionReason.TYPE_MISMATCH.value)
        result = (value,)
        return result
    if object_type == ExpectedObjectType.DATE:
        # A date without an offset is UTC, as elsewhere in Engram, so the order
        # never depends on the host time zone.
        try:
            parsed = datetime.fromisoformat(label.replace("Z", "+00:00"))
        except ValueError as err:
            raise InvalidRequestError(CompositionReason.TYPE_MISMATCH.value) from err
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        fraction = COMPOSITION_DATE_SECONDS_FRACTION.search(label)
        excess_digits = fraction.group(1)[DATETIME_FRACTION_DIGITS:] if fraction else ""
        remainder = Decimal(f"0.{excess_digits}") if excess_digits else Decimal(0)
        result = (parsed, remainder)
        return result
    raise InvalidRequestError(CompositionReason.TYPE_UNAVAILABLE.value)


def path_key(path: tuple) -> tuple[str, ...]:
    # Path entries are built by execute_composition_plan from validated relation projections.
    result = tuple(entry.get("proposition", {}).get("projection", {}).get("proposition_id", "") for entry in path)
    return result


def ordered_paths(paths: list[tuple]) -> tuple[tuple, ...]:
    by_key = {path_key(path): path for path in paths}
    result = tuple(by_key.get(key, ()) for key in sorted(by_key))
    return result


def execute_composition_plan(
    plan: object,
    query: object,
    evaluate: object,
    revalidate: object,
    cooperative_check: object = lambda: False,
) -> dict:
    """Execute fixed one-hop capabilities sequentially under declared non-time bounds."""
    current = validate_composition_plan(plan)
    if not callable(query):
        raise InvalidRequestError("composition query dependency must be callable")
    if not callable(evaluate):
        raise InvalidRequestError("composition eligibility dependency must be callable")
    if not callable(revalidate):
        raise InvalidRequestError("composition revalidation dependency must be callable")
    if not callable(cooperative_check):
        raise InvalidRequestError("composition execution dependencies must be callable")
    graph_rows = 0
    truncated = False
    reasons: set[CompositionReason] = set()
    complete_paths: list[tuple] = []
    partial_paths: list[tuple] = []
    # validate_composition_plan enforces the exact plan and step field sets, so the typed defaults are unreachable.
    plan_steps = current.get("steps", ())
    root_entity_id = current.get("root_entity_id", "")
    max_rows = current.get("max_rows", 0)
    max_path_propositions = current.get("max_path_propositions", 0)
    state_limit = current.get("max_branches", 0) * current.get("max_candidates_per_step", 0)
    branches = tuple(sorted({step.get("branch", 0) for step in plan_steps}))
    branch_complete: list[bool] = []
    branch_known: list[bool] = []

    for branch in branches:
        cooperative_check()
        states: list[dict] = [
            {
                "entity_id": root_entity_id,
                "entity_label": current.get("root_label", ""),
                "entity_type": ExpectedObjectType.ENTITY,
                "entity_history": (root_entity_id,),
                "path": (),
            }
        ]
        branch_steps = tuple(step for step in plan_steps if step.get("branch", 0) == branch)
        branch_truncated = False
        branch_failed = False
        branch_cycle = False
        for step in branch_steps:
            step_max_candidates = step.get("max_candidates", 0)
            step_predicate_id = step.get("predicate_id", "")
            next_states: list[dict] = []
            for state in states:
                cooperative_check()
                # Every state is built in this function with the same five fields.
                state_entity_id = state.get("entity_id", "")
                state_path = state.get("path", ())
                state_history = state.get("entity_history", ())
                state_advanced = False
                remaining = max_rows - graph_rows
                if remaining <= 0:
                    truncated = True
                    branch_truncated = True
                    reasons.add(CompositionReason.ROW_LIMIT)
                    if state_path:
                        partial_paths.append(state_path)
                    continue
                sentinel_limit = step_max_candidates + 1
                query_limit = min(sentinel_limit, remaining)
                try:
                    raw_rows = query(state_entity_id, step_predicate_id, query_limit)
                except (ResolutionCancelledError, CancelledError, MemoryError):
                    # Cancellation and the request's working-memory budget belong to the caller;
                    # only an ordinary graph failure becomes partial dependency evidence.
                    raise
                except Exception as error:
                    logger.warning("Composition graph step failed", exc_info=error)
                    branch_failed = True
                    reasons.add(CompositionReason.DEPENDENCY_FAILED)
                    if state_path:
                        partial_paths.append(state_path)
                    continue
                if not isinstance(raw_rows, list) or len(raw_rows) > query_limit:
                    raise InvalidRequestError("composition query returned an invalid collection")
                graph_rows += len(raw_rows)
                # validate_relation_proposition_projection enforces the exact relation and projection field sets.
                rows = [validate_relation_proposition_projection(value) for value in raw_rows]
                rows.sort(key=lambda value: value.get("projection", {}).get("proposition_id", ""))
                if len(rows) == sentinel_limit:
                    truncated = True
                    branch_truncated = True
                    reasons.add(CompositionReason.CANDIDATE_LIMIT)
                    rows = rows[:step_max_candidates]
                elif query_limit < sentinel_limit and len(rows) == query_limit:
                    truncated = True
                    branch_truncated = True
                    reasons.add(CompositionReason.ROW_LIMIT)
                for item in rows:
                    cooperative_check()
                    projection = item.get("projection", {})
                    if (
                        projection.get("subject_entity_id", "") != state_entity_id
                        or projection.get("predicate_id", "") != step_predicate_id
                    ):
                        raise InvalidRequestError("composition query returned a Proposition outside the requested binding")
                    initial = validate_proposition_eligibility_decision(evaluate(projection))
                    if not initial.get("eligible", False):
                        continue
                    if graph_rows >= max_rows:
                        truncated = True
                        branch_truncated = True
                        reasons.add(CompositionReason.ROW_LIMIT)
                        if state_path:
                            partial_paths.append(state_path)
                        break
                    decision = validate_proposition_eligibility_decision(revalidate(projection))
                    graph_rows += 1
                    if not decision.get("eligible", False) or not decision.get("revalidated", False):
                        continue
                    expected = step.get("expected_object_type", ExpectedObjectType.UNKNOWN)
                    item_type = item.get("object_type", ExpectedObjectType.UNKNOWN)
                    if expected != ExpectedObjectType.UNKNOWN and item_type != expected:
                        reasons.add(
                            CompositionReason.TYPE_UNAVAILABLE
                            if item_type == ExpectedObjectType.UNKNOWN
                            else CompositionReason.TYPE_MISMATCH
                        )
                        continue
                    object_id = projection.get("object_entity_id", "")
                    if object_id in state_history:
                        # The pruned path is a real graph path whose terminal value is dropped, so
                        # absence, counts and terminal sets over this branch are no longer exhaustive.
                        branch_cycle = True
                        reasons.add(CompositionReason.CYCLE)
                        if state_path:
                            partial_paths.append(state_path)
                        continue
                    entry: dict = {
                        "step": step,
                        "proposition": item,
                        "decision": decision,
                    }
                    path = (*state_path, entry)
                    if len(path) > max_path_propositions:
                        truncated = True
                        branch_truncated = True
                        reasons.add(CompositionReason.PATH_LIMIT)
                        partial_paths.append(state_path)
                        continue
                    next_states.append(
                        {
                            "entity_id": object_id,
                            "entity_label": item.get("object_label", ""),
                            "entity_type": item_type,
                            "entity_history": (*state_history, object_id),
                            "path": path,
                        }
                    )
                    state_advanced = True
                if not state_advanced and state_path:
                    partial_paths.append(state_path)
            states = next_states
            if not states:
                break
            if len(states) > state_limit:
                truncated = True
                branch_truncated = True
                reasons.add(CompositionReason.BRANCH_LIMIT)
                states = sorted(states, key=lambda value: path_key(value.get("path", ())))[:state_limit]
        completed = [state.get("path", ()) for state in states if len(state.get("path", ())) == len(branch_steps)]
        complete_paths.extend(completed)
        branch_complete.append(bool(completed))
        branch_known.append(not branch_truncated and not branch_failed and not branch_cycle)

    complete = ordered_paths(complete_paths)
    partial = ordered_paths(partial_paths)
    if complete:
        reasons.add(CompositionReason.COMPLETE_UNIQUE if len(complete) == 1 else CompositionReason.COMPLETE_MULTIPLE)
    elif partial:
        reasons.add(CompositionReason.PARTIAL_PATH)
    else:
        reasons.add(CompositionReason.NO_PATH)

    terminal_propositions = tuple(path[-1].get("proposition", {}) for path in complete)
    raw_terminal_ids = tuple(item.get("projection", {}).get("object_entity_id", "") for item in terminal_propositions)
    raw_terminal_labels = tuple(item.get("object_label", "") for item in terminal_propositions)
    raw_terminal_types = tuple(item.get("object_type", ExpectedObjectType.UNKNOWN) for item in terminal_propositions)
    terminal_values: dict[str, tuple[str, ExpectedObjectType]] = {}
    terminal_consistent = True
    for entity_id, label, object_type in zip(
        raw_terminal_ids,
        raw_terminal_labels,
        raw_terminal_types,
        strict=True,
    ):
        value = (label, object_type)
        if entity_id not in terminal_values:
            terminal_values[entity_id] = value
            continue
        prior = terminal_values.get(entity_id, ("", ExpectedObjectType.UNKNOWN))
        if prior != value:
            terminal_consistent = False
            reasons.add(CompositionReason.CARDINALITY_CONFLICT)
    terminal_ids = tuple(sorted(terminal_values))
    terminal_labels = tuple(terminal_values.get(entity_id, ("", ExpectedObjectType.UNKNOWN))[0] for entity_id in terminal_ids)
    terminal_types = tuple(terminal_values.get(entity_id, ("", ExpectedObjectType.UNKNOWN))[1] for entity_id in terminal_ids)
    truth_value = False
    truth_available = False
    aggregate_value = ""
    aggregate_available = False
    direct = False
    operator = current.get("operator", GraphCompositionOperator.LOOKUP)
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
        terminal_cardinalities = tuple(
            item.get("predicate_cardinality", PredicateCardinality.UNKNOWN) for item in terminal_propositions
        )
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
                keyed = tuple(
                    (typed_order_value(label, object_type), label)
                    for label, object_type in zip(terminal_labels, terminal_types, strict=True)
                )
                # Exact NUMBER and DATE keys are not mutually ordered.
                if len(set(terminal_types)) != 1:
                    raise InvalidRequestError(CompositionReason.TYPE_MISMATCH.value)
            except InvalidRequestError as error:
                reasons.add(
                    CompositionReason.TYPE_UNAVAILABLE
                    if str(error) == CompositionReason.TYPE_UNAVAILABLE.value
                    else CompositionReason.TYPE_MISMATCH
                )
            else:
                ordered = sorted(
                    keyed,
                    key=lambda value: value[0],
                    reverse=(operator == GraphCompositionOperator.MAX or current.get("descending", False)),
                )
                aggregate_value = (
                    ordered[0][1]
                    if operator in {GraphCompositionOperator.MIN, GraphCompositionOperator.MAX}
                    else " | ".join(value[1] for value in ordered)
                )
                aggregate_available = True
                direct = True
        elif known_complete:
            reasons.add(CompositionReason.AGGREGATE_UNSAFE)

    if truncated and not (operator in {GraphCompositionOperator.EXISTS, GraphCompositionOperator.OR} and truth_value):
        direct = False
        if not known_complete:
            reasons.add(CompositionReason.COMPLETENESS_UNKNOWN)
    result: dict = {
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
    if not isinstance(execution, dict) or not execution.get("direct_result", False):
        raise InvalidRequestError("composition result is not direct-result eligible")
    operator = current.get("operator", GraphCompositionOperator.LOOKUP)
    root_label = current.get("root_label", "")
    chain = " → ".join(step.get("predicate_label", "") for step in current.get("steps", ()) if step.get("branch", 0) == 0)
    if operator == GraphCompositionOperator.LOOKUP:
        labels = execution.get("terminal_labels", ())
        if not isinstance(labels, tuple) or len(labels) != 1 or not isinstance(labels[0], str):
            raise InvalidRequestError("composition lookup result is malformed")
        result = f"{root_label} — {chain}: {labels[0]}."
        return result
    if operator in {
        GraphCompositionOperator.EXISTS,
        GraphCompositionOperator.AND,
        GraphCompositionOperator.OR,
        GraphCompositionOperator.NOT,
    }:
        # A missing truth value is malformed; False is a real answer, so presence is checked explicitly.
        truth = execution.get("truth_value", False)
        if "truth_value" not in execution or not isinstance(truth, bool) or not execution.get("truth_available", False):
            raise InvalidRequestError("composition Boolean result is malformed")
        result = f"{root_label} — {operator.value.lower()} {chain}: {'true' if truth else 'false'}."
        return result
    aggregate = execution.get("aggregate_value", "")
    if not isinstance(aggregate, str) or not aggregate or not execution.get("aggregate_value_available", False):
        raise InvalidRequestError("composition aggregate result is malformed")
    result = f"{root_label} — {operator.value.lower()} {chain}: {aggregate}."
    return result
