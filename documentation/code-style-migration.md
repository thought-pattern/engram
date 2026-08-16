# Code-style migration

Status: In progress

The authoritative rules are in [code-style.md](code-style.md). This migration is complete only when the entire non-generated Python repository conforms and the full functional, static, formatting, dead-code, security, and style-specific verification gates pass.

## Baseline inventory

The 2026-08-16 AST inventory excludes compiler-generated files under `engram/v1/` and covers 105 Python files in `engram`, `scripts`, `eval`, and `tests`.

| Rule | Baseline evidence |
| --- | ---: |
| Dataclass records to replace with validated dictionaries | 98 |
| Module constants outside `engram/constants.py` | 351 |
| Return statements containing expressions rather than a prior name | 1,301 |
| `None` literals requiring classification and migration | 1,092 |
| Pipe-union expressions/annotations requiring classification | 21 |
| Classes requiring statefulness review | 381 |

The counts are discovery signals, not completion gates by themselves. The final audit must parse the repository and prove each rule directly, because a raw count can include permitted bare returns, enum classes, exception classes, test-only malformed external fixtures, bitwise operations, and compiler-owned files.

## Migration order

1. Identity, artifact, eligibility, and shared constant foundations.
2. Index, repository, mutation, response, and persistence contracts.
3. Resolution, evidence, fusion, resolver, and feedback contracts.
4. Coordination, service, adapters, scripts, evaluation runners, and tests.
5. Repository-wide return/import cleanup and final class-state audit.

Every phase must update consumers and tests in the same change, retain concrete absence at boundaries, and pass its focused tests before the next dependency layer is migrated.
