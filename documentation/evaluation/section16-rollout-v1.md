# Section 16 namespace rollout contract v1

**Status:** Qualified for staged rollout
**Owner:** EGR-1608
**Configuration:** `rollout.policy_version`, `rollout.default_mode`, and `rollout.namespaces`

The rollout policy is intentionally small. An exact namespace override wins;
otherwise the default mode applies. The policy version and selected mode are part of
resolution retry identity. Changing either prevents a prior request ID from replaying
under different visibility semantics.

| Mode | Resolver work | Public result | Accepted-success credit |
| --- | --- | --- | --- |
| `disabled` | None | `MISS` | None |
| `shadow` | Configured plan | Candidate/evidence content suppressed to `MISS`; aggregate shadow outcome/count diagnostics only | None |
| `evidence_only` | Configured plan | `ANSWER` becomes `EVIDENCE`; existing `EVIDENCE`/`MISS` remains its class | None |
| `regulated_direct_answer` | Configured plan | Existing unified-resolution behavior | Only eligible exact answers when the caller sets `accept_exact=true` |
| `rollback` | Exact resolver only | Exact `ANSWER` becomes `EVIDENCE`; otherwise `MISS` | None |

`EngramCore.status().rollout` exposes the version, default mode, total override
count, and fixed counts for all five modes. It does not expose namespace names.
Rollout affects unified `resolve_request`; it does not replace deployment
authentication, mutate accepted artifacts, or alter the legacy proposal/Regulator
workflow. The tracked and development configurations preserve existing behavior with
`regulated_direct_answer` and no overrides.

Focused tests cover configuration/YAML round-trip, exact namespace selection,
status cardinality, every mode, retry conflicts, candidate suppression, and absence
of accepted-hit credit in non-direct modes.
