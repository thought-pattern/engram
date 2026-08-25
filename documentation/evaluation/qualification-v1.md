# Project qualification contract v1

**Status:** Current qualified contract
**Owner:** Engram project
**Scope:** Closed, low-risk release qualification

The Engram project owns the evaluation data, execution, numerical approval, and
release decision.

The qualification preserves the safeguards that affect measurement validity:

- tuning, release-gate, and final-test requests are disjoint and versioned;
- only tuning data may select parameters;
- numerical gates are frozen and approved before release-gate execution;
- release-gate and final-test quality probes each run three trials;
- the final test executes after a passing release gate;
- every result binds the governed source and configuration template;
- supporting engineering and live-MCP artifacts must be current and passing; and
- the final decision binds every artifact and partition-result digest.

A partition is invalidated if it is reused for parameter selection contrary to its
declared role or if its requests overlap another partition.

The single completion command is:

```bash
python eval/run_release_gate_foundation.py
```

It executes tuning measurement, the release gate, and—only after a passing
gate—the final test. It emits the complete machine-readable qualification result
in one run.

Passing qualification approves the candidate for the documented namespace rollout.
Activation uses the runtime configuration described by the rollout contract.

## Approved gates and observations

| Measure | Gate |
| --- | --- |
| False direct answers | 0%, at least 15 evaluated outcomes per release partition, and rate at or below baseline |
| Useful evidence | At least 60%, at least 3 eligible observations per release partition, and rate at or above baseline |
| Peak memory | At most 768 MiB and at most 1.25x the recorded baseline |
| Lost completed mutations | Zero |
| Evidence package size | At most 64 KiB and at or below baseline |
| Turn length and startup | Descriptive p50, p95, p99, and maximum only |

The observation floors match the closed first-release workload. A separate 1,000-turn
MCP run evaluates the adapter, conversation state, profile behavior, and configured
live MemGraph path.

## Reproducibility

`eval/release-gate-foundation-v1.json` defines the versioned partitions, cases,
provenance, and approved gates. `eval/run_release_gate_foundation.py` validates and
executes them in order.
