# Cross-feature persistence schema management v1

**Status:** Implemented by EGR-1505
**Applies to:** Engram persistence v1 and v2

## Version manifest

Every newly written v2 store contains one exact top-level `manifest`. It records the versions needed to interpret authoritative state and rebuild disposable state:

- persistence, response-state, feedback-state, and identity schema versions;
- retrieval normalization and response, sparse, and semantic index versions;
- fusion and feedback policy schema and named policy versions; and
- configured standalone-semantic, reranker, and graph-vector model identities or versions.

The manifest contains no model path, graph credential, request text, response text, customer identifier, or Claim content. A manifest whose fields or values disagree with the stored configuration and this runtime is rejected before derived state is served.

Existing v2 stores written before this manifest remain readable. Their readiness report sets `manifest_present: false` and `migration_required: true`; normal startup does not rewrite them. An explicit migration produces the current manifest. A runtime configuration override may intentionally change a rebuildable model identity. Startup reports `runtime_manifest_matches_source: false`, rebuilds derived indexes under the active configuration, and does not mutate the source file.

## Startup and readiness

After a successful load, `core.status().persistence` reports:

- source and current persistence versions;
- whether a manifest was present and whether migration is required;
- whether the active runtime manifest matches the stored source manifest;
- bounded quarantine counts grouped only by the closed reason code;
- whether compatibility views and indexes were rebuilt; and
- the active bounded manifest.

Unsupported persistence or feature schemas, a conflicting manifest, malformed authoritative state, repository inconsistency, and pattern/artifact ID collision still fail startup. A successful rebuild reports `ready: true`; optional graph or model readiness remains independent in `components`.

## Explicit migration

Stop writers, preserve the source file, and choose a new output path:

```powershell
python scripts/migrate_persistence.py .\data\engram-v1.json .\data\engram-v2.candidate.json
```

The command never edits the source and refuses an identical path or an existing output. It transforms through the feature-owned codecs, validates a second migration as identical, loads the result, rebuilds derived state, and only then atomically writes the candidate. Its JSON report includes versions, artifact count, quarantine count and reason counts, the manifest, and the idempotence result; it contains no quarantined identifiers or content.

Before promotion, inspect the complete `response_state.quarantine` in the candidate through the controlled operator environment, resolve each record using the Section 3 recovery procedure, and exercise adapter readiness against the candidate. Preserve the prior file until rollback validation is complete.

## Downgrade constraint

There is no automatic v2-to-v1 downgrade. V1 cannot represent accepted-response lifecycle, generations, namespace epochs, durable mutation receipts, typed feedback, or quarantine. Roll back the binary, configuration, policies, models, and a known-good compatible backup together. Never strip v2 fields or open the live v2 file in place with a v1-only binary.
