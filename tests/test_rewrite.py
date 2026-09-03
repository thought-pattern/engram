"""Section 11 symbolic retrieval rewrite contracts and integration."""

from datetime import UTC, datetime
from json import dumps as json_dumps, loads as json_loads
from pathlib import Path

from pytest import raises as pytest_raises

from engram.artifacts import LifecycleState, artifact_provenance, artifact_statistics, cached_response_artifact
from engram.config import engram_config
from engram.constants import CostClass, QueryOperator, Tier
from engram.core import Engram
from engram.errors import InvalidRequestError, RewriteLimitError
from engram.identity import build_retrieval_representation, build_standalone_identity, scope_key
from engram.repository import ArtifactRepository
from engram.resolution import (
    QueryFrameBuilder,
    inheritance_provenance,
    query_frame_to_json,
    query_frame_with_changes,
    resolution_budget,
)
from engram.rewrite import (
    RewriteEngine,
    RewriteStopReason,
    apply_rewrites_to_frame,
    lint_rewrite_corpus,
    load_default_rewrite_corpus,
    load_rewrite_corpus_text,
    rewrite_rule,
    rewrite_rule_to_dict,
)
from engram.service import EngramCore

NOW = datetime(2026, 8, 20, 18, 0, tzinfo=UTC)
REPOSITORY = Path(__file__).resolve().parents[1]


def internal_rule(
    rule_id: str,
    pattern: str,
    output: str,
    *,
    priority: int = 10,
    match_mode: str = "exact",
    maximum: int = 1,
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "rule_id": rule_id,
        "rule_version": 1,
        "category": "test",
        "input_constraints": {
            "match_mode": match_mode,
            "pattern": pattern,
            "min_tokens": 1,
            "max_tokens": 32,
            "required_operators": [],
            "requires_inherited_subject": False,
        },
        "output_template": output,
        "priority": priority,
        "scope": "global",
        "max_applications": maximum,
        "provenance": {
            "author": "test",
            "origin": "independent test fixture",
            "license": "Apache-2.0",
            "created_at": "2026-08-20",
        },
    }


def internal_artifact(request: str, response: str = "Atlas runs in Virginia.") -> dict:
    selected_scope = scope_key()
    result = cached_response_artifact(
        statement_id="rewrite-artifact",
        generation=1,
        response=response,
        query_identity=build_standalone_identity(request, selected_scope),
        retrieval=build_retrieval_representation(request),
        tier=Tier.STATIC,
        lifecycle=LifecycleState.ACTIVE,
        scope=selected_scope,
        support_references=(),
        valid_from="",
        valid_from_available=False,
        valid_until="",
        valid_until_available=False,
        superseded_by="",
        provenance=artifact_provenance("section11:test", "tester", "2026-08-20T18:00:00Z"),
        statistics=artifact_statistics(),
        metadata={},
    )
    return result


def engine_with_artifact(request: str) -> Engram:
    engine = Engram(engram_config(expand_contractions=False, retrieval_rewrites_enabled=True))
    engine.response_repository = ArtifactRepository((internal_artifact(request),))
    return engine


def test_rule_schema_round_trip_and_strict_validation() -> None:
    rule = rewrite_rule(internal_rule("one", "alpha beta gamma", "delta"))
    assert rewrite_rule(rewrite_rule_to_dict(rule)) == rule
    malformed = rewrite_rule_to_dict(rule)
    malformed["unknown"] = True
    with pytest_raises(InvalidRequestError, match="invalid fields"):
        rewrite_rule(malformed)


def test_corpus_loader_rejects_duplicate_rule_versions_and_unknown_fields() -> None:
    rule = internal_rule("duplicate", "alpha beta gamma", "delta")
    payload = {"schema_version": 1, "corpus_id": "test", "corpus_version": 1, "rules": [rule, rule]}
    with pytest_raises(InvalidRequestError, match="duplicate rule"):
        load_rewrite_corpus_text(json_dumps(payload))
    payload["extra"] = 1
    with pytest_raises(InvalidRequestError, match="invalid fields"):
        load_rewrite_corpus_text(json_dumps(payload))


def test_engine_rejects_empty_oversized_and_duplicate_rule_sets() -> None:
    rule = rewrite_rule(internal_rule("bounded", "alpha beta gamma", "delta"))
    with pytest_raises(InvalidRequestError, match="1 through"):
        RewriteEngine(())
    with pytest_raises(InvalidRequestError, match="1 through"):
        RewriteEngine(tuple(rule for _ in range(257)))
    with pytest_raises(InvalidRequestError, match="unique identity"):
        RewriteEngine((rule, rule))


def test_no_matching_rule_does_not_hide_implicit_normalization() -> None:
    engine = RewriteEngine(load_default_rewrite_corpus())
    original = "  untouched   spacing  "
    execution = engine.rewrite(original)
    assert execution["final_text"] == original
    assert execution["chain"] == ()


def test_deterministic_priority_chain_and_application_limit() -> None:
    rules = (
        rewrite_rule(internal_rule("second", "bravo", "charlie", priority=10)),
        rewrite_rule(internal_rule("first", "alpha", "bravo", priority=20)),
        rewrite_rule(internal_rule("grow-once", "charlie", "charlie delta", priority=5, match_mode="token_sequence")),
    )
    engine = RewriteEngine(rules)
    first = engine.rewrite("alpha")
    second = engine.rewrite("alpha")
    assert first["final_text"] == "charlie delta"
    assert first["chain"] == second["chain"]
    assert [step[0] for step in first["chain"]] == ["first@1", "second@1", "grow-once@1"]


def test_depth_expansion_cycle_output_and_time_bounds() -> None:
    chain = (
        rewrite_rule(internal_rule("a", "alpha", "bravo")),
        rewrite_rule(internal_rule("b", "bravo", "charlie")),
    )
    assert RewriteEngine(chain, max_depth=1).rewrite("alpha")["stop_reason"] == RewriteStopReason.DEPTH_LIMIT

    overlapping = (
        rewrite_rule(internal_rule("high", "alpha", "bravo", priority=20)),
        rewrite_rule(internal_rule("low", "alpha", "charlie", priority=10)),
    )
    assert RewriteEngine(overlapping, max_expansions=1).rewrite("alpha")["stop_reason"] == RewriteStopReason.EXPANSION_LIMIT

    cycle = (
        rewrite_rule(internal_rule("forward", "alpha", "bravo")),
        rewrite_rule(internal_rule("back", "bravo", "alpha")),
    )
    assert RewriteEngine(cycle).rewrite("alpha")["stop_reason"] == RewriteStopReason.CYCLE

    output = (rewrite_rule(internal_rule("large", "alpha", "a much larger output")),)
    assert RewriteEngine(output, max_output_bytes=8).rewrite("alpha")["stop_reason"] == RewriteStopReason.OUTPUT_LIMIT

    ticks = iter((0, 2, 3))
    timed = RewriteEngine(chain, max_elapsed_ns=1, clock_ns=lambda: next(ticks))
    assert timed.rewrite("alpha")["stop_reason"] == RewriteStopReason.TIME_LIMIT


def test_cooperative_cancellation_propagates_without_partial_frame() -> None:
    engine = RewriteEngine(load_default_rewrite_corpus())

    def cancel() -> None:
        raise RuntimeError("cancelled")

    with pytest_raises(RuntimeError, match="cancelled"):
        engine.rewrite("could you tell me where atlas runs?", operator=QueryOperator.WHERE, cooperative_check=cancel)


def test_frame_integration_rejects_partial_resource_limited_chain() -> None:
    frame = QueryFrameBuilder(Engram(), lambda: 1, lambda: NOW).build("alpha", diagnostic_seed="bounded")
    engine = RewriteEngine((rewrite_rule(internal_rule("first", "alpha", "bravo")),), max_depth=1)
    with pytest_raises(RewriteLimitError, match="depth_limit"):
        apply_rewrites_to_frame(frame, engine)


def test_frame_trace_preserves_original_identity_and_round_trips() -> None:
    frame = QueryFrameBuilder(
        Engram(engram_config(expand_contractions=False)),
        lambda: 1,
        lambda: NOW,
    ).build("could you tell me where atlas runs?", diagnostic_seed="rewrite-frame")
    identity = frame["identity"]
    rewritten = apply_rewrites_to_frame(frame, RewriteEngine(load_default_rewrite_corpus()))
    assert rewritten["original_text"] == "could you tell me where atlas runs?"
    assert rewritten["resolved_text"] == "where atlas runs?"
    assert rewritten["identity"] == identity
    assert rewritten["rewrite_chain"] == (
        {
            "rule_id": "question-could-you-tell-me@1",
            "input_text": "could you tell me where atlas runs?",
            "output_text": "where atlas runs?",
        },
    )
    assert "question-could-you-tell-me@1" in query_frame_to_json(rewritten)


def test_contextual_rule_requires_inherited_subject() -> None:
    base = QueryFrameBuilder(Engram(), lambda: 1, lambda: NOW).build("what about it?", diagnostic_seed="context")
    no_context = apply_rewrites_to_frame(base, RewriteEngine(load_default_rewrite_corpus()))
    assert no_context["resolved_text"] == "what about it?"
    contextual = query_frame_with_changes(
        base,
        {
            "identity": build_standalone_identity("what about Kestrel?", scope_key()),
            "inheritance": (inheritance_provenance("subjects", 1),),
        },
    )
    rewritten = apply_rewrites_to_frame(contextual, RewriteEngine(load_default_rewrite_corpus()))
    assert rewritten["resolved_text"] == "what about Kestrel"


def test_held_out_engineering_corpus_has_expected_rewrites_and_identity_stability() -> None:
    payload = json_loads((REPOSITORY / "eval" / "section11-rewrite-v1.json").read_text(encoding="utf-8"))
    engine = RewriteEngine(load_default_rewrite_corpus())
    seen = set()
    for case in payload["cases"]:
        result = engine.rewrite(
            case["input"],
            operator=QueryOperator(case["operator"]),
            subject=case["subject"],
            inherited_subject=case["inherited_subject"],
        )
        assert result["final_text"] == case["expected_final"], case["case_id"]
        assert bool(result["chain"]) is case["should_rewrite"], case["case_id"]
        assert case["case_id"] not in seen
        seen.add(case["case_id"])


def test_linter_detects_each_required_structural_failure_class() -> None:
    collision = (
        rewrite_rule(internal_rule("first", "alpha beta gamma", "delta", priority=20)),
        rewrite_rule(internal_rule("shadow", "alpha beta gamma", "echo", priority=10)),
        rewrite_rule(internal_rule("duplicate-output", "foxtrot golf hotel", "delta")),
        rewrite_rule(internal_rule("broad", "small", "large", match_mode="token_sequence")),
        rewrite_rule(internal_rule("cycle-one", "india juliet kilo", "lima mike november")),
        rewrite_rule(internal_rule("cycle-two", "lima mike november", "india juliet kilo")),
    )
    codes = {finding["code"] for finding in lint_rewrite_corpus(collision)}
    assert {"rule_collision", "duplicate_output", "overbroad_rule", "rewrite_cycle"}.issubset(codes)


def test_rewritten_exact_recall_uses_only_the_artifact_resolver() -> None:
    engine = engine_with_artifact("where atlas runs?")
    engine.store("Pattern must remain unreachable.", tier=Tier.STATIC, pattern="WHERE ATLAS RUNS")
    core = EngramCore(engine, clock=lambda: NOW)
    result = core.resolve_request(
        "could you tell me where atlas runs?",
        "rewrite-exact",
        configured_resolvers=("exact",),
        budget=resolution_budget(allowed_cost_classes=(CostClass.EXACT, CostClass.CHEAP)),
    )
    results = {item["resolver"]: item for item in result["resolver_results"]}
    assert results["exact"]["reason_code"] == "exact_found"
    assert tuple(results) == ("exact",)
    assert result["response_candidates"][0]["response"] == "Atlas runs in Virginia."


def test_conversation_pattern_matching_is_separate_from_retrieval_rewrite() -> None:
    engine = Engram(engram_config(expand_contractions=False, retrieval_rewrites_enabled=True))
    engine.store("Original pattern selected.", tier=Tier.STATIC, pattern="COULD YOU TELL ME WHERE ATLAS RUNS")
    result = engine.pattern_query("could you tell me where atlas runs?")
    assert result[2] == "Original pattern selected."


def test_rewrite_configuration_is_opt_in_and_validated() -> None:
    assert engram_config()["retrieval_rewrites_enabled"] is False
    assert engram_config(retrieval_rewrites_enabled=True)["retrieval_rewrites_enabled"] is True
    with pytest_raises(ValueError, match="must be a boolean"):
        engram_config(retrieval_rewrites_enabled=1)  # type: ignore[arg-type]
