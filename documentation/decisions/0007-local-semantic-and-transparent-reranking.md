# ADR 0007: Local native embeddings and transparent shortlist reranking

**Status:** Accepted
**Date:** 2026-08-22

## Decision

Use the pinned Apache-2.0 `all-MiniLM-L6-v2` native CPU artifact for optional
standalone request/alias embeddings. Keep exact in-memory cosine search until
corpus size or measured latency justifies a separately evaluated ANN index. Use
the fixed `transparent_logistic_v1` scorer for the optional bounded reranker.
Both controls remain disabled by default and support separate rollback.

## Selection basis

- Graph support-semantic search covers Claim premises; standalone semantic search
  covers accepted-response request representations.
- The native artifact has a reviewed runtime, checksum, license, and benchmark.
- The fixed logistic scorer provides visible coefficients and deterministic output.

## Consequences

The native model adds local artifact storage and roughly the measured RSS shown
in the benchmark. Startup verifies the whole payload checksum. Search is linear
but bounded and simple to rebuild. Reranking adds sub-millisecond observed
overhead on the current corpus; failure restores baseline order.
