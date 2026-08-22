# Section 12 provenance and license inventory

**Date:** 2026-08-21  
**Scope:** Sparse engine comparison, selected implementation, and evaluation corpus

## Selected implementation

`fielded_bm25_v1` is implemented entirely in Engram project code under the
repository's Apache-2.0 license. Runtime execution uses the Python standard library
and existing Engram contracts. It introduces no search service, binary extension,
downloaded model, or additional package.

The formulas are conventional BM25 and project-authored bounded phrase, proximity,
prefix, character-trigram, and technical-identifier signals. No third-party search
implementation or corpus was copied into the selected engine.

## Compared implementations

| Engine | Runtime role | License/provenance | Portability |
| --- | --- | --- | --- |
| Existing IDF overlap | Existing production baseline | Engram project code, Apache-2.0 | Python runtime |
| Unfielded BM25 | Benchmark-only comparator | Engram project benchmark code, Apache-2.0 | Python runtime |
| SQLite FTS5 | Benchmark-only comparator | SQLite is public-domain software; `sqlite3` is the Python standard-library binding | Requires a Python SQLite build with FTS5 enabled |
| Fielded BM25 v1 | Selected optional production resolver | Engram project code, Apache-2.0 | Python runtime; no extension or service dependency |

SQLite FTS5 benchmark availability does not become a runtime requirement because
the selected resolver does not import or invoke it.

## Corpus provenance

`eval/section12-sparse-v1.json` contains 28 synthetic documents and 28 synthetic
queries authored for EGR-1202 on 2026-08-21. The cases are purpose-built technical
and long-tail contrasts; they are not copied user data, production traffic, an
external benchmark, or a protected Section 16 partition.

The corpus is repository-visible engineering holdout evidence. It may establish
Section 12 component promotion, but it cannot authorize release or be represented
as independent final evaluation.
