# Accepted-response persistence v2 and migration contract

Status: Implemented by EGR-309  
Authority: ADR 0001, ADR 0003, and Section 3 of `ENGRAM-DEVELOPMENT.md`

## Top-level compatibility

Engram persistence version is `2`. The loader continues to accept version `1`, including an omitted version that historically meant v1. Existing configuration, counters, bot state, sets, maps, substitutions, general statements, keyword statistics, and sessions remain in their established top-level fields.

Version 2 adds the exact required `response_state` object:

| Field | Authority |
| --- | --- |
| `schema_version` | Response-state schema, currently `1`. |
| `artifacts` | Statement-ID-ordered authoritative `CachedResponseArtifact` records. |
| `namespace_epochs` | Complete deterministic `NamespaceEpochState` snapshot. |
| `mutation_receipts` | Complete bounded receipt/tombstone ledger and next sequence. |
| `quarantine` | Ordered legacy migration exclusions with statement ID, reason, and bounded detail. |

[The response-state v2 fixture](persistence-v2-response-state.json) exercises all four feature records. [The sanitized persistence-v1 regulated-response fixture](../baseline/fixtures/regulated-response-v1.json) remains the supported legacy fixture.

## Derived-state rule

Response compatibility statements and every exact, alias, and support index are absent from `response_state`. On startup, `ArtifactRepository` decodes authoritative artifacts, reconstructs detached compatibility statements, creates conservative projections, and builds and checks a fresh Section 2 index. A matching general statement record is replaced by the artifact-derived view; artifact text, identity, provenance, metadata, and statistics win. A pattern statement with the same ID is corruption and blocks startup.

The general legacy Engram index is then rebuilt for compatibility. It never manufactures exact response identity. Contextual direct lookup remains owned by the response repository and EGR-311.

## Version 1 migration

The migration recognizes a recoverable legacy accepted response only when its patternless statement metadata contains a concrete non-empty `tapestry.request`. It constructs:

- standalone `QueryIdentity` from that exact request and scope;
- canonical request and validated optional `retrieval_aliases`;
- exact response text and preserved tier;
- namespace and context fingerprint;
- opaque support Claim IDs;
- ACTIVE lifecycle, generation 1, open validity, and unavailable artifact epoch;
- source/caller and canonical UTC acceptance provenance;
- hit/query/last-hit statistics; and
- all non-contract Tapestry metadata, including a historical request ID as metadata only.

The standalone namespace epoch is explicitly initialized to zero for every recovered namespace. V1 carried no durable mutation receipt, so migration never fabricates one.

Classification is deterministic:

- no concrete request → `missing_identity`, legacy statement retained and exact-unindexed;
- malformed scope, aliases, support, provenance, metadata, or artifact field → `malformed_identity`, legacy statement retained and exact-unindexed; or
- multiple recovered artifacts owning one scoped canonical or alias key → each receives `ambiguous_identity`; artifacts remain inspectable, but the index retains every owner and contextual lookup returns COLLISION rather than a winner.

Migration never derives identity from response text, pattern text, or lexical keywords. `migrate_persistence_state` copies its input, produces v2 for v1, validates v2, and returns an exact copy when invoked again on v2.

## Recovery procedure

Section 3 supplies the pure transformation and validation. The Section 15 operator flow must use it with an explicit output path or backup:

1. stop writers and retain the original v1 file unchanged;
2. parse and load v1, failing before output on structural corruption;
3. run `migrate_persistence_state` and write v2 atomically to a distinct output or a backed-up target;
4. load that v2 output and require repository consistency before readiness;
5. inspect every quarantine record;
6. repair missing identity through an authoritative recommit, repair malformed fields explicitly, and resolve ambiguous keys through explicit supersession or an authoritative scope/alias correction;
7. rerun migration/load to prove idempotence and index rebuild; and
8. preserve v1 until adapter validation and rollback exercise complete.

The v2 writer does not offer automatic downgrade. A v1 binary cannot safely represent artifacts, lifecycle, epochs, receipts, or quarantine. Section 15 must document downgrade constraints and select a compatible backup rather than stripping those fields.

## Failure behavior

File save continues to write a same-directory temporary JSON file, restrict permissions where supported, and replace the target atomically. A malformed or unsupported v2 response contract, duplicate artifact ID, invalid receipt/epoch state, repository inconsistency, or pattern/artifact ID conflict prevents startup. Missing or corrupt derived indexes do not matter because no response index snapshot is persisted.
