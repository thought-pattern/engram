# Security and privacy review — 2026-08-20

**Status:** EGR-1507 engineering controls complete; release approval remains open
**Scope:** Current core, persistence, graph boundary, Python/MCP/gRPC adapters, and evaluation artifacts

## Effective controls

- Graph runtime operations are fixed or conservatively read-only, with caller
  procedures and mutation clauses refused. Deployment still requires a database
  account restricted to reads.
- Scope, lifecycle, temporal eligibility, disclosure, and publication revalidation
  fail closed. Scope metadata is not represented as an authentication system.
- Request, caller/request/artifact identifiers, namespace, context, source,
  candidate, graph-row, vector-result, evidence, output, diagnostic, and
  working-memory dimensions have concrete bounds. The shared service boundary
  applies them before transient state or mutations, so Python, MCP, and gRPC use
  the same enforcement.
- Secrets are supplied by runtime configuration and are excluded from persisted
  cache state. Graph/core/gRPC failure logs emit stable operation text and bounded
  exception class names, not exception messages or tracebacks that can contain raw
  requests, responses, context, Claim content, or credentials.
- Enabled model and graph dependencies are eagerly preflighted. Serving paths do not
  download code or model/data artifacts.
- Mutation receipts and atomic checkpoint coordination distinguish live and durable
  outcomes, reducing unsafe replay after failure.
- Cooperative cancellation now propagates as `ResolutionCancelledError` without
  caching a false knowledge miss or partially publishing resolver output.
- Release-gate and final-test content is prohibited from the repository manifest;
  the visible examples formerly described as sealed were removed from release use.

## Tool review

The 20 August repository Bandit scan reported no medium- or high-severity findings and 18
low-severity findings. The low findings include empty password defaults,
non-cryptographic response selection, false-positive password-like strings,
fail-soft exception handling, and fixed-argument benchmark subprocess use; they are
retained for manual classification. The release owner must review the final scan
output rather than treating a severity-only scan as proof of safety.
Ruff, Pyright, the focused source-contract checks, and unit/integration tests cover
additional correctness boundaries but are not penetration tests.

## Authority separation

Authentication is deliberately owned by the embedding application, MCP host, or
trusted gRPC proxy. The authenticated principal is that boundary's process,
connection, or account identity. `user_id`, namespace, context fingerprint, source
label, and metadata are conversation, eligibility, or provenance inputs and must
never be accepted as credentials.

| Authority | Operations | Enforcement point |
| --- | --- | --- |
| Observation | gRPC `GetStatus`, `GetPredicate`, and `InspectConversation`; bounded Python inspection/status calls | Embedder allow-list or gRPC method policy |
| Conversation and export | start, finish, stop, and report operations | Owning process/MCP host or gRPC method policy; report paths remain operator configured |
| State mutation and accounted resolution | chat/send, add fact, set predicate, propose, resolve, unified evidence resolution, learn, retire, feedback, flush, and repair | Explicit Python caller grant, MCP tool grant, or gRPC method policy |
| Administration | configuration, schema setup, migration, backup/restore, model provisioning, and deployment | Operator process outside the serving protocol |

Chat/send can learn facts; proposal and unified resolution can update context,
candidacy, and accounting. They therefore belong to mutation authority even when a
particular request makes no durable change. MCP remains a local stdio protocol with
ten frozen tools: its host grants tools to an already authenticated local client.
For gRPC, use an interceptor or trusted proxy policy keyed by the fully qualified
method. The service stays loopback-only unless that boundary also supplies encrypted
transport and authentication. Engram does not add a second token format or RBAC
database inside the low-risk single-instance core.

## Enforced input and output bounds

| Family | Bound |
| --- | ---: |
| Request text | 16,384 UTF-8 bytes |
| Request, artifact, caller, and support identifiers | 256 UTF-8 bytes each |
| Namespace / context fingerprint / source label | 128 / 512 / 256 UTF-8 bytes |
| Retrieval aliases | 32 values, 4,096 UTF-8 bytes each |
| Artifact metadata | 65,536 serialized UTF-8 bytes, depth 8, 1,024 items |
| Artifact support | 256 Claim IDs |
| Evidence package | 10 records and 65,536 serialized UTF-8 bytes |
| Resolution diagnostic budget | 16,384 bytes by default; validated maximum 1,048,576 |
| Rewrite corpus | 256 rules; 512-byte patterns; at most 8 applications per rule |
| Stored response / feedback reason | 1,048,576 / 512 UTF-8 bytes |

Strict identity, artifact, evidence, resolution, feedback, and rewrite factories
retain their deeper field and collection bounds. `tests/test_security_privacy.py`
proves service/MCP rejection occurs before state change and that graph failure text is
absent from logs and wrapped errors. The gRPC network suite proves the v2 request
bound maps to `INVALID_ARGUMENT` and unexpected failures expose only a generic
client error and exception class in logs.

## Open risks and required controls

| Risk | Current position | Required closure |
| --- | --- | --- |
| Authentication and per-operation authorization | Deployment-owned; the authority matrix above defines the principal and actual state effects | Enforce the matrix in the MCP host, embedding application, or gRPC interceptor/proxy before any remote deployment |
| Optional graph availability | Graph and graph-vector readiness are independent from core readiness | Report unavailable capabilities, skip their resolvers, and continue local serving without admitting a false negative cache entry |
| Optional graph execution isolation | Graph driver calls cannot be interrupted by cooperative cancellation once executing | Keep graph I/O outside the core-wide state lock in unified resolution and legacy chat, serialize same-user context and retry identity, and exercise supervisor termination during deployment chaos tests |
| Lower-level supplied read Cypher | Conservative blocklist exists but textual filtering is not a database permission boundary | Retain read-only DB credentials and prefer fixed capabilities for exposed resolution paths |
| Sensitive operational output | Failure logs and persistence client/status errors are redacted; schema-version 1 operational telemetry has fixed keys and tested content exclusion | Export only the reviewed fixed keys and set deployment-specific alert thresholds without adding caller-controlled labels |
| Adapter request limits and cancellation parity | Shared bounds and completed MCP/v1/v2/cancellation contract tests cover the current adapters | Re-run the adapter contract suite for each schema change |
| Persistence backups and downgrade | Explicit-output migration and the isolated rollback exercise preserve their sources | Retain deployment-specific backup evidence |
| Evaluation leakage | Engineering fixtures must not be reused to select policy and then counted as release cases | Keep the project-owned tuning, release-gate, and final-test requests disjoint and freeze policy before the release gate |

## Release decision

The implemented shared bounds, graph boundary, method-level authority separation,
tested log/error redaction, and fixed-cardinality operational aggregate close the
Section 15 engineering security and telemetry work. Authentication, grants,
collector translation, and alert thresholds remain deployment responsibilities.
This review supplies security evidence to the Section 16 project qualification; it does not make the release decision by itself.
