"""Conformance, safety, and integration tests for Section 14 utilities."""

from decimal import Decimal
from random import Random as random_Random
from string import ascii_letters as string_ascii_letters, digits as string_digits, punctuation as string_punctuation

from pytest import mark as pytest_mark, raises as pytest_raises

from engram import utilities as utility_module
from engram.config import engram_config, load_config
from engram.constants import UTILITY_MAX_COLLECTION_ITEMS, CandidateSource
from engram.core import Engram
from engram.fusion import EngramCandidateAuthority
from engram.resolution import ResolutionOutcome, validate_candidate, validate_query_frame
from engram.service import EngramCore
from engram.utilities import UtilityRegistry, evaluate_named_utility, utility_config


@pytest_mark.parametrize(
    ("query", "plugin", "response"),
    [
        ("calculate 2 + 3 * 4", "arithmetic", "14"),
        ("arithmetic (2 + 3) ** 2", "arithmetic", "25"),
        ("boolean true and not false", "boolean", "true"),
        ("boolean false or true xor true", "boolean", "false"),
        ("set union {b,a} and {b,c}", "set", "{a, b, c}"),
        ("set symmetric difference {a,b} with {b,c}", "set", "{a, c}"),
        ("date 2024-02-28 plus 1 day", "date_time", "2024-02-29"),
        ("days between 2026-08-01 and 2026-08-22", "date_time", "21"),
        (
            "convert time 2026-08-22T14:30:00-04:00 to UTC",
            "date_time",
            "2026-08-22T18:30:00+00:00[UTC]",
        ),
        (
            "convert time 2026-08-22T14:30:00.100000+00:00 to UTC",
            "date_time",
            "2026-08-22T14:30:00.100000+00:00[UTC]",
        ),
        ("convert 32 F to C", "unit_conversion", "0 C"),
        ("convert 5 km to mi", "unit_conversion", "3.10685596118667 mi"),
        ("compare version 1.2.3-alpha.1 and 1.2.3", "version", "1.2.3-alpha.1 < 1.2.3"),
        ("compare version 1.2.3+first and 1.2.3+second", "version", "1.2.3+first = 1.2.3+second"),
        (
            "validate uuid 550e8400-e29b-41d4-a716-446655440000",
            "identifier",
            "valid uuid: 550e8400-e29b-41d4-a716-446655440000",
        ),
        ("validate slug cats-and-sushi", "identifier", "valid slug: cats-and-sushi"),
        ("validate slug Cats_and_sushi", "identifier", "invalid slug"),
    ],
)
def test_each_allowlisted_grammar_has_a_deterministic_canonical_result(query: str, plugin: str, response: str) -> None:
    registry = UtilityRegistry(utility_config(enabled=True))

    first = registry.evaluate(query)
    second = registry.evaluate(query)

    assert first == second
    assert first.get("status", "") == "resolved"
    assert first.get("plugin_name", "") == plugin
    assert first.get("response", "") == response
    assert "operations" in first
    assert first.get("operations", 0) <= 32


def test_registry_rejects_unknown_dynamic_plugin_names_and_duplicate_configuration() -> None:
    with pytest_raises(ValueError, match="unknown utility plugins"):
        utility_config(enabled=True, plugins=("pathlib.Path",))
    with pytest_raises(ValueError, match="duplicates"):
        utility_config(enabled=True, plugins=("arithmetic", "arithmetic"))
    with pytest_raises(ValueError, match="at least one"):
        utility_config(enabled=True, plugins=())


def test_default_off_and_independent_plugin_selection() -> None:
    disabled = UtilityRegistry()
    arithmetic_only = UtilityRegistry(utility_config(enabled=True, plugins=("arithmetic",)))

    assert disabled.evaluate("calculate 1 + 1").get("status", "") == "miss"
    assert arithmetic_only.evaluate("calculate 1 + 1").get("response", "") == "2"
    assert arithmetic_only.evaluate("boolean true").get("status", "") == "miss"
    plugin_health = arithmetic_only.health().get("plugins", {})
    arithmetic_health = plugin_health.get("arithmetic", {})
    boolean_health = plugin_health.get("boolean", {})
    assert arithmetic_health.get("ready", False) is True
    assert "ready" in boolean_health
    assert boolean_health.get("ready", False) is False


@pytest_mark.parametrize(
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
        ("compare version 1.2.3٠ and 1.2.30", "version_syntax"),
    ],
)
def test_malformed_or_ambiguous_inputs_are_stable_rejections(query: str, error_code: str) -> None:
    result = UtilityRegistry(utility_config(enabled=True)).evaluate(query)

    assert result.get("status", "") == "rejected"
    assert result.get("error_code", "") == error_code
    assert "response" in result
    assert result.get("response", "") == ""


def test_collection_and_operation_resource_limits_are_hard() -> None:
    items = ",".join(f"i{index}" for index in range(UTILITY_MAX_COLLECTION_ITEMS + 1))
    collection = UtilityRegistry(utility_config(enabled=True)).evaluate(f"set union {{{items}}} and {{x}}")
    operations = UtilityRegistry(utility_config(enabled=True)).evaluate("calculate " + " + ".join("1" for _ in range(34)))

    assert collection.get("status", "") == "rejected"
    assert collection.get("error_code", "") == "collection_limit"
    assert operations.get("status", "") == "rejected"
    assert operations.get("error_code", "") == "operation_limit"


def test_date_time_canonicalization_keeps_distinct_fractional_instants() -> None:
    first = evaluate_named_utility("convert time 2026-08-22T14:30:00.100000+00:00 to UTC", "date_time")
    second = evaluate_named_utility("convert time 2026-08-22T14:30:00.900000+00:00 to UTC", "date_time")

    assert first.get("status", "") == second.get("status", "") == "resolved"
    assert {"canonical_input", "response"} <= first.keys() & second.keys()
    assert first.get("canonical_input", "") != second.get("canonical_input", "")
    assert first.get("response", "") != second.get("response", "")


def test_expression_payload_is_data_and_never_executes(tmp_path) -> None:
    target = tmp_path / "executed.txt"
    payload = f"calculate __import__('pathlib').Path('{target}').write_text('bad')"

    result = UtilityRegistry(utility_config(enabled=True)).evaluate(payload)

    assert result.get("status", "") == "rejected"
    assert result.get("error_code", "") == "arithmetic_syntax"
    assert not target.exists()


def test_arithmetic_set_version_and_unit_properties() -> None:
    registry = UtilityRegistry(utility_config(enabled=True))
    randomizer = random_Random(1406)
    for _ in range(100):
        left = randomizer.randint(-10_000, 10_000)
        right = randomizer.randint(-10_000, 10_000)
        first = registry.evaluate(f"calculate {left} + {right}")
        second = registry.evaluate(f"calculate {right} + {left}")
        assert "response" in first.keys() & second.keys()
        assert first.get("response", "") == second.get("response", "")

        left_set = {f"i{randomizer.randint(0, 20)}" for _ in range(5)}
        right_set = {f"i{randomizer.randint(0, 20)}" for _ in range(5)}
        left_text = ",".join(sorted(left_set))
        right_text = ",".join(sorted(right_set))
        first_set = registry.evaluate(f"set union {{{left_text}}} and {{{right_text}}}")
        second_set = registry.evaluate(f"set union {{{right_text}}} and {{{left_text}}}")
        assert "response" in first_set.keys() & second_set.keys()
        assert first_set.get("response", "") == second_set.get("response", "")

    forward = registry.evaluate("convert 123.5 km to mi").get("response", "").split()[0]
    reverse = registry.evaluate(f"convert {forward} mi to km").get("response", "").split()[0]
    assert abs(Decimal(reverse) - Decimal("123.5")) < Decimal("0.000000000001")
    assert registry.evaluate("compare version 1.2.3 and 2.0.0").get("response", "") == "1.2.3 < 2.0.0"
    assert registry.evaluate("compare version 2.0.0 and 1.2.3").get("response", "") == "2.0.0 > 1.2.3"


def test_bounded_random_inputs_never_escape_the_closed_result_contract() -> None:
    registry = UtilityRegistry(utility_config(enabled=True))
    randomizer = random_Random(1406)
    alphabet = string_ascii_letters + string_digits + string_punctuation + " \t"
    for _ in range(500):
        payload = "".join(randomizer.choice(alphabet) for _ in range(randomizer.randint(0, 200)))
        result = registry.evaluate(payload)
        assert result.get("status", "") in {"resolved", "rejected", "miss", "failed"}
        assert set(result) == {
            "status",
            "plugin_name",
            "response",
            "canonical_input",
            "error_code",
            "operations",
        }
        assert len(result.get("response", "").encode("utf-8")) <= 2_048


def test_unexpected_plugin_failure_is_contained(monkeypatch) -> None:
    def fail(text: str) -> tuple[str, str, int]:
        del text
        raise RuntimeError("injected")

    evaluators = dict(utility_module.UTILITY_EVALUATORS)
    evaluators["arithmetic"] = fail
    monkeypatch.setattr(utility_module, "UTILITY_EVALUATORS", dict(evaluators))

    result = UtilityRegistry(utility_config(enabled=True, plugins=("arithmetic",))).evaluate("calculate 1 + 1")

    assert result.get("status", "") == "failed"
    assert result.get("error_code", "") == "plugin_failure"


def test_yaml_config_loads_selected_plugins_and_ignores_unknown_keys(tmp_path) -> None:
    selected = tmp_path / "selected.yml"
    selected.write_text("utility:\n  enabled: true\n  plugins: [arithmetic, version]\n", encoding="utf-8")
    unknown = tmp_path / "unknown.yml"
    unknown.write_text("utility:\n  enabled: false\n  module: os\n", encoding="utf-8")

    loaded = load_config(str(selected))

    assert loaded.get("utility", {}) == utility_config(enabled=True, plugins=("arithmetic", "version"))
    # An unknown key never reaches the utility settings, so it cannot name a module to load.
    assert load_config(str(unknown)).get("utility", {}) == utility_config(enabled=False)


def test_core_resolves_utility_without_learning_or_accounting() -> None:
    engram = Engram(engram_config(utility=utility_config(enabled=True)))
    core = EngramCore(engram)
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
        core.close()

    assert result.get("outcome", "") == ResolutionOutcome.ANSWER
    selected = result.get("selected_candidate", {})
    assert selected.get("response", "") == "14"
    assert selected.get("source", "") == CandidateSource.UTILITY
    resolver_result = result.get("resolver_results", ())[0]
    assert "accounting" in resolver_result
    assert resolver_result.get("accounting", ()) == ()
    provenance = resolver_result.get("candidates", ())[0].get("provenance", {})
    assert "learnable" in provenance
    assert provenance.get("learnable", False) is False
    repository_snapshot = engram.response_repository.snapshot()
    assert "artifacts" in repository_snapshot
    assert repository_snapshot.get("artifacts", {}) == {}
    assert engram.statements == []
    assert filtered.get("outcome", "") == ResolutionOutcome.MISS
    assert filtered.get("resolver_results", ())[0].get("reason_code", "") == "utility_filters_unsupported"


def test_authority_reexecutes_plugin_and_rejects_a_forged_response() -> None:
    engram = Engram(engram_config(utility=utility_config(enabled=True)))
    core = EngramCore(engram)
    try:
        result = core.resolve_request(
            "boolean true and false",
            "utility-authority",
            configured_resolvers=("utility",),
        )
        frame = validate_query_frame(core.resolution_requests.get("utility-authority", {}).get("frame", {}))
        candidate = result.get("resolver_results", ())[0].get("candidates", ())[0]
        forged = validate_candidate({**candidate, "response": "true"})
        authority = EngramCandidateAuthority(engram)

        authentic = authority(candidate, frame)
        rejected = authority(forged, frame)
    finally:
        core.close()

    assert authentic.get("answer_eligible", False) is True
    assert "answer_eligible" in rejected
    assert rejected.get("answer_eligible", False) is False


def test_named_authority_evaluation_cannot_select_an_unlisted_plugin() -> None:
    result = evaluate_named_utility("calculate 1 + 1", "os.system")

    assert result.get("status", "") == "rejected"
    assert result.get("error_code", "") == "invalid_boundary"
