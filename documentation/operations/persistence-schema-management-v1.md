# Cross-feature persistence schema management v1

**Status:** Current implemented contract
**Applies to:** Engram persistence v1 and v2

## Version manifest

Every newly written v2 store contains one exact top-level `manifest`. It records the versions needed to interpret authoritative state and rebuild disposable state:

- persistence, response-state, feedback-state, and identity schema versions;
- retrieval normalization and response, sparse, and semantic index versions;
- fusion and feedback policy schema and named policy versions; and
- configured standalone-semantic, reranker, and graph-vector model identities or versions.

The manifest is limited to the versions and model identities listed above. A
manifest that conflicts with stored configuration or runtime support is rejected
before derived state is served.

Existing v2 stores written before this manifest remain readable. Their readiness
report sets `manifest_present: false` and `migration_required: true`. Explicit
migration produces the current manifest. A runtime configuration override may change
a rebuildable model identity; startup then rebuilds derived indexes and preserves the
source file.

## Startup and readiness

After a successful load, `core.status().persistence` reports:

- source and current persistence versions;
- whether a manifest was present and whether migration is required;
- whether the active runtime manifest matches the stored source manifest;
- bounded quarantine counts grouped only by the closed reason code;
- whether compatibility views and indexes were rebuilt; and
- the active bounded manifest.

Unsupported persistence or feature schemas, a conflicting manifest, malformed
authoritative state, repository inconsistency, and pattern/artifact ID collision
fail startup. A successful rebuild reports `ready: true`; `components` reports
optional graph and model readiness.

## Explicit migration

Stop writers, preserve the source file, and choose a new output path:

```powershell
python scripts/migrate_persistence.py .\data\engram-v1.json .\data\engram-v2.candidate.json
```

The command requires a distinct, unused output path. It transforms through the
feature-owned codecs, validates idempotence, loads the result, rebuilds derived
state, and atomically writes the candidate. Its JSON report includes versions,
artifact and quarantine counts, reason counts, the manifest, and idempotence.

Before promotion, inspect the complete `response_state.quarantine` in the candidate through the controlled operator environment, resolve each record using the accepted-response recovery procedure, and exercise adapter readiness against the candidate. Preserve the prior file until rollback validation is complete.

## Downgrade constraint

Version 2 rollback restores the binary, configuration, policies, models, and a
known-good compatible backup together. Keep each persistence file with a binary that
supports its schema.
