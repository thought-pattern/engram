# Section 15 operational telemetry v1

**Status:** Implemented engineering contract; not a release dashboard
**Owner:** EGR-1509
**Schema version:** 1

## Boundary

`EngramCore.operational_telemetry()` returns one JSON-ready, process-lifetime
aggregate. The same snapshot is nested under `core.status().telemetry`, so Python,
MCP inspection, and gRPC `GetStatus` observe the same owner. The aggregate is not
persisted, does not affect retrieval or answer policy, and resets when a new
`Engram` instance starts.

The implementation stores counters, totals, maxima, and fixed exclusive latency
buckets. It does not retain events, raw text, arbitrary reason strings, request or
artifact IDs, callers, namespaces, context fingerprints, Claim IDs, paths, model
names, or endpoints. Elapsed observations remain telemetry; no latency bucket is an
answer-quality or release threshold.

## Measurements

| Family | Measurements |
| --- | --- |
| Resolution | requests, new executions, exact replays, fixed `ANSWER`/`EVIDENCE`/`MISS` outcomes |
| Resolver contribution | invocation and fixed state counts, produced candidate/evidence counts, selected-candidate contribution, elapsed observations for eight built-ins plus `other` |
| Rejection | the five fixed Regulator outcomes; caller-supplied `reason` is discarded |
| Latency | observations, total nanoseconds, maximum nanoseconds, and exclusive `<=1 ms`, `<=10 ms`, `<=100 ms`, `<=1 s`, `<=10 s`, `>10 s` buckets |
| Budget/resource | total and maximum resolvers, candidates, graph rows, vector results, evidence count/bytes, output bytes, diagnostic bytes, and working-memory estimates; exhaustion counts use those fixed dimensions plus `other` |
| Rebuild | primary, sparse, and semantic attempts, applied successes, failures, dry runs, and elapsed aggregates |
| Durability | checkpoint attempts, successes, failures, recovered-after-state count, elapsed aggregates, and current durability state |
| Compatibility gauges | legacy cache query/hit/eviction counters and bounded regulated-cache counts |

A replay contributes one request/outcome and one replay, but does not duplicate the
original execution latency, resource use, or resolver work. A resolver name outside
the built-in set maps to `other`; an unknown exhaustion dimension also maps to
`other`. Error status records only an exception class name. These rules preserve the
diagnostic distinction without creating caller-controlled labels.

## Cardinality review

| Label family | Fixed values |
| --- | ---: |
| Resolution outcome | 3 |
| Resolver | 9 (8 built-ins plus `other`) |
| Resolver state | 5 |
| Regulator outcome | 5 |
| Resource exhaustion | 10 (9 declared dimensions plus `other`) |
| Rebuild kind | 3 |
| Latency bucket | 6 |

Every dictionary key is constructed from these constants. Counter values grow with
work, but no new series or key is created from a request. `tests/test_telemetry.py`
also injects custom resolver, reason, exhaustion, request, user, namespace, statement,
feedback, and persistence-error content and proves none appears in the snapshot.

## Operational use

- Inspect outcome and resolver-state changes alongside component readiness; an
  unavailable optional resolver must not be mistaken for a completed knowledge miss.
- Correlate budget exhaustion with the matching resource total/max before changing a
  configured bound.
- Review rebuild failures with the corresponding component readiness and consistency
  check. Rebuild counters do not replace those checks.
- Treat any checkpoint failure or current `degraded` durability as an incident signal;
  use the Section 3 recovery outcome rather than inventing a new request ID.
- Export the bounded snapshot through the deployment's existing metrics collector if
  required. Do not add caller-controlled labels during translation.

Alert thresholds and release gates depend on the deployment workload and remain
Section 16 decisions. This contract supplies measurements, not preselected numerical
policy.

## Verification

Four focused tests cover real resolution/replay/feedback aggregation, fixed unknown
buckets, rebuild success/failure/dry-run reporting, checkpoint recovery, snapshot
isolation, and content exclusion. The dependency-complete service, core, persistence,
feedback, resolver, sparse, semantic, gRPC, and MCP run passes 363 tests.
