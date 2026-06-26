"""Tests for the centralized NLTK data bootstrap."""

import os

import nltk

from engram.nltk_data import NLTK_DATA_DIR, REQUIRED_PACKAGES, configure_path, ensure_nltk_data


class TestDataDirectory:
    """Tests for the local data directory configuration."""

    def test_data_dir_is_local(self):
        """The data dir lives under the repo's data/nltk_data path."""
        assert NLTK_DATA_DIR.replace("\\", "/").endswith("data/nltk_data")

    def test_configure_path_creates_and_registers(self):
        """configure_path creates the dir and puts it on nltk's path."""
        result = configure_path()
        assert result == NLTK_DATA_DIR
        assert os.path.isdir(NLTK_DATA_DIR)
        assert NLTK_DATA_DIR in nltk.data.path

    def test_configure_path_idempotent(self):
        """Calling configure_path repeatedly does not duplicate the path entry."""
        configure_path()
        configure_path()
        assert nltk.data.path.count(NLTK_DATA_DIR) == 1


class TestRequiredPackages:
    """Tests for the declared package set."""

    def test_includes_vader(self):
        """The VADER lexicon is among the required packages."""
        names = [name for _, name in REQUIRED_PACKAGES]
        assert "vader_lexicon" in names

    def test_includes_wordnet_and_punkt(self):
        """Core corpora used across the package are declared."""
        names = [name for _, name in REQUIRED_PACKAGES]
        assert "wordnet" in names
        assert "punkt" in names

    def test_pairs_are_well_formed(self):
        """Each entry is a (find_path, download_name) pair of strings."""
        for entry in REQUIRED_PACKAGES:
            assert len(entry) == 2
            find_path, download_name = entry
            assert isinstance(find_path, str) and find_path
            assert isinstance(download_name, str) and download_name


class TestEnsure:
    """Tests for the ensure routine (report-only mode is network-free)."""

    def test_report_only_returns_list(self):
        """ensure_nltk_data(download=False) reports missing packages as a list."""
        missing = ensure_nltk_data(download=False)
        assert isinstance(missing, list)
