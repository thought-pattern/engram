# ADR 0001: Authoritative response artifact and concrete absence

- Status: Accepted
- Date: 2026-08-11
- Applies from: Increment A

## Context

Persistence version 1 stores response-cache entries as general statement dictionaries. The same record also represents AIML patterns and learned facts, so response identity, lifecycle, validity, and provenance cannot be made explicit without adding loosely related keys. Existing loaders accept omitted fields and legacy JSON `null` values. Maintained runtime output now uses concrete falsy values, but that contract needs to remain true as the response model expands.

## Decision

`CachedResponseArtifact` will become the authoritative persisted record for an accepted response. It will be a typed record with deterministic serialization and a compatibility projection into the current statement view while older APIs are migrated. Its required fields will include exact response text, query identity, retrieval representations, exact scope, tier, lifecycle, validity, knowledge epoch, support, supersession, provenance, statistics, bounded metadata, schema version, and generation.

The accepted response text is stored and returned unchanged. Normalization, aliases, matcher patterns, adapters, and migrations cannot rewrite it.

Every field has one concrete runtime type. Absence is represented as follows:

| Type | Empty value |
| --- | --- |
| String or identifier | `""` |
| Sequence | `[]` |
| Mapping | `{}` |
| Set in live state | `set()` |
| Count, generation, epoch, or unavailable numeric feature | `0` or `0.0` |
| Availability or presence | `false` |

When an empty scalar is meaningful, the record carries a separate boolean such as `valid_until_available` or `feature_available`. Enums use an explicit member such as `UNKNOWN` only when unknown is a domain state, not as a generic absence sentinel. Maintained core, persistence, and transport output cannot contain Python `None` or JSON `null`, and maintained annotations cannot use optional unions. Boundary loaders normalize omitted and legacy-null inputs immediately.

Boundary normalization follows type validation. A falsey value of the wrong concrete type is rejected rather than treated as omission; for example, mapping inputs accept `{}` and reject `[]`, `()`, `""`, `0`, and `false`. Compatibility translation of historical persisted `null` is explicit and tested separately from current public input.

Persistence version 1 remains readable. Writing the typed artifact requires a new persistence version, an idempotent migration, an explicit backup or output path, and downgrade documentation. Secondary indexes are never part of the authoritative artifact.

## Consequences

- Response-cache evolution no longer overloads the general statement dictionary.
- Compatibility projections add migration code but keep existing Python, CLI, MCP, and gRPC callers usable during Increment A.
- Presence booleans make wire records slightly larger while preserving precise, concrete types.
- Legacy ambiguous records can be retained without pretending that missing identity or validity was known.
