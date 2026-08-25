# Namespace rollout contract v1

**Status:** Qualified for staged rollout
**Owner:** Engram project
**Configuration:** `rollout.policy_version`, `rollout.default_mode`, and `rollout.namespaces`

An exact namespace override wins;
otherwise the default mode applies. The policy version and selected mode are part of
resolution retry identity. A policy or mode change creates a new retry identity.

| Mode | Resolver work | Public result | Accepted-success credit |
| --- | --- | --- | --- |
| `disabled` | None | `MISS` | None |
| `shadow` | Configured plan | Candidate/evidence content suppressed to `MISS`; aggregate shadow outcome/count diagnostics only | None |
| `evidence_only` | Configured plan | `ANSWER` becomes `EVIDENCE`; existing `EVIDENCE`/`MISS` remains its class | None |
| `regulated_direct_answer` | Configured plan | Existing unified-resolution behavior | Only eligible exact answers when the caller sets `accept_exact=true` |
| `rollback` | Exact resolver only | Exact `ANSWER` becomes `EVIDENCE`; otherwise `MISS` | None |

`EngramCore.status().rollout` exposes the version, default mode, total override
count, and fixed counts for all five modes aggregated across namespaces. Rollout
applies to unified `resolve_request`; deployment authentication, accepted artifacts,
and the proposal/Regulator workflow retain their existing owners. The tracked and
development configurations use `regulated_direct_answer` with an empty override set.

Focused tests cover configuration/YAML round-trip, exact namespace selection,
status cardinality, every mode, retry conflicts, candidate suppression, and absence
of accepted-hit credit in suppressed modes.
