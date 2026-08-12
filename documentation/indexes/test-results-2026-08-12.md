# Section 2 verification results

Date: 2026-08-12  
Python: 3.12.2  
pytest: 8.1.1  
Scope: EGR-201 through EGR-211

## Focused index suite

Command:

```powershell
python -m pytest -q tests/test_indexes.py
```

Result:

```text
collected 36 items
36 passed in 29.32s
```

The suite covers projection codec determinism, exact and alias provenance, scope separation, within-artifact deduplication,
cross-artifact collision abstention, ineligible ownership, paired Claim support, invalid-input classifications, bounded reports,
the classification fixture, immutable state, every checker category, generic mutation/rebuild equivalence, stale swaps,
abandoned builds, dry-run and applied repair, concurrent readers and writers, current statement support compatibility, support
updates and eviction, absence of a vector-path corpus scan, and authoritative-content preservation during repair. The
remediation cases additionally cover complete 1,001-owner fan-out before scope/top-k selection, scan-limit abstention, matched
Claim deduplication, explicit empty-set comparison and repair, reproducible report corruption, and atomic-swap rejection.

## Full repository suite

Command:

```powershell
python -m pytest -q
```

Result:

```text
collected 964 items
963 passed, 1 xfailed in 72.68s
```

The remaining strict expected failure is now assigned to EGR-308: the legacy `learn_from_response` compatibility path still
replaces equal lexical-keyword sets until Section 3 supplies authoritative response artifacts and its commit wrapper. The
obsolete Section 0 assertions for absent exact and support indexes were converted into positive Section 2 compatibility tests:
legacy exact lookup is an explicit `MISS`, and current Tapestry support metadata populates both support maps.

## Static, formatting, security, and artifact checks

```text
python -m ruff check engram scripts tests eval
All checks passed!

python -m isort --check-only <Section 2 implementation and test files>
exit 0

npx --yes pyright@1.1.411
0 errors, 0 warnings, 0 informations

python -m bandit -q engram/indexes.py
exit 0; no findings

python -m bandit -q -lll engram/indexes.py engram/core.py engram/eviction.py engram/persistence.py
exit 0; no high-severity findings

git diff --check
exit 0
```

The ordinary Bandit scan of the touched integration files retains three pre-existing low-severity `random.choice` findings in
non-cryptographic conversational response selection in `engram/core.py`; Section 2 adds no security finding. Generated gRPC
code remains unmodified. The full suite includes the AST checks for concrete absence and production optional-union avoidance.

## Engineering benchmark

Command:

```powershell
python scripts/benchmark_indexes.py
```

The offline harness used 30 post-warm-up samples. Lookup samples average batches of 1,000 calls to reduce timer noise. It built
scoped exact indexes at 10,000 and 100,000 projections, measured support lookup fan-out 1/10/100 at 100,000 projections, ran the
full regulated proposal at 5,000 artifacts for the same fan-outs, verified complete output truncation and scan-exhaustion
abstention, and measured rebuild, add, replace, remove, support-update, and traced memory at 5,000 projections.

| Measure | Observed p95 | ADR 0004 comparison |
| --- | ---: | --- |
| Exact lookup, 10,000 projections | 0.031549 ms | slope baseline |
| Exact lookup, 100,000 projections | 0.007102 ms | <= 5 ms; passed |
| 10,000 to 100,000 exact p95 slope | 0.2251x | <= 1.5x; passed |
| Support lookup, fan-out 1 | 0.014483 ms | engineering measurement |
| Support lookup, fan-out 10 | 0.032184 ms | engineering measurement |
| Support lookup, fan-out 100 | 0.236671 ms | engineering measurement |
| Full support proposal, fan-out 1 | 0.6818 ms | <= 30 ms and <= 24.6445 ms; passed |
| Full support proposal, fan-out 10 | 1.0115 ms | <= 30 ms and <= 25.9345 ms; passed |
| Full support proposal, fan-out 100 | 1.6250 ms | <= 30 ms and <= 24.744875 ms; passed |
| Rebuild, 5,000 projections | 434.1384 ms | <= 1,000 ms; passed |
| Add mutation, 5,000 projections | 72.6678 ms | informational; no ADR threshold |
| Replace mutation, 5,000 projections | 99.3789 ms | informational; no ADR threshold |
| Remove mutation, 5,000 projections | 59.4137 ms | informational; no ADR threshold |
| Support update, 5,000 projections | 59.2904 ms | informational; no ADR threshold |
| Peak traced index-build memory, 5,000 | 3,981,121 bytes | <= 11 MiB; passed |

All lookup and candidate-correctness expectations passed. The 100,000-projection state passed the complete consistency checker
with no omitted findings. The full proposal passed both ADR 0004 support gates at every fan-out; a 1,001-owner result was
truncated only after a complete scan, and a deliberately exhausted scan emitted no partial matches. These are Section 2
engineering measurements, not the Section 16 held-out release evaluation.

Benchmark: `benchmark-2026-08-12.json`  
SHA-256: `1d785ddbac21e91d794317d9e11701c2d23900bbcf3089196edaf5b128ebbc24`

Classification fixture: `classification-v1.json`  
SHA-256: `1907254d66978db0f1c84ceab3054e1a4f31e4fe09115b66ca48dcdb60999091`

## Requirement evidence

| Item | Evidence |
| --- | --- |
| EGR-201 | Immutable bounded `IndexProjection`, `IndexState`, exact/support results, build/check/repair reports, strict projection codec |
| EGR-202 | Paired retrieval maps, ownership/direct views, provenance and scope tests, duplicate and collision fixture cases |
| EGR-203 | Paired Claim maps, bounded matched-Claim result, inverse and fan-out tests |
| EGR-204 | Deterministic `build_index_state`, all requested classifications, bounded report tests and fixture |
| EGR-205 | Clean-rebuild comparison across all maps and reproducible diagnostics; explicit empty-set comparison; corrupt report rejection |
| EGR-206 | `IndexOwner`, re-entrant core mutation lock and lock order, immutable snapshots, checked map/report generation swap, reader/writer test |
| EGR-207 | Generic pure and live add/replace/remove/support-update; rebuild-equivalence test after every operation |
| EGR-208 | Complete bounded Claim fan-out, scope-before-top-k scoring, no corpus iteration, over-limit abstention, 1,001-owner regression |
| EGR-209 | Separate current-state and explicit-projection check/repair, meaningful empty set, atomic repair, content preservation |
| EGR-210 | Focused 36-case suite plus full regression, stale/corrupt state, abandoned build, concurrency, legacy/current compatibility |
| EGR-211 | Reproducible 100,000-projection and full 5,000-artifact proposal benchmark, both ADR support gates, rebuild/mutation/memory evidence |
