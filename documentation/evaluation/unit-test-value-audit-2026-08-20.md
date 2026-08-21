# Unit-test value audit — 2026-08-20

## Outcome

The complete suite was reviewed test by test against observable product behavior. Sixty-two tests were removed because they duplicated stronger coverage, asserted ordinary container or constructor mechanics, or froze an implementation choice without protecting a user-visible or safety invariant. Resolution budget v2 then removed seven obsolete latency-budget cases: four deadline-only tests and three parameterized obsolete time-field cases. Subsequent component and remediation coverage brings the current suite to 1,499 collected cases, all passing.

Ruff, Black, Pyright, Bandit, and Vulture remain separate engineering analyses. Their
general rules are not duplicated in pytest. A later remediation restored two focused
AST checks for project-specific safety contracts that those tools do not enforce:
production imports are eager and module-scoped, and production annotations use one
concrete type rather than unions. Those checks live in `test_source_contracts.py`.

## Retention standard

A test was retained when a failure would identify at least one meaningful regression in behavior, contract validation, persistence, authorization, disclosure, resource bounds, determinism, concurrency, recovery, or supported integration. A test was removed when its main assertion did one of the following:

- prescribed source syntax, class shape, general import topology, or dependency-file formatting, except for an explicitly documented safety boundary not enforced by the static-analysis toolchain;
- restated a language or collection primitive rather than an ENGRAM invariant;
- checked a default constant without exercising the behavior that depends on it;
- repeated an already-covered branch with no distinct risk or boundary;
- required a mock CRUD implementation that production does not use;
- locked a provisional absence or implementation limitation into the suite.

This standard was also applied to the new Section 8 tests. The retained tests assert strict codecs, bounded inheritance, isolation, persistence, canonical ambiguity, fixed parameterized graph capabilities, eligibility reuse, type handling, phrasing suppression, and time-of-check/time-of-use revalidation. They do not assert private helper decomposition or a preferred parsing algorithm.

## Removed tests

| File | Removed | Reason |
| --- | ---: | --- |
| `test_baseline_gaps.py` | 2 | Encoded temporary missing-feature statements as permanent behavior. |
| `test_code_style.py` | 5 | Source-shape gates for returns, dictionaries, annotations, classes, and module state. |
| `test_concrete_absence.py` | 2 | Duplicated annotation/source-shape policy rather than runtime absence behavior. |
| `test_config.py` | 4 | Asserted literal defaults and set membership without exercising configuration behavior. |
| `test_core.py` | 8 | Trivial wrappers plus redundant multi-sentence variants already covered by pipeline behavior. |
| `test_graph.py` | 5 | Exercised an unused mock CRUD store rather than the fixed production graph boundary. |
| `test_identity.py` | 1 | Froze the identity module's import layout rather than identity behavior. |
| `test_import_hygiene.py` | 3 | Dynamic-import, acyclic-import, and requirements-file formatting gates. |
| `test_indexes.py` | 1 | Asserted one default constant rather than bounded scan behavior. |
| `test_models.py` | 15 | Restated dictionary construction, list membership, counters, and convenience extraction. |
| `test_nltk_data.py` | 5 | Checked local paths and literal package inventory without validating NLP behavior or failure handling. |
| `test_pattern.py` | 2 | Redundant regex/priority examples covered by end-to-end matcher tests. |
| `test_scoring.py` | 2 | Redundant empty/missing-entry cases replaced by one bounded public-behavior test. |
| `test_spacy_features.py` | 1 | Asserted a configuration literal rather than feature behavior. |
| `test_substitutions.py` | 6 | Froze the exact contents of substitution dictionaries instead of testing transformations. |
| **Total** | **62** | |

The two deleted modules, `test_code_style.py` and `test_import_hygiene.py`, consisted entirely of source and repository-layout gates. No product behavior test was moved out of pytest merely to reduce the count.

## Focused source-contract restoration

The repository policy still required eager module-scope imports and concrete
single-type annotations after the broad source-shape suites were deleted. Neither
Ruff nor Pyright expresses those two local constraints, and the implementation had
drifted. `test_source_contracts.py` now enforces only those safety-relevant rules.
It deliberately does not restore gates for direct-return syntax, class shape,
dictionary construction, constant placement, or other stylistic preferences.

## Retained coverage assessment

Every remaining collected case has a behavior-level failure interpretation. Parametrized cases were evaluated as distinct inputs, including malformed values and boundary values. The retained suite covers:

- externally observable conversation, CLI, MCP, and proposed gRPC behavior;
- exact runtime contracts, invalid-input rejection, concrete absence, and serialization round trips;
- identity, eligibility, evidence, feedback, fusion, and resolver authority boundaries;
- mutation atomicity, namespace isolation, lifecycle, concurrency, persistence, and recovery;
- bounded graph, index, resource, output, and working-memory behavior, plus elapsed reporting that does not alter completed results;
- NLP, matching, phrasing, scoring, and dialogue behavior using representative inputs;
- Section 8 contextual and relation behavior through 31 contextual cases and 14 relation cases.

The audit deliberately does not claim that a passing test proves the implementation correct. Several production defects and one flawed test fixture were corrected while applying the standard; assertions were kept only when the intended invariant survived independent review.

## Verification

- `pytest -q`: 1,499 passed in 129.00 seconds.
- Focused cancellation, evaluation-custody, governed-source, source-contract, and rollback checks pass.
- Pyright: 0 errors, 0 warnings, 0 information messages.
- Ruff, Black, isort, Vulture, bytecode compilation, JSON parsing, Markdown-link validation, and diff checks: clean.
- Bandit: 0 medium/high findings and 17 low-severity findings retained for manual review.
