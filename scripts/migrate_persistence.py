"""Migrate an Engram store to a distinct, validated persistence output."""

import argparse
import json
import sys
from pathlib import Path

REPOSITORY = Path(__file__).resolve().parents[1]
if str(REPOSITORY) not in sys.path:
    sys.path.insert(0, str(REPOSITORY))

from engram.persistence import migrate_persistence_file


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path, help="Existing persistence file; never modified.")
    parser.add_argument("output", type=Path, help="Distinct new output path; must not already exist.")
    args = parser.parse_args()
    report = migrate_persistence_file(args.source, args.output)
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
