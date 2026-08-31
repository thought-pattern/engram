// Engram standalone corrected recall schema
// Memgraph 3.9 Cypher DDL
//
// Schema owner: engram
// Representation contract: tapestry-ke-representation-v1
// Engram support contract: tapestry-engram-support-v1
//
// Apply only to an empty standalone Engram Memgraph with:
//   python3 scripts/setup_schema.py --apply
//
// A Tapestry-managed Memgraph receives only the root Tapestry installer.
// Engram verifies that catalog in tapestry_managed mode and never applies this
// file to it. This schema contains no retired-representation or compatibility
// declarations.

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

CREATE INDEX ON :SchemaRevision(component);
CREATE INDEX ON :SchemaRevision(store_epoch);
CREATE INDEX ON :SchemaRevision(representation_contract);
CREATE INDEX ON :SchemaRevision(engram_support_contract);
CREATE INDEX ON :GraphState(name);
CREATE INDEX ON :GraphState(store_epoch);
CREATE INDEX ON :GraphState(installation_state);
CREATE INDEX ON :GraphState(graph_revision);

CREATE INDEX ON :Source(id);
CREATE INDEX ON :Source(source_kind);
CREATE INDEX ON :Source(identity_namespace);
CREATE INDEX ON :Source(trust_revision);
CREATE INDEX ON :Source(lifecycle_disposition);
CREATE INDEX ON :Source(ownership_category);
CREATE INDEX ON :Source(visibility_kind);
CREATE INDEX ON :Source(company_id);
CREATE INDEX ON :Source(customer_id);
CREATE INDEX ON :Source(engagement_id);
CREATE INDEX ON :SourceArtifact(id);
CREATE INDEX ON :SourceArtifact(source_id);
CREATE INDEX ON :SourceArtifact(artifact_locator_profile);
CREATE INDEX ON :SourceArtifact(source_revision);
CREATE INDEX ON :SourceArtifact(content_digest);
CREATE INDEX ON :SourceArtifact(lifecycle_disposition);
CREATE INDEX ON :SourceArtifact(recorded_at);
CREATE INDEX ON :SourceArtifact(retired_at);
CREATE INDEX ON :SourceArtifact(visibility_kind);
CREATE INDEX ON :SourceArtifact(company_id);
CREATE INDEX ON :SourceArtifact(customer_id);
CREATE INDEX ON :SourceArtifact(engagement_id);
CREATE INDEX ON :Observation(id);
CREATE INDEX ON :Observation(source_id);
CREATE INDEX ON :Observation(event_key);
CREATE INDEX ON :Observation(result_digest);
CREATE INDEX ON :Observation(lifecycle_disposition);
CREATE INDEX ON :Observation(recorded_at);
CREATE INDEX ON :Observation(retired_at);
CREATE INDEX ON :Observation(visibility_kind);
CREATE INDEX ON :Observation(company_id);
CREATE INDEX ON :Observation(customer_id);
CREATE INDEX ON :Observation(engagement_id);
CREATE INDEX ON :Passage(id);
CREATE INDEX ON :Passage(basis_id);
CREATE INDEX ON :Passage(locator_profile);
CREATE INDEX ON :Passage(passage_digest);
CREATE INDEX ON :Passage(visibility_kind);
CREATE INDEX ON :ExtractionReceipt(id);
CREATE INDEX ON :ExtractionReceipt(method_id);
CREATE INDEX ON :ExtractionReceipt(method_version);
CREATE INDEX ON :ExtractionReceipt(contract_version);
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
CREATE INDEX ON :Proposition(visibility_kind);
CREATE INDEX ON :Proposition(company_id);
CREATE INDEX ON :Proposition(customer_id);
CREATE INDEX ON :Proposition(engagement_id);
CREATE INDEX ON :Proposition(embedded_at);
CREATE INDEX ON :Proposition(embedding_model_version);
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
CREATE INDEX ON :Assertion(visibility_kind);
CREATE INDEX ON :Assertion(company_id);
CREATE INDEX ON :Assertion(customer_id);
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
CREATE INDEX ON :SemanticDefinition(registry_version);
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
// SchemaRevision: component, deployment_owner, store_epoch,
// representation_contract, engram_support_contract, schema_digest,
// installed_at.
// GraphState: name, deployment_owner, store_epoch, graph_revision,
// installation_state.
//
// Source: id, schema_version, source_kind, identity_namespace, authority_key,
// trust_revision, lifecycle_disposition, state_revision, ownership_category,
// classification_level, visibility_kind, company_id, customer_id,
// engagement_id, recorded_at, retired_at.
// SourceArtifact: id, schema_version, source_id, artifact_locator_profile,
// artifact_locator_key, source_revision, digest_algorithm, content_digest,
// content_carrier_kind, content_inline or immutable content-reference fields,
// byte_count, character_encoding, lifecycle/state, transaction-time,
// classification, ownership, and visibility primitive properties.
// Observation: id, schema_version, source_id, observation_kind,
// event_namespace, event_key, result_digest, observed_at, adapter and producer
// receipt identifiers, exactly one inline or immutable referenced result
// carrier, lifecycle/state, transaction-time, classification, ownership, and
// visibility primitive properties.
// Passage: id, schema_version, basis_kind, basis_id, locator_profile,
// locator_key, digest_algorithm, passage_digest, text, character_encoding,
// byte_count, lifecycle/state, classification, ownership, and visibility
// primitive properties.
// ExtractionReceipt: id, schema_version, method_id, method_version,
// contract_version, candidate_output_digest, selection_count,
// transformation_receipt_digest, classification, ownership, visibility,
// recorded_at, and retired_at. AcquisitionReceipt records repeat acquisition
// provenance by id, basis_id, method/version, acquired_at, and result status.
// Proposition: id, schema_version, predicate_id, predicate_identity_revision,
// polarity, modality_family, modality_operator, canonicalization_profile,
// canonicalization_profile_version, ontology_version_digest,
// unit_registry_version, lifecycle_disposition, state_revision,
// support_revision, lifecycle_reason, superseded_by, recorded_at, retired_at,
// ownership_category, classification_level, visibility_kind, company_id,
// customer_id, engagement_id, rendering, embedding, embedded_at,
// embedding_model_version, and reasoning_projection_member. Arguments,
// qualifications, semantic context, and applicability scope use owned
// SemanticBinding nodes.
// Assertion: id, schema_version, source_id, basis_kind, basis_id,
// proposition_id, valid_time kind/bounds/instant/precision/basis,
// transaction-time, lifecycle/state, reliability, ownership/classification,
// and visibility primitive properties. Exact selections and extraction lineage
// use relationships; source qualifications and assertion context use bindings.
// Entity: canonical_id, identity_key, identity_revision, primary_label,
// entity_type, lifecycle/state, classification, ownership, and visibility.
// Predicate: canonical_id, identity_revision, label, arity, semantic_class,
// role-contract and lifecycle/version properties. SemanticDefinition: id,
// definition_kind, identity_revision, registry_version, content_digest,
// lifecycle/state, classification, ownership, and visibility.
// SemanticBinding: id, schema_version, owner_kind, owner_id, binding_kind,
// role, position, value_kind, datatype, literal value, exact-decimal or rational
// fields, dimension, canonical_unit_id, unit_registry_version, and binding-
// profile version. Entity- and Proposition-valued bindings use relationships.
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
