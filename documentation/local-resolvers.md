# Local resolver contracts

**Status:** Current implemented behavior

Engram provides sparse, standalone semantic, reranking, and deterministic utility
capabilities in addition to exact artifact lookup and graph-backed evidence. The
conversational statement/keyword matcher is separate and never contributes an
accepted-response candidate. Configuration enables each optional capability separately.

## Symbolic retrieval rewrites

Retrieval rewrites transform the resolver request representation. Identity, scope,
temporal inputs, source requirements, accepted text, and AIML behavior remain
unchanged. Rules come from the packaged
`rewrite-rules-v1.json` corpus and use escaped exact, prefix, suffix, or token-sequence
matches with one bounded inherited `{subject}` value.

Rules are deterministic and ordered by priority and identity. Depth, expansion,
cycle, output-size, and operational-time controls prevent unbounded work. Only a
complete fixed point reaches resolver planning; the frame retains the original text,
final representation, and rewrite trace. Disable `retrieval_rewrites_enabled` for
rollback.

## Fielded sparse retrieval

For each request, the fielded BM25 scorer builds bounded working documents directly
from the current active accepted-response artifact snapshot. It reads canonical
requests, aliases, entities, relation, keywords, and technical identifiers. Response
text is included only when explicitly configured. Its working documents and
postings belong to that request and are discarded with its result.

The scorer combines fixed field weights with bounded phrase, proximity, prefix,
character-trigram, and exact technical-identifier signals. Set `sparse.enabled: false`
to disable it.

## Standalone semantic retrieval and reranking

Standalone semantic retrieval embeds canonical requests and aliases using the
reviewed local `all-MiniLM-L6-v2` artifact. Runtime is
CPU-only and offline. Enabling it requires the configured model path, immutable
version, payload checksum, license, backend, and dimension. Provision explicitly:

```powershell
python scripts/provision_semantic_model.py
```

Each request embeds the bounded current artifact snapshot and performs exact cosine
comparison within configured record and scan limits. The embeddings are
request-local working values. Artifact, checksum, model, or dimension failure
affects only semantic retrieval.

The optional `transparent_logistic_v1` reranker scores only the already fused bounded
shortlist with fixed visible coefficients. Failure preserves
the baseline order. Native semantic retrieval is qualified; the reranker remains
unpromoted and disabled. The two flags can be rolled back separately.

## Deterministic utilities

The utility resolver has seven compiled-in operations: arithmetic, Boolean, set,
date/time, unit conversion, SemVer comparison, and UUID/slug validation.
Configuration selects from this closed list of compiled handlers.

Each operation has a bounded grammar and deterministic result. Fusion re-executes a
successful operation and checks its identity and output before granting deterministic
answer authority. Utility results remain transient and outside knowledge accounting.
Disable `utility.enabled` or remove one configured plugin to roll back.

## Status and verification

`core.status()["components"]` reports enablement and readiness for sparse,
semantic, reranker, and utility components through fixed identifiers.
Relevant coverage is in `tests/test_rewrite.py`, `tests/test_sparse.py`,
`tests/test_semantic.py`, `tests/test_reranking.py`, and `tests/test_utilities.py`.
