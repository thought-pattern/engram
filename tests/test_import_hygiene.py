"""Import-hygiene guards.

Two invariants for the engram package, enforced at test time:

1. No dynamic (function-local) imports - every import is at module top.
2. The intra-package import graph is acyclic (a DAG / tree).

Dynamic imports are banned because they hide circular dependencies: a real
import cycle then lurks behind a lazy import instead of failing loudly. Forcing
every import to the top is the discipline that keeps the graph a tree.
"""

from ast import (
    AsyncFunctionDef as ast_AsyncFunctionDef,
    FunctionDef as ast_FunctionDef,
    Import as ast_Import,
    ImportFrom as ast_ImportFrom,
    Module as ast_Module,
    parse as ast_parse,
    walk as ast_walk,
)
from os import listdir as os_listdir, path as os_path

ENGRAM_DIR = os_path.join(os_path.dirname(os_path.dirname(os_path.abspath(__file__))), "engram")


def _engram_files() -> list[str]:
    _return_value = [os_path.join(ENGRAM_DIR, f) for f in sorted(os_listdir(ENGRAM_DIR)) if f.endswith(".py")]
    return _return_value


def _parse(path: str) -> ast_Module:
    with open(path, encoding="utf-8") as f:
        _return_value = ast_parse(f.read(), filename=path)
        return _return_value


def test_no_dynamic_imports() -> bool:
    """No import statement may appear inside a function or method in engram/."""
    offenders = set()
    for path in _engram_files():
        tree = _parse(path)
        for func in ast_walk(tree):
            if not isinstance(func, (ast_FunctionDef, ast_AsyncFunctionDef)):
                continue
            for node in ast_walk(func):
                if isinstance(node, (ast_Import, ast_ImportFrom)):
                    offenders.add(f"{os_path.basename(path)}:{node.lineno}")
    assert not offenders, "Dynamic (function-local) imports are forbidden: " + ", ".join(sorted(offenders))
    return False


def _import_graph() -> dict[str, set[str]]:
    graph: dict[str, set[str]] = {}
    for path in _engram_files():
        mod = os_path.basename(path)[:-3]
        deps: set[str] = set()
        for node in ast_walk(_parse(path)):
            if isinstance(node, ast_ImportFrom) and node.module and node.module.startswith("engram"):
                parts = node.module.split(".")
                if len(parts) >= 2:
                    deps.add(parts[1])  # engram.X -> X
                else:
                    deps.update(a.name for a in node.names)  # from engram import X
            elif isinstance(node, ast_Import):
                for a in node.names:
                    if a.name.startswith("engram."):
                        deps.add(a.name.split(".")[1])
        deps.discard(mod)
        graph[mod] = deps
    return graph


def test_import_graph_is_acyclic() -> bool:
    """The engram.* module import graph must be a DAG (no circular imports)."""
    graph = _import_graph()
    white, gray, black = 0, 1, 2
    color = dict.fromkeys(graph, white)
    stack: list[str] = []
    cycles: list[str] = []

    def visit(u: str) -> bool:
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
        return False

    for node in sorted(graph):
        if color[node] == white:
            visit(node)

    assert not cycles, "Circular imports detected: " + "; ".join(cycles)
    return False
