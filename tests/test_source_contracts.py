"""Focused executable checks for architecture rules not covered by Ruff or Pyright."""

import ast
from pathlib import Path

REPOSITORY = Path(__file__).resolve().parent.parent
PACKAGE = REPOSITORY / "engram"
GENERATED = PACKAGE / "v1"


def _modules() -> tuple[tuple[Path, ast.Module], ...]:
    result = tuple(
        (path, ast.parse(path.read_text(encoding="utf-8"), filename=str(path)))
        for path in sorted(PACKAGE.rglob("*.py"))
        if GENERATED not in path.parents
    )
    return result


def _annotations(tree: ast.Module) -> tuple[ast.expr, ...]:
    values = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            arguments = [*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs]
            if node.args.vararg:
                arguments.append(node.args.vararg)
            if node.args.kwarg:
                arguments.append(node.args.kwarg)
            values.extend(argument.annotation for argument in arguments if argument.annotation)
            if node.returns:
                values.append(node.returns)
        elif isinstance(node, ast.AnnAssign):
            values.append(node.annotation)
    result = tuple(values)
    return result


def _uses_union(annotation: ast.expr) -> bool:
    result = False
    for node in ast.walk(annotation):
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
            result = True
            break
        if isinstance(node, (ast.Name, ast.Attribute)) and getattr(node, "id", getattr(node, "attr", "")) in {
            "Optional",
            "Union",
        }:
            result = True
            break
        if (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and (" | " in node.value or "Optional[" in node.value or "Union[" in node.value)
        ):
            result = True
            break
    return result


def test_production_imports_remain_eager_and_module_scoped() -> None:
    violations = []
    for path, tree in _modules():
        for function in (node for node in ast.walk(tree) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))):
            for node in ast.walk(function):
                if isinstance(node, (ast.Import, ast.ImportFrom)):
                    violations.append(f"{path.relative_to(REPOSITORY)}:{node.lineno}")
    assert violations == []


def test_production_annotations_use_one_concrete_type() -> None:
    violations = []
    for path, tree in _modules():
        for annotation in _annotations(tree):
            if _uses_union(annotation):
                violations.append(f"{path.relative_to(REPOSITORY)}:{annotation.lineno}: {ast.unparse(annotation)}")
    assert violations == []


def test_production_avoids_rigid_dictionary_typing_and_unneeded_frozen_sets() -> None:
    violations = []
    prohibited = {"TypedDict", "frozenset"}
    for path, tree in _modules():
        for node in ast.walk(tree):
            name = ""
            if isinstance(node, ast.Name):
                name = node.id
            elif isinstance(node, ast.Attribute):
                name = node.attr
            elif isinstance(node, ast.alias):
                name = node.name.rsplit(".", 1)[-1]
            if name in prohibited:
                violations.append(f"{path.relative_to(REPOSITORY)}:{getattr(node, 'lineno', 0)}: {name}")
    assert violations == []
