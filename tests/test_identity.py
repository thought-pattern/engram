"""Section 1 contract, normalization, extraction, and conformance tests."""

from json import dumps as json_dumps, loads as json_loads
from pathlib import Path

from pytest import mark as pytest_mark, raises as pytest_raises

from engram.errors import IdentityValidationError
from engram.identity import (
    MAX_CANONICAL_FORM_BYTES,
    MAX_CONTEXT_FINGERPRINT_BYTES,
    MAX_RETRIEVAL_ALIASES,
    MAX_RETRIEVAL_REPRESENTATION_BYTES,
    QualifierKind,
    QueryOperator,
    RetrievalOrigin,
    entity_reference,
    extract_entities_and_identifiers,
    extract_lexical_terms,
    extract_operator,
    extract_qualifiers,
    extract_relation_surface,
    extract_standalone_identity,
    identity_qualifier,
    normalize_retrieval_key,
    query_identity,
    query_identity_from_dict,
    query_identity_to_dict,
    query_identity_to_json,
    relation_reference,
    retrieval_representation,
    retrieval_representation_bindings,
    retrieval_representation_from_dict,
    retrieval_representation_to_dict,
    scope_key,
    scope_key_from_dict,
    scope_key_signature,
    scope_key_to_dict,
    scoped_retrieval_key_from_dict,
    scoped_retrieval_key_from_text,
    scoped_retrieval_key_to_dict,
    scoped_retrieval_key_to_json,
    validate_authoritative_identity,
    validate_entity_reference,
    validate_identity_qualifier,
    validate_scope_key,
    validate_scoped_retrieval_key,
)

NORMALIZATION_FIXTURE = Path(__file__).parent / "fixtures" / "identity" / "normalization.json"
# The external corpus is decoded once at ingress; both case lists are required, so a malformed corpus fails collection.
NORMALIZATION_CORPUS = json_loads(NORMALIZATION_FIXTURE.read_text(encoding="utf-8"))
NORMALIZATION_CASES = NORMALIZATION_CORPUS.get("normalization_cases", [])
IDENTITY_CONTRASTS = NORMALIZATION_CORPUS.get("identity_contrasts", [])
if not NORMALIZATION_CASES or not IDENTITY_CONTRASTS:
    raise ValueError("identity normalization corpus must define normalization_cases and identity_contrasts")


def none_paths(value, path: str = "root") -> list[str]:
    if value is None:
        result = [path]
        return result
    if isinstance(value, dict):
        result = [nested for key, item in value.items() for nested in none_paths(item, f"{path}.{key}")]
        return result
    if isinstance(value, (list, tuple, set)):
        result = [nested for index, item in enumerate(value) for nested in none_paths(item, f"{path}[{index}]")]
        return result
    result = []
    return result


def test_scope_key_codec_equality_and_order_are_deterministic() -> None:
    empty = scope_key()
    support = scope_key(namespace="support", context_fingerprint="account-tier:pro")
    restored = scope_key_from_dict(scope_key_to_dict(support))

    assert restored == support
    assert scope_key_to_dict(restored) == {
        "namespace": "support",
        "context_fingerprint": "account-tier:pro",
    }
    assert sorted([scope_key_signature(support), scope_key_signature(empty)]) == [
        scope_key_signature(empty),
        scope_key_signature(support),
    ]


@pytest_mark.parametrize(
    ("value", "message"),
    [
        ({"namespace": "", "context_fingerprint": "", "extra": ""}, "unsupported fields"),
        ({"namespace": [], "context_fingerprint": ""}, "namespace must be a string"),
    ],
)
def test_scope_key_invalid_scope_payloads_fail_explicitly(value: dict, message: str) -> None:
    with pytest_raises(IdentityValidationError, match=message):
        scope_key_from_dict(value)


def test_scope_key_scope_bounds_are_utf8_bytes_and_controls_are_rejected() -> None:
    scope_key(context_fingerprint="x" * MAX_CONTEXT_FINGERPRINT_BYTES)
    with pytest_raises(IdentityValidationError, match="exceeds"):
        scope_key(context_fingerprint="é" * (MAX_CONTEXT_FINGERPRINT_BYTES // 2 + 1))
    with pytest_raises(IdentityValidationError, match="control"):
        scope_key(namespace="bad\nnamespace")


def test_scope_key_scope_validation_revalidates_and_copies_mutable_input() -> None:
    source = scope_key(namespace="support", context_fingerprint="pro")
    validated = validate_scope_key(source)

    assert type(validated) is dict
    assert validated == source
    assert validated is not source
    source["namespace"] = "mutated"
    assert validated.get("namespace", "") == "support"


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

    restored = query_identity_from_dict(query_identity_to_dict(query))

    assert restored == query
    assert query_identity_to_json(restored) == query_identity_to_json(query)
    assert none_paths(query_identity_to_dict(query)) == []
    assert "null" not in query_identity_to_json(query)


def test_identity_contracts_unknown_operator_and_empty_relation_are_concrete() -> None:
    query = query_identity(canonical_form="opaque request")

    assert {"operator", "entities", "qualifiers", "lexical_terms"} <= set(query)
    assert query.get("operator", QueryOperator.UNKNOWN) == QueryOperator.UNKNOWN
    assert query.get("entities", ()) == ()
    assert query.get("relation", {}) == relation_reference()
    assert query.get("qualifiers", ()) == ()
    assert query.get("lexical_terms", ()) == ()
    assert query.get("scope", {}) == scope_key()


def test_identity_contracts_reject_unknown_fields() -> None:
    payload = query_identity_to_dict(extract_standalone_identity("Who created Python?"))
    payload["unexpected"] = "value"
    with pytest_raises(IdentityValidationError, match="unsupported fields"):
        query_identity_from_dict(payload)


@pytest_mark.parametrize(
    "malformed_call",
    [
        lambda: entity_reference("Ada Lovelace", "not a canonical id"),
        lambda: relation_reference("", "predicate:born"),
        lambda: identity_qualifier(QualifierKind.CURRENT, "Not Normalized"),
        lambda: query_identity(canonical_form="Not Normalized"),
        lambda: query_identity(canonical_form="normalized", lexical_terms=("two words",)),
    ],
)
def test_identity_contracts_malformed_components_fail_without_reinterpretation(malformed_call) -> None:
    with pytest_raises(IdentityValidationError):
        malformed_call()


def test_identity_contracts_leaf_records_are_exact_revalidated_dictionaries() -> None:
    entity = entity_reference("Alan Turing", "entity:alan-turing")
    qualifier = identity_qualifier(QualifierKind.HISTORICAL, "historical")
    query = query_identity(canonical_form="alan turing", entities=(entity,), qualifiers=(qualifier,))

    query_entity = query.get("entities", ())[0]
    query_qualifier = query.get("qualifiers", ())[0]
    assert type(entity) is dict
    assert type(query) is dict
    assert type(query_entity) is dict
    assert type(query_qualifier) is dict
    entity["surface"] = "mutated"
    assert query_entity.get("surface", "") == "Alan Turing"

    malformed_entity = dict(query_entity)
    malformed_entity["unexpected"] = "value"
    with pytest_raises(IdentityValidationError, match="unsupported fields"):
        validate_entity_reference(malformed_entity)

    malformed_qualifier = dict(query_qualifier)
    malformed_qualifier["value"] = "Not Normalized"
    with pytest_raises(IdentityValidationError, match="already be normalized"):
        validate_identity_qualifier(malformed_qualifier)


@pytest_mark.parametrize("case", NORMALIZATION_CASES, ids=lambda case: case.get("id", ""))
def test_retrieval_normalization_golden_normalization_cases(case: dict) -> None:
    assert normalize_retrieval_key(case.get("input", "")) == case.get("expected", "")


@pytest_mark.parametrize("case", NORMALIZATION_CASES, ids=lambda case: case.get("id", ""))
def test_retrieval_normalization_normalization_is_idempotent(case: dict) -> None:
    once = normalize_retrieval_key(case.get("input", ""))
    assert normalize_retrieval_key(once) == once


def test_retrieval_normalization_empty_and_punctuation_only_inputs_are_concrete() -> None:
    assert normalize_retrieval_key("") == ""
    assert normalize_retrieval_key("?!…") == ""


def test_retrieval_normalization_generated_normalization_corpus_is_idempotent() -> None:
    stems = ("Latency", "Version v1.2", "$PATH", "A|B", "2*3", "E-1234")
    wrappers = ("{}", "  {}  ", "What is {}?", "‘{}’", "[{}]")

    for stem in stems:
        for wrapper in wrappers:
            normalized = normalize_retrieval_key(wrapper.format(stem))
            assert normalize_retrieval_key(normalized) == normalized


@pytest_mark.parametrize("symbol", ["<", ">", "<=", ">=", "==", "!=", "$", "%", "|", "&", "*"])
def test_retrieval_normalization_identity_bearing_symbols_survive_normalization(symbol: str) -> None:
    assert symbol in normalize_retrieval_key(f"left {symbol} right")


def test_scoped_retrieval_and_representations_scoped_key_codec_and_scope_separation() -> None:
    support = scoped_retrieval_key_from_text(scope_key("support", "pro"), "What’s the port?")
    billing = scoped_retrieval_key_from_text(scope_key("billing", "pro"), "What is the port?")

    assert type(support) is dict
    assert support.get("normalized_key", "") == "what is the port"
    assert support != billing
    assert scoped_retrieval_key_from_dict(scoped_retrieval_key_to_dict(support)) == support

    validated = validate_scoped_retrieval_key(support)
    assert validated is not support
    assert "scope" in validated
    assert "scope" in support
    validated_scope = validated.get("scope", {})
    support_scope = support.get("scope", {})
    assert validated_scope is not support_scope
    support["normalized_key"] = "mutated"
    support_scope["namespace"] = "mutated"
    assert validated.get("normalized_key", "") == "what is the port"
    assert validated_scope.get("namespace", "") == "support"


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
    assert all("origin" in binding for binding in bindings)
    assert retrieval.get("aliases", ()) == ("Postgres default port",)
    assert [binding.get("origin", RetrievalOrigin.CANONICAL) for binding in bindings] == [
        RetrievalOrigin.CANONICAL,
        RetrievalOrigin.ALIAS,
    ]
    assert [binding.get("representation", "") for binding in bindings] == [
        "What's the default PostgreSQL port?",
        "Postgres default port",
    ]
    assert len({scoped_retrieval_key_to_json(binding.get("key", {})) for binding in bindings}) == 2
    assert retrieval_representation_from_dict(retrieval_representation_to_dict(retrieval)) == retrieval
    assert "pattern_aliases" not in retrieval_representation_to_dict(retrieval)
    assert "response" not in retrieval_representation_to_dict(retrieval)


def test_scoped_retrieval_and_representations_representation_enforces_bounds_and_concrete_tuple_input() -> None:
    with pytest_raises(IdentityValidationError, match="must be a tuple"):
        retrieval_representation("request", aliases=["alias"])
    with pytest_raises(IdentityValidationError, match="exceed"):
        retrieval_representation(
            "request",
            aliases=tuple(f"alias {index}" for index in range(MAX_RETRIEVAL_ALIASES + 1)),
        )
    with pytest_raises(IdentityValidationError, match="non-whitespace"):
        retrieval_representation("   ")


def test_scoped_retrieval_and_representations_maximum_alias_payload_round_trips() -> None:
    aliases = tuple(
        f"alias-{index}-" + "x" * (MAX_RETRIEVAL_REPRESENTATION_BYTES - len(f"alias-{index}-"))
        for index in range(MAX_RETRIEVAL_ALIASES)
    )
    retrieval = retrieval_representation("canonical request", aliases)

    assert retrieval_representation_from_dict(retrieval_representation_to_dict(retrieval)) == retrieval


@pytest_mark.parametrize(
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


@pytest_mark.parametrize("symbol", ["<", ">", "<=", ">=", "==", "!="])
def test_identity_extraction_symbolic_comparisons_are_typed_qualifiers(symbol: str) -> None:
    request = f"Is latency {symbol} 100 ms?"
    qualifiers = extract_qualifiers(request, extract_operator(request))

    assert identity_qualifier(QualifierKind.COMPARISON, symbol) in qualifiers


def test_identity_extraction_entity_and_technical_identifier_extraction_is_surface_only() -> None:
    entities = extract_entities_and_identifiers(
        "Compare Ada Lovelace with PostgreSQL v16.2 at config/engram.yml after RFC 7231, error E-1234, and C++."
    )
    surfaces = [entity.get("surface", "") for entity in entities]

    assert "Ada Lovelace" in surfaces
    assert "PostgreSQL" in surfaces
    assert "v16.2" in surfaces
    assert "config/engram.yml" in surfaces
    assert "RFC 7231" in surfaces
    assert all("canonical_id" in entity for entity in entities)
    assert all(entity.get("canonical_id", "") == "" for entity in entities)


@pytest_mark.parametrize(
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
    relation = extract_relation_surface(input_text, operator, entities)
    assert "surface" in relation
    assert relation.get("surface", "") == expected


def test_identity_extraction_standalone_builder_is_deterministic_and_preserves_semantic_contrasts() -> None:
    scope = scope_key("biography", "public")
    when = extract_standalone_identity("When was Ada Lovelace born?", scope)
    where = extract_standalone_identity("Where was Ada Lovelace born?", scope)

    when_canonical_form = when.get("canonical_form", "")
    where_canonical_form = where.get("canonical_form", "")

    assert extract_standalone_identity("When was Ada Lovelace born?", scope) == when
    assert when.get("operator", QueryOperator.UNKNOWN) == QueryOperator.WHEN
    assert where.get("operator", QueryOperator.UNKNOWN) == QueryOperator.WHERE
    assert "lexical_terms" in when
    assert "lexical_terms" in where
    assert when.get("lexical_terms", ()) == where.get("lexical_terms", ())
    assert when_canonical_form
    assert where_canonical_form
    assert when_canonical_form != where_canonical_form
    assert scoped_retrieval_key_from_text(scope, when_canonical_form) != scoped_retrieval_key_from_text(
        scope,
        where_canonical_form,
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
    decoded_identity = query_identity_from_dict(query_identity_to_dict(authoritative))
    decoded_retrieval = retrieval_representation_from_dict(retrieval_representation_to_dict(retrieval))
    validate_authoritative_identity(decoded_identity, decoded_retrieval)

    assert validated is authoritative
    assert decoded_identity == authoritative
    assert decoded_retrieval == retrieval
    assert decoded_identity.get("entities", ())[0].get("surface", "") == "Alan Turing"
    assert decoded_retrieval.get("canonical", "") == "When was Alan Turing born?"


def test_authoritative_identity_authoritative_contract_rejects_null_malformed_and_oversized_input() -> None:
    identity = query_identity_to_dict(extract_standalone_identity("Who created Python?"))
    retrieval = retrieval_representation_to_dict(retrieval_representation("Who created Python?"))

    identity["relation"] = json_loads("null")
    with pytest_raises(IdentityValidationError, match="identity relation must be an object"):
        validate_authoritative_identity(query_identity_from_dict(identity), retrieval_representation_from_dict(retrieval))

    with pytest_raises(IdentityValidationError, match="exceeds"):
        query_identity(canonical_form="x" * (MAX_CANONICAL_FORM_BYTES + 1))


@pytest_mark.parametrize("case", IDENTITY_CONTRASTS, ids=lambda case: case.get("id", ""))
def test_identity_conformance_corpus_adversarial_pairs_produce_distinct_scoped_keys(case: dict) -> None:
    scope = scope_key("conformance", "v1")
    left = extract_standalone_identity(case.get("left", ""), scope)
    right = extract_standalone_identity(case.get("right", ""), scope)

    assert left != right
    assert scoped_retrieval_key_from_text(scope, left.get("canonical_form", "")) != scoped_retrieval_key_from_text(
        scope,
        right.get("canonical_form", ""),
    )


def test_identity_conformance_corpus_same_language_in_different_scopes_produces_distinct_keys() -> None:
    request = "What are the support hours?"
    support = extract_standalone_identity(request, scope_key("support", "pro"))
    billing = extract_standalone_identity(request, scope_key("billing", "pro"))
    support_canonical_form = support.get("canonical_form", "")
    billing_canonical_form = billing.get("canonical_form", "")
    support_scope = support.get("scope", {})
    billing_scope = billing.get("scope", {})

    assert support_canonical_form
    assert support_canonical_form == billing_canonical_form
    assert support_scope
    assert billing_scope
    assert support_scope != billing_scope
    assert scoped_retrieval_key_from_text(support_scope, support_canonical_form) != scoped_retrieval_key_from_text(
        billing_scope, billing_canonical_form
    )


def test_identity_conformance_corpus_generated_scoped_key_properties() -> None:
    requests = tuple(case.get("input", "") for case in NORMALIZATION_CASES)
    scopes = (scope_key(), scope_key("support", "free"), scope_key("support", "pro"))

    for request in requests:
        keys = tuple(scoped_retrieval_key_from_text(scope, request) for scope in scopes)
        assert len({scoped_retrieval_key_to_json(key) for key in keys}) == len(scopes)
        for key in keys:
            assert scoped_retrieval_key_from_dict(scoped_retrieval_key_to_dict(key)) == key
            assert scoped_retrieval_key_from_text(key.get("scope", {}), key.get("normalized_key", "")) == key


def test_identity_conformance_corpus_contract_outputs_are_recursively_concrete() -> None:
    identity = extract_standalone_identity("Where is PostgreSQL v16.2 supported?", scope_key("support", "v1"))
    retrieval = retrieval_representation(
        "Where is PostgreSQL v16.2 supported?",
        ("PostgreSQL v16.2 support location",),
    )
    bindings = retrieval_representation_bindings(retrieval, identity.get("scope", {}))
    outputs = {
        "identity": query_identity_to_dict(identity),
        "retrieval": retrieval_representation_to_dict(retrieval),
        "keys": [scoped_retrieval_key_to_dict(binding.get("key", {})) for binding in bindings],
    }

    assert none_paths(outputs) == []
    assert "null" not in json_dumps(outputs, sort_keys=True)
