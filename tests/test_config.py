"""Tests for configuration."""

import pytest

from engram.config import config_from_dict, engram_config, graph_config
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
        assert config["learn_user_facts"] is True

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
        with pytest.raises(ValueError):
            engram_config(weight_base=-0.1)
        with pytest.raises(ValueError):
            engram_config(weight_recency=float("nan"))
        with pytest.raises(ValueError):
            engram_config(weight_hit_rate=float("inf"))

    def test_invalid_matching_and_eviction_settings(self) -> None:
        with pytest.raises(ValueError):
            engram_config(srai_depth_limit=0)
        with pytest.raises(ValueError):
            engram_config(max_synonyms_per_word=-1)
        with pytest.raises(ValueError):
            engram_config(min_hit_rate=1.1)
        with pytest.raises(ValueError):
            engram_config(protect_static=False)

    def test_graph_config_validation(self) -> None:
        with pytest.raises(ValueError):
            graph_config(host="")
        with pytest.raises(ValueError):
            graph_config(port=0)
        with pytest.raises(ValueError):
            graph_config(port=70000)
        with pytest.raises(ValueError):
            graph_config(vector_limit=0)
        with pytest.raises(ValueError):
            graph_config(vector_min_similarity=1.1)
        with pytest.raises(ValueError):
            graph_config(vector_weight=-0.1)
        with pytest.raises(ValueError, match="requires graph enabled"):
            graph_config(vector_enabled=True)

    @pytest.mark.parametrize("invalid", [[], (), "", 0, False])
    def test_engram_config_rejects_falsey_non_object_graph_config(self, invalid) -> None:
        with pytest.raises(ValueError, match="graph config must be an object"):
            engram_config(graph=invalid)

        with pytest.raises(ValueError, match="serialized graph config must be an object"):
            config_from_dict({"graph": invalid})

    def test_graph_vector_config(self) -> None:
        config = graph_config(
            enabled=True,
            vector_enabled=True,
            vector_index_name="claim_premise_embeddings",
            vector_model_path="/models/minilm",
            vector_limit=75,
            vector_min_similarity=0.52,
            vector_weight=0.8,
        )

        assert config["vector_enabled"] is True
        assert config["vector_limit"] == 75
        assert config["vector_min_similarity"] == 0.52
        assert config["vector_weight"] == 0.8

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
