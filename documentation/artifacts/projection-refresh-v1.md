# Artifact projection and contextual exact refresh contract v1

Status: Implemented by EGR-311  
Authority: ADR 0003, ADR 0004, and Sections 2–3 of `ENGRAM-DEVELOPMENT.md`

## Projection boundary

`index_projection_from_artifact` accepts one authoritative `CachedResponseArtifact` and its matching `EligibilityDecision`. Statement ID, generation, and namespace must agree. It emits only the bounded Section 2 fields:

- statement ID and generation;
- scoped canonical and alias bindings;
- opaque support Claim IDs;
- the context-specific `direct_answer_eligible` Boolean;
- one empty or stable exclusion reason; and
- schema and normalization versions.

Response text, lifecycle, tier, validity, epoch, provenance, statistics, and metadata remain exclusively in the artifact. The projection is disposable derived input.

## Why refresh is mandatory

Lifecycle changes create artifact mutations, but time advances and namespace epochs change independently. Therefore an eligible Boolean already stored in an `IndexState` cannot authorize a direct response by itself.

`ContextualExactLookup.exact_lookup` performs this bounded sequence:

1. snapshot the current index state;
2. enumerate every owner of the requested key, bounded by Section 2's 1,000-owner diagnostic limit;
3. require the authoritative artifact for every owner;
4. evaluate every artifact against the one request context and policy;
5. derive complete replacement projections;
6. atomically refresh those owners against the expected index-state generation; and
7. return only the lookup from the post-refresh state.

The path never returns the lookup observed before refresh. Missing artifact authority, excessive ownership, malformed refresh data, or retry exhaustion fails without a direct answer.

## Atomic generic index primitive

`IndexOwner.atomic_refresh_exact_lookup` is a generic Section 2 operation. It requires all and only the current owners, unique statement IDs, and the exact expected state generation. Refresh may change only `direct_answer_eligible` and `exclusion_reason`; identity, generation, retrieval keys, support, and versions must remain equal. A change builds and checks a complete next `IndexState` and publishes it atomically. A no-op retains the current state generation.

Concurrent generic index mutation produces a state-generation conflict. The contextual path recomputes against a fresh snapshot up to its bounded retry count; it never applies a calculation to a different owner set.

## Result evidence

`ContextualExactLookupResult` carries the post-refresh typed exact result, every owner decision, the context signature, whether the index changed, and the visible index-state generation. Exact expiration and epoch changes are tested in both directions, including restoration under an older captured context without artifact mutation. Multi-owner collision state is also recomputed for every owner before the outcome is returned.
