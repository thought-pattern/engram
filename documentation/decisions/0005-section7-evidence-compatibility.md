# ADR 0005: Pre-exposure Section 7 evidence evolution

- Status: Accepted
- Date: 2026-08-16
- Applies from: Section 7
- Supersedes: The unresolved compatibility choice in ADR 0003; all other ADR 0003 decisions remain active

## Context

Section 4 introduced strict transport-neutral `EvidenceReference`, `ResolverResult`, and `ResolutionResult` codecs while the unified resolution pipeline was still an internal core mechanism. The unified result has not been published by the CLI, MCP tools, the gRPC protocol, or persistence:

- CLI `query` continues to use the legacy query operation;
- MCP continues to expose conversation and proposal operations;
- the existing gRPC `Resolve` RPC resolves a proposal and does not expose unified resolution; and
- transient resolution request caching stores Python values, not a persisted result wire payload.

Treating the pre-exposure Section 4 shape as an externally released version would create migration and compatibility machinery without a consumer boundary. Internal type evolution is not an interface version change. This repository already follows that principle for normalization: ADR 0002 corrected its first releasable shape before exposure rather than inventing a second version.

Section 7 still requires a strict full-Claim record and bounded package. The compatibility decision therefore has to distinguish an in-place core evolution from a future adapter protocol change.

## Decision

### One current core result shape

`ResolverResult` and `ResolutionResult` each retain one exact current schema, numbered `schema_version = 1`. Section 7 evolves those internal shapes atomically:

- `ResolverResult` always has a concrete `claim_evidence` tuple in addition to candidates, minimal evidence references, accounting, diagnostics, and consumption.
- `ResolutionResult` always has concrete `evidence_package_available` and `evidence_package` fields in addition to the unified ANSWER/EVIDENCE/MISS fields.

There is one exact current core result, with no dual decoder, downgrade operation, or compatibility projection. A previously serialized development-only result without the current required fields is rejected by the exact decoder; it was never an adapter or persistence contract.

The minimal `EvidenceReference` remains the support/reference type used by response candidates and legacy graph-reference mechanisms. It is not used as a lossy substitute for a full Claim package.

### New evidence structures

`ClaimEvidenceRecord.schema_version = 1` and `EvidencePackage.wire_version = 1` are exact format markers inside the one current pre-exposure mechanism. They do not create a second internal implementation or describe a migration from an externally released evidence format. External versioning begins only if the future gRPC evidence interface changes that actual system boundary.

The package owns canonical ordering, Claim-ID deduplication, retained/omitted accounting, truncation reasons, and complete size limits. The unified result exposes full Claim records only through that package. Raw producer records are removed from nested resolver diagnostics/results before the unified value is returned, so they cannot bypass eligibility, usefulness, count, byte, or output limits.

### Exact decoding and concrete absence

The maintained core decoders accept only the current exact field sets:

- every required collection and availability flag is present, including concrete empty values;
- missing fields, extra fields, non-integer schema values, and unsupported schema values fail explicitly;
- `evidence_package_available = false` requires the concrete empty package;
- an available empty package means eligible producers completed and package evaluation was available but retained no record;
- `ANSWER` cannot contain response-less package records; and
- `MISS` cannot contain retained evidence.

### Actual adapter boundaries

Section 7 does not change an external interface:

- **CLI:** no unified evidence output is added; existing commands and output remain unchanged.
- **MCP:** no tool input or output is changed; conversation and proposal behavior remain unchanged.
- **gRPC:** the committed `engram.v1` protocol remains unchanged. If Section 15 exposes unified resolution or the evidence package, that work must introduce an explicitly reviewed versioned RPC, message, or service because omission of Claim-only evidence is not semantically safe.
- **Python core:** `EngramCore.resolve_request` returns the one current transport-neutral value. Section 15 owns any separately documented stable Python adapter surface.
- **Persistence:** Section 7 results are transient and introduce no persistence migration. Persisted schemas continue to version independently at their actual durable boundary.

An adapter must not serialize arbitrary graph content, derive disclosure authority from namespace or context text, or imply that the unchanged CLI/MCP/gRPC operations expose the package.

## Boundary matrix

| Boundary | Section 7 behavior |
| --- | --- |
| Resolver to core orchestrator | One current strict `ResolverResult` shape; full records are internal producer values |
| Core orchestration result | One current strict `ResolutionResult` shape; full records appear only in `EvidencePackage` |
| CLI | Unchanged; no unified evidence result |
| MCP | Unchanged; no unified evidence result |
| Current gRPC v1 | Unchanged proposal-resolution RPC; no unified evidence result |
| Future gRPC evidence exposure | Section 15 must define and negotiate the external protocol version |
| Persistence | Unchanged; no resolution-result persistence format |

## Consequences

- The code models the mechanism that exists instead of maintaining an imaginary legacy consumer.
- Section 7 tests one current exact core shape and rejects stale development payloads.
- CLI and MCP conformance remain meaningful regressions because Section 7 does not change those interfaces.
- The gRPC version decision occurs when an actual external message changes and committed stubs can be reviewed and regenerated.
- Full Claim evidence keeps a narrow typed package boundary without a lossy minimal-reference downgrade path.
- Section 15 remains responsible for stable adapter exposure, authorization, redaction, protocol generation, and deployment.

## Verification basis

The decision was checked against the active CLI command path, MCP tool registrations, `engram/v1/engram.proto`, the gRPC `Resolve` implementation, `EngramCore.resolve_request`, persistence ownership, ADRs 0002 and 0003, and the current exact-field codec tests. EGR-702 and EGR-703 implement the strict nested formats; EGR-706 and EGR-710 evolve the single core result path; EGR-711 freezes the unchanged-adapter and future-gRPC handoff; and EGR-712 supplies the complete conformance gate.
