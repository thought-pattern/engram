"""Behavioral checks for concrete absence values at public boundaries."""

from json import dumps as json_dumps

from engram.core import Engram
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
        "status": core.status(),
        "pipeline": pipeline_result("", "none"),
        "fact": fact,
        "inspection": core.inspect_conversation("0"),
        "report": runtime.report(),
    }

    assert none_paths(values) == []
    assert "null" not in json_dumps(values, sort_keys=True)
