# Section 16 release evidence packet — 25 August 2026

**Decision:** APPROVED FOR STAGED ROLLOUT
**Rollout mode:** `regulated_direct_answer`
**Release authority:** Engram project owner

## Candidate and decision bindings

The qualification applies to governed source digest
`db62f0330841aa011252d379ac1addd1e6c56d729344ac27789fc0375acc1aac`.
The result binds configuration digest
`1ff6d36f27681c466a0e12b54ba886b65da7cb6a27d2a99d815b51b2f0007d12`,
approved numerical-gate digest
`23e6b9d462ab4b25e13ac7d32f977d63523e6ceeca3f9c009421ac49cd2d4a77`,
release-gate result
`0778b8f649dd71be7387effb51dab27331cda8156679bd54ba0974cb2abac724`,
and final-test result
`e1d1c3148b1d07724ce83638c09dcb8cadee8577dd5e069ae6a67dc1782c8d02`.
The final decision digest is
`3f253f480a942e2ab82afc15365d1d61c7f6c60d52a88d36f7b04f80a87e3848`.

The tracked `config.example.yml` preserves normal regulated-direct-answer
behavior and exposes the five namespace rollout modes. The live MCP artifact
records the exercised development configuration without credentials. Section 16
changes no persisted or wire schema, so no migration is required. The rollout
configuration is additive and defaults to the prior behavior.

## Evaluation design

The [project qualification contract](qualification-v1.md), [foundation](foundation-v1.md),
and [manifest](../../eval/release-gate-foundation-v1.json) define three project-owned,
versioned partitions: tuning, release gate, and final test. Their requests and
scoped adversarial identities are disjoint. Only tuning may select parameters;
the gates were frozen and approved before release-gate execution; and the final
test ran only after the release gate passed.

Each partition contains all 15 required workload families and all five required
adversarial contrasts. All 45 case labels and all 15 contrast identities passed
validation. Release-gate and final-test quality each ran three trials.

## Qualification results

The [machine-readable qualification result](foundation-2026-08-19.json) records
`release_ready: true`, `release_approved: true`, and
`approved_for_staged_rollout`. Both release partitions passed every gate:

| Measure | Release gate | Final test | Gate |
| --- | ---: | ---: | --- |
| False direct answers | 0/45 | 0/45 | 0%, minimum 15 |
| Useful evidence | 9/9 | 9/9 | At least 60%, minimum 3 |
| Correct abstention | 30/30 | 30/30 | Label conformance |
| Eligible direct answers accepted | 6/6 | 6/6 | Label conformance |
| Actual ten-record evidence package | 16,760 bytes | 16,760 bytes | At most 64 KiB and no regression |
| Peak memory | 456.47 MiB | 456.47 MiB | At most 768 MiB and 1.25x baseline |
| Lost completed mutations | 0 | 0 | Zero and no regression |

Release-gate turn-length p50/p95/p99/maximum observations were
16.3001/127.5188/131.5767/131.5767 ms. Final-test observations were
16.0054/126.1965/159.1883/159.1883 ms. Maximum measured startup was
40,704.5813 ms. These values are descriptive, not correctness or release gates.

## Evidence index

- [Engineering evaluation](section16-engineering-2026-08-23.json): all 15
  workload families, all seven quality categories, modeled Tapestry work avoided,
  component/resource performance, and source-currency checks.
- [Claim evidence-package benchmark](section16-evidence-2026-08-23.json): actual
  complete-package serialization, output accounting, truncation, order
  independence, and memory gates.
- [Chaos evaluation](section16-chaos-2026-08-23.json): all ten required failure
  scenarios and their asserted fallback behavior pass.
- [Performance](section16-performance-2026-08-23.json),
  [semantic](section16-semantic-2026-08-23.json),
  [contextual](section16-contextual-2026-08-23.json),
  [temporal](section16-temporal-2026-08-23.json), and
  [composition](section16-composition-2026-08-23.json) results are current,
  passing, and bound to the same governed source digest.
- [Official MCP conversation](section16-mcp-conversation-1000-turns-2026-08-23.json):
  1,000 requested, completed, and individually evaluated turns as Sarah, who likes
  sushi and cats and dislikes dogs. All turns pass; 1,003 MCP tool calls produce
  995 pattern responses and five responses from the configured live MemGraph.
  The graph connection is ready and disconnects cleanly after the run.
- [Rollout contract](section16-rollout-v1.md),
  [security/privacy review](../operations/security-privacy-review-2026-08-20.md),
  [deployment/rollback runbook](../operations/deployment-and-rollback-v1.md), and
  [rollback exercise](../operations/rollback-exercise-2026-08-20.md).

## Verification

- Complete repository suite: 1,671 passed.
- Section 16 and source-contract suite: 22 passed.
- Black 26.5.1 in write mode with cache disabled: 152 files unchanged.
- Isort, Ruff, bytecode compilation, Vulture, high-severity Bandit, and Pyright:
  clean; Pyright reports zero errors and warnings.
- Every supporting artifact records the same governed source digest and a passing
  result; every foundation evidence-currency check passes.

## Known limitations

- Avoided Actor calls, inference calls, tokens, Knowledge Engine calls, prompt
  evidence bytes, and latency are a declared planning counterfactual. They are not
  production Tapestry telemetry.
- Turn length and startup are environment-sensitive observations and deliberately
  do not change answer eligibility or release status.
- The transparent reranker remains unpromoted because its engineering evaluation
  shows no positive recall gain. This does not block the qualified components.
- MemGraph remains an optional, fail-soft capability. The live run proves the
  configured graph path, not permanent availability of an external service.
- The evidence binds a dirty working-tree content digest rather than claiming an
  unmodified Git commit. Reproduction therefore uses the recorded governed-source
  digest and changed-path inventory.

## Rollout decision

The source-bound candidate is approved for the documented staged rollout in
`regulated_direct_answer` mode. Engram applies that choice through its ordinary
runtime configuration; namespace staging, monitoring, and rollback follow the
rollout contract and operating runbook.
