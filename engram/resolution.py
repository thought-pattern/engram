"""Transport-neutral contracts for the bounded unified resolution pipeline."""

from engram.artifacts import LifecycleState
from engram.constants import (
    ACCOUNTING_OBSERVATION_FIELDS,
    CANDIDATE_FIELDS,
    EMPTY_MAPPING,
    EMPTY_SCOPE_KEY,
    EVIDENCE_REFERENCE_FIELDS,
    MAX_ACCOUNTING_KEYWORD_BYTES,
    MAX_ACCOUNTING_KEYWORDS,
    MAX_CANDIDATE_ID_BYTES,
    MAX_ENUM_WIRE_BYTES,
    MAX_EVIDENCE_ID_BYTES,
    MAX_REASON_CODE_BYTES,
    MAX_RESOLUTION_REASON_CODES,
    MAX_RESOLUTION_VALUES,
    MAX_RESOLVER_NAME_BYTES,
    MAX_RESPONSE_BYTES,
    MAX_STATEMENT_ID_BYTES,
    RESOLUTION_RESULT_FIELDS,
    RESOLVER_RESULT_FIELDS,
    CandidateSource,
    EvidenceKind,
    ResolutionOutcome,
    ResolverState,
)
from engram.errors import IdentityValidationError, InvalidRequestError
from engram.identity import (
    scope_key_from_dict,
    scope_key_to_dict,
    validate_scope_key,
)
from engram.resolution_budget import (
    budget_consumption,
    budget_consumption_from_dict,
    budget_consumption_to_dict,
    validate_budget_consumption,
)
from engram.resolution_evidence import (
    feature_set,
    feature_set_from_dict,
    proposition_evidence_record_from_dict,
    trusted_proposition_evidence_record_to_dict,
    validate_feature_set,
    validate_proposition_evidence_record,
)
from engram.resolution_package import (
    empty_evidence_package,
    evidence_package_from_dict,
    evidence_package_payload,
    validate_evidence_package,
)
from engram.resolution_values import (
    exact_mapping,
    freeze_mapping,
    json_text,
    load_json_mapping,
    require_float,
    require_list,
    thaw_json,
)
from engram.validation import require_any_text


def evidence_reference(
    evidence_id: object,
    resolver: object,
    kind: object,
    scope: object,
    provenance: object = EMPTY_MAPPING,
    diagnostics: object = EMPTY_MAPPING,
) -> dict:
    """Build one minimal stable evidence reference safe for Section 4 results."""
    normalized_id = require_any_text(evidence_id, "evidence_id", MAX_EVIDENCE_ID_BYTES, allow_empty=False)
    normalized_resolver = require_any_text(resolver, "evidence resolver", MAX_RESOLVER_NAME_BYTES, allow_empty=False)
    if not isinstance(kind, EvidenceKind):
        raise InvalidRequestError("evidence kind must be an EvidenceKind")
    try:
        validated_scope = validate_scope_key(scope)
    except IdentityValidationError as err:
        raise InvalidRequestError("evidence scope must be a ScopeKey") from err
    result: dict = {
        "evidence_id": normalized_id,
        "resolver": normalized_resolver,
        "kind": kind,
        "scope": validated_scope,
        "provenance": freeze_mapping(provenance, "evidence provenance"),
        "diagnostics": freeze_mapping(diagnostics, "evidence diagnostics"),
    }
    return result


def validate_evidence_reference(value: object) -> dict:
    """Revalidate and defensively copy one evidence-reference dictionary."""
    data = exact_mapping(value, "EvidenceReference", EVIDENCE_REFERENCE_FIELDS)
    result = evidence_reference(
        data.get("evidence_id", ""),
        data.get("resolver", ""),
        data.get("kind", EvidenceKind.PROPOSITION),
        data.get("scope", {}),
        data.get("provenance", {}),
        data.get("diagnostics", {}),
    )
    return result


def evidence_reference_to_dict(value: object) -> dict[str, object]:
    """Serialize one evidence reference."""
    reference = validate_evidence_reference(value)
    result = trusted_evidence_reference_to_dict(reference)
    return result


def trusted_evidence_reference_to_dict(reference: dict) -> dict[str, object]:
    """Serialize an evidence reference already validated at a public boundary."""
    result = {
        "evidence_id": reference.get("evidence_id", ""),
        "resolver": reference.get("resolver", ""),
        "kind": reference.get("kind", EvidenceKind.PROPOSITION).value,
        "scope": scope_key_to_dict(reference.get("scope", {})),
        "provenance": thaw_json(reference.get("provenance", {})),
        "diagnostics": thaw_json(reference.get("diagnostics", {})),
    }
    return result


def evidence_reference_from_dict(value: object) -> dict:
    """Decode one evidence reference from its exact serialized form."""
    data = exact_mapping(value, "EvidenceReference", EVIDENCE_REFERENCE_FIELDS)
    try:
        kind = EvidenceKind(require_any_text(data.get("kind", ""), "evidence kind", MAX_ENUM_WIRE_BYTES, allow_empty=False))
    except ValueError as err:
        raise InvalidRequestError("unsupported evidence kind") from err
    result = evidence_reference(
        data.get("evidence_id", ""),
        data.get("resolver", ""),
        kind,
        scope_key_from_dict(freeze_mapping(data.get("scope", {}), "evidence scope")),
        freeze_mapping(data.get("provenance", {}), "evidence provenance"),
        freeze_mapping(data.get("diagnostics", {}), "evidence diagnostics"),
    )
    return result


def evidence_reference_to_json(value: object) -> str:
    payload = evidence_reference_to_dict(value)
    result = json_text(payload)
    return result


def candidate(
    candidate_id: object,
    statement_id: object,
    response: object,
    source: object,
    features: object,
    evidence: object,
    scope: object,
    lifecycle: object,
    provenance: object = EMPTY_MAPPING,
    diagnostics: object = EMPTY_MAPPING,
) -> dict:
    """Build one response candidate emitted by a resolver without selecting it."""
    if not isinstance(source, CandidateSource):
        raise InvalidRequestError("candidate source must be a CandidateSource")
    try:
        validated_features = validate_feature_set(features)
    except InvalidRequestError as err:
        raise InvalidRequestError("candidate features must be a FeatureSet") from err
    if not isinstance(evidence, tuple):
        raise InvalidRequestError("candidate evidence must be a tuple of EvidenceReference values")
    try:
        validated_evidence = tuple(validate_evidence_reference(item) for item in evidence)
    except InvalidRequestError as err:
        raise InvalidRequestError("candidate evidence must be a tuple of EvidenceReference values") from err
    if len(validated_evidence) > MAX_RESOLUTION_VALUES:
        raise InvalidRequestError(f"candidate evidence exceeds the limit of {MAX_RESOLUTION_VALUES}")
    try:
        validated_scope = validate_scope_key(scope)
    except IdentityValidationError as err:
        raise InvalidRequestError("candidate scope must be a ScopeKey") from err
    if not isinstance(lifecycle, LifecycleState):
        raise InvalidRequestError("candidate lifecycle must be a LifecycleState")
    result: dict = {
        "candidate_id": require_any_text(candidate_id, "candidate_id", MAX_CANDIDATE_ID_BYTES, allow_empty=False),
        "statement_id": require_any_text(
            statement_id,
            "candidate statement_id",
            MAX_STATEMENT_ID_BYTES,
            allow_empty=False,
        ),
        "response": require_any_text(response, "candidate response", MAX_RESPONSE_BYTES, allow_empty=False),
        "source": source,
        "features": validated_features,
        "evidence": validated_evidence,
        "scope": validated_scope,
        "lifecycle": lifecycle,
        "provenance": freeze_mapping(provenance, "candidate provenance"),
        "diagnostics": freeze_mapping(diagnostics, "candidate diagnostics"),
    }
    return result


def validate_candidate(value: object) -> dict:
    data = exact_mapping(value, "Candidate", CANDIDATE_FIELDS)
    result = candidate(
        candidate_id=data.get("candidate_id", ""),
        statement_id=data.get("statement_id", ""),
        response=data.get("response", ""),
        source=data.get("source", CandidateSource.EXACT),
        features=data.get("features", {}),
        evidence=data.get("evidence", ()),
        scope=data.get("scope", {}),
        lifecycle=data.get("lifecycle", LifecycleState.ACTIVE),
        provenance=data.get("provenance", {}),
        diagnostics=data.get("diagnostics", {}),
    )
    return result


def trusted_candidate(
    candidate_id: str,
    statement_id: str,
    response: str,
    source: CandidateSource,
    features: dict,
    evidence: tuple[dict, ...],
    scope: dict,
    lifecycle: LifecycleState,
    provenance: dict[str, object] = EMPTY_MAPPING,
    diagnostics: dict[str, object] = EMPTY_MAPPING,
) -> dict:
    """Build a candidate from values established inside the resolution engine."""
    result: dict = {
        "candidate_id": candidate_id,
        "statement_id": statement_id,
        "response": response,
        "source": source,
        "features": features,
        "evidence": evidence,
        "scope": scope,
        "lifecycle": lifecycle,
        "provenance": freeze_mapping(provenance, "candidate provenance"),
        "diagnostics": freeze_mapping(diagnostics, "candidate diagnostics"),
    }
    return result


def trusted_candidate_with_changes(value: dict, changes: dict[str, object]) -> dict:
    """Copy an engine-owned candidate and apply engine-established fields."""
    updated: dict[str, object] = dict(value)
    updated.update(changes)
    result = updated
    return result


def candidate_to_dict(value: object) -> dict[str, object]:
    current = validate_candidate(value)
    result = trusted_candidate_to_dict(current)
    return result


def trusted_candidate_to_dict(current: dict) -> dict[str, object]:
    """Serialize a candidate already validated at a public boundary."""
    features = current.get("features", {})
    result = {
        "candidate_id": current.get("candidate_id", ""),
        "statement_id": current.get("statement_id", ""),
        "response": current.get("response", ""),
        "source": current.get("source", CandidateSource.EXACT).value,
        "features": {
            "values": dict(features.get("values", {})),
            "unavailable": list(features.get("unavailable", ())),
        },
        "evidence": [trusted_evidence_reference_to_dict(item) for item in current.get("evidence", ())],
        "scope": dict(current.get("scope", {})),
        "lifecycle": current.get("lifecycle", LifecycleState.RETIRED).value,
        "provenance": thaw_json(current.get("provenance", {})),
        "diagnostics": thaw_json(current.get("diagnostics", {})),
    }
    return result


def candidate_from_dict(value: object) -> dict:
    data = exact_mapping(value, "Candidate", CANDIDATE_FIELDS)
    try:
        source = CandidateSource(
            require_any_text(data.get("source", ""), "candidate source", MAX_ENUM_WIRE_BYTES, allow_empty=False)
        )
        lifecycle = LifecycleState(
            require_any_text(data.get("lifecycle", ""), "candidate lifecycle", MAX_ENUM_WIRE_BYTES, allow_empty=False)
        )
    except ValueError as err:
        raise InvalidRequestError("candidate source or lifecycle is unsupported") from err
    evidence = require_list(data.get("evidence", []), "candidate evidence")
    result = candidate(
        candidate_id=data.get("candidate_id", ""),
        statement_id=data.get("statement_id", ""),
        response=data.get("response", ""),
        source=source,
        features=feature_set_from_dict(freeze_mapping(data.get("features", {}), "candidate features")),
        evidence=tuple(evidence_reference_from_dict(freeze_mapping(item, "candidate evidence item")) for item in evidence),
        scope=scope_key_from_dict(freeze_mapping(data.get("scope", {}), "candidate scope")),
        lifecycle=lifecycle,
        provenance=freeze_mapping(data.get("provenance", {}), "candidate provenance"),
        diagnostics=freeze_mapping(data.get("diagnostics", {}), "candidate diagnostics"),
    )
    return result


def trusted_candidate_to_json(value: dict) -> str:
    """Serialize a candidate already validated at a public boundary."""
    payload = trusted_candidate_to_dict(value)
    result = json_text(payload)
    return result


def empty_candidate() -> dict:
    """Return one isolated concrete empty candidate, the null object for an unavailable selection."""
    result = candidate(
        candidate_id="empty",
        statement_id="empty",
        response="empty",
        source=CandidateSource.EXACT,
        features=feature_set(),
        evidence=(),
        scope=EMPTY_SCOPE_KEY,
        lifecycle=LifecycleState.ACTIVE,
    )
    return result


def accounting_observation(
    statement_id: object,
    keywords: object = (),
) -> dict:
    """Build one pure resolver observation applied only by the finalizer."""
    if not isinstance(keywords, tuple):
        raise InvalidRequestError("accounting keywords must be a tuple")
    if len(keywords) > MAX_ACCOUNTING_KEYWORDS:
        raise InvalidRequestError(f"accounting keywords exceeds the limit of {MAX_ACCOUNTING_KEYWORDS}")
    normalized_keywords = tuple(
        require_any_text(keyword, "accounting keyword", MAX_ACCOUNTING_KEYWORD_BYTES, allow_empty=False) for keyword in keywords
    )
    if len(set(normalized_keywords)) != len(normalized_keywords):
        raise InvalidRequestError("accounting keywords must be unique")
    result: dict = {
        "statement_id": require_any_text(
            statement_id,
            "accounting statement_id",
            MAX_STATEMENT_ID_BYTES,
            allow_empty=False,
        ),
        "keywords": normalized_keywords,
    }
    return result


def validate_accounting_observation(value: object) -> dict:
    data = exact_mapping(value, "AccountingObservation", ACCOUNTING_OBSERVATION_FIELDS)
    result = accounting_observation(data.get("statement_id", ""), data.get("keywords", ()))
    return result


def accounting_observation_to_dict(value: object) -> dict[str, object]:
    observation = validate_accounting_observation(value)
    result = {
        "statement_id": observation.get("statement_id", ""),
        "keywords": list(observation.get("keywords", ())),
    }
    return result


def accounting_observation_from_dict(value: object) -> dict:
    data = exact_mapping(value, "AccountingObservation", ACCOUNTING_OBSERVATION_FIELDS)
    keywords = require_list(data.get("keywords", []), "accounting keywords")
    result = accounting_observation(data.get("statement_id", ""), tuple(keywords))
    return result


def resolver_result(
    resolver: object,
    state: object,
    reason_code: object = "",
    candidates: object = (),
    evidence: object = (),
    proposition_evidence: object = (),
    accounting: object = (),
    diagnostics: object = EMPTY_MAPPING,
    consumption: object = EMPTY_MAPPING,
) -> dict:
    """Build the bounded output from one side-effect-free resolver invocation."""
    resolver_name = require_any_text(resolver, "resolver result resolver", MAX_RESOLVER_NAME_BYTES, allow_empty=False)
    if not isinstance(state, ResolverState):
        raise InvalidRequestError("resolver result state must be a ResolverState")
    validated_reason_code = require_any_text(
        reason_code,
        "resolver result reason_code",
        MAX_REASON_CODE_BYTES,
        allow_empty=True,
    )
    if (
        not isinstance(candidates, tuple)
        or not isinstance(evidence, tuple)
        or not isinstance(proposition_evidence, tuple)
        or not isinstance(accounting, tuple)
    ):
        raise InvalidRequestError("resolver outputs and accounting must be tuples")
    # Counts are refused before any item is validated or copied.
    if max(len(candidates), len(evidence), len(proposition_evidence), len(accounting)) > MAX_RESOLUTION_VALUES:
        raise InvalidRequestError(f"resolver output exceeds the item limit of {MAX_RESOLUTION_VALUES}")
    try:
        validated_candidates = tuple(validate_candidate(value) for value in candidates)
    except InvalidRequestError as err:
        raise InvalidRequestError("resolver candidates must be a tuple of Candidate values") from err
    try:
        validated_evidence = tuple(validate_evidence_reference(value) for value in evidence)
    except InvalidRequestError as err:
        raise InvalidRequestError("resolver evidence must be a tuple of EvidenceReference values") from err
    try:
        validated_proposition_evidence = tuple(validate_proposition_evidence_record(value) for value in proposition_evidence)
    except InvalidRequestError as err:
        raise InvalidRequestError("resolver proposition_evidence must be a tuple of PropositionEvidenceRecord values") from err
    try:
        validated_accounting = tuple(validate_accounting_observation(value) for value in accounting)
    except InvalidRequestError as err:
        raise InvalidRequestError("resolver accounting must be a tuple of AccountingObservation values") from err
    if state != ResolverState.COMPLETED and (
        validated_candidates or validated_evidence or validated_proposition_evidence or validated_accounting
    ):
        raise InvalidRequestError("non-completed resolver results cannot contain output or accounting")
    if any(
        value.get("source_resolver", "") != resolver_name or value.get("source_contributions", ()) != (resolver_name,)
        for value in validated_proposition_evidence
    ):
        raise InvalidRequestError("resolver proposition_evidence source must match its producing resolver")
    frozen_diagnostics = freeze_mapping(diagnostics, "resolver diagnostics")
    if not isinstance(consumption, dict):
        raise InvalidRequestError("resolver consumption must be a BudgetConsumption")
    try:
        validated_consumption = budget_consumption() if not consumption else validate_budget_consumption(consumption)
    except InvalidRequestError as err:
        raise InvalidRequestError("resolver consumption must be a BudgetConsumption") from err
    result: dict = {
        "resolver": resolver_name,
        "state": state,
        "reason_code": validated_reason_code,
        "candidates": validated_candidates,
        "evidence": validated_evidence,
        "proposition_evidence": validated_proposition_evidence,
        "accounting": validated_accounting,
        "diagnostics": frozen_diagnostics,
        "consumption": validated_consumption,
    }
    return result


def validate_resolver_result(value: object) -> dict:
    data = exact_mapping(value, "ResolverResult", RESOLVER_RESULT_FIELDS)
    result = resolver_result(
        data.get("resolver", ""),
        data.get("state", ResolverState.FAILED),
        data.get("reason_code", ""),
        data.get("candidates", ()),
        data.get("evidence", ()),
        data.get("proposition_evidence", ()),
        data.get("accounting", ()),
        data.get("diagnostics", {}),
        data.get("consumption", {}),
    )
    return result


def trusted_resolver_result_with_changes(
    value: dict,
    changes: dict[str, object],
) -> dict:
    """Copy an executor-owned resolver result and apply orchestrator fields."""
    updated: dict[str, object] = dict(value)
    updated.update(changes)
    result = updated
    return result


def resolver_result_to_dict(value: object) -> dict[str, object]:
    current = validate_resolver_result(value)
    result = trusted_resolver_result_to_dict(current)
    return result


def trusted_resolver_result_to_dict(current: dict) -> dict[str, object]:
    """Serialize a resolver result already validated by the executor."""
    result = {
        "resolver": current.get("resolver", ""),
        "state": current.get("state", ResolverState.FAILED).value,
        "reason_code": current.get("reason_code", ""),
        "candidates": [trusted_candidate_to_dict(item) for item in current.get("candidates", ())],
        "evidence": [trusted_evidence_reference_to_dict(item) for item in current.get("evidence", ())],
        "proposition_evidence": [
            trusted_proposition_evidence_record_to_dict(item) for item in current.get("proposition_evidence", ())
        ],
        "accounting": [accounting_observation_to_dict(item) for item in current.get("accounting", ())],
        "diagnostics": thaw_json(current.get("diagnostics", {})),
        "consumption": budget_consumption_to_dict(current.get("consumption", {})),
    }
    return result


def resolver_result_to_json(value: object) -> str:
    payload = resolver_result_to_dict(value)
    result = json_text(payload)
    return result


def resolver_result_from_dict(value: object) -> dict:
    data = exact_mapping(value, "ResolverResult", RESOLVER_RESULT_FIELDS)
    try:
        state = ResolverState(require_any_text(data.get("state", ""), "resolver state", MAX_ENUM_WIRE_BYTES, allow_empty=False))
    except ValueError as err:
        raise InvalidRequestError("unsupported resolver state") from err
    candidates = require_list(data.get("candidates", []), "resolver candidates")
    evidence = require_list(data.get("evidence", []), "resolver evidence")
    proposition_evidence = require_list(data.get("proposition_evidence", []), "resolver Proposition evidence")
    accounting = require_list(data.get("accounting", []), "resolver accounting")
    if max(len(candidates), len(evidence), len(proposition_evidence), len(accounting)) > MAX_RESOLUTION_VALUES:
        raise InvalidRequestError(f"resolver output exceeds the item limit of {MAX_RESOLUTION_VALUES}")
    result = resolver_result(
        resolver=require_any_text(data.get("resolver", ""), "resolver result resolver", MAX_RESOLVER_NAME_BYTES, allow_empty=False),
        state=state,
        reason_code=require_any_text(
            data.get("reason_code", ""), "resolver result reason_code", MAX_REASON_CODE_BYTES, allow_empty=True
        ),
        candidates=tuple(candidate_from_dict(freeze_mapping(item, "resolver candidate")) for item in candidates),
        evidence=tuple(evidence_reference_from_dict(freeze_mapping(item, "resolver evidence item")) for item in evidence),
        proposition_evidence=tuple(
            proposition_evidence_record_from_dict(freeze_mapping(item, "resolver Proposition evidence item"))
            for item in proposition_evidence
        ),
        accounting=tuple(accounting_observation_from_dict(freeze_mapping(item, "resolver accounting item")) for item in accounting),
        diagnostics=freeze_mapping(data.get("diagnostics", {}), "resolver diagnostics"),
        consumption=budget_consumption_from_dict(freeze_mapping(data.get("consumption", {}), "resolver consumption")),
    )
    return result


def resolver_result_from_json(value: str) -> dict:
    decoded = load_json_mapping(value, "ResolverResult JSON")
    result = resolver_result_from_dict(decoded)
    return result


def resolution_result(
    outcome: object,
    selected_candidate: object,
    selected_candidate_available: object,
    response_candidates: object,
    evidence: object,
    confidence: object,
    confidence_available: object,
    reason_codes: object,
    frame_diagnostics: object,
    resolver_results: object,
    budget: object,
    evidence_package_available: object = False,
    evidence_package: object = EMPTY_MAPPING,
) -> dict:
    """Build a strict unified result with concrete ANSWER/EVIDENCE/MISS invariants."""
    if not isinstance(outcome, ResolutionOutcome):
        raise InvalidRequestError("resolution outcome must be a ResolutionOutcome")
    try:
        validated_selected_candidate = validate_candidate(selected_candidate)
    except InvalidRequestError as err:
        raise InvalidRequestError("selected_candidate must be a Candidate") from err
    if (
        not isinstance(selected_candidate_available, bool)
        or not isinstance(confidence_available, bool)
        or not isinstance(evidence_package_available, bool)
    ):
        raise InvalidRequestError("resolution availability fields must be booleans")
    if not isinstance(response_candidates, tuple):
        raise InvalidRequestError("response_candidates must be a tuple of Candidate values")
    if not isinstance(evidence, tuple):
        raise InvalidRequestError("resolution evidence must be a tuple of EvidenceReference values")
    if not isinstance(resolver_results, tuple):
        raise InvalidRequestError("resolver_results must be a tuple of ResolverResult values")
    # Counts are refused before any candidate, reference or resolver result is validated or copied.
    if len(response_candidates) > MAX_RESOLUTION_VALUES or len(evidence) > MAX_RESOLUTION_VALUES:
        raise InvalidRequestError(f"resolution output exceeds the item limit of {MAX_RESOLUTION_VALUES}")
    if len(resolver_results) > MAX_RESOLUTION_REASON_CODES:
        raise InvalidRequestError(f"resolver_results exceeds the limit of {MAX_RESOLUTION_REASON_CODES}")
    try:
        validated_response_candidates = tuple(validate_candidate(value) for value in response_candidates)
    except InvalidRequestError as err:
        raise InvalidRequestError("response_candidates must be a tuple of Candidate values") from err
    try:
        validated_evidence = tuple(validate_evidence_reference(value) for value in evidence)
    except InvalidRequestError as err:
        raise InvalidRequestError("resolution evidence must be a tuple of EvidenceReference values") from err
    validated_confidence = require_float(confidence, "resolution confidence", 0.0, 1.0)
    if not isinstance(reason_codes, tuple):
        raise InvalidRequestError("reason_codes must be a tuple")
    if len(reason_codes) > MAX_RESOLUTION_REASON_CODES:
        raise InvalidRequestError(f"reason_codes exceeds the limit of {MAX_RESOLUTION_REASON_CODES}")
    validated_reason_codes = tuple(
        require_any_text(value, "reason code", MAX_REASON_CODE_BYTES, allow_empty=False) for value in reason_codes
    )
    if validated_reason_codes != tuple(dict.fromkeys(validated_reason_codes)):
        raise InvalidRequestError("reason_codes must be unique and ordered")
    frozen_diagnostics = freeze_mapping(frame_diagnostics, "frame diagnostics")
    try:
        validated_resolver_results = tuple(validate_resolver_result(value) for value in resolver_results)
    except InvalidRequestError as err:
        raise InvalidRequestError("resolver_results must be a tuple of ResolverResult values") from err
    if any(value.get("proposition_evidence", ()) for value in validated_resolver_results):
        raise InvalidRequestError("resolution resolver_results cannot expose unpackaged Proposition evidence")
    try:
        validated_budget = validate_budget_consumption(budget)
    except InvalidRequestError as err:
        raise InvalidRequestError("resolution budget must be a BudgetConsumption") from err
    if not isinstance(evidence_package, dict):
        raise InvalidRequestError("evidence_package must be an EvidencePackage")
    try:
        validated_evidence_package = (
            empty_evidence_package() if not evidence_package else validate_evidence_package(evidence_package)
        )
    except InvalidRequestError as err:
        raise InvalidRequestError("evidence_package must be an EvidencePackage") from err
    concrete_empty_package = empty_evidence_package()
    if not evidence_package_available and validated_evidence_package != concrete_empty_package:
        raise InvalidRequestError("unavailable evidence_package must use the concrete empty package")
    if outcome == ResolutionOutcome.ANSWER:
        if not selected_candidate_available:
            raise InvalidRequestError("ANSWER requires one selected candidate")
        if validated_response_candidates != (validated_selected_candidate,):
            raise InvalidRequestError("ANSWER response_candidates must contain only the selected candidate")
        if validated_evidence:
            raise InvalidRequestError("ANSWER cannot contain top-level evidence")
        if not confidence_available or validated_confidence <= 0.0:
            raise InvalidRequestError("ANSWER requires available positive confidence")
        if evidence_package_available or validated_evidence_package != concrete_empty_package:
            raise InvalidRequestError("ANSWER cannot contain a response-less evidence package")
    else:
        if selected_candidate_available:
            raise InvalidRequestError("only ANSWER can make selected_candidate available")
        if validated_selected_candidate != empty_candidate():
            raise InvalidRequestError("an unavailable selected_candidate must be the concrete empty candidate")
        if confidence_available or validated_confidence != 0.0:
            raise InvalidRequestError("non-ANSWER confidence must be unavailable and zero")
    retained_evidence_available = bool(
        validated_response_candidates or validated_evidence or validated_evidence_package.get("records", ())
    )
    if outcome == ResolutionOutcome.EVIDENCE and not retained_evidence_available:
        raise InvalidRequestError("EVIDENCE requires response candidates, evidence references, or package records")
    if outcome == ResolutionOutcome.MISS and retained_evidence_available:
        raise InvalidRequestError("MISS cannot contain response candidates or retained evidence")
    result: dict = {
        "outcome": outcome,
        "selected_candidate": validated_selected_candidate,
        "selected_candidate_available": selected_candidate_available,
        "response_candidates": validated_response_candidates,
        "evidence": validated_evidence,
        "confidence": validated_confidence,
        "confidence_available": confidence_available,
        "reason_codes": validated_reason_codes,
        "frame_diagnostics": frozen_diagnostics,
        "resolver_results": validated_resolver_results,
        "budget": validated_budget,
        "evidence_package_available": evidence_package_available,
        "evidence_package": validated_evidence_package,
    }
    return result


def trusted_resolution_result(
    outcome: ResolutionOutcome,
    selected_candidate: dict,
    selected_candidate_available: bool,
    response_candidates: tuple[dict, ...],
    evidence: tuple[dict, ...],
    confidence: float,
    confidence_available: bool,
    reason_codes: tuple[str, ...],
    frame_diagnostics: dict,
    resolver_results: tuple[dict, ...],
    budget: dict,
    evidence_package_available: bool,
    evidence_package: dict,
) -> dict:
    """Build a result from values whose invariants the orchestrator established."""
    result: dict = {
        "outcome": outcome,
        "selected_candidate": trusted_candidate_with_changes(selected_candidate, {}),
        "selected_candidate_available": selected_candidate_available,
        "response_candidates": tuple(trusted_candidate_with_changes(value, {}) for value in response_candidates),
        "evidence": evidence,
        "confidence": confidence,
        "confidence_available": confidence_available,
        "reason_codes": reason_codes,
        "frame_diagnostics": freeze_mapping(frame_diagnostics, "frame diagnostics"),
        "resolver_results": resolver_results,
        "budget": budget,
        "evidence_package_available": evidence_package_available,
        "evidence_package": evidence_package,
    }
    return result


def validate_resolution_result(value: object) -> dict:
    data = exact_mapping(value, "ResolutionResult", RESOLUTION_RESULT_FIELDS)
    result = resolution_result(
        data.get("outcome", ResolutionOutcome.MISS),
        data.get("selected_candidate", {}),
        data.get("selected_candidate_available", False),
        data.get("response_candidates", ()),
        data.get("evidence", ()),
        data.get("confidence", 0.0),
        data.get("confidence_available", False),
        data.get("reason_codes", ()),
        data.get("frame_diagnostics", {}),
        data.get("resolver_results", ()),
        data.get("budget", {}),
        data.get("evidence_package_available", False),
        data.get("evidence_package", {}),
    )
    return result


def resolution_result_with_changes(value: object, changes: object) -> dict:
    current = validate_resolution_result(value)
    if not isinstance(changes, dict) or not set(changes).issubset(RESOLUTION_RESULT_FIELDS):
        raise InvalidRequestError("resolution result changes contain invalid fields")
    updated: dict[str, object] = dict(current)
    updated.update(changes)
    result = validate_resolution_result(updated)
    return result


def resolution_result_to_dict(value: object) -> dict:
    current = validate_resolution_result(value)
    result = trusted_resolution_result_to_dict(current)
    return result


def trusted_resolution_result_to_dict(current: dict) -> dict:
    """Serialize a result already validated or built by the orchestrator."""
    selected = (
        trusted_candidate_to_dict(current.get("selected_candidate", {}))
        if current.get("selected_candidate_available", False)
        else {}
    )
    result = {
        "outcome": current.get("outcome", ResolutionOutcome.MISS).value,
        "selected_candidate": selected,
        "selected_candidate_available": current.get("selected_candidate_available", False),
        "response_candidates": [trusted_candidate_to_dict(item) for item in current.get("response_candidates", ())],
        "evidence": [trusted_evidence_reference_to_dict(item) for item in current.get("evidence", ())],
        "confidence": current.get("confidence", 0.0),
        "confidence_available": current.get("confidence_available", False),
        "reason_codes": list(current.get("reason_codes", ())),
        "frame_diagnostics": thaw_json(current.get("frame_diagnostics", {})),
        "resolver_results": [trusted_resolver_result_to_dict(item) for item in current.get("resolver_results", ())],
        "budget": budget_consumption_to_dict(current.get("budget", {})),
        "evidence_package_available": current.get("evidence_package_available", False),
        "evidence_package": evidence_package_payload(current.get("evidence_package", {})),
    }
    return result


def resolution_result_to_json(value: object) -> str:
    payload = resolution_result_to_dict(value)
    result = json_text(payload)
    return result


def trusted_resolution_result_to_json(value: dict) -> str:
    """Serialize an orchestrator-owned result without redundant revalidation."""
    payload = trusted_resolution_result_to_dict(value)
    result = json_text(payload)
    return result


def resolution_result_from_dict(value: object) -> dict:
    data = exact_mapping(value, "ResolutionResult", RESOLUTION_RESULT_FIELDS)
    try:
        outcome = ResolutionOutcome(
            require_any_text(data.get("outcome", ""), "resolution outcome", MAX_ENUM_WIRE_BYTES, allow_empty=False)
        )
    except ValueError as err:
        raise InvalidRequestError("unsupported resolution outcome") from err
    selected_available = data.get("selected_candidate_available", False)
    if not isinstance(selected_available, bool):
        raise InvalidRequestError("selected_candidate_available must be a boolean")
    selected_mapping = freeze_mapping(data.get("selected_candidate", {}), "selected_candidate")
    selected = candidate_from_dict(selected_mapping) if selected_available else empty_candidate()
    if not selected_available and selected_mapping:
        raise InvalidRequestError("unavailable selected_candidate must be an empty object")
    confidence_available = data.get("confidence_available", False)
    if not isinstance(confidence_available, bool):
        raise InvalidRequestError("confidence_available must be a boolean")
    response_candidates = require_list(data.get("response_candidates", []), "response_candidates")
    evidence = require_list(data.get("evidence", []), "resolution evidence")
    reasons = require_list(data.get("reason_codes", []), "reason_codes")
    resolver_results = require_list(data.get("resolver_results", []), "resolver_results")
    if len(response_candidates) > MAX_RESOLUTION_VALUES or len(evidence) > MAX_RESOLUTION_VALUES:
        raise InvalidRequestError(f"resolution output exceeds the item limit of {MAX_RESOLUTION_VALUES}")
    if len(reasons) > MAX_RESOLUTION_REASON_CODES or len(resolver_results) > MAX_RESOLUTION_REASON_CODES:
        raise InvalidRequestError(f"reason_codes and resolver_results are limited to {MAX_RESOLUTION_REASON_CODES} values")
    package_available = data.get("evidence_package_available", False)
    if not isinstance(package_available, bool):
        raise InvalidRequestError("evidence_package_available must be a boolean")
    result = resolution_result(
        outcome=outcome,
        selected_candidate=selected,
        selected_candidate_available=selected_available,
        response_candidates=tuple(candidate_from_dict(freeze_mapping(item, "response candidate")) for item in response_candidates),
        evidence=tuple(evidence_reference_from_dict(freeze_mapping(item, "resolution evidence item")) for item in evidence),
        confidence=require_float(data.get("confidence", 0.0), "resolution confidence", 0.0, 1.0),
        confidence_available=confidence_available,
        reason_codes=tuple(require_any_text(item, "reason code", MAX_REASON_CODE_BYTES, allow_empty=False) for item in reasons),
        frame_diagnostics=freeze_mapping(data.get("frame_diagnostics", {}), "frame diagnostics"),
        resolver_results=tuple(resolver_result_from_dict(freeze_mapping(item, "resolver result")) for item in resolver_results),
        budget=budget_consumption_from_dict(freeze_mapping(data.get("budget", {}), "resolution budget")),
        evidence_package_available=package_available,
        evidence_package=evidence_package_from_dict(freeze_mapping(data.get("evidence_package", {}), "evidence package")),
    )
    return result
