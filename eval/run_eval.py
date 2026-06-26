"""Evaluation harness: cycle prompts through engram and report coverage gaps.

Runs a corpus of natural-language prompts against a freshly seeded engram
instance, classifies how each prompt is answered, and prints the gaps so that
content and engine improvements can be driven from real signal.

The harness is side-effect free on the user's data: it builds state in memory
from data/seed.json and never saves to engram.json.

Classification (pattern path):
    specific  - matched a real, intentional pattern (the desired outcome)
    catchall  - matched only the '*' fallback pattern (no specific response)
    fallback  - no statement backed the response (graph/configured fallback)

A catchall/fallback result is a gap. The matched pattern shown for each prompt
tells you which fix it needs: a near-miss against an existing specific pattern
points to an engine bug; a topic with no related pattern points to a content gap.

Usage:
    python eval/run_eval.py
    python eval/run_eval.py --json path/to/report.json
"""

import argparse
import json
import os
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from engram.config import engram_config
from engram.constants import Tier
from engram.core import Engram

CATCHALL = "*"
WEAK_SCORE = 1.0  # keyword top-score at or below this is a weak retrieval


def build_seeded_engram() -> Engram:
    """Build an engram instance populated from the bundled seed file."""
    engram = Engram(config=engram_config())
    seed_path = os.path.join(REPO_ROOT, "data", "seed.json")
    with open(seed_path, encoding="utf-8") as f:
        seed_data = json.load(f)
    for pair in seed_data.get("pairs", []):
        engram.store(
            pair.get("response", ""),
            tier=Tier.STATIC,
            pattern=pair.get("pattern", ""),
            template=pair.get("template", {}),
        )
    return engram


def load_corpus() -> list:
    """Load evaluation prompts from the corpus file."""
    corpus_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "corpus.json")
    with open(corpus_path, encoding="utf-8") as f:
        data = json.load(f)
    return data.get("prompts", [])


def classify(engram, prompt: dict) -> dict:
    """Run one prompt through both query paths and classify the outcome."""
    text = prompt.get("text", "")
    result = engram.pattern_query(text)
    if result:
        stmt = result[0]
        response = result[2]
        pattern = stmt["pattern"] if stmt else ""
    else:
        pattern = ""
        response = ""

    if pattern == CATCHALL:
        kind = "catchall"
    elif pattern:
        kind = "specific"
    else:
        kind = "fallback"

    # Keyword retrieval signal (the path coverage/keywords analysis uses).
    keyword_result = engram.query(text)
    matches = keyword_result["matches"]
    top_score = matches[0][1] if matches else 0.0
    if matches and top_score > WEAK_SCORE:
        engram.record_hit(keyword_result["keywords"])

    return {
        "text": text,
        "category": prompt.get("category", ""),
        "kind": kind,
        "pattern": pattern,
        "response": response,
        "top_score": round(top_score, 3),
        "keyword_matches": len(matches),
    }


def pct(part: int, total: int) -> str:
    """Format a count as a percentage of the total."""
    if total == 0:
        return "0.0%"
    return f"{100.0 * part / total:.1f}%"


def main() -> int:
    """Run the evaluation corpus and print a coverage report."""
    parser = argparse.ArgumentParser(description="Engram evaluation harness")
    parser.add_argument("--json", default="", help="Write a JSON report to this path")
    args = parser.parse_args()

    engram = build_seeded_engram()
    prompts = load_corpus()
    results = [classify(engram, p) for p in prompts]

    specific = [r for r in results if r["kind"] == "specific"]
    catchall = [r for r in results if r["kind"] == "catchall"]
    fallback = [r for r in results if r["kind"] == "fallback"]
    gaps = catchall + fallback
    total = len(results)

    print("ENGRAM Eval")
    print("=" * 64)
    print(f"Prompts:           {total}")
    print(f"  Specific match:  {len(specific):>3}  ({pct(len(specific), total)})")
    print(f"  Catch-all only:  {len(catchall):>3}  ({pct(len(catchall), total)})")
    print(f"  Fallback/none:   {len(fallback):>3}  ({pct(len(fallback), total)})")
    print()

    if gaps:
        print("Coverage gaps (no specific pattern matched):")
        for r in gaps:
            tag = r["category"] or "?"
            print(f"  [{tag:<12}] {r['text']!r}")
            print(f"                 -> {r['response']!r}")
        print()

    print("Specific matches:")
    for r in specific:
        print(f"  {r['text']!r:<34} -> pattern {r['pattern']!r}")
    print()

    print(f"SUMMARY specific={len(specific)} catchall={len(catchall)} fallback={len(fallback)}")

    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump({"results": results}, f, indent=2)
        print(f"Wrote JSON report: {args.json}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
