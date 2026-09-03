"""Concurrency smoke tests: the documented flows are safe under threads.

These hammer store / query / pattern_query / record_hit / retire from several
threads at once and then assert the core invariants: no exceptions escaped,
the statement index is consistent, and the metrics counters saw every event.
"""

from threading import Thread as threading_Thread

from engram import pipeline, sessions
from engram.constants import Tier
from engram.core import Engram

THREADS = 4
ITERATIONS = 25


def test_concurrent_flows_hold_invariants() -> None:
    engram = Engram()
    engram.store("Tell me more.", pattern="*", tier=Tier.STATIC)
    errors: list[Exception] = []

    def worker(n: int) -> None:
        try:
            for i in range(ITERATIONS):
                stmt_id = engram.store(f"worker {n} statement {i} about subject {i % 5}")
                result = engram.query(f"subject {i % 5} statement")
                if result["matches"]:
                    top = result["matches"][0][0]
                    engram.record_hit(result["keywords"], statement_id=top["id"])
                engram.store(f"Patterned answer {n} {i}", pattern=f"WORKER {n} ITEM {i}")
                engram.pattern_query(f"worker {n} item {i}")
                engram.retire_statement(stmt_id)
        except Exception as err:
            errors.append(err)

    threads = [threading_Thread(target=worker, args=(n,)) for n in range(THREADS)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []

    # Statement index invariant: every id maps to the statement at its index.
    assert len(engram.statement_index) == len(engram.statements)
    for stmt_id, idx in engram.statement_index.items():
        assert engram.statements[idx]["id"] == stmt_id

    # The counters saw every query event exactly once (query + pattern_query
    # per iteration per worker).
    assert engram.query_count == THREADS * ITERATIONS * 2


def test_concurrent_session_updates() -> None:

    engram = Engram()
    errors: list[Exception] = []

    def worker(n: int) -> None:
        try:
            for i in range(ITERATIONS):
                session_id = sessions.start_session(engram, session_id=f"sess_{n}_{i}")
                sessions.update_session_context(engram, session_id, f"response {n} {i}")
                sessions.get_session(engram, session_id, create_if_missing=False)
                sessions.delete_session(engram, session_id)
        except Exception as err:
            errors.append(err)

    threads = [threading_Thread(target=worker, args=(n,)) for n in range(THREADS)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    assert len(engram.sessions) == 0


def test_concurrent_user_chat_contexts_are_isolated() -> None:
    engram = Engram()
    engram.store("Hello.", pattern="HELLO *", tier=Tier.STATIC)
    errors: list[Exception] = []

    def worker(n: int) -> None:
        try:
            user_id = f"user-{n}"
            for i in range(ITERATIONS):
                result = pipeline.chat(
                    engram,
                    f"hello {user_id} item {i}",
                    user_id=user_id,
                )
                assert result["user_id"] == user_id
        except Exception as err:
            errors.append(err)

    threads = [threading_Thread(target=worker, args=(n,)) for n in range(THREADS)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    assert set(engram.sessions) == {f"user-{n}" for n in range(THREADS)}
    for n in range(THREADS):
        history = engram.sessions[f"user-{n}"]["input_history"]
        assert history
        assert all(entry.startswith(f"hello user-{n} item ") for entry in history)


def test_concurrent_speakers_store_one_copy_of_the_same_fact() -> None:
    engram = Engram()
    engram.store("Go on.", pattern="*", tier=Tier.STATIC)
    errors: list[Exception] = []

    def worker(n: int) -> None:
        try:
            pipeline.chat(engram, "Sushi is good.", user_id=f"user-{n}")
        except Exception as err:
            errors.append(err)

    threads = [threading_Thread(target=worker, args=(n,)) for n in range(THREADS)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    learned = [statement for statement in engram.statements if statement["tier"] == Tier.DYNAMIC]
    assert len(learned) == 1
    assert engram.pattern_query("What's good?")[2] == "Sushi is good."
