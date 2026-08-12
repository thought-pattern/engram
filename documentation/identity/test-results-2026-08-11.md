# Section 1 verification results

Initial date: 2026-08-11  
Remediation verification: 2026-08-12
Python: 3.12.2
pytest: 8.1.1
Scope: EGR-101 through EGR-112

## Focused identity suite

Command:

```powershell
python -m pytest -q tests/test_identity.py
```

Result:

```text
collected 103 items
103 passed in 0.66s
```

## Full repository suite

Command:

```powershell
python -m pytest -q
```

Result:

```text
collected 928 items
927 passed, 1 xfailed in 47.49s
```

The remaining strict expected failure is the Section 0 characterization of the legacy `learn_from_response` equal-keyword replacement path. Section 1 deliberately supplies pure identity and retrieval-key contracts; Sections 2 and 3 own index integration and commit-time replacement prevention. The expected failure remains executable until that integration turns it green.

## Static, formatting, security, and artifact checks

```text
python -m ruff check engram scripts tests eval
All checks passed!

python -m ruff format --check <14 remediation Python files>
14 files already formatted

python -m isort --check-only <14 remediation Python files>
exit 0

python -m bandit -q engram/identity.py engram/lexical.py
exit 0; no findings

python -m bandit -q -lll engram/identity.py engram/lexical.py engram/core.py engram/service.py engram/config.py engram/persistence.py
exit 0; no high-severity findings

normalization fixture validation
identity-fixture-ok
```

The full suite includes the production AST checks that reject optional union annotations and non-procedural `None` literals. It also includes import-cycle, legacy lexical retrieval, NLP, graph, MCP, gRPC, persistence, generated-stub, strict falsey-boundary, and enabled-component preflight coverage. The installed Black 24.3.0 targeted check again exceeded three minutes without output, matching the Section 0 tool-drift finding; Ruff formatting and isort checks pass on every remediation file.

Golden fixture: `normalization-v1.json`

SHA-256: `da88f88fc2c3069e3d8a550c497187f86e225ca50ce279208d4323abc39a36ac`

Remediation benchmark: `remediation-benchmark-2026-08-11.json`  
SHA-256: `3e2d28fb6b2d601bba0e194290f8f3feb08796b62740bc65839db8eff66e3af6`

The offline benchmark ran 10,000 calls per operation over eight adversarial requests. Observed p95 latency was 0.0480 ms for normalization, 0.1198 ms for scoped-key construction, and 0.8895 ms for standalone identity construction. Building 1,000 identities peaked at 701,019 traced bytes.

## Requirement evidence

| Item | Evidence |
| --- | --- |
| EGR-101 | `ScopeKey`; deterministic order, strict dictionary/JSON codecs, UTF-8 and control-character tests |
| EGR-102 | `QueryOperator`, `EntityReference`, `RelationReference`, `IdentityQualifier`; closed vocabularies and malformed-component tests |
| EGR-103 | `QueryIdentity`; equality, validation, exact keys, unsupported-version errors, deterministic codec and concrete-absence tests |
| EGR-104 | `normalize_retrieval_key`; twelve golden fixtures, generated idempotence properties, explicit version rejection, Unicode prose-dash handling, comparison and technical-operator preservation |
| EGR-105 | `ScopedRetrievalKey`; immutable scoped builder, scope-separation tests, dictionary/JSON codec |
| EGR-106 | `RetrievalRepresentation` and `RetrievalKeyBinding`; normalized deduplication, provenance, bounds, maximum-payload codec test, no matcher/response fields |
| EGR-107 | `extract_operator` and `extract_qualifiers`; full operator vocabulary, prose-dash operators, negation/current/temporal/location/quantity, and six symbolic comparisons |
| EGR-108 | `extract_entities_and_identifiers`; names, versions, paths, error codes, language and identity-bearing symbols, empty canonical IDs, no graph lookup |
| EGR-109 | `extract_relation_surface`, `extract_lexical_terms`, and shared dependency-free `select_lexical_terms`; supported predicates, trailing version/time/context cases, and abstention tests |
| EGR-110 | `build_standalone_identity`; deterministic contrast tests and AST proof of no NLP model, graph, network, or transport imports |
| EGR-111 | `validate_authoritative_identity` and `load_authoritative_identity`; exact-value preservation, version/bounds/ID/null/malformed rejection |
| EGR-112 | Fixture-driven and generated contrast corpus, symbolic near-collisions, scope isolation, codec/idempotence properties, recursive concrete-output checks, boundary/preflight integration coverage, benchmark, and full regression suite |
