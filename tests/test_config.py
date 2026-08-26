"""Tests for configuration."""

from pytest import raises as pytest_raises

from engram.config import engram_config, graph_config
from engram.constants import DEFAULT_STOPWORDS, SessionOverflow


class TestEngramConfig:
    """Tests for configuration."""

    def test_defaults(self) -> bool:
        config = engram_config()

        assert config.get("capacity", 0) == 10000
        assert config.get("max_sessions", 0) == 10000
        assert config.get("session_ttl_seconds", 0.0) == 86400.0
        assert config.get("weight_base", 0.0) == 0.5
        assert config.get("weight_recency", 0.0) == 0.3
        assert config.get("weight_hit_rate", 0.0) == 0.2
        assert config.get("session_overflow", False) == SessionOverflow.LRU
        assert config.get("stopwords", []) == DEFAULT_STOPWORDS
        assert config.get("learn_user_facts", False) is True
        return False

    def test_custom_values(self) -> bool:
        config = engram_config(
            capacity=5000,
            max_sessions=100,
            session_ttl_seconds=3600.0,
            weight_base=0.4,
            weight_recency=0.4,
            weight_hit_rate=0.2,
            session_overflow=SessionOverflow.REJECT,
        )

        assert config.get("capacity", 0) == 5000
        assert config.get("max_sessions", 0) == 100
        assert config.get("session_overflow", False) == SessionOverflow.REJECT
        return False

    def test_invalid_capacity(self) -> bool:
        with pytest_raises(ValueError):
            engram_config(capacity=0)

        with pytest_raises(ValueError):
            engram_config(capacity=-1)
        return False

    def test_invalid_max_sessions(self) -> bool:
        with pytest_raises(ValueError):
            engram_config(max_sessions=0)
        return False

    def test_invalid_session_ttl(self) -> bool:
        with pytest_raises(ValueError):
            engram_config(session_ttl_seconds=0)

        with pytest_raises(ValueError):
            engram_config(session_ttl_seconds=-1)
        return False

    def test_invalid_weights(self) -> bool:
        with pytest_raises(ValueError):
            engram_config(weight_base=-1, weight_recency=-1, weight_hit_rate=-1)
        with pytest_raises(ValueError):
            engram_config(weight_base=-0.1)
        with pytest_raises(ValueError):
            engram_config(weight_recency=float("nan"))
        with pytest_raises(ValueError):
            engram_config(weight_hit_rate=float("inf"))
        return False

    def test_invalid_matching_and_eviction_settings(self) -> bool:
        with pytest_raises(ValueError):
            engram_config(srai_depth_limit=0)
        with pytest_raises(ValueError):
            engram_config(max_synonyms_per_word=-1)
        with pytest_raises(ValueError):
            engram_config(min_hit_rate=1.1)
        with pytest_raises(ValueError):
            engram_config(protect_static=False)
        return False

    def test_graph_config_validation(self) -> bool:
        with pytest_raises(ValueError):
            graph_config(host="")
        with pytest_raises(ValueError):
            graph_config(port=0)
        with pytest_raises(ValueError):
            graph_config(port=70000)
        with pytest_raises(ValueError):
            graph_config(vector_limit=0)
        with pytest_raises(ValueError):
            graph_config(vector_min_similarity=1.1)
        with pytest_raises(ValueError):
            graph_config(vector_weight=-0.1)
        return False

    def test_graph_vector_config(self) -> bool:
        config = graph_config(
            enabled=True,
            vector_enabled=True,
            vector_index_name="claim_premise_embeddings",
            vector_model_path="/models/minilm",
            vector_limit=75,
            vector_min_similarity=0.52,
            vector_weight=0.8,
        )

        assert config.get("vector_enabled", False) is True
        assert config.get("vector_limit", 0) == 75
        assert config.get("vector_min_similarity", 0.0) == 0.52
        assert config.get("vector_weight", 0.0) == 0.8
        return False

    def test_custom_stopwords(self) -> bool:
        custom = {"custom", "stop", "words"}
        config = engram_config(stopwords=custom)

        assert config.get("stopwords", False) == custom
        return False


class TestDefaultStopwords:
    """Tests for default stopwords."""

    def test_common_stopwords_present(self) -> bool:
        assert "the" in DEFAULT_STOPWORDS
        assert "is" in DEFAULT_STOPWORDS
        assert "are" in DEFAULT_STOPWORDS
        assert "a" in DEFAULT_STOPWORDS
        assert "an" in DEFAULT_STOPWORDS
        return False

    def test_content_words_absent(self) -> bool:
        assert "paris" not in DEFAULT_STOPWORDS
        assert "capital" not in DEFAULT_STOPWORDS
        assert "population" not in DEFAULT_STOPWORDS
        return False

    def test_is_set(self) -> bool:
        assert isinstance(DEFAULT_STOPWORDS, set)
        return False
