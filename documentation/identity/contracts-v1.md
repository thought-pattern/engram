# Query identity and retrieval contracts, version 1

## Status and boundary

`engram.identity` implements the pure identity foundation used by exact indexes
and accepted-response commits.

The contracts are immutable dataclasses with deterministic dictionary and compact
JSON codecs. Runtime fields have one concrete type; empty strings, tuples, and an
empty `ScopeKey` represent absence.

## Contracts

### `ScopeKey`

`ScopeKey` is the exact eligibility boundary. Namespace and context fingerprint are
opaque caller values.

```json
{
  "schema_version": 1,
  "namespace": "support",
  "context_fingerprint": "account-tier:pro"
}
```

Two otherwise identical requests in different scopes produce different `ScopedRetrievalKey` values.

### Identity components

`QueryOperator` is a closed vocabulary: `who`, `what`, `where`, `when`, `which`, `why`, `how`, `how_many`, `lookup`, `exists`, `count`, `compare`, and `unknown`.

An `EntityReference` carries a required surface and a concrete canonical ID string. Standalone extraction leaves the ID as `""`; authoritative IDs use URI-like syntax such as `entity:alan-turing`. `RelationReference` follows the same shape, but both fields may be empty when relation extraction abstains.

`IdentityQualifier` uses a closed kind (`negation`, `quantity`, `comparison`, `temporal`, `location`, `current`, or `historical`) and a required version-1-normalized value.

### `QueryIdentity`

```json
{
  "schema_version": 1,
  "normalization_version": 1,
  "canonical_form": "birth date of alan turing",
  "operator": "when",
  "entities": [
    {
      "surface": "Alan Turing",
      "canonical_id": "entity:alan-turing"
    }
  ],
  "relation": {
    "surface": "born",
    "canonical_id": "predicate:date_of_birth"
  },
  "qualifiers": [],
  "lexical_terms": [
    "alan",
    "turing",
    "born"
  ],
  "scope": {
    "schema_version": 1,
    "namespace": "biography",
    "context_fingerprint": ""
  }
}
```

Standalone construction uses the normalized request as its canonical form. This preserves `when` versus `where`, positive versus negative, current versus historical, and count versus lookup distinctions even when their lexical terms are equal. Authoritative writers may supply a richer normalized canonical form and canonical IDs.

### `ScopedRetrievalKey`

The immutable logical key is:

```text
(ScopeKey, normalization_version, normalized representation)
```

Its schema-versioned JSON codec supports fixtures and diagnostics. Disposable
indexes consume the value as an in-memory key.

### `RetrievalRepresentation`

```json
{
  "schema_version": 1,
  "normalization_version": 1,
  "canonical": "What's the default PostgreSQL port?",
  "aliases": [
    "Postgres default port"
  ]
}
```

The canonical representation is required. Aliases are retrieval data separate from
matcher `pattern_aliases`. Construction normalizes each value for comparison,
removes aliases equivalent to the canonical representation, and retains the first
spelling of each distinct alias. `bindings(scope)` emits one `RetrievalKeyBinding`
per surviving representation with `canonical` or `alias` provenance.

## Bounds

Limits are measured in UTF-8 bytes unless the row names a count.

| Value | Limit |
| --- | ---: |
| Namespace | 128 bytes |
| Context fingerprint | 512 bytes |
| Canonical form or input request | 4,096 bytes |
| Entity/relation surface | 512 bytes |
| Canonical identifier | 256 bytes |
| Qualifier value | 512 bytes |
| Lexical term | 256 bytes |
| Entities | 32 |
| Qualifiers | 32 |
| Lexical terms | 128 |
| Canonical or alias representation | 4,096 bytes |
| Aliases | 32 |
| Serialized identity-contract JSON | 262,144 bytes |

Scope and contract fields reject control and surrogate characters. Raw request and retrieval strings permit tab and line separators only so versioned whitespace normalization can collapse them. Unsupported schema and normalization versions fail explicitly.

## Retrieval normalization version 1

`normalize_retrieval_key` and `engram.text.normalize` have separate versioned
behavior for identity and legacy retrieval.

Version 1 performs these ordered operations:

1. validate type, UTF-8 size, and control characters;
2. apply Unicode NFKC;
3. normalize recognized apostrophe variants, treat Unicode prose dashes as separators, and retain the mathematical minus as a hyphen;
4. case-fold;
5. expand the repository's bounded contraction table using whole-token matches;
6. replace non-semantic punctuation with spaces while retaining `.`, `_`, `:`, `/`, `\\`, `-`, `+`, `#`, `@`, `$`, `%`, `|`, `&`, `*`, comparison operators, and internal apostrophes used by technical tokens or identifiers;
7. remove terminal sentence periods and non-semantic quote apostrophes; and
8. collapse whitespace.

The function is deterministic and idempotent. Its golden fixture is
[normalization-v1.json](normalization-v1.json). Key-changing behavior requires a
new normalization version and fixtures.

## Standalone extraction

`build_standalone_identity` uses bounded string and regular-expression logic only:

- operators are classified before lexical filtering;
- explicit negation, quantity, word-based or symbolic comparison, temporal, current/historical, and location cues become qualifiers;
- title-cased surfaces, quoted values, versions, paths, symbols, qualified identifiers, and error codes become entity references with empty canonical IDs;
- relation extraction scans supported predicates after the main auxiliary, ignores trailing version, time, and context tokens, and returns `RelationReference()` when unmatched;
- lexical terms are normalized and deduplicated separately from identity fields; and
- the normalized complete request remains the canonical form.

Contextual resolution owns graph identity, ambiguous predicates, and conversation context.

## Authoritative input

`QueryIdentity.from_dict` and `RetrievalRepresentation.from_dict` strictly decode JSON-compatible mappings. `validate_authoritative_identity` verifies supported versions and normalization compatibility and requires the canonical representation to produce a non-empty key in the supplied scope. It returns the supplied identity unchanged.

Canonical IDs receive syntax validation. Contextual resolution and eligibility
evaluate graph existence, visibility, trust, and temporal state.

## Verification

`python -m pytest -q tests/test_identity.py` verifies the contract.
