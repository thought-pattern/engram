# Engram Section 0 verification results

Date: 2026-08-11
Python: 3.12.2
pytest: 8.1.1
Base commit: `ab9acf0c76f40453c5b11b7679e38c6768b62ffd`
Worktree: dirty Section 0 evidence revision

## Focused characterization and absence contract

Command:

```powershell
python -m pytest -q tests/test_baseline_gaps.py tests/test_concrete_absence.py
```

Result:

```text
collected 10 items
tests\test_baseline_gaps.py x.....
tests\test_concrete_absence.py ....
9 passed, 1 xfailed in 31.05s
```

The strict expected failure is `test_when_and_where_requests_keep_distinct_cached_responses`. It reproduces the current semantic replacement defect and will become an unexpected pass, failing the suite, when the assertion turns green so the marker cannot be forgotten.

The passing cases characterize absence of retrieval aliases, normalization version, exact lookup, independent lifecycle/validity/supersession, and Claim reverse indexes. They also load the sanitized persistence fixture through the production version-1 loader. The concrete-absence tests reject optional union annotations, non-procedural `None` literals, and representative `None`/JSON `null` output while checking legacy-null boundary normalization.

## Full repository suite

Command:

```powershell
python -m pytest -q
```

Result:

```text
collected 804 items
803 passed, 1 xfailed in 50.34s
```

This run includes Python core, persistence, concurrency, CLI, MCP, gRPC, graph, semantic support, NLP, spaCy model, and service lifecycle coverage. `test_committed_generated_stubs_match_the_proto` regenerated the protobuf/gRPC files with installed `grpcio-tools` 1.83.0 and compared them byte-for-byte with the committed files.

## Static and artifact checks

Commands and results:

```text
python -m ruff check scripts/benchmark_section0.py tests/test_baseline_gaps.py tests/test_concrete_absence.py
All checks passed!

python -m ruff format --check scripts/benchmark_section0.py tests/test_baseline_gaps.py tests/test_concrete_absence.py
3 files already formatted

JSON parse validation
validated-json: documentation\baseline\benchmark-2026-08-11.json,
                documentation\baseline\fixtures\regulated-response-v1.json

python -m engram.nltk_data
[DONE] All required NLTK data is available.

spaCy package/model probe
en_core_web_sm=3.8.0
spacy_model_load=core_web_sm
```

The installed Black 24.3.0 whole-tree check did not complete within 300 seconds; it reformatted the benchmark script before timeout. A subsequent single-worker check also timed out silently. The repository declares Black 26.5.1 or newer, so this is recorded as part of the development-tool drift. Ruff's formatter and linter both pass the Section 0 Python files.

## Benchmark reproduction

Command:

```powershell
python scripts/benchmark_section0.py
```

Result: exit 0 in 197.8 seconds and wrote `documentation/baseline/benchmark-2026-08-11.json`.

Artifact digests:

| Artifact | SHA-256 |
| --- | --- |
| `benchmark-2026-08-11.json` | `8114facf69aadf8c583ab6edecff5400a4494713421e13eed9a6577165519672` |
| `regulated-response-v1.json` | `a38bc81c40f50cd543295c3a9d2b92702b44503312033fff4b19e787c85aed14` |

The benchmark is offline: it creates synthetic statements, substitutes a deterministic vector result and embedding, uses temporary directories for persistence timing, and never connects to the configured graph database.

## Remediation audit addendum — 2026-08-12

The evaluation found two additional baseline discrepancies: falsey non-object mapping inputs could bypass truthiness-based validation, and enabled graph/spaCy readiness was not transport-neutral. After remediation, the affected cross-cutting suite passed 96 tests across graph, spaCy, MCP, service, configuration, and concrete-absence behavior. The final full suite collected 928 tests and produced 927 passes with the one strict expected §2–§3 replacement failure retained.

Ruff lint passed across `engram`, `scripts`, `tests`, and `eval`; Ruff formatting and isort passed on all 14 remediation Python files. The installed Black 24.3.0 again exceeded three minutes on a targeted check, consistent with the tool-drift finding above. An expanded Bandit scan reported no high-severity findings.
