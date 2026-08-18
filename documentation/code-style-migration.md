# Code-style migration

Status: Complete

The authoritative rules are in [code-style.md](code-style.md). This migration is complete only when the entire non-generated Python repository conforms and the full functional, static, formatting, dead-code, security, and style-specific verification gates pass.

## Baseline inventory

The 2026-08-16 AST inventory excludes compiler-generated files under `engram/v1/` and covers 105 Python files in `engram`, `scripts`, `eval`, and `tests`.

| Rule | Baseline evidence |
| --- | ---: |
| Dataclass records to replace with validated dictionaries | 98 |
| Module constants outside `engram/constants.py` | 351 |
| Return statements containing expressions rather than a prior name | 1,301 |
| `None` literals requiring classification and migration | 1,092 |
| Pipe-union expressions/annotations requiring classification | 21 |
| Classes requiring statefulness review | 381 |

The counts are discovery signals, not completion gates by themselves. The final audit must parse the repository and prove each rule directly, because a raw count can include permitted bare returns, enum classes, exception classes, test-only malformed external fixtures, bitwise operations, and compiler-owned files.

## Migration order

1. Identity, artifact, eligibility, and shared constant foundations.
2. Index, repository, mutation, response, and persistence contracts.
3. Resolution, evidence, fusion, resolver, and feedback contracts.
4. Coordination, service, adapters, scripts, evaluation runners, and tests.
5. Repository-wide return/import cleanup and final class-state audit.

Every phase must update consumers and tests in the same change, retain concrete absence at boundaries, and pass its focused tests before the next dependency layer is migrated.

## Validated migration slices

### Response mutation and coordination results

The first dependency-complete slice replaces four production dataclasses with validated plain dictionaries:

- `ResponseMutationResult`;
- `MutationExecutionResult`;
- `CoordinatedResponseState`; and
- `CoordinatedMutationCandidate`.

Their runtime values are exact built-in `dict` objects. Functional `TypedDict` declarations retain precise keyed annotations without introducing stateless classes. Construction, copying, persistence conversion, signature calculation, and malformed-state rejection are functions. `ReceiptClock`, `CheckpointCallback`, `RecoveryLoader`, and `PublicationHook` were removed; their call sites now use `Callable` annotations.

The slice also centralizes the coordinated-state field sets, mutation-execution field set, and response-state schema version in `engram/constants.py`. Every return in `engram/responses.py` and `engram/coordination.py` is now either bare or returns a previously assigned name. The production dataclass baseline is therefore 94, down from 98.

Verification completed on 2026-08-16:

- 41 coordination and response contract tests passed;
- 96 persistence, service, feedback, MCP, and gRPC integration tests passed;
- the migrated dictionary factories explicitly reject extra fields, invalid disposition combinations, invalid publication state, and mutations made after construction; and
- Black, isort, Ruff, and the AST return audit pass for the complete touched Python surface.

### Mutation receipt records and ledger boundary

The complete mutation record layer now uses validated plain dictionaries:

- `ArtifactGenerationChange` uses explicit tuple keys for deterministic ordering and duplicate detection instead of dataclass ordering and hashing;
- `MutationReceipt` retains exact persistent and canonical JSON codecs, deep result freezing, prepared/completed invariants, and normalized generation effects through functions;
- `ReceiptTombstone` is copied and validated when it enters the stateful ledger; and
- `ReceiptLookup` has a separate `receipt_lookup_receipt` decoding function.

Ledger, feedback, response, coordination, persistence, service, and test consumers use keyed access and cannot rely on an object compatibility facade. The mutation receipt and ledger schema versions, all 13 mutation bounds, and every migrated record field set now live in `engram/constants.py` instead of beside the implementation.

All return statements in `engram/mutations.py` now return a previously assigned name or are bare. Runtime `isinstance` checks also use ordinary concrete type tuples rather than union-type expressions. The production dataclass baseline is 90, down from 98. A dependency-complete mutation, coordination, response, persistence, feedback, and service run passed 126 tests; a further MCP, gRPC, and baseline run passed 32 tests. Black, isort, Ruff, and the AST return audit pass for this expanded surface.

### Persistence quarantine

`ResponseQuarantineRecord` is now a validated dictionary. Its former dataclass ordering is an explicit `(statement_id, reason, detail)` key used by migration and load, and its exact persistent codec is implemented with functions. The reason policy, record field set, legacy persistence version, and quarantine bounds are centralized in `engram/constants.py`. `engram/persistence.py` now has no dataclasses and no expression-bearing returns.

The production dataclass baseline is 89, down from 98. All 15 persistence migration/restart tests and all 29 service tests pass after the conversion; Black, isort, Ruff, and the AST return audit pass for the touched surface.

### Repository contracts and state snapshot

The repository layer now uses validated dictionaries for all four former records:

- `TierAdmissionPolicy` validates bounded capacity, eviction policy, and protected hit rate;
- `AdmissionPlan` preserves outcome, candidate-state, eviction, namespace, residency, and lifecycle invariants through functions;
- `RepositoryCheckReport` validates exact issue accounting and has a separate serialization function; and
- `RepositoryState` copies its top-level dictionary and freezes authoritative artifact and compatibility-view mappings so snapshots cannot mutate the stateful repository owner.

`ArtifactRepository` remains a class because it owns a lock, live repository state, and an index owner across calls. Every other repository operation remains or is now a module-level function. Repository limits, record field sets, admission/removal enums, and tier policy identifiers are centralized in `engram/constants.py`. Every `engram/repository.py` return is bare or returns a previously assigned name, and the module contains no dataclass or union expression.

The production dataclass baseline is 85, down from 98. A dependency-complete repository, response, coordination, persistence, service, fusion, and resolver run passed 198 tests. The dictionary-specific tests prove exact built-in `dict` runtime values, malformed field rejection, invariant revalidation after construction, and non-aliasing of returned repository snapshots. Black, isort, Ruff, and the AST return/union/dataclass audit pass for the repository slice.

### Identity records and transport-neutral envelopes

All eight identity dataclasses now use exact validated dictionaries:

- `EntityReference`, `RelationReference`, and `IdentityQualifier` use explicit tuple signatures where uniqueness formerly depended on dataclass hashing;
- `RetrievalKeyBinding` remains a scoped-key/provenance/representation contract while index construction now copies and revalidates each binding;
- `RetrievalRepresentation` exposes separate factory, validator, codec, and scoped-binding functions; and
- `QueryIdentity` copies and revalidates every nested record, preserves exact serialization, and retains concrete empty relation/entity/qualifier/lexical fields;
- `ScopeKey` has functional construction, validation, codecs, and an explicit immutable signature for ordering, equality, and configured visibility grants; and
- `ScopedRetrievalKey` has functional construction, validation, codecs, and an explicit immutable signature for exact-index map keys and duplicate detection.

The containing artifact, query-frame, feedback, negative-resolution, service, repository, persistence, eligibility, index, benchmark, and test consumers now use keyed access and functional codecs. Exact-index maps retain immutable tuple signatures internally rather than attempting to hash mutable dictionary records. Identity schema versions, bounds, enum vocabularies, exact field sets, and concrete empty relation and scope values are centralized in `engram/constants.py`.

The production dataclass baseline is 71, down from 98. The final scope/key slice passed all 145 identity and index tests and all 415 scope-dependent artifact, eligibility, evidence, feedback, fusion, persistence, repository, resolution, resolver, response, coordination, and baseline tests. Black, isort, and Ruff pass for the expanded touched surface, and tests explicitly verify built-in `dict` values, post-construction validation, defensive nested copying, deterministic codecs, and replacement of dataclass-only hashing, ordering, and `replace` assumptions.

### Accepted-response artifact contracts and lifecycle policy

The artifact layer now has no dataclasses. All six former records use validated dictionaries:

- `ArtifactProvenance`, `ArtifactStatistics`, and `CachedResponseArtifact` have separate factory, validator, dictionary codec, and JSON codec functions;
- `LifecycleBaseDecision`, `LifecycleTransitionDecision`, and `HistoricalKeyReuseDecision` are exact policy result dictionaries whose validators recompute and enforce the applicable lifecycle invariant; and
- repository, coordination, response mutation, persistence, eligibility, fusion, resolver, service, benchmark, and test consumers use keyed access without an object compatibility facade.

Artifact construction defensively copies query identity, retrieval representation, provenance, statistics, support IDs, and metadata. Repository entry, snapshots, and lookups revalidate and copy mutable artifact dictionaries, so caller mutation cannot change live authority. Artifact schema versions, limits, exact field sets, lifecycle enums, reason vocabularies, transition maps, and the shared concrete empty mapping now live in `engram/constants.py`. The lifecycle values and transition-map shape remain byte-for-byte and behaviorally compatible with the pre-migration contract.

The production dataclass baseline is 73, down from 98. The complete artifact dependency run passed 328 tests. Black, isort, and Ruff pass for the 21-file migrated surface; `engram/artifacts.py` has no expression-bearing return, dataclass, or pipe-union expression. Tests explicitly prove exact built-in `dict` values, deterministic codecs, malformed post-construction rejection, nested non-aliasing, and repository authority isolation.

### Shared constants and graph projection boundary

Behavioral schema numbers, limits, policy identifiers, thresholds, fixed queries, regular expressions, vocabularies, empty defaults, adapter defaults, and dialogue/identity parsing tables now live in `engram/constants.py`. Eligibility enums and `ClaimProjectionQuery` also moved to the shared enumeration root while their original modules re-export the imported names to existing callers. Module-local logger and generated gRPC descriptor state remain derived runtime state rather than independently configurable application constants. Constructed empty records and default policy objects remain coupled to record migrations and will disappear or move when those contracts become dictionaries and stateless policy classes become functions.

The graph layer's sole dataclass, `ClaimProjection`, is now an exact validated dictionary with separate construction, in-memory validation, graph-row decoding, and serialization functions. External graph `null` values are normalized explicitly by runtime type at the graph boundary; the old generated-`None` sentinel is gone. Core structured, vector, and by-ID capabilities revalidate and copy every graph-client result before retaining it, so caller mutation cannot alias transport-neutral resolver state.

The production dataclass baseline is 70, down from 98. The graph/evidence dependency gate passed 298 tests, and the complete repository passed all 1,430 tests. Black, isort, Ruff, `compileall`, and the production union/`None` AST gates pass for this expanded surface. Tests prove exact built-in `dict` values, post-construction malformed-field rejection, mutation isolation, bounded graph-row decoding, and unchanged fixed-query behavior.

### Eligibility context, epoch, and contextual lookup records

The eligibility layer now has no dataclasses. All six former records use validated dictionaries:

- `NamespaceEpoch` and `EpochIncrement` retain explicit availability, monotonicity, and auditable reason invariants;
- `TrustedEligibilityInput` is revalidated and copied at the configured trusted capture boundary;
- `EligibilityContext` has separate factory, validator, dictionary codec, and canonical JSON codec functions;
- `EligibilityDecision` has separate validation, serialization, and stable context-signature functions; and
- `ContextualExactLookupResult` copies and revalidates its eligibility decisions while retaining the existing exact-index result until the index record layer is migrated.

`QueryFrame` now defensively validates and copies the nested eligibility context, and repository, resolver, fusion, evidence, service, persistence, coordination, and test consumers use direct keyed access without an object compatibility facade. Exact field sets and eligibility-specific bounds are centralized in `engram/constants.py`. Every return in `engram/eligibility.py` is bare or returns a previously assigned name.

The production dataclass baseline is 64, down from 98. The eligibility dependency gate passed 358 tests, the concrete-absence and import-hygiene gate passed all 7 tests, and the complete repository passed all 1,432 tests. Black, isort, Ruff, and `compileall` pass for the migrated surface. Tests explicitly prove exact built-in `dict` values, exact-field rejection, post-construction invariant revalidation, defensive copying, deterministic codecs and signatures, and unchanged eligibility, contextual refresh, persistence, resolution, and adapter behavior.

### Exact-retrieval and support-index contracts

The index layer now has no dataclasses. All 13 former records use validated dictionaries:

- `SupportMatch`, `SupportLookupResult`, and `SupportScanPlan` retain bounded traversal and concrete incomplete-scan behavior;
- `ExactLookupResult` and `RetrievalOwner` retain exact scope, provenance, representation, collision, and truncation semantics;
- `IndexCollisionReport`, `IndexBuildIssue`, `IndexBuildReport`, `IndexCheckIssue`, and `IndexCheckReport` use explicit signatures where dataclass ordering formerly supplied deterministic diagnostics;
- `IndexRepairResult` remains a bounded transport-neutral repair result;
- `IndexProjection` has separate factory, validator, replacement, dictionary codec, and JSON codec functions; and
- `IndexState` is an exact validated dictionary whose nested maps remain immutable and whose snapshot validation defensively copies every mutable owner, projection, and report record.

Exact and support lookups are module-level functions that receive an `IndexState`. `IndexOwner` remains a class because it owns the lock and live index state across calls. Pure state mutations validate and isolate their inputs; trusted internal publication avoids repeatedly revalidating the entire live index while preserving snapshot isolation. The 328-projection internal publication check completes in approximately 0.24 seconds on the migration host, preventing the dictionary conversion from turning corpus initialization into quadratic nested-record validation.

Index state and projection field sets, schema versions, report fields, lookup bounds, reason identifiers, and representation limits are centralized in `engram/constants.py`. Production, repository, eligibility, persistence, response, core, benchmark, and test consumers use keyed access and functional lookup/codecs without an object compatibility facade. Every return in `engram/indexes.py` is bare or returns a previously assigned name.

The production dataclass baseline is 51, down from 98. The index dependency gate passed 173 tests, the CLI boundary passed all 18 tests after the publication-path performance correction, and the complete repository passed all 1,438 tests in 93.37 seconds. Ruff passes repository-wide; Black and isort pass for the 13-file index migration surface; `compileall` passes for `engram`, `scripts`, `eval`, and `tests`; and the AST audit finds no index dataclass or expression-bearing return. Tests explicitly prove exact built-in `dict` values, exact-field rejection, post-construction revalidation, nested non-aliasing, immutable maps, deterministic codecs, atomic owner isolation, corruption detection, and equivalence between incremental mutation and a clean rebuild.

### Claim evidence, disclosure, and bounded package contracts

The complete response-less Claim evidence layer now uses validated dictionaries for all 11 former records:

- canonical references, current validity inputs, supplied-trust inputs, and disclosure decisions retain concrete availability and exact nested copying;
- full Claim evidence records retain allow-listed content, singleton paths, sorted sources and reasons, deterministic codecs, and source-projection invariants;
- evidence packages retain canonical Claim ordering, conflict detection, deduplication, exact omission accounting, and count and serialized-byte truncation before final construction;
- usefulness policies and decisions are data dictionaries, while validation, codecs, and deterministic policy evaluation are module-level functions; and
- visibility grants, visibility authorizations, and Claim eligibility decisions retain exact-scope authorization, fail-closed reasons, current-time evaluation, and publication revalidation.

The shared mutable empty evidence-package instance was replaced by an isolated factory. `ClaimEligibilityEvaluator` and `ExactScopeVisibilityAuthority` remain classes because they retain configured authority state across calls; the stateless visibility and projection protocol classes were removed. `engram/evidence.py` now has no dataclasses, union annotations, or expression-bearing returns. CLI, MCP, and the current gRPC contract remain unchanged.

The production dataclass baseline is 40, down from 98. The dependency-complete evidence, eligibility, resolver, and resolution gate passes all 169 tests. The complete repository passes all 1,439 tests; Ruff passes repository-wide; `compileall` passes for `engram`, `scripts`, `eval`, and `tests`; and Black and isort pass for the migrated surface. Tests explicitly prove exact built-in dictionaries, exact-field rejection, post-construction revalidation, nested non-aliasing, deterministic codecs, malformed external payload rejection, package fitting, disclosure matrices, response-less orchestration, and unchanged adapter behavior.

## Final repository conformance

The historical slice counts above record the migration sequence; they are not the final state. The final 2026-08-16 audit covers all 106 non-generated Python modules in `engram`, `scripts`, `eval`, and `tests`. Compiler-generated protobuf and gRPC modules under `engram/v1` remain excluded and reproducible from their source protocol.

| Rule | Final evidence |
| --- | --- |
| Centralized application constants | `tests/test_code_style.py` rejects unclassified package-level module or class state outside `engram/constants.py`. The permitted derived state is limited to module loggers, names/maps derived from the generated gRPC descriptor and centralized vocabulary, and compiled template expressions derived from centralized regular-expression text. Resolver identities and cost classes are centralized values copied into explicit instance state. Script, benchmark, evaluation, and test module values are local paths or fixture inputs rather than application policy. |
| Logic-free returns | The repository-wide AST gate reports no return containing a call, expression, conditional, comprehension, container construction, or other computation; every return is bare or returns a name. |
| Concrete absence | Production AST coverage for `engram`, `scripts`, and `eval` reports no `None` literal except `-> None` procedure annotations. Representative config, persistence, pipeline, fact, inspection, and report outputs recursively contain no `None` and serialize without JSON `null`. Explicit legacy persistence fixtures still prove documented historical `null` normalization. |
| No union annotations | The repository-wide annotation gate reports no pipe union, `Optional`, or `Union` annotation in any governed module. |
| Dictionaries instead of dataclasses | The repository contains no dataclass decorator and no class-syntax `TypedDict`. Record contracts use functional `TypedDict` declarations, validating factories, defensive validators, and explicit codecs. |
| Classes only for state | Every remaining class either writes or inherits persistent instance state or is an enum/exception type. There are no static/class methods and no stateless namespace classes; parsing, validation, policy, codec, transformation, and selection behavior is implemented as module-level functions. |

`tests/test_code_style.py` makes those six architectural conclusions executable rather than relying on searches or the migration narrative. `tests/test_concrete_absence.py` independently covers production union and absence behavior, now including evaluation runners.

Final verification on the completed worktree:

- pytest 9.1.1: **1,446 passed** in 110.84 seconds;
- learning-loop evaluation: **6 passed, 0 failed**;
- Pyright 1.1.411: **0 errors, 0 warnings**;
- Ruff 0.16.3: **all checks passed**;
- Black 26.5.1: **107 files unchanged** under the 132-column Python 3.11 formatting policy;
- isort 5.13.2: **all governed imports passed**; four generated files were skipped by configuration;
- Vulture 2.16 at 65 percent confidence: **no findings**;
- Bandit 1.9.4 at the configured high-severity gate: **no findings**;
- pip-audit 2.10.1 over every exact pin in `requirements.txt`: **no known vulnerabilities**;
- isolated `pip --dry-run --ignore-installed` resolution of `requirements.txt`: **compatible**;
- `compileall` for `engram`, `scripts`, `eval`, and `tests`: **passed**; and
- `git diff --check`: **passed**; the Windows line-ending notices are informational.

The shared host's global `pip check` remains contaminated by a FastAPI/Starlette conflict outside Engram's dependency set. The isolated resolution above verifies the Engram pins without using that unrelated installation as evidence.
