"""Tests for the centralized NLTK data bootstrap."""

from nltk import data as nltk_runtime_data
from pytest import MonkeyPatch as pytest_MonkeyPatch

from engram import nltk_data
from engram.nltk_data import configure_path


def test_data_directory_configure_path_creates_and_registers():
    """configure_path creates the dir and puts it on nltk's path."""
    result = configure_path()
    assert result in nltk_runtime_data.path


def test_data_directory_configure_path_idempotent():
    """Calling configure_path repeatedly does not duplicate the path entry."""
    result = configure_path()
    assert configure_path() == result
    assert nltk_runtime_data.path.count(result) == 1


def test_ensure_runtime_resource_check_never_downloads(monkeypatch: pytest_MonkeyPatch) -> None:
    """The default resource path is offline and reports absence."""
    calls = []

    def unavailable(internal_path):
        assert internal_path == "corpora/missing"
        return False

    monkeypatch.setattr(nltk_data, "is_available", unavailable)
    monkeypatch.setattr(nltk_data, "nltk_download", lambda *args, **kwargs: calls.append((args, kwargs)))

    assert nltk_data.ensure_resource("corpora/missing", "missing") is False
    assert calls == []


def test_ensure_explicit_bootstrap_can_download(monkeypatch: pytest_MonkeyPatch) -> None:
    """Only an explicit setup-time flag authorizes acquisition."""
    availability = iter((False, True))
    calls = []

    def availability_probe(internal_path):
        assert internal_path == "corpora/missing"
        result = next(availability)
        return result

    monkeypatch.setattr(nltk_data, "is_available", availability_probe)
    monkeypatch.setattr(nltk_data, "nltk_download", lambda *args, **kwargs: calls.append((args, kwargs)))

    assert nltk_data.ensure_resource("corpora/missing", "missing", download=True) is True
    assert len(calls) == 1
