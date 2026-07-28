"""Conversation soak rig: drive a scripted multi-turn chat through the pipeline.

Builds a seeded engram in memory (it never touches engram.json), then runs each
turn of a conversation script through pipeline.respond with one persistent
session -- the same path the interactive CLI uses -- and checks every exchange
for mechanical defects:

  - a turn raising an exception
  - an empty response from an answering tier (source "none" is reported as a
    warning, since the caller decides the fallback there)
  - casing polish failures (lowercase sentence start, standalone lowercase i)
  - template artifacts leaking into the response (unresolved {...} tokens)

Softer signals are warnings rather than failures: unanswered turns, three
identical responses in a row (a conversation loop), and slow turns. After the
conversation, the rig checks session hygiene (no scratch predicates, bounded
history) and reports what the store learned along the way.

Usage:
    python eval/run_conversation.py                        # bundled script
    python eval/run_conversation.py --script mine.json     # custom turns
    python eval/run_conversation.py --json report.json     # full report
    python eval/run_conversation.py --quiet                # summary only
"""

import argparse
import json
import os
import sys
import time

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from engram import metrics, pipeline, sessions
from engram.config import engram_config
from engram.constants import Tier
from engram.core import Engram

SESSION_ID = "soak"
SLOW_TURN_SECONDS = 1.0
LOOP_LENGTH = 3  # identical consecutive responses that count as a loop
LOWER_I_FORMS = {"i", "i'm", "i've", "i'll", "i'd"}


def build_seeded_engram() -> Engram:
    """Build an engram instance populated from the bundled seed file."""
    engram = Engram(config=engram_config(learn_user_facts=True))
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


def load_turns(path: str) -> list:
    """Load conversation turns from a script file."""
    script_path = path if path else os.path.join(os.path.dirname(os.path.abspath(__file__)), "conversation.json")
    with open(script_path, encoding="utf-8") as f:
        data = json.load(f)
    turns = data.get("turns", [])
    return turns


def check_response(response: str, source: str) -> tuple[list, list]:
    """Apply the mechanical checks to one response; returns (defects, warnings)."""
    defects: list[str] = []
    warnings: list[str] = []

    if not response:
        if source == "none":
            warnings.append("unanswered (source none)")
        else:
            defects.append(f"empty response from source {source}")
        return defects, warnings

    first_alpha = next((c for c in response if c.isalpha()), "")
    if first_alpha and first_alpha.islower():
        defects.append("response starts lowercase")

    for token in response.split():
        if token.strip(".,!?;:'\"").lower() in LOWER_I_FORMS and token.strip(".,!?;:'\"") in LOWER_I_FORMS:
            defects.append(f"lowercase pronoun in response: {token!r}")
            break

    if "{" in response and "}" in response:
        defects.append("unresolved template token in response")

    return defects, warnings


def run_conversation(turns: list, verbose: bool) -> dict:
    """Run the scripted conversation and collect the per-turn report."""
    engram = build_seeded_engram()
    sessions.create_session(engram, session_id=SESSION_ID)
    baseline = metrics.get_metrics(engram)

    records: list[dict] = []
    recent_responses: list[str] = []

    for number, text in enumerate(turns, 1):
        started = time.perf_counter()
        defects: list[str] = []
        warnings: list[str] = []
        result = {"response": "", "source": "error", "score": 0.0, "pattern": ""}
        try:
            result = pipeline.respond(engram, text, session_id=SESSION_ID)
        except Exception as err:
            defects.append(f"exception: {type(err).__name__}: {err}")
        elapsed = time.perf_counter() - started

        response = result["response"]
        checked_defects, checked_warnings = check_response(response, result["source"])
        defects.extend(checked_defects)
        warnings.extend(checked_warnings)

        if elapsed > SLOW_TURN_SECONDS:
            warnings.append(f"slow turn: {elapsed:.2f}s")

        recent_responses.append(response)
        if len(recent_responses) >= LOOP_LENGTH and len(set(recent_responses[-LOOP_LENGTH:])) == 1 and response:
            warnings.append(f"conversation loop: same response {LOOP_LENGTH} turns running")

        record = {
            "turn": number,
            "input": text,
            "response": response,
            "source": result["source"],
            "score": round(result["score"], 3),
            "pattern": result["pattern"],
            "seconds": round(elapsed, 3),
            "defects": defects,
            "warnings": warnings,
        }
        records.append(record)

        if verbose:
            print(f"{number:>3} You: {text}")
            print(f"    Bot: [{result['source']} {result['score']:.2f}] {response}")
            for defect in defects:
                print(f"    [DEFECT] {defect}")
            for warning in warnings:
                print(f"    [WARNING] {warning}")

    # Post-conversation hygiene checks
    hygiene: list[str] = []
    session = engram.sessions.get(SESSION_ID, {})
    scratch = [name for name in session.get("predicates", {}) if name.startswith("_")]
    if scratch:
        hygiene.append(f"scratch predicates persisted: {scratch}")
    history_size = session.get("history_size", 10)
    if len(session.get("response_history", [])) > history_size:
        hygiene.append("response history exceeds its bound")

    final = metrics.get_metrics(engram)
    learned = [s["pattern"] or s["text"][:60] for s in engram.statements if s["tier"] == Tier.DYNAMIC]

    report = {
        "turns": records,
        "hygiene_defects": hygiene,
        "session_predicates": dict(session.get("predicates", {})),
        "learned_dynamic": learned,
        "metrics_baseline": baseline,
        "metrics_final": final,
    }
    return report


def main() -> int:
    """Run the conversation soak and print a report."""
    parser = argparse.ArgumentParser(description="Engram conversation soak rig")
    parser.add_argument("--script", default="", help="Conversation script (default: eval/conversation.json)")
    parser.add_argument("--json", default="", help="Write the full JSON report to this path")
    parser.add_argument("--quiet", action="store_true", help="Suppress the per-turn transcript")
    args = parser.parse_args()

    turns = load_turns(args.script)
    if not turns:
        print("No turns found in the conversation script", file=sys.stderr)
        return 1

    report = run_conversation(turns, verbose=not args.quiet)

    records = report["turns"]
    defect_turns = [r for r in records if r["defects"]]
    warning_turns = [r for r in records if r["warnings"]]
    sources: dict[str, int] = {}
    for r in records:
        sources[r["source"]] = sources.get(r["source"], 0) + 1
    slowest = max(records, key=lambda r: r["seconds"])

    print()
    print("Conversation Soak Report")
    print("=" * 64)
    print(f"Turns:            {len(records)}")
    print(f"Sources:          {', '.join(f'{k}={v}' for k, v in sorted(sources.items()))}")
    print(f"Defect turns:     {len(defect_turns)}")
    print(f"Warning turns:    {len(warning_turns)}")
    print(f"Hygiene defects:  {len(report['hygiene_defects'])}")
    print(f"Learned entries:  {len(report['learned_dynamic'])}")
    print(f"Slowest turn:     #{slowest['turn']} at {slowest['seconds']:.2f}s")
    for r in defect_turns:
        print(f"  [DEFECT] turn {r['turn']} {r['input']!r}: {'; '.join(r['defects'])}")
    for issue in report["hygiene_defects"]:
        print(f"  [DEFECT] session: {issue}")

    print()
    print(
        f"SUMMARY turns={len(records)} defects={len(defect_turns) + len(report['hygiene_defects'])} warnings={len(warning_turns)}"
    )

    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump(report, f, indent=2)
        print(f"Wrote JSON report: {args.json}")

    return 1 if defect_turns or report["hygiene_defects"] else 0


if __name__ == "__main__":
    sys.exit(main())
