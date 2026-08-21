# Contextual relation contracts v2

Status: current internal Section 8 contract, superseding the timeout-bearing one-hop plan in v1.

Section 8 retains the compact previous-frame, bounded inheritance, canonical entity and Predicate resolution, expected-object-type, fixed-query, Section 7 disclosure/revalidation, deterministic phrasing, and evidence-package behavior documented by v1. Version 2 changes timing authority and the one-hop plan shape.

`OneHopQueryPlan` version 2 has exactly: schema version, allow-listed template ID, canonical subject ID, canonical Predicate ID, expected object type, and maximum rows. It contains no answer-policy timeout or caller-supplied Cypher/procedure input.

Canonical lookup, fixed one-hop execution, current-Claim revalidation, phrasing, and evidence packaging consume graph-row, evidence, output, diagnostic, and working-memory allowances. They do not consume an elapsed-time allowance. Resolver and complete-turn duration are reported in `BudgetConsumption.elapsed_ns` and in reproducible conversation artifacts; duration never decides whether a valid relation result is retained.

Callers may cancel at cooperative boundaries through the transient Python callback.
That check cannot interrupt a driver call already blocked in `Cursor.execute`, so an
enabled Memgraph deployment also requires its approved server-side query-execution
timeout. Cancellation and the backend timeout are operational bounds; neither is a
field of `OneHopQueryPlan` nor an answer-quality threshold. See the
[deployment runbook](../operations/deployment-and-rollback-v1.md).

The configured live MemGraph path resolves Sarah and `married_to`, returns the Abraham Claim through the allow-listed query, revalidates it, and completes as `EVIDENCE`. Ambiguous identity, multiple eligible results, object-type mismatch, or current ineligibility still prevents a phrase because those are semantic or disclosure conditions, not latency policy.
