# Deterministic utility plugin contract v1

**Status:** Section 14 component contract  
**Date:** 2026-08-22  
**Release authority:** Section 16; every plugin remains disabled by default

## Boundary and registry

The utility resolver accepts only the seven compiled-in names below. Configuration
selects names from that closed tuple; it cannot supply a module, entry point,
callable, expression, or import path. Runtime code contains no `eval`, `exec`,
`compile`, `__import__`, subprocess, shell, network, file, or graph operation.
Unknown and duplicate names fail configuration. One malformed operation fails or
rejects only the utility resolver invocation; the shared executor continues to
later resolvers.

Each plugin publishes the same contract fields through
`utility_plugin_contracts()`: contract name/version, accepted frame type, input
schema, hard bounds, deterministic result format, evidence policy, stable errors,
and health rule. `UtilityRegistry` is stateful only because it owns the configured
allow-list. The operation implementations are ordinary pure functions.

The resolver reads `QueryFrame.original_text`; retrieval rewrites, inherited
identity fields, and ambient conversation state never become executable input.
Frames with required metadata or a required source label abstain because a
calculation cannot satisfy knowledge-source filters. The resolver emits at most
one active, scope-matched candidate and no accounting observation.

## Common result and authority rule

A successful evaluation has these ordinary-dictionary fields:

- `status`: `resolved`, `rejected`, `miss`, or `failed`;
- plugin name and semantic version;
- utility contract version;
- canonical input and deterministically formatted response;
- stable error code and operation count.

Only `resolved` creates a candidate. Its statement identity is a SHA-256 digest of
plugin name, plugin version, and canonical input. Provenance records the producer,
contract and plugin versions, canonical input, and `learnable: false`. It has no
Claim or accepted-response evidence because the result is locally computed, not
retrieved knowledge.

Fusion does not trust that provenance by assertion. `EngramCandidateAuthority`
re-runs the named built-in plugin against the original request and compares the
contract version, plugin version, canonical input, statement digest, and response.
Only an exact re-execution match receives deterministic answer authority. Graph
relation and composition candidates that already use `CandidateSource.UTILITY`
remain on their existing independent authority paths. Utility output is never
inserted into the response repository, legacy statement store, feedback
accounting, or persistence state.

## Hard common bounds

| Bound | Value |
| --- | ---: |
| Input | 4,096 UTF-8 bytes |
| Output | 2,048 UTF-8 bytes |
| Tokens | 128 |
| Operations | 32 |
| Parenthesis/unary nesting | 16 |
| Decimal literal digits | 128 |
| Decimal magnitude | adjusted exponent at most 100 |
| Integer power magnitude | 12 |
| Set items | 64 per input set |
| Set item | 64 UTF-8 bytes |

The existing resolver lease supplies the outer candidate, output, diagnostic, and
working-memory bounds. Limits are checked at the untrusted text/config boundary;
internal dictionaries are not repeatedly passed through schema factories.

## Plugin grammars and policies

| Plugin | Accepted grammar and deterministic policy |
| --- | --- |
| `arithmetic_v1` | `calculate` or `arithmetic`, decimal literals, `+ - * / % **`, unary signs, and parentheses. A recursive-descent parser—not Python—uses 34 significant Decimal digits. Division by zero, non-integral or oversized powers, excessive operations/nesting, and excessive magnitude reject. |
| `boolean_v1` | `boolean` plus `true`, `false`, `not`, `and`, `xor`, `or`, and parentheses. Precedence is `not`, `and`, `xor`, `or`; output is lowercase `true` or `false`. |
| `set_v1` | `set union`, `intersection`, `difference`, or `symmetric difference`, followed by two brace-delimited comma lists joined by `and` or `with`. Items match `[A-Za-z0-9_.:-]+`; duplicates collapse and output sorts by Unicode code point. |
| `date_time_v1` | ISO Gregorian date plus/minus days; signed day difference between two ISO dates; or conversion of an aware RFC3339 timestamp to `UTC`, `America/New_York`, `America/Los_Angeles`, `Europe/London`, or `Asia/Tokyo`. A source offset is mandatory, eliminating ambiguous/nonexistent local source times. Output is ISO 8601 with seconds and the target zone name. The packaged, locked `tzdata` version is reported in health. No current time or locale is read. |
| `unit_conversion_v1` | `convert <decimal> <unit> to <unit>` for length (`mm cm m km in ft yd mi`), mass (`g kg oz lb`), duration (`s min h`), or temperature (`C F K`). Cross-dimension conversion rejects. Factors are fixed Decimal constants; output uses 16 significant digits. |
| `version_v1` | `compare version <left> and|to|with <right>` using SemVer 2.0.0 precedence. Leading-zero and grammar violations reject; build metadata is retained in output but ignored for precedence. |
| `identifier_v1` | `validate uuid|slug <value>`. UUID accepts 32 hexadecimal digits or the canonical hyphenated form and renders lowercase hyphenated text. Slug accepts lowercase ASCII alphanumeric segments separated by one hyphen. Invalid bounded identifiers produce an explicit deterministic `invalid` result; they are not executed or normalized into another grammar. |

## Errors, health, and rollback

Recognized malformed input returns `rejected` with a plugin-owned stable error
such as `arithmetic_syntax`, `arithmetic_domain`, `operation_limit`,
`collection_limit`, `date_time_domain`, `timezone_not_allowed`,
`dimension_mismatch`, `version_syntax`, or `identifier_limit`. Text matching no
enabled grammar returns `miss`. An unexpected implementation exception is reduced
to `failed/plugin_failure`; exception text and input are not exposed.

`core.status().components.utility` reports overall enabled/ready state, contract
version, every built-in plugin's independent enabled/ready/version fields, and the
date plugin's timezone-database version. Disable `utility.enabled` to roll back all
utility resolution, or remove one name from `utility.plugins` to roll back only
that plugin. No data migration or index rebuild is involved.

