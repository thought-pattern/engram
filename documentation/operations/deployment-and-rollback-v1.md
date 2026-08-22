# Deployment and rollback runbook v1

**Status:** Current operator procedure; release qualification remains pending  
**Owner:** EGR-1510  
**Applies to:** standalone, graph-backed, and Tapestry-backed deployments

## Supported deployment boundary

| Mode | Current control | Release position |
| --- | --- | --- |
| Standalone | `EngramCore`, CLI, MCP, or current gRPC v1 with graph disabled | Component-supported; production approval is external |
| Graph-backed | `graph.enabled`, optional `graph.vector_enabled`, fixed relation/Claim capabilities, read-only account | Optional capability; readiness is reported independently |
| Tapestry-backed | Existing proposal/resolve/learn workflow and Python evidence handoff | Future gRPC evidence exposure and staged release controls remain pending |

The current implementation does not provide a namespace rollout matrix, shadow
mode, or an independently approved regulated-direct-answer policy. Do not describe
those future Section 16 controls as deployed. `accept_exact=False`, an explicit
`configured_resolvers` tuple, and graph/vector enablement are engineering controls,
not substitutes for authorization or release approval.

## Pre-deployment checklist

1. Pin the source revision, dependency set, configuration, policy versions, and
   persisted-state schema being deployed.
2. Provision required NLTK data before startup. Provision spaCy and any local
   embedding model for capabilities expected to report ready. Serving processes
   must have no artifact-download permission.
3. Preserve an immutable copy of the current persistence file and record its
   SHA-256 digest. Copy to a new path; never edit the live file in place.
4. Load and migrate only through the supported persistence codecs. Require the
   response repository and derived-index checks to be consistent. When sparse
   retrieval is enabled, require `check_sparse_index()` to report consistent after
   its startup rebuild. Follow the
   [Section 3 recovery runbook](../artifacts/section3-recovery-runbook.md) for
   quarantine or uncertain mutation outcomes.
5. For graph mode, apply `schema.cypher`, use a database account restricted to
   reads, and inspect the configured graph and optional vector index/dimension.
   Graph readiness is optional and never controls overall Engram readiness.
6. Keep the service bound to loopback unless TLS and deployment authentication are
   configured at the service or trusted proxy boundary.

## Time and cancellation boundary

Elapsed time is observability, never an answer-quality threshold. The Python
`resolve_request` boundary accepts a transient `cancellation_check` callback.
Engram invokes it before expensive resolver work and at cooperative boundaries in
structured, semantic, and composition processing. The callback raises
`ResolutionCancelledError`; a cancelled attempt is not cached as a knowledge miss
or completed idempotency result and can be retried with the same request ID.

Graph capability availability is evaluated independently from local serving
readiness. A connection or query failure marks the optional capability unavailable;
subsequent resolver plans skip it and continue through local paths. Do not add an
elapsed cutoff to fusion or reinterpret a slow valid result as a knowledge failure.
Optional graph I/O releases the core-wide state lock in unified resolution and legacy chat. While a graph-bound
request is outstanding, status, mutations, and another user's local-only resolution
remain available; the same user's contextual requests and an identical request ID
remain serialized. Cooperative cancellation is checked around graph calls but cannot
interrupt a driver call already executing. Graceful shutdown waits for active
resolutions, so operators may still need the deployment supervisor's ordinary hard
process-stop procedure when a driver never returns.

## Startup and readiness

Construct the service with `open_engram_core(...)`. Startup loads authoritative
state, rebuilds disposable projections, synchronizes an optional seed, and performs
component preflight. Do not send traffic when startup raises a persistence,
configuration, or required local-resource error. Graph, graph-vector, sparse, and
their model/index readiness are reported independently and do not prevent serving.
The optional local sparse index rebuilds from accepted-response artifacts and persists
no posting data.

Require `core.status()` to report:

- `state: "running"`, `ready: true`, and `healthy: true`;
- durability other than `degraded`;
- every required component ready; optional graph, graph-vector, and sparse components may be enabled and not ready; and
- the expected persistence path and a reviewed checkpoint state.

The gRPC health service reflects core ready/healthy state. Readiness is not release
authorization and is independent of optional graph readiness.

## Normal operation

- Alert on unhealthy state, degraded durability, resolver failures/unavailability,
  and budget exhaustion without logging raw requests,
  responses, context fingerprints, Claim content, or identifiers as metric labels.
- Treat `PersistenceError.state_changed` as authoritative when deciding whether an
  identical mutation request ID may be retried.
- Call `flush()` before a planned stop and `close()` for final lifecycle ownership.
  A failed flush blocks a clean deployment handoff.
- Keep backups outside the live store path and test restoration on a copy.

## Rollback

1. Stop new mutations and drain or stop adapters. Record the last health,
   durability, source, configuration, state digest, and failure class.
2. Flush the current core when it is safe. If durability is indeterminate, preserve
   all candidate files and use the Section 3 outcome table; do not retry with a new
   mutation ID.
3. Disable the affected resolver, sparse feature, or graph/vector feature in reviewed configuration.
   For a policy-only issue, use the last known-good resolver plan and keep
   `accept_exact` disabled unless that exact policy was separately approved.
4. Restore the known-good source, dependencies, configuration, and a copied
   known-good persistence file as one compatible set. An older binary must not open
   a newer schema in place unless its documented downgrade path explicitly permits
   it.
5. Start without traffic, require preflight/readiness and consistency checks, replay
   a completed idempotent request, and exercise a representative MISS and evidence
   request.
6. Restore traffic gradually. Retain incident artifacts and the failed version until
   the release owner closes the review.

## Incident classes

| Signal | Immediate action |
| --- | --- |
| Graph query is non-responsive | The graph-bound request remains outstanding, but other users' local-only work and status continue outside the graph call; disable graph for new work and use the supervisor's ordinary process-stop procedure if graceful shutdown cannot drain it |
| Optional graph/model/index unavailable | Report the capability not ready and skip its resolvers; provision or disable it independently of local serving |
| Sparse index inconsistent or unavailable | Disable sparse candidates, run the explicit check, and rebuild the disposable projection from authoritative artifacts |
| Durability degraded or checkpoint failed | Stop mutations, preserve files, and follow the recovery outcome table |
| Repository/index inconsistency | Keep service out; rebuild only disposable indexes from authoritative artifacts on a copy |
| Suspected disclosure or authorization failure | Stop affected namespace/service traffic, preserve bounded audit metadata, rotate exposed credentials, and escalate |
| False direct answer regression | Disable direct-answer acceptance or the affected resolver/policy version; retain evidence-only behavior only if separately safe |

## Completion record

This runbook is paired with the
[recorded rollback exercise](rollback-exercise-2026-08-20.md). Together they close
the EGR-1510 documentation deliverable. Section 16 still owns rollout
implementation, protected evaluation, numerical gates, and release approval; this
document grants none of those approvals.
