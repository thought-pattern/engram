# Engram Python Code Style

Engram follows the [Google Python Style Guide](https://google.github.io/styleguide/pyguide.html), subject to the project rules in this document. This document takes precedence wherever the rules differ; the Google guide governs all other Python style decisions.

## Imports

Engram intentionally differs from the Google Python Style Guide by allowing and preferring imported symbols. Use the `from x import y` form for nested modules and for classes, functions, constants, and other symbols.

Use full, absolute package paths. Do not use relative imports. Combine imports from the same source, sort them with isort, and use aliases only to avoid a collision or make a module's role clearer.

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

Keep imports at module scope, immediately after the module docstring. Group them in this order, with one blank line between groups:

1. Future imports.
2. Python standard-library imports.
3. Third-party imports.
4. Engram imports.

Generated protobuf and gRPC modules under `engram/v1/` are compiler output. Regenerate them from `engram.proto`; do not edit or reformat them by hand.

## Constants

Centralize application constants in `engram/constants.py`. A value that controls behavior, defines a limit, names a policy or schema, supplies a fixed identifier, or is intended to be changed independently of an algorithm belongs in that constants module. Import the named constant where it is used instead of redefining it beside the implementation or copying its literal value into multiple modules.

Local variables whose values are derived during one operation are not constants. Compiler-generated constants remain in their generated files, which must stay reproducible from their source definitions.

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

The avoided example is invalid because `available` is not a Boolean, not because the
dictionary is returned directly.

## Concrete absence values

Do not generate, store, or return `None` as an absence value. Use the falsey value of the field's concrete type: `""`, `[]`, `{}`, `()`, `0`, `0.0`, `b""`, or `False`. When that value is also a meaningful observation, add a separate Boolean presence field; `random_seed` and `random_seed_present` are the reference example. Normalize omitted external inputs at the adapter boundary and never emit JSON `null`.

Validate the concrete type before copying or normalizing a supplied value. A falsey value of the wrong type is malformed input, not an omission: for example, a mapping field accepts `{}` but rejects `[]`, `()`, `""`, `0`, and `False`. Legacy persistence loaders may translate a specifically documented historical `null` to the current concrete empty value, but new public calls remain strict.

Procedures may retain `-> None` because that annotation describes a side-effect-only function rather than an absent data value. Compiler-generated files under `engram/v1/` are exempt from the absence rule and remain byte-for-byte reproducible from `engram.proto`.

## Type annotations

Do not use union types, including `Optional`, `X | Y`, or `Union[X, Y]`, in production annotations. Give each value one concrete type and represent availability separately with a Boolean when necessary. Normalize external variants at the boundary before passing data into production code.

## Data structures

Use dictionaries instead of `@dataclass` records. Data contracts, decoded records, intermediate values, and return payloads must be dictionaries with explicit validation and concrete fields. Do not introduce new dataclasses; replace an existing dataclass with a validated dictionary representation when changing that contract within the scope of the work.

When a dictionary needs a precise static type, declare its `TypedDict` with functional syntax and construct runtime values through a validating function. Class-syntax `TypedDict` declarations are stateless type namespaces and therefore conflict with the class rule below. Ruff rule `UP013` is disabled for this reason.

Use `set` instead of `frozenset`. Follow ordinary Python naming conventions to distinguish constants from variables: an uppercase module-level name denotes a constant and must not be mutated, while a lowercase local name may be updated during its operation. Do not use `frozenset` merely to signal that a value is constant. Use it only when hashability is required, such as when a set must be a dictionary key, a member of another set, or an input to an API that explicitly requires `frozenset`.

## Classes and functions

Use a class only when an operation owns state that persists across calls, such as a connection, repository, cache, lock-protected coordinator, or lifecycle-managed service. Keep that state explicit and keep the class responsible for its invariants.

Use module-level functions for stateless behavior. Parsing, validation, normalization, transformation, policy evaluation, codecs, calculations, and deterministic selection should be functions that receive all required inputs and return validated dictionaries or other concrete values. Do not create a class merely to namespace related functions.

## All Other Python Style

Follow the Google Python Style Guide for naming, docstrings, comments, exceptions, comprehensions, type annotations, function design, and every topic not explicitly overridden above.

Format Python code automatically with Black's 132-character line length and Python 3.11 output target:

```bash
black -l 132 -t py311 .
```

Before submitting a change, run:

```bash
black --check -l 132 -t py311 .
isort --check-only .
ruff check --line-length 132 .
pyright
pytest
pytest tests/test_source_contracts.py
```
