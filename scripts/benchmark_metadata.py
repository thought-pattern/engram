"""Shared source-state metadata for reproducible Engram benchmarks."""

from datetime import UTC, datetime
from hashlib import sha256 as hashlib_sha256
from pathlib import Path
from subprocess import run as subprocess_run

REPOSITORY = Path(__file__).resolve().parent.parent
SOURCE_GLOBS = (
    ("engram", ("*.py", "*.json")),
    ("scripts", ("*.py",)),
    ("eval", ("*.py", "*.json")),
    ("tests", ("*.py",)),
)
SOURCE_FILES = (
    "pyproject.toml",
    "requirements.txt",
    "config.example.yml",
    "schema.cypher",
    ".github/workflows/ci.yml",
)


def git_output(*arguments: str) -> str:
    process = subprocess_run(
        ["git", *arguments],
        cwd=REPOSITORY,
        check=True,
        capture_output=True,
        text=True,
    )
    # Only trim record terminators.  Leading whitespace is meaningful in
    # porcelain status output (for example, `` M .github/workflows/ci.yml``).
    result = process.stdout.rstrip("\r\n")
    return result


def governed_source_sha256(repository: Path = REPOSITORY) -> str:
    """Digest governed code and configuration while excluding output artifacts."""
    digest = hashlib_sha256()
    paths = []
    for root_name, patterns in SOURCE_GLOBS:
        for pattern in patterns:
            paths.extend(path for path in (repository / root_name).rglob(pattern) if "__pycache__" not in path.parts)
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
