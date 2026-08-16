"""Import-hygiene guards.

Two invariants for the engram package, enforced at test time:

1. No dynamic (function-local) imports - every import is at module top.
2. The intra-package import graph is acyclic (a DAG / tree).

Dynamic imports are banned because they hide circular dependencies: a real
import cycle then lurks behind a lazy import instead of failing loudly. Forcing
every import to the top is the discipline that keeps the graph a tree.
"""

import ast
import os

ENGRAM_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "engram")
REPOSITORY_ROOT = os.path.dirname(ENGRAM_DIR)


def _engram_files() -> list[str]:
    return [os.path.join(ENGRAM_DIR, f) for f in sorted(os.listdir(ENGRAM_DIR)) if f.endswith(".py")]


def _parse(path: str) -> ast.Module:
    with open(path, encoding="utf-8") as f:
        return ast.parse(f.read(), filename=path)


def test_no_dynamic_imports() -> None:
    """No import statement may appear inside a function or method in engram/."""
    offenders = set()
    for path in _engram_files():
        tree = _parse(path)
        for func in ast.walk(tree):
            if not isinstance(func, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            for node in ast.walk(func):
                if isinstance(node, ast.Import | ast.ImportFrom):
                    offenders.add(f"{os.path.basename(path)}:{node.lineno}")
    assert not offenders, "Dynamic (function-local) imports are forbidden: " + ", ".join(sorted(offenders))


def _import_graph() -> dict[str, set[str]]:
    graph: dict[str, set[str]] = {}
    for path in _engram_files():
        mod = os.path.basename(path)[:-3]
        deps: set[str] = set()
        for node in ast.walk(_parse(path)):
            if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("engram"):
                parts = node.module.split(".")
                if len(parts) >= 2:
                    deps.add(parts[1])  # engram.X -> X
                else:
                    deps.update(a.name for a in node.names)  # from engram import X
            elif isinstance(node, ast.Import):
                for a in node.names:
                    if a.name.startswith("engram."):
                        deps.add(a.name.split(".")[1])
        deps.discard(mod)
        graph[mod] = deps
    return graph


def test_import_graph_is_acyclic() -> None:
    """The engram.* module import graph must be a DAG (no circular imports)."""
    graph = _import_graph()
    white, gray, black = 0, 1, 2
    color = dict.fromkeys(graph, white)
    stack: list[str] = []
    cycles: list[str] = []

    def visit(u: str) -> None:
        color[u] = gray
        stack.append(u)
        for v in graph.get(u, ()):
            if v not in graph:
                continue
            if color[v] == gray:
                cycles.append(" -> ".join(stack[stack.index(v) :] + [v]))
            elif color[v] == white:
                visit(v)
        stack.pop()
        color[u] = black

    for node in sorted(graph):
        if color[node] == white:
            visit(node)

    assert not cycles, "Circular imports detected: " + "; ".join(cycles)


def test_requirements_have_one_pin_per_distribution() -> None:
    """The reproducibility input cannot contain contradictory exact pins."""
    requirements_path = os.path.join(REPOSITORY_ROOT, "requirements.txt")
    observed: dict[str, str] = {}
    duplicates = []
    with open(requirements_path, encoding="utf-8") as requirements:
        for raw_line in requirements:
            line = raw_line.split("#", 1)[0].strip()
            if not line or "==" not in line:
                continue
            name, version = (value.strip() for value in line.split("==", 1))
            normalized = name.lower().replace("_", "-")
            if normalized in observed:
                duplicates.append(f"{normalized}=={observed[normalized]} and {version}")
            observed[normalized] = version
    assert not duplicates, "requirements.txt contains duplicate exact pins: " + ", ".join(duplicates)
