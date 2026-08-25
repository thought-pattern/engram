# Section 14 utility resolver conformance — 2026-08-22

## Outcome

Section 14 is component-complete. Seven fixed built-in plugins implement bounded
arithmetic, Boolean logic, sets, date/time, unit conversion, SemVer comparison,
and identifier validation. Every plugin independently passed conformance,
held-out, determinism, property, resource-rejection, fuzz, latency, security, and
unified-resolution integration checks. All seven are promoted for opt-in component
use and remain disabled by default; Section 16 owns project rollout authority.

## Task evidence

| Task | Evidence |
| --- | --- |
| EGR-1401 | The [version-1 plugin contract](contracts-v1.md) defines name/version, direct-frame input, grammar, hard bounds, canonical result, no-Claim evidence policy, stable errors, and health. The same fields are executable through `utility_plugin_contracts()`. |
| EGR-1402 | `UtilityRegistry` selects only seven compiled-in functions. The [threat model](threat-model-v1.md), AST check, injection probes, and failure-containment regression demonstrate that imports/expressions cannot be supplied or executed and one utility failure does not escape the shared resolver boundary. |
| EGR-1403 | `arithmetic_v1`, `boolean_v1`, and `set_v1` publish Decimal domains/precision/magnitude/power rules, Boolean precedence, set grammar/canonical ordering, and hard token/operation/collection limits. |
| EGR-1404 | `date_time_v1` requires Gregorian ISO dates or aware RFC3339 timestamps, rejects offset ambiguity, allow-lists five target zones loaded from exactly pinned packaged tzdata, reports that identity, ignores the system timezone database/locale/current time, and preserves distinct fractional instants. `unit_conversion_v1` fixes dimensions, factors, and 16-digit Decimal output. |
| EGR-1405 | `version_v1` implements only ASCII SemVer 2.0.0 precedence and rejects Unicode digits; `identifier_v1` implements only UUID/slug grammars. Neither value enters an import, evaluator, file, shell, subprocess, or network operation. |
| EGR-1406 | The [repository-visible corpus](../../eval/section14-utilities-v1.json), [summary benchmark](benchmark-2026-08-22.json), and seven adjacent per-plugin JSON artifacts record independent conformance/held-out accuracy, deterministic replay, properties, fuzz, resource rejection, latency, threat probes, source state, and promotion/default decisions. |

## Independent gate results

The corpus has four conformance and four held-out cases per plugin, including
positive and adversarial results. The benchmark adds a plugin-specific resource exhaustion probe, one code-shaped threat input with an exact expected safe disposition,
one algebraic/domain property, and 500 seeded matched-prefix fuzz cases per plugin
(3,500 total) with deterministic result-shape and canonical-replay oracles. Every
corpus and property check passed; every resource probe rejected with its declared
error; every threat probe matched its exact disposition; and no fuzz case caused
an unexpected plugin failure, contract/replay failure, or oversized output.

| Plugin | Conformance | Held-out | Determinism | Fuzz failure | Observed p95 |
| --- | ---: | ---: | ---: | ---: | ---: |
| `arithmetic_v1` | 100% | 100% | 100% | 0% | 0.8605 ms |
| `boolean_v1` | 100% | 100% | 100% | 0% | 0.5842 ms |
| `set_v1` | 100% | 100% | 100% | 0% | 0.5853 ms |
| `date_time_v1` | 100% | 100% | 100% | 0% | 8.2615 ms |
| `unit_conversion_v1` | 100% | 100% | 100% | 0% | 0.9552 ms |
| `version_v1` | 100% | 100% | 100% | 0% | 0.3952 ms |
| `identifier_v1` | 100% | 100% | 100% | 0% | 0.3431 ms |

Latency is descriptive correctness evidence, not an answer-quality cutoff. The
gate's generous 250 ms ceiling covers first-use timezone database loading; hard
computational safety comes from grammar and work bounds rather than elapsed-time
guessing.

## Enablement and rollback decision

All plugins are independently promoted for opt-in component use. The built-in
default remains `utility.enabled: false`. Operators can enable a subset by listing
only reviewed names. Removing one name rolls back one plugin; disabling the flag
rolls back the resolver. Neither action changes accepted-response artifacts,
statements, feedback, indexes, graph data, or persisted state.

## Comprehensive review and verification

The initial review found and remediated two Section 14 defects:

1. Python's permissive `datetime.fromisoformat` accepted compact timestamp forms
   outside the declared RFC3339 grammar. A dedicated bounded RFC3339 check now runs
   before conversion, with a regression for compact-form rejection.
2. Resolver readiness built the complete nested health report for every plan. A
   constant-time registry readiness operation now serves planning while the detailed
   report remains available only to health inspection.

The follow-up review also corrected fractional timestamp truncation, Unicode
digit acceptance in SemVer, host-timezone fallback, and weak fuzz/threat oracles.
The project tracker records each defect and its completed bounded remediation.

No excessively defensive validation layer was added. Config/text validation occurs
at its external boundary; fusion performs the one security-relevant re-execution;
trusted internal result dictionaries are constructed directly.

- Focused utility suite: **45 passed**.
- Complete repository suite: **1,616 passed** in 99.50 seconds.
- Pyright: **0 errors, 0 warnings, 0 information messages**.
- Ruff lint, changed-surface Ruff formatting, and isort: passed.
- Vulture found no project-owned dead code with generated protobuf/gRPC output
  excluded; compileall passed.
- Bandit found no high-severity issue at medium-or-higher confidence.
- The source contract confirms no production `TypedDict`, `frozenset`, optional
  union annotation, or function-local import.
- A direct full-tree Black run completed successfully: four files were
  reformatted and 137 were unchanged. A later `python -m black` wrapper check
  stalled without output and was stopped; no wrapper result is claimed.
- `pip check` reports an unrelated host-global `fastapi 0.110.0` / `starlette 1.6.0`
  mismatch. Neither package is declared or imported by this project, so this is an
  environment issue rather than a Section 14 dependency defect.

## Official MCP and live MemGraph

The [final MCP artifact](section14-mcp-sarah-preferences-memgraph-enabled-1000-turns-2026-08-22.json)
uses the official MCP client against the repository server and an ephemeral config
derived from the configured live MemGraph endpoint. The pinned semantic model was
loaded from the existing ignored `data/artifacts/models/` directory; no model was
downloaded. Health reported graph, semantic, reranker, and all seven utility
plugins ready under `utility-plugin-v1`.

Sarah declared that she likes sushi and cats and dislikes dogs. All **1,000 turns
completed**, all **1,000 were evaluated**, and all **1,000 passed**, with zero
failed checks. The run made 1,003 MCP calls and produced 995 pattern replies plus
five live-MemGraph replies at turns 200, 400, 600, 800, and 1,000. Observed turn
p95 was 3.8023 ms and total duration was 25.820621 seconds; elapsed values are
descriptive and do not affect knowledge eligibility.

The regenerated benchmark and each regenerated per-plugin artifact bind to
governed source SHA-256
`f7dde4844763c227a279a4fab3b24dea3d465c066816957846ba58f4e1743193`.
The earlier MCP artifact remains historical evidence bound to its recorded
pre-remediation digest
`f18bd0e7c8a7bac7bf3d23f8711e44b46f44cd36ccb08d6cb6ce8ed7966c4b25`;
it is not represented as source-identical to the regenerated benchmark.
