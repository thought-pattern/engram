"""Behavioral checks for concrete absence values at public boundaries."""

import json

from engram import persistence
from engram.config import config_from_dict, config_to_dict, engram_config
from engram.core import Engram
from engram.models import session_from_dict, statement_from_dict
from engram.pipeline import pipeline_result
from engram.service import EngramCore


def _none_paths(value, path: str = "root") -> list[str]:
    if value is None:
        result = [path]
        return result
    if isinstance(value, dict):
        result = [nested for key, item in value.items() for nested in _none_paths(item, f"{path}.{key}")]
        return result
    if isinstance(value, (list, tuple, set)):
        result = [nested for index, item in enumerate(value) for nested in _none_paths(item, f"{path}[{index}]")]
        return result
    result = []
    return result


def test_representative_outputs_are_recursively_concrete() -> None:
    engram = Engram()
    core = EngramCore(engram)
    core.start_conversation(random_seed=0, random_seed_present=True)
    fact = core.add_fact("Tokyo is the capital of Japan.", source_label="research")
    runtime = core.get_conversation("0")

    values = {
        "config": config_to_dict(engram_config()),
        "persistence": persistence.to_dict(engram),
        "pipeline": pipeline_result("", "none"),
        "fact": fact,
        "inspection": core.inspect_conversation("0"),
        "report": runtime.report(),
    }

    assert _none_paths(values) == []
    assert "null" not in json.dumps(values, sort_keys=True)


def test_legacy_null_inputs_are_normalized_at_load_boundaries() -> None:
    statement = statement_from_dict(
        {
            "id": "legacy",
            "text": "Legacy",
            "tier": "DYNAMIC",
            "created_at": "2026-01-01T00:00:00+00:00",
            "introduced_by_user_id": None,
            "template": None,
        }
    )
    session = session_from_dict(
        {
            "session_id": "legacy",
            "created_at": "2026-01-01T00:00:00+00:00",
            "last_active": "2026-01-01T00:00:00+00:00",
            "metadata": None,
            "entities": None,
        }
    )
    config = config_from_dict({"graph": None})

    assert _none_paths({"statement": statement, "session": session, "config": config}) == []
    assert statement["introduced_by_user_id"] == ""
    assert statement["template"] == {}
    assert session["metadata"] == {}
    assert session["entities"] == []
    assert config["graph"] == {}
