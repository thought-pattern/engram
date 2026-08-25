# Section 16 project qualification contract v1

**Status:** Implemented
**Owner:** Engram project
**Scope:** Closed, low-risk release qualification

The Section 16 qualification uses project-owned data and one project release
authority. “Independent” in resolver and component contracts describes unrelated
signals or controls; it does not impose organizational separation on evaluation.

The qualification preserves the safeguards that affect measurement validity:

- tuning, release-gate, and final-test requests are disjoint and versioned;
- only tuning data may select parameters;
- numerical gates are frozen and approved before release-gate execution;
- release-gate and final-test quality probes each run three trials;
- the final test does not execute after a failed release gate;
- every result binds the governed source and configuration template;
- supporting engineering and live-MCP artifacts must be current and passing; and
- the final decision binds every artifact and partition-result digest.

The manifest and results remain visible in the repository. Visibility does not
invalidate a partition merely because the system is project-owned. A partition is
invalidated only if it is reused for parameter selection contrary to its declared
role or if its requests overlap another partition.

The single completion command is:

```bash
python eval/run_release_gate_foundation.py
```

It executes tuning measurement, the release gate, and—only after a passing
gate—the final test. It emits the complete machine-readable qualification result
without requiring custody envelopes, external files, a second execution service,
or a separate release owner.

Passing qualification approves the candidate for the documented namespace rollout.
Activation is the ordinary runtime configuration choice described by the rollout
contract; the qualifier performs no separate system action.
