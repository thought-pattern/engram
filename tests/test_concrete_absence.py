"""Architectural checks for concrete absence values and precise annotations."""

import ast
import json
from pathlib import Path

from engram import persistence
from engram.config import config_from_dict, config_to_dict, engram_config
from engram.core import Engram
from engram.models import session_from_dict, statement_from_dict
from engram.pipeline import pipeline_result
from engram.service import EngramCore

REPOSITORY = Path(__file__).resolve().parent.parent
PRODUCTION_ROOTS = (REPOSITORY / "engram", REPOSITORY / "scripts")
GENERATED_MODULES = {"engram_pb2.py", "engram_pb2.pyi", "engram_pb2_grpc.py"}


def _production_modules() -> list[Path]:
    return sorted(path for root in PRODUCTION_ROOTS for path in root.rglob("*.py") if path.name not in GENERATED_MODULES)


def _annotations(tree: ast.AST) -> list[ast.expr]:
    annotations: list[ast.expr] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            arguments = [*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs]
            if node.args.vararg:
                arguments.append(node.args.vararg)
            if node.args.kwarg:
                arguments.append(node.args.kwarg)
            annotations.extend(argument.annotation for argument in arguments if argument.annotation)
            if node.returns:
                annotations.append(node.returns)
        elif isinstance(node, ast.AnnAssign):
            annotations.append(node.annotation)
    return annotations


def _annotation_uses_union(annotation: ast.AST) -> bool:
    for node in ast.walk(annotation):
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
            return True
        if isinstance(node, ast.Subscript):
            name = node.value.id if isinstance(node.value, ast.Name) else getattr(node.value, "attr", "")
            if name in {"Optional", "Union"}:
                return True
        if (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and (" | " in node.value or "Optional[" in node.value or "Union[" in node.value)
        ):
            return True
    return False


def _none_paths(value, path: str = "root") -> list[str]:
    if value is None:
        return [path]
    if isinstance(value, dict):
        return [nested for key, item in value.items() for nested in _none_paths(item, f"{path}.{key}")]
    if isinstance(value, (list, tuple, set)):
        return [nested for index, item in enumerate(value) for nested in _none_paths(item, f"{path}[{index}]")]
    return []


def test_production_annotations_do_not_use_unions() -> None:
    violations = []
    for path in _production_modules():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for annotation in _annotations(tree):
            if _annotation_uses_union(annotation):
                violations.append(f"{path.relative_to(REPOSITORY)}:{annotation.lineno}")
    assert violations == []


def test_production_none_literals_only_describe_procedures() -> None:
    violations = []
    for path in _production_modules():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        procedural_annotations = {
            id(node.returns)
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and isinstance(node.returns, ast.Constant)
            and node.returns.value is None
        }
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and node.value is None and id(node) not in procedural_annotations:
                violations.append(f"{path.relative_to(REPOSITORY)}:{node.lineno}")
    assert violations == []


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
