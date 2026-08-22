# Section 10 conformance report — 20 August 2026

## Outcome

Section 10's bounded composition implementation is complete at the transport-neutral core boundary. It supplies the closed algebra and typed plan, conservative compilation, deterministic one/two-hop execution through fixed graph capabilities, safe Boolean and aggregate semantics, version-2 multi-hop Claim evidence, resource and rejection coverage, a versioned engineering regression corpus, and configured live MemGraph evidence.

This is not a release approval. Section 16 retains independent calibration and release authority.

## Requirement evidence

| Task | Implementation and evidence |
| --- | --- |
| EGR-1001 | `engram/composition.py` defines all nine operators, strict step/plan codecs, typed bindings and outputs. |
| EGR-1002 | The compiler accepts only selected canonical identities and fixed plan fields; unsupported, ambiguous, missing, repeated-at-the-same-position, unbound, or cartesian forms abstain. Repeated Predicate occurrences at distinct positions are preserved. |
| EGR-1003 | Hop, row, branch, candidate, path, evidence-byte, output-byte, and working-memory limits are checked before/during execution. Cooperative cancellation propagates. No elapsed answer threshold or required MemGraph timeout exists; an outstanding driver call is isolated from unrelated local work. |
| EGR-1004 | Execution sequences the fixed one-hop capability, validates every binding, reuses temporal/visibility eligibility, revalidates by Claim ID, rejects cycles, deduplicates paths and terminals, and orders results canonically. |
| EGR-1005 | Boolean results require complete branch knowledge except invariant positive `EXISTS`/`OR`; aggregates refuse unknown completeness, duplicate COUNT cardinality, and missing/invalid types. |
| EGR-1006 | evidence record schema 2 and package wire 2 carry ordered one/two-Claim paths with exact bindings, filters, operator, and aggregation inputs. |
| EGR-1007 | Behavior-focused tests cover closed codecs, arbitrary-Cypher rejection, unbound/cartesian/cyclic plans, repeated Predicates, branch requirements, Boolean outcomes, typed aggregates, cycle/partial failure, high fan-out sentinels, duplicate/unknown cardinality, invalid paths, cancellation, total graph-row exhaustion, real resolver publication, and fusion revalidation. |
| EGR-1008 | The 15-case engineering corpus passes both repository-visible splits; all execution is bounded and every abstention retains useful complete or partial evidence. Durations are reported without a timing gate. The configured MemGraph produces a real two-Claim evidence path. Independent value evidence remains Section 16 work. |

## Unit-test value review

The Section 10 tests assert observable safety or integration behavior rather than implementation shape. None is a latency assertion. No test requires an internal container to be a `frozenset`, freezes incidental diagnostic wording, or forces a single known `MULTI` relation to abstain. The plan-field assertion is retained because excluding caller Cypher and time controls is an external security/policy contract. Codec round trips, invalid-path rejection, and exact version rejection are retained because they protect persisted/wire compatibility.

The vertical test found three defects that lower-level success tests did not: invalid whitespace in canonical-resolution evidence, incorrect suppression of all `MULTI` Predicate metadata, and repeated Predicate occurrences being collapsed. Those defects were fixed; this is why the resolver/fusion test remains high value.

## Engineering regression evaluation

[The versioned corpus](../../eval/section10-composition-v1.json) has six development and nine legacy `held_out` engineering cases. Both splits are visible in this repository and are ineligible as independent release evidence. [The reproducible runner](../../scripts/benchmark_composition.py) measures the complete plan/Claim/execution operation for each case and records governed source metadata.

[The recorded result](benchmark-2026-08-20.json) passes 15/15 engineering cases, reports the legacy split accuracy as 1.0, records zero unbounded execution and useful evidence on every abstention, and binds to governed source state. `timing_gate` is explicitly false; durations are descriptive only.

## Configured MemGraph

[The bounded live probe](../../scripts/probe_composition_memgraph.py) uses `config.yml` and only canonical Entity/Predicate lookup, fixed one-hop Claim projection, by-ID revalidation through the core, and structured resolution. It does not issue arbitrary Cypher.

[The live report](live-memgraph-2026-08-20.json) records:

- canonical Sarah and `married_to` identities from the configured MemGraph;
- Sarah → Abraham and Abraham → Sarah/Keturah fixed one-hop rows;
- compilation of `Who is Sarah's married partner married to?` into two `married_to` steps;
- one complete Sarah → Abraham → Keturah path and one rejected Sarah cycle;
- `EVIDENCE` with the two ordered Claim IDs and no direct candidate; and
- an observed complete core duration of 19,754.3644 ms, with no timing gate.

## Verification record

Verification recorded at the original Section 10 capture:

- focused composition/evidence/relation/fusion/resolver suite: 213 passed;
- complete repository: 1,492 passed in 119.93 seconds;
- Ruff, Black (122 files), Pyright, bytecode compilation, Vulture, high-severity Bandit, JSON parsing, and `git diff --check`: clean; and
- [official-client MCP run](section10-mcp-sarah-sushi-memgraph-enabled-1000-turns-2026-08-20.json): 1,000/1,000 turns and 1,003 calls pass, with Sarah's sushi profile retained and five configured MemGraph turns.

The MCP run's observed p50, p95, and maximum turn lengths are 2.4631 ms, 4.9851 ms, and 3,761.5909 ms. These are observations, not a budget or gate. Its graph probes demonstrate live configuration use; the separate composition probe demonstrates the new two-hop resolver itself.
