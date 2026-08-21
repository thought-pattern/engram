# Deployment and rollback runbook v1

**Status:** Current operator procedure; release qualification remains pending  
**Owner:** EGR-1510  
**Applies to:** standalone, graph-backed, and Tapestry-backed deployments

## Supported deployment boundary

| Mode | Current control | Release position |
| --- | --- | --- |
| Standalone | `EngramCore`, CLI, MCP, or current gRPC v1 with graph disabled | Component-supported; production approval is external |
| Graph-backed | `graph.enabled`, optional `graph.vector_enabled`, fixed relation/Claim capabilities, read-only account | Requires the backend bound and readiness checks below |
| Tapestry-backed | Existing proposal/resolve/learn workflow and Python evidence handoff | Future gRPC evidence exposure and staged release controls remain pending |

The current implementation does not provide a namespace rollout matrix, shadow
mode, or an independently approved regulated-direct-answer policy. Do not describe
those future Section 16 controls as deployed. `accept_exact=False`, an explicit
`configured_resolvers` tuple, and graph/vector enablement are engineering controls,
not substitutes for authorization or release approval.

## Pre-deployment checklist

1. Pin the source revision, dependency set, configuration, policy versions, and
   persisted-state schema being deployed.
2. Provision NLTK, spaCy, and any local embedding model before startup. Serving
   processes must have no artifact-download permission.
3. Preserve an immutable copy of the current persistence file and record its
   SHA-256 digest. Copy to a new path; never edit the live file in place.
4. Load and migrate only through the supported persistence codecs. Require the
   response repository and derived-index checks to be consistent. Follow the
   [Section 3 recovery runbook](../artifacts/section3-recovery-runbook.md) for
   quarantine or uncertain mutation outcomes.
5. For graph mode, apply `schema.cypher`, use a database account restricted to
   reads, and verify the configured graph and optional vector index/dimension.
6. Start Memgraph with a deployment-approved positive
   `--query-execution-timeout-sec=<seconds>` value. Memgraph documents this as the
   server maximum query duration. Engram cannot currently attest this server flag,
   so the deployment record must capture it before graph readiness is accepted.
7. Keep the service bound to loopback unless TLS and deployment authentication are
   configured at the service or trusted proxy boundary.

## Time and cancellation boundary

Elapsed time is observability, never an answer-quality threshold. The Python
`resolve_request` boundary accepts a transient `cancellation_check` callback.
Engram invokes it before expensive resolver work and at cooperative boundaries in
structured, semantic, and composition processing. The callback raises
`ResolutionCancelledError`; a cancelled attempt is not cached as a knowledge miss
or completed idempotency result and can be retried with the same request ID.

Cooperative cancellation cannot interrupt a Python thread already blocked inside
`pymgclient.Cursor.execute`. Pymgclient exposes no per-execution timeout argument.
The Memgraph server flag above is therefore the required hard in-flight graph-query
bound. If that bound cannot be verified, deploy with `graph.enabled: false`. Do not
add an elapsed cutoff to fusion or reinterpret a slow valid result as a knowledge
failure.

Reference: [Memgraph query execution timeout configuration](https://memgraph.com/blog/handling-large-graph-datasets).

## Startup and readiness

Construct the service with `open_engram_core(...)`. Startup loads authoritative
state, rebuilds disposable projections, synchronizes an optional seed, and performs
enabled-component preflight. Do not send traffic when startup raises a persistence,
configuration, graph, model, index, or dimension error.

Require `core.status()` to report:

- `state: "running"`, `ready: true`, and `healthy: true`;
- durability other than `degraded`;
- every enabled component both enabled and ready; and
- the expected persistence path and a reviewed checkpoint state.

The gRPC health service reflects core ready/healthy state. Readiness is not release
authorization and does not attest the Memgraph server timeout flag.

## Normal operation

- Alert on unhealthy state, degraded durability, resolver failures/unavailability,
  budget exhaustion, and backend query-timeout events without logging raw requests,
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
3. Disable the affected resolver or graph/vector feature in reviewed configuration.
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
| Graph query exceeds approved duration | Remove graph traffic or disable graph; verify the Memgraph server flag and inspect the fixed query plan |
| Graph/model/index unavailable at startup | Keep the enabled configuration out of service; provision or deliberately disable it |
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
