"""Tests for the centralized NLTK data bootstrap."""

from pathlib import Path

import nltk
import pytest

from engram import nltk_data
from engram.constants import NLTK_DATA_DIR
from engram.nltk_data import configure_path


def test_data_directory_configure_path_creates_and_registers():
    """configure_path creates the dir and puts it on nltk's path."""
    result = configure_path()
    assert result == NLTK_DATA_DIR
    assert NLTK_DATA_DIR in nltk.data.path


def test_data_directory_is_independent_of_process_working_directory():
    """The provisioned data path is anchored to the Engram checkout."""
    data_directory = Path(NLTK_DATA_DIR)
    package_directory = Path(nltk_data.__file__).resolve().parent

    assert data_directory.is_absolute()
    assert data_directory == package_directory.parent / "data" / "nltk_data"


def test_data_directory_configure_path_idempotent():
    """Calling configure_path repeatedly does not duplicate the path entry."""
    configure_path()
    configure_path()
    assert nltk.data.path.count(NLTK_DATA_DIR) == 1


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
