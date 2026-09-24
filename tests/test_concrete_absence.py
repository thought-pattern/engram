"""Behavioral checks for concrete absence values at public boundaries."""

from json import dumps as json_dumps, loads as json_loads

from pytest import raises as pytest_raises

from engram.config import config_from_dict, config_to_dict, engram_config
from engram.core import Engram
from engram.models import session_from_dict, statement_from_dict
from engram.pipeline import pipeline_result
from engram.service import EngramCore


def none_paths(value, path: str = "root") -> list[str]:
    if value is None:
        result = [path]
        return result
    if isinstance(value, dict):
        result = [nested for key, item in value.items() for nested in none_paths(item, f"{path}.{key}")]
        return result
    if isinstance(value, (list, tuple, set)):
        result = [nested for index, item in enumerate(value) for nested in none_paths(item, f"{path}[{index}]")]
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
        "status": core.status(),
        "pipeline": pipeline_result("", "none"),
        "fact": fact,
        "inspection": core.inspect_conversation("0"),
        "report": runtime.report(),
    }

    assert none_paths(values) == []
    assert "null" not in json_dumps(values, sort_keys=True)


def test_null_inputs_are_rejected_at_load_boundaries() -> None:
    external_null = json_loads("null")
    with pytest_raises(ValueError, match="must not be null"):
        statement_from_dict(
            {
                "id": "invalid",
                "text": "Invalid",
                "tier": "DYNAMIC",
                "created_at": "2026-01-01T00:00:00+00:00",
                "introduced_by_user_id": external_null,
            }
        )
    with pytest_raises(ValueError, match="must not be null"):
        session_from_dict(
            {
                "session_id": "invalid",
                "created_at": "2026-01-01T00:00:00+00:00",
                "last_active": "2026-01-01T00:00:00+00:00",
                "metadata": external_null,
            }
        )
    with pytest_raises(ValueError, match="must be an object"):
        config_from_dict({"graph": external_null})
