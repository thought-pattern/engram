"""Lint the Section 11 rule corpus and its frozen engineering regressions."""

from argparse import ArgumentParser as argparse_ArgumentParser
from collections import Counter
from json import dumps as json_dumps, loads as json_loads
from pathlib import Path
from sys import path as sys_path

REPOSITORY = Path(__file__).resolve().parents[1]
if str(REPOSITORY) not in sys_path:
    sys_path.insert(0, str(REPOSITORY))

from engram.constants import QueryOperator
from engram.rewrite import RewriteEngine, lint_rewrite_corpus, load_default_rewrite_corpus
from scripts.benchmark_metadata import benchmark_source_state, recorded_at

DEFAULT_CASES = Path("eval/section11-rewrite-v1.json")
DEFAULT_OUTPUT = Path("eval/results/rewrite/lint-2026-08-20.json")


def internal_cases(path: Path) -> list[dict[str, object]]:
    decoded = json_loads(path.read_text(encoding="utf-8"))
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
    for raw_case in internal_cases(cases_path):
        case = dict(raw_case)
        case_id = str(case.get("case_id", ""))
        try:
            execution = engine.rewrite(
                str(case.get("input", "")),
                operator=QueryOperator(str(case.get("operator", ""))),
                subject=str(case.get("subject", "")),
                inherited_subject=case.get("inherited_subject", False) is True,
            )
        except (KeyError, ValueError) as error:
            regressions.append({"severity": "error", "code": "malformed_regression", "case_id": case_id, "detail": str(error)})
            continue
        if execution["final_text"] != case.get("expected_final", ""):
            regressions.append(
                {
                    "severity": "error",
                    "code": "unexpected_identity_loss",
                    "case_id": case_id,
                    "detail": "final retrieval representation differs from the frozen semantic reduction",
                }
            )
        expected_rewrite = case.get("should_rewrite", False) is True
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
    result = {
        "schema_version": 1,
        "created_at": recorded_at(),
        "source_state": benchmark_source_state(),
        "inputs": {
            "rule_corpus": "engram/data/rewrite-rules-v1.json",
            "regression_corpus": cases_path.as_posix(),
            "rules": len(rules),
            "regression_cases": len(internal_cases(cases_path)),
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
    return result


def main() -> int:
    parser = argparse_ArgumentParser(description=__doc__)
    parser.add_argument("--cases", type=Path, default=DEFAULT_CASES)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    report = lint(args.cases)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json_dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json_dumps({"passed": report["passed"], "errors": report["errors"], "warnings": report["warnings"]}))
    result = 0 if report["passed"] else 1
    return result


if __name__ == "__main__":
    raise SystemExit(main())
