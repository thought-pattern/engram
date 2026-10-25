"""Native FeatureSet and full-Proposition evidence contracts.

This owner validates and isolates disclosed evidence. It performs no graph reads,
candidate selection, request execution, or Channel release judgment.
"""

from engram.constants import EMPTY_MAPPING
from engram.constants_evidence import (
    CANONICAL_PROPOSITION_REFERENCES_FIELDS,
    DISCLOSURE_DECISION_FIELDS,
    MAX_DISCLOSURE_AUTHORITY_BYTES,
    MAX_DISCLOSURE_ENUM_BYTES,
    MAX_PROPOSITION_IDENTIFIER_BYTES,
    MAX_PROPOSITION_SELECTION_REASONS,
    MAX_PROPOSITION_SOURCE_CONTRIBUTIONS,
    MAX_PROPOSITION_TRUST_CATEGORY_BYTES,
    PROPOSITION_EVIDENCE_PATH_STEP_FIELDS,
    PROPOSITION_EVIDENCE_RECORD_FIELDS,
    PROPOSITION_PATH_FILTERS,
    PROPOSITION_TRUST_INPUTS_FIELDS,
    PROPOSITION_VALIDITY_INPUTS_FIELDS,
    DisclosureBasis,
    PropositionOwnership,
)
from engram.constants_identity import MAX_ENUM_WIRE_BYTES
from engram.constants_relation import (
    MAX_COMPOSITION_BINDING_BYTES,
    MAX_COMPOSITION_PATH_PROPOSITIONS,
    GraphCompositionOperator,
    TemporalAxis,
    TemporalQueryOperator,
)
from engram.constants_resolution import (
    FEATURE_SET_FIELDS,
    MAX_FEATURE_MAGNITUDE,
    MAX_FEATURE_NAME_BYTES,
    MAX_FEATURES,
    MAX_REASON_CODE_BYTES,
    MAX_RESOLVER_NAME_BYTES,
)
from engram.errors import IdentityValidationError, InvalidRequestError
from engram.identity import scope_key_from_dict, validate_scope_key
from engram.resolution_values import (
    exact_mapping,
    freeze_mapping,
    json_text,
    load_json_mapping,
    require_float,
    require_int,
    require_list,
)
from engram.validation import require_any_text, require_available_utc_timestamp, require_bool, require_identifier, utc_datetime


def feature_set(
    values: object = EMPTY_MAPPING,
    unavailable: object = (),
) -> dict:
    """Build a generic feature value/availability dictionary; Section 5 owns semantics."""
    if not isinstance(values, dict):
        raise InvalidRequestError("feature values must be an object")
    # Check name types before sorting: mixed key types cannot be ordered.
    if not all(isinstance(name, str) for name in values):
        raise InvalidRequestError("feature names must be strings")
    validated_values = {}
    # Names are unique strings, so ordering the items never compares their values.
    for name, item in sorted(values.items()):
        key = require_any_text(name, "feature name", MAX_FEATURE_NAME_BYTES, allow_empty=False)
        validated_values[key] = require_float(item, f"feature {key}", -MAX_FEATURE_MAGNITUDE, MAX_FEATURE_MAGNITUDE)
    if len(validated_values) > MAX_FEATURES:
        raise InvalidRequestError(f"feature values exceed the limit of {MAX_FEATURES}")
    if not isinstance(unavailable, tuple):
        raise InvalidRequestError("unavailable features must be a tuple")
    normalized_unavailable = tuple(
        require_any_text(name, "unavailable feature", MAX_FEATURE_NAME_BYTES, allow_empty=False) for name in unavailable
    )
    if normalized_unavailable != tuple(sorted(set(normalized_unavailable))):
        raise InvalidRequestError("unavailable features must be unique and sorted")
    if set(validated_values).intersection(normalized_unavailable):
        raise InvalidRequestError("a feature cannot be both available and unavailable")
    if len(validated_values) + len(normalized_unavailable) > MAX_FEATURES:
        raise InvalidRequestError(f"features exceed the limit of {MAX_FEATURES}")
    result: dict = {
        "values": dict(validated_values),
        "unavailable": normalized_unavailable,
    }
    return result


def validate_feature_set(value: object) -> dict:
    """Revalidate and defensively copy one feature-set dictionary."""
    data = exact_mapping(value, "FeatureSet", FEATURE_SET_FIELDS)
    result = feature_set(data.get("values", {}), data.get("unavailable", ()))
    return result


def feature_set_to_dict(value: object) -> dict[str, object]:
    """Serialize one feature set."""
    features = validate_feature_set(value)
    result = {
        "values": dict(features.get("values", {})),
        "unavailable": list(features.get("unavailable", ())),
    }
    return result


def feature_set_from_dict(value: object) -> dict:
    """Decode one feature set from its exact serialized form."""
    data = exact_mapping(value, "FeatureSet", FEATURE_SET_FIELDS)
    unavailable = require_list(data.get("unavailable", []), "unavailable features")
    raw_values = freeze_mapping(data.get("values", {}), "feature values")
    values = {
        name: require_float(item, f"feature {name}", -MAX_FEATURE_MAGNITUDE, MAX_FEATURE_MAGNITUDE)
        for name, item in raw_values.items()
    }
    normalized_unavailable = tuple(
        require_any_text(item, "unavailable feature", MAX_FEATURE_NAME_BYTES, allow_empty=False) for item in unavailable
    )
    result = feature_set(values, normalized_unavailable)
    return result


def canonical_proposition_references(
    subject_entity_id: object,
    predicate_id: object,
    object_entity_id: object,
) -> dict:
    """Build canonical graph identifiers for one full Proposition record."""
    result: dict = {
        "subject_entity_id": require_identifier(
            subject_entity_id, "Proposition subject_entity_id", maximum_bytes=MAX_PROPOSITION_IDENTIFIER_BYTES
        ),
        "predicate_id": require_identifier(
            predicate_id, "Proposition predicate_id", maximum_bytes=MAX_PROPOSITION_IDENTIFIER_BYTES
        ),
        "object_entity_id": require_identifier(
            object_entity_id, "Proposition object_entity_id", maximum_bytes=MAX_PROPOSITION_IDENTIFIER_BYTES
        ),
    }
    return result


def validate_canonical_proposition_references(value: object) -> dict:
    """Revalidate and copy one canonical Proposition-reference dictionary."""
    data = exact_mapping(value, "CanonicalPropositionReferences", CANONICAL_PROPOSITION_REFERENCES_FIELDS)
    result = canonical_proposition_references(
        data.get("subject_entity_id", ""),
        data.get("predicate_id", ""),
        data.get("object_entity_id", ""),
    )
    return result


def canonical_proposition_references_from_dict(value: object) -> dict:
    """Decode canonical Proposition references from their exact serialized form."""
    result = validate_canonical_proposition_references(value)
    return result


def proposition_validity_inputs(
    evaluation_time: object,
    active: object,
    system_current: object,
    valid_time_current: object,
    valid_from: object = "",
    valid_from_available: object = False,
    valid_to: object = "",
    valid_to_available: object = False,
    temporal_operator: object = TemporalQueryOperator.UNSPECIFIED,
    temporal_axis: object = TemporalAxis.VALID_TIME,
    requested_start: object = "",
    requested_start_available: object = False,
    requested_end: object = "",
    requested_end_available: object = False,
    system_from: object = "",
    system_from_available: object = False,
    system_to: object = "",
    system_to_available: object = False,
    invalidated_at: object = "",
    invalidated_at_available: object = False,
    eligible_for_request: object = True,
    system_time_match: object = True,
    valid_time_match: object = True,
    valid_time_match_available: object = True,
) -> dict:
    """Build inspectable temporal inputs for one eligible Proposition."""
    evaluation, _ = require_available_utc_timestamp(evaluation_time, True, "Proposition evaluation_time")
    normalized_active = require_bool(active, "Proposition active")
    normalized_system_current = require_bool(system_current, "Proposition system_current")
    normalized_valid_time_current = require_bool(valid_time_current, "Proposition valid_time_current")
    if not isinstance(temporal_operator, TemporalQueryOperator):
        raise InvalidRequestError("Proposition temporal_operator is unsupported")
    if not isinstance(temporal_axis, TemporalAxis):
        raise InvalidRequestError("Proposition temporal_axis is unsupported")
    requested_lower, requested_lower_available = require_available_utc_timestamp(
        requested_start,
        requested_start_available,
        "Proposition requested_start",
    )
    requested_upper, requested_upper_available = require_available_utc_timestamp(
        requested_end,
        requested_end_available,
        "Proposition requested_end",
    )
    system_lower, system_lower_available = require_available_utc_timestamp(
        system_from, system_from_available, "Proposition system_from"
    )
    system_upper, system_upper_available = require_available_utc_timestamp(system_to, system_to_available, "Proposition system_to")
    invalidated, invalidated_available = require_available_utc_timestamp(
        invalidated_at,
        invalidated_at_available,
        "Proposition invalidated_at",
    )
    lower, lower_available = require_available_utc_timestamp(valid_from, valid_from_available, "Proposition valid_from")
    upper, upper_available = require_available_utc_timestamp(valid_to, valid_to_available, "Proposition valid_to")
    for name, interval_lower, interval_lower_available, interval_upper, interval_upper_available in (
        ("requested_start", requested_lower, requested_lower_available, requested_upper, requested_upper_available),
        ("system_from", system_lower, system_lower_available, system_upper, system_upper_available),
        ("valid_from", lower, lower_available, upper, upper_available),
    ):
        if interval_lower_available and interval_upper_available:
            lower_time = utc_datetime(interval_lower)
            upper_time = utc_datetime(interval_upper)
            if lower_time >= upper_time:
                raise InvalidRequestError(f"Proposition {name} must be earlier than its upper bound")
    expected_request_bounds = {
        TemporalQueryOperator.UNSPECIFIED: (False, False),
        TemporalQueryOperator.CURRENT: (False, False),
        TemporalQueryOperator.NOW: (False, False),
        TemporalQueryOperator.AS_OF: (True, False),
        TemporalQueryOperator.IN_YEAR: (True, True),
        TemporalQueryOperator.BEFORE: (False, True),
        TemporalQueryOperator.AFTER: (True, False),
        TemporalQueryOperator.BETWEEN: (True, True),
        TemporalQueryOperator.LATEST: (False, False),
    }
    if (requested_lower_available, requested_upper_available) != expected_request_bounds.get(temporal_operator, ()):
        raise InvalidRequestError("Proposition requested temporal bounds conflict with temporal_operator")
    evaluated_at = utc_datetime(evaluation)
    effective_system_upper = system_upper
    effective_system_upper_available = system_upper_available
    if invalidated_available and (
        not effective_system_upper_available or utc_datetime(invalidated) < utc_datetime(effective_system_upper)
    ):
        effective_system_upper = invalidated
        effective_system_upper_available = True
    observed_active = not invalidated_available
    observed_system_current = (not system_lower_available or evaluated_at >= utc_datetime(system_lower)) and (
        not effective_system_upper_available or evaluated_at < utc_datetime(effective_system_upper)
    )
    observed_valid_current = (not lower_available or evaluated_at >= utc_datetime(lower)) and (
        not upper_available or evaluated_at < utc_datetime(upper)
    )
    if normalized_active != observed_active:
        raise InvalidRequestError("Proposition active conflicts with the disclosed invalidation boundary")
    if normalized_system_current != observed_system_current:
        raise InvalidRequestError("Proposition system_current conflicts with the disclosed system interval")
    if normalized_valid_time_current != observed_valid_current:
        raise InvalidRequestError("Proposition valid_time_current conflicts with the disclosed valid interval")
    normalized_request_eligible = require_bool(eligible_for_request, "Proposition eligible_for_request")
    normalized_system_match = require_bool(system_time_match, "Proposition system_time_match")
    normalized_valid_match = require_bool(valid_time_match, "Proposition valid_time_match")
    normalized_valid_match_available = require_bool(
        valid_time_match_available,
        "Proposition valid_time_match_available",
    )
    if not normalized_request_eligible or not normalized_system_match:
        raise InvalidRequestError("Proposition evidence validity inputs must describe a Proposition eligible for the request")
    if normalized_valid_match_available != (temporal_axis == TemporalAxis.VALID_TIME):
        raise InvalidRequestError("Proposition valid_time_match availability conflicts with temporal_axis")
    if normalized_valid_match_available != normalized_valid_match:
        raise InvalidRequestError("available Proposition valid_time_match must be true and unavailable match must be false")
    result: dict = {
        "evaluation_time": evaluation,
        "active": normalized_active,
        "system_current": normalized_system_current,
        "valid_time_current": normalized_valid_time_current,
        "eligible_for_request": normalized_request_eligible,
        "system_time_match": normalized_system_match,
        "valid_time_match": normalized_valid_match,
        "valid_time_match_available": normalized_valid_match_available,
        "temporal_operator": temporal_operator,
        "temporal_axis": temporal_axis,
        "requested_start": requested_lower,
        "requested_start_available": requested_lower_available,
        "requested_end": requested_upper,
        "requested_end_available": requested_upper_available,
        "system_from": system_lower,
        "system_from_available": system_lower_available,
        "system_to": system_upper,
        "system_to_available": system_upper_available,
        "invalidated_at": invalidated,
        "invalidated_at_available": invalidated_available,
        "valid_from": lower,
        "valid_from_available": lower_available,
        "valid_to": upper,
        "valid_to_available": upper_available,
    }
    return result


def validate_proposition_validity_inputs(value: object) -> dict:
    """Revalidate and copy one Proposition-validity input dictionary."""
    data = exact_mapping(value, "PropositionValidityInputs", PROPOSITION_VALIDITY_INPUTS_FIELDS)
    result = proposition_validity_inputs(
        data.get("evaluation_time", ""),
        data.get("active", False),
        data.get("system_current", False),
        data.get("valid_time_current", False),
        data.get("valid_from", ""),
        data.get("valid_from_available", False),
        data.get("valid_to", ""),
        data.get("valid_to_available", False),
        data.get("temporal_operator", TemporalQueryOperator.UNSPECIFIED),
        data.get("temporal_axis", TemporalAxis.VALID_TIME),
        data.get("requested_start", ""),
        data.get("requested_start_available", False),
        data.get("requested_end", ""),
        data.get("requested_end_available", False),
        data.get("system_from", ""),
        data.get("system_from_available", False),
        data.get("system_to", ""),
        data.get("system_to_available", False),
        data.get("invalidated_at", ""),
        data.get("invalidated_at_available", False),
        data.get("eligible_for_request", False),
        data.get("system_time_match", False),
        data.get("valid_time_match", False),
        data.get("valid_time_match_available", False),
    )
    return result


def trusted_proposition_validity_inputs_to_dict(validity: dict) -> dict[str, object]:
    """Serialize Proposition-validity inputs already validated inside a trusted record."""
    result: dict[str, object] = dict(validity)
    result["temporal_operator"] = validity.get("temporal_operator", TemporalQueryOperator.UNSPECIFIED).value
    result["temporal_axis"] = validity.get("temporal_axis", TemporalAxis.VALID_TIME).value
    return result


def proposition_validity_inputs_from_dict(value: object) -> dict:
    """Decode Proposition-validity inputs from their exact serialized form."""
    if not isinstance(value, dict):
        raise InvalidRequestError("PropositionValidityInputs must be an object")
    decoded = dict(value)
    try:
        decoded["temporal_operator"] = TemporalQueryOperator(str(decoded.get("temporal_operator", "")))
        decoded["temporal_axis"] = TemporalAxis(str(decoded.get("temporal_axis", "")))
    except ValueError as err:
        raise InvalidRequestError("Proposition validity temporal enum is unsupported") from err
    result = validate_proposition_validity_inputs(decoded)
    return result


def proposition_trust_inputs(
    trust_category: object = "",
    trust_category_available: object = False,
    supplied_trust: object = 0.0,
    supplied_trust_available: object = False,
) -> dict:
    """Build supplied Proposition trust values with concrete availability."""
    category_available = require_bool(trust_category_available, "Proposition trust_category_available")
    category = require_any_text(
        trust_category,
        "Proposition trust_category",
        MAX_PROPOSITION_TRUST_CATEGORY_BYTES,
        allow_empty=not category_available,
    )
    if not category_available and category:
        raise InvalidRequestError("Proposition trust_category must be empty when unavailable")
    supplied_available = require_bool(supplied_trust_available, "Proposition supplied_trust_available")
    supplied = require_float(supplied_trust, "Proposition supplied_trust", 0.0, 1.0)
    if not supplied_available and supplied != 0.0:
        raise InvalidRequestError("Proposition supplied_trust must be zero when unavailable")
    result: dict = {
        "trust_category": category,
        "trust_category_available": category_available,
        "supplied_trust": supplied,
        "supplied_trust_available": supplied_available,
    }
    return result


def validate_proposition_trust_inputs(value: object) -> dict:
    """Revalidate and copy one Proposition-trust input dictionary."""
    data = exact_mapping(value, "PropositionTrustInputs", PROPOSITION_TRUST_INPUTS_FIELDS)
    result = proposition_trust_inputs(
        data.get("trust_category", ""),
        data.get("trust_category_available", False),
        data.get("supplied_trust", 0.0),
        data.get("supplied_trust_available", False),
    )
    return result


def proposition_trust_inputs_from_dict(value: object) -> dict:
    """Decode Proposition-trust inputs from their exact serialized form."""
    result = validate_proposition_trust_inputs(value)
    return result


def disclosure_decision(
    ownership: object,
    basis: object,
    scope: object,
    authority: object = "",
    authority_available: object = False,
) -> dict:
    """Build an exact scoped Proposition-disclosure decision."""
    if not isinstance(ownership, PropositionOwnership):
        raise InvalidRequestError("disclosure ownership must be a PropositionOwnership")
    if not isinstance(basis, DisclosureBasis):
        raise InvalidRequestError("disclosure basis must be a DisclosureBasis")
    try:
        validated_scope = validate_scope_key(scope)
    except IdentityValidationError as err:
        raise InvalidRequestError("disclosure scope must be a ScopeKey") from err
    normalized_authority_available = require_bool(authority_available, "disclosure authority_available")
    if normalized_authority_available:
        normalized_authority = require_identifier(authority, "disclosure authority", maximum_bytes=MAX_PROPOSITION_IDENTIFIER_BYTES)
    else:
        normalized_authority = require_any_text(
            authority,
            "disclosure authority",
            MAX_DISCLOSURE_AUTHORITY_BYTES,
            allow_empty=True,
        )
    if not normalized_authority_available and normalized_authority:
        raise InvalidRequestError("disclosure authority must be empty when unavailable")
    if ownership == PropositionOwnership.PUBLIC:
        if basis != DisclosureBasis.PUBLIC_RULE or normalized_authority_available:
            raise InvalidRequestError("PUBLIC Proposition disclosure requires the public rule without an authority")
    elif basis != DisclosureBasis.TRUSTED_SCOPE_AUTHORITY or not normalized_authority_available:
        raise InvalidRequestError("non-PUBLIC Proposition disclosure requires an available trusted scope authority")
    result: dict = {
        "ownership": ownership,
        "basis": basis,
        "scope": validated_scope,
        "authority": normalized_authority,
        "authority_available": normalized_authority_available,
    }
    return result


def validate_disclosure_decision(value: object) -> dict:
    """Revalidate and copy one disclosure-decision dictionary."""
    data = exact_mapping(value, "DisclosureDecision", DISCLOSURE_DECISION_FIELDS)
    result = disclosure_decision(
        data.get("ownership", PropositionOwnership.PUBLIC),
        data.get("basis", DisclosureBasis.PUBLIC_RULE),
        data.get("scope", {}),
        data.get("authority", ""),
        data.get("authority_available", False),
    )
    return result


def disclosure_decision_from_dict(value: object) -> dict:
    """Decode one disclosure decision from its exact serialized form."""
    data = exact_mapping(value, "DisclosureDecision", DISCLOSURE_DECISION_FIELDS)
    try:
        ownership = PropositionOwnership(
            require_any_text(data.get("ownership", ""), "disclosure ownership", MAX_DISCLOSURE_ENUM_BYTES, allow_empty=False)
        )
        basis = DisclosureBasis(
            require_any_text(data.get("basis", ""), "disclosure basis", MAX_DISCLOSURE_ENUM_BYTES, allow_empty=False)
        )
    except ValueError as err:
        raise InvalidRequestError("unsupported disclosure ownership or basis") from err
    result = disclosure_decision(
        ownership,
        basis,
        scope_key_from_dict(freeze_mapping(data.get("scope", {}), "disclosure scope")),
        data.get("authority", ""),
        data.get("authority_available", False),
    )
    return result


def path_binding(value: object, name: str) -> str:
    binding = require_identifier(value, name, MAX_COMPOSITION_BINDING_BYTES)
    if not binding.startswith("$") or len(binding) == 1:
        raise InvalidRequestError(f"{name} must start with $ and contain a name")
    return binding


def proposition_evidence_path_step(
    position: object,
    proposition_id: object,
    subject_entity_id: object,
    predicate_id: object,
    object_entity_id: object,
    operator: object,
    input_binding: object,
    output_binding: object,
    filters: object,
    aggregation_inputs: object = (),
) -> dict:
    """Build one closed Proposition-path step without unrestricted graph content."""
    normalized_position = require_int(position, "Proposition evidence path position", 0, MAX_COMPOSITION_PATH_PROPOSITIONS - 1)
    if not isinstance(operator, GraphCompositionOperator):
        raise InvalidRequestError("Proposition evidence path operator is unsupported")
    if not isinstance(filters, tuple):
        raise InvalidRequestError("Proposition evidence path filters must be a tuple")
    normalized_filters = tuple(
        require_identifier(value, "Proposition evidence path filter", maximum_bytes=MAX_PROPOSITION_IDENTIFIER_BYTES)
        for value in filters
    )
    if not normalized_filters or normalized_filters != tuple(sorted(set(normalized_filters))):
        raise InvalidRequestError("Proposition evidence path filters must be non-empty, unique, and sorted")
    if not set(normalized_filters).issubset(PROPOSITION_PATH_FILTERS):
        raise InvalidRequestError("Proposition evidence path filter is unsupported")
    if not isinstance(aggregation_inputs, tuple):
        raise InvalidRequestError("Proposition evidence path aggregation_inputs must be a tuple")
    normalized_aggregation = tuple(
        path_binding(value, "Proposition evidence path aggregation input") for value in aggregation_inputs
    )
    if normalized_aggregation != tuple(sorted(set(normalized_aggregation))):
        raise InvalidRequestError("Proposition evidence path aggregation_inputs must be unique and sorted")
    result: dict = {
        "position": normalized_position,
        "proposition_id": require_identifier(
            proposition_id, "Proposition evidence path proposition_id", maximum_bytes=MAX_PROPOSITION_IDENTIFIER_BYTES
        ),
        "subject_entity_id": require_identifier(
            subject_entity_id, "Proposition evidence path subject_entity_id", maximum_bytes=MAX_PROPOSITION_IDENTIFIER_BYTES
        ),
        "predicate_id": require_identifier(
            predicate_id, "Proposition evidence path predicate_id", maximum_bytes=MAX_PROPOSITION_IDENTIFIER_BYTES
        ),
        "object_entity_id": require_identifier(
            object_entity_id, "Proposition evidence path object_entity_id", maximum_bytes=MAX_PROPOSITION_IDENTIFIER_BYTES
        ),
        "operator": operator,
        "input_binding": path_binding(input_binding, "Proposition evidence path input_binding"),
        "output_binding": path_binding(output_binding, "Proposition evidence path output_binding"),
        "filters": normalized_filters,
        "aggregation_inputs": normalized_aggregation,
    }
    if result.get("input_binding", "") == result.get("output_binding", ""):
        raise InvalidRequestError("Proposition evidence path input and output bindings must differ")
    return result


def validate_proposition_evidence_path_step(value: object) -> dict:
    data = exact_mapping(value, "PropositionEvidencePathStep", PROPOSITION_EVIDENCE_PATH_STEP_FIELDS)
    result = proposition_evidence_path_step(
        data.get("position", 0),
        data.get("proposition_id", ""),
        data.get("subject_entity_id", ""),
        data.get("predicate_id", ""),
        data.get("object_entity_id", ""),
        data.get("operator", GraphCompositionOperator.LOOKUP),
        data.get("input_binding", ""),
        data.get("output_binding", ""),
        data.get("filters", ()),
        data.get("aggregation_inputs", ()),
    )
    return result


def proposition_evidence_path_step_to_dict(value: object) -> dict[str, object]:
    step = validate_proposition_evidence_path_step(value)
    result = {
        "position": step.get("position", 0),
        "proposition_id": step.get("proposition_id", ""),
        "subject_entity_id": step.get("subject_entity_id", ""),
        "predicate_id": step.get("predicate_id", ""),
        "object_entity_id": step.get("object_entity_id", ""),
        "operator": step.get("operator", GraphCompositionOperator.LOOKUP).value,
        "input_binding": step.get("input_binding", ""),
        "output_binding": step.get("output_binding", ""),
        "filters": list(step.get("filters", ())),
        "aggregation_inputs": list(step.get("aggregation_inputs", ())),
    }
    return result


def proposition_evidence_path_step_from_dict(value: object) -> dict:
    data = exact_mapping(value, "PropositionEvidencePathStep", PROPOSITION_EVIDENCE_PATH_STEP_FIELDS)
    try:
        operator = GraphCompositionOperator(
            require_any_text(data.get("operator", ""), "Proposition evidence path operator", MAX_ENUM_WIRE_BYTES, allow_empty=False)
        )
    except ValueError as err:
        raise InvalidRequestError("Proposition evidence path operator is unsupported") from err
    raw_filters = require_list(data.get("filters", []), "Proposition evidence path filters")
    raw_aggregation = require_list(data.get("aggregation_inputs", []), "Proposition evidence path aggregation_inputs")
    result = proposition_evidence_path_step(
        data.get("position", 0),
        data.get("proposition_id", ""),
        data.get("subject_entity_id", ""),
        data.get("predicate_id", ""),
        data.get("object_entity_id", ""),
        operator,
        data.get("input_binding", ""),
        data.get("output_binding", ""),
        tuple(raw_filters),
        tuple(raw_aggregation),
    )
    return result


def proposition_evidence_record(
    proposition_id: object,
    source_resolver: object,
    source_contributions: object,
    features: object,
    canonical_references: object,
    validity: object,
    trust: object,
    disclosure: object,
    path: object,
    selection_reasons: object,
    *,
    trusted_components: bool = False,
) -> dict:
    """Build strict wire-safe full-Proposition evidence without unrestricted graph content.

    ``trusted_components`` is for a caller that has just built ``features``,
    ``canonical_references``, ``validity``, ``trust``, and ``disclosure`` with
    their validating constructors; they are not validated again. Identifiers,
    ordering, and path checks always run.
    """
    normalized_proposition_id = require_identifier(
        proposition_id, "Proposition evidence proposition_id", maximum_bytes=MAX_PROPOSITION_IDENTIFIER_BYTES
    )
    source = require_identifier(source_resolver, "Proposition evidence source_resolver", MAX_RESOLVER_NAME_BYTES)
    if not isinstance(source_contributions, tuple):
        raise InvalidRequestError("Proposition evidence source_contributions must be a tuple")
    contributions = tuple(
        require_identifier(value, "Proposition evidence source contribution", MAX_RESOLVER_NAME_BYTES)
        for value in source_contributions
    )
    if not contributions or len(contributions) > MAX_PROPOSITION_SOURCE_CONTRIBUTIONS:
        raise InvalidRequestError(
            f"Proposition evidence source_contributions must contain 1 through {MAX_PROPOSITION_SOURCE_CONTRIBUTIONS} values"
        )
    if contributions != tuple(sorted(set(contributions))):
        raise InvalidRequestError("Proposition evidence source_contributions must be unique and sorted")
    if source not in contributions:
        raise InvalidRequestError("Proposition evidence source_resolver must be present in source_contributions")
    if trusted_components:
        validated_features = features
        validated_references = canonical_references
        validated_validity = validity
        validated_trust = trust
        validated_disclosure = disclosure
    else:
        try:
            validated_features = validate_feature_set(features)
        except InvalidRequestError as err:
            raise InvalidRequestError("Proposition evidence features must be a FeatureSet") from err
        try:
            validated_references = validate_canonical_proposition_references(canonical_references)
        except InvalidRequestError as err:
            raise InvalidRequestError("Proposition evidence canonical_references must be CanonicalPropositionReferences") from err
        try:
            validated_validity = validate_proposition_validity_inputs(validity)
        except InvalidRequestError as err:
            raise InvalidRequestError("Proposition evidence validity must be PropositionValidityInputs") from err
        try:
            validated_trust = validate_proposition_trust_inputs(trust)
        except InvalidRequestError as err:
            raise InvalidRequestError("Proposition evidence trust must be PropositionTrustInputs") from err
        try:
            validated_disclosure = validate_disclosure_decision(disclosure)
        except InvalidRequestError as err:
            raise InvalidRequestError("Proposition evidence disclosure must be DisclosureDecision") from err
    if not isinstance(path, tuple):
        raise InvalidRequestError("Proposition evidence path must be a tuple")
    # A direct record's path is its own Proposition ID; a composed record's path is its steps.
    if all(isinstance(value, str) for value in path):
        normalized_path: tuple[object, ...] = tuple(
            require_identifier(value, "Proposition evidence path identifier", maximum_bytes=MAX_PROPOSITION_IDENTIFIER_BYTES)
            for value in path
        )
        if normalized_path != (normalized_proposition_id,):
            raise InvalidRequestError("Section 7 Proposition evidence path must be the singleton proposition_id")
    else:
        normalized_path = tuple(validate_proposition_evidence_path_step(value) for value in path)
        if not 1 <= len(normalized_path) <= MAX_COMPOSITION_PATH_PROPOSITIONS:
            raise InvalidRequestError(
                f"composed Proposition evidence path must contain 1 through {MAX_COMPOSITION_PATH_PROPOSITIONS} steps"
            )
        steps = normalized_path
        if tuple(step.get("position", 0) for step in steps) != tuple(range(len(steps))):
            raise InvalidRequestError("composed Proposition evidence path positions must be contiguous and ordered")
        proposition_ids = tuple(step.get("proposition_id", "") for step in steps)
        if len(set(proposition_ids)) != len(proposition_ids) or normalized_proposition_id not in proposition_ids:
            raise InvalidRequestError(
                "composed Proposition evidence path must contain unique Propositions including proposition_id"
            )
        entity_ids = [steps[0].get("subject_entity_id", "")]
        for index, step in enumerate(steps):
            entity_ids.append(step.get("object_entity_id", ""))
            if index and (
                steps[index - 1].get("object_entity_id", "") != step.get("subject_entity_id", "")
                or steps[index - 1].get("output_binding", "") != step.get("input_binding", "")
            ):
                raise InvalidRequestError("composed Proposition evidence path bindings are not contiguous")
        if len(set(entity_ids)) != len(entity_ids):
            raise InvalidRequestError("composed Proposition evidence path contains a cycle")
    if not isinstance(selection_reasons, tuple):
        raise InvalidRequestError("Proposition evidence selection_reasons must be a tuple")
    reasons = tuple(
        require_identifier(value, "Proposition evidence selection reason", MAX_REASON_CODE_BYTES) for value in selection_reasons
    )
    if not reasons or len(reasons) > MAX_PROPOSITION_SELECTION_REASONS:
        raise InvalidRequestError(
            f"Proposition evidence selection_reasons must contain 1 through {MAX_PROPOSITION_SELECTION_REASONS} values"
        )
    if reasons != tuple(sorted(set(reasons))):
        raise InvalidRequestError("Proposition evidence selection_reasons must be unique and sorted")
    result: dict = {
        "proposition_id": normalized_proposition_id,
        "source_resolver": source,
        "source_contributions": contributions,
        "features": validated_features,
        "canonical_references": validated_references,
        "validity": validated_validity,
        "trust": validated_trust,
        "disclosure": validated_disclosure,
        "path": normalized_path,
        "selection_reasons": reasons,
    }
    return result


def validate_proposition_evidence_record(value: object) -> dict:
    """Revalidate and defensively copy one full-Proposition evidence dictionary."""
    data = exact_mapping(value, "PropositionEvidenceRecord", PROPOSITION_EVIDENCE_RECORD_FIELDS)
    result = proposition_evidence_record(
        data.get("proposition_id", ""),
        data.get("source_resolver", ""),
        data.get("source_contributions", ()),
        data.get("features", {}),
        data.get("canonical_references", {}),
        data.get("validity", {}),
        data.get("trust", {}),
        data.get("disclosure", {}),
        data.get("path", ()),
        data.get("selection_reasons", ()),
    )
    return result


def proposition_evidence_record_with_changes(value: object, changes: object) -> dict:
    """Apply named fields and revalidate one full-Proposition evidence dictionary."""
    record = validate_proposition_evidence_record(value)
    if not isinstance(changes, dict):
        raise InvalidRequestError("Proposition evidence changes must be an object")
    if not set(changes).issubset(PROPOSITION_EVIDENCE_RECORD_FIELDS):
        raise InvalidRequestError("Proposition evidence changes contain an unknown field")
    updated: dict[str, object] = dict(record)
    updated.update(changes)
    result = validate_proposition_evidence_record(updated)
    return result


def proposition_evidence_record_to_dict(value: object) -> dict[str, object]:
    """Serialize one full-Proposition evidence record."""
    record = validate_proposition_evidence_record(value)
    result = trusted_proposition_evidence_record_to_dict(record)
    return result


def trusted_proposition_evidence_record_to_dict(record: dict) -> dict[str, object]:
    """Serialize a Proposition-evidence record already validated at a public boundary."""
    features = record.get("features", {})
    disclosure = record.get("disclosure", {})
    result = {
        "proposition_id": record.get("proposition_id", ""),
        "source_resolver": record.get("source_resolver", ""),
        "source_contributions": list(record.get("source_contributions", ())),
        "features": {
            "values": dict(features.get("values", {})),
            "unavailable": list(features.get("unavailable", ())),
        },
        "canonical_references": dict(record.get("canonical_references", {})),
        "validity": trusted_proposition_validity_inputs_to_dict(record.get("validity", {})),
        "trust": dict(record.get("trust", {})),
        "disclosure": {
            "ownership": disclosure.get("ownership", PropositionOwnership.PUBLIC).value,
            "basis": disclosure.get("basis", DisclosureBasis.PUBLIC_RULE).value,
            "scope": dict(disclosure.get("scope", {})),
            "authority": disclosure.get("authority", ""),
            "authority_available": disclosure.get("authority_available", False),
        },
        "path": (
            list(record.get("path", ()))
            if all(isinstance(step, str) for step in record.get("path", ()))
            else [proposition_evidence_path_step_to_dict(step) for step in record.get("path", ())]
        ),
        "selection_reasons": list(record.get("selection_reasons", ())),
    }
    return result


def proposition_evidence_record_from_dict(value: object) -> dict:
    """Decode one full-Proposition evidence record from its exact serialized form."""
    data = exact_mapping(value, "PropositionEvidenceRecord", PROPOSITION_EVIDENCE_RECORD_FIELDS)
    contributions = require_list(data.get("source_contributions", []), "Proposition evidence source_contributions")
    path = require_list(data.get("path", []), "Proposition evidence path")
    reasons = require_list(data.get("selection_reasons", []), "Proposition evidence selection_reasons")
    normalized_contributions = tuple(
        require_identifier(item, "Proposition evidence source contribution", MAX_RESOLVER_NAME_BYTES) for item in contributions
    )
    normalized_path: tuple[object, ...]
    if all(isinstance(item, str) for item in path):
        normalized_path = tuple(
            require_identifier(item, "Proposition evidence path identifier", maximum_bytes=MAX_PROPOSITION_IDENTIFIER_BYTES)
            for item in path
        )
    else:
        normalized_path = tuple(
            proposition_evidence_path_step_from_dict(freeze_mapping(item, "Proposition evidence path step")) for item in path
        )
    normalized_reasons = tuple(
        require_identifier(item, "Proposition evidence selection reason", MAX_REASON_CODE_BYTES) for item in reasons
    )
    validated_features = feature_set_from_dict(freeze_mapping(data.get("features", {}), "Proposition evidence features"))
    validated_references = canonical_proposition_references_from_dict(
        freeze_mapping(data.get("canonical_references", {}), "Proposition evidence canonical_references")
    )
    validated_validity = proposition_validity_inputs_from_dict(
        freeze_mapping(data.get("validity", {}), "Proposition evidence validity")
    )
    validated_trust = proposition_trust_inputs_from_dict(freeze_mapping(data.get("trust", {}), "Proposition evidence trust"))
    validated_disclosure = disclosure_decision_from_dict(
        freeze_mapping(data.get("disclosure", {}), "Proposition evidence disclosure")
    )
    result = proposition_evidence_record(
        data.get("proposition_id", ""),
        data.get("source_resolver", ""),
        normalized_contributions,
        validated_features,
        validated_references,
        validated_validity,
        validated_trust,
        validated_disclosure,
        normalized_path,
        normalized_reasons,
    )
    return result


def proposition_evidence_record_to_json(value: object) -> str:
    """Serialize one full-Proposition evidence record deterministically."""
    payload = proposition_evidence_record_to_dict(value)
    result = json_text(payload)
    return result


def proposition_evidence_record_from_json(value: object) -> dict:
    """Decode one full-Proposition evidence record from deterministic JSON."""
    if not isinstance(value, str):
        raise InvalidRequestError("PropositionEvidenceRecord JSON must be a string")
    data = load_json_mapping(value, "PropositionEvidenceRecord JSON")
    result = proposition_evidence_record_from_dict(data)
    return result
