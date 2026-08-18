"""Repository-wide executable checks for the Engram-specific code-style rules."""

import ast
from pathlib import Path

REPOSITORY = Path(__file__).resolve().parent.parent
PYTHON_ROOTS = tuple(REPOSITORY / name for name in ("engram", "scripts", "eval", "tests"))
GENERATED_DIRECTORY = REPOSITORY / "engram" / "v1"
STATEFUL_CONTAINER_BASES = frozenset({"deque", "dict", "list", "set"})


def repository_python_modules() -> tuple[Path, ...]:
    """Return every governed Python module except reproducible generated files."""
    modules = []
    for root in PYTHON_ROOTS:
        for path in root.rglob("*.py"):
            if GENERATED_DIRECTORY in path.parents:
                continue
            modules.append(path)
    result = tuple(sorted(modules))
    return result


def parsed_modules() -> tuple[tuple[Path, ast.Module], ...]:
    """Parse the complete governed Python repository."""
    result = tuple((path, ast.parse(path.read_text(encoding="utf-8"), filename=str(path))) for path in repository_python_modules())
    return result


def annotation_nodes(tree: ast.AST) -> tuple[ast.expr, ...]:
    """Collect every function and variable annotation in one syntax tree."""
    annotations = []
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
    result = tuple(annotations)
    return result


def annotation_uses_union(annotation: ast.AST) -> bool:
    """Return whether an annotation uses pipe, Optional, or Union syntax."""
    for node in ast.walk(annotation):
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
            result = True
            return result
        if isinstance(node, (ast.Name, ast.Attribute)) and getattr(node, "id", getattr(node, "attr", "")) in {
            "Optional",
            "Union",
        }:
            result = True
            return result
        if (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and (" | " in node.value or "Optional[" in node.value or "Union[" in node.value)
        ):
            result = True
            return result
    result = False
    return result


def base_name(base: ast.expr) -> str:
    """Return the terminal name of one class base expression."""
    rendered = ast.unparse(base)
    result = rendered.split(".")[-1]
    return result


def class_writes_instance_state(node: ast.ClassDef) -> bool:
    """Return whether a class explicitly stores or deletes instance state."""
    for child in ast.walk(node):
        if not isinstance(child, ast.Attribute):
            continue
        if not isinstance(child.value, ast.Name) or child.value.id not in {"self", "cls"}:
            continue
        if isinstance(child.ctx, (ast.Store, ast.Del)):
            result = True
            return result
    result = False
    return result


def is_enum_or_exception(node: ast.ClassDef) -> bool:
    """Return whether a class is a permitted enum or exception type."""
    names = tuple(base_name(base) for base in node.bases)
    result = any(name.endswith(("Enum", "Error", "Exception")) or name in {"Exception", "ValueError"} for name in names)
    return result


def is_type_declaration(target: ast.Name, value: ast.expr) -> bool:
    """Return whether a module assignment declares a functional type contract."""
    if isinstance(value, ast.Call) and isinstance(value.func, ast.Name) and value.func.id in {"TypedDict", "TypeVar"}:
        result = True
        return result
    if target.id[:1].isupper() and isinstance(value, (ast.Name, ast.Attribute, ast.Subscript)):
        result = True
        return result
    result = False
    return result


def is_derived_module_state(path: Path, target: ast.Name, value: ast.expr) -> bool:
    """Classify the narrow runtime state derived from central or generated definitions."""
    rendered = ast.unparse(value)
    if target.id in {"logger", "LOGGER"} and rendered == "logging.getLogger(__name__)":
        result = True
        return result
    if path.name == "grpc_server.py" and target.id == "SERVICE_NAME" and "DESCRIPTOR.services_by_name" in rendered:
        result = True
        return result
    if path.name == "grpc_server.py" and target.id == "outcome_names" and "GRPC_REGULATOR_OUTCOME_NAMES" in rendered:
        result = True
        return result
    if path.name == "template.py" and target.id.endswith("_pattern"):
        result = rendered.startswith("re.compile(TEMPLATE_")
        return result
    result = False
    return result


def test_returns_only_names_or_bare_procedure_returns() -> None:
    """Every governed return must expose a previously collected name."""
    violations = []
    for path, tree in parsed_modules():
        for node in ast.walk(tree):
            if isinstance(node, ast.Return) and node.value and not isinstance(node.value, ast.Name):
                violations.append(f"{path.relative_to(REPOSITORY)}:{node.lineno}: {ast.unparse(node.value)}")
    assert violations == []


def test_records_use_functional_dictionaries_without_dataclasses() -> None:
    """Reject dataclass records and class-syntax TypedDict namespaces."""
    violations = []
    for path, tree in parsed_modules():
        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef):
                continue
            decorators = tuple(ast.unparse(decorator) for decorator in node.decorator_list)
            if any(decorator.split(".")[-1] == "dataclass" for decorator in decorators):
                violations.append(f"{path.relative_to(REPOSITORY)}:{node.lineno}: dataclass {node.name}")
            if any(base_name(base) == "TypedDict" for base in node.bases):
                violations.append(f"{path.relative_to(REPOSITORY)}:{node.lineno}: class TypedDict {node.name}")
    assert violations == []


def test_annotations_do_not_use_union_types() -> None:
    """Keep every governed annotation concrete rather than variant-shaped."""
    violations = []
    for path, tree in parsed_modules():
        for annotation in annotation_nodes(tree):
            if annotation_uses_union(annotation):
                violations.append(f"{path.relative_to(REPOSITORY)}:{annotation.lineno}: {ast.unparse(annotation)}")
    assert violations == []


def test_classes_own_persistent_state_and_contain_no_static_namespaces() -> None:
    """Allow classes only for explicit or inherited stateful operations."""
    classes = []
    violations = []
    stateful_names = set(STATEFUL_CONTAINER_BASES)
    for path, tree in parsed_modules():
        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef):
                continue
            classes.append((path, node))
            for method in node.body:
                if not isinstance(method, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                decorators = tuple(ast.unparse(decorator) for decorator in method.decorator_list)
                if "staticmethod" in decorators or "classmethod" in decorators:
                    violations.append(f"{path.relative_to(REPOSITORY)}:{method.lineno}: {node.name}.{method.name}")
            if class_writes_instance_state(node):
                stateful_names.add(node.name)

    changed = True
    while changed:
        changed = False
        for _path, node in classes:
            if node.name in stateful_names or is_enum_or_exception(node):
                continue
            if any(base_name(base) in stateful_names for base in node.bases):
                stateful_names.add(node.name)
                changed = True

    for path, node in classes:
        if node.name not in stateful_names and not is_enum_or_exception(node):
            violations.append(f"{path.relative_to(REPOSITORY)}:{node.lineno}: stateless class {node.name}")
    assert violations == []


def test_application_module_state_is_centralized_or_explicitly_derived() -> None:
    """Keep application module and class values centralized or explicitly derived."""
    violations = []
    constants_path = REPOSITORY / "engram" / "constants.py"
    for path, tree in parsed_modules():
        if path.parent != constants_path.parent or path == constants_path:
            continue
        for node in tree.body:
            assignments = []
            if isinstance(node, ast.Assign):
                assignments = [(target, node.value) for target in node.targets]
            elif isinstance(node, ast.AnnAssign) and node.value:
                assignments = [(node.target, node.value)]
            for target, value in assignments:
                if not isinstance(target, ast.Name):
                    continue
                if is_type_declaration(target, value) or is_derived_module_state(path, target, value):
                    continue
                violations.append(f"{path.relative_to(REPOSITORY)}:{node.lineno}: {target.id} = {ast.unparse(value)}")
            if not isinstance(node, ast.ClassDef):
                continue
            for member in node.body:
                class_assignments = []
                if isinstance(member, ast.Assign):
                    class_assignments = [(target, member.value) for target in member.targets]
                elif isinstance(member, ast.AnnAssign) and member.value:
                    class_assignments = [(member.target, member.value)]
                for target, value in class_assignments:
                    if isinstance(target, ast.Name):
                        violations.append(
                            f"{path.relative_to(REPOSITORY)}:{member.lineno}: {node.name}.{target.id} = {ast.unparse(value)}"
                        )
    assert violations == []
