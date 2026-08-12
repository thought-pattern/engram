# Exact, alias, and Claim-support index contracts, version 1

## Status and boundary

`engram.indexes` implements Section 2's disposable in-memory indexes. It accepts validated `IndexProjection` values and does
not depend on Section 3 artifact or lifecycle types. It does not persist authoritative responses, infer lifecycle from storage
tier, manufacture exact identity from lexical keywords, patterns, or response text, or expose transport endpoints. Sections 3
and 15 retain those responsibilities.

All public records are immutable dataclasses with concrete fields. Empty strings and tuples represent absence; production
annotations do not use optional unions. Mappings inside `IndexState` are copied into read-only mapping proxies before the state
can become visible.

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
  "support_claim_ids": ["claim-1"],
  "direct_answer_eligible": true,
  "exclusion_reason": "",
  "normalization_version": 1
}
```

The strict dictionary and deterministic compact JSON codecs reject missing, extra, falsey non-object, unsupported-version,
and malformed values. Retrieval keys reuse the Section 1 `ScopedRetrievalKey` and retain the source spelling plus
`canonical`/`alias` provenance. Claim IDs are bounded opaque strings and are never interpreted as graph content.

Current persistence version 1 has no authoritative request identity. `projection_from_statement` therefore projects its
current `template.tapestry.support` metadata but emits no retrieval keys and uses `missing_identity`. Malformed support is
classified as `malformed_support`. It never promotes a response, pattern, `keyword_source`, or lexical keyword into an exact
identity.

## `IndexState` and paired invariants

One state owns all six maps as a unit:

| Map | Invariant |
| --- | --- |
| `retrieval_to_owners` | Every indexable owner of a scoped key, including ineligible owners, with generation and provenance. |
| `statement_to_retrieval` | Exact inverse of retrieval ownership after within-artifact key deduplication. |
| `claim_to_statements` | Sorted statement IDs for each support Claim. |
| `statement_to_claims` | Exact inverse of Claim support. |
| `direct_retrieval` | Only one eligible owner; absent for zero owners, ineligible-only ownership, or collisions. |
| `projections` | Validated source projections used to check and reproduce this disposable state. |

`exact_lookup` performs dictionary lookup by `ScopedRetrievalKey` and returns `FOUND`, `MISS`, or `COLLISION`. `FOUND` carries
the statement ID, generation, original representation, and canonical/alias provenance. `COLLISION` returns bounded sorted
owner IDs and an empty selected statement; deterministic ordering is never treated as a winner.

`support_lookup` accepts bounded matched Claim IDs and returns sorted `SupportMatch` values containing each statement and the
specific queried Claims that reached it. Its result records `scanned_edge_count`, `complete`, `reason`, `omitted_edge_count`,
and post-scan `omitted_match_count`.
The default traversal bound is 100,000 Claim-to-statement edges. When the complete matched fan-out exceeds the bound, lookup
returns `complete=false`, `reason=scan_limit_exceeded`, no partial matches, and the complete omitted edge count. Runtime work is proportional to queried
Claim IDs and their reached fan-out, not total statement corpus size.

The support-aware vector path obtains one immutable state, validates the complete fan-out against
`graph.vector_support_scan_limit`, filters statement existence and caller scope while scanning, calculates each statement's
maximum matched-Claim score, and maintains only the requested top-k candidates. Output truncation therefore occurs after
eligibility and scoring. Exceeding the scan bound produces a diagnostic warning and abstention; a statement-ID prefix can never
be ranked as though it were a complete candidate set.

## Construction and classification

`build_index_state` materializes a candidate entirely off-live. It accepts `IndexProjection` objects or strict JSON-compatible
projection mappings so unsupported and malformed inputs can be reported rather than aborting the complete diagnostic build.
Issues caused only by rejected source inputs carry `input_only=true`; retained-projection diagnostics remain reproducible from
live state and are validated before publication.
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

The reproducible fixture [classification-v1.json](classification-v1.json) covers legacy missing identity, a canonical/alias
cross-artifact collision, an unsupported schema, and malformed support.

## Mutation, concurrency, and repair

The core lock order is:

```text
mutation_lock -> statement_lock -> keyword_lock -> IndexOwner private lock
```

Readers acquire the private owner lock only long enough to obtain one immutable `IndexState`; they do not retain it while
acquiring statement locks. Writers copy the immutable top-level maps, alter only the changed exact and support edges, refresh
bounded diagnostics, check all touched forward/inverse/direct relationships, and publish one next generation. Add, replace,
remove, and support-update use generic projection verbs so Section 3 can compose lifecycle transactions without putting
lifecycle policy into Section 2.

`check_index_state` is a non-mutating self-check against the state's retained projections. `check_index_state_against` compares
against an explicitly supplied authoritative collection; an empty collection means the expected index is empty. Both compare
all live maps with a deterministic rebuild and report missing, extra, or asymmetric entries as errors. The self-check also
validates reproducible build-report counts, collision groups, omission counts, and retained-projection issue diagnostics.
Expected ineligibility, unindexable projections, and collision groups are explicit notices rather than corruption.
`IndexOwner.atomic_swap` rejects a map or reproducible-report mismatch and rejects a stale expected generation.

`Engram.check_indexes` and `repair_indexes` always use current authoritative statements. `check_index_projections` and
`repair_index_projections` consume an explicit collection, including an empty collection. Dry-run returns the candidate build
report and live diff without changing state. Explicit apply rechecks the candidate, compares the expected generation, and
atomically swaps it. Current response statements are read but never modified. Persistence/startup policy, adapter exposure,
authorization, and operator procedures remain Section 15 work.

## Bounds and complexity

| Value | Limit |
| --- | ---: |
| Statement ID | 256 UTF-8 bytes |
| Support Claim ID | 256 UTF-8 bytes |
| Retrieval keys per projection | 33 |
| Support Claim IDs per projection or lookup | 256 |
| Exclusion reason | 128 UTF-8 bytes |
| Report items per category | 1,000 |
| Report detail | 512 characters |
| Owner IDs returned by one lookup/collision | 1,000 |
| Default support scan | 100,000 Claim-to-statement edges |
| Configurable support scan | 1 through 1,000,000 Claim-to-statement edges |

Exact lookup is expected O(1) relative to corpus size. Support lookup is O(matched Claim IDs plus reached fan-out). Rebuild is
O(projections + retrieval edges + support edges, with deterministic sorting). Immutable mutation copies top-level maps,
updates only the named projection edges, and refreshes diagnostics; its measured cost is reported because ADR 0004 defines no
mutation-latency release threshold.

## Verification

Run focused verification and the offline benchmark with:

```powershell
python -m pytest -q tests/test_indexes.py
python scripts/benchmark_indexes.py
```

The benchmark uses 30 post-warm-up samples, exact lookup at 10,000 and 100,000 projections, Claim lookup and full regulated
proposal fan-out 1/10/100, complete output truncation and scan-exhaustion checks, rebuild and every mutation at 5,000
projections, and traced build memory. It evaluates the full proposal against both the absolute and baseline-relative ADR 0004
gates. Its machine-readable result is
[benchmark-2026-08-12.json](benchmark-2026-08-12.json).
