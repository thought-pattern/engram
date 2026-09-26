"""Startup loading of the seed files named by conversation config.

These tests use temporary files. They do not pin prose from data/seed.json.
"""

from json import dumps as json_dumps
from os import path as os_path
from pathlib import Path

from pytest import raises as pytest_raises

from engram.config import config_from_dict, config_to_dict, conversation_config, engram_config, load_config
from engram.core import Engram, load_seed_files


def write_seed(path: Path, pairs: list) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json_dumps({"pairs": pairs}), encoding="utf-8")
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
