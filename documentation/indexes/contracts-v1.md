# Exact, alias, and typed-support index contracts, version 1

## Status and boundary

`engram.indexes` implements disposable in-memory indexes from validated
`IndexProjection` values. Accepted-response storage and lifecycle policy remain
with the artifact repository; adapters own transport exposure.

All public records are native dictionaries with concrete fields. Empty strings,
tuples, and dictionaries represent contract-specific absence. Mappings inside
`IndexState` are copied into read-only mapping proxies before publication.

## `IndexProjection`

The version-1 projection carries only derived-index input:

```json
{
  "schema_version": 1,
  "statement_id": "stmt-123",
  "generation": 1,
  "retrieval_keys": [
    {
      "key": {},
      "provenance": "canonical",
      "representation": "Who acquired GitHub?"
    }
  ],
  "support_references": [
    {
      "schema_version": "tapestry-engram-support-v1",
      "record_kind": "proposition",
      "id": "prp_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
      "state_revision": 0,
      "support_revision": 0,
      "representation_contract": "tapestry-ke-representation-v1",
      "visibility_scope": {
        "kind": "global",
        "company_id": {},
        "customer_id": {},
        "engagement_id": {}
      },
      "dependency_state_digest": "dep_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
      "store_epoch": "store-epoch"
    }
  ],
  "direct_answer_eligible": true,
  "exclusion_reason": "",
  "normalization_version": 1
}
```

The strict dictionary and deterministic compact JSON codecs reject malformed or
unsupported values. Retrieval keys reuse `ScopedRetrievalKey` and retain source
spelling plus `canonical`/`alias` provenance. Support values are ordered,
duplicate-free, exact `tapestry-engram-support-v1` mappings for durable
Assertions or Propositions. Engram validates their shape but does not interpret
their epistemic state.

`projection_from_statement` projects
`template.tapestry.support`, emits an empty retrieval-key set, and reports
`missing_identity`; malformed support reports
`malformed_support`.

## `IndexState` and paired invariants

One state owns all six maps as a unit:

| Map | Invariant |
| --- | --- |
| `retrieval_to_owners` | Every indexable owner of a scoped key, including ineligible owners, with generation and provenance. |
| `statement_to_retrieval` | Exact inverse of retrieval ownership after within-artifact key deduplication. |
| `record_to_statements` | Sorted statement IDs for each durable support-record ID. |
| `statement_to_references` | Exact inverse containing each statement's ordered typed support mappings. |
| `direct_retrieval` | Only one eligible owner; absent for zero owners, ineligible-only ownership, or collisions. |
| `projections` | Validated source projections used to check and reproduce this disposable state. |

`exact_lookup` performs dictionary lookup by `ScopedRetrievalKey` and returns `FOUND`, `MISS`, or `COLLISION`. `FOUND` carries
the statement ID, generation, original representation, and canonical/alias provenance. `COLLISION` returns bounded sorted
owner IDs and an empty selected statement.

`support_lookup` accepts bounded matched Assertion or Proposition IDs and returns sorted `SupportMatch` values containing each statement and the
specific queried record IDs that reached it. Its result records `scanned_edge_count`, `complete`, `reason`, `omitted_edge_count`,
and post-scan `omitted_match_count`.
The default traversal bound is 100,000 support-record-to-statement edges. When the complete
matched fan-out exceeds the bound, lookup returns `complete=false`,
`reason=scan_limit_exceeded`, an empty match set, and the omitted edge count.
Runtime work is proportional to queried record IDs and their reached fan-out.

The support-aware vector path obtains one immutable state, validates the complete fan-out against
`graph.vector_support_scan_limit`, filters statement existence and caller scope while scanning, calculates each statement's
maximum matched-Proposition score, and maintains only the requested top-k candidates. Output truncation therefore occurs after
eligibility and scoring. Exceeding the scan bound produces a diagnostic warning
and abstention.

## Construction and classification

`build_index_state` materializes an off-live candidate from `IndexProjection`
objects or strict JSON-compatible mappings. It reports rejected source inputs
with `input_only=true`; retained diagnostics are reproducible from live state.
The bounded report distinguishes:

- `missing_identity`;
- `unsupported_schema_version` and `unsupported_normalization_version`;
- `malformed_projection` and `malformed_support`;
- `ineligible`;
- `within_artifact_duplicate`;
- `duplicate_statement_id`; and
- `cross_artifact_collision`.

Identical duplicate inputs are deduplicated. Conflicting projections with the same statement ID are excluded together.
Within-artifact duplicate keys retain canonical provenance when one duplicate is canonical. Multiple eligible artifacts with
one key remain visible in ownership and collision reports but are omitted from direct lookup. Reports expose at most 1,000
issues, collisions, or lookup owners and record omitted counts.

The reproducible fixture [classification-v1.json](../../tests/fixtures/indexes/classification-v1.json) covers a statement missing retrieval identity, a canonical/alias
cross-artifact collision, an unsupported schema, and malformed support.

## Mutation, concurrency, and repair

The core lock order is:

```text
mutation_lock -> statement_lock -> keyword_lock -> IndexOwner private lock
```

Readers acquire the private owner lock long enough to obtain one immutable
`IndexState`, then release it before statement locks. Writers copy the immutable
top-level maps, alter only the changed exact and support edges, refresh
bounded diagnostics, check all touched forward/inverse/direct relationships, and publish one next generation. Add, replace,
remove, and support-update use generic projection verbs; the repository owns
lifecycle policy.

`check_index_state` compares the state with a deterministic rebuild from its
retained projections. `check_index_state_against` uses an explicitly supplied
authoritative collection; an empty collection expects an empty index. Both
report missing, extra, asymmetric, or diagnostic mismatches.
`IndexOwner.atomic_swap` rejects inconsistent state and stale generations.

`Engram.check_indexes` and `repair_indexes` use current authoritative statements;
`check_index_projections` and `repair_index_projections` use the supplied
collection. Dry-run returns the candidate report and live diff. Apply rechecks
the candidate and expected generation before one atomic swap; statements are not
modified.

## Bounds and complexity

| Value | Limit |
| --- | ---: |
| Statement ID | 256 UTF-8 bytes |
| Support record ID | 256 UTF-8 bytes |
| Retrieval keys per projection | 33 |
| Support references per projection or record IDs per lookup | 256 |
| Exclusion reason | 128 UTF-8 bytes |
| Report items per category | 1,000 |
| Report detail | 512 characters |
| Owner IDs returned by one lookup/collision | 1,000 |
| Default support scan | 100,000 support-record-to-statement edges |
| Configurable support scan | 1 through 1,000,000 support-record-to-statement edges |

Exact lookup is expected O(1) relative to corpus size. Support lookup is O(matched record IDs plus reached fan-out). Rebuild is
O(projections + retrieval edges + support edges, with deterministic sorting). Immutable mutation copies top-level maps,
updates only the named projection edges, and refreshes diagnostics.

## Verification

Run focused verification and the offline benchmark with:

```powershell
python -m pytest -q tests/test_indexes.py
python scripts/benchmark_indexes.py
```

The benchmark reports p50, p95, p99, maximum timing, and memory.
