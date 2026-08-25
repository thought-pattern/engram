# Section 16 evaluation foundation v1

**Status:** Project-owned release qualification implemented
**Owners:** EGR-1601, EGR-1602, EGR-1603, and EGR-1609
**Release authority:** Engram project owner

## Evaluation partitions

The versioned manifest at `eval/release-gate-foundation-v1.json` contains three
project-owned partitions in their execution order:

1. `tuning` is the only partition permitted to select policy parameters.
2. `release_gate` evaluates the frozen policy.
3. `final_test` executes only after the release gate passes and supplies the
   release decision.

Each partition contains one request for all 15 EGR-1602 workload families and
one contrast for all five EGR-1603 adversarial dimensions. Requests and complete
scoped contrast identities are disjoint across partitions. Every case records a
stable identifier, expected behavior class, and label provenance.

This is logical evaluation separation, not organizational separation. Engram is
a closed, low-risk system, so the project owns the data, execution, numerical
approval, and release decision. A separate custodian, external service, hidden
partition, or independent release organization is not required.

## Qualification

`eval/run_release_gate_foundation.py` performs the complete qualification in one
command. It:

- validates partition completeness, provenance, disjointness, and approved gates;
- executes every adversarial identity contrast;
- runs three deterministic quality trials for each partition;
- executes the final-test trials only after the release gate passes;
- measures direct answers, false direct answers, useful evidence, proposal
  acceptance, typed rejections, later correction, and abstention quality;
- incorporates current source-bound performance, semantic, contextual, temporal,
  composition, Claim evidence-package, chaos, engineering, and live-MCP evidence;
- applies the approved false-answer, usefulness, memory, durability, and evidence
  size gates; and
- binds the source, configuration template, numerical gates, partition results,
  and supporting artifacts into the release decision.

Turn lengths and startup are reported as p50, p95, p99, and maximum observations.
They remain descriptive and do not change answer correctness or release outcome.
Avoided Tapestry work uses the manifest's explicit planning counterfactual because
production Tapestry telemetry is outside this repository; it is never presented as
observed production behavior.

## Approved numerical gates

The project owner approved the following first-release gates before release-gate
execution:

| Measure | Gate |
| --- | --- |
| False direct answers | 0%, at least 15 evaluated outcomes per release partition, and no regression from its baseline |
| Useful evidence | At least 60%, at least 3 eligible observations per release partition, and no regression from its baseline |
| Peak memory | At most 768 MiB and at most 1.25x the recorded pre-feature baseline |
| Lost completed mutations | Zero and no regression |
| Evidence package size | At most 64 KiB and no regression |
| Turn length and startup | Descriptive p50, p95, p99, and maximum only |

The 15- and 3-observation floors match the closed first-release workload rather
than inventing production-scale custody requirements. The separate required
1,000-turn MCP run supplies a broad adapter, conversation-state, profile, and live
MemGraph integration evaluation.

## Reproducibility

After refreshing the supporting source-bound artifacts, run:

```bash
python eval/run_section16_engineering.py
python eval/run_release_gate_foundation.py
```

The result at `documentation/evaluation/foundation-2026-08-19.json` records the
base revision, dirty paths, governed-source SHA-256, configuration SHA-256, three
partition results, every gate check, evidence artifact digests, and the rollout
decision. The command exits successfully only when the release is approved.
