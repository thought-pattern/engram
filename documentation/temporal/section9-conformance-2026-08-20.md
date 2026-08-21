# Section 9 component conformance — 2026-08-20

**Status:** Component pass; not release qualified

Section 9 implements temporal interpretation, shared bitemporal Claim eligibility, supplied-trust comparison, Predicate cardinality, conflict classification, and arbitrary-direct-answer suppression.

| Requirement | Evidence |
| --- | --- |
| Temporal qualifiers and conservative parsing | `engram/temporal.py`, `tests/test_temporal.py` |
| Valid/system half-open filtering and unchanged disclosure gate | `engram/evidence.py`, `tests/test_claim_eligibility.py` |
| Current, historical, bounded, and latest selection | `engram/relation.py`, `tests/test_temporal_conflict.py` |
| Visibility and graph-supplied trust | Claim eligibility and temporal-conflict tests; no trust threshold/default exists |
| Cardinality and incompatible-object detection | Optional `Predicate.cardinality`, stable selection reasons, preserved conflict Claim IDs |
| Direct-answer suppression | Resolver integration tests verify no candidate for conflict, unknown cardinality, missing trust, ties, or open historical bounds |
| Adversarial engineering safety cases | [Versioned regression corpus](../../eval/section9-temporal-conflict-v1.json) and [source-bound result](benchmark-2026-08-20.json) |
| Unit-test value | [Per-test audit](unit-test-value-audit-2026-08-20.md) |
| MemGraph-enabled conversation | [1,000-turn MCP artifact](section9-mcp-sarah-sushi-memgraph-enabled-1000-turns-2026-08-20.json) |

The corpus passes all 12 repository-visible engineering cases. Its legacy
`held_out` key does not represent independent custody and cannot support a release
decision. The report includes per-case and aggregate durations plus governed source
metadata; timing does not affect component conformance.

At this report's original capture, the focused Section 9 suite passed 125 tests and
the complete repository passed 1,476 tests. Current repository verification is
recorded separately after remediation.

The MCP artifact records 1,000 completed and evaluated turns as Sarah, including repeated sushi statements and recall checks. A test-only seed omits the ordinary catch-all so five evenly spaced `Who is Sarah married to?` prompts must reach the configured graph. MemGraph reports enabled and ready, all five probes return source `graph`, the other 995 turns return source `pattern`, and every turn passes its 16 protocol checks plus one applicable profile check. Observed turn lengths are p50 2.3665 ms, p95 4.8499 ms, and maximum 3,799.6718 ms; no duration changes the result.
