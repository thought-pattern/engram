"""Section 1 contract, normalization, extraction, and conformance tests."""

import json
from pathlib import Path

import pytest

from engram.errors import IdentityValidationError, UnsupportedIdentityVersionError
from engram.identity import (
    IDENTITY_SCHEMA_VERSION,
    MAX_CANONICAL_FORM_BYTES,
    MAX_CONTEXT_FINGERPRINT_BYTES,
    MAX_RETRIEVAL_ALIASES,
    MAX_RETRIEVAL_REPRESENTATION_BYTES,
    RETRIEVAL_NORMALIZATION_VERSION,
    QualifierKind,
    QueryOperator,
    RetrievalOrigin,
    build_retrieval_representation,
    build_scoped_retrieval_key,
    build_standalone_identity,
    entity_reference,
    extract_entities_and_identifiers,
    extract_lexical_terms,
    extract_operator,
    extract_qualifiers,
    extract_relation_surface,
    identity_qualifier,
    load_authoritative_identity,
    normalize_retrieval_key,
    query_identity,
    query_identity_from_dict,
    query_identity_from_json,
    query_identity_to_dict,
    query_identity_to_json,
    relation_reference,
    retrieval_representation,
    retrieval_representation_bindings,
    retrieval_representation_from_json,
    retrieval_representation_to_dict,
    retrieval_representation_to_json,
    scope_key,
    scope_key_from_dict,
    scope_key_from_json,
    scope_key_signature,
    scope_key_to_dict,
    scope_key_to_json,
    scoped_retrieval_key_from_json,
    scoped_retrieval_key_signature,
    scoped_retrieval_key_to_dict,
    scoped_retrieval_key_to_json,
    validate_authoritative_identity,
    validate_entity_reference,
    validate_identity_qualifier,
    validate_scope_key,
    validate_scoped_retrieval_key,
)

NORMALIZATION_FIXTURE = Path(__file__).parent / "fixtures" / "identity" / "normalization-v1.json"


def _fixture() -> dict:
    result = json.loads(NORMALIZATION_FIXTURE.read_text(encoding="utf-8"))
    return result


def _none_paths(value, path: str = "root") -> list[str]:
    if value is None:
        result = [path]
        return result
    if isinstance(value, dict):
        result = [nested for key, item in value.items() for nested in _none_paths(item, f"{path}.{key}")]
        return result
    if isinstance(value, (list, tuple, set)):
        result = [nested for index, item in enumerate(value) for nested in _none_paths(item, f"{path}[{index}]")]
        return result
    result = []
    return result


def test_scope_key_codec_equality_and_order_are_deterministic() -> None:
    empty = scope_key()
    support = scope_key(namespace="support", context_fingerprint="account-tier:pro")
    restored = scope_key_from_json(scope_key_to_json(support))

    assert restored == support
    assert type(restored) is dict
    assert scope_key_to_dict(restored) == {
        "schema_version": 1,
        "namespace": "support",
        "context_fingerprint": "account-tier:pro",
    }
    assert sorted([scope_key_signature(support), scope_key_signature(empty)]) == [
        scope_key_signature(empty),
        scope_key_signature(support),
    ]
    assert scope_key_to_json(support) == scope_key_to_json(restored)


@pytest.mark.parametrize(
    ("value", "message"),
    [
        ({"schema_version": 2, "namespace": "", "context_fingerprint": ""}, "unsupported scope schema_version"),
        ({"schema_version": 1, "namespace": "", "context_fingerprint": "", "extra": ""}, "unsupported fields"),
        ({"schema_version": 1, "namespace": [], "context_fingerprint": ""}, "namespace must be a string"),
    ],
)
def test_scope_key_invalid_scope_payloads_fail_explicitly(value: dict, message: str) -> None:
    with pytest.raises(IdentityValidationError, match=message):
        scope_key_from_dict(value)


def test_scope_key_scope_bounds_are_utf8_bytes_and_controls_are_rejected() -> None:
    scope_key(context_fingerprint="x" * MAX_CONTEXT_FINGERPRINT_BYTES)
    with pytest.raises(IdentityValidationError, match="exceeds"):
        scope_key(context_fingerprint="é" * (MAX_CONTEXT_FINGERPRINT_BYTES // 2 + 1))
    with pytest.raises(IdentityValidationError, match="control"):
        scope_key(namespace="bad\nnamespace")


def test_scope_key_scope_validation_revalidates_and_copies_mutable_input() -> None:
    source = scope_key(namespace="support", context_fingerprint="pro")
    validated = validate_scope_key(source)

    assert type(validated) is dict
    assert validated == source
    assert validated is not source
    source["namespace"] = "mutated"
    assert validated["namespace"] == "support"


def test_identity_contracts_component_and_query_identity_codecs_round_trip() -> None:
    query = query_identity(
        canonical_form="birth date of alan turing",
        operator=QueryOperator.WHEN,
        entities=(entity_reference("Alan Turing", "entity:alan-turing"),),
        relation=relation_reference("born", "predicate:date_of_birth"),
        qualifiers=(identity_qualifier(QualifierKind.HISTORICAL, "historical"),),
        lexical_terms=("alan", "turing", "born"),
        scope=scope_key("biography", ""),
    )

    restored = query_identity_from_json(query_identity_to_json(query))

    assert restored == query
    assert query_identity_to_json(restored) == query_identity_to_json(query)
    assert _none_paths(query_identity_to_dict(query)) == []
    assert "null" not in query_identity_to_json(query)


def test_identity_contracts_unknown_operator_and_empty_relation_are_concrete() -> None:
    query = query_identity(canonical_form="opaque request")

    assert query["operator"] == QueryOperator.UNKNOWN
    assert query["entities"] == ()
    assert query["relation"] == relation_reference()
    assert query["qualifiers"] == ()
    assert query["lexical_terms"] == ()
    assert query["scope"] == scope_key()


def test_identity_contracts_unsupported_versions_and_unknown_fields_are_rejected() -> None:
    payload = query_identity_to_dict(build_standalone_identity("Who created Python?"))
    payload["schema_version"] = IDENTITY_SCHEMA_VERSION + 1
    with pytest.raises(UnsupportedIdentityVersionError, match="identity schema_version"):
        query_identity_from_dict(payload)

    payload = query_identity_to_dict(build_standalone_identity("Who created Python?"))
    payload["unexpected"] = "value"
    with pytest.raises(IdentityValidationError, match="unsupported fields"):
        query_identity_from_dict(payload)


@pytest.mark.parametrize(
    "factory",
    [
        lambda: entity_reference("Ada Lovelace", "not a canonical id"),
        lambda: relation_reference("", "predicate:born"),
        lambda: identity_qualifier(QualifierKind.CURRENT, "Not Normalized"),
        lambda: query_identity(canonical_form="Not Normalized"),
        lambda: query_identity(canonical_form="normalized", lexical_terms=("two words",)),
    ],
)
def test_identity_contracts_malformed_components_fail_without_reinterpretation(factory) -> None:
    with pytest.raises(IdentityValidationError):
        factory()


def test_identity_contracts_leaf_records_are_exact_revalidated_dictionaries() -> None:
    entity = entity_reference("Alan Turing", "entity:alan-turing")
    qualifier = identity_qualifier(QualifierKind.HISTORICAL, "historical")
    query = query_identity(canonical_form="alan turing", entities=(entity,), qualifiers=(qualifier,))

    assert type(entity) is dict
    assert type(query) is dict
    assert type(query["entities"][0]) is dict
    assert type(query["qualifiers"][0]) is dict
    entity["surface"] = "mutated"
    assert query["entities"][0]["surface"] == "Alan Turing"

    malformed_entity = dict(query["entities"][0])
    malformed_entity["unexpected"] = "value"
    with pytest.raises(IdentityValidationError, match="unsupported fields"):
        validate_entity_reference(malformed_entity)

    malformed_qualifier = dict(query["qualifiers"][0])
    malformed_qualifier["value"] = "Not Normalized"
    with pytest.raises(IdentityValidationError, match="already use retrieval normalization"):
        validate_identity_qualifier(malformed_qualifier)


@pytest.mark.parametrize("case", _fixture()["normalization_cases"], ids=lambda case: case["id"])
def test_retrieval_normalization_golden_normalization_cases(case: dict) -> None:
    assert normalize_retrieval_key(case["input"]) == case["expected"]


@pytest.mark.parametrize("case", _fixture()["normalization_cases"], ids=lambda case: case["id"])
def test_retrieval_normalization_normalization_is_idempotent(case: dict) -> None:
    once = normalize_retrieval_key(case["input"])
    assert normalize_retrieval_key(once) == once


def test_retrieval_normalization_empty_and_punctuation_only_inputs_are_concrete() -> None:
    assert normalize_retrieval_key("") == ""
    assert normalize_retrieval_key("?!…") == ""


def test_retrieval_normalization_normalization_version_is_explicit() -> None:
    with pytest.raises(UnsupportedIdentityVersionError, match="normalization_version"):
        normalize_retrieval_key("request", RETRIEVAL_NORMALIZATION_VERSION + 1)


def test_retrieval_normalization_generated_normalization_corpus_is_idempotent() -> None:
    stems = ("Latency", "Version v1.2", "$PATH", "A|B", "2*3", "E-1234")
    wrappers = ("{}", "  {}  ", "What is {}?", "‘{}’", "[{}]")

    for stem in stems:
        for wrapper in wrappers:
            normalized = normalize_retrieval_key(wrapper.format(stem))
            assert normalize_retrieval_key(normalized) == normalized


@pytest.mark.parametrize("symbol", ["<", ">", "<=", ">=", "==", "!=", "$", "%", "|", "&", "*"])
def test_retrieval_normalization_identity_bearing_symbols_survive_normalization(symbol: str) -> None:
    assert symbol in normalize_retrieval_key(f"left {symbol} right")


def test_scoped_retrieval_and_representations_scoped_key_codec_and_scope_separation() -> None:
    support = build_scoped_retrieval_key(scope_key("support", "pro"), "What’s the port?")
    billing = build_scoped_retrieval_key(scope_key("billing", "pro"), "What is the port?")

    assert type(support) is dict
    assert support["normalized_key"] == "what is the port"
    assert support != billing
    assert scoped_retrieval_key_from_json(scoped_retrieval_key_to_json(support)) == support

    validated = validate_scoped_retrieval_key(support)
    assert validated is not support
    assert validated["scope"] is not support["scope"]
    support["normalized_key"] = "mutated"
    support["scope"]["namespace"] = "mutated"
    assert validated["normalized_key"] == "what is the port"
    assert validated["scope"]["namespace"] == "support"


def test_scoped_retrieval_and_representations_representation_deduplicates_by_normalized_key_and_retains_provenance() -> None:
    retrieval = retrieval_representation(
        canonical="What's the default PostgreSQL port?",
        aliases=(
            "What is the default PostgreSQL port?",
            "Postgres default port",
            "POSTGRES   DEFAULT PORT!",
        ),
    )
    bindings = retrieval_representation_bindings(retrieval, scope_key("support", "pro"))

    assert all(type(binding) is dict for binding in bindings)
    assert retrieval["aliases"] == ("Postgres default port",)
    assert [binding["origin"] for binding in bindings] == [RetrievalOrigin.CANONICAL, RetrievalOrigin.ALIAS]
    assert [binding["representation"] for binding in bindings] == [
        "What's the default PostgreSQL port?",
        "Postgres default port",
    ]
    assert len({scoped_retrieval_key_signature(binding["key"]) for binding in bindings}) == 2
    assert retrieval_representation_from_json(retrieval_representation_to_json(retrieval)) == retrieval
    assert "pattern_aliases" not in retrieval_representation_to_dict(retrieval)
    assert "response" not in retrieval_representation_to_dict(retrieval)


def test_scoped_retrieval_and_representations_representation_enforces_bounds_and_concrete_tuple_input() -> None:
    with pytest.raises(IdentityValidationError, match="must be a tuple"):
        retrieval_representation("request", aliases=["alias"])
    with pytest.raises(IdentityValidationError, match="exceed"):
        retrieval_representation(
            "request",
            aliases=tuple(f"alias {index}" for index in range(MAX_RETRIEVAL_ALIASES + 1)),
        )
    with pytest.raises(IdentityValidationError, match="non-whitespace"):
        build_retrieval_representation("   ")


def test_scoped_retrieval_and_representations_maximum_alias_payload_round_trips_through_json_codec() -> None:
    aliases = tuple(
        f"alias-{index}-" + "x" * (MAX_RETRIEVAL_REPRESENTATION_BYTES - len(f"alias-{index}-"))
        for index in range(MAX_RETRIEVAL_ALIASES)
    )
    retrieval = retrieval_representation("canonical request", aliases)

    assert retrieval_representation_from_json(retrieval_representation_to_json(retrieval)) == retrieval


@pytest.mark.parametrize(
    ("input_text", "expected"),
    [
        ("Who created Python?", QueryOperator.WHO),
        ("What created Python?", QueryOperator.WHAT),
        ("Where was Ada Lovelace born?", QueryOperator.WHERE),
        ("When was Ada Lovelace born?", QueryOperator.WHEN),
        ("Which release is current?", QueryOperator.WHICH),
        ("Why was it retired?", QueryOperator.WHY),
        ("How does it work?", QueryOperator.HOW),
        ("How many releases exist?", QueryOperator.HOW_MANY),
        ("Find PostgreSQL", QueryOperator.LOOKUP),
        ("Are there supported releases?", QueryOperator.EXISTS),
        ("Count supported releases", QueryOperator.COUNT),
        ("Compare Python versus Ruby", QueryOperator.COMPARE),
        ("What—exactly—is PostgreSQL?", QueryOperator.WHAT),
        ("An opaque request", QueryOperator.UNKNOWN),
    ],
)
def test_identity_extraction_closed_operator_vocabulary(input_text: str, expected: QueryOperator) -> None:
    assert extract_operator(input_text) == expected


def test_identity_extraction_qualifiers_are_preserved_outside_lexical_terms() -> None:
    request = "Which current releases do not support feature X after 2020?"
    operator = extract_operator(request)
    qualifiers = extract_qualifiers(request, operator)
    lexical_terms = extract_lexical_terms(request)

    assert identity_qualifier(QualifierKind.NEGATION, "not") in qualifiers
    assert identity_qualifier(QualifierKind.CURRENT, "current") in qualifiers
    assert identity_qualifier(QualifierKind.TEMPORAL, "after 2020") in qualifiers
    assert "which" not in lexical_terms
    assert "not" not in lexical_terms
    assert "current" in lexical_terms


@pytest.mark.parametrize("symbol", ["<", ">", "<=", ">=", "==", "!="])
def test_identity_extraction_symbolic_comparisons_are_typed_qualifiers(symbol: str) -> None:
    request = f"Is latency {symbol} 100 ms?"
    qualifiers = extract_qualifiers(request, extract_operator(request))

    assert identity_qualifier(QualifierKind.COMPARISON, symbol) in qualifiers


def test_identity_extraction_entity_and_technical_identifier_extraction_is_surface_only() -> None:
    entities = extract_entities_and_identifiers(
        "Compare Ada Lovelace with PostgreSQL v16.2 at config/engram.yml after RFC 7231, error E-1234, and C++."
    )
    surfaces = [entity["surface"] for entity in entities]

    assert "Ada Lovelace" in surfaces
    assert "PostgreSQL" in surfaces
    assert "v16.2" in surfaces
    assert "config/engram.yml" in surfaces
    assert "RFC 7231" in surfaces
    assert all(entity["canonical_id"] == "" for entity in entities)


@pytest.mark.parametrize(
    ("input_text", "expected"),
    [
        ("When was Ada Lovelace born?", "born"),
        ("Where was Ada Lovelace born?", "born"),
        ("What port does PostgreSQL use?", "use"),
        ("What features does Engram support in v1?", "support"),
        ("What company did Microsoft acquire in 2020?", "acquire"),
        ("What port does PostgreSQL use in production?", "use"),
        ("Who created Python?", "created"),
        ("What is the current Python version?", ""),
        ("What is the Spring building?", ""),
        ("How many Python releases exist?", ""),
    ],
)
def test_identity_extraction_relation_extraction_abstains_when_uncertain(input_text: str, expected: str) -> None:
    operator = extract_operator(input_text)
    entities = extract_entities_and_identifiers(input_text)
    assert extract_relation_surface(input_text, operator, entities)["surface"] == expected


def test_identity_extraction_standalone_builder_is_deterministic_and_preserves_semantic_contrasts() -> None:
    scope = scope_key("biography", "public")
    when = build_standalone_identity("When was Ada Lovelace born?", scope)
    where = build_standalone_identity("Where was Ada Lovelace born?", scope)

    assert build_standalone_identity("When was Ada Lovelace born?", scope) == when
    assert when["operator"] == QueryOperator.WHEN
    assert where["operator"] == QueryOperator.WHERE
    assert when["lexical_terms"] == where["lexical_terms"]
    assert when["canonical_form"] != where["canonical_form"]
    assert build_scoped_retrieval_key(scope, when["canonical_form"]) != build_scoped_retrieval_key(
        scope,
        where["canonical_form"],
    )


def test_authoritative_identity_valid_authoritative_identity_is_preserved_exactly() -> None:
    authoritative = query_identity(
        canonical_form="birth date of alan turing",
        operator=QueryOperator.WHEN,
        entities=(entity_reference("Alan Turing", "entity:alan-turing"),),
        relation=relation_reference("born", "predicate:date_of_birth"),
        lexical_terms=("alan", "turing", "born"),
        scope=scope_key("biography", "released"),
    )
    retrieval = retrieval_representation(
        canonical="When was Alan Turing born?",
        aliases=("What is Alan Turing's birth date?",),
    )

    validated = validate_authoritative_identity(authoritative, retrieval)
    decoded_identity, decoded_retrieval = load_authoritative_identity(
        query_identity_to_dict(authoritative),
        retrieval_representation_to_dict(retrieval),
    )

    assert validated is authoritative
    assert decoded_identity == authoritative
    assert decoded_retrieval == retrieval
    assert decoded_identity["entities"][0]["surface"] == "Alan Turing"
    assert decoded_retrieval["canonical"] == "When was Alan Turing born?"


def test_authoritative_identity_authoritative_contract_rejects_null_malformed_and_oversized_input() -> None:
    identity = query_identity_to_dict(build_standalone_identity("Who created Python?"))
    retrieval = retrieval_representation_to_dict(build_retrieval_representation("Who created Python?"))

    identity["relation"] = None
    with pytest.raises(IdentityValidationError, match="identity relation must be an object"):
        load_authoritative_identity(identity, retrieval)

    with pytest.raises(IdentityValidationError, match="exceeds"):
        query_identity(canonical_form="x" * (MAX_CANONICAL_FORM_BYTES + 1))


@pytest.mark.parametrize("case", _fixture()["identity_contrasts"], ids=lambda case: case["id"])
def test_identity_conformance_corpus_adversarial_pairs_produce_distinct_scoped_keys(case: dict) -> None:
    scope = scope_key("conformance", "v1")
    left = build_standalone_identity(case["left"], scope)
    right = build_standalone_identity(case["right"], scope)

    assert left != right
    assert build_scoped_retrieval_key(scope, left["canonical_form"]) != build_scoped_retrieval_key(
        scope,
        right["canonical_form"],
    )


def test_identity_conformance_corpus_same_language_in_different_scopes_produces_distinct_keys() -> None:
    request = "What are the support hours?"
    support = build_standalone_identity(request, scope_key("support", "pro"))
    billing = build_standalone_identity(request, scope_key("billing", "pro"))

    assert support["canonical_form"] == billing["canonical_form"]
    assert support["scope"] != billing["scope"]
    assert build_scoped_retrieval_key(support["scope"], support["canonical_form"]) != build_scoped_retrieval_key(
        billing["scope"], billing["canonical_form"]
    )


def test_identity_conformance_corpus_generated_scoped_key_properties() -> None:
    requests = tuple(case["input"] for case in _fixture()["normalization_cases"])
    scopes = (scope_key(), scope_key("support", "free"), scope_key("support", "pro"))

    for request in requests:
        keys = tuple(build_scoped_retrieval_key(scope, request) for scope in scopes)
        assert len({scoped_retrieval_key_signature(key) for key in keys}) == len(scopes)
        for key in keys:
            assert scoped_retrieval_key_from_json(scoped_retrieval_key_to_json(key)) == key
            assert build_scoped_retrieval_key(key["scope"], key["normalized_key"]) == key


def test_identity_conformance_corpus_contract_outputs_are_recursively_concrete() -> None:
    identity = build_standalone_identity("Where is PostgreSQL v16.2 supported?", scope_key("support", "v1"))
    retrieval = build_retrieval_representation(
        "Where is PostgreSQL v16.2 supported?",
        ("PostgreSQL v16.2 support location",),
    )
    bindings = retrieval_representation_bindings(retrieval, identity["scope"])
    outputs = {
        "identity": query_identity_to_dict(identity),
        "retrieval": retrieval_representation_to_dict(retrieval),
        "keys": [scoped_retrieval_key_to_dict(binding["key"]) for binding in bindings],
    }

    assert _none_paths(outputs) == []
    assert "null" not in json.dumps(outputs, sort_keys=True)
