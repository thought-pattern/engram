"""In-process tests for the CLI (scripts/cli.py).

The CLI is driven through main(argv) rather than subprocesses, so each test
costs a function call instead of an interpreter start. Every test uses its own
store and config paths under tmp_path -- the repo's engram.json and config.yml
are never touched.
"""

import json
import os
import sys
from importlib import util

import pytest

from engram.errors import PersistenceError

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CLI_PATH = os.path.join(REPO_ROOT, "scripts", "cli.py")

_spec = util.spec_from_file_location("engram_cli", CLI_PATH)
if not _spec or not _spec.loader:
    raise RuntimeError(f"could not load CLI module from {CLI_PATH}")
cli = util.module_from_spec(_spec)
sys.modules.setdefault("engram_cli", cli)
_spec.loader.exec_module(cli)


@pytest.fixture
def store(tmp_path):
    """Path to a per-test store file (missing config falls back to defaults)."""
    return str(tmp_path / "store.json")


def run_cli(store: str, *args: str) -> int:
    """Run the CLI against an isolated store and a nonexistent config file."""
    missing_config = f"{store}.no-config.yml"
    argv = ["--store", store, "--config", missing_config, *args]
    return cli.main(argv)


class TestInitAndStore:
    def test_init_creates_store(self, store) -> None:
        assert run_cli(store, "init") == 0
        assert os.path.exists(store)

    def test_init_refuses_overwrite(self, store, capsys) -> None:
        run_cli(store, "init")
        assert run_cli(store, "init") == 1
        assert "already exists" in capsys.readouterr().err

    def test_store_and_query_round_trip(self, store, capsys) -> None:
        run_cli(store, "init")
        assert run_cli(store, "store", "Paris is the capital of France", "--static") == 0

        assert run_cli(store, "query", "capital of France") == 0
        out = capsys.readouterr().out
        assert "Paris is the capital of France" in out

    def test_query_hit_persists_statistics(self, store) -> None:
        run_cli(store, "init")
        run_cli(store, "store", "Paris is the capital of France")
        assert run_cli(store, "query", "capital of France", "--hit") == 0

        with open(store, encoding="utf-8") as f:
            state = json.load(f)
        assert state["hit_count"] == 1
        assert state["query_count"] >= 1
        statement = next(s for s in state["statements"] if s["text"] == "Paris is the capital of France")
        assert statement["hit_count"] == 1


class TestSessions:
    def test_session_lifecycle(self, store, capsys) -> None:
        run_cli(store, "init")
        assert run_cli(store, "session", "create", "--id", "user1") == 0
        assert run_cli(store, "session", "update", "user1", "Previous response") == 0
        assert run_cli(store, "session", "get", "user1") == 0
        assert "user1" in capsys.readouterr().out
        assert run_cli(store, "session", "delete", "user1") == 0
        assert run_cli(store, "session", "get", "user1") == 1


class TestMetricsAndDecay:
    def test_metrics_reports(self, store, capsys) -> None:
        run_cli(store, "init")
        run_cli(store, "store", "Paris is the capital of France")
        assert run_cli(store, "metrics") == 0
        out = capsys.readouterr().out
        assert "Statements" in out

    def test_decay_ages_statistics(self, store) -> None:
        run_cli(store, "init")
        run_cli(store, "store", "Paris is the capital of France")
        for _ in range(2):
            run_cli(store, "query", "capital of France", "--hit")

        assert run_cli(store, "decay", "--factor", "0.5") == 0

        with open(store, encoding="utf-8") as f:
            state = json.load(f)
        capital = state["keywords"]["capital"]
        assert capital["query_count"] == 1
        assert capital["hit_count"] == 1

    def test_decay_rejects_bad_factor(self, store) -> None:
        run_cli(store, "init")
        assert run_cli(store, "decay", "--factor", "1.5") == 1


class TestExportAndLoad:
    def test_export_json(self, store, tmp_path, capsys) -> None:
        run_cli(store, "init")
        run_cli(store, "store", "Exported statement", "--static")
        out_path = str(tmp_path / "export.json")

        assert run_cli(store, "export", "--json", "-o", out_path) == 0

        with open(out_path, encoding="utf-8") as f:
            data = json.load(f)
        assert any(p["response"] == "Exported statement" for p in data["pairs"])

    def test_load_pairs_file(self, store, tmp_path, capsys) -> None:
        run_cli(store, "init")
        corpus = tmp_path / "corpus.json"
        corpus.write_text(
            json.dumps({"pairs": [{"pattern": "HELLO", "response": "Hi there!"}]}),
            encoding="utf-8",
        )

        assert run_cli(store, "load", str(corpus), "--static") == 0
        out = capsys.readouterr().out
        assert "Loaded 1 statements" in out


class TestConfigErrors:
    def test_stale_graph_config_raises_named_error(self, store, tmp_path) -> None:
        config = tmp_path / "config.yml"
        config.write_text("graph:\n  uri: bolt://localhost:7687\n  enabled: false\n", encoding="utf-8")
        with pytest.raises(ValueError, match="uri"):
            cli.main(["--store", store, "--config", str(config), "metrics"])

    def test_core_error_is_reported_without_a_traceback(self, store, capsys, monkeypatch) -> None:
        run_cli(store, "init")

        def fail_open(cls, **kwargs):
            raise PersistenceError("store load", OSError("unavailable"), state_changed=False)

        monkeypatch.setattr(cli.EngramCore, "open", classmethod(fail_open))

        assert run_cli(store, "metrics") == 1
        assert "store load failed: unavailable" in capsys.readouterr().err


class TestSyncSeed:
    def test_sync_seed_refreshes_stale_template(self, store, tmp_path, capsys) -> None:
        # Sync a v1 seed into the store, then a v2 seed over it. Patterns are
        # chosen not to collide with the repo seed that init loads.
        seed_v1 = tmp_path / "seed_v1.json"
        seed_v1.write_text(
            json.dumps({"pairs": [{"pattern": "ZETA PROTOCOL *", "response": "Old zeta answer {star1}!"}]}),
            encoding="utf-8",
        )
        seed_v2 = tmp_path / "seed_v2.json"
        seed_v2.write_text(
            json.dumps(
                {
                    "pairs": [
                        {"pattern": "ZETA PROTOCOL *", "response": "", "template": {"text": "Updated zeta: {star1}."}},
                        {"pattern": "OMEGA HANDSHAKE", "response": "Omega acknowledged."},
                    ]
                }
            ),
            encoding="utf-8",
        )

        run_cli(store, "init")
        assert run_cli(store, "sync-seed", "--file", str(seed_v1)) == 0
        assert run_cli(store, "sync-seed", "--file", str(seed_v2)) == 0
        out = capsys.readouterr().out
        assert "1 added, 1 updated" in out

        with open(store, encoding="utf-8") as f:
            state = json.load(f)
        synced = [s for s in state["statements"] if s.get("pattern") == "ZETA PROTOCOL *"]
        assert any(s.get("template") == {"text": "Updated zeta: {star1}."} for s in synced)

    def test_sync_seed_missing_file_errors(self, store, tmp_path) -> None:
        run_cli(store, "init")
        assert run_cli(store, "sync-seed", "--file", str(tmp_path / "absent.json")) == 1

    def test_init_seeds_store(self, store, capsys) -> None:
        assert run_cli(store, "init") == 0
        out = capsys.readouterr().out
        assert "seed statements" in out


class TestInteractiveChat:
    def test_process_input_routes_through_pipeline(self) -> None:
        from engram.constants import Tier
        from engram.core import Engram

        engram = Engram()
        engram.store("Hi there!", pattern="HELLO", tier=Tier.STATIC)
        chat = cli.InteractiveChat(engram)

        assert chat.process_input("hello") == "Hi there!"
        # Nothing matches and there is no catch-all: the chat default answers.
        assert chat.process_input("zzz qqq xxx") == "Tell me more about that."

    def test_persistent_runtime_supports_inspection_and_reports(self, tmp_path, capsys) -> None:
        from engram.constants import Tier
        from engram.core import Engram

        engram = Engram()
        engram.store("Hi there!", pattern="HELLO", tier=Tier.STATIC)
        transcript = tmp_path / "recovery.json"
        chat = cli.InteractiveChat(
            engram,
            session_id="Human label",
            initial_bot_text=".",
            transcript_path=str(transcript),
        )

        assert chat.process_input("hello") == "Hi there!"
        assert chat.runtime.inspect()["user_id"] == "Human label"
        assert chat.runtime.inspect()["turn_count"] == 1
        assert transcript.exists()

        assert chat._handle_command("/inspect") is False
        assert '"turn_count": 1' in capsys.readouterr().out

        prefix = tmp_path / "human-chat"
        assert chat._handle_command(f"/finish {prefix}") is False
        assert prefix.with_suffix(".json").exists()
        assert prefix.with_suffix(".md").exists()
