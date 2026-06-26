"""Tests for configuration."""

import pytest

from engram.config import engram_config
from engram.constants import DEFAULT_STOPWORDS, SessionOverflow


class TestEngramConfig:
    """Tests for configuration."""

    def test_defaults(self) -> None:
        config = engram_config()

        assert config["capacity"] == 10000
        assert config["max_sessions"] == 10000
        assert config["session_ttl_seconds"] == 86400.0
        assert config["weight_base"] == 0.5
        assert config["weight_recency"] == 0.3
        assert config["weight_hit_rate"] == 0.2
        assert config["session_overflow"] == SessionOverflow.LRU
        assert config["stopwords"] == DEFAULT_STOPWORDS

    def test_custom_values(self) -> None:
        config = engram_config(
            capacity=5000,
            max_sessions=100,
            session_ttl_seconds=3600.0,
            weight_base=0.4,
            weight_recency=0.4,
            weight_hit_rate=0.2,
            session_overflow=SessionOverflow.REJECT,
        )

        assert config["capacity"] == 5000
        assert config["max_sessions"] == 100
        assert config["session_overflow"] == SessionOverflow.REJECT

    def test_invalid_capacity(self) -> None:
        with pytest.raises(ValueError):
            engram_config(capacity=0)

        with pytest.raises(ValueError):
            engram_config(capacity=-1)

    def test_invalid_max_sessions(self) -> None:
        with pytest.raises(ValueError):
            engram_config(max_sessions=0)

    def test_invalid_session_ttl(self) -> None:
        with pytest.raises(ValueError):
            engram_config(session_ttl_seconds=0)

        with pytest.raises(ValueError):
            engram_config(session_ttl_seconds=-1)

    def test_invalid_weights(self) -> None:
        with pytest.raises(ValueError):
            engram_config(weight_base=-1, weight_recency=-1, weight_hit_rate=-1)

    def test_custom_stopwords(self) -> None:
        custom = {"custom", "stop", "words"}
        config = engram_config(stopwords=custom)

        assert config["stopwords"] == custom


class TestDefaultStopwords:
    """Tests for default stopwords."""

    def test_common_stopwords_present(self) -> None:
        assert "the" in DEFAULT_STOPWORDS
        assert "is" in DEFAULT_STOPWORDS
        assert "are" in DEFAULT_STOPWORDS
        assert "a" in DEFAULT_STOPWORDS
        assert "an" in DEFAULT_STOPWORDS

    def test_content_words_absent(self) -> None:
        assert "paris" not in DEFAULT_STOPWORDS
        assert "capital" not in DEFAULT_STOPWORDS
        assert "population" not in DEFAULT_STOPWORDS

    def test_is_set(self) -> None:
        assert isinstance(DEFAULT_STOPWORDS, set)
