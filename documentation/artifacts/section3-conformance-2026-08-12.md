# Section 3 conformance report

Date: 2026-08-12  
Scope: EGR-301 through EGR-315  
Outcome: passed

## Exit assessment

Section 3 meets its exit condition. Authoritative callers can create and retrieve STATIC or DYNAMIC accepted responses, invalidate or retire them with audit and generation guards, and explicitly supersede them with preserved lineage. Artifact authority, compatibility views, derived indexes, namespace epochs, and durable receipts remain equivalent across the required concurrency, capacity, checkpoint, migration, and restart cases. Request-time eligibility excludes terminal, expired, epoch-stale, colliding, and dependency-unavailable artifacts from direct exact answers.

## Comprehensive evidence matrix

| Required case | Evidence |
| --- | --- |
| Artifact codec and exact text | `tests/test_artifacts.py`; deterministic Unicode codec, concrete bounds, scope and lifecycle invariants |
| Artifact/view/index equivalence | `tests/test_repository.py`, `tests/test_responses.py`, `tests/test_service.py`; checked candidate publication and authoritative accounting |
| Concurrent operations | Repository swap, epoch increment, receipt record, base-commit retry, terminal-transition, coordinator, and supersession single-winner tests |
| Restart and receipts | Real-file base commit, lifecycle, supersession, accounting, and v2 restart tests; exact replay performs no second checkpoint |
| Persistence failure | Definite, indeterminate-before, indeterminate-after, divergent, post-publication recovery, and recovery-failure injection in `tests/test_coordination.py` and `tests/test_responses.py` |
| Migration and quarantine | `tests/test_persistence_v2.py`; exact v1 preservation, idempotence, missing/malformed/ambiguous identity quarantine, strict v2 startup |
| Clock boundaries and epoch changes | `tests/test_eligibility.py`; injected UTC half-open bounds, unavailable dependencies, epoch policy, expiration and epoch refresh |
| DYNAMIC eviction and STATIC retention | `tests/test_repository.py` and `tests/test_responses.py`; policy victims, protected history, capacity rejection, synchronized effects |
| Exact and conflicting retries | `tests/test_mutations.py` and `tests/test_responses.py`; replay, conflicting payload/operation, retention tombstone, concurrent retry |
| Compatibility and adapters | `tests/test_service.py`, `tests/test_mcp_server.py`, and `tests/test_grpc_server.py`; DYNAMIC ACTIVE wrapper with no implicit supersession |
| Section 2 engineering gates | Fresh 100,000-projection benchmark and 5,000-artifact support proposal; every ADR 0004 engineering threshold passed |
| Long MCP conversation | Fresh official MCP client run completed 1,000 sequential `engram_send` turns and 1,003 total tool calls with complete inspect/stop counts |
| Recovery procedure | [Section 3 recovery runbook](section3-recovery-runbook.md) |

## Review and remediation

The final review found and corrected four issues before closure:

1. Internal proposal-query and accepted-hit receipt IDs could exceed the 256-byte receipt limit when a caller supplied a maximum-length request ID. They now use deterministic, bounded SHA-256-derived internal identities; the caller-controlled value is not copied into that key.
2. The MCP service docstring and integration guide still described implicit replacement. They now state the implemented base-commit collision behavior and explicit-supersession boundary.
3. Strict type analysis found unchecked object-to-mapping transitions in codecs, repository views, receipt metadata, persistence, and service receipt decoding, plus a potentially unbound accepted-resolution set. Production boundaries now validate and narrow these values; adversarial tests explicitly cast intentionally invalid inputs.
4. The MCP conformance harness initially violated the repository's concrete-absence AST policy. The optional metadata check now uses the project's concrete boundary. The first exploratory run completed 1,000 turns but failed while writing evidence after the client context closed; the harness lifecycle was fixed and a fresh independent 1,000-turn run produced the recorded passing artifact.

## Verification results

| Gate | Result |
| --- | --- |
| Focused Section 3, Section 2, service, MCP, and gRPC suite | 289 passed in 35.09s |
| Full repository suite | 1,171 passed in 70.02s; no expected failures |
| Ruff | all checks passed |
| Black formatter-of-record check | 24 Section 3 implementation and test files unchanged |
| Pyright 1.1.411 | 0 errors, 0 warnings, 0 informations |
| Vulture, confidence 65 | no findings |
| Bandit, high-severity gate over the complete Section 3 production surface | no findings |
| Python compileall | passed |
| MCP protocol conversation | 1,000/1,000 turns; passed |

## Engineering benchmark

The reproducible Section 2 harness was rerun after Section 3 integration. Exact lookup p95 was 0.006717 ms at 10,000 projections and 0.006554 ms at 100,000, a 0.9757x slope against the 1.5x limit. Full support-proposal p95 was 0.7611/1.3051/1.7207 ms at fan-out 1/10/100; each passed both the 30 ms absolute and baseline-relative gates. Rebuild p95 at 5,000 projections was 358.3101 ms against the 1,000 ms limit, and peak traced build memory was 3,981,121 bytes against 11 MiB.

- Benchmark: [benchmark-2026-08-12.json](../indexes/benchmark-2026-08-12.json)
- Benchmark SHA-256: `bed00dccc0b33bad4e48b965a2343d72cca175900c9828ca34598e9f353345a6`
- MCP evidence: [mcp-conversation-1000-turns-2026-08-12.json](mcp-conversation-1000-turns-2026-08-12.json)
- MCP evidence SHA-256: `2137fcaa5f534241af32be3f55eda9194ddb4fc416d6962d9bd85f8956b4e1cf`

The MCP run used the official `mcp.client.Client` against the repository's in-process `MCPServer`, called `engram_start`, issued 1,000 sequential `engram_send` calls, then called `engram_inspect` and `engram_stop`. It reported 1,000 responses, turn 1 through turn 1,000 in order, bounded session history size 10, and 1,000 exchanges in the stop summary. Synthetic prompts only are used, and the evidence stores no raw prompt or response body.
