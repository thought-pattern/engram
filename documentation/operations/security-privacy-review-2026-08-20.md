# Security and privacy review — 2026-08-20

**Status:** Engineering review complete; EGR-1507 and release approval remain open  
**Scope:** Current core, persistence, graph boundary, Python/MCP/gRPC adapters, and evaluation artifacts

## Effective controls

- Graph runtime operations are fixed or conservatively read-only, with caller
  procedures and mutation clauses refused. Deployment still requires a database
  account restricted to reads.
- Scope, lifecycle, temporal eligibility, disclosure, and publication revalidation
  fail closed. Scope metadata is not represented as an authentication system.
- Request, candidate, graph-row, vector-result, evidence, output, diagnostic, and
  working-memory dimensions have concrete bounds.
- Secrets are supplied by runtime configuration and are excluded from persisted
  cache state. Logs and metric labels must exclude raw text, responses, context
  fingerprints, customer Claim content, credentials, and unbounded identifiers.
- Enabled model and graph dependencies are eagerly preflighted. Serving paths do not
  download code or model/data artifacts.
- Mutation receipts and atomic checkpoint coordination distinguish live and durable
  outcomes, reducing unsafe replay after failure.
- Cooperative cancellation now propagates as `ResolutionCancelledError` without
  caching a false knowledge miss or partially publishing resolver output.
- Release-gate and final-test content is prohibited from the repository manifest;
  the visible examples formerly described as sealed were removed from release use.

## Tool review

The repository Bandit scan reports no medium- or high-severity findings and 18
low-severity findings. The low findings include empty password defaults,
non-cryptographic response selection, false-positive password-like strings,
fail-soft exception handling, and fixed-argument benchmark subprocess use; they are
retained for manual classification. The release owner must review the final scan
output rather than treating a severity-only scan as proof of safety.
Ruff, Pyright, the focused source-contract checks, and unit/integration tests cover
additional correctness boundaries but are not penetration tests.

## Open risks and required controls

| Risk | Current position | Required closure |
| --- | --- | --- |
| Authentication and per-operation authorization | Deployment-owned; scope is not authorization | Define identities and separate read, mutation, inspection, and administrative authority for every exposed adapter |
| Optional graph availability | Graph and graph-vector readiness are independent from core readiness | Report unavailable capabilities, skip their resolvers, and continue local serving without admitting a false negative cache entry |
| Optional graph execution isolation | Graph driver calls cannot be interrupted by cooperative cancellation once executing | Keep graph I/O outside the core-wide state lock in unified resolution and legacy chat, serialize same-user context and retry identity, and exercise supervisor termination during deployment chaos tests |
| Lower-level supplied read Cypher | Conservative blocklist exists but textual filtering is not a database permission boundary | Retain read-only DB credentials and prefer fixed capabilities for exposed resolution paths |
| Sensitive operational output | Low-cardinality/redaction policy is documented; full production telemetry is incomplete | Complete EGR-1509 cardinality review and exercise log/trace redaction |
| Adapter request limits and cancellation parity | Core bounds exist; future evidence gRPC and cross-adapter cancellation are incomplete | Complete EGR-1503, EGR-1504, and EGR-1506 contract tests |
| Persistence backups and downgrade | Feature codecs and recovery rules exist; operator orchestration is partly manual | Exercise the deployment runbook on representative state and retain the signed result |
| Evaluation leakage | Public examples were previously mislabeled sealed | Independent custodians must author protected content outside the repository and meet approved sample floors |

## Release decision

This review supports continued engineering and component testing. It does not close
EGR-1507 because adapter authorization, production telemetry/redaction, and external
deployment controls remain incomplete. It does not approve a release.
