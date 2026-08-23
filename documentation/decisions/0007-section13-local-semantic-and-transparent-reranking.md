# ADR 0007: Local native embeddings and transparent shortlist reranking

**Status:** Accepted for Section 13 component completion  
**Date:** 2026-08-22

## Decision

Use the pinned Apache-2.0 `all-MiniLM-L6-v2` native CPU artifact for optional
standalone request/alias embeddings. Keep exact in-memory cosine search until
corpus size or measured latency justifies a separately evaluated ANN index. Use
the fixed `transparent_logistic_v1` scorer for the optional bounded reranker.
Both controls remain disabled by default and can be rolled back independently.

## Alternatives

- The graph support-semantic resolver is not a substitute: it searches Claim
  premises and requires MemGraph, while this feature searches accepted-response
  request representations without graph access.
- ONNX and quantized variants were inspected but not executed because this
  environment has neither the required runtime nor a reviewed provisioned
  artifact. Reporting them unavailable is preferable to an implicit export or
  runtime download.
- A pairwise cross-encoder was not selected because no approved local artifact is
  provisioned and its latency, memory, license, and precision value are therefore
  unmeasured.
- A learned LTR model adds training and calibration state. The fixed logistic
  scorer supplies an auditable baseline with no learned artifact; a learned
  successor must use new disjoint data and a new model version.

## Consequences

The native model adds local artifact storage and roughly the measured RSS shown
in the benchmark. Startup verifies the whole payload checksum. Search is linear
but bounded and simple to rebuild. Reranking adds sub-millisecond observed
overhead on the current corpus and always falls back to baseline order on its
own failure. Future ONNX, quantized, ANN, LTR, or pairwise implementations require
new artifact identities and independent gate evidence; they are not aliases for
this decision.
