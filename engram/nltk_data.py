"""Centralized NLTK data management for ENGRAM.

All NLTK corpora and models used by ENGRAM are stored in a local, gitignored
``data/nltk_data`` directory at the repository root, so they can be fetched once
at setup time rather than retrieved during normal runtime. Importing this module
wires that directory onto NLTK's search path; ``ensure_nltk_data`` downloads any
missing packages into it.

Run ``python -m engram.nltk_data`` once after installation to pre-fetch
everything into the local directory.
"""

import os

import nltk

# Local data directory: <repo root>/data/nltk_data
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(_THIS_DIR)
NLTK_DATA_DIR = os.path.join(_REPO_ROOT, "data", "nltk_data")

# Required packages as (find_path, download_name) pairs. find_path is what
# nltk.data.find expects; download_name is what nltk.download expects.
REQUIRED_PACKAGES = (
    ("tokenizers/punkt", "punkt"),
    ("tokenizers/punkt_tab", "punkt_tab"),
    ("taggers/averaged_perceptron_tagger", "averaged_perceptron_tagger"),
    ("taggers/averaged_perceptron_tagger_eng", "averaged_perceptron_tagger_eng"),
    ("chunkers/maxent_ne_chunker", "maxent_ne_chunker"),
    ("chunkers/maxent_ne_chunker_tab", "maxent_ne_chunker_tab"),
    ("corpora/words", "words"),
    ("corpora/wordnet", "wordnet"),
    ("corpora/omw-1.4", "omw-1.4"),
    ("sentiment/vader_lexicon", "vader_lexicon"),
)

# Stopwords filtered out during keyword extraction. A set for O(1) membership.
DEFAULT_STOPWORDS: set[str] = {
    "a",
    "an",
    "the",
    "is",
    "are",
    "was",
    "were",
    "be",
    "been",
    "being",
    "have",
    "has",
    "had",
    "do",
    "does",
    "did",
    "will",
    "would",
    "could",
    "should",
    "may",
    "might",
    "must",
    "shall",
    "can",
    "need",
    "dare",
    "ought",
    "used",
    "to",
    "of",
    "in",
    "for",
    "on",
    "with",
    "at",
    "by",
    "from",
    "as",
    "into",
    "through",
    "during",
    "before",
    "after",
    "above",
    "below",
    "between",
    "under",
    "again",
    "further",
    "then",
    "once",
    "here",
    "there",
    "when",
    "where",
    "why",
    "how",
    "all",
    "each",
    "few",
    "more",
    "most",
    "other",
    "some",
    "such",
    "no",
    "nor",
    "not",
    "only",
    "own",
    "same",
    "so",
    "than",
    "too",
    "very",
    "just",
    "also",
}


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


def ensure_resource(find_path: str, download_name: str) -> bool:
    """Ensure a single NLTK resource is available, downloading it if missing.

    Args:
        find_path: Path passed to ``nltk.data.find`` to test availability.
        download_name: Package name passed to ``nltk.download`` when missing.

    Returns:
        True if the resource is available after the call, False otherwise.
    """
    configure_path()
    if _is_available(find_path):
        return True
    nltk.download(download_name, download_dir=NLTK_DATA_DIR, quiet=True)
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
        if download and ensure_resource(find_path, download_name):
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
