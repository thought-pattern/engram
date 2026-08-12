# ADR 0003: Rebuildable derived indexes and bounded evidence wire format

- Status: Accepted
- Date: 2026-08-11
- Applies from: Increments A and B

## Context

Persistence version 1 stores a keyword index including statistics, but it has no scoped exact index or Claim-support reverse index. Support-aware recall scans all statements after vector search. The future resolver also needs to return useful Claim evidence when no cached response is eligible without exposing arbitrary graph content.

## Decision

The scoped exact, statement-to-key, Claim-to-statement, and statement-to-Claim indexes are derived in-memory state. They are rebuilt deterministically from authoritative artifacts at startup, checked before readiness, and atomically swapped into live state. They are not written into the first typed persistence version.

Index snapshots may be reconsidered only if index rebuild p95 exceeds 1,000 ms at the approved maximum corpus. A future snapshot must be schema- and normalization-versioned, bound to an authoritative-state digest, disposable on any mismatch, and followed by the same consistency check as a rebuild.

Evidence wire version 1 is a bounded structured record. A resolution carries `outcome` (`ANSWER`, `EVIDENCE`, or `MISS`), `wire_version`, selected artifact fields when an answer exists, and up to 10 evidence records. Each evidence record contains only a stable Claim ID, resolver name, concretely typed feature values plus availability booleans, permissible canonical entity/predicate references, validity and trust inputs, a path of at most two hops, and stable selection reason codes.

The complete serialized evidence package is limited to 64 KiB. Individual identifiers are limited to 256 UTF-8 bytes and reason-code collections to 16 entries. Raw Claim bodies, Passage content, proof payloads, credentials, caller-supplied Cypher, and arbitrary graph properties are excluded. Truncation is explicit through concrete count and boolean fields.

Core contracts land first. MCP receives additive fields after the contract stabilizes. gRPC uses additive version-1 fields only when old clients can interpret omission safely; otherwise a version-2 result or service is introduced and committed stubs are regenerated.

## Consequences

- Authoritative JSON remains portable and repairable; startup pays a measured rebuild cost.
- Support-aware retrieval can scale with matched Claim IDs and fan-out after Increment A rather than scanning the entire statement corpus.
- Evidence can help Tapestry without authorizing a direct response or leaking unrestricted graph data.
- Payload limits are part of core validation, not merely adapter configuration.

