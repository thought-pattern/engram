"""Centralized NLTK data management for ENGRAM.

All NLTK corpora and models used by ENGRAM are stored in a local, gitignored
``data/nltk_data`` directory at the repository root, so they can be fetched once
at setup time rather than retrieved during normal runtime. Importing this module
wires that directory onto NLTK's search path. Downloading is available only
through an explicit bootstrap request; normal callers perform an offline check.

Run ``python -m engram.nltk_data`` once after installation to pre-fetch
everything into the local directory.
"""

import os
from functools import lru_cache

import nltk

from engram.constants import NLTK_DATA_DIR, REQUIRED_PACKAGES


def configure_path() -> str:
    """Ensure the local data directory exists and is first on NLTK's path.

    Inserting the local directory at the front of ``nltk.data.path`` means
    locally bootstrapped data is preferred, while any pre-existing data in the
    default user location still resolves as a fallback.

    Returns:
        The local data directory path.
    """
    os.makedirs(NLTK_DATA_DIR, exist_ok=True)
    if NLTK_DATA_DIR not in nltk.data.path:
        nltk.data.path.insert(0, NLTK_DATA_DIR)
    return NLTK_DATA_DIR


@lru_cache(maxsize=64)
def _is_available(find_path: str) -> bool:
    """Return True if a resource resolves on NLTK's path.

    Checks both the bare path (extracted installs, the default location) and the
    ``.zip`` form (packages downloaded into a custom directory stay zipped).
    """
    for candidate in (find_path, find_path + ".zip"):
        try:
            nltk.data.find(candidate)
            return True
        except LookupError:
            continue
    return False


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
    if _is_available(find_path):
        return True
    if not download:
        return False
    nltk.download(download_name, download_dir=NLTK_DATA_DIR, quiet=True)
    cache_clear = getattr(_is_available, "cache_clear", ())
    if callable(cache_clear):
        cache_clear()
    available = _is_available(find_path)
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
        if _is_available(find_path):
            continue
        if download and ensure_resource(find_path, download_name, download=True):
            continue
        missing.append((find_path, download_name))
    return missing


# Wire the local path on import so any nltk.data.find sees local data first.
configure_path()


def main() -> int:
    """Bootstrap entry point: download all required NLTK data locally."""
    print(f"NLTK data directory: {NLTK_DATA_DIR}")
    missing = ensure_nltk_data(download=True)
    if missing:
        print("[WARNING] Could not obtain the following packages:")
        for find_path, download_name in missing:
            print(f"  - {download_name} ({find_path})")
        return 1
    print("[DONE] All required NLTK data is available.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
