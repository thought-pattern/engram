"""Conformance, safety, and integration tests for Section 14 utilities."""

import ast
import random
import string
from decimal import Decimal
from pathlib import Path
from types import MappingProxyType

import pytest

from engram import utilities as utility_module
from engram.config import config_from_dict, config_to_dict, engram_config, load_config
from engram.constants import UTILITY_CONTRACT_VERSION, UTILITY_MAX_COLLECTION_ITEMS, UTILITY_PLUGIN_NAMES, CandidateSource
from engram.core import Engram
from engram.fusion import EngramCandidateAuthority
from engram.resolution import ResolutionOutcome, candidate_with_changes, validate_query_frame
from engram.service import EngramCore
from engram.utilities import UtilityRegistry, evaluate_named_utility, utility_config, utility_plugin_contracts


def enabled_registry(plugins=UTILITY_PLUGIN_NAMES) -> UtilityRegistry:
    return UtilityRegistry(utility_config(enabled=True, plugins=plugins))


@pytest.mark.parametrize(
    ("query", "plugin", "response"),
    [
        ("calculate 2 + 3 * 4", "arithmetic_v1", "14"),
        ("arithmetic (2 + 3) ** 2", "arithmetic_v1", "25"),
        ("boolean true and not false", "boolean_v1", "true"),
        ("boolean false or true xor true", "boolean_v1", "false"),
        ("set union {b,a} and {b,c}", "set_v1", "{a, b, c}"),
        ("set symmetric difference {a,b} with {b,c}", "set_v1", "{a, c}"),
        ("date 2024-02-28 plus 1 day", "date_time_v1", "2024-02-29"),
        ("days between 2026-08-01 and 2026-08-22", "date_time_v1", "21"),
        (
            "convert time 2026-08-22T14:30:00-04:00 to UTC",
            "date_time_v1",
            "2026-08-22T18:30:00+00:00[UTC]",
        ),
        ("convert 32 F to C", "unit_conversion_v1", "0 C"),
        ("convert 5 km to mi", "unit_conversion_v1", "3.10685596118667 mi"),
        ("compare version 1.2.3-alpha.1 and 1.2.3", "version_v1", "1.2.3-alpha.1 < 1.2.3"),
        ("compare version 1.2.3+first and 1.2.3+second", "version_v1", "1.2.3+first = 1.2.3+second"),
        (
            "validate uuid 550e8400-e29b-41d4-a716-446655440000",
            "identifier_v1",
            "valid uuid: 550e8400-e29b-41d4-a716-446655440000",
        ),
        ("validate slug cats-and-sushi", "identifier_v1", "valid slug: cats-and-sushi"),
        ("validate slug Cats_and_sushi", "identifier_v1", "invalid slug"),
    ],
)
def test_each_allowlisted_grammar_has_a_deterministic_canonical_result(query: str, plugin: str, response: str) -> None:
    registry = enabled_registry()

    first = registry.evaluate(query)
    second = registry.evaluate(query)

    assert first == second
    assert first["status"] == "resolved"
    assert first["plugin_name"] == plugin
    assert first["response"] == response
    assert first["operations"] <= 32


def test_plugin_contract_is_complete_and_matches_executable_allowlist() -> None:
    contracts = utility_plugin_contracts()

    assert tuple(contract["name"] for contract in contracts) == UTILITY_PLUGIN_NAMES
    assert all(contract["contract_version"] == UTILITY_CONTRACT_VERSION for contract in contracts)
    assert all(
        set(contract)
        == {
            "contract_version",
            "name",
            "version",
            "accepted_frame_types",
            "input_schema",
            "bounds",
            "deterministic_result",
            "evidence",
            "errors",
            "health",
        }
        for contract in contracts
    )


def test_registry_rejects_unknown_dynamic_plugin_names_and_duplicate_configuration() -> None:
    with pytest.raises(ValueError, match="unknown utility plugins"):
        utility_config(enabled=True, plugins=("pathlib.Path",))
    with pytest.raises(ValueError, match="duplicates"):
        utility_config(enabled=True, plugins=("arithmetic_v1", "arithmetic_v1"))
    with pytest.raises(ValueError, match="at least one"):
        utility_config(enabled=True, plugins=())


def test_default_off_and_independent_plugin_selection() -> None:
    disabled = UtilityRegistry()
    arithmetic_only = enabled_registry(("arithmetic_v1",))

    assert disabled.evaluate("calculate 1 + 1")["status"] == "miss"
    assert arithmetic_only.evaluate("calculate 1 + 1")["response"] == "2"
    assert arithmetic_only.evaluate("boolean true")["status"] == "miss"
    health = arithmetic_only.health()
    assert health["plugins"]["arithmetic_v1"]["ready"] is True
    assert health["plugins"]["boolean_v1"]["ready"] is False


@pytest.mark.parametrize(
    ("query", "error_code"),
    [
        ("calculate 1 / 0", "arithmetic_domain"),
        ("calculate 2 ** 13", "operation_limit"),
        ("calculate 1 + unknown", "arithmetic_syntax"),
        ("boolean true and maybe", "boolean_syntax"),
        ("set union {valid,bad item} and {x}", "collection_item_invalid"),
        ("date 2023-02-29 plus 1 day", "date_time_domain"),
        ("convert time 2026-08-22T14:30:00 to UTC", "date_time_domain"),
        ("convert time 20260822T143000+00:00 to UTC", "date_time_domain"),
        ("convert time 2026-08-22T14:30:00+00:00 to Etc/Unknown", "timezone_not_allowed"),
        ("convert 1 kg to m", "dimension_mismatch"),
        ("convert 1 parsec to m", "unit_unknown"),
        ("compare version 01.2.3 and 1.2.3", "version_syntax"),
    ],
)
def test_malformed_or_ambiguous_inputs_are_stable_rejections(query: str, error_code: str) -> None:
    result = enabled_registry().evaluate(query)

    assert result["status"] == "rejected"
    assert result["error_code"] == error_code
    assert result["response"] == ""


def test_collection_and_operation_resource_limits_are_hard() -> None:
    items = ",".join(f"i{index}" for index in range(UTILITY_MAX_COLLECTION_ITEMS + 1))
    collection = enabled_registry().evaluate(f"set union {{{items}}} and {{x}}")
    operations = enabled_registry().evaluate("calculate " + " + ".join("1" for _ in range(34)))

    assert collection["status"] == "rejected"
    assert collection["error_code"] == "collection_limit"
    assert operations["status"] == "rejected"
    assert operations["error_code"] == "operation_limit"


def test_expression_payload_is_data_and_never_executes(tmp_path) -> None:
    target = tmp_path / "executed.txt"
    payload = f"calculate __import__('pathlib').Path('{target}').write_text('bad')"

    result = enabled_registry().evaluate(payload)

    assert result["status"] == "rejected"
    assert result["error_code"] == "arithmetic_syntax"
    assert not target.exists()


def test_production_utility_module_has_no_dynamic_execution_calls() -> None:
    tree = ast.parse(Path(utility_module.__file__).read_text(encoding="utf-8"))
    forbidden = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id in {"eval", "exec", "compile", "__import__"}
        ):
            forbidden.append((node.func.id, node.lineno))
    assert forbidden == []


def test_arithmetic_set_version_and_unit_properties() -> None:
    registry = enabled_registry()
    randomizer = random.Random(1406)
    for _ in range(100):
        left = randomizer.randint(-10_000, 10_000)
        right = randomizer.randint(-10_000, 10_000)
        first = registry.evaluate(f"calculate {left} + {right}")["response"]
        second = registry.evaluate(f"calculate {right} + {left}")["response"]
        assert first == second

        left_set = {f"i{randomizer.randint(0, 20)}" for _ in range(5)}
        right_set = {f"i{randomizer.randint(0, 20)}" for _ in range(5)}
        left_text = ",".join(sorted(left_set))
        right_text = ",".join(sorted(right_set))
        first_set = registry.evaluate(f"set union {{{left_text}}} and {{{right_text}}}")["response"]
        second_set = registry.evaluate(f"set union {{{right_text}}} and {{{left_text}}}")["response"]
        assert first_set == second_set

    forward = registry.evaluate("convert 123.5 km to mi")["response"].split()[0]
    reverse = registry.evaluate(f"convert {forward} mi to km")["response"].split()[0]
    assert abs(Decimal(reverse) - Decimal("123.5")) < Decimal("0.000000000001")
    assert registry.evaluate("compare version 1.2.3 and 2.0.0")["response"] == "1.2.3 < 2.0.0"
    assert registry.evaluate("compare version 2.0.0 and 1.2.3")["response"] == "2.0.0 > 1.2.3"


def test_bounded_random_inputs_never_escape_the_closed_result_contract() -> None:
    registry = enabled_registry()
    randomizer = random.Random(1406)
    alphabet = string.ascii_letters + string.digits + string.punctuation + " \t"
    for _ in range(500):
        payload = "".join(randomizer.choice(alphabet) for _ in range(randomizer.randint(0, 200)))
        result = registry.evaluate(payload)
        assert result["status"] in {"resolved", "rejected", "miss", "failed"}
        assert set(result) == {
            "status",
            "plugin_name",
            "plugin_version",
            "contract_version",
            "response",
            "canonical_input",
            "error_code",
            "operations",
        }
        assert len(result["response"].encode("utf-8")) <= 2_048


def test_unexpected_plugin_failure_is_contained(monkeypatch) -> None:
    def fail(text: str) -> tuple[str, str, int]:
        del text
        raise RuntimeError("injected")

    evaluators = dict(utility_module.UTILITY_EVALUATORS)
    evaluators["arithmetic_v1"] = fail
    monkeypatch.setattr(utility_module, "UTILITY_EVALUATORS", MappingProxyType(evaluators))

    result = enabled_registry(("arithmetic_v1",)).evaluate("calculate 1 + 1")

    assert result["status"] == "failed"
    assert result["error_code"] == "plugin_failure"


def test_config_round_trip_preserves_independent_plugin_selection() -> None:
    config = engram_config(utility=utility_config(enabled=True, plugins=("arithmetic_v1", "version_v1")))

    restored = config_from_dict(config_to_dict(config))

    assert restored["utility"] == config["utility"]


def test_yaml_config_loads_selected_plugins_and_rejects_unknown_keys(tmp_path) -> None:
    selected = tmp_path / "selected.yml"
    selected.write_text("utility:\n  enabled: true\n  plugins: [arithmetic_v1, version_v1]\n", encoding="utf-8")
    invalid = tmp_path / "invalid.yml"
    invalid.write_text("utility:\n  enabled: false\n  module: os\n", encoding="utf-8")

    loaded = load_config(str(selected))

    assert loaded["utility"] == utility_config(enabled=True, plugins=("arithmetic_v1", "version_v1"))
    with pytest.raises(ValueError, match="module"):
        load_config(str(invalid))


def test_core_resolves_utility_without_learning_or_accounting() -> None:
    engram = Engram(engram_config(utility=utility_config(enabled=True)))
    core = EngramCore(engram, checkpoint_on_mutation=False)
    try:
        result = core.resolve_request(
            "calculate 2 + 3 * 4",
            "utility-integration",
            user_id="Sarah",
            configured_resolvers=("utility",),
        )
        filtered = core.resolve_request(
            "calculate 2 + 3 * 4",
            "utility-filtered",
            user_id="Sarah",
            required_source_label="knowledge-source",
            configured_resolvers=("utility",),
        )
    finally:
        core.close(flush=False)

    assert result["outcome"] == ResolutionOutcome.ANSWER
    assert result["selected_candidate"]["response"] == "14"
    assert result["selected_candidate"]["source"] == CandidateSource.UTILITY
    assert result["resolver_results"][0]["accounting"] == ()
    assert result["resolver_results"][0]["candidates"][0]["provenance"]["learnable"] is False
    assert engram.response_repository.snapshot()["artifacts"] == {}
    assert engram.statements == []
    assert filtered["outcome"] == ResolutionOutcome.MISS
    assert filtered["resolver_results"][0]["reason_code"] == "utility_filters_unsupported"


def test_authority_reexecutes_plugin_and_rejects_a_forged_response() -> None:
    engram = Engram(engram_config(utility=utility_config(enabled=True)))
    core = EngramCore(engram, checkpoint_on_mutation=False)
    try:
        result = core.resolve_request(
            "boolean true and false",
            "utility-authority",
            configured_resolvers=("utility",),
        )
        frame = validate_query_frame(core._resolution_requests["utility-authority"]["frame"])
        candidate = result["resolver_results"][0]["candidates"][0]
        forged = candidate_with_changes(candidate, {"response": "true"})
        authority = EngramCandidateAuthority(engram)

        authentic = authority(candidate, frame)
        rejected = authority(forged, frame)
    finally:
        core.close(flush=False)

    assert authentic["answer_eligible"] is True
    assert rejected["answer_eligible"] is False


def test_named_authority_evaluation_cannot_select_an_unlisted_plugin() -> None:
    result = evaluate_named_utility("calculate 1 + 1", "os.system")

    assert result["status"] == "rejected"
    assert result["error_code"] == "invalid_boundary"
