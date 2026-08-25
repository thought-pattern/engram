# ADR 0003: Rebuildable derived indexes and bounded evidence wire format

- Status: Accepted
- Date: 2026-08-11
- Applies from: Derived indexes and Claim evidence

## Context

Scoped exact and Claim-support indexes provide bounded accepted-response and graph
support retrieval. Unified resolution returns bounded Claim evidence alongside
response candidates.

## Decision

The scoped exact, statement-to-key, Claim-to-statement, and statement-to-Claim indexes are derived in-memory state. They are rebuilt deterministically from authoritative artifacts at startup, checked before readiness, and atomically swapped into live state.

Evidence wire versions 1 and 2 are bounded structured records. A resolution carries `outcome` (`ANSWER`, `EVIDENCE`, or `MISS`), `wire_version`, selected artifact fields when an answer exists, and up to 10 evidence records. Each evidence record contains only a stable Claim ID, resolver name, concretely typed feature values plus availability booleans, permissible canonical entity/predicate references, validity and trust inputs, a singleton path in version 1 or at most two typed steps in version 2, and stable selection reason codes.

The complete serialized evidence package is limited to 64 KiB. Individual
identifiers are limited to 256 UTF-8 bytes and reason-code collections to 16
entries. The package carries the typed fields listed above and explicit truncation
counts and booleans.

CLI and MCP retain their result shapes. The gRPC v2 evidence service exposes the
unified result alongside gRPC v1; committed stubs come from both protocol sources.

## Consequences

- Authoritative JSON remains portable and repairable; startup pays a measured rebuild cost.
- Support-aware retrieval scales with matched Claim IDs and fan-out.
- Evidence supplies Tapestry with bounded graph support.
- Core validation enforces payload limits.
