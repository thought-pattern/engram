# Query identity and retrieval contracts, version 1

## Status and boundary

`engram.identity` implements the pure identity foundation used by later exact indexes and accepted-response commits. It performs no persistence, graph access, model loading, resource acquisition, network access, or transport translation.

The contracts are immutable dataclasses with deterministic dictionary and compact JSON codecs. Runtime fields have one concrete type. Empty strings, tuples, and an empty `ScopeKey` represent absence; the codecs never generate Python `None` or JSON `null`, and their annotations do not use optional unions.

Cross-artifact mappings and collision discovery belong to Section 2. Commit-time collision rejection and explicit supersession belong to Section 3. Contextual and graph-backed canonical resolution belongs to Section 8. Persistence and adapter translation belong to Section 15.

## Contracts

### `ScopeKey`

`ScopeKey` is the exact eligibility boundary. Namespace and context fingerprint are preserved as opaque caller values rather than parsed, normalized, or hashed.

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

Standalone construction uses the normalized request as the conservative canonical form. This intentionally preserves `when` versus `where`, positive versus negative, current versus historical, and count versus lookup distinctions even when their lexical terms are equal. Authoritative writers may supply a richer normalized canonical form and canonical IDs.

### `ScopedRetrievalKey`

The immutable logical key is:

```text
(ScopeKey, normalization_version, normalized representation)
```

Its schema-versioned JSON codec is suitable for fixtures and diagnostics. Section 2 consumes the value as an in-memory index key; it is not an authoritative persisted index record.

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

The canonical representation is required. Aliases are non-executable data, never matcher `pattern_aliases`. Construction normalizes each value for comparison, removes aliases equivalent to the canonical representation, and retains the first spelling of each distinct alias. `bindings(scope)` emits one `RetrievalKeyBinding` per surviving representation with `canonical` or `alias` provenance and the original spelling.

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

`normalize_retrieval_key` is separate from `engram.text.normalize`; changing identity behavior cannot silently alter AIML matching or legacy lexical retrieval.

Version 1 was corrected during the Section 1 remediation on 2026-08-11. At that point the identity module, fixtures, and tracker were uncommitted worktree additions; no identity-bearing persistence schema, exact index, Python integration API, MCP field, or gRPC field existed. There was therefore no released or persisted version-1 keyspace to migrate. The corrected fixture below defines the first releasable version 1. Any later key-changing behavior requires a new normalization version.

Version 1 performs these ordered operations:

1. validate type, UTF-8 size, and control characters;
2. apply Unicode NFKC;
3. normalize recognized apostrophe variants, treat Unicode prose dashes as separators, and retain the mathematical minus as a hyphen;
4. case-fold;
5. expand the repository's bounded contraction table using whole-token matches;
6. replace non-semantic punctuation with spaces while retaining `.`, `_`, `:`, `/`, `\\`, `-`, `+`, `#`, `@`, `$`, `%`, `|`, `&`, `*`, comparison operators, and internal apostrophes used by technical tokens or identifiers;
7. remove terminal sentence periods and non-semantic quote apostrophes; and
8. collapse whitespace.

The function is deterministic and idempotent. Its golden fixture is [normalization-v1.json](normalization-v1.json). Any behavior change that can alter a retrieval key requires a new normalization version and new fixtures; version 1 data is never silently reinterpreted.

## Standalone extraction

`build_standalone_identity` uses bounded string and regular-expression logic only:

- operators are classified before lexical filtering;
- explicit negation, quantity, word-based or symbolic comparison, temporal, current/historical, and location cues become qualifiers;
- title-cased surfaces, quoted values, versions, paths, symbols, qualified identifiers, and error codes become entity references with empty canonical IDs;
- relation extraction scans supported predicates after the main auxiliary, ignores trailing version, time, and context tokens, and otherwise returns `RelationReference()` rather than using the final content token as a guess;
- lexical terms are normalized and deduplicated separately from identity fields; and
- the normalized complete request remains the canonical form.

The builder does not claim graph-backed entity identity, resolve ambiguous predicates, inherit conversation context, or rewrite accepted response text.

## Authoritative input

`QueryIdentity.from_dict` and `RetrievalRepresentation.from_dict` strictly decode JSON-compatible mappings. `validate_authoritative_identity` verifies supported versions and cross-contract normalization compatibility, then proves that the canonical representation produces a non-empty key in the supplied scope. It returns the same `QueryIdentity` object; it does not normalize, enrich, resolve, or otherwise rewrite authoritative fields.

Canonical IDs are syntax-validated but are not looked up in the graph. Existence, visibility, trust, and temporal eligibility are later integration concerns.

## Verification

Run the Section 1 suite with:

```powershell
python -m pytest -q tests/test_identity.py
```

The suite covers codecs, version rejection, UTF-8 bounds, concrete absence, fixture-driven and generated idempotence properties, scope and codec properties, representation deduplication and provenance, operator/qualifier extraction, symbolic comparison and technical-operator contrasts, conservative relations, authoritative preservation, dependency-free construction, and adversarial identity contrasts.

The reproducible remediation benchmark is [remediation-benchmark-2026-08-11.json](remediation-benchmark-2026-08-11.json), generated by `scripts/benchmark_identity.py` without graph, model, network, or persistence access.
