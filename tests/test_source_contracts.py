"""Focused executable checks for architecture rules not covered by Ruff."""

import ast
import json
from pathlib import Path

REPOSITORY = Path(__file__).resolve().parent.parent
PACKAGE = REPOSITORY / "engram"
GENERATED_DIRECTORIES = (PACKAGE / "v1", PACKAGE / "v2")
FIRST_PARTY_ROOTS = ("engram", "scripts", "eval")
FIRST_PARTY_DIRECTORIES = tuple(REPOSITORY / name for name in FIRST_PARTY_ROOTS)
PYTHON_DIRECTORIES = (*FIRST_PARTY_DIRECTORIES, REPOSITORY / "tests")


def _modules() -> tuple[tuple[Path, ast.Module], ...]:
    result = tuple(
        (path, ast.parse(path.read_text(encoding="utf-8"), filename=str(path)))
        for path in sorted(PACKAGE.rglob("*.py"))
        if not any(directory in path.parents for directory in GENERATED_DIRECTORIES)
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


def _first_party_module_exists(name: str) -> bool:
    parts = name.split(".")
    path = REPOSITORY.joinpath(*parts)
    result = path.is_dir() or path.with_suffix(".py").is_file()
    return result


def _first_party_source_modules() -> tuple[tuple[Path, ast.Module], ...]:
    result = tuple(
        (path, ast.parse(path.read_text(encoding="utf-8"), filename=str(path)))
        for directory in FIRST_PARTY_DIRECTORIES
        for path in sorted(directory.rglob("*.py"))
    )
    return result


def _repository_python_modules() -> tuple[tuple[Path, ast.Module], ...]:
    result = tuple(
        (path, ast.parse(path.read_text(encoding="utf-8"), filename=str(path)))
        for directory in PYTHON_DIRECTORIES
        for path in sorted(directory.rglob("*.py"))
    )
    return result


def test_python_sources_do_not_import_future_or_typing() -> None:
    prohibited = {"__future__", "typing", "typing_extensions"}
    violations = []
    for path, tree in _repository_python_modules():
        for node in ast.walk(tree):
            imported_modules = []
            if isinstance(node, ast.Import):
                imported_modules.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported_modules.append(node.module)
            if any(name.split(".", 1)[0] in prohibited for name in imported_modules):
                violations.append(f"{path.relative_to(REPOSITORY)}:{node.lineno}: {', '.join(imported_modules)}")
    assert violations == []


def test_python_sources_do_not_access_documentation_directory() -> None:
    prohibited_directory = "".join(("doc", "umentation"))
    violations = []
    for path, tree in _repository_python_modules():
        for node in ast.walk(tree):
            if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
                continue
            components = node.value.replace("\\", "/").split("/")
            if prohibited_directory in components:
                violations.append(f"{path.relative_to(REPOSITORY)}:{node.lineno}: {node.value}")
    assert violations == []


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


def test_first_party_imports_resolve_to_repository_modules() -> None:
    violations = []
    for path, tree in _first_party_source_modules():
        for node in ast.walk(tree):
            imported_modules = []
            if isinstance(node, ast.Import):
                imported_modules.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                imported_modules.append(node.module)
                module_path = REPOSITORY.joinpath(*node.module.split("."))
                if module_path.is_dir():
                    imported_modules.extend(f"{node.module}.{alias.name}" for alias in node.names if alias.name != "*")
            for name in imported_modules:
                if name.split(".", 1)[0] in FIRST_PARTY_ROOTS and not _first_party_module_exists(name):
                    violations.append(f"{path.relative_to(REPOSITORY)}:{getattr(node, 'lineno', 0)}: {name}")
    assert violations == []


def test_section13_visible_data_remains_engineering_only() -> None:
    corpus_path = REPOSITORY / "eval" / "section13-semantic-v1.json"
    corpus = json.loads(corpus_path.read_text(encoding="utf-8"))
    partitions = {query["partition"] for query in corpus["queries"]}

    assert corpus["evaluation_role"] == "repository-visible engineering holdout; not a Section 16 release partition"
    assert corpus["section16_release_eligible"] is False
    assert partitions == {"train", "calibration", "engineering_holdout"}
    assert partitions.isdisjoint({"release", "release_gate", "final_test"})


def test_section13_timing_is_observed_without_a_gate() -> None:
    corpus_path = REPOSITORY / "eval" / "section13-semantic-v1.json"
    corpus = json.loads(corpus_path.read_text(encoding="utf-8"))

    for component in ("semantic", "reranker"):
        gate_names = corpus["gates"][component]
        assert all("_ms_" not in name and "latency" not in name for name in gate_names)


def test_section16_timing_is_observed_without_a_gate() -> None:
    manifest_path = REPOSITORY / "eval" / "release-gate-foundation-v1.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    gates = manifest["numerical_gates"]

    assert gates["turn_length_reporting"] == {
        "metrics": ["p50_ms", "p95_ms", "p99_ms", "max_ms"],
        "pass_fail": False,
    }
    assert all("latency" not in name and "startup" not in name for name in gates)
