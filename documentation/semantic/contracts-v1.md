# Standalone semantic retrieval and reranking contracts v1

**Status:** Section 13 component contract  
**Date:** 2026-08-22  
**Release authority:** Section 16; both features remain disabled by default

## Authoritative boundary and embedding records

Accepted-response artifacts remain authoritative. The standalone semantic index
is disposable, in-memory state rebuilt from those artifacts. Each version-1
embedding record contains the statement and artifact generation, exact scope and
lifecycle, representation ID, canonical-or-alias origin and alias ordinal, the
request representation, model ID/version/checksum/backend/dimension,
normalization version, and a unit-normalized vector.

Only `retrieval.canonical` and `retrieval.aliases` are encoded. Accepted-response
prose is never an embedding input. Duplicate representations are collapsed
case-insensitively. Oversized inputs are rejected rather than truncated, so the
record never silently represents a different request.

## Artifact and offline policy

`semantic.enabled` requires all of `model_path`, `model_version`, and
`artifact_sha256`. Normal runtime loading requires the exact reviewed model ID,
immutable revision, license ID, native backend, dimension, and payload checksum;
the checksum is not approved merely because configuration supplies it. The local
directory must carry `LICENSE`, and a versioned hash over sorted model payload
paths, sizes, and bytes must match the reviewed checksum before the model is
loaded. Transient Hugging Face `.cache` metadata is excluded from that payload
hash. Injected loaders are a test boundary and do not weaken normal runtime.

Runtime loading is CPU-only and calls `SentenceTransformer` with
`local_files_only=True` and `trust_remote_code=False`. A missing file, checksum
mismatch, missing license, unsupported backend, load failure, or dimension
mismatch marks only standalone semantic retrieval unavailable. It does not block
exact, pattern, lexical, sparse, graph, conversation, or persistence paths.
There is no runtime download fallback.

Provisioning is an explicit setup action:

```powershell
python scripts/provision_semantic_model.py
```

The command pins immutable upstream revision
`826711e54e001c83835913827a843d8dd0a1def9`, downloads only the native model
payload and license, computes the Engram checksum, and writes an ignored local
manifest beneath `data/artifacts/models/`. A later invocation verifies the
manifest identity, license, and complete payload checksum and reuses the existing
artifact without calling the downloader. An incomplete, changed, or conflicting
destination fails closed rather than overwriting or downloading again. The
initial download and manifest are staged in one temporary sibling directory;
checksum verification completes before the model directory is atomically
published, and an interrupted or rejected download leaves no partial destination.
The tracked manifest records the reviewed identity without embedding a machine-local
path. The upstream [model card](https://huggingface.co/sentence-transformers/all-MiniLM-L6-v2)
and [license at the pinned revision](https://huggingface.co/sentence-transformers/all-MiniLM-L6-v2/blob/826711e54e001c83835913827a843d8dd0a1def9/LICENSE)
identify the artifact as Apache-2.0.

## Index lifecycle and search

The owner publishes immutable generations. A full rebuild batch-encodes every
active canonical request and alias. Incremental synchronization encodes only
changed active statement projections and reuses unchanged record tuples.
Supersession, invalidation, retirement, and deletion remove the affected
projection without running the model; reactivation or a new active generation
creates a fresh projection. A consistency check compares repository generation
and all active source representations, and a clean rebuild repairs drift.

The initial CPU index is exact cosine search, not ANN. This keeps the small-corpus
implementation inspectable; `max_records` and `max_scan_records` impose hard
corpus and request bounds. Query bytes, candidate count, vector results, working
memory, and cooperative cancellation share the resolution budgets. A dimension
or artifact mismatch cannot publish a partial index. Mutation success does not
depend on derived-index success.

## Resolver and fusion

The `standalone_semantic` resolver is registered after exact, pattern, lexical,
and sparse retrieval. Exact resolution may short-circuit before it. The resolver
re-reads each current artifact and rechecks generation, exact scope, lifecycle,
required source, and required metadata before emitting a candidate.

Candidates use `CandidateSource.STANDALONE_SEMANTIC`, expose raw
`semantic_score`, alias-match status, matched representation identity and origin
without exposing the stored representation text,
model identity, normalization version, and the common accounting/resource
records. They enter the existing authoritative eligibility and fusion engine and
have no independent answer authority.

## Reranker shortlist contract

`transparent_logistic_v1` accepts only the already fused, score-eligible
shortlist. Configuration independently bounds the shortlist (maximum 64, default
8) and serialized feature input. Inputs are the published
normalized fusion features plus the base fusion score. Version 1 has a fixed
intercept and fixed visible coefficients in `engram/reranking.py`; it has no
learned or hidden state and no additional runtime dependency.

Successful scoring records base score, normalized input features, final score,
implementation, model version, elapsed time, and whether the configured model-time
reporting target was exceeded. Elapsed time is observational and never changes
fusion. Cooperative cancellation propagates without publishing a partial decision.
Input exhaustion, an unavailable model, or an unexpected scorer exception
preserves the pre-rerank order. Disabling
`reranker.enabled` is the complete rollback and requires no data migration.

The current approved pairwise comparison is `unavailable`: no reviewed local
pairwise artifact is provisioned. The benchmark does not download, simulate, or
credit one. Sentence Transformers documents ONNX and int8 options, but those
variants likewise remain unavailable until their runtimes and separately
checksummed artifacts are reviewed and provisioned.

## Health, rollback, and independent promotion

`core.status().components.semantic` exposes enabled/ready state, bounded error
type, loaded artifact identity, repository/index generations, and record count.
The reranker component exposes implementation/model versions and low-cardinality
request, completion, fallback, cancellation, and last-reason counters. Neither
health result contains request or response text.

Semantic and reranker flags are independent. Rollback is configuration-only:
disable the affected flag and restart. Semantic derived state is discarded and
rebuilt if re-enabled; authoritative artifacts and the sparse index do not
change. A model identity change similarly requires restart and a clean rebuild.

The repository-visible engineering gate evaluates the two components
separately. Passing does not grant Section 16 release authority.
