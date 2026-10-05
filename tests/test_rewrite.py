"""Section 11 symbolic retrieval rewrite contracts and integration."""

from datetime import UTC, datetime
from json import dumps as json_dumps, loads as json_loads
from pathlib import Path

from pytest import raises as pytest_raises

from engram.artifacts import validate_cached_response_artifact
from engram.config import engram_config
from engram.constants import INITIAL_ARTIFACT_STATISTICS, CostClass, LifecycleState, QueryOperator, Tier
from engram.core import Engram
from engram.errors import InvalidRequestError
from engram.identity import extract_standalone_identity, retrieval_representation, scope_key
from engram.repository import ArtifactRepository
from engram.resolution import (
    QueryFrameBuilder,
    inheritance_provenance,
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
)
from engram.service import EngramCore

NOW = datetime(2026, 8, 20, 18, 0, tzinfo=UTC)
REPOSITORY = Path(__file__).resolve().parents[1]
# Shared fields of the independent test rules; each rule adds its rule_id, input_constraints and output_template.
# rewrite_rule validates into a new rule, so these read-only constants are never shared with a validated rule.
REWRITE_RULE_FIELDS = {
    "category": "test",
    "priority": 10,
    "scope": "global",
    "max_applications": 1,
    "provenance": {
        "author": "test",
        "origin": "independent test fixture",
        "license": "Apache-2.0",
        "created_at": "2026-08-20",
    },
}
EXACT_INPUT_CONSTRAINTS = {
    "match_mode": "exact",
    "min_tokens": 1,
    "max_tokens": 32,
    "required_operators": [],
    "requires_inherited_subject": False,
}
REWRITE_CASE_FIELDS = {"case_id", "input", "operator", "subject", "inherited_subject", "expected_final", "should_rewrite"}


def test_rule_schema_is_strict() -> None:
    rule = {
        **REWRITE_RULE_FIELDS,
        "rule_id": "one",
        "input_constraints": {**EXACT_INPUT_CONSTRAINTS, "pattern": "alpha beta gamma"},
        "output_template": "delta",
    }
    assert rewrite_rule(rule).get("rule_id", "") == "one"
    malformed = dict(rule)
    malformed["unknown"] = True
    with pytest_raises(InvalidRequestError, match="invalid fields"):
        rewrite_rule(malformed)


def test_corpus_loader_rejects_duplicate_rules_and_unknown_fields() -> None:
    rule = {
        **REWRITE_RULE_FIELDS,
        "rule_id": "duplicate",
        "input_constraints": {**EXACT_INPUT_CONSTRAINTS, "pattern": "alpha beta gamma"},
        "output_template": "delta",
    }
    payload = {"corpus_id": "test", "rules": [rule, rule]}
    with pytest_raises(InvalidRequestError, match="duplicate rule"):
        load_rewrite_corpus_text(json_dumps(payload))
    payload["extra"] = 1
    with pytest_raises(InvalidRequestError, match="invalid fields"):
        load_rewrite_corpus_text(json_dumps(payload))


def test_engine_rejects_empty_oversized_and_duplicate_rule_sets() -> None:
    rule = rewrite_rule(
        {
            **REWRITE_RULE_FIELDS,
            "rule_id": "bounded",
            "input_constraints": {**EXACT_INPUT_CONSTRAINTS, "pattern": "alpha beta gamma"},
            "output_template": "delta",
        }
    )
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
    assert "chain" in execution
    assert execution.get("final_text", "") == original
    assert execution.get("chain", ()) == ()


def test_deterministic_priority_chain_and_application_limit() -> None:
    rules = (
        rewrite_rule(
            {
                **REWRITE_RULE_FIELDS,
                "rule_id": "second",
                "input_constraints": {**EXACT_INPUT_CONSTRAINTS, "pattern": "bravo"},
                "output_template": "charlie",
                "priority": 10,
            }
        ),
        rewrite_rule(
            {
                **REWRITE_RULE_FIELDS,
                "rule_id": "first",
                "input_constraints": {**EXACT_INPUT_CONSTRAINTS, "pattern": "alpha"},
                "output_template": "bravo",
                "priority": 20,
            }
        ),
        rewrite_rule(
            {
                **REWRITE_RULE_FIELDS,
                "rule_id": "grow-once",
                "input_constraints": {**EXACT_INPUT_CONSTRAINTS, "match_mode": "token_sequence", "pattern": "charlie"},
                "output_template": "charlie delta",
                "priority": 5,
            }
        ),
    )
    engine = RewriteEngine(rules)
    first = engine.rewrite("alpha")
    second = engine.rewrite("alpha")
    first_chain = first.get("chain", ())
    assert first.get("final_text", "") == "charlie delta"
    assert first_chain == second.get("chain", ())
    assert [step[0] for step in first_chain] == ["first", "second", "grow-once"]


def test_depth_expansion_cycle_output_and_time_bounds() -> None:
    chain = (
        rewrite_rule(
            {
                **REWRITE_RULE_FIELDS,
                "rule_id": "a",
                "input_constraints": {**EXACT_INPUT_CONSTRAINTS, "pattern": "alpha"},
                "output_template": "bravo",
            }
        ),
        rewrite_rule(
            {
                **REWRITE_RULE_FIELDS,
                "rule_id": "b",
                "input_constraints": {**EXACT_INPUT_CONSTRAINTS, "pattern": "bravo"},
                "output_template": "charlie",
            }
        ),
    )
    depth_limited = RewriteEngine(chain, max_depth=1).rewrite("alpha")
    assert depth_limited.get("stop_reason", RewriteStopReason.FIXED_POINT) == RewriteStopReason.DEPTH_LIMIT

    overlapping = (
        rewrite_rule(
            {
                **REWRITE_RULE_FIELDS,
                "rule_id": "high",
                "input_constraints": {**EXACT_INPUT_CONSTRAINTS, "pattern": "alpha"},
                "output_template": "bravo",
                "priority": 20,
            }
        ),
        rewrite_rule(
            {
                **REWRITE_RULE_FIELDS,
                "rule_id": "low",
                "input_constraints": {**EXACT_INPUT_CONSTRAINTS, "pattern": "alpha"},
                "output_template": "charlie",
                "priority": 10,
            }
        ),
    )
    expansion_limited = RewriteEngine(overlapping, max_expansions=1).rewrite("alpha")
    assert expansion_limited.get("stop_reason", RewriteStopReason.FIXED_POINT) == RewriteStopReason.EXPANSION_LIMIT

    cycle = (
        rewrite_rule(
            {
                **REWRITE_RULE_FIELDS,
                "rule_id": "forward",
                "input_constraints": {**EXACT_INPUT_CONSTRAINTS, "pattern": "alpha"},
                "output_template": "bravo",
            }
        ),
        rewrite_rule(
            {
                **REWRITE_RULE_FIELDS,
                "rule_id": "back",
                "input_constraints": {**EXACT_INPUT_CONSTRAINTS, "pattern": "bravo"},
                "output_template": "alpha",
            }
        ),
    )
    assert RewriteEngine(cycle).rewrite("alpha").get("stop_reason", RewriteStopReason.FIXED_POINT) == RewriteStopReason.CYCLE

    output = (
        rewrite_rule(
            {
                **REWRITE_RULE_FIELDS,
                "rule_id": "large",
                "input_constraints": {**EXACT_INPUT_CONSTRAINTS, "pattern": "alpha"},
                "output_template": "a much larger output",
            }
        ),
    )
    output_limited = RewriteEngine(output, max_output_bytes=8).rewrite("alpha")
    assert output_limited.get("stop_reason", RewriteStopReason.FIXED_POINT) == RewriteStopReason.OUTPUT_LIMIT

    ticks = iter((0, 2, 3))
    timed = RewriteEngine(chain, max_elapsed_ns=1, clock_ns=lambda: next(ticks))
    assert timed.rewrite("alpha").get("stop_reason", RewriteStopReason.FIXED_POINT) == RewriteStopReason.TIME_LIMIT


def test_cooperative_cancellation_propagates_without_partial_frame() -> None:
    engine = RewriteEngine(load_default_rewrite_corpus())

    def cancel() -> None:
        raise RuntimeError("cancelled")

    with pytest_raises(RuntimeError, match="cancelled"):
        engine.rewrite("could you tell me where atlas runs?", operator=QueryOperator.WHERE, cooperative_check=cancel)


def test_frame_integration_discards_a_partial_resource_limited_chain() -> None:
    frame = QueryFrameBuilder(Engram(), lambda: 1, lambda: NOW).build("alpha", diagnostic_seed="bounded")
    rule = rewrite_rule(
        {
            **REWRITE_RULE_FIELDS,
            "rule_id": "first",
            "input_constraints": {**EXACT_INPUT_CONSTRAINTS, "pattern": "alpha"},
            "output_template": "bravo",
        }
    )
    engine = RewriteEngine((rule,), max_depth=1)

    assert engine.rewrite("alpha").get("stop_reason", RewriteStopReason.FIXED_POINT) == RewriteStopReason.DEPTH_LIMIT
    result = apply_rewrites_to_frame(frame, engine)

    assert result == frame
    assert "rewrite_chain" in result
    assert result.get("resolved_text", "") == "alpha"
    assert result.get("rewrite_chain", ()) == ()


def test_frame_trace_preserves_original_identity_and_round_trips() -> None:
    frame = QueryFrameBuilder(
        Engram(engram_config(expand_contractions=False)),
        lambda: 1,
        lambda: NOW,
    ).build("could you tell me where atlas runs?", diagnostic_seed="rewrite-frame")
    identity = frame.get("identity", {})
    assert identity
    rewritten = apply_rewrites_to_frame(frame, RewriteEngine(load_default_rewrite_corpus()))
    assert rewritten.get("original_text", "") == "could you tell me where atlas runs?"
    assert rewritten.get("resolved_text", "") == "where atlas runs?"
    assert rewritten.get("identity", {}) == identity
    assert rewritten.get("rewrite_chain", ()) == (
        {
            "rule_id": "question-could-you-tell-me",
            "input_text": "could you tell me where atlas runs?",
            "output_text": "where atlas runs?",
        },
    )


def test_contextual_rule_requires_inherited_subject() -> None:
    base = QueryFrameBuilder(Engram(), lambda: 1, lambda: NOW).build("what about it?", diagnostic_seed="context")
    no_context = apply_rewrites_to_frame(base, RewriteEngine(load_default_rewrite_corpus()))
    assert no_context.get("resolved_text", "") == "what about it?"
    contextual = query_frame_with_changes(
        base,
        {
            "identity": extract_standalone_identity("what about Kestrel?", scope_key()),
            "inheritance": (inheritance_provenance("subjects", 1),),
        },
    )
    rewritten = apply_rewrites_to_frame(contextual, RewriteEngine(load_default_rewrite_corpus()))
    assert rewritten.get("resolved_text", "") == "what about Kestrel"


def test_held_out_engineering_corpus_has_expected_rewrites_and_identity_stability() -> None:
    payload = json_loads((REPOSITORY / "tests" / "fixtures" / "rewrite" / "cases.json").read_text(encoding="utf-8"))
    engine = RewriteEngine(load_default_rewrite_corpus())
    seen = set()
    assert "cases" in payload
    for case in payload.get("cases", []):
        case_id = case.get("case_id", "")
        assert case.keys() >= REWRITE_CASE_FIELDS, case_id
        result = engine.rewrite(
            case.get("input", ""),
            operator=QueryOperator(case.get("operator", "")),
            subject=case.get("subject", ""),
            inherited_subject=case.get("inherited_subject", ""),
        )
        assert result.get("final_text", "") == case.get("expected_final", ""), case_id
        assert bool(result.get("chain", ())) is case.get("should_rewrite", False), case_id
        assert case_id not in seen
        seen.add(case_id)


def test_linter_detects_each_required_structural_failure_class() -> None:
    collision = (
        rewrite_rule(
            {
                **REWRITE_RULE_FIELDS,
                "rule_id": "first",
                "input_constraints": {**EXACT_INPUT_CONSTRAINTS, "pattern": "alpha beta gamma"},
                "output_template": "delta",
                "priority": 20,
            }
        ),
        rewrite_rule(
            {
                **REWRITE_RULE_FIELDS,
                "rule_id": "shadow",
                "input_constraints": {**EXACT_INPUT_CONSTRAINTS, "pattern": "alpha beta gamma"},
                "output_template": "echo",
                "priority": 10,
            }
        ),
        rewrite_rule(
            {
                **REWRITE_RULE_FIELDS,
                "rule_id": "duplicate-output",
                "input_constraints": {**EXACT_INPUT_CONSTRAINTS, "pattern": "foxtrot golf hotel"},
                "output_template": "delta",
            }
        ),
        rewrite_rule(
            {
                **REWRITE_RULE_FIELDS,
                "rule_id": "broad",
                "input_constraints": {**EXACT_INPUT_CONSTRAINTS, "match_mode": "token_sequence", "pattern": "small"},
                "output_template": "large",
            }
        ),
        rewrite_rule(
            {
                **REWRITE_RULE_FIELDS,
                "rule_id": "cycle-one",
                "input_constraints": {**EXACT_INPUT_CONSTRAINTS, "pattern": "india juliet kilo"},
                "output_template": "lima mike november",
            }
        ),
        rewrite_rule(
            {
                **REWRITE_RULE_FIELDS,
                "rule_id": "cycle-two",
                "input_constraints": {**EXACT_INPUT_CONSTRAINTS, "pattern": "lima mike november"},
                "output_template": "india juliet kilo",
            }
        ),
    )
    codes = {finding.get("code", "") for finding in lint_rewrite_corpus(collision)}
    assert {"rule_collision", "duplicate_output", "overbroad_rule", "rewrite_cycle"}.issubset(codes)


def test_rewritten_exact_recall_uses_only_the_artifact_resolver() -> None:
    request = "where atlas runs?"
    selected_scope = scope_key()
    artifact = validate_cached_response_artifact(
        {
            "statement_id": "rewrite-artifact",
            "generation": 1,
            "response": "Atlas runs in Virginia.",
            "query_identity": extract_standalone_identity(request, selected_scope),
            "retrieval": retrieval_representation(request),
            "tier": Tier.STATIC,
            "lifecycle": LifecycleState.ACTIVE,
            "scope": selected_scope,
            "support_references": (),
            "valid_from": "",
            "valid_from_available": False,
            "valid_until": "",
            "valid_until_available": False,
            "superseded_by": "",
            "provenance": {"source_label": "section11:test", "caller_id": "tester", "accepted_at": "2026-08-20T18:00:00Z"},
            "statistics": INITIAL_ARTIFACT_STATISTICS,
            "metadata": {},
        }
    )
    engine = Engram(engram_config(expand_contractions=False, retrieval_rewrites_enabled=True))
    engine.response_repository = ArtifactRepository((artifact,))
    engine.store("Pattern must remain unreachable.", tier=Tier.STATIC, pattern="WHERE ATLAS RUNS")
    core = EngramCore(engine, clock=lambda: NOW)
    result = core.resolve_request(
        "could you tell me where atlas runs?",
        "rewrite-exact",
        configured_resolvers=("exact",),
        budget=resolution_budget(allowed_cost_classes=(CostClass.EXACT, CostClass.CHEAP)),
    )
    results = {item.get("resolver", ""): item for item in result.get("resolver_results", ())}
    assert results.get("exact", {}).get("reason_code", "") == "exact_found"
    assert tuple(results) == ("exact",)
    assert result.get("response_candidates", ())[0].get("response", "") == "Atlas runs in Virginia."


def test_conversation_pattern_matching_is_separate_from_retrieval_rewrite() -> None:
    engine = Engram(engram_config(expand_contractions=False, retrieval_rewrites_enabled=True))
    engine.store("Original pattern selected.", tier=Tier.STATIC, pattern="COULD YOU TELL ME WHERE ATLAS RUNS")
    result = engine.pattern_query("could you tell me where atlas runs?")
    assert result[2] == "Original pattern selected."


def test_rewrite_configuration_is_opt_in_and_validated() -> None:
    default_config = engram_config()
    assert "retrieval_rewrites_enabled" in default_config
    assert default_config.get("retrieval_rewrites_enabled", False) is False
    assert engram_config(retrieval_rewrites_enabled=True).get("retrieval_rewrites_enabled", False) is True
    with pytest_raises(ValueError, match="must be a boolean"):
        engram_config(retrieval_rewrites_enabled=1)  # type: ignore[arg-type]
