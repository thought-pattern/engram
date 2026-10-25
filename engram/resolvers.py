"""Resolver adapters over the response-artifact repository and the deterministic utility registry."""

from hashlib import sha256 as hashlib_sha256

from engram.constants_artifacts import LifecycleState
from engram.constants_identity import ExactLookupOutcome
from engram.constants_resolution import (
    EXACT_RESOLVER_COST_CLASS,
    EXACT_RESOLVER_NAME,
    SPARSE_RESOLVER_COST_CLASS,
    SPARSE_RESOLVER_NAME,
    STANDALONE_SEMANTIC_RESOLVER_COST_CLASS,
    STANDALONE_SEMANTIC_RESOLVER_NAME,
    UTILITY_RESOLVER_COST_CLASS,
    UTILITY_RESOLVER_NAME,
    UTILITY_RESOLVER_PRODUCER,
    CandidateSource,
    EvidenceKind,
    ResolverState,
)
from engram.errors import (
    InvalidRequestError,
    ResourceNotFoundError,
)
from engram.identity import scoped_retrieval_key_from_text
from engram.resolution import (
    accounting_observation,
    candidate as resolution_candidate,
    candidate_to_dict,
    evidence_reference,
    resolver_result,
)
from engram.resolution_budget import (
    budget_consumption,
)
from engram.resolution_evidence import (
    feature_set,
)
from engram.resolution_sizing import json_size
from engram.resolver_plan import (
    resolver_clock_ns,
    run_cooperative_check,
)
from engram.utilities import UtilityRegistry, utility_statement_id


def internal_candidate_id(source: CandidateSource, statement_id: str, diagnostic_id: str) -> str:
    digest = hashlib_sha256(f"{diagnostic_id}:{source.value}:{statement_id}".encode()).hexdigest()
    result = f"candidate:sha256:{digest}"
    return result


def artifact_matches_frame(artifact: dict, frame: dict) -> bool:
    if (
        artifact.get("scope", {}) != frame.get("scope", {})
        or artifact.get("lifecycle", LifecycleState.RETIRED) != LifecycleState.ACTIVE
    ):
        result = False
        return result
    required_source_label = frame.get("required_source_label", "")
    if required_source_label and artifact.get("provenance", {}).get("source_label", "") != required_source_label:
        result = False
        return result
    metadata = artifact.get("metadata", {})
    result = all(key in metadata and metadata.get(key, ()) == value for key, value in frame.get("required_metadata", {}).items())
    return result


def exhausted_result(name: str, dimensions: tuple[str, ...]) -> dict:
    result = resolver_result(
        resolver=name,
        state=ResolverState.EXHAUSTED,
        reason_code=f"{dimensions[0]}_budget",
        consumption=budget_consumption(resolvers=1, exhausted_dimensions=dimensions),
    )
    return result


class ExactResolver:
    """Adapter over the Section 3 contextual exact repository."""

    def __init__(self, engram, clock_ns: object) -> None:
        self.name = EXACT_RESOLVER_NAME
        self.cost_class = EXACT_RESOLVER_COST_CLASS
        self.internal_engram = engram
        self.internal_clock_ns = clock_ns

    def available(self, frame: dict) -> bool:
        result = frame.get("eligibility_context", {}).get("artifact_repository_available", False)
        return result

    def resolve(
        self,
        frame: dict,
        budget: dict,
        cooperative_check: object = (),
    ) -> dict:
        run_cooperative_check(cooperative_check)
        if not budget.get("max_candidates", 0) or not budget.get("max_working_memory_bytes", 0):
            dimension = "candidates" if not budget.get("max_candidates", 0) else "working_memory_bytes"
            result = exhausted_result(self.name, (dimension,))
            return result
        started = resolver_clock_ns(self.internal_clock_ns)
        key = scoped_retrieval_key_from_text(frame.get("scope", {}), frame.get("resolved_text", ""))
        contextual = self.internal_engram.response_repository.exact_lookup(
            key,
            frame.get("eligibility_context", {}),
        )
        lookup = contextual.get("lookup", {})
        outcome = lookup.get("outcome", ExactLookupOutcome.MISS)
        if outcome != ExactLookupOutcome.FOUND:
            result = resolver_result(
                resolver=self.name,
                state=ResolverState.COMPLETED,
                reason_code=f"exact_{outcome.value}",
                diagnostics={
                    "owner_count": len(lookup.get("owner_statement_ids", ())),
                    "truncated": lookup.get("truncated", False),
                },
                consumption=budget_consumption(elapsed_ns=max(0, resolver_clock_ns(self.internal_clock_ns) - started), resolvers=1),
            )
            return result
        artifact = self.internal_engram.response_repository.get_artifact(lookup.get("statement_id", ""))
        if not artifact_matches_frame(artifact, frame):
            result = resolver_result(
                resolver=self.name,
                state=ResolverState.COMPLETED,
                reason_code="exact_required_filter_excluded",
                consumption=budget_consumption(elapsed_ns=max(0, resolver_clock_ns(self.internal_clock_ns) - started), resolvers=1),
            )
            return result
        statement_id = artifact.get("statement_id", "")
        scope = artifact.get("scope", {})
        candidate = resolution_candidate(
            candidate_id=internal_candidate_id(CandidateSource.EXACT, statement_id, frame.get("diagnostic_id", "")),
            statement_id=statement_id,
            response=artifact.get("response", ""),
            source=CandidateSource.EXACT,
            features=feature_set(values={"exact_match": 1.0}, unavailable=()),
            evidence=tuple(
                evidence_reference(
                    evidence_id=reference.get("id", ""),
                    resolver=self.name,
                    kind=EvidenceKind.SUPPORT,
                    scope=scope,
                    provenance={"support_linked": True},
                )
                for reference in artifact.get("support_references", ())[: budget.get("max_evidence", 0)]
            ),
            scope=scope,
            lifecycle=artifact.get("lifecycle", LifecycleState.RETIRED),
            provenance={
                "retrieval_origin": lookup.get("provenance", ""),
                "representation": lookup.get("representation", ""),
                "generation": artifact.get("generation", 0),
                "source_label": artifact.get("provenance", {}).get("source_label", ""),
            },
            diagnostics={"context_signature": contextual.get("context_signature", "")},
        )
        if json_size(candidate_to_dict(candidate)) > budget.get("max_working_memory_bytes", 0):
            result = exhausted_result(self.name, ("working_memory_bytes",))
            return result
        result = resolver_result(
            resolver=self.name,
            state=ResolverState.COMPLETED,
            reason_code="exact_found",
            candidates=(candidate,),
            accounting=(accounting_observation(statement_id),),
            consumption=budget_consumption(
                elapsed_ns=max(0, resolver_clock_ns(self.internal_clock_ns) - started),
                resolvers=1,
                candidates=1,
                evidence=len(candidate.get("evidence", ())),
            ),
        )
        return result


class UtilityResolver:
    """Adapter over the configured fixed registry of deterministic operations."""

    def __init__(self, registry: UtilityRegistry, clock_ns: object) -> None:
        if not isinstance(registry, UtilityRegistry):
            raise InvalidRequestError("utility resolver requires a UtilityRegistry")
        if not callable(clock_ns):
            raise InvalidRequestError("utility resolver clock_ns must be callable")
        self.name = UTILITY_RESOLVER_NAME
        self.cost_class = UTILITY_RESOLVER_COST_CLASS
        self.internal_registry = registry
        self.internal_clock_ns = clock_ns

    def available(self, frame: dict) -> bool:
        del frame
        result = self.internal_registry.available()
        return result

    def resolve(
        self,
        frame: dict,
        budget: dict,
        cooperative_check: object = (),
    ) -> dict:
        run_cooperative_check(cooperative_check)
        if not budget.get("max_candidates", 0) or not budget.get("max_working_memory_bytes", 0):
            dimension = "candidates" if not budget.get("max_candidates", 0) else "working_memory_bytes"
            result = exhausted_result(self.name, (dimension,))
            return result
        started = resolver_clock_ns(self.internal_clock_ns)
        if frame.get("required_metadata", {}) or frame.get("required_source_label", ""):
            result = resolver_result(
                resolver=self.name,
                state=ResolverState.COMPLETED,
                reason_code="utility_filters_unsupported",
                consumption=budget_consumption(elapsed_ns=max(0, resolver_clock_ns(self.internal_clock_ns) - started), resolvers=1),
            )
            return result
        evaluation = self.internal_registry.evaluate(frame.get("original_text", ""))
        status = evaluation.get("status", "failed")
        plugin_name = evaluation.get("plugin_name", "")
        canonical_input = evaluation.get("canonical_input", "")
        operations = evaluation.get("operations", 0)
        diagnostics = {
            "plugin_name": plugin_name,
            "error_code": evaluation.get("error_code", ""),
            "operations": operations,
        }
        if status == "miss":
            result = resolver_result(
                resolver=self.name,
                state=ResolverState.COMPLETED,
                reason_code="utility_miss",
                diagnostics=diagnostics,
                consumption=budget_consumption(elapsed_ns=max(0, resolver_clock_ns(self.internal_clock_ns) - started), resolvers=1),
            )
            return result
        if status == "rejected":
            result = resolver_result(
                resolver=self.name,
                state=ResolverState.COMPLETED,
                reason_code="utility_rejected",
                diagnostics=diagnostics,
                consumption=budget_consumption(elapsed_ns=max(0, resolver_clock_ns(self.internal_clock_ns) - started), resolvers=1),
            )
            return result
        if status == "failed":
            result = resolver_result(
                resolver=self.name,
                state=ResolverState.FAILED,
                reason_code="utility_plugin_failure",
                diagnostics=diagnostics,
                consumption=budget_consumption(elapsed_ns=max(0, resolver_clock_ns(self.internal_clock_ns) - started), resolvers=1),
            )
            return result
        statement_id = utility_statement_id(plugin_name, canonical_input)
        current = resolution_candidate(
            candidate_id=internal_candidate_id(CandidateSource.UTILITY, statement_id, frame.get("diagnostic_id", "")),
            statement_id=statement_id,
            response=evaluation.get("response", ""),
            source=CandidateSource.UTILITY,
            features=feature_set(values={"utility_match": 1.0}, unavailable=()),
            evidence=(),
            scope=frame.get("scope", {}),
            lifecycle=LifecycleState.ACTIVE,
            provenance={
                "producer": UTILITY_RESOLVER_PRODUCER,
                "plugin_name": plugin_name,
                "canonical_input": canonical_input,
                "learnable": False,
            },
            diagnostics={"operations": operations},
        )
        working_memory_bytes = json_size(candidate_to_dict(current))
        if working_memory_bytes > budget.get("max_working_memory_bytes", 0):
            result = exhausted_result(self.name, ("working_memory_bytes",))
            return result
        result = resolver_result(
            resolver=self.name,
            state=ResolverState.COMPLETED,
            reason_code="utility_resolved",
            candidates=(current,),
            diagnostics=diagnostics,
            consumption=budget_consumption(
                elapsed_ns=max(0, resolver_clock_ns(self.internal_clock_ns) - started),
                resolvers=1,
                candidates=1,
                working_memory_bytes=working_memory_bytes,
            ),
        )
        return result


class StandaloneSemanticResolver:
    """Bounded adapter over canonical-request and alias embeddings."""

    def __init__(self, engram, clock_ns: object) -> None:
        self.name = STANDALONE_SEMANTIC_RESOLVER_NAME
        self.cost_class = STANDALONE_SEMANTIC_RESOLVER_COST_CLASS
        self.internal_engram = engram
        self.internal_clock_ns = clock_ns

    # The resolver protocol passes the request frame to every availability check; this resolver's readiness does not depend on it.
    def available(self, frame: dict) -> bool:  # pylint: disable=unused-argument
        settings = self.internal_engram.config.get("semantic", {})
        result = bool(settings.get("enabled", False) and self.internal_engram.semantic_retriever.available)
        return result

    def resolve(
        self,
        frame: dict,
        budget: dict,
        cooperative_check: object = (),
    ) -> dict:
        run_cooperative_check(cooperative_check)
        if (
            not budget.get("max_candidates", 0)
            or not budget.get("max_vector_results", 0)
            or not budget.get("max_working_memory_bytes", 0)
        ):
            exhausted = tuple(
                name
                for present, name in (
                    (budget.get("max_candidates", 0), "candidates"),
                    (budget.get("max_vector_results", 0), "vector_results"),
                    (budget.get("max_working_memory_bytes", 0), "working_memory_bytes"),
                )
                if not present
            )
            result = exhausted_result(self.name, exhausted)
            return result
        started = resolver_clock_ns(self.internal_clock_ns)
        discovery = self.internal_engram.semantic_candidates(
            frame.get("resolved_text", ""),
            frame.get("scope", {}),
            limit=budget.get("max_candidates", 0),
            max_vector_results=budget.get("max_vector_results", 0),
            max_working_memory_bytes=budget.get("max_working_memory_bytes", 0),
            cooperative_check=cooperative_check,
        )
        scanned_records = discovery.get("scanned_records", 0)
        if not discovery.get("complete", False):
            reason = discovery.get("reason", "")
            if reason == "semantic_unavailable":
                result = resolver_result(resolver=self.name, state=ResolverState.UNAVAILABLE, reason_code=reason)
                return result
            dimension = {
                "semantic_input_too_large": "input_bytes",
                "semantic_scan_budget": "semantic_scan_records",
                "vector_result_budget": "vector_results",
                "working_memory_budget": "working_memory_bytes",
            }.get(reason, "semantic_resources")
            result = resolver_result(
                resolver=self.name,
                state=ResolverState.EXHAUSTED,
                reason_code=reason,
                diagnostics={"scanned_records": scanned_records},
                consumption=budget_consumption(
                    elapsed_ns=max(0, resolver_clock_ns(self.internal_clock_ns) - started),
                    resolvers=1,
                    working_memory_bytes=discovery.get("working_memory_bytes", 0),
                    exhausted_dimensions=(dimension,),
                ),
            )
            return result
        matches = discovery.get("matches", ())
        artifact_identity = self.internal_engram.semantic_retriever.health().get("artifact_identity", {})
        lexical_terms = frame.get("identity", {}).get("lexical_terms", ())
        candidates = []
        accounting = []
        retained_bytes = discovery.get("working_memory_bytes", 0)
        for match in matches:
            try:
                artifact = self.internal_engram.response_repository.get_artifact(match.get("statement_id", ""))
            except ResourceNotFoundError:
                continue
            generation = artifact.get("generation", 0)
            if generation != match.get("generation", 0) or not artifact_matches_frame(artifact, frame):
                continue
            statement_id = artifact.get("statement_id", "")
            origin = match.get("origin", "")
            candidate = resolution_candidate(
                candidate_id=internal_candidate_id(
                    CandidateSource.STANDALONE_SEMANTIC, statement_id, frame.get("diagnostic_id", "")
                ),
                statement_id=statement_id,
                response=artifact.get("response", ""),
                source=CandidateSource.STANDALONE_SEMANTIC,
                features=feature_set(
                    values={
                        "semantic_score": match.get("similarity", 0.0),
                        "semantic_alias_match": float(origin == "alias"),
                    },
                    unavailable=(),
                ),
                evidence=(),
                scope=artifact.get("scope", {}),
                lifecycle=artifact.get("lifecycle", LifecycleState.RETIRED),
                provenance={
                    "generation": generation,
                    "source_label": artifact.get("provenance", {}).get("source_label", ""),
                    "semantic_model_id": artifact_identity.get("model_id", ""),
                    "semantic_model_revision": artifact_identity.get("model_revision", ""),
                    "matched_representation_id": match.get("representation_id", ""),
                    "matched_representation_origin": origin,
                    "matched_alias_ordinal": match.get("ordinal", -1) if origin == "alias" else -1,
                },
                diagnostics={},
            )
            candidate_bytes = json_size(candidate_to_dict(candidate))
            if retained_bytes + candidate_bytes > budget.get("max_working_memory_bytes", 0):
                result = resolver_result(
                    resolver=self.name,
                    state=ResolverState.EXHAUSTED,
                    reason_code="semantic_candidate_memory_budget",
                    diagnostics={"scanned_records": scanned_records},
                    consumption=budget_consumption(
                        elapsed_ns=max(0, resolver_clock_ns(self.internal_clock_ns) - started),
                        resolvers=1,
                        vector_results=len(matches),
                        working_memory_bytes=min(retained_bytes, budget.get("max_working_memory_bytes", 0)),
                        exhausted_dimensions=("working_memory_bytes",),
                    ),
                )
                return result
            retained_bytes += candidate_bytes
            candidates.append(candidate)
            accounting.append(accounting_observation(statement_id, lexical_terms))
        result = resolver_result(
            resolver=self.name,
            state=ResolverState.COMPLETED,
            reason_code="semantic_candidates" if candidates else "semantic_miss",
            candidates=tuple(candidates),
            accounting=tuple(accounting),
            diagnostics={
                "scanned_records": scanned_records,
                "model_revision": artifact_identity.get("model_revision", ""),
                "backend": artifact_identity.get("backend", ""),
            },
            consumption=budget_consumption(
                elapsed_ns=max(0, resolver_clock_ns(self.internal_clock_ns) - started),
                resolvers=1,
                candidates=len(candidates),
                vector_results=len(matches),
                working_memory_bytes=retained_bytes,
            ),
        )
        return result


class SparseResolver:
    """Pure adapter over request-local fielded sparse retrieval."""

    def __init__(self, engram, clock_ns: object) -> None:
        self.name = SPARSE_RESOLVER_NAME
        self.cost_class = SPARSE_RESOLVER_COST_CLASS
        self.internal_engram = engram
        self.internal_clock_ns = clock_ns

    # The resolver protocol passes the request frame to every availability check; this resolver's readiness does not depend on it.
    def available(self, frame: dict) -> bool:  # pylint: disable=unused-argument
        settings = self.internal_engram.config.get("sparse", {})
        result = bool(settings.get("enabled", False))
        return result

    def resolve(
        self,
        frame: dict,
        budget: dict,
        cooperative_check: object = (),
    ) -> dict:
        run_cooperative_check(cooperative_check)
        if not budget.get("max_candidates", 0) or not budget.get("max_working_memory_bytes", 0):
            dimension = "candidates" if not budget.get("max_candidates", 0) else "working_memory_bytes"
            result = exhausted_result(self.name, (dimension,))
            return result
        started = resolver_clock_ns(self.internal_clock_ns)
        discovery = self.internal_engram.sparse_candidates(
            frame.get("resolved_text", ""),
            frame.get("scope", {}),
            limit=budget.get("max_candidates", 0),
            max_working_memory_bytes=budget.get("max_working_memory_bytes", 0),
        )
        posting_visits = discovery.get("posting_visits", 0)
        if not discovery.get("complete", False):
            reason = discovery.get("reason", "")
            if reason == "sparse_unavailable":
                result = resolver_result(
                    resolver=self.name,
                    state=ResolverState.UNAVAILABLE,
                    reason_code=reason,
                )
                return result
            exhausted_dimension = {
                "query_term_budget": "query_terms",
                "posting_visit_budget": "posting_visits",
                "working_memory_budget": "working_memory_bytes",
            }.get(reason, "sparse_resources")
            result = resolver_result(
                resolver=self.name,
                state=ResolverState.EXHAUSTED,
                reason_code=reason,
                diagnostics={"posting_visits": posting_visits},
                consumption=budget_consumption(
                    elapsed_ns=max(0, resolver_clock_ns(self.internal_clock_ns) - started),
                    resolvers=1,
                    working_memory_bytes=discovery.get("working_memory_bytes", 0),
                    exhausted_dimensions=(exhausted_dimension,),
                ),
            )
            return result

        query_term_count = discovery.get("query_term_count", 0)
        lexical_terms = frame.get("identity", {}).get("lexical_terms", ())
        candidates = []
        accounting = []
        retained_bytes = discovery.get("working_memory_bytes", 0)
        for match in discovery.get("matches", ()):
            try:
                artifact = self.internal_engram.response_repository.get_artifact(match.get("statement_id", ""))
            except ResourceNotFoundError:
                continue
            if not artifact_matches_frame(artifact, frame):
                continue
            statement_id = artifact.get("statement_id", "")
            field_contributions = match.get("field_contributions", {})
            phrase_fields = match.get("phrase_fields", ())
            proximity_fields = match.get("proximity_fields", ())
            prefix_match_count = match.get("prefix_match_count", 0)
            features = {
                "sparse_score": match.get("score", 0.0),
                "sparse_phrase_match": float(bool(phrase_fields)),
                "sparse_proximity_match": float(bool(proximity_fields)),
                "sparse_prefix_match": float(bool(prefix_match_count)),
                "sparse_character_ngram_similarity": match.get("character_ngram_similarity", 0.0),
                "sparse_technical_exact_match": float(match.get("technical_exact_match", False)),
                "sparse_technical_exact_ratio": match.get("technical_exact_ratio", 0.0),
            }
            features.update({f"sparse_field_{name}": contribution for name, contribution in field_contributions.items()})
            candidate = resolution_candidate(
                candidate_id=internal_candidate_id(CandidateSource.SPARSE, statement_id, frame.get("diagnostic_id", "")),
                statement_id=statement_id,
                response=artifact.get("response", ""),
                source=CandidateSource.SPARSE,
                features=feature_set(values=features, unavailable=()),
                evidence=(),
                scope=artifact.get("scope", {}),
                lifecycle=artifact.get("lifecycle", LifecycleState.RETIRED),
                provenance={
                    "generation": artifact.get("generation", 0),
                    "source_label": artifact.get("provenance", {}).get("source_label", ""),
                },
                diagnostics={
                    "field_contributions": dict(field_contributions),
                    "phrase_fields": list(phrase_fields),
                    "proximity_fields": list(proximity_fields),
                    "minimum_proximity": match.get("minimum_proximity", 0),
                    "prefix_match_count": prefix_match_count,
                    "matched_term_count": match.get("matched_term_count", 0),
                },
            )
            candidate_bytes = json_size(candidate_to_dict(candidate))
            if retained_bytes + candidate_bytes > budget.get("max_working_memory_bytes", 0):
                result = resolver_result(
                    resolver=self.name,
                    state=ResolverState.EXHAUSTED,
                    reason_code="sparse_candidate_memory_budget",
                    diagnostics={
                        "query_term_count": query_term_count,
                        "posting_visits": posting_visits,
                    },
                    consumption=budget_consumption(
                        elapsed_ns=max(0, resolver_clock_ns(self.internal_clock_ns) - started),
                        resolvers=1,
                        working_memory_bytes=min(retained_bytes, budget.get("max_working_memory_bytes", 0)),
                        exhausted_dimensions=("working_memory_bytes",),
                    ),
                )
                return result
            retained_bytes += candidate_bytes
            candidates.append(candidate)
            accounting.append(accounting_observation(statement_id, lexical_terms))
        result = resolver_result(
            resolver=self.name,
            state=ResolverState.COMPLETED,
            reason_code="sparse_candidates" if candidates else "sparse_miss",
            candidates=tuple(candidates),
            accounting=tuple(accounting),
            diagnostics={
                "query_term_count": query_term_count,
                "posting_visits": posting_visits,
            },
            consumption=budget_consumption(
                elapsed_ns=max(0, resolver_clock_ns(self.internal_clock_ns) - started),
                resolvers=1,
                candidates=len(candidates),
                working_memory_bytes=retained_bytes,
            ),
        )
        return result
