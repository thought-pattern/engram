"""Pure resolver adapters, bounded execution, accounting, and baseline policy."""

import hashlib
import json
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import datetime
from types import MappingProxyType
from typing import Protocol, cast

from engram.artifacts import CachedResponseArtifact, LifecycleState
from engram.eligibility import EpochEligibilityPolicy
from engram.errors import ConflictError, InvalidRequestError, ResourceNotFoundError
from engram.fusion import CandidateFusionEngine, EngramCandidateAuthority
from engram.identity import ScopedRetrievalKey, ScopeKey
from engram.indexes import ExactLookupOutcome
from engram.models import record_statement_hit, record_statement_query
from engram.resolution import (
    EMPTY_CANDIDATE,
    AccountingObservation,
    BudgetConsumption,
    BudgetLedger,
    Candidate,
    CandidateSource,
    CostClass,
    EvidenceKind,
    EvidenceReference,
    FeatureSet,
    QueryFrame,
    ResolutionOutcome,
    ResolutionResult,
    ResolverResult,
    ResolverState,
)

MAX_PLAN_RESOLVERS = 64
RESOLVER_BUDGET_SCHEMA_VERSION = 1
RESOLVER_RESERVATION_SCHEMA_VERSION = 1


class Resolver(Protocol):
    """Side-effect-free resolver interface."""

    name: str
    cost_class: CostClass

    def available(self, frame: QueryFrame) -> bool: ...

    def resolve(self, frame: QueryFrame, budget: "ResolverBudget") -> ResolverResult: ...


@dataclass(frozen=True, slots=True)
class ResolverBudget:
    """Read-only bounded lease passed to one resolver."""

    deadline_ns: int
    max_candidates: int
    max_graph_rows: int
    max_vector_results: int
    max_evidence: int
    max_evidence_bytes: int
    max_output_bytes: int
    max_diagnostic_bytes: int
    max_working_memory_bytes: int
    schema_version: int = RESOLVER_BUDGET_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != RESOLVER_BUDGET_SCHEMA_VERSION:
            raise InvalidRequestError(f"unsupported resolver budget schema_version: {self.schema_version}")
        for name in (
            "deadline_ns",
            "max_candidates",
            "max_graph_rows",
            "max_vector_results",
            "max_evidence",
            "max_evidence_bytes",
            "max_output_bytes",
            "max_diagnostic_bytes",
            "max_working_memory_bytes",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise InvalidRequestError(f"resolver budget {name} must be a nonnegative integer")

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "deadline_ns": self.deadline_ns,
            "max_candidates": self.max_candidates,
            "max_graph_rows": self.max_graph_rows,
            "max_vector_results": self.max_vector_results,
            "max_evidence": self.max_evidence,
            "max_evidence_bytes": self.max_evidence_bytes,
            "max_output_bytes": self.max_output_bytes,
            "max_diagnostic_bytes": self.max_diagnostic_bytes,
            "max_working_memory_bytes": self.max_working_memory_bytes,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"))

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> "ResolverBudget":
        if not isinstance(value, Mapping):
            raise InvalidRequestError("ResolverBudget must be an object")
        expected = frozenset(cls(0, 0, 0, 0, 0, 0, 0, 0, 0).to_dict())
        if frozenset(value) != expected:
            raise InvalidRequestError("ResolverBudget has invalid fields")
        values = {}
        for name in expected:
            raw = value[name]
            if isinstance(raw, bool) or not isinstance(raw, int) or raw < 0:
                raise InvalidRequestError(f"ResolverBudget {name} must be a nonnegative integer")
            values[name] = raw
        return cls(**values)

    @classmethod
    def from_json(cls, value: str) -> "ResolverBudget":
        try:
            decoded = json.loads(value)
        except (TypeError, json.JSONDecodeError) as error:
            raise InvalidRequestError("ResolverBudget JSON must be valid JSON") from error
        return cls.from_dict(decoded)


@dataclass(frozen=True, slots=True)
class ResolverReservation:
    """Versioned inspectable lease and resulting consumption for one invocation."""

    resolver: str
    order: int
    lease: ResolverBudget
    consumption: BudgetConsumption
    schema_version: int = RESOLVER_RESERVATION_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != RESOLVER_RESERVATION_SCHEMA_VERSION:
            raise InvalidRequestError(f"unsupported resolver reservation schema_version: {self.schema_version}")
        if not isinstance(self.resolver, str) or not self.resolver or len(self.resolver.encode("utf-8")) > 96:
            raise InvalidRequestError("reservation resolver must be a bounded non-empty string")
        if isinstance(self.order, bool) or not isinstance(self.order, int) or not 0 <= self.order < MAX_PLAN_RESOLVERS:
            raise InvalidRequestError("reservation order is out of bounds")
        if not isinstance(self.lease, ResolverBudget) or not isinstance(self.consumption, BudgetConsumption):
            raise InvalidRequestError("reservation lease and consumption have invalid types")

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "resolver": self.resolver,
            "order": self.order,
            "lease": self.lease.to_dict(),
            "consumption": self.consumption.to_dict(),
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"))

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> "ResolverReservation":
        expected = frozenset({"schema_version", "resolver", "order", "lease", "consumption"})
        if not isinstance(value, Mapping) or frozenset(value) != expected:
            raise InvalidRequestError("ResolverReservation has invalid fields")
        schema = value["schema_version"]
        order = value["order"]
        if isinstance(schema, bool) or not isinstance(schema, int):
            raise InvalidRequestError("ResolverReservation schema_version must be an integer")
        if isinstance(order, bool) or not isinstance(order, int):
            raise InvalidRequestError("ResolverReservation order must be an integer")
        if not isinstance(value["resolver"], str):
            raise InvalidRequestError("ResolverReservation resolver must be a string")
        if not isinstance(value["lease"], Mapping) or not isinstance(value["consumption"], Mapping):
            raise InvalidRequestError("ResolverReservation nested records must be objects")
        return cls(
            schema_version=schema,
            resolver=value["resolver"],
            order=order,
            lease=ResolverBudget.from_dict(value["lease"]),
            consumption=BudgetConsumption.from_dict(value["consumption"]),
        )

    @classmethod
    def from_json(cls, value: str) -> "ResolverReservation":
        try:
            decoded = json.loads(value)
        except (TypeError, json.JSONDecodeError) as error:
            raise InvalidRequestError("ResolverReservation JSON must be valid JSON") from error
        return cls.from_dict(decoded)


@dataclass(frozen=True, slots=True)
class ResolutionPlanEntry:
    """One inspectable configured resolver decision."""

    resolver: Resolver
    order: int
    configured: bool
    available: bool
    reason_code: str

    def __post_init__(self) -> None:
        if not hasattr(self.resolver, "name") or not hasattr(self.resolver, "cost_class"):
            raise InvalidRequestError("plan resolver must implement Resolver")
        if isinstance(self.order, bool) or not isinstance(self.order, int) or self.order < 0:
            raise InvalidRequestError("plan order must be a nonnegative integer")
        if not isinstance(self.configured, bool) or not isinstance(self.available, bool):
            raise InvalidRequestError("plan configured and available must be booleans")
        if not isinstance(self.reason_code, str):
            raise InvalidRequestError("plan reason_code must be a string")

    def to_dict(self) -> dict[str, object]:
        return {
            "resolver": self.resolver.name,
            "cost_class": self.resolver.cost_class.value,
            "order": self.order,
            "configured": self.configured,
            "available": self.available,
            "reason_code": self.reason_code,
        }


@dataclass(frozen=True, slots=True)
class ResolutionPlan:
    """Deterministic plan retaining configured skip and availability decisions."""

    entries: tuple[ResolutionPlanEntry, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.entries, tuple) or not all(isinstance(value, ResolutionPlanEntry) for value in self.entries):
            raise InvalidRequestError("plan entries must be a tuple of ResolutionPlanEntry values")
        if len(self.entries) > MAX_PLAN_RESOLVERS:
            raise InvalidRequestError(f"plan entries exceed the limit of {MAX_PLAN_RESOLVERS}")
        if tuple(value.order for value in self.entries) != tuple(range(len(self.entries))):
            raise InvalidRequestError("plan entry order must be contiguous")
        names = tuple(value.resolver.name for value in self.entries)
        if len(set(names)) != len(names):
            raise InvalidRequestError("plan resolver names must be unique")

    def to_dict(self) -> dict[str, object]:
        return {"entries": [entry.to_dict() for entry in self.entries]}


def _json_size(value: object) -> int:
    return len(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8"))


def _candidate_id(source: CandidateSource, statement_id: str, diagnostic_id: str) -> str:
    digest = hashlib.sha256(f"{diagnostic_id}:{source.value}:{statement_id}".encode()).hexdigest()
    return f"candidate:sha256:{digest}"


def _evidence_id(row: Mapping[str, object]) -> tuple[str, bool]:
    claim_id = row.get("claim_id", "")
    if isinstance(claim_id, str) and claim_id:
        return claim_id, False
    bounded = {
        "subject": str(row.get("subject", ""))[:512],
        "predicate": str(row.get("predicate", ""))[:512],
        "object": str(row.get("object", ""))[:512],
    }
    digest = hashlib.sha256(json.dumps(bounded, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()
    return f"legacy-graph:sha256:{digest}", True


def _statement_metadata(statement: Mapping[str, object]) -> tuple[Mapping[str, object], ScopeKey, LifecycleState]:
    template = statement.get("template", {})
    if not isinstance(template, Mapping):
        return MappingProxyType({}), ScopeKey(), LifecycleState.ACTIVE
    tapestry = template.get("tapestry", {})
    if not isinstance(tapestry, Mapping):
        tapestry = MappingProxyType({})
    metadata = tapestry.get("metadata", {})
    if not isinstance(metadata, Mapping):
        metadata = MappingProxyType({})
    namespace = tapestry.get("namespace", "")
    context_fingerprint = tapestry.get("context_fingerprint", "")
    if not isinstance(namespace, str) or not isinstance(context_fingerprint, str):
        return metadata, ScopeKey(), LifecycleState.ACTIVE
    response_artifact = template.get("response_artifact", {})
    lifecycle = LifecycleState.ACTIVE
    if isinstance(response_artifact, Mapping):
        lifecycle_value = response_artifact.get("lifecycle", "ACTIVE")
        if isinstance(lifecycle_value, str):
            try:
                lifecycle = LifecycleState(lifecycle_value)
            except ValueError:
                lifecycle = LifecycleState.RETIRED
    try:
        scope = ScopeKey(namespace=namespace, context_fingerprint=context_fingerprint)
    except InvalidRequestError:
        scope = ScopeKey()
    return metadata, scope, lifecycle


def _matches_frame(statement: Mapping[str, object], frame: QueryFrame, *, allow_pattern: bool) -> bool:
    if bool(statement.get("pattern", "")) != allow_pattern:
        return False
    metadata, statement_scope, lifecycle = _statement_metadata(statement)
    if statement_scope != ScopeKey() and statement_scope != frame.scope:
        return False
    if lifecycle != LifecycleState.ACTIVE:
        return False
    source_label = statement.get("source_label", "")
    if frame.required_source_label and source_label != frame.required_source_label:
        return False
    return all(key in metadata and metadata[key] == value for key, value in frame.required_metadata.items())


def _artifact_matches_frame(artifact: CachedResponseArtifact, frame: QueryFrame) -> bool:
    if artifact.scope != frame.scope or artifact.lifecycle != LifecycleState.ACTIVE:
        return False
    if frame.required_source_label and artifact.provenance.source_label != frame.required_source_label:
        return False
    return all(key in artifact.metadata and artifact.metadata[key] == value for key, value in frame.required_metadata.items())


def _legacy_candidate(
    statement: Mapping[str, object],
    frame: QueryFrame,
    source: CandidateSource,
    features: FeatureSet,
    evidence: tuple[EvidenceReference, ...] = (),
    diagnostics: Mapping[str, object] = MappingProxyType({}),
    response: str = "",
) -> Candidate:
    statement_id = str(statement["id"])
    metadata, statement_scope, lifecycle = _statement_metadata(statement)
    selected_scope = frame.scope if statement_scope == ScopeKey() else statement_scope
    selected_response = response or str(statement["text"])
    return Candidate(
        candidate_id=_candidate_id(source, statement_id, frame.diagnostic_id),
        statement_id=statement_id,
        response=selected_response,
        source=source,
        features=features,
        evidence=evidence,
        scope=selected_scope,
        lifecycle=lifecycle,
        provenance={
            "source_label": str(statement.get("source_label", "")),
            "introduced_by_user_id": str(statement.get("introduced_by_user_id", "")),
            "metadata_present": bool(metadata),
        },
        diagnostics=diagnostics,
    )


def _exhausted_result(name: str, dimensions: tuple[str, ...]) -> ResolverResult:
    return ResolverResult(
        resolver=name,
        state=ResolverState.EXHAUSTED,
        reason_code=f"{dimensions[0]}_budget",
        consumption=BudgetConsumption(resolvers=1, exhausted_dimensions=dimensions),
    )


def _cooperative_deadline_check(clock_ns: Callable[[], int], budget: ResolverBudget) -> None:
    """Raise at resolver cooperation points after the executor-owned deadline."""
    if budget.deadline_ns and clock_ns() >= budget.deadline_ns:
        raise TimeoutError("resolver deadline exhausted")


def _resource_exhausted_result(name: str, error: Exception) -> ResolverResult:
    dimension = "resolver_time" if isinstance(error, TimeoutError) else "working_memory_bytes"
    return _exhausted_result(name, (dimension,))


class ExactResolver:
    """Adapter over the Section 3 contextual exact repository."""

    name = "exact"
    cost_class = CostClass.EXACT

    def __init__(self, engram, clock_ns: Callable[[], int]) -> None:
        self._engram = engram
        self._clock_ns = clock_ns

    def available(self, frame: QueryFrame) -> bool:
        return frame.eligibility_context.artifact_repository_available

    def resolve(self, frame: QueryFrame, budget: ResolverBudget) -> ResolverResult:
        if not budget.max_candidates or not budget.max_working_memory_bytes:
            dimension = "candidates" if not budget.max_candidates else "working_memory_bytes"
            return _exhausted_result(self.name, (dimension,))
        started = self._clock_ns()
        try:
            _cooperative_deadline_check(self._clock_ns, budget)
        except TimeoutError as error:
            return _resource_exhausted_result(self.name, error)
        key = ScopedRetrievalKey.build(frame.scope, frame.resolved_text)
        contextual = self._engram.response_repository.exact_lookup(
            key,
            frame.eligibility_context,
            EpochEligibilityPolicy.MATCH_WHEN_ARTIFACT_AVAILABLE,
        )
        try:
            _cooperative_deadline_check(self._clock_ns, budget)
        except TimeoutError as error:
            return _resource_exhausted_result(self.name, error)
        lookup = contextual.lookup
        if lookup.outcome != ExactLookupOutcome.FOUND:
            return ResolverResult(
                resolver=self.name,
                state=ResolverState.COMPLETED,
                reason_code=f"exact_{lookup.outcome.value}",
                diagnostics={
                    "owner_count": len(lookup.owner_statement_ids),
                    "truncated": lookup.truncated,
                    "index_refreshed": contextual.index_refreshed,
                },
                consumption=BudgetConsumption(elapsed_ns=max(0, self._clock_ns() - started), resolvers=1),
            )
        artifact = self._engram.response_repository.get_artifact(lookup.statement_id)
        if not _artifact_matches_frame(artifact, frame):
            return ResolverResult(
                resolver=self.name,
                state=ResolverState.COMPLETED,
                reason_code="exact_required_filter_excluded",
                consumption=BudgetConsumption(elapsed_ns=max(0, self._clock_ns() - started), resolvers=1),
            )
        candidate = Candidate(
            candidate_id=_candidate_id(CandidateSource.EXACT, artifact.statement_id, frame.diagnostic_id),
            statement_id=artifact.statement_id,
            response=artifact.response,
            source=CandidateSource.EXACT,
            features=FeatureSet(values={"exact_match": 1.0}, unavailable=()),
            evidence=tuple(
                EvidenceReference(
                    evidence_id=claim_id,
                    resolver=self.name,
                    kind=EvidenceKind.SUPPORT,
                    scope=artifact.scope,
                    provenance={"support_linked": True},
                )
                for claim_id in artifact.support_claim_ids[: budget.max_evidence]
            ),
            scope=artifact.scope,
            lifecycle=artifact.lifecycle,
            provenance={
                "retrieval_origin": lookup.provenance,
                "representation": lookup.representation,
                "generation": artifact.generation,
                "source_label": artifact.provenance.source_label,
            },
            diagnostics={"context_signature": contextual.context_signature},
        )
        if _json_size(candidate.to_dict()) > budget.max_working_memory_bytes:
            return _exhausted_result(self.name, ("working_memory_bytes",))
        return ResolverResult(
            resolver=self.name,
            state=ResolverState.COMPLETED,
            reason_code="exact_found",
            candidates=(candidate,),
            accounting=(AccountingObservation(artifact.statement_id),),
            consumption=BudgetConsumption(
                elapsed_ns=max(0, self._clock_ns() - started),
                resolvers=1,
                candidates=1,
                evidence=len(candidate.evidence),
            ),
        )


class PatternResolver:
    """Pure adapter over statement-backed AIML matching."""

    name = "pattern"
    cost_class = CostClass.CHEAP

    def __init__(self, engram, clock_ns: Callable[[], int]) -> None:
        self._engram = engram
        self._clock_ns = clock_ns

    def available(self, frame: QueryFrame) -> bool:
        return bool(len(self._engram.pattern_matcher))

    def resolve(self, frame: QueryFrame, budget: ResolverBudget) -> ResolverResult:
        if not budget.max_candidates or not budget.max_working_memory_bytes:
            dimension = "candidates" if not budget.max_candidates else "working_memory_bytes"
            return _exhausted_result(self.name, (dimension,))
        started = self._clock_ns()
        try:
            discoveries = self._engram.pattern_candidates(
                frame.resolved_text,
                limit=budget.max_candidates,
                cooperative_check=lambda: _cooperative_deadline_check(self._clock_ns, budget),
                max_working_memory_bytes=budget.max_working_memory_bytes,
            )
        except (TimeoutError, MemoryError) as error:
            return _resource_exhausted_result(self.name, error)
        candidates = []
        accounting = []
        for discovery in discoveries:
            try:
                _cooperative_deadline_check(self._clock_ns, budget)
            except TimeoutError as error:
                return _resource_exhausted_result(self.name, error)
            statement = discovery["statement"]
            if not _matches_frame(statement, frame, allow_pattern=True):
                continue
            pattern = str(discovery["pattern"])
            words = pattern.split()
            wildcard_count = sum(1 for word in words if word.lstrip("$") in {"*", "_", "#", "^"})
            specificity = float(max(0, len(words) - wildcard_count))
            candidate = _legacy_candidate(
                statement,
                frame,
                CandidateSource.PATTERN,
                FeatureSet(values={"pattern_specificity": specificity}, unavailable=()),
                diagnostics={
                    "pattern": pattern,
                    "captures": discovery["captured"],
                    "that_captures": discovery["thatstars"],
                    "topic_captures": discovery["topicstars"],
                    "that": discovery["that"],
                    "topic": discovery["topic"],
                },
                response=str(discovery["response"]),
            )
            candidates.append(candidate)
            accounting.append(AccountingObservation(candidate.statement_id))
            if len(candidates) >= budget.max_candidates:
                break
        return ResolverResult(
            resolver=self.name,
            state=ResolverState.COMPLETED,
            reason_code="pattern_candidates" if candidates else "pattern_miss",
            candidates=tuple(candidates),
            accounting=tuple(accounting),
            consumption=BudgetConsumption(elapsed_ns=max(0, self._clock_ns() - started), resolvers=1, candidates=len(candidates)),
        )


class LexicalResolver:
    """Pure adapter over existing lexical scoring and diagnostics."""

    name = "lexical"
    cost_class = CostClass.CHEAP

    def __init__(self, engram, clock_ns: Callable[[], int]) -> None:
        self._engram = engram
        self._clock_ns = clock_ns

    def available(self, frame: QueryFrame) -> bool:
        return bool(self._engram.keywords)

    def resolve(self, frame: QueryFrame, budget: ResolverBudget) -> ResolverResult:
        if not budget.max_candidates or not budget.max_working_memory_bytes:
            dimension = "candidates" if not budget.max_candidates else "working_memory_bytes"
            return _exhausted_result(self.name, (dimension,))
        started = self._clock_ns()
        try:
            discovered = self._engram.query_candidates(
                frame.resolved_text,
                limit=budget.max_candidates,
                statement_filter=lambda statement: _matches_frame(statement, frame, allow_pattern=False),
                reference_time=datetime.fromisoformat(frame.eligibility_context.evaluation_time.replace("Z", "+00:00")),
                cooperative_check=lambda: _cooperative_deadline_check(self._clock_ns, budget),
                max_working_memory_bytes=budget.max_working_memory_bytes,
            )
        except (TimeoutError, MemoryError) as error:
            return _resource_exhausted_result(self.name, error)
        keywords = tuple(dict.fromkeys(str(keyword) for keyword in discovered["keywords"]))
        discovery_features = discovered["features"]
        discovery_diagnostics = discovered["diagnostics"]
        candidates = tuple(
            _legacy_candidate(
                statement,
                frame,
                CandidateSource.LEXICAL,
                FeatureSet(
                    values={
                        "exact_match_ratio": discovery_features[statement["id"]]["exact_match_ratio"],
                        "keyword_hit_rate": discovery_features[statement["id"]]["keyword_hit_rate"],
                        "lexical_overlap": discovery_features[statement["id"]]["overlap"],
                        "lexical_score": float(score),
                        "phrase_keywords_enabled": float(discovery_diagnostics["phrase_keywords_enabled"]),
                        "priority": discovery_features[statement["id"]]["priority"],
                        "recency": discovery_features[statement["id"]]["recency"],
                        "spelling_correction_applied": float(discovery_diagnostics["spelling_correction_applied"]),
                        "synonym_match_ratio": discovery_features[statement["id"]]["synonym_match_ratio"],
                    },
                    unavailable=("lemma_match_ratio", "stem_match_ratio"),
                ),
                diagnostics={
                    "keywords": list(keywords),
                    "synonym_expansion_count": discovery_diagnostics["synonym_expansion_count"],
                },
            )
            for statement, score in discovered["matches"]
        )
        return ResolverResult(
            resolver=self.name,
            state=ResolverState.COMPLETED,
            reason_code="lexical_candidates" if candidates else "lexical_miss",
            candidates=candidates,
            accounting=tuple(AccountingObservation(candidate.statement_id, keywords) for candidate in candidates),
            diagnostics={"keywords": list(keywords)},
            consumption=BudgetConsumption(
                elapsed_ns=max(0, self._clock_ns() - started),
                resolvers=1,
                candidates=len(candidates),
                working_memory_bytes=int(discovery_diagnostics["working_memory_bytes"]),
            ),
        )


class StructuredGraphResolver:
    """Pure minimal-evidence adapter over the existing read-only graph path."""

    name = "structured_graph"
    cost_class = CostClass.STANDARD

    def __init__(self, engram, clock_ns: Callable[[], int]) -> None:
        self._engram = engram
        self._clock_ns = clock_ns

    def available(self, frame: QueryFrame) -> bool:
        return bool(self._engram.graph_client)

    def resolve(self, frame: QueryFrame, budget: ResolverBudget) -> ResolverResult:
        if not budget.max_graph_rows or not budget.max_evidence or not budget.max_working_memory_bytes:
            dimensions = tuple(
                name
                for name, value in (
                    ("evidence", budget.max_evidence),
                    ("graph_rows", budget.max_graph_rows),
                    ("working_memory_bytes", budget.max_working_memory_bytes),
                )
                if not value
            )
            return _exhausted_result(self.name, dimensions)
        started = self._clock_ns()
        try:
            rows = self._engram.structured_graph_evidence(
                frame.resolved_text,
                row_limit=min(budget.max_graph_rows, budget.max_evidence),
                cooperative_check=lambda: _cooperative_deadline_check(self._clock_ns, budget),
                max_working_memory_bytes=budget.max_working_memory_bytes,
            )
        except (TimeoutError, MemoryError) as error:
            return _resource_exhausted_result(self.name, error)
        evidence = []
        seen = set()
        for row in rows:
            evidence_id, synthesized = _evidence_id(row)
            if evidence_id in seen:
                continue
            seen.add(evidence_id)
            evidence.append(
                EvidenceReference(
                    evidence_id=evidence_id,
                    resolver=self.name,
                    kind=EvidenceKind.GRAPH_FACT,
                    scope=frame.scope,
                    provenance={"legacy_identifier_synthesized": synthesized},
                    diagnostics={"structured_match": True},
                )
            )
        return ResolverResult(
            resolver=self.name,
            state=ResolverState.COMPLETED,
            reason_code="structured_graph_evidence" if evidence else "structured_graph_miss",
            evidence=tuple(evidence),
            diagnostics={"rows": len(rows)},
            consumption=BudgetConsumption(
                elapsed_ns=max(0, self._clock_ns() - started),
                resolvers=1,
                graph_rows=len(rows),
                evidence=len(evidence),
            ),
        )


class SupportSemanticResolver:
    """Pure adapter over fixed Claim-vector support-to-artifact intersection."""

    name = "support_semantic"
    cost_class = CostClass.EXPENSIVE

    def __init__(self, engram, clock_ns: Callable[[], int]) -> None:
        self._engram = engram
        self._clock_ns = clock_ns

    def available(self, frame: QueryFrame) -> bool:
        graph = self._engram.config.get("graph") or {}
        return bool(graph.get("enabled") and graph.get("vector_enabled"))

    def resolve(self, frame: QueryFrame, budget: ResolverBudget) -> ResolverResult:
        if not budget.max_candidates or not budget.max_vector_results or not budget.max_working_memory_bytes:
            dimensions = tuple(
                name
                for name, value in (
                    ("candidates", budget.max_candidates),
                    ("vector_results", budget.max_vector_results),
                    ("working_memory_bytes", budget.max_working_memory_bytes),
                )
                if not value
            )
            return _exhausted_result(self.name, dimensions)
        started = self._clock_ns()

        def statement_filter(statement: Mapping[str, object]) -> bool:
            statement_id = str(statement.get("id", ""))
            try:
                artifact = self._engram.response_repository.get_artifact(statement_id)
            except ResourceNotFoundError:
                return False
            return _artifact_matches_frame(artifact, frame)

        try:
            matches = self._engram.vector_supported_match_components(
                frame.resolved_text,
                limit=min(budget.max_candidates, budget.max_vector_results),
                statement_filter=statement_filter,
                cooperative_check=lambda: _cooperative_deadline_check(self._clock_ns, budget),
                max_working_memory_bytes=budget.max_working_memory_bytes,
            )
        except (TimeoutError, MemoryError) as error:
            return _resource_exhausted_result(self.name, error)
        candidates = []
        accounting = []
        evidence_count = 0
        for match in matches:
            statement = match["statement"]
            artifact = self._engram.response_repository.get_artifact(str(statement["id"]))
            references = tuple(
                EvidenceReference(
                    evidence_id=claim_id,
                    resolver=self.name,
                    kind=EvidenceKind.SUPPORT,
                    scope=frame.scope,
                    provenance={"support_linked": True},
                    diagnostics={"semantic_match": True},
                )
                for claim_id in artifact.support_claim_ids[: max(0, budget.max_evidence - evidence_count)]
            )
            evidence_count += len(references)
            candidate = Candidate(
                candidate_id=_candidate_id(CandidateSource.SUPPORT_SEMANTIC, artifact.statement_id, frame.diagnostic_id),
                statement_id=artifact.statement_id,
                response=artifact.response,
                source=CandidateSource.SUPPORT_SEMANTIC,
                features=FeatureSet(
                    values={
                        "legacy_retrieval_score": float(match["retrieval_score"]),
                        "priority": float(match["priority"]),
                        "semantic_score": float(match["semantic_similarity"]),
                        "support_coverage": float(bool(references)),
                        "vector_weight": float(match["vector_weight"]),
                    },
                    unavailable=("lexical_score",),
                ),
                evidence=references,
                scope=artifact.scope,
                lifecycle=artifact.lifecycle,
                provenance={"generation": artifact.generation, "source_label": artifact.provenance.source_label},
                diagnostics={"support_count": len(artifact.support_claim_ids)},
            )
            candidates.append(candidate)
            accounting.append(AccountingObservation(candidate.statement_id))
        return ResolverResult(
            resolver=self.name,
            state=ResolverState.COMPLETED,
            reason_code="support_semantic_candidates" if candidates else "support_semantic_miss",
            candidates=tuple(candidates),
            accounting=tuple(accounting),
            consumption=BudgetConsumption(
                elapsed_ns=max(0, self._clock_ns() - started),
                resolvers=1,
                candidates=len(candidates),
                vector_results=len(matches),
                evidence=evidence_count,
            ),
        )


class ResolverRegistry:
    """Allow-listed deterministic resolver registry."""

    def __init__(self, resolvers: tuple[Resolver, ...]) -> None:
        if not isinstance(resolvers, tuple):
            raise InvalidRequestError("resolvers must be a tuple")
        if len(resolvers) > MAX_PLAN_RESOLVERS:
            raise InvalidRequestError(f"resolvers exceed the limit of {MAX_PLAN_RESOLVERS}")
        names = []
        for resolver in resolvers:
            if not hasattr(resolver, "name") or not isinstance(resolver.name, str) or not resolver.name:
                raise InvalidRequestError("every resolver must have a non-empty name")
            if not isinstance(resolver.cost_class, CostClass):
                raise InvalidRequestError("every resolver must have a CostClass")
            names.append(resolver.name)
        if len(set(names)) != len(names):
            raise InvalidRequestError("resolver names must be unique")
        self._resolvers = resolvers

    def plan(self, frame: QueryFrame, configured_names: tuple[str, ...] = ()) -> ResolutionPlan:
        if not isinstance(frame, QueryFrame):
            raise InvalidRequestError("plan frame must be a QueryFrame")
        if not isinstance(configured_names, tuple):
            raise InvalidRequestError("configured_names must be a tuple")
        if not all(isinstance(name, str) and name for name in configured_names):
            raise InvalidRequestError("configured_names must contain non-empty strings")
        if len(set(configured_names)) != len(configured_names):
            raise InvalidRequestError("configured_names must not contain duplicates")
        configured = set(configured_names) if configured_names else {resolver.name for resolver in self._resolvers}
        unknown = configured.difference(resolver.name for resolver in self._resolvers)
        if unknown:
            raise InvalidRequestError(f"unknown configured resolvers: {sorted(unknown)}")
        entries = []
        for order, resolver in enumerate(self._resolvers):
            selected = resolver.name in configured
            cost_allowed = resolver.cost_class in frame.budget.allowed_cost_classes
            availability_failed = False
            try:
                available = bool(selected and cost_allowed and resolver.available(frame))
            except Exception:
                available = False
                availability_failed = True
            reason = ""
            if not selected:
                reason = "not_configured"
            elif not cost_allowed:
                reason = "cost_class_disabled"
            elif availability_failed:
                reason = "availability_check_failed"
            elif not available:
                reason = "dependency_unavailable"
            entries.append(ResolutionPlanEntry(resolver, order, selected, available, reason))
        return ResolutionPlan(tuple(entries))


@dataclass(frozen=True, slots=True)
class ExecutionReport:
    """Raw executor output consumed by baseline orchestration."""

    results: tuple[ResolverResult, ...]
    consumption: BudgetConsumption
    exact_short_circuited: bool
    reservations: tuple[ResolverReservation, ...]


class ResolverExecutor:
    """Sequential bounded fail-soft executor for one deterministic plan."""

    def __init__(self, clock_ns: Callable[[], int]) -> None:
        if not callable(clock_ns):
            raise InvalidRequestError("executor clock_ns must be callable")
        self._clock_ns = clock_ns

    @property
    def clock_ns(self) -> Callable[[], int]:
        """Expose the injected request clock to post-execution policy stages."""
        return self._clock_ns

    def _lease(self, frame: QueryFrame, ledger: BudgetLedger) -> ResolverBudget:
        consumed = ledger.snapshot()
        started_ns = self._clock_ns()
        resolver_deadline_ns = started_ns + frame.budget.resolver_time_ms * 1_000_000
        deadline_ns = min(frame.budget.deadline_ns, resolver_deadline_ns) if frame.budget.deadline_ns else resolver_deadline_ns
        return ResolverBudget(
            deadline_ns=deadline_ns,
            max_candidates=ledger.remaining_candidates(),
            max_graph_rows=max(0, frame.budget.max_graph_rows - consumed.graph_rows),
            max_vector_results=max(0, frame.budget.max_vector_results - consumed.vector_results),
            max_evidence=ledger.remaining_evidence(),
            max_evidence_bytes=max(0, frame.budget.max_evidence_bytes - consumed.evidence_bytes),
            max_output_bytes=max(0, frame.budget.max_output_bytes - consumed.output_bytes),
            max_diagnostic_bytes=max(0, frame.budget.max_diagnostic_bytes - consumed.diagnostic_bytes),
            max_working_memory_bytes=max(0, frame.budget.max_working_memory_bytes - consumed.working_memory_bytes),
        )

    @staticmethod
    def _bounded_result(result: ResolverResult, lease: ResolverBudget) -> ResolverResult:
        candidates = list(result.candidates[: lease.max_candidates])
        evidence = list(result.evidence)
        exhausted = set(result.consumption.exhausted_dimensions)
        if len(candidates) < len(result.candidates):
            exhausted.add("candidates")
        remaining_evidence = lease.max_evidence
        for index, candidate in enumerate(candidates):
            retained = candidate.evidence[:remaining_evidence]
            if len(retained) < len(candidate.evidence):
                exhausted.add("evidence")
            if retained != candidate.evidence:
                candidates[index] = replace(candidate, evidence=retained)
            remaining_evidence -= len(retained)
        retained_evidence = evidence[:remaining_evidence]
        if len(retained_evidence) < len(evidence):
            exhausted.add("evidence")
        evidence = retained_evidence

        def evidence_values() -> list[dict[str, object]]:
            return [reference.to_dict() for candidate in candidates for reference in candidate.evidence] + [
                reference.to_dict() for reference in evidence
            ]

        def values_size(values: list[dict[str, object]]) -> int:
            return _json_size(values) if values else 0

        while values_size(evidence_values()) > lease.max_evidence_bytes:
            if evidence:
                evidence.pop()
            else:
                candidate_index = next(
                    (index for index in range(len(candidates) - 1, -1, -1) if candidates[index].evidence),
                    -1,
                )
                if candidate_index < 0:
                    break
                candidate = candidates[candidate_index]
                candidates[candidate_index] = replace(candidate, evidence=candidate.evidence[:-1])
            exhausted.add("evidence_bytes")

        def output_size() -> int:
            values = [candidate.to_dict() for candidate in candidates] + [reference.to_dict() for reference in evidence]
            return values_size(values)

        while output_size() > lease.max_output_bytes and (candidates or evidence):
            if evidence:
                evidence.pop()
            else:
                candidates.pop()
            exhausted.add("output_bytes")
        diagnostics = result.diagnostics
        if diagnostics and _json_size(dict(diagnostics)) > lease.max_diagnostic_bytes:
            marker = MappingProxyType({"truncated": True})
            diagnostics = marker if _json_size(dict(marker)) <= lease.max_diagnostic_bytes else MappingProxyType({})
            exhausted.add("diagnostic_bytes")
        accounting_ids = {candidate.statement_id for candidate in candidates}
        accounting = tuple(value for value in result.accounting if value.statement_id in accounting_ids)
        candidate_bytes = values_size([value.to_dict() for value in candidates])
        evidence_bytes = values_size(evidence_values())
        diagnostic_bytes = _json_size(dict(diagnostics)) if diagnostics else 0
        graph_rows = min(result.consumption.graph_rows, lease.max_graph_rows)
        vector_results = min(result.consumption.vector_results, lease.max_vector_results)
        if graph_rows < result.consumption.graph_rows:
            exhausted.add("graph_rows")
        if vector_results < result.consumption.vector_results:
            exhausted.add("vector_results")
        estimated_memory = max(
            candidate_bytes + values_size([value.to_dict() for value in evidence]) + diagnostic_bytes,
            result.consumption.working_memory_bytes,
        )
        if estimated_memory > lease.max_working_memory_bytes:
            exhausted.add("working_memory_bytes")
            candidates = []
            evidence = []
            accounting = ()
            diagnostics = MappingProxyType({})
            candidate_bytes = 0
            evidence_bytes = 0
            diagnostic_bytes = 0
        working_memory = min(estimated_memory, lease.max_working_memory_bytes)
        consumption = BudgetConsumption(
            elapsed_ns=result.consumption.elapsed_ns,
            resolvers=1,
            candidates=len(candidates),
            graph_rows=graph_rows,
            vector_results=vector_results,
            evidence=len(evidence) + sum(len(candidate.evidence) for candidate in candidates),
            evidence_bytes=evidence_bytes,
            output_bytes=output_size(),
            diagnostic_bytes=diagnostic_bytes,
            working_memory_bytes=working_memory,
            exhausted_dimensions=tuple(sorted(exhausted)),
            measurement_available=result.consumption.measurement_available,
        )
        return ResolverResult(
            resolver=result.resolver,
            state=result.state,
            reason_code=result.reason_code,
            candidates=tuple(candidates),
            evidence=tuple(evidence),
            accounting=accounting,
            diagnostics=diagnostics,
            consumption=consumption,
        )

    def execute(self, frame: QueryFrame, plan: ResolutionPlan) -> ExecutionReport:
        if not isinstance(frame, QueryFrame) or not isinstance(plan, ResolutionPlan):
            raise InvalidRequestError("executor requires a QueryFrame and ResolutionPlan")
        ledger = BudgetLedger(frame.budget, self._clock_ns)
        results = []
        reservations = []
        exact_short_circuited = False
        for entry in plan.entries:
            if ledger.deadline_exhausted():
                exhausted = ResolverResult(
                    resolver=entry.resolver.name,
                    state=ResolverState.EXHAUSTED,
                    reason_code="total_deadline",
                    consumption=BudgetConsumption(exhausted_dimensions=("total_time",)),
                )
                results.append(exhausted)
                ledger.add(exhausted.consumption)
                break
            if ledger.snapshot().resolvers >= frame.budget.max_resolvers:
                exhausted = ResolverResult(
                    resolver=entry.resolver.name,
                    state=ResolverState.EXHAUSTED,
                    reason_code="resolver_budget",
                    consumption=BudgetConsumption(exhausted_dimensions=("resolvers",)),
                )
                results.append(exhausted)
                ledger.add(exhausted.consumption)
                break
            if not entry.configured:
                results.append(
                    ResolverResult(resolver=entry.resolver.name, state=ResolverState.SKIPPED, reason_code=entry.reason_code)
                )
                continue
            if not entry.available:
                results.append(
                    ResolverResult(resolver=entry.resolver.name, state=ResolverState.UNAVAILABLE, reason_code=entry.reason_code)
                )
                continue
            lease = self._lease(frame, ledger)
            if not lease.max_candidates and entry.resolver.name != "structured_graph":
                results.append(
                    ResolverResult(resolver=entry.resolver.name, state=ResolverState.EXHAUSTED, reason_code="candidate_budget")
                )
                continue
            started = self._clock_ns()
            try:
                raw = entry.resolver.resolve(frame, lease)
            except Exception as error:
                finished = self._clock_ns()
                elapsed = max(0, finished - started)
                if lease.deadline_ns and finished >= lease.deadline_ns:
                    total_expired = bool(frame.budget.deadline_ns and finished >= frame.budget.deadline_ns)
                    dimension = "total_time" if total_expired else "resolver_time"
                    result = ResolverResult(
                        resolver=entry.resolver.name,
                        state=ResolverState.EXHAUSTED,
                        reason_code="total_deadline" if total_expired else "resolver_deadline",
                        consumption=BudgetConsumption(
                            elapsed_ns=elapsed,
                            resolvers=1,
                            exhausted_dimensions=(dimension,),
                        ),
                    )
                else:
                    result = ResolverResult(
                        resolver=entry.resolver.name,
                        state=ResolverState.FAILED,
                        reason_code="resolver_exception",
                        diagnostics={"exception_type": type(error).__name__},
                        consumption=BudgetConsumption(elapsed_ns=elapsed, resolvers=1),
                    )
            else:
                finished = self._clock_ns()
                elapsed = max(0, finished - started)
                if lease.deadline_ns and finished >= lease.deadline_ns:
                    total_expired = bool(frame.budget.deadline_ns and finished >= frame.budget.deadline_ns)
                    dimension = "total_time" if total_expired else "resolver_time"
                    result = ResolverResult(
                        resolver=entry.resolver.name,
                        state=ResolverState.EXHAUSTED,
                        reason_code="total_deadline" if total_expired else "resolver_deadline",
                        consumption=BudgetConsumption(
                            elapsed_ns=elapsed,
                            resolvers=1,
                            exhausted_dimensions=(dimension,),
                        ),
                    )
                else:
                    raw = replace(raw, consumption=replace(raw.consumption, elapsed_ns=elapsed))
                    result = self._bounded_result(raw, lease)
            results.append(result)
            ledger.add(result.consumption)
            reservations.append(ResolverReservation(entry.resolver.name, entry.order, lease, result.consumption))
            if (
                entry.resolver.name == "exact"
                and result.state == ResolverState.COMPLETED
                and len(result.candidates) == 1
                and result.candidates[0].source == CandidateSource.EXACT
            ):
                exact_short_circuited = True
                break
        return ExecutionReport(tuple(results), ledger.snapshot(), exact_short_circuited, tuple(reservations))


@dataclass(frozen=True, slots=True)
class AccountingFinalization:
    """Inspectably records which unique observations were applied."""

    candidate_statement_ids: tuple[str, ...]
    accepted_statement_id: str
    candidacy_applied: bool
    success_applied: bool
    idempotent: bool

    def to_dict(self) -> dict[str, object]:
        visible_ids = self.candidate_statement_ids[:64]
        return {
            "candidate_statement_ids": list(visible_ids),
            "omitted_candidate_statement_id_count": len(self.candidate_statement_ids) - len(visible_ids),
            "accepted_statement_id": self.accepted_statement_id,
            "candidacy_applied": self.candidacy_applied,
            "success_applied": self.success_applied,
            "idempotent": self.idempotent,
        }


class ResolutionAccountingFinalizer:
    """Exactly-once boundary for aggregate candidacy and accepted success."""

    def __init__(self, engram, accepted_response_service=(), compatibility_sync=(), max_requests: int = 1_000) -> None:
        self._engram = engram
        self._accepted_response_service = accepted_response_service
        if compatibility_sync and not callable(compatibility_sync):
            raise InvalidRequestError("compatibility_sync must be callable")
        if isinstance(max_requests, bool) or not isinstance(max_requests, int) or max_requests < 1:
            raise InvalidRequestError("accounting max_requests must be a positive integer")
        self._compatibility_sync = compatibility_sync
        self._max_requests = max_requests
        self._lock = threading.RLock()
        self._requests: dict[str, tuple[str, AccountingFinalization]] = {}

    @property
    def candidate_authority(self) -> EngramCandidateAuthority:
        """Return current-state authority bound to the same Engram as accounting."""
        return EngramCandidateAuthority(self._engram)

    @staticmethod
    def _signature(results: tuple[ResolverResult, ...], accepted_statement_id: str) -> str:
        stable_results = []
        for result in results:
            value = result.to_dict()
            consumption = dict(cast(Mapping[str, object], value["consumption"]))
            consumption["elapsed_ns"] = 0
            value["consumption"] = consumption
            stable_results.append(value)
        payload = {
            "accepted_statement_id": accepted_statement_id,
            "results": stable_results,
        }
        return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()

    @staticmethod
    def _receipt_id(kind: str, request_id: str) -> str:
        digest = hashlib.sha256(request_id.encode("utf-8")).hexdigest()
        return f"resolution:{kind}:sha256:{digest}"

    def discard(self, request_id: str) -> None:
        """Forget one transient retry record after the owning result cache evicts it."""
        if not isinstance(request_id, str) or not request_id:
            raise InvalidRequestError("accounting request_id must be a non-empty string")
        with self._lock:
            if request_id in self._requests:
                del self._requests[request_id]

    def finalize(
        self,
        request_id: str,
        results: tuple[ResolverResult, ...],
        accepted_statement_id: str = "",
    ) -> AccountingFinalization:
        if not isinstance(request_id, str) or not request_id:
            raise InvalidRequestError("accounting request_id must be a non-empty string")
        if not isinstance(results, tuple) or not all(isinstance(value, ResolverResult) for value in results):
            raise InvalidRequestError("accounting results must be a tuple of ResolverResult values")
        if not isinstance(accepted_statement_id, str):
            raise InvalidRequestError("accepted_statement_id must be a string")
        signature = self._signature(results, accepted_statement_id)
        with self._lock:
            if request_id in self._requests:
                previous_signature, previous = self._requests[request_id]
                if previous_signature != signature:
                    raise ConflictError("accounting request_id is associated with different observations")
                return AccountingFinalization(
                    previous.candidate_statement_ids,
                    previous.accepted_statement_id,
                    previous.candidacy_applied,
                    previous.success_applied,
                    True,
                )
            observations = {}
            keywords = set()
            for result in results:
                for observation in result.accounting:
                    observations.setdefault(observation.statement_id, observation)
                    keywords.update(observation.keywords)
            candidate_ids = tuple(sorted(observations))
            repository_ids = set(self._engram.response_repository.snapshot().artifacts)
            artifact_ids = tuple(statement_id for statement_id in candidate_ids if statement_id in repository_ids)
            legacy_ids = tuple(statement_id for statement_id in candidate_ids if statement_id not in repository_ids)
            artifact_query_replayed = False
            if accepted_statement_id:
                if accepted_statement_id not in observations:
                    raise InvalidRequestError("accepted_statement_id was not an observed candidate")
                if accepted_statement_id not in repository_ids and accepted_statement_id not in self._engram.statement_index:
                    raise InvalidRequestError("accepted statement no longer exists")
            if artifact_ids and not self._accepted_response_service:
                raise InvalidRequestError("accepted-response accounting service is required for artifact candidates")
            accepted_artifact_id = accepted_statement_id if accepted_statement_id in repository_ids else ""
            if artifact_ids:
                accounting_mutation = self._accepted_response_service.finalize_resolution_accounting(
                    artifact_ids,
                    accepted_artifact_id,
                    self._receipt_id("finalize", request_id),
                )
                artifact_query_replayed = accounting_mutation.replayed
                if not accounting_mutation.replayed and self._compatibility_sync:
                    self._compatibility_sync(tuple(sorted(repository_ids)))
            with self._engram.statement_lock:
                for statement_id in legacy_ids:
                    if statement_id in self._engram.statement_index:
                        record_statement_query(self._engram.statements[self._engram.statement_index[statement_id]])
            if legacy_ids or not artifact_query_replayed:
                with self._engram.count_lock:
                    self._engram.query_count += 1
            if legacy_ids or not artifact_query_replayed:
                with self._engram.keyword_lock:
                    for keyword in keywords:
                        if keyword in self._engram.keywords:
                            self._engram.keywords[keyword]["query_count"] += 1
            success_applied = False
            if accepted_statement_id:
                if accepted_statement_id not in repository_ids:
                    with self._engram.statement_lock:
                        record_statement_hit(self._engram.statements[self._engram.statement_index[accepted_statement_id]])
                    with self._engram.count_lock:
                        self._engram.hit_count += 1
                    with self._engram.keyword_lock:
                        for keyword in observations[accepted_statement_id].keywords:
                            if keyword in self._engram.keywords:
                                self._engram.keywords[keyword]["hit_count"] += 1
                elif not artifact_query_replayed:
                    with self._engram.count_lock:
                        self._engram.hit_count += 1
                    with self._engram.keyword_lock:
                        for keyword in observations[accepted_statement_id].keywords:
                            if keyword in self._engram.keywords:
                                self._engram.keywords[keyword]["hit_count"] += 1
                success_applied = True
            finalization = AccountingFinalization(
                candidate_ids,
                accepted_statement_id,
                True,
                success_applied,
                bool(artifact_ids and artifact_query_replayed and not legacy_ids),
            )
            self._requests[request_id] = (signature, finalization)
            while len(self._requests) > self._max_requests:
                self._requests.pop(next(iter(self._requests)))
            return finalization


class ResolutionOrchestrator:
    """Section 5 fused ANSWER/EVIDENCE/MISS orchestration."""

    def __init__(
        self,
        registry: ResolverRegistry,
        executor: ResolverExecutor,
        accounting: ResolutionAccountingFinalizer,
        fusion: object = (),
    ) -> None:
        if not isinstance(registry, ResolverRegistry) or not isinstance(executor, ResolverExecutor):
            raise InvalidRequestError("orchestrator registry and executor have invalid types")
        if not isinstance(accounting, ResolutionAccountingFinalizer):
            raise InvalidRequestError("orchestrator accounting has an invalid type")
        if fusion != () and not isinstance(fusion, CandidateFusionEngine):
            raise InvalidRequestError("orchestrator fusion has an invalid type")
        self._registry = registry
        self._executor = executor
        self._accounting = accounting
        self._clock_ns = executor.clock_ns
        self._fusion = (
            fusion
            if isinstance(fusion, CandidateFusionEngine)
            else CandidateFusionEngine(authority=accounting.candidate_authority, clock_ns=self._clock_ns)
        )

    def resolve(
        self,
        frame: QueryFrame,
        request_id: str,
        configured_names: tuple[str, ...] = (),
        accept_exact: bool = False,
    ) -> tuple[ResolutionResult, AccountingFinalization]:
        if not isinstance(accept_exact, bool):
            raise InvalidRequestError("accept_exact must be a boolean")
        plan = self._registry.plan(frame, configured_names)
        execution = self._executor.execute(frame, plan)
        candidates = []
        evidence = []
        for result in execution.results:
            candidates.extend(result.candidates)
            evidence.extend(result.evidence)
        fusion_memory_limit = max(
            0,
            frame.budget.max_working_memory_bytes - execution.consumption.working_memory_bytes,
        )
        decision = self._fusion.decide(
            frame,
            tuple(candidates),
            tuple(evidence),
            working_memory_limit=fusion_memory_limit,
            working_memory_limit_available=True,
        )
        outcome = decision.outcome
        selected = decision.selected_candidate
        selected_available = decision.selected_candidate_available
        response_candidates = decision.response_candidates
        reason_codes = (*decision.reason_codes, "accounting_finalized")
        confidence = decision.confidence
        confidence_available = decision.confidence_available
        accepted_statement_id = (
            selected.statement_id
            if outcome == ResolutionOutcome.ANSWER and selected.source == CandidateSource.EXACT and accept_exact
            else ""
        )
        preview_ids = tuple(sorted({observation.statement_id for result in execution.results for observation in result.accounting}))
        accounting_preview = AccountingFinalization(
            preview_ids,
            accepted_statement_id,
            False,
            False,
            False,
        )
        frame_diagnostics = {
            "diagnostic_id": frame.diagnostic_id,
            "plan": plan.to_dict(),
            "reservations": [reservation.to_dict() for reservation in execution.reservations],
            "fusion": decision.report,
            "accounting": accounting_preview.to_dict(),
        }
        resolver_results = execution.results
        response_evidence = decision.evidence

        def make_result(consumption: BudgetConsumption) -> ResolutionResult:
            return ResolutionResult(
                outcome=outcome,
                selected_candidate=selected,
                selected_candidate_available=selected_available,
                response_candidates=response_candidates,
                evidence=response_evidence,
                confidence=confidence,
                confidence_available=confidence_available,
                reason_codes=reason_codes,
                frame_diagnostics=frame_diagnostics,
                resolver_results=resolver_results,
                budget=consumption,
            )

        result = make_result(execution.consumption)
        if len(result.to_json().encode("utf-8")) > frame.budget.max_output_bytes:
            reason_codes = (*reason_codes, "output_truncated")
            frame_diagnostics = {
                "diagnostic_id": frame.diagnostic_id,
                "fusion": {
                    "policy_version": self._fusion.policy.policy_version,
                    "candidate_count": decision.report["candidate_count"],
                    "output_truncated": True,
                },
                "accounting": {
                    "candidate_count": len(accounting_preview.candidate_statement_ids),
                    "accepted_present": False,
                    "candidacy_applied": accounting_preview.candidacy_applied,
                    "success_applied": accounting_preview.success_applied,
                    "idempotent": accounting_preview.idempotent,
                },
                "output_truncated": True,
            }
            while (
                resolver_results
                and len(make_result(execution.consumption).to_json().encode("utf-8")) > frame.budget.max_output_bytes
            ):
                resolver_results = resolver_results[:-1]
            while (
                response_evidence
                and len(make_result(execution.consumption).to_json().encode("utf-8")) > frame.budget.max_output_bytes
            ):
                response_evidence = response_evidence[:-1]
            while (
                response_candidates
                and len(make_result(execution.consumption).to_json().encode("utf-8")) > frame.budget.max_output_bytes
            ):
                response_candidates = response_candidates[:-1]
            if outcome == ResolutionOutcome.ANSWER and not response_candidates:
                outcome = ResolutionOutcome.MISS
                selected = EMPTY_CANDIDATE
                selected_available = False
                confidence = 0.0
                confidence_available = False
                accepted_statement_id = ""
                reason_codes = (*reason_codes, "answer_exceeds_output_budget")
            if outcome == ResolutionOutcome.EVIDENCE and not (response_candidates or response_evidence):
                outcome = ResolutionOutcome.MISS
                reason_codes = (*reason_codes, "no_usable_output_after_truncation")

        finalization = self._accounting.finalize(request_id, execution.results, accepted_statement_id)
        if "output_truncated" in reason_codes:
            frame_diagnostics["accounting"] = {
                "candidate_count": len(finalization.candidate_statement_ids),
                "accepted_present": bool(finalization.accepted_statement_id),
                "candidacy_applied": finalization.candidacy_applied,
                "success_applied": finalization.success_applied,
                "idempotent": finalization.idempotent,
            }
        else:
            frame_diagnostics["accounting"] = finalization.to_dict()

        exhausted = set(execution.consumption.exhausted_dimensions)
        if "output_truncated" in reason_codes:
            exhausted.add("output_bytes")
        fusion_exhaustion = decision.report.get("budget_exhausted", "")
        if fusion_exhaustion == "fusion_deadline_exhausted":
            exhausted.add("fusion_deadline")
        if fusion_exhaustion == "fusion_memory_exhausted":
            exhausted.add("working_memory_bytes")
        elapsed_ns = execution.consumption.elapsed_ns
        if frame.budget.started_ns:
            current_ns = self._clock_ns()
            if isinstance(current_ns, bool) or not isinstance(current_ns, int) or current_ns < 0:
                raise InvalidRequestError("orchestrator clock_ns must return a nonnegative integer")
            elapsed_ns = max(elapsed_ns, max(0, current_ns - frame.budget.started_ns))
        consumption = replace(
            execution.consumption,
            elapsed_ns=elapsed_ns,
            output_bytes=0,
            working_memory_bytes=execution.consumption.working_memory_bytes + decision.working_memory_bytes,
            exhausted_dimensions=tuple(sorted(exhausted)),
        )
        for _ in range(4):
            result = make_result(consumption)
            encoded_size = len(result.to_json().encode("utf-8"))
            if consumption.output_bytes == encoded_size:
                break
            consumption = replace(consumption, output_bytes=encoded_size)
        result = make_result(consumption)
        if len(result.to_json().encode("utf-8")) > frame.budget.max_output_bytes:
            raise InvalidRequestError("minimum resolution result exceeds max_output_bytes")
        return result, finalization
