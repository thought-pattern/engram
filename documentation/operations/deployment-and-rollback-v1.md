# Deployment and rollback runbook v1

**Status:** Current operator procedure; release qualification remains pending  
**Owner:** EGR-1510  
**Applies to:** standalone, graph-backed, and Tapestry-backed deployments

## Supported deployment boundary

| Mode | Current control | Release position |
| --- | --- | --- |
| Standalone | `EngramCore`, CLI, MCP, or current gRPC v1 with graph disabled | Component-supported; production approval is external |
| Standalone semantic | Independent `semantic.enabled` and `reranker.enabled` controls with checksum-gated local model | Optional capabilities; readiness and gate verdicts are independent |
| Deterministic utilities | `utility.enabled` plus a closed list of independently selected built-in plugin names | Optional capability; all plugins remain default-off pending release authority |
| Graph-backed | `graph.enabled`, optional `graph.vector_enabled`, fixed relation/Claim capabilities, read-only account | Optional capability; readiness is reported independently |
| Tapestry-backed | Existing proposal/resolve/learn workflow, Python evidence handoff, separate gRPC v2 evidence service, and namespace rollout policy | Controls implemented; release approval remains pending |

The `rollout` configuration selects one policy version, one default mode, and exact
namespace overrides. Supported modes are `disabled`, `shadow`, `evidence_only`,
`regulated_direct_answer`, and `rollback`. The default remains
`regulated_direct_answer`, which preserves the existing `accept_exact` contract.
These controls do not supply authorization or release approval. `shadow` executes
resolution without exposing or crediting candidates; `rollback` restricts work to
exact retrieval and returns evidence rather than a direct answer. See the
[rollout contract](../evaluation/section16-rollout-v1.md).

## Pre-deployment checklist

1. Pin the source revision, dependency set, configuration, policy versions, and
   persisted-state schema being deployed.
2. Provision required NLTK data before startup. Provision spaCy and any local
   embedding model for capabilities expected to report ready. For standalone
   semantic retrieval, record the approved license, immutable model version,
   payload SHA-256, dimension, and backend emitted by
   `scripts/provision_semantic_model.py`. Serving processes must have no
   artifact-download permission.
3. Preserve an immutable copy of the current persistence file and record its
   SHA-256 digest. Copy to a new path; never edit the live file in place. When
   migration is required, use `python scripts/migrate_persistence.py SOURCE OUTPUT`;
   it refuses an existing output and reports bounded quarantine counts.
4. Load and migrate only through the supported persistence codecs. Require the
   response repository and derived-index checks to be consistent. When sparse
   retrieval is enabled, require `check_sparse_index()` to report consistent after
   its startup rebuild. When standalone semantic retrieval is enabled, require
   `components.semantic.ready` and `check_semantic_index()` consistency after its
   startup rebuild. When utilities are enabled, review every configured built-in
   name and require `components.utility` to report that subset ready with the
   expected contract and timezone-database versions. Follow the
   [Section 3 recovery runbook](../artifacts/section3-recovery-runbook.md) for
   quarantine or uncertain mutation outcomes.
5. For graph mode, apply `schema.cypher`, use a database account restricted to
   reads, and inspect the configured graph and optional vector index/dimension.
   Graph readiness is optional and never controls overall Engram readiness.
6. Keep the service bound to loopback unless TLS and deployment authentication are
   configured at the service or trusted proxy boundary.
7. Apply the authority matrix from the
   [security/privacy review](security-privacy-review-2026-08-20.md). Treat the
   embedding process, MCP host session, or proxy-authenticated gRPC account as the
   principal. Do not grant authority from `user_id`, namespace, context fingerprint,
   source label, or metadata. Remember that chat, proposal, and evidence resolution
   can update learned state, context, candidacy, or accounting and therefore require
   mutation authority.

## Adapter authorization boundary

- Python callers receive only the `EngramCore` operations their embedding process is
  authorized to invoke. Administration and migration remain separate operator
  commands.
- MCP is local stdio. Configure tool grants in the authenticated MCP host; do not
  expose the process as an unauthenticated network bridge.
- For gRPC, authorize fully qualified methods in an interceptor or trusted proxy.
  Observation methods may be granted independently; state-changing, lifecycle,
  export, flush, and v2 evidence-resolution methods require their corresponding
  explicit grants.
- Keep MemGraph credentials read-only for serving even when the adapter principal
  has Engram mutation authority. Graph schema setup uses a separate administrative
  account and process.

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
configuration, or required local-resource error. Graph, graph-vector, sparse,
standalone-semantic, reranker, and utility readiness are reported independently and do not
prevent serving.
The optional local sparse and semantic indexes rebuild from accepted-response
artifacts and persist no posting or embedding data. Semantic startup never
downloads a model; an absent or incompatible artifact marks only that component
unavailable.

Require `core.status()` to report:

- `state: "running"`, `ready: true`, and `healthy: true`;
- durability other than `degraded`;
- every required component ready; optional graph, graph-vector, sparse, semantic,
  reranker, and utility components may be enabled and not ready; and
- the expected persistence path and a reviewed checkpoint state; and
- `persistence.ready: true`, the expected schema manifest, no unexpected
  migration requirement, and reviewed quarantine counts; and
- the expected schema-version 1 bounded telemetry keys, no checkpoint or rebuild
  failure requiring action, and resource/exhaustion observations appropriate to
  the deployment. Numerical alert/release thresholds remain separately approved.

The gRPC health service reflects core ready/healthy state. Readiness is not release
authorization and is independent of optional graph readiness.

## Normal operation

- Alert on unhealthy state, degraded durability, resolver failures/unavailability,
  and budget exhaustion without logging raw requests,
  responses, context fingerprints, Claim content, or identifiers as metric labels.
- Export only the fixed keys described by the
  [operational telemetry contract](section15-operational-telemetry-v1.md). Do not
  translate user, namespace, context, statement, Claim, model, endpoint, or arbitrary
  reason values into collector labels.
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
3. Set the affected namespace to `rollback` (exact-only evidence) or `disabled`,
   then disable the affected resolver, sparse, semantic, reranker, utility plugin, or graph/vector
   feature in reviewed configuration. Semantic and reranker flags are independent;
   either rollback changes no authoritative data and needs no migration.
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
| Semantic artifact or dimension incompatible | Disable `semantic.enabled`, verify the tracked identity and local payload checksum, then rebuild the disposable projection after provisioning the reviewed artifact |
| Reranker fallbacks or quality regression | Disable `reranker.enabled`; baseline fusion order remains available and no index or persistence rollback is required |
| Utility parser/result regression | Remove the affected name from `utility.plugins`, or disable `utility.enabled`; no authoritative data, index, or persistence migration is required |
| Durability degraded or checkpoint failed | Stop mutations, preserve files, and follow the recovery outcome table |
| Repository/index inconsistency | Keep service out; rebuild only disposable indexes from authoritative artifacts on a copy |
| Suspected disclosure or authorization failure | Stop affected namespace/service traffic, preserve bounded audit metadata, rotate exposed credentials, and escalate |
| False direct answer regression | Move the affected namespace to `evidence_only`, `rollback`, or `disabled`; then disable the affected resolver/policy version |

## Completion record

This runbook is paired with the
[recorded rollback exercise](rollback-exercise-2026-08-20.md). Together they close
the EGR-1510 documentation deliverable. EGR-1608 adds the namespace rollout
control described above. Section 16 still owns project release evaluation, numerical
gates, and release approval; this document grants none of those approvals.
