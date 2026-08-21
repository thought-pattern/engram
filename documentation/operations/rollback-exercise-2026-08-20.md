# Persistence rollback exercise — 2026-08-20

**Result:** Passed  
**Scope:** Isolated temporary persistence files; no user or shared workspace state modified

## Scenario

The executable exercise in `tests/test_operations.py`:

1. creates a primary store and commits a baseline accepted response;
2. preserves a byte copy as the rollback candidate;
3. commits a later response to the primary store and closes it cleanly;
4. opens the preserved copy through `open_engram_core` rather than editing a live file;
5. replays the baseline mutation with its original request ID and receives the same
   idempotent receipt;
6. verifies the later response is absent; and
7. requires healthy service state plus consistent authoritative repository and
   derived indexes.

## Recorded command

```text
python -m pytest -q tests/test_operations.py
```

Observed result: `1 passed in 34.68s` on Python 3.12.2 / Windows. The test uses
pytest's isolated temporary directory and is part of the repeatable full suite.

This exercise validates the persistence-copy rollback slice of EGR-1510. It does
not exercise a production traffic drain, external Memgraph reconfiguration, a
future schema downgrade, or Section 16 release rollout.
