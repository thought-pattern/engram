# Engram Section 0 source audit

Audit date: 2026-08-11
Audit scope: active local Engram source and the Section 0 evidence worktree
Audit method: source inspection, deterministic characterization tests, dependency metadata, generated-stub comparison, and offline benchmarks

## Audited revision and environment

| Item | Audited value |
| --- | --- |
| Repository | `https://github.com/thought-pattern/engram.git` |
| Branch | `graph` |
| Upstream tracking branch | Not configured locally |
| HEAD | `ab9acf0c76f40453c5b11b7679e38c6768b62ffd` |
| HEAD date and subject | `2026-08-08T23:47:52-04:00`, `Update Spacy` |
| Worktree | Dirty; Section 0 and preceding concrete-absence/dependency work are not represented by HEAD |
| Python | CPython 3.12.2 |
| Platform | Windows 10.0.19045, Intel64 Family 6 Model 158, 6 logical CPUs |
| Engram version | 1.1.11 |
| Persistence version | 1 |
| Graph schema | 3.8, dated 2026-08-06 |
| Generated gRPC | `grpcio-tools` 1.83.0; generated runtime floor 1.83.0 |
| Generated protobuf | Python protobuf 7.35.1 |

The benchmark artifact records the same commit, branch, environment, and dirty-worktree flag. HEAD identifies the audited source base, while the worktree state identifies the completed evidence revision. Commit and merge state do not affect implementation completion. Gate A is accepted by the completed, verified evidence package.

## Dependency and resource state

There is no resolver-generated lock file. `pyproject.toml` contains compatible ranges and `requirements.txt` is intended to contain pins, but the latter is not a valid reproducible lock in its audited state.

| Package | Declared runtime value | Installed value | Finding |
| --- | --- | --- | --- |
| PyYAML | `>=6.0.3`; pin `6.0.3` | 6.0.3 | Aligned |
| NumPy | transitive; pins `2.4.4` and `2.5.1` | 2.2.6 | Defect: contradictory pins and environment drift |
| sentence-transformers | `>=5.6.0`; pin `5.6.0` | 5.7.0 | Environment drift; model construction is eager when vector recall is enabled |
| NLTK | `>=3.10.1`; pin `3.10.1` | 3.10.2 | Environment drift |
| spaCy | `>=3.8.14`; pin `3.8.15` | 3.8.15 | Aligned |
| `en_core_web_sm` | one-time setup resource | 3.8.0 | Installed and loadable |
| pymgclient | `>=1.6.0`; pin `1.6.0` | 1.6.0 | Aligned |
| MCP | `>=2.0.0,<3`; pin `2.0.0` | 2.0.0 | Aligned |
| grpcio / health checking | `>=1.83,<1.84`; pin `1.83.0` | 1.83.0 | Aligned |
| protobuf | `>=7.35.1,<8`; pin `7.35.1` | 7.35.1 | Aligned |
| grpcio-tools | dev pin `1.83.0` | 1.83.0 | Aligned; committed stubs reproduce byte-for-byte in the full suite |

All resources listed by `engram.constants.REQUIRED_PACKAGES` resolve through the local, gitignored `data/nltk_data` directory. The spaCy model is installed as an environment package rather than stored in the repository. The local ignored `config.yml` contains the requested graph endpoint `192.168.100.19:7687`, graph access enabled, vector recall disabled, and no configured username or password. The audit and performance harness did not connect to that endpoint.

Development-tool constraints also drift from the installed environment: the declared dev extra requires pytest 9.1.1+, ruff 0.16.1+, Black 26.5.1+, pip-audit 2.10.1+, vulture 2.16+, and setuptools 83+, while the audited environment includes pytest 8.1.1, ruff 0.13.2, Black 24.3.0, pip-audit 2.7.2, vulture 2.11, and setuptools 80.7.1. Tests and Ruff run successfully, but this state is not a lock.

## Documented-baseline implementation map

| Baseline area | Modules and live data | Configuration and persistence | Test coverage | Reconciliation finding |
| --- | --- | --- | --- | --- |
| Core and adapters | `engram/core.py:Engram`; `engram/service.py:EngramCore`; `engram/pipeline.py`; `scripts/cli.py`; `engram/mcp_server.py`; `engram/grpc_server.py`; `engram/v1/engram.proto`. `EngramCore` owns one `Engram`, conversation runtimes, a re-entrant service lock, durability state, and transient regulation state. | `engram_config`, YAML loading, CLI/server store and TLS arguments. Persistence serializes `Engram`, not `EngramCore` or adapter state. | `test_core`, `test_pipeline`, `test_cli`, `test_service`, `test_mcp_server`, `test_grpc_server`, `test_import_hygiene`. | Shared transport-neutral service behavior is present. The Python API can also use `Engram` directly. Single-instance gRPC ownership is documented and tested. |
| Statements and storage | `engram/models.py` statement/keyword factories; `engram/core.py` store, learn, retire, and indexes; `engram/eviction.py`; `engram/persistence.py`. Live records are dictionaries; tiers are `STATIC` and `DYNAMIC`. | Capacity, scoring weights, eviction policy, and static protection live in `engram_config`. Persistence v1 stores full non-secret config, counters, bot/sets/maps/substitutions, statements, keywords, and sessions through atomic replace. | `test_models`, `test_core`, `test_eviction`, `test_concurrency`, `test_recall_support`. | Capability confirmed. Graph passwords are omitted. The persisted keyword index is authoritative enough to retain statistics; `rebuild_index` loses them. |
| Retrieval | `engram/pattern.py:PatternMatcher`; normalization and terms in `engram/text.py`; scoring in `engram/scoring.py`; orchestration in `Engram.query`, `pattern_query`, and `learn_from_response`. | Stemming, WordNet lemmatization/synonyms, spelling, spaCy lemmatization/phrases, weights, recency, stopwords, and fallback fields. Statements persist keywords/pattern/context; keyword entries persist membership and counts. | `test_pattern`, `test_text`, `test_scoring`, `test_core`, `test_spacy_features`, `test_baseline_gaps`. | Pattern and IDF-weighted keyword retrieval are present. There is no versioned query identity, retrieval alias collection, or scoped exact index. Equal keyword sets can replace semantically distinct requests. |
| Conversation | `engram/conversation.py`, `engram/dialogue.py`, `engram/sessions.py`, `engram/pipeline.py`, and conversation paths in `Engram`. Sessions contain per-user predicates, histories, topic, entities, dialogue state, and metadata. | Session count/TTL/overflow, fact learning, contraction expansion, spaCy fact flag, polishing, and fallback fields. Sessions are persisted with the core or separately. | `test_conversation`, `test_dialogue`, `test_sessions` behavior in `test_core`, `test_pipeline`, `test_live_conversation_regressions`, `test_facts_spacy`. | Reported isolation, fact learning, repetition, dialogue, and pronoun behavior is present. Conversation reports are separate generated artifacts. |
| Regulated cache | `EngramCore.propose`, `resolve`, `learn_response`, and `retire_response`; `proposals`, `proposal_requests`, `learn_requests`, `retire_requests`, and `regulated_metrics`. | Store path and checkpoint-on-mutation are service settings. Statement `template.tapestry` carries namespace, context fingerprint, request ID, caller metadata, and support. Proposal/idempotency maps are bounded to 1,000 records and expire after 300 seconds. | `test_service`, regulated MCP tests, gRPC protocol/restart tests, `test_recall_support`, `test_baseline_gaps`. | Exact scope checks, typed verdicts, and retry behavior are present. Proposal and idempotency records are deliberately transient and do not survive restart. Original `keyword_source` is not persisted. |
| Graph | `engram/graph.py:MemGraphConnection`; fixed queries and graph fallback in `engram/core.py`; `engram/template.py` read callback; `scripts/setup_schema.py`; `schema.cypher`. | Host, port, username, password, enablement, and vector fields in `graph_config`; password is excluded from persisted config. Graph data is never copied into Engram persistence except opaque support IDs/metadata supplied with a response. | `test_graph`, `test_config`, `test_config_yaml`, `test_template`, CLI stale-config tests. | Optional read-only canonical Claim/Entity/Predicate recall and active-Claim filtering are present. Schema is 3.8, not the reported 3.5. |
| Semantic support recall | Eager vector model construction in `Engram.__init__`/`_load_graph_embedding_model`; `graph_vector_claims`; `_statement_support_ids`; `vector_supported_matches`; service warm-up and proposal merge. | Vector model name/path, dimension, index name, result limit, similarity floor, and weight. Opaque Claim IDs live under `statement.template.tapestry.support`. | `test_graph`, `test_recall_support`, regulated service/adapter tests, Section 0 benchmark. | Local CPU embedding and support intersection are present. There is no Claim-to-statement reverse index, so every vector proposal scans the statement corpus. |
| Service operation | `engram/service.py` lifecycle/durability; `engram/grpc_server.py` health, errors, TLS, and shutdown; MCP/CLI ownership paths; atomic persistence checkpoints. | Store/checkpoint settings and server CLI TLS/listen parameters; graph failures degrade recall. Only durable `Engram` state is persisted. | `test_service`, `test_mcp_server`, and gRPC health, deadline, durability recovery, restart, TLS, drain, shutdown-retry, and generated-stub tests. | Reported health, typed errors, TLS, graceful shutdown, retry safety, and durability degradation are present. Transient proposals intentionally disappear on restart. |

## Concrete-absence and annotation audit

`tests/test_concrete_absence.py` parses every maintained Python module under `engram/` and `scripts/`, excluding generated protobuf files. It rejects `T | U`, `Optional[T]`, and `Union[...]` annotations, and rejects Python `None` literals except the return annotation on procedures. It also recursively checks representative config, persistence, pipeline, fact, inspection, and report output for `None`/JSON `null`, and verifies that legacy-null persistence inputs normalize immediately to concrete empty values.

The row-by-row structures above currently use `""`, `{}`, `[]`, `()`, `set()`, `0`, `0.0`, and `false` according to their boundary or live representation. Generated protobuf code is compiler-owned and is checked by byte-for-byte regeneration instead of the handwritten-code annotation rule.

The remediation audit found that `engram/spacy_setup.py:get_nlp` was still first-call cached and enabled graph connectivity was not included in core readiness. `Engram` now runs a component preflight during construction: enabled graph access must be available, vector recall must load and probe its configured index, and each enabled spaCy pipeline is loaded before the instance can serve. Request paths retain cached models and fail-soft behavior only for outages after successful startup.

## Confirmed discrepancies and disposition

| ID | Discrepancy | Evidence | Disposition |
| --- | --- | --- | --- |
| BASE-001 | Source documentation reported branch `pdc-3` and Schema 3.5; the active branch is `graph` and schema is 3.8. | Git metadata and `schema.cypher` | README and gRPC schema references corrected to 3.8; the tracker retains the reported baseline as historical input. |
| BASE-002 | `requirements.txt` has contradictory NumPy pins and differs from installed sentence-transformers/NLTK versions; dev requirements also drift. | Dependency table above | Open packaging defect; do not describe the file as a lock. |
| BASE-003 | Equal-keyword `when`/`where` learned responses replaced one another. | Resolved by EGR-308 through artifact base commit and exact scoped proposal lookup; the regression is a normal passing test. | Resolved 2026-08-12. |
| BASE-004 | No retrieval aliases, normalization version, or scoped exact index exists. | Passing characterization tests | Planned by §§1-2. |
| BASE-005 | Tier gates retirement and lifecycle/validity/supersession fields are absent. | Passing characterization test | Planned by §3. |
| BASE-006 | Support recall scans all statements because no Claim reverse index exists. | Passing characterization test and scaling benchmark | Planned by §2. |
| BASE-007 | Original keyword source plus proposal and idempotency records are not persisted. | Sanitized fixture and restart tests | Preserve transient proposal behavior; typed artifacts must persist identity/retrieval source in §3. |
| BASE-008 | spaCy pipelines were first-call cached rather than eagerly initialized. | `engram/spacy_setup.py`, `Engram.preflight_components`, and startup tests | Resolved in the Section 1 remediation: enabled pipelines load during construction; no runtime download occurs. |
| BASE-009 | Falsey non-object metadata and configuration values could pass truthiness-based validation and normalize to `{}`. | Reproduction against `EngramCore.propose`, `learn_response`, and config boundaries | Resolved: current public boundaries validate the concrete dictionary type before copying; legacy persisted `null` remains an explicitly tested compatibility case. |
| BASE-010 | An enabled but unreachable graph client could coexist with `ready: true`; only the gRPC vector path had an additional warm-up. | `create_graph_client`, `EngramCore.status`, MCP/gRPC startup paths | Resolved: transport-neutral preflight runs before Python, MCP, CLI, or gRPC serving, and bounded component readiness is part of status. |

## Performance baseline summary

The complete machine-readable artifact is `benchmark-2026-08-11.json`; it uses 30 samples per proposal case, deterministic synthetic content, no network, no MemGraph connection, and a synthetic fixed vector result/embedding.

| Case | p50 | p95 |
| --- | ---: | ---: |
| Lexical proposal, 100 artifacts | 0.2601 ms | 0.4040 ms |
| Lexical proposal, 1,000 artifacts | 0.2643 ms | 0.4288 ms |
| Lexical proposal, 5,000 artifacts | 0.2661 ms | 0.3787 ms |
| Support proposal, 5,000 artifacts / fan-out 1 | 17.2344 ms | 19.7156 ms |
| Support proposal, 5,000 artifacts / fan-out 10 | 18.7974 ms | 20.7476 ms |
| Support proposal, 5,000 artifacts / fan-out 100 | 18.1374 ms | 19.7959 ms |
| Warm core construction | 0.0304 ms | 0.0452 ms |
| Fresh process import and construction | 24,157.5073 ms | 29,910.1450 ms |
| Atomic save, 5,000 artifacts | 159.7321 ms | 447.6939 ms |
| Load, 5,000 artifacts | 56.4605 ms | 267.1175 ms |

Repeated proposal requests returned a stable candidate and idempotent retries were marked at every corpus size. Peak `tracemalloc` build memory was 5,251,919 bytes for the 5,000-artifact lexical path and 8,101,287 bytes for the largest observed support path. The approximately linear support latency confirms the missing reverse-index cost. Fresh-process time includes eager import of all hard dependency modules; it does not connect to external services or load a graph vector model while graph/vector flags are disabled.

## Evidence index

- Characterization and fixture contract: `tests/test_baseline_gaps.py`
- Concrete absence contract: `tests/test_concrete_absence.py`
- Sanitized response and transient-state fixture: `documentation/baseline/fixtures/regulated-response-v1.json`
- Reproducible benchmark harness: `scripts/benchmark_section0.py`
- Benchmark output: `documentation/baseline/benchmark-2026-08-11.json`
- Test output: `documentation/baseline/test-results-2026-08-11.md`
- Accepted decisions: `documentation/decisions/0001-authoritative-response-artifact-and-absence.md` through `0004-evaluation-time-epoch-and-release-gates.md`
