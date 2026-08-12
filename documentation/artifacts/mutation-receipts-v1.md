# Durable mutation receipt contract v1

Status: Implemented by EGR-313  
Authority: ADR 0002 and Section 3 of `ENGRAM-DEVELOPMENT.md`

## Idempotency authority

Every Section 3 mutation has a bounded non-empty `request_id`, closed `MutationOperation`, and canonical payload signature. `canonical_payload_signature` recursively validates a concrete bounded JSON object, serializes it with sorted keys and compact UTF-8-preserving JSON, and records lowercase `sha256:<64 hex>`.

Authoritative query and accepted-hit accounting use the same receipt machinery. Their internal request identities are deterministic SHA-256 derivations of the external proposal request or proposal ID, keeping the receipt key within its UTF-8 bound without persisting caller-controlled text in the internal key.

The request ID is the idempotency key. The signature covers every semantic input to the operation. An exact operation/signature retry receives the original completed `MutationReceipt`; reuse with a different operation or payload yields `CONFLICT` without exposing the previous result.

## Receipt record

A deterministic `MutationReceipt` contains:

- schema version and monotonic ledger sequence;
- request ID, operation, and payload signature;
- stable result code and bounded concrete result object;
- at most 1,024 unique ordered `ArtifactGenerationChange` records, where generation zero denotes absence before creation or after physical removal;
- `PREPARED` or `COMPLETED`; and
- canonical RFC 3339 UTC creation time.

Prepared records carry no effects or result and use `REJECTED_CAPACITY` as a concrete placeholder result code. They return `IN_PROGRESS` and may only advance, at the same sequence and identity, to completed. Completed records are immutable. EGR-314 ordinarily checkpoints a completed receipt with its complete candidate state; prepared exists to make recovery of an explicitly durable precommit record unambiguous.

## Bounded ledger and retry outcomes

`MutationReceiptLedger` is thread-safe and has separate positive limits for live receipts and tombstones. Its complete lookup outcomes are:

| Outcome | Meaning |
| --- | --- |
| `NEW` | No retained knowledge of this request identity. |
| `REPLAY` | Exact completed receipt is returned. |
| `IN_PROGRESS` | Exact prepared receipt is returned; the caller must recover rather than reapply blindly. |
| `EXPIRED` | The result was pruned but a matching tombstone forbids ambiguous replay. |
| `CONFLICT` | Retained receipt or tombstone has another operation or payload. |

When live retention is exceeded, the oldest sequence becomes a tombstone containing only request ID, operation, signature, and sequence. Tombstones are separately bounded and oldest-first. A matching tombstone produces stable `EXPIRED`; it cannot become a new mutation. After a tombstone ages out, the request is outside the advertised retry horizon and appears NEW. Operators must configure both bounds to exceed the documented client retry horizon.

## Restart semantics

The ledger snapshot records schema, both bounds, next sequence, sequence-ordered receipts, and sequence-ordered tombstones. `from_snapshot` validates unique sequences and request IDs and requires `next_sequence` beyond all retained state. After deterministic round trip, REPLAY, IN_PROGRESS, EXPIRED, and CONFLICT behavior is unchanged. EGR-309 embeds this complete snapshot in response persistence v2.

## Concurrency

Recording is serialized. New receipts require the exact next sequence, so competing writers for that sequence have one winner. Exact duplicate recording returns the existing receipt. Changed completed results, invalid prepared promotion, request-ID reuse, stale sequence, and expired identity reuse conflict without advancing the ledger.
