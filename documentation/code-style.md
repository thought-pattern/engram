# Engram Python Code Style

Engram follows the [Google Python Style Guide](https://google.github.io/styleguide/pyguide.html), subject to the project rules in this document. This document takes precedence wherever the rules differ; the Google guide governs all other Python style decisions.

## Imports

Engram permits and prefers imported symbols. Use the `from x import y` form for nested modules and for classes, functions, constants, and other symbols.

Use full, absolute package paths. Combine imports from the same source, sort them
with isort, and reserve aliases for collisions or clearer module roles.

Preferred:

```python
import json
from importlib import util
from pathlib import Path

import yaml

from engram import metrics, sessions
from engram.config import engram_config
from engram.service import EngramCore
from engram.text import normalize, stem_text
```

Avoid:

```python
import importlib.util
import engram.service
from . import sessions
from .config import engram_config
from engram.text import *
```

Engram runs exclusively on Python 3.12 and later. Never use `__future__` imports.

Do not import `typing` or `typing_extensions`. Use native Python types such as
`dict`, `list`, `tuple`, `set`, and `object` directly. Duck typing is acceptable;
omit an annotation when expressing it would require a typing-only construct. Do
not add casts, typing helpers, or compatibility imports to satisfy a static type
checker.

Keep imports at module scope, immediately after the module docstring. Group them in this order, with one blank line between groups:

1. Python standard-library imports.
2. Third-party imports.
3. Engram imports.

Generated protobuf and gRPC modules under `engram/v1/` and `engram/v2/` are
compiler output. Regenerate them from the corresponding `engram.proto`.

## Constants

Centralize application constants in `engram/constants.py`. A value that controls
behavior, defines a limit, names a policy or schema, supplies a fixed identifier,
or changes separately from an algorithm belongs in that module. Import the named
constant where it is used.

Values derived during one operation are local variables. Compiler-generated
constants remain in reproducible generated files.

## Project boundaries

Python modules, tests, scripts, and evaluation tools must not read from, write
to, or otherwise depend on the `documentation/` directory. Store test-owned
inputs under `tests/fixtures/` and generated evaluation results under
`eval/results/`. Documentation may describe those resources, but it is never a
runtime or test data source.

## Return statements

Use the clearest form for the operation. Directly return a simple expression when no
intermediate validation or explanation is needed. Assign a named result first when it
improves type narrowing, permits validation, makes a multi-step transformation easier
to inspect, or avoids repeating work. Side-effect-only procedures use a bare `return`
only when an early exit is needed.

Preferred:

```python
normalized = normalize(value)
result = {"value": normalized, "available": bool(normalized)}
return result
```

Avoid:

```python
return {"value": normalize(value), "available": "yes"}
```

The avoided example is invalid because `available` requires a Boolean.

## Concrete absence values

Represent absence with the falsey value of the field's concrete type: `""`, `[]`,
`{}`, `()`, `0`, `0.0`, `b""`, or `False`. When that value is meaningful, add a
Boolean presence field; `random_seed` and `random_seed_present` are the reference.
Normalize omitted external inputs at the adapter boundary.

Validate the concrete type before copying or normalizing a supplied value. A falsey
value of the wrong type is malformed input: for example, a mapping field accepts
`{}` and rejects `[]`, `()`, `""`, `0`, and `False`. Legacy persistence loaders may
translate a documented historical `null`; current public calls remain strict.

Procedures may retain `-> None` to describe a side-effect-only function.
Compiler-generated files under `engram/v1/` and `engram/v2/` remain byte-for-byte
reproducible from their `engram.proto` sources.

## Type annotations

Use one concrete type in production annotations and represent availability with a
Boolean. Normalize external variants at the boundary.

Use only native Python types in annotations. Never import `typing` or
`typing_extensions`, and never use `typing.cast`. Runtime behavior and clear
validation take precedence over satisfying a static type checker; duck-typed values do not need
an annotation merely to guide a static checker.

## Data structures

Use dictionaries for data contracts, decoded records, intermediate values, and
return payloads. When changing an existing dataclass contract, replace it with a
validated dictionary representation.

Use ordinary `dict` annotations and document meaningful fields through focused
docstrings and behavior tests. Validate external API input, persistence decoding,
configuration, graph records, and compatibility or resource-bound invariants.
Runtime behavior remains authoritative.

Use `set` by default and `frozenset` when hashability is required. An uppercase
module-level name denotes a constant; a lowercase local name may change during its
operation.

## Classes and functions

Use a class only when an operation owns state that persists across calls, such as a connection, repository, cache, lock-protected coordinator, or lifecycle-managed service. Keep that state explicit and keep the class responsible for its invariants.

Use module-level functions for stateless parsing, validation, normalization,
transformation, policy evaluation, codecs, calculations, and deterministic
selection. Pass required inputs explicitly and return concrete values.

## All Other Python Style

The Google Python Style Guide governs naming, docstrings, comments, exceptions,
comprehensions, type annotations, function design, and topics beyond these rules.

The runtime baseline is Python 3.12 or later. Format Python code automatically
with Black's intentionally retained Python 3.11 formatting mode, because the
project does not use Black's Python 3.12 formatting choices:

```bash
black -l 132 -t py311 .
```

Before submitting a change, run:

```bash
black -l 132 -t py311 .
isort --check-only .
ruff check --line-length 132 .
pytest
pytest tests/test_source_contracts.py
```
