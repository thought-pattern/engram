"""Centralized NLTK data management for ENGRAM.

In a source checkout, all NLTK corpora and models used by ENGRAM are stored in
a local, gitignored ``data/nltk_data`` directory at the repository root, so they
can be fetched once at setup time rather than retrieved during normal runtime.
Importing this module wires that directory onto NLTK's search path. An installed
package has no checkout, so NLTK's standard locations apply instead (the
``NLTK_DATA`` environment variable, ``~/nltk_data``, and so on). Downloading is
available only through an explicit bootstrap request; normal callers perform an
offline check.

Run ``python -m engram.nltk_data`` once after installation to pre-fetch
everything.
"""

from functools import lru_cache
from os import makedirs as os_makedirs
from pathlib import Path

from nltk import data as nltk_data, download as nltk_download

from engram.constants import REQUIRED_PACKAGES

SOURCE_ROOT = Path(__file__).resolve().parent.parent


def local_data_dir(root: Path = SOURCE_ROOT) -> str:
    """Return the checkout's NLTK data directory, or "" when Engram is installed.

    A checkout is recognized by its ``pyproject.toml``. Without one, nothing is
    created next to the installed package and NLTK's standard locations apply.
    """
    if (root / "pyproject.toml").is_file():
        result = str(root / "data" / "nltk_data")
        return result
    result = ""
    return result


def configure_path() -> str:
    """Ensure a checkout's data directory exists and is first on NLTK's path.

    Inserting the local directory at the front of ``nltk.data.path`` means
    locally bootstrapped data is preferred, while any pre-existing data in the
    default user location still resolves as a fallback. An installed package
    leaves NLTK's path unchanged.

    Returns:
        The local data directory path, or "" when NLTK's own locations apply.
    """
    directory = local_data_dir()
    if directory:
        os_makedirs(directory, exist_ok=True)
        if directory not in nltk_data.path:
            nltk_data.path.insert(0, directory)
    return directory


@lru_cache(maxsize=64)
def is_available(find_path: str) -> bool:
    """Return True if a resource resolves on NLTK's path.

    Checks both the bare path (extracted installs, the default location) and the
    ``.zip`` form (packages downloaded into a custom directory stay zipped).
    """
    for candidate in (find_path, find_path + ".zip"):
        try:
            nltk_data.find(candidate)
            result = True
            return result
        except LookupError:
            continue
    result = False
    return result


def ensure_resource(find_path: str, download_name: str, *, download: bool = False) -> bool:
    """Return whether one NLTK resource is available after an optional bootstrap.

    Args:
        find_path: Path passed to ``nltk.data.find`` to test availability.
        download_name: Package name passed to ``nltk.download`` when missing.
        download: Explicit setup-time authorization to acquire the resource.

    Returns:
        True if the resource is available after the call, False otherwise.
    """
    configure_path()
    if is_available(find_path):
        result = True
        return result
    if not download:
        result = False
        return result
    nltk_download(download_name, download_dir=local_data_dir() or None, quiet=True)
    cache_clear = getattr(is_available, "cache_clear", ())
    if callable(cache_clear):
        cache_clear()
    available = is_available(find_path)
    return available


def ensure_nltk_data(download: bool = True) -> list:
    """Ensure all required NLTK data is available in the local directory.

    Args:
        download: If True, download missing packages into the local directory.
            If False, only report what is missing without downloading.

    Returns:
        List of (find_path, download_name) pairs that are still missing.
    """
    configure_path()
    missing = []
    for find_path, download_name in REQUIRED_PACKAGES:
        if is_available(find_path):
            continue
        if download and ensure_resource(find_path, download_name, download=True):
            continue
        missing.append((find_path, download_name))
    return missing


# Wire the local path on import so any nltk.data.find sees local data first.
configure_path()


def main() -> int:
    """Bootstrap entry point: download all required NLTK data locally."""
    print(f"NLTK data directory: {local_data_dir() or 'NLTK default locations'}")
    missing = ensure_nltk_data(download=True)
    if missing:
        print("[WARNING] Could not obtain the following packages:")
        for find_path, download_name in missing:
            print(f"  - {download_name} ({find_path})")
        result = 1
        return result
    print("[DONE] All required NLTK data is available.")
    result = 0
    return result


if __name__ == "__main__":
    raise SystemExit(main())
