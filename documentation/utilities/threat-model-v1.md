# Utility resolver threat model v1

**Date:** 2026-08-22  
**Scope:** Section 14 built-in deterministic utility plugins

## Protected assets and trust boundaries

The protected assets are the host process, filesystem, network credentials,
authoritative Engram knowledge, request budgets, and deterministic result
semantics. Request text and external configuration are untrusted. The fixed
registry, pure operation functions, resolver adapter, and authority re-execution
are trusted project code. Utility results are ephemeral and cannot mutate the
protected knowledge stores.

## Threats and controls

| Threat | Control | Verification |
| --- | --- | --- |
| Arbitrary code, import, or expression execution | Fixed plugin table; dedicated tokenizers/parsers; no dynamic execution, subprocess, shell, file, network, graph, or dynamic plugin API | AST check plus injection probes and source review |
| Catastrophic or unbounded computation | Input, token, operation, nesting, power, magnitude, collection, output, and shared resolver-lease bounds | Per-plugin resource rejection and fuzz artifacts |
| Regex denial of service | Anchored/simple grammars and a 4,096-byte input ceiling | Randomized matched-prefix fuzz and latency gate |
| Numeric overflow, non-finite output, or unstable formatting | Decimal-only numeric path, finite/magnitude checks, fixed precision, canonical formatting | Arithmetic/unit properties and held-out cases |
| Timezone, calendar, locale, or current-time ambiguity | Explicit ISO inputs and numeric source offset; five target zones; locked packaged tzdata identity; Gregorian calendar; no locale/current time | Ambiguity rejections, leap-date and timezone held-out cases, health identity |
| Unit dimension confusion | Fixed unit metadata and same-dimension check before conversion | Cross-dimension negative cases and round-trip property |
| Version/identifier interpreted as code | Dedicated SemVer/UUID/slug grammars; values never leave pure string/numeric operations | Injection probes and invalid-grammar cases |
| Forged candidate provenance or response | Fusion authority re-executes the named built-in and checks all identity/result fields | Forged-response integration regression |
| Utility result learned as authoritative knowledge | No accounting observations, `learnable: false`, no repository/store/persistence writes | Core integration regression and contract audit |
| One plugin failure blocks resolution | Unexpected error becomes bounded `utility_plugin_failure`; shared executor continues its plan | Injected failure regression and existing executor fail-soft suite |
| Cross-scope/source-policy confusion | Candidate uses exact frame scope; source/metadata-constrained frames abstain | Resolver filter regression and authority checks |

## Explicit non-goals

The utility resolver is not a Python calculator, scripting engine, symbolic algebra
system, natural-language date interpreter, locale-aware formatter, arbitrary unit
ontology, general version library, or extensible third-party plugin host. Adding any
of those would materially expand the attack surface and requires a new contract and
independent gate.

The design also avoids blanket runtime schema validation inside trusted operations.
Validation occurs where untrusted configuration/text enters and where a candidate
crosses into authoritative fusion. Adding repeated dictionary validation or a
generic sandbox process would add complexity without protecting another boundary
in the current pure, hard-bounded implementation.

