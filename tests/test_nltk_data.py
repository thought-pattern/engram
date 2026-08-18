"""Tests for the centralized NLTK data bootstrap."""

import os

import nltk
import pytest

from engram import nltk_data
from engram.constants import NLTK_DATA_DIR, REQUIRED_PACKAGES
from engram.nltk_data import configure_path, ensure_nltk_data

"""Tests for the local data directory configuration."""


def test_data_directory_data_dir_is_local():
    """The data dir lives under the repo's data/nltk_data path."""
    assert NLTK_DATA_DIR.replace("\\", "/").endswith("data/nltk_data")


def test_data_directory_configure_path_creates_and_registers():
    """configure_path creates the dir and puts it on nltk's path."""
    result = configure_path()
    assert result == NLTK_DATA_DIR
    assert os.path.isdir(NLTK_DATA_DIR)
    assert NLTK_DATA_DIR in nltk.data.path


def test_data_directory_configure_path_idempotent():
    """Calling configure_path repeatedly does not duplicate the path entry."""
    configure_path()
    configure_path()
    assert nltk.data.path.count(NLTK_DATA_DIR) == 1


"""Tests for the declared package set."""


def test_required_packages_includes_vader():
    """The VADER lexicon is among the required packages."""
    names = [name for _, name in REQUIRED_PACKAGES]
    assert "vader_lexicon" in names


def test_required_packages_includes_wordnet_and_punkt():
    """Core corpora used across the package are declared."""
    names = [name for _, name in REQUIRED_PACKAGES]
    assert "wordnet" in names
    assert "punkt" in names


def test_required_packages_pairs_are_well_formed():
    """Each entry is a (find_path, download_name) pair of strings."""
    for entry in REQUIRED_PACKAGES:
        assert len(entry) == 2
        find_path, download_name = entry
        assert isinstance(find_path, str) and find_path
        assert isinstance(download_name, str) and download_name


"""Tests for the ensure routine (report-only mode is network-free)."""


def test_ensure_report_only_returns_list():
    """ensure_nltk_data(download=False) reports missing packages as a list."""
    missing = ensure_nltk_data(download=False)
    assert isinstance(missing, list)


def test_ensure_runtime_resource_check_never_downloads(monkeypatch: pytest.MonkeyPatch) -> None:
    """The default resource path is offline and reports absence."""
    calls = []
    monkeypatch.setattr(nltk_data, "_is_available", lambda _path: False)
    monkeypatch.setattr(nltk_data.nltk, "download", lambda *args, **kwargs: calls.append((args, kwargs)))

    assert nltk_data.ensure_resource("corpora/missing", "missing") is False
    assert calls == []


def test_ensure_explicit_bootstrap_can_download(monkeypatch: pytest.MonkeyPatch) -> None:
    """Only an explicit setup-time flag authorizes acquisition."""
    availability = iter((False, True))
    calls = []
    monkeypatch.setattr(nltk_data, "_is_available", lambda _path: next(availability))
    monkeypatch.setattr(nltk_data.nltk, "download", lambda *args, **kwargs: calls.append((args, kwargs)))

    assert nltk_data.ensure_resource("corpora/missing", "missing", download=True) is True
    assert len(calls) == 1
