"""QueryFrame interpretation, provenance, and trusted request clock capture.

This owner retains original identity, request limits, scope, and temporal
interpretation. It performs no resolver execution or release judgment.
"""

from hashlib import sha256 as hashlib_sha256

from engram.constants import (
    EMPTY_MAPPING,
    EMPTY_SCOPE_KEY,
    INHERITANCE_PROVENANCE_FIELDS,
    MAX_DIAGNOSTIC_ID_BYTES,
    MAX_ENUM_WIRE_BYTES,
    MAX_INHERITANCE_FIELD_NAME_BYTES,
    MAX_INHERITANCE_SOURCE_TURN,
    MAX_REQUEST_BYTES,
    MAX_REQUIRED_SOURCE_LABEL_BYTES,
    MAX_REWRITE_RULE_ID_BYTES,
    MAX_TRACE_STEPS,
    QUERY_FRAME_FIELDS,
    REWRITE_TRACE_STEP_FIELDS,
    ExpectedObjectType,
)
from engram.eligibility import (
    capture_standalone_eligibility_context,
    eligibility_context_from_dict,
    eligibility_context_to_dict,
    validate_eligibility_context,
)
from engram.errors import IdentityValidationError, InvalidRequestError
from engram.identity import (
    extract_standalone_identity,
    query_identity_from_dict,
    query_identity_to_dict,
    query_identity_to_json,
    retrieval_representation,
    scope_key_from_dict,
    scope_key_to_dict,
    validate_authoritative_identity,
    validate_query_identity,
    validate_scope_key,
)
from engram.resolution_budget import (
    capture_resolution_budget,
    recapture_resolution_budget,
    resolution_budget_from_dict,
    resolution_budget_to_dict,
    validate_resolution_budget,
)
from engram.resolution_values import exact_mapping, freeze_mapping, require_int, require_list, thaw_json
from engram.substitutions import expand_contractions
from engram.temporal import (
    parse_temporal_query,
    temporal_query,
    temporal_query_from_dict,
    temporal_query_to_dict,
    validate_temporal_query,
)
from engram.validation import require_any_text


def inheritance_provenance(field_name: object, source_turn: object) -> dict:
    """Build concrete empty-capable contextual field provenance owned by Section 8."""
    result: dict = {
        "field_name": require_any_text(field_name, "inheritance field_name", MAX_INHERITANCE_FIELD_NAME_BYTES, allow_empty=False),
        "source_turn": require_int(source_turn, "inheritance source_turn", 1, MAX_INHERITANCE_SOURCE_TURN),
    }
    return result


def validate_inheritance_provenance(value: object) -> dict:
    data = exact_mapping(value, "InheritanceProvenance", INHERITANCE_PROVENANCE_FIELDS)
    result = inheritance_provenance(data.get("field_name", ""), data.get("source_turn", 0))
    return result


def inheritance_provenance_signature(value: object) -> tuple[str, int]:
    provenance = validate_inheritance_provenance(value)
    result = (provenance.get("field_name", ""), provenance.get("source_turn", 0))
    return result


def inheritance_provenance_to_dict(value: object) -> dict[str, object]:
    provenance = validate_inheritance_provenance(value)
    result: dict[str, object] = dict(provenance)
    return result


def inheritance_provenance_from_dict(value: object) -> dict:
    result = validate_inheritance_provenance(value)
    return result


def rewrite_trace_step(rule_id: object, input_text: object, output_text: object) -> dict:
    """Build an empty-capable rewrite trace whose rule semantics belong to Section 11."""
    result: dict = {
        "rule_id": require_any_text(rule_id, "rewrite rule_id", MAX_REWRITE_RULE_ID_BYTES, allow_empty=False),
        "input_text": require_any_text(input_text, "rewrite input_text", MAX_REQUEST_BYTES, allow_empty=False),
        "output_text": require_any_text(output_text, "rewrite output_text", MAX_REQUEST_BYTES, allow_empty=False),
    }
    return result


def validate_rewrite_trace_step(value: object) -> dict:
    data = exact_mapping(value, "RewriteTraceStep", REWRITE_TRACE_STEP_FIELDS)
    result = rewrite_trace_step(data.get("rule_id", ""), data.get("input_text", ""), data.get("output_text", ""))
    return result


def rewrite_trace_step_to_dict(value: object) -> dict[str, object]:
    step = validate_rewrite_trace_step(value)
    result: dict[str, object] = dict(step)
    return result


def rewrite_trace_step_from_dict(value: object) -> dict:
    result = validate_rewrite_trace_step(value)
    return result


def query_frame(
    original_text: object,
    resolved_text: object,
    identity: object,
    expected_object_type: object,
    inheritance: object,
    rewrite_chain: object,
    scope: object,
    required_metadata: object,
    required_source_label: object,
    budget: object,
    eligibility_context: object,
    diagnostic_id: object,
    temporal_query_value: object = (),
) -> dict:
    """Build the immutable base interpretation passed to every resolver."""
    original = require_any_text(original_text, "frame original_text", MAX_REQUEST_BYTES, allow_empty=False)
    resolved = require_any_text(resolved_text, "frame resolved_text", MAX_REQUEST_BYTES, allow_empty=False)
    try:
        validated_identity = validate_query_identity(identity)
    except IdentityValidationError as err:
        raise InvalidRequestError("frame identity must be a QueryIdentity") from err
    if not isinstance(expected_object_type, ExpectedObjectType):
        raise InvalidRequestError("frame expected_object_type must be an ExpectedObjectType")
    try:
        validated_temporal = temporal_query() if temporal_query_value == () else validate_temporal_query(temporal_query_value)
    except InvalidRequestError as err:
        raise InvalidRequestError("frame temporal_query must be a TemporalQuery") from err
    if not isinstance(inheritance, tuple):
        raise InvalidRequestError("frame inheritance must be a tuple of InheritanceProvenance values")
    try:
        validated_inheritance = tuple(validate_inheritance_provenance(value) for value in inheritance)
    except InvalidRequestError as err:
        raise InvalidRequestError("frame inheritance must be a tuple of InheritanceProvenance values") from err
    inheritance_signatures = tuple(inheritance_provenance_signature(value) for value in validated_inheritance)
    if len(validated_inheritance) > MAX_TRACE_STEPS or len(set(inheritance_signatures)) != len(validated_inheritance):
        raise InvalidRequestError("frame inheritance must be bounded and unique")
    if not isinstance(rewrite_chain, tuple):
        raise InvalidRequestError("frame rewrite_chain must be a tuple of RewriteTraceStep values")
    try:
        validated_rewrite_chain = tuple(validate_rewrite_trace_step(value) for value in rewrite_chain)
    except InvalidRequestError as err:
        raise InvalidRequestError("frame rewrite_chain must be a tuple of RewriteTraceStep values") from err
    if len(validated_rewrite_chain) > MAX_TRACE_STEPS:
        raise InvalidRequestError(f"frame rewrite_chain exceeds the limit of {MAX_TRACE_STEPS}")
    try:
        validated_scope = validate_scope_key(scope)
    except IdentityValidationError as err:
        raise InvalidRequestError("frame scope must match identity scope") from err
    if validated_identity.get("scope", {}) != validated_scope:
        raise InvalidRequestError("frame scope must match identity scope")
    frozen_metadata = freeze_mapping(required_metadata, "frame required_metadata")
    source_label = require_any_text(
        required_source_label,
        "frame required_source_label",
        MAX_REQUIRED_SOURCE_LABEL_BYTES,
        allow_empty=True,
    )
    try:
        validated_budget = validate_resolution_budget(budget)
    except InvalidRequestError as err:
        raise InvalidRequestError("frame budget must be a ResolutionBudget") from err
    try:
        validated_context = validate_eligibility_context(eligibility_context)
    except InvalidRequestError as err:
        raise InvalidRequestError("frame eligibility_context must be an EligibilityContext") from err
    if validated_context.get("namespace", "") != validated_scope.get("namespace", ""):
        raise InvalidRequestError("frame eligibility context namespace must match scope")
    validated_diagnostic_id = require_any_text(
        diagnostic_id,
        "frame diagnostic_id",
        MAX_DIAGNOSTIC_ID_BYTES,
        allow_empty=False,
    )
    result: dict = {
        "original_text": original,
        "resolved_text": resolved,
        "identity": validated_identity,
        "expected_object_type": expected_object_type,
        "temporal_query": validated_temporal,
        "inheritance": validated_inheritance,
        "rewrite_chain": validated_rewrite_chain,
        "scope": validated_scope,
        "required_metadata": frozen_metadata,
        "required_source_label": source_label,
        "budget": validated_budget,
        "eligibility_context": validated_context,
        "diagnostic_id": validated_diagnostic_id,
    }
    return result


def validate_query_frame(value: object) -> dict:
    data = exact_mapping(value, "QueryFrame", QUERY_FRAME_FIELDS)
    result = query_frame(
        data.get("original_text", ""),
        data.get("resolved_text", ""),
        data.get("identity", {}),
        data.get("expected_object_type", ExpectedObjectType.UNKNOWN),
        data.get("inheritance", ()),
        data.get("rewrite_chain", ()),
        data.get("scope", {}),
        data.get("required_metadata", {}),
        data.get("required_source_label", ""),
        data.get("budget", {}),
        data.get("eligibility_context", {}),
        data.get("diagnostic_id", ""),
        data.get("temporal_query", {}),
    )
    return result


def query_frame_with_changes(value: object, changes: object) -> dict:
    current = validate_query_frame(value)
    if not isinstance(changes, dict) or not set(changes).issubset(QUERY_FRAME_FIELDS):
        raise InvalidRequestError("query frame changes contain invalid fields")
    updated: dict[str, object] = dict(current)
    updated.update(changes)
    result = validate_query_frame(updated)
    return result


def query_frame_to_dict(value: object) -> dict[str, object]:
    frame = validate_query_frame(value)
    result = {
        "original_text": frame.get("original_text", ""),
        "resolved_text": frame.get("resolved_text", ""),
        "identity": query_identity_to_dict(frame.get("identity", {})),
        "expected_object_type": frame.get("expected_object_type", ExpectedObjectType.UNKNOWN).value,
        "temporal_query": temporal_query_to_dict(frame.get("temporal_query", {})),
        "inheritance": [inheritance_provenance_to_dict(item) for item in frame.get("inheritance", ())],
        "rewrite_chain": [rewrite_trace_step_to_dict(item) for item in frame.get("rewrite_chain", ())],
        "scope": scope_key_to_dict(frame.get("scope", {})),
        "required_metadata": thaw_json(frame.get("required_metadata", {})),
        "required_source_label": frame.get("required_source_label", ""),
        "budget": resolution_budget_to_dict(frame.get("budget", {})),
        "eligibility_context": eligibility_context_to_dict(frame.get("eligibility_context", {})),
        "diagnostic_id": frame.get("diagnostic_id", ""),
    }
    return result


def query_frame_from_dict(value: object) -> dict:
    data = exact_mapping(value, "QueryFrame", QUERY_FRAME_FIELDS)
    inheritance = require_list(data.get("inheritance", []), "frame inheritance")
    rewrites = require_list(data.get("rewrite_chain", []), "frame rewrite_chain")
    try:
        expected_type = ExpectedObjectType(
            require_any_text(data.get("expected_object_type", ""), "expected_object_type", MAX_ENUM_WIRE_BYTES, allow_empty=False)
        )
    except ValueError as err:
        raise InvalidRequestError("unsupported expected_object_type") from err
    result = query_frame(
        original_text=require_any_text(data.get("original_text", ""), "frame original_text", MAX_REQUEST_BYTES, allow_empty=False),
        resolved_text=require_any_text(data.get("resolved_text", ""), "frame resolved_text", MAX_REQUEST_BYTES, allow_empty=False),
        identity=query_identity_from_dict(thaw_json(freeze_mapping(data.get("identity", {}), "frame identity"))),
        expected_object_type=expected_type,
        temporal_query_value=temporal_query_from_dict(freeze_mapping(data.get("temporal_query", {}), "frame temporal_query")),
        inheritance=tuple(inheritance_provenance_from_dict(freeze_mapping(item, "inheritance item")) for item in inheritance),
        rewrite_chain=tuple(rewrite_trace_step_from_dict(freeze_mapping(item, "rewrite item")) for item in rewrites),
        scope=scope_key_from_dict(freeze_mapping(data.get("scope", {}), "frame scope")),
        required_metadata=freeze_mapping(data.get("required_metadata", {}), "frame required_metadata"),
        required_source_label=require_any_text(
            data.get("required_source_label", ""),
            "frame required_source_label",
            MAX_REQUIRED_SOURCE_LABEL_BYTES,
            allow_empty=True,
        ),
        budget=resolution_budget_from_dict(freeze_mapping(data.get("budget", {}), "frame budget")),
        eligibility_context=eligibility_context_from_dict(
            freeze_mapping(data.get("eligibility_context", {}), "frame eligibility_context")
        ),
        diagnostic_id=require_any_text(
            data.get("diagnostic_id", ""),
            "frame diagnostic_id",
            MAX_DIAGNOSTIC_ID_BYTES,
            allow_empty=False,
        ),
    )
    return result


class QueryFrameBuilder:
    """Trusted transport-neutral base-frame construction boundary."""

    def __init__(self, engram, monotonic_clock_ns: object, utc_clock: object) -> None:
        if not callable(monotonic_clock_ns) or not callable(utc_clock):
            raise InvalidRequestError("frame builder clocks must be callable")
        self.internal_engram = engram
        self.internal_monotonic_clock_ns = monotonic_clock_ns
        self.internal_utc_clock = utc_clock

    def build(
        self,
        request: str,
        scope: object = EMPTY_SCOPE_KEY,
        identity: dict[str, object] = EMPTY_MAPPING,
        required_metadata: dict[str, object] = EMPTY_MAPPING,
        required_source_label: str = "",
        diagnostic_seed: str = "",
        budget: dict[str, object] = EMPTY_MAPPING,
    ) -> dict:
        """Build the base QueryFrame for one request.

        A supplied ``identity`` is validated and must be authoritative for the
        request; without one, the standalone identity is extracted from the
        text. A supplied ``budget`` is re-captured against the monotonic clock;
        without one, the default budget is captured. Contractions are expanded
        into the resolved text when ``expand_contractions`` is configured, the
        eligibility context is captured from the UTC clock, and the diagnostic ID
        is the SHA-256 of ``diagnostic_seed`` or, without one, of the identity
        and resolved text.

        Raises:
            InvalidRequestError: the request, scope, identity, budget or seed is
                malformed, or the identity's scope differs from ``scope``.
        """
        original = require_any_text(request, "request", MAX_REQUEST_BYTES, allow_empty=False)
        try:
            scope = validate_scope_key(scope)
        except IdentityValidationError as err:
            raise InvalidRequestError("scope must be a ScopeKey") from err
        if not isinstance(identity, dict):
            raise InvalidRequestError("identity must be an object")
        if not isinstance(budget, dict):
            raise InvalidRequestError("budget must be an object")
        if identity:
            try:
                selected_identity = validate_query_identity(identity)
            except IdentityValidationError as err:
                raise InvalidRequestError("identity must be a QueryIdentity") from err
            validate_authoritative_identity(selected_identity, retrieval_representation(original))
        else:
            selected_identity = extract_standalone_identity(original, scope)
        if selected_identity.get("scope", {}) != scope:
            raise InvalidRequestError("identity scope must match frame scope")
        if budget:
            try:
                selected_budget = recapture_resolution_budget(budget, self.internal_monotonic_clock_ns)
            except InvalidRequestError as err:
                raise InvalidRequestError("budget must be a ResolutionBudget") from err
        else:
            selected_budget = capture_resolution_budget(self.internal_monotonic_clock_ns)
        resolved = original
        if self.internal_engram.config.get("expand_contractions", False):
            resolved = expand_contractions(original, self.internal_engram.substitution_maps.get("contractions", {}))
        eligibility = capture_standalone_eligibility_context(self.internal_utc_clock, scope, True)
        seed = diagnostic_seed or f"{query_identity_to_json(selected_identity)}:{resolved}"
        require_any_text(seed, "diagnostic_seed", MAX_REQUEST_BYTES * 4, allow_empty=False)
        diagnostic_id = f"resolution:sha256:{hashlib_sha256(seed.encode('utf-8')).hexdigest()}"
        result = query_frame(
            original_text=original,
            resolved_text=resolved,
            identity=selected_identity,
            expected_object_type=ExpectedObjectType.UNKNOWN,
            temporal_query_value=parse_temporal_query(original),
            inheritance=(),
            rewrite_chain=(),
            scope=scope,
            required_metadata=required_metadata,
            required_source_label=required_source_label,
            budget=selected_budget,
            eligibility_context=eligibility,
            diagnostic_id=diagnostic_id,
        )
        return result
