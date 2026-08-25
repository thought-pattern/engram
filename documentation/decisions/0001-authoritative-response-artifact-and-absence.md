# ADR 0001: Authoritative response artifact and concrete absence

- Status: Accepted
- Date: 2026-08-11
- Applies from: Accepted-response artifacts

## Context

Persistence version 1 stores response-cache entries, AIML patterns, and learned facts
as general statement dictionaries. Accepted responses require explicit identity,
lifecycle, validity, provenance, and concrete absence values.

## Decision

`CachedResponseArtifact` is the authoritative persisted record for an accepted response. It is a typed record with deterministic serialization and a compatibility projection into the current statement view. Its required fields include exact response text, query identity, retrieval representations, exact scope, tier, lifecycle, validity, knowledge epoch, support, supersession, provenance, statistics, bounded metadata, schema version, and generation.

The accepted response text is stored and returned byte-for-byte.

Every field has one concrete runtime type. Absence is represented as follows:

| Type | Empty value |
| --- | --- |
| String or identifier | `""` |
| Sequence | `[]` |
| Mapping | `{}` |
| Set in live state | `set()` |
| Count, generation, epoch, or unavailable numeric feature | `0` or `0.0` |
| Availability or presence | `false` |

When an empty scalar is meaningful, the record carries a separate boolean such as
`valid_until_available` or `feature_available`. Enum `UNKNOWN` members represent
domain states. Core, persistence, and transport output use concrete absence values;
boundary loaders normalize omitted and legacy-null inputs immediately.

Boundary normalization follows type validation. Mapping inputs accept `{}` and
reject `[]`, `()`, `""`, `0`, and `false`. Compatibility translation of historical
persisted `null` is explicit and tested separately from current public input.

Persistence version 1 remains readable. Writing the typed artifact requires a new
persistence version, an idempotent migration, an explicit backup or output path,
and downgrade documentation. Secondary indexes remain derived state.

## Consequences

- The typed artifact separates response-cache evolution from general statements.
- Compatibility projections add migration code while keeping existing Python, CLI, MCP, and gRPC callers usable.
- Presence booleans make wire records slightly larger while preserving precise, concrete types.
- Legacy ambiguous records enter quarantine with explicit missing identity or validity.
