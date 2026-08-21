# Section 9 Unit-Test Value Audit — 2026-08-20

The Section 9 tests were reviewed for observable behavioral protection. Tests that merely freeze private helper structure, exact exception prose, arbitrary confidence thresholds, or duplicated constants are not retained.

| Test or parameter group | Decision | Distinct behavior protected |
| --- | --- | --- |
| Temporal parser operator table (8 cases) | Keep | Each required operator maps to its public operator and normalized request bounds. |
| System-axis parser case | Keep | System observation time cannot silently become valid time. |
| Unresolved/conflicting qualifier case | Keep | Source text survives and unsafe interpretations fail closed. |
| Bare-year follow-up | Keep | A common elliptical request becomes one bounded year. |
| Temporal contract codec and malformed bounds | Keep, loosened | Public round-trip and interval consistency matter; exact exception wording was removed. |
| Query-frame lexical separation and codec | Keep | Temporal tokens cannot contaminate standalone identity, and the public frame retains the interpretation. |
| Compact-frame temporal codec | Keep | Session persistence cannot silently discard the temporal qualifier. |
| Temporal follow-up replacement | Keep | A new year replaces the prior year while inheriting subject/relation only. |
| Elliptical temporal inheritance | Keep | A non-temporal follow-up retains the prior requested time with provenance. |
| Current Claim eligibility table and boundary case | Keep | Section 7 current behavior remains unchanged and half-open. |
| Historical valid-time swap | Keep | A past Claim is historical evidence and is not presented as current. |
| Historical range boundary case | Keep | Exact lower/upper equality follows half-open semantics. |
| Historical system-time lifecycle case | Keep | Knowledge visible before invalidation is excluded after its effective system end. |
| Unresolved temporal eligibility | Keep | Parser uncertainty reaches the disclosure gate and fails closed. |
| Historical visibility case | Keep | History cannot bypass the exact public-or-trusted-scope decision. |
| Future `latest` case | Keep | A future lower bound cannot win latest selection. |
| Unique/missing-trust selection | Keep | Direct phrasing requires graph-supplied trust without inventing a default. |
| Comparable trust ranking | Keep | Same-version supplied values may rank compatible evidence. |
| Trust-version mismatch | Keep | Values from different scoring versions are not compared. |
| Cardinality table (`SINGLE`, `MULTI`, `UNKNOWN`) | Keep | Contradiction, valid multiplicity, and conservative unknown behavior remain distinct. |
| Successive bounded values | Keep | Non-overlapping historical values are not mislabeled as simultaneous conflict. |
| Latest selection and distinct-object tie | Keep | Deterministic lower-bound selection works and ties suppress phrasing. |
| Missing/latest and open/history bounds | Keep | Incomparable or open bounds retain evidence and abstain. |
| System-time latest case | Keep | Latest uses the requested temporal axis rather than valid time unconditionally. |
| Resolver historical query/evidence case | Keep | The fixed graph capability requests history and exposes normalized bounds in evidence. |
| Resolver conflict suppression case | Keep, strengthened | No candidate is emitted; stable conflict reason and all Claim IDs are retained. |

The exact numeric parser-confidence assertions were changed from uncalibrated values (`0.98`, `0.99`, `0.2`) to deterministic grammar recognition (`1.0` exact, `0.0` unresolved). No test asserts a latency threshold. The versioned corpus intentionally repeats a small safety subset outside the unit suite as an engineering regression artifact. Its legacy `held_out` split is repository-visible and is not independently held release evidence; its timings are observations only.
