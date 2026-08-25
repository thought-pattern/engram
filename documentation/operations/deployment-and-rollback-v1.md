# Deployment and rollback runbook v1

**Status:** Current qualified operator procedure
**Owner:** Engram project
**Applies to:** standalone, graph-backed, and Tapestry-backed deployments

## Supported deployment boundary

| Mode | Current control | Release position |
| --- | --- | --- |
| Standalone | `EngramCore`, CLI, MCP, or current gRPC v1 with graph disabled | Qualified |
| Standalone semantic | Separate `semantic.enabled` and `reranker.enabled` controls with checksum-gated local model | Native semantic retrieval qualified; reranker remains unpromoted and disabled |
| Deterministic utilities | `utility.enabled` plus a closed list of selected built-in plugin names | Qualified, optional, and disabled by default |
| Graph-backed | `graph.enabled`, optional `graph.vector_enabled`, fixed relation/Claim capabilities, read-only account | Qualified as an optional fail-soft capability; readiness is reported separately |
| Tapestry-backed | Existing proposal/resolve/learn workflow, Python evidence handoff, separate gRPC v2 evidence service, and namespace rollout policy | Qualified for `regulated_direct_answer` rollout |

The `rollout` configuration selects one policy version, one default mode, and exact
namespace overrides. Supported modes are `disabled`, `shadow`, `evidence_only`,
`regulated_direct_answer`, and `rollback`. The default remains
`regulated_direct_answer`, which preserves the existing `accept_exact` contract.
Deployment policy supplies authorization and release approval. `shadow` executes
resolution and suppresses candidate exposure and credit; `rollback` restricts
work to exact retrieval and returns evidence. See the
[rollout contract](../evaluation/rollout-v1.md).

## Pre-deployment checklist

1. Pin the source revision, dependency set, configuration, policy versions, and
   persisted-state schema being deployed.
2. Provision required NLTK data before startup. Provision spaCy and any local
   embedding model for capabilities expected to report ready. For standalone
   semantic retrieval, record the approved license, immutable model version,
   payload SHA-256, dimension, and backend emitted by
   `scripts/provision_semantic_model.py`. Give serving processes read-only access
   to provisioned artifacts.
3. Preserve an immutable copy of the current persistence file and record its
   SHA-256 digest. Perform checks and repairs on a copied path. When
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
   [accepted-response recovery runbook](../artifacts/accepted-response-recovery.md) for
   quarantine or uncertain mutation outcomes.
5. For graph mode, apply `schema.cypher`, use a database account restricted to
   reads, and inspect the configured graph and optional vector index/dimension.
   Overall Engram readiness uses required components; graph readiness is optional.
6. Bind the service to loopback. Remote deployments use TLS and authentication at
   the service or trusted proxy boundary.
7. Treat the embedding process, MCP host session, or proxy-authenticated gRPC account as the
   principal. Deployment identity and policy grant authority. Chat, proposal, and evidence resolution
   can update learned state, context, candidacy, or accounting and therefore require
   mutation authority.

## Adapter authorization boundary

- Python callers receive only the `EngramCore` operations their embedding process is
  authorized to invoke. Administration and migration remain separate operator
  commands.
- MCP is local stdio. Configure tool grants in the authenticated MCP host and keep
  the process on its stdio boundary.
- For gRPC, authorize fully qualified methods in an interceptor or trusted proxy.
  Observation and mutation methods use separate explicit grants.
- Keep MemGraph credentials read-only for serving even when the adapter principal
  has Engram mutation authority. Graph schema setup uses a separate administrative
  account and process.

## Time and cancellation boundary

The Python `resolve_request` boundary accepts a transient `cancellation_check` callback.
Engram invokes it before expensive resolver work and at cooperative boundaries in
structured, semantic, and composition processing. The callback raises
`ResolutionCancelledError`; a cancelled attempt remains transient and permits
retry with the same request ID.

Graph connection or query failure marks the optional capability unavailable;
subsequent resolver plans continue through local paths.
Optional graph I/O releases the core-wide state lock in unified resolution and legacy chat. While a graph-bound
request is outstanding, status, mutations, and another user's local-only resolution
remain available; the same user's contextual requests and an identical request ID
remain serialized. Cooperative cancellation is checked around graph calls; an
executing driver call continues until the driver returns. Graceful shutdown waits
for active resolutions, and the deployment supervisor handles stalled drivers.

## Startup and readiness

Construct the service with `open_engram_core(...)`. Startup loads authoritative
state, rebuilds disposable projections, synchronizes an optional seed, and performs
component preflight. Open traffic after persistence, configuration, and required
local-resource checks pass. Graph, graph-vector, sparse,
standalone-semantic, reranker, and utility components report their own readiness;
core serving continues when an optional component is unavailable.
The optional local sparse and semantic indexes rebuild from accepted-response
artifacts at startup. The setup workflow provisions semantic models; an absent or
incompatible artifact marks that component unavailable.

Require `core.status()` to report:

- `state: "running"`, `ready: true`, and `healthy: true`;
- durability other than `degraded`;
- every required component ready and reviewed enablement/readiness state for graph,
  graph-vector, sparse, semantic, reranker, and utility components;
- the expected persistence path and a reviewed checkpoint state; and
- `persistence.ready: true`, the expected schema manifest, reviewed migration
  state, and reviewed quarantine counts; and
- the expected schema-version 1 bounded telemetry keys, reviewed checkpoint and
  rebuild state, and resource/exhaustion observations appropriate to
  the deployment. Numerical alert/release thresholds remain separately approved.

The gRPC health service reflects core ready/healthy state.

## Normal operation

- Alert on unhealthy state, degraded durability, resolver failures/unavailability,
  and budget exhaustion through fixed telemetry fields.
- Export the fixed keys returned by `core.operational_telemetry()`.
- Treat `PersistenceError.state_changed` as authoritative when deciding whether an
  identical mutation request ID may be retried.
- Call `flush()` before a planned stop and `close()` for final lifecycle ownership.
  A failed flush blocks a clean deployment handoff.
- Keep backups outside the live store path and test restoration on a copy.

## Rollback

1. Stop new mutations and drain or stop adapters. Record the last health,
   durability, source, configuration, state digest, and failure class.
2. Flush the current core when it is safe. If durability is indeterminate, preserve
   all candidate files, use the accepted-response outcome table, and retain the
   mutation ID.
3. Set the affected namespace to `rollback` (exact-only evidence) or `disabled`,
   then disable the affected resolver, sparse, semantic, reranker, utility plugin, or graph/vector
   feature in reviewed configuration. Semantic and reranker flags are separate;
   either rollback changes transient resolution behavior only.
   For a policy-only issue, use the last known-good resolver plan and enable
   `accept_exact` for approved exact policies.
4. Restore the known-good source, dependencies, configuration, and a copied
   known-good persistence file as one compatible set. Pair each binary with a
   supported persistence schema.
5. Start in pre-traffic mode, require preflight/readiness and consistency checks, replay
   a completed idempotent request, and exercise a representative MISS and evidence
   request.
6. Restore traffic gradually. Retain incident artifacts and the failed version until
   the release owner closes the review.

## Incident classes

| Signal | Immediate action |
| --- | --- |
| Graph query is non-responsive | Other users' local work and status continue; disable graph for new work and use the supervisor process-stop procedure if graceful shutdown stalls |
| Optional graph/model/index unavailable | Report the capability as unavailable, skip its resolvers, and provision or disable it while local serving continues |
| Sparse index inconsistent or unavailable | Disable sparse candidates, run the explicit check, and rebuild the disposable projection from authoritative artifacts |
| Semantic artifact or dimension incompatible | Disable `semantic.enabled`, verify the tracked identity and local payload checksum, then rebuild the disposable projection after provisioning the reviewed artifact |
| Reranker fallbacks or quality regression | Disable `reranker.enabled`; baseline fusion order resumes and indexes/store remain unchanged |
| Utility parser/result regression | Remove the affected name from `utility.plugins`, or disable `utility.enabled`; the rollback changes configuration only |
| Durability degraded or checkpoint failed | Stop mutations, preserve files, and follow the recovery outcome table |
| Repository/index inconsistency | Keep service out; rebuild only disposable indexes from authoritative artifacts on a copy |
| Suspected disclosure or authorization failure | Stop affected namespace/service traffic, preserve bounded audit metadata, rotate exposed credentials, and escalate |
| False direct answer regression | Move the affected namespace to `evidence_only`, `rollback`, or `disabled`; then disable the affected resolver/policy version |

## Completion record

This runbook is paired with the [namespace rollout contract](../evaluation/rollout-v1.md).
`tests/test_operations.py` verifies restoration from an isolated persistence copy,
idempotent receipt replay, absence of later state, and repository/index consistency.
