// ENGRAM Knowledge Graph Schema (sample)
// MemGraph/Cypher DDL for the recall-only graph layer.
// Apply via:
//   python scripts/setup_schema.py
// Or with mgconsole:
//   mgconsole < schema.cypher
//
// Schema version: 3.4 (aligned with the Tapestry knowledge graph)
// Last updated: 2026-08-01
//
// 3.4 adds only Tapestry-owned reasoning-projection metadata outside Engram's
// recall subset; the Claim validity and vector-recall contract is unchanged.
// 3.3 retains the calibrated source-trust projection on Claim records. Proof
// review coordination and audit dispositions are intentionally absent because
// ENGRAM is a read-only recall client and the active-Claim filter is its only
// lifecycle concern.
//
// ENGRAM is a fast-recall cache: it recalls settled conclusions so proven work
// is reused, and it has read-only access to Memgraph. This file is the subset
// of the Tapestry canonical schema that ENGRAM reads. The canonical store is a
// superset (Passage, Document, Event, Proof, Source, Inquiry nodes and the
// vector indexes for ANN); ENGRAM does not own or create those indexes. When
// vector recall is enabled it reads the externally managed Claim-premise index.
// Running this schema standalone gives a graph ENGRAM's recall queries and the
// <triple_add> / <triple_query> template operations can use directly.
//
// Canonical-first model: a Claim is a node that links by edge to canonical
// Entity and Predicate nodes. The structural relationship of a Claim to its
// subject, predicate, and object is carried by edges, never by string matching:
//
//     (:Claim)-[:HAS_SUBJECT  {surface_form}]->(:Entity)
//     (:Claim)-[:USES_PREDICATE]->(:Predicate)
//     (:Claim)-[:HAS_OBJECT   {surface_form}]->(:Entity)
//
// The subject / predicate / object string properties are retained on the Claim
// node as a denormalized rendering projection so a reader sees the surface
// triple without joining three edges. They are NEVER matched on — every lookup
// resolves through the canonical edges above (matching a surface slot is the
// alias-miss the canonical model exists to prevent), so no index is created on
// them.

// =============================================================================
// INDEXES
// =============================================================================
// MemGraph creates indexes asynchronously; these are idempotent.

// Claim indexes (the primary recall unit). ENGRAM filters every hot-path lookup
// on `invalidated_at IS NULL`, so that index is the one that matters most.
CREATE INDEX ON :Claim(id);
CREATE INDEX ON :Claim(normalized);
CREATE INDEX ON :Claim(semantic_fingerprint);
CREATE INDEX ON :Claim(trust_category);
CREATE INDEX ON :Claim(ownership_category);
CREATE INDEX ON :Claim(created_at);
CREATE INDEX ON :Claim(invalidated_at);
CREATE INDEX ON :Claim(system_from);
CREATE INDEX ON :Claim(system_to);
CREATE INDEX ON :Claim(valid_from);
CREATE INDEX ON :Claim(valid_to);
CREATE INDEX ON :Claim(established_at);
CREATE INDEX ON :Claim(source_report_date);
CREATE INDEX ON :Claim(next_review_at);

// Entity indexes (canonical identity for subjects/objects). ENGRAM resolves an
// extracted surface form to a node by primary_label or aliases.
CREATE INDEX ON :Entity(canonical_id);
CREATE INDEX ON :Entity(identity_key);
CREATE INDEX ON :Entity(primary_label);

// Predicate indexes (canonical relation vocabulary). ENGRAM resolves a relation
// surface form to a node by label or synonyms.
CREATE INDEX ON :Predicate(canonical_id);
CREATE INDEX ON :Predicate(label);

// =============================================================================
// NODE TYPE DOCUMENTATION
// =============================================================================
// MemGraph doesn't enforce schemas, but these are the expected node structures
// for the subset ENGRAM reads:
//
// :Claim {
//     id: String (UUID),
//     claim_type: String ('factual', 'behavioral', 'relational', 'temporal', 'attributed'),
//     normalized: String (denormalized text representation, retained for
//                         full-text similarity; canonical matching is by the
//                         HAS_SUBJECT / USES_PREDICATE / HAS_OBJECT edges, not
//                         by this field),
//     semantic_fingerprint: String (SHA-256 of canonical semantic content;
//                                  provenance is excluded),
//     trust_category: String (knowledge-source/verifier trust tier),
//     source_calibrated_trust: Float (nullable, versioned source/person score),
//     source_trust_score_version: Integer (nullable),
//     ownership_category: String ('PUBLIC', 'COMPANY', or 'CUSTOMER'),
//     created_at: DateTime,
//     system_from: DateTime (inclusive transaction-time lower bound),
//     system_to: DateTime (nullable exclusive transaction-time upper bound),
//     valid_from: DateTime (nullable inclusive world-validity lower bound),
//     valid_to: DateTime (nullable exclusive world-validity upper bound),
//     established_at: DateTime,
//     source_report_date: DateTime (nullable),
//     next_review_at: DateTime,
//     predicate_canonical: Boolean (false for retrieval-only staging),
//     invalidated_at: DateTime (nullable, set when a claim is retired; every
//                               hot-path recall filters `invalidated_at IS NULL`),
//     -- Denormalized structural projection (never matched on):
//     subject: String (projection of the HAS_SUBJECT surface form),
//     predicate: String (projection of the USES_PREDICATE label),
//     object: String (projection of the HAS_OBJECT surface form)
// }
//
// :Entity {
//     canonical_id: String (stable identity across surface forms),
//     identity_key: String (stable alias-convergence key),
//     primary_label: String (preferred display name for the entity),
//     aliases: List[String] (all observed surface forms — e.g. 'Microsoft',
//                            'Microsoft Corporation', 'MSFT' all resolve to one
//                            Entity node),
//     entity_type: String (nullable; 'person', 'organization', 'place',
//                          'concept', ...),
//     created_at: DateTime
// }
//
// :Predicate {
//     canonical_id: String (canonical relation slug — e.g. 'founded',
//                           'located_in', 'capital_of'),
//     label: String (preferred display form),
//     synonyms: List[String] (surface forms that map to this canonical_id —
//                             e.g. 'establish', 'set up', 'start' all map to
//                             'founded'),
//     created_at: DateTime
// }

// =============================================================================
// RELATIONSHIP TYPE DOCUMENTATION
// =============================================================================
// Canonical structural slots (replace Claim.subject / .predicate / .object):
//   (:Claim)-[:HAS_SUBJECT {surface_form}]->(:Entity)
//   (:Claim)-[:HAS_OBJECT  {surface_form}]->(:Entity)
//   (:Claim)-[:USES_PREDICATE]->(:Predicate)
//
// Relationship properties:
//   [:HAS_SUBJECT]    { surface_form: String }
//   [:HAS_OBJECT]     { surface_form: String }
//   [:USES_PREDICATE] { }

// =============================================================================
// EXAMPLE QUERIES (for reference, not executed)
// =============================================================================

// -- Facts about an entity (graph_lookup): the entity as the claim subject.
// MATCH (c:Claim)-[hs:HAS_SUBJECT]->(e:Entity)
// WHERE (toLower(e.primary_label) = toLower($name)
//        OR toLower($name) IN [a IN e.aliases | toLower(a)]
//        OR toLower(hs.surface_form) = toLower($name))
//   AND c.invalidated_at IS NULL AND c.system_to IS NULL
// RETURN c.subject AS subject, c.predicate AS predicate, c.object AS object
// LIMIT 5;

// -- Triple query (subject + predicate known, object unknown).
// MATCH (c:Claim)-[hs:HAS_SUBJECT]->(s:Entity),
//       (c)-[:USES_PREDICATE]->(p:Predicate),
//       (c)-[ho:HAS_OBJECT]->(o:Entity)
// WHERE (toLower(s.primary_label) = toLower($subject)
//        OR toLower($subject) IN [a IN s.aliases | toLower(a)]
//        OR toLower(hs.surface_form) = toLower($subject))
//   AND (toLower(p.label) = toLower($predicate)
//        OR toLower($predicate) IN [y IN p.synonyms | toLower(y)])
//   AND c.invalidated_at IS NULL AND c.system_to IS NULL
// RETURN ho.surface_form AS result
// LIMIT 1;

// Engram never writes Claim/Entity/Predicate nodes. Identity admission,
// temporal versioning, and Claim lifecycle remain owned by the Knowledge
// Engine; Engram consumes only active, proof-canonical rows over Memgraph.
