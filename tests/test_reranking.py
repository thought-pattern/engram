"""Section 13 bounded reranker contracts and integration."""

from datetime import UTC, datetime

from pytest import raises as pytest_raises

from engram.config import engram_config, reranker_config
from engram.constants import CandidateSource, LifecycleState
from engram.core import Engram
from engram.errors import InvalidRequestError, ResolutionCancelledError
from engram.fusion import CandidateFusionEngine, permissive_candidate_authority
from engram.identity import scope_key
from engram.reranking import TransparentLogisticReranker
from engram.resolution import QueryFrameBuilder, candidate, capture_resolution_budget, feature_set

# Two-candidate reranker shortlist; rerank validates and copies it, so tests share this read-only constant.
SHORTLIST = (
    {
        "statement_id": "semantic",
        "base_score": 0.70,
        "features": {"base_score": 0.70, "semantic": 0.95, "agreement": 0.5},
    },
    {
        "statement_id": "lexical",
        "base_score": 0.75,
        "features": {"base_score": 0.75, "lexical": 0.75},
    },
)


def test_transparent_logistic_contract_exposes_fixed_features_and_coefficients() -> None:
    reranker = TransparentLogisticReranker(reranker_config(enabled=True))

    result = reranker.rerank(SHORTLIST)
    scores = result.get("scores", [])

    assert result.get("applied", False) is True
    assert result.get("reason", "") == "completed"
    assert [value.get("statement_id", "") for value in scores] == ["semantic", "lexical"]
    assert all("score" in value for value in scores)
    assert scores[0].get("score", 0.0) > scores[1].get("score", 0.0)


def test_reranker_bounds_shortlist_and_input_bytes_with_baseline_fallback() -> None:
    bounded = TransparentLogisticReranker(reranker_config(enabled=True, shortlist_size=1))
    input_limited = TransparentLogisticReranker(reranker_config(enabled=True, max_input_bytes=1))

    shortlist_fallback = bounded.rerank(SHORTLIST)
    fallback = input_limited.rerank(SHORTLIST)

    assert {"applied", "scores"} <= shortlist_fallback.keys()
    assert {"applied", "scores"} <= fallback.keys()
    assert shortlist_fallback.get("applied", False) is False
    assert shortlist_fallback.get("reason", "") == "shortlist_budget"
    assert shortlist_fallback.get("scores", []) == []
    assert fallback.get("applied", False) is False
    assert fallback.get("reason", "") == "input_budget"
    assert fallback.get("scores", []) == []
    assert input_limited.health().get("fallbacks", 0) == 1


def test_reranker_reports_elapsed_target_without_changing_the_result() -> None:
    ticks = iter((0, 2_000_000))
    reranker = TransparentLogisticReranker(
        reranker_config(enabled=True, max_model_time_ms=1),
        clock_ns=lambda: next(ticks),
    )

    result = reranker.rerank(SHORTLIST)

    assert result.get("applied", False) is True
    assert result.get("reason", "") == "completed"
    assert result.get("elapsed_ns", 0) == 2_000_000
    assert result.get("model_time_target_exceeded", False) is True
    assert reranker.health().get("last_reason", "") == "completed"


def test_reranker_propagates_cancellation_and_counts_it() -> None:
    reranker = TransparentLogisticReranker(reranker_config(enabled=True))

    def cancel() -> None:
        raise ResolutionCancelledError("cancelled")

    with pytest_raises(ResolutionCancelledError):
        reranker.rerank(SHORTLIST, cancel)

    assert reranker.health().get("cancellations", 0) == 1


def test_reranker_rejects_malformed_internal_contract_values() -> None:
    reranker = TransparentLogisticReranker(reranker_config(enabled=True))

    with pytest_raises(InvalidRequestError):
        reranker.rerank(({"statement_id": "bad", "base_score": float("nan"), "features": {}},))
    with pytest_raises(InvalidRequestError):
        reranker.rerank(({"statement_id": "", "base_score": 0.5, "features": {}},))


def test_semantic_and_reranker_readiness_are_independent() -> None:
    engine = Engram(config=engram_config(reranker=reranker_config(enabled=True)))

    components = engine.component_status_snapshot()
    semantic = components.get("semantic", {})
    reranker = components.get("reranker", {})

    assert {"enabled", "ready"} <= semantic.keys()
    assert semantic.get("enabled", False) is False
    assert semantic.get("ready", False) is False
    assert reranker.get("enabled", False) is True
    assert reranker.get("ready", False) is True


def test_fusion_applies_reranker_to_bounded_shortlist_and_preserves_provenance() -> None:
    engine = Engram()
    selected_scope = scope_key(namespace="tenant-a")
    frame = QueryFrameBuilder(engine, lambda: 1, lambda: datetime(2026, 8, 22, tzinfo=UTC)).build(
        "exact request",
        selected_scope,
        diagnostic_seed="reranker-fusion",
        budget=capture_resolution_budget(lambda: 1),
    )
    exact = candidate(
        candidate_id="exact-candidate",
        statement_id="exact-statement",
        response="Exact response",
        source=CandidateSource.EXACT,
        features=feature_set({"exact_match": 1.0}),
        evidence=(),
        scope=selected_scope,
        lifecycle=LifecycleState.ACTIVE,
    )
    lexical = candidate(
        candidate_id="sparse-candidate",
        statement_id="sparse-statement",
        response="Sparse response",
        source=CandidateSource.SPARSE,
        features=feature_set({"sparse_score": 0.8}),
        evidence=(),
        scope=selected_scope,
        lifecycle=LifecycleState.ACTIVE,
    )
    semantic = candidate(
        candidate_id="semantic-candidate",
        statement_id="semantic-statement",
        response="Semantic response",
        source=CandidateSource.STANDALONE_SEMANTIC,
        features=feature_set({"semantic_score": 0.7}),
        evidence=(),
        scope=selected_scope,
        lifecycle=LifecycleState.ACTIVE,
    )
    fusion = CandidateFusionEngine(
        authority=permissive_candidate_authority,
        reranker=TransparentLogisticReranker(reranker_config(enabled=True, shortlist_size=2)),
    )

    decision = fusion.decide(frame, (exact, lexical, semantic))
    reranker_report = decision.get("report", {}).get("reranker", {})
    selected = decision.get("selected_candidate", {})

    assert reranker_report.get("applied", False) is True
    assert len(reranker_report.get("scores", [])) == 2
    assert selected.get("provenance", {}).get("reranker_model_version", "")
    assert selected.get("diagnostics", {}).get("reranker_score", 0.0) > 0.0


def test_fusion_does_not_use_reranker_elapsed_time_as_answer_policy() -> None:
    engine = Engram()
    selected_scope = scope_key()
    frame = QueryFrameBuilder(engine, lambda: 1, lambda: datetime(2026, 8, 22, tzinfo=UTC)).build(
        "exact request",
        selected_scope,
        diagnostic_seed="reranker-fallback",
        budget=capture_resolution_budget(lambda: 1),
    )
    exact = candidate(
        candidate_id="exact-candidate",
        statement_id="exact-statement",
        response="Exact response",
        source=CandidateSource.EXACT,
        features=feature_set({"exact_match": 1.0}),
        evidence=(),
        scope=selected_scope,
        lifecycle=LifecycleState.ACTIVE,
    )
    ticks = iter((0, 2_000_000))
    reranker = TransparentLogisticReranker(
        reranker_config(enabled=True, max_model_time_ms=1),
        clock_ns=lambda: next(ticks),
    )

    decision = CandidateFusionEngine(authority=permissive_candidate_authority, reranker=reranker).decide(frame, (exact,))
    reranker_report = decision.get("report", {}).get("reranker", {})
    selected = decision.get("selected_candidate", {})

    assert reranker_report.get("applied", False) is True
    assert reranker_report.get("reason", "") == "completed"
    assert reranker_report.get("model_time_target_exceeded", False) is True
    assert selected.get("statement_id", "") == "exact-statement"
    assert selected.get("provenance", {}).get("reranker_model_version", "")


def test_fusion_counts_an_isolated_reranker_exception_as_a_fallback(monkeypatch) -> None:
    engine = Engram()
    selected_scope = scope_key()
    frame = QueryFrameBuilder(engine, lambda: 1, lambda: datetime(2026, 8, 22, tzinfo=UTC)).build(
        "exact request",
        selected_scope,
        diagnostic_seed="reranker-exception",
        budget=capture_resolution_budget(lambda: 1),
    )
    exact = candidate(
        candidate_id="exact-candidate",
        statement_id="exact-statement",
        response="Exact response",
        source=CandidateSource.EXACT,
        features=feature_set({"exact_match": 1.0}),
        evidence=(),
        scope=selected_scope,
        lifecycle=LifecycleState.ACTIVE,
    )
    reranker = TransparentLogisticReranker(reranker_config(enabled=True))

    def fail(internal_shortlist, cooperative_check) -> dict:
        del internal_shortlist
        raise RuntimeError("simulated reranker failure")

    monkeypatch.setattr(reranker, "rerank", fail)
    decision = CandidateFusionEngine(authority=permissive_candidate_authority, reranker=reranker).decide(frame, (exact,))

    assert decision.get("report", {}).get("reranker", {}).get("reason", "") == "reranker_exception"
    assert decision.get("selected_candidate", {}).get("statement_id", "") == "exact-statement"
    assert reranker.health().get("fallbacks", 0) == 1
    assert reranker.health().get("last_reason", "") == "reranker_exception"
