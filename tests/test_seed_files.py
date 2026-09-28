"""Startup loading of the seed files named by conversation config.

These tests use temporary files. They do not pin prose from data/seed.json.
"""

from json import dumps as json_dumps, loads as json_loads
from os import path as os_path
from pathlib import Path

from pytest import raises as pytest_raises

from engram.config import config_from_dict, config_to_dict, conversation_config, engram_config, load_config
from engram.constants import VERSION
from engram.core import Engram, load_seed_files
from engram.pattern import normalize_pattern


def write_seed(path: Path, pairs: list) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json_dumps({"pairs": pairs}), encoding="utf-8")
    result = str(path)
    return result


def write_json(path: Path, payload: dict) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json_dumps(payload), encoding="utf-8")
    result = str(path)
    return result


def test_seed_files_load_in_order_and_each_pattern_answers(tmp_path):
    first = write_seed(
        tmp_path / "first.json",
        [{"pattern": "FIRST FILE PATTERN", "response": "Noted, first-file-marker."}],
    )
    second = write_seed(
        tmp_path / "second.json",
        [{"pattern": "SECOND FILE PATTERN", "response": "Noted, second-file-marker."}],
    )
    engram = Engram(config=engram_config(conversation=conversation_config(seed_files=[first, second])))

    patterns = [statement["pattern"] for statement in engram.statements]
    assert patterns == ["FIRST FILE PATTERN", "SECOND FILE PATTERN"]
    first_result = engram.pattern_query("first file pattern")
    second_result = engram.pattern_query("second file pattern")
    assert first_result and "first-file-marker" in first_result[2]
    assert second_result and "second-file-marker" in second_result[2]


def test_empty_seed_files_leave_the_statement_store_empty():
    engram = Engram(config=engram_config(conversation=conversation_config(bot_name="Mara")))

    assert engram.statements == []
    assert engram.bot_properties["name"] == "Mara"


def test_missing_malformed_and_pairless_seeds_fail_before_statements_exist(tmp_path):
    missing = tmp_path / "missing.json"
    malformed = tmp_path / "malformed.json"
    malformed.write_text("{", encoding="utf-8")
    pairless = tmp_path / "pairless.json"
    pairless.write_text('{"categories": []}', encoding="utf-8")
    directory = tmp_path / "seed-dir"
    directory.mkdir()

    with pytest_raises(ValueError, match="missing or is not a file"):
        load_seed_files([str(missing)])
    with pytest_raises(ValueError, match="missing or is not a file"):
        load_seed_files([str(directory)])
    with pytest_raises(ValueError, match="not valid JSON"):
        load_seed_files([str(malformed)])
    with pytest_raises(ValueError, match="pairs"):
        load_seed_files([str(pairless)])

    for path, match in (
        (malformed, "not valid JSON"),
        (pairless, "requires a pairs array"),
        (missing, "missing or is not a file"),
    ):
        with pytest_raises(ValueError, match=match):
            Engram(config=engram_config(conversation=conversation_config(seed_files=[str(path)])))


def test_duplicate_pattern_names_both_files_and_stores_nothing(tmp_path):
    first = write_seed(tmp_path / "a.json", [{"pattern": "HELLO", "response": "Hi from the first file."}])
    second = write_seed(tmp_path / "b.json", [{"pattern": "HELLO", "response": "Hi from the second file."}])

    with pytest_raises(ValueError, match="HELLO") as caught:
        load_seed_files([first, second])
    message = str(caught.value)
    assert first in message
    assert second in message
    with pytest_raises(ValueError, match="HELLO"):
        Engram(config=engram_config(conversation=conversation_config(seed_files=[first, second])))


def test_relative_seed_path_resolves_from_the_config_directory(tmp_path, monkeypatch):
    project = tmp_path / "project"
    config_seed = project / "data" / "one.json"
    write_seed(config_seed, [{"pattern": "PING", "response": "config-directory-marker."}])
    (project / "config.yml").write_text("conversation:\n  seed_files:\n    - data/one.json\n", encoding="utf-8")
    elsewhere = tmp_path / "elsewhere"
    write_seed(elsewhere / "data" / "one.json", [{"pattern": "PING", "response": "working-directory-marker."}])
    monkeypatch.chdir(elsewhere)

    loaded = load_config(str(project / "config.yml"))
    expected = os_path.abspath(os_path.join(str(project), "data", "one.json"))
    assert os_path.normcase(loaded["conversation"]["seed_files"][0]) == os_path.normcase(expected)
    engram = Engram(config=loaded)
    result = engram.pattern_query("ping")
    assert result and "config-directory-marker" in result[2].lower()


def test_bot_name_renders_in_seed_responses(tmp_path):
    path = write_seed(
        tmp_path / "name.json",
        [{"pattern": "WHAT IS YOUR NAME", "response": "I'm {bot:name}."}],
    )
    engram = Engram(config=engram_config(conversation=conversation_config(bot_name="Mara", seed_files=[path])))

    result = engram.pattern_query("What is your name?")
    assert result and "Mara" in result[2]


def test_conversation_round_trip_keeps_absolute_paths_without_rereading_files(tmp_path):
    path = write_seed(tmp_path / "one.json", [{"pattern": "PING", "response": "Pong."}])
    config = engram_config(conversation=conversation_config(bot_name="Mara", seed_files=[path]))
    exported = config_to_dict(config)
    Path(path).unlink()

    restored = config_from_dict(exported)
    assert restored["conversation"] == config["conversation"]


def test_duplicate_policy_last_keeps_one_later_pair(tmp_path):
    first = write_seed(
        tmp_path / "a.json",
        [
            {"pattern": "HELLO", "response": "Hi from the first file."},
            {"pattern": "ONLY FIRST", "response": "First only."},
        ],
    )
    second = write_seed(
        tmp_path / "b.json",
        [
            {"pattern": "HELLO", "response": "Hi from the second file."},
            {"pattern": "ONLY SECOND", "response": "Second only."},
        ],
    )

    engram = Engram(
        config=engram_config(
            conversation=conversation_config(seed_files=[first, second], duplicate_policy="last"),
        )
    )
    patterns = [statement["pattern"] for statement in engram.statements]

    assert patterns.count("HELLO") == 1
    assert "ONLY FIRST" in patterns
    assert "ONLY SECOND" in patterns
    result = engram.pattern_query("hello")
    assert result and "second file" in result[2].lower()


def test_duplicate_policy_first_keeps_the_earlier_pair(tmp_path):
    first = write_seed(tmp_path / "a.json", [{"pattern": "HELLO", "response": "Hi from the first file."}])
    second = write_seed(tmp_path / "b.json", [{"pattern": "HELLO", "response": "Hi from the second file."}])

    engram = Engram(
        config=engram_config(
            conversation=conversation_config(seed_files=[first, second], duplicate_policy="first"),
        )
    )

    assert len(engram.statements) == 1
    result = engram.pattern_query("hello")
    assert result and "first file" in result[2].lower()


def test_duplicate_policy_rejects_an_unknown_value():
    with pytest_raises(ValueError, match="duplicate_policy"):
        conversation_config(duplicate_policy="maybe")


def test_hyphenated_questions_match_spaced_corpus_patterns():
    root = Path(__file__).resolve().parents[1] / "data"
    engram = Engram(
        config=engram_config(
            conversation=conversation_config(
                seed_files=[str(root / "software_development.json"), str(root / "python.json")],
            )
        )
    )

    mil = engram.pattern_query("What is MIL-STD-498?")
    fstring = engram.pattern_query("What is an f-string?")

    assert mil and mil[0]["pattern"] == "WHAT IS MIL STD 498"
    assert fstring and fstring[0]["pattern"] == "WHAT IS AN F STRING"


def test_seed_patterns_stay_unique_when_hyphens_become_spaces():
    root = Path(__file__).resolve().parents[1] / "data"
    names = [
        "seed.json",
        "python.json",
        "software_development.json",
        "software_requirements.json",
        "software_design.json",
        "software_testing.json",
        "software_delivery.json",
    ]
    seen = {}
    for name in names:
        pairs = json_loads((root / name).read_text(encoding="utf-8"))["pairs"]
        for pair in pairs:
            key = (
                normalize_pattern(pair["pattern"]),
                normalize_pattern(pair.get("that", "")),
                normalize_pattern(pair.get("topic", "")),
            )
            assert key not in seen, f"{pair['pattern']!r} in {name} collides with {seen[key]}"
            seen[key] = f"{name}: {pair['pattern']}"


def test_set_file_and_seed_file_match_a_set_member(tmp_path):
    sets = write_json(tmp_path / "colors.json", {"color": ["red", "blue"]})
    seed = write_seed(
        tmp_path / "seed.json",
        [{"pattern": "I LIKE {set:color}", "response": "{star1} is a nice color."}],
    )
    engram = Engram(config=engram_config(conversation=conversation_config(set_files=[sets], seed_files=[seed])))

    result = engram.pattern_query("I like blue")

    assert result
    assert "blue" in result[2].lower()
    assert "nice color" in result[2].lower()
    assert not engram.pattern_query("I like purple")


def test_later_set_file_replaces_the_same_set_name(tmp_path):
    first = write_json(tmp_path / "first.json", {"color": ["red"]})
    second = write_json(tmp_path / "second.json", {"color": ["blue"]})
    seed = write_seed(
        tmp_path / "seed.json",
        [{"pattern": "I LIKE {set:color}", "response": "{star1} is a nice color."}],
    )
    engram = Engram(config=engram_config(conversation=conversation_config(set_files=[first, second], seed_files=[seed])))

    assert engram.pattern_query("I like blue")
    assert not engram.pattern_query("I like red")


def test_missing_set_file_names_the_config_path(tmp_path):
    config_path = tmp_path / "config.yml"
    config_path.write_text("conversation:\n  set_files:\n    - data/missing-set.json\n", encoding="utf-8")

    with pytest_raises(ValueError, match="missing-set.json") as caught:
        load_config(str(config_path))

    assert str(config_path) in str(caught.value)


def test_predicate_file_supplies_get_name_on_a_new_session(tmp_path):
    predicates = write_json(tmp_path / "predicates.json", {"name": "Robin"})
    seed = write_seed(
        tmp_path / "seed.json",
        [{"pattern": "HELLO", "response": "Hello, {get:name}."}],
    )
    engram = Engram(config=engram_config(conversation=conversation_config(predicate_file=predicates, seed_files=[seed])))

    result = engram.pattern_query("hello", context_id="robin")

    assert result[2] == "Hello, Robin."
    assert engram.sessions["robin"]["predicates"]["name"] == "Robin"


def test_bot_name_wins_over_the_properties_file(tmp_path):
    properties = write_json(
        tmp_path / "properties.json",
        {"name": "FILEBOT", "version": "9.9.9", "city": "Kyoto"},
    )
    engram = Engram(config=engram_config(conversation=conversation_config(bot_name="Mara", properties_file=properties)))

    assert engram.bot_properties["name"] == "Mara"
    assert engram.bot_properties["version"] == VERSION
    assert engram.bot_properties["city"] == "Kyoto"


def test_custom_substitution_applies_before_the_pattern_walk(tmp_path):
    substitutions = write_json(tmp_path / "subs.json", {"custom": {"colour": "color"}})
    seed = write_seed(tmp_path / "seed.json", [{"pattern": "COLOR", "response": "A color."}])
    engram = Engram(config=engram_config(conversation=conversation_config(substitution_file=substitutions, seed_files=[seed])))

    result = engram.pattern_query("colour")

    assert result
    assert result[0]["pattern"] == "COLOR"
    assert "color" in result[2].lower()
