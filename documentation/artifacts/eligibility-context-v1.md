# Eligibility context and namespace epoch contract v1

Status: Implemented by EGR-310  
Authority: ADR 0004 and Section 3 of `ENGRAM-DEVELOPMENT.md`

## One request, one reference state

Every accepted-response lookup captures one immutable `EligibilityContext`. All eligibility stages for that request receive the same values:

- canonical RFC 3339 UTC `evaluation_time` plus availability;
- exact namespace;
- nonnegative 64-bit `knowledge_epoch` plus availability;
- authoritative artifact-repository availability; and
- `epoch_source`, one of `standalone`, `trusted_integration`, or `unavailable`.

The deterministic dictionary and compact sorted JSON codecs require every field, reject extras, and never emit null.

## Standalone capture

`EligibilityContextFactory.capture_standalone` calls an injected clock exactly once. The clock must return a timezone-aware UTC `datetime`; local, naive, textual, and ambient timestamps are rejected. The factory reads the namespace epoch once from `NamespaceEpochState`. An uninitialized namespace yields epoch `0`, availability `false`, and source `unavailable`; eligibility work must abstain rather than invent an epoch.

`NamespaceEpochState.initialize` makes initialization explicit and idempotent only when the requested initial value is unchanged. `increment` requires the exact current epoch and one of two reasons:

- `accepted_artifact_eligibility`; or
- `graph_snapshot_activated`.

A successful increment is exactly `current + 1`. Stale expected values conflict, uninitialized namespaces fail, and signed 64-bit exhaustion fails. The lock makes competing expected-epoch increments single-winner. Deterministic snapshots are the feature state later persisted by EGR-309 and coordinated by EGR-314.

## Trusted integration capture

Ordinary request metadata cannot set evaluation time or knowledge epoch. `capture_trusted` accepts only a validated `TrustedEligibilityInput` supplied by a configured trusted integration boundary. It does not consult or combine the standalone clock or namespace epoch state. The record requires an available canonical UTC evaluation time, an exact namespace, a bounded non-empty source label, and a concrete epoch plus availability.

Adapter authentication and authorization of the trusted source remain Section 15 responsibilities. Section 3 defines the record and ensures core eligibility consumes only one of the two explicit capture paths.

## Availability

Repository, time, and epoch availability are facts about dependencies, not truth judgments. EGR-306 converts unavailable required inputs into stable exclusion reasons. `0` is the only epoch value permitted when availability is false; epoch zero may also be a real initialized epoch when availability is true.
