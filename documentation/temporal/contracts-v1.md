# Temporal, Trust, and Conflict Contracts v1

Section 9 extends the Section 7 Claim evidence path and the Section 8 one-hop relation path. It does not create a less restrictive historical disclosure path.

## Temporal query frame

`TemporalQuery` is an exact dictionary carried by `QueryFrame` and `CompactQueryFrame`. It contains the operator, valid-time or system-time axis, preserved source text, normalized half-open request bounds, explicit bound availability, deterministic parse confidence, and resolution state. Temporal source tokens are not lexical identity terms.

The supported operators are `current`, `now`, `as_of`, `in_year`, `before`, `after`, `between`, and `latest`. Exact supported date/year grammar reports confidence `1.0`; unresolved temporal text is preserved and reports `0.0`. These values describe deterministic grammar recognition, not a fitted probability. Unresolved or conflicting qualifiers fail closed at Claim eligibility.

Years and dates normalize as follows:

| Operator | Normalized interpretation |
| --- | --- |
| `as_of X` | Point at the final microsecond of X's date or year |
| `in_year Y` | `[Y-01-01, (Y+1)-01-01)` |
| `before X` | `[-infinity, start(X))` |
| `after X` | `[end(X), +infinity)` |
| `between X and Y` | `[start(X), end(Y))` |
| `latest` | Greatest eligible lower bound not later than evaluation time |

## Shared temporal eligibility

`ClaimEligibilityEvaluator` remains the sole Claim disclosure gate. Every path still requires canonical subject, Predicate, and object identities, a proof-canonical non-generic Predicate, by-ID publication revalidation, and either the public rule or an exact trusted-scope visibility authorization.

- Unqualified, `current`, and `now` requests preserve the Section 7 behavior: the Claim must be active at evaluation time and both system and valid intervals use lower-inclusive, upper-exclusive containment.
- Historical valid-time requests use the requested point or interval while requiring the Claim to be active in the current system snapshot.
- Historical system-time requests use the Claim's system interval. `invalidated_at` is an effective system upper bound when it precedes or replaces `system_to`.
- `latest` uses `valid_from` on the valid-time axis and `system_from` on the system-time axis. Future lower bounds are not eligible.
- Open intervals may be retained as evidence when matching is possible, but explicit historical direct phrasing abstains when the relevant Claim interval is open.

`ClaimValidityInputs` schema version 2 exposes the temporal operator and axis, requested bounds, system bounds, invalidation boundary, valid bounds, evaluation time, actual evaluation-time `active`/`system_current`/`valid_time_current` state, and separate request-match fields. Historical matches therefore never claim to be current. Serialized enums are strings and absence remains concrete—never JSON `null`.

## Supplied trust

Engram consumes `source_calibrated_trust` and `source_trust_score_version` exactly as supplied by the canonical Claim. It does not calculate, upgrade, default, threshold, or calibrate these values.

A direct one-hop phrase requires supplied trust and its version. Multiple compatible Claims may be ranked only when every compared value is present and versions match. Different versions are incomparable. Contradictory objects are never declared true merely because one has a larger trust value; all supplied ranking inputs remain in Claim evidence and the stable ranking order is diagnostic.

## Cardinality and conflict

The optional canonical Predicate property `cardinality` accepts `SINGLE` or `MULTI`; absence decodes as `UNKNOWN`.

- `SINGLE`: overlapping eligible Claims with different canonical objects are a conflict. Every conflicting Claim ID is preserved and direct phrasing is suppressed.
- `MULTI`: different eligible objects are a valid multi-value result, not a contradiction. Version 1 returns bounded evidence rather than inventing a single-object phrase.
- `UNKNOWN`: multiple different objects fail conservatively to bounded evidence without claiming either contradiction or valid multiplicity.
- Different objects in non-overlapping periods of a bounded historical request are successive values, not a simultaneous conflict.
- A `latest` tie across different canonical objects suppresses direct phrasing regardless of trust.

Stable selection reasons are recorded in resolver diagnostics and each retained Claim's `selection_reasons`. Conflict output includes all conflicting Claim IDs; evidence records expose canonical SPO identity, temporal inputs, disclosure basis, trust value/version availability, and retrieval measurements.

## Graph execution and bounds

All Cypher remains internal, fixed, parameterized, and read-only. Current one-hop queries retain the active-Claim prefilter. Explicit historical/latest queries set the fixed `include_historical` Boolean parameter and rely on the shared evaluator and by-ID revalidation. One-hop discovery remains capped at ten rows and output/evidence/working-memory limits remain unchanged.

The versioned [engineering regression corpus](../../eval/section9-temporal-conflict-v1.json) is run by [benchmark_temporal_conflict.py](../../scripts/benchmark_temporal_conflict.py). Its legacy `held_out` split is visible repository material, not a Section 16 release partition. The report binds to governed source state and records each case duration plus aggregate turn lengths; there are no latency answer budgets or pass/fail timing gates.
