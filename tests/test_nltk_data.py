"""Tests for the centralized NLTK data bootstrap."""

import nltk
import pytest

from engram import nltk_data
from engram.constants import NLTK_DATA_DIR
from engram.nltk_data import configure_path

"""Tests for the local data directory configuration."""


def test_data_directory_configure_path_creates_and_registers():
    """configure_path creates the dir and puts it on nltk's path."""
    result = configure_path()
    assert result == NLTK_DATA_DIR
    assert NLTK_DATA_DIR in nltk.data.path


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
