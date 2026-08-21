# Section 16 evaluation foundation v1

**Status:** In Progress  
**Owners:** EGR-1601, EGR-1602, EGR-1603, and EGR-1609  
**Release authority:** Pending independent label and numerical-gate approval

## Implemented foundation

The versioned manifest at `eval/release-gate-foundation-v1.json` establishes
three custody slots: tuning, release gate, and final test. The repository contains
only the open engineering tuning content. It covers all fifteen workload families;
each record has a stable identifier, expected behavior class, and explicit
provisional label provenance.

The previously committed release-gate and final-test examples were visible to
engineering and therefore were not sealed evidence. They have been removed and
must not be reused for a release decision. Independent custodians must author,
label, retain, and authorize those contents outside this repository. Until that
happens the two protected slots are explicitly `awaiting_independent_custodian`,
unprovisioned, and unauthorized—not sealed.

The tuning manifest declares `when`/`where`, current/historical,
positive/negative, relation, and exact-scope contrasts. The runner validates and
executes those public tuning contrasts and rejects any protected case or contrast
content committed to the repository.

Initial absolute and baseline-relative gates are recorded for false direct
answers, useful evidence, peak memory, durability, and evidence size. Turn
lengths are reported as p50, p95, p99, and maximum observations with no
pass/fail threshold. The numerical-gate status is
`proposed_pending_release_owner_approval`; they cannot be used to promote or
tune a feature until an independent release owner is recorded.

The tuning foundation contains 15 workload cases, below the proposed 1,000-case
false-answer and 200-case usefulness floors. The runner reports these readiness
gates as false. A valid foundation-integrity result therefore does not imply that
the system is release-ready or release-approved.

## Reproducibility

Run:

```bash
python eval/run_release_gate_foundation.py
```

The machine-readable result records the base Git commit, dirty state, changed
paths, and a SHA-256 digest over governed source, scripts, evaluation code,
tests, project metadata, dependency pins, and CI. This makes an uncommitted
evaluation worktree identifiable without claiming that its base commit alone
contains the changes.

## Remaining completion work

These EGR items remain In Progress because the foundation does not yet provide
independent custodians and labels, protected end-to-end inputs, approved numerical
gates, required sample sizes, repeated nondeterministic trials, or authorization to
run the release-gate and final-test partitions. Existing Section 8 through 10
corpora are engineering regression fixtures only and must not be promoted as
independent release evidence.
