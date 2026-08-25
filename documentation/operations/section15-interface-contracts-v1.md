# Section 15 interface ownership and compatibility matrix v1

**Status:** EGR-1501, EGR-1503, and EGR-1504 component evidence
**Date:** 2026-08-23

## One core, transport-only adapters

`EngramCore` is the process-owned application boundary. Identity construction,
accepted-response mutation/lifecycle, index publication, unified resolution,
evidence packaging, accounting/feedback, persistence, readiness, and transient
regulated-cache semantics are implemented in `EngramCore` or the feature-owned
modules it coordinates. Adapters may validate their wire input, translate it to
one core call, translate the result, and map typed errors. They do not own a
second repository, receipt ledger, resolver, fusion policy, evidence policy, or
checkpoint coordinator.

| Capability | Authoritative implementation | Python | MCP v1 | gRPC v1 | gRPC v2 |
| --- | --- | --- | --- | --- | --- |
| Query identity and scope | `engram.identity`, `QueryFrameBuilder`, `EngramCore.resolve_request` | Direct mapping boundary | Existing proposal fields only | Existing proposal fields only | `ResolveEvidenceRequest` identity/scope fields |
| Commit and lifecycle | Section 3 repository/coordinator below `EngramCore` | Existing core/lower APIs | Existing learn/retire tools | Existing learn/retire RPCs | No duplicate mutation RPC |
| Unified resolution and evidence | Resolver/orchestrator/fusion/evidence modules owned by `EngramCore` | `resolve_request` | No new tool | No v1 change | `ResolveEvidence` delegates once |
| Index lifecycle | Repository mutation publication and component owners | No adapter implementation | No adapter implementation | No adapter implementation | No adapter implementation |
| Feedback and idempotency | `EngramCore` plus Section 3/6 stores and receipts | Direct core calls | Delegating tools | Delegating RPCs | Resolution request identity delegates to core |
| Persistence/readiness | `EngramCore` and persistence codecs | Core status/flush | Delegating lifecycle | Status, health, flush | Shared health and core state |

## Compatibility evidence

- The MCP protocol test freezes all ten tool names, descriptions, complete input
  properties/defaults/required fields, and the absence of output schemas, then
  exercises the official in-process MCP client.
- The v1 protobuf descriptor test freezes all 14 RPC names, the existing
  proposal-resolution message identity, and its four field numbers. The v1
  source and generated files are unchanged by the v2 addition.
- The v2 descriptor test freezes its request and result field numbers. A network
  test learns through v1, resolves through v2, and observes the shared core
  result and explicit evidence-package envelope.
- The generated-stub test independently regenerates v1 and v2 Python, typing,
  and gRPC files with the pinned compiler and compares every file byte-for-byte.

These are component compatibility checks. Cross-adapter concurrency,
authorization, persistence-schema management, and operational telemetry retain
their separate Section 15 gates.
