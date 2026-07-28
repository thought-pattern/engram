# Engram Python Code Style

Engram follows the [Google Python Style Guide](https://google.github.io/styleguide/pyguide.html) with two intentional exceptions: the import convention and the 132-character line length defined below. This document takes precedence for those exceptions; the Google guide governs all other Python style decisions.

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
pytest
```
