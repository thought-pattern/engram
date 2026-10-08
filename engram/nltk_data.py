"""Centralized NLTK data management for ENGRAM.

In a source checkout, all NLTK corpora and models used by ENGRAM are stored in
a local, gitignored ``data/nltk_data`` directory at the repository root, so they
can be fetched once at setup time rather than retrieved during normal runtime.
An installed package has no checkout, so NLTK's standard locations apply instead
(the ``NLTK_DATA`` environment variable, ``~/nltk_data``, and so on).
Downloading is available only through an explicit bootstrap request; normal
callers perform an offline check.

``NLTK_RESOURCES`` owns NLTK readiness for the process. Every Engram function
that reads NLTK data calls ``NLTK_RESOURCES.require()`` first, which wires the
checkout directory onto NLTK's search path, requires every package in
``REQUIRED_PACKAGES`` and loads the shared readers once. Importing this module
changes nothing.

Run ``python -m engram.nltk_data`` once after installation to pre-fetch
everything.
"""

from os import makedirs as os_makedirs
from pathlib import Path
from threading import Lock

from nltk import data as nltk_data, download as nltk_download
from nltk.corpus import wordnet, words
from nltk.sentiment import SentimentIntensityAnalyzer

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


def is_available(find_path: str) -> bool:
    """Return True if a resource resolves on NLTK's path now.

    Checks both the bare path (extracted installs, the default location) and the
    ``.zip`` form (packages downloaded into a custom directory stay zipped). The
    answer is not cached, so a change to the data or the path is seen at once.
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
    data_directory = local_data_dir()
    if data_directory:
        nltk_download(download_name, download_dir=data_directory, quiet=True)
    else:
        # An installed Engram has no checkout data directory, so NLTK chooses its standard download location.
        nltk_download(download_name, quiet=True)
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


class NltkResources:
    """Own NLTK readiness and the shared NLTK resources for the process.

    ``require`` loads once, under ``lock``: it requires every package (through
    ``verify``) and then loads the WordNet and word-list readers, the English
    word set and the VADER analyzer, so concurrent requests never race a lazy
    first load. ``verify`` re-checks availability on every call without
    reloading; Engram construction runs it before ``require``.

    ``reader_lock`` serializes WordNet reads: NLTK's shared reader opens and
    closes its zipped corpus around each read, and concurrent reads can trip its
    internal file-handle assertion. Loading never takes ``reader_lock``, so a
    caller may load before taking it but must not load while holding it.
    """

    def __init__(self) -> None:
        self.lock = Lock()
        self.reader_lock = Lock()
        self.loaded = False
        self.english_words: set[str] = set()
        # Holds the one analyzer once ``require`` has loaded.
        self.sentiment_analyzers: list[SentimentIntensityAnalyzer] = []

    def verify(self) -> None:
        """Put a checkout's data directory on NLTK's path and require every package.

        Raises:
            RuntimeError: a package in ``REQUIRED_PACKAGES`` is not available.
        """
        missing = ensure_nltk_data(download=False)
        if missing:
            missing_names = ", ".join(download_name for _, download_name in missing)
            raise RuntimeError(f"required NLTK resources are unavailable: {missing_names}")

    def require(self) -> None:
        """Make NLTK data usable, verifying and loading the shared resources on the first call.

        Raises:
            RuntimeError: a package in ``REQUIRED_PACKAGES`` is not available.
        """
        with self.lock:
            if not self.loaded:
                self.verify()
                words.ensure_loaded()
                wordnet.ensure_loaded()
                self.english_words = {entry.lower() for entry in words.words()}
                self.sentiment_analyzers = [SentimentIntensityAnalyzer()]
                self.loaded = True

    def is_english_word(self, word: str) -> bool:
        """Return whether the already lowercased ``word`` is in NLTK's English word list."""
        self.require()
        known = word in self.english_words
        return known

    def sentiment_analyzer(self) -> SentimentIntensityAnalyzer:
        """Return the process's VADER analyzer."""
        self.require()
        analyzer = self.sentiment_analyzers[0]
        return analyzer


NLTK_RESOURCES = NltkResources()


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
