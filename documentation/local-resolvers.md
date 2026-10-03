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
`rewrite-rules.json` corpus and use escaped exact, prefix, suffix, or token-sequence
matches with one bounded inherited `{subject}` value.

Rules are deterministic and ordered by priority and identity. Depth, expansion,
cycle, output-size, and operational-time controls prevent unbounded work. Only a
complete fixed point reaches resolver planning; the frame retains the original text,
final representation, and rewrite trace. Disable `retrieval_rewrites_enabled` for
rollback.

## Fielded sparse retrieval

The fielded BM25 scorer searches the current active accepted-response artifacts in
the request's scope. It reads canonical requests, aliases, entities, relation,
keywords, and technical identifiers. Response text is included only when explicitly
configured.

Engram keeps a sparse index per scope: each active artifact's document, postings, and
field-length totals, which give the same document frequencies and average lengths as
building them from scratch. Before each search the index is synced to the artifact
snapshot being searched. Artifacts are compared by identity; one whose indexed content
(retrieval, query identity, scope, lifecycle, and response text when included) changed
is re-indexed, while one whose only change is its statistics is not. A search reads
postings for its own terms only, so its cost follows the query rather than the store,
and the working-memory budget charges the per-search state it builds, reserving the
snapshot map and per-term references before building them. Query state refers to the
index's posting maps instead of copying them. When rare anchors or exact identifiers
select a candidate pool, the scorer looks up each candidate's posting directly when
that examines fewer entries than the term's postings; `max_posting_visits` counts
every posting entry examined, whether or not it matches. Tokenization of each field
stops once its token limit is reached. Tests compare
index-backed results with a full rebuild through random adds, removals, and changes.

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

Each request compares the query with every representation in the bounded current
artifact snapshot, using exact cosine similarity within configured record and scan
limits. A representation's embedding record is cached by artifact identity, in the
same way as sparse documents. A new generation of the same statement keeps the
embedding of each unchanged representation text and rebuilds only its record, so
only new or changed text is encoded. Record, scan, and memory budgets are still
checked before any encoding. The budget pass counts and sizes representations
without retaining them, and the scan derives them again one batch at a time, so a
request holds the query, one batch, and the retained shortlist. Artifact, checksum,
model, or dimension failure affects only semantic retrieval.

The optional `transparent_logistic` reranker scores only the already fused bounded
shortlist with fixed visible coefficients. Failure preserves
the baseline order. Native semantic retrieval is qualified; the reranker remains
unpromoted and disabled. The two flags can be rolled back separately.

## Deterministic utilities

The utility resolver has seven compiled-in operations: arithmetic, Boolean, set,
date/time, unit conversion, SemVer comparison, and UUID/slug validation.
Configuration selects from this closed list of compiled handlers.

Each operation has a bounded grammar and deterministic result. Arithmetic follows
Python's operator precedence: `**` is right-associative and binds more tightly than a
unary sign on its left, while its exponent may be signed, so `-2 ** 2` is `-4` and
`2 ** -2` is `0.25`. Time conversion accepts RFC 3339 offsets only with hours 00-23
and minutes 00-59. Fusion re-executes a
successful operation and checks its identity and output before granting deterministic
answer authority. Utility results remain transient and outside knowledge accounting.
Disable `utility.enabled` or remove one configured plugin to roll back.

## Status and verification

`core.status()["components"]` reports enablement and readiness for sparse,
semantic, reranker, and utility components through fixed identifiers.
Relevant coverage is in `tests/test_rewrite.py`, `tests/test_sparse.py`,
`tests/test_semantic.py`, `tests/test_reranking.py`, and `tests/test_utilities.py`.
