"""Section 5 fused ANSWER/EVIDENCE/MISS orchestration, cancellation, and complete output-budget conformance."""

from pytest import mark as pytest_mark, raises as pytest_raises

from engram.artifacts import validate_cached_response_artifact
from engram.constants_fusion import FusionPolicyReason
from engram.constants_identity import EMPTY_SCOPE_KEY
from engram.constants_resolution import (
    ACCOUNTING_FINALIZATION_FIELDS,
    RESOLUTION_RESULT_FIELDS,
    CandidateSource,
    EvidenceKind,
    ResolutionOutcome,
    ResolverState,
)
from engram.coordination import AtomicMutationCoordinator
from engram.core import Engram
from engram.errors import InvalidRequestError, ResolutionCancelledError
from engram.fusion import CandidateFusionEngine, permissive_candidate_authority
from engram.identity import extract_standalone_identity
from engram.repository import ArtifactRepository, tier_admission_policy
from engram.resolution import (
    accounting_observation,
    evidence_reference,
    resolution_result_to_json,
    resolver_result,
    validate_candidate,
)
from engram.resolution_accounting import ResolutionAccountingFinalizer
from engram.resolution_budget import capture_resolution_budget, validate_resolution_budget
from engram.resolution_evidence import (
    feature_set,
)
from engram.resolution_frame import QueryFrameBuilder, query_frame_with_changes
from engram.resolution_orchestrator import ResolutionOrchestrator
from engram.resolver_execution import ResolverExecutor
from engram.resolver_plan import (
    ResolverRegistry,
)
from engram.responses import AcceptedResponseService
from tests.resolver_fixtures import (
    ACCEPTED_ARTIFACT,
    NOW,
    SPARSE_CANDIDATE,
    START_NS,
    FakeResolver,
)
from tests.support_fixtures import PROPOSITION_REFERENCE_A


def test_orchestrator_cancellation_after_execution_prevents_accounting_publication() -> None:
    engine = Engram()
    query_frame = QueryFrameBuilder(engine, lambda: START_NS, lambda: NOW).build(
        "What is Engram?", EMPTY_SCOPE_KEY, diagnostic_seed="test:What is Engram?:"
    )
    resolver = FakeResolver("structured_graph", resolver_result("structured_graph", ResolverState.COMPLETED))
    finalizer = ResolutionAccountingFinalizer(
        engine,
        AcceptedResponseService(
            AtomicMutationCoordinator(engine.response_repository, engine.mutation_receipts),
            tier_admission_policy(engine.config.get("capacity", 1)),
        ),
    )
    orchestrator = ResolutionOrchestrator(
        ResolverRegistry((resolver,)),
        ResolverExecutor(lambda: START_NS),
        finalizer,
    )
    checks = 0

    def cancel_after_execution() -> None:
        nonlocal checks
        checks += 1
        if checks == 4:
            raise ResolutionCancelledError("cancel after resolver execution")

    with pytest_raises(ResolutionCancelledError, match="after resolver execution"):
        orchestrator.resolve(
            query_frame,
            "cancel-after-execution",
            cooperative_check=cancel_after_execution,
        )

    assert resolver.calls == 1
    assert finalizer.internal_requests == {}
    assert engine.query_count == 0


def test_orchestration_returns_evidence_for_non_exact_and_miss_for_no_output() -> None:
    engine = Engram()
    query_frame = QueryFrameBuilder(engine, lambda: START_NS, lambda: NOW).build(
        "What is Engram?", EMPTY_SCOPE_KEY, diagnostic_seed="test:What is Engram?:"
    )
    sparse = FakeResolver(
        "sparse",
        resolver_result(
            "sparse",
            ResolverState.COMPLETED,
            candidates=(SPARSE_CANDIDATE,),
        ),
    )
    evidence_accounting = ResolutionAccountingFinalizer(
        engine,
        AcceptedResponseService(
            AtomicMutationCoordinator(engine.response_repository, engine.mutation_receipts),
            tier_admission_policy(engine.config.get("capacity", 1)),
        ),
    )
    evidence_orchestrator = ResolutionOrchestrator(
        ResolverRegistry((sparse,)),
        ResolverExecutor(lambda: START_NS),
        evidence_accounting,
        CandidateFusionEngine(authority=permissive_candidate_authority),
    )
    evidence_result, _ = evidence_orchestrator.resolve(query_frame, "request-evidence")
    empty = FakeResolver("empty", resolver_result("empty", ResolverState.COMPLETED))
    miss_orchestrator = ResolutionOrchestrator(
        ResolverRegistry((empty,)),
        ResolverExecutor(lambda: START_NS),
        ResolutionAccountingFinalizer(
            engine,
            AcceptedResponseService(
                AtomicMutationCoordinator(engine.response_repository, engine.mutation_receipts),
                tier_admission_policy(engine.config.get("capacity", 1)),
            ),
        ),
    )
    miss_result, _ = miss_orchestrator.resolve(query_frame, "request-miss")
    response_candidates = evidence_result.get("response_candidates", ())

    assert set(evidence_result) == RESOLUTION_RESULT_FIELDS
    assert set(miss_result) == RESOLUTION_RESULT_FIELDS
    assert evidence_result.get("outcome", ResolutionOutcome.MISS) == ResolutionOutcome.EVIDENCE
    assert evidence_result.get("selected_candidate_available", False) is False
    assert tuple(value.get("statement_id", "") for value in response_candidates) == ("stmt-candidate",)
    assert response_candidates[0].get("features", {}).get("values", {}).get("lexical", 0.0) == 1.0
    assert miss_result.get("outcome", ResolutionOutcome.MISS) == ResolutionOutcome.MISS
    assert miss_result.get("response_candidates", ()) == ()
    assert miss_result.get("evidence", ()) == ()


def test_fused_non_exact_answer_is_fail_soft_and_accounted_once_without_implicit_acceptance() -> None:
    statement_id = "stmt-accepted"
    engine = Engram()
    engine.response_repository = ArtifactRepository(
        (
            validate_cached_response_artifact(
                {
                    **ACCEPTED_ARTIFACT,
                    "response": "Candidate response",
                    "query_identity": extract_standalone_identity("What is Engram?", EMPTY_SCOPE_KEY),
                    "scope": EMPTY_SCOPE_KEY,
                    "support_references": (PROPOSITION_REFERENCE_A,),
                }
            ),
        )
    )
    reference = evidence_reference(PROPOSITION_REFERENCE_A.get("id", ""), "semantic", EvidenceKind.SUPPORT, EMPTY_SCOPE_KEY)
    sparse_candidate = validate_candidate(
        {
            **SPARSE_CANDIDATE,
            "candidate_id": f"candidate:sparse:{statement_id}",
            "statement_id": statement_id,
            "features": feature_set(values={"sparse_score": 0.95}),
        }
    )
    semantic_candidate = validate_candidate(
        {
            **SPARSE_CANDIDATE,
            "candidate_id": f"candidate:support_semantic:{statement_id}",
            "statement_id": statement_id,
            "source": CandidateSource.SUPPORT_SEMANTIC,
            "evidence": (reference,),
            "features": feature_set(values={"semantic_score": 0.92, "support_coverage": 1.0}),
        }
    )
    observation = accounting_observation(statement_id, ("candidate",))
    failed = FakeResolver("failed", resolver_result("failed", ResolverState.COMPLETED), error=True)
    sparse = FakeResolver(
        "sparse",
        resolver_result(
            "sparse",
            ResolverState.COMPLETED,
            candidates=(sparse_candidate,),
            accounting=(observation,),
        ),
    )
    semantic = FakeResolver(
        "semantic",
        resolver_result(
            "semantic",
            ResolverState.COMPLETED,
            candidates=(semantic_candidate,),
            accounting=(observation,),
        ),
    )
    orchestrator = ResolutionOrchestrator(
        ResolverRegistry((failed, sparse, semantic)),
        ResolverExecutor(lambda: START_NS),
        ResolutionAccountingFinalizer(
            engine,
            AcceptedResponseService(
                AtomicMutationCoordinator(engine.response_repository, engine.mutation_receipts),
                tier_admission_policy(engine.config.get("capacity", 1)),
            ),
        ),
    )
    query_frame = QueryFrameBuilder(engine, lambda: START_NS, lambda: NOW).build(
        "What is Engram?", EMPTY_SCOPE_KEY, diagnostic_seed="test:What is Engram?:"
    )

    result, first = orchestrator.resolve(query_frame, "request-fused", accept_exact=True)
    replay_result, replay = orchestrator.resolve(query_frame, "request-fused", accept_exact=True)

    assert set(first) == ACCOUNTING_FINALIZATION_FIELDS
    assert result.get("outcome", ResolutionOutcome.MISS) == ResolutionOutcome.ANSWER
    assert replay_result.get("outcome", ResolutionOutcome.MISS) == ResolutionOutcome.ANSWER
    assert replay_result.get("selected_candidate", {}).get("statement_id", "") == statement_id
    assert [value.get("state", ResolverState.FAILED) for value in result.get("resolver_results", ())] == [
        ResolverState.FAILED,
        ResolverState.COMPLETED,
        ResolverState.COMPLETED,
    ]
    assert first.get("candidate_statement_ids", ()) == (statement_id,)
    assert first.get("accepted_statement_id", "") == ""
    assert first.get("success_applied", False) is False
    assert replay.get("idempotent", False) is True
    statistics = engine.response_repository.get_artifact(statement_id).get("statistics", {})
    assert "hit_count" in statistics
    assert statistics.get("query_count", 0) == 1
    assert statistics.get("hit_count", 0) == 0
    assert engine.get_statement(statement_id) == {}


def test_orchestrator_reserves_remaining_memory_and_reports_fusion_consumption() -> None:
    engine = Engram()
    query_frame = QueryFrameBuilder(engine, lambda: START_NS, lambda: NOW).build(
        "What is Engram?", EMPTY_SCOPE_KEY, diagnostic_seed="test:What is Engram?:"
    )
    value = validate_candidate({**SPARSE_CANDIDATE, "features": feature_set(values={"sparse_score": 0.9})})
    raw = resolver_result(
        "sparse",
        ResolverState.COMPLETED,
        candidates=(value,),
    )
    executor = ResolverExecutor(lambda: START_NS)
    probe_resolver = FakeResolver("sparse", raw)
    probe_execution = executor.execute(query_frame, ResolverRegistry((probe_resolver,)).plan(query_frame))
    fusion = CandidateFusionEngine(authority=permissive_candidate_authority)
    fusion_required = fusion.decide(query_frame, (value,)).get("working_memory_bytes", 0)
    probe_memory_bytes = probe_execution.get("consumption", {}).get("working_memory_bytes", 0)
    total_limit = probe_memory_bytes + fusion_required - 1
    constrained_frame = query_frame_with_changes(
        query_frame,
        {"budget": validate_resolution_budget({**query_frame.get("budget", {}), "max_working_memory_bytes": total_limit})},
    )
    orchestrator = ResolutionOrchestrator(
        ResolverRegistry((FakeResolver("sparse", raw),)),
        executor,
        ResolutionAccountingFinalizer(
            engine,
            AcceptedResponseService(
                AtomicMutationCoordinator(engine.response_repository, engine.mutation_receipts),
                tier_admission_policy(engine.config.get("capacity", 1)),
            ),
        ),
        fusion,
    )

    result, finalization = orchestrator.resolve(constrained_frame, "request-fusion-memory")
    budget = result.get("budget", {})

    assert set(result) == RESOLUTION_RESULT_FIELDS
    assert set(finalization) == ACCOUNTING_FINALIZATION_FIELDS
    assert result.get("outcome", ResolutionOutcome.MISS) == ResolutionOutcome.MISS
    assert FusionPolicyReason.FUSION_MEMORY_EXHAUSTED.value in result.get("reason_codes", ())
    assert "working_memory_bytes" in budget.get("exhausted_dimensions", ())
    assert budget.get("working_memory_bytes", 0) == total_limit
    assert finalization.get("success_applied", False) is False


def test_complete_result_serialization_obeys_and_reports_output_budget() -> None:
    engine = Engram()
    selected_budget = capture_resolution_budget(
        lambda: START_NS,
        max_output_bytes=4_096,
    )
    query_frame = QueryFrameBuilder(engine, lambda: START_NS, lambda: NOW).build(
        "What is Engram?", EMPTY_SCOPE_KEY, diagnostic_seed="test:What is Engram?:", budget=selected_budget
    )
    empty = FakeResolver("empty", resolver_result("empty", ResolverState.COMPLETED))
    orchestrator = ResolutionOrchestrator(
        ResolverRegistry((empty,)),
        ResolverExecutor(lambda: START_NS),
        ResolutionAccountingFinalizer(
            engine,
            AcceptedResponseService(
                AtomicMutationCoordinator(engine.response_repository, engine.mutation_receipts),
                tier_admission_policy(engine.config.get("capacity", 1)),
            ),
        ),
    )

    result, _ = orchestrator.resolve(query_frame, "request-output-envelope")
    encoded_size = len(resolution_result_to_json(result).encode("utf-8"))

    assert encoded_size <= query_frame.get("budget", {}).get("max_output_bytes", 0)
    assert result.get("budget", {}).get("output_bytes", 0) == encoded_size


def test_complete_result_truncates_variable_payload_to_output_budget() -> None:
    engine = Engram()
    selected_budget = capture_resolution_budget(
        lambda: START_NS,
        max_output_bytes=4_096,
    )
    query_frame = QueryFrameBuilder(engine, lambda: START_NS, lambda: NOW).build(
        "What is Engram?", EMPTY_SCOPE_KEY, diagnostic_seed="test:What is Engram?:", budget=selected_budget
    )
    large_candidate = validate_candidate({**SPARSE_CANDIDATE, "response": "x" * 2_500})
    resolver = FakeResolver(
        "large",
        resolver_result(
            "large",
            ResolverState.COMPLETED,
            candidates=(large_candidate,),
        ),
    )
    orchestrator = ResolutionOrchestrator(
        ResolverRegistry((resolver,)),
        ResolverExecutor(lambda: START_NS),
        ResolutionAccountingFinalizer(
            engine,
            AcceptedResponseService(
                AtomicMutationCoordinator(engine.response_repository, engine.mutation_receipts),
                tier_admission_policy(engine.config.get("capacity", 1)),
            ),
        ),
        CandidateFusionEngine(authority=permissive_candidate_authority),
    )

    result, _ = orchestrator.resolve(query_frame, "request-output-payload")
    encoded_size = len(resolution_result_to_json(result).encode("utf-8"))
    budget = result.get("budget", {})

    assert encoded_size <= query_frame.get("budget", {}).get("max_output_bytes", 0)
    assert budget.get("output_bytes", 0) == encoded_size
    assert "output_truncated" in result.get("reason_codes", ())
    assert "output_bytes" in budget.get("exhausted_dimensions", ())


@pytest_mark.parametrize("max_output_bytes", (4_096, 8_192))
def test_resolution_never_fails_after_accounting_at_the_output_boundary(max_output_bytes: int) -> None:
    statement_id = "stmt-candidate"
    engine = Engram()
    engine.response_repository = ArtifactRepository(
        (
            validate_cached_response_artifact(
                {
                    **ACCEPTED_ARTIFACT,
                    "statement_id": statement_id,
                    "query_identity": extract_standalone_identity("What is Engram?", EMPTY_SCOPE_KEY),
                    "scope": EMPTY_SCOPE_KEY,
                }
            ),
        )
    )
    finalizer = ResolutionAccountingFinalizer(
        engine,
        AcceptedResponseService(
            AtomicMutationCoordinator(engine.response_repository, engine.mutation_receipts),
            tier_admission_policy(engine.config.get("capacity", 1)),
        ),
    )
    selected_budget = capture_resolution_budget(lambda: START_NS, max_output_bytes=max_output_bytes)
    query_frame = QueryFrameBuilder(engine, lambda: START_NS, lambda: NOW).build(
        "What is Engram?", EMPTY_SCOPE_KEY, diagnostic_seed="test:What is Engram?:", budget=selected_budget
    )
    request_ids = iter(range(1_000_000))

    def resolve(response_bytes: int) -> dict:
        found = validate_candidate({**SPARSE_CANDIDATE, "response": "x" * response_bytes})
        resolver = FakeResolver(
            "large",
            resolver_result(
                "large",
                ResolverState.COMPLETED,
                candidates=(found,),
                accounting=(accounting_observation(statement_id),),
            ),
        )
        orchestrator = ResolutionOrchestrator(
            ResolverRegistry((resolver,)),
            ResolverExecutor(lambda: START_NS),
            finalizer,
            CandidateFusionEngine(authority=permissive_candidate_authority),
        )
        result, _ = orchestrator.resolve(query_frame, f"request-boundary-{next(request_ids)}")
        return result

    def check(response_bytes: int) -> tuple:
        """Assert R5 for one size and return the result's shape."""
        before = engine.response_repository.get_artifact(statement_id)
        try:
            result = resolve(response_bytes)
        except InvalidRequestError:
            assert engine.response_repository.get_artifact(statement_id) == before
            return ("failed",)
        encoded_size = len(resolution_result_to_json(result).encode("utf-8"))
        assert encoded_size <= max_output_bytes
        assert set(result) == RESOLUTION_RESULT_FIELDS
        assert result.get("budget", {}).get("output_bytes", 0) == encoded_size
        shape = (
            result.get("reason_codes", ()),
            len(result.get("resolver_results", ())),
            len(result.get("response_candidates", ())),
        )
        return shape

    # Growth after the write shows up where one trimming step stops being enough, so
    # scan coarsely and check every size between two scanned sizes whose shapes differ.
    step = 16
    shapes = {response_bytes: check(response_bytes) for response_bytes in range(1, max_output_bytes + 1, step)}
    scanned = sorted(shapes)
    for previous, current in zip(scanned, scanned[1:], strict=False):
        if shapes.get(previous, ()) != shapes.get(current, ()):
            for response_bytes in range(previous + 1, current):
                check(response_bytes)
