// Engram recall schema
// Memgraph 3.9 Cypher DDL
//
// The definitions Engram needs from a knowledge graph. A graph is compatible
// when it has all of them; it may have more. The graph's owner creates them;
// Engram only reads the graph. To check a graph:
//   python3 scripts/verify_schema.py
//
// Record IDs and Entity/Predicate canonical IDs are opaque strings owned by
// the graph's writer (UUIDv7 in a Tapestry-managed graph). Existence,
// uniqueness and lookup indexes below are intentionally spelling-neutral:
// Engram reads those references as opaque text and never allocates or
// interprets them.

CREATE CONSTRAINT ON (n:SchemaRevision) ASSERT EXISTS (n.component);
CREATE CONSTRAINT ON (n:SchemaRevision) ASSERT n.component IS UNIQUE;
CREATE CONSTRAINT ON (n:GraphState) ASSERT EXISTS (n.name);
CREATE CONSTRAINT ON (n:GraphState) ASSERT n.name IS UNIQUE;
CREATE CONSTRAINT ON (n:Source) ASSERT EXISTS (n.id);
CREATE CONSTRAINT ON (n:Source) ASSERT n.id IS UNIQUE;
CREATE CONSTRAINT ON (n:SourceArtifact) ASSERT EXISTS (n.id);
CREATE CONSTRAINT ON (n:SourceArtifact) ASSERT n.id IS UNIQUE;
CREATE CONSTRAINT ON (n:Observation) ASSERT EXISTS (n.id);
CREATE CONSTRAINT ON (n:Observation) ASSERT n.id IS UNIQUE;
CREATE CONSTRAINT ON (n:Passage) ASSERT EXISTS (n.id);
CREATE CONSTRAINT ON (n:Passage) ASSERT n.id IS UNIQUE;
CREATE CONSTRAINT ON (n:ExtractionReceipt) ASSERT EXISTS (n.id);
CREATE CONSTRAINT ON (n:ExtractionReceipt) ASSERT n.id IS UNIQUE;
CREATE CONSTRAINT ON (n:AcquisitionReceipt) ASSERT EXISTS (n.id);
CREATE CONSTRAINT ON (n:AcquisitionReceipt) ASSERT n.id IS UNIQUE;
CREATE CONSTRAINT ON (n:Proposition) ASSERT EXISTS (n.id);
CREATE CONSTRAINT ON (n:Proposition) ASSERT n.id IS UNIQUE;
CREATE CONSTRAINT ON (n:Assertion) ASSERT EXISTS (n.id);
CREATE CONSTRAINT ON (n:Assertion) ASSERT n.id IS UNIQUE;
CREATE CONSTRAINT ON (n:Entity) ASSERT EXISTS (n.canonical_id);
CREATE CONSTRAINT ON (n:Entity) ASSERT n.canonical_id IS UNIQUE;
CREATE CONSTRAINT ON (n:Predicate) ASSERT EXISTS (n.canonical_id);
CREATE CONSTRAINT ON (n:Predicate) ASSERT n.canonical_id IS UNIQUE;
CREATE CONSTRAINT ON (n:SemanticDefinition) ASSERT EXISTS (n.id);
CREATE CONSTRAINT ON (n:SemanticDefinition) ASSERT n.id IS UNIQUE;
CREATE CONSTRAINT ON (n:SemanticBinding) ASSERT EXISTS (n.id);
CREATE CONSTRAINT ON (n:SemanticBinding) ASSERT n.id IS UNIQUE;

// Memgraph creates no implicit label index. Without one, a match on a label
// alone, or on a label with only unindexed properties, scans every vertex.
CREATE INDEX ON :AcquisitionReceipt;
CREATE INDEX ON :Assertion;
CREATE INDEX ON :Entity;
CREATE INDEX ON :ExtractionReceipt;
CREATE INDEX ON :GraphState;
CREATE INDEX ON :Observation;
CREATE INDEX ON :Passage;
CREATE INDEX ON :Predicate;
CREATE INDEX ON :Proposition;
CREATE INDEX ON :SchemaRevision;
CREATE INDEX ON :SemanticBinding;
CREATE INDEX ON :SemanticDefinition;
CREATE INDEX ON :Source;
CREATE INDEX ON :SourceArtifact;

CREATE INDEX ON :SchemaRevision(component);
CREATE INDEX ON :GraphState(name);
CREATE INDEX ON :GraphState(installation_state);
CREATE INDEX ON :GraphState(graph_revision);

CREATE INDEX ON :Source(id);
CREATE INDEX ON :Source(source_kind);
CREATE INDEX ON :Source(identity_namespace);
CREATE INDEX ON :Source(trust_revision);
CREATE INDEX ON :Source(lifecycle_disposition);
CREATE INDEX ON :Source(ownership_category);
CREATE INDEX ON :Source(engagement_id);
CREATE INDEX ON :SourceArtifact(id);
CREATE INDEX ON :SourceArtifact(source_id);
CREATE INDEX ON :SourceArtifact(artifact_locator_profile);
CREATE INDEX ON :SourceArtifact(source_revision);
CREATE INDEX ON :SourceArtifact(content_digest);
CREATE INDEX ON :SourceArtifact(lifecycle_disposition);
CREATE INDEX ON :SourceArtifact(recorded_at);
CREATE INDEX ON :SourceArtifact(retired_at);
CREATE INDEX ON :SourceArtifact(engagement_id);
CREATE INDEX ON :Observation(id);
CREATE INDEX ON :Observation(source_id);
CREATE INDEX ON :Observation(event_key);
CREATE INDEX ON :Observation(result_digest);
CREATE INDEX ON :Observation(lifecycle_disposition);
CREATE INDEX ON :Observation(recorded_at);
CREATE INDEX ON :Observation(retired_at);
CREATE INDEX ON :Observation(engagement_id);
CREATE INDEX ON :Passage(id);
CREATE INDEX ON :Passage(basis_id);
CREATE INDEX ON :Passage(locator_profile);
CREATE INDEX ON :Passage(passage_digest);
CREATE INDEX ON :ExtractionReceipt(id);
CREATE INDEX ON :ExtractionReceipt(method_id);
CREATE INDEX ON :AcquisitionReceipt(id);
CREATE INDEX ON :AcquisitionReceipt(basis_id);

CREATE INDEX ON :Proposition(id);
CREATE INDEX ON :Proposition(predicate_id);
CREATE INDEX ON :Proposition(polarity);
CREATE INDEX ON :Proposition(modality_family);
CREATE INDEX ON :Proposition(modality_operator);
CREATE INDEX ON :Proposition(lifecycle_disposition);
CREATE INDEX ON :Proposition(state_revision);
CREATE INDEX ON :Proposition(support_revision);
CREATE INDEX ON :Proposition(recorded_at);
CREATE INDEX ON :Proposition(retired_at);
CREATE INDEX ON :Proposition(ownership_category);
CREATE INDEX ON :Proposition(engagement_id);
CREATE INDEX ON :Proposition(embedded_at);
CREATE INDEX ON :Proposition(embedding_model_id);
CREATE INDEX ON :Proposition(reasoning_projection_member);

CREATE INDEX ON :Assertion(id);
CREATE INDEX ON :Assertion(source_id);
CREATE INDEX ON :Assertion(basis_kind);
CREATE INDEX ON :Assertion(basis_id);
CREATE INDEX ON :Assertion(valid_time_kind);
CREATE INDEX ON :Assertion(valid_time_start);
CREATE INDEX ON :Assertion(valid_time_end);
CREATE INDEX ON :Assertion(valid_time_at);
CREATE INDEX ON :Assertion(lifecycle_disposition);
CREATE INDEX ON :Assertion(state_revision);
CREATE INDEX ON :Assertion(recorded_at);
CREATE INDEX ON :Assertion(retired_at);
CREATE INDEX ON :Assertion(trust_revision);
CREATE INDEX ON :Assertion(ownership_category);
CREATE INDEX ON :Assertion(engagement_id);

CREATE INDEX ON :Entity(canonical_id);
CREATE INDEX ON :Entity(identity_key);
CREATE INDEX ON :Entity(identity_revision);
CREATE INDEX ON :Entity(primary_label);
CREATE INDEX ON :Entity(entity_type);
CREATE INDEX ON :Predicate(canonical_id);
CREATE INDEX ON :Predicate(identity_revision);
CREATE INDEX ON :Predicate(label);
CREATE INDEX ON :Predicate(arity);
CREATE INDEX ON :Predicate(semantic_class);
CREATE INDEX ON :SemanticDefinition(id);
CREATE INDEX ON :SemanticDefinition(definition_kind);
CREATE INDEX ON :SemanticDefinition(identity_revision);
CREATE INDEX ON :SemanticBinding(id);
CREATE INDEX ON :SemanticBinding(owner_kind);
CREATE INDEX ON :SemanticBinding(owner_id);
CREATE INDEX ON :SemanticBinding(binding_kind);
CREATE INDEX ON :SemanticBinding(role);
CREATE INDEX ON :SemanticBinding(position);
CREATE INDEX ON :SemanticBinding(value_kind);

CREATE TEXT INDEX proposition_rendering ON :Proposition(rendering);

CREATE VECTOR INDEX proposition_embeddings ON :Proposition(embedding)
WITH CONFIG {
    "dimension": 384,
    "capacity": 5000000,
    "metric": "cos",
    "scalar_kind": "f32",
    "resize_coefficient": 2
};

// Physical records use primitive properties and SemanticBinding nodes. Nested
// internal JSON is prohibited. Missing scalar properties hydrate to native
// empty mappings/lists at the application boundary.
//
// SchemaRevision: component, deployment_owner, schema_digest, installed_at.
// GraphState: name, deployment_owner, graph_revision,
// installation_state.
//
// Source: id, source_kind, identity_namespace, authority_key,
// trust_revision, lifecycle_disposition, state_revision, ownership_category,
// owner_id, engagement_id, classification_level, recorded_at, retired_at.
// SourceArtifact: id, source_id, artifact_locator_profile,
// artifact_locator_key, source_revision, digest_algorithm, content_digest,
// content_carrier_kind, content_inline or immutable content-reference fields,
// byte_count, character_encoding, lifecycle/state, transaction-time,
// classification and ownership primitive properties.
// Observation: id, source_id, observation_kind,
// event_namespace, event_key, result_digest, observed_at, adapter and producer
// receipt identifiers, exactly one inline or immutable referenced result
// carrier, lifecycle/state, transaction-time, classification and ownership
// primitive properties.
// Passage: id, basis_kind, basis_id, locator_profile,
// locator_key, digest_algorithm, passage_digest, text, character_encoding,
// byte_count, lifecycle/state, classification and ownership primitive
// properties.
// ExtractionReceipt: id, method_id, candidate_output_digest, selection_count,
// transformation_receipt_digest, classification, ownership, recorded_at, and
// retired_at. AcquisitionReceipt records repeat acquisition
// provenance by id, basis_id, method, acquired_at, and result status.
// Proposition: id, predicate_id, predicate_identity_revision,
// polarity, modality_family, modality_operator, canonicalization_profile,
// lifecycle_disposition, state_revision,
// support_revision, lifecycle_reason, superseded_by, recorded_at, retired_at,
// ownership_category, owner_id, engagement_id, classification_level,
// rendering, embedding, embedded_at,
// embedding_model_id, and reasoning_projection_member. Arguments,
// qualifications, semantic context, and applicability scope use owned
// SemanticBinding nodes.
// Assertion: id, source_id, basis_kind, basis_id,
// proposition_id, valid_time kind/bounds/instant/precision/basis,
// transaction-time, lifecycle/state, reliability, ownership and classification
// primitive properties. Exact selections and extraction lineage
// use relationships; source qualifications and assertion context use bindings.
// Entity: canonical_id, identity_key, identity_revision, primary_label,
// entity_type, lifecycle/state, classification and ownership.
// Predicate: canonical_id, identity_revision, label, arity, semantic_class,
// role-contract and lifecycle/state properties. SemanticDefinition: id,
// definition_kind, identity_revision, definition_text, content_digest,
// lifecycle/state, classification and ownership.
// SemanticBinding: id, owner_kind, owner_id, binding_kind, role, position,
// value_kind, datatype, literal value, exact-decimal or rational fields,
// dimension, and canonical_unit_id. Entity- and Proposition-valued bindings use
// relationships.
//
// SourceArtifact -[:FROM_SOURCE]-> Source
// Observation -[:FROM_SOURCE]-> Source
// Passage -[:FROM_BASIS]-> SourceArtifact|Observation
// ExtractionReceipt -[:SELECTS]-> Passage
// ExtractionReceipt -[:SELECTS_FROM]-> Observation
// Assertion -[:FROM_SOURCE]-> Source
// Assertion -[:FROM_BASIS]-> SourceArtifact|Observation
// Assertion -[:EXTRACTED_BY]-> ExtractionReceipt
// Assertion -[:ASSERTS]-> Proposition
// Proposition -[:USES_PREDICATE]-> Predicate
// Proposition -[:HAS_ARGUMENT]-> SemanticBinding
// Proposition -[:HAS_QUALIFICATION]-> SemanticBinding
// Proposition -[:HAS_CONTEXT]-> SemanticBinding
// Proposition -[:HAS_APPLICABILITY_SCOPE]-> SemanticBinding
// Assertion -[:HAS_QUALIFICATION]-> SemanticBinding
// Assertion -[:HAS_CONTEXT]-> SemanticBinding
// SemanticBinding -[:BINDS_ENTITY]-> Entity
// SemanticBinding -[:BINDS_PROPOSITION]-> Proposition
// Proposition -[:SUPPORTED_BY]-> Assertion
// Proposition -[:OPPOSED_BY]-> Assertion
// Proposition -[:CONTRADICTS]-> Proposition
// SourceArtifact -[:REVISES]-> SourceArtifact
// Assertion -[:SUPERSEDES]-> Assertion
