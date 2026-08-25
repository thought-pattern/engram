"""Lint the Section 11 rule corpus and its frozen engineering regressions."""

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

REPOSITORY = Path(__file__).resolve().parents[1]
if str(REPOSITORY) not in sys.path:
    sys.path.insert(0, str(REPOSITORY))

from engram.constants import QueryOperator
from engram.rewrite import RewriteEngine, lint_rewrite_corpus, load_default_rewrite_corpus
from scripts.benchmark_metadata import benchmark_source_state, recorded_at

DEFAULT_CASES = Path("eval/section11-rewrite-v1.json")
DEFAULT_OUTPUT = Path("eval/results/rewrite/lint-2026-08-20.json")


def _cases(path: Path) -> list[dict[str, object]]:
    decoded = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(decoded, dict) or decoded.get("schema_version") != 1 or not isinstance(decoded.get("cases"), list):
        raise ValueError("rewrite evaluation corpus must be a schema-1 object with cases")
    cases = decoded["cases"]
    if not all(isinstance(case, dict) for case in cases):
        raise ValueError("rewrite evaluation cases must be objects")
    return cases


def lint(cases_path: Path = DEFAULT_CASES) -> dict[str, object]:
    rules = load_default_rewrite_corpus()
    structural = list(lint_rewrite_corpus(rules))
    engine = RewriteEngine(rules)
    regressions = []
    for raw_case in _cases(cases_path):
        case = dict(raw_case)
        case_id = str(case.get("case_id", ""))
        try:
            execution = engine.rewrite(
                str(case["input"]),
                operator=QueryOperator(str(case["operator"])),
                subject=str(case["subject"]),
                inherited_subject=case["inherited_subject"] is True,
            )
        except (KeyError, ValueError) as error:
            regressions.append({"severity": "error", "code": "malformed_regression", "case_id": case_id, "detail": str(error)})
            continue
        if execution["final_text"] != case.get("expected_final"):
            regressions.append(
                {
                    "severity": "error",
                    "code": "unexpected_identity_loss",
                    "case_id": case_id,
                    "detail": "final retrieval representation differs from the frozen semantic reduction",
                }
            )
        expected_rewrite = case.get("should_rewrite") is True
        if bool(execution["chain"]) != expected_rewrite:
            regressions.append(
                {
                    "severity": "error",
                    "code": "unexpected_rewrite" if execution["chain"] else "unreachable_rule",
                    "case_id": case_id,
                    "detail": "rewrite application differs from the frozen expectation",
                }
            )
    findings = [*structural, *regressions]
    counts = Counter(str(finding["code"]) for finding in findings)
    errors = sum(finding["severity"] == "error" for finding in findings)
    return {
        "schema_version": 1,
        "created_at": recorded_at(),
        "source_state": benchmark_source_state(),
        "inputs": {
            "rule_corpus": "engram/data/rewrite-rules-v1.json",
            "regression_corpus": cases_path.as_posix(),
            "rules": len(rules),
            "regression_cases": len(_cases(cases_path)),
        },
        "checks": [
            "cycles",
            "unreachable_rules",
            "collisions",
            "overbroad_rewrites",
            "duplicate_outputs",
            "unexpected_identity_loss",
        ],
        "finding_counts": dict(sorted(counts.items())),
        "errors": errors,
        "warnings": len(findings) - errors,
        "passed": errors == 0,
        "findings": findings,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", type=Path, default=DEFAULT_CASES)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    report = lint(args.cases)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({"passed": report["passed"], "errors": report["errors"], "warnings": report["warnings"]}))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
