"""Shared source-state metadata for reproducible Engram benchmarks."""

from datetime import UTC, datetime
from hashlib import sha256 as hashlib_sha256
from pathlib import Path
from subprocess import run as subprocess_run
from tracemalloc import (
    get_traced_memory as tracemalloc_get_traced_memory,
    is_tracing as tracemalloc_is_tracing,
    reset_peak as tracemalloc_reset_peak,
    start as tracemalloc_start,
    stop as tracemalloc_stop,
)

REPOSITORY = Path(__file__).resolve().parent.parent
SOURCE_GLOBS = (
    ("engram", ("*.py", "*.json", "*.toml")),
    ("scripts", ("*.py",)),
    ("eval", ("*.py", "*.json")),
    ("tests", ("*.py", "*.json")),
    # Workflows are globbed: naming one file broke every benchmark when it was renamed.
    (".github/workflows", ("*.yml",)),
)
SOURCE_FILES = (
    "pyproject.toml",
    "requirements.txt",
    "config.example.yml",
    "engram/schema.cypher",
)
# Benchmark runners write their reports under these roots. Reports are output,
# not governed source, so a new or changed report never alters the source
# digest of another run; corpora and fixtures elsewhere under eval stay governed.
GENERATED_OUTPUT_ROOTS = ("eval/results",)


def git_output(*arguments: str) -> str:
    process = subprocess_run(
        ["git", *arguments],
        cwd=REPOSITORY,
        check=True,
        capture_output=True,
        text=True,
    )
    # Only trim record terminators.  Leading whitespace is meaningful in
    # porcelain status output (for example, `` M .github/workflows/python_analysis.yml``).
    result = process.stdout.rstrip("\r\n")
    return result


def governed_source_sha256(repository: Path = REPOSITORY) -> str:
    """Digest governed code and configuration while excluding output artifacts."""
    digest = hashlib_sha256()
    paths = []
    for root_name, patterns in SOURCE_GLOBS:
        for pattern in patterns:
            for path in (repository / root_name).rglob(pattern):
                relative = path.relative_to(repository)
                if "__pycache__" in relative.parts or any(relative.is_relative_to(root) for root in GENERATED_OUTPUT_ROOTS):
                    continue
                paths.append(path)
    paths.extend(repository / name for name in SOURCE_FILES)
    for path in sorted(paths):
        relative = path.relative_to(repository).as_posix()
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    result = digest.hexdigest()
    return result


def benchmark_source_state() -> dict[str, object]:
    """Capture base revision, dirty paths, and a content digest for one run."""
    status = git_output("status", "--short")
    changed_paths = tuple(sorted(line[3:].strip().replace("\\", "/") for line in status.splitlines() if len(line) > 3))
    result = {
        "repository": git_output("remote", "get-url", "origin"),
        "branch": git_output("branch", "--show-current"),
        "base_commit": git_output("rev-parse", "HEAD"),
        "working_tree_dirty": bool(changed_paths),
        "changed_paths": changed_paths,
        "governed_source_sha256": governed_source_sha256(),
    }
    return result


def recorded_at() -> str:
    """Return one current UTC artifact timestamp."""
    result = datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")
    return result


class TracedMemoryScope:
    """Measure Python traced-memory delta and peak without polluting or stopping a caller's tracer."""

    def __init__(self) -> None:
        self.was_tracing = False
        self.baseline_current = 0

    def __enter__(self) -> "TracedMemoryScope":
        self.was_tracing = tracemalloc_is_tracing()
        if self.was_tracing:
            current_before, _ = tracemalloc_get_traced_memory()
            self.baseline_current = current_before
            tracemalloc_reset_peak()
        else:
            self.baseline_current = 0
            tracemalloc_start()
        return self

    def read(self) -> tuple[int, int]:
        """Return (current_bytes, peak_bytes) attributable to this scope."""
        raw_current, raw_peak = tracemalloc_get_traced_memory()
        measured_current = max(0, raw_current - self.baseline_current)
        measured_peak = max(0, raw_peak - self.baseline_current)
        result = (measured_current, measured_peak)
        return result

    def __exit__(self, exc_type: object, exc_val: object, exc_tb: object) -> bool:
        del exc_type, exc_val, exc_tb
        if not self.was_tracing:
            tracemalloc_stop()
        return False
