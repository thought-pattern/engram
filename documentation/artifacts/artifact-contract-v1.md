# Cached response artifact contract v1

Status: Implemented by EGR-301  
Authority: ADR 0001, ADR 0002, and Section 3 of `ENGRAM-DEVELOPMENT.md`

## Authority and immutability

`CachedResponseArtifact` is the typed authoritative record for one accepted response. Its `response` field is stored exactly once and survives dictionary, JSON, persistence, migration, and compatibility projection without normalization or rewriting. Query normalization and retrieval aliases affect lookup metadata only.

Secondary indexes, the legacy statement dictionary, and adapter records are derived views. They cannot become a second source of response text, lifecycle, validity, epoch, provenance, or statistics.

## Version 1 fields

| Field | Concrete type | Contract |
| --- | --- | --- |
| `schema_version` | positive integer | Exactly `1`. |
| `statement_id` | string | Non-empty, bounded stable identifier. |
| `generation` | positive integer | Optimistic-concurrency generation. |
| `response` | string | Non-empty, at most 1 MiB UTF-8, exact Unicode scalar sequence; tab and line breaks are preserved. |
| `query_identity` | `QueryIdentity` | Valid Section 1 authoritative identity. |
| `retrieval` | `RetrievalRepresentation` | Canonical representation and at most 32 non-executable aliases. |
| `tier` | `Tier` | `STATIC` or `DYNAMIC`; residency policy only. |
| `lifecycle` | `LifecycleState` | `ACTIVE`, `SUPERSEDED`, `INVALIDATED`, or `RETIRED`. |
| `scope` | `ScopeKey` | Must exactly equal `query_identity.scope`. |
| `support_claim_ids` | tuple/string array | At most 256 bounded opaque Claim IDs; deterministically deduplicated and sorted. |
| `valid_from`, `valid_until` | string | Empty or canonical RFC 3339 UTC ending in `Z`. |
| `valid_from_available`, `valid_until_available` | boolean | Presence for the corresponding bound; absent requires the empty string. |
| `knowledge_epoch` | nonnegative integer | `0` when unavailable; zero remains meaningful when availability is true. |
| `knowledge_epoch_available` | boolean | Whether the artifact carries an epoch requirement. |
| `superseded_by` | string | Empty unless lineage is present; required for `SUPERSEDED`, forbidden for `ACTIVE`, and cannot self-reference. |
| `provenance` | `ArtifactProvenance` | Version, bounded source label and caller ID, and required canonical acceptance time. |
| `statistics` | `ArtifactStatistics` | Nonnegative hit/query counts and presence-bearing last-hit time. |
| `metadata` | object | Deeply immutable, deterministic JSON values only; bounded to 64 KiB, depth 8, and 1,024 items. |

Current codecs require every field and reject extras. Legacy omissions and historical `null` are handled only by the EGR-309 migration boundary; maintained construction, core state, and output never contain `None` or JSON `null`.

## Deterministic codec

`to_dict()` emits only concrete JSON-compatible values. `to_json()` uses UTF-8-preserving JSON with sorted keys and compact separators. `from_dict()` requires the exact schema, and `from_json()` requires an object. A successful round trip produces an equal artifact and byte-identical JSON serialization.

Metadata mappings are copied, recursively validated, sorted, and frozen at construction. Subsequent mutation of caller-owned mappings or lists cannot alter the artifact. Non-finite floats, null, unsupported types, non-string keys, excessive nesting, excessive item counts, and excessive encoded size are rejected.

## Deferred behavior

This contract does not decide request-time eligibility, perform projection, mutate repositories, assign statement IDs, checkpoint, or accept legacy records. Those operations remain ordered under EGR-310, EGR-306, EGR-311, EGR-312, EGR-309, and EGR-302.
