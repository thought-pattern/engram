"""Section 1 contract, normalization, extraction, and conformance tests."""

import ast
import json
from pathlib import Path

import pytest

import engram.identity as identity_module
from engram.errors import IdentityValidationError, UnsupportedIdentityVersionError
from engram.identity import (
    IDENTITY_SCHEMA_VERSION,
    MAX_CANONICAL_FORM_BYTES,
    MAX_CONTEXT_FINGERPRINT_BYTES,
    MAX_RETRIEVAL_ALIASES,
    MAX_RETRIEVAL_REPRESENTATION_BYTES,
    RETRIEVAL_NORMALIZATION_VERSION,
    EntityReference,
    IdentityQualifier,
    QualifierKind,
    QueryIdentity,
    QueryOperator,
    RelationReference,
    RetrievalOrigin,
    RetrievalRepresentation,
    ScopedRetrievalKey,
    ScopeKey,
    build_retrieval_representation,
    build_standalone_identity,
    extract_entities_and_identifiers,
    extract_lexical_terms,
    extract_operator,
    extract_qualifiers,
    extract_relation_surface,
    load_authoritative_identity,
    normalize_retrieval_key,
    validate_authoritative_identity,
)

REPOSITORY = Path(__file__).resolve().parent.parent
NORMALIZATION_FIXTURE = REPOSITORY / "documentation" / "identity" / "normalization-v1.json"


def _fixture() -> dict:
    return json.loads(NORMALIZATION_FIXTURE.read_text(encoding="utf-8"))


def _none_paths(value, path: str = "root") -> list[str]:
    if value is None:
        return [path]
    if isinstance(value, dict):
        return [nested for key, item in value.items() for nested in _none_paths(item, f"{path}.{key}")]
    if isinstance(value, (list, tuple, set)):
        return [nested for index, item in enumerate(value) for nested in _none_paths(item, f"{path}[{index}]")]
    return []


class TestScopeKey:
    def test_codec_equality_and_order_are_deterministic(self) -> None:
        empty = ScopeKey()
        support = ScopeKey(namespace="support", context_fingerprint="account-tier:pro")
        restored = ScopeKey.from_json(support.to_json())

        assert restored == support
        assert restored.to_dict() == {
            "schema_version": 1,
            "namespace": "support",
            "context_fingerprint": "account-tier:pro",
        }
        assert sorted([support, empty]) == [empty, support]
        assert support.to_json() == restored.to_json()

    @pytest.mark.parametrize(
        ("value", "message"),
        [
            ({"schema_version": 2, "namespace": "", "context_fingerprint": ""}, "unsupported scope schema_version"),
            ({"schema_version": 1, "namespace": "", "context_fingerprint": "", "extra": ""}, "unsupported fields"),
            ({"schema_version": 1, "namespace": [], "context_fingerprint": ""}, "namespace must be a string"),
        ],
    )
    def test_invalid_scope_payloads_fail_explicitly(self, value: dict, message: str) -> None:
        with pytest.raises(IdentityValidationError, match=message):
            ScopeKey.from_dict(value)

    def test_scope_bounds_are_utf8_bytes_and_controls_are_rejected(self) -> None:
        ScopeKey(context_fingerprint="x" * MAX_CONTEXT_FINGERPRINT_BYTES)
        with pytest.raises(IdentityValidationError, match="exceeds"):
            ScopeKey(context_fingerprint="é" * (MAX_CONTEXT_FINGERPRINT_BYTES // 2 + 1))
        with pytest.raises(IdentityValidationError, match="control"):
            ScopeKey(namespace="bad\nnamespace")


class TestIdentityContracts:
    def test_component_and_query_identity_codecs_round_trip(self) -> None:
        query = QueryIdentity(
            canonical_form="birth date of alan turing",
            operator=QueryOperator.WHEN,
            entities=(EntityReference("Alan Turing", "entity:alan-turing"),),
            relation=RelationReference("born", "predicate:date_of_birth"),
            qualifiers=(IdentityQualifier(QualifierKind.HISTORICAL, "historical"),),
            lexical_terms=("alan", "turing", "born"),
            scope=ScopeKey("biography", ""),
        )

        restored = QueryIdentity.from_json(query.to_json())

        assert restored == query
        assert restored.to_json() == query.to_json()
        assert _none_paths(query.to_dict()) == []
        assert "null" not in query.to_json()

    def test_unknown_operator_and_empty_relation_are_concrete(self) -> None:
        query = QueryIdentity(canonical_form="opaque request")

        assert query.operator == QueryOperator.UNKNOWN
        assert query.entities == ()
        assert query.relation == RelationReference()
        assert query.qualifiers == ()
        assert query.lexical_terms == ()
        assert query.scope == ScopeKey()

    def test_unsupported_versions_and_unknown_fields_are_rejected(self) -> None:
        payload = build_standalone_identity("Who created Python?").to_dict()
        payload["schema_version"] = IDENTITY_SCHEMA_VERSION + 1
        with pytest.raises(UnsupportedIdentityVersionError, match="identity schema_version"):
            QueryIdentity.from_dict(payload)

        payload = build_standalone_identity("Who created Python?").to_dict()
        payload["unexpected"] = "value"
        with pytest.raises(IdentityValidationError, match="unsupported fields"):
            QueryIdentity.from_dict(payload)

    @pytest.mark.parametrize(
        "factory",
        [
            lambda: EntityReference("Ada Lovelace", "not a canonical id"),
            lambda: RelationReference("", "predicate:born"),
            lambda: IdentityQualifier(QualifierKind.CURRENT, "Not Normalized"),
            lambda: QueryIdentity(canonical_form="Not Normalized"),
            lambda: QueryIdentity(canonical_form="normalized", lexical_terms=("two words",)),
        ],
    )
    def test_malformed_components_fail_without_reinterpretation(self, factory) -> None:
        with pytest.raises(IdentityValidationError):
            factory()


class TestRetrievalNormalization:
    @pytest.mark.parametrize("case", _fixture()["normalization_cases"], ids=lambda case: case["id"])
    def test_golden_normalization_cases(self, case: dict) -> None:
        assert normalize_retrieval_key(case["input"]) == case["expected"]

    @pytest.mark.parametrize("case", _fixture()["normalization_cases"], ids=lambda case: case["id"])
    def test_normalization_is_idempotent(self, case: dict) -> None:
        once = normalize_retrieval_key(case["input"])
        assert normalize_retrieval_key(once) == once

    def test_empty_and_punctuation_only_inputs_are_concrete(self) -> None:
        assert normalize_retrieval_key("") == ""
        assert normalize_retrieval_key("?!…") == ""

    def test_normalization_version_is_explicit(self) -> None:
        with pytest.raises(UnsupportedIdentityVersionError, match="normalization_version"):
            normalize_retrieval_key("request", RETRIEVAL_NORMALIZATION_VERSION + 1)

    def test_generated_normalization_corpus_is_idempotent(self) -> None:
        stems = ("Latency", "Version v1.2", "$PATH", "A|B", "2*3", "E-1234")
        wrappers = ("{}", "  {}  ", "What is {}?", "‘{}’", "[{}]")

        for stem in stems:
            for wrapper in wrappers:
                normalized = normalize_retrieval_key(wrapper.format(stem))
                assert normalize_retrieval_key(normalized) == normalized

    @pytest.mark.parametrize("symbol", ["<", ">", "<=", ">=", "==", "!=", "$", "%", "|", "&", "*"])
    def test_identity_bearing_symbols_survive_normalization(self, symbol: str) -> None:
        assert symbol in normalize_retrieval_key(f"left {symbol} right")


class TestScopedRetrievalAndRepresentations:
    def test_scoped_key_codec_and_scope_separation(self) -> None:
        support = ScopedRetrievalKey.build(ScopeKey("support", "pro"), "What’s the port?")
        billing = ScopedRetrievalKey.build(ScopeKey("billing", "pro"), "What is the port?")

        assert support.normalized_key == "what is the port"
        assert support != billing
        assert ScopedRetrievalKey.from_json(support.to_json()) == support

    def test_representation_deduplicates_by_normalized_key_and_retains_provenance(self) -> None:
        retrieval = RetrievalRepresentation(
            canonical="What's the default PostgreSQL port?",
            aliases=(
                "What is the default PostgreSQL port?",
                "Postgres default port",
                "POSTGRES   DEFAULT PORT!",
            ),
        )
        bindings = retrieval.bindings(ScopeKey("support", "pro"))

        assert retrieval.aliases == ("Postgres default port",)
        assert [binding.origin for binding in bindings] == [RetrievalOrigin.CANONICAL, RetrievalOrigin.ALIAS]
        assert [binding.representation for binding in bindings] == [
            "What's the default PostgreSQL port?",
            "Postgres default port",
        ]
        assert len({binding.key for binding in bindings}) == 2
        assert RetrievalRepresentation.from_json(retrieval.to_json()) == retrieval
        assert "pattern_aliases" not in retrieval.to_dict()
        assert "response" not in retrieval.to_dict()

    def test_representation_enforces_bounds_and_concrete_tuple_input(self) -> None:
        with pytest.raises(IdentityValidationError, match="must be a tuple"):
            RetrievalRepresentation("request", aliases=["alias"])
        with pytest.raises(IdentityValidationError, match="exceed"):
            RetrievalRepresentation("request", aliases=tuple(f"alias {index}" for index in range(MAX_RETRIEVAL_ALIASES + 1)))
        with pytest.raises(IdentityValidationError, match="non-whitespace"):
            build_retrieval_representation("   ")

    def test_maximum_alias_payload_round_trips_through_json_codec(self) -> None:
        aliases = tuple(
            f"alias-{index}-" + "x" * (MAX_RETRIEVAL_REPRESENTATION_BYTES - len(f"alias-{index}-"))
            for index in range(MAX_RETRIEVAL_ALIASES)
        )
        retrieval = RetrievalRepresentation("canonical request", aliases)

        assert RetrievalRepresentation.from_json(retrieval.to_json()) == retrieval


class TestIdentityExtraction:
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
    def test_closed_operator_vocabulary(self, input_text: str, expected: QueryOperator) -> None:
        assert extract_operator(input_text) == expected

    def test_qualifiers_are_preserved_outside_lexical_terms(self) -> None:
        request = "Which current releases do not support feature X after 2020?"
        operator = extract_operator(request)
        qualifiers = extract_qualifiers(request, operator)
        lexical_terms = extract_lexical_terms(request)

        assert IdentityQualifier(QualifierKind.NEGATION, "not") in qualifiers
        assert IdentityQualifier(QualifierKind.CURRENT, "current") in qualifiers
        assert IdentityQualifier(QualifierKind.TEMPORAL, "after 2020") in qualifiers
        assert "which" not in lexical_terms
        assert "not" not in lexical_terms
        assert "current" in lexical_terms

    @pytest.mark.parametrize("symbol", ["<", ">", "<=", ">=", "==", "!="])
    def test_symbolic_comparisons_are_typed_qualifiers(self, symbol: str) -> None:
        request = f"Is latency {symbol} 100 ms?"
        qualifiers = extract_qualifiers(request, extract_operator(request))

        assert IdentityQualifier(QualifierKind.COMPARISON, symbol) in qualifiers

    def test_entity_and_technical_identifier_extraction_is_surface_only(self) -> None:
        entities = extract_entities_and_identifiers(
            r"Compare Ada Lovelace with PostgreSQL v16.2 at C:\Engram\config.yml after error E-1234 and C++."
        )
        surfaces = [entity.surface for entity in entities]

        assert "Ada Lovelace" in surfaces
        assert "PostgreSQL" in surfaces
        assert "v16.2" in surfaces
        assert r"C:\Engram\config.yml" in surfaces
        assert all(entity.canonical_id == "" for entity in entities)

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
    def test_relation_extraction_abstains_when_uncertain(self, input_text: str, expected: str) -> None:
        operator = extract_operator(input_text)
        entities = extract_entities_and_identifiers(input_text)
        assert extract_relation_surface(input_text, operator, entities).surface == expected

    def test_standalone_builder_is_deterministic_and_preserves_semantic_contrasts(self) -> None:
        scope = ScopeKey("biography", "public")
        when = build_standalone_identity("When was Ada Lovelace born?", scope)
        where = build_standalone_identity("Where was Ada Lovelace born?", scope)

        assert build_standalone_identity("When was Ada Lovelace born?", scope) == when
        assert when.operator == QueryOperator.WHEN
        assert where.operator == QueryOperator.WHERE
        assert when.lexical_terms == where.lexical_terms
        assert when.canonical_form != where.canonical_form
        assert ScopedRetrievalKey.build(scope, when.canonical_form) != ScopedRetrievalKey.build(scope, where.canonical_form)

    def test_identity_module_has_no_nlp_model_graph_or_network_imports(self) -> None:
        tree = ast.parse(Path(identity_module.__file__).read_text(encoding="utf-8"))
        imports = {
            alias.name.split(".")[0]
            for node in ast.walk(tree)
            if isinstance(node, (ast.Import, ast.ImportFrom))
            for alias in node.names
        }

        assert imports.isdisjoint({"nltk", "spacy", "sentence_transformers", "mgclient", "grpc", "mcp", "requests"})


class TestAuthoritativeIdentity:
    def test_valid_authoritative_identity_is_preserved_exactly(self) -> None:
        authoritative = QueryIdentity(
            canonical_form="birth date of alan turing",
            operator=QueryOperator.WHEN,
            entities=(EntityReference("Alan Turing", "entity:alan-turing"),),
            relation=RelationReference("born", "predicate:date_of_birth"),
            lexical_terms=("alan", "turing", "born"),
            scope=ScopeKey("biography", "released"),
        )
        retrieval = RetrievalRepresentation(
            canonical="When was Alan Turing born?",
            aliases=("What is Alan Turing's birth date?",),
        )

        validated = validate_authoritative_identity(authoritative, retrieval)
        decoded_identity, decoded_retrieval = load_authoritative_identity(authoritative.to_dict(), retrieval.to_dict())

        assert validated is authoritative
        assert decoded_identity == authoritative
        assert decoded_retrieval == retrieval
        assert decoded_identity.entities[0].surface == "Alan Turing"
        assert decoded_retrieval.canonical == "When was Alan Turing born?"

    def test_authoritative_contract_rejects_null_malformed_and_oversized_input(self) -> None:
        identity = build_standalone_identity("Who created Python?").to_dict()
        retrieval = build_retrieval_representation("Who created Python?").to_dict()

        identity["relation"] = None
        with pytest.raises(IdentityValidationError, match="identity relation must be an object"):
            load_authoritative_identity(identity, retrieval)

        with pytest.raises(IdentityValidationError, match="exceeds"):
            QueryIdentity(canonical_form="x" * (MAX_CANONICAL_FORM_BYTES + 1))


class TestIdentityConformanceCorpus:
    @pytest.mark.parametrize("case", _fixture()["identity_contrasts"], ids=lambda case: case["id"])
    def test_adversarial_pairs_produce_distinct_scoped_keys(self, case: dict) -> None:
        scope = ScopeKey("conformance", "v1")
        left = build_standalone_identity(case["left"], scope)
        right = build_standalone_identity(case["right"], scope)

        assert left != right
        assert ScopedRetrievalKey.build(scope, left.canonical_form) != ScopedRetrievalKey.build(scope, right.canonical_form)

    def test_same_language_in_different_scopes_produces_distinct_keys(self) -> None:
        request = "What are the support hours?"
        support = build_standalone_identity(request, ScopeKey("support", "pro"))
        billing = build_standalone_identity(request, ScopeKey("billing", "pro"))

        assert support.canonical_form == billing.canonical_form
        assert support.scope != billing.scope
        assert ScopedRetrievalKey.build(support.scope, support.canonical_form) != ScopedRetrievalKey.build(
            billing.scope, billing.canonical_form
        )

    def test_generated_scoped_key_properties(self) -> None:
        requests = tuple(case["input"] for case in _fixture()["normalization_cases"])
        scopes = (ScopeKey(), ScopeKey("support", "free"), ScopeKey("support", "pro"))

        for request in requests:
            keys = tuple(ScopedRetrievalKey.build(scope, request) for scope in scopes)
            assert len(set(keys)) == len(scopes)
            for key in keys:
                assert ScopedRetrievalKey.from_json(key.to_json()) == key
                assert ScopedRetrievalKey.build(key.scope, key.normalized_key) == key

    def test_contract_outputs_are_recursively_concrete(self) -> None:
        identity = build_standalone_identity("Where is PostgreSQL v16.2 supported?", ScopeKey("support", "v1"))
        retrieval = build_retrieval_representation(
            "Where is PostgreSQL v16.2 supported?",
            ("PostgreSQL v16.2 support location",),
        )
        bindings = retrieval.bindings(identity.scope)
        outputs = {
            "identity": identity.to_dict(),
            "retrieval": retrieval.to_dict(),
            "keys": [binding.key.to_dict() for binding in bindings],
        }

        assert _none_paths(outputs) == []
        assert "null" not in json.dumps(outputs, sort_keys=True)
