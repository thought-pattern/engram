"""Learning-loop evaluation: does usage actually shape retention and ranking?

Simulates the documented cache-of-LLM-answers cycle on a fresh in-memory
instance (it never touches engram.json): responses are learned, a subset is
repeatedly confirmed by users, unconfirmed one-offs churn through a small
DYNAMIC capacity, and statistics are aged. The report checks the loop's
invariants end to end:

  - confirmed answers survive the churn; unconfirmed ones wash out
  - capacity holds and evictions actually happen
  - the pattern matcher stays bounded (no dead patterns accumulate)
  - a confirmed answer still retrieves first, above the pipeline's default
    confidence threshold
  - repeated decay returns idle entries to no-history, removing min_hit_rate
    protection

Run this after changes to scoring, eviction, or the learning path -- it fails
(exit 1) when any invariant regresses.

Usage:
    python eval/run_learning_eval.py
    python eval/run_learning_eval.py --json report.json
"""

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from engram import eviction, metrics
from engram.config import engram_config
from engram.constants import EvictionPolicy
from engram.core import Engram

CAPACITY = 8
CONFIRMATION_ROUNDS = 5
CHURN_ONE_OFFS = 60
CONFIDENCE_THRESHOLD = 0.7

PROVEN_PAIRS = [
    ("how do I reset my password", "Click the reset link on the login page."),
    ("what are the support hours", "Support is available 9 to 5 weekdays."),
    ("where is the data stored", "All data is stored in the EU region."),
    ("how do I cancel my subscription", "Open billing settings and choose cancel."),
]


def check(checks: list, name: str, ok: bool, detail: str) -> None:
    """Record one invariant check."""
    checks.append({"name": name, "ok": bool(ok), "detail": detail})


def run_simulation() -> list:
    """Run the usage simulation and return the list of invariant checks."""
    config = engram_config(
        capacity=CAPACITY,
        eviction_policy=EvictionPolicy.HIT_RATE,
        min_hit_rate=0.4,
    )
    engram = Engram(config=config)
    checks: list = []

    # Learn the proven answers, then confirm them through repeated use.
    proven_ids = {}
    for question, answer in PROVEN_PAIRS:
        proven_ids[question] = engram.learn_from_response(question, answer)
    for _ in range(CONFIRMATION_ROUNDS):
        for question, _ in PROVEN_PAIRS:
            result = engram.query(question)
            if result["matches"]:
                top_stmt, _ = result["matches"][0]
                engram.record_hit(result["keywords"], statement_id=top_stmt["id"])

    # Churn: one-off LLM responses that nobody ever confirms.
    for i in range(CHURN_ONE_OFFS):
        engram.learn_from_response(f"one off question number {i}", f"One-off answer {i}.")

    # Invariant: every confirmed answer survived the churn.
    survivors = sum(1 for stmt_id in proven_ids.values() if engram.get_statement(stmt_id))
    check(
        checks,
        "proven answers survive churn",
        survivors == len(PROVEN_PAIRS),
        f"{survivors}/{len(PROVEN_PAIRS)} confirmed answers still stored",
    )

    # Invariant: capacity held and eviction actually ran.
    stats = metrics.get_metrics(engram)
    check(
        checks,
        "capacity holds under churn",
        stats["dynamic_count"] <= CAPACITY,
        f"dynamic_count={stats['dynamic_count']} capacity={CAPACITY}",
    )
    check(
        checks,
        "evictions happen",
        stats["eviction_count"] > 0,
        f"eviction_count={stats['eviction_count']}",
    )

    # Invariant: learned entries create no patterns, so the matcher is bounded.
    check(
        checks,
        "matcher stays bounded",
        len(engram.pattern_matcher) == 0,
        f"pattern entries={len(engram.pattern_matcher)}",
    )

    # Invariant: a confirmed answer retrieves first, above the pipeline's
    # default confidence threshold, after all the churn.
    question, answer = PROVEN_PAIRS[0]
    result = engram.query(question)
    top_text = result["matches"][0][0]["text"] if result["matches"] else ""
    top_score = result["matches"][0][1] if result["matches"] else 0.0
    check(
        checks,
        "confirmed answer retrieves confidently",
        top_text == answer and top_score >= CONFIDENCE_THRESHOLD,
        f"top={top_text!r} score={top_score:.3f} threshold={CONFIDENCE_THRESHOLD}",
    )

    # Invariant: repeated decay ages idle entries back to no-history, so
    # min_hit_rate protection lapses and they become evictable again.
    for _ in range(CONFIRMATION_ROUNDS):
        metrics.decay_statistics(engram, factor=0.5)
    candidates = {stmt["id"] for _, stmt in eviction.get_eviction_candidates(engram)}
    decayed_out = sum(1 for stmt_id in proven_ids.values() if engram.get_statement(stmt_id) and stmt_id in candidates)
    check(
        checks,
        "decay removes stale protection",
        decayed_out > 0,
        f"{decayed_out} previously protected entries evictable after decay",
    )

    return checks


def main() -> int:
    """Run the learning-loop evaluation and print a report."""
    parser = argparse.ArgumentParser(description="Engram learning-loop evaluation")
    parser.add_argument("--json", default="", help="Write a JSON report to this path")
    args = parser.parse_args()

    checks = run_simulation()
    passed = [c for c in checks if c["ok"]]
    failed = [c for c in checks if not c["ok"]]

    print("ENGRAM Learning-Loop Eval")
    print("=" * 64)
    for c in checks:
        marker = "[DONE]" if c["ok"] else "[FAILED]"
        print(f"{marker} {c['name']}")
        print(f"         {c['detail']}")
    print()
    print(f"SUMMARY passed={len(passed)} failed={len(failed)}")

    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump({"checks": checks}, f, indent=2)
        print(f"Wrote JSON report: {args.json}")

    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
