# Contextual relation contracts v1

**Status:** Historical contract, superseded by [contextual relation contracts v2](contracts-v2.md); its timeout field is not current behavior.

Section 8 adds context and canonical one-hop interpretation without changing the Section 4 base-frame schema or the Section 7 Claim/evidence codecs.

## Previous-frame state

Each user session may retain one `CompactQueryFrame` with exactly these fields: schema version, operator, subjects, relation, expected object type, qualifiers, source turn, confidence, and topic. It contains no response, raw graph row, Cypher, resolver output, or accepted knowledge. The session persistence codec validates the frame on save and load, requires its source turn to match the session frame counter, and treats only omitted legacy keys as absence; falsey values of the wrong type are rejected. Legacy sessions without the two keys decode to empty contextual state.

The state is keyed by normalized `user_id`, uses the existing session lock, persistence path, activity timestamp, maximum-session policy, and TTL expiration. Idempotent request replay does not advance the turn. The turn counter is bounded at 1,000,000 and resets the retained frame before overflow.

Follow-up enrichment permits a maximum turn distance of two and a minimum prior confidence of 0.55. It fills only missing operator, subjects, relation, expected type, or qualifier kinds. Every inherited field records its source turn. An explicit subject in a self-contained request prevents inheritance; a topic mismatch, expired session, low confidence, or excessive distance also prevents it.

Operator classification reuses the Section 1 `QueryOperator` enum and extractor. Context only supplies a prior operator when current classification is `UNKNOWN` and the request has bounded follow-up evidence. Expected-type inference is closed over `PERSON`, `PLACE`, `DATE`, `NUMBER`, `BOOLEAN`, `ENTITY`, and `UNKNOWN`. `UNKNOWN` is preserved when no safe inference exists.

## Canonical identity resolution

Canonical entity lookup uses one fixed parameterized graph query over `Entity.primary_label`, `Entity.aliases`, and `HAS_SUBJECT|HAS_OBJECT.surface_form`. Named-entity recognition contributes surface evidence but never creates a canonical ID. An exact caller-supplied canonical ID bypasses surface lookup. Results are `selected`, `ambiguous`, or `miss`; tied candidates within the conservative margin are explicitly ambiguous and cannot compile a plan.

Predicate lookup uses a second fixed parameterized graph query over canonical ID, primary label, and configured synonyms. Candidate surfaces come from an explicit caller Predicate identity, the base relation surface, dependency-backed verb lemmas and prepositions, and bounded lexical fallback. Expected object type may disambiguate compatible canonical Predicates, but `UNKNOWN` does not reject a candidate.

Graph-enabled startup preloads the full spaCy pipeline, including parser and NER. Request handling never downloads or first-loads the model.

## One-hop plan and execution

`OneHopQueryPlan` has exactly: schema version, allow-listed template ID, canonical subject ID, canonical Predicate ID, expected object type, maximum rows, and timeout nanoseconds. Version 1 supports only `one_hop_claim_v1`, at most ten rows, and at most 250 ms. There is no Cypher or procedure-name input.

The graph capability executes the fixed query with only `subject_entity_id`, `predicate_id`, and `limit` parameters. Its result is the existing strict Section 7 `ClaimProjection` plus a bounded object label and closed object type. The projection identifier `relation_one_hop_claim_projection_v1` follows the structured-projection measurement rules.

Every discovered Claim passes the Section 7 current disclosure evaluator and a by-ID canonical revalidation. That preserves active/system-current state, current valid-time interpretation, proof-canonical Predicate identity, retrieval-only exclusion, ownership visibility, and exact scope authorization. Discovery, canonical lookup, revalidation, evidence, output, memory, row, and cooperative deadline work all consume the Section 4 resolver lease.

## Phrasing and evidence

One eligible result with no explicit object-type mismatch may produce the deterministic candidate form `subject — predicate: object.` The candidate carries a single Claim evidence reference and the measured `entity_match`, `relation_match`, and, when available, `object_type_match` features.

Current Claim authority is checked again by the fusion authority before the candidate can participate. Section 5 policy remains unchanged for graph truth: the non-exact support and independent-source requirements prevent a lone graph phrase from becoming `ANSWER`. It may remain a useful response candidate in an `EVIDENCE` result.

Multiple eligible results, ambiguous entity or Predicate identity, object-type mismatch, ineligible current state, budget exhaustion, or timeout cannot produce a phrase. Eligible Claims are still encoded through the Section 7 `ClaimEvidenceRecord` and package with relation-plan, ambiguity, and object-type reasons. Arbitrary graph properties, proof text, embeddings, and Cypher are never exposed.

## API boundary

The transport-neutral Python `EngramCore.resolve_request` accepts an additive keyword-only `user_id`, defaulting to the legacy user identity. Section 15 still owns a stable public Python adapter and any future gRPC evidence/context schema. Existing CLI, MCP, and gRPC v1 response shapes remain unchanged.
