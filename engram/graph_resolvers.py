"""Pure graph-backed resolvers: structured Proposition projections and support-linked vector recall."""

from engram.constants_artifacts import LifecycleState
from engram.constants_evidence import PropositionEligibilityReason
from engram.constants_relation import (
    MAX_RELATION_PLAN_ROWS,
    RELATION_ONE_HOP_PRODUCER,
    CanonicalResolutionStatus,
    ExpectedObjectType,
    PredicateCardinality,
    RelationPlanTemplate,
    RelationSelectionReason,
    TemporalQueryOperator,
)
from engram.constants_resolution import (
    STRUCTURED_GRAPH_RESOLVER_COST_CLASS,
    STRUCTURED_GRAPH_RESOLVER_NAME,
    SUPPORT_SEMANTIC_RESOLVER_COST_CLASS,
    SUPPORT_SEMANTIC_RESOLVER_NAME,
    CandidateSource,
    EvidenceKind,
    ResolverState,
)
from engram.errors import (
    InvalidRequestError,
    VectorSupportScanExceededError,
    WorkingMemoryExceededError,
)
from engram.evidence import (
    PropositionEligibilityEvaluator,
    assertion_basis_window,
    proposition_evidence_record,
)
from engram.graph import proposition_projection_to_dict
from engram.graph_composition_resolution import composition_not_applicable, internal_composition_result, with_prior_graph_rows
from engram.relation import (
    RelationQuestion,
    object_type_match,
    one_hop_query_plan,
    phrase_relation_result,
    resolve_canonical_predicate,
    resolve_canonical_subject,
    select_relation_propositions,
)
from engram.resolution import (
    accounting_observation,
    candidate as resolution_candidate,
    candidate_to_dict,
    evidence_reference,
    evidence_reference_to_dict,
    resolver_result,
)
from engram.resolution_budget import (
    budget_consumption,
)
from engram.resolution_evidence import (
    feature_set,
    proposition_evidence_record_with_changes,
    trusted_proposition_evidence_record_to_dict,
)
from engram.resolution_frame import validate_query_frame
from engram.resolution_sizing import json_array_bytes, json_size, working_size
from engram.resolver_plan import (
    resolver_budget_with_changes,
    resolver_clock_ns,
    run_cooperative_check,
)
from engram.resolvers import artifact_matches_frame, exhausted_result, internal_candidate_id


class StructuredGraphResolver:
    """Pure full-Proposition evidence adapter over fixed structured graph projections."""

    def __init__(self, engram, clock_ns: object, eligibility_evaluator: object = ()) -> None:
        self.name = STRUCTURED_GRAPH_RESOLVER_NAME
        self.cost_class = STRUCTURED_GRAPH_RESOLVER_COST_CLASS
        self.internal_engram = engram
        self.internal_clock_ns = clock_ns
        if eligibility_evaluator == ():
            selected_evaluator = PropositionEligibilityEvaluator(getattr(engram, "proposition_visibility_authority", ()))
        elif isinstance(eligibility_evaluator, PropositionEligibilityEvaluator):
            selected_evaluator = eligibility_evaluator
        else:
            raise InvalidRequestError("structured graph eligibility_evaluator must be PropositionEligibilityEvaluator")
        self.internal_eligibility_evaluator: PropositionEligibilityEvaluator = selected_evaluator

    # The resolver protocol passes the request frame to every availability check; this resolver's readiness does not depend on it.
    def available(self, frame: dict) -> bool:  # pylint: disable=unused-argument
        client = self.internal_engram.graph_client
        result = bool(client and getattr(client, "available", True))
        return result

    # The composed two-hop path lives in graph_composition_resolution, bound here as a method.
    internal_composition_result = internal_composition_result

    def internal_relation_result(
        self,
        frame: dict,
        budget: dict,
        started: int,
        question: RelationQuestion,
        cooperative_check: object = (),
    ) -> tuple[dict, ...]:
        """Interpret and execute one canonical one-hop relation plan."""
        if not self.internal_engram.graph_client:
            result = ()
            return result
        graph_rows = 0

        def check() -> None:
            run_cooperative_check(cooperative_check)

        def entity_lookup(surface: str, *, limit: int, cooperative_check=()) -> list:
            nonlocal graph_rows
            remaining = max(0, budget.get("max_graph_rows", 0) - graph_rows - 2)
            if not remaining:
                return []
            rows = self.internal_engram.canonical_entity_matches(
                surface,
                limit=min(limit, remaining),
                cooperative_check=cooperative_check,
            )
            graph_rows += len(rows)
            return rows

        def predicate_lookup(surface: str, *, limit: int, cooperative_check=()) -> list:
            nonlocal graph_rows
            remaining = max(0, budget.get("max_graph_rows", 0) - graph_rows - 2)
            if not remaining:
                return []
            rows = self.internal_engram.canonical_predicate_matches(
                surface,
                limit=min(limit, remaining),
                cooperative_check=cooperative_check,
            )
            graph_rows += len(rows)
            return rows

        subject = resolve_canonical_subject(frame, entity_lookup, question=question, cooperative_check=check)
        predicate = resolve_canonical_predicate(frame, predicate_lookup, question=question, cooperative_check=check)
        subject_status = subject.get("status", CanonicalResolutionStatus.MISS)
        predicate_status = predicate.get("status", CanonicalResolutionStatus.MISS)
        statuses = (subject_status, predicate_status)
        if statuses == (CanonicalResolutionStatus.MISS, CanonicalResolutionStatus.MISS):
            result = ()
            return result
        if CanonicalResolutionStatus.AMBIGUOUS in statuses or CanonicalResolutionStatus.MISS in statuses:
            reason = "relation_identity_ambiguous" if CanonicalResolutionStatus.AMBIGUOUS in statuses else "relation_identity_miss"
            result = resolver_result(
                resolver=self.name,
                state=ResolverState.COMPLETED,
                reason_code=reason,
                diagnostics={
                    "entity_status": subject_status.value,
                    "entity_candidates": len(subject.get("candidate_ids", ())),
                    "predicate_status": predicate_status.value,
                    "predicate_candidates": len(predicate.get("candidate_ids", ())),
                },
                consumption=budget_consumption(
                    elapsed_ns=max(0, resolver_clock_ns(self.internal_clock_ns) - started),
                    resolvers=1,
                    graph_rows=graph_rows,
                ),
            )
            return (result,)
        remaining_rows = budget.get("max_graph_rows", 0) - graph_rows
        if remaining_rows < 2:
            result = exhausted_result(self.name, ("graph_rows",))
            return (result,)
        plan = one_hop_query_plan(
            subject,
            predicate,
            frame.get("expected_object_type", ExpectedObjectType.UNKNOWN),
            max_rows=min(MAX_RELATION_PLAN_ROWS, budget.get("max_evidence", 0), max(1, remaining_rows // 2)),
        )
        template_id = plan.get("template_id", RelationPlanTemplate.ONE_HOP_PROPOSITION)
        expected_object_type = plan.get("expected_object_type", ExpectedObjectType.UNKNOWN)
        include_historical = frame.get("temporal_query", {}).get("operator", TemporalQueryOperator.UNSPECIFIED) not in {
            TemporalQueryOperator.UNSPECIFIED,
            TemporalQueryOperator.CURRENT,
            TemporalQueryOperator.NOW,
        }

        results = self.internal_engram.relation_one_hop_proposition_projections(
            plan.get("subject_entity_id", ""),
            plan.get("predicate_id", ""),
            row_limit=plan.get("max_rows", 0),
            include_historical=include_historical,
            cooperative_check=check,
            max_working_memory_bytes=budget.get("max_working_memory_bytes", 0),
            basis_window=assertion_basis_window(frame),
        )
        graph_rows += len(results)
        retained: list[tuple[dict, dict, float, bool]] = []
        exclusion_counts: dict[str, int] = {}
        revalidation_rows = 0
        checked_frame = validate_query_frame(frame)
        for item in results:
            check()
            projection = item.get("projection", {})
            initial = self.internal_eligibility_evaluator.evaluate(projection, checked_frame, trusted_frame=True)
            if not initial.get("eligible", False):
                reason = initial.get("reason", PropositionEligibilityReason.REVALIDATION_UNAVAILABLE).value
                exclusion_counts[reason] = exclusion_counts.get(reason, 0) + 1
                continue
            if graph_rows >= budget.get("max_graph_rows", 0):
                break
            decision = self.internal_eligibility_evaluator.revalidate(
                projection, checked_frame, self.internal_engram.current_proposition_projection, trusted_frame=True
            )
            graph_rows += 1
            if decision.get("revalidated", False):
                revalidation_rows += 1
            if not decision.get("eligible", False):
                reason = decision.get("reason", PropositionEligibilityReason.REVALIDATION_UNAVAILABLE).value
                exclusion_counts[reason] = exclusion_counts.get(reason, 0) + 1
                continue
            object_type = item.get("object_type", ExpectedObjectType.UNKNOWN)
            type_match, type_match_available = object_type_match(expected_object_type, object_type)
            retained.append((item, decision, type_match, type_match_available))

        selection = select_relation_propositions(
            tuple(item for item, internal_decision, internal_type_match, internal_type_match_available in retained),
            frame.get("temporal_query", {}),
        )
        direct_answer = selection.get("direct_answer", False)
        selection_reason = selection.get("reason", RelationSelectionReason.NO_ELIGIBLE_PROPOSITION)
        selection_cardinality = selection.get("cardinality", PredicateCardinality.UNKNOWN)
        conflict_proposition_ids = selection.get("conflict_proposition_ids", ())
        ranking_proposition_ids = selection.get("ranking_proposition_ids", ())
        subject_score = subject.get("score", 0.0)
        predicate_score = predicate.get("score", 0.0)
        records = []
        ambiguous_result = not direct_answer
        for item, decision, type_match, type_match_available in retained:
            item_projection = item.get("projection", {})
            base = proposition_evidence_record(item_projection, decision, frame, self.name, trusted=True)
            base_features = base.get("features", {})
            values = dict(base_features.get("values", {}))
            values.update({"entity_match": subject_score, "relation_match": predicate_score})
            unavailable = set(base_features.get("unavailable", ()))
            reasons = set(base.get("selection_reasons", ()))
            reasons.update({"entity_resolved", "predicate_resolved", "relation_plan_match"})
            reasons.add(selection_reason.value)
            if item_projection.get("proposition_id", "") in conflict_proposition_ids:
                reasons.add("relation_conflicting_proposition")
            if not type_match_available:
                unavailable.add("object_type_match")
                reasons.add("object_type_unavailable")
            else:
                values["object_type_match"] = type_match
                reasons.add("object_type_match" if type_match else "object_type_mismatch")
            reasons.add("relation_result_ambiguous" if ambiguous_result else "relation_result_unique")
            records.append(
                proposition_evidence_record_with_changes(
                    base,
                    {
                        "features": feature_set(values, tuple(sorted(unavailable - set(values)))),
                        "selection_reasons": tuple(sorted(reasons)),
                    },
                )
            )
        records.sort(key=lambda record: record.get("proposition_id", ""))
        candidates = []
        if direct_answer and budget.get("max_candidates", 0):
            selected_proposition_id = selection.get("selected_proposition_id", "")
            item, _, type_match, type_match_available = next(
                value for value in retained if value[0].get("projection", {}).get("proposition_id", "") == selected_proposition_id
            )
            if not type_match_available or type_match != 0.0:
                item_projection = item.get("projection", {})
                proposition_id = item_projection.get("proposition_id", "")
                reference = evidence_reference(
                    evidence_id=proposition_id,
                    resolver=self.name,
                    kind=EvidenceKind.PROPOSITION,
                    scope=frame.get("scope", {}),
                    provenance={"relation_plan": True},
                    diagnostics={},
                )
                unavailable = () if type_match_available else ("object_type_match",)
                values = {"entity_match": subject_score, "relation_match": predicate_score}
                if type_match_available:
                    values["object_type_match"] = type_match
                subject_label = subject.get("primary_label", "")
                predicate_label = predicate.get("primary_label", "")
                object_label = item.get("object_label", "")
                response = phrase_relation_result(subject_label, predicate_label, object_label)
                candidates.append(
                    resolution_candidate(
                        candidate_id=internal_candidate_id(CandidateSource.UTILITY, proposition_id, frame.get("diagnostic_id", "")),
                        statement_id=proposition_id,
                        response=response,
                        source=CandidateSource.UTILITY,
                        features=feature_set(values, unavailable),
                        evidence=(reference,),
                        scope=frame.get("scope", {}),
                        lifecycle=LifecycleState.ACTIVE,
                        provenance={
                            "producer": RELATION_ONE_HOP_PRODUCER,
                            "subject_entity_id": plan.get("subject_entity_id", ""),
                            "subject_label": subject_label,
                            "predicate_id": plan.get("predicate_id", ""),
                            "predicate_label": predicate_label,
                            "object_entity_id": item_projection.get("object_entity_id", ""),
                            "object_label": object_label,
                            "object_type": item.get("object_type", ExpectedObjectType.UNKNOWN).value,
                            "predicate_cardinality": selection_cardinality.value,
                            "selection_reason": selection_reason.value,
                            "supplied_trust": item_projection.get("supplied_trust", 0.0),
                        },
                        diagnostics={
                            "template_id": template_id.value,
                            "ranking_proposition_ids": ranking_proposition_ids,
                        },
                    )
                )
        exhausted = set()
        record_sizes = [json_size(trusted_proposition_evidence_record_to_dict(record)) for record in records]
        record_total = sum(record_sizes)

        def record_bytes() -> int:
            result = json_array_bytes(record_total, len(records))
            return result

        while records and record_bytes() > budget.get("max_evidence_bytes", 0):
            records.pop()
            record_total -= record_sizes.pop()
            exhausted.add("evidence_bytes")
        output_bytes = record_bytes() + sum(json_size(candidate_to_dict(value)) for value in candidates)
        while candidates and output_bytes > budget.get("max_output_bytes", 0):
            candidates.pop()
            exhausted.add("output_bytes")
            output_bytes = record_bytes()
        if candidates and not records:
            candidates.clear()
            output_bytes = 0
        working_memory = working_size(results) + record_bytes() + output_bytes
        if working_memory > budget.get("max_working_memory_bytes", 0):
            raise WorkingMemoryExceededError("relation resolution working-memory estimate exceeded")
        result = resolver_result(
            resolver=self.name,
            state=ResolverState.COMPLETED,
            reason_code=(
                "relation_proposition_candidate"
                if candidates
                else (
                    "relation_proposition_conflict"
                    if conflict_proposition_ids
                    else "relation_proposition_evidence" if records else "relation_graph_miss"
                )
            ),
            candidates=tuple(candidates),
            proposition_evidence=tuple(records),
            diagnostics={
                "entity_status": subject_status.value,
                "predicate_status": predicate_status.value,
                "template_id": template_id.value,
                "discovery_rows": len(results),
                "revalidation_rows": revalidation_rows,
                "exclusion_counts": exclusion_counts,
                "ambiguous_result": ambiguous_result,
                "selection_reason": selection_reason.value,
                "predicate_cardinality": selection_cardinality.value,
                "conflict_proposition_ids": conflict_proposition_ids,
                "ranking_proposition_ids": ranking_proposition_ids,
            },
            consumption=budget_consumption(
                elapsed_ns=max(0, resolver_clock_ns(self.internal_clock_ns) - started),
                resolvers=1,
                candidates=len(candidates),
                graph_rows=graph_rows,
                evidence=len(records),
                evidence_bytes=record_bytes(),
                output_bytes=output_bytes,
                working_memory_bytes=working_memory,
                exhausted_dimensions=tuple(sorted(exhausted)),
            ),
        )
        return (result,)

    def resolve(
        self,
        frame: dict,
        budget: dict,
        cooperative_check: object = (),
    ) -> dict:
        """Resolve while honoring a transient caller cancellation check."""
        run_cooperative_check(cooperative_check)
        if (
            budget.get("max_graph_rows", 0) < 2
            or not budget.get("max_evidence", 0)
            or not budget.get("max_evidence_bytes", 0)
            or not budget.get("max_output_bytes", 0)
            or not budget.get("max_working_memory_bytes", 0)
        ):
            dimensions = tuple(
                name
                for name, value in (
                    ("evidence", budget.get("max_evidence", 0)),
                    ("evidence_bytes", budget.get("max_evidence_bytes", 0)),
                    ("graph_rows", budget.get("max_graph_rows", 0) if budget.get("max_graph_rows", 0) >= 2 else 0),
                    ("output_bytes", budget.get("max_output_bytes", 0)),
                    ("working_memory_bytes", budget.get("max_working_memory_bytes", 0)),
                )
                if not value
            )
            result = exhausted_result(self.name, dimensions)
            return result
        started = resolver_clock_ns(self.internal_clock_ns)
        composition_rows = 0
        # Composition and one-hop discovery read one parse of the same question.
        question = RelationQuestion(frame.get("resolved_text", ""))
        try:
            composition_result = self.internal_composition_result(frame, budget, started, question, cooperative_check)
            if composition_result and not composition_not_applicable(composition_result[0]):
                result = composition_result[0]
                return result
            if composition_result:
                # "of" and "'s" also appear in one-hop questions. When the
                # subject resolved but no two-hop path compiled, the one-hop
                # and structured paths answer from the rows still unspent.
                composition_rows = composition_result[0].get("consumption", {}).get("graph_rows", 0)
                budget = resolver_budget_with_changes(
                    budget,
                    {"max_graph_rows": max(0, budget.get("max_graph_rows", 0) - composition_rows)},
                )
            relation_result = self.internal_relation_result(frame, budget, started, question, cooperative_check)
            if relation_result:
                result = with_prior_graph_rows(relation_result[0], composition_rows)
                return result
            projections = self.internal_engram.structured_proposition_projections(
                frame.get("resolved_text", ""),
                row_limit=min(budget.get("max_graph_rows", 0) // 2, budget.get("max_evidence", 0)),
                cooperative_check=cooperative_check,
                max_working_memory_bytes=budget.get("max_working_memory_bytes", 0),
                basis_window=assertion_basis_window(frame),
            )
        except WorkingMemoryExceededError:
            result = exhausted_result(self.name, ("working_memory_bytes",))
            return result
        records = []
        exclusion_counts: dict[str, int] = {}
        revalidation_rows = 0
        checked_frame = validate_query_frame(frame)
        for projection in projections:
            run_cooperative_check(cooperative_check)
            initial = self.internal_eligibility_evaluator.evaluate(projection, checked_frame, trusted_frame=True)
            if not initial.get("eligible", False):
                reason = initial.get("reason", PropositionEligibilityReason.REVALIDATION_UNAVAILABLE).value
                exclusion_counts[reason] = exclusion_counts.get(reason, 0) + 1
                continue
            decision = self.internal_eligibility_evaluator.revalidate(
                projection, checked_frame, self.internal_engram.current_proposition_projection, trusted_frame=True
            )
            if decision.get("revalidated", False):
                revalidation_rows += 1
            if not decision.get("eligible", False):
                reason = decision.get("reason", PropositionEligibilityReason.REVALIDATION_UNAVAILABLE).value
                exclusion_counts[reason] = exclusion_counts.get(reason, 0) + 1
                continue
            records.append(proposition_evidence_record(projection, decision, frame, self.name, trusted=True))
        records.sort(key=lambda record: record.get("proposition_id", ""))
        exhausted = set()
        record_sizes = [json_size(trusted_proposition_evidence_record_to_dict(record)) for record in records]
        record_total = sum(record_sizes)

        def record_bytes() -> int:
            result = json_array_bytes(record_total, len(records))
            return result

        while records and record_bytes() > budget.get("max_evidence_bytes", 0):
            records.pop()
            record_total -= record_sizes.pop()
            exhausted.add("evidence_bytes")
        while records and record_bytes() > budget.get("max_output_bytes", 0):
            records.pop()
            record_total -= record_sizes.pop()
            exhausted.add("output_bytes")
        working_memory = json_size([proposition_projection_to_dict(projection) for projection in projections]) + record_bytes()
        if working_memory > budget.get("max_working_memory_bytes", 0):
            result = exhausted_result(self.name, ("working_memory_bytes",))
            return result
        evidence_bytes = record_bytes()
        result = resolver_result(
            resolver=self.name,
            state=ResolverState.COMPLETED,
            reason_code="structured_proposition_evidence" if records else "structured_graph_miss",
            proposition_evidence=tuple(records),
            diagnostics={
                "discovery_rows": len(projections),
                "revalidation_rows": revalidation_rows,
                "exclusion_counts": exclusion_counts,
            },
            consumption=budget_consumption(
                elapsed_ns=max(0, resolver_clock_ns(self.internal_clock_ns) - started),
                resolvers=1,
                graph_rows=composition_rows + len(projections) + revalidation_rows,
                evidence=len(records),
                evidence_bytes=evidence_bytes,
                output_bytes=evidence_bytes,
                working_memory_bytes=working_memory,
                exhausted_dimensions=tuple(sorted(exhausted)),
            ),
        )
        run_cooperative_check(cooperative_check)
        return result


class SupportSemanticResolver:
    """Pure fixed-vector adapter for support candidates and full Proposition evidence."""

    def __init__(self, engram, clock_ns: object, eligibility_evaluator: object = ()) -> None:
        self.name = SUPPORT_SEMANTIC_RESOLVER_NAME
        self.cost_class = SUPPORT_SEMANTIC_RESOLVER_COST_CLASS
        self.internal_engram = engram
        self.internal_clock_ns = clock_ns
        if eligibility_evaluator == ():
            selected_evaluator = PropositionEligibilityEvaluator(getattr(engram, "proposition_visibility_authority", ()))
        elif isinstance(eligibility_evaluator, PropositionEligibilityEvaluator):
            selected_evaluator = eligibility_evaluator
        else:
            raise InvalidRequestError("support semantic eligibility_evaluator must be PropositionEligibilityEvaluator")
        self.internal_eligibility_evaluator: PropositionEligibilityEvaluator = selected_evaluator

    # The resolver protocol passes the request frame to every availability check; this resolver's readiness does not depend on it.
    def available(self, frame: dict) -> bool:  # pylint: disable=unused-argument
        graph = self.internal_engram.config.get("graph", {})
        client = self.internal_engram.graph_client
        result = bool(
            graph.get("enabled", False)
            and graph.get("vector_enabled", False)
            and client
            and getattr(client, "available", True)
            and self.internal_engram.graph_embedding_model
        )
        return result

    def resolve(
        self,
        frame: dict,
        budget: dict,
        cooperative_check: object = (),
    ) -> dict:
        """Resolve semantic candidates while honoring caller cancellation."""
        run_cooperative_check(cooperative_check)
        proposition_capacity = bool(
            budget.get("max_graph_rows", 0)
            and budget.get("max_evidence", 0)
            and budget.get("max_evidence_bytes", 0)
            and budget.get("max_output_bytes", 0)
        )
        if (
            not budget.get("max_vector_results", 0)
            or not budget.get("max_working_memory_bytes", 0)
            or (not budget.get("max_candidates", 0) and not proposition_capacity)
        ):
            dimensions = tuple(
                name
                for name, value in (
                    ("candidates", budget.get("max_candidates", 0)),
                    ("evidence", budget.get("max_evidence", 0)),
                    ("evidence_bytes", budget.get("max_evidence_bytes", 0)),
                    ("graph_rows", budget.get("max_graph_rows", 0)),
                    ("output_bytes", budget.get("max_output_bytes", 0)),
                    ("vector_results", budget.get("max_vector_results", 0)),
                    ("working_memory_bytes", budget.get("max_working_memory_bytes", 0)),
                )
                if not value
            )
            result = exhausted_result(self.name, dimensions)
            return result
        started = resolver_clock_ns(self.internal_clock_ns)

        def artifact_filter(artifact: dict[str, object]) -> bool:
            result = artifact_matches_frame(artifact, frame)
            return result

        try:
            candidate_limit = min(budget.get("max_candidates", 0), budget.get("max_vector_results", 0))
            run_cooperative_check(cooperative_check)
            graph_rows = (
                self.internal_engram.graph_vector_propositions(
                    frame.get("resolved_text", ""),
                    limit=candidate_limit,
                    evaluation_time=frame.get("eligibility_context", {}).get("evaluation_time", ""),
                )[:candidate_limit]
                if candidate_limit
                else []
            )
            run_cooperative_check(cooperative_check)
            matches = (
                self.internal_engram.vector_supported_proposition_match_components(
                    graph_rows,
                    limit=candidate_limit,
                    artifact_filter=artifact_filter,
                    cooperative_check=cooperative_check,
                    max_working_memory_bytes=budget.get("max_working_memory_bytes", 0),
                )
                if candidate_limit
                else []
            )
            remaining_vector_results = max(0, budget.get("max_vector_results", 0) - len(graph_rows))
            projections = (
                self.internal_engram.graph_vector_proposition_projections(
                    frame.get("resolved_text", ""),
                    limit=remaining_vector_results,
                    cooperative_check=cooperative_check,
                    max_working_memory_bytes=budget.get("max_working_memory_bytes", 0),
                    basis_window=assertion_basis_window(frame),
                )
                if remaining_vector_results
                else []
            )
        except WorkingMemoryExceededError:
            result = exhausted_result(self.name, ("working_memory_bytes",))
            return result
        except VectorSupportScanExceededError:
            result = exhausted_result(self.name, ("vector_results",))
            return result
        candidates = []
        accounting = []
        evidence_count = 0
        for match in matches:
            run_cooperative_check(cooperative_check)
            artifact = match.get("artifact", {})
            statement_id = artifact.get("statement_id", "")
            support_references = artifact.get("support_references", ())
            references = tuple(
                evidence_reference(
                    evidence_id=reference.get("id", ""),
                    resolver=self.name,
                    kind=EvidenceKind.SUPPORT,
                    scope=frame.get("scope", {}),
                    provenance={"support_linked": True},
                    diagnostics={"semantic_match": True},
                )
                for reference in support_references[: max(0, budget.get("max_evidence", 0) - evidence_count)]
            )
            evidence_count += len(references)
            candidate = resolution_candidate(
                candidate_id=internal_candidate_id(CandidateSource.SUPPORT_SEMANTIC, statement_id, frame.get("diagnostic_id", "")),
                statement_id=statement_id,
                response=artifact.get("response", ""),
                source=CandidateSource.SUPPORT_SEMANTIC,
                features=feature_set(
                    values={
                        "retrieval_score": float(match.get("retrieval_score", 0.0)),
                        "priority": float(match.get("priority", 0.0)),
                        "semantic_score": float(match.get("semantic_similarity", 0.0)),
                        "support_coverage": float(bool(references)),
                        "vector_weight": float(match.get("vector_weight", 0.0)),
                    },
                    unavailable=("lexical_score",),
                ),
                evidence=references,
                scope=artifact.get("scope", {}),
                lifecycle=artifact.get("lifecycle", LifecycleState.RETIRED),
                provenance={
                    "generation": artifact.get("generation", 0),
                    "source_label": artifact.get("provenance", {}).get("source_label", ""),
                },
                diagnostics={"support_count": len(support_references)},
            )
            candidates.append(candidate)
            accounting.append(accounting_observation(candidate.get("statement_id", "")))
        records = []
        exclusion_counts: dict[str, int] = {}
        revalidation_attempts = 0
        revalidation_rows = 0
        exhausted = set()
        remaining_evidence = max(0, budget.get("max_evidence", 0) - evidence_count)
        if proposition_capacity and remaining_evidence:
            checked_frame = validate_query_frame(frame)
            for projection in projections:
                run_cooperative_check(cooperative_check)
                initial = self.internal_eligibility_evaluator.evaluate(projection, checked_frame, trusted_frame=True)
                if not initial.get("eligible", False):
                    reason = initial.get("reason", PropositionEligibilityReason.REVALIDATION_UNAVAILABLE).value
                    exclusion_counts[reason] = exclusion_counts.get(reason, 0) + 1
                    continue
                if revalidation_attempts >= budget.get("max_graph_rows", 0):
                    exhausted.add("graph_rows")
                    break
                revalidation_attempts += 1
                decision = self.internal_eligibility_evaluator.revalidate(
                    projection, checked_frame, self.internal_engram.current_proposition_projection, trusted_frame=True
                )
                if decision.get("revalidated", False):
                    revalidation_rows += 1
                if not decision.get("eligible", False):
                    reason = decision.get("reason", PropositionEligibilityReason.REVALIDATION_UNAVAILABLE).value
                    exclusion_counts[reason] = exclusion_counts.get(reason, 0) + 1
                    continue
                records.append(proposition_evidence_record(projection, decision, frame, self.name, trusted=True))
                if len(records) >= remaining_evidence:
                    break
        records.sort(key=lambda record: record.get("proposition_id", ""))

        # Candidates are fixed while records are trimmed, so their sizes are
        # computed once and each record's size is subtracted as it goes.
        reference_sizes = [
            json_size(evidence_reference_to_dict(reference))
            for candidate in candidates
            for reference in candidate.get("evidence", ())
        ]
        candidate_sizes = [json_size(candidate_to_dict(candidate)) for candidate in candidates]
        record_sizes = [json_size(trusted_proposition_evidence_record_to_dict(record)) for record in records]
        record_total = sum(record_sizes)

        def evidence_bytes() -> int:
            result = json_array_bytes(sum(reference_sizes) + record_total, len(reference_sizes) + len(records))
            return result

        def output_bytes() -> int:
            result = json_array_bytes(sum(candidate_sizes) + record_total, len(candidate_sizes) + len(records))
            return result

        while records and evidence_bytes() > budget.get("max_evidence_bytes", 0):
            records.pop()
            record_total -= record_sizes.pop()
            exhausted.add("evidence_bytes")
        while records and output_bytes() > budget.get("max_output_bytes", 0):
            records.pop()
            record_total -= record_sizes.pop()
            exhausted.add("output_bytes")
        fixed_working_memory = (
            working_size(graph_rows) + working_size(projections) + working_size(matches) + working_size(candidates)
        )
        # working_size counts a shared object once, in list order, so each
        # record's share is fixed by the records before it and dropping from
        # the end subtracts exactly that share.
        record_visited = {id(records)}
        record_shares = [working_size(record, record_visited) for record in records]
        record_working = 64 + sum(record_shares)
        working_memory = fixed_working_memory + record_working
        while records and working_memory > budget.get("max_working_memory_bytes", 0):
            records.pop()
            record_working -= record_shares.pop()
            exhausted.add("working_memory_bytes")
            working_memory = fixed_working_memory + record_working
        if working_memory > budget.get("max_working_memory_bytes", 0):
            result = exhausted_result(self.name, ("working_memory_bytes",))
            return result
        selected_reason = "support_semantic_candidates" if candidates else "semantic_proposition_evidence"
        if not candidates and not records:
            selected_reason = "support_semantic_miss"
        result = resolver_result(
            resolver=self.name,
            state=ResolverState.COMPLETED,
            reason_code=selected_reason,
            candidates=tuple(candidates),
            proposition_evidence=tuple(records),
            accounting=tuple(accounting),
            diagnostics={
                "projection_rows": len(projections),
                "revalidation_attempts": revalidation_attempts,
                "revalidation_rows": revalidation_rows,
                "exclusion_counts": exclusion_counts,
            },
            consumption=budget_consumption(
                elapsed_ns=max(0, resolver_clock_ns(self.internal_clock_ns) - started),
                resolvers=1,
                candidates=len(candidates),
                graph_rows=revalidation_attempts,
                vector_results=len(graph_rows) + len(projections),
                evidence=evidence_count + len(records),
                evidence_bytes=evidence_bytes(),
                output_bytes=output_bytes(),
                working_memory_bytes=working_memory,
                exhausted_dimensions=tuple(sorted(exhausted)),
            ),
        )
        run_cooperative_check(cooperative_check)
        return result
