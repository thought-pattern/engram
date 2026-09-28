# Engram Remediation Plan

This plan covers the larger issues found in the whole-codebase review of the
`social` branch (PR #29) on 2026-09-27. Smaller defects from that review are
already fixed on the branch (see [Already fixed](#already-fixed)). What remains
here either needs a design decision or is a refactor too large to fold into a
bug-fix pass.

Each item lists the problem, the evidence, the impact, the options, a
recommendation, and acceptance criteria. Measurements come from the review's
reproduction scripts on a Windows workstation; treat them as orders of
magnitude, not benchmarks. Effort is a rough size: **S** (a day or less),
**M** (a few days), **L** (a week or more).

## Summary

| ID | Item | Severity | Effort | Needs a decision |
|----|------|----------|--------|------------------|
| R1 | Per-request work grows with the store, under one lock | High | L | **Done** (2026-09-28) |
| R2 | Service-surface security | High / Medium | M | Yes: deployment model |
| R3 | Request-size contract (16 KB accepted, 4 KB identity) | Medium | S | Yes: resolve limit |
| R4 | Conversation-engine semantics | Medium | M | Yes: topic order |
| R5 | Resolution sizing after durable accounting | Medium | S | No |
| R6 | Graph database operations | Low / Medium | M | No |
| R7 | Engineering pipeline | Medium | M | Partly |
| R8 | Code structure | Medium | M–L | No |

Decisions needed before work starts are collected under
[Open decisions](#open-decisions).

---

## R1. Per-request work grows with the store, under one lock

### Problem

Engram validates complete collections at every boundary and builds retrieval
indexes per request. Every service operation runs under one core-wide lock
(`engram/service.py:854`, `with self.resolution_slot(...), self.lock`). The cost
of each request therefore grows with the number of stored artifacts, receipts,
and patterns, and a slow request blocks every other caller.

### Evidence

| Path | Where | Measured |
|------|-------|----------|
| Mutation publication re-validates the repository and receipt ledger several times per call | `engram/coordination.py:156-222` (`snapshot`, `build_candidate`, `execute`), `engram/repository.py:257`, `engram/mutations.py:510-704` | One `learn_response` with 61 artifacts: 1,213 artifact validations and 11 `MutationReceiptLedger` constructions. 92 ms at 1 artifact, 578 ms at 50, 2.5 s at 100 |
| Exact lookup scans and re-normalizes every artifact | `engram/eligibility.py:584-610` | 48 ms at 200 artifacts, 245 ms at 1,000, on every request |
| Sparse retrieval rebuilds its inverted index per query | `engram/core.py:747`, `engram/sparse.py:255`, `engram/sparse.py:793` | About 0.9–1.0 s per query at 500 artifacts. Around 1,000 small artifacts the 16 MiB default budget is exceeded and it returns **zero matches** while still spending the time |
| Semantic retrieval re-embeds every artifact per query | `engram/core.py:768`, `engram/semantic.py:303`, `engram/semantic.py:392` (`max_scan_records`) | Traced, not timed: one transformer inference per artifact per query, up to 100,000 records |
| Each evicted pattern rebuilds the whole trie | `engram/eviction.py:66` → `engram/pattern.py:576` → `engram/pattern.py:564` | 1.6 s per rebuild at 40k patterns with fallbacks off, 4.6 s with the default stem and lemma fallbacks, under `statement_lock` |
| Evidence trimming drops one item, then re-serializes the rest | `engram/resolvers.py:1402-1410`, `1663-1671`, `1804-1811`, `2044-2057` | Structured fallback with `max_evidence_bytes=2500`: 0.48 s at 50 records, 1.72 s at 100, 5.95 s at 200. Callers can raise `max_evidence` to 1,000 over gRPC |

Already mitigated on this branch: the per-candidate repository snapshot in the
fusion authority, the repository snapshot in `finalize()`, and the double
artifact validation in sparse retrieval. Those now use single-artifact lookups
(`ArtifactRepository.find_artifact` and `has_artifact`) or trust the snapshot
that was just validated.

### Impact

- Latency grows linearly or worse with the store, and the global lock turns
  one slow request into a stall for all callers.
- A modest stream of `LearnResponse`, `Propose`, or accepted `Resolve` calls
  becomes a denial of service at the default capacities (10k artifacts, 10k
  receipts, 10k tombstones).
- Sparse retrieval silently stops working past about 1,000 artifacts.
- Once DYNAMIC capacity is reached, each learned fact evicts a statement, so
  one chat turn can take seconds.

### Recommendation

Work in this order. Each step is independently shippable.

1. **Scale benchmark first (S).** Add a benchmark under `scripts/` that loads
   10k artifacts, 10k receipts and 40k patterns, then times `resolve_request`,
   `learn_response`, `propose`, accepted `resolve`, a chat turn that learns a
   fact at capacity, and eviction. Record a latency budget for each and run it
   in CI as a non-blocking job at first.
2. **Validate once, at ingress (L).** State that has already been validated
   stays trusted inside the process. Only the changed artifact or receipt is
   validated on mutation. Replace full `snapshot()` / re-validate cycles in
   `AtomicMutationCoordinator` with copy-on-write of the changed entries.
   Keep full validation for loading external state (`replace_state`,
   `load_static_data`) and for tests.
3. **Statement IDs in trie leaves (M).** Store the owning statement ID in each
   trie category. Eviction and retirement then delete one path in place
   instead of calling `rebuild_indexes`. This also removes the linear statement
   scan in `Engram._statement_for_match` and fixes re-teaching (R4.1).
4. **Linear trimming (S).** Replace the pop-and-re-serialize loops with the
   pre-sized approach already used by `bound_validated_resolver_result`
   (`engram/resolvers.py:2249`).
5. **Incremental retrieval indexes (L).**
   - Exact lookup: a scoped-key → statement IDs index maintained on mutation.
   - Sparse: an inverted index updated on mutation, not per query.
   - Semantic: an embedding cache keyed by
     `(statement_id, generation, representation)`.
6. **Revisit the global lock (M, only if still needed).** After steps 2–5 the
   per-request work should be small. If the benchmark still shows contention,
   move reads to an immutable published snapshot so they never wait on
   mutations.

### Acceptance criteria

- The benchmark exists, and each operation above stays within its budget at
  10k artifacts and 40k patterns.
- `learn_response` cost does not grow with the number of stored artifacts.
- Sparse retrieval returns matches at 10k artifacts under the default budget.
- Evicting one pattern does not call `rebuild_indexes`.
- Trimming time grows linearly with the number of records.

### Status (2026-09-28): done

Every acceptance criterion is met. The benchmark passes `--check` at 10,000
artifacts, 10,000 prior mutation receipts, and 40,000 patterns
(`python scripts/benchmark_scale.py --sizes 1000 10000 --pattern-sizes 10000 40000 --trim-sizes 50 200 --iterations 10 --check`;
ten iterations after a warm-up, setup objects frozen out of garbage
collection as in the gRPC server).

| Scenario | Before (50 artifacts) | After, p50 / p95 (1,000) | After, p50 / p95 (10,000) | Budget (p95) |
|----------|-----------------------|--------------------------|---------------------------|--------------|
| `learn_response` | 399 ms | 4.7 / 5.8 ms | 5.5 / 6.7 ms | 50 ms |
| `propose` | 385 ms | 8.7 / 9.9 ms | 10.6 / 12.8 ms | 50 ms |
| accepted `resolve` | 367 ms | 6.7 / 8.3 ms | 7.6 / 8.7 ms | 50 ms |
| `resolve_request`, exact | 332 ms | 26.0 / 28.2 ms | 26.8 / 28.2 ms | 50 ms |
| `resolve_request`, sparse | 373 ms; no matches past about 1,300 artifacts | 26.4 / 29.0 ms | 36.4 / 39.7 ms, finds the target | 100 ms |
| `retire_response` | 372 ms | 5.2 / 6.0 ms | 6.1 / 7.0 ms | 50 ms |

| Scenario | Before | After, p50 / p95 | Budget (p95) |
|----------|--------|------------------|--------------|
| Chat turn learning a fact at capacity, 40,000 patterns | full trie rebuild per eviction (1.6–4.6 s) | 2.6 / 2.8 ms | 100 ms |
| Retire one pattern, 40,000 patterns | full trie rebuild | 0.1 / 0.2 ms | 20 ms |
| Evidence trimming, 200 records | 5.95 s | 73.1 / 77.8 ms | 100 ms |

| Acceptance criterion | Result |
|----------------------|--------|
| Benchmark exists; every operation within budget at 10k artifacts and 40k patterns | Met, including 10k seeded receipts |
| `learn_response` cost does not grow with stored artifacts | Met: 4.7 ms at 1k, 5.5 ms at 10k |
| Sparse retrieval returns matches at 10k artifacts under the default budget | Met |
| Evicting one pattern does not call `rebuild_indexes` | Met |
| Trimming time grows linearly with records | Met: 19.6 ms at 50, 73.1 ms at 200 |

What changed:

- **Benchmark (step 1).** `scripts/benchmark_scale.py` times each scenario at
  several sizes against a p95 budget, seeds prior mutation receipts
  (`--receipts`, default 10,000), and checks that sparse retrieval finds its
  target so a fast failure cannot pass. `--check` exits 1 on any miss. Not
  wired into CI yet. `scripts/benchmark_metadata.py` now globs the workflow
  files; it named a `ci.yml` that no longer exists, so every benchmark
  script crashed.
- **Validate once (step 2).** The repository validates an artifact when it
  enters; package code reads live state through `trusted_state()` /
  `trusted_artifacts()`, and public reads return structural copies
  (`engram/copies.py`). Candidates built by the repository or the
  coordinator record the IDs they change and the live state they came from,
  so planning and publishing touch only those IDs; any other candidate is
  fully validated. Staleness uses the state generation and a ledger
  `revision`. Admission no longer revalidates its own candidate or scans
  STATIC artifacts: DYNAMIC IDs are tracked in the repository's index and
  victims come from `heapq.nsmallest`. The feedback store has `clone()` /
  `adopt()` and a snapshot-free `apply_validated()`.
- **Trie and statements (step 3).** Removing a pattern edits only its own
  path; the next entry claiming the path takes over its leaf, and emptied
  nodes are pruned. `Engram.pattern_statements` indexes statements by
  pattern. Statements are looked up through `statement_by_id`, and a
  position comes from a store-order sequence (`statement_position`), so a
  removal no longer renumbers every later statement; `statement_index` is
  now a computed property. DYNAMIC statements sit in a lazily checked LRU
  heap, so eviction and `store()` no longer scan every statement. A
  randomized test checks eviction choices against the old full scan. This
  work also fixed a leak where evicting every carrier of a shared pattern
  left a dead matcher entry. Re-teaching (R4.1) is still open.
- **Trimming (step 4).** Resolver trimming keeps running byte totals
  (`json_array_bytes`). Evidence records are no longer revalidated at every
  hop: projection validation and identifier checks are memoized by exact
  input, each resolver validates its query frame once per loop and passes it
  as trusted, and records are built from components their validating
  constructors just produced (`trusted=True` / `trusted_components=True` on
  internal paths only).
- **Indexes and caches (step 5, decision D4: option C).**
  - Exact lookup and the commit collision check read a key signature →
    statement IDs index kept by the repository.
  - Sparse retrieval keeps a persistent per-scope index (`SparseIndex`):
    documents, postings, and field totals for ACTIVE artifacts. Each search
    syncs it to the artifact snapshot by identity and re-indexes only
    artifacts whose sparse-relevant content changed, so statistics-only
    updates cost nothing. A search reads postings for its own terms only.
    A randomized test compares index-backed results with a full rebuild
    through adds, removals, lifecycle and scope changes, and content and
    statistics edits.
  - Semantic retrieval caches each representation's embedding record by
    artifact identity, so only new or changed text is encoded.
  - `documentation/local-resolvers.md` and `tests/test_sparse.py` describe
    the new model.
- **Garbage collection.** The gRPC server calls `gc.freeze()` after startup;
  without it a full collection over a large store paused a request for about
  350 ms.
- **Global lock (step 6).** Measured with four reader threads and one writer
  at 1,000 artifacts: throughput equals sequential execution because the
  interpreter lock already serializes this CPU-bound work, so a
  published-snapshot read path would reorder the queue without adding
  throughput. Not changed. Lower per-request cost is what shortened the
  queue.

Left over, outside the acceptance criteria:

- **Semantic similarity is computed in Python per record** (about 400
  multiply-adds per record per query). Not in the benchmark because it needs
  the model; a vectorized comparison would be faster but changes
  floating-point summation order.
- **Output truncation in `resolve_with_plan`** still re-encodes the whole
  result per dropped item. It runs only when a result overflows its output
  budget and is bounded by resolver, candidate, and evidence counts.
- **Fixed per-request cost.** About 20 ms of an exact resolve is freezing and
  validating resolution results, independent of store size.
- **Cold start.** The first sparse search after startup derives every
  artifact's document (about 1 ms each); the first learning chat turn loads
  the NLP models (about 400 ms).

---

## R2. Service-surface security

The gRPC and MCP adapters are the trust boundary. Some items below apply to
every deployment; others depend on who can reach the service
([Open decisions](#open-decisions), D1).

### R2.1 Template graph queries substitute text into Cypher (applies everywhere)

- **Problem.** `TemplateProcessor.process_graph_query` substitutes template
  variables into the Cypher text itself (`engram/template.py:465`) before the
  write check. `{get:x}` predicate values are caller-controlled through
  `SetPredicate` and are stored without normalization
  (`engram/service.py:1275`). The review confirmed that a `UNION MATCH` payload
  reaches the driver. Template queries also carry no visibility-scope filter.
- **The write check is a blocklist.** `WRITE_CLAUSE` (`engram/constants.py:1026`)
  misses administrative statements such as `TERMINATE TRANSACTIONS`,
  `STOP ALL STREAMS`, `DUMP DATABASE`, `USE DATABASE`, `ANALYZE GRAPH`,
  `STORAGE MODE`, and `REGISTER REPLICA`.
- **Impact.** No shipped seed file uses `graph_query`, so today only custom
  seeds are exposed. Any seed that does use it lets a user steer the query.
- **Recommendation.**
  - Substitute template variables into `params` only. Reject a template whose
    `query` text contains a substitution token at load time.
  - Replace the blocklist with an allowlist of read-only clause shapes
    (`MATCH`, `OPTIONAL MATCH`, `WITH`, `WHERE`, `RETURN`, `ORDER BY`, `SKIP`,
    `LIMIT`, `UNWIND`) and reject everything else.
  - Apply the visibility-scope filter to template queries as it is applied to
    the fixed queries.
  - Document that the graph database account should be read-only.
- **Acceptance.** An injection test (`{get:x}` holding a `UNION` payload) finds
  the payload only in a parameter. Each administrative statement above is
  refused.

### R2.2 Caller parameters can overwrite visibility parameters (applies everywhere)

- **Problem.** `MemGraphConnection` builds the visibility parameters and then
  applies the caller's parameters over them (`engram/graph.py:767-769`), so a
  parameter with the same name replaces the visibility value.
- **Recommendation.** Apply visibility parameters last, or reject caller keys
  that collide with them.
- **Acceptance.** A query supplying a visibility key still runs with the
  configured visibility value, or it is rejected.

### R2.3 Authentication and user identity (depends on D1)

- **Problem.** The gRPC server has no authentication hook. Any client can pass
  any `user_id` and read or change that user's conversations and predicates.
  TLS is server-side only (`engram/grpc_server.py:431-434`); there is no
  client-certificate option. The documentation leaves authorization to the
  deployer.
- **Options.**
  - **A. Trusted caller only.** Engram runs behind a trusted service (for
    example Tapestry) on loopback or a private network. Document it, keep the
    loopback default, and refuse non-loopback binds unless a flag
    acknowledges it.
  - **B. Direct clients.** Add a server interceptor that authenticates each
    call (mTLS client certificates or a bearer token) and binds the
    authenticated principal to `user_id`, so a caller can only act as itself.
- **Recommendation.** Decide D1 first. Option A is a documentation and
  configuration change; option B is about M effort.

### R2.4 Unbounded per-caller state (applies everywhere)

- **Problem.** Nothing caps the number of conversations
  (`engram/service.py:1191`), the turns kept by a `ConversationRuntime`
  (`engram/conversation.py:183`), or predicates per session (values up to
  16 KB each). With `session_overflow: lru`, one caller can evict other
  users' sessions.
- **Recommendation.** Add configurable caps for conversations, turns per
  conversation, and predicates per session. Return `RESOURCE_EXHAUSTED` when a
  cap is hit instead of evicting another user's state.
- **Acceptance.** Tests show each cap is enforced and a capped caller cannot
  evict another caller's session.

### R2.5 MCP `engram_start(config_path)` (depends on D1)

- **Problem.** The model chooses `config_path`
  (`engram/mcp_server.py:35-44`), but the documentation calls it trusted
  deployment configuration. It can read any local file. A missing path falls
  back to defaults silently (`engram/config.py`, `load_config`). A foreign
  YAML file leaks its key names through validation errors (for example
  `unexpected keyword argument 'db_password'`). It can also point the graph
  connection at any host.
- **Recommendation.** Remove the argument from the MCP tool and take the path
  from the server's own startup configuration, or accept only paths from an
  allowlist. Make a missing explicit path an error. Report unknown keys
  without echoing their names.

### R2.6 Graph database connection hardening (applies everywhere)

- **Problem.** The connection passes no TLS mode (`engram/graph.py:694`), so
  credentials and queries travel in plaintext Bolt. There is no connect or
  query timeout, so an unresponsive graph database serializes reads and
  `close()` waits without a bound.
- **Recommendation.** Add `tls` and `timeout` settings to the `graph` config
  section, pass them to the driver, and bound `close()`.

### R2.7 Error detail (applies everywhere)

- **Problem.** `MutationCoordinationError` embeds the inner exception text
  (`engram/coordination.py:213-217`), which reaches clients as the INTERNAL
  status detail. Unexpected gRPC failures are logged with only the exception
  type (`engram/grpc_server.py:218`), which makes production diagnosis hard.
  `SessionLimitExceededError` maps to an opaque INTERNAL.
- **Recommendation.** Return a stable error code to clients. Log the full
  traceback server-side, at a level operators control. Map session limits to
  `RESOURCE_EXHAUSTED`.

---

## R3. Request-size contract

- **Problem.** The service and frame builder accept requests up to
  `MAX_REQUEST_BYTES = 16_384` (`engram/constants.py:657`), but the request's
  identity is capped at `MAX_CANONICAL_FORM_BYTES = 4_096`
  (`engram/constants.py:209`, enforced at `engram/identity.py:1007` and
  `engram/identity.py:1116`). A resolve request between 4 KB and 16 KB passes
  the boundary and then fails with `IdentityValidationError`.
- **Options.**
  - **A. Enforce 4 KB for resolve requests at the boundary** with a clear
    error, and document it. Chat keeps 16 KB. An identity built from 16 KB of
    text is not a useful exact-match key.
  - **B. Raise the identity limit to 16 KB.** Stored keys and exact-lookup
    work grow accordingly.
  - **C. Build the identity from the first 4 KB.** Silent truncation; not
    recommended.
- **Recommendation.** Option A, subject to D2.
- **Acceptance.** A 5 KB resolve request is rejected at the boundary with a
  message naming the limit. A 5 KB chat message still works.

---

## R4. Conversation-engine semantics

### R4.1 Re-teaching a fact is ignored

- **Problem.** The first category stored at a trie path keeps it
  (`engram/pattern.py:443`, `_index_entry`), and `learn_fact` skips a subject
  it already knows. "Learn that zorblax is blue", then "…is green" replies
  "Got it. Zorblax is green." but keeps answering "blue". The unreachable
  DYNAMIC statement still uses capacity.
- **Recommendation.** With statement IDs in trie leaves (R1 step 3), let a
  newer DYNAMIC fact for the same subject replace the older one and retire it.
- **Acceptance.** Re-teaching changes the answer, and the store holds one
  statement for the subject.

### R4.2 Topic matching order (depends on D3)

- **Problem.** The matcher routes by topic first (`engram/pattern.py:677`). While
  a topic has a `*` category, that catch-all answers everything, including
  inputs a specific default-topic category would match. AIML matches pattern,
  then `that`, then topic, so a specific pattern in the default topic wins.
- **Options.** Match strict AIML order, or keep topic-first and document it.
  No shipped seed data uses topics yet, so either choice is cheap to make now.
- **Recommendation.** Strict AIML order, unless topic-first is intentional.

### R4.3 Year heuristic

- **Problem.** Any "in NNNN" is read as a resolved year with confidence 1.0
  (`engram/temporal.py`, `IN_YEAR_RE`). "MTU limit in 1500 byte frames"
  becomes the year 1500, and "1500" is removed from the lexical terms. (The
  related "in 2024-05-01" case is fixed on this branch.)
- **Recommendation.** Require a plausible year range, or no unit-like noun
  right after the number, or a date context word; otherwise leave the
  expression unresolved.

### R4.4 `ConversationRuntime` shared state

- **Problem.** `ConversationRuntime` keeps an unbounded `turns` list
  (`engram/conversation.py:183`), reads `engram.statements` without the engine's
  locks, and reseeds the process-global `random` module per turn
  (`engram/conversation.py:216-227`), which affects other threads.
- **Recommendation.** Cap turns (R2.4). Read statements through a locked
  accessor. Use a per-conversation `random.Random` instance.

### R4.5 Matching cost and depth on long inputs

- **Problem.** Each wildcard step rebuilds the captured string, making matching
  quadratic in input length: 4k words took 0.17 s and 16k words 2.6 s, under
  the lock. `Engram.pattern_query` and `pipeline` have no input cap (the service
  caps at 16 KB). The walk recurses once per trie edge, so a learned pattern of
  about 2,000 words raises `RecursionError` on inputs that follow it.
- **Recommendation.** Carry capture start and end indexes and join once at a
  leaf. Cap input length in `pattern_query`. Cap learned pattern length.

### R4.6 Lemma and stem fallback captures (low)

- **Problem.** When matching falls back to lemmas or stems, the captures are
  lemmatized ("the store where dog be bark"), and `restore_capture_case`
  cannot recover the original words.
- **Recommendation.** Map fallback token positions back to the original input
  and capture original words.

---

## R5. Resolution sizing after durable accounting

- **Problem.** Receipt-backed accounting is finalized
  (`engram/resolvers.py:2698`) before the final output-size check
  (`engram/resolvers.py:3202`). The final result is larger than the one checked
  earlier, so it can exceed `max_output_bytes` after `query_count` has already
  been recorded. With `max_output_bytes=4096`, 3 of 210 sampled candidate sizes
  failed this way.
- **Recommendation.** Size the complete result, or reserve its fixed overhead,
  before calling `finalize_resolution_accounting`. Nothing may fail after a
  durable write.
- **Acceptance.** A test at the size boundary shows either success, or failure
  with no accounting change.

---

## R6. Graph database operations

### R6.1 Schema changes have no migration path

- **Problem.** Standalone install and reset both require an empty graph
  (`engram/schema_admin.py:182`, `engram/schema_reset.py:24`), and preflight
  requires an exact DDL digest. Any change to `schema.cypher` blocks startup
  against a populated standalone graph.
- **Recommendation.** Version the schema catalog and add forward migrations
  that are verified by digest after they run.

### R6.2 `schema.cypher` is not packaged

- **Problem.** `connect_graph` resolves the schema file relative to the source
  tree (`engram/graph.py:972`, `Path(__file__).parents[1]`), and the file is not
  in `package-data`. A wheel install with the graph database enabled would fail
  preflight. (Plausible; not yet reproduced from a wheel.)
- **Recommendation.** Move `schema.cypher` into the package, add it to
  `package-data`, and load it with `importlib.resources`.

---

## R7. Engineering pipeline

### R7.1 Autoformat workflow

- **Problem.** `.github/workflows/python_formatting.yml` has `contents: write`,
  runs on pull requests and on pushes to `main` and `develop`, runs
  `ruff check --fix`, and pushes "Apply Python autoformatting" commits. Commits
  pushed with `GITHUB_TOKEN` do not trigger other workflows, so the final head
  commit of a pull request is never analyzed or tested. PR #29's head commit
  `6aa4787` is one of these.
- **Recommendation.** Make the workflow check-only (`black --check`,
  `isort --check-only`, `ruff check`) and fail the build on differences. Never
  push to `main` or `develop` from CI. Developers format locally, ideally with
  a pre-commit hook.

### R7.2 Dependency locking and Dependabot

- **Problem.**
  - `requirements.txt` pins direct dependencies only. About 24 transitive
    packages float, including torch, transformers, tokenizers,
    huggingface-hub, safetensors, scipy, scikit-learn, starlette, uvicorn,
    sse-starlette and jinja2. Builds are not reproducible, and `pip-audit`
    cannot see those packages.
  - The single grouped Dependabot update (`"*"` in `.github/dependabot.yml`)
    has already produced pins that could not install together: thinc 9 was
    bumped in `807945f` and reverted in `27fae95`, and `pydantic-core` was
    commented out in `75a42c9`. There are no ignore rules, so the thinc 9
    bump will return.
  - On Linux CI, torch from PyPI pulls CUDA wheels even though Engram runs on
    CPU.
- **Recommendation.**
  - Generate a full lock file (`uv lock` or `pip-compile`) with torch from the
    CPU index.
  - Split the Dependabot group (for example ML stack, service stack,
    tooling).
  - Add ignore rules for thinc major versions and for `pydantic-core`.
  - Run `pip-audit` against the lock file in CI. It currently reports
    GHSA-8mgp-746c-j5xp in nltk 3.10.3 (model save/load path handling, no
    fix released). Engram does not call the affected functions.

### R7.3 Tool versions float

- **Problem.** The `dev` extras (black, isort, ruff, bandit, vulture) are
  unpinned, CI installs them with `pip install --upgrade`, and pyright comes
  from npm at the latest version. Local isort 5.13.2 already disagrees with
  CI on eight files: it collapses multi-line imports that CI keeps.
- **Recommendation.** Pin tool versions in the `dev` extras and in CI, and pin
  pyright.

### R7.4 Coverage

- **Problem.** `pytest-cov` is a dev dependency, but there is no
  `[tool.coverage]` configuration and no coverage threshold in CI.
  `engram/schema_reset.py`, which generates `DROP` statements, has no tests.
  `engram/metrics.py` and `engram/schema_catalog.py` have one or two
  references each.
- **Recommendation.** Add `--cov=engram --cov-branch` with a minimum
  threshold. Add tests for `schema_reset`, `metrics`, and `schema_catalog`.

### R7.5 Local environment and CI coverage

- **Problem.**
  - There is no project virtual environment. Tests run against global
    packages, several of which are below the `pyproject.toml` minimums:
    grpcio 1.83.0, mcp 2.0.0, sentence-transformers 5.7.0, spacy 3.8.15,
    nltk 3.10.2.
  - CI tests only Python 3.12, although `requires-python` is `>=3.12`.
  - Actions are pinned to tags rather than commit SHAs, and jobs have no
    `timeout-minutes` or concurrency settings.
  - Tests that need the spaCy model are skipped silently when the model is
    missing.
- **Recommendation.**
  - Document creating a `.venv` from the lock file.
  - Add Python 3.13 to the CI matrix, or narrow `requires-python`.
  - Pin actions to SHAs, and add timeouts and concurrency groups.
  - In CI, fail rather than skip when the spaCy model is missing.

---

## R8. Code structure

### R8.1 One shared validation module

- **Problem.** Text and timestamp validators are copied across twelve modules
  with different size limits and control-character rules: `artifacts`,
  `composition`, `contextual`, `eligibility`, `feedback`, `fusion`,
  `identity`, `mutations`, `relation`, `resolution`, `rewrite`, `temporal`.
  The mismatches caused most of the input-handling bugs fixed on this branch:
  newline rejection, the 256-byte and 512-byte limits applied to whole
  requests, and 4 KB against 16 KB. The RFC 3339 check exists twice, and the
  `fromisoformat(x[:-1] + "+00:00")` idiom appears about 15 times.
- **Recommendation.** Create one module with `require_text` (with
  `maximum_bytes`, `allow_empty` and a whitespace policy), `require_timestamp`
  and `require_identifier`. Migrate the modules one at a time, with tests
  pinning today's limits.

### R8.2 Split large modules

- **Problem.**
  - Several modules are very large: `resolvers.py` (about 3.2k lines),
    `resolution.py` (3.1k), `constants.py` (3.0k), `feedback.py` (2.8k),
    `fusion.py` (2.4k) and `core.py` (2.1k).
  - `resolve_with_plan` is about 440 lines, and `pattern_query` about 250.
  - `core.py` also holds about 200 lines of seed and table file loading.
  - Most of `resolution.py` repeats one seven-function codec pattern per type.
- **Recommendation.**
  - Move seed and table loading out of `core.py` into its own module.
  - Split `resolvers.py` into one module per resolver.
  - Generate or share the codec boilerplate.
  - Do this after R8.1, so the moves carry the shared validators.

### R8.3 Dead code

- **Problem.**
  - The regex matcher in `engram/pattern.py` (`pattern_to_regex`,
    `match_pattern`, `find_best_match`, `match_result`) is used only by tests,
    and it behaves differently from the production trie.
  - `substitutions.normalize_for_matching` and `template.parse_template` are
    unused.
  - About 46 public functions in the resolution modules have no production
    caller.
- **Recommendation.** Delete the test-only regex matcher and its tests, and
  prune unused codec helpers as modules are split.

### R8.4 Shared mutable defaults

- **Problem.** About 29 function defaults share module-level dictionaries such
  as `EMPTY_MAPPING = {}` (`engram/constants.py:25`). Ruff's mutable-default
  rule (B006) does not catch this, and an in-place change would carry over
  between calls.
- **Recommendation.** Use `types.MappingProxyType({})`, or `None` with a local
  default.

---

## Open decisions

| ID | Question | Affects | Recommended default |
|----|----------|---------|---------------------|
| D1 | Who calls Engram in production, and can a client present any `user_id`? | R2.3, R2.5 | Trusted caller only (option A), documented and enforced by bind address |
| D2 | What is the largest resolve request? | R3 | 4 KB for resolve, 16 KB for chat |
| D3 | Strict AIML topic order, or topic-first? | R4.2 | Strict AIML order |
| D4 | Keep sparse and semantic retrieval request-local, or allow persistent derived state? | R1 step 5 | **Decided 2026-09-28:** persistent per-scope sparse index synced to each search's snapshot (option C); semantic records cached by artifact identity. |

## Proposed sequence

1. **Pipeline guard rails:** R7.1, R7.3, then R7.2. These are small and
   protect every later change.
2. **Measurement:** the R1 benchmark (step 1).
3. **Correctness with no decision needed:** R5, R2.1, R2.2, R2.6.
4. **Validate once:** R1 step 2.
5. **Trie refactor:** R1 step 3, which also delivers R4.1.
6. **Decided items:** R3 (D2), R2.3 and R2.5 (D1), R4.2 (D3).
7. **Incremental indexes:** R1 step 5, then linear trimming (R1 step 4).
8. **Structure:** R8.1, then R8.2 and R8.3.
9. **Remaining semantics:** R4.3–R4.6, R6.

## Already fixed

The same review produced these fixes, which are in the working tree of the
`social` branch with regression tests:

- **Seed loading:** the seed-file duplicate check compares normalized match
  paths (PR #29 review comment), and the `config.example.yml` seed example
  is valid YAML.
- **Deadlocks:** two lock-order deadlocks fixed.
  - Learning and template rendering take `mutation_lock` before
    `statement_lock`.
  - The service keeps its lock during graph I/O inside an Engram lock.
- **Answer correctness:**
  - A redirect reaches alias patterns.
  - "of" and "'s" questions reach the one-hop relation path.
  - The reranker reorders without changing thresholds.
  - An accepted Resolve requires an ACTIVE artifact.
- **Conversation behavior:**
  - `that` matches the last sentence of the previous reply.
  - Polishing leaves authored text (including code) unchanged.
  - Set and bot-property names match with capitals or underscores.
- **Input handling:**
  - Tabs and newlines no longer fail requests.
  - A long unresolved temporal expression is shortened, not rejected.
  - "in YYYY-MM-DD" is not read as a whole year.
  - Relation lookup accepts full-length requests.
  - Rewrite limits keep the original text.
  - Live requests keep every entity.
  - Naive dates are treated as UTC.
  - Mixed key types raise a validation error.
- **Performance:** single-artifact lookups replace whole-repository snapshots
  in the fusion authority and in `finalize()`, and sparse retrieval validates
  each artifact once.
- **Startup and docs:**
  - The gRPC server reports an unreachable graph database cleanly.
  - `config.example.yml` describes startup behavior correctly and notes that
    the `seed.json` catch-all bypasses the fallback tiers.
