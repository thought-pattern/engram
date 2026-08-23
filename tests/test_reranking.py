"""Section 13 bounded reranker contracts and integration."""

from datetime import UTC, datetime

import pytest

from engram.artifacts import LifecycleState
from engram.config import engram_config, reranker_config
from engram.constants import CandidateSource
from engram.core import Engram
from engram.errors import InvalidRequestError, ResolutionCancelledError
from engram.fusion import CandidateFusionEngine, permissive_candidate_authority
from engram.identity import scope_key
from engram.reranking import RERANKER_COEFFICIENTS, RERANKER_FEATURES, TransparentLogisticReranker
from engram.resolution import QueryFrameBuilder, candidate, capture_resolution_budget, feature_set


def shortlist() -> tuple[dict[str, object], ...]:
    return (
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


def enabled_reranker(**changes) -> TransparentLogisticReranker:
    return TransparentLogisticReranker(reranker_config(enabled=True, **changes))


def test_transparent_logistic_contract_exposes_fixed_features_and_coefficients() -> None:
    reranker = enabled_reranker()

    result = reranker.rerank(shortlist())

    assert result["applied"] is True
    assert result["reason"] == "completed"
    assert [value["statement_id"] for value in result["scores"]] == ["semantic", "lexical"]
    assert set(result["scores"][0]["features"]) == set(RERANKER_FEATURES)
    assert set(RERANKER_COEFFICIENTS) == set(RERANKER_FEATURES)
    assert result["scores"][0]["score"] > result["scores"][1]["score"]


def test_reranker_bounds_shortlist_and_input_bytes_with_baseline_fallback() -> None:
    bounded = enabled_reranker(shortlist_size=1)
    input_limited = enabled_reranker(max_input_bytes=1)

    shortlist_fallback = bounded.rerank(shortlist())
    fallback = input_limited.rerank(shortlist())

    assert shortlist_fallback["applied"] is False
    assert shortlist_fallback["reason"] == "shortlist_budget"
    assert shortlist_fallback["scores"] == []
    assert fallback["applied"] is False
    assert fallback["reason"] == "input_budget"
    assert fallback["scores"] == []
    assert input_limited.health()["fallbacks"] == 1


def test_reranker_reports_elapsed_target_without_changing_the_result() -> None:
    ticks = iter((0, 2_000_000))
    reranker = TransparentLogisticReranker(
        reranker_config(enabled=True, max_model_time_ms=1),
        clock_ns=lambda: next(ticks),
    )

    result = reranker.rerank(shortlist())

    assert result["applied"] is True
    assert result["reason"] == "completed"
    assert result["elapsed_ns"] == 2_000_000
    assert result["model_time_target_exceeded"] is True
    assert reranker.health()["last_reason"] == "completed"


def test_reranker_propagates_cancellation_and_counts_it() -> None:
    reranker = enabled_reranker()

    def cancel() -> None:
        raise ResolutionCancelledError("cancelled")

    with pytest.raises(ResolutionCancelledError):
        reranker.rerank(shortlist(), cancel)

    assert reranker.health()["cancellations"] == 1


def test_reranker_rejects_malformed_internal_contract_values() -> None:
    reranker = enabled_reranker()

    with pytest.raises(InvalidRequestError):
        reranker.rerank(({"statement_id": "bad", "base_score": float("nan"), "features": {}},))
    with pytest.raises(InvalidRequestError):
        reranker.rerank(({"statement_id": "", "base_score": 0.5, "features": {}},))


def test_semantic_and_reranker_readiness_are_independent() -> None:
    engine = Engram(config=engram_config(reranker=reranker_config(enabled=True)))

    components = engine.component_status_snapshot()

    assert components["semantic"]["enabled"] is False
    assert components["semantic"]["ready"] is False
    assert components["reranker"]["enabled"] is True
    assert components["reranker"]["ready"] is True


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
        candidate_id="lexical-candidate",
        statement_id="lexical-statement",
        response="Lexical response",
        source=CandidateSource.LEXICAL,
        features=feature_set({"lexical_score": 0.8}),
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
        reranker=enabled_reranker(shortlist_size=2),
    )

    decision = fusion.decide(frame, (exact, lexical, semantic))

    assert decision["report"]["reranker"]["applied"] is True
    assert len(decision["report"]["reranker"]["scores"]) == 2
    assert decision["selected_candidate"]["provenance"]["reranker_model_version"] == "transparent-logistic-v1"
    assert decision["selected_candidate"]["diagnostics"]["reranker_score"] > 0.0


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

    assert decision["report"]["reranker"]["applied"] is True
    assert decision["report"]["reranker"]["reason"] == "completed"
    assert decision["report"]["reranker"]["model_time_target_exceeded"] is True
    assert decision["selected_candidate"]["statement_id"] == "exact-statement"
    assert decision["selected_candidate"]["provenance"]["reranker_model_version"] == "transparent-logistic-v1"


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
    reranker = enabled_reranker()

    def fail(_shortlist, _cooperative_check) -> dict:
        raise RuntimeError("simulated reranker failure")

    monkeypatch.setattr(reranker, "rerank", fail)
    decision = CandidateFusionEngine(authority=permissive_candidate_authority, reranker=reranker).decide(frame, (exact,))

    assert decision["report"]["reranker"]["reason"] == "reranker_exception"
    assert decision["selected_candidate"]["statement_id"] == "exact-statement"
    assert reranker.health()["fallbacks"] == 1
    assert reranker.health()["last_reason"] == "reranker_exception"
